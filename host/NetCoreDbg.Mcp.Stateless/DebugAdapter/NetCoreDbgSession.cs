using System.Collections.Concurrent;
using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Unicode;
using System.Text.Json;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed class NetCoreDbgSession : IAsyncDisposable
{
    private const int MaximumHeaderBytes = 16 * 1024;
    private const int MaximumPayloadBytes = 16 * 1024 * 1024;
    private static readonly Encoding HeaderEncoding = Encoding.ASCII;
    private static readonly JsonSerializerOptions JsonOptions = new()
    {
        Encoder = JavaScriptEncoder.Create(UnicodeRanges.All),
    };

    private readonly Process _process;
    private readonly Stream _input;
    private readonly Stream _output;
    private readonly TimeSpan _requestTimeout;
    private readonly TimeSpan _stopTimeout;
    private readonly SemaphoreSlim _writeGate = new(1, 1);
    private readonly ConcurrentDictionary<int, PendingRequest> _pending = new();
    private readonly TaskCompletionSource<bool> _initialized = new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly object _stateGate = new();
    private readonly object _cleanupGate = new();
    private readonly Task _readerTask;
    private readonly Task _stderrTask;
    private readonly byte[] _headerByte = new byte[1];

    private DapSessionState _state = new(null, null, null);
    private Task? _cleanupTask;
    private int _outgoingSequence;
    private bool _supportsConfigurationDone;
    private bool _supportsTerminate;
    private bool _initializeResponseObserved;
    private bool CapabilitiesObserved { get; set; }
    private NetCoreDbgSession(Process process, TimeSpan requestTimeout, TimeSpan stopTimeout)
    {
        _process = process;
        _input = process.StandardInput.BaseStream;
        _output = process.StandardOutput.BaseStream;
        _requestTimeout = RequirePositiveTimeout(requestTimeout, nameof(requestTimeout));
        _stopTimeout = RequirePositiveTimeout(stopTimeout, nameof(stopTimeout));
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

    internal static async Task<NetCoreDbgSession> StartAsync(
        string debuggerPath,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(debuggerPath);
        ArgumentException.ThrowIfNullOrWhiteSpace(programPath);
        RequirePositiveTimeout(initializeTimeout, nameof(initializeTimeout));
        RequirePositiveTimeout(requestTimeout, nameof(requestTimeout));
        RequirePositiveTimeout(stopTimeout, nameof(stopTimeout));

        Process? process = null;
        NetCoreDbgSession? session = null;
        try
        {
            var startInfo = new ProcessStartInfo
            {
                FileName = debuggerPath,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            startInfo.ArgumentList.Add("--interpreter=vscode");

            process = Process.Start(startInfo)
                ?? throw new InvalidOperationException($"Could not start debugger '{debuggerPath}'.");
            session = new NetCoreDbgSession(process, requestTimeout, stopTimeout);
            await session.StartProtocolAsync(programPath, initializeTimeout, cancellationToken).ConfigureAwait(false);
            return session;
        }
        catch
        {
            if (session is not null)
            {
                await session.EnsureCleanupAsync().ConfigureAwait(false);
            }
            else if (process is not null)
            {
                await StopUnmanagedProcessAsync(process, stopTimeout).ConfigureAwait(false);
            }

            throw;
        }
    }

    internal async Task StopAsync(CancellationToken cancellationToken) =>
        await EnsureCleanupAsync().WaitAsync(cancellationToken).ConfigureAwait(false);

    public ValueTask DisposeAsync() => new(EnsureCleanupAsync());

    private async Task StartProtocolAsync(string programPath, TimeSpan initializeTimeout, CancellationToken cancellationToken)
    {
        var initialize = await SendRequestAsync(
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

        ReadInitializeCapabilities(initialize.Body);
        await _initialized.Task.WaitAsync(initializeTimeout, cancellationToken).ConfigureAwait(false);

        var launch = await BeginRequestAsync(
            "launch",
            new { program = programPath },
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
                _initialized.TrySetException(failure);
                FailPending(failure);
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

        if (!message.TryGetProperty("success", out var success) || success.ValueKind != JsonValueKind.True)
        {
            request.Completion.TrySetException(new InvalidDataException(
                $"DAP request '{request.Command}' failed or returned an invalid success flag."));
            return;
        }

        var body = message.TryGetProperty("body", out var bodyElement)
            ? bodyElement.Clone()
            : default;
        if (request.Command == "initialize")
        {
            _initializeResponseObserved = true;
        }

        request.Completion.TrySetResult(new DapResponse(body));
    }

    private void HandleEvent(JsonElement message)
    {
        var eventName = RequireString(message, "event");
        var body = message.TryGetProperty("body", out var bodyElement) && bodyElement.ValueKind == JsonValueKind.Object
            ? bodyElement
            : default;

        switch (eventName)
        {
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

    private void ReadInitializeCapabilities(JsonElement body)
    {
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
            if (!HasExited())
            {
                if (_supportsTerminate)
                {
                    await TrySendCleanupRequestAsync("terminate", new { restart = false }).ConfigureAwait(false);
                }

                await TrySendCleanupRequestAsync(
                    "disconnect",
                    new { restart = false, terminateDebuggee = true }).ConfigureAwait(false);
                _input.Dispose();

                if (!await WaitForProcessExitAsync().ConfigureAwait(false))
                {
                    KillProcessTree();
                    if (!await WaitForProcessExitAsync().ConfigureAwait(false))
                    {
                        throw new InvalidOperationException("The owned debugger process did not exit after its process tree was killed.");
                    }
                }
            }
        }
        finally
        {
            _input.Dispose();
            await ObserveBackgroundTasksAsync().ConfigureAwait(false);
            _writeGate.Dispose();
            _process.Dispose();
        }
    }

    private async Task TrySendCleanupRequestAsync(string command, object arguments)
    {
        try
        {
            _ = await SendRequestAsync(command, arguments, _requestTimeout, CancellationToken.None).ConfigureAwait(false);
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
        while (await _process.StandardError.BaseStream.ReadAsync(buffer).ConfigureAwait(false) != 0)
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

    private static async Task StopUnmanagedProcessAsync(Process process, TimeSpan stopTimeout)
    {
        try
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync().WaitAsync(RequirePositiveTimeout(stopTimeout, nameof(stopTimeout))).ConfigureAwait(false);
            }
        }
        finally
        {
            process.Dispose();
        }
    }

    private sealed class PendingRequest(int sequence, string command)
    {
        public int Sequence { get; } = sequence;
        public string Command { get; } = command;
        public TaskCompletionSource<DapResponse> Completion { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private sealed record DapResponse(JsonElement Body);
}
