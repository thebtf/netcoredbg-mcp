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
                    return NativeCodeSearch.IsNativeTool(context.Params.Name) || context.Params.Name == "search_source"
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
                        && name is "pause_execution" or "quick_evaluate" or "get_modules" or "get_progress" or "get_loaded_sources" or "configure_exceptions" or "set_variable" or "search_output" or "get_stop_context" or "get_exception_context"
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
                    new Resource { Name = "debug_breakpoints_resource", Uri = "debug://breakpoints", MimeType = "application/json" },
                    new Resource { Name = "debug_state_resource", Uri = "debug://state", MimeType = "application/json" },
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
        private const string AttachDebug = "attach_debug";
        private const string GetDebugState = "get_debug_state";
        private const string DebuggeeActivity = "debuggee_activity";
        private const string StopDebug = "stop_debug";
        private const string CleanupProcesses = "cleanup_processes";
        private const string RestartDebug = "restart_debug";
        private const string TerminateDebug = "terminate_debug";
        private const string GetThreads = "get_threads";
        private const string GetCallStack = "get_call_stack";
        private const string GetScopes = "get_scopes";
        private const string GetStepInTargets = "get_step_in_targets";
        private const string GetVariables = "get_variables";
        private const string EvaluateExpression = "evaluate_expression";
        private const string QuickEvaluate = "quick_evaluate";
        private const string SetVariable = "set_variable";
        private const string ApplyCodeChange = "apply_code_change";
        private const string GetExceptionInfo = "get_exception_info";
        private const string GetStopContext = "get_stop_context";
        private const string GetExceptionContext = "get_exception_context";
        private const string GetModules = "get_modules";
        private const string GetProgress = "get_progress";
        private const string GetLoadedSources = "get_loaded_sources";
        private const string ReadMemory = "read_memory";
        private const string WriteMemory = "write_memory";
        private const string Disassemble = "disassemble";
        private const string GetLocations = "get_locations";
        private const string GetOutput = "get_output";
        private const string GetOutputTail = "get_output_tail";
        private const string GetBuildDiagnostics = "get_build_diagnostics";
        private const string VerifyDebugFreshness = "verify_debug_freshness";
        private const string InspectLaunchCompatibility = "inspect_debug_launch_compatibility";
        private const string RuntimeSmokeValidatePlan = "runtime_smoke_validate_plan";
        private const string RunRuntimeSmoke = "run_runtime_smoke";
        private const string SearchOutput = "search_output";
        private const string StepOver = "step_over";
        private const string StepInto = "step_into";
        private const string StepOut = "step_out";
        private const string ContinueExecution = "continue_execution";
        private const string PauseExecution = "pause_execution";
        private const string AddBreakpoint = "add_breakpoint";
        private const string RemoveBreakpoint = "remove_breakpoint";
        private const string AddFunctionBreakpoint = "add_function_breakpoint";
        private const string RemoveFunctionBreakpoint = "remove_function_breakpoint";
        private const string ListBreakpoints = "list_breakpoints";
        private const string ClearBreakpoints = "clear_breakpoints";
        private const string HygienePreflight = "debug_hygiene_preflight";
        private const string AddTracepoint = "add_tracepoint";
        private const string RemoveTracepoint = "remove_tracepoint";
        private const string GroupCreate = "instrumentation_group_create";
        private const string GroupInspect = "instrumentation_group_inspect";
        private const string GroupClear = "instrumentation_group_clear";
        private const string GetTraceLog = "get_trace_log";
        private const string ClearTraceLog = "clear_trace_log";
        private const string MarkTraceCursor = "mark_trace_cursor";
        private const string GetTraceDelta = "get_trace_delta";
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
        private readonly Dictionary<string, UtilitySnapshot> _utilitySnapshots = new(StringComparer.Ordinal);
        private readonly Queue<string> _utilitySnapshotOrder = new();
        private sealed record UtilitySnapshot(string Name, double Timestamp, string Frame, Dictionary<string, (string Value, string Type)> Variables);

        private readonly ConcurrentDictionary<string, NetCoreDbgSession> _sessions = new(StringComparer.Ordinal);
        private readonly ConcurrentDictionary<string, SessionSlot> _slots = new(StringComparer.Ordinal);
        private readonly object _lifetimeGate = new();
        private readonly Dictionary<string, OutputLifetime> _outputs = new(StringComparer.Ordinal);
        private readonly Dictionary<string, (SessionSlot Slot, OutputLifetime Output, NetCoreDbgSession Session)> _ownedSlots = new(StringComparer.Ordinal);
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
        private LegacyBuildSnapshot? _legacyBuildSnapshot;
        private string? _legacyBuildUnavailable;
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
        private readonly LegacyOutputEvidence _legacyOutputEvidence = new();
        private readonly LegacyRuntimeSmokeFacade _legacySmoke = new();
        private readonly SemaphoreSlim _legacyLifecycle = new(1, 1);
        private readonly SemaphoreSlim _legacyBreakpointGate = new(1, 1);
        // Identity survives option changes; incarnation fences observations from earlier revisions.
        private readonly List<(string File, int Line, long Incarnation, long Identity, string? Condition, string? HitCondition)> _legacyBreakpoints = [];
        private sealed class LegacyGroupBreakpoint(string file, int line, string? condition, string? hitCondition, long incarnation, long identity)
        {
            internal string File { get; } = file;
            internal int Line { get; } = line;
            internal string? Condition { get; } = condition;
            internal string? HitCondition { get; } = hitCondition;
            internal long Incarnation { get; } = incarnation;
            internal long Identity { get; } = identity;
            internal long ObservedIncarnation { get; set; }
            internal bool Verified { get; set; }
            internal int? DapLine { get; set; }
            internal int? Id { get; set; }
        }
        private sealed class LegacyInstrumentationGroup(string name)
        {
            internal string Name { get; } = name;
            internal List<LegacyGroupBreakpoint> Breakpoints { get; } = [];
            internal List<LegacyTracepoint> Tracepoints { get; } = [];
        }
        private readonly Dictionary<string, LegacyInstrumentationGroup> _legacyGroups = new(StringComparer.Ordinal);
        private sealed class LegacyTracepoint(string id, string file, int line, string expression, long incarnation, long identity)
        {
            internal string Id { get; } = id;
            internal string File { get; } = file;
            internal int Line { get; } = line;
            internal string Expression { get; } = expression;
            internal long Incarnation { get; } = incarnation;
            internal long Identity { get; } = identity;
            internal long ObservedIncarnation { get; set; }
            internal int? DapLine { get; set; }
            internal int? BreakpointId { get; set; }
            internal bool Verified { get; set; }
            internal long HitCount { get; set; }
            internal double LastHit { get; set; } = double.NegativeInfinity;
        }
        private sealed record LegacyTraceEntry(double timestamp, string file, int line, string expression, string value, int thread_id, string tracepoint_id);
        private readonly List<LegacyTracepoint> _legacyTracepoints = [];
        private readonly Queue<LegacyTraceEntry> _legacyTraceLog = new();
        private long _legacyTraceCounter;
        private long _legacyTraceAppendCount;
        private long _legacyTraceDropCount;
        private bool _legacyTraceManagerCreated;
        private long _legacyTraceRevision;
        private string? _legacyTraceLastContext;
        private bool _legacyTracePauseVeto;
        private long _legacyTracePauseRevision;
        private TaskCompletionSource<bool>? _legacyTraceResumePending;
        private readonly List<LegacyFunctionBreakpoint> _legacyFunctionBreakpoints = [];
        private long _legacyBreakpointIncarnation;
        private readonly Dictionary<long, LegacyBreakpointObservation> _legacyBreakpointObservations = [];
        private bool _legacyBreakpointObservationsIncomplete;
        private NetCoreDbgSession? _legacyBreakpointObserverSession;
        private readonly StringComparer _legacyBreakpointPathComparer = OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;
        private readonly HashSet<string> _legacyUnconfirmedBreakpointFiles = new(OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
        private readonly HashSet<string> _legacyGroupUnconfirmedFiles = new(OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
        private bool _legacyUnconfirmedBreakpointOverflow;
        private string? _legacyBreakpointSessionId;
        private long _legacySessionGeneration;
        private bool _legacyStarting;
        private bool _legacyStopping;
        private bool _legacyHasOwner;
        private int _legacyCompositeEvaluations;
        private string? _legacySessionId;
        private NetCoreDbgSession? _legacyAttachSession;
        private SessionSlot? _legacyAttachSlot;
        private bool _legacyAttachClosing;
        private Dictionary<string, JsonElement>? _legacyLaunchConfig;
        private LegacyResolvedLaunch? _legacyResolvedLaunch;
        private sealed record LegacyResolvedLaunch(string ProjectRoot, string Program, DapLaunchProfileResult Profile);
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
                internal void Observe(string eventName) => owner.Signal(this, eventName == "state" ? 8 : eventName == "output" ? 1 : eventName == "breakpoint" ? 4 : 2);
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
                    if (_subscriptions.Contains("debug://state")) generation.Pending |= 8;
                    Schedule();
                }
            }

            internal void SignalSession(string sessionId, string name)
            {
                lock (_gate)
                {
                    if (_current is { Published: true } current && StringComparer.Ordinal.Equals(current.SessionId, sessionId))
                        SignalLocked(current, name == "state" ? 8 : name == "output" ? 1 : name == "breakpoint" ? 4 : 2);
                }
            }

            internal void SignalBreakpoints()
            {
                lock (_gate)
                {
                    if (_closed) return;
                    _current ??= new Generation(this) { Published = true };
                    SignalLocked(_current, 4);
                }
            }

            private void Signal(Generation generation, int bits)
            {
                lock (_gate) SignalLocked(generation, bits);
            }

            private void SignalLocked(Generation generation, int bits)
            {
                if (_closed || !ReferenceEquals(_current, generation)) return;
                var uri = bits == 1 ? "debug://output" : bits == 2 ? "debug://threads" : bits == 4 ? "debug://breakpoints" : "debug://state";
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
                            bit = (generation.Pending & 1) != 0 ? 1 : (generation.Pending & 2) != 0 ? 2 : (generation.Pending & 4) != 0 ? 4 : 8;
                            generation.Pending &= ~bit;
                            uri = bit == 1 ? "debug://output" : bit == 2 ? "debug://threads" : bit == 4 ? "debug://breakpoints" : "debug://state";
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
                if (uri is not ("debug://output" or "debug://threads" or "debug://breakpoints" or "debug://state"))
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
                            var bit = uri == "debug://output" ? 1 : uri == "debug://threads" ? 2 : uri == "debug://breakpoints" ? 4 : 8;
                            _carry &= ~bit;
                            if (_current is { } current) current.Pending &= ~bit;
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
                    SignalLocked(generation, 8);
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
            if (uri is not ("debug://output" or "debug://threads" or "debug://breakpoints" or "debug://state"))
                throw new McpProtocolException($"Unknown resource: {uri}", McpErrorCode.InvalidParams);
            await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            string? sessionId;
            try
            {
                if (uri == "debug://state")
                {
                    var text = SerializeLegacyResourceState();
                    return new ReadResourceResult { Contents = [new TextResourceContents { Uri = uri, MimeType = "application/json", Text = text }] };
                }
                if (uri == "debug://breakpoints")
                {
                    var snapshot = WithLegacyBreakpointState(() =>
                    {
                        if (!LegacyBreakpointObservationsComplete())
                            throw new McpProtocolException("Breakpoint observations are unavailable; requested breakpoints are retained.", McpErrorCode.InvalidRequest);
                        return LegacyBreakpointSnapshot(null, includeHits: false);
                    });
                    return new ReadResourceResult { Contents = [new TextResourceContents { Uri = uri, MimeType = "application/json", Text = JsonSerializer.Serialize(snapshot) }] };
                }
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

        private string SerializeLegacyResourceState()
        {
            if (_legacyStarting || _legacyStopping)
                throw new McpProtocolException("Debug session transition is in progress.", McpErrorCode.InvalidRequest);
            var idle = _legacySessionId is null;
            lock (_lifetimeGate)
            {
                if (_startCleanupFailure is not null)
                    throw new McpProtocolException("Debug session cleanup is unconfirmed.", McpErrorCode.InvalidRequest);
            }
            NetCoreDbgSession? session = null;
            if (!idle && (!_sessions.TryGetValue(_legacySessionId!, out session) || !_isUsable(session)))
                throw new McpProtocolException("Debug session state is unavailable.", McpErrorCode.InvalidRequest);

            var state = idle ? default : session!.GetLegacyResourceState();
            if (!idle && !state.Complete)
                throw new McpProtocolException("Loaded source state is incomplete.", McpErrorCode.InvalidRequest);
            if (!idle && !state.DetailsComplete)
                throw new McpProtocolException("Debug event details are incomplete.", McpErrorCode.InvalidRequest);
            JsonNode modules = new JsonArray();
            var progress = new JsonObject();
            if (!idle)
            {
                var moduleSnapshot = session!.GetRuntimeInventory(true, MaximumSerializedInspectionBytes);
                var progressSnapshot = session.GetRuntimeInventory(false, MaximumSerializedInspectionBytes);
                if (moduleSnapshot.Status != DapRuntimeInventoryStatus.Success || progressSnapshot.Status != DapRuntimeInventoryStatus.Success)
                    throw new McpProtocolException("Runtime inventory state is unavailable or too large.", McpErrorCode.InvalidRequest);
                modules = JsonNode.Parse(moduleSnapshot.SerializedContent!)!["modules"]!.DeepClone();
                foreach (var entry in JsonNode.Parse(progressSnapshot.SerializedContent!)!["progress"]!.AsArray())
                {
                    var item = entry!.DeepClone().AsObject();
                    var id = item["progressId"]!.GetValue<string>();
                    item["progress_id"] = id;
                    item["started_at"] = item["startedAt"]!.DeepClone();
                    progress[id] = item;
                }
            }
            var lifecycle = idle ? (State: "idle", ThreadId: (int?)null, Reason: (string?)null, ExitCode: (int?)null)
                : ProjectLegacyState(state.State);
            var execution = lifecycle.State == "stopped" ? lifecycle.Reason switch
            {
                "breakpoint" or "function breakpoint" or "data breakpoint" or "entry" => "stopped-at-breakpoint",
                "exception" => "stopped-at-exception",
                "step" => "stepping",
                "pause" => "stopped-at-pause",
                _ => "stopped-other",
            } : lifecycle.State;
            var frame = !idle && _legacyInspectionSessionId == _legacySessionId && _legacyInspectionContextId is { } context
                && session!.CaptureInspectionState(context) is not null ? _legacyCurrentFrameId : null;
            var payload = new
            {
                state = lifecycle.State,
                execState = execution,
                currentThreadId = lifecycle.ThreadId,
                stopReason = lifecycle.Reason,
                threads = idle ? Array.Empty<DapThread>() : state.Threads,
                threadCount = idle ? 0 : (int?)state.Threads?.Length,
                currentFrameId = frame,
                exitCode = lifecycle.ExitCode,
                exceptionInfo = idle ? null : state.ExceptionInfo,
                processId = idle ? null : state.ProcessId,
                processName = idle ? null : state.ProcessName,
                debuggeePid = idle ? null : state.ProcessId,
                debuggeeAlive = idle ? (bool?)false : null,
                lastResumeAt = idle ? null : state.LastResumeAt,
                lastStopAt = idle ? null : state.LastStopAt,
                modules,
                loadedSources = idle ? new Dictionary<string, DapLoadedSource>() : state.Sources,
                activeProgress = progress,
                stopDescription = idle ? null : state.Description,
                stopText = idle ? null : state.Text,
                lastInvalidation = idle ? null : state.Invalidation,
                lastMemoryEvent = idle ? null : state.MemoryEvent,
            };
            var serialized = JsonSerializer.Serialize(payload);
            if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes)
                throw new McpProtocolException("Debug state resource is too large.", McpErrorCode.InvalidRequest);
            return serialized;
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
            if (name is not (StartDebug or AttachDebug or GetDebugState or GetThreads or GetCallStack or StepOver or StepInto or StepOut
                or PauseExecution or ContinueExecution or StopDebug or CleanupProcesses or RestartDebug or TerminateDebug or GetOutput or GetOutputTail or GetBuildDiagnostics or SearchOutput or DebuggeeActivity or VerifyDebugFreshness or "output_checkpoint" or "output_assert_since"
                or AddBreakpoint or RemoveBreakpoint or AddFunctionBreakpoint or RemoveFunctionBreakpoint or ListBreakpoints or ClearBreakpoints or HygienePreflight or AddTracepoint or RemoveTracepoint or GroupCreate or GroupInspect or GroupClear or GetTraceLog or ClearTraceLog or MarkTraceCursor or GetTraceDelta or GetScopes or GetVariables or EvaluateExpression or QuickEvaluate
                or SetVariable or ApplyCodeChange or ConfigureExceptions or GetExceptionInfo or GetStopContext or GetExceptionContext or GetModules or GetProgress or GetLoadedSources or GetStepInTargets or ReadMemory or WriteMemory or Disassemble or GetLocations or InspectLaunchCompatibility or RuntimeSmokeValidatePlan or RunRuntimeSmoke
                or "create_snapshot" or "list_snapshots" or "diff_snapshots" or "analyze_collection" or "summarize_object") && !LegacyRuntimeSmokeFacade.IsTool(name))
                return UnknownTool(name);
            var arguments = context.Params.Arguments is null
                ? new Dictionary<string, JsonElement>(StringComparer.Ordinal)
                : new Dictionary<string, JsonElement>(context.Params.Arguments, StringComparer.Ordinal);
            string? encRoot = null;
            string? encFile = null;
            List<LegacyEncEdit>? encEdits = null;
            if (name == ApplyCodeChange)
            {
                if (!TryReadLegacyEncArguments(arguments, out encFile, out encEdits))
                    return LegacyError("Invalid source file or edits.");
                encRoot = await ProjectRootResolver.ResolveAsync(context.Server, cancellationToken,
                    requestProtocolVersion: context.JsonRpcRequest.Context?.ProtocolVersion).ConfigureAwait(false);
                if (encRoot is null) return LegacyError("Project root is unavailable.");
            }
            var cleanupRequested = name == CleanupProcesses;
            var cleanupForce = false;
            LegacyCleanupSnapshot? cleanupObservations = null;
            if (cleanupRequested)
            {
                if (arguments.Keys.Any(static key => key != "force") ||
                    (arguments.TryGetValue("force", out var force) && force.ValueKind is not (JsonValueKind.True or JsonValueKind.False)))
                    return LegacyError("Unsupported or invalid argument.", "unknown");
                cleanupForce = arguments.TryGetValue("force", out force) && force.ValueKind == JsonValueKind.True;
                if (cleanupForce) { name = StopDebug; arguments.Clear(); }
            }
            string? breakpointFile = null;
            var breakpointSourceValid = false;
            long breakpointGeneration = 0;
            if (name is StartDebug or AttachDebug)
            {
                await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                try
                {
                    if (_legacyBreakpointSessionId is not null)
                        return LegacyError("A breakpoint update is still settling.");
                }
                finally { _legacyGate.Release(); }
            }
            if (name is AddBreakpoint or RemoveBreakpoint or AddFunctionBreakpoint or RemoveFunctionBreakpoint or ListBreakpoints or ClearBreakpoints or HygienePreflight or AddTracepoint or RemoveTracepoint)
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
                    breakpointSourceValid = NetCoreDbgSession.TryNormalizeLegacyBreakpointSource(source.GetString()!, out breakpointFile, requireExistingFile: name is AddBreakpoint or AddTracepoint);
                else if (name is (ListBreakpoints or ClearBreakpoints or HygienePreflight) && (!arguments.ContainsKey("file") || source.ValueKind == JsonValueKind.Null || (source.ValueKind == JsonValueKind.String && source.GetString() == string.Empty)))
                    breakpointSourceValid = true;
                cancellationToken.ThrowIfCancellationRequested();
            }
            string? hygieneValidationError = null;
            if (name == HygienePreflight && arguments.TryGetValue("file", out var hygieneFile)
                && hygieneFile.ValueKind == JsonValueKind.String && !string.IsNullOrEmpty(hygieneFile.GetString()))
            {
                var root = await ProjectRootResolver.ResolveAsync(context.Server, cancellationToken,
                    requestProtocolVersion: context.JsonRpcRequest.Context?.ProtocolVersion).ConfigureAwait(false);
                if (root is null || !NetCoreDbgSession.TryNormalizeLegacyBreakpointSource(root, out var normalizedRoot, requireExistingFile: false))
                    hygieneValidationError = "Project root unavailable.";
                else
                {
                    string candidate;
                    try { candidate = Path.GetFullPath(hygieneFile.GetString()!, normalizedRoot); }
                    catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException)
                    { candidate = string.Empty; }
                    if (candidate.Length == 0 || !NetCoreDbgSession.TryNormalizeLegacyBreakpointSource(candidate, out var normalizedFile, requireExistingFile: false))
                        hygieneValidationError = "Invalid file scope.";
                    else
                    {
                        var prefix = Path.EndsInDirectorySeparator(normalizedRoot) ? normalizedRoot : normalizedRoot + Path.DirectorySeparatorChar;
                        var comparison = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
                        if (!normalizedFile.StartsWith(prefix, comparison)) hygieneValidationError = "Path outside project root.";
                        else { breakpointFile = normalizedFile; breakpointSourceValid = true; }
                    }
                }
            }
            var lifecycleHeld = false;
            var gateHeld = false;
            var breakpointHeld = false;
            var compositeEvaluationHeld = false;
            var smokeDrainPending = false;
            TaskCompletionSource<bool>? traceResumePending = null;
            var traceResumeRevision = 0L;
            try
            {
                if (name is StartDebug or AttachDebug or StopDebug or RestartDebug or TerminateDebug)
                {
                    await _legacyLifecycle.WaitAsync(cancellationToken).ConfigureAwait(false);
                    lifecycleHeld = true;
                }
                await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                gateHeld = true;
                if (HasInvalidLegacyArguments(name, arguments))
                    return LegacyError("Unsupported or invalid argument.");
                LegacyDebugFreshness.Arguments? freshnessArguments = null;
                if (name == VerifyDebugFreshness && !LegacyDebugFreshness.TryParse(arguments, out freshnessArguments))
                    return LegacyError("Unsupported or invalid argument.");
                if (name is (AddBreakpoint or RemoveBreakpoint or AddFunctionBreakpoint or RemoveFunctionBreakpoint or ListBreakpoints or ClearBreakpoints or HygienePreflight or AddTracepoint or RemoveTracepoint) && breakpointGeneration != _legacySessionGeneration)
                    return LegacyError("Debug session changed before breakpoint admission.", "unknown");
                if (_legacyBreakpointSessionId is not null
                    && (name is StartDebug or AttachDebug or RestartDebug || (name is (AddBreakpoint or RemoveBreakpoint or AddFunctionBreakpoint or RemoveFunctionBreakpoint or ClearBreakpoints or HygienePreflight or AddTracepoint or RemoveTracepoint) && _legacySessionId is null)))
                    return LegacyError("A breakpoint update is still settling.");
                var consumesOutput = name == GetOutput && arguments.TryGetValue("clear", out var clearOption)
                    && clearOption.ValueKind == JsonValueKind.True
                    && (!arguments.TryGetValue("category", out var categoryOption) || categoryOption.ValueKind == JsonValueKind.Null
                        || (categoryOption.ValueKind == JsonValueKind.String && categoryOption.GetString() == string.Empty));
                var compositeMayEvaluate = name == GetExceptionContext
                    && (!arguments.TryGetValue("max_inner_exceptions", out var innerDepth)
                        || !TryReadOutputLineCount(innerDepth, out var depth) || depth != 0);
                var mutation = name is StartDebug or AttachDebug or StopDebug or RestartDebug or TerminateDebug or StepOver or StepInto or StepOut or PauseExecution
                    or ContinueExecution or AddBreakpoint or RemoveBreakpoint or AddFunctionBreakpoint or RemoveFunctionBreakpoint or ClearBreakpoints or HygienePreflight or AddTracepoint or RemoveTracepoint or GroupCreate or GroupInspect or GroupClear or ClearTraceLog or EvaluateExpression or QuickEvaluate or SetVariable or ApplyCodeChange or WriteMemory or ConfigureExceptions or GetStepInTargets
                    or "create_snapshot" or "analyze_collection" or "summarize_object" or "output_checkpoint" or "output_assert_since" or RunRuntimeSmoke || LegacyRuntimeSmokeFacade.RequiresOwner(name) || consumesOutput || compositeMayEvaluate;
                var raw = context.JsonRpcRequest.Params as JsonObject;
                var mux = raw?["_meta"]?["muxSessionId"];
                if (mux is not null && (mux is not JsonValue muxValue || !muxValue.TryGetValue<string>(out _)))
                    return LegacyError("Invalid muxSessionId.");
                var muxId = mux?.GetValue<string>();
                if (name is (StartDebug or AttachDebug) && _legacySessionId is not null)
                    return LegacyError("A debug session is already active; stop it before starting another.");
                if (mutation && _legacyStarting)
                    return LegacyError("A debug session is starting.");
                if (mutation && _legacyStopping)
                    return LegacyError("A debug session is stopping.");
                if ((mutation || (name is (DebuggeeActivity or VerifyDebugFreshness) && muxId is not null)) && name is not (StartDebug or AttachDebug) && _legacySessionId is not null)
                {
                    var timeout = 60.0;
                    var configured = Environment.GetEnvironmentVariable("NETCOREDBG_SESSION_TIMEOUT");
                    if (configured is not null && (!double.TryParse(configured, System.Globalization.NumberStyles.Float,
                        System.Globalization.CultureInfo.InvariantCulture, out timeout) || !double.IsFinite(timeout) || timeout < 0))
                        return LegacyError("Invalid NETCOREDBG_SESSION_TIMEOUT.");
                    if (!_legacyAttachClosing && !(_legacyAttachSlot?.IsClosing ?? false) && _legacyHasOwner && _legacyOwner is not null && _legacyBreakpointSessionId != _legacySessionId
                        && _legacyCompositeEvaluations == 0 && Stopwatch.GetElapsedTime(_legacyOwnerActivity).TotalSeconds > timeout)
                    {
                        _legacyHasOwner = false;
                        _legacyOwner = null;
                    }
                    if (_legacyHasOwner && _legacyOwner != muxId && !(name is (DebuggeeActivity or VerifyDebugFreshness) && _legacyOwner is null))
                        return LegacyError("Debug session is owned by another agent.");
                    if (name != VerifyDebugFreshness && !LegacyRuntimeSmokeFacade.IsReadOnly(name))
                    {
                        _legacyHasOwner = true;
                        _legacyOwner = muxId;
                    }
                    if (muxId is not null && _legacyHasOwner && _legacyOwner == muxId) _legacyOwnerActivity = Stopwatch.GetTimestamp();
                }
                if (_legacyAttachClosing && mutation && name != StopDebug)
                    return LegacyError("Attach cleanup is unconfirmed; only owner stop can re-enter it.", "unknown");
                if (name == StopDebug)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    JsonObject? smokeStop = null;
                    _legacyStopping = true;
                    _legacyGate.Release();
                    gateHeld = false;
                    try { smokeStop = await _legacySmoke.StopActiveAsync(muxId, CancellationToken.None).ConfigureAwait(false); }
                    finally
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        _legacyStopping = false;
                    }
                    smokeDrainPending = smokeStop?["final"]?.GetValue<bool>() == false;
                }
                if (cleanupRequested)
                {
                    if (_legacyStarting || _legacyStopping) return LegacyError("A debug session transition is in progress.");
                    cleanupObservations = CaptureLegacyProcessEntries();
                    if (!cleanupForce) return LegacyProcessStatus(cleanupObservations);
                }
                if (_legacyAttachSession is not null && name == RestartDebug)
                    return LegacyError("An attached session cannot be restarted as a launch.");
                if (name == HygienePreflight)
                {
                    foreach (var flag in new[] { "clear_breakpoints", "clear_trace_log", "clear_exception_filters" })
                        if (arguments.TryGetValue(flag, out var value) && value.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                            return LegacyError("Unsupported or invalid argument.");
                    var clearPoints = !arguments.TryGetValue("clear_breakpoints", out var clearPointsValue) || clearPointsValue.ValueKind == JsonValueKind.True;
                    var clearLog = !arguments.TryGetValue("clear_trace_log", out var clearLogValue) || clearLogValue.ValueKind == JsonValueKind.True;
                    var clearFilters = arguments.TryGetValue("clear_exception_filters", out var clearFiltersValue) && clearFiltersValue.ValueKind == JsonValueKind.True;
                    var errors = new List<object>();
                    var remaining = new List<object>();
                    var clearedPoints = 0;
                    var clearedLog = 0;
                    var clearedFilters = 0;
                    var clearedTraces = 0;
                    CallToolResult HygieneResult(string status, string reason, string? validationError = null)
                    {
                        var data = new Dictionary<string, object?>
                        {
                            ["status"] = status,
                            ["reason"] = reason,
                            ["cleared"] = new { breakpoints = clearedPoints, trace_log_entries = clearedLog, exception_filters = clearedFilters },
                            ["remaining_breakpoints"] = remaining,
                            ["cleanup_errors"] = errors,
                        };
                        if (validationError is not null) data["validation_error"] = validationError;
                        if (clearedTraces != 0) data["tracepoints_removed"] = clearedTraces;
                        var state = LegacySnapshot().State;
                        return Result(new
                        {
                            state,
                            next_actions = LegacyActions(state).Append(HygienePreflight).Distinct(StringComparer.Ordinal).ToArray(),
                            message = status == "PASS" ? "Hygiene preflight passed." : "Hygiene preflight failed.",
                            data
                        }, false);
                    }
                    if (!breakpointSourceValid || hygieneValidationError is not null)
                        return HygieneResult("FAIL", "invalid file scope", hygieneValidationError ?? "Invalid file scope.");
                    if (clearLog)
                    {
                        clearedLog = _legacyTraceLog.Count;
                        _legacyTraceDropCount += clearedLog;
                        _legacyTraceLog.Clear();
                    }
                    bool InScope(string file) => breakpointFile is null || _legacyBreakpointPathComparer.Equals(file, breakpointFile);
                    var stoppedOnFailure = false;
                    if (clearPoints)
                    {
                        if (_legacySessionId is not null)
                        {
                            if (!_legacyBreakpointGate.Wait(0))
                                errors.Add(new { operation = "clear_breakpoints", error = "A breakpoint update is already in progress." });
                            else { breakpointHeld = true; _legacyBreakpointSessionId = _legacySessionId; }
                            stoppedOnFailure = !breakpointHeld;
                        }
                        var session = _legacySessionId;
                        async Task<bool> ApplyFileAsync(string file, string operation)
                        {
                            if (session is null) return true;
                            var lines = SnapshotLegacyBreakpointLines(file);
                            MarkLegacyBreakpointUnconfirmed(file);
                            var replacementRequest = LegacyRequest(RemoveBreakpoint, session, new Dictionary<string, JsonElement>
                            {
                                ["file"] = JsonSerializer.SerializeToElement(file),
                                ["line"] = JsonSerializer.SerializeToElement(1),
                            });
                            _legacyGate.Release();
                            gateHeld = false;
                            var update = await UpdateBreakpointAsync(replacementRequest, context.JsonRpcRequest.Id, cancellationToken, remove: true, lines).ConfigureAwait(false);
                            await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                            gateHeld = true;
                            if (_legacySessionId != session || update.IsError == true)
                            {
                                errors.Add(new { operation, error = "Breakpoint application was refused or is unconfirmed; do not retry automatically." });
                                return false;
                            }
                            _legacyUnconfirmedBreakpointFiles.Remove(file);
                            return true;
                        }
                        var directTraces = _legacyTracepoints.Where(point => InScope(point.File)
                            && !_legacyGroups.Values.Any(group => group.Tracepoints.Contains(point))).ToArray();
                        foreach (var point in directTraces)
                        {
                            if (stoppedOnFailure) break;
                            if (!IsLegacyTraceSourceConfirmed(point.File))
                            {
                                errors.Add(new { operation = "clear_tracepoints", error = "Breakpoint application is unconfirmed; do not retry automatically." });
                                stoppedOnFailure = true;
                                break;
                            }
                            _legacyTraceRevision++;
                            _legacyTracepoints.Remove(point);
                            _legacyResources.SignalBreakpoints();
                            if (!await ApplyFileAsync(point.File, "clear_tracepoints").ConfigureAwait(false)) { stoppedOnFailure = true; break; }
                            clearedTraces++;
                        }
                        var files = _legacyBreakpoints.Where(point => InScope(point.File)).Select(static point => point.File).Distinct(_legacyBreakpointPathComparer).ToArray();
                        foreach (var file in files)
                        {
                            if (stoppedOnFailure) break;
                            if (!IsLegacyTraceSourceConfirmed(file))
                            {
                                errors.Add(new { operation = "clear_breakpoints", error = "Breakpoint application is unconfirmed; do not retry automatically." });
                                stoppedOnFailure = true;
                                break;
                            }
                            var removed = WithLegacyBreakpointState(() =>
                            {
                                var matches = _legacyBreakpoints.Where(point => _legacyBreakpointPathComparer.Equals(point.File, file)).ToArray();
                                foreach (var point in matches) { _legacyBreakpointObservations.Remove(point.Incarnation); _legacyBreakpoints.Remove(point); }
                                if (matches.Length != 0) { _legacyTraceRevision++; _legacyResources.SignalBreakpoints(); }
                                return matches.Length;
                            });
                            if (!await ApplyFileAsync(file, "clear_breakpoints").ConfigureAwait(false)) { stoppedOnFailure = true; break; }
                            clearedPoints += removed;
                        }
                        if (!stoppedOnFailure && breakpointFile is null && _legacyFunctionBreakpoints.Count != 0)
                        {
                            ReconcileLegacyFunctionBreakpoints();
                            var functions = _legacyFunctionBreakpoints.ToArray();
                            _legacyFunctionBreakpoints.Clear();
                            if (session is not null && _legacyBreakpointObserverSession is { SupportsFunctionBreakpoints: true })
                            {
                                _legacyGate.Release(); gateHeld = false;
                                var update = await UpdateFunctionBreakpointsAsync(session, [], context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false);
                                await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false); gateHeld = true;
                                if (_legacySessionId != session || update is not { Outcome: DapBreakpointUpdateOutcome.Added or DapBreakpointUpdateOutcome.Unsupported })
                                {
                                    _legacyFunctionBreakpoints.AddRange(functions);
                                    errors.Add(new { operation = "clear_breakpoints", error = "Function breakpoint application was refused or is unconfirmed; do not retry automatically." });
                                    stoppedOnFailure = true;
                                }
                            }
                            if (!stoppedOnFailure) clearedPoints += functions.Length;
                        }
                    }
                    if (clearFilters && _legacySessionId is { } filterSession)
                    {
                        var filterRequest = LegacyRequest(ConfigureExceptions, filterSession, new Dictionary<string, JsonElement> { ["filters"] = JsonSerializer.SerializeToElement(Array.Empty<string>()) });
                        _legacyGate.Release(); gateHeld = false;
                        var update = await ConfigureExceptionsAsync(filterRequest, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false);
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false); gateHeld = true;
                        if (_legacySessionId != filterSession || update.IsError == true)
                            errors.Add(new { operation = "clear_exception_filters", error = "Exception filter clear failed or is unconfirmed; do not retry automatically." });
                        else clearedFilters = 1;
                    }
                    if (_legacyUnconfirmedBreakpointOverflow || _legacyUnconfirmedBreakpointFiles.Any(InScope) || _legacyGroupUnconfirmedFiles.Any(InScope))
                        errors.Add(new { operation = "clear_breakpoints", error = "Breakpoint application is unconfirmed; do not retry automatically." });
                    WithLegacyBreakpointState(() =>
                    {
                        foreach (var point in _legacyBreakpoints.Where(point => InScope(point.File)))
                        {
                            _legacyBreakpointObservations.TryGetValue(point.Incarnation, out var observed);
                            remaining.Add(new { file = point.File, line = point.Line, dap_line = observed?.DapLine, condition = point.Condition, verified = observed?.Verified ?? false });
                        }
                        foreach (var point in _legacyGroups.Values.SelectMany(static group => group.Breakpoints).Where(point => InScope(point.File)))
                            remaining.Add(new { file = point.File, line = point.Line, dap_line = point.DapLine, condition = point.Condition, verified = point.Verified });
                        foreach (var point in _legacyTracepoints.Where(point => InScope(point.File)))
                            remaining.Add(new { file = point.File, line = point.Line, dap_line = point.DapLine, condition = (string?)null, verified = point.Verified });
                        if (breakpointFile is null)
                        {
                            ReconcileLegacyFunctionBreakpoints();
                            foreach (var point in _legacyFunctionBreakpoints)
                                remaining.Add(new { function = point.Name, condition = point.Condition, hit_condition = point.HitCondition, verified = point.Verified });
                        }
                        return true;
                    });
                    if (remaining.Count != 0) return HygieneResult("FAIL", "targeted breakpoints remain after hygiene preflight");
                    if (errors.Count != 0) return HygieneResult("FAIL", "hygiene cleanup failed");
                    return HygieneResult("PASS", "no targeted breakpoints remain after hygiene preflight");
                }
                if (name is MarkTraceCursor or GetTraceDelta)
                    return LegacyTraceCursorOperation(name, arguments);
                if (name == GetTraceLog)
                {
                    double? since = null;
                    if (arguments.TryGetValue("since", out var sinceValue) && sinceValue.ValueKind != JsonValueKind.Null)
                    {
                        if (sinceValue.ValueKind != JsonValueKind.Number || !sinceValue.TryGetDouble(out var number) || !double.IsFinite(number))
                            return LegacyError("Invalid trace timestamp.");
                        since = number;
                    }
                    if (!TryReadOptionalLegacyBreakpointOption(arguments, "tracepoint_id", out var traceFilter))
                        return LegacyError("Invalid tracepoint ID.");
                    var entries = _legacyTraceLog.Where(entry => (since is null || entry.timestamp >= since)
                        && (traceFilter is null || entry.tracepoint_id == traceFilter)).ToArray();
                    return LegacyBoundedUtilityResponse(new { entries, total = entries.Length, truncated = _legacyTraceLog.Count >= 1000 }, LegacySnapshot().State);
                }
                if (name == ClearTraceLog)
                {
                    var cleared = _legacyTraceLog.Count;
                    _legacyTraceDropCount += cleared;
                    _legacyTraceLog.Clear();
                    return LegacyResponse(new { cleared });
                }
                if (name is GroupCreate or GroupInspect or GroupClear)
                {
                    if (!arguments.TryGetValue("name", out var groupNameValue) || groupNameValue.ValueKind != JsonValueKind.String
                        || groupNameValue.GetString() is not { } groupName)
                        return LegacyGroupFailure(string.Empty, "invalid group name");
                    if (name == GroupCreate && !ValidLegacyGroupName(groupName))
                        return LegacyGroupFailure(groupName, "invalid group name");
                    if (name == GroupCreate && _legacyGroups.ContainsKey(groupName))
                        return LegacyGroupFailure(groupName, "group already exists");
                    if (name != GroupCreate && !_legacyGroups.ContainsKey(groupName))
                        return LegacyGroupFailure(groupName, "group not found");
                    if (name == GroupInspect)
                        return WithLegacyBreakpointState(() => LegacyGroupResponse(_legacyGroups[groupName], inspect: true));
                    if (_legacyStarting || _legacyStopping || (_legacySessionId is null && _legacyBreakpointSessionId is not null))
                        return LegacyError("A breakpoint or lifecycle operation is still settling.");

                    LegacyInstrumentationGroup group;
                    if (name == GroupCreate)
                    {
                        if (!TryCreateLegacyGroup(groupName, arguments, out group))
                            return LegacyGroupFailure(groupName, "invalid or empty instrumentation group");
                        if (group.Breakpoints.Any(point => !LegacyOwnersCompatible(point.File, point.Line, group))
                            || group.Tracepoints.Any(point => !LegacyOwnersCompatible(point.File, point.Line, group)))
                            return LegacyGroupFailure(groupName, "unsupported overlapping breakpoint predicates or tracepoint");
                    }
                    else group = _legacyGroups[groupName];
                    var files = group.Breakpoints.Select(static point => point.File)
                        .Concat(group.Tracepoints.Select(static point => point.File)).Distinct(_legacyBreakpointPathComparer).ToArray();
                    if (files.Any(file => !IsLegacyGroupMutationSourceConfirmed(file)))
                        return LegacyError("Breakpoint application is unconfirmed; stop the session before changing this group.", "unknown");
                    if (_legacySessionId is not null)
                    {
                        if (!_legacyBreakpointGate.Wait(0)) return LegacyError("A breakpoint update is already in progress.");
                        breakpointHeld = true;
                        _legacyBreakpointSessionId = _legacySessionId;
                    }
                    cancellationToken.ThrowIfCancellationRequested();
                    _legacyTraceRevision++;
                    if (name == GroupCreate)
                    {
                        _legacyGroups.Add(groupName, group);
                        _legacyTracepoints.AddRange(group.Tracepoints);
                        if (group.Tracepoints.Count != 0) _legacyTraceManagerCreated = true;
                    }
                    if (_legacySessionId is { } groupSession)
                    {
                        var replacements = files.Select(file => (File: file,
                            Lines: SnapshotLegacyBreakpointLines(file, name == GroupClear ? group : null))).ToArray();
                        foreach (var file in files)
                        {
                            MarkLegacyBreakpointUnconfirmed(file);
                            _legacyGroupUnconfirmedFiles.Add(file);
                        }
                        _legacyGate.Release();
                        gateHeld = false;
                        CallToolResult? failed = null;
                        foreach (var replacement in replacements)
                        {
                            var groupRequest = LegacyRequest(name == GroupClear ? RemoveBreakpoint : AddBreakpoint, groupSession, new Dictionary<string, JsonElement>
                            {
                                ["file"] = JsonSerializer.SerializeToElement(replacement.File),
                                ["line"] = JsonSerializer.SerializeToElement(name == GroupClear ? 1 : replacement.Lines[0].Line),
                            });
                            var groupUpdate = await UpdateBreakpointAsync(groupRequest, context.JsonRpcRequest.Id, cancellationToken,
                                remove: name == GroupClear, replacement.Lines).ConfigureAwait(false);
                            if (groupUpdate.IsError == true) { failed = groupUpdate; break; }
                        }
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        if (_legacySessionId != groupSession || failed is not null)
                            return LegacyError("Group retained; breakpoint application is unconfirmed. Do not retry automatically.", "unknown");
                        foreach (var file in files)
                        {
                            _legacyGroupUnconfirmedFiles.Remove(file);
                            _legacyUnconfirmedBreakpointFiles.Remove(file);
                        }
                    }
                    if (name == GroupClear)
                    {
                        _legacyGroups.Remove(groupName);
                        foreach (var point in group.Tracepoints) _legacyTracepoints.Remove(point);
                        _legacyResources.SignalBreakpoints();
                        return LegacyGroupCleared(group);
                    }
                    _legacyResources.SignalBreakpoints();
                    return WithLegacyBreakpointState(() => LegacyGroupResponse(group, inspect: false));
                }
                if (name is AddTracepoint or RemoveTracepoint)
                {
                    LegacyTracepoint point;
                    if (name == AddTracepoint)
                    {
                        if (breakpointSourceValid && IsLegacyGroupSourceQuarantined(breakpointFile!))
                            return LegacyError("Breakpoint application is unconfirmed; stop the session before changing this source.", "unknown");
                        if (!breakpointSourceValid || !arguments.TryGetValue("line", out var traceLine)
                            || !TryReadOutputLineCount(traceLine, out var lineNumber) || lineNumber is < 1 or > int.MaxValue
                            || !arguments.TryGetValue("expression", out var expression) || expression.ValueKind != JsonValueKind.String
                            || expression.GetString() is not { } expressionText || Encoding.UTF8.GetByteCount(expressionText) > 4096)
                            return LegacyError("Invalid tracepoint source, line or expression.");
                        if (!LegacyOwnersCompatible(breakpointFile!, (int)lineNumber, addTrace: true))
                            return LegacyError("Unsupported overlapping group predicate and tracepoint.");
                        if (LegacyOwnedBreakpointCount() >= MaximumBreakpointCount)
                            return LegacyError("Breakpoint limit reached.");
                        var incarnation = ++_legacyBreakpointIncarnation;
                        var identity = LegacyPhysicalIdentity(breakpointFile!, (int)lineNumber) ?? incarnation;
                        point = new LegacyTracepoint($"tp-{++_legacyTraceCounter}", breakpointFile!, (int)lineNumber, expressionText, incarnation, identity);
                    }
                    else
                    {
                        if (!arguments.TryGetValue("tracepoint_id", out var idValue) || idValue.ValueKind != JsonValueKind.String
                            || _legacyTracepoints.FirstOrDefault(item => item.Id == idValue.GetString()) is not { } existing)
                            return LegacyError("Tracepoint not found.");
                        if (_legacyGroups.Values.Any(group => group.Tracepoints.Contains(existing)))
                            return LegacyError("Tracepoint belongs to an instrumentation group.");
                        point = existing;
                    }
                    if (IsLegacyGroupSourceQuarantined(point.File))
                        return LegacyError("Breakpoint application is unconfirmed; stop the session before changing this source.", "unknown");
                    if (_legacySessionId is not null)
                    {
                        if (!_legacyBreakpointGate.Wait(0)) return LegacyError("A breakpoint update is already in progress.");
                        breakpointHeld = true;
                        _legacyBreakpointSessionId = _legacySessionId;
                    }
                    cancellationToken.ThrowIfCancellationRequested();
                    _legacyTraceRevision++;
                    if (name == AddTracepoint) _legacyTraceManagerCreated = true;
                    if (name == AddTracepoint) _legacyTracepoints.Add(point); else _legacyTracepoints.Remove(point);
                    var requested = SnapshotLegacyBreakpointLines(point.File);
                    if (_legacySessionId is { } traceSession)
                    {
                        MarkLegacyBreakpointUnconfirmed(point.File);
                        var traceRequest = LegacyRequest(name == AddTracepoint ? AddBreakpoint : RemoveBreakpoint, traceSession, new Dictionary<string, JsonElement>
                        {
                            ["file"] = JsonSerializer.SerializeToElement(point.File),
                            ["line"] = JsonSerializer.SerializeToElement(point.Line),
                        });
                        _legacyGate.Release();
                        gateHeld = false;
                        var update = await UpdateBreakpointAsync(traceRequest, context.JsonRpcRequest.Id, cancellationToken, name == RemoveTracepoint, requested).ConfigureAwait(false);
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        if (_legacySessionId != traceSession || update.IsError == true)
                            return LegacyError("Tracepoint intent was retained; live breakpoint application is unconfirmed. Do not retry automatically.", "unknown");
                        _legacyUnconfirmedBreakpointFiles.Remove(point.File);
                        WithLegacyBreakpointState(() => true);
                    }
                    return name == AddTracepoint
                        ? LegacyResponse(new { id = point.Id, file = point.File, line = point.Line, dap_line = point.DapLine, expression = point.Expression, breakpoint_verified = point.Verified })
                        : LegacyResponse(new { removed = point.Id, was_active = true, hit_count = point.HitCount });
                }
                if (name is AddFunctionBreakpoint or RemoveFunctionBreakpoint)
                {
                    if (!arguments.TryGetValue("function_name", out var functionValue) || functionValue.ValueKind != JsonValueKind.String
                        || !TryReadOptionalLegacyBreakpointOption(arguments, "condition", out var condition)
                        || !TryReadOptionalLegacyBreakpointOption(arguments, "hit_condition", out var hitCondition))
                        return LegacyError("Invalid function breakpoint.");
                    var function = functionValue.GetString()!;
                    var index = _legacyFunctionBreakpoints.FindIndex(item => item.Name == function);
                    var previousFunction = index >= 0 ? _legacyFunctionBreakpoints[index] : null;
                    if (name == RemoveFunctionBreakpoint && index < 0) return LegacyResponse(new { removed = false, function });
                    if (name == AddFunctionBreakpoint && index < 0 && LegacyOwnedBreakpointCount() >= MaximumBreakpointCount)
                        return LegacyError("Breakpoint limit reached.");
                    if (name == AddFunctionBreakpoint && !LegacyFunctionOptionsFit(index, function, condition, hitCondition))
                        return LegacyError("Breakpoint options exceed the retained state limit.");
                    if (_legacySessionId is not null && (_legacyBreakpointObserverSession is null || !_legacyBreakpointObserverSession.SupportsFunctionBreakpoints))
                    {
                        if (name == AddFunctionBreakpoint) return LegacyError("DAP adapter does not support function breakpoints.");
                    }
                    if (_legacySessionId is not null)
                    {
                        if (!_legacyBreakpointGate.Wait(0)) return LegacyError("A breakpoint update is already in progress.");
                        breakpointHeld = true;
                        _legacyBreakpointSessionId = _legacySessionId;
                    }
                    cancellationToken.ThrowIfCancellationRequested();
                    _legacyTraceRevision++;
                    if (name == RemoveFunctionBreakpoint) _legacyFunctionBreakpoints.RemoveAt(index);
                    else
                    {
                        var point = new LegacyFunctionBreakpoint(function, condition, hitCondition, Incarnation: ++_legacyBreakpointIncarnation);
                        if (index < 0) _legacyFunctionBreakpoints.Add(point); else _legacyFunctionBreakpoints[index] = point;
                    }
                    if (_legacySessionId is not { } functionSession)
                        return name == AddFunctionBreakpoint ? LegacyResponse(new { function, condition, verified = false }) : LegacyResponse(new { removed = true, function });
                    var requestedFunctions = _legacyFunctionBreakpoints.ToArray();
                    _legacyGate.Release();
                    gateHeld = false;
                    var functionResult = await UpdateFunctionBreakpointsAsync(functionSession, requestedFunctions, context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false);
                    await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                    gateHeld = true;
                    if (functionResult is { Outcome: DapBreakpointUpdateOutcome.Refused })
                    {
                        if (name == RemoveFunctionBreakpoint) return LegacyResponse(new { removed = true, function });
                        if (previousFunction is null) _legacyFunctionBreakpoints.RemoveAll(item => item.Name == function);
                        else _legacyFunctionBreakpoints[index] = previousFunction;
                        return LegacyError("The debugger refused function breakpoints.");
                    }
                    if (functionResult is { Outcome: DapBreakpointUpdateOutcome.Unsupported })
                    {
                        if (name == RemoveFunctionBreakpoint) return LegacyResponse(new { removed = true, function });
                        if (previousFunction is null) _legacyFunctionBreakpoints.RemoveAll(item => item.Name == function);
                        else _legacyFunctionBreakpoints[index] = previousFunction;
                        return LegacyError("DAP adapter does not support function breakpoints.");
                    }
                    if (_legacySessionId != functionSession || functionResult is not { Outcome: DapBreakpointUpdateOutcome.Added })
                        return LegacyError("Function breakpoint outcome is unknown. Do not retry automatically.", "unknown");
                    ReconcileLegacyFunctionBreakpoints();
                    var verified = _legacyFunctionBreakpoints.FirstOrDefault(item => item.Name == function)?.Verified ?? false;
                    return name == AddFunctionBreakpoint ? LegacyResponse(new { function, condition, verified }) : LegacyResponse(new { removed = true, function });
                }
                if (name is ListBreakpoints or ClearBreakpoints)
                {
                    if (!breakpointSourceValid) return LegacyError("Breakpoint source unavailable.");
                    if (name == ListBreakpoints)
                    {
                        return WithLegacyBreakpointState(() =>
                        {
                            if (!LegacyBreakpointObservationsComplete())
                                return LegacyError("Breakpoint observations are unavailable; requested breakpoints are retained.");
                            if (_legacyBreakpointObserverSession is { } active && !active.LegacyHitCountsComplete)
                                return LegacyError(active.LegacyHitCountsUnavailable ? "Breakpoint hit counts are unavailable." : "Breakpoint hit counts are pending or unavailable.");
                            return LegacyResponse(LegacyBreakpointSnapshot(breakpointFile, includeHits: true));
                        });
                    }
                    if (breakpointFile is null ? _legacyGroupUnconfirmedFiles.Count != 0 : IsLegacyGroupSourceQuarantined(breakpointFile))
                        return LegacyError("Breakpoint application is unconfirmed; stop the session before clearing this source.", "unknown");
                    var files = new List<string>();
                    var clearLines = new Dictionary<string, LegacyRequestedLine[]>(_legacyBreakpointPathComparer);
                    var confirmedFiles = new List<string>();
                    var removedCount = 0;
                    LegacyFunctionBreakpoint[] removedFunctions = [];
                    var admission = WithLegacyBreakpointState<CallToolResult?>(() =>
                    {
                        foreach (var breakpoint in _legacyBreakpoints)
                        {
                            if (breakpointFile is not null && !_legacyBreakpointPathComparer.Equals(breakpoint.File, breakpointFile)) continue;
                            removedCount++;
                            if (!files.Contains(breakpoint.File, _legacyBreakpointPathComparer)) files.Add(breakpoint.File);
                        }
                        if (breakpointFile is null)
                        {
                            ReconcileLegacyFunctionBreakpoints();
                            removedFunctions = _legacyFunctionBreakpoints.ToArray();
                            removedCount += removedFunctions.Length;
                        }
                        if (removedCount == 0) return LegacyResponse(new { removed = 0 });
                        if (_legacySessionId is not null)
                        {
                            if (!_legacyBreakpointGate.Wait(0)) return LegacyError("A breakpoint update is already in progress.");
                            breakpointHeld = true;
                            _legacyBreakpointSessionId = _legacySessionId;
                        }
                        cancellationToken.ThrowIfCancellationRequested();
                        _legacyTraceRevision++;
                        for (var index = _legacyBreakpoints.Count - 1; index >= 0; index--)
                        {
                            var breakpoint = _legacyBreakpoints[index];
                            if (breakpointFile is not null && !_legacyBreakpointPathComparer.Equals(breakpoint.File, breakpointFile)) continue;
                            _legacyBreakpointObservations.Remove(breakpoint.Incarnation);
                            _legacyBreakpoints.RemoveAt(index);
                        }
                        foreach (var file in files) clearLines[file] = SnapshotLegacyBreakpointLines(file);
                        if (_legacySessionId is not null)
                            foreach (var file in files) MarkLegacyBreakpointUnconfirmed(file);
                        if (breakpointFile is null && _legacySessionId is null) _legacyFunctionBreakpoints.Clear();
                        if (!LegacyHasStoppingBreakpoints()) _legacyBreakpointObservationsIncomplete = false;
                        if (files.Count > 0) _legacyResources.SignalBreakpoints();
                        return null;
                    });
                    if (admission is not null) return admission;
                    if (_legacySessionId is not { } clearingSession) return LegacyResponse(new { removed = removedCount });
                    _legacyGate.Release();
                    gateHeld = false;
                    CallToolResult? failure = null;
                    foreach (var file in files)
                    {
                        var clearRequest = LegacyRequest(RemoveBreakpoint, clearingSession, new Dictionary<string, JsonElement>
                        {
                            ["file"] = JsonSerializer.SerializeToElement(file),
                            ["line"] = JsonSerializer.SerializeToElement(1),
                        });
                        var clearResult = await UpdateBreakpointAsync(clearRequest, context.JsonRpcRequest.Id, cancellationToken, remove: true, clearLines[file]).ConfigureAwait(false);
                        if (clearResult.IsError == true) { failure = clearResult; break; }
                        confirmedFiles.Add(file);
                    }
                    DapBreakpointUpdateResult? functionClear = null;
                    var functionClearRequired = false;
                    if (failure is null && removedFunctions.Length > 0)
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        if (_legacySessionId != clearingSession)
                            return LegacyError("Clear outcome is unknown. Do not retry automatically.", "unknown");
                        cancellationToken.ThrowIfCancellationRequested();
                        _legacyFunctionBreakpoints.Clear();
                        functionClearRequired = _legacyBreakpointObserverSession is { SupportsFunctionBreakpoints: true };
                        _legacyGate.Release();
                        gateHeld = false;
                        if (functionClearRequired)
                            functionClear = await UpdateFunctionBreakpointsAsync(clearingSession, [], context.JsonRpcRequest.Id, cancellationToken).ConfigureAwait(false);
                    }
                    await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                    gateHeld = true;
                    if (_legacySessionId == clearingSession)
                        foreach (var file in confirmedFiles) _legacyUnconfirmedBreakpointFiles.Remove(file);
                    if (functionClear is { Outcome: DapBreakpointUpdateOutcome.Refused })
                    {
                        _legacyFunctionBreakpoints.AddRange(removedFunctions);
                        return LegacyError("The debugger refused the function breakpoint clear; function entries were restored.");
                    }
                    if (_legacySessionId != clearingSession)
                        return LegacyError("Clear intent was retained; live application is unknown. Do not retry automatically.", "unknown");
                    if (failure is not null)
                    {
                        if (failure.StructuredContent is { } clearContent && clearContent.TryGetProperty("error", out var code) && code.GetString() == "DAP_SET_BREAKPOINTS_REFUSED")
                            return LegacyError("Clear intent was retained; the debugger rejected a file set. Remaining files were not sent; a later explicit update or start may reapply intent.");
                        return LegacyError("Clear intent was retained; live application is unknown and remaining files were not sent. Do not retry automatically.", "unknown");
                    }
                    if (functionClearRequired && functionClear is not { Outcome: DapBreakpointUpdateOutcome.Added or DapBreakpointUpdateOutcome.Unsupported })
                        return LegacyError("Function breakpoint clear outcome is unknown. Do not retry automatically.", "unknown");
                    return LegacyResponse(new { removed = removedCount });
                }
                var breakpointLine = 0;
                LegacyRequestedLine[]? breakpointLines = null;
                var breakpointRemoved = false;
                string? breakpointCondition = null;
                string? breakpointHitCondition = null;
                if (name is AddBreakpoint or RemoveBreakpoint)
                {
                    if (!TryReadOptionalLegacyBreakpointOption(arguments, "condition", out breakpointCondition)
                        || !TryReadOptionalLegacyBreakpointOption(arguments, "hit_condition", out breakpointHitCondition))
                        return LegacyError("Invalid breakpoint condition.");
                    if (!arguments.TryGetValue("line", out var line) || !TryReadOutputLineCount(line, out var lineNumber)
                        || lineNumber is < 1 or > int.MaxValue)
                        return LegacyError("Invalid breakpoint line.");
                    if (!breakpointSourceValid) return LegacyError("Breakpoint source unavailable.");
                    breakpointLine = (int)lineNumber;
                    if (IsLegacyGroupSourceQuarantined(breakpointFile!))
                        return LegacyError("Breakpoint application is unconfirmed; stop the session before changing this source.", "unknown");
                    if (name == AddBreakpoint && !LegacyOwnersCompatible(breakpointFile!, breakpointLine,
                        overrideDirect: true, directCondition: breakpointCondition, directHitCondition: breakpointHitCondition))
                        return LegacyError("Unsupported overlapping breakpoint predicates.");
                    arguments.Remove("condition");
                    arguments.Remove("hit_condition");
                    arguments["file"] = JsonSerializer.SerializeToElement(breakpointFile);
                    arguments["line"] = JsonSerializer.SerializeToElement(breakpointLine);
                    var admission = WithLegacyBreakpointState<CallToolResult?>(() =>
                    {
                        var index = FindLegacyBreakpoint(breakpointFile!, breakpointLine);
                        if (name == AddBreakpoint && index < 0 && LegacyOwnedBreakpointCount() >= MaximumBreakpointCount)
                            return LegacyError("Breakpoint limit reached.");
                        if (name == AddBreakpoint && !LegacyBreakpointOptionsFit(index, breakpointCondition, breakpointHitCondition))
                            return LegacyError("Breakpoint options exceed the retained state limit.");
                        if (_legacySessionId is null)
                        {
                            if (name == AddBreakpoint)
                            {
                                _legacyTraceRevision++;
                                ApplyLegacyBreakpoint(breakpointFile!, breakpointLine, false, breakpointCondition, breakpointHitCondition);
                                _legacyResources.SignalBreakpoints();
                                return LegacyResponse(new { file = breakpointFile, line = breakpointLine, condition = breakpointCondition, verified = false });
                            }
                            if (index >= 0) _legacyTraceRevision++;
                            var removed = ApplyLegacyBreakpoint(breakpointFile!, breakpointLine, true);
                            if (removed) _legacyResources.SignalBreakpoints();
                            return LegacyResponse(new { removed });
                        }
                        if (!_legacyBreakpointGate.Wait(0)) return LegacyError("A breakpoint update is already in progress.");
                        breakpointHeld = true;
                        _legacyBreakpointSessionId = _legacySessionId;
                        cancellationToken.ThrowIfCancellationRequested();
                        if (name == AddBreakpoint || index >= 0) _legacyTraceRevision++;
                        breakpointRemoved = ApplyLegacyBreakpoint(breakpointFile!, breakpointLine, name == RemoveBreakpoint, breakpointCondition, breakpointHitCondition);
                        if (name == AddBreakpoint || breakpointRemoved) _legacyResources.SignalBreakpoints();
                        if (name == RemoveBreakpoint && !breakpointRemoved) return LegacyResponse(new { removed = false });
                        MarkLegacyBreakpointUnconfirmed(breakpointFile!);
                        breakpointLines = SnapshotLegacyBreakpointLines(breakpointFile!);
                        return null;
                    });
                    if (admission is not null) return admission;
                }
                var restarting = name == RestartDebug;
                if (restarting)
                {
                    if (arguments.TryGetValue("rebuild", out var rebuildValue) && rebuildValue.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                        return LegacyError("Invalid rebuild.");
                    var rebuild = !arguments.TryGetValue("rebuild", out rebuildValue) || rebuildValue.ValueKind == JsonValueKind.True;
                    if (_legacyLaunchConfig is null) return LegacyError("No previous launch configuration for restart");
                    if (rebuild && (!_legacyLaunchConfig.TryGetValue("build_project", out var project)
                        || project.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(project.GetString())))
                        return LegacyError("Cannot rebuild on restart: no build_project in saved configuration");
                    cancellationToken.ThrowIfCancellationRequested();
                    JsonObject? smokeStop = null;
                    _legacyStopping = true;
                    _legacyGate.Release();
                    gateHeld = false;
                    try { smokeStop = await _legacySmoke.StopActiveAsync(muxId, CancellationToken.None).ConfigureAwait(false); }
                    finally
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        _legacyStopping = false;
                    }
                    if (smokeStop?["final"]?.GetValue<bool>() == false)
                        return LegacyError("Runtime smoke is still draining; restart was not attempted.");
                    var restartArguments = new Dictionary<string, JsonElement>(_legacyLaunchConfig, StringComparer.Ordinal);
                    if (rebuild) restartArguments["pre_build"] = JsonSerializer.SerializeToElement(true);
                    if (_legacySessionId is { } previous)
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        if (_sessions.TryGetValue(previous, out var restartSession)) restartSession.VetoQuickPause();
                        _legacySessionGeneration++;
                        _legacyStopping = true;
                        _legacyInspectionSessionId = null;
                        _legacyInspectionContextId = null;
                        _legacyInspectionMuxId = null;
                        _legacyCurrentFrameId = null;
                        _legacyInspectionRevision++;
                        var breakpointSession = _legacyBreakpointObserverSession;
                        _legacyGate.Release();
                        gateHeld = false;
                        CallToolResult stopped;
                        LegacyResourceOwner.Generation? retired = null;
                        try
                        {
                            retired = _legacyResources.Retire();
                            stopped = await StopAsync(LegacyRequest(StopDebug, previous, null), CancellationToken.None).ConfigureAwait(false);
                        }
                        finally
                        {
                            await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                            gateHeld = true;
                            _legacyStopping = false;
                        }
                        if (stopped.IsError == true) { _legacyResources.Restore(retired); return LegacyNativeError(stopped); }
                        _legacyOutputEvidence.Clear();
                        if (breakpointSession is not null)
                            breakpointSession.WithLegacyBreakpointLock(() =>
                            {
                                ReconcileLegacyBreakpointObservations(breakpointSession);
                                _legacyBreakpointObservationsIncomplete = LegacyHasStoppingBreakpoints() && !breakpointSession.LegacyBreakpointEventsComplete;
                                return true;
                            });
                        ClearLegacyGroupsAfterConfirmedStop();
                        foreach (var key in _legacyBreakpointObservations.Keys.ToArray())
                            _legacyBreakpointObservations[key] = _legacyBreakpointObservations[key] with { HitCount = 0 };
                        _legacySessionId = null;
                        _legacyBreakpointObserverSession = null;
                        _legacyHasOwner = false;
                        _legacyOwner = null;
                        var idleGeneration = _legacyResources.Begin();
                        idleGeneration.Observe("thread");
                        _legacyResources.Publish(idleGeneration);
                    }
                    arguments = restartArguments;
                    name = StartDebug;
                }
                if (name == AttachDebug)
                {
                    if (!OperatingSystem.IsWindows()) return LegacyError("Safe process-bound attach is supported only on Windows.");
                    if (!arguments.TryGetValue("process_id", out var requestedPid) || requestedPid.ValueKind != JsonValueKind.Number
                        || !requestedPid.TryGetInt32(out var processId) || processId <= 0) return LegacyError("Invalid process_id.");
                    if (string.IsNullOrWhiteSpace(_debuggerPath)) return LegacyError("Debugger is unavailable.");
                    var files = _legacyBreakpoints.Select(static point => point.File)
                        .Concat(_legacyGroups.Values.SelectMany(static group => group.Breakpoints.Select(static point => point.File)))
                        .Concat(_legacyTracepoints.Select(static point => point.File)).Distinct(_legacyBreakpointPathComparer);
                    var requested = files.SelectMany(file => SnapshotLegacyBreakpointLines(file).Select(line => (File: file, Line: line))).ToArray();
                    var points = requested.Select((point, index) => new DapSourceBreakpoint(index, point.File, point.Line.Line,
                        point.Line.Incarnation, point.Line.Condition, point.Line.HitCondition, point.Line.Identity)).ToArray();
                    if (!TryNormalizeBreakpointSources(points)) return LegacyError("Breakpoint source unavailable.");
                    var functions = _legacyFunctionBreakpoints.ToArray();
                    var attachToken = CreateToken();
                    lock (_lifetimeGate)
                    {
                        if (_shutdownRequested || _buildActive || _buildCleanupUnconfirmed || _startCleanupFailure is not null)
                            return LegacyError("Debug session cleanup is unconfirmed.");
                        _starts++;
                    }
                    _legacyHasOwner = true;
                    _legacyOwner = muxId;
                    _legacyOwnerActivity = Stopwatch.GetTimestamp();
                    _legacySessionGeneration++;
                    var attachGeneration = _legacySessionGeneration;
                    _legacyTraceLastContext = null;
                    _legacyTracePauseVeto = false;
                    for (var index = 0; index < _legacyFunctionBreakpoints.Count; index++)
                        _legacyFunctionBreakpoints[index] = _legacyFunctionBreakpoints[index] with { Verified = false };
                    foreach (var point in _legacyTracepoints) { point.Verified = false; point.DapLine = null; point.BreakpointId = null; point.ObservedIncarnation = 0; }
                    foreach (var point in _legacyGroups.Values.SelectMany(static group => group.Breakpoints))
                    { point.Verified = false; point.DapLine = null; point.Id = null; point.ObservedIncarnation = 0; }
                    _legacyStarting = true;
                    _legacyGate.Release();
                    gateHeld = false;
                    NetCoreDbgSession? attached = null;
                    NativeSceneSessionBinding? attachBinding = null;
                    SessionSlot? attachSlot = null;
                    SessionSlot.SessionLease? attachLease = null;
                    LegacyResourceOwner.Generation? attachResources = null;
                    var attachRegistered = false;
                    var attachSucceeded = false;
                    try
                    {
                        using var startupCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, _startShutdown.Token);
                        attachResources = _legacyResources.Begin();
                        attached = await NetCoreDbgSession.PrepareAttachAsync(_debuggerPath, processId, InitializeTimeout, RequestTimeout,
                            StopTimeout, startupCancellation.Token, eventName =>
                            {
                                attachResources!.Observe(eventName);
                                if (eventName == "stopped") ScheduleLegacyTrace(attachGeneration);
                            }).ConfigureAwait(false);
                        attachBinding = new NativeSceneSessionBinding(attachToken, _bridgePath, _artifactRoot, _supportsNativeSceneCapture);
                        attachBinding.AttachSession(attached);
                        var attachOutput = new OutputLifetime(attached.OutputBuffer, attached);
                        attachSlot = new SessionSlot(StopTimeout, attached.StopForOwnerAsync,
                            () => DisposeSlotResourcesAsync(attached!, attachBinding!),
                            () => RemoveSlot(attachToken, attached!, attachBinding!, attachSlot!, attachOutput),
                            () => DrainAttachedSessionAsync(attached!));
                        lock (_lifetimeGate)
                        {
                            if (_shutdownRequested) throw new InvalidOperationException("The debug-session registry is shutting down.");
                            if (_outputs.ContainsKey(attachToken) || _ownedSlots.ContainsKey(attachToken))
                                throw new InvalidOperationException("The debug-session token is already registered.");
                            _outputs.Add(attachToken, attachOutput);
                            _ownedSlots.Add(attachToken, (attachSlot, attachOutput, attached));
                            attachRegistered = true;
                            attachLease = attachSlot.TryAcquire() ?? throw new InvalidOperationException("The attach owner is closing.");
                        }
                        startupCancellation.CancelAfter(RequestTimeout);
                        await attached.AttachPreparedAsync(points, functions, startupCancellation.Token).ConfigureAwait(false);
                        startupCancellation.Token.ThrowIfCancellationRequested();
                        lock (_lifetimeGate)
                        {
                            if (_shutdownRequested || attached.IsRetirementRequested || !_isUsable(attached))
                                throw new InvalidOperationException("The attach session cannot be published.");
                            if (!_sessions.TryAdd(attachToken, attached) || !_slots.TryAdd(attachToken, attachSlot)
                                || !_nativeSceneBindings.TryAdd(attachToken, attachBinding))
                            {
                                _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(attachToken, attached));
                                _slots.TryRemove(new KeyValuePair<string, SessionSlot>(attachToken, attachSlot));
                                _nativeSceneBindings.TryRemove(new KeyValuePair<string, NativeSceneSessionBinding>(attachToken, attachBinding));
                                throw new InvalidOperationException("The debug-session token is already registered.");
                            }
                            attached.RetirementRequested += retiring => OnRetirementRequested(attachToken, retiring);
                            if (attached.IsRetirementRequested || !_isUsable(attached))
                                throw new InvalidOperationException("The attach session retired before publication.");
                            attachSucceeded = true;
                        }
                    }
                    catch (Exception exception)
                    {
                        if (exception is DapStartupCleanupException) RecordStartCleanupFailure(exception);
                    }
                    finally
                    {
                        if (!attachSucceeded && attachRegistered)
                        {
                            try { _ = attached!.DisconnectAttachOnceAsync(CancellationToken.None); }
                            catch (Exception) { }
                        }
                        attachLease?.Dispose();
                        if (!attachSucceeded)
                        {
                            if (attachRegistered)
                            {
                                lock (_lifetimeGate)
                                {
                                    _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(attachToken, attached!));
                                    _slots.TryRemove(new KeyValuePair<string, SessionSlot>(attachToken, attachSlot!));
                                    _nativeSceneBindings.TryRemove(new KeyValuePair<string, NativeSceneSessionBinding>(attachToken, attachBinding!));
                                }
                                _ = attachSlot!.CloseAndDrainAsync();
                            }
                            else
                            {
                                try { await DisposeUnpublishedResourcesAsync(attached, attachBinding, CancellationToken.None).ConfigureAwait(false); }
                                catch (Exception cleanupFailure) { RecordStartCleanupFailure(cleanupFailure); }
                            }
                        }
                        lock (_lifetimeGate) if (--_starts == 0) _startsDrained?.TrySetResult(true);
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        _legacyStarting = false;
                    }
                    if (attachSucceeded)
                    {
                        lock (_lifetimeGate)
                        {
                            if (_shutdownRequested || cancellationToken.IsCancellationRequested || attachSlot!.IsClosing)
                            {
                                attachSucceeded = false;
                                _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(attachToken, attached!));
                                _slots.TryRemove(new KeyValuePair<string, SessionSlot>(attachToken, attachSlot!));
                                _nativeSceneBindings.TryRemove(new KeyValuePair<string, NativeSceneSessionBinding>(attachToken, attachBinding!));
                                _ = attachSlot!.CloseAndDrainAsync();
                            }
                        }
                    }
                    if (attachRegistered)
                    {
                        _legacySessionId = attachToken;
                        _legacyAttachSession = attached;
                        _legacyAttachSlot = attachSlot;
                        _legacyAttachClosing = !attachSucceeded;
                    }
                    if (!attachSucceeded)
                    {
                        if (attachResources is not null) _legacyResources.Discard(attachResources);
                        if (!attachRegistered) { _legacyHasOwner = false; _legacyOwner = null; }
                        return LegacyError(attachRegistered ? "Attach outcome and cleanup are unconfirmed; do not retry automatically." : "Attach preparation failed.", "unknown");
                    }
                    _legacyBreakpointObserverSession = attached;
                    ReconcileLegacyFunctionBreakpoints();
                    _legacyBreakpointObservationsIncomplete = false;
                    _legacyUnconfirmedBreakpointFiles.Clear();
                    _legacyUnconfirmedBreakpointOverflow = false;
                    _legacyResources.Publish(attachResources!, attachToken);
                    ScheduleLegacyTrace(attachGeneration);
                    return Result(new
                    {
                        state = LegacySnapshot().State,
                        next_actions = LegacyActions(LegacySnapshot().State),
                        message = "Debug session state observed.",
                        data = new { success = true, processId },
                        warning = "ATTACH MODE HAS LIMITED FUNCTIONALITY. Stack traces may be incomplete due to netcoredbg limitation. For reliable debugging, use start_debug instead."
                    }, false);
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
                    if (arguments["pre_build"].ValueKind == JsonValueKind.True)
                    {
                        lock (_lifetimeGate)
                        {
                            _legacyBuildSnapshot = null;
                            _legacyBuildUnavailable = "The latest build attempt has not completed.";
                        }
                    }
                    var launchConfig = new Dictionary<string, JsonElement>(arguments, StringComparer.Ordinal);
                    arguments["justMyCode"] = JsonSerializer.SerializeToElement(false);
                    var startupFiles = _legacyBreakpoints.Select(static point => point.File).Concat(_legacyGroups.Values.SelectMany(static group => group.Breakpoints.Select(static point => point.File))).Concat(_legacyTracepoints.Select(static point => point.File)).Distinct(_legacyBreakpointPathComparer);
                    var startupBreakpoints = startupFiles.SelectMany(file => SnapshotLegacyBreakpointLines(file).Select(line => (File: file, Line: line))).ToArray();
                    arguments["breakpoints"] = JsonSerializer.SerializeToElement(startupBreakpoints.Select(static point => new { file = point.File, line = point.Line.Line }).ToArray());
                    for (var index = 0; index < _legacyFunctionBreakpoints.Count; index++)
                        _legacyFunctionBreakpoints[index] = _legacyFunctionBreakpoints[index] with { Verified = false };
                    var breakpointOptions = startupBreakpoints.Select(static point => point.Line).ToArray();
                    _legacyHasOwner = true;
                    _legacyOwner = muxId;
                    _legacyOwnerActivity = Stopwatch.GetTimestamp();
                    _legacySessionGeneration++;
                    var traceGeneration = _legacySessionGeneration;
                    _legacyTraceLastContext = null;
                    _legacyTracePauseVeto = false;
                    foreach (var point in _legacyTracepoints) { point.Verified = false; point.DapLine = null; point.BreakpointId = null; point.ObservedIncarnation = 0; }
                    foreach (var point in _legacyGroups.Values.SelectMany(static group => group.Breakpoints))
                    { point.Verified = false; point.DapLine = null; point.Id = null; point.ObservedIncarnation = 0; }
                    _legacyStarting = true;
                    _legacyGate.Release();
                    gateHeld = false;
                    CallToolResult started;
                    var startCompleted = false;
                    var adapterStartAttempted = false;
                    LegacyResourceOwner.Generation? generation = null;
                    LegacyResolvedLaunch? resolvedLaunch = null;
                    try
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        generation = _legacyResources.Begin();
                        started = await StartAsync(context, cancellationToken, arguments, eventName =>
                        {
                            generation.Observe(eventName);
                            if (eventName == "stopped") ScheduleLegacyTrace(traceGeneration);
                        }, breakpointOptions, _legacyFunctionBreakpoints.ToArray(), restarting ? _legacyResolvedLaunch : null, value => resolvedLaunch = value,
                            () => adapterStartAttempted = true).ConfigureAwait(false);
                        startCompleted = true;
                    }
                    finally
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        _legacyStarting = false;
                        lock (_lifetimeGate)
                            if (_legacyBuildSnapshot is null && _legacyBuildUnavailable == "The latest build attempt has not completed.")
                                _legacyBuildUnavailable = "No build result is available for the latest attempt.";
                        if (!startCompleted && adapterStartAttempted) _legacyOutputEvidence.InvalidateForUnpublishedStart();
                        if (!startCompleted && generation is not null) _legacyResources.Discard(generation);
                        if (!startCompleted) { _legacyHasOwner = false; _legacyOwner = null; }
                    }
                    if (started.IsError == true || started.StructuredContent is not { } startContent
                        || !startContent.TryGetProperty("debugSessionId", out var tokenElement)
                        || tokenElement.GetString() is not { } token)
                    {
                        if (adapterStartAttempted) _legacyOutputEvidence.InvalidateForUnpublishedStart();
                        _legacyResources.Discard(generation!);
                        _legacyHasOwner = false;
                        _legacyOwner = null;
                        return LegacyNativeError(started);
                    }
                    _legacySessionId = token;
                    lock (_lifetimeGate)
                        if (_outputs.TryGetValue(token, out var legacyOutput)) _legacyBreakpointObserverSession = legacyOutput.LegacySession;
                    ReconcileLegacyFunctionBreakpoints();
                    if (cancellationToken.IsCancellationRequested)
                    {
                        _legacyOutputEvidence.InvalidateForUnpublishedStart();
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
                            _legacyBreakpointObserverSession = null;
                            _legacyHasOwner = false;
                            _legacyOwner = null;
                        }
                        cancellationToken.ThrowIfCancellationRequested();
                    }
                    _legacyBreakpointObservationsIncomplete = false;
                    _legacyUnconfirmedBreakpointFiles.Clear();
                    _legacyUnconfirmedBreakpointOverflow = false;
                    _legacyResources.Publish(generation!, token);
                    _legacyLaunchConfig = launchConfig;
                    _legacyResolvedLaunch = resolvedLaunch;
                    ScheduleLegacyTrace(traceGeneration);
                    return LegacyResponse(new { success = true, program = resolvedLaunch!.Program });
                }
                if (name == GetBuildDiagnostics)
                {
                    if (arguments.TryGetValue("include_warnings", out var include) && include.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
                        return LegacyError("Invalid include_warnings.");
                    LegacyBuildSnapshot? buildSnapshot;
                    string? unavailable;
                    lock (_lifetimeGate) { buildSnapshot = _legacyBuildSnapshot; unavailable = _legacyBuildUnavailable; }
                    if (buildSnapshot is null) return LegacyResponse(new { message = unavailable ?? "No build has been performed yet." });
                    var includeWarnings = !arguments.TryGetValue("include_warnings", out include) || include.ValueKind == JsonValueKind.True;
                    var capturedGeneration = _legacySessionGeneration;
                    var capturedState = LegacySnapshot().State;
                    _legacyGate.Release();
                    gateHeld = false;
                    var parsed = await Task.Run(() =>
                    {
                        try
                        {
                            var data = buildSnapshot.ToData(includeWarnings, MaximumSerializedInspectionBytes, cancellationToken, out var message);
                            cancellationToken.ThrowIfCancellationRequested();
                            var serialized = JsonSerializer.Serialize(new { state = capturedState, next_actions = LegacyActions(capturedState), message, data });
                            if (Encoding.UTF8.GetByteCount(serialized) > MaximumSerializedInspectionBytes)
                                return LegacyError("Build diagnostics exceed the native 256 KiB response limit.", capturedState);
                            cancellationToken.ThrowIfCancellationRequested();
                            return SerializedResult(serialized, false);
                        }
                        catch (LegacyBuildDiagnosticsException exception)
                        {
                            return LegacyError(exception.Message, capturedState);
                        }
                    }, cancellationToken).ConfigureAwait(false);
                    await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    gateHeld = true;
                    lock (_lifetimeGate)
                        if (!ReferenceEquals(buildSnapshot, _legacyBuildSnapshot))
                            return LegacyError("Build result changed before diagnostics completed.");
                    if (capturedGeneration != _legacySessionGeneration || capturedState != LegacySnapshot().State)
                        return LegacyError("Debug session changed before diagnostics completed.");
                    cancellationToken.ThrowIfCancellationRequested();
                    return parsed;
                }
                if (LegacyRuntimeSmokeFacade.IsTool(name))
                {
                    JsonObject data;
                    if (name is "runtime_smoke_validate_probe" or "runtime_smoke_run_probe" or "runtime_smoke_run_plan" or "runtime_smoke_start")
                    {
                        var generation = _legacySessionGeneration;
                        _legacyGate.Release();
                        gateHeld = false;
                        var prepared = await Task.Run(() => _legacySmoke.Prepare(name, arguments), cancellationToken).ConfigureAwait(false);
                        await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                        gateHeld = true;
                        if (name != "runtime_smoke_validate_probe" &&
                            (generation != _legacySessionGeneration || _legacyStarting || _legacyStopping || (_legacyHasOwner && _legacyOwner != muxId)))
                            return LegacyError("Debug session changed before runtime smoke admission.");
                        if (name == "runtime_smoke_validate_probe") data = prepared.Data;
                        else
                        {
                            var effect = LegacyRuntimeSmokeFileRunner.UsesEvaluation(prepared.Plan) ? new LegacyRuntimeSmokeEffect() : null;
                            var observer = CreateLegacySmokeOutputObserver(prepared.Plan, generation, _legacySessionId, muxId)
                                ?? CreateLegacySmokeEvaluationObserver(generation, _legacySessionId, muxId, effect);
                            Func<JsonObject, CancellationToken, Task<JsonObject>>? runner = observer is null ? null :
                                (plan, token) => LegacyRuntimeSmokeFileRunner.RunAsync(plan, token, observer, effect);
                            data = _legacySmoke.Start(name, prepared, muxId, runner, effect);
                        }
                    }
                    else if (name is "runtime_smoke_evidence_bundle" or "runtime_smoke_tail_events" or "runtime_smoke_mark_event_cursor" or "runtime_smoke_get_event_delta" or "runtime_smoke_cleanup_contract")
                        data = _legacySmoke.Observe(name, arguments, muxId);
                    else
                    {
                        if (!arguments.TryGetValue("run_id", out var runIdValue) || runIdValue.ValueKind != JsonValueKind.String ||
                            string.IsNullOrEmpty(runIdValue.GetString()) || runIdValue.GetString()!.Length > 128)
                            return LegacyError("A valid run_id is required.");
                        var runId = runIdValue.GetString()!;
                        if (name == "runtime_smoke_get_result") data = _legacySmoke.GetResult(runId, muxId);
                        else
                        {
                            _legacyGate.Release();
                            gateHeld = false;
                            data = name == "runtime_smoke_wait_for_result"
                                ? await _legacySmoke.WaitAsync(runId, muxId, arguments, cancellationToken).ConfigureAwait(false)
                                : await _legacySmoke.StopAsync(runId, muxId, cancellationToken).ConfigureAwait(false);
                            await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                            gateHeld = true;
                        }
                    }
                    cancellationToken.ThrowIfCancellationRequested();
                    var state = LegacySnapshot().State;
                    var next = data["next_actions"] is JsonArray proposed
                        ? proposed.Select(static item => item!.GetValue<string>()).ToArray()
                        : name == "runtime_smoke_validate_probe" && data["can_run"]?.GetValue<bool>() == true
                            ? new[] { "runtime_smoke_run_probe", "runtime_smoke_validate_plan" }
                            : data["run_id"] is not null
                                ? new[] { "runtime_smoke_wait_for_result", "runtime_smoke_get_result", "runtime_smoke_stop" }
                                : new[] { "runtime_smoke_validate_probe", "runtime_smoke_validate_plan" };
                    var response = Result(new { state, next_actions = next, message = "Native runtime smoke result.", data }, false);
                    return response.StructuredContent is { } body && Encoding.UTF8.GetByteCount(body.GetRawText()) <= MaximumSerializedInspectionBytes
                        ? response : LegacyError("Runtime smoke result exceeds 256 KiB.", state);
                }
                if (name is RuntimeSmokeValidatePlan or RunRuntimeSmoke)
                {
                    var hasPlan = arguments.TryGetValue("plan", out var planValue) && planValue.ValueKind != JsonValueKind.Null;
                    var hasPath = arguments.TryGetValue("plan_path", out var pathValue) && pathValue.ValueKind == JsonValueKind.String && !string.IsNullOrEmpty(pathValue.GetString());
                    if ((hasPlan && planValue.ValueKind != JsonValueKind.Object) ||
                        (arguments.ContainsKey("plan_path") && pathValue.ValueKind is not (JsonValueKind.String or JsonValueKind.Null)))
                        return LegacyError("Unsupported or invalid argument.");
                    JsonObject data;
                    if ((name == RuntimeSmokeValidatePlan && hasPlan == hasPath) || (name == RunRuntimeSmoke && !hasPlan))
                    {
                        data = LegacyRuntimeSmokeValidation.ValidateNative(null);
                        data["validation_errors"] = new JsonArray("provide exactly one runtime smoke plan input: plan or plan_path");
                        data["accepted_input"] = new JsonObject { ["plan"] = "inline JSON object", ["plan_path"] = "path to a UTF-8 JSON or YAML object plan file" };
                    }
                    else if (hasPath)
                    {
                        data = LegacyRuntimeSmokeValidation.ValidateNative(null);
                        data["status"] = "BLOCKED";
                        data["can_run"] = false;
                        data["validation_errors"] = new JsonArray("Native plan_path loading is not supported; provide an inline plan.");
                    }
                    else
                    {
                        var plan = JsonNode.Parse(planValue.GetRawText());
                        var effect = name == RunRuntimeSmoke && LegacyRuntimeSmokeFileRunner.UsesEvaluation(plan) ? new LegacyRuntimeSmokeEffect() : null;
                        var observer = name == RunRuntimeSmoke
                            ? CreateLegacySmokeOutputObserver(plan, _legacySessionGeneration, _legacySessionId, muxId)
                                ?? CreateLegacySmokeEvaluationObserver(_legacySessionGeneration, _legacySessionId, muxId, effect) : null;
                        _legacyGate.Release();
                        gateHeld = false;
                        data = name == RuntimeSmokeValidatePlan
                            ? LegacyRuntimeSmokeFileRunner.Validate(plan)
                            : await _legacySmoke.RunDirectAsync(plan, observer, effect, cancellationToken).ConfigureAwait(false);
                        await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                        gateHeld = true;
                    }
                    cancellationToken.ThrowIfCancellationRequested();
                    var state = LegacySnapshot().State;
                    var next = name == RuntimeSmokeValidatePlan && data["can_run"]?.GetValue<bool>() == true
                        ? new[] { "run_runtime_smoke" } : new[] { "runtime_smoke_validate_plan" };
                    var response = Result(new { state, next_actions = next, message = "Native runtime smoke result.", data }, false);
                    return response.StructuredContent is { } body && Encoding.UTF8.GetByteCount(body.GetRawText()) <= MaximumSerializedInspectionBytes
                        ? response : LegacyError("Runtime smoke result exceeds 256 KiB.", state);
                }
                if (name == InspectLaunchCompatibility)
                {
                    if (!arguments.TryGetValue("program", out var requestedProgram) || requestedProgram.ValueKind != JsonValueKind.String
                        || string.IsNullOrWhiteSpace(requestedProgram.GetString())) return LegacyError("A program is required.");
                    var programPath = requestedProgram.GetString()!;
                    _legacyGate.Release();
                    gateHeld = false;
                    var root = await ProjectRootResolver.ResolveAsync(context.Server, cancellationToken,
                        context.Params.InputResponses, context.JsonRpcRequest.Context?.ProtocolVersion).ConfigureAwait(false);
                    object? data = null;
                    string? error = null;
                    if (root is null) error = "Resolved project root is required for launch inspection.";
                    else
                    {
                        try { data = await Task.Run(() => LegacyLaunchCompatibility.Inspect(root, programPath, _debuggerPath), cancellationToken).ConfigureAwait(false); }
                        catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException or IOException or UnauthorizedAccessException)
                        { error = exception.Message; }
                    }
                    await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    gateHeld = true;
                    if (error is not null) return LegacyError(error);
                    cancellationToken.ThrowIfCancellationRequested();
                    return LegacyBoundedUtilityResponse(data!, LegacySnapshot().State);
                }
                if (name == VerifyDebugFreshness)
                {
                    if (_legacyStarting || _legacyStopping || _legacyAttachClosing) return LegacyError("A debug session transition is in progress.");
                    var freshnessGeneration = _legacySessionGeneration;
                    var freshnessSessionId = _legacySessionId;
                    var projectPath = freshnessSessionId is not null && _legacyAttachSession is null ? _legacyResolvedLaunch?.ProjectRoot : null;
                    _legacyGate.Release();
                    gateHeld = false;
                    if (projectPath is null)
                        projectPath = await ProjectRootResolver.ResolveAsync(context.Server, cancellationToken,
                            context.Params.InputResponses, context.JsonRpcRequest.Context?.ProtocolVersion).ConfigureAwait(false);
                    await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    gateHeld = true;
                    if (freshnessGeneration != _legacySessionGeneration || freshnessSessionId != _legacySessionId || _legacyStarting || _legacyStopping || _legacyAttachClosing)
                        return LegacyError("Debug session changed before freshness observation.");
                    if (muxId is not null && _legacyHasOwner && _legacyOwner is not null && _legacyOwner != muxId)
                        return LegacyError("Debug session is owned by another agent.");
                    NetCoreDbgSession? freshnessSession = null;
                    if (freshnessSessionId is not null && (!_sessions.TryGetValue(freshnessSessionId, out freshnessSession) || !_isUsable(freshnessSession)))
                        return LegacyError("Debug session state is unavailable.");
                    var snapshot = freshnessSession?.GetLegacyResourceState();
                    if (snapshot is { Complete: false } || snapshot is { DetailsComplete: false })
                        return LegacyError("Debug session evidence is incomplete.");
                    var inventory = freshnessSession?.GetRuntimeInventory(true, MaximumSerializedInspectionBytes);
                    if (inventory is { } observedInventory && observedInventory.Status != DapRuntimeInventoryStatus.Success)
                        return LegacyError("Runtime inventory state is unavailable or too large.");
                    var moduleContent = inventory?.SerializedContent ?? "{\"modules\":[]}";
                    _legacyGate.Release();
                    gateHeld = false;
                    object? data = null;
                    string? error = null;
                    try
                    {
                        data = await Task.Run(() =>
                        {
                            using var moduleDocument = JsonDocument.Parse(moduleContent);
                            return LegacyDebugFreshness.Verify(freshnessArguments!, projectPath, snapshot?.ProcessId, snapshot?.ProcessName,
                                snapshot?.Sources, moduleDocument.RootElement.GetProperty("modules"));
                        }, cancellationToken).ConfigureAwait(false);
                    }
                    catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException or IOException or UnauthorizedAccessException)
                    {
                        error = "Invalid or unavailable freshness path.";
                    }
                    await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    gateHeld = true;
                    if (freshnessGeneration != _legacySessionGeneration || freshnessSessionId != _legacySessionId || _legacyStarting || _legacyStopping || _legacyAttachClosing
                        || (freshnessSessionId is not null && (!_sessions.TryGetValue(freshnessSessionId, out var currentFreshnessSession) || !ReferenceEquals(currentFreshnessSession, freshnessSession))))
                        return LegacyError("Debug session changed before freshness observation.");
                    if (muxId is not null && _legacyHasOwner && _legacyOwner is not null && _legacyOwner != muxId)
                        return LegacyError("Debug session is owned by another agent.");
                    if (error is not null) return LegacyError(error);
                    cancellationToken.ThrowIfCancellationRequested();
                    return LegacyBoundedUtilityResponse(data!, LegacySnapshot().State);
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
                if (name == DebuggeeActivity)
                {
                    long windowMs = 1000;
                    if (arguments.TryGetValue("window_ms", out var window) &&
                        (window.ValueKind != JsonValueKind.Number || !window.TryGetInt64(out windowMs) || windowMs is < 1 or > 30000))
                        return LegacyError("window_ms must be an integer from 1 to 30000");
                    if (_legacyStarting || _legacyStopping || _legacySessionId is not { } activityId ||
                        !_sessions.TryGetValue(activityId, out var activitySession))
                        return LegacyError("debuggee_activity requires state RUNNING");
                    var before = activitySession.CaptureLegacyActivity();
                    if (ProjectLegacyState(before.State).State != "running" || !_isUsable(activitySession))
                        return LegacyError("debuggee_activity requires state RUNNING");
                    var generation = _legacySessionGeneration;
                    var traceBefore = _legacyTraceAppendCount;
                    _legacyGate.Release();
                    gateHeld = false;
                    await Task.Delay((int)windowMs, cancellationToken).ConfigureAwait(false);
                    await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
                    gateHeld = true;
                    cancellationToken.ThrowIfCancellationRequested();
                    var after = activitySession.CaptureLegacyActivity();
                    var end = ProjectLegacyState(after.State);
                    if (_legacySessionGeneration != generation || _legacySessionId != activityId || _legacyStarting || _legacyStopping ||
                        end.State is not ("running" or "stopped" or "terminated") ||
                        (end.State != "terminated" && (!_sessions.TryGetValue(activityId, out var currentActivity) ||
                            !ReferenceEquals(currentActivity, activitySession) || !_isUsable(activitySession))) ||
                        (end.State == "terminated" && LegacySnapshot(activityId).State != "terminated"))
                        return LegacyError("debug session changed during activity window");
                    var continued = after.Continued - before.Continued;
                    var stopped = after.Stopped - before.Stopped;
                    var steps = after.StepStops - before.StepStops;
                    var output = after.Output - before.Output;
                    var added = after.ModuleNew - before.ModuleNew;
                    var changed = after.ModuleChanged - before.ModuleChanged;
                    var removed = after.ModuleRemoved - before.ModuleRemoved;
                    var traceEntries = _legacyTraceAppendCount - traceBefore;
                    var signals = new List<string>();
                    if (continued > 0) signals.Add("continuedEvents");
                    if (stopped > 0) signals.Add("stoppedEvents");
                    if (steps > 0) signals.Add("stepStops");
                    if (output > 0) signals.Add("outputEvents");
                    if (added > 0) signals.Add("moduleEvents.new");
                    if (changed > 0) signals.Add("moduleEvents.changed");
                    if (removed > 0) signals.Add("moduleEvents.removed");
                    if (traceEntries > 0) signals.Add("traceEntries");
                    static object ActivityState(DapSessionState state, int? pid)
                    {
                        var projected = ProjectLegacyState(state);
                        var execution = projected.State == "stopped" ? projected.Reason switch
                        {
                            "breakpoint" or "function breakpoint" or "data breakpoint" or "entry" => "stopped-at-breakpoint",
                            "exception" => "stopped-at-exception",
                            "step" => "stepping",
                            "pause" => "stopped-at-pause",
                            _ => "stopped-other"
                        } : projected.State;
                        return new { state = projected.State, execState = execution, stopReason = projected.Reason, debuggeePid = pid };
                    }
                    return LegacyResponse(new
                    {
                        windowMs,
                        startedAt = before.CapturedAt,
                        endedAt = after.CapturedAt,
                        elapsedMs = (after.CapturedAt - before.CapturedAt) * 1000,
                        start = ActivityState(before.State, before.ProcessId),
                        end = ActivityState(after.State, after.ProcessId),
                        deltas = new
                        {
                            continuedEvents = continued,
                            stoppedEvents = stopped,
                            stepStops = steps,
                            outputEvents = output,
                            moduleEvents = new { total = added + changed + removed, @new = added, changed, removed },
                            traceEntries
                        },
                        observedActivity = signals.Count != 0,
                        activitySignals = signals,
                        instructionsExecuted = new { available = false, reason = "The current NetCoreDbg/DAP capability surface exposes no executed-instruction counter." }
                    }, end.State);
                }
                if (name is SearchOutput or "output_checkpoint" or "output_assert_since")
                {
                    if (_legacyStarting || _legacyStopping) return LegacyError("A debug session transition is in progress.");
                    DapOutputBuffer? buffer = null;
                    if (_legacySessionId is { } outputSession)
                    {
                        lock (_lifetimeGate)
                        {
                            if (!_outputs.TryGetValue(outputSession, out var lifetime)) return LegacyError("Output is unavailable.");
                            buffer = lifetime.Buffer;
                        }
                    }
                    else if (name is "output_checkpoint" or "output_assert_since")
                    {
                        lock (_lifetimeGate)
                        {
                            if (_startCleanupFailure is not null || _buildCleanupUnconfirmed || _starts != 0 || _ownedSlots.Count != 0)
                                return LegacyError("Debug session cleanup is unconfirmed.");
                        }
                    }
                    cancellationToken.ThrowIfCancellationRequested();
                    return LegacyOutputOperation(name, arguments, buffer, cancellationToken);
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
                if (name is "list_snapshots" or "diff_snapshots")
                    return LegacyStoredSnapshots(name, arguments);
                if (name is "create_snapshot" or "analyze_collection" or "summarize_object")
                {
                    if (name == "create_snapshot" && arguments.TryGetValue("name", out var snapshotName)
                        && snapshotName.ValueKind == JsonValueKind.String && snapshotName.GetString() is { } requestedName
                        && _utilitySnapshots.ContainsKey(requestedName)) return LegacyError($"Snapshot '{requestedName}' already exists.");
                    if (_legacySessionId is not { } utilitySession || !_sessions.TryGetValue(utilitySession, out var utilityOwner)
                        || !_slots.TryGetValue(utilitySession, out var utilitySlot)) return LegacyError("No active debug session.");
                    var captured = utilityOwner.CaptureStoppedContext();
                    if (captured is null) return LegacyError("No current stopped context.");
                    if (name != "create_snapshot" && (_legacyInspectionSessionId != utilitySession
                        || !StringComparer.Ordinal.Equals(_legacyInspectionMuxId, muxId)
                        || !StringComparer.Ordinal.Equals(_legacyInspectionContextId, captured.Value.Context)))
                        return LegacyError("Call get_call_stack for the current stopped context first.");
                    var revision = _legacyInspectionRevision;
                    _legacyCompositeEvaluations++;
                    compositeEvaluationHeld = true;
                    _legacyGate.Release();
                    gateHeld = false;
                    var utility = await ReadLegacyUtilityAsync(name, arguments, utilityOwner, utilitySlot, captured.Value.State.CurrentThreadId!.Value,
                        captured.Value.Context, cancellationToken).ConfigureAwait(false);
                    await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                    gateHeld = true;
                    cancellationToken.ThrowIfCancellationRequested();
                    if (_legacySessionId != utilitySession || revision != _legacyInspectionRevision
                        || !_sessions.TryGetValue(utilitySession, out var currentUtility) || !ReferenceEquals(currentUtility, utilityOwner))
                        return LegacyError("Stopped inspection context changed before the operation completed.", "unknown");
                    return utilityOwner.WithLegacyBreakpointLock(() =>
                    {
                        if (!utilityOwner.IsStoppedContextCurrent(captured.Value.State.CurrentThreadId!.Value, captured.Value.Context))
                            return LegacyError("Stopped inspection context changed before the operation completed.", "unknown");
                        if (utility.Error is not null) return LegacyError(utility.Error, "stopped");
                        var response = LegacyBoundedUtilityResponse(utility.Data!, "stopped");
                        if (response.StructuredContent is { } failure && failure.TryGetProperty("error", out _)) return response;
                        if (utility.Snapshot is { } snapshot)
                        {
                            if (_utilitySnapshots.ContainsKey(snapshot.Name)) return LegacyError($"Snapshot '{snapshot.Name}' already exists.", "stopped");
                            if (!TryUtilityLimits(out var maxSnapshots, out _)) return LegacyError("Invalid snapshot limits.", "stopped");
                            while (_utilitySnapshots.Count >= maxSnapshots) _utilitySnapshots.Remove(_utilitySnapshotOrder.Dequeue());
                            _utilitySnapshots.Add(snapshot.Name, snapshot);
                            _utilitySnapshotOrder.Enqueue(snapshot.Name);
                        }
                        return response;
                    });
                }
                if (cleanupForce && _legacySessionId is null)
                {
                    KeyValuePair<string, (SessionSlot Slot, OutputLifetime Output, NetCoreDbgSession Session)>[] orphaned;
                    lock (_lifetimeGate) orphaned = _ownedSlots.ToArray();
                    if (orphaned.Length > 0 && (!_legacyHasOwner || _legacyOwner != muxId))
                        return LegacyCleanupResult(cleanupObservations!, false, "Cleanup owner is unavailable or belongs to another agent.");
                    _legacyStopping = true;
                    _legacyGate.Release();
                    gateHeld = false;
                    try
                    {
                        foreach (var entry in orphaned)
                        {
                            var close = entry.Value.Slot.CloseAndDrainAsync();
                            _ = CompleteRetirementAsync(entry.Key, entry.Value.Session, entry.Value.Slot, entry.Value.Output, close);
                            await Task.WhenAny(close, Task.Delay(StopTimeout)).ConfigureAwait(false);
                            if (close.IsFaulted) _ = close.Exception;
                        }
                    }
                    finally
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        _legacyStopping = false;
                    }
                    return smokeDrainPending
                        ? LegacyCleanupResult(cleanupObservations!, false, "Debugger cleanup finished; runtime smoke is still draining.")
                        : LegacyCleanupResult(cleanupObservations!, true);
                }
                if (name == StopDebug && _legacySessionId is null)
                {
                    if (_legacyBreakpointSessionId is not null)
                        return LegacyError("A breakpoint update is still settling.");
                    lock (_lifetimeGate)
                    {
                        if (_shutdownRequested || _startCleanupFailure is not null || _buildCleanupUnconfirmed || _buildActive
                            || _starts != 0 || _ownedSlots.Count != 0 || _outputs.Count != 0 || !_sessions.IsEmpty || !_slots.IsEmpty)
                            return LegacyError("Debug session cleanup is unconfirmed.");
                        cancellationToken.ThrowIfCancellationRequested();
                        _legacyOutputEvidence.Clear();
                    }
                    ClearLegacyGroupsAfterConfirmedStop();
                    if (smokeDrainPending) return LegacyError("Debugger is idle; runtime smoke is still draining.", "idle");
                    return LegacyResponse(new { success = true });
                }
                if (_legacySessionId is not { } sessionId) return LegacyError("No active debug session.");
                if (name == QuickEvaluate)
                {
                    if (!arguments.TryGetValue("expression", out var expression) || expression.ValueKind != JsonValueKind.String
                        || expression.GetString() is not { } text || Encoding.UTF8.GetByteCount(text) > MaximumSerializedInspectionBytes)
                        return LegacyError("Invalid expression.");
                    int? frameId = null;
                    if (arguments.TryGetValue("frame_id", out var frame) && frame.ValueKind != JsonValueKind.Null)
                    {
                        if (!TryReadOutputLineCount(frame, out var number) || number is < int.MinValue or > int.MaxValue)
                            return LegacyError("Invalid frame_id.");
                        frameId = (int)number;
                    }
                    if (!_sessions.TryGetValue(sessionId, out var quickSession) || !_slots.TryGetValue(sessionId, out var quickSlot))
                        return LegacyError("No active debug session.");
                    if (LegacySnapshot(sessionId).State != "running") return LegacyError("Program is not running; use evaluate_expression instead.");
                    if (_legacyTracePauseVeto) return LegacyError("A previous pause may still stop the program; use explicit execution control.");
                    var quickLease = quickSlot.TryAcquire();
                    if (quickLease is null) return LegacyError("No active debug session.");
                    using var quickCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
                    _stepOverRequests.TryAdd(context.JsonRpcRequest.Id, quickCancellation);
                    _legacyInspectionSessionId = null;
                    _legacyInspectionContextId = null;
                    _legacyInspectionMuxId = null;
                    _legacyCurrentFrameId = null;
                    _legacyInspectionRevision++;
                    _legacyTracePauseVeto = true;
                    _legacyTraceRevision++;
                    var quickPauseRevision = ++_legacyTracePauseRevision;
                    _legacyCompositeEvaluations++;
                    compositeEvaluationHeld = true;
                    var close = false;
                    (DapEvaluateResult? Value, string? Error, bool Drain) evaluated;
                    try
                    {
                        using var operation = CancellationTokenSource.CreateLinkedTokenSource(quickCancellation.Token, quickLease.AbortToken);
                        var quickOperation = quickSession.QuickEvaluateAsync(text, frameId, operation.Token);
                        _legacyGate.Release();
                        gateHeld = false;
                        evaluated = await quickOperation.ConfigureAwait(false);
                        close = evaluated.Drain;
                    }
                    catch (Exception exception)
                    {
                        close = quickSession.RequiresInspectionDrain() || exception is DapPauseCanceledException { RequiresDrain: true }
                            or DapContinueCanceledException { RequiresDrain: true } || exception is not OperationCanceledException;
                        if (exception is OperationCanceledException && quickCancellation.IsCancellationRequested) throw;
                        evaluated = (null, "Quick evaluation failed or its outcome is unknown; do not retry automatically.", close);
                    }
                    finally
                    {
                        _stepOverRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(context.JsonRpcRequest.Id, quickCancellation));
                        quickLease.Dispose();
                        if (close) { try { await quickSlot.CloseAndDrainAsync().ConfigureAwait(false); } catch (Exception) { } }
                    }
                    await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                    gateHeld = true;
                    if (_legacySessionId != sessionId || !_sessions.TryGetValue(sessionId, out var currentQuick) || !ReferenceEquals(currentQuick, quickSession))
                        return LegacyError("Quick evaluation outcome is unknown; do not retry automatically.", "unknown");
                    if (quickPauseRevision == _legacyTracePauseRevision
                        && (evaluated.Error is null || evaluated.Error == NetCoreDbgSession.QuickEvaluationFailedAfterResume))
                        _legacyTracePauseVeto = false;
                    if (evaluated.Error is not null) return LegacyError(evaluated.Error);
                    var value = evaluated.Value!;
                    return LegacyBoundedUtilityResponse(new { result = value.Result, type = value.Type ?? "", variablesReference = value.VariablesReference }, LegacySnapshot(sessionId).State);
                }
                var terminateFallback = false;
                if (name == TerminateDebug)
                {
                    if (!_sessions.TryGetValue(sessionId, out var terminatingSession) || !_slots.TryGetValue(sessionId, out var terminatingSlot))
                        return LegacyError("No active debug session.");
                    using var lease = terminatingSlot.TryAcquire();
                    if (lease is null) return LegacyError("No active debug session.");
                    _legacyStopping = true;
                    _legacyGate.Release();
                    gateHeld = false;
                    string outcome;
                    try
                    {
                        using var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                        outcome = await terminatingSession.TerminateLegacyAsync(linked.Token).ConfigureAwait(false);
                    }
                    finally
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                        _legacyStopping = false;
                    }
                    if (_legacySessionId != sessionId)
                        return LegacyError("Terminate outcome is unknown; do not retry automatically.", "unknown");
                    if (outcome == "unsupported")
                    {
                        lease.Dispose();
                        terminateFallback = true;
                        name = StopDebug;
                    }
                    else
                    {
                        _legacyInspectionSessionId = null;
                        _legacyInspectionContextId = null;
                        _legacyInspectionMuxId = null;
                        _legacyCurrentFrameId = null;
                        _legacyInspectionRevision++;
                        var observed = outcome == "terminated" ? "terminated" : LegacySnapshot(sessionId).State;
                        if (outcome == "unknown") return LegacyError("Terminate outcome is unknown; do not retry automatically.", observed);
                        var message = outcome == "timeout" ? "Terminate sent but program did not exit within 10s." : "Program terminated gracefully.";
                        return LegacyResponseWithMessage(new { state = observed }, observed, message);
                    }
                }
                if (name == StopDebug && _legacyAttachSession is not null && _legacyAttachSlot is { } attachedSlot)
                {
                    _legacyAttachClosing = true;
                    _legacySessionGeneration++;
                    _legacyInspectionSessionId = null;
                    _legacyInspectionContextId = null;
                    _legacyInspectionMuxId = null;
                    _legacyCurrentFrameId = null;
                    _legacyInspectionRevision++;
                    var retiredAttachResources = _legacyResources.Retire();
                    var close = attachedSlot.CloseAndDrainAsync();
                    _legacyGate.Release();
                    gateHeld = false;
                    try { await Task.WhenAny(close, Task.Delay(StopTimeout)).ConfigureAwait(false); }
                    finally
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                    }
                    if (close.IsCompletedSuccessfully && _legacySessionId == sessionId)
                    {
                        OutputLifetime? retiringOutput;
                        lock (_lifetimeGate) _outputs.Remove(sessionId, out retiringOutput);
                        _legacyOutputEvidence.Clear();
                        if (_legacyBreakpointObserverSession is { } breakpointSession)
                            breakpointSession.WithLegacyBreakpointLock(() =>
                            {
                                ReconcileLegacyBreakpointObservations(breakpointSession);
                                _legacyBreakpointObservationsIncomplete = LegacyHasStoppingBreakpoints() && !breakpointSession.LegacyBreakpointEventsComplete;
                                return true;
                            });
                        ClearLegacyGroupsAfterConfirmedStop();
                        foreach (var key in _legacyBreakpointObservations.Keys.ToArray())
                            _legacyBreakpointObservations[key] = _legacyBreakpointObservations[key] with { HitCount = 0 };
                        _legacySessionId = null;
                        _legacyAttachSession = null;
                        _legacyAttachSlot = null;
                        _legacyAttachClosing = false;
                        _legacyBreakpointObserverSession = null;
                        _legacyHasOwner = false;
                        _legacyOwner = null;
                        var idleGeneration = _legacyResources.Begin();
                        if (retiringOutput?.Buffer.Snapshot(false, null).Length > 0) idleGeneration.Observe("output");
                        idleGeneration.Observe("thread");
                        _legacyResources.Publish(idleGeneration);
                        return cleanupForce
                            ? LegacyCleanupResult(cleanupObservations!, false, "CLR detach is unconfirmed; adapter cleanup completed without proving detach." + (smokeDrainPending ? " Runtime smoke is still draining." : ""), "unknown")
                            : LegacyError("CLR detach is unconfirmed; adapter cleanup completed without proving detach." + (smokeDrainPending ? " Runtime smoke is still draining." : ""), "unknown");
                    }
                    else if (close.IsFaulted) _ = close.Exception;
                    _legacyResources.Restore(retiredAttachResources);
                    return cleanupForce
                        ? LegacyCleanupResult(cleanupObservations!, false, "CLR detach is unconfirmed; the attach owner is retained until safe cleanup." + (smokeDrainPending ? " Runtime smoke is still draining." : ""), "unknown")
                        : LegacyError("CLR detach is unconfirmed; the attach owner is retained until safe cleanup." + (smokeDrainPending ? " Runtime smoke is still draining." : ""), "unknown");
                }
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
                if (name == PauseExecution)
                {
                    if (_sessions.TryGetValue(sessionId, out var pauseSession)) pauseSession.VetoQuickPause();
                    _legacyTracePauseVeto = true;
                    _legacyTraceRevision++;
                    _legacyTracePauseRevision++;
                }
                if (name == GetExceptionInfo && arguments.TryGetValue("thread_id", out var exceptionThread)
                    && TryReadOutputLineCount(exceptionThread, out var exceptionThreadNumber) && exceptionThreadNumber == 0)
                    arguments["thread_id"] = JsonSerializer.SerializeToElement<int?>(null);
                if (name == GetExceptionContext && !arguments.ContainsKey("max_inner_exceptions"))
                    arguments["max_inner_exceptions"] = JsonSerializer.SerializeToElement(5);
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
                if (name == ApplyCodeChange)
                {
                    _legacyInspectionSessionId = null;
                    _legacyInspectionContextId = null;
                    _legacyInspectionMuxId = null;
                    _legacyCurrentFrameId = null;
                    _legacyInspectionRevision++;
                }
                var inspectionContext = name is GetScopes or GetVariables or EvaluateExpression or GetStepInTargets ? _legacyInspectionContextId : null;
                NetCoreDbgSession? inspectionSession = null;
                if (name is GetCallStack or GetScopes or GetVariables or EvaluateExpression or SetVariable or GetExceptionInfo
                    or GetStepInTargets or GetModules or GetProgress or GetLoadedSources or GetStopContext or GetExceptionContext || targetedStep)
                {
                    if (!_sessions.TryGetValue(sessionId, out inspectionSession))
                        return LegacyError("No active debug session.");
                }
                var exceptionContext = name is GetExceptionInfo or GetStopContext or GetExceptionContext ? inspectionSession!.CaptureStoppedContext() : null;
                if (name is (GetExceptionInfo or GetStopContext or GetExceptionContext) && exceptionContext is null)
                    return LegacyError("No current stopped context.");
                var request = LegacyRequest(name, sessionId, arguments);
                if (name == StopDebug)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (_sessions.TryGetValue(sessionId, out var stopSession)) stopSession.VetoQuickPause();
                    _legacySessionGeneration++;
                    _legacyStopping = true;
                    OutputLifetime? retiringOutput;
                    lock (_lifetimeGate) _outputs.TryGetValue(sessionId, out retiringOutput);
                    var breakpointSession = _legacyBreakpointObserverSession;
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
                    if (stopped.IsError == true)
                    {
                        _legacyResources.Restore(retired);
                        if (cleanupForce)
                        {
                            var error = stopped.StructuredContent is { } failure && failure.TryGetProperty("error", out var value)
                                ? value.GetString() : "Debug session cleanup is unconfirmed.";
                            return LegacyCleanupResult(cleanupObservations!, false, error);
                        }
                        return LegacyNativeError(stopped);
                    }
                    _legacyOutputEvidence.Clear();
                    if (breakpointSession is not null)
                        breakpointSession.WithLegacyBreakpointLock(() =>
                        {
                            ReconcileLegacyBreakpointObservations(breakpointSession);
                            _legacyBreakpointObservationsIncomplete = LegacyHasStoppingBreakpoints() && !breakpointSession.LegacyBreakpointEventsComplete;
                            return true;
                        });
                    ClearLegacyGroupsAfterConfirmedStop();
                    foreach (var key in _legacyBreakpointObservations.Keys.ToArray())
                        _legacyBreakpointObservations[key] = _legacyBreakpointObservations[key] with { HitCount = 0 };
                    _legacySessionId = null;
                    _legacyBreakpointObserverSession = null;
                    _legacyHasOwner = false;
                    _legacyOwner = null;
                    var idleGeneration = _legacyResources.Begin();
                    if (retiringOutput?.Buffer.Snapshot(false, null).Length > 0) idleGeneration.Observe("output");
                    idleGeneration.Observe("thread");
                    _legacyResources.Publish(idleGeneration);
                    if (smokeDrainPending)
                        return cleanupForce
                            ? LegacyCleanupResult(cleanupObservations!, false, "Debugger stopped; runtime smoke is still draining.", "idle")
                            : LegacyError("Debugger stopped; runtime smoke is still draining.", "idle");
                    if (cleanupForce) return LegacyCleanupResult(cleanupObservations!, true);
                    return terminateFallback
                        ? LegacyResponseWithMessage(new { state = "idle" }, "idle", "Adapter does not support terminate; used disconnect instead.")
                        : LegacyResponse(new { success = true });
                }
                if (compositeMayEvaluate || name is WriteMemory or ApplyCodeChange)
                {
                    _legacyCompositeEvaluations++;
                    compositeEvaluationHeld = true;
                }
                if (_legacyTracePauseVeto && name is (ContinueExecution or StepOver or StepInto or StepOut))
                {
                    if (_legacyTraceResumePending is not null) return LegacyError("An execution request is still settling.");
                    traceResumePending = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                    _legacyTraceResumePending = traceResumePending;
                    _legacyTraceRevision++;
                    traceResumeRevision = _legacyTracePauseRevision;
                }
                _legacyGate.Release();
                gateHeld = false;
                if (name == ApplyCodeChange)
                {
                    var enc = await LegacyApplyCodeChangeAsync(sessionId, encRoot!, encFile!, encEdits!, cancellationToken).ConfigureAwait(false);
                    await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                    gateHeld = true;
                    if (!StringComparer.Ordinal.Equals(_legacySessionId, sessionId))
                        return LegacyError("The debug session changed; the source or runtime may have changed. Do not retry automatically.", "unknown");
                    var state = LegacySnapshot(sessionId).State;
                    if (!enc.Success)
                        return LegacyError(enc.Error + (enc.BackupPath is null ? "" : $" Original backup: {enc.BackupPath}"), state);
                    return LegacyResponseWithMessage(new
                    {
                        success = true,
                        file = enc.File,
                        module = enc.Module,
                        deltas = new { metadata_path = enc.MetadataPath, il_path = enc.IlPath, pdb_path = enc.PdbPath },
                        apply = enc.Apply ?? JsonSerializer.SerializeToElement(new { }),
                        backup_path = enc.BackupPath,
                    }, state, "Code change applied. Select a fresh stack before inspection.");
                }
                if (name is ReadMemory or WriteMemory or Disassemble or GetLocations)
                {
                    var memory = name == WriteMemory
                        ? await LegacyWriteMemoryAsync(sessionId, arguments, cancellationToken).ConfigureAwait(false)
                        : await LegacyMemoryInspectionAsync(name, sessionId, arguments, cancellationToken).ConfigureAwait(false);
                    await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                    gateHeld = true;
                    if (!StringComparer.Ordinal.Equals(_legacySessionId, sessionId)) return LegacyInspectionChanged(name);
                    var state = LegacySnapshot(sessionId).State;
                    if (memory.Context is not null && (!_sessions.TryGetValue(sessionId, out var current) ||
                        !ReferenceEquals(current, memory.Session) || current.CaptureInspectionState(memory.Context) is null))
                        return LegacyInspectionChanged(name);
                    if (memory.Error is not null) return LegacyError(memory.Error, state);
                    return name == WriteMemory
                        ? LegacyResponseWithMessage(memory.Data!, state, "Memory write completed. Debuggee state may have changed.")
                        : LegacyBoundedUtilityResponse(memory.Data!, state);
                }
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
                    GetStopContext => await GetCompositeContextAsync(request, false, cancellationToken, legacy: true, expectedContext: exceptionContext!.Value.Context).ConfigureAwait(false),
                    GetExceptionContext => await GetCompositeContextAsync(request, true, cancellationToken, legacy: true, expectedContext: exceptionContext!.Value.Context).ConfigureAwait(false),
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
                    return name is GetCallStack or GetScopes or GetVariables or EvaluateExpression or SetVariable or GetExceptionInfo or GetStopContext or GetExceptionContext or GetStepInTargets
                        ? LegacyInspectionChanged(compositeMayEvaluate ? EvaluateExpression : name)
                        : LegacyError(consumesOutput ? "Output may have been cleared; do not retry automatically."
                            : name == ConfigureExceptions ? "Exception filters may have changed; do not retry automatically."
                            : targetedStep ? "Step outcome is unknown; it may have executed. Do not retry automatically."
                            : name is (AddBreakpoint or RemoveBreakpoint) ? "Requested breakpoint intent was retained; application to the live debugger is unknown. Do not retry automatically."
                            : "Debug session changed before operation completed.", "unknown");
                if (name is (AddBreakpoint or RemoveBreakpoint) && result.IsError != true
                    && result.StructuredContent is { } breakpointContent && breakpointContent.TryGetProperty("kind", out var breakpointKind)
                    && breakpointKind.GetString() is ("breakpoint_added" or "breakpoint_removed"))
                    _legacyUnconfirmedBreakpointFiles.Remove(breakpointFile!);
                if (traceResumePending is not null)
                {
                    if (result.IsError != true && result.StructuredContent is { } resumeContent
                        && resumeContent.TryGetProperty("kind", out var resumeKind)
                        && (resumeKind.GetString() is "continue_stopped" or "continue_terminal" or "step_over_success"
                            or "step_into_success" or "step_out_success" or "step_into_terminal" or "step_out_terminal"
                            || (resumeKind.GetString() == "continue_timed_out" && resumeContent.TryGetProperty("acknowledged", out var acknowledged)
                                && acknowledged.ValueKind == JsonValueKind.True))
                        && traceResumeRevision == _legacyTracePauseRevision)
                        _legacyTracePauseVeto = false;
                    _legacyTraceResumePending = null;
                    traceResumePending.TrySetResult(true);
                    traceResumePending = null;
                }
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
                    if (compositeMayEvaluate && result.StructuredContent is { } compositeFailure
                        && compositeFailure.TryGetProperty("error", out var compositeCode)
                        && compositeCode.GetString() is "DEBUG_CONTEXT_EVALUATION_UNKNOWN" or "DEBUG_CONTEXT_STALE")
                        return LegacyError("Evaluation outcome is unknown; it may have executed. Do not retry automatically.", "unknown");
                    if (compositeMayEvaluate && result.StructuredContent is { } evaluationFailure
                        && evaluationFailure.TryGetProperty("error", out var evaluationCode)
                        && evaluationCode.GetString() == "DEBUG_CONTEXT_EVALUATION_FAILED")
                        return LegacyError("Evaluation failed and may have executed. Do not retry automatically.", observedState);
                    if (name is AddBreakpoint or RemoveBreakpoint)
                    {
                        if (result.StructuredContent is { } breakpointFailure
                            && breakpointFailure.TryGetProperty("error", out var breakpointCode)
                            && breakpointCode.GetString() == "DAP_SET_BREAKPOINTS_REFUSED")
                            return LegacyError("Requested breakpoint intent was retained; the live debugger rejected the file set. A later explicit add/remove for this file or explicit start can reapply it.", observedState);
                        if (result.StructuredContent is { } unsupported
                            && unsupported.TryGetProperty("error", out var unsupportedCode)
                            && unsupportedCode.GetString() == "DAP_BREAKPOINT_OPTIONS_UNSUPPORTED")
                            return LegacyError("Requested breakpoint intent was retained; the debugger does not advertise support for the requested condition.", observedState);
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
                        _legacyResources.SignalSession(sessionId, "state");
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
                if (name is GetStopContext or GetExceptionContext)
                {
                    if (inspectionSession is null || exceptionContext is not { } captured || stackRevision != _legacyInspectionRevision
                        || !_sessions.TryGetValue(sessionId, out var currentSession) || !ReferenceEquals(currentSession, inspectionSession)
                        || captured.State.CurrentThreadId is not { } stoppedThread
                        || !inspectionSession.IsStoppedContextCurrent(stoppedThread, captured.Context)
                        || content.GetProperty("inspectionContextId").GetString() != captured.Context)
                        return compositeMayEvaluate ? LegacyInspectionChanged(EvaluateExpression) : LegacyInspectionChanged(name);
                    long? hitCount = null;
                    if (name == GetStopContext && content.GetProperty("frames") is { } frameList && frameList.GetArrayLength() > 0)
                    {
                        var first = frameList[0];
                        if (first.GetProperty("source").ValueKind == JsonValueKind.String && first.GetProperty("line").TryGetInt32(out var line) && line > 0)
                        {
                            var path = first.GetProperty("source").GetString()!;
                            if (!NetCoreDbgSession.TryNormalizeLegacyBreakpointSource(path, out var source, requireExistingFile: false))
                                return LegacyError("Breakpoint hit count source is unavailable.", "stopped");
                            hitCount = WithLegacyBreakpointState<long?>(() =>
                            {
                                if (!LegacyBreakpointObservationsComplete() || !inspectionSession.LegacyHitCountsComplete) return null;
                                var requested = _legacyBreakpoints.FirstOrDefault(item => _legacyBreakpointPathComparer.Equals(item.File, source)
                                    && (_legacyBreakpointObservations.TryGetValue(item.Incarnation, out var observation) ? (observation.DapLine ?? item.Line) : item.Line) == line);
                                return inspectionSession.GetLegacyHitCount(source, requested.File is null ? line : requested.Line);
                            });
                            if (hitCount is null) return LegacyError("Breakpoint hit counts are pending or unavailable.", "stopped");
                        }
                    }
                    var mapped = LegacyCompositeResult(content, name == GetExceptionContext, hitCount);
                    if (!inspectionSession.IsStoppedContextCurrent(stoppedThread, captured.Context))
                        return compositeMayEvaluate ? LegacyInspectionChanged(EvaluateExpression) : LegacyInspectionChanged(name);
                    return mapped;
                }
                if (name == AddBreakpoint && kind == "breakpoint_added")
                {
                    return LegacyResponse(new { file = content.GetProperty("file"), line = content.GetProperty("line"), condition = breakpointCondition, verified = content.GetProperty("verified") }, observedState);
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
                if (traceResumePending is not null)
                {
                    if (!gateHeld)
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                    }
                    if (ReferenceEquals(_legacyTraceResumePending, traceResumePending)) _legacyTraceResumePending = null;
                    traceResumePending.TrySetResult(false);
                }
                if (compositeEvaluationHeld)
                {
                    if (!gateHeld)
                    {
                        await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                        gateHeld = true;
                    }
                    _legacyCompositeEvaluations--;
                    if (_legacyOwner is not null) _legacyOwnerActivity = Stopwatch.GetTimestamp();
                }
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

        private bool ApplyLegacyBreakpoint(string file, int line, bool remove, string? condition = null, string? hitCondition = null)
        {
            var index = FindLegacyBreakpoint(file, line);
            if (remove)
            {
                if (index >= 0)
                {
                    _legacyBreakpointObservations.Remove(_legacyBreakpoints[index].Incarnation);
                    _legacyBreakpoints.RemoveAt(index);
                    if (!LegacyHasStoppingBreakpoints()) _legacyBreakpointObservationsIncomplete = false;
                }
                return index >= 0;
            }
            if (index < 0)
            {
                var incarnation = ++_legacyBreakpointIncarnation;
                var sharedIdentity = LegacyPhysicalIdentity(file, line) ?? incarnation;
                _legacyBreakpoints.Add((file, line, incarnation, sharedIdentity, condition, hitCondition));
            }
            else
            {
                var existing = _legacyBreakpoints[index];
                if (existing.Condition != condition || existing.HitCondition != hitCondition)
                {
                    _legacyBreakpointObservations.Remove(existing.Incarnation);
                    _legacyBreakpoints[index] = (existing.File, existing.Line, ++_legacyBreakpointIncarnation, existing.Identity, condition, hitCondition);
                }
            }
            return false;
        }

        private void ScheduleLegacyTrace(long generation) => _ = Task.Run(() => ProcessLegacyTraceAsync(generation));

        private bool IsLegacyTraceCurrent(long generation, string sessionId, NetCoreDbgSession session, int threadId, string context) =>
            _legacySessionGeneration == generation && !_legacyStarting && !_legacyStopping
            && _legacySessionId == sessionId && ReferenceEquals(_legacyBreakpointObserverSession, session)
            && session.IsStoppedContextCurrent(threadId, context);

        private async Task<DapEvaluateResult?> EvaluateLegacyTraceAsync(long generation, string sessionId, NetCoreDbgSession session,
            int threadId, string context, int frameId, string expression, long revision, long observationRevision, CancellationToken cancellationToken)
        {
            await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            Task<DapEvaluateResult> evaluation;
            try
            {
                if (!IsLegacyTraceCurrent(generation, sessionId, session, threadId, context) || revision != _legacyTraceRevision
                    || !_legacyBreakpointGate.Wait(0)) return null;
                try
                {
                    var admitted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                    evaluation = session.EvaluateTracepointAsync(context, frameId, expression, admitted, observationRevision, cancellationToken);
                    await admitted.Task.ConfigureAwait(false);
                }
                finally { _legacyBreakpointGate.Release(); }
            }
            finally { _legacyGate.Release(); }
            return await evaluation.ConfigureAwait(false);
        }

        private async Task ProcessLegacyTraceAsync(long generation)
        {
            NetCoreDbgSession? session = null;
            SessionSlot? slot = null;
            SessionSlot.SessionLease? lease = null;
            string? sessionId = null;
            string? context = null;
            var threadId = 0;
            var revision = 0L;
            var counted = false;
            var close = false;
            try
            {
                while (true)
                {
                    Task? pendingResume;
                    await _legacyGate.WaitAsync().ConfigureAwait(false);
                    try
                    {
                        pendingResume = _legacyTraceResumePending?.Task;
                        if (pendingResume is null)
                        {
                            if (_legacySessionGeneration != generation || _legacyStarting || _legacyStopping || _legacyTracepoints.Count == 0
                                || _legacySessionId is not { } id || _legacyBreakpointObserverSession is not { } current
                                || !_slots.TryGetValue(id, out slot) || current.CaptureStoppedContext() is not { } stopped
                                || stopped.State.StopReason != "breakpoint" || stopped.State.CurrentThreadId is not { } thread
                                || _legacyTraceLastContext == stopped.Context) return;
                            lease = slot.TryAcquire();
                            if (lease is null) return;
                            session = current;
                            sessionId = id;
                            threadId = thread;
                            context = stopped.Context;
                            revision = _legacyTraceRevision;
                            _legacyTraceLastContext = context;
                            _legacyCompositeEvaluations++;
                            counted = true;
                            break;
                        }
                    }
                    finally { _legacyGate.Release(); }
                    await pendingResume.ConfigureAwait(false);
                }

                using var operation = CancellationTokenSource.CreateLinkedTokenSource(lease.AbortToken);
                await session.WaitForExecutionSettlementAsync().WaitAsync(operation.Token).ConfigureAwait(false);
                if (!session.IsStoppedContextCurrent(threadId, context)) return;
                var stack = await session.GetCallStackAsync(threadId, 0, 1, true, operation.Token).ConfigureAwait(false);
                if (stack.IsRefused || stack.Frames.Count == 0 || stack.InspectionContextId != context
                    || stack.Frames[0].Source is not { } source || stack.Frames[0].Line is < 1 or > int.MaxValue
                    || !NetCoreDbgSession.TryNormalizeLegacyBreakpointSource(source, out var file, requireExistingFile: false)) return;
                var frame = stack.Frames[0];
                LegacyTracepoint[] points;
                string? condition = null;
                var observationRevision = 0L;
                var mayContinue = true;
                await _legacyGate.WaitAsync(operation.Token).ConfigureAwait(false);
                try
                {
                    if (!IsLegacyTraceCurrent(generation, sessionId, session, threadId, context) || revision != _legacyTraceRevision) return;
                    if (!IsLegacyTraceSourceConfirmed(file)) return;
                    mayContinue = !_legacyTracePauseVeto;
                    points = WithLegacyBreakpointState(() =>
                    {
                        observationRevision = session.LegacyBreakpointRevision;
                        return _legacyTracepoints.Where(point => LegacyTraceObservationCurrent(point) && _legacyBreakpointPathComparer.Equals(point.File, file)
                            && (point.DapLine ?? point.Line) == frame.Line).ToArray();
                    });
                    if (points.Length == 0) return;
                    var physicalLine = points[0].Line;
                    if (points.Any(point => point.Line != physicalLine) || !session.LegacyBreakpointEventsComplete
                        || _legacyFunctionBreakpoints.Count != 0) return;
                    foreach (var point in _legacyTracepoints)
                        if (_legacyBreakpointPathComparer.Equals(point.File, file)
                            && (!LegacyTraceObservationCurrent(point) || ((point.DapLine ?? point.Line) == frame.Line && point.Line != physicalLine))) return;
                    foreach (var groupPoint in _legacyGroups.Values.SelectMany(static group => group.Breakpoints))
                    {
                        var sameFile = _legacyBreakpointPathComparer.Equals(groupPoint.File, file);
                        if (!sameFile && !_legacyBreakpointPathComparer.Equals(Path.GetFileName(groupPoint.File), Path.GetFileName(file))) continue;
                        if (!groupPoint.Verified) return;
                        if ((groupPoint.DapLine ?? groupPoint.Line) != frame.Line) continue;
                        if (!sameFile || groupPoint.Line != physicalLine) return;
                        mayContinue = false;
                    }
                    foreach (var user in _legacyBreakpoints)
                    {
                        var sameFile = _legacyBreakpointPathComparer.Equals(user.File, file);
                        if (!sameFile && !_legacyBreakpointPathComparer.Equals(Path.GetFileName(user.File), Path.GetFileName(file))) continue;
                        if (!_legacyBreakpointObservations.TryGetValue(user.Incarnation, out var observed) || !observed.Verified)
                            return;
                        if ((observed.DapLine ?? user.Line) != frame.Line) continue;
                        if (!sameFile || user.Line != physicalLine) return;
                        if (string.IsNullOrEmpty(user.Condition) || !string.IsNullOrEmpty(user.HitCondition))
                            mayContinue = false;
                        else condition = user.Condition;
                    }
                    foreach (var point in points) point.HitCount++;
                }
                finally { _legacyGate.Release(); }

                if (mayContinue && condition is not null)
                {
                    using var timeout = CancellationTokenSource.CreateLinkedTokenSource(operation.Token);
                    timeout.CancelAfter(TimeSpan.FromMilliseconds(500));
                    var result = await EvaluateLegacyTraceAsync(generation, sessionId, session, threadId, context, frame.Id, condition, revision, observationRevision, timeout.Token).ConfigureAwait(false);
                    if (result?.Outcome == DapEvaluateOutcome.OutcomeUnknown) { close = true; return; }
                    mayContinue = result is { Outcome: DapEvaluateOutcome.Success, Type: "bool", Result: "false" };
                }

                var entries = new List<LegacyTraceEntry>(points.Length);
                foreach (var point in points)
                {
                    var now = (double)Stopwatch.GetTimestamp() / Stopwatch.Frequency;
                    string value;
                    if (now - point.LastHit < 0.1) value = "<rate limited>";
                    else
                    {
                        point.LastHit = now;
                        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(operation.Token);
                        timeout.CancelAfter(TimeSpan.FromMilliseconds(500));
                        var result = await EvaluateLegacyTraceAsync(generation, sessionId, session, threadId, context, frame.Id, point.Expression, revision, observationRevision, timeout.Token).ConfigureAwait(false);
                        if (result is null) { mayContinue = false; break; }
                        if (result.Outcome == DapEvaluateOutcome.OutcomeUnknown) { close = true; return; }
                        if (result.Outcome != DapEvaluateOutcome.Success) { mayContinue = false; value = "<error: evaluation failed>"; }
                        else value = result.Result ?? string.Empty;
                        if (Encoding.UTF8.GetByteCount(value) > 16 * 1024) value = "<result exceeds limit>";
                    }
                    entries.Add(new LegacyTraceEntry((double)Stopwatch.GetTimestamp() / Stopwatch.Frequency, point.File, point.Line, point.Expression, value, threadId, point.Id));
                }

                await _legacyGate.WaitAsync(operation.Token).ConfigureAwait(false);
                Task<DapContinueResult>? continuation = null;
                try
                {
                    if (!IsLegacyTraceCurrent(generation, sessionId, session, threadId, context)) return;
                    if (revision != _legacyTraceRevision || _legacyTracePauseVeto) mayContinue = false;
                    if (mayContinue && _legacyBreakpointGate.Wait(0))
                    {
                        try
                        {
                            var admitted = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                            continuation = session.ContinueTracepointAsync(threadId, context, admitted, observationRevision, operation.Token);
                            await admitted.Task.ConfigureAwait(false);
                        }
                        finally { _legacyBreakpointGate.Release(); }
                    }
                    foreach (var entry in entries)
                    {
                        if (_legacyTraceLog.Count == 1000) { _legacyTraceLog.Dequeue(); _legacyTraceDropCount++; }
                        _legacyTraceLog.Enqueue(entry);
                        _legacyTraceAppendCount++;
                    }
                }
                finally { _legacyGate.Release(); }
                if (continuation is not null)
                {
                    var result = await continuation.ConfigureAwait(false);
                    close = result.RequiresDrain;
                }
            }
            catch (Exception exception)
            {
                close |= session?.RequiresInspectionDrain() == true || exception is DapContinueCanceledException { RequiresDrain: true }
                    || exception is not OperationCanceledException;
            }
            finally
            {
                if (counted)
                {
                    await _legacyGate.WaitAsync().ConfigureAwait(false);
                    try { _legacyCompositeEvaluations--; }
                    finally { _legacyGate.Release(); }
                }
                lease?.Dispose();
                if (close && slot is not null) { try { await slot.CloseAndDrainAsync().ConfigureAwait(false); } catch (Exception) { } }
            }
        }

        private T WithLegacyBreakpointState<T>(Func<T> action)
        {
            if (_legacyBreakpointObserverSession is not { } session) return action();
            return session.WithLegacyBreakpointLock(() =>
            {
                ReconcileLegacyBreakpointObservations(session);
                return action();
            });
        }

        private bool LegacyHasStoppingBreakpoints() => _legacyBreakpoints.Count != 0 || _legacyGroups.Values.Any(static group => group.Breakpoints.Count != 0);
        private bool LegacyBreakpointObservationsComplete() => !LegacyHasStoppingBreakpoints()
            || (_legacyBreakpointObserverSession is { } session ? session.LegacyBreakpointEventsComplete : !_legacyBreakpointObservationsIncomplete);

        private void ReconcileLegacyBreakpointObservations(NetCoreDbgSession session)
        {
            foreach (var observation in session.CaptureLegacyBreakpoints())
            {
                var index = FindLegacyBreakpoint(observation.File, observation.Line);
                var physical = SnapshotLegacyBreakpointLines(observation.File).FirstOrDefault(line => line.Line == observation.Line);
                if (physical.Incarnation != observation.Incarnation) continue;
                foreach (var point in _legacyTracepoints)
                {
                    if (point.Line != observation.Line || !_legacyBreakpointPathComparer.Equals(point.File, observation.File)) continue;
                    point.ObservedIncarnation = observation.Incarnation;
                    point.Verified = observation.Verified && !observation.Removed;
                    point.DapLine = observation.DapLine;
                    point.BreakpointId = observation.Id;
                }
                foreach (var point in _legacyGroups.Values.SelectMany(static group => group.Breakpoints))
                {
                    if (point.Line != observation.Line || !_legacyBreakpointPathComparer.Equals(point.File, observation.File)) continue;
                    point.ObservedIncarnation = observation.Incarnation;
                    point.Verified = observation.Verified && !observation.Removed;
                    point.DapLine = observation.DapLine;
                    point.Id = observation.Id;
                }
                if (index < 0 || _legacyBreakpoints[index].Incarnation != observation.Incarnation) continue;
                if (observation.Removed)
                {
                    _legacyBreakpoints.RemoveAt(index);
                    _legacyBreakpointObservations.Remove(observation.Incarnation);
                }
                else _legacyBreakpointObservations[observation.Incarnation] = observation;
            }
            if (!LegacyHasStoppingBreakpoints()) _legacyBreakpointObservationsIncomplete = false;
        }
        private void ReconcileLegacyFunctionBreakpoints()
        {
            if (_legacyBreakpointObserverSession is not { } session) return;
            foreach (var observed in session.CaptureLegacyFunctionBreakpoints())
            {
                var index = _legacyFunctionBreakpoints.FindIndex(item => item.Incarnation == observed.Incarnation);
                if (index >= 0) _legacyFunctionBreakpoints[index] = observed;
            }
        }


        private Dictionary<string, List<object>> LegacyBreakpointSnapshot(string? file, bool includeHits)
        {
            var result = new Dictionary<string, List<object>>(_legacyBreakpointPathComparer);
            if (file is not null) result[file] = [];
            foreach (var breakpoint in _legacyBreakpoints)
            {
                if (file is not null && !_legacyBreakpointPathComparer.Equals(file, breakpoint.File)) continue;
                if (!result.TryGetValue(breakpoint.File, out var entries)) result[breakpoint.File] = entries = [];
                _legacyBreakpointObservations.TryGetValue(breakpoint.Incarnation, out var observed);
                entries.Add(includeHits
                    ? new { line = breakpoint.Line, dap_line = observed?.DapLine, condition = breakpoint.Condition, verified = observed?.Verified ?? false, hit_count = _legacyBreakpointObserverSession?.GetLegacyHitCount(breakpoint.File, breakpoint.Line) ?? 0 }
                    : new { line = breakpoint.Line, dap_line = observed?.DapLine, condition = breakpoint.Condition, verified = observed?.Verified ?? false });
            }
            var groupedLocations = new List<(string File, int Line)>();
            foreach (var point in _legacyGroups.Values.SelectMany(static group => group.Breakpoints))
            {
                if (file is not null && !_legacyBreakpointPathComparer.Equals(file, point.File)) continue;
                if (FindLegacyBreakpoint(point.File, point.Line) >= 0 || groupedLocations.Any(item => item.Line == point.Line && _legacyBreakpointPathComparer.Equals(item.File, point.File))) continue;
                groupedLocations.Add((point.File, point.Line));
                if (!result.TryGetValue(point.File, out var entries)) result[point.File] = entries = [];
                entries.Add(includeHits
                    ? new { line = point.Line, dap_line = point.DapLine, condition = point.Condition, verified = point.Verified, hit_count = _legacyBreakpointObserverSession?.GetLegacyHitCount(point.File, point.Line) ?? 0 }
                    : new { line = point.Line, dap_line = point.DapLine, condition = point.Condition, verified = point.Verified });
            }
            if (includeHits && file is null && _legacyFunctionBreakpoints.Count > 0)
            {
                ReconcileLegacyFunctionBreakpoints();
                result["__function_breakpoints__"] = _legacyFunctionBreakpoints.Select(static point => (object)new { function = point.Name, condition = point.Condition, hit_condition = point.HitCondition, verified = point.Verified }).ToList();
            }
            return result;
        }

        private bool LegacyTraceObservationCurrent(LegacyTracepoint point)
        {
            if (!point.Verified) return false;
            var physical = SnapshotLegacyBreakpointLines(point.File).FirstOrDefault(line => line.Line == point.Line);
            return point.ObservedIncarnation == physical.Incarnation;
        }

        private void MarkLegacyBreakpointUnconfirmed(string file)
        {
            if (_legacyUnconfirmedBreakpointFiles.Count >= MaximumBreakpointCount && !_legacyUnconfirmedBreakpointFiles.Contains(file))
                _legacyUnconfirmedBreakpointOverflow = true;
            else _legacyUnconfirmedBreakpointFiles.Add(file);
        }

        private bool IsLegacyGroupSourceQuarantined(string file) => _legacyGroupUnconfirmedFiles.Contains(file);

        private bool IsLegacyGroupMutationSourceConfirmed(string file)
        {
            if (_legacyUnconfirmedBreakpointOverflow || IsLegacyGroupSourceQuarantined(file)) return false;
            foreach (var pending in _legacyUnconfirmedBreakpointFiles)
            {
                if (_legacyGroupUnconfirmedFiles.Contains(pending)) continue;
                if (_legacyBreakpointPathComparer.Equals(pending, file)
                    || _legacyBreakpointPathComparer.Equals(Path.GetFileName(pending), Path.GetFileName(file))) return false;
            }
            return true;
        }

        private bool LegacyOwnersCompatible(string file, int line, LegacyInstrumentationGroup? candidate = null,
            bool overrideDirect = false, string? directCondition = null, string? directHitCondition = null, bool addTrace = false)
        {
            string? predicate = null;
            string? hitPredicate = null;
            var havePredicate = false;
            var conflict = false;
            var conditionalGroup = false;
            void AddStopping(string? condition, string? hit, bool groupOwner)
            {
                condition = string.IsNullOrEmpty(condition) ? null : condition;
                hit = string.IsNullOrEmpty(hit) ? null : hit;
                if (condition is null && hit is null) return;
                if (groupOwner) conditionalGroup = true;
                if (havePredicate && (predicate != condition || hitPredicate != hit)) conflict = true;
                else { havePredicate = true; predicate = condition; hitPredicate = hit; }
            }
            var direct = FindLegacyBreakpoint(file, line);
            if (overrideDirect) AddStopping(directCondition, directHitCondition, false);
            else if (direct >= 0) AddStopping(_legacyBreakpoints[direct].Condition, _legacyBreakpoints[direct].HitCondition, false);
            foreach (var group in _legacyGroups.Values)
                foreach (var point in group.Breakpoints)
                    if (point.Line == line && _legacyBreakpointPathComparer.Equals(point.File, file))
                        AddStopping(point.Condition, point.HitCondition, true);
            if (candidate is not null)
                foreach (var point in candidate.Breakpoints)
                    if (point.Line == line && _legacyBreakpointPathComparer.Equals(point.File, file))
                        AddStopping(point.Condition, point.HitCondition, true);
            var hasTrace = addTrace || _legacyTracepoints.Any(point => point.Line == line && _legacyBreakpointPathComparer.Equals(point.File, file))
                || (candidate is not null && candidate.Tracepoints.Any(point => point.Line == line && _legacyBreakpointPathComparer.Equals(point.File, file)));
            return !conflict && !(conditionalGroup && hasTrace);
        }

        private bool IsLegacyTraceSourceConfirmed(string file)
        {
            if (_legacyUnconfirmedBreakpointOverflow) return false;
            foreach (var pending in _legacyGroupUnconfirmedFiles)
                if (_legacyBreakpointPathComparer.Equals(pending, file)
                    || _legacyBreakpointPathComparer.Equals(Path.GetFileName(pending), Path.GetFileName(file))) return false;
            foreach (var pending in _legacyUnconfirmedBreakpointFiles)
                if (_legacyBreakpointPathComparer.Equals(pending, file)
                    || _legacyBreakpointPathComparer.Equals(Path.GetFileName(pending), Path.GetFileName(file))) return false;
            return true;
        }

        private void ClearLegacyGroupsAfterConfirmedStop()
        {
            if (_legacyGroups.Count != 0)
            {
                foreach (var group in _legacyGroups.Values)
                    foreach (var point in group.Tracepoints) _legacyTracepoints.Remove(point);
                _legacyGroups.Clear();
                _legacyTraceRevision++;
                _legacyResources.SignalBreakpoints();
            }
            if (_legacyBreakpoints.Count == 0) _legacyBreakpointObservationsIncomplete = false;
            _legacyGroupUnconfirmedFiles.Clear();
            _legacyUnconfirmedBreakpointFiles.Clear();
            _legacyUnconfirmedBreakpointOverflow = false;
        }

        private long? LegacyPhysicalIdentity(string file, int line)
        {
            var direct = FindLegacyBreakpoint(file, line);
            if (direct >= 0) return _legacyBreakpoints[direct].Identity;
            var grouped = _legacyGroups.Values.SelectMany(static group => group.Breakpoints)
                .FirstOrDefault(point => point.Line == line && _legacyBreakpointPathComparer.Equals(point.File, file));
            if (grouped is not null) return grouped.Identity;
            return _legacyTracepoints.FirstOrDefault(point => point.Line == line && _legacyBreakpointPathComparer.Equals(point.File, file))?.Identity;
        }

        private LegacyRequestedLine[] SnapshotLegacyBreakpointLines(string file, LegacyInstrumentationGroup? excluding = null)
        {
            var lines = new List<LegacyRequestedLine>();
            var grouped = _legacyGroups.Values.Where(group => !ReferenceEquals(group, excluding))
                .SelectMany(static group => group.Breakpoints).Where(point => _legacyBreakpointPathComparer.Equals(point.File, file)).ToArray();
            var excludedTraces = excluding?.Tracepoints;
            var traces = _legacyTracepoints.Where(point => _legacyBreakpointPathComparer.Equals(point.File, file)
                && (excludedTraces is null || !excludedTraces.Contains(point))).ToArray();
            foreach (var breakpoint in _legacyBreakpoints)
            {
                if (!_legacyBreakpointPathComparer.Equals(breakpoint.File, file)) continue;
                var unconditional = traces.Any(point => point.Line == breakpoint.Line)
                    || grouped.Any(point => point.Line == breakpoint.Line && string.IsNullOrEmpty(point.Condition) && string.IsNullOrEmpty(point.HitCondition));
                lines.Add(new LegacyRequestedLine(breakpoint.Line, breakpoint.Incarnation,
                    unconditional ? null : breakpoint.Condition, unconditional ? null : breakpoint.HitCondition, breakpoint.Identity));
            }
            foreach (var point in grouped)
            {
                if (lines.Any(line => line.Line == point.Line)) continue;
                var unconditional = traces.Any(trace => trace.Line == point.Line)
                    || grouped.Any(other => other.Line == point.Line && string.IsNullOrEmpty(other.Condition) && string.IsNullOrEmpty(other.HitCondition));
                lines.Add(new LegacyRequestedLine(point.Line, point.Incarnation,
                    unconditional ? null : point.Condition, unconditional ? null : point.HitCondition, point.Identity));
            }
            foreach (var point in traces)
                if (!lines.Any(line => line.Line == point.Line))
                    lines.Add(new LegacyRequestedLine(point.Line, point.Incarnation, Identity: point.Identity));
            return lines.ToArray();
        }

        private int LegacyOwnedBreakpointCount() => _legacyBreakpoints.Count + _legacyTracepoints.Count
            + _legacyFunctionBreakpoints.Count + _legacyGroups.Values.Sum(static group => group.Breakpoints.Count);

        private static bool ValidLegacyGroupName(string name) => name.Length is > 0 and <= 64
            && name.All(static c => c is >= 'A' and <= 'Z' or >= 'a' and <= 'z' or >= '0' and <= '9' or '_' or '-' or '.');

        private bool TryCreateLegacyGroup(string name, Dictionary<string, JsonElement> arguments, out LegacyInstrumentationGroup group)
        {
            group = new LegacyInstrumentationGroup(name);
            var specs = new List<(bool Trace, string File, int Line, string? Condition, string? HitCondition, string? Expression)>();
            foreach (var (key, trace) in new[] { ("breakpoints", false), ("tracepoints", true) })
            {
                if (!arguments.TryGetValue(key, out var items) || items.ValueKind == JsonValueKind.Null) continue;
                if (items.ValueKind != JsonValueKind.Array) return false;
                foreach (var item in items.EnumerateArray())
                {
                    if (item.ValueKind != JsonValueKind.Object || specs.Count >= MaximumBreakpointCount) return false;
                    var fields = new Dictionary<string, JsonElement>(StringComparer.Ordinal);
                    foreach (var field in item.EnumerateObject())
                        if (!fields.TryAdd(field.Name, field.Value)) return false;
                    if (fields.Keys.Any(field => field is not ("file" or "line" or "condition" or "hit_condition" or "expression"))
                        || !fields.TryGetValue("file", out var fileValue) || fileValue.ValueKind != JsonValueKind.String
                        || string.IsNullOrWhiteSpace(fileValue.GetString())
                        || !NetCoreDbgSession.TryNormalizeLegacyBreakpointSource(fileValue.GetString()!, out var file, requireExistingFile: true)
                        || !fields.TryGetValue("line", out var lineValue) || !TryReadOutputLineCount(lineValue, out var line)
                        || line is < 1 or > int.MaxValue
                        || !TryReadOptionalLegacyBreakpointOption(fields, "condition", out var condition)
                        || !TryReadOptionalLegacyBreakpointOption(fields, "hit_condition", out var hitCondition)) return false;
                    string? expression = null;
                    if (trace && (!fields.TryGetValue("expression", out var value) || value.ValueKind != JsonValueKind.String
                        || (expression = value.GetString()) is null || Encoding.UTF8.GetByteCount(expression) > 4096)) return false;
                    if (!trace && fields.ContainsKey("expression")) return false;
                    if (trace && (condition is not null || hitCondition is not null)) return false;
                    if (Encoding.UTF8.GetByteCount(condition ?? string.Empty) + Encoding.UTF8.GetByteCount(hitCondition ?? string.Empty) > 4096) return false;
                    specs.Add((trace, file, (int)line, condition, hitCondition, expression));
                }
            }
            if (specs.Count == 0 || _legacyGroups.Count >= MaximumBreakpointCount
                || specs.Count + _legacyBreakpoints.Count + _legacyTracepoints.Count + _legacyFunctionBreakpoints.Count
                    + _legacyGroups.Values.Sum(static value => value.Breakpoints.Count) > MaximumBreakpointCount) return false;
            foreach (var spec in specs)
            {
                var incarnation = ++_legacyBreakpointIncarnation;
                var identity = LegacyPhysicalIdentity(spec.File, spec.Line)
                    ?? group.Breakpoints.FirstOrDefault(point => point.Line == spec.Line && _legacyBreakpointPathComparer.Equals(point.File, spec.File))?.Identity
                    ?? group.Tracepoints.FirstOrDefault(point => point.Line == spec.Line && _legacyBreakpointPathComparer.Equals(point.File, spec.File))?.Identity
                    ?? incarnation;
                if (spec.Trace)
                    group.Tracepoints.Add(new LegacyTracepoint($"tp-{++_legacyTraceCounter}", spec.File, spec.Line, spec.Expression!, incarnation, identity));
                else
                    group.Breakpoints.Add(new LegacyGroupBreakpoint(spec.File, spec.Line, spec.Condition, spec.HitCondition, incarnation, identity));
            }
            return true;
        }

        private CallToolResult LegacyGroupEnvelope(object data, params string[] actions)
        {
            var state = LegacySnapshot().State;
            var result = Result(new
            {
                state,
                next_actions = LegacyActions(state).Concat(actions).Distinct(StringComparer.Ordinal).ToArray(),
                message = state == "idle" ? "No active debug session." : "Debug session state observed.",
                data
            }, false);
            return result.StructuredContent is { } body && Encoding.UTF8.GetByteCount(body.GetRawText()) <= MaximumSerializedInspectionBytes
                ? result : LegacyError("Instrumentation evidence exceeds 256 KiB.", state);
        }

        private CallToolResult LegacyGroupFailure(string name, string reason) =>
            LegacyGroupEnvelope(new { status = "FAIL", reason, group = name }, GroupCreate);

        private CallToolResult LegacyGroupResponse(LegacyInstrumentationGroup group, bool inspect)
        {
            var breakpoints = group.Breakpoints.Select(point => new
            {
                kind = "breakpoint",
                file = point.File,
                line = point.Line,
                condition = point.Condition,
                hit_condition = point.HitCondition,
                verified = point.Verified,
                id = point.Id,
                dap_line = point.DapLine,
                hit_count = _legacyBreakpointObserverSession?.GetLegacyHitCount(point.File, point.Line) ?? 0,
            }).ToArray();
            var tracepoints = group.Tracepoints.Select(point =>
            {
                var entries = _legacyTraceLog.Where(entry => entry.tracepoint_id == point.Id).ToArray();
                return new
                {
                    kind = "tracepoint",
                    id = point.Id,
                    file = point.File,
                    line = point.Line,
                    expression = point.Expression,
                    breakpoint_id = point.BreakpointId,
                    dap_line = point.DapLine,
                    hit_count = point.HitCount,
                    log_count = entries.Length,
                    logs = entries.Take(20).Select(static entry => new
                    { entry.line, entry.expression, entry.value, entry.tracepoint_id }).ToArray()
                };
            }).ToArray();
            var summary = new
            {
                group = group.Name,
                breakpoint_count = breakpoints.Length,
                tracepoint_count = tracepoints.Length,
                hit_count = breakpoints.Sum(static point => point.hit_count),
                trace_log_count = tracepoints.Sum(static point => point.log_count)
            };
            var applicationConfirmed = group.Breakpoints.Select(static point => point.File)
                .Concat(group.Tracepoints.Select(static point => point.File)).All(IsLegacyGroupMutationSourceConfirmed);
            var hitCountsComplete = group.Breakpoints.Count == 0 || (_legacySessionId is null
                ? !_legacyBreakpointObservationsIncomplete
                : _legacyBreakpointObserverSession is { } observer && observer.LegacyHitCountsComplete && LegacyBreakpointObservationsComplete());
            var complete = applicationConfirmed && (!inspect || hitCountsComplete);
            return LegacyGroupEnvelope(new
            {
                status = complete ? "PASS" : "FAIL",
                reason = !applicationConfirmed ? "instrumentation application is unconfirmed" : !complete ? "breakpoint hit counts are unconfirmed" : inspect ? "instrumentation group inspected" : "instrumentation group created",
                group = group.Name,
                breakpoints,
                tracepoints,
                application = !applicationConfirmed ? "unknown" : _legacySessionId is null ? "deferred" : "acknowledged",
                hit_counts_complete = hitCountsComplete,
                summary,
                evidence_refs = new[] { new { kind = "instrumentation_group", @ref = $"group:{group.Name}", summary = "instrumentation group" } }
            },
                GroupInspect, GroupClear);
        }

        private CallToolResult LegacyGroupCleared(LegacyInstrumentationGroup group) => LegacyGroupEnvelope(new
        {
            status = "PASS",
            reason = "instrumentation group cleared",
            group = group.Name,
            removed = new { breakpoints = group.Breakpoints.Count, tracepoints = group.Tracepoints.Count },
            summary = new { group = group.Name, breakpoint_count = 0, tracepoint_count = 0, hit_count = 0, trace_log_count = 0 },
            evidence_refs = new[] { new { kind = "instrumentation_group", @ref = $"group:{group.Name}", summary = "instrumentation group" } },
        }, GroupCreate);

        private bool LegacyFunctionOptionsFit(int replacementIndex, string name, string? condition, string? hitCondition)
        {
            long bytes = JsonSerializer.SerializeToUtf8Bytes(name).Length + JsonSerializer.SerializeToUtf8Bytes(condition).Length
                + JsonSerializer.SerializeToUtf8Bytes(hitCondition).Length;
            for (var index = 0; index < _legacyFunctionBreakpoints.Count; index++)
            {
                if (index == replacementIndex) continue;
                var point = _legacyFunctionBreakpoints[index];
                bytes += JsonSerializer.SerializeToUtf8Bytes(point.Name).Length + JsonSerializer.SerializeToUtf8Bytes(point.Condition).Length
                    + JsonSerializer.SerializeToUtf8Bytes(point.HitCondition).Length;
                if (bytes > MaximumSerializedInspectionBytes - 4096) return false;
            }
            return bytes <= MaximumSerializedInspectionBytes - 4096;
        }

        private bool LegacyBreakpointOptionsFit(int replacementIndex, string? condition, string? hitCondition)
        {
            long bytes = JsonSerializer.SerializeToUtf8Bytes(condition).Length + JsonSerializer.SerializeToUtf8Bytes(hitCondition).Length;
            for (var index = 0; index < _legacyBreakpoints.Count; index++)
            {
                if (index == replacementIndex) continue;
                var breakpoint = _legacyBreakpoints[index];
                bytes += JsonSerializer.SerializeToUtf8Bytes(breakpoint.Condition).Length + JsonSerializer.SerializeToUtf8Bytes(breakpoint.HitCondition).Length;
                if (bytes > MaximumSerializedInspectionBytes - 4096) return false;
            }
            return bytes <= MaximumSerializedInspectionBytes - 4096;
        }

        private static bool TryReadOptionalLegacyBreakpointOption(Dictionary<string, JsonElement> arguments, string name, out string? value)
        {
            value = null;
            if (!arguments.TryGetValue(name, out var element) || element.ValueKind == JsonValueKind.Null) return true;
            if (element.ValueKind != JsonValueKind.String) return false;
            value = element.GetString();
            return true;
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

        private CallToolResult LegacyTraceCursorOperation(string name, Dictionary<string, JsonElement> arguments)
        {
            if (!TryReadOptionalLegacyBreakpointOption(arguments, "tracepoint_id", out var overrideFilter))
                return LegacyError("Invalid tracepoint ID.");
            var all = _legacyTraceLog.ToArray();
            if (name == MarkTraceCursor)
            {
                var filtered = overrideFilter is null ? all : all.Where(entry => entry.tracepoint_id == overrideFilter).ToArray();
                object cursor = _legacyTraceManagerCreated ? BuildLegacyTraceCursor(filtered, overrideFilter, all)
                    : new { after_timestamp = (double?)null, tracepoint_id = overrideFilter, buffer_start_timestamp = (double?)null, buffer_size = 0 };
                if (JsonSerializer.SerializeToUtf8Bytes(cursor).Length > 16 * 1024) return LegacyError("Trace cursor exceeds 16 KiB.");
                return LegacyBoundedUtilityResponse(new { cursor, buffer_size = filtered.Length, tracepoint_id = overrideFilter }, LegacySnapshot().State);
            }

            if (!arguments.TryGetValue("cursor", out var original)
                || original.ValueKind is not (JsonValueKind.Object or JsonValueKind.Number or JsonValueKind.Null)
                || (original.ValueKind == JsonValueKind.Object && Encoding.UTF8.GetByteCount(original.GetRawText()) > 16 * 1024))
                return LegacyError("Invalid trace cursor.");
            long? limit = null;
            if (arguments.TryGetValue("limit", out var limitValue) && limitValue.ValueKind != JsonValueKind.Null)
            {
                if (!TryReadOutputLineCount(limitValue, out var count)) return LegacyError("Invalid trace limit.");
                limit = count;
            }
            if (!_legacyTraceManagerCreated)
            {
                object next;
                if (original.ValueKind == JsonValueKind.Object)
                {
                    var copied = JsonNode.Parse(original.GetRawText())!.AsObject();
                    if (overrideFilter is not null) copied["tracepoint_id"] = overrideFilter;
                    next = copied;
                }
                else next = new { after_timestamp = original, tracepoint_id = overrideFilter, buffer_start_timestamp = (double?)null, buffer_size = 0 };
                if (JsonSerializer.SerializeToUtf8Bytes(next).Length > 16 * 1024) return LegacyError("Trace cursor exceeds 16 KiB.");
                return LegacyBoundedUtilityResponse(new
                {
                    entries = Array.Empty<LegacyTraceEntry>(),
                    available = 0,
                    total = 0,
                    limit,
                    limited = false,
                    truncated = false,
                    stale = false,
                    dropped_count = 0,
                    cursor = original,
                    next_cursor = next
                }, LegacySnapshot().State);
            }

            double? after = null;
            long ordinal = 0;
            long dropGeneration = 0;
            string? cursorFilter = null;
            var markedEmpty = false;
            var useGlobal = false;
            if (original.ValueKind == JsonValueKind.Number)
            {
                if (!original.TryGetDouble(out var timestamp) || !double.IsFinite(timestamp)) return LegacyError("Invalid trace cursor.");
                after = timestamp;
            }
            else if (original.ValueKind == JsonValueKind.Object)
            {
                if (original.TryGetProperty("tracepoint_id", out var filterValue) && filterValue.ValueKind != JsonValueKind.Null)
                {
                    if (filterValue.ValueKind != JsonValueKind.String) return LegacyError("Invalid trace cursor.");
                    cursorFilter = filterValue.GetString();
                }
                useGlobal = overrideFilter is not null && cursorFilter != overrideFilter;
                var timeKey = useGlobal && original.TryGetProperty("global_after_timestamp", out _) ? "global_after_timestamp" : "after_timestamp";
                var ordinalKey = useGlobal && original.TryGetProperty("global_after_ordinal", out _) ? "global_after_ordinal" : "after_ordinal";
                if (!TryReadTraceTimestamp(original, timeKey, out after)
                    || !TryReadTraceInteger(original, ordinalKey, out ordinal)
                    || !TryReadTraceInteger(original, "drop_generation", out dropGeneration)) return LegacyError("Invalid trace cursor.");
                markedEmpty = (!original.TryGetProperty("after_timestamp", out var originalTime) || originalTime.ValueKind == JsonValueKind.Null)
                    && (!original.TryGetProperty("buffer_start_timestamp", out var start) || start.ValueKind == JsonValueKind.Null)
                    && original.TryGetProperty("buffer_size", out var size) && size.ValueKind == JsonValueKind.Number
                    && size.TryGetInt64(out var sizeValue) && sizeValue == 0;
            }

            var filter = overrideFilter ?? cursorFilter;
            if (useGlobal && after is { } globalTime && ordinal > 0)
            {
                long globalSeen = 0;
                long filteredSeen = 0;
                foreach (var entry in all)
                {
                    if (entry.timestamp != globalTime) continue;
                    globalSeen++;
                    if (entry.tracepoint_id == filter) filteredSeen++;
                    if (globalSeen >= ordinal) break;
                }
                ordinal = filteredSeen;
            }
            var retained = filter is null ? all : all.Where(entry => entry.tracepoint_id == filter).ToArray();
            var oldest = retained.Length == 0 ? (double?)null : retained[0].timestamp;
            bool stale;
            if (after is { } boundary)
            {
                if (oldest is null || oldest > boundary)
                    stale = original.ValueKind == JsonValueKind.Object ? _legacyTraceDropCount > dropGeneration : true;
                else
                    stale = oldest == boundary && ((original.ValueKind == JsonValueKind.Object && _legacyTraceDropCount > dropGeneration)
                        || retained.LongCount(entry => entry.timestamp == boundary) < ordinal);
            }
            else stale = original.ValueKind == JsonValueKind.Object && markedEmpty && _legacyTraceDropCount > dropGeneration;

            var availableEntries = new List<LegacyTraceEntry>(retained.Length);
            long sameTimestamp = 0;
            foreach (var entry in retained)
            {
                if (after is null || entry.timestamp > after || (entry.timestamp == after && ++sameTimestamp > ordinal))
                    availableEntries.Add(entry);
            }
            var available = availableEntries.Count;
            var pageCount = limit is null ? available : (int)Math.Min(available, Math.Max(limit.Value, 0));
            var page = availableEntries.Take(pageCount).ToArray();
            object nextCursor;
            if (limit is <= 0 && available > 0)
            {
                if (after is { } unadvanced)
                    nextCursor = BuildLegacyTraceBoundary(unadvanced, ordinal, filter, retained);
                else if (original.ValueKind == JsonValueKind.Object)
                {
                    if (!TryReadTraceInteger(original, "after_ordinal", out var originalOrdinal)) return LegacyError("Invalid trace cursor.");
                    var copied = JsonNode.Parse(original.GetRawText())!.AsObject();
                    copied["after_ordinal"] = originalOrdinal;
                    copied["tracepoint_id"] = filter;
                    nextCursor = copied;
                }
                else nextCursor = BuildLegacyTraceCursor([], filter);
            }
            else if (page.Length > 0)
            {
                var last = page[^1];
                long boundaryOrdinal = 0;
                foreach (var entry in retained)
                {
                    if (entry.timestamp == last.timestamp) boundaryOrdinal++;
                    if (ReferenceEquals(entry, last)) break;
                }
                nextCursor = BuildLegacyTraceBoundary(last.timestamp, boundaryOrdinal, filter, retained);
            }
            else if (after is { } unchanged) nextCursor = BuildLegacyTraceBoundary(unchanged, ordinal, filter, retained);
            else nextCursor = BuildLegacyTraceCursor(retained, filter);
            if (JsonSerializer.SerializeToUtf8Bytes(nextCursor).Length > 16 * 1024) return LegacyError("Trace cursor exceeds 16 KiB.");


            return LegacyBoundedUtilityResponse(new
            {
                entries = page,
                available,
                total = page.Length,
                limit,
                limited = limit is not null && available > Math.Max(limit.Value, 0),
                truncated = all.Length >= 1000,
                stale,
                dropped_count = stale ? (long?)null : 0,
                cursor = original,
                next_cursor = nextCursor
            }, LegacySnapshot().State);
        }

        private static bool TryReadTraceTimestamp(JsonElement cursor, string key, out double? result)
        {
            result = null;
            if (!cursor.TryGetProperty(key, out var value) || value.ValueKind == JsonValueKind.Null) return true;
            if (value.ValueKind != JsonValueKind.Number || !value.TryGetDouble(out var number) || !double.IsFinite(number)) return false;
            result = number;
            return true;
        }

        private static bool TryReadTraceInteger(JsonElement cursor, string key, out long result)
        {
            result = 0;
            return !cursor.TryGetProperty(key, out var value) || value.ValueKind == JsonValueKind.Null || TryReadOutputLineCount(value, out result);
        }

        private Dictionary<string, object?> BuildLegacyTraceCursor(LegacyTraceEntry[] entries, string? filter, LegacyTraceEntry[]? global = null)
        {
            global ??= entries;
            var last = entries.Length == 0 ? null : entries[^1];
            var globalLast = global.Length == 0 ? null : global[^1];
            return new Dictionary<string, object?>
            {
                ["after_timestamp"] = last?.timestamp,
                ["after_ordinal"] = last is null ? 0 : entries.Count(entry => entry.timestamp == last.timestamp),
                ["global_after_timestamp"] = globalLast?.timestamp,
                ["global_after_ordinal"] = globalLast is null ? 0 : global.Count(entry => entry.timestamp == globalLast.timestamp),
                ["tracepoint_id"] = filter,
                ["buffer_start_timestamp"] = entries.Length == 0 ? null : entries[0].timestamp,
                ["buffer_size"] = entries.Length,
                ["append_generation"] = _legacyTraceAppendCount,
                ["drop_generation"] = _legacyTraceDropCount,
            };
        }

        private Dictionary<string, object?> BuildLegacyTraceBoundary(double timestamp, long ordinal, string? filter, LegacyTraceEntry[] retained) => new()
        {
            ["after_timestamp"] = timestamp,
            ["after_ordinal"] = ordinal,
            ["tracepoint_id"] = filter,
            ["buffer_start_timestamp"] = retained.Length == 0 ? null : retained[0].timestamp,
            ["buffer_size"] = retained.Length,
            ["append_generation"] = _legacyTraceAppendCount,
            ["drop_generation"] = _legacyTraceDropCount,
        };

        private CallToolResult LegacyOutputOperation(string name, Dictionary<string, JsonElement> arguments, DapOutputBuffer? buffer, CancellationToken cancellationToken)
        {
            static bool ReadPatterns(Dictionary<string, JsonElement> args, string key, out string[] patterns)
            {
                patterns = [];
                if (!args.TryGetValue(key, out var value) || value.ValueKind == JsonValueKind.Null) return true;
                if (value.ValueKind != JsonValueKind.Array || value.GetArrayLength() > 256) return false;
                var items = new List<string>();
                foreach (var item in value.EnumerateArray())
                {
                    if (item.ValueKind != JsonValueKind.String) return false;
                    items.Add(item.GetString()!);
                }
                patterns = items.ToArray();
                return true;
            }

            if (name == SearchOutput)
            {
                if (!arguments.TryGetValue("pattern", out var pattern) || pattern.ValueKind != JsonValueKind.String)
                    return LegacyError("Unsupported or invalid argument.");
                long contextLines = 2;
                if (arguments.TryGetValue("context_lines", out var context) && !TryReadOutputLineCount(context, out contextLines))
                    return LegacyError("Unsupported or invalid argument.");
                string? category = null;
                if (arguments.TryGetValue("category", out var categoryValue))
                {
                    if (categoryValue.ValueKind is not (JsonValueKind.Null or JsonValueKind.String)) return LegacyError("Unsupported or invalid argument.");
                    category = categoryValue.GetString();
                }
                var result = DapOutputSearch.Search(buffer?.Snapshot(false, category) ?? string.Empty, pattern.GetString()!, contextLines, cancellationToken);
                if (result.Status == DapOutputSearchStatus.InvalidPattern) return Result(new { success = false, error = "Invalid regex" }, false);
                if (result.Status != DapOutputSearchStatus.Success) return LegacyError(result.Status == DapOutputSearchStatus.Timeout ? "OUTPUT_SEARCH_TIMEOUT" : "OUTPUT_SEARCH_TOO_LARGE");
                using var document = JsonDocument.Parse(result.SerializedContent!);
                var root = document.RootElement;
                var matches = root.GetProperty("matches").EnumerateArray().Select(static item => new
                {
                    line_number = item.GetProperty("lineNumber").GetInt32(),
                    match = item.GetProperty("match").GetString(),
                    context = item.GetProperty("context").EnumerateArray().Select(static line => line.GetString()).ToArray()
                }).ToArray();
                return LegacyBoundedUtilityResponse(new { pattern = pattern.GetString(), match_count = root.GetProperty("matchCount").GetInt32(), matches }, LegacySnapshot().State);
            }
            var key = name == "output_checkpoint" ? "name" : "checkpoint";
            if (!arguments.TryGetValue(key, out var checkpoint) || checkpoint.ValueKind != JsonValueKind.String)
                return LegacyError("Unsupported or invalid argument.");
            if (name == "output_checkpoint") return LegacyEvidenceResponse(_legacyOutputEvidence.Checkpoint(checkpoint.GetString()!, buffer), checkpoint: true);
            if (!ReadPatterns(arguments, "required", out var required) || !ReadPatterns(arguments, "forbidden", out var forbidden))
                return LegacyError("Unsupported or invalid argument.");
            var regex = true;
            if (arguments.TryGetValue("regex", out var regexValue))
            {
                if (regexValue.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return LegacyError("Unsupported or invalid argument.");
                regex = regexValue.GetBoolean();
            }
            long maxMatches = 20;
            if (arguments.TryGetValue("max_matches", out var maxValue) && !TryReadOutputLineCount(maxValue, out maxMatches))
                return LegacyError("Unsupported or invalid argument.");
            if (maxMatches > 256 || maxMatches < int.MinValue) return LegacyError("OUTPUT_ASSERT_TOO_LARGE");
            try
            {
                var data = _legacyOutputEvidence.Assert(checkpoint.GetString()!, required, forbidden, regex, (int)maxMatches, buffer, cancellationToken);
                return LegacyEvidenceResponse(data, checkpoint: false);
            }
            catch (InvalidOperationException ex) { return LegacyError(ex.Message); }
        }

        private Func<JsonObject, CancellationToken, Task<JsonObject>>? CreateLegacySmokeOutputObserver(
            JsonNode? plan, long generation, string? sessionId, string? owner)
        {
            if (!LegacyRuntimeSmokeFileRunner.UsesOutput(plan)) return null;
            return (probe, token) => ObserveLegacySmokeOutputAsync(probe, generation, sessionId, owner, token);
        }

        private async Task<JsonObject> ObserveLegacySmokeOutputAsync(JsonObject probe, long generation,
            string? sessionId, string? owner, CancellationToken cancellationToken)
        {
            var name = probe["name"]?.GetValue<string>();
            var kind = probe["kind"]!.GetValue<string>();
            var observed = new JsonObject
            {
                ["name"] = string.IsNullOrEmpty(name) ? kind : name,
                ["kind"] = kind,
                ["status"] = "FAIL",
            };
            await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                if (_legacySessionGeneration != generation || _legacySessionId != sessionId || _legacyStarting || _legacyStopping || _legacyAttachClosing ||
                    (sessionId is not null && _legacyHasOwner && _legacyOwner != owner))
                {
                    observed["reason"] = "debug session changed before output observation";
                    return observed;
                }
                DapOutputBuffer? buffer = null;
                lock (_lifetimeGate)
                {
                    if (sessionId is not null)
                    {
                        if (!_outputs.TryGetValue(sessionId, out var lifetime))
                        {
                            observed["reason"] = "output is unavailable";
                            return observed;
                        }
                        buffer = lifetime.Buffer;
                    }
                    else if (_startCleanupFailure is not null || _buildCleanupUnconfirmed || _starts != 0 || _ownedSlots.Count != 0)
                    {
                        observed["reason"] = "debug session cleanup is unconfirmed";
                        return observed;
                    }
                }
                var checkpoint = probe["checkpoint"]?.GetValue<string>();
                if (string.IsNullOrEmpty(checkpoint)) checkpoint = "default";
                if (kind == "output.field")
                {
                    JsonObject fieldResult;
                    try
                    {
                        fieldResult = _legacyOutputEvidence.Field(checkpoint,
                            probe["source"]?.GetValue<string>() ?? string.Empty,
                            probe["reason"]?.GetValue<string>() ?? string.Empty,
                            probe["field"]?.GetValue<string>() ?? string.Empty, buffer, cancellationToken);
                    }
                    catch (InvalidOperationException exception)
                    {
                        observed["reason"] = exception.Message;
                        return observed;
                    }
                    foreach (var item in fieldResult) observed[item.Key] = item.Value?.DeepClone();
                    if (fieldResult["status"]?.GetValue<string>() == "PASS" && (probe.ContainsKey("expected") || probe.ContainsKey("expect")))
                    {
                        var expected = probe.ContainsKey("expected") ? probe["expected"] : probe["expect"];
                        observed["expected"] = expected?.DeepClone();
                        var actual = fieldResult["value"]!.GetValue<string>();
                        var matches = expected is JsonValue expectedValue && expectedValue.TryGetValue<string>(out var expectedText) &&
                            (expectedText.StartsWith("contains:", StringComparison.Ordinal)
                                ? actual.Contains(expectedText[9..], StringComparison.Ordinal)
                                : string.Equals(actual, expectedText, StringComparison.Ordinal));
                        if (!matches)
                        {
                            observed["status"] = "FAIL";
                            observed["reason"] = "expected value did not match";
                        }
                    }
                    return observed;
                }
                var required = probe["required"] is JsonArray requiredItems ? requiredItems.Select(item => item!.GetValue<string>()).ToArray() : [];
                var forbidden = probe["forbidden"] is JsonArray forbiddenItems ? forbiddenItems.Select(item => item!.GetValue<string>()).ToArray() : [];
                var maxMatches = probe["max_matches"]?.GetValue<int>() ?? 20;
                JsonObject result;
                try
                {
                    result = JsonSerializer.SerializeToNode(_legacyOutputEvidence.Assert(checkpoint, required, forbidden, false, maxMatches, buffer, cancellationToken))!.AsObject();
                }
                catch (InvalidOperationException exception)
                {
                    observed["reason"] = exception.Message;
                    return observed;
                }
                var status = result["status"]?.GetValue<string>() ?? "FAIL";
                observed["status"] = status;
                observed["value"] = new JsonObject
                {
                    ["matched_line_count"] = (result["matches"] as JsonArray)?.Count ?? 0,
                    ["missing_required"] = result["missing_required"]?.DeepClone() ?? new JsonArray(),
                    ["forbidden_matches"] = result["forbidden_matches"]?.DeepClone() ?? new JsonArray(),
                };
                if (status != "PASS") observed["reason"] = result["reason"]?.DeepClone() ?? JsonValue.Create("output assertion failed");
                if (result["evidence_refs"] is JsonArray { Count: > 0 } refs && refs[0]?["ref"] is JsonValue evidence && evidence.TryGetValue<string>(out var reference))
                    observed["evidence_ref"] = reference;
                return observed;
            }
            finally { _legacyGate.Release(); }
        }

        private Func<JsonObject, CancellationToken, Task<JsonObject>>? CreateLegacySmokeEvaluationObserver(
            long generation, string? sessionId, string? owner, LegacyRuntimeSmokeEffect? effect) => effect is null ? null :
                (probe, token) => ObserveLegacySmokeEvaluationAsync(probe, generation, sessionId, owner, effect, token);

        private async Task<JsonObject> ObserveLegacySmokeEvaluationAsync(JsonObject probe, long generation,
            string? sessionId, string? owner, LegacyRuntimeSmokeEffect effect, CancellationToken cancellationToken)
        {
            var expression = probe["expression"]!.GetValue<string>();
            var name = probe["name"]?.GetValue<string>();
            var observed = new JsonObject
            {
                ["name"] = !string.IsNullOrEmpty(name) ? name : expression.Length > 0 ? expression : "debug.evaluate",
                ["kind"] = "debug.evaluate", ["status"] = "BLOCKED", ["value"] = null,
            };
            await _legacyGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            var gateHeld = true;
            var compositeHeld = false;
            try
            {
                if (_legacySessionGeneration != generation || _legacySessionId != sessionId || sessionId is null ||
                    _legacyStarting || _legacyStopping || _legacyAttachClosing || (_legacyHasOwner && _legacyOwner != owner))
                {
                    observed["reason"] = "No current owned debug session.";
                    return observed;
                }
                if (_legacyInspectionSessionId != sessionId || _legacyInspectionContextId is null || _legacyCurrentFrameId is null ||
                    !StringComparer.Ordinal.Equals(_legacyInspectionMuxId, owner) || !_sessions.TryGetValue(sessionId, out var session))
                {
                    observed["reason"] = "Call get_call_stack for the current stopped context first.";
                    return observed;
                }
                var context = _legacyInspectionContextId;
                var frame = _legacyCurrentFrameId.Value;
                var revision = _legacyInspectionRevision;
                if (session.CaptureInspectionState(context) is null)
                {
                    observed["reason"] = "No current stopped frame.";
                    return observed;
                }
                var request = LegacyRequest(EvaluateExpression, sessionId, new Dictionary<string, JsonElement>
                {
                    ["inspectionContextId"] = JsonSerializer.SerializeToElement(context),
                    ["frameId"] = JsonSerializer.SerializeToElement(frame),
                    ["expression"] = JsonSerializer.SerializeToElement(expression),
                });
                _legacyCompositeEvaluations++;
                compositeHeld = true;
                _legacyGate.Release();
                gateHeld = false;
                var result = await EvaluateExpressionCoreAsync(request, cancellationToken, effect).ConfigureAwait(false);
                await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                gateHeld = true;
                if (_legacySessionGeneration != generation || _legacySessionId != sessionId || _legacyStarting || _legacyStopping || _legacyAttachClosing ||
                    (_legacyHasOwner && _legacyOwner != owner) || _legacyInspectionRevision != revision ||
                    _legacyInspectionSessionId != sessionId || _legacyInspectionContextId != context || _legacyCurrentFrameId != frame ||
                    !StringComparer.Ordinal.Equals(_legacyInspectionMuxId, owner) || !_sessions.TryGetValue(sessionId, out var current) ||
                    !ReferenceEquals(current, session) || session.CaptureInspectionState(context) is null)
                {
                    observed["status"] = "IMPASSE";
                    observed["reason"] = "Evaluation outcome is unknown; it may have executed. Do not retry automatically.";
                    observed["outcome_unknown"] = true;
                    return observed;
                }
                if (result.StructuredContent is not { } content || content.ValueKind != JsonValueKind.Object)
                {
                    observed["status"] = "IMPASSE";
                    observed["reason"] = "Evaluation outcome is unknown; it may have executed. Do not retry automatically.";
                    observed["outcome_unknown"] = true;
                    return observed;
                }
                if (result.IsError == true)
                {
                    var code = content.TryGetProperty("error", out var error) && error.ValueKind == JsonValueKind.String ? error.GetString() : null;
                    if (code == "DAP_EVALUATE_REFUSED") observed["reason"] = "Evaluation was refused before dispatch.";
                    else if (code == "DAP_EVALUATE_FAILED") observed["reason"] = "Evaluation failed and may have executed. Do not retry automatically.";
                    else
                    {
                        observed["status"] = "IMPASSE";
                        observed["reason"] = "Evaluation outcome is unknown; it may have executed. Do not retry automatically.";
                        observed["outcome_unknown"] = true;
                    }
                    return observed;
                }
                if (!content.TryGetProperty("kind", out var kind) || kind.GetString() != "evaluate_success" ||
                    !content.TryGetProperty("result", out var value) || value.ValueKind != JsonValueKind.String)
                {
                    observed["status"] = "IMPASSE";
                    observed["reason"] = "Evaluation outcome is unknown; it may have executed. Do not retry automatically.";
                    observed["outcome_unknown"] = true;
                    return observed;
                }
                observed["status"] = "PASS";
                observed["value"] = value.GetString();
                if (probe.ContainsKey("expected"))
                {
                    observed["expected"] = probe["expected"]?.DeepClone();
                    if (!JsonNode.DeepEquals(observed["value"], probe["expected"]))
                    {
                        observed["status"] = "FAIL";
                        observed["reason"] = "expected value did not match";
                    }
                }
                return observed;
            }
            finally
            {
                if (!gateHeld) await _legacyGate.WaitAsync(CancellationToken.None).ConfigureAwait(false);
                if (compositeHeld)
                {
                    _legacyCompositeEvaluations--;
                    if (_legacySessionId == sessionId && _legacyOwner is not null && _legacyOwner == owner)
                        _legacyOwnerActivity = Stopwatch.GetTimestamp();
                }
                _legacyGate.Release();
            }
        }

        private static bool TryReadLegacyEncArguments(Dictionary<string, JsonElement> arguments,
            out string? file, out List<LegacyEncEdit>? edits)
        {
            file = null;
            edits = null;
            if (!arguments.TryGetValue("file", out var source) || source.ValueKind != JsonValueKind.String
                || string.IsNullOrWhiteSpace(file = source.GetString())
                || !arguments.TryGetValue("edits", out var values) || values.ValueKind != JsonValueKind.Array)
                return false;
            var parsed = new List<LegacyEncEdit>();
            foreach (var value in values.EnumerateArray())
            {
                if (value.ValueKind != JsonValueKind.Object
                    || !value.TryGetProperty("start_line", out var start) || start.ValueKind != JsonValueKind.Number || !start.TryGetInt32(out var first)
                    || !value.TryGetProperty("end_line", out var end) || end.ValueKind != JsonValueKind.Number || !end.TryGetInt32(out var last)
                    || !value.TryGetProperty("new_text", out var text) || text.ValueKind != JsonValueKind.String)
                    return false;
                parsed.Add(new LegacyEncEdit(first, last, text.GetString()!));
            }
            edits = parsed;
            return true;
        }

        private async Task<LegacyEncResult> LegacyApplyCodeChangeAsync(string sessionId, string root,
            string file, IReadOnlyList<LegacyEncEdit> edits, CancellationToken cancellationToken)
        {
            if (!_sessions.TryGetValue(sessionId, out var session) || !_slots.TryGetValue(sessionId, out var slot))
                return new(false, "No active debug session.");
            var lease = slot.TryAcquire();
            if (lease is null) return new(false, "No active debug session.");
            var close = false;
            try
            {
                if (!_isUsable(session)) { close = true; return new(false, "No active debug session."); }
                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var result = await session.ApplyLegacyCodeChangeAsync(root, file, edits, operation.Token).ConfigureAwait(false);
                close = result.RequiresDrain;
                return result;
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                close = session.RequiresInspectionDrain();
                throw;
            }
            catch (Exception)
            {
                close = true;
                return new(false, "Code change outcome is unknown; do not retry automatically.", OutcomeUnknown: true, RequiresDrain: true);
            }
            finally
            {
                lease.Dispose();
                if (close)
                {
                    var cleanup = slot.CloseAndDrainAsync();
                    if (slot.IsAttached)
                    {
                        _ = cleanup.ContinueWith(static task => { _ = task.Exception; }, CancellationToken.None,
                            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
                        await Task.WhenAny(cleanup, Task.Delay(StopTimeout)).ConfigureAwait(false);
                        if (cleanup.IsFaulted) _ = cleanup.Exception;
                    }
                    else { try { await cleanup.ConfigureAwait(false); } catch (Exception) { } }
                }
            }
        }

        private static bool HasInvalidLegacyArguments(string name, Dictionary<string, JsonElement> arguments)
        {
            if (LegacyRuntimeSmokeFacade.IsTool(name)) return !LegacyRuntimeSmokeFacade.ValidArguments(name, arguments);
            foreach (var key in arguments.Keys)
            {
                var invalid = name switch
                {
                    StartDebug => key is not ("program" or "cwd" or "args" or "env" or "launch_profile" or "stop_at_entry" or "pre_build" or "build_project" or "build_configuration" or "stealth_mode"),
                    InspectLaunchCompatibility => key != "program",
                    RuntimeSmokeValidatePlan => key is not ("plan" or "plan_path"),
                    RunRuntimeSmoke => key != "plan",
                    AttachDebug => key != "process_id",
                    CleanupProcesses => key != "force",
                    RestartDebug => key != "rebuild",
                    GetCallStack => key is not ("thread_id" or "levels"),
                    AddBreakpoint => key is not ("file" or "line" or "condition" or "hit_condition"),
                    RemoveBreakpoint => key is not ("file" or "line"),
                    AddFunctionBreakpoint => key is not ("function_name" or "condition" or "hit_condition"),
                    RemoveFunctionBreakpoint => key != "function_name",
                    AddTracepoint => key is not ("file" or "line" or "expression"),
                    RemoveTracepoint => key != "tracepoint_id",
                    GroupCreate => key is not ("name" or "breakpoints" or "tracepoints"),
                    GroupInspect or GroupClear => key != "name",
                    GetTraceLog => key is not ("since" or "tracepoint_id"),
                    ClearTraceLog => true,
                    MarkTraceCursor => key != "tracepoint_id",
                    GetTraceDelta => key is not ("cursor" or "limit" or "tracepoint_id"),
                    ListBreakpoints or ClearBreakpoints => key != "file",
                    HygienePreflight => key is not ("file" or "clear_breakpoints" or "clear_trace_log" or "clear_exception_filters"),
                    GetScopes => key != "frame_id",
                    GetStepInTargets => key != "frame_id",
                    GetVariables => key is not ("variables_reference" or "filter" or "start" or "count"),
                    EvaluateExpression or QuickEvaluate => key is not ("expression" or "frame_id"),
                    ReadMemory => key is not ("memory_reference" or "offset" or "count"),
                    WriteMemory => key is not ("memory_reference" or "data" or "offset" or "allow_partial"),
                    Disassemble => key is not ("memory_reference" or "offset" or "instruction_offset" or "instruction_count" or "resolve_symbols"),
                    GetLocations => key != "location_reference",
                    SetVariable => key is not ("variables_reference" or "name" or "value"),
                    ApplyCodeChange => key is not ("file" or "edits"),
                    ConfigureExceptions => key != "filters",
                    GetExceptionInfo => key != "thread_id",
                    GetStopContext => key is not ("include_variables" or "include_output_tail"),
                    GetExceptionContext => key is not ("max_frames" or "include_variables_for_frames" or "max_inner_exceptions"),
                    "create_snapshot" => key != "name",
                    "list_snapshots" => true,
                    "diff_snapshots" => key is not ("name1" or "name2"),
                    "analyze_collection" => key is not ("variables_reference" or "sample_size"),
                    "summarize_object" => key is not ("variables_reference" or "max_depth" or "max_properties"),
                    StepOver or StepOut or PauseExecution or ContinueExecution => key != "thread_id",
                    StepInto => key is not ("thread_id" or "target_id"),
                    GetOutput => key is not ("clear" or "category"),
                    GetOutputTail => key is not ("lines" or "category"),
                    DebuggeeActivity => key != "window_ms",
                    GetBuildDiagnostics => key != "include_warnings",
                    SearchOutput => key is not ("pattern" or "context_lines" or "category"),
                    "output_checkpoint" => key != "name",
                    "output_assert_since" => key is not ("checkpoint" or "required" or "forbidden" or "regex" or "max_matches"),
                    VerifyDebugFreshness => key is not ("expected_process_id" or "expected_process_name" or "expected_workspace" or "expected_sources" or "expected_modules" or "expected_artifacts" or "require_active_process"),
                    _ => true,
                };
                if (invalid) return true;
            }
            return false;
        }

        private sealed record UtilityResult(object? Data = null, UtilitySnapshot? Snapshot = null, string? Error = null);

        private static bool TryUtilityLimits(out int snapshots, out int variables)
        {
            static bool Read(string key, int fallback, int maximum, out int value)
            {
                var text = Environment.GetEnvironmentVariable(key);
                value = fallback;
                return text is null || (int.TryParse(text, System.Globalization.NumberStyles.Integer,
                    System.Globalization.CultureInfo.InvariantCulture, out value) && value > 0 && value <= maximum);
            }
            var valid = Read("NETCOREDBG_MAX_SNAPSHOTS", 20, 1024, out snapshots);
            return Read("NETCOREDBG_MAX_VARS_PER_SNAPSHOT", 200, 256, out variables) && valid;
        }

        private CallToolResult LegacyBoundedUtilityResponse(object data, string state)
        {
            var result = LegacyResponse(data, state);
            return result.StructuredContent is { } body && Encoding.UTF8.GetByteCount(body.GetRawText()) <= MaximumSerializedInspectionBytes
                ? result : LegacyError("Inspection result exceeds 256 KiB.", state);
        }

        private async Task<(object? Data, string? Error, string? Context, NetCoreDbgSession? Session)> LegacyWriteMemoryAsync(
            string sessionId, Dictionary<string, JsonElement> arguments, CancellationToken cancellationToken)
        {
            if (!arguments.TryGetValue("memory_reference", out var reference) || reference.ValueKind != JsonValueKind.String
                || string.IsNullOrEmpty(reference.GetString()) || Encoding.UTF8.GetByteCount(reference.GetString()!) > MaximumSerializedInspectionBytes)
                return (null, "memory_reference is required and must fit the inspection limit.", null, null);
            if (!arguments.TryGetValue("data", out var encoded) || encoded.ValueKind != JsonValueKind.String
                || encoded.GetString() is not { } data || data.Length > MaximumSerializedInspectionBytes || !IsStrictWriteBase64(data))
                return (null, "data must be valid base64 and fit the inspection limit.", null, null);
            var offset = 0;
            if (arguments.TryGetValue("offset", out var suppliedOffset)
                && (suppliedOffset.ValueKind != JsonValueKind.Number || !suppliedOffset.TryGetInt32(out offset)))
                return (null, "Invalid offset.", null, null);
            var allowPartial = false;
            if (arguments.TryGetValue("allow_partial", out var partial))
            {
                if (partial.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return (null, "Invalid allow_partial.", null, null);
                allowPartial = partial.GetBoolean();
            }
            if (!_sessions.TryGetValue(sessionId, out var session) || !_slots.TryGetValue(sessionId, out var slot))
                return (null, "No active debug session.", null, null);
            var lease = slot.TryAcquire();
            if (lease is null) return (null, "No active debug session.", null, null);
            var close = false;
            try
            {
                if (!_isUsable(session)) { close = true; return (null, "No active debug session.", null, session); }
                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var result = await session.WriteMemoryAsync(new { memoryReference = reference.GetString(), data, offset, allowPartial }, operation.Token).ConfigureAwait(false);
                if (result.Status == "unsupported") return (null, "Adapter does not support writeMemory. This feature requires supportsWriteMemoryRequest: true.", null, session);
                if (result.Status == "unavailable") return (null, "No confirmed running or stopped debug session.", null, session);
                if (result.Status == "unknown") { close = session.RequiresInspectionDrain(); return (null, "Memory write outcome is unknown; it may have executed. Do not retry automatically.", null, session); }
                if (result.Status == "refused") return (null, "Adapter reported writeMemory failure; memory may have changed. Do not retry automatically.", result.Context, session);
                var body = result.Body;
                if (body.ValueKind is not (JsonValueKind.Object or JsonValueKind.Undefined or JsonValueKind.Null))
                    throw new InvalidDataException("Invalid writeMemory response.");
                long bytesWritten = 0, resultOffset = offset;
                if (body.ValueKind == JsonValueKind.Object)
                {
                    if (body.TryGetProperty("bytesWritten", out var bytes) && (bytes.ValueKind != JsonValueKind.Number || !bytes.TryGetInt64(out bytesWritten) || bytesWritten < 0 || bytesWritten > NetCoreDbgSession.MaximumSafeDapInteger))
                        throw new InvalidDataException("Invalid writeMemory bytesWritten.");
                    if (body.TryGetProperty("offset", out var returnedOffset) && (returnedOffset.ValueKind != JsonValueKind.Number || !returnedOffset.TryGetInt64(out resultOffset) || resultOffset < -NetCoreDbgSession.MaximumSafeDapInteger || resultOffset > NetCoreDbgSession.MaximumSafeDapInteger))
                        throw new InvalidDataException("Invalid writeMemory offset.");
                }
                return (new { bytes_written = bytesWritten, offset = resultOffset }, null, result.Context, session);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                close = session.RequiresInspectionDrain();
                throw;
            }
            catch (Exception)
            {
                close = true;
                return (null, "Memory write outcome is unknown; it may have executed. Do not retry automatically.", null, session);
            }
            finally
            {
                lease.Dispose();
                if (close)
                {
                    var cleanup = slot.CloseAndDrainAsync();
                    if (slot.IsAttached)
                    {
                        _ = cleanup.ContinueWith(static task => { _ = task.Exception; }, CancellationToken.None,
                            TaskContinuationOptions.OnlyOnFaulted | TaskContinuationOptions.ExecuteSynchronously, TaskScheduler.Default);
                        await Task.WhenAny(cleanup, Task.Delay(StopTimeout)).ConfigureAwait(false);
                        if (cleanup.IsFaulted) _ = cleanup.Exception;
                    }
                    else
                    {
                        try { await cleanup.ConfigureAwait(false); } catch (Exception) { }
                    }
                }
            }
        }

        private static bool IsStrictWriteBase64(string text)
        {
            var symbols = 0;
            var padding = 0;
            foreach (var character in text)
            {
                if (character == '=') { padding++; continue; }
                if (padding != 0 || !(character is >= 'A' and <= 'Z' or >= 'a' and <= 'z' or >= '0' and <= '9' or '+' or '/')) return false;
                symbols++;
            }
            if (symbols == 0) return padding == 0;
            return (symbols % 4) switch { 0 => padding == 0, 2 => padding == 2, 3 => padding == 1, _ => false };
        }

        private async Task<(object? Data, string? Error, string? Context, NetCoreDbgSession? Session)> LegacyMemoryInspectionAsync(
            string name, string sessionId, Dictionary<string, JsonElement> arguments, CancellationToken cancellationToken)
        {
            static bool Number(Dictionary<string, JsonElement> values, string key, int fallback, out int value)
            {
                value = fallback;
                return !values.TryGetValue(key, out var element) || (element.ValueKind == JsonValueKind.Number && element.TryGetInt32(out value));
            }
            var memoryReference = "";
            if (name != GetLocations && (!arguments.TryGetValue("memory_reference", out var reference) ||
                reference.ValueKind != JsonValueKind.String || string.IsNullOrEmpty(memoryReference = reference.GetString()!) ||
                Encoding.UTF8.GetByteCount(memoryReference) > MaximumSerializedInspectionBytes))
                return (null, "memory_reference is required and must fit the inspection limit.", null, null);
            if (!Number(arguments, "offset", 0, out var offset) || !Number(arguments, "count", 0, out var count) ||
                !Number(arguments, "instruction_offset", 0, out var instructionOffset) ||
                !Number(arguments, "instruction_count", 64, out var instructionCount) ||
                !Number(arguments, "location_reference", 0, out var locationReference) ||
                (name == ReadMemory && count < 0) || (name == Disassemble && instructionCount <= 0) ||
                (name == GetLocations && locationReference <= 0))
                return (null, "Invalid memory inspection argument.", null, null);
            var resolveSymbols = true;
            if (arguments.TryGetValue("resolve_symbols", out var resolve))
            {
                if (resolve.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return (null, "Invalid resolve_symbols.", null, null);
                resolveSymbols = resolve.GetBoolean();
            }
            if (!_sessions.TryGetValue(sessionId, out var session) || !_slots.TryGetValue(sessionId, out var slot))
                return (null, "No active debug session.", null, null);
            var lease = slot.TryAcquire();
            if (lease is null) return (null, "No active debug session.", null, null);
            var closeAfterLease = false;
            try
            {
                if (!_isUsable(session)) { closeAfterLease = true; return (null, "No active debug session.", null, null); }
                var command = name == ReadMemory ? "readMemory" : name == Disassemble ? "disassemble" : "locations";
                var inspectionCommand = name == ReadMemory ? MemoryInspectionCommand.ReadMemory
                    : name == Disassemble ? MemoryInspectionCommand.Disassemble : MemoryInspectionCommand.Locations;
                object dapArguments = name switch
                {
                    ReadMemory => new { memoryReference, offset, count },
                    Disassemble => new { memoryReference, offset, instructionOffset, instructionCount, resolveSymbols },
                    _ => new { locationReference },
                };
                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var result = await session.ReadMemoryInspectionAsync(inspectionCommand, dapArguments, name == ReadMemory && count == 0, operation.Token).ConfigureAwait(false);
                if (result.Status == "unsupported")
                {
                    var capability = name == ReadMemory ? "supportsReadMemoryRequest" : name == Disassemble ? "supportsDisassembleRequest" : "supportsLocationsRequest";
                    return (null, $"Adapter does not support {command}. This feature requires {capability}: true.", null, session);
                }
                if (result.Status == "stale") return (null, "Inspection context changed before the operation completed.", null, session);
                if (result.Status == "refused") return (null, $"Adapter refused {command}.", null, session);
                if (result.Status == "empty") return (new { address = "", unreadable_bytes = 0, data = "" }, null, result.Context, session);
                var body = result.Body;
                object data;
                if (name == ReadMemory)
                {
                    var address = body.TryGetProperty("address", out var a) ? a.GetString() : "";
                    var unreadable = body.TryGetProperty("unreadableBytes", out var u) ? u.GetInt64() : 0;
                    var bytes = body.TryGetProperty("data", out var d) ? d.GetString() : "";
                    if (address is null || bytes is null || unreadable < 0 || unreadable > NetCoreDbgSession.MaximumSafeDapInteger || !IsValidMemoryBase64(bytes)) throw new InvalidDataException("Invalid readMemory response.");
                    data = new { address, unreadable_bytes = unreadable, data = bytes };
                }
                else if (name == Disassemble)
                {
                    var instructions = body.TryGetProperty("instructions", out var i) ? i : JsonSerializer.SerializeToElement(Array.Empty<object>());
                    if (instructions.ValueKind != JsonValueKind.Array || instructions.GetArrayLength() > instructionCount) throw new InvalidDataException("Invalid disassemble response.");
                    foreach (var instruction in instructions.EnumerateArray()) ValidateDisassembledInstruction(instruction);
                    data = new { instructions };
                }
                else
                {
                    JsonElement? Field(string key) => body.TryGetProperty(key, out var value) ? value : null;
                    var source = Field("source") ?? JsonSerializer.SerializeToElement(new { });
                    if (Field("source") is not null) ValidateMemorySource(source, requireLocation: true);
                    ValidateMemoryCoordinates(body);
                    data = new { source, line = Field("line"), column = Field("column"), end_line = Field("endLine"), end_column = Field("endColumn"), endLine = Field("endLine"), endColumn = Field("endColumn") };
                }
                return (data, null, result.Context, session);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                closeAfterLease = session.RequiresInspectionDrain();
                throw;
            }
            catch (Exception)
            {
                closeAfterLease = true;
                return (null, "Memory inspection failed or returned an invalid or oversized response.", null, session);
            }
            finally
            {
                lease.Dispose();
                if (closeAfterLease) { try { await slot.CloseAndDrainAsync().ConfigureAwait(false); } catch (Exception) { } }
            }
        }

        private static readonly string[] MemoryPositionFields = ["line", "column", "endLine", "endColumn"];
        private static readonly string[] MemoryInstructionStringFields = ["instructionBytes", "symbol", "presentationHint"];
        private static readonly string[] MemorySourceStringFields = ["name", "path", "origin", "presentationHint"];

        private static bool IsValidMemoryBase64(string text)
        {
            var symbols = 0;
            var padding = 0;
            foreach (var character in text)
            {
                if (character is ' ' or '\t' or '\r' or '\n') continue;
                symbols++;
                if (character == '=') { if (++padding > 2) return false; continue; }
                if (padding != 0 || !(character is >= 'A' and <= 'Z' or >= 'a' and <= 'z' or >= '0' and <= '9' or '+' or '/'))
                    return false;
            }
            return symbols % 4 == 0 && (padding == 0 || symbols >= 4);
        }

        private static void ValidateMemoryCoordinates(JsonElement value)
        {
            foreach (var field in MemoryPositionFields)
            {
                if (value.TryGetProperty(field, out var position) &&
                    (position.ValueKind != JsonValueKind.Number || !position.TryGetInt64(out var number) || number < 1 || number > NetCoreDbgSession.MaximumSafeDapInteger))
                    throw new InvalidDataException("Invalid DAP source position.");
            }
        }

        private static void ValidateDisassembledInstruction(JsonElement instruction)
        {
            if (instruction.ValueKind != JsonValueKind.Object ||
                !instruction.TryGetProperty("address", out var address) || address.ValueKind != JsonValueKind.String ||
                !instruction.TryGetProperty("instruction", out var text) || text.ValueKind != JsonValueKind.String)
                throw new InvalidDataException("Invalid DAP instruction.");
            foreach (var field in MemoryInstructionStringFields)
                if (instruction.TryGetProperty(field, out var value) && value.ValueKind != JsonValueKind.String)
                    throw new InvalidDataException("Invalid DAP instruction field.");
            if (instruction.TryGetProperty("presentationHint", out var hint) && hint.GetString() is not ("normal" or "invalid"))
                throw new InvalidDataException("Invalid DAP instruction hint.");
            if (instruction.TryGetProperty("location", out var location)) ValidateMemorySource(location, requireLocation: false);
            ValidateMemoryCoordinates(instruction);
        }

        private static void ValidateMemorySource(JsonElement source, bool requireLocation)
        {
            if (source.ValueKind != JsonValueKind.Object) throw new InvalidDataException("Invalid DAP source.");
            foreach (var field in MemorySourceStringFields)
                if (source.TryGetProperty(field, out var value) && value.ValueKind != JsonValueKind.String)
                    throw new InvalidDataException("Invalid DAP source field.");
            if (source.TryGetProperty("presentationHint", out var hint) && hint.GetString() is not ("normal" or "emphasize" or "deemphasize"))
                throw new InvalidDataException("Invalid DAP source hint.");
            var hasReference = false;
            if (source.TryGetProperty("sourceReference", out var reference))
            {
                if (reference.ValueKind != JsonValueKind.Number || !reference.TryGetInt32(out var number) || number < 0)
                    throw new InvalidDataException("Invalid DAP source reference.");
                hasReference = number > 0;
            }
            if (requireLocation && !hasReference && (!source.TryGetProperty("path", out var path) || string.IsNullOrEmpty(path.GetString())))
                throw new InvalidDataException("DAP location source has no path or reference.");
            if (source.TryGetProperty("sources", out var sources))
            {
                if (sources.ValueKind != JsonValueKind.Array) throw new InvalidDataException("Invalid DAP related sources.");
                foreach (var related in sources.EnumerateArray()) ValidateMemorySource(related, requireLocation: false);
            }
            if (source.TryGetProperty("checksums", out var checksums))
            {
                if (checksums.ValueKind != JsonValueKind.Array) throw new InvalidDataException("Invalid DAP source checksums.");
                foreach (var checksum in checksums.EnumerateArray())
                {
                    if (checksum.ValueKind != JsonValueKind.Object || !checksum.TryGetProperty("algorithm", out var algorithm) ||
                        algorithm.ValueKind != JsonValueKind.String || algorithm.GetString() is not ("MD5" or "SHA1" or "SHA256" or "timestamp") ||
                        !checksum.TryGetProperty("checksum", out var content) || content.ValueKind != JsonValueKind.String)
                        throw new InvalidDataException("Invalid DAP source checksum.");
                    foreach (var digit in content.GetString()!)
                        if (digit is not (>= '0' and <= '9' or >= 'a' and <= 'f' or >= 'A' and <= 'F'))
                            throw new InvalidDataException("Invalid DAP source checksum value.");
                }
            }
        }

        private CallToolResult LegacyStoredSnapshots(string name, Dictionary<string, JsonElement> arguments)
        {
            if (name == "list_snapshots")
                return LegacyBoundedUtilityResponse(new
                {
                    snapshots = _utilitySnapshotOrder.Select(key => _utilitySnapshots[key]).Select(static snapshot => new
                    { name = snapshot.Name, timestamp = snapshot.Timestamp, frame = snapshot.Frame, variable_count = snapshot.Variables.Count }).ToArray()
                }, LegacySnapshot().State);
            if (!arguments.TryGetValue("name1", out var first) || first.ValueKind != JsonValueKind.String
                || !arguments.TryGetValue("name2", out var second) || second.ValueKind != JsonValueKind.String)
                return LegacyError("Snapshot names are required.");
            var one = first.GetString()!; var two = second.GetString()!;
            if (!_utilitySnapshots.TryGetValue(one, out var left) || !_utilitySnapshots.TryGetValue(two, out var right))
                return LegacyError("Snapshot not found.");
            var added = new List<object>(); var removed = new List<object>(); var changed = new List<object>(); var unchanged = 0;
            foreach (var key in left.Variables.Keys.Union(right.Variables.Keys).OrderBy(static item => item, StringComparer.Ordinal))
            {
                var hasLeft = left.Variables.TryGetValue(key, out var oldValue);
                var hasRight = right.Variables.TryGetValue(key, out var newValue);
                if (!hasLeft) added.Add(new { name = key, value = newValue.Value, type = newValue.Type });
                else if (!hasRight) removed.Add(new { name = key, value = oldValue.Value, type = oldValue.Type });
                else if (oldValue != newValue) changed.Add(new { name = key, old_value = oldValue.Value, new_value = newValue.Value, type = newValue.Type });
                else unchanged++;
            }
            return LegacyBoundedUtilityResponse(new { snapshot1 = one, snapshot2 = two, added, removed, changed, unchanged_count = unchanged }, LegacySnapshot().State);
        }

        private static bool UtilityNumber(Dictionary<string, JsonElement> arguments, string key, int fallback, int minimum, int maximum, out int value)
        {
            value = fallback;
            if (!arguments.TryGetValue(key, out var input)) return true;
            if (!TryReadOutputLineCount(input, out var number) || number < minimum || number > maximum) return false;
            value = (int)number;
            return true;
        }

        private async Task<UtilityResult> ReadLegacyUtilityAsync(string tool, Dictionary<string, JsonElement> arguments,
            NetCoreDbgSession session, SessionSlot slot, int thread, string context, CancellationToken cancellationToken)
        {
            var lease = slot.TryAcquire();
            if (lease is null) return new(Error: "No active debug session.");
            var close = false;
            try
            {
                if (!_isUsable(session)) { close = true; return new(Error: "No active debug session."); }
                using var operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, lease.AbortToken);
                var token = operation.Token;
                var bytes = 0;
                async Task<IReadOnlyList<DapVariable>> ReadVariables(int reference)
                {
                    var result = await session.GetVariablesAsync(context, reference, null, null, token).ConfigureAwait(false);
                    if (result.IsRefused) throw new ArgumentException("Stopped inspection context is unavailable.");
                    foreach (var variable in result.Variables)
                    {
                        bytes = checked(bytes + Encoding.UTF8.GetByteCount(variable.Name) + Encoding.UTF8.GetByteCount(variable.Value)
                            + (variable.Type is null ? 0 : Encoding.UTF8.GetByteCount(variable.Type)) + 64);
                        if (bytes > MaximumSerializedInspectionBytes) throw new ArgumentException("Inspection work budget exceeded.");
                    }
                    return result.Variables;
                }
                if (tool == "create_snapshot")
                {
                    if (!arguments.TryGetValue("name", out var nameValue) || nameValue.ValueKind != JsonValueKind.String
                        || nameValue.GetString() is not { } name || name.Length > 1024) return new(Error: "Invalid snapshot name.");
                    if (!TryUtilityLimits(out _, out var maxVariables)) return new(Error: "Invalid snapshot limits.");
                    var stack = await session.GetCallStackAsync(thread, 0, 1, true, token).ConfigureAwait(false);
                    if (stack.IsRefused || stack.InspectionContextId != context || stack.Frames.Count == 0)
                        return new(Error: "No current stack frame.");
                    var scopes = await session.GetScopesAsync(context, stack.Frames[0].Id, token).ConfigureAwait(false);
                    if (scopes.IsRefused) return new(Error: "Stopped inspection context is unavailable.");
                    var values = new Dictionary<string, (string Value, string Type)>(StringComparer.Ordinal);
                    foreach (var scope in scopes.Scopes)
                    {
                        if (scope.VariablesReference <= 0) continue;
                        foreach (var variable in await ReadVariables(scope.VariablesReference).ConfigureAwait(false))
                        {
                            if (!values.ContainsKey(variable.Name) && values.Count >= maxVariables)
                                return new(Error: "Snapshot variable limit exceeded.");
                            values[variable.Name] = (variable.Value, variable.Type ?? "");
                        }
                    }
                    var timestamp = Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency;
                    var snapshot = new UtilitySnapshot(name, timestamp, stack.Frames[0].Name, values);
                    return new(new { name, frame = snapshot.Frame, variable_count = values.Count, timestamp }, snapshot);
                }
                if (!arguments.ContainsKey("variables_reference") || !UtilityNumber(arguments, "variables_reference", 0, 1, int.MaxValue, out var reference))
                    return new(Error: "A valid variables_reference is required.");
                if (tool == "analyze_collection")
                {
                    if (!UtilityNumber(arguments, "sample_size", 5, 1, 256, out var sample)) return new(Error: "Invalid sample_size.");
                    var items = await ReadVariables(reference).ConfigureAwait(false);
                    if (items.Count == 0) return new(Error: "No items found. Variable may not be a collection or reference is expired.");
                    object Project(DapVariable item) => new { name = item.Name, value = item.Value, type = item.Type ?? "" };
                    var result = new Dictionary<string, object?>
                    {
                        ["count"] = items.Count,
                        ["element_type"] = items[0].Type ?? "unknown",
                        ["null_count"] = items.Count(static item => item.Value is "null" or "Nothing" or "None" or ""),
                        ["first_items"] = items.Take(sample).Select(Project).ToArray(),
                        ["last_items"] = items.Count > sample ? items.TakeLast(sample).Select(Project).ToArray() : Array.Empty<object>(),
                        ["duplicate_count"] = items.Count - items.Select(static item => item.Value).Distinct(StringComparer.Ordinal).Count()
                    };
                    var numeric = new List<double>();
                    foreach (var item in items)
                        if (double.TryParse(item.Value, System.Globalization.NumberStyles.Float, System.Globalization.CultureInfo.InvariantCulture, out var number))
                        {
                            if (!double.IsFinite(number)) return new(Error: "Non-finite numeric values are unsupported.");
                            numeric.Add(number);
                        }
                    if (numeric.Count > 0)
                    {
                        var sum = numeric.Sum();
                        if (!double.IsFinite(sum)) return new(Error: "Numeric total is out of range.");
                        result["min"] = numeric.Min(); result["max"] = numeric.Max(); result["sum"] = sum; result["average"] = sum / numeric.Count;
                    }
                    return new(result);
                }
                if (!UtilityNumber(arguments, "max_depth", 2, int.MinValue, int.MaxValue, out var depth)
                    || !UtilityNumber(arguments, "max_properties", 50, 1, 256, out var limit)) return new(Error: "Invalid object summary limits.");
                depth = Math.Clamp(depth, 1, 5);
                var properties = new List<object>(); var ancestors = new HashSet<int>(); var requests = 0;
                async Task Walk(int referenceId, string prefix, int level)
                {
                    if (level > depth || properties.Count >= limit) return;
                    if (!ancestors.Add(referenceId)) { properties.Add(new { path = prefix.Length == 0 ? "<root>" : prefix, value = "<circular ref>", type = "" }); return; }
                    if (++requests > 256) throw new ArgumentException("Object traversal limit exceeded.");
                    try
                    {
                        foreach (var variable in await ReadVariables(referenceId).ConfigureAwait(false))
                        {
                            if (properties.Count >= limit) break;
                            var path = prefix.Length == 0 ? variable.Name : prefix + "." + variable.Name;
                            properties.Add(new { path, value = variable.Value, type = variable.Type ?? "" });
                            if (variable.VariablesReference > 0 && level < depth)
                                await Walk(variable.VariablesReference, path, level + 1).ConfigureAwait(false);
                        }
                    }
                    finally { ancestors.Remove(referenceId); }
                }
                await Walk(reference, "", 0).ConfigureAwait(false);
                return new(new { properties, total_properties = properties.Count, truncated = properties.Count >= limit, depth_reached = depth });
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested) { close = session.RequiresInspectionDrain(); throw; }
            catch (ArgumentException exception) { return new(Error: exception.Message); }
            catch (Exception) { close = true; return new(Error: "Inspection failed or context became unavailable."); }
            finally
            {
                lease.Dispose();
                if (close) { try { await slot.CloseAndDrainAsync().ConfigureAwait(false); } catch (Exception) { } }
            }
        }

        private static CallToolRequestParams LegacyRequest(string name, string sessionId, Dictionary<string, JsonElement>? arguments)
        {
            arguments ??= new Dictionary<string, JsonElement>(StringComparer.Ordinal);
            arguments["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId);
            return new CallToolRequestParams { Name = name, Arguments = arguments };
        }

        private (string State, int? ThreadId, string? Reason, int? ExitCode) LegacySnapshot()
        {
            if (_legacyAttachClosing) return ("unknown", null, null, null);
            if (_legacySessionId is null) return (_legacyStarting ? "initializing" : "idle", null, null, null);
            return LegacySnapshot(_legacySessionId);
        }

        private (string State, int? ThreadId, string? Reason, int? ExitCode) LegacySnapshot(string sessionId)
        {
            if (_legacySessionId == sessionId && (_legacyAttachClosing || (_legacyAttachSlot?.IsClosing ?? false)))
                return ("unknown", null, null, null);
            if (_sessions.TryGetValue(sessionId, out var session)) return ProjectLegacyState(session.LegacyState);
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
            "stopped" => ["get_debug_state", "get_threads", "get_call_stack", "step_over", "step_into", "step_out", "continue_execution", "apply_code_change", "get_output", "get_output_tail", "stop_debug"],
            "running" => ["get_debug_state", "debuggee_activity", "pause_execution", "get_output", "get_output_tail", "stop_debug"],
            _ => ["get_debug_state", "get_output", "get_output_tail", "stop_debug"],
        };

        private static CallToolResult LegacyCompositeResult(JsonElement content, bool exception, long? hitCount)
        {
            static object[] Variables(JsonElement locals) => locals.EnumerateArray().Select(static value => (object)new
            {
                name = value.GetProperty("name"),
                value = value.GetProperty("value"),
                type = value.GetProperty("type")
            }).ToArray();
            var frames = content.GetProperty("frames");
            object data;
            if (exception)
            {
                var mappedFrames = new List<Dictionary<string, object?>>();
                var scopes = content.GetProperty("scopeVariables");
                var index = 0;
                foreach (var frame in frames.EnumerateArray())
                {
                    var mapped = new Dictionary<string, object?>
                    {
                        ["index"] = index,
                        ["id"] = frame.GetProperty("id"),
                        ["name"] = frame.GetProperty("name"),
                        ["source"] = frame.GetProperty("source"),
                        ["line"] = frame.GetProperty("line"),
                        ["column"] = frame.GetProperty("column")
                    };
                    if (scopes[index].ValueKind == JsonValueKind.Object)
                    {
                        var variables = new Dictionary<string, object>();
                        foreach (var scope in scopes[index].EnumerateObject())
                            variables[scope.Name] = Variables(scope.Value);
                        mapped["variables"] = variables;
                    }
                    mappedFrames.Add(mapped);
                    index++;
                }
                data = new
                {
                    threadId = content.GetProperty("threadId"),
                    exception = content.GetProperty("exception"),
                    frames = mappedFrames,
                    totalFrames = mappedFrames.Count,
                    innerExceptions = content.GetProperty("innerExceptions")
                };
            }
            else
            {
                var mapped = new Dictionary<string, object?>
                {
                    ["state"] = "stopped",
                    ["reason"] = content.GetProperty("reason"),
                    ["threadId"] = content.GetProperty("threadId"),
                    ["description"] = content.TryGetProperty("description", out var description) ? description : null,
                    ["text"] = content.TryGetProperty("text", out var text) ? text : null,
                    ["frames"] = frames.EnumerateArray().Select(static frame => new
                    {
                        id = frame.GetProperty("id"),
                        name = frame.GetProperty("name"),
                        source = frame.GetProperty("source"),
                        line = frame.GetProperty("line")
                    }).ToArray()
                };
                if (hitCount is { } count) mapped["hitCount"] = count;
                if (content.TryGetProperty("locals", out var locals)) mapped["locals"] = Variables(locals);
                if (content.TryGetProperty("recentOutput", out var output)) mapped["recentOutput"] = output;
                if (content.TryGetProperty("exception", out var info)) mapped["exception"] = info;
                data = mapped;
            }
            var result = LegacyResponse(data, "stopped");
            return result.StructuredContent is { } resultContent && Encoding.UTF8.GetByteCount(resultContent.GetRawText()) <= MaximumSerializedInspectionBytes
                ? result : LegacyError("DEBUG_CONTEXT_TOO_LARGE", "stopped");
        }

        private CallToolResult LegacyEvidenceResponse(object data, bool checkpoint)
        {
            var state = LegacySnapshot().State;
            var invalidName = checkpoint && JsonSerializer.SerializeToElement(data).GetProperty("reason").GetString() == "invalid output checkpoint name";
            var extra = checkpoint ? new[] { invalidName ? "output_checkpoint" : "output_assert_since" }
                : new[] { "output_checkpoint", "instrumentation_group_inspect" };
            var result = Result(new
            {
                state,
                next_actions = LegacyActions(state).Concat(extra).Distinct(StringComparer.Ordinal).ToArray(),
                message = state == "idle" ? "No active debug session." : "Debug session state observed.",
                data
            }, false);
            return result.StructuredContent is { } body && Encoding.UTF8.GetByteCount(body.GetRawText()) <= MaximumSerializedInspectionBytes
                ? result : LegacyError("Output evidence result exceeds 256 KiB.", state);
        }

        private sealed record LegacyProcessEntry(int pid, string role, string? program, string? session_id, double registered_at, bool? alive);
        private sealed record LegacyCleanupSnapshot(LegacyProcessEntry[] Processes, (NetCoreDbgSession Session, int ForcedBefore)[] Owners, bool ObservationsComplete);

        private LegacyCleanupSnapshot CaptureLegacyProcessEntries()
        {
            KeyValuePair<string, (SessionSlot Slot, OutputLifetime Output, NetCoreDbgSession Session)>[] owners;
            lock (_lifetimeGate) owners = _ownedSlots.ToArray();
            var entries = new Dictionary<int, LegacyProcessEntry>();
            var capturedOwners = new (NetCoreDbgSession Session, int ForcedBefore)[owners.Length];
            var complete = true;
            var budget = 0;
            for (var index = 0; index < owners.Length; index++)
            {
                var owner = owners[index];
                var observations = owner.Value.Session.CaptureLegacyProcessObservations(out var ownerComplete);
                complete &= ownerComplete;
                foreach (var process in observations)
                {
                    var bytes = 256 + 6 * Encoding.UTF8.GetByteCount(process.Program ?? string.Empty);
                    if ((!entries.ContainsKey(process.Pid) && entries.Count == 256) || budget + bytes > 192 * 1024)
                    {
                        complete = false;
                        break;
                    }
                    budget += bytes;
                    entries[process.Pid] = new(process.Pid, process.Role, process.Program, owner.Key, process.RegisteredAt, process.Alive);
                }
                capturedOwners[index] = (owner.Value.Session, owner.Value.Session.LegacyForcedRootTerminations);
            }
            return new(entries.Values.ToArray(), capturedOwners, complete);
        }

        private CallToolResult LegacyProcessStatus(LegacyCleanupSnapshot snapshot)
        {
            var processes = snapshot.Processes;
            var data = new
            {
                action = "status",
                processes,
                total = processes.Length,
                alive = processes.Count(static process => process.alive == true),
                alive_unknown = processes.Count(static process => process.alive is null),
                observations_complete = snapshot.ObservationsComplete
            };
            if (snapshot.ObservationsComplete) return LegacyResponse(data);
            const string error = "Process observations are incomplete.";
            var state = LegacySnapshot().State;
            return Result(new { error, state, next_actions = LegacyActions(state), message = $"Error: {error}", data }, false);
        }

        private CallToolResult LegacyCleanupResult(LegacyCleanupSnapshot snapshot, bool success, string? reason = null, string? stateOverride = null)
        {
            int remaining;
            bool unavailable;
            lock (_lifetimeGate)
            {
                remaining = _ownedSlots.Count;
                unavailable = _shutdownRequested || _startCleanupFailure is not null || _buildCleanupUnconfirmed || _buildActive || _starts != 0;
            }
            var complete = success && remaining == 0 && !unavailable;
            var terminated = 0;
            foreach (var owner in snapshot.Owners)
                terminated += Math.Max(0, owner.Session.LegacyForcedRootTerminations - owner.ForcedBefore);
            var error = reason ?? "Owner cleanup incomplete; unfinished owners remain registered or cleanup is unconfirmed.";
            var data = new
            {
                action = "cleanup",
                terminated,
                processes = snapshot.Processes,
                observations_complete = snapshot.ObservationsComplete,
                complete,
                remaining_owners = remaining,
                errors = complete ? Array.Empty<string>() : new[] { error },
                tree_terminated = (int?)null,
                tree_drained = (bool?)null
            };
            var state = stateOverride ?? LegacySnapshot().State;
            return complete
                ? LegacyResponseWithMessage(data, state, "Cleanup complete; the count includes only confirmed forced roots.")
                : Result(new { error, state, next_actions = LegacyActions(state), message = $"Error: {error}", data }, false);
        }

        private CallToolResult LegacyResponse(object data) => LegacyResponse(data, LegacySnapshot().State);

        private static CallToolResult LegacyResponse(object data, string state) =>
            Result(new { state, next_actions = LegacyActions(state), message = state == "idle" ? "No active debug session." : "Debug session state observed.", data }, false);

        private static CallToolResult LegacyResponseWithMessage(object data, string state, string message) =>
            Result(new { state, next_actions = LegacyActions(state), message, data }, false);

        private CallToolResult LegacyError(string error) => LegacyError(error, LegacySnapshot().State);

        private static CallToolResult LegacyError(string error, string state) =>
            Result(new { error, state, next_actions = LegacyActions(state), message = $"Error: {error}" }, false);

        private static CallToolResult LegacyInspectionChanged(string tool) => LegacyError(
            tool is EvaluateExpression or SetVariable or WriteMemory
                ? $"{(tool == WriteMemory ? "Memory write" : tool == SetVariable ? "Variable mutation" : "Evaluation")} outcome is unknown; it may have executed. Do not retry automatically."
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
            CancellationToken cancellationToken, IDictionary<string, JsonElement>? argumentsOverride = null, Action<string>? resourceObserver = null, LegacyRequestedLine[]? breakpointOptions = null,
            LegacyFunctionBreakpoint[]? functionBreakpoints = null, LegacyResolvedLaunch? resolvedLaunch = null, Action<LegacyResolvedLaunch>? captureResolvedLaunch = null,
            Action? adapterStartAttempted = null)
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
            if (resolvedLaunch is not null)
            {
                projectRoot = resolvedLaunch.ProjectRoot;
                program = resolvedLaunch.Program;
                hasProgram = true;
            }
            if (breakpointOptions is not null)
            {
                if (breakpointOptions.Length != breakpoints.Length) return InvalidArguments(StartDebug);
                for (var index = 0; index < breakpoints.Length; index++)
                    breakpoints[index] = breakpoints[index] with { Incarnation = breakpointOptions[index].Incarnation, Identity = breakpointOptions[index].Identity, Condition = breakpointOptions[index].Condition, HitCondition = breakpointOptions[index].HitCondition };
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
                if (resolvedLaunch is not null) profile = resolvedLaunch.Profile;
                else
                {
                    try
                    {
                        profile = await DapLaunchProfile.ResolveAsync(projectRoot, profileName, launchOptions?.Environment, startupCancellation.Token).ConfigureAwait(false);
                    }
                    catch (DapLaunchProfileException)
                    {
                        return Error("start_debug_launch_profile_invalid", "START_DEBUG_LAUNCH_PROFILE_INVALID");
                    }
                }

                if (preBuild.Enabled)
                {
                    if (!DapBuildCoordinator.TryCreatePlan(projectRoot, preBuild.Project, preBuild.Configuration, out var plan))
                        return Error("start_debug_build_project_invalid", "START_DEBUG_BUILD_PROJECT_INVALID");
                    DapBuildResult build;
                    string? buildStage = null;
                    var buildTimer = Stopwatch.StartNew();
                    void CaptureInterruptedBuild(string interruption)
                    {
                        if (argumentsOverride is null || buildStage is null) return;
                        lock (_lifetimeGate)
                            _legacyBuildSnapshot = new LegacyBuildSnapshot(plan!, null, buildStage, interruption, buildTimer.Elapsed.TotalMilliseconds);
                    }
                    try
                    {
                        build = await DapBuildCoordinator.BuildAsync(plan!, startupCancellation.Token, stage => { buildStage = stage; buildTimer.Restart(); }).ConfigureAwait(false);
                        if (argumentsOverride is not null)
                            lock (_lifetimeGate) _legacyBuildSnapshot = new LegacyBuildSnapshot(plan!, build, build.Stage, null, build.DurationMs);
                    }
                    catch (OperationCanceledException)
                    {
                        CaptureInterruptedBuild("cancelled");
                        throw;
                    }
                    catch (Exception exception) when (exception is DapBuildCleanupException or AggregateException)
                    {
                        lock (_lifetimeGate) { _buildCleanupUnconfirmed = true; }
                        if (cancellationToken.IsCancellationRequested) throw;
                        return Error("start_debug_build_cleanup_unconfirmed", "START_DEBUG_BUILD_CLEANUP_UNCONFIRMED");
                    }
                    catch (TimeoutException)
                    {
                        CaptureInterruptedBuild("timeout");
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
                if (argumentsOverride is not null && !preBuild.Enabled)
                {
                    try { program = LegacyLaunchCompatibility.ResolveProgram(Environment.CurrentDirectory, program!, out _); }
                    catch (Exception exception) when (exception is ArgumentException or NotSupportedException or IOException or UnauthorizedAccessException)
                    { return Error("start_debug_program_unavailable", "START_DEBUG_PROGRAM_UNAVAILABLE"); }
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
                adapterStartAttempted?.Invoke();
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
                    startupCancellation.Token, resourceObserver, functionBreakpoints).ConfigureAwait(false);
                binding.AttachSession(session);
                var output = new OutputLifetime(session.OutputBuffer, LegacyCompat ? session : null);
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
                    _ownedSlots.Add(token, (slot, output, session));
                    registeredSlot = slot;
                    registeredToken = token;
                    session.RetirementRequested += retiringSession => OnRetirementRequested(token, retiringSession);
                }

                if (session.IsRetirementRequested || !_isUsable(session))
                {
                    OnRetirementRequested(token, session);
                }
                captureResolvedLaunch?.Invoke(new LegacyResolvedLaunch(projectRoot, program!, profile));

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
                    : exception is DapBreakpointOptionsUnsupportedException && cleanupComplete
                    ? Error("dap_breakpoint_options_unsupported", "DAP_BREAKPOINT_OPTIONS_UNSUPPORTED")
                    : exception is DapBreakpointsException breakpointFailure && cleanupComplete
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

        private async ValueTask<CallToolResult> GetCompositeContextAsync(CallToolRequestParams request, bool exceptionContext, CancellationToken cancellationToken, bool legacy = false, string? expectedContext = null)
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
                if (expectedContext is not null && !StringComparer.Ordinal.Equals(captured.Value.Context, expectedContext))
                    return Error("debug_context_stale", "DEBUG_CONTEXT_STALE");
                var (state, context, description, text) = captured.Value;
                var threadId = state.CurrentThreadId!.Value;
                CallToolResult InspectionRefusal() => session.IsStoppedContextCurrent(threadId, context)
                    ? Error("debug_context_refused", "DEBUG_CONTEXT_REFUSED")
                    : Error("debug_context_stale", "DEBUG_CONTEXT_STALE");
                if (exceptionContext && state.StopReason != "exception")
                    return Error("debug_context_not_exception", "DEBUG_CONTEXT_NOT_EXCEPTION");
                var frameData = new List<object>();
                var inner = new List<object>();
                var scopeVariables = new List<object?>();
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
                if (legacy && exceptionContext) { budget.AddProperty("scopeVariables", scopeVariables); content["scopeVariables"] = scopeVariables; }
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
                            ["locals"] = index < args.VariableFrames && !legacy ? Array.Empty<DapVariable>() : null
                        }
                        : frame;
                    budget.Append(item, frameData.Count == 0);
                    frameData.Add(item);
                    if (legacy && exceptionContext) { budget.Append(null, scopeVariables.Count == 0); scopeVariables.Add(null); }
                }
                for (var index = 0; index < frames.Length; index++)
                {
                    IReadOnlyList<DapVariable>? variables = null;
                    if (index < args.VariableFrames)
                    {
                        var scopes = await session.GetScopesAsync(context, frames[index].Id, operation.Token).ConfigureAwait(false);
                        if (scopes.IsRefused) return InspectionRefusal();
                        if (legacy && exceptionContext)
                        {
                            var groups = new Dictionary<string, object?>(StringComparer.Ordinal);
                            var sizes = new Dictionary<string, int>(StringComparer.Ordinal);
                            budget.Replace(4, groups);
                            scopeVariables[index] = groups;
                            foreach (var groupScope in scopes.Scopes)
                            {
                                if (groupScope.VariablesReference <= 0) continue;
                                var result = await session.GetVariablesAsync(context, groupScope.VariablesReference, 0, 20, operation.Token).ConfigureAwait(false);
                                if (result.IsRefused) return InspectionRefusal();
                                var values = result.Variables.Count > 20 ? result.Variables.Take(20).ToArray() : result.Variables;
                                sizes[groupScope.Name] = sizes.TryGetValue(groupScope.Name, out var previousSize)
                                    ? budget.Replace(previousSize, values)
                                    : budget.AddProperty(groupScope.Name, values, groups.Count == 0);
                                groups[groupScope.Name] = values;
                            }
                            continue;
                        }
                        var scope = scopes.Scopes.FirstOrDefault(static item => item.VariablesReference > 0);
                        if (scope is null) variables = Array.Empty<DapVariable>();
                        else
                        {
                            var result = await session.GetVariablesAsync(context, scope.VariablesReference, 0, legacy && !exceptionContext ? 15 : 20, operation.Token).ConfigureAwait(false);
                            if (result.IsRefused) return InspectionRefusal();
                            variables = legacy && !exceptionContext && result.Variables.Count > 15 ? result.Variables.Take(15).ToArray() : result.Variables;
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

            internal int AddProperty(string name, object? value, bool first = false)
            {
                _bytes = 0;
                _limit = MaximumSerializedInspectionBytes - _total;
                if (!first) Add(1);
                String(name);
                Add(1);
                var valueStart = _bytes;
                Value(value);
                _total += _bytes;
                return _bytes - valueStart;
            }

            internal int Replace(int previousSize, object? value)
            {
                _bytes = 0;
                _limit = MaximumSerializedInspectionBytes - _total + previousSize;
                Value(value);
                _total += _bytes - previousSize;
                return _bytes;
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

        private ValueTask<CallToolResult> EvaluateExpressionAsync(CallToolRequestParams request, CancellationToken cancellationToken) =>
            EvaluateExpressionCoreAsync(request, cancellationToken, null);

        private async ValueTask<CallToolResult> EvaluateExpressionCoreAsync(
            CallToolRequestParams request, CancellationToken cancellationToken, LegacyRuntimeSmokeEffect? effect)
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
                var result = effect is null
                    ? await session.EvaluateExpressionAsync(arguments.InspectionContextId, arguments.FrameId,
                        arguments.Expression, operation.Token).ConfigureAwait(false)
                    : await session.EvaluateExpressionForSmokeAsync(arguments.InspectionContextId, arguments.FrameId,
                        arguments.Expression, effect.MarkEvaluationDispatched, operation.Token).ConfigureAwait(false);
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

        private async Task<DapBreakpointUpdateResult?> UpdateFunctionBreakpointsAsync(string sessionId, LegacyFunctionBreakpoint[] functions, RequestId requestId, CancellationToken cancellationToken)
        {
            if (!_sessions.TryGetValue(sessionId, out var session) || !_slots.TryGetValue(sessionId, out var slot)) return null;
            var lease = slot.TryAcquire();
            if (lease is null) return null;
            var closeAfterLease = false;
            using var caller = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            using var operation = CancellationTokenSource.CreateLinkedTokenSource(caller.Token, lease.AbortToken);
            _stepOverRequests.TryAdd(requestId, caller);
            try
            {
                if (!_isUsable(session)) { closeAfterLease = true; return null; }
                var result = await session.ReplaceFunctionBreakpointsAsync(functions, operation.Token).ConfigureAwait(false);
                closeAfterLease = result.RequiresDrain;
                return result;
            }
            catch (DapBreakpointCanceledException exception) when (caller.IsCancellationRequested)
            {
                closeAfterLease = exception.RequiresDrain;
                throw;
            }
            catch (OperationCanceledException) when (caller.IsCancellationRequested) { throw; }
            catch (Exception) { closeAfterLease = true; return null; }
            finally
            {
                _stepOverRequests.TryRemove(new KeyValuePair<RequestId, CancellationTokenSource>(requestId, caller));
                lease.Dispose();
                if (closeAfterLease)
                {
                    try { await slot.CloseAndDrainAsync().ConfigureAwait(false); }
                    catch (Exception) { }
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
            LegacyRequestedLine[]? replacementLines = null)
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
                    DapBreakpointUpdateOutcome.Unsupported => Error("dap_breakpoint_options_unsupported", "DAP_BREAKPOINT_OPTIONS_UNSUPPORTED"),
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
            Task? close = null;
            lock (_lifetimeGate)
            {
                if (_shutdownRequested)
                {
                    return NotFound();
                }

                _outputs.TryGetValue(sessionId!, out retainedOutput);
                if (retainedOutput is null && _ownedSlots.ContainsKey(sessionId!))
                {
                    return NotFound();
                }

                if (retainedOutput is not null && _ownedSlots.TryGetValue(sessionId!, out var ownership))
                {
                    // The same slot owns each cleanup attempt; a retry never repeats a completed stop.
                    close = ownership.Slot.CloseAndDrainAsync();
                    _ = CompleteRetirementAsync(sessionId!, ownership.Session, ownership.Slot, retainedOutput, close);
                }
            }

            if (retainedOutput is not null)
            {
                try
                {
                    if (close is not null) await close.WaitAsync(cancellationToken).ConfigureAwait(false);
                    var state = await retainedOutput.Cleanup.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
                    lock (_lifetimeGate)
                        if (_outputs.TryGetValue(sessionId!, out var current) && ReferenceEquals(current, retainedOutput))
                            _outputs.Remove(sessionId!);
                    return StopSuccess(state);
                }
                catch (SessionCleanupUnconfirmedException)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    return Error("debug_session_cleanup_unconfirmed", "DEBUG_SESSION_CLEANUP_UNCONFIRMED");
                }
                catch (Exception) when (!cancellationToken.IsCancellationRequested)
                {
                    lock (_lifetimeGate)
                        if (_outputs.TryGetValue(sessionId!, out var current) && ReferenceEquals(current, retainedOutput))
                            _outputs.Remove(sessionId!);
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
            KeyValuePair<string, (SessionSlot Slot, OutputLifetime Output, NetCoreDbgSession Session)>[] owned;
            Task<DapSessionState>[] outputCleanup;
            Task startsDrained;
            TaskCompletionSource<bool> completion;
            CancellationToken shutdownToken;
            lock (_lifetimeGate)
            {
                _shutdownRequested = true;
                RegisterStartShutdownCancellation(cancellationToken);
                if (_shutdownTask is { IsFaulted: true } previous && previous.Exception?.InnerException is SessionCleanupUnconfirmedException)
                    _shutdownTask = null;
                if (_shutdownTask is not null)
                {
                    return new ValueTask(_shutdownTask);
                }

                owned = _ownedSlots.ToArray();
                slots = owned.Select(static ownership => ownership.Value.Slot)
                    .Concat(_slots.Values).Distinct().ToArray();
                outputCleanup = _ownedSlots.Values.Select(static ownership => ownership.Output)
                    .Concat(_outputs.Values).Distinct().Select(static output => output.Cleanup.Task).ToArray();
                _outputs.Clear();
                startsDrained = _starts == 0
                    ? Task.CompletedTask
                    : (_startsDrained ??= new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously)).Task;
                completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
                _shutdownTask = completion.Task;
                shutdownToken = _startShutdownDisposed ? cancellationToken : _startShutdown.Token;
            }

            var closes = slots.ToDictionary(static slot => slot, slot => slot.CloseAndDrainAsync(shutdownToken));
            foreach (var entry in owned)
                _ = CompleteRetirementAsync(entry.Key, entry.Value.Session, entry.Value.Slot, entry.Value.Output, closes[entry.Value.Slot]);
            var smokeCleanup = _legacySmoke.CloseAsync(shutdownToken);
            _ = DisposeCoreAsync(closes.Values, outputCleanup, startsDrained, smokeCleanup, shutdownToken, completion);
            return new ValueTask(completion.Task);
        }

        private async Task DisposeCoreAsync(
            IEnumerable<Task> closes,
            Task<DapSessionState>[] outputCleanup,
            Task startsDrained,
            Task smokeCleanup,
            CancellationToken cancellationToken,
            TaskCompletionSource<bool> completion)
        {
            Exception? failure = null;
            try
            {
                try
                {
                    await Task.WhenAll(closes.Append(startsDrained).Append(smokeCleanup)).ConfigureAwait(false);
                    await Task.WhenAll(outputCleanup).ConfigureAwait(false);
                }
                finally
                {
                    // Starts admitted before shutdown finish their own unpublished cleanup.
                    await startsDrained.ConfigureAwait(false);
                    KeyValuePair<string, NetCoreDbgSession>[] sessions;
                    KeyValuePair<string, NativeSceneSessionBinding>[] bindings;
                    lock (_lifetimeGate)
                    {
                        sessions = _sessions.Where(item => !_ownedSlots.ContainsKey(item.Key)).ToArray();
                        bindings = _nativeSceneBindings.Where(item => !_ownedSlots.ContainsKey(item.Key)).ToArray();
                        foreach (var item in sessions) _sessions.TryRemove(item);
                        foreach (var item in bindings) _nativeSceneBindings.TryRemove(item);
                    }
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
            bool disposeShutdown;
            lock (_lifetimeGate)
            {
                disposeShutdown = !_startShutdownDisposed;
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
                if (disposeShutdown) _startShutdown.Dispose();
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
            OutputLifetime output,
            Task? close = null)
        {
            DapSessionState? state = null;
            Exception? failure = null;
            try
            {
                await (close ?? slot.CloseAndDrainAsync()).ConfigureAwait(false);
                state = session.State;
            }
            catch (SessionCleanupUnconfirmedException)
            {
                // Keep this exact slot and its pending output until a later cleanup attempt succeeds.
                return;
            }
            catch (Exception exception)
            {
                failure = exception;
            }
            lock (_lifetimeGate)
            {
                if (_ownedSlots.TryGetValue(token, out var current) && ReferenceEquals(current.Slot, slot))
                    _ownedSlots.Remove(token);
            }

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

        private sealed class OutputLifetime(DapOutputBuffer buffer, NetCoreDbgSession? legacySession)
        {
            internal DapOutputBuffer Buffer { get; } = buffer;
            internal NetCoreDbgSession? LegacySession { get; } = legacySession;
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

        private static async Task DrainAttachedSessionAsync(NetCoreDbgSession session)
        {
            try { await session.DisconnectAttachOnceAsync(CancellationToken.None).ConfigureAwait(false); }
            catch (Exception) { }
            if (session.AttachMayHaveDispatched)
                await session.WaitForAttachTargetExitAsync().ConfigureAwait(false);
            await session.StopForOwnerAsync(CancellationToken.None).ConfigureAwait(false);
        }

        private sealed class SessionCleanupUnconfirmedException(Exception inner)
            : InvalidOperationException("Debug session cleanup is unconfirmed.", inner)
        { }

        private sealed class SessionSlot
        {
            private readonly object _gate = new();
            private readonly TimeSpan _drainTimeout;
            private readonly Func<CancellationToken, Task> _stop;
            private readonly Func<ValueTask> _dispose;
            private readonly Action _remove;
            private readonly Func<Task>? _attachDrain;
            private readonly CancellationTokenSource _abort = new();
            private CancellationTokenRegistration? _cancellationRegistration;
            private CancellationToken _externalCloseCancellation;
            private Task? _closeTask;
            private Task? _launchStopTask;
            private Task? _attachDrainTask;
            private TaskCompletionSource<bool>? _drained;
            private int _leases;
            private bool _closed;
            private bool _abortDisposed;

            internal SessionSlot(
                TimeSpan drainTimeout,
                Func<CancellationToken, Task> stop,
                Func<ValueTask> dispose,
                Action remove, Func<Task>? attachDrain = null)
            {
                _drainTimeout = drainTimeout;
                _stop = stop;
                _dispose = dispose;
                _remove = remove;
                _attachDrain = attachDrain;
            }

            internal bool IsClosing { get { lock (_gate) return _closed; } }
            internal bool IsAttached => _attachDrain is not null;

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
                    if (_closeTask is { IsFaulted: true } previous
                        && (_attachDrain is not null || previous.Exception?.InnerException is SessionCleanupUnconfirmedException))
                        _closeTask = null;
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
                if (_attachDrain is not null)
                {
                    await drained.ConfigureAwait(false);
                    try
                    {
                        if (_attachDrainTask is null || _attachDrainTask.IsFaulted || _attachDrainTask.IsCanceled)
                            _attachDrainTask = _attachDrain();
                        await _attachDrainTask.ConfigureAwait(false);
                    }
                    catch (Exception exception)
                    {
                        throw new SessionCleanupUnconfirmedException(exception);
                    }
                    await DisposeForCloseAsync().ConfigureAwait(false);
                    _remove();
                    lock (_gate) _abortDisposed = true;
                    _abort.Dispose();
                    return;
                }
                var retryJobDrain = _launchStopTask is { IsFaulted: true } failedStop
                    && failedStop.Exception?.InnerException is NetCoreDbgSession.WindowsJobCleanupUnconfirmedException;
                var retainForRetry = false;
                try
                {
                    if (!retryJobDrain) await (_launchStopTask ??= StopLaunchedOnceAsync(drained)).ConfigureAwait(false);
                }
                catch (NetCoreDbgSession.WindowsJobCleanupUnconfirmedException exception)
                {
                    retainForRetry = true;
                    throw new SessionCleanupUnconfirmedException(exception);
                }
                finally
                {
                    try
                    {
                        if (!retainForRetry) await DisposeForCloseAsync().ConfigureAwait(false);
                    }
                    finally
                    {
                        CancellationTokenRegistration? registration = null;
                        bool disposeAbort;
                        lock (_gate)
                        {
                            disposeAbort = !_abortDisposed;
                            if (disposeAbort)
                            {
                                _abortDisposed = true;
                                registration = _cancellationRegistration;
                                _cancellationRegistration = null;
                            }
                        }

                        try
                        {
                            // A running cancellation callback may release a lease. Do
                            // not wait for it while holding the lease gate.
                            registration?.Dispose();
                        }
                        finally
                        {
                            if (disposeAbort) _abort.Dispose();
                        }
                    }
                }
            }

            private async Task StopLaunchedOnceAsync(Task drained)
            {
                _remove();
                var drainExpired = await Task.WhenAny(drained, Task.Delay(_drainTimeout)).ConfigureAwait(false) != drained;
                if (drainExpired) _abort.Cancel();
                await drained.ConfigureAwait(false);
                await _stop(_abort.IsCancellationRequested ? _abort.Token : CancellationToken.None).ConfigureAwait(false);
                lock (_gate) _externalCloseCancellation.ThrowIfCancellationRequested();
            }

            private async Task DisposeForCloseAsync()
            {
                try { await _dispose().ConfigureAwait(false); }
                catch (Exception exception) { throw new SessionCleanupUnconfirmedException(exception); }
            }

            private void RegisterCancellation(CancellationToken cancellationToken)
            {
                if (_attachDrain is not null) return;
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
                Tool("inspect_debug_launch_compatibility", "Inspect target and active dbgshim versions without launching. Native launch does not automatically select cached shims.", """{"type":"object","properties":{"program":{"type":"string","minLength":1}},"required":["program"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_validate_plan", "Validate an inline native file.json or literal output.since or output.field or debug.evaluate after-probe plan. Other plans and plan_path loading are blocked; Python remains the full implementation.", """{"type":"object","properties":{"plan":{"type":["object","null"]},"plan_path":{"type":["string","null"]}},"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("run_runtime_smoke", "Run an admitted native file.json or literal output.since or output.field or debug.evaluate after-probe plan without UI actions; debug.evaluate can execute code. Other scenarios are blocked.", """{"type":"object","properties":{"plan":{"type":"object"}},"required":["plan"],"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_validate_probe", "Validate a Windows file.json or literal output.since or output.field or debug.evaluate after-probe; other probes and agent_mode are blocked.", """{"type":"object","properties":{"probe":{"type":"object"},"name":{"type":["string","null"]},"phase":{"type":"string","default":"after"},"budgets":{"type":["object","null"]},"debug_preflight":{"type":"boolean","default":false},"tracepoint_guard":{"type":["object","null"]},"agent_mode":{"type":"boolean","default":false}},"required":["probe"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_run_probe", "Start a durable Windows file.json or literal output.since or output.field or debug.evaluate after-probe; other probes and agent_mode are blocked.", """{"type":"object","properties":{"probe":{"type":"object"},"name":{"type":["string","null"]},"phase":{"type":"string","default":"after"},"budgets":{"type":["object","null"]},"debug_preflight":{"type":"boolean","default":false},"tracepoint_guard":{"type":["object","null"]},"agent_mode":{"type":"boolean","default":false}},"required":["probe"],"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_run_plan", "Start an admitted native file.json or literal output.since or output.field or debug.evaluate plan; plan_path and agent_mode are blocked.", """{"type":"object","properties":{"plan":{"type":["object","null"]},"plan_path":{"type":["string","null"]},"agent_mode":{"type":"boolean","default":false}},"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_get_result", "Get a retained native runtime-smoke result owned by this mux client.", """{"type":"object","properties":{"run_id":{"type":"string","minLength":1}},"required":["run_id"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_wait_for_result", "Wait up to 30 seconds for an owned native runtime-smoke result; agent_mode is blocked.", """{"type":"object","properties":{"run_id":{"type":"string","minLength":1},"timeout_ms":{"type":"integer","minimum":0,"maximum":30000,"default":1000},"poll_interval_ms":{"type":"integer","minimum":0,"maximum":30000,"default":100},"after_cursor":{"type":"integer","minimum":0},"event_limit":{"type":"integer","minimum":0,"maximum":128,"default":50},"agent_mode":{"type":"boolean","default":false}},"required":["run_id"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_stop", "Cancel or join an owned native runtime-smoke run; an undrained run stays nonfinal.", """{"type":"object","properties":{"run_id":{"type":"string","minLength":1}},"required":["run_id"],"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_start", "Start an admitted native file.json or literal output.since or output.field or debug.evaluate runtime-smoke plan.", """{"type":"object","properties":{"plan":{"type":"object"}},"required":["plan"],"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_evidence_bundle", "Read bounded lifecycle evidence for an owned native runtime-smoke run; agent_mode is blocked.", """{"type":"object","properties":{"run_id":{"type":"string","minLength":1},"after_cursor":{"type":"integer","minimum":0},"event_limit":{"type":"integer","minimum":0,"maximum":128},"agent_mode":{"type":"boolean","default":false}},"required":["run_id"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_tail_events", "Read bounded lifecycle events for an owned native runtime-smoke run.", """{"type":"object","properties":{"run_id":{"type":"string","minLength":1},"after_cursor":{"type":"integer","minimum":0},"limit":{"type":"integer","minimum":0,"maximum":128}},"required":["run_id"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_mark_event_cursor", "Mark an owned native lifecycle cursor; additional sources and agent_mode are blocked.", """{"type":"object","properties":{"run_id":{"type":"string","minLength":1},"agent_mode":{"type":"boolean","default":false},"include_debug_output":{"type":["boolean","null"]},"include_trace_source":{"type":["boolean","null"]},"include_app_diagnostics":{"type":["boolean","null"]}},"required":["run_id"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_get_event_delta", "Read lifecycle events after an owned cursor; additional sources and agent_mode are blocked.", """{"type":"object","properties":{"cursor":{"type":"object"},"event_limit":{"type":"integer","minimum":0,"maximum":128},"agent_mode":{"type":"boolean","default":false}},"required":["cursor"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, OpenWorldHint = false }),
                Tool("runtime_smoke_cleanup_contract", "Check native runtime-smoke cleanup; unconfirmed cleanup remains blocked.", """{"type":"object","properties":{},"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("attach_debug", "Attach to an existing Windows process. Stop reports unconfirmed CLR detach; do not attach to a process that cannot be lost.", """{"type":"object","properties":{"process_id":{"type":"integer","minimum":1,"maximum":2147483647}},"required":["process_id"],"additionalProperties":false}"""),
                Tool("restart_debug", "Restart with the saved launch configuration; rebuild defaults to true and requires build_project.", """{"type":"object","properties":{"rebuild":{"type":"boolean","default":true}},"additionalProperties":false}"""),
                Tool("terminate_debug", "Terminate the owned debuggee or disconnect if terminate is unsupported.", """{"type":"object","properties":{},"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true }),
                Tool("apply_code_change", "Apply one same-line Edit-and-Continue delta per module and debuggee session. Windows and an EnC-enabled debugger are required; unconfirmed outcomes are not retried.", """{"type":"object","properties":{"file":{"type":"string","minLength":1},"edits":{"type":"array","items":{"type":"object","properties":{"start_line":{"type":"integer"},"end_line":{"type":"integer"},"new_text":{"type":"string"}},"required":["start_line","end_line","new_text"]}}},"required":["file","edits"],"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("cleanup_processes", "View process observations or join captured cleanup owners. PIDs do not authorize termination; attached detach remains unconfirmed.", """{"type":"object","properties":{"force":{"type":"boolean","default":false}},"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("get_debug_state", "Read observed implicit session state; unavailable fields are omitted.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("debuggee_activity", "Observe DAP events and trace entries during a bounded running-session window.", """{"type":"object","properties":{"window_ms":{"type":"integer","minimum":1,"maximum":30000,"default":1000}},"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("verify_debug_freshness", "Compare recorded DAP process metadata, observed inventories, workspace and artifact paths; a recorded PID does not prove liveness or process identity.", """{"type":"object","properties":{"expected_process_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"expected_process_name":{"type":["string","null"]},"expected_workspace":{"type":["string","null"]},"expected_sources":{"type":["array","null"],"items":{"type":"string"}},"expected_modules":{"type":["array","null"],"items":{"type":"string"}},"expected_artifacts":{"type":["array","null"],"items":{"type":"string"}},"require_active_process":{"type":"boolean","default":false}},"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, OpenWorldHint = false }),
                Tool("get_threads", "Get threads from the implicit session.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("get_call_stack", "Read up to 256 frames from a stopped thread.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"]},"levels":{"type":"integer","minimum":1,"maximum":256}},"additionalProperties":false}"""),
                Tool("add_breakpoint", "Set a source breakpoint; requested options require advertised adapter support and are not proof of enforcement.", """{"type":"object","properties":{"file":{"type":"string","minLength":1},"line":{"type":"integer","minimum":1,"maximum":2147483647},"condition":{"type":["string","null"]},"hit_condition":{"type":["string","null"]}},"required":["file","line"],"additionalProperties":false}"""),
                Tool("remove_breakpoint", "Remove a requested source breakpoint.", """{"type":"object","properties":{"file":{"type":"string","minLength":1},"line":{"type":"integer","minimum":1,"maximum":2147483647}},"required":["file","line"],"additionalProperties":false}"""),
                Tool("add_function_breakpoint", "Set a function breakpoint; adapter support for function breakpoints is required while active. Conditions may be ignored by the adapter.", """{"type":"object","properties":{"function_name":{"type":"string"},"condition":{"type":["string","null"]},"hit_condition":{"type":["string","null"]}},"required":["function_name"],"additionalProperties":false}"""),
                Tool("remove_function_breakpoint", "Remove a requested function breakpoint by exact name.", """{"type":"object","properties":{"function_name":{"type":"string"}},"required":["function_name"],"additionalProperties":false}"""),
                Tool("list_breakpoints", "List requested source breakpoints and observed verification and hit counts; global listing also includes function breakpoints.", """{"type":"object","properties":{"file":{"type":["string","null"]}},"additionalProperties":false}"""),
                Tool("add_tracepoint", "Add an owner-guarded source tracepoint; ambiguous hits remain stopped and uncertain evaluations are not retried.", """{"type":"object","properties":{"file":{"type":"string","minLength":1},"line":{"type":"integer","minimum":1,"maximum":2147483647},"expression":{"type":"string"}},"required":["file","line","expression"],"additionalProperties":false}"""),
                Tool("remove_tracepoint", "Remove a tracepoint while preserving a coincident user breakpoint.", """{"type":"object","properties":{"tracepoint_id":{"type":"string"}},"required":["tracepoint_id"],"additionalProperties":false}"""),
                Tool("instrumentation_group_create", "Create a named group of source breakpoints and tracepoints.", """{"type":"object","properties":{"name":{"type":"string"},"breakpoints":{"type":["array","null"],"items":{"type":"object"}},"tracepoints":{"type":["array","null"],"items":{"type":"object"}}},"required":["name"],"additionalProperties":false}"""),
                Tool("instrumentation_group_inspect", "Inspect a named group's breakpoint hits and trace logs.", """{"type":"object","properties":{"name":{"type":"string"}},"required":["name"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, OpenWorldHint = false }),
                Tool("instrumentation_group_clear", "Remove only the named group's instrumentation.", """{"type":"object","properties":{"name":{"type":"string"}},"required":["name"],"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("get_trace_log", "Read bounded process-local trace entries; entries are retained across stop.", """{"type":"object","properties":{"since":{"type":["number","null"]},"tracepoint_id":{"type":["string","null"]}},"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("mark_trace_cursor", "Mark a process-local trace-log boundary, optionally filtered by tracepoint.", """{"type":"object","properties":{"tracepoint_id":{"type":["string","null"]}},"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("get_trace_delta", "Read entries after a trace cursor or monotonic timestamp; stale marks report lost retention.", """{"type":"object","properties":{"cursor":{"type":["object","number","null"]},"limit":{"type":["integer","null"]},"tracepoint_id":{"type":["string","null"]}},"required":["cursor"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, OpenWorldHint = false }),
                Tool("clear_trace_log", "Clear retained trace entries with owner permission.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("clear_breakpoints", "Clear source breakpoints for one file, or all source and function breakpoints. Uncertain writes are not retried.", """{"type":"object","properties":{"file":{"type":["string","null"]}},"additionalProperties":false}"""),
                Tool("debug_hygiene_preflight", "Clear scoped direct instrumentation and report remaining points; group owners are preserved and unconfirmed writes are not retried.", """{"type":"object","properties":{"file":{"type":["string","null"]},"clear_breakpoints":{"type":"boolean","default":true},"clear_trace_log":{"type":"boolean","default":true},"clear_exception_filters":{"type":"boolean","default":false}},"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("get_scopes", "Get scopes for the current stopped stack frame, or an explicit frame ID. Select a fresh stack after execution.", """{"type":"object","properties":{"frame_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("get_variables", "Get variables for a reference in the current stopped context. Filtering is unsupported; paging is nonnegative.", """{"type":"object","properties":{"variables_reference":{"type":"integer","minimum":1,"maximum":2147483647},"filter":{"type":"null"},"start":{"type":["integer","null"],"minimum":0,"maximum":2147483647},"count":{"type":["integer","null"],"minimum":0,"maximum":2147483647}},"required":["variables_reference"],"additionalProperties":false}"""),
                Tool("evaluate_expression", "Evaluate in the current stopped context. Expressions may mutate the debuggee; mux ownership is required and uncertain writes are not retried.", """{"type":"object","properties":{"expression":{"type":"string"},"frame_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"required":["expression"],"additionalProperties":false}"""),
                Tool("quick_evaluate", "Pause a running owned session, evaluate once and resume only the same observed pause stop. Uncertain actions are not retried.", """{"type":"object","properties":{"expression":{"type":"string"},"frame_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"required":["expression"],"additionalProperties":false}"""),
                Tool("set_variable", "Set a variable in the selected stopped context; select a fresh stack afterwards. Uncertain writes must not be retried automatically.", """{"type":"object","properties":{"variables_reference":{"type":"integer","minimum":1,"maximum":2147483647},"name":{"type":"string"},"value":{"type":"string"}},"required":["variables_reference","name","value"],"additionalProperties":false}"""),
                Tool("configure_exceptions", "Replace exception filters with owner permission. Omitted or null filters select an empty list.", """{"type":"object","properties":{"filters":{"type":["array","null"],"items":{"type":"string"}}},"additionalProperties":false}"""),
                Tool("get_exception_info", "Read exception information for the observed stopped context and optional thread.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("get_stop_context", "Read the implicit stopped context with bounded frames, first-scope locals, observed breakpoint hit counts and recent output. Pending or unavailable hit counts are refused.", """{"type":"object","properties":{"include_variables":{"type":"boolean"},"include_output_tail":{"type":"integer","minimum":0,"maximum":100}},"additionalProperties":false}"""),
                Tool("get_exception_context", "Read the implicit exception context. Inner exception evaluation defaults to depth 5, may execute code and requires mux ownership; uncertain outcomes are not retried. Available scopes are included for selected frames within native bounds.", """{"type":"object","properties":{"max_frames":{"type":"integer","minimum":1,"maximum":20},"include_variables_for_frames":{"type":"integer","minimum":0,"maximum":5},"max_inner_exceptions":{"type":"integer","minimum":0,"maximum":5}},"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = false, IdempotentHint = false, DestructiveHint = true }),
                Tool("create_snapshot", "Capture all current-frame scope variables in a process-local snapshot; requires mux ownership. Oversized snapshots are refused.", """{"type":"object","properties":{"name":{"type":"string","maxLength":1024}},"required":["name"],"additionalProperties":false}"""),
                Tool("list_snapshots", "List process-local snapshots in creation order.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("diff_snapshots", "Compare named process-local snapshots in sorted name order.", """{"type":"object","properties":{"name1":{"type":"string"},"name2":{"type":"string"}},"required":["name1","name2"],"additionalProperties":false}"""),
                Tool("analyze_collection", "Analyze bounded variables in the selected stopped context; mux ownership is required.", """{"type":"object","properties":{"variables_reference":{"type":"integer","minimum":1,"maximum":2147483647},"sample_size":{"type":"integer","minimum":1,"maximum":256}},"required":["variables_reference"],"additionalProperties":false}"""),
                Tool("summarize_object", "Summarize a bounded object graph in the selected stopped context; mux ownership is required.", """{"type":"object","properties":{"variables_reference":{"type":"integer","minimum":1,"maximum":2147483647},"max_depth":{"type":"integer"},"max_properties":{"type":"integer","minimum":1,"maximum":256}},"required":["variables_reference"],"additionalProperties":false}"""),
                Tool("get_modules", "Read the bounded module event inventory for the implicit session.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("get_progress", "Read the bounded progress event inventory for the implicit session.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("get_loaded_sources", "Query and refresh loaded sources when the debugger supports it.", """{"type":"object","properties":{},"additionalProperties":false}"""),
                Tool("read_memory", "Read memory when the adapter advertises supportsReadMemoryRequest; count defaults to zero.", """{"type":"object","properties":{"memory_reference":{"type":"string","minLength":1},"offset":{"type":"integer","minimum":-2147483648,"maximum":2147483647,"default":0},"count":{"type":"integer","minimum":0,"maximum":2147483647,"default":0}},"required":["memory_reference"],"additionalProperties":false}"""),
                Tool("write_memory", "Write base64 bytes to debuggee memory with owner permission when supportsWriteMemoryRequest is advertised. Uncertain writes must not be retried automatically.", """{"type":"object","properties":{"memory_reference":{"type":"string","minLength":1},"data":{"type":"string"},"offset":{"type":"integer","minimum":-2147483648,"maximum":2147483647,"default":0},"allow_partial":{"type":"boolean","default":false}},"required":["memory_reference","data"],"additionalProperties":false}""", new ToolAnnotations { DestructiveHint = true, OpenWorldHint = false }),
                Tool("disassemble", "Disassemble memory when the adapter advertises supportsDisassembleRequest.", """{"type":"object","properties":{"memory_reference":{"type":"string","minLength":1},"offset":{"type":"integer","minimum":-2147483648,"maximum":2147483647,"default":0},"instruction_offset":{"type":"integer","minimum":-2147483648,"maximum":2147483647,"default":0},"instruction_count":{"type":"integer","minimum":1,"maximum":2147483647,"default":64},"resolve_symbols":{"type":"boolean","default":true}},"required":["memory_reference"],"additionalProperties":false}"""),
                Tool("get_locations", "Resolve a location reference when the adapter advertises supportsLocationsRequest.", """{"type":"object","properties":{"location_reference":{"type":"integer","minimum":1,"maximum":2147483647}},"required":["location_reference"],"additionalProperties":false}"""),
                Tool("get_step_in_targets", "Query targets for the selected stopped frame; mux ownership is required.", """{"type":"object","properties":{"frame_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("step_over", "Step one stopped thread; uncertain outcomes are not retried.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"]}},"additionalProperties":false}"""),
                Tool("step_into", "Step into using a stopped thread; target_id requires the selected stopped context. Uncertain outcomes are not retried.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647},"target_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("step_out", "Step out using a stopped thread; uncertain outcomes are not retried.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("pause_execution", "Request pause; acknowledgement does not itself prove a stopped event.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"],"minimum":-2147483648,"maximum":2147483647}},"additionalProperties":false}"""),
                Tool("get_output", "Read captured output. Clearing the whole buffer requires mux ownership; filtered reads do not clear it.", """{"type":"object","properties":{"clear":{"type":"boolean"},"category":{"type":["string","null"]}},"additionalProperties":false}"""),
                Tool("get_output_tail", "Read the tail of captured output.", """{"type":"object","properties":{"lines":{"type":"integer","minimum":-9223372036854775808,"maximum":9223372036854775807},"category":{"type":["string","null"]}},"additionalProperties":false}"""),
                Tool("get_build_diagnostics", "Read the latest legacy pre-build result. Output retention and interrupted attempts can make diagnostic counts unavailable.", """{"type":"object","properties":{"include_warnings":{"type":"boolean","default":true}},"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true, DestructiveHint = false }),
                Tool("search_output", "Search shared retained output with case-insensitive culture-invariant .NET regex; not Python regex parity. Counts all lines and returns at most 50 matches; bounded failures return errors.", """{"type":"object","properties":{"pattern":{"type":"string"},"context_lines":{"type":"integer","default":2},"category":{"type":["string","null"]}},"required":["pattern"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true, IdempotentHint = true }),
                Tool("output_checkpoint", "Create an owner-guarded output checkpoint, including before start; successful stop clears it.", """{"type":"object","properties":{"name":{"type":"string"}},"required":["name"],"additionalProperties":false}""", new ToolAnnotations { IdempotentHint = true }),
                Tool("output_assert_since", "Assert required and forbidden patterns since an owner-guarded checkpoint. Regex uses case-sensitive .NET semantics. Discarded output fails; bounded work returns errors.", """{"type":"object","properties":{"checkpoint":{"type":"string"},"required":{"type":["array","null"],"items":{"type":"string"}},"forbidden":{"type":["array","null"],"items":{"type":"string"}},"regex":{"type":"boolean","default":true},"max_matches":{"type":"integer","default":20}},"required":["checkpoint"],"additionalProperties":false}""", new ToolAnnotations { ReadOnlyHint = true }),
                Tool("continue_execution", "Continue one stopped thread and observe the next stop, terminal event, or timeout; uncertain writes are not retried.", """{"type":"object","properties":{"thread_id":{"type":["integer","null"]}},"additionalProperties":false}"""),
                NativeCodeSearchCatalog.Definition("find_code_symbol"),
                NativeCodeSearchCatalog.Definition("find_code_references"),
                NativeCodeSearchCatalog.Definition("get_source_context"),
                NativeCodeSearchCatalog.Definition("search_source"),
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
