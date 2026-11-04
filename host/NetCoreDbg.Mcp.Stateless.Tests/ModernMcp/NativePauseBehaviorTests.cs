using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativePauseBehaviorTests
{
    private const string Tool = "pause_execution";
    private static readonly TimeSpan OperationTimeout = TimeSpan.FromSeconds(40);
    private static readonly TimeSpan ObservationTimeout = TimeSpan.FromSeconds(5);

    [Theory]
    [InlineData(int.MinValue)]
    [InlineData(-1)]
    [InlineData(7)]
    [InlineData(int.MaxValue)]
    public async Task Pause_ExplicitNonzeroTarget_ForwardsOnlyExactPauseWithoutDiscovery(int threadId)
    {
        await using var driver = await StartDriverAsync();
        var session = await StartSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, threadId)), false), threadId);

        await AssertWritesAsync(driver, before, threadId, "pause");
    }

    [Theory]
    [InlineData("omitted", "all-stop", 1)]
    [InlineData("null", "all-stop", 1)]
    [InlineData("zero", "all-stop", 1)]
    [InlineData("omitted", "all-stop-zero", 0)]
    [InlineData("null", "all-stop-zero", 0)]
    [InlineData("zero", "all-stop-zero", 0)]
    public async Task Pause_DefaultInput_UsesObservedCurrentIncludingZeroWithoutDiscovery(
        string input, string lifecycleMode, int selectedThreadId)
    {
        await using var driver = await StartDriverAsync(lifecycleMode: lifecycleMode);
        var session = await StartSessionAsync(driver, stopped: true);
        var initial = await StateAsync(driver, session);
        Assert.Equal(selectedThreadId, initial["currentThreadId"]?.GetValue<int>());
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertAcknowledged(Envelope(await CallAsync(driver, Tool, DefaultArguments(session, input)), false), selectedThreadId);

        await AssertWritesAsync(driver, before, selectedThreadId, "pause");
        Assert.True(JsonNode.DeepEquals(initial, await StateAsync(driver, session)),
            "Admission and ACK must not rewrite the previously observed stopped event.");
    }

    [Theory]
    [InlineData("omitted", "first-seven", 7)]
    [InlineData("null", "first-seven", 7)]
    [InlineData("zero", "first-seven", 7)]
    [InlineData("omitted", "first-zero", 0)]
    [InlineData("null", "first-zero", 0)]
    [InlineData("zero", "first-zero", 0)]
    public async Task Pause_WithoutCurrent_UsesFirstUnsortedDiscoveredIdIncludingZero(
        string input, string threadsMode, int selectedThreadId)
    {
        await using var driver = await StartDriverAsync(threadsMode: threadsMode);
        var session = await StartSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertAcknowledged(Envelope(await CallAsync(driver, Tool, DefaultArguments(session, input)), false), selectedThreadId);

        await AssertWritesAsync(driver, before, selectedThreadId, "threads", "pause");
    }

    [Fact]
    public async Task Pause_EmptyDiscovery_ReturnsFixedNoThreadsWithoutPauseOrRetry()
    {
        await using var driver = await StartDriverAsync(threadsMode: "empty");
        var session = await StartSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session)), true),
            "dap_pause_no_threads", "DAP_PAUSE_NO_THREADS");

        Assert.Equal(new[] { "threads" }, (await driver.ReadNativeActionsAsync()).Skip(before).Select(static entry => entry.Command));
        _ = await StateAsync(driver, session);
    }

    [Fact]
    public async Task Pause_CurrentArrivingDuringDiscovery_RechecksReaderStateBeforeSelectingTarget()
    {
        await using var driver = await StartDriverAsync(threadsMode: "discovery-current-before-reply");
        var session = await StartSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session)), false), 9);

        await AssertWritesAsync(driver, before, 9, "threads", "pause");
        var state = await StateAsync(driver, session);
        Assert.Equal("stopped", state["event"]?.GetValue<string>());
        Assert.Equal(9, state["currentThreadId"]?.GetValue<int>());
    }

    [Theory]
    [InlineData("refused", "dap_pause_refused", "DAP_PAUSE_REFUSED", false)]
    [InlineData("malformed-body", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    [InlineData("wrong-command", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    [InlineData("wrong-request-sequence", "dap_pause_timeout", "DAP_PAUSE_TIMEOUT", false)]
    [InlineData("eof", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    [InlineData("too-many-threads", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    [InlineData("too-many-name-bytes", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    public async Task Pause_FailedDiscovery_ReturnsFixedOutcomeWithoutRetryOrPause(
        string threadsMode, string kind, string error, bool drains)
    {
        await using var driver = await StartDriverAsync(threadsMode: threadsMode);
        var session = await StartSessionAsync(driver);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session)), true), kind, error);

        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
        Assert.Single(writes, static entry => entry.Command == "threads");
        Assert.DoesNotContain(writes, static entry => entry.Command == "pause");
        if (drains)
        {
            await AssertRetiredAsync(driver, session);
            var afterDrain = (await driver.ReadNativeActionsAsync()).Count;
            AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session)), true),
                "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
            Assert.Equal(afterDrain, (await driver.ReadNativeActionsAsync()).Count);
        }
        else
        {
            _ = await StateAsync(driver, session);
            var beforeRecovery = (await driver.ReadNativeActionsAsync()).Count;
            AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, 11)), false), 11);
            await AssertWritesAsync(driver, beforeRecovery, 11, "pause");
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static entry => entry.Command is "terminate" or "disconnect");
        }
    }

    [Fact]
    public async Task Pause_CommittedDiscoveryTimeout_PreservesSessionAndReservationAndIgnoresLateReplyWithoutReplay()
    {
        await using var driver = await StartDriverAsync(threadsMode: "hold-independent");
        var session = await StartSessionAsync(driver);
        var initialState = await StateAsync(driver, session);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var pause = CallAsync(driver, Tool, Arguments(session));
        int? discoverySequence = null;
        int? publicSequence = null;
        try
        {
            await RequestCountAsync(driver, "threads", 1);
            discoverySequence = Assert.IsType<int>(Assert.Single(await driver.ReadFixtureTranscriptAsync(),
                static entry => entry.Kind == "request" && entry.Command == "threads").Sequence);

            AssertFixedError(Envelope(await pause, true), "dap_pause_timeout", "DAP_PAUSE_TIMEOUT");

            Assert.Equal(new[] { "threads" }, (await driver.ReadNativeActionsAsync()).Skip(before).Select(static entry => entry.Command));
            Assert.True(JsonNode.DeepEquals(initialState, await StateAsync(driver, session)));
            // A new acknowledged pause proves that the abandoned discovery released its reservation.
            AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, 11)), false), 11);
            var threads = CallAsync(driver, "get_threads", Arguments(session));
            await RequestCountAsync(driver, "threads", 2);
            var requests = (await driver.ReadFixtureTranscriptAsync())
                .Where(static entry => entry.Kind == "request" && entry.Command == "threads").ToArray();
            Assert.Equal(2, requests.Length);
            publicSequence = Assert.IsType<int>(requests[1].Sequence);
            Assert.NotEqual(discoverySequence, publicSequence);

            driver.ReleaseThreadsResponse(discoverySequence.Value);
            using (var observation = new CancellationTokenSource(ObservationTimeout))
            {
                await driver.WaitForDapResponseAsync("threads", discoverySequence.Value, observation.Token);
            }
            // The exact late response was written before this ACK, so the reader has consumed both.
            AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, 12)), false), 12);
            Assert.False(threads.IsCompleted, "An abandoned discovery reply must not settle a newer pending threads request.");
            Assert.True(JsonNode.DeepEquals(initialState, await StateAsync(driver, session)));

            driver.ReleaseThreadsResponse(publicSequence.Value);
            var publicResult = Envelope(await threads, false);
            Assert.Equal("threads_success", publicResult["kind"]?.GetValue<string>());
            Assert.Equal(29, Assert.Single(Assert.IsType<JsonArray>(publicResult["threads"]))!["id"]?.GetValue<int>());
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            Assert.Equal(new[] { "threads", "pause", "threads", "pause" }, writes.Select(static entry => entry.Command));
            var pauses = writes.Where(static entry => entry.Command == "pause").ToArray();
            Assert.Equal(2, pauses.Length);
            AssertPauseRequest(pauses[0], 11);
            AssertPauseRequest(pauses[1], 12);
        }
        finally
        {
            if (publicSequence.HasValue) driver.ReleaseThreadsResponse(publicSequence.Value);
            if (discoverySequence.HasValue) driver.ReleaseThreadsResponse(discoverySequence.Value);
        }
    }

    [Fact]
    public async Task Pause_AckWithoutStopped_DoesNotCreateStateOrInspectionAuthority()
    {
        await using var driver = await StartDriverAsync();
        var session = await StartSessionAsync(driver);
        var initial = await StateAsync(driver, session);
        Assert.False(initial.ContainsKey("currentThreadId"));

        AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, 7)), false), 7);

        Assert.True(JsonNode.DeepEquals(initial, await StateAsync(driver, session)));
        var beforeStack = (await driver.ReadNativeActionsAsync()).Count;
        AssertFixedError(Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 7)), true),
            "dap_stack_trace_refused", "DAP_STACK_TRACE_REFUSED");
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeStack), static entry => entry.Command == "stackTrace");
    }

    [Theory]
    [InlineData("ack-body-absent", "absent")]
    [InlineData("ack-body-null", "null")]
    [InlineData("ack-body-object", "object")]
    [InlineData("ack-body-array", "array")]
    [InlineData("ack-body-string", "string")]
    [InlineData("ack-body-boolean", "boolean")]
    [InlineData("ack-body-number", "number")]
    public async Task Pause_LegalResponseBodyShape_IsIgnoredWithoutInventingStoppedStateOrInspectionAuthority(
        string pauseMode, string bodyShape)
    {
        await using var driver = await StartDriverAsync(pauseMode: pauseMode);
        var session = await StartSessionAsync(driver);
        var initial = await StateAsync(driver, session);
        Assert.False(initial.ContainsKey("currentThreadId"));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, 7)), false), 7);

        await RecordAsync(driver, "pause-acknowledged");
        var transcript = await driver.ReadFixtureTranscriptAsync();
        var request = Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "pause");
        var sequence = Assert.IsType<int>(request.Sequence);
        var response = Assert.Single(transcript,
            entry => entry.Kind == "response" && entry.Command == "pause" && entry.Sequence == sequence);
        using var wire = System.Text.Json.JsonDocument.Parse(Assert.IsType<string>(response.RawPayload));
        Assert.Equal(sequence, wire.RootElement.GetProperty("request_seq").GetInt32());
        Assert.Equal("pause", wire.RootElement.GetProperty("command").GetString());
        Assert.True(wire.RootElement.GetProperty("success").GetBoolean());
        var hasBody = wire.RootElement.TryGetProperty("body", out var body);
        Assert.Equal(bodyShape != "absent", hasBody);
        if (hasBody)
        {
            var actualShape = body.ValueKind is System.Text.Json.JsonValueKind.True or System.Text.Json.JsonValueKind.False
                ? "boolean"
                : body.ValueKind.ToString().ToLowerInvariant();
            Assert.Equal(bodyShape, actualShape);
        }
        Assert.True(JsonNode.DeepEquals(initial, await StateAsync(driver, session)),
            "A legal acknowledgement body is not a stopped event and cannot rewrite lifecycle state.");
        AssertFixedError(Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 7)), true),
            "dap_stack_trace_refused", "DAP_STACK_TRACE_REFUSED");
        await AssertWritesAsync(driver, before, 7, "pause");
    }

    [Fact]
    public async Task Pause_AdmissionAndAck_PreserveExistingFrameInspectionContext()
    {
        await using var driver = await StartDriverAsync(pauseMode: "hold", lifecycleMode: "all-stop");
        var session = await StartSessionAsync(driver, stopped: true);
        var stackArguments = Arguments(session, 1);
        stackArguments["includeInspectionContext"] = true;
        var stack = Envelope(await CallAsync(driver, "get_call_stack", stackArguments), false);
        Assert.Equal("call_stack_success", stack["kind"]?.GetValue<string>());
        var context = Assert.IsType<string>(stack["inspectionContextId"]?.GetValue<string>());
        var frame = Assert.IsType<JsonArray>(stack["frames"])[0]!["id"]!.GetValue<int>();
        var initial = await StateAsync(driver, session);
        var pause = CallAsync(driver, Tool, Arguments(session, 1));
        try
        {
            await RecordAsync(driver, "pause-response-gated");
            Assert.False(pause.IsCompleted);
            Assert.True(JsonNode.DeepEquals(initial, await StateAsync(driver, session)));
            await AssertScopesAsync(driver, session, context, frame);

            driver.ReleasePauseResponse();
            AssertAcknowledged(Envelope(await pause, false), 1);

            Assert.True(JsonNode.DeepEquals(initial, await StateAsync(driver, session)));
            await AssertScopesAsync(driver, session, context, frame);
        }
        finally
        {
            driver.ReleasePauseResponse();
        }
    }

    [Theory]
    [InlineData(1, true, 7, true)]
    [InlineData(7, false, 1, false)]
    [InlineData(null, true, 7, true)]
    [InlineData(null, false, 7, false)]
    [InlineData(1, null, 7, false)]
    public async Task Pause_StoppedBeforeAck_UsesActualEventAuthorityNotRequestedTarget(
        int? eventThreadId, bool? allThreadsStopped, int inspectedThreadId, bool canInspect)
    {
        await using var driver = await StartDriverAsync(
            pauseMode: "stopped-before-ack", eventThreadId: eventThreadId, allThreadsStopped: allThreadsStopped);
        var session = await StartSessionAsync(driver);
        var pause = CallAsync(driver, Tool, Arguments(session, 7));
        try
        {
            await RecordAsync(driver, "pause-response-gated");
            await ReaderBarrierAsync(driver, session);
            Assert.False(pause.IsCompleted, "A real stop does not replace the still-held pause acknowledgement.");
            var state = await StateAsync(driver, session);
            Assert.Equal("stopped", state["event"]?.GetValue<string>());
            Assert.Equal(eventThreadId.HasValue, state.ContainsKey("currentThreadId"));
            if (eventThreadId.HasValue)
            {
                Assert.Equal(eventThreadId.Value, state["currentThreadId"]?.GetValue<int>());
            }
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var stack = Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, inspectedThreadId)), !canInspect);
            Assert.Equal(canInspect ? "call_stack_success" : "dap_stack_trace_refused", stack["kind"]?.GetValue<string>());
            Assert.Equal(canInspect ? 1 : 0,
                (await driver.ReadNativeActionsAsync()).Skip(before).Count(static entry => entry.Command == "stackTrace"));

            driver.ReleasePauseResponse();
            AssertAcknowledged(Envelope(await pause, false), 7);
            Assert.True(JsonNode.DeepEquals(state, await StateAsync(driver, session)));
        }
        finally
        {
            driver.ReleasePauseResponse();
        }
    }

    [Fact]
    public async Task Pause_AckBeforeStopped_ReturnsBeforeStopAndLaterPublishesOnlyRealEvent()
    {
        await using var driver = await StartDriverAsync(pauseMode: "ack-before-stopped-held", eventThreadId: 9);
        var session = await StartSessionAsync(driver);
        var initial = await StateAsync(driver, session);
        try
        {
            AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, 7)), false), 7);
            await RecordAsync(driver, "pause-stop-gated");
            Assert.True(JsonNode.DeepEquals(initial, await StateAsync(driver, session)));
            var beforeStack = (await driver.ReadNativeActionsAsync()).Count;
            AssertFixedError(Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 9)), true),
                "dap_stack_trace_refused", "DAP_STACK_TRACE_REFUSED");
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeStack), static entry => entry.Command == "stackTrace");

            driver.ReleasePauseStoppedEvent();
            await RecordAsync(driver, "pause-stopped");
            await ReaderBarrierAsync(driver, session);

            var stopped = await StateAsync(driver, session);
            Assert.Equal("stopped", stopped["event"]?.GetValue<string>());
            Assert.Equal(9, stopped["currentThreadId"]?.GetValue<int>());
            Assert.Equal("call_stack_success", Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 9)), false)["kind"]?.GetValue<string>());
            Assert.Single(await driver.ReadNativeActionsAsync(), static entry => entry.Command == "pause");
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
        }
    }

    [Fact]
    public async Task Pause_StoppedThenContinuedBeforeAck_DoesNotResurrectStopAuthority()
    {
        await using var driver = await StartDriverAsync(pauseMode: "stopped-continued-before-ack");
        var session = await StartSessionAsync(driver);
        var pause = CallAsync(driver, Tool, Arguments(session, 7));
        try
        {
            await RecordAsync(driver, "pause-response-gated");
            await ReaderBarrierAsync(driver, session);
            var continued = await StateAsync(driver, session);
            Assert.Equal("continued", continued["event"]?.GetValue<string>());
            Assert.False(continued.ContainsKey("currentThreadId"));
            Assert.False(pause.IsCompleted);

            driver.ReleasePauseResponse();
            AssertAcknowledged(Envelope(await pause, false), 7);

            Assert.True(JsonNode.DeepEquals(continued, await StateAsync(driver, session)));
            var beforeStack = (await driver.ReadNativeActionsAsync()).Count;
            AssertFixedError(Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 1)), true),
                "dap_stack_trace_refused", "DAP_STACK_TRACE_REFUSED");
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeStack), static entry => entry.Command == "stackTrace");
        }
        finally
        {
            driver.ReleasePauseResponse();
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Pause_BesideCommittedContinue_PreservesContinueRecordAndSharedRealStop(bool continueAckHeld)
    {
        await using var driver = await StartDriverAsync(
            pauseMode: "ack-before-stopped-held", lifecycleMode: "all-stop",
            nextMode: continueAckHeld ? "continue-hold-ack" : "continue-ack-only");
        var session = await StartSessionAsync(driver, stopped: true);
        var continuation = CallAsync(driver, "continue_execution", Arguments(session, 1));
        try
        {
            await RecordAsync(driver, continueAckHeld ? "continue-response-gated" : "continue-acknowledged");
            await ReaderBarrierAsync(driver, session);
            Assert.False(continuation.IsCompleted);
            var before = (await driver.ReadNativeActionsAsync()).Count;

            AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, 1)), false), 1);
            await RecordAsync(driver, "pause-stop-gated");
            Assert.False(continuation.IsCompleted, "Pause ACK alone must not complete the continue waiter.");
            await AssertWritesAsync(driver, before, 1, "pause");

            driver.ReleasePauseStoppedEvent();
            await RecordAsync(driver, "pause-stopped");
            await ReaderBarrierAsync(driver, session);
            if (continueAckHeld)
            {
                Assert.False(continuation.IsCompleted, "The observed pause stop must remain available while the continue ACK is held.");
                driver.ReleaseContinueResponse();
            }
            var content = Envelope(await continuation, false);
            Assert.Equal("continue_stopped", content["kind"]?.GetValue<string>());
            Assert.Equal("stopped", (await StateAsync(driver, session))["event"]?.GetValue<string>());
            Assert.Equal("call_stack_success", Envelope(await CallAsync(driver, "get_call_stack", Arguments(session, 1)), false)["kind"]?.GetValue<string>());
            var writes = await driver.ReadNativeActionsAsync();
            Assert.Single(writes, static entry => entry.Command == "continue");
            Assert.Single(writes, static entry => entry.Command == "pause");
            Assert.DoesNotContain(writes, static entry => entry.Command is "terminate" or "disconnect");
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            driver.ReleaseContinueResponse();
        }
    }

    [Fact]
    public async Task Pause_SeparateReservation_RefusesSecondPauseAndNewExecutionWithoutAdapterWrites()
    {
        await using var driver = await StartDriverAsync(pauseMode: "hold", lifecycleMode: "all-stop");
        var session = await StartSessionAsync(driver, stopped: true);
        var first = CallAsync(driver, Tool, Arguments(session, 1));
        try
        {
            await RecordAsync(driver, "pause-response-gated");
            var before = (await driver.ReadNativeActionsAsync()).Count;

            AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session, 1)), true), "dap_pause_refused", "DAP_PAUSE_REFUSED");
            AssertFixedError(Envelope(await CallAsync(driver, "continue_execution", Arguments(session, 1)), true), "dap_continue_refused", "DAP_CONTINUE_REFUSED");
            AssertFixedError(Envelope(await CallAsync(driver, "step_over", Arguments(session, 1)), true), "dap_step_over_refused", "DAP_STEP_OVER_REFUSED");

            Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
            Assert.False(first.IsCompleted);
            driver.ReleasePauseResponse();
            AssertAcknowledged(Envelope(await first, false), 1);
            AssertAcknowledged(Envelope(await CallAsync(driver, Tool, Arguments(session, 1)), false), 1);
            Assert.Equal(2, (await driver.ReadNativeActionsAsync()).Count(static entry => entry.Command == "pause"));
        }
        finally
        {
            driver.ReleasePauseResponse();
        }
    }

    [Fact]
    public async Task Pause_ActiveStep_RefusesWithoutPauseOrDiscoveryWrite()
    {
        await using var driver = await StartDriverAsync(lifecycleMode: "all-stop", nextMode: "hold");
        var session = await StartSessionAsync(driver, stopped: true);
        var step = CallAsync(driver, "step_over", Arguments(session, 1));
        try
        {
            await RecordAsync(driver, "next-response-gated");
            var before = (await driver.ReadNativeActionsAsync()).Count;

            AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session)), true), "dap_pause_refused", "DAP_PAUSE_REFUSED");

            Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
            driver.ReleaseNextResponse();
            Assert.Equal("step_over_success", Envelope(await step, false)["kind"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task Pause_ConcurrentThreadsRepliesOutOfOrder_CorrelatesEachBodyToItsOwnRequestSequence()
    {
        await using var driver = await StartDriverAsync(threadsMode: "hold-independent");
        var session = await StartSessionAsync(driver);
        var pause = CallAsync(driver, Tool, Arguments(session));
        int? discoverySequence = null;
        int? publicSequence = null;
        try
        {
            await RequestCountAsync(driver, "threads", 1);
            var first = Assert.Single(await driver.ReadFixtureTranscriptAsync(), static entry => entry.Kind == "request" && entry.Command == "threads");
            discoverySequence = Assert.IsType<int>(first.Sequence);
            var beforeSecondPause = (await driver.ReadNativeActionsAsync()).Count;
            AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session, 11)), true), "dap_pause_refused", "DAP_PAUSE_REFUSED");
            Assert.Equal(beforeSecondPause, (await driver.ReadNativeActionsAsync()).Count);
            var threads = CallAsync(driver, "get_threads", Arguments(session));
            await RequestCountAsync(driver, "threads", 2);
            var requests = (await driver.ReadFixtureTranscriptAsync()).Where(static entry => entry.Kind == "request" && entry.Command == "threads").ToArray();
            Assert.Equal(2, requests.Length);
            publicSequence = Assert.IsType<int>(requests[1].Sequence);
            Assert.NotEqual(discoverySequence, publicSequence);

            driver.ReleaseThreadsResponse(publicSequence.Value);
            var publicResult = Envelope(await threads, false);
            Assert.Equal("threads_success", publicResult["kind"]?.GetValue<string>());
            Assert.Equal(29, Assert.Single(Assert.IsType<JsonArray>(publicResult["threads"]))!["id"]?.GetValue<int>());
            Assert.False(pause.IsCompleted, "The other threads response must not settle pause discovery.");
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static entry => entry.Command == "pause");

            driver.ReleaseThreadsResponse(discoverySequence.Value);
            AssertAcknowledged(Envelope(await pause, false), 7);
            var transcript = await driver.ReadFixtureTranscriptAsync();
            var responses = transcript.Where(static entry => entry.Kind == "response" && entry.Command == "threads").ToArray();
            Assert.Equal(new[] { publicSequence.Value, discoverySequence.Value }, responses.Select(static entry => entry.Sequence!.Value));
            AssertPauseRequest(Assert.Single(await driver.ReadNativeActionsAsync(), static entry => entry.Command == "pause"), 7);
        }
        finally
        {
            if (publicSequence.HasValue) driver.ReleaseThreadsResponse(publicSequence.Value);
            if (discoverySequence.HasValue) driver.ReleaseThreadsResponse(discoverySequence.Value);
        }
    }

    [Theory]
    [InlineData("negative", "dap_pause_refused", "DAP_PAUSE_REFUSED", false)]
    [InlineData("wrong-command", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    [InlineData("wrong-request-sequence", "dap_pause_timeout", "DAP_PAUSE_TIMEOUT", true)]
    [InlineData("malformed-success", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    [InlineData("reader-failure", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    [InlineData("eof", "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR", true)]
    [InlineData("timeout", "dap_pause_timeout", "DAP_PAUSE_TIMEOUT", true)]
    public async Task Pause_RefusedOrUncertainExchange_ReturnsFixedRedactedOutcomeAndCorrectDisposition(
        string pauseMode, string kind, string error, bool drains)
    {
        await using var driver = await StartDriverAsync(pauseMode: pauseMode);
        var session = await StartSessionAsync(driver);

        AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session, 7)), true), kind, error);

        Assert.Single(await driver.ReadNativeActionsAsync(), static entry => entry.Command == "pause");
        if (drains)
        {
            await AssertRetiredAsync(driver, session);
        }
        else
        {
            _ = await StateAsync(driver, session);
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static entry => entry.Command is "terminate" or "disconnect");
        }
    }

    [Theory]
    [InlineData(23)]
    [InlineData(0)]
    [InlineData(-1)]
    public async Task Pause_TerminalBeforeAck_ReturnsOnlyObservedTerminalFieldsWithoutWaitingForAck(int exitCode)
    {
        await using var driver = await StartDriverAsync(pauseMode: "terminal-before-ack", exitCode: exitCode);
        var session = await StartSessionAsync(driver);
        var pause = CallAsync(driver, Tool, Arguments(session, 7));
        try
        {
            await RecordAsync(driver, "pause-terminal");
            var terminal = Envelope(await pause.WaitAsync(ObservationTimeout), false);
            Assert.Equal("pause_terminal", terminal["kind"]?.GetValue<string>());
            Assert.Equal("exited", terminal["terminal"]?.GetValue<string>());
            Assert.Equal(exitCode, terminal["exitCode"]?.GetValue<int>());
            Assert.Equal(3, terminal.Count);
            Assert.Single(await driver.ReadNativeActionsAsync(), static entry => entry.Command == "pause");
        }
        finally
        {
            driver.ReleasePauseResponse();
        }
    }

    [Fact]
    public async Task Pause_MatchedSequenceWithContinueCommand_DeliversProtocolErrorToPauseInsteadOfContinueAcknowledgement()
    {
        await using var driver = await StartDriverAsync(
            pauseMode: "wrong-command-continue", lifecycleMode: "all-stop", nextMode: "continue-ack-only");
        var session = await StartSessionAsync(driver, stopped: true);
        using var adapter = System.Diagnostics.Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        _ = adapter.Handle;
        Assert.False(adapter.HasExited, "The controlled adapter must be live and its process identity pinned before the collision.");
        var continuation = CallAsync(driver, "continue_execution", Arguments(session, 1));
        await RecordAsync(driver, "continue-acknowledged");
        await ReaderBarrierAsync(driver, session);
        Assert.False(continuation.IsCompleted, "The acknowledged continue still awaits a real stop.");
        var continueRequest = Assert.Single(await driver.ReadFixtureTranscriptAsync(),
            static entry => entry.Kind == "request" && entry.Command == "continue");
        var continueSequence = Assert.IsType<int>(continueRequest.Sequence);
        var continueFrame = JsonNode.Parse(Assert.IsType<string>(continueRequest.RawPayload))!.AsObject();
        Assert.Equal(continueSequence, continueFrame["seq"]?.GetValue<int>());
        Assert.Equal("continue", continueFrame["command"]?.GetValue<string>());

        var pause = CallAsync(driver, Tool, Arguments(session, 1));
        await RequestCountAsync(driver, "pause", 1);
        var pauseRequest = Assert.Single(await driver.ReadFixtureTranscriptAsync(),
            static entry => entry.Kind == "request" && entry.Command == "pause");
        var pauseSequence = Assert.IsType<int>(pauseRequest.Sequence);
        Assert.NotEqual(continueSequence, pauseSequence);
        var pauseFrame = JsonNode.Parse(Assert.IsType<string>(pauseRequest.RawPayload))!.AsObject();
        Assert.Equal(pauseSequence, pauseFrame["seq"]?.GetValue<int>());
        AssertPauseRequest(Assert.Single(await driver.ReadNativeActionsAsync(), static entry => entry.Command == "pause"), 1);
        using (var observation = new CancellationTokenSource(ObservationTimeout))
        {
            await driver.WaitForDapResponseAsync("continue", pauseSequence, observation.Token);
        }
        var transcript = await driver.ReadFixtureTranscriptAsync();
        var originalAck = Assert.Single(transcript,
            entry => entry.Kind == "response" && entry.Command == "continue" && entry.Sequence == continueSequence);
        Assert.True(JsonNode.Parse(Assert.IsType<string>(originalAck.RawPayload))!["success"]!.GetValue<bool>());
        var mismatchedResponse = Assert.Single(transcript,
            entry => entry.Kind == "response" && entry.Sequence == pauseSequence);
        var responseFrame = JsonNode.Parse(Assert.IsType<string>(mismatchedResponse.RawPayload))!.AsObject();
        Assert.Equal(pauseSequence, responseFrame["request_seq"]?.GetValue<int>());
        Assert.Equal("continue", responseFrame["command"]?.GetValue<string>());
        Assert.True(responseFrame["success"]!.GetValue<bool>());

        AssertFixedError(Envelope(await pause.WaitAsync(ObservationTimeout), true),
            "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR");
        await AssertRetiredAsync(driver, session);
        var interruptedContinue = Envelope(await continuation.WaitAsync(ObservationTimeout), true);
        Assert.Contains(interruptedContinue["kind"]?.GetValue<string>(),
            new[] { "debug_session_not_found", "dap_continue_protocol_error" });
        var writes = await driver.ReadNativeActionsAsync();
        Assert.Single(writes, static entry => entry.Command == "continue");
        Assert.Single(writes, static entry => entry.Command == "pause");
        await adapter.WaitForExitAsync().WaitAsync(ObservationTimeout);
        adapter.Refresh();
        Assert.True(adapter.HasExited, "Pause protocol fencing must finish actual owned-adapter cleanup, whether graceful or forced.");
        var beforeReplay = writes.Count;
        AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session, 1)), true),
            "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        AssertFixedError(Envelope(await CallAsync(driver, "continue_execution", Arguments(session, 1)), true),
            "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        Assert.Equal(beforeReplay, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task Pause_ProtocolFenceBesideContinue_FinalizationCannotReopenRetiredSession()
    {
        await using var driver = await StartDriverAsync(pauseMode: "wrong-command", lifecycleMode: "all-stop", nextMode: "continue-ack-only");
        var session = await StartSessionAsync(driver, stopped: true);
        var continuation = CallAsync(driver, "continue_execution", Arguments(session, 1));
        await RecordAsync(driver, "continue-acknowledged");
        await ReaderBarrierAsync(driver, session);
        Assert.False(continuation.IsCompleted);

        AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session, 1)), true), "dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR");
        await AssertRetiredAsync(driver, session);
        _ = Envelope(await continuation.WaitAsync(ObservationTimeout), true);

        var before = (await driver.ReadNativeActionsAsync()).Count;
        AssertFixedError(Envelope(await CallAsync(driver, Tool, Arguments(session, 1)), true), "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        AssertFixedError(Envelope(await CallAsync(driver, "continue_execution", Arguments(session, 1)), true), "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    private static Task<ModernMcpProcessDriver> StartDriverAsync(
        string pauseMode = "ack-only", string? lifecycleMode = null, string threadsMode = "success",
        string nextMode = "success", int? eventThreadId = 1, bool? allThreadsStopped = true, int exitCode = 23) =>
        ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(
                SuppressLifecycleEvents: lifecycleMode is null,
                LifecycleMode: lifecycleMode ?? "default",
                ThreadsResponseMode: threadsMode,
                SupportsDelayedStackTraceLoading: true,
                NextResponseMode: nextMode,
                ExitCode: exitCode,
                PauseResponseMode: pauseMode,
                PauseEventThreadId: eventThreadId,
                PauseAllThreadsStopped: allThreadsStopped)), startupTimeout: TimeSpan.FromSeconds(30));

    private static async Task<string> StartSessionAsync(ModernMcpProcessDriver driver, bool stopped = false)
    {
        var content = Envelope(await CallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath }), false);
        Assert.Equal("start_debug_success", content["kind"]?.GetValue<string>());
        var session = Assert.IsType<string>(content["debugSessionId"]?.GetValue<string>());
        if (stopped)
        {
            await RecordAsync(driver, "stopped-event");
            await ReaderBarrierAsync(driver, session);
        }
        return session;
    }

    private static Task<JsonRpcResponse> CallAsync(
        ModernMcpProcessDriver driver, string tool, JsonObject arguments, CancellationToken cancellationToken = default) =>
        driver.CallToolRawAsync(tool, arguments, ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(Guid.NewGuid().ToString("N")), cancellationToken, OperationTimeout);

    private static JsonObject Arguments(string session, int? threadId = null)
    {
        var arguments = new JsonObject { ["debugSessionId"] = session };
        if (threadId.HasValue) arguments["threadId"] = threadId.Value;
        return arguments;
    }

    private static JsonObject DefaultArguments(string session, string input)
    {
        var arguments = Arguments(session);
        if (input == "null") arguments["threadId"] = null;
        else if (input == "zero") arguments["threadId"] = 0;
        return arguments;
    }

    private static async Task<JsonObject> StateAsync(ModernMcpProcessDriver driver, string session)
    {
        var result = Envelope(await CallAsync(driver, "get_debug_state", Arguments(session)), false);
        Assert.Equal("debug_state_success", result["kind"]?.GetValue<string>());
        Assert.Equal(session, result["debugSessionId"]?.GetValue<string>());
        return Assert.IsType<JsonObject>(result["state"]);
    }

    private static async Task ReaderBarrierAsync(ModernMcpProcessDriver driver, string session)
    {
        Assert.Equal("threads_success", Envelope(await CallAsync(driver, "get_threads", Arguments(session)), false)["kind"]?.GetValue<string>());
    }

    private static async Task RecordAsync(ModernMcpProcessDriver driver, string kind)
    {
        using var deadline = new CancellationTokenSource(ObservationTimeout);
        await driver.WaitForFixtureRecordAsync(kind, deadline.Token);
    }

    private static async Task RequestCountAsync(ModernMcpProcessDriver driver, string command, int count)
    {
        using var deadline = new CancellationTokenSource(ObservationTimeout);
        await driver.WaitForDapRequestCountAsync(command, count, deadline.Token);
    }

    private static async Task AssertScopesAsync(ModernMcpProcessDriver driver, string session, string context, int frame)
    {
        var arguments = Arguments(session);
        arguments["inspectionContextId"] = context;
        arguments["frameId"] = frame;
        Assert.Equal("scopes_success", Envelope(await CallAsync(driver, "get_scopes", arguments), false)["kind"]?.GetValue<string>());
    }

    private static async Task AssertRetiredAsync(ModernMcpProcessDriver driver, string session)
    {
        AssertFixedError(Envelope(await CallAsync(driver, "get_debug_state", Arguments(session)), true),
            "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
    }

    private static async Task AssertWritesAsync(ModernMcpProcessDriver driver, int before, int selectedThreadId, params string[] commands)
    {
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
        Assert.Equal(commands, writes.Select(static entry => entry.Command));
        AssertPauseRequest(Assert.Single(writes, static entry => entry.Command == "pause"), selectedThreadId);
    }

    private static void AssertPauseRequest(ModernNativeAction entry, int selectedThreadId)
    {
        var request = JsonNode.Parse(entry.Detail ?? throw new Xunit.Sdk.XunitException("Controlled adapter omitted the actual pause frame."))!.AsObject();
        Assert.Equal("pause", request["command"]?.GetValue<string>());
        var arguments = Assert.IsType<JsonObject>(request["arguments"]);
        Assert.Equal(new[] { "threadId" }, arguments.Select(static property => property.Key));
        Assert.Equal(selectedThreadId, arguments["threadId"]?.GetValue<int>());
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

    private static void AssertAcknowledged(JsonObject content, int selectedThreadId)
    {
        Assert.Equal("pause_acknowledged", content["kind"]?.GetValue<string>());
        Assert.Equal(selectedThreadId, content["threadId"]?.GetValue<int>());
        Assert.Equal(2, content.Count);
    }

    private static void AssertFixedError(JsonObject content, string kind, string error)
    {
        Assert.Equal(kind, content["kind"]?.GetValue<string>());
        Assert.Equal(error, content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }
}
