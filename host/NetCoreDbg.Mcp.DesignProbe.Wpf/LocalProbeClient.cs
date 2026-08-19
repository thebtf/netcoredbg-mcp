using System.IO;
using System.Buffers;
using System.Buffers.Binary;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace NetCoreDbg.Mcp.DesignProbe.Wpf;

public sealed class LocalProbeClientOptions
{
    public TimeSpan ConnectTimeout { get; init; } = TimeSpan.FromSeconds(5);

    public TimeSpan WriteTimeout { get; init; } = TimeSpan.FromSeconds(5);

    public TimeSpan ReadTimeout { get; init; } = TimeSpan.FromSeconds(5);

    public int MaximumRequestBytes { get; init; } = 64 * 1024;

    public int MaximumResponseBytes { get; init; } = 1024 * 1024;
}

public sealed class LocalProbeClient : IAsyncDisposable, IDisposable
{
    public const string PipeNameEnvironmentVariable = "NETCOREDBG_MCP_NATIVE_SCENE_PROBE_PIPE";
    public const string NonceEnvironmentVariable = "NETCOREDBG_MCP_NATIVE_SCENE_PROBE_NONCE";
    public const string CaptureOperation = "capture";

    private const int MinimumAuthorizationTokenLength = 22;
    private const int MaximumAuthorizationTokenLength = 86;
    private const int MaximumPipeNameLength = 256;
    private const int MaximumControlFrameBytes = 4 * 1024 * 1024;

    private readonly WpfAtomicSnapshotTransaction _transaction;
    private readonly string _pipeName;
    private readonly string _nonce;
    private readonly TimeSpan _connectTimeout;
    private readonly TimeSpan _writeTimeout;
    private readonly TimeSpan _readTimeout;
    private readonly int _maximumRequestBytes;
    private readonly int _maximumResponseBytes;
    private readonly CancellationTokenSource _stopping = new();
    private readonly object _pipeLock = new();

    private NamedPipeClientStream? _pipe;
    private Task _runTask = Task.CompletedTask;
    private int _disposed;

    private LocalProbeClient(
        WpfAtomicSnapshotTransaction transaction,
        string pipeName,
        string nonce,
        LocalProbeClientOptions options)
    {
        _transaction = transaction ?? throw new ArgumentNullException(nameof(transaction));
        _pipeName = pipeName;
        _nonce = nonce;
        ValidateOptions(options);
        _connectTimeout = options.ConnectTimeout;
        _writeTimeout = options.WriteTimeout;
        _readTimeout = options.ReadTimeout;
        _maximumRequestBytes = options.MaximumRequestBytes;
        _maximumResponseBytes = options.MaximumResponseBytes;
    }

    public static LocalProbeClient? TryStartFromEnvironment(
        WpfAtomicSnapshotTransaction transaction,
        LocalProbeClientOptions? options = null)
    {
        ArgumentNullException.ThrowIfNull(transaction);
        if (!TryReadAuthorization(out var pipeName, out var nonce))
        {
            return null;
        }

        var client = new LocalProbeClient(transaction, pipeName, nonce, options ?? new LocalProbeClientOptions());
        client._runTask = Task.Run(client.RunAsync);
        return client;
    }

    public void Dispose()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 0)
        {
            _stopping.Cancel();
            ClosePipe();
        }
    }

    public async ValueTask DisposeAsync()
    {
        Dispose();
        await _runTask.ConfigureAwait(false);
    }

    private async Task RunAsync()
    {
        var pipe = new NamedPipeClientStream(
            serverName: ".",
            pipeName: _pipeName,
            direction: PipeDirection.InOut,
            options: PipeOptions.Asynchronous);
        try
        {
            using (var connectCancellation = CreatePhaseCancellation(_connectTimeout))
            {
                await pipe.ConnectAsync(connectCancellation.Token).ConfigureAwait(false);
            }

            if (!pipe.IsConnected || Volatile.Read(ref _disposed) != 0)
            {
                return;
            }

            SetPipe(pipe);
            while (!_stopping.IsCancellationRequested)
            {
                byte[] requestFrame;
                using (var readCancellation = CreatePhaseCancellation(_readTimeout))
                {
                    requestFrame = await ReadFrameAsync(pipe, _maximumRequestBytes, readCancellation.Token).ConfigureAwait(false);
                }

                if (!TryReadCaptureRequest(requestFrame, out var correlationId))
                {
                    return;
                }

                var snapshot = _transaction.Capture();
                var responseFrame = SerializeResponse(correlationId, snapshot);
                using var writeCancellation = CreatePhaseCancellation(_writeTimeout);
                await WriteFrameAsync(pipe, responseFrame, writeCancellation.Token).ConfigureAwait(false);
            }
        }
        catch (OperationCanceledException) when (_stopping.IsCancellationRequested)
        {
        }
        catch
        {
        }
        finally
        {
            ClosePipe();
            pipe.Dispose();
        }
    }

    private bool TryReadCaptureRequest(ReadOnlyMemory<byte> requestFrame, out string correlationId)
    {
        try
        {
            using var document = JsonDocument.Parse(requestFrame, new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 16,
            });
            var envelope = document.RootElement;
            if (envelope.ValueKind != JsonValueKind.Object ||
                !TryGetString(envelope, "nonce", out var nonce) ||
                !FixedTimeEquals(_nonce, nonce) ||
                !TryGetToken(envelope, "correlationId", out correlationId) ||
                !envelope.TryGetProperty("request", out var request) ||
                request.ValueKind != JsonValueKind.Object ||
                !TryGetString(request, "operation", out var operation) ||
                !StringComparer.Ordinal.Equals(operation, CaptureOperation))
            {
                correlationId = string.Empty;
                return false;
            }

            return true;
        }
        catch (JsonException)
        {
            correlationId = string.Empty;
            return false;
        }
    }

    private ReadOnlyMemory<byte> SerializeResponse(string correlationId, WpfSceneSnapshotDto snapshot)
    {
        var buffer = new BoundedBufferWriter(_maximumResponseBytes);
        using var writer = new Utf8JsonWriter(buffer);
        JsonSerializer.Serialize(writer, new ProbeResponseEnvelope(_nonce, correlationId, snapshot));
        writer.Flush();
        return buffer.WrittenMemory;
    }

    private void SetPipe(NamedPipeClientStream pipe)
    {
        lock (_pipeLock)
        {
            if (Volatile.Read(ref _disposed) != 0)
            {
                throw new ObjectDisposedException(nameof(LocalProbeClient));
            }

            _pipe = pipe;
        }
    }

    private void ClosePipe()
    {
        NamedPipeClientStream? pipe;
        lock (_pipeLock)
        {
            pipe = _pipe;
            _pipe = null;
        }

        pipe?.Dispose();
    }

    private CancellationTokenSource CreatePhaseCancellation(TimeSpan timeout)
    {
        var cancellation = CancellationTokenSource.CreateLinkedTokenSource(_stopping.Token);
        cancellation.CancelAfter(timeout);
        return cancellation;
    }

    private static async Task WriteFrameAsync(Stream stream, ReadOnlyMemory<byte> payload, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await stream.WriteAsync(header, cancellationToken).ConfigureAwait(false);
        await stream.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    private static async Task<byte[]> ReadFrameAsync(Stream stream, int maximumPayloadBytes, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        await ReadExactlyAsync(stream, header, cancellationToken).ConfigureAwait(false);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length <= 0 || length > maximumPayloadBytes)
        {
            throw new InvalidDataException($"The local WPF probe frame length {length} is outside 1..{maximumPayloadBytes}.");
        }

        var payload = new byte[length];
        await ReadExactlyAsync(stream, payload, cancellationToken).ConfigureAwait(false);
        return payload;
    }

    private static async Task ReadExactlyAsync(Stream stream, Memory<byte> destination, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < destination.Length)
        {
            var read = await stream.ReadAsync(destination[offset..], cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                throw new EndOfStreamException("The local WPF probe pipe closed before a complete frame was received.");
            }

            offset += read;
        }
    }

    private static bool TryReadAuthorization(out string pipeName, out string nonce)
    {
        pipeName = Environment.GetEnvironmentVariable(PipeNameEnvironmentVariable) ?? string.Empty;
        nonce = Environment.GetEnvironmentVariable(NonceEnvironmentVariable) ?? string.Empty;
        return IsValidPipeName(pipeName) && IsAuthorizationToken(nonce);
    }

    private static bool IsValidPipeName(string value)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > MaximumPipeNameLength)
        {
            return false;
        }

        foreach (var character in value)
        {
            if (char.IsControl(character) || character is '\\' or '/')
            {
                return false;
            }
        }

        return true;
    }

    private static bool TryGetToken(JsonElement source, string name, out string value) =>
        TryGetString(source, name, out value) && IsAuthorizationToken(value);

    private static bool TryGetString(JsonElement source, string name, out string value)
    {
        if (source.TryGetProperty(name, out var property) &&
            property.ValueKind == JsonValueKind.String &&
            property.GetString() is { Length: > 0 } parsed)
        {
            value = parsed;
            return true;
        }

        value = string.Empty;
        return false;
    }

    private static bool IsAuthorizationToken(string value)
    {
        if (value.Length is < MinimumAuthorizationTokenLength or > MaximumAuthorizationTokenLength)
        {
            return false;
        }

        foreach (var character in value)
        {
            if (!(char.IsAsciiLetterOrDigit(character) || character is '-' or '_'))
            {
                return false;
            }
        }

        return true;
    }

    private static bool FixedTimeEquals(string expected, string actual)
    {
        if (expected.Length != actual.Length)
        {
            return false;
        }

        return CryptographicOperations.FixedTimeEquals(
            System.Text.Encoding.UTF8.GetBytes(expected),
            System.Text.Encoding.UTF8.GetBytes(actual));
    }

    private static void ValidateOptions(LocalProbeClientOptions options)
    {
        ArgumentNullException.ThrowIfNull(options);
        ValidateTimeout(options.ConnectTimeout, nameof(options.ConnectTimeout));
        ValidateTimeout(options.WriteTimeout, nameof(options.WriteTimeout));
        ValidateTimeout(options.ReadTimeout, nameof(options.ReadTimeout));
        ValidateBound(options.MaximumRequestBytes, nameof(options.MaximumRequestBytes));
        ValidateBound(options.MaximumResponseBytes, nameof(options.MaximumResponseBytes));
    }

    private static void ValidateTimeout(TimeSpan timeout, string name)
    {
        if (timeout <= TimeSpan.Zero || timeout == Timeout.InfiniteTimeSpan)
        {
            throw new ArgumentOutOfRangeException(name, "A positive finite timeout is required.");
        }
    }

    private static void ValidateBound(int value, string name)
    {
        if (value <= 0 || value > MaximumControlFrameBytes)
        {
            throw new ArgumentOutOfRangeException(name, $"A frame bound in 1..{MaximumControlFrameBytes} is required.");
        }
    }

    private sealed record ProbeResponseEnvelope(
        [property: JsonPropertyName("nonce")] string Nonce,
        [property: JsonPropertyName("correlationId")] string CorrelationId,
        [property: JsonPropertyName("response")] WpfSceneSnapshotDto Response);

    private sealed class BoundedBufferWriter : IBufferWriter<byte>
    {
        private const int InitialCapacity = 256;

        private readonly int _maximumLength;
        private byte[] _buffer = Array.Empty<byte>();
        private int _written;

        internal BoundedBufferWriter(int maximumLength)
        {
            _maximumLength = maximumLength;
        }

        internal ReadOnlyMemory<byte> WrittenMemory => _buffer.AsMemory(0, _written);

        public void Advance(int count)
        {
            if (count < 0 || count > _buffer.Length - _written)
            {
                throw new InvalidDataException("The WPF probe JSON writer advanced beyond its response bound.");
            }

            _written += count;
        }

        public Memory<byte> GetMemory(int sizeHint = 0)
        {
            EnsureCapacity(sizeHint);
            return _buffer.AsMemory(_written);
        }

        public Span<byte> GetSpan(int sizeHint = 0)
        {
            EnsureCapacity(sizeHint);
            return _buffer.AsSpan(_written);
        }

        private void EnsureCapacity(int sizeHint)
        {
            if (sizeHint < 0)
            {
                throw new ArgumentOutOfRangeException(nameof(sizeHint));
            }

            var minimumAdditionalBytes = Math.Max(sizeHint, 1);
            if (minimumAdditionalBytes > _maximumLength - _written)
            {
                throw new InvalidDataException("The WPF probe response exceeds its configured frame bound.");
            }

            if (_buffer.Length - _written >= minimumAdditionalBytes)
            {
                return;
            }

            var requiredLength = _written + minimumAdditionalBytes;
            var doubledLength = _buffer.Length == 0 ? InitialCapacity : checked(_buffer.Length * 2);
            var nextLength = Math.Min(_maximumLength, Math.Max(requiredLength, doubledLength));
            Array.Resize(ref _buffer, nextLength);
        }
    }
}
