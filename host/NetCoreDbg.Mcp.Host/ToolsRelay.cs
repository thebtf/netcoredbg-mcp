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
/// <see cref="RelaySession.ForwardRequestAsync"/> itself now normalizes a genuine upstream
/// JSON-RPC protocol error's doubled "Request failed (remote): " prefix and preserves its
/// <c>Data</c> - a shared fix, not a tools-specific one, so this module needs no local
/// wrapper around it (see host/NetCoreDbg.Mcp.Host.Tests/ToolsCatalogContractTests.cs for
/// the tools-family regression proving the observed downstream behavior).
/// </summary>
internal static class ToolsRelay
{
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

                var response = await RelaySession.ForwardRequestAsync(upstream, request, cancellationToken).ConfigureAwait(false);
                return response.Result.Deserialize<ListToolsResult>(McpJsonUtilities.DefaultOptions)!;
            })
            .WithCallToolHandler(async (context, cancellationToken) =>
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                var response = await RelaySession.ForwardRequestAsync(upstream, context.JsonRpcRequest, cancellationToken).ConfigureAwait(false);
                return response.Result.Deserialize<CallToolResult>(McpJsonUtilities.DefaultOptions)!;
            });
    }
}
