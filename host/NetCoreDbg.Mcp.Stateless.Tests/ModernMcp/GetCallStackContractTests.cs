using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class GetCallStackContractTests
{
    private const string Tool = "get_call_stack";
    private const string EvaluateTool = "evaluate_expression";
    private const string ExceptionInfoTool = "get_exception_info";
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
    public async Task GetCallStack_CapabilityOmittedOrFalse_ReturnsDefaultAndExplicitLocalPagesWithOneUnpagedWrite(bool explicitFalse)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack("full-stack-with-total-frames", supportsDelayedStackTraceLoading: false, omitDelayedStackTraceLoading: !explicitFalse));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-unpaged-{explicitFalse}-start");
        var pages = new (string Name, uint? StartFrame, uint? Levels, int FirstFrameId, int Count)[]
        {
            ("default", null, null, 256, 20),
            ("whole-stack", 0U, 256U, 256, 256),
            ("middle", 1U, 3U, 255, 3),
            ("tail", 255U, 256U, 1, 1),
            ("past-end", 256U, 1U, 0, 0),
            ("maximum-offset", uint.MaxValue, 256U, 0, 0),
        };
        foreach (var page in pages)
        {
            var before = await driver.ReadNativeActionsAsync();
            var response = await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, int.MinValue, page.StartFrame, page.Levels), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-unpaged-{explicitFalse}-{page.Name}"));
            var content = AssertApplicationEnvelope(response, isError: false);
            Assert.Equal("call_stack_success", content["kind"]?.GetValue<string>());
            Assert.Equal(256, content["totalFrames"]?.GetValue<int>());
            var frames = Assert.IsType<JsonArray>(content["frames"]);
            Assert.Equal(page.Count, frames.Count);
            for (var index = 0; index < frames.Count; index++)
            {
                AssertFrame(frames[index], page.FirstFrameId - index, "frame", null, 0, 0);
            }

            AssertSingleUnpagedStackTrace((await driver.ReadNativeActionsAsync()).Skip(before.Count), int.MinValue);
        }

        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-unpaged-{explicitFalse}-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task GetCallStack_CapabilityOmittedOrFalse_PreservesOmittedTotalFramesOnLocalPage(bool explicitFalse)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(supportsDelayedStackTraceLoading: false, omitDelayedStackTraceLoading: !explicitFalse));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-unpaged-no-total-{explicitFalse}-start");
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 17, 1, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-unpaged-no-total-{explicitFalse}"));
        var content = AssertApplicationEnvelope(response, isError: false);
        Assert.Equal("call_stack_success", content["kind"]?.GetValue<string>());
        Assert.False(content.ContainsKey("totalFrames"));
        AssertFrame(Assert.Single(Assert.IsType<JsonArray>(content["frames"])), 2, "frame", null, 0, 0);
        AssertSingleUnpagedStackTrace((await driver.ReadNativeActionsAsync()).Skip(before.Count), 17);
    }

    [Theory]
    [InlineData(false, "too-many-frames", 0U)]
    [InlineData(true, "too-many-frames", 0U)]
    [InlineData(false, "too-many-frames", uint.MaxValue)]
    [InlineData(true, "too-many-frames", uint.MaxValue)]
    [InlineData(false, "malformed-trailing-frame", 0U)]
    [InlineData(true, "malformed-trailing-frame", 0U)]
    [InlineData(false, "malformed-trailing-frame", uint.MaxValue)]
    [InlineData(true, "malformed-trailing-frame", uint.MaxValue)]
    public async Task GetCallStack_CapabilityOmittedOrFalse_ValidatesWholeReplyBeforePagingAndDrainsOnProtocolError(bool explicitFalse, string responseMode, uint startFrame)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(responseMode, supportsDelayedStackTraceLoading: false, omitDelayedStackTraceLoading: !explicitFalse));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-unpaged-{explicitFalse}-{responseMode}-{startFrame}-start");
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1, startFrame, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-unpaged-{explicitFalse}-{responseMode}-{startFrame}"));
        AssertProtocolError(AssertApplicationEnvelope(response, isError: true));
        AssertSingleUnpagedStackTrace((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1);
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-unpaged-{explicitFalse}-{responseMode}-{startFrame}-state")), isError: true));
    }

    [Theory]
    [InlineData(false, "hold-then-continued-omitted", "continued-event")]
    [InlineData(true, "hold-then-continued-omitted", "continued-event")]
    [InlineData(false, "hold-then-continued-restop", "restopped-event")]
    [InlineData(true, "hold-then-continued-restop", "restopped-event")]
    public async Task GetCallStack_CapabilityOmittedOrFalse_ResponseAfterCapturedTargetInvalidationRefusesStaleFrames(bool explicitFalse, string responseMode, string invalidationObservation)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(responseMode, supportsDelayedStackTraceLoading: false, omitDelayedStackTraceLoading: !explicitFalse));
        var debugSessionId = await StartStoppedSessionAsync(driver, $"call-stack-unpaged-{explicitFalse}-{responseMode}-start");
        var before = await driver.ReadNativeActionsAsync();
        var response = driver.CallToolRawAsync(Tool, Arguments(debugSessionId, 1, 0, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-unpaged-{explicitFalse}-{responseMode}"), timeout: TimeSpan.FromSeconds(35));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForStackTraceRequestAsync(observation.Token);
        await driver.WaitForFixtureRecordAsync(invalidationObservation, observation.Token);
        driver.ReleaseStackTraceResponse();
        var content = AssertApplicationEnvelope(await response, isError: true);
        AssertRefused(content);
        Assert.False(content.ContainsKey("frames"));
        AssertSingleUnpagedStackTrace((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1);
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = debugSessionId }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"call-stack-unpaged-{explicitFalse}-{responseMode}-state")), isError: false)["kind"]?.GetValue<string>());
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
    [InlineData("timeout", true)]
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

    [Fact]
    public async Task FrameInspection_OptInContext_NormalizesScopesVariablesAndNestedValuesWithoutChangingLegacyStack()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack());
        var session = await StartStoppedSessionAsync(driver, "inspection-start");
        var legacy = AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("inspection-legacy")), isError: false);
        var explicitFalse = Arguments(session, 1);
        explicitFalse["includeInspectionContext"] = false;
        var unchanged = AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, explicitFalse, ModernMcpProcessDriver.CurrentMeta(), new RequestId("inspection-false")), isError: false);
        Assert.True(JsonNode.DeepEquals(legacy, unchanged));
        Assert.False(legacy.ContainsKey("inspectionContextId"));

        var stack = await InspectionStackAsync(driver, session, "inspection-stack");
        var context = Assert.IsType<string>(stack["inspectionContextId"]?.GetValue<string>());
        Assert.False(string.IsNullOrWhiteSpace(context));
        var withoutContext = stack.DeepClone().AsObject();
        withoutContext.Remove("inspectionContextId");
        Assert.True(JsonNode.DeepEquals(legacy, withoutContext));
        var frame = Assert.IsType<JsonArray>(stack["frames"])[0]!["id"]!.GetValue<int>();
        var before = await driver.ReadNativeActionsAsync();
        var scopes = await InspectionCallAsync(driver, "get_scopes", InspectionArguments(session, context, "frameId", frame), "inspection-scopes");
        Assert.Equal("scopes_success", scopes["kind"]?.GetValue<string>());
        Assert.Equal(context, scopes["inspectionContextId"]?.GetValue<string>());
        var scopeEntries = Assert.IsType<JsonArray>(scopes["scopes"]);
        Assert.Equal(2, scopeEntries.Count);
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("{\"name\":\"Locals\",\"variablesReference\":1001,\"expensive\":false}"), scopeEntries[0]));
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("{\"name\":\"Registers\",\"variablesReference\":1003,\"expensive\":true}"), scopeEntries[1]));
        var reference = scopeEntries[0]!["variablesReference"]!.GetValue<int>();
        var variables = await InspectionCallAsync(driver, "get_variables", InspectionArguments(session, context, "variablesReference", reference), "inspection-variables");
        Assert.Equal("variables_success", variables["kind"]?.GetValue<string>());
        Assert.Equal(context, variables["inspectionContextId"]?.GetValue<string>());
        var entries = Assert.IsType<JsonArray>(variables["variables"]);
        Assert.Equal(3, entries.Count);
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("{\"name\":\"intVar\",\"value\":\"42\",\"type\":\"int\",\"variablesReference\":0}"), entries[0]));
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("{\"name\":\"tupleVar\",\"value\":\"(\\\"test\\\", 123)\",\"type\":null,\"variablesReference\":1002}"), entries[1]));
        Assert.Equal(new string('v', 16_385), entries[2]?["value"]?.GetValue<string>());
        Assert.True(Assert.IsType<JsonObject>(entries[2]).ContainsKey("type"));
        Assert.Null(entries[2]?["type"]);
        Assert.Equal(0, entries[2]?["variablesReference"]?.GetValue<int>());
        var nested = await InspectionCallAsync(driver, "get_variables", InspectionArguments(session, context, "variablesReference", entries[1]!["variablesReference"]!.GetValue<int>()), "inspection-nested");
        Assert.Equal("variables_success", nested["kind"]?.GetValue<string>());
        Assert.Equal(context, nested["inspectionContextId"]?.GetValue<string>());
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("[{\"name\":\"Item1\",\"value\":\"\\\"test\\\"\",\"type\":\"string\",\"variablesReference\":0},{\"name\":\"Item2\",\"value\":\"123\",\"type\":\"int\",\"variablesReference\":0}]"), nested["variables"]));
        var actions = (await driver.ReadNativeActionsAsync()).Skip(before.Count).ToArray();
        Assert.Equal(["scopes", "variables", "variables"], actions.Select(static action => action.Command));
        Assert.True(JsonNode.DeepEquals(new JsonObject { ["frameId"] = frame }, JsonNode.Parse(actions[0].Detail!)!["arguments"]));
        Assert.True(JsonNode.DeepEquals(new JsonObject { ["variablesReference"] = reference }, JsonNode.Parse(actions[1].Detail!)!["arguments"]));
        Assert.True(JsonNode.DeepEquals(new JsonObject { ["variablesReference"] = 1002 }, JsonNode.Parse(actions[2].Detail!)!["arguments"]));
    }

    [Fact]
    public async Task FrameInspection_StepReusesNumericReference_OldContextIsRefusedWithoutIoAndFreshContextWorks()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack());
        var session = await StartStoppedSessionAsync(driver, "inspection-reuse-start");
        var stack = await InspectionStackAsync(driver, session, "inspection-reuse-old-stack");
        var oldContext = stack["inspectionContextId"]!.GetValue<string>();
        var frame = Assert.IsType<JsonArray>(stack["frames"])[0]!["id"]!.GetValue<int>();
        var oldScopes = await InspectionCallAsync(driver, "get_scopes", InspectionArguments(session, oldContext, "frameId", frame), "inspection-reuse-old-scopes");
        var reference = Assert.IsType<JsonArray>(oldScopes["scopes"])[0]!["variablesReference"]!.GetValue<int>();
        var oldVariables = await InspectionCallAsync(driver, "get_variables", InspectionArguments(session, oldContext, "variablesReference", reference), "inspection-reuse-old-variables");
        Assert.Equal("42", Assert.IsType<JsonArray>(oldVariables["variables"])[0]?["value"]?.GetValue<string>());
        var step = AssertApplicationEnvelope(await driver.CallToolRawAsync("step_over", Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(), new RequestId("inspection-reuse-step")), isError: false);
        Assert.Equal("step_over_success", step["kind"]?.GetValue<string>());
        var freshStack = await InspectionStackAsync(driver, session, "inspection-reuse-fresh-stack");
        var context = freshStack["inspectionContextId"]!.GetValue<string>();
        Assert.NotEqual(oldContext, context);
        var freshFrame = Assert.IsType<JsonArray>(freshStack["frames"])[0]!["id"]!.GetValue<int>();
        var freshScopes = await InspectionCallAsync(driver, "get_scopes", InspectionArguments(session, context, "frameId", freshFrame), "inspection-reuse-fresh-scopes");
        Assert.Equal(reference, Assert.IsType<JsonArray>(freshScopes["scopes"])[0]?["variablesReference"]?.GetValue<int>());
        var before = await driver.ReadNativeActionsAsync();
        foreach (var (tool, field, id) in new[] { ("get_scopes", "frameId", frame), ("get_variables", "variablesReference", reference) })
        {
            var refused = await InspectionCallAsync(driver, tool, InspectionArguments(session, oldContext, field, id), $"inspection-reuse-stale-{tool}", isError: true);
            AssertInspectionFailure(refused, tool, protocolFailure: false);
        }
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        var freshVariables = await InspectionCallAsync(driver, "get_variables", InspectionArguments(session, context, "variablesReference", reference), "inspection-reuse-fresh-variables");
        Assert.Equal("variables_success", freshVariables["kind"]?.GetValue<string>());
        Assert.Equal(context, freshVariables["inspectionContextId"]?.GetValue<string>());
        Assert.Equal("43", Assert.IsType<JsonArray>(freshVariables["variables"])[0]?["value"]?.GetValue<string>());
    }

    [Theory]
    [InlineData("get_scopes", "frameId", 1)]
    [InlineData("get_variables", "variablesReference", 1001)]
    public async Task FrameInspection_ResponseHeldAcrossInvalidatedEvent_RefusesStaleDataAndKeepsSession(string tool, string field, int id)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(InspectionOptions(tool, "hold-then-invalidated"));
        var session = await StartStoppedSessionAsync(driver, $"inspection-held-{tool}-start");
        var stack = await InspectionStackAsync(driver, session, $"inspection-held-{tool}-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var response = driver.CallToolRawAsync(tool, InspectionArguments(session, context, field, id), ModernMcpProcessDriver.CurrentMeta(), new RequestId($"inspection-held-{tool}"));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureRecordAsync("inspection-response-gated", observation.Token);
        var barrier = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"inspection-held-{tool}-reader-barrier")), isError: false);
        Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
        driver.ReleaseStackTraceResponse();
        var refused = AssertApplicationEnvelope(await response, isError: true);
        StructuredContentSchemaParityTests.AssertMatchesSchema(refused);
        AssertInspectionFailure(refused, tool, protocolFailure: false);
        var freshStack = await InspectionStackAsync(driver, session, $"inspection-held-{tool}-fresh-stack");
        var freshContext = freshStack["inspectionContextId"]!.GetValue<string>();
        Assert.NotEqual(context, freshContext);
        var fresh = await InspectionCallAsync(driver, tool, InspectionArguments(session, freshContext, field, id), $"inspection-held-{tool}-fresh");
        Assert.Equal(tool == "get_scopes" ? "scopes_success" : "variables_success", fresh["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task FrameInspection_InvalidInputsAndUnknownContext_ReturnSafeErrorsWithoutDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack());
        var session = await StartStoppedSessionAsync(driver, "inspection-invalid-start");
        var stack = await InspectionStackAsync(driver, session, "inspection-invalid-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var missingContext = new JsonObject { ["debugSessionId"] = session, ["frameId"] = 1 };
        var fractionalFrame = InspectionArguments(session, context, "frameId", 1);
        fractionalFrame["frameId"] = 1.5;
        var invalidOptIn = Arguments(session, 1);
        invalidOptIn["includeInspectionContext"] = "true";
        var before = await driver.ReadNativeActionsAsync();
        var caseIndex = 0;
        foreach (var (tool, arguments) in new[]
        {
            ("get_scopes", missingContext),
            ("get_scopes", fractionalFrame),
            ("get_variables", InspectionArguments(session, context, "variablesReference", 0)),
            (Tool, invalidOptIn),
        })
        {
            var invalid = AssertApplicationEnvelope(await driver.CallToolRawAsync(tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"inspection-invalid-{tool}-{caseIndex++}")), isError: true);
            Assert.Equal("invalid_tool_arguments", invalid["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", invalid["error"]?.GetValue<string>());
            Assert.Equal(tool, invalid["tool"]?.GetValue<string>());
            StructuredContentSchemaParityTests.AssertMatchesSchema(invalid);
        }
        foreach (var (tool, field) in new[] { ("get_scopes", "frameId"), ("get_variables", "variablesReference") })
        {
            var refused = await InspectionCallAsync(driver, tool, InspectionArguments(session, "unknown-inspection-context", field, 1), $"inspection-unknown-{tool}", isError: true);
            AssertInspectionFailure(refused, tool, protocolFailure: false);
        }
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("get_scopes", "frameId", 1, "refused", false)]
    [InlineData("get_variables", "variablesReference", 1001, "refused", false)]
    [InlineData("get_scopes", "frameId", 1, "malformed-tail", true)]
    [InlineData("get_variables", "variablesReference", 1001, "malformed-tail", true)]
    public async Task FrameInspection_AdapterRefusalOrMalformedTail_RedactsAndPreservesOrDrains(string tool, string field, int id, string mode, bool protocolFailure)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(InspectionOptions(tool, mode));
        var session = await StartStoppedSessionAsync(driver, $"inspection-failure-{tool}-{mode}-start");
        var stack = await InspectionStackAsync(driver, session, $"inspection-failure-{tool}-{mode}-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var before = await driver.ReadNativeActionsAsync();
        var failure = await InspectionCallAsync(driver, tool, InspectionArguments(session, context, field, id), $"inspection-failure-{tool}-{mode}", isError: true);
        AssertInspectionFailure(failure, tool, protocolFailure);
        Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), action => action.Command == (tool == "get_scopes" ? "scopes" : "variables"));
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"inspection-failure-{tool}-{mode}-state")), isError: protocolFailure);
        if (protocolFailure)
        {
            AssertNotFound(state);
        }
        else
        {
            Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
        }
    }

    [Fact]
    public async Task ToolsList_GetExceptionInfo_PublishesClosedExplicitStoppedThreadInputWithoutDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var before = await driver.ReadNativeActionsAsync();
        var result = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-catalog")));
        var tool = Assert.Single(Assert.IsType<JsonArray>(result["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == ExceptionInfoTool);
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId", "threadId"], Assert.IsType<JsonArray>(schema["required"]).Select(static item => item?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(2, properties.Count);
        Assert.Equal("string", properties["debugSessionId"]?["type"]?.GetValue<string>());
        Assert.Equal("integer", properties["threadId"]?["type"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData(1, "success", "always")]
    [InlineData(17, "break-never", "never")]
    [InlineData(int.MinValue, "break-unhandled", "unhandled")]
    [InlineData(int.MaxValue, "break-userUnhandled", "userUnhandled")]
    public async Task GetExceptionInfo_AdvertisedStoppedThread_ForwardsExactRequestAndPreservesCompleteNestedBody(int threadId, string mode, string breakMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions(mode), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-exact-start");
        await ExceptionInfoReaderBarrierAsync(driver, session, "exception-info-exact-ready");
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, threadId),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-exact")), isError: false);

        Assert.Equal("exception_info_success", content["kind"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        var expected = JsonNode.Parse("""
            {
              "exceptionId": "System.InvalidOperationException",
              "breakMode": "always",
              "description": "Outer \"failure\"\nЖ",
              "details": {
                "message": "Outer failure",
                "typeName": "InvalidOperationException",
                "fullTypeName": "System.InvalidOperationException",
                "evaluateName": "$exception",
                "stackTrace": "at Controlled.Throw()\nat Controlled.Main()",
                "formattedDescription": "<b>Outer failure</b>",
                "source": "ControlledAssembly",
                "innerException": [{
                  "message": "Inner failure",
                  "typeName": "ArgumentException",
                  "fullTypeName": "System.ArgumentException",
                  "formattedDescription": "<i>Inner failure</i>",
                  "source": "ControlledInnerAssembly",
                  "innerException": [{ "message": "Root cause" }]
                }]
              },
              "adapterExtension": { "code": 17, "tags": ["controlled", "nested"] }
            }
            """)!.AsObject();
        expected["breakMode"] = breakMode;
        Assert.True(JsonNode.DeepEquals(expected, Assert.IsType<JsonObject>(content["exception"])));
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        AssertSingleExceptionInfoRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), threadId);
    }

    [Fact]
    public async Task GetExceptionInfo_MinimalBody_PreservesOmittedOptionalFields()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions("minimal"), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-minimal-start");
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-minimal")), isError: false);
        Assert.True(JsonNode.DeepEquals(new JsonObject
        {
            ["kind"] = "exception_info_success",
            ["exception"] = new JsonObject { ["exceptionId"] = "ControlledException", ["breakMode"] = "never" },
        }, content));
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(null)]
    public async Task GetExceptionInfo_CapabilityFalseOrOmitted_RefusesWithoutDapWriteAndKeepsSession(bool? supported)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions(supported: supported), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-unsupported-start");
        await ExceptionInfoReaderBarrierAsync(driver, session, "exception-info-unsupported-ready");
        var before = await driver.ReadNativeActionsAsync();

        AssertExceptionInfoFailure(AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-unsupported")), isError: true), "unsupported");

        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state",
            new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-unsupported-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Theory]
    [InlineData(false, "enable-on-threads", true)]
    [InlineData(null, "enable-on-threads", true)]
    [InlineData(true, "disable-on-threads", false)]
    public async Task GetExceptionInfo_CapabilityDelta_ChangesAdmissionInReaderOrder(bool? initialSupport, string delta, bool finalSupport)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions(supported: initialSupport, capabilityDelta: delta), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-delta-start");
        var before = await driver.ReadNativeActionsAsync();
        var initial = AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-before-delta")), isError: initialSupport != true);
        if (initialSupport == true)
        {
            Assert.Equal("exception_info_success", initial["kind"]?.GetValue<string>());
            AssertSingleExceptionInfoRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1);
        }
        else
        {
            AssertExceptionInfoFailure(initial, "unsupported");
            Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        }

        // The correlated threads reply follows the capability delta on the same DAP reader.
        await ExceptionInfoReaderBarrierAsync(driver, session, "exception-info-delta-barrier");
        before = await driver.ReadNativeActionsAsync();
        var updated = AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-after-delta")), isError: !finalSupport);
        if (finalSupport)
        {
            Assert.Equal("exception_info_success", updated["kind"]?.GetValue<string>());
            AssertSingleExceptionInfoRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1);
        }
        else
        {
            AssertExceptionInfoFailure(updated, "unsupported");
            Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        }
    }

    [Theory]
    [InlineData("missing-arguments")]
    [InlineData("missing-session")]
    [InlineData("blank-session")]
    [InlineData("non-string-session")]
    [InlineData("missing-thread")]
    [InlineData("null-thread")]
    [InlineData("string-thread")]
    [InlineData("fractional-thread")]
    [InlineData("thread-above-int32")]
    [InlineData("thread-below-int32")]
    [InlineData("extra-field")]
    public async Task GetExceptionInfo_InvalidInput_RejectsBeforeAnyDapWrite(string scenario)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-invalid-start");
        JsonObject? arguments = Arguments(session, 1);
        switch (scenario)
        {
            case "missing-arguments": arguments = null; break;
            case "missing-session": arguments.Remove("debugSessionId"); break;
            case "blank-session": arguments["debugSessionId"] = " \t"; break;
            case "non-string-session": arguments["debugSessionId"] = 17; break;
            case "missing-thread": arguments.Remove("threadId"); break;
            case "null-thread": arguments["threadId"] = null; break;
            case "string-thread": arguments["threadId"] = "1"; break;
            case "fractional-thread": arguments["threadId"] = 1.5; break;
            case "thread-above-int32": arguments["threadId"] = 2_147_483_648L; break;
            case "thread-below-int32": arguments["threadId"] = -2_147_483_649L; break;
            case "extra-field": arguments["inspectionContextId"] = "not-an-input"; break;
        }
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, arguments,
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"exception-info-invalid-{scenario}")), isError: true);

        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(ExceptionInfoTool, content["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task GetExceptionInfo_UnknownOrRetiredSession_ReturnsNotFoundWithoutDapIo(bool retired)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = "unknown-opaque-session";
        if (retired)
        {
            session = await StartStoppedSessionAsync(driver, "exception-info-retired-start");
            AssertApplicationEnvelope(await driver.CallToolRawAsync("stop_debug", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-retire")), isError: false);
        }
        var before = await driver.ReadNativeActionsAsync();

        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-not-found")), isError: true));

        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("partial-stop", 2, false)]
    [InlineData("all-stop-continued-true", 1, true)]
    public async Task GetExceptionInfo_NonStoppedOrContinuedTarget_RefusesBeforeDapWrite(string lifecycle, int threadId, bool awaitContinued)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions(lifecycleMode: lifecycle), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-admission-start");
        if (awaitContinued)
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await driver.WaitForFixtureEventAsync("continued", observation.Token);
        }
        await ExceptionInfoReaderBarrierAsync(driver, session, "exception-info-admission-barrier");
        var before = await driver.ReadNativeActionsAsync();

        AssertExceptionInfoFailure(AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, threadId),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-admission")), isError: true), "refused");

        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("hold-then-continued")]
    [InlineData("hold-then-continued-restop")]
    public async Task GetExceptionInfo_ResponseAfterReaderOrderedInvalidation_RefusesStaleBodyWithoutRetry(string mode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions(mode), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-held-start");
        var before = await driver.ReadNativeActionsAsync();
        var response = driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("exception-info-held"), timeout: TimeSpan.FromSeconds(35));
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await driver.WaitForFixtureRecordAsync("exception-info-response-gated", observation.Token);
            await ExceptionInfoReaderBarrierAsync(driver, session, "exception-info-held-barrier", observation.Token);
            Assert.False(response.IsCompleted, "The exception reply must remain held until invalidation has passed the reader barrier.");
        }
        finally
        {
            // Reuse the existing shared inspection-response gate, not a new fixture harness.
            driver.ReleaseStackTraceResponse();
        }

        AssertExceptionInfoFailure(AssertApplicationEnvelope(await response, isError: true), "refused");
        AssertSingleExceptionInfoRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1);
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state",
            new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-held-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetExceptionInfo_CapabilityRevokedAfterAdmission_PreservesCurrentReplyAndRefusesSubsequentWrite()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions("hold-then-capability-revoked"), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-revoked-start");
        var before = await driver.ReadNativeActionsAsync();
        var response = driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("exception-info-revoked-held"), timeout: TimeSpan.FromSeconds(35));
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await driver.WaitForFixtureRecordAsync("exception-info-response-gated", observation.Token);
            await ExceptionInfoReaderBarrierAsync(driver, session, "exception-info-revoked-barrier", observation.Token);
            Assert.False(response.IsCompleted, "The admitted reply must remain held until capability revocation has passed the reader barrier.");
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
        }

        var content = AssertApplicationEnvelope(await response, isError: false);
        Assert.Equal("exception_info_success", content["kind"]?.GetValue<string>());
        var exception = Assert.IsType<JsonObject>(content["exception"]);
        Assert.Equal("System.InvalidOperationException", exception["exceptionId"]?.GetValue<string>());
        Assert.Equal("always", exception["breakMode"]?.GetValue<string>());
        Assert.Equal("Outer \"failure\"\nЖ", exception["description"]?.GetValue<string>());
        Assert.Equal("<b>Outer failure</b>", exception["details"]?["formattedDescription"]?.GetValue<string>());
        Assert.Equal("ControlledAssembly", exception["details"]?["source"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        AssertSingleExceptionInfoRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1);
        var afterReply = await driver.ReadNativeActionsAsync();

        AssertExceptionInfoFailure(AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-revoked-next")), isError: true), "unsupported");

        Assert.Equal(afterReply.Count, (await driver.ReadNativeActionsAsync()).Count);
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state",
            new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-revoked-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Theory]
    [InlineData("refused", false)]
    [InlineData("non-object-body", true)]
    [InlineData("missing-id", true)]
    [InlineData("non-string-id", true)]
    [InlineData("missing-break-mode", true)]
    [InlineData("invalid-break-mode", true)]
    [InlineData("non-string-description", true)]
    [InlineData("null-description", true)]
    [InlineData("non-object-details", true)]
    [InlineData("null-details", true)]
    [InlineData("oversized", true)]
    [InlineData("wrong-command", true)]
    [InlineData("timeout", true)]
    public async Task GetExceptionInfo_RefusalOrProtocolError_RedactsWithoutRetryAndPreservesOrDrainsSession(string mode, bool protocolError)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions(mode), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-failure-start");
        var before = await driver.ReadNativeActionsAsync();

        var response = await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1), ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("exception-info-failure"), timeout: TimeSpan.FromSeconds(35));

        AssertExceptionInfoFailure(AssertApplicationEnvelope(response, isError: true), protocolError ? "protocol_error" : "refused");
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-failure-state")), isError: protocolError);
        if (protocolError)
        {
            AssertNotFound(state);
        }
        else
        {
            Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
        }
        var actions = (await driver.ReadNativeActionsAsync()).Skip(before.Count).ToArray();
        AssertSingleExceptionInfoRequest(actions, 1);
        Assert.Equal(protocolError ? 1 : 0, actions.Count(static action => action.Command == "disconnect"));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    public async Task GetExceptionInfo_CompleteSerializedByteLimit_IncludesEnvelopeEscapingAndRejectsOnlyAboveLimit(int excessBytes)
    {
        const int limit = 256 * 1024;
        const string suffix = "\"\nЖ";
        var expected = new JsonObject
        {
            ["kind"] = "exception_info_success",
            ["exception"] = new JsonObject { ["exceptionId"] = "ControlledException", ["breakMode"] = "always", ["description"] = suffix },
        };
        var size = limit - Encoding.UTF8.GetByteCount(expected.ToJsonString()) + excessBytes;
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionInfoOptions($"sized:{size}"), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "exception-info-boundary-start");
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(ExceptionInfoTool, Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-boundary"), timeout: TimeSpan.FromSeconds(35)), isError: excessBytes != 0);

        AssertSingleExceptionInfoRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1);
        if (excessBytes == 0)
        {
            expected["exception"]!["description"] = new string('x', size) + suffix;
            Assert.True(JsonNode.DeepEquals(expected, content));
            Assert.Equal(limit, Encoding.UTF8.GetByteCount(content.ToJsonString()));
            StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        }
        else
        {
            AssertExceptionInfoFailure(content, "protocol_error");
            AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-info-boundary-state")), isError: true));
        }
    }

    private static ModernMcpStartOptions ExceptionInfoOptions(string mode = "success", bool? supported = true, string capabilityDelta = "none", string lifecycleMode = "all-stop") => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(
            LifecycleMode: lifecycleMode,
            SupportsExceptionInfoRequest: supported,
            ExceptionInfoResponseMode: mode,
            ExceptionInfoCapabilityDelta: capabilityDelta));

    private static async Task ExceptionInfoReaderBarrierAsync(ModernMcpProcessDriver driver, string session, string requestId, CancellationToken cancellationToken = default)
    {
        var result = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId), cancellationToken), isError: false);
        Assert.Equal("threads_success", result["kind"]?.GetValue<string>());
    }

    private static void AssertSingleExceptionInfoRequest(IEnumerable<ModernNativeAction> actions, int threadId)
    {
        var request = Assert.Single(actions, static action => action.Command == "exceptionInfo");
        Assert.True(JsonNode.DeepEquals(new JsonObject { ["threadId"] = threadId }, JsonNode.Parse(request.Detail!)!["arguments"]));
    }

    private static void AssertExceptionInfoFailure(JsonObject content, string outcome)
    {
        Assert.Equal($"dap_exception_info_{outcome}", content["kind"]?.GetValue<string>());
        Assert.Equal($"DAP_EXCEPTION_INFO_{outcome.ToUpperInvariant()}", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.DoesNotContain("private", content.ToJsonString(), StringComparison.OrdinalIgnoreCase);
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
    }

    [Fact]
    public async Task ToolsList_SetVariable_PublishesExplicitClosedMutationInput()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var result = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-variable-catalog")));
        var tool = Assert.Single(Assert.IsType<JsonArray>(result["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
                static item => item["name"]?.GetValue<string>() == "set_variable");
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        var fields = new[] { "debugSessionId", "inspectionContextId", "name", "value", "variablesReference" };
        Assert.Equal(fields, Assert.IsType<JsonArray>(schema["required"]).Select(static item => item!.GetValue<string>()).Order(StringComparer.Ordinal));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(fields, properties.Select(static item => item.Key).Order(StringComparer.Ordinal));
        foreach (var field in new[] { "debugSessionId", "inspectionContextId", "name", "value" })
        {
            Assert.Equal("string", properties[field]?["type"]?.GetValue<string>());
        }
        Assert.Equal("integer", properties["variablesReference"]?["type"]?.GetValue<string>());
        Assert.False(tool["annotations"]?["readOnlyHint"]?.GetValue<bool>() ?? false);
        Assert.False(tool["annotations"]?["idempotentHint"]?.GetValue<bool>() ?? false);
    }

    [Theory]
    [InlineData("  имя\t", " \"雪\"\r\n", "updated", "tuple", 1002)]
    [InlineData("fixture:optional", "", "untyped", null, 0)]
    public async Task SetVariable_CurrentContext_ForwardsExactArgumentsAndRotatesContext(
            string name, string value, string expectedValue, string? expectedType, int expectedReference)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(SetVariableOptions(true), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "set-exact-start");
        var context = (await InspectionStackAsync(driver, session, "set-exact-stack"))["inspectionContextId"]!.GetValue<string>();
        var before = await driver.ReadNativeActionsAsync();
        var content = await SetVariableCallAsync(driver, SetVariableArguments(session, context, 1001, name, value), "set-exact");
        Assert.Equal("set_variable_success", content["kind"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        Assert.Equal(expectedValue, content["value"]?.GetValue<string>());
        Assert.Equal(expectedType, content["type"]?.GetValue<string>());
        Assert.Equal(expectedReference, content["variablesReference"]?.GetValue<int>());
        var fresh = Assert.IsType<string>(content["inspectionContextId"]?.GetValue<string>());
        Assert.False(string.IsNullOrWhiteSpace(fresh));
        Assert.NotEqual(context, fresh);
        AssertSetVariableRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1001, name, value);

        var after = await driver.ReadNativeActionsAsync();
        var stale = await InspectionCallAsync(driver, "get_variables", InspectionArguments(session, context, "variablesReference", 1001), "set-old-read", isError: true);
        AssertInspectionFailure(stale, "get_variables", protocolFailure: false);
        AssertSetVariableFailure(await SetVariableCallAsync(driver, SetVariableArguments(session, context, 1001, name, value), "set-old-write", true), "refused");
        Assert.Equal(after.Count, (await driver.ReadNativeActionsAsync()).Count);
        if (expectedReference > 0)
        {
            var nested = await InspectionCallAsync(driver, "get_variables", InspectionArguments(session, fresh, "variablesReference", expectedReference), "set-new-read");
            Assert.Equal("variables_success", nested["kind"]?.GetValue<string>());
            Assert.Equal(fresh, nested["inspectionContextId"]?.GetValue<string>());
            Assert.Equal("Item1", nested["variables"]?[0]?["name"]?.GetValue<string>());
        }
        await driver.StopSingleControlledSessionAsync(session);
    }

    [Theory]
    [InlineData("missing-session")]
    [InlineData("blank-session")]
    [InlineData("missing-context")]
    [InlineData("blank-context")]
    [InlineData("missing-reference")]
    [InlineData("zero-reference")]
    [InlineData("negative-reference")]
    [InlineData("fractional-reference")]
    [InlineData("overflow-reference")]
    [InlineData("missing-name")]
    [InlineData("null-name")]
    [InlineData("missing-value")]
    [InlineData("non-string-value")]
    [InlineData("extra")]
    public async Task SetVariable_InvalidInput_IsRejectedBeforeDapIo(string scenario)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(SetVariableOptions(true));
        var session = await StartStoppedSessionAsync(driver, "set-invalid-start");
        var context = (await InspectionStackAsync(driver, session, "set-invalid-stack"))["inspectionContextId"]!.GetValue<string>();
        var arguments = SetVariableArguments(session, context, 1001, "x", "1");
        switch (scenario)
        {
            case "missing-session": arguments.Remove("debugSessionId"); break;
            case "blank-session": arguments["debugSessionId"] = " \t"; break;
            case "missing-context": arguments.Remove("inspectionContextId"); break;
            case "blank-context": arguments["inspectionContextId"] = " \t"; break;
            case "missing-reference": arguments.Remove("variablesReference"); break;
            case "zero-reference": arguments["variablesReference"] = 0; break;
            case "negative-reference": arguments["variablesReference"] = -1; break;
            case "fractional-reference": arguments["variablesReference"] = 1.5; break;
            case "overflow-reference": arguments["variablesReference"] = (long)int.MaxValue + 1; break;
            case "missing-name": arguments.Remove("name"); break;
            case "null-name": arguments["name"] = null; break;
            case "missing-value": arguments.Remove("value"); break;
            case "non-string-value": arguments["value"] = 42; break;
            case "extra": arguments["unexpected"] = true; break;
            default: throw new ArgumentOutOfRangeException(nameof(scenario));
        }
        var before = await driver.ReadNativeActionsAsync();
        var result = await SetVariableCallAsync(driver, arguments, "set-invalid", true);
        Assert.Equal("invalid_tool_arguments", result["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", result["error"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(result);
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData(null, "none", false)]
    [InlineData(false, "none", false)]
    [InlineData(true, "none", true)]
    [InlineData(false, "enable-on-threads", true)]
    [InlineData(true, "disable-on-threads", false)]
    public async Task SetVariable_CapabilityBaselineAndReaderOrderedDelta_ControlAdmission(bool? baseline, string delta, bool supported)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(SetVariableOptions(baseline, delta), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "set-cap-start");
        var context = (await InspectionStackAsync(driver, session, "set-cap-stack"))["inspectionContextId"]!.GetValue<string>();
        if (delta != "none")
        {
            var barrier = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
                    ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-cap-barrier")), isError: false);
            Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
        }
        var before = await driver.ReadNativeActionsAsync();
        var result = await SetVariableCallAsync(driver, SetVariableArguments(session, context, 1001, "x", "1"), "set-cap", !supported);
        if (supported)
        {
            Assert.Equal("set_variable_success", result["kind"]?.GetValue<string>());
            AssertSetVariableRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1001, "x", "1");
        }
        else
        {
            AssertSetVariableFailure(result, "unsupported");
            Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        }
    }

    [Fact]
    public async Task SetVariable_UnknownContext_IsRefusedWithoutWrite()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(SetVariableOptions(true), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "set-unknown-start");
        var before = await driver.ReadNativeActionsAsync();
        AssertSetVariableFailure(await SetVariableCallAsync(driver, SetVariableArguments(session, "unknown-context", 1001, "x", "1"), "set-unknown", true), "refused");
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task SetVariable_ConcurrentOldContextRead_RefusesReplyAfterMutation()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(SupportsDelayedStackTraceLoading: true,
                LifecycleMode: "all-stop", SupportsSetVariable: true, VariablesResponseMode: "hold")));
        var session = await StartStoppedSessionAsync(driver, "set-race-start");
        var context = (await InspectionStackAsync(driver, session, "set-race-stack"))["inspectionContextId"]!.GetValue<string>();
        var before = await driver.ReadNativeActionsAsync();
        var read = driver.CallToolRawAsync("get_variables", InspectionArguments(session, context, "variablesReference", 1001),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-race-read"), timeout: TimeSpan.FromSeconds(35));
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await driver.WaitForFixtureRecordAsync("inspection-response-gated", observation.Token);
            Assert.False(read.IsCompleted);
            var updated = await SetVariableCallAsync(driver, SetVariableArguments(session, context, 1001, "x", "1"), "set-race-write");
            Assert.Equal("set_variable_success", updated["kind"]?.GetValue<string>());
            var fresh = Assert.IsType<string>(updated["inspectionContextId"]?.GetValue<string>());
            Assert.NotEqual(context, fresh);
            Assert.False(read.IsCompleted);
            driver.ReleaseStackTraceResponse();
            var stale = AssertApplicationEnvelope(await read, isError: true);
            AssertInspectionFailure(stale, "get_variables", protocolFailure: false);
            Assert.False(stale.ContainsKey("variables"));
            var nested = await InspectionCallAsync(driver, "get_variables",
                InspectionArguments(session, fresh, "variablesReference", 1002), "set-race-fresh");
            Assert.Equal("variables_success", nested["kind"]?.GetValue<string>());
            Assert.Equal(fresh, nested["inspectionContextId"]?.GetValue<string>());
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before.Count).ToArray();
            Assert.Equal(["variables", "setVariable", "variables"], actions.Select(static action => action.Command));
            AssertSetVariableRequest(actions, 1001, "x", "1");
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
        }
    }

    [Theory]
    [InlineData("fixture:negative", "failed", false)]
    [InlineData("fixture:malformed", "outcome_unknown", true)]
    [InlineData("fixture:oversized", "outcome_unknown", true)]
    [InlineData("fixture:wrong-command", "outcome_unknown", true)]
    [InlineData("fixture:timeout", "outcome_unknown", true)]
    public async Task SetVariable_NegativeOrInvalidReply_InvalidatesContextAndNeverRetries(string name, string outcome, bool drained)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(SetVariableOptions(true), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "set-failure-start");
        var context = (await InspectionStackAsync(driver, session, "set-failure-stack"))["inspectionContextId"]!.GetValue<string>();
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync("set_variable", SetVariableArguments(session, context, 1001, name, "1"),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-failure"), timeout: TimeSpan.FromSeconds(35));
        AssertSetVariableFailure(AssertApplicationEnvelope(response, isError: true), outcome);
        AssertSetVariableRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1001, name, "1");
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-failure-state")), isError: drained);
        if (drained)
        {
            AssertNotFound(state);
        }
        else
        {
            Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
            var stale = await InspectionCallAsync(driver, "get_variables", InspectionArguments(session, context, "variablesReference", 1001), "set-failure-old", true);
            AssertInspectionFailure(stale, "get_variables", protocolFailure: false);
        }
    }

    [Fact]
    public async Task SetVariable_ReplyAfterInvalidation_ReturnsUnknownAndDrains()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(SetVariableOptions(true));
        var session = await StartStoppedSessionAsync(driver, "set-held-start");
        var context = (await InspectionStackAsync(driver, session, "set-held-stack"))["inspectionContextId"]!.GetValue<string>();
        var before = await driver.ReadNativeActionsAsync();
        var operation = driver.CallToolRawAsync("set_variable", SetVariableArguments(session, context, 1001, "fixture:hold-invalidated", "1"),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-held"), timeout: TimeSpan.FromSeconds(35));
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await driver.WaitForFixtureRecordAsync("set-variable-response-gated", observation.Token);
            var barrier = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
                    ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-held-barrier"), observation.Token), isError: false);
            Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
            Assert.False(operation.IsCompleted);
        }
        finally { driver.ReleaseStackTraceResponse(); }
        AssertSetVariableFailure(AssertApplicationEnvelope(await operation, isError: true), "outcome_unknown");
        AssertSetVariableRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1001, "fixture:hold-invalidated", "1");
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-held-state")), isError: true));
    }

    [Fact]
    public async Task SetVariable_PostWriteCancellation_DrainsWithoutRetry()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(SetVariableOptions(true));
        var session = await StartStoppedSessionAsync(driver, "set-cancel-start");
        var context = (await InspectionStackAsync(driver, session, "set-cancel-stack"))["inspectionContextId"]!.GetValue<string>();
        using var adapter = System.Diagnostics.Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        using var cancellation = new CancellationTokenSource();
        var before = await driver.ReadNativeActionsAsync();
        var operation = driver.CallToolRawAsync("set_variable", SetVariableArguments(session, context, 1001, "fixture:hold", "1"),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-cancel"), cancellation.Token, timeout: TimeSpan.FromSeconds(35));
        using (var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
        {
            await driver.WaitForFixtureRecordAsync("set-variable-response-gated", observation.Token);
            Assert.False(operation.IsCompleted);
        }
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
        using var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureRecordAsync("disconnect-response", cleanup.Token);
        await adapter.WaitForExitAsync(cleanup.Token);
        AssertSetVariableRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1001, "fixture:hold", "1");
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("set-cancel-state"), cleanup.Token), isError: true));
    }

    private static ModernMcpStartOptions SetVariableOptions(bool? supported, string delta = "none") => new(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(SupportsDelayedStackTraceLoading: true, LifecycleMode: "all-stop",
                    SupportsSetVariable: supported, SetVariableCapabilityDelta: delta));

    private static JsonObject SetVariableArguments(string session, string context, int reference, string name, string value) => new()
    {
        ["debugSessionId"] = session,
        ["inspectionContextId"] = context,
        ["variablesReference"] = reference,
        ["name"] = name,
        ["value"] = value,
    };

    private static async Task<JsonObject> SetVariableCallAsync(ModernMcpProcessDriver driver, JsonObject arguments, string id, bool isError = false) =>
            AssertApplicationEnvelope(await driver.CallToolRawAsync("set_variable", arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId(id)), isError);

    private static void AssertSetVariableFailure(JsonObject content, string outcome)
    {
        Assert.Equal($"DAP_SET_VARIABLE_{outcome.ToUpperInvariant()}", content["error"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        Assert.DoesNotContain("private", content.ToJsonString(), StringComparison.OrdinalIgnoreCase);
    }

    private static void AssertSetVariableRequest(IEnumerable<ModernNativeAction> actions, int reference, string name, string value)
    {
        var request = Assert.Single(actions, static action => action.Command == "setVariable");
        var arguments = JsonNode.Parse(request.Detail!)!["arguments"];
        Assert.True(JsonNode.DeepEquals(new JsonObject { ["variablesReference"] = reference, ["name"] = name, ["value"] = value }, arguments));
    }

    [Fact]
    public async Task ToolsList_EvaluateExpression_PublishesExplicitClosedInputWithoutReadOnlyOrIdempotentClaims()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var before = await driver.ReadNativeActionsAsync();
        var result = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-catalog")));
        var tool = Assert.Single(Assert.IsType<JsonArray>(result["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == EvaluateTool);
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        var fields = new[] { "debugSessionId", "expression", "frameId", "inspectionContextId" };
        Assert.Equal(fields, Assert.IsType<JsonArray>(schema["required"]).Select(static item => item!.GetValue<string>()).Order(StringComparer.Ordinal));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(fields, properties.Select(static item => item.Key).Order(StringComparer.Ordinal));
        foreach (var field in new[] { "debugSessionId", "inspectionContextId", "expression" })
        {
            Assert.Equal("string", properties[field]?["type"]?.GetValue<string>());
        }
        Assert.Equal("integer", properties["frameId"]?["type"]?.GetValue<string>());
        Assert.False(tool["annotations"]?["readOnlyHint"]?.GetValue<bool>() ?? false);
        Assert.False(tool["annotations"]?["idempotentHint"]?.GetValue<bool>() ?? false);
        Assert.True(tool["annotations"]?["destructiveHint"]?.GetValue<bool>() ?? true);
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("6 * 7", "42", "int")]
    [InlineData(" \t6 * 7\r\n", "42", "int")]
    [InlineData("fixture:missing-type", "untyped", null)]
    [InlineData("fixture:null-type", "untyped", null)]
    [InlineData("fixture:empty-result", "", "string")]
    public async Task EvaluateExpression_CurrentContext_ForwardsExactWatchRequestAndNormalizesResult(string expression, string expectedResult, string? expectedType)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-exact-start");
        var stack = await InspectionStackAsync(driver, session, "evaluate-exact-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        // The stopped epoch is not a capability proving that a numeric frame was previously issued.
        const int frame = 17;
        var before = await driver.ReadNativeActionsAsync();

        var content = await InspectionCallAsync(driver, EvaluateTool, EvaluationArguments(session, context, frame, expression), "evaluate-exact");

        Assert.True(JsonNode.DeepEquals(new JsonObject
        {
            ["kind"] = "evaluate_success",
            ["inspectionContextId"] = context,
            ["result"] = expectedResult,
            ["type"] = expectedType,
            ["variablesReference"] = 0,
        }, content));
        AssertSingleEvaluateRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), frame, expression);
    }

    [Fact]
    public async Task EvaluateExpression_StructuredResult_ExpandsNestedReferenceInSameInspectionContext()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-nested-start");
        var stack = await InspectionStackAsync(driver, session, "evaluate-nested-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var frame = Assert.IsType<JsonArray>(stack["frames"])[0]!["id"]!.GetValue<int>();
        var before = await driver.ReadNativeActionsAsync();

        var content = await InspectionCallAsync(driver, EvaluateTool, EvaluationArguments(session, context, frame, "tupleVar"), "evaluate-nested");

        Assert.True(JsonNode.DeepEquals(new JsonObject
        {
            ["kind"] = "evaluate_success",
            ["inspectionContextId"] = context,
            ["result"] = "(\"test\", 123)",
            ["type"] = "(string, int)",
            ["variablesReference"] = 1002,
        }, content));
        var nested = await InspectionCallAsync(driver, "get_variables",
            InspectionArguments(session, context, "variablesReference", content["variablesReference"]!.GetValue<int>()), "evaluate-nested-variables");
        Assert.Equal("variables_success", nested["kind"]?.GetValue<string>());
        Assert.Equal(context, nested["inspectionContextId"]?.GetValue<string>());
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("[{\"name\":\"Item1\",\"value\":\"\\\"test\\\"\",\"type\":\"string\",\"variablesReference\":0},{\"name\":\"Item2\",\"value\":\"123\",\"type\":\"int\",\"variablesReference\":0}]"), nested["variables"]));
        var actions = (await driver.ReadNativeActionsAsync()).Skip(before.Count).ToArray();
        Assert.Equal(["evaluate", "variables"], actions.Select(static action => action.Command));
        AssertSingleEvaluateRequest(actions, frame, "tupleVar");
        Assert.True(JsonNode.DeepEquals(new JsonObject { ["variablesReference"] = 1002 }, JsonNode.Parse(actions[1].Detail!)!["arguments"]));
    }

    [Theory]
    [InlineData("missing-session")]
    [InlineData("missing-context")]
    [InlineData("missing-frame")]
    [InlineData("missing-expression")]
    [InlineData("null-context")]
    [InlineData("empty-context")]
    [InlineData("fractional-frame")]
    [InlineData("overflow-frame")]
    [InlineData("null-expression")]
    [InlineData("non-string-expression")]
    [InlineData("extra-field")]
    public async Task EvaluateExpression_InvalidInput_ReturnsInvalidArgumentsWithoutDapIo(string scenario)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, $"evaluate-invalid-{scenario}-start");
        var stack = await InspectionStackAsync(driver, session, $"evaluate-invalid-{scenario}-stack");
        var arguments = EvaluationArguments(session, stack["inspectionContextId"]!.GetValue<string>(), 1, "6 * 7");
        switch (scenario)
        {
            case "missing-session": arguments.Remove("debugSessionId"); break;
            case "missing-context": arguments.Remove("inspectionContextId"); break;
            case "missing-frame": arguments.Remove("frameId"); break;
            case "missing-expression": arguments.Remove("expression"); break;
            case "null-context": arguments["inspectionContextId"] = null; break;
            case "empty-context": arguments["inspectionContextId"] = string.Empty; break;
            case "fractional-frame": arguments["frameId"] = 1.5; break;
            case "overflow-frame": arguments["frameId"] = (long)int.MaxValue + 1; break;
            case "null-expression": arguments["expression"] = null; break;
            case "non-string-expression": arguments["expression"] = 42; break;
            case "extra-field": arguments["context"] = "repl"; break;
            default: throw new ArgumentOutOfRangeException(nameof(scenario));
        }
        var before = await driver.ReadNativeActionsAsync();

        var invalid = await InspectionCallAsync(driver, EvaluateTool, arguments, $"evaluate-invalid-{scenario}", isError: true);

        Assert.Equal("invalid_tool_arguments", invalid["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", invalid["error"]?.GetValue<string>());
        Assert.Equal(EvaluateTool, invalid["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task EvaluateExpression_UnknownContext_RefusesBeforeAnyDapWriteAndKeepsSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-unknown-context-start");
        var before = await driver.ReadNativeActionsAsync();

        var refused = await InspectionCallAsync(driver, EvaluateTool,
            EvaluationArguments(session, "unknown-inspection-context", 1, "6 * 7"), "evaluate-unknown-context", isError: true);

        AssertEvaluationFailure(refused, "refused");
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state",
            new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-unknown-context-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task EvaluateExpression_StaleContextAfterStep_RefusesWithoutWriteAndFreshContextWorks()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-stale-start");
        var stack = await InspectionStackAsync(driver, session, "evaluate-stale-old-stack");
        var oldContext = stack["inspectionContextId"]!.GetValue<string>();
        var frame = Assert.IsType<JsonArray>(stack["frames"])[0]!["id"]!.GetValue<int>();
        var step = AssertApplicationEnvelope(await driver.CallToolRawAsync("step_over", Arguments(session, 1),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-stale-step")), isError: false);
        Assert.Equal("step_over_success", step["kind"]?.GetValue<string>());
        var freshStack = await InspectionStackAsync(driver, session, "evaluate-stale-fresh-stack");
        var context = freshStack["inspectionContextId"]!.GetValue<string>();
        Assert.NotEqual(oldContext, context);
        Assert.Equal(frame, Assert.IsType<JsonArray>(freshStack["frames"])[0]!["id"]!.GetValue<int>());
        var before = await driver.ReadNativeActionsAsync();

        AssertEvaluationFailure(await InspectionCallAsync(driver, EvaluateTool,
            EvaluationArguments(session, oldContext, frame, "6 * 7"), "evaluate-stale-old", isError: true), "refused");

        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        var fresh = await InspectionCallAsync(driver, EvaluateTool, EvaluationArguments(session, context, frame, "6 * 7"), "evaluate-stale-fresh");
        Assert.Equal("evaluate_success", fresh["kind"]?.GetValue<string>());
        Assert.Equal("42", fresh["result"]?.GetValue<string>());
        Assert.Equal(context, fresh["inspectionContextId"]?.GetValue<string>());
        AssertSingleEvaluateRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), frame, "6 * 7");
    }

    [Fact]
    public async Task EvaluateExpression_NegativeDapResponse_ReturnsFailedNotRefusedAndDoesNotRetry()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-negative-start");
        var stack = await InspectionStackAsync(driver, session, "evaluate-negative-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var before = await driver.ReadNativeActionsAsync();

        var failure = await InspectionCallAsync(driver, EvaluateTool,
            EvaluationArguments(session, context, 1, "fixture:negative"), "evaluate-negative", isError: true);

        AssertEvaluationFailure(failure, "failed");
        AssertSingleEvaluateRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1, "fixture:negative");
        Assert.Equal("debug_state_success", AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state",
            new JsonObject { ["debugSessionId"] = session }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-negative-state")), isError: false)["kind"]?.GetValue<string>());
    }

    [Theory]
    [InlineData("fixture:hold-then-invalidated")]
    [InlineData("fixture:hold-then-continued-restop")]
    public async Task EvaluateExpression_ResponseAfterContextInvalidation_ReturnsRedactedUnknownOutcomeWithoutRetry(string expression)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-held-start");
        var stack = await InspectionStackAsync(driver, session, "evaluate-held-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var before = await driver.ReadNativeActionsAsync();
        var response = driver.CallToolRawAsync(EvaluateTool, EvaluationArguments(session, context, 1, expression),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-held"), timeout: TimeSpan.FromSeconds(35));
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", observation.Token);
            var barrier = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-held-reader-barrier"), observation.Token), isError: false);
            Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
            Assert.False(response.IsCompleted, "The evaluation reply must remain held until the invalidating event has passed the reader barrier.");
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
        }

        AssertEvaluationFailure(AssertApplicationEnvelope(await response, isError: true), "outcome_unknown");
        AssertSingleEvaluateRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1, expression);
    }

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    public async Task EvaluateExpression_SerializedByteBoundary_IncludesEscapingAndDrainsOnlyAboveLimit(int excessBytes)
    {
        const int limit = 256 * 1024;
        const string suffix = "\"\nЖ";
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, $"evaluate-boundary-{excessBytes}-start");
        var stack = await InspectionStackAsync(driver, session, $"evaluate-boundary-{excessBytes}-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var template = new JsonObject
        {
            ["kind"] = "evaluate_success",
            ["inspectionContextId"] = context,
            ["result"] = suffix,
            ["type"] = "string",
            ["variablesReference"] = 0,
        };
        var count = limit - Encoding.UTF8.GetByteCount(template.ToJsonString()) + excessBytes;
        var expression = $"fixture:sized:{count}";
        var before = await driver.ReadNativeActionsAsync();
        var response = await driver.CallToolRawAsync(EvaluateTool, EvaluationArguments(session, context, 1, expression),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"evaluate-boundary-{excessBytes}"), timeout: TimeSpan.FromSeconds(35));
        var content = AssertApplicationEnvelope(response, isError: excessBytes != 0);
        AssertSingleEvaluateRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1, expression);
        if (excessBytes == 0)
        {
            Assert.Equal("evaluate_success", content["kind"]?.GetValue<string>());
            Assert.Equal(new string('x', count) + suffix, content["result"]?.GetValue<string>());
            Assert.Equal(limit, Encoding.UTF8.GetByteCount(content.ToJsonString()));
        }
        else
        {
            AssertEvaluationFailure(content, "outcome_unknown");
            AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-boundary-state")), isError: true));
        }
    }

    [Theory]
    [InlineData("fixture:missing-result")]
    [InlineData("fixture:non-string-result")]
    [InlineData("fixture:non-string-type")]
    [InlineData("fixture:missing-reference")]
    [InlineData("fixture:negative-reference")]
    [InlineData("fixture:fractional-reference")]
    [InlineData("fixture:oversized-result")]
    [InlineData("fixture:wrong-command")]
    [InlineData("fixture:timeout")]
    public async Task EvaluateExpression_PostWriteProtocolFailure_ReturnsUnknownOutcomeAndDrainsWithoutRetry(string expression)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-protocol-start");
        var stack = await InspectionStackAsync(driver, session, "evaluate-protocol-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        var before = await driver.ReadNativeActionsAsync();

        var response = await driver.CallToolRawAsync(EvaluateTool, EvaluationArguments(session, context, 1, expression),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-protocol"), timeout: TimeSpan.FromSeconds(35));

        AssertEvaluationFailure(AssertApplicationEnvelope(response, isError: true), "outcome_unknown");
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-protocol-state")), isError: true));
        var actions = (await driver.ReadNativeActionsAsync()).Skip(before.Count).ToArray();
        AssertSingleEvaluateRequest(actions, 1, expression);
        Assert.Single(actions, static action => action.Command == "disconnect");
    }

    [Fact]
    public async Task EvaluateExpression_CommittedWriteCancellation_DrainsSessionAndInvalidatesInspectionContext()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-cancel-start");
        var stack = await InspectionStackAsync(driver, session, "evaluate-cancel-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        using var adapter = System.Diagnostics.Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        using var cancellation = new CancellationTokenSource();
        var before = await driver.ReadNativeActionsAsync();
        var operation = driver.CallToolRawAsync(EvaluateTool, EvaluationArguments(session, context, 1, "fixture:hold"),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-cancel"), cancellation.Token, timeout: TimeSpan.FromSeconds(35));
        using (var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5)))
        {
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", observation.Token);
            var barrier = AssertApplicationEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-cancel-reader-barrier"), observation.Token), isError: false);
            Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
            Assert.False(operation.IsCompleted, "The adapter consumed the evaluate request, but its reply must still be held before cancellation.");
        }

        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
        using var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureRecordAsync("disconnect-response", cleanup.Token);
        await adapter.WaitForExitAsync(cleanup.Token);
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-cancel-state"), cleanup.Token), isError: true));
        var beforeReuse = await driver.ReadNativeActionsAsync();
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(EvaluateTool, EvaluationArguments(session, context, 1, "6 * 7"),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-cancel-reuse"), cleanup.Token), isError: true));
        Assert.Equal(beforeReuse.Count, (await driver.ReadNativeActionsAsync()).Count);
        var actions = (await driver.ReadNativeActionsAsync()).Skip(before.Count).ToArray();
        AssertSingleEvaluateRequest(actions, 1, "fixture:hold");
        Assert.Single(actions, static action => action.Command == "terminate");
        Assert.Single(actions, static action => action.Command == "disconnect");
    }

    [Fact]
    public async Task EvaluateExpression_ExplicitStopAbortsHeldEvaluationAndRetiresSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(CallStack(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartStoppedSessionAsync(driver, "evaluate-stop-start");
        var stack = await InspectionStackAsync(driver, session, "evaluate-stop-stack");
        var context = stack["inspectionContextId"]!.GetValue<string>();
        using var adapter = System.Diagnostics.Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        var before = await driver.ReadNativeActionsAsync();
        var evaluation = driver.CallToolRawAsync(EvaluateTool, EvaluationArguments(session, context, 1, "fixture:hold"),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-stop-held"), timeout: TimeSpan.FromSeconds(35));
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", observation.Token);
            Assert.False(evaluation.IsCompleted);
            var stop = driver.CallToolRawAsync("stop_debug", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-stop"), timeout: TimeSpan.FromSeconds(15));

            var outcome = AssertApplicationEnvelope(await evaluation.WaitAsync(observation.Token), isError: true);
            AssertEvaluationFailure(outcome, "outcome_unknown");
            Assert.Equal("complete", ModernMcpProcessDriver.RequireResult(await stop.WaitAsync(observation.Token))["resultType"]?.GetValue<string>());
            await adapter.WaitForExitAsync(observation.Token);
            AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("evaluate-stop-state"), observation.Token), isError: true));
            AssertSingleEvaluateRequest((await driver.ReadNativeActionsAsync()).Skip(before.Count), 1, "fixture:hold");
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
        }
    }

    private static JsonObject EvaluationArguments(string session, string context, int frame, string expression)
    {
        var arguments = InspectionArguments(session, context, "frameId", frame);
        arguments["expression"] = expression;
        return arguments;
    }

    private static void AssertSingleEvaluateRequest(IEnumerable<ModernNativeAction> actions, int frame, string expression)
    {
        var request = Assert.Single(actions, static action => action.Command == "evaluate");
        var arguments = JsonNode.Parse(request.Detail!)!["arguments"];
        Assert.True(JsonNode.DeepEquals(new JsonObject { ["expression"] = expression, ["frameId"] = frame, ["context"] = "watch" }, arguments));
    }

    private static void AssertEvaluationFailure(JsonObject content, string outcome)
    {
        Assert.Equal($"dap_evaluate_{outcome}", content["kind"]?.GetValue<string>());
        Assert.Equal($"DAP_EVALUATE_{outcome.ToUpperInvariant()}", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.DoesNotContain("private", content.ToJsonString(), StringComparison.OrdinalIgnoreCase);
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
    }

    private static ModernMcpStartOptions InspectionOptions(string tool, string mode) => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(
            SupportsDelayedStackTraceLoading: true,
            LifecycleMode: "all-stop",
            ScopesResponseMode: tool == "get_scopes" ? mode : "success",
            VariablesResponseMode: tool == "get_variables" ? mode : "success"));

    private static JsonObject InspectionArguments(string session, string context, string field, int id) => new()
    {
        ["debugSessionId"] = session,
        ["inspectionContextId"] = context,
        [field] = id,
    };

    private static async Task<JsonObject> InspectionStackAsync(ModernMcpProcessDriver driver, string session, string requestId)
    {
        var arguments = Arguments(session, 1);
        arguments["includeInspectionContext"] = true;
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(Tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: false);
        Assert.Equal("call_stack_success", content["kind"]?.GetValue<string>());
        Assert.False(string.IsNullOrWhiteSpace(content["inspectionContextId"]?.GetValue<string>()));
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        return content;
    }

    private static async Task<JsonObject> InspectionCallAsync(ModernMcpProcessDriver driver, string tool, JsonObject arguments, string requestId, bool isError = false)
    {
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError);
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        return content;
    }

    private static void AssertInspectionFailure(JsonObject content, string tool, bool protocolFailure)
    {
        var operation = tool == "get_scopes" ? "scopes" : "variables";
        var outcome = protocolFailure ? "protocol_error" : "refused";
        Assert.Equal($"dap_{operation}_{outcome}", content["kind"]?.GetValue<string>());
        Assert.Equal($"DAP_{operation.ToUpperInvariant()}_{outcome.ToUpperInvariant()}", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.DoesNotContain("private", content.ToJsonString(), StringComparison.OrdinalIgnoreCase);
    }

    private static ModernMcpStartOptions CallStack(string responseMode = "success", bool supportsDelayedStackTraceLoading = true, bool omitDelayedStackTraceLoading = false, string lifecycleMode = "all-stop") => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(
            SupportsDelayedStackTraceLoading: supportsDelayedStackTraceLoading,
            OmitDelayedStackTraceLoading: omitDelayedStackTraceLoading,
            LifecycleMode: lifecycleMode,
            StackTraceResponseMode: responseMode));

    private static async Task<string> StartStoppedSessionAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var start = AssertApplicationEnvelope(await driver.CallToolRawAsync("start_debug", new JsonObject { ["program"] = driver.InertProgramPath }, ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: false);
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

    private static void AssertSingleUnpagedStackTrace(IEnumerable<ModernNativeAction> actions, int threadId)
    {
        var stackTrace = Assert.Single(actions, static action => action.Command == "stackTrace");
        using var request = JsonDocument.Parse(stackTrace.Detail ?? throw new Xunit.Sdk.XunitException("Controlled adapter did not record stackTrace arguments."));
        var arguments = request.RootElement.GetProperty("arguments");
        Assert.Equal(JsonValueKind.Object, arguments.ValueKind);
        Assert.Single(arguments.EnumerateObject());
        Assert.Equal(threadId, arguments.GetProperty("threadId").GetInt32());
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
