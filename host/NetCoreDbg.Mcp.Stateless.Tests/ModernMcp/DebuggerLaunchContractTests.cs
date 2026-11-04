using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class DebuggerLaunchContractTests
{
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task StartDebug_NonexistentWorkingDirectory_RejectsBeforeAdapterStarts(bool existingFile)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var missingDirectory = existingFile ? driver.InertProgramPath : driver.InertProgramPath + ".missing-directory";
        Assert.False(Directory.Exists(missingDirectory));

        var response = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["cwd"] = missingDirectory,
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-missing-cwd")));

        Assert.True(response["isError"]?.GetValue<bool>() ?? false, "A missing working directory must not start the adapter.");
        var content = Assert.IsType<JsonObject>(response["structuredContent"]);
        Assert.Equal("start_debug_cwd_unavailable", content["kind"]?.GetValue<string>());
        Assert.Equal("START_DEBUG_CWD_UNAVAILABLE", content["error"]?.GetValue<string>());
        Assert.False(content.ContainsKey("debugSessionId"));
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }

    [Fact]
    public async Task StartDebug_AdapterRefusesLaunch_ReturnsErrorWithoutSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = new JsonObject { ["launchRefused"] = true }.ToJsonString(),
                ["CONTROLLED_DAP_HOLD_LAUNCH_RESPONSE"] = "1",
            }), startupTimeout: TimeSpan.FromSeconds(10));

        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        var request = driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["cwd"] = RepositoryLayout.Root,
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-refused"), timeout: TimeSpan.FromSeconds(20));
        try
        {
            await driver.WaitForFixtureRecordAsync("launch-response-gated", observation.Token);
            var startup = Assert.Single(await driver.ReadFixtureTranscriptAsync(observation.Token), static entry => entry.Kind == "startup");
            using var adapter = System.Diagnostics.Process.GetProcessById(Assert.IsType<int>(startup.ProcessId));
            Assert.Equal(startup.ProcessStartTimeUtcTicks, adapter.StartTime.ToUniversalTime().Ticks);
            Assert.False(adapter.HasExited);
            driver.ReleaseThreadsResponse();

            var response = ModernMcpProcessDriver.RequireResult(await request);
            Assert.True(response["isError"]?.GetValue<bool>() ?? false, "An adapter refusal must not return a successful session.");
            var content = Assert.IsType<JsonObject>(response["structuredContent"]);
            Assert.Equal("dap_launch_refused", content["kind"]?.GetValue<string>());
            Assert.Equal("DAP_LAUNCH_REFUSED", content["error"]?.GetValue<string>());
            Assert.False(content.ContainsKey("debugSessionId"));
            StructuredContentSchemaParityTests.AssertMatchesSchema(content);
            await adapter.WaitForExitAsync(observation.Token);
            Assert.True(adapter.HasExited);
            Assert.Contains(await driver.ReadNativeActionsAsync(), static action => action.Command == "configurationDone");
        }
        finally
        {
            driver.ReleaseThreadsResponse();
            _ = await Record.ExceptionAsync(() => request);
        }
    }

    [Fact]
    public async Task StartDebug_InvalidLaunchOptions_RejectBeforeAdapterStarts()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var cases = new[]
        {
            new JsonObject { ["args"] = "not-an-array" },
            new JsonObject { ["args"] = new JsonArray("ok", true) },
            new JsonObject { ["args"] = new JsonArray((JsonNode?)null) },
            new JsonObject { ["cwd"] = 12 },
            new JsonObject { ["stopAtEntry"] = "true" },
        };
        for (var index = 0; index < cases.Length; index++)
        {
            cases[index]["program"] = driver.InertProgramPath;
            var response = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(
                "start_debug", cases[index], ModernMcpProcessDriver.CurrentMeta(), new RequestId($"launch-invalid-{index}")));
            Assert.True(response["isError"]?.GetValue<bool>() ?? false);
            Assert.Equal("INVALID_TOOL_ARGUMENTS", response["structuredContent"]?["error"]?.GetValue<string>());
        }
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }

    [Fact]
    public async Task StartDebug_NullLaunchOptions_PreserveExistingAdapterDefaults()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var started = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["args"] = null,
            ["cwd"] = null,
            ["stopAtEntry"] = null,
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-options-null")));
        Assert.False(started["isError"]?.GetValue<bool>() ?? true, "Null launch options must retain the existing launch behavior.");
        var session = Assert.IsType<string>(started["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        var launch = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "launch");
        using var document = System.Text.Json.JsonDocument.Parse(launch.Detail!);
        var arguments = document.RootElement.GetProperty("arguments");
        Assert.False(arguments.TryGetProperty("args", out _));
        Assert.False(arguments.TryGetProperty("cwd", out _));
        Assert.False(arguments.TryGetProperty("stopAtEntry", out _));
        await driver.StopSingleControlledSessionAsync(session);
    }

    [Theory]
    [InlineData("")]
    [InlineData(".")]
    public async Task StartDebug_EmptyArgumentsAndFalseEntry_PreserveExplicitValues(string cwd)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var started = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["args"] = new JsonArray(),
            ["cwd"] = cwd,
            ["stopAtEntry"] = false,
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-options-empty")));
        Assert.False(started["isError"]?.GetValue<bool>() ?? true);
        var session = Assert.IsType<string>(started["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        var launch = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "launch");
        using var document = System.Text.Json.JsonDocument.Parse(launch.Detail!);
        var arguments = document.RootElement.GetProperty("arguments");
        Assert.Empty(arguments.GetProperty("args").EnumerateArray());
        Assert.False(arguments.GetProperty("stopAtEntry").GetBoolean());
        if (cwd.Length == 0) Assert.False(arguments.TryGetProperty("cwd", out _));
        else Assert.Equal(cwd, arguments.GetProperty("cwd").GetString());
        await driver.StopSingleControlledSessionAsync(session);
    }

    [Fact]
    public async Task StartDebug_ExplicitArgumentsWorkingDirectoryAndStopAtEntry_ReachAdapterUnchanged()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var started = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["args"] = new JsonArray("first argument", "", "Привет 😀"),
            ["cwd"] = RepositoryLayout.Root,
            ["stopAtEntry"] = true,
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-options")));

        Assert.False(started["isError"]?.GetValue<bool>() ?? true, "Explicit launch options must reach the adapter.");
        var session = Assert.IsType<string>(started["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        var launch = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "launch");
        using var document = System.Text.Json.JsonDocument.Parse(launch.Detail!);
        var arguments = document.RootElement.GetProperty("arguments");
        Assert.Equal(RepositoryLayout.Root, arguments.GetProperty("cwd").GetString());
        Assert.True(arguments.GetProperty("stopAtEntry").GetBoolean());
        Assert.Equal(new[] { "first argument", "", "Привет 😀" }, arguments.GetProperty("args").EnumerateArray().Select(static value => value.GetString()));
        await driver.StopSingleControlledSessionAsync(session);
    }

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
            await Assert.ThrowsAsync<InvalidOperationException>(() =>
                control.ConfirmMissingWindowsDebuggerDidNotLaunchAsync(new RequestId("absolute-debugger-control")));

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
        await Assert.ThrowsAsync<InvalidOperationException>(() =>
            driver.ConfirmMissingWindowsDebuggerDidNotLaunchAsync(new RequestId("wrong-request")));
        await driver.ConfirmMissingWindowsDebuggerDidNotLaunchAsync(new RequestId("path-only-debugger-refused"));
    }
}
