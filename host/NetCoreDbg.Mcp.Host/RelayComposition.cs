using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// The only integration-owned module list and downstream host construction point. Builds
/// one explicit <see cref="RelayRouteCatalog"/>, wires the logging-suppression filter pair
/// and the paired-session bootstrap filter, and calls every accepted relay or native
/// module's <c>Register</c> method. Feature makers never edit this file directly for their
/// own module; the integrator adds an accepted module here after checker PASS.
/// </summary>
internal static class RelayComposition
{
    private const string HostServerName = "netcoredbg-mcp-host";
    private const string HostServerVersion = "1.0.0";

    private static readonly JsonElement IsolatedMuxSharing =
        JsonDocument.Parse("""{"sharing":"isolated"}""").RootElement;

    /// <summary>
    /// Bootstrap-time validation that Python's advertised capabilities cover every
    /// downstream route this build advertises: only the tools route for FD-000. FD-001/
    /// FD-002 add their own checks here alongside their module registration, once accepted.
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
    public static async Task RunAsync(RelaySession session)
    {
        using var host = Build(session, static builder => builder.WithStdioServerTransport(), static _ => new ClientCapabilities());

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
    /// logging-suppression filter pair, the paired-session bootstrap filter, and every
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
        Func<ClientCapabilities?, ClientCapabilities> projectReverseRouteCapabilities)
    {
        var catalog = new RelayRouteCatalog();

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
                // Mirrors the Python server's own x-mux.sharing=isolated capability so
                // mcp-mux gives this compatibility process its own isolated session.
                Experimental = new Dictionary<string, object> { ["x-mux"] = IsolatedMuxSharing },
                Tools = new ToolsCapability(),
                Prompts = new PromptsCapability { ListChanged = false },
            };

            NativePrompts.Register(options.Handlers);

            RelayRouteCatalog.SuppressUnregisteredLogging(options.Filters);
            options.Filters.Message.IncomingFilters.Add(session.CreateBootstrapFilter(projectReverseRouteCapabilities));
        });

        ToolsRelay.Register(mcpBuilder, catalog, session);
        configureTransport(mcpBuilder);

        return builder.Build();
    }
}
