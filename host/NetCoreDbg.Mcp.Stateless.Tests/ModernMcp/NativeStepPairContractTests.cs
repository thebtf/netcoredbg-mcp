using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeStepPairContractTests
{
    private static readonly string[] StepTools = ["step_into", "step_out"];
    private static readonly TimeSpan ObservationTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan OperationTimeout = TimeSpan.FromSeconds(40);

    public static IEnumerable<object?[]> InvalidArguments()
    {
        (string Scenario, JsonObject? Arguments)[] cases =
        [
            ("missing_arguments", null),
            ("omitted_debug_session_id", new() { ["threadId"] = 1 }),
            ("conflicting_thread_aliases", new() { ["debugSessionId"] = "opaque", ["threadId"] = null, ["thread_id"] = 1 }),
            ("empty_token", new() { ["debugSessionId"] = "", ["threadId"] = 1 }),
            ("whitespace_token", new() { ["debugSessionId"] = " \t", ["threadId"] = 1 }),
            ("null_token", new() { ["debugSessionId"] = null, ["threadId"] = 1 }),
            ("non_string_token", new() { ["debugSessionId"] = 1, ["threadId"] = 1 }),
            ("string_thread", new() { ["debugSessionId"] = "opaque", ["threadId"] = "1" }),
            ("boolean_thread", new() { ["debugSessionId"] = "opaque", ["threadId"] = true }),
            ("non_integral_thread", new() { ["debugSessionId"] = "opaque", ["threadId"] = 1.5 }),
            ("inexact_alias", new() { ["debugSessionId"] = "opaque", ["thread_id"] = JsonNode.Parse("1.0000000000000000001") }),
            ("underflow_thread", new() { ["debugSessionId"] = "opaque", ["threadId"] = -2_147_483_649L }),
            ("overflow_thread", new() { ["debugSessionId"] = "opaque", ["threadId"] = 2_147_483_648L }),
            ("target_is_not_input", new() { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["targetId"] = 3 }),
            ("single_thread_is_not_input", new() { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["singleThread"] = true }),
            ("granularity_is_not_input", new() { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["granularity"] = "line" }),
            ("extra_field", new() { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["unexpected"] = true }),
        ];
        foreach (var tool in StepTools)
            foreach (var item in cases)
                yield return [tool, item.Scenario, item.Arguments?.DeepClone()];
    }

    public static IEnumerable<object[]> ExecutionConflicts()
    {
        string[] executionTools = ["step_into", "step_out", "step_over", "continue_execution", "pause_execution"];
        foreach (var active in executionTools)
            foreach (var incoming in executionTools)
            {
                if (StepTools.Contains(active) || StepTools.Contains(incoming))
                    yield return [active, incoming];
            }
    }

    [Theory]
    [InlineData("step_into")]
    [InlineData("step_out")]
    public async Task ToolsList_StepPairPublishesOnlyRequiredOpaqueSessionAndInt32Thread(string tool)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"{tool}-catalog")));
        var descriptor = Assert.Single(Assert.IsType<JsonArray>(catalog["tools"]).Select(static entry => Assert.IsType<JsonObject>(entry)),
            entry => entry["name"]?.GetValue<string>() == tool);
        var schema = Assert.IsType<JsonObject>(descriptor["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(new[] { "debugSessionId" },
            Assert.IsType<JsonArray>(schema["required"]).Select(static value => value?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(3, properties.Count);
        Assert.Equal("string", properties["debugSessionId"]?["type"]?.GetValue<string>());
        Assert.Equal(1, properties["debugSessionId"]?["minLength"]?.GetValue<int>());
        Assert.Equal(new[] { "integer", "null" }, Assert.IsType<JsonArray>(properties["threadId"]?["type"]).Select(static value => value?.GetValue<string>()));
        Assert.True(JsonNode.DeepEquals(properties["threadId"], properties["thread_id"]));
        Assert.Equal(int.MinValue, properties["threadId"]?["minimum"]?.GetValue<int>());
        Assert.Equal(int.MaxValue, properties["threadId"]?["maximum"]?.GetValue<int>());
    }

    [Theory]
    [MemberData(nameof(InvalidArguments))]
    public async Task StepPair_InvalidInputNamesItsOwnToolAndWritesNothing(
        string tool, string scenario, JsonObject? input)
    {
        await using var driver = await StartDriverAsync();
        var session = await StartStoppedSessionAsync(driver);
        var arguments = input?.DeepClone().AsObject();
        if (arguments?["debugSessionId"] is JsonValue token && token.TryGetValue<string>(out var value) && value == "opaque")
            arguments["debugSessionId"] = session;
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var content = Envelope(await CallAsync(driver, tool, arguments, requestId: $"{tool}-{scenario}"), true);

        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(tool, content["tool"]?.GetValue<string>());
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("step_over", "omitted")]
    [InlineData("step_over", "null")]
    [InlineData("step_over", "alias")]
    [InlineData("step_over", "alias-null")]
    [InlineData("step_over", "zero")]
    [InlineData("step_into", "omitted")]
    [InlineData("step_into", "null")]
    [InlineData("step_into", "alias")]
    [InlineData("step_into", "alias-null")]
    [InlineData("step_into", "zero")]
    [InlineData("step_out", "omitted")]
    [InlineData("step_out", "null")]
    [InlineData("step_out", "alias")]
    [InlineData("step_out", "alias-null")]
    [InlineData("step_out", "zero")]
    public async Task StepFamily_OptionalThread_UsesObservedStoppedThread(string tool, string input)
    {
        await using var driver = await StartDriverAsync();
        var session = await StartStoppedSessionAsync(driver);
        var args = Arguments(session);
        if (input == "null") args["threadId"] = null;
        if (input == "alias") args["thread_id"] = JsonNode.Parse("1e0");
        if (input == "alias-null") args["thread_id"] = null;
        if (input == "zero") args["threadId"] = 0;
        var selected = input == "zero" ? 0 : 1;
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var result = Envelope(await CallAsync(driver, tool, args), false);
        Success(result, tool, selected);
        ExactRequest(Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before)), Command(tool), selected);
    }

    [Theory]
    [InlineData("step_over")]
    [InlineData("step_into")]
    [InlineData("step_out")]
    public async Task StepFamily_OptionalThreadWithoutCurrentStop_RefusesWithoutWrite(string tool)
    {
        await using var driver = await StartDriverAsync(lifecycleMode: "partial-stop-missing-thread");
        var session = await StartStoppedSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        Refused(Envelope(await CallAsync(driver, tool, Arguments(session)), true), tool);
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }


    [Theory]
    [InlineData("step_into", "!")]
    [InlineData("step_into", "not-a-capability")]
    [InlineData("step_out", "!")]
    [InlineData("step_out", "not-a-capability")]
    public async Task StepPair_UnknownOpaqueTokenReturnsUniformNotFoundWithoutDapIo(string tool, string token)
    {
        await using var driver = await StartDriverAsync();
        _ = await StartStoppedSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        FixedError(Envelope(await CallAsync(driver, tool, Arguments(token, 1)), true),
            "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");

        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("step_into", int.MinValue)]
    [InlineData("step_into", 0)]
    [InlineData("step_into", int.MaxValue)]
    [InlineData("step_out", int.MinValue)]
    [InlineData("step_out", 0)]
    [InlineData("step_out", int.MaxValue)]
    public async Task StepPair_AllStopWithoutStackCapabilitySendsExactlyOneTypedRequestAndObservesExactStep(
        string tool, int threadId)
    {
        await using var driver = await StartDriverAsync(supportsStack: false);
        var session = await StartStoppedSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        Success(Envelope(await CallAsync(driver, tool, Arguments(session, threadId)), false), tool, threadId);

        var action = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before));
        ExactRequest(action, Command(tool), threadId);
        var transcript = await driver.ReadFixtureTranscriptAsync();
        var request = Assert.Single(transcript, entry => entry.Kind == "request" && entry.Command == Command(tool));
        var response = Assert.Single(transcript, entry => entry.Kind == "response" && entry.Command == Command(tool)
            && entry.Sequence == request.Sequence);
        using var acknowledgement = JsonDocument.Parse(Assert.IsType<string>(response.RawPayload));
        Assert.True(acknowledgement.RootElement.GetProperty("success").GetBoolean());
        Assert.Equal(request.Sequence, acknowledgement.RootElement.GetProperty("request_seq").GetInt32());
        var state = await StateAsync(driver, session);
        Assert.Equal("stopped", state["event"]?.GetValue<string>());
        Assert.Equal("step", state["stopReason"]?.GetValue<string>());
        Assert.Equal(threadId, state["currentThreadId"]?.GetValue<int>());
    }

    [Theory]
    [InlineData("step_into", "partial-stop", 1, true)]
    [InlineData("step_into", "partial-stop", 2, false)]
    [InlineData("step_into", "partial-stop-missing-thread", 1, false)]
    [InlineData("step_out", "partial-stop", 1, true)]
    [InlineData("step_out", "partial-stop", 2, false)]
    [InlineData("step_out", "partial-stop-missing-thread", 1, false)]
    public async Task StepPair_OnlyCurrentStoppedAuthorityAdmitsTheExplicitTarget(
        string tool, string lifecycleMode, int threadId, bool authorized)
    {
        await using var driver = await StartDriverAsync(lifecycleMode: lifecycleMode);
        var session = await StartStoppedSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var result = Envelope(await CallAsync(driver, tool, Arguments(session, threadId)), !authorized);

        if (authorized)
        {
            Success(result, tool, threadId);
            ExactRequest(Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before)), Command(tool), threadId);
        }
        else
        {
            Refused(result, tool);
            Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        }
    }

    [Theory]
    [InlineData("step_into")]
    [InlineData("step_out")]
    public async Task StepPair_AdmissionClearsCurrentThreadAndInspectionBeforeHeldAck(string tool)
    {
        await using var driver = await StartDriverAsync("hold", supportsStack: true);
        var session = await StartStoppedSessionAsync(driver);
        var oldStack = Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 1)), false);
        Assert.Equal("call_stack_success", oldStack["kind"]?.GetValue<string>());
        var step = CallAsync(driver, tool, Arguments(session, 1));
        try
        {
            await RecordAsync(driver, "next-response-gated");
            Assert.False(step.IsCompleted);
            Assert.False((await StateAsync(driver, session)).ContainsKey("currentThreadId"));
            var before = (await driver.ReadNativeActionsAsync()).Count;
            FixedError(Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 1)), true),
                "dap_stack_trace_refused", "DAP_STACK_TRACE_REFUSED");
            Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);

            driver.ReleaseNextResponse();
            Success(Envelope(await step, false), tool, 1);
            Assert.Equal("call_stack_success", Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 1)), false)["kind"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Theory]
    [InlineData("step_into", "pre-ack-ignored-stops")]
    [InlineData("step_into", "post-ack-ignored-stops")]
    [InlineData("step_out", "pre-ack-ignored-stops")]
    [InlineData("step_out", "post-ack-ignored-stops")]
    public async Task StepPair_AckAloneAndPreAckMissingDifferentThreadOrNonStepStopsCannotCompleteTheStep(
        string tool, string mode)
    {
        await using var driver = await StartDriverAsync(mode);
        var session = await StartStoppedSessionAsync(driver);
        var operation = CallAsync(driver, tool, Arguments(session, 1));
        try
        {
            await RecordAsync(driver, "next-final-stop-gated");
            await ReaderBarrierAsync(driver, session);
            Assert.False(operation.IsCompleted,
                "The reader consumed the ACK and unrelated stops; only the held later exact step stop may complete the step.");

            driver.ReleaseNextResponse();
            Success(Envelope(await operation, false), tool, 1);
            Assert.Single(await driver.ReadNativeActionsAsync(), entry => entry.Command == Command(tool));
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Theory]
    [InlineData("step_into", "terminal-before-ack", "exited", 23)]
    [InlineData("step_into", "terminal-after-ack", "terminated", null)]
    [InlineData("step_out", "terminal-before-ack", "exited", 23)]
    [InlineData("step_out", "terminal-after-ack", "terminated", null)]
    public async Task StepPair_TerminalBeforeOrAfterAckReturnsOnlyObservedTerminalAndDrains(
        string tool, string mode, string terminal, int? exitCode)
    {
        await using var driver = await StartDriverAsync(mode);
        var session = await StartStoppedSessionAsync(driver);

        var content = Envelope(await CallAsync(driver, tool, Arguments(session, 1)), false);

        Assert.Equal($"{tool}_terminal", content["kind"]?.GetValue<string>());
        Assert.Equal(terminal, content["terminal"]?.GetValue<string>());
        Assert.Equal(exitCode.HasValue, content.ContainsKey("exitCode"));
        if (exitCode.HasValue) Assert.Equal(exitCode, content["exitCode"]?.GetValue<int>());
        Assert.Equal(exitCode.HasValue ? 3 : 2, content.Count);
        await AssertRetiredAsync(driver, session);
    }

    [Theory]
    [InlineData("step_into")]
    [InlineData("step_out")]
    public async Task StepPair_NegativeAckReturnsOwnRedactedRefusalAndRetainsLiveTransportWithoutOldAuthority(string tool)
    {
        await using var driver = await StartDriverAsync("negative", supportsStack: true);
        var session = await StartStoppedSessionAsync(driver);

        Refused(Envelope(await CallAsync(driver, tool, Arguments(session, 1)), true), tool);

        await ReaderBarrierAsync(driver, session);
        _ = await StateAsync(driver, session);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        FixedError(Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 1)), true),
            "dap_stack_trace_refused", "DAP_STACK_TRACE_REFUSED");
        Refused(Envelope(await CallAsync(driver, tool, Arguments(session, 1)), true), tool);
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static entry => entry.Command is "terminate" or "disconnect");
    }

    [Theory]
    [InlineData("step_into", "wrong-command")]
    [InlineData("step_into", "wrong-request-sequence")]
    [InlineData("step_into", "malformed-success")]
    [InlineData("step_into", "missing-response-sequence")]
    [InlineData("step_into", "missing-stop-sequence")]
    [InlineData("step_into", "reader-failure")]
    [InlineData("step_out", "wrong-command")]
    [InlineData("step_out", "wrong-request-sequence")]
    [InlineData("step_out", "malformed-success")]
    [InlineData("step_out", "missing-response-sequence")]
    [InlineData("step_out", "missing-stop-sequence")]
    [InlineData("step_out", "reader-failure")]
    public async Task StepPair_MalformedOrUncorrelatedExchangeReturnsOwnFixedProtocolErrorAndRetires(
        string tool, string mode)
    {
        await using var driver = await StartDriverAsync(mode);
        var session = await StartStoppedSessionAsync(driver);

        FixedError(Envelope(await CallAsync(driver, tool, Arguments(session, 1)), true),
            $"dap_{tool}_protocol_error", $"DAP_{tool.ToUpperInvariant()}_PROTOCOL_ERROR");

        await AssertRetiredAsync(driver, session);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        FixedError(Envelope(await CallAsync(driver, tool, Arguments(session, 1)), true),
            "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [MemberData(nameof(ExecutionConflicts))]
    public async Task StepPair_SingleFlightAndExecutionPauseExclusionRejectsWithoutIoEvenWithFreshStopAuthority(
        string activeTool, string incomingTool)
    {
        var mode = activeTool == "continue_execution" ? "continue-pre-ack-replaced" : "post-ack-ignored-stops";
        await using var driver = await StartDriverAsync(mode, pauseMode: "hold");
        var session = await StartStoppedSessionAsync(driver);
        var active = CallAsync(driver, activeTool, Arguments(session, 1));
        try
        {
            await RecordAsync(driver, activeTool switch
            {
                "pause_execution" => "pause-response-gated",
                "continue_execution" => "continue-pre-ack-gated",
                _ => "next-final-stop-gated",
            });
            await ReaderBarrierAsync(driver, session);
            Assert.Equal("stopped", (await StateAsync(driver, session))["event"]?.GetValue<string>());
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var incomingArguments = incomingTool == "pause_execution" ? Arguments(session) : Arguments(session, 1);

            Refused(Envelope(await CallAsync(driver, incomingTool, incomingArguments), true), incomingTool);

            Assert.False(active.IsCompleted, "A refused sibling must not release or settle the admitted execution record.");
            Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
            driver.ReleaseNextResponse();
            driver.ReleasePauseResponse();
            var completed = Envelope(await active, false);
            Assert.Equal(activeTool switch
            {
                "pause_execution" => "pause_acknowledged",
                "continue_execution" => "continue_stopped",
                _ => $"{activeTool}_success",
            }, completed["kind"]?.GetValue<string>());
            var executionWrites = (await driver.ReadNativeActionsAsync())
                .Where(static entry => entry.Command is "stepIn" or "stepOut" or "next" or "continue" or "pause").ToArray();
            ExactRequest(Assert.Single(executionWrites), Command(activeTool), 1);
        }
        finally
        {
            driver.ReleaseNextResponse();
            driver.ReleasePauseResponse();
        }
    }

    private static Task<ModernMcpProcessDriver> StartDriverAsync(
        string mode = "success", string lifecycleMode = "all-stop", bool supportsStack = false, string pauseMode = "ack-only") =>
        ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: lifecycleMode,
                SupportsDelayedStackTraceLoading: supportsStack, NextResponseMode: mode, PauseResponseMode: pauseMode)),
            startupTimeout: TimeSpan.FromSeconds(30));

    private static async Task<string> StartStoppedSessionAsync(ModernMcpProcessDriver driver)
    {
        var start = Envelope(await CallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath }), false);
        Assert.Equal("start_debug_success", start["kind"]?.GetValue<string>());
        var session = Assert.IsType<string>(start["debugSessionId"]?.GetValue<string>());
        await RecordAsync(driver, "stopped-event");
        await ReaderBarrierAsync(driver, session);
        return session;
    }

    private static Task<JsonRpcResponse> CallAsync(ModernMcpProcessDriver driver, string tool, JsonObject? arguments,
        CancellationToken cancellationToken = default, string? requestId = null) =>
        driver.CallToolRawAsync(tool, arguments, ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId ?? Guid.NewGuid().ToString("N")), cancellationToken, OperationTimeout);

    private static JsonObject Arguments(string session, int? threadId = null)
    {
        var arguments = new JsonObject { ["debugSessionId"] = session };
        if (threadId.HasValue) arguments["threadId"] = threadId.Value;
        return arguments;
    }

    private static async Task ReaderBarrierAsync(ModernMcpProcessDriver driver, string session) =>
        Assert.Equal("threads_success", Envelope(await CallAsync(driver, "get_threads", Arguments(session)), false)["kind"]?.GetValue<string>());

    private static async Task<JsonObject> StateAsync(ModernMcpProcessDriver driver, string session)
    {
        var result = Envelope(await CallAsync(driver, "get_debug_state", Arguments(session)), false);
        Assert.Equal("debug_state_success", result["kind"]?.GetValue<string>());
        return Assert.IsType<JsonObject>(result["state"]);
    }

    private static async Task RecordAsync(ModernMcpProcessDriver driver, string kind)
    {
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await driver.WaitForFixtureRecordAsync(kind, observation.Token);
    }

    private static async Task AssertRetiredAsync(ModernMcpProcessDriver driver, string session) =>
        FixedError(Envelope(await CallAsync(driver, "get_debug_state", Arguments(session)), true),
            "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");

    private static string Command(string tool) => tool switch
    {
        "step_into" => "stepIn",
        "step_out" => "stepOut",
        "step_over" => "next",
        "continue_execution" => "continue",
        "pause_execution" => "pause",
        _ => throw new ArgumentOutOfRangeException(nameof(tool)),
    };

    private static void ExactRequest(ModernNativeAction action, string command, int threadId)
    {
        Assert.Equal(command, action.Command);
        using var request = JsonDocument.Parse(Assert.IsType<string>(action.Detail));
        Assert.Equal(command, request.RootElement.GetProperty("command").GetString());
        var arguments = request.RootElement.GetProperty("arguments");
        Assert.Equal(new[] { "threadId" }, arguments.EnumerateObject().Select(static property => property.Name));
        Assert.Equal(threadId, arguments.GetProperty("threadId").GetInt32());
    }

    private static JsonObject Envelope(JsonRpcResponse response, bool isError)
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

    private static void Success(JsonObject content, string tool, int threadId)
    {
        Assert.Equal($"{tool}_success", content["kind"]?.GetValue<string>());
        Assert.Equal(threadId, content["threadId"]?.GetValue<int>());
        Assert.Equal(2, content.Count);
    }

    private static void Refused(JsonObject content, string tool)
    {
        var name = tool switch { "continue_execution" => "continue", "pause_execution" => "pause", _ => tool };
        FixedError(content, $"dap_{name}_refused", $"DAP_{name.ToUpperInvariant()}_REFUSED");
    }

    private static void FixedError(JsonObject content, string kind, string error)
    {
        Assert.Equal(kind, content["kind"]?.GetValue<string>());
        Assert.Equal(error, content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }
}
