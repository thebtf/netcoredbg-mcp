using System.Diagnostics;
using ModelContextProtocol.Client;
using Xunit;

namespace NetCoreDbg.Mcp.Host.PromptTests;

public sealed class PythonBaselineLifecycleTests
{
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task InitializeAsync_NoHandshakeResponse_PreservesTimeoutAndDrainsPartialFixture(bool ignoreEof)
    {
        const string marker = "controlled baseline received initialize";
        var startInfo = new ProcessStartInfo
        {
            FileName = PythonExecutableLocator.Resolve(),
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        startInfo.ArgumentList.Add("-c");
        startInfo.ArgumentList.Add(
            "import json, sys, time; "
            + "request = json.loads(sys.stdin.readline()); "
            + "assert request['method'] == 'initialize'; "
            + $"print('{marker}', file=sys.stderr, flush=True); "
            + (ignoreEof ? "time.sleep(120)" : "sys.stdin.read()"));
        var process = Process.Start(startInfo)!;
        var processId = process.Id;
        using var observer = Process.GetProcessById(processId);
        var fixture = new PythonBaselineFixture();
        try
        {
            var startup = PythonBaselineServer.StartAsync(process, new McpClientOptions
            {
                InitializationTimeout = TimeSpan.FromSeconds(2),
            });
            var primary = await Record.ExceptionAsync(() => fixture.InitializeAsync(startup));
            var cleanup = await Record.ExceptionAsync(fixture.DisposeAsync);

            var timeout = Assert.IsType<TimeoutException>(primary);
            Assert.Equal("Initialization timed out", timeout.Message);
            Assert.Null(cleanup);
            Assert.True(observer.HasExited, "Failed initialization left its Python child running.");
            Assert.Contains(marker, Assert.IsType<string>(timeout.Data["PythonBaseline.StandardError"]));
            Assert.Equal(processId.ToString(), timeout.Data["PythonBaseline.ProcessId"]);
        }
        finally
        {
            if (!observer.HasExited)
            {
                observer.Kill(entireProcessTree: true);
                await observer.WaitForExitAsync();
            }
            process.Dispose();
        }
    }
}
