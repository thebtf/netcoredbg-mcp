using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class SessionSlotLifecycleTests
{
    private const string GetThreads = "get_threads";
    private static readonly TimeSpan ObservationTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan DrainDeadline = TimeSpan.FromSeconds(4);

    [Fact]
    public async Task ExplicitStop_ClosesAdmissionBeforeDrainingHeldThreadsLease()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads("hold"));
        var debugSessionId = await StartSessionAsync(driver, "slot-explicit-stop-start");
        var inFlight = driver.CallToolRawAsync(
            GetThreads,
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-explicit-stop-threads"));
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await driver.WaitForThreadsRequestAsync(observation.Token);
        var beforeStop = await driver.ReadNativeActionsAsync();

        // Act
        var stop = driver.CallToolRawAsync(
            "stop_debug",
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-explicit-stop"));
        await WaitForTokenRemovalAsync(driver, debugSessionId, "slot-explicit-stop-removed", observation.Token);
        var late = await driver.CallToolRawAsync(
            GetThreads,
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-explicit-stop-late"));

        // Assert
        Assert.False(stop.IsCompleted, "Close-and-drain must wait for the admitted threads lease.");
        Assert.False(inFlight.IsCompleted, "The admitted threads operation must remain leased until its response settles.");
        AssertNotFound(AssertApplicationEnvelope(late, isError: true));
        Assert.DoesNotContain(
            (await driver.ReadNativeActionsAsync()).Skip(beforeStop.Count),
            static action => action.Command is "terminate" or "disconnect");

        driver.ReleaseThreadsResponse();
        AssertThreadsSuccess(AssertApplicationEnvelope(await inFlight.WaitAsync(DrainDeadline), isError: false));
        Assert.Equal("stop_debug_success", AssertApplicationEnvelope(await stop.WaitAsync(DrainDeadline), isError: false)["kind"]?.GetValue<string>());
        AssertSingleCleanup(await driver.ReadNativeActionsAsync());
    }

    [Fact]
    public async Task HostDisposal_DrainsHeldThreadsLeaseBeforeProcessCompletion()
    {
        // The official stdio client owns and force-kills its child on disposal. This test
        // therefore exercises the same registered SessionDisposer through the generic-host
        // lifecycle rather than treating client transport completion as graceful shutdown.
        using var cancellation = new CancellationTokenSource(DrainDeadline);
        await using var session = await NetCoreDbgSessionContractDriver.StartAsync(
            new FixtureConfiguration(
                SuppressLifecycleEvents: true,
                ThreadsResponseMode: "hold"),
            "D:\\fixtures\\program.dll",
            ObservationTimeout,
            ObservationTimeout,
            TimeSpan.FromMilliseconds(300),
            cancellation.Token);
        await using var hosted = await session.StartHostedThreadsLeaseAsync(cancellation.Token);
        await session.Fixture.WaitForThreadsRequestAsync(cancellation.Token);
        var beforeStop = await session.Fixture.ReadTranscriptAsync();

        // Act
        var completion = hosted.StopAsync(cancellation.Token);

        // Assert
        Assert.False(completion.IsCompleted, "Host disposal must wait for the admitted threads lease.");
        Assert.DoesNotContain(
            (await session.Fixture.ReadTranscriptAsync()).Skip(beforeStop.Count),
            static entry => entry.Command is "terminate" or "disconnect");

        session.Fixture.ReleaseThreadsResponse();
        var result = await hosted.Threads.WaitAsync(DrainDeadline);
        var content = Assert.IsType<JsonElement>(result.StructuredContent);
        Assert.Equal("complete", result.ResultType);
        Assert.Equal("threads_success", content.GetProperty("kind").GetString());
        await completion.WaitAsync(DrainDeadline);
        AssertSingleCleanup(await session.Fixture.ReadTranscriptAsync());
    }

    [Fact]
    public async Task GetThreads_UnusableSessionEviction_ReturnsNotFoundWithoutDapIo()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads("success"));
        var debugSessionId = await StartSessionAsync(driver, "slot-unusable-start");
        var beforeTermination = await driver.ReadNativeActionsAsync();
        await driver.TerminateControlledAdapterAsync();

        // Act
        var response = await driver.CallToolRawAsync(
            GetThreads,
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-unusable-threads"));

        // Assert
        AssertNotFound(AssertApplicationEnvelope(response, isError: true));
        Assert.Equal(beforeTermination.Count, (await driver.ReadNativeActionsAsync()).Count);
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-unusable-state")), isError: true));
    }

    [Fact]
    public async Task ReaderFailureWithoutInflightThreads_EvictsTokenBeforeLaterThreadsCall()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(
                SendMalformedDapFrameAfterStartup: true,
                SuppressLifecycleEvents: true,
                ThreadsResponseMode: "success")));
        var debugSessionId = await StartSessionAsync(driver, "slot-reader-idle-start");
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await driver.WaitForFixtureRecordAsync("malformed-dap-frame-sent", observation.Token);
        var before = await driver.ReadNativeActionsAsync();

        // Act
        var response = await driver.CallToolRawAsync(
            GetThreads,
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-reader-idle-threads"));

        // Assert
        AssertNotFound(AssertApplicationEnvelope(response, isError: true));
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task GetThreads_ProtocolFailure_ReleasesOwnLeaseBeforeJoiningCleanup()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads("malformed-body"));
        var debugSessionId = await StartSessionAsync(driver, "slot-protocol-start");

        // Act
        var response = await driver.CallToolRawAsync(
            GetThreads,
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-protocol-threads")).WaitAsync(ObservationTimeout);

        // Assert
        AssertProtocolError(AssertApplicationEnvelope(response, isError: true));
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-protocol-state")), isError: true));
        AssertSingleCleanup(await driver.ReadNativeActionsAsync());
    }

    [Fact]
    public async Task ExplicitStop_DrainDeadlineAbortsHeldThreadsLeaseThenCleansUp()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads("hold"));
        var debugSessionId = await StartSessionAsync(driver, "slot-deadline-start");
        var inFlight = driver.CallToolRawAsync(
            GetThreads,
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-deadline-threads"));
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await driver.WaitForThreadsRequestAsync(observation.Token);
        var beforeForcedCleanup = await driver.ReadNativeActionsAsync();

        // Act
        var stop = driver.CallToolRawAsync(
            "stop_debug",
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-deadline-stop"));

        // Assert
        var inFlightResult = await inFlight.WaitAsync(DrainDeadline);
        var stopResult = await stop.WaitAsync(DrainDeadline);
        AssertProtocolError(AssertApplicationEnvelope(inFlightResult, isError: true));
        Assert.Equal("complete", ModernMcpProcessDriver.RequireResult(stopResult)["resultType"]?.GetValue<string>());

        // The cancelled deadline token forces process-tree cleanup. A terminate request may
        // already be in flight when cancellation arrives, but forced cleanup never relies on
        // completing the graceful terminate/disconnect sequence.
        var afterForcedCleanup = await driver.ReadNativeActionsAsync();
        Assert.InRange(afterForcedCleanup.Count(static action => action.Command == "terminate"), 0, 1);
        Assert.DoesNotContain(afterForcedCleanup, static action => action.Command == "disconnect");
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-deadline-state")), isError: true));
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            GetThreads,
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-deadline-late-threads")), isError: true));
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "stop_debug",
            SessionArguments(debugSessionId),
            Meta(),
            new RequestId("slot-deadline-second-stop")), isError: true));
        Assert.Equal(afterForcedCleanup, await driver.ReadNativeActionsAsync());
    }

    private static ModernMcpStartOptions Threads(string responseMode) => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(
            SuppressLifecycleEvents: true,
            ThreadsResponseMode: responseMode));

    private static JsonObject Meta() => ModernMcpProcessDriver.CurrentMeta();

    private static JsonObject SessionArguments(string debugSessionId) => new() { ["debugSessionId"] = debugSessionId };

    private static async Task<string> StartSessionAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var start = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            Meta(),
            new RequestId(requestId)), isError: false);
        Assert.Equal("start_debug_success", start["kind"]?.GetValue<string>());
        return Assert.IsType<string>(start["debugSessionId"]?.GetValue<string>());
    }

    private static async Task WaitForTokenRemovalAsync(
        ModernMcpProcessDriver driver,
        string debugSessionId,
        string requestId,
        CancellationToken cancellationToken)
    {
        for (var attempt = 0; ; attempt++)
        {
            var response = await driver.CallToolRawAsync(
                "get_debug_state",
                SessionArguments(debugSessionId),
                Meta(),
                new RequestId($"{requestId}-{attempt}"),
                cancellationToken);
            var result = ModernMcpProcessDriver.RequireResult(response);
            if (result["structuredContent"] is JsonObject content &&
                content["kind"]?.GetValue<string>() == "debug_session_not_found")
            {
                return;
            }

            await Task.Delay(TimeSpan.FromMilliseconds(25), cancellationToken);
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

    private static void AssertThreadsSuccess(JsonObject content)
    {
        Assert.Equal("threads_success", content["kind"]?.GetValue<string>());
        Assert.NotEmpty(Assert.IsType<JsonArray>(content["threads"]));
    }

    private static void AssertSingleCleanup(IReadOnlyList<FixtureTranscriptEntry> entries)
    {
        Assert.Equal(1, entries.Count(static entry => entry.Command == "terminate"));
        Assert.Equal(1, entries.Count(static entry => entry.Command == "disconnect"));
    }

    private static void AssertNotFound(JsonObject content)
    {
        Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static void AssertProtocolError(JsonObject content)
    {
        Assert.Equal("dap_threads_protocol_error", content["kind"]?.GetValue<string>());
        Assert.Equal("DAP_THREADS_PROTOCOL_ERROR", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static void AssertSingleCleanup(IReadOnlyList<ModernNativeAction> actions)
    {
        Assert.Equal(1, actions.Count(static action => action.Command == "terminate"));
        Assert.Equal(1, actions.Count(static action => action.Command == "disconnect"));
    }

}
