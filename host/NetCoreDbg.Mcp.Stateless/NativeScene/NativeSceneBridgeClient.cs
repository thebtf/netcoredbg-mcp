using System.Buffers;
using System.Buffers.Binary;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Json.Serialization;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

internal sealed class NativeSceneBridgeClient : IAsyncDisposable
{
    private const string ObserverUnavailable = "OBSERVER_UNAVAILABLE";

    private readonly string _pipeName;
    private readonly TimeSpan _connectTimeout;
    private readonly TimeSpan _writeTimeout;
    private readonly TimeSpan _readTimeout;
    private readonly int _maximumRequestBytes;
    private readonly int _maximumResponseBytes;
    private readonly SemaphoreSlim _oneInFlight = new(initialCount: 1, maxCount: 1);
    private readonly object _pipeLock = new();

    private NamedPipeClientStream? _pipe;
    private int _disposed;

    internal NativeSceneBridgeClient(
        string pipeName,
        TimeSpan connectTimeout,
        TimeSpan writeTimeout,
        TimeSpan readTimeout,
        int maximumRequestBytes,
        int maximumResponseBytes)
    {
        if (string.IsNullOrWhiteSpace(pipeName))
        {
            throw new ArgumentException("A local pipe name is required.", nameof(pipeName));
        }

        ValidateTimeout(connectTimeout, nameof(connectTimeout));
        ValidateTimeout(writeTimeout, nameof(writeTimeout));
        ValidateTimeout(readTimeout, nameof(readTimeout));

        if (maximumRequestBytes <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maximumRequestBytes));
        }

        if (maximumResponseBytes <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maximumResponseBytes));
        }

        _pipeName = pipeName;
        _connectTimeout = connectTimeout;
        _writeTimeout = writeTimeout;
        _readTimeout = readTimeout;
        _maximumRequestBytes = maximumRequestBytes;
        _maximumResponseBytes = maximumResponseBytes;
    }

    internal async Task<NativeSceneBridgeCallResult> SendAsync(
        string authorizationNonce,
        JsonObject request,
        CancellationToken cancellationToken)
    {
        var entered = false;
        try
        {
            if (Volatile.Read(ref _disposed) != 0)
            {
                return Unavailable();
            }

            await _oneInFlight.WaitAsync(cancellationToken).ConfigureAwait(false);
            entered = true;

            if (Volatile.Read(ref _disposed) != 0 || string.IsNullOrWhiteSpace(authorizationNonce) || request is null)
            {
                ClosePipe();
                return Unavailable();
            }

            var correlationId = CreateCorrelationId(authorizationNonce);
            var payload = SerializeRequest(new BridgeRequestEnvelope(authorizationNonce, correlationId, request));
            var pipe = await GetConnectedPipeAsync(cancellationToken).ConfigureAwait(false);

            using (var writeCancellation = CreatePhaseCancellation(cancellationToken, _writeTimeout))
            {
                await WriteFrameAsync(pipe, payload, writeCancellation.Token).ConfigureAwait(false);
            }

            byte[] responseFrame;
            using (var readCancellation = CreatePhaseCancellation(cancellationToken, _readTimeout))
            {
                responseFrame = await ReadFrameAsync(pipe, _maximumResponseBytes, readCancellation.Token).ConfigureAwait(false);
            }

            return ParseResponse(responseFrame, authorizationNonce, correlationId);
        }
        catch (Exception)
        {
            if (entered)
            {
                ClosePipe();
            }

            return Unavailable();
        }
        finally
        {
            if (entered)
            {
                _oneInFlight.Release();
            }
        }
    }

    public ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) == 0)
        {
            ClosePipe();
        }

        return ValueTask.CompletedTask;
    }

    private async Task<NamedPipeClientStream> GetConnectedPipeAsync(CancellationToken cancellationToken)
    {
        var connected = GetReusablePipe();
        if (connected is not null)
        {
            return connected;
        }

        var pipe = new NamedPipeClientStream(
            serverName: ".",
            pipeName: _pipeName,
            direction: PipeDirection.InOut,
            options: PipeOptions.Asynchronous);
        try
        {
            using var connectCancellation = CreatePhaseCancellation(cancellationToken, _connectTimeout);
            await pipe.ConnectAsync(connectCancellation.Token).ConfigureAwait(false);
            if (!pipe.IsConnected)
            {
                throw new IOException("The local native-scene observer pipe did not connect.");
            }

            lock (_pipeLock)
            {
                if (Volatile.Read(ref _disposed) != 0)
                {
                    throw new ObjectDisposedException(nameof(NativeSceneBridgeClient));
                }

                _pipe = pipe;
            }

            return pipe;
        }
        catch
        {
            pipe.Dispose();
            throw;
        }
    }

    private NamedPipeClientStream? GetReusablePipe()
    {
        NamedPipeClientStream? stale;
        lock (_pipeLock)
        {
            if (_pipe is { IsConnected: true } connected)
            {
                return connected;
            }

            stale = _pipe;
            _pipe = null;
        }

        stale?.Dispose();
        return null;
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

    private ReadOnlyMemory<byte> SerializeRequest(BridgeRequestEnvelope request)
    {
        var buffer = new BoundedBufferWriter(_maximumRequestBytes);
        using var writer = new Utf8JsonWriter(buffer);
        JsonSerializer.Serialize(writer, request);
        writer.Flush();
        return buffer.WrittenMemory;
    }

    private static NativeSceneBridgeCallResult ParseResponse(
        ReadOnlyMemory<byte> responseFrame,
        string expectedNonce,
        string expectedCorrelationId)
    {
        var envelope = JsonNode.Parse(responseFrame.Span) as JsonObject
            ?? throw new InvalidDataException("The local native-scene observer response must be a JSON object.");
        if (!TryGetString(envelope, "nonce", out var nonce) ||
            !StringComparer.Ordinal.Equals(nonce, expectedNonce) ||
            !TryGetString(envelope, "correlationId", out var correlationId) ||
            !StringComparer.Ordinal.Equals(correlationId, expectedCorrelationId) ||
            envelope["response"] is not JsonObject response)
        {
            throw new InvalidDataException("The local native-scene observer response did not match its request authorization.");
        }

        return new NativeSceneBridgeCallResult(true, null, response);
    }

    private static bool TryGetString(JsonObject source, string propertyName, out string value)
    {
        if (source[propertyName] is JsonValue property && property.TryGetValue<string>(out var parsed) && !string.IsNullOrEmpty(parsed))
        {
            value = parsed;
            return true;
        }

        value = string.Empty;
        return false;
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
            throw new InvalidDataException($"Pipe frame length {length} is outside 1..{maximumPayloadBytes}.");
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
                throw new EndOfStreamException("The local native-scene observer pipe closed before a complete frame was received.");
            }

            offset += read;
        }
    }

    private static CancellationTokenSource CreatePhaseCancellation(CancellationToken cancellationToken, TimeSpan timeout)
    {
        var linked = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        linked.CancelAfter(timeout);
        return linked;
    }

    private static string CreateCorrelationId(string authorizationNonce)
    {
        Span<byte> bytes = stackalloc byte[16];
        try
        {
            string correlationId;
            do
            {
                RandomNumberGenerator.Fill(bytes);
                correlationId = Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
            }
            while (StringComparer.Ordinal.Equals(correlationId, authorizationNonce));

            return correlationId;
        }
        finally
        {
            CryptographicOperations.ZeroMemory(bytes);
        }
    }

    private static NativeSceneBridgeCallResult Unavailable() => new(false, ObserverUnavailable, null);

    private static void ValidateTimeout(TimeSpan timeout, string parameterName)
    {
        if (timeout <= TimeSpan.Zero || timeout == Timeout.InfiniteTimeSpan)
        {
            throw new ArgumentOutOfRangeException(parameterName, "A positive finite timeout is required.");
        }
    }

    private sealed record BridgeRequestEnvelope(
        [property: JsonPropertyName("nonce")] string Nonce,
        [property: JsonPropertyName("correlationId")] string CorrelationId,
        [property: JsonPropertyName("request")] JsonObject Request);

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
                throw new InvalidDataException("The JSON writer advanced beyond the native-scene request bound.");
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
                throw new InvalidDataException("The native-scene request exceeds its configured frame bound.");
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

internal sealed record NativeSceneBridgeCallResult(bool IsAvailable, string? Code, JsonObject? Payload);
