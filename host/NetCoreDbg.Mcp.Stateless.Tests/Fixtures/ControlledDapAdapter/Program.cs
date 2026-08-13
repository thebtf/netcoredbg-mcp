using System.Diagnostics;
using System.Globalization;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Unicode;

if (args.Length == 1 && string.Equals(args[0], "--controlled-dap-descendant", StringComparison.Ordinal))
{
    await Task.Delay(Timeout.InfiniteTimeSpan);
    return;
}

var options = AdapterOptions.Parse(args, Environment.GetEnvironmentVariable("CONTROLLED_DAP_OPTIONS"));
var transcriptPath = Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT")
    ?? throw new InvalidOperationException("CONTROLLED_DAP_TRANSCRIPT is required.");
var adapter = new ControlledDapAdapter(options, transcriptPath, args);
await adapter.RunAsync(CancellationToken.None);

internal sealed record AdapterOptions(
    bool SupportsConfigurationDone,
    bool SupportsTerminate,
    bool IgnoreGracefulShutdown,
    bool SpawnDescendant,
    bool InitializedBeforeCorrectInitializeResponse,
    bool SuppressInitializedAfterInitializeResponse,
    bool SuppressLifecycleEvents,
    string StopReason,
    int ExitCode,
    bool BlockGracefulShutdown,
    bool HoldExitAfterDisconnectResponse,
    bool MalformedCapabilitiesEvent)
{
    public static AdapterOptions Parse(string[] args, string? environmentOptions)
    {
        var supportsConfigurationDone = false;
        var supportsTerminate = false;
        var ignoreGracefulShutdown = false;
        var spawnDescendant = false;
        var initializedBeforeCorrectInitializeResponse = false;
        var suppressInitializedAfterInitializeResponse = false;
        var suppressLifecycleEvents = false;
        var stopReason = "entry";
        var exitCode = 23;
        var blockGracefulShutdown = false;
        var holdExitAfterDisconnectResponse = false;
        var malformedCapabilitiesEvent = false;

        foreach (var argument in args.Concat(SplitOptions(environmentOptions)))
        {
            switch (argument)
            {
                case "--interpreter=vscode":
                    break;
                case "--supports-configuration-done":
                    supportsConfigurationDone = true;
                    break;
                case "--supports-terminate":
                    supportsTerminate = true;
                    break;
                case "--ignore-graceful-shutdown":
                    ignoreGracefulShutdown = true;
                    break;
                case "--spawn-descendant":
                    spawnDescendant = true;
                    break;
                case "--initialized-before-correct-initialize-response":
                    initializedBeforeCorrectInitializeResponse = true;
                    break;
                case "--suppress-initialized-after-initialize-response":
                    suppressInitializedAfterInitializeResponse = true;
                    break;
                case "--suppress-lifecycle-events":
                    suppressLifecycleEvents = true;
                    break;
                case "--block-graceful-shutdown":
                    blockGracefulShutdown = true;
                    break;
                case "--hold-exit-after-disconnect-response":
                    holdExitAfterDisconnectResponse = true;
                    break;
                case "--malformed-capabilities-event":
                    malformedCapabilitiesEvent = true;
                    break;
                case var _ when argument.StartsWith("--stop-reason=", StringComparison.Ordinal):
                    stopReason = argument["--stop-reason=".Length..];
                    break;
                case var _ when argument.StartsWith("--exit-code=", StringComparison.Ordinal)
                    && int.TryParse(argument["--exit-code=".Length..], NumberStyles.None, CultureInfo.InvariantCulture, out var parsedExitCode):
                    exitCode = parsedExitCode;
                    break;
                default:
                    throw new ArgumentException($"Unknown fixture option '{argument}'.", nameof(args));
            }
        }

        return new AdapterOptions(
            supportsConfigurationDone,
            supportsTerminate,
            ignoreGracefulShutdown,
            spawnDescendant,
            initializedBeforeCorrectInitializeResponse,
            suppressInitializedAfterInitializeResponse,
            suppressLifecycleEvents,
            stopReason,
            exitCode,
            blockGracefulShutdown,
            holdExitAfterDisconnectResponse,
            malformedCapabilitiesEvent);
    }

    private static IEnumerable<string> SplitOptions(string? options) =>
        string.IsNullOrWhiteSpace(options)
            ? []
            : options.Split(';', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries);
}

internal sealed class ControlledDapAdapter
{
    private static readonly Encoding HeaderEncoding = Encoding.ASCII;
    private static readonly Encoding BodyEncoding = new UTF8Encoding(encoderShouldEmitUTF8Identifier: false);
    private static readonly JsonSerializerOptions MessageSerializerOptions = new() { Encoder = JavaScriptEncoder.Create(UnicodeRanges.All) };

    private readonly AdapterOptions _options;
    private readonly string _transcriptPath;
    private readonly string[] _processArguments;
    private readonly Stream _input = Console.OpenStandardInput();
    private readonly Stream _output = Console.OpenStandardOutput();
    private readonly SemaphoreSlim _writeGate = new(1, 1);
    private DapFrame? _pendingLaunch;
    private readonly Queue<DapFrame> _deferredRequests = new();
    private readonly string? _gracefulReleasePath = Environment.GetEnvironmentVariable("CONTROLLED_DAP_GRACEFUL_RELEASE");
    private Process? _descendant;
    private Task? _lifecycleEvents;
    private static readonly TimeSpan InitializeGateWindow = TimeSpan.FromMilliseconds(75);
    private static readonly TimeSpan GracefulReleaseTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan DescendantCleanupTimeout = TimeSpan.FromSeconds(1);
    private Task<DapFrame?>? _nextRequest;
    private int _outgoingSequence = 1;

    public ControlledDapAdapter(AdapterOptions options, string transcriptPath, string[] processArguments)
    {
        _options = options;
        _transcriptPath = transcriptPath;
        _processArguments = processArguments;
    }

    public async Task RunAsync(CancellationToken cancellationToken)
    {
        await RecordAsync(new { kind = "startup", arguments = JsonSerializer.Serialize(_processArguments), processId = Environment.ProcessId }, cancellationToken);
        if (_options.SpawnDescendant)
        {
            _descendant = StartDescendant();
            await RecordAsync(new { kind = "descendant", processId = _descendant.Id }, cancellationToken);
        }

        try
        {
            while (await ReadNextFrameAsync(cancellationToken) is { } request)
            {
                try
                {
                    if (await HandleRequestAsync(request, cancellationToken))
                    {
                        return;
                    }
                }
                finally
                {
                    request.Document.Dispose();
                }
            }
        }
        finally
        {
            if (_descendant is { HasExited: false })
            {
                _descendant.Kill(entireProcessTree: true);
                using var descendantCleanup = new CancellationTokenSource(DescendantCleanupTimeout);
                await _descendant.WaitForExitAsync(descendantCleanup.Token);
            }

            _descendant?.Dispose();
            _writeGate.Dispose();
        }
    }

    private async Task<bool> HandleRequestAsync(DapFrame request, CancellationToken cancellationToken)
    {
        var root = request.Document.RootElement;
        var sequence = root.GetProperty("seq").GetInt32();
        var command = root.GetProperty("command").GetString()
            ?? throw new InvalidDataException("DAP request command is required.");
        var arguments = root.TryGetProperty("arguments", out var argumentsElement)
            ? argumentsElement.GetRawText()
            : "null";
        await RecordAsync(new
        {
            kind = "request",
            sequence,
            command,
            arguments,
            rawPayload = request.RawPayload,
            contentLength = request.ContentLength,
            payloadByteCount = request.PayloadByteCount,
        }, cancellationToken);

        switch (command)
        {
            case "initialize":
                await WriteEventAsync(
                    "capabilities",
                    _options.MalformedCapabilitiesEvent
                        ? new { capabilities = new { supportsTerminateRequest = "true" } }
                        : new { capabilities = new { supportsTerminateRequest = true } },
                    cancellationToken);
                await RecordAsync(new { kind = "capabilities-event" }, cancellationToken);
                await WriteResponseAsync(sequence + 10_000, command, body: null, cancellationToken);
                await RecordAsync(new { kind = "unmatched-response", requestSequence = sequence + 10_000 }, cancellationToken);
                if (_options.InitializedBeforeCorrectInitializeResponse)
                {
                    await WriteEventAsync("initialized", body: null, cancellationToken);
                    await RecordAsync(new { kind = "early-initialized-event" }, cancellationToken);
                }

                await DeferGateRequestAsync("before-initialize-response", cancellationToken);
                await WriteResponseAsync(sequence, command, new
                {
                    supportsConfigurationDoneRequest = _options.SupportsConfigurationDone,
                    supportsTerminateRequest = _options.SupportsTerminate,
                }, cancellationToken);
                await RecordAsync(new { kind = "initialize-response", requestSequence = sequence }, cancellationToken);
                await ProcessDeferredRequestsAsync(cancellationToken);
                await DeferGateRequestAsync("before-initialized-event", cancellationToken);
                if (!_options.SuppressInitializedAfterInitializeResponse)
                {
                    await WriteEventAsync("initialized", body: null, cancellationToken);
                    await RecordAsync(new { kind = "initialized-event" }, cancellationToken);
                }
                await ProcessDeferredRequestsAsync(cancellationToken);
                return false;
            case "launch":
                _pendingLaunch = request.Detach();
                await RecordAsync(new { kind = "launch-gated", sequence }, cancellationToken);
                if (!_options.SupportsConfigurationDone)
                {
                    await CompleteLaunchAsync(cancellationToken);
                }
                return false;
            case "configurationDone":
                await WriteResponseAsync(sequence, command, body: null, cancellationToken);
                await RecordAsync(new { kind = "configuration-done", sequence }, cancellationToken);
                await CompleteLaunchAsync(cancellationToken);
                return false;
            case "terminate":
                if (!_options.IgnoreGracefulShutdown)
                {
                    await WaitForGracefulReleaseAsync(_options.BlockGracefulShutdown, cancellationToken);
                    await WriteResponseAsync(sequence, command, body: null, cancellationToken);
                }

                return false;
            case "disconnect":
                if (!_options.IgnoreGracefulShutdown)
                {
                    await WaitForGracefulReleaseAsync(_options.BlockGracefulShutdown, cancellationToken);
                    await WriteResponseAsync(sequence, command, body: null, cancellationToken);
                    await RecordAsync(new { kind = "disconnect-response", sequence }, cancellationToken);
                    await WaitForGracefulReleaseAsync(_options.HoldExitAfterDisconnectResponse, cancellationToken);
                    return true;
                }

                return false;
            default:
                await WriteResponseAsync(sequence, command, body: null, cancellationToken);
                return false;
        }
    }

    private async Task CompleteLaunchAsync(CancellationToken cancellationToken)
    {
        if (_pendingLaunch is not { } launch)
        {
            return;
        }

        _pendingLaunch = null;
        await WriteResponseAsync(launch.Document.RootElement.GetProperty("seq").GetInt32(), "launch", body: null, cancellationToken);
        await RecordAsync(new { kind = "launch-released" }, cancellationToken);
        if (!_options.SuppressLifecycleEvents)
        {
            _lifecycleEvents ??= EmitLifecycleEventsAsync(cancellationToken);
        }

        launch.Document.Dispose();
    }

    private async Task EmitLifecycleEventsAsync(CancellationToken cancellationToken)
    {
        await WriteEventAsync("stopped", new { reason = _options.StopReason, threadId = 1 }, cancellationToken);
        await Task.Delay(TimeSpan.FromMilliseconds(75), cancellationToken);
        await WriteEventAsync("continued", new { threadId = 1, allThreadsContinued = true }, cancellationToken);
        await Task.Delay(TimeSpan.FromMilliseconds(75), cancellationToken);
        await WriteEventAsync("exited", new { exitCode = _options.ExitCode }, cancellationToken);
        await Task.Delay(TimeSpan.FromMilliseconds(75), cancellationToken);
        await WriteEventAsync("terminated", body: null, cancellationToken);
    }

    private static Process StartDescendant() =>
        Process.Start(new ProcessStartInfo
        {
            FileName = Environment.ProcessPath ?? throw new InvalidOperationException("Process path is unavailable."),
            UseShellExecute = false,
            CreateNoWindow = true,
            ArgumentList = { "--controlled-dap-descendant" },
        }) ?? throw new InvalidOperationException("Could not start controlled adapter descendant.");

    private async Task<DapFrame?> ReadNextFrameAsync(CancellationToken cancellationToken)
    {
        var nextRequest = _nextRequest ??= ReadFrameAsync(cancellationToken);
        try
        {
            return await nextRequest;
        }
        finally
        {
            _nextRequest = null;
        }
    }

    private async Task DeferGateRequestAsync(string stage, CancellationToken cancellationToken)
    {
        var request = await ProbeInitializeGateAsync(stage, cancellationToken);
        if (request is not null)
        {
            _deferredRequests.Enqueue(request);
        }
    }

    private async Task ProcessDeferredRequestsAsync(CancellationToken cancellationToken)
    {
        while (_deferredRequests.TryDequeue(out var request))
        {
            try
            {
                if (await HandleRequestAsync(request, cancellationToken))
                {
                    return;
                }
            }
            finally
            {
                request.Document.Dispose();
            }
        }
    }

    private async Task<DapFrame?> ProbeInitializeGateAsync(string stage, CancellationToken cancellationToken)
    {
        var nextRequest = _nextRequest ??= ReadFrameAsync(cancellationToken);
        var timeout = Task.Delay(InitializeGateWindow, cancellationToken);
        if (await Task.WhenAny(nextRequest, timeout) != nextRequest)
        {
            cancellationToken.ThrowIfCancellationRequested();
            await RecordAsync(new { kind = "initialize-gate", stage, command = (string?)null }, cancellationToken);
            return null;
        }

        var request = await nextRequest;
        _nextRequest = null;
        var command = request?.Document.RootElement.GetProperty("command").GetString();
        await RecordAsync(new { kind = "initialize-gate", stage, command }, cancellationToken);
        return request;
    }

    private async Task WaitForGracefulReleaseAsync(bool shouldWait, CancellationToken cancellationToken)
    {
        if (!shouldWait)
        {
            return;
        }

        if (string.IsNullOrWhiteSpace(_gracefulReleasePath))
        {
            throw new InvalidOperationException("CONTROLLED_DAP_GRACEFUL_RELEASE is required when graceful shutdown is blocked.");
        }

        using var timeout = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        timeout.CancelAfter(GracefulReleaseTimeout);
        while (!File.Exists(_gracefulReleasePath))
        {
            await Task.Delay(TimeSpan.FromMilliseconds(25), timeout.Token);
        }
    }

    private async Task<DapFrame?> ReadFrameAsync(CancellationToken cancellationToken)
    {
        var header = await ReadHeaderAsync(cancellationToken);
        if (header is null)
        {
            return null;
        }

        if (!header.StartsWith("Content-Length: ", StringComparison.Ordinal)
            || !int.TryParse(header.AsSpan("Content-Length: ".Length), NumberStyles.None, CultureInfo.InvariantCulture, out var contentLength)
            || contentLength < 0)
        {
            throw new InvalidDataException($"Invalid DAP header '{header}'.");
        }

        var payload = new byte[contentLength];
        await ReadExactlyAsync(_input, payload, cancellationToken);
        return new DapFrame(JsonDocument.Parse(payload), payload, BodyEncoding.GetString(payload), contentLength);
    }

    private async Task<string?> ReadHeaderAsync(CancellationToken cancellationToken)
    {
        using var header = new MemoryStream();
        while (true)
        {
            var value = await ReadByteAsync(_input, cancellationToken);
            if (value < 0)
            {
                return header.Length == 0 ? null : throw new EndOfStreamException("DAP header ended prematurely.");
            }

            header.WriteByte((byte)value);
            if (header.Length >= 4)
            {
                var buffer = header.GetBuffer();
                var length = checked((int)header.Length);
                if (buffer[length - 4] == '\r' && buffer[length - 3] == '\n'
                    && buffer[length - 2] == '\r' && buffer[length - 1] == '\n')
                {
                    return HeaderEncoding.GetString(buffer, 0, length - 4);
                }
            }
        }
    }

    private async Task WriteResponseAsync(int requestSequence, string command, object? body, CancellationToken cancellationToken) =>
        await WriteMessageAsync(new
        {
            seq = _outgoingSequence++,
            type = "response",
            request_seq = requestSequence,
            success = true,
            command,
            body,
        }, cancellationToken);

    private async Task WriteEventAsync(string eventName, object? body, CancellationToken cancellationToken) =>
        await WriteMessageAsync(new
        {
            seq = _outgoingSequence++,
            type = "event",
            @event = eventName,
            body,
        }, cancellationToken);

    private async Task WriteMessageAsync(object message, CancellationToken cancellationToken)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(message, MessageSerializerOptions);
        var header = HeaderEncoding.GetBytes($"Content-Length: {payload.Length}\r\n\r\n");
        await _writeGate.WaitAsync(cancellationToken);
        try
        {
            await _output.WriteAsync(header, cancellationToken);
            await _output.WriteAsync(payload, cancellationToken);
            await _output.FlushAsync(cancellationToken);
        }
        finally
        {
            _writeGate.Release();
        }
    }

    private async Task RecordAsync(object item, CancellationToken cancellationToken)
    {
        var line = JsonSerializer.Serialize(item) + Environment.NewLine;
        await File.AppendAllTextAsync(_transcriptPath, line, BodyEncoding, cancellationToken);
    }

    private static async Task<int> ReadByteAsync(Stream stream, CancellationToken cancellationToken)
    {
        var buffer = new byte[1];
        var read = await stream.ReadAsync(buffer, cancellationToken);
        return read == 0 ? -1 : buffer[0];
    }

    private static async Task ReadExactlyAsync(Stream stream, byte[] buffer, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer.AsMemory(offset), cancellationToken);
            if (read == 0)
            {
                throw new EndOfStreamException("DAP frame ended prematurely.");
            }

            offset += read;
        }
    }

    private sealed class DapFrame(JsonDocument document, byte[] rawPayloadBytes, string rawPayload, int contentLength)
    {
        public JsonDocument Document { get; private set; } = document;
        public byte[] RawPayloadBytes { get; } = rawPayloadBytes;
        public string RawPayload { get; } = rawPayload;
        public int ContentLength { get; } = contentLength;
        public int PayloadByteCount => RawPayloadBytes.Length;

        public DapFrame Detach()
        {
            var frame = new DapFrame(Document, RawPayloadBytes, RawPayload, ContentLength);
            Document = JsonDocument.Parse("null");
            return frame;
        }
    }
}
