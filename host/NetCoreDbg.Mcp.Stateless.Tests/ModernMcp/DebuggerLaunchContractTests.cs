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
    public async Task StartDebug_EnvironmentRemoval_IsolatedToOwnedAdapterAndSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true),
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["CONTROLLED_DAP_INSPECT_ENV"] = "1",
                ["NCD_LAUNCH_TEST_REMOVE"] = "parent-remove",
                ["NCD_LAUNCH_TEST_INHERITED"] = "parent-keep",
                ["NCD_LAUNCH_TEST_VALUE"] = "parent-old",
            }), startupTimeout: TimeSpan.FromSeconds(10));
        var removeName = OperatingSystem.IsWindows() ? "ncd_launch_test_remove" : "NCD_LAUNCH_TEST_REMOVE";
        var first = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["env"] = new JsonObject { [removeName] = null, ["NCD_LAUNCH_TEST_VALUE"] = "child-new", ["NCD_LAUNCH_TEST_NEW"] = "" },
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-env-first")));
        Assert.False(first["isError"]?.GetValue<bool>() ?? true, "Valid environment overrides must start a session.");
        var firstId = Assert.IsType<string>(first["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        var second = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-env-second")));
        Assert.False(second["isError"]?.GetValue<bool>() ?? true);
        var secondId = Assert.IsType<string>(second["structuredContent"]?["debugSessionId"]?.GetValue<string>());

        var observations = (await driver.ReadFixtureTranscriptAsync()).Where(static entry => entry.Kind == "environment-observation").ToArray();
        Assert.Equal(2, observations.Length);
        using var isolated = System.Text.Json.JsonDocument.Parse(observations[0].Arguments!);
        using var inherited = System.Text.Json.JsonDocument.Parse(observations[1].Arguments!);
        Assert.Equal(System.Text.Json.JsonValueKind.Null, isolated.RootElement.GetProperty("removed").ValueKind);
        Assert.Equal("parent-keep", isolated.RootElement.GetProperty("unchanged").GetString());
        Assert.Equal("parent-old", isolated.RootElement.GetProperty("overridden").GetString());
        Assert.Equal(System.Text.Json.JsonValueKind.Null, isolated.RootElement.GetProperty("added").ValueKind);
        Assert.Equal("parent-remove", inherited.RootElement.GetProperty("removed").GetString());
        Assert.Equal("parent-old", inherited.RootElement.GetProperty("overridden").GetString());
        var launches = (await driver.ReadNativeActionsAsync()).Where(static action => action.Command == "launch").ToArray();
        Assert.Equal(2, launches.Length);
        using var firstLaunch = System.Text.Json.JsonDocument.Parse(launches[0].Detail!);
        var overrides = firstLaunch.RootElement.GetProperty("arguments").GetProperty("env");
        Assert.Equal("child-new", overrides.GetProperty("NCD_LAUNCH_TEST_VALUE").GetString());
        Assert.Equal("", overrides.GetProperty("NCD_LAUNCH_TEST_NEW").GetString());
        Assert.False(overrides.TryGetProperty(removeName, out _));
        var descendants = (await driver.ReadFixtureTranscriptAsync()).Where(static entry => entry.Kind == "descendant").ToArray();
        Assert.Equal(2, descendants.Length);
        using var isolatedChild = await ReadDescendantEnvironmentAsync(Assert.IsType<int>(descendants[0].ProcessId));
        using var inheritedChild = await ReadDescendantEnvironmentAsync(Assert.IsType<int>(descendants[1].ProcessId));
        Assert.Equal(System.Text.Json.JsonValueKind.Null, isolatedChild.RootElement.GetProperty("removed").ValueKind);
        Assert.Equal("parent-keep", isolatedChild.RootElement.GetProperty("unchanged").GetString());
        Assert.Equal("child-new", isolatedChild.RootElement.GetProperty("overridden").GetString());
        Assert.Equal("", isolatedChild.RootElement.GetProperty("added").GetString());
        Assert.Equal("parent-remove", inheritedChild.RootElement.GetProperty("removed").GetString());
        Assert.Equal("parent-old", inheritedChild.RootElement.GetProperty("overridden").GetString());
        Assert.Equal(System.Text.Json.JsonValueKind.Null, inheritedChild.RootElement.GetProperty("added").ValueKind);
        foreach (var id in new[] { firstId, secondId })
        {
            var stopped = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("stop_debug",
                new JsonObject { ["debugSessionId"] = id }, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"launch-env-stop-{(id == firstId ? 1 : 2)}")));
            Assert.False(stopped["isError"]?.GetValue<bool>() ?? true);
        }
    }
    [Fact]
    public async Task StartDebug_WindowsInheritedAliases_AreSynchronizedInDebuggee()
    {
        if (!OperatingSystem.IsWindows()) return;
        var windowsRoot = Assert.IsType<string>(Environment.GetEnvironmentVariable("SYSTEMROOT"));
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true),
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["CONTROLLED_DAP_INSPECT_ENV"] = "1",
                ["WINDIR"] = windowsRoot.ToUpperInvariant(),
                ["SYSTEMROOT"] = windowsRoot.ToLowerInvariant(),
            }), startupTimeout: TimeSpan.FromSeconds(10));
        var response = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["env"] = new JsonObject { ["NCD_LAUNCH_TEST_VALUE"] = "child-new" },
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-env-aliases")));
        Assert.False(response["isError"]?.GetValue<bool>() ?? true);
        var id = Assert.IsType<string>(response["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        var descendant = Assert.Single(await driver.ReadFixtureTranscriptAsync(), static entry => entry.Kind == "descendant");
        using var environment = await ReadDescendantEnvironmentAsync(Assert.IsType<int>(descendant.ProcessId));
        Assert.True(environment.RootElement.GetProperty("aliasesEqual").GetBoolean());
        await driver.StopSingleControlledSessionAsync(id);
    }


    [Fact]
    public async Task StartDebug_RemovedPathWithoutDotnet_RejectsBeforeAdapterStarts()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            AdditionalEnvironment: new Dictionary<string, string?> { ["PATH"] = "" }),
            startupTimeout: TimeSpan.FromSeconds(10));
        var response = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["env"] = new JsonObject { ["PATH"] = null },
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-no-dotnet")));
        Assert.True(response["isError"]?.GetValue<bool>() ?? false);
        var content = Assert.IsType<JsonObject>(response["structuredContent"]);
        Assert.Equal("start_debug_dotnet_unavailable", content["kind"]?.GetValue<string>());
        Assert.Equal("START_DEBUG_DOTNET_UNAVAILABLE", content["error"]?.GetValue<string>());
        Assert.False(content.ContainsKey("debugSessionId"));
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }

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
            new JsonObject { ["env"] = "not-an-object" },
            new JsonObject { ["env"] = new JsonObject { ["BAD"] = 12 } },
            new JsonObject { ["env"] = new JsonObject { [""] = "value" } },
            new JsonObject { ["env"] = new JsonObject { ["BAD=NAME"] = "value" } },
            new JsonObject { ["env"] = new JsonObject { ["BAD\0NAME"] = "value" } },
            new JsonObject { ["env"] = new JsonObject { ["VALID_NAME"] = "bad\0value" } },
            new JsonObject { ["env"] = new JsonObject { ["NETCOREDBG_MCP_NATIVE_SCENE_PROBE_PIPE"] = "value" } },
            new JsonObject { ["env"] = new JsonObject { ["NETCOREDBG_MCP_NATIVE_SCENE_PROBE_NONCE"] = null } },
            new JsonObject { ["env"] = new JsonObject { [OperatingSystem.IsWindows() ? "netcoredbg_mcp_native_scene_probe_pipe" : "NETCOREDBG_MCP_NATIVE_SCENE_PROBE_PIPE"] = "value" } },
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
            ["env"] = null,
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
    private static async Task<System.Text.Json.JsonDocument> ReadDescendantEnvironmentAsync(int processId)
    {
        var transcript = Assert.IsType<string>(Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT"));
        var path = transcript + ".environment-" + processId;
        using var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        while (!File.Exists(path)) await Task.Delay(25, timeout.Token);
        return System.Text.Json.JsonDocument.Parse(await File.ReadAllTextAsync(path, timeout.Token));
    }

}
