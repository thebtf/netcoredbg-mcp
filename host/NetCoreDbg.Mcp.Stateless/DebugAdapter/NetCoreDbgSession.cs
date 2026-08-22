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
    private readonly ConcurrentDictionary<int, PendingRequest> _pending = new();
    private readonly TaskCompletionSource<bool> _initialized = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly object _stateGate = new();
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
    private int _outgoingSequence;
    private bool _supportsConfigurationDone;
    private bool _supportsTerminate;
    private bool _initializeResponseObserved;
    private bool CapabilitiesObserved { get; set; }
    private readonly TaskCompletionSource<bool>? _initializeResponseContinuationReached;
    private readonly Task? _initializeResponseContinuationRelease;
    private NetCoreDbgSession(
        Process process,
        Stream input,
        Stream output,
        Stream error,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        IProcessTreeOwnership? processTreeOwnership,
        TaskCompletionSource<bool>? initializeResponseContinuationReached = null,
        Task? initializeResponseContinuationRelease = null)
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

    internal event Action<NetCoreDbgSession>? ReaderFailed;
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

    internal static async Task<NetCoreDbgSession> StartAsync(
        string debuggerPath,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
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

            await session.StartProtocolAsync(programPath, initializeTimeout, launchEnvironment, cancellationToken).ConfigureAwait(false);
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

    public ValueTask DisposeAsync() => new(EnsureCleanupAsync());

    private Task StartProtocolAsync(string programPath, TimeSpan initializeTimeout, CancellationToken cancellationToken) =>
        StartProtocolAsync(programPath, initializeTimeout, launchEnvironment: null, cancellationToken: cancellationToken);

    private async Task StartProtocolAsync(
        string programPath,
        TimeSpan initializeTimeout,
        IReadOnlyDictionary<string, string>? launchEnvironment,
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
            },
            initializeTimeout,
            cancellationToken).ConfigureAwait(false);

        await _initialized.Task.WaitAsync(initializeTimeout, cancellationToken).ConfigureAwait(false);

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

    private async Task<PendingRequest> BeginRequestAsync(
        string command,
        object? arguments,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        if (Volatile.Read(ref _readerFailure) is { } readerFailure)
        {
            throw new IOException("The debugger DAP reader is no longer available.", readerFailure);
        }

        var sequence = Interlocked.Increment(ref _outgoingSequence);
        var request = new PendingRequest(sequence, command);
        if (!_pending.TryAdd(sequence, request))
        {
            throw new InvalidOperationException($"DAP request sequence '{sequence}' was reused.");
        }

        try
        {
            using var deadline = CreateDeadline(timeout, cancellationToken);
            await WriteMessageAsync(new
            {
                seq = sequence,
                type = "request",
                command,
                arguments,
            }, deadline.Token).ConfigureAwait(false);
            return request;
        }
        catch (Exception exception)
        {
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

    private async Task WriteMessageAsync(object message, CancellationToken cancellationToken)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(message, JsonOptions);
        var header = HeaderEncoding.GetBytes($"Content-Length: {payload.Length}\r\n\r\n");
        await _writeGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            await _input.WriteAsync(header, cancellationToken).ConfigureAwait(false);
            await _input.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
            await _input.FlushAsync(cancellationToken).ConfigureAwait(false);
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

    private void HandleResponse(JsonElement message)
    {
        var requestSequence = RequireInt32(message, "request_seq");
        if (!_pending.TryGetValue(requestSequence, out var request))
        {
            return;
        }

        var command = RequireString(message, "command");
        if (!string.Equals(command, request.Command, StringComparison.Ordinal))
        {
            request.Completion.TrySetException(new InvalidDataException(
                $"DAP response for request '{requestSequence}' named '{command}' instead of '{request.Command}'."));
            return;
        }

        if (!message.TryGetProperty("success", out var success)
            || (success.ValueKind != JsonValueKind.True && success.ValueKind != JsonValueKind.False))
        {
            request.Completion.TrySetException(new InvalidDataException(
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

        request.Completion.TrySetResult(new DapResponse(body, success.ValueKind == JsonValueKind.True));
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
                UpdateState(current => current with
                {
                    Event = eventName,
                    StopReason = TryGetString(body, "reason"),
                });
                break;
            case "continued":
                UpdateState(current => current with { Event = eventName });
                break;
            case "exited":
                UpdateState(current => current with
                {
                    Event = eventName,
                    ExitCode = TryGetInt32(body, "exitCode"),
                });
                break;
            case "terminated":
                UpdateState(current => current with { Event = eventName });
                break;
        }
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
            return;
        }

        if (body.ValueKind != JsonValueKind.Object)
        {
            throw new InvalidDataException("The initialize response body must be a JSON object.");
        }

        _supportsConfigurationDone = ReadOptionalBoolean(body, "supportsConfigurationDoneRequest");
        _supportsTerminate = ReadOptionalBoolean(body, "supportsTerminateRequest");
    }

    private void UpdateState(Func<DapSessionState, DapSessionState> update)
    {
        lock (_stateGate)
        {
            _state = update(_state);
        }
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

    private void FailPending(Exception failure)
    {
        foreach (var pending in _pending)
        {
            if (_pending.TryRemove(pending.Key, out var request))
            {
                request.Completion.TrySetException(failure);
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

    private interface IProcessTreeOwnership : IDisposable
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

    private sealed class WindowsProcessTreeOwnership : IProcessTreeOwnership
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

        public static WindowsProcessTreeLaunch Start(string debuggerPath)
        {
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
                var commandLine = new StringBuilder($"{QuoteCommandLineArgument(debuggerPath)} --interpreter=vscode");
                var processInformation = CreateProcessWithStandardHandles(
                    debuggerPath,
                    commandLine,
                    childInput.DangerousGetHandle(),
                    childOutput.DangerousGetHandle(),
                    childError.DangerousGetHandle());

                processHandle = processInformation.Process;
                threadHandle = processInformation.Thread;
                childInput.Dispose();
                childInput = null;
                childOutput.Dispose();
                childOutput = null;
                childError.Dispose();
                childError = null;

                if (!AssignProcessToJobObject(job.DangerousGetHandle(), processHandle))
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
                input = new FileStream(standardInput!, FileAccess.Write, bufferSize: 4096, isAsync: false);
                standardInput = null;
                output = new FileStream(standardOutput!, FileAccess.Read, bufferSize: 4096, isAsync: false);
                standardOutput = null;
                error = new FileStream(standardError!, FileAccess.Read, bufferSize: 4096, isAsync: false);
                standardError = null;
                CloseHandle(processHandle);
                processHandle = IntPtr.Zero;
                var ownership = new WindowsProcessTreeOwnership(job!);
                job = null;
                return new WindowsProcessTreeLaunch(process!, input!, output!, error!, ownership);
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
            IntPtr standardError)
        {
            IntPtr attributeList = IntPtr.Zero;
            IntPtr inheritedHandleList = IntPtr.Zero;
            var attributeListInitialized = false;
            try
            {
                var attributeListSize = IntPtr.Zero;
                _ = InitializeProcThreadAttributeList(IntPtr.Zero, 1, 0, ref attributeListSize);
                if (attributeListSize == IntPtr.Zero)
                {
                    throw LastWin32Error("Could not allocate debugger handle inheritance attributes.");
                }

                attributeList = Marshal.AllocHGlobal(attributeListSize);
                if (!InitializeProcThreadAttributeList(attributeList, 1, 0, ref attributeListSize))
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

    private sealed class WindowsProcessTreeLaunch(
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

    private sealed class PendingRequest(int sequence, string command)
    {
        public int Sequence { get; } = sequence;
        public string Command { get; } = command;
        public TaskCompletionSource<DapResponse> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed record DapResponse(JsonElement Body, bool Success);
}

internal sealed record DapThread(
    [property: JsonPropertyName("id")] int Id,
    [property: JsonPropertyName("name")] string Name);

internal sealed record DapThreadsResult(bool IsRefused, IReadOnlyList<DapThread> Threads)
{
    internal static DapThreadsResult Refused { get; } = new(true, Array.Empty<DapThread>());

    internal static DapThreadsResult Succeeded(IReadOnlyList<DapThread> threads) => new(false, threads);
}
