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
/// <c>RelayComposition.RequiredUpstreamCapabilityChecks</c>), a resources capability is
/// projected downstream only when Python's own negotiated capabilities actually include it:
/// architecture.md requires "a capability absent downstream is not advertised upstream; the
/// relay never substitutes an empty result" for this exact shape of conditional projection.
/// <see cref="ConfigureCapabilityProjection"/> is the paired entry point that declares the
/// static <c>subscribe=false</c>/<c>listChanged=false</c> capability this build advertises by
/// default and removes it from the serialized <c>initialize</c> response whenever Python
/// lacks a resources capability, mirroring
/// <c>RelayRouteCatalog.SuppressUnregisteredLogging</c>'s existing outgoing-filter technique
/// for the same class of "SDK-declared capability vs. actual upstream support" problem.
/// subscribe/listChanged stay false for every build until FD-006 implements real updates.
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
    /// Called from within the same <c>AddMcpServer(options =&gt; ...)</c> factory as
    /// <c>RelayRouteCatalog.SuppressUnregisteredLogging(options.Filters)</c> - this build's
    /// static resources capability declaration and its one paired outgoing filter both need
    /// to exist before the downstream <c>initialize</c> response can ever be produced.
    /// Declares <c>subscribe=false</c>/<c>listChanged=false</c> unconditionally (no update
    /// support is implemented until FD-006), then registers the outgoing filter that removes
    /// the "resources" key from the serialized initialize response whenever Python's own
    /// negotiated capabilities do not include a resources capability.
    /// </summary>
    public static void ConfigureCapabilityProjection(ServerCapabilities capabilities, McpServerFilters filters, RelaySession session)
    {
        capabilities.Resources = new ResourcesCapability { Subscribe = false, ListChanged = false };

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
                if (upstream.ServerCapabilities?.Resources is null)
                {
                    serializedCapabilities.Remove("resources");
                }
            }

            await next(context, cancellationToken).ConfigureAwait(false);
        });
    }
}
