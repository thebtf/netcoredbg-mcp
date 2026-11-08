using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class LegacyDebuggerDiscoveryTests
{
    [Theory]
    [InlineData("managed-direct")]
    [InlineData("managed-nested")]
    [InlineData("path")]
    [InlineData("explicit")]
    [InlineData("homedrive")]
    public async Task LegacyStart_FindsAndRunsThePreferredInstalledDebugger(string selection)
    {
        if (selection == "homedrive" && !OperatingSystem.IsWindows()) return;
        var scratch = ModernMcpScratchDirectory.Create();
        var startAttempted = false;
        var cleanupConfirmed = false;
        try
        {
            var home = Path.Combine(scratch, "home");
            var managed = Path.Combine(home, ".netcoredbg-mcp", "netcoredbg");
            var nested = Path.Combine(managed, "nested");
            var pathDirectory = Path.Combine(scratch, "path");
            var explicitDirectory = Path.Combine(scratch, "explicit");
            Directory.CreateDirectory(home);
            CopyAdapter(pathDirectory);
            if (selection is "managed-direct" or "managed-nested" or "explicit") CopyAdapter(nested);
            if (selection is "managed-direct" or "explicit" or "homedrive") CopyAdapter(managed);
            if (selection == "explicit") CopyAdapter(explicitDirectory);
            var expected = selection switch
            {
                "managed-direct" => managed,
                "homedrive" => managed,
                "managed-nested" => nested,
                "path" => pathDirectory,
                "explicit" => explicitDirectory,
                _ => throw new InvalidOperationException("Unknown selection."),
            };
            var executableName = OperatingSystem.IsWindows() ? "netcoredbg.exe" : "netcoredbg";
            var shimName = OperatingSystem.IsWindows() ? "dbgshim.dll" : "libdbgshim.so";
            var drive = selection == "homedrive" ? Path.GetPathRoot(home)!.TrimEnd(Path.DirectorySeparatorChar) : null;
            var environment = new Dictionary<string, string?>
            {
                ["HOME"] = home,
                ["USERPROFILE"] = selection == "homedrive" ? null : home,
                ["HOMEDRIVE"] = drive,
                ["HOMEPATH"] = drive is null ? null : home[drive.Length..],
                ["PATH"] = pathDirectory,
                ["NETCOREDBG_PATH"] = selection == "explicit" ? Path.Combine(explicitDirectory, executableName) : null,
            };

            await using (var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                AdditionalEnvironment: environment, InheritEnvironmentVariables: false)))
            {
                var owner = new JsonObject { ["muxSessionId"] = "debugger-discovery-test" };
                var inspection = await CallAsync(driver, "inspect_debug_launch_compatibility",
                    new JsonObject { ["program"] = driver.InertProgramPath }, owner);
                Assert.Equal(Path.Combine(expected, shimName), inspection["data"]?["activeDbgshim"]?["path"]?.GetValue<string>());

                startAttempted = true;
                var started = await CallAsync(driver, "start_debug", new JsonObject
                {
                    ["program"] = driver.InertProgramPath,
                    ["pre_build"] = false,
                    ["stop_at_entry"] = false,
                }, owner);
                Assert.True(started["data"]?["success"]?.GetValue<bool>(), started.ToJsonString());
                Assert.Contains(await driver.ReadNativeActionsAsync(), static action => action.Command == "launch");
                var stopped = await CallAsync(driver, "stop_debug", new JsonObject(), owner);
                Assert.True(stopped["data"]?["success"]?.GetValue<bool>(), stopped.ToJsonString());
            }
            cleanupConfirmed = true;
        }
        finally
        {
            if (!startAttempted || cleanupConfirmed) await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    [Fact]
    public async Task LegacyInspection_DoesNotReplaceAnExplicitMissingDebuggerWithPathCandidate()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var home = Path.Combine(scratch, "home");
            var pathDirectory = Path.Combine(scratch, "path");
            var explicitPath = Path.Combine(scratch, "missing", OperatingSystem.IsWindows() ? "netcoredbg.exe" : "netcoredbg");
            Directory.CreateDirectory(home);
            CopyAdapter(pathDirectory);
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                InheritEnvironmentVariables: false, AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["HOME"] = home,
                    ["USERPROFILE"] = home,
                    ["PATH"] = pathDirectory,
                    ["NETCOREDBG_PATH"] = explicitPath,
                }));
            var inspection = await CallAsync(driver, "inspect_debug_launch_compatibility",
                new JsonObject { ["program"] = driver.InertProgramPath }, new JsonObject());
            var shimName = OperatingSystem.IsWindows() ? "dbgshim.dll" : "libdbgshim.so";
            Assert.Equal(Path.Combine(Path.GetDirectoryName(explicitPath)!, shimName),
                inspection["data"]?["activeDbgshim"]?["path"]?.GetValue<string>());
            Assert.Empty(await driver.ReadNativeActionsAsync());
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task LegacyInspection_UsesHomeDriveAndHomePathWhenUserProfileIsAbsent()
    {
        if (!OperatingSystem.IsWindows()) return;
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var home = Path.Combine(scratch, "profile");
            var managed = Path.Combine(home, ".netcoredbg-mcp", "netcoredbg");
            var pathDirectory = Path.Combine(scratch, "path");
            Directory.CreateDirectory(managed);
            Directory.CreateDirectory(pathDirectory);
            await File.WriteAllBytesAsync(Path.Combine(managed, "netcoredbg.exe"), []);
            var drive = Path.GetPathRoot(home)!.TrimEnd(Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar);
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                InheritEnvironmentVariables: false, AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["USERPROFILE"] = null,
                    ["HOMEDRIVE"] = drive,
                    ["HOMEPATH"] = home[drive.Length..],
                    ["PATH"] = pathDirectory,
                    ["NETCOREDBG_PATH"] = null,
                }));
            var inspection = await CallAsync(driver, "inspect_debug_launch_compatibility",
                new JsonObject { ["program"] = driver.InertProgramPath }, new JsonObject());
            Assert.Equal(Path.Combine(managed, "dbgshim.dll"), inspection["data"]?["activeDbgshim"]?["path"]?.GetValue<string>());
            Assert.Empty(await driver.ReadNativeActionsAsync());
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    private static async Task<JsonObject> CallAsync(ModernMcpProcessDriver driver, string name, JsonObject arguments, JsonObject owner)
    {
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(name, arguments, owner,
            new RequestId($"discovery-{Guid.NewGuid():N}"), timeout: TimeSpan.FromSeconds(15)));
        return Assert.IsType<JsonObject>(ModernMcpProcessDriver.LegacyPayload(result));
    }

    private static void CopyAdapter(string destination)
    {
        var adapter = TestOutputPathResolver.ResolveProcess(RepositoryLayout.ControlledAdapterDirectory, "ControlledDapAdapter");
        Assert.Empty(adapter.Arguments);
        Directory.CreateDirectory(destination);
        var sourceDirectory = Path.GetDirectoryName(adapter.Command)!;
        foreach (var file in new[] { "ControlledDapAdapter.dll", "ControlledDapAdapter.deps.json", "ControlledDapAdapter.runtimeconfig.json" })
            File.Copy(Path.Combine(sourceDirectory, file), Path.Combine(destination, file));
        var executable = Path.Combine(destination, OperatingSystem.IsWindows() ? "netcoredbg.exe" : "netcoredbg");
        File.Copy(adapter.Command, executable);
        if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(executable, File.GetUnixFileMode(adapter.Command));
    }
}
