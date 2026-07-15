using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Additive downstream-roots relay module (FD-001, Engram #385): capability-gated reverse
/// <c>roots/list</c> and <c>notifications/roots/list_changed</c> routing so Python's existing
/// MCP-roots-first project-root resolution (see
/// <c>src/netcoredbg_mcp/utils/project.py</c>'s <c>get_project_root</c>) reaches the real
/// downstream client through this host exactly as it already does when a client talks to
/// Python directly. This module never touches Python's explicit <c>--project</c>,
/// <c>NETCOREDBG_PROJECT_ROOT</c>, or <c>--project-from-cwd</c> resolution: those remain
/// Python-owned and byte-for-byte unchanged. Roots capability is projected upstream only when
/// the real downstream client advertises it, so Python's own capability check - and therefore
/// its unchanged env/explicit/cwd fallback order - is exactly what runs when it is absent.
///
/// One instance is required per paired <see cref="RelaySession"/> (never a shared/static
/// instance across sessions): SDK 1.4.1's <c>McpClient</c> derives the effective
/// <c>ClientCapabilities.Roots</c> it advertises during the upstream handshake from whether
/// <see cref="McpClientHandlers.RootsHandler"/> is set at all - independent of, and in
/// addition to, whatever <see cref="ClientCapabilities.Roots"/> object is otherwise supplied
/// - verified directly against the compiled SDK. <see cref="ConfigureUpstreamHandlers"/>
/// therefore only wires the handler when <see cref="ProjectCapabilities"/> already decided,
/// for this same instance, that the real downstream client actually declared Roots;
/// registering it unconditionally would silently re-advertise Roots to Python even when the
/// downstream client cannot answer it, defeating the capability gate entirely. FD-000's
/// paired session guarantees <see cref="ProjectCapabilities"/> always runs (from the bootstrap
/// filter) before <see cref="ConfigureUpstreamHandlers"/> (from the single-flight upstream
/// handshake it triggers) for the same <c>initialize</c> exchange, so this ordering is safe
/// without any additional synchronization.
///
/// Follows the same one-module-one-registration-entry convention as <see cref="ToolsRelay"/>,
/// split across the only two places FD-000's paired session lets a reverse-route module
/// attach:
/// <list type="bullet">
/// <item><see cref="ProjectCapabilities"/> - capability projection, wired into
/// <c>RelayComposition</c>'s <c>projectReverseRouteCapabilities</c> callback.</item>
/// <item><see cref="ConfigureUpstreamHandlers"/> - the upstream (Python-facing) reverse
/// request handler, wired into <see cref="RelaySession"/>'s constructor
/// <c>configureUpstreamHandlers</c> parameter in <c>Program.cs</c>.</item>
/// <item><see cref="Register"/> - the one static, stateless call (catalog bookkeeping plus
/// the downstream-to-upstream <c>list_changed</c> notification forward) the integrator adds
/// to <c>RelayComposition.Build</c> alongside <see cref="ToolsRelay.Register"/>.</item>
/// </list>
/// See the integration hook reported with this change for the exact production call each
/// site needs once this module is accepted.
/// </summary>
internal sealed class RootsRelay
{
    private bool _projectedRootsUpstream;

    /// <summary>
    /// Projects the real downstream client's Roots capability upstream to Python: present,
    /// with the same <see cref="RootsCapability.ListChanged"/> flag, only when the downstream
    /// client actually declared it during its own initialize handshake. A capability absent
    /// downstream is never advertised upstream and this relay never substitutes an empty
    /// result, so Python's own client-capability check - and therefore its existing
    /// roots-then-env-then-explicit-then-cwd fallback order - is unaffected when the real
    /// client cannot answer <c>roots/list</c>. Mutates and returns
    /// <paramref name="upstreamCapabilities"/> so other reverse-route modules can compose
    /// their own projections onto the same instance. Remembers the decision on this instance
    /// for <see cref="ConfigureUpstreamHandlers"/> to honor.
    /// </summary>
    public ClientCapabilities ProjectCapabilities(
        ClientCapabilities? downstreamCapabilities, ClientCapabilities upstreamCapabilities)
    {
        if (downstreamCapabilities?.Roots is { } downstreamRoots)
        {
            upstreamCapabilities.Roots = new RootsCapability { ListChanged = downstreamRoots.ListChanged };
            _projectedRootsUpstream = true;
        }

        return upstreamCapabilities;
    }

    /// <summary>
    /// Wires the upstream client's reverse <c>roots/list</c> handler, but only when
    /// <see cref="ProjectCapabilities"/> already decided (for this same instance, during the
    /// same bootstrap sequence) that the real downstream client declared Roots. Leaving the
    /// handler entirely unset when it did not is required, not cosmetic: the SDK advertises
    /// <c>ClientCapabilities.Roots</c> upstream whenever this handler is set at all (see this
    /// type's own remarks), so an unconditional registration would defeat the capability
    /// gate <see cref="ProjectCapabilities"/> exists to enforce.
    ///
    /// When wired, Python's request is forwarded verbatim (preserving params, <c>_meta</c>,
    /// and any progress token) to the real downstream client, and its raw result is returned
    /// unchanged - no substitution or reconstruction.
    ///
    /// Awaits <see cref="RelaySession.DownstreamReady"/> before the handler's first use of
    /// the downstream session (architecture.md FD-000 steps 5-6): the upstream Python
    /// handshake this handler is registered for, and the downstream client's own
    /// <c>notifications/initialized</c>, race independently once the downstream
    /// <c>initialize</c> request arrives - so without this await, Python could reach a
    /// downstream session the MCP lifecycle does not yet permit server-initiated requests
    /// on. Links the SDK-supplied per-request token with <paramref name="session"/>'s
    /// <see cref="RelaySession.SessionEndingToken"/> so an in-flight reverse call is
    /// cancelled - never left hung - on downstream disconnect or Python's own session
    /// ending: exactly the deadlock/hang the FD-000 readiness and reentrant forwarding
    /// primitives exist to let feature modules avoid.
    /// </summary>
    public void ConfigureUpstreamHandlers(McpClientHandlers handlers, RelaySession session)
    {
        if (!_projectedRootsUpstream)
        {
            return;
        }

        handlers.RootsHandler = async (typedParams, cancellationToken) =>
        {
            using var linked = CancellationTokenSource.CreateLinkedTokenSource(
                cancellationToken, session.SessionEndingToken);

            await session.DownstreamReady.WaitAsync(linked.Token).ConfigureAwait(false);

            var forwarded = new JsonRpcRequest
            {
                Method = RequestMethods.RootsList,
                Params = JsonSerializer.SerializeToNode(typedParams, McpJsonUtilities.DefaultOptions),
            };
            var response = await RelaySession
                .ForwardRequestAsync(session.Downstream!, forwarded, linked.Token)
                .ConfigureAwait(false);
            return response.Result!.Deserialize<ListRootsResult>(McpJsonUtilities.DefaultOptions)!;
        };
    }

    /// <summary>
    /// Registers this module's two routes in the shared catalog (duplicate-ownership
    /// bookkeeping only, per <see cref="RelayRouteCatalog"/>'s own contract - it does not by
    /// itself wire any handler) and wires the downstream-to-upstream half: a real
    /// <c>notifications/roots/list_changed</c> push from the downstream client is forwarded
    /// to Python unchanged, preserving method, params, and <c>_meta</c> exactly, and awaited
    /// so source ordering for that route is retained. Stateless and static - forwarding an
    /// occasional <c>list_changed</c> push from a client that turns out not to have declared
    /// Roots is harmless (unlike <see cref="ConfigureUpstreamHandlers"/>, this registration
    /// does not itself change any advertised capability) - so, unlike the upstream handler,
    /// this needs no per-instance gating and no <see cref="RootsRelay"/> instance at all.
    ///
    /// This is the one call the integrator adds to <c>RelayComposition.Build</c> alongside
    /// <see cref="ToolsRelay.Register"/> once this module is accepted. The reverse
    /// <c>roots/list</c> route is recorded here for catalog completeness even though its
    /// handler lives in <see cref="ConfigureUpstreamHandlers"/>, wired separately at
    /// <see cref="RelaySession"/> construction in <c>Program.cs</c> - the catalog is pure
    /// bookkeeping, decoupled from where a route's real handler is attached.
    /// </summary>
    public static void Register(IMcpServerBuilder builder, RelayRouteCatalog catalog, RelaySession session)
    {
        catalog.Add(new RelayRoute(RequestMethods.RootsList, RelayDirection.UpstreamToDownstream, RelayRouteKind.Request));
        catalog.Add(new RelayRoute(NotificationMethods.RootsListChangedNotification, RelayDirection.DownstreamToUpstream, RelayRouteKind.Notification));

        builder.Services.Configure<McpServerOptions>(options =>
        {
            options.Handlers.NotificationHandlers = (options.Handlers.NotificationHandlers ?? [])
                .Append(new KeyValuePair<string, Func<JsonRpcNotification, CancellationToken, ValueTask>>(
                    NotificationMethods.RootsListChangedNotification,
                    async (notification, cancellationToken) =>
                    {
                        var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                        await RelaySession.ForwardNotificationAsync(upstream, notification, cancellationToken).ConfigureAwait(false);
                    }));
        });
    }
}
