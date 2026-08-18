using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Entry point for the .NET MCP compatibility host. Starts the existing Python
/// netcoredbg-mcp server as an upstream MCP session over stdio, then composes its relay
/// routes with the native project-scoped code-search replacements. Python remains the
/// implementation of every other tool.
///
/// This file owns only top-level composition and exit-code reporting; every other concern
/// - process lifecycle, paired-session bootstrap, route registration, handler wiring, and
/// native source navigation - lives in <see cref="PythonBackendProcess"/>,
/// <see cref="RelaySession"/>, <see cref="RelayRouteCatalog"/>,
/// <see cref="RelayComposition"/>, <see cref="ProjectRootResolver"/>, and their modules.
/// </summary>
public static class Program
{
    private const string HostServerName = "netcoredbg-mcp-host";

    public static async Task<int> Main(string[] args)
    {
        var projectRootResolver = ProjectRootResolver.FromHostArguments(args, Environment.CurrentDirectory);
        PythonBackendProcess pythonBackend;
        try
        {
            pythonBackend = PythonBackendProcess.Start(args);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(
                $"[{HostServerName}] Failed to start the Python backend: {ex}");
            return 1;
        }

        var exitCode = 0;
        using (pythonBackend)
        {
            var processStopped = false;

            try
            {
                var rootsRelay = new RootsRelay();
                var progressNotificationState = new ProgressLoggingRelay.NotificationState();
                RelaySession relaySession = null!;
                relaySession = new RelaySession(
                    () => ProgressLoggingRelay.WrapUpstreamTransport(
                        pythonBackend.CreateUpstreamTransport(),
                        relaySession,
                        progressNotificationState),
                    RelayComposition.RequiredUpstreamCapabilityChecks,
                    handlers => rootsRelay.ConfigureUpstreamHandlers(handlers, relaySession));
                await using (relaySession)
                {
                    await RelayComposition.RunAsync(
                        relaySession,
                        downstreamCapabilities => rootsRelay.ProjectCapabilities(
                            downstreamCapabilities,
                            new ClientCapabilities()),
                        progressNotificationState,
                        projectRootResolver);
                }
            }
            catch (Exception ex)
            {
                Console.Error.WriteLine(
                    $"[{HostServerName}] Python backend or MCP proxy failed: {ex}");
                exitCode = 1;
            }
            finally
            {
                try
                {
                    await pythonBackend.StopAsync();
                    processStopped = true;
                }
                catch (Exception ex)
                {
                    Console.Error.WriteLine(
                        $"[{HostServerName}] Failed to stop the Python backend: {ex}");
                    exitCode = 1;
                }

                if (processStopped)
                {
                    try
                    {
                        await pythonBackend.WaitForStderrForwardedAsync();
                    }
                    catch (Exception ex)
                    {
                        Console.Error.WriteLine(
                            $"[{HostServerName}] Failed to forward Python stderr: {ex}");
                        exitCode = 1;
                    }
                }
            }
        }

        return exitCode;
    }
}
