using System.Diagnostics;
using System.Text.Json;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

public sealed class NativeOfflineSetupTests
{
    [Theory]
    [InlineData("setup", "managed")]
    [InlineData("--setup", "managed")]
    [InlineData("setup", "invalid")]
    [InlineData("--setup", "invalid")]
    [InlineData("setup", "path-name")]
    [InlineData("--setup", "path-name")]
    [InlineData("setup", "invalid-bridge")]
    [InlineData("--setup", "invalid-bridge")]
    public async Task ExistingDebuggerProducesNativeClientConfiguration(string command, string selection)
    {
        var candidate = TestOutputPathResolver.ResolveProcess(
            Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"),
            "NetCoreDbg.Mcp.Stateless");
        var scratch = ModernMcpScratchDirectory.Create();
        var name = OperatingSystem.IsWindows() ? "netcoredbg.exe" : "netcoredbg";
        var managedDebugger = Path.Combine(scratch, ".netcoredbg-mcp", "netcoredbg", name);
        var pathDebugger = Path.Combine(scratch, "path", name);
        var debugger = selection == "path-name" ? pathDebugger : managedDebugger;
        var bridge = Path.Combine(scratch, "bridge", "FlaUIBridge.exe");
        Directory.CreateDirectory(Path.GetDirectoryName(managedDebugger)!);
        Directory.CreateDirectory(Path.GetDirectoryName(pathDebugger)!);
        Directory.CreateDirectory(Path.GetDirectoryName(bridge)!);
        await File.WriteAllTextAsync(managedDebugger, "managed debugger placeholder");
        await File.WriteAllTextAsync(pathDebugger, "path debugger placeholder");
        await File.WriteAllTextAsync(bridge, "controlled bridge placeholder");
        if (!OperatingSystem.IsWindows())
        {
            File.SetUnixFileMode(managedDebugger, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            File.SetUnixFileMode(pathDebugger, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
        }

        using var process = new Process
        {
            StartInfo = new ProcessStartInfo(candidate.Command)
            {
                WorkingDirectory = scratch,
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            },
        };
        foreach (var argument in candidate.Arguments.Concat(new[] { "--legacy-compat", command }))
            process.StartInfo.ArgumentList.Add(argument);
        process.StartInfo.Environment["USERPROFILE"] = scratch;
        process.StartInfo.Environment["HOME"] = scratch;
        process.StartInfo.Environment["NETCOREDBG_PATH"] = selection switch
        {
            "invalid" => Path.Combine(scratch, "missing-debugger"),
            "path-name" => "netcoredbg",
            _ => "",
        };
        process.StartInfo.Environment["FLAUI_BRIDGE_PATH"] = selection == "invalid-bridge"
            ? Path.Combine(scratch, "missing-bridge.exe") : bridge;
        process.StartInfo.Environment["PATH"] = Path.GetDirectoryName(pathDebugger)!;
        process.StartInfo.Environment["DOTNET_ADD_GLOBAL_TOOLS_TO_PATH"] = "false";
        var started = false;
        try
        {
            Assert.True(process.Start());
            started = true;
            var stdout = process.StandardOutput.ReadToEndAsync();
            var stderr = process.StandardError.ReadToEndAsync();
            process.StandardInput.Close();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(15));
            await process.WaitForExitAsync(timeout.Token);
            var output = await stdout;
            var error = await stderr;
            if (selection is "invalid" or "invalid-bridge")
            {
                Assert.NotEqual(0, process.ExitCode);
                Assert.Empty(output);
                Assert.Contains(selection == "invalid" ? "NETCOREDBG_PATH" : "FLAUI_BRIDGE_PATH", error, StringComparison.Ordinal);
                return;
            }
            Assert.True(process.ExitCode == 0, error);
            using var json = JsonDocument.Parse(output);
            var config = json.RootElement.GetProperty("mcpServers").GetProperty("netcoredbg");
            Assert.False(string.IsNullOrWhiteSpace(config.GetProperty("command").GetString()));
            var args = config.GetProperty("args").EnumerateArray().Select(value => value.GetString()).ToArray();
            var actualCommand = config.GetProperty("command").GetString();
            if (candidate.Arguments.Count == 0)
                Assert.Equal(candidate.Command, actualCommand);
            else
            {
                Assert.Equal("dotnet", Path.GetFileNameWithoutExtension(actualCommand), ignoreCase: true);
                Assert.Equal(candidate.TargetPath, args[0]);
            }
            Assert.Contains("--legacy-compat", args);
            Assert.Contains("--project-from-cwd", args);
            var env = config.GetProperty("env");
            Assert.Equal(debugger, env.GetProperty("NETCOREDBG_PATH").GetString());
            Assert.Equal(bridge, env.GetProperty("FLAUI_BRIDGE_PATH").GetString());
            Assert.Contains("download", error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("dbgshim", error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("bridge", error, StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain("transport reading messages", error, StringComparison.OrdinalIgnoreCase);
        }
        finally
        {
            if (started && !process.HasExited)
            {
                try { process.Kill(); }
                catch (InvalidOperationException) when (process.HasExited) { }
                using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await process.WaitForExitAsync(timeout.Token);
            }
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }
}
