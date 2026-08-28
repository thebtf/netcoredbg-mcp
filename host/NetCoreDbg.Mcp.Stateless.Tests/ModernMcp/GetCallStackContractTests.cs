using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class GetCallStackContractTests
{
    private const string Tool = "get_call_stack";
    private const long MaximumSafeDapInteger = 9_007_199_254_740_991L;

    public static TheoryData<string, JsonObject?> InvalidArguments => new()
    {
        { "missing_arguments", null },
        { "omitted_debug_session_id", new JsonObject { ["threadId"] = 1 } },
        { "omitted_thread_id", new JsonObject { ["debugSessionId"] = "opaque" } },
        { "empty_token", new JsonObject { ["debugSessionId"] = string.Empty, ["threadId"] = 1 } },
        { "non_string_token", new JsonObject { ["debugSessionId"] = 17, ["threadId"] = 1 } },
        { "whitespace_token", new JsonObject { ["debugSessionId"] = " \t", ["threadId"] = 1 } },
        { "non_integral_thread", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1.5 } },
        { "out_of_range_thread", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 2_147_483_648L } },
        { "negative_start", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["startFrame"] = -1 } },
        { "non_integral_start", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["startFrame"] = 1.5 } },
        { "out_of_range_start", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["startFrame"] = 4_294_967_296L } },
        { "zero_levels", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["levels"] = 0 } },
        { "non_integral_levels", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["levels"] = 1.5 } },
        { "out_of_range_levels", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["levels"] = 257 } },
        { "extra_field", new JsonObject { ["debugSessionId"] = "opaque", ["threadId"] = 1, ["unexpected"] = true } },
    };

    [Fact]
    public async Task ToolsList_GetCallStackPublishesClosedExplicitBoundedInputAfterGetThreads()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var result = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("call-stack-catalog")));
        var tools = Assert.IsType<JsonArray>(result["tools"]);
        Assert.Equal("get_threads", tools[3]?["name"]?.GetValue<string>());
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
        Assert.Equal(0, properties["startFrame"]?["minimum"]?.GetValue<int>());
        Assert.Equal(1, properties["levels"]?["minimum"]?.GetValue<int>());
        Assert.Equal(256, properties["levels"]?["maximum"]?.GetValue<int>());
    }

    [Theory]
    [MemberData(nameof(InvalidArguments))]
    public async Task GetCallStack_InvalidInput_ReturnsCompleteInvalidArgumentsWithoutDapIo(string scenario, JsonObject? arguments)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync(Tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-invalid-{scenario}"));
        var content = AssertApplicationEnvelope(response, isError: true);
        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(Tool, content["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("short")]
    [InlineData("!")]
    [InlineData("not-a-capability")]
    public async Task GetCallStack_UnavailableOpaqueToken_ReturnsCompleteNotFoundWithoutDapIo(string token)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync(Tool, Arguments(token, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-unavailable-{token}"));
        AssertNotFound(AssertApplicationEnvelope(response, isError: true));
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task GetCallStack_CapabilityOmittedOrFalse_ReturnsRedactedRefusalWithoutStackTraceWrite(bool explicitFalse)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(supportsDelayedStackTraceLoading: false, omitDelayedStackTraceLoading: !explicitFalse));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-capability-{explicitFalse}");
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-capability-{explicitFalse}-call"));
        AssertRefused(AssertApplicationEnvelope(response, isError: true));
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-capability-{explicitFalse}-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetCallStack_AllStoppedTarget_UsesDefaultBoundedPageAndNormalizesFrames()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack("empty-name-source-reference"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "call-stack-defaults-start");
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 17), ModernMcpProcessDriver.CurrentMeta(), new RequestId("call-stack-defaults"));
        var content = AssertApplicationEnvelope(response, isError: false);
        Assert.Equal("call_stack_success", content["kind"]?.GetValue<string>());
        Assert.Equal(2, content["totalFrames"]?.GetValue<int>());
        var frames = Assert.IsType<JsonArray>(content["frames"]);
        AssertFrame(frames[0], 1, string.Empty, null, 0, 0);
        AssertFrame(frames[1], 2, "source-reference-only", null, 0, 0);
        Assert.All(frames, static frame => Assert.False(Assert.IsType<JsonObject>(frame).ContainsKey("sourceReference")));
        var stackTrace = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
        using var request = JsonDocument.Parse(stackTrace.Detail ?? throw new Xunit.Sdk.XunitException("Controlled adapter did not record stackTrace arguments."));
        var arguments = request.RootElement.GetProperty("arguments");
        Assert.Equal(17, arguments.GetProperty("threadId").GetInt32());
        Assert.Equal(0, arguments.GetProperty("startFrame").GetInt32());
        Assert.Equal(20, arguments.GetProperty("levels").GetInt32());
    }

    [Fact]
    public async Task GetCallStack_AllStoppedTarget_ForwardsExactThreadAndPaging()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack());
        var debugSessionId = await StartStoppedSessionAsync(driver, "call-stack-page-start");
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, int.MinValue, uint.MaxValue, 256), ModernMcpProcessDriver.CurrentMeta(), new RequestId("call-stack-page"));
        Assert.Equal("call_stack_success", AssertApplicationEnvelope(response, isError: false)["kind"]?.GetValue<string>());
        var stackTrace = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
        using var request = JsonDocument.Parse(stackTrace.Detail ?? throw new Xunit.Sdk.XunitException("Controlled adapter did not record stackTrace arguments."));
        var arguments = request.RootElement.GetProperty("arguments");
        Assert.Equal(int.MinValue, arguments.GetProperty("threadId").GetInt32());
        Assert.Equal(uint.MaxValue, arguments.GetProperty("startFrame").GetUInt32());
        Assert.Equal(256, arguments.GetProperty("levels").GetInt32());
    }

    [Fact]
    public async Task GetCallStack_PartialStoppedTarget_ReplacesPriorTargetEligibility()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(lifecycleMode: "partial-stop-replacement"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "call-stack-partial-replacement-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureRecordAsync("partial-stop-replacement", observation.Token);
        var before = await driver.ReadNativeActionsAsync();
        AssertRefused(AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("call-stack-partial-replaced")), isError: true));
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
        Assert.Equal("call_stack_success", AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 2), ModernMcpProcessDriver.CurrentMeta(), new RequestId("call-stack-partial-current")), isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetCallStack_PartialStoppedWithoutThread_FailsClosedWithoutStackTraceWrite()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(lifecycleMode: "partial-stop-missing-thread"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "call-stack-partial-missing-start");
        var before = await driver.ReadNativeActionsAsync();
        AssertRefused(AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("call-stack-partial-missing")), isError: true));
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
    }

    [Theory]
    [InlineData("all-stop-continued-omitted")]
    [InlineData("all-stop-continued-true")]
    public async Task GetCallStack_AllThreadContinuation_ClearsEveryTargetWithoutPostEventWrite(string lifecycleMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(lifecycleMode: lifecycleMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-{lifecycleMode}-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync("continued", observation.Token);
        var before = await driver.ReadNativeActionsAsync();
        foreach (var target in new[] { 1, 2 })
        {
            AssertRefused(AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, target), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{lifecycleMode}-{target}")), isError: true));
        }

        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
    }
    [Theory]
    [InlineData("all-stop-exited", "exited")]
    [InlineData("all-stop-terminated", "terminated")]
    public async Task GetCallStack_TerminalEvent_ClearsEveryTargetWithoutPostEventWrite(string lifecycleMode, string terminalEvent)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(lifecycleMode: lifecycleMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-{lifecycleMode}-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync(terminalEvent, observation.Token);
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{lifecycleMode}-state-before")), isError: false);
        Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
        Assert.Equal(terminalEvent, Assert.IsType<JsonObject>(state["state"])["event"]?.GetValue<string>());
        var before = await driver.ReadNativeActionsAsync();

        AssertRefused(AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{lifecycleMode}")), isError: true));

        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{lifecycleMode}-state-after")), isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetCallStack_NamedPartialContinuation_InvalidatesOnlyNamedTarget()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(lifecycleMode: "all-stop-continued-partial"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "call-stack-partial-continued-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync("continued", observation.Token);
        var before = await driver.ReadNativeActionsAsync();
        AssertRefused(AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("call-stack-partial-continued-one")), isError: true));
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
        Assert.Equal("call_stack_success", AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 2), ModernMcpProcessDriver.CurrentMeta(), new RequestId("call-stack-partial-continued-two")), isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetCallStack_PartialContinuationWithoutThread_FailsClosedForEveryTarget()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(lifecycleMode: "all-stop-continued-partial-missing-thread"));
        var debugSessionId = await StartStoppedSessionAsync(driver, "call-stack-partial-continued-missing-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync("continued", observation.Token);
        var before = await driver.ReadNativeActionsAsync();
        foreach (var target in new[] { 1, 2 })
        {
            AssertRefused(AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, target), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-partial-continued-missing-{target}")), isError: true));
        }

        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "stackTrace");
    }

    [Theory]
    [InlineData("hold-then-continued-omitted", "continued-event")]
    [InlineData("hold-then-continued-restop", "restopped-event")]
    public async Task GetCallStack_ResponseAfterCapturedTargetInvalidation_RefusesWithoutFrames(string responseMode, string invalidationObservation)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(responseMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-{responseMode}-start");
        var response = driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{responseMode}"), timeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForStackTraceRequestAsync(observation.Token);
        await driver.WaitForFixtureRecordAsync(invalidationObservation, observation.Token);
        driver.ReleaseStackTraceResponse();
        var content = AssertApplicationEnvelope(await response, isError: true);
        AssertRefused(content);
        Assert.False(content.ContainsKey("frames"));
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{responseMode}-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Theory]
    [InlineData("refused", false)]
    [InlineData("wrong-command", true)]
    [InlineData("malformed-body", true)]
    [InlineData("reader-failure", true)]
    [InlineData("too-many-frames", true)]
    [InlineData("too-many-name-bytes", true)]
    [InlineData("too-many-path-bytes", true)]
    [InlineData("line-above-safe", true)]
    [InlineData("column-negative", true)]
    [InlineData("line-non-integral", true)]
    public async Task GetCallStack_AdapterRefusalOrProtocolFailure_ReturnsRedactedResultAndPreservesOrEvicts(string responseMode, bool protocolFailure)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(responseMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-{responseMode}-start");
        var response = await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{responseMode}"), timeout: TimeSpan.FromSeconds(35));
        var content = AssertApplicationEnvelope(response, isError: true);
        if (protocolFailure)
        {
            AssertProtocolError(content);
            AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{responseMode}-state")), isError: true));
            return;
        }

        AssertRefused(content);
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{responseMode}-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetCallStack_Timeout_ReturnsRedactedProtocolErrorAndEvictsToken()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack("timeout"));
        var debugSessionId = await StartStoppedSessionAsync(
            driver,
            "call-stack-timeout-start",
            setupObservationTimeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        var response = driver.CallToolRawAsync(
            Tool,
            Arguments(debugSessionId, 1),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("call-stack-timeout"),
            timeout: TimeSpan.FromSeconds(35));
        await driver.WaitForStackTraceRequestAsync(observation.Token);
        await driver.WaitForFixtureRecordAsync("stack-trace-response-timeout", observation.Token);

        var content = AssertApplicationEnvelope(await response, isError: true);

        AssertProtocolError(content);
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("call-stack-timeout-state")), isError: true));
    }

    [Theory]
    [InlineData("max-name-bytes", false, 1, null)]
    [InlineData("max-path-bytes", false, 1, null)]
    [InlineData("safe-line-column", false, 1, null)]
    [InlineData("structured-content-at-limit", false, 256, 262144)]
    [InlineData("structured-content-over-limit", true, 0, null)]
    public async Task GetCallStack_BoundaryResponse_ReturnsExactSuccessOrProtocolError(string responseMode, bool isError, int expectedFrameCount, int? expectedStructuredContentBytes)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(responseMode));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-{responseMode}-start");
        var response = await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-{responseMode}"));
        var content = AssertApplicationEnvelope(response, isError);
        if (isError)
        {
            AssertProtocolError(content);
            return;
        }

        Assert.Equal("call_stack_success", content["kind"]?.GetValue<string>());
        Assert.Equal(expectedFrameCount, Assert.IsType<JsonArray>(content["frames"]).Count);
        if (responseMode == "safe-line-column")
        {
            AssertFrame(Assert.IsType<JsonArray>(content["frames"])[0], 1, "frame", "C:/safe.cs", MaximumSafeDapInteger, MaximumSafeDapInteger);
        }

        if (expectedStructuredContentBytes is { } expected)
        {
            Assert.Equal(expected, Encoding.UTF8.GetByteCount(content.ToJsonString()));
        }
    }

    private static ModernMcpStartOptions CallStack(string responseMode = "success", bool supportsDelayedStackTraceLoading = true, bool omitDelayedStackTraceLoading = false, string lifecycleMode = "all-stop") => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(
            SupportsDelayedStackTraceLoading: supportsDelayedStackTraceLoading,
            OmitDelayedStackTraceLoading: omitDelayedStackTraceLoading,
            LifecycleMode: lifecycleMode,
            StackTraceResponseMode: responseMode));

    private static async Task<string> StartStoppedSessionAsync(
        ModernMcpProcessDriver driver,
        string requestId,
        TimeSpan? setupObservationTimeout = null)
    {
        var start = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId),
            timeout: setupObservationTimeout), isError: false);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        return Assert.IsType<string>(start["debugSessionId"]?.GetValue<string>());
    }

    private static JsonObject Arguments(string debugSessionId, int threadId, uint? startFrame = null, uint? levels = null)
    {
        var arguments = new JsonObject { ["debugSessionId"] = debugSessionId, ["threadId"] = threadId };
        if (startFrame is { } start)
        {
            arguments["startFrame"] = start;
        }

        if (levels is { } requestedLevels)
        {
            arguments["levels"] = requestedLevels;
        }

        return arguments;
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

    private static void AssertFrame(JsonNode? node, int id, string name, string? source, long line, long column)
    {
        var frame = Assert.IsType<JsonObject>(node);
        Assert.Equal(id, frame["id"]?.GetValue<int>());
        Assert.Equal(name, frame["name"]?.GetValue<string>());
        Assert.Equal(source, frame["source"]?.GetValue<string>());
        Assert.Equal(line, frame["line"]?.GetValue<long>());
        Assert.Equal(column, frame["column"]?.GetValue<long>());
    }

    private static void AssertNotFound(JsonObject content)
    {
        Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static void AssertRefused(JsonObject content)
    {
        Assert.Equal("dap_stack_trace_refused", content["kind"]?.GetValue<string>());
        Assert.Equal("DAP_STACK_TRACE_REFUSED", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.False(content.ContainsKey("body"));
        Assert.False(content.ContainsKey("message"));
    }

    private static void AssertProtocolError(JsonObject content)
    {
        Assert.Equal("dap_stack_trace_protocol_error", content["kind"]?.GetValue<string>());
        Assert.Equal("DAP_STACK_TRACE_PROTOCOL_ERROR", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.False(content.ContainsKey("body"));
        Assert.False(content.ContainsKey("message"));
    }
}
