using System.Buffers.Binary;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

/// <summary>
/// A one-connection local endpoint created before DAP launch. The debuggee owns the outbound connection.
/// </summary>
internal sealed class NativeSceneProbeChannel : IAsyncDisposable
{
    private const int MaximumRequestBytes = 64 * 1024;
    private const int MaximumResponseBytes = 16 * 1024 * 1024;
    private const int MaximumCustomAdapterPayloadBytes = 262_144;
    private static readonly TimeSpan ConnectionTimeout = TimeSpan.FromSeconds(1);
    private static readonly TimeSpan Timeout = TimeSpan.FromSeconds(5);
    private const string TransportFailureProperty = "_nativeSceneProbeTransportFailure";

    private readonly NamedPipeServerStream _pipe;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly object _lifecycleGate = new();
    private readonly CancellationTokenSource _stopping = new();
    private readonly Task _connectionTask;
    private Task? _disposeTask;
    private int _connectionOpportunityObserved;
    private int _exchangeStarted;
    private int _terminalized;

    internal NativeSceneProbeChannel()
    {
        PipeName = "native-scene-probe-" + CreateToken();
        Nonce = CreateToken();
        _pipe = new NamedPipeServerStream(
            PipeName,
            PipeDirection.InOut,
            maxNumberOfServerInstances: 1,
            PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous);
        _connectionTask = WaitForConnectionAsync();
    }

    internal string PipeName { get; }

    internal string Nonce { get; }

    internal bool IsConnected => !IsTerminal && _pipe.IsConnected;

    internal IReadOnlyDictionary<string, string> LaunchEnvironment => new Dictionary<string, string>(StringComparer.Ordinal)
    {
        ["NETCOREDBG_MCP_NATIVE_SCENE_PROBE_PIPE"] = PipeName,
        ["NETCOREDBG_MCP_NATIVE_SCENE_PROBE_NONCE"] = Nonce,
    };

    internal async Task<JsonObject?> CaptureAsync(CancellationToken cancellationToken)
    {
        if (IsTerminal)
        {
            return null;
        }

        var enteredGate = false;
        try
        {
            await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
            enteredGate = true;
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (ObjectDisposedException) when (IsTerminal)
        {
            return null;
        }

        try
        {
            if (IsTerminal)
            {
                return null;
            }

            if (!_pipe.IsConnected)
            {
                if (Interlocked.Exchange(ref _connectionOpportunityObserved, 1) != 0)
                {
                    return null;
                }

                var completed = await Task.WhenAny(_connectionTask, Task.Delay(ConnectionTimeout, cancellationToken)).ConfigureAwait(false);
                if (completed != _connectionTask)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    return null;
                }

                await _connectionTask.ConfigureAwait(false);
                if (!_pipe.IsConnected)
                {
                    return null;
                }
            }

            using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, _stopping.Token);
            deadline.CancelAfter(Timeout);
            var correlationId = CreateToken();
            var request = JsonSerializer.SerializeToUtf8Bytes(new
            {
                nonce = Nonce,
                correlationId,
                request = new { operation = "capture" },
            });
            if (request.Length is < 1 or > MaximumRequestBytes)
            {
                return null;
            }

            var write = TryStartExchange(request, deadline.Token);
            if (write is null)
            {
                return null;
            }

            await write.ConfigureAwait(false);
            var response = await ReadFrameAsync(_pipe, MaximumResponseBytes, deadline.Token).ConfigureAwait(false);
            if (!TryReadResponse(response, correlationId, out var payload))
            {
                Terminalize();
                return null;
            }

            return payload;
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            if (HasStartedExchange)
            {
                Terminalize();
            }

            throw;
        }
        catch (OperationCanceledException)
        {
            if (HasStartedExchange)
            {
                Terminalize();
            }

            return null;
        }
        catch (Exception)
        {
            if (HasStartedExchange)
            {
                Terminalize();
            }

            return null;
        }
        finally
        {
            if (enteredGate)
            {
                Volatile.Write(ref _exchangeStarted, 0);
                _gate.Release();
            }
        }
    }

    public ValueTask DisposeAsync()
    {
        Terminalize();
        lock (_lifecycleGate)
        {
            _disposeTask ??= DisposeResourcesAsync();
            return new ValueTask(_disposeTask);
        }
    }

    private bool HasStartedExchange => Volatile.Read(ref _exchangeStarted) != 0;

    private bool IsTerminal => Volatile.Read(ref _terminalized) != 0;

    private Task? TryStartExchange(ReadOnlyMemory<byte> request, CancellationToken cancellationToken)
    {
        lock (_lifecycleGate)
        {
            if (IsTerminal)
            {
                return null;
            }

            Volatile.Write(ref _exchangeStarted, 1);
            return WriteFrameAsync(_pipe, request, cancellationToken);
        }
    }

    private void Terminalize()
    {
        lock (_lifecycleGate)
        {
            if (Interlocked.Exchange(ref _terminalized, 1) != 0)
            {
                return;
            }

            _stopping.Cancel();
            _pipe.Dispose();
        }
    }

    private async Task DisposeResourcesAsync()
    {
        await _connectionTask.ConfigureAwait(false);
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            _stopping.Dispose();
        }
        finally
        {
            _gate.Release();
            _gate.Dispose();
        }
    }

    internal static bool IsTransportFailure(JsonObject? result, out string code)
    {
        if (result?[TransportFailureProperty] is JsonValue value &&
            value.TryGetValue<string>(out code!) &&
            StringComparer.Ordinal.Equals(code, "OBSERVER_UNAVAILABLE"))
        {
            return true;
        }

        code = string.Empty;
        return false;
    }

    private bool TryReadResponse(ReadOnlyMemory<byte> response, string correlationId, out JsonObject? payload)
    {
        payload = null;
        try
        {
            using var document = JsonDocument.Parse(response, new JsonDocumentOptions
            {
                AllowTrailingCommas = false,
                CommentHandling = JsonCommentHandling.Disallow,
                MaxDepth = 16,
            });
            var envelope = document.RootElement;
            if (envelope.ValueKind != JsonValueKind.Object ||
                !TryReadToken(envelope, "nonce", out var nonce) || !FixedTimeEquals(Nonce, nonce) ||
                !TryReadToken(envelope, "correlationId", out var receivedCorrelation) || !FixedTimeEquals(correlationId, receivedCorrelation) ||
                !envelope.TryGetProperty("response", out var responsePayload) || responsePayload.ValueKind != JsonValueKind.Object)
            {
                return false;
            }

            if (HasOversizedCustomAdapterPayload(responsePayload))
            {
                payload = new JsonObject { [TransportFailureProperty] = "OBSERVER_UNAVAILABLE" };
                Terminalize();
                return true;
            }

            payload = JsonNode.Parse(responsePayload.GetRawText()) as JsonObject;
            return payload is not null;
        }
        catch (JsonException)
        {
            return false;
        }
    }

    private static bool HasOversizedCustomAdapterPayload(JsonElement responsePayload)
    {
        if (!responsePayload.TryGetProperty("nodes", out var nodes) || nodes.ValueKind != JsonValueKind.Array)
        {
            return false;
        }

        foreach (var node in nodes.EnumerateArray())
        {
            if (node.ValueKind != JsonValueKind.Object ||
                !node.TryGetProperty("adapterEvidence", out var adapterEvidence) ||
                adapterEvidence.ValueKind != JsonValueKind.Array)
            {
                continue;
            }

            foreach (var evidence in adapterEvidence.EnumerateArray())
            {
                if (evidence.ValueKind == JsonValueKind.Object &&
                    evidence.TryGetProperty("payload", out var adapterPayload) &&
                    Encoding.UTF8.GetByteCount(adapterPayload.GetRawText()) > MaximumCustomAdapterPayloadBytes)
                {
                    return true;
                }
            }
        }

        return false;
    }

    private async Task WaitForConnectionAsync()
    {
        try
        {
            await _pipe.WaitForConnectionAsync(_stopping.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (_stopping.IsCancellationRequested)
        {
        }
        catch (ObjectDisposedException) when (IsTerminal)
        {
        }
    }

    private static async Task WriteFrameAsync(Stream stream, ReadOnlyMemory<byte> payload, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await stream.WriteAsync(header, cancellationToken).ConfigureAwait(false);
        await stream.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    private static async Task<byte[]> ReadFrameAsync(Stream stream, int maximumBytes, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        await ReadExactlyAsync(stream, header, cancellationToken).ConfigureAwait(false);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length is < 1 or > MaximumResponseBytes || length > maximumBytes)
        {
            throw new InvalidDataException("The native-scene probe frame is outside the negotiated bound.");
        }

        var frame = new byte[length];
        await ReadExactlyAsync(stream, frame, cancellationToken).ConfigureAwait(false);
        return frame;
    }

    private static async Task ReadExactlyAsync(Stream stream, Memory<byte> destination, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < destination.Length)
        {
            var read = await stream.ReadAsync(destination[offset..], cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                throw new EndOfStreamException("The native-scene probe pipe closed before a complete frame arrived.");
            }

            offset += read;
        }
    }

    private static bool TryReadToken(JsonElement source, string name, out string value)
    {
        value = string.Empty;
        return source.TryGetProperty(name, out var property) &&
               property.ValueKind == JsonValueKind.String &&
               property.GetString() is { Length: 22 } parsed &&
               parsed.All(static character => char.IsAsciiLetterOrDigit(character) || character is '-' or '_') &&
               (value = parsed).Length == 22;
    }

    private static bool FixedTimeEquals(string expected, string actual) =>
        expected.Length == actual.Length &&
        CryptographicOperations.FixedTimeEquals(Encoding.UTF8.GetBytes(expected), Encoding.UTF8.GetBytes(actual));

    private static string CreateToken()
    {
        Span<byte> bytes = stackalloc byte[16];
        try
        {
            RandomNumberGenerator.Fill(bytes);
            return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }
        finally
        {
            CryptographicOperations.ZeroMemory(bytes);
        }
    }
}
