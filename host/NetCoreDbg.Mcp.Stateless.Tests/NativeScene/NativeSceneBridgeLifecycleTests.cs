using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Runtime.InteropServices;
using Xunit.Abstractions;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class NativeSceneBridgeLifecycleTests
{
    private const string AuthorizationNonce = "native-scene-test-nonce";
    private const int MaximumRequestBytes = 1_024;
    private const int MaximumResponseBytes = 2_048;
    private const int FakeFrameLimit = 8_192;
    private static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan WriteTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan ReadTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan TestTimeout = TimeSpan.FromSeconds(5);
    private readonly ITestOutputHelper _output;

    public NativeSceneBridgeLifecycleTests(ITestOutputHelper output) => _output = output;

    [Fact]
    public async Task SendAsync_SerializesConcurrentNonceAuthorizedRequests_AndWritesNothingToStdout()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var originalStdout = Console.Out;
        using var stdout = new StringWriter();
        Console.SetOut(stdout);
        try
        {
            var first = client.SendAsync(AuthorizationNonce, Request("first"), deadline.Token);
            var firstRequest = await observer.ReadRequestAsync(deadline.Token);

            Assert.Equal(AuthorizationNonce, firstRequest.Nonce);
            Assert.False(string.IsNullOrWhiteSpace(firstRequest.CorrelationId));
            Assert.NotEqual(AuthorizationNonce, firstRequest.CorrelationId);
            Assert.Equal("first", Text(firstRequest.Request["operation"]));

            var secondRead = observer.ReadRequestAsync(deadline.Token);
            var second = client.SendAsync(AuthorizationNonce, Request("second"), deadline.Token);

            await Task.Yield();
            Assert.False(secondRead.IsCompleted, "The second request reached the synchronous bridge before the first response was written.");

            await observer.WriteResponseAsync(ResponseFor(firstRequest, Result("first")), deadline.Token);
            var secondRequest = await secondRead;
            await observer.WriteResponseAsync(ResponseFor(secondRequest, Result("second")), deadline.Token);

            Assert.Equal(1, observer.RequestsObservedBeforeFirstResponse);
            AssertAvailable(await first, "first");
            AssertAvailable(await second, "second");
        }
        finally
        {
            Console.SetOut(originalStdout);
        }

        Assert.Equal(string.Empty, stdout.ToString());
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenConnectCannotBeEstablishedWithinItsBound()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var client = NativeSceneBridgeClientDriver.Create($"native-scene-missing-{Guid.NewGuid():N}");

        AssertUnavailable(await client.SendAsync(AuthorizationNonce, Request("connect"), deadline.Token));
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheRequestExceedsTheWriteFrameLimit_WithoutWritingIt()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        using var observerStop = new CancellationTokenSource();
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var observedRequest = observer.TryReadRequestUntilStoppedAsync(observerStop.Token);
        var result = await client.SendAsync(
            AuthorizationNonce,
            new JsonObject { ["operation"] = new string('x', MaximumRequestBytes) },
            deadline.Token);

        observerStop.Cancel();
        AssertUnavailable(result);
        Assert.Null(await observedRequest);
    }

    [Theory]
    [InlineData("nonce")]
    [InlineData("correlationId")]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheResponseAuthorizationOrCorrelationDoesNotMatch(string mismatchedMember)
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request(mismatchedMember), deadline.Token);
        var request = await observer.ReadRequestAsync(deadline.Token);
        var response = mismatchedMember switch
        {
            "nonce" => ResponseFor(request, Result("ignored"), nonce: "wrong-nonce"),
            "correlationId" => ResponseFor(request, Result("ignored"), correlationId: "wrong-correlation"),
            _ => throw new InvalidOperationException($"Unexpected mismatch selector '{mismatchedMember}'."),
        };

        await observer.WriteResponseAsync(response, deadline.Token);

        AssertUnavailable(await pending);
        await observer.AssertClientDisconnectedAsync(deadline.Token);
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheObserverDisconnectsBeforeResponding()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("disconnect"), deadline.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        await observer.DisposeAsync();

        AssertUnavailable(await pending);
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheResponseFrameExceedsTheReadLimit_AndClosesThePipe()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("oversized-response"), deadline.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        await observer.WriteResponseLengthAsync(MaximumResponseBytes + 1, deadline.Token);

        AssertUnavailable(await pending);
        await observer.AssertClientDisconnectedAsync(deadline.Token);
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheObserverNeverCompletesTheBoundedRead_AndClosesThePipe()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("read-timeout"), deadline.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        var disconnected = observer.AssertClientDisconnectedAsync(deadline.Token);

        AssertUnavailable(await pending);
        await disconnected;
    }

    [Fact]
    public async Task SendAsync_CancellationReturnsObserverUnavailable_AndClosesThePipe()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(deadline.Token);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("cancel"), cancellation.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        var disconnected = observer.AssertClientDisconnectedAsync(deadline.Token);
        cancellation.Cancel();

        AssertUnavailable(await pending);
        await disconnected;
    }

    [Fact]
    public async Task DisposeAsync_ClosesAnInflightPipeRequest_AndReturnsObserverUnavailable()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("dispose"), deadline.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        var disconnected = observer.AssertClientDisconnectedAsync(deadline.Token);
        var disposal = client.DisposeAsync().AsTask();

        AssertUnavailable(await pending);
        await disposal;
        await disconnected;
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Binding_KillFailure_HandsLiveProcessToKernelBeforeReleasingOwner(bool assemblyLaunch)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var sentinel = new IOException("controlled pre-kill failure");
        await using var binding = new BindingDriver(stop: (_, _) => Task.FromException(sentinel), assemblyLaunch: assemblyLaunch);
        await binding.StartAsync();
        Assert.False(binding.Process!.HasExited);
        Assert.False(binding.Descendant!.HasExited);
        binding.AssertDedicatedJobContainsTree();

        var failure = await Record.ExceptionAsync(() => binding.DisposeBindingAsync());

        Assert.True(binding.Process.WaitForExit(3_000), "The live bridge survived cleanup after the injected pre-kill failure; its sole production Process owner was discarded.");
        Assert.True(binding.Descendant!.WaitForExit(3_000), "The dedicated bridge Job did not terminate its descendant.");
        _output.WriteLine($"kill-failure {(assemblyLaunch ? ".dll" : ".exe")}: bridge={binding.Process.Id}, descendant={binding.Descendant.Id}; both exited before fixture cleanup.");
        Assert.Same(sentinel, failure);
    }

    [Fact]
    public async Task Binding_WaitFailure_ReportsKernelHandoffWithoutClaimingObservedExit()
    {
        if (!OperatingSystem.IsWindows()) return;
        var sentinel = new IOException("controlled wait failure");
        await using var binding = new BindingDriver(stop: (process, _) =>
        {
            process.Kill();
            return Task.FromException(sentinel);
        });
        await binding.StartAsync();

        var outcome = await binding.CleanupBridgeAsync();

        Assert.False(binding.Outcome<bool>(outcome, "ExitObserved"));
        Assert.True(binding.Outcome<bool>(outcome, "TerminationHandedToKernel"));
        Assert.Same(sentinel, binding.Outcome<Exception>(outcome, "Failure"));
        binding.AssertTreeExited();
        _output.WriteLine("wait-failure: failed wait preserved; exitObserved=false, terminationHandedToKernel=true; independent handles signaled.");
    }

    [Fact]
    public async Task Binding_ExitedRootAndJobCloseFailure_BlocksAdmissionUntilRetainedOwnerIsReleased()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var binding = new BindingDriver(stop: StopRootOnlyAsync);
        await binding.StartAsync();
        binding.AssertDedicatedJobContainsTree();
        var owner = binding.Ownership!;
        var job = BindingDriver.Job(owner);
        BindingDriver.ProtectJob(job, protect: true);
        try
        {
            var outcome = await binding.CleanupBridgeAsync();

            Assert.True(binding.Outcome<bool>(outcome, "ExitObserved"));
            Assert.False(binding.Outcome<bool>(outcome, "TerminationHandedToKernel"));
            var failure = binding.Outcome<Exception?>(outcome, "Failure");
            Assert.NotNull(failure);
            _output.WriteLine($"protected Job cleanup failure: {failure}");
            Assert.Same(owner, binding.Ownership);
            BindingDriver.AssertJobProtected(job);
            Assert.False(binding.Descendant!.HasExited);

            var admissionFailure = await Record.ExceptionAsync(binding.StartAsync);
            Assert.IsType<InvalidOperationException>(admissionFailure);
            Assert.Equal(1, binding.LaunchCount);
            Assert.Same(owner, binding.Ownership);

            BindingDriver.ProtectJob(job, protect: false);
            var recovery = await binding.CleanupBridgeAsync();
            Assert.Null(binding.Outcome<Exception?>(recovery, "Failure"));
            Assert.True(binding.Outcome<bool>(recovery, "TerminationHandedToKernel"));
            Assert.Null(binding.Ownership);
            Assert.True(job.IsClosed);
            binding.AssertTreeExited();

            await binding.StartAsync();
            Assert.Equal(2, binding.LaunchCount);
            _ = await binding.CleanupBridgeAsync();
            binding.AssertTreeExited();
        }
        finally
        {
            BindingDriver.ProtectJob(job, protect: false);
            BindingDriver.CloseOwner(owner);
            _ = await binding.CleanupBridgeAsync();
        }
    }

    [Fact]
    public async Task Binding_DisposeJobCloseFailure_RetriesSameOwnerAndPreservesFirstFailure()
    {
        if (!OperatingSystem.IsWindows()) return;
        var primary = new IOException("controlled first disposal stop failure");
        var stopCalls = 0;
        await using var binding = new BindingDriver(stop: async (process, token) =>
        {
            await StopRootOnlyAsync(process, token);
            if (++stopCalls == 1) throw primary;
        });
        await binding.StartAsync();
        binding.AssertDedicatedJobContainsTree();
        var owner = binding.Ownership!;
        var job = BindingDriver.Job(owner);
        BindingDriver.ProtectJob(job, protect: true);
        try
        {
            var failure = await Record.ExceptionAsync(binding.DisposeBindingAsync);
            var failures = Assert.IsType<AggregateException>(failure).Flatten().InnerExceptions;
            Assert.Same(primary, failures[0]);
            Assert.Equal(2, failures.Count);
            Assert.NotSame(primary, failures[1]);
            _output.WriteLine($"secondary protected Job cleanup failure: {failures[1]}");
            Assert.Same(owner, binding.Ownership);
            BindingDriver.AssertJobProtected(job);
            Assert.False(binding.Descendant!.HasExited);
            Assert.Equal(1, binding.GateCount);

            BindingDriver.ProtectJob(job, protect: false);
            await binding.DisposeBindingAsync();

            Assert.Null(binding.Ownership);
            Assert.True(job.IsClosed);
            Assert.Equal(2, stopCalls);
            Assert.Equal(1, binding.LaunchCount);
            Assert.Equal(1, binding.GateCount);
            binding.AssertTreeExited();
            await binding.DisposeBindingAsync();
            Assert.Equal(2, stopCalls);
        }
        finally
        {
            BindingDriver.ProtectJob(job, protect: false);
            BindingDriver.CloseOwner(owner);
            _ = await binding.CleanupBridgeAsync();
        }
    }

    [Fact]
    public async Task Binding_LaunchRollbackJobCloseFailure_RetainsOwnerBeforeObserverUnavailable()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var session = await StartIndependentSessionAsync();
        var targetPid = Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry => entry.Kind == "descendant").ProcessId!.Value;
        var primary = new IOException("controlled owned-launch initialization failure");
        await using var binding = new BindingDriver(stop: StopRootOnlyAsync, retainedLaunchFailure: primary);
        binding.AttachSession(session);
        await binding.WaitForCandidateAsync(targetPid);
        try
        {
            Assert.Equal("OBSERVER_UNAVAILABLE", await binding.CaptureVisualAsync(CancellationToken.None));
            Assert.NotNull(binding.LaunchedOwnership);
            var launchedOwner = binding.LaunchedOwnership!;
            var job = BindingDriver.Job(launchedOwner);
            var cleanupFailure = Assert.IsAssignableFrom<Exception>(primary.Data["NativeSceneBridgeCleanupFailure"]);
            Assert.NotSame(primary, cleanupFailure);
            _output.WriteLine($"owned-launch protected Job cleanup failure: {cleanupFailure}");
            Assert.Contains(nameof(ThrowStartup), primary.StackTrace);
            BindingDriver.AssertJobProtected(job);
            Assert.False(binding.Descendant!.HasExited);

            Assert.NotNull(binding.Ownership);
            var retainedOwner = binding.Ownership!;
            Assert.Same(job, BindingDriver.Job(retainedOwner));
            binding.AssertDedicatedJobContainsTree();
            Assert.Equal("OBSERVER_UNAVAILABLE", await binding.CaptureVisualAsync(CancellationToken.None));
            Assert.Equal(1, binding.LaunchCount);
            Assert.Same(retainedOwner, binding.Ownership);

            BindingDriver.ProtectJob(job, protect: false);
            var recovery = await binding.CleanupBridgeAsync();
            Assert.Null(binding.Outcome<Exception?>(recovery, "Failure"));
            Assert.True(binding.Outcome<bool>(recovery, "TerminationHandedToKernel"));
            Assert.Null(binding.Ownership);
            Assert.True(job.IsClosed);
            binding.AssertTreeExited();
        }
        finally
        {
            if (binding.LaunchedOwnership is { } owner)
            {
                BindingDriver.ProtectJob(BindingDriver.Job(owner), protect: false);
                BindingDriver.CloseOwner(owner);
            }
            _ = await binding.CleanupBridgeAsync();
        }
    }

    [Fact]
    public async Task Binding_StartupFailure_PreservesPrimaryIdentityAndStackDespiteStopFailure()
    {
        if (!OperatingSystem.IsWindows()) return;
        var primary = new IOException("controlled client construction failure");
        var secondary = new IOException("controlled rollback stop failure");
        await using var binding = new BindingDriver(stop: (_, _) => Task.FromException(secondary), createClient: _ => ThrowStartup(primary));

        var failure = await Record.ExceptionAsync(binding.StartAsync);

        Assert.Same(primary, failure);
        Assert.Contains(nameof(ThrowStartup), failure!.StackTrace);
        Assert.Same(secondary, failure.Data["NativeSceneBridgeCleanupFailure"]);
        binding.AssertTreeExited();
        _output.WriteLine("startup rollback: sentinel A identity/throw-site retained; sentinel B secondary; real bridge tree exited.");
    }

    [Fact]
    public async Task Binding_ClientAndKillFailures_StillDisconnectProbeDeleteArtifactsAndReleaseGate()
    {
        if (!OperatingSystem.IsWindows()) return;
        var closeFailure = new IOException("controlled client close failure");
        var stopFailure = new IOException("controlled process stop failure");
        IAsyncDisposable? retainedClient = null;
        await using var artifacts = ArtifactStoreTestScope.Create();
        await using var binding = new BindingDriver(stop: (_, _) => Task.FromException(stopFailure), disposeClient: client =>
        {
            retainedClient = client;
            return ValueTask.FromException(closeFailure);
        });
        binding.AttachArtifacts(artifacts.Store);
        var staged = await artifacts.Store.StageAsync(BindingDriver.SessionId, "cleanup-capture", "image/png", "native-scene-artifact/1", Encoding.UTF8.GetBytes("{\"owned\":true}"));
        var descriptor = (await staged.CommitAsync()).Descriptor!;
        Assert.True(Directory.EnumerateFiles(artifacts.Root, "*", SearchOption.AllDirectories).Any());
        using var probePeer = await binding.ConnectProbeAsync();
        await binding.StartAsync();
        try
        {
            var failure = await Record.ExceptionAsync(binding.DisposeBindingAsync);

            var failures = Assert.IsType<AggregateException>(failure).Flatten().InnerExceptions;
            Assert.Contains(closeFailure, failures);
            Assert.Contains(stopFailure, failures);
            binding.AssertTreeExited();
            Assert.Equal(0, await probePeer.ReadAsync(new byte[1]).AsTask().WaitAsync(TestTimeout));
            Assert.Equal("ARTIFACT_NOT_FOUND", (await artifacts.Store.ReadAsync(BindingDriver.SessionId, descriptor.ArtifactId, 0, 32)).Code);
            Assert.Empty(Directory.EnumerateFiles(artifacts.Root, "*", SearchOption.AllDirectories));
            Assert.True(binding.ArtifactStoreDisposed(artifacts.Store));
            Assert.Equal(1, binding.GateCount);
            _output.WriteLine("client+kill failures: two failures reported after probe EOF, artifact unavailability/data deletion, store disposal and gate release.");
        }
        finally
        {
            if (retainedClient is not null) await retainedClient.DisposeAsync();
        }
    }

    [Fact]
    public async Task Binding_StopSessionFailure_StillDisposesArtifactStoreAndReleasesGate()
    {
        await using var artifacts = ArtifactStoreTestScope.Create();
        await using var binding = new BindingDriver();
        binding.AttachArtifacts(artifacts.Store);
        var staged = await artifacts.Store.StageAsync(BindingDriver.SessionId, "timer-capture", "image/png", "native-scene-artifact/1", Encoding.UTF8.GetBytes("{}"));
        _ = await staged.CommitAsync();
        var sentinel = new IOException("controlled stop-session timer failure");
        var timer = new FailingChangeTimer(sentinel);
        binding.ReplaceArtifactTimer(artifacts.Store, timer);

        var failure = await Record.ExceptionAsync(binding.DisposeBindingAsync);

        Assert.Same(sentinel, failure);
        Assert.True(timer.Disposed);
        Assert.True(binding.ArtifactStoreDisposed(artifacts.Store));
        Assert.Equal(1, binding.GateCount);
        Assert.Empty(Directory.EnumerateFiles(artifacts.Root, "*", SearchOption.AllDirectories));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Binding_CaptureStartupFailure_PreservesUnavailableOrCallerCancellationAndDebuggerTree(bool cancel)
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var session = await StartIndependentSessionAsync(windowed: true);
        using var debugger = Process.GetProcessById(session.OwnedProcessId);
        using var debuggee = Process.GetProcessById(Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry => entry.Kind == "descendant").ProcessId!.Value);
        _ = debugger.SafeHandle;
        _ = debuggee.SafeHandle;
        using var cancellation = new CancellationTokenSource();
        var cancellationFailure = new OperationCanceledException("controlled caller cancellation", null, cancellation.Token);
        var startupFailure = new IOException("controlled capture startup failure");
        var secondary = new IOException("controlled capture rollback failure");
        await using var binding = new BindingDriver(stop: (_, _) => Task.FromException(secondary), createClient: _ =>
        {
            if (cancel)
            {
                cancellation.Cancel();
                throw cancellationFailure;
            }

            throw startupFailure;
        });
        binding.AttachSession(session);
        await WaitForWindowAsync(debuggee);
        await binding.WaitForCandidateAsync(debuggee.Id);

        if (cancel)
        {
            var failure = await Record.ExceptionAsync(async () => await binding.CaptureGuardedAsync(cancellation.Token));
            Assert.Same(cancellationFailure, failure);
            Assert.Equal(cancellation.Token, Assert.IsType<OperationCanceledException>(failure).CancellationToken);
            Assert.Same(secondary, failure!.Data["NativeSceneBridgeCleanupFailure"]);
        }
        else
        {
            Assert.Null(await binding.CaptureGuardedAsync(CancellationToken.None));
        }

        binding.AssertTreeExited();
        Assert.False(debugger.HasExited);
        Assert.False(debuggee.HasExited);
        Assert.Equal("OBSERVER_UNAVAILABLE", await binding.CaptureVisualAsync(CancellationToken.None));
        binding.AssertTreeExited();
        Assert.False(debugger.HasExited);
        Assert.False(debuggee.HasExited);
        _output.WriteLine($"capture cancel={cancel}: bridge tree terminated; debugger={debugger.Id} and debuggee={debuggee.Id} survived each per-capture close.");
    }

    [Theory]
    [InlineData("slot")]
    [InlineData("host")]
    [InlineData("unregistered")]
    [InlineData("unusable")]
    [InlineData("stop")]
    public async Task Registry_BindingCleanupFailure_StillDisposesDebuggerTreeAndPreservesMapping(string cleanupPath)
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var session = await StartIndependentSessionAsync();
        using var debugger = Process.GetProcessById(session.OwnedProcessId);
        using var debuggee = Process.GetProcessById(Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry => entry.Kind == "descendant").ProcessId!.Value);
        _ = debugger.SafeHandle;
        _ = debuggee.SafeHandle;
        var sentinel = new IOException("controlled registry binding cleanup failure");
        await using var binding = new BindingDriver(stop: (_, _) => Task.FromException(sentinel));
        await binding.StartAsync();
        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
        var instance = typeof(NetCoreDbgSessionContractDriver).GetField("_session", flags)!.GetValue(session)!;
        var registryType = LoadHostAssembly().GetType("NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry", true)!;
        var evaluatorType = typeof(Func<,>).MakeGenericType(instance.GetType(), typeof(bool));
        var registry = registryType.GetConstructor(flags, null, [typeof(string), evaluatorType], null)!
            .Invoke([null, (Func<object, bool>)(_ => cleanupPath != "unusable")]);
        await using var registryCleanup = (IAsyncDisposable)registry;
        if (cleanupPath is "host" or "unusable" or "stop")
        {
            foreach (var (name, value) in new[] { ("_sessions", instance), ("_nativeSceneBindings", binding.Instance) })
            {
                var entries = registryType.GetField(name, flags)!.GetValue(registry)!;
                Assert.True((bool)entries.GetType().GetMethod("TryAdd", [typeof(string), value.GetType()])!
                    .Invoke(entries, [BindingDriver.SessionId, value])!);
            }
        }

        ModelContextProtocol.Protocol.CallToolResult? result = null;
        var failure = await Record.ExceptionAsync(async () =>
        {
            if (cleanupPath == "host")
            {
                await registryCleanup.DisposeAsync();
                return;
            }

            if (cleanupPath is "slot" or "unregistered")
            {
                var method = cleanupPath == "slot" ? "DisposeSlotResourcesAsync" : "DisposeUnregisteredResourcesAsync";
                var arguments = cleanupPath == "slot" ? new[] { BindingDriver.SessionId, instance, binding.Instance } : new[] { binding.Instance, instance };
                await (ValueTask)registryType.GetMethod(method, flags)!.Invoke(registry, arguments)!;
                return;
            }

            var request = new ModelContextProtocol.Protocol.CallToolRequestParams
            {
                Name = cleanupPath == "stop" ? "stop_debug" : "get_debug_state",
                Arguments = new Dictionary<string, JsonElement> { ["debugSessionId"] = JsonSerializer.SerializeToElement(BindingDriver.SessionId) },
            };
            var methodName = cleanupPath == "stop" ? "StopAsync" : "GetStateAsync";
            result = await (ValueTask<ModelContextProtocol.Protocol.CallToolResult>)registryType.GetMethod(methodName, flags)!
                .Invoke(registry, [request, CancellationToken.None])!;
        });

        if (cleanupPath is "slot" or "host") Assert.Same(sentinel, failure);
        else Assert.Null(failure);
        if (result is not null)
        {
            var content = Assert.IsType<JsonElement>(result.StructuredContent);
            Assert.Equal(cleanupPath == "stop" ? "stop_debug_success" : "debug_session_not_found", content.GetProperty("kind").GetString());
        }

        binding.AssertTreeExited();
        Assert.True(debugger.WaitForExit(3_000), "Registry skipped independent debugger cleanup after binding disposal failed.");
        Assert.True(debuggee.WaitForExit(3_000), "Registry skipped independent debuggee cleanup after binding disposal failed.");
        _output.WriteLine($"registry {cleanupPath}: binding failure consumed after bridge tree termination; debugger={debugger.Id}, debuggee={debuggee.Id} exited; established result preserved.");
    }

    [Theory]
    [InlineData("stop")]
    [InlineData("host")]
    public async Task Registry_ProtectedJobCleanupFailure_RetriesReachableOwnerWithoutReopeningSession(string cleanupPath)
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var session = await StartIndependentSessionAsync();
        using var debugger = Process.GetProcessById(session.OwnedProcessId);
        using var debuggee = Process.GetProcessById(Assert.Single(await session.Fixture.ReadTranscriptAsync(), entry => entry.Kind == "descendant").ProcessId!.Value);
        _ = debugger.SafeHandle;
        _ = debuggee.SafeHandle;
        var primary = new IOException("controlled registry pre-kill failure while the Job is protected");
        var failStop = true;
        var stopCalls = 0;
        await using var binding = new BindingDriver(stop: (process, token) =>
        {
            stopCalls++;
            return failStop ? Task.FromException(primary) : StopRootOnlyAsync(process, token);
        });
        binding.AttachSession(session);
        await binding.StartAsync();
        binding.AssertDedicatedJobContainsTree();

        const BindingFlags flags = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
        var assembly = LoadHostAssembly();
        var instance = typeof(NetCoreDbgSessionContractDriver).GetField("_session", flags)!.GetValue(session)!;
        var registryType = assembly.GetType("NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry", true)!;
        var registry = registryType.GetConstructor(flags, null, [typeof(string)], null)!.Invoke([null]);
        await using var registryCleanup = (IAsyncDisposable)registry;
        var slotType = registryType.GetNestedType("SessionSlot", BindingFlags.NonPublic)!;
        object? slot = null;
        Func<CancellationToken, Task> stopSession = session.StopAsync;
        Func<ValueTask> dispose = () => (ValueTask)registryType.GetMethod("DisposeSlotResourcesAsync", flags)!
            .Invoke(registry, [BindingDriver.SessionId, instance, binding.Instance])!;
        Action remove = () => registryType.GetMethod("RemoveSlot", flags)!
            .Invoke(registry, [BindingDriver.SessionId, instance, slot!]);
        slot = slotType.GetConstructor(flags, null,
            [typeof(TimeSpan), typeof(Func<CancellationToken, Task>), typeof(Func<ValueTask>), typeof(Action)], null)!
            .Invoke([TestTimeout, stopSession, dispose, remove]);
        foreach (var (name, value) in new[] { ("_sessions", instance), ("_slots", slot), ("_nativeSceneBindings", binding.Instance) })
        {
            var entries = (System.Collections.IDictionary)registryType.GetField(name, flags)!.GetValue(registry)!;
            entries.Add(BindingDriver.SessionId, value);
        }

        var bindings = (System.Collections.IDictionary)registryType.GetField("_nativeSceneBindings", flags)!.GetValue(registry)!;
        var resolver = registryType.GetMethod("ResolveNativeSceneBindingAsync", flags)!;
        var resolveBinding = resolver.CreateDelegate(typeof(Func<,>).MakeGenericType(typeof(string), resolver.ReturnType), registry);
        var dispatcher = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneToolDispatcher", true)!
            .GetMethod("DispatchAsync", BindingFlags.Static | BindingFlags.NonPublic)!;
        var owner = binding.Ownership!;
        var job = BindingDriver.Job(owner);
        var processHandle = (SafeHandle)owner.GetType().GetField("_processHandle", flags)!.GetValue(owner)!;
        var originalJobHandle = job.DangerousGetHandle();
        var originalProcessHandle = processHandle.DangerousGetHandle();
        try
        {
            BindingDriver.ProtectJob(job, protect: true);
            ModelContextProtocol.Protocol.CallToolResult? firstResult = null;
            var firstFailure = await Record.ExceptionAsync(async () =>
            {
                if (cleanupPath == "host") await registryCleanup.DisposeAsync();
                else firstResult = await CallRegistryAsync("StopAsync", "stop_debug");
            });
            var firstStopCalls = stopCalls;
            if (cleanupPath == "host")
            {
                var failures = Assert.IsType<AggregateException>(firstFailure).Flatten().InnerExceptions;
                Assert.Same(primary, failures[0]);
                Assert.Contains(failures, failure => !ReferenceEquals(primary, failure));
                _output.WriteLine($"registry {cleanupPath} protected Job cleanup failure: {firstFailure}");
            }
            else
            {
                Assert.Null(firstFailure);
                AssertSessionNotFound(firstResult!);
                Assert.True(firstStopCalls >= 2, "Explicit stop must exercise both binding removal and registered slot disposal while protection persists.");
            }

            Assert.Same(owner, binding.Ownership);
            BindingDriver.AssertJobProtected(job);
            Assert.False(processHandle.IsClosed);
            Assert.Equal(originalJobHandle, job.DangerousGetHandle());
            Assert.Equal(originalProcessHandle, processHandle.DangerousGetHandle());
            Assert.False(binding.Process!.HasExited);
            Assert.False(binding.Descendant!.HasExited);
            Assert.True(debugger.WaitForExit(3_000), "Failed native ownership cleanup must not skip debugger disposal.");
            Assert.True(debuggee.WaitForExit(3_000), "Failed native ownership cleanup must not skip debuggee disposal.");
            var retainedBinding = bindings[BindingDriver.SessionId];

            AssertSessionNotFound(await CallRegistryAsync("GetStateAsync", "get_debug_state"));
            AssertSessionNotFound(await CallRegistryAsync("GetThreadsAsync", "get_threads"));
            var nativeArguments = new Dictionary<string, JsonElement>
            {
                ["debugSessionId"] = JsonSerializer.SerializeToElement(BindingDriver.SessionId),
                ["protocolVersion"] = JsonSerializer.SerializeToElement("native-scene-probe/1"),
                ["schemaVersion"] = JsonSerializer.SerializeToElement("native-scene-probe.schema/1"),
            };
            var nativeResult = await (ValueTask<ModelContextProtocol.Protocol.CallToolResult>)dispatcher.Invoke(null,
                ["get_ui_probe_capabilities", nativeArguments, resolveBinding, CancellationToken.None])!;
            var nativeContent = ErrorContent(nativeResult);
            Assert.Equal("tool_error", nativeContent.GetProperty("kind").GetString());
            Assert.Equal("get_ui_probe_capabilities", nativeContent.GetProperty("tool").GetString());
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", nativeContent.GetProperty("code").GetString());
            var retainedAfterLookup = bindings[BindingDriver.SessionId];
            Assert.Same(owner, binding.Ownership);
            BindingDriver.AssertJobProtected(job);
            Assert.False(processHandle.IsClosed);
            _output.WriteLine($"registry {cleanupPath}: first stop attempts={firstStopCalls}; cleanup reachable={ReferenceEquals(binding.Instance, retainedBinding)}; closed session/native lookups refused while original Job/process handles and descendant remain live.");

            BindingDriver.ProtectJob(job, protect: false);
            failStop = false;
            var beforeRetry = stopCalls;
            if (cleanupPath == "host") await registryCleanup.DisposeAsync();
            else AssertSessionNotFound(await CallRegistryAsync("StopAsync", "stop_debug"));

            Assert.True(binding.Descendant!.WaitForExit(3_000), $"The same registry {cleanupPath} retry could not reach the retained protected Job owner; its descendant survived before fixture safety cleanup.");
            binding.AssertTreeExited();
            Assert.True(job.IsClosed, "Registry retry must close the original Job handle.");
            Assert.True(processHandle.IsClosed, "Registry retry must close the original process handle.");
            Assert.Null(binding.Ownership);
            Assert.True(stopCalls > beforeRetry, "Registry retry must invoke retained binding cleanup, not only replay a cached failed slot-close task.");
            Assert.Same(binding.Instance, retainedBinding);
            Assert.Same(binding.Instance, retainedAfterLookup);
            Assert.False(bindings.Contains(BindingDriver.SessionId));
            Assert.Equal(1, binding.LaunchCount);
            Assert.Equal(1, binding.GateCount);
            _output.WriteLine($"registry {cleanupPath}: existing cleanup entry retried the same owner; bridge={binding.Process!.Id}, descendant={binding.Descendant!.Id} exited and both original handles closed before safety cleanup.");
        }
        finally
        {
            failStop = false;
            BindingDriver.ProtectJob(job, protect: false);
            BindingDriver.CloseOwner(owner);
            _ = await binding.CleanupBridgeAsync();
        }

        async ValueTask<ModelContextProtocol.Protocol.CallToolResult> CallRegistryAsync(string method, string tool) =>
            await (ValueTask<ModelContextProtocol.Protocol.CallToolResult>)registryType.GetMethod(method, flags)!.Invoke(registry,
                [new ModelContextProtocol.Protocol.CallToolRequestParams
                {
                    Name = tool,
                    Arguments = new Dictionary<string, JsonElement> { ["debugSessionId"] = JsonSerializer.SerializeToElement(BindingDriver.SessionId) },
                }, CancellationToken.None])!;

        static JsonElement ErrorContent(ModelContextProtocol.Protocol.CallToolResult result)
        {
            Assert.Equal("complete", result.ResultType);
            Assert.True(result.IsError);
            var content = Assert.IsType<JsonElement>(result.StructuredContent);
            Assert.Equal(content.GetRawText(), Assert.IsType<ModelContextProtocol.Protocol.TextContentBlock>(Assert.Single(result.Content)).Text);
            return content;
        }

        static void AssertSessionNotFound(ModelContextProtocol.Protocol.CallToolResult result)
        {
            var content = ErrorContent(result);
            Assert.Equal("debug_session_not_found", content.GetProperty("kind").GetString());
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", content.GetProperty("error").GetString());
        }
    }

    [Fact]
    public void Launcher_JobCreationFailure_LeavesNoChildAndPreservesTheUnrelatedNamedEvent()
    {
        if (!OperatingSystem.IsWindows()) return;
        var name = $"Local\\controlled-bridge-job-collision-{Guid.NewGuid():N}";
        using var collision = new EventWaitHandle(false, EventResetMode.ManualReset, name, out var created);
        Assert.True(created);
        var before = ControlledProcessIdentities();
        var ownership = LoadHostAssembly().GetType("NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession+WindowsProcessTreeOwnership", true)!;
        var failure = Assert.Throws<TargetInvocationException>(() => ownership.GetMethod("CreateKillOnCloseJob", BindingFlags.Static | BindingFlags.NonPublic)!
            .Invoke(null, [name]));

        Assert.Equal(6, Assert.IsType<System.ComponentModel.Win32Exception>(failure.InnerException).NativeErrorCode);
        Assert.Empty(ControlledProcessIdentities().Except(before));
        Assert.True(collision.Set());
        Assert.True(collision.WaitOne(0));
        _output.WriteLine("job creation: real named-event collision returned ERROR_INVALID_HANDLE before child launch; unrelated event still valid; no newly running controlled child.");
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void Launcher_FailedCreationTimeContainmentOrProcessCreation_StartsNoUnownedChild(bool invalidJob)
    {
        if (!OperatingSystem.IsWindows()) return;
        var ownership = LoadHostAssembly().GetType("NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession+WindowsProcessTreeOwnership", true)!;
        var bridge = ownership.GetNestedType("WindowsBridgeProcess", BindingFlags.NonPublic)!;
        var info = new ProcessStartInfo { FileName = invalidJob ? Path.ChangeExtension(FixtureAssemblyPath(), ".exe") : "controlled-missing-bridge.exe", UseShellExecute = false };
        info.ArgumentList.Add("--controlled-dap-descendant");
        var before = ControlledProcessIdentities();
        TargetInvocationException failure;
        if (invalidJob)
        {
            var handleType = ownership.GetNestedType("SafeKernelHandle", BindingFlags.NonPublic)!;
            using var invalid = (SafeHandle)Activator.CreateInstance(handleType, [IntPtr.Zero])!;
            failure = Assert.Throws<TargetInvocationException>(() => bridge.GetMethod("CreateBridgeProcess", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, [info, invalid]));
        }
        else
        {
            failure = Assert.Throws<TargetInvocationException>(() => bridge.GetMethod("Start", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, [info]));
        }

        Assert.IsType<System.ComponentModel.Win32Exception>(failure.InnerException);
        Assert.Empty(ControlledProcessIdentities().Except(before));
        _output.WriteLine($"native creation failure invalidJob={invalidJob}: Win32 rejection; no newly running controlled child.");
    }

    private static async Task StopRootOnlyAsync(Process process, CancellationToken cancellationToken)
    {
        if (!process.HasExited)
        {
            process.Kill();
            await process.WaitForExitAsync(cancellationToken);
        }
    }

    private static IAsyncDisposable ThrowStartup(Exception exception) => throw exception;

    private static Task<NetCoreDbgSessionContractDriver> StartIndependentSessionAsync(bool windowed = false) =>
        NetCoreDbgSessionContractDriver.StartAsync(new FixtureConfiguration(SpawnDescendant: true, SpawnWindowedDescendant: windowed, LifecycleMode: "all-stop"),
            "D:\\fixtures\\program.dll", TimeSpan.FromSeconds(5), TimeSpan.FromSeconds(2), TimeSpan.FromMilliseconds(300), CancellationToken.None);

    private static async Task WaitForWindowAsync(Process process)
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        while (process.MainWindowHandle == IntPtr.Zero)
        {
            await Task.Delay(20, deadline.Token);
            process.Refresh();
        }
    }

    private static Assembly LoadHostAssembly() => AssemblyLoadContext.Default.LoadFromAssemblyPath(TestOutputPathResolver.ResolveManagedAssembly(
        RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));

    private static string FixtureAssemblyPath() => TestOutputPathResolver.ResolveManagedAssembly(RepositoryLayout.Root,
        Path.Combine("host", "NetCoreDbg.Mcp.Stateless.Tests", "Fixtures", "ControlledDapAdapter"), "ControlledDapAdapter");

    private static HashSet<(int Pid, long Start)> ControlledProcessIdentities()
    {
        var identities = new HashSet<(int, long)>();
        foreach (var process in Process.GetProcessesByName("ControlledDapAdapter"))
        {
            using (process)
            {
                if (!process.HasExited) identities.Add((process.Id, process.StartTime.ToUniversalTime().Ticks));
            }
        }

        return identities;
    }

    private sealed class FailingChangeTimer(Exception failure) : ITimer
    {
        public bool Disposed { get; private set; }
        public bool Change(TimeSpan dueTime, TimeSpan period) => throw failure;
        public void Dispose() => Disposed = true;
        public ValueTask DisposeAsync() { Dispose(); return ValueTask.CompletedTask; }
    }

    private sealed class BindingDriver : IAsyncDisposable
    {
        private const BindingFlags InstanceFlags = BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.Public;
        public const string SessionId = "controlled-bridge-session";
        private readonly object _binding;
        private readonly Type _type;
        private NamedPipeClientStream? _ready;
        public Process? Process { get; private set; }
        private readonly List<Process> _observedProcesses = [];
        public Process? Descendant { get; private set; }
        public object Instance => _binding;
        public object? Ownership => _type.GetField("_bridgeOwnership", InstanceFlags)!.GetValue(_binding);
        public int LaunchCount { get; private set; }
        public object? LaunchedOwnership { get; private set; }
        public int GateCount => ((SemaphoreSlim)_type.GetField("_gate", InstanceFlags)!.GetValue(_binding)!).CurrentCount;

        public BindingDriver(Func<Process, CancellationToken, Task>? stop = null, Func<string, IAsyncDisposable>? createClient = null,
            Func<IAsyncDisposable, ValueTask>? disposeClient = null, bool assemblyLaunch = false, Exception? retainedLaunchFailure = null)
        {
            _type = LoadHostAssembly().GetType("NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneSessionBinding", throwOnError: true)!;
            var fixtureAssembly = FixtureAssemblyPath();
            var fixturePath = assemblyLaunch ? fixtureAssembly : Path.ChangeExtension(fixtureAssembly, ".exe");
            object? instance = null;
            Func<ProcessStartInfo, Process> launch = info =>
            {
                LaunchCount++;
                var readyPipe = $"controlled-bridge-ready-{Guid.NewGuid():N}";
                info.ArgumentList.Clear();
                if (assemblyLaunch) info.ArgumentList.Add(fixtureAssembly);
                info.ArgumentList.Add("--controlled-bridge-tree");
                info.ArgumentList.Add(readyPipe);
                Process process;
                if (retainedLaunchFailure is null)
                {
                    process = (Process)_type.GetMethod("LaunchBridge", InstanceFlags)!.Invoke(instance, [info])!;
                    LaunchedOwnership = _type.GetField("_bridgeOwnership", InstanceFlags)!.GetValue(instance);
                }
                else
                {
                    var ownerType = LoadHostAssembly().GetType("NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession+WindowsProcessTreeOwnership+WindowsBridgeProcess", true)!;
                    LaunchedOwnership = ownerType.GetMethod("Start", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, [info]);
                    process = (Process)ownerType.GetProperty("Process", InstanceFlags)!.GetValue(LaunchedOwnership)!;
                }
                Process = System.Diagnostics.Process.GetProcessById(process.Id);
                _ = Process.SafeHandle;
                _observedProcesses.Add(Process);
                _ = Process.StartTime;
                _ready?.Dispose();
                _ready = new NamedPipeClientStream(".", readyPipe, PipeDirection.In, PipeOptions.Asynchronous);
                _ready.Connect(5_000);
                using var deadline = new CancellationTokenSource(TestTimeout);
                var pid = new byte[sizeof(int)];
                _ready.ReadExactlyAsync(pid, deadline.Token).AsTask().GetAwaiter().GetResult();
                Descendant = System.Diagnostics.Process.GetProcessById(BinaryPrimitives.ReadInt32LittleEndian(pid));
                _ = Descendant.SafeHandle;
                _observedProcesses.Add(Descendant);
                _ = Descendant.StartTime;
                if (retainedLaunchFailure is not null)
                {
                    var owner = LaunchedOwnership!;
                    var job = Job(owner);
                    ProtectJob(job, protect: true);
                    try
                    {
                        _ = ThrowStartup(retainedLaunchFailure);
                    }
                    catch (Exception primary)
                    {
                        try
                        {
                            owner.GetType().GetMethod("CloseJob", InstanceFlags)!.Invoke(owner, []);
                        }
                        catch (TargetInvocationException rollback)
                        {
                            primary.Data["NativeSceneBridgeTerminationOwner"] = job;
                            primary.Data["NativeSceneBridgeProcessHandle"] = owner.GetType().GetField("_processHandle", InstanceFlags)!.GetValue(owner);
                            primary.Data["NativeSceneBridgeCleanupFailure"] = rollback.InnerException!;
                        }
                        throw;
                    }
                }
                return process;
            };
            var constructor = _type.GetConstructors(InstanceFlags).Single(candidate => candidate.GetParameters().Length == 8);
            _binding = instance = constructor.Invoke([SessionId, fixturePath, null, true, launch, createClient, disposeClient, stop]);
        }

        public async Task StartAsync() => await (Task)_type.GetMethod("StartBridgeAsync", InstanceFlags)!.Invoke(_binding, [Environment.ProcessId])!;

        public void AttachSession(NetCoreDbgSessionContractDriver driver) => _type.GetMethod("AttachSession", InstanceFlags)!.Invoke(_binding,
            [typeof(NetCoreDbgSessionContractDriver).GetField("_session", InstanceFlags)!.GetValue(driver)]);

        public void AttachArtifacts(NativeSceneArtifactStoreDriver driver) => _type.GetField("_artifactStore", InstanceFlags)!.SetValue(_binding, Store(driver));
        private static object Store(NativeSceneArtifactStoreDriver driver) => typeof(NativeSceneArtifactStoreDriver).GetField("_store", InstanceFlags)!.GetValue(driver)!;
        public bool ArtifactStoreDisposed(NativeSceneArtifactStoreDriver driver) => (bool)Store(driver).GetType().GetField("_disposed", InstanceFlags)!.GetValue(Store(driver))!;
        public void ReplaceArtifactTimer(NativeSceneArtifactStoreDriver driver, ITimer timer)
        {
            var store = Store(driver);
            var field = store.GetType().GetField("_expiryTimer", InstanceFlags)!;
            ((ITimer?)field.GetValue(store))?.Dispose();
            field.SetValue(store, timer);
        }

        public async Task<NamedPipeClientStream> ConnectProbeAsync()
        {
            var probe = _type.GetField("_probeChannel", InstanceFlags)!.GetValue(_binding)!;
            var pipe = new NamedPipeClientStream(".", (string)probe.GetType().GetProperty("PipeName", InstanceFlags)!.GetValue(probe)!, PipeDirection.InOut, PipeOptions.Asynchronous);
            using var deadline = new CancellationTokenSource(TestTimeout);
            await pipe.ConnectAsync(deadline.Token);
            return pipe;
        }

        public async Task WaitForCandidateAsync(int processId)
        {
            using var deadline = new CancellationTokenSource(TestTimeout);
            var candidate = _type.GetMethod("TryGetCandidate", InstanceFlags)!;
            while (true)
            {
                object?[] arguments = [null];
                if ((bool)candidate.Invoke(_binding, arguments)!)
                {
                    Assert.Equal(processId, ((JsonElement)arguments[0]!).GetProperty("processId").GetInt32());
                    return;
                }

                await Task.Delay(20, deadline.Token);
            }
        }

        public async Task<JsonObject?> CaptureGuardedAsync(CancellationToken token) => (JsonObject?)await AwaitResultAsync(
            _type.GetMethod("CaptureGuardedAsync", InstanceFlags)!.Invoke(_binding, ["capture_native_scene", new JsonObject(), token])!);
        public async Task<string?> CaptureVisualAsync(CancellationToken token)
        {
            var request = JsonSerializer.SerializeToElement(new { });
            var result = (await AwaitResultAsync(_type.GetMethod("CaptureVisualEvidenceAsync", InstanceFlags)!.Invoke(_binding, [request, request, request, token])!))!;
            return (string?)result.GetType().GetProperty("Code", InstanceFlags)!.GetValue(result);
        }

        public async Task<object> CleanupBridgeAsync() => (await AwaitResultAsync(_type.GetMethod("DisposeBridgeAsync", InstanceFlags)!.Invoke(_binding, [])!))!;
        public T Outcome<T>(object outcome, string property) => (T)outcome.GetType().GetProperty(property, InstanceFlags)!.GetValue(outcome)!;
        public Task DisposeBindingAsync() => ((IAsyncDisposable)_binding).DisposeAsync().AsTask();

        public static SafeHandle Job(object owner) => (SafeHandle)owner.GetType().GetField("_job", InstanceFlags)!.GetValue(owner)!;

        public static void ProtectJob(SafeHandle job, bool protect)
        {
            if (job.IsClosed) return;
            const uint protectFromClose = 2;
            Assert.True(SetHandleInformation(job.DangerousGetHandle(), protectFromClose, protect ? protectFromClose : 0),
                $"Could not {(protect ? "protect" : "unprotect")} the controlled Job handle: {Marshal.GetLastWin32Error()}.");
        }

        public static void AssertJobProtected(SafeHandle job)
        {
            Assert.False(job.IsClosed);
            Assert.True(GetHandleInformation(job.DangerousGetHandle(), out var flags),
                $"Could not inspect the retained Job handle: {Marshal.GetLastWin32Error()}.");
            Assert.Equal(2u, flags & 2u);
        }

        public static void CloseOwner(object owner)
        {
            owner.GetType().GetMethod("CloseJob", InstanceFlags)!.Invoke(owner, []);
            owner.GetType().GetMethod("CloseProcessHandle", InstanceFlags)!.Invoke(owner, []);
        }

        public void AssertDedicatedJobContainsTree()
        {
            var ownership = _type.GetField("_bridgeOwnership", InstanceFlags)!.GetValue(_binding)!;
            var job = (SafeHandle)ownership.GetType().GetField("_job", InstanceFlags)!.GetValue(ownership)!;
            Assert.True(IsProcessInJob(Process!.SafeHandle, job.DangerousGetHandle(), out var rootContained));
            Assert.True(rootContained);
            Assert.True(IsProcessInJob(Descendant!.SafeHandle, job.DangerousGetHandle(), out var childContained));
            Assert.True(childContained);
        }

        public void AssertTreeExited()
        {
            Assert.NotNull(Process);
            Assert.NotNull(Descendant);
            Assert.True(Process!.WaitForExit(3_000), "Bridge remained alive before test safety cleanup.");
            Assert.True(Descendant!.WaitForExit(3_000), "Bridge descendant remained alive before test safety cleanup.");
        }

        private static async Task<object?> AwaitResultAsync(object pending)
        {
            var task = pending as Task ?? (Task)pending.GetType().GetMethod("AsTask")!.Invoke(pending, [])!;
            await task;
            return task.GetType().GetProperty("Result")?.GetValue(task);
        }

        public async ValueTask DisposeAsync()
        {
            try
            {
                await DisposeBindingAsync();
            }
            finally
            {
                _ready?.Dispose();
                foreach (var process in _observedProcesses)
                {
                    try
                    {
                        if (!process.HasExited)
                        {
                            process.Kill(entireProcessTree: true);
                            await process.WaitForExitAsync().WaitAsync(TestTimeout);
                        }
                    }
                    finally
                    {
                        process.Dispose();
                    }
                }
            }
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool IsProcessInJob(Microsoft.Win32.SafeHandles.SafeProcessHandle process, IntPtr job, [MarshalAs(UnmanagedType.Bool)] out bool contained);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetHandleInformation(IntPtr handle, out uint flags);
    }

    private static JsonObject Request(string operation) => new()
    {
        ["operation"] = operation,
        ["candidate"] = new JsonObject
        {
            ["processId"] = 4242,
            ["windowHandle"] = "0x0000000000001234",
        },
    };

    private static JsonObject Result(string operation) => new()
    {
        ["operation"] = operation,
        ["state"] = "observed",
    };

    private static JsonObject ResponseFor(BridgeRequest request, JsonObject payload, string? nonce = null, string? correlationId = null) => new()
    {
        ["nonce"] = nonce ?? request.Nonce,
        ["correlationId"] = correlationId ?? request.CorrelationId,
        ["response"] = payload,
    };

    private static void AssertAvailable(NativeSceneBridgeCallResult result, string expectedOperation)
    {
        Assert.True(result.IsAvailable);
        Assert.Null(result.Code);
        Assert.NotNull(result.Payload);
        Assert.Equal(expectedOperation, Text(result.Payload!["operation"]));
        Assert.Equal("observed", Text(result.Payload["state"]));
    }

    private static void AssertUnavailable(NativeSceneBridgeCallResult result)
    {
        Assert.False(result.IsAvailable);
        Assert.Equal("OBSERVER_UNAVAILABLE", result.Code);
        Assert.Null(result.Payload);
    }

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();

    private sealed class LocalNamedPipeObserver : IAsyncDisposable
    {
        private readonly NamedPipeServerStream _server;
        private Task? _connection;
        private int _requestsObserved;

        public LocalNamedPipeObserver()
        {
            PipeName = $"native-scene-bridge-{Guid.NewGuid():N}";
            _server = new NamedPipeServerStream(
                PipeName,
                PipeDirection.InOut,
                maxNumberOfServerInstances: 1,
                PipeTransmissionMode.Byte,
                PipeOptions.Asynchronous);
        }

        public string PipeName { get; }

        public int RequestsObservedBeforeFirstResponse { get; private set; }

        public bool HasWrittenResponse { get; private set; }

        public async Task<BridgeRequest> ReadRequestAsync(CancellationToken cancellationToken)
        {
            var request = await ReadRequestOrEndAsync(cancellationToken);
            return request ?? throw new EndOfStreamException("The bridge closed its pipe before writing a complete request frame.");
        }

        public async Task<BridgeRequest?> TryReadRequestUntilStoppedAsync(CancellationToken stopToken)
        {
            try
            {
                return await ReadRequestOrEndAsync(stopToken);
            }
            catch (OperationCanceledException) when (stopToken.IsCancellationRequested)
            {
                return null;
            }
        }

        public async Task WriteResponseAsync(JsonObject response, CancellationToken cancellationToken)
        {
            HasWrittenResponse = true;
            await WriteFrameAsync(_server, JsonSerializer.SerializeToUtf8Bytes(response), cancellationToken);
        }

        public async Task WriteResponseLengthAsync(int length, CancellationToken cancellationToken)
        {
            HasWrittenResponse = true;
            var header = new byte[sizeof(int)];
            BinaryPrimitives.WriteInt32LittleEndian(header, length);
            await _server.WriteAsync(header, cancellationToken);
            await _server.FlushAsync(cancellationToken);
        }

        public async Task AssertClientDisconnectedAsync(CancellationToken cancellationToken)
        {
            var buffer = new byte[1];
            var read = await _server.ReadAsync(buffer, cancellationToken);
            Assert.Equal(0, read);
        }

        public ValueTask DisposeAsync()
        {
            _server.Dispose();
            return ValueTask.CompletedTask;
        }

        private async Task<BridgeRequest?> ReadRequestOrEndAsync(CancellationToken cancellationToken)
        {
            await EnsureConnectionAsync(cancellationToken);
            var payload = await ReadFrameOrEndAsync(_server, FakeFrameLimit, cancellationToken);
            if (payload is null)
            {
                return null;
            }

            var document = JsonNode.Parse(payload) as JsonObject
                ?? throw new InvalidDataException("The bridge request frame must be a JSON object.");
            var request = new BridgeRequest(
                Text(document["nonce"]),
                Text(document["correlationId"]),
                document["request"] as JsonObject
                    ?? throw new InvalidDataException("The bridge request frame is missing its JSON object request."));

            _requestsObserved++;
            if (!HasWrittenResponse)
            {
                RequestsObservedBeforeFirstResponse++;
            }

            return request;
        }

        private Task EnsureConnectionAsync(CancellationToken cancellationToken) =>
            _connection ??= _server.WaitForConnectionAsync(cancellationToken);
    }

    private sealed class NativeSceneBridgeClientDriver : IAsyncDisposable
    {
        private const string ProductionAssemblyName = "NetCoreDbg.Mcp.Stateless";
        private const string ClientTypeName = "NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneBridgeClient";

        private readonly IAsyncDisposable _client;
        private readonly object _instance;
        private readonly MethodInfo _sendAsync;

        private NativeSceneBridgeClientDriver(IAsyncDisposable client, object instance, MethodInfo sendAsync)
        {
            _client = client;
            _instance = instance;
            _sendAsync = sendAsync;
        }

        public static NativeSceneBridgeClientDriver Create(string pipeName)
        {
            var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(
                TestOutputPathResolver.ResolveManagedAssembly(
                    RepositoryLayout.Root,
                    Path.Combine("host", ProductionAssemblyName),
                    ProductionAssemblyName));
            var clientType = assembly.GetType(ClientTypeName, throwOnError: false)
                ?? throw new InvalidOperationException(
                    $"Missing production contract: type '{ClientTypeName}' is absent from '{assembly.Location}'. " +
                    "T017 must implement it without changing this RED suite.");
            var constructor = clientType.GetConstructor(
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                binder: null,
                types:
                [
                    typeof(string),
                    typeof(TimeSpan),
                    typeof(TimeSpan),
                    typeof(TimeSpan),
                    typeof(int),
                    typeof(int),
                ],
                modifiers: null)
                ?? throw new InvalidOperationException(
                    "NativeSceneBridgeClient must accept pipeName, connect timeout, write timeout, read timeout, maximum request bytes, and maximum response bytes.");
            var sendAsync = clientType.GetMethod(
                "SendAsync",
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                binder: null,
                types: [typeof(string), typeof(JsonObject), typeof(CancellationToken)],
                modifiers: null)
                ?? throw new InvalidOperationException(
                    "NativeSceneBridgeClient must expose SendAsync(string authorizationNonce, JsonObject request, CancellationToken cancellationToken).");
            var instance = constructor.Invoke(
            [
                pipeName,
                ConnectTimeout,
                WriteTimeout,
                ReadTimeout,
                MaximumRequestBytes,
                MaximumResponseBytes,
            ]);
            var client = instance as IAsyncDisposable
                ?? throw new InvalidOperationException("NativeSceneBridgeClient must implement IAsyncDisposable for bounded pipe cleanup.");

            return new NativeSceneBridgeClientDriver(client, instance, sendAsync);
        }

        public async Task<NativeSceneBridgeCallResult> SendAsync(string authorizationNonce, JsonObject request, CancellationToken cancellationToken)
        {
            var value = await AwaitResultAsync(
                _sendAsync.Invoke(_instance, [authorizationNonce, request, cancellationToken]),
                "NativeSceneBridgeClient.SendAsync");
            var resultType = value.GetType();
            var isAvailable = RequireReadableProperty(resultType, "IsAvailable", typeof(bool));
            var code = RequireReadableProperty(resultType, "Code", typeof(string));
            var payload = RequireReadableProperty(resultType, "Payload", typeof(JsonObject));

            return new NativeSceneBridgeCallResult(
                Assert.IsType<bool>(isAvailable.GetValue(value)),
                code.GetValue(value) as string,
                payload.GetValue(value) as JsonObject);
        }

        public ValueTask DisposeAsync() => _client.DisposeAsync();

        private static async Task<object> AwaitResultAsync(object? pending, string memberName)
        {
            if (pending is null)
            {
                throw new InvalidOperationException($"{memberName} returned null instead of Task<T> or ValueTask<T>.");
            }

            Task task;
            if (pending is Task directTask)
            {
                task = directTask;
            }
            else
            {
                var asTask = pending.GetType().GetMethod("AsTask", Type.EmptyTypes)
                    ?? throw new InvalidOperationException($"{memberName} must return Task<T> or ValueTask<T>.");
                task = asTask.Invoke(pending, []) as Task
                    ?? throw new InvalidOperationException($"{memberName}.AsTask() did not return a Task.");
            }

            await task.ConfigureAwait(false);
            var result = task.GetType().GetProperty("Result", BindingFlags.Instance | BindingFlags.Public)?.GetValue(task);
            return result ?? throw new InvalidOperationException($"{memberName} must return Task<T> or ValueTask<T>.");
        }

        private static PropertyInfo RequireReadableProperty(Type resultType, string name, Type expectedType)
        {
            var property = resultType.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                ?? throw new InvalidOperationException($"NativeSceneBridgeCallResult is missing readable {name}.");
            if (!property.CanRead || property.PropertyType != expectedType)
            {
                throw new InvalidOperationException($"NativeSceneBridgeCallResult.{name} must be readable {expectedType.Name}.");
            }

            return property;
        }
    }

    private sealed record BridgeRequest(string Nonce, string CorrelationId, JsonObject Request);

    private sealed record NativeSceneBridgeCallResult(bool IsAvailable, string? Code, JsonObject? Payload);

    private static async Task<byte[]?> ReadFrameOrEndAsync(Stream stream, int maximumPayloadBytes, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        var firstRead = await stream.ReadAsync(header.AsMemory(0, 1), cancellationToken);
        if (firstRead == 0)
        {
            return null;
        }

        await ReadExactlyAsync(stream, header.AsMemory(firstRead), cancellationToken);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length <= 0 || length > maximumPayloadBytes)
        {
            throw new InvalidDataException($"Pipe frame length {length} is outside 1..{maximumPayloadBytes}.");
        }

        var payload = new byte[length];
        await ReadExactlyAsync(stream, payload, cancellationToken);
        return payload;
    }

    private static async Task WriteFrameAsync(Stream stream, byte[] payload, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await stream.WriteAsync(header, cancellationToken);
        await stream.WriteAsync(payload, cancellationToken);
        await stream.FlushAsync(cancellationToken);
    }

    private static async Task ReadExactlyAsync(Stream stream, Memory<byte> buffer, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer[offset..], cancellationToken);
            if (read == 0)
            {
                throw new EndOfStreamException("Pipe frame ended before all declared bytes were received.");
            }

            offset += read;
        }
    }
}
