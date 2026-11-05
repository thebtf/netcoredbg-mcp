using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;


[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class ModernProtocolContractTests
{
    private const string ValidProgram = "controlled-program.dll";

    [Theory]
    [InlineData("2025-11-25")]
    [InlineData("2026-07-28")]
    public async Task ProjectSearch_UsesPinnedProjectAndExistingConsumerContract(string protocolVersion)
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var root = Path.Combine(scratch, "project");
            Directory.CreateDirectory(root);
            await File.WriteAllTextAsync(Path.Combine(root, "Alpha.cs"),
                "public class VisibleProbe\n{\n    public VisibleProbe Next;\n}\n");
            await File.WriteAllTextAsync(Path.Combine(scratch, "Outside.cs"), "outside-project-secret");

            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["NETCOREDBG_PROJECT_ROOT"] = root,
                    ["MCP_PROJECT_ROOT"] = null,
                },
                ClientProtocolVersion: protocolVersion), startupTimeout: TimeSpan.FromSeconds(10));

            async Task<JsonObject> InvokeAsync(string name, JsonObject arguments, bool expectedError = false)
            {
                var request = new JsonObject { ["name"] = name, ["arguments"] = arguments };
                if (protocolVersion == "2026-07-28") request["_meta"] = ModernMcpProcessDriver.CurrentMeta();
                var response = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync(
                    "tools/call", request, new RequestId($"project-search-{name}")));
                Assert.Equal("complete", response["resultType"]?.GetValue<string>());
                Assert.Equal(expectedError, response["isError"]?.GetValue<bool>() ?? false);
                var content = Assert.IsType<JsonObject>(response["structuredContent"]);
                Assert.False(content.ContainsKey("state"));
                Assert.False(content.ContainsKey("message"));
                return content;
            }

            var symbols = await InvokeAsync("find_code_symbol", new JsonObject { ["name"] = "VisibleProbe", ["kind"] = "class" });
            var symbolData = Assert.IsType<JsonObject>(symbols["data"]);
            Assert.Equal(root, symbolData["project_root"]?.GetValue<string>());
            var symbol = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(symbolData["results"])));
            Assert.Equal("Alpha.cs", symbol["file"]?.GetValue<string>());
            Assert.Equal(1, symbol["line"]?.GetValue<int>());
            Assert.Equal("class", symbol["kind"]?.GetValue<string>());

            var references = await InvokeAsync("find_code_references", new JsonObject { ["name"] = "VisibleProbe" });
            var referenceData = Assert.IsType<JsonObject>(references["data"]);
            Assert.Equal(2, referenceData["count"]?.GetValue<int>());
            Assert.Equal([1, 3], Assert.IsType<JsonArray>(referenceData["results"]).Select(static item => item!["line"]!.GetValue<int>()));

            var context = await InvokeAsync("get_source_context", new JsonObject { ["file"] = "Alpha.cs", ["line"] = 2, ["radius"] = 1 });
            var contextData = Assert.IsType<JsonObject>(context["data"]);
            Assert.Equal("Alpha.cs", contextData["file"]?.GetValue<string>());
            Assert.Equal(["public class VisibleProbe", "{", "    public VisibleProbe Next;"],
                Assert.IsType<JsonArray>(contextData["lines"]).Select(static item => item!["text"]!.GetValue<string>()));

            var outside = await InvokeAsync("get_source_context", new JsonObject { ["file"] = "../Outside.cs", ["line"] = 1 }, expectedError: true);
            Assert.NotNull(outside["error"]);
            Assert.DoesNotContain("outside-project-secret", outside.ToJsonString(), StringComparison.Ordinal);
        }
        finally
        {
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    [Fact]
    public async Task ProjectSearch_ModernRootsAreRequestedAndResolvedPerCall()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var firstRoot = Path.Combine(scratch, "first");
            var secondRoot = Path.Combine(scratch, "second");
            Directory.CreateDirectory(firstRoot);
            Directory.CreateDirectory(secondRoot);
            await File.WriteAllTextAsync(Path.Combine(firstRoot, "Marker.cs"), "public class FirstProjectMarker {}\n");
            await File.WriteAllTextAsync(Path.Combine(secondRoot, "Marker.cs"), "public class SecondProjectMarker {}\n");

            await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync(options: new ModernMcpStartOptions(
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["NETCOREDBG_PROJECT_ROOT"] = null,
                    ["MCP_PROJECT_ROOT"] = null,
                }));

            async Task CheckRootAsync(string root, string expectedLine, string id)
            {
                var meta = ModernMcpProcessDriver.CurrentMeta();
                Assert.IsType<JsonObject>(meta[MetaKeys.ClientCapabilities])["roots"] = new JsonObject();
                var request = new JsonObject
                {
                    ["name"] = "get_source_context",
                    ["arguments"] = new JsonObject { ["file"] = "Marker.cs", ["line"] = 1, ["radius"] = 0 },
                    ["_meta"] = meta,
                };
                var initial = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync(
                    "tools/call", request, new RequestId($"{id}-roots-request"))));
                Assert.Equal("input_required", initial["resultType"]?.GetValue<string>());
                var input = Assert.Single(Assert.IsType<JsonObject>(initial["inputRequests"]));
                Assert.Equal("roots/list", input.Value?["method"]?.GetValue<string>());
                Assert.False(initial.ContainsKey("requestState"));

                request["inputResponses"] = new JsonObject
                {
                    [input.Key] = new JsonObject
                    {
                        ["roots"] = new JsonArray(
                            new JsonObject { ["uri"] = "file://remote.invalid/share" },
                            new JsonObject { ["uri"] = new Uri(root + Path.DirectorySeparatorChar).AbsoluteUri }),
                    },
                };
                var completed = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync(
                    "tools/call", request, new RequestId($"{id}-roots-response"))));
                Assert.Equal("complete", completed["resultType"]?.GetValue<string>());
                var data = Assert.IsType<JsonObject>(completed["structuredContent"]?["data"]);
                Assert.Equal(root, data["project_root"]?.GetValue<string>());
                Assert.Equal(expectedLine, Assert.Single(Assert.IsType<JsonArray>(data["lines"]))?["text"]?.GetValue<string>());
            }

            await CheckRootAsync(firstRoot, "public class FirstProjectMarker {}", "first");
            await CheckRootAsync(secondRoot, "public class SecondProjectMarker {}", "second");

            var fallback = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync("tools/call", new JsonObject
            {
                ["name"] = "get_source_context",
                ["arguments"] = new JsonObject { ["file"] = "host/NetCoreDbg.Mcp.Stateless/Program.cs", ["line"] = 1, ["radius"] = 0 },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
            }, new RequestId("no-roots-fallback"))));
            Assert.Equal(Path.GetFullPath(RepositoryLayout.Root), fallback["structuredContent"]?["data"]?["project_root"]?.GetValue<string>());
        }
        finally
        {
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    [Theory]
    [InlineData("2025-06-18")]
    [InlineData("2025-11-25")]
    public async Task LegacyHandshake_CanUseTheExplicitNativeSession(string version)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(ClientProtocolVersion: version), startupTimeout: TimeSpan.FromSeconds(10));
        Assert.Equal(version, driver.Client.NegotiatedProtocolVersion);
        var response = await driver.SendRawRequestAsync("tools/list", new JsonObject(), new RequestId("legacy-catalog"));
        AssertCatalog(ModernMcpProcessDriver.RequireResult(response));
        var started = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("tools/call", new JsonObject
        {
            ["name"] = "start_debug",
            ["arguments"] = new JsonObject { ["program"] = driver.InertProgramPath, ["justMyCode"] = false },
        }, new RequestId("legacy-start")));
        Assert.False(started["isError"]?.GetValue<bool>() ?? false);
        var id = Assert.IsType<string>(started["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        var state = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("tools/call", new JsonObject
        {
            ["name"] = "get_debug_state",
            ["arguments"] = new JsonObject { ["debugSessionId"] = id },
        }, new RequestId("legacy-state")));
        Assert.Equal("debug_state_success", state["structuredContent"]?["kind"]?.GetValue<string>());
        var missing = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("tools/call", new JsonObject
        {
            ["name"] = "get_debug_state",
            ["arguments"] = new JsonObject(),
        }, new RequestId("legacy-state-without-id")));
        Assert.Equal("debug_session_not_found", missing["structuredContent"]?["kind"]?.GetValue<string>());
        var stopped = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("tools/call", new JsonObject
        {
            ["name"] = "stop_debug",
            ["arguments"] = new JsonObject { ["debugSessionId"] = id },
        }, new RequestId("legacy-stop")));
        Assert.Equal("stop_debug_success", stopped["structuredContent"]?["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task LegacyCompat_ConsoleJourney_UsesImplicitSessionAndPythonEnvelopes()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true)),
            startupTimeout: TimeSpan.FromSeconds(10));
        Assert.Equal("2025-11-25", driver.Client.NegotiatedProtocolVersion);
        var meta = new JsonObject();
        var idle = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), meta);
        Assert.Equal("idle", idle["state"]?.GetValue<string>());
        Assert.Equal("idle", idle["data"]?["state"]?.GetValue<string>());
        Assert.Equal("idle", idle["data"]?["execState"]?.GetValue<string>());
        Assert.Null(idle["data"]?["currentThreadId"]);
        Assert.Contains("start_debug", Assert.IsType<JsonArray>(idle["next_actions"]).Select(static item => item?.GetValue<string>()));

        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, meta);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        Assert.Equal(driver.InertProgramPath, started["data"]?["program"]?.GetValue<string>());
        Assert.False(started.ContainsKey("debugSessionId"));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);

        var threads = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), meta);
        Assert.Equal("stopped", threads["state"]?.GetValue<string>());
        var firstThread = Assert.IsType<JsonObject>(Assert.IsType<JsonArray>(threads["data"])[0]);
        Assert.IsType<int>(firstThread["id"]?.GetValue<int>());
        Assert.IsType<string>(firstThread["name"]?.GetValue<string>());
        var state = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), meta);
        Assert.Equal("stopped", state["state"]?.GetValue<string>());
        Assert.Equal("stopped", state["data"]?["state"]?.GetValue<string>());
        Assert.Equal(1, state["data"]?["currentThreadId"]?.GetValue<int>());

        var stack = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject { ["levels"] = 20 }, meta);
        Assert.Equal("stopped", stack["state"]?.GetValue<string>());
        var frames = Assert.IsType<JsonArray>(Assert.IsType<JsonObject>(stack["data"])["frames"]);
        Assert.NotEmpty(frames);
        var firstFrame = Assert.IsType<JsonObject>(frames[0]);
        Assert.IsType<int>(firstFrame["id"]?.GetValue<int>());
        Assert.IsType<string>(firstFrame["name"]?.GetValue<string>());
        var step = await LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), meta, TimeSpan.FromSeconds(35));
        Assert.Equal("stopped", step["state"]?.GetValue<string>());
        Assert.Equal("step", step["reason"]?.GetValue<string>());
        Assert.Equal(1, step["thread_id"]?.GetValue<int>());
        Assert.False(step["timed_out"]?.GetValue<bool>() ?? true);
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), meta);
        Assert.Equal("idle", stop["state"]?.GetValue<string>());
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_MuxOwner_DeniesOtherMutationsButAllowsReadsAndOwnerStop()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true)),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "owner-A" };
        var other = new JsonObject { ["muxSessionId"] = "owner-B" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        var threads = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), other);
        Assert.Equal("stopped", threads["state"]?.GetValue<string>());
        Assert.NotEmpty(Assert.IsType<JsonArray>(threads["data"]));
        var before = await driver.ReadNativeActionsAsync();
        foreach (var tool in new[] { "step_over", "stop_debug" })
        {
            var denied = await LegacyConsoleCallAsync(driver, tool, new JsonObject(), other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.Equal("stopped", denied["state"]?.GetValue<string>());
        }
        var unowned = await LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), new JsonObject());
        Assert.Contains("owned by another agent", unowned["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        var state = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), other);
        Assert.Equal("stopped", state["state"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.Equal("idle", stop["state"]?.GetValue<string>());
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_DirectOwnerSurvivesRejectedTaggedStart()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var direct = new JsonObject();
        var tagged = new JsonObject { ["muxSessionId"] = "other" };
        var launch = new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, direct))["data"]?["success"]?.GetValue<bool>());
        var rejected = await LegacyConsoleCallAsync(driver, "start_debug", launch, tagged);
        Assert.Contains("already active", rejected["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        var denied = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), tagged);
        Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        var stopped = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), direct);
        Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_StartingOwnerCannotBeReclaimedEvenAtZeroTimeout()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["NETCOREDBG_SESSION_TIMEOUT"] = "0",
                ["CONTROLLED_DAP_HOLD_LAUNCH_RESPONSE"] = "1",
            }, FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "owner-A" };
        var other = new JsonObject { ["muxSessionId"] = "owner-B" };
        var start = LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner, TimeSpan.FromSeconds(20));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        try
        {
            await driver.WaitForFixtureRecordAsync("launch-response-gated", observation.Token);
            var denied = await LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), other);
            Assert.Contains("starting", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var state = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), other);
            Assert.Equal("initializing", state["state"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleaseThreadsResponse();
            _ = await Record.ExceptionAsync(async () => await start);
        }
        Assert.True((await start)["data"]?["success"]?.GetValue<bool>());
        var stopped = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_MuxCapability_IsAdvertised()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]),
            startupTimeout: TimeSpan.FromSeconds(10));
        var experimental = Assert.IsAssignableFrom<IDictionary<string, object>>(driver.Client.ServerCapabilities?.Experimental);
        var mux = Assert.IsType<JsonElement>(experimental["x-mux"]);
        Assert.Equal("isolated", mux.GetProperty("sharing").GetString());
    }

    [Fact]
    public async Task LegacyCompat_StateRemainsResponsiveDuringPendingStep()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", NextResponseMode: "hold")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "owner-A" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var step = LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), owner, TimeSpan.FromSeconds(35));
        Task<JsonObject>? state = null;
        var responsive = false;
        try
        {
            await driver.WaitForFixtureRecordAsync("next-response-gated", observation.Token);
            state = LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner, TimeSpan.FromSeconds(10));
            responsive = await Task.WhenAny(state, Task.Delay(TimeSpan.FromSeconds(2))) == state;
            if (responsive)
                Assert.Equal("unknown", (await state)["state"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleaseNextResponse();
            await step.WaitAsync(TimeSpan.FromSeconds(10));
            if (state is not null) await state.WaitAsync(TimeSpan.FromSeconds(10));
        }
        Assert.True(responsive, "State inspection must not wait for an in-flight step to finish.");
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_OwnerCanStopDuringPendingStep()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", NextResponseMode: "hold")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "owner-A" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var step = LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), owner, TimeSpan.FromSeconds(35));
        Task<JsonObject>? stop = null;
        var responsive = false;
        try
        {
            await driver.WaitForFixtureRecordAsync("next-response-gated", observation.Token);
            stop = LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner, TimeSpan.FromSeconds(10));
            responsive = await Task.WhenAny(stop, Task.Delay(TimeSpan.FromSeconds(5))) == stop;
        }
        finally
        {
            driver.ReleaseNextResponse();
            await step.WaitAsync(TimeSpan.FromSeconds(10));
            if (stop is not null) await stop.WaitAsync(TimeSpan.FromSeconds(10));
        }
        Assert.True(responsive, "Owner stop must not wait for the pending step response.");
        Assert.True((await stop!)["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_Continue_UsesImplicitSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint", NextResponseMode: "continue-breakpoint-terminal")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var meta = new JsonObject();
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, meta);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), meta);
        var continued = await LegacyConsoleCallAsync(driver, "continue_execution", new JsonObject(), meta, TimeSpan.FromSeconds(35));
        Assert.Equal("stopped", continued["state"]?.GetValue<string>());
        Assert.Equal("breakpoint", continued["reason"]?.GetValue<string>());
        Assert.Equal(2, continued["thread_id"]?.GetValue<int>());
        Assert.False(continued["timed_out"]?.GetValue<bool>() ?? true);
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), meta);
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_StepIntoOut_UsesCurrentThreadAndInvalidatesInspection()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true)),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "navigation-owner" };
        var other = new JsonObject { ["muxSessionId"] = "navigation-other" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);

        var before = await driver.ReadNativeActionsAsync();
        foreach (var tool in new[] { "step_into", "step_out", "pause_execution" })
        {
            var denied = await LegacyConsoleCallAsync(driver, tool, new JsonObject(), other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        }
        var target = await LegacyConsoleCallAsync(driver, "step_into", new JsonObject { ["target_id"] = 17 }, owner);
        Assert.NotNull(target["error"]);
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), action => action.Command is "stepIn" or "stepOut" or "pause");

        foreach (var (tool, arguments, command) in new[]
        {
            ("step_into", new JsonObject(), "stepIn"),
            ("step_into", new JsonObject { ["target_id"] = null }, "stepIn"),
            ("step_out", new JsonObject(), "stepOut"),
        })
        {
            var from = (await driver.ReadNativeActionsAsync()).Count;
            var step = await LegacyConsoleCallAsync(driver, tool, arguments, owner, TimeSpan.FromSeconds(35));
            Assert.Equal("stopped", step["state"]?.GetValue<string>());
            Assert.Equal("step", step["reason"]?.GetValue<string>());
            Assert.Equal(1, step["thread_id"]?.GetValue<int>());
            Assert.False(step["timed_out"]?.GetValue<bool>() ?? true);
            var action = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(from), entry => entry.Command == command);
            Assert.Equal(1, JsonNode.Parse(action.Detail!)?["arguments"]?["threadId"]?.GetValue<int>());
            var staleBefore = (await driver.ReadNativeActionsAsync()).Count;
            var stale = await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject(), owner);
            Assert.NotNull(stale["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(staleBefore), entry => entry.Command == "scopes");
            _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
        }
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_PauseCanInterruptPendingContinue_AndIsOwnerBound()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", NextResponseMode: "continue-ack-only", PauseResponseMode: "ack-before-stopped-held")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "pause-owner" };
        var other = new JsonObject { ["muxSessionId"] = "pause-other" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
        var continuation = LegacyConsoleCallAsync(driver, "continue_execution", new JsonObject(), owner, TimeSpan.FromSeconds(35));
        var stoppedByOwner = false;
        try
        {
            await driver.WaitForFixtureRecordAsync("continue-acknowledged", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.False(continuation.IsCompleted);
            var pendingState = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), other);
            Assert.Equal("running", pendingState["state"]?.GetValue<string>());
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var denied = await LegacyConsoleCallAsync(driver, "pause_execution", new JsonObject(), other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command == "pause");
            var pause = await LegacyConsoleCallAsync(driver, "pause_execution", new JsonObject { ["thread_id"] = 1 }, owner);
            Assert.True(pause["data"]?["success"]?.GetValue<bool>());
            Assert.Equal(1, pause["data"]?["threadId"]?.GetValue<int>());
            Assert.Equal("running", pause["state"]?.GetValue<string>());
            var pauseWrite = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command == "pause");
            Assert.Equal(1, JsonNode.Parse(pauseWrite.Detail!)?["arguments"]?["threadId"]?.GetValue<int>());
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", observation.Token);
            Assert.False(continuation.IsCompleted, "Pause acknowledgement must not wait for or imply a stopped event.");
            driver.ReleasePauseStoppedEvent();
            var stopped = await continuation.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.Equal("stopped", stopped["state"]?.GetValue<string>());
            Assert.Equal("pause", stopped["reason"]?.GetValue<string>());
            var beforeInspection = (await driver.ReadNativeActionsAsync()).Count;
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject(), owner))["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeInspection), action => action.Command == "scopes");
            var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
            Assert.True(stop["data"]?["success"]?.GetValue<bool>());
            stoppedByOwner = true;
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            driver.ReleaseContinueResponse();
            if (!stoppedByOwner)
                await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
            await Record.ExceptionAsync(async () => await continuation.WaitAsync(TimeSpan.FromSeconds(10)));
        }
    }

    [Fact]
    public async Task LegacyCompat_OutputReadsAreShared_OnlyOwnerCanConsumeWholeBuffer()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(
                new JsonObject { ["category"] = "stdout", ["output"] = "one\n" },
                new JsonObject { ["category"] = "stderr", ["output"] = "err\n" },
                new JsonObject { ["category"] = "stdout", ["output"] = "two\n" }),
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "output-owner" };
        var other = new JsonObject { ["muxSessionId"] = "output-other" };
        var idle = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), other);
        Assert.Equal("idle", idle["state"]?.GetValue<string>());
        Assert.Equal(string.Empty, idle["data"]?["output"]?.GetValue<string>());
        var idleTail = await LegacyConsoleCallAsync(driver, "get_output_tail", new JsonObject(), other);
        Assert.Equal(0, idleTail["data"]?["total_lines"]?.GetValue<int>());
        Assert.Equal(string.Empty, idleTail["data"]?["output"]?.GetValue<string>());
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var all = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), other);
        Assert.Equal("one\nerr\ntwo\n", all["data"]?["output"]?.GetValue<string>());
        var stdout = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject { ["category"] = "stdout" }, other);
        Assert.Equal("one\ntwo\n", stdout["data"]?["output"]?.GetValue<string>());
        var tail = await LegacyConsoleCallAsync(driver, "get_output_tail", new JsonObject { ["lines"] = 2 }, other);
        Assert.Equal(3, tail["data"]?["total_lines"]?.GetValue<int>());
        Assert.Equal(2, tail["data"]?["returned_lines"]?.GetValue<int>());
        Assert.Equal("err\ntwo", tail["data"]?["output"]?.GetValue<string>());
        var stdoutTail = await LegacyConsoleCallAsync(driver, "get_output_tail", new JsonObject { ["lines"] = 1, ["category"] = "stdout" }, other);
        Assert.Equal(2, stdoutTail["data"]?["total_lines"]?.GetValue<int>());
        Assert.Equal(1, stdoutTail["data"]?["returned_lines"]?.GetValue<int>());
        Assert.Equal("two", stdoutTail["data"]?["output"]?.GetValue<string>());
        var filtered = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject { ["category"] = "stdout", ["clear"] = true }, other);
        Assert.Equal("one\ntwo\n", filtered["data"]?["output"]?.GetValue<string>());
        foreach (var arguments in new[]
        {
            new JsonObject { ["clear"] = true },
            new JsonObject { ["clear"] = true, ["category"] = null },
            new JsonObject { ["clear"] = true, ["category"] = "" },
        })
        {
            var denied = await LegacyConsoleCallAsync(driver, "get_output", arguments, other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        }
        Assert.Equal("one\nerr\ntwo\n", (await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), other))["data"]?["output"]?.GetValue<string>());
        var cleared = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject { ["clear"] = true }, owner);
        Assert.Equal("one\nerr\ntwo\n", cleared["data"]?["output"]?.GetValue<string>());
        Assert.Equal(string.Empty, (await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), other))["data"]?["output"]?.GetValue<string>());
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
        var afterStop = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), other);
        Assert.Equal("idle", afterStop["state"]?.GetValue<string>());
        Assert.Equal(string.Empty, afterStop["data"]?["output"]?.GetValue<string>());
    }

    [Theory]
    [InlineData("exited")]
    [InlineData("terminated")]
    public async Task LegacyCompat_OutputSurvivesNaturalTerminationUntilStop(string terminalEvent)
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject { ["category"] = "stdout", ["output"] = "before\n" }),
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(new JsonObject { ["category"] = "stderr", ["output"] = "final\n" }),
                ["terminal"] = terminalEvent,
            }),
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "terminal-output-owner" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var retained = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), owner);
        Assert.Equal("before\nfinal\n", retained["data"]?["output"]?.GetValue<string>());
        var stderr = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject { ["category"] = "stderr" }, owner);
        Assert.Equal("final\n", stderr["data"]?["output"]?.GetValue<string>());
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
        Assert.Equal(string.Empty, (await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), owner))["data"]?["output"]?.GetValue<string>());
    }

    [Fact]
    public async Task LegacyCompat_BreakpointsLocalsAndEvaluation_UseCurrentStoppedEpoch()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true)),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "inspection-owner" };
        var source = Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/RemoveBreakpointContractTests.cs");
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);

        var beforeUnsupported = await driver.ReadNativeActionsAsync();
        foreach (var option in new[] { "condition", "hit_condition" })
        {
            var denied = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject
            {
                ["file"] = source,
                ["line"] = 2,
                [option] = "1",
            }, owner);
            Assert.NotNull(denied["error"]);
        }
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeUnsupported.Count), action => action.Command == "setBreakpoints");
        var location = new JsonObject { ["file"] = source, ["line"] = JsonNode.Parse("2.0") };
        var added = await LegacyConsoleCallAsync(driver, "add_breakpoint", location, owner);
        Assert.Equal("stopped", added["state"]?.GetValue<string>());
        Assert.Equal(Path.GetFullPath(source), added["data"]?["file"]?.GetValue<string>());
        Assert.Equal(2, added["data"]?["line"]?.GetValue<int>());
        Assert.True(added["data"]?["verified"]?.GetValue<bool>());
        Assert.Null(added["data"]?["condition"]);
        var removed = await LegacyConsoleCallAsync(driver, "remove_breakpoint", location, owner);
        Assert.True(removed["data"]?["removed"]?.GetValue<bool>());

        var stack = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
        var frames = Assert.IsType<JsonArray>(stack["data"]?["frames"]);
        Assert.NotEmpty(frames);
        var frame = Assert.IsType<JsonObject>(frames[0])["id"]!.GetValue<int>();
        var scopes = await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject { ["frame_id"] = JsonNode.Parse("0.0") }, owner);
        var locals = Assert.IsType<JsonObject>(Assert.IsType<JsonArray>(scopes["data"])[0]);
        Assert.Equal("Locals", locals["name"]?.GetValue<string>());
        var reference = locals["variablesReference"]!.GetValue<int>();
        Assert.Equal(1001, reference);
        var beforeFilter = await driver.ReadNativeActionsAsync();
        var filtered = await LegacyConsoleCallAsync(driver, "get_variables", new JsonObject
        {
            ["variables_reference"] = reference,
            ["filter"] = "indexed",
        }, owner);
        Assert.NotNull(filtered["error"]);
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeFilter.Count), action => action.Command == "variables");
        var variables = await LegacyConsoleCallAsync(driver, "get_variables", new JsonObject
        {
            ["variables_reference"] = JsonNode.Parse($"{reference}.0"),
            ["filter"] = null,
            ["start"] = null,
            ["count"] = null,
        }, owner);
        var first = Assert.IsType<JsonObject>(Assert.IsType<JsonArray>(variables["data"])[0]);
        Assert.Equal("intVar", first["name"]?.GetValue<string>());
        Assert.Equal("42", first["value"]?.GetValue<string>());
        Assert.Equal("int", first["type"]?.GetValue<string>());
        Assert.Equal(0, first["variablesReference"]?.GetValue<int>());
        var evaluation = await LegacyConsoleCallAsync(driver, "evaluate_expression", new JsonObject
        {
            ["expression"] = "6 * 7",
            ["frame_id"] = frame,
        }, owner);
        Assert.Equal("42", evaluation["data"]?["result"]?.GetValue<string>());
        Assert.Equal("int", evaluation["data"]?["type"]?.GetValue<string>());
        Assert.Equal(0, evaluation["data"]?["variablesReference"]?.GetValue<int>());
        var inspectionWrites = await driver.ReadNativeActionsAsync();
        var scopesWrite = Assert.Single(inspectionWrites, static action => action.Command == "scopes");
        var evaluationWrite = Assert.Single(inspectionWrites, static action => action.Command == "evaluate");
        Assert.Equal(frame, JsonNode.Parse(scopesWrite.Detail!)?["arguments"]?["frameId"]?.GetValue<int>());
        Assert.Equal(frame, JsonNode.Parse(evaluationWrite.Detail!)?["arguments"]?["frameId"]?.GetValue<int>());
        Assert.Equal("watch", JsonNode.Parse(evaluationWrite.Detail!)?["arguments"]?["context"]?.GetValue<string>());

        var step = await LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), owner, TimeSpan.FromSeconds(35));
        Assert.Equal("step", step["reason"]?.GetValue<string>());
        var before = await driver.ReadNativeActionsAsync();
        var oldScopes = await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject(), owner);
        var oldVariables = await LegacyConsoleCallAsync(driver, "get_variables", new JsonObject { ["variables_reference"] = reference }, owner);
        var oldEvaluation = await LegacyConsoleCallAsync(driver, "evaluate_expression", new JsonObject { ["expression"] = "6 * 7", ["frame_id"] = frame }, owner);
        Assert.NotNull(oldScopes["error"]);
        Assert.NotNull(oldVariables["error"]);
        Assert.NotNull(oldEvaluation["error"]);
        var after = await driver.ReadNativeActionsAsync();
        Assert.DoesNotContain(after.Skip(before.Count), action => action.Command is "scopes" or "variables" or "evaluate");

        _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
        var freshScopes = await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject(), owner);
        var freshReference = Assert.IsType<JsonArray>(freshScopes["data"])[0]!["variablesReference"]!.GetValue<int>();
        var freshVariables = await LegacyConsoleCallAsync(driver, "get_variables", new JsonObject { ["variables_reference"] = freshReference }, owner);
        Assert.Equal("43", Assert.IsType<JsonArray>(freshVariables["data"])[0]?["value"]?.GetValue<string>());
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_InspectionReadsAreShared_BreakpointAndEvaluationMutationsAreOwnerBound()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true)),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "inspection-owner" };
        var other = new JsonObject { ["muxSessionId"] = "inspection-other" };
        var source = Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/RemoveBreakpointContractTests.cs");
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var location = new JsonObject { ["file"] = source, ["line"] = 2 };
        Assert.True((await LegacyConsoleCallAsync(driver, "add_breakpoint", location, owner))["data"]?["verified"]?.GetValue<bool>());
        _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
        var stack = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject { ["thread_id"] = 2 }, other);
        var frame = Assert.IsType<JsonArray>(stack["data"]?["frames"])[0]!["id"]!.GetValue<int>();
        var before = await driver.ReadNativeActionsAsync();
        foreach (var (tool, args) in new[]
        {
            ("add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 3 }),
            ("remove_breakpoint", location),
            ("evaluate_expression", new JsonObject { ["expression"] = "6 * 7", ["frame_id"] = frame }),
        })
        {
            var denied = await LegacyConsoleCallAsync(driver, tool, args, other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        }
        var after = await driver.ReadNativeActionsAsync();
        Assert.DoesNotContain(after.Skip(before.Count), action => action.Command is "setBreakpoints" or "evaluate");
        var scopes = await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject(), other);
        Assert.Equal("Locals", Assert.IsType<JsonArray>(scopes["data"])[0]?["name"]?.GetValue<string>());
        var beforeOwnerInspection = await driver.ReadNativeActionsAsync();
        var displaced = await LegacyConsoleCallAsync(driver, "evaluate_expression", new JsonObject { ["expression"] = "6 * 7" }, owner);
        Assert.NotNull(displaced["error"]);
        var displacedScopes = await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject(), owner);
        var displacedVariables = await LegacyConsoleCallAsync(driver, "get_variables", new JsonObject { ["variables_reference"] = 1001 }, owner);
        Assert.NotNull(displacedScopes["error"]);
        Assert.NotNull(displacedVariables["error"]);
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(beforeOwnerInspection.Count),
            action => action.Command is "scopes" or "variables" or "evaluate");
        _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
        var restored = await LegacyConsoleCallAsync(driver, "evaluate_expression", new JsonObject { ["expression"] = "6 * 7" }, owner);
        Assert.Equal("42", restored["data"]?["result"]?.GetValue<string>());
        var stop = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stop["data"]?["success"]?.GetValue<bool>());
    }

    private static async Task<JsonObject> LegacyConsoleCallAsync(ModernMcpProcessDriver driver, string tool,
        JsonObject arguments, JsonObject meta, TimeSpan? timeout = null)
    {
        var response = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(tool, arguments, meta,
            new RequestId($"legacy-console-{Guid.NewGuid():N}"), timeout: timeout ?? TimeSpan.FromSeconds(15)));
        return Assert.IsType<JsonObject>(ModernMcpProcessDriver.LegacyPayload(response));
    }


    [Fact]
    public async Task Discover_IsSupportedAsTheLiteralFirstFreshProcessRequest_AndDeclaresCacheableTools()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();

        var message = await driver.SendFirstRequestAsync(
            "server/discover",
            new JsonObject { ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
            new RequestId("discover-first"));
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(message));

        Assert.True(result["capabilities"]?["tools"] is JsonObject, "Discovery must declare the tools capability.");
        AssertCacheable(result);
    }

    [Fact]
    public async Task ListTools_IsSupportedAsTheLiteralFirstFreshProcessRequest_WithoutDiscoveryState()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();

        var message = await driver.SendFirstRequestAsync(
            "tools/list",
            new JsonObject { ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
            new RequestId("list-first"));
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(message));

        AssertCatalog(result);
        AssertCacheable(result);
    }

    [Fact]
    public async Task ValidStartCall_IsSupportedAsTheLiteralFirstFreshProcessRequest_WithoutDiscoveryState()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();

        var message = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject { ["program"] = ValidProgram },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
            },
            new RequestId("call-first"));

        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(message));
        AssertCompleteStartEnvelope(result);
        await driver.StopSingleControlledSessionAsync(Assert.IsType<string>(result["structuredContent"]?["debugSessionId"]?.GetValue<string>()));
    }

    [Fact]
    public async Task FirstWire_OverlappingRequests_AreRefusedBeforeDispatch()
    {
        var driver = await ModernMcpProcessDriver.StartFirstWireAsync(
            fixtureConfiguration: new FixtureConfiguration(DelayLaunchResponseForStartupTimeout: true));
        var primary = await Record.ExceptionAsync(async () =>
        {
            var first = driver.SendFirstRequestAsync("tools/call", new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject { ["program"] = ValidProgram },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
            }, new RequestId("overlap-start"), timeout: TimeSpan.FromSeconds(10));
            Assert.False(first.IsCompleted);

            var secondFailure = await Record.ExceptionAsync(async () => await driver.SendFirstRequestAsync(
                "tools/list", new JsonObject { ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
                new RequestId("overlap-list")));
            var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await first));
            AssertCompleteStartEnvelope(result);
            await driver.StopSingleControlledSessionAsync(Assert.IsType<string>(result["structuredContent"]?["debugSessionId"]?.GetValue<string>()));
            Assert.IsType<InvalidOperationException>(secondFailure);
        });
        var cleanup = await Record.ExceptionAsync(async () => await driver.DisposeAsync());
        if (primary is not null && cleanup is not null) throw new AggregateException(primary, cleanup);
        if (primary is not null) throw primary;
        if (cleanup is not null) throw cleanup;
    }

    [Fact]
    public async Task Metadata_IsEvaluatedPerRequest_AndIsNotRetainedAfterAnUnsupportedVersion()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var rejectedMeta = ModernMcpProcessDriver.CurrentMeta();
        rejectedMeta[MetaKeys.ProtocolVersion] = "1900-01-01";

        var rejected = await driver.SendFirstRequestAsync(
            "tools/list",
            new JsonObject { ["_meta"] = rejectedMeta },
            new RequestId("unsupported-version"));
        AssertExactUnsupportedVersionError(rejected);

        var current = await driver.SendFirstRequestAsync(
            "tools/list",
            new JsonObject { ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
            new RequestId("current-version"));
        AssertCatalog(ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(current)));
    }

    [Fact]
    public async Task UnsupportedVersion_ReturnsExactOfficialJsonRpcErrorData()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var meta = ModernMcpProcessDriver.CurrentMeta();
        meta[MetaKeys.ProtocolVersion] = "1900-01-01";

        var message = await driver.SendFirstRequestAsync(
            "server/discover",
            new JsonObject { ["_meta"] = meta },
            new RequestId("version-error"));
        AssertExactUnsupportedVersionError(message);
    }

    [Fact]
    public async Task ToolsList_ReturnsExactlyTheOrderedNativeCatalogWithRuntimeSchemas()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("catalog"));

        AssertCatalog(ModernMcpProcessDriver.RequireResult(response));
    }

    [Fact]
    public async Task ToolsList_ConfigureExceptions_PublishesClosedOptionalFiltersInput()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("exception-filters-catalog")));
        var tool = Assert.Single(
            Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == "configure_exceptions");
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId"], Assert.IsType<JsonArray>(schema["required"])
            .Select(static item => item?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(["debugSessionId", "filters"], properties.Select(static item => item.Key).Order());
        Assert.Equal("string", properties["debugSessionId"]?["type"]?.GetValue<string>());
        Assert.Equal(1, properties["debugSessionId"]?["minLength"]?.GetValue<int>());
        Assert.Equal("\\S", properties["debugSessionId"]?["pattern"]?.GetValue<string>());
        Assert.IsType<JsonObject>(properties["filters"]);
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("get_modules")]
    [InlineData("get_progress")]
    [InlineData("get_loaded_sources")]
    public async Task ToolsList_RuntimeInventory_PublishesRequiredOpaqueSessionOnly(string toolName)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var result = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"inventory-catalog-{toolName}")));

        var tool = Assert.Single(
            Assert.IsType<JsonArray>(result["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            item => item["name"]?.GetValue<string>() == toolName);
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId"], Assert.IsType<JsonArray>(schema["required"])
            .Select(static item => item?.GetValue<string>()));
        var property = Assert.Single(Assert.IsType<JsonObject>(schema["properties"]));
        Assert.Equal("debugSessionId", property.Key);
        var sessionId = Assert.IsType<JsonObject>(property.Value);
        Assert.Equal("string", sessionId["type"]?.GetValue<string>());
        Assert.Equal(1, sessionId["minLength"]?.GetValue<int>());
        Assert.Equal("\\S", sessionId["pattern"]?.GetValue<string>());
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData(129, "\U0001F600", "DEBUG_SESSION_NOT_FOUND")]
    [InlineData(256, "\U0001F600", "DEBUG_SESSION_NOT_FOUND")]
    [InlineData(256, "x", "DEBUG_SESSION_NOT_FOUND")]
    [InlineData(257, "\U0001F600", "INVALID_TOOL_ARGUMENTS")]
    public async Task UiInvoke_AutomationIdLength_MatchesPublishedSchemaBeforeSessionLookup(
        int characterCount,
        string character,
        string expectedError)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var catalog = ModernMcpProcessDriver.RequireResult(
            await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("invoke-length-schema")));
        var invoke = Assert.Single(
            Assert.IsType<JsonArray>(catalog["tools"]).Select(static tool => Assert.IsType<JsonObject>(tool)),
            static tool => tool["name"]?.GetValue<string>() == "ui_invoke");
        Assert.Equal(256, invoke["inputSchema"]?["properties"]?["automationId"]?["maxLength"]?.GetValue<int>());

        var response = await driver.CallToolRawAsync(
            "ui_invoke",
            new JsonObject
            {
                ["debugSessionId"] = "unknown-debug-session",
                ["automationId"] = string.Concat(Enumerable.Repeat(character, characterCount)),
            },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("invoke-length-call"));
        var result = ModernMcpProcessDriver.RequireResult(response);

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        await AssertNoNativeActionsAsync(driver);
        var error = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal(expectedError, (error["code"] ?? error["error"])?.GetValue<string>());
    }

    [Fact]
    public async Task CompleteStart_ReturnsCallToolEnvelopeWithoutCacheFields()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var raw = await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = ValidProgram },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("complete-envelope"));

        AssertCompleteStartEnvelope(ModernMcpProcessDriver.RequireResult(raw));
    }

    [Fact]
    public async Task UnknownTool_ReturnsCompleteTextErrorWithoutApplicationStructuredContent()
    {
        const string unknownTool = "pr242-unknown-tool-schema";
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.CallToolRawAsync(
            unknownTool,
            new JsonObject(),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("unknown-tool"));
        var result = ModernMcpProcessDriver.RequireResult(response);

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.False(result.ContainsKey("structuredContent"));
        var content = Assert.Single(Assert.IsType<JsonArray>(result["content"]));
        Assert.Equal("text", Assert.IsType<JsonObject>(content)["type"]?.GetValue<string>());
        Assert.Equal($"Unknown tool: {unknownTool}", Assert.IsType<string>(Assert.IsType<JsonObject>(content)["text"]?.GetValue<string>()));
        await AssertNoNativeActionsAsync(driver);
    }

    [Fact]
    public async Task DelayedControlledStartup_ReturnsCompleteStartEnvelope()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(FixtureConfiguration: new(DelayLaunchResponseForStartupTimeout: true)));

        var response = await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = ValidProgram },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("startup-timeout"));
        var result = ModernMcpProcessDriver.RequireResult(response);

        AssertCompleteStartEnvelope(result);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task MissingProgram_WithFormElicitation_UsesInputRequiredAndNewIdRetryWithoutRequestState(bool includeBreakpoints)
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var initialId = new RequestId("input-required");
        var arguments = includeBreakpoints
            ? new JsonObject { ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = BreakpointSourcePath, ["line"] = 2 }) }
            : new JsonObject();
        arguments["args"] = new JsonArray("via elicitation");
        arguments["cwd"] = RepositoryLayout.Root;
        arguments["stopAtEntry"] = true;
        var inputRequestId = await RequestProgramElicitationAsync(driver, initialId, arguments);
        await AssertNoNativeActionsAsync(driver);
        var retryId = new RequestId("input-required-retry");
        var retry = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = arguments.DeepClone(),
                ["inputResponses"] = new JsonObject { [inputRequestId] = new JsonObject { ["action"] = "accept", ["content"] = new JsonObject { ["program"] = ValidProgram } } },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(formElicitation: true),
            },
            retryId, timeout: TimeSpan.FromSeconds(10));

        Assert.NotEqual(initialId, retryId);
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(retry));
        AssertCompleteStartEnvelope(result);
        var launch = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "launch");
        using var document = System.Text.Json.JsonDocument.Parse(launch.Detail!);
        var launchArguments = document.RootElement.GetProperty("arguments");
        Assert.Equal("via elicitation", Assert.Single(launchArguments.GetProperty("args").EnumerateArray()).GetString());
        Assert.Equal(RepositoryLayout.Root, launchArguments.GetProperty("cwd").GetString());
        Assert.True(launchArguments.GetProperty("stopAtEntry").GetBoolean());
        if (includeBreakpoints)
        {
            AssertBreakpointRequest(Assert.Single(await driver.ReadNativeActionsAsync(),
                static action => action.Command == "setBreakpoints"), BreakpointSourcePath, [2]);
        }
        await driver.StopSingleControlledSessionAsync(Assert.IsType<string>(result["structuredContent"]?["debugSessionId"]?.GetValue<string>()));
    }

    [Theory]
    [InlineData("decline")]
    [InlineData("cancel")]
    public async Task MissingProgram_WithFormElicitation_DeclineOrCancelReturnsCompleteApplicationErrorWithoutNativeAction(string action)
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var inputRequestId = await RequestProgramElicitationAsync(driver, new RequestId($"input-required-{action}-initial"));

        var retry = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject(),
                ["inputResponses"] = new JsonObject { [inputRequestId] = new JsonObject { ["action"] = action } },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(formElicitation: true),
            },
            new RequestId($"input-required-{action}"));
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(retry));

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("start_debug_input_unavailable", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.Equal("START_DEBUG_PROGRAM_REQUIRED", result["structuredContent"]?["error"]?.GetValue<string>());
        Assert.False(result.ContainsKey("requestState"));
        await AssertNoNativeActionsAsync(driver);
    }

    [Fact]
    public async Task MissingProgram_WithoutFormElicitation_ReturnsCompleteApplicationErrorWithoutNativeAction()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();

        var response = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject(),
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
            },
            new RequestId("no-elicitation"));
        var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(response));

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("start_debug_input_unavailable", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.Equal("START_DEBUG_PROGRAM_REQUIRED", result["structuredContent"]?["error"]?.GetValue<string>());
        Assert.False(result.ContainsKey("requestState"));
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("start_debug", "program")]
    [InlineData("get_debug_state", "debugSessionId")]
    [InlineData("stop_debug", "debugSessionId")]
    public async Task EmptyArguments_ReturnCompleteInvalidArgumentErrorsBeforeNativeActions(string tool, string field)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.CallToolRawAsync(
            tool,
            new JsonObject { [field] = "" },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"empty-{tool}"));

        AssertInvalidArguments(ModernMcpProcessDriver.RequireResult(response), tool);
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("start_debug")]
    [InlineData("get_debug_state")]
    [InlineData("stop_debug")]
    public async Task ExtraArguments_ReturnCompleteInvalidArgumentErrorsBeforeNativeActions(string tool)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var arguments = tool == "start_debug"
            ? new JsonObject { ["program"] = ValidProgram, ["extra"] = true }
            : new JsonObject { ["debugSessionId"] = new string('x', 32), ["extra"] = true };

        var response = await driver.CallToolRawAsync(
            tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"extra-{tool}"));

        AssertInvalidArguments(ModernMcpProcessDriver.RequireResult(response), tool);
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("get_debug_state", null)]
    [InlineData("get_debug_state", "short")]
    [InlineData("get_debug_state", "not-a-capability")]
    [InlineData("stop_debug", null)]
    [InlineData("stop_debug", "short")]
    [InlineData("stop_debug", "not-a-capability")]
    public async Task MissingShortAndMalformedHandles_AreUniformNotFoundWithoutNativeActions(string tool, string? handle)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var arguments = new JsonObject();
        if (handle is not null)
        {
            arguments["debugSessionId"] = handle;
        }

        var response = await driver.CallToolRawAsync(
            tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"handle-{tool}-{handle ?? "missing"}"));
        var result = ModernMcpProcessDriver.RequireResult(response);

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("debug_session_not_found", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", result["structuredContent"]?["error"]?.GetValue<string>());
        await AssertNoNativeActionsAsync(driver);
    }
    [Fact]
    public async Task SuccessfulOfficialSdkExchange_ProvesStdoutContainsOnlyMcpFrames()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("stdout-purity"));

        AssertCatalog(ModernMcpProcessDriver.RequireResult(response));
        Assert.False(driver.Client.Completion.IsCompleted, "Non-MCP stdout would make the official stdio transport fail the exchange.");
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task StartDebug_AbsentOrEmptyBreakpoints_PreservesExistingLaunch(bool includeEmptyArray)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var arguments = new JsonObject { ["program"] = driver.InertProgramPath };
        if (includeEmptyArray)
        {
            arguments["breakpoints"] = new JsonArray();
        }

        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(
            "start_debug", arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId("breakpoints-empty")));

        AssertCompleteStartEnvelope(result);
        Assert.Null(result["structuredContent"]?["initialBreakpoints"]);
        Assert.Equal(["initialize", "launch", "configurationDone"], (await driver.ReadNativeActionsAsync())
            .Where(static action => action.Kind == "request").Select(static action => action.Command));
    }

    [Fact]
    public async Task StartDebug_InterleavedBreakpoints_GroupsBeforeLaunchAndRetainsInitialResults()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("mixed"));
        var secondSource = Path.Combine(RepositoryLayout.Root,
            "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs");
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug",
            BreakpointArguments(driver, (BreakpointSourcePath, 2), (secondSource, 4),
                (Path.GetRelativePath(RepositoryLayout.Root, BreakpointSourcePath), 6), (secondSource, 8), (BreakpointSourcePath, 2)),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("breakpoints-grouped")));

        AssertCompleteStartEnvelope(result);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureRecordAsync("breakpoint-verified-event", deadline.Token);
        var actions = await driver.ReadNativeActionsAsync();
        Assert.Equal(["initialize", "setBreakpoints", "setBreakpoints", "launch", "configurationDone"],
            actions.Where(static action => action.Kind == "request").Select(static action => action.Command));
        var requests = actions.Where(static action => action.Command == "setBreakpoints").ToArray();
        AssertBreakpointRequest(requests[0], BreakpointSourcePath, [2, 6, 2]);
        AssertBreakpointRequest(requests[1], secondSource, [4, 8]);
        var initial = Assert.IsType<JsonArray>(result["structuredContent"]?["initialBreakpoints"]);
        Assert.Equal([(0, false, (int?)null), (1, false, null), (2, true, 7), (3, true, 9), (4, true, 3)],
            initial.Select(static entry => (entry!["index"]!.GetValue<int>(), entry["verified"]!.GetValue<bool>(),
                entry["actualLine"]?.GetValue<int>())));
        Assert.DoesNotContain("private controlled breakpoint payload", result.ToJsonString());
    }

    [Theory]
    [InlineData("not-array")]
    [InlineData("invalid-line")]
    [InlineData("unsupported-condition")]
    [InlineData("over-limit")]
    [InlineData("missing-source")]
    public async Task StartDebug_InvalidBreakpointsOrMissingSource_PerformsNoAdapterWork(string scenario)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var arguments = BreakpointArguments(driver, (BreakpointSourcePath, 2));
        var entry = arguments["breakpoints"]![0]!;
        switch (scenario)
        {
            case "not-array": arguments["breakpoints"] = new JsonObject(); break;
            case "invalid-line": entry["line"] = 0; break;
            case "unsupported-condition": entry["condition"] = "true"; break;
            case "over-limit":
                arguments["breakpoints"] = new JsonArray(Enumerable.Range(0, 257)
                    .Select(_ => entry.DeepClone()).ToArray());
                break;
            case "missing-source": entry["file"] = driver.InertProgramPath + ".missing.cs"; break;
        }

        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(
            "start_debug", arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"breakpoints-{scenario}")));

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        if (scenario != "missing-source")
        {
            AssertInvalidArguments(result, "start_debug");
        }
        await AssertNoNativeActionsAsync(driver);
    }

    [Theory]
    [InlineData("refused")]
    [InlineData("missing-verified")]
    [InlineData("too-few-results")]
    public async Task StartDebug_RejectedOrMalformedBreakpoints_DrainsAdapterWithoutLaunching(string responseMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions(responseMode));
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug",
            BreakpointArguments(driver, (BreakpointSourcePath, 2), (BreakpointSourcePath, 6)),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"breakpoints-response-{responseMode}")));

        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Null(result["structuredContent"]?["debugSessionId"]);
        Assert.DoesNotContain("private controlled breakpoint payload", result.ToJsonString());
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        var actions = await driver.ReadNativeActionsAsync();
        Assert.Single(actions, static action => action.Command == "setBreakpoints");
        Assert.DoesNotContain(actions, static action => action.Command is "launch" or "configurationDone");
        var transcriptPath = Assert.IsType<string>(Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT"));
        var transcript = (await File.ReadAllLinesAsync(transcriptPath, deadline.Token)).Select(FixtureTranscriptEntry.Parse);
        var processId = Assert.IsType<int>(Assert.Single(transcript, static entry => entry.Kind == "startup").ProcessId);
        try
        {
            using var adapter = Process.GetProcessById(processId);
            await adapter.WaitForExitAsync(deadline.Token);
        }
        catch (ArgumentException)
        {
            // The owned adapter has already exited.
        }
    }

    [Fact]
    public async Task StartDebug_SeparateSessions_DoNotShareBreakpointConfiguration()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var first = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug",
            BreakpointArguments(driver, (BreakpointSourcePath, 2)), ModernMcpProcessDriver.CurrentMeta(), new RequestId("breakpoints-first")));
        AssertCompleteStartEnvelope(first);
        var afterFirst = (await driver.ReadNativeActionsAsync()).Count;
        var second = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug",
            BreakpointArguments(driver, (BreakpointSourcePath, 6)), ModernMcpProcessDriver.CurrentMeta(), new RequestId("breakpoints-second")));
        AssertCompleteStartEnvelope(second);

        Assert.NotEqual(first["structuredContent"]?["debugSessionId"]?.GetValue<string>(),
            second["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        AssertBreakpointRequest(Assert.Single((await driver.ReadNativeActionsAsync()).Skip(afterFirst),
            static action => action.Command == "setBreakpoints"), BreakpointSourcePath, [6]);
        Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(first["structuredContent"]?["initialBreakpoints"]))?["actualLine"]?.GetValue<int>());
        Assert.Equal(6, Assert.Single(Assert.IsType<JsonArray>(second["structuredContent"]?["initialBreakpoints"]))?["actualLine"]?.GetValue<int>());
    }

    [Fact]
    public async Task ToolsList_AddBreakpointPublishesClosedSourceAndPositiveInt32Input()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-breakpoint-catalog")));
        var tool = Assert.Single(
            Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == "add_breakpoint");
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId", "file", "line"], Assert.IsType<JsonArray>(schema["required"]).Select(static value => value?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(3, properties.Count);
        foreach (var field in new[] { "debugSessionId", "file" })
        {
            Assert.Equal("string", properties[field]?["type"]?.GetValue<string>());
            Assert.Equal(1, properties[field]?["minLength"]?.GetValue<int>());
        }
        Assert.Equal("integer", properties["line"]?["type"]?.GetValue<string>());
        Assert.Equal(1, properties["line"]?["minimum"]?.GetValue<int>());
        Assert.Equal(int.MaxValue, properties["line"]?["maximum"]?.GetValue<int>());
    }

    [Theory]
    [InlineData("success", true)]
    [InlineData("success", false)]
    [InlineData("mixed", true)]
    public async Task AddBreakpoint_InitialAndTwoRuntimeAdditions_PreserveRequestedLinesAndInitialDuplicates(string responseMode, bool stopped)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(SetBreakpointsResponseMode: responseMode,
                LifecycleMode: stopped ? "all-stop" : "all-stop-continued-true")));
        var session = await StartBreakpointSessionAsync(driver, $"add-preserve-{responseMode}-{stopped}",
            (BreakpointSourcePath, 2), (BreakpointSourcePath, 6), (BreakpointSourcePath, 2));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync(stopped ? "stopped" : "continued", observation.Token);
        await BreakpointReaderBarrierAsync(driver, session, "add-preserve-reader-barrier", observation.Token);
        var state = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-preserve-state")), isError: false);
        Assert.Equal(stopped ? "stopped" : "continued", state["state"]?["event"]?.GetValue<string>());
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var first = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint",
            AddBreakpointArguments(session, Path.GetRelativePath(RepositoryLayout.Root, BreakpointSourcePath), 8),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-preserve-first")), isError: false);
        AssertAddedBreakpoint(first, BreakpointSourcePath, 8, true, responseMode == "mixed" ? 9 : 8);
        var second = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 10),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-preserve-second")), isError: false);
        AssertAddedBreakpoint(second, BreakpointSourcePath, 10, true, responseMode == "mixed" ? 11 : 10);
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.Equal(2, writes.Length);
        AssertBreakpointRequest(writes[0], BreakpointSourcePath, [2, 6, 2, 8]);
        AssertBreakpointRequest(writes[1], BreakpointSourcePath, [2, 6, 2, 8, 10]);

        var beforeDuplicate = (await driver.ReadNativeActionsAsync()).Count;
        var duplicate = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 8),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-preserve-duplicate")), isError: false);
        AssertAddedBreakpoint(duplicate, BreakpointSourcePath, 8, true, responseMode == "mixed" ? 9 : 8);
        var duplicateWrites = (await driver.ReadNativeActionsAsync()).Skip(beforeDuplicate).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.InRange(duplicateWrites.Length, 0, 1);
        foreach (var write in duplicateWrites)
        {
            AssertBreakpointRequest(write, BreakpointSourcePath, [2, 6, 2, 8, 10]);
        }
    }

    [Fact]
    public async Task AddBreakpoint_UnverifiedResult_PreservesFalseAndOmittedActualLine()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("mixed"));
        var session = await StartBreakpointSessionAsync(driver, "add-unverified-start");
        var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-unverified")), isError: false);
        AssertAddedBreakpoint(content, BreakpointSourcePath, 2, false, null);
        var second = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 6),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-after-unverified")), isError: false);
        AssertAddedBreakpoint(second, BreakpointSourcePath, 6, true, 7);
        AssertBreakpointRequest((await driver.ReadNativeActionsAsync()).Last(static action => action.Command == "setBreakpoints"), BreakpointSourcePath, [2, 6]);
    }

    [Fact]
    public async Task AddBreakpoint_ConcurrentSameSourceUpdates_SerializeWithoutLostLines()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("hold-first-runtime"));
        var session = await StartBreakpointSessionAsync(driver, "add-concurrent-start", (BreakpointSourcePath, 2));
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var first = driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 6),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-concurrent-first"), timeout: TimeSpan.FromSeconds(10));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("runtime-breakpoints-response-gated", observation.Token);
            var second = driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 8),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-concurrent-second"), timeout: TimeSpan.FromSeconds(10));
            await BreakpointReaderBarrierAsync(driver, session, "add-concurrent-reader-barrier", observation.Token);
            Assert.False(first.IsCompleted);
            Assert.False(second.IsCompleted);
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            driver.ReleaseNextResponse();
            var results = await Task.WhenAll(first, second).WaitAsync(observation.Token);
            AssertAddedBreakpoint(AssertBreakpointEnvelope(results[0], isError: false), BreakpointSourcePath, 6, true, 6);
            AssertAddedBreakpoint(AssertBreakpointEnvelope(results[1], isError: false), BreakpointSourcePath, 8, true, 8);
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            Assert.Equal(2, writes.Length);
            AssertBreakpointRequest(writes[0], BreakpointSourcePath, [2, 6]);
            AssertBreakpointRequest(writes[1], BreakpointSourcePath, [2, 6, 8]);
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task AddBreakpoint_InvalidArgumentsUnavailableTokenOrSource_PerformsNoDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var session = await StartBreakpointSessionAsync(driver, "add-invalid-start", (BreakpointSourcePath, 2));
        var invalid = new List<JsonObject?> { null };
        foreach (var field in new[] { "debugSessionId", "file", "line" })
        {
            var arguments = AddBreakpointArguments(session, BreakpointSourcePath, 6);
            arguments.Remove(field);
            invalid.Add(arguments);
        }
        foreach (var field in new[] { "debugSessionId", "file" })
        {
            foreach (var value in new JsonNode?[] { JsonValue.Create(""), JsonValue.Create(" \t "), JsonValue.Create(1), null })
            {
                var arguments = AddBreakpointArguments(session, BreakpointSourcePath, 6);
                arguments[field] = value?.DeepClone();
                invalid.Add(arguments);
            }
        }
        foreach (var value in new JsonNode?[] { JsonValue.Create(0), JsonValue.Create(-1), JsonValue.Create(1.5), JsonValue.Create(2_147_483_648L), JsonValue.Create("6"), null })
        {
            var arguments = AddBreakpointArguments(session, BreakpointSourcePath, 6);
            arguments["line"] = value;
            invalid.Add(arguments);
        }
        foreach (var field in new[] { "condition", "hitCondition", "unexpected" })
        {
            var arguments = AddBreakpointArguments(session, BreakpointSourcePath, 6);
            arguments[field] = "true";
            invalid.Add(arguments);
        }
        var before = (await driver.ReadNativeActionsAsync()).Count;
        for (var index = 0; index < invalid.Count; index++)
        {
            var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", invalid[index],
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-invalid-{index}")), isError: true);
            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
            Assert.Equal("add_breakpoint", content["tool"]?.GetValue<string>());
        }
        AssertFixedBreakpointError(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint",
            AddBreakpointArguments("unavailable-opaque-token", BreakpointSourcePath, 6), ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-unavailable-token")), isError: true),
            "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        AssertFixedBreakpointError(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint",
            AddBreakpointArguments(session, driver.InertProgramPath + ".missing.cs", 6), ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-unavailable-source")), isError: true),
            "breakpoint_source_unavailable", "BREAKPOINT_SOURCE_UNAVAILABLE");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task AddBreakpoint_RefusedUpdate_DoesNotContaminateLaterReplacement()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("refuse-first-runtime"));
        var session = await StartBreakpointSessionAsync(driver, "add-refused-start", (BreakpointSourcePath, 2));
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var refused = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 6),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-refused")), isError: true);
        AssertFixedBreakpointError(refused, "dap_set_breakpoints_refused", "DAP_SET_BREAKPOINTS_REFUSED");
        var state = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-refused-retained")), isError: false);
        Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
        var added = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 8),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-after-refusal")), isError: false);
        AssertAddedBreakpoint(added, BreakpointSourcePath, 8, true, 8);
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.Equal(2, writes.Length);
        AssertBreakpointRequest(writes[0], BreakpointSourcePath, [2, 6]);
        AssertBreakpointRequest(writes[1], BreakpointSourcePath, [2, 8]);
    }

    [Theory]
    [InlineData("missing-verified")]
    [InlineData("too-few-results")]
    [InlineData("reader-failure")]
    public async Task AddBreakpoint_MalformedOrUncertainResponse_ReturnsRedactedProtocolErrorAndDrains(string responseMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions(responseMode));
        var session = await StartBreakpointSessionAsync(driver, $"add-malformed-{responseMode}-start");
        var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-malformed-{responseMode}")), isError: true);
        AssertFixedBreakpointError(content, "dap_set_breakpoints_protocol_error", "DAP_SET_BREAKPOINTS_PROTOCOL_ERROR");
        await AssertBreakpointSessionNotFoundAsync(driver, session, $"add-malformed-{responseMode}-state");
    }

    [Fact]
    public async Task AddBreakpoint_PostWriteCallerCancellation_DrainsBeforeReuse()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions("hold-first-runtime"));
        var session = await StartBreakpointSessionAsync(driver, "add-cancel-start", (BreakpointSourcePath, 2));
        using var cancellation = new CancellationTokenSource();
        var operation = driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 6),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-cancel"), cancellation.Token, timeout: TimeSpan.FromSeconds(10));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureRecordAsync("runtime-breakpoints-response-gated", observation.Token);
        await BreakpointReaderBarrierAsync(driver, session, "add-cancel-reader-barrier", observation.Token);
        Assert.False(operation.IsCompleted);
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
        await driver.WaitForFixtureRecordAsync("disconnect-response", observation.Token);
        await AssertBreakpointSessionNotFoundAsync(driver, session, "add-cancel-state");
        var before = (await driver.ReadNativeActionsAsync()).Count;
        AssertFixedBreakpointError(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 8),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-cancel-reuse")), isError: true), "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task AddBreakpoint_SeparateSessions_DoNotShareRuntimeConfiguration()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var first = await StartBreakpointSessionAsync(driver, "add-isolation-first", (BreakpointSourcePath, 2));
        var second = await StartBreakpointSessionAsync(driver, "add-isolation-second", (BreakpointSourcePath, 4));
        Assert.NotEqual(first, second);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        foreach (var (session, line) in new[] { (first, 6), (second, 8), (first, 10), (second, 12) })
        {
            AssertAddedBreakpoint(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, line),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-isolation-{line}")), isError: false), BreakpointSourcePath, line, true, line);
        }
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.Equal(4, writes.Length);
        AssertBreakpointRequest(writes[0], BreakpointSourcePath, [2, 6]);
        AssertBreakpointRequest(writes[1], BreakpointSourcePath, [4, 8]);
        AssertBreakpointRequest(writes[2], BreakpointSourcePath, [2, 6, 10]);
        AssertBreakpointRequest(writes[3], BreakpointSourcePath, [4, 8, 12]);
    }

    [Fact]
    public async Task AddBreakpoint_TotalSessionLimit_RejectsNewLineWithoutIoButAllowsExistingLine()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(BreakpointLaunchOptions());
        var otherSource = Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs");
        var seeds = Enumerable.Range(1, 128).Select(line => (BreakpointSourcePath, line))
            .Concat(Enumerable.Range(1, 128).Select(line => (otherSource, line))).ToArray();
        var session = await StartBreakpointSessionAsync(driver, "add-limit-start", seeds);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        AssertFixedBreakpointError(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 129),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-limit-new-line")), isError: true), "breakpoint_limit_reached", "BREAKPOINT_LIMIT_REACHED");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        AssertAddedBreakpoint(AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("add-limit-existing-line")), isError: false), BreakpointSourcePath, 2, true, 2);
        var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
        Assert.InRange(writes.Length, 0, 1);
        foreach (var write in writes)
        {
            AssertBreakpointRequest(write, BreakpointSourcePath, Enumerable.Range(1, 128).ToArray());
        }
    }

    [Theory]
    [InlineData("all-stop-exited", "exited")]
    [InlineData("all-stop-terminated", "terminated")]
    public async Task AddBreakpoint_AlreadyObservedTerminalSession_RefusesWithoutDapIo(string lifecycleMode, string terminal)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: lifecycleMode)));
        var session = await StartBreakpointSessionAsync(driver, $"add-terminal-{terminal}-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync(terminal, observation.Token);
        await BreakpointReaderBarrierAsync(driver, session, $"add-terminal-{terminal}-reader-barrier", observation.Token);
        var state = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-terminal-{terminal}-state")), isError: false);
        Assert.Equal(terminal, state["state"]?["event"]?.GetValue<string>());
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var refused = AssertBreakpointEnvelope(await driver.CallToolRawAsync("add_breakpoint", AddBreakpointArguments(session, BreakpointSourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"add-terminal-{terminal}")), isError: true);
        Assert.NotEqual("breakpoint_added", refused["kind"]?.GetValue<string>());
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    private static JsonObject AddBreakpointArguments(string session, string file, int line) => new()
    {
        ["debugSessionId"] = session,
        ["file"] = file,
        ["line"] = line,
    };

    private static async Task<string> StartBreakpointSessionAsync(ModernMcpProcessDriver driver, string requestId, params (string File, int Line)[] breakpoints)
    {
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", BreakpointArguments(driver, breakpoints),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)));
        AssertCompleteStartEnvelope(result);
        return Assert.IsType<string>(result["structuredContent"]?["debugSessionId"]?.GetValue<string>());
    }

    private static async Task BreakpointReaderBarrierAsync(ModernMcpProcessDriver driver, string session, string requestId, CancellationToken cancellationToken)
    {
        var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId), cancellationToken), isError: false);
        Assert.Equal("threads_success", content["kind"]?.GetValue<string>());
    }

    private static async Task AssertBreakpointSessionNotFoundAsync(ModernMcpProcessDriver driver, string session, string requestId)
    {
        var content = AssertBreakpointEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: true);
        AssertFixedBreakpointError(content, "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
    }

    private static JsonObject AssertBreakpointEnvelope(JsonRpcResponse response, bool isError)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() == true);
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", text["type"]?.GetValue<string>());
        Assert.Equal(content.ToJsonString(), text["text"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        return content;
    }

    private static void AssertAddedBreakpoint(JsonObject content, string file, int line, bool verified, int? actualLine)
    {
        Assert.Equal("breakpoint_added", content["kind"]?.GetValue<string>());
        Assert.Equal(Path.GetFullPath(file), content["file"]?.GetValue<string>());
        Assert.Equal(line, content["line"]?.GetValue<int>());
        Assert.Equal(verified, content["verified"]?.GetValue<bool>());
        Assert.Equal(actualLine.HasValue, content.ContainsKey("actualLine"));
        if (actualLine is { } actual)
        {
            Assert.Equal(actual, content["actualLine"]?.GetValue<int>());
        }
        Assert.Equal(actualLine.HasValue ? 5 : 4, content.Count);
    }

    private static void AssertFixedBreakpointError(JsonObject content, string kind, string error)
    {
        Assert.Equal(kind, content["kind"]?.GetValue<string>());
        Assert.Equal(error, content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static string BreakpointSourcePath => Path.Combine(RepositoryLayout.Root,
        "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/ModernProtocolContractTests.cs");

    private static ModernMcpStartOptions BreakpointLaunchOptions(string responseMode = "success") => new(
        FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, SetBreakpointsResponseMode: responseMode));

    private static JsonObject BreakpointArguments(ModernMcpProcessDriver driver, params (string File, int Line)[] breakpoints) => new()
    {
        ["program"] = driver.InertProgramPath,
        ["breakpoints"] = new JsonArray(breakpoints.Select(static entry => (JsonNode)new JsonObject
        { ["file"] = entry.File, ["line"] = entry.Line }).ToArray()),
    };

    private static void AssertBreakpointRequest(ModernNativeAction action, string source, int[] lines)
    {
        var arguments = Assert.IsType<JsonObject>(JsonNode.Parse(Assert.IsType<string>(action.Detail))?["arguments"]);
        Assert.Equal(Path.GetFullPath(source), arguments["source"]?["path"]?.GetValue<string>());
        Assert.Equal(lines, Assert.IsType<JsonArray>(arguments["breakpoints"]).Select(static entry => entry!["line"]!.GetValue<int>()));
    }

    private static void AssertCacheable(JsonObject result)
    {
        Assert.True(result["ttlMs"]?.GetValue<long>() > 0);
        Assert.Equal("public", result["cacheScope"]?.GetValue<string>());
    }

    private static void AssertCatalog(JsonObject result)
    {
        var tools = Assert.IsType<JsonArray>(result["tools"]);
        Assert.Equal(39, tools.Count);
        Assert.Equal(
            [
                "start_debug",
                "get_debug_state",
                "stop_debug",
                "get_threads",
                "get_call_stack",
                "step_over",
                "get_scopes",
                "get_variables",
                "continue_execution",
                "pause_execution",
                "add_breakpoint",
                "remove_breakpoint",
                "configure_exceptions",
                "ui_invoke",
                "ui_invoke_observed",
                "ui_get_window_tree",
                "get_output",
                "get_output_tail",
                "search_output",
                "step_into",
                "step_out",
                "evaluate_expression",
                "set_variable",
                "get_exception_info",
                "get_modules",
                "get_progress",
                "get_loaded_sources",
                "get_stop_context",
                "get_exception_context",
                "get_ui_probe_capabilities",
                "capture_visual_evidence",
                "read_capture_artifact",
                "wait_for_ui_stable",
                "capture_element_snapshot",
                "capture_native_scene",
                "find_code_symbol",
                "find_code_references",
                "get_source_context",
                "get_step_in_targets",
            ],
            tools.Select(static tool => tool?["name"]?.GetValue<string>()));
        Assert.Equal(
            ["start_debug", "get_debug_state", "stop_debug", "get_threads", "get_call_stack", "step_over", "get_scopes", "get_variables", "continue_execution", "pause_execution", "add_breakpoint", "remove_breakpoint"],
            tools.Take(12).Select(static tool => tool?["name"]?.GetValue<string>()));

        var start = Assert.IsType<JsonObject>(tools[0]);
        Assert.Equal("object", start["inputSchema"]?["type"]?.GetValue<string>());
        Assert.False(start["inputSchema"]?["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(1, Assert.IsType<JsonObject>(start["inputSchema"]?["properties"])["program"]?["minLength"]?.GetValue<int>());
        var breakpoints = Assert.IsType<JsonObject>(start["inputSchema"]?["properties"]?["breakpoints"]);
        Assert.Equal(256, breakpoints["maxItems"]?.GetValue<int>());
        Assert.Equal("array", breakpoints["type"]?.GetValue<string>());
        Assert.False(breakpoints["items"]?["additionalProperties"]?.GetValue<bool>() ?? true);

        foreach (var tool in tools.Skip(1).Take(2).Select(static tool => Assert.IsType<JsonObject>(tool)))
        {
            var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
            Assert.Equal("object", schema["type"]?.GetValue<string>());
            Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
            Assert.Contains("debugSessionId", Assert.IsType<JsonArray>(schema["required"]).Select(static value => value?.GetValue<string>()));
            Assert.Equal(32, Assert.IsType<JsonObject>(schema["properties"])["debugSessionId"]?["minLength"]?.GetValue<int>());
        }

        var threads = Assert.IsType<JsonObject>(tools[3]);
        var threadsSchema = Assert.IsType<JsonObject>(threads["inputSchema"]);
        Assert.Equal("object", threadsSchema["type"]?.GetValue<string>());
        Assert.False(threadsSchema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId"], Assert.IsType<JsonArray>(threadsSchema["required"]).Select(static value => value?.GetValue<string>()));
        var debugSessionId = Assert.IsType<JsonObject>(threadsSchema["properties"])["debugSessionId"];
        Assert.Equal("string", debugSessionId?["type"]?.GetValue<string>());
        Assert.Equal(1, debugSessionId?["minLength"]?.GetValue<int>());

        var invoke = Assert.IsType<JsonObject>(tools[13]);
        var invokeSchema = Assert.IsType<JsonObject>(invoke["inputSchema"]);
        Assert.Equal("object", invokeSchema["type"]?.GetValue<string>());
        Assert.False(invokeSchema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId", "automationId"], Assert.IsType<JsonArray>(invokeSchema["required"]).Select(static value => value?.GetValue<string>()));
        var invokeProperties = Assert.IsType<JsonObject>(invokeSchema["properties"]);
        Assert.Equal(3, invokeProperties.Count);
        foreach (var property in new[] { invokeProperties["debugSessionId"], invokeProperties["automationId"] }.Select(static value => Assert.IsType<JsonObject>(value)))
        {
            Assert.Equal("string", property["type"]?.GetValue<string>());
            Assert.Equal(1, property["minLength"]?.GetValue<int>());
            Assert.Equal("\\S", property["pattern"]?.GetValue<string>());
        }
        Assert.Equal(256, invokeProperties["automationId"]?["maxLength"]?.GetValue<int>());
        var expectedTarget = Assert.IsType<JsonObject>(invokeProperties["expectedTarget"]);
        Assert.Equal("object", expectedTarget["type"]?.GetValue<string>());
        Assert.False(expectedTarget["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["processIdentity", "hwnd", "rootRuntimeId", "elementRuntimeId"],
            Assert.IsType<JsonArray>(expectedTarget["required"]).Select(static value => value?.GetValue<string>()));
        var targetProperties = Assert.IsType<JsonObject>(expectedTarget["properties"]);
        Assert.Equal(4, targetProperties.Count);
        foreach (var name in new[] { "processIdentity", "hwnd" })
        {
            Assert.Equal("string", targetProperties[name]?["type"]?.GetValue<string>());
            Assert.Equal(1, targetProperties[name]?["minLength"]?.GetValue<int>());
            Assert.Equal("\\S", targetProperties[name]?["pattern"]?.GetValue<string>());
        }
        foreach (var name in new[] { "rootRuntimeId", "elementRuntimeId" })
        {
            Assert.Equal("array", targetProperties[name]?["type"]?.GetValue<string>());
            Assert.Equal(1, targetProperties[name]?["minItems"]?.GetValue<int>());
            Assert.Equal(64, targetProperties[name]?["maxItems"]?.GetValue<int>());
            Assert.Equal("integer", targetProperties[name]?["items"]?["type"]?.GetValue<string>());
        }
    }

    private static async Task<string> RequestProgramElicitationAsync(ModernMcpFirstWireDriver driver, RequestId initialId, JsonObject? arguments = null)
    {
        var initial = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = arguments?.DeepClone() ?? new JsonObject(),
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(formElicitation: true),
            },
            initialId);
        var initialResult = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(initial));

        Assert.Equal("input_required", initialResult["resultType"]?.GetValue<string>());
        var inputRequests = Assert.IsType<JsonObject>(initialResult["inputRequests"]);
        var inputRequest = Assert.Single(inputRequests);
        var inputRequestEnvelope = Assert.IsType<JsonObject>(inputRequest.Value);
        Assert.Equal("elicitation/create", inputRequestEnvelope["method"]?.GetValue<string>());
        Assert.Equal("form", inputRequestEnvelope["params"]?["mode"]?.GetValue<string>());
        Assert.False(initialResult.ContainsKey("requestState"));
        return inputRequest.Key;
    }

    private static void AssertCompleteStartEnvelope(JsonObject result)
    {
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.False(result["isError"]?.GetValue<bool>() ?? true);
        Assert.NotEmpty(Assert.IsType<JsonArray>(result["content"]));
        Assert.Equal("start_debug_success", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.NotNull(result["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        Assert.False(result.ContainsKey("ttlMs"));
        Assert.False(result.ContainsKey("cacheScope"));
    }

    private static void AssertInvalidArguments(JsonObject result, string tool)
    {
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        Assert.Equal("invalid_tool_arguments", result["structuredContent"]?["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", result["structuredContent"]?["error"]?.GetValue<string>());
        Assert.Equal(tool, result["structuredContent"]?["tool"]?.GetValue<string>());
    }

    private static void AssertExactUnsupportedVersionError(JsonRpcMessage message)
    {
        var error = Assert.IsType<JsonRpcError>(message);

        Assert.Equal(-32022, error.Error.Code);
        var data = Assert.IsType<JsonElement>(error.Error.Data);
        Assert.Equal(JsonValueKind.Object, data.ValueKind);
        Assert.Equal(["requested", "supported"], data.EnumerateObject().Select(static property => property.Name).Order());
        Assert.True(data.TryGetProperty("requested", out var requested));
        Assert.Equal("1900-01-01", requested.GetString());
        Assert.True(data.TryGetProperty("supported", out var supported));
        Assert.Equal(JsonValueKind.Array, supported.ValueKind);
        Assert.Equal([ModernMcpProcessDriver.CurrentProtocolVersion], supported.EnumerateArray().Select(static value => value.GetString()));
    }



    private static async Task AssertNoNativeActionsAsync(ModernMcpProcessDriver driver)
    {
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }

    private static async Task AssertNoNativeActionsAsync(ModernMcpFirstWireDriver driver)
    {
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }
}
