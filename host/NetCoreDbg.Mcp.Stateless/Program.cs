using System.Diagnostics;
using System.Collections.Concurrent;
using System.Runtime.CompilerServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using NetCoreDbg.Mcp.Host;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.NativeScene;
using NetCoreDbg.Mcp.Stateless.NativeUi;

namespace NetCoreDbg.Mcp.Stateless;

internal static class Program
{
    private const string UnixProcessGroupProxy = "--unix-process-group-proxy";
    private static readonly TimeSpan CacheLifetime = TimeSpan.FromMinutes(5);

    private static async Task Main(string[] arguments)
    {
        if (!OperatingSystem.IsWindows()
            && arguments.Length == 3
            && string.Equals(arguments[0], "--unix-build-group-anchor", StringComparison.Ordinal))
        {
            await DapBuildUnixRunner.RunAnchorAsync(arguments).ConfigureAwait(false);
            return;
        }

        if (!OperatingSystem.IsWindows()
            && arguments.Length >= 5
            && string.Equals(arguments[0], "--unix-build-command-proxy", StringComparison.Ordinal))
        {
            await DapBuildUnixRunner.RunGuardianAsync(arguments).ConfigureAwait(false);
            return;
        }

        if (!OperatingSystem.IsWindows()
            && arguments.Length == 3
            && string.Equals(arguments[0], UnixProcessGroupProxy, StringComparison.Ordinal)
            && !string.IsNullOrWhiteSpace(arguments[1])
            && !string.IsNullOrWhiteSpace(arguments[2]))
        {
            await RunUnixProcessGroupProxyAsync(arguments[1], arguments[2]).ConfigureAwait(false);
            return;
        }

        try
        {
            // Validate the existing limits before serving requests, without adding a dependency to the Unix proxy.
            RuntimeHelpers.RunClassConstructor(typeof(DapOutputBuffer).TypeHandle);
        }
        catch (TypeInitializationException exception) when (exception.InnerException is FormatException)
        {
            await Console.Error.WriteLineAsync(exception.InnerException.Message).ConfigureAwait(false);
            Environment.ExitCode = 1;
            return;
        }

        var sessions = new DebugSessionRegistry(
            Environment.GetEnvironmentVariable("NETCOREDBG_PATH"),
            Environment.GetEnvironmentVariable("FLAUI_BRIDGE_PATH"),
            Environment.GetEnvironmentVariable("NETCOREDBG_MCP_ARTIFACT_ROOT"))
        {
            ProjectRootResolver = ProjectRootResolver.FromHostArguments(arguments, Environment.CurrentDirectory),
            LegacyCompat = arguments.Contains("--legacy-compat", StringComparer.Ordinal),
        };
        var host = BuildHost(sessions);
        await host.RunAsync().ConfigureAwait(false);
    }

    private static IHost BuildHost(DebugSessionRegistry sessions)
    {
        var builder = global::Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Logging.AddConsole(options => options.LogToStandardErrorThreshold = LogLevel.Trace);
        builder.Services.AddSingleton(sessions);
        builder.Services.AddHostedService(_ => new SessionDisposer(sessions));
        var projectSearch = new NativeCodeSearch(sessions.ProjectRootResolver, sessions.ShutdownToken, useModernResultEnvelope: true);

        var mcp = builder.Services.AddMcpServer(options =>
            {
                // Negotiate initialize-era clients and per-request modern clients independently.
                options.ProtocolVersion = null;
                options.ServerInfo = new Implementation { Name = "netcoredbg-mcp-stateless", Version = "1.0.0" };
                options.Capabilities = new ServerCapabilities
                {
                    Tools = new ToolsCapability { ListChanged = false },
                    Resources = sessions.LegacyCompat ? new ResourcesCapability { Subscribe = true, ListChanged = false } : null,
                    Experimental = sessions.LegacyCompat
                        ? new Dictionary<string, object> { ["x-mux"] = JsonSerializer.SerializeToElement(new { sharing = "isolated" }) }
                        : null,
                };
            })
            .WithStdioServerTransport()
            .WithListToolsHandler((context, cancellationToken) =>
                ValueTask.FromResult(sessions.LegacyCompat ? ToolCatalog.LegacyList() : ToolCatalog.List()))
            .WithCallToolHandler(async (context, cancellationToken) =>
            {
                if (sessions.LegacyCompat)
                    return NativeCodeSearch.IsNativeTool(context.Params.Name)
                        ? await sessions.CallLegacySearchAsync(projectSearch, context, cancellationToken).ConfigureAwait(false)
                        : await sessions.CallLegacyAsync(context, cancellationToken).ConfigureAwait(false);
                if (!NativeCodeSearch.IsNativeTool(context.Params.Name))
                    return await sessions.CallAsync(context, cancellationToken).ConfigureAwait(false);

                var result = await projectSearch.CallAsync(context, cancellationToken).ConfigureAwait(false);
                result.ResultType = "complete";
                return result;
            })
            .WithMessageFilters(filters =>
            {
                filters.AddIncomingFilter(next => async (context, cancellationToken) =>
                {
                    if (context.JsonRpcMessage is JsonRpcNotification
                        {
                            Method: NotificationMethods.CancelledNotification,
                            Params: JsonObject parameters,
                        }
                        && TryReadCancelledRequestId(parameters["requestId"], out var requestId))
                    {
                        sessions.CancelStepOver(requestId);
                        sessions.CancelUiInvoke(requestId);
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest { Method: "tools/call", Params: JsonObject call }
                        && call["name"] is JsonValue toolName && toolName.TryGetValue<string>(out var name)
                        && name is "pause_execution" or "get_modules" or "get_progress" or "get_loaded_sources" or "configure_exceptions" or "set_variable" or "search_output" or "get_stop_context" or "get_exception_context"
                        && call.TryGetPropertyValue("arguments", out var rawArguments)
                        && rawArguments is not null
                        && (rawArguments is not JsonObject objectArguments
                            || ((name is "configure_exceptions" or "set_variable") && !CanDeserializeMutationArguments(name, objectArguments, sessions.LegacyCompat))))
                    {
                        // A null legacy argument object means defaults; retain an invalid marker instead of executing them.
                        call["arguments"] = sessions.LegacyCompat
                            ? new JsonObject { ["__invalid_legacy_arguments"] = true }
                            : null;
                    }

                    await next(context, cancellationToken).ConfigureAwait(false);
                });
                filters.AddOutgoingFilter(next => async (context, cancellationToken) =>
                {
                    if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                        && result.ContainsKey("supportedVersions")
                        && result.ContainsKey("capabilities"))
                    {
                        result["ttlMs"] = (long)CacheLifetime.TotalMilliseconds;
                        result["cacheScope"] = "public";
                    }

                    await next(context, cancellationToken).ConfigureAwait(false);
                });
            });

        if (sessions.LegacyCompat)
        {
            mcp.WithListResourcesHandler((_, _) => ValueTask.FromResult(new ListResourcesResult
            {
                Resources = [
                    new Resource { Name = "debug_output_resource", Uri = "debug://output", MimeType = "text/plain" },
                    new Resource { Name = "debug_threads_resource", Uri = "debug://threads", MimeType = "application/json" },
                ],
            }))
            .WithReadResourceHandler((context, cancellationToken) => sessions.ReadLegacyResourceAsync(context.Params.Uri, cancellationToken))
            .WithSubscribeToResourcesHandler((context, cancellationToken) => sessions.SubscribeLegacyResourceAsync(context.Server, context.Params.Uri, true, cancellationToken))
            .WithUnsubscribeFromResourcesHandler((context, cancellationToken) => sessions.SubscribeLegacyResourceAsync(context.Server, context.Params.Uri, false, cancellationToken));
        }

        return builder.Build();
    }

    private static bool CanDeserializeMutationArguments(string tool, JsonObject arguments, bool legacyCompat)
    {
        try
        {
            foreach (var (name, value) in arguments)
            {
                if (name == "debugSessionId" || (tool == "set_variable" && name is ("inspectionContextId" or "name" or "value")))
                {
                    if (value is not JsonValue text || !text.TryGetValue<string>(out _)) return false;
                }
                else if (tool == "set_variable" && name == "variablesReference")
                {
                    if (value is not JsonValue reference || !reference.TryGetValue<int>(out _)) return false;
                }
                else if (legacyCompat && tool == "set_variable" && name == "variables_reference")
                {
                    if (value is not JsonValue reference || reference.GetValueKind() != JsonValueKind.Number) return false;
                }
                else if (tool == "configure_exceptions" && name == "filters")
                {
                    if (value is null) continue;
                    if (value is not JsonArray filters) return false;
                    foreach (var filter in filters)
                    {
                        if (filter is not JsonValue text || !text.TryGetValue<string>(out _)) return false;
                    }
                }
                else
                {
                    return false;
                }
            }
            return true;
        }
        catch (Exception exception) when (exception is ArgumentException or InvalidOperationException)
        {
            return false;
        }
    }

    private static bool TryReadCancelledRequestId(JsonNode? value, out RequestId requestId)
    {
        if (value is JsonValue stringValue && stringValue.TryGetValue<string>(out var stringId))
        {
            requestId = new RequestId(stringId);
            return true;
        }

        if (value is JsonValue numericValue && numericValue.TryGetValue<long>(out var numericId))
        {
            requestId = new RequestId(numericId);
            return true;
        }

        requestId = default;
        return false;
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
        private const string GetScopes = "get_scopes";
        private const string GetStepInTargets = "get_step_in_targets";
        private const string GetVariables = "get_variables";
        private const string EvaluateExpression = "evaluate_expression";
        private const string SetVariable = "set_variable";
        private const string GetExceptionInfo = "get_exception_info";
        private const string GetStopContext = "get_stop_context";
        private const string GetExceptionContext = "get_exception_context";
        private const string GetModules = "get_modules";
        private const string GetProgress = "get_progress";
        private const string GetLoadedSources = "get_loaded_sources";
        private const string GetOutput = "get_output";
        private const string GetOutputTail = "get_output_tail";
        private const string SearchOutput = "search_output";
        private const string StepOver = "step_over";
        private const string StepInto = "step_into";
        private const string StepOut = "step_out";
        private const string ContinueExecution = "continue_execution";
        private const string PauseExecution = "pause_execution";
        private const string AddBreakpoint = "add_breakpoint";
        private const string RemoveBreakpoint = "remove_breakpoint";
        private const string ConfigureExceptions = "configure_exceptions";
        private const string UiInvoke = "ui_invoke";
        private const string UiInvokeObserved = "ui_invoke_observed";
        private const string UiGetWindowTree = "ui_get_window_tree";
        private const string InputRequestId = "start_debug_program";
        private static readonly TimeSpan InitializeTimeout = TimeSpan.FromSeconds(10);
        private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(30);
        private static readonly TimeSpan StopTimeout = TimeSpan.FromSeconds(1);
        private const int MaximumSerializedThreadsBytes = 256 * 1024;
        private const int MaximumSerializedCallStackBytes = 256 * 1024;
        private const int MaximumSerializedInspectionBytes = 256 * 1024;
        private const int MaximumBreakpointCount = 256;

        private readonly ConcurrentDictionary<string, NetCoreDbgSession> _sessions = new(StringComparer.Ordinal);
        private readonly ConcurrentDictionary<string, SessionSlot> _slots = new(StringComparer.Ordinal);
        private readonly object _lifetimeGate = new();
        private readonly Dictionary<string, OutputLifetime> _outputs = new(StringComparer.Ordinal);
        private readonly Dictionary<string, (SessionSlot Slot, OutputLifetime Output)> _ownedSlots = new(StringComparer.Ordinal);
        private int _starts;
        private TaskCompletionSource<bool>? _startsDrained;
        private Exception? _startCleanupFailure;
        private readonly CancellationTokenSource _startShutdown = new();
        private List<(CancellationToken Token, CancellationTokenRegistration Registration)>? _startShutdownRegistrations;
        private bool _startShutdownDisposed;
        private Task? _shutdownTask;
        private bool _shutdownRequested;
        private bool _buildActive;
        private bool _buildCleanupUnconfirmed;
        private readonly ConcurrentDictionary<RequestId, CancellationTokenSource> _stepOverRequests = new();
        private readonly ConcurrentDictionary<RequestId, CancellationTokenSource> _uiInvokeRequests = new();
        private readonly ConcurrentDictionary<string, NativeSceneSessionBinding> _nativeSceneBindings = new(StringComparer.Ordinal);
        private readonly string? _debuggerPath;
        private readonly string? _bridgePath;
        private readonly string? _artifactRoot;
        private readonly Func<NetCoreDbgSession, bool> _isUsable;
        private readonly Func<NetCoreDbgSession, ValueTask> _dispose;
        private readonly Action? _failedStartCleanupCompleted;
        internal bool LegacyCompat { get; init; }
        private readonly SemaphoreSlim _legacyGate = new(1, 1);
        private readonly SemaphoreSlim _legacyLifecycle = new(1, 1);
        private readonly SemaphoreSlim _legacyBreakpointGate = new(1, 1);
        private readonly List<(string File, int Line)> _legacyBreakpoints = [];
        private readonly StringComparer _legacyBreakpointPathComparer = OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;
        private string? _legacyBreakpointSessionId;
        private long _legacySessionGeneration;
        private bool _legacyStarting;
        private bool _legacyStopping;
        private bool _legacyHasOwner;
        private string? _legacySessionId;
        private string? _legacyOwner;
        private long _legacyOwnerActivity;
        private string? _legacyInspectionSessionId;
        private string? _legacyInspectionContextId;
        private string? _legacyInspectionMuxId;
        private int? _legacyCurrentFrameId;
        private long _legacyInspectionRevision;
        private readonly LegacyResourceOwner _legacyResources = new();

        private sealed class LegacyResourceOwner
        {
            private readonly object _gate = new();
            private readonly SemaphoreSlim _sendGate = new(1, 1);
            private readonly HashSet<string> _subscriptions = new(StringComparer.Ordinal);
            private McpServer? _server;
            private Generation? _current;
            private int _carry;
            private bool _dispatching;
            private bool _closed;

            internal sealed class Generation(LegacyResourceOwner owner)
            {
                internal int Pending;
                internal bool Published;
                internal int InFlight;
                internal string? SessionId;
                internal void Observe(string eventName) => owner.Signal(this, eventName == "output" ? 1 : 2);
            }

            internal Generation Begin()
            {
                lock (_gate)
                {
                    if (_current is { } previous) _carry |= previous.Pending | previous.InFlight;
                    var generation = new Generation(this);
                    _current = generation;
                    return generation;
                }
            }

            internal void Discard(Generation generation)
            {
                lock (_gate)
                {
                    if (_closed || !ReferenceEquals(_current, generation)) return;
                    var idle = new Generation(this) { Published = true, Pending = _carry };
                    _carry = 0;
                    _current = idle;
                    Schedule();
                }
            }

            internal void Publish(Generation generation, string? sessionId = null)
            {
                lock (_gate)
                {
                    if (!ReferenceEquals(_current, generation) || _closed) return;
                    generation.SessionId = sessionId;
                    generation.Pending |= _carry;
                    _carry = 0;
                    generation.Published = true;
                    if (_subscriptions.Contains("debug://threads")) generation.Pending |= 2;
                    Schedule();
                }
            }

            internal void SignalSession(string sessionId, string name)
            {
                lock (_gate)
                {
                    if (_current is { Published: true } current && StringComparer.Ordinal.Equals(current.SessionId, sessionId))
                        SignalLocked(current, name == "output" ? 1 : 2);
                }
            }

            private void Signal(Generation generation, int bits)
            {
                lock (_gate) SignalLocked(generation, bits);
            }

            private void SignalLocked(Generation generation, int bits)
            {
                if (_closed || !ReferenceEquals(_current, generation)) return;
                var uri = bits == 1 ? "debug://output" : "debug://threads";
                if (!_subscriptions.Contains(uri)) return;
                generation.Pending |= bits;
                if (generation.Published) Schedule();
            }

            private void Schedule()
            {
                if (_dispatching) return;
                _dispatching = true;
                _ = Task.Run(DispatchAsync);
            }

            private async Task DispatchAsync()
            {
                while (true)
                {
                    await _sendGate.WaitAsync().ConfigureAwait(false);
                    try
                    {
                        McpServer? server;
                        string? uri;
                        Generation generation;
                        int bit;
                        lock (_gate)
                        {
                            if (_closed || _current is not { Published: true } current || current.Pending == 0)
                            {
                                _dispatching = false;
                                return;
                            }
                            generation = current;
                            bit = (generation.Pending & 1) != 0 ? 1 : 2;
                            generation.Pending &= ~bit;
                            uri = bit == 1 ? "debug://output" : "debug://threads";
                            server = _subscriptions.Contains(uri) ? _server : null;
                            generation.InFlight = server is null ? 0 : bit;
                        }
                        if (server is not null)
                        {
                            try
                            {
                                await server.SendNotificationAsync(NotificationMethods.ResourceUpdatedNotification, new ResourceUpdatedNotificationParams { Uri = uri }, cancellationToken: CancellationToken.None).ConfigureAwait(false);
                            }
                            catch (Exception) { }
                            finally { lock (_gate) generation.InFlight = 0; }
                        }
                    }
                    finally { _sendGate.Release(); }
                }
            }

            internal async ValueTask<EmptyResult> SubscribeAsync(McpServer server, string uri, bool subscribe, CancellationToken cancellationToken)
            {
                if (uri is not ("debug://output" or "debug://threads"))
                    throw new McpProtocolException($"Unknown resource: {uri}", McpErrorCode.InvalidParams);
                if (!await _sendGate.WaitAsync(TimeSpan.FromSeconds(2), cancellationToken).ConfigureAwait(false))
                    throw new McpProtocolException("Resource notification delivery is busy.", McpErrorCode.InvalidRequest);
                try
                {
                    lock (_gate)
                    {
                        if (_closed)
                            throw new McpProtocolException("Resource connection unavailable.", McpErrorCode.InvalidRequest);
                        _server = server;
                        if (subscribe) _subscriptions.Add(uri);
                        else
                        {
                            _subscriptions.Remove(uri);
                            _carry &= uri == "debug://output" ? ~1 : ~2;
                            if (_current is { } current) current.Pending &= uri == "debug://output" ? ~1 : ~2;
                        }
                    }
                    return new EmptyResult();
                }
                finally { _sendGate.Release(); }
            }

            internal Generation? Retire()
            {
                Generation? previous;
                lock (_gate)
                {
                    previous = _current;
                    if (previous is not null) _carry |= previous.Pending | previous.InFlight;
                    _current = null;
                }
                return previous;
            }

            internal void Restore(Generation? generation)
            {
                if (generation is null) return;
                lock (_gate)
                {
                    if (_closed || _current is not null) return;
                    generation.Pending |= _carry;
                    _carry = 0;
                    _current = generation;
                    SignalLocked(generation, 1);
                    if (generation.Published && generation.Pending != 0) Schedule();
                    SignalLocked(generation, 2);
                }
            }

            internal Task CloseAsync()
            {
                lock (_gate) { _closed = true; _current = null; _carry = 0; _subscriptions.Clear(); _server = null; }
                return Task.CompletedTask;
            }
        }

        internal ValueTask<EmptyResult> SubscribeLegacyResourceAsync(McpServer server, string uri, bool subscribe, CancellationToken cancellationToken) =>
            _legacyResources.SubscribeAsync(server, uri, subscribe, cancellationToken);

        internal Task CloseLegacyResourcesAsync() => _legacyResources.CloseAsync();

        internal async ValueTask<ReadResourceResult> ReadLegacyResourceAsync(string uri, CancellationToken cancellationToken)
        {
            if (uri is not ("debug://output" or "debug://threads"))
                throw new McpProtocolException($"Unknown resource: {uri}", McpErrorCode.InvalidParams);
            await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            string? sessionId;
            try
            {
                sessionId = _legacySessionId;
                if (sessionId is null)
                {
                    if (uri == "debug://threads") throw new McpProtocolException("No active debug session.", McpErrorCode.InvalidRequest);
                    return new ReadResourceResult { Contents = [new TextResourceContents { Uri = uri, MimeType = "text/plain", Text = string.Empty }] };
                }
            }
            finally { _legacyGate.Release(); }
            var request = LegacyRequest(uri == "debug://output" ? GetOutput : GetThreads, sessionId, null);
            var result = uri == "debug://output"
                ? await GetOutputAsync(request, cancellationToken).ConfigureAwait(false)
                : await GetThreadsAsync(request, cancellationToken).ConfigureAwait(false);
            await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                if (_legacySessionId != sessionId || _legacyStopping)
                    throw new McpProtocolException("Debug session changed before resource read completed.", McpErrorCode.InvalidRequest);
                if (result.IsError == true || result.StructuredContent is not { } content)
                    throw new McpProtocolException("Resource read failed.", McpErrorCode.InvalidRequest);
                var text = uri == "debug://output" ? content.GetProperty("output").GetString()! : content.GetProperty("threads").GetRawText();
                return new ReadResourceResult { Contents = [new TextResourceContents { Uri = uri, MimeType = uri == "debug://output" ? "text/plain" : "application/json", Text = text }] };
            }
            finally { _legacyGate.Release(); }
        }

        internal async ValueTask<CallToolResult> CallLegacySearchAsync(
            NativeCodeSearch search, RequestContext<CallToolRequestParams> context, CancellationToken cancellationToken)
        {
            var result = await search.CallAsync(context, cancellationToken).ConfigureAwait(false);
            await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                var state = LegacySnapshot().State;
                string[] actions = ["find_code_symbol", "find_code_references", "get_source_context", "search_source"];
                var content = result.StructuredContent!.Value;
                if (result.IsError == true)
                {
                    var error = content.GetProperty("error").GetString()!;
                    return Result(new { error, state, next_actions = actions, message = $"Error: {error}. Try one of the suggested next_actions." }, false);
                }
                var message = state switch
                {
                    "idle" => "No active debug session.",
                    "initializing" => "Debug session is initializing.",
                    "running" => "Program is RUNNING. Variable references from previous stops are INVALID. Do NOT call get_variables — wait for the program to stop.",
                    "stopped" => "Program is PAUSED. UI is frozen, app cannot respond to input. Inspect state with get_call_stack/get_variables, then resume.",
                    "terminated" => "Program has terminated. Check output for errors, then stop the session.",
                    _ => string.Empty,
                };
                return Result(new { state, next_actions = actions, message, data = content.GetProperty("data") }, false);
            }
            finally { _legacyGate.Release(); }
        }

        internal async ValueTask<CallToolResult> CallLegacyAsync(RequestContext<CallToolRequestParams> context, CancellationToken cancellationToken)
        {
            var name = context.Params.Name;
            if (name is not (StartDebug or GetDebugState or GetThreads or GetCallStack or StepOver or StepInto or StepOut
                or PauseExecution or ContinueExecution or StopDebug or GetOutput or GetOutputTail
                or AddBreakpoint or RemoveBreakpoint or GetScopes or GetVariables or EvaluateExpression
                or SetVariable or ConfigureExceptions or GetExceptionInfo or GetModules or GetProgress or GetLoadedSources or GetStepInTargets))
                return UnknownTool(name);
            var arguments = context.Params.Arguments is null
                ? new Dictionary<string, JsonElement>(StringComparer.Ordinal)
                : new Dictionary<string, JsonElement>(context.Params.Arguments, StringComparer.Ordinal);
            string? breakpointFile = null;
            var breakpointSourceValid = false;
            long breakpointGeneration = 0;
            if (name == StartDebug)
            {
                await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                try
                {
                    if (_legacyBreakpointSessionId is not null)
                        return LegacyError("A breakpoint update is still settling.");
                }
                finally { _legacyGate.Release(); }
            }
            if (name is AddBreakpoint or RemoveBreakpoint)
            {
                await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                try
                {
                    if (_legacyStarting) return LegacyError("A debug session is starting.");
                    if (_legacyStopping) return LegacyError("A debug session is stopping.");
                    if (_legacySessionId is null && _legacyBreakpointSessionId is not null)
                        return LegacyError("A breakpoint update is still settling.");
                    breakpointGeneration = _legacySessionGeneration;
                }
                finally { _legacyGate.Release(); }
                if (arguments.TryGetValue("file", out var source) && source.ValueKind == JsonValueKind.String
                    && !string.IsNullOrWhiteSpace(source.GetString()))
                    breakpointSourceValid = TryNormalizeLegacyBreakpointSource(source.GetString()!, out breakpointFile, requireExistingFile: name == AddBreakpoint);
                cancellationToken.ThrowIfCancellationRequested();
            }
            var lifecycleHeld = false;
            var gateHeld = false;
            var breakpointHeld = false;
            try
            {
                if (name is StartDebug or StopDebug)
                {
                    await _legacyLifecycle.WaitAsync(cancellationToken).ConfigureAwait(false);
                    lifecycleHeld = true;
                }
                await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                gateHeld = true;
                if (HasInvalidLegacyArguments(name, arguments))
                    return LegacyError("Unsupported or invalid argument.");
                if (name is (AddBreakpoint or RemoveBreakpoint) && breakpointGeneration != _legacySessionGeneration)
                    return LegacyError("Debug session changed before breakpoint admission.", "unknown");
                if (_legacyBreakpointSessionId is not null
                    && (name == StartDebug || (name is (AddBreakpoint or RemoveBreakpoint) && _legacySessionId is null)))
                    return LegacyError("A breakpoint update is still settling.");
                var consumesOutput = name == GetOutput && arguments.TryGetValue("clear", out var clearOption)
                    && clearOption.ValueKind == JsonValueKind.True
                    && (!arguments.TryGetValue("category", out var categoryOption) || categoryOption.ValueKind == JsonValueKind.Null
                        || (categoryOption.ValueKind == JsonValueKind.String && categoryOption.GetString() == string.Empty));
                var mutation = name is StartDebug or StopDebug or StepOver or StepInto or StepOut or PauseExecution
                    or ContinueExecution or AddBreakpoint or RemoveBreakpoint or EvaluateExpression or SetVariable or ConfigureExceptions or GetStepInTargets || consumesOutput;
                var raw = context.JsonRpcRequest.Params as JsonObject;
                var mux = raw?["_meta"]?["muxSessionId"];
                if (mux is not null && (mux is not JsonValue muxValue || !muxValue.TryGetValue<string>(out _)))
                    return LegacyError("Invalid muxSessionId.");
                var muxId = mux?.GetValue<string>();
                if (name == StartDebug && _legacySessionId is not null)
                    return LegacyError("A debug session is already active; stop it before starting another.");
                if (mutation && _legacyStarting)
                    return LegacyError("A debug session is starting.");
                if (mutation && _legacyStopping)
                    return LegacyError("A debug session is stopping.");
                if (mutation && name != StartDebug && _legacySessionId is not null)
                {
                    var timeout = 60.0;
                    var configured = Environment.GetEnvironmentVariable("NETCOREDBG_SESSION_TIMEOUT");
                    if (configured is not null && (!double.TryParse(configured, System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out timeout) || !double.IsFinite(timeout) || timeout < 0))
                        return LegacyError("Invalid NETCOREDBG_SESSION_TIMEOUT.");
                    if (_legacyHasOwner && _legacyOwner is not null && _legacyBreakpointSessionId != _legacySessionId
                        && Stopwatch.GetElapsedTime(_legacyOwnerActivity).TotalSeconds > timeout)
                    {
                        _legacyHasOwner = false;
                        _legacyOwner = null;
                    }
                    if (_legacyHasOwner && _legacyOwner != muxId)
                        return LegacyError("Debug session is owned by another agent.");
                    _legacyHasOwner = true;
                    _legacyOwner = muxId;
                    if (muxId is not null) _legacyOwnerActivity = Stopwatch.GetTimestamp();
                }
                var breakpointLine = 0;
                int[]? breakpointLines = null;
                var breakpointRemoved = false;
                if (name is AddBreakpoint or RemoveBreakpoint)
                {
                    if ((arguments.TryGetValue("condition", out var condition) && condition.ValueKind != JsonValueKind.Null)
                        || (arguments.TryGetValue("hit_condition", out var hit) && hit.ValueKind != JsonValueKind.Null))
                        return LegacyError("Conditional and hit-count breakpoints are not supported.");
                    if (!arguments.TryGetValue("line", out var line) || !TryReadOutputLineCount(line, out var lineNumber)
                        || lineNumber is < 1 or > int.MaxValue)
                        return LegacyError("Invalid breakpoint line.");
                    if (!breakpointSourceValid) return LegacyError("Breakpoint source unavailable.");
                    breakpointLine = (int)lineNumber;
                    var index = FindLegacyBreakpoint(breakpointFile!, breakpointLine);
                    if (name == AddBreakpoint && index < 0 && _legacyBreakpoints.Count >= MaximumBreakpointCount)
                        return LegacyError("Breakpoint limit reached.");
                    arguments.Remove("condition");
                    arguments.Remove("hit_condition");
                    arguments["file"] = JsonSerializer.SerializeToElement(breakpointFile);
                    arguments["line"] = JsonSerializer.SerializeToElement(breakpointLine);
                    if (_legacySessionId is null)
                    {
                        if (name == AddBreakpoint)
                        {
                            if (index < 0) _legacyBreakpoints.Add((breakpointFile!, breakpointLine));
                            return LegacyResponse(new { file = breakpointFile, line = breakpointLine, condition = (string?)null, verified = false });
                        }
                        if (index >= 0) _legacyBreakpoints.RemoveAt(index);
                        return LegacyResponse(new { removed = index >= 0 });
                    }
                    if (!_legacyBreakpointGate.Wait(0)) return LegacyError("A breakpoint update is already in progress.");
                    breakpointHeld = true;
                    _legacyBreakpointSessionId = _legacySessionId;
                    cancellationToken.ThrowIfCancellationRequested();
                    breakpointRemoved = ApplyLegacyBreakpoint(breakpointFile!, breakpointLine, name == RemoveBreakpoint);
                    if (name == RemoveBreakpoint && !breakpointRemoved)
                        return LegacyResponse(new { removed = false });
                    breakpointLines = SnapshotLegacyBreakpointLines(breakpointFile!);
                }
                if (name == StartDebug)
                {
                    if (!arguments.TryGetValue("program", out var program) || program.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(program.GetString()))
                        return LegacyError("A program is required.");
                    if (arguments.TryGetValue("stealth_mode", out var stealth))
                    {
                        if (stealth.ValueKind != JsonValueKind.False) return LegacyError("stealth_mode is not supported.");
                        arguments.Remove("stealth_mode");
                    }
                    if (arguments.TryGetValue("pre_build", out var preBuild) && preBuild.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                        return LegacyError("Invalid pre_build.");
                    if (arguments.TryGetValue("stop_at_entry", out var stopAtEntry) && stopAtEntry.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                        return LegacyError("Invalid stop_at_entry.");
                    if (!arguments.ContainsKey("pre_build")) arguments["pre_build"] = JsonSerializer.SerializeToElement(true);
                    if (!arguments.ContainsKey("stop_at_entry")) arguments["stop_at_entry"] = JsonSerializer.SerializeToElement(false);
                    arguments["justMyCode"] = JsonSerializer.SerializeToElement(false);
                    arguments["breakpoints"] = JsonSerializer.SerializeToElement(_legacyBreakpoints.Select(static breakpoint => new { file = breakpoint.File, line = breakpoint.Line }).ToArray());
                    _legacyHasOwner = true;
                    _legacyOwner = muxId;
                    _legacyOwnerActivity = Stopwatch.GetTimestamp();
                    _legacySessionGeneration++;
                    _legacyStarting = true;
                    _legacyGate.Release();
                    gateHeld = false;
                    CallToolResult started;
                    var startCompleted = false;
                    LegacyResourceOwner.Generation? generation = null;
                    try
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        generation = _legacyResources.Begin();
                        started = await StartAsync(context, cancellationToken, arguments, generation.Observe).ConfigureAwait(false);
                        startCompleted = true;
                    }
                    finally
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        _legacyStarting = false;
                        if (!startCompleted && generation is not null) _legacyResources.Discard(generation);
                        if (!startCompleted) { _legacyHasOwner = false; _legacyOwner = null; }
                    }
                    if (started.IsError == true || started.StructuredContent is not { } startContent
                        || !startContent.TryGetProperty("debugSessionId", out var tokenElement)
                        || tokenElement.GetString() is not { } token)
                    {
                        _legacyResources.Discard(generation!);
                        _legacyHasOwner = false;
                        _legacyOwner = null;
                        return LegacyNativeError(started);
                    }
                    _legacySessionId = token;
                    if (cancellationToken.IsCancellationRequested)
                    {
                        _legacyResources.Discard(generation!);
                        _legacyStopping = true;
                        _legacyGate.Release();
                        gateHeld = false;
                        CallToolResult cleanup;
                        try { cleanup = await StopAsync(LegacyRequest(StopDebug, token, null), CancellationToken.None).ConfigureAwait(false); }
                        finally
                        {
                            await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                            gateHeld = true;
                            _legacyStopping = false;
                        }
                        if (cleanup.IsError != true)
                        {
                            _legacySessionId = null;
                            _legacyHasOwner = false;
                            _legacyOwner = null;
                        }
                        cancellationToken.ThrowIfCancellationRequested();
                    }
                    _legacyResources.Publish(generation!, token);
                    return LegacyResponse(new { success = true, program = program.GetString() });
                }
                if (name == GetDebugState)
                {
                    var snapshot = LegacySnapshot();
                    return LegacyResponse(new
                    {
                        state = snapshot.State,
                        execState = snapshot.State == "stopped" ? snapshot.Reason switch
                        {
                            "breakpoint" or "function breakpoint" or "data breakpoint" or "entry" => "stopped-at-breakpoint",
                            "exception" => "stopped-at-exception",
                            "step" => "stepping",
                            "pause" => "stopped-at-pause",
                            _ => "stopped-other",
                        } : snapshot.State,
                        currentThreadId = snapshot.ThreadId,
                        stopReason = snapshot.Reason,
                        exitCode = snapshot.ExitCode
                    }, snapshot.State);
                }
                if (_legacySessionId is null && name is (GetOutput or GetOutputTail))
                {
                    var idleArguments = LegacyRequest(name, "idle", arguments).Arguments;
                    if (name == GetOutput)
                        return TryReadOutputArguments(idleArguments, out _, out _, out _)
                            ? LegacyResponse(new { output = string.Empty }) : LegacyError("Unsupported or invalid argument.");
                    return TryReadOutputTailArguments(idleArguments, out _, out _, out _)
                        ? LegacyResponse(new { total_lines = 0, returned_lines = 0, output = string.Empty })
                        : LegacyError("Unsupported or invalid argument.");
                }
                if (_legacySessionId is not { } sessionId) return LegacyError("No active debug session.");
                var targetedStep = false;
                if (name == StepInto && arguments.Remove("target_id", out var target) && target.ValueKind != JsonValueKind.Null)
                {
                    if (!TryReadOutputLineCount(target, out var targetNumber) || targetNumber is < int.MinValue or > int.MaxValue)
                        return LegacyError("Invalid target_id.");
                    if (arguments.TryGetValue("thread_id", out var targetThread) && targetThread.ValueKind != JsonValueKind.Null)
                    {
                        if (!TryReadOutputLineCount(targetThread, out var targetThreadNumber) || targetThreadNumber is < int.MinValue or > int.MaxValue)
                            return LegacyError("Invalid thread_id.");
                        if (targetThreadNumber == 0) arguments["thread_id"] = JsonSerializer.SerializeToElement<int?>(null);
                    }
                    if (_legacyInspectionSessionId != sessionId || _legacyInspectionContextId is null || _legacyCurrentFrameId is null
                        || !StringComparer.Ordinal.Equals(_legacyInspectionMuxId, muxId))
                        return LegacyError("Call get_call_stack for the current stopped context first.");
                    arguments["target_id"] = JsonSerializer.SerializeToElement((int)targetNumber);
                    arguments["inspectionContextId"] = JsonSerializer.SerializeToElement(_legacyInspectionContextId);
                    targetedStep = true;
                }
                if (name == PauseExecution && arguments.Remove("thread_id", out var pauseThread))
                {
                    if (pauseThread.ValueKind == JsonValueKind.Null)
                        arguments["threadId"] = pauseThread;
                    else if (TryReadOutputLineCount(pauseThread, out var threadNumber) && threadNumber is >= int.MinValue and <= int.MaxValue)
                        arguments["threadId"] = JsonSerializer.SerializeToElement((int)threadNumber);
                    else return LegacyError("Invalid thread_id.");
                }
                if (name == GetExceptionInfo && arguments.TryGetValue("thread_id", out var exceptionThread)
                    && TryReadOutputLineCount(exceptionThread, out var exceptionThreadNumber) && exceptionThreadNumber == 0)
                    arguments["thread_id"] = JsonSerializer.SerializeToElement<int?>(null);
                var stackRevision = _legacyInspectionRevision;
                if (name is GetCallStack or StepOver or StepInto or StepOut or PauseExecution or ContinueExecution or StopDebug)
                {
                    _legacyInspectionSessionId = null;
                    _legacyInspectionContextId = null;
                    _legacyInspectionMuxId = null;
                    _legacyCurrentFrameId = null;
                    stackRevision = ++_legacyInspectionRevision;
                }
                if (name == GetCallStack)
                    arguments["includeInspectionContext"] = JsonSerializer.SerializeToElement(true);
                if (name is GetScopes or GetVariables or EvaluateExpression or SetVariable or GetStepInTargets)
                {
                    if (_legacyInspectionSessionId != sessionId || _legacyInspectionContextId is null || _legacyCurrentFrameId is null
                        || !StringComparer.Ordinal.Equals(_legacyInspectionMuxId, muxId))
                        return LegacyError("Call get_call_stack for the current stopped context first.");
                    if (name is GetVariables or SetVariable)
                    {
                        if (name == GetVariables && arguments.TryGetValue("filter", out var filter) && filter.ValueKind != JsonValueKind.Null)
                            return LegacyError("Variable filtering is not supported.");
                        arguments.Remove("filter");
                        if (!arguments.Remove("variables_reference", out var reference)
                            || !TryReadOutputLineCount(reference, out var referenceNumber)
                            || referenceNumber is < 1 or > int.MaxValue)
                            return LegacyError("A valid variables_reference is required.");
                        arguments["variablesReference"] = JsonSerializer.SerializeToElement((int)referenceNumber);
                    }
                    else
                    {
                        var selectedFrame = _legacyCurrentFrameId.Value;
                        if (arguments.Remove("frame_id", out var frame) && frame.ValueKind != JsonValueKind.Null)
                        {
                            if (!TryReadOutputLineCount(frame, out var frameNumber) || frameNumber is < int.MinValue or > int.MaxValue)
                                return LegacyError("Invalid frame_id.");
                            if (frameNumber != 0) selectedFrame = (int)frameNumber;
                        }
                        arguments["frameId"] = JsonSerializer.SerializeToElement(selectedFrame);
                    }
                    arguments["inspectionContextId"] = JsonSerializer.SerializeToElement(_legacyInspectionContextId);
                    if (name == SetVariable)
                    {
                        _legacyInspectionSessionId = null;
                        _legacyInspectionContextId = null;
                        _legacyInspectionMuxId = null;
                        _legacyCurrentFrameId = null;
                        stackRevision = ++_legacyInspectionRevision;
                    }
                }
                var inspectionContext = name is GetScopes or GetVariables or EvaluateExpression or GetStepInTargets ? _legacyInspectionContextId : null;
                NetCoreDbgSession? inspectionSession = null;
                if (name is GetCallStack or GetScopes or GetVariables or EvaluateExpression or SetVariable or GetExceptionInfo
                    or GetStepInTargets or GetModules or GetProgress or GetLoadedSources || targetedStep)
                {
                    if (!_sessions.TryGetValue(sessionId, out inspectionSession))
                        return LegacyError("No active debug session.");
                }
                var exceptionContext = name == GetExceptionInfo ? inspectionSession!.CaptureStoppedContext() : null;
                if (name == GetExceptionInfo && exceptionContext is null)
                    return LegacyError("No current stopped context.");
                var request = LegacyRequest(name, sessionId, arguments);
                if (name == StopDebug)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    _legacySessionGeneration++;
                    _legacyStopping = true;
                    OutputLifetime? retiringOutput;
                    lock (_lifetimeGate) _outputs.TryGetValue(sessionId, out retiringOutput);
                    _legacyGate.Release();
                    gateHeld = false;
                    CallToolResult stopped;
                    LegacyResourceOwner.Generation? retired = null;
                    try
                    {
                        retired = _legacyResources.Retire();
                        stopped = await StopAsync(request, CancellationToken.None).ConfigureAwait(false);
                    }
                    finally
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        _legacyStopping = false;
                    }
                    if (stopped.IsError == true) { _legacyResources.Restore(retired); return LegacyNativeError(stopped); }
                    _legacySessionId = null;
                    _legacyHasOwner = false;
                    _legacyOwner = null;
                    var idleGeneration = _legacyResources.Begin();
                    if (retiringOutput?.Buffer.Snapshot(false, null).Length > 0) idleGeneration.Observe("output");
                    idleGeneration.Observe("thread");
                    _legacyResources.Publish(idleGeneration);
                    return LegacyResponse(new { success = true });
                }
                _legacyGate.Release();
                gateHeld = false;
                var result = name switch
                {
                    GetThreads => await GetThreadsAsync(request, cancellationToken).ConfigureAwait(false),
                    GetCallStack => await GetCallStackAsync(request, cancellationToken).ConfigureAwait(false),
                    StepOver => await StepOverAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                    StepInto => await StepIntoAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                    StepOut => await StepOutAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                    PauseExecution => await PauseAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                    GetOutput => await GetOutputAsync(request, cancellationToken).ConfigureAwait(false),
                    GetOutputTail => await GetOutputAsync(request, cancellationToken, tail: true).ConfigureAwait(false),
                    GetScopes => await GetScopesAsync(request, cancellationToken).ConfigureAwait(false),
                    GetVariables => await GetVariablesAsync(request, cancellationToken).ConfigureAwait(false),
                    EvaluateExpression => await EvaluateExpressionAsync(request, cancellationToken).ConfigureAwait(false),
                    SetVariable => await SetVariableAsync(request, cancellationToken).ConfigureAwait(false),
                    ConfigureExceptions => await ConfigureExceptionsAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                    GetExceptionInfo => await GetExceptionInfoAsync(request, cancellationToken).ConfigureAwait(false),
                    GetModules => await GetRuntimeInventoryAsync(request, modules: true, cancellationToken).ConfigureAwait(false),
                    GetProgress => await GetRuntimeInventoryAsync(request, modules: false, cancellationToken).ConfigureAwait(false),
                    GetLoadedSources => await GetLoadedSourcesAsync(request, cancellationToken).ConfigureAwait(false),
                    GetStepInTargets => await GetStepInTargetsAsync(request, cancellationToken).ConfigureAwait(false),
                    AddBreakpoint => await UpdateBreakpointAsync(request, context.JsonRpcRequest.Id, cancellationToken, remove: false, breakpointLines).ConfigureAwait(false),
                    RemoveBreakpoint => await UpdateBreakpointAsync(request, context.JsonRpcRequest.Id, cancellationToken, remove: true, breakpointLines).ConfigureAwait(false),
                    ContinueExecution => await ContinueAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                    _ => throw new InvalidOperationException(),
                };
                await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                gateHeld = true;
                if (!string.Equals(_legacySessionId, sessionId, StringComparison.Ordinal))
                    return name is GetCallStack or GetScopes or GetVariables or EvaluateExpression or SetVariable or GetExceptionInfo or GetStepInTargets
                        ? LegacyInspectionChanged(name)
                        : LegacyError(consumesOutput ? "Output may have been cleared; do not retry automatically."
                            : name == ConfigureExceptions ? "Exception filters may have changed; do not retry automatically."
                            : targetedStep ? "Step outcome is unknown; it may have executed. Do not retry automatically."
                            : name is (AddBreakpoint or RemoveBreakpoint) ? "Requested breakpoint intent was retained; application to the live debugger is unknown. Do not retry automatically."
                            : "Debug session changed before operation completed.", "unknown");
                var observedState = LegacySnapshot(sessionId).State;
                if (name is GetScopes or GetVariables or EvaluateExpression or GetStepInTargets)
                {
                    var capturedState = stackRevision == _legacyInspectionRevision && inspectionSession is not null
                        && _sessions.TryGetValue(sessionId, out var currentSession) && ReferenceEquals(currentSession, inspectionSession)
                        && inspectionContext is not null ? inspectionSession.CaptureInspectionState(inspectionContext) : null;
                    if (capturedState is null) return LegacyInspectionChanged(name);
                    observedState = ProjectLegacyState(capturedState).State;
                }
                if (result.IsError == true || result.StructuredContent is not { } content)
                {
                    if (name is AddBreakpoint or RemoveBreakpoint)
                    {
                        if (result.StructuredContent is { } breakpointFailure
                            && breakpointFailure.TryGetProperty("error", out var breakpointCode)
                            && breakpointCode.GetString() == "DAP_SET_BREAKPOINTS_REFUSED")
                            return LegacyError("Requested breakpoint intent was retained; the live debugger rejected the file set. A later explicit add/remove for this file or explicit start can reapply it.", observedState);
                        return LegacyError("Requested breakpoint intent was retained; application to the live debugger is unknown. Do not retry automatically.", "unknown");
                    }
                    if (name == SetVariable && result.StructuredContent is { } failure
                        && failure.TryGetProperty("error", out var failureCode)
                        && failureCode.GetString() == "DAP_SET_VARIABLE_OUTCOME_UNKNOWN")
                        return LegacyInspectionChanged(name);
                    if (name == ConfigureExceptions && result.StructuredContent is { } filterFailure
                        && filterFailure.TryGetProperty("error", out var filterCode)
                        && filterCode.GetString() == "DAP_EXCEPTION_FILTERS_PROTOCOL_ERROR")
                        return LegacyError("Exception filters may have changed; do not retry automatically.", "unknown");
                    if (targetedStep && result.StructuredContent is { } stepFailure
                        && stepFailure.TryGetProperty("error", out var stepCode)
                        && stepCode.GetString() == "DAP_STEP_INTO_PROTOCOL_ERROR")
                        return LegacyError("Step outcome is unknown; it may have executed. Do not retry automatically.", "unknown");
                    return LegacyNativeError(result, observedState);
                }
                var kind = content.GetProperty("kind").GetString();
                if (name is GetModules or GetProgress or GetLoadedSources)
                {
                    if (inspectionSession is null || !_sessions.TryGetValue(sessionId, out var currentSession)
                        || !ReferenceEquals(currentSession, inspectionSession))
                        return LegacyError("Debug session changed before operation completed.", "unknown");
                    if (name == GetModules && kind == "modules_success")
                        return LegacyResponse(new { modules = content.GetProperty("modules"), count = content.GetProperty("count") }, observedState);
                    if (name == GetProgress && kind == "progress_success")
                    {
                        var mapped = LegacyResponse(new
                        {
                            progress = content.GetProperty("progress").EnumerateArray().Select(static item => new
                            {
                                progressId = item.GetProperty("progressId"),
                                progress_id = item.GetProperty("progressId"),
                                title = item.GetProperty("title"),
                                message = item.GetProperty("message"),
                                percentage = item.GetProperty("percentage"),
                                cancellable = item.GetProperty("cancellable"),
                                startedAt = item.GetProperty("startedAt"),
                                started_at = item.GetProperty("startedAt"),
                            }).ToArray(),
                            count = content.GetProperty("count")
                        }, observedState);
                        return mapped.StructuredContent is { } mappedContent
                            && Encoding.UTF8.GetByteCount(mappedContent.GetRawText()) <= MaximumSerializedInspectionBytes
                            ? mapped : LegacyError("RUNTIME_INVENTORY_TOO_LARGE", observedState);
                    }
                    if (name == GetLoadedSources && kind == "loaded_sources_success")
                        return LegacyResponse(new { sources = content.GetProperty("sources"), count = content.GetProperty("count") }, observedState);
                }
                if (name == GetStepInTargets && kind == "step_in_targets_success")
                    return LegacyResponse(new
                    {
                        targets = content.GetProperty("targets").EnumerateArray().Select(static item => new
                        {
                            id = item.GetProperty("id"),
                            label = item.GetProperty("label"),
                        }).ToArray()
                    }, observedState);
                if (targetedStep && (stackRevision != _legacyInspectionRevision
                    || (kind == "step_into_success" && (inspectionSession is null
                        || !_sessions.TryGetValue(sessionId, out var stepSession) || !ReferenceEquals(stepSession, inspectionSession)))))
                    return LegacyError("Step outcome is unknown; it may have executed. Do not retry automatically.", "unknown");
                if (name == GetThreads && kind == "threads_success") return LegacyResponse(content.GetProperty("threads"), observedState);
                if (name == GetCallStack && kind == "call_stack_success")
                {
                    if (stackRevision != _legacyInspectionRevision
                        || !content.TryGetProperty("inspectionContextId", out var inspection)
                        || inspection.ValueKind != JsonValueKind.String || inspection.GetString() is not { } stackContext
                        || inspectionSession is null || !_sessions.TryGetValue(sessionId, out var currentSession)
                        || !ReferenceEquals(currentSession, inspectionSession)
                        || inspectionSession.CaptureInspectionState(stackContext) is not { } stackState)
                        return LegacyInspectionChanged(name);
                    observedState = ProjectLegacyState(stackState).State;
                    var frames = content.GetProperty("frames");
                    if (frames.GetArrayLength() > 0)
                    {
                        _legacyInspectionSessionId = sessionId;
                        _legacyInspectionContextId = stackContext;
                        _legacyInspectionMuxId = muxId;
                        _legacyCurrentFrameId = frames[0].GetProperty("id").GetInt32();
                    }
                    return LegacyResponse(new { frames }, observedState);
                }
                if (name == GetScopes && kind == "scopes_success")
                    return LegacyResponse(content.GetProperty("scopes").EnumerateArray().Select(static scope => new
                    {
                        name = scope.GetProperty("name").GetString(),
                        variablesReference = scope.GetProperty("variablesReference").GetInt32(),
                    }).ToArray(), observedState);
                if (name == GetVariables && kind == "variables_success") return LegacyResponse(content.GetProperty("variables"), observedState);
                if (name == EvaluateExpression && kind == "evaluate_success")
                    return LegacyResponse(new { result = content.GetProperty("result"), type = content.GetProperty("type"), variablesReference = content.GetProperty("variablesReference") }, observedState);
                if (name == SetVariable && kind == "set_variable_success")
                {
                    if (stackRevision != _legacyInspectionRevision || inspectionSession is null
                        || !_sessions.TryGetValue(sessionId, out var currentSession) || !ReferenceEquals(currentSession, inspectionSession)
                        || !content.TryGetProperty("inspectionContextId", out var updatedContext)
                        || updatedContext.ValueKind != JsonValueKind.String || updatedContext.GetString() is not { } contextId
                        || inspectionSession.CaptureInspectionState(contextId) is not { } updatedState)
                        return LegacyInspectionChanged(name);
                    return LegacyResponse(new { value = content.GetProperty("value"), type = content.GetProperty("type"), variablesReference = content.GetProperty("variablesReference") }, ProjectLegacyState(updatedState).State);
                }
                if (name == ConfigureExceptions && kind == "exception_filters_acknowledged")
                    return LegacyResponse(new { filters = content.GetProperty("filters"), configured = true }, observedState);
                if (name == GetExceptionInfo && kind == "exception_info_success")
                {
                    if (stackRevision != _legacyInspectionRevision || inspectionSession is null || exceptionContext is not { } captured
                        || !_sessions.TryGetValue(sessionId, out var currentSession) || !ReferenceEquals(currentSession, inspectionSession)
                        || captured.State.CurrentThreadId is not { } stoppedThread
                        || !inspectionSession.IsStoppedContextCurrent(stoppedThread, captured.Context))
                        return LegacyInspectionChanged(name);
                    return LegacyResponse(content.GetProperty("exception"), ProjectLegacyState(captured.State).State);
                }
                if (name == AddBreakpoint && kind == "breakpoint_added")
                {
                    return LegacyResponse(new { file = content.GetProperty("file"), line = content.GetProperty("line"), condition = (string?)null, verified = content.GetProperty("verified") }, observedState);
                }
                if (name == RemoveBreakpoint && kind == "breakpoint_removed")
                {
                    return LegacyResponse(new { removed = breakpointRemoved }, observedState);
                }
                if (name == GetOutput && kind == "output_success")
                    return LegacyResponse(new { output = content.GetProperty("output") }, observedState);
                if (name == GetOutputTail && kind == "output_tail_success")
                    return LegacyResponse(new { total_lines = content.GetProperty("totalLines"), returned_lines = content.GetProperty("returnedLines"), output = content.GetProperty("output") }, observedState);
                if (name == PauseExecution && kind == "pause_acknowledged")
                    return LegacyResponse(new { success = true, threadId = content.GetProperty("threadId") }, observedState);
                if ((name == StepInto && kind == "step_into_success") || (name == StepOut && kind == "step_out_success"))
                    return LegacyExecution("stopped", "step", content.GetProperty("threadId").GetInt32(), false, null);
                if (name == StepOver && kind == "step_over_success")
                    return LegacyExecution("stopped", "step", content.GetProperty("threadId").GetInt32(), false, null);
                if ((name == StepInto && kind == "step_into_terminal") || (name == StepOut && kind == "step_out_terminal"))
                    return LegacyExecution("terminated", null, null, false,
                        content.TryGetProperty("exitCode", out var stepExit) ? stepExit.GetInt32() : null);
                if ((name == StepInto && kind == "step_into_timed_out") || (name == StepOut && kind == "step_out_timed_out"))
                    return LegacyExecution("unknown", null, content.GetProperty("threadId").GetInt32(), true, null);
                if (name == ContinueExecution && kind == "continue_stopped")
                    return LegacyExecution("stopped", content.GetProperty("reason").GetString(),
                        content.TryGetProperty("threadId", out var thread) ? thread.GetInt32() : null, false, null);
                if (name == ContinueExecution && kind == "continue_terminal")
                    return LegacyExecution("terminated", null, null, false,
                        content.TryGetProperty("exitCode", out var exit) ? exit.GetInt32() : null);
                if (name == ContinueExecution && kind == "continue_timed_out")
                    return LegacyExecution("unknown", null, content.GetProperty("threadId").GetInt32(), true, null);
                return LegacyError($"Native operation outcome: {kind ?? "unknown"}.", observedState);
            }
            finally
            {
                if (breakpointHeld)
                {
                    if (!gateHeld)
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                    }
                    _legacyBreakpointSessionId = null;
                    _legacyBreakpointGate.Release();
                }
                if (gateHeld) _legacyGate.Release();
                if (lifecycleHeld) _legacyLifecycle.Release();
            }
        }

        private bool ApplyLegacyBreakpoint(string file, int line, bool remove)
        {
            var index = FindLegacyBreakpoint(file, line);
            if (remove)
            {
                if (index >= 0) _legacyBreakpoints.RemoveAt(index);
                return index >= 0;
            }
            if (index < 0) _legacyBreakpoints.Add((file, line));
            return false;
        }

        private int[] SnapshotLegacyBreakpointLines(string file)
        {
            var count = 0;
            foreach (var breakpoint in _legacyBreakpoints)
                if (_legacyBreakpointPathComparer.Equals(breakpoint.File, file)) count++;
            var lines = new int[count];
            var index = 0;
            foreach (var breakpoint in _legacyBreakpoints)
                if (_legacyBreakpointPathComparer.Equals(breakpoint.File, file)) lines[index++] = breakpoint.Line;
            return lines;
        }

        private int FindLegacyBreakpoint(string file, int line)
        {
            for (var index = 0; index < _legacyBreakpoints.Count; index++)
            {
                var breakpoint = _legacyBreakpoints[index];
                if (breakpoint.Line == line && _legacyBreakpointPathComparer.Equals(breakpoint.File, file)) return index;
            }
            return -1;
        }

        private static bool HasInvalidLegacyArguments(string name, Dictionary<string, JsonElement> arguments)
        {
            foreach (var key in arguments.Keys)
            {
                var invalid = name switch
                {
                    StartDebug => key is not ("program" or "cwd" or "args" or "env" or "launch_profile" or "stop_at_entry" or "pre_build" or "build_project" or "build_configuration" or "stealth_mode"),
                    GetCallStack => key is not ("thread_id" or "levels"),
                    AddBreakpoint => key is not ("file" or "line" or "condition" or "hit_condition"),
                    RemoveBreakpoint => key is not ("file" or "line"),
                    GetScopes => key != "frame_id",
                    GetStepInTargets => key != "frame_id",
                    GetVariables => key is not ("variables_reference" or "filter" or "start" or "count"),
                    EvaluateExpression => key is not ("expression" or "frame_id"),
                    SetVariable => key is not ("variables_reference" or "name" or "value"),
                    ConfigureExceptions => key != "filters",
                    GetExceptionInfo => key != "thread_id",
                    StepOver or StepOut or PauseExecution or ContinueExecution => key != "thread_id",
                    StepInto => key is not ("thread_id" or "target_id"),
                    GetOutput => key is not ("clear" or "category"),
                    GetOutputTail => key is not ("lines" or "category"),
                    _ => true,
                };
                if (invalid) return true;
            }
            return false;
        }

        private static CallToolRequestParams LegacyRequest(string name, string sessionId, Dictionary<string, JsonElement>? arguments)
        {
            arguments ??= new Dictionary<string, JsonElement>(StringComparer.Ordinal);
            arguments["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId);
            return new CallToolRequestParams { Name = name, Arguments = arguments };
        }

        private (string State, int? ThreadId, string? Reason, int? ExitCode) LegacySnapshot()
        {
            if (_legacySessionId is null) return (_legacyStarting ? "initializing" : "idle", null, null, null);
            return LegacySnapshot(_legacySessionId);
        }

        private (string State, int? ThreadId, string? Reason, int? ExitCode) LegacySnapshot(string sessionId)
        {
            if (_sessions.TryGetValue(sessionId, out var session)) return ProjectLegacyState(session.State);
            lock (_lifetimeGate)
            {
                if (_outputs.TryGetValue(sessionId, out var output))
                {
                    var cleanup = output.Cleanup.Task;
                    if (cleanup.IsFaulted || cleanup.IsCanceled) return ("unknown", null, null, null);
                    var state = cleanup.IsCompletedSuccessfully ? cleanup.Result : output.RetirementState;
                    if (state?.Event is "exited" or "terminated") return ProjectLegacyState(state);
                }
            }
            return ("unknown", null, null, null);
        }

        private static (string State, int? ThreadId, string? Reason, int? ExitCode) ProjectLegacyState(DapSessionState state)
        {
            if (state.Event == "stopped" && state.CurrentThreadId is null)
                return ("unknown", null, null, state.ExitCode);
            return (state.Event switch
            {
                "stopped" when state.CurrentThreadId is not null => "stopped",
                "continued" => "running",
                "terminated" or "exited" => "terminated",
                _ => "unknown"
            }, state.CurrentThreadId, state.StopReason, state.ExitCode);
        }

        private static string[] LegacyActions(string state) => state switch
        {
            "idle" => ["start_debug"],
            "stopped" => ["get_debug_state", "get_threads", "get_call_stack", "step_over", "step_into", "step_out", "continue_execution", "get_output", "get_output_tail", "stop_debug"],
            "running" => ["get_debug_state", "pause_execution", "get_output", "get_output_tail", "stop_debug"],
            _ => ["get_debug_state", "get_output", "get_output_tail", "stop_debug"],
        };

        private CallToolResult LegacyResponse(object data) => LegacyResponse(data, LegacySnapshot().State);

        private static CallToolResult LegacyResponse(object data, string state) =>
            Result(new { state, next_actions = LegacyActions(state), message = state == "idle" ? "No active debug session." : "Debug session state observed.", data }, false);

        private CallToolResult LegacyError(string error) => LegacyError(error, LegacySnapshot().State);

        private static CallToolResult LegacyError(string error, string state) =>
            Result(new { error, state, next_actions = LegacyActions(state), message = $"Error: {error}" }, false);

        private static CallToolResult LegacyInspectionChanged(string tool) => LegacyError(
            tool is EvaluateExpression or SetVariable
                ? $"{(tool == SetVariable ? "Variable mutation" : "Evaluation")} outcome is unknown; it may have executed. Do not retry automatically."
                : "Stopped inspection context changed before the operation completed.", "unknown");

        private CallToolResult LegacyNativeError(CallToolResult result) => LegacyNativeError(result, LegacySnapshot().State);

        private static CallToolResult LegacyNativeError(CallToolResult result, string state)
        {
            var error = result.StructuredContent is { } content && content.TryGetProperty("error", out var value)
                ? value.GetString() : "Native operation did not complete successfully.";
            return LegacyError(error ?? "Native operation did not complete successfully.", state);
        }

        private static CallToolResult LegacyExecution(string state, string? reason, int? threadId, bool timedOut, int? exitCode) =>
            Result(new
            {
                state,
                reason,
                thread_id = threadId,
                timed_out = timedOut,
                exit_code = exitCode,
                message = timedOut ? "The execution wait timed out; current state is unknown." : "Execution event observed.",
                next_actions = LegacyActions(state)
            }, false);
        private ProjectRootResolver? _projectRootResolver;
        internal ProjectRootResolver ProjectRootResolver
        {
            get => _projectRootResolver ??= NetCoreDbg.Mcp.Host.ProjectRootResolver.FromHostArguments([], Environment.CurrentDirectory);
            init => _projectRootResolver = value;
        }
        internal CancellationToken ShutdownToken => _startShutdown.Token;
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

        private DebugSessionRegistry(string? debuggerPath, Action? failedStartCleanupCompleted)
            : this(debuggerPath, bridgePath: null, artifactRoot: null,
                static session => session.IsUsable, static session => session.DisposeAsync(), failedStartCleanupCompleted)
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
            : this(debuggerPath, bridgePath, artifactRoot, isUsable, dispose, failedStartCleanupCompleted: null)
        {
        }

        private DebugSessionRegistry(
            string? debuggerPath,
            string? bridgePath,
            string? artifactRoot,
            Func<NetCoreDbgSession, bool> isUsable,
            Func<NetCoreDbgSession, ValueTask> dispose,
            Action? failedStartCleanupCompleted)
        {
            _debuggerPath = debuggerPath;
            _bridgePath = bridgePath;
            _artifactRoot = artifactRoot;
            _isUsable = isUsable;
            _dispose = dispose;
            _failedStartCleanupCompleted = failedStartCleanupCompleted;
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
                GetScopes => await GetScopesAsync(request, cancellationToken).ConfigureAwait(false),
                GetStepInTargets => await GetStepInTargetsAsync(request, cancellationToken).ConfigureAwait(false),
                GetVariables => await GetVariablesAsync(request, cancellationToken).ConfigureAwait(false),
                EvaluateExpression => await EvaluateExpressionAsync(request, cancellationToken).ConfigureAwait(false),
                SetVariable => await SetVariableAsync(request, cancellationToken).ConfigureAwait(false),
                GetExceptionInfo => await GetExceptionInfoAsync(request, cancellationToken).ConfigureAwait(false),
                GetStopContext => await GetCompositeContextAsync(request, false, cancellationToken).ConfigureAwait(false),
                GetExceptionContext => await GetCompositeContextAsync(request, true, cancellationToken).ConfigureAwait(false),
                GetModules => await GetRuntimeInventoryAsync(request, modules: true, cancellationToken).ConfigureAwait(false),
                GetProgress => await GetRuntimeInventoryAsync(request, modules: false, cancellationToken).ConfigureAwait(false),
                GetLoadedSources => await GetLoadedSourcesAsync(request, cancellationToken).ConfigureAwait(false),
                GetOutput => await GetOutputAsync(request, cancellationToken).ConfigureAwait(false),
                GetOutputTail => await GetOutputAsync(request, cancellationToken, tail: true).ConfigureAwait(false),
                SearchOutput => await SearchOutputAsync(request, cancellationToken).ConfigureAwait(false),
                StepOver => await StepOverAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                StepInto => await StepIntoAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                StepOut => await StepOutAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                ContinueExecution => await ContinueAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                PauseExecution => await PauseAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                AddBreakpoint => await AddBreakpointAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                RemoveBreakpoint => await RemoveBreakpointAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                ConfigureExceptions => await ConfigureExceptionsAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                UiInvoke => await UiInvokeAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                UiInvokeObserved => await UiInvokeObservedAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
                UiGetWindowTree => await UiGetWindowTreeAsync(request, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false),
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
            CancellationToken cancellationToken, IDictionary<string, JsonElement>? argumentsOverride = null, Action<string>? resourceObserver = null)
        {
            var arguments = argumentsOverride ?? context.Params.Arguments;
            if (!TryReadProgram(arguments, out var program, out var hasProgram)
                || !TryReadBreakpoints(arguments, out var breakpoints)
                || !TryReadLaunchOptions(arguments, out var launchOptions)
                || !TryReadLaunchProfileArguments(arguments, out var projectRoot, out var profileName)
                || !TryReadPreBuildArguments(arguments, out var preBuild))
            {
                return InvalidArguments(StartDebug);
            }

            if (preBuild.Enabled && preBuild.Project is null)
                return Error("start_debug_build_project_required", "START_DEBUG_BUILD_PROJECT_REQUIRED");

            if (!preBuild.Enabled && launchOptions?.WorkingDirectory is { } workingDirectory && !Directory.Exists(workingDirectory))
            {
                return Error("start_debug_cwd_unavailable", "START_DEBUG_CWD_UNAVAILABLE");
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

                    var inputRequests = new Dictionary<string, InputRequest>
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
                    };
                    if (projectRoot is null && global::NetCoreDbg.Mcp.Host.ProjectRootResolver.SupportsClientRoots(context.Server)
                        && global::NetCoreDbg.Mcp.Host.ProjectRootResolver.UsesNativeMrtr(context.Server, context.JsonRpcRequest.Context?.ProtocolVersion))
                    {
                        try
                        {
                            // Request roots again even if this attempt supplied them: SDK clients replace
                            // inputResponses on retry, so both inputs must travel in the same round trip.
                            _ = await ProjectRootResolver.ResolveAsync(context.Server, cancellationToken,
                                requestProtocolVersion: context.JsonRpcRequest.Context?.ProtocolVersion).ConfigureAwait(false);
                        }
                        catch (InputRequiredException rootsRequired)
                        {
                            if (rootsRequired.Result.InputRequests is { } rootsRequests)
                                foreach (var (key, request) in rootsRequests) inputRequests.Add(key, request);
                        }
                    }
                    cancellationToken.ThrowIfCancellationRequested();
                    throw new InputRequiredException(inputRequests);
                }
            }

            if (preBuild.Enabled && !DapBuildCoordinator.TryNormalizeProgram(program!, out program))
                return InvalidArguments(StartDebug);
            if (preBuild.Enabled && launchOptions?.WorkingDirectory is { } futureWorkingDirectory &&
                !DapBuildCoordinator.IsValidDirectoryPath(futureWorkingDirectory))
                return Error("start_debug_cwd_unavailable", "START_DEBUG_CWD_UNAVAILABLE");


            if (!TryNormalizeBreakpointSources(breakpoints))
            {
                return Error("start_debug_source_unavailable", "START_DEBUG_SOURCE_UNAVAILABLE");
            }

            if (projectRoot is not null)
            {
                try { projectRoot = Path.GetFullPath(projectRoot); }
                catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException)
                {
                    return Error("start_debug_project_unavailable", "START_DEBUG_PROJECT_UNAVAILABLE");
                }
                if (!Directory.Exists(projectRoot))
                    return Error("start_debug_project_unavailable", "START_DEBUG_PROJECT_UNAVAILABLE");
            }

            if (string.IsNullOrWhiteSpace(_debuggerPath))
            {
                return Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
            }

            lock (_lifetimeGate)
            {
                if (_shutdownRequested)
                {
                    return NotFound();
                }
                if (_buildCleanupUnconfirmed)
                    return Error("start_debug_build_cleanup_unconfirmed", "START_DEBUG_BUILD_CLEANUP_UNCONFIRMED");
                if (_buildActive || (preBuild.Enabled && (_starts != 0 || _ownedSlots.Count != 0)))
                    return Error("start_debug_build_busy", "START_DEBUG_BUILD_BUSY");
                if (preBuild.Enabled) _buildActive = true;

                _starts++;
            }

            NetCoreDbgSession? session = null;
            NativeSceneSessionBinding? binding = null;
            SessionSlot? registeredSlot = null;
            string? registeredToken = null;
            CancellationTokenSource? startupCancellation = null;
            try
            {
                startupCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, _startShutdown.Token);
                startupCancellation.Token.ThrowIfCancellationRequested();
                if (projectRoot is null)
                {
                    projectRoot = await ProjectRootResolver.ResolveAsync(context.Server, startupCancellation.Token,
                        context.Params.InputResponses, context.JsonRpcRequest.Context?.ProtocolVersion).ConfigureAwait(false);
                    if (projectRoot is null)
                        return Error("start_debug_project_unavailable", "START_DEBUG_PROJECT_UNAVAILABLE");
                }
                DapLaunchProfileResult profile;
                try
                {
                    profile = await DapLaunchProfile.ResolveAsync(projectRoot, profileName, launchOptions?.Environment, startupCancellation.Token).ConfigureAwait(false);
                }
                catch (DapLaunchProfileException)
                {
                    return Error("start_debug_launch_profile_invalid", "START_DEBUG_LAUNCH_PROFILE_INVALID");
                }

                if (preBuild.Enabled)
                {
                    if (!DapBuildCoordinator.TryCreatePlan(projectRoot, preBuild.Project, preBuild.Configuration, out var plan))
                        return Error("start_debug_build_project_invalid", "START_DEBUG_BUILD_PROJECT_INVALID");
                    DapBuildResult build;
                    try
                    {
                        build = await DapBuildCoordinator.BuildAsync(plan!, startupCancellation.Token).ConfigureAwait(false);
                    }
                    catch (Exception exception) when (exception is DapBuildCleanupException or AggregateException)
                    {
                        lock (_lifetimeGate) { _buildCleanupUnconfirmed = true; }
                        if (cancellationToken.IsCancellationRequested) throw;
                        return Error("start_debug_build_cleanup_unconfirmed", "START_DEBUG_BUILD_CLEANUP_UNCONFIRMED");
                    }
                    catch (TimeoutException)
                    {
                        return Error("start_debug_build_timeout", "START_DEBUG_BUILD_TIMEOUT");
                    }
                    catch (FileNotFoundException)
                    {
                        return Error("start_debug_dotnet_unavailable", "START_DEBUG_DOTNET_UNAVAILABLE");
                    }
                    catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or InvalidOperationException or System.ComponentModel.Win32Exception or ArgumentException)
                    {
                        return Error("start_debug_build_start_failed", "START_DEBUG_BUILD_START_FAILED");
                    }
                    if (build.Command.ExitCode != 0) return BuildFailure(build);
                    if (!DapBuildCoordinator.TryResolveBuiltProgram(program!, out program))
                        return Error("start_debug_program_unavailable", "START_DEBUG_PROGRAM_UNAVAILABLE");
                    if (launchOptions?.WorkingDirectory is { } builtWorkingDirectory && !Directory.Exists(builtWorkingDirectory))
                        return Error("start_debug_cwd_unavailable", "START_DEBUG_CWD_UNAVAILABLE");
                }

                if (profile.Environment is not null)
                {
                    launchOptions = launchOptions is null
                        ? new DapLaunchOptions(null, null, null, null, profile.Environment)
                        : launchOptions with { Environment = profile.Environment };
                }
                if (launchOptions?.Environment is { } environment
                    && environment.TryGetValue("PATH", out var pathOverride) && pathOverride is null
                    && program!.EndsWith(".dll", StringComparison.OrdinalIgnoreCase))
                {
                    var dotnetHost = DapLaunchEnvironment.FindExecutable("dotnet");
                    if (dotnetHost is null)
                        return Error("start_debug_dotnet_unavailable", "START_DEBUG_DOTNET_UNAVAILABLE");
                    launchOptions = launchOptions with { DotnetHostPath = dotnetHost };
                }
                var token = CreateToken();
                binding = new NativeSceneSessionBinding(token, _bridgePath, _artifactRoot, _supportsNativeSceneCapture);
                session = await NetCoreDbgSession.StartAsync(
                    _debuggerPath,
                    program!,
                    InitializeTimeout,
                    RequestTimeout,
                    StopTimeout,
                    binding.ProbeLaunchEnvironment,
                    breakpoints,
                    _failedStartCleanupCompleted,
                    launchOptions,
                    startupCancellation.Token, resourceObserver).ConfigureAwait(false);
                binding.AttachSession(session);
                var output = new OutputLifetime(session.OutputBuffer);
                SessionSlot? slot = null;
                slot = new SessionSlot(
                    StopTimeout,
                    session.StopForOwnerAsync,
                    () => DisposeSlotResourcesAsync(session, binding),
                    () => RemoveSlot(token, session, binding, slot!, output));
                lock (_lifetimeGate)
                {
                    if (_shutdownRequested)
                    {
                        throw new InvalidOperationException("The debug-session registry is shutting down.");
                    }

                    if (_outputs.ContainsKey(token) || _ownedSlots.ContainsKey(token)
                        || !_sessions.TryAdd(token, session)
                        || !_slots.TryAdd(token, slot)
                        || !_nativeSceneBindings.TryAdd(token, binding))
                    {
                        _slots.TryRemove(new KeyValuePair<string, SessionSlot>(token, slot));
                        _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(token, session));
                        _nativeSceneBindings.TryRemove(new KeyValuePair<string, NativeSceneSessionBinding>(token, binding));
                        throw new InvalidOperationException("The debug-session token is already registered.");
                    }

                    _outputs.Add(token, output);
                    _ownedSlots.Add(token, (slot, output));
                    registeredSlot = slot;
                    registeredToken = token;
                    session.RetirementRequested += retiringSession => OnRetirementRequested(token, retiringSession);
                }

                if (session.IsRetirementRequested || !_isUsable(session))
                {
                    OnRetirementRequested(token, session);
                }

                var metadata = profile.Metadata;
                return Result(new StartDebugSuccessContent(
                    "start_debug_success", token, LifecycleState(session.State),
                    breakpoints.Length == 0 ? null : session.InitialBreakpoints,
                    metadata is null ? null : new LaunchEnvironmentContent(metadata.Source, metadata.Profile,
                        metadata.Path, metadata.VariableNames, metadata.AppliedCount)), isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                if (registeredSlot is not null)
                {
                    lock (_lifetimeGate)
                    {
                        _outputs.Remove(registeredToken!);
                    }

                    try
                    {
                        await registeredSlot.CloseAndDrainAsync().ConfigureAwait(false);
                    }
                    catch (Exception cleanupFailure)
                    {
                        RecordStartCleanupFailure(cleanupFailure);
                    }
                }
                else
                {
                    await DisposeUnpublishedResourcesAsync(session, binding, startupCancellation?.Token ?? cancellationToken).ConfigureAwait(false);
                }

                throw;
            }
            catch (InputRequiredException)
            {
                throw;
            }
            catch (Exception exception)
            {
                var cleanupComplete = true;
                if (exception is DapStartupCleanupException)
                {
                    RecordStartCleanupFailure(exception);
                }

                if (registeredSlot is not null)
                {
                    lock (_lifetimeGate)
                    {
                        _outputs.Remove(registeredToken!);
                    }

                    try
                    {
                        await registeredSlot.CloseAndDrainAsync().ConfigureAwait(false);
                    }
                    catch (Exception cleanupFailure)
                    {
                        RecordStartCleanupFailure(cleanupFailure);
                        cleanupComplete = false;
                    }
                }
                else
                {
                    try
                    {
                        await DisposeUnpublishedResourcesAsync(session, binding, startupCancellation?.Token ?? cancellationToken).ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                        cleanupComplete = false;
                    }
                }

                if (cancellationToken.IsCancellationRequested)
                {
                    throw;
                }

                return exception is DapLaunchRefusedException && cleanupComplete
                    ? Error("dap_launch_refused", "DAP_LAUNCH_REFUSED")
                    : exception is DapBreakpointsException breakpointFailure
                    ? breakpointFailure.IsRefused
                        ? Error("dap_breakpoints_refused", "DAP_BREAKPOINTS_REFUSED")
                        : Error("dap_breakpoints_protocol_error", "DAP_BREAKPOINTS_PROTOCOL_ERROR")
                    : Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
            }
            finally
            {
                try
                {
                    startupCancellation?.Dispose();
                }
                finally
                {
                    lock (_lifetimeGate)
                    {
                        if (preBuild.Enabled) _buildActive = false;
                        if (--_starts == 0)
                        {
                            _startsDrained?.TrySetResult(true);
                        }
                    }
                }
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
                else if (TryRemoveUnownedSession(sessionId!, session))
                {
                    await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
                    await DisposeRemovedSessionAsync(session).ConfigureAwait(false);
                }

                return NotFound();
            }

            var snapshot = session.GetStateWithSources();
            var serialized = JsonSerializer.Serialize(new
            {
                kind = "debug_state_success",
                debugSessionId = sessionId!,
                state = new DebugStateContent(snapshot.State.Event, snapshot.State.StopReason, snapshot.State.ExitCode,
                    snapshot.State.CurrentThreadId, snapshot.Sources, snapshot.Complete),
            });
            return Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes
                ? Error("debug_state_too_large", "DEBUG_STATE_TOO_LARGE")
                : SerializedResult(serialized, isError: false);
        }

        private ValueTask<CallToolResult> GetOutputAsync(
            CallToolRequestParams request,
            CancellationToken cancellationToken,
            bool tail = false)
        {
            var clear = false;
            var lines = 50L;
            string sessionId;
            string? category;
            var valid = tail
                ? TryReadOutputTailArguments(request.Arguments, out sessionId, out lines, out category)
                : TryReadOutputArguments(request.Arguments, out sessionId, out clear, out category);
            if (!valid)
            {
                return new(InvalidArguments(tail ? GetOutputTail : GetOutput));
            }

            OutputLifetime output;
            lock (_lifetimeGate)
            {
                if (!_outputs.TryGetValue(sessionId, out output!))
                {
                    return new(NotFound());
                }
            }

            // Admission ends at explicit release, not native retirement. The admitted
            // buffer reference needs no native lease and cannot delay debugger cleanup.
            cancellationToken.ThrowIfCancellationRequested();
            if (tail)
            {
                var result = output.Buffer.Tail(lines, category);
                return new(SerializedResult(JsonSerializer.Serialize(new
                {
                    kind = "output_tail_success",
                    totalLines = result.TotalLines,
                    returnedLines = result.ReturnedLines,
                    output = result.Output,
                }), isError: false));
            }

            var snapshot = output.Buffer.Snapshot(clear, category);
            if (LegacyCompat && clear && string.IsNullOrEmpty(category) && snapshot.Length != 0)
                _legacyResources.SignalSession(sessionId, "output");
            return new(SerializedResult(JsonSerializer.Serialize(new { kind = "output_success", output = snapshot }), isError: false));
        }

        private ValueTask<CallToolResult> SearchOutputAsync(CallToolRequestParams request, CancellationToken cancellationToken)
        {
            if (!TryReadOutputSearchArguments(request.Arguments, out var sessionId, out var pattern, out var contextLines, out var category))
            {
                return new(InvalidArguments(SearchOutput));
            }

            OutputLifetime output;
            lock (_lifetimeGate)
            {
                if (!_outputs.TryGetValue(sessionId, out output!))
                {
                    return new(NotFound());
                }
            }

            cancellationToken.ThrowIfCancellationRequested();
            var snapshot = output.Buffer.Snapshot(clear: false, category);
            var result = DapOutputSearch.Search(snapshot, pattern, contextLines, cancellationToken);
            return new(result.Status switch
            {
                DapOutputSearchStatus.Success => SerializedResult(result.SerializedContent!, isError: false),
                DapOutputSearchStatus.InvalidPattern => Error("output_search_invalid_pattern", "OUTPUT_SEARCH_INVALID_PATTERN"),
                DapOutputSearchStatus.Timeout => Error("output_search_timeout", "OUTPUT_SEARCH_TIMEOUT"),
                DapOutputSearchStatus.TooLarge => Error("output_search_too_large", "OUTPUT_SEARCH_TOO_LARGE"),
                _ => throw new InvalidOperationException("Unexpected output search status."),
            });
        }

        private async ValueTask<CallToolResult> GetRuntimeInventoryAsync(
            CallToolRequestParams request,
            bool modules,
            CancellationToken cancellationToken)
        {
            var tool = modules ? GetModules : GetProgress;
            if (!TryReadSessionId(request.Arguments, out var sessionId, out var hasSessionId) || !hasSessionId)
            {
                return InvalidArguments(tool);
            }
            if (!_sessions.TryGetValue(sessionId!, out var session) || !_slots.TryGetValue(sessionId!, out var slot))
            {
                return NotFound();
            }
            var lease = slot.TryAcquire();
            if (lease is null) return NotFound();

            var closeAfterLease = false;
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }
                if (lease.AbortToken.IsCancellationRequested) return NotFound();

                var snapshot = session.GetRuntimeInventory(modules, MaximumSerializedInspectionBytes);
                return snapshot.Status switch
                {
                    DapRuntimeInventoryStatus.Success => SerializedResult(snapshot.SerializedContent!, isError: false),
                    DapRuntimeInventoryStatus.Incomplete => Result(new
                    {
                        kind = "runtime_inventory_incomplete",
                        error = "RUNTIME_INVENTORY_INCOMPLETE",
                        tool,
                    }, isError: true),
                    DapRuntimeInventoryStatus.TooLarge => Result(new
                    {
                        kind = "runtime_inventory_too_large",
                        error = "RUNTIME_INVENTORY_TOO_LARGE",
                        tool,
                    }, isError: true),
                    _ => throw new InvalidOperationException("Unknown runtime inventory status."),
                };
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

        private async ValueTask<CallToolResult> GetLoadedSourcesAsync(CallToolRequestParams request, CancellationToken cancellationToken)
        {
            if (!TryReadSessionId(request.Arguments, out var sessionId, out var hasSessionId) || !hasSessionId)
            {
                return InvalidArguments(GetLoadedSources);
            }
            if (!_sessions.TryGetValue(sessionId!, out var session) || !_slots.TryGetValue(sessionId!, out var slot))
            {
                return NotFound();
            }
            var lease = slot.TryAcquire();
            if (lease is null) return NotFound();

            var closeAfterLease = false;
            try
            {
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }

                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var result = await session.GetLoadedSourcesAsync(operation.Token).ConfigureAwait(false);
                if (result.Outcome == DapLoadedSourcesOutcome.Unsupported)
                    return Error("dap_loaded_sources_unsupported", "DAP_LOADED_SOURCES_UNSUPPORTED");
                if (result.Outcome == DapLoadedSourcesOutcome.Refused)
                    return Error("dap_loaded_sources_refused", "DAP_LOADED_SOURCES_REFUSED");
                if (result.Outcome != DapLoadedSourcesOutcome.Success || result.Sources is null)
                    throw new InvalidOperationException("Unexpected loadedSources outcome.");

                var serialized = JsonSerializer.Serialize(new { kind = "loaded_sources_success", sources = result.Sources, count = result.Sources.Count });
                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes)
                {
                    closeAfterLease = true;
                    return Error("dap_loaded_sources_protocol_error", "DAP_LOADED_SOURCES_PROTOCOL_ERROR");
                }
                return SerializedResult(serialized, isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresLoadedSourcesDrain();
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return Error("dap_loaded_sources_protocol_error", "DAP_LOADED_SOURCES_PROTOCOL_ERROR");
            }
            finally
            {
                lease.Dispose();
                if (closeAfterLease)
                {
                    try { await slot.CloseAndDrainAsync().ConfigureAwait(false); }
                    catch (Exception) { }
                }
            }
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
                var callStack = await session.GetCallStackForOptionalThreadAsync(
                    arguments.ThreadId,
                    arguments.StartFrame,
                    arguments.Levels,
                    arguments.IncludeInspectionContext,
                    operation.Token).ConfigureAwait(false);
                if (callStack.IsRefused)
                {
                    return Error("dap_stack_trace_refused", "DAP_STACK_TRACE_REFUSED");
                }

                var serialized = JsonSerializer.Serialize(new CallStackSuccessContent(
                    "call_stack_success",
                    callStack.Frames,
                    callStack.TotalFrames,
                    callStack.InspectionContextId));
                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedCallStackBytes)
                {
                    closeAfterLease = true;
                    return Error("dap_stack_trace_protocol_error", "DAP_STACK_TRACE_PROTOCOL_ERROR");
                }

                return SerializedResult(serialized, isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresInspectionDrain();
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

        private async ValueTask<CallToolResult> GetCompositeContextAsync(CallToolRequestParams request, bool exceptionContext, CancellationToken cancellationToken)
        {
            var tool = exceptionContext ? GetExceptionContext : GetStopContext;
            if (!TryReadCompositeArguments(request.Arguments, exceptionContext, out var args))
                return InvalidArguments(tool);
            if (!_sessions.TryGetValue(args.SessionId, out var session) || !_slots.TryGetValue(args.SessionId, out var slot))
                return NotFound();
            var lease = slot.TryAcquire();
            if (lease is null) return NotFound();
            var closeAfterLease = false;
            var evaluating = false;
            try
            {
                if (!_isUsable(session)) { closeAfterLease = true; return NotFound(); }
                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var captured = session.CaptureStoppedContext();
                if (captured is null) return Error("debug_context_unavailable", "DEBUG_CONTEXT_UNAVAILABLE");
                var (state, context, description, text) = captured.Value;
                var threadId = state.CurrentThreadId!.Value;
                CallToolResult InspectionRefusal() => session.IsStoppedContextCurrent(threadId, context)
                    ? Error("debug_context_refused", "DEBUG_CONTEXT_REFUSED")
                    : Error("debug_context_stale", "DEBUG_CONTEXT_STALE");
                if (exceptionContext && state.StopReason != "exception")
                    return Error("debug_context_not_exception", "DEBUG_CONTEXT_NOT_EXCEPTION");
                var frameData = new List<object>();
                var inner = new List<object>();
                var content = new Dictionary<string, object?>
                {
                    ["kind"] = exceptionContext ? "exception_context_success" : "stop_context_success",
                    ["state"] = "stopped",
                    ["reason"] = state.StopReason,
                    ["threadId"] = threadId,
                    ["inspectionContextId"] = context,
                    ["frames"] = frameData,
                };
                if (description is not null) content["description"] = description;
                if (text is not null) content["text"] = text;
                if (exceptionContext) content["innerExceptions"] = inner;
                else
                {
                    if (args.VariableFrames > 0) content["locals"] = Array.Empty<DapVariable>();
                    if (args.OutputEntries > 0) content["recentOutput"] = new List<object>();
                }
                if (state.StopReason == "exception") content["exception"] = new Dictionary<string, object?>();
                using var budget = new CompositeByteCounter(content);
                if (!exceptionContext && args.OutputEntries > 0)
                {
                    var output = (List<object>)content["recentOutput"]!;
                    foreach (var entry in session.OutputBuffer.RecentEntries(args.OutputEntries))
                    {
                        if (entry.Text.AsSpan().TrimEnd().Length > MaximumSerializedInspectionBytes)
                            throw new CompositeTooLargeException();
                        var item = new Dictionary<string, object?> { ["text"] = entry.Text.TrimEnd(), ["category"] = entry.Category };
                        budget.Append(item, output.Count == 0);
                        output.Add(item);
                    }
                }
                var stack = await session.GetCallStackAsync(threadId, 0, (uint)args.MaxFrames, true, operation.Token).ConfigureAwait(false);
                if (stack.IsRefused) return InspectionRefusal();
                if (stack.InspectionContextId != context) return Error("debug_context_stale", "DEBUG_CONTEXT_STALE");
                var frames = stack.Frames.Take(args.MaxFrames).ToArray();
                if (exceptionContext)
                {
                    budget.AddProperty("totalFrames", stack.TotalFrames);
                    content["totalFrames"] = stack.TotalFrames;
                }
                for (var index = 0; index < frames.Length; index++)
                {
                    var frame = frames[index];
                    object item = exceptionContext
                        ? new Dictionary<string, object?>
                        {
                            ["index"] = index,
                            ["id"] = frame.Id,
                            ["name"] = frame.Name,
                            ["source"] = frame.Source,
                            ["line"] = frame.Line,
                            ["column"] = frame.Column,
                            ["locals"] = index < args.VariableFrames ? Array.Empty<DapVariable>() : null
                        }
                        : frame;
                    budget.Append(item, frameData.Count == 0);
                    frameData.Add(item);
                }
                for (var index = 0; index < frames.Length; index++)
                {
                    IReadOnlyList<DapVariable>? variables = null;
                    if (index < args.VariableFrames)
                    {
                        var scopes = await session.GetScopesAsync(context, frames[index].Id, operation.Token).ConfigureAwait(false);
                        if (scopes.IsRefused) return InspectionRefusal();
                        var scope = scopes.Scopes.FirstOrDefault(static item => item.VariablesReference > 0);
                        if (scope is null) variables = Array.Empty<DapVariable>();
                        else
                        {
                            var result = await session.GetVariablesAsync(context, scope.VariablesReference, 0, 20, operation.Token).ConfigureAwait(false);
                            if (result.IsRefused) return InspectionRefusal();
                            variables = result.Variables;
                        }
                    }
                    if (index < args.VariableFrames)
                    {
                        var observed = variables ?? Array.Empty<DapVariable>();
                        budget.Replace(2, observed);
                        if (!exceptionContext) content["locals"] = observed;
                        else ((Dictionary<string, object?>)frameData[index])["locals"] = observed;
                    }
                }
                if (state.StopReason == "exception")
                {
                    var info = await session.GetExceptionInfoAsync(threadId, operation.Token).ConfigureAwait(false);
                    if (info.Outcome == DapExceptionInfoOutcome.Unsupported)
                        return Error("dap_exception_info_unsupported", "DAP_EXCEPTION_INFO_UNSUPPORTED");
                    if (info.Outcome != DapExceptionInfoOutcome.Success) return InspectionRefusal();
                    budget.Replace(2, info.Body);
                    content["exception"] = info.Body;
                }
                if (exceptionContext && args.InnerExceptions > 0 && frames.Length == 0)
                    return Error("debug_context_unavailable", "DEBUG_CONTEXT_UNAVAILABLE");
                if (exceptionContext && args.InnerExceptions > 0 && frames.Length > 0)
                {
                    var prefix = "$exception.InnerException";
                    for (var depth = 1; depth <= args.InnerExceptions; depth++)
                    {
                        evaluating = true;
                        var current = await session.EvaluateExpressionAsync(context, frames[0].Id, prefix, operation.Token).ConfigureAwait(false);
                        if (current.Outcome != DapEvaluateOutcome.Success)
                        {
                            closeAfterLease = current.Outcome == DapEvaluateOutcome.OutcomeUnknown;
                            return closeAfterLease ? Error("debug_context_evaluation_unknown", "DEBUG_CONTEXT_EVALUATION_UNKNOWN")
                                : Error("debug_context_evaluation_failed", "DEBUG_CONTEXT_EVALUATION_FAILED");
                        }
                        if (string.Equals(current.Result, "null", StringComparison.Ordinal)) break;
                        var type = await session.EvaluateExpressionAsync(context, frames[0].Id, prefix + ".GetType().FullName", operation.Token).ConfigureAwait(false);
                        if (type.Outcome != DapEvaluateOutcome.Success)
                        {
                            closeAfterLease = type.Outcome == DapEvaluateOutcome.OutcomeUnknown;
                            return closeAfterLease ? Error("debug_context_evaluation_unknown", "DEBUG_CONTEXT_EVALUATION_UNKNOWN")
                                : Error("debug_context_evaluation_failed", "DEBUG_CONTEXT_EVALUATION_FAILED");
                        }
                        var innerEntry = new Dictionary<string, object?> { ["depth"] = depth, ["type"] = type.Result, ["message"] = string.Empty };
                        budget.Append(innerEntry, inner.Count == 0);
                        inner.Add(innerEntry);
                        var message = await session.EvaluateExpressionAsync(context, frames[0].Id, prefix + ".Message", operation.Token).ConfigureAwait(false);
                        if (message.Outcome != DapEvaluateOutcome.Success)
                        {
                            closeAfterLease = message.Outcome == DapEvaluateOutcome.OutcomeUnknown;
                            return closeAfterLease ? Error("debug_context_evaluation_unknown", "DEBUG_CONTEXT_EVALUATION_UNKNOWN")
                                : Error("debug_context_evaluation_failed", "DEBUG_CONTEXT_EVALUATION_FAILED");
                        }
                        budget.Replace(2, message.Result);
                        innerEntry["message"] = message.Result;
                        prefix += ".InnerException";
                    }
                    evaluating = false;
                }
                var serialized = JsonSerializer.Serialize(content);
                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes)
                    return Error("debug_context_too_large", "DEBUG_CONTEXT_TOO_LARGE");
                if (!session.IsStoppedContextCurrent(threadId, context))
                    return Error("debug_context_stale", "DEBUG_CONTEXT_STALE");
                return SerializedResult(serialized, false);
            }
            catch (CompositeTooLargeException)
            {
                return Error("debug_context_too_large", "DEBUG_CONTEXT_TOO_LARGE");
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresInspectionDrain();
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return evaluating ? Error("debug_context_evaluation_unknown", "DEBUG_CONTEXT_EVALUATION_UNKNOWN")
                    : Error("debug_context_protocol_error", "DEBUG_CONTEXT_PROTOCOL_ERROR");
            }
            finally
            {
                lease.Dispose();
                if (closeAfterLease) { try { await slot.CloseAndDrainAsync().ConfigureAwait(false); } catch (Exception) { } }
            }
        }

        private sealed class CompositeTooLargeException : Exception { }

        // Incrementally account for added or replaced JSON values; existing values are never revisited.
        // Utf8JsonWriter uses the same default encoder as the final serializer, in bounded chunks.
        private sealed class CompositeByteCounter : IDisposable
        {
            private readonly System.Buffers.ArrayBufferWriter<byte> _scratch = new(4096);
            private readonly Utf8JsonWriter _writer;
            private int _bytes;
            private int _limit;
            private int _total;

            internal CompositeByteCounter(Dictionary<string, object?> content)
            {
                _writer = new Utf8JsonWriter(_scratch);
                _limit = MaximumSerializedInspectionBytes;
                try
                {
                    Value(content);
                    _total = _bytes;
                }
                catch
                {
                    _writer.Dispose();
                    throw;
                }
            }

            internal void Append(object? value, bool first)
            {
                _bytes = 0;
                _limit = MaximumSerializedInspectionBytes - _total;
                if (!first) Add(1);
                Value(value);
                _total += _bytes;
            }

            internal void AddProperty(string name, object? value)
            {
                _bytes = 0;
                _limit = MaximumSerializedInspectionBytes - _total;
                var first = false;
                Property(name, value, ref first);
                _total += _bytes;
            }

            internal void Replace(int previousSize, object? value)
            {
                _bytes = 0;
                _limit = MaximumSerializedInspectionBytes - _total + previousSize;
                Value(value);
                _total += _bytes - previousSize;
            }

            public void Dispose() => _writer.Dispose();

            private void Add(int count)
            {
                if (count > _limit - _bytes) throw new CompositeTooLargeException();
                _bytes += count;
            }

            private void String(string value)
            {
                Add(2);
                if (value.Length > _limit - _bytes) throw new CompositeTooLargeException();
                for (var offset = 0; offset < value.Length;)
                {
                    var length = Math.Min(512, value.Length - offset);
                    if (offset + length < value.Length && char.IsHighSurrogate(value[offset + length - 1])
                        && char.IsLowSurrogate(value[offset + length])) length--;
                    _scratch.Clear();
                    _writer.Reset(_scratch);
                    _writer.WriteStringValue(value.AsSpan(offset, length));
                    _writer.Flush();
                    Add(_scratch.WrittenCount - 2);
                    offset += length;
                }
            }

            private void Property(string name, object? value, ref bool first)
            {
                if (!first) Add(1);
                first = false;
                String(name);
                Add(1);
                Value(value);
            }

            internal void Value(object? value)
            {
                switch (value)
                {
                    case null: Add(4); return;
                    case string text: String(text); return;
                    case bool flag: Add(flag ? 4 : 5); return;
                    case int or uint or long:
                        Add(Convert.ToString(value, System.Globalization.CultureInfo.InvariantCulture)!.Length); return;
                    case DapStackFrame frame:
                        {
                            Add(2); var first = true;
                            Property("id", frame.Id, ref first); Property("name", frame.Name, ref first);
                            Property("source", frame.Source, ref first); Property("line", frame.Line, ref first);
                            Property("column", frame.Column, ref first); return;
                        }
                    case DapVariable variable:
                        {
                            Add(2); var first = true;
                            Property("name", variable.Name, ref first); Property("value", variable.Value, ref first);
                            Property("type", variable.Type, ref first); Property("variablesReference", variable.VariablesReference, ref first); return;
                        }
                    case JsonElement element: Element(element); return;
                    case IDictionary<string, object?> properties:
                        {
                            Add(2); var first = true;
                            foreach (var pair in properties) Property(pair.Key, pair.Value, ref first);
                            return;
                        }
                    case System.Collections.IEnumerable items:
                        {
                            Add(2); var first = true;
                            foreach (var item in items) { if (!first) Add(1); first = false; Value(item); }
                            return;
                        }
                    default: throw new InvalidOperationException("Unsupported composite context value.");
                }
            }

            private void Element(JsonElement element)
            {
                switch (element.ValueKind)
                {
                    case JsonValueKind.Object:
                        {
                            Add(2); var first = true;
                            foreach (var property in element.EnumerateObject()) Property(property.Name, property.Value, ref first);
                            return;
                        }
                    case JsonValueKind.Array:
                        {
                            Add(2); var first = true;
                            foreach (var item in element.EnumerateArray()) { if (!first) Add(1); first = false; Element(item); }
                            return;
                        }
                    case JsonValueKind.String: String(element.GetString()!); return;
                    case JsonValueKind.Number: Add(element.GetRawText().Length); return;
                    case JsonValueKind.True: Add(4); return;
                    case JsonValueKind.False: Add(5); return;
                    case JsonValueKind.Null: Add(4); return;
                    default: throw new InvalidOperationException("Unsupported exception body value.");
                }
            }
        }

        private async ValueTask<CallToolResult> GetExceptionInfoAsync(CallToolRequestParams request, CancellationToken cancellationToken)
        {
            if (!TryReadContinueArguments(request.Arguments, out var arguments))
            {
                return InvalidArguments(GetExceptionInfo);
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
                var result = await session.GetExceptionInfoForOptionalThreadAsync(arguments.ThreadId, operation.Token).ConfigureAwait(false);
                switch (result.Outcome)
                {
                    case DapExceptionInfoOutcome.Unsupported:
                        return Error("dap_exception_info_unsupported", "DAP_EXCEPTION_INFO_UNSUPPORTED");
                    case DapExceptionInfoOutcome.Refused:
                        return Error("dap_exception_info_refused", "DAP_EXCEPTION_INFO_REFUSED");
                }

                var serialized = JsonSerializer.Serialize(new ExceptionInfoSuccessContent("exception_info_success", result.Body));
                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes)
                {
                    closeAfterLease = true;
                    return ExceptionInfoProtocolError();
                }

                return SerializedResult(serialized, isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresInspectionDrain();
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return ExceptionInfoProtocolError();
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

        private static CallToolResult ExceptionInfoProtocolError() =>
            Error("dap_exception_info_protocol_error", "DAP_EXCEPTION_INFO_PROTOCOL_ERROR");

        private async ValueTask<CallToolResult> GetStepInTargetsAsync(CallToolRequestParams request, CancellationToken cancellationToken)
        {
            if (!TryReadStepTargetArguments(request.Arguments, out var args)) return InvalidArguments(GetStepInTargets);
            if (!_sessions.TryGetValue(args.SessionId, out var session) || !_slots.TryGetValue(args.SessionId, out var slot)) return NotFound();
            var lease = slot.TryAcquire();
            if (lease is null) return NotFound();
            var close = false;
            try
            {
                if (!_isUsable(session)) { close = true; return NotFound(); }
                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var result = await session.GetStepInTargetsAsync(args.InspectionContextId, args.Id, operation.Token).ConfigureAwait(false);
                if (result.Outcome == DapStepTargetsOutcome.Unsupported) return Error("dap_step_targets_unsupported", "DAP_STEP_TARGETS_UNSUPPORTED");
                if (result.Outcome == DapStepTargetsOutcome.Refused) return Error("dap_step_targets_refused", "DAP_STEP_TARGETS_REFUSED");
                var serialized = JsonSerializer.Serialize(new { kind = "step_in_targets_success", inspectionContextId = args.InspectionContextId, targets = result.Targets });
                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes)
                { close = true; return Error("dap_step_targets_protocol_error", "DAP_STEP_TARGETS_PROTOCOL_ERROR"); }
                return SerializedResult(serialized, false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            { close = session.RequiresInspectionDrain(); throw; }
            catch (Exception) { close = true; return Error("dap_step_targets_protocol_error", "DAP_STEP_TARGETS_PROTOCOL_ERROR"); }
            finally
            {
                lease.Dispose();
                if (close) { try { await slot.CloseAndDrainAsync().ConfigureAwait(false); } catch (Exception) { } }
            }
        }

        private ValueTask<CallToolResult> GetScopesAsync(CallToolRequestParams request, CancellationToken cancellationToken) =>
            GetInspectionAsync(request, scopes: true, cancellationToken);

        private ValueTask<CallToolResult> GetVariablesAsync(CallToolRequestParams request, CancellationToken cancellationToken) =>
            GetInspectionAsync(request, scopes: false, cancellationToken);

        private async ValueTask<CallToolResult> GetInspectionAsync(
            CallToolRequestParams request,
            bool scopes,
            CancellationToken cancellationToken)
        {
            var tool = scopes ? GetScopes : GetVariables;
            if (!TryReadInspectionArguments(request.Arguments, scopes, out var arguments))
            {
                return InvalidArguments(tool);
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
                string serialized;
                if (scopes)
                {
                    var result = await session.GetScopesAsync(arguments.InspectionContextId, arguments.Id, operation.Token).ConfigureAwait(false);
                    if (result.IsRefused)
                    {
                        return Error("dap_scopes_refused", "DAP_SCOPES_REFUSED");
                    }

                    serialized = JsonSerializer.Serialize(new ScopesSuccessContent("scopes_success", result.InspectionContextId!, result.Scopes));
                }
                else
                {
                    var result = await session.GetVariablesAsync(arguments.InspectionContextId, arguments.Id,
                        arguments.Start, arguments.Count, operation.Token).ConfigureAwait(false);
                    if (result.IsRefused)
                    {
                        return Error("dap_variables_refused", "DAP_VARIABLES_REFUSED");
                    }

                    serialized = JsonSerializer.Serialize(new VariablesSuccessContent("variables_success", result.InspectionContextId!, result.Variables));
                }

                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes)
                {
                    closeAfterLease = true;
                    return InspectionProtocolError(scopes);
                }

                return SerializedResult(serialized, isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresInspectionDrain();
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return InspectionProtocolError(scopes);
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

        private async ValueTask<CallToolResult> EvaluateExpressionAsync(CallToolRequestParams request, CancellationToken cancellationToken)
        {
            if (!TryReadEvaluateArguments(request.Arguments, out var arguments))
            {
                return InvalidArguments(EvaluateExpression);
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
                var result = await session.EvaluateExpressionAsync(arguments.InspectionContextId, arguments.FrameId,
                    arguments.Expression, operation.Token).ConfigureAwait(false);
                switch (result.Outcome)
                {
                    case DapEvaluateOutcome.Refused:
                        return Error("dap_evaluate_refused", "DAP_EVALUATE_REFUSED");
                    case DapEvaluateOutcome.Failed:
                        return Error("dap_evaluate_failed", "DAP_EVALUATE_FAILED");
                    case DapEvaluateOutcome.OutcomeUnknown:
                        closeAfterLease = true;
                        return EvaluateOutcomeUnknown();
                }

                var serialized = JsonSerializer.Serialize(new EvaluateSuccessContent(
                    "evaluate_success", result.InspectionContextId!, result.Result!, result.Type, result.VariablesReference));
                if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes)
                {
                    closeAfterLease = true;
                    return EvaluateOutcomeUnknown();
                }
                return SerializedResult(serialized, isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresInspectionDrain();
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return EvaluateOutcomeUnknown();
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

        private async ValueTask<CallToolResult> SetVariableAsync(CallToolRequestParams request, CancellationToken cancellationToken)
        {
            if (!TryReadSetVariableArguments(request.Arguments, out var arguments))
            {
                return InvalidArguments(SetVariable);
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
                var result = await session.SetVariableAsync(arguments.InspectionContextId, arguments.VariablesReference,
                    arguments.Name, arguments.Value, operation.Token).ConfigureAwait(false);
                switch (result.Outcome)
                {
                    case DapSetVariableOutcome.Unsupported:
                        return Error("dap_set_variable_unsupported", "DAP_SET_VARIABLE_UNSUPPORTED");
                    case DapSetVariableOutcome.Refused:
                        return Error("dap_set_variable_refused", "DAP_SET_VARIABLE_REFUSED");
                    case DapSetVariableOutcome.Failed:
                        return Error("dap_set_variable_failed", "DAP_SET_VARIABLE_FAILED");
                    case DapSetVariableOutcome.OutcomeUnknown:
                        closeAfterLease = true;
                        return SetVariableOutcomeUnknown();
                }
                return SerializedResult(result.SerializedContent!, isError: false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresInspectionDrain();
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return SetVariableOutcomeUnknown();
            }
            finally
            {
                lease.Dispose();
                if (closeAfterLease)
                {
                    try { await slot.CloseAndDrainAsync().ConfigureAwait(false); }
                    catch (Exception) { }
                }
            }
        }

        private static CallToolResult SetVariableOutcomeUnknown() =>
            Error("dap_set_variable_outcome_unknown", "DAP_SET_VARIABLE_OUTCOME_UNKNOWN");

        private static CallToolResult EvaluateOutcomeUnknown() =>
            Error("dap_evaluate_outcome_unknown", "DAP_EVALUATE_OUTCOME_UNKNOWN");

        private static CallToolResult InspectionProtocolError(bool scopes) => scopes
            ? Error("dap_scopes_protocol_error", "DAP_SCOPES_PROTOCOL_ERROR")
            : Error("dap_variables_protocol_error", "DAP_VARIABLES_PROTOCOL_ERROR");

        private ValueTask<CallToolResult> StepOverAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken) =>
            StepAsync(request, requestId, DapStepKind.Over, cancellationToken);

        private ValueTask<CallToolResult> StepIntoAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken) =>
            StepAsync(request, requestId, DapStepKind.Into, cancellationToken);

        private ValueTask<CallToolResult> StepOutAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken) =>
            StepAsync(request, requestId, DapStepKind.Out, cancellationToken);

        private async ValueTask<CallToolResult> StepAsync(
            CallToolRequestParams request,
            RequestId requestId,
            DapStepKind kind,
            CancellationToken cancellationToken)
        {
            var tool = kind switch
            {
                DapStepKind.Over => StepOver,
                DapStepKind.Into => StepInto,
                DapStepKind.Out => StepOut,
                _ => throw new ArgumentOutOfRangeException(nameof(kind)),
            };
            if (!TryReadTargetedStepArguments(request.Arguments, kind == DapStepKind.Into, out var arguments))
            {
                return InvalidArguments(tool);
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
            using var callerCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            using var operation = CancellationTokenSource.CreateLinkedTokenSource(callerCancellation.Token, lease.AbortToken);
            _stepOverRequests.TryAdd(requestId, callerCancellation);
            try
            {
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }

                var stepOver = arguments.TargetId is { } target
                    ? await session.StepIntoTargetAsync(arguments.ThreadId, target, arguments.InspectionContextId!, operation.Token).ConfigureAwait(false)
                    : await session.StepForOptionalThreadAsync(arguments.ThreadId, kind, operation.Token).ConfigureAwait(false);
                if (stepOver.Outcome == DapStepOverOutcome.Unsupported)
                    return Error("dap_step_targets_unsupported", "DAP_STEP_TARGETS_UNSUPPORTED");
                if (stepOver.Outcome == DapStepOverOutcome.Refused)
                {
                    return kind switch
                    {
                        DapStepKind.Into => Error("dap_step_into_refused", "DAP_STEP_INTO_REFUSED"),
                        DapStepKind.Out => Error("dap_step_out_refused", "DAP_STEP_OUT_REFUSED"),
                        _ => Error("dap_step_over_refused", "DAP_STEP_OVER_REFUSED"),
                    };
                }

                if (stepOver.Outcome == DapStepOverOutcome.Succeeded)
                {
                    return Result(new StepOverSuccessContent(kind switch
                    {
                        DapStepKind.Into => "step_into_success",
                        DapStepKind.Out => "step_out_success",
                        _ => "step_over_success",
                    }, stepOver.ThreadId), isError: false);
                }

                closeAfterLease = true;
                if (stepOver.Outcome == DapStepOverOutcome.Terminal)
                {
                    return Result(new StepOverTerminalContent(kind switch
                    {
                        DapStepKind.Into => "step_into_terminal",
                        DapStepKind.Out => "step_out_terminal",
                        _ => "step_over_terminal",
                    }, stepOver.Terminal!, stepOver.ExitCode), isError: false);
                }

                if (stepOver.Outcome == DapStepOverOutcome.TimedOut)
                {
                    return Result(new StepOverTimedOutContent(kind switch
                    {
                        DapStepKind.Into => "step_into_timed_out",
                        DapStepKind.Out => "step_out_timed_out",
                        _ => "step_over_timed_out",
                    }, stepOver.ThreadId, 30), isError: false);
                }

                throw new InvalidOperationException("Unknown step outcome.");
            }
            catch (OperationCanceledException) when (callerCancellation.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresStepOverDrain();
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return kind switch
                {
                    DapStepKind.Into => Error("dap_step_into_protocol_error", "DAP_STEP_INTO_PROTOCOL_ERROR"),
                    DapStepKind.Out => Error("dap_step_out_protocol_error", "DAP_STEP_OUT_PROTOCOL_ERROR"),
                    _ => Error("dap_step_over_protocol_error", "DAP_STEP_OVER_PROTOCOL_ERROR"),
                };
            }
            finally
            {
                _stepOverRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, callerCancellation));
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

        private async ValueTask<CallToolResult> ContinueAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken)
        {
            if (!TryReadContinueArguments(request.Arguments, out var arguments))
            {
                return InvalidArguments(ContinueExecution);
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
            using var callerCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            using var operation = CancellationTokenSource.CreateLinkedTokenSource(callerCancellation.Token, lease.AbortToken);
            _stepOverRequests.TryAdd(requestId, callerCancellation);
            try
            {
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }

                var continued = await session.ContinueForOptionalThreadAsync(arguments.ThreadId, operation.Token).ConfigureAwait(false);
                closeAfterLease = continued.RequiresDrain;
                return continued.Outcome switch
                {
                    DapContinueOutcome.Refused => Error("dap_continue_refused", "DAP_CONTINUE_REFUSED"),
                    DapContinueOutcome.Stopped => Result(new ContinueStoppedContent(
                        "continue_stopped", continued.Reason!, continued.ThreadId, continued.AllThreadsStopped), isError: false),
                    DapContinueOutcome.Terminal => Result(new ContinueTerminalContent(
                        "continue_terminal", continued.Terminal!, continued.ExitCode), isError: false),
                    DapContinueOutcome.TimedOut => Result(new ContinueTimedOutContent(
                        "continue_timed_out", continued.ThreadId!.Value, 30, continued.Acknowledged), isError: false),
                    _ => throw new InvalidOperationException("Unknown continue outcome."),
                };
            }
            catch (DapContinueCanceledException exception) when (callerCancellation.IsCancellationRequested)
            {
                closeAfterLease = exception.RequiresDrain;
                throw;
            }
            catch (OperationCanceledException) when (callerCancellation.IsCancellationRequested)
            {
                throw;
            }
            catch (OperationCanceledException) when (lease.AbortToken.IsCancellationRequested)
            {
                // The owner has retired this session; this is not a DAP protocol failure.
                return NotFound();
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return Error("dap_continue_protocol_error", "DAP_CONTINUE_PROTOCOL_ERROR");
            }
            finally
            {
                _stepOverRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, callerCancellation));
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

        private async ValueTask<CallToolResult> PauseAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken)
        {
            if (!TryReadPauseArguments(request.Arguments, out var arguments))
            {
                return InvalidArguments(PauseExecution);
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
            using var callerCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            using var operation = CancellationTokenSource.CreateLinkedTokenSource(callerCancellation.Token, lease.AbortToken);
            _stepOverRequests.TryAdd(requestId, callerCancellation);
            try
            {
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }

                var paused = await session.PauseAsync(arguments.ThreadId, operation.Token).ConfigureAwait(false);
                closeAfterLease = paused.RequiresDrain;
                return paused.Outcome switch
                {
                    DapPauseOutcome.Acknowledged => Result(new PauseAcknowledgedContent(
                        "pause_acknowledged", paused.ThreadId!.Value), isError: false),
                    DapPauseOutcome.Terminal => Result(new PauseTerminalContent(
                        "pause_terminal", paused.Terminal!, paused.ExitCode), isError: false),
                    DapPauseOutcome.Refused => Error("dap_pause_refused", "DAP_PAUSE_REFUSED"),
                    DapPauseOutcome.NoThreads => Error("dap_pause_no_threads", "DAP_PAUSE_NO_THREADS"),
                    DapPauseOutcome.TimedOut => Error("dap_pause_timeout", "DAP_PAUSE_TIMEOUT"),
                    DapPauseOutcome.ProtocolError => Error("dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR"),
                    _ => throw new InvalidOperationException("Unknown pause outcome."),
                };
            }
            catch (DapPauseCanceledException exception)
            {
                closeAfterLease = exception.RequiresDrain;
                if (!callerCancellation.IsCancellationRequested && lease.AbortToken.IsCancellationRequested)
                {
                    return NotFound();
                }
                throw;
            }
            catch (OperationCanceledException) when (callerCancellation.IsCancellationRequested)
            {
                throw;
            }
            catch (OperationCanceledException) when (lease.AbortToken.IsCancellationRequested)
            {
                return NotFound();
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return Error("dap_pause_protocol_error", "DAP_PAUSE_PROTOCOL_ERROR");
            }
            finally
            {
                _stepOverRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, callerCancellation));
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

        private async ValueTask<CallToolResult> ConfigureExceptionsAsync(
            CallToolRequestParams request, RequestId requestId, CancellationToken cancellationToken)
        {
            if (!TryReadExceptionFilterArguments(request.Arguments, out var arguments))
            {
                return InvalidArguments(ConfigureExceptions);
            }
            if (!_sessions.TryGetValue(arguments.SessionId, out var session)
                || !_slots.TryGetValue(arguments.SessionId, out var slot))
            {
                return NotFound();
            }
            var lease = slot.TryAcquire();
            if (lease is null)
            {
                return NotFound();
            }
            var closeAfterLease = false;
            using var callerCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            using var operation = CancellationTokenSource.CreateLinkedTokenSource(callerCancellation.Token, lease.AbortToken);
            _stepOverRequests.TryAdd(requestId, callerCancellation);
            try
            {
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }
                var updated = await session.ConfigureExceptionsAsync(arguments.Filters, operation.Token).ConfigureAwait(false);
                closeAfterLease = updated.RequiresDrain;
                return updated.Outcome switch
                {
                    DapBreakpointUpdateOutcome.Unsupported => Error("dap_exception_filters_unsupported", "DAP_EXCEPTION_FILTERS_UNSUPPORTED"),
                    DapBreakpointUpdateOutcome.Refused => Error("dap_exception_filters_refused", "DAP_EXCEPTION_FILTERS_REFUSED"),
                    DapBreakpointUpdateOutcome.Acknowledged => Result(new ExceptionFiltersAcknowledgedContent(
                        "exception_filters_acknowledged", arguments.Filters,
                        updated.ExceptionVerification?.Select(static verified => new ExceptionFilterVerification(verified)).ToArray()), isError: false),
                    _ => throw new InvalidOperationException("Unknown exception filter update outcome."),
                };
            }
            catch (DapBreakpointCanceledException exception) when (callerCancellation.IsCancellationRequested)
            {
                closeAfterLease = exception.RequiresDrain;
                throw;
            }
            catch (OperationCanceledException) when (callerCancellation.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return Error("dap_exception_filters_protocol_error", "DAP_EXCEPTION_FILTERS_PROTOCOL_ERROR");
            }
            finally
            {
                _stepOverRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, callerCancellation));
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

        private ValueTask<CallToolResult> AddBreakpointAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken) =>
            UpdateBreakpointAsync(request, requestId, cancellationToken, remove: false);

        private ValueTask<CallToolResult> RemoveBreakpointAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken) =>
            UpdateBreakpointAsync(request, requestId, cancellationToken, remove: true);

        private async ValueTask<CallToolResult> UpdateBreakpointAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken,
            bool remove,
            int[]? replacementLines = null)
        {
            if (!TryReadBreakpointArguments(request.Arguments, out var arguments))
            {
                return InvalidArguments(remove ? RemoveBreakpoint : AddBreakpoint);
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
            using var callerCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            using var operation = CancellationTokenSource.CreateLinkedTokenSource(callerCancellation.Token, lease.AbortToken);
            _stepOverRequests.TryAdd(requestId, callerCancellation);
            try
            {
                if (!_isUsable(session))
                {
                    closeAfterLease = true;
                    return NotFound();
                }

                if (!TryNormalizeBreakpointSource(arguments.File, out var file, requireExistingFile: !remove))
                {
                    return Error("breakpoint_source_unavailable", "BREAKPOINT_SOURCE_UNAVAILABLE");
                }

                var updated = replacementLines is not null
                    ? await session.ReplaceBreakpointsAsync(file, arguments.Line, remove, replacementLines, operation.Token).ConfigureAwait(false)
                    : remove
                        ? await session.RemoveBreakpointAsync(file, arguments.Line, operation.Token).ConfigureAwait(false)
                        : await session.AddBreakpointAsync(file, arguments.Line, operation.Token).ConfigureAwait(false);
                closeAfterLease = updated.RequiresDrain;
                return updated.Outcome switch
                {
                    DapBreakpointUpdateOutcome.Added => Result(new BreakpointAddedContent(
                        "breakpoint_added", file, arguments.Line, updated.Verified, updated.ActualLine), isError: false),
                    DapBreakpointUpdateOutcome.Removed or DapBreakpointUpdateOutcome.NotFound => Result(new BreakpointRemovedContent(
                        "breakpoint_removed", file, arguments.Line, updated.Outcome == DapBreakpointUpdateOutcome.Removed), isError: false),
                    DapBreakpointUpdateOutcome.Refused => Error("dap_set_breakpoints_refused", "DAP_SET_BREAKPOINTS_REFUSED"),
                    DapBreakpointUpdateOutcome.LimitReached => Error("breakpoint_limit_reached", "BREAKPOINT_LIMIT_REACHED"),
                    DapBreakpointUpdateOutcome.Unavailable => Error("breakpoint_session_unavailable", "BREAKPOINT_SESSION_UNAVAILABLE"),
                    _ => throw new InvalidOperationException("Unknown breakpoint update outcome."),
                };
            }
            catch (DapBreakpointCanceledException exception) when (callerCancellation.IsCancellationRequested)
            {
                closeAfterLease = exception.RequiresDrain;
                throw;
            }
            catch (OperationCanceledException) when (callerCancellation.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return Error("dap_set_breakpoints_protocol_error", "DAP_SET_BREAKPOINTS_PROTOCOL_ERROR");
            }
            finally
            {
                _stepOverRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, callerCancellation));
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

        private async ValueTask<CallToolResult> UiInvokeAsync(
            CallToolRequestParams request,
            RequestId requestId,
            CancellationToken cancellationToken)
        {
            if (request.Arguments is not { Count: >= 2 and <= 3 } arguments ||
                arguments.Keys.Any(key => key is not ("debugSessionId" or "automationId" or "expectedTarget")) ||
                !arguments.TryGetValue("debugSessionId", out var sessionValue) || sessionValue.ValueKind != JsonValueKind.String ||
                string.IsNullOrWhiteSpace(sessionValue.GetString()) ||
                !arguments.TryGetValue("automationId", out var automationIdValue) || automationIdValue.ValueKind != JsonValueKind.String ||
                automationIdValue.GetString() is not { } automationId || !IsValidUiInvokeAutomationId(automationId))
            {
                return InvalidArguments(UiInvoke);
            }
            JsonObject? expectedTarget = null;
            if (arguments.TryGetValue("expectedTarget", out var expectedValue) &&
                (!NativeUiDiscoveryContract.TryReadExpectation(expectedValue, out expectedTarget) ||
                 JsonSerializer.SerializeToUtf8Bytes(arguments).Length > NativeUiDiscoveryContract.MaximumRequestBytes))
                return InvalidArguments(UiInvoke);

            var sessionId = sessionValue.GetString()!;
            var binding = await ResolveNativeSceneBindingAsync(sessionId).ConfigureAwait(false);
            if (binding is null || !_sessions.TryGetValue(sessionId, out var session))
                return NotFound();

            using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            _uiInvokeRequests.TryAdd(requestId, cancellation);
            try
            {
                var payload = await binding.InvokeAsync(
                    automationId,
                    () => _sessions.TryGetValue(sessionId, out var current) && ReferenceEquals(current, session) &&
                          _nativeSceneBindings.TryGetValue(sessionId, out var currentBinding) && ReferenceEquals(currentBinding, binding) &&
                          _slots.ContainsKey(sessionId),
                    cancellation.Token, expectedTarget).ConfigureAwait(false);
                return Result(payload, isError: payload["kind"]?.GetValue<string>() != "ui_invoke_success");
            }
            finally
            {
                _uiInvokeRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, cancellation));
            }
        }

        private async ValueTask<CallToolResult> UiInvokeObservedAsync(
            CallToolRequestParams request, RequestId requestId, CancellationToken cancellationToken)
        {
            if (!TryReadObservedInvokeArguments(request.Arguments, out var sessionId, out var automationId, out var target))
                return InvalidArguments(UiInvokeObserved);

            var binding = await ResolveNativeSceneBindingAsync(sessionId).ConfigureAwait(false);
            if (binding is null || !_sessions.TryGetValue(sessionId, out var session))
                return NotFound();

            using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            _uiInvokeRequests.TryAdd(requestId, cancellation);
            try
            {
                var payload = await binding.InvokeAsync(
                    automationId,
                    () => _sessions.TryGetValue(sessionId, out var current) && ReferenceEquals(current, session) &&
                          _nativeSceneBindings.TryGetValue(sessionId, out var currentBinding) && ReferenceEquals(currentBinding, binding) &&
                          _slots.ContainsKey(sessionId),
                    cancellation.Token, target, observed: true).ConfigureAwait(false);
                return Result(payload, isError: NativeUiDiscoveryContract.Text(payload, "kind") != "ui_invoke_observed_success");
            }
            finally
            {
                _uiInvokeRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, cancellation));
            }
        }

        private static bool TryReadObservedInvokeArguments(
            IDictionary<string, JsonElement>? arguments, out string sessionId, out string automationId, out JsonObject target)
        {
            sessionId = automationId = string.Empty;
            target = null!;
            try
            {
                if (arguments is not { Count: 3 } ||
                    arguments.Keys.Any(key => key is not ("debugSessionId" or "target" or "expectedAutomationId")) ||
                    !arguments.TryGetValue("debugSessionId", out var sessionValue) || sessionValue.ValueKind != JsonValueKind.String ||
                    !IsValidObservedText(sessionValue.GetString(), NativeUiDiscoveryContract.MaximumRequestBytes) ||
                    !arguments.TryGetValue("expectedAutomationId", out var idValue) || idValue.ValueKind != JsonValueKind.String ||
                    !IsValidObservedText(idValue.GetString(), 256) ||
                    JsonSerializer.SerializeToUtf8Bytes(arguments).Length > NativeUiDiscoveryContract.MaximumRequestBytes ||
                    !arguments.TryGetValue("target", out var targetValue) ||
                    !NativeUiDiscoveryContract.TryReadExpectation(targetValue, out var expectation) || expectation is null ||
                    !IsValidObservedText(NativeUiDiscoveryContract.Text(expectation, "processIdentity"), NativeUiDiscoveryContract.MaximumRequestBytes) ||
                    !IsValidObservedText(NativeUiDiscoveryContract.Text(expectation, "hwnd"), NativeUiDiscoveryContract.MaximumRequestBytes))
                    return false;

                sessionId = sessionValue.GetString()!;
                automationId = idValue.GetString()!;
                target = expectation;
                return true;
            }
            catch (Exception exception) when (exception is ArgumentException or InvalidOperationException or JsonException)
            {
                return false;
            }
        }

        private static bool IsValidObservedText(string? text, int maximumCodePoints)
        {
            if (string.IsNullOrWhiteSpace(text) || text.Length > maximumCodePoints * 2)
                return false;
            var codePoints = 0;
            for (var index = 0; index < text.Length; index++)
            {
                if (char.IsSurrogate(text[index]) &&
                    (!char.IsHighSurrogate(text[index]) || ++index >= text.Length || !char.IsLowSurrogate(text[index])))
                    return false;
                if (++codePoints > maximumCodePoints)
                    return false;
            }
            return true;
        }

        private async ValueTask<CallToolResult> UiGetWindowTreeAsync(
            CallToolRequestParams request, RequestId requestId, CancellationToken cancellationToken)
        {
            var maxDepth = 3;
            var maxChildren = 50;
            if (request.Arguments is not { Count: >= 1 and <= 3 } arguments ||
                arguments.Keys.Any(key => key is not ("debugSessionId" or "maxDepth" or "maxChildren")) ||
                !arguments.TryGetValue("debugSessionId", out var sessionValue) || sessionValue.ValueKind != JsonValueKind.String ||
                (sessionValue.GetString() is not { Length: <= NativeUiDiscoveryContract.MaximumRequestBytes } sessionText ||
                 string.IsNullOrWhiteSpace(sessionText)) ||
                (arguments.TryGetValue("maxDepth", out var depth) &&
                    (depth.ValueKind != JsonValueKind.Number || !depth.TryGetInt32(out maxDepth) || maxDepth is < 0 or > 32)) ||
                (arguments.TryGetValue("maxChildren", out var children) &&
                    (children.ValueKind != JsonValueKind.Number || !children.TryGetInt32(out maxChildren) || maxChildren is < 1 or > 256)) ||
                JsonSerializer.SerializeToUtf8Bytes(arguments).Length > NativeUiDiscoveryContract.MaximumRequestBytes)
                return InvalidArguments(UiGetWindowTree);

            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, deadline.Token);
            _uiInvokeRequests.TryAdd(requestId, cancellation);
            try
            {
                var sessionId = sessionValue.GetString()!;
                var binding = await ResolveNativeSceneBindingAsync(sessionId).ConfigureAwait(false);
                if (binding is null || !_sessions.TryGetValue(sessionId, out var session))
                    return NotFound();
                bool Current() => _sessions.TryGetValue(sessionId, out var current) && ReferenceEquals(current, session) &&
                    _nativeSceneBindings.TryGetValue(sessionId, out var currentBinding) && ReferenceEquals(currentBinding, binding) &&
                    _slots.ContainsKey(sessionId);
                var payload = await binding.ObserveWindowTreeAsync(maxDepth, maxChildren, Current, cancellation.Token).ConfigureAwait(false);
                if (NativeUiDiscoveryContract.Text(payload, "code") != "UI_DISCOVERY_CLEANUP_UNCONFIRMED")
                {
                    if (deadline.IsCancellationRequested && !cancellationToken.IsCancellationRequested)
                        payload = NativeUiDiscoveryResult.Error("UI_DISCOVERY_TIMEOUT");
                    else if (cancellation.IsCancellationRequested)
                        payload = NativeUiDiscoveryResult.Error("UI_DISCOVERY_CANCELLED");
                    else if (NativeUiDiscoveryContract.Text(payload, "kind") == "ui_window_tree" && !Current())
                        payload = NativeUiDiscoveryResult.Error("UI_DISCOVERY_TARGET_CHANGED");
                }
                var result = Result(payload, isError: NativeUiDiscoveryContract.Text(payload, "kind") != "ui_window_tree");
                return JsonSerializer.SerializeToUtf8Bytes(result).Length <= NativeUiDiscoveryContract.MaximumResultBytes
                    ? result : Result(NativeUiDiscoveryResult.Error("UI_DISCOVERY_LIMIT_EXCEEDED"), isError: true);
            }
            finally
            {
                _uiInvokeRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, cancellation));
            }
        }

        private static bool IsValidUiInvokeAutomationId(string automationId)
        {
            if (string.IsNullOrWhiteSpace(automationId))
                return false;
            if (automationId.Length <= 256)
                return true;

            var length = 0;
            foreach (var _ in automationId.EnumerateRunes())
            {
                if (++length > 256)
                    return false;
            }
            return true;
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

            OutputLifetime? retainedOutput;
            lock (_lifetimeGate)
            {
                if (_shutdownRequested)
                {
                    return NotFound();
                }

                _outputs.Remove(sessionId!, out retainedOutput);
                if (retainedOutput is null && _ownedSlots.ContainsKey(sessionId!))
                {
                    return NotFound();
                }

                if (retainedOutput is not null && _ownedSlots.TryGetValue(sessionId!, out var ownership))
                {
                    // Revocation and control closure share the publication/admission gate.
                    // Close publishes its completion before calling back into this registry.
                    _ = ownership.Slot.CloseAndDrainAsync();
                }
            }

            if (retainedOutput is not null)
            {
                try
                {
                    var state = await retainedOutput.Cleanup.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
                    return StopSuccess(state);
                }
                catch (Exception) when (!cancellationToken.IsCancellationRequested)
                {
                    return NotFound();
                }
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

        public ValueTask DisposeAsync(CancellationToken cancellationToken)
        {
            SessionSlot[] slots;
            Task<DapSessionState>[] outputCleanup;
            Task startsDrained;
            TaskCompletionSource<bool> completion;
            lock (_lifetimeGate)
            {
                _shutdownRequested = true;
                RegisterStartShutdownCancellation(cancellationToken);
                if (_shutdownTask is not null)
                {
                    return new ValueTask(_shutdownTask);
                }

                slots = _ownedSlots.Values.Select(static ownership => ownership.Slot)
                    .Concat(_slots.Values).Distinct().ToArray();
                outputCleanup = _ownedSlots.Values.Select(static ownership => ownership.Output)
                    .Concat(_outputs.Values).Distinct().Select(static output => output.Cleanup.Task).ToArray();
                _outputs.Clear();
                startsDrained = _starts == 0
                    ? Task.CompletedTask
                    : (_startsDrained ??= new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously)).Task;
                completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                _shutdownTask = completion.Task;
            }

            _ = DisposeCoreAsync(slots, outputCleanup, startsDrained, _startShutdown.Token, completion);
            return new ValueTask(completion.Task);
        }

        private async Task DisposeCoreAsync(
            SessionSlot[] slots,
            Task<DapSessionState>[] outputCleanup,
            Task startsDrained,
            CancellationToken cancellationToken,
            TaskCompletionSource<bool> completion)
        {
            Exception? failure = null;
            try
            {
                try
                {
                    await Task.WhenAll(slots.Select(slot => slot.CloseAndDrainAsync(cancellationToken))
                        .Concat(outputCleanup).Append(startsDrained)).ConfigureAwait(false);
                }
                finally
                {
                    // Starts admitted before shutdown finish their own unpublished cleanup.
                    await startsDrained.ConfigureAwait(false);
                    var sessions = _sessions.ToArray();
                    _sessions.Clear();
                    var bindings = _nativeSceneBindings.ToArray();
                    _nativeSceneBindings.Clear();
                    try
                    {
                        await Task.WhenAll(bindings.Select(static binding => binding.Value.DisposeAsync().AsTask())).ConfigureAwait(false);
                    }
                    finally
                    {
                        await Task.WhenAll(sessions.Select(session => DisposeRemovedSessionAsync(session.Value, cancellationToken))).ConfigureAwait(false);
                    }
                }
            }
            catch (Exception exception)
            {
                failure = exception;
            }

            DisposeStartShutdownCancellation(out var startCleanupFailure, out var shutdownCancellation);
            if (failure is null or OperationCanceledException)
            {
                // Cleanup failure must not be hidden by the shutdown cancellation
                // that requested it. All owners have finished before publishing either.
                failure = startCleanupFailure ?? failure;
            }

            if (failure is OperationCanceledException cancelled)
            {
                completion.TrySetCanceled(shutdownCancellation.IsCancellationRequested ? shutdownCancellation : cancelled.CancellationToken);
            }
            else if (failure is not null)
            {
                completion.TrySetException(failure);
            }
            else if (shutdownCancellation.IsCancellationRequested)
            {
                completion.TrySetCanceled(shutdownCancellation);
            }
            else
            {
                completion.TrySetResult(true);
            }
        }

        private void RegisterStartShutdownCancellation(CancellationToken cancellationToken)
        {
            // The caller holds _lifetimeGate, as for publication and start admission.
            if (_startShutdownDisposed || !cancellationToken.CanBeCanceled)
            {
                return;
            }

            var registrations = _startShutdownRegistrations ??= new();
            foreach (var existing in registrations)
            {
                if (existing.Token == cancellationToken)
                {
                    return;
                }
            }

            var registration = cancellationToken.Register(
                static source => ((CancellationTokenSource)source!).Cancel(), _startShutdown);
            registrations.Add((cancellationToken, registration));
        }

        private void DisposeStartShutdownCancellation(out Exception? startCleanupFailure, out CancellationToken shutdownCancellation)
        {
            List<(CancellationToken Token, CancellationTokenRegistration Registration)>? registrations;
            lock (_lifetimeGate)
            {
                _startShutdownDisposed = true;
                startCleanupFailure = _startCleanupFailure;
                registrations = _startShutdownRegistrations;
                _startShutdownRegistrations = null;
            }

            try
            {
                // Callbacks can finish starts and re-enter _lifetimeGate.
                if (registrations is not null)
                {
                    foreach (var registration in registrations)
                    {
                        registration.Registration.Dispose();
                    }
                }
            }
            finally
            {
                _startShutdown.Dispose();
            }

            shutdownCancellation = default;
            if (registrations is not null)
            {
                foreach (var registration in registrations)
                {
                    if (registration.Token.IsCancellationRequested)
                    {
                        shutdownCancellation = registration.Token;
                        break;
                    }
                }
            }
        }

        private void OnRetirementRequested(string token, NetCoreDbgSession session)
        {
            if (_sessions.TryGetValue(token, out var registeredSession)
                && ReferenceEquals(session, registeredSession)
                && _slots.TryGetValue(token, out var slot))
            {
                _ = ObserveCloseAsync(slot);
            }
        }
        internal void CancelStepOver(RequestId requestId)
        {
            if (_stepOverRequests.TryGetValue(requestId, out var cancellation))
            {
                try
                {
                    cancellation.Cancel();
                }
                catch (ObjectDisposedException)
                {
                }
            }
        }



        internal void CancelUiInvoke(RequestId requestId)
        {
            if (_uiInvokeRequests.TryGetValue(requestId, out var cancellation))
            {
                try
                {
                    cancellation.Cancel();
                }
                catch (ObjectDisposedException)
                {
                }
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
            SessionSlot slot,
            OutputLifetime output)
        {
            lock (_lifetimeGate)
            {
                var state = session.State;
                if (state.Event is "exited" or "terminated") output.RetirementState = state;
                _slots.TryRemove(new KeyValuePair<string, SessionSlot>(token, slot));
                _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(token, session));
                _nativeSceneBindings.TryRemove(new KeyValuePair<string, NativeSceneSessionBinding>(token, binding));
            }

            _ = CompleteRetirementAsync(token, session, slot, output);
            binding.RetireUiInvoke();
        }

        private async Task CompleteRetirementAsync(
            string token,
            NetCoreDbgSession session,
            SessionSlot slot,
            OutputLifetime output)
        {
            DapSessionState? state = null;
            Exception? failure = null;
            try
            {
                await slot.CloseAndDrainAsync().ConfigureAwait(false);
                state = session.State;
            }
            catch (Exception exception)
            {
                failure = exception;
            }
            finally
            {
                lock (_lifetimeGate)
                {
                    _ownedSlots.Remove(token);
                }
            }

            // The retained task carries only a value snapshot or the cleanup failure,
            // never the slot's resource-owning delegates or an async cleanup state machine.
            if (failure is OperationCanceledException cancellation)
            {
                output.Cleanup.TrySetCanceled(cancellation.CancellationToken);
            }
            else if (failure is not null)
            {
                output.Cleanup.TrySetException(failure);
                _ = output.Cleanup.Task.Exception;
            }
            else
            {
                output.Cleanup.TrySetResult(state!);
            }
        }

        private sealed class OutputLifetime(DapOutputBuffer buffer)
        {
            internal DapOutputBuffer Buffer { get; } = buffer;
            internal TaskCompletionSource<DapSessionState> Cleanup { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
            internal DapSessionState? RetirementState { get; set; }
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

        private async ValueTask DisposeUnpublishedResourcesAsync(
            NetCoreDbgSession? session,
            NativeSceneSessionBinding? binding,
            CancellationToken cancellationToken)
        {
            try
            {
                try
                {
                    if (session is not null)
                    {
                        // The startup token remains live through rollback so shutdown
                        // can force a cleanup already waiting for a graceful response.
                        await session.StopForOwnerAsync(cancellationToken).ConfigureAwait(false);
                    }
                }
                finally
                {
                    try
                    {
                        if (binding is not null)
                        {
                            await binding.DisposeAsync().ConfigureAwait(false);
                        }
                    }
                    finally
                    {
                        if (session is not null)
                        {
                            await _dispose(session).ConfigureAwait(false);
                        }
                    }
                }
            }
            catch (Exception exception)
            {
                RecordStartCleanupFailure(exception);
                throw;
            }
        }

        private void RecordStartCleanupFailure(Exception exception)
        {
            lock (_lifetimeGate)
            {
                _startCleanupFailure ??= exception;
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
            private CancellationToken _externalCloseCancellation;
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
                Task drained;
                TaskCompletionSource<bool> completion;
                lock (_gate)
                {
                    if (_closeTask is not null)
                    {
                        RegisterCancellation(cancellationToken);
                        return _closeTask;
                    }

                    _closed = true;
                    drained = _leases == 0
                        ? Task.CompletedTask
                        : (_drained ??= new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously)).Task;
                    completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                    _closeTask = completion.Task;
                    RegisterCancellation(cancellationToken);
                }

                // Removal can re-enter close to observe this same completion. Never call
                // the registry while holding the lease gate or before publishing the task.
                _ = CompleteCloseAsync(drained, completion);
                return completion.Task;
            }

            private async Task CompleteCloseAsync(Task drained, TaskCompletionSource<bool> completion)
            {
                try
                {
                    await CloseAndDrainCoreAsync(drained).ConfigureAwait(false);
                    completion.TrySetResult(true);
                }
                catch (OperationCanceledException exception)
                {
                    completion.TrySetCanceled(exception.CancellationToken);
                }
                catch (Exception exception)
                {
                    completion.TrySetException(exception);
                }
            }

            private async Task CloseAndDrainCoreAsync(Task drained)
            {
                try
                {
                    _remove();
                    var drainExpired = await Task.WhenAny(drained, Task.Delay(_drainTimeout)).ConfigureAwait(false) != drained;
                    if (drainExpired)
                    {
                        _abort.Cancel();
                    }

                    await drained.ConfigureAwait(false);
                    await _stop(_abort.IsCancellationRequested ? _abort.Token : CancellationToken.None).ConfigureAwait(false);
                    lock (_gate)
                    {
                        _externalCloseCancellation.ThrowIfCancellationRequested();
                    }
                }
                finally
                {
                    try
                    {
                        await _dispose().ConfigureAwait(false);
                    }
                    finally
                    {
                        CancellationTokenRegistration? registration;
                        lock (_gate)
                        {
                            _abortDisposed = true;
                            registration = _cancellationRegistration;
                            _cancellationRegistration = null;
                        }

                        try
                        {
                            // A running cancellation callback may release a lease. Do
                            // not wait for it while holding the lease gate.
                            registration?.Dispose();
                        }
                        finally
                        {
                            _abort.Dispose();
                        }
                    }
                }
            }

            private void RegisterCancellation(CancellationToken cancellationToken)
            {
                if (!_abortDisposed && !_cancellationRegistration.HasValue && cancellationToken.CanBeCanceled)
                {
                    _externalCloseCancellation = cancellationToken;
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
                else if (TryRemoveUnownedSession(sessionId, session))
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
            NativeSceneSessionBinding? binding;
            lock (_lifetimeGate)
            {
                if (_ownedSlots.ContainsKey(sessionId))
                {
                    return;
                }

                _nativeSceneBindings.TryRemove(sessionId, out binding);
            }

            if (binding is not null)
            {
                await binding.DisposeAsync().ConfigureAwait(false);
            }
        }

        private bool TryRemoveUnownedSession(string sessionId, NetCoreDbgSession session)
        {
            lock (_lifetimeGate)
            {
                return !_ownedSlots.ContainsKey(sessionId)
                    && _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(sessionId, session));
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

            if (arguments.Count > 17 || arguments.Keys.Any(static name => name is not "program" and not "breakpoints" and not "args" and not "cwd" and not "stopAtEntry" and not "stop_at_entry" and not "justMyCode" and not "env" and not "projectRoot" and not "launchProfile" and not "launch_profile" and not "preBuild" and not "pre_build" and not "buildProject" and not "build_project" and not "buildConfiguration" and not "build_configuration"))
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

        private static bool TryReadLaunchOptions(IDictionary<string, JsonElement>? arguments, out DapLaunchOptions? options)
        {
            options = null;
            if (arguments is null) return true;

            string[]? args = null;
            if (arguments.TryGetValue("args", out var argumentList) && argumentList.ValueKind != JsonValueKind.Null)
            {
                if (argumentList.ValueKind != JsonValueKind.Array) return false;
                args = new string[argumentList.GetArrayLength()];
                var index = 0;
                foreach (var argument in argumentList.EnumerateArray())
                {
                    if (argument.ValueKind != JsonValueKind.String) return false;
                    args[index++] = argument.GetString()!;
                }
            }

            string? cwd = null;
            if (arguments.TryGetValue("cwd", out var directory) && directory.ValueKind != JsonValueKind.Null)
            {
                if (directory.ValueKind != JsonValueKind.String) return false;
                cwd = directory.GetString();
                if (cwd?.Length == 0) cwd = null;
            }

            bool? stopAtEntry = null;
            if (!TryGetAliased(arguments, "stopAtEntry", "stop_at_entry", out var stop)) return false;
            if (stop.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null))
            {
                if (stop.ValueKind is not JsonValueKind.True and not JsonValueKind.False) return false;
                stopAtEntry = stop.GetBoolean();
            }

            bool? justMyCode = null;
            if (arguments.TryGetValue("justMyCode", out var justMyCodeElement) && justMyCodeElement.ValueKind != JsonValueKind.Null)
            {
                if (justMyCodeElement.ValueKind is not JsonValueKind.True and not JsonValueKind.False) return false;
                justMyCode = justMyCodeElement.GetBoolean();
            }

            Dictionary<string, string?>? environment = null;
            if (arguments.TryGetValue("env", out var env) && env.ValueKind != JsonValueKind.Null)
            {
                if (env.ValueKind != JsonValueKind.Object) return false;
                environment = new Dictionary<string, string?>(DapLaunchEnvironment.NameComparer);
                foreach (var property in env.EnumerateObject())
                {
                    var name = property.Name;
                    if (name.Length == 0 || name.Contains('=') || name.Contains('\0')
                        || DapLaunchEnvironment.NameComparer.Equals(name, NativeSceneProbeChannel.PipeEnvironmentName)
                        || DapLaunchEnvironment.NameComparer.Equals(name, NativeSceneProbeChannel.NonceEnvironmentName)
                        || property.Value.ValueKind is not (JsonValueKind.String or JsonValueKind.Null)) return false;
                    var value = property.Value.ValueKind == JsonValueKind.Null ? null : property.Value.GetString();
                    if (value?.Contains('\0') == true) return false;
                    environment[name] = value;
                }
            }

            if (args is not null || cwd is not null || stopAtEntry is not null || justMyCode is not null || environment is not null)
                options = new DapLaunchOptions(cwd, args, stopAtEntry, justMyCode, environment);
            return true;
        }

        private static bool TryReadLaunchProfileArguments(
            IDictionary<string, JsonElement>? arguments, out string? projectRoot, out string? profileName)
        {
            projectRoot = null;
            profileName = null;
            if (arguments is null) return true;
            if (arguments.TryGetValue("projectRoot", out var root) && root.ValueKind != JsonValueKind.Null)
            {
                if (root.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(projectRoot = root.GetString())
                    || projectRoot.Contains('\0')) return false;
            }
            if (!TryGetAliased(arguments, "launchProfile", "launch_profile", out var profile)) return false;
            if (profile.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null))
            {
                if (profile.ValueKind != JsonValueKind.String || (profileName = profile.GetString())?.Contains('\0') == true) return false;
                if (profileName?.Length == 0) profileName = null;
            }
            return true;
        }

        private static bool TryReadPreBuildArguments(IDictionary<string, JsonElement>? arguments, out PreBuildOptions options)
        {
            var enabled = false;
            string? project = null;
            var configuration = "Debug";
            options = new PreBuildOptions(enabled, project, configuration);
            if (arguments is null) return true;
            if (!TryGetAliased(arguments, "preBuild", "pre_build", out var requested) ||
                !TryGetAliased(arguments, "buildProject", "build_project", out var path) ||
                !TryGetAliased(arguments, "buildConfiguration", "build_configuration", out var config)) return false;
            if (requested.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null))
            {
                if (requested.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return false;
                enabled = requested.GetBoolean();
            }
            if (path.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null))
            {
                if (path.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(project = path.GetString()) || project.Contains('\0')) return false;
            }
            if (config.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.Null))
            {
                if (config.ValueKind != JsonValueKind.String || config.GetString() is not ("Debug" or "Release")) return false;
                configuration = config.GetString()!;
            }
            options = new PreBuildOptions(enabled, project, configuration);
            return true;
        }

        private static bool TryGetAliased(IDictionary<string, JsonElement> arguments, string name, string alias, out JsonElement value)
        {
            var present = arguments.TryGetValue(name, out value);
            if (!arguments.TryGetValue(alias, out var alternate)) return true;
            if (present) return false;
            value = alternate;
            return true;
        }

        private static bool TryReadBreakpoints(
            IDictionary<string, JsonElement>? arguments,
            out DapSourceBreakpoint[] breakpoints)
        {
            breakpoints = Array.Empty<DapSourceBreakpoint>();
            if (arguments is null || !arguments.TryGetValue("breakpoints", out var element))
            {
                return true;
            }

            if (element.ValueKind != JsonValueKind.Array || element.GetArrayLength() > MaximumBreakpointCount)
            {
                return false;
            }

            if (element.GetArrayLength() == 0)
            {
                return true;
            }

            breakpoints = new DapSourceBreakpoint[element.GetArrayLength()];
            var index = 0;
            foreach (var breakpoint in element.EnumerateArray())
            {
                if (breakpoint.ValueKind != JsonValueKind.Object
                    || !breakpoint.TryGetProperty("file", out var file)
                    || file.ValueKind != JsonValueKind.String
                    || string.IsNullOrWhiteSpace(file.GetString())
                    || !breakpoint.TryGetProperty("line", out var line)
                    || line.ValueKind != JsonValueKind.Number
                    || !line.TryGetInt32(out var lineNumber)
                    || lineNumber <= 0)
                {
                    return false;
                }

                var propertyCount = 0;
                foreach (var property in breakpoint.EnumerateObject())
                {
                    if (property.Name is not "file" and not "line")
                    {
                        return false;
                    }

                    propertyCount++;
                }

                if (propertyCount != 2)
                {
                    return false;
                }

                breakpoints[index] = new DapSourceBreakpoint(index, file.GetString()!, lineNumber);
                index++;
            }

            return true;
        }

        private static bool TryNormalizeBreakpointSources(DapSourceBreakpoint[] breakpoints)
        {
            if (breakpoints.Length == 0)
            {
                return true;
            }

            try
            {
                var workingDirectory = Environment.CurrentDirectory;
                for (var index = 0; index < breakpoints.Length; index++)
                {
                    var breakpoint = breakpoints[index];
                    var path = Path.GetFullPath(breakpoint.File, workingDirectory);
                    if (!File.Exists(path))
                    {
                        return false;
                    }

                    breakpoints[index] = breakpoint with { File = path };
                }

                return true;
            }
            catch (Exception exception) when (exception is ArgumentException
                or IOException or NotSupportedException or UnauthorizedAccessException or System.Security.SecurityException)
            {
                return false;
            }
        }

        private static bool TryNormalizeBreakpointSource(string file, out string normalized, bool requireExistingFile = true)
        {
            normalized = string.Empty;
            try
            {
                var path = Path.GetFullPath(file, Environment.CurrentDirectory);
                if (requireExistingFile && !File.Exists(path))
                {
                    return false;
                }

                normalized = path;
                return true;
            }
            catch (Exception exception) when (exception is ArgumentException
                or IOException or NotSupportedException or UnauthorizedAccessException or System.Security.SecurityException)
            {
                return false;
            }
        }

        private static bool TryNormalizeLegacyBreakpointSource(string file, out string normalized, bool requireExistingFile)
        {
            normalized = string.Empty;
            if (!TryNormalizeBreakpointSource(file, out var fullPath, requireExistingFile)) return false;
            try
            {
                var root = Path.GetPathRoot(fullPath)!;
                var components = fullPath[root.Length..].Split([Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar], StringSplitOptions.RemoveEmptyEntries);
                var path = root;
                for (var index = 0; index < components.Length; index++)
                {
                    path = Path.Combine(path, components[index]);
                    FileSystemInfo entry = index == components.Length - 1 ? new FileInfo(path) : new DirectoryInfo(path);
                    if (entry.Exists && entry.LinkTarget is not null)
                    {
                        var target = entry.ResolveLinkTarget(returnFinalTarget: true);
                        if (target is null) return false;
                        path = target.FullName;
                    }
                }
                if (requireExistingFile && !File.Exists(path)) return false;
                normalized = path;
                return true;
            }
            catch (Exception exception) when (exception is ArgumentException
                or IOException or NotSupportedException or UnauthorizedAccessException or System.Security.SecurityException)
            {
                return false;
            }
        }

        private static bool TryReadExceptionFilterArguments(IDictionary<string, JsonElement>? arguments, out ExceptionFilterArguments result)
        {
            result = null!;
            if (arguments is null || arguments.Keys.Any(static name => name is not "debugSessionId" and not "filters")
                || !arguments.TryGetValue("debugSessionId", out var sessionId)
                || sessionId.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(sessionId.GetString()))
            {
                return false;
            }
            string[] filters = [];
            if (arguments.TryGetValue("filters", out var value) && value.ValueKind != JsonValueKind.Null)
            {
                if (value.ValueKind != JsonValueKind.Array)
                {
                    return false;
                }
                filters = new string[value.GetArrayLength()];
                var index = 0;
                foreach (var filter in value.EnumerateArray())
                {
                    if (filter.ValueKind != JsonValueKind.String)
                    {
                        return false;
                    }
                    filters[index++] = filter.GetString()!;
                }
            }
            var filterBytes = JsonSerializer.SerializeToUtf8Bytes(filters).Length;
            var verificationBytes = filters.Length == 0 ? 0L : (long)filters.Length * "{\"verified\":false}".Length + filters.Length - 1;
            if (filterBytes + verificationBytes + "{\"kind\":\"exception_filters_acknowledged\",\"filters\":,\"breakpoints\":[]}".Length > MaximumSerializedInspectionBytes)
            {
                return false;
            }
            result = new ExceptionFilterArguments(sessionId.GetString()!, filters);
            return true;
        }

        private static bool TryReadBreakpointArguments(
            IDictionary<string, JsonElement>? arguments,
            out BreakpointArguments result)
        {
            result = null!;
            if (arguments is null
                || arguments.Count != 3
                || arguments.Keys.Any(static name => name is not "debugSessionId" and not "file" and not "line")
                || !arguments.TryGetValue("debugSessionId", out var sessionId)
                || sessionId.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionId.GetString())
                || !arguments.TryGetValue("file", out var file)
                || file.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(file.GetString())
                || !arguments.TryGetValue("line", out var line)
                || line.ValueKind != JsonValueKind.Number
                || !line.TryGetInt32(out var number)
                || number <= 0)
            {
                return false;
            }

            result = new BreakpointArguments(sessionId.GetString()!, file.GetString()!, number);
            return true;
        }

        private static bool TryReadOutputArguments(
            IDictionary<string, JsonElement>? arguments,
            out string sessionId,
            out bool clear,
            out string? category)
        {
            sessionId = string.Empty;
            clear = false;
            category = null;
            if (arguments is null
                || arguments.Count is < 1 or > 3
                || arguments.Keys.Any(static name => name is not "debugSessionId" and not "clear" and not "category")
                || !arguments.TryGetValue("debugSessionId", out var sessionIdElement)
                || sessionIdElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionIdElement.GetString()))
            {
                return false;
            }

            if (arguments.TryGetValue("clear", out var clearElement))
            {
                if (clearElement.ValueKind is not JsonValueKind.True and not JsonValueKind.False)
                {
                    return false;
                }

                clear = clearElement.GetBoolean();
            }

            if (arguments.TryGetValue("category", out var categoryElement))
            {
                if (categoryElement.ValueKind is not JsonValueKind.String and not JsonValueKind.Null)
                {
                    return false;
                }

                category = categoryElement.GetString();
            }

            sessionId = sessionIdElement.GetString()!;
            return true;
        }

        private static bool TryReadOutputTailArguments(
            IDictionary<string, JsonElement>? arguments,
            out string sessionId,
            out long lines,
            out string? category)
        {
            sessionId = string.Empty;
            lines = 50;
            category = null;
            if (arguments is null
                || arguments.Count is < 1 or > 3
                || arguments.Keys.Any(static name => name is not "debugSessionId" and not "lines" and not "category")
                || !arguments.TryGetValue("debugSessionId", out var sessionIdElement)
                || sessionIdElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionIdElement.GetString()))
            {
                return false;
            }

            if (arguments.TryGetValue("lines", out var linesElement) && !TryReadOutputLineCount(linesElement, out lines))
            {
                return false;
            }

            if (arguments.TryGetValue("category", out var categoryElement))
            {
                if (categoryElement.ValueKind is not JsonValueKind.String and not JsonValueKind.Null)
                {
                    return false;
                }

                category = categoryElement.GetString();
            }

            sessionId = sessionIdElement.GetString()!;
            return true;
        }

        private static bool TryReadOutputSearchArguments(
            IDictionary<string, JsonElement>? arguments,
            out string sessionId,
            out string pattern,
            out long contextLines,
            out string? category)
        {
            sessionId = string.Empty;
            pattern = string.Empty;
            contextLines = 2;
            category = null;
            if (arguments is null
                || arguments.Count is < 2 or > 4
                || arguments.Keys.Any(static name => name is not "debugSessionId" and not "pattern" and not "contextLines" and not "category")
                || !arguments.TryGetValue("debugSessionId", out var sessionIdElement)
                || sessionIdElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionIdElement.GetString())
                || !arguments.TryGetValue("pattern", out var patternElement)
                || patternElement.ValueKind != JsonValueKind.String)
            {
                return false;
            }

            if (arguments.TryGetValue("contextLines", out var contextElement) && !TryReadOutputLineCount(contextElement, out contextLines))
            {
                return false;
            }

            if (arguments.TryGetValue("category", out var categoryElement))
            {
                if (categoryElement.ValueKind is not JsonValueKind.String and not JsonValueKind.Null)
                {
                    return false;
                }

                category = categoryElement.GetString();
            }

            sessionId = sessionIdElement.GetString()!;
            pattern = patternElement.GetString()!;
            return true;
        }

        private static bool TryReadOutputLineCount(JsonElement element, out long lines)
        {
            lines = 0;
            if (element.ValueKind != JsonValueKind.Number)
            {
                return false;
            }

            if (element.TryGetInt64(out lines))
            {
                return true;
            }

            // JSON Schema integers include exact decimal/exponent forms; do not round through double or decimal.
            var number = element.GetRawText().AsSpan();
            var negative = number[0] == '-';
            if (negative)
            {
                number = number[1..];
            }

            var exponentOffset = number.IndexOfAny('e', 'E');
            var significand = exponentOffset < 0 ? number : number[..exponentOffset];
            var decimalOffset = significand.IndexOf('.');
            var fractionalDigits = decimalOffset < 0 ? 0 : significand.Length - decimalOffset - 1;
            var digits = 0;
            var trailingZeros = 0;
            foreach (var character in significand)
            {
                if (character != '.')
                {
                    digits++;
                    trailingZeros = character == '0' ? trailingZeros + 1 : 0;
                }
            }

            if (digits == trailingZeros)
            {
                lines = 0;
                return true;
            }

            long exponent = 0;
            if (exponentOffset >= 0)
            {
                var exponentText = number[(exponentOffset + 1)..];
                var negativeExponent = exponentText[0] == '-';
                if (exponentText[0] is '+' or '-')
                {
                    exponentText = exponentText[1..];
                }

                // Beyond this magnitude, the exponent already determines integral overflow or a nonzero fraction.
                var exponentLimit = number.Length + 20L;
                foreach (var character in exponentText)
                {
                    exponent = Math.Min(exponentLimit, exponent * 10 + character - '0');
                }

                if (negativeExponent)
                {
                    exponent = -exponent;
                }
            }

            var shift = exponent - fractionalDigits;
            if (shift < -trailingZeros)
            {
                return false;
            }

            var digitsToKeep = digits + Math.Min(shift, 0);
            long magnitude = 0;
            foreach (var character in significand)
            {
                if (character == '.')
                {
                    continue;
                }

                if (digitsToKeep-- == 0)
                {
                    break;
                }

                var digit = character - '0';
                magnitude = magnitude > (long.MaxValue - digit) / 10 ? long.MaxValue : magnitude * 10 + digit;
            }

            while (shift-- > 0 && magnitude < long.MaxValue)
            {
                magnitude = magnitude > long.MaxValue / 10 ? long.MaxValue : magnitude * 10;
            }

            // Saturation preserves slicing against every representable retained-line count without bounding JSON integers.
            lines = negative ? -magnitude : magnitude;
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

        private static bool TryReadPauseArguments(
            IDictionary<string, JsonElement>? arguments,
            out PauseArguments result)
        {
            result = null!;
            if (arguments is null
                || arguments.Keys.Any(static name => name is not "debugSessionId" and not "threadId")
                || !arguments.TryGetValue("debugSessionId", out var sessionId)
                || sessionId.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionId.GetString()))
            {
                return false;
            }

            int? threadId = null;
            if (arguments.TryGetValue("threadId", out var target) && target.ValueKind != JsonValueKind.Null)
            {
                if (target.ValueKind != JsonValueKind.Number || !target.TryGetInt32(out var selected))
                {
                    return false;
                }
                threadId = selected;
            }

            result = new PauseArguments(sessionId.GetString()!, threadId);
            return true;
        }


        private static bool TryReadOptionalThread(IDictionary<string, JsonElement> arguments, out int? threadId)
        {
            threadId = null;
            if (!TryGetAliased(arguments, "threadId", "thread_id", out var value)) return false;
            if (value.ValueKind is JsonValueKind.Undefined or JsonValueKind.Null) return true;
            if (!TryReadOutputLineCount(value, out var parsed) || parsed is < int.MinValue or > int.MaxValue) return false;
            threadId = (int)parsed;
            return true;
        }

        private static bool TryReadContinueArguments(IDictionary<string, JsonElement>? arguments, out PauseArguments result)
        {
            result = null!;
            if (arguments is null || arguments.Keys.Any(static name => name is not "debugSessionId" and not "threadId" and not "thread_id")
                || !arguments.TryGetValue("debugSessionId", out var session)
                || session.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(session.GetString())
                || !TryReadOptionalThread(arguments, out var threadId)) return false;
            result = new PauseArguments(session.GetString()!, threadId);
            return true;
        }

        private static bool TryReadTargetedStepArguments(IDictionary<string, JsonElement>? input, bool allowTarget, out TargetedStepArguments result)
        {
            result = null!;
            if (input is null || input.Keys.Any(key => key is not ("debugSessionId" or "threadId" or "thread_id" or "targetId" or "target_id" or "inspectionContextId"))
                || !input.TryGetValue("debugSessionId", out var session) || session.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(session.GetString()) || !TryReadOptionalThread(input, out var thread)
                || !TryGetAliased(input, "targetId", "target_id", out var target)) return false;
            int? targetId = null;
            if (target.ValueKind != JsonValueKind.Undefined)
            {
                if (!allowTarget || !TryReadOutputLineCount(target, out var number) || number is < int.MinValue or > int.MaxValue) return false;
                targetId = (int)number;
            }
            string? context = null;
            if (input.TryGetValue("inspectionContextId", out var element))
            {
                if (element.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(context = element.GetString()) || context.Length > 128) return false;
            }
            if ((targetId is null) != (context is null)) return false;
            result = new TargetedStepArguments(session.GetString()!, thread, targetId, context);
            return true;
        }

        private static bool TryReadSetVariableArguments(IDictionary<string, JsonElement>? arguments, out SetVariableArguments result)
        {
            result = null!;
            if (arguments is null || arguments.Count != 5
                || !arguments.TryGetValue("debugSessionId", out var session)
                || session.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(session.GetString())
                || !arguments.TryGetValue("inspectionContextId", out var context)
                || context.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(context.GetString())
                || context.GetString()!.Length > 128
                || !arguments.TryGetValue("variablesReference", out var reference)
                || reference.ValueKind != JsonValueKind.Number || !reference.TryGetInt32(out var id) || id <= 0
                || !arguments.TryGetValue("name", out var name) || name.ValueKind != JsonValueKind.String
                || !arguments.TryGetValue("value", out var value) || value.ValueKind != JsonValueKind.String)
            {
                return false;
            }
            result = new SetVariableArguments(session.GetString()!, context.GetString()!, id, name.GetString()!, value.GetString()!);
            return true;
        }

        private static bool TryReadEvaluateArguments(
            IDictionary<string, JsonElement>? arguments,
            out EvaluateArguments result)
        {
            result = null!;
            if (arguments is null
                || arguments.Count != 4
                || !arguments.TryGetValue("debugSessionId", out var sessionIdElement)
                || sessionIdElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionIdElement.GetString())
                || !arguments.TryGetValue("inspectionContextId", out var contextElement)
                || contextElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(contextElement.GetString())
                || contextElement.GetString()!.Length > 128
                || !arguments.TryGetValue("frameId", out var frameElement)
                || frameElement.ValueKind != JsonValueKind.Number
                || !frameElement.TryGetInt32(out var frameId)
                || !arguments.TryGetValue("expression", out var expressionElement)
                || expressionElement.ValueKind != JsonValueKind.String)
            {
                return false;
            }

            result = new EvaluateArguments(sessionIdElement.GetString()!, contextElement.GetString()!, frameId, expressionElement.GetString()!);
            return true;
        }

        private static bool TryReadStepTargetArguments(IDictionary<string, JsonElement>? arguments, out InspectionArguments result)
        {
            result = null!;
            if (arguments is null || arguments.Count != 3
                || arguments.Keys.Any(static key => key is not ("debugSessionId" or "inspectionContextId" or "frameId"))
                || !arguments.TryGetValue("debugSessionId", out var session) || session.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(session.GetString())
                || !arguments.TryGetValue("inspectionContextId", out var context) || context.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(context.GetString()) || context.GetString()!.Length > 128
                || !arguments.TryGetValue("frameId", out var frame) || !TryReadOutputLineCount(frame, out var id)
                || id is < int.MinValue or > int.MaxValue) return false;
            result = new InspectionArguments(session.GetString()!, context.GetString()!, (int)id, null, null);
            return true;
        }

        private static bool TryReadInspectionArguments(
            IDictionary<string, JsonElement>? arguments,
            bool scopes,
            out InspectionArguments result)
        {
            result = null!;
            var idName = scopes ? "frameId" : "variablesReference";
            if (arguments is null
                || arguments.Keys.Any(name => name != "debugSessionId" && name != "inspectionContextId" && name != idName
                    && (scopes || name is not ("start" or "count")))
                || !arguments.TryGetValue("debugSessionId", out var sessionIdElement)
                || sessionIdElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionIdElement.GetString())
                || !arguments.TryGetValue("inspectionContextId", out var contextElement)
                || contextElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(contextElement.GetString())
                || contextElement.GetString()!.Length > 128
                || !arguments.TryGetValue(idName, out var idElement)
                || idElement.ValueKind != JsonValueKind.Number
                || !idElement.TryGetInt32(out var id)
                || (!scopes && id <= 0))
            {
                return false;
            }

            int? start = null;
            if (arguments.TryGetValue("start", out var startElement) && startElement.ValueKind != JsonValueKind.Null)
            {
                if (!TryReadOutputLineCount(startElement, out var value) || value is < 0 or > int.MaxValue)
                {
                    return false;
                }
                start = (int)value;
            }

            int? count = null;
            if (arguments.TryGetValue("count", out var countElement) && countElement.ValueKind != JsonValueKind.Null)
            {
                if (!TryReadOutputLineCount(countElement, out var value) || value is < 0 or > int.MaxValue)
                {
                    return false;
                }
                count = (int)value;
            }

            result = new InspectionArguments(sessionIdElement.GetString()!, contextElement.GetString()!, id, start, count);
            return true;
        }

        private static bool TryReadCompositeArguments(IDictionary<string, JsonElement>? input, bool exception, out CompositeArguments result)
        {
            result = null!;
            if (input is null || !input.TryGetValue("debugSessionId", out var id)
                || id.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(id.GetString())) return false;
            var allowed = exception
                ? new[] { "debugSessionId", "maxFrames", "max_frames", "includeVariablesForFrames", "include_variables_for_frames", "maxInnerExceptions", "max_inner_exceptions" }
                : new[] { "debugSessionId", "includeVariables", "include_variables", "includeOutputTail", "include_output_tail" };
            if (input.Keys.Any(key => !allowed.Contains(key))) return false;
            bool ReadOption(string camel, string snake, out JsonElement value)
            {
                var hasCamel = input.TryGetValue(camel, out value);
                var hasSnake = input.TryGetValue(snake, out var alternate);
                if (hasCamel && hasSnake) return false;
                if (hasSnake) value = alternate;
                return true;
            }
            bool ReadInteger(string camel, string snake, int defaultValue, int minimum, int maximum, out int value)
            {
                value = defaultValue;
                if (!ReadOption(camel, snake, out var element)) return false;
                if (element.ValueKind == JsonValueKind.Undefined) return true;
                if (!TryReadOutputLineCount(element, out var parsed) || parsed < minimum || parsed > maximum) return false;
                value = (int)parsed;
                return true;
            }
            var frames = exception ? 10 : 5;
            var variableFrames = 1;
            var output = 10;
            var inner = 0;
            if (exception)
            {
                if (!ReadInteger("maxFrames", "max_frames", 10, 1, 20, out frames)
                    || !ReadInteger("includeVariablesForFrames", "include_variables_for_frames", 1, 0, 5, out variableFrames)
                    || !ReadInteger("maxInnerExceptions", "max_inner_exceptions", 0, 0, 5, out inner)) return false;
            }
            else
            {
                if (!ReadOption("includeVariables", "include_variables", out var variables)
                    || variables.ValueKind is not (JsonValueKind.Undefined or JsonValueKind.True or JsonValueKind.False)
                    || !ReadInteger("includeOutputTail", "include_output_tail", 10, 0, 100, out output)) return false;
                variableFrames = variables.ValueKind == JsonValueKind.False ? 0 : 1;
            }
            result = new CompositeArguments(id.GetString()!, frames, variableFrames, output, inner);
            return true;
        }

        private static bool TryReadCallStackArguments(
            IDictionary<string, JsonElement>? arguments,
            out CallStackArguments result)
        {
            result = null!;
            if (arguments is null
                || arguments.Count is < 1 or > 6
                || arguments.Keys.Any(static name => name is not "debugSessionId" and not "threadId" and not "thread_id" and not "startFrame" and not "levels" and not "includeInspectionContext")
                || !arguments.TryGetValue("debugSessionId", out var sessionIdElement)
                || sessionIdElement.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(sessionIdElement.GetString())
                || !TryReadOptionalThread(arguments, out var threadId))
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

            var includeInspectionContext = false;
            if (arguments.TryGetValue("includeInspectionContext", out var contextElement))
            {
                if (contextElement.ValueKind is not JsonValueKind.True and not JsonValueKind.False)
                {
                    return false;
                }

                includeInspectionContext = contextElement.GetBoolean();
            }

            result = new CallStackArguments(sessionIdElement.GetString()!, threadId, startFrame, levels, includeInspectionContext);
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

        private sealed record PreBuildOptions(bool Enabled, string? Project, string Configuration);

        private sealed record StartDebugSuccessContent(
            string kind, string debugSessionId, LifecycleStateContent state,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] IReadOnlyList<DapInitialBreakpoint>? initialBreakpoints,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] LaunchEnvironmentContent? launchEnvironment);

        private sealed record LaunchEnvironmentContent(
            string source, string? profile, string? path, IReadOnlyList<string> variableNames, int appliedCount);

        private static CallToolResult StopSuccess(DapSessionState state) => Result(new
        {
            kind = "stop_debug_success",
            state = new LifecycleStateContent(state.Event, state.StopReason, state.ExitCode),
        }, isError: false);

        private static LifecycleStateContent LifecycleState(DapSessionState state) => new(
            state.Event,
            state.StopReason,
            state.ExitCode,
            state.CurrentThreadId);

        private static CallToolResult NotFound() => Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");

        private static CallToolResult InvalidArguments(string tool) => Result(new
        {
            kind = "invalid_tool_arguments",
            error = "INVALID_TOOL_ARGUMENTS",
            tool,
        }, isError: true);

        private static CallToolResult Error(string kind, string error) => Result(new { kind, error }, isError: true);
        private static CallToolResult BuildFailure(DapBuildResult build)
        {
            const int maximumOutput = 4096;
            static string Tail(string value) => value.Length <= maximumOutput ? value : value[^maximumOutput..];
            return Result(new
            {
                kind = "start_debug_build_failed",
                error = "START_DEBUG_BUILD_FAILED",
                stage = build.Stage,
                exitCode = build.Command.ExitCode,
                stdout = Tail(build.Command.Stdout),
                stderr = Tail(build.Command.Stderr),
                stdoutTruncated = build.Command.Stdout.Length > maximumOutput,
                stderrTruncated = build.Command.Stderr.Length > maximumOutput,
            }, isError: true);
        }

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

        private sealed record LifecycleStateContent(
            string? @event,
            string? stopReason,
            int? exitCode,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? currentThreadId = null);

        private sealed record DebugStateContent(
            string? @event,
            string? stopReason,
            int? exitCode,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? currentThreadId,
            IReadOnlyDictionary<string, DapLoadedSource>? loadedSources,
            bool loadedSourcesComplete);

        private sealed record ThreadsSuccessContent(string kind, IReadOnlyList<DapThread> threads);
        private sealed record CompositeArguments(string SessionId, int MaxFrames, int VariableFrames, int OutputEntries, int InnerExceptions);
        private sealed record CallStackArguments(string SessionId, int? ThreadId, uint StartFrame, uint Levels, bool IncludeInspectionContext);
        private sealed record InspectionArguments(string SessionId, string InspectionContextId, int Id, int? Start, int? Count);
        private sealed record TargetedStepArguments(string SessionId, int? ThreadId, int? TargetId, string? InspectionContextId);
        private sealed record ScopesSuccessContent(string kind, string inspectionContextId, IReadOnlyList<DapScope> scopes);
        private sealed record VariablesSuccessContent(string kind, string inspectionContextId, IReadOnlyList<DapVariable> variables);
        private sealed record EvaluateArguments(string SessionId, string InspectionContextId, int FrameId, string Expression);
        private sealed record EvaluateSuccessContent(string kind, string inspectionContextId, string result, string? type, int variablesReference);
        private sealed record SetVariableArguments(string SessionId, string InspectionContextId, int VariablesReference, string Name, string Value);
        private sealed record ExceptionInfoSuccessContent(string kind, JsonElement exception);

        private sealed record CallStackSuccessContent(
            string kind,
            IReadOnlyList<DapStackFrame> frames,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] uint? totalFrames,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? inspectionContextId = null);
        private sealed record StepOverSuccessContent(string kind, int threadId);
        private sealed record StepOverTerminalContent(
            string kind,
            string terminal,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? exitCode);
        private sealed record StepOverTimedOutContent(string kind, int threadId, int timeoutSeconds);
        private sealed record ContinueStoppedContent(
            string kind,
            string reason,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? threadId,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] bool? allThreadsStopped);
        private sealed record ContinueTerminalContent(
            string kind,
            string terminal,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? exitCode);
        private sealed record ContinueTimedOutContent(string kind, int threadId, int timeoutSeconds, bool acknowledged);
        private sealed record PauseArguments(string SessionId, int? ThreadId);
        private sealed record PauseAcknowledgedContent(string kind, int threadId);
        private sealed record PauseTerminalContent(
            string kind,
            string terminal,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? exitCode);
        private sealed record BreakpointArguments(string SessionId, string File, int Line);
        private sealed record ExceptionFilterArguments(string SessionId, string[] Filters);
        private sealed record ExceptionFilterVerification(bool verified);
        private sealed record ExceptionFiltersAcknowledgedContent(string kind, string[] filters, ExceptionFilterVerification[]? breakpoints);
        private sealed record BreakpointAddedContent(
            string kind,
            string file,
            int line,
            bool verified,
            [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? actualLine);
        private sealed record BreakpointRemovedContent(string kind, string file, int line, bool removed);

    }

    private static class ToolCatalog
    {
        private const string OptionalStoppedThreadSchema = """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1},"threadId":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"required":["debugSessionId"],"additionalProperties":false,"allOf":[{"not":{"required":["threadId","thread_id"]}}]}""";
        private const string OptionalStoppedThreadStrictSessionSchema = """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"threadId":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"required":["debugSessionId"],"additionalProperties":false,"allOf":[{"not":{"required":["threadId","thread_id"]}}]}""";
        internal static ListToolsResult LegacyList() => new()
        {
            Tools =
            [
                Tool("start_debug", "Start one implicit native session. pre_build defaults to true and requires build_project; stealth_mode true is unsupported.", """{"type":"object","properties":{"program":{"type":"string","minLength":1},"cwd":{"type":["string","null"]},"args":{"type":["array","null"],"items":{"type":"string"}},"env":{"type":["object","null"]},"launch_profile":{"type":["string","null"]},"stop_at_entry":{"type":"boolean"},"pre_build":{"type":"boolean"},"build_project":{"type":["string","null"]},"build_configuration":{"type":"string","enum":["Debug","Release"]},"stealth_mode":{"const":false}},"required":["program"],"additionalProperties":false}"""),
                Tool("get_debug_state", "Read observed implicit session state; unavailable fields are omitted.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("get_threads", "Get threads from the implicit session.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("get_call_stack", "Read up to 256 frames from a stopped thread.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"]},"levels":{"type":"integer","minimum":1,"maximum":256}},"additionalProperties":false}"""),
                Tool("add_breakpoint", "Set an unconditional source breakpoint. Conditions and hit counts are unsupported.", """{"type":"object","properties":{"file":{"type":"string","minLength":1},"line":{"type":"integer","minimum":1,"maximum":2147483647},"condition":{"type":"null"},"hit_condition":{"type":"null"}},"required":["file","line"],"additionalProperties":false}"""),
                Tool("remove_breakpoint", "Remove a requested source breakpoint.", """{"type":"object","properties":{"file":{"type":"string","minLength":1},"line":{"type":"integer","minimum":1,"maximum":2147483647}},"required":["file","line"],"additionalProperties":false}"""),
                Tool("get_scopes", "Get scopes for the current stopped stack frame, or an explicit frame ID. Select a fresh stack after execution.", """{"type":"object","properties":{"frame_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("get_variables", "Get variables for a reference in the current stopped context. Filtering is unsupported; paging is nonnegative.", """{"type":"object","properties":{"variables_reference":{"type":"integer","minimum":1,"maximum":2147483647},"filter":{"type":"null"},"start":{"type":["integer","null"],"minimum":0,"maximum":2147483647},"count":{"type":["integer","null"],"minimum":0,"maximum":2147483647}},"required":["variables_reference"],"additionalProperties":false}"""),
                Tool("evaluate_expression", "Evaluate in the current stopped context. Expressions may mutate the debuggee; mux ownership is required and uncertain writes are not retried.", """{"type":"object","properties":{"expression":{"type":"string"},"frame_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"required":["expression"],"additionalProperties":false}"""),
                Tool("set_variable", "Set a variable in the selected stopped context; select a fresh stack afterwards. Uncertain writes must not be retried automatically.", """{"type":"object","properties":{"variables_reference":{"type":"integer","minimum":1,"maximum":2147483647},"name":{"type":"string"},"value":{"type":"string"}},"required":["variables_reference","name","value"],"additionalProperties":false}"""),
                Tool("configure_exceptions", "Replace exception filters with owner permission. Omitted or null filters select an empty list.", """{"type":"object","properties":{"filters":{"type":["array","null"],"items":{"type":"string"}}},"additionalProperties":false}"""),
                Tool("get_exception_info", "Read exception information for the observed stopped context and optional thread.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("get_modules", "Read the bounded module event inventory for the implicit session.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("get_progress", "Read the bounded progress event inventory for the implicit session.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("get_loaded_sources", "Query and refresh loaded sources when the debugger supports it.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("get_step_in_targets", "Query targets for the selected stopped frame; mux ownership is required.", """{"type":"object","properties":{"frame_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("step_over", "Step one stopped thread; uncertain outcomes are not retried.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"]}},"additionalProperties":false}"""),
                Tool("step_into", "Step into using a stopped thread; target_id requires the selected stopped context. Uncertain outcomes are not retried.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"target_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("step_out", "Step out using a stopped thread; uncertain outcomes are not retried.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("pause_execution", "Request pause; acknowledgement does not itself prove a stopped event.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("get_output", "Read captured output. Clearing the whole buffer requires mux ownership; filtered reads do not clear it.", """{"type":"object","properties":{"clear":{"type":"boolean"},"category":{"type":["string","null"]}},"additionalProperties":false}"""),
                Tool("get_output_tail", "Read the tail of captured output.", """{"type":"object","properties":{"lines":{"type":"integer","minimum":-9223372036854775808,"maximum":9223372036854775807},"category":{"type":["string","null"]}},"additionalProperties":false}"""),
                Tool("continue_execution", "Continue one stopped thread and observe the next stop, terminal event, or timeout; uncertain writes are not retried.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"]}},"additionalProperties":false}"""),
                NativeCodeSearchCatalog.Definition("find_code_symbol"),
                NativeCodeSearchCatalog.Definition("find_code_references"),
                NativeCodeSearchCatalog.Definition("get_source_context"),
                Tool("stop_debug", "Stop the implicit session and release its owner.", """{"type":"object","properties":{},"additionalProperties":false}"""),
            ],
        };


        internal static ListToolsResult List() => new()
        {
            Tools =
            [
                Tool("start_debug", "Start debugging with optional project profile and pre-build. Pre-build requires buildProject and refuses while another native session or start is active. Unix cleanup controls a process group, not escaped descendants. Non-ASCII environment data may be corrupted by some Windows debugger builds. Initial breakpoint verification is not a breakpoint-hit report.", """{"type":"object","properties":{"program":{"type":"string","minLength":1},"breakpoints":{"type":"array","maxItems":256,"items":{"type":"object","properties":{"file":{"type":"string","minLength":1},"line":{"type":"integer","minimum":1,"maximum":2147483647}},"required":["file","line"],"additionalProperties":false}},"args":{"type":["array","null"],"items":{"type":"string"}},"cwd":{"type":["string","null"]},"stopAtEntry":{"type":["boolean","null"]},"stop_at_entry":{"type":["boolean","null"]},"justMyCode":{"type":["boolean","null"]},"env":{"type":["object","null"],"additionalProperties":{"type":["string","null"]}},"projectRoot":{"type":["string","null"],"minLength":1},"launchProfile":{"type":["string","null"]},"launch_profile":{"type":["string","null"]},"preBuild":{"type":["boolean","null"]},"pre_build":{"type":["boolean","null"]},"buildProject":{"type":["string","null"],"minLength":1},"build_project":{"type":["string","null"],"minLength":1},"buildConfiguration":{"type":["string","null"],"enum":["Debug","Release",null]},"build_configuration":{"type":["string","null"],"enum":["Debug","Release",null]}},"allOf":[{"not":{"required":["stopAtEntry","stop_at_entry"]}},{"not":{"required":["launchProfile","launch_profile"]}},{"not":{"required":["preBuild","pre_build"]}},{"not":{"required":["buildProject","build_project"]}},{"not":{"required":["buildConfiguration","build_configuration"]}}],"additionalProperties":false}"""),
                Tool("get_debug_state", "Get the state of a debug session.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":32}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("stop_debug", "Stop a debug session.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":32}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("get_threads", "Get threads in a debug session.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":1}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("get_call_stack", "Get a bounded stack-frame page for a stopped thread. Omit threadId or use null to select the current observed stopped thread; thread_id is an alternative spelling. Carry numeric IDs with an opted-in inspection context from the same observation.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1},"threadId":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"startFrame":{"type":"integer","minimum":0,"maximum":4294967295},"levels":{"type":"integer","minimum":1,"maximum":256},"includeInspectionContext":{"type":"boolean","default":false}},"required":["debugSessionId"],"additionalProperties":false,"allOf":[{"not":{"required":["threadId","thread_id"]}}]}"""),
                Tool("step_over", "Step over an explicit or current observed stopped thread. Omitted or null threadId selects the current thread; thread_id is an alternative spelling.", OptionalStoppedThreadSchema),
                Tool("get_scopes", "Get bounded scopes using a frame ID and inspection context from the same stopped observation. The context is process/session-local and expires on execution or inspection invalidation.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":1},\"inspectionContextId\":{\"type\":\"string\",\"minLength\":1,\"maxLength\":128,\"pattern\":\"\\\\S\"},\"frameId\":{\"type\":\"integer\",\"minimum\":-2147483648,\"maximum\":2147483647}},\"required\":[\"debugSessionId\",\"inspectionContextId\",\"frameId\"],\"additionalProperties\":false}"),
                Tool("get_variables", "Get bounded variables using a positive variablesReference and inspection context from the same stopped observation. Optional start and count are sent to the debugger; adapter support determines the result. Zero count requests all remaining children. A returned reference of zero is a leaf. Filtering is not supported.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1},"inspectionContextId":{"type":"string","minLength":1,"maxLength":128,"pattern":"\\S"},"variablesReference":{"type":"integer","minimum":1,"maximum":2147483647},"start":{"type":["integer","null"],"minimum":0,"maximum":2147483647},"count":{"type":["integer","null"],"minimum":0,"maximum":2147483647}},"required":["debugSessionId","inspectionContextId","variablesReference"],"additionalProperties":false}"""),
                Tool("continue_execution", "Continue from an explicit or current observed stopped thread; the request resumes all threads. Omitted or null threadId selects the current thread; thread_id is an alternative spelling. Observe the next stop or terminal event for up to 30 seconds.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"threadId":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"required":["debugSessionId"],"additionalProperties":false,"allOf":[{"not":{"required":["threadId","thread_id"]}}]}"""),
                Tool("pause_execution", "Request a pause while a session is live, including alongside a committed continue. Nonzero threadId selects that exact thread; omitted, null or zero selects the observed current thread or the first bounded discovered thread. Returns the adapter acknowledgement, not a stopped-state claim. Only a real stopped event authorizes inspection; uncertain writes are never retried.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"threadId":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"required":["debugSessionId"],"additionalProperties":false}"""),
                Tool("add_breakpoint", "Add a source breakpoint to a live debug session while preserving its requested source breakpoint sets. Verification is an adapter observation, not proof of a hit. Conditions and hit counts are not supported.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":1,\"pattern\":\"\\\\S\"},\"file\":{\"type\":\"string\",\"minLength\":1,\"pattern\":\"\\\\S\"},\"line\":{\"type\":\"integer\",\"minimum\":1,\"maximum\":2147483647}},\"required\":[\"debugSessionId\",\"file\",\"line\"],\"additionalProperties\":false}"),
                Tool("remove_breakpoint", "Remove every requested source breakpoint matching this file and line from a live debug session while preserving the remaining requested order. The source file need not still exist. An absent request returns removed:false without sending setBreakpoints; successful removal confirms adapter acknowledgement, not final Python wire parity.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":1,\"pattern\":\"\\\\S\"},\"file\":{\"type\":\"string\",\"minLength\":1,\"pattern\":\"\\\\S\"},\"line\":{\"type\":\"integer\",\"minimum\":1,\"maximum\":2147483647}},\"required\":[\"debugSessionId\",\"file\",\"line\"],\"additionalProperties\":false}"),
                Tool("configure_exceptions", "Replace exception breakpoint filters in a live session. Acknowledgement is not an exception stop; per-filter verification is reported only when supplied by the adapter. Uncertain updates are not retried.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"filters":{"type":["array","null"],"items":{"type":"string"}}},"required":["debugSessionId"],"additionalProperties":false}"""),
                Tool("ui_invoke", "Invoke one uniquely resolved exact AutomationId in this session's bound debuggee main window using InvokePattern only. Optional expectedTarget compares identities observed by ui_get_window_tree; it cannot select a different target. Success confirms only that the UIA call returned; never automatically retry an unknown outcome.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"automationId":{"type":"string","minLength":1,"maxLength":256,"pattern":"\\S"},"expectedTarget":{"type":"object","properties":{"processIdentity":{"type":"string","minLength":1,"pattern":"\\S"},"hwnd":{"type":"string","minLength":1,"pattern":"\\S"},"rootRuntimeId":{"type":"array","minItems":1,"maxItems":64,"items":{"type":"integer","minimum":-2147483648,"maximum":2147483647}},"elementRuntimeId":{"type":"array","minItems":1,"maxItems":64,"items":{"type":"integer","minimum":-2147483648,"maximum":2147483647}}},"required":["processIdentity","hwnd","rootRuntimeId","elementRuntimeId"],"additionalProperties":false}},"required":["debugSessionId","automationId"],"additionalProperties":false}"""),
                Tool("ui_invoke_observed", "Invoke one explicitly observed runtime-ID-selected element in the bound debuggee main window using InvokePattern only. Echo target identities from ui_get_window_tree and require the selected element's exact expectedAutomationId; other AutomationIds need not be unique. Complete bounded runtime-ID selection and commit-time checks fail closed. Runtime IDs may be reused and atomicity is unproven; never automatically retry an unknown outcome. No selector or input fallback.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"target":{"type":"object","properties":{"processIdentity":{"type":"string","minLength":1,"pattern":"\\S"},"hwnd":{"type":"string","minLength":1,"pattern":"\\S"},"rootRuntimeId":{"type":"array","minItems":1,"maxItems":64,"items":{"type":"integer","minimum":-2147483648,"maximum":2147483647}},"elementRuntimeId":{"type":"array","minItems":1,"maxItems":64,"items":{"type":"integer","minimum":-2147483648,"maximum":2147483647}}},"required":["processIdentity","hwnd","rootRuntimeId","elementRuntimeId"],"additionalProperties":false},"expectedAutomationId":{"type":"string","minLength":1,"maxLength":256,"pattern":"\\S"}},"required":["debugSessionId","target","expectedAutomationId"],"additionalProperties":false}"""),
                Tool("ui_get_window_tree", "Read a bounded preorder tree of only the bound debuggee's current main window without activation, focus, input, expansion or resume. Atomicity is unproven; partial/unreadable observations cannot prove absence or uniqueness. This is not Python all-window parity.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"maxDepth":{"type":"integer","minimum":0,"maximum":32,"default":3},"maxChildren":{"type":"integer","minimum":1,"maximum":256,"default":50}},"required":["debugSessionId"],"additionalProperties":false}"""),
                Tool("get_output", "Read retained debuggee output in DAP reader order. Category matching is case-sensitive. Clear consumes the snapshot only when category is omitted, null or empty; a nonempty category never clears the buffer.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":1,\"pattern\":\"\\\\S\"},\"clear\":{\"type\":\"boolean\",\"default\":false},\"category\":{\"type\":[\"string\",\"null\"],\"default\":null}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("get_output_tail", "Read retained output without clearing it. Filter categories before joining event fragments and splitting Python-style logical lines. Positive lines selects the last N, zero selects all, and negative lines skips the first N.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":1,\"pattern\":\"\\\\S\"},\"lines\":{\"type\":\"integer\",\"default\":50},\"category\":{\"type\":[\"string\",\"null\"],\"default\":null}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("search_output", "Search retained output without clearing it. The pattern uses case-insensitive, culture-invariant .NET regular expressions, not Python regex parity. Category filtering precedes Python-style line splitting. Up to 50 matches and their context are returned with the total count; timeouts and application content over 256 KiB fail without partial results.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"pattern":{"type":"string"},"contextLines":{"type":"integer","default":2},"category":{"type":["string","null"],"default":null}},"required":["debugSessionId","pattern"],"additionalProperties":false}""",
                    new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, DestructiveHint = false }),
                Tool("step_into", "Step into a stopped thread; an optional targetId requires the current inspectionContextId and adapter capability. Numeric IDs are not verified as previously issued.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1},"threadId":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"targetId":{"type":"integer","minimum":-2147483648,"maximum":2147483647},"target_id":{"type":"integer","minimum":-2147483648,"maximum":2147483647},"inspectionContextId":{"type":"string","minLength":1,"maxLength":128,"pattern":"\\S"}},"required":["debugSessionId"],"additionalProperties":false,"allOf":[{"not":{"required":["threadId","thread_id"]}},{"not":{"required":["targetId","target_id"]}},{"if":{"anyOf":[{"required":["targetId"]},{"required":["target_id"]}]},"then":{"required":["inspectionContextId"]},"else":{"not":{"required":["inspectionContextId"]}}}]}"""),
                Tool("step_out", "Step out of an explicit or current observed stopped thread. Completion requires acknowledgement and a later exact step stop; inspect the stack to establish the caller.", OptionalStoppedThreadSchema),
                Tool("evaluate_expression", "Evaluate an exact expression in an explicit frame and current inspection context using DAP watch. Expressions may execute code or mutate the debuggee; watch is not read-only. A positive result reference can be expanded with get_variables in the same context. Never automatically retry a failed or unknown outcome.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1},"inspectionContextId":{"type":"string","minLength":1,"maxLength":128,"pattern":"\\S"},"frameId":{"type":"integer","minimum":-2147483648,"maximum":2147483647},"expression":{"type":"string"}},"required":["debugSessionId","inspectionContextId","frameId","expression"],"additionalProperties":false}""",
                    new ToolAnnotations { ReadOnlyHint = false, IdempotentHint = false, DestructiveHint = true }),
                Tool("set_variable", "Set an exact named variable in an explicit container and current inspection context. This may execute code or mutate the debuggee. Old references expire; use the returned context with the new reference. Never automatically retry an uncertain outcome.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"},"inspectionContextId":{"type":"string","minLength":1,"maxLength":128,"pattern":"\\S"},"variablesReference":{"type":"integer","minimum":1,"maximum":2147483647},"name":{"type":"string"},"value":{"type":"string"}},"required":["debugSessionId","inspectionContextId","variablesReference","name","value"],"additionalProperties":false}""",
                    new ToolAnnotations { ReadOnlyHint = false, IdempotentHint = false, DestructiveHint = true }),
                Tool("get_exception_info", "Get bounded exception information for an explicit or current observed stopped thread when the adapter advertises support. A later stop is a different observation; no exception cache or automatic retry.", OptionalStoppedThreadStrictSessionSchema),
                Tool("get_modules", "Read insertion-ordered module events retained by this live session, preserving numeric and string IDs distinctly. No DAP request or OS module scan is made; unseen events are not inferred. Incomplete retention and snapshots over 256 KiB return fixed errors without truncation or closing the session.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"}},"required":["debugSessionId"],"additionalProperties":false}""",
                    new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, DestructiveHint = false }),
                Tool("get_progress", "Read insertion-ordered active progress from this live session's DAP reader. Initialization advertises progress reporting; only reported starts, updates and ends are reflected. No DAP request is made. Incomplete retention and snapshots over 256 KiB return fixed errors without truncation or closing the session.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"}},"required":["debugSessionId"],"additionalProperties":false}""",
                    new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, DestructiveHint = false }),
                Tool("get_loaded_sources", "Request the current loaded-source snapshot when the adapter advertises support. Duplicate normalized source identities replace earlier entries. The result is a snapshot, not a live event cache, and content above 256 KiB fails without partial results.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1,"pattern":"\\S"}},"required":["debugSessionId"],"additionalProperties":false}""",
                    new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, DestructiveHint = false }),
                Tool("get_stop_context", "Read the current stopped thread, up to five frames, optional first-scope locals and recent retained output. Defaults: includeVariables true, includeOutputTail 10. An exception stop requires adapter exceptionInfo support. Results are limited to 256 KiB and refused if the inspection epoch changes.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1},"includeVariables":{"type":"boolean"},"include_variables":{"type":"boolean"},"includeOutputTail":{"type":"integer","minimum":0,"maximum":100},"include_output_tail":{"type":"integer","minimum":0,"maximum":100}},"required":["debugSessionId"],"additionalProperties":false,"allOf":[{"not":{"required":["includeVariables","include_variables"]}},{"not":{"required":["includeOutputTail","include_output_tail"]}}]}""",
                    new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, DestructiveHint = false }),
                Tool("get_exception_context", "Read exception information and bounded frames and first-scope locals for the current exception stop. Defaults: maxFrames 10, includeVariablesForFrames 1, maxInnerExceptions 0. Optional inner-exception evaluation may execute debuggee code and mutate state; it is off by default and never retried. Results over 256 KiB or from stale epochs are refused.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1},"maxFrames":{"type":"integer","minimum":1,"maximum":20},"max_frames":{"type":"integer","minimum":1,"maximum":20},"includeVariablesForFrames":{"type":"integer","minimum":0,"maximum":5},"include_variables_for_frames":{"type":"integer","minimum":0,"maximum":5},"maxInnerExceptions":{"type":"integer","minimum":0,"maximum":5},"max_inner_exceptions":{"type":"integer","minimum":0,"maximum":5}},"required":["debugSessionId"],"additionalProperties":false,"allOf":[{"not":{"required":["maxFrames","max_frames"]}},{"not":{"required":["includeVariablesForFrames","include_variables_for_frames"]}},{"not":{"required":["maxInnerExceptions","max_inner_exceptions"]}}]}""",
                    new ToolAnnotations { ReadOnlyHint = false, IdempotentHint = false, DestructiveHint = true }),
                .. NativeSceneToolDispatcher.ListTools(),
                NativeCodeSearchCatalog.Definition("find_code_symbol"),
                NativeCodeSearchCatalog.Definition("find_code_references"),
                NativeCodeSearchCatalog.Definition("get_source_context"),
                Tool("get_step_in_targets", "Get bounded adapter step-in targets for an explicit frame and current inspection context when supported. IDs are adapter observations, not proof of later validity.", """{"type":"object","properties":{"debugSessionId":{"type":"string","minLength":1},"inspectionContextId":{"type":"string","minLength":1,"maxLength":128,"pattern":"\\S"},"frameId":{"type":"integer","minimum":-2147483648,"maximum":2147483647}},"required":["debugSessionId","inspectionContextId","frameId"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, DestructiveHint = false }),
            ],
            TimeToLive = CacheLifetime,
            CacheScope = CacheScope.Public,
        };

        private static Tool Tool(string name, string description, string schema, ToolAnnotations? annotations = null) => new()
        {
            Name = name,
            Description = description,
            InputSchema = JsonDocument.Parse(schema).RootElement.Clone(),
            Annotations = annotations,
        };
    }

    private sealed class SessionDisposer(DebugSessionRegistry sessions) : IHostedService
    {
        public Task StartAsync(CancellationToken cancellationToken) => Task.CompletedTask;

        public async Task StopAsync(CancellationToken cancellationToken)
        {
            await sessions.CloseLegacyResourcesAsync().ConfigureAwait(false);
            await sessions.DisposeAsync(cancellationToken).ConfigureAwait(false);
        }
    }
}
