using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Builds a downstream host that wires <see cref="ProgressLoggingRelay"/> with the production
/// composition shape: <c>ProgressLoggingRelay.ConfigureFilters</c> in the
/// <c>AddMcpServer(options =&gt; ...)</c> block, <c>ProgressLoggingRelay.Register</c> alongside
/// <c>ToolsRelay.Register</c>, and <c>ProgressLoggingRelay.WrapUpstreamTransport</c> wrapping the
/// upstream transport factory passed to <see cref="RelaySession"/>'s constructor.
/// </summary>
internal static class ProgressLoggingComposition
{
    private const string HostServerName = "netcoredbg-mcp-host-fd002-test";
    private const string HostServerVersion = "1.0.0";

    public static (RelaySession Session, IHost Host) Build(
        Func<IClientTransport> createUpstreamTransport,
        Action<IMcpServerBuilder> configureTransport,
        IReadOnlyList<Func<ServerCapabilities?, string?>>? requiredUpstreamCapabilityChecks = null,
        Action<ProgressLoggingRelay.NotificationState>? observeNotificationState = null)
    {
        var notificationState = new ProgressLoggingRelay.NotificationState();
        observeNotificationState?.Invoke(notificationState);
        RelaySession? session = null;
        session = new RelaySession(
            () => ProgressLoggingRelay.WrapUpstreamTransport(createUpstreamTransport(), session!, notificationState),
            requiredUpstreamCapabilityChecks ?? RelayComposition.RequiredUpstreamCapabilityChecks);

        var catalog = new RelayRouteCatalog();
        var builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(Array.Empty<string>());
        builder.Logging.ClearProviders();
        builder.Logging.AddConsole(options => options.LogToStandardErrorThreshold = LogLevel.Trace);

        var mcpBuilder = builder.Services.AddMcpServer(options =>
        {
            options.ServerInfo = new Implementation { Name = HostServerName, Version = HostServerVersion };
            options.Capabilities = new ServerCapabilities { Tools = new ToolsCapability() };

            ProgressLoggingRelay.ConfigureFilters(options.Filters, session, notificationState);
            options.Filters.Message.IncomingFilters.Add(session.CreateBootstrapFilter(static _ => new ClientCapabilities()));
        });

        ToolsRelay.Register(mcpBuilder, catalog, session);
        ProgressLoggingRelay.Register(mcpBuilder, catalog, session);
        configureTransport(mcpBuilder);

        var host = builder.Build();
        _ = host.RunAsync();

        return (session, host);
    }
}
