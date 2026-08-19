using System.Reflection;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;
using Microsoft.Extensions.Time.Testing;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

/// <summary>
/// RED-first contract for T022. The coordinator stays internal, but it must
/// accept an injected clock and a controlled condition observer so T023 can
/// prove stability without wall-clock delays or a live UI target.
/// </summary>
[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
public sealed class NativeSceneStabilityTests
{
    private static readonly DateTimeOffset Start = new(2026, 8, 19, 12, 0, 0, TimeSpan.Zero);
    private static readonly TimeSpan HarnessTimeout = TimeSpan.FromSeconds(1);


    [Theory]
    [InlineData(2)]
    [InlineData(16)]
    public async Task StandaloneWait_ObservesEveryExplicitBoundedSampleAndRecordsHistoricalEvidence(int sampleCount)
    {
        var clock = new FakeTimeProvider(Start);
        var fixture = new ControlledStabilityFixture(clock);
        var coordinator = NativeSceneStabilityCoordinatorDriver.Create(clock, fixture.ObserveAsync);
        using var request = JsonDocument.Parse(SceneRequest(sampleCount).ToJsonString());

        var stability = await coordinator.WaitForStableAsync(request.RootElement);

        Assert.Equal(sampleCount, fixture.ObservationCount);
        Assert.Equal("STABLE", Text(stability["status"]));
        Assert.False(Boolean(stability["revalidatedByCapture"]));
        Assert.Equal(0, Integer(stability["settleDurationMs"]));
        Assert.Equal(Start, Timestamp(stability["observedAt"]));
        Assert.Equal(41, Integer(stability["sceneEpoch"]));
        Assert.Equal(sampleCount, Integer(stability["sequence"]));
        AssertAllConditions(stability, "met");
    }
    [Fact]
    public async Task IncompleteObservationAtDeadline_ReturnsNonStableEvidenceAndReleasesGate()
    {
        const int timeoutMs = 10;
        var clock = new FakeTimeProvider(Start);
        var sampler = new DeadlineStabilitySampler();
        var coordinator = NativeSceneStabilityCoordinatorDriver.Create(clock, sampler.ObserveAsync);
        using var request = JsonDocument.Parse(SceneRequest(sampleCount: 2, timeoutMs).ToJsonString());

        var firstWait = coordinator.WaitForStableAsync(request.RootElement);
        await sampler.FirstObservationStarted.WaitAsync(HarnessTimeout);

        clock.Advance(TimeSpan.FromMilliseconds(timeoutMs));

        var timedOut = await firstWait.WaitAsync(HarnessTimeout);
        Assert.NotEqual("STABLE", Text(timedOut["status"]));
        Assert.False(Boolean(timedOut["revalidatedByCapture"]));
        AssertConditionEvidence(timedOut);
        AssertSchemaValid("wait_for_ui_stable", StabilityReceipt(timedOut));

        sampler.UseImmediateUnobservableEvidence();

        var secondWait = await coordinator.WaitForStableAsync(request.RootElement).WaitAsync(HarnessTimeout);
        Assert.Equal("UNOBSERVABLE", Text(secondWait["status"]));
        Assert.False(Boolean(secondWait["revalidatedByCapture"]));
        AssertConditionEvidence(secondWait);
        AssertSchemaValid("wait_for_ui_stable", StabilityReceipt(secondWait));
    }


    [Fact]
    public async Task ChangedRequiredConditionAfterWait_CannotAuthorizeLaterCapture()
    {
        var clock = new FakeTimeProvider(Start);
        var fixture = new ControlledStabilityFixture(clock);
        var coordinator = NativeSceneStabilityCoordinatorDriver.Create(clock, fixture.ObserveAsync);
        using var request = JsonDocument.Parse(SceneRequest(sampleCount: 2).ToJsonString());

        var historical = await coordinator.WaitForStableAsync(request.RootElement);
        Assert.Equal("STABLE", Text(historical["status"]));
        Assert.False(Boolean(historical["revalidatedByCapture"]));
        Assert.Equal(41, Integer(historical["sceneEpoch"]));

        fixture.SetCondition("stableLayout", "not_met", sceneEpoch: 42);
        clock.Advance(TimeSpan.FromSeconds(1));

        var captureTime = await coordinator.RevalidateForCaptureAsync(request.RootElement);

        Assert.Equal(4, fixture.ObservationCount);
        Assert.Equal("PARTIAL", Text(captureTime["status"]));
        Assert.True(Boolean(captureTime["revalidatedByCapture"]));
        Assert.Equal("not_met", Text(Conditions(captureTime)["stableLayout"]!.AsObject()["state"]));
        Assert.Equal(42, Integer(captureTime["sceneEpoch"]));
        Assert.Equal(clock.GetUtcNow(), Timestamp(captureTime["observedAt"]));
        Assert.NotEqual(Integer(historical["sequence"]), Integer(captureTime["sequence"]));
    }

    [Theory]
    [InlineData("stableLayout", "not_met", "PARTIAL")]
    [InlineData("contextMaterialization", "unobservable", "UNOBSERVABLE")]
    public async Task QualifiedCaptureEvidence_AlwaysRecordsFreshRevalidation(
        string condition,
        string state,
        string expectedStatus)
    {
        var clock = new FakeTimeProvider(Start);
        var fixture = new ControlledStabilityFixture(clock);
        fixture.SetCondition(condition, state, sceneEpoch: 42);
        var coordinator = NativeSceneStabilityCoordinatorDriver.Create(clock, fixture.ObserveAsync);
        using var request = JsonDocument.Parse(SceneRequest(sampleCount: 2).ToJsonString());

        var stability = await coordinator.RevalidateForCaptureAsync(request.RootElement);

        Assert.Equal(2, fixture.ObservationCount);
        Assert.Equal(expectedStatus, Text(stability["status"]));
        Assert.True(Boolean(stability["revalidatedByCapture"]));
        Assert.Equal(state, Text(Conditions(stability)[condition]!.AsObject()["state"]));
        Assert.Equal(42, Integer(stability["sceneEpoch"]));
        Assert.Equal(Start, Timestamp(stability["observedAt"]));
    }

    [Theory]
    [InlineData("PARTIAL")]
    [InlineData("UNOBSERVABLE")]
    public void C023QualifiedCaptureThatReusesHistoricalReceipt_IsRejected(string status)
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var corpus = JsonNode.Parse(System.Text.Encoding.UTF8.GetString(catalog.GetArtifactBytes("parity-corpus.json")))!.AsObject();
        var c023 = corpus["cases"]!.AsArray()
            .Single(@case => Text(@case!?.AsObject()["id"]) == "C023-partial-capture-without-revalidation-is-schema-rejected")!
            .AsObject();
        var capture = c023["response"]!.DeepClone().AsObject();
        var stability = Object(capture["stability"]);
        capture["status"] = status;
        stability["status"] = status;
        capture["artifacts"] = new JsonArray();

        stability["revalidatedByCapture"] = true;
        Assert.True(catalog.ValidateResult("capture_native_scene", capture.ToJsonString()).IsValid);

        stability["revalidatedByCapture"] = false;
        var validation = catalog.ValidateResult("capture_native_scene", capture.ToJsonString());

        Assert.False(validation.IsValid);
        Assert.Equal("INVALID_TOOL_ARGUMENTS", validation.Code);
    }

    [Fact]
    public async Task StandaloneWaitThenVisualCapture_RecordsHistoricalThenCaptureTimeStability()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        await using var driver = await StartWindowedDescendantDriverAsync();
        var debugSessionId = await StartDebugAsync(driver, "stability-wait-start");
        var candidate = await GetCandidateAsync(driver, debugSessionId, "stability-wait-capabilities");
        var sceneArguments = SceneArguments(debugSessionId, candidate, sampleCount: 2);

        var receipt = await CallToolAsync(
            driver,
            "wait_for_ui_stable",
            sceneArguments,
            "stability-wait",
            isError: false);
        AssertSchemaValid("wait_for_ui_stable", receipt.StructuredContent);
        Assert.Equal("ui_stability_receipt", Text(receipt.StructuredContent["kind"]));
        Assert.False(Boolean(Object(receipt.StructuredContent["stability"])["revalidatedByCapture"]));
        AssertConditionEvidence(Object(receipt.StructuredContent["stability"]));

        var visualArguments = SceneArguments(debugSessionId, candidate, sampleCount: 2);
        visualArguments["evidenceScope"] = new JsonObject { ["kind"] = "window" };
        var capture = await CallToolAsync(
            driver,
            "capture_visual_evidence",
            visualArguments,
            "stability-capture",
            isError: false);
        AssertSchemaValid("capture_visual_evidence", capture.StructuredContent);
        Assert.True(Boolean(Object(capture.StructuredContent["stability"])["revalidatedByCapture"]));
        AssertConditionEvidence(Object(capture.StructuredContent["stability"]));
    }

    [Fact]
    public async Task ChangedCandidateContextAfterWait_CannotAuthorizeLaterVisualCapture()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        await using var driver = await StartWindowedDescendantDriverAsync(publishSecondWindowedDescendantAfterRelease: true);
        var debugSessionId = await StartDebugAsync(driver, "stability-context-start");
        var originalCandidate = await GetCandidateAsync(driver, debugSessionId, "stability-context-capabilities");
        var wait = await CallToolAsync(
            driver,
            "wait_for_ui_stable",
            SceneArguments(debugSessionId, originalCandidate, sampleCount: 2),
            "stability-context-wait",
            isError: false);
        Assert.False(Boolean(Object(wait.StructuredContent["stability"])["revalidatedByCapture"]));

        _ = await driver.PublishSecondWindowedDescendantAsync();
        var staleCaptureArguments = SceneArguments(debugSessionId, originalCandidate, sampleCount: 2);
        staleCaptureArguments["evidenceScope"] = new JsonObject { ["kind"] = "window" };
        var capture = await CallToolAsync(
            driver,
            "capture_visual_evidence",
            staleCaptureArguments,
            "stability-context-capture",
            isError: true);

        AssertSchemaValid("capture_visual_evidence", capture.StructuredContent);
        Assert.Equal("tool_error", Text(capture.StructuredContent["kind"]));
        Assert.Contains(Text(capture.StructuredContent["code"]), new[] { "CANDIDATE_MISMATCH", "OBSERVER_UNAVAILABLE" });
        Assert.DoesNotContain(
            EnumeratePropertyNames(capture.StructuredContent),
            name => name is "artifactId" or "artifacts" or "captureId" or "rasterCaptureId" or "dataBase64" or "path" or "root");
    }

    private static async Task<JsonObject> GetCandidateAsync(ModernMcpProcessDriver driver, string debugSessionId, string requestId)
    {
        var declaration = await CallToolAsync(
            driver,
            "get_ui_probe_capabilities",
            new JsonObject
            {
                ["debugSessionId"] = debugSessionId,
                ["protocolVersion"] = "native-scene-probe/1",
                ["schemaVersion"] = "native-scene-probe.schema/1",
            },
            requestId,
            isError: false);
        return Object(declaration.StructuredContent["candidate"]);
    }

    private static Task<ModernMcpProcessDriver> StartWindowedDescendantDriverAsync(bool publishSecondWindowedDescendantAfterRelease = false) =>
        ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(
                    SpawnWindowedDescendant: true,
                    PublishSecondWindowedDescendantAfterRelease: publishSecondWindowedDescendantAfterRelease),
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["FLAUI_BRIDGE_PATH"] = ResolveBridgeAssemblyPath(),
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

    private static JsonObject SceneArguments(string debugSessionId, JsonObject candidate, int sampleCount)
    {
        var arguments = new JsonObject
        {
            ["debugSessionId"] = debugSessionId,
            ["protocolVersion"] = "native-scene-probe/1",
            ["schemaVersion"] = "native-scene-probe.schema/1",
            ["sceneRequest"] = SceneRequest(sampleCount),
        };
        var expectation = Object(arguments["sceneRequest"])["expectedCandidateIdentity"]!.AsObject();
        expectation["executableSha256"] = candidate["executableSha256"]?.DeepClone();
        expectation["assemblyVersion"] = candidate["assemblyVersion"]?.DeepClone();
        expectation["probeVersion"] = candidate["probeVersion"]?.DeepClone();
        return arguments;
    }

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
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() ?? false);
        return new ToolCall(result, Object(result["structuredContent"]));
    }

    private static void AssertSchemaValid(string tool, JsonObject content)
    {
        var validation = NativeSceneContractCatalogDriver.Load().ValidateResult(tool, content.ToJsonString());
        Assert.True(validation.IsValid, $"Expected schema-valid {tool} content, got {validation.Code ?? "<null>"}: {validation.Message ?? "<null>"}.");
    }

    private static void AssertConditionEvidence(JsonObject stability)
    {
        Assert.Contains(Text(stability["status"]), new[] { "STABLE", "PARTIAL", "UNOBSERVABLE" });
        Assert.Equal(6, Conditions(stability).Count);
        Assert.All(
            Conditions(stability),
            condition => Assert.Contains(Text(condition.Value!.AsObject()["state"]), new[] { "met", "not_met", "unsupported", "unobservable" }));
    }

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

    private sealed record ToolCall(JsonObject Result, JsonObject StructuredContent);

    private static JsonObject SceneRequest(int sampleCount, int timeoutMs = 30_000) => new()
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
            ["timeoutMs"] = timeoutMs,
            ["sampleCount"] = sampleCount,
            ["stableForMs"] = 0,
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
            ["executableSha256"] = new string('b', 64),
            ["assemblyVersion"] = "1.0.0",
            ["probeVersion"] = "1.0.0",
        },
    };
    private static JsonObject StabilityReceipt(JsonObject stability) => new()
    {
        ["kind"] = "ui_stability_receipt",
        ["protocolVersion"] = "native-scene-probe/1",
        ["schemaVersion"] = "native-scene-probe.schema/1",
        ["candidate"] = new JsonObject
        {
            ["processId"] = 4242,
            ["processIdentity"] = "process_4242_start_1",
            ["hwnd"] = null,
            ["executableSha256"] = new string('b', 64),
            ["assemblyVersion"] = "1.0.0",
            ["probeVersion"] = "1.0.0",
            ["observerVersions"] = new JsonArray(),
            ["contractSetHash"] = new string('a', 64),
            ["storyHash"] = null,
            ["capturedAt"] = Start.ToString("O", System.Globalization.CultureInfo.InvariantCulture),
            ["source"] = new JsonObject
            {
                ["kind"] = "probe_manifest",
                ["verification"] = "verified",
            },
        },
        ["stability"] = stability.DeepClone(),
    };


    private static void AssertAllConditions(JsonObject stability, string expectedState)
    {
        var conditions = Conditions(stability);
        Assert.Equal(
            new[]
            {
                "animationState",
                "asyncLoadSettled",
                "contextMaterialization",
                "dispatcherIdle",
                "stableLayout",
                "windowGeometry",
            },
            conditions.Select(static condition => condition.Key).OrderBy(static name => name, StringComparer.Ordinal));
        Assert.All(conditions, condition => Assert.Equal(expectedState, Text(condition.Value!.AsObject()["state"])));
    }

    private static JsonObject Conditions(JsonObject stability) => Object(stability["conditions"]);

    private static JsonObject Object(JsonNode? node) => Assert.IsType<JsonObject>(node);

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();

    private static int Integer(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<int>();

    private static bool Boolean(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<bool>();

    private static DateTimeOffset Timestamp(JsonNode? node) => DateTimeOffset.Parse(Text(node), System.Globalization.CultureInfo.InvariantCulture);

    private sealed class ControlledStabilityFixture
    {
        private readonly Dictionary<string, string> _conditions = new(StringComparer.Ordinal)
        {
            ["dispatcherIdle"] = "met",
            ["stableLayout"] = "met",
            ["animationState"] = "met",
            ["windowGeometry"] = "met",
            ["contextMaterialization"] = "met",
            ["asyncLoadSettled"] = "met",
        };
        private readonly FakeTimeProvider _clock;
        private int _sceneEpoch = 41;
        private int _sequence;

        public ControlledStabilityFixture(FakeTimeProvider clock) => _clock = clock;

        public int ObservationCount { get; private set; }

        public void SetCondition(string name, string state, int sceneEpoch)
        {
            Assert.Contains(name, _conditions.Keys);
            Assert.Contains(state, new[] { "met", "not_met", "unsupported", "unobservable" });
            _conditions[name] = state;
            _sceneEpoch = sceneEpoch;
        }

        public Task<JsonObject> ObserveAsync(JsonElement sceneRequest, CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            Assert.True(sceneRequest.TryGetProperty("settlePolicy", out var policy));
            Assert.Equal(JsonValueKind.Object, policy.ValueKind);
            Assert.Equal("ControlledFixture", sceneRequest.GetProperty("fixtureId").GetString());
            ObservationCount++;

            var conditions = new JsonObject();
            foreach (var (name, state) in _conditions)
            {
                conditions[name] = new JsonObject { ["state"] = state };
            }

            return Task.FromResult(new JsonObject
            {
                ["conditions"] = conditions,
                ["sceneEpoch"] = _sceneEpoch,
                ["sequence"] = _sequence = ObservationCount,
                ["observedAt"] = _clock.GetUtcNow().ToString("O", System.Globalization.CultureInfo.InvariantCulture),
            });
        }
    }
    private sealed class DeadlineStabilitySampler
    {
        private readonly TaskCompletionSource<bool> _firstObservationStarted = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<JsonObject> _neverCompletingObservation = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private bool _useImmediateUnobservableEvidence;

        public Task FirstObservationStarted => _firstObservationStarted.Task;

        public void UseImmediateUnobservableEvidence() => _useImmediateUnobservableEvidence = true;

        public Task<JsonObject> ObserveAsync(JsonElement _, CancellationToken cancellationToken)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (!_useImmediateUnobservableEvidence)
            {
                _firstObservationStarted.TrySetResult(true);
                return _neverCompletingObservation.Task;
            }

            return Task.FromResult(new JsonObject
            {
                ["conditions"] = new JsonObject
                {
                    ["dispatcherIdle"] = new JsonObject { ["state"] = "met" },
                    ["stableLayout"] = new JsonObject { ["state"] = "met" },
                    ["animationState"] = new JsonObject { ["state"] = "met" },
                    ["windowGeometry"] = new JsonObject { ["state"] = "met" },
                    ["contextMaterialization"] = new JsonObject { ["state"] = "unobservable" },
                    ["asyncLoadSettled"] = new JsonObject { ["state"] = "met" },
                },
                ["sceneEpoch"] = 42,
            });
        }
    }
}

/// <summary>
/// Exact T021 reflection seam for the internal coordinator. A receipt is not
/// accepted by RevalidateForCaptureAsync: each capture path supplies the full
/// scene request and samples conditions again, so a previous wait cannot be an
/// authorization token.
/// </summary>
internal sealed class NativeSceneStabilityCoordinatorDriver
{
    private const string ProductionAssemblyName = "NetCoreDbg.Mcp.Stateless";
    private const string CoordinatorTypeName = "NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneStabilityCoordinator";

    private readonly object _coordinator;
    private readonly MethodInfo _waitForStableAsync;
    private readonly MethodInfo _revalidateForCaptureAsync;

    private NativeSceneStabilityCoordinatorDriver(
        object coordinator,
        MethodInfo waitForStableAsync,
        MethodInfo revalidateForCaptureAsync)
    {
        _coordinator = coordinator;
        _waitForStableAsync = waitForStableAsync;
        _revalidateForCaptureAsync = revalidateForCaptureAsync;
    }

    public static NativeSceneStabilityCoordinatorDriver Create(
        TimeProvider timeProvider,
        Func<JsonElement, CancellationToken, Task<JsonObject>> observeAsync)
    {
        var assemblyPath = TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root,
            Path.Combine("host", ProductionAssemblyName),
            ProductionAssemblyName);
        var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyPath);
        var coordinatorType = assembly.GetType(CoordinatorTypeName, throwOnError: false)
            ?? throw new InvalidOperationException(
                $"Missing production contract: type '{CoordinatorTypeName}' is absent from '{assembly.Location}'. " +
                "T022 must implement it without changing this RED suite.");
        Assert.False(coordinatorType.IsPublic, "NativeSceneStabilityCoordinator must remain an internal host authority.");

        var constructor = coordinatorType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(TimeProvider), typeof(Func<JsonElement, CancellationToken, Task<JsonObject>>)],
            modifiers: null)
            ?? throw new InvalidOperationException(
                "NativeSceneStabilityCoordinator must accept an injected TimeProvider and controlled JsonElement condition observer.");
        Assert.False(constructor.IsPublic, "The controlled condition observer is an internal host/test seam, never caller-selected MCP input.");

        var waitForStableAsync = RequireTaskOfJsonObjectMethod(
            coordinatorType,
            "WaitForStableAsync",
            typeof(JsonElement),
            typeof(CancellationToken));
        var revalidateForCaptureAsync = RequireTaskOfJsonObjectMethod(
            coordinatorType,
            "RevalidateForCaptureAsync",
            typeof(JsonElement),
            typeof(CancellationToken));
        var coordinator = constructor.Invoke([timeProvider, observeAsync]);
        Assert.NotNull(coordinator);

        return new NativeSceneStabilityCoordinatorDriver(coordinator!, waitForStableAsync, revalidateForCaptureAsync);
    }

    public Task<JsonObject> WaitForStableAsync(JsonElement sceneRequest, CancellationToken cancellationToken = default) =>
        InvokeAsync(_waitForStableAsync, sceneRequest, cancellationToken);

    public Task<JsonObject> RevalidateForCaptureAsync(JsonElement sceneRequest, CancellationToken cancellationToken = default) =>
        InvokeAsync(_revalidateForCaptureAsync, sceneRequest, cancellationToken);

    private async Task<JsonObject> InvokeAsync(MethodInfo method, JsonElement sceneRequest, CancellationToken cancellationToken)
    {
        var task = Assert.IsAssignableFrom<Task>(method.Invoke(_coordinator, [sceneRequest, cancellationToken]));
        await task.ConfigureAwait(false);
        return Assert.IsType<JsonObject>(task.GetType().GetProperty("Result", BindingFlags.Instance | BindingFlags.Public)?.GetValue(task));
    }

    private static MethodInfo RequireTaskOfJsonObjectMethod(Type type, string name, params Type[] parameterTypes)
    {
        var method = type.GetMethod(
            name,
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: parameterTypes,
            modifiers: null)
            ?? throw new InvalidOperationException($"{type.FullName} must expose {name}.");
        Assert.False(method.IsPublic, $"{type.FullName}.{name} must remain an internal host operation.");
        Assert.Equal(typeof(Task<JsonObject>), method.ReturnType);
        return method;
    }
}
