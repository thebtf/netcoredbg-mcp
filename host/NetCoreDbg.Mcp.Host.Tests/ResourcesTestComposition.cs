using System.Runtime.CompilerServices;
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
    private static readonly ConditionalWeakTable<RelaySession, ProgressLoggingRelay.NotificationState>
        s_notificationStates = new();

    public static RelaySession CreateSession(Func<IClientTransport> createUpstreamTransport)
    {
        var notificationState = new ProgressLoggingRelay.NotificationState();
        RelaySession? session = null;
        session = new RelaySession(
            () => ProgressLoggingRelay.WrapUpstreamTransport(
                createUpstreamTransport(),
                session!,
                notificationState),
            RelayComposition.RequiredUpstreamCapabilityChecks);
        s_notificationStates.Add(session, notificationState);
        return session;
    }

    public static IHost BuildHost(
        RelaySession session,
        Action<IMcpServerBuilder> configureTransport,
        Func<ClientCapabilities?, ClientCapabilities>? projectReverseRouteCapabilities = null,
        Action<McpServerOptions>? configureOptions = null)
    {
        var catalog = new RelayRouteCatalog();
        var notificationState = s_notificationStates.GetValue(
            session,
            static _ => new ProgressLoggingRelay.NotificationState());
        var builder = global::Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(Array.Empty<string>());
        builder.Logging.ClearProviders();

        var mcpBuilder = builder.Services.AddMcpServer(options =>
        {
            options.ServerInfo = new Implementation { Name = "fd005-test-host", Version = "1.0.0" };
            options.Capabilities = new ServerCapabilities { Tools = new ToolsCapability() };

            // Same production filters RelayComposition uses — never a retired parallel path.
            ProgressLoggingRelay.ConfigureFilters(options.Filters, session, notificationState);
            options.Filters.Message.IncomingFilters.Add(
                session.CreateBootstrapFilter(projectReverseRouteCapabilities ?? (static _ => new ClientCapabilities())));
            ResourcesRelay.ConfigureCapabilityProjection(options.Capabilities, options.Filters, session);
            configureOptions?.Invoke(options);
        });

        ToolsRelay.Register(mcpBuilder, catalog, session);
        ProgressLoggingRelay.Register(mcpBuilder, catalog, session);
        ResourcesRelay.Register(mcpBuilder, catalog, session);
        ResourceUpdatesRelay.Register(mcpBuilder, catalog, session);
        configureTransport(mcpBuilder);

        return builder.Build();
    }
}
