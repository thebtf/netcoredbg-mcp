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
    private readonly ConcurrentDictionary<int, PendingRequest> _pending = new();
    private readonly TaskCompletionSource<bool> _initialized = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly object _stateGate = new();
    private readonly DapOutputBuffer _outputBuffer = new();
    private readonly object _cleanupGate = new();
    private readonly Task _readerTask;
    private readonly CancellationTokenSource _forceCleanup = new();
    private readonly Task _stderrTask;
    private readonly byte[] _headerByte = new byte[1];

    private DapSessionState _state = new(null, null, null);
    private Task? _cleanupTask;
    private Exception? _readerFailure;
    private NativeSceneTargetIdentity? _nativeSceneTargetIdentity;
    private readonly object _nativeSceneTargetIdentityGate = new();
    private int _nativeSceneCaptureAuthorityInvalidated;
    private int _nativeUiInvokeAuthorityInvalidated;
    private int _outgoingSequence;
    private bool _supportsConfigurationDone;
    private bool _supportsTerminate;
    private DapStackTraceCapabilities _stackTraceCapabilities;
    private CallStackEligibility? _allThreadsStoppedEligibility;
    private Dictionary<int, CallStackEligibility>? _threadEligibility;
    private HashSet<int>? _invalidatedAllThreadTargets;
    private string? _inspectionContextId;
    private bool _inspectionClosed;
    private int _inspectionWriteDrainRequired;
    private StepOverRecord? _activeStepOver;
    private int _stepOverDrainRequired;
    private ContinueRecord? _activeContinue;
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
        TimeProvider? stepOverTimeProvider = null)
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
    internal bool IsUsable => !_readerTask.IsCompleted && !HasExited();
    internal IReadOnlyList<DapInitialBreakpoint> InitialBreakpoints { get; private set; } = Array.Empty<DapInitialBreakpoint>();

    internal string GetOutput(bool clear, string? category) => _outputBuffer.Snapshot(clear, category);

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

    internal static async Task<NetCoreDbgSession> StartAsync(
        string debuggerPath,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        IReadOnlyList<DapSourceBreakpoint> breakpoints,
        CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(debuggerPath);
        ArgumentException.ThrowIfNullOrWhiteSpace(programPath);
        RequirePositiveTimeout(initializeTimeout, nameof(initializeTimeout));
        RequirePositiveTimeout(requestTimeout, nameof(requestTimeout));
        RequirePositiveTimeout(stopTimeout, nameof(stopTimeout));

        Process? process = null;
        IProcessTreeOwnership? processTreeOwnership = null;
        WindowsProcessTreeLaunch? windowsLaunch = null;
        NetCoreDbgSession? session = null;
        try
        {
            if (OperatingSystem.IsWindows())
            {
                windowsLaunch = WindowsProcessTreeOwnership.Start(debuggerPath);
                process = windowsLaunch.Process;
                session = new NetCoreDbgSession(
                    process,
                    windowsLaunch.Input,
                    windowsLaunch.Output,
                    windowsLaunch.Error,
                    requestTimeout,
                    stopTimeout,
                    windowsLaunch.Ownership);
                windowsLaunch = null;
            }
            else
            {
                var unixLaunch = UnixProcessGroupOwnership.Start(debuggerPath);
                process = unixLaunch.Process;
                processTreeOwnership = unixLaunch.Ownership;
                session = new NetCoreDbgSession(
                    process,
                    process.StandardInput.BaseStream,
                    process.StandardOutput.BaseStream,
                    process.StandardError.BaseStream,
                    requestTimeout,
                    stopTimeout,
                    processTreeOwnership);
            }

            await session.StartProtocolAsync(programPath, initializeTimeout, launchEnvironment, breakpoints, cancellationToken).ConfigureAwait(false);
            return session;
        }
        catch
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

        if (response.Body.ValueKind != JsonValueKind.Object
            || !response.Body.TryGetProperty("threads", out var threads)
            || threads.ValueKind != JsonValueKind.Array
            || threads.GetArrayLength() > MaximumThreadCount)
        {
            throw new InvalidDataException("DAP threads response body is invalid.");
        }

        var normalized = new DapThread[threads.GetArrayLength()];
        var index = 0;
        foreach (var thread in threads.EnumerateArray())
        {
            var id = RequireInt32(thread, "id");
            var name = RequireString(thread, "name");
            if (Encoding.UTF8.GetByteCount(name) > MaximumThreadNameBytes)
            {
                throw new InvalidDataException("DAP thread name exceeds the supported maximum.");
            }

            normalized[index++] = new DapThread(id, name);
        }

        return DapThreadsResult.Succeeded(normalized);
    }
    internal Task<DapCallStackResult> GetCallStackAsync(
        int threadId,
        uint startFrame,
        uint levels,
        CancellationToken cancellationToken) =>
        GetCallStackAsync(threadId, startFrame, levels, false, cancellationToken);

    internal async Task<DapCallStackResult> GetCallStackAsync(
        int threadId,
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
        CancellationToken cancellationToken)
    {
        var response = await SendInspectionRequestAsync(
            "variables", new { variablesReference }, inspectionContextId, cancellationToken).ConfigureAwait(false);
        if (response is null || !response.Success || !response.TargetCurrent)
        {
            return DapVariablesResult.Refused;
        }

        var variables = RequireInspectionArray(response.Body, "variables");
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

    internal async Task<DapStepOverResult> StepOverAsync(int threadId, CancellationToken cancellationToken)
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
                lock (_stateGate)
                {
                    if (_inspectionClosed || !HasStepOverAuthority(threadId))
                    {
                        return DapStepOverResult.Refused;
                    }

                    active = new StepOverRecord(threadId, cancellationToken);
                    _activeStepOver = active;
                    _state = _state with { CurrentThreadId = null };
                    ClearCallStackEligibility();
                    _inspectionContextId = null;
                }

                request = await BeginRequestAsync(
                    "next",
                    new { threadId },
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
                return DapStepOverResult.TimedOut(threadId);
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

                _executionAdmission = closeAdmission || RequiresStepOverDrain()
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
            if (_executionAdmission != ExecutionAdmission.Available || _inspectionClosed)
            {
                return false;
            }

            _executionAdmission = ExecutionAdmission.Active;
            return true;
        }
    }

    internal async Task<DapContinueResult> ContinueAsync(int threadId, CancellationToken cancellationToken)
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
                lock (_stateGate)
                {
                    if (_inspectionClosed || !HasStepOverAuthority(threadId))
                    {
                        return DapContinueResult.Refused;
                    }

                    active = new ContinueRecord(threadId, cancellationToken);
                    _activeContinue = active;
                    _state = new DapSessionState(null, null, null);
                    ClearCallStackEligibility();
                    _inspectionContextId = null;
                }

                request = await BeginRequestAsync(
                    "continue",
                    new DapContinueArguments(threadId),
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

                if (active is null || _executionAdmission == ExecutionAdmission.Active)
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
        _executionAdmission = requiresDrain ? ExecutionAdmission.Closing : ExecutionAdmission.Active;
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

    private async Task StartProtocolAsync(
        string programPath,
        TimeSpan initializeTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
        IReadOnlyList<DapSourceBreakpoint> breakpoints,
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
                supportsInvalidatedEvent = true,
            },
            initializeTimeout,
            cancellationToken).ConfigureAwait(false);

        await _initialized.Task.WaitAsync(initializeTimeout, cancellationToken).ConfigureAwait(false);
        await ConfigureBreakpointsAsync(breakpoints, cancellationToken).ConfigureAwait(false);

        object launchArguments = launchEnvironment is { Count: > 0 }
            ? new { program = programPath, env = launchEnvironment }
            : new { program = programPath };
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

            _ = await WaitForResponseAsync(launch, _requestTimeout, cancellationToken).ConfigureAwait(false);
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
                    requested[index] = new DapRequestedBreakpoint(fileBreakpoints[index].Line);
                }

                var response = await SendRequestAcceptingRefusalAsync(
                    "setBreakpoints",
                    new DapSetBreakpointsArguments(new DapBreakpointSource(file), requested),
                    _requestTimeout,
                    deadline.Token).ConfigureAwait(false);
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

    internal async Task<DapAddBreakpointResult> AddBreakpointAsync(string file, int line, CancellationToken cancellationToken)
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
                        return DapAddBreakpointResult.Refused;
                    }

                    var previous = _requestedBreakpoints.TryGetValue(file, out var existing)
                        ? existing
                        : Array.Empty<DapRequestedBreakpoint>();
                    var targetIndex = -1;
                    for (var index = 0; index < previous.Length; index++)
                    {
                        if (previous[index].Line == line)
                        {
                            targetIndex = index;
                            break;
                        }
                    }

                    var addsEntry = targetIndex < 0;
                    if (addsEntry && _requestedBreakpointCount >= MaximumBreakpointCount)
                    {
                        return DapAddBreakpointResult.LimitReached;
                    }

                    var requested = previous;
                    if (addsEntry)
                    {
                        targetIndex = previous.Length;
                        requested = new DapRequestedBreakpoint[previous.Length + 1];
                        Array.Copy(previous, requested, previous.Length);
                        requested[targetIndex] = new DapRequestedBreakpoint(line);
                    }

                    update = new BreakpointUpdate(file, requested, targetIndex, addsEntry);
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
        CancellationToken cancellationToken)
    {
        var request = await BeginRequestAsync(command, arguments, timeout, cancellationToken).ConfigureAwait(false);
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
        int threadId,
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
            var target = CaptureCallStackTarget(threadId, includeInspectionContext);
            if (target is null)
            {
                return (null, false);
            }

            isPaged = target.Value.SupportsPaging;
            object arguments = isPaged ? new { threadId, startFrame, levels } : new { threadId };
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
        BreakpointUpdate? breakpointUpdate = null)
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
            inspectionContextId ?? stackTraceTarget?.InspectionContextId, continueExecution, breakpointUpdate);
        if (!_pending.TryAdd(sequence, request))
        {
            throw new InvalidOperationException($"DAP request sequence '{sequence}' was reused.");
        }

        try
        {
            using var deadline = stepOver is null && continueExecution is null && breakpointUpdate is null
                ? CreateDeadline(timeout, cancellationToken) : null;
            await WriteMessageAsync(new
            {
                seq = sequence,
                type = "request",
                command,
                arguments,
            }, deadline?.Token ?? cancellationToken, stepOver,
                request.InspectionContextId is null ? null : request, continueExecution, breakpointUpdate).ConfigureAwait(false);
            return request;
        }
        catch (Exception exception)
        {
            if (exception is OperationCanceledException && request.InspectionContextId is not null && request.WriteStarted)
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
            await (_preHeaderWriteGate?.Invoke(cancellationToken) ?? Task.CompletedTask).ConfigureAwait(false);
            cancellationToken.ThrowIfCancellationRequested();
            stepOver?.MarkWriteStarted();
            continueExecution?.MarkWriteStarted();
            inspectionRequest?.MarkWriteStarted();
            breakpointUpdate?.MarkWriteStarted();
            await _input.WriteAsync(header, cancellationToken).ConfigureAwait(false);
            await _input.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
            await _input.FlushAsync(cancellationToken).ConfigureAwait(false);
            breakpointUpdate?.CommitWrite();
            stepOver?.Commit(_requestTimeout, _stepOverTimeProvider);
            continueExecution?.Commit(_stepOverTimeProvider);
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
                Volatile.Write(ref _readerFailure, failure);
                _initialized.TrySetException(failure);
                FailPending(failure);
                try
                {
                    ReaderFailed?.Invoke(this);
                }
                catch (Exception)
                {
                }
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
                HandleResponse(message);
                break;
            case "event":
                HandleEvent(message);
                break;
        }
    }

    private bool FailActiveStepOverForWrongCorrelation(int requestSequence, string? command)
    {
        if (!string.Equals(command, "next", StringComparison.Ordinal))
        {
            return false;
        }

        lock (_stateGate)
        {
            if (_activeStepOver is not { } active || active.RequestSequence == requestSequence)
            {
                return false;
            }

            active.Completion.TrySetException(new InvalidDataException(
                $"DAP next response for request '{requestSequence}' did not match active request '{active.RequestSequence}'."));
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
        if (!string.Equals(command, "setBreakpoints", StringComparison.Ordinal))
        {
            return false;
        }

        lock (_stateGate)
        {
            if (_activeBreakpointUpdate is not { Finalized: false } update || update.RequestSequence == requestSequence)
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
        if (FailActiveStepOverForWrongCorrelation(requestSequence, TryGetString(message, "command"))
            || FailActiveContinueForWrongCorrelation(requestSequence, TryGetString(message, "command"))
            || FailActiveBreakpointForWrongCorrelation(requestSequence, TryGetString(message, "command")))
        {
            return;
        }

        if (!_pending.TryGetValue(requestSequence, out var request))
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

        var body = message.TryGetProperty("body", out var bodyElement)
            ? bodyElement.Clone()
            : default;
        if (success.ValueKind == JsonValueKind.True && request.Command == "initialize")
        {
            ReadInitializeCapabilities(body);
            _initializeResponseObserved = true;
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
                    update.Complete(DapAddBreakpointResult.Refused);
                }
                else
                {
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

                    _requestedBreakpoints[update.File] = update.Requested;
                    if (update.AddsEntry)
                    {
                        _requestedBreakpointCount++;
                    }

                    update.Complete(new DapAddBreakpointResult(DapAddBreakpointOutcome.Added, selected.Verified, selected.ActualLine));
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

                if (acknowledged && !continued.HasObservedLifecycle)
                {
                    _state = _state with { Event = "continued", StopReason = null, CurrentThreadId = null };
                }

                continued.ObserveAcknowledgement(acknowledged);
            }

            var targetCurrent = (request.StackTraceTarget is not { } target || IsCallStackTargetCurrent(target))
                && (request.InspectionContextId is null
                    || (!_inspectionClosed && StringComparer.Ordinal.Equals(_inspectionContextId, request.InspectionContextId)));
            request.Completion.TrySetResult(new DapResponse(body, success.ValueKind == JsonValueKind.True,
                targetCurrent, request.InspectionContextId));
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
                break;
            case "output":
                HandleOutputEvent(body);
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
    }

    private void HandleOutputEvent(JsonElement body)
    {
        if (body.ValueKind != JsonValueKind.Object)
        {
            return;
        }

        var category = "console";
        if (body.TryGetProperty("category", out var categoryElement))
        {
            if (!TryDecodeOutputString(categoryElement, out category))
            {
                return;
            }

            if (category is not "console" and not "stdout" and not "stderr" and not "telemetry")
            {
                return;
            }
        }

        var text = string.Empty;
        if (body.TryGetProperty("output", out var outputElement) && !TryDecodeOutputString(outputElement, out text))
        {
            return;
        }

        _outputBuffer.Append(text, category);
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
            using var process = Process.GetProcessById(processId);
            var executablePath = process.MainModule?.FileName;
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
                    process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture)),
                executablePath,
                Convert.ToHexString(SHA256.HashData(executable)).ToLowerInvariant(),
                TryGetAssemblyVersion(executablePath),
                ProbeVersion: null);
            lock (_nativeSceneTargetIdentityGate)
            {
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
        lock (_stateGate)
        {
            _stackTraceCapabilities = capabilities;
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
                ClearCallStackEligibility();
                _inspectionContextId = null;
                if (allThreadsStopped)
                {
                    _allThreadsStoppedEligibility = new CallStackEligibility();
                }
                else if (threadId is { } stoppedThreadId)
                {
                    (_threadEligibility ??= new())[stoppedThreadId] = new CallStackEligibility();
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
                _inspectionContextId = null;
                if (!hasAllThreadsContinued || allThreadsContinued || threadId is null)
                {
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

    private bool HasStepOverAuthority(int threadId) =>
        (_allThreadsStoppedEligibility is not null
            && (_invalidatedAllThreadTargets is null || !_invalidatedAllThreadTargets.Contains(threadId)))
        || (_threadEligibility is not null && _threadEligibility.ContainsKey(threadId));

    private CapturedCallStackTarget? CaptureCallStackTarget(int threadId, bool includeInspectionContext)
    {
        lock (_stateGate)
        {
            if (_executionAdmission == ExecutionAdmission.Closing || (includeInspectionContext && _inspectionClosed))
            {
                return null;
            }

            var supportsPaging = _stackTraceCapabilities.SupportsDelayedStackTraceLoading;
            if (_allThreadsStoppedEligibility is { } allTargets
                && (_invalidatedAllThreadTargets is null || !_invalidatedAllThreadTargets.Contains(threadId)))
            {
                return new CapturedCallStackTarget(allTargets, threadId, supportsPaging,
                    includeInspectionContext ? GetOrCreateInspectionContext() : null);
            }

            return _threadEligibility is not null
                && _threadEligibility.TryGetValue(threadId, out var target)
                    ? new CapturedCallStackTarget(target, threadId, supportsPaging,
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

        public static UnixProcessGroupLaunch Start(string debuggerPath)
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
        private const uint ExtendedStartupInfoPresent = 0x00080000;
        private const uint HandleFlagInherit = 0x00000001;
        private const uint JobObjectExtendedLimitInformationClass = 9;
        private const uint JobObjectLimitKillOnJobClose = 0x00002000;
        private const uint StartfUseStdHandles = 0x00000100;

        private readonly SafeKernelHandle _job;

        private WindowsProcessTreeOwnership(SafeKernelHandle job) => _job = job;

        public static WindowsProcessTreeLaunch Start(string debuggerPath) =>
            StartCore(debuggerPath, ["--interpreter=vscode"], atomicJobAssignment: false);

        internal static WindowsProcessTreeLaunch Start(string executablePath, IReadOnlyList<string> arguments) =>
            StartCore(executablePath, arguments, atomicJobAssignment: true);

        private static WindowsProcessTreeLaunch StartCore(
            string executablePath, IReadOnlyList<string> arguments, bool atomicJobAssignment)
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(executablePath);
            ArgumentNullException.ThrowIfNull(arguments);
            executablePath = ResolveExecutablePath(executablePath);
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
                    atomicJobAssignment ? job : null);

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
                CloseHandle(processHandle);
                processHandle = IntPtr.Zero;
                var ownership = new WindowsProcessTreeOwnership(job);
                job = null;
                return new WindowsProcessTreeLaunch(process, input, output, error, ownership);
            }
            catch
            {
                if (processHandle != IntPtr.Zero)
                {
                    TerminateProcess(processHandle, 1);
                }

                input?.Dispose();
                output?.Dispose();
                error?.Dispose();
                process?.Dispose();
                job?.Dispose();
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
            SafeKernelHandle? atomicJob)
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
                        CreateNoWindow | CreateSuspended | ExtendedStartupInfoPresent,
                        IntPtr.Zero,
                        currentDirectory: null,
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
        private static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [DllImport("kernel32.dll", SetLastError = true)]
        private static extern uint ResumeThread(IntPtr thread);

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
        BreakpointUpdate? breakpointUpdate = null)
    {
        public int Sequence { get; } = sequence;
        public string Command { get; } = command;
        public CapturedCallStackTarget? StackTraceTarget { get; } = stackTraceTarget;
        public StepOverRecord? StepOver { get; } = stepOver;
        public ContinueRecord? ContinueExecution { get; } = continueExecution;
        public BreakpointUpdate? BreakpointUpdate { get; } = breakpointUpdate;
        public string? InspectionContextId { get; } = inspectionContextId;
        public bool WriteStarted { get; private set; }
        public void MarkWriteStarted() => WriteStarted = true;
        public TaskCompletionSource<DapResponse> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed class StepOverRecord(int threadId, CancellationToken callerCancellation) : IDisposable
    {
        private bool _acknowledged;
        private int _writeStarted;
        private int _committedWrite;
        private int _requestSequence;
        private CancellationTokenSource? _deadline;
        private CancellationTokenRegistration _callerCancellationRegistration;

        public int ThreadId { get; } = threadId;
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

    private sealed record DapResponse(JsonElement Body, bool Success, bool TargetCurrent = true, string? InspectionContextId = null);

    private sealed record DapSetBreakpointsArguments(
        [property: JsonPropertyName("source")] DapBreakpointSource Source,
        [property: JsonPropertyName("breakpoints")] IReadOnlyList<DapRequestedBreakpoint> Breakpoints);

    private sealed record DapBreakpointSource([property: JsonPropertyName("path")] string Path);

    private readonly record struct DapRequestedBreakpoint([property: JsonPropertyName("line")] int Line);

    private readonly record struct DapBreakpointObservation(bool Verified, int? ActualLine);

    private sealed class BreakpointUpdate(string file, DapRequestedBreakpoint[] requested, int targetIndex, bool addsEntry)
    {
        private int _writeStarted;
        private int _committedWrite;
        private int _requestSequence;

        public string File { get; } = file;
        public DapRequestedBreakpoint[] Requested { get; } = requested;
        public int TargetIndex { get; } = targetIndex;
        public bool AddsEntry { get; } = addsEntry;
        public int RequestSequence => Volatile.Read(ref _requestSequence);
        public bool WriteStarted => Volatile.Read(ref _writeStarted) != 0;
        public bool CommittedWrite => Volatile.Read(ref _committedWrite) != 0;
        public bool Finalized { get; set; }
        public bool RequiresDrain { get; private set; }
        public bool IsSettled => Result is not null || Failure is not null;
        public DapAddBreakpointResult? Result { get; private set; }
        public Exception? Failure { get; private set; }
        public TaskCompletionSource<bool> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        public void SetRequestSequence(int sequence) => Volatile.Write(ref _requestSequence, sequence);
        public void MarkWriteStarted() => Volatile.Write(ref _writeStarted, 1);
        public void CommitWrite() => Volatile.Write(ref _committedWrite, 1);

        public void Complete(DapAddBreakpointResult result)
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

internal readonly record struct DapSourceBreakpoint(int Index, string File, int Line);

internal readonly record struct DapInitialBreakpoint(
    [property: JsonPropertyName("index")] int Index,
    [property: JsonPropertyName("verified")] bool Verified,
    [property: JsonPropertyName("actualLine")]
    [property: JsonIgnore(Condition = JsonIgnoreCondition.WhenWritingNull)] int? ActualLine);

internal sealed class DapBreakpointsException(bool isRefused) : Exception(
    isRefused ? "DAP refused source breakpoint configuration." : "DAP source breakpoint configuration failed.")
{
    internal bool IsRefused { get; } = isRefused;
}

internal enum DapAddBreakpointOutcome
{
    Added,
    Refused,
    LimitReached,
}

internal sealed record DapAddBreakpointResult(
    DapAddBreakpointOutcome Outcome,
    bool Verified = false,
    int? ActualLine = null,
    bool RequiresDrain = false)
{
    internal static DapAddBreakpointResult Refused { get; } = new(DapAddBreakpointOutcome.Refused);
    internal static DapAddBreakpointResult LimitReached { get; } = new(DapAddBreakpointOutcome.LimitReached);
}

internal sealed class DapBreakpointCanceledException(bool requiresDrain, Exception innerException, CancellationToken cancellationToken)
    : OperationCanceledException("Breakpoint update was cancelled.", innerException, cancellationToken)
{
    internal bool RequiresDrain { get; } = requiresDrain;
}

internal enum DapStepOverOutcome
{
    Refused,
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

internal sealed record DapVariablesResult(bool IsRefused, string? InspectionContextId, IReadOnlyList<DapVariable> Variables)
{
    internal static DapVariablesResult Refused { get; } = new(true, null, Array.Empty<DapVariable>());
}
