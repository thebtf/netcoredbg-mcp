using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// FD-005's own test-only mirror of <c>RelayComposition.Build</c>: tools + the
/// logging-suppression filter + the bootstrap filter, exactly like production, plus
/// <see cref="ResourcesRelay"/> registered. <see cref="ResourcesRelay"/> is not yet part of
/// the production <c>RelayComposition.Build</c> module list - per architecture.md, "the
/// integrator adds the accepted module to the central list after checker PASS" - so this is
/// how both <see cref="ResourcesRelayTests"/> (in-memory fake Python) and
/// <see cref="ResourcesRealPythonTests"/> (real stdio Python) prove the module directly, the
/// same convention <c>ReverseRouteAndLifecycleTests</c> already uses for FD-000's reverse
/// route. No line here differs from what the integrator will add to the real file.
/// </summary>
internal static class ResourcesTestComposition
{
    public static IHost BuildHost(
        RelaySession session,
        Action<IMcpServerBuilder> configureTransport,
        Func<ClientCapabilities?, ClientCapabilities>? projectReverseRouteCapabilities = null)
    {
        var catalog = new RelayRouteCatalog();
        var builder = global::Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(Array.Empty<string>());
        builder.Logging.ClearProviders();

        var mcpBuilder = builder.Services.AddMcpServer(options =>
        {
            options.ServerInfo = new Implementation { Name = "fd005-test-host", Version = "1.0.0" };
            options.Capabilities = new ServerCapabilities { Tools = new ToolsCapability() };

            RelayRouteCatalog.SuppressUnregisteredLogging(options.Filters);
            options.Filters.Message.IncomingFilters.Add(
                session.CreateBootstrapFilter(projectReverseRouteCapabilities ?? (static _ => new ClientCapabilities())));
            ResourcesRelay.ConfigureCapabilityProjection(options.Capabilities, options.Filters, session);
        });

        ToolsRelay.Register(mcpBuilder, catalog, session);
        ResourcesRelay.Register(mcpBuilder, catalog, session);
        configureTransport(mcpBuilder);

        return builder.Build();
    }
}
