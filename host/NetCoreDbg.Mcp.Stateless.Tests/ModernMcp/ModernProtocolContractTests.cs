using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;


[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class ModernProtocolContractTests
{
    private const string ValidProgram = "controlled-program.dll";

    [Fact]
    public async Task Discover_IsSupportedAsTheLiteralFirstFreshProcessRequest_AndDeclaresCacheableTools()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();

        var message = await driver.SendFirstRequestAsync(
            "server/discover",
            new JsonObject { ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
            new RequestId("discover-first"));
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(message));

        Assert.True(result["capabilities"]?["tools"] is JsonObject, "Discovery must declare the tools capability.");
        AssertCacheable(result);
    }

    [Fact]
    public async Task ListTools_IsSupportedAsTheLiteralFirstFreshProcessRequest_WithoutDiscoveryState()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();

        var message = await driver.SendFirstRequestAsync(
            "tools/list",
            new JsonObject { ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
            new RequestId("list-first"));
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(message));

        AssertCatalog(result);
        AssertCacheable(result);
    }

    [Fact]
    public async Task ValidStartCall_IsSupportedAsTheLiteralFirstFreshProcessRequest_WithoutDiscoveryState()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();

        var message = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject { ["program"] = ValidProgram },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
            },
            new RequestId("call-first"));

        AssertCompleteStartEnvelope(ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(message)));
    }

    [Fact]
    public async Task Metadata_IsEvaluatedPerRequest_AndIsNotRetainedAfterAnUnsupportedVersion()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var rejectedMeta = ModernMcpProcessDriver.CurrentMeta();
        rejectedMeta[MetaKeys.ProtocolVersion] = "1900-01-01";

        var rejected = await driver.SendFirstRequestAsync(
            "tools/list",
            new JsonObject { ["_meta"] = rejectedMeta },
            new RequestId("unsupported-version"));
        AssertExactUnsupportedVersionError(rejected);

        var current = await driver.SendFirstRequestAsync(
            "tools/list",
            new JsonObject { ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
            new RequestId("current-version"));
        AssertCatalog(ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(current)));
    }

    [Fact]
    public async Task UnsupportedVersion_ReturnsExactOfficialJsonRpcErrorData()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var meta = ModernMcpProcessDriver.CurrentMeta();
        meta[MetaKeys.ProtocolVersion] = "1900-01-01";

        var message = await driver.SendFirstRequestAsync(
            "server/discover",
            new JsonObject { ["_meta"] = meta },
            new RequestId("version-error"));
        AssertExactUnsupportedVersionError(message);
    }

    [Fact]
    public async Task ToolsList_ReturnsExactlyTheOrderedFifteenToolCatalogWithRuntimeSchemas()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("catalog"));

        AssertCatalog(ModernMcpProcessDriver.RequireResult(response));
    }

    [Fact]
    public async Task CompleteStart_ReturnsCallToolEnvelopeWithoutCacheFields()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var raw = await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = ValidProgram },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("complete-envelope"));

        AssertCompleteStartEnvelope(ModernMcpProcessDriver.RequireResult(raw));
    }

    [Fact]
    public async Task UnknownTool_ReturnsCompleteTextErrorWithoutApplicationStructuredContent()
    {
        const string unknownTool = "pr242-unknown-tool-schema";
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.CallToolRawAsync(
            unknownTool,
            new JsonObject(),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("unknown-tool"));
        var result = ModernMcpProcessDriver.RequireResult(response);

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.False(result.ContainsKey("structuredContent"));
        var content = Assert.Single(Assert.IsType<JsonArray>(result["content"]));
        Assert.Equal("text", Assert.IsType<JsonObject>(content)["type"]?.GetValue<string>());
        Assert.Equal($"Unknown tool: {unknownTool}", Assert.IsType<string>(Assert.IsType<JsonObject>(content)["text"]?.GetValue<string>()));
        await AssertNoNativeActionsAsync(driver);
    }

    [Fact]
    public async Task DelayedControlledStartup_ReturnsCompleteStartEnvelope()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(FixtureConfiguration: new(DelayLaunchResponseForStartupTimeout: true)));

        var response = await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = ValidProgram },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("startup-timeout"));
        var result = ModernMcpProcessDriver.RequireResult(response);

        AssertCompleteStartEnvelope(result);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task MissingProgram_WithFormElicitation_UsesInputRequiredAndNewIdRetryWithoutRequestState(bool includeBreakpoints)
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var initialId = new RequestId("input-required");
        var arguments = includeBreakpoints
            ? new JsonObject { ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = BreakpointSourcePath, ["line"] = 2 }) }
            : new JsonObject();
        var inputRequestId = await RequestProgramElicitationAsync(driver, initialId, arguments);
        await AssertNoNativeActionsAsync(driver);
        var retryId = new RequestId("input-required-retry");
        var retry = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = arguments.DeepClone(),
                ["inputResponses"] = new JsonObject { [inputRequestId] = new JsonObject { ["action"] = "accept", ["content"] = new JsonObject { ["program"] = ValidProgram } } },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(formElicitation: true),
            },
            retryId);

        Assert.NotEqual(initialId, retryId);
        AssertCompleteStartEnvelope(ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(retry)));
        if (includeBreakpoints)
        {
            AssertBreakpointRequest(Assert.Single(await driver.ReadNativeActionsAsync(),
                static action => action.Command == "setBreakpoints"), BreakpointSourcePath, [2]);
        }
    }

    [Theory]
    [InlineData("decline")]
    [InlineData("cancel")]
    public async Task MissingProgram_WithFormElicitation_DeclineOrCancelReturnsCompleteApplicationErrorWithoutNativeAction(string action)
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var inputRequestId = await RequestProgramElicitationAsync(driver, new RequestId($"input-required-{action}-initial"));

        var retry = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject(),
                ["inputResponses"] = new JsonObject { [inputRequestId] = new JsonObject { ["action"] = action } },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(formElicitation: true),
            },
            new RequestId($"input-required-{action}"));
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(retry));

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("start_debug_input_unavailable", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.Equal("START_DEBUG_PROGRAM_REQUIRED", result["structuredContent"]?["error"]?.GetValue<string>());
        Assert.False(result.ContainsKey("requestState"));
        await AssertNoNativeActionsAsync(driver);
    }

    [Fact]
    public async Task MissingProgram_WithoutFormElicitation_ReturnsCompleteApplicationErrorWithoutNativeAction()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();

        var response = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject(),
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
            },
            new RequestId("no-elicitation"));
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(response));

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("start_debug_input_unavailable", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.Equal("START_DEBUG_PROGRAM_REQUIRED", result["structuredContent"]?["error"]?.GetValue<string>());
        Assert.False(result.ContainsKey("requestState"));
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("start_debug", "program")]
    [InlineData("get_debug_state", "debugSessionId")]
    [InlineData("stop_debug", "debugSessionId")]
    public async Task EmptyArguments_ReturnCompleteInvalidArgumentErrorsBeforeNativeActions(string tool, string field)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.CallToolRawAsync(
            tool,
            new JsonObject { [field] = "" },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"empty-{tool}"));

        AssertInvalidArguments(ModernMcpProcessDriver.RequireResult(response), tool);
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("start_debug")]
    [InlineData("get_debug_state")]
    [InlineData("stop_debug")]
    public async Task ExtraArguments_ReturnCompleteInvalidArgumentErrorsBeforeNativeActions(string tool)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var arguments = tool == "start_debug"
            ? new JsonObject { ["program"] = ValidProgram, ["extra"] = true }
            : new JsonObject { ["debugSessionId"] = new string('x', 32), ["extra"] = true };

        var response = await driver.CallToolRawAsync(
            tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"extra-{tool}"));

        AssertInvalidArguments(ModernMcpProcessDriver.RequireResult(response), tool);
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("get_debug_state", null)]
    [InlineData("get_debug_state", "short")]
    [InlineData("get_debug_state", "not-a-capability")]
    [InlineData("stop_debug", null)]
    [InlineData("stop_debug", "short")]
    [InlineData("stop_debug", "not-a-capability")]
    public async Task MissingShortAndMalformedHandles_AreUniformNotFoundWithoutNativeActions(string tool, string? handle)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var arguments = new JsonObject();
        if (handle is not null)
        {
            arguments["debugSessionId"] = handle;
        }

        var response = await driver.CallToolRawAsync(
            tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"handle-{tool}-{handle ?? "missing"}"));
        var result = ModernMcpProcessDriver.RequireResult(response);

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("debug_session_not_found", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", result["structuredContent"]?["error"]?.GetValue<string>());
        await AssertNoNativeActionsAsync(driver);
    }
    [Fact]
    public async Task SuccessfulOfficialSdkExchange_ProvesStdoutContainsOnlyMcpFrames()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("stdout-purity"));

        AssertCatalog(ModernMcpProcessDriver.RequireResult(response));
        Assert.False(driver.Client.Completion.IsCompleted, "Non-MCP stdout would make the official stdio transport fail the exchange.");
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task StartDebug_AbsentOrEmptyBreakpoints_PreservesExistingLaunch(bool includeEmptyArray)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var arguments = new JsonObject { ["program"] = driver.InertProgramPath };
        if (includeEmptyArray)
        {
            arguments["breakpoints"] = new JsonArray();
        }

        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(
            "start_debug", arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId("breakpoints-empty")));

        AssertCompleteStartEnvelope(result);
        Assert.Null(result["structuredContent"]?["initialBreakpoints"]);
        Assert.Equal(["initialize", "launch", "configurationDone"], (await driver.ReadNativeActionsAsync())
            .Where(static action => action.Kind == "request").Select(static action => action.Command));
    }

    [Fact]
    public async Task StartDebug_InterleavedBreakpoints_GroupsBeforeLaunchAndRetainsInitialResults()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("mixed"));
        var secondSource = Path.Combine(RepositoryLayout.Root,
            "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs");
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug",
            BreakpointArguments(driver, (BreakpointSourcePath, 2), (secondSource, 4),
                (Path.GetRelativePath(RepositoryLayout.Root, BreakpointSourcePath), 6), (secondSource, 8), (BreakpointSourcePath, 2)),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("breakpoints-grouped")));

        AssertCompleteStartEnvelope(result);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureRecordAsync("breakpoint-verified-event", deadline.Token);
        var actions = await driver.ReadNativeActionsAsync();
        Assert.Equal(["initialize", "setBreakpoints", "setBreakpoints", "launch", "configurationDone"],
            actions.Where(static action => action.Kind == "request").Select(static action => action.Command));
        var requests = actions.Where(static action => action.Command == "setBreakpoints").ToArray();
        AssertBreakpointRequest(requests[0], BreakpointSourcePath, [2, 6, 2]);
        AssertBreakpointRequest(requests[1], secondSource, [4, 8]);
        var initial = Assert.IsType<JsonArray>(result["structuredContent"]?["initialBreakpoints"]);
        Assert.Equal([(0, false, (int?)null), (1, false, null), (2, true, 7), (3, true, 9), (4, true, 3)],
            initial.Select(static entry => (entry!["index"]!.GetValue<int>(), entry["verified"]!.GetValue<bool>(),
                entry["actualLine"]?.GetValue<int>())));
        Assert.DoesNotContain("private controlled breakpoint payload", result.ToJsonString());
    }

    [Theory]
    [InlineData("not-array")]
    [InlineData("invalid-line")]
    [InlineData("unsupported-condition")]
    [InlineData("over-limit")]
    [InlineData("missing-source")]
    public async Task StartDebug_InvalidBreakpointsOrMissingSource_PerformsNoAdapterWork(string scenario)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var arguments = BreakpointArguments(driver, (BreakpointSourcePath, 2));
        var entry = arguments["breakpoints"]![0]!;
        switch (scenario)
        {
            case "not-array": arguments["breakpoints"] = new JsonObject(); break;
            case "invalid-line": entry["line"] = 0; break;
            case "unsupported-condition": entry["condition"] = "true"; break;
            case "over-limit":
                arguments["breakpoints"] = new JsonArray(Enumerable.Range(0, 257)
                    .Select(_ => entry.DeepClone()).ToArray());
                break;
            case "missing-source": entry["file"] = driver.InertProgramPath + ".missing.cs"; break;
        }

        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(
            "start_debug", arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"breakpoints-{scenario}")));

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        if (scenario != "missing-source")
        {
            AssertInvalidArguments(result, "start_debug");
        }
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("refused")]
    [InlineData("missing-verified")]
    [InlineData("too-few-results")]
    public async Task StartDebug_RejectedOrMalformedBreakpoints_DrainsAdapterWithoutLaunching(string responseMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions(responseMode));
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug",
            BreakpointArguments(driver, (BreakpointSourcePath, 2), (BreakpointSourcePath, 6)),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"breakpoints-response-{responseMode}")));

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Null(result["structuredContent"]?["debugSessionId"]);
        Assert.DoesNotContain("private controlled breakpoint payload", result.ToJsonString());
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        var actions = await driver.ReadNativeActionsAsync();
        Assert.Single(actions, static action => action.Command == "setBreakpoints");
        Assert.DoesNotContain(actions, static action => action.Command is "launch" or "configurationDone");
        var transcriptPath = Assert.IsType<string>(Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT"));
        var transcript = (await File.ReadAllLinesAsync(transcriptPath, deadline.Token)).Select(FixtureTranscriptEntry.Parse);
        var processId = Assert.IsType<int>(Assert.Single(transcript, static entry => entry.Kind == "startup").ProcessId);
        try
        {
            using var adapter = Process.GetProcessById(processId);
            await adapter.WaitForExitAsync(deadline.Token);
        }
        catch (ArgumentException)
        {
            // The owned adapter has already exited.
        }
    }

    [Fact]
    public async Task StartDebug_SeparateSessions_DoNotShareBreakpointConfiguration()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var first = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug",
            BreakpointArguments(driver, (BreakpointSourcePath, 2)), ModernMcpProcessDriver.CurrentMeta(), new RequestId("breakpoints-first")));
        AssertCompleteStartEnvelope(first);
        var afterFirst = (await driver.ReadNativeActionsAsync()).Count;
        var second = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug",
            BreakpointArguments(driver, (BreakpointSourcePath, 6)), ModernMcpProcessDriver.CurrentMeta(), new RequestId("breakpoints-second")));
        AssertCompleteStartEnvelope(second);

        Assert.NotEqual(first["structuredContent"]?["debugSessionId"]?.GetValue<string>(),
            second["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        AssertBreakpointRequest(Assert.Single((await driver.ReadNativeActionsAsync()).Skip(afterFirst),
            static action => action.Command == "setBreakpoints"), BreakpointSourcePath, [6]);
        Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(first["structuredContent"]?["initialBreakpoints"]))?["actualLine"]?.GetValue<int>());
        Assert.Equal(6, Assert.Single(Assert.IsType<JsonArray>(second["structuredContent"]?["initialBreakpoints"]))?["actualLine"]?.GetValue<int>());
    }

    private static string BreakpointSourcePath => Path.Combine(RepositoryLayout.Root,
        "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/ModernProtocolContractTests.cs");

    private static ModernMcpStartOptions BreakpointLaunchOptions(string responseMode = "success") => new(
        FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, SetBreakpointsResponseMode: responseMode));

    private static JsonObject BreakpointArguments(ModernMcpProcessDriver driver, params (string File, int Line)[] breakpoints) => new()
    {
        ["program"] = driver.InertProgramPath,
        ["breakpoints"] = new JsonArray(breakpoints.Select(static entry => (JsonNode)new JsonObject
        { ["file"] = entry.File, ["line"] = entry.Line }).ToArray()),
    };

    private static void AssertBreakpointRequest(ModernNativeAction action, string source, int[] lines)
    {
        var arguments = Assert.IsType<JsonObject>(JsonNode.Parse(Assert.IsType<string>(action.Detail))?["arguments"]);
        Assert.Equal(Path.GetFullPath(source), arguments["source"]?["path"]?.GetValue<string>());
        Assert.Equal(lines, Assert.IsType<JsonArray>(arguments["breakpoints"]).Select(static entry => entry!["line"]!.GetValue<int>()));
    }

    private static void AssertCacheable(JsonObject result)
    {
        Assert.True(result["ttlMs"]?.GetValue<long>() > 0);
        Assert.Equal("public", result["cacheScope"]?.GetValue<string>());
    }

    private static void AssertCatalog(JsonObject result)
    {
        var tools = Assert.IsType<JsonArray>(result["tools"]);
        Assert.Equal(15, tools.Count);
        Assert.Equal(
            [
                "start_debug",
                "get_debug_state",
                "stop_debug",
                "get_threads",
                "get_call_stack",
                "step_over",
                "get_scopes",
                "get_variables",
                "continue_execution",
                "get_ui_probe_capabilities",
                "capture_visual_evidence",
                "read_capture_artifact",
                "wait_for_ui_stable",
                "capture_element_snapshot",
                "capture_native_scene",
            ],
            tools.Select(static tool => tool?["name"]?.GetValue<string>()));
        Assert.Equal(
            ["start_debug", "get_debug_state", "stop_debug", "get_threads", "get_call_stack", "step_over", "get_scopes", "get_variables", "continue_execution"],
            tools.Take(9).Select(static tool => tool?["name"]?.GetValue<string>()));

        var start = Assert.IsType<JsonObject>(tools[0]);
        Assert.Equal("object", start["inputSchema"]?["type"]?.GetValue<string>());
        Assert.False(start["inputSchema"]?["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(1, Assert.IsType<JsonObject>(start["inputSchema"]?["properties"])["program"]?["minLength"]?.GetValue<int>());
        var breakpoints = Assert.IsType<JsonObject>(start["inputSchema"]?["properties"]?["breakpoints"]);
        Assert.Equal(256, breakpoints["maxItems"]?.GetValue<int>());
        Assert.Equal("array", breakpoints["type"]?.GetValue<string>());
        Assert.False(breakpoints["items"]?["additionalProperties"]?.GetValue<bool>() ?? true);

        foreach (var tool in tools.Skip(1).Take(2).Select(static tool => Assert.IsType<JsonObject>(tool)))
        {
            var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
            Assert.Equal("object", schema["type"]?.GetValue<string>());
            Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
            Assert.Contains("debugSessionId", Assert.IsType<JsonArray>(schema["required"]).Select(static value => value?.GetValue<string>()));
            Assert.Equal(32, Assert.IsType<JsonObject>(schema["properties"])["debugSessionId"]?["minLength"]?.GetValue<int>());
        }

        var threads = Assert.IsType<JsonObject>(tools[3]);
        var threadsSchema = Assert.IsType<JsonObject>(threads["inputSchema"]);
        Assert.Equal("object", threadsSchema["type"]?.GetValue<string>());
        Assert.False(threadsSchema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId"], Assert.IsType<JsonArray>(threadsSchema["required"]).Select(static value => value?.GetValue<string>()));
        var debugSessionId = Assert.IsType<JsonObject>(threadsSchema["properties"])["debugSessionId"];
        Assert.Equal("string", debugSessionId?["type"]?.GetValue<string>());
        Assert.Equal(1, debugSessionId?["minLength"]?.GetValue<int>());
    }

    private static async Task<string> RequestProgramElicitationAsync(ModernMcpFirstWireDriver driver, RequestId initialId, JsonObject? arguments = null)
    {
        var initial = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = arguments?.DeepClone() ?? new JsonObject(),
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(formElicitation: true),
            },
            initialId);
        var initialResult = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(initial));

        Assert.Equal("input_required", initialResult["resultType"]?.GetValue<string>());
        var inputRequests = Assert.IsType<JsonObject>(initialResult["inputRequests"]);
        var inputRequest = Assert.Single(inputRequests);
        var inputRequestEnvelope = Assert.IsType<JsonObject>(inputRequest.Value);
        Assert.Equal("elicitation/create", inputRequestEnvelope["method"]?.GetValue<string>());
        Assert.Equal("form", inputRequestEnvelope["params"]?["mode"]?.GetValue<string>());
        Assert.False(initialResult.ContainsKey("requestState"));
        return inputRequest.Key;
    }

    private static void AssertCompleteStartEnvelope(JsonObject result)
    {
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.False(result["isError"]?.GetValue<bool>() ?? true);
        Assert.NotEmpty(Assert.IsType<JsonArray>(result["content"]));
        Assert.Equal("start_debug_success", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.NotNull(result["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        Assert.False(result.ContainsKey("ttlMs"));
        Assert.False(result.ContainsKey("cacheScope"));
    }

    private static void AssertInvalidArguments(JsonObject result, string tool)
    {
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("invalid_tool_arguments", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", result["structuredContent"]?["error"]?.GetValue<string>());
        Assert.Equal(tool, result["structuredContent"]?["tool"]?.GetValue<string>());
    }

    private static void AssertExactUnsupportedVersionError(JsonRpcMessage message)
    {
        var error = Assert.IsType<JsonRpcError>(message);

        Assert.Equal(-32022, error.Error.Code);
        var data = Assert.IsType<JsonElement>(error.Error.Data);
        Assert.Equal(JsonValueKind.Object, data.ValueKind);
        Assert.Equal(["requested", "supported"], data.EnumerateObject().Select(static property => property.Name).Order());
        Assert.True(data.TryGetProperty("requested", out var requested));
        Assert.Equal("1900-01-01", requested.GetString());
        Assert.True(data.TryGetProperty("supported", out var supported));
        Assert.Equal(JsonValueKind.Array, supported.ValueKind);
        Assert.Equal([ModernMcpProcessDriver.CurrentProtocolVersion], supported.EnumerateArray().Select(static value => value.GetString()));
    }



    private static async Task AssertNoNativeActionsAsync(ModernMcpProcessDriver driver)
    {
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }

    private static async Task AssertNoNativeActionsAsync(ModernMcpFirstWireDriver driver)
    {
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }
}
