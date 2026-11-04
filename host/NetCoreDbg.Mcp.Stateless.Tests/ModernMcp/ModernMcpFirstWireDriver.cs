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
    private readonly object _disposeGate = new();
    private Task? _disposeTask;
    private bool _startUnconfirmed;
    private bool _multipleUnconfirmedStarts;

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
        var start = method == "tools/call"
            && parameters?["name"] is JsonValue name
            && name.TryGetValue<string>(out var toolName)
            && toolName == "start_debug";
        var priorStartUnconfirmed = _startUnconfirmed;
        if (start)
        {
            _multipleUnconfirmedStarts |= priorStartUnconfirmed;
            _startUnconfirmed = true;
            _fixture.MarkAdapterStartAttempted();
        }

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
                if (start && !priorStartUnconfirmed
                    && incoming is JsonRpcResponse { Result: JsonObject result }
                    && ModernMcpProcessDriver.IsPreLaunchStartResult(result))
                {
                    _startUnconfirmed = false;
                    _fixture.MarkCleanupConfirmed();
                }
                return incoming;
            }
        }
    }

    internal async Task StopSingleControlledSessionAsync(string sessionId)
    {
        var transcript = await _fixture.ReadTranscriptAsync().ConfigureAwait(false);
        if (transcript.Count(static entry => entry.Kind == "startup") != 1)
        {
            throw new InvalidOperationException("Expected exactly one controlled adapter for cleanup.");
        }
        var message = await SendFirstRequestAsync("tools/call", new JsonObject
        {
            ["name"] = "stop_debug",
            ["arguments"] = new JsonObject { ["debugSessionId"] = sessionId },
            ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
        }, new RequestId("first-wire-cleanup")).ConfigureAwait(false);
        if (message is not JsonRpcResponse { Result: JsonObject result }
            || result["resultType"]?.GetValue<string>() != "complete"
            || result["isError"]?.GetValue<bool>() == true
            || result["structuredContent"]?["kind"]?.GetValue<string>() != "stop_debug_success")
        {
            throw new InvalidOperationException("The controlled session did not confirm cleanup.");
        }
        if (_multipleUnconfirmedStarts)
        {
            throw new InvalidOperationException("A prior controlled start remains unresolved.");
        }
        _fixture.MarkCleanupConfirmed();
        _startUnconfirmed = false;
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

    public ValueTask DisposeAsync()
    {
        lock (_disposeGate)
        {
            return new ValueTask(_disposeTask ??= DisposeCoreAsync());
        }
    }

    private async Task DisposeCoreAsync()
    {
        Exception? cleanupFailure = null;
        var fixtureCleaned = false;
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
            fixtureCleaned = true;
        }
        catch (Exception exception)
        {
            if (cleanupFailure is null) cleanupFailure = exception;
            else cleanupFailure.Data["FixtureCleanupFailure"] = exception;
        }

        if (fixtureCleaned)
        {
            try
            {
                await ModernMcpScratchDirectory.DeleteAsync(_scratchDirectory).ConfigureAwait(false);
            }
            catch (Exception exception)
            {
                if (cleanupFailure is null) cleanupFailure = exception;
                else cleanupFailure.Data["ScratchCleanupFailure"] = exception;
            }
        }

        if (cleanupFailure is not null)
        {
            throw cleanupFailure;
        }
    }
}
