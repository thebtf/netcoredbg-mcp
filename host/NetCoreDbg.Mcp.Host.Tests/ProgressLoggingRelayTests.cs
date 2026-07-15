using Microsoft.Extensions.DependencyInjection;
using System.Collections.Concurrent;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Proves FD-002's <see cref="ProgressLoggingRelay"/> against real SDK endpoints
/// (<c>McpServer</c>/<c>McpClient</c>) over an in-memory <see cref="DuplexChannel"/> - never mocks.
/// Composition is built by <see cref="ProgressLoggingComposition"/>, which wires this module exactly
/// as the reported integration hook describes; production composition (<c>RelayComposition.cs</c>)
/// is untouched and continues to advertise no logging capability and no progress/logging routes until
/// an integrator adds them. The real-stdio cases below cover the same contracts against a real Python
/// child process instead of a fake in-memory one.
///
/// Ordering-sensitive assertions never rely on <see cref="McpClientHandlers.NotificationHandlers"/> for
/// *observation*: SDK 1.4.1 dispatches every incoming message (requests, responses, and notifications
/// alike) via an unawaited fire-and-forget task on the reading side too, so a client's own notification
/// handlers can be invoked out of the order the messages actually arrived - the same hazard this
/// module's <c>WrapUpstreamTransport</c> closes on the upstream leg. Tests that need true arrival order
/// use <see cref="SequentialOrderObserverTransport"/>, a test-only transport wrapper built the same way,
/// to observe the downstream leg reliably.
/// </summary>
public sealed class ProgressLoggingRelayTests
{
    private const string SlowProbeToolName = "probe";
    private const string FastProbeToolName = "probe-fast";

    /// <summary>Records events in the order this process observed them, for cross-checking relative
    /// order between concurrently-dispatched notifications and request completions.</summary>
    private sealed class EventLog
    {
        private int _sequence;

        public ConcurrentQueue<(int Sequence, string Label)> Events { get; } = new();

        public int Record(string label)
        {
            var sequence = Interlocked.Increment(ref _sequence);
            Events.Enqueue((sequence, label));
            return sequence;
        }
    }

    /// <summary>
    /// Test-only transport wrapper that observes every message a real downstream <see cref="McpClient"/>
    /// receives in the exact order this wrapper's own single sequential reader loop consumed them from
    /// the wrapped transport, before handing each message off unchanged to the wrapped
    /// <see cref="McpClient"/> for its own normal processing. This is the reliable oracle for
    /// arrival-order assertions in this file - see the type's own class doc comment for why
    /// <see cref="McpClientHandlers.NotificationHandlers"/> is not.
    /// </summary>
    private sealed class SequentialOrderObserverTransport(IClientTransport inner, EventLog log) : IClientTransport
    {
        public string Name => inner.Name;

        public async Task<ITransport> ConnectAsync(CancellationToken cancellationToken = default)
        {
            var innerTransport = await inner.ConnectAsync(cancellationToken).ConfigureAwait(false);
            return new ObservingSessionTransport(innerTransport, log);
        }

        private sealed class ObservingSessionTransport : ITransport
        {
            private readonly ITransport _inner;
            private readonly EventLog _log;
            private readonly ConcurrentDictionary<string, string> _progressTokensByRequestId = new();
            private readonly Channel<JsonRpcMessage> _passthrough =
                Channel.CreateUnbounded<JsonRpcMessage>(new UnboundedChannelOptions { SingleReader = true, SingleWriter = true });
            private readonly Task _pumpTask;

            public ObservingSessionTransport(ITransport inner, EventLog log)
            {
                _inner = inner;
                _log = log;
                _pumpTask = PumpAsync();
            }

            public string? SessionId => _inner.SessionId;

            public ChannelReader<JsonRpcMessage> MessageReader => _passthrough.Reader;

            public Task SendMessageAsync(JsonRpcMessage message, CancellationToken cancellationToken = default)
            {
                if (message is JsonRpcRequest { Method: RequestMethods.ToolsCall } request
                    && request.Params?.Deserialize<CallToolRequestParams>(McpJsonUtilities.DefaultOptions)?.ProgressToken is { } token)
                {
                    _progressTokensByRequestId[request.Id.ToString()] = token.ToString()!;
                }

                return _inner.SendMessageAsync(message, cancellationToken);
            }

            private async Task PumpAsync()
            {
                Exception? failure = null;
                try
                {
                    await foreach (var message in _inner.MessageReader.ReadAllAsync().ConfigureAwait(false))
                    {
                        switch (message)
                        {
                            case JsonRpcNotification { Method: NotificationMethods.ProgressNotification } progress:
                                var progressParams = progress.Params.Deserialize<ProgressNotificationParams>(McpJsonUtilities.DefaultOptions)!;
                                _log.Record($"progress|{progressParams.ProgressToken}|{progressParams.Progress.Progress}");
                                break;

                            case JsonRpcNotification { Method: NotificationMethods.LoggingMessageNotification } logging:
                                var loggingParams = logging.Params.Deserialize<LoggingMessageNotificationParams>(McpJsonUtilities.DefaultOptions)!;
                                _log.Record($"log|{loggingParams.Logger}|{loggingParams.Level}|{loggingParams.Data.GetString()}");
                                break;

                            case JsonRpcResponse response:
                                var responseId = response.Id.ToString();
                                _log.Record(_progressTokensByRequestId.TryRemove(responseId, out var token)
                                    ? $"response|{token}"
                                    : $"response|{responseId}");
                                break;
                        }

                        await _passthrough.Writer.WriteAsync(message).ConfigureAwait(false);
                    }
                }
                catch (Exception ex)
                {
                    failure = ex;
                }
                finally
                {
                    _passthrough.Writer.TryComplete(failure);
                }
            }

            public async ValueTask DisposeAsync()
            {
                await _inner.DisposeAsync().ConfigureAwait(false);
                try
                {
                    await _pumpTask.ConfigureAwait(false);
                }
                catch
                {
                    // The pump's own failure (if any) already completed _passthrough with it.
                }
            }
        }
    }

    private static Task<McpClient> CreateObservedDownstreamClientAsync(DuplexChannel channel, EventLog log) =>
        McpClient.CreateAsync(new SequentialOrderObserverTransport(channel.CreateClientTransport(), log));

    /// <summary>A fake Python advertising Tools (and, optionally, Logging). Its <c>probe</c> tool
    /// reports <paramref name="steps"/> increasing progress notifications interleaved with log
    /// messages for the caller's own progress token, invoking <paramref name="afterStep"/> (a no-op
    /// unless supplied) between steps, then returns a structured result; its independent
    /// <c>probe-fast</c> tool always runs two steps with no delay, so a test can prove the session
    /// stays usable after cancelling/disconnecting a slow <c>probe</c> call without inheriting that
    /// call's own delay configuration.</summary>
    private static FakePythonServer StartProbeFakePython(
        DuplexChannel channel,
        int steps = 3,
        bool advertiseLogging = false,
        McpRequestHandler<SetLevelRequestParams, EmptyResult>? setLoggingLevelHandler = null,
        Func<int, CancellationToken, Task>? afterStep = null)
    {
        async ValueTask<CallToolResult> RunProbeAsync(
            RequestContext<CallToolRequestParams> context, int stepCount, Func<int, CancellationToken, Task>? afterStepHook, CancellationToken cancellationToken)
        {
            var token = context.Params?.ProgressToken;

            for (var step = 1; step <= stepCount; step++)
            {
                if (token is { } activeToken)
                {
                    await context.Server.SendMessageAsync(
                        new JsonRpcNotification
                        {
                            Method = NotificationMethods.ProgressNotification,
                            Params = JsonSerializer.SerializeToNode(
                                new ProgressNotificationParams
                                {
                                    ProgressToken = activeToken,
                                    Progress = new ProgressNotificationValue { Progress = step, Total = stepCount },
                                },
                                McpJsonUtilities.DefaultOptions),
                        },
                        cancellationToken).ConfigureAwait(false);
                }

                await context.Server.SendMessageAsync(
                    new JsonRpcNotification
                    {
                        Method = NotificationMethods.LoggingMessageNotification,
                        Params = JsonSerializer.SerializeToNode(
                            new LoggingMessageNotificationParams
                            {
                                Level = LoggingLevel.Info,
                                Logger = "fake-python",
                                Data = JsonSerializer.SerializeToElement($"log-{step}"),
                            },
                            McpJsonUtilities.DefaultOptions),
                    },
                    cancellationToken).ConfigureAwait(false);

                if (afterStepHook is not null)
                {
                    await afterStepHook(step, cancellationToken).ConfigureAwait(false);
                }
            }

            return new CallToolResult { Content = [new TextContentBlock { Text = "done" }] };
        }

        var options = new McpServerOptions
        {
            ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
            Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
            Handlers = new McpServerHandlers
            {
                ListToolsHandler = (context, cancellationToken) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                CallToolHandler = (context, cancellationToken) => context.Params?.Name == FastProbeToolName
                    ? RunProbeAsync(context, stepCount: 2, afterStepHook: null, cancellationToken)
                    : RunProbeAsync(context, steps, afterStep, cancellationToken),
                SetLoggingLevelHandler = setLoggingLevelHandler,
            },
        };

        if (!advertiseLogging)
        {
            // SDK 1.4.1's McpServerImpl.ConfigureLogging unconditionally sets ServerCapabilities.Logging
            // regardless of options.Capabilities (verified directly against the compiled SDK, same as
            // RelayRouteCatalog.SuppressUnregisteredLogging's own baseline finding for the host itself).
            // To make this fake accurately represent "Python has no logging capability" the way real,
            // unmodified netcoredbg_mcp genuinely does not, strip the same JSON key from this fake's own
            // initialize response.
            options.Filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
            {
                if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                    && result.TryGetPropertyValue("capabilities", out var capabilitiesNode)
                    && capabilitiesNode is JsonObject capabilities)
                {
                    capabilities.Remove("logging");
                }

                await next(context, cancellationToken).ConfigureAwait(false);
            });
        }

        return FakePythonServer.Start(channel, options);
    }

    private static CallToolRequestParams ProbeCall(string progressToken) => new()
    {
        Name = SlowProbeToolName,
        Meta = new JsonObject { ["progressToken"] = progressToken },
    };

    private static CallToolRequestParams FastProbeCall(string progressToken) => new()
    {
        Name = FastProbeToolName,
        Meta = new JsonObject { ["progressToken"] = progressToken },
    };

    [Fact]
    public async Task MultipleConcurrentProgressTokens_EachPreservesMonotonicOrderIndependently()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var fakePython = StartProbeFakePython(upstreamChannel, steps: 5);
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        var log = new EventLog();
        await using var downstreamClient = await CreateObservedDownstreamClientAsync(downstreamChannel, log);

        var callA = downstreamClient.CallToolAsync(ProbeCall("token-a"));
        var callB = downstreamClient.CallToolAsync(ProbeCall("token-b"));
        await Task.WhenAll(callA.AsTask(), callB.AsTask());

        var progressValuesByToken = log.Events
            .Where(e => e.Label.StartsWith("progress|", StringComparison.Ordinal))
            .OrderBy(e => e.Sequence)
            .Select(e => e.Label.Split('|'))
            .GroupBy(parts => parts[1], parts => float.Parse(parts[2]));

        foreach (var tokenGroup in progressValuesByToken)
        {
            var observed = tokenGroup.ToArray();
            Assert.Equal(5, observed.Length);
            Assert.Equal([1f, 2f, 3f, 4f, 5f], observed);
        }

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task Progress_NeverArrivesAfterOwningCallsTerminalResult()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var fakePython = StartProbeFakePython(upstreamChannel, steps: 6);
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        var log = new EventLog();
        await using var downstreamClient = await CreateObservedDownstreamClientAsync(downstreamChannel, log);

        var result = await downstreamClient.CallToolAsync(ProbeCall("token-x"));
        await Task.Delay(TimeSpan.FromMilliseconds(50));

        Assert.False(result.IsError == true);
        var responseSequence = log.Events.Single(e => e.Label == "response|token-x").Sequence;
        var progressSequences = log.Events
            .Where(e => e.Label.StartsWith("progress|token-x|", StringComparison.Ordinal))
            .Select(e => e.Sequence)
            .ToArray();
        Assert.Equal(6, progressSequences.Length);
        Assert.All(progressSequences, sequence => Assert.True(
            sequence < responseSequence,
            $"progress observed at sequence {sequence} arrived at or after the terminal result at sequence {responseSequence}"));

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task LoggingNotifications_PreserveSendOrderAndStructuredFields()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var fakePython = StartProbeFakePython(upstreamChannel, steps: 4);
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        var log = new EventLog();
        await using var downstreamClient = await CreateObservedDownstreamClientAsync(downstreamChannel, log);

        await downstreamClient.CallToolAsync(ProbeCall("token-y"));

        var observedLogs = log.Events
            .Where(e => e.Label.StartsWith("log|", StringComparison.Ordinal))
            .OrderBy(e => e.Sequence)
            .Select(e => e.Label)
            .ToArray();
        Assert.Equal(
            ["log|fake-python|Info|log-1", "log|fake-python|Info|log-2", "log|fake-python|Info|log-3", "log|fake-python|Info|log-4"],
            observedLogs);

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task SetLoggingLevel_CapabilityAbsent_RejectsWithMethodNotFoundAndStripsCapabilityKey()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var fakePython = StartProbeFakePython(upstreamChannel, advertiseLogging: false);
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        Assert.Null(downstreamClient.ServerCapabilities?.Logging);

        var error = await Assert.ThrowsAsync<McpProtocolException>(() => downstreamClient.SetLoggingLevelAsync(LoggingLevel.Info));
        Assert.Equal((int)McpErrorCode.MethodNotFound, (int)error.ErrorCode);
        Assert.Contains("Method not found", error.Message);

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task SetLoggingLevel_CapabilityPresent_ForwardsToPythonAndPreservesCapabilityKey()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        LoggingLevel? observedLevel = null;
        var fakePython = StartProbeFakePython(
            upstreamChannel,
            advertiseLogging: true,
            setLoggingLevelHandler: (context, cancellationToken) =>
            {
                observedLevel = context.Params.Level;
                return ValueTask.FromResult(new EmptyResult());
            });
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        Assert.NotNull(downstreamClient.ServerCapabilities?.Logging);

        await downstreamClient.SetLoggingLevelAsync(LoggingLevel.Warning);
        Assert.Equal(LoggingLevel.Warning, observedLevel);

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task NonConsumingDownstreamClient_ToolCallStillCompletesWithoutDeadlock()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var fakePython = StartProbeFakePython(upstreamChannel, steps: 4);
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        // No NotificationHandlers registered at all: this client neither advertised nor consumes
        // progress/logging notifications - it must still receive its own tool result promptly.
        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var result = await downstreamClient.CallToolAsync(ProbeCall("token-z")).AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Assert.False(result.IsError == true);

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task Cancellation_DownstreamCancelsMidRun_CleansUpAndSessionStaysUsable()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var firstStepStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var fakePython = StartProbeFakePython(
            upstreamChannel,
            steps: 1000,
            afterStep: async (step, cancellationToken) =>
            {
                if (step == 1)
                {
                    firstStepStarted.TrySetResult();
                }

                await Task.Delay(TimeSpan.FromMilliseconds(20), cancellationToken).ConfigureAwait(false);
            });
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        using var deadline = new CancellationTokenSource();
        var slowCall = downstreamClient.CallToolAsync(ProbeCall("token-slow"), cancellationToken: deadline.Token);
        await firstStepStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));
        deadline.Cancel();

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => slowCall.AsTask());

        // The relay's upstream pump and the underlying connection must both remain healthy: an
        // unrelated, independent (always-fast) call still completes normally afterward.
        var followUp = await downstreamClient.CallToolAsync(FastProbeCall("token-after-cancel")).AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Assert.False(followUp.IsError == true);

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task DownstreamDisconnect_DuringInFlightProgress_SessionEndsCleanlyWithoutException()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var firstStepStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var fakePython = StartProbeFakePython(
            upstreamChannel,
            steps: 1000,
            afterStep: async (step, cancellationToken) =>
            {
                if (step == 1)
                {
                    firstStepStarted.TrySetResult();
                }

                await Task.Delay(TimeSpan.FromMilliseconds(20), cancellationToken).ConfigureAwait(false);
            });
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());
        _ = downstreamClient.CallToolAsync(ProbeCall("token-disconnect"));
        await firstStepStarted.Task.WaitAsync(TimeSpan.FromSeconds(10));

        // Simulate the downstream client disconnecting mid-flight while progress is still arriving.
        await downstreamClient.DisposeAsync();

        await session.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task SetLoggingLevel_PythonSideError_PreservesErrorCodeAndMessage()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var fakePython = StartProbeFakePython(
            upstreamChannel,
            advertiseLogging: true,
            setLoggingLevelHandler: (context, cancellationToken) =>
                throw new McpProtocolException("invalid level requested by test", McpErrorCode.InvalidParams));
        var (session, host) = ProgressLoggingComposition.Build(
            upstreamChannel.CreateClientTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var error = await Assert.ThrowsAsync<McpProtocolException>(() => downstreamClient.SetLoggingLevelAsync(LoggingLevel.Info));

        // Register() calls RelaySession.ForwardRequestAsync directly and never catches or rewraps its
        // result, so Python's own protocol error code and message text must survive the relay
        // unchanged, regardless of exactly how the shared forwarding primitive formats or wraps the
        // message on top of them.
        Assert.Equal((int)McpErrorCode.InvalidParams, (int)error.ErrorCode);
        Assert.Contains("invalid level requested by test", error.Message);

        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    private sealed class RealProbePython : IAsyncDisposable
    {
        private static readonly string RepoRoot = Path.GetFullPath(
            Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));

        private readonly string _packageRoot;

        private RealProbePython(PythonBackendProcess backend, string packageRoot)
        {
            Backend = backend;
            _packageRoot = packageRoot;
        }

        public PythonBackendProcess Backend { get; }

        public static RealProbePython Start(bool advertiseLogging)
        {
            var pythonExecutable = Path.Combine(
                RepoRoot,
                ".venv",
                OperatingSystem.IsWindows() ? Path.Combine("Scripts", "python.exe") : Path.Combine("bin", "python"));
            Assert.True(File.Exists(pythonExecutable), $"expected the worktree venv interpreter at {pythonExecutable}");

            var fixturePath = Path.Combine(RepoRoot, "tests", "fixtures", "fd002_notification_probe_server.py");
            Assert.True(File.Exists(fixturePath), $"expected the FD-002 probe fixture at {fixturePath}");

            var packageRoot = Path.Combine(Path.GetTempPath(), $"netcoredbg-mcp-fd002-{Guid.NewGuid():N}");
            var packageDir = Path.Combine(packageRoot, "netcoredbg_mcp");
            Directory.CreateDirectory(packageDir);
            File.WriteAllText(Path.Combine(packageDir, "__init__.py"), string.Empty);
            File.WriteAllText(
                Path.Combine(packageDir, "__main__.py"),
                $"import runpy\nrunpy.run_path({JsonSerializer.Serialize(fixturePath)}, run_name=\"__main__\")\n");

            var originalPython = Environment.GetEnvironmentVariable("NETCOREDBG_MCP_PYTHON_EXECUTABLE");
            var originalPythonPath = Environment.GetEnvironmentVariable("PYTHONPATH");
            try
            {
                Environment.SetEnvironmentVariable("NETCOREDBG_MCP_PYTHON_EXECUTABLE", pythonExecutable);
                Environment.SetEnvironmentVariable("PYTHONPATH", packageRoot);
                var backend = PythonBackendProcess.Start(advertiseLogging ? ["--with-logging-capability"] : []);
                return new RealProbePython(backend, packageRoot);
            }
            catch
            {
                Directory.Delete(packageRoot, recursive: true);
                throw;
            }
            finally
            {
                Environment.SetEnvironmentVariable("NETCOREDBG_MCP_PYTHON_EXECUTABLE", originalPython);
                Environment.SetEnvironmentVariable("PYTHONPATH", originalPythonPath);
            }
        }

        public async ValueTask DisposeAsync()
        {
            try
            {
                await Backend.StopAsync().ConfigureAwait(false);
                await Backend.WaitForStderrForwardedAsync().ConfigureAwait(false);
            }
            finally
            {
                Backend.Dispose();
                Directory.Delete(_packageRoot, recursive: true);
            }
        }
    }

    private static CallToolRequestParams RealProbeCall(
        string progressToken,
        int steps,
        double holdSeconds,
        string loggerName) => new()
        {
            Name = "emit_progress_and_logs",
            Arguments = new Dictionary<string, JsonElement>
            {
                ["steps"] = JsonSerializer.SerializeToElement(steps),
                ["hold_seconds"] = JsonSerializer.SerializeToElement(holdSeconds),
                ["logger_name"] = JsonSerializer.SerializeToElement(loggerName),
            },
            Meta = new JsonObject { ["progressToken"] = progressToken },
        };

    private static async Task WaitForEventAsync(EventLog log, Func<string, bool> predicate)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        while (!log.Events.Any(e => predicate(e.Label)))
        {
            await Task.Delay(TimeSpan.FromMilliseconds(10), deadline.Token).ConfigureAwait(false);
        }
    }


    [Fact]
    public async Task RealStdio_InterleavedTokensLogsAndSetLevelPreservePerCallOrder()
    {
        await using var probePython = RealProbePython.Start(advertiseLogging: true);
        var downstreamChannel = new DuplexChannel();
        var (session, host) = ProgressLoggingComposition.Build(
            probePython.Backend.CreateUpstreamTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        var log = new EventLog();
        await using var downstreamClient = await CreateObservedDownstreamClientAsync(downstreamChannel, log)
            .WaitAsync(TimeSpan.FromSeconds(20));

        Assert.NotNull(downstreamClient.ServerCapabilities?.Logging);
        await downstreamClient.SetLoggingLevelAsync(LoggingLevel.Warning).WaitAsync(TimeSpan.FromSeconds(10));

        var callA = downstreamClient.CallToolAsync(RealProbeCall("real-a", 4, 0.01, "real-a"));
        var callB = downstreamClient.CallToolAsync(RealProbeCall("real-b", 4, 0.01, "real-b"));
        var results = await Task.WhenAll(callA.AsTask(), callB.AsTask()).WaitAsync(TimeSpan.FromSeconds(20));
        await Task.Delay(TimeSpan.FromMilliseconds(50));

        Assert.All(results, result =>
        {
            Assert.False(result.IsError == true);
            var text = Assert.IsType<TextContentBlock>(Assert.Single(result.Content));
            using var structured = JsonDocument.Parse(text.Text);
            Assert.Equal("warning", structured.RootElement.GetProperty("active_logging_level").GetString());
        });

        foreach (var token in new[] { "real-a", "real-b" })
        {
            var responseSequence = log.Events.Single(e => e.Label == $"response|{token}").Sequence;
            var progress = log.Events
                .Where(e => e.Label.StartsWith($"progress|{token}|", StringComparison.Ordinal))
                .OrderBy(e => e.Sequence)
                .ToArray();
            var logs = log.Events
                .Where(e => e.Label.StartsWith($"log|{token}|", StringComparison.Ordinal))
                .OrderBy(e => e.Sequence)
                .ToArray();

            Assert.Equal(["1", "2", "3", "4"], progress.Select(e => e.Label.Split('|')[2]));
            Assert.Equal(
                [$"log|{token}|Info|log-1", $"log|{token}|Info|log-2", $"log|{token}|Info|log-3", $"log|{token}|Info|log-4"],
                logs.Select(e => e.Label));
            Assert.All(progress, item => Assert.True(item.Sequence < responseSequence));
            Assert.All(logs, item => Assert.True(item.Sequence < responseSequence));
        }

        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
        host.Dispose();
        await session.DisposeAsync();
    }


    [Fact]
    public async Task RealStdio_CancellationReturnsPromptlyAndLeavesSessionUsable()
    {
        await using var probePython = RealProbePython.Start(advertiseLogging: false);
        var downstreamChannel = new DuplexChannel();
        var (session, host) = ProgressLoggingComposition.Build(
            probePython.Backend.CreateUpstreamTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        var log = new EventLog();
        await using var downstreamClient = await CreateObservedDownstreamClientAsync(downstreamChannel, log)
            .WaitAsync(TimeSpan.FromSeconds(20));
        Assert.Null(downstreamClient.ServerCapabilities?.Logging);

        using var cancellation = new CancellationTokenSource();
        var slowCall = downstreamClient.CallToolAsync(
            RealProbeCall("real-cancel", 1000, 0.02, "real-cancel"),
            cancellationToken: cancellation.Token);
        await WaitForEventAsync(log, label => label.StartsWith("progress|real-cancel|", StringComparison.Ordinal));
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => slowCall.AsTask());


        var followUp = await downstreamClient.CallToolAsync(RealProbeCall("real-after-cancel", 2, 0, "real-after-cancel"))
            .AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        Assert.False(followUp.IsError == true);

        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
        host.Dispose();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task RealStdio_DownstreamDisconnectDuringProgressCleansUpDeterministically()
    {
        await using var probePython = RealProbePython.Start(advertiseLogging: false);
        var downstreamChannel = new DuplexChannel();
        var (session, host) = ProgressLoggingComposition.Build(
            probePython.Backend.CreateUpstreamTransport,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));

        var log = new EventLog();
        var downstreamClient = await CreateObservedDownstreamClientAsync(downstreamChannel, log)
            .WaitAsync(TimeSpan.FromSeconds(20));
        _ = downstreamClient.CallToolAsync(RealProbeCall("real-disconnect", 1000, 0.02, "real-disconnect"));
        await WaitForEventAsync(log, label => label.StartsWith("progress|real-disconnect|", StringComparison.Ordinal));

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(10));
        await host.StopAsync().WaitAsync(TimeSpan.FromSeconds(10));
        host.Dispose();
    }

    [Fact]
    public void Register_RecordsExactlyItsOwnRoutesWithoutDuplicateConflict()
    {
        var catalog = new RelayRouteCatalog();
        var upstreamChannel = new DuplexChannel();
        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);

        var builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(Array.Empty<string>());
        var mcpBuilder = builder.Services.AddMcpServer();
        ProgressLoggingRelay.Register(mcpBuilder, catalog, session);

        Assert.Contains(
            catalog.Routes,
            route => route.Method == RequestMethods.LoggingSetLevel && route.Direction == RelayDirection.DownstreamToUpstream
                && route.Kind == RelayRouteKind.Request);
        Assert.Contains(
            catalog.Routes,
            route => route.Method == NotificationMethods.ProgressNotification && route.Direction == RelayDirection.UpstreamToDownstream
                && route.Kind == RelayRouteKind.Notification);
        Assert.Contains(
            catalog.Routes,
            route => route.Method == NotificationMethods.LoggingMessageNotification && route.Direction == RelayDirection.UpstreamToDownstream
                && route.Kind == RelayRouteKind.Notification);

        // Same (direction, method) registered again must still fail fast, per the FD-000 contract.
        Assert.Throws<InvalidOperationException>(() =>
            catalog.Add(new RelayRoute(RequestMethods.LoggingSetLevel, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request)));
    }
}
