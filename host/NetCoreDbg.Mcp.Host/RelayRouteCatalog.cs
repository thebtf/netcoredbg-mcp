using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host;

/// <summary>Which leg of the paired session a relay route travels across.</summary>
internal enum RelayDirection
{
    /// <summary>Downstream client -&gt; this host -&gt; Python.</summary>
    DownstreamToUpstream,

    /// <summary>Python -&gt; this host -&gt; downstream client.</summary>
    UpstreamToDownstream,
}

/// <summary>Whether a route is a request/response pair or a one-way notification.</summary>
internal enum RelayRouteKind
{
    Request,
    Notification,
}

/// <summary>
/// Identifies one relay route by its JSON-RPC method, direction, and kind. Used only for
/// duplicate-ownership enforcement and documentation; registering a route here does not by
/// itself wire any handler or filter. See <c>ToolsRelay.cs</c> for the paired
/// registration + handler-wiring convention every relay module follows.
/// </summary>
internal sealed record RelayRoute(string Method, RelayDirection Direction, RelayRouteKind Kind);

/// <summary>
/// Duplicate-safe registry of every relay route this host build advertises, plus the
/// protocol-version fallback policy and downstream-capability normalization shared by the
/// paired session.
///
/// This is an allowlist, not a catch-all JSON-RPC tunnel: adding a route only records that
/// it exists so a second registration for the same <c>(direction, method)</c> fails host
/// construction immediately. Core MCP protocol infrastructure that the SDK answers itself
/// (<c>initialize</c>, <c>ping</c>, <c>notifications/cancelled</c>, ...) is deliberately never
/// entered here: it is not an application relay route, it is transport-level plumbing every
/// paired session gets for free.
/// </summary>
internal sealed class RelayRouteCatalog
{
    /// <summary>SDK 1.4.1's supported protocol set (see <c>McpSessionHandler.SupportedProtocolVersions</c>).</summary>
    private static readonly string[] SupportedProtocolVersions =
    {
        "2024-11-05", "2025-03-26", "2025-06-18", "2025-11-25",
    };

    private const string FallbackProtocolVersion = "2025-11-25";

    private readonly HashSet<(RelayDirection Direction, string Method)> _registered = new();
    private readonly List<RelayRoute> _routes = new();

    public IReadOnlyList<RelayRoute> Routes => _routes;

    /// <summary>Registers one route. Throws if <c>(Direction, Method)</c> is already owned.</summary>
    public void Add(RelayRoute route)
    {
        if (!_registered.Add((route.Direction, route.Method)))
        {
            throw new InvalidOperationException(
                $"Duplicate relay route ownership for {route.Direction}/{route.Method}: " +
                "the FD-000 contract requires exactly one owner per (direction, method).");
        }

        _routes.Add(route);
    }

    /// <summary>
    /// Resolves the effective MCP protocol version applied to both paired legs: the
    /// downstream client's requested version when it belongs to SDK 1.4.1's supported set,
    /// otherwise the documented MCP fallback version.
    /// </summary>
    public static string ResolveEffectiveProtocolVersion(string? requestedVersion) =>
        requestedVersion is not null && Array.IndexOf(SupportedProtocolVersions, requestedVersion) >= 0
            ? requestedVersion
            : FallbackProtocolVersion;
}
