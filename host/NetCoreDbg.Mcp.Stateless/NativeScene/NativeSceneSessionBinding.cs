using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

internal sealed record NativeSceneTargetIdentity(
    int ProcessId,
    string ProcessIdentity,
    string ExecutablePath,
    string ExecutableSha256,
    string? AssemblyVersion,
    string? ProbeVersion);

internal sealed class NativeSceneSessionBinding : IAsyncDisposable
{
    private const string ArtifactSchemaVersion = "native-scene-artifact/1";
    private const int MaximumPngBytes = 67_108_864;
    private const int MaximumBridgeRequestBytes = 1_048_576;
    private const int MaximumBridgeResponseBytes = 100_663_296;
    private const int MaximumPngBase64Length = 89_478_488;
    private static readonly TimeSpan BridgeTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan ArtifactRetention = TimeSpan.FromHours(4);

    private readonly NetCoreDbgSession _session;
    private readonly string _debugSessionId;
    private readonly string? _bridgePath;
    private readonly string? _artifactRoot;
    private readonly SemaphoreSlim _gate = new(initialCount: 1, maxCount: 1);

    private NativeSceneBridgeClient? _bridgeClient;
    private Process? _bridgeProcess;
    private NativeSceneArtifactStore? _artifactStore;
    private int _disposed;

    internal NativeSceneSessionBinding(
        NetCoreDbgSession session,
        string debugSessionId,
        string? bridgePath,
        string? artifactRoot)
    {
        _session = session ?? throw new ArgumentNullException(nameof(session));
        ArgumentException.ThrowIfNullOrWhiteSpace(debugSessionId);
        _debugSessionId = debugSessionId;
        _bridgePath = bridgePath;
        _artifactRoot = artifactRoot;
        AuthorizationNonce = CreateOpaqueId();
    }

    internal string AuthorizationNonce { get; }

    internal bool SupportsVisualEvidence =>
        Volatile.Read(ref _disposed) == 0 &&
        OperatingSystem.IsWindows() &&
        TryGetBridgePath(out _);

    internal bool TryGetCandidate(out JsonElement candidate)
    {
        if (!_session.TryGetNativeSceneTargetIdentity(out var targetIdentity))
        {
            candidate = default;
            return false;
        }

        candidate = JsonSerializer.SerializeToElement(new
        {
            processId = targetIdentity.ProcessId,
            processIdentity = targetIdentity.ProcessIdentity,
            hwnd = (string?)null,
            executableSha256 = targetIdentity.ExecutableSha256,
            assemblyVersion = targetIdentity.AssemblyVersion,
            probeVersion = targetIdentity.ProbeVersion,
            observerVersions = Array.Empty<object>(),
            contractSetHash = NativeSceneContractCatalog.GetArtifactSha256("native-scene-probe.schema.json"),
            storyHash = (string?)null,
            capturedAt = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            source = new
            {
                kind = "launch_manifest",
                verification = "verified",
            },
        });
        return true;
    }

    internal bool MatchesExpectedCandidateIdentity(JsonElement expectedCandidateIdentity)
    {
        return _session.TryGetNativeSceneCaptureTargetIdentity(out var targetIdentity)
            && (expectedCandidateIdentity.ValueKind == JsonValueKind.Null ||
                (expectedCandidateIdentity.ValueKind == JsonValueKind.Object &&
                 MatchesExpectedValue(expectedCandidateIdentity, "executableSha256", targetIdentity.ExecutableSha256) &&
                 MatchesExpectedValue(expectedCandidateIdentity, "assemblyVersion", targetIdentity.AssemblyVersion) &&
                 MatchesExpectedValue(expectedCandidateIdentity, "probeVersion", targetIdentity.ProbeVersion)));
    }

    internal async Task<NativeSceneVisualEvidenceResult> CaptureVisualEvidenceAsync(
        JsonElement sceneRequest,
        JsonElement evidenceScope,
        JsonElement candidate,
        CancellationToken cancellationToken)
    {
        var entered = false;
        try
        {
            await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
            entered = true;
            if (!SupportsVisualEvidence || !_session.TryGetNativeSceneCaptureTargetIdentity(out var targetIdentity))
            {
                return NativeSceneVisualEvidenceResult.CandidateMismatch();
            }

            NativeSceneBridgeCallResult bridgeResult;
            try
            {
                StartBridge(targetIdentity.ProcessId);
                bridgeResult = await _bridgeClient!.SendAsync(
                    AuthorizationNonce,
                    new JsonObject
                    {
                        ["operation"] = "capture_visual_evidence",
                        ["processId"] = targetIdentity.ProcessId,
                        ["processIdentity"] = targetIdentity.ProcessIdentity,
                    },
                    cancellationToken).ConfigureAwait(false);
            }
            catch (Exception)
            {
                return NativeSceneVisualEvidenceResult.ObserverUnavailable();
            }
            finally
            {
                await DisposeBridgeAsync().ConfigureAwait(false);
            }

            if (!bridgeResult.IsAvailable ||
                bridgeResult.Payload is null ||
                !TryReadPng(bridgeResult.Payload, targetIdentity, out var png))
            {
                return NativeSceneVisualEvidenceResult.ObserverUnavailable();
            }

            if (!_session.TryGetNativeSceneCaptureTargetIdentity(out var recheckedTargetIdentity) ||
                !targetIdentity.Equals(recheckedTargetIdentity))
            {
                return NativeSceneVisualEvidenceResult.CandidateMismatch();
            }

            try
            {
                var capturedAt = DateTimeOffset.UtcNow;
                var captureId = CreateOpaqueId();
                var staged = await GetOrCreateArtifactStore().StageAsync(
                    _debugSessionId,
                    captureId,
                    "image/png",
                    ArtifactSchemaVersion,
                    png,
                    cancellationToken).ConfigureAwait(false);
                if (!_session.TryGetNativeSceneCaptureTargetIdentity(out recheckedTargetIdentity) ||
                    !targetIdentity.Equals(recheckedTargetIdentity))
                {
                    return NativeSceneVisualEvidenceResult.CandidateMismatch();
                }

                var commit = await staged.CommitAsync(cancellationToken).ConfigureAwait(false);
                if (commit.Descriptor is not { } descriptor)
                {
                    return NativeSceneVisualEvidenceResult.ArtifactWriteFailed();
                }

                return NativeSceneVisualEvidenceResult.Succeeded(CreateVisualEvidenceManifest(
                    sceneRequest,
                    evidenceScope,
                    candidate,
                    descriptor,
                    captureId,
                    capturedAt));
            }
            catch (Exception)
            {
                return NativeSceneVisualEvidenceResult.ArtifactWriteFailed();
            }
        }
        catch (OperationCanceledException)
        {
            return NativeSceneVisualEvidenceResult.ObserverUnavailable();
        }
        finally
        {
            if (entered)
            {
                _gate.Release();
            }
        }
    }

    internal async Task<NativeSceneArtifactReadResult> ReadCaptureArtifactAsync(
        string artifactId,
        long offset,
        int maxBytes,
        CancellationToken cancellationToken)
    {
        var entered = false;
        try
        {
            await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
            entered = true;
            if (Volatile.Read(ref _disposed) != 0 || _artifactStore is null)
            {
                return new NativeSceneArtifactReadError("ARTIFACT_NOT_FOUND", "Artifact is not available.");
            }

            return await _artifactStore.ReadAsync(
                _debugSessionId,
                artifactId,
                offset,
                maxBytes,
                cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            return new NativeSceneArtifactReadError("ARTIFACT_NOT_FOUND", "Artifact is not available.");
        }
        finally
        {
            if (entered)
            {
                _gate.Release();
            }
        }
    }

    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            await DisposeBridgeAsync().ConfigureAwait(false);
            if (_artifactStore is { } store)
            {
                _artifactStore = null;
                try
                {
                    await store.StopSessionAsync(_debugSessionId, CancellationToken.None).ConfigureAwait(false);
                }
                finally
                {
                    await store.DisposeAsync().ConfigureAwait(false);
                }
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    private NativeSceneArtifactStore GetOrCreateArtifactStore() => _artifactStore ??= new NativeSceneArtifactStore(
        _artifactRoot ?? Path.Combine(Path.GetTempPath(), "netcoredbg-mcp-native-scene"),
        TimeProvider.System);

    private void StartBridge(int processId)
    {
        if (_bridgeClient is not null || _bridgeProcess is not null || !TryGetBridgePath(out var bridgePath))
        {
            throw new InvalidOperationException("The local native-scene observer cannot be started.");
        }

        var isAssembly = bridgePath.EndsWith(".dll", StringComparison.OrdinalIgnoreCase);
        var startInfo = new ProcessStartInfo
        {
            FileName = isAssembly
                ? Environment.GetEnvironmentVariable("DOTNET_HOST_PATH") is { Length: > 0 } dotnetHost ? dotnetHost : "dotnet"
                : bridgePath,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        if (isAssembly)
        {
            startInfo.ArgumentList.Add(bridgePath);
        }

        var pipeName = "native-scene-" + CreateOpaqueId();
        startInfo.ArgumentList.Add("--native-scene-pipe");
        startInfo.ArgumentList.Add(pipeName);
        startInfo.ArgumentList.Add(AuthorizationNonce);
        startInfo.ArgumentList.Add(processId.ToString(CultureInfo.InvariantCulture));

        var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("The local native-scene observer process did not start.");
        try
        {
            _bridgeProcess = process;
            _bridgeClient = new NativeSceneBridgeClient(
                pipeName,
                BridgeTimeout,
                BridgeTimeout,
                BridgeTimeout,
                MaximumBridgeRequestBytes,
                MaximumBridgeResponseBytes);
        }
        catch
        {
            try
            {
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                }
            }
            catch (Exception)
            {
            }
            finally
            {
                process.Dispose();
                _bridgeProcess = null;
            }

            throw;
        }
    }

    private async ValueTask DisposeBridgeAsync()
    {
        var client = _bridgeClient;
        var process = _bridgeProcess;
        _bridgeClient = null;
        _bridgeProcess = null;

        if (client is not null)
        {
            try
            {
                await client.DisposeAsync().ConfigureAwait(false);
            }
            catch (Exception)
            {
            }
        }

        if (process is null)
        {
            return;
        }

        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                using var timeout = new CancellationTokenSource(BridgeTimeout);
                await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
            }
        }
        catch (Exception)
        {
        }
        finally
        {
            process.Dispose();
        }
    }

    private bool TryGetBridgePath(out string bridgePath)
    {
        bridgePath = string.Empty;
        if (string.IsNullOrWhiteSpace(_bridgePath))
        {
            return false;
        }

        try
        {
            var resolved = Path.GetFullPath(_bridgePath);
            if (!File.Exists(resolved) ||
                (!resolved.EndsWith(".dll", StringComparison.OrdinalIgnoreCase) &&
                 !resolved.EndsWith(".exe", StringComparison.OrdinalIgnoreCase)))
            {
                return false;
            }

            bridgePath = resolved;
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    private static bool TryReadPng(JsonObject payload, NativeSceneTargetIdentity targetIdentity, out byte[] png)
    {
        png = Array.Empty<byte>();
        if (payload.Count != 4 ||
            !TryGetString(payload, "pngBase64", out var pngBase64) ||
            pngBase64.Length > MaximumPngBase64Length ||
            !TryGetInt32(payload, "byteLength", out var byteLength) ||
            byteLength is < 1 or > MaximumPngBytes ||
            !TryGetString(payload, "sha256", out var sha256) ||
            payload["provenance"] is not JsonObject provenance ||
            !IsExpectedProvenance(provenance, targetIdentity))
        {
            return false;
        }

        try
        {
            png = Convert.FromBase64String(pngBase64);
        }
        catch (FormatException)
        {
            return false;
        }

        var actualSha256 = Convert.ToHexString(SHA256.HashData(png)).ToLowerInvariant();
        return png.Length == byteLength &&
               png.Length <= MaximumPngBytes &&
               StringComparer.Ordinal.Equals(pngBase64, Convert.ToBase64String(png)) &&
               StringComparer.Ordinal.Equals(sha256, actualSha256);
    }

    private static bool IsExpectedProvenance(JsonObject provenance, NativeSceneTargetIdentity targetIdentity)
    {
        return provenance.Count == 9 &&
               TryGetInt32(provenance, "processId", out var observedProcessId) && observedProcessId == targetIdentity.ProcessId &&
               TryGetString(provenance, "processIdentity", out var observedProcessIdentity) &&
               StringComparer.Ordinal.Equals(observedProcessIdentity, targetIdentity.ProcessIdentity) &&
               TryGetInt64(provenance, "hwnd", out var hwnd) && hwnd != 0 &&
               TryGetInt32(provenance, "width", out var width) && width > 0 &&
               TryGetInt32(provenance, "height", out var height) && height > 0 &&
               provenance["clientRect"] is JsonObject clientRect &&
               clientRect.Count == 4 &&
               TryGetInt32(clientRect, "left", out var left) &&
               TryGetInt32(clientRect, "top", out var top) &&
               TryGetInt32(clientRect, "right", out var right) && right > left &&
               TryGetInt32(clientRect, "bottom", out var bottom) && bottom > top &&
               TryGetInt32(provenance, "dpi", out var dpi) && dpi > 0 &&
               TryGetString(provenance, "captureMethod", out var captureMethod) &&
               StringComparer.Ordinal.Equals(captureMethod, "PrintWindow") &&
               TryGetInt32(provenance, "printWindowFlags", out var printWindowFlags) && printWindowFlags == 2;
    }

    private static JsonObject CreateVisualEvidenceManifest(
        JsonElement sceneRequest,
        JsonElement evidenceScope,
        JsonElement candidate,
        NativeSceneArtifactDescriptor descriptor,
        string captureId,
        DateTimeOffset capturedAt)
    {
        var timestamp = capturedAt.ToString("O", CultureInfo.InvariantCulture);
        return new JsonObject
        {
            ["kind"] = "visual_evidence_capture",
            ["status"] = "COMPLETE",
            ["captureId"] = captureId,
            ["protocolVersion"] = "native-scene-probe/1",
            ["schemaVersion"] = "native-scene-probe.schema/1",
            ["sceneRequest"] = CloneObject(sceneRequest),
            ["evidenceScope"] = CloneObject(evidenceScope),
            ["capturedAt"] = timestamp,
            ["candidate"] = CloneObject(candidate),
            ["stability"] = CreateUnobservableStability(timestamp),
            ["atomicity"] = new JsonObject { ["authority"] = "not_applicable" },
            ["element"] = null,
            ["artifacts"] = new JsonArray
            {
                new JsonObject
                {
                    ["artifactId"] = descriptor.ArtifactId,
                    ["mediaType"] = descriptor.MediaType,
                    ["byteLength"] = descriptor.ByteLength,
                    ["sha256"] = descriptor.Sha256,
                    ["artifactSchemaVersion"] = descriptor.ArtifactSchemaVersion,
                    ["captureId"] = captureId,
                    ["retention"] = new JsonObject
                    {
                        ["endsOn"] = "session_stop_or_expiry",
                        ["maximumAgeSeconds"] = (int)ArtifactRetention.TotalSeconds,
                        ["expiresAt"] = capturedAt.Add(ArtifactRetention).ToString("O", CultureInfo.InvariantCulture),
                    },
                    ["evidenceGrade"] = "lossless_visual",
                    ["capturedAt"] = timestamp,
                    ["rasterCaptureId"] = CreateOpaqueId(),
                    ["adjacentToCaptureId"] = null,
                    ["relativeProvenance"] = "bridge_print_window",
                },
            },
            ["issues"] = new JsonArray(),
        };
    }

    private static JsonObject CreateUnobservableStability(string timestamp) => new()
    {
        ["status"] = "UNOBSERVABLE",
        ["revalidatedByCapture"] = false,
        ["conditions"] = new JsonObject
        {
            ["dispatcherIdle"] = ConditionUnobservable(),
            ["stableLayout"] = ConditionUnobservable(),
            ["animationState"] = ConditionUnobservable(),
            ["windowGeometry"] = ConditionUnobservable(),
            ["contextMaterialization"] = ConditionUnobservable(),
            ["asyncLoadSettled"] = ConditionUnobservable(),
        },
        ["settleDurationMs"] = 0,
        ["observedAt"] = timestamp,
        ["sceneEpoch"] = 0,
        ["sequence"] = 0,
    };

    private static JsonObject ConditionUnobservable() => new() { ["state"] = "unobservable" };

    private static JsonObject CloneObject(JsonElement value) => JsonNode.Parse(value.GetRawText()) as JsonObject
        ?? throw new InvalidOperationException("A validated native-scene request projection was not an object.");

    private static bool TryGetString(JsonObject source, string name, out string value)
    {
        if (source[name] is JsonValue node && node.TryGetValue<string>(out var parsed) && !string.IsNullOrEmpty(parsed))
        {
            value = parsed;
            return true;
        }

        value = string.Empty;
        return false;
    }

    private static bool TryGetInt32(JsonObject source, string name, out int value)
    {
        value = 0;
        return source[name] is JsonValue node && node.TryGetValue(out value);
    }

    private static bool MatchesExpectedValue(JsonElement expectedCandidateIdentity, string propertyName, string? actual)
    {
        if (!expectedCandidateIdentity.TryGetProperty(propertyName, out var expected) || expected.ValueKind == JsonValueKind.Null)
        {
            return true;
        }

        return expected.ValueKind == JsonValueKind.String &&
               actual is not null &&
               StringComparer.Ordinal.Equals(expected.GetString(), actual);
    }
    private static bool TryGetInt64(JsonObject source, string name, out long value)
    {
        value = 0;
        return source[name] is JsonValue node && node.TryGetValue(out value);
    }

    private static string CreateOpaqueId()
    {
        Span<byte> bytes = stackalloc byte[32];
        try
        {
            RandomNumberGenerator.Fill(bytes);
            return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }
        finally
        {
            CryptographicOperations.ZeroMemory(bytes);
        }
    }
}

internal sealed record NativeSceneVisualEvidenceResult(JsonObject? Manifest, string? Code, string? Message)
{
    internal static NativeSceneVisualEvidenceResult Succeeded(JsonObject manifest) => new(manifest, null, null);

    internal static NativeSceneVisualEvidenceResult ObserverUnavailable() => new(
        null,
        "OBSERVER_UNAVAILABLE",
        "Native-scene observer is unavailable.");

    internal static NativeSceneVisualEvidenceResult ArtifactWriteFailed() => new(
        null,
        "ARTIFACT_WRITE_FAILED",
        "Artifact could not be committed.");

    internal static NativeSceneVisualEvidenceResult CandidateMismatch() => new(
        null,
        "CANDIDATE_MISMATCH",
        "Candidate identity does not match.");
}
