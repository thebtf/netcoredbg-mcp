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
