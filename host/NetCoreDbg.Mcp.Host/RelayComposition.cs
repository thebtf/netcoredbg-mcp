using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// The only integration-owned module list and downstream host construction point. Builds
/// one explicit <see cref="RelayRouteCatalog"/>, wires the progress/logging capability-aware filter pair
/// and the paired-session bootstrap filter, and calls every accepted relay or native
/// module's <c>Register</c> method. Feature makers never edit this file directly for their
/// own module; the integrator adds an accepted module here after checker PASS.
/// </summary>
internal static class RelayComposition
{
    private const string HostServerName = "netcoredbg-mcp-host";
    private const string HostServerVersion = "1.0.0";


    /// <summary>
    /// Bootstrap-time validation that Python's advertised capabilities cover every
    /// downstream route this build always advertises. Conditionally projected resources,
    /// logging, and experimental capabilities are removed downstream when Python does not
    /// advertise them.
    /// </summary>
    public static readonly IReadOnlyList<Func<ServerCapabilities?, string?>> RequiredUpstreamCapabilityChecks =
        new Func<ServerCapabilities?, string?>[]
        {
            capabilities => capabilities?.Tools is null
                ? "Python does not advertise a tools capability, but this host build advertises tools downstream."
                : null,
        };

    /// <summary>
    /// Builds the downstream host and races it against the paired session's terminal
    /// signal, exactly mirroring the pre-FD-000 proxy loop: whichever finishes first decides
    /// whether this was a clean downstream disconnect or an unrecoverable Python/bootstrap
    /// failure that must stop serving and propagate a non-zero exit.
    /// </summary>
    public static async Task RunAsync(
        RelaySession session,
        Func<ClientCapabilities?, ClientCapabilities> projectReverseRouteCapabilities,
        ProgressLoggingRelay.NotificationState? progressNotificationState = null)
    {
        using var host = Build(session, static builder => builder.WithStdioServerTransport(), projectReverseRouteCapabilities, progressNotificationState);

        var hostRunTask = host.RunAsync();
        var sessionEndedTask = session.RunUntilSessionEndedAsync(CancellationToken.None);
        var firstCompleted = await Task.WhenAny(hostRunTask, sessionEndedTask).ConfigureAwait(false);
        if (firstCompleted == sessionEndedTask)
        {
            // The Python backend ended (or the bootstrap/capability validation failed)
            // before the downstream session closed. Stop serving and propagate the failure
            // rather than advertising a successful proxy shutdown.
            await host.StopAsync().ConfigureAwait(false);
            await sessionEndedTask.ConfigureAwait(false);
        }

        await hostRunTask.ConfigureAwait(false);
    }

    /// <summary>
    /// Builds the downstream host: one explicit <see cref="RelayRouteCatalog"/>, the
    /// progress/logging capability-aware filter pair, the paired-session bootstrap filter, and every
    /// accepted relay module's <c>Register</c> call. Internal (not private) so the focused
    /// .NET test project can build the exact same real composition over an in-memory
    /// transport instead of real stdio - a transport choice, never a mock of this method's
    /// own logic. <paramref name="configureTransport"/> and
    /// <paramref name="projectReverseRouteCapabilities"/> are the only two axes production
    /// and tests differ on; everything else here is identical for both.
    /// </summary>
    internal static IHost Build(
        RelaySession session,
        Action<IMcpServerBuilder> configureTransport,
        Func<ClientCapabilities?, ClientCapabilities> projectReverseRouteCapabilities,
        ProgressLoggingRelay.NotificationState? progressNotificationState = null)
    {
        var catalog = new RelayRouteCatalog();
        var notificationState = progressNotificationState ?? new ProgressLoggingRelay.NotificationState();

        var builder = global::Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(Array.Empty<string>());

        // stdout is reserved exclusively for the downstream MCP JSON-RPC transport; all host
        // diagnostics (and forwarded Python stderr, pumped separately by PythonBackendProcess)
        // go to stderr.
        builder.Logging.ClearProviders();
        builder.Logging.AddConsole(options => options.LogToStandardErrorThreshold = LogLevel.Trace);

        var mcpBuilder = builder.Services.AddMcpServer(options =>
        {
            options.ServerInfo = new Implementation { Name = HostServerName, Version = HostServerVersion };
            options.Capabilities = new ServerCapabilities
            {
                Tools = new ToolsCapability(),
                Prompts = new PromptsCapability { ListChanged = false },
            };

            NativePrompts.Register(options.Handlers);
            ResourcesRelay.ConfigureCapabilityProjection(options.Capabilities, options.Filters, session);
            MuxCapabilityRelay.RegisterCapabilityProjectionFilter(options.Filters, session);

            ProgressLoggingRelay.ConfigureFilters(options.Filters, session, notificationState);
            options.Filters.Message.IncomingFilters.Add(session.CreateBootstrapFilter(projectReverseRouteCapabilities));
        });

        ToolsRelay.Register(mcpBuilder, catalog, session);
        ProgressLoggingRelay.Register(mcpBuilder, catalog, session);
        RootsRelay.Register(mcpBuilder, catalog, session);
        ResourcesRelay.Register(mcpBuilder, catalog, session);
        ResourceUpdatesRelay.Register(mcpBuilder, catalog, session);
        configureTransport(mcpBuilder);

        return builder.Build();
    }
}
