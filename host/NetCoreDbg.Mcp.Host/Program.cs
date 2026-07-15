using System.Diagnostics;
using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Entry point for the .NET MCP compatibility host. Starts the existing Python
/// netcoredbg-mcp server as an upstream MCP session over stdio, then dynamically
/// forwards <c>tools/list</c> and <c>tools/call</c> to it for whatever downstream
/// MCP client launches this process. Python remains the sole implementation of every
/// tool; this host does not reconstruct or translate tool schemas or results.
/// </summary>
public static class Program
{
    private const string HostServerName = "netcoredbg-mcp-host";
    private const string HostServerVersion = "1.0.0";

    /// <summary>
    /// Overrides only the Python executable used to launch the backend. Never forwarded
    /// to the child as a CLI argument.
    /// </summary>
    private const string PythonExecutableEnvironmentVariable = "NETCOREDBG_MCP_PYTHON_EXECUTABLE";

    private const string DefaultPythonExecutable = "python";

    private static readonly JsonElement IsolatedMuxSharing =
        JsonDocument.Parse("""{"sharing":"isolated"}""").RootElement;

    public static async Task<int> Main(string[] args)
    {
        Process pythonProcess;
        try
        {
            pythonProcess = StartPythonProcess(args);
        }
        catch (Exception ex)
        {
            Console.Error.WriteLine(
                $"[{HostServerName}] Failed to start the Python backend: {ex}");
            return 1;
        }

        var exitCode = 0;
        using (pythonProcess)
        {
            // Preserve the child diagnostics byte-for-byte while keeping stdout
            // exclusively available for the downstream MCP JSON-RPC transport.
            var stderrPump = pythonProcess.StandardError.BaseStream.CopyToAsync(
                Console.OpenStandardError());
            var processStopped = false;

            try
            {
                var upstreamTransport = new StreamClientTransport(
                    pythonProcess.StandardInput.BaseStream,
                    pythonProcess.StandardOutput.BaseStream);

                await using var upstream = await McpClient.CreateAsync(upstreamTransport);
                await RunProxyAsync(upstream);
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
                    await StopPythonProcessAsync(pythonProcess);
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
                        await stderrPump;
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

    private static async Task RunProxyAsync(McpClient upstream)
    {
        var builder = global::Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder(Array.Empty<string>());

        // stdout is reserved exclusively for the downstream MCP JSON-RPC transport;
        // all host diagnostics (and forwarded Python stderr) go to stderr.
        builder.Logging.ClearProviders();
        builder.Logging.AddConsole(options => options.LogToStandardErrorThreshold = LogLevel.Trace);

        builder.Services
            .AddMcpServer(options =>
            {
                options.ServerInfo = new Implementation
                {
                    Name = HostServerName,
                    Version = HostServerVersion,
                };
                options.Capabilities = new ServerCapabilities
                {
                    // Mirrors the Python server's own x-mux.sharing=isolated capability so
                    // mcp-mux gives this compatibility process its own isolated session.
                    Experimental = new Dictionary<string, object> { ["x-mux"] = IsolatedMuxSharing },
                };
            })
            .WithStdioServerTransport()
            // Forward the exact protocol request params and result objects; no schema
            // reconstruction, no local tool registration, no result translation.
            .WithListToolsHandler((request, cancellationToken) =>
                // Some clients omit `params` entirely for a cursor-less tools/list;
                // the SDK then leaves RequestContext.Params null even though the
                // client-side type isn't nullable. Forward an empty page request.
                upstream.ListToolsAsync(request.Params ?? new(), cancellationToken))
            .WithCallToolHandler((request, cancellationToken) =>
                upstream.CallToolAsync(request.Params, cancellationToken));

        using var host = builder.Build();

        var hostRunTask = host.RunAsync();
        var firstCompleted = await Task.WhenAny(hostRunTask, upstream.Completion);
        if (firstCompleted == upstream.Completion)
        {
            // The Python backend ended before the downstream session closed. Stop
            // serving, propagate any transport fault, and treat a clean early EOF as
            // a failure rather than advertising a successful proxy shutdown.
            await host.StopAsync();
            await upstream.Completion;
            throw new InvalidOperationException(
                "The Python backend ended before the downstream MCP session closed.");
        }

        await hostRunTask;
    }

    /// <summary>Resolves the executable used to launch the Python backend.</summary>
    private static string ResolvePythonExecutable()
    {
        var overridden = Environment.GetEnvironmentVariable(PythonExecutableEnvironmentVariable);
        return string.IsNullOrEmpty(overridden) ? DefaultPythonExecutable : overridden;
    }

    /// <summary>
    /// Builds the full child process argument list: the fixed <c>-m netcoredbg_mcp</c> module
    /// invocation followed by every ordinary host CLI argument. Values remain discrete process
    /// arguments and must come only from trusted launch-time configuration.
    /// </summary>
    private static List<string> BuildPythonArguments(IReadOnlyList<string> hostArguments)
    {
        var arguments = new List<string>(hostArguments.Count + 2) { "-m", "netcoredbg_mcp" };
        arguments.AddRange(hostArguments);
        return arguments;
    }

    /// <summary>
    /// Starts the Python backend directly. <see cref="ProcessStartInfo.ArgumentList"/> keeps
    /// every value as a process argument on every supported platform; no command shell parses,
    /// expands, or rewrites the executable or arguments.
    /// </summary>
    private static Process StartPythonProcess(IReadOnlyList<string> hostArguments)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = ResolvePythonExecutable(),
            WorkingDirectory = Environment.CurrentDirectory,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        foreach (var argument in BuildPythonArguments(hostArguments))
        {
            startInfo.ArgumentList.Add(argument);
        }

        var process = new Process { StartInfo = startInfo };
        try
        {
            if (!process.Start())
            {
                throw new InvalidOperationException("The Python backend process did not start.");
            }

            return process;
        }
        catch
        {
            process.Dispose();
            throw;
        }
    }

    private static async Task StopPythonProcessAsync(Process process)
    {
        try
        {
            process.StandardInput.Close();
        }
        catch (IOException)
        {
            // The child may have already closed its input while terminating.
        }
        catch (InvalidOperationException)
        {
            // The child already exited before its redirected input was observed.
        }

        if (process.HasExited)
        {
            return;
        }

        try
        {
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        }
        catch (TimeoutException)
        {
            process.Kill(entireProcessTree: true);
            await process.WaitForExitAsync();
        }
    }
}
