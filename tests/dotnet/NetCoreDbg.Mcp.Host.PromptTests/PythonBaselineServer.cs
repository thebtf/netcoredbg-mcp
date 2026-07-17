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

    private PythonBaselineServer(Process process, McpClient client)
    {
        _process = process;
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
        _ = process.StandardError.ReadToEndAsync();

        var transport = new StreamClientTransport(process.StandardInput.BaseStream, process.StandardOutput.BaseStream);
        var client = await McpClient.CreateAsync(transport);

        return new PythonBaselineServer(process, client);
    }

    public async ValueTask DisposeAsync()
    {
        await Client.DisposeAsync();
        _process.StandardInput.Close();
        if (!_process.WaitForExit(5000))
        {
            _process.Kill(entireProcessTree: true);
        }

        _process.Dispose();
    }
}
