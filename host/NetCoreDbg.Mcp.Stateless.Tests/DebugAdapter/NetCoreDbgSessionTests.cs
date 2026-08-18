using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;
using System.Text;
using System.Text.Json;
using System.Diagnostics;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

[CollectionDefinition(Name, DisableParallelization = true)]
public sealed class NetCoreDbgSessionProcessCollection
{
    public const string Name = "NetCoreDbgSessionProcess";
}

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class NetCoreDbgSessionTests
{
    private static readonly TimeSpan InitializeTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan StopTimeout = TimeSpan.FromMilliseconds(300);

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
        var failure = await Record.ExceptionAsync(async () => session = await NetCoreDbgSessionContractDriver.StartAsync(
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
            "The unmatched response and early initialized event must precede the correlated initialize response.");
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

        Assert.Equal(new string?[] { "stopped", "continued", "exited", "terminated" }, lifecycleEvents);
        Assert.Equal(new DapSessionSnapshot("terminated", stopReason, exitCode), emitting.State);
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
