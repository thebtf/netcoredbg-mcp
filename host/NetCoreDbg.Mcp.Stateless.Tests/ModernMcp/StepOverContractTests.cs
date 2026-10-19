using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class StepOverContractTests
{
    private const string Tool = "step_over";

    public static TheoryData<string, JsonObject?> InvalidArguments => new()
    {
        { "missing_arguments", null },
        { "omitted_debug_session_id", new JsonObject { ["threadId"] = 1 } },
        { "omitted_thread_id", new JsonObject { ["debugSessionId"] = "opaque" } },
        { "empty_token", new JsonObject { ["debugSessionId"] = string.Empty, ["threadId"] = 1 } },
        { "whitespace_token", new JsonObject { ["debugSessionId"] = " \t", ["threadId"] = 1 } },
        { "non_string_token", new JsonObject { ["debugSessionId"] = 1, ["threadId"] = 1 } },
        { "non_integral_thread", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1.5 } },
        { "underflow_thread", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = -2_147_483_649L } },
        { "overflow_thread", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 2_147_483_648L } },
        { "single_thread_is_not_input", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["singleThread"] = true } },
        { "granularity_is_not_input", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["granularity"] = "line" } },
        { "extra_field", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["unexpected"] = true } },
    };

    [Fact]
    public async Task ToolsList_StepOverPublishesClosedInt32InputAfterGetCallStack()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var result = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-catalog")));
        var tools = Assert.IsType<JsonArray>(result["tools"]);
        Assert.Equal("get_call_stack", tools[4]?["name"]?.GetValue<string>());
        Assert.Equal(Tool, tools[5]?["name"]?.GetValue<string>());
        var tool = Assert.Single(
            tools.Select(static candidate => Assert.IsType<JsonObject>(candidate)),
            candidate => candidate["name"]?.GetValue<string>() == Tool);
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId", "threadId"], Assert.IsType<JsonArray>(schema["required"]).Select(static value => value?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal("string", properties["debugSessionId"]?["type"]?.GetValue<string>());
        Assert.Equal("integer", properties["threadId"]?["type"]?.GetValue<string>());
        Assert.Equal(int.MinValue, properties["threadId"]?["minimum"]?.GetValue<int>());
        Assert.Equal(int.MaxValue, properties["threadId"]?["maximum"]?.GetValue<int>());
    }

    [Theory]
    [MemberData(nameof(InvalidArguments))]
    public async Task StepOver_InvalidInput_ReturnsCompleteInvalidArgumentsWithoutDapIo(string scenario, JsonObject? arguments)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"step-over-invalid-{scenario}")), isError: true);

        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(Tool, content["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("short")]
    [InlineData("!")]
    [InlineData("not-a-capability")]
    public async Task StepOver_UnavailableOpaqueToken_ReturnsNotFoundWithoutDapIo(string token)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(token, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"step-over-unavailable-{token}")), isError: true);

        AssertNotFound(content);
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task StepOver_AllStoppedAuthorityWithoutDelayedStackCapability_ForwardsOnlyExactInt32NextAndSucceeds()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver());
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-all-stop-start");
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, int.MinValue),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-all-stop")), isError: false);

        Assert.Equal("step_over_success", content["kind"]?.GetValue<string>());
        Assert.Equal(int.MinValue, content["threadId"]?.GetValue<int>());
        var next = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "next");
        using var request = JsonDocument.Parse(next.Detail ?? throw new Xunit.Sdk.XunitException("Controlled adapter did not record next arguments."));
        var arguments = request.RootElement.GetProperty("arguments");
        Assert.Equal(["threadId"], arguments.EnumerateObject().Select(static property => property.Name));
        Assert.Equal(int.MinValue, arguments.GetProperty("threadId").GetInt32());
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-all-stop-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task StepOver_PartialStoppedAuthority_AdmitsOnlyTheNamedTarget()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(lifecycleMode: "partial-stop"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-partial-start");
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-partial")), isError: false);

        Assert.Equal("step_over_success", content["kind"]?.GetValue<string>());
        Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "next");
    }

    [Theory]
    [InlineData("partial-stop", 2)]
    [InlineData("partial-stop-missing-thread", 1)]
    public async Task StepOver_AbsentStoppedAuthority_ReturnsRedactedRefusalWithoutNextWrite(string lifecycleMode, int threadId)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(lifecycleMode: lifecycleMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"step-over-no-authority-{lifecycleMode}");
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, threadId),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"step-over-no-authority-{lifecycleMode}")), isError: true);

        AssertRefused(content);
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "next");
    }

    [Fact]
    public async Task StepOver_ConcurrentCallDuringHeldActiveRecord_RefusesWithoutSecondNextWrite()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(nextResponseMode: "hold"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-single-flight-start");
        var first = driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-single-flight-first"),
            timeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForNextRequestAsync(observation.Token);
        var beforeSecond = await driver.ReadNativeActionsAsync();

        var second = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-single-flight-second")), isError: true);

        AssertRefused(second);
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeSecond.Count), static action => action.Command == "next");
        driver.ReleaseNextResponse();
        Assert.Equal("step_over_success", AssertApplicationEnvelope(await first, isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task StepOver_NegativeAcknowledgement_ReturnsRedactedRefusalAndPreservesSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(nextResponseMode: "negative"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-negative-start");

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-negative")), isError: true);

        AssertRefused(content);
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-negative-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Theory]
    [InlineData("pre-ack-ignored-stops")]
    [InlineData("post-ack-ignored-stops")]
    public async Task StepOver_UnrelatedOrUncorrelatedStops_DoNotSettleBeforeLaterExactStepStop(string nextResponseMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(nextResponseMode: nextResponseMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"step-over-{nextResponseMode}-start");

        var operation = driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"step-over-{nextResponseMode}"));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        try
        {
            await driver.WaitForFixtureRecordAsync("next-final-stop-gated", observation.Token);
            var threads = AssertApplicationEnvelope(await driver.CallToolRawAsync(
                "get_threads",
                new JsonObject { ["debugSessionId"] = debugSessionId },
                ModernMcpProcessDriver.CurrentMeta(),
                new RequestId($"step-over-{nextResponseMode}-reader-barrier"),
                observation.Token), isError: false);
            Assert.Equal("threads_success", threads["kind"]?.GetValue<string>());
            Assert.False(operation.IsCompleted,
                "The acknowledgement and all preceding stopped events have been read, but only the held later exact step stop may complete this operation.");

            driver.ReleaseNextResponse();
            var content = AssertApplicationEnvelope(await operation, isError: false);
            Assert.Equal("step_over_success", content["kind"]?.GetValue<string>());
            Assert.Equal(1, content["threadId"]?.GetValue<int>());
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Theory]
    [InlineData("terminal-before-ack", "exited", true)]
    [InlineData("terminal-after-ack", "terminated", false)]
    public async Task StepOver_TerminalEvent_SettlesWithNormalizedTerminalOnly(string nextResponseMode, string terminal, bool hasExitCode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(nextResponseMode: nextResponseMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"step-over-{nextResponseMode}-start");

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"step-over-{nextResponseMode}")), isError: false);

        Assert.Equal("step_over_terminal", content["kind"]?.GetValue<string>());
        Assert.Equal(terminal, content["terminal"]?.GetValue<string>());
        Assert.Equal(hasExitCode, content.ContainsKey("exitCode"));
        Assert.DoesNotContain("body", content.Select(static property => property.Key));
        Assert.DoesNotContain("message", content.Select(static property => property.Key));
    }

    [Theory]
    [InlineData("wrong-command")]
    [InlineData("wrong-request-sequence")]
    [InlineData("malformed-success")]
    [InlineData("reader-failure")]
    [InlineData("missing-response-sequence")]
    [InlineData("wrong-type-response-sequence")]
    [InlineData("zero-response-sequence")]
    [InlineData("negative-response-sequence")]
    [InlineData("overflow-response-sequence")]
    [InlineData("underflow-response-sequence")]
    [InlineData("missing-stop-sequence")]
    [InlineData("wrong-type-stop-sequence")]
    [InlineData("zero-stop-sequence")]
    [InlineData("negative-stop-sequence")]
    [InlineData("overflow-stop-sequence")]
    [InlineData("underflow-stop-sequence")]
    public async Task StepOver_MalformedOrWronglyCorrelatedActiveExchange_ReturnsRedactedProtocolErrorAndEvicts(string nextResponseMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(nextResponseMode: nextResponseMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"step-over-{nextResponseMode}-start");

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"step-over-{nextResponseMode}"),
            timeout: TimeSpan.FromSeconds(35)), isError: true);

        AssertProtocolError(content);
        await AssertEventuallyNotFoundAsync(driver, debugSessionId, $"step-over-{nextResponseMode}-state");
    }

    [Fact]
    public async Task StepOver_CollidingPendingRequestSequence_ReturnsRedactedProtocolErrorAndDrains()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(
            supportsDelayedStackTraceLoading: true,
            stackTraceResponseMode: "hold",
            nextResponseMode: "wrong-request-sequence-collides-with-stack-trace"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-collision-start");
        var stackTrace = driver.CallToolRawAsync(
            "get_call_stack",
            new JsonObject { ["debugSessionId"] = debugSessionId, ["threadId"] = 1 },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-collision-stack-trace"),
            timeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForStackTraceRequestAsync(observation.Token);
        await driver.WaitForFixtureRecordAsync("stack-trace-response-gated", observation.Token);

        var stepOver = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-collision"),
            timeout: TimeSpan.FromSeconds(35)), isError: true);

        AssertProtocolError(stepOver);
        await AssertEventuallyNotFoundAsync(driver, debugSessionId, "step-over-collision-state");
        driver.ReleaseStackTraceResponse();
        _ = await stackTrace;
    }

    [Fact]
    public async Task StepOver_AdmittedNextInvalidatesB2CallStackAuthorityBeforeHeldResponse()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(
            nextResponseMode: "hold",
            supportsDelayedStackTraceLoading: true));
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-b2-invalidation-start");
        var step = driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-b2-invalidation"),
            timeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForNextRequestAsync(observation.Token);
        var beforeCallStack = await driver.ReadNativeActionsAsync();

        var callStack = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_call_stack",
            new JsonObject { ["debugSessionId"] = debugSessionId, ["threadId"] = 1 },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-b2-invalidation-call-stack")), isError: true);

        Assert.Equal("dap_stack_trace_refused", callStack["kind"]?.GetValue<string>());
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeCallStack.Count), static action => action.Command == "stackTrace");
        driver.ReleaseNextResponse();
        Assert.Equal("step_over_success", AssertApplicationEnvelope(await step, isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task StepOver_ContinuedDuringHeldResponse_DoesNotRestoreB2CallStackAuthority()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(
            nextResponseMode: "hold-then-continued",
            supportsDelayedStackTraceLoading: true));
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-continued-start");
        var step = driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-continued"),
            timeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync("continued", observation.Token);
        var beforeCallStack = await driver.ReadNativeActionsAsync();

        var callStack = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_call_stack",
            new JsonObject { ["debugSessionId"] = debugSessionId, ["threadId"] = 1 },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-continued-call-stack")), isError: true);

        Assert.Equal("dap_stack_trace_refused", callStack["kind"]?.GetValue<string>());
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeCallStack.Count), static action => action.Command == "stackTrace");
        driver.ReleaseNextResponse();
        Assert.Equal("step_over_success", AssertApplicationEnvelope(await step, isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task StepOver_PostWriteCallerCancellation_DrainsSessionBeforeLaterAccess()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(nextResponseMode: "hold"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-cancel-start");
        using var cancellation = new CancellationTokenSource();
        var operation = driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-cancel"),
            cancellation.Token,
            timeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForNextRequestAsync(observation.Token);
        var barrier = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_threads",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-cancel-reader-barrier"),
            observation.Token), isError: false);
        Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
        Assert.False(operation.IsCompleted, "The committed next must still be waiting for its held acknowledgement before cancellation.");
        cancellation.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
        await driver.WaitForFixtureRecordAsync("disconnect-response", observation.Token);
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-cancel-state"),
            observation.Token), isError: true));
        var actions = await driver.ReadNativeActionsAsync();
        Assert.Single(actions, static action => action.Command == "terminate");
        Assert.Single(actions, static action => action.Command == "disconnect");
    }

    [Fact]
    public async Task StepOver_PostWriteTimeout_ReturnsBoundedResultThenDrainsSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(nextResponseMode: "timeout"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "step-over-timeout-start");

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-timeout"),
            timeout: TimeSpan.FromSeconds(35)), isError: false);

        Assert.Equal("step_over_timed_out", content["kind"]?.GetValue<string>());
        Assert.Equal(1, content["threadId"]?.GetValue<int>());
        Assert.Equal(30, content["timeoutSeconds"]?.GetValue<int>());
        await AssertEventuallyNotFoundAsync(driver, debugSessionId, "step-over-timeout-state");
    }

    private static ModernMcpStartOptions StepOver(
        string nextResponseMode = "success",
        bool supportsDelayedStackTraceLoading = false,
        string lifecycleMode = "all-stop",
        string stackTraceResponseMode = "success") => new(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(
                SupportsDelayedStackTraceLoading: supportsDelayedStackTraceLoading,
                LifecycleMode: lifecycleMode,
                StackTraceResponseMode: stackTraceResponseMode,
                NextResponseMode: nextResponseMode));

    private static JsonObject Arguments(string debugSessionId, int threadId) => new()
    {
        ["debugSessionId"] = debugSessionId,
        ["threadId"] = threadId,
    };

    private static async Task<string> StartStoppedSessionAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var start = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId)), isError: false);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        return Assert.IsType<string>(start["debugSessionId"]?.GetValue<string>());
    }

    private static async Task AssertEventuallyNotFoundAsync(ModernMcpProcessDriver driver, string debugSessionId, string requestId)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        while (true)
        {
            var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
                "get_debug_state",
                new JsonObject { ["debugSessionId"] = debugSessionId },
                ModernMcpProcessDriver.CurrentMeta(),
                new RequestId(requestId),
                deadline.Token), isError: true);
            if (content["kind"]?.GetValue<string>() == "debug_session_not_found")
            {
                return;
            }

            await Task.Delay(TimeSpan.FromMilliseconds(25), deadline.Token);
        }
    }

    private static JsonObject AssertApplicationEnvelope(JsonRpcResponse response, bool isError)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() == true);
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", text["type"]?.GetValue<string>());
        Assert.Equal(content.ToJsonString(), text["text"]?.GetValue<string>());
        return content;
    }

    private static void AssertNotFound(JsonObject content)
    {
        Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static void AssertRefused(JsonObject content)
    {
        Assert.Equal("dap_step_over_refused", content["kind"]?.GetValue<string>());
        Assert.Equal("DAP_STEP_OVER_REFUSED", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.False(content.ContainsKey("body"));
        Assert.False(content.ContainsKey("message"));
    }

    private static void AssertProtocolError(JsonObject content)
    {
        Assert.Equal("dap_step_over_protocol_error", content["kind"]?.GetValue<string>());
        Assert.Equal("DAP_STEP_OVER_PROTOCOL_ERROR", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.False(content.ContainsKey("body"));
        Assert.False(content.ContainsKey("message"));
    }
}