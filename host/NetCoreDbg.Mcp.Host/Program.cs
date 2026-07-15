using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Entry point for the .NET MCP compatibility host. Starts the existing Python
/// netcoredbg-mcp server as an upstream MCP session over stdio, then forwards the routes
/// <see cref="RelayComposition"/> registers to it for whatever downstream MCP client
/// launches this process. Python remains the sole implementation of every tool; this host
/// does not reconstruct or translate tool schemas or results.
///
/// This file owns only top-level composition and exit-code reporting; every other concern
/// - process lifecycle, paired-session bootstrap, route registration, and handler wiring -
/// lives in <see cref="PythonBackendProcess"/>, <see cref="RelaySession"/>,
/// <see cref="RelayRouteCatalog"/>, <see cref="RelayComposition"/>, and the accepted relay modules.
/// </summary>
public static class Program
{
    private const string HostServerName = "netcoredbg-mcp-host";

    public static async Task<int> Main(string[] args)
    {
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
                RelaySession relaySession = null!;
                relaySession = new RelaySession(
                    pythonBackend.CreateUpstreamTransport,
                    RelayComposition.RequiredUpstreamCapabilityChecks,
                    handlers => rootsRelay.ConfigureUpstreamHandlers(handlers, relaySession));
                await using (relaySession)
                {
                    await RelayComposition.RunAsync(
                        relaySession,
                        downstreamCapabilities => rootsRelay.ProjectCapabilities(
                            downstreamCapabilities,
                            new ClientCapabilities()));
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
