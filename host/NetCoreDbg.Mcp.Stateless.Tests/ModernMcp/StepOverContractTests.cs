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

    [Theory]
    [InlineData(int.MinValue)]
    [InlineData(int.MaxValue)]
    public async Task StepOver_AdmittedNext_ClearsCurrentThreadIdBeforeHeldResponseAndPublishesActualNewStepThread(int nextThreadId)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(StepOver(nextResponseMode: "hold"));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"current-thread-step-{nextThreadId}-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureRecordAsync("stopped-event", observation.Token);
        var threads = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_threads",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"current-thread-step-{nextThreadId}-reader-barrier"),
            observation.Token), isError: false);
        Assert.Equal("threads_success", threads["kind"]?.GetValue<string>());
        var initialState = await ReadDebugStateAsync(driver, debugSessionId, $"current-thread-step-{nextThreadId}-initial");
        Assert.Equal("stopped", initialState["event"]?.GetValue<string>());
        Assert.Equal(1, initialState["currentThreadId"]?.GetValue<int>());

        var step = driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, nextThreadId),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"current-thread-step-{nextThreadId}"),
            timeout: TimeSpan.FromSeconds(35));
        try
        {
            await driver.WaitForNextRequestAsync(observation.Token);
            await driver.WaitForFixtureRecordAsync("next-response-gated", observation.Token);

            var heldState = await ReadDebugStateAsync(driver, debugSessionId, $"current-thread-step-{nextThreadId}-held");

            Assert.False(step.IsCompleted, "The next acknowledgement and step stop are still held by the fixture.");
            Assert.False(heldState.ContainsKey("currentThreadId"));
            driver.ReleaseNextResponse();
            var result = AssertApplicationEnvelope(await step, isError: false);
            Assert.Equal("step_over_success", result["kind"]?.GetValue<string>());
            Assert.Equal(nextThreadId, result["threadId"]?.GetValue<int>());

            var stoppedState = await ReadDebugStateAsync(driver, debugSessionId, $"current-thread-step-{nextThreadId}-stopped");

            Assert.Equal("stopped", stoppedState["event"]?.GetValue<string>());
            Assert.Equal("step", stoppedState["stopReason"]?.GetValue<string>());
            Assert.Equal(nextThreadId, stoppedState["currentThreadId"]?.GetValue<int>());
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
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
    [InlineData("terminal-before-ack", "exited", 23)]
    [InlineData("terminal-after-ack", "terminated", null)]
    [InlineData("terminal-exit-zero", "exited", 0)]
    [InlineData("terminal-exit-negative", "exited", -1)]
    public async Task StepOver_TerminalEvent_SettlesWithNormalizedTerminalOnly(string nextResponseMode, string terminal, int? expectedExitCode)
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
        Assert.Equal(expectedExitCode.HasValue, content.ContainsKey("exitCode"));
        if (expectedExitCode is { } exitCode)
        {
            Assert.Equal(exitCode, content["exitCode"]?.GetValue<int>());
        }
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
    [InlineData("exited-missing-sequence")]
    [InlineData("exited-wrong-type-sequence")]
    [InlineData("exited-zero-sequence")]
    [InlineData("exited-negative-sequence")]
    [InlineData("exited-overflow-sequence")]
    [InlineData("exited-underflow-sequence")]
    [InlineData("terminated-missing-sequence")]
    [InlineData("terminated-wrong-type-sequence")]
    [InlineData("terminated-zero-sequence")]
    [InlineData("terminated-negative-sequence")]
    [InlineData("terminated-overflow-sequence")]
    [InlineData("terminated-underflow-sequence")]
    [InlineData("exited-missing-exit-code")]
    [InlineData("exited-wrong-type-exit-code")]
    [InlineData("exited-overflow-exit-code")]
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

    [Fact]
    public async Task ToolsList_ContinueExecutionPublishesClosedInt32InputAfterEightDebugTools()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-catalog")));
        var tool = Assert.IsType<JsonObject>(Assert.IsType<JsonArray>(catalog["tools"])[8]);
        Assert.Equal("continue_execution", tool["name"]?.GetValue<string>());
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId", "threadId"], Assert.IsType<JsonArray>(schema["required"]).Select(static value => value?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(2, properties.Count);
        Assert.Equal("string", properties["debugSessionId"]?["type"]?.GetValue<string>());
        Assert.Equal(1, properties["debugSessionId"]?["minLength"]?.GetValue<int>());
        Assert.Equal("integer", properties["threadId"]?["type"]?.GetValue<string>());
        Assert.Equal(int.MinValue, properties["threadId"]?["minimum"]?.GetValue<int>());
        Assert.Equal(int.MaxValue, properties["threadId"]?["maximum"]?.GetValue<int>());
    }

    [Theory]
    [MemberData(nameof(InvalidArguments))]
    public async Task ContinueExecution_InvalidInput_ReturnsCompleteInvalidArgumentsWithoutDapIo(string scenario, JsonObject? arguments)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var before = await driver.ReadNativeActionsAsync();
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "continue_execution", arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"continue-invalid-{scenario}")), isError: true);
        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal("continue_execution", content["tool"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("continue-breakpoint-terminal", "breakpoint", true)]
    [InlineData("continue-breakpoint-terminal", "adapter-specific-stop", true)]
    [InlineData("continue-breakpoint-terminal", "", true)]
    [InlineData("continue-breakpoint-terminal", " \t ", true)]
    [InlineData("continue-optional-stop", "breakpoint", false)]
    public async Task ContinueExecution_BreakpointThenExit_PreservesObservedOptionalFieldsAndSchemaParity(string mode, string reason, bool hasStopFields)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ContinueOptions(mode, reason));
        var session = await StartStoppedSessionAsync(driver, $"{mode}-{reason}-start");
        var before = await driver.ReadNativeActionsAsync();
        var stopped = AssertContinueEnvelope(await driver.CallToolRawAsync(
            "continue_execution", Arguments(session, int.MinValue), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"{mode}-{reason}-stop")));
        Assert.Equal("continue_stopped", stopped["kind"]?.GetValue<string>());
        Assert.Equal(reason, stopped["reason"]?.GetValue<string>());
        Assert.Equal(hasStopFields, stopped.ContainsKey("threadId"));
        Assert.Equal(hasStopFields, stopped.ContainsKey("allThreadsStopped"));
        if (hasStopFields)
        {
            Assert.Equal(2, stopped["threadId"]?.GetValue<int>());
            Assert.False(stopped["allThreadsStopped"]!.GetValue<bool>());
            var state = await ReadDebugStateAsync(driver, session, $"{mode}-{reason}-state");
            Assert.Equal("stopped", state["event"]?.GetValue<string>());
            Assert.Equal(2, state["currentThreadId"]?.GetValue<int>());
        }
        var request = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "continue");
        Assert.True(JsonNode.DeepEquals(new JsonObject { ["threadId"] = int.MinValue }, JsonNode.Parse(request.Detail!)!["arguments"]));
        if (hasStopFields)
        {
            var terminal = AssertContinueEnvelope(await driver.CallToolRawAsync(
                "continue_execution", Arguments(session, 2), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"{mode}-{reason}-exit")));
            Assert.Equal("continue_terminal", terminal["kind"]?.GetValue<string>());
            Assert.Equal("exited", terminal["terminal"]?.GetValue<string>());
            Assert.Equal(0, terminal["exitCode"]?.GetValue<int>());
            await AssertEventuallyNotFoundAsync(driver, session, $"{mode}-{reason}-terminal-not-found");
        }
    }

    [Theory]
    [InlineData("continue-pre-ack", "breakpoint", 2, false)]
    [InlineData("continue-pre-ack-replaced", "adapter-specific-stop", 3, true)]
    public async Task ContinueExecution_PreAckStop_WaitsForAckAndUsesLatestCurrentStop(string mode, string reason, int threadId, bool allThreadsStopped)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ContinueOptions(mode));
        var session = await StartStoppedSessionAsync(driver, $"{mode}-start");
        var operation = driver.CallToolRawAsync("continue_execution", Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId(mode));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        try
        {
            await driver.WaitForFixtureRecordAsync("continue-pre-ack-gated", observation.Token);
            await ContinueReaderBarrierAsync(driver, session, $"{mode}-before-ack", observation.Token);
            Assert.False(operation.IsCompleted, "A reader-observed stop cannot settle continue before its positive acknowledgement.");
            driver.ReleaseNextResponse();
            var content = AssertContinueEnvelope(await operation);
            Assert.Equal("continue_stopped", content["kind"]?.GetValue<string>());
            Assert.Equal(reason, content["reason"]?.GetValue<string>());
            Assert.Equal(threadId, content["threadId"]?.GetValue<int>());
            Assert.Equal(allThreadsStopped, content["allThreadsStopped"]?.GetValue<bool>());
            var state = await ReadDebugStateAsync(driver, session, $"{mode}-after-ack");
            Assert.Equal("stopped", state["event"]?.GetValue<string>());
            Assert.Equal(threadId, state["currentThreadId"]?.GetValue<int>());
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task ContinueExecution_PreAckStopThenContinued_DoesNotResurrectCandidate()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ContinueOptions("continue-pre-ack-continued"));
        var session = await StartStoppedSessionAsync(driver, "continue-stale-candidate-start");
        var operation = driver.CallToolRawAsync("continue_execution", Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-stale-candidate"));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        try
        {
            await driver.WaitForFixtureRecordAsync("continue-pre-ack-gated", observation.Token);
            await ContinueReaderBarrierAsync(driver, session, "continue-stale-before-ack", observation.Token);
            Assert.False(operation.IsCompleted);
            driver.ReleaseNextResponse();
            await driver.WaitForFixtureRecordAsync("continue-later-stop-gated", observation.Token);
            await ContinueReaderBarrierAsync(driver, session, "continue-stale-after-ack", observation.Token);
            Assert.False(operation.IsCompleted, "An intervening continued event invalidates the remembered pre-ack stop.");
            driver.ReleaseThreadsResponse();
            var content = AssertContinueEnvelope(await operation);
            Assert.Equal("continue_stopped", content["kind"]?.GetValue<string>());
            Assert.Equal(1, content["threadId"]?.GetValue<int>());
        }
        finally
        {
            driver.ReleaseNextResponse();
            driver.ReleaseThreadsResponse();
        }
    }

    [Theory]
    [InlineData("continue_execution", "step_over", "continue-hold", "continue-ack-gated", "dap_step_over_refused")]
    [InlineData("continue_execution", "continue_execution", "continue-hold", "continue-ack-gated", "dap_continue_refused")]
    [InlineData("step_over", "continue_execution", "hold", "next-response-gated", "dap_continue_refused")]
    public async Task ContinueExecution_CompetingExecutionAndOldInspection_AreRefusedWithoutContinuedEvent(string firstTool, string secondTool, string mode, string gate, string refusal)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ContinueOptions(mode));
        var session = await StartStoppedSessionAsync(driver, $"continue-single-flight-{firstTool}-{secondTool}-start");
        var stack = await ContinueInspectionStackAsync(driver, session, "continue-old-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var frame = Assert.IsType<JsonArray>(stack["frames"])[0]!["id"]!.GetValue<int>();
        var scopes = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_scopes",
            new JsonObject { ["debugSessionId"] = session, ["inspectionContextId"] = context, ["frameId"] = frame },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-old-scopes")), isError: false);
        var reference = Assert.IsType<JsonArray>(scopes["scopes"])[0]!["variablesReference"]!.GetValue<int>();
        var first = driver.CallToolRawAsync(firstTool, Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-first"));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        try
        {
            await driver.WaitForFixtureRecordAsync(gate, observation.Token);
            var before = await driver.ReadNativeActionsAsync();
            var second = AssertApplicationEnvelope(await driver.CallToolRawAsync(secondTool, Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-competing")), isError: true);
            Assert.Equal(refusal, second["kind"]?.GetValue<string>());
            Assert.Equal(2, second.Count);
            var refusedStack = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_call_stack", Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-old-stack-refused")), isError: true);
            Assert.Equal("dap_stack_trace_refused", refusedStack["kind"]?.GetValue<string>());
            foreach (var (tool, field, id, kind) in new[]
            {
                ("get_scopes", "frameId", frame, "dap_scopes_refused"),
                ("get_variables", "variablesReference", reference, "dap_variables_refused"),
            })
            {
                var stale = AssertApplicationEnvelope(await driver.CallToolRawAsync(tool,
                    new JsonObject { ["debugSessionId"] = session, ["inspectionContextId"] = context, [field] = id },
                    ModernMcpProcessDriver.CurrentMeta(), new RequestId($"continue-stale-{tool}")), isError: true);
                Assert.Equal(kind, stale["kind"]?.GetValue<string>());
            }
            Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
            driver.ReleaseNextResponse();
            var completed = AssertApplicationEnvelope(await first, isError: false);
            Assert.Equal(firstTool == "step_over" ? "step_over_success" : "continue_stopped", completed["kind"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task ContinueExecution_OrdinaryTimeout_RetainsOnlyAcknowledgedSessionAndAllowsLaterInspection(bool acknowledged)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ContinueOptions(acknowledged ? "continue-ack-held-stop" : "continue-hold"));
        var session = await StartStoppedSessionAsync(driver, $"continue-timeout-{acknowledged}-start");
        var operation = driver.CallToolRawAsync("continue_execution", Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"continue-timeout-{acknowledged}"), timeout: TimeSpan.FromSeconds(35));
        using (var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2)))
        {
            await driver.WaitForFixtureRecordAsync(acknowledged ? "continue-acknowledged" : "continue-ack-gated", observation.Token);
            await ContinueReaderBarrierAsync(driver, session, $"continue-timeout-{acknowledged}-barrier", observation.Token);
            if (acknowledged)
            {
                var running = await ReadDebugStateAsync(driver, session, "continue-ack-no-continued-state");
                Assert.Contains(running["event"]?.GetValue<string>(), new[] { "continued", "running" });
                Assert.False(running.ContainsKey("currentThreadId"));
            }
        }
        var content = AssertContinueEnvelope(await operation);
        Assert.Equal("continue_timed_out", content["kind"]?.GetValue<string>());
        Assert.Equal(1, content["threadId"]?.GetValue<int>());
        Assert.Equal(30, content["timeoutSeconds"]?.GetValue<int>());
        Assert.Equal(acknowledged, content["acknowledged"]?.GetValue<bool>());
        if (!acknowledged)
        {
            await AssertEventuallyNotFoundAsync(driver, session, "continue-unacknowledged-timeout-state");
            return;
        }
        _ = await ReadDebugStateAsync(driver, session, "continue-acknowledged-timeout-retained");
        Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command is "terminate" or "disconnect");
        driver.ReleaseNextResponse();
        using var later = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureRecordAsync("continue-later-stop", later.Token);
        await ContinueReaderBarrierAsync(driver, session, "continue-timeout-later-reader-barrier", later.Token);
        var stack = await ContinueInspectionStackAsync(driver, session, "continue-timeout-later-stack");
        Assert.Equal("call_stack_success", stack["kind"]?.GetValue<string>());
        Assert.False(string.IsNullOrWhiteSpace(stack["inspectionContextId"]?.GetValue<string>()));
        Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command is "terminate" or "disconnect");
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task ContinueExecution_PublicCallerCancellation_RetainsAcknowledgedSessionButDrainsUnacknowledgedWrite(bool acknowledged)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ContinueOptions(acknowledged ? "continue-ack-held-stop" : "continue-hold"));
        var session = await StartStoppedSessionAsync(driver, $"continue-cancellation-{acknowledged}-start");
        using var cancellation = new CancellationTokenSource();
        var operation = driver.CallToolRawAsync("continue_execution", Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"continue-cancellation-{acknowledged}"), cancellation.Token, timeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync(acknowledged ? "continue-acknowledged" : "continue-ack-gated", observation.Token);
            await ContinueReaderBarrierAsync(driver, session, "continue-cancellation-before-reader-barrier", observation.Token);
            Assert.False(operation.IsCompleted);
            cancellation.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
            if (!acknowledged)
            {
                await driver.WaitForFixtureRecordAsync("disconnect-response", observation.Token);
                await AssertEventuallyNotFoundAsync(driver, session, "continue-cancellation-unacknowledged-state");
                return;
            }
            _ = await ReadDebugStateAsync(driver, session, "continue-cancellation-retained-state");
            driver.ReleaseNextResponse();
            await driver.WaitForFixtureRecordAsync("continue-later-stop", observation.Token);
            await ContinueReaderBarrierAsync(driver, session, "continue-cancellation-later-reader-barrier", observation.Token);
            var stack = await ContinueInspectionStackAsync(driver, session, "continue-cancellation-later-stack");
            Assert.Equal("call_stack_success", stack["kind"]?.GetValue<string>());
            var context = stack["inspectionContextId"]!.GetValue<string>();
            var frame = Assert.IsType<JsonArray>(stack["frames"])[0]!["id"]!.GetValue<int>();
            var scopes = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_scopes",
                new JsonObject { ["debugSessionId"] = session, ["inspectionContextId"] = context, ["frameId"] = frame },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-cancellation-later-scopes")), isError: false);
            Assert.Equal("scopes_success", scopes["kind"]?.GetValue<string>());
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command is "terminate" or "disconnect");
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task ContinueExecution_ExplicitStopAfterAcknowledgement_CompletesCleanupAndRetiresTheInterruptedCall()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(
                SpawnDescendant: true,
                SupportsDelayedStackTraceLoading: true,
                LifecycleMode: "all-stop",
                NextResponseMode: "continue-ack-held-stop")));
        var session = await StartStoppedSessionAsync(driver, "continue-explicit-stop-start");
        using var debuggee = System.Diagnostics.Process.GetProcessById(await driver.ReadDescendantProcessIdAsync());
        Assert.False(debuggee.HasExited);
        using (var setup = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
        {
            await ContinueReaderBarrierAsync(driver, session, "continue-explicit-stop-initial-reader-barrier", setup.Token);
        }
        var operation = driver.CallToolRawAsync(
            "continue_execution", Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("continue-explicit-stop-inflight"), timeout: TimeSpan.FromSeconds(15));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("continue-acknowledged", observation.Token);
            await ContinueReaderBarrierAsync(driver, session, "continue-explicit-stop-reader-barrier", observation.Token);
            var running = await ReadDebugStateAsync(driver, session, "continue-explicit-stop-running");
            Assert.Contains(running["event"]?.GetValue<string>(), new[] { "continued", "running" });
            Assert.False(running.ContainsKey("currentThreadId"));
            Assert.False(operation.IsCompleted, "The acknowledged continue must still await its held stop event.");
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command is "terminate" or "disconnect");

            var stop = driver.CallToolRawAsync(
                "stop_debug", new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(),
                new RequestId("continue-explicit-stop"), timeout: TimeSpan.FromSeconds(15));
            var stopResponse = await stop.WaitAsync(TimeSpan.FromSeconds(10));

            debuggee.Refresh();
            Assert.True(debuggee.HasExited, "stop_debug must finish owned process-tree cleanup before returning success.");
            var continueResponse = await operation.WaitAsync(TimeSpan.FromSeconds(5));
            var stopped = AssertApplicationEnvelope(stopResponse, isError: false);
            Assert.Equal("stop_debug_success", stopped["kind"]?.GetValue<string>());
            AssertNotFound(AssertContinueEnvelope(continueResponse, isError: true));

            var afterCleanup = await driver.ReadNativeActionsAsync();
            Assert.Single(afterCleanup, static action => action.Command == "continue");
            AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
                "get_debug_state", new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(),
                new RequestId("continue-explicit-stop-retired-state")), isError: true));
            AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
                "stop_debug", new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(),
                new RequestId("continue-explicit-stop-retired-stop")), isError: true));
            Assert.Equal(afterCleanup, await driver.ReadNativeActionsAsync());
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Theory]
    [InlineData("continue-negative", "dap_continue_refused", "DAP_CONTINUE_REFUSED", false)]
    [InlineData("continue-wrong-command", "dap_continue_protocol_error", "DAP_CONTINUE_PROTOCOL_ERROR", true)]
    [InlineData("continue-eof", "dap_continue_protocol_error", "DAP_CONTINUE_PROTOCOL_ERROR", true)]
    [InlineData("continue-missing-reason", "dap_continue_protocol_error", "DAP_CONTINUE_PROTOCOL_ERROR", true)]
    public async Task ContinueExecution_NegativeOrUncertainExchange_ReturnsRedactedErrorAndCorrectSessionDisposition(string mode, string kind, string error, bool drains)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ContinueOptions(mode));
        var session = await StartStoppedSessionAsync(driver, $"{mode}-start");
        var content = AssertContinueEnvelope(await driver.CallToolRawAsync("continue_execution", Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(mode)), isError: true);
        Assert.Equal(kind, content["kind"]?.GetValue<string>());
        Assert.Equal(error, content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        if (drains)
        {
            await AssertEventuallyNotFoundAsync(driver, session, $"{mode}-not-found");
        }
        else
        {
            _ = await ReadDebugStateAsync(driver, session, "continue-negative-retained");
            var before = await driver.ReadNativeActionsAsync();
            var stack = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_call_stack", Arguments(session, 1),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-negative-old-stack")), isError: true);
            Assert.Equal("dap_stack_trace_refused", stack["kind"]?.GetValue<string>());
            Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        }
    }

    [Fact]
    public async Task ContinueExecution_TerminalBeforeAck_SettlesWithoutInventedExitCode()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ContinueOptions("continue-terminal-before-ack"));
        var session = await StartStoppedSessionAsync(driver, "continue-terminal-start");
        var content = AssertContinueEnvelope(await driver.CallToolRawAsync("continue_execution", Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("continue-terminal-before-ack")));
        Assert.Equal("continue_terminal", content["kind"]?.GetValue<string>());
        Assert.Equal("terminated", content["terminal"]?.GetValue<string>());
        Assert.False(content.ContainsKey("exitCode"));
        await AssertEventuallyNotFoundAsync(driver, session, "continue-terminal-before-ack-not-found");
    }

    private static ModernMcpStartOptions ContinueOptions(string mode, string reason = "breakpoint") => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(SupportsDelayedStackTraceLoading: true,
            LifecycleMode: "all-stop", NextResponseMode: mode, StopReason: reason));

    private static async Task ContinueReaderBarrierAsync(ModernMcpProcessDriver driver, string session, string requestId, CancellationToken cancellationToken)
    {
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId), cancellationToken), isError: false);
        Assert.Equal("threads_success", content["kind"]?.GetValue<string>());
    }

    private static async Task<JsonObject> ContinueInspectionStackAsync(ModernMcpProcessDriver driver, string session, string requestId)
    {
        var arguments = Arguments(session, 1);
        arguments["includeInspectionContext"] = true;
        return AssertApplicationEnvelope(await driver.CallToolRawAsync("get_call_stack", arguments,
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: false);
    }

    private static JsonObject AssertContinueEnvelope(JsonRpcResponse response, bool isError = false)
    {
        var content = AssertApplicationEnvelope(response, isError);
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        return content;
    }

    private static async Task<JsonObject> ReadDebugStateAsync(ModernMcpProcessDriver driver, string debugSessionId, string requestId)
    {
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId)), isError: false);
        Assert.Equal("debug_state_success", content["kind"]?.GetValue<string>());
        Assert.Equal(debugSessionId, content["debugSessionId"]?.GetValue<string>());
        return Assert.IsType<JsonObject>(content["state"]);
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