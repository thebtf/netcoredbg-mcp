using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Extraction of the current <c>tools/list</c> and <c>tools/call</c> handlers: the
/// canonical downstream-to-upstream relay module. Forward the exact protocol request and
/// result objects; no schema reconstruction, no local tool registration, no result
/// translation. Python remains the sole implementation of every tool.
///
/// This is the convention every additive downstream-to-upstream relay module follows: one
/// static <c>Register</c> method, called only from <c>RelayComposition.cs</c>, that records
/// its method(s) in the shared <see cref="RelayRouteCatalog"/> for duplicate-ownership
/// checking and wires typed handlers (via the same <c>With...Handler</c> builder surface
/// the pre-FD-000 composition already used) that reach Python only through
/// <see cref="RelaySession.UpstreamAsync"/> and <see cref="RelaySession.ForwardRequestAsync"/>.
/// </summary>
internal static class ToolsRelay
{
    /// <summary>
    /// The prefix SDK 1.4.1's upstream <c>McpSession.SendRequestAsync</c> applies when it
    /// converts a genuine remote JSON-RPC error into a thrown <see cref="McpProtocolException"/>
    /// (observed directly against a real, non-mocked upstream server returning a protocol
    /// error from a <c>tools/call</c> handler). Left alone, this host's own downstream
    /// dispatch wraps that already-wrapped message a second time when converting the
    /// propagated exception into the downstream JSON-RPC error response, turning what should
    /// be Python's own message into a doubled "Request failed (remote): Request failed
    /// (remote): &lt;message&gt;" - the error code round-trips exactly regardless, only the
    /// message doubles.
    /// </summary>
    private const string UpstreamRemoteErrorPrefix = "Request failed (remote): ";

    public static void Register(IMcpServerBuilder builder, RelayRouteCatalog catalog, RelaySession session)
    {
        catalog.Add(new RelayRoute(RequestMethods.ToolsList, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request));
        catalog.Add(new RelayRoute(RequestMethods.ToolsCall, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request));

        builder
            .WithListToolsHandler(async (context, cancellationToken) =>
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);

                // Some clients omit `params` entirely for a cursor-less tools/list; the raw
                // downstream request then carries a null Params. Forward an empty params
                // object instead of null so Python sees the same shape a cursor-less client
                // sends it directly, without reconstructing or renaming any field the caller
                // did supply.
                var request = context.JsonRpcRequest.Params is null
                    ? new JsonRpcRequest { Method = context.JsonRpcRequest.Method, Params = new JsonObject() }
                    : context.JsonRpcRequest;

                var response = await ForwardAndUnwrapUpstreamErrorAsync(upstream, request, cancellationToken).ConfigureAwait(false);
                return response.Result.Deserialize<ListToolsResult>(McpJsonUtilities.DefaultOptions)!;
            })
            .WithCallToolHandler(async (context, cancellationToken) =>
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                var response = await ForwardAndUnwrapUpstreamErrorAsync(upstream, context.JsonRpcRequest, cancellationToken).ConfigureAwait(false);
                return response.Result.Deserialize<CallToolResult>(McpJsonUtilities.DefaultOptions)!;
            });
    }

    /// <summary>
    /// Forwards through <see cref="RelaySession.ForwardRequestAsync"/> and, if the upstream
    /// leg's own <c>SendRequestAsync</c> wrapped a genuine remote JSON-RPC error with
    /// <see cref="UpstreamRemoteErrorPrefix"/>, strips that one wrap before letting the
    /// exception propagate to this host's own downstream dispatch. The single wrap that
    /// dispatch itself then applies when converting the propagated exception into the
    /// downstream JSON-RPC error is an unavoidable trait of the typed
    /// <c>With...Handler</c> API surface - the only way any handler here can signal a
    /// protocol error is a thrown exception, converted by shared dispatch this module
    /// cannot bypass without a message-level filter that only
    /// <c>RelayComposition</c>/<c>RelayRouteCatalog</c> may own - so a single wrap remains
    /// by design; only the doubling is a ToolsRelay-owned defect this corrects.
    /// </summary>
    private static async Task<JsonRpcResponse> ForwardAndUnwrapUpstreamErrorAsync(
        McpSession upstream, JsonRpcRequest request, CancellationToken cancellationToken)
    {
        try
        {
            return await RelaySession.ForwardRequestAsync(upstream, request, cancellationToken).ConfigureAwait(false);
        }
        catch (McpProtocolException ex) when (ex.Message.StartsWith(UpstreamRemoteErrorPrefix, StringComparison.Ordinal))
        {
            throw new McpProtocolException(ex.Message[UpstreamRemoteErrorPrefix.Length..], ex, ex.ErrorCode);
        }
    }
}
