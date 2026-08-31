using System.Collections.Immutable;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeSceneVisualEvidenceTests
{
    private const string ActiveProtocolVersion = "native-scene-probe/1";
    private const string ActiveSchemaVersion = "native-scene-probe.schema/1";
    private const int ArtifactReadMaxBytes = 65_536;
    private static readonly ImmutableArray<string> CandidateIdentityFailureCodes = ImmutableArray.Create(
        "CANDIDATE_MISMATCH",
        "OBSERVER_UNAVAILABLE");

    [Fact]
    public async Task BoundControlledSession_CapturesCompactLosslessVisualEvidenceAndReconstructsArtifact()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }
        await using var driver = await StartWindowedDescendantDriverAsync();
        var debugSessionId = await StartDebugAsync(driver, "visual-start");
        var declaration = (await CallToolAsync(
            driver,
            "get_ui_probe_capabilities",
            CapabilityArguments(debugSessionId),
            "visual-capabilities",
            isError: false)).StructuredContent;
        var candidate = Object(declaration["candidate"]);
        Assert.Equal(await driver.ReadDescendantProcessIdAsync(), Integer(candidate["processId"]));

        var arguments = VisualCaptureArguments(debugSessionId, candidate);
        var capture = await CallToolAsync(
            driver,
            "capture_visual_evidence",
            arguments,
            "visual-capture",
            isError: false);
        var manifest = capture.StructuredContent;

        AssertSchemaValid("capture_visual_evidence", manifest);
        AssertCompactManifest(capture.Result, manifest, arguments, candidate);

        var losslessDescriptor = Assert.Single(
            Array(manifest["artifacts"]).Select(Object),
            descriptor => Text(descriptor["mediaType"]) == "image/png" &&
                          Text(descriptor["evidenceGrade"]) == "lossless_visual");
        AssertLosslessDescriptor(manifest, losslessDescriptor);
        Assert.All(
            Array(manifest["artifacts"]).Select(Object).Where(descriptor => Text(descriptor["mediaType"]) == "image/webp"),
            descriptor => Assert.Equal("preview_only", Text(descriptor["evidenceGrade"])));

        var c015 = CorpusExpected("C015-preview-is-independent-and-non-authoritative");
        var requiredArtifact = Object(Assert.Single(Array(c015["requiredArtifacts"])));
        Assert.Equal("image/png", Text(requiredArtifact["mediaType"]));
        Assert.Equal("lossless_visual", Text(requiredArtifact["evidenceGrade"]));
        Assert.True(Boolean(requiredArtifact["independentRasterCaptureId"]));
        var optionalPreview = Object(Assert.Single(Array(c015["optionalArtifactsIfPresent"])));
        Assert.Equal("image/webp", Text(optionalPreview["mediaType"]));
        Assert.Equal("preview_only", Text(optionalPreview["evidenceGrade"]));
        Assert.True(Boolean(optionalPreview["independentRasterCaptureId"]));
        Assert.True(Boolean(optionalPreview["nonAuthoritative"]));
        var forbiddenPreviewClaims = Array(c015["mustNotClaim"]).Select(Text).ToHashSet(StringComparer.Ordinal);
        Assert.DoesNotContain(EnumeratePropertyNames(manifest), static name => name == "comparisonAuthority");
        Assert.All(
            Array(manifest["artifacts"]).Select(Object).Where(descriptor => Text(descriptor["mediaType"]) == "image/webp"),
            descriptor => Assert.DoesNotContain(EnumeratePropertyNames(descriptor), forbiddenPreviewClaims.Contains));

        var chunks = await ReadArtifactAsync(driver, debugSessionId, losslessDescriptor);
        Assert.True(chunks.Count >= 3, "The controlled read must include beginning, middle, and ending chunks.");
        Assert.Equal(0, chunks[0].Offset);
        Assert.Contains(chunks, chunk =>
            chunk.Offset <= Integer(losslessDescriptor["byteLength"]) / 2 &&
            chunk.Offset + chunk.Bytes.Length > Integer(losslessDescriptor["byteLength"]) / 2);
        Assert.True(chunks[^1].EndOfArtifact);

        var reconstructed = chunks.SelectMany(static chunk => chunk.Bytes).ToArray();
        Assert.Equal(Integer(losslessDescriptor["byteLength"]), reconstructed.Length);
        Assert.Equal(Text(losslessDescriptor["sha256"]), Convert.ToHexString(SHA256.HashData(reconstructed)).ToLowerInvariant());
    }

    [Fact]
    public async Task LaterLocalProcessEventAfterCandidate_DoesNotCommitOrReturnArtifactAuthority()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        await using var driver = await StartWindowedDescendantDriverAsync(publishSecondWindowedDescendantAfterRelease: true);
        var debugSessionId = await StartDebugAsync(driver, "visual-identity-race-start");
        var declaration = (await CallToolAsync(
            driver,
            "get_ui_probe_capabilities",
            CapabilityArguments(debugSessionId),
            "visual-identity-race-capabilities",
            isError: false)).StructuredContent;
        var originalCandidate = Object(declaration["candidate"]);
        Assert.Equal(await driver.ReadDescendantProcessIdAsync(), Integer(originalCandidate["processId"]));

        var secondProcessId = await driver.PublishSecondWindowedDescendantAsync();
        var postEventDeclaration = (await CallToolAsync(
            driver,
            "get_ui_probe_capabilities",
            CapabilityArguments(debugSessionId),
            "visual-identity-race-post-event-capabilities",
            isError: false)).StructuredContent;
        var postEventCandidate = Object(postEventDeclaration["candidate"]);

        var capture = await CallToolAsync(
            driver,
            "capture_visual_evidence",
            VisualCaptureArguments(debugSessionId, originalCandidate),
            "visual-identity-race-capture",
            isError: true);
        var error = capture.StructuredContent;

        AssertSchemaValid("capture_visual_evidence", error);
        Assert.Equal("tool_error", Text(error["kind"]));
        Assert.Equal("capture_visual_evidence", Text(error["tool"]));
        Assert.Contains(Text(error["code"]), CandidateIdentityFailureCodes);
        Assert.DoesNotContain(
            EnumeratePropertyNames(error),
            name => name is "artifactId" or "artifacts" or "captureId" or "rasterCaptureId" or "byteLength" or "sha256" or "dataBase64" or "path" or "root");

        AssertCandidateBinding(originalCandidate, postEventCandidate);
        Assert.NotEqual(secondProcessId, Integer(postEventCandidate["processId"]));
    }

    [Fact]
    public async Task PostStageIdentityChange_AbortsStagingAndAllowsLaterVisualCapture()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var artifactRoot = Path.Combine(RepositoryLayout.ScratchRoot, $"native-scene-visual-staging-{Guid.NewGuid():N}");
        Directory.CreateDirectory(artifactRoot);
        try
        {
            await using (var driver = await StartWindowedDescendantDriverAsync(
                             publishSecondWindowedDescendantAfterRelease: true,
                             artifactRoot: artifactRoot))
            {
                var debugSessionId = await StartDebugAsync(driver, "visual-post-stage-identity-start");
                var originalCandidate = Object((await CallToolAsync(
                    driver,
                    "get_ui_probe_capabilities",
                    CapabilityArguments(debugSessionId),
                    "visual-post-stage-identity-capabilities",
                    isError: false)).StructuredContent["candidate"]);

                var captureTask = CallToolAsync(
                    driver,
                    "capture_visual_evidence",
                    VisualCaptureArguments(debugSessionId, originalCandidate),
                    "visual-post-stage-identity-capture",
                    isError: true);
                await WaitForStagedArtifactAsync(artifactRoot);

                _ = await driver.PublishSecondWindowedDescendantAsync();
                var failedCapture = await captureTask;
                var error = failedCapture.StructuredContent;

                AssertSchemaValid("capture_visual_evidence", error);
                Assert.Equal("tool_error", Text(error["kind"]));
                Assert.Equal("CANDIDATE_MISMATCH", Text(error["code"]));
                Assert.Empty(Directory.EnumerateFiles(artifactRoot, "*", SearchOption.AllDirectories));

            }

            await using (var retryDriver = await StartWindowedDescendantDriverAsync(artifactRoot: artifactRoot))
            {
                var retrySessionId = await StartDebugAsync(retryDriver, "visual-post-stage-retry-start");
                var retryCandidate = Object((await CallToolAsync(
                    retryDriver,
                    "get_ui_probe_capabilities",
                    CapabilityArguments(retrySessionId),
                    "visual-post-stage-retry-capabilities",
                    isError: false)).StructuredContent["candidate"]);
                var retryArguments = VisualCaptureArguments(retrySessionId, retryCandidate);
                var retry = await CallToolAsync(
                    retryDriver,
                    "capture_visual_evidence",
                    retryArguments,
                    "visual-post-stage-identity-retry",
                    isError: false);
                var manifest = retry.StructuredContent;

                AssertSchemaValid("capture_visual_evidence", manifest);
                AssertCompactManifest(retry.Result, manifest, retryArguments, retryCandidate);
                AssertLosslessDescriptor(
                    manifest,
                    Assert.Single(Array(manifest["artifacts"]).Select(Object), artifact => Text(artifact["mediaType"]) == "image/png"));
            }

        }
        finally
        {
            if (Directory.Exists(artifactRoot))
            {
                Directory.Delete(artifactRoot, recursive: true);
            }
        }
    }

    [Fact]
    public void BuiltBridge_ScreenshotDimensionGuard_RejectsUnsafeRastersBeforeAllocation()
    {
        var bridgeAssembly = System.Reflection.Assembly.LoadFrom(ResolveBridgeAssemblyPath());
        var screenshotCommands = bridgeAssembly.GetType("FlaUIBridge.Commands.ScreenshotCommands", throwOnError: true)!;
        var guard = Assert.IsAssignableFrom<System.Reflection.MethodInfo>(screenshotCommands.GetMethod(
            "ValidateCaptureDimensions",
            System.Reflection.BindingFlags.Static | System.Reflection.BindingFlags.NonPublic,
            binder: null,
            types: new[] { typeof(int), typeof(int) },
            modifiers: null));

        Assert.True(guard.IsAssembly, "The raster guard must remain internal.");
        Assert.Equal(typeof(void), guard.ReturnType);
        guard.Invoke(null, new object[] { 1280, 720 });

        foreach (var (width, height) in new[]
                 {
                     (100_000, 1),
                     (1, 100_000),
                     (10_000, 10_000),
                 })
        {
            var failure = Assert.Throws<System.Reflection.TargetInvocationException>(
                () => guard.Invoke(null, new object[] { width, height }));
            Assert.IsType<ArgumentOutOfRangeException>(failure.InnerException);
        }
    }

    [Theory]
    [InlineData("OBSERVER_UNAVAILABLE")]
    [InlineData("ARTIFACT_WRITE_FAILED")]
    public void VisualCaptureObserverOrCommitFailure_IsTypedAndDoesNotExposeArtifactAuthority(string code)
    {
        var error = new JsonObject
        {
            ["kind"] = "tool_error",
            ["tool"] = "capture_visual_evidence",
            ["code"] = code,
            ["message"] = "Controlled visual evidence is unavailable.",
        };

        AssertSchemaValid("capture_visual_evidence", error);
        Assert.Equal("tool_error", Text(error["kind"]));
        Assert.Equal("capture_visual_evidence", Text(error["tool"]));
        Assert.Equal(code, Text(error["code"]));
        Assert.DoesNotContain(EnumeratePropertyNames(error), name => name is
            "artifactId" or "artifacts" or "captureId" or "byteLength" or "dataBase64" or "path" or "root");
    }

    private static async Task WaitForStagedArtifactAsync(string artifactRoot)
    {
        for (var attempt = 0; attempt < 80; attempt++)
        {
            if (Directory.EnumerateFiles(artifactRoot, "*", SearchOption.AllDirectories).Any())
            {
                return;
            }

            await Task.Delay(TimeSpan.FromMilliseconds(25));
        }

        Assert.NotEmpty(Directory.EnumerateFiles(artifactRoot, "*", SearchOption.AllDirectories));
    }


    private static async Task<IReadOnlyList<ArtifactChunk>> ReadArtifactAsync(
        ModernMcpProcessDriver driver,
        string debugSessionId,
        JsonObject descriptor)
    {
        var artifactId = Text(descriptor["artifactId"]);
        var byteLength = Integer(descriptor["byteLength"]);
        Assert.True(byteLength >= 3, "A lossless PNG must be large enough to reconstruct beginning, middle, and ending chunks.");

        var chunkSize = Math.Min(ArtifactReadMaxBytes, Math.Max(1, byteLength / 3));
        var chunks = new List<ArtifactChunk>();
        var offset = 0;
        while (offset < byteLength)
        {
            var response = await CallToolAsync(
                driver,
                "read_capture_artifact",
                ArtifactReadArguments(debugSessionId, artifactId, offset, chunkSize),
                $"visual-read-{offset}",
                isError: false);
            var chunk = response.StructuredContent;

            AssertSchemaValid("read_capture_artifact", chunk);
            Assert.Equal("capture_artifact_chunk", Text(chunk["kind"]));
            Assert.Equal(artifactId, Text(chunk["artifactId"]));
            Assert.Equal(offset, Integer(chunk["offset"]));
            Assert.Equal(Text(descriptor["mediaType"]), Text(chunk["mediaType"]));
            Assert.Equal(byteLength, Integer(chunk["byteLength"]));
            Assert.Equal(Text(descriptor["sha256"]), Text(chunk["sha256"]));

            var bytes = Convert.FromBase64String(Text(chunk["dataBase64"]));
            Assert.Equal(Convert.ToBase64String(bytes), Text(chunk["dataBase64"]));
            Assert.Equal(bytes.Length, Integer(chunk["bytesRead"]));
            Assert.InRange(bytes.Length, 1, chunkSize);
            Assert.Equal(offset + bytes.Length == byteLength, Boolean(chunk["endOfArtifact"]));

            chunks.Add(new ArtifactChunk(offset, bytes, Boolean(chunk["endOfArtifact"])));
            offset += bytes.Length;
        }

        Assert.Equal(byteLength, offset);
        return chunks;
    }

    private static void AssertCompactManifest(
        JsonObject result,
        JsonObject manifest,
        JsonObject request,
        JsonObject expectedCandidate)
    {
        Assert.Equal("visual_evidence_capture", Text(manifest["kind"]));
        Assert.Equal("COMPLETE", Text(manifest["status"]));
        Assert.True(JsonNode.DeepEquals(request["sceneRequest"], manifest["sceneRequest"]));
        Assert.NotNull(manifest["evidenceScope"]);
        Assert.True(JsonNode.DeepEquals(request["evidenceScope"], manifest["evidenceScope"]));
        AssertCandidateBinding(expectedCandidate, Object(manifest["candidate"]));
        Assert.InRange(Encoding.UTF8.GetByteCount(manifest.ToJsonString()), 1, 262_144);

        var names = EnumeratePropertyNames(manifest).ToArray();
        Assert.DoesNotContain(names, name => name is "dataBase64" or "pngBytes" or "path" or "root");

        var text = Text(Object(Assert.Single(Array(result["content"])))["text"]);
        Assert.True(JsonNode.DeepEquals(manifest, JsonNode.Parse(text)), "MCP text content must be the compact manifest, not a raw capture payload.");
    }

    private static void AssertCandidateBinding(JsonObject expected, JsonObject actual)
    {
        foreach (var name in new[]
                 {
                     "processId", "processIdentity", "hwnd", "executableSha256", "assemblyVersion", "probeVersion",
                     "observerVersions", "contractSetHash", "storyHash", "source",
                 })
        {
            Assert.True(JsonNode.DeepEquals(expected[name], actual[name]), $"Capture candidate '{name}' must bind to the declared local candidate.");
        }
    }

    private static void AssertLosslessDescriptor(JsonObject manifest, JsonObject descriptor)
    {
        Assert.Matches("^[A-Za-z0-9_-]{22,86}$", Text(manifest["captureId"]));
        Assert.Matches("^[A-Za-z0-9_-]{22,86}$", Text(descriptor["artifactId"]));
        Assert.Matches("^[A-Za-z0-9_-]{22,86}$", Text(descriptor["rasterCaptureId"]));
        Assert.NotEqual(Text(manifest["captureId"]), Text(descriptor["rasterCaptureId"]));
        Assert.True(DateTimeOffset.TryParse(Text(descriptor["capturedAt"]), out _));
        Assert.True(Integer(descriptor["byteLength"]) > 0);
        Assert.Matches("^[a-f0-9]{64}$", Text(descriptor["sha256"]));

        var retention = Object(descriptor["retention"]);
        Assert.Equal("session_stop_or_expiry", Text(retention["endsOn"]));
        Assert.InRange(Integer(retention["maximumAgeSeconds"]), 1, 14_400);
        Assert.True(DateTimeOffset.TryParse(Text(retention["expiresAt"]), out _));
    }

    private static Task<ModernMcpProcessDriver> StartWindowedDescendantDriverAsync(
        bool publishSecondWindowedDescendantAfterRelease = false,
        string? artifactRoot = null) =>
        ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(
                    SpawnWindowedDescendant: true,
                    PublishSecondWindowedDescendantAfterRelease: publishSecondWindowedDescendantAfterRelease),
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["FLAUI_BRIDGE_PATH"] = ResolveBridgeAssemblyPath(),
                    ["NETCOREDBG_MCP_ARTIFACT_ROOT"] = artifactRoot,
                }));


    private static string ResolveBridgeAssemblyPath()
    {
        var testOutputDirectory = new DirectoryInfo(AppContext.BaseDirectory);
        var configuration = testOutputDirectory.Parent?.Name
            ?? throw new InvalidOperationException("Test output configuration is absent.");
        var bridgeDirectory = Path.Combine(RepositoryLayout.Root, "bridge", "bin", configuration, "net8.0-windows");
        var candidates = new[]
        {
            Path.Combine(bridgeDirectory, "win-x64", "FlaUIBridge.dll"),
            Path.Combine(bridgeDirectory, "FlaUIBridge.dll"),
        };
        return candidates.FirstOrDefault(File.Exists)
            ?? throw new InvalidOperationException("Built FlaUI bridge assembly is absent.");
    }

    private static async Task<string> StartDebugAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var content = (await CallToolAsync(
            driver,
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            requestId,
            isError: false)).StructuredContent;
        Assert.Equal("start_debug_success", Text(content["kind"]));
        return Text(content["debugSessionId"]);
    }

    private static JsonObject CapabilityArguments(string debugSessionId) => new()
    {
        ["debugSessionId"] = debugSessionId,
        ["protocolVersion"] = ActiveProtocolVersion,
        ["schemaVersion"] = ActiveSchemaVersion,
    };

    private static JsonObject VisualCaptureArguments(string debugSessionId, JsonObject candidate) => new()
    {
        ["debugSessionId"] = debugSessionId,
        ["protocolVersion"] = ActiveProtocolVersion,
        ["schemaVersion"] = ActiveSchemaVersion,
        ["sceneRequest"] = new JsonObject
        {
            ["storyId"] = "Button.Primary",
            ["sceneId"] = "Default",
            ["fixtureId"] = "ControlledFixture",
            ["scope"] = new JsonObject { ["kind"] = "scene" },
            ["appearance"] = "light",
            ["theme"] = "baseline",
            ["density"] = "regular",
            ["contrast"] = "standard",
            ["viewport"] = new JsonObject
            {
                ["policy"] = "exact",
                ["width"] = 1280,
                ["height"] = 720,
            },
            ["expectedDpiPolicy"] = new JsonObject
            {
                ["mode"] = "exact",
                ["x"] = 96,
                ["y"] = 96,
            },
            ["focusTarget"] = null,
            ["selectedState"] = null,
            ["currentState"] = null,
            ["scrollOffsets"] = null,
            ["animationPolicy"] = "finished",
            ["settlePolicy"] = new JsonObject
            {
                ["timeoutMs"] = 30_000,
                ["sampleCount"] = 2,
                ["stableForMs"] = 100,
                ["requireDispatcherIdle"] = true,
                ["requireStableLayout"] = true,
                ["requireAnimationState"] = true,
                ["requireWindowGeometry"] = true,
                ["requireContextMaterialization"] = true,
                ["requireAsyncLoadSettled"] = true,
            },
            ["contractSetHash"] = new string('a', 64),
            ["expectedCandidateIdentity"] = new JsonObject
            {
                ["executableSha256"] = candidate["executableSha256"]?.DeepClone(),
                ["assemblyVersion"] = candidate["assemblyVersion"]?.DeepClone(),
                ["probeVersion"] = candidate["probeVersion"]?.DeepClone(),
            },
        },
        ["evidenceScope"] = new JsonObject { ["kind"] = "window" },
    };

    private static JsonObject ArtifactReadArguments(string debugSessionId, string artifactId, int offset, int maxBytes) => new()
    {
        ["debugSessionId"] = debugSessionId,
        ["protocolVersion"] = ActiveProtocolVersion,
        ["schemaVersion"] = ActiveSchemaVersion,
        ["artifactId"] = artifactId,
        ["offset"] = offset,
        ["maxBytes"] = maxBytes,
    };

    private static async Task<ToolCall> CallToolAsync(
        ModernMcpProcessDriver driver,
        string tool,
        JsonObject arguments,
        string requestId,
        bool isError)
    {
        var response = await driver.CallToolRawAsync(
            tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId));
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", Text(result["resultType"]));
        Assert.True(
            isError == (result["isError"]?.GetValue<bool>() ?? false),
            $"{tool} returned an unexpected result: {result["structuredContent"]?.ToJsonString() ?? "<null>"}.");
        return new ToolCall(result, Object(result["structuredContent"]));
    }

    private static void AssertSchemaValid(string tool, JsonObject content)
    {
        var validation = NativeSceneContractCatalogDriver.Load().ValidateResult(tool, content.ToJsonString());
        Assert.True(validation.IsValid, $"Expected schema-valid {tool} content, got {validation.Code ?? "<null>"}: {validation.Message ?? "<null>"}.");
    }
    private static JsonObject CorpusExpected(string caseId) =>
        JsonNode.Parse(Encoding.UTF8.GetString(NativeSceneContractCatalogDriver.Load().GetArtifactBytes("parity-corpus.json")))!
            .AsObject()["cases"]!
            .AsArray()
            .Single(@case => Text(@case!.AsObject()["id"]) == caseId)!["expected"]!
            .DeepClone()
            .AsObject();


    private static IEnumerable<string> EnumeratePropertyNames(JsonNode? node)
    {
        if (node is JsonObject objectNode)
        {
            foreach (var property in objectNode)
            {
                yield return property.Key;
                foreach (var name in EnumeratePropertyNames(property.Value))
                {
                    yield return name;
                }
            }
        }
        else if (node is JsonArray arrayNode)
        {
            foreach (var item in arrayNode)
            {
                foreach (var name in EnumeratePropertyNames(item))
                {
                    yield return name;
                }
            }
        }
    }

    private static JsonObject Object(JsonNode? node) => Assert.IsType<JsonObject>(node);

    private static JsonArray Array(JsonNode? node) => Assert.IsType<JsonArray>(node);

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();

    private static int Integer(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<int>();

    private static bool Boolean(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<bool>();

    private sealed record ToolCall(JsonObject Result, JsonObject StructuredContent);

    private sealed record ArtifactChunk(int Offset, byte[] Bytes, bool EndOfArtifact);
}
