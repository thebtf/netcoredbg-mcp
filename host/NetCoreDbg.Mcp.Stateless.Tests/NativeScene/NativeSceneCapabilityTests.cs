using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
public sealed class NativeSceneCapabilityTests
{
    private const string ActiveProtocolVersion = "native-scene-probe/1";
    private const string ActiveSchemaVersion = "native-scene-probe.schema/1";

    private static readonly (string Name, string Milestone, string Availability)[] ExpectedPrimitives =
    [
        ("get_ui_probe_capabilities", "M0", "supported"),
        ("capture_visual_evidence", "M0", "unsupported"),
        ("read_capture_artifact", "M0", "unsupported"),
        ("wait_for_ui_stable", "M1", "unsupported"),
        ("capture_element_snapshot", "M1", "unsupported"),
        ("capture_native_scene", "M1", "unsupported"),
    ];

    private static readonly (string Name, string Availability)[] ExpectedContextStates =
    [
        ("storyId", "unsupported"),
        ("sceneId", "unsupported"),
        ("fixtureId", "unsupported"),
        ("scope", "unsupported"),
        ("appearance", "unsupported"),
        ("theme", "unsupported"),
        ("density", "unsupported"),
        ("contrast", "unsupported"),
        ("viewport", "unsupported"),
        ("expectedDpiPolicy", "unsupported"),
        ("focusTarget", "unsupported"),
        ("selectedState", "unsupported"),
        ("currentState", "unsupported"),
        ("scrollOffsets", "unsupported"),
        ("animationPolicy", "unsupported"),
    ];

    private static readonly (string Name, string Availability)[] ExpectedSettleConditionStates =
    [
        ("dispatcherIdle", "unsupported"),
        ("stableLayout", "unsupported"),
        ("animationState", "unsupported"),
        ("windowGeometry", "unsupported"),
        ("contextMaterialization", "unsupported"),
        ("asyncLoadSettled", "unsupported"),
    ];

    private static readonly (string Name, int Value)[] ExpectedLimits =
    [
        ("artifactReadMaxBytes", 65_536),
        ("losslessArtifactMaxBytes", 67_108_864),
        ("sceneArtifactMaxBytes", 16_777_216),
        ("structuredResponseMaxBytes", 262_144),
        ("sceneGraphMaxNodes", 4_096),
        ("issuesMaxCount", 256),
        ("artifactRefsMaxCount", 4),
        ("retentionMaxSeconds", 14_400),
        ("settleTimeoutMaxMs", 30_000),
        ("settleSampleCountMin", 2),
        ("settleSampleCountMax", 16),
    ];

    [Fact]
    public async Task ActiveLocalSession_DeclaresTheFrozenCapabilityContractWithoutArtifactEnumeration()
    {
        await using var driver = await StartDescendantDriverAsync();
        var debugSessionId = await StartDebugAsync(driver, "capabilities-start");

        var declaration = await AssertNoNativeActionsAsync(
            driver,
            () => GetCapabilitiesAsync(driver, debugSessionId, "capabilities"));

        Assert.Equal("ui_probe_capabilities", Text(declaration["kind"]));
        Assert.Equal(ActiveProtocolVersion, Text(declaration["protocolVersion"]));
        Assert.Equal(ActiveSchemaVersion, Text(declaration["schemaVersion"]));

        var candidate = Object(declaration["candidate"]);
        Assert.True(Integer(candidate["processId"]) > 0);
        Assert.Equal(await driver.ReadDescendantProcessIdAsync(), Integer(candidate["processId"]));
        Assert.Matches("^[a-f0-9]{64}$", Text(candidate["executableSha256"]));
        Assert.False(string.IsNullOrWhiteSpace(Text(candidate["processIdentity"])));
        var source = Object(candidate["source"]);
        Assert.Equal("launch_manifest", Text(source["kind"]));
        Assert.Equal("verified", Text(source["verification"]));

        var capabilities = Object(declaration["capabilities"]);
        Assert.Equal([ActiveProtocolVersion], Strings(Array(capabilities["supportedProtocolVersions"])));
        Assert.Equal([ActiveSchemaVersion], Strings(Array(capabilities["supportedSchemaVersions"])));
        Assert.Equal(
            ExpectedPrimitives.OrderBy(static primitive => primitive.Name, StringComparer.Ordinal),
            Array(capabilities["primitives"])
                .Select(Object)
                .Select(primitive => (Text(primitive["name"]), Text(primitive["milestone"]), Text(primitive["availability"])))
                .OrderBy(static primitive => primitive.Item1, StringComparer.Ordinal));
        AssertCapabilityStates(Object(capabilities["context"]), ExpectedContextStates);
        AssertCapabilityStates(Object(capabilities["settleConditions"]), ExpectedSettleConditionStates);
        Assert.Equal("unsupported", Text(capabilities["losslessVisualEvidence"]));

        _ = await AssertNoNativeActionsAsync(
            driver,
            () => AssertToolErrorAsync(
                driver,
                "capture_visual_evidence",
                VisualCaptureArguments(debugSessionId, candidate, sampleCount: 2, mismatchCandidate: false),
                "unsupported-visual-evidence",
                "UNSUPPORTED_CAPABILITY"));
        _ = await AssertNoNativeActionsAsync(
            driver,
            () => AssertToolErrorAsync(
                driver,
                "read_capture_artifact",
                ArtifactReadArguments(debugSessionId),
                "unsupported-artifact-read",
                "UNSUPPORTED_CAPABILITY"));

        var limits = Object(capabilities["limits"]);
        Assert.Equal(
            ExpectedLimits.Select(static limit => limit.Name).OrderBy(static name => name, StringComparer.Ordinal),
            limits.Select(static limit => limit.Key).OrderBy(static name => name, StringComparer.Ordinal));
        foreach (var (name, value) in ExpectedLimits)
        {
            Assert.Equal(value, Integer(limits[name]));
        }

        Assert.DoesNotContain(EnumeratePropertyNames(declaration), static name => name == "artifactId");
    }

    [Fact]
    public async Task MissingAndUnknownSessions_AreRejectedBeforeNativeTargetDiscovery()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        _ = await AssertNoNativeActionsAsync(
            driver,
            () => AssertToolErrorAsync(
                driver,
                "get_ui_probe_capabilities",
                CapabilityArguments(debugSessionId: null),
                "missing-session",
                "INVALID_TOOL_ARGUMENTS"));
        _ = await AssertNoNativeActionsAsync(
            driver,
            () => AssertToolErrorAsync(
                driver,
                "get_ui_probe_capabilities",
                CapabilityArguments(new string('a', 32)),
                "unknown-session",
                "DEBUG_SESSION_NOT_FOUND"));
    }

    [Fact]
    public async Task StaleSession_IsRejectedBeforeNativeTargetDiscovery()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var debugSessionId = await StartDebugAsync(driver, "stale-start");
        await StopDebugAsync(driver, debugSessionId, "stale-stop");

        _ = await AssertNoNativeActionsAsync(
            driver,
            () => AssertToolErrorAsync(
                driver,
                "get_ui_probe_capabilities",
                CapabilityArguments(debugSessionId),
                "stale-capabilities",
                "DEBUG_SESSION_NOT_FOUND"));
    }

    [Fact]
    public async Task PriorHostSession_IsRejectedAsRemoteBeforeNativeTargetDiscovery()
    {
        string remoteDebugSessionId;
        await using (var remoteDriver = await ModernMcpProcessDriver.StartAsync())
        {
            remoteDebugSessionId = await StartDebugAsync(remoteDriver, "remote-start");
        }

        await using var driver = await ModernMcpProcessDriver.StartAsync();
        _ = await AssertNoNativeActionsAsync(
            driver,
            () => AssertToolErrorAsync(
                driver,
                "get_ui_probe_capabilities",
                CapabilityArguments(remoteDebugSessionId),
                "remote-capabilities",
                "DEBUG_SESSION_NOT_FOUND"));
    }

    [Fact]
    public async Task MismatchedCandidate_IsRejectedBeforeNativeTargetDiscovery()
    {
        await using var driver = await StartDescendantDriverAsync();
        var debugSessionId = await StartDebugAsync(driver, "mismatch-start");
        var declaration = await AssertNoNativeActionsAsync(
            driver,
            () => GetCapabilitiesAsync(driver, debugSessionId, "mismatch-capabilities"));
        var candidate = Object(declaration["candidate"]);
        Assert.Equal(await driver.ReadDescendantProcessIdAsync(), Integer(candidate["processId"]));

        _ = await AssertNoNativeActionsAsync(
            driver,
            () => AssertToolErrorAsync(
                driver,
                "capture_visual_evidence",
                VisualCaptureArguments(debugSessionId, candidate, sampleCount: 2, mismatchCandidate: true),
                "mismatch-capture",
                "CANDIDATE_MISMATCH"));
    }

    [Fact]
    public async Task EveryInRangeSampleCount_ReachesTheDeclaredUnsupportedM1CapabilityWithoutNativeTargetDiscovery()
    {
        await using var driver = await StartDescendantDriverAsync();
        var debugSessionId = await StartDebugAsync(driver, "samples-start");
        var declaration = await AssertNoNativeActionsAsync(
            driver,
            () => GetCapabilitiesAsync(driver, debugSessionId, "samples-capabilities"));
        var candidate = Object(declaration["candidate"]);
        Assert.Equal(await driver.ReadDescendantProcessIdAsync(), Integer(candidate["processId"]));

        for (var sampleCount = 2; sampleCount <= 16; sampleCount++)
        {
            var currentSampleCount = sampleCount;
            _ = await AssertNoNativeActionsAsync(
                driver,
                () => AssertToolErrorAsync(
                    driver,
                    "wait_for_ui_stable",
                    SceneArguments(debugSessionId, candidate, currentSampleCount, mismatchCandidate: false),
                    $"sample-{currentSampleCount}",
                    "UNSUPPORTED_CAPABILITY"));
        }
    }

    [Fact]
    public async Task ExistingDebugToolLifecycle_RemainsUnchangedAlongsideTheNativeSceneFrontDoor()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var debugSessionId = await StartDebugAsync(driver, "legacy-start");

        var state = CompleteContent(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("legacy-state")), isError: false);
        Assert.Equal("debug_state_success", Text(state["kind"]));
        Assert.Equal(debugSessionId, Text(state["debugSessionId"]));

        await StopDebugAsync(driver, debugSessionId, "legacy-stop");

        var stopped = CompleteContent(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("legacy-after-stop")), isError: true);
        Assert.Equal("debug_session_not_found", Text(stopped["kind"]));
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", Text(stopped["error"]));
    }

    private static Task<ModernMcpProcessDriver> StartDescendantDriverAsync() =>
        ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true)));

    private static async Task<string> StartDebugAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var content = CompleteContent(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId)), isError: false);
        Assert.Equal("start_debug_success", Text(content["kind"]));
        return Text(content["debugSessionId"]);
    }

    private static async Task StopDebugAsync(ModernMcpProcessDriver driver, string debugSessionId, string requestId)
    {
        var content = CompleteContent(await driver.CallToolRawAsync(
            "stop_debug",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId)), isError: false);
        Assert.Equal("stop_debug_success", Text(content["kind"]));
    }

    private static Task<JsonObject> GetCapabilitiesAsync(ModernMcpProcessDriver driver, string debugSessionId, string requestId) =>
        CompleteContentAsync(driver, "get_ui_probe_capabilities", CapabilityArguments(debugSessionId), requestId, isError: false);

    private static JsonObject CapabilityArguments(string? debugSessionId)
    {
        var arguments = new JsonObject
        {
            ["protocolVersion"] = ActiveProtocolVersion,
            ["schemaVersion"] = ActiveSchemaVersion,
        };
        if (debugSessionId is not null)
        {
            arguments["debugSessionId"] = debugSessionId;
        }

        return arguments;
    }

    private static JsonObject VisualCaptureArguments(
        string debugSessionId,
        JsonObject candidate,
        int sampleCount,
        bool mismatchCandidate)
    {
        var arguments = SceneArguments(debugSessionId, candidate, sampleCount, mismatchCandidate);
        arguments["evidenceScope"] = new JsonObject { ["kind"] = "window" };
        return arguments;
    }

    private static JsonObject ArtifactReadArguments(string debugSessionId) => new()
    {
        ["debugSessionId"] = debugSessionId,
        ["protocolVersion"] = ActiveProtocolVersion,
        ["schemaVersion"] = ActiveSchemaVersion,
        ["artifactId"] = "artifact_visual_capability_0001",
        ["offset"] = 0,
        ["maxBytes"] = 1,
    };

    private static JsonObject SceneArguments(
        string debugSessionId,
        JsonObject candidate,
        int sampleCount,
        bool mismatchCandidate) =>
        new()
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
                    ["sampleCount"] = sampleCount,
                    ["stableForMs"] = 100,
                    ["requireDispatcherIdle"] = true,
                    ["requireStableLayout"] = true,
                    ["requireAnimationState"] = true,
                    ["requireWindowGeometry"] = true,
                    ["requireContextMaterialization"] = true,
                    ["requireAsyncLoadSettled"] = true,
                },
                ["contractSetHash"] = new string('a', 64),
                ["expectedCandidateIdentity"] = CandidateExpectation(candidate, mismatchCandidate),
            },
        };

    private static JsonObject CandidateExpectation(JsonObject candidate, bool mismatchCandidate)
    {
        var executableSha256 = Text(candidate["executableSha256"]);
        return new JsonObject
        {
            ["executableSha256"] = mismatchCandidate ? DifferentSha256(executableSha256) : executableSha256,
            ["assemblyVersion"] = candidate["assemblyVersion"]?.DeepClone(),
            ["probeVersion"] = candidate["probeVersion"]?.DeepClone(),
        };
    }

    private static string DifferentSha256(string value) =>
        (value[0] == '0' ? "1" : "0") + value[1..];

    private static async Task<JsonObject> AssertToolErrorAsync(
        ModernMcpProcessDriver driver,
        string tool,
        JsonObject arguments,
        string requestId,
        string expectedCode)
    {
        var content = await CompleteContentAsync(driver, tool, arguments, requestId, isError: true);
        Assert.Equal("tool_error", Text(content["kind"]));
        Assert.Equal(tool, Text(content["tool"]));
        Assert.Equal(expectedCode, Text(content["code"]));
        return content;
    }

    private static async Task<JsonObject> CompleteContentAsync(
        ModernMcpProcessDriver driver,
        string tool,
        JsonObject arguments,
        string requestId,
        bool isError) =>
        CompleteContent(await driver.CallToolRawAsync(
            tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId)), isError);

    private static JsonObject CompleteContent(JsonRpcResponse response, bool isError)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", Text(result["resultType"]));
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() ?? false);
        return Object(result["structuredContent"]);
    }

    private static async Task<T> AssertNoNativeActionsAsync<T>(ModernMcpProcessDriver driver, Func<Task<T>> action)
    {
        var before = await driver.ReadNativeActionsAsync();
        var result = await action();
        var after = await driver.ReadNativeActionsAsync();
        Assert.Equal(before.Count, after.Count);
        return result;
    }

    private static void AssertCapabilityStates(
        JsonObject capabilities,
        IEnumerable<(string Name, string Availability)> expectedStates)
    {
        Assert.Equal(
            expectedStates.OrderBy(static state => state.Name, StringComparer.Ordinal),
            capabilities
                .Select(capability => (capability.Key, Text(capability.Value)))
                .OrderBy(static state => state.Key, StringComparer.Ordinal));
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

    private static JsonObject Object(JsonNode? node) => Assert.IsType<JsonObject>(node);

    private static JsonArray Array(JsonNode? node) => Assert.IsType<JsonArray>(node);

    private static string[] Strings(JsonArray array) => array.Select(Text).ToArray();

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();

    private static int Integer(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<int>();
}
