using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class DebuggerLaunchContractTests
{
    [Fact]
    public async Task StartDebug_WindowsDebuggerBasenameWithoutExtension_RefusesAlternatePathTarget()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var adapter = TestOutputPathResolver.ResolveProcess(
            RepositoryLayout.ControlledAdapterDirectory, "ControlledDapAdapter");
        var candidate = TestOutputPathResolver.ResolveProcess(
            Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");
        Assert.Empty(adapter.Arguments);
        Assert.Empty(candidate.Arguments);
        Assert.True(Path.IsPathFullyQualified(adapter.Command));
        Assert.True(Path.IsPathFullyQualified(candidate.Command));
        var adapterDirectory = Path.GetDirectoryName(adapter.Command)!;
        var basename = Path.GetFileNameWithoutExtension(adapter.Command);
        Assert.Equal(".exe", Path.GetExtension(adapter.Command));
        Assert.False(File.Exists(Path.Combine(adapterDirectory, basename)));
        foreach (var directory in new[] { RepositoryLayout.Root, Path.GetDirectoryName(candidate.Command)! })
        {
            Assert.False(File.Exists(Path.Combine(directory, basename)));
            Assert.False(File.Exists(Path.Combine(directory, basename + ".exe")));
        }

        // Both children see only the fixture directory on PATH; the test process environment stays unchanged.
        await using (var control = await ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["PATH"] = adapterDirectory,
                ["NETCOREDBG_PATH"] = adapter.Command,
            })))
        {
            var started = ModernMcpProcessDriver.RequireResult(await control.CallToolRawAsync(
                "start_debug",
                new JsonObject { ["program"] = control.InertProgramPath },
                ModernMcpProcessDriver.CurrentMeta(),
                new RequestId("absolute-debugger-control")));
            Assert.Equal("complete", started["resultType"]?.GetValue<string>());
            Assert.False(started["isError"]?.GetValue<bool>() ?? true, started.ToJsonString());
            var session = Assert.IsType<JsonObject>(started["structuredContent"]);
            Assert.Equal("start_debug_success", session["kind"]?.GetValue<string>());
            var debugSessionId = Assert.IsType<string>(session["debugSessionId"]?.GetValue<string>());
            var actions = await control.ReadNativeActionsAsync();
            Assert.Single(actions, static action => action.Kind == "startup");
            Assert.Contains(actions, static action => action.Command == "initialize");
            Assert.Contains(actions, static action => action.Command == "launch");

            var stopped = ModernMcpProcessDriver.RequireResult(await control.CallToolRawAsync(
                "stop_debug",
                new JsonObject { ["debugSessionId"] = debugSessionId },
                ModernMcpProcessDriver.CurrentMeta(),
                new RequestId("absolute-debugger-control-stop")));
            Assert.False(stopped["isError"]?.GetValue<bool>() ?? true, stopped.ToJsonString());
            Assert.Equal("stop_debug_success", stopped["structuredContent"]?["kind"]?.GetValue<string>());
        }

        // SearchPath adds .exe and searches PATH; non-null CreateProcess applicationName must do neither.
        await using var driver = await ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["PATH"] = adapterDirectory,
                ["NETCOREDBG_PATH"] = basename,
            }));
        var refused = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("path-only-debugger-refused")));

        Assert.Equal("complete", refused["resultType"]?.GetValue<string>());
        Assert.True(refused["isError"]?.GetValue<bool>() ?? false,
            "A debugger basename without an extension must not launch the alternate PATH adapter: " + refused.ToJsonString());
        var error = Assert.IsType<JsonObject>(refused["structuredContent"]);
        Assert.Equal("debug_session_not_found", error["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", error["error"]?.GetValue<string>());
        Assert.False(error.ContainsKey("debugSessionId"));
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }
}
