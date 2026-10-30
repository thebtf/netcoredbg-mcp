using System.ComponentModel;
using System.Collections.Immutable;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using System.Text;
using System.Text.Json;
using System.Diagnostics;
using Microsoft.Extensions.Time.Testing;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

[CollectionDefinition(Name, DisableParallelization = true)]
public sealed class NetCoreDbgSessionProcessCollection
{
    public const string Name = "NetCoreDbgSessionProcess";
}

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NetCoreDbgSessionTests
{
    private static readonly TimeSpan InitializeTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan StopTimeout = TimeSpan.FromMilliseconds(300);
    private static readonly ImmutableArray<string?> ExpectedLifecycleEvents = ImmutableArray.Create<string?>(
        "stopped",
        "continued",
        "exited",
        "terminated");

    [Theory]
    [InlineData("primary")]
    [InlineData("linked-relative")]
    [InlineData("linked-absolute")]
    [InlineData("invalid-gitdir")]
    [InlineData("empty-commondir")]
    [InlineData("missing-common")]
    public void RepositoryLayout_ScratchUsesSharedGitOwnerOrFailsClosed(string layout)
    {
        var scratchDirectory = Path.Combine(RepositoryLayout.ScratchRoot, $"repository-layout-{Guid.NewGuid():N}");
        var coordinationRoot = Path.Combine(scratchDirectory, "primary repository");
        var commonDirectory = Path.Combine(coordinationRoot, ".git");
        var sourceRoot = layout == "primary" ? coordinationRoot : Path.Combine(scratchDirectory, "linked source");
        try
        {
            Directory.CreateDirectory(Path.Combine(commonDirectory, "objects"));
            Directory.CreateDirectory(sourceRoot);
            if (layout != "primary")
            {
                var gitDirectory = Path.Combine(commonDirectory, "worktrees", "linked-test");
                Directory.CreateDirectory(gitDirectory);
                File.WriteAllText(Path.Combine(sourceRoot, ".git"), "gitdir: "
                    + (layout == "linked-absolute" ? gitDirectory : Path.GetRelativePath(sourceRoot, gitDirectory)) + "\n");
                File.WriteAllText(Path.Combine(gitDirectory, "commondir"),
                    layout == "linked-absolute" ? commonDirectory : Path.GetRelativePath(gitDirectory, commonDirectory));
                switch (layout)
                {
                    case "invalid-gitdir":
                        File.WriteAllText(Path.Combine(sourceRoot, ".git"), "not a Git directory pointer");
                        break;
                    case "empty-commondir":
                        File.WriteAllText(Path.Combine(gitDirectory, "commondir"), " \r\n");
                        break;
                    case "missing-common":
                        File.WriteAllText(Path.Combine(gitDirectory, "commondir"), commonDirectory + "-missing");
                        break;
                }
            }

            if (layout is "invalid-gitdir" or "empty-commondir" or "missing-common")
            {
                Assert.Throws<InvalidDataException>(() => RepositoryLayout.ResolveScratchCoordinationRoot(sourceRoot));
            }
            else
            {
                Assert.Equal(coordinationRoot, RepositoryLayout.ResolveScratchCoordinationRoot(sourceRoot));
            }

            Assert.False(Directory.Exists(Path.Combine(sourceRoot, ".agent")));
        }
        finally
        {
            if (Directory.Exists(scratchDirectory))
            {
                Directory.Delete(scratchDirectory, recursive: true);
            }
        }
    }

    [Fact]
    public void UnixProcessGroupOwnership_UsesLiveGuardianTerminationControl() =>
        NetCoreDbgSessionContractDriver.AssertUnixGuardianOwnershipContract();

    [Fact]
    public async Task StartAsync_StartsDebuggerWithExactVscodeInterpreterArgument()
    {
        await using var session = await StartAsync(new FixtureConfiguration());
        var startup = Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry => entry.Kind == "startup");
        Assert.Equal("[\"--interpreter=vscode\"]", startup.Arguments);
    }

    [Fact]
    public async Task StartAsync_UsesUtf8ByteLengthForNonAsciiProgramPath()
    {
        const string program = "D:\\fixtures\\программа-雪.dll";
        await using var session = await StartAsync(new FixtureConfiguration(), program);
        var launch = Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry => entry.Command == "launch");
        var rawPayload = Assert.IsType<string>(launch.RawPayload);
        using var payload = JsonDocument.Parse(rawPayload);
        var payloadByteCount = Assert.IsType<int>(launch.PayloadByteCount);
        var contentLength = Assert.IsType<int>(launch.ContentLength);

        Assert.Contains(program.Replace("\\", "\\\\", StringComparison.Ordinal), rawPayload, StringComparison.Ordinal);
        Assert.DoesNotContain("\\u", rawPayload, StringComparison.Ordinal);
        Assert.Equal(program, payload.RootElement.GetProperty("arguments").GetProperty("program").GetString());
        Assert.Equal(Encoding.UTF8.GetByteCount(rawPayload), payloadByteCount);
        Assert.Equal(payloadByteCount, contentLength);
        Assert.True(rawPayload.Length < payloadByteCount, "The non-ASCII raw payload must occupy more UTF-8 bytes than UTF-16 characters.");
    }

    [Fact]
    public async Task StartAsync_ConcurrentSessionsInheritOnlyTheirStandardHandles()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        using var probe = InheritablePipeProbe.Create();
        var endOfPipe = probe.ReadEndAsync();
        await using (var sessions = await NetCoreDbgSessionContractDriver.StartConcurrentAsync(
            new FixtureConfiguration(),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            RequestTimeout,
            StopTimeout,
            CancellationToken.None))
        {
            probe.CloseWriter();
            Assert.Equal(0, await endOfPipe.WaitAsync(TimeSpan.FromSeconds(1)));
        }
    }

    [Fact]
    public async Task StartAsync_IgnoresUnmatchedResponseAndRejectsEarlyInitializedEvent()
    {
        var transcript = new List<FixtureTranscriptEntry>();
        NetCoreDbgSessionContractDriver? session = null;
        var failure = await Record.ExceptionAsync(async () => session = await NetCoreDbgSessionContractDriver.StartWithInitializeContinuationGateAsync(
            new FixtureConfiguration(
                InitializedBeforeCorrectInitializeResponse: true,
                SuppressInitializedAfterInitializeResponse: true),
            "D:\\fixtures\\program.dll",
            TimeSpan.FromMilliseconds(250),
            RequestTimeout,
            StopTimeout,
            CancellationToken.None,
            transcript));

        if (session is not null)
        {
            transcript.AddRange(await session.Fixture.ReadTranscriptAsync());
            await ((IAsyncDisposable)session).DisposeAsync();
        }

        Assert.IsType<TimeoutException>(failure);
        var unmatched = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "unmatched-response");
        var earlyInitialized = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "early-initialized-event");
        var initializeResponse = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "initialize-response");
        Assert.True(unmatched >= 0 && unmatched < earlyInitialized && earlyInitialized < initializeResponse,
            $"The unmatched response and early initialized event must precede the correlated initialize response. Indices: unmatched={unmatched}, earlyInitialized={earlyInitialized}, initializeResponse={initializeResponse}. Failure: {failure}. Transcript: {JsonSerializer.Serialize(transcript)}");
        Assert.DoesNotContain(transcript, entry => entry.Kind == "request" && entry.Command == "launch");
    }

    [Fact]
    public async Task StartAsync_AcceptsCapabilitiesEventBeforeInitializeResponse()
    {
        var malformedTranscript = new List<FixtureTranscriptEntry>();
        NetCoreDbgSessionContractDriver? malformedSession = null;
        Exception? malformedFailure;
        try
        {
            malformedFailure = await Record.ExceptionAsync(async () =>
            {
                malformedSession = await StartAsync(
                    new FixtureConfiguration(MalformedCapabilitiesEvent: true),
                    failedTranscript: malformedTranscript);
            });
        }
        finally
        {
            if (malformedSession is not null)
            {
                try
                {
                    malformedTranscript.AddRange(await malformedSession.Fixture.ReadTranscriptAsync());
                }
                finally
                {
                    await ((IAsyncDisposable)malformedSession).DisposeAsync();
                }
            }
        }

        Assert.IsType<InvalidDataException>(malformedFailure);
        Assert.Contains(malformedTranscript, entry => entry.Kind == "capabilities-event");
        Assert.DoesNotContain(malformedTranscript, entry => entry.Kind == "request" && entry.Command == "launch");

        await using var session = await StartAsync(new FixtureConfiguration());
        var transcript = await session.Fixture.ReadTranscriptAsync();
        var capabilities = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "capabilities-event");
        var initializeResponse = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "initialize-response");
        Assert.NotNull(session);
        Assert.True(capabilities >= 0 && capabilities < initializeResponse, "capabilities must precede the correlated initialize response.");
        Assert.True(session.CapabilitiesObserved, "The capabilities event body must be observed before startup completes.");
    }

    [Fact]
    public async Task StartAsync_WaitsForInitializeResponseAndInitializedBeforeLaunch()
    {
        await using var session = await StartAsync(new FixtureConfiguration(SupportsConfigurationDone: true));
        var transcript = await session.Fixture.ReadTranscriptAsync();
        var beforeResponse = Assert.Single(transcript, entry => entry.Kind == "initialize-gate" && entry.Stage == "before-initialize-response");
        var initializeResponse = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "initialize-response");
        var beforeInitialized = Assert.Single(transcript, entry => entry.Kind == "initialize-gate" && entry.Stage == "before-initialized-event");
        var beforeInitializedIndex = Array.FindIndex(transcript.ToArray(), entry => entry == beforeInitialized);
        var initialized = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "initialized-event");
        var launch = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "request" && entry.Command == "launch");

        Assert.Null(beforeResponse.Command);
        Assert.Null(beforeInitialized.Command);
        Assert.True(initializeResponse >= 0 && initializeResponse < beforeInitializedIndex
            && beforeInitializedIndex < initialized && initialized < launch,
            "The standard path must send the correlated response, observe an empty initialized gate, emit initialized, then launch.");
    }

    [Fact]
    public async Task StartAsync_SendsConfigurationDoneOnlyWhenAdvertised()
    {
        await using var advertised = await StartAsync(new FixtureConfiguration(SupportsConfigurationDone: true));
        Assert.Contains(await advertised.Fixture.ReadTranscriptAsync(), entry => entry.Command == "configurationDone");

        await using var omitted = await StartAsync(new FixtureConfiguration(SupportsConfigurationDone: false));
        Assert.DoesNotContain(await omitted.Fixture.ReadTranscriptAsync(), entry => entry.Command == "configurationDone");
    }

    [Fact]
    public async Task StartAsync_TreatsOmittedInitializeResponseBodyAsEmptyCapabilities()
    {
        await using var session = await StartAsync(new FixtureConfiguration(
            SupportsConfigurationDone: false,
            OmitInitializeResponseBody: true));

        var transcript = await session.Fixture.ReadTranscriptAsync();
        var initializeResponse = Assert.Single(transcript, entry => entry.Kind == "initialize-response");
        Assert.True(initializeResponse.BodyOmitted is true, "The controlled adapter must omit, not null-serialize, the initialize response body.");
        Assert.DoesNotContain(transcript, entry => entry.Command == "configurationDone");
    }

    [Fact]
    public async Task StartAsync_AppliesInitializeBaselineBeforeConfigurationDoneCapabilityDelta()
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithInitializeContinuationGateAsync(
            new FixtureConfiguration(
                SupportsConfigurationDone: false,
                EnableConfigurationDoneAfterInitialization: true,
                HoldConfigurationDoneCapabilityDeltaUntilRelease: true),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            RequestTimeout,
            StopTimeout,
            CancellationToken.None);

        var transcript = await session.Fixture.ReadTranscriptAsync();
        var initializeResponse = Assert.Single(transcript, entry => entry.Kind == "initialize-response");
        var delta = Assert.Single(transcript, entry => entry.Kind == "configuration-done-capabilities-delta-event");
        var initializeResponseIndex = Array.FindIndex(transcript.ToArray(), entry => entry == initializeResponse);
        var deltaIndex = Array.FindIndex(transcript.ToArray(), entry => entry == delta);
        var initialized = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "initialized-event");
        var configurationDone = Array.FindIndex(transcript.ToArray(), entry => entry.Kind == "configuration-done");

        Assert.False(initializeResponse.SupportsConfigurationDoneRequest ?? true, "The initialize baseline must disable configurationDone before the delta.");
        Assert.True(delta.SupportsConfigurationDoneRequest is true, "The capability delta must enable configurationDone before startup continues.");
        Assert.True(initializeResponseIndex >= 0 && initializeResponseIndex < deltaIndex && deltaIndex < initialized && initialized < configurationDone,
            "The reader must apply the response baseline, then the delta, before releasing the held caller continuation.");
        Assert.Equal(1, Requests(transcript).Count(command => command == "configurationDone"));
    }

    [Fact]
    public async Task State_TracksStoppedContinuedExitedAndTerminatedEvents()
    {
        await using (var quiet = await StartAsync(new FixtureConfiguration(SuppressLifecycleEvents: true)))
        {
            await Task.Delay(TimeSpan.FromMilliseconds(350));
            Assert.Equal(new DapSessionSnapshot(null, null, null), quiet.State);
        }

        var stopReason = $"controlled-stop-причина-雪-{Guid.NewGuid():N}";
        const int exitCode = 47_003;
        await using var emitting = await StartAsync(new FixtureConfiguration(StopReason: stopReason, ExitCode: exitCode));
        string?[] lifecycleEvents = [];
        var startedAt = Stopwatch.GetTimestamp();
        while (true)
        {
            var transcript = await emitting.Fixture.ReadTranscriptAsync();
            var launchReleased = Array.FindLastIndex(transcript.ToArray(), static entry => entry.Kind == "launch-released");
            lifecycleEvents = launchReleased < 0
                ? []
                : transcript.Skip(launchReleased + 1)
                    .Where(static entry => entry.Kind == "event")
                    .Select(static entry => entry.Event)
                    .ToArray();
            if (lifecycleEvents.Contains("terminated"))
            {
                break;
            }

            if (Stopwatch.GetElapsedTime(startedAt) >= TimeSpan.FromSeconds(2))
            {
                Assert.Fail($"Timed out waiting for the controlled lifecycle transcript. Observed: [{string.Join(", ", lifecycleEvents.Select(static value => value ?? "<missing>"))}].");
                return;
            }

            await Task.Delay(TimeSpan.FromMilliseconds(15));
        }

        Assert.Equal(ExpectedLifecycleEvents, lifecycleEvents);
        Assert.Equal(new DapSessionSnapshot("terminated", stopReason, exitCode), emitting.State);
    }
    [Fact]
    public async Task GetCallStack_ContinuedDuringAdmission_WritesBeforeLifecycleTransition()
    {
        await using var session = await StartAsync(new FixtureConfiguration(
            SupportsDelayedStackTraceLoading: true,
            LifecycleMode: "all-stop-held-continued"));
        using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await session.Fixture.WaitForEventAsync("stopped", cancellation.Token);
        await session.HoldDapWriteAsync(cancellation.Token);
        var writeHeld = true;
        var callStack = session.GetCallStackIsRefusedAsync(1, 0, 20, cancellation.Token);
        try
        {
            await session.WaitForPendingStackTraceAsync(cancellation.Token);
            var lifecycleTransition = WaitUntilAsync(
                () => Task.FromResult(session.State.Event == "continued"),
                TimeSpan.FromSeconds(2));
            session.Fixture.ReleaseGracefulShutdown();
            await session.Fixture.WaitForEventAsync("continued", cancellation.Token);
            var transitionedBeforeWrite = await Task.WhenAny(
                lifecycleTransition,
                Task.Delay(TimeSpan.FromMilliseconds(200), cancellation.Token)) == lifecycleTransition;
            session.ReleaseDapWrite();
            writeHeld = false;

            Assert.True(await callStack);
            await lifecycleTransition;
            Assert.False(transitionedBeforeWrite,
                "A continued lifecycle transition must not complete before the pending stackTrace write is admitted.");
        }
        finally
        {
            if (writeHeld)
            {
                session.ReleaseDapWrite();
            }
        }
    }


    [Fact]
    public async Task StepOver_DelayedAcknowledgementSharesPostWriteDeadlineAndDrainsThroughRegistry()
    {
        var clock = new FakeTimeProvider();
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop", NextResponseMode: "hold-ack-no-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            TimeSpan.FromSeconds(30),
            StopTimeout,
            CancellationToken.None,
            stepOverTimeProvider: clock);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        var operation = session.StepOverThroughRegistryAsync(1, observation.Token);
        await session.Fixture.WaitForNextRequestAsync(observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);

        clock.Advance(TimeSpan.FromSeconds(20));
        Assert.False(operation.IsCompleted, "The next acknowledgement is still held before the shared deadline.");
        session.Fixture.ReleaseNextResponse();
        await session.Fixture.WaitForTranscriptKindAsync("next-acknowledged", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);

        clock.Advance(TimeSpan.FromSeconds(9));
        Assert.False(operation.IsCompleted, "The observation phase still has one second of the original post-write deadline.");
        clock.Advance(TimeSpan.FromSeconds(1));
        var probe = await operation.WaitAsync(observation.Token);

        Assert.Equal("complete", probe.Result.ResultType);
        Assert.False(probe.Result.IsError == true);
        var content = Assert.IsType<JsonElement>(probe.Result.StructuredContent);
        Assert.Equal("step_over_timed_out", content.GetProperty("kind").GetString());
        Assert.Equal(1, content.GetProperty("threadId").GetInt32());
        Assert.Equal(30, content.GetProperty("timeoutSeconds").GetInt32());
        Assert.Equal(3, content.EnumerateObject().Count());
        var text = Assert.IsType<ModelContextProtocol.Protocol.TextContentBlock>(Assert.Single(probe.Result.Content));
        Assert.Equal(content.GetRawText(), text.Text);
        Assert.False(probe.TokenRetained);
        Assert.False(probe.SlotRetained);
        Assert.False(probe.AdapterAlive);
        var transcript = await session.Fixture.ReadTranscriptAsync();
        Assert.Single(transcript, static entry => entry.Command == "next");
        Assert.Single(transcript, static entry => entry.Command == "terminate");
        Assert.Single(transcript, static entry => entry.Command == "disconnect");
    }

    [Theory]
    [InlineData("write-gate")]
    [InlineData("pre-header")]
    public async Task StepOver_CancellationBeforeWriterEntry_PreservesRegisteredSessionAndLiveTransport(string barrier)
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(SupportsDelayedStackTraceLoading: true, LifecycleMode: "all-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            RequestTimeout,
            StopTimeout,
            CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        var writeHeld = barrier == "write-gate";
        if (writeHeld)
        {
            await session.HoldDapWriteAsync(observation.Token);
        }
        else
        {
            session.ArmDapWriteBeforeHeaderBarrier();
        }
        void ReleaseBarrier()
        {
            if (writeHeld)
            {
                session.ReleaseDapWrite();
                writeHeld = false;
            }
            session.ReleaseDapWriteBarrier();
        }
        var writesBefore = session.DapWriteCalls;
        using var cancellation = new CancellationTokenSource();
        var operation = session.StepOverThroughRegistryAsync(1, cancellation.Token, inspectCancellation: true,
            beforeCancellationInspection: async () =>
            {
                Assert.False(session.StepOverDrainRequired);
                Assert.False(session.HasActiveStepOver);
                Assert.Equal(0, session.PendingRequestCount);
                Assert.Equal(writesBefore, session.DapWriteCalls);
                Assert.True(session.IsUsable);
                ReleaseBarrier();
                Assert.True(await session.GetCallStackIsRefusedAsync(1, 0, 20, observation.Token));
                await session.StepOverAsync(1, observation.Token);
            });
        try
        {
            if (barrier == "write-gate")
            {
                await session.WaitForPendingNextAsync(observation.Token);
            }
            else
            {
                await session.WaitForDapPreHeaderWriteAsync(observation.Token);
            }
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            Assert.True(probe.TokenRetained);
            Assert.True(probe.SlotRetained);
            Assert.True(probe.AdapterAlive);
            Assert.Null(probe.LeaseReleasedBeforeRemoval);
            Assert.Equal("debug_state_success", Assert.IsType<JsonElement>(probe.Result.StructuredContent).GetProperty("kind").GetString());
            var threads = Assert.IsType<ModelContextProtocol.Protocol.CallToolResult>(probe.RetainedThreadsResult);
            Assert.Equal("complete", threads.ResultType);
            Assert.False(threads.IsError == true);
            var content = Assert.IsType<JsonElement>(threads.StructuredContent);
            Assert.Equal("threads_success", content.GetProperty("kind").GetString());
            var text = Assert.IsType<ModelContextProtocol.Protocol.TextContentBlock>(Assert.Single(threads.Content));
            Assert.Equal(content.GetRawText(), text.Text);
            var transcript = await session.Fixture.ReadTranscriptAsync();
            Assert.Equal(2, transcript.Count(static entry => entry.Command == "threads"));
            Assert.DoesNotContain(transcript, static entry => entry.Command is "next" or "stackTrace");
        }
        finally
        {
            ReleaseBarrier();
        }
    }

    [Theory]
    [InlineData(false, false)]
    [InlineData(false, true)]
    [InlineData(true, false)]
    [InlineData(true, true)]
    public async Task Continue_ResponseFaultAndTerminalBeforeOutcomeWait_PreservesReaderOrder(bool terminalFirst, bool invalidSuccess)
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop", NextResponseMode: "continue-stop-after-injected-ack"),
            "D:\\fixtures\\program.dll", InitializeTimeout, RequestTimeout, StopTimeout, CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        session.ArmDapWriteAfterFlushBarrier();
        try
        {
            var probe = await session.ContinueThroughRegistryAsync(1, observation.Token,
                invokeContinue: async invoke =>
                {
                    var pending = invoke($"continue-fault-terminal-{terminalFirst}-{invalidSuccess}", observation.Token);
                    await session.WaitForDapFlushAsync(observation.Token);
                    await session.Fixture.WaitForTranscriptKindAsync("continue-ack-gated", observation.Token);
                    var request = Assert.Single(await session.Fixture.ReadTranscriptAsync(), static entry => entry.Command == "continue");
                    Assert.NotNull(request.Sequence);
                    await session.DispatchContinueResponseFaultAndTerminalAsync(request.Sequence.Value, terminalFirst, invalidSuccess, observation.Token);
                    return await pending.WaitAsync(observation.Token);
                });

            Assert.Equal("complete", probe.Result.ResultType);
            Assert.Equal(!terminalFirst, probe.Result.IsError == true);
            var content = Assert.IsType<JsonElement>(probe.Result.StructuredContent);
            Assert.Equal(2, content.EnumerateObject().Count());
            if (terminalFirst)
            {
                Assert.Equal("continue_terminal", content.GetProperty("kind").GetString());
                Assert.Equal("terminated", content.GetProperty("terminal").GetString());
                Assert.False(content.TryGetProperty("exitCode", out _));
            }
            else
            {
                Assert.Equal("dap_continue_protocol_error", content.GetProperty("kind").GetString());
                Assert.Equal("DAP_CONTINUE_PROTOCOL_ERROR", content.GetProperty("error").GetString());
            }
            var text = Assert.IsType<ModelContextProtocol.Protocol.TextContentBlock>(Assert.Single(probe.Result.Content));
            Assert.Equal(content.GetRawText(), text.Text);
            Assert.False(probe.TokenRetained);
            Assert.False(probe.SlotRetained);
            Assert.False(probe.AdapterAlive);
            Assert.True(probe.LeaseReleasedBeforeRemoval == true);
            Assert.Equal(0, probe.ActiveRequestCount);
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Continue_PostAckContinuedBeforeFinalization_DiscardsStaleStopAndAwaitsFreshStop(bool stoppedBeforeAcknowledgement)
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(SupportsDelayedStackTraceLoading: true, LifecycleMode: "all-stop", NextResponseMode: "continue-stop-after-injected-ack"),
            "D:\\fixtures\\program.dll", InitializeTimeout, RequestTimeout, StopTimeout, CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        try
        {
            var probe = await session.ContinueThroughRegistryAsync(1, observation.Token,
                invokeContinue: async invoke =>
                {
                    var pending = invoke("continue-post-ack-invalidation", observation.Token);
                    await session.Fixture.WaitForTranscriptKindAsync("continue-ack-gated", observation.Token);
                    await session.WaitForReaderBarrierAsync(observation.Token);
                    var request = Assert.Single(await session.Fixture.ReadTranscriptAsync(), static entry => entry.Command == "continue");
                    Assert.NotNull(request.Sequence);
                    await session.DispatchContinueStopThenContinuedAsync(request.Sequence.Value, stoppedBeforeAcknowledgement, observation.Token);

                    // Dispatch order is already fixed; this deadline observes premature completion, not event ordering.
                    await Assert.ThrowsAsync<TimeoutException>(async () =>
                    {
                        _ = await pending.WaitAsync(TimeSpan.FromMilliseconds(250), observation.Token);
                    });

                    session.Fixture.ReleaseNextResponse();
                    var result = await pending.WaitAsync(observation.Token);
                    Assert.Equal("complete", result.ResultType);
                    Assert.False(result.IsError == true);
                    var content = Assert.IsType<JsonElement>(result.StructuredContent);
                    Assert.Equal("continue_stopped", content.GetProperty("kind").GetString());
                    Assert.Equal("breakpoint", content.GetProperty("reason").GetString());
                    Assert.Equal(1, content.GetProperty("threadId").GetInt32());
                    Assert.True(content.GetProperty("allThreadsStopped").GetBoolean());
                    var text = Assert.IsType<ModelContextProtocol.Protocol.TextContentBlock>(Assert.Single(result.Content));
                    Assert.Equal(content.GetRawText(), text.Text);
                    return result;
                });
            Assert.True(probe.TokenRetained);
            Assert.True(probe.SlotRetained);
            Assert.True(probe.AdapterAlive);
        }
        finally
        {
            session.Fixture.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task Continue_AcknowledgedCancellation_DetachesWaiterAndLaterStopRestoresInspection()
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(SupportsDelayedStackTraceLoading: true, LifecycleMode: "all-stop", NextResponseMode: "continue-ack-held-stop"),
            "D:\\fixtures\\program.dll", InitializeTimeout, RequestTimeout, StopTimeout, CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        using var cancellation = new CancellationTokenSource();
        var operation = session.ContinueThroughRegistryAsync(1, cancellation.Token,
            beforeCancellationInspection: async () =>
            {
                Assert.True(session.IsUsable);
                Assert.Equal(0, session.PendingRequestCount);
                Assert.True(await session.GetCallStackIsRefusedAsync(1, 0, 20, observation.Token));
                Assert.DoesNotContain(await session.Fixture.ReadTranscriptAsync(), static entry => entry.Command is "terminate" or "disconnect");
                session.Fixture.ReleaseNextResponse();
                await session.Fixture.WaitForTranscriptKindAsync("continue-later-stop", observation.Token);
                await session.WaitForReaderBarrierAsync(observation.Token);
                Assert.False(await session.GetCallStackIsRefusedAsync(1, 0, 20, observation.Token));
            });
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync("continue-acknowledged", observation.Token);
            await session.WaitForReaderBarrierAsync(observation.Token);
            Assert.False(operation.IsCompleted);
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);
            Assert.True(probe.CancellationObserved);
            Assert.True(probe.TokenRetained);
            Assert.True(probe.SlotRetained);
            Assert.True(probe.AdapterAlive);
            Assert.Null(probe.LeaseReleasedBeforeRemoval);
            Assert.Equal("threads_success", Assert.IsType<JsonElement>(probe.RetainedThreadsResult!.StructuredContent).GetProperty("kind").GetString());
        }
        finally
        {
            session.Fixture.ReleaseNextResponse();
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Continue_CancellationBeforeHeaderOrAfterPartialHeader_RetainsOnlyProvenUnwrittenSession(bool partialHeader)
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(SupportsDelayedStackTraceLoading: true, LifecycleMode: "all-stop", NextResponseMode: "continue-hold"),
            "D:\\fixtures\\program.dll", InitializeTimeout, RequestTimeout, StopTimeout, CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        var writesBefore = session.DapWriteCalls;
        if (partialHeader)
        {
            session.ArmDapWriteAtHeaderEntryBarrier(prefixBytes: 1);
        }
        else
        {
            session.ArmDapWriteBeforeHeaderBarrier();
        }
        using var cancellation = new CancellationTokenSource();
        var operation = session.ContinueThroughRegistryAsync(1, cancellation.Token,
            beforeCancellationInspection: async () =>
            {
                Assert.Equal(0, session.PendingRequestCount);
                session.ReleaseDapWriteBarrier();
                if (!partialHeader)
                {
                    Assert.Equal(writesBefore, session.DapWriteCalls);
                    Assert.True(session.IsUsable);
                    Assert.True(await session.GetCallStackIsRefusedAsync(1, 0, 20, observation.Token));
                }
            });
        try
        {
            if (partialHeader)
            {
                await session.WaitForDapHeaderEntryAsync(observation.Token);
            }
            else
            {
                await session.WaitForDapPreHeaderWriteAsync(observation.Token);
            }
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);
            Assert.True(probe.CancellationObserved);
            Assert.Equal(!partialHeader, probe.TokenRetained);
            Assert.Equal(!partialHeader, probe.SlotRetained);
            Assert.Equal(!partialHeader, probe.AdapterAlive);
            Assert.Equal(partialHeader ? "debug_session_not_found" : "debug_state_success",
                Assert.IsType<JsonElement>(probe.Result.StructuredContent).GetProperty("kind").GetString());
            if (partialHeader)
            {
                Assert.Equal(1, session.ArmedHeaderUnderlyingWriteCalls);
                Assert.Equal(1L, session.ArmedHeaderBytesForwarded);
                Assert.True(probe.LeaseReleasedBeforeRemoval == true);
                Assert.Null(probe.RetainedThreadsResult);
            }
            else
            {
                Assert.Null(probe.LeaseReleasedBeforeRemoval);
                Assert.Equal("threads_success", Assert.IsType<JsonElement>(probe.RetainedThreadsResult!.StructuredContent).GetProperty("kind").GetString());
            }
            Assert.DoesNotContain(await session.Fixture.ReadTranscriptAsync(), static entry => entry.Command is "continue" or "stackTrace");
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task GetCallStack_OptInCancellation_BeforeHeaderPreservesSessionButPartialHeaderDrains(bool partialHeader)
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(SupportsDelayedStackTraceLoading: true, LifecycleMode: "all-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            RequestTimeout,
            StopTimeout,
            CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        var writesBefore = session.DapWriteCalls;
        if (partialHeader)
        {
            session.ArmDapWriteAtHeaderEntryBarrier(prefixBytes: 1);
        }
        else
        {
            session.ArmDapWriteBeforeHeaderBarrier();
        }
        using var cancellation = new CancellationTokenSource();
        var operation = session.GetCallStackThroughRegistryAsync(1, cancellation.Token,
            beforeCancellationInspection: () =>
            {
                Assert.Equal(0, session.PendingRequestCount);
                if (!partialHeader)
                {
                    Assert.Equal(writesBefore, session.DapWriteCalls);
                    Assert.True(session.IsUsable);
                }
                session.ReleaseDapWriteBarrier();
                return Task.CompletedTask;
            });
        try
        {
            if (partialHeader)
            {
                await session.WaitForDapHeaderEntryAsync(observation.Token);
            }
            else
            {
                await session.WaitForDapPreHeaderWriteAsync(observation.Token);
            }
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);
            Assert.True(probe.CancellationObserved);
            Assert.Equal(!partialHeader, probe.TokenRetained);
            Assert.Equal(!partialHeader, probe.SlotRetained);
            Assert.Equal(!partialHeader, probe.AdapterAlive);
            Assert.Equal(0, probe.ActiveRequestCount);
            var content = Assert.IsType<JsonElement>(probe.Result.StructuredContent);
            Assert.Equal(partialHeader ? "debug_session_not_found" : "debug_state_success", content.GetProperty("kind").GetString());
            if (partialHeader)
            {
                Assert.Equal(1, session.ArmedHeaderUnderlyingWriteCalls);
                Assert.Equal(1L, session.ArmedHeaderBytesForwarded);
                Assert.True(probe.LeaseReleasedBeforeRemoval == true);
                Assert.Null(probe.RetainedThreadsResult);
            }
            else
            {
                Assert.Null(probe.LeaseReleasedBeforeRemoval);
                var threads = Assert.IsType<ModelContextProtocol.Protocol.CallToolResult>(probe.RetainedThreadsResult);
                Assert.False(threads.IsError == true);
                Assert.Equal("threads_success", Assert.IsType<JsonElement>(threads.StructuredContent).GetProperty("kind").GetString());
            }
            Assert.DoesNotContain(await session.Fixture.ReadTranscriptAsync(), static entry => entry.Command == "stackTrace");
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
        }
    }

    [Theory]
    [InlineData("zero", 0, false, false)]
    [InlineData("header-prefix", 1, false, false)]
    [InlineData("opaque-oce", 1, true, false)]
    [InlineData("cancel-on-entry", 0, false, true)]
    public async Task StepOver_CancellationAfterWriterEntry_ZeroPartialOrUnknown_DrainsRegisteredSession(
        string writeMode, int prefixBytes, bool opaqueCancellation, bool cancelOnEntry)
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            RequestTimeout,
            StopTimeout,
            CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        using var cancellation = new CancellationTokenSource();
        session.ArmDapWriteAtHeaderEntryBarrier(prefixBytes, opaqueCancellation, cancelOnEntry ? cancellation.Cancel : null);
        var operation = session.StepOverThroughRegistryAsync(1, cancellation.Token, inspectCancellation: true,
            beforeCancellationInspection: () =>
            {
                Assert.False(session.HasActiveStepOver);
                Assert.Equal(0, session.PendingRequestCount);
                return Task.CompletedTask;
            });
        try
        {
            await session.WaitForDapHeaderEntryAsync(observation.Token);
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);
            var content = Assert.IsType<JsonElement>(probe.Result.StructuredContent);
            var diagnostic = $"mode={writeMode}; stdin={session.DapUnderlyingInputStreamType}; underlyingHeaderCalls={session.ArmedHeaderUnderlyingWriteCalls}; forwardedHeaderBytes={session.ArmedHeaderBytesForwarded}; cancellationObserved={probe.CancellationObserved}; tokenRetained={probe.TokenRetained}; slotRetained={probe.SlotRetained}; adapterAlive={probe.AdapterAlive}; state={content.GetRawText()}";

            Assert.True(probe.CancellationObserved, diagnostic);
            Assert.Equal(prefixBytes == 0 ? 0 : 1, session.ArmedHeaderUnderlyingWriteCalls);
            Assert.Equal((long)prefixBytes, session.ArmedHeaderBytesForwarded);
            var transcript = await session.Fixture.ReadTranscriptAsync();
            Assert.DoesNotContain(transcript, static entry => entry.Command == "next");
            Assert.Single(transcript, static entry => entry.Command == "threads");
            Assert.False(probe.TokenRetained, diagnostic);
            Assert.False(probe.SlotRetained, diagnostic);
            Assert.False(probe.AdapterAlive, diagnostic);
            Assert.True(probe.LeaseReleasedBeforeRemoval == true, diagnostic);
            Assert.Null(probe.RetainedThreadsResult);
            Assert.Equal("complete", probe.Result.ResultType);
            Assert.True(probe.Result.IsError == true);
            Assert.Equal("debug_session_not_found", content.GetProperty("kind").GetString());
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", content.GetProperty("error").GetString());
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
        }
    }

    [Fact]
    public async Task StepOver_ConcurrentPreAdmissionCancellation_CannotReleaseWrittenRequestBeforeRealCleanup()
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            RequestTimeout,
            StopTimeout,
            CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        using var cancellationA = new CancellationTokenSource();
        using var cancellationB = new CancellationTokenSource();
        cancellationB.Cancel();
        using var releaseStateGate = new ManualResetEventSlim();
        using var releaseBUsability = new ManualResetEventSlim();
        var bUsabilityObserved = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var stateGateEntered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var stopEntered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var releaseStop = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var disposeEntered = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var releaseDispose = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        Task<ModelContextProtocol.Protocol.CallToolResult>? requestA = null;
        Task<ModelContextProtocol.Protocol.CallToolResult>? requestB = null;
        Task? stateGateHolder = null;
        Task? cancelA = null;
        var exerciseStarted = false;

        void ReleaseBarriers()
        {
            releaseStateGate.Set();
            releaseBUsability.Set();
            session.ReleaseDapWriteBarrier();
            releaseStop.TrySetResult(true);
            releaseDispose.TrySetResult(true);
        }
        using var releaseOnObservationDeadline = observation.Token.Register(ReleaseBarriers);

        async Task AssertRequestAWaitsForCleanupAsync(string stage)
        {
            var completion = await Record.ExceptionAsync(() => requestA!.WaitAsync(TimeSpan.FromMilliseconds(150), observation.Token));
            Assert.True(completion is TimeoutException,
                $"Request A must remain incomplete while real {stage} is held; expected an observation TimeoutException, got {completion?.ToString() ?? "successful completion"}.");
        }

        session.ArmDapWriteAtHeaderEntryBarrier(prefixBytes: 1);
        try
        {
            var probe = await session.StepOverThroughRegistryAsync(1, cancellationA.Token, inspectCancellation: true,
                invokeSteps: async invokeStep =>
                {
                    exerciseStarted = true;
                    try
                    {
                        var invocationA = Task.Factory.StartNew(() => invokeStep("step-over-concurrent-A", cancellationA.Token),
                            CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
                        requestA = invocationA.Unwrap();
                        await session.WaitForDapHeaderEntryAsync(observation.Token);

                        var invocationB = Task.Factory.StartNew(() => invokeStep("step-over-concurrent-B", cancellationB.Token),
                            CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
                        requestB = invocationB.Unwrap();
                        Assert.True(await bUsabilityObserved.Task.WaitAsync(observation.Token), "B must first observe the real session as usable.");
                        stateGateHolder = session.HoldStateGateAsync(stateGateEntered, releaseStateGate, observation.Token);
                        await stateGateEntered.Task.WaitAsync(observation.Token);
                        cancelA = Task.Factory.StartNew(cancellationA.Cancel,
                            CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
                        // The flag only places B before A can unwind; completion against held cleanup is the oracle.
                        await WaitUntilAsync(() => Task.FromResult(session.StepOverDrainRequired), TimeSpan.FromSeconds(2));
                        releaseBUsability.Set();
                        // Invoke has returned only after B's synchronous pre-admission cancellation catch ran.
                        _ = await invocationB.WaitAsync(observation.Token);
                        releaseStateGate.Set();
                        await stateGateHolder.WaitAsync(observation.Token);
                        await cancelA.WaitAsync(observation.Token);

                        await stopEntered.Task.WaitAsync(observation.Token);
                        await AssertRequestAWaitsForCleanupAsync("StopAsync");
                        releaseStop.TrySetResult(true);
                        await disposeEntered.Task.WaitAsync(observation.Token);
                        await AssertRequestAWaitsForCleanupAsync("DisposeAsync");
                        releaseDispose.TrySetResult(true);

                        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => requestA).WaitAsync(observation.Token);
                        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => requestB).WaitAsync(observation.Token);
                    }
                    catch (OperationCanceledException exception)
                    {
                        throw new InvalidOperationException("The regression interleaving timed out before both real cleanup barriers were exercised.", exception);
                    }
                    finally
                    {
                        ReleaseBarriers();
                        cancelA ??= Task.Factory.StartNew(cancellationA.Cancel,
                            CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
                        _ = await Record.ExceptionAsync(() => Task.WhenAll(new Task?[] { requestA, requestB, stateGateHolder, cancelA }.OfType<Task>()));
                    }
                    return await requestA!;
                },
                beforeStop: async () =>
                {
                    if (exerciseStarted)
                    {
                        stopEntered.TrySetResult(true);
                        await releaseStop.Task;
                    }
                },
                beforeDispose: async () =>
                {
                    if (exerciseStarted)
                    {
                        disposeEntered.TrySetResult(true);
                        await releaseDispose.Task;
                    }
                },
                afterUsabilityObservation: (requestId, usable) =>
                {
                    if (requestId == "step-over-concurrent-B")
                    {
                        bUsabilityObserved.TrySetResult(usable);
                        releaseBUsability.Wait(observation.Token);
                    }
                });

            Assert.True(probe.CancellationObserved);
            Assert.False(session.HasActiveStepOver);
            Assert.Equal(0, session.PendingRequestCount);
            Assert.Equal(0, probe.ActiveRequestCount);
            Assert.False(probe.TokenRetained);
            Assert.False(probe.SlotRetained);
            Assert.False(probe.AdapterAlive);
            Assert.False(session.IsUsable);
            Assert.Equal("complete", probe.Result.ResultType);
            Assert.True(probe.Result.IsError == true);
            var content = Assert.IsType<JsonElement>(probe.Result.StructuredContent);
            Assert.Equal("debug_session_not_found", content.GetProperty("kind").GetString());
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", content.GetProperty("error").GetString());
        }
        finally
        {
            ReleaseBarriers();
        }
    }

    [Fact]
    public async Task StepOver_NextCorrelationCollisionWithPendingStackTrace_FailsActiveBeforeUnrelatedRouting()
    {
        await using var session = await StartAsync(new FixtureConfiguration(
            SupportsDelayedStackTraceLoading: true,
            LifecycleMode: "all-stop",
            StackTraceResponseMode: "hold",
            NextResponseMode: "wrong-request-sequence-collides-with-stack-trace"));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        var stackTrace = session.GetCallStackIsRefusedAsync(1, 0, 20, observation.Token);
        await session.WaitForPendingStackTraceAsync(observation.Token);
        await session.Fixture.WaitForTranscriptKindAsync("stack-trace-response-gated", observation.Token);

        var stepOver = session.StepOverAsync(1, CancellationToken.None);
        try
        {
            await Assert.ThrowsAsync<InvalidDataException>(() => stepOver);
            Assert.False(stackTrace.IsCompleted, "A corrupt next response must fail the active step before it can settle the colliding stackTrace request.");
            session.Fixture.ReleaseStackTraceResponse();
            Assert.True(await stackTrace);
            Assert.True(session.IsUsable);
        }
        finally
        {
            session.Fixture.ReleaseStackTraceResponse();
        }
    }

    [Fact]
    public async Task StepOver_CancellationAfterHeaderBeforePayload_DrainsRegisteredSession()
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            RequestTimeout,
            StopTimeout,
            CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        using var cancellation = new CancellationTokenSource();
        session.ArmDapWriteAfterHeaderBarrier();
        var operation = session.StepOverThroughRegistryAsync(1, cancellation.Token, inspectCancellation: true);
        try
        {
            await session.WaitForDapHeaderWriteAsync(observation.Token);
            cancellation.Cancel();
            var probe = await operation.WaitAsync(observation.Token);

            Assert.True(probe.CancellationObserved);
            Assert.False(session.HasActiveStepOver);
            Assert.Equal(0, session.PendingRequestCount);
            Assert.False(probe.TokenRetained);
            Assert.False(probe.SlotRetained);
            Assert.False(probe.AdapterAlive);
            Assert.True(probe.LeaseReleasedBeforeRemoval == true);
            Assert.Equal("debug_session_not_found", Assert.IsType<JsonElement>(probe.Result.StructuredContent).GetProperty("kind").GetString());
            Assert.DoesNotContain(await session.Fixture.ReadTranscriptAsync(), static entry => entry.Command == "next");
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
        }
    }

    [Fact]
    public async Task StepOver_DeadlineStartsAfterCompleteWrite_NotAtPossibleWriteMarker()
    {
        var clock = new FakeTimeProvider();
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop", NextResponseMode: "hold-ack-no-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            TimeSpan.FromSeconds(30),
            StopTimeout,
            CancellationToken.None,
            stepOverTimeProvider: clock);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        session.ArmDapWriteAfterHeaderBarrier();
        var operation = session.StepOverThroughRegistryAsync(1, observation.Token);
        try
        {
            await session.WaitForDapHeaderWriteAsync(observation.Token);
            clock.Advance(TimeSpan.FromSeconds(31));
            Assert.False(operation.IsCompleted, "A possible or partial write must not start the post-commit observation deadline.");
            session.ReleaseDapWriteBarrier();
            await session.Fixture.WaitForNextRequestAsync(observation.Token);
            await session.WaitForReaderBarrierAsync(observation.Token);
            clock.Advance(TimeSpan.FromSeconds(29));
            Assert.False(operation.IsCompleted, "The complete write must receive its full 30-second post-commit budget.");
            clock.Advance(TimeSpan.FromSeconds(1));
            var probe = await operation.WaitAsync(observation.Token);

            var content = Assert.IsType<JsonElement>(probe.Result.StructuredContent);
            Assert.Equal("step_over_timed_out", content.GetProperty("kind").GetString());
            Assert.Equal(30, content.GetProperty("timeoutSeconds").GetInt32());
            Assert.False(probe.TokenRetained);
            Assert.False(probe.SlotRetained);
            Assert.False(probe.AdapterAlive);
            Assert.True(probe.LeaseReleasedBeforeRemoval == true);
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            session.Fixture.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task StepOver_WriterHeldPastOrdinaryRequestTimeout_CompletesOnlyAfterFullWriteAndStepStop()
    {
        var ordinaryRequestTimeout = TimeSpan.FromMilliseconds(500);
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            ordinaryRequestTimeout,
            StopTimeout,
            CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        using var cancellation = new CancellationTokenSource();
        using var releaseOnDeadline = observation.Token.Register(session.ReleaseDapWriteBarrier);
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        session.ArmDapWriteAfterHeaderBarrier();
        var operation = session.StepOverAsync(1, cancellation.Token);
        try
        {
            await session.WaitForDapHeaderWriteAsync(observation.Token);
            var completion = await Record.ExceptionAsync(() => operation.WaitAsync(ordinaryRequestTimeout * 3, observation.Token));
            Assert.True(completion is TimeoutException,
                $"A held, uncommitted next must outlive the real ordinary request timeout; expected observation TimeoutException, got {completion?.ToString() ?? "successful completion"}.");
            session.ReleaseDapWriteBarrier();
            await operation.WaitAsync(observation.Token);
            await session.WaitForReaderBarrierAsync(observation.Token);

            Assert.Equal(new DapSessionSnapshot("stopped", "step", null), session.State);
            Assert.True(session.IsUsable);
            Assert.False(session.HasActiveStepOver);
            Assert.Equal(0, session.PendingRequestCount);
            Assert.Single(await session.Fixture.ReadTranscriptAsync(), static entry => entry.Command == "next");
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Fact]
    public async Task GetThreads_PreHeaderWriterHeld_StillHonorsOrdinaryRequestTimeout()
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop"),
            "D:\\fixtures\\program.dll",
            InitializeTimeout,
            TimeSpan.FromMilliseconds(500),
            StopTimeout,
            CancellationToken.None);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        using var cancellation = new CancellationTokenSource();
        using var releaseOnDeadline = observation.Token.Register(session.ReleaseDapWriteBarrier);
        await session.Fixture.WaitForEventAsync("stopped", observation.Token);
        await session.WaitForReaderBarrierAsync(observation.Token);
        session.ArmDapWriteBeforeHeaderBarrier();
        var operation = session.WaitForReaderBarrierAsync(cancellation.Token);
        try
        {
            await session.WaitForDapPreHeaderWriteAsync(observation.Token);
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation).WaitAsync(observation.Token);
            Assert.False(cancellation.IsCancellationRequested);
            Assert.False(observation.IsCancellationRequested);
            Assert.Equal(0, session.PendingRequestCount);
            Assert.True(session.IsUsable);
            session.ReleaseDapWriteBarrier();
            await session.WaitForReaderBarrierAsync(observation.Token);
        }
        finally
        {
            session.ReleaseDapWriteBarrier();
            cancellation.Cancel();
            _ = await Record.ExceptionAsync(() => operation);
        }
    }

    [Fact]
    public async Task StopAsync_ReaderFailureSkipsGracefulRequestTimeouts()
    {
        await using var session = await StartAsync(new FixtureConfiguration(SendMalformedDapFrameAfterStartup: true));
        var adapterProcessId = await session.Fixture.GetProcessIdAsync(CancellationToken.None);

        await WaitUntilAsync(
            async () => (await session.Fixture.ReadTranscriptAsync()).Any(entry => entry.Kind == "malformed-dap-frame-sent"),
            TimeSpan.FromSeconds(2));
        Assert.False(HasExited(adapterProcessId), "The controlled adapter must remain alive after sending its malformed DAP frame.");

        using var readerCompletion = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await session.WaitForReaderCompletionAsync(readerCompletion.Token);

        Assert.False(session.IsUsable, "A session whose DAP reader ended after a malformed frame must not remain usable while its adapter process is alive.");
        await session.StopAsync(CancellationToken.None).WaitAsync(StopTimeout + TimeSpan.FromSeconds(1));
        Assert.True(HasExited(adapterProcessId), "Reader failure cleanup must kill the owned adapter without waiting for graceful request timeouts.");
    }

    [Fact]
    public async Task StopAsync_SendsTerminateBeforeDisconnectWhenSupported()
    {
        await using var session = await StartAsync(new FixtureConfiguration(SupportsTerminate: true, HoldExitAfterDisconnectResponse: true));
        var adapterProcessId = await session.Fixture.GetProcessIdAsync(CancellationToken.None);
        var stop = session.StopAsync(CancellationToken.None);
        try
        {
            await WaitUntilAsync(async () => (await session.Fixture.ReadTranscriptAsync()).Any(entry => entry.Kind == "disconnect-response"), TimeSpan.FromSeconds(2));
            Assert.False(stop.IsCompleted, "StopAsync must wait for the controlled adapter process to exit.");
            Assert.False(HasExited(adapterProcessId), "The controlled adapter process must remain alive until graceful release.");
        }
        finally
        {
            session.Fixture.ReleaseGracefulShutdown();
        }

        await stop;
        Assert.True(HasExited(adapterProcessId), "StopAsync must not complete before the graceful adapter process exits.");
        var commands = Requests(await session.Fixture.ReadTranscriptAsync());
        var terminate = Array.IndexOf(commands, "terminate");
        var disconnect = Array.IndexOf(commands, "disconnect");
        Assert.True(terminate >= 0, "supported adapters require terminate.");
        Assert.True(disconnect >= 0, "supported adapters require disconnect.");
        Assert.True(terminate < disconnect, "terminate must precede disconnect.");
    }

    [Fact]
    public async Task StopAsync_UsesTerminateEnabledByCapabilityDeltaBeforeDisconnect()
    {
        await using var session = await StartAsync(new FixtureConfiguration(
            SupportsConfigurationDone: true,
            SupportsTerminate: true,
            EnableTerminateAfterInitialization: true,
            HoldExitAfterDisconnectResponse: true));
        var transcript = await session.Fixture.ReadTranscriptAsync();
        var initializeResponse = Assert.Single(transcript, entry => entry.Kind == "initialize-response");
        Assert.False(initializeResponse.SupportsTerminateRequest ?? true, "The initial response must not advertise terminate before the capability delta.");
        Assert.Contains(transcript, entry => entry.Kind == "capabilities-delta-event");

        var stop = session.StopAsync(CancellationToken.None);
        try
        {
            await WaitUntilAsync(async () => (await session.Fixture.ReadTranscriptAsync()).Any(entry => entry.Kind == "disconnect-response"), TimeSpan.FromSeconds(2));
        }
        finally
        {
            session.Fixture.ReleaseGracefulShutdown();
        }

        await stop;
        var commands = Requests(await session.Fixture.ReadTranscriptAsync());
        var terminate = Array.IndexOf(commands, "terminate");
        var disconnect = Array.IndexOf(commands, "disconnect");
        Assert.Equal(1, commands.Count(command => command == "configurationDone"));
        Assert.Equal(1, commands.Count(command => command == "terminate"));
        Assert.Equal(1, commands.Count(command => command == "disconnect"));
        Assert.True(terminate >= 0 && terminate < disconnect, "terminate must precede disconnect after the capability delta enables it.");
    }

    [Fact]
    public async Task StopAsync_SkipsTerminateWhenUnsupported()
    {
        await using var session = await StartAsync(new FixtureConfiguration(SupportsTerminate: false, HoldExitAfterDisconnectResponse: true));
        var adapterProcessId = await session.Fixture.GetProcessIdAsync(CancellationToken.None);
        var stop = session.StopAsync(CancellationToken.None);
        try
        {
            await WaitUntilAsync(async () => (await session.Fixture.ReadTranscriptAsync()).Any(entry => entry.Kind == "disconnect-response"), TimeSpan.FromSeconds(2));
            Assert.False(stop.IsCompleted, "StopAsync must wait for the controlled adapter process to exit.");
            Assert.False(HasExited(adapterProcessId), "The controlled adapter process must remain alive until graceful release.");
        }
        finally
        {
            session.Fixture.ReleaseGracefulShutdown();
        }

        await stop;
        Assert.True(HasExited(adapterProcessId), "StopAsync must not complete before the graceful adapter process exits.");
        var transcript = await session.Fixture.ReadTranscriptAsync();
        var commands = Requests(transcript);
        var disconnect = Assert.Single(transcript, entry => entry.Command == "disconnect");
        using var disconnectArguments = JsonDocument.Parse(disconnect.Arguments ?? "{}");
        Assert.True(
            !disconnectArguments.RootElement.TryGetProperty("terminateDebuggee", out var terminateDebuggee) || terminateDebuggee.ValueKind == JsonValueKind.True,
            "launch-session disconnect must omit terminateDebuggee or set it true.");
        Assert.DoesNotContain("terminate", commands);
        Assert.Contains("disconnect", commands);
    }

    [Fact]
    public async Task StopAsync_AndDisposeAsync_ShareOneCleanupOperation()
    {
        await using var session = await StartAsync(new FixtureConfiguration(SupportsTerminate: true, BlockGracefulShutdown: true));
        var fixture = session.Fixture;
        var stop = session.StopAsync(CancellationToken.None);
        var dispose = session.DisposeSessionAsync();

        try
        {
            await WaitUntilAsync(async () => (await fixture.ReadTranscriptAsync()).Any(entry => entry.Command == "terminate"), TimeSpan.FromSeconds(2));
            Assert.False(stop.IsCompleted, "StopAsync must wait for the shared graceful cleanup operation.");
            Assert.False(dispose.IsCompleted, "DisposeAsync must wait for the shared graceful cleanup operation.");
        }
        finally
        {
            fixture.ReleaseGracefulShutdown();
        }

        await Task.WhenAll(stop, dispose);
        var commands = Requests(await fixture.ReadTranscriptAsync());
        Assert.Equal(1, commands.Count(command => command == "terminate"));
        Assert.Equal(1, commands.Count(command => command == "disconnect"));
    }

    [Fact]
    public async Task StopAsync_UsesBoundedTreeKillWhenGracefulShutdownIsIgnored()
    {
        await using var session = await StartAsync(new FixtureConfiguration(
            SupportsTerminate: true,
            IgnoreGracefulShutdown: true,
            SpawnDescendant: true));
        var fixture = session.Fixture;
        var descendant = Assert.Single(await fixture.ReadTranscriptAsync(), entry => entry.Kind == "descendant").ProcessId;
        Assert.NotNull(descendant);

        var maximumStopDuration = RequestTimeout + RequestTimeout + StopTimeout + StopTimeout + TimeSpan.FromSeconds(2);
        var stopwatch = Stopwatch.StartNew();
        await session.StopAsync(CancellationToken.None).WaitAsync(maximumStopDuration);
        stopwatch.Stop();
        Assert.True(stopwatch.Elapsed <= maximumStopDuration, $"StopAsync tree kill must finish within {maximumStopDuration}; bound = two request timeouts ({RequestTimeout}), two stop timeouts ({StopTimeout}), and a two-second process/scheduler margin. Actual: {stopwatch.Elapsed}.");
        await WaitUntilAsync(async () => HasExited(await fixture.GetProcessIdAsync(CancellationToken.None)), TimeSpan.FromSeconds(2));
        Assert.True(HasExited(descendant.Value), "The owned adapter descendant must be killed with the controlled adapter tree.");
    }

    [Fact]
    public async Task StopAsync_AndDisposeAsync_KeepUnixGuardianAliveUntilDescendantCleanup()
    {
        await using var session = await StartAsync(new FixtureConfiguration(
            SpawnDescendant: true,
            SuppressLifecycleEvents: true,
            ExitAfterLaunchResponse: true));
        var fixture = session.Fixture;
        var guardianProcessId = session.OwnedProcessId;
        int? descendant = null;

        try
        {
            descendant = Assert.Single(await fixture.ReadTranscriptAsync(), entry => entry.Kind == "descendant").ProcessId;
            Assert.NotNull(descendant);
            var adapterProcessId = await fixture.GetProcessIdAsync(CancellationToken.None);
            await WaitUntilAsync(() => Task.FromResult(HasExited(adapterProcessId)), TimeSpan.FromSeconds(2));
            Assert.Contains(await fixture.ReadTranscriptAsync(), entry => entry.Kind == "unexpected-root-exit");
            Assert.False(HasExited(descendant.Value), "The controlled descendant must still be alive after its adapter root exits unexpectedly.");
            if (!OperatingSystem.IsWindows())
            {
                Assert.False(HasExited(guardianProcessId), "The Unix guardian must remain alive after the debugger root exits and before cleanup is requested.");
            }

            await Task.WhenAll(session.StopAsync(CancellationToken.None), session.DisposeSessionAsync());
            Assert.True(HasExited(descendant.Value), "StopAsync and DisposeAsync must terminate the owned descendant after its adapter root has exited unexpectedly.");
            if (!OperatingSystem.IsWindows())
            {
                Assert.True(HasExited(guardianProcessId), "The Unix guardian must exit only after it terminates the owned process group during cleanup.");
            }
        }
        finally
        {
            if (descendant is { } processId)
            {
                await fixture.TerminateProcessTreeAsync(processId);
            }
        }
    }

    [Fact]
    public async Task HostedStop_CancellationForcesTreeCleanupBeforeShutdownReturns()
    {
        await using var session = await StartAsync(new FixtureConfiguration(
            SupportsTerminate: true,
            BlockGracefulShutdown: true,
            SpawnDescendant: true));
        var adapterProcessId = await session.Fixture.GetProcessIdAsync(CancellationToken.None);
        var descendantProcessId = Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry => entry.Kind == "descendant").ProcessId;
        Assert.NotNull(descendantProcessId);
        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => session.StopHostedRegistryAsync(cancellation.Token).WaitAsync(StopTimeout + TimeSpan.FromSeconds(2)));
        Assert.True(HasExited(adapterProcessId), "Host cancellation must not return before the adapter process is gone.");
        Assert.True(HasExited(descendantProcessId.Value), "Host cancellation must not return before the owned adapter descendant is gone.");
    }

    private static string?[] Requests(IReadOnlyList<FixtureTranscriptEntry> transcript) =>
        transcript.Where(entry => entry.Kind == "request").Select(entry => entry.Command).ToArray();

    private static Task<NetCoreDbgSessionContractDriver> StartAsync(
        FixtureConfiguration configuration,
        string program = "D:\\fixtures\\program.dll",
        ICollection<FixtureTranscriptEntry>? failedTranscript = null) =>
        NetCoreDbgSessionContractDriver.StartAsync(
            configuration,
            program,
            InitializeTimeout,
            RequestTimeout,
            StopTimeout,
            CancellationToken.None,
            failedTranscript);

    private static async Task WaitUntilAsync(Func<Task<bool>> condition, TimeSpan timeout)
    {
        using var cancellation = new CancellationTokenSource(timeout);
        while (!await condition())
        {
            await Task.Delay(TimeSpan.FromMilliseconds(25), cancellation.Token);
        }
    }

    private static bool HasExited(int processId)
    {
        try
        {
            using var process = Process.GetProcessById(processId);
            return process.HasExited;
        }
        catch (ArgumentException)
        {
            return true;
        }
    }
    private sealed class InheritablePipeProbe : IDisposable
    {
        private const uint HandleFlagInherit = 0x00000001;

        private readonly FileStream _reader;
        private SafeFileHandle? _writer;

        private InheritablePipeProbe(SafeFileHandle reader, SafeFileHandle writer)
        {
            _reader = new FileStream(reader, FileAccess.Read, bufferSize: 1, isAsync: false);
            _writer = writer;
        }

        public static InheritablePipeProbe Create()
        {
            var attributes = new SecurityAttributes
            {
                Length = Marshal.SizeOf<SecurityAttributes>(),
                InheritHandle = true,
            };
            if (!CreatePipe(out var readerHandle, out var writerHandle, ref attributes, 0))
            {
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not create the inheritable handle probe.");
            }

            var reader = new SafeFileHandle(readerHandle, ownsHandle: true);
            var writer = new SafeFileHandle(writerHandle, ownsHandle: true);
            try
            {
                if (!SetHandleInformation(reader.DangerousGetHandle(), HandleFlagInherit, 0))
                {
                    throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not configure the inheritable handle probe.");
                }

                return new InheritablePipeProbe(reader, writer);
            }
            catch
            {
                reader.Dispose();
                writer.Dispose();
                throw;
            }
        }

        public void CloseWriter()
        {
            _writer?.Dispose();
            _writer = null;
        }

        public Task<int> ReadEndAsync() => _reader.ReadAsync(new byte[1]).AsTask();

        public void Dispose()
        {
            CloseWriter();
            _reader.Dispose();
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreatePipe(
            out IntPtr readPipe,
            out IntPtr writePipe,
            ref SecurityAttributes pipeAttributes,
            uint size);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int Length;
            public IntPtr SecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)]
            public bool InheritHandle;
        }
    }

}
