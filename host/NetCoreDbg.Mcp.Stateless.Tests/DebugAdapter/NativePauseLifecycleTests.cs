using System.Text.Json;
using Microsoft.Extensions.Time.Testing;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativePauseLifecycleTests
{
    private static readonly TimeSpan ObservationTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(30);

    [Theory]
    [InlineData(false, "write-gate")]
    [InlineData(false, "pre-header")]
    [InlineData(true, "write-gate")]
    [InlineData(true, "pre-header")]
    public async Task Pause_CancellationBeforeWriterHandoff_PreservesSessionAndReleasesReservation(bool discover, string barrier)
    {
        await using var session = await StartAsync();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        using var cancellation = new CancellationTokenSource();
        var writesBefore = session.DapWriteCalls;
        var writeGateHeld = barrier == "write-gate";
        if (writeGateHeld)
        {
            await session.HoldDapWriteAsync(observation.Token);
        }
        else
        {
            session.ArmDapWriteBeforeHeaderBarrier();
        }
        void Release()
        {
            if (writeGateHeld)
            {
                session.ReleaseDapWrite();
                writeGateHeld = false;
            }
            session.ReleaseDapWriteBarrier();
        }
        Func<string, CancellationToken, Task<CallToolResult>>? invokeAgain = null;
        var operation = session.PauseThroughRegistryAsync(discover ? null : 7, cancellation.Token,
            beforeCancellationInspection: async () =>
            {
                Assert.Equal(0, session.PendingRequestCount);
                Assert.Equal(writesBefore, session.DapWriteCalls);
                Assert.True(session.IsUsable);
                Assert.False(session.IsExecutionClosing);
                Release();
                Assert.NotNull(invokeAgain);
                var retry = await invokeAgain!("pause-after-unwritten-cancellation", observation.Token).WaitAsync(observation.Token);
                Assert.Equal("pause_acknowledged", Content(retry).GetProperty("kind").GetString());
            },
            invokePause: invoke =>
            {
                invokeAgain = invoke;
                return invoke("pause-before-handoff", cancellation.Token);
            });
        try
        {
            if (barrier == "write-gate")
            {
                await session.WaitForPendingCommandAsync(discover ? "threads" : "pause", observation.Token);
            }
            else
            {
                await session.WaitForDapPreHeaderWriteAsync(observation.Token);
            }
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            AssertRetained(probe);
            Assert.Equal("debug_state_success", Content(probe.Result).GetProperty("kind").GetString());
            Assert.Equal("threads_success", Content(Assert.IsType<CallToolResult>(probe.RetainedThreadsResult)).GetProperty("kind").GetString());
            Assert.Single(await RequestsAsync(session), static entry => entry.Command == "pause");
        }
        finally
        {
            Release();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Theory]
    [InlineData(false, "zero", 0, false)]
    [InlineData(false, "prefix", 1, false)]
    [InlineData(false, "opaque", 1, true)]
    [InlineData(false, "after-header", 0, false)]
    [InlineData(true, "zero", 0, false)]
    [InlineData(true, "prefix", 1, false)]
    [InlineData(true, "opaque", 1, true)]
    [InlineData(true, "after-header", 0, false)]
    public async Task Pause_CancellationAfterWriterHandoff_UncertainPauseOrDiscoveryFrameDrains(
        bool discover, string barrier, int prefixBytes, bool opaqueCancellation)
    {
        await using var session = await StartAsync();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        using var cancellation = new CancellationTokenSource();
        if (barrier == "after-header")
        {
            session.ArmDapWriteAfterHeaderBarrier();
        }
        else
        {
            session.ArmDapWriteAtHeaderEntryBarrier(prefixBytes, opaqueCancellation);
        }
        var operation = session.PauseThroughRegistryAsync(discover ? null : 7, cancellation.Token,
            beforeCancellationInspection: () =>
            {
                Assert.Equal(0, session.PendingRequestCount);
                return Task.CompletedTask;
            });
        try
        {
            if (barrier == "after-header")
            {
                await session.WaitForDapHeaderWriteAsync(observation.Token);
            }
            else
            {
                await session.WaitForDapHeaderEntryAsync(observation.Token);
            }
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            AssertRetired(probe);
            if (barrier != "after-header")
            {
                Assert.Equal(prefixBytes == 0 ? 0 : 1, session.ArmedHeaderUnderlyingWriteCalls);
                Assert.Equal((long)prefixBytes, session.ArmedHeaderBytesForwarded);
            }
            Assert.DoesNotContain(await RequestsAsync(session), static entry => entry.Command is "pause" or "threads");
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Pause_ResponseReadBeforeFlushPublishesCommit_DoesNotMakeUncertainFrameSafe(bool discover)
    {
        await using var session = await StartAsync(pauseMode: "ack-only");
        using var observation = new CancellationTokenSource(ObservationTimeout);
        using var cancellation = new CancellationTokenSource();
        session.ArmDapWriteAfterFlushBarrier();
        var operation = session.PauseThroughRegistryAsync(discover ? null : 7, cancellation.Token);
        try
        {
            await session.WaitForDapFlushAsync(observation.Token);
            await session.WaitForPendingResponseAsync(discover ? "threads" : "pause", observation.Token);
            Assert.False(operation.IsCompleted, "An observed response must not bypass the held write-commit publication.");
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            AssertRetired(probe);
            var requests = await RequestsAsync(session);
            Assert.Single(requests, entry => entry.Command == (discover ? "threads" : "pause"));
            if (discover)
            {
                Assert.DoesNotContain(requests, static entry => entry.Command == "pause");
            }
            Assert.Equal(0, session.PendingRequestCount);
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Fact]
    public async Task Pause_CommittedDiscoveryCancellation_LateReplyCannotSettleNewerThreadsRequest()
    {
        await using var session = await StartAsync(threadsMode: "hold-independent-late-response-event");
        using var observation = new CancellationTokenSource(ObservationTimeout);
        using var cancellation = new CancellationTokenSource();
        int? abandonedSequence = null;
        int? newerSequence = null;
        Task<int[]>? newerThreads = null;
        var operation = session.PauseThroughRegistryAsync(null, cancellation.Token,
            beforeCancellationInspection: async () =>
            {
                Assert.Equal(0, session.PendingRequestCount);
                Assert.True(session.IsUsable);
                Assert.False(session.IsExecutionClosing);
                newerThreads = session.GetThreadIdsAsync(observation.Token);
                await session.Fixture.WaitForDapRequestCountAsync("threads", 2, observation.Token);
                var requests = (await RequestsAsync(session)).Where(static entry => entry.Command == "threads").ToArray();
                Assert.Equal(2, requests.Length);
                newerSequence = Assert.IsType<int>(requests[1].Sequence);
                Assert.NotEqual(abandonedSequence, newerSequence);
                Assert.NotNull(abandonedSequence);
                session.Fixture.ReleaseThreadsResponse(abandonedSequence.Value);

                await session.WaitForObservedStopReasonAsync("discovery-reply-barrier", observation.Token);
                await session.Fixture.WaitForDapResponseAsync("threads", abandonedSequence.Value, observation.Token);
                var late = Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry =>
                    entry.Kind == "response" && entry.Command == "threads" && entry.Sequence == abandonedSequence);
                using var lateResponse = JsonDocument.Parse(Assert.IsType<string>(late.RawPayload));
                Assert.Equal(7, Assert.Single(lateResponse.RootElement.GetProperty("body").GetProperty("threads").EnumerateArray()).GetProperty("id").GetInt32());
                Assert.False(newerThreads.IsCompleted, "The reader processed the old reply and its following event, but the newer request must remain pending.");
                Assert.Equal(1, session.PendingRequestCount);
                Assert.True(session.IsUsable);

                session.Fixture.ReleaseThreadsResponse(newerSequence.Value);
                Assert.Equal(new[] { 29 }, await newerThreads.WaitAsync(observation.Token));
                Assert.Equal(0, session.PendingRequestCount);
            });
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync("threads-response-gated", observation.Token);
            await WaitForCommittedWriteAsync(session, observation.Token);
            abandonedSequence = Assert.IsType<int>(Assert.Single(await RequestsAsync(session), static entry => entry.Command == "threads").Sequence);
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            AssertRetained(probe);
            var retained = Content(Assert.IsType<CallToolResult>(probe.RetainedThreadsResult));
            Assert.Equal("threads_success", retained.GetProperty("kind").GetString());
            Assert.Equal(29, Assert.Single(retained.GetProperty("threads").EnumerateArray()).GetProperty("id").GetInt32());
            Assert.DoesNotContain(await RequestsAsync(session), static entry => entry.Command == "pause");
        }
        finally
        {
            if (abandonedSequence is { } abandoned)
            {
                session.Fixture.ReleaseThreadsResponse(abandoned);
            }
            if (newerSequence is { } newer)
            {
                session.Fixture.ReleaseThreadsResponse(newer);
            }
            cancellation.Cancel();
            if (newerThreads is not null)
            {
                _ = await Record.ExceptionAsync(() => newerThreads.WaitAsync(observation.Token));
            }
            _ = await Record.ExceptionAsync(() => operation.WaitAsync(observation.Token));
        }
    }

    [Fact]
    public async Task Pause_AbandonedOrdinaryThreadsReply_DoesNotFailOrSettleActiveDiscovery()
    {
        await using var session = await StartAsync(threadsMode: "hold-independent");
        using var observation = new CancellationTokenSource(ObservationTimeout);
        using var ordinaryCancellation = new CancellationTokenSource();
        var ordinary = session.GetThreadIdsAsync(ordinaryCancellation.Token);
        Task<StepOverRegistryProbe>? paused = null;
        Task<int[]>? readerBarrier = null;
        try
        {
            await session.Fixture.WaitForDapRequestCountAsync("threads", 1, observation.Token);
            await WaitForCommittedWriteAsync(session, observation.Token);
            var abandonedSequence = Assert.IsType<int>(Assert.Single(await RequestsAsync(session), static entry => entry.Command == "threads").Sequence);
            ordinaryCancellation.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => ordinary).WaitAsync(observation.Token);
            Assert.Equal(0, session.PendingRequestCount);
            Assert.True(session.IsUsable);

            paused = session.PauseThroughRegistryAsync(null, observation.Token,
                invokePause: async invoke =>
                {
                    var discovery = invoke("pause-after-abandoned-ordinary-threads", observation.Token);
                    await session.Fixture.WaitForDapRequestCountAsync("threads", 2, observation.Token);
                    await WaitForCommittedWriteAsync(session, observation.Token);
                    var discoveryRequest = (await RequestsAsync(session)).Last(static entry => entry.Command == "threads");
                    var discoverySequence = Assert.IsType<int>(discoveryRequest.Sequence);
                    Assert.NotEqual(abandonedSequence, discoverySequence);
                    session.Fixture.ReleaseThreadsResponse(abandonedSequence);
                    await session.Fixture.WaitForDapResponseAsync("threads", abandonedSequence, observation.Token);
                    var oldResponse = Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry =>
                        entry.Kind == "response" && entry.Command == "threads" && entry.Sequence == abandonedSequence);
                    using var oldPayload = JsonDocument.Parse(Assert.IsType<string>(oldResponse.RawPayload));
                    Assert.Equal(7, Assert.Single(oldPayload.RootElement.GetProperty("body").GetProperty("threads").EnumerateArray()).GetProperty("id").GetInt32());

                    readerBarrier = session.GetThreadIdsAsync(observation.Token);
                    await session.Fixture.WaitForDapRequestCountAsync("threads", 3, observation.Token);
                    var barrierSequence = Assert.IsType<int>((await RequestsAsync(session)).Last(static entry => entry.Command == "threads").Sequence);
                    Assert.NotEqual(discoverySequence, barrierSequence);
                    session.Fixture.ReleaseThreadsResponse(barrierSequence);
                    Assert.Equal(new[] { 29 }, await readerBarrier.WaitAsync(observation.Token));

                    Assert.False(discovery.IsCompleted, "The barrier reply was written after the abandoned ordinary reply; the pause discovery must still await its own sequence.");
                    Assert.Equal(1, session.PendingRequestCount);
                    Assert.True(session.IsUsable);
                    Assert.False(session.IsExecutionClosing);
                    Assert.DoesNotContain(await RequestsAsync(session), static entry => entry.Command == "pause");

                    session.Fixture.ReleaseThreadsResponse(discoverySequence);
                    var result = await discovery.WaitAsync(observation.Token);
                    var content = Content(result);
                    Assert.Equal("pause_acknowledged", content.GetProperty("kind").GetString());
                    Assert.Equal(29, content.GetProperty("threadId").GetInt32());
                    return result;
                });
            var probe = await paused.WaitAsync(observation.Token);

            AssertRetained(probe);
            Assert.False(probe.CancellationObserved);
            Assert.Equal(0, session.PendingRequestCount);
            var writes = await RequestsAsync(session);
            Assert.Equal(3, writes.Count(static entry => entry.Command == "threads"));
            var pause = Assert.Single(writes, static entry => entry.Command == "pause");
            using var arguments = JsonDocument.Parse(Assert.IsType<string>(pause.Arguments));
            Assert.Equal(29, arguments.RootElement.GetProperty("threadId").GetInt32());
        }
        finally
        {
            ordinaryCancellation.Cancel();
            foreach (var request in (await RequestsAsync(session)).Where(static entry => entry.Command == "threads"))
            {
                session.Fixture.ReleaseThreadsResponse(Assert.IsType<int>(request.Sequence));
            }
            _ = await Record.ExceptionAsync(() => ordinary.WaitAsync(observation.Token));
            if (readerBarrier is not null)
            {
                _ = await Record.ExceptionAsync(() => readerBarrier.WaitAsync(observation.Token));
            }
            if (paused is not null)
            {
                _ = await Record.ExceptionAsync(() => paused.WaitAsync(observation.Token));
            }
        }
    }

    [Fact]
    public async Task Pause_CommittedPauseWithoutAcknowledgement_CancellationReleasesLeaseBeforeRetirement()
    {
        await using var session = await StartAsync(pauseMode: "hold");
        using var observation = new CancellationTokenSource(ObservationTimeout);
        using var cancellation = new CancellationTokenSource();
        var operation = session.PauseThroughRegistryAsync(7, cancellation.Token);
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync("pause-response-gated", observation.Token);
            await WaitForCommittedWriteAsync(session, observation.Token);
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            AssertRetired(probe);
            Assert.Equal(0, session.PendingRequestCount);
            Assert.Single(await RequestsAsync(session), static entry => entry.Command == "pause");
        }
        finally
        {
            session.Fixture.ReleasePauseResponse();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Fact]
    public async Task Pause_CommittedAcknowledgementBeforeCallerCancellation_PreservesSessionWithoutReplay()
    {
        await using var session = await StartAsync(pauseMode: "hold");
        using var observation = new CancellationTokenSource(ObservationTimeout);
        using var cancellation = new CancellationTokenSource();
        var probe = await session.PauseThroughRegistryAsync(7, cancellation.Token,
            invokePause: async invoke =>
            {
                var pending = invoke("pause-ack-before-cancel", cancellation.Token);
                await session.Fixture.WaitForTranscriptKindAsync("pause-response-gated", observation.Token);
                await WaitForCommittedWriteAsync(session, observation.Token);
                var request = Assert.Single(await RequestsAsync(session), static entry => entry.Command == "pause");
                Assert.NotNull(request.Sequence);
                await session.DispatchPauseAcknowledgementThenCancelAsync(request.Sequence.Value, cancellation.Cancel, observation.Token);
                return await pending.WaitAsync(observation.Token);
            },
            beforeCancellationInspection: () =>
            {
                Assert.True(session.IsUsable);
                Assert.False(session.IsExecutionClosing);
                Assert.Equal(0, session.PendingRequestCount);
                return Task.CompletedTask;
            });

        AssertRetained(probe);
        var content = Content(probe.Result);
        Assert.Equal(probe.CancellationObserved ? "debug_state_success" : "pause_acknowledged", content.GetProperty("kind").GetString());
        Assert.Single(await RequestsAsync(session), static entry => entry.Command == "pause");
    }

    [Fact]
    public async Task Pause_DiscoveryAndAcknowledgementShareOneAbsoluteDeadline()
    {
        var clock = new FakeTimeProvider();
        await using var session = await StartAsync(pauseMode: "hold", threadsMode: "hold", clock: clock);
        using var observation = new CancellationTokenSource(ObservationTimeout);
        var operation = session.PauseThroughRegistryAsync(null, observation.Token);
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync("threads-response-gated", observation.Token);
            await WaitForCommittedWriteAsync(session, observation.Token);
            clock.Advance(TimeSpan.FromSeconds(20));
            Assert.False(operation.IsCompleted);
            session.Fixture.ReleaseThreadsResponse();
            await session.Fixture.WaitForTranscriptKindAsync("pause-response-gated", observation.Token);
            await WaitForCommittedWriteAsync(session, observation.Token);
            clock.Advance(TimeSpan.FromSeconds(9));
            Assert.False(operation.IsCompleted, "Discovery must leave only the remainder of the original deadline for pause ACK.");
            clock.Advance(TimeSpan.FromSeconds(1));
            var probe = await operation.WaitAsync(observation.Token);

            var content = Content(probe.Result);
            Assert.True(probe.Result.IsError == true);
            Assert.Equal("dap_pause_timeout", content.GetProperty("kind").GetString());
            Assert.Equal("DAP_PAUSE_TIMEOUT", content.GetProperty("error").GetString());
            AssertRetired(probe);
            var requests = await RequestsAsync(session);
            Assert.Single(requests, static entry => entry.Command == "threads");
            Assert.Single(requests, static entry => entry.Command == "pause");
        }
        finally
        {
            session.Fixture.ReleaseThreadsResponse();
            session.Fixture.ReleasePauseResponse();
            _ = await Record.ExceptionAsync(() => operation.WaitAsync(observation.Token));
        }
    }

    [Fact]
    public async Task Pause_HostShutdownClosesAdmissionAndDrainsAcknowledgedLeaseBeforeCleanup()
    {
        await using var session = await StartAsync(pauseMode: "hold");
        using var observation = new CancellationTokenSource(ObservationTimeout);
        await using var hosted = await session.StartHostedPauseLeaseAsync(7, observation.Token);
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync("pause-response-gated", observation.Token);
            await WaitForCommittedWriteAsync(session, observation.Token);
            var shutdown = hosted.StopAsync(observation.Token);
            await hosted.AdmissionClosed.WaitAsync(observation.Token);

            Assert.False(shutdown.IsCompleted);
            Assert.False(hosted.Operation.IsCompleted);
            Assert.Null(hosted.LeaseCountAtStop);
            Assert.DoesNotContain(await RequestsAsync(session), static entry => entry.Command is "terminate" or "disconnect");
            session.Fixture.ReleasePauseResponse();
            Assert.Equal("pause_acknowledged", Content(await hosted.Operation.WaitAsync(observation.Token)).GetProperty("kind").GetString());
            await shutdown.WaitAsync(observation.Token);

            Assert.Equal(0, hosted.LeaseCountAtStop);
            Assert.Equal(0, session.PendingRequestCount);
            var requests = await RequestsAsync(session);
            Assert.Single(requests, static entry => entry.Command == "pause");
            Assert.Single(requests, static entry => entry.Command == "terminate");
            Assert.Single(requests, static entry => entry.Command == "disconnect");
        }
        finally
        {
            session.Fixture.ReleasePauseResponse();
        }
    }

    [Fact]
    public async Task Pause_FencesBeforeContinueFinalization_ContinueCannotReopenClosingAdmission()
    {
        await using var session = await StartAsync(pauseMode: "hold", stopped: true, continueMode: "continue-ack-only");
        using var observation = new CancellationTokenSource(ObservationTimeout);
        using var continueCancellation = new CancellationTokenSource();
        using var pauseCancellation = new CancellationTokenSource();
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        var continued = session.ContinueAsync(1, continueCancellation.Token);
        Task<StepOverRegistryProbe>? paused = null;
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync("continue-acknowledged", observation.Token);
            await session.WaitForReaderBarrierAsync(observation.Token);
            Assert.False(continued.IsCompleted);
            session.ArmDapWriteAtHeaderEntryBarrier(prefixBytes: 1);
            var continueFinalizedAfterFence = false;
            paused = session.PauseThroughRegistryAsync(7, pauseCancellation.Token,
                beforeStop: async () =>
                {
                    Assert.True(session.IsExecutionClosing, "The uncertain pause must fence admission before its slot starts cleanup.");
                    continueCancellation.Cancel();
                    await Assert.ThrowsAnyAsync<OperationCanceledException>(() => continued).WaitAsync(observation.Token);
                    continueFinalizedAfterFence = true;
                    Assert.True(session.IsExecutionClosing, "A later acknowledged-continue finalization must never reopen the pause fence.");
                });
            await session.WaitForDapHeaderEntryAsync(observation.Token);
            pauseCancellation.Cancel();
            var probe = await paused.WaitAsync(observation.Token);

            Assert.True(continueFinalizedAfterFence);
            Assert.True(probe.CancellationObserved);
            AssertRetired(probe);
            Assert.Single(await RequestsAsync(session), static entry => entry.Command == "continue");
            Assert.DoesNotContain(await RequestsAsync(session), static entry => entry.Command == "pause");
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            continueCancellation.Cancel();
            pauseCancellation.Cancel();
            _ = await Record.ExceptionAsync(() => continued);
            if (paused is not null)
            {
                _ = await Record.ExceptionAsync(() => paused);
            }
        }
    }

    private static Task<NetCoreDbgSessionContractDriver> StartAsync(
        string pauseMode = "ack-only", string threadsMode = "success", bool stopped = false,
        string continueMode = "success", TimeProvider? clock = null) =>
        NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(SuppressLifecycleEvents: !stopped, LifecycleMode: "all-stop",
                PauseResponseMode: pauseMode, ThreadsResponseMode: threadsMode, NextResponseMode: continueMode),
            "D:\\fixtures\\program.dll", TimeSpan.FromSeconds(2), RequestTimeout, TimeSpan.FromMilliseconds(300),
            CancellationToken.None, stepOverTimeProvider: clock);

    private static async Task WaitForCommittedWriteAsync(NetCoreDbgSessionContractDriver session, CancellationToken cancellationToken)
    {
        await session.HoldDapWriteAsync(cancellationToken);
        session.ReleaseDapWrite();
    }

    private static JsonElement Content(CallToolResult result)
    {
        Assert.Equal("complete", result.ResultType);
        var content = Assert.IsType<JsonElement>(result.StructuredContent);
        var text = Assert.IsType<TextContentBlock>(Assert.Single(result.Content));
        Assert.Equal(content.GetRawText(), text.Text);
        return content;
    }

    private static void AssertRetained(StepOverRegistryProbe probe)
    {
        Assert.True(probe.TokenRetained);
        Assert.True(probe.SlotRetained);
        Assert.True(probe.AdapterAlive);
        Assert.Null(probe.LeaseReleasedBeforeRemoval);
    }

    private static void AssertRetired(StepOverRegistryProbe probe)
    {
        Assert.False(probe.TokenRetained);
        Assert.False(probe.SlotRetained);
        Assert.False(probe.AdapterAlive);
        Assert.True(probe.LeaseReleasedBeforeRemoval == true);
        Assert.Null(probe.RetainedThreadsResult);
    }

    private static async Task<FixtureTranscriptEntry[]> RequestsAsync(NetCoreDbgSessionContractDriver session) =>
        (await session.Fixture.ReadTranscriptAsync()).Where(static entry => entry.Kind == "request").ToArray();
}
