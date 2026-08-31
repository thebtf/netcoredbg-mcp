using System.Diagnostics;
using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.NativeScene;

namespace NetCoreDbg.Mcp.Stateless;

internal static class Program
{
    private const string ProtocolVersion = "2026-07-28";
    private const string UnixProcessGroupProxy = "--unix-process-group-proxy";
    private static readonly TimeSpan CacheLifetime = TimeSpan.FromMinutes(5);

    private static async Task Main(string[] arguments)
    {
        if (!OperatingSystem.IsWindows()
            && arguments.Length == 3
            && string.Equals(arguments[0], UnixProcessGroupProxy, StringComparison.Ordinal)
            && !string.IsNullOrWhiteSpace(arguments[1])
            && !string.IsNullOrWhiteSpace(arguments[2]))
        {
            await RunUnixProcessGroupProxyAsync(arguments[1], arguments[2]).ConfigureAwait(false);
            return;
        }

        var sessions = new DebugSessionRegistry(
            Environment.GetEnvironmentVariable("NETCOREDBG_PATH"),
            Environment.GetEnvironmentVariable("FLAUI_BRIDGE_PATH"),
            Environment.GetEnvironmentVariable("NETCOREDBG_MCP_ARTIFACT_ROOT"));
        var host = BuildHost(sessions);
        await host.RunAsync().ConfigureAwait(false);
    }

    private static IHost BuildHost(DebugSessionRegistry sessions)
    {
        var builder = Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Logging.AddConsole(options => options.LogToStandardErrorThreshold = LogLevel.Trace);
        builder.Services.AddSingleton(sessions);
        builder.Services.AddHostedService(_ => new SessionDisposer(sessions));

        builder.Services.AddMcpServer(options =>
            {
                options.ProtocolVersion = ProtocolVersion;
                options.ServerInfo = new Implementation { Name = "netcoredbg-mcp-stateless", Version = "1.0.0" };
                options.Capabilities = new ServerCapabilities { Tools = new ToolsCapability { ListChanged = false } };
            })
            .WithStdioServerTransport()
            .WithListToolsHandler((context, cancellationToken) =>
                ValueTask.FromResult(ToolCatalog.List()))
            .WithCallToolHandler((context, cancellationToken) =>
                sessions.CallAsync(context, cancellationToken))
            .WithMessageFilters(filters => filters.AddOutgoingFilter(next => async (context, cancellationToken) =>
            {
                if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                    && result.ContainsKey("supportedVersions")
                    && result.ContainsKey("capabilities"))
                {
                    result["ttlMs"] = (long)CacheLifetime.TotalMilliseconds;
                    result["cacheScope"] = "public";
                }

                await next(context, cancellationToken).ConfigureAwait(false);
            }));

        return builder.Build();
    }

    private static async Task RunUnixProcessGroupProxyAsync(string debuggerPath, string terminationControlHandle)
    {
        UnixProcessGroup.BecomeOwnProcessGroup();
        using var terminationControl = new System.IO.Pipes.AnonymousPipeClientStream(
            System.IO.Pipes.PipeDirection.In,
            terminationControlHandle);
        using var debugger = Process.Start(new ProcessStartInfo
        {
            FileName = debuggerPath,
            UseShellExecute = false,
            CreateNoWindow = true,
            ArgumentList = { "--interpreter=vscode" },
        }) ?? throw new InvalidOperationException($"Could not start debugger '{debuggerPath}'.");
        var debuggerExit = debugger.WaitForExitAsync();
        var terminationControlBuffer = new byte[1];
        var terminationRequested = terminationControl.ReadAsync(terminationControlBuffer).AsTask();
        if (await Task.WhenAny(debuggerExit, terminationRequested).ConfigureAwait(false) == terminationRequested)
        {
            if (await terminationRequested.ConfigureAwait(false) != 0)
            {
                await debuggerExit.ConfigureAwait(false);
            }

            UnixProcessGroup.TerminateOwnProcessGroup();
            return;
        }

        await debuggerExit.ConfigureAwait(false);
        await terminationRequested.ConfigureAwait(false);
        UnixProcessGroup.TerminateOwnProcessGroup();
    }


    private sealed class DebugSessionRegistry : IAsyncDisposable
    {
        private const string StartDebug = "start_debug";
        private const string GetDebugState = "get_debug_state";
        private const string StopDebug = "stop_debug";
        private const string GetThreads = "get_threads";
        private const string GetCallStack = "get_call_stack";
        private const string InputRequestId = "start_debug_program";
        private static readonly TimeSpan InitializeTimeout = TimeSpan.FromSeconds(10);
        private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(30);
        private static readonly TimeSpan StopTimeout = TimeSpan.FromSeconds(1);
        private const int MaximumSerializedThreadsBytes = 256 * 1024;
        private const int MaximumSerializedCallStackBytes = 256 * 1024;

        private readonly ConcurrentDictionary<string, NetCoreDbgSession> _sessions = new(StringComparer.Ordinal);
        private readonly ConcurrentDictionary<string, SessionSlot> _slots = new(StringComparer.Ordinal);
        private readonly ConcurrentDictionary<string, NativeSceneSessionBinding> _nativeSceneBindings = new(StringComparer.Ordinal);
        private readonly string? _debuggerPath;
        private readonly string? _bridgePath;
        private readonly string? _artifactRoot;
        private readonly Func<NetCoreDbgSession, bool> _isUsable;
        private readonly Func<NetCoreDbgSession, ValueTask> _dispose;
        private readonly bool _supportsNativeSceneCapture = !StringComparer.OrdinalIgnoreCase.Equals(
            Environment.GetEnvironmentVariable("NETCOREDBG_MCP_NATIVE_SCENE_CAPTURE"),
            "unsupported");

        internal DebugSessionRegistry(string? debuggerPath)
            : this(
                debuggerPath,
                Environment.GetEnvironmentVariable("FLAUI_BRIDGE_PATH"),
                Environment.GetEnvironmentVariable("NETCOREDBG_MCP_ARTIFACT_ROOT"))
        {
        }

        internal DebugSessionRegistry(string? debuggerPath, string? bridgePath, string? artifactRoot)
            : this(debuggerPath, bridgePath, artifactRoot, static session => session.IsUsable)
        {
        }

        private DebugSessionRegistry(string? debuggerPath, Func<NetCoreDbgSession, bool> isUsable)
            : this(debuggerPath, bridgePath: null, artifactRoot: null, isUsable)
        {
        }

        private DebugSessionRegistry(
            string? debuggerPath,
            Func<NetCoreDbgSession, bool> isUsable,
            Func<NetCoreDbgSession, ValueTask> dispose)
            : this(debuggerPath, bridgePath: null, artifactRoot: null, isUsable, dispose)
        {
        }

        private DebugSessionRegistry(
            string? debuggerPath,
            string? bridgePath,
            string? artifactRoot,
            Func<NetCoreDbgSession, bool> isUsable)
            : this(debuggerPath, bridgePath, artifactRoot, isUsable, static session => session.DisposeAsync())
        {
        }

        private DebugSessionRegistry(
            string? debuggerPath,
            string? bridgePath,
            string? artifactRoot,
            Func<NetCoreDbgSession, bool> isUsable,
            Func<NetCoreDbgSession, ValueTask> dispose)
        {
            _debuggerPath = debuggerPath;
            _bridgePath = bridgePath;
            _artifactRoot = artifactRoot;
            _isUsable = isUsable;
            _dispose = dispose;
        }

        internal async ValueTask<CallToolResult> CallAsync(
            RequestContext<CallToolRequestParams> context,
            CancellationToken cancellationToken)
        {
            var request = context.Params;
            return request.Name switch
            {
                StartDebug => await StartAsync(context, cancellationToken).ConfigureAwait(false),
                GetDebugState => await GetStateAsync(request, cancellationToken).ConfigureAwait(false),
                StopDebug => await StopAsync(request, cancellationToken).ConfigureAwait(false),
                GetThreads => await GetThreadsAsync(request, cancellationToken).ConfigureAwait(false),
                GetCallStack => await GetCallStackAsync(request, cancellationToken).ConfigureAwait(false),
                _ when IsNativeSceneTool(request.Name) => await NativeSceneToolDispatcher.DispatchAsync(
                    request.Name,
                    request.Arguments,
                    ResolveNativeSceneBindingAsync,
                    cancellationToken).ConfigureAwait(false),
                _ => UnknownTool(request.Name),
            };
        }

        private async ValueTask<CallToolResult> StartAsync(
            RequestContext<CallToolRequestParams> context,
            CancellationToken cancellationToken)
        {
            if (!TryReadProgram(context.Params.Arguments, out var program, out var hasProgram))
            {
                return InvalidArguments(StartDebug);
            }

            if (!hasProgram)
            {
                program = ReadElicitedProgram(context.Params.InputResponses, out var hasElicitedResponse);
                if (program is null)
                {
                    if (hasElicitedResponse || !context.Server.IsMrtrSupported || context.Server.ClientCapabilities?.Elicitation?.Form is null)
                    {
                        return Error("start_debug_input_unavailable", "START_DEBUG_PROGRAM_REQUIRED");
                    }

                    throw new InputRequiredException(new Dictionary<string, InputRequest>
                    {
                        [InputRequestId] = InputRequest.ForElicitation(new ElicitRequestParams
                        {
                            Mode = "form",
                            Message = "Provide the program to debug.",
                            RequestedSchema = new ElicitRequestParams.RequestSchema
                            {
                                Properties = new Dictionary<string, ElicitRequestParams.PrimitiveSchemaDefinition>
                                {
                                    ["program"] = new ElicitRequestParams.StringSchema
                                    {
                                        Description = "Path to the program to debug.",
                                        MinLength = 1,
                                    },
                                },
                                Required = ["program"],
                            },
                        }),
                    });
                }
            }

            if (string.IsNullOrWhiteSpace(_debuggerPath))
            {
                return Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
            }

            NetCoreDbgSession? session = null;
            NativeSceneSessionBinding? binding = null;
            SessionSlot? registeredSlot = null;
            try
            {
                var token = CreateToken();
                binding = new NativeSceneSessionBinding(token, _bridgePath, _artifactRoot, _supportsNativeSceneCapture);
                session = await NetCoreDbgSession.StartAsync(
                    _debuggerPath,
                    program!,
                    InitializeTimeout,
                    RequestTimeout,
                    StopTimeout,
                    binding.ProbeLaunchEnvironment,
                    cancellationToken).ConfigureAwait(false);
                binding.AttachSession(session);
                SessionSlot? slot = null;
                slot = new SessionSlot(
                    StopTimeout,
                    session.StopAsync,
                    () => DisposeSlotResourcesAsync(session, binding),
                    () => RemoveSlot(token, session, binding, slot!));
                if (!_sessions.TryAdd(token, session)
                    || !_slots.TryAdd(token, slot)
                    || !_nativeSceneBindings.TryAdd(token, binding))
                {
                    _slots.TryRemove(new KeyValuePair<string, SessionSlot>(token, slot));
                    _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(token, session));
                    _nativeSceneBindings.TryRemove(new KeyValuePair<string, NativeSceneSessionBinding>(token, binding));
                    await binding.DisposeAsync().ConfigureAwait(false);
                    binding = null;
                    await _dispose(session).ConfigureAwait(false);
                    session = null;
                    return Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
                }

                registeredSlot = slot;
                session.ReaderFailed += failedSession => OnReaderFailed(token, failedSession);
                if (!_isUsable(session))
                {
                    OnReaderFailed(token, session);
                }

                return Success("start_debug_success", token, session.State);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                if (registeredSlot is not null)
                {
                    try
                    {
                        await registeredSlot.CloseAndDrainAsync().ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                    }
                }
                else
                {
                    if (binding is not null)
                    {
                        await binding.DisposeAsync().ConfigureAwait(false);
                    }

                    if (session is not null)
                    {
                        await _dispose(session).ConfigureAwait(false);
                    }
                }

                throw;
            }
            catch (Exception) when (!cancellationToken.IsCancellationRequested)
            {
                if (registeredSlot is not null)
                {
                    try
                    {
                        await registeredSlot.CloseAndDrainAsync().ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                    }
                }
                else
                {
                    if (binding is not null)
                    {
                        await binding.DisposeAsync().ConfigureAwait(false);
                    }

                    if (session is not null)
                    {
                        try
                        {
                            await _dispose(session).ConfigureAwait(false);
                        }
                        catch (Exception)
                        {
                        }
                    }
                }

                return Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
            }
        }

        private async ValueTask<CallToolResult> GetStateAsync(
            CallToolRequestParams request,
            CancellationToken cancellationToken)
        {
            if (!TryReadSessionId(request.Arguments, out var sessionId, out var hasSessionId))
            {
                return InvalidArguments(GetDebugState);
            }

            if (!hasSessionId)
            {
                return NotFound();
            }

            if (!_sessions.TryGetValue(sessionId!, out var session))
            {
                await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
                return NotFound();
            }

            var isUsable = _isUsable(session);
            if (!isUsable)
            {
                if (_slots.TryGetValue(sessionId!, out var slot))
                {
                    try
                    {
                        await slot.CloseAndDrainAsync().ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                    }
                }
                else if (_sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(sessionId!, session)))
                {
                    await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
                    await DisposeRemovedSessionAsync(session).ConfigureAwait(false);
                }

                return NotFound();
            }

            return Success("debug_state_success", sessionId!, session.State);
        }

        private async ValueTask<CallToolResult> GetThreadsAsync(
            CallToolRequestParams request,
            CancellationToken cancellationToken)
        {
            if (!TryReadSessionId(request.Arguments, out var sessionId, out var hasSessionId) || !hasSessionId)
            {
                return InvalidArguments(GetThreads);
            }

            if (!_sessions.TryGetValue(sessionId!, out var session)
                || !_slots.TryGetValue(sessionId!, out var slot))
            {
                await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
                return NotFound();
            }

            var lease = slot.TryAcquire();
            if (lease is null)
            {
                return NotFound();
            }

            var closeAfterLease = false;
            try
            {
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }

                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var threads = await session.GetThreadsAsync(operation.Token).ConfigureAwait(false);
                if (threads.IsRefused)
                {
                    return Error("dap_threads_refused", "DAP_THREADS_REFUSED");
                }

                var serialized = JsonSerializer.Serialize(new ThreadsSuccessContent("threads_success", threads.Threads));
                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedThreadsBytes)
                {
                    closeAfterLease = true;
                    return Error("dap_threads_protocol_error", "DAP_THREADS_PROTOCOL_ERROR");
                }

                return SerializedResult(serialized, isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return Error("dap_threads_protocol_error", "DAP_THREADS_PROTOCOL_ERROR");
            }
            finally
            {
                lease.Dispose();
                if (closeAfterLease)
                {
                    try
                    {
                        await slot.CloseAndDrainAsync().ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                    }
                }
            }
        }
        private async ValueTask<CallToolResult> GetCallStackAsync(
            CallToolRequestParams request,
            CancellationToken cancellationToken)
        {
            if (!TryReadCallStackArguments(request.Arguments, out var arguments))
            {
                return InvalidArguments(GetCallStack);
            }

            if (!_sessions.TryGetValue(arguments.SessionId, out var session)
                || !_slots.TryGetValue(arguments.SessionId, out var slot))
            {
                await RemoveNativeSceneBindingAsync(arguments.SessionId).ConfigureAwait(false);
                return NotFound();
            }

            var lease = slot.TryAcquire();
            if (lease is null)
            {
                return NotFound();
            }

            var closeAfterLease = false;
            try
            {
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }

                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var callStack = await session.GetCallStackAsync(
                    arguments.ThreadId,
                    arguments.StartFrame,
                    arguments.Levels,
                    operation.Token).ConfigureAwait(false);
                if (callStack.IsRefused)
                {
                    return Error("dap_stack_trace_refused", "DAP_STACK_TRACE_REFUSED");
                }

                var serialized = JsonSerializer.Serialize(new CallStackSuccessContent(
                    "call_stack_success",
                    callStack.Frames,
                    callStack.TotalFrames));
                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedCallStackBytes)
                {
                    closeAfterLease = true;
                    return Error("dap_stack_trace_protocol_error", "DAP_STACK_TRACE_PROTOCOL_ERROR");
                }

                return SerializedResult(serialized, isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return Error("dap_stack_trace_protocol_error", "DAP_STACK_TRACE_PROTOCOL_ERROR");
            }
            finally
            {
                lease.Dispose();
                if (closeAfterLease)
                {
                    try
                    {
                        await slot.CloseAndDrainAsync().ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                    }
                }
            }
        }
        private async ValueTask<CallToolResult> StopAsync(
            CallToolRequestParams request,
            CancellationToken cancellationToken)
        {
            if (!TryReadSessionId(request.Arguments, out var sessionId, out var hasSessionId))
            {
                return InvalidArguments(StopDebug);
            }

            if (!hasSessionId)
            {
                return NotFound();
            }

            if (!_sessions.TryRemove(sessionId!, out var session))
            {
                await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
                return NotFound();
            }

            _slots.TryRemove(sessionId!, out var slot);
            await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
            if (slot is not null)
            {
                try
                {
                    await slot.CloseAndDrainAsync().WaitAsync(cancellationToken).ConfigureAwait(false);
                    return StopSuccess(session.State);
                }
                catch (Exception) when (!cancellationToken.IsCancellationRequested)
                {
                    return NotFound();
                }
            }

            try
            {
                await session.StopAsync(cancellationToken).ConfigureAwait(false);
                return StopSuccess(session.State);
            }
            catch (Exception) when (!cancellationToken.IsCancellationRequested)
            {
                return NotFound();
            }
        }

        public ValueTask DisposeAsync() => DisposeAsync(CancellationToken.None);

        public async ValueTask DisposeAsync(CancellationToken cancellationToken)
        {
            var slots = _slots.Values.ToArray();
            try
            {
                await Task.WhenAll(slots.Select(slot => slot.CloseAndDrainAsync(cancellationToken))).ConfigureAwait(false);
            }
            finally
            {
                var sessions = _sessions.ToArray();
                _sessions.Clear();
                var bindings = _nativeSceneBindings.ToArray();
                _nativeSceneBindings.Clear();
                await Task.WhenAll(bindings.Select(static binding => binding.Value.DisposeAsync().AsTask())).ConfigureAwait(false);
                await Task.WhenAll(sessions.Select(session => DisposeRemovedSessionAsync(session.Value, cancellationToken))).ConfigureAwait(false);
            }
        }

        private void OnReaderFailed(string token, NetCoreDbgSession session)
        {
            if (_sessions.TryGetValue(token, out var registeredSession)
                && ReferenceEquals(session, registeredSession)
                && _slots.TryGetValue(token, out var slot))
            {
                _ = ObserveCloseAsync(slot);
            }
        }

        private static async Task ObserveCloseAsync(SessionSlot slot)
        {
            try
            {
                await slot.CloseAndDrainAsync().ConfigureAwait(false);
            }
            catch (Exception)
            {
            }
        }

        private void RemoveSlot(
            string token,
            NetCoreDbgSession session,
            NativeSceneSessionBinding binding,
            SessionSlot slot)
        {
            _slots.TryRemove(new KeyValuePair<string, SessionSlot>(token, slot));
            _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(token, session));
            _nativeSceneBindings.TryRemove(new KeyValuePair<string, NativeSceneSessionBinding>(token, binding));
        }

        private async ValueTask DisposeSlotResourcesAsync(
            NetCoreDbgSession session,
            NativeSceneSessionBinding binding)
        {
            try
            {
                await binding.DisposeAsync().ConfigureAwait(false);
            }
            finally
            {
                await _dispose(session).ConfigureAwait(false);
            }
        }

        private sealed class SessionSlot
        {
            private readonly object _gate = new();
            private readonly TimeSpan _drainTimeout;
            private readonly Func<CancellationToken, Task> _stop;
            private readonly Func<ValueTask> _dispose;
            private readonly Action _remove;
            private readonly CancellationTokenSource _abort = new();
            private CancellationTokenRegistration? _cancellationRegistration;
            private Task? _closeTask;
            private TaskCompletionSource<bool>? _drained;
            private int _leases;
            private bool _closed;
            private bool _abortDisposed;

            internal SessionSlot(
                TimeSpan drainTimeout,
                Func<CancellationToken, Task> stop,
                Func<ValueTask> dispose,
                Action remove)
            {
                _drainTimeout = drainTimeout;
                _stop = stop;
                _dispose = dispose;
                _remove = remove;
            }

            internal SessionLease? TryAcquire()
            {
                lock (_gate)
                {
                    if (_closed)
                    {
                        return null;
                    }

                    _leases++;
                    return new SessionLease(this, _abort.Token);
                }
            }

            internal Task CloseAndDrainAsync(CancellationToken cancellationToken = default)
            {
                lock (_gate)
                {
                    if (_closeTask is not null)
                    {
                        RegisterCancellation(cancellationToken);
                        return _closeTask;
                    }

                    _closed = true;
                    var drained = _leases == 0
                        ? Task.CompletedTask
                        : (_drained ??= new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously)).Task;
                    _remove();
                    RegisterCancellation(cancellationToken);
                    return _closeTask = CloseAndDrainCoreAsync(drained);
                }
            }

            private async Task CloseAndDrainCoreAsync(Task drained)
            {
                try
                {
                    var drainExpired = await Task.WhenAny(drained, Task.Delay(_drainTimeout)).ConfigureAwait(false) != drained;
                    if (drainExpired)
                    {
                        _abort.Cancel();
                    }

                    await drained.ConfigureAwait(false);
                    await _stop(_abort.IsCancellationRequested ? _abort.Token : CancellationToken.None).ConfigureAwait(false);
                }
                finally
                {
                    try
                    {
                        await _dispose().ConfigureAwait(false);
                    }
                    finally
                    {
                        lock (_gate)
                        {
                            _cancellationRegistration?.Dispose();
                            _abort.Dispose();
                            _abortDisposed = true;
                        }
                    }
                }
            }

            private void RegisterCancellation(CancellationToken cancellationToken)
            {
                if (!_abortDisposed && !_cancellationRegistration.HasValue && cancellationToken.CanBeCanceled)
                {
                    _cancellationRegistration = cancellationToken.Register(
                        static source => ((CancellationTokenSource)source!).Cancel(),
                        _abort);
                }
            }

            private void Release()
            {
                lock (_gate)
                {
                    _leases--;
                    if (_closed && _leases == 0)
                    {
                        _drained?.TrySetResult(true);
                    }
                }
            }

            internal sealed class SessionLease(SessionSlot slot, CancellationToken abortToken) : IDisposable
            {
                private SessionSlot? _slot = slot;

                internal CancellationToken AbortToken { get; } = abortToken;

                public void Dispose()
                {
                    Interlocked.Exchange(ref _slot, null)?.Release();
                }
            }
        }

        private static bool IsNativeSceneTool(string tool) => NativeSceneToolDispatcher.ListTools()
            .Any(candidate => StringComparer.Ordinal.Equals(candidate.Name, tool));

        private async ValueTask<NativeSceneSessionBinding?> ResolveNativeSceneBindingAsync(string sessionId)
        {
            if (!_sessions.TryGetValue(sessionId, out var session))
            {
                await RemoveNativeSceneBindingAsync(sessionId).ConfigureAwait(false);
                return null;
            }

            if (!_isUsable(session))
            {
                if (_slots.TryGetValue(sessionId, out var slot))
                {
                    try
                    {
                        await slot.CloseAndDrainAsync().ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                    }
                }
                else if (_sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(sessionId, session)))
                {
                    await RemoveNativeSceneBindingAsync(sessionId).ConfigureAwait(false);
                    await DisposeRemovedSessionAsync(session).ConfigureAwait(false);
                }

                return null;
            }

            return _nativeSceneBindings.TryGetValue(sessionId, out var binding) ? binding : null;
        }

        private async ValueTask RemoveNativeSceneBindingAsync(string sessionId)
        {
            if (_nativeSceneBindings.TryRemove(sessionId, out var binding))
            {
                await binding.DisposeAsync().ConfigureAwait(false);
            }
        }

        private async Task DisposeRemovedSessionAsync(
            NetCoreDbgSession session,
            CancellationToken cancellationToken = default)
        {
            try
            {
                try
                {
                    await session.StopAsync(cancellationToken).ConfigureAwait(false);
                }
                catch (Exception) when (!cancellationToken.IsCancellationRequested)
                {
                }
            }
            finally
            {
                try
                {
                    await _dispose(session).ConfigureAwait(false);
                }
                catch (Exception)
                {
                }
            }
        }

        private static bool TryReadProgram(
            IDictionary<string, JsonElement>? arguments,
            out string? program,
            out bool hasProgram)
        {
            program = null;
            hasProgram = false;
            if (arguments is null)
            {
                return true;
            }

            if (arguments.Count > 1 || arguments.Keys.Any(static name => name != "program"))
            {
                return false;
            }

            if (!arguments.TryGetValue("program", out var element))
            {
                return true;
            }

            hasProgram = true;
            if (element.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(program = element.GetString()))
            {
                return false;
            }

            return true;
        }

        private static bool TryReadSessionId(
            IDictionary<string, JsonElement>? arguments,
            out string? sessionId,
            out bool hasSessionId)
        {
            sessionId = null;
            hasSessionId = false;
            if (arguments is null)
            {
                return true;
            }

            if (arguments.Count > 1 || arguments.Keys.Any(static name => name != "debugSessionId"))
            {
                return false;
            }

            if (!arguments.TryGetValue("debugSessionId", out var element))
            {
                return true;
            }

            hasSessionId = true;
            if (element.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(sessionId = element.GetString()))
            {
                return false;
            }

            return true;
        }
        private static bool TryReadCallStackArguments(
            IDictionary<string, JsonElement>? arguments,
            out CallStackArguments result)
        {
            result = null!;
            if (arguments is null
                || arguments.Count is < 2 or > 4
                || arguments.Keys.Any(static name => name is not "debugSessionId" and not "threadId" and not "startFrame" and not "levels")
                || !arguments.TryGetValue("debugSessionId", out var sessionIdElement)
                || sessionIdElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionIdElement.GetString())
                || !arguments.TryGetValue("threadId", out var threadIdElement)
                || threadIdElement.ValueKind != JsonValueKind.Number
                || !threadIdElement.TryGetInt32(out var threadId))
            {
                return false;
            }

            var startFrame = 0U;
            if (arguments.TryGetValue("startFrame", out var startFrameElement)
                && (startFrameElement.ValueKind != JsonValueKind.Number
                    || !startFrameElement.TryGetUInt32(out startFrame)))
            {
                return false;
            }

            var levels = 20U;
            if (arguments.TryGetValue("levels", out var levelsElement)
                && (levelsElement.ValueKind != JsonValueKind.Number
                    || !levelsElement.TryGetUInt32(out levels)
                    || levels is 0 or > 256))
            {
                return false;
            }

            result = new CallStackArguments(sessionIdElement.GetString()!, threadId, startFrame, levels);
            return true;
        }

        private static string? ReadElicitedProgram(IDictionary<string, InputResponse>? responses, out bool hasResponse)
        {
            if (responses is null || !responses.TryGetValue(InputRequestId, out var response))
            {
                hasResponse = false;
                return null;
            }

            hasResponse = true;

            var result = response.Deserialize(InputResponse.ElicitResultJsonTypeInfo);
            return result is { IsAccepted: true, Content: { } content }
                && content.TryGetValue("program", out var program)
                && program.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(program.GetString())
                    ? program.GetString()
                    : null;
        }

        private static string CreateToken()
        {
            Span<byte> bytes = stackalloc byte[32];
            RandomNumberGenerator.Fill(bytes);
            var base64 = Convert.ToBase64String(bytes);
            CryptographicOperations.ZeroMemory(bytes);
            return base64.TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }

        private static CallToolResult Success(string kind, string sessionId, DapSessionState state) => Result(new
        {
            kind,
            debugSessionId = sessionId,
            state = LifecycleState(state),
        }, isError: false);

        private static CallToolResult StopSuccess(DapSessionState state) => Result(new
        {
            kind = "stop_debug_success",
            state = LifecycleState(state),
        }, isError: false);

        private static object LifecycleState(DapSessionState state) => new
        {
            @event = state.Event,
            stopReason = state.StopReason,
            exitCode = state.ExitCode,
        };

        private static CallToolResult NotFound() => Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");

        private static CallToolResult InvalidArguments(string tool) => Result(new
        {
            kind = "invalid_tool_arguments",
            error = "INVALID_TOOL_ARGUMENTS",
            tool,
        }, isError: true);

        private static CallToolResult Error(string kind, string error) => Result(new { kind, error }, isError: true);
        private static CallToolResult UnknownTool(string tool) => new()
        {
            ResultType = "complete",
            IsError = true,
            Content = [new TextContentBlock { Text = $"Unknown tool: {tool}" }],
        };


        private static CallToolResult Result<T>(T content, bool isError) => new()
        {
            ResultType = "complete",
            IsError = isError,
            Content = [new TextContentBlock { Text = JsonSerializer.Serialize(content) }],
            StructuredContent = JsonSerializer.SerializeToElement(content),
        };

        private static CallToolResult SerializedResult(string serializedContent, bool isError)
        {
            using var document = JsonDocument.Parse(serializedContent);
            return new CallToolResult
            {
                ResultType = "complete",
                IsError = isError,
                Content = [new TextContentBlock { Text = serializedContent }],
                StructuredContent = document.RootElement.Clone(),
            };
        }

        private sealed record ThreadsSuccessContent(string kind, IReadOnlyList<DapThread> threads);
        private sealed record CallStackArguments(string SessionId, int ThreadId, uint StartFrame, uint Levels);

        private sealed record CallStackSuccessContent(
            string kind,
            IReadOnlyList<DapStackFrame> frames,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] uint? totalFrames);
    }

    private static class ToolCatalog
    {
        internal static ListToolsResult List() => new()
        {
            Tools =
            [
                Tool("start_debug", "Start debugging a program.", "{\"type\":\"object\",\"properties\":{\"program\":{\"type\":\"string\",\"minLength\":1}},\"additionalProperties\":false}"),
                Tool("get_debug_state", "Get the state of a debug session.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":32}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("stop_debug", "Stop a debug session.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":32}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("get_threads", "Get threads in a debug session.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":1}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("get_call_stack", "Get a bounded stack-frame page for one stopped thread.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":1},\"threadId\":{\"type\":\"integer\",\"minimum\":-2147483648,\"maximum\":2147483647},\"startFrame\":{\"type\":\"integer\",\"minimum\":0,\"maximum\":4294967295},\"levels\":{\"type\":\"integer\",\"minimum\":1,\"maximum\":256}},\"required\":[\"debugSessionId\",\"threadId\"],\"additionalProperties\":false}"),
                .. NativeSceneToolDispatcher.ListTools(),
            ],
            TimeToLive = CacheLifetime,
            CacheScope = CacheScope.Public,
        };

        private static Tool Tool(string name, string description, string schema) => new()
        {
            Name = name,
            Description = description,
            InputSchema = JsonDocument.Parse(schema).RootElement.Clone(),
        };
    }

    private sealed class SessionDisposer(DebugSessionRegistry sessions) : IHostedService
    {
        public Task StartAsync(CancellationToken cancellationToken) => Task.CompletedTask;

        public Task StopAsync(CancellationToken cancellationToken) => sessions.DisposeAsync(cancellationToken).AsTask();
    }
}
