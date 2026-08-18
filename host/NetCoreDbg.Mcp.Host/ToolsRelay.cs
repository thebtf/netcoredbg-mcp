using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Composite owner for the one public <c>tools/list</c> and <c>tools/call</c> handlers.
/// It forwards Python's complete catalog, replaces only the three deterministic code-search
/// definitions in place, and dispatches only those names locally; <c>search_source</c> and
/// every other tool remain unchanged Python relays.
///
/// Keeping this composition in one handler is intentional: registering a second tool
/// handler would split MCP route ownership and make handler order observable. The shared
/// <see cref="RelayRouteCatalog"/> still records the two public routes exactly once.
/// </summary>
internal static class ToolsRelay
{
    public static void Register(
        IMcpServerBuilder builder,
        RelayRouteCatalog catalog,
        RelaySession session,
        NativeCodeSearch? nativeCodeSearch = null)
    {
        nativeCodeSearch ??= new NativeCodeSearch(
            ProjectRootResolver.FromHostArguments(Array.Empty<string>(), Environment.CurrentDirectory),
            session);
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
                    ? new JsonRpcRequest
                    {
                        Id = context.JsonRpcRequest.Id,
                        Method = context.JsonRpcRequest.Method,
                        Params = new JsonObject(),
                    }
                    : context.JsonRpcRequest;

                var response = await session
                    .ForwardApplicationRequestAsync(upstream, request, cancellationToken)
                    .ConfigureAwait(false);
                var result = response.Result.Deserialize<ListToolsResult>(McpJsonUtilities.DefaultOptions)!;
                NativeCodeSearchCatalog.ReplaceInCatalog(result.Tools);
                return result;
            })
            .WithCallToolHandler(async (context, cancellationToken) =>
            {
                if (NativeCodeSearch.IsNativeTool(context.Params.Name))
                {
                    return await nativeCodeSearch.CallAsync(context, cancellationToken).ConfigureAwait(false);
                }

                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                var response = await session
                    .ForwardApplicationRequestAsync(upstream, context.JsonRpcRequest, cancellationToken)
                    .ConfigureAwait(false);
                return response.Result.Deserialize<CallToolResult>(McpJsonUtilities.DefaultOptions)!;
            });
    }
}
