using System.Diagnostics;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

public sealed class NativeEncSetupTests
{
    [Fact]
    public void HostBuildIncludesTheActualEncSetupScript()
    {
        var candidate = Candidate();
        var script = Path.Combine(Path.GetDirectoryName(candidate.TargetPath)!, "scripts", "build-netcoredbg-enc.ps1");
        var source = Path.Combine(RepositoryLayout.Root, "scripts", "build-netcoredbg-enc.ps1");

        Assert.True(File.Exists(script), "The source-built host must include its setup script.");
        Assert.Equal(File.ReadAllBytes(source), File.ReadAllBytes(script));
    }

    [Theory]
    [InlineData("setup", false)]
    [InlineData("--setup", false)]
    [InlineData("setup", true)]
    [InlineData("--setup", true)]
    public async Task EncSetupRunsThePackagedScriptAndPropagatesItsExitCode(string command, bool conflictingProjectOptions)
    {
        if (!OperatingSystem.IsWindows()) return;
        var candidate = Candidate();
        Assert.Empty(candidate.Arguments);
        var sourceDirectory = Path.GetDirectoryName(candidate.Command)!;
        var scratch = ModernMcpScratchDirectory.Create();
        var copiedHost = Path.Combine(scratch, "host");
        using var process = new Process();
        var started = false;
        try
        {
            foreach (var file in Directory.EnumerateFiles(sourceDirectory, "*", SearchOption.AllDirectories))
            {
                var destination = Path.Combine(copiedHost, Path.GetRelativePath(sourceDirectory, file));
                Directory.CreateDirectory(Path.GetDirectoryName(destination)!);
                File.Copy(file, destination);
            }
            var script = Path.Combine(copiedHost, "scripts", "build-netcoredbg-enc.ps1");
            Directory.CreateDirectory(Path.GetDirectoryName(script)!);
            await File.WriteAllTextAsync(script, "[Console]::Error.WriteLine('owned-enc-setup-stderr'); Write-Output 'owned-enc-setup-fixture'; exit 37\n");
            var powershellDirectory = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.System), "WindowsPowerShell", "v1.0");
            Assert.True(File.Exists(Path.Combine(powershellDirectory, "powershell.exe")));

            process.StartInfo = new ProcessStartInfo(Path.Combine(copiedHost, Path.GetFileName(candidate.Command)))
            {
                WorkingDirectory = scratch,
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            var arguments = conflictingProjectOptions
                ? new[] { "--legacy-compat", "--project", ".", "--project-from-cwd", command, "--enc" }
                : new[] { "--legacy-compat", command, "--enc" };
            foreach (var argument in arguments)
                process.StartInfo.ArgumentList.Add(argument);
            process.StartInfo.Environment["PATH"] = powershellDirectory;
            process.StartInfo.Environment["USERPROFILE"] = scratch;
            process.StartInfo.Environment["HOME"] = scratch;
            process.StartInfo.Environment["TEMP"] = scratch;
            process.StartInfo.Environment["TMP"] = scratch;
            process.StartInfo.Environment["DOTNET_ADD_GLOBAL_TOOLS_TO_PATH"] = "false";

            Assert.True(process.Start());
            started = true;
            var output = process.StandardOutput.ReadToEndAsync();
            var error = process.StandardError.ReadToEndAsync();
            process.StandardInput.Close();
            using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(20));
            await process.WaitForExitAsync(timeout.Token);
            var stdout = await output;
            var stderr = await error;
            if (conflictingProjectOptions)
            {
                Assert.Equal(2, process.ExitCode);
                Assert.DoesNotContain("owned-enc-setup-fixture", stdout, StringComparison.Ordinal);
                Assert.DoesNotContain("owned-enc-setup-stderr", stderr, StringComparison.Ordinal);
            }
            else
            {
                Assert.Equal(37, process.ExitCode);
                Assert.Contains("owned-enc-setup-fixture", stdout, StringComparison.Ordinal);
                Assert.Contains("owned-enc-setup-stderr", stderr, StringComparison.Ordinal);
            }
            Assert.DoesNotContain("transport reading messages", stderr, StringComparison.OrdinalIgnoreCase);
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

    private static TestOutputProcess Candidate() => TestOutputPathResolver.ResolveProcess(
        Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"),
        "NetCoreDbg.Mcp.Stateless");
}
