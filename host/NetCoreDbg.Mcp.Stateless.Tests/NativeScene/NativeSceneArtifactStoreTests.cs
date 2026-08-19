using System.Reflection;
using System.Runtime.Loader;
using System.Security.Cryptography;
using System.Text;
using Microsoft.Extensions.Time.Testing;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;
using static NetCoreDbg.Mcp.Stateless.Tests.NativeScene.NativeSceneArtifactReflection;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

/// <summary>
/// RED contract for T016's server-owned artifact store. The driver deliberately
/// loads the production assembly by reflection: this test project has no IVT
/// access to the future store implementation.
/// </summary>
public sealed class NativeSceneArtifactStoreTests
{
    private const int InternalChunkBytes = 65_536;
    private const string MediaType = "image/png";
    private const string ArtifactSchemaVersion = "native-scene-artifact/1";

    [Fact]
    public async Task StagedArtifact_IsUnreadableUntilCommit_ThenPublishesAnImmutableDescriptor()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var payload = Payload(513);
        var sessionId = Capability("staged-session");
        var captureId = Capability("staged-capture");
        var staged = await scope.Store.StageAsync(sessionId, captureId, MediaType, ArtifactSchemaVersion, payload);

        AssertOpaqueCapability(staged.ArtifactId);
        AssertFixedNotFound(await scope.Store.ReadAsync(sessionId, staged.ArtifactId, offset: 0, maxBytes: 1));
        Assert.NotEmpty(Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories));

        var descriptor = AssertCommitted(await staged.CommitAsync());

        Assert.Equal(staged.ArtifactId, descriptor.ArtifactId);
        Assert.Equal(captureId, descriptor.CaptureId);
        Assert.Equal(MediaType, descriptor.MediaType);
        Assert.Equal(ArtifactSchemaVersion, descriptor.ArtifactSchemaVersion);
        Assert.Equal(payload.Length, descriptor.ByteLength);
        Assert.Equal(Sha256(payload), descriptor.Sha256);
        AssertImmutableAndNonDisclosingDescriptor(descriptor);
        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 0, maxBytes: payload.Length),
            descriptor,
            offset: 0,
            expectedBytes: payload,
            endOfArtifact: true);
    }

    [Fact]
    public async Task ArtifactIds_AreBase64UrlRepresentableByAtLeast128Bits_AndDoNotRepeatAcrossIndependentStages()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("entropy-session");
        var artifactIds = new HashSet<string>(StringComparer.Ordinal);

        for (var index = 0; index < 64; index++)
        {
            var staged = await scope.Store.StageAsync(
                sessionId,
                Capability($"entropy-capture-{index}"),
                MediaType,
                ArtifactSchemaVersion,
                Payload(index + 1));

            AssertOpaqueCapability(staged.ArtifactId);
            Assert.True(artifactIds.Add(staged.ArtifactId), "A server-minted artifact capability repeated across independent stages.");
        }

        Assert.Equal(64, artifactIds.Count);
    }

    [Fact]
    public async Task ArtifactAuthorization_IsBoundToTheOwningSessionWhileCapturesRemainSeparateAttribution()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var ownerSessionId = Capability("owner-session");
        var foreignSessionId = Capability("foreign-session");
        var firstCaptureId = Capability("first-capture");
        var secondCaptureId = Capability("second-capture");
        var firstPayload = Payload(193);
        var secondPayload = Payload(257);

        var first = AssertCommitted(await (await scope.Store.StageAsync(
            ownerSessionId,
            firstCaptureId,
            MediaType,
            ArtifactSchemaVersion,
            firstPayload)).CommitAsync());
        var second = AssertCommitted(await (await scope.Store.StageAsync(
            ownerSessionId,
            secondCaptureId,
            MediaType,
            ArtifactSchemaVersion,
            secondPayload)).CommitAsync());

        Assert.NotEqual(first.ArtifactId, second.ArtifactId);
        Assert.Equal(firstCaptureId, first.CaptureId);
        Assert.Equal(secondCaptureId, second.CaptureId);
        AssertChunk(
            await scope.Store.ReadAsync(ownerSessionId, first.ArtifactId, offset: 0, maxBytes: firstPayload.Length),
            first,
            offset: 0,
            expectedBytes: firstPayload,
            endOfArtifact: true);
        AssertChunk(
            await scope.Store.ReadAsync(ownerSessionId, second.ArtifactId, offset: 0, maxBytes: secondPayload.Length),
            second,
            offset: 0,
            expectedBytes: secondPayload,
            endOfArtifact: true);
        AssertFixedNotFound(await scope.Store.ReadAsync(foreignSessionId, first.ArtifactId, offset: 0, maxBytes: 1));
        AssertFixedNotFound(await scope.Store.ReadAsync(ownerSessionId, Capability("unknown-artifact"), offset: 0, maxBytes: 1));
    }

    [Fact]
    public async Task Reads_ReturnBoundedBeginningMiddleFinalAndTerminalRangesWithExactEndSemantics()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("range-session");
        var payload = Payload((InternalChunkBytes * 2) + 91);
        var descriptor = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("range-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload)).CommitAsync());

        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 0, maxBytes: 31),
            descriptor,
            offset: 0,
            expectedBytes: payload[..31],
            endOfArtifact: false);
        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 509, maxBytes: 777),
            descriptor,
            offset: 509,
            expectedBytes: payload[509..1286],
            endOfArtifact: false);
        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: payload.Length - 1, maxBytes: InternalChunkBytes),
            descriptor,
            offset: payload.Length - 1,
            expectedBytes: payload[^1..],
            endOfArtifact: true);
        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: payload.Length, maxBytes: 1),
            descriptor,
            offset: payload.Length,
            expectedBytes: [],
            endOfArtifact: true);

        var bounded = await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 0, maxBytes: InternalChunkBytes);
        Assert.True(bounded.BytesRead <= InternalChunkBytes);
    }

    [Fact]
    public async Task Commit_RecordsTheFullHashAndVerifiesEachTouchedFixedSizeChunkBeforeRelease()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("chunk-table-session");
        var payload = Payload((InternalChunkBytes * 3) + 17);
        var descriptor = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("chunk-table-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload)).CommitAsync());

        Assert.Equal(Sha256(payload), descriptor.Sha256);
        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 0, maxBytes: 1),
            descriptor,
            offset: 0,
            expectedBytes: payload[..1],
            endOfArtifact: false);
        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: InternalChunkBytes, maxBytes: 1),
            descriptor,
            offset: InternalChunkBytes,
            expectedBytes: payload[InternalChunkBytes..(InternalChunkBytes + 1)],
            endOfArtifact: false);
        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: InternalChunkBytes * 2, maxBytes: 1),
            descriptor,
            offset: InternalChunkBytes * 2,
            expectedBytes: payload[(InternalChunkBytes * 2)..((InternalChunkBytes * 2) + 1)],
            endOfArtifact: false);
    }

    [Fact]
    public async Task EveryRead_VerifiesOnlyTheRequestedFixedChunksBeforeReleasingTheirBytes()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("touched-chunk-session");
        var payload = Payload((InternalChunkBytes * 3) + 17);
        var descriptor = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("touched-chunk-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload)).CommitAsync());

        var payloadPath = FindCommittedPayloadPath(scope.Root, payload);
        MutateOneByteWithoutChangingIdentityOrLength(payloadPath, (InternalChunkBytes * 2) + 5);

        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 11, maxBytes: 23),
            descriptor,
            offset: 11,
            expectedBytes: payload[11..34],
            endOfArtifact: false);
        AssertIntegrityFailure(await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: InternalChunkBytes * 2, maxBytes: 32));
    }

    [Fact]
    public async Task EveryRead_OnlyReleasesBytesFromTheSameVerifiedUnalignedTwoChunkSnapshotDuringOpenHandleMutation()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("tamper-session");
        var payload = Payload((InternalChunkBytes * 2) + 17);
        var descriptor = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("tamper-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload)).CommitAsync());
        const int offset = InternalChunkBytes - 19;
        const int maxBytes = 64;
        var expectedBytes = payload[offset..(offset + maxBytes)];

        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset, maxBytes),
            descriptor,
            offset,
            expectedBytes,
            endOfArtifact: false);

        var payloadPath = FindCommittedPayloadPath(scope.Root, payload);
        using var mutationHandle = new FileStream(
            payloadPath,
            FileMode.Open,
            FileAccess.ReadWrite,
            FileShare.ReadWrite | FileShare.Delete);
        const long mutationOffset = InternalChunkBytes + 17;
        mutationHandle.Position = mutationOffset;
        var original = mutationHandle.ReadByte();
        Assert.NotEqual(-1, original);

        using var mutationCancellation = new CancellationTokenSource();
        var mutationTask = Task.Run(() => ToggleByteUntilCancelledAsync(
            mutationHandle,
            mutationOffset,
            (byte)original,
            mutationCancellation.Token));
        try
        {
            for (var attempt = 0; attempt < 32; attempt++)
            {
                AssertVerifiedChunkOrIntegrityFailure(
                    await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset, maxBytes),
                    descriptor,
                    offset,
                    expectedBytes,
                    endOfArtifact: false);
            }
        }
        finally
        {
            mutationCancellation.Cancel();
            await mutationTask;
            mutationHandle.Position = mutationOffset;
            mutationHandle.WriteByte((byte)original);
            mutationHandle.Flush(flushToDisk: true);
        }
    }

    [Fact]
    public async Task EveryRead_VerifiesCommittedFileIdentityBeforeReleasingBytes()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("identity-session");
        var payload = Payload(2_049);
        var descriptor = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("identity-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload)).CommitAsync());

        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 0, maxBytes: 32),
            descriptor,
            offset: 0,
            expectedBytes: payload[..32],
            endOfArtifact: false);

        var payloadPath = FindCommittedPayloadPath(scope.Root, payload);
        File.Move(payloadPath, payloadPath + ".replaced");
        File.WriteAllBytes(payloadPath, payload);

        AssertIntegrityFailure(await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 0, maxBytes: 32));
    }

    [Fact]
    public async Task EveryRead_VerifiesCommittedLengthBeforeReleasingBytes()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("length-session");
        var payload = Payload(2_049);
        var descriptor = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("length-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload)).CommitAsync());

        AssertChunk(
            await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 0, maxBytes: 32),
            descriptor,
            offset: 0,
            expectedBytes: payload[..32],
            endOfArtifact: false);

        var payloadPath = FindCommittedPayloadPath(scope.Root, payload);
        using (var append = new FileStream(payloadPath, FileMode.Append, FileAccess.Write, FileShare.Read))
        {
            append.WriteByte(0x5a);
            append.Flush(flushToDisk: true);
        }

        AssertIntegrityFailure(await scope.Store.ReadAsync(sessionId, descriptor.ArtifactId, offset: 0, maxBytes: 32));
    }

    [Fact]
    public async Task UnknownForeignAndUncommittedArtifacts_ReturnOneFixedDisclosureFreeUnavailableEnvelope()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("not-found-session");
        var staged = await scope.Store.StageAsync(
            sessionId,
            Capability("not-found-capture"),
            MediaType,
            ArtifactSchemaVersion,
            Payload(99));

        AssertFixedNotFound(await scope.Store.ReadAsync(sessionId, Capability("not-found-unknown"), offset: 0, maxBytes: 1));
        AssertFixedNotFound(await scope.Store.ReadAsync(sessionId, staged.ArtifactId, offset: 0, maxBytes: 1));

        var descriptor = AssertCommitted(await staged.CommitAsync());
        AssertFixedNotFound(await scope.Store.ReadAsync(Capability("not-found-foreign-session"), descriptor.ArtifactId, offset: 0, maxBytes: 1));
    }

    [Fact]
    public async Task FakeTime_ExpiresCommittedArtifactsAndMetadataPastFourHoursWithoutReadingTheExpiredArtifact()
    {
        var clock = new FakeTimeProvider(new DateTimeOffset(2026, 8, 19, 12, 0, 0, TimeSpan.Zero));
        await using var scope = ArtifactStoreTestScope.Create(clock);
        var sessionId = Capability("expiry-session");
        var payload = Payload(131);
        _ = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("expiry-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload)).CommitAsync());
        var payloadPath = FindCommittedPayloadPath(scope.Root, payload);

        clock.Advance(TimeSpan.FromHours(4) + TimeSpan.FromTicks(1));

        await WaitForAsync(
            () => !File.Exists(payloadPath) && scope.Store.CommittedArtifactMetadataCount == 0,
            "Committed artifact expiry must remove both file bytes and in-memory metadata without a retrieval request.");
        Assert.Empty(Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories));
    }

    [Fact]
    public async Task PerStoreArtifactCountBudget_RejectsFurtherStagingWithoutPublishingAdditionalMetadata()
    {
        await using var scope = ArtifactStoreTestScope.Create(maximumArtifactCount: 2, maximumAggregateBytes: 1_024);
        var sessionId = Capability("count-budget-session");

        for (var index = 0; index < 2; index++)
        {
            _ = AssertCommitted(await (await scope.Store.StageAsync(
                sessionId,
                Capability($"count-budget-capture-{index}"),
                MediaType,
                ArtifactSchemaVersion,
                Payload(17 + index))).CommitAsync());
        }

        var filesBeforeRejection = Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories).OrderBy(static path => path, StringComparer.Ordinal).ToArray();
        await Assert.ThrowsAsync<IOException>(() => scope.Store.StageAsync(
            sessionId,
            Capability("count-budget-overflow"),
            MediaType,
            ArtifactSchemaVersion,
            Payload(1)));

        Assert.Equal(2, scope.Store.CommittedArtifactMetadataCount);
        Assert.Equal(filesBeforeRejection, Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories).OrderBy(static path => path, StringComparer.Ordinal));
    }

    [Fact]
    public async Task PerStoreAggregateByteBudget_RejectsFurtherStagingWithoutPublishingAdditionalMetadata()
    {
        await using var scope = ArtifactStoreTestScope.Create(maximumArtifactCount: 8, maximumAggregateBytes: 15);
        var sessionId = Capability("byte-budget-session");

        _ = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("byte-budget-first"),
            MediaType,
            ArtifactSchemaVersion,
            Payload(7))).CommitAsync());
        _ = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("byte-budget-second"),
            MediaType,
            ArtifactSchemaVersion,
            Payload(8))).CommitAsync());

        var filesBeforeRejection = Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories).OrderBy(static path => path, StringComparer.Ordinal).ToArray();
        await Assert.ThrowsAsync<IOException>(() => scope.Store.StageAsync(
            sessionId,
            Capability("byte-budget-overflow"),
            MediaType,
            ArtifactSchemaVersion,
            Payload(1)));

        Assert.Equal(2, scope.Store.CommittedArtifactMetadataCount);
        Assert.Equal(filesBeforeRejection, Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories).OrderBy(static path => path, StringComparer.Ordinal));
    }

    [Fact]
    public async Task SessionStop_CleansCommittedAndStagedArtifactsAndMakesCleanupIdempotent()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("stop-session");
        var committed = AssertCommitted(await (await scope.Store.StageAsync(
            sessionId,
            Capability("stop-committed-capture"),
            MediaType,
            ArtifactSchemaVersion,
            Payload(71))).CommitAsync());
        var staged = await scope.Store.StageAsync(
            sessionId,
            Capability("stop-staged-capture"),
            MediaType,
            ArtifactSchemaVersion,
            Payload(73));

        await scope.Store.StopSessionAsync(sessionId);
        await scope.Store.StopSessionAsync(sessionId);

        AssertFixedNotFound(await scope.Store.ReadAsync(sessionId, committed.ArtifactId, offset: 0, maxBytes: 1));
        AssertFixedNotFound(await scope.Store.ReadAsync(sessionId, staged.ArtifactId, offset: 0, maxBytes: 1));
        Assert.Empty(Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories));

        await scope.Store.DisposeAsync();
        await scope.Store.DisposeAsync();
        Assert.Empty(Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories));
    }

    [Fact]
    public async Task CommitFailure_DoesNotPublishAnArtifactAndCleansItsStagedBytes()
    {
        await using var scope = ArtifactStoreTestScope.Create();
        var sessionId = Capability("commit-failure-session");
        var payload = Payload(151);
        var staged = await scope.Store.StageAsync(
            sessionId,
            Capability("commit-failure-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload);

        File.Delete(FindCommittedPayloadPath(scope.Root, payload));

        AssertCommitFailed(await staged.CommitAsync());
        AssertFixedNotFound(await scope.Store.ReadAsync(sessionId, staged.ArtifactId, offset: 0, maxBytes: 1));
        Assert.Empty(Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories));
    }

    [Fact]
    public async Task StagedArtifact_AbortDiscardsPrivateStateReleasesCapacityAndIsIdempotent()
    {
        var payload = Payload(97);
        await using var scope = ArtifactStoreTestScope.Create(maximumArtifactCount: 1, maximumAggregateBytes: payload.Length);
        var sessionId = Capability("abort-session");
        var staged = await scope.Store.StageAsync(
            sessionId,
            Capability("abort-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload);

        Assert.Equal(1, scope.Store.StagedArtifactMetadataCount);

        await staged.AbortAsync();
        await staged.AbortAsync();

        Assert.Equal(0, scope.Store.StagedArtifactMetadataCount);
        Assert.Equal(0, scope.Store.CommittedArtifactMetadataCount);
        Assert.Empty(Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories));
        AssertFixedNotFound(await scope.Store.ReadAsync(sessionId, staged.ArtifactId, offset: 0, maxBytes: 1));

        var replacement = await scope.Store.StageAsync(
            sessionId,
            Capability("abort-replacement-capture"),
            MediaType,
            ArtifactSchemaVersion,
            payload);
        await replacement.AbortAsync();

        Assert.Equal(0, scope.Store.StagedArtifactMetadataCount);
        Assert.Empty(Directory.EnumerateFiles(scope.Root, "*", SearchOption.AllDirectories));
    }

    private static NativeSceneArtifactDescriptorSnapshot AssertCommitted(NativeSceneArtifactCommitResultSnapshot result)
    {
        Assert.Null(result.Code);
        Assert.Null(result.Message);
        Assert.NotNull(result.Descriptor);
        return result.Descriptor!;
    }

    private static void AssertCommitFailed(NativeSceneArtifactCommitResultSnapshot result)
    {
        Assert.Equal("ARTIFACT_WRITE_FAILED", result.Code);
        Assert.Null(result.Descriptor);
    }

    private static void AssertOpaqueCapability(string value)
    {
        Assert.Matches("^[A-Za-z0-9_-]{22,86}$", value);
        Assert.True(DecodeBase64Url(value).Length >= 16, "Capability must encode at least 128 bits.");
    }

    private static void AssertImmutableAndNonDisclosingDescriptor(NativeSceneArtifactDescriptorSnapshot descriptor)
    {
        Assert.All(descriptor.PublicProperties, static property => Assert.Null(property.SetMethod));
        Assert.DoesNotContain(descriptor.PublicProperties, static property =>
            property.Name.Contains("path", StringComparison.OrdinalIgnoreCase) ||
            property.Name.Contains("root", StringComparison.OrdinalIgnoreCase) ||
            property.Name.Contains("chunk", StringComparison.OrdinalIgnoreCase) ||
            property.Name.Contains("storage", StringComparison.OrdinalIgnoreCase));
    }

    private static void AssertChunk(
        NativeSceneArtifactReadResultSnapshot result,
        NativeSceneArtifactDescriptorSnapshot descriptor,
        long offset,
        byte[] expectedBytes,
        bool endOfArtifact)
    {
        Assert.Equal("capture_artifact_chunk", result.Kind);
        Assert.Equal(descriptor.ArtifactId, result.ArtifactId);
        Assert.Equal(offset, result.Offset);
        Assert.Equal(expectedBytes.Length, result.BytesRead);
        Assert.Equal(expectedBytes, Convert.FromBase64String(result.DataBase64));
        Assert.Equal(endOfArtifact, result.EndOfArtifact);
        Assert.Equal(descriptor.MediaType, result.MediaType);
        Assert.Equal(descriptor.ByteLength, result.ByteLength);
        Assert.Equal(descriptor.Sha256, result.Sha256);
        Assert.Equal(descriptor.ArtifactSchemaVersion, result.ArtifactSchemaVersion);
        Assert.DoesNotContain(result.PublicPropertyNames, static name =>
            name.Contains("path", StringComparison.OrdinalIgnoreCase) ||
            name.Contains("root", StringComparison.OrdinalIgnoreCase) ||
            name.Contains("chunkhash", StringComparison.OrdinalIgnoreCase));
    }

    private static void AssertFixedNotFound(NativeSceneArtifactReadResultSnapshot result)
    {
        Assert.Equal("tool_error", result.Kind);
        Assert.Equal("read_capture_artifact", result.Tool);
        Assert.Equal("ARTIFACT_NOT_FOUND", result.Code);
        Assert.Equal("Artifact is not available.", result.Message);
        Assert.Equal(["Code", "Kind", "Message", "Tool"], result.PublicPropertyNames.OrderBy(static name => name, StringComparer.Ordinal));
    }

    private static void AssertIntegrityFailure(NativeSceneArtifactReadResultSnapshot result)
    {
        Assert.Equal("tool_error", result.Kind);
        Assert.Equal("read_capture_artifact", result.Tool);
        Assert.Equal("ARTIFACT_INTEGRITY_FAILED", result.Code);
        Assert.Equal(["Code", "Kind", "Message", "Tool"], result.PublicPropertyNames.OrderBy(static name => name, StringComparer.Ordinal));
    }

    private static void AssertVerifiedChunkOrIntegrityFailure(
        NativeSceneArtifactReadResultSnapshot result,
        NativeSceneArtifactDescriptorSnapshot descriptor,
        long offset,
        byte[] expectedBytes,
        bool endOfArtifact)
    {
        if (StringComparer.Ordinal.Equals("capture_artifact_chunk", result.Kind))
        {
            AssertChunk(result, descriptor, offset, expectedBytes, endOfArtifact);
            return;
        }

        AssertIntegrityFailure(result);
    }

    private static string Capability(string seed)
    {
        var bytes = SHA256.HashData(Encoding.UTF8.GetBytes(seed));
        return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }

    private static byte[] DecodeBase64Url(string value)
    {
        var padded = value.Replace('-', '+').Replace('_', '/');
        padded += new string('=', (4 - (padded.Length % 4)) % 4);
        return Convert.FromBase64String(padded);
    }

    private static string FindCommittedPayloadPath(string root, byte[] expectedBytes)
    {
        var matches = Directory
            .EnumerateFiles(root, "*", SearchOption.AllDirectories)
            .Where(path => File.ReadAllBytes(path).AsSpan().SequenceEqual(expectedBytes))
            .ToArray();
        return Assert.Single(matches);
    }

    private static void MutateOneByteWithoutChangingIdentityOrLength(string path, long offset)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.ReadWrite, FileShare.Read);
        stream.Position = offset;
        var original = stream.ReadByte();
        Assert.NotEqual(-1, original);
        stream.Position = offset;
        stream.WriteByte((byte)(original ^ byte.MaxValue));
        stream.Flush(flushToDisk: true);
    }

    private static async Task WaitForAsync(Func<bool> condition, string failureMessage)
    {
        for (var poll = 0; poll < 64; poll++)
        {
            if (condition())
            {
                return;
            }

            await Task.Yield();
        }

        Assert.True(condition(), failureMessage);
    }

    private static async Task ToggleByteUntilCancelledAsync(
        FileStream stream,
        long offset,
        byte original,
        CancellationToken cancellationToken)
    {
        var alternate = (byte)(original ^ byte.MaxValue);
        while (!cancellationToken.IsCancellationRequested)
        {
            stream.Position = offset;
            stream.WriteByte(alternate);
            stream.Flush(flushToDisk: true);
            stream.Position = offset;
            stream.WriteByte(original);
            stream.Flush(flushToDisk: true);
            await Task.Yield();
        }
    }

    private static byte[] Payload(int length) => Enumerable.Range(0, length).Select(static index => (byte)((index * 31) % 251)).ToArray();

    private static string Sha256(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
}

internal sealed class ArtifactStoreTestScope : IAsyncDisposable
{
    private bool _disposed;

    private ArtifactStoreTestScope(string root, NativeSceneArtifactStoreDriver store)
    {
        Root = root;
        Store = store;
    }

    public string Root { get; }

    public NativeSceneArtifactStoreDriver Store { get; }

    public static ArtifactStoreTestScope Create(
        FakeTimeProvider? clock = null,
        int? maximumArtifactCount = null,
        long? maximumAggregateBytes = null)
    {
        var root = Path.Combine(RepositoryLayout.ScratchRoot, $"native-scene-artifact-{Guid.NewGuid():N}");
        Directory.CreateDirectory(root);
        return new ArtifactStoreTestScope(
            root,
            NativeSceneArtifactStoreDriver.Create(
                root,
                clock ?? new FakeTimeProvider(),
                maximumArtifactCount,
                maximumAggregateBytes));
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        try
        {
            await Store.DisposeAsync();
        }
        finally
        {
            if (Directory.Exists(Root))
            {
                Directory.Delete(Root, recursive: true);
            }
        }
    }
}

/// <summary>
/// The exact T016 reflection contract. It leaves production types internal and
/// requires no InternalsVisibleTo grant from the host assembly.
/// </summary>
internal sealed class NativeSceneArtifactStoreDriver : IAsyncDisposable
{
    private const string ProductionAssemblyName = "NetCoreDbg.Mcp.Stateless";
    private const string StoreTypeName = "NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneArtifactStore";

    private readonly object _store;
    private readonly MethodInfo _stageAsync;
    private readonly MethodInfo _readAsync;
    private readonly MethodInfo _stopSessionAsync;
    private readonly FieldInfo _stagedArtifactMetadata;
    private readonly FieldInfo _artifactMetadata;

    private NativeSceneArtifactStoreDriver(
        object store,
        MethodInfo stageAsync,
        MethodInfo readAsync,
        MethodInfo stopSessionAsync,
        FieldInfo stagedArtifactMetadata,
        FieldInfo artifactMetadata)
    {
        _store = store;
        _stageAsync = stageAsync;
        _readAsync = readAsync;
        _stopSessionAsync = stopSessionAsync;
        _stagedArtifactMetadata = stagedArtifactMetadata;
        _artifactMetadata = artifactMetadata;
    }

    public static NativeSceneArtifactStoreDriver Create(
        string root,
        TimeProvider timeProvider,
        int? maximumArtifactCount = null,
        long? maximumAggregateBytes = null)
    {
        Assert.Equal(maximumArtifactCount.HasValue, maximumAggregateBytes.HasValue);

        var assembly = LoadProductionAssembly();
        var storeType = assembly.GetType(StoreTypeName, throwOnError: false)
            ?? throw new InvalidOperationException($"Missing production contract: type '{StoreTypeName}' is absent from '{assembly.Location}'. T016 must implement it without changing this suite.");
        Assert.False(storeType.IsPublic, "NativeSceneArtifactStore must remain an internal host authority.");
        Assert.True(typeof(IAsyncDisposable).IsAssignableFrom(storeType), "NativeSceneArtifactStore must support idempotent asynchronous cleanup.");

        var hasConfiguredBudgets = maximumArtifactCount.HasValue;
        Type[] constructorParameterTypes = hasConfiguredBudgets
            ? [typeof(string), typeof(TimeProvider), typeof(int), typeof(long)]
            : [typeof(string), typeof(TimeProvider)];
        var constructor = storeType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: constructorParameterTypes,
            modifiers: null);
        Assert.NotNull(constructor);
        Assert.False(constructor!.IsPublic, "The controlled artifact root and test-only capacity limits are internal host construction concerns, never caller-selected MCP input.");

        var stageAsync = RequireGenericTaskMethod(
            storeType,
            "StageAsync",
            typeof(string),
            typeof(string),
            typeof(string),
            typeof(string),
            typeof(ReadOnlyMemory<byte>),
            typeof(CancellationToken));
        var readAsync = RequireGenericTaskMethod(
            storeType,
            "ReadAsync",
            typeof(string),
            typeof(string),
            typeof(long),
            typeof(int),
            typeof(CancellationToken));
        var stopSessionAsync = RequireTaskMethod(storeType, "StopSessionAsync", typeof(string), typeof(CancellationToken));
        var stagedArtifactMetadata = storeType.GetField("_staged", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(stagedArtifactMetadata);
        var artifactMetadata = storeType.GetField("_artifacts", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(artifactMetadata);

        object?[] constructorArguments = hasConfiguredBudgets
            ? [root, timeProvider, maximumArtifactCount!.Value, maximumAggregateBytes!.Value]
            : [root, timeProvider];
        var store = constructor.Invoke(constructorArguments);
        Assert.NotNull(store);
        return new NativeSceneArtifactStoreDriver(store!, stageAsync, readAsync, stopSessionAsync, stagedArtifactMetadata!, artifactMetadata!);
    }

    public async Task<NativeSceneArtifactStagingDriver> StageAsync(
        string debugSessionId,
        string captureId,
        string mediaType,
        string artifactSchemaVersion,
        byte[] bytes,
        CancellationToken cancellationToken = default)
    {
        var staged = await AwaitTaskResultAsync(_stageAsync.Invoke(
            _store,
            [debugSessionId, captureId, mediaType, artifactSchemaVersion, new ReadOnlyMemory<byte>(bytes), cancellationToken]), "StageAsync");
        return new NativeSceneArtifactStagingDriver(staged);
    }

    public async Task<NativeSceneArtifactReadResultSnapshot> ReadAsync(
        string debugSessionId,
        string artifactId,
        long offset,
        int maxBytes,
        CancellationToken cancellationToken = default)
    {
        var result = await AwaitTaskResultAsync(_readAsync.Invoke(
            _store,
            [debugSessionId, artifactId, offset, maxBytes, cancellationToken]), "ReadAsync");
        return new NativeSceneArtifactReadResultSnapshot(result);
    }

    public async Task StopSessionAsync(string debugSessionId, CancellationToken cancellationToken = default)
    {
        var task = Assert.IsAssignableFrom<Task>(_stopSessionAsync.Invoke(_store, [debugSessionId, cancellationToken]));
        await task;
    }

    public int CommittedArtifactMetadataCount
    {
        get
        {
            var metadata = _artifactMetadata.GetValue(_store);
            Assert.NotNull(metadata);
            var count = metadata!.GetType().GetProperty("Count", BindingFlags.Instance | BindingFlags.Public);
            Assert.NotNull(count);
            return Assert.IsType<int>(count!.GetValue(metadata));
        }
    }

    public int StagedArtifactMetadataCount
    {
        get
        {
            var metadata = _stagedArtifactMetadata.GetValue(_store);
            Assert.NotNull(metadata);
            var count = metadata!.GetType().GetProperty("Count", BindingFlags.Instance | BindingFlags.Public);
            Assert.NotNull(count);
            return Assert.IsType<int>(count!.GetValue(metadata));
        }
    }

    public async ValueTask DisposeAsync()
    {
        var asyncDisposable = Assert.IsAssignableFrom<IAsyncDisposable>(_store);
        await asyncDisposable.DisposeAsync();
    }

    private static Assembly LoadProductionAssembly()
    {
        var productionProject = Path.Combine(RepositoryLayout.Root, "host", ProductionAssemblyName, $"{ProductionAssemblyName}.csproj");
        Assert.True(File.Exists(productionProject), $"Missing production project: '{productionProject}'.");
        var assemblyPath = TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root,
            Path.Combine("host", ProductionAssemblyName),
            ProductionAssemblyName);
        return AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyPath);
    }

    private static MethodInfo RequireGenericTaskMethod(Type type, string name, params Type[] parameterTypes)
    {
        var method = RequireMethod(type, name, parameterTypes);
        Assert.True(
            method.ReturnType.IsGenericType && method.ReturnType.GetGenericTypeDefinition() == typeof(Task<>),
            $"Missing production contract: {type.FullName}.{name} must return Task<T>.");
        return method;
    }

    private static MethodInfo RequireTaskMethod(Type type, string name, params Type[] parameterTypes)
    {
        var method = RequireMethod(type, name, parameterTypes);
        Assert.True(typeof(Task).IsAssignableFrom(method.ReturnType), $"Missing production contract: {type.FullName}.{name} must return Task.");
        return method;
    }

    private static MethodInfo RequireMethod(Type type, string name, params Type[] parameterTypes)
    {
        var method = type.GetMethod(
            name,
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: parameterTypes,
            modifiers: null);
        Assert.NotNull(method);
        Assert.False(method!.IsPublic, $"{type.FullName}.{name} must remain an internal host operation.");
        return method;
    }

    private static async Task<object> AwaitTaskResultAsync(object? operation, string operationName)
    {
        var task = Assert.IsAssignableFrom<Task>(operation);
        await task;
        var result = task.GetType().GetProperty("Result", BindingFlags.Instance | BindingFlags.Public)?.GetValue(task);
        Assert.NotNull(result);
        return result!;
    }
}

internal sealed class NativeSceneArtifactStagingDriver
{
    private readonly object _staged;
    private readonly MethodInfo _commitAsync;

    public NativeSceneArtifactStagingDriver(object staged)
    {
        _staged = staged;
        ArtifactId = RequiredString(staged, "ArtifactId");
        _commitAsync = RequiredGenericTaskMethod(staged.GetType(), "CommitAsync", typeof(CancellationToken));
    }

    public string ArtifactId { get; }

    public async Task<NativeSceneArtifactCommitResultSnapshot> CommitAsync(CancellationToken cancellationToken = default)
    {
        var task = Assert.IsAssignableFrom<Task>(_commitAsync.Invoke(_staged, [cancellationToken]));
        await task;
        var result = task.GetType().GetProperty("Result", BindingFlags.Instance | BindingFlags.Public)?.GetValue(task);
        Assert.NotNull(result);
        return new NativeSceneArtifactCommitResultSnapshot(result!);
    }

    public async Task AbortAsync(CancellationToken cancellationToken = default)
    {
        var abortAsync = RequiredTaskMethod(_staged.GetType(), "AbortAsync", typeof(CancellationToken));
        var task = Assert.IsAssignableFrom<Task>(abortAsync.Invoke(_staged, [cancellationToken]));
        await task;
    }

    private static MethodInfo RequiredGenericTaskMethod(Type type, string name, params Type[] parameterTypes)
    {
        var method = type.GetMethod(
            name,
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: parameterTypes,
            modifiers: null);
        Assert.NotNull(method);
        Assert.True(
            method!.ReturnType.IsGenericType && method.ReturnType.GetGenericTypeDefinition() == typeof(Task<>),
            $"Missing production contract: {type.FullName}.{name} must return Task<T>.");
        return method;
    }

    private static MethodInfo RequiredTaskMethod(Type type, string name, params Type[] parameterTypes)
    {
        var method = type.GetMethod(
            name,
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: parameterTypes,
            modifiers: null);
        Assert.NotNull(method);
        Assert.False(method!.IsPublic, $"{type.FullName}.{name} must remain an internal host operation.");
        Assert.True(typeof(Task).IsAssignableFrom(method.ReturnType), $"Missing production contract: {type.FullName}.{name} must return Task.");
        return method;
    }

    private static string RequiredString(object value, string propertyName) =>
        Assert.IsType<string>(RequiredProperty(value.GetType(), propertyName).GetValue(value));

    private static PropertyInfo RequiredProperty(Type type, string name)
    {
        var property = type.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        Assert.NotNull(property);
        Assert.True(property!.CanRead, $"Missing production contract: {type.FullName}.{name} must be readable.");
        return property;
    }
}

internal sealed class NativeSceneArtifactCommitResultSnapshot
{
    private readonly object _result;

    public NativeSceneArtifactCommitResultSnapshot(object result)
    {
        _result = result;
    }

    public string? Code => OptionalString(_result, "Code");

    public string? Message => OptionalString(_result, "Message");

    public NativeSceneArtifactDescriptorSnapshot? Descriptor
    {
        get
        {
            var property = OptionalProperty(_result.GetType(), "Descriptor");
            return property?.GetValue(_result) is { } descriptor ? new NativeSceneArtifactDescriptorSnapshot(descriptor) : null;
        }
    }
}

internal sealed class NativeSceneArtifactDescriptorSnapshot
{
    private readonly object _descriptor;

    public NativeSceneArtifactDescriptorSnapshot(object descriptor)
    {
        _descriptor = descriptor;
    }

    public string ArtifactId => RequiredString(_descriptor, "ArtifactId");

    public string CaptureId => RequiredString(_descriptor, "CaptureId");

    public string MediaType => RequiredString(_descriptor, "MediaType");

    public long ByteLength => RequiredInt64(_descriptor, "ByteLength");

    public string Sha256 => RequiredString(_descriptor, "Sha256");

    public string ArtifactSchemaVersion => RequiredString(_descriptor, "ArtifactSchemaVersion");

    public PropertyInfo[] PublicProperties => _descriptor.GetType()
        .GetProperties(BindingFlags.Instance | BindingFlags.Public)
        .Where(static property => property.CanRead)
        .ToArray();
}

internal sealed class NativeSceneArtifactReadResultSnapshot
{
    private readonly object _result;

    public NativeSceneArtifactReadResultSnapshot(object result)
    {
        _result = result;
    }

    public string Kind => RequiredString(_result, "Kind");

    public string? Tool => OptionalString(_result, "Tool");

    public string? Code => OptionalString(_result, "Code");

    public string? Message => OptionalString(_result, "Message");

    public string ArtifactId => RequiredString(_result, "ArtifactId");

    public long Offset => RequiredInt64(_result, "Offset");

    public int BytesRead => RequiredInt32(_result, "BytesRead");

    public string DataBase64 => RequiredString(_result, "DataBase64");

    public bool EndOfArtifact => RequiredBoolean(_result, "EndOfArtifact");

    public string MediaType => RequiredString(_result, "MediaType");

    public long ByteLength => RequiredInt64(_result, "ByteLength");

    public string Sha256 => RequiredString(_result, "Sha256");

    public string ArtifactSchemaVersion => RequiredString(_result, "ArtifactSchemaVersion");

    public string[] PublicPropertyNames => _result.GetType()
        .GetProperties(BindingFlags.Instance | BindingFlags.Public)
        .Where(static property => property.CanRead)
        .Select(static property => property.Name)
        .ToArray();
}

internal static class NativeSceneArtifactReflection
{
    public static string RequiredString(object value, string propertyName) =>
        Assert.IsType<string>(RequiredProperty(value.GetType(), propertyName).GetValue(value));

    public static long RequiredInt64(object value, string propertyName)
    {
        var actual = RequiredProperty(value.GetType(), propertyName).GetValue(value);
        Assert.NotNull(actual);
        return Convert.ToInt64(actual, System.Globalization.CultureInfo.InvariantCulture);
    }

    public static int RequiredInt32(object value, string propertyName)
    {
        var actual = RequiredProperty(value.GetType(), propertyName).GetValue(value);
        Assert.NotNull(actual);
        return Convert.ToInt32(actual, System.Globalization.CultureInfo.InvariantCulture);
    }

    public static bool RequiredBoolean(object value, string propertyName) =>
        Assert.IsType<bool>(RequiredProperty(value.GetType(), propertyName).GetValue(value));

    public static string? OptionalString(object value, string propertyName) =>
        OptionalProperty(value.GetType(), propertyName)?.GetValue(value) as string;

    public static PropertyInfo RequiredProperty(Type type, string name)
    {
        var property = OptionalProperty(type, name);
        Assert.NotNull(property);
        Assert.True(property!.CanRead, $"Missing production contract: {type.FullName}.{name} must be readable.");
        return property;
    }

    public static PropertyInfo? OptionalProperty(Type type, string name) =>
        type.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
}
