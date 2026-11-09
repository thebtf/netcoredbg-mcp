using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

public sealed class NativeBundledBridgeTests
{
    [WindowsBundledBridgeTheory]
    [InlineData("bundled")]
    [InlineData("explicit")]
    [InlineData("managed")]
    [InlineData("invalid")]
    [InlineData("absent")]
    public async Task Setup_SelectsBridgeByPrecedenceWithoutOverwritingFiles(string scenario)
    {
        await using var sandbox = await Sandbox.CreateAsync(scenario);
        var packageBefore = Snapshot(sandbox.App);
        var homeBefore = Snapshot(sandbox.Home);
        var explicitBefore = Snapshot(sandbox.Fixtures);

        var result = await sandbox.SetupAsync(scenario);

        AssertSnapshotsEqual(packageBefore, Snapshot(sandbox.App));
        AssertSnapshotsPreserved(homeBefore, Snapshot(sandbox.Home));
        AssertSnapshotsEqual(explicitBefore, Snapshot(sandbox.Fixtures));
        if (scenario == "invalid")
        {
            Assert.NotEqual(0, result.ExitCode);
            Assert.Empty(result.Output);
            Assert.Contains("FLAUI_BRIDGE_PATH", result.Error, StringComparison.Ordinal);
            return;
        }

        Assert.True(result.ExitCode == 0, result.Error);
        using var json = JsonDocument.Parse(result.Output);
        var config = json.RootElement.GetProperty("mcpServers").GetProperty("netcoredbg");
        Assert.Equal(sandbox.Dotnet, config.GetProperty("command").GetString(), ignoreCase: true);
        var args = config.GetProperty("args").EnumerateArray().Select(value => value.GetString()).ToArray();
        Assert.Equal(new[] { sandbox.HostAssembly, "--legacy-compat", "--project-from-cwd" }, args);
        var env = config.GetProperty("env");
        Assert.Equal(sandbox.Debugger, env.GetProperty("NETCOREDBG_PATH").GetString());
        if (scenario == "absent")
            Assert.False(env.TryGetProperty("FLAUI_BRIDGE_PATH", out _));
        else
            Assert.Equal(scenario switch
            {
                "explicit" => sandbox.ExplicitBridge,
                "managed" => sandbox.ManagedBridge,
                _ => sandbox.BundledBridge,
            }, env.GetProperty("FLAUI_BRIDGE_PATH").GetString());
    }

    private sealed record FileSnapshot(string Sha256, DateTime LastWriteTimeUtc);

    private static Dictionary<string, FileSnapshot> Snapshot(string root) =>
        Directory.EnumerateFiles(root, "*", SearchOption.AllDirectories).ToDictionary(
            path => Path.GetRelativePath(root, path),
            path => new FileSnapshot(Convert.ToHexString(SHA256.HashData(File.ReadAllBytes(path))), File.GetLastWriteTimeUtc(path)),
            StringComparer.OrdinalIgnoreCase);

    private static void AssertSnapshotsEqual(Dictionary<string, FileSnapshot> expected, Dictionary<string, FileSnapshot> actual)
    {
        Assert.Equal(expected.Keys.OrderBy(key => key, StringComparer.OrdinalIgnoreCase),
            actual.Keys.OrderBy(key => key, StringComparer.OrdinalIgnoreCase));
        foreach (var (path, before) in expected)
        {
            Assert.Equal(before.Sha256, actual[path].Sha256);
            Assert.Equal(before.LastWriteTimeUtc, actual[path].LastWriteTimeUtc);
        }
    }

    private static void AssertSnapshotsPreserved(Dictionary<string, FileSnapshot> expected, Dictionary<string, FileSnapshot> actual)
    {
        foreach (var (path, before) in expected)
        {
            Assert.True(actual.TryGetValue(path, out var after));
            Assert.Equal(before.Sha256, after!.Sha256);
            Assert.Equal(before.LastWriteTimeUtc, after.LastWriteTimeUtc);
        }
    }

    private sealed record SetupResult(int ExitCode, string Output, string Error);

    private sealed class Sandbox : IAsyncDisposable
    {
        private bool _preserve;
        internal string Root { get; }
        internal string App => Path.Combine(Root, "app");
        internal string Home => Path.Combine(Root, "home");
        internal string Fixtures => Path.Combine(Root, "fixtures");
        internal string HostAssembly => Path.Combine(App, "NetCoreDbg.Mcp.Stateless.dll");
        internal string Debugger => Path.Combine(Fixtures, "netcoredbg.exe");
        internal string ExplicitBridge => Path.Combine(Fixtures, "FlaUIBridge.exe");
        internal string ManagedBridge => Path.Combine(Home, ".netcoredbg-mcp", "bridge", "FlaUIBridge.exe");
        internal string BundledBridge => Path.Combine(App, "bridge", "FlaUIBridge.exe");
        internal string Dotnet { get; }

        private Sandbox(string root)
        {
            Root = root;
            var runtimeDirectory = new DirectoryInfo(RuntimeEnvironment.GetRuntimeDirectory());
            var dotnetRoot = runtimeDirectory.Parent?.Parent?.Parent?.FullName
                ?? throw new InvalidOperationException("Could not locate the executing dotnet host.");
            Dotnet = Path.Combine(dotnetRoot, "dotnet.exe");
            if (!File.Exists(Dotnet)) throw new FileNotFoundException("The executing runtime's dotnet host is required.", Dotnet);
        }

        internal static async Task<Sandbox> CreateAsync(string scenario)
        {
            var source = TestOutputPathResolver.ResolveManagedAssembly(RepositoryLayout.Root,
                Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");
            var sandbox = new Sandbox(ModernMcpScratchDirectory.Create());
            try
            {
                CopyOutput(Path.GetDirectoryName(source)!, sandbox.App, isRoot: true);
                Directory.CreateDirectory(sandbox.Home);
                Directory.CreateDirectory(sandbox.Fixtures);
                await File.WriteAllBytesAsync(sandbox.Debugger, [1, 2, 3]);
                if (scenario != "absent") await WriteFixtureAsync(sandbox.BundledBridge, [4, 5, 6]);
                if (scenario is "explicit" or "managed") await WriteFixtureAsync(sandbox.ManagedBridge, [7, 8, 9]);
                if (scenario == "explicit") await WriteFixtureAsync(sandbox.ExplicitBridge, [10, 11, 12]);
                await WriteFixtureAsync(Path.Combine(sandbox.Home, ".netcoredbg-mcp", "dbgshim", "12345.71.31", "dbgshim.dll"), [13, 14, 15]);
                await WriteFixtureAsync(Path.Combine(sandbox.Root, "runtime", "shared", "Microsoft.NETCore.App", "12345.71.31", "dbgshim.dll"), [16, 17, 18]);
                return sandbox;
            }
            catch
            {
                await sandbox.DisposeAsync();
                throw;
            }
        }

        private static async Task WriteFixtureAsync(string path, byte[] bytes)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            await File.WriteAllBytesAsync(path, bytes);
        }

        private static void CopyOutput(string source, string destination, bool isRoot)
        {
            if ((File.GetAttributes(source) & FileAttributes.ReparsePoint) != 0)
                throw new IOException($"Refusing reparse-point output directory: {source}");
            Directory.CreateDirectory(destination);
            foreach (var entry in Directory.EnumerateFileSystemEntries(source))
            {
                var attributes = File.GetAttributes(entry);
                if ((attributes & FileAttributes.ReparsePoint) != 0)
                    throw new IOException($"Refusing reparse-point output entry: {entry}");
                var name = Path.GetFileName(entry);
                if (isRoot && string.Equals(name, "bridge", StringComparison.OrdinalIgnoreCase)) continue;
                var target = Path.Combine(destination, name);
                if ((attributes & FileAttributes.Directory) != 0) CopyOutput(entry, target, isRoot: false);
                else File.Copy(entry, target);
            }
        }

        internal async Task<SetupResult> SetupAsync(string scenario)
        {
            using var process = new Process { StartInfo = new ProcessStartInfo(Dotnet)
            {
                WorkingDirectory = Root, UseShellExecute = false, RedirectStandardInput = true,
                RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
            } };
            process.StartInfo.ArgumentList.Add(HostAssembly);
            process.StartInfo.ArgumentList.Add("--legacy-compat");
            process.StartInfo.ArgumentList.Add("setup");
            var environment = process.StartInfo.Environment;
            environment["HOME"] = Home;
            environment["USERPROFILE"] = Home;
            environment.Remove("HOMEDRIVE");
            environment.Remove("HOMEPATH");
            environment["PATH"] = Fixtures;
            environment["DOTNET_ROOT"] = Path.Combine(Root, "runtime");
            environment.Remove("DOTNET_ROOT_X64");
            environment.Remove("DOTNET_ROOT_X86");
            environment.Remove("DOTNET_ROOT_ARM64");
            environment.Remove("DOTNET_HOST_PATH");
            environment["DOTNET_MULTILEVEL_LOOKUP"] = "0";
            environment["DOTNET_ADD_GLOBAL_TOOLS_TO_PATH"] = "false";
            environment["ProgramFiles"] = Path.Combine(Root, "program-files");
            environment["ProgramFiles(x86)"] = Path.Combine(Root, "program-files-x86");
            environment["NETCOREDBG_PATH"] = Debugger;
            environment.Remove("FLAUI_BRIDGE_PATH");
            if (scenario == "explicit") environment["FLAUI_BRIDGE_PATH"] = ExplicitBridge;
            if (scenario == "invalid") environment["FLAUI_BRIDGE_PATH"] = Path.Combine(Root, "missing-bridge.exe");

            var started = false;
            var exited = false;
            try
            {
                Assert.True(process.Start());
                started = true;
                var stdout = process.StandardOutput.ReadToEndAsync();
                var stderr = process.StandardError.ReadToEndAsync();
                process.StandardInput.Close();
                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(20));
                await process.WaitForExitAsync(deadline.Token);
                exited = true;
                return new SetupResult(process.ExitCode, await stdout.WaitAsync(deadline.Token), await stderr.WaitAsync(deadline.Token));
            }
            finally
            {
                if (started && !exited)
                {
                    _preserve = true;
                    try
                    {
                        if (!process.HasExited) process.Kill();
                        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                        await process.WaitForExitAsync(deadline.Token);
                    }
                    catch (InvalidOperationException) { }
                    catch (OperationCanceledException) { }
                }
            }
        }

        public async ValueTask DisposeAsync()
        {
            if (!_preserve) await ModernMcpScratchDirectory.DeleteAsync(Root);
        }
    }
}

public sealed class WindowsBundledBridgeTheoryAttribute : TheoryAttribute
{
    public WindowsBundledBridgeTheoryAttribute()
    {
        if (!OperatingSystem.IsWindows()) Skip = "Bundled FlaUI bridge discovery is Windows-only.";
    }
}
