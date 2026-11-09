using System.Diagnostics;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

public sealed class NativeCommandLineTests
{
    [Fact]
    public async Task VersionExitsWithoutStartingMcp()
    {
        var result = await RunAsync("--legacy-compat", "--version");

        Assert.Equal(0, result.ExitCode);
        Assert.Matches(@"netcoredbg-mcp-stateless\s+\d+\.\d+\.\d+", result.StandardOutput);
        Assert.DoesNotContain("transport reading messages", result.StandardError, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task HelpExitsAndDescribesNativeOptions()
    {
        var result = await RunAsync("--legacy-compat", "--help");

        Assert.Equal(0, result.ExitCode);
        Assert.Contains("--project", result.StandardOutput, StringComparison.Ordinal);
        Assert.Contains("--project-from-cwd", result.StandardOutput, StringComparison.Ordinal);
        Assert.Contains("--version", result.StandardOutput, StringComparison.Ordinal);
        Assert.DoesNotContain("transport reading messages", result.StandardError, StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData("--not-a-real-option")]
    [InlineData("--project")]
    [InlineData("--enc")]
    public async Task InvalidArgumentsFailWithoutStartingMcp(string argument)
    {
        var result = await RunAsync("--legacy-compat", argument);

        Assert.NotEqual(0, result.ExitCode);
        Assert.NotEmpty(result.StandardError);
        Assert.DoesNotContain("transport reading messages", result.StandardError, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task ConflictingProjectOptionsFailWithoutStartingMcp()
    {
        var result = await RunAsync("--legacy-compat", "--project", ".", "--project-from-cwd");

        Assert.NotEqual(0, result.ExitCode);
        Assert.NotEmpty(result.StandardError);
        Assert.DoesNotContain("transport reading messages", result.StandardError, StringComparison.OrdinalIgnoreCase);
    }

    [Theory]
    [InlineData("--setup")]
    [InlineData("setup")]
    public async Task UnsupportedSetupIsReportedInsteadOfStartingMcp(string setup)
    {
        var result = await RunAsync("--legacy-compat", setup);

        Assert.NotEqual(0, result.ExitCode);
        Assert.Contains("setup", result.StandardError, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("transport reading messages", result.StandardError, StringComparison.OrdinalIgnoreCase);
    }

    [Fact]
    public async Task EncSetupWithoutPowerShellFailsBeforeInstallation()
    {
        var result = await RunCoreAsync(true, ["--legacy-compat", "setup", "--enc"]);

        Assert.Equal(1, result.ExitCode);
        Assert.Contains("PowerShell", result.StandardError, StringComparison.OrdinalIgnoreCase);
        Assert.DoesNotContain("transport reading messages", result.StandardError, StringComparison.OrdinalIgnoreCase);
    }

    private static Task<(int ExitCode, string StandardOutput, string StandardError)> RunAsync(params string[] arguments) =>
        RunCoreAsync(false, arguments);

    private static async Task<(int ExitCode, string StandardOutput, string StandardError)> RunCoreAsync(bool noPowerShell, string[] arguments)
    {
        var candidate = TestOutputPathResolver.ResolveProcess(
            Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"),
            "NetCoreDbg.Mcp.Stateless");
        var scratch = ModernMcpScratchDirectory.Create();
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo(candidate.Command)
            {
                WorkingDirectory = RepositoryLayout.Root,
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            },
        };
        foreach (var argument in candidate.Arguments.Concat(arguments))
            process.StartInfo.ArgumentList.Add(argument);
        process.StartInfo.Environment["NETCOREDBG_PATH"] = Path.Combine(scratch, "absent-debugger");
        process.StartInfo.Environment["USERPROFILE"] = scratch;
        process.StartInfo.Environment["HOME"] = scratch;
        process.StartInfo.Environment["DOTNET_ADD_GLOBAL_TOOLS_TO_PATH"] = "false";
        process.StartInfo.Environment["DOTNET_NOLOGO"] = "1";
        if (noPowerShell) process.StartInfo.Environment["PATH"] = scratch;
        var started = false;
        try
        {
            Assert.True(process.Start());
            started = true;
            var output = process.StandardOutput.ReadToEndAsync();
            var error = process.StandardError.ReadToEndAsync();
            process.StandardInput.Close();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await process.WaitForExitAsync(timeout.Token);
            return (process.ExitCode, await output, await error);
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
