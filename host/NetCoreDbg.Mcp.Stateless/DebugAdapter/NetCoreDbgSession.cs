using System.Collections.Concurrent;
using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Unicode;
using System.Text.Json;
using System.Text.Json.Serialization;
using Microsoft.Win32.SafeHandles;
using NetCoreDbg.Mcp.Stateless.NativeScene;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed class NetCoreDbgSession : IAsyncDisposable
{
    private const int MaximumHeaderBytes = 16 * 1024;
    private const int MaximumPayloadBytes = 16 * 1024 * 1024;
    private const int MaximumThreadCount = 256;
    private const int MaximumThreadNameBytes = 1024;
    private const int MaximumStackFrameCount = 256;
    private const int MaximumStackFrameNameBytes = 1024;
    private const int MaximumStackFramePathBytes = 4096;
    private const int MaximumInspectionEntryCount = 256;
    private const int MaximumInspectionNameBytes = 1024;
    private const long MaximumSafeDapInteger = 9_007_199_254_740_991L;
    private const int MaximumContinueReasonBytes = 1024;
    private const int MaximumBreakpointCount = 256;
    private static readonly TimeSpan ContinueTimeout = TimeSpan.FromSeconds(30);
    private static readonly Encoding HeaderEncoding = Encoding.ASCII;
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        Encoder = JavaScriptEncoder.Create(UnicodeRanges.All),
    };

    private readonly Process _process;
    private readonly Stream _input;
    private readonly Stream _output;
    private readonly Stream _error;
    private readonly IProcessTreeOwnership? _processTreeOwnership;
    private readonly TimeSpan _requestTimeout;
    private readonly TimeSpan _stopTimeout;
    private readonly SemaphoreSlim _writeGate = new(1, 1);
    private readonly SemaphoreSlim _callStackAdmissionGate = new(1, 1);
    private readonly SemaphoreSlim _breakpointUpdateGate = new(1, 1);
    private readonly Dictionary<string, DapRequestedBreakpoint[]> _requestedBreakpoints = new(
        OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
    private readonly Dictionary<string, List<LegacyBreakpointObservation>> _legacyBreakpointObservations = new(
        OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
    private readonly Dictionary<long, LegacyBreakpointObservation> _legacyRemovedBreakpoints = [];
    private readonly Dictionary<int, long> _legacyBreakpointIds = [];
    private bool _legacyBreakpointEventsIncomplete;
    private readonly Dictionary<string, Dictionary<int, long>> _legacyHitCounts = new(
        OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
    private int _legacyHitCountLocations;
    private int _legacyHitObservationActive;
    private bool _legacyHitCountsIncomplete;
    private readonly ConcurrentDictionary<int, PendingRequest> _pending = new();
    private readonly TaskCompletionSource<bool> _initialized = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly object _stateGate = new();
    private readonly Dictionary<string, (DapLoadedSource Source, int Bytes)> _loadedSources = new(StringComparer.Ordinal);
    private int _loadedSourceBytes;
    private bool _loadedSourcesIncomplete;
    private const int MaximumLoadedSourceStateBytes = 256 * 1024 - 4096;
    private readonly DapOutputBuffer _outputBuffer = new();
    private readonly object _runtimeInventoryGate = new();
    private readonly Dictionary<RuntimeModuleId, LinkedListNode<RuntimeModuleEntry>> _modules = [];
    private readonly LinkedList<RuntimeModuleEntry> _moduleOrder = [];
    private readonly Dictionary<string, LinkedListNode<RuntimeProgressEntry>> _progress = new(StringComparer.Ordinal);
    private readonly LinkedList<RuntimeProgressEntry> _progressOrder = [];
    private long _moduleBytes;
    private long _progressBytes;
    private bool _modulesIncomplete;
    private bool _progressIncomplete;
    private static readonly int ModuleEntrySyntaxBytes = "{\"id\":,\"name\":,\"path\":,\"version\":,\"isOptimized\":,\"symbolStatus\":}".Length;
    private static readonly int ProgressEntrySyntaxBytes = "{\"progressId\":,\"title\":,\"message\":,\"percentage\":,\"cancellable\":,\"startedAt\":}".Length;
    private static readonly int ModulesSnapshotSyntaxBytes = "{\"kind\":\"modules_success\",\"modules\":[],\"count\":}".Length;
    private static readonly int ProgressSnapshotSyntaxBytes = "{\"kind\":\"progress_success\",\"progress\":[],\"count\":}".Length;
    private readonly object _cleanupGate = new();
    private readonly Task _readerTask;
    private readonly CancellationTokenSource _forceCleanup = new();
    private readonly Task _stderrTask;
    private readonly byte[] _headerByte = new byte[1];

    private DapSessionState _state = new(null, null, null);
    private string? _stopDescription;
    private string? _stopText;
    private DapThread[]? _legacyThreads;
    private int? _legacyProcessId;
    private string? _legacyProcessName;
    private double? _legacyLastStopAt;
    private double? _legacyLastResumeAt;
    private JsonElement? _legacyInvalidation;
    private JsonElement? _legacyMemoryEvent;
    private bool _legacyEventDetailsIncomplete;
    private JsonElement? _legacyExceptionInfo;
    private Task? _cleanupTask;
    private Exception? _readerFailure;
    private int _retirementRequested;
    private NativeSceneTargetIdentity? _nativeSceneTargetIdentity;
    private readonly object _nativeSceneTargetIdentityGate = new();
    private int _nativeSceneCaptureAuthorityInvalidated;
    private int _nativeUiInvokeAuthorityInvalidated;
    private int _outgoingSequence;
    private bool _supportsConfigurationDone;
    private bool _supportsTerminate;
    private DapStackTraceCapabilities _stackTraceCapabilities;
    private bool _supportsExceptionInfoRequest;
    private bool _supportsStepInTargetsRequest;
    private bool _supportsLoadedSourcesRequest;
    private bool _supportsSetVariable;
    private bool _supportsExceptionFilters;
    private CallStackEligibility? _allThreadsStoppedEligibility;
    private Dictionary<int, CallStackEligibility>? _threadEligibility;
    private HashSet<int>? _invalidatedAllThreadTargets;
    private string? _inspectionContextId;
    private bool _inspectionClosed;
    private int _inspectionWriteDrainRequired;
    private int _loadedSourcesWriteDrainRequired;
    private StepOverRecord? _activeStepOver;
    private int _stepOverDrainRequired;
    private ContinueRecord? _activeContinue;
    private PauseRecord? _activePause;
    private ExecutionAdmission _executionAdmission;
    private BreakpointUpdate? _activeBreakpointUpdate;
    private int _requestedBreakpointCount;
    private bool _breakpointUpdatesClosed;
    private bool _initializeResponseObserved;
    private bool CapabilitiesObserved { get; set; }
    private readonly TaskCompletionSource<bool>? _initializeResponseContinuationReached;
    private readonly Task? _initializeResponseContinuationRelease;
    private readonly Func<CancellationToken, Task>? _preHeaderWriteGate;
    private readonly TimeProvider _stepOverTimeProvider;
    private readonly Action<string>? _resourceEventObserver;
    private NetCoreDbgSession(
        Process process,
        Stream input,
        Stream output,
        Stream error,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        IProcessTreeOwnership? processTreeOwnership,
        TaskCompletionSource<bool>? initializeResponseContinuationReached = null,
        Task? initializeResponseContinuationRelease = null,
        Func<CancellationToken, Task>? preHeaderWriteGate = null,
        TimeProvider? stepOverTimeProvider = null,
        Action<string>? resourceEventObserver = null)
    {
        _process = process;
        _input = input;
        _output = output;
        _error = error;
        _processTreeOwnership = processTreeOwnership;
        _requestTimeout = RequirePositiveTimeout(requestTimeout, nameof(requestTimeout));
        _stopTimeout = RequirePositiveTimeout(stopTimeout, nameof(stopTimeout));
        _initializeResponseContinuationReached = initializeResponseContinuationReached;
        _initializeResponseContinuationRelease = initializeResponseContinuationRelease;
        _preHeaderWriteGate = preHeaderWriteGate;
        _stepOverTimeProvider = stepOverTimeProvider ?? TimeProvider.System;
        _resourceEventObserver = resourceEventObserver;
        _readerTask = ReadMessagesAsync();
        _stderrTask = DrainStandardErrorAsync();
    }

    internal DapSessionState State
    {
        get
        {
            lock (_stateGate)
            {
                return _state;
            }
        }
    }
    internal (DapSessionState State, IReadOnlyDictionary<string, DapLoadedSource>? Sources, bool Complete) GetStateWithSources()
    {
        lock (_stateGate)
        {
            return (_state, _loadedSourcesIncomplete ? null : _loadedSources.ToDictionary(
                static item => item.Key, static item => item.Value.Source, StringComparer.Ordinal), !_loadedSourcesIncomplete);
        }
    }

    internal (DapSessionState State, IReadOnlyDictionary<string, DapLoadedSource>? Sources, bool Complete,
        DapThread[]? Threads, int? ProcessId, string? ProcessName, string? Description, string? Text,
        double? LastStopAt, double? LastResumeAt, JsonElement? Invalidation, JsonElement? MemoryEvent, JsonElement? ExceptionInfo, bool DetailsComplete) GetLegacyResourceState()
    {
        lock (_stateGate)
        {
            return (_state, _loadedSourcesIncomplete ? null : _loadedSources.ToDictionary(
                static item => item.Key, static item => item.Value.Source, StringComparer.Ordinal), !_loadedSourcesIncomplete,
                _legacyThreads, _legacyProcessId, _legacyProcessName, _stopDescription, _stopText,
                _legacyLastStopAt, _legacyLastResumeAt, _legacyInvalidation, _legacyMemoryEvent, _legacyExceptionInfo, !_legacyEventDetailsIncomplete);
        }
    }

    private void InvalidateLoadedSources()
    {
        _loadedSources.Clear();
        _loadedSourceBytes = 0;
        _loadedSourcesIncomplete = true;
    }

    private void StoreLoadedSource(DapLoadedSource source)
    {
        var key = DapLoadedSources.Key(source);
        var bytes = JsonSerializer.SerializeToUtf8Bytes(key).Length + JsonSerializer.SerializeToUtf8Bytes(source).Length + 2;
        _loadedSources.TryGetValue(key, out var previous);
        if ((_loadedSources.Count >= MaximumInspectionEntryCount && previous.Source is null)
            || (long)_loadedSourceBytes - previous.Bytes + bytes > MaximumLoadedSourceStateBytes)
        {
            InvalidateLoadedSources();
            return;
        }
        _loadedSourceBytes += bytes - previous.Bytes;
        _loadedSources[key] = (source, bytes);
    }

    private void RefreshLoadedSources(JsonElement body)
    {
        lock (_stateGate)
        {
            try
            {
                var sources = DapLoadedSources.Parse(body);
                _loadedSources.Clear();
                _loadedSourceBytes = 0;
                _loadedSourcesIncomplete = false;
                foreach (var source in sources)
                {
                    StoreLoadedSource(source);
                    if (_loadedSourcesIncomplete) break;
                }
            }
            catch (Exception exception) when (exception is InvalidDataException or InvalidOperationException or JsonException or ArgumentException or OverflowException)
            {
                InvalidateLoadedSources();
            }
        }
    }

    private void HandleLoadedSourceEvent(JsonElement body)
    {
        lock (_stateGate)
        {
            if (_loadedSourcesIncomplete) return;
            try
            {
                var reason = RequireString(body, "reason");
                if (reason is not "new" and not "changed" and not "removed"
                    || !body.TryGetProperty("source", out var sourceElement))
                    throw new InvalidDataException("Malformed loadedSource transition.");
                var source = DapLoadedSources.ParseSource(sourceElement);
                if (reason == "removed")
                {
                    if (_loadedSources.Remove(DapLoadedSources.Key(source), out var removed))
                        _loadedSourceBytes -= removed.Bytes;
                }
                else StoreLoadedSource(source);
            }
            catch (Exception exception) when (exception is InvalidDataException or InvalidOperationException or JsonException or ArgumentException or OverflowException)
            {
                InvalidateLoadedSources();
            }
        }
    }
    internal bool IsUsable => !IsRetirementRequested && Volatile.Read(ref _loadedSourcesWriteDrainRequired) == 0 && !_readerTask.IsCompleted && !HasExited();
    internal IReadOnlyList<DapInitialBreakpoint> InitialBreakpoints { get; private set; } = Array.Empty<DapInitialBreakpoint>();

    internal string GetOutput(bool clear, string? category) => _outputBuffer.Snapshot(clear, category);

    internal (long TotalLines, long ReturnedLines, string Output) GetOutputTail(long lines, string? category) => _outputBuffer.Tail(lines, category);
    internal DapOutputBuffer OutputBuffer => _outputBuffer;

    internal (DapSessionState State, string Context, string? Description, string? Text)? CaptureStoppedContext()
    {
        lock (_stateGate)
        {
            if (_inspectionClosed || _executionAdmission == ExecutionAdmission.Closing
                || _state.Event != "stopped" || _state.StopReason is null || _state.CurrentThreadId is not { } threadId
                || !HasStepOverAuthority(threadId))
                return null;
            return (_state, GetOrCreateInspectionContext(), _stopDescription, _stopText);
        }
    }

    internal bool IsStoppedContextCurrent(int threadId, string context)
    {
        lock (_stateGate)
            return !_inspectionClosed && _executionAdmission != ExecutionAdmission.Closing
                && _state.Event == "stopped" && _state.CurrentThreadId == threadId
                && HasStepOverAuthority(threadId) && StringComparer.Ordinal.Equals(_inspectionContextId, context);
    }

    internal DapSessionState? CaptureInspectionState(string context)
    {
        lock (_stateGate)
        {
            return !_inspectionClosed && _executionAdmission != ExecutionAdmission.Closing
                && StringComparer.Ordinal.Equals(_inspectionContextId, context) ? _state : null;
        }
    }

    internal DapRuntimeInventorySnapshot GetRuntimeInventory(bool modules, int maximumSerializedBytes)
    {
        lock (_runtimeInventoryGate)
        {
            if (modules ? _modulesIncomplete : _progressIncomplete)
            {
                return new(DapRuntimeInventoryStatus.Incomplete);
            }

            var count = modules ? _modules.Count : _progress.Count;
            var bytes = (modules ? ModulesSnapshotSyntaxBytes + _moduleBytes : ProgressSnapshotSyntaxBytes + _progressBytes)
                + Math.Max(0, count - 1) + RuntimeIntegerBytes(count);
            if (bytes > maximumSerializedBytes)
            {
                return new(DapRuntimeInventoryStatus.TooLarge);
            }

            // Encoded fields and incremental sizes are retained at ingestion. Only an
            // admitted, bounded snapshot traverses the inventory, under the reader's gate.
            using var stream = new MemoryStream((int)bytes);
            using (var writer = new Utf8JsonWriter(stream))
            {
                writer.WriteStartObject();
                writer.WriteString("kind", modules ? "modules_success" : "progress_success");
                writer.WriteStartArray(modules ? "modules" : "progress");
                if (modules)
                {
                    foreach (var entry in _moduleOrder)
                    {
                        writer.WriteStartObject();
                        if (entry.Id.Text is null) writer.WriteNumber("id", entry.Id.Number);
                        else writer.WriteString("id", entry.EncodedId!.Value);
                        WriteRuntimeText(writer, "name", entry.Name);
                        WriteRuntimeText(writer, "path", entry.Path);
                        WriteRuntimeText(writer, "version", entry.Version);
                        writer.WriteBoolean("isOptimized", entry.IsOptimized);
                        WriteRuntimeText(writer, "symbolStatus", entry.SymbolStatus);
                        writer.WriteEndObject();
                    }
                }
                else
                {
                    foreach (var entry in _progressOrder)
                    {
                        writer.WriteStartObject();
                        WriteRuntimeText(writer, "progressId", entry.Id);
                        WriteRuntimeText(writer, "title", entry.Title);
                        WriteRuntimeText(writer, "message", entry.Message);
                        if (entry.Percentage is { } percentage) writer.WriteNumber("percentage", percentage);
                        else writer.WriteNull("percentage");
                        writer.WriteBoolean("cancellable", entry.Cancellable);
                        writer.WriteNumber("startedAt", entry.StartedAt);
                        writer.WriteEndObject();
                    }
                }
                writer.WriteEndArray();
                writer.WriteNumber("count", count);
                writer.WriteEndObject();
            }

            if (stream.Length > maximumSerializedBytes)
            {
                return new(DapRuntimeInventoryStatus.TooLarge);
            }
            return new(DapRuntimeInventoryStatus.Success, Encoding.UTF8.GetString(stream.GetBuffer(), 0, (int)stream.Length));
        }
    }

    private static void WriteRuntimeText(Utf8JsonWriter writer, string name, JsonEncodedText? value)
    {
        if (value is { } text) writer.WriteString(name, text);
        else writer.WriteNull(name);
    }

    private static int RuntimeTextBytes(JsonEncodedText? value) => value is { } text ? text.EncodedUtf8Bytes.Length + 2 : 4;

    private static int RuntimeIntegerBytes(long value)
    {
        Span<char> buffer = stackalloc char[20];
        _ = value.TryFormat(buffer, out var written, provider: CultureInfo.InvariantCulture);
        return written;
    }

    private static int RuntimeNumberBytes(double value)
    {
        Span<char> buffer = stackalloc char[32];
        _ = value.TryFormat(buffer, out var written, provider: CultureInfo.InvariantCulture);
        return written;
    }

    private readonly record struct RuntimeModuleId(long Number, string? Text);

    private sealed record RuntimeModuleEntry(RuntimeModuleId Id, JsonEncodedText? EncodedId, JsonEncodedText Name,
        JsonEncodedText? Path, JsonEncodedText? Version, bool IsOptimized, JsonEncodedText? SymbolStatus)
    {
        internal long Bytes => ModuleEntrySyntaxBytes
            + (Id.Text is null ? RuntimeIntegerBytes(Id.Number) : RuntimeTextBytes(EncodedId))
            + RuntimeTextBytes(Name) + RuntimeTextBytes(Path) + RuntimeTextBytes(Version)
            + (IsOptimized ? 4 : 5) + RuntimeTextBytes(SymbolStatus);
    }

    private sealed record RuntimeProgressEntry(JsonEncodedText Id, JsonEncodedText Title, string? MessageText,
        JsonEncodedText? Message, double? Percentage, bool Cancellable, double StartedAt)
    {
        internal long Bytes => ProgressEntrySyntaxBytes + RuntimeTextBytes(Id) + RuntimeTextBytes(Title)
            + RuntimeTextBytes(Message) + (Percentage is { } percentage ? RuntimeNumberBytes(percentage) : 4)
            + (Cancellable ? 4 : 5) + RuntimeNumberBytes(StartedAt);
    }

    internal bool IsRetirementRequested => Volatile.Read(ref _retirementRequested) != 0;
    internal event Action<NetCoreDbgSession>? RetirementRequested;

    internal event Action<NetCoreDbgSession>? ReaderFailed;
    internal event Action<NetCoreDbgSession>? NativeUiInvokeAuthorityRevoked;
    internal bool TryGetNativeSceneTargetIdentity(out NativeSceneTargetIdentity targetIdentity)
    {
        targetIdentity = Volatile.Read(ref _nativeSceneTargetIdentity)!;
        return targetIdentity is not null;
    }

    internal bool TryGetNativeSceneCaptureTargetIdentity(out NativeSceneTargetIdentity targetIdentity)
    {
        if (Volatile.Read(ref _nativeSceneCaptureAuthorityInvalidated) != 0)
        {
            targetIdentity = null!;
            return false;
        }

        targetIdentity = Volatile.Read(ref _nativeSceneTargetIdentity)!;
        return targetIdentity is not null;
    }

    internal bool TryGetNativeUiInvokeTargetIdentity(out NativeSceneTargetIdentity targetIdentity)
    {
        if (Volatile.Read(ref _nativeUiInvokeAuthorityInvalidated) != 0 || !IsUsable)
        {
            targetIdentity = null!;
            return false;
        }

        return TryGetNativeSceneCaptureTargetIdentity(out targetIdentity) &&
               Volatile.Read(ref _nativeUiInvokeAuthorityInvalidated) == 0;
    }

    internal static Task<NetCoreDbgSession> StartAsync(
        string debuggerPath,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        CancellationToken cancellationToken) =>
        StartAsync(
            debuggerPath,
            programPath,
            initializeTimeout,
            requestTimeout,
            stopTimeout,
            launchEnvironment: null,
            cancellationToken: cancellationToken);

    internal static Task<NetCoreDbgSession> StartAsync(
        string debuggerPath,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        CancellationToken cancellationToken) =>
        StartAsync(
            debuggerPath,
            programPath,
            initializeTimeout,
            requestTimeout,
            stopTimeout,
            launchEnvironment,
            Array.Empty<DapSourceBreakpoint>(),
            cancellationToken);

    internal static Task<NetCoreDbgSession> StartAsync(
        string debuggerPath,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        IReadOnlyList<DapSourceBreakpoint> breakpoints,
        CancellationToken cancellationToken) =>
        StartAsync(debuggerPath, programPath, initializeTimeout, requestTimeout, stopTimeout,
            launchEnvironment, breakpoints, failedStartCleanupCompleted: null, cancellationToken);

    internal static Task<NetCoreDbgSession> StartAsync(
        string debuggerPath,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        IReadOnlyList<DapSourceBreakpoint> breakpoints,
        Action? failedStartCleanupCompleted,
        CancellationToken cancellationToken) =>
        StartAsync(debuggerPath, programPath, initializeTimeout, requestTimeout, stopTimeout,
            launchEnvironment, breakpoints, failedStartCleanupCompleted, launchOptions: null, cancellationToken);

    internal static async Task<NetCoreDbgSession> StartAsync(
        string debuggerPath,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        IReadOnlyList<DapSourceBreakpoint> breakpoints,
        Action? failedStartCleanupCompleted,
        DapLaunchOptions? launchOptions,
        CancellationToken cancellationToken, Action<string>? resourceEventObserver = null)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(debuggerPath);
        ArgumentException.ThrowIfNullOrWhiteSpace(programPath);
        RequirePositiveTimeout(initializeTimeout, nameof(initializeTimeout));
        RequirePositiveTimeout(requestTimeout, nameof(requestTimeout));
        RequirePositiveTimeout(stopTimeout, nameof(stopTimeout));
        var removals = DapLaunchEnvironment.Removals(launchOptions?.Environment);
        var dapEnvironment = DapLaunchEnvironment.Merge(launchEnvironment, launchOptions?.Environment);

        Process? process = null;
        IProcessTreeOwnership? processTreeOwnership = null;
        WindowsProcessTreeLaunch? windowsLaunch = null;
        NetCoreDbgSession? session = null;
        try
        {
            if (OperatingSystem.IsWindows())
            {
                using var childEnvironment = WindowsChildEnvironmentBlock.Create(removals, dapEnvironment?.Keys);
                dapEnvironment = childEnvironment.MatchInheritedSpellings(dapEnvironment);
                windowsLaunch = WindowsProcessTreeOwnership.Start(debuggerPath, childEnvironment);
                process = windowsLaunch.Process;
                session = new NetCoreDbgSession(
                    process,
                    windowsLaunch.Input,
                    windowsLaunch.Output,
                    windowsLaunch.Error,
                    requestTimeout,
                    stopTimeout,
                    windowsLaunch.Ownership, resourceEventObserver: resourceEventObserver);
                windowsLaunch = null;
            }
            else
            {
                var unixLaunch = UnixProcessGroupOwnership.Start(debuggerPath, removals);
                process = unixLaunch.Process;
                processTreeOwnership = unixLaunch.Ownership;
                session = new NetCoreDbgSession(
                    process,
                    process.StandardInput.BaseStream,
                    process.StandardOutput.BaseStream,
                    process.StandardError.BaseStream,
                    requestTimeout,
                    stopTimeout,
                    processTreeOwnership, resourceEventObserver: resourceEventObserver);
            }

            await session.StartProtocolAsync(programPath, initializeTimeout, dapEnvironment, breakpoints, launchOptions, cancellationToken).ConfigureAwait(false);
            return session;
        }
        catch (Exception startupFailure)
        {
            try
            {
                if (session is not null)
                {
                    session._forceCleanup.Cancel();
                    await session.EnsureCleanupAsync().ConfigureAwait(false);
                }
                else if (windowsLaunch is not null)
                {
                    windowsLaunch.Dispose();
                }
                else if (process is not null)
                {
                    await StopUnmanagedProcessAsync(process, stopTimeout, processTreeOwnership).ConfigureAwait(false);
                }

                if (session is not null || windowsLaunch is not null || process is not null)
                {
                    failedStartCleanupCompleted?.Invoke();
                }
            }
            catch (Exception cleanupFailure)
            {
                throw new DapStartupCleanupException(startupFailure, cleanupFailure);
            }

            throw;
        }
    }

    internal async Task StopAsync(CancellationToken cancellationToken)
    {
        var cleanup = EnsureCleanupAsync();
        try
        {
            await cleanup.WaitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            _forceCleanup.Cancel();
            await cleanup.ConfigureAwait(false);
            throw;
        }
    }

    internal async Task StopForOwnerAsync(CancellationToken forceCleanupToken)
    {
        // A drain abort requests forced cleanup; SessionSlot owns external cancellation reporting.
        using var forceCleanup = forceCleanupToken.Register(
            static state => ((CancellationTokenSource)state!).Cancel(), _forceCleanup);
        await EnsureCleanupAsync().ConfigureAwait(false);
    }

    internal async Task<DapThreadsResult> GetThreadsAsync(CancellationToken cancellationToken)
    {
        var response = await SendRequestAcceptingRefusalAsync(
            "threads",
            arguments: null,
            _requestTimeout,
            cancellationToken).ConfigureAwait(false);
        if (!response.Success)
        {
            return DapThreadsResult.Refused;
        }

        var threads = RequireThreads(response.Body);
        var normalized = new DapThread[threads.GetArrayLength()];
        _ = ReadThreads(threads, normalized);
        return DapThreadsResult.Succeeded(normalized);
    }

    private static JsonElement RequireThreads(JsonElement body)
    {
        if (body.ValueKind != JsonValueKind.Object
            || !body.TryGetProperty("threads", out var threads)
            || threads.ValueKind != JsonValueKind.Array
            || threads.GetArrayLength() > MaximumThreadCount)
        {
            throw new InvalidDataException("DAP threads response body is invalid.");
        }

        return threads;
    }

    private static int? ReadThreads(JsonElement threads, DapThread[]? normalized)
    {
        int? firstThread = null;
        var index = 0;
        foreach (var thread in threads.EnumerateArray())
        {
            var id = RequireInt32(thread, "id");
            var name = RequireString(thread, "name");
            if (Encoding.UTF8.GetByteCount(name) > MaximumThreadNameBytes)
            {
                throw new InvalidDataException("DAP thread name exceeds the supported maximum.");
            }

            if (index == 0)
            {
                firstThread = id;
            }
            if (normalized is not null)
            {
                normalized[index] = new DapThread(id, name);
            }
            index++;
        }

        return firstThread;
    }

    internal Task<DapCallStackResult> GetCallStackAsync(
        int threadId,
        uint startFrame,
        uint levels,
        CancellationToken cancellationToken) =>
        GetCallStackAsync(threadId, startFrame, levels, false, cancellationToken);

    internal Task<DapCallStackResult> GetCallStackAsync(
        int threadId,
        uint startFrame,
        uint levels,
        bool includeInspectionContext,
        CancellationToken cancellationToken) =>
        GetCallStackForOptionalThreadAsync(threadId, startFrame, levels, includeInspectionContext, cancellationToken);

    internal async Task<DapCallStackResult> GetCallStackForOptionalThreadAsync(
        int? threadId,
        uint startFrame,
        uint levels,
        bool includeInspectionContext,
        CancellationToken cancellationToken)
    {
        var (response, isPaged) = await SendStackTraceRequestAsync(
            threadId,
            startFrame,
            levels,
            includeInspectionContext,
            cancellationToken).ConfigureAwait(false);
        if (response is null || !response.Success || !response.TargetCurrent)
        {
            return DapCallStackResult.Refused;
        }

        if (response.Body.ValueKind != JsonValueKind.Object
            || !response.Body.TryGetProperty("stackFrames", out var stackFrames)
            || stackFrames.ValueKind != JsonValueKind.Array
            || stackFrames.GetArrayLength() > MaximumStackFrameCount)
        {
            throw new InvalidDataException("DAP stackTrace response body is invalid.");
        }

        uint? totalFrames = null;
        if (response.Body.TryGetProperty("totalFrames", out var totalFramesElement))
        {
            if (totalFramesElement.ValueKind != JsonValueKind.Number
                || !totalFramesElement.TryGetUInt32(out var parsedTotalFrames))
            {
                throw new InvalidDataException("DAP stackTrace totalFrames must be an unsigned 32-bit integer.");
            }

            totalFrames = parsedTotalFrames;
        }

        var frameCount = stackFrames.GetArrayLength();
        var pageStart = isPaged ? 0 : (int)Math.Min(startFrame, (uint)frameCount);
        var pageLength = isPaged ? frameCount : (int)Math.Min(levels, (uint)(frameCount - pageStart));
        var normalized = pageLength == 0 ? Array.Empty<DapStackFrame>() : new DapStackFrame[pageLength];
        var index = 0;
        foreach (var stackFrame in stackFrames.EnumerateArray())
        {
            var pageIndex = index - pageStart;
            var frame = NormalizeStackFrame(stackFrame, pageIndex >= 0 && pageIndex < pageLength);
            if (frame is not null)
            {
                normalized[pageIndex] = frame;
            }

            index++;
        }

        return DapCallStackResult.Succeeded(normalized, totalFrames, response.InspectionContextId);
    }

    internal Task<DapExceptionInfoResult> GetExceptionInfoAsync(int threadId, CancellationToken cancellationToken) =>
        GetExceptionInfoForOptionalThreadAsync(threadId, cancellationToken);

    internal async Task<DapExceptionInfoResult> GetExceptionInfoForOptionalThreadAsync(int? threadId, CancellationToken cancellationToken)
    {
        PendingRequest request;
        await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            lock (_stateGate)
            {
                if (!_supportsExceptionInfoRequest)
                {
                    return DapExceptionInfoResult.Unsupported;
                }
            }

            var target = CaptureOptionalCallStackTarget(threadId, includeInspectionContext: true);
            if (target is null)
            {
                return DapExceptionInfoResult.Refused;
            }

            request = await BeginRequestAsync("exceptionInfo", new { threadId = target.Value.ThreadId }, _requestTimeout,
                cancellationToken, stackTraceTarget: target).ConfigureAwait(false);
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }

        try
        {
            var response = await WaitForResponseAsync(request, _requestTimeout, cancellationToken).ConfigureAwait(false);
            if (!response.Success || !response.TargetCurrent)
            {
                return DapExceptionInfoResult.Refused;
            }

            var body = response.Body;
            _ = RequireString(body, "exceptionId");
            if (RequireString(body, "breakMode") is not ("never" or "always" or "unhandled" or "userUnhandled"))
            {
                throw new InvalidDataException("DAP exceptionInfo breakMode is invalid.");
            }
            if (body.TryGetProperty("description", out _))
            {
                _ = RequireString(body, "description");
            }
            if (body.TryGetProperty("details", out var details) && details.ValueKind != JsonValueKind.Object)
            {
                throw new InvalidDataException("DAP exceptionInfo details must be an object.");
            }

            return new DapExceptionInfoResult(DapExceptionInfoOutcome.Success, body);
        }
        finally
        {
            RemovePending(request);
        }
    }

    internal async Task<DapLoadedSourcesResult> GetLoadedSourcesAsync(CancellationToken cancellationToken)
    {
        PendingRequest request;
        await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            lock (_stateGate)
            {
                if (!_supportsLoadedSourcesRequest) return DapLoadedSourcesResult.Unsupported;
            }

            request = await BeginRequestAsync("loadedSources", new { }, _requestTimeout, cancellationToken, trackLoadedSourcesWrite: true).ConfigureAwait(false);
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }

        try
        {
            var response = await WaitForResponseAsync(request, _requestTimeout, cancellationToken).ConfigureAwait(false);
            return response.Success
                ? new DapLoadedSourcesResult(DapLoadedSourcesOutcome.Success, DapLoadedSources.Parse(response.Body))
                : DapLoadedSourcesResult.Refused;
        }
        finally
        {
            RemovePending(request);
        }
    }

    internal bool RequiresLoadedSourcesDrain() => Volatile.Read(ref _loadedSourcesWriteDrainRequired) != 0;

    internal async Task<DapStepTargetsResult> GetStepInTargetsAsync(string inspectionContextId, int frameId, CancellationToken cancellationToken)
    {
        PendingRequest request;
        await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            lock (_stateGate)
            {
                if (!_supportsStepInTargetsRequest) return new(DapStepTargetsOutcome.Unsupported, null);
                if (_inspectionClosed || !StringComparer.Ordinal.Equals(_inspectionContextId, inspectionContextId))
                    return new(DapStepTargetsOutcome.Refused, null);
            }
            request = await BeginRequestAsync("stepInTargets", new { frameId }, _requestTimeout, cancellationToken,
                inspectionContextId: inspectionContextId).ConfigureAwait(false);
        }
        finally { _callStackAdmissionGate.Release(); }

        try
        {
            var response = await WaitForResponseAsync(request, _requestTimeout, cancellationToken).ConfigureAwait(false);
            if (!response.Success || !response.TargetCurrent) return new(DapStepTargetsOutcome.Refused, null);
            var entries = RequireInspectionArray(response.Body, "targets");
            var targets = new DapStepTarget[entries.GetArrayLength()];
            var index = 0;
            foreach (var entry in entries.EnumerateArray())
            {
                int? Position(string name)
                {
                    if (!entry.TryGetProperty(name, out var value)) return null;
                    if (value.ValueKind != JsonValueKind.Number || !value.TryGetInt32(out var number) || number < 0)
                        throw new InvalidDataException("DAP step target position is invalid.");
                    return number;
                }
                targets[index++] = new DapStepTarget(RequireInt32(entry, "id"), RequireInspectionName(entry, "label"),
                    Position("line"), Position("column"), Position("endLine"), Position("endColumn"));
            }
            return new(DapStepTargetsOutcome.Success, targets);
        }
        finally { RemovePending(request); }
    }

    internal async Task<DapScopesResult> GetScopesAsync(
        string inspectionContextId,
        int frameId,
        CancellationToken cancellationToken)
    {
        var response = await SendInspectionRequestAsync(
            "scopes", new { frameId }, inspectionContextId, cancellationToken).ConfigureAwait(false);
        if (response is null || !response.Success || !response.TargetCurrent)
        {
            return DapScopesResult.Refused;
        }

        var scopes = RequireInspectionArray(response.Body, "scopes");
        var normalized = new DapScope[scopes.GetArrayLength()];
        var index = 0;
        foreach (var scope in scopes.EnumerateArray())
        {
            var name = RequireInspectionName(scope, "name");
            var reference = RequireVariablesReference(scope);
            if (!scope.TryGetProperty("expensive", out var expensive)
                || expensive.ValueKind is not JsonValueKind.True and not JsonValueKind.False)
            {
                throw new InvalidDataException("DAP scope expensive must be a boolean.");
            }

            normalized[index++] = new DapScope(name, reference, expensive.GetBoolean());
        }

        return new DapScopesResult(false, response.InspectionContextId!, normalized);
    }

    internal async Task<DapVariablesResult> GetVariablesAsync(
        string inspectionContextId,
        int variablesReference,
        int? start,
        int? count,
        CancellationToken cancellationToken)
    {
        var response = await SendInspectionRequestAsync(
            "variables", new DapVariablesArguments(variablesReference, start, count), inspectionContextId, cancellationToken).ConfigureAwait(false);
        if (response is null || !response.Success || !response.TargetCurrent)
        {
            return DapVariablesResult.Refused;
        }

        var variables = RequireInspectionArray(response.Body, "variables");
        if (count is > 0 && variables.GetArrayLength() > count.Value)
        {
            throw new InvalidDataException("DAP variables response exceeds the requested count.");
        }

        var normalized = new DapVariable[variables.GetArrayLength()];
        var index = 0;
        foreach (var variable in variables.EnumerateArray())
        {
            var name = RequireInspectionName(variable, "name");
            var value = RequireString(variable, "value");
            var type = variable.TryGetProperty("type", out _) ? RequireInspectionName(variable, "type") : null;
            normalized[index++] = new DapVariable(name, value, type, RequireVariablesReference(variable));
        }

        return new DapVariablesResult(false, response.InspectionContextId!, normalized);
    }

    internal async Task<DapEvaluateResult> EvaluateExpressionAsync(
        string inspectionContextId,
        int frameId,
        string expression,
        CancellationToken cancellationToken)
    {
        var response = await SendInspectionRequestAsync(
            "evaluate", new { expression, frameId, context = "watch" }, inspectionContextId, cancellationToken).ConfigureAwait(false);
        if (response is null)
        {
            return DapEvaluateResult.Refused;
        }
        if (!response.TargetCurrent)
        {
            return DapEvaluateResult.OutcomeUnknown;
        }
        if (!response.Success)
        {
            // An adapter failure does not prove that the expression had no side effects.
            return DapEvaluateResult.Failed;
        }

        var result = RequireString(response.Body, "result");
        var type = response.Body.TryGetProperty("type", out var typeElement) && typeElement.ValueKind != JsonValueKind.Null
            ? RequireInspectionName(response.Body, "type") : null;
        return new DapEvaluateResult(DapEvaluateOutcome.Success, response.InspectionContextId!, result, type,
            RequireVariablesReference(response.Body));
    }

    internal async Task<DapSetVariableResult> SetVariableAsync(
        string inspectionContextId, int variablesReference, string name, string value, CancellationToken cancellationToken)
    {
        PendingRequest request;
        await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            lock (_stateGate)
            {
                if (_inspectionClosed || !StringComparer.Ordinal.Equals(_inspectionContextId, inspectionContextId))
                {
                    return DapSetVariableResult.Refused;
                }
                if (!_supportsSetVariable)
                {
                    return DapSetVariableResult.Unsupported;
                }
            }
            request = await BeginRequestAsync("setVariable", new { variablesReference, name, value }, _requestTimeout,
                cancellationToken, inspectionContextId: inspectionContextId).ConfigureAwait(false);
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }

        try
        {
            var response = await WaitForResponseAsync(request, _requestTimeout, cancellationToken).ConfigureAwait(false);
            if (!response.TargetCurrent)
            {
                return DapSetVariableResult.OutcomeUnknown;
            }
            if (!response.Success)
            {
                return DapSetVariableResult.Failed;
            }
            return new DapSetVariableResult(DapSetVariableOutcome.Success, response.SerializedContent);
        }
        catch (Exception) when (request.WriteStarted)
        {
            Interlocked.Exchange(ref _inspectionWriteDrainRequired, 1);
            InvalidateInspectionContext(close: true);
            throw;
        }
        finally
        {
            RemovePending(request);
        }
    }

    internal bool RequiresInspectionDrain() => Volatile.Read(ref _inspectionWriteDrainRequired) != 0;

    private static JsonElement RequireInspectionArray(JsonElement body, string name)
    {
        if (body.ValueKind != JsonValueKind.Object
            || !body.TryGetProperty(name, out var entries)
            || entries.ValueKind != JsonValueKind.Array
            || entries.GetArrayLength() > MaximumInspectionEntryCount)
        {
            throw new InvalidDataException("DAP inspection response array is invalid.");
        }

        return entries;
    }

    private static string RequireInspectionName(JsonElement entry, string name)
    {
        var value = RequireString(entry, name);
        if (Encoding.UTF8.GetByteCount(value) > MaximumInspectionNameBytes)
        {
            throw new InvalidDataException("DAP inspection name or type exceeds the supported maximum.");
        }

        return value;
    }

    private static int RequireVariablesReference(JsonElement entry)
    {
        var reference = RequireInt32(entry, "variablesReference");
        if (reference < 0)
        {
            throw new InvalidDataException("DAP variablesReference must be non-negative.");
        }

        return reference;
    }

    internal async Task<DapPauseResult> PauseAsync(int? threadId, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        PauseRecord active;
        lock (_stateGate)
        {
            if (_inspectionClosed || IsRetirementRequested || _executionAdmission == ExecutionAdmission.Closing
                || _activePause is not null || _activeStepOver is not null
                || (_executionAdmission == ExecutionAdmission.Active && _activeContinue is not { CommittedWrite: true }))
            {
                return DapPauseResult.Refused;
            }

            if (_state.Event is "exited" or "terminated")
            {
                FencePause();
                return new DapPauseResult(DapPauseOutcome.Terminal, Terminal: _state.Event,
                    ExitCode: _state.ExitCode, RequiresDrain: true);
            }

            active = new PauseRecord(_requestTimeout, _stepOverTimeProvider, cancellationToken);
            _activePause = active;
        }

        try
        {
            int? selectedThread = threadId is { } explicitThread && explicitThread != 0 ? explicitThread : null;
            PendingRequest request;
            await _callStackAdmissionGate.WaitAsync(active.DeadlineToken).ConfigureAwait(false);
            try
            {
                lock (_stateGate)
                {
                    if (!CanWritePause(active))
                    {
                        return CompletePause(active, active.ProtocolFailure is not null
                            ? new DapPauseResult(DapPauseOutcome.ProtocolError) : active.Outcome ?? DapPauseResult.Refused);
                    }

                    selectedThread ??= _state.CurrentThreadId;
                    active.ThreadId = selectedThread;
                }

                request = await BeginRequestAsync(selectedThread.HasValue ? "pause" : "threads",
                    selectedThread.HasValue ? new DapPauseArguments(selectedThread.Value) : null,
                    _requestTimeout, active.DeadlineToken, pause: active).ConfigureAwait(false);
            }
            finally
            {
                _callStackAdmissionGate.Release();
            }

            if (!selectedThread.HasValue)
            {
                var response = await WaitForPauseResponseAsync(request, active).ConfigureAwait(false);
                lock (_stateGate)
                {
                    if (active.Outcome is { } observed)
                    {
                        return CompletePause(active, observed);
                    }
                }

                if (!response!.Success)
                {
                    return CompletePause(active, DapPauseResult.Refused);
                }

                var discoveredThread = ReadThreads(RequireThreads(response.Body), normalized: null);
                RemovePending(request);
                await _callStackAdmissionGate.WaitAsync(active.DeadlineToken).ConfigureAwait(false);
                try
                {
                    lock (_stateGate)
                    {
                        if (!CanWritePause(active))
                        {
                            return CompletePause(active, active.ProtocolFailure is not null
                                ? new DapPauseResult(DapPauseOutcome.ProtocolError) : active.Outcome ?? DapPauseResult.Refused);
                        }

                        selectedThread = _state.CurrentThreadId ?? discoveredThread;
                        if (!selectedThread.HasValue)
                        {
                            return CompletePause(active, new DapPauseResult(DapPauseOutcome.NoThreads));
                        }

                        active.ThreadId = selectedThread;
                    }

                    request = await BeginRequestAsync("pause", new DapPauseArguments(selectedThread.Value),
                        _requestTimeout, active.DeadlineToken, pause: active).ConfigureAwait(false);
                }
                finally
                {
                    _callStackAdmissionGate.Release();
                }
            }

            _ = await WaitForPauseResponseAsync(request, active).ConfigureAwait(false);
            lock (_stateGate)
            {
                return CompletePause(active, active.Outcome
                    ?? throw new InvalidDataException("DAP pause completed without an acknowledgement."));
            }
        }
        catch (OperationCanceledException exception)
        {
            lock (_stateGate)
            {
                var requiresDrain = PauseWriteRequiresDrain(active);
                if (requiresDrain)
                {
                    FencePause();
                }

                if (cancellationToken.IsCancellationRequested)
                {
                    throw new DapPauseCanceledException(requiresDrain, exception, cancellationToken);
                }

                if (active.Outcome is { } observed && active.Request is { CommittedWrite: true })
                {
                    return CompletePause(active, observed);
                }

                return CompletePause(active, new DapPauseResult(active.ProtocolFailure is null && active.DeadlineToken.IsCancellationRequested
                    ? DapPauseOutcome.TimedOut : DapPauseOutcome.ProtocolError));
            }
        }
        catch (Exception)
        {
            return CompletePause(active, new DapPauseResult(DapPauseOutcome.ProtocolError));
        }
        finally
        {
            lock (_stateGate)
            {
                active.Finalized = true;
                if (active.Request is { } pending)
                {
                    RemovePending(pending);
                }

                if (ReferenceEquals(_activePause, active))
                {
                    _activePause = null;
                }
            }

            active.Dispose();
        }
    }

    private bool CanWritePause(PauseRecord active) =>
        ReferenceEquals(_activePause, active) && active.Outcome is null && active.ProtocolFailure is null
        && !_inspectionClosed && !IsRetirementRequested && _executionAdmission != ExecutionAdmission.Closing
        && _state.Event is not "exited" and not "terminated";

    private static bool PauseWriteRequiresDrain(PauseRecord active) =>
        active.ProtocolFailure is not null || active.Outcome?.Outcome == DapPauseOutcome.Terminal
        || (active.Request is { WriteStarted: true } request
            && (!request.CommittedWrite || (request.Command == "pause" && !active.AcknowledgementObserved)));

    private DapPauseResult CompletePause(PauseRecord active, DapPauseResult result)
    {
        lock (_stateGate)
        {
            var requiresDrain = result.Outcome is DapPauseOutcome.ProtocolError or DapPauseOutcome.Terminal
                || PauseWriteRequiresDrain(active);
            if (requiresDrain)
            {
                FencePause();
            }
            active.Finalized = true;

            return result with { RequiresDrain = requiresDrain };
        }
    }

    private void FencePause()
    {
        _executionAdmission = ExecutionAdmission.Closing;
        _inspectionClosed = true;
        _inspectionContextId = null;
        ClearCallStackEligibility();
    }

    private async Task<DapResponse?> WaitForPauseResponseAsync(PendingRequest request, PauseRecord active)
    {
        await Task.WhenAny(request.Completion.Task, active.Completion.Task)
            .WaitAsync(active.DeadlineToken).ConfigureAwait(false);
        lock (_stateGate)
        {
            if (active.ProtocolFailure is { } failure)
            {
                throw new InvalidDataException("DAP pause exchange failed.", failure);
            }

            if (active.Outcome is not null)
            {
                return null;
            }
            active.DeadlineToken.ThrowIfCancellationRequested();
        }

        var response = await request.Completion.Task.WaitAsync(active.DeadlineToken).ConfigureAwait(false);
        lock (_stateGate)
        {
            if (active.Outcome is null)
            {
                active.DeadlineToken.ThrowIfCancellationRequested();
            }
        }
        return response;
    }

    internal Task<DapStepOverResult> StepOverAsync(int threadId, CancellationToken cancellationToken) =>
        StepAsync(threadId, DapStepKind.Over, cancellationToken);

    internal Task<DapStepOverResult> StepIntoAsync(int threadId, CancellationToken cancellationToken) =>
        StepAsync(threadId, DapStepKind.Into, cancellationToken);

    internal Task<DapStepOverResult> StepOutAsync(int threadId, CancellationToken cancellationToken) =>
        StepAsync(threadId, DapStepKind.Out, cancellationToken);

    internal Task<DapStepOverResult> StepForOptionalThreadAsync(int? threadId, DapStepKind kind, CancellationToken cancellationToken) =>
        StepAsync(threadId, kind, cancellationToken);

    internal Task<DapStepOverResult> StepIntoTargetAsync(int? threadId, int targetId, string inspectionContextId, CancellationToken cancellationToken) =>
        StepAsync(threadId, DapStepKind.Into, cancellationToken, targetId, inspectionContextId);

    private async Task<DapStepOverResult> StepAsync(int? threadId, DapStepKind kind, CancellationToken cancellationToken,
        int? targetId = null, string? inspectionContextId = null)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!TryAdmitExecution())
        {
            return DapStepOverResult.Refused;
        }

        StepOverRecord? active = null;
        PendingRequest? request = null;
        var closeAdmission = false;
        try
        {
            await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                int selectedThreadId;
                lock (_stateGate)
                {
                    if (targetId is not null && !_supportsStepInTargetsRequest) return DapStepOverResult.Unsupported;
                    if (targetId is not null && (_inspectionClosed || !StringComparer.Ordinal.Equals(_inspectionContextId, inspectionContextId)))
                        return DapStepOverResult.Refused;
                    var selected = threadId ?? _state.CurrentThreadId;
                    if (_inspectionClosed || (threadId is null && _state.Event != "stopped")
                        || selected is not { } resolved || !HasStepOverAuthority(resolved))
                    {
                        return DapStepOverResult.Refused;
                    }

                    selectedThreadId = resolved;
                    active = new StepOverRecord(selectedThreadId, kind, cancellationToken);
                    _activeStepOver = active;
                    _state = _state with { CurrentThreadId = null };
                    ClearCallStackEligibility();
                    _inspectionContextId = null;
                }

                request = await BeginRequestAsync(
                    active.Command,
                    targetId is null ? new { threadId = selectedThreadId } : (object)new { threadId = selectedThreadId, targetId = targetId.Value },
                    _requestTimeout,
                    cancellationToken,
                    stepOver: active).ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                closeAdmission = exception is not OperationCanceledException || active?.WriteStarted == true;
                if (active?.WriteStarted == true)
                {
                    Interlocked.Exchange(ref _stepOverDrainRequired, 1);
                }

                throw;
            }
            finally
            {
                _callStackAdmissionGate.Release();
            }

            try
            {
                var result = await WaitForStepOverOutcomeAsync(request!, active!, active!.DeadlineToken).ConfigureAwait(false);
                closeAdmission = result.Outcome is DapStepOverOutcome.Terminal or DapStepOverOutcome.TimedOut;
                return result;
            }
            catch (OperationCanceledException) when (!cancellationToken.IsCancellationRequested)
            {
                Interlocked.Exchange(ref _stepOverDrainRequired, 1);
                closeAdmission = true;
                return DapStepOverResult.TimedOut(active!.ThreadId);
            }
            catch (Exception exception)
            {
                closeAdmission = true;
                if (exception is OperationCanceledException && active!.CommittedWrite)
                {
                    Interlocked.Exchange(ref _stepOverDrainRequired, 1);
                }

                throw;
            }
        }
        finally
        {
            if (request is not null)
            {
                RemovePending(request);
            }

            lock (_stateGate)
            {
                if (ReferenceEquals(_activeStepOver, active))
                {
                    _activeStepOver = null;
                }

                _executionAdmission = _executionAdmission == ExecutionAdmission.Closing
                    || closeAdmission || RequiresStepOverDrain()
                    ? ExecutionAdmission.Closing
                    : ExecutionAdmission.Available;
            }

            active?.Dispose();
        }
    }

    internal bool RequiresStepOverDrain() => Volatile.Read(ref _stepOverDrainRequired) != 0;

    private bool TryAdmitExecution()
    {
        lock (_stateGate)
        {
            if (_executionAdmission != ExecutionAdmission.Available || _inspectionClosed || _activePause is not null)
            {
                return false;
            }

            _executionAdmission = ExecutionAdmission.Active;
            return true;
        }
    }

    internal Task<DapContinueResult> ContinueAsync(int threadId, CancellationToken cancellationToken) =>
        ContinueForOptionalThreadAsync(threadId, cancellationToken);

    internal async Task<DapContinueResult> ContinueForOptionalThreadAsync(int? threadId, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!TryAdmitExecution())
        {
            return DapContinueResult.Refused;
        }

        ContinueRecord? active = null;
        PendingRequest? request = null;
        try
        {
            await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
            try
            {
                int selectedThreadId;
                lock (_stateGate)
                {
                    var selected = threadId ?? _state.CurrentThreadId;
                    if (_inspectionClosed || (threadId is null && _state.Event != "stopped")
                        || selected is not { } resolved || !HasStepOverAuthority(resolved))
                    {
                        return DapContinueResult.Refused;
                    }

                    selectedThreadId = resolved;
                    active = new ContinueRecord(selectedThreadId, cancellationToken);
                    _activeContinue = active;
                    _state = new DapSessionState(null, null, null);
                    ClearCallStackEligibility();
                    _inspectionContextId = null;
                }

                request = await BeginRequestAsync(
                    "continue",
                    new DapContinueArguments(selectedThreadId),
                    ContinueTimeout,
                    cancellationToken,
                    continueExecution: active).ConfigureAwait(false);
            }
            finally
            {
                _callStackAdmissionGate.Release();
            }

            return await WaitForContinueOutcomeAsync(
                active!, active!.DeadlineToken, cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException exception)
        {
            if (active is null)
            {
                throw;
            }

            DapContinueResult result;
            Exception? protocolFailure;
            lock (_stateGate)
            {
                protocolFailure = active.ProtocolFailure;
                result = FinalizeContinue(active, interrupted: true);
            }

            if (cancellationToken.IsCancellationRequested)
            {
                throw new DapContinueCanceledException(result.RequiresDrain, exception, cancellationToken);
            }

            if (protocolFailure is not null)
            {
                throw new InvalidDataException("DAP continue exchange failed.", protocolFailure);
            }

            return result;
        }
        catch
        {
            if (active is not null)
            {
                lock (_stateGate)
                {
                    active.Finalized = true;
                    _executionAdmission = ExecutionAdmission.Closing;
                    _inspectionClosed = true;
                    ClearCallStackEligibility();
                    _inspectionContextId = null;
                }
            }

            throw;
        }
        finally
        {
            if (request is not null)
            {
                RemovePending(request);
            }

            lock (_stateGate)
            {
                if (ReferenceEquals(_activeContinue, active))
                {
                    _activeContinue = null;
                }

                if (_executionAdmission == ExecutionAdmission.Active)
                {
                    _executionAdmission = ExecutionAdmission.Available;
                }
            }

            active?.Dispose();
        }
    }

    private DapContinueResult FinalizeContinue(ContinueRecord active, bool interrupted)
    {
        var acknowledged = active.Acknowledged;
        var result = active.Outcome;
        if (result is null)
        {
            if (!interrupted)
            {
                throw new InvalidOperationException("Continue completed without an observed outcome.");
            }

            result = new DapContinueResult(DapContinueOutcome.TimedOut, active.ThreadId);
        }

        var requiresDrain = active.ProtocolFailure is not null || result.Outcome == DapContinueOutcome.Terminal
            || (interrupted && active.WriteStarted && !(active.CommittedWrite && acknowledged));
        active.Finalized = true;
        if (requiresDrain)
        {
            _executionAdmission = ExecutionAdmission.Closing;
        }
        else if (_executionAdmission != ExecutionAdmission.Closing)
        {
            _executionAdmission = ExecutionAdmission.Active;
        }
        if (requiresDrain)
        {
            _inspectionClosed = true;
            ClearCallStackEligibility();
            _inspectionContextId = null;
        }

        return result with { Acknowledged = acknowledged, RequiresDrain = requiresDrain };
    }


    public ValueTask DisposeAsync() => new(EnsureCleanupAsync());

    private Task StartProtocolAsync(string programPath, TimeSpan initializeTimeout, CancellationToken cancellationToken) =>
        StartProtocolAsync(programPath, initializeTimeout, launchEnvironment: null, cancellationToken: cancellationToken);

    private Task StartProtocolAsync(
        string programPath,
        TimeSpan initializeTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        CancellationToken cancellationToken) =>
        StartProtocolAsync(programPath, initializeTimeout, launchEnvironment, Array.Empty<DapSourceBreakpoint>(), cancellationToken);

    private Task StartProtocolAsync(
        string programPath,
        TimeSpan initializeTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        IReadOnlyList<DapSourceBreakpoint> breakpoints,
        CancellationToken cancellationToken) =>
        StartProtocolAsync(programPath, initializeTimeout, launchEnvironment, breakpoints, launchOptions: null, cancellationToken);

    private async Task StartProtocolAsync(
        string programPath,
        TimeSpan initializeTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        IReadOnlyList<DapSourceBreakpoint> breakpoints,
        DapLaunchOptions? launchOptions,
        CancellationToken cancellationToken)
    {
        _ = await SendRequestAsync(
            "initialize",
            new
            {
                clientID = "netcoredbg-mcp-stateless",
                clientName = "netcoredbg-mcp-stateless",
                adapterID = "coreclr",
                pathFormat = "path",
                linesStartAt1 = true,
                columnsStartAt1 = true,
                supportsRunInTerminalRequest = false,
                supportsVariableType = true,
                supportsVariablePaging = true,
                supportsInvalidatedEvent = true,
                supportsProgressReporting = true,
            },
            initializeTimeout,
            cancellationToken).ConfigureAwait(false);

        await _initialized.Task.WaitAsync(initializeTimeout, cancellationToken).ConfigureAwait(false);
        await ConfigureBreakpointsAsync(breakpoints, cancellationToken).ConfigureAwait(false);

        var debuggeeProgram = programPath;
        var debuggeeArguments = launchOptions?.Arguments;
        if (launchOptions?.DotnetHostPath is { } dotnetHost)
        {
            debuggeeProgram = dotnetHost;
            debuggeeArguments = new string[(launchOptions.Arguments?.Length ?? 0) + 1];
            debuggeeArguments[0] = programPath;
            launchOptions.Arguments?.CopyTo(debuggeeArguments, 1);
        }
        var launchArguments = new DapLaunchArguments(debuggeeProgram,
            launchEnvironment is { Count: > 0 } ? launchEnvironment : null,
            launchOptions?.WorkingDirectory, debuggeeArguments, launchOptions?.StopAtEntry, launchOptions?.JustMyCode);
        var launch = await BeginRequestAsync(
            "launch",
            launchArguments,
            _requestTimeout,
            cancellationToken).ConfigureAwait(false);
        try
        {
            if (_supportsConfigurationDone)
            {
                _ = await SendRequestAsync(
                    "configurationDone",
                    arguments: null,
                    _requestTimeout,
                    cancellationToken).ConfigureAwait(false);
            }

            var response = await WaitForResponseAsync(launch, _requestTimeout, cancellationToken).ConfigureAwait(false);
            if (!response.Success)
            {
                throw new DapLaunchRefusedException();
            }
        }
        finally
        {
            RemovePending(launch);
        }
    }

    private async Task ConfigureBreakpointsAsync(
        IReadOnlyList<DapSourceBreakpoint> breakpoints,
        CancellationToken cancellationToken)
    {
        if (breakpoints.Count == 0)
        {
            return;
        }

        using var deadline = CreateDeadline(_requestTimeout, cancellationToken);
        try
        {
            var files = new Dictionary<string, List<DapSourceBreakpoint>>(
                OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal);
            foreach (var breakpoint in breakpoints)
            {
                if (!files.TryGetValue(breakpoint.File, out var fileBreakpoints))
                {
                    fileBreakpoints = [];
                    files.Add(breakpoint.File, fileBreakpoints);
                }

                fileBreakpoints.Add(breakpoint);
            }

            var initial = new DapInitialBreakpoint[breakpoints.Count];
            var desired = new Dictionary<string, DapRequestedBreakpoint[]>(_requestedBreakpoints.Comparer);
            foreach (var (file, fileBreakpoints) in files)
            {
                deadline.Token.ThrowIfCancellationRequested();
                var requested = new DapRequestedBreakpoint[fileBreakpoints.Count];
                for (var index = 0; index < requested.Length; index++)
                {
                    requested[index] = new DapRequestedBreakpoint(fileBreakpoints[index].Line, fileBreakpoints[index].Incarnation);
                }

                var response = await SendRequestAcceptingRefusalAsync(
                    "setBreakpoints",
                    new DapSetBreakpointsArguments(new DapBreakpointSource(file), requested),
                    _requestTimeout,
                    deadline.Token, new InitialBreakpointRequest(file, requested)).ConfigureAwait(false);
                if (!response.Success)
                {
                    throw new DapBreakpointsException(isRefused: true);
                }

                var results = RequireBreakpointResults(response.Body, requested.Length);

                var resultIndex = 0;
                foreach (var result in results.EnumerateArray())
                {
                    var observed = ReadBreakpointObservation(result);
                    var originalIndex = fileBreakpoints[resultIndex++].Index;
                    initial[originalIndex] = new DapInitialBreakpoint(originalIndex, observed.Verified, observed.ActualLine);
                }
                desired.Add(file, requested);
            }

            deadline.Token.ThrowIfCancellationRequested();
            lock (_stateGate)
            {
                foreach (var (file, requested) in desired)
                {
                    _requestedBreakpoints.Add(file, requested);
                }

                _requestedBreakpointCount = breakpoints.Count;
                InitialBreakpoints = initial;
            }
        }
        catch (DapBreakpointsException)
        {
            throw;
        }
        catch (Exception) when (!cancellationToken.IsCancellationRequested)
        {
            throw new DapBreakpointsException(isRefused: false);
        }
    }

    private static JsonElement RequireBreakpointResults(JsonElement body, int expectedCount)
    {
        if (body.ValueKind != JsonValueKind.Object
            || !body.TryGetProperty("breakpoints", out var results)
            || results.ValueKind != JsonValueKind.Array
            || results.GetArrayLength() != expectedCount)
        {
            throw new InvalidDataException("DAP setBreakpoints result count is invalid.");
        }

        return results;
    }

    private static DapBreakpointObservation ReadBreakpointObservation(JsonElement result)
    {
        if (result.ValueKind != JsonValueKind.Object
            || !result.TryGetProperty("verified", out var verified)
            || verified.ValueKind is not JsonValueKind.True and not JsonValueKind.False)
        {
            throw new InvalidDataException("DAP breakpoint verification must be a boolean.");
        }

        int? actualLine = null;
        if (result.TryGetProperty("line", out _))
        {
            actualLine = RequireInt32(result, "line");
            if (actualLine <= 0)
            {
                throw new InvalidDataException("DAP breakpoint line must be positive.");
            }
        }

        return new DapBreakpointObservation(verified.GetBoolean(), actualLine);
    }

    private void ObserveLegacyBreakpointResponse(string file, DapRequestedBreakpoint[] requested, JsonElement body)
    {
        var results = RequireBreakpointResults(body, requested.Length);
        var next = new List<LegacyBreakpointObservation>(requested.Length);
        var index = 0;
        foreach (var result in results.EnumerateArray())
        {
            var observation = ReadBreakpointObservation(result);
            var line = requested[index++].Line;
            var incarnation = requested[index - 1].Incarnation;
            var count = GetLegacyHitCount(file, line);
            var id = TryGetInt32(result, "id");
            if (id is not { } observedId) _legacyBreakpointEventsIncomplete = true;
            else if (_legacyBreakpointIds.TryGetValue(observedId, out var owner))
            {
                if (owner != incarnation) _legacyBreakpointEventsIncomplete = true;
            }
            else if (_legacyBreakpointIds.Count >= 4096) _legacyBreakpointEventsIncomplete = true;
            else _legacyBreakpointIds[observedId] = incarnation;
            next.Add(new LegacyBreakpointObservation(file, line, incarnation, observation.Verified,
                observation.ActualLine == line ? null : observation.ActualLine, id, count, false));
        }
        if (next.Count == 0) _legacyBreakpointObservations.Remove(file);
        else _legacyBreakpointObservations[file] = next;
        try { _resourceEventObserver?.Invoke("breakpoint"); } catch (Exception) { }
    }

    private void ObserveLegacyBreakpointEvent(JsonElement body)
    {
        if (body.ValueKind != JsonValueKind.Object || !body.TryGetProperty("breakpoint", out var value) || value.ValueKind != JsonValueKind.Object
            || TryGetInt32(value, "id") is not { } id || TryGetString(body, "reason") is not { } reason)
            return;
        lock (_stateGate)
        {
            if (_legacyBreakpointEventsIncomplete) return;
            foreach (var observations in _legacyBreakpointObservations.Values)
            {
                for (var index = 0; index < observations.Count; index++)
                {
                    var previous = observations[index];
                    if (previous.Id != id || previous.Removed) continue;
                    LegacyBreakpointObservation updated;
                    if (reason == "removed") updated = previous with { Removed = true };
                    else if (reason is "changed" or "new")
                    {
                        if (!value.TryGetProperty("verified", out var verified) || verified.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return;
                        var line = TryGetInt32(value, "line");
                        updated = previous with { Verified = verified.GetBoolean(), DapLine = line is null ? previous.DapLine : line == previous.Line ? null : line };
                    }
                    else return;
                    if (updated != previous)
                    {
                        if (updated.Removed) _legacyRemovedBreakpoints[updated.Incarnation] = updated;
                        observations[index] = updated;
                        try { _resourceEventObserver?.Invoke("breakpoint"); } catch (Exception) { }
                    }
                    return;
                }
            }
        }
    }

    internal LegacyBreakpointObservation[] CaptureLegacyBreakpoints()
    {
        lock (_stateGate)
        {
            var result = _legacyRemovedBreakpoints.Values.Concat(_legacyBreakpointObservations.Values.SelectMany(static value => value)).ToArray();
            _legacyRemovedBreakpoints.Clear();
            return result;
        }
    }

    internal long GetLegacyHitCount(string file, int line)
    {
        lock (_stateGate)
            return _legacyHitCounts.TryGetValue(file, out var counts) && counts.TryGetValue(line, out var count) ? count : 0;
    }

    internal T WithLegacyBreakpointLock<T>(Func<T> action)
    {
        lock (_stateGate) return action();
    }

    internal bool LegacyBreakpointEventsComplete { get { lock (_stateGate) return !_legacyBreakpointEventsIncomplete; } }

    internal static bool TryNormalizeLegacyBreakpointSource(string file, out string normalized, bool requireExistingFile)
    {
        normalized = string.Empty;
        try
        {
            var fullPath = Path.GetFullPath(file, Environment.CurrentDirectory);
            if (requireExistingFile && !File.Exists(fullPath)) return false;
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

    private void ScheduleLegacyBreakpointHit(int threadId, string context, LegacyBreakpointObservation[] captured)
    {
        if (Interlocked.CompareExchange(ref _legacyHitObservationActive, 1, 0) != 0)
        {
            lock (_stateGate) _legacyHitCountsIncomplete = true;
            return;
        }
        _ = Task.Run(async () =>
        {
            try
            {
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(3));
                var stack = await GetCallStackAsync(threadId, 0, 1, true, timeout.Token).ConfigureAwait(false);
                if (stack.IsRefused || stack.Frames.Count == 0 || stack.Frames[0].Source is not { } frameSource
                    || stack.Frames[0].Line <= 0 || !TryNormalizeLegacyBreakpointSource(frameSource, out var source, requireExistingFile: false))
                {
                    lock (_stateGate) _legacyHitCountsIncomplete = true;
                    return;
                }
                var frame = stack.Frames[0];
                lock (_stateGate)
                {
                    if (_inspectionClosed || _state.Event != "stopped" || _state.CurrentThreadId != threadId
                        || !StringComparer.Ordinal.Equals(_inspectionContextId, context))
                    {
                        _legacyHitCountsIncomplete = true;
                        return;
                    }
                    foreach (var item in captured)
                    {
                        if (!item.Removed && _requestedBreakpoints.Comparer.Equals(item.File, source) && (item.Line == frame.Line || item.DapLine == frame.Line))
                        {
                            if (!_legacyHitCounts.TryGetValue(source, out var counts))
                            {
                                if (_legacyHitCountLocations >= MaximumBreakpointCount) { _legacyHitCountsIncomplete = true; return; }
                                _legacyHitCounts[source] = counts = [];
                            }
                            if (!counts.TryGetValue(item.Line, out var count))
                            {
                                if (_legacyHitCountLocations >= MaximumBreakpointCount) { _legacyHitCountsIncomplete = true; return; }
                                _legacyHitCountLocations++;
                            }
                            counts[item.Line] = checked(count + 1);
                            if (_legacyBreakpointObservations.TryGetValue(source, out var current))
                            {
                                var index = current.FindIndex(entry => entry.Incarnation == item.Incarnation);
                                if (index >= 0) current[index] = current[index] with { HitCount = counts[item.Line] };
                            }
                            return;
                        }
                    }
                }
            }
            catch (Exception) { lock (_stateGate) _legacyHitCountsIncomplete = true; }
            finally { Volatile.Write(ref _legacyHitObservationActive, 0); }
        });
    }

    internal bool LegacyHitCountsComplete { get { lock (_stateGate) return !_legacyHitCountsIncomplete && Volatile.Read(ref _legacyHitObservationActive) == 0; } }

    internal Task<DapBreakpointUpdateResult> AddBreakpointAsync(string file, int line, CancellationToken cancellationToken) =>
        UpdateBreakpointAsync(file, line, remove: false, cancellationToken);

    internal Task<DapBreakpointUpdateResult> RemoveBreakpointAsync(string file, int line, CancellationToken cancellationToken) =>
        UpdateBreakpointAsync(file, line, remove: true, cancellationToken);

    internal Task<DapBreakpointUpdateResult> ReplaceBreakpointsAsync(string file, int line, bool remove, LegacyRequestedLine[] requestedLines, CancellationToken cancellationToken) =>
        UpdateBreakpointAsync(file, line, remove, cancellationToken, requestedLines);

    internal async Task<DapBreakpointUpdateResult> ConfigureExceptionsAsync(string[] filters, CancellationToken cancellationToken)
    {
        await _breakpointUpdateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        BreakpointUpdate? update = null;
        PendingRequest? request = null;
        try
        {
            using var deadline = CreateDeadline(_requestTimeout, cancellationToken);
            await _callStackAdmissionGate.WaitAsync(deadline.Token).ConfigureAwait(false);
            try
            {
                lock (_stateGate)
                {
                    if (_breakpointUpdatesClosed || _inspectionClosed || IsRetirementRequested || _executionAdmission == ExecutionAdmission.Closing)
                    {
                        return DapBreakpointUpdateResult.Refused;
                    }
                    if (!_supportsExceptionFilters)
                    {
                        return new DapBreakpointUpdateResult(DapBreakpointUpdateOutcome.Unsupported);
                    }
                    update = new BreakpointUpdate(string.Empty, [], -1, 0) { ExceptionFilters = filters };
                    _activeBreakpointUpdate = update;
                }
                request = await BeginRequestAsync("setExceptionBreakpoints", new { filters }, _requestTimeout,
                    deadline.Token, breakpointUpdate: update).ConfigureAwait(false);
            }
            finally
            {
                _callStackAdmissionGate.Release();
            }

            await update.Completion.Task.WaitAsync(deadline.Token).ConfigureAwait(false);
            lock (_stateGate)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (update.Failure is { } failure)
                {
                    throw new InvalidDataException("DAP exception filter update failed.", failure);
                }
                var result = update.Result ?? throw new InvalidOperationException("Exception filter update has no result.");
                update.Finalized = true;
                if (update.RequiresDrain)
                {
                    FenceBreakpointUpdates();
                }
                return result with { RequiresDrain = update.RequiresDrain };
            }
        }
        catch (OperationCanceledException exception) when (cancellationToken.IsCancellationRequested)
        {
            bool requiresDrain;
            lock (_stateGate)
            {
                var outcomeKnown = update is { CommittedWrite: true, Result: not null, Failure: null, RequiresDrain: false };
                requiresDrain = update is not null && (update.RequiresDrain || (update.WriteStarted && !outcomeKnown));
                if (update is not null)
                {
                    update.Finalized = true;
                }
                if (requiresDrain)
                {
                    FenceBreakpointUpdates();
                }
            }
            throw new DapBreakpointCanceledException(requiresDrain, exception, cancellationToken);
        }
        catch
        {
            lock (_stateGate)
            {
                if (update is not null)
                {
                    update.Finalized = true;
                }
                FenceBreakpointUpdates();
            }
            throw;
        }
        finally
        {
            if (request is not null)
            {
                RemovePending(request);
            }
            lock (_stateGate)
            {
                if (ReferenceEquals(_activeBreakpointUpdate, update))
                {
                    _activeBreakpointUpdate = null;
                }
            }
            _breakpointUpdateGate.Release();
        }
    }

    private async Task<DapBreakpointUpdateResult> UpdateBreakpointAsync(string file, int line, bool remove, CancellationToken cancellationToken, LegacyRequestedLine[]? requestedLines = null)
    {
        await _breakpointUpdateGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        BreakpointUpdate? update = null;
        PendingRequest? request = null;
        try
        {
            using var deadline = CreateDeadline(_requestTimeout, cancellationToken);
            await _callStackAdmissionGate.WaitAsync(deadline.Token).ConfigureAwait(false);
            try
            {
                lock (_stateGate)
                {
                    if (_breakpointUpdatesClosed || _inspectionClosed || _executionAdmission == ExecutionAdmission.Closing)
                    {
                        return requestedLines is null ? DapBreakpointUpdateResult.Refused : DapBreakpointUpdateResult.Unavailable;
                    }

                    var previous = _requestedBreakpoints.TryGetValue(file, out var existing)
                        ? existing
                        : Array.Empty<DapRequestedBreakpoint>();
                    var targetIndex = -1;
                    var requested = previous;
                    if (requestedLines is not null)
                    {
                        if (_requestedBreakpointCount - previous.Length + requestedLines.Length > MaximumBreakpointCount)
                            return DapBreakpointUpdateResult.LimitReached;
                        requested = new DapRequestedBreakpoint[requestedLines.Length];
                        for (var index = 0; index < requestedLines.Length; index++)
                        {
                            requested[index] = new DapRequestedBreakpoint(requestedLines[index].Line, requestedLines[index].Incarnation);
                            if (!remove && requestedLines[index].Line == line) targetIndex = index;
                        }
                    }
                    else if (remove)
                    {
                        var remainingCount = 0;
                        foreach (var breakpoint in previous)
                        {
                            if (breakpoint.Line != line)
                            {
                                remainingCount++;
                            }
                        }

                        if (remainingCount == previous.Length)
                        {
                            return DapBreakpointUpdateResult.NotFound;
                        }

                        if (remainingCount == 0)
                        {
                            requested = Array.Empty<DapRequestedBreakpoint>();
                        }
                        else
                        {
                            requested = new DapRequestedBreakpoint[remainingCount];
                            var nextIndex = 0;
                            foreach (var breakpoint in previous)
                            {
                                if (breakpoint.Line != line)
                                {
                                    requested[nextIndex++] = breakpoint;
                                }
                            }
                        }
                    }
                    else
                    {
                        for (var index = 0; index < previous.Length; index++)
                        {
                            if (previous[index].Line == line)
                            {
                                targetIndex = index;
                                break;
                            }
                        }

                        if (targetIndex < 0)
                        {
                            if (_requestedBreakpointCount >= MaximumBreakpointCount)
                            {
                                return DapBreakpointUpdateResult.LimitReached;
                            }

                            targetIndex = previous.Length;
                            requested = new DapRequestedBreakpoint[previous.Length + 1];
                            Array.Copy(previous, requested, previous.Length);
                            requested[targetIndex] = new DapRequestedBreakpoint(line);
                        }
                    }

                    update = new BreakpointUpdate(file, requested, targetIndex, requested.Length - previous.Length, remove);
                    _activeBreakpointUpdate = update;
                }

                request = await BeginRequestAsync(
                    "setBreakpoints",
                    new DapSetBreakpointsArguments(new DapBreakpointSource(file), update.Requested),
                    _requestTimeout,
                    deadline.Token,
                    breakpointUpdate: update).ConfigureAwait(false);
            }
            finally
            {
                _callStackAdmissionGate.Release();
            }

            await update.Completion.Task.WaitAsync(deadline.Token).ConfigureAwait(false);
            lock (_stateGate)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (update.Failure is { } failure)
                {
                    throw new InvalidDataException("DAP breakpoint update failed.", failure);
                }

                var result = update.Result ?? throw new InvalidOperationException("Breakpoint update has no result.");
                update.Finalized = true;
                if (update.RequiresDrain)
                {
                    FenceBreakpointUpdates();
                }

                return result with { RequiresDrain = update.RequiresDrain };
            }
        }
        catch (OperationCanceledException exception) when (cancellationToken.IsCancellationRequested)
        {
            bool requiresDrain;
            lock (_stateGate)
            {
                var outcomeKnown = update is
                {
                    CommittedWrite: true,
                    Result: not null,
                    Failure: null,
                    RequiresDrain: false,
                };
                requiresDrain = update is not null
                    && (update.RequiresDrain || (update.WriteStarted && !outcomeKnown));
                if (update is not null)
                {
                    update.Finalized = true;
                }

                if (requiresDrain)
                {
                    FenceBreakpointUpdates();
                }
            }

            throw new DapBreakpointCanceledException(requiresDrain, exception, cancellationToken);
        }
        catch
        {
            lock (_stateGate)
            {
                if (update is not null)
                {
                    update.Finalized = true;
                }

                FenceBreakpointUpdates();
            }

            throw;
        }
        finally
        {
            if (request is not null)
            {
                RemovePending(request);
            }

            lock (_stateGate)
            {
                if (ReferenceEquals(_activeBreakpointUpdate, update))
                {
                    _activeBreakpointUpdate = null;
                }
            }

            _breakpointUpdateGate.Release();
        }
    }

    private void FenceBreakpointUpdates()
    {
        _breakpointUpdatesClosed = true;
        _inspectionClosed = true;
        _executionAdmission = ExecutionAdmission.Closing;
        _inspectionContextId = null;
        ClearCallStackEligibility();
    }

    private async Task<DapResponse> SendRequestAsync(
        string command,
        object? arguments,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        var request = await BeginRequestAsync(command, arguments, timeout, cancellationToken).ConfigureAwait(false);
        try
        {
            var response = await WaitForResponseAsync(request, timeout, cancellationToken).ConfigureAwait(false);
            if (!response.Success)
            {
                throw new InvalidDataException($"DAP request '{command}' failed or returned an invalid success flag.");
            }

            if (string.Equals(command, "initialize", StringComparison.Ordinal)
                && _initializeResponseContinuationReached is { } continuationReached)
            {
                continuationReached.TrySetResult(true);
                await (_initializeResponseContinuationRelease ?? Task.CompletedTask).ConfigureAwait(false);
            }

            return response;
        }
        finally
        {
            RemovePending(request);
        }
    }

    private async Task<DapResponse> SendRequestAcceptingRefusalAsync(
        string command,
        object? arguments,
        TimeSpan timeout,
        CancellationToken cancellationToken,
        InitialBreakpointRequest? initialBreakpoints = null)
    {
        var request = await BeginRequestAsync(command, arguments, timeout, cancellationToken, initialBreakpoints: initialBreakpoints).ConfigureAwait(false);
        try
        {
            return await WaitForResponseAsync(request, timeout, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            RemovePending(request);
        }
    }
    private async Task<(DapResponse? Response, bool IsPaged)> SendStackTraceRequestAsync(
        int? threadId,
        uint startFrame,
        uint levels,
        bool includeInspectionContext,
        CancellationToken cancellationToken)
    {
        PendingRequest? request = null;
        bool isPaged;
        await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            var target = CaptureOptionalCallStackTarget(threadId, includeInspectionContext);
            if (target is null)
            {
                return (null, false);
            }

            isPaged = target.Value.SupportsPaging;
            var selectedThreadId = target.Value.ThreadId;
            object arguments = isPaged ? new { threadId = selectedThreadId, startFrame, levels } : new { threadId = selectedThreadId };
            request = await BeginRequestAsync(
                "stackTrace",
                arguments,
                _requestTimeout,
                cancellationToken,
                target).ConfigureAwait(false);
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }

        try
        {
            var response = await WaitForResponseAsync(request, _requestTimeout, cancellationToken).ConfigureAwait(false);
            return (response, isPaged);
        }
        finally
        {
            RemovePending(request);
        }
    }

    private async Task<DapResponse?> SendInspectionRequestAsync(
        string command,
        object arguments,
        string inspectionContextId,
        CancellationToken cancellationToken)
    {
        PendingRequest request;
        await _callStackAdmissionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            lock (_stateGate)
            {
                if (_inspectionClosed || !StringComparer.Ordinal.Equals(_inspectionContextId, inspectionContextId))
                {
                    return null;
                }
            }

            request = await BeginRequestAsync(command, arguments, _requestTimeout, cancellationToken,
                inspectionContextId: inspectionContextId).ConfigureAwait(false);
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }

        try
        {
            return await WaitForResponseAsync(request, _requestTimeout, cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (command == "evaluate" && request.WriteStarted)
        {
            // A committed evaluate can still be executing after its caller stops waiting.
            Interlocked.Exchange(ref _inspectionWriteDrainRequired, 1);
            InvalidateInspectionContext(close: true);
            throw;
        }
        finally
        {
            RemovePending(request);
        }
    }

    private async Task<PendingRequest> BeginRequestAsync(
        string command,
        object? arguments,
        TimeSpan timeout,
        CancellationToken cancellationToken,
        CapturedCallStackTarget? stackTraceTarget = null,
        StepOverRecord? stepOver = null,
        string? inspectionContextId = null,
        ContinueRecord? continueExecution = null,
        BreakpointUpdate? breakpointUpdate = null,
        PauseRecord? pause = null,
        bool trackLoadedSourcesWrite = false,
        InitialBreakpointRequest? initialBreakpoints = null)
    {
        if (Volatile.Read(ref _readerFailure) is { } readerFailure)
        {
            throw new IOException("The debugger DAP reader is no longer available.", readerFailure);
        }

        var sequence = Interlocked.Increment(ref _outgoingSequence);
        stepOver?.SetRequestSequence(sequence);
        continueExecution?.SetRequestSequence(sequence);
        breakpointUpdate?.SetRequestSequence(sequence);
        var request = new PendingRequest(sequence, command, stackTraceTarget, stepOver,
            inspectionContextId ?? stackTraceTarget?.InspectionContextId, continueExecution, breakpointUpdate, pause, initialBreakpoints);
        if (pause is not null)
        {
            lock (_stateGate)
            {
                pause.Request = request;
            }
        }
        if (!_pending.TryAdd(sequence, request))
        {
            throw new InvalidOperationException($"DAP request sequence '{sequence}' was reused.");
        }

        try
        {
            using var deadline = stepOver is null && continueExecution is null && breakpointUpdate is null && pause is null
                ? CreateDeadline(timeout, cancellationToken) : null;
            await WriteMessageAsync(new
            {
                seq = sequence,
                type = "request",
                command,
                arguments,
            }, deadline?.Token ?? cancellationToken, stepOver,
                request.InspectionContextId is null && pause is null && !trackLoadedSourcesWrite ? null : request, continueExecution, breakpointUpdate).ConfigureAwait(false);
            return request;
        }
        catch (Exception exception)
        {
            if (request.InspectionContextId is not null && request.WriteStarted
                && (exception is OperationCanceledException || request.Command == "setVariable"))
            {
                Interlocked.Exchange(ref _inspectionWriteDrainRequired, 1);
                lock (_stateGate)
                {
                    _inspectionContextId = null;
                    _inspectionClosed = true;
                }
            }

            RemovePending(request);
            request.Completion.TrySetException(exception);
            if (pause is not null)
            {
                _ = request.Completion.Task.Exception;
            }
            throw;
        }
    }

    private static async Task<DapResponse> WaitForResponseAsync(
        PendingRequest request,
        TimeSpan timeout,
        CancellationToken cancellationToken) =>
        await request.Completion.Task.WaitAsync(timeout, cancellationToken).ConfigureAwait(false);
    private static async Task<DapStepOverResult> WaitForStepOverOutcomeAsync(
        PendingRequest request,
        StepOverRecord active,
        CancellationToken cancellationToken)
    {
        var first = await Task.WhenAny(request.Completion.Task, active.Completion.Task)
            .WaitAsync(cancellationToken).ConfigureAwait(false);
        if (ReferenceEquals(first, request.Completion.Task))
        {
            _ = await request.Completion.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        }

        return await active.Completion.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
    }

    private async Task<DapContinueResult> WaitForContinueOutcomeAsync(
        ContinueRecord active,
        CancellationToken deadlineToken,
        CancellationToken callerCancellation)
    {
        while (true)
        {
            Task<bool> signal;
            lock (_stateGate)
            {
                callerCancellation.ThrowIfCancellationRequested();
                if (active.ProtocolFailure is { } failure)
                {
                    throw new InvalidDataException("DAP continue exchange failed.", failure);
                }

                // A wake-up does not prove that its stopped observation is still current.
                if (active.Outcome is not null)
                {
                    return FinalizeContinue(active, interrupted: false);
                }

                signal = active.Completion.Task;
            }

            await signal.WaitAsync(deadlineToken).ConfigureAwait(false);
        }
    }


    private async Task WriteMessageAsync(
        object message,
        CancellationToken cancellationToken,
        StepOverRecord? stepOver = null,
        PendingRequest? inspectionRequest = null,
        ContinueRecord? continueExecution = null,
        BreakpointUpdate? breakpointUpdate = null)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(message, JsonOptions);
        var header = HeaderEncoding.GetBytes($"Content-Length: {payload.Length}\r\n\r\n");
        await _writeGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (Volatile.Read(ref _loadedSourcesWriteDrainRequired) != 0)
            {
                throw new IOException("The DAP stream has an incomplete loadedSources request.");
            }
            await (_preHeaderWriteGate?.Invoke(cancellationToken) ?? Task.CompletedTask).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            if (inspectionRequest?.Pause is { } pause)
            {
                lock (_stateGate)
                {
                    if (!CanWritePause(pause))
                    {
                        throw new InvalidDataException("DAP pause lost write admission.");
                    }
                }
            }
            stepOver?.MarkWriteStarted();
            continueExecution?.MarkWriteStarted();
            if (inspectionRequest is { Command: "setVariable" } mutation)
            {
                lock (_stateGate)
                {
                    if (Volatile.Read(ref _readerFailure) is not null || IsRetirementRequested || _inspectionClosed
                        || !StringComparer.Ordinal.Equals(_inspectionContextId, mutation.InspectionContextId))
                    {
                        throw new InvalidDataException("DAP variable update lost write admission.");
                    }
                    mutation.MarkWriteStarted();
                }
            }
            else
            {
                inspectionRequest?.MarkWriteStarted();
            }
            if (breakpointUpdate?.ExceptionFilters is not null)
            {
                lock (_stateGate)
                {
                    if (IsRetirementRequested || _breakpointUpdatesClosed || _inspectionClosed || _executionAdmission == ExecutionAdmission.Closing)
                    {
                        throw new InvalidDataException("DAP exception filter update lost write admission.");
                    }
                    breakpointUpdate.MarkWriteStarted();
                }
            }
            else
            {
                breakpointUpdate?.MarkWriteStarted();
            }
            await _input.WriteAsync(header, cancellationToken).ConfigureAwait(false);
            await _input.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
            await _input.FlushAsync(cancellationToken).ConfigureAwait(false);
            inspectionRequest?.CommitWrite();
            breakpointUpdate?.CommitWrite();
            stepOver?.Commit(_requestTimeout, _stepOverTimeProvider);
            continueExecution?.Commit(_stepOverTimeProvider);
        }
        catch when (inspectionRequest is { Command: "loadedSources", WriteStarted: true, CommittedWrite: false })
        {
            // Fence queued writers before releasing the gate after an incomplete frame.
            Interlocked.Exchange(ref _loadedSourcesWriteDrainRequired, 1);
            throw;
        }
        finally
        {
            _writeGate.Release();
        }
    }

    private async Task ReadMessagesAsync()
    {
        Exception? failure = null;
        try
        {
            while (await ReadFrameAsync(CancellationToken.None).ConfigureAwait(false) is { } document)
            {
                using (document)
                {
                    DispatchMessage(document.RootElement);
                }
            }

            failure = new EndOfStreamException("The debugger closed its DAP output stream.");
        }
        catch (Exception exception)
        {
            failure = exception;
        }
        finally
        {
            if (failure is not null)
            {
                lock (_stateGate)
                {
                    Volatile.Write(ref _readerFailure, failure);
                    _inspectionContextId = null;
                    _inspectionClosed = true;
                }
                _initialized.TrySetException(failure);
                FailPending(failure);
                lock (_stateGate)
                {
                    if (_activePause is { Finalized: false, Outcome: null } pause)
                    {
                        pause.ObserveFailure(failure);
                        FencePause();
                    }
                }
                try
                {
                    ReaderFailed?.Invoke(this);
                }
                catch (Exception)
                {
                }
                RequestRetirement();
                try { _resourceEventObserver?.Invoke("state"); } catch (Exception) { }
            }
            InvalidateInspectionContext(close: true);
        }
    }

    private void DispatchMessage(JsonElement message)
    {
        if (message.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException("DAP message must be a JSON object.");
        }

        var type = RequireString(message, "type");
        switch (type)
        {
            case "response":
                if (message.TryGetProperty("request_seq", out var requestSequence)
                    && requestSequence.TryGetInt32(out var sequence)
                    && _pending.TryGetValue(sequence, out var pending)
                    && pending.Command == "setVariable")
                {
                    _callStackAdmissionGate.Wait();
                    try { HandleResponse(message); }
                    finally { _callStackAdmissionGate.Release(); }
                }
                else
                {
                    HandleResponse(message);
                }
                break;
            case "event":
                HandleEvent(message);
                break;
        }
    }

    private bool FailActiveStepOverForWrongCorrelation(int requestSequence, string? command)
    {
        if (command is not ("next" or "stepIn" or "stepOut"))
        {
            return false;
        }

        lock (_stateGate)
        {
            if (_activeStepOver is not { } active || active.RequestSequence == requestSequence
                || !string.Equals(command, active.Command, StringComparison.Ordinal))
            {
                return false;
            }

            active.Completion.TrySetException(new InvalidDataException(
                $"DAP {active.Command} response for request '{requestSequence}' did not match active request '{active.RequestSequence}'."));
            return true;
        }
    }

    private bool FailActiveContinueForWrongCorrelation(int requestSequence, string? command)
    {
        if (!string.Equals(command, "continue", StringComparison.Ordinal))
        {
            return false;
        }

        lock (_stateGate)
        {
            if (_activeContinue is not { Finalized: false } active || active.RequestSequence == requestSequence)
            {
                return false;
            }

            active.ObserveProtocolFailure(new InvalidDataException("DAP continue response correlation is invalid."));
            return true;
        }
    }

    private bool FailActiveBreakpointForWrongCorrelation(int requestSequence, string? command)
    {
        if (command is not ("setBreakpoints" or "setExceptionBreakpoints"))
        {
            return false;
        }

        lock (_stateGate)
        {
            if (_activeBreakpointUpdate is not { Finalized: false } update || update.RequestSequence == requestSequence
                || (update.ExceptionFilters is null) != (command == "setBreakpoints"))
            {
                return false;
            }

            update.ObserveFailure(new InvalidDataException("DAP setBreakpoints response correlation is invalid."));
            FenceBreakpointUpdates();
            return true;
        }
    }

    private void HandleResponse(JsonElement message)
    {
        var requestSequence = RequireInt32(message, "request_seq");
        var pauseOwned = _pending.TryGetValue(requestSequence, out var request) && request.Pause is not null;
        if (!pauseOwned && (FailActiveStepOverForWrongCorrelation(requestSequence, TryGetString(message, "command"))
            || FailActiveContinueForWrongCorrelation(requestSequence, TryGetString(message, "command"))
            || FailActiveBreakpointForWrongCorrelation(requestSequence, TryGetString(message, "command"))))
        {
            return;
        }

        if (request is null)
        {
            return;
        }

        var command = RequireString(message, "command");
        if (!string.Equals(command, request.Command, StringComparison.Ordinal))
        {
            FailRequest(request, new InvalidDataException(
                $"DAP response for request '{requestSequence}' named '{command}' instead of '{request.Command}'."));
            return;
        }

        if (!message.TryGetProperty("success", out var success)
            || (success.ValueKind != JsonValueKind.True && success.ValueKind != JsonValueKind.False))
        {
            FailRequest(request, new InvalidDataException(
                $"DAP request '{request.Command}' failed or returned an invalid success flag."));
            return;
        }

        var body = (request.Pause is null || request.Command != "pause")
            && message.TryGetProperty("body", out var bodyElement)
            ? bodyElement.Clone()
            : default;
        if (success.ValueKind == JsonValueKind.True && request.Command == "initialize")
        {
            ReadInitializeCapabilities(body);
            _initializeResponseObserved = true;
        }
        if (success.ValueKind == JsonValueKind.True && request.Command == "loadedSources")
        {
            RefreshLoadedSources(body);
            try { _resourceEventObserver?.Invoke("state"); } catch (Exception) { }
        }
        if (_resourceEventObserver is not null && success.ValueKind == JsonValueKind.True && request.Command == "threads")
        {
            try
            {
                var array = RequireThreads(body);
                var threads = new DapThread[array.GetArrayLength()];
                ReadThreads(array, threads);
                lock (_stateGate) _legacyThreads = threads;
                _resourceEventObserver("state");
            }
            catch (Exception exception) when (exception is InvalidDataException or InvalidOperationException or JsonException or ArgumentException)
            {
                lock (_stateGate) _legacyThreads = null;
            }
        }
        if (_resourceEventObserver is not null && success.ValueKind == JsonValueKind.True && request.InitialBreakpoints is { } initial)
        {
            lock (_stateGate) ObserveLegacyBreakpointResponse(initial.File, initial.Requested, body);
        }

        if (request.StepOver is { } stepOver)
        {
            lock (_stateGate)
            {
                if (ReferenceEquals(_activeStepOver, stepOver))
                {
                    if (RequireInt32(message, "seq") <= 0)
                    {
                        throw new InvalidDataException("DAP next response sequence must be positive.");
                    }

                    stepOver.ObserveAcknowledgement(success.ValueKind == JsonValueKind.True);
                }
            }
        }

        lock (_stateGate)
        {
            if (request.BreakpointUpdate is { Finalized: false, IsSettled: false } update
                && ReferenceEquals(_activeBreakpointUpdate, update))
            {
                if (RequireInt32(message, "seq") <= 0)
                {
                    throw new InvalidDataException("DAP setBreakpoints response sequence must be positive.");
                }

                if (success.ValueKind == JsonValueKind.False)
                {
                    update.Complete(DapBreakpointUpdateResult.Refused);
                }
                else if (update.ExceptionFilters is { } filters)
                {
                    var verified = ReadExceptionFilterResults(body, filters.Length);
                    update.Complete(new DapBreakpointUpdateResult(DapBreakpointUpdateOutcome.Acknowledged, ExceptionVerification: verified));
                }
                else
                {
                    if (_resourceEventObserver is not null) ObserveLegacyBreakpointResponse(update.File, update.Requested, body);
                    var results = RequireBreakpointResults(body, update.Requested.Length);
                    var selected = default(DapBreakpointObservation);
                    var index = 0;
                    foreach (var result in results.EnumerateArray())
                    {
                        var observed = ReadBreakpointObservation(result);
                        if (index++ == update.TargetIndex)
                        {
                            selected = observed;
                        }
                    }

                    if (update.Requested.Length == 0)
                    {
                        _requestedBreakpoints.Remove(update.File);
                    }
                    else
                    {
                        _requestedBreakpoints[update.File] = update.Requested;
                    }
                    _requestedBreakpointCount += update.CountDelta;

                    update.Complete(new DapBreakpointUpdateResult(
                        update.IsRemoval ? DapBreakpointUpdateOutcome.Removed : DapBreakpointUpdateOutcome.Added,
                        selected.Verified, selected.ActualLine));
                }
            }

            if (request.ContinueExecution is { Finalized: false } continued
                && ReferenceEquals(_activeContinue, continued))
            {
                var acknowledged = success.ValueKind == JsonValueKind.True;
                if (RequireInt32(message, "seq") <= 0
                    || (acknowledged && body.ValueKind != JsonValueKind.Undefined && body.ValueKind != JsonValueKind.Object))
                {
                    throw new InvalidDataException("DAP continue acknowledgement is invalid.");
                }

                if (acknowledged && body.ValueKind == JsonValueKind.Object)
                {
                    _ = ReadOptionalBoolean(body, "allThreadsContinued");
                }

                if (acknowledged && !continued.HasObservedLifecycle
                    && !_inspectionClosed && _executionAdmission != ExecutionAdmission.Closing)
                {
                    _state = _state with { Event = "continued", StopReason = null, CurrentThreadId = null };
                }

                continued.ObserveAcknowledgement(acknowledged);
            }

            if (request.Pause is { Finalized: false } pause && ReferenceEquals(_activePause, pause)
                && ReferenceEquals(pause.Request, request))
            {
                if (RequireInt32(message, "seq") <= 0)
                {
                    throw new InvalidDataException("DAP pause acknowledgement is invalid.");
                }

                if (request.Command == "pause")
                {
                    pause.ObserveAcknowledgement(success.ValueKind == JsonValueKind.True);
                }
            }

            var targetCurrent = (request.StackTraceTarget is not { } target || IsCallStackTargetCurrent(target))
                && (request.InspectionContextId is null
                    || (!_inspectionClosed && StringComparer.Ordinal.Equals(_inspectionContextId, request.InspectionContextId)));
            if (_resourceEventObserver is not null && request.Command == "exceptionInfo" && targetCurrent && success.ValueKind == JsonValueKind.True)
            {
                if (body.ValueKind == JsonValueKind.Object && Encoding.UTF8.GetByteCount(body.GetRawText()) <= 64 * 1024)
                    _legacyExceptionInfo = body.Clone();
                else _legacyEventDetailsIncomplete = true;
                try { _resourceEventObserver("state"); } catch (Exception) { }
            }
            var responseContext = request.InspectionContextId;
            string? serializedContent = null;
            if (request.Command == "setVariable")
            {
                try
                {
                    if (RequireInt32(message, "seq") <= 0)
                    {
                        throw new InvalidDataException("DAP setVariable response sequence must be positive.");
                    }
                    _inspectionContextId = null;
                    if (!targetCurrent)
                    {
                        _inspectionClosed = true;
                        Interlocked.Exchange(ref _inspectionWriteDrainRequired, 1);
                    }
                    else if (success.ValueKind == JsonValueKind.True)
                    {
                        var value = RequireString(body, "value");
                        var type = body.TryGetProperty("type", out var typeElement) && typeElement.ValueKind != JsonValueKind.Null
                            ? RequireString(body, "type") : null;
                        var reference = body.TryGetProperty("variablesReference", out _) ? RequireVariablesReference(body) : 0;
                        responseContext = GetOrCreateInspectionContext();
                        serializedContent = JsonSerializer.Serialize(new
                        {
                            kind = "set_variable_success",
                            inspectionContextId = responseContext,
                            value,
                            type,
                            variablesReference = reference,
                        });
                        if (Encoding.UTF8.GetByteCount(serializedContent) > 256 * 1024)
                        {
                            throw new InvalidDataException("DAP setVariable result exceeds the inspection response bound.");
                        }
                    }
                }
                catch
                {
                    _inspectionContextId = null;
                    _inspectionClosed = true;
                    Interlocked.Exchange(ref _inspectionWriteDrainRequired, 1);
                    throw;
                }
            }
            request.Completion.TrySetResult(new DapResponse(body, success.ValueKind == JsonValueKind.True,
                targetCurrent, responseContext, serializedContent));
        }
    }

    private void HandleEvent(JsonElement message)
    {
        var eventName = RequireString(message, "event");
        var body = message.TryGetProperty("body", out var bodyElement) && bodyElement.ValueKind == JsonValueKind.Object
            ? bodyElement
            : default;

        switch (eventName)
        {
            case "process":
                CaptureNativeSceneTargetIdentity(body);
                if (_resourceEventObserver is not null)
                {
                    lock (_stateGate)
                    {
                        _legacyProcessId = TryGetInt32(body, "systemProcessId");
                        var name = TryGetString(body, "name");
                        _legacyProcessName = name is not null && Encoding.UTF8.GetByteCount(name) <= 4096 ? name : null;
                    }
                }
                break;
            case "output":
                if (HandleOutputEvent(body))
                {
                    try { _resourceEventObserver?.Invoke("output"); }
                    catch (Exception) { }
                }
                break;
            case "breakpoint":
                if (_resourceEventObserver is not null) ObserveLegacyBreakpointEvent(body);
                break;
            case "loadedSource":
                HandleLoadedSourceEvent(body);
                break;
            case "module":
            case "progressStart":
            case "progressUpdate":
            case "progressEnd":
                HandleRuntimeInventoryEvent(eventName, body);
                break;
            case "initialized":
                if (_initializeResponseObserved)
                {
                    _initialized.TrySetResult(true);
                }
                break;
            case "capabilities":
                if (body.ValueKind != JsonValueKind.Object
                    || !body.TryGetProperty("capabilities", out var capabilities)
                    || capabilities.ValueKind != JsonValueKind.Object)
                {
                    throw new InvalidDataException("DAP capabilities event body.capabilities must be a JSON object.");
                }
                if (capabilities.TryGetProperty("supportsTerminateRequest", out _))
                {
                    _supportsTerminate = ReadOptionalBoolean(capabilities, "supportsTerminateRequest");
                }

                if (capabilities.TryGetProperty("supportsConfigurationDoneRequest", out _))
                {
                    _supportsConfigurationDone = ReadOptionalBoolean(capabilities, "supportsConfigurationDoneRequest");
                }
                if (capabilities.TryGetProperty("supportsSetVariable", out _))
                {
                    var supported = ReadOptionalBoolean(capabilities, "supportsSetVariable");
                    _callStackAdmissionGate.Wait();
                    try
                    {
                        lock (_stateGate)
                        {
                            _supportsSetVariable = supported;
                        }
                    }
                    finally
                    {
                        _callStackAdmissionGate.Release();
                    }
                }
                if (capabilities.TryGetProperty("supportsExceptionInfoRequest", out _))
                {
                    var supportsExceptionInfo = ReadOptionalBoolean(capabilities, "supportsExceptionInfoRequest");
                    _callStackAdmissionGate.Wait();
                    try
                    {
                        lock (_stateGate)
                        {
                            _supportsExceptionInfoRequest = supportsExceptionInfo;
                        }
                    }
                    finally
                    {
                        _callStackAdmissionGate.Release();
                    }
                }
                if (capabilities.TryGetProperty("supportsStepInTargetsRequest", out _))
                {
                    var supported = ReadOptionalBoolean(capabilities, "supportsStepInTargetsRequest");
                    _callStackAdmissionGate.Wait();
                    try { lock (_stateGate) { _supportsStepInTargetsRequest = supported; } }
                    finally { _callStackAdmissionGate.Release(); }
                }
                if (capabilities.TryGetProperty("supportsLoadedSourcesRequest", out _))
                {
                    var supported = ReadOptionalBoolean(capabilities, "supportsLoadedSourcesRequest");
                    _callStackAdmissionGate.Wait();
                    try
                    {
                        lock (_stateGate) { _supportsLoadedSourcesRequest = supported; }
                    }
                    finally
                    {
                        _callStackAdmissionGate.Release();
                    }
                }
                if (capabilities.TryGetProperty("exceptionBreakpointFilters", out _))
                {
                    var supportsFilters = ReadExceptionFilterSupport(capabilities);
                    _callStackAdmissionGate.Wait();
                    try
                    {
                        lock (_stateGate)
                        {
                            _supportsExceptionFilters = supportsFilters;
                        }
                    }
                    finally
                    {
                        _callStackAdmissionGate.Release();
                    }
                }

                CapabilitiesObserved = true;
                break;
            case "stopped":
                HandleStoppedEvent(message, body);
                break;
            case "continued":
                HandleContinuedEvent(body);
                break;
            case "invalidated":
                InvalidateInspectionContext();
                break;
            case "thread" when string.Equals(TryGetString(body, "reason"), "exited", StringComparison.Ordinal):
                InvalidateInspectionContext();
                break;
            case "exited":
            case "terminated":
                HandleTerminalEvent(message, eventName, body);
                break;
        }
        if (eventName is "thread" or "stopped" or "continued" or "exited" or "terminated")
        {
            try { _resourceEventObserver?.Invoke(eventName); }
            catch (Exception) { }
        }
        if (eventName is "process" or "loadedSource" or "module" or "progressStart" or "progressUpdate" or "progressEnd"
            or "thread" or "stopped" or "continued" or "exited" or "terminated" or "invalidated" or "memory")
        {
            if (_resourceEventObserver is not null && eventName is ("invalidated" or "memory"))
            {
                lock (_stateGate)
                {
                    try
                    {
                        if (body.ValueKind != JsonValueKind.Object || Encoding.UTF8.GetByteCount(body.GetRawText()) > 4096)
                            throw new InvalidDataException("Invalid event detail.");
                        if (eventName == "invalidated")
                        {
                            var fields = new Dictionary<string, object?>();
                            fields["areas"] = body.TryGetProperty("areas", out var areas) ? areas.Clone() : JsonSerializer.SerializeToElement(Array.Empty<string>());
                            if (body.TryGetProperty("threadId", out var thread) && thread.ValueKind != JsonValueKind.Null) fields["threadId"] = thread.Clone();
                            if (body.TryGetProperty("stackFrameId", out var frame) && frame.ValueKind != JsonValueKind.Null) fields["stackFrameId"] = frame.Clone();
                            _legacyInvalidation = JsonSerializer.SerializeToElement(fields);
                        }
                        else
                        {
                            var reference = body.TryGetProperty("memoryReference", out var referenceElement)
                                ? referenceElement.ValueKind == JsonValueKind.String
                                    ? referenceElement.GetString()!
                                    : throw new InvalidDataException("DAP memoryReference must be a string.")
                                : string.Empty;
                            var offset = ReadOptionalMemoryInteger(body, "offset", -MaximumSafeDapInteger);
                            var count = ReadOptionalMemoryInteger(body, "count", 0);
                            _legacyMemoryEvent = JsonSerializer.SerializeToElement(new { memoryReference = reference, offset, count });
                        }
                    }
                    catch (Exception exception) when (exception is InvalidDataException or InvalidOperationException or JsonException or ArgumentException)
                    {
                        _legacyEventDetailsIncomplete = true;
                    }
                }
            }
            if (eventName == "thread") { lock (_stateGate) _legacyThreads = null; }
            try { _resourceEventObserver?.Invoke("state"); } catch (Exception) { }
        }
    }

    private void HandleRuntimeInventoryEvent(string eventName, JsonElement body)
    {
        var modules = eventName == "module";
        lock (_runtimeInventoryGate)
        {
            if (modules ? _modulesIncomplete : _progressIncomplete) return;
            try
            {
                if (modules) UpdateModuleInventory(body);
                else UpdateProgressInventory(eventName, body);
            }
            catch (Exception exception) when (exception is InvalidDataException or InvalidOperationException
                or ArgumentException or JsonException or OverflowException)
            {
                // A lost transition cannot be repaired by later events. Release its
                // retained values, latch only this inventory, and keep the reader usable.
                InvalidateRuntimeInventory(modules);
            }
        }
    }

    private void InvalidateRuntimeInventory(bool modules)
    {
        if (modules)
        {
            _modulesIncomplete = true;
            _modules.Clear();
            _moduleOrder.Clear();
            _moduleBytes = 0;
        }
        else
        {
            _progressIncomplete = true;
            _progress.Clear();
            _progressOrder.Clear();
            _progressBytes = 0;
        }
    }

    private void UpdateModuleInventory(JsonElement body)
    {
        var reason = RequireString(body, "reason");
        if (reason is not "new" and not "changed" and not "removed"
            || !body.TryGetProperty("module", out var module) || module.ValueKind != JsonValueKind.Object
            || !module.TryGetProperty("id", out var idElement))
        {
            throw new InvalidDataException("Malformed module transition.");
        }

        RuntimeModuleId id;
        if (idElement.ValueKind == JsonValueKind.String) id = new(0, idElement.GetString()!);
        else if (idElement.ValueKind == JsonValueKind.Number && idElement.TryGetInt64(out var number)) id = new(number, null);
        else throw new InvalidDataException("Malformed module identifier.");

        _modules.TryGetValue(id, out var previous);
        if (reason == "removed")
        {
            if (previous is not null)
            {
                _moduleBytes -= previous.Value.Bytes;
                _moduleOrder.Remove(previous);
                _modules.Remove(id);
            }
            return;
        }

        var name = RequireString(module, "name");
        var path = ReadRuntimeOptionalString(module, "path");
        var version = ReadRuntimeOptionalString(module, "version");
        var optimized = ReadOptionalBoolean(module, "isOptimized");
        var symbols = ReadRuntimeOptionalString(module, "symbolStatus");
        if (reason == "new" ? previous is not null : previous is null) return;

        var entry = new RuntimeModuleEntry(id, EncodeRuntimeOptionalText(id.Text), JsonEncodedText.Encode(name),
            EncodeRuntimeOptionalText(path), EncodeRuntimeOptionalText(version), optimized, EncodeRuntimeOptionalText(symbols));
        var nextBytes = _moduleBytes - (previous?.Value.Bytes ?? 0) + entry.Bytes;
        var nextCount = _modules.Count + (previous is null ? 1 : 0);
        if (RuntimeArrayBytes(nextBytes, nextCount) > MaximumPayloadBytes)
        {
            InvalidateRuntimeInventory(modules: true);
            return;
        }
        if (previous is null) _modules.Add(id, _moduleOrder.AddLast(entry));
        else previous.Value = entry;
        _moduleBytes = nextBytes;
    }

    private void UpdateProgressInventory(string eventName, JsonElement body)
    {
        var id = RequireString(body, "progressId");
        _progress.TryGetValue(id, out var previous);
        if (eventName == "progressEnd")
        {
            if (previous is not null)
            {
                _progressBytes -= previous.Value.Bytes;
                _progressOrder.Remove(previous);
                _progress.Remove(id);
            }
            return;
        }

        var message = ReadRuntimeOptionalString(body, "message");
        var percentage = ReadRuntimeOptionalPercentage(body);
        RuntimeProgressEntry entry;
        if (eventName == "progressStart")
        {
            var title = RequireString(body, "title");
            var cancellable = ReadOptionalBoolean(body, "cancellable");
            entry = new RuntimeProgressEntry(JsonEncodedText.Encode(id), JsonEncodedText.Encode(title),
                message, EncodeRuntimeOptionalText(message), percentage, cancellable, Stopwatch.GetTimestamp() / (double)Stopwatch.Frequency);
        }
        else
        {
            if (previous is null) return;
            var prior = previous.Value;
            var messageChanged = message is not null && !string.Equals(message, prior.MessageText, StringComparison.Ordinal);
            if (!messageChanged && (percentage is null || percentage == prior.Percentage)) return;
            entry = prior with
            {
                MessageText = messageChanged ? message : prior.MessageText,
                Message = messageChanged ? JsonEncodedText.Encode(message!) : prior.Message,
                Percentage = percentage ?? prior.Percentage,
            };
        }

        var nextBytes = _progressBytes - (previous?.Value.Bytes ?? 0) + entry.Bytes;
        var nextCount = _progress.Count + (previous is null ? 1 : 0);
        if (RuntimeArrayBytes(nextBytes, nextCount) > MaximumPayloadBytes)
        {
            InvalidateRuntimeInventory(modules: false);
            return;
        }
        if (previous is null) _progress.Add(id, _progressOrder.AddLast(entry));
        else previous.Value = entry;
        _progressBytes = nextBytes;
    }

    private static long RuntimeArrayBytes(long entryBytes, int count) => entryBytes + Math.Max(0, count - 1) + 2;

    private static JsonEncodedText? EncodeRuntimeOptionalText(string? value) => value is null ? null : JsonEncodedText.Encode(value);

    private static string? ReadRuntimeOptionalString(JsonElement body, string name)
    {
        if (!body.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null) return null;
        if (value.ValueKind != JsonValueKind.String) throw new InvalidDataException("Malformed runtime inventory text.");
        return value.GetString();
    }

    private static double? ReadRuntimeOptionalPercentage(JsonElement body)
    {
        if (!body.TryGetProperty("percentage", out var value) || value.ValueKind == JsonValueKind.Null) return null;
        if (value.ValueKind != JsonValueKind.Number || !value.TryGetDouble(out var percentage)
            || !double.IsFinite(percentage) || percentage is < 0 or > 100)
        {
            throw new InvalidDataException("Malformed runtime inventory percentage.");
        }
        return percentage;
    }


    private bool HandleOutputEvent(JsonElement body)
    {
        if (body.ValueKind != JsonValueKind.Object)
        {
            return false;
        }

        var category = "console";
        if (body.TryGetProperty("category", out var categoryElement))
        {
            if (!TryDecodeOutputString(categoryElement, out category))
            {
                return false;
            }

            if (category is not "console" and not "stdout" and not "stderr" and not "telemetry")
            {
                return false;
            }
        }

        var text = string.Empty;
        if (body.TryGetProperty("output", out var outputElement) && !TryDecodeOutputString(outputElement, out text))
        {
            return false;
        }

        return _outputBuffer.Append(text, category);
    }

    private static bool TryDecodeOutputString(JsonElement element, out string text)
    {
        text = string.Empty;
        if (element.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        try
        {
            text = element.GetString()!;
        }
        catch (InvalidOperationException)
        {
            // Invalid Unicode in one output event must not retire the DAP session.
            return false;
        }

        return true;
    }

    private void CaptureNativeSceneTargetIdentity(JsonElement body)
    {
        if (TryGetInt32(body, "systemProcessId") is not int processId
            || processId <= 0
            || !body.TryGetProperty("isLocalProcess", out var isLocalProcess)
            || isLocalProcess.ValueKind != JsonValueKind.True)
        {
            return;
        }

        var pinned = Volatile.Read(ref _nativeSceneTargetIdentity);
        if (pinned is not null && pinned.ProcessId != processId)
        {
            Interlocked.Exchange(ref _nativeSceneCaptureAuthorityInvalidated, 1);
            return;
        }

        try
        {
            using var windowsProcess = OperatingSystem.IsWindows() ? WindowsNativeSceneProcess.Open(processId) : null;

            // Job membership rejects other sessions, but does not prove an incarnation reused within this job.
            if (windowsProcess is not null
                && (_processTreeOwnership is not WindowsProcessTreeOwnership windowsOwnership
                    || !windowsOwnership.Contains(windowsProcess)))
            {
                return;
            }

            using var process = windowsProcess is null ? Process.GetProcessById(processId) : null;
            string? executablePath;
            long windowsCreationUtcTicks = 0;
            if (windowsProcess is null)
            {
                executablePath = process!.MainModule?.FileName;
            }
            else if (!WindowsNativeSceneProcess.TryReadIdentity(windowsProcess, out executablePath, out windowsCreationUtcTicks))
            {
                return;
            }

            if (string.IsNullOrWhiteSpace(executablePath))
            {
                return;
            }

            using var executable = File.OpenRead(executablePath);
            var identity = new NativeSceneTargetIdentity(
                processId,
                string.Concat(
                    "process_",
                    processId.ToString(CultureInfo.InvariantCulture),
                    "_start_",
                    (windowsProcess is null ? process!.StartTime.ToUniversalTime().Ticks : windowsCreationUtcTicks).ToString(CultureInfo.InvariantCulture)),
                executablePath,
                Convert.ToHexString(SHA256.HashData(executable)).ToLowerInvariant(),
                TryGetAssemblyVersion(executablePath),
                ProbeVersion: null);
            lock (_nativeSceneTargetIdentityGate)
            {
                if (windowsProcess is not null && !WindowsNativeSceneProcess.IsAlive(windowsProcess))
                {
                    return;
                }

                pinned = _nativeSceneTargetIdentity;
                if (pinned is null)
                {
                    Volatile.Write(ref _nativeSceneTargetIdentity, identity);
                }
                else if (!pinned.Equals(identity))
                {
                    Interlocked.Exchange(ref _nativeSceneCaptureAuthorityInvalidated, 1);
                }
            }
        }
        catch (Exception exception) when (exception is ArgumentException
                                         or InvalidOperationException
                                         or NotSupportedException
                                         or Win32Exception
                                         or UnauthorizedAccessException
                                         or IOException
                                         or System.Security.SecurityException
                                         or CryptographicException)
        {
        }
    }

    private static class WindowsNativeSceneProcess
    {
        private const uint ProcessQueryLimitedInformation = 0x1000;
        private const uint Synchronize = 0x00100000;
        private const uint WaitTimeout = 258;
        private const int MaximumImagePathCharacters = 32_768;

        public static SafeProcessHandle Open(int processId) =>
            OpenProcess(ProcessQueryLimitedInformation | Synchronize, inheritHandle: false, checked((uint)processId));

        public static bool IsAlive(SafeProcessHandle process) =>
            !process.IsInvalid && WaitForSingleObject(process, 0) == WaitTimeout;

        public static bool TryReadIdentity(SafeProcessHandle process, out string executablePath, out long creationUtcTicks)
        {
            executablePath = string.Empty;
            creationUtcTicks = 0;
            if (!IsAlive(process))
            {
                return false;
            }

            // Query the kernel process object, not its not-yet-initialized user-mode module list.
            // Keep this same handle through creation-time capture, hashing and the final pin liveness check.
            var image = System.Buffers.ArrayPool<char>.Shared.Rent(MaximumImagePathCharacters);
            try
            {
                uint characters = MaximumImagePathCharacters;
                if (!QueryFullProcessImageNameW(process, 0, image, ref characters)
                    || characters == 0 || characters >= MaximumImagePathCharacters)
                {
                    return false;
                }

                var length = checked((int)characters);
                ReadOnlySpan<char> imagePath = image.AsSpan(0, length);
                if (image[length] != '\0' || imagePath.IndexOf('\0') >= 0 || imagePath.IsWhiteSpace()
                    || !GetProcessTimes(process, out var creation, out _, out _, out _))
                {
                    return false;
                }

                var fileTime = ((long)(uint)creation.dwHighDateTime << 32) | (uint)creation.dwLowDateTime;
                if (fileTime <= 0)
                {
                    return false;
                }

                creationUtcTicks = DateTime.FromFileTimeUtc(fileTime).Ticks;
                if (!IsAlive(process))
                {
                    return false;
                }

                executablePath = new string(image, 0, length);
                return true;
            }
            finally
            {
                System.Buffers.ArrayPool<char>.Shared.Return(image, clearArray: true);
            }
        }

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern SafeProcessHandle OpenProcess(
            uint desiredAccess, [MarshalAs(UnmanagedType.Bool)] bool inheritHandle, uint processId);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryFullProcessImageNameW(
            SafeProcessHandle process, uint flags, [Out] char[] executableName, ref uint characters);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool GetProcessTimes(
            SafeProcessHandle process, out System.Runtime.InteropServices.ComTypes.FILETIME creationTime,
            out System.Runtime.InteropServices.ComTypes.FILETIME exitTime,
            out System.Runtime.InteropServices.ComTypes.FILETIME kernelTime,
            out System.Runtime.InteropServices.ComTypes.FILETIME userTime);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(SafeProcessHandle handle, uint milliseconds);
    }

    private static string? TryGetAssemblyVersion(string executablePath)
    {
        try
        {
            return AssemblyName.GetAssemblyName(executablePath).Version?.ToString();
        }
        catch (BadImageFormatException)
        {
            return null;
        }
        catch (FileLoadException)
        {
            return null;
        }
        catch (IOException)
        {
            return null;
        }
        catch (UnauthorizedAccessException)
        {
            return null;
        }
        catch (System.Security.SecurityException)
        {
            return null;
        }
    }

    private void ReadInitializeCapabilities(JsonElement body)
    {
        if (body.ValueKind == JsonValueKind.Undefined)
        {
            _supportsConfigurationDone = false;
            _supportsTerminate = false;
            lock (_stateGate)
            {
                _stackTraceCapabilities = default;
                _supportsExceptionInfoRequest = false;
                _supportsStepInTargetsRequest = false;
                _supportsLoadedSourcesRequest = false;
                _supportsExceptionFilters = false;
                _supportsSetVariable = false;
            }

            return;
        }

        if (body.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException("The initialize response body must be a JSON object.");
        }

        _supportsConfigurationDone = ReadOptionalBoolean(body, "supportsConfigurationDoneRequest");
        _supportsTerminate = ReadOptionalBoolean(body, "supportsTerminateRequest");
        var capabilities = new DapStackTraceCapabilities(ReadOptionalBoolean(body, "supportsDelayedStackTraceLoading"));
        var supportsExceptionInfo = ReadOptionalBoolean(body, "supportsExceptionInfoRequest");
        var supportsStepInTargets = ReadOptionalBoolean(body, "supportsStepInTargetsRequest");
        var supportsSetVariable = ReadOptionalBoolean(body, "supportsSetVariable");
        var supportsLoadedSources = ReadOptionalBoolean(body, "supportsLoadedSourcesRequest");
        var supportsExceptionFilters = ReadExceptionFilterSupport(body);
        lock (_stateGate)
        {
            _stackTraceCapabilities = capabilities;
            _supportsExceptionInfoRequest = supportsExceptionInfo;
            _supportsStepInTargetsRequest = supportsStepInTargets;
            _supportsLoadedSourcesRequest = supportsLoadedSources;
            _supportsExceptionFilters = supportsExceptionFilters;
            _supportsSetVariable = supportsSetVariable;
        }
    }

    private void UpdateState(Func<DapSessionState, DapSessionState> update)
    {
        lock (_stateGate)
        {
            _state = update(_state);
        }
    }
    private void HandleStoppedEvent(JsonElement message, JsonElement body)
    {
        var allThreadsStopped = ReadOptionalBoolean(body, "allThreadsStopped");
        var threadId = TryGetInt32(body, "threadId");
        var reason = TryGetString(body, "reason");
        _callStackAdmissionGate.Wait();
        try
        {
            lock (_stateGate)
            {
                if (_activeContinue is { Finalized: false } continued)
                {
                    if (RequireInt32(message, "seq") <= 0
                        || reason is null
                        || Encoding.UTF8.GetByteCount(reason) > MaximumContinueReasonBytes)
                    {
                        throw new InvalidDataException("DAP continue stop reason or sequence is invalid.");
                    }

                    if (body.TryGetProperty("threadId", out _))
                    {
                        threadId = RequireInt32(body, "threadId");
                    }

                    continued.ObserveStopped(reason, threadId,
                        body.TryGetProperty("allThreadsStopped", out _) ? allThreadsStopped : null);
                }

                _state = _state with
                {
                    Event = "stopped",
                    StopReason = reason,
                    CurrentThreadId = threadId,
                };
                _stopDescription = TryGetString(body, "description");
                _stopText = TryGetString(body, "text");
                _legacyLastStopAt = (double)Stopwatch.GetTimestamp() / Stopwatch.Frequency;
                ClearCallStackEligibility();
                _inspectionContextId = null;
                if (!_inspectionClosed && allThreadsStopped)
                {
                    _allThreadsStoppedEligibility = new CallStackEligibility();
                }
                else if (!_inspectionClosed && threadId is { } stoppedThreadId)
                {
                    (_threadEligibility ??= new())[stoppedThreadId] = new CallStackEligibility();
                }
                if (_resourceEventObserver is not null && _legacyBreakpointObservations.Count != 0 && reason == "breakpoint")
                {
                    if (threadId is { } hitThread && HasStepOverAuthority(hitThread))
                        ScheduleLegacyBreakpointHit(hitThread, GetOrCreateInspectionContext(), _legacyBreakpointObservations.Values.SelectMany(static value => value).ToArray());
                    else _legacyHitCountsIncomplete = true;
                }
                _activeStepOver?.ObserveStopped(message, reason, threadId);
            }
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }
    }

    private void HandleContinuedEvent(JsonElement body)
    {
        var hasAllThreadsContinued = body.ValueKind == JsonValueKind.Object
            && body.TryGetProperty("allThreadsContinued", out _);
        var allThreadsContinued = ReadOptionalBoolean(body, "allThreadsContinued");
        var threadId = TryGetInt32(body, "threadId");
        _callStackAdmissionGate.Wait();
        try
        {
            lock (_stateGate)
            {
                _activeContinue?.ObserveContinued();
                _state = _state with { Event = "continued", CurrentThreadId = null };
                _legacyLastResumeAt = (double)Stopwatch.GetTimestamp() / Stopwatch.Frequency;
                _inspectionContextId = null;
                if (!hasAllThreadsContinued || allThreadsContinued || threadId is null)
                {
                    if (_resourceEventObserver is not null && (!hasAllThreadsContinued || allThreadsContinued))
                    {
                        _state = _state with { StopReason = null };
                        _stopDescription = null;
                        _stopText = null;
                    }
                    ClearCallStackEligibility();
                    return;
                }

                _threadEligibility?.Remove(threadId.Value);
                if (_allThreadsStoppedEligibility is not null)
                {
                    (_invalidatedAllThreadTargets ??= new()).Add(threadId.Value);
                }
            }
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }
    }

    private void HandleTerminalEvent(JsonElement message, string eventName, JsonElement body)
    {
        var revokeUiInvoke = false;
        _callStackAdmissionGate.Wait();
        try
        {
            lock (_stateGate)
            {
                if (_activeStepOver is not null && RequireInt32(message, "seq") <= 0)
                {
                    throw new InvalidDataException("DAP step terminal event sequence must be positive.");
                }

                if (_activePause is not null)
                {
                    if (RequireInt32(message, "seq") <= 0)
                    {
                        throw new InvalidDataException("DAP pause terminal event sequence is invalid.");
                    }
                    if (body.ValueKind == JsonValueKind.Object && body.TryGetProperty("exitCode", out _))
                    {
                        _state = _state with { ExitCode = RequireInt32(body, "exitCode") };
                    }
                }

                if (_activeContinue is { Finalized: false })
                {
                    if (RequireInt32(message, "seq") <= 0)
                    {
                        throw new InvalidDataException("DAP continue terminal event sequence is invalid.");
                    }

                    if (body.ValueKind == JsonValueKind.Object && body.TryGetProperty("exitCode", out _))
                    {
                        _state = _state with { ExitCode = RequireInt32(body, "exitCode") };
                    }
                }

                _state = eventName == "exited"
                    ? _state with
                    {
                        Event = eventName,
                        ExitCode = _activeStepOver is null ? TryGetInt32(body, "exitCode") : RequireInt32(body, "exitCode"),
                        CurrentThreadId = null,
                    }
                    : _state with { Event = eventName, CurrentThreadId = null };
                revokeUiInvoke = Interlocked.Exchange(ref _nativeUiInvokeAuthorityInvalidated, 1) == 0;
                ClearCallStackEligibility();
                _inspectionContextId = null;
                _executionAdmission = ExecutionAdmission.Closing;
                _inspectionClosed = true;
                _activePause?.ObserveTerminal(eventName, _state.ExitCode);
                _activeStepOver?.ObserveTerminal(eventName, _state.ExitCode);
                _activeContinue?.ObserveTerminal(eventName, _state.ExitCode);
                _breakpointUpdatesClosed = true;
                _activeBreakpointUpdate?.ObserveFailure(new InvalidDataException("The debug session terminated during breakpoint update."));
            }
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }

        // Cancel an in-flight helper only after releasing DAP admission/state locks.
        if (revokeUiInvoke)
            NativeUiInvokeAuthorityRevoked?.Invoke(this);
    }

    private void RequestRetirement()
    {
        lock (_stateGate)
        {
            if (Interlocked.Exchange(ref _retirementRequested, 1) != 0)
            {
                return;
            }
        }

        try
        {
            RetirementRequested?.Invoke(this);
        }
        catch (Exception)
        {
            // Registry cleanup is observed separately from reader dispatch.
        }
    }

    private bool HasStepOverAuthority(int threadId) =>
        (_allThreadsStoppedEligibility is not null
            && (_invalidatedAllThreadTargets is null || !_invalidatedAllThreadTargets.Contains(threadId)))
        || (_threadEligibility is not null && _threadEligibility.ContainsKey(threadId));

    private CapturedCallStackTarget? CaptureCallStackTarget(int threadId, bool includeInspectionContext) =>
        CaptureOptionalCallStackTarget(threadId, includeInspectionContext);

    private CapturedCallStackTarget? CaptureOptionalCallStackTarget(int? threadId, bool includeInspectionContext)
    {
        lock (_stateGate)
        {
            if (_executionAdmission == ExecutionAdmission.Closing || (includeInspectionContext && _inspectionClosed))
            {
                return null;
            }
            if (threadId is null && _state.Event != "stopped") return null;
            var selected = threadId ?? _state.CurrentThreadId;
            if (selected is not { } resolved) return null;

            var supportsPaging = _stackTraceCapabilities.SupportsDelayedStackTraceLoading;
            if (_allThreadsStoppedEligibility is { } allTargets
                && (_invalidatedAllThreadTargets is null || !_invalidatedAllThreadTargets.Contains(resolved)))
            {
                return new CapturedCallStackTarget(allTargets, resolved, supportsPaging,
                    includeInspectionContext ? GetOrCreateInspectionContext() : null);
            }

            return _threadEligibility is not null
                && _threadEligibility.TryGetValue(resolved, out var target)
                    ? new CapturedCallStackTarget(target, resolved, supportsPaging,
                        includeInspectionContext ? GetOrCreateInspectionContext() : null)
                    : null;
        }
    }

    private string GetOrCreateInspectionContext()
    {
        if (_inspectionContextId is null)
        {
            Span<byte> bytes = stackalloc byte[32];
            RandomNumberGenerator.Fill(bytes);
            _inspectionContextId = Convert.ToHexString(bytes);
            CryptographicOperations.ZeroMemory(bytes);
        }

        return _inspectionContextId;
    }

    private void InvalidateInspectionContext(bool close = false)
    {
        _callStackAdmissionGate.Wait();
        try
        {
            lock (_stateGate)
            {
                _inspectionContextId = null;
                _inspectionClosed |= close;
            }
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }
    }

    private bool IsCallStackTargetCurrent(CapturedCallStackTarget target)
    {
        lock (_stateGate)
        {
            if (ReferenceEquals(_allThreadsStoppedEligibility, target.Eligibility))
            {
                return _invalidatedAllThreadTargets is null || !_invalidatedAllThreadTargets.Contains(target.ThreadId);
            }

            return _threadEligibility is not null
                && _threadEligibility.TryGetValue(target.ThreadId, out var current)
                && ReferenceEquals(current, target.Eligibility);
        }
    }

    private void ClearCallStackEligibility()
    {
        _allThreadsStoppedEligibility = null;
        _threadEligibility?.Clear();
        _invalidatedAllThreadTargets = null;
    }

    private static DapStackFrame? NormalizeStackFrame(JsonElement stackFrame, bool materialize)
    {
        var id = RequireInt32(stackFrame, "id");
        var name = RequireString(stackFrame, "name");
        if (Encoding.UTF8.GetByteCount(name) > MaximumStackFrameNameBytes)
        {
            throw new InvalidDataException("DAP stack frame name exceeds the supported maximum.");
        }

        var line = RequireSafeDapInteger(stackFrame, "line");
        var column = RequireSafeDapInteger(stackFrame, "column");
        if (!stackFrame.TryGetProperty("source", out var sourceElement))
        {
            return materialize ? new DapStackFrame(id, name, null, 0, 0) : null;
        }

        if (sourceElement.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException("DAP stack frame source must be an object.");
        }

        if (!sourceElement.TryGetProperty("path", out var pathElement))
        {
            return materialize ? new DapStackFrame(id, name, null, 0, 0) : null;
        }

        var path = RequireString(sourceElement, "path");
        if (Encoding.UTF8.GetByteCount(path) > MaximumStackFramePathBytes)
        {
            throw new InvalidDataException("DAP stack frame source path exceeds the supported maximum.");
        }

        return materialize ? new DapStackFrame(id, name, path, line, column) : null;
    }

    private Task EnsureCleanupAsync()
    {
        lock (_cleanupGate)
        {
            return _cleanupTask ??= CleanupAsync();
        }
    }

    private async Task CleanupAsync()
    {
        await _callStackAdmissionGate.WaitAsync().ConfigureAwait(false);
        try
        {
            lock (_stateGate)
            {
                _inspectionContextId = null;
                _inspectionClosed = true;
                _breakpointUpdatesClosed = true;
            }
        }
        finally
        {
            _callStackAdmissionGate.Release();
        }

        try
        {
            if (!HasExited() && !_forceCleanup.IsCancellationRequested)
            {
                if (_supportsTerminate)
                {
                    await TrySendCleanupRequestAsync("terminate", new { restart = false }).ConfigureAwait(false);
                }

                if (!_forceCleanup.IsCancellationRequested)
                {
                    await TrySendCleanupRequestAsync(
                        "disconnect",
                        new { restart = false, terminateDebuggee = true }).ConfigureAwait(false);
                }
            }

            _input.Dispose();
            var unixOwnership = _processTreeOwnership as UnixProcessGroupOwnership;
            var guardianSignaled = unixOwnership is not null;

            if (_forceCleanup.IsCancellationRequested)
            {
                unixOwnership?.Terminate();
                if (!guardianSignaled || !await WaitForProcessExitAsync().ConfigureAwait(false))
                {
                    KillProcessTree();
                    if (!await WaitForProcessExitAsync().ConfigureAwait(false))
                    {
                        throw new InvalidOperationException("The owned debugger process did not exit after forced process tree cleanup.");
                    }
                }
            }
            else
            {
                unixOwnership?.SignalGracefulShutdown();
                if (!HasExited() && !await WaitForProcessExitAsync().ConfigureAwait(false))
                {
                    KillProcessTree();
                    if (!await WaitForProcessExitAsync().ConfigureAwait(false))
                    {
                        throw new InvalidOperationException("The owned debugger process did not exit after its process tree was killed.");
                    }
                }
            }

            if (HasExited())
            {
                _processTreeOwnership?.Terminate();
            }
        }
        finally
        {
            try
            {
                _input.Dispose();
            }
            finally
            {
                try
                {
                    await ObserveBackgroundTasksAsync().ConfigureAwait(false);
                }
                finally
                {
                    try
                    {
                        _output.Dispose();
                        _error.Dispose();
                    }
                    finally
                    {
                        try
                        {
                            _writeGate.Dispose();
                        }
                        finally
                        {
                            try
                            {
                                _process.Dispose();
                            }
                            finally
                            {
                                _processTreeOwnership?.Dispose();
                            }
                        }
                    }
                }
            }
        }
    }

    private async Task TrySendCleanupRequestAsync(string command, object arguments)
    {
        try
        {
            using var requestCancellation = CancellationTokenSource.CreateLinkedTokenSource(_forceCleanup.Token);
            _ = await SendRequestAsync(command, arguments, _requestTimeout, requestCancellation.Token).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Cleanup must still attempt the next ownership step after a bounded request failure.
        }
    }

    private async Task<bool> WaitForProcessExitAsync()
    {
        if (HasExited())
        {
            return true;
        }

        try
        {
            await _process.WaitForExitAsync().WaitAsync(_stopTimeout).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            return HasExited();
        }

        return HasExited();
    }

    private void KillProcessTree()
    {
        try
        {
            if (!HasExited())
            {
                _process.Kill(entireProcessTree: true);
            }
        }
        catch (InvalidOperationException) when (HasExited())
        {
            // The owned process exited between the check and the kill request.
        }
    }

    private async Task ObserveBackgroundTasksAsync()
    {
        try
        {
            await Task.WhenAll(_readerTask, _stderrTask).WaitAsync(_stopTimeout).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // Reader parse errors and process shutdown races have already completed or faulted pending requests.
        }
    }

    private async Task DrainStandardErrorAsync()
    {
        var buffer = new byte[8192];
        while (await _error.ReadAsync(buffer).ConfigureAwait(false) != 0)
        {
        }
    }

    private async Task<JsonDocument?> ReadFrameAsync(CancellationToken cancellationToken)
    {
        var headers = await ReadHeadersAsync(cancellationToken).ConfigureAwait(false);
        if (headers is null)
        {
            return null;
        }

        var contentLength = ReadContentLength(headers);
        if (contentLength > MaximumPayloadBytes)
        {
            throw new InvalidDataException($"DAP frame length '{contentLength}' exceeds the supported maximum.");
        }

        var payload = GC.AllocateUninitializedArray<byte>(contentLength);
        await ReadExactlyAsync(_output, payload, cancellationToken).ConfigureAwait(false);
        return JsonDocument.Parse(payload);
    }

    private async Task<string?> ReadHeadersAsync(CancellationToken cancellationToken)
    {
        using var header = new MemoryStream();
        while (true)
        {
            var read = await _output.ReadAsync(_headerByte, cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                return header.Length == 0
                    ? null
                    : throw new EndOfStreamException("DAP header ended before its terminating blank line.");
            }

            if (header.Length == MaximumHeaderBytes)
            {
                throw new InvalidDataException("DAP header exceeds the supported maximum.");
            }

            header.WriteByte(_headerByte[0]);
            if (header.Length >= 4)
            {
                var bytes = header.GetBuffer();
                var length = checked((int)header.Length);
                if (bytes[length - 4] == '\r' && bytes[length - 3] == '\n'
                    && bytes[length - 2] == '\r' && bytes[length - 1] == '\n')
                {
                    return HeaderEncoding.GetString(bytes, 0, length - 4);
                }
            }
        }
    }

    private static int ReadContentLength(string headers)
    {
        int? contentLength = null;
        foreach (var header in headers.Split("\r\n", StringSplitOptions.None))
        {
            var separator = header.IndexOf(':');
            if (separator <= 0)
            {
                throw new InvalidDataException($"Invalid DAP header '{header}'.");
            }

            if (!header[..separator].Equals("Content-Length", StringComparison.OrdinalIgnoreCase))
            {
                continue;
            }

            if (contentLength is not null
                || !int.TryParse(header[(separator + 1)..].Trim(), NumberStyles.None, CultureInfo.InvariantCulture, out var parsed)
                || parsed < 0)
            {
                throw new InvalidDataException("DAP Content-Length header is invalid.");
            }

            contentLength = parsed;
        }

        return contentLength ?? throw new InvalidDataException("DAP Content-Length header is required.");
    }

    private static async Task ReadExactlyAsync(Stream stream, byte[] buffer, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(offset), cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                throw new EndOfStreamException("DAP payload ended before Content-Length bytes were read.");
            }

            offset += read;
        }
    }

    private static bool ReadExceptionFilterSupport(JsonElement capabilities)
    {
        if (!capabilities.TryGetProperty("exceptionBreakpointFilters", out var filters))
        {
            return false;
        }
        if (filters.ValueKind != JsonValueKind.Array)
        {
            throw new InvalidDataException("DAP exceptionBreakpointFilters must be an array.");
        }
        return filters.GetArrayLength() != 0;
    }

    private static bool[]? ReadExceptionFilterResults(JsonElement body, int expectedCount)
    {
        if (body.ValueKind == JsonValueKind.Undefined)
        {
            return null;
        }
        if (body.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException("DAP exception breakpoint response body must be an object.");
        }
        if (!body.TryGetProperty("breakpoints", out var results))
        {
            return null;
        }
        if (results.ValueKind != JsonValueKind.Array || results.GetArrayLength() != expectedCount)
        {
            throw new InvalidDataException("DAP exception breakpoint response count is invalid.");
        }
        var verified = new bool[expectedCount];
        var index = 0;
        foreach (var result in results.EnumerateArray())
        {
            if (result.ValueKind != JsonValueKind.Object || !result.TryGetProperty("verified", out var value)
                || value.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            {
                throw new InvalidDataException("DAP exception breakpoint verification is invalid.");
            }
            verified[index++] = value.GetBoolean();
        }
        return verified;
    }

    private static bool ReadOptionalBoolean(JsonElement objectElement, string name)
    {
        if (!objectElement.TryGetProperty(name, out var value))
        {
            return false;
        }

        return value.ValueKind switch
        {
            JsonValueKind.True => true,
            JsonValueKind.False => false,
            _ => throw new InvalidDataException($"DAP property '{name}' must be a boolean."),
        };
    }

    private static string RequireString(JsonElement objectElement, string name) =>
        TryGetString(objectElement, name)
        ?? throw new InvalidDataException($"DAP property '{name}' must be a string.");

    private static string? TryGetString(JsonElement objectElement, string name) =>
        objectElement.ValueKind == JsonValueKind.Object
        && objectElement.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    private static int RequireInt32(JsonElement objectElement, string name) =>
        TryGetInt32(objectElement, name)
        ?? throw new InvalidDataException($"DAP property '{name}' must be an Int32.");

    private static int? TryGetInt32(JsonElement objectElement, string name) =>
        objectElement.ValueKind == JsonValueKind.Object
        && objectElement.TryGetProperty(name, out var value)
        && value.ValueKind == JsonValueKind.Number
        && value.TryGetInt32(out var number)
            ? number
            : null;
    private static long ReadOptionalMemoryInteger(JsonElement body, string name, long minimum)
    {
        if (!body.TryGetProperty(name, out var value)) return 0;
        if (value.ValueKind != JsonValueKind.Number || !value.TryGetInt64(out var number)
            || number < minimum || number > MaximumSafeDapInteger)
            throw new InvalidDataException($"DAP memory {name} must be a safe integer in range.");
        return number;
    }

    private static long RequireSafeDapInteger(JsonElement objectElement, string name)
    {
        if (objectElement.ValueKind != JsonValueKind.Object
            || !objectElement.TryGetProperty(name, out var value)
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt64(out var number)
            || number < 0
            || number > MaximumSafeDapInteger)
        {
            throw new InvalidDataException($"DAP property '{name}' must be a safe non-negative integer.");
        }

        return number;
    }

    private static TimeSpan RequirePositiveTimeout(TimeSpan timeout, string parameterName) =>
        timeout > TimeSpan.Zero
            ? timeout
            : throw new ArgumentOutOfRangeException(parameterName, "Timeout must be greater than zero.");

    private static CancellationTokenSource CreateDeadline(TimeSpan timeout, CancellationToken cancellationToken)
    {
        var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(timeout);
        return deadline;
    }

    private void RemovePending(PendingRequest request) =>
        _pending.TryRemove(new KeyValuePair<int, PendingRequest>(request.Sequence, request));

    private void FailRequest(PendingRequest request, Exception failure)
    {
        if (request.Command == "setVariable")
        {
            lock (_stateGate)
            {
                _inspectionContextId = null;
                _inspectionClosed = true;
            }
        }
        if (request.Pause is { } pause)
        {
            lock (_stateGate)
            {
                if (ReferenceEquals(_activePause, pause) && !pause.Finalized && pause.Outcome is null)
                {
                    pause.ObserveFailure(failure);
                    FencePause();
                }
                request.Completion.TrySetException(failure);
                _ = request.Completion.Task.Exception;
            }
            return;
        }

        if (request.BreakpointUpdate is { } update)
        {
            lock (_stateGate)
            {
                if (ReferenceEquals(_activeBreakpointUpdate, update) && !update.Finalized)
                {
                    update.ObserveFailure(failure);
                    FenceBreakpointUpdates();
                }

                request.Completion.TrySetException(failure);
                _ = request.Completion.Task.Exception;
            }

            return;
        }

        if (request.ContinueExecution is not { } continued)
        {
            request.Completion.TrySetException(failure);
            return;
        }

        lock (_stateGate)
        {
            if (ReferenceEquals(_activeContinue, continued))
            {
                continued.ObserveProtocolFailure(failure);
            }

            request.Completion.TrySetException(failure);
            _ = request.Completion.Task.Exception;
        }
    }

    private void FailPending(Exception failure)
    {
        foreach (var pending in _pending)
        {
            if (_pending.TryRemove(pending.Key, out var request))
            {
                FailRequest(request, failure);
            }
        }
    }

    private bool HasExited()
    {
        try
        {
            return _process.HasExited;
        }
        catch (InvalidOperationException)
        {
            return true;
        }
    }

    private static async Task StopUnmanagedProcessAsync(
        Process process,
        TimeSpan stopTimeout,
        IProcessTreeOwnership? processTreeOwnership)
    {
        try
        {
            if (processTreeOwnership is UnixProcessGroupOwnership unixOwnership)
            {
                unixOwnership.Terminate();
                if (!process.HasExited)
                {
                    try
                    {
                        await process.WaitForExitAsync().WaitAsync(RequirePositiveTimeout(stopTimeout, nameof(stopTimeout))).ConfigureAwait(false);
                    }
                    catch (TimeoutException)
                    {
                        // Fall through to the bounded process-tree fallback when the guardian does not exit.
                    }
                }
            }

            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync().WaitAsync(RequirePositiveTimeout(stopTimeout, nameof(stopTimeout))).ConfigureAwait(false);
            }
        }
        finally
        {
            try
            {
                processTreeOwnership?.Terminate();
            }
            finally
            {
                process.Dispose();
                processTreeOwnership?.Dispose();
            }
        }
    }

    internal interface IProcessTreeOwnership : IDisposable
    {
        void Terminate();
    }

    private sealed class UnixProcessGroupOwnership : IProcessTreeOwnership
    {
        private readonly System.IO.Pipes.AnonymousPipeServerStream _terminationControl;

        private UnixProcessGroupOwnership(System.IO.Pipes.AnonymousPipeServerStream terminationControl) =>
            _terminationControl = terminationControl;

        public static UnixProcessGroupLaunch Start(string debuggerPath, IReadOnlySet<string>? removals)
        {
            var terminationControl = new System.IO.Pipes.AnonymousPipeServerStream(
                System.IO.Pipes.PipeDirection.Out,
                System.IO.HandleInheritability.Inheritable);
            try
            {
                var proxyAssemblyPath = typeof(NetCoreDbgSession).Assembly.Location;
                if (string.IsNullOrWhiteSpace(proxyAssemblyPath))
                {
                    throw new InvalidOperationException("The stateless proxy assembly path is unavailable.");
                }

                var proxyAppHostPath = Path.ChangeExtension(proxyAssemblyPath, extension: null);
                var useAppHost = File.Exists(proxyAppHostPath);
                var dotnetHostPath = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
                var startInfo = new ProcessStartInfo
                {
                    FileName = useAppHost
                        ? proxyAppHostPath
                        : string.IsNullOrWhiteSpace(dotnetHostPath) ? "dotnet" : dotnetHostPath,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    RedirectStandardInput = true,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                };
                if (removals is not null)
                {
                    foreach (var name in removals) startInfo.Environment.Remove(name);
                }
                if (removals?.Contains("PATH") == true)
                {
                    if (!useAppHost && !Path.IsPathFullyQualified(startInfo.FileName))
                        startInfo.FileName = DapLaunchEnvironment.FindExecutable(startInfo.FileName)
                            ?? throw new InvalidOperationException("The .NET host executable is unavailable.");
                    if (!Path.IsPathFullyQualified(debuggerPath))
                        debuggerPath = DapLaunchEnvironment.FindExecutable(debuggerPath)
                            ?? throw new InvalidOperationException("The debugger executable is unavailable.");
                }
                if (!useAppHost)
                {
                    startInfo.ArgumentList.Add(proxyAssemblyPath);
                }

                startInfo.ArgumentList.Add("--unix-process-group-proxy");
                startInfo.ArgumentList.Add(debuggerPath);
                startInfo.ArgumentList.Add(terminationControl.GetClientHandleAsString());
                var process = Process.Start(startInfo)
                    ?? throw new InvalidOperationException($"Could not start debugger '{debuggerPath}'.");
                terminationControl.DisposeLocalCopyOfClientHandle();
                return new UnixProcessGroupLaunch(process, new UnixProcessGroupOwnership(terminationControl));
            }
            catch
            {
                terminationControl.Dispose();
                throw;
            }
        }

        public void SignalGracefulShutdown()
        {
            try
            {
                _terminationControl.WriteByte(1);
            }
            finally
            {
                _terminationControl.Dispose();
            }
        }

        public void Terminate() => _terminationControl.Dispose();

        public void Dispose() => Terminate();
    }

    private sealed record UnixProcessGroupLaunch(Process Process, UnixProcessGroupOwnership Ownership);

    internal sealed class WindowsProcessTreeOwnership : IProcessTreeOwnership
    {
        private const uint CreateNoWindow = 0x08000000;
        private const uint CreateSuspended = 0x00000004;
        private const uint CreateUnicodeEnvironment = 0x00000400;
        private const uint ExtendedStartupInfoPresent = 0x00080000;
        private const uint HandleFlagInherit = 0x00000001;
        private const uint JobObjectExtendedLimitInformationClass = 9;
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private const uint StartfUseStdHandles = 0x00000100;

        private readonly SafeKernelHandle _job;

        private WindowsProcessTreeOwnership(SafeKernelHandle job) => _job = job;

        public static WindowsProcessTreeLaunch Start(string debuggerPath, WindowsChildEnvironmentBlock environment) =>
            StartCore(debuggerPath, ["--interpreter=vscode"], atomicJobAssignment: false, environment, workingDirectory: null);

        internal static WindowsProcessTreeLaunch Start(string executablePath, IReadOnlyList<string> arguments) =>
            StartCore(executablePath, arguments, atomicJobAssignment: true, environment: null, workingDirectory: null);

        internal static WindowsProcessTreeLaunch Start(string executablePath, IReadOnlyList<string> arguments, string workingDirectory) =>
            StartCore(executablePath, arguments, atomicJobAssignment: true, environment: null, workingDirectory);

        private static WindowsProcessTreeLaunch StartCore(
            string executablePath, IReadOnlyList<string> arguments, bool atomicJobAssignment, WindowsChildEnvironmentBlock? environment, string? workingDirectory)
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(executablePath);
            ArgumentNullException.ThrowIfNull(arguments);
            if (atomicJobAssignment)
            {
                executablePath = ResolveExecutablePath(executablePath);
            }
            SafeFileHandle? standardInput = null;
            SafeFileHandle? standardOutput = null;
            SafeFileHandle? standardError = null;
            SafeKernelHandle? childInput = null;
            SafeKernelHandle? childOutput = null;
            SafeKernelHandle? childError = null;
            SafeKernelHandle? job = null;
            IntPtr processHandle = IntPtr.Zero;
            IntPtr threadHandle = IntPtr.Zero;
            Process? process = null;
            Stream? input = null;
            Stream? output = null;
            Stream? error = null;

            try
            {
                CreatePipe(out standardInput, out childInput, parentReads: false);
                CreatePipe(out standardOutput, out childOutput, parentReads: true);
                CreatePipe(out standardError, out childError, parentReads: true);

                job = CreateKillOnCloseJob();
                var commandLine = new StringBuilder(QuoteCommandLineArgument(executablePath));
                for (var index = 0; index < arguments.Count; index++)
                    commandLine.Append(' ').Append(QuoteCommandLineArgument(arguments[index]));
                var processInformation = CreateProcessWithStandardHandles(
                    executablePath,
                    commandLine,
                    childInput.DangerousGetHandle(),
                    childOutput.DangerousGetHandle(),
                    childError.DangerousGetHandle(),
                    atomicJobAssignment ? job : null, environment, workingDirectory);

                processHandle = processInformation.Process;
                threadHandle = processInformation.Thread;
                childInput.Dispose();
                childInput = null;
                childOutput.Dispose();
                childOutput = null;
                childError.Dispose();
                childError = null;

                if (!atomicJobAssignment && !AssignProcessToJobObject(job.DangerousGetHandle(), processHandle))
                {
                    throw LastWin32Error("Could not assign the debugger process to its job object.");
                }

                process = Process.GetProcessById(checked((int)processInformation.ProcessId));
                // Keep the same process instance while the original creation handle still prevents PID reuse.
                _ = process.SafeHandle;
                if (ResumeThread(threadHandle) == uint.MaxValue)
                {
                    throw LastWin32Error("Could not resume the debugger process.");
                }

                CloseHandle(threadHandle);
                threadHandle = IntPtr.Zero;
                input = new FileStream(standardInput, FileAccess.Write, bufferSize: 4096, isAsync: false);
                standardInput = null;
                output = new FileStream(standardOutput, FileAccess.Read, bufferSize: 4096, isAsync: false);
                standardOutput = null;
                error = new FileStream(standardError, FileAccess.Read, bufferSize: 4096, isAsync: false);
                standardError = null;
                var ownership = new WindowsProcessTreeOwnership(job);
                var launch = new WindowsProcessTreeLaunch(process, input, output, error, ownership);
                CloseHandle(processHandle);
                processHandle = IntPtr.Zero;
                job = null;
                return launch;
            }
            catch (Exception failure)
            {
                Exception? cleanupFailure = null;
                if (processHandle != IntPtr.Zero)
                {
                    if (atomicJobAssignment && job is not null)
                    {
                        try
                        {
                            TerminateAndDrainFailedLaunch(job, processHandle);
                        }
                        catch (Exception exception)
                        {
                            cleanupFailure = exception;
                        }
                    }
                    else
                    {
                        TerminateProcess(processHandle, 1);
                    }
                }

                input?.Dispose();
                output?.Dispose();
                error?.Dispose();
                process?.Dispose();
                job?.Dispose();
                if (cleanupFailure is not null)
                    throw new AggregateException("Process launch failed and owned job cleanup could not be confirmed.", failure, cleanupFailure);
                throw;
            }
            finally
            {
                if (processHandle != IntPtr.Zero)
                {
                    CloseHandle(processHandle);
                }

                if (threadHandle != IntPtr.Zero)
                {
                    CloseHandle(threadHandle);
                }

                standardInput?.Dispose();
                standardOutput?.Dispose();
                standardError?.Dispose();
                childInput?.Dispose();
                childOutput?.Dispose();
                childError?.Dispose();
            }
        }

        public void Dispose() => _job.Dispose();

        internal bool Contains(SafeProcessHandle process) =>
            !process.IsInvalid && IsProcessInJob(process, _job, out var inJob) && inJob;

        internal uint ActiveProcessCount() => ActiveProcessCount(_job);

        private static uint ActiveProcessCount(SafeKernelHandle job)
        {
            if (!QueryInformationJobObject(job, 1, out var accounting,
                    (uint)Marshal.SizeOf<JobObjectBasicAccountingInformation>(), IntPtr.Zero))
            {
                throw LastWin32Error("Could not query the process job object accounting.");
            }

            return accounting.ActiveProcesses;
        }

        private static void TerminateAndDrainFailedLaunch(SafeKernelHandle job, IntPtr process)
        {
            if (!TerminateJobObject(job.DangerousGetHandle(), 1))
                throw LastWin32Error("Could not terminate the failed launch job object.");
            var elapsed = Stopwatch.StartNew();
            var wait = WaitForSingleObject(process, 5000);
            if (wait == uint.MaxValue)
                throw LastWin32Error("Could not wait for the failed launch process.");
            if (wait != 0)
                throw new TimeoutException("The failed launch process did not exit.");
            while (ActiveProcessCount(job) != 0)
            {
                if (elapsed.Elapsed >= TimeSpan.FromSeconds(5))
                    throw new TimeoutException("The failed launch job did not drain.");
                Thread.Sleep(50);
            }
        }

        public void Terminate()
        {
            if (!TerminateJobObject(_job.DangerousGetHandle(), 1))
            {
                throw LastWin32Error("Could not terminate the debugger process job object.");
            }
        }

        private static SafeKernelHandle CreateKillOnCloseJob()
        {
            var job = new SafeKernelHandle(CreateJobObject(IntPtr.Zero, null));
            if (job.IsInvalid)
            {
                var error = Marshal.GetLastWin32Error();
                job.Dispose();
                throw new Win32Exception(error, "Could not create the debugger process job object.");
            }

            var limits = new JobObjectExtendedLimitInformation
            {
                BasicLimitInformation = new JobObjectBasicLimitInformation
                {
                    LimitFlags = JobObjectLimitKillOnJobClose,
                },
            };
            if (!SetInformationJobObject(
                    job.DangerousGetHandle(),
                    JobObjectExtendedLimitInformationClass,
                    ref limits,
                    (uint)Marshal.SizeOf<JobObjectExtendedLimitInformation>()))
            {
                var exception = LastWin32Error("Could not configure the debugger process job object.");
                job.Dispose();
                throw exception;
            }

            return job;
        }

        private static void CreatePipe(
            out SafeFileHandle parent,
            out SafeKernelHandle child,
            bool parentReads)
        {
            var attributes = new SecurityAttributes
            {
                Length = Marshal.SizeOf<SecurityAttributes>(),
                InheritHandle = true,
            };
            if (!CreatePipe(out var read, out var write, ref attributes, 0))
            {
                throw LastWin32Error("Could not create a debugger standard I/O pipe.");
            }

            parent = new SafeFileHandle(parentReads ? read : write, ownsHandle: true);
            child = new SafeKernelHandle(parentReads ? write : read);
            try
            {
                if (!SetHandleInformation(parent.DangerousGetHandle(), HandleFlagInherit, 0))
                {
                    throw LastWin32Error("Could not configure a debugger standard I/O pipe.");
                }
            }
            catch
            {
                parent.Dispose();
                child.Dispose();
                throw;
            }
        }

        private static ProcessInformation CreateProcessWithStandardHandles(
            string debuggerPath,
            StringBuilder commandLine,
            IntPtr standardInput,
            IntPtr standardOutput,
            IntPtr standardError,
            SafeKernelHandle? atomicJob,
            WindowsChildEnvironmentBlock? environment,
            string? workingDirectory)
        {
            IntPtr attributeList = IntPtr.Zero;
            IntPtr inheritedHandleList = IntPtr.Zero;
            IntPtr jobHandleList = IntPtr.Zero;
            var attributeListInitialized = false;
            try
            {
                var attributeListSize = IntPtr.Zero;
                var attributeCount = atomicJob is null ? 1u : 2u;
                _ = InitializeProcThreadAttributeList(IntPtr.Zero, attributeCount, 0, ref attributeListSize);
                if (attributeListSize == IntPtr.Zero)
                {
                    throw LastWin32Error("Could not allocate debugger handle inheritance attributes.");
                }

                attributeList = Marshal.AllocHGlobal(attributeListSize);
                if (!InitializeProcThreadAttributeList(attributeList, attributeCount, 0, ref attributeListSize))
                {
                    throw LastWin32Error("Could not initialize debugger handle inheritance attributes.");
                }

                attributeListInitialized = true;
                inheritedHandleList = Marshal.AllocHGlobal(IntPtr.Size * 3);
                Marshal.WriteIntPtr(inheritedHandleList, 0, standardInput);
                Marshal.WriteIntPtr(inheritedHandleList, IntPtr.Size, standardOutput);
                Marshal.WriteIntPtr(inheritedHandleList, IntPtr.Size * 2, standardError);
                if (!UpdateProcThreadAttribute(
                        attributeList,
                        0,
                        new UIntPtr(0x00020002),
                        inheritedHandleList,
                        new UIntPtr((uint)(IntPtr.Size * 3)),
                        IntPtr.Zero,
                        IntPtr.Zero))
                {
                    throw LastWin32Error("Could not restrict debugger handle inheritance.");
                }

                if (atomicJob is not null)
                {
                    // JOB_LIST closes the parent-death gap between creation and a later AssignProcessToJobObject.
                    // Both attribute value buffers remain allocated until DeleteProcThreadAttributeList below.
                    jobHandleList = Marshal.AllocHGlobal(IntPtr.Size);
                    Marshal.WriteIntPtr(jobHandleList, atomicJob.DangerousGetHandle());
                    if (!UpdateProcThreadAttribute(
                            attributeList,
                            0,
                            new UIntPtr(0x0002000D),
                            jobHandleList,
                            new UIntPtr((uint)IntPtr.Size),
                            IntPtr.Zero,
                            IntPtr.Zero))
                    {
                        throw LastWin32Error("Could not atomically assign the invoke helper to its job object.");
                    }
                }

                var startupInfo = new StartupInfoEx
                {
                    StartupInfo = new StartupInfo
                    {
                        Size = (uint)Marshal.SizeOf<StartupInfoEx>(),
                        Flags = StartfUseStdHandles,
                        StandardInput = standardInput,
                        StandardOutput = standardOutput,
                        StandardError = standardError,
                    },
                    AttributeList = attributeList,
                };
                if (!CreateProcess(
                        debuggerPath,
                        commandLine,
                        IntPtr.Zero,
                        IntPtr.Zero,
                        inheritHandles: true,
                        CreateNoWindow | CreateSuspended | ExtendedStartupInfoPresent | (environment is null ? 0 : CreateUnicodeEnvironment),
                        environment?.DangerousGetHandle() ?? IntPtr.Zero,
                        currentDirectory: workingDirectory,
                        ref startupInfo,
                        out var processInformation))
                {
                    throw LastWin32Error($"Could not start debugger '{debuggerPath}'.");
                }

                return processInformation;
            }
            finally
            {
                if (attributeListInitialized)
                {
                    DeleteProcThreadAttributeList(attributeList);
                }

                GC.KeepAlive(atomicJob);
                GC.KeepAlive(environment);
                if (jobHandleList != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(jobHandleList);
                }

                if (inheritedHandleList != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(inheritedHandleList);
                }

                if (attributeList != IntPtr.Zero)
                {
                    Marshal.FreeHGlobal(attributeList);
                }
            }
        }

        private static string QuoteCommandLineArgument(string argument)
        {
            if (argument.Length != 0 && !argument.Any(char.IsWhiteSpace) && !argument.Contains('"'))
            {
                return argument;
            }

            var quoted = new StringBuilder();
            quoted.Append('"');
            var backslashes = 0;
            foreach (var character in argument)
            {
                if (character == '\\')
                {
                    backslashes++;
                }
                else if (character == '"')
                {
                    quoted.Append('\\', (backslashes * 2) + 1);
                    quoted.Append(character);
                    backslashes = 0;
                }
                else
                {
                    quoted.Append('\\', backslashes);
                    quoted.Append(character);
                    backslashes = 0;
                }
            }

            quoted.Append('\\', backslashes * 2);
            quoted.Append('"');
            return quoted.ToString();
        }

        private static string ResolveExecutablePath(string executablePath)
        {
            var buffer = new StringBuilder(260);
            var extension = Path.HasExtension(executablePath) ? null : ".exe";
            var length = SearchPath(null, executablePath, extension, (uint)buffer.Capacity, buffer, IntPtr.Zero);
            if (length >= buffer.Capacity && length <= 32_768)
            {
                buffer.EnsureCapacity(checked((int)length));
                length = SearchPath(null, executablePath, extension, (uint)buffer.Capacity, buffer, IntPtr.Zero);
            }
            if (length == 0 || length >= buffer.Capacity)
                throw LastWin32Error("Could not resolve the owned process executable.");
            return buffer.ToString();
        }

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern uint SearchPath(
            string? path, string fileName, string? extension, uint bufferLength, StringBuilder buffer, IntPtr filePart);

        private static Win32Exception LastWin32Error(string message) =>
            new(Marshal.GetLastWin32Error(), message);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreateProcess(
            string applicationName,
            StringBuilder commandLine,
            IntPtr processAttributes,
            IntPtr threadAttributes,
            [MarshalAs(UnmanagedType.Bool)] bool inheritHandles,
            uint creationFlags,
            IntPtr environment,
            string? currentDirectory,
            ref StartupInfoEx startupInfo,
            out ProcessInformation processInformation);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool InitializeProcThreadAttributeList(
            IntPtr attributeList,
            uint attributeCount,
            uint flags,
            ref IntPtr size);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool UpdateProcThreadAttribute(
            IntPtr attributeList,
            uint flags,
            UIntPtr attribute,
            IntPtr value,
            UIntPtr size,
            IntPtr previousValue,
            IntPtr returnSize);

        [DllImport("kernel32.dll")]
        private static extern void DeleteProcThreadAttributeList(IntPtr attributeList);


        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CreatePipe(
            out IntPtr readPipe,
            out IntPtr writePipe,
            ref SecurityAttributes pipeAttributes,
            uint size);

        [DllImport("kernel32.dll", SetLastError = true, CharSet = CharSet.Unicode)]
        private static extern IntPtr CreateJobObject(IntPtr jobAttributes, string? name);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetInformationJobObject(
            IntPtr job,
            uint informationClass,
            ref JobObjectExtendedLimitInformation jobObjectInformation,
            uint jobObjectInformationLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool QueryInformationJobObject(
            SafeKernelHandle job, uint informationClass,
            out JobObjectBasicAccountingInformation information, uint informationLength, IntPtr returnLength);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool IsProcessInJob(
            SafeProcessHandle process, SafeKernelHandle job, [MarshalAs(UnmanagedType.Bool)] out bool result);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint WaitForSingleObject(IntPtr handle, uint milliseconds);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateProcess(IntPtr process, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool TerminateJobObject(IntPtr job, uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool SetHandleInformation(IntPtr handle, uint mask, uint flags);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CloseHandle(IntPtr handle);

        [StructLayout(LayoutKind.Sequential)]
        private struct SecurityAttributes
        {
            public int Length;
            public IntPtr SecurityDescriptor;
            [MarshalAs(UnmanagedType.Bool)]
            public bool InheritHandle;
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct StartupInfo
        {
            public uint Size;
            public string? Reserved;
            public string? Desktop;
            public string? Title;
            public uint X;
            public uint Y;
            public uint XSize;
            public uint YSize;
            public uint XCountChars;
            public uint YCountChars;
            public uint FillAttribute;
            public uint Flags;
            public ushort ShowWindow;
            public ushort Reserved2Count;
            public IntPtr Reserved2;
            public IntPtr StandardInput;
            public IntPtr StandardOutput;
            public IntPtr StandardError;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct StartupInfoEx
        {
            public StartupInfo StartupInfo;
            public IntPtr AttributeList;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct ProcessInformation
        {
            public IntPtr Process;
            public IntPtr Thread;
            public uint ProcessId;
            public uint ThreadId;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JobObjectBasicAccountingInformation
        {
            public long TotalUserTime;
            public long TotalKernelTime;
            public long ThisPeriodTotalUserTime;
            public long ThisPeriodTotalKernelTime;
            public uint TotalPageFaultCount;
            public uint TotalProcesses;
            public uint ActiveProcesses;
            public uint TotalTerminatedProcesses;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JobObjectExtendedLimitInformation
        {
            public JobObjectBasicLimitInformation BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct JobObjectBasicLimitInformation
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        private struct IoCounters
        {
            public ulong ReadOperationCount;
            public ulong WriteOperationCount;
            public ulong OtherOperationCount;
            public ulong ReadTransferCount;
            public ulong WriteTransferCount;
            public ulong OtherTransferCount;
        }

        private sealed class SafeKernelHandle : SafeHandleZeroOrMinusOneIsInvalid
        {
            public SafeKernelHandle(IntPtr handle)
                : base(ownsHandle: true) => SetHandle(handle);

            protected override bool ReleaseHandle() => CloseHandle(handle);
        }
    }

    internal sealed class WindowsProcessTreeLaunch(
        Process process,
        Stream input,
        Stream output,
        Stream error,
        WindowsProcessTreeOwnership ownership) : IDisposable
    {
        public Process Process { get; } = process;
        public Stream Input { get; } = input;
        public Stream Output { get; } = output;
        public Stream Error { get; } = error;
        public WindowsProcessTreeOwnership Ownership { get; } = ownership;

        public void Dispose()
        {
            try
            {
                Input.Dispose();
            }
            finally
            {
                try
                {
                    Output.Dispose();
                }
                finally
                {
                    try
                    {
                        Error.Dispose();
                    }
                    finally
                    {
                        try
                        {
                            Process.Dispose();
                        }
                        finally
                        {
                            Ownership.Dispose();
                        }
                    }
                }
            }
        }
    }

    private sealed class PendingRequest(
        int sequence,
        string command,
        CapturedCallStackTarget? stackTraceTarget = null,
        StepOverRecord? stepOver = null,
        string? inspectionContextId = null,
        ContinueRecord? continueExecution = null,
        BreakpointUpdate? breakpointUpdate = null,
        PauseRecord? pause = null,
        InitialBreakpointRequest? initialBreakpoints = null)
    {
        public int Sequence { get; } = sequence;
        public string Command { get; } = command;
        public CapturedCallStackTarget? StackTraceTarget { get; } = stackTraceTarget;
        public StepOverRecord? StepOver { get; } = stepOver;
        public ContinueRecord? ContinueExecution { get; } = continueExecution;
        public BreakpointUpdate? BreakpointUpdate { get; } = breakpointUpdate;
        public PauseRecord? Pause { get; } = pause;
        public InitialBreakpointRequest? InitialBreakpoints { get; } = initialBreakpoints;
        public string? InspectionContextId { get; } = inspectionContextId;
        private int _writeStarted;
        private int _committedWrite;
        public bool WriteStarted => Volatile.Read(ref _writeStarted) != 0;
        public bool CommittedWrite => Volatile.Read(ref _committedWrite) != 0;
        public void MarkWriteStarted() => Volatile.Write(ref _writeStarted, 1);
        public void CommitWrite() => Volatile.Write(ref _committedWrite, 1);
        public TaskCompletionSource<DapResponse> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class PauseRecord : IDisposable
    {
        private readonly CancellationTokenSource _deadline;
        private readonly CancellationTokenRegistration _callerCancellation;

        internal PauseRecord(TimeSpan timeout, TimeProvider timeProvider, CancellationToken cancellationToken)
        {
            _deadline = new CancellationTokenSource(timeout, timeProvider);
            _callerCancellation = cancellationToken.Register(static state => ((CancellationTokenSource)state!).Cancel(), _deadline);
        }

        public CancellationToken DeadlineToken => _deadline.Token;
        public PendingRequest? Request { get; set; }
        public int? ThreadId { get; set; }
        public bool Finalized { get; set; }
        public bool AcknowledgementObserved { get; private set; }
        public DapPauseResult? Outcome { get; private set; }
        public Exception? ProtocolFailure { get; private set; }
        public TaskCompletionSource<bool> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void ObserveAcknowledgement(bool success)
        {
            if (!Finalized && Outcome is null && ProtocolFailure is null && !_deadline.IsCancellationRequested)
            {
                AcknowledgementObserved = true;
                Outcome = success
                    ? new DapPauseResult(DapPauseOutcome.Acknowledged, ThreadId)
                    : DapPauseResult.Refused;
                Completion.TrySetResult(true);
            }
        }

        public void ObserveTerminal(string terminal, int? exitCode)
        {
            if (!Finalized && Outcome is null && ProtocolFailure is null && !_deadline.IsCancellationRequested)
            {
                Outcome = new DapPauseResult(DapPauseOutcome.Terminal, Terminal: terminal, ExitCode: exitCode);
                Completion.TrySetResult(true);
            }
        }

        public void ObserveFailure(Exception failure)
        {
            if (!Finalized && Outcome is null && ProtocolFailure is null)
            {
                ProtocolFailure = failure;
                Completion.TrySetResult(true);
            }
        }

        public void Dispose()
        {
            _callerCancellation.Dispose();
            _deadline.Dispose();
        }
    }

    private sealed record DapPauseArguments([property: JsonPropertyName("threadId")] int ThreadId);

    private sealed class StepOverRecord(int threadId, DapStepKind kind, CancellationToken callerCancellation) : IDisposable
    {
        private bool _acknowledged;
        private int _writeStarted;
        private int _committedWrite;
        private int _requestSequence;
        private CancellationTokenSource? _deadline;
        private CancellationTokenRegistration _callerCancellationRegistration;

        public int ThreadId { get; } = threadId;
        public string Command { get; } = kind switch
        {
            DapStepKind.Over => "next",
            DapStepKind.Into => "stepIn",
            DapStepKind.Out => "stepOut",
            _ => throw new ArgumentOutOfRangeException(nameof(kind)),
        };
        public int RequestSequence => Volatile.Read(ref _requestSequence);
        public bool WriteStarted => Volatile.Read(ref _writeStarted) != 0;
        public bool CommittedWrite => Volatile.Read(ref _committedWrite) != 0;
        public CancellationToken DeadlineToken => (_deadline
            ?? throw new InvalidOperationException("The step-over write has not committed.")).Token;
        public TaskCompletionSource<DapStepOverResult> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void SetRequestSequence(int requestSequence) => Volatile.Write(ref _requestSequence, requestSequence);

        public void MarkWriteStarted() => Volatile.Write(ref _writeStarted, 1);

        public void Commit(TimeSpan timeout, TimeProvider timeProvider)
        {
            if (Interlocked.Exchange(ref _committedWrite, 1) != 0)
            {
                throw new InvalidOperationException("The step-over record was already committed.");
            }

            _deadline = new CancellationTokenSource(timeout, timeProvider);
            _callerCancellationRegistration = callerCancellation.Register(
                static state => ((CancellationTokenSource)state!).Cancel(), _deadline);
        }

        public void ObserveAcknowledgement(bool success)
        {
            if (_deadline?.IsCancellationRequested == true)
            {
                return;
            }

            if (success)
            {
                _acknowledged = true;
            }
            else
            {
                Completion.TrySetResult(DapStepOverResult.Refused);
            }
        }

        public void ObserveStopped(JsonElement message, string? reason, int? threadId)
        {
            if (_deadline?.IsCancellationRequested != true
                && _acknowledged
                && string.Equals(reason, "step", StringComparison.Ordinal)
                && threadId == ThreadId)
            {
                if (RequireInt32(message, "seq") <= 0)
                {
                    throw new InvalidDataException("DAP step stop event sequence must be positive.");
                }

                Completion.TrySetResult(DapStepOverResult.Succeeded(ThreadId));
            }
        }

        public void ObserveTerminal(string terminal, int? exitCode)
        {
            if (_deadline?.IsCancellationRequested != true)
            {
                Completion.TrySetResult(DapStepOverResult.ForTerminal(terminal, exitCode));
            }
        }

        public void Dispose()
        {
            _callerCancellationRegistration.Dispose();
            _deadline?.Dispose();
        }
    }

    private enum ExecutionAdmission
    {
        Available,
        Active,
        Closing,
    }

    private sealed class ContinueRecord(int threadId, CancellationToken callerCancellation) : IDisposable
    {
        private int _writeStarted;
        private int _committedWrite;
        private int _requestSequence;
        private CancellationTokenSource? _deadline;
        private CancellationTokenRegistration _callerCancellationRegistration;
        private DapContinueResult? _stoppedCandidate;

        public int ThreadId { get; } = threadId;
        public int RequestSequence => Volatile.Read(ref _requestSequence);
        public bool WriteStarted => Volatile.Read(ref _writeStarted) != 0;
        public bool CommittedWrite => Volatile.Read(ref _committedWrite) != 0;
        public bool Acknowledged { get; private set; }
        public bool HasObservedLifecycle { get; private set; }
        public bool Finalized { get; set; }
        public DapContinueResult? Outcome { get; private set; }
        public Exception? ProtocolFailure { get; private set; }
        public CancellationToken DeadlineToken => (_deadline
            ?? throw new InvalidOperationException("The continue write has not committed.")).Token;
        public TaskCompletionSource<bool> Completion { get; private set; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void SetRequestSequence(int sequence) => Volatile.Write(ref _requestSequence, sequence);
        public void MarkWriteStarted() => Volatile.Write(ref _writeStarted, 1);

        public void Commit(TimeProvider timeProvider)
        {
            _deadline = new CancellationTokenSource(ContinueTimeout, timeProvider);
            _callerCancellationRegistration = callerCancellation.Register(
                static state => ((CancellationTokenSource)state!).Cancel(), _deadline);
            Volatile.Write(ref _committedWrite, 1);
        }

        public void ObserveProtocolFailure(Exception failure)
        {
            if (!Finalized && ProtocolFailure is null && Outcome?.Outcome != DapContinueOutcome.Terminal)
            {
                ProtocolFailure = failure;
                _stoppedCandidate = null;
                Outcome = null;
                Completion.TrySetResult(true);
            }
        }

        public void ObserveAcknowledgement(bool success)
        {
            if (Finalized)
            {
                return;
            }

            if (success)
            {
                Acknowledged = true;
                if (ProtocolFailure is null && Outcome?.Outcome != DapContinueOutcome.Terminal && _stoppedCandidate is not null)
                {
                    Outcome = _stoppedCandidate;
                    Completion.TrySetResult(true);
                }
            }
            else if (ProtocolFailure is null && Outcome?.Outcome != DapContinueOutcome.Terminal)
            {
                Outcome = DapContinueResult.Refused;
                Completion.TrySetResult(true);
            }
        }

        public void ObserveStopped(string reason, int? stoppedThreadId, bool? allThreadsStopped)
        {
            if (Finalized || Outcome?.Outcome == DapContinueOutcome.Terminal)
            {
                return;
            }

            HasObservedLifecycle = true;
            if (ProtocolFailure is not null)
            {
                return;
            }

            _stoppedCandidate = new DapContinueResult(DapContinueOutcome.Stopped, stoppedThreadId, reason, allThreadsStopped);
            if (Acknowledged)
            {
                Outcome = _stoppedCandidate;
                Completion.TrySetResult(true);
            }
        }

        public void ObserveContinued()
        {
            if (!Finalized)
            {
                HasObservedLifecycle = true;
                _stoppedCandidate = null;
                if (Outcome?.Outcome == DapContinueOutcome.Stopped)
                {
                    Outcome = null;
                    if (Completion.Task.IsCompletedSuccessfully)
                    {
                        Completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
                    }
                }
            }
        }

        public void ObserveTerminal(string terminal, int? exitCode)
        {
            if (!Finalized)
            {
                HasObservedLifecycle = true;
                _stoppedCandidate = null;
                if (ProtocolFailure is null)
                {
                    Outcome = new DapContinueResult(DapContinueOutcome.Terminal, Terminal: terminal, ExitCode: exitCode);
                    Completion.TrySetResult(true);
                }
            }
        }

        public void Dispose()
        {
            _callerCancellationRegistration.Dispose();
            _deadline?.Dispose();
        }
    }

    private sealed record DapContinueArguments([property: JsonPropertyName("threadId")] int ThreadId);

    private sealed class CallStackEligibility;

    private readonly record struct DapStackTraceCapabilities(bool SupportsDelayedStackTraceLoading);

    private readonly record struct CapturedCallStackTarget(
        CallStackEligibility Eligibility,
        int ThreadId,
        bool SupportsPaging,
        string? InspectionContextId = null);

    private sealed record DapResponse(JsonElement Body, bool Success, bool TargetCurrent = true, string? InspectionContextId = null, string? SerializedContent = null);

    private sealed record DapSetBreakpointsArguments(
        [property: JsonPropertyName("source")] DapBreakpointSource Source,
        [property: JsonPropertyName("breakpoints")] IReadOnlyList<DapRequestedBreakpoint> Breakpoints);

    private sealed record DapBreakpointSource([property: JsonPropertyName("path")] string Path);

    private readonly record struct DapRequestedBreakpoint([property: JsonPropertyName("line")] int Line, [property: JsonIgnore] long Incarnation = 0);
    private sealed record InitialBreakpointRequest(string File, DapRequestedBreakpoint[] Requested);

    private readonly record struct DapBreakpointObservation(bool Verified, int? ActualLine);

    private sealed class BreakpointUpdate(string file, DapRequestedBreakpoint[] requested, int targetIndex, int countDelta, bool isRemoval = false)
    {
        private int _writeStarted;
        private int _committedWrite;
        private int _requestSequence;

        public string File { get; } = file;
        public DapRequestedBreakpoint[] Requested { get; } = requested;
        public int TargetIndex { get; } = targetIndex;
        public int CountDelta { get; } = countDelta;
        public bool IsRemoval { get; } = isRemoval;
        public string[]? ExceptionFilters { get; init; }
        public int RequestSequence => Volatile.Read(ref _requestSequence);
        public bool WriteStarted => Volatile.Read(ref _writeStarted) != 0;
        public bool CommittedWrite => Volatile.Read(ref _committedWrite) != 0;
        public bool Finalized { get; set; }
        public bool RequiresDrain { get; private set; }
        public bool IsSettled => Result is not null || Failure is not null;
        public DapBreakpointUpdateResult? Result { get; private set; }
        public Exception? Failure { get; private set; }
        public TaskCompletionSource<bool> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void SetRequestSequence(int sequence) => Volatile.Write(ref _requestSequence, sequence);
        public void MarkWriteStarted() => Volatile.Write(ref _writeStarted, 1);
        public void CommitWrite() => Volatile.Write(ref _committedWrite, 1);

        public void Complete(DapBreakpointUpdateResult result)
        {
            if (!Finalized && !IsSettled)
            {
                Result = result;
                Completion.TrySetResult(true);
            }
        }

        public void ObserveFailure(Exception failure)
        {
            if (Finalized)
            {
                return;
            }

            RequiresDrain = true;
            if (!IsSettled)
            {
                Failure = failure;
                Completion.TrySetResult(true);
            }
        }
    }
}

internal sealed class DapStartupCleanupException(Exception startupFailure, Exception cleanupFailure)
    : AggregateException("DAP startup cleanup failed.", cleanupFailure, startupFailure)
{
    internal Exception StartupFailure { get; } = startupFailure;
    internal Exception CleanupFailure { get; } = cleanupFailure;
}

internal sealed class DapLaunchRefusedException() : Exception("DAP launch request was refused.");

internal readonly record struct DapSourceBreakpoint(int Index, string File, int Line, long Incarnation = 0);
internal readonly record struct LegacyRequestedLine(int Line, long Incarnation);

internal readonly record struct DapInitialBreakpoint(
    [property: JsonPropertyName("index")] int Index,
    [property: JsonPropertyName("verified")] bool Verified,
    [property: JsonPropertyName("actualLine")]
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? ActualLine);

internal sealed record LegacyBreakpointObservation(string File, int Line, long Incarnation, bool Verified, int? DapLine, int? Id, long HitCount, bool Removed);

internal sealed class DapBreakpointsException(bool isRefused) : Exception(
    isRefused ? "DAP refused source breakpoint configuration." : "DAP source breakpoint configuration failed.")
{
    internal bool IsRefused { get; } = isRefused;
}

internal enum DapBreakpointUpdateOutcome
{
    Added,
    Removed,
    NotFound,
    Refused,
    Unsupported,
    Acknowledged,
    LimitReached,
    Unavailable,
}

internal sealed record DapBreakpointUpdateResult(
    DapBreakpointUpdateOutcome Outcome,
    bool Verified = false,
    int? ActualLine = null,
    bool RequiresDrain = false,
    bool[]? ExceptionVerification = null)
{
    internal static DapBreakpointUpdateResult Refused { get; } = new(DapBreakpointUpdateOutcome.Refused);
    internal static DapBreakpointUpdateResult NotFound { get; } = new(DapBreakpointUpdateOutcome.NotFound);
    internal static DapBreakpointUpdateResult LimitReached { get; } = new(DapBreakpointUpdateOutcome.LimitReached);
    internal static DapBreakpointUpdateResult Unavailable { get; } = new(DapBreakpointUpdateOutcome.Unavailable);
}

internal sealed class DapBreakpointCanceledException(bool requiresDrain, Exception innerException, CancellationToken cancellationToken)
    : OperationCanceledException("Breakpoint update was cancelled.", innerException, cancellationToken)
{
    internal bool RequiresDrain { get; } = requiresDrain;
}

internal enum DapStepKind
{
    Over,
    Into,
    Out,
}

internal enum DapStepOverOutcome
{
    Refused,
    Unsupported,
    Succeeded,
    Terminal,
    TimedOut,
}

internal sealed record DapStepOverResult(
    DapStepOverOutcome Outcome,
    int ThreadId,
    string? Terminal = null,
    int? ExitCode = null)
{
    internal static DapStepOverResult Refused { get; } = new(DapStepOverOutcome.Refused, 0);
    internal static DapStepOverResult Unsupported { get; } = new(DapStepOverOutcome.Unsupported, 0);
    internal static DapStepOverResult Succeeded(int threadId) => new(DapStepOverOutcome.Succeeded, threadId);
    internal static DapStepOverResult ForTerminal(string terminal, int? exitCode) => new(DapStepOverOutcome.Terminal, 0, terminal, exitCode);
    internal static DapStepOverResult TimedOut(int threadId) => new(DapStepOverOutcome.TimedOut, threadId);
}

internal enum DapContinueOutcome
{
    Refused,
    Stopped,
    Terminal,
    TimedOut,
}

internal sealed record DapContinueResult(
    DapContinueOutcome Outcome,
    int? ThreadId = null,
    string? Reason = null,
    bool? AllThreadsStopped = null,
    string? Terminal = null,
    int? ExitCode = null,
    bool Acknowledged = false,
    bool RequiresDrain = false)
{
    internal static DapContinueResult Refused { get; } = new(DapContinueOutcome.Refused);
}

internal sealed class DapContinueCanceledException(bool requiresDrain, Exception innerException, CancellationToken cancellationToken)
    : OperationCanceledException("Continue observation was cancelled.", innerException, cancellationToken)
{
    internal bool RequiresDrain { get; } = requiresDrain;
}

internal enum DapPauseOutcome
{
    Refused,
    Acknowledged,
    NoThreads,
    Terminal,
    TimedOut,
    ProtocolError,
}

internal sealed record DapPauseResult(
    DapPauseOutcome Outcome,
    int? ThreadId = null,
    string? Terminal = null,
    int? ExitCode = null,
    bool RequiresDrain = false)
{
    internal static DapPauseResult Refused { get; } = new(DapPauseOutcome.Refused);
}

internal sealed class DapPauseCanceledException(bool requiresDrain, Exception innerException, CancellationToken cancellationToken)
    : OperationCanceledException("Pause observation was cancelled.", innerException, cancellationToken)
{
    internal bool RequiresDrain { get; } = requiresDrain;
}

internal sealed record DapLaunchOptions(
    string? WorkingDirectory,
    string[]? Arguments,
    bool? StopAtEntry,
    bool? JustMyCode,
    IReadOnlyDictionary<string, string?>? Environment,
    string? DotnetHostPath = null);

internal sealed record DapLaunchArguments(
    [property: JsonPropertyName("program")] string Program,
    [property: JsonPropertyName("env"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] IReadOnlyDictionary<string, string>? Environment,
    [property: JsonPropertyName("cwd"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string? WorkingDirectory,
    [property: JsonPropertyName("args"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] string[]? Arguments,
    [property: JsonPropertyName("stopAtEntry"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] bool? StopAtEntry,
    [property: JsonPropertyName("justMyCode"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] bool? JustMyCode);

internal sealed record DapThread(
    [property: JsonPropertyName("id")] int Id,
    [property: JsonPropertyName("name")] string Name);

internal sealed record DapThreadsResult(bool IsRefused, IReadOnlyList<DapThread> Threads)
{
    internal static DapThreadsResult Refused { get; } = new(true, Array.Empty<DapThread>());

    internal static DapThreadsResult Succeeded(IReadOnlyList<DapThread> threads) => new(false, threads);
}

internal sealed record DapStackFrame(
    [property: JsonPropertyName("id")] int Id,
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("source")] string? Source,
    [property: JsonPropertyName("line")] long Line,
    [property: JsonPropertyName("column")] long Column);

internal sealed record DapCallStackResult(
    bool IsRefused, IReadOnlyList<DapStackFrame> Frames, uint? TotalFrames, string? InspectionContextId = null)
{
    internal static DapCallStackResult Refused { get; } = new(true, Array.Empty<DapStackFrame>(), null);

    internal static DapCallStackResult Succeeded(
        IReadOnlyList<DapStackFrame> frames, uint? totalFrames, string? inspectionContextId = null) =>
        new(false, frames, totalFrames, inspectionContextId);
}

internal sealed record DapVariablesArguments(
    [property: JsonPropertyName("variablesReference")] int VariablesReference,
    [property: JsonPropertyName("start"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Start,
    [property: JsonPropertyName("count"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Count);

internal sealed record DapScope(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("variablesReference")] int VariablesReference,
    [property: JsonPropertyName("expensive")] bool Expensive);

internal sealed record DapVariable(
    [property: JsonPropertyName("name")] string Name,
    [property: JsonPropertyName("value")] string Value,
    [property: JsonPropertyName("type")] string? Type,
    [property: JsonPropertyName("variablesReference")] int VariablesReference);

internal sealed record DapScopesResult(bool IsRefused, string? InspectionContextId, IReadOnlyList<DapScope> Scopes)
{
    internal static DapScopesResult Refused { get; } = new(true, null, Array.Empty<DapScope>());
}

internal enum DapStepTargetsOutcome { Success, Unsupported, Refused }
internal sealed record DapStepTargetsResult(DapStepTargetsOutcome Outcome, IReadOnlyList<DapStepTarget>? Targets);
internal sealed record DapStepTarget(
    [property: JsonPropertyName("id")] int Id,
    [property: JsonPropertyName("label")] string Label,
    [property: JsonPropertyName("line"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Line,
    [property: JsonPropertyName("column"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? Column,
    [property: JsonPropertyName("endLine"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? EndLine,
    [property: JsonPropertyName("endColumn"), JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? EndColumn);

internal sealed record DapVariablesResult(bool IsRefused, string? InspectionContextId, IReadOnlyList<DapVariable> Variables)
{
    internal static DapVariablesResult Refused { get; } = new(true, null, Array.Empty<DapVariable>());
}

internal enum DapExceptionInfoOutcome
{
    Success,
    Unsupported,
    Refused,
}

internal sealed record DapExceptionInfoResult(DapExceptionInfoOutcome Outcome, JsonElement Body = default)
{
    internal static DapExceptionInfoResult Unsupported { get; } = new(DapExceptionInfoOutcome.Unsupported);
    internal static DapExceptionInfoResult Refused { get; } = new(DapExceptionInfoOutcome.Refused);
}

internal enum DapEvaluateOutcome
{
    Success,
    Refused,
    Failed,
    OutcomeUnknown,
}

internal sealed record DapEvaluateResult(
    DapEvaluateOutcome Outcome,
    string? InspectionContextId = null,
    string? Result = null,
    string? Type = null,
    int VariablesReference = 0)
{
    internal static DapEvaluateResult Refused { get; } = new(DapEvaluateOutcome.Refused);
    internal static DapEvaluateResult Failed { get; } = new(DapEvaluateOutcome.Failed);
    internal static DapEvaluateResult OutcomeUnknown { get; } = new(DapEvaluateOutcome.OutcomeUnknown);
}

internal enum DapSetVariableOutcome { Success, Unsupported, Refused, Failed, OutcomeUnknown }

internal sealed record DapSetVariableResult(DapSetVariableOutcome Outcome, string? SerializedContent = null)
{
    internal static DapSetVariableResult Unsupported { get; } = new(DapSetVariableOutcome.Unsupported);
    internal static DapSetVariableResult Refused { get; } = new(DapSetVariableOutcome.Refused);
    internal static DapSetVariableResult Failed { get; } = new(DapSetVariableOutcome.Failed);
    internal static DapSetVariableResult OutcomeUnknown { get; } = new(DapSetVariableOutcome.OutcomeUnknown);
}

internal enum DapRuntimeInventoryStatus { Success, Incomplete, TooLarge }

internal readonly record struct DapRuntimeInventorySnapshot(DapRuntimeInventoryStatus Status, string? SerializedContent = null);
