using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Test-only mirror of the accepted resource modules over a caller-selected real transport.
/// Production integration remains owned by <c>RelayComposition</c>; focused FD-005/FD-006
/// tests use this fixture to exercise the modules without editing the common module list.
/// </summary>
internal static class ResourcesTestComposition
{
    public static RelaySession CreateSession(Func<IClientTransport> createUpstreamTransport)
    {
        var resourceUpdates = ResourceUpdatesRelay.CreateOrderedUpstream();
        RelaySession? session = null;
        session = new RelaySession(
            () => resourceUpdates.WrapTransport(createUpstreamTransport()),
            RelayComposition.RequiredUpstreamCapabilityChecks,
            handlers => resourceUpdates.ConfigureHandlers(handlers, session!),
            resourceUpdates.WaitForDrainAsync);
        return session;
    }

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
        ResourceUpdatesRelay.Register(mcpBuilder, catalog, session);
        configureTransport(mcpBuilder);

        return builder.Build();
    }
}
