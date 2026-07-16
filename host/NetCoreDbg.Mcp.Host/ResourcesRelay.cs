using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Capability-scoped relay for <c>resources/list</c>, <c>resources/templates/list</c>, and
/// <c>resources/read</c> (FD-005). Forwards the exact protocol request and result objects;
/// no host-defined DTO conversion, no local resource registration - Python remains the sole
/// implementation of every resource. Follows the same one-module, one-<c>Register</c>-call
/// convention as <see cref="ToolsRelay"/> for route/handler wiring.
///
/// Unlike tools (always required - see
/// <c>RelayComposition.RequiredUpstreamCapabilityChecks</c>), the resources capability is
/// projected only when Python advertises the FD-006 subscription contract. The catalog is
/// static, so the host always keeps <c>listChanged=false</c>; <c>subscribe=true</c> is exposed
/// only when Python advertises the same value and remains the sole subscription authority.
/// </summary>
internal static class ResourcesRelay
{
    /// <summary>
    /// Registers the three downstream-to-upstream routes and wires the three raw forwarding
    /// handlers. Called from <c>RelayComposition.Build</c> the same way and at the same call
    /// site as <see cref="ToolsRelay.Register"/> - outside the <c>AddMcpServer</c> options
    /// factory, using the <see cref="IMcpServerBuilder"/> it returns.
    /// </summary>
    public static void Register(IMcpServerBuilder builder, RelayRouteCatalog catalog, RelaySession session)
    {
        catalog.Add(new RelayRoute(RequestMethods.ResourcesList, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request));
        catalog.Add(new RelayRoute(RequestMethods.ResourcesTemplatesList, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request));
        catalog.Add(new RelayRoute(RequestMethods.ResourcesRead, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request));

        builder
            .WithListResourcesHandler(async (context, cancellationToken) =>
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);

                // Some clients omit `params` entirely for a cursor-less resources/list; the
                // raw downstream request then carries a null Params. Forward an empty params
                // object instead of null so Python sees the same shape a cursor-less client
                // sends it directly, without reconstructing or renaming any field the caller
                // did supply (mirrors ToolsRelay's WithListToolsHandler).
                var request = context.JsonRpcRequest.Params is null
                    ? new JsonRpcRequest { Method = context.JsonRpcRequest.Method, Params = new JsonObject() }
                    : context.JsonRpcRequest;

                var response = await RelaySession.ForwardRequestAsync(upstream, request, cancellationToken).ConfigureAwait(false);
                return response.Result.Deserialize<ListResourcesResult>(McpJsonUtilities.DefaultOptions)!;
            })
            .WithListResourceTemplatesHandler(async (context, cancellationToken) =>
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);

                // Same cursor-less shape preservation as resources/list above.
                var request = context.JsonRpcRequest.Params is null
                    ? new JsonRpcRequest { Method = context.JsonRpcRequest.Method, Params = new JsonObject() }
                    : context.JsonRpcRequest;

                var response = await RelaySession.ForwardRequestAsync(upstream, request, cancellationToken).ConfigureAwait(false);
                return response.Result.Deserialize<ListResourceTemplatesResult>(McpJsonUtilities.DefaultOptions)!;
            })
            .WithReadResourceHandler(async (context, cancellationToken) =>
            {
                // resources/read always carries a mandatory `uri` param, exactly like
                // tools/call always carries a mandatory `name`: no null-Params handling
                // needed here, mirroring ToolsRelay's WithCallToolHandler.
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                var response = await RelaySession.ForwardRequestAsync(upstream, context.JsonRpcRequest, cancellationToken).ConfigureAwait(false);
                return response.Result.Deserialize<ReadResourceResult>(McpJsonUtilities.DefaultOptions)!;
            });
    }

    /// <summary>
    /// Declares the downstream resources capability whenever Python advertises resources,
    /// projects <c>subscribe</c> from Python, and keeps the static catalog's
    /// <c>listChanged</c> value false.
    /// </summary>
    public static void ConfigureCapabilityProjection(ServerCapabilities capabilities, McpServerFilters filters, RelaySession session)
    {
        capabilities.Resources = new ResourcesCapability { Subscribe = true, ListChanged = false };

        filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
        {
            if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                && result.TryGetPropertyValue("capabilities", out var capabilitiesNode)
                && capabilitiesNode is JsonObject serializedCapabilities)
            {
                // By the time any outgoing message exists for the downstream initialize
                // response, the paired session's upstream bootstrap has already completed
                // successfully - a failed bootstrap throws before the SDK's own initialize
                // handler ever runs (see RelaySession.CreateBootstrapFilter), so no
                // response would exist to intercept here. UpstreamAsync therefore always
                // completes synchronously in every real execution of this filter.
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                if (upstream.ServerCapabilities?.Resources is not { } upstreamResources)
                {
                    serializedCapabilities.Remove("resources");
                }
                else if (serializedCapabilities["resources"] is JsonObject projectedResources)
                {
                    projectedResources["subscribe"] = upstreamResources.Subscribe is true;
                    projectedResources["listChanged"] = false;
                }
            }

            await next(context, cancellationToken).ConfigureAwait(false);
        });
    }
}
