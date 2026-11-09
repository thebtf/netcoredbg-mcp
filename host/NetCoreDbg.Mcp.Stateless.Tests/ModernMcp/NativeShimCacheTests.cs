using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Security.AccessControl;
using System.Security.Principal;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class NativeShimCacheTests
{
    private const string FirstVersion = "12345.71.31";
    private const string SecondVersion = "12345.71.32";
    private static readonly byte[] FirstBytes = [1, 4, 7, 10];
    private static readonly byte[] SecondBytes = [2, 5, 8, 11];
    private static readonly byte[] OtherBytes = [3, 6, 9, 12];
    private static string ShimName => OperatingSystem.IsWindows() ? "dbgshim.dll" : "libdbgshim.so";

    [WindowsShimTheory]
    [InlineData("setup")]
    [InlineData("--setup")]
    public async Task Setup_CachesMultipleRuntimes_AndRerunPreservesBytes(string command)
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, FirstBytes);
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, SecondVersion, SecondBytes);

        var first = await sandbox.SetupAsync(command);
        AssertUsableConfig(first, sandbox);
        Assert.Equal(FirstBytes, await File.ReadAllBytesAsync(sandbox.CachePath(FirstVersion)));
        Assert.Equal(SecondBytes, await File.ReadAllBytesAsync(sandbox.CachePath(SecondVersion)));
        var firstWrite = File.GetLastWriteTimeUtc(sandbox.CachePath(FirstVersion));
        var secondWrite = File.GetLastWriteTimeUtc(sandbox.CachePath(SecondVersion));
        var cacheDirectory = Path.GetDirectoryName(sandbox.CachePath(FirstVersion))!;
        Directory.SetLastWriteTimeUtc(cacheDirectory, DateTime.UtcNow.AddMinutes(-2));
        var directoryWrite = Directory.GetLastWriteTimeUtc(cacheDirectory);

        var second = await sandbox.SetupAsync(command);
        AssertUsableConfig(second, sandbox);
        Assert.Equal(FirstBytes, await File.ReadAllBytesAsync(sandbox.CachePath(FirstVersion)));
        Assert.Equal(SecondBytes, await File.ReadAllBytesAsync(sandbox.CachePath(SecondVersion)));
        Assert.Equal(firstWrite, File.GetLastWriteTimeUtc(sandbox.CachePath(FirstVersion)));
        Assert.Equal(secondWrite, File.GetLastWriteTimeUtc(sandbox.CachePath(SecondVersion)));
        Assert.Equal(directoryWrite, Directory.GetLastWriteTimeUtc(cacheDirectory));
    }

    [WindowsShimFact]
    public async Task Cache_RespectsRootPrecedenceAndFallsBackForMissingShim()
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, FirstBytes);
        await sandbox.RuntimeShimAsync(sandbox.PlatformDotnetRoot, FirstVersion, OtherBytes);
        Directory.CreateDirectory(Path.Combine(sandbox.DotnetRoot, "shared", "Microsoft.NETCore.App", SecondVersion));
        await sandbox.RuntimeShimAsync(sandbox.PlatformDotnetRoot, SecondVersion, SecondBytes);

        var result = PopulateForTest(sandbox, 128, sandbox.DotnetRoot, sandbox.PlatformDotnetRoot);

        Assert.Equal(2, result.GetType().GetProperty("Cached")!.GetValue(result));
        Assert.Equal(FirstBytes, await File.ReadAllBytesAsync(sandbox.CachePath(FirstVersion)));
        Assert.Equal(SecondBytes, await File.ReadAllBytesAsync(sandbox.CachePath(SecondVersion)));
    }

    [WindowsShimFact]
    public async Task Cache_InvalidRootWarnsAndContinuesWithLaterRoot()
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, FirstBytes);

        var result = PopulateForTest(sandbox, 128, "invalid\0root", sandbox.DotnetRoot);

        var warnings = Assert.IsAssignableFrom<IReadOnlyList<string>>(result.GetType().GetProperty("Warnings")!.GetValue(result));
        Assert.Contains(warnings, warning => warning.Contains("Could not scan", StringComparison.Ordinal));
        Assert.Equal(FirstBytes, await File.ReadAllBytesAsync(sandbox.CachePath(FirstVersion)));
    }

    [WindowsShimFact]
    public async Task Cache_UnreadableRootWarnsAndContinuesWithLaterRoot()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var sandbox = await Sandbox.CreateAsync();
        var shared = Path.Combine(sandbox.DotnetRoot, "shared", "Microsoft.NETCore.App");
        Directory.CreateDirectory(shared);
        await sandbox.RuntimeShimAsync(sandbox.PlatformDotnetRoot, FirstVersion, FirstBytes);
        var directory = new DirectoryInfo(shared);
        var security = directory.GetAccessControl();
        var deny = new FileSystemAccessRule(new SecurityIdentifier(WellKnownSidType.WorldSid, null),
            FileSystemRights.ListDirectory, InheritanceFlags.None, PropagationFlags.None, AccessControlType.Deny);
        security.AddAccessRule(deny);
        var restricted = false;
        try
        {
            directory.SetAccessControl(security);
            restricted = true;
            Assert.Throws<UnauthorizedAccessException>(() => Directory.EnumerateDirectories(shared).ToArray());
            var result = PopulateForTest(sandbox, 128, sandbox.DotnetRoot, sandbox.PlatformDotnetRoot);
            var warnings = Assert.IsAssignableFrom<IReadOnlyList<string>>(result.GetType().GetProperty("Warnings")!.GetValue(result));
            Assert.Contains(warnings, warning => warning.Contains("Could not scan", StringComparison.Ordinal));
            Assert.Equal(FirstBytes, await File.ReadAllBytesAsync(sandbox.CachePath(FirstVersion)));
        }
        finally
        {
            if (restricted)
            {
                var restored = directory.GetAccessControl();
                restored.RemoveAccessRuleSpecific(deny);
                directory.SetAccessControl(restored);
            }
        }
    }

    [WindowsShimFact]
    public async Task Cache_WriteBudgetStopsAdditionalVersionsWithoutOverwriting()
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, FirstBytes);
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, SecondVersion, SecondBytes);
        var result = PopulateForTest(sandbox, 4, sandbox.DotnetRoot);

        Assert.Equal(1, result.GetType().GetProperty("Cached")!.GetValue(result));
        var warnings = Assert.IsAssignableFrom<IReadOnlyList<string>>(result.GetType().GetProperty("Warnings")!.GetValue(result));
        Assert.Contains(warnings, warning => warning.Contains("budget", StringComparison.OrdinalIgnoreCase));
        var cacheRoot = Path.Combine(sandbox.Home, ".netcoredbg-mcp", "dbgshim");
        Assert.Single(Directory.GetFiles(cacheRoot, ShimName, SearchOption.AllDirectories));
    }

    [WindowsShimFact]
    public async Task Cache_OversizedShimDoesNotExhaustReadBudgetForLaterRoot()
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, [1, 2, 3, 4, 5]);
        await sandbox.RuntimeShimAsync(sandbox.PlatformDotnetRoot, SecondVersion, SecondBytes);
        var assembly = Assembly.LoadFrom(TestOutputPathResolver.ResolveManagedAssembly(RepositoryLayout.Root,
            Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));
        var type = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeDbgshimCache", throwOnError: true)!;
        var method = type.GetMethod("Populate", BindingFlags.Static | BindingFlags.NonPublic, null,
            [typeof(string), typeof(string[]), typeof(long), typeof(long), typeof(int)], null)!;

        var result = method.Invoke(null, [sandbox.Home, new[] { sandbox.DotnetRoot, sandbox.PlatformDotnetRoot }, 100L, 6L, 4])!;

        Assert.Equal(1, result.GetType().GetProperty("Cached")!.GetValue(result));
        Assert.False(File.Exists(sandbox.CachePath(FirstVersion)));
        Assert.Equal(SecondBytes, await File.ReadAllBytesAsync(sandbox.CachePath(SecondVersion)));
    }

    [WindowsShimFact]
    public async Task Setup_ConflictingCache_PreservesExistingBytesAndWarnsWithoutBreakingJson()
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, FirstBytes);
        var cached = sandbox.CachePath(FirstVersion);
        Directory.CreateDirectory(Path.GetDirectoryName(cached)!);
        await File.WriteAllBytesAsync(cached, OtherBytes);

        var result = await sandbox.SetupAsync("--setup");
        AssertUsableConfig(result, sandbox);
        Assert.Equal(OtherBytes, await File.ReadAllBytesAsync(cached));
        Assert.Contains("warning", result.Error, StringComparison.OrdinalIgnoreCase);
        Assert.Contains(FirstVersion, result.Error, StringComparison.Ordinal);
        Assert.Contains("dbgshim", result.Error, StringComparison.OrdinalIgnoreCase);
    }

    [UnixShimFact]
    public async Task Setup_UnixReportsUnavailableWithoutCaching()
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, FirstBytes);

        var result = await sandbox.SetupAsync("--setup");

        AssertUsableConfig(result, sandbox);
        Assert.Contains("dbgshim caching is unavailable", result.Error, StringComparison.OrdinalIgnoreCase);
        Assert.False(File.Exists(sandbox.CachePath(FirstVersion)));
    }

    [Theory]
    [InlineData("debugger")]
    [InlineData("bridge")]
    public async Task Setup_InvalidExplicitPath_DoesNotMutateCache(string invalid)
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, FirstBytes);
        var cached = sandbox.CachePath(SecondVersion);
        Directory.CreateDirectory(Path.GetDirectoryName(cached)!);
        await File.WriteAllBytesAsync(cached, OtherBytes);

        var result = await sandbox.SetupAsync("--setup", invalid);
        Assert.NotEqual(0, result.ExitCode);
        Assert.Empty(result.Output);
        Assert.Contains(invalid == "debugger" ? "NETCOREDBG_PATH" : "FLAUI_BRIDGE_PATH", result.Error, StringComparison.Ordinal);
        Assert.Equal(OtherBytes, await File.ReadAllBytesAsync(cached));
        Assert.False(File.Exists(sandbox.CachePath(FirstVersion)));
        Assert.Single(Directory.GetDirectories(Path.GetDirectoryName(Path.GetDirectoryName(cached)!)!));
    }

    [WindowsShimFact]
    public async Task Setup_CachedShim_IsDiscoverableByCompatibilityInspectionWithoutDebuggerMutation()
    {
        await using var sandbox = await Sandbox.CreateAsync();
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, FirstVersion, FirstBytes);
        await sandbox.RuntimeShimAsync(sandbox.DotnetRoot, SecondVersion, SecondBytes);
        AssertUsableConfig(await sandbox.SetupAsync("--setup"), sandbox);
        var active = Path.Combine(Path.GetDirectoryName(sandbox.Debugger)!, ShimName);
        await File.WriteAllBytesAsync(active, OtherBytes);
        var program = Path.Combine(sandbox.Root, "target.dll");
        await File.WriteAllBytesAsync(program, []);
        await File.WriteAllTextAsync(Path.ChangeExtension(program, ".runtimeconfig.json"),
            "{\"runtimeOptions\":{\"framework\":{\"name\":\"Microsoft.NETCore.App\",\"version\":\"12345.0.0\"}}}");

        var payload = await sandbox.InspectAsync(program);
        var data = Assert.IsType<JsonObject>(payload["data"]);
        Assert.Equal("known", data["cachedCandidate"]?["status"]?.GetValue<string>());
        Assert.Equal(SecondVersion, data["cachedCandidate"]?["version"]?.GetValue<string>());
        Assert.Equal(sandbox.CachePath(SecondVersion), data["cachedCandidate"]?["path"]?.GetValue<string>());
        Assert.False(data["willMutateSharedDebugger"]?.GetValue<bool>() ?? true);
        Assert.False(data["mutationPerformed"]?.GetValue<bool>() ?? true);
        Assert.Equal(OtherBytes, await File.ReadAllBytesAsync(active));
        Assert.Equal(SecondBytes, await File.ReadAllBytesAsync(sandbox.CachePath(SecondVersion)));
    }

    private static object PopulateForTest(Sandbox sandbox, long budget, params string[] roots)
    {
        var assembly = Assembly.LoadFrom(TestOutputPathResolver.ResolveManagedAssembly(RepositoryLayout.Root,
            Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));
        var type = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeDbgshimCache", throwOnError: true)!;
        var method = type.GetMethod("Populate", BindingFlags.Static | BindingFlags.NonPublic, null,
            [typeof(string), typeof(string[]), typeof(long)], null)!;
        return method.Invoke(null, [sandbox.Home, roots, budget])!;
    }

    private static void AssertUsableConfig(SetupResult result, Sandbox sandbox)
    {
        Assert.True(result.ExitCode == 0, result.Error);
        using var json = JsonDocument.Parse(result.Output);
        var config = json.RootElement.GetProperty("mcpServers").GetProperty("netcoredbg");
        Assert.False(string.IsNullOrWhiteSpace(config.GetProperty("command").GetString()));
        var args = config.GetProperty("args").EnumerateArray().Select(static value => value.GetString()).ToArray();
        Assert.Contains("--legacy-compat", args);
        Assert.Contains("--project-from-cwd", args);
        Assert.Equal(sandbox.Debugger, config.GetProperty("env").GetProperty("NETCOREDBG_PATH").GetString());
        Assert.Equal(sandbox.Bridge, config.GetProperty("env").GetProperty("FLAUI_BRIDGE_PATH").GetString());
    }

    private sealed record SetupResult(int ExitCode, string Output, string Error);

    private sealed class Sandbox : IAsyncDisposable
    {
        private bool _preserve;
        private readonly string _hostAssembly;
        private readonly string _dotnet;
        internal string Root { get; }
        internal string Home => Path.Combine(Root, "home");
        internal string DotnetRoot => Path.Combine(Root, "runtime");
        internal string PlatformDotnetRoot => OperatingSystem.IsWindows()
            ? Path.Combine(Root, "program-files", "dotnet") : Path.Combine(Home, ".dotnet");
        internal string Debugger => Path.Combine(Root, "debugger", OperatingSystem.IsWindows() ? "netcoredbg.exe" : "netcoredbg");
        internal string Bridge => Path.Combine(Root, "bridge", "FlaUIBridge.exe");

        private Sandbox(string root)
        {
            Root = root;
            _hostAssembly = TestOutputPathResolver.ResolveManagedAssembly(RepositoryLayout.Root,
                Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");
            var runtimeDirectory = new DirectoryInfo(RuntimeEnvironment.GetRuntimeDirectory());
            var dotnetRoot = runtimeDirectory.Parent?.Parent?.Parent?.FullName
                ?? throw new InvalidOperationException("Could not locate the executing dotnet host.");
            _dotnet = Path.Combine(dotnetRoot, OperatingSystem.IsWindows() ? "dotnet.exe" : "dotnet");
            if (!File.Exists(_dotnet)) throw new FileNotFoundException("The executing runtime's dotnet host is required.", _dotnet);
        }

        internal static async Task<Sandbox> CreateAsync()
        {
            var sandbox = new Sandbox(ModernMcpScratchDirectory.Create());
            Directory.CreateDirectory(sandbox.Home);
            Directory.CreateDirectory(sandbox.DotnetRoot);
            Directory.CreateDirectory(Path.Combine(sandbox.Root, "path"));
            Directory.CreateDirectory(Path.GetDirectoryName(sandbox.Debugger)!);
            Directory.CreateDirectory(Path.GetDirectoryName(sandbox.Bridge)!);
            await File.WriteAllTextAsync(sandbox.Debugger, "controlled debugger placeholder");
            await File.WriteAllTextAsync(sandbox.Bridge, "controlled bridge placeholder");
            if (!OperatingSystem.IsWindows())
                File.SetUnixFileMode(sandbox.Debugger, UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute);
            return sandbox;
        }

        internal string CachePath(string version) => Path.Combine(Home, ".netcoredbg-mcp", "dbgshim", version, ShimName);

        internal async Task RuntimeShimAsync(string root, string version, byte[] bytes)
        {
            var path = Path.Combine(root, "shared", "Microsoft.NETCore.App", version, ShimName);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            await File.WriteAllBytesAsync(path, bytes);
        }

        private Dictionary<string, string?> EnvironmentValues(string? invalid = null) => new()
        {
            ["HOME"] = Home,
            ["USERPROFILE"] = Home,
            ["HOMEDRIVE"] = null,
            ["HOMEPATH"] = null,
            ["PATH"] = Path.Combine(Root, "path"),
            ["DOTNET_ROOT"] = DotnetRoot,
            ["DOTNET_ROOT_X64"] = null,
            ["DOTNET_ROOT_X86"] = null,
            ["DOTNET_ROOT_ARM64"] = null,
            ["DOTNET_HOST_PATH"] = null,
            ["DOTNET_MULTILEVEL_LOOKUP"] = "0",
            ["ProgramFiles"] = Path.Combine(Root, "program-files"),
            ["ProgramFiles(x86)"] = Path.Combine(Root, "program-files-x86"),
            ["NETCOREDBG_PATH"] = invalid == "debugger" ? Path.Combine(Root, "missing-debugger") : Debugger,
            ["FLAUI_BRIDGE_PATH"] = invalid == "bridge" ? Path.Combine(Root, "missing-bridge.exe") : Bridge,
        };

        internal async Task<SetupResult> SetupAsync(string command, string? invalid = null)
        {
            using var process = new Process { StartInfo = new ProcessStartInfo(_dotnet)
            {
                WorkingDirectory = Root, UseShellExecute = false, RedirectStandardInput = true,
                RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
            } };
            process.StartInfo.ArgumentList.Add(_hostAssembly);
            process.StartInfo.ArgumentList.Add("--legacy-compat");
            process.StartInfo.ArgumentList.Add(command);
            foreach (var (key, value) in EnvironmentValues(invalid))
            {
                if (value is null) process.StartInfo.Environment.Remove(key);
                else process.StartInfo.Environment[key] = value;
            }
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
                    if (!process.HasExited) process.Kill();
                    using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                    await process.WaitForExitAsync(deadline.Token);
                }
            }
        }

        internal async Task<JsonObject> InspectAsync(string program)
        {
            var environment = EnvironmentValues();
            environment["DOTNET_ROOT"] = Path.GetDirectoryName(_dotnet);
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                StartupDirectory: Root, InheritEnvironmentVariables: false, AdditionalEnvironment: environment));
            var response = await driver.CallToolRawAsync("inspect_debug_launch_compatibility",
                new JsonObject { ["program"] = program }, new JsonObject { ["muxSessionId"] = "shim-cache-inspection" },
                new RequestId("shim-cache-inspection"), timeout: TimeSpan.FromSeconds(20));
            return Assert.IsType<JsonObject>(ModernMcpProcessDriver.LegacyPayload(ModernMcpProcessDriver.RequireResult(response)));
        }

        public async ValueTask DisposeAsync()
        {
            if (!_preserve) await ModernMcpScratchDirectory.DeleteAsync(Root);
        }
    }
}

public sealed class WindowsShimFactAttribute : FactAttribute
{
    public WindowsShimFactAttribute() { if (!OperatingSystem.IsWindows()) Skip = "Windows shim caching only; Unix remains unimplemented."; }
}

public sealed class WindowsShimTheoryAttribute : TheoryAttribute
{
    public WindowsShimTheoryAttribute() { if (!OperatingSystem.IsWindows()) Skip = "Windows shim caching only; Unix remains unimplemented."; }
}

public sealed class UnixShimFactAttribute : FactAttribute
{
    public UnixShimFactAttribute() { if (OperatingSystem.IsWindows()) Skip = "Unix-only unsupported-cache contract."; }
}
