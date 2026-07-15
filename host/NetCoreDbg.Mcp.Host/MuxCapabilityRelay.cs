using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// FD-007: allowlisted projection of Python's <c>experimental.x-mux</c> capability. This is
/// the only .NET-side behavior FD-007 owns - mux session <em>ownership</em> stays entirely
/// inside Python's <c>SessionOwnership</c> (see <c>src/netcoredbg_mcp/mux.py</c>); this module
/// never claims, releases, checks, or otherwise participates in ownership. Every tool
/// request's <c>_meta</c> (including <c>muxSessionId</c> and any sibling field) already
/// travels through <see cref="ToolsRelay"/>/<see cref="RelaySession.ForwardRequestAsync"/>
/// unchanged, so ownership arbitration continues to happen exactly where Python performs it
/// today - this module adds no new forwarding path for that.
/// </summary>
/// <remarks>
/// <para>
/// <b>Integration note (parent/integrator-owned; not applied by this module):</b>
/// <c>RelayComposition.Build</c> currently sets <c>options.Capabilities.Experimental</c>
/// unconditionally to a fixed <c>{"x-mux":{"sharing":"isolated"}}</c> dictionary, before
/// Python is ever contacted. Replacing that with this allowlisted, upstream-capability-aware
/// projection requires exactly two changes inside <c>RelayComposition.Build</c>'s
/// <c>AddMcpServer</c> options callback:
/// </para>
/// <list type="number">
/// <item>Delete the <c>Experimental = ...</c> assignment (or simply omit it) from the
/// <c>options.Capabilities = new ServerCapabilities { ... }</c> initializer, so the SDK's
/// upfront (pre-handshake) capabilities carry no <c>x-mux</c> value.</item>
/// <item>Add one call alongside the existing
/// <c>RelayRouteCatalog.SuppressUnregisteredLogging(options.Filters)</c> line:
/// <c>MuxCapabilityRelay.RegisterCapabilityProjectionFilter(options.Filters, session);</c></item>
/// </list>
/// <para>
/// No other production file changes: <c>Program.cs</c>, <c>RelaySession.cs</c>,
/// <c>RelayRouteCatalog.cs</c>, and <c>ToolsRelay.cs</c> are untouched. The projection must run
/// as an <em>outgoing</em> filter (not an upfront capability value) because Python's actual
/// capabilities are only known after the paired-session bootstrap handshake completes, which
/// happens inside the incoming bootstrap filter <em>before</em> the SDK's own <c>initialize</c>
/// handler serializes the response this outgoing filter rewrites - the same reason
/// <see cref="RelayRouteCatalog.SuppressUnregisteredLogging"/> also rewrites the serialized
/// response instead of the upfront <see cref="ServerCapabilities"/> value.
/// </para>
/// </remarks>
internal static class MuxCapabilityRelay
{
    /// <summary>The only experimental capability key this host is ever allowed to project downstream.</summary>
    public const string ExperimentalKey = "x-mux";

    /// <summary>
    /// The exact, fixed downstream projection this host advertises once Python's own
    /// <c>x-mux</c> value is confirmed to match: never Python's raw bytes, always this
    /// canonical literal, so a byte-for-byte Python quirk (formatting, extra whitespace) can
    /// never leak through the allowlist.
    /// </summary>
    private const string AllowedProjectionJson = """{"x-mux":{"sharing":"isolated"}}""";

    /// <summary>
    /// Projects Python's advertised <c>experimental</c> capabilities down to, at most, the one
    /// allowlisted <c>x-mux</c> key/value this host may mirror downstream. Returns
    /// <see langword="null"/> (no <c>experimental</c> object at all) unless Python's own
    /// <c>x-mux</c> value is an exact structural match for <c>{"sharing":"isolated"}</c> - a
    /// missing key, a different value, extra nested fields, or Python advertising some other
    /// sibling experimental capability alongside <c>x-mux</c> never leaks past this allowlist,
    /// because the result is always this exact fixed literal, never a copy of whatever else
    /// Python's <paramref name="upstreamExperimental"/> happens to contain.
    /// </summary>
    public static JsonObject? ProjectExperimentalCapabilities(IDictionary<string, object>? upstreamExperimental)
    {
        if (upstreamExperimental is null || !upstreamExperimental.TryGetValue(ExperimentalKey, out var muxValue))
        {
            return null;
        }

        if (muxValue is not JsonElement muxElement || !IsExactAllowedMuxShape(muxElement))
        {
            return null;
        }

        return (JsonObject)JsonNode.Parse(AllowedProjectionJson)!;
    }

    /// <summary>True only for an object with exactly one property, <c>"sharing": "isolated"</c>.</summary>
    private static bool IsExactAllowedMuxShape(JsonElement value)
    {
        if (value.ValueKind != JsonValueKind.Object)
        {
            return false;
        }

        var propertyCount = 0;
        var sharingIsIsolated = false;

        foreach (var property in value.EnumerateObject())
        {
            propertyCount++;
            if (propertyCount > 1)
            {
                return false;
            }

            sharingIsIsolated =
                property.NameEquals("sharing")
                && property.Value.ValueKind == JsonValueKind.String
                && property.Value.ValueEquals("isolated");
        }

        return propertyCount == 1 && sharingIsIsolated;
    }

    /// <summary>
    /// Registers the outgoing-message filter that replaces the SDK-serialized
    /// <c>initialize</c> response's <c>capabilities.experimental</c> field with
    /// <see cref="ProjectExperimentalCapabilities"/>'s allowlisted projection of Python's
    /// already-bootstrapped capabilities (or removes the field entirely when nothing is
    /// allowlisted). Uses the identical structural match
    /// <see cref="RelayRouteCatalog.SuppressUnregisteredLogging"/> uses to find the
    /// <c>initialize</c> response among every outgoing message. Reading
    /// <paramref name="session"/>'s upstream client here is safe: the incoming bootstrap
    /// filter for the same <c>initialize</c> request has already fully awaited the Python
    /// handshake before the SDK's own handler - and therefore this outgoing filter - ever
    /// runs, so <see cref="RelaySession.UpstreamAsync"/> always resolves immediately here.
    /// </summary>
    public static void RegisterCapabilityProjectionFilter(McpServerFilters filters, RelaySession session)
    {
        filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
        {
            if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                && result.TryGetPropertyValue("capabilities", out var capabilitiesNode)
                && capabilitiesNode is JsonObject capabilities)
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                var projected = ProjectExperimentalCapabilities(upstream.ServerCapabilities?.Experimental);

                if (projected is null)
                {
                    capabilities.Remove("experimental");
                }
                else
                {
                    capabilities["experimental"] = projected;
                }
            }

            await next(context, cancellationToken).ConfigureAwait(false);
        });
    }
}
