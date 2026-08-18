using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

/// <summary>One uninitialized official stdio connection for asserting the literal first wire request.</summary>
internal sealed class ModernMcpFirstWireDriver : IAsyncDisposable
{
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(2);

    private readonly FixtureProcess _fixture;
    private readonly string _scratchDirectory;
    private readonly ITransport _transport;
    private bool _disposed;

    internal ModernMcpFirstWireDriver(FixtureProcess fixture, string scratchDirectory, ITransport transport)
    {
        _fixture = fixture;
        _scratchDirectory = scratchDirectory;
        _transport = transport;
    }

    internal async Task<JsonRpcMessage> SendFirstRequestAsync(
        string method,
        JsonNode? parameters,
        RequestId id,
        CancellationToken cancellationToken = default)
    {
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(RequestTimeout);
        var token = deadline.Token;

        await _transport.SendMessageAsync(
            new JsonRpcRequest
            {
                Id = id,
                Method = method,
                Params = parameters?.DeepClone(),
            },
            token).ConfigureAwait(false);

        while (true)
        {
            var incoming = await _transport.MessageReader.ReadAsync(token).ConfigureAwait(false);
            if (incoming is JsonRpcMessageWithId correlated && correlated.Id == id)
            {
                return incoming;
            }
        }
    }

    internal async Task<IReadOnlyList<ModernNativeAction>> ReadNativeActionsAsync(CancellationToken cancellationToken = default)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var transcript = await _fixture.ReadTranscriptAsync().ConfigureAwait(false);
        return transcript
            .Where(static entry => entry.Kind is "startup" or "request")
            .Select(static entry => new ModernNativeAction(entry.Kind, entry.Command, entry.RawPayload))
            .ToArray();
    }

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        Exception? cleanupFailure = null;
        try
        {
            await _transport.DisposeAsync().ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            cleanupFailure = exception;
        }

        try
        {
            await _fixture.DisposeAsync().ConfigureAwait(false);
        }
        catch (Exception exception) when (cleanupFailure is null)
        {
            cleanupFailure = exception;
        }

        try
        {
            await ModernMcpScratchDirectory.DeleteAsync(_scratchDirectory).ConfigureAwait(false);
        }
        catch (Exception exception) when (cleanupFailure is null)
        {
            cleanupFailure = exception;
        }

        if (cleanupFailure is not null)
        {
            throw cleanupFailure;
        }
    }
}
