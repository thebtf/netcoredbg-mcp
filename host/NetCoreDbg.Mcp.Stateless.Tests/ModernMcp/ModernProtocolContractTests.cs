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
    public async Task ToolsList_ReturnsExactlyTheOrderedNativeCatalogWithRuntimeSchemas()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("catalog"));

        AssertCatalog(ModernMcpProcessDriver.RequireResult(response));
    }

    [Theory]
    [InlineData(129, "\U0001F600", "DEBUG_SESSION_NOT_FOUND")]
    [InlineData(256, "\U0001F600", "DEBUG_SESSION_NOT_FOUND")]
    [InlineData(256, "x", "DEBUG_SESSION_NOT_FOUND")]
    [InlineData(257, "\U0001F600", "INVALID_TOOL_ARGUMENTS")]
    public async Task UiInvoke_AutomationIdLength_MatchesPublishedSchemaBeforeSessionLookup(
        int characterCount,
        string character,
        string expectedError)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var catalog = ModernMcpProcessDriver.RequireResult(
            await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("invoke-length-schema")));
        var invoke = Assert.Single(
            Assert.IsType<JsonArray>(catalog["tools"]).Select(static tool => Assert.IsType<JsonObject>(tool)),
            static tool => tool["name"]?.GetValue<string>() == "ui_invoke");
        Assert.Equal(256, invoke["inputSchema"]?["properties"]?["automationId"]?["maxLength"]?.GetValue<int>());

        var response = await driver.CallToolRawAsync(
            "ui_invoke",
            new JsonObject
            {
                ["debugSessionId"] = "unknown-debug-session",
                ["automationId"] = string.Concat(Enumerable.Repeat(character, characterCount)),
            },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("invoke-length-call"));
        var result = ModernMcpProcessDriver.RequireResult(response);

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        await AssertNoNativeActionsAsync(driver);
        var error = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal(expectedError, (error["code"] ?? error["error"])?.GetValue<string>());
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

    [Fact]
    public async Task ToolsList_AddBreakpointPublishesClosedSourceAndPositiveInt32Input()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-breakpoint-catalog")));
        var tool = Assert.IsType<JsonObject>(Assert.IsType<JsonArray>(catalog["tools"])[9]);
        Assert.Equal("add_breakpoint", tool["name"]?.GetValue<string>());
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId", "file", "line"], Assert.IsType<JsonArray>(schema["required"]).Select(static value => value?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(3, properties.Count);
        foreach (var field in new[] { "debugSessionId", "file" })
        {
            Assert.Equal("string", properties[field]?["type"]?.GetValue<string>());
            Assert.Equal(1, properties[field]?["minLength"]?.GetValue<int>());
        }
        Assert.Equal("integer", properties["line"]?["type"]?.GetValue<string>());
        Assert.Equal(1, properties["line"]?["minimum"]?.GetValue<int>());
        Assert.Equal(int.MaxValue, properties["line"]?["maximum"]?.GetValue<int>());
    }

    [Theory]
    [InlineData("success", true)]
    [InlineData("success", false)]
    [InlineData("mixed", true)]
    public async Task AddBreakpoint_InitialAndTwoRuntimeAdditions_PreserveRequestedLinesAndInitialDuplicates(string responseMode, bool stopped)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(SetBreakpointsResponseMode: responseMode,
                LifecycleMode: stopped ? "all-stop" : "all-stop-continued-true")));
        var session = await StartBreakpointSessionAsync(driver, $"add-preserve-{responseMode}-{stopped}",
            (BreakpointSourcePath, 2), (BreakpointSourcePath, 6), (BreakpointSourcePath, 2));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync(stopped ? "stopped" : "continued", observation.Token);
        await BreakpointReaderBarrierAsync(driver, session, "add-preserve-reader-barrier", observation.Token);
        var state = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-preserve-state")), isError: false);
        Assert.Equal(stopped ? "stopped" : "continued", state["state"]?["event"]?.GetValue<string>());
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var first = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint",
            AddBreakpointArguments(session, Path.GetRelativePath(RepositoryLayout.Root, BreakpointSourcePath), 8),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-preserve-first")), isError: false);
        AssertAddedBreakpoint(first, BreakpointSourcePath, 8, true, responseMode == "mixed" ? 9 : 8);
        var second = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 10),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-preserve-second")), isError: false);
        AssertAddedBreakpoint(second, BreakpointSourcePath, 10, true, responseMode == "mixed" ? 11 : 10);
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.Equal(2, writes.Length);
        AssertBreakpointRequest(writes[0], BreakpointSourcePath, [2, 6, 2, 8]);
        AssertBreakpointRequest(writes[1], BreakpointSourcePath, [2, 6, 2, 8, 10]);

        var beforeDuplicate = (await driver.ReadNativeActionsAsync()).Count;
        var duplicate = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 8),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-preserve-duplicate")), isError: false);
        AssertAddedBreakpoint(duplicate, BreakpointSourcePath, 8, true, responseMode == "mixed" ? 9 : 8);
        var duplicateWrites = (await driver.ReadNativeActionsAsync()).Skip(beforeDuplicate).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.InRange(duplicateWrites.Length, 0, 1);
        foreach (var write in duplicateWrites)
        {
            AssertBreakpointRequest(write, BreakpointSourcePath, [2, 6, 2, 8, 10]);
        }
    }

    [Fact]
    public async Task AddBreakpoint_UnverifiedResult_PreservesFalseAndOmittedActualLine()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("mixed"));
        var session = await StartBreakpointSessionAsync(driver, "add-unverified-start");
        var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-unverified")), isError: false);
        AssertAddedBreakpoint(content, BreakpointSourcePath, 2, false, null);
        var second = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 6),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-after-unverified")), isError: false);
        AssertAddedBreakpoint(second, BreakpointSourcePath, 6, true, 7);
        AssertBreakpointRequest((await driver.ReadNativeActionsAsync()).Last(static action => action.Command == "setBreakpoints"), BreakpointSourcePath, [2, 6]);
    }

    [Fact]
    public async Task AddBreakpoint_ConcurrentSameSourceUpdates_SerializeWithoutLostLines()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("hold-first-runtime"));
        var session = await StartBreakpointSessionAsync(driver, "add-concurrent-start", (BreakpointSourcePath, 2));
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var first = driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 6),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-concurrent-first"), timeout: TimeSpan.FromSeconds(10));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("runtime-breakpoints-response-gated", observation.Token);
            var second = driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 8),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-concurrent-second"), timeout: TimeSpan.FromSeconds(10));
            await BreakpointReaderBarrierAsync(driver, session, "add-concurrent-reader-barrier", observation.Token);
            Assert.False(first.IsCompleted);
            Assert.False(second.IsCompleted);
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            driver.ReleaseNextResponse();
            var results = await Task.WhenAll(first, second).WaitAsync(observation.Token);
            AssertAddedBreakpoint(AssertBreakpointEnvelope(results[0], isError: false), BreakpointSourcePath, 6, true, 6);
            AssertAddedBreakpoint(AssertBreakpointEnvelope(results[1], isError: false), BreakpointSourcePath, 8, true, 8);
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            Assert.Equal(2, writes.Length);
            AssertBreakpointRequest(writes[0], BreakpointSourcePath, [2, 6]);
            AssertBreakpointRequest(writes[1], BreakpointSourcePath, [2, 6, 8]);
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task AddBreakpoint_InvalidArgumentsUnavailableTokenOrSource_PerformsNoDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var session = await StartBreakpointSessionAsync(driver, "add-invalid-start", (BreakpointSourcePath, 2));
        var invalid = new List<JsonObject?> { null };
        foreach (var field in new[] { "debugSessionId", "file", "line" })
        {
            var arguments = AddBreakpointArguments(session, BreakpointSourcePath, 6);
            arguments.Remove(field);
            invalid.Add(arguments);
        }
        foreach (var field in new[] { "debugSessionId", "file" })
        {
            foreach (var value in new JsonNode?[] { JsonValue.Create(""), JsonValue.Create(" \t "), JsonValue.Create(1), null })
            {
                var arguments = AddBreakpointArguments(session, BreakpointSourcePath, 6);
                arguments[field] = value?.DeepClone();
                invalid.Add(arguments);
            }
        }
        foreach (var value in new JsonNode?[] { JsonValue.Create(0), JsonValue.Create(-1), JsonValue.Create(1.5), JsonValue.Create(2_147_483_648L), JsonValue.Create("6"), null })
        {
            var arguments = AddBreakpointArguments(session, BreakpointSourcePath, 6);
            arguments["line"] = value;
            invalid.Add(arguments);
        }
        foreach (var field in new[] { "condition", "hitCondition", "unexpected" })
        {
            var arguments = AddBreakpointArguments(session, BreakpointSourcePath, 6);
            arguments[field] = "true";
            invalid.Add(arguments);
        }
        var before = (await driver.ReadNativeActionsAsync()).Count;
        for (var index = 0; index < invalid.Count; index++)
        {
            var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", invalid[index],
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-invalid-{index}")), isError: true);
            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
            Assert.Equal("add_breakpoint", content["tool"]?.GetValue<string>());
        }
        AssertFixedBreakpointError(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint",
            AddBreakpointArguments("unavailable-opaque-token", BreakpointSourcePath, 6), ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-unavailable-token")), isError: true),
            "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        AssertFixedBreakpointError(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint",
            AddBreakpointArguments(session, driver.InertProgramPath + ".missing.cs", 6), ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-unavailable-source")), isError: true),
            "breakpoint_source_unavailable", "BREAKPOINT_SOURCE_UNAVAILABLE");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task AddBreakpoint_RefusedUpdate_DoesNotContaminateLaterReplacement()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("refuse-first-runtime"));
        var session = await StartBreakpointSessionAsync(driver, "add-refused-start", (BreakpointSourcePath, 2));
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var refused = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 6),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-refused")), isError: true);
        AssertFixedBreakpointError(refused, "dap_set_breakpoints_refused", "DAP_SET_BREAKPOINTS_REFUSED");
        var state = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-refused-retained")), isError: false);
        Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
        var added = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 8),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-after-refusal")), isError: false);
        AssertAddedBreakpoint(added, BreakpointSourcePath, 8, true, 8);
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.Equal(2, writes.Length);
        AssertBreakpointRequest(writes[0], BreakpointSourcePath, [2, 6]);
        AssertBreakpointRequest(writes[1], BreakpointSourcePath, [2, 8]);
    }

    [Theory]
    [InlineData("missing-verified")]
    [InlineData("too-few-results")]
    [InlineData("reader-failure")]
    public async Task AddBreakpoint_MalformedOrUncertainResponse_ReturnsRedactedProtocolErrorAndDrains(string responseMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions(responseMode));
        var session = await StartBreakpointSessionAsync(driver, $"add-malformed-{responseMode}-start");
        var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-malformed-{responseMode}")), isError: true);
        AssertFixedBreakpointError(content, "dap_set_breakpoints_protocol_error", "DAP_SET_BREAKPOINTS_PROTOCOL_ERROR");
        await AssertBreakpointSessionNotFoundAsync(driver, session, $"add-malformed-{responseMode}-state");
    }

    [Fact]
    public async Task AddBreakpoint_PostWriteCallerCancellation_DrainsBeforeReuse()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("hold-first-runtime"));
        var session = await StartBreakpointSessionAsync(driver, "add-cancel-start", (BreakpointSourcePath, 2));
        using var cancellation = new CancellationTokenSource();
        var operation = driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 6),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-cancel"), cancellation.Token, timeout: TimeSpan.FromSeconds(10));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureRecordAsync("runtime-breakpoints-response-gated", observation.Token);
        await BreakpointReaderBarrierAsync(driver, session, "add-cancel-reader-barrier", observation.Token);
        Assert.False(operation.IsCompleted);
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
        await driver.WaitForFixtureRecordAsync("disconnect-response", observation.Token);
        await AssertBreakpointSessionNotFoundAsync(driver, session, "add-cancel-state");
        var before = (await driver.ReadNativeActionsAsync()).Count;
        AssertFixedBreakpointError(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 8),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-cancel-reuse")), isError: true), "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task AddBreakpoint_SeparateSessions_DoNotShareRuntimeConfiguration()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var first = await StartBreakpointSessionAsync(driver, "add-isolation-first", (BreakpointSourcePath, 2));
        var second = await StartBreakpointSessionAsync(driver, "add-isolation-second", (BreakpointSourcePath, 4));
        Assert.NotEqual(first, second);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        foreach (var (session, line) in new[] { (first, 6), (second, 8), (first, 10), (second, 12) })
        {
            AssertAddedBreakpoint(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, line),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-isolation-{line}")), isError: false), BreakpointSourcePath, line, true, line);
        }
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.Equal(4, writes.Length);
        AssertBreakpointRequest(writes[0], BreakpointSourcePath, [2, 6]);
        AssertBreakpointRequest(writes[1], BreakpointSourcePath, [4, 8]);
        AssertBreakpointRequest(writes[2], BreakpointSourcePath, [2, 6, 10]);
        AssertBreakpointRequest(writes[3], BreakpointSourcePath, [4, 8, 12]);
    }

    [Fact]
    public async Task AddBreakpoint_TotalSessionLimit_RejectsNewLineWithoutIoButAllowsExistingLine()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var otherSource = Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs");
        var seeds = Enumerable.Range(1, 128).Select(line => (BreakpointSourcePath, line))
            .Concat(Enumerable.Range(1, 128).Select(line => (otherSource, line))).ToArray();
        var session = await StartBreakpointSessionAsync(driver, "add-limit-start", seeds);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        AssertFixedBreakpointError(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 129),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-limit-new-line")), isError: true), "breakpoint_limit_reached", "BREAKPOINT_LIMIT_REACHED");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        AssertAddedBreakpoint(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-limit-existing-line")), isError: false), BreakpointSourcePath, 2, true, 2);
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.InRange(writes.Length, 0, 1);
        foreach (var write in writes)
        {
            AssertBreakpointRequest(write, BreakpointSourcePath, Enumerable.Range(1, 128).ToArray());
        }
    }

    [Theory]
    [InlineData("all-stop-exited", "exited")]
    [InlineData("all-stop-terminated", "terminated")]
    public async Task AddBreakpoint_AlreadyObservedTerminalSession_RefusesWithoutDapIo(string lifecycleMode, string terminal)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: lifecycleMode)));
        var session = await StartBreakpointSessionAsync(driver, $"add-terminal-{terminal}-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync(terminal, observation.Token);
        await BreakpointReaderBarrierAsync(driver, session, $"add-terminal-{terminal}-reader-barrier", observation.Token);
        var state = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-terminal-{terminal}-state")), isError: false);
        Assert.Equal(terminal, state["state"]?["event"]?.GetValue<string>());
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var refused = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-terminal-{terminal}")), isError: true);
        Assert.NotEqual("breakpoint_added", refused["kind"]?.GetValue<string>());
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    private static JsonObject AddBreakpointArguments(string session, string file, int line) => new()
    {
        ["debugSessionId"] = session,
        ["file"] = file,
        ["line"] = line,
    };

    private static async Task<string> StartBreakpointSessionAsync(ModernMcpProcessDriver driver, string requestId, params (string File, int Line)[] breakpoints)
    {
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", BreakpointArguments(driver, breakpoints),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)));
        AssertCompleteStartEnvelope(result);
        return Assert.IsType<string>(result["structuredContent"]?["debugSessionId"]?.GetValue<string>());
    }

    private static async Task BreakpointReaderBarrierAsync(ModernMcpProcessDriver driver, string session, string requestId, CancellationToken cancellationToken)
    {
        var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId), cancellationToken), isError: false);
        Assert.Equal("threads_success", content["kind"]?.GetValue<string>());
    }

    private static async Task AssertBreakpointSessionNotFoundAsync(ModernMcpProcessDriver driver, string session, string requestId)
    {
        var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: true);
        AssertFixedBreakpointError(content, "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
    }

    private static JsonObject AssertBreakpointEnvelope(JsonRpcResponse response, bool isError)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() == true);
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", text["type"]?.GetValue<string>());
        Assert.Equal(content.ToJsonString(), text["text"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        return content;
    }

    private static void AssertAddedBreakpoint(JsonObject content, string file, int line, bool verified, int? actualLine)
    {
        Assert.Equal("breakpoint_added", content["kind"]?.GetValue<string>());
        Assert.Equal(Path.GetFullPath(file), content["file"]?.GetValue<string>());
        Assert.Equal(line, content["line"]?.GetValue<int>());
        Assert.Equal(verified, content["verified"]?.GetValue<bool>());
        Assert.Equal(actualLine.HasValue, content.ContainsKey("actualLine"));
        if (actualLine is { } actual)
        {
            Assert.Equal(actual, content["actualLine"]?.GetValue<int>());
        }
        Assert.Equal(actualLine.HasValue ? 5 : 4, content.Count);
    }

    private static void AssertFixedBreakpointError(JsonObject content, string kind, string error)
    {
        Assert.Equal(kind, content["kind"]?.GetValue<string>());
        Assert.Equal(error, content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
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
        Assert.Equal(20, tools.Count);
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
                "add_breakpoint",
                "remove_breakpoint",
                "ui_invoke",
                "get_output",
                "get_output_tail",
                "get_ui_probe_capabilities",
                "capture_visual_evidence",
                "read_capture_artifact",
                "wait_for_ui_stable",
                "capture_element_snapshot",
                "capture_native_scene",
            ],
            tools.Select(static tool => tool?["name"]?.GetValue<string>()));
        Assert.Equal(
            ["start_debug", "get_debug_state", "stop_debug", "get_threads", "get_call_stack", "step_over", "get_scopes", "get_variables", "continue_execution", "add_breakpoint", "remove_breakpoint"],
            tools.Take(11).Select(static tool => tool?["name"]?.GetValue<string>()));

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

        var invoke = Assert.IsType<JsonObject>(tools[11]);
        var invokeSchema = Assert.IsType<JsonObject>(invoke["inputSchema"]);
        Assert.Equal("object", invokeSchema["type"]?.GetValue<string>());
        Assert.False(invokeSchema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId", "automationId"], Assert.IsType<JsonArray>(invokeSchema["required"]).Select(static value => value?.GetValue<string>()));
        var invokeProperties = Assert.IsType<JsonObject>(invokeSchema["properties"]);
        Assert.Equal(2, invokeProperties.Count);
        foreach (var property in invokeProperties.Select(static member => Assert.IsType<JsonObject>(member.Value)))
        {
            Assert.Equal("string", property["type"]?.GetValue<string>());
            Assert.Equal(1, property["minLength"]?.GetValue<int>());
            Assert.Equal("\\S", property["pattern"]?.GetValue<string>());
        }
        Assert.Equal(256, invokeProperties["automationId"]?["maxLength"]?.GetValue<int>());
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
