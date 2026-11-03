using System.Text.Json;
using Microsoft.Extensions.Time.Testing;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeStepPairLifecycleTests
{
    private static readonly TimeSpan ObservationTimeout = TimeSpan.FromSeconds(10);

    [Theory]
    [InlineData("step_into", "write-gate")]
    [InlineData("step_into", "pre-header")]
    [InlineData("step_out", "write-gate")]
    [InlineData("step_out", "pre-header")]
    public async Task StepPair_CancellationBeforeWriterHandoffRetainsSameRegistrySlotAndLiveTransportWithoutRestoringAuthority(
        string tool, string barrier)
    {
        await using var session = await StartAsync();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await ReadyAsync(session, observation.Token);
        using var cancellation = new CancellationTokenSource();
        var writesBefore = session.DapWriteCalls;
        var writeHeld = barrier == "write-gate";
        if (writeHeld) await session.HoldDapWriteAsync(observation.Token);
        else session.ArmDapWriteBeforeHeaderBarrier();
        void Release()
        {
            if (writeHeld)
            {
                session.ReleaseDapWrite();
                writeHeld = false;
            }
            session.ReleaseDapWriteBarrier();
        }
        var operation = session.StepPairThroughRegistryAsync(tool, 1, cancellation.Token,
            beforeCancellationInspection: async () =>
            {
                Assert.Equal(0, session.PendingRequestCount);
                Assert.Equal(writesBefore, session.DapWriteCalls);
                Assert.True(session.IsUsable);
                Assert.False(session.IsExecutionClosing);
                Release();
                Assert.True(await session.GetCallStackIsRefusedAsync(1, 0, 20, observation.Token));
                await session.StepOverAsync(1, observation.Token);
            });
        try
        {
            if (barrier == "write-gate") await session.WaitForPendingCommandAsync(Command(tool), observation.Token);
            else await session.WaitForDapPreHeaderWriteAsync(observation.Token);
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            Assert.True(probe.TokenRetained);
            Assert.True(probe.SlotRetained);
            Assert.True(probe.AdapterAlive);
            Assert.Null(probe.LeaseReleasedBeforeRemoval);
            Assert.Equal(0, probe.ActiveRequestCount);
            Assert.Equal("debug_state_success", Content(probe.Result, false).GetProperty("kind").GetString());
            Assert.Equal("threads_success", Content(Assert.IsType<CallToolResult>(probe.RetainedThreadsResult), false).GetProperty("kind").GetString());
            Assert.DoesNotContain(await RequestsAsync(session), static entry => entry.Command is "stepIn" or "stepOut" or "next" or "stackTrace");
        }
        finally
        {
            Release();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Theory]
    [InlineData("step_into", "zero", 0, false)]
    [InlineData("step_into", "prefix", 1, false)]
    [InlineData("step_into", "opaque", 1, true)]
    [InlineData("step_into", "after-header", 0, false)]
    [InlineData("step_out", "zero", 0, false)]
    [InlineData("step_out", "prefix", 1, false)]
    [InlineData("step_out", "opaque", 1, true)]
    [InlineData("step_out", "after-header", 0, false)]
    public async Task StepPair_CancellationAfterWriterHandoffDrainsBeforePropagationEvenWhenNoCompleteRequestReachedAdapter(
        string tool, string barrier, int prefixBytes, bool opaqueCancellation)
    {
        await using var session = await StartAsync();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await ReadyAsync(session, observation.Token);
        using var cancellation = new CancellationTokenSource();
        if (barrier == "after-header") session.ArmDapWriteAfterHeaderBarrier();
        else session.ArmDapWriteAtHeaderEntryBarrier(prefixBytes, opaqueCancellation);
        var operation = session.StepPairThroughRegistryAsync(tool, 1, cancellation.Token,
            beforeCancellationInspection: () =>
            {
                Assert.Equal(0, session.PendingRequestCount);
                return Task.CompletedTask;
            });
        try
        {
            if (barrier == "after-header") await session.WaitForDapHeaderWriteAsync(observation.Token);
            else await session.WaitForDapHeaderEntryAsync(observation.Token);
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            Retired(probe);
            if (barrier != "after-header")
            {
                Assert.Equal(prefixBytes == 0 ? 0 : 1, session.ArmedHeaderUnderlyingWriteCalls);
                Assert.Equal((long)prefixBytes, session.ArmedHeaderBytesForwarded);
            }
            Assert.DoesNotContain(await RequestsAsync(session), static entry => entry.Command is "stepIn" or "stepOut");
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Theory]
    [InlineData("step_into")]
    [InlineData("step_out")]
    public async Task StepPair_AckReadBeforeFullWriteCommitCannotMakeCancellationSafe(string tool)
    {
        await using var session = await StartAsync();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await ReadyAsync(session, observation.Token);
        using var cancellation = new CancellationTokenSource();
        session.ArmDapWriteAfterFlushBarrier();
        var operation = session.StepPairThroughRegistryAsync(tool, 1, cancellation.Token);
        try
        {
            await session.WaitForDapFlushAsync(observation.Token);
            await session.WaitForPendingResponseAsync(Command(tool), observation.Token);
            Assert.False(operation.IsCompleted, "A reader ACK and stop must not bypass held write-commit publication.");
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            Retired(probe);
            Assert.Single(await RequestsAsync(session), entry => entry.Command == Command(tool));
            Assert.Equal(0, session.PendingRequestCount);
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Theory]
    [InlineData("step_into", "hold")]
    [InlineData("step_into", "post-ack-ignored-stops")]
    [InlineData("step_out", "hold")]
    [InlineData("step_out", "post-ack-ignored-stops")]
    public async Task StepPair_CommittedCancellationBeforeOrAfterAckDrainsAndReleasesLeaseBeforeRemoval(string tool, string mode)
    {
        await using var session = await StartAsync(mode);
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await ReadyAsync(session, observation.Token);
        using var cancellation = new CancellationTokenSource();
        var operation = session.StepPairThroughRegistryAsync(tool, 1, cancellation.Token);
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync(
                mode == "hold" ? "next-response-gated" : "next-final-stop-gated", observation.Token);
            await session.WaitForReaderBarrierAsync(observation.Token);
            Assert.False(operation.IsCompleted);
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            Retired(probe);
            Assert.Equal(0, session.PendingRequestCount);
            Assert.Single(await RequestsAsync(session), entry => entry.Command == Command(tool));
            Assert.Single(await RequestsAsync(session), static entry => entry.Command == "terminate");
            Assert.Single(await RequestsAsync(session), static entry => entry.Command == "disconnect");
        }
        finally
        {
            session.Fixture.ReleaseNextResponse();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Theory]
    [InlineData("step_into")]
    [InlineData("step_out")]
    public async Task StepPair_DelayedAckAndLaterStopShareOneThirtySecondPostCommitDeadline(string tool)
    {
        var clock = new FakeTimeProvider();
        await using var session = await StartAsync("hold-ack-no-stop", clock);
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await ReadyAsync(session, observation.Token);
        var operation = session.StepPairThroughRegistryAsync(tool, 1, observation.Token);
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync("next-response-gated", observation.Token);
            await session.WaitForReaderBarrierAsync(observation.Token);
            clock.Advance(TimeSpan.FromSeconds(20));
            Assert.False(operation.IsCompleted);
            session.Fixture.ReleaseNextResponse();
            await session.Fixture.WaitForTranscriptKindAsync("next-acknowledged", observation.Token);
            await session.WaitForReaderBarrierAsync(observation.Token);
            clock.Advance(TimeSpan.FromSeconds(9));
            Assert.False(operation.IsCompleted, "ACK must not reset the original observation budget.");
            clock.Advance(TimeSpan.FromSeconds(1));
            var probe = await operation.WaitAsync(observation.Token);

            TimedOut(probe, tool);
            Assert.Single(await RequestsAsync(session), entry => entry.Command == Command(tool));
            Assert.Single(await RequestsAsync(session), static entry => entry.Command == "terminate");
            Assert.Single(await RequestsAsync(session), static entry => entry.Command == "disconnect");
        }
        finally
        {
            session.Fixture.ReleaseNextResponse();
            session.ReleaseDapWriteBarrier();
            _ = await Record.ExceptionAsync(() => operation.WaitAsync(observation.Token));
        }
    }

    [Theory]
    [InlineData("step_into")]
    [InlineData("step_out")]
    public async Task StepPair_DeadlineDoesNotStartUntilHeaderPayloadAndFlushAreCommitted(string tool)
    {
        var clock = new FakeTimeProvider();
        await using var session = await StartAsync("hold-ack-no-stop", clock);
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await ReadyAsync(session, observation.Token);
        session.ArmDapWriteAfterHeaderBarrier();
        var operation = session.StepPairThroughRegistryAsync(tool, 1, observation.Token);
        try
        {
            await session.WaitForDapHeaderWriteAsync(observation.Token);
            clock.Advance(TimeSpan.FromSeconds(31));
            Assert.False(operation.IsCompleted, "A partial write is not the start of the post-commit deadline.");
            session.ReleaseDapWriteBarrier();
            await session.Fixture.WaitForTranscriptKindAsync("next-response-gated", observation.Token);
            await session.WaitForReaderBarrierAsync(observation.Token);
            clock.Advance(TimeSpan.FromSeconds(29));
            Assert.False(operation.IsCompleted, "The complete write receives its full thirty-second budget.");
            clock.Advance(TimeSpan.FromSeconds(1));

            TimedOut(await operation.WaitAsync(observation.Token), tool);
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            session.Fixture.ReleaseNextResponse();
            _ = await Record.ExceptionAsync(() => operation.WaitAsync(observation.Token));
        }
    }

    private static Task<NetCoreDbgSessionContractDriver> StartAsync(string mode = "success", TimeProvider? clock = null) =>
        NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(SupportsDelayedStackTraceLoading: true, LifecycleMode: "all-stop", NextResponseMode: mode),
            "D:\\fixtures\\program.dll", TimeSpan.FromSeconds(2), TimeSpan.FromSeconds(30), TimeSpan.FromMilliseconds(300),
            CancellationToken.None, stepOverTimeProvider: clock);

    private static async Task ReadyAsync(NetCoreDbgSessionContractDriver session, CancellationToken cancellationToken)
    {
        await session.Fixture.WaitForEventAsync("stopped", cancellationToken);
        await session.WaitForReaderBarrierAsync(cancellationToken);
    }

    private static string Command(string tool) => tool switch
    {
        "step_into" => "stepIn",
        "step_out" => "stepOut",
        _ => throw new ArgumentOutOfRangeException(nameof(tool)),
    };

    private static JsonElement Content(CallToolResult result, bool isError)
    {
        Assert.Equal("complete", result.ResultType);
        Assert.Equal(isError, result.IsError == true);
        var content = Assert.IsType<JsonElement>(result.StructuredContent);
        Assert.Equal(content.GetRawText(), Assert.IsType<TextContentBlock>(Assert.Single(result.Content)).Text);
        return content;
    }

    private static void TimedOut(StepOverRegistryProbe probe, string tool)
    {
        Assert.False(probe.CancellationObserved);
        var content = Content(probe.Result, false);
        Assert.Equal($"{tool}_timed_out", content.GetProperty("kind").GetString());
        Assert.Equal(1, content.GetProperty("threadId").GetInt32());
        Assert.Equal(30, content.GetProperty("timeoutSeconds").GetInt32());
        Assert.Equal(3, content.EnumerateObject().Count());
        Assert.False(probe.TokenRetained);
        Assert.False(probe.SlotRetained);
        Assert.False(probe.AdapterAlive);
        Assert.True(probe.LeaseReleasedBeforeRemoval == true);
        Assert.Equal(0, probe.ActiveRequestCount);
        Assert.Null(probe.RetainedThreadsResult);
    }

    private static void Retired(StepOverRegistryProbe probe)
    {
        Assert.False(probe.TokenRetained);
        Assert.False(probe.SlotRetained);
        Assert.False(probe.AdapterAlive);
        Assert.True(probe.LeaseReleasedBeforeRemoval == true);
        Assert.Equal(0, probe.ActiveRequestCount);
        Assert.Null(probe.RetainedThreadsResult);
        var content = Content(probe.Result, true);
        Assert.Equal("debug_session_not_found", content.GetProperty("kind").GetString());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", content.GetProperty("error").GetString());
        Assert.Equal(2, content.EnumerateObject().Count());
    }

    private static async Task<FixtureTranscriptEntry[]> RequestsAsync(NetCoreDbgSessionContractDriver session) =>
        (await session.Fixture.ReadTranscriptAsync()).Where(static entry => entry.Kind == "request").ToArray();
}
