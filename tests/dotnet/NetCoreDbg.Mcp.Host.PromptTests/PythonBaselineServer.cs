using System.Diagnostics;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Launches the real, unmodified <c>python -m netcoredbg_mcp</c> server as a child process
/// over real stdio pipes - the actual "direct Python server baseline" the PR-001 acceptance
/// criteria call for, not a re-implementation of its behavior. Mirrors
/// <c>tests/test_prompts.py</c>'s own <c>mcp_server</c> fixture: only
/// <c>NETCOREDBG_PATH</c> needs to be set, because <c>DAPClient.__init__</c> only calls
/// the (unmockable-from-here) <c>_find_netcoredbg()</c> when no path is supplied at all,
/// and prompts never touch the debugger regardless.
/// </summary>
public sealed class PythonBaselineServer : IAsyncDisposable
{
    private readonly Process _process;
    private readonly Task<string> _standardError;

    private PythonBaselineServer(Process process, McpClient client, Task<string> standardError)
    {
        _process = process;
        _standardError = standardError;
        Client = client;
    }

    public McpClient Client { get; }

    public static async Task<PythonBaselineServer> StartAsync()
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = PythonExecutableLocator.Resolve(),
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        startInfo.ArgumentList.Add("-m");
        startInfo.ArgumentList.Add("netcoredbg_mcp");
        startInfo.Environment["NETCOREDBG_PATH"] = "/fake/netcoredbg";

        var process = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Failed to start the direct Python baseline server.");
        return await StartAsync(process);
    }

    internal static async Task<PythonBaselineServer> StartAsync(Process process, McpClientOptions? clientOptions = null)
    {
        var standardError = process.StandardError.ReadToEndAsync();
        var processId = process.Id.ToString();
        var executable = process.StartInfo.FileName;
        var workingDirectory = string.IsNullOrEmpty(process.StartInfo.WorkingDirectory)
            ? Environment.CurrentDirectory
            : process.StartInfo.WorkingDirectory;
        try
        {
            var transport = new StreamClientTransport(process.StandardInput.BaseStream, process.StandardOutput.BaseStream);
            var client = await McpClient.CreateAsync(transport, clientOptions);
            return new PythonBaselineServer(process, client, standardError);
        }
        catch (Exception primary)
        {
            primary.Data["PythonBaseline.ProcessId"] = processId;
            primary.Data["PythonBaseline.Executable"] = executable;
            primary.Data["PythonBaseline.WorkingDirectory"] = workingDirectory;
            try
            {
                primary.Data["PythonBaseline.StandardError"] = await StopProcessAsync(process, standardError);
            }
            catch (Exception cleanup)
            {
                primary.Data["PythonBaseline.CleanupFailure"] = cleanup.ToString();
            }
            Console.Error.WriteLine($"Direct Python baseline initialization failed: {primary.GetType().Name}: {primary.Message}; executable={executable}; cwd={workingDirectory}; pid={processId}\n{primary.Data["PythonBaseline.StandardError"]}\n{primary.Data["PythonBaseline.CleanupFailure"]}");
            throw;
        }
    }

    public async ValueTask DisposeAsync()
    {
        try
        {
            await Client.DisposeAsync();
        }
        finally
        {
            await StopProcessAsync(_process, _standardError);
        }
    }

    private static async Task<string> StopProcessAsync(Process process, Task<string> standardError)
    {
        try
        {
            process.StandardInput.Close();
            if (!process.WaitForExit(5000))
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync();
            }
            return await standardError;
        }
        finally
        {
            process.Dispose();
        }
    }
}
