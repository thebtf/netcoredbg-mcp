using System.Diagnostics;
using System.Collections.Concurrent;
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

    [Fact]
    public async Task LegacyCompat_Attach_OrdersRequestsAndReportsUnconfirmedDetach()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: true, SuppressLifecycleEvents: true)));
        using var target = driver.StartOwnedTarget();
        var owner = new JsonObject { ["muxSessionId"] = "attach-owner" };
        var other = new JsonObject { ["muxSessionId"] = "attach-other" };
        try
        {
            var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(new JsonObject(), new RequestId("attach-catalog")));
            var descriptor = Assert.Single(Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
                item => item["name"]?.GetValue<string>() == "attach_debug");
            Assert.Contains("process_id", Assert.IsType<JsonArray>(descriptor["inputSchema"]?["required"]).Select(static item => item?.GetValue<string>()));
            var source = Path.GetFullPath(BreakpointSourcePath);
            JsonObject Group() => new()
            {
                ["name"] = "attach-lifecycle",
                ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2 }),
                ["tracepoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 6, ["expression"] = "1" }),
            };
            Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "instrumentation_group_create", Group(), owner))["data"]?["status"]?.GetValue<string>());
            var attached = await LegacyConsoleCallAsync(driver, "attach_debug", new JsonObject { ["process_id"] = target.Id }, owner);
            Assert.True(attached["data"]?["success"]?.GetValue<bool>());
            Assert.Equal(target.Id, attached["data"]?["processId"]?.GetValue<int>());
            var denied = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var stopped = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
            Assert.Contains("unconfirmed", stopped["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var reentry = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
            Assert.Contains("unconfirmed", reentry["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.False(target.HasExited);
            var actions = (await driver.ReadNativeActionsAsync()).Where(static item => item.Kind == "request").ToArray();
            var commands = actions.Select(static item => item.Command).ToArray();
            Assert.Contains("initialize", commands);
            Assert.Contains("setExceptionBreakpoints", commands);
            Assert.Contains("attach", commands);
            Assert.Contains("configurationDone", commands);
            Assert.True(Array.IndexOf(commands, "initialize") < Array.IndexOf(commands, "setExceptionBreakpoints"));
            Assert.True(Array.IndexOf(commands, "setExceptionBreakpoints") < Array.IndexOf(commands, "attach"));
            Assert.True(Array.IndexOf(commands, "attach") < Array.IndexOf(commands, "configurationDone"));
            Assert.DoesNotContain("terminate", commands);
            var attach = JsonNode.Parse(Assert.Single(actions, static item => item.Command == "attach").Detail!)!;
            Assert.Equal(target.Id, attach["arguments"]?["processId"]?.GetValue<int>());
            Assert.False(attach["arguments"]?["justMyCode"]?.GetValue<bool>());
            var disconnect = JsonNode.Parse(Assert.Single(actions, static item => item.Command == "disconnect").Detail!)!;
            Assert.False(disconnect["arguments"]?["terminateDebuggee"]?.GetValue<bool>());
            await FixtureProcess.StopOwnedTargetAsync(target);
            var drained = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
            Assert.Contains("unconfirmed", drained["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.Equal("FAIL", (await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "attach-lifecycle" }, owner))["data"]?["status"]?.GetValue<string>());
            Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "instrumentation_group_create", Group(), owner))["data"]?["status"]?.GetValue<string>());
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
        }
        finally
        {
            await FixtureProcess.StopOwnedTargetAsync(target);
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Attach_TerminateFallbackRetainsOwnerUntilOwnedTargetExits()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, SuppressLifecycleEvents: true)));
        using var target = driver.StartOwnedTarget();
        var owner = new JsonObject { ["muxSessionId"] = "attach-fallback-owner" };
        var other = new JsonObject { ["muxSessionId"] = "attach-fallback-other" };
        try
        {
            var attached = await LegacyConsoleCallAsync(driver, "attach_debug", new JsonObject { ["process_id"] = target.Id }, owner);
            Assert.True(attached["data"]?["success"]?.GetValue<bool>());
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var fallback = await LegacyConsoleCallAsync(driver, "terminate_debug", new JsonObject(), owner, TimeSpan.FromSeconds(15));
            Assert.Contains("unconfirmed", fallback["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.False(target.HasExited);
            var denied = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static item => item.Kind == "request").ToArray();
            Assert.DoesNotContain(actions, static item => item.Command == "terminate");
            var disconnects = actions.Where(static item => item.Command == "disconnect").ToArray();
            Assert.True(disconnects.Length <= 1);
            if (disconnects.Length == 1)
                Assert.False(JsonNode.Parse(disconnects[0].Detail!)?["arguments"]?["terminateDebuggee"]?.GetValue<bool>());
            await FixtureProcess.StopOwnedTargetAsync(target);
            _ = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
        }
        finally
        {
            await FixtureProcess.StopOwnedTargetAsync(target);
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_Attach_WithoutConfigurationDoneCapabilitySkipsRequest(bool omitInitializeBody)
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsConfigurationDone: false, OmitInitializeResponseBody: omitInitializeBody, SuppressLifecycleEvents: true)));
        using var target = driver.StartOwnedTarget();
        var owner = new JsonObject { ["muxSessionId"] = "attach-no-config-owner" };
        try
        {
            var attached = await LegacyConsoleCallAsync(driver, "attach_debug", new JsonObject { ["process_id"] = target.Id }, owner);
            Assert.True(attached["data"]?["success"]?.GetValue<bool>());
            Assert.Equal(target.Id, attached["data"]?["processId"]?.GetValue<int>());
            var actions = (await driver.ReadNativeActionsAsync()).Where(static item => item.Kind == "request").ToArray();
            Assert.Single(actions, static item => item.Command == "attach");
            Assert.DoesNotContain(actions, static item => item.Command == "configurationDone");
            Assert.False(target.HasExited);
        }
        finally
        {
            await FixtureProcess.StopOwnedTargetAsync(target);
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_AttachRefused_DoesNotPublishOrTerminateTarget()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: true, SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = "{\"attachRefused\":true}" }));
        using var target = driver.StartOwnedTarget();
        var owner = new JsonObject { ["muxSessionId"] = "attach-refusal-owner" };
        try
        {
            var result = await LegacyConsoleCallAsync(driver, "attach_debug", new JsonObject { ["process_id"] = target.Id }, owner);
            Assert.False(string.IsNullOrWhiteSpace(result["error"]?.GetValue<string>()));
            Assert.NotEqual("running", result["state"]?.GetValue<string>());
            Assert.Null(result["data"]);
            Assert.False(target.HasExited);
            var actions = await driver.ReadNativeActionsAsync();
            Assert.Contains(actions, static item => item.Command == "attach");
            Assert.DoesNotContain(actions, static item => item.Command == "terminate");
            var disconnect = JsonNode.Parse(Assert.Single(actions, static item => item.Command == "disconnect").Detail!)!;
            Assert.False(disconnect["arguments"]?["terminateDebuggee"]?.GetValue<bool>());
        }
        finally
        {
            await FixtureProcess.StopOwnedTargetAsync(target);
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

#pragma warning disable MCP9005 // These tests exercise supported initialize-era roots.
    [Fact]
    public async Task LegacyCompat_ProjectSearch_UsesCurrentStateAndPythonEnvelope()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var root = Path.Combine(scratch, "project");
            Directory.CreateDirectory(root);
            await File.WriteAllTextAsync(Path.Combine(root, "Alpha.cs"), "public class VisibleProbe\n{\n    public VisibleProbe Next;\n}\n");
            Directory.CreateDirectory(Path.Combine(root, "Nested"));
            Directory.CreateDirectory(Path.Combine(root, "ignored"));
            await File.WriteAllTextAsync(Path.Combine(root, ".gitignore"), "ignored/\n");
            await File.WriteAllTextAsync(Path.Combine(root, "Nested", "Search.cs"), "first line\n    SearchNeedle42;   \n  SearchNeedle99;\n");
            await File.WriteAllTextAsync(Path.Combine(root, "View.xaml"), "  <Text>SearchNeedle17</Text>  \n");
            await File.WriteAllTextAsync(Path.Combine(root, "ignored", "Hidden.cs"), "SearchNeedle88\n");
            await File.WriteAllTextAsync(Path.Combine(root, "Long.cs"), new string('a', 4096) + "!\n");
            await File.WriteAllTextAsync(Path.Combine(scratch, "OutsideSearch.cs"), "SearchNeedle77\n");
            await File.WriteAllTextAsync(Path.Combine(scratch, "Outside.cs"), "outside-project-secret");
            var rootsCalls = 0;
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = root, ["MCP_PROJECT_ROOT"] = null },
                RootsHandler: _ => { Interlocked.Increment(ref rootsCalls); return ValueTask.FromResult(new ListRootsResult { Roots = [new Root { Uri = new Uri(scratch).AbsoluteUri }] }); }),
                startupTimeout: TimeSpan.FromSeconds(10));
            var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(new JsonObject(), new RequestId("legacy-search-list")));
            var names = Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => item!["name"]!.GetValue<string>()).ToArray();
            foreach (var name in new[] { "find_code_symbol", "find_code_references", "get_source_context", "search_source" }) Assert.Contains(name, names);
            var owner = new JsonObject { ["muxSessionId"] = "legacy-search-owner" };

            async Task<JsonObject> SearchAsync(string name, JsonObject arguments, string state)
            {
                var response = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(name, arguments, owner,
                    new RequestId($"legacy-search-{Guid.NewGuid():N}")));
                Assert.False(response["isError"]?.GetValue<bool>() ?? false);
                var payload = Assert.IsType<JsonObject>(response["structuredContent"]);
                var text = Assert.IsType<JsonArray>(response["content"])[0]?["text"]?.GetValue<string>();
                Assert.True(JsonNode.DeepEquals(payload, JsonNode.Parse(Assert.IsType<string>(text))));
                Assert.Equal(state, payload["state"]?.GetValue<string>());
                Assert.Equal(new[] { "find_code_symbol", "find_code_references", "get_source_context", "search_source" },
                    Assert.IsType<JsonArray>(payload["next_actions"]).Select(static item => item!.GetValue<string>()));
                return payload;
            }

            async Task CheckAsync(string state)
            {
                var symbols = await SearchAsync("find_code_symbol", new JsonObject { ["name"] = "VisibleProbe", ["kind"] = "class" }, state);
                Assert.Equal(root, symbols["data"]?["project_root"]?.GetValue<string>());
                Assert.Equal("Alpha.cs", Assert.Single(Assert.IsType<JsonArray>(symbols["data"]?["results"]))?["file"]?.GetValue<string>());
                var references = await SearchAsync("find_code_references", new JsonObject { ["name"] = "VisibleProbe" }, state);
                Assert.Equal(2, references["data"]?["count"]?.GetValue<int>());
                var context = await SearchAsync("get_source_context", new JsonObject { ["file"] = "Alpha.cs", ["line"] = 2, ["radius"] = 1 }, state);
                Assert.Equal(3, Assert.IsType<JsonArray>(context["data"]?["lines"]).Count);
                var search = await SearchAsync("search_source", new JsonObject { ["pattern"] = "SearchNeedle[0-9]+" }, state);
                var searchData = Assert.IsType<JsonObject>(search["data"]);
                Assert.Equal(root, searchData["project_root"]?.GetValue<string>());
                Assert.Equal(3, searchData["count"]?.GetValue<int>());
                var matches = Assert.IsType<JsonArray>(searchData["results"]);
                Assert.Equal(3, matches.Count);
                Assert.Contains(matches, item => item?["file"]?.GetValue<string>() == "Nested/Search.cs"
                    && item?["line"]?.GetValue<int>() == 2 && item?["context"]?.GetValue<string>() == "SearchNeedle42;");
                Assert.Contains(matches, item => item?["file"]?.GetValue<string>() == "Nested/Search.cs"
                    && item?["line"]?.GetValue<int>() == 3 && item?["context"]?.GetValue<string>() == "SearchNeedle99;");
                Assert.Contains(matches, item => item?["file"]?.GetValue<string>() == "View.xaml"
                    && item?["line"]?.GetValue<int>() == 1 && item?["context"]?.GetValue<string>() == "<Text>SearchNeedle17</Text>");

                var globbed = await SearchAsync("search_source", new JsonObject { ["pattern"] = "SearchNeedle[0-9]+", ["file_glob"] = "*.xaml" }, state);
                Assert.Equal(1, globbed["data"]?["count"]?.GetValue<int>());
                Assert.Equal("View.xaml", Assert.Single(Assert.IsType<JsonArray>(globbed["data"]?["results"]))?["file"]?.GetValue<string>());

                var limited = await SearchAsync("search_source", new JsonObject { ["pattern"] = "SearchNeedle[0-9]+", ["max_results"] = 1 }, state);
                Assert.Equal(1, limited["data"]?["count"]?.GetValue<int>());
                Assert.Single(Assert.IsType<JsonArray>(limited["data"]?["results"]));
                var caseSensitive = await SearchAsync("search_source", new JsonObject { ["pattern"] = "searchneedle42" }, state);
                Assert.Equal(0, caseSensitive["data"]?["count"]?.GetValue<int>());
                var timedOut = await SearchAsync("search_source", new JsonObject
                {
                    ["pattern"] = "^(a+)+$",
                    ["file_glob"] = "Long.cs",
                    ["timeout_seconds"] = 0.01,
                }, state);
                Assert.NotNull(timedOut["error"]);
                Assert.False(timedOut.ContainsKey("data"));


                foreach (var arguments in new[]
                {
                    new JsonObject { ["pattern"] = "[" },
                    new JsonObject { ["pattern"] = "" },
                    new JsonObject { ["pattern"] = "SearchNeedle", ["max_results"] = 0 },
                    new JsonObject { ["pattern"] = "SearchNeedle", ["timeout_seconds"] = 0 },
                })
                {
                    var invalid = await SearchAsync("search_source", arguments, state);
                    Assert.NotNull(invalid["error"]);
                    Assert.False(invalid.ContainsKey("data"));
                }
                var outside = await SearchAsync("get_source_context", new JsonObject { ["file"] = "../Outside.cs", ["line"] = 1 }, state);
                Assert.NotNull(outside["error"]);
                Assert.False(outside.ContainsKey("data"));
                Assert.DoesNotContain("outside-project-secret", outside.ToJsonString(), StringComparison.Ordinal);
            }

            await CheckAsync("idle");
            var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner);
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            try
            {
                using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await driver.WaitForFixtureEventAsync("stopped", observation.Token);
                _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
                await CheckAsync("stopped");
                Assert.Equal(0, rootsCalls);
            }
            finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task LegacyCompat_ProjectSearch_ResolvesClientRootsPerRequestAndInvalidPinFailsClosed()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var first = Path.Combine(scratch, "first");
            var second = Path.Combine(scratch, "second");
            Directory.CreateDirectory(first);
            Directory.CreateDirectory(second);
            await File.WriteAllTextAsync(Path.Combine(first, "Marker.cs"), "public class FirstMarker {}\n");
            await File.WriteAllTextAsync(Path.Combine(second, "Marker.cs"), "public class SecondMarker {}\n");
            var calls = 0;
            ValueTask<ListRootsResult> Roots(CancellationToken _) => ValueTask.FromResult(new ListRootsResult
            {
                Roots = [new Root { Uri = new Uri(Interlocked.Increment(ref calls) == 1 ? first : second).AbsoluteUri }],
            });
            var env = new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null };
            await using (var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"], AdditionalEnvironment: env, RootsHandler: Roots)))
            {
                for (var index = 0; index < 2; index++)
                {
                    var response = await LegacyConsoleCallAsync(driver, "get_source_context", new JsonObject { ["file"] = "Marker.cs", ["line"] = 1, ["radius"] = 0 }, new JsonObject());
                    Assert.Equal(index == 0 ? first : second, response["data"]?["project_root"]?.GetValue<string>());
                }
                Assert.Equal(2, calls);
            }
            env["NETCOREDBG_PROJECT_ROOT"] = Path.Combine(scratch, "missing-pin");
            await using var invalid = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"], AdditionalEnvironment: env, RootsHandler: Roots));
            var rejected = await LegacyConsoleCallAsync(invalid, "find_code_symbol", new JsonObject { ["name"] = "FirstMarker" }, new JsonObject());
            Assert.NotNull(rejected["error"]);
            Assert.Equal("idle", rejected["state"]?.GetValue<string>());
            Assert.Equal(2, calls);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task LegacyCompat_ProjectSearch_DoesNotHoldLifecycleDuringClientRoots()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            await File.WriteAllTextAsync(Path.Combine(scratch, "Marker.cs"), "public class SearchMarker {}\n");
            var hold = 0;
            var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null },
                RootsHandler: async token =>
                {
                    if (Volatile.Read(ref hold) != 0)
                    {
                        entered.TrySetResult();
                        await release.Task.WaitAsync(token);
                    }
                    return new ListRootsResult { Roots = [new Root { Uri = new Uri(scratch).AbsoluteUri }] };
                }));
            var owner = new JsonObject { ["muxSessionId"] = "search-concurrency-owner" };
            var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner);
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            Task<JsonObject>? search = null;
            Task<JsonObject>? stop = null;
            try
            {
                using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await driver.WaitForFixtureEventAsync("stopped", observation.Token);
                _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
                Volatile.Write(ref hold, 1);
                search = LegacyConsoleCallAsync(driver, "find_code_symbol", new JsonObject { ["name"] = "SearchMarker" }, owner);
                await entered.Task.WaitAsync(TimeSpan.FromSeconds(10));
                stop = LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
                var finished = await Task.WhenAny(stop, Task.Delay(TimeSpan.FromSeconds(1)));
                Assert.Same(stop, finished);
                Assert.True((await stop)["data"]?["success"]?.GetValue<bool>());
                release.TrySetResult();
                var result = await search.WaitAsync(TimeSpan.FromSeconds(10));
                Assert.Equal("idle", result["state"]?.GetValue<string>());
                Assert.Equal(1, result["data"]?["count"]?.GetValue<int>());
            }
            finally
            {
                release.TrySetResult();
                if (search is not null) await Record.ExceptionAsync(async () => await search.WaitAsync(TimeSpan.FromSeconds(10)));
                if (stop is not null) await Record.ExceptionAsync(async () => await stop.WaitAsync(TimeSpan.FromSeconds(10)));
                else await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
            }
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task LegacyCompat_Freshness_UnownedReadDoesNotProtectFutureOwner()
    {
        if (!OperatingSystem.IsWindows()) return;
        var root = ModernMcpScratchDirectory.Create();
        var entered = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        try
        {
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["NETCOREDBG_PROJECT_ROOT"] = null,
                    ["MCP_PROJECT_ROOT"] = null,
                    ["NETCOREDBG_SESSION_TIMEOUT"] = "0",
                },
                RootsHandler: async token =>
                {
                    entered.TrySetResult();
                    await release.Task.WaitAsync(token);
                    return new ListRootsResult { Roots = [new Root { Uri = new Uri(root).AbsoluteUri }] };
                }));
            using var target = driver.StartOwnedTarget();
            var original = new JsonObject { ["muxSessionId"] = "freshness-original" };
            var reader = new JsonObject { ["muxSessionId"] = "freshness-reader" };
            var first = new JsonObject { ["muxSessionId"] = "freshness-first-owner" };
            var second = new JsonObject { ["muxSessionId"] = "freshness-second-owner" };
            Task<JsonObject>? pending = null;
            JsonObject? firstResult = null, secondResult = null, readResult = null;
            try
            {
                Assert.True((await LegacyConsoleCallAsync(driver, "attach_debug", new JsonObject { ["process_id"] = target.Id }, original))["data"]?["success"]?.GetValue<bool>());
                pending = LegacyConsoleCallAsync(driver, "verify_debug_freshness", new JsonObject(), reader);
                await entered.Task.WaitAsync(TimeSpan.FromSeconds(10));
                firstResult = await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "first" }, first);
                secondResult = await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "second" }, second);
                release.TrySetResult();
                readResult = await pending.WaitAsync(TimeSpan.FromSeconds(10));
            }
            finally
            {
                release.TrySetResult();
                if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
                await FixtureProcess.StopOwnedTargetAsync(target);
                await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), original));
                await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), original));
            }
            Assert.Equal("PASS", firstResult?["data"]?["status"]?.GetValue<string>());
            Assert.Equal("PASS", secondResult?["data"]?["status"]?.GetValue<string>());
            Assert.Contains("owned by another agent", readResult?["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(root); }
    }

#pragma warning restore MCP9005

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
    public async Task LegacyCompat_IdleDebugState_ContainsTheIdleResourceStateAndAvailableActions()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var state = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), new JsonObject());
        var data = Assert.IsType<JsonObject>(state["data"]);
        Assert.Equal("idle", data["state"]?.GetValue<string>());
        Assert.False(data["debuggeeAlive"]?.GetValue<bool>() ?? true);
        Assert.Equal(0, data["threadCount"]?.GetValue<int>());
        Assert.Empty(Assert.IsType<JsonArray>(data["threads"]));
        Assert.Empty(Assert.IsType<JsonArray>(data["modules"]));
        Assert.Empty(Assert.IsType<JsonObject>(data["loadedSources"]));
        Assert.Empty(Assert.IsType<JsonObject>(data["activeProgress"]));
        var expectedActions = OperatingSystem.IsWindows()
            ? new[] { "start_debug", "attach_debug", "get_progress" }
            : new[] { "start_debug", "get_progress" };
        Assert.Equal(expectedActions, Assert.IsType<JsonArray>(state["next_actions"]).Select(static item => item?.GetValue<string>()));

        var resource = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync(
            "resources/read", new JsonObject { ["uri"] = "debug://state" }, new RequestId("idle-state-resource")));
        var resourceState = JsonNode.Parse(resource["contents"]![0]!["text"]!.GetValue<string>());
        Assert.True(JsonNode.DeepEquals(data, resourceState));
    }

    [Theory]
    [InlineData("get_modules", "modules")]
    [InlineData("get_progress", "progress")]
    public async Task LegacyCompat_IdleInventories_ReturnEmptySnapshots(string tool, string field)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var response = await LegacyConsoleCallAsync(driver, tool, new JsonObject(), new JsonObject());
        Assert.Null(response["error"]);
        Assert.Equal("idle", response["state"]?.GetValue<string>());
        Assert.Equal(0, response["data"]?["count"]?.GetValue<int>());
        Assert.Empty(Assert.IsType<JsonArray>(response["data"]?[field]));
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
        Assert.Contains("UI may be unresponsive", state["message"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        var stoppedActions = Assert.IsType<JsonArray>(state["next_actions"]).Select(static item => item?.GetValue<string>()).ToArray();
        Assert.Contains("get_scopes", stoppedActions);
        Assert.Contains("get_variables", stoppedActions);
        Assert.DoesNotContain("ui_click", stoppedActions);

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
        var idleAfterStop = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), meta);
        Assert.False(idleAfterStop["data"]?["debuggeeAlive"]?.GetValue<bool>() ?? true);
        foreach (var (tool, field) in new[] { ("get_modules", "modules"), ("get_progress", "progress") })
        {
            var inventory = await LegacyConsoleCallAsync(driver, tool, new JsonObject(), meta);
            Assert.Null(inventory["error"]);
            Assert.Equal(0, inventory["data"]?["count"]?.GetValue<int>());
            Assert.Empty(Assert.IsType<JsonArray>(inventory["data"]?[field]));
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_RestartWithoutRebuild_RetainsLaunchConfigurationAndOrdersDisconnect(bool stopFirst)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true } }.ToJsonString(),
            }));
        var owner = new JsonObject { ["muxSessionId"] = "restart-owner" };
        var other = new JsonObject { ["muxSessionId"] = "restart-other" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Restart" }, owner))["data"]);
        var cwd = Path.GetDirectoryName(driver.InertProgramPath)!;
        var launch = new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["cwd"] = cwd,
            ["args"] = new JsonArray("first argument", "second"),
            ["env"] = new JsonObject { ["RESTART_MARKER"] = "retained" },
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var denied = await LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject { ["rebuild"] = false }, other);
        Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        if (stopFirst)
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());

        var restarted = await LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject { ["rebuild"] = false }, owner, TimeSpan.FromSeconds(30));
        Assert.True(restarted["data"]?["success"]?.GetValue<bool>());
        Assert.Equal(driver.InertProgramPath, restarted["data"]?["program"]?.GetValue<string>());
        var actions = await driver.ReadNativeActionsAsync();
        var launches = actions.Where(static action => action.Command == "launch").ToArray();
        Assert.Equal(2, launches.Length);
        var first = JsonNode.Parse(launches[0].Detail!)!["arguments"];
        var second = JsonNode.Parse(launches[1].Detail!)!["arguments"];
        foreach (var key in new[] { "program", "cwd", "args", "stopAtEntry", "justMyCode" })
            Assert.True(JsonNode.DeepEquals(first?[key], second?[key]));
        Assert.Equal(first?["env"]?["RESTART_MARKER"]?.GetValue<string>(), second?["env"]?["RESTART_MARKER"]?.GetValue<string>());
        Assert.Equal(cwd, second?["cwd"]?.GetValue<string>());
        Assert.Equal("first argument", second?["args"]?[0]?.GetValue<string>());
        Assert.Equal("retained", second?["env"]?["RESTART_MARKER"]?.GetValue<string>());
        var functionRequests = actions.Where(static action => action.Command == "setFunctionBreakpoints").ToArray();
        Assert.Equal(2, functionRequests.Length);
        foreach (var request in functionRequests)
            Assert.Equal("Example.Restart", JsonNode.Parse(request.Detail!)?["arguments"]?["breakpoints"]?[0]?["name"]?.GetValue<string>());
        var lifecycle = actions.Where(static action => action.Command is "launch" or "disconnect").Select(static action => action.Command).ToArray();
        Assert.Equal(new[] { "launch", "disconnect", "launch" }, lifecycle);
        Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_BuildDiagnostics_BeforeBuildReportsNoBuild()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var result = await LegacyConsoleCallAsync(driver, "get_build_diagnostics", new JsonObject(), new JsonObject());
        Assert.Null(result["error"]);
        Assert.Equal("idle", result["state"]?.GetValue<string>());
        Assert.Equal("No build has been performed yet.", result["data"]?["message"]?.GetValue<string>());
    }

    [Fact]
    public async Task LegacyCompat_BuildDiagnostics_RetainsLatestCompletedBuildAndFiltersWarnings()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var project = Path.Combine(scratch, "BuildProbe.csproj");
            var source = Path.Combine(scratch, "Program.cs");
            var output = Path.Combine(scratch, "bin", "Debug", "net8.0", "BuildProbe.dll");
            await File.WriteAllTextAsync(project, """<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>""");
            await File.WriteAllTextAsync(Path.Combine(scratch, "NuGet.Config"), """<configuration><packageSources><clear /></packageSources></configuration>""");
            await File.WriteAllTextAsync(source, "#warning diagnostic-success-probe\nSystem.Console.WriteLine(\"build-probe\");\n");
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"], StartupDirectory: scratch,
                FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = scratch, ["MCP_PROJECT_ROOT"] = null }),
                startupTimeout: TimeSpan.FromSeconds(10));
            var owner = new JsonObject { ["muxSessionId"] = "build-diagnostics-owner" };
            try
            {
                var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
                {
                    ["program"] = output,
                    ["pre_build"] = true,
                    ["build_project"] = project,
                    ["build_configuration"] = "Debug",
                    ["stop_at_entry"] = true,
                }, owner, TimeSpan.FromSeconds(90));
                Assert.True(started["data"]?["success"]?.GetValue<bool>());
                Assert.True(File.Exists(output));

                var full = await LegacyConsoleCallAsync(driver, "get_build_diagnostics", new JsonObject(), owner);
                Assert.Null(full["error"]);
                var success = Assert.IsType<JsonObject>(full["data"]);
                Assert.True(success["success"]?.GetValue<bool>());
                Assert.Equal("ready", success["state"]?.GetValue<string>());
                Assert.Equal("build", success["command"]?.GetValue<string>());
                Assert.Equal(project, success["projectPath"]?.GetValue<string>());
                Assert.Equal("Debug", success["configuration"]?.GetValue<string>());
                Assert.Equal(0, success["exitCode"]?.GetValue<int>());
                Assert.Equal(0, success["errorCount"]?.GetValue<int>());
                var warningCount = Assert.IsType<int>(success["warningCount"]?.GetValue<int>());
                Assert.True(warningCount > 0);
                Assert.Contains(Assert.IsType<JsonArray>(success["diagnostics"]), item =>
                    item?["severity"]?.GetValue<string>() == "warning" && item?["code"]?.GetValue<string>() == "CS1030");

                var filtered = await LegacyConsoleCallAsync(driver, "get_build_diagnostics", new JsonObject { ["include_warnings"] = false }, owner);
                Assert.Null(filtered["error"]);
                var filteredData = Assert.IsType<JsonObject>(filtered["data"]);
                Assert.Equal(warningCount, filteredData["warningCount"]?.GetValue<int>());
                Assert.False(filteredData.ContainsKey("diagnostics"));
                Assert.NotNull(filteredData["warningHint"]);
                Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());

                await File.WriteAllTextAsync(source, "#error diagnostic-failure-probe\nSystem.Console.WriteLine(\"build-probe\");\n");
                var failed = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
                {
                    ["program"] = output,
                    ["pre_build"] = true,
                    ["build_project"] = project,
                    ["build_configuration"] = "Release",
                    ["stop_at_entry"] = true,
                }, owner, TimeSpan.FromSeconds(90));
                Assert.NotNull(failed["error"]);
                var latest = await LegacyConsoleCallAsync(driver, "get_build_diagnostics", new JsonObject(), owner);
                Assert.Null(latest["error"]);
                var failure = Assert.IsType<JsonObject>(latest["data"]);
                Assert.False(failure["success"]?.GetValue<bool>() ?? true);
                Assert.Equal("failed", failure["state"]?.GetValue<string>());
                Assert.Equal("build", failure["command"]?.GetValue<string>());
                Assert.Equal(project, failure["projectPath"]?.GetValue<string>());
                Assert.Equal("Release", failure["configuration"]?.GetValue<string>());
                Assert.NotEqual(0, Assert.IsType<int>(failure["exitCode"]?.GetValue<int>()));
                Assert.True(failure["errorCount"]?.GetValue<int>() > 0);
                Assert.Equal(0, failure["warningCount"]?.GetValue<int>());
                var diagnostics = Assert.IsType<JsonArray>(failure["diagnostics"]);
                Assert.Contains(diagnostics, item => item?["severity"]?.GetValue<string>() == "error"
                    && item?["code"]?.GetValue<string>() == "CS1029" && item?["file"]?.GetValue<string>() == source
                    && item?["line"]?.GetValue<int>() == 1);
                Assert.DoesNotContain(diagnostics, item => item?["code"]?.GetValue<string>() == "CS1030");
            }
            finally
            {
                await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner, TimeSpan.FromSeconds(15)));
            }
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task LegacyCompat_BuildDiagnostics_OversizedWarningsReturnErrorWithoutPartialData()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var project = Path.Combine(scratch, "BuildProbe.csproj");
            var output = Path.Combine(scratch, "bin", "Debug", "net8.0", "BuildProbe.dll");
            await File.WriteAllTextAsync(project, """<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>""");
            await File.WriteAllTextAsync(Path.Combine(scratch, "NuGet.Config"), """<configuration><packageSources><clear /></packageSources></configuration>""");
            var warningText = new string('w', 256);
            await File.WriteAllTextAsync(Path.Combine(scratch, "Program.cs"),
                string.Concat(Enumerable.Range(0, 1500).Select(index => $"#warning {index:D4}-{warningText}\n"))
                + "System.Console.WriteLine(\"build-probe\");\n");
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"], StartupDirectory: scratch,
                FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = scratch, ["MCP_PROJECT_ROOT"] = null }),
                startupTimeout: TimeSpan.FromSeconds(10));
            var owner = new JsonObject { ["muxSessionId"] = "build-diagnostics-size-owner" };
            try
            {
                var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
                {
                    ["program"] = output,
                    ["pre_build"] = true,
                    ["build_project"] = project,
                    ["build_configuration"] = "Debug",
                    ["stop_at_entry"] = true,
                }, owner, TimeSpan.FromSeconds(90));
                Assert.True(started["data"]?["success"]?.GetValue<bool>());
                Assert.True(File.Exists(output));
                var result = await LegacyConsoleCallAsync(driver, "get_build_diagnostics", new JsonObject(), owner, TimeSpan.FromSeconds(30));
                Assert.False(string.IsNullOrWhiteSpace(result["error"]?.GetValue<string>()));
                Assert.False(result.ContainsKey("data"));
            }
            finally
            {
                await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner, TimeSpan.FromSeconds(15)));
            }
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task LegacyCompat_RestartWithoutRebuild_PreservesSavedPreBuildAndRefusesCompilerFailure()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var project = Path.Combine(scratch, "BuildProbe.csproj");
            var source = Path.Combine(scratch, "Program.cs");
            var output = Path.Combine(scratch, "bin", "Debug", "net8.0", "BuildProbe.dll");
            await File.WriteAllTextAsync(project, """<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework></PropertyGroup></Project>""");
            await File.WriteAllTextAsync(Path.Combine(scratch, "NuGet.Config"), """<configuration><packageSources><clear /></packageSources></configuration>""");
            await File.WriteAllTextAsync(source, "System.Console.WriteLine(\"build-probe\");\n");
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"], StartupDirectory: scratch,
                FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = scratch, ["MCP_PROJECT_ROOT"] = null }),
                startupTimeout: TimeSpan.FromSeconds(10));
            var owner = new JsonObject { ["muxSessionId"] = "restart-prebuild-owner" };
            try
            {
                var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
                {
                    ["program"] = output,
                    ["pre_build"] = true,
                    ["build_project"] = project,
                    ["build_configuration"] = "Debug",
                    ["stop_at_entry"] = true,
                }, owner, TimeSpan.FromSeconds(90));
                Assert.True(started["data"]?["success"]?.GetValue<bool>());
                Assert.True(File.Exists(output));
                await File.WriteAllTextAsync(source, "public static class Program { public static void Main() { int value = ; } }\n");
                var restarted = await LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject { ["rebuild"] = false }, owner, TimeSpan.FromSeconds(90));
                Assert.Equal("START_DEBUG_BUILD_FAILED", restarted["error"]?.GetValue<string>());
                Assert.Equal("idle", restarted["state"]?.GetValue<string>());
                Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "launch");
            }
            finally
            {
                await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner, TimeSpan.FromSeconds(15)));
            }
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task LegacyCompat_RestartDefaultRebuild_RefusesMissingConfigurationOrProjectWithoutStopping()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop")));
        var owner = new JsonObject { ["muxSessionId"] = "restart-validation-owner" };
        var missing = await LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject(), owner);
        Assert.Contains("No previous launch configuration", missing["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        Assert.Empty(await driver.ReadNativeActionsAsync());
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var refused = await LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject(), owner);
        Assert.Contains("no build_project", refused["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        Assert.Equal("stopped", refused["state"]?.GetValue<string>());
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        Assert.Equal("stopped", (await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>());
        Assert.True((await LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject { ["rebuild"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_TerminateUnsupported_DisconnectsAndDeniesForeignOwner()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop")));
        var owner = new JsonObject { ["muxSessionId"] = "terminate-owner" };
        var other = new JsonObject { ["muxSessionId"] = "terminate-other" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var denied = await LegacyConsoleCallAsync(driver, "terminate_debug", new JsonObject(), other);
        Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        var result = await LegacyConsoleCallAsync(driver, "terminate_debug", new JsonObject(), owner);
        Assert.Null(result["error"]);
        Assert.Equal("idle", result["state"]?.GetValue<string>());
        Assert.Equal("idle", result["data"]?["state"]?.GetValue<string>());
        Assert.Contains("disconnect", result["message"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        var actions = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
        Assert.DoesNotContain(actions, static action => action.Command == "terminate");
        Assert.Single(actions, static action => action.Command == "disconnect");
        var recorded = Assert.Single(driver.Requests, static request => request.Parameters?["name"]?.GetValue<string>() == "terminate_debug"
            && request.Parameters?["_meta"]?["muxSessionId"]?.GetValue<string>() == "terminate-owner");
        Assert.True(recorded.StartedOrder > 0);
        Assert.True(recorded.CompletedOrder > recorded.StartedOrder);
        Assert.NotNull(recorded.Result);
    }

    [Fact]
    public async Task LegacyCompat_TerminateSupported_WaitsForObservedTerminalEvent()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: true, LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_TERMINATE_MODE"] = "held-terminal" }));
        var owner = new JsonObject { ["muxSessionId"] = "terminate-event-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var terminate = LegacyConsoleCallAsync(driver, "terminate_debug", new JsonObject(), owner, TimeSpan.FromSeconds(25));
        try
        {
            await driver.WaitForFixtureRecordAsync("terminate-acknowledged", observation.Token);
            Assert.False(terminate.IsCompleted, "A DAP acknowledgement is not a terminal event.");
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            Assert.Single(actions, static action => action.Command == "terminate");
            Assert.DoesNotContain(actions, static action => action.Command == "disconnect");
            driver.ReleaseNextResponse();
            var result = await terminate.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.Null(result["error"]);
            Assert.Equal("terminated", result["state"]?.GetValue<string>());
            Assert.Equal("terminated", result["data"]?["state"]?.GetValue<string>());
            Assert.Equal("terminated", (await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleaseNextResponse();
            await Record.ExceptionAsync(async () => await terminate.WaitAsync(TimeSpan.FromSeconds(10)));
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_TerminateRefused_ReportsTimeoutWithoutClaimingTerminationOrDisconnect()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: true, LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_TERMINATE_MODE"] = "refuse" }));
        var owner = new JsonObject { ["muxSessionId"] = "terminate-refusal-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var result = await LegacyConsoleCallAsync(driver, "terminate_debug", new JsonObject(), owner, TimeSpan.FromSeconds(20));
        Assert.Null(result["error"]);
        Assert.Equal("stopped", result["state"]?.GetValue<string>());
        Assert.Equal("stopped", result["data"]?["state"]?.GetValue<string>());
        Assert.Contains("did not exit", result["message"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        var actions = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
        Assert.Single(actions, static action => action.Command == "terminate");
        Assert.DoesNotContain(actions, static action => action.Command == "disconnect");
        await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
    }

    [Fact]
    public async Task LegacyCompat_TerminateAcknowledgedTimeout_PreservesStoppedInspectionAndExecution()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: true, LifecycleMode: "all-stop")));
        var owner = new JsonObject { ["muxSessionId"] = "terminate-timeout-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var result = await LegacyConsoleCallAsync(driver, "terminate_debug", new JsonObject(), owner, TimeSpan.FromSeconds(20));
        Assert.Equal("stopped", result["state"]?.GetValue<string>());
        Assert.Contains("did not exit", result["message"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        var stack = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
        Assert.Null(stack["error"]);
        Assert.NotEmpty(Assert.IsType<JsonArray>(stack["data"]?["frames"]));
        var step = await LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), owner, TimeSpan.FromSeconds(35));
        Assert.Equal("stopped", step["state"]?.GetValue<string>());
        Assert.Equal("step", step["reason"]?.GetValue<string>());
        Assert.False(step["timed_out"]?.GetValue<bool>() ?? true);
        Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "terminate");
        Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
    }

    [Theory]
    [InlineData("terminal-before-ack")]
    [InlineData("exited-before-ack")]
    public async Task LegacyCompat_Terminate_ObservedTerminalPrecedesHeldAcknowledgement(string mode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: true, LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_TERMINATE_MODE"] = mode }));
        var owner = new JsonObject { ["muxSessionId"] = "terminal-before-ack-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
        var terminate = LegacyConsoleCallAsync(driver, "terminate_debug", new JsonObject(), owner, TimeSpan.FromSeconds(25));
        try
        {
            await driver.WaitForFixtureRecordAsync("terminate-terminal-before-ack", observation.Token);
            Assert.Same(terminate, await Task.WhenAny(terminate, Task.Delay(TimeSpan.FromSeconds(2))));
            var result = await terminate;
            Assert.Null(result["error"]);
            Assert.Equal("terminated", result["state"]?.GetValue<string>());
            Assert.Equal("terminated", result["data"]?["state"]?.GetValue<string>());
            Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "terminate");
        }
        finally
        {
            driver.ReleaseNextResponse();
            await Record.ExceptionAsync(async () => await terminate.WaitAsync(TimeSpan.FromSeconds(10)));
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

#pragma warning disable MCP9005 // This legacy request uses initialize-era roots.
    [Fact]
    public async Task LegacyCompat_RestartWithoutRebuild_RetainsResolvedProfileAcrossChangedRoots()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var first = Directory.CreateDirectory(Path.Combine(scratch, "first")).FullName;
            var second = Directory.CreateDirectory(Path.Combine(scratch, "second")).FullName;
            static string Profile(string value) => new JsonObject
            {
                ["defaultProfile"] = "selected",
                ["profiles"] = new JsonObject { ["selected"] = new JsonObject { ["env"] = new JsonObject { ["RESTART_PROFILE_MARKER"] = value } } },
            }.ToJsonString();
            var firstProfile = Path.Combine(first, ".netcoredbg-mcp.launch.json");
            await File.WriteAllTextAsync(firstProfile, Profile("original"));
            await File.WriteAllTextAsync(Path.Combine(second, ".netcoredbg-mcp.launch.json"), Profile("other-root"));
            var calls = 0;
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null },
                RootsHandler: _ => ValueTask.FromResult(new ListRootsResult
                {
                    Roots = [new Root { Uri = new Uri(Interlocked.Increment(ref calls) == 1 ? first : second).AbsoluteUri }],
                })));
            var owner = new JsonObject { ["muxSessionId"] = "restart-profile-owner" };
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
                ["launch_profile"] = "selected",
            }, owner))["data"]?["success"]?.GetValue<bool>());
            Assert.Equal(1, calls);
            await File.WriteAllTextAsync(firstProfile, Profile("changed"));
            var restarted = await LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject { ["rebuild"] = false }, owner, TimeSpan.FromSeconds(30));
            Assert.True(restarted["data"]?["success"]?.GetValue<bool>());
            var launches = (await driver.ReadNativeActionsAsync()).Where(static action => action.Command == "launch").ToArray();
            Assert.Equal(2, launches.Length);
            Assert.Equal("original", JsonNode.Parse(launches[0].Detail!)?["arguments"]?["env"]?["RESTART_PROFILE_MARKER"]?.GetValue<string>());
            Assert.Equal("original", JsonNode.Parse(launches[1].Detail!)?["arguments"]?["env"]?["RESTART_PROFILE_MARKER"]?.GetValue<string>());
            Assert.Equal(1, calls);
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }
#pragma warning restore MCP9005

    [Fact]
    public async Task LegacyDriver_IncompleteRestartAfterStop_DoesNotConfirmCleanup()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_HOLD_RESTART_LAUNCH"] = "1" }));
        var owner = new JsonObject { ["muxSessionId"] = "incomplete-restart-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
        var restart = LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject { ["rebuild"] = false }, owner, TimeSpan.FromSeconds(30));
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("restart-launch-response-gated", observation.Token);
            var recorded = Assert.Single(driver.Requests, static request => request.Parameters?["name"]?.GetValue<string>() == "restart_debug");
            Assert.True(recorded.StartedOrder > 0);
            Assert.Equal(0, recorded.CompletedOrder);
            var method = typeof(ModernMcpProcessDriver).GetMethod("StopKnownSessionsAsync", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
            Assert.NotNull(method);
            var cleanup = Assert.IsAssignableFrom<Task<bool>>(method.Invoke(driver, null));
            Assert.False(await cleanup);
        }
        finally
        {
            driver.ReleaseThreadsResponse();
            await Record.ExceptionAsync(async () => await restart.WaitAsync(TimeSpan.FromSeconds(15)));
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
        var completed = Assert.Single(driver.Requests, static request => request.Parameters?["name"]?.GetValue<string>() == "restart_debug");
        Assert.True(completed.CompletedOrder > completed.StartedOrder);
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
        var source = BreakpointSourcePath;
        Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["verified"]?.GetValue<bool>());
        var start = LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner, TimeSpan.FromSeconds(20));
        Task<JsonObject>? queuedStart = null;
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        try
        {
            await driver.WaitForFixtureRecordAsync("launch-response-gated", observation.Token);
            var denied = await LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), other);
            Assert.Contains("starting", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var breakpoint = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 6 }, owner);
            Assert.Contains("starting", breakpoint["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            AssertBreakpointRequest(Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "setBreakpoints"), source, [2]);
            queuedStart = LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, other, TimeSpan.FromSeconds(20));
            Assert.NotSame(queuedStart, await Task.WhenAny(queuedStart, Task.Delay(TimeSpan.FromSeconds(1))));
            var state = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), other);
            Assert.Equal("initializing", state["state"]?.GetValue<string>());
            Assert.Contains("initializing", state["message"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            foreach (var tool in new[] { "get_modules", "get_progress" })
            {
                var inventory = await LegacyConsoleCallAsync(driver, tool, new JsonObject(), other);
                Assert.NotNull(inventory["error"]);
                Assert.Null(inventory["data"]);
            }
        }
        finally
        {
            driver.ReleaseThreadsResponse();
            _ = await Record.ExceptionAsync(async () => await start);
            if (queuedStart is not null) _ = await Record.ExceptionAsync(async () => await queuedStart);
        }
        Assert.True((await start)["data"]?["success"]?.GetValue<bool>());
        Assert.NotNull(queuedStart);
        Assert.Contains("already active", (await queuedStart!)["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        var stopped = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
        Assert.False((await LegacyConsoleCallAsync(driver, "remove_breakpoint", new JsonObject { ["file"] = source, ["line"] = 6 }, owner))["data"]?["removed"]?.GetValue<bool>());
        Assert.True((await LegacyConsoleCallAsync(driver, "remove_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["removed"]?.GetValue<bool>());
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
            var malformedPause = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("tools/call", new JsonObject
            {
                ["name"] = "pause_execution",
                ["arguments"] = new JsonArray(),
                ["_meta"] = owner.DeepClone(),
            }, new RequestId("legacy-malformed-pause")));
            Assert.NotNull(ModernMcpProcessDriver.LegacyPayload(malformedPause)?["error"]);
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

    private static ModernMcpStartOptions QuickEvaluateOptions(string pauseMode = "ack-before-stopped-held") => new(
        ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
        FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true,
            PauseResponseMode: pauseMode, NextResponseMode: "continue-ack-only"));

    private static async Task StartQuickEvaluateSessionAsync(ModernMcpProcessDriver driver, JsonObject owner)
    {
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(owner, new RequestId("quick-evaluate-catalog")));
        Assert.Contains(Assert.IsType<JsonArray>(catalog["tools"]), item => item?["name"]?.GetValue<string>() == "quick_evaluate");
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        Assert.Equal("running", (await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>());
    }

    [Fact]
    public async Task LegacyCompat_RunningState_ProvidesSafeInspectionGuidance()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions());
        var owner = new JsonObject { ["muxSessionId"] = "running-guidance-owner" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        try
        {
            var state = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner);
            Assert.Equal("running", state["state"]?.GetValue<string>());
            Assert.Contains("references", state["message"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.Contains("invalid", state["message"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var actions = Assert.IsType<JsonArray>(state["next_actions"]).Select(static item => item?.GetValue<string>()).ToArray();
            Assert.Contains("get_progress", actions);
            Assert.Contains("search_output", actions);
            Assert.DoesNotContain("get_variables", actions);
        }
        finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
    }

    [Fact]
    public async Task LegacyCompat_QuickEvaluate_RunningWaitsForStopEvaluatesFrameAndResumesOnce()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions());
        var owner = new JsonObject { ["muxSessionId"] = "quick-evaluate-owner" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var pending = LegacyConsoleCallAsync(driver, "quick_evaluate", new JsonObject { ["expression"] = "tupleVar" }, owner);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", deadline.Token);
            Assert.False(pending.IsCompleted);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), a => a.Command is "stackTrace" or "evaluate" or "continue");
            driver.ReleasePauseStoppedEvent();
            var result = await pending;
            Assert.Null(result["error"]);
            Assert.Equal("(\"test\", 123)", result["data"]?["result"]?.GetValue<string>());
            Assert.Equal("(string, int)", result["data"]?["type"]?.GetValue<string>());
            Assert.Equal(1002, result["data"]?["variablesReference"]?.GetValue<int>());
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).Where(a => a.Command is "pause" or "stackTrace" or "evaluate" or "continue").ToArray();
            Assert.Equal(new[] { "pause", "stackTrace", "evaluate", "continue" }, actions.Select(a => a.Command));
            Assert.Equal(1, JsonNode.Parse(actions[1].Detail!)?["arguments"]?["threadId"]?.GetValue<int>());
            Assert.Equal(1, JsonNode.Parse(actions[2].Detail!)?["arguments"]?["frameId"]?.GetValue<int>());
            Assert.Equal("tupleVar", JsonNode.Parse(actions[2].Detail!)?["arguments"]?["expression"]?.GetValue<string>());
            Assert.Equal(1, JsonNode.Parse(actions[3].Detail!)?["arguments"]?["threadId"]?.GetValue<int>());
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        }
    }

    [Fact]
    public async Task LegacyCompat_QuickEvaluate_NegativeEvaluationReturnsErrorAndResumesOnce()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions());
        var owner = new JsonObject { ["muxSessionId"] = "quick-negative-owner" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var pending = LegacyConsoleCallAsync(driver, "quick_evaluate", new JsonObject { ["expression"] = "fixture:negative" }, owner);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", deadline.Token);
            driver.ReleasePauseStoppedEvent();
            var result = await pending;
            Assert.NotNull(result["error"]);
            Assert.Null(result["data"]);
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).Where(a => a.Command is "pause" or "stackTrace" or "evaluate" or "continue").ToArray();
            Assert.Equal(new[] { "pause", "stackTrace", "evaluate", "continue" }, actions.Select(a => a.Command));
            Assert.Equal("running", (await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        }
    }

    [Fact]
    public async Task LegacyCompat_QuickEvaluate_RejectsStoppedAndForeignOwnerWithoutDispatch()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions());
        var owner = new JsonObject { ["muxSessionId"] = "quick-owner" };
        var other = new JsonObject { ["muxSessionId"] = "quick-other" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var denied = await LegacyConsoleCallAsync(driver, "quick_evaluate", new JsonObject { ["expression"] = "6 * 7" }, other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), a => a.Command is "pause" or "evaluate" or "continue");
            Assert.True((await LegacyConsoleCallAsync(driver, "pause_execution", new JsonObject { ["thread_id"] = 1 }, owner))["data"]?["success"]?.GetValue<bool>());
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", deadline.Token);
            driver.ReleasePauseStoppedEvent();
            await driver.WaitForFixtureRecordAsync("pause-stopped", deadline.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            before = (await driver.ReadNativeActionsAsync()).Count;
            var stopped = await LegacyConsoleCallAsync(driver, "quick_evaluate", new JsonObject { ["expression"] = "6 * 7" }, owner);
            Assert.NotNull(stopped["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), a => a.Command is "pause" or "evaluate" or "continue");
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        }
    }

    [Fact]
    public async Task LegacyCompat_QuickEvaluate_BreakpointDuringPauseIsNotContinued()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions("ack-before-breakpoint-held"));
        var owner = new JsonObject { ["muxSessionId"] = "quick-breakpoint-owner" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var pending = LegacyConsoleCallAsync(driver, "quick_evaluate", new JsonObject { ["expression"] = "6 * 7" }, owner);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", deadline.Token);
            driver.ReleasePauseStoppedEvent();
            var result = await pending;
            Assert.NotNull(result["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), a => a.Command is "evaluate" or "continue");
            Assert.Equal("stopped", (await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        }
    }

    [Fact]
    public async Task LegacyCompat_QuickEvaluate_ChangedStopDuringEvaluationIsNotContinued()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions());
        var owner = new JsonObject { ["muxSessionId"] = "quick-restop-owner" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var pending = LegacyConsoleCallAsync(driver, "quick_evaluate", new JsonObject { ["expression"] = "fixture:hold-then-continued-restop" }, owner);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", deadline.Token);
            driver.ReleasePauseStoppedEvent();
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", deadline.Token);
            await driver.WaitForFixtureRecordAsync("evaluate-restopped-event", deadline.Token);
            driver.ReleaseStackTraceResponse();
            var result = await pending;
            Assert.NotNull(result["error"]);
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            Assert.Single(actions, a => a.Command == "evaluate");
            Assert.DoesNotContain(actions, a => a.Command == "continue");
            Assert.Equal("stopped", (await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>());
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            driver.ReleaseStackTraceResponse();
            await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        }
    }

    [Fact]
    public async Task LegacyCompat_QuickEvaluate_PreHeaderVetoPreservesStoppedSession()
    {
        await using var session = await NetCoreDbgSessionContractDriver.StartWithDapWriteBarrierAsync(
            new FixtureConfiguration(LifecycleMode: "all-stop", PauseResponseMode: "ack-before-stopped-held", NextResponseMode: "continue-ack-only"),
            Path.GetFullPath(BreakpointSourcePath), TimeSpan.FromSeconds(2), TimeSpan.FromSeconds(30), TimeSpan.FromMilliseconds(300), CancellationToken.None);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await session.Fixture.WaitForEventAsync("stopped", deadline.Token);
        await session.WaitForReaderBarrierAsync(deadline.Token);
        var field = typeof(NetCoreDbgSessionContractDriver).GetField("_session", System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic)!;
        var target = field.GetValue(session)!;
        var flags = System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic;
        var dispatch = target.GetType().GetMethod("DispatchMessage", flags)!;
        dispatch.Invoke(target, [JsonSerializer.SerializeToElement(new { seq = 10000, type = "event", @event = "continued", body = new { threadId = 1, allThreadsContinued = true } })]);
        var quick = target.GetType().GetMethod("QuickEvaluateAsync", flags)!;
        var operation = Assert.IsAssignableFrom<Task>(quick.Invoke(target, ["fixture:hold", null, deadline.Token]));
        try
        {
            await session.Fixture.WaitForTranscriptKindAsync("pause-stop-gated", deadline.Token);
            session.Fixture.ReleasePauseStoppedEvent();
            await session.Fixture.WaitForTranscriptKindAsync("evaluate-response-gated", deadline.Token);
            session.ArmDapWriteBeforeHeaderBarrier();
            session.Fixture.ReleaseStackTraceResponse();
            await session.WaitForDapPreHeaderWriteAsync(deadline.Token);
            target.GetType().GetMethod("VetoQuickPause", flags)!.Invoke(target, []);
            session.ReleaseDapWriteBarrier();
            await operation.WaitAsync(deadline.Token);
            var result = operation.GetType().GetProperty("Result")!.GetValue(operation)!;
            Assert.NotNull(result.GetType().GetField("Item2")!.GetValue(result));
            Assert.True(session.IsUsable);
            Assert.False(session.IsExecutionClosing);
            Assert.Equal("stopped", session.State.Event);
            Assert.True(session.CanCaptureInspectionTarget());
            Assert.DoesNotContain(await session.Fixture.ReadTranscriptAsync(), entry => entry.Kind == "request" && entry.Command == "continue");
        }
        finally
        {
            session.Fixture.ReleasePauseStoppedEvent();
            session.Fixture.ReleaseStackTraceResponse();
            session.ReleaseDapWriteBarrier();
            await Record.ExceptionAsync(async () => await operation.WaitAsync(TimeSpan.FromSeconds(10)));
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_QuickEvaluate_StopOrRestartDuringEvaluationNeverResumes(bool restart)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions());
        var owner = new JsonObject { ["muxSessionId"] = "quick-stop-owner" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        Task<JsonObject>? pending = null;
        Task<JsonObject>? stop = null;
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            pending = LegacyConsoleCallAsync(driver, "quick_evaluate", new JsonObject { ["expression"] = "fixture:hold" }, owner, TimeSpan.FromSeconds(30));
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", deadline.Token);
            driver.ReleasePauseStoppedEvent();
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", deadline.Token);
            stop = LegacyConsoleCallAsync(driver, restart ? "restart_debug" : "stop_debug",
                restart ? new JsonObject { ["rebuild"] = false } : new JsonObject(), owner, TimeSpan.FromSeconds(30));
            while ((await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>() != "unknown")
                await Task.Delay(10, deadline.Token);
            Assert.False(stop.IsCompleted);
            driver.ReleaseStackTraceResponse();
            Assert.NotNull((await pending.WaitAsync(TimeSpan.FromSeconds(10)))["error"]);
            Assert.True((await stop.WaitAsync(TimeSpan.FromSeconds(15)))["data"]?["success"]?.GetValue<bool>());
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            Assert.Single(actions, a => a.Command == "evaluate");
            Assert.DoesNotContain(actions, a => a.Command == "continue");
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            driver.ReleaseStackTraceResponse();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(15)));
            if (stop is not null) await Record.ExceptionAsync(async () => await stop.WaitAsync(TimeSpan.FromSeconds(15)));
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_QuickEvaluate_CancelledEvaluationDoesNotReplayOrContinue()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions());
        var owner = new JsonObject { ["muxSessionId"] = "quick-cancel-owner" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            using var cancellation = new CancellationTokenSource();
            var pending = driver.CallToolRawAsync("quick_evaluate", new JsonObject { ["expression"] = "fixture:hold" }, owner,
                new RequestId("quick-evaluate-cancel"), cancellation.Token, timeout: TimeSpan.FromSeconds(30));
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", deadline.Token);
            driver.ReleasePauseStoppedEvent();
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", deadline.Token);
            cancellation.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(async () => await pending);
            await driver.WaitForFixtureRecordAsync("disconnect-response", deadline.Token);
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            Assert.Single(actions, a => a.Command == "pause");
            Assert.Single(actions, a => a.Command == "evaluate");
            Assert.DoesNotContain(actions, a => a.Command == "continue");
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            driver.ReleaseStackTraceResponse();
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_QuickEvaluate_UncertainPauseIsNotRetriedOrReportedAsSuccess()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(QuickEvaluateOptions("wrong-command"));
        var owner = new JsonObject { ["muxSessionId"] = "quick-uncertain-owner" };
        await StartQuickEvaluateSessionAsync(driver, owner);
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "quick_evaluate", new JsonObject { ["expression"] = "6 * 7" }, owner, TimeSpan.FromSeconds(35));
            Assert.NotNull(result["error"]);
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            Assert.Single(actions, a => a.Command == "pause");
            Assert.DoesNotContain(actions, a => a.Command is "evaluate" or "continue");
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_OutputEvidence_IdleCheckpointDuplicateAndMissing()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var owner = new JsonObject { ["muxSessionId"] = "idle-evidence-owner" };
        var checkpoint = await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "before" }, owner);
        Assert.Equal("PASS", checkpoint["data"]?["status"]?.GetValue<string>());
        Assert.Equal("before", checkpoint["data"]?["checkpoint"]?.GetValue<string>());
        var noOutput = await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject { ["checkpoint"] = "before", ["regex"] = false }, owner);
        Assert.Equal("PASS", noOutput["data"]?["status"]?.GetValue<string>());
        Assert.Equal(1, noOutput["data"]?["searched_range"]?["start_sequence"]?.GetValue<int>());
        Assert.Null(noOutput["data"]?["searched_range"]?["end_sequence"]);
        var duplicate = await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "before" }, owner);
        Assert.Equal("FAIL", duplicate["data"]?["status"]?.GetValue<string>());
        var missing = await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject { ["checkpoint"] = "missing" }, owner);
        Assert.Equal("FAIL", missing["data"]?["status"]?.GetValue<string>());
        var empty = await LegacyConsoleCallAsync(driver, "search_output", new JsonObject { ["pattern"] = "ready" }, owner);
        Assert.Equal("ready", empty["data"]?["pattern"]?.GetValue<string>());
        Assert.Equal(0, empty["data"]?["match_count"]?.GetValue<int>());
        Assert.Empty(Assert.IsType<JsonArray>(empty["data"]?["matches"]));
        var stopped = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
        var afterStop = await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject { ["checkpoint"] = "before" }, owner);
        Assert.Equal("FAIL", afterStop["data"]?["status"]?.GetValue<string>());
        Assert.Equal("output checkpoint not found", afterStop["data"]?["reason"]?.GetValue<string>());
    }

    [Fact]
    public async Task LegacyCompat_OutputEvidence_RefusedLaunchCannotHideForbiddenOutput()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject { ["category"] = "stdout", ["output"] = "forbidden launch marker\n" }),
            ["launchRefused"] = true,
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "refused-output-owner" };
        Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "before" }, owner))["data"]?["status"]?.GetValue<string>());
        try
        {
            var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner);
            Assert.NotNull(started["error"]);
            var output = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), owner);
            Assert.Equal(string.Empty, output["data"]?["output"]?.GetValue<string>());
            var result = await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject
            {
                ["checkpoint"] = "before",
                ["forbidden"] = new JsonArray("forbidden launch marker"),
                ["regex"] = false,
            }, owner);
            Assert.Equal("FAIL", result["data"]?["status"]?.GetValue<string>());
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_OutputEvidence_PreStartCheckpointAssertionsAndStopReset()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(
                new JsonObject { ["category"] = "stdout", ["output"] = "ready [1]\nready [2]\n" },
                new JsonObject { ["category"] = "stderr", ["output"] = "warning: slow\nfatal [x]\n" }),
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "evidence-owner" };
        var foreign = new JsonObject { ["muxSessionId"] = "evidence-foreign" };
        Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "before" }, owner))["data"]?["status"]?.GetValue<string>());
        var active = false;
        try
        {
            active = true;
            var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner);
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "foreign" }, foreign))["error"]);
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject { ["checkpoint"] = "before" }, foreign))["error"]);
            Assert.Equal("FAIL", (await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "before" }, owner))["data"]?["status"]?.GetValue<string>());
            var passed = await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject
            {
                ["checkpoint"] = "before",
                ["required"] = new JsonArray("ready [", "warning: slow"),
                ["forbidden"] = new JsonArray("fatal [absent]"),
                ["regex"] = false,
                ["max_matches"] = 1,
            }, owner);
            Assert.Equal("PASS", passed["data"]?["status"]?.GetValue<string>());
            Assert.Empty(Assert.IsType<JsonArray>(passed["data"]?["missing_required"]));
            Assert.Empty(Assert.IsType<JsonArray>(passed["data"]?["forbidden_matches"]));
            var match = Assert.Single(Assert.IsType<JsonArray>(passed["data"]?["matches"]));
            Assert.Equal("ready [", match?["pattern"]?.GetValue<string>());
            Assert.Equal("ready [1]", match?["text"]?.GetValue<string>());
            var failed = await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject
            {
                ["checkpoint"] = "before",
                ["required"] = new JsonArray("missing [literal]"),
                ["forbidden"] = new JsonArray("fatal [x]"),
                ["regex"] = false,
            }, owner);
            Assert.Equal("FAIL", failed["data"]?["status"]?.GetValue<string>());
            Assert.Equal("missing [literal]", Assert.Single(Assert.IsType<JsonArray>(failed["data"]?["missing_required"]))?.GetValue<string>());
            Assert.Equal("fatal [x]", Assert.Single(Assert.IsType<JsonArray>(failed["data"]?["forbidden_matches"]))?["pattern"]?.GetValue<string>());
            var capped = await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject
            {
                ["checkpoint"] = "before",
                ["forbidden"] = new JsonArray("fatal [x]"),
                ["regex"] = false,
                ["max_matches"] = 0,
            }, owner);
            Assert.Equal("FAIL", capped["data"]?["status"]?.GetValue<string>());
            Assert.Equal(1, capped["data"]?["summary"]?["forbidden_count"]?.GetValue<int>());
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            Assert.Equal("FAIL", (await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject { ["checkpoint"] = "before" }, owner))["data"]?["status"]?.GetValue<string>());
            Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "before" }, owner))["data"]?["status"]?.GetValue<string>());
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_OutputEvidence_ClearedOrTrimmedRangeCannotPass(bool trim)
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject { ["category"] = "stdout", ["output"] = "forbidden marker\n" }),
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(
                new JsonObject { ["category"] = "stdout", ["output"] = "replacement output one\n" },
                new JsonObject { ["category"] = "stderr", ["output"] = "replacement output two\n" })
            }),
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString(),
                ["NETCOREDBG_MAX_OUTPUT_BYTES"] = trim ? "32" : "64",
            }));
        var owner = new JsonObject { ["muxSessionId"] = "truncated-evidence-owner" };
        Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "before" }, owner))["data"]?["status"]?.GetValue<string>());
        var active = false;
        try
        {
            active = true;
            var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner);
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            if (!trim) _ = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject { ["clear"] = true }, owner);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var output = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject(), owner);
            Assert.DoesNotContain("forbidden marker", output["data"]?["output"]?.GetValue<string>());
            Assert.Contains("replacement output two", output["data"]?["output"]?.GetValue<string>());
            var result = await LegacyConsoleCallAsync(driver, "output_assert_since", new JsonObject
            {
                ["checkpoint"] = "before",
                ["forbidden"] = new JsonArray("forbidden marker"),
                ["regex"] = false,
            }, owner);
            Assert.Equal("FAIL", result["data"]?["status"]?.GetValue<string>());
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_OutputEvidence_SearchUsesCaseInsensitivePatternCategoryAndContext()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(
                new JsonObject { ["category"] = "stdout", ["output"] = "before\nREADY 1\nafter\n" },
                new JsonObject { ["category"] = "stderr", ["output"] = "ready 2\n" }),
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "search-evidence-owner" };
        var foreign = new JsonObject { ["muxSessionId"] = "search-evidence-reader" };
        var active = false;
        try
        {
            active = true;
            var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner);
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var all = await LegacyConsoleCallAsync(driver, "search_output", new JsonObject { ["pattern"] = "ready [0-9]", ["context_lines"] = 1 }, foreign);
            Assert.Equal("ready [0-9]", all["data"]?["pattern"]?.GetValue<string>());
            Assert.Equal(2, all["data"]?["match_count"]?.GetValue<int>());
            Assert.Equal(2, Assert.IsType<JsonArray>(all["data"]?["matches"]).Count);
            var filtered = await LegacyConsoleCallAsync(driver, "search_output", new JsonObject { ["pattern"] = "ready [0-9]", ["context_lines"] = 1, ["category"] = "stdout" }, foreign);
            Assert.Equal(1, filtered["data"]?["match_count"]?.GetValue<int>());
            var match = Assert.Single(Assert.IsType<JsonArray>(filtered["data"]?["matches"]));
            Assert.Equal(2, match?["line_number"]?.GetValue<int>());
            Assert.Equal("READY 1", match?["match"]?.GetValue<string>());
            Assert.Equal(new[] { "before", "READY 1", "after" }, Assert.IsType<JsonArray>(match?["context"]).Select(item => item!.GetValue<string>()));
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
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
    public async Task LegacyCompat_FunctionBreakpoints_ReplaceFullSetAndPersistAcrossStops()
    {
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true } };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "function-owner" };
        var foreign = new JsonObject { ["muxSessionId"] = "function-foreign" };
        const string first = "Example.First";
        const string second = "Example.Second";
        JsonObject Point(string name, string? condition = null, string? hit = null) => new()
        {
            ["function_name"] = name,
            ["condition"] = condition,
            ["hit_condition"] = hit,
        };
        async Task AssertReplacementAsync(int from, params JsonObject[] expected)
        {
            var action = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(from), entry => entry.Command == "setFunctionBreakpoints");
            var actual = Assert.IsType<JsonArray>(JsonNode.Parse(action.Detail!)?["arguments"]?["breakpoints"]);
            Assert.Equal(expected.Length, actual.Count);
            for (var i = 0; i < expected.Length; i++) Assert.True(JsonNode.DeepEquals(expected[i], actual[i]));
        }
        JsonObject Dap(string name, string? condition = null, string? hit = null)
        {
            var point = new JsonObject { ["name"] = name };
            if (condition is not null) point["condition"] = condition;
            if (hit is not null) point["hitCondition"] = hit;
            return point;
        }

        var offline = await LegacyConsoleCallAsync(driver, "add_function_breakpoint", Point(first, "x > 1", "3"), owner);
        Assert.Equal(first, offline["data"]?["function"]?.GetValue<string>());
        Assert.Equal("x > 1", offline["data"]?["condition"]?.GetValue<string>());
        Assert.False(offline["data"]?["verified"]?.GetValue<bool>() ?? true);
        var idle = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), foreign);
        var initial = Assert.Single(Assert.IsType<JsonArray>(idle["data"]?["__function_breakpoints__"]));
        Assert.Equal(first, initial?["function"]?.GetValue<string>());
        Assert.Equal("3", initial?["hit_condition"]?.GetValue<string>());
        var launch = new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false };
        var active = false;
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            await AssertReplacementAsync(before, Dap(first, "x > 1", "3"));
            var listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), foreign);
            Assert.True(listed["data"]?["__function_breakpoints__"]?[0]?["verified"]?.GetValue<bool>());

            before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", Point(second), foreign))["error"]);
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "remove_function_breakpoint", new JsonObject { ["function_name"] = first }, foreign))["error"]);
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject(), foreign))["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), entry => entry.Command == "setFunctionBreakpoints");

            var added = await LegacyConsoleCallAsync(driver, "add_function_breakpoint", Point(second), owner);
            Assert.Equal(second, added["data"]?["function"]?.GetValue<string>());
            await AssertReplacementAsync(before, Dap(first, "x > 1", "3"), Dap(second));
            listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            Assert.False(listed["data"]?["__function_breakpoints__"]?[1]?["verified"]?.GetValue<bool>() ?? true);
            before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", Point(first, "x > 2", "4"), owner))["data"]);
            await AssertReplacementAsync(before, Dap(first, "x > 2", "4"), Dap(second));
            listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            Assert.Equal(2, Assert.IsType<JsonArray>(listed["data"]?["__function_breakpoints__"]).Count);
            Assert.Equal("x > 2", listed["data"]?["__function_breakpoints__"]?[0]?["condition"]?.GetValue<string>());
            Assert.Equal("4", listed["data"]?["__function_breakpoints__"]?[0]?["hit_condition"]?.GetValue<string>());
            before = (await driver.ReadNativeActionsAsync()).Count;
            var removed = await LegacyConsoleCallAsync(driver, "remove_function_breakpoint", new JsonObject { ["function_name"] = first }, owner);
            Assert.True(removed["data"]?["removed"]?.GetValue<bool>());
            Assert.Equal(first, removed["data"]?["function"]?.GetValue<string>());
            await AssertReplacementAsync(before, Dap(second));
            before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.False((await LegacyConsoleCallAsync(driver, "remove_function_breakpoint", new JsonObject { ["function_name"] = first }, owner))["data"]?["removed"]?.GetValue<bool>() ?? true);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), entry => entry.Command == "setFunctionBreakpoints");
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var retained = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            Assert.Equal(second, Assert.Single(Assert.IsType<JsonArray>(retained["data"]?["__function_breakpoints__"]))?["function"]?.GetValue<string>());
            before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            await AssertReplacementAsync(before, Dap(second));
            before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.Equal(1, (await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject(), owner))["data"]?["removed"]?.GetValue<int>());
            await AssertReplacementAsync(before);
            Assert.Null((await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner))["data"]?["__function_breakpoints__"]);
        }
        finally { if (active) await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_FunctionBreakpoints_SourceEventDuringFunctionSyncRemainsVisible()
    {
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionSyncSourceEvent"] = new JsonObject
            {
                ["reason"] = "changed",
                ["breakpoint"] = new JsonObject { ["id"] = 1, ["verified"] = false, ["line"] = 2 },
            },
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "function-source-event-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner);
        _ = await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Method" }, owner);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            var entry = Assert.Single(Assert.IsType<JsonArray>(list["data"]?[source]));
            Assert.False(entry?["verified"]?.GetValue<bool>());
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_FunctionBreakpoints_AmbiguousSourceEventWaitsForFunctionAck(bool collidingId)
    {
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionSyncEventRuntimeOnly"] = true,
            ["functionSyncHold"] = true,
            ["functionSyncReuseSourceId"] = collidingId,
            ["functionSyncSourceEvent"] = new JsonObject
            {
                ["reason"] = "changed",
                ["breakpoint"] = new JsonObject { ["id"] = 1, ["verified"] = false, ["line"] = 2 },
            },
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "function-ambiguous-event-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        Task<JsonObject>? pending = null;
        try
        {
            pending = LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Method" }, owner);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("function-breakpoints-response-gated", deadline.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var during = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.NotNull(during["error"]);
            Assert.False(during.ContainsKey("data"));
            driver.ReleaseNextResponse();
            Assert.NotNull((await pending.WaitAsync(deadline.Token))["data"]);
            var after = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            if (collidingId) Assert.NotNull(after["error"]);
            else Assert.False(Assert.Single(Assert.IsType<JsonArray>(after["data"]?[source]))?["verified"]?.GetValue<bool>());
        }
        finally
        {
            driver.ReleaseNextResponse();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        }
    }

    [Fact]
    public async Task LegacyCompat_FunctionBreakpoints_RefusedSyncInvalidatesSourceResource()
    {
        using var updates = new SemaphoreSlim(0);
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionSyncEventRuntimeOnly"] = true,
            ["functionSyncRefuseAfterEvent"] = true,
            ["functionSyncSourceEvent"] = new JsonObject
            {
                ["reason"] = "changed",
                ["breakpoint"] = new JsonObject { ["id"] = 1, ["verified"] = false, ["line"] = 2 },
            },
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() },
            NotificationHandler: (notification, _) =>
            {
                if (notification.Params?["uri"]?.GetValue<string>() == "debug://breakpoints") updates.Release();
                return ValueTask.CompletedTask;
            }));
        var owner = new JsonObject { ["muxSessionId"] = "function-invalidation-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/subscribe",
                new JsonObject { ["uri"] = "debug://breakpoints" }, new RequestId("function-invalidation-subscribe")));
            var initial = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read",
                new JsonObject { ["uri"] = "debug://breakpoints" }, new RequestId("function-invalidation-before")));
            Assert.NotNull(JsonNode.Parse(initial["contents"]![0]!["text"]!.GetValue<string>())?[source]);
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Method" }, owner))["error"]);
            Assert.True(await updates.WaitAsync(TimeSpan.FromSeconds(3)));
            await Assert.ThrowsAsync<McpProtocolException>(async () => await driver.SendRawRequestAsync("resources/read",
                new JsonObject { ["uri"] = "debug://breakpoints" }, new RequestId("function-invalidation-after")));
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_FunctionBreakpoints_UnverifiedZeroLineDoesNotPreventLaunch()
    {
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionBreakpointZeroLine"] = true,
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "function-zero-line-owner" };
        _ = await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Method" }, owner);
        var start = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner);
        Assert.True(start["data"]?["success"]?.GetValue<bool>());
        try
        {
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            var entry = Assert.Single(Assert.IsType<JsonArray>(list["data"]?["__function_breakpoints__"]));
            Assert.False(entry?["verified"]?.GetValue<bool>());
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_FunctionBreakpoints_FileScopedClearDoesNotRemoveFunctions()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var owner = new JsonObject { ["muxSessionId"] = "function-file-clear" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Method" }, owner))["data"]);
        var filtered = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
        Assert.Null(filtered["data"]?["__function_breakpoints__"]);
        Assert.Equal(1, (await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject { ["file"] = source }, owner))["data"]?["removed"]?.GetValue<int>());
        var global = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
        Assert.Equal("Example.Method", Assert.Single(Assert.IsType<JsonArray>(global["data"]?["__function_breakpoints__"]))?["function"]?.GetValue<string>());
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        Assert.Equal(2, (await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject(), owner))["data"]?["removed"]?.GetValue<int>());
        Assert.Null((await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner))["data"]?["__function_breakpoints__"]);
    }

    [Fact]
    public async Task LegacyCompat_FunctionBreakpoints_AreIsolatedBetweenHostProcesses()
    {
        var options = new ModernMcpStartOptions(ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]);
        await using var first = await ModernMcpProcessDriver.StartAsync(options);
        await using var second = await ModernMcpProcessDriver.StartAsync(options);
        var owner = new JsonObject { ["muxSessionId"] = "function-process-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(first, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Local" }, owner))["data"]);
        var firstList = await LegacyConsoleCallAsync(first, "list_breakpoints", new JsonObject(), owner);
        Assert.Equal("Example.Local", Assert.Single(Assert.IsType<JsonArray>(firstList["data"]?["__function_breakpoints__"]))?["function"]?.GetValue<string>());
        var secondList = await LegacyConsoleCallAsync(second, "list_breakpoints", new JsonObject(), owner);
        Assert.Null(secondList["data"]?["__function_breakpoints__"]);
    }

    [Fact]
    public async Task LegacyCompat_FunctionBreakpoints_UnsupportedAdapterSkipsDeferredSyncAndRejectsLiveAdd()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)));
        var owner = new JsonObject { ["muxSessionId"] = "unsupported-function-owner" };
        JsonObject Point(string name) => new() { ["function_name"] = name };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", Point("Example.Deferred"), owner))["data"]);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), entry => entry.Command == "setFunctionBreakpoints");
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", Point("Example.Live"), owner))["error"]);
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), entry => entry.Command == "setFunctionBreakpoints");
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            Assert.Equal("Example.Deferred", Assert.Single(Assert.IsType<JsonArray>(list["data"]?["__function_breakpoints__"]))?["function"]?.GetValue<string>());
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Theory]
    [InlineData("empty")]
    [InlineData("nonempty")]
    public async Task LegacyCompat_FunctionBreakpoints_RefusedMutationRestoresPythonRegistrySemantics(string refusal)
    {
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionBreakpointRefusal"] = refusal,
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "function-refusal-owner" };
        if (refusal == "empty")
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Keep" }, owner))["data"]);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = refusal == "empty"
                ? await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject(), owner)
                : await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Rejected" }, owner);
            Assert.NotNull(result["error"]);
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), entry => entry.Command == "setFunctionBreakpoints");
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            if (refusal == "empty")
                Assert.Equal("Example.Keep", Assert.Single(Assert.IsType<JsonArray>(list["data"]?["__function_breakpoints__"]))?["function"]?.GetValue<string>());
            else
                Assert.Null(list["data"]?["__function_breakpoints__"]);
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Theory]
    [InlineData("malformed")]
    [InlineData("timeout")]
    public async Task LegacyCompat_FunctionBreakpoints_StartupProtocolFailureIsNotSessionNotFound(string failure)
    {
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionStartupFailure"] = failure,
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "function-start-failure-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Deferred" }, owner))["data"]);
        var result = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
        }, owner, TimeSpan.FromSeconds(45));
        Assert.Equal("DAP_BREAKPOINTS_PROTOCOL_ERROR", result["error"]?.GetValue<string>());
        var actions = await driver.ReadNativeActionsAsync();
        Assert.Single(actions, static action => action.Command == "setFunctionBreakpoints");
        Assert.DoesNotContain(actions, static action => action.Command == "launch");
        var retained = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
        Assert.Equal("Example.Deferred", Assert.Single(Assert.IsType<JsonArray>(retained["data"]?["__function_breakpoints__"]))?["function"]?.GetValue<string>());
    }

    [Fact]
    public async Task LegacyCompat_FunctionBreakpoints_CancelledStartupRemainsCancelled()
    {
        if (!OperatingSystem.IsWindows()) return;
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionStartupFailure"] = "timeout",
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString(),
                ["CONTROLLED_DAP_OPTIONS"] = new FixtureConfiguration(SuppressLifecycleEvents: true).AsEnvironmentValue(),
            }));
        var owner = new JsonObject { ["muxSessionId"] = "function-start-cancel-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Deferred" }, owner))["data"]);
        using var cancellation = new CancellationTokenSource();
        var startId = new RequestId("function-start-cancel");
        var pending = driver.CallToolRawAsync("start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false },
            owner, startId, cancellation.Token, timeout: TimeSpan.FromSeconds(45));
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        await driver.WaitForFixtureRecordAsync("function-startup-response-withheld", deadline.Token);
        await driver.CaptureCancelledLegacyStartupAdapterAsync(startId, deadline.Token);
        await Assert.ThrowsAsync<InvalidOperationException>(() => driver.ConfirmCancelledLegacyStartupCleanupAsync(startId, deadline.Token));
        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () => await pending);
        Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command == "launch");
        using (var cancelledConfirmation = new CancellationTokenSource())
        {
            cancelledConfirmation.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => driver.ConfirmCancelledLegacyStartupCleanupAsync(startId, cancelledConfirmation.Token));
        }
        await driver.ConfirmCancelledLegacyStartupCleanupAsync(startId, deadline.Token);
    }

    [Fact]
    public async Task LegacyCompat_FunctionBreakpoints_RefusedReplacementRetainsAcknowledgedEntry()
    {
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionSyncRefuseAfterEvent"] = true,
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "function-replacement-refusal-owner" };
        JsonObject Point(string condition, string hit) => new() { ["function_name"] = "Example.Keep", ["condition"] = condition, ["hit_condition"] = hit };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", Point("x > 1", "3"), owner))["data"]);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var refused = await LegacyConsoleCallAsync(driver, "add_function_breakpoint", Point("x > 2", "4"), owner);
            Assert.NotNull(refused["error"]);
            var request = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setFunctionBreakpoints");
            var sent = Assert.Single(Assert.IsType<JsonArray>(JsonNode.Parse(request.Detail!)?["arguments"]?["breakpoints"]));
            Assert.Equal("x > 2", sent?["condition"]?.GetValue<string>());
            Assert.Equal("4", sent?["hitCondition"]?.GetValue<string>());
            var listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            var retained = Assert.Single(Assert.IsType<JsonArray>(listed["data"]?["__function_breakpoints__"]));
            Assert.Equal("Example.Keep", retained?["function"]?.GetValue<string>());
            Assert.Equal("x > 1", retained?["condition"]?.GetValue<string>());
            Assert.Equal("3", retained?["hit_condition"]?.GetValue<string>());
            Assert.True(retained?["verified"]?.GetValue<bool>());
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_SourceBreakpointOptions_PersistAndRequireAdapterCapabilities()
    {
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject
            {
                ["supportsConditionalBreakpoints"] = true,
                ["supportsHitConditionalBreakpoints"] = true,
            }
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "source-options-owner" };
        var file = BreakpointSourcePath;
        JsonObject Add(string condition, string hit) => new() { ["file"] = file, ["line"] = 2, ["condition"] = condition, ["hit_condition"] = hit };
        var offline = await LegacyConsoleCallAsync(driver, "add_breakpoint", Add("i == 5", "3"), owner);
        Assert.Equal("i == 5", offline["data"]?["condition"]?.GetValue<string>());
        var launch = new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false };
        var active = false;
        try
        {
            for (var cycle = 0; cycle < 2; cycle++)
            {
                var before = (await driver.ReadNativeActionsAsync()).Count;
                Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
                active = true;
                var request = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
                var breakpoint = JsonNode.Parse(request.Detail!)!["arguments"]!["breakpoints"]![0]!;
                Assert.Equal(cycle == 0 ? "i == 5" : "i > 2", breakpoint["condition"]?.GetValue<string>());
                Assert.Equal(cycle == 0 ? "3" : "4", breakpoint["hitCondition"]?.GetValue<string>());
                if (cycle == 0)
                {
                    Assert.True((await LegacyConsoleCallAsync(driver, "add_breakpoint", Add("i > 2", "4"), owner))["data"]?["verified"]?.GetValue<bool>());
                    var latest = (await driver.ReadNativeActionsAsync()).Last(static action => action.Command == "setBreakpoints");
                    var changed = JsonNode.Parse(latest.Detail!)!["arguments"]!["breakpoints"]![0]!;
                    Assert.Equal("i > 2", changed["condition"]?.GetValue<string>());
                    Assert.Equal("4", changed["hitCondition"]?.GetValue<string>());
                }
                Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
                active = false;
            }
        }
        finally { if (active) await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_SourceBreakpointOptions_StableAdapterIdRemainsInspectableAfterUpdate()
    {
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsConditionalBreakpoints"] = true } };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, SetBreakpointsResponseMode: "stable-id"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "stable-breakpoint-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        JsonObject Point(string condition) => new() { ["file"] = source, ["line"] = 2, ["condition"] = condition };
        _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", Point("i == 1"), owner);
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            Assert.True((await LegacyConsoleCallAsync(driver, "add_breakpoint", Point("i == 2"), owner))["data"]?["verified"]?.GetValue<bool>());
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            var entry = Assert.Single(Assert.IsType<JsonArray>(list["data"]?[source]));
            Assert.Equal("i == 2", entry?["condition"]?.GetValue<string>());
            Assert.True(entry?["verified"]?.GetValue<bool>());
            var response = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read",
                new JsonObject { ["uri"] = "debug://breakpoints" }, new RequestId(Guid.NewGuid().ToString("N"))));
            var resource = Assert.IsType<JsonObject>(JsonNode.Parse(response["contents"]![0]!["text"]!.GetValue<string>()));
            Assert.Equal("i == 2", resource[source]?[0]?["condition"]?.GetValue<string>());
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var retained = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Equal("i == 2", retained["data"]?[source]?[0]?["condition"]?.GetValue<string>());
        }
        finally { if (active) await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_HitConditionWithoutCapability_RefusesDeferredLaunch()
    {
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsConditionalBreakpoints"] = true } };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "deferred-hit-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject
        {
            ["file"] = BreakpointSourcePath,
            ["line"] = 2,
            ["hit_condition"] = "3",
        }, owner))["data"]);
        var start = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
        }, owner);
        Assert.Equal("DAP_BREAKPOINT_OPTIONS_UNSUPPORTED", start["error"]?.GetValue<string>());
        Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command is "setBreakpoints" or "launch");
    }

    [Fact]
    public async Task LegacyCompat_HitConditionWithoutCapability_RefusesLiveUpdate()
    {
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsConditionalBreakpoints"] = true } };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "live-hit-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var rejected = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject
            {
                ["file"] = BreakpointSourcePath,
                ["line"] = 2,
                ["hit_condition"] = "3",
            }, owner);
            Assert.Contains("does not advertise support", rejected["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointOptions_RejectOversizeWithoutReplacingIntent()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "bounded-source-options" };
        var initial = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject
        {
            ["file"] = BreakpointSourcePath,
            ["line"] = 2,
            ["condition"] = "i == 5",
        }, owner);
        Assert.NotNull(initial["data"]);
        var rejected = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject
        {
            ["file"] = BreakpointSourcePath,
            ["line"] = 2,
            ["condition"] = new string('x', 256 * 1024),
        }, owner);
        Assert.NotNull(rejected["error"]);
        var listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
        Assert.Equal("i == 5", listed["data"]?[Path.GetFullPath(BreakpointSourcePath)]?[0]?["condition"]?.GetValue<string>());
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_IdleListClearAndSubscription()
    {
        using var updates = new SemaphoreSlim(0);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            NotificationHandler: (notification, _) =>
            {
                if (notification.Params?["uri"]?.GetValue<string>() == "debug://breakpoints") updates.Release();
                return ValueTask.CompletedTask;
            }));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-inspection-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        async Task<JsonObject> ReadResourceAsync()
        {
            var response = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync(
                "resources/read", new JsonObject { ["uri"] = "debug://breakpoints" }, new RequestId(Guid.NewGuid().ToString("N"))));
            return Assert.IsType<JsonObject>(JsonNode.Parse(response["contents"]![0]!["text"]!.GetValue<string>()));
        }
        Assert.Empty(await ReadResourceAsync());
        _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/subscribe",
            new JsonObject { ["uri"] = "debug://breakpoints" }, new RequestId("breakpoint-subscribe")));
        var add = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner);
        Assert.False(add["data"]?["verified"]?.GetValue<bool>());
        Assert.True(await updates.WaitAsync(TimeSpan.FromSeconds(10)));
        var resource = await ReadResourceAsync();
        var entry = Assert.Single(Assert.IsType<JsonArray>(resource[source]));
        Assert.Equal(2, entry?["line"]?.GetValue<int>());
        Assert.Null(entry?["dap_line"]);
        Assert.Null(entry?["condition"]);
        Assert.False(entry?["verified"]?.GetValue<bool>());
        var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
        var listed = Assert.Single(Assert.IsType<JsonArray>(list["data"]?[source]));
        Assert.Equal(0, listed?["hit_count"]?.GetValue<int>());
        var clear = await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject { ["file"] = source }, owner);
        Assert.Equal(1, clear["data"]?["removed"]?.GetValue<int>());
        Assert.True(await updates.WaitAsync(TimeSpan.FromSeconds(10)));
        Assert.Empty(await ReadResourceAsync());
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_StartupVerificationAndRestart()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: "mixed")));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-verification-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        foreach (var line in new[] { 2, 6 })
            Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = line }, owner))["data"]?["verified"]?.GetValue<bool>());
        var launch = new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true };
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            var entries = Assert.IsType<JsonArray>(list["data"]?[source]);
            Assert.Equal(2, entries.Count);
            Assert.Equal(2, entries[0]?["line"]?.GetValue<int>());
            Assert.True(entries[0]?["verified"]?.GetValue<bool>());
            Assert.Null(entries[0]?["dap_line"]);
            Assert.Equal(6, entries[1]?["line"]?.GetValue<int>());
            Assert.True(entries[1]?["verified"]?.GetValue<bool>());
            Assert.Equal(7, entries[1]?["dap_line"]?.GetValue<int>());
            Assert.All(entries, item => Assert.Equal(0, item?["hit_count"]?.GetValue<int>()));
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var idle = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            var retained = Assert.IsType<JsonArray>(idle["data"]?[source]);
            Assert.True(retained[0]?["verified"]?.GetValue<bool>());
            Assert.All(retained, item => Assert.Equal(0, item?["hit_count"]?.GetValue<int>()));
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_StartupRemovedEventRemovesDesiredEntry()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: "mixed-remove")));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-event-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["verified"]?.GetValue<bool>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Empty(Assert.IsType<JsonArray>(list["data"]?[source]));
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var idle = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Empty(Assert.IsType<JsonArray>(idle["data"]?[source]));
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_MissingAdapterIdDoesNotPublishStaleObservations()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: "mixed-missing-id")));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-missing-id-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        foreach (var line in new[] { 2, 6 })
            _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = line }, owner);
        async Task AssertUnavailableAsync()
        {
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            Assert.NotNull(list["error"]);
            Assert.False(list.ContainsKey("data"));
            await Assert.ThrowsAsync<McpProtocolException>(async () => await driver.SendRawRequestAsync(
                "resources/read", new JsonObject { ["uri"] = "debug://breakpoints" }, new RequestId(Guid.NewGuid().ToString("N"))));
        }
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            await AssertUnavailableAsync();
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            await AssertUnavailableAsync();
            Assert.Equal(2, (await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject(), owner))["data"]?["removed"]?.GetValue<int>());
            Assert.Empty(Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner))["data"]));
            var resource = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read",
                new JsonObject { ["uri"] = "debug://breakpoints" }, new RequestId("breakpoints-after-clear")));
            Assert.Empty(Assert.IsType<JsonObject>(JsonNode.Parse(resource["contents"]![0]!["text"]!.GetValue<string>())));
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_OldRemovalCannotRemoveReaddedEntry()
    {
        var script = new JsonObject
        {
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(new JsonObject
                {
                    ["event"] = "breakpoint",
                    ["body"] = new JsonObject { ["reason"] = "removed", ["breakpoint"] = new JsonObject { ["id"] = 1 } },
                })
            }),
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-incarnation-owner" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        JsonObject Location() => new() { ["file"] = source, ["line"] = 2 };
        _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(), owner);
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            Assert.True((await LegacyConsoleCallAsync(driver, "remove_breakpoint", Location(), owner))["data"]?["removed"]?.GetValue<bool>());
            Assert.True((await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(), owner))["data"]?["verified"]?.GetValue<bool>());
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            var entry = Assert.Single(Assert.IsType<JsonArray>(list["data"]?[source]));
            Assert.Equal(2, entry?["line"]?.GetValue<int>());
            Assert.True(entry?["verified"]?.GetValue<bool>());
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Hygiene_ScopedCleanupRemovesDirectPointsOnlyInTargetFile()
    {
        var source = Path.GetFullPath(Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless/Program.cs"));
        var other = Path.GetFullPath(Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs"));
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)));
        var owner = new JsonObject { ["muxSessionId"] = "hygiene-scope-owner" };
        foreach (var file in new[] { source, other })
        {
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = file, ["line"] = 2 }, owner))["data"]);
        }
        var targetTrace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject { ["file"] = source, ["line"] = 6, ["expression"] = "6 * 7" }, owner);
        var otherTrace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject { ["file"] = other, ["line"] = 6, ["expression"] = "6 * 7" }, owner);
        var targetId = Assert.IsType<string>(targetTrace["data"]?["id"]?.GetValue<string>());
        var otherId = Assert.IsType<string>(otherTrace["data"]?["id"]?.GetValue<string>());
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["file"] = source }, owner);
            Assert.Equal("PASS", result["data"]?["status"]?.GetValue<string>());
            Assert.Equal(1, result["data"]?["cleared"]?["breakpoints"]?.GetValue<int>());
            Assert.Equal(1, result["data"]?["tracepoints_removed"]?.GetValue<int>());
            Assert.Empty(Assert.IsType<JsonArray>(result["data"]?["remaining_breakpoints"]));
            Assert.Empty(Assert.IsType<JsonArray>(result["data"]?["cleanup_errors"]));
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            Assert.NotEmpty(writes);
            AssertBreakpointRequest(writes[^1], source, []);
            Assert.DoesNotContain(writes, action => JsonNode.Parse(action.Detail!)?["arguments"]?["source"]?["path"]?.GetValue<string>() == other);
            var remaining = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = other }, owner);
            Assert.Contains(Assert.IsType<JsonArray>(remaining["data"]?[other]), item => item?["line"]?.GetValue<int>() == 2);
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "remove_tracepoint", new JsonObject { ["tracepoint_id"] = targetId }, owner))["error"]);
            Assert.Equal(otherId, (await LegacyConsoleCallAsync(driver, "remove_tracepoint", new JsonObject { ["tracepoint_id"] = otherId }, owner))["data"]?["removed"]?.GetValue<string>());
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_Hygiene_DisabledBreakpointCleanupReportsRemainingWithoutEffects()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)));
        var owner = new JsonObject { ["muxSessionId"] = "hygiene-disabled-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        var trace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject { ["file"] = source, ["line"] = 6, ["expression"] = "6 * 7" }, owner);
        var traceId = Assert.IsType<string>(trace["data"]?["id"]?.GetValue<string>());
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["file"] = source, ["clear_breakpoints"] = false, ["clear_trace_log"] = false }, owner);
            Assert.Equal("FAIL", result["data"]?["status"]?.GetValue<string>());
            Assert.Equal(0, result["data"]?["cleared"]?["breakpoints"]?.GetValue<int>());
            Assert.Contains(Assert.IsType<JsonArray>(result["data"]?["remaining_breakpoints"]), item => item?["line"]?.GetValue<int>() == 2);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            var listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Contains(Assert.IsType<JsonArray>(listed["data"]?[source]), item => item?["line"]?.GetValue<int>() == 2);
            Assert.Equal(traceId, (await LegacyConsoleCallAsync(driver, "remove_tracepoint", new JsonObject { ["tracepoint_id"] = traceId }, owner))["data"]?["removed"]?.GetValue<string>());
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_Hygiene_RemainingFunctionBreakpointFailsWhenDisabledOrRefused()
    {
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsFunctionBreakpoints"] = true },
            ["functionBreakpointRefusal"] = "empty",
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "hygiene-function-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_function_breakpoint", new JsonObject { ["function_name"] = "Example.Keep" }, owner))["data"]);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var disabled = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["clear_breakpoints"] = false }, owner);
            Assert.Equal("FAIL", disabled["data"]?["status"]?.GetValue<string>());
            var retained = Assert.Single(Assert.IsType<JsonArray>(disabled["data"]?["remaining_breakpoints"]));
            Assert.Equal("Example.Keep", retained?["function"]?.GetValue<string>());
            Assert.True(retained?["verified"]?.GetValue<bool>());
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setFunctionBreakpoints");
            var scoped = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["file"] = BreakpointSourcePath, ["clear_breakpoints"] = false }, owner);
            Assert.Equal("PASS", scoped["data"]?["status"]?.GetValue<string>());
            var refused = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject(), owner);
            Assert.Equal("FAIL", refused["data"]?["status"]?.GetValue<string>());
            Assert.NotEmpty(Assert.IsType<JsonArray>(refused["data"]?["cleanup_errors"]));
            Assert.Equal("Example.Keep", Assert.Single(Assert.IsType<JsonArray>(refused["data"]?["remaining_breakpoints"]))?["function"]?.GetValue<string>());
            var write = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setFunctionBreakpoints");
            Assert.Empty(Assert.IsType<JsonArray>(JsonNode.Parse(write.Detail!)?["arguments"]?["breakpoints"]));
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_Hygiene_ForeignOwnerIsRejectedBeforeCleanup()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)));
        var owner = new JsonObject { ["muxSessionId"] = "hygiene-owner" };
        var foreign = new JsonObject { ["muxSessionId"] = "hygiene-foreign" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var rejected = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["file"] = source }, foreign);
            Assert.Contains("owned by another agent", rejected["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            var listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(listed["data"]?[source]))?["line"]?.GetValue<int>());
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_Hygiene_RefusedActiveReplacementFailsWithoutRetry()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, SetBreakpointsResponseMode: "refuse-first-runtime")));
        var owner = new JsonObject { ["muxSessionId"] = "hygiene-refusal-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["file"] = source, ["clear_trace_log"] = false }, owner);
            Assert.Equal("FAIL", result["data"]?["status"]?.GetValue<string>());
            Assert.NotEmpty(Assert.IsType<JsonArray>(result["data"]?["cleanup_errors"]));
            var request = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(request, source, []);
            var repeated = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["file"] = source, ["clear_trace_log"] = false }, owner);
            Assert.Equal("FAIL", repeated["data"]?["status"]?.GetValue<string>());
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_Hygiene_InvalidScopeDoesNotClearPoints()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        var outside = Path.GetFullPath(Path.Combine(RepositoryLayout.Root, "..", "outside-hygiene.cs"));
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var owner = new JsonObject { ["muxSessionId"] = "hygiene-validation-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        var result = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["file"] = outside }, owner);
        Assert.Equal("FAIL", result["data"]?["status"]?.GetValue<string>());
        Assert.Equal("invalid file scope", result["data"]?["reason"]?.GetValue<string>());
        Assert.NotNull(result["data"]?["validation_error"]);
        var listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
        Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(listed["data"]?[source]))?["line"]?.GetValue<int>());
    }

    [Fact]
    public async Task LegacyCompat_Hygiene_ExceptionFiltersAreClearedOnlyWhenRequested()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, ExceptionBreakpointFilters: "supported")));
        var owner = new JsonObject { ["muxSessionId"] = "hygiene-filters-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "configure_exceptions", new JsonObject { ["filters"] = new JsonArray("all") }, owner))["data"]?["configured"]?.GetValue<bool>());
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var unchanged = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject(), owner);
            Assert.Equal("PASS", unchanged["data"]?["status"]?.GetValue<string>());
            Assert.Equal(0, unchanged["data"]?["cleared"]?["exception_filters"]?.GetValue<int>());
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setExceptionBreakpoints");
            var cleared = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["clear_exception_filters"] = true }, owner);
            Assert.Equal("PASS", cleared["data"]?["status"]?.GetValue<string>());
            Assert.Equal(1, cleared["data"]?["cleared"]?["exception_filters"]?.GetValue<int>());
            var write = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setExceptionBreakpoints");
            Assert.Empty(Assert.IsType<JsonArray>(JsonNode.Parse(write.Detail!)?["arguments"]?["filters"]));
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_Hygiene_GroupOwnedPointsRemainAndPreventPass()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)));
        var owner = new JsonObject { ["muxSessionId"] = "hygiene-group-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        var group = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "preserve",
            ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 8 }),
            ["tracepoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 8, ["expression"] = "6 * 7" }),
        }, owner);
        Assert.Equal("PASS", group["data"]?["status"]?.GetValue<string>());
        var groupTraceId = Assert.IsType<string>(group["data"]?["tracepoints"]?[0]?["id"]?.GetValue<string>());
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "debug_hygiene_preflight", new JsonObject { ["file"] = source }, owner);
            Assert.Equal("FAIL", result["data"]?["status"]?.GetValue<string>());
            Assert.Contains(Assert.IsType<JsonArray>(result["data"]?["remaining_breakpoints"]), item => item?["line"]?.GetValue<int>() == 8);
            var inspected = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "preserve" }, owner);
            Assert.Equal("PASS", inspected["data"]?["status"]?.GetValue<string>());
            Assert.Equal(8, Assert.Single(Assert.IsType<JsonArray>(inspected["data"]?["breakpoints"]))?["line"]?.GetValue<int>());
            Assert.Equal(groupTraceId, Assert.Single(Assert.IsType<JsonArray>(inspected["data"]?["tracepoints"]))?["id"]?.GetValue<string>());
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            Assert.NotEmpty(writes);
            AssertBreakpointRequest(writes[^1], source, [8]);
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointClear_RejectsForeignOwnerAndRetainsIntentOnRefusal()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: "refuse-first-runtime")));
        var owner = new JsonObject { ["muxSessionId"] = "clear-owner" };
        var foreign = new JsonObject { ["muxSessionId"] = "clear-foreign" };
        var source = Path.GetFullPath(BreakpointSourcePath);
        Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["verified"]?.GetValue<bool>());
        var launch = new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true };
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject { ["file"] = source }, foreign))["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            var refused = await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.NotNull(refused["error"]);
            var requests = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            AssertBreakpointRequest(Assert.Single(requests), source, []);
            var desired = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Empty(Assert.IsType<JsonArray>(desired["data"]?[source]));
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var afterStop = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Empty(Assert.IsType<JsonArray>(afterStop["data"]?[source]));
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointClear_MultiFileRefusalRetainsIntentWithoutRetry()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: "refuse-first-runtime")));
        var owner = new JsonObject { ["muxSessionId"] = "multi-clear-owner" };
        var first = BreakpointSourcePath;
        var second = Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs");
        foreach (var file in new[] { first, second })
            Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = file, ["line"] = 2 }, owner))["data"]?["verified"]?.GetValue<bool>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var clear = await LegacyConsoleCallAsync(driver, "clear_breakpoints", new JsonObject(), owner);
            Assert.NotNull(clear["error"]);
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            AssertBreakpointRequest(Assert.Single(writes), first, []);
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject(), owner);
            Assert.Empty(Assert.IsType<JsonObject>(list["data"]));
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_InstrumentationGroups_ReportHitsAndClearOnlyRequestedGroup()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "group-journey-owner" };
        var created = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "flow",
            ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2 }),
            ["tracepoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2, ["expression"] = "6 * 7" }),
        }, owner);
        Assert.Equal("PASS", created["data"]?["status"]?.GetValue<string>());
        var traceId = Assert.IsType<string>(created["data"]?["tracepoints"]?[0]?["id"]?.GetValue<string>());
        var other = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "other",
            ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 6 }),
        }, owner);
        Assert.Equal("PASS", other["data"]?["status"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            JsonObject inspected;
            while (true)
            {
                inspected = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect",
                    new JsonObject { ["name"] = "flow" }, owner))["data"]);
                if (inspected["status"]?.GetValue<string>() == "FAIL")
                {
                    Assert.False(inspected["hit_counts_complete"]?.GetValue<bool>() ?? true);
                    Assert.Equal("acknowledged", inspected["application"]?.GetValue<string>());
                    await Task.Delay(25, deadline.Token);
                    continue;
                }
                Assert.Equal("PASS", inspected["status"]?.GetValue<string>());
                if ((inspected["summary"]?["hit_count"]?.GetValue<int>() ?? 0) > 0 &&
                    (inspected["summary"]?["trace_log_count"]?.GetValue<int>() ?? 0) > 0) break;
                await Task.Delay(25, deadline.Token);
            }
            Assert.Equal(1, inspected["summary"]?["breakpoint_count"]?.GetValue<int>());
            Assert.Equal(1, inspected["summary"]?["tracepoint_count"]?.GetValue<int>());
            Assert.True(inspected["breakpoints"]?[0]?["hit_count"]?.GetValue<int>() > 0);
            var trace = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(inspected["tracepoints"])));
            Assert.True(trace["hit_count"]?.GetValue<int>() > 0);
            var log = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(trace["logs"])));
            Assert.Equal(traceId, log["tracepoint_id"]?.GetValue<string>());
            Assert.Equal(2, log["line"]?.GetValue<int>());
            Assert.Equal("6 * 7", log["expression"]?.GetValue<string>());
            Assert.Equal("42", log["value"]?.GetValue<string>());

            var cleared = await LegacyConsoleCallAsync(driver, "instrumentation_group_clear", new JsonObject { ["name"] = "flow" }, owner);
            Assert.Equal("PASS", cleared["data"]?["status"]?.GetValue<string>());
            Assert.Equal("FAIL", (await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "flow" }, owner))["data"]?["status"]?.GetValue<string>());
            var retained = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "other" }, owner);
            Assert.Equal("PASS", retained["data"]?["status"]?.GetValue<string>());
            Assert.Equal(6, retained["data"]?["breakpoints"]?[0]?["line"]?.GetValue<int>());
            AssertBreakpointRequest((await driver.ReadNativeActionsAsync()).Last(static action => action.Command == "setBreakpoints"), source, [6]);
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_InstrumentationGroups_InvalidRequestsDoNotMutateExistingGroup()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var owner = new JsonObject { ["muxSessionId"] = "group-invalid-owner" };
        JsonObject Create(string name, int line) => new()
        {
            ["name"] = name,
            ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = line })
        };
        Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "instrumentation_group_create", Create("keep", 2), owner))["data"]?["status"]?.GetValue<string>());
        var before = (await driver.ReadNativeActionsAsync()).Count;
        foreach (var (tool, args) in new (string, JsonObject)[]
        {
            ("instrumentation_group_create", Create("keep", 6)),
            ("instrumentation_group_create", Create("bad group name", 6)),
            ("instrumentation_group_create", new JsonObject { ["name"] = "empty", ["breakpoints"] = new JsonArray(), ["tracepoints"] = new JsonArray() }),
            ("instrumentation_group_create", new JsonObject { ["name"] = "invalid", ["breakpoints"] = new JsonArray(new JsonObject { ["line"] = 6 }) }),
            ("instrumentation_group_inspect", new JsonObject { ["name"] = "missing" }),
            ("instrumentation_group_clear", new JsonObject { ["name"] = "missing" }),
        })
        {
            var rejected = await LegacyConsoleCallAsync(driver, tool, args, owner);
            Assert.Equal("FAIL", rejected["data"]?["status"]?.GetValue<string>());
            var retained = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "keep" }, owner);
            Assert.Equal("PASS", retained["data"]?["status"]?.GetValue<string>());
            Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(retained["data"]?["breakpoints"]))?["line"]?.GetValue<int>());
        }
        Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
        var listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
        Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(listed["data"]?[source]))?["line"]?.GetValue<int>());
    }

    [Fact]
    public async Task LegacyCompat_InstrumentationGroups_ClearPreservesCoincidentUserBreakpoint()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsConditionalBreakpoints"] = true } };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "group-shared-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject
        { ["file"] = source, ["line"] = 2, ["condition"] = "value > 0" }, owner))["data"]);
        Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "trace",
            ["tracepoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2, ["expression"] = "6 * 7" }),
        }, owner))["data"]?["status"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "instrumentation_group_clear", new JsonObject { ["name"] = "trace" }, owner))["data"]?["status"]?.GetValue<string>());
            var latest = (await driver.ReadNativeActionsAsync()).Last(static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(latest, source, [2]);
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Equal("value > 0", Assert.Single(Assert.IsType<JsonArray>(list["data"]?[source]))?["condition"]?.GetValue<string>());
            Assert.True((await LegacyConsoleCallAsync(driver, "remove_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["removed"]?.GetValue<bool>());
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_InstrumentationGroups_RefusedClearRetainsGroupForRecovery()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: "refuse-first-runtime")));
        var owner = new JsonObject { ["muxSessionId"] = "group-refusal-owner" };
        Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "recover",
            ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2 }),
        }, owner))["data"]?["status"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var refused = await LegacyConsoleCallAsync(driver, "instrumentation_group_clear", new JsonObject { ["name"] = "recover" }, owner);
            Assert.True(refused["error"] is not null || refused["data"]?["status"]?.GetValue<string>() == "FAIL");
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            AssertBreakpointRequest(Assert.Single(writes), source, []);
            var retained = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "recover" }, owner);
            Assert.Equal("FAIL", retained["data"]?["status"]?.GetValue<string>());
            Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(retained["data"]?["breakpoints"]))?["line"]?.GetValue<int>());
            var repeat = await LegacyConsoleCallAsync(driver, "instrumentation_group_clear", new JsonObject { ["name"] = "recover" }, owner);
            Assert.True(repeat["error"] is not null || repeat["data"]?["status"]?.GetValue<string>() == "FAIL");
            var blockedCreate = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
            {
                ["name"] = "another",
                ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 6 }),
            }, owner);
            Assert.True(blockedCreate["error"] is not null || blockedCreate["data"]?["status"]?.GetValue<string>() == "FAIL");
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            Assert.Equal("FAIL", (await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "recover" }, owner))["data"]?["status"]?.GetValue<string>());
            var afterCleanup = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
            {
                ["name"] = "another",
                ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 6 }),
            }, owner);
            Assert.Equal("PASS", afterCleanup["data"]?["status"]?.GetValue<string>());
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Theory]
    [InlineData("add_breakpoint")]
    [InlineData("remove_breakpoint")]
    [InlineData("clear_breakpoints")]
    [InlineData("add_tracepoint")]
    [InlineData("remove_tracepoint")]
    public async Task LegacyCompat_InstrumentationGroups_RefusedClearQuarantinesDirectSourceMutations(string mutation)
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        var unrelated = Path.GetFullPath(Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs"));
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, SetBreakpointsResponseMode: "refuse-first-runtime")));
        var owner = new JsonObject { ["muxSessionId"] = "group-quarantine-direct-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 6 }, owner))["data"]);
        var trace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject { ["file"] = source, ["line"] = 8, ["expression"] = "6 * 7" }, owner);
        var traceId = Assert.IsType<string>(trace["data"]?["id"]?.GetValue<string>());
        var created = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "recover",
            ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2 }),
        }, owner);
        Assert.Equal("PASS", created["data"]?["status"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var refused = await LegacyConsoleCallAsync(driver, "instrumentation_group_clear", new JsonObject { ["name"] = "recover" }, owner);
            Assert.True(refused["error"] is not null || refused["data"]?["status"]?.GetValue<string>() == "FAIL");
            var failedSet = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(failedSet, source, [6, 8]);
            var arguments = mutation switch
            {
                "add_breakpoint" => new JsonObject { ["file"] = source, ["line"] = 10 },
                "remove_breakpoint" => new JsonObject { ["file"] = source, ["line"] = 6 },
                "clear_breakpoints" => new JsonObject { ["file"] = source },
                "add_tracepoint" => new JsonObject { ["file"] = source, ["line"] = 10, ["expression"] = "6 * 7" },
                _ => new JsonObject { ["tracepoint_id"] = traceId },
            };
            var rejected = await LegacyConsoleCallAsync(driver, mutation, arguments, owner);
            Assert.NotNull(rejected["error"]);
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            var retained = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "recover" }, owner);
            Assert.Equal("FAIL", retained["data"]?["status"]?.GetValue<string>());
            Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(retained["data"]?["breakpoints"]))?["line"]?.GetValue<int>());
            var other = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = unrelated, ["line"] = 2 }, owner);
            Assert.True(other["data"]?["verified"]?.GetValue<bool>());
            var replacements = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            Assert.Equal(2, replacements.Length);
            AssertBreakpointRequest(replacements[1], unrelated, [2]);
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_InstrumentationGroups_QuarantineUsesNormalizedSourceNotBasename()
    {
        var source = Path.GetFullPath(Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs"));
        var otherSource = Path.GetFullPath(Path.Combine(RepositoryLayout.Root, "host/NetCoreDbg.Mcp.Stateless/Program.cs"));
        var alias = Path.Combine(Path.GetDirectoryName(source)!, ".", Path.GetFileName(source));
        Assert.Equal(Path.GetFileName(source), Path.GetFileName(otherSource));
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, SetBreakpointsResponseMode: "refuse-first-runtime")));
        var owner = new JsonObject { ["muxSessionId"] = "group-same-basename-owner" };
        Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "uncertain",
            ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2 }),
        }, owner))["data"]?["status"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var refused = await LegacyConsoleCallAsync(driver, "instrumentation_group_clear", new JsonObject { ["name"] = "uncertain" }, owner);
            Assert.True(refused["error"] is not null || refused["data"]?["status"]?.GetValue<string>() == "FAIL");
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = alias, ["line"] = 6 }, owner))["error"]);
            var aliasGroup = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
            {
                ["name"] = "alias",
                ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = alias, ["line"] = 4 }),
            }, owner);
            Assert.True(aliasGroup["error"] is not null || aliasGroup["data"]?["status"]?.GetValue<string>() == "FAIL");
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            var otherGroup = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
            {
                ["name"] = "other",
                ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = otherSource, ["line"] = 4 }),
            }, owner);
            Assert.Equal("PASS", otherGroup["data"]?["status"]?.GetValue<string>());
            Assert.True((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = otherSource, ["line"] = 6 }, owner))["data"]?["verified"]?.GetValue<bool>());
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            Assert.Equal(3, writes.Length);
            AssertBreakpointRequest(writes[0], source, []);
            AssertBreakpointRequest(writes[1], otherSource, [4]);
            AssertBreakpointRequest(writes[2], otherSource, [6, 4]);
            var stillUnknown = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "uncertain" }, owner);
            Assert.Equal("FAIL", stillUnknown["data"]?["status"]?.GetValue<string>());
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_InstrumentationGroups_RejectedOverlapDoesNotInterruptInFlightTrace()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "group-rejected-trace-owner" };
        var trace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = "fixture:hold",
        }, owner);
        var traceId = Assert.IsType<string>(trace["data"]?["id"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", deadline.Token);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var rejected = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
            {
                ["name"] = "conflict",
                ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2, ["condition"] = "false" }),
            }, owner);
            Assert.True(rejected["error"] is not null || rejected["data"]?["status"]?.GetValue<string>() == "FAIL");
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            driver.ReleaseStackTraceResponse();
            await driver.WaitForFixtureRecordAsync("evaluate-response-written", deadline.Token);
            while (true)
            {
                var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["tracepoint_id"] = traceId }, owner);
                if (log["data"]?["entries"] is JsonArray entries && entries.Count > 0)
                {
                    var entry = Assert.Single(entries)!;
                    Assert.Equal(traceId, entry["tracepoint_id"]?.GetValue<string>());
                    Assert.Equal("private controlled evaluation payload", entry["value"]?.GetValue<string>());
                    break;
                }
                await Task.Delay(25, deadline.Token);
            }
            while (!(await driver.ReadNativeActionsAsync()).Any(static action => action.Command == "continue"))
                await Task.Delay(25, deadline.Token);
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_InstrumentationGroups_SharedConditionalBreakpointIsPreservedOrRefused(bool secondIsTracepoint)
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsConditionalBreakpoints"] = true } };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "group-conditional-owner" };
        JsonObject Point() => new() { ["file"] = source, ["line"] = 2, ["condition"] = "false" };
        var first = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "first",
            ["breakpoints"] = new JsonArray(Point()),
        }, owner);
        Assert.Equal("PASS", first["data"]?["status"]?.GetValue<string>());
        var secondArgs = new JsonObject { ["name"] = "second" };
        if (secondIsTracepoint)
            secondArgs["tracepoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2, ["expression"] = "6 * 7" });
        else secondArgs["breakpoints"] = new JsonArray(Point());
        var second = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", secondArgs, owner);
        var accepted = second["data"]?["status"]?.GetValue<string>() == "PASS";
        if (secondIsTracepoint) Assert.False(accepted);
        if (!accepted)
        {
            Assert.True(second["error"] is not null || second["data"]?["status"]?.GetValue<string>() == "FAIL");
            Assert.Equal("FAIL", (await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "second" }, owner))["data"]?["status"]?.GetValue<string>());
        }
        var retained = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "first" }, owner);
        Assert.Equal("PASS", retained["data"]?["status"]?.GetValue<string>());
        Assert.Equal("false", retained["data"]?["breakpoints"]?[0]?["condition"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var startup = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(startup, source, [2]);
            var physical = Assert.Single(Assert.IsType<JsonArray>(JsonNode.Parse(startup.Detail!)?["arguments"]?["breakpoints"]));
            Assert.Equal("false", physical?["condition"]?.GetValue<string>());
            if (accepted)
            {
                var other = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "second" }, owner);
                Assert.Equal("PASS", other["data"]?["status"]?.GetValue<string>());
            }
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Theory]
    [InlineData("group", "direct")]
    [InlineData("direct", "group")]
    [InlineData("group", "trace")]
    [InlineData("trace", "group")]
    public async Task LegacyCompat_InstrumentationGroups_IncompatibleOwnerAdmissionPreservesFirst(string first, string second)
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsConditionalBreakpoints"] = true } };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "group-admission-owner" };
        JsonObject Group(string condition) => new()
        {
            ["name"] = "conditional",
            ["breakpoints"] = new JsonArray(
            new JsonObject { ["file"] = source, ["line"] = 2, ["condition"] = condition })
        };
        JsonObject Direct(string condition) => new() { ["file"] = source, ["line"] = 2, ["condition"] = condition };
        JsonObject Trace() => new() { ["file"] = source, ["line"] = 2, ["expression"] = "6 * 7" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var initial = first switch
            {
                "group" => await LegacyConsoleCallAsync(driver, "instrumentation_group_create", Group("false"), owner),
                "direct" => await LegacyConsoleCallAsync(driver, "add_breakpoint", Direct("false"), owner),
                _ => await LegacyConsoleCallAsync(driver, "add_tracepoint", Trace(), owner),
            };
            if (first == "group") Assert.Equal("PASS", initial["data"]?["status"]?.GetValue<string>());
            else Assert.NotNull(initial["data"]);
            var traceId = first == "trace" ? Assert.IsType<string>(initial["data"]?["id"]?.GetValue<string>()) : null;
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var rejected = second switch
            {
                "group" => await LegacyConsoleCallAsync(driver, "instrumentation_group_create", Group(first == "direct" ? "true" : "false"), owner),
                "direct" => await LegacyConsoleCallAsync(driver, "add_breakpoint", Direct("true"), owner),
                _ => await LegacyConsoleCallAsync(driver, "add_tracepoint", Trace(), owner),
            };
            Assert.True(rejected["error"] is not null || rejected["data"]?["status"]?.GetValue<string>() == "FAIL");
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "setBreakpoints");
            if (first == "group")
            {
                var retained = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "conditional" }, owner);
                Assert.Equal("PASS", retained["data"]?["status"]?.GetValue<string>());
                Assert.Equal("false", retained["data"]?["breakpoints"]?[0]?["condition"]?.GetValue<string>());
            }
            else if (first == "direct")
            {
                var retained = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
                Assert.Equal("false", Assert.Single(Assert.IsType<JsonArray>(retained["data"]?[source]))?["condition"]?.GetValue<string>());
            }
            else Assert.Equal(traceId, (await LegacyConsoleCallAsync(driver, "remove_tracepoint", new JsonObject { ["tracepoint_id"] = traceId }, owner))["data"]?["removed"]?.GetValue<string>());
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Theory]
    [InlineData("idle")]
    [InlineData("stop")]
    [InlineData("restart")]
    public async Task LegacyCompat_InstrumentationGroups_ConfirmedStopOrRestartClearsOnlyGroupOwners(string mode)
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)));
        var owner = new JsonObject { ["muxSessionId"] = "group-lifecycle-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        var independent = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 6,
            ["expression"] = "6 * 7",
        }, owner);
        var independentId = Assert.IsType<string>(independent["data"]?["id"]?.GetValue<string>());
        var group = await LegacyConsoleCallAsync(driver, "instrumentation_group_create", new JsonObject
        {
            ["name"] = "lifecycle",
            ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 8 }),
            ["tracepoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2, ["expression"] = "6 * 7" }),
        }, owner);
        Assert.Equal("PASS", group["data"]?["status"]?.GetValue<string>());
        var groupTraceId = Assert.IsType<string>(group["data"]?["tracepoints"]?[0]?["id"]?.GetValue<string>());
        var active = false;
        try
        {
            if (mode != "idle")
            {
                Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
                { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
                active = true;
            }
            var stopped = mode == "restart"
                ? await LegacyConsoleCallAsync(driver, "restart_debug", new JsonObject { ["rebuild"] = false }, owner)
                : await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
            Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
            active = mode == "restart";
            var inspected = await LegacyConsoleCallAsync(driver, "instrumentation_group_inspect", new JsonObject { ["name"] = "lifecycle" }, owner);
            Assert.Equal("FAIL", inspected["data"]?["status"]?.GetValue<string>());
            var listed = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Equal(2, Assert.Single(Assert.IsType<JsonArray>(listed["data"]?[source]))?["line"]?.GetValue<int>());
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "remove_tracepoint", new JsonObject { ["tracepoint_id"] = groupTraceId }, owner))["error"]);
            Assert.Equal(independentId, (await LegacyConsoleCallAsync(driver, "remove_tracepoint", new JsonObject { ["tracepoint_id"] = independentId }, owner))["data"]?["removed"]?.GetValue<string>());
        }
        finally { if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Theory]
    [InlineData(true, true)]
    [InlineData(true, false)]
    [InlineData(false, true)]
    [InlineData(false, false)]
    public async Task LegacyCompat_Tracepoint_CoincidentOwnerRemovalPreservesOther(bool userFirst, bool removeUser)
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsConditionalBreakpoints"] = true } };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-shared-owner" };
        async Task AddUserAsync() => Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint",
            new JsonObject { ["file"] = source, ["line"] = 2, ["condition"] = "value > 0" }, owner))["data"]);
        if (userFirst) await AddUserAsync();
        var trace = await LegacyConsoleCallAsync(driver, "add_tracepoint",
            new JsonObject { ["file"] = source, ["line"] = 2, ["expression"] = "6 * 7" }, owner);
        var id = Assert.IsType<string>(trace["data"]?["id"]?.GetValue<string>());
        if (!userFirst) await AddUserAsync();
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug",
                new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var startup = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(startup, source, [2]);
            if (removeUser)
                Assert.True((await LegacyConsoleCallAsync(driver, "remove_breakpoint",
                    new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["removed"]?.GetValue<bool>());
            else
                Assert.Equal(id, (await LegacyConsoleCallAsync(driver, "remove_tracepoint",
                    new JsonObject { ["tracepoint_id"] = id }, owner))["data"]?["removed"]?.GetValue<string>());

            var latest = (await driver.ReadNativeActionsAsync()).Last(static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(latest, source, [2]);
            var remaining = Assert.Single(Assert.IsType<JsonArray>(JsonNode.Parse(latest.Detail!)?["arguments"]?["breakpoints"]));
            if (!removeUser) Assert.Equal("value > 0", remaining?["condition"]?.GetValue<string>());
            else Assert.Null(remaining?["condition"]);

            if (removeUser)
                Assert.Equal(id, (await LegacyConsoleCallAsync(driver, "remove_tracepoint",
                    new JsonObject { ["tracepoint_id"] = id }, owner))["data"]?["removed"]?.GetValue<string>());
            else
            {
                var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
                Assert.Equal("value > 0", Assert.Single(Assert.IsType<JsonArray>(list["data"]?[source]))?["condition"]?.GetValue<string>());
                Assert.True((await LegacyConsoleCallAsync(driver, "remove_breakpoint",
                    new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["removed"]?.GetValue<bool>());
            }
            var final = (await driver.ReadNativeActionsAsync()).Last(static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(final, source, []);
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Tracepoint_RefusedUserRemovalDoesNotResumeLaterStop()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop-threads-release-breakpoint-restop",
                StopReason: "breakpoint", StackTraceResponseMode: "legacy-breakpoint-frame",
                NextResponseMode: "continue-ack-only", SetBreakpointsResponseMode: "refuse-first-runtime"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-refused-removal-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint",
            new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        var trace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = "6 * 7"
        }, owner);
        var id = Assert.IsType<string>(trace["data"]?["id"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            while (true)
            {
                var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["tracepoint_id"] = id }, owner);
                if (log["data"]?["entries"] is JsonArray entries && entries.Count > 0) break;
                await Task.Delay(25, deadline.Token);
            }
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command == "continue");
            var removal = await LegacyConsoleCallAsync(driver, "remove_breakpoint",
                new JsonObject { ["file"] = source, ["line"] = 2 }, owner);
            Assert.NotNull(removal["error"]);
            var before = await driver.ReadNativeActionsAsync();
            Assert.Equal(2, before.Count(static action => action.Command == "setBreakpoints"));
            driver.ReleaseThreadsResponse();
            await driver.WaitForFixtureRecordAsync("breakpoint-restopped-event", deadline.Token);
            await driver.WaitForFixtureRecordAsync("second-breakpoint-frame-response", deadline.Token);
            await Task.Delay(TimeSpan.FromSeconds(1), deadline.Token);
            var later = (await driver.ReadNativeActionsAsync()).Skip(before.Count).ToArray();
            Assert.DoesNotContain(later, static action => action.Command is "evaluate" or "continue");
        }
        finally
        {
            driver.ReleaseThreadsResponse();
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Tracepoint_RefusedRelocatedUserRemovalDoesNotResumeLaterStop()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop-threads-release-breakpoint-restop",
                StopReason: "breakpoint", StackTraceResponseMode: "legacy-breakpoint-frame",
                NextResponseMode: "continue-ack-only", SetBreakpointsResponseMode: "same-effective-line-refuse-first-runtime"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-relocated-refusal-owner" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint",
            new JsonObject { ["file"] = source, ["line"] = 6 }, owner))["data"]);
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source, ["line"] = 2, ["expression"] = "6 * 7"
        }, owner))["data"]?["id"]);
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("first-breakpoint-frame-response", deadline.Token);
            var startup = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(startup, source, [6, 2]);
            var removal = await LegacyConsoleCallAsync(driver, "remove_breakpoint",
                new JsonObject { ["file"] = source, ["line"] = 6 }, owner);
            Assert.NotNull(removal["error"]);
            var before = await driver.ReadNativeActionsAsync();
            Assert.Equal(2, before.Count(static action => action.Command == "setBreakpoints"));
            Assert.DoesNotContain(before, static action => action.Command == "continue");
            driver.ReleaseThreadsResponse();
            await driver.WaitForFixtureRecordAsync("breakpoint-restopped-event", deadline.Token);
            await driver.WaitForFixtureRecordAsync("second-breakpoint-frame-response", deadline.Token);
            await Task.Delay(TimeSpan.FromSeconds(1), deadline.Token);
            var later = (await driver.ReadNativeActionsAsync()).Skip(before.Count).ToArray();
            Assert.DoesNotContain(later, static action => action.Command is "evaluate" or "continue");
        }
        finally
        {
            driver.ReleaseThreadsResponse();
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Tracepoint_BreakpointAddedDuringEvaluationPreventsContinue()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-mutation-owner" };
        var trace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = "fixture:hold"
        }, owner);
        var id = Assert.IsType<string>(trace["data"]?["id"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", deadline.Token);
            var added = await LegacyConsoleCallAsync(driver, "add_breakpoint",
                new JsonObject { ["file"] = source, ["line"] = 2 }, owner);
            Assert.NotNull(added["data"]);
            var latest = (await driver.ReadNativeActionsAsync()).Last(static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(latest, source, [2]);
            driver.ReleaseStackTraceResponse();
            await driver.WaitForFixtureRecordAsync("evaluate-response-written", deadline.Token);
            while (true)
            {
                var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["tracepoint_id"] = id }, owner);
                if (log["data"]?["entries"] is JsonArray entries && entries.Count > 0)
                {
                    var entry = Assert.Single(entries)!;
                    Assert.Equal(id, entry["tracepoint_id"]?.GetValue<string>());
                    Assert.Equal("private controlled evaluation payload", entry["value"]?.GetValue<string>());
                    break;
                }
                await Task.Delay(25, deadline.Token);
            }
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command == "continue");
            var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.Single(Assert.IsType<JsonArray>(list["data"]?[source]));
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Tracepoint_PauseBeforeBreakpointStopPreventsContinue()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-breakpoint-terminal",
                PauseResponseMode: "ack-before-breakpoint-held"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-pause-before-stop-owner" };
        var trace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = "fixture:hold"
        }, owner);
        var id = Assert.IsType<string>(trace["data"]?["id"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            var pause = await LegacyConsoleCallAsync(driver, "pause_execution", new JsonObject { ["thread_id"] = 1 }, owner);
            Assert.True(pause["data"]?["success"]?.GetValue<bool>());
            await driver.WaitForFixtureRecordAsync("pause-stop-gated", deadline.Token);
            Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "pause");
            driver.ReleasePauseStoppedEvent();
            await driver.WaitForFixtureRecordAsync("pause-stopped", deadline.Token);
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", deadline.Token);
            driver.ReleaseStackTraceResponse();
            await driver.WaitForFixtureRecordAsync("evaluate-response-written", deadline.Token);
            while (true)
            {
                var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["tracepoint_id"] = id }, owner);
                if (log["data"]?["entries"] is JsonArray entries && entries.Count > 0)
                {
                    var entry = Assert.Single(entries)!;
                    Assert.Equal(id, entry["tracepoint_id"]?.GetValue<string>());
                    Assert.Equal("private controlled evaluation payload", entry["value"]?.GetValue<string>());
                    break;
                }
                await Task.Delay(25, deadline.Token);
            }
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command == "continue");
            var resumed = await LegacyConsoleCallAsync(driver, "continue_execution", new JsonObject { ["thread_id"] = 1 }, owner);
            Assert.Equal("stopped", resumed["state"]?.GetValue<string>());
            Assert.Equal("breakpoint", resumed["reason"]?.GetValue<string>());
            while ((await driver.ReadNativeActionsAsync()).Count(static action => action.Command == "continue") < 2)
                await Task.Delay(25, deadline.Token);
            Assert.Equal(2, (await driver.ReadNativeActionsAsync()).Count(static action => action.Command == "continue"));
            while ((await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>() != "terminated")
                await Task.Delay(25, deadline.Token);
        }
        finally
        {
            driver.ReleasePauseStoppedEvent();
            driver.ReleaseStackTraceResponse();
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Tracepoint_PauseDuringEvaluationPreventsContinue()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only", PauseResponseMode: "ack-only"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-pause-owner" };
        var trace = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = "fixture:hold"
        }, owner);
        var id = Assert.IsType<string>(trace["data"]?["id"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", deadline.Token);
            var afterAdmission = (double)Stopwatch.GetTimestamp() / Stopwatch.Frequency;
            var pause = await LegacyConsoleCallAsync(driver, "pause_execution", new JsonObject { ["thread_id"] = 1 }, owner);
            Assert.True(pause["data"]?["success"]?.GetValue<bool>());
            Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "pause");
            driver.ReleaseStackTraceResponse();
            await driver.WaitForFixtureRecordAsync("evaluate-response-written", deadline.Token);
            while (true)
            {
                var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["tracepoint_id"] = id }, owner);
                if (log["data"]?["entries"] is JsonArray entries && entries.Count > 0)
                {
                    var entry = Assert.Single(entries)!;
                    Assert.Equal(id, entry["tracepoint_id"]?.GetValue<string>());
                    Assert.Equal("private controlled evaluation payload", entry["value"]?.GetValue<string>());
                    break;
                }
                await Task.Delay(25, deadline.Token);
            }
            var since = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["tracepoint_id"] = id, ["since"] = afterAdmission }, owner);
            Assert.Single(Assert.IsType<JsonArray>(since["data"]?["entries"]));
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command == "continue");
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Tracepoint_CrossedEffectiveLinesEvaluateOnlyMatchingTrace()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only",
                SetBreakpointsResponseMode: "crossed-lines"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-crossed-lines-owner" };
        var first = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = "6 * 7"
        }, owner);
        var second = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 6,
            ["expression"] = "tupleVar"
        }, owner);
        var firstId = Assert.IsType<string>(first["data"]?["id"]?.GetValue<string>());
        var secondId = Assert.IsType<string>(second["data"]?["id"]?.GetValue<string>());
        Assert.NotEqual(firstId, secondId);
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var startup = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(startup, source, [2, 6]);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            JsonArray entries;
            while (true)
            {
                var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject(), owner);
                entries = Assert.IsType<JsonArray>(log["data"]?["entries"]);
                if (entries.Count > 0) break;
                await Task.Delay(25, deadline.Token);
            }
            var evaluation = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "evaluate");
            Assert.Equal("tupleVar", JsonNode.Parse(evaluation.Detail!)?["arguments"]?["expression"]?.GetValue<string>());
            var entry = Assert.Single(entries)!;
            Assert.Equal(secondId, entry["tracepoint_id"]?.GetValue<string>());
            Assert.Equal("(\"test\", 123)", entry["value"]?.GetValue<string>());
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Tracepoint_StaleEvaluationDoesNotResumeNewStop()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-stale-owner" };
        var point = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = "fixture:trace-restop"
        }, owner);
        Assert.NotNull(point["data"]?["id"]);
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("evaluate-response-gated", deadline.Token);
            await driver.WaitForFixtureRecordAsync("evaluate-restopped-event", deadline.Token);
            driver.ReleaseStackTraceResponse();
            await driver.WaitForFixtureRecordAsync("evaluate-response-written", deadline.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command == "continue");
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_TraceCursor_RejectsOversizedFilterAtMark()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var result = await LegacyConsoleCallAsync(driver, "mark_trace_cursor",
            new JsonObject { ["tracepoint_id"] = new string('x', 20 * 1024) }, new JsonObject());
        Assert.False(string.IsNullOrWhiteSpace(result["error"]?.GetValue<string>()));
        Assert.Null(result["data"]?["cursor"]);
        var mark = await LegacyConsoleCallAsync(driver, "mark_trace_cursor", new JsonObject(), new JsonObject());
        var cursor = Assert.IsType<JsonObject>(mark["data"]?["cursor"]);
        var delta = await LegacyConsoleCallAsync(driver, "get_trace_delta", new JsonObject
        {
            ["cursor"] = cursor.DeepClone(),
            ["tracepoint_id"] = new string('x', 20 * 1024)
        }, new JsonObject());
        Assert.NotNull(delta["error"]);
        Assert.Null(delta["data"]?["next_cursor"]);
    }

    [Fact]
    public async Task LegacyCompat_TraceCursor_EmptyMarkPaginationFilterOverrideAndClear()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "trace-cursor-owner" };
        var ids = new List<string>();
        foreach (var expression in new[] { "6 * 7", "tupleVar", "6 * 7" })
        {
            var point = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
            {
                ["file"] = source,
                ["line"] = 2,
                ["expression"] = expression
            }, owner);
            ids.Add(Assert.IsType<string>(point["data"]?["id"]?.GetValue<string>()));
        }

        var mark = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "mark_trace_cursor", new JsonObject(), owner))["data"]);
        Assert.Equal(0, mark["buffer_size"]?.GetValue<int>());
        var cursor = Assert.IsType<JsonObject>(mark["cursor"]);
        Assert.Null(cursor["after_timestamp"]);
        var empty = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "get_trace_delta",
            new JsonObject { ["cursor"] = cursor.DeepClone() }, owner))["data"]);
        Assert.Empty(Assert.IsType<JsonArray>(empty["entries"]));
        Assert.False(empty["stale"]?.GetValue<bool>());

        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            JsonArray entries;
            while (true)
            {
                var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject(), owner);
                entries = Assert.IsType<JsonArray>(log["data"]?["entries"]);
                if (entries.Count >= ids.Count) break;
                await Task.Delay(25, deadline.Token);
            }
            Assert.Equal(ids, entries.Select(entry => entry!["tracepoint_id"]!.GetValue<string>()));

            var zero = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "get_trace_delta",
                new JsonObject { ["cursor"] = cursor.DeepClone(), ["limit"] = 0 }, owner))["data"]);
            Assert.Empty(Assert.IsType<JsonArray>(zero["entries"]));
            Assert.Equal(3, zero["available"]?.GetValue<int>());
            Assert.Equal(0, zero["total"]?.GetValue<int>());
            Assert.True(zero["limited"]?.GetValue<bool>());
            Assert.False(zero["stale"]?.GetValue<bool>());

            var next = Assert.IsType<JsonObject>(zero["next_cursor"]).DeepClone();
            var seen = new List<string>();
            for (var remaining = ids.Count; remaining > 0; remaining--)
            {
                var page = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "get_trace_delta",
                    new JsonObject { ["cursor"] = next, ["limit"] = 1 }, owner))["data"]);
                Assert.Equal(remaining, page["available"]?.GetValue<int>());
                Assert.Equal(1, page["total"]?.GetValue<int>());
                Assert.Equal(remaining > 1, page["limited"]?.GetValue<bool>());
                Assert.False(page["stale"]?.GetValue<bool>());
                seen.Add(Assert.Single(Assert.IsType<JsonArray>(page["entries"]))!["tracepoint_id"]!.GetValue<string>());
                next = Assert.IsType<JsonObject>(page["next_cursor"]).DeepClone();
            }
            Assert.Equal(ids, seen);
            var exhausted = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "get_trace_delta",
                new JsonObject { ["cursor"] = next }, owner))["data"]);
            Assert.Empty(Assert.IsType<JsonArray>(exhausted["entries"]));
            Assert.Equal(0, exhausted["available"]?.GetValue<int>());

            var filteredMark = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "mark_trace_cursor",
                new JsonObject { ["tracepoint_id"] = "no-such-tracepoint" }, owner))["data"]?["cursor"]);
            Assert.Null(filteredMark["after_timestamp"]);
            Assert.NotNull(filteredMark["global_after_timestamp"]);
            var overridden = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "get_trace_delta",
                new JsonObject { ["cursor"] = filteredMark.DeepClone(), ["tracepoint_id"] = ids[0] }, owner))["data"]);
            Assert.Empty(Assert.IsType<JsonArray>(overridden["entries"]));
            Assert.Equal(0, overridden["available"]?.GetValue<int>());

            var retainedMark = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "mark_trace_cursor", new JsonObject(), owner))["data"]?["cursor"]);
            Assert.Equal(3, (await LegacyConsoleCallAsync(driver, "clear_trace_log", new JsonObject(), owner))["data"]?["cleared"]?.GetValue<int>());
            var stale = Assert.IsType<JsonObject>((await LegacyConsoleCallAsync(driver, "get_trace_delta",
                new JsonObject { ["cursor"] = retainedMark.DeepClone() }, owner))["data"]);
            Assert.Empty(Assert.IsType<JsonArray>(stale["entries"]));
            Assert.True(stale["stale"]?.GetValue<bool>());
            Assert.Null(stale["dropped_count"]);
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Tracepoint_RecordsEvaluationResumesAndRemoves()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-journey-owner" };
        var tracepoint = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = "6 * 7"
        }, owner);
        var id = Assert.IsType<string>(tracepoint["data"]?["id"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var startupSet = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "setBreakpoints");
            AssertBreakpointRequest(startupSet, source, [2]);
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            JsonArray entries;
            while (true)
            {
                var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["tracepoint_id"] = id }, owner);
                entries = Assert.IsType<JsonArray>(log["data"]?["entries"]);
                if (entries.Count != 0) break;
                await Task.Delay(25, deadline.Token);
            }
            var entry = Assert.Single(entries)!;
            Assert.Equal("42", entry["value"]?.GetValue<string>());
            Assert.Equal(id, entry["tracepoint_id"]?.GetValue<string>());
            Assert.Equal(source, entry["file"]?.GetValue<string>());
            var timestamp = entry["timestamp"]!.GetValue<double>();
            var filtered = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["since"] = timestamp + 1 }, owner);
            Assert.Empty(Assert.IsType<JsonArray>(filtered["data"]?["entries"]));
            while (!(await driver.ReadNativeActionsAsync()).Any(static action => action.Command == "continue"))
                await Task.Delay(25, deadline.Token);
            Assert.Contains(await driver.ReadNativeActionsAsync(), static action => action.Command == "evaluate");
            var removed = await LegacyConsoleCallAsync(driver, "remove_tracepoint", new JsonObject { ["tracepoint_id"] = id }, owner);
            Assert.Equal(id, removed["data"]?["removed"]?.GetValue<string>());
            var replacements = (await driver.ReadNativeActionsAsync()).Where(static action => action.Command == "setBreakpoints").ToArray();
            Assert.Equal(2, replacements.Length);
            AssertBreakpointRequest(replacements[1], source, []);
            Assert.Equal(1, (await LegacyConsoleCallAsync(driver, "clear_trace_log", new JsonObject(), owner))["data"]?["cleared"]?.GetValue<int>());
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Theory]
    [InlineData(true, "6 * 7")]
    [InlineData(false, "fixture:negative")]
    public async Task LegacyCompat_Tracepoint_UnsafeHitNeverAutomaticallyContinues(bool userBreakpoint, string expression)
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", NextResponseMode: "continue-ack-only"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "tracepoint-safety-owner" };
        if (userBreakpoint)
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]);
        var tracepoint = await LegacyConsoleCallAsync(driver, "add_tracepoint", new JsonObject
        {
            ["file"] = source,
            ["line"] = 2,
            ["expression"] = expression
        }, owner);
        var id = Assert.IsType<string>(tracepoint["data"]?["id"]?.GetValue<string>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForStackTraceRequestAsync(deadline.Token);
            if (!userBreakpoint)
            {
                while (true)
                {
                    var log = await LegacyConsoleCallAsync(driver, "get_trace_log", new JsonObject { ["tracepoint_id"] = id }, owner);
                    if (log["data"]?["entries"] is JsonArray entries && entries.Count > 0) break;
                    await Task.Delay(25, deadline.Token);
                }
            }
            else
            {
                while (true)
                {
                    var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
                    if (list["data"]?[source] is JsonArray entries && entries.Count == 1 && entries[0]?["hit_count"]?.GetValue<long>() == 1) break;
                    await Task.Delay(25, deadline.Token);
                }
            }
            var removed = await LegacyConsoleCallAsync(driver, "remove_tracepoint", new JsonObject { ["tracepoint_id"] = id }, owner);
            Assert.Equal(id, removed["data"]?["removed"]?.GetValue<string>());
            Assert.DoesNotContain(await driver.ReadNativeActionsAsync(), static action => action.Command == "continue");
            if (userBreakpoint)
            {
                var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
                Assert.Single(Assert.IsType<JsonArray>(list["data"]?[source]));
            }
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_ObservedHitResetsOnStop()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint", StackTraceResponseMode: "legacy-breakpoint-frame"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-hit-owner" };
        Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["verified"]?.GetValue<bool>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            while (true)
            {
                var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
                if (list["data"]?[source] is JsonArray entries && entries[0]?["hit_count"]?.GetValue<long>() == 1) break;
                await Task.Delay(25, deadline.Token);
            }
            var context = await LegacyConsoleCallAsync(driver, "get_stop_context", new JsonObject { ["include_variables"] = false, ["include_output_tail"] = 0 }, owner);
            Assert.Equal(1, context["data"]?["hitCount"]?.GetValue<long>());
            Assert.Equal(source, context["data"]?["frames"]?[0]?["source"]?.GetValue<string>());
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var idle = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
            Assert.True(idle["data"]?[source]?[0]?["verified"]?.GetValue<bool>());
            Assert.Equal(0, idle["data"]?[source]?[0]?["hit_count"]?.GetValue<int>());
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_UsesEffectiveLineWhenRequestedLinesCross()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", SetBreakpointsResponseMode: "crossed-lines"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-adjusted-owner" };
        foreach (var line in new[] { 2, 6 })
            _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = line }, owner);
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForStackTraceRequestAsync(deadline.Token);
            while (true)
            {
                var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
                if (list["data"]?[source] is JsonArray entries)
                {
                    Assert.Equal(0, entries[0]?["hit_count"]?.GetValue<int>());
                    Assert.Equal(1, entries[1]?["hit_count"]?.GetValue<int>());
                    break;
                }
                await Task.Delay(25, deadline.Token);
            }
            var context = await LegacyConsoleCallAsync(driver, "get_stop_context", new JsonObject { ["include_variables"] = false, ["include_output_tail"] = 0 }, owner);
            Assert.Equal(1, context["data"]?["hitCount"]?.GetValue<int>());
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_SharedEffectiveLineReturnsUnavailableCount()
    {
        var source = Path.GetFullPath(BreakpointSourcePath);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint",
                StackTraceResponseMode: "legacy-breakpoint-frame", SetBreakpointsResponseMode: "same-effective-line"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = source }));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-ambiguous-owner" };
        foreach (var line in new[] { 2, 6 })
            _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = line }, owner);
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForStackTraceRequestAsync(deadline.Token);
            while (true)
            {
                var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
                Assert.False(list.ContainsKey("data"), "An ambiguous hit must not be assigned to an arbitrary requested breakpoint.");
                if (list["error"]?.GetValue<string>() == "Breakpoint hit counts are unavailable.") break;
                await Task.Delay(25, deadline.Token);
            }
            var context = await LegacyConsoleCallAsync(driver, "get_stop_context", new JsonObject { ["include_variables"] = false, ["include_output_tail"] = 0 }, owner);
            Assert.NotNull(context["error"]);
            Assert.False(context.ContainsKey("data"));
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_BreakpointInspection_HitThroughSymbolicLinkUsesSameSourceIdentity()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        var source = Path.GetFullPath(BreakpointSourcePath);
        var alias = Path.Combine(scratch, "source-alias.cs");
        try
        {
            File.CreateSymbolicLink(alias, source);
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "breakpoint", StackTraceResponseMode: "legacy-breakpoint-frame"),
                AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_BREAKPOINT_SOURCE"] = alias }));
            var owner = new JsonObject { ["muxSessionId"] = "breakpoint-link-owner" };
            _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = alias, ["line"] = 2 }, owner);
            var active = false;
            try
            {
                Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true }, owner))["data"]?["success"]?.GetValue<bool>());
                active = true;
                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                await driver.WaitForStackTraceRequestAsync(deadline.Token);
                while (true)
                {
                    var list = await LegacyConsoleCallAsync(driver, "list_breakpoints", new JsonObject { ["file"] = source }, owner);
                    if (list["data"]?[source] is JsonArray entries)
                    {
                        Assert.Equal(1, Assert.Single(entries)?["hit_count"]?.GetValue<int>());
                        break;
                    }
                    await Task.Delay(25, deadline.Token);
                }
            }
            finally
            {
                if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
            }
        }
        finally
        {
            if (File.Exists(alias)) File.Delete(alias);
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    [Fact]
    public async Task LegacyCompat_Breakpoints_PersistAcrossStopsAndTrackAcknowledgedUpdates()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop")));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-owner" };
        var other = new JsonObject { ["muxSessionId"] = "breakpoint-other" };
        var source = BreakpointSourcePath;
        JsonObject Location(int line) => new() { ["file"] = source, ["line"] = line };
        var idle = await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(2), owner);
        Assert.Equal("idle", idle["state"]?.GetValue<string>());
        Assert.False(idle["data"]?["verified"]?.GetValue<bool>());
        Assert.Null(idle["data"]?["condition"]);
        Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(6), owner))["data"]?["verified"]?.GetValue<bool>());
        Assert.True((await LegacyConsoleCallAsync(driver, "remove_breakpoint", Location(6), owner))["data"]?["removed"]?.GetValue<bool>());
        Assert.False((await LegacyConsoleCallAsync(driver, "remove_breakpoint", Location(6), owner))["data"]?["removed"]?.GetValue<bool>());
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 4, ["condition"] = 123 }, owner))["error"]);
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(0), owner))["error"]);
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source + ".missing", ["line"] = 1 }, owner))["error"]);

        var active = false;
        try
        {
            for (var cycle = 0; cycle < 2; cycle++)
            {
                var before = (await driver.ReadNativeActionsAsync()).Count;
                var start = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
                {
                    ["program"] = driver.InertProgramPath,
                    ["pre_build"] = false,
                    ["stop_at_entry"] = true,
                }, owner);
                Assert.True(start["data"]?["success"]?.GetValue<bool>());
                active = true;
                var launchRequests = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
                AssertBreakpointRequest(Assert.Single(launchRequests), source, cycle == 0 ? [2] : [8]);
                if (cycle == 0)
                {
                    Assert.NotNull((await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(9), other))["error"]);
                    Assert.True((await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(8), owner))["data"]?["verified"]?.GetValue<bool>());
                    Assert.True((await LegacyConsoleCallAsync(driver, "remove_breakpoint", Location(2), owner))["data"]?["removed"]?.GetValue<bool>());
                    var updates = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
                    Assert.Equal(3, updates.Length);
                    AssertBreakpointRequest(updates[1], source, [2, 8]);
                    AssertBreakpointRequest(updates[2], source, [8]);
                }
                Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
                active = false;
            }
            Assert.True((await LegacyConsoleCallAsync(driver, "remove_breakpoint", Location(8), owner))["data"]?["removed"]?.GetValue<bool>());
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Theory]
    [InlineData("refuse-first-runtime", false)]
    [InlineData("refuse-first-runtime", true)]
    [InlineData("missing-verified-first-runtime", false)]
    [InlineData("missing-verified-first-runtime", true)]
    public async Task LegacyCompat_Breakpoints_FailedActiveUpdateIsRetainedForReplay(string responseMode, bool remove)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: responseMode)));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-failure-owner" };
        var source = BreakpointSourcePath;
        Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner))["data"]?["verified"]?.GetValue<bool>());
        if (remove)
            Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 6 }, owner))["data"]?["verified"]?.GetValue<bool>());
        var active = false;
        try
        {
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var failed = await LegacyConsoleCallAsync(driver, remove ? "remove_breakpoint" : "add_breakpoint",
                new JsonObject { ["file"] = source, ["line"] = remove ? 2 : 6 }, owner);
            Assert.NotNull(failed["error"]);
            var error = failed["error"]?.GetValue<string>();
            Assert.Contains("retained", error, StringComparison.OrdinalIgnoreCase);
            if (responseMode == "missing-verified-first-runtime")
            {
                Assert.Contains("unknown", error, StringComparison.OrdinalIgnoreCase);
                Assert.Contains("do not retry", error, StringComparison.OrdinalIgnoreCase);
            }
            else
            {
                Assert.Contains("rejected", error, StringComparison.OrdinalIgnoreCase);
                Assert.Contains("debugger", error, StringComparison.OrdinalIgnoreCase);
                Assert.Contains("explicit", error, StringComparison.OrdinalIgnoreCase);
                Assert.Contains("later", error, StringComparison.OrdinalIgnoreCase);
            }
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var requests = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            AssertBreakpointRequest(Assert.Single(requests), source, remove ? [6] : [2, 6]);
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_Breakpoints_AfterRefusalNextExplicitMutationSendsFullDesiredSet(bool remove)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: "refuse-first-runtime")));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-resync-owner" };
        var source = BreakpointSourcePath;
        JsonObject Location(int line) => new() { ["file"] = source, ["line"] = line };
        Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(2), owner))["data"]?["verified"]?.GetValue<bool>());
        if (remove) Assert.False((await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(6), owner))["data"]?["verified"]?.GetValue<bool>());
        var active = false;
        try
        {
            var launch = new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true };
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var refused = await LegacyConsoleCallAsync(driver, remove ? "remove_breakpoint" : "add_breakpoint", Location(remove ? 2 : 6), owner);
            var error = refused["error"]?.GetValue<string>();
            Assert.Contains("retained", error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("rejected", error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("debugger", error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("explicit", error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("later", error, StringComparison.OrdinalIgnoreCase);
            var first = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            AssertBreakpointRequest(Assert.Single(first), source, remove ? [6] : [2, 6]);

            var added = await LegacyConsoleCallAsync(driver, "add_breakpoint", Location(8), owner);
            Assert.True(added["data"]?["verified"]?.GetValue<bool>());
            var replacements = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            Assert.Equal(2, replacements.Length);
            AssertBreakpointRequest(replacements[1], source, remove ? [6, 8] : [2, 6, 8]);
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var beforeRestart = (await driver.ReadNativeActionsAsync()).Count;
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var replay = (await driver.ReadNativeActionsAsync()).Skip(beforeRestart).Where(static action => action.Command == "setBreakpoints").ToArray();
            AssertBreakpointRequest(Assert.Single(replay), source, remove ? [6, 8] : [2, 6, 8]);
        }
        finally
        {
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Breakpoints_AreIsolatedBetweenHostProcesses()
    {
        var options = new ModernMcpStartOptions(ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"));
        await using var first = await ModernMcpProcessDriver.StartAsync(options);
        await using var second = await ModernMcpProcessDriver.StartAsync(options);
        var owner = new JsonObject { ["muxSessionId"] = "isolated-owner" };
        var source = BreakpointSourcePath;
        _ = await LegacyConsoleCallAsync(first, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner);
        async Task RunAsync(ModernMcpProcessDriver driver, bool expectedBreakpoint)
        {
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var start = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner);
            Assert.True(start["data"]?["success"]?.GetValue<bool>());
            try
            {
                var requests = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
                if (expectedBreakpoint) AssertBreakpointRequest(Assert.Single(requests), source, [2]);
                else Assert.Empty(requests);
            }
            finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
        }
        await RunAsync(first, true);
        await RunAsync(second, false);
        await RunAsync(first, true);
    }

    [Fact]
    public async Task LegacyCompat_Breakpoints_HeldUpdateDoesNotPreventStopAndIntentSurvives()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SetBreakpointsResponseMode: "hold-first-runtime")));
        var owner = new JsonObject { ["muxSessionId"] = "breakpoint-held-owner" };
        var source = BreakpointSourcePath;
        _ = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, owner);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        var active = true;
        Task<JsonObject>? pending = null;
        try
        {
            pending = LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 6 }, owner);
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureRecordAsync("runtime-breakpoints-response-gated", observation.Token);
            var busy = await LegacyConsoleCallAsync(driver, "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 8 }, owner);
            Assert.NotNull(busy["error"]);
            var stopped = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner).WaitAsync(TimeSpan.FromSeconds(10));
            Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
            active = false;
            var pendingResult = await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            if (pendingResult is null) Assert.NotNull((await pending)["error"]);
            else Assert.IsAssignableFrom<OperationCanceledException>(pendingResult);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var requests = (await driver.ReadNativeActionsAsync()).Skip(before).Where(static action => action.Command == "setBreakpoints").ToArray();
            AssertBreakpointRequest(Assert.Single(requests), source, [2, 6]);
            var afterRestart = (await driver.ReadNativeActionsAsync()).Count;
            driver.ReleaseNextResponse();
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(afterRestart), static action => action.Command == "setBreakpoints");
        }
        finally
        {
            driver.ReleaseNextResponse();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            if (active) await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    private static ModernMcpStartOptions MemoryWriteOptions(bool? supported, string? mode = null, bool expireOwner = false)
    {
        var capabilities = new JsonObject();
        if (supported is { } value) capabilities["supportsWriteMemoryRequest"] = value;
        var script = new JsonObject { ["breakpointCapabilities"] = capabilities, ["memoryWriteMode"] = mode };
        var environment = new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() };
        if (expireOwner) environment["NETCOREDBG_SESSION_TIMEOUT"] = "0";
        return new ModernMcpStartOptions(ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: environment);
    }

    [Fact]
    public async Task LegacyCompat_WriteMemory_CatalogDeclaresLegacySchemaAndDestructiveHint()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryWriteOptions(true));
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("tools/list", new JsonObject(), new RequestId("write-memory-catalog")));
        var tool = Assert.Single(Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == "write_memory");
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        var required = Assert.IsType<JsonArray>(schema["required"]).Select(static item => item?.GetValue<string>()).ToArray();
        Assert.Equal(["data", "memory_reference"], required.Order());
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(["allow_partial", "data", "memory_reference", "offset"], properties.Select(static item => item.Key).Order());
        Assert.Equal("string", properties["memory_reference"]?["type"]?.GetValue<string>());
        Assert.Equal("string", properties["data"]?["type"]?.GetValue<string>());
        Assert.Equal("integer", properties["offset"]?["type"]?.GetValue<string>());
        Assert.Equal("boolean", properties["allow_partial"]?["type"]?.GetValue<string>());
        Assert.True(tool["annotations"]?["destructiveHint"]?.GetValue<bool>());
    }

    [Theory]
    [InlineData(false)]
    [InlineData(null)]
    public async Task LegacyCompat_WriteMemory_UnsupportedCapabilityDoesNotWrite(bool? supported)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryWriteOptions(supported));
        var owner = new JsonObject { ["muxSessionId"] = "write-memory-unsupported" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID" }, owner);
            Assert.Contains("supportsWriteMemoryRequest", result["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.False(result.ContainsKey("data"));
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "writeMemory");
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_WriteMemory_InvalidInputAndForeignOwnerDoNotWrite()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryWriteOptions(true));
        var owner = new JsonObject { ["muxSessionId"] = "write-memory-owner" };
        var foreign = new JsonObject { ["muxSessionId"] = "write-memory-other" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            foreach (var arguments in new[]
            {
                new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "!!!" },
                new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID=" },
                new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AAAA====" },
                new JsonObject { ["memory_reference"] = "", ["data"] = "AQID" },
            })
                Assert.NotNull((await LegacyConsoleCallAsync(driver, "write_memory", arguments, owner))["error"]);
            var denied = await LegacyConsoleCallAsync(driver, "write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID" }, foreign);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "writeMemory");
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Theory]
    [InlineData("")]
    [InlineData("AQ==")]
    [InlineData("AQI=")]
    public async Task LegacyCompat_WriteMemory_ValidBase64IsDispatched(string data)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryWriteOptions(true));
        var owner = new JsonObject { ["muxSessionId"] = "write-memory-valid" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = data }, owner);
            Assert.Null(result["error"]);
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "writeMemory");
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_WriteMemory_NegativeResponseWarnsOfPossibleMutation()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryWriteOptions(true, "refused"));
        var owner = new JsonObject { ["muxSessionId"] = "write-memory-refused" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID", ["allow_partial"] = true }, owner);
            var error = Assert.IsType<string>(result["error"]?.GetValue<string>());
            Assert.Contains("may have changed", error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("do not retry", error, StringComparison.OrdinalIgnoreCase);
            Assert.False(result.ContainsKey("data"));
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "writeMemory");
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_WriteMemory_PartialRequestMapsResponseOnce()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryWriteOptions(true));
        var owner = new JsonObject { ["muxSessionId"] = "write-memory-partial" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID", ["offset"] = 4, ["allow_partial"] = true }, owner);
            Assert.Equal(2, result["data"]?["bytes_written"]?.GetValue<int>());
            Assert.Equal(7, result["data"]?["offset"]?.GetValue<int>());
            var write = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "writeMemory");
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["memoryReference"] = "0x1234", ["data"] = "AQID", ["offset"] = 4, ["allowPartial"] = true }, JsonNode.Parse(write.Detail!)?["arguments"]));
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_WriteMemory_InFlightWritePreventsOwnerExpiry()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryWriteOptions(true, "held", expireOwner: true));
        var owner = new JsonObject { ["muxSessionId"] = "write-memory-held-owner" };
        var foreign = new JsonObject { ["muxSessionId"] = "write-memory-held-foreign" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        Task<JsonObject>? pending = null;
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            pending = LegacyConsoleCallAsync(driver, "write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID" }, owner);
            await driver.WaitForFixtureRecordAsync("memory-write-response-gated", observation.Token);
            var denied = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), foreign);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            driver.ReleaseStackTraceResponse();
            Assert.Null((await pending.WaitAsync(TimeSpan.FromSeconds(10)))["error"]);
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "writeMemory");
            var checkpoint = await LegacyConsoleCallAsync(driver, "output_checkpoint", new JsonObject { ["name"] = "after-write" }, foreign);
            Assert.Equal("PASS", checkpoint["data"]?["status"]?.GetValue<string>());
            var stopped = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
            Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_WriteMemory_ChangedStoppedEpochIsNotReportedOrReplayed()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryWriteOptions(true, "stale"));
        var owner = new JsonObject { ["muxSessionId"] = "write-memory-stale" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        Task<JsonObject>? pending = null;
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            pending = LegacyConsoleCallAsync(driver, "write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID", ["offset"] = 4, ["allow_partial"] = true }, owner);
            await driver.WaitForFixtureRecordAsync("memory-write-response-gated", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            driver.ReleaseStackTraceResponse();
            var result = await pending.WaitAsync(TimeSpan.FromSeconds(10));
            var error = Assert.IsType<string>(result["error"]?.GetValue<string>());
            Assert.True(error.Contains("unknown", StringComparison.OrdinalIgnoreCase)
                || error.Contains("uncertain", StringComparison.OrdinalIgnoreCase)
                || error.Contains("may have", StringComparison.OrdinalIgnoreCase));
            Assert.False(result.ContainsKey("data"));
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "writeMemory");
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    private static ModernMcpStartOptions MemoryInspectionOptions(bool? supported, string? mode = null)
    {
        var capabilities = new JsonObject();
        if (supported is { } value)
        {
            capabilities["supportsReadMemoryRequest"] = value;
            capabilities["supportsDisassembleRequest"] = value;
            capabilities["supportsLocationsRequest"] = value;
        }
        var script = new JsonObject { ["breakpointCapabilities"] = capabilities, ["memoryInspectionMode"] = mode };
        return new ModernMcpStartOptions(ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() });
    }

    [Theory]
    [InlineData(false)]
    [InlineData(null)]
    public async Task LegacyCompat_MemoryInspection_UnsupportedCapabilitiesDoNotWrite(bool? supported)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryInspectionOptions(supported));
        var owner = new JsonObject { ["muxSessionId"] = "memory-unsupported" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            foreach (var (tool, arguments, capability) in new[]
            {
                ("read_memory", new JsonObject { ["memory_reference"] = "0x1234", ["count"] = 0 }, "supportsReadMemoryRequest"),
                ("read_memory", new JsonObject { ["memory_reference"] = "0x1234", ["count"] = 3 }, "supportsReadMemoryRequest"),
                ("disassemble", new JsonObject { ["memory_reference"] = "0x1234" }, "supportsDisassembleRequest"),
                ("get_locations", new JsonObject { ["location_reference"] = 42 }, "supportsLocationsRequest"),
            })
            {
                var result = await LegacyConsoleCallAsync(driver, tool, arguments, owner);
                Assert.Contains(capability, result["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
                Assert.False(result.ContainsKey("data"));
            }
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command is "readMemory" or "disassemble" or "locations");
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_MemoryInspection_MapsRequestsAndPythonFields()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryInspectionOptions(true));
        var owner = new JsonObject { ["muxSessionId"] = "memory-supported" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var empty = await LegacyConsoleCallAsync(driver, "read_memory", new JsonObject { ["memory_reference"] = "0x1234", ["count"] = 0 }, owner);
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["address"] = "", ["unreadable_bytes"] = 0, ["data"] = "" }, empty["data"]));
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "readMemory");
            var read = await LegacyConsoleCallAsync(driver, "read_memory", new JsonObject { ["memory_reference"] = "0x1234", ["offset"] = 4, ["count"] = 5 }, owner);
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["address"] = "0x1238", ["unreadable_bytes"] = 2, ["data"] = "AQID" }, read["data"]));
            var disassembly = await LegacyConsoleCallAsync(driver, "disassemble", new JsonObject { ["memory_reference"] = "0x1234", ["offset"] = 4, ["instruction_offset"] = -1, ["instruction_count"] = 2, ["resolve_symbols"] = false }, owner);
            var instructions = Assert.IsType<JsonArray>(disassembly["data"]?["instructions"]);
            Assert.Equal(2, instructions.Count);
            Assert.Equal("0x1234", instructions[0]?["address"]?.GetValue<string>());
            Assert.Equal("55", instructions[0]?["instructionBytes"]?.GetValue<string>());
            Assert.Equal("push rbp", instructions[0]?["instruction"]?.GetValue<string>());
            Assert.Equal("main", instructions[0]?["symbol"]?.GetValue<string>());
            Assert.Equal("normal", instructions[0]?["presentationHint"]?.GetValue<string>());
            Assert.Equal("nop", instructions[1]?["instruction"]?.GetValue<string>());
            var location = await LegacyConsoleCallAsync(driver, "get_locations", new JsonObject { ["location_reference"] = 42 }, owner);
            Assert.Equal("C:/src/Program.cs", location["data"]?["source"]?["path"]?.GetValue<string>());
            Assert.Equal(12, location["data"]?["line"]?.GetValue<int>());
            Assert.Equal(3, location["data"]?["column"]?.GetValue<int>());
            Assert.Equal(13, location["data"]?["end_line"]?.GetValue<int>());
            Assert.Equal(8, location["data"]?["end_column"]?.GetValue<int>());
            Assert.Equal(13, location["data"]?["endLine"]?.GetValue<int>());
            Assert.Equal(8, location["data"]?["endColumn"]?.GetValue<int>());
            var actions = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            JsonNode? Args(string command) => JsonNode.Parse(Assert.Single(actions, action => action.Command == command).Detail!)?["arguments"];
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["memoryReference"] = "0x1234", ["offset"] = 4, ["count"] = 5 }, Args("readMemory")));
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["memoryReference"] = "0x1234", ["offset"] = 4, ["instructionOffset"] = -1, ["instructionCount"] = 2, ["resolveSymbols"] = false }, Args("disassemble")));
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["locationReference"] = 42 }, Args("locations")));
            var defaultsBefore = (await driver.ReadNativeActionsAsync()).Count;
            var defaultRead = await LegacyConsoleCallAsync(driver, "read_memory", new JsonObject { ["memory_reference"] = "0x1234" }, owner);
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["address"] = "", ["unreadable_bytes"] = 0, ["data"] = "" }, defaultRead["data"]));
            var defaultDisassembly = await LegacyConsoleCallAsync(driver, "disassemble", new JsonObject { ["memory_reference"] = "0x1234" }, owner);
            Assert.Equal(2, Assert.IsType<JsonArray>(defaultDisassembly["data"]?["instructions"]).Count);
            var defaultActions = (await driver.ReadNativeActionsAsync()).Skip(defaultsBefore).ToArray();
            Assert.DoesNotContain(defaultActions, static action => action.Command == "readMemory");
            var defaultRequest = Assert.Single(defaultActions, static action => action.Command == "disassemble");
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["memoryReference"] = "0x1234", ["offset"] = 0, ["instructionOffset"] = 0, ["instructionCount"] = 64, ["resolveSymbols"] = true }, JsonNode.Parse(defaultRequest.Detail!)?["arguments"]));
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_MemoryInspection_InvalidBoundariesDoNotWrite()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryInspectionOptions(true));
        var owner = new JsonObject { ["muxSessionId"] = "memory-invalid" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            foreach (var (tool, arguments) in new[]
            {
                ("read_memory", new JsonObject { ["memory_reference"] = "", ["count"] = 0 }),
                ("read_memory", new JsonObject { ["memory_reference"] = "0x1234", ["count"] = -1 }),
                ("disassemble", new JsonObject { ["memory_reference"] = "", ["instruction_count"] = 1 }),
                ("disassemble", new JsonObject { ["memory_reference"] = "0x1234", ["instruction_count"] = 0 }),
                ("disassemble", new JsonObject { ["memory_reference"] = "0x1234", ["instruction_count"] = -1 }),
                ("get_locations", new JsonObject { ["location_reference"] = 0 }),
                ("get_locations", new JsonObject { ["location_reference"] = -1 }),
            })
                Assert.NotNull((await LegacyConsoleCallAsync(driver, tool, arguments, owner))["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command is "readMemory" or "disassemble" or "locations");
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Theory]
    [InlineData("read_memory", "readMemory")]
    [InlineData("disassemble", "disassemble")]
    [InlineData("get_locations", "locations")]
    public async Task LegacyCompat_MemoryInspection_ChangedStoppedEpochRejectsReply(string tool, string command)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryInspectionOptions(true, "stale"));
        var owner = new JsonObject { ["muxSessionId"] = "memory-stale" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        Task<JsonObject>? pending = null;
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var arguments = tool == "get_locations" ? new JsonObject { ["location_reference"] = 42 } : new JsonObject { ["memory_reference"] = "0x1234", [tool == "read_memory" ? "count" : "instruction_count"] = 1 };
            pending = LegacyConsoleCallAsync(driver, tool, arguments, owner);
            await driver.WaitForFixtureRecordAsync("memory-inspection-response-gated", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            driver.ReleaseStackTraceResponse();
            var response = await pending.WaitAsync(TimeSpan.FromSeconds(10));
            Assert.NotNull(response["error"]);
            Assert.False(response.ContainsKey("data"));
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command == command);
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner);
        }
    }

    [Theory]
    [InlineData("read_memory", "invalid-read")]
    [InlineData("disassemble", "invalid-disassemble")]
    [InlineData("get_locations", "invalid-locations")]
    public async Task LegacyCompat_MemoryInspection_InvalidAdapterResponseIsRejected(string tool, string mode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryInspectionOptions(true, mode));
        var owner = new JsonObject { ["muxSessionId"] = "memory-invalid-response" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var arguments = tool == "get_locations" ? new JsonObject { ["location_reference"] = 42 }
                : new JsonObject { ["memory_reference"] = "0x1234", [tool == "read_memory" ? "count" : "instruction_count"] = 1 };
            var response = await LegacyConsoleCallAsync(driver, tool, arguments, owner);
            Assert.NotNull(response["error"]);
            Assert.False(response.ContainsKey("data"));
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Theory]
    [InlineData("get_locations", "wide-location")]
    [InlineData("disassemble", "wide-instruction")]
    [InlineData("read_memory", "wide-unreadable")]
    public async Task LegacyCompat_MemoryInspection_WideSafeDapIntegersRemainReadable(string tool, string mode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryInspectionOptions(true, mode));
        var owner = new JsonObject { ["muxSessionId"] = "memory-wide-response" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var arguments = tool == "get_locations" ? new JsonObject { ["location_reference"] = 42 }
                : new JsonObject { ["memory_reference"] = "0x1234", [tool == "read_memory" ? "count" : "instruction_count"] = 1 };
            var response = await LegacyConsoleCallAsync(driver, tool, arguments, owner);
            var value = tool == "get_locations" ? response["data"]?["column"]
                : tool == "disassemble" ? response["data"]?["instructions"]?[0]?["column"] : response["data"]?["unreadable_bytes"];
            Assert.Equal(2_147_483_648L, value?.GetValue<long>());
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_Disassemble_OversizedReplyDoesNotReturnPartialData()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(MemoryInspectionOptions(true, "large"));
        var owner = new JsonObject { ["muxSessionId"] = "memory-large" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var response = await LegacyConsoleCallAsync(driver, "disassemble", new JsonObject { ["memory_reference"] = "0x1234", ["instruction_count"] = 2 }, owner);
            Assert.NotNull(response["error"]);
            Assert.False(response.ContainsKey("data"));
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), static action => action.Command == "disassemble");
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_InspectionUtilities_SnapshotDiffCollectionAndRetention()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_MAX_SNAPSHOTS"] = "2" }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "utility-owner" };
        var other = new JsonObject { ["muxSessionId"] = "utility-other" };
        Assert.Empty(Assert.IsType<JsonArray>((await LegacyConsoleCallAsync(driver, "list_snapshots", new JsonObject(), other))["data"]?["snapshots"]));
        await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner);
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var denied = await LegacyConsoleCallAsync(driver, "create_snapshot", new JsonObject { ["name"] = "before" }, other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var before = await LegacyConsoleCallAsync(driver, "create_snapshot", new JsonObject { ["name"] = "before" }, owner);
            Assert.Equal("before", before["data"]?["name"]?.GetValue<string>());
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "create_snapshot", new JsonObject { ["name"] = "before" }, owner))["error"]);
            _ = await LegacyConsoleCallAsync(driver, "step_over", new JsonObject(), owner);
            var after = await LegacyConsoleCallAsync(driver, "create_snapshot", new JsonObject { ["name"] = "after" }, owner);
            Assert.Equal("after", after["data"]?["name"]?.GetValue<string>());
            var diff = await LegacyConsoleCallAsync(driver, "diff_snapshots", new JsonObject { ["name1"] = "before", ["name2"] = "after" }, other);
            Assert.Contains(Assert.IsType<JsonArray>(diff["data"]?["changed"]), item => item?["name"]?.GetValue<string>() == "intVar");
            _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
            var collection = await LegacyConsoleCallAsync(driver, "analyze_collection", new JsonObject { ["variables_reference"] = 1002, ["sample_size"] = 1 }, owner);
            Assert.Equal(2, collection["data"]?["count"]?.GetValue<int>());
            Assert.Equal(123d, collection["data"]?["sum"]?.GetValue<double>());
            var summary = await LegacyConsoleCallAsync(driver, "summarize_object", new JsonObject { ["variables_reference"] = 1002 }, owner);
            Assert.Equal("Item1", summary["data"]?["properties"]?[0]?["path"]?.GetValue<string>());
            var cycle = await LegacyConsoleCallAsync(driver, "summarize_object", new JsonObject { ["variables_reference"] = 1010 }, owner);
            Assert.Equal("self", cycle["data"]?["properties"]?[1]?["path"]?.GetValue<string>());
            Assert.Equal("<circular ref>", cycle["data"]?["properties"]?[1]?["value"]?.GetValue<string>());
            var third = await LegacyConsoleCallAsync(driver, "create_snapshot", new JsonObject { ["name"] = "third" }, owner);
            Assert.Equal("third", third["data"]?["name"]?.GetValue<string>());
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
        var retained = await LegacyConsoleCallAsync(driver, "list_snapshots", new JsonObject(), other);
        Assert.Equal(2, Assert.IsType<JsonArray>(retained["data"]?["snapshots"]).Count);
        Assert.Equal("after", retained["data"]?["snapshots"]?[0]?["name"]?.GetValue<string>());
        Assert.Equal("third", retained["data"]?["snapshots"]?[1]?["name"]?.GetValue<string>());
    }

    [Fact]
    public async Task LegacyCompat_InspectionUtilities_InvalidatedSnapshotIsNotStored()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true,
                VariablesResponseMode: "hold-then-invalidated")), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "utility-owner" };
        await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner);
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var pending = LegacyConsoleCallAsync(driver, "create_snapshot", new JsonObject { ["name"] = "stale" }, owner);
            try
            {
                await driver.WaitForFixtureRecordAsync("inspection-response-gated", observation.Token);
                _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            }
            finally { driver.ReleaseStackTraceResponse(); }
            Assert.NotNull((await pending)["error"]);
            Assert.Empty(Assert.IsType<JsonArray>((await LegacyConsoleCallAsync(driver, "list_snapshots", new JsonObject(), owner))["data"]?["snapshots"]));
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
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

    [Fact]
    public async Task LegacyCompat_CompositeContexts_RespectEnvelopeDefaultEvaluationAndOwner()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "exception",
                SupportsDelayedStackTraceLoading: true, SupportsExceptionInfoRequest: true)), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "context-owner" };
        var other = new JsonObject { ["muxSessionId"] = "context-other" };
        Assert.NotNull((await LegacyConsoleCallAsync(driver, "get_stop_context", new JsonObject(), owner))["error"]);
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var stop = await LegacyConsoleCallAsync(driver, "get_stop_context", new JsonObject { ["include_output_tail"] = 0 }, other);
            Assert.Equal("stopped", stop["state"]?.GetValue<string>());
            Assert.Equal("exception", stop["data"]?["reason"]?.GetValue<string>());
            Assert.Equal("42", stop["data"]?["locals"]?[0]?["value"]?.GetValue<string>());
            Assert.NotNull(stop["data"]?["frames"]?[0]?["id"]);
            Assert.Null(stop["data"]?["inspectionContextId"]);
            var before = await driver.ReadNativeActionsAsync();
            var denied = await LegacyConsoleCallAsync(driver, "get_exception_context", new JsonObject(), other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), action => action.Command == "evaluate");
            var read = await LegacyConsoleCallAsync(driver, "get_exception_context", new JsonObject { ["max_inner_exceptions"] = 0 }, other);
            Assert.Equal("42", read["data"]?["frames"]?[0]?["variables"]?["Locals"]?[0]?["value"]?.GetValue<string>());
            Assert.Equal("42", read["data"]?["frames"]?[0]?["variables"]?["Registers"]?[0]?["value"]?.GetValue<string>());
            var result = await LegacyConsoleCallAsync(driver, "get_exception_context", new JsonObject(), owner);
            Assert.Equal("System.InvalidOperationException", result["data"]?["exception"]?["exceptionId"]?.GetValue<string>());
            Assert.Equal(Assert.IsType<JsonArray>(result["data"]?["frames"]).Count, result["data"]?["totalFrames"]?.GetValue<int>());
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), action => action.Command == "evaluate");
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }
    [Fact]
    public async Task LegacyCompat_ExceptionContext_MultipleScopesExceedingLimit_ReturnsNoPartialData()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "exception",
                SupportsExceptionInfoRequest: true, VariablesResponseMode: "large-context")), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "context-size-owner" };
        await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner);
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = await driver.ReadNativeActionsAsync();
            var result = await LegacyConsoleCallAsync(driver, "get_exception_context", new JsonObject(), owner);
            Assert.Equal("DEBUG_CONTEXT_TOO_LARGE", result["error"]?.GetValue<string>());
            Assert.False(result.ContainsKey("data"));
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before.Count), action => action.Command == "evaluate");
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }


    [Fact]
    public async Task LegacyCompat_ExceptionContext_NonExceptionStop_ReturnsError()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop")), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "context-owner" };
        await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner);
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var result = await LegacyConsoleCallAsync(driver, "get_exception_context", new JsonObject { ["max_inner_exceptions"] = 0 }, owner);
            Assert.NotNull(result["error"]);
            Assert.Null(result["data"]);
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_StopContext_InvalidatedLocals_ReturnsNoPartialData()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true,
                VariablesResponseMode: "hold-then-invalidated")), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "context-owner" };
        await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner);
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var pending = LegacyConsoleCallAsync(driver, "get_stop_context", new JsonObject(), owner);
            try
            {
                await driver.WaitForFixtureRecordAsync("inspection-response-gated", observation.Token);
                _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            }
            finally { driver.ReleaseStackTraceResponse(); }
            var result = await pending;
            Assert.NotNull(result["error"]);
            Assert.Null(result["data"]);
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_ExceptionContext_InvalidatedEvaluation_IsUnknownAndNeverRetried()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "exception",
                SupportsDelayedStackTraceLoading: true, SupportsExceptionInfoRequest: true,
                VariablesResponseMode: "context-evaluation-invalidated")), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "context-owner" };
        await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner);
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var before = await driver.ReadNativeActionsAsync();
            var result = await LegacyConsoleCallAsync(driver, "get_exception_context", new JsonObject { ["include_variables_for_frames"] = 0 }, owner);
            Assert.Contains("Do not retry", result["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.Null(result["data"]);
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), action => action.Command == "evaluate");
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_VariableMutationAndExceptions_UseOwnerAndCurrentContext()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "exception",
                SupportsDelayedStackTraceLoading: true, SupportsSetVariable: true,
                SupportsExceptionInfoRequest: true, ExceptionBreakpointFilters: "supported")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "mutation-owner" };
        var other = new JsonObject { ["muxSessionId"] = "mutation-other" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
            var scopes = await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject(), owner);
            var reference = Assert.IsType<JsonArray>(scopes["data"])[0]!["variablesReference"]!.GetValue<int>();
            var arguments = new JsonObject { ["variables_reference"] = reference, ["name"] = "intVar", ["value"] = "77" };
            var filters = new JsonObject { ["filters"] = new JsonArray("all", "user-unhandled") };
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var deniedSet = await LegacyConsoleCallAsync(driver, "set_variable", arguments, other);
            var deniedFilters = await LegacyConsoleCallAsync(driver, "configure_exceptions", filters, other);
            Assert.Contains("owned by another agent", deniedSet["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.Contains("owned by another agent", deniedFilters["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command is "setVariable" or "setExceptionBreakpoints");

            var configured = await LegacyConsoleCallAsync(driver, "configure_exceptions", filters, owner);
            Assert.True(configured["data"]?["configured"]?.GetValue<bool>());
            Assert.True(JsonNode.DeepEquals(new JsonArray("all", "user-unhandled"), configured["data"]?["filters"]));
            var exception = await LegacyConsoleCallAsync(driver, "get_exception_info", new JsonObject(), other);
            Assert.Equal("System.InvalidOperationException", exception["data"]?["exceptionId"]?.GetValue<string>());
            Assert.Equal("Root cause", exception["data"]?["details"]?["innerException"]?[0]?["innerException"]?[0]?["message"]?.GetValue<string>());
            var changed = await LegacyConsoleCallAsync(driver, "set_variable", arguments, owner);
            Assert.Equal("updated", changed["data"]?["value"]?.GetValue<string>());
            Assert.Equal("tuple", changed["data"]?["type"]?.GetValue<string>());
            Assert.Equal(1002, changed["data"]?["variablesReference"]?.GetValue<int>());
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            var set = Assert.Single(writes, action => action.Command == "setVariable");
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["variablesReference"] = reference, ["name"] = "intVar", ["value"] = "77" }, JsonNode.Parse(set.Detail!)?["arguments"]));
            var filterWrite = Assert.Single(writes, action => action.Command == "setExceptionBreakpoints");
            Assert.True(JsonNode.DeepEquals(new JsonArray("all", "user-unhandled"), JsonNode.Parse(filterWrite.Detail!)?["arguments"]?["filters"]));
            var infoWrite = Assert.Single(writes, action => action.Command == "exceptionInfo");
            Assert.Equal(1, JsonNode.Parse(infoWrite.Detail!)?["arguments"]?["threadId"]?.GetValue<int>());
            foreach (var emptyFilters in new[] { new JsonObject(), new JsonObject { ["filters"] = null } })
            {
                var beforeEmpty = (await driver.ReadNativeActionsAsync()).Count;
                var empty = await LegacyConsoleCallAsync(driver, "configure_exceptions", emptyFilters, owner);
                Assert.True(empty["data"]?["configured"]?.GetValue<bool>());
                Assert.Empty(Assert.IsType<JsonArray>(empty["data"]?["filters"]));
                var emptyWrite = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(beforeEmpty), action => action.Command == "setExceptionBreakpoints");
                Assert.Empty(Assert.IsType<JsonArray>(JsonNode.Parse(emptyWrite.Detail!)?["arguments"]?["filters"]));
            }
            var beforeZero = (await driver.ReadNativeActionsAsync()).Count;
            var zeroThread = await LegacyConsoleCallAsync(driver, "get_exception_info", new JsonObject { ["thread_id"] = 0 }, other);
            Assert.Equal("System.InvalidOperationException", zeroThread["data"]?["exceptionId"]?.GetValue<string>());
            var zeroWrite = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(beforeZero), action => action.Command == "exceptionInfo");
            Assert.Equal(1, JsonNode.Parse(zeroWrite.Detail!)?["arguments"]?["threadId"]?.GetValue<int>());
            var after = (await driver.ReadNativeActionsAsync()).Count;
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "get_variables", new JsonObject { ["variables_reference"] = reference }, owner))["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(after), action => action.Command == "variables");
            _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
            var freshScopes = await LegacyConsoleCallAsync(driver, "get_scopes", new JsonObject(), owner);
            var freshReference = Assert.IsType<JsonArray>(freshScopes["data"])[0]!["variablesReference"]!.GetValue<int>();
            var fresh = await LegacyConsoleCallAsync(driver, "get_variables", new JsonObject { ["variables_reference"] = freshReference }, owner);
            Assert.NotEmpty(Assert.IsType<JsonArray>(fresh["data"]));
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_MalformedExceptionFilters_DoNotClearConfiguration()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", ExceptionBreakpointFilters: "supported")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "filter-owner" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            var configured = await LegacyConsoleCallAsync(driver, "configure_exceptions",
                new JsonObject { ["filters"] = new JsonArray("all") }, owner);
            Assert.True(configured["data"]?["configured"]?.GetValue<bool>());
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var invalid = new JsonNode[]
            {
                new JsonObject { ["filters"] = "all" },
                new JsonObject { ["filters"] = new JsonArray(1) },
                new JsonObject { ["filters"] = new JsonObject() },
                new JsonObject { ["unknown"] = true },
                new JsonArray(),
            };
            var responses = new List<JsonObject>();
            for (var index = 0; index < invalid.Length; index++)
            {
                var result = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("tools/call", new JsonObject
                {
                    ["name"] = "configure_exceptions",
                    ["arguments"] = invalid[index],
                    ["_meta"] = owner.DeepClone(),
                }, new RequestId($"legacy-malformed-filters-{index}")));
                responses.Add(Assert.IsType<JsonObject>(ModernMcpProcessDriver.LegacyPayload(result)));
            }
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command == "setExceptionBreakpoints");
            Assert.All(responses, response => Assert.NotNull(response["error"]));
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Theory]
    [InlineData(false, "success")]
    [InlineData(true, "refused")]
    public async Task LegacyCompat_UnsupportedOrRefusedOperations_DoNotClaimSuccess(bool supported, string exceptionMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", StopReason: "exception",
                SupportsDelayedStackTraceLoading: true, SupportsSetVariable: supported,
                SupportsExceptionInfoRequest: supported, ExceptionInfoResponseMode: exceptionMode,
                ExceptionBreakpointFilters: "supported", SetExceptionBreakpointsResponseMode: "refused")),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "failure-owner" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var changed = await LegacyConsoleCallAsync(driver, "set_variable", new JsonObject
            {
                ["variables_reference"] = 1001,
                ["name"] = "fixture:negative",
                ["value"] = "1",
            }, owner);
            Assert.NotNull(changed["error"]);
            Assert.Null(changed["data"]?["value"]);
            var configured = await LegacyConsoleCallAsync(driver, "configure_exceptions", new JsonObject { ["filters"] = new JsonArray("all") }, owner);
            Assert.NotNull(configured["error"]);
            Assert.NotEqual(true, configured["data"]?["configured"]?.GetValue<bool>());
            var info = await LegacyConsoleCallAsync(driver, "get_exception_info", new JsonObject(), owner);
            Assert.Null(info["data"]);
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            Assert.Equal(supported ? 1 : 0, writes.Count(action => action.Command == "setVariable"));
            Assert.Single(writes, action => action.Command == "setExceptionBreakpoints");
            Assert.Equal(supported ? 1 : 0, writes.Count(action => action.Command == "exceptionInfo"));
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_SetVariable_InvalidatedAfterWrite_ReportsUnknownWithoutRetry()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop",
                SupportsDelayedStackTraceLoading: true, SupportsSetVariable: true)),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "race-owner" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        Task<JsonObject>? operation = null;
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            operation = LegacyConsoleCallAsync(driver, "set_variable", new JsonObject
            {
                ["variables_reference"] = 1001,
                ["name"] = "fixture:hold-invalidated",
                ["value"] = "1",
            }, owner, TimeSpan.FromSeconds(35));
            var gated = driver.WaitForFixtureRecordAsync("set-variable-response-gated", observation.Token);
            if (await Task.WhenAny(gated, operation) == operation)
            {
                var early = await operation;
                Assert.Fail($"set_variable completed before the DAP response gate: {early.ToJsonString()}");
            }
            await gated;
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.False(operation.IsCompleted);
            driver.ReleaseStackTraceResponse();
            var result = await operation;
            Assert.NotNull(result["error"]);
            Assert.Contains("may have", result["error"]!.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.Contains("retry", result["error"]!.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command == "setVariable");
        }
        finally
        {
            driver.ReleaseStackTraceResponse();
            if (operation is not null) await Record.ExceptionAsync(async () => await operation.WaitAsync(TimeSpan.FromSeconds(10)));
            await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner));
        }
    }

    [Fact]
    public async Task LegacyCompat_Freshness_ModernRootsResponseCompletesRequest()
    {
        var root = ModernMcpScratchDirectory.Create();
        try
        {
            var artifact = Path.Combine(root, "App.dll");
            await File.WriteAllTextAsync(artifact, "fixture artifact");
            await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync(options: new ModernMcpStartOptions(
                HostArguments: ["--legacy-compat"],
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null }));
            var meta = ModernMcpProcessDriver.CurrentMeta();
            Assert.IsType<JsonObject>(meta[MetaKeys.ClientCapabilities])["roots"] = new JsonObject();
            var request = new JsonObject
            {
                ["name"] = "verify_debug_freshness",
                ["arguments"] = new JsonObject { ["expected_workspace"] = root + Path.DirectorySeparatorChar, ["expected_artifacts"] = new JsonArray(artifact) },
                ["_meta"] = meta,
            };
            var initial = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync(
                "tools/call", request, new RequestId("freshness-roots-request"))));
            Assert.Equal("input_required", initial["resultType"]?.GetValue<string>());
            var input = Assert.Single(Assert.IsType<JsonObject>(initial["inputRequests"]));
            Assert.Equal("roots/list", input.Value?["method"]?.GetValue<string>());
            request["inputResponses"] = new JsonObject
            {
                [input.Key] = new JsonObject { ["roots"] = new JsonArray(new JsonObject { ["uri"] = new Uri(root + Path.DirectorySeparatorChar).AbsoluteUri }) },
            };
            var completed = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync(
                "tools/call", request, new RequestId("freshness-roots-response"))));
            Assert.Equal("complete", completed["resultType"]?.GetValue<string>());
            Assert.Equal("PASS", completed["structuredContent"]?["data"]?["status"]?.GetValue<string>());
            Assert.True(completed["structuredContent"]?["data"]?["workspace"]?["matched"]?.GetValue<bool>());
            Assert.Equal(root, completed["structuredContent"]?["data"]?["workspace"]?["expected"]?.GetValue<string>());
            Assert.Equal(artifact, Assert.Single(Assert.IsType<JsonArray>(completed["structuredContent"]?["data"]?["artifacts"]?["existing"]))?.GetValue<string>());
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(root); }
    }

    [Fact]
    public async Task LegacyCompat_Freshness_DoesNotClaimAnonymousSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_SESSION_TIMEOUT"] = "5" }));
        var direct = new JsonObject();
        var reader = new JsonObject { ["muxSessionId"] = "freshness-reader" };
        var idle = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", new JsonObject(), reader);
        Assert.Equal("PASS", idle["data"]?["status"]?.GetValue<string>());
        Assert.Null(idle["data"]?["process"]?["process_id"]);
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, direct))["data"]?["success"]?.GetValue<bool>());
        var read = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", new JsonObject(), reader);
        var stopped = await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), direct);
        if (stopped["error"]?.GetValue<string>() == "Debug session is owned by another agent.")
        {
            await Task.Delay(TimeSpan.FromMilliseconds(5100));
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), direct))["data"]?["success"]?.GetValue<bool>());
        }
        Assert.Equal("PASS", read["data"]?["status"]?.GetValue<string>());
        Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
    }

    [Fact]
    public async Task LegacyCompat_Freshness_CatalogExposesOptionalInputsAndReadOnlyAnnotation()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(new JsonObject(), new RequestId("freshness-catalog")));
        var tool = Assert.Single(Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == "verify_debug_freshness");
        Assert.True(tool["annotations"]?["readOnlyHint"]?.GetValue<bool>());
        Assert.False(tool["annotations"]?["openWorldHint"]?.GetValue<bool>() ?? true);
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.Empty(schema["required"] as JsonArray ?? new JsonArray());
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(new[] { "expected_artifacts", "expected_modules", "expected_process_id", "expected_process_name", "expected_sources", "expected_workspace", "require_active_process" },
            properties.Select(static item => item.Key).OrderBy(static key => key, StringComparer.Ordinal));
        Assert.Contains("integer", properties["expected_process_id"]?["type"]?.ToJsonString());
        foreach (var name in new[] { "expected_process_name", "expected_workspace" })
            Assert.Contains("string", properties[name]?["type"]?.ToJsonString());
        foreach (var name in new[] { "expected_sources", "expected_modules", "expected_artifacts" })
        {
            Assert.Contains("array", properties[name]?["type"]?.ToJsonString());
            Assert.Equal("string", properties[name]?["items"]?["type"]?.GetValue<string>());
        }
        Assert.Equal("boolean", properties["require_active_process"]?["type"]?.GetValue<string>());
        Assert.False(properties["require_active_process"]?["default"]?.GetValue<bool>() ?? true);
    }

    [Fact]
    public async Task LegacyCompat_Freshness_ReportsObservedMatchesMismatchesAndLaterSnapshot()
    {
        var root = ModernMcpScratchDirectory.Create();
        var workspace = Path.Combine(root, "workspace");
        var other = Path.Combine(root, "other");
        Directory.CreateDirectory(workspace);
        Directory.CreateDirectory(other);
        var source = Path.Combine(workspace, "Program.cs");
        var artifact = Path.Combine(workspace, "App.dll");
        var missing = Path.Combine(other, "Missing.dll");
        await File.WriteAllTextAsync(source, "fixture source");
        await File.WriteAllTextAsync(artifact, "fixture artifact");
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(
                new JsonObject { ["event"] = "process", ["body"] = new JsonObject { ["systemProcessId"] = Environment.ProcessId, ["name"] = "FixtureApp", ["isLocalProcess"] = false } },
                new JsonObject { ["event"] = "loadedSource", ["body"] = new JsonObject { ["reason"] = "new", ["source"] = new JsonObject { ["name"] = "Program.cs", ["path"] = source } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 7, ["name"] = "", ["path"] = artifact, ["symbolStatus"] = "Symbols loaded." } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 8, ["name"] = "NoPath.dll", ["path"] = "" } } }),
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(
                new JsonObject { ["event"] = "loadedSource", ["body"] = new JsonObject { ["reason"] = "removed", ["source"] = new JsonObject { ["name"] = "Program.cs", ["path"] = source } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "removed", ["module"] = new JsonObject { ["id"] = 7 } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "removed", ["module"] = new JsonObject { ["id"] = 8 } } })
            })
        };
        try
        {
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = workspace, ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
            var owner = new JsonObject { ["muxSessionId"] = "freshness-owner" };
            var foreign = new JsonObject { ["muxSessionId"] = "freshness-foreign" };
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
            try
            {
                JsonObject Expected() => new()
                {
                    ["expected_process_id"] = Environment.ProcessId,
                    ["expected_process_name"] = "fixtureapp",
                    ["expected_workspace"] = workspace,
                    ["expected_sources"] = new JsonArray(source),
                    ["expected_modules"] = new JsonArray(Path.Combine(other, "App.dll")),
                    ["expected_artifacts"] = new JsonArray(artifact),
                    ["require_active_process"] = true
                };
                var before = (await driver.ReadNativeActionsAsync()).Count;
                var pass = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", Expected(), owner);
                Assert.Equal("PASS", pass["data"]?["status"]?.GetValue<string>());
                Assert.Equal(Environment.ProcessId, pass["data"]?["process"]?["process_id"]?.GetValue<int>());
                Assert.Equal("FixtureApp", pass["data"]?["process"]?["process_name"]?.GetValue<string>());
                Assert.True(pass["data"]?["workspace"]?["matched"]?.GetValue<bool>());
                Assert.Equal(source, Assert.Single(Assert.IsType<JsonArray>(pass["data"]?["loaded_sources"]?["paths"]))?.GetValue<string>());
                var records = Assert.IsType<JsonArray>(pass["data"]?["modules"]?["records"]);
                var namedFromPath = Assert.Single(records, item => item?["id"]?.GetValue<int>() == 7);
                Assert.Equal("App.dll", namedFromPath?["name"]?.GetValue<string>());
                Assert.Equal("Symbols loaded.", namedFromPath?["symbolStatus"]?.GetValue<string>());
                Assert.Equal(artifact, Assert.Single(Assert.IsType<JsonArray>(pass["data"]?["artifacts"]?["existing"]))?.GetValue<string>());
                Assert.Empty(Assert.IsType<JsonArray>(pass["data"]?["warnings"]));
                Assert.Empty(Assert.IsType<JsonArray>(pass["data"]?["mismatches"]));
                var emptyPathMatch = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", new JsonObject { ["expected_modules"] = new JsonArray(".") }, owner);
                Assert.Equal("FAIL", emptyPathMatch["data"]?["status"]?.GetValue<string>());
                Assert.Null(Assert.Single(records, item => item?["id"]?.GetValue<int>() == 8)?["path"]);
                Assert.NotNull((await LegacyConsoleCallAsync(driver, "verify_debug_freshness", Expected(), foreign))["error"]);
                Assert.Equal("PASS", (await LegacyConsoleCallAsync(driver, "verify_debug_freshness", Expected(), new JsonObject()))["data"]?["status"]?.GetValue<string>());
                var failed = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", new JsonObject
                {
                    ["expected_process_id"] = 0,
                    ["expected_process_name"] = "DifferentApp",
                    ["expected_workspace"] = other,
                    ["expected_sources"] = new JsonArray(Path.Combine(other, "Missing.cs")),
                    ["expected_modules"] = new JsonArray(missing),
                    ["expected_artifacts"] = new JsonArray(missing)
                }, owner);
                Assert.Equal("FAIL", failed["data"]?["status"]?.GetValue<string>());
                var kinds = Assert.IsType<JsonArray>(failed["data"]?["mismatches"]).Select(static item => item?["kind"]?.GetValue<string>()).ToArray();
                foreach (var kind in new[] { "process_id_mismatch", "process_name_mismatch", "workspace_mismatch", "source_workspace_mismatch", "expected_source_missing", "expected_module_missing", "artifact_missing" }) Assert.Contains(kind, kinds);
                Assert.Equal(missing, Assert.Single(Assert.IsType<JsonArray>(failed["data"]?["artifacts"]?["missing"]))?.GetValue<string>());
                Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
                _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
                File.Delete(artifact);
                var later = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", Expected(), owner);
                Assert.Equal("FAIL", later["data"]?["status"]?.GetValue<string>());
                var laterWarnings = Assert.IsType<JsonArray>(later["data"]?["warnings"]).Select(static item => item?["kind"]?.GetValue<string>()).ToArray();
                Assert.Contains("loaded_sources_unavailable", laterWarnings);
                Assert.Contains("modules_unavailable", laterWarnings);
                Assert.Equal(artifact, Assert.Single(Assert.IsType<JsonArray>(later["data"]?["artifacts"]?["missing"]))?.GetValue<string>());
                Assert.Equal("PASS", pass["data"]?["status"]?.GetValue<string>());
                Assert.Equal(artifact, Assert.Single(Assert.IsType<JsonArray>(pass["data"]?["artifacts"]?["existing"]))?.GetValue<string>());
            }
            finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(root); }
    }

    [Fact]
    public async Task LegacyCompat_Freshness_UsesPythonUnicodeCasefoldForNames()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(
                new JsonObject { ["event"] = "process", ["body"] = new JsonObject { ["name"] = "Straße", ["isLocalProcess"] = false } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 1, ["name"] = "Straße.dll" } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 2, ["name"] = "İ.dll" } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 3, ["name"] = "ς.dll" } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 4, ["name"] = "\U00010400.dll" } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 5, ["name"] = "ﬀ.dll" } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 6, ["name"] = "\uE000.dll" } } },
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 7, ["name"] = "\U00010000.dll" } } })
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "freshness-unicode-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var result = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", new JsonObject
            {
                ["expected_process_name"] = "STRASSE",
                ["expected_modules"] = new JsonArray("STRASSE.dll", "i\u0307.dll", "σ.dll", "\U00010428.dll", "FF.dll")
            }, owner);
            Assert.Equal("PASS", result["data"]?["status"]?.GetValue<string>());
            Assert.Equal(new[] { "ff.dll", "i\u0307.dll", "strasse.dll", "σ.dll", "\uE000.dll", "\U00010000.dll", "\U00010428.dll" },
                Assert.IsType<JsonArray>(result["data"]?["modules"]?["names"]).Select(static item => item?.GetValue<string>()));
            var mismatch = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", new JsonObject
            {
                ["expected_process_name"] = "STRASSE2",
                ["expected_modules"] = new JsonArray("FF2.dll")
            }, owner);
            Assert.Equal("FAIL", mismatch["data"]?["status"]?.GetValue<string>());
            var kinds = Assert.IsType<JsonArray>(mismatch["data"]?["mismatches"]).Select(static item => item?["kind"]?.GetValue<string>());
            Assert.Contains("process_name_mismatch", kinds);
            Assert.Contains("expected_module_missing", kinds);
        }
        finally { await Record.ExceptionAsync(async () => await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner)); }
    }

    [Fact]
    public async Task LegacyCompat_Freshness_UnavailableObservationsWarnAndInvalidInputsHaveNoEffects()
    {
        var root = ModernMcpScratchDirectory.Create();
        var artifact = Path.Combine(root, "App.dll");
        await File.WriteAllTextAsync(artifact, "unchanged");
        try
        {
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = Path.Combine(root, "absent-root"), ["MCP_PROJECT_ROOT"] = Path.Combine(root, "absent-root") }));
            var owner = new JsonObject { ["muxSessionId"] = "freshness-unavailable-owner" };
            var arguments = new JsonObject
            {
                ["expected_process_id"] = Environment.ProcessId,
                ["expected_process_name"] = "FixtureApp",
                ["expected_workspace"] = root,
                ["expected_sources"] = new JsonArray(Path.Combine(root, "Program.cs")),
                ["expected_modules"] = new JsonArray(artifact),
                ["expected_artifacts"] = new JsonArray(artifact),
                ["require_active_process"] = true
            };
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var result = await LegacyConsoleCallAsync(driver, "verify_debug_freshness", arguments, owner);
            Assert.Equal("WARN", result["data"]?["status"]?.GetValue<string>());
            Assert.Null(result["data"]?["process"]?["process_id"]);
            Assert.Null(result["data"]?["process"]?["process_name"]);
            Assert.Null(result["data"]?["workspace"]?["matched"]);
            Assert.Empty(Assert.IsType<JsonArray>(result["data"]?["mismatches"]));
            var kinds = Assert.IsType<JsonArray>(result["data"]?["warnings"]).Select(static item => item?["kind"]?.GetValue<string>()).ToArray();
            foreach (var kind in new[] { "process_id_unavailable", "process_name_unavailable", "workspace_unavailable", "loaded_sources_unavailable", "modules_unavailable" }) Assert.Contains(kind, kinds);
            Assert.Equal(artifact, Assert.Single(Assert.IsType<JsonArray>(result["data"]?["artifacts"]?["existing"]))?.GetValue<string>());
            foreach (var invalid in new[] {
                new JsonObject { ["expected_process_id"] = "not-an-integer" },
                new JsonObject { ["expected_process_name"] = new JsonArray("bad") },
                new JsonObject { ["expected_workspace"] = true },
                new JsonObject { ["expected_sources"] = new JsonArray(42) },
                new JsonObject { ["expected_modules"] = "not-an-array" },
                new JsonObject { ["expected_artifacts"] = new JsonArray(false) },
                new JsonObject { ["require_active_process"] = "true" },
                new JsonObject { ["unexpected"] = true } })
                Assert.NotNull((await LegacyConsoleCallAsync(driver, "verify_debug_freshness", invalid, owner))["error"]);
            Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
            Assert.Equal("unchanged", await File.ReadAllTextAsync(artifact));
            Assert.Equal("WARN", (await LegacyConsoleCallAsync(driver, "verify_debug_freshness", arguments, owner))["data"]?["status"]?.GetValue<string>());
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(root); }
    }

    [Fact]
    public async Task LegacyCompat_Inventories_ObserveEventTransitionsAndRefreshSources()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 7, ["name"] = "initial", ["version"] = "1" } } },
                new JsonObject { ["event"] = "progressStart", ["body"] = new JsonObject { ["progressId"] = "build", ["title"] = "Working", ["percentage"] = 20 } }),
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(
                new JsonObject { ["event"] = "module", ["body"] = new JsonObject { ["reason"] = "changed", ["module"] = new JsonObject { ["id"] = 7, ["name"] = "updated", ["version"] = "2" } } },
                new JsonObject { ["event"] = "progressUpdate", ["body"] = new JsonObject { ["progressId"] = "build", ["message"] = "almost", ["percentage"] = 75 } })
            }),
            ["loadedSources"] = new JsonObject { ["sources"] = new JsonArray(new JsonObject { ["name"] = "Marker.cs", ["path"] = "src/Marker.cs" }) },
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsLoadedSourcesRequest: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "inventory-owner" };
        var reader = new JsonObject { ["muxSessionId"] = "inventory-reader" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            var modules = await LegacyConsoleCallAsync(driver, "get_modules", new JsonObject(), reader);
            Assert.Equal(1, modules["data"]?["count"]?.GetValue<int>());
            Assert.Equal("initial", Assert.Single(Assert.IsType<JsonArray>(modules["data"]?["modules"]))?["name"]?.GetValue<string>());
            var progress = await LegacyConsoleCallAsync(driver, "get_progress", new JsonObject(), reader);
            Assert.Equal(1, progress["data"]?["count"]?.GetValue<int>());
            var entry = Assert.Single(Assert.IsType<JsonArray>(progress["data"]?["progress"]));
            Assert.Equal("build", entry?["progressId"]?.GetValue<string>());
            Assert.Equal("build", entry?["progress_id"]?.GetValue<string>());
            Assert.Equal(20, entry?["percentage"]?.GetValue<int>());
            Assert.Equal(entry?["startedAt"]?.GetValue<double>(), entry?["started_at"]?.GetValue<double>());
            var initialState = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), reader);
            var initialData = Assert.IsType<JsonObject>(initialState["data"]);
            Assert.Equal(initialState["state"]?.GetValue<string>(), initialData["state"]?.GetValue<string>());
            Assert.True(initialData.ContainsKey("debuggeeAlive"));
            Assert.Null(initialData["debuggeeAlive"]);
            Assert.Equal("initial", Assert.Single(Assert.IsType<JsonArray>(initialData["modules"]))?["name"]?.GetValue<string>());
            Assert.Equal(20, initialData["activeProgress"]?["build"]?["percentage"]?.GetValue<int>());
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var sources = await LegacyConsoleCallAsync(driver, "get_loaded_sources", new JsonObject(), reader);
            Assert.Equal(1, sources["data"]?["count"]?.GetValue<int>());
            Assert.Equal("src/Marker.cs", Assert.Single(Assert.IsType<JsonArray>(sources["data"]?["sources"]))?["path"]?.GetValue<string>());
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var updatedModules = await LegacyConsoleCallAsync(driver, "get_modules", new JsonObject(), reader);
            Assert.Equal("updated", Assert.Single(Assert.IsType<JsonArray>(updatedModules["data"]?["modules"]))?["name"]?.GetValue<string>());
            var updatedProgress = await LegacyConsoleCallAsync(driver, "get_progress", new JsonObject(), reader);
            var updatedEntry = Assert.Single(Assert.IsType<JsonArray>(updatedProgress["data"]?["progress"]));
            Assert.Equal("almost", updatedEntry?["message"]?.GetValue<string>());
            Assert.Equal(75, updatedEntry?["percentage"]?.GetValue<int>());
            var refreshed = await LegacyConsoleCallAsync(driver, "get_loaded_sources", new JsonObject(), reader);
            Assert.Equal(1, refreshed["data"]?["count"]?.GetValue<int>());
            var updatedState = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), reader);
            var updatedData = Assert.IsType<JsonObject>(updatedState["data"]);
            Assert.Equal(updatedState["state"]?.GetValue<string>(), updatedData["state"]?.GetValue<string>());
            Assert.Equal("updated", Assert.Single(Assert.IsType<JsonArray>(updatedData["modules"]))?["name"]?.GetValue<string>());
            Assert.Equal(75, updatedData["activeProgress"]?["build"]?["percentage"]?.GetValue<int>());
            Assert.Contains(Assert.IsType<JsonObject>(updatedData["loadedSources"]),
                static source => source.Value?["name"]?.GetValue<string>() == "Marker.cs");
            Assert.Equal(2, (await driver.ReadNativeActionsAsync()).Skip(before).Count(action => action.Command == "loadedSources"));
        }
        finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_ActiveDebugState_IncompleteOrOversizedDetailsReportPartialState(bool oversized)
    {
        var events = oversized
            ? new JsonArray(
                new JsonObject { ["event"] = "module", ["generatedTextLength"] = 160 * 1024,
                    ["body"] = new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 1, ["name"] = "module" } } },
                new JsonObject { ["event"] = "loadedSource", ["generatedTextLength"] = 110 * 1024,
                    ["body"] = new JsonObject { ["reason"] = "new", ["source"] = new JsonObject { ["name"] = "source" } } })
            : new JsonArray(new JsonObject { ["event"] = "loadedSource", ["body"] = new JsonObject { ["reason"] = "broken" } });
        var script = new JsonObject { ["launch"] = events };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "partial-state-owner" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            var state = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner);
            var data = Assert.IsType<JsonObject>(state["data"]);
            Assert.Equal(state["state"]?.GetValue<string>(), data["state"]?.GetValue<string>());
            Assert.NotEqual("idle", state["state"]?.GetValue<string>());
            Assert.False(data.ContainsKey("loadedSources"));
            Assert.Contains("only execution state", state["warning"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
        }
        finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
    }

    [Fact]
    public async Task LegacyCompat_StepTargets_RequireOwnerAndFreshSelectedContext()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsStepTargets: true)),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "targets-owner" };
        var other = new JsonObject { ["muxSessionId"] = "targets-other" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var stack = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
            var frame = Assert.IsType<JsonArray>(stack["data"]?["frames"])[0]!["id"]!.GetValue<int>();
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var deniedQuery = await LegacyConsoleCallAsync(driver, "get_step_in_targets", new JsonObject(), other);
            var deniedStep = await LegacyConsoleCallAsync(driver, "step_into", new JsonObject { ["target_id"] = 42 }, other);
            Assert.Contains("owned by another agent", deniedQuery["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.Contains("owned by another agent", deniedStep["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command is "stepInTargets" or "stepIn");
            var targets = await LegacyConsoleCallAsync(driver, "get_step_in_targets", new JsonObject(), owner);
            var target = Assert.Single(Assert.IsType<JsonArray>(targets["data"]?["targets"]));
            Assert.Equal(42, target?["id"]?.GetValue<int>());
            Assert.Equal("Call", target?["label"]?.GetValue<string>());
            var queryWrite = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command == "stepInTargets");
            Assert.Equal(frame, JsonNode.Parse(queryWrite.Detail!)?["arguments"]?["frameId"]?.GetValue<int>());
            var stepped = await LegacyConsoleCallAsync(driver, "step_into", new JsonObject { ["target_id"] = 42 }, owner, TimeSpan.FromSeconds(35));
            Assert.Equal("stopped", stepped["state"]?.GetValue<string>());
            Assert.Equal("step", stepped["reason"]?.GetValue<string>());
            var stepWrite = Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before), action => action.Command == "stepIn");
            Assert.True(JsonNode.DeepEquals(new JsonObject { ["threadId"] = 1, ["targetId"] = 42 }, JsonNode.Parse(stepWrite.Detail!)?["arguments"]));
            var after = (await driver.ReadNativeActionsAsync()).Count;
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "get_step_in_targets", new JsonObject(), owner))["error"]);
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "step_into", new JsonObject { ["target_id"] = 42 }, owner))["error"]);
            Assert.DoesNotContain((await driver.ReadNativeActionsAsync()).Skip(after), action => action.Command is "stepInTargets" or "stepIn");
        }
        finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_InventoryAndTargets_UnsupportedOrRefusedAreHonest(bool advertised)
    {
        var script = new JsonObject
        {
            ["loadedSourcesRefused"] = true,
            ["loadedSources"] = new JsonObject { ["message"] = "private-adapter-detail" },
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop", SupportsStepTargets: advertised,
                StepTargetsMode: "refused", SupportsLoadedSourcesRequest: advertised),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "unsupported-owner" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await driver.WaitForFixtureEventAsync("stopped", observation.Token);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            _ = await LegacyConsoleCallAsync(driver, "get_call_stack", new JsonObject(), owner);
            var before = (await driver.ReadNativeActionsAsync()).Count;
            var sources = await LegacyConsoleCallAsync(driver, "get_loaded_sources", new JsonObject(), owner);
            Assert.NotNull(sources["error"]);
            Assert.Null(sources["data"]?["sources"]);
            Assert.DoesNotContain("private-adapter-detail", sources.ToJsonString());
            var targets = await LegacyConsoleCallAsync(driver, "get_step_in_targets", new JsonObject(), owner);
            Assert.NotNull(targets["error"]);
            Assert.Null(targets["data"]?["targets"]);
            if (!advertised)
            {
                var rejectedStep = await LegacyConsoleCallAsync(driver, "step_into", new JsonObject { ["target_id"] = 42 }, owner);
                Assert.NotNull(rejectedStep["error"]);
            }
            var writes = (await driver.ReadNativeActionsAsync()).Skip(before).ToArray();
            Assert.Equal(advertised ? 1 : 0, writes.Count(action => action.Command == "loadedSources"));
            Assert.Equal(advertised ? 1 : 0, writes.Count(action => action.Command == "stepInTargets"));
            Assert.DoesNotContain(writes, action => action.Command == "stepIn");
        }
        finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
    }

    [Fact]
    public async Task LegacyCompat_StateResource_FullContinuedClearsStopDetails()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject
            {
                ["event"] = "stopped",
                ["body"] = new JsonObject
                {
                    ["reason"] = "breakpoint",
                    ["threadId"] = 1,
                    ["allThreadsStopped"] = true,
                    ["description"] = "stop description",
                    ["text"] = "stop text",
                }
            }),
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(new JsonObject
                {
                    ["event"] = "continued",
                    ["body"] = new JsonObject { ["threadId"] = 1, ["allThreadsContinued"] = true },
                })
            }),
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "state-continue-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var before = await ReadLegacyStateResourceAsync(driver, "state-before-full-continue");
            Assert.Equal("breakpoint", before["stopReason"]?.GetValue<string>());
            Assert.Equal("stop description", before["stopDescription"]?.GetValue<string>());
            Assert.Equal("stop text", before["stopText"]?.GetValue<string>());
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var after = await ReadLegacyStateResourceAsync(driver, "state-after-full-continue");
            Assert.Equal("running", after["state"]?.GetValue<string>());
            Assert.Null(after["stopReason"]);
            Assert.Null(after["stopDescription"]);
            Assert.Null(after["stopText"]);
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Theory]
    [InlineData("\"reference\"", "2147483648", "4096", true)]
    [InlineData("\"reference\"", "\"invalid\"", "4096", false)]
    [InlineData("\"reference\"", "1.5", "4096", false)]
    [InlineData("\"reference\"", "9007199254740992", "4096", false)]
    [InlineData("\"reference\"", "-9007199254740992", "4096", false)]
    [InlineData("\"reference\"", "1", "\"invalid\"", false)]
    [InlineData("\"reference\"", "1", "9007199254740992", false)]
    [InlineData("\"reference\"", "1", "-1", false)]
    [InlineData("42", "1", "4096", false)]
    public async Task LegacyCompat_StateResource_MemoryEventPreservesIntegersOrRefusesInvalid(string reference, string offset, string count, bool valid)
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject
            {
                ["event"] = "memory",
                ["body"] = new JsonObject
                {
                    ["memoryReference"] = JsonNode.Parse(reference),
                    ["offset"] = JsonNode.Parse(offset),
                    ["count"] = JsonNode.Parse(count),
                },
            })
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "state-memory-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            if (valid)
            {
                var state = await ReadLegacyStateResourceAsync(driver, "state-memory-valid");
                Assert.Equal(2147483648L, state["lastMemoryEvent"]?["offset"]?.GetValue<long>());
                Assert.Equal(4096L, state["lastMemoryEvent"]?["count"]?.GetValue<long>());
            }
            else
            {
                var error = await Assert.ThrowsAsync<McpProtocolException>(async () => await ReadLegacyStateResourceAsync(driver, "state-memory-invalid"));
                Assert.Equal(McpErrorCode.InvalidRequest, error.ErrorCode);
            }
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Theory]
    [InlineData("reader-failure")]
    [InlineData("eof")]
    public async Task LegacyCompat_StateResource_ReaderFailureNotifiesSubscriberWithoutTerminalEvent(string mode)
    {
        using var notifications = new SemaphoreSlim(0);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, ThreadsResponseMode: mode),
            NotificationHandler: (notification, _) =>
            {
                if (notification.Params?["uri"]?.GetValue<string>() == "debug://state") notifications.Release();
                return ValueTask.CompletedTask;
            }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "state-reader-failure-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            _ = await ReadLegacyStateResourceAsync(driver, "state-before-reader-failure");
            _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/subscribe",
                new JsonObject { ["uri"] = "debug://state" }, new RequestId("state-reader-failure-subscribe")));
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner))["error"]);
            Assert.True(await notifications.WaitAsync(TimeSpan.FromSeconds(5)), "A reader failure must invalidate the current subscribed state.");
            var error = await Assert.ThrowsAsync<McpProtocolException>(async () => await ReadLegacyStateResourceAsync(driver, "state-after-reader-failure"));
            Assert.Equal(McpErrorCode.InvalidRequest, error.ErrorCode);
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    private static async Task<JsonObject> ReadLegacyStateResourceAsync(ModernMcpProcessDriver driver, string id)
    {
        var result = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read",
            new JsonObject { ["uri"] = "debug://state" }, new RequestId(id)));
        return Assert.IsType<JsonObject>(JsonNode.Parse(result["contents"]![0]!["text"]!.GetValue<string>()));
    }

    [Fact]
    public async Task LegacyCompat_StateResource_RejectsIncompleteSourceEvents()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject
            {
                ["event"] = "loadedSource",
                ["body"] = new JsonObject { ["reason"] = "new", ["source"] = 17 },
            })
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }),
            startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "state-incomplete-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
        }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var error = await Assert.ThrowsAsync<McpProtocolException>(async () => await driver.SendRawRequestAsync(
                "resources/read", new JsonObject { ["uri"] = "debug://state" }, new RequestId("state-incomplete")));
            Assert.Equal(McpErrorCode.InvalidRequest, error.ErrorCode);
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner))["data"]);
        }
        finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
    }

    [Fact]
    public async Task LegacyCompat_StateResource_TracksSourcesThreadsAndSessionReplacement()
    {
        using var signal = new SemaphoreSlim(0);
        static JsonObject Source(string reason, string name) => new()
        {
            ["event"] = "loadedSource",
            ["body"] = new JsonObject
            {
                ["reason"] = reason,
                ["source"] = new JsonObject { ["path"] = "file.cs", ["name"] = name },
            },
        };
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject
            {
                ["event"] = "stopped",
                ["body"] = new JsonObject
                {
                    ["reason"] = "breakpoint",
                    ["threadId"] = 1,
                    ["allThreadsStopped"] = true,
                }
            }),
            ["threads"] = new JsonArray(
            new JsonObject { ["entries"] = new JsonArray(Source("new", "old"), Source("changed", "new")) },
            new JsonObject { ["entries"] = new JsonArray(Source("removed", "new")) })
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() },
            NotificationHandler: (notification, _) =>
            {
                if (notification.Params?["uri"]?.GetValue<string>() == "debug://state") signal.Release();
                return ValueTask.CompletedTask;
            }), startupTimeout: TimeSpan.FromSeconds(10));
        async Task<JsonObject> ReadState(string id)
        {
            var result = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read",
                new JsonObject { ["uri"] = "debug://state" }, new RequestId(id)));
            Assert.Equal("application/json", result["contents"]?[0]?["mimeType"]?.GetValue<string>());
            return Assert.IsType<JsonObject>(JsonNode.Parse(result["contents"]![0]!["text"]!.GetValue<string>()));
        }
        var initial = await ReadState("state-idle");
        Assert.Equal("idle", initial["state"]?.GetValue<string>());
        Assert.Empty(Assert.IsType<JsonObject>(initial["loadedSources"]));
        _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/subscribe",
            new JsonObject { ["uri"] = "debug://state" }, new RequestId("state-subscribe")));
        var owner = new JsonObject { ["muxSessionId"] = "state-resource-owner" };
        var launch = new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true };
        var active = false;
        try
        {
            for (var generation = 0; generation < 2; generation++)
            {
                Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
                active = true;
                Assert.True(await signal.WaitAsync(TimeSpan.FromSeconds(10)));
                while (signal.Wait(0)) { }
                _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
                Assert.True(await signal.WaitAsync(TimeSpan.FromSeconds(10)));
                var observed = await ReadState($"state-active-{generation}");
                Assert.Equal("stopped", observed["state"]?.GetValue<string>());
                Assert.Equal("breakpoint", observed["stopReason"]?.GetValue<string>());
                Assert.Equal(2, observed["threadCount"]?.GetValue<int>());
                Assert.Equal("new", observed["loadedSources"]?["path:file.cs"]?["name"]?.GetValue<string>());
                _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
                Assert.Empty(Assert.IsType<JsonObject>((await ReadState($"state-removed-{generation}"))["loadedSources"]));
                Assert.True(await signal.WaitAsync(TimeSpan.FromSeconds(10)));
                Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
                active = false;
                Assert.Equal("idle", (await ReadState($"state-idle-{generation}"))["state"]?.GetValue<string>());
                while (signal.Wait(0)) { }
            }
            _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/unsubscribe",
                new JsonObject { ["uri"] = "debug://state" }, new RequestId("state-unsubscribe")));
            while (signal.Wait(0)) { }
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.False(await signal.WaitAsync(TimeSpan.FromSeconds(1)));
        }
        finally
        {
            if (active) Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
        }
    }

    [Fact]
    public async Task LegacyCompat_LiveResources_ReadSubscribeAndUnsubscribeObservedEvents()
    {
        var updates = new ConcurrentQueue<string>();
        using var signal = new SemaphoreSlim(0);
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject { ["category"] = "stdout", ["output"] = "initial\n" }),
            ["threads"] = new JsonArray(
                new JsonObject
                {
                    ["entries"] = new JsonArray(
                    new JsonObject { ["category"] = "stderr", ["output"] = "subscribed\n" },
                    new JsonObject { ["event"] = "thread", ["body"] = new JsonObject { ["reason"] = "started", ["threadId"] = 2 } })
                },
                new JsonObject { ["entries"] = new JsonArray() },
                new JsonObject
                {
                    ["entries"] = new JsonArray(
                    new JsonObject { ["category"] = "stdout", ["output"] = "after-unsubscribe\n" },
                    new JsonObject { ["event"] = "thread", ["body"] = new JsonObject { ["reason"] = "exited", ["threadId"] = 2 } })
                }),
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() },
            NotificationHandler: (notification, _) =>
            {
                if (notification.Params?["uri"]?.GetValue<string>() is { } uri)
                {
                    updates.Enqueue(uri);
                    signal.Release();
                }
                return ValueTask.CompletedTask;
            }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "resource-owner" };
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            var list = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/list", new JsonObject(), new RequestId("legacy-resources-list")));
            var resources = Assert.IsType<JsonArray>(list["resources"]);
            var output = Assert.Single(resources, item => item?["uri"]?.GetValue<string>() == "debug://output");
            var threads = Assert.Single(resources, item => item?["uri"]?.GetValue<string>() == "debug://threads");
            Assert.Equal("text/plain", output?["mimeType"]?.GetValue<string>());
            Assert.Equal("application/json", threads?["mimeType"]?.GetValue<string>());
            Assert.True(driver.Client.ServerCapabilities?.Resources?.Subscribe);
            foreach (var uri in new[] { "debug://output", "debug://output", "debug://threads", "debug://threads" })
                _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/subscribe", new JsonObject { ["uri"] = uri }, new RequestId(Guid.NewGuid().ToString("N"))));
            var invalid = await Assert.ThrowsAsync<McpProtocolException>(async () => await driver.SendRawRequestAsync("resources/subscribe", new JsonObject { ["uri"] = "debug://unknown" }, new RequestId("unknown-resource")));
            Assert.Equal(McpErrorCode.InvalidParams, invalid.ErrorCode);
            await Assert.ThrowsAsync<McpProtocolException>(async () => await driver.SendRawRequestAsync("resources/read", new JsonObject { ["uri"] = "debug://unknown" }, new RequestId("unknown-resource-read")));

            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var seen = new List<string>();
            using (var timeout = new CancellationTokenSource(TimeSpan.FromSeconds(10)))
            {
                while (!seen.Contains("debug://output") || !seen.Contains("debug://threads"))
                {
                    await signal.WaitAsync(timeout.Token);
                    while (updates.TryDequeue(out var uri)) seen.Add(uri);
                }
            }
            Assert.All(seen, uri => Assert.Contains(uri, new[] { "debug://output", "debug://threads" }));
            var outputResult = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read", new JsonObject { ["uri"] = "debug://output" }, new RequestId("read-output")));
            var outputContent = Assert.Single(Assert.IsType<JsonArray>(outputResult["contents"]));
            Assert.Equal("debug://output", outputContent?["uri"]?.GetValue<string>());
            Assert.Equal("text/plain", outputContent?["mimeType"]?.GetValue<string>());
            Assert.Equal("initial\nsubscribed\n", outputContent?["text"]?.GetValue<string>());
            var threadResult = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read", new JsonObject { ["uri"] = "debug://threads" }, new RequestId("read-threads")));
            var threadContent = Assert.Single(Assert.IsType<JsonArray>(threadResult["contents"]));
            Assert.Equal("application/json", threadContent?["mimeType"]?.GetValue<string>());
            var threadList = Assert.IsType<JsonArray>(JsonNode.Parse(threadContent!["text"]!.GetValue<string>()));
            Assert.Equal(2, threadList.Count);
            Assert.Equal(1, threadList[0]?["id"]?.GetValue<int>());
            Assert.Equal("worker", threadList[0]?["name"]?.GetValue<string>());
            Assert.Equal(2, threadList[1]?["id"]?.GetValue<int>());
            foreach (var uri in new[] { "debug://output", "debug://threads", "debug://output", "debug://threads" })
                _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/unsubscribe", new JsonObject { ["uri"] = uri }, new RequestId(Guid.NewGuid().ToString("N"))));
            while (updates.TryDequeue(out _)) { }
            while (signal.Wait(0)) { }
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.False(await signal.WaitAsync(TimeSpan.FromSeconds(1)), "No resource updates may be delivered after unsubscribe.");
            var after = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read", new JsonObject { ["uri"] = "debug://output" }, new RequestId("read-output-after")));
            Assert.Equal("initial\nsubscribed\nafter-unsubscribe\n", after["contents"]?[0]?["text"]?.GetValue<string>());
        }
        finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
    }

    [Fact]
    public async Task LegacyCompat_ResourceSubscription_FollowsConnectionAcrossDebugSessions()
    {
        using var signal = new SemaphoreSlim(0);
        var script = new JsonObject
        {
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(new JsonObject { ["category"] = "stdout", ["output"] = "session-output\n" }),
            })
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() },
            NotificationHandler: (notification, _) =>
            {
                if (notification.Params?["uri"]?.GetValue<string>() == "debug://output") signal.Release();
                return ValueTask.CompletedTask;
            }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "resource-session-owner" };
        var launch = new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false, ["stop_at_entry"] = true };
        _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/subscribe", new JsonObject { ["uri"] = "debug://output" }, new RequestId("connection-subscribe")));
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
        var active = true;
        try
        {
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.True(await signal.WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
            active = false;
            await Task.Delay(250);
            while (signal.Wait(0)) { }
            Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", launch, owner))["data"]?["success"]?.GetValue<bool>());
            active = true;
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.True(await signal.WaitAsync(TimeSpan.FromSeconds(10)), "The connection subscription must receive the replacement session's output update.");
            var output = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read", new JsonObject { ["uri"] = "debug://output" }, new RequestId("replacement-output")));
            Assert.Equal("session-output\n", output["contents"]?[0]?["text"]?.GetValue<string>());
        }
        finally
        {
            if (active) Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyCompat_ResourceLaunchPublicationAndClear_OnlyNotifyCommittedChanges(bool refused)
    {
        using var signal = new SemaphoreSlim(0);
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject { ["category"] = "stdout", ["output"] = "launch-output\n" }),
            ["launchRefused"] = refused,
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() },
            NotificationHandler: (notification, _) =>
            {
                if (notification.Params?["uri"]?.GetValue<string>() == "debug://output") signal.Release();
                return ValueTask.CompletedTask;
            }), startupTimeout: TimeSpan.FromSeconds(10));
        var owner = new JsonObject { ["muxSessionId"] = "resource-publication-owner" };
        _ = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/subscribe", new JsonObject { ["uri"] = "debug://output" }, new RequestId("publication-subscribe")));
        var started = await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["pre_build"] = false,
            ["stop_at_entry"] = true,
        }, owner);
        if (refused)
        {
            Assert.NotNull(started["error"]);
            Assert.False(await signal.WaitAsync(TimeSpan.FromSeconds(1)), "A failed start must not publish output updates.");
            var idle = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read", new JsonObject { ["uri"] = "debug://output" }, new RequestId("failed-start-output")));
            Assert.Equal(string.Empty, idle["contents"]?[0]?["text"]?.GetValue<string>());
            return;
        }
        Assert.True(started["data"]?["success"]?.GetValue<bool>());
        try
        {
            Assert.True(await signal.WaitAsync(TimeSpan.FromSeconds(10)), "Committed launch output must notify a pre-start subscriber.");
            var before = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read", new JsonObject { ["uri"] = "debug://output" }, new RequestId("launch-output")));
            Assert.Equal("launch-output\n", before["contents"]?[0]?["text"]?.GetValue<string>());
            while (signal.Wait(0)) { }
            var cleared = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject { ["clear"] = true }, owner);
            Assert.Equal("launch-output\n", cleared["data"]?["output"]?.GetValue<string>());
            Assert.True(await signal.WaitAsync(TimeSpan.FromSeconds(10)));
            var empty = ModernMcpProcessDriver.RequireResult(await driver.SendRawRequestAsync("resources/read", new JsonObject { ["uri"] = "debug://output" }, new RequestId("cleared-output")));
            Assert.Equal(string.Empty, empty["contents"]?[0]?["text"]?.GetValue<string>());
            while (signal.Wait(0)) { }
            _ = await LegacyConsoleCallAsync(driver, "get_output", new JsonObject { ["clear"] = true }, owner);
            Assert.False(await signal.WaitAsync(TimeSpan.FromSeconds(1)), "Clearing an empty buffer must not notify.");
        }
        finally { Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>()); }
    }

    [Fact]
    public async Task ModernRoute_DoesNotAdvertiseLegacyResources()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        Assert.Null(driver.Client.ServerCapabilities?.Resources);
    }

    [Fact]
    public async Task LegacyCompat_DebuggeeActivity_RejectsUnprojectableEndState()
    {
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(new JsonObject { ["event"] = "continued", ["body"] = new JsonObject { ["allThreadsContinued"] = true } }),
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(new JsonObject
                {
                    ["event"] = "stopped",
                    ["body"] = new JsonObject { ["reason"] = "pause", ["allThreadsStopped"] = true }
                })
            })
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "activity-invalid-end-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var activity = LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = 1500 }, owner);
            await Task.Delay(200);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            Assert.Equal("debug session changed during activity window", (await activity)["error"]?.GetValue<string>());
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_DebuggeeActivity_ReportsObservedEventDeltas()
    {
        static JsonObject Event(string name, JsonObject body) => new() { ["event"] = name, ["body"] = body };
        var script = new JsonObject
        {
            ["launch"] = new JsonArray(Event("continued", new JsonObject { ["threadId"] = 1, ["allThreadsContinued"] = true })),
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(
                Event("continued", new JsonObject { ["threadId"] = 1, ["allThreadsContinued"] = true }),
                new JsonObject { ["category"] = "stdout", ["output"] = "activity\n" },
                new JsonObject { ["category"] = "invalid", ["output"] = "ignored\n" },
                new JsonObject { ["category"] = "stdout", ["output"] = "" },
                Event("module", new JsonObject { ["reason"] = "new", ["module"] = new JsonObject { ["id"] = 1, ["name"] = "app" } }),
                Event("module", new JsonObject { ["reason"] = "changed", ["module"] = new JsonObject { ["id"] = 1, ["name"] = "app" } }),
                Event("module", new JsonObject { ["reason"] = "removed", ["module"] = new JsonObject { ["id"] = 1, ["name"] = "app" } }),
                Event("stopped", new JsonObject { ["reason"] = "step", ["threadId"] = 1, ["allThreadsStopped"] = true }))
            })
        };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "activity-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var activity = LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = 1500 }, owner);
            await Task.Delay(200);
            _ = await LegacyConsoleCallAsync(driver, "get_threads", new JsonObject(), owner);
            var result = await activity;
            Assert.Null(result["error"]);
            var data = Assert.IsType<JsonObject>(result["data"]);
            Assert.Equal("running", data["start"]?["state"]?.GetValue<string>());
            Assert.Equal("stopped", data["end"]?["state"]?.GetValue<string>());
            Assert.Equal("stepping", data["end"]?["execState"]?.GetValue<string>());
            Assert.Equal(1, data["deltas"]?["continuedEvents"]?.GetValue<int>());
            Assert.Equal(1, data["deltas"]?["stoppedEvents"]?.GetValue<int>());
            Assert.Equal(1, data["deltas"]?["stepStops"]?.GetValue<int>());
            Assert.Equal(2, data["deltas"]?["outputEvents"]?.GetValue<int>());
            Assert.Equal(3, data["deltas"]?["moduleEvents"]?["total"]?.GetValue<int>());
            Assert.Equal(1, data["deltas"]?["moduleEvents"]?["new"]?.GetValue<int>());
            Assert.Equal(1, data["deltas"]?["moduleEvents"]?["changed"]?.GetValue<int>());
            Assert.Equal(1, data["deltas"]?["moduleEvents"]?["removed"]?.GetValue<int>());
            Assert.Equal(0, data["deltas"]?["traceEntries"]?.GetValue<int>());
            Assert.True(data["observedActivity"]?.GetValue<bool>());
            Assert.Equal(new[] { "continuedEvents", "stoppedEvents", "stepStops", "outputEvents", "moduleEvents.new", "moduleEvents.changed", "moduleEvents.removed" },
                Assert.IsType<JsonArray>(data["activitySignals"]).Select(item => item!.GetValue<string>()));
            Assert.False(data["instructionsExecuted"]?["available"]?.GetValue<bool>());
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject(), owner))["error"]);
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }
    [Fact]
    public async Task LegacyCompat_DebuggeeActivity_RejectsForeignOwnerButAllowsMissingMuxId()
    {
        var script = new JsonObject { ["launch"] = new JsonArray(new JsonObject { ["event"] = "continued", ["body"] = new JsonObject { ["threadId"] = 1, ["allThreadsContinued"] = true } }) };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "activity-owner" };
        var other = new JsonObject { ["muxSessionId"] = "activity-other" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var denied = await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = 1 }, other);
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var anonymous = await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = 1 }, new JsonObject());
            Assert.Null(anonymous["error"]);
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_DebuggeeActivity_AcceptsLaunchWithoutContinuedEvent()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)));
        var owner = new JsonObject { ["muxSessionId"] = "activity-launch-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var state = await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner);
            Assert.Equal("running", state["state"]?.GetValue<string>());
            var activity = await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = 1 }, owner);
            Assert.Null(activity["error"]);
            Assert.Equal("running", activity["data"]?["start"]?["state"]?.GetValue<string>());
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_DebuggeeActivity_PreservesStoppedEventBeforeLaunchAcknowledgement()
    {
        var script = new JsonObject { ["launch"] = new JsonArray(new JsonObject { ["event"] = "stopped", ["body"] = new JsonObject { ["reason"] = "breakpoint", ["threadId"] = 1, ["allThreadsStopped"] = true } }) };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "activity-stopped-launch-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        try
        {
            Assert.Equal("stopped", (await LegacyConsoleCallAsync(driver, "get_debug_state", new JsonObject(), owner))["state"]?.GetValue<string>());
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = 1 }, owner))["error"]);
        }
        finally { await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner); }
    }

    [Fact]
    public async Task LegacyCompat_DebuggeeActivity_ClaimsNamedOwnerAfterAnonymousLaunch()
    {
        var script = new JsonObject { ["launch"] = new JsonArray(new JsonObject { ["event"] = "continued", ["body"] = new JsonObject { ["threadId"] = 1, ["allThreadsContinued"] = true } }) };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString(), ["NETCOREDBG_SESSION_TIMEOUT"] = "0" }));
        var anonymous = new JsonObject();
        var owner = new JsonObject { ["muxSessionId"] = "activity-after-anonymous" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, anonymous))["data"]?["success"]?.GetValue<bool>());
        try
        {
            var activity = await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = 1 }, owner);
            Assert.Null(activity["error"]);
        }
        finally
        {
            // Expire the named claim so cleanup uses the driver's original anonymous launch identity.
            Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), anonymous))["data"]?["success"]?.GetValue<bool>());
        }
    }


    [Fact]
    public async Task LegacyCompat_DebuggeeActivity_ValidatesWindowAndRejectsSessionReplacement()
    {
        var script = new JsonObject { ["launch"] = new JsonArray(new JsonObject { ["event"] = "continued", ["body"] = new JsonObject { ["threadId"] = 1, ["allThreadsContinued"] = true } }) };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "activity-replacement-owner" };
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(new JsonObject(), new RequestId("activity-catalog")));
        var tool = Assert.Single(Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == "debuggee_activity");
        var windowSchema = tool["inputSchema"]?["properties"]?["window_ms"];
        Assert.Equal("integer", windowSchema?["type"]?.GetValue<string>());
        Assert.Equal(1, windowSchema?["minimum"]?.GetValue<int>());
        Assert.Equal(30000, windowSchema?["maximum"]?.GetValue<int>());
        Assert.Equal(1000, windowSchema?["default"]?.GetValue<int>());
        foreach (var value in new JsonNode?[] { JsonValue.Create(true), JsonValue.Create(0), JsonValue.Create(30001), JsonValue.Create(1.5), JsonValue.Create("1000"), null })
            Assert.NotNull((await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = value }, owner))["error"]);
        Assert.Equal("debuggee_activity requires state RUNNING", (await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject(), owner))["error"]?.GetValue<string>());
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        var quiet = await LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject(), owner);
        Assert.Null(quiet["error"]);
        Assert.Equal(1000, quiet["data"]?["windowMs"]?.GetValue<int>());
        Assert.False(quiet["data"]?["observedActivity"]?.GetValue<bool>());
        Assert.Empty(Assert.IsType<JsonArray>(quiet["data"]?["activitySignals"]));
        var activity = LegacyConsoleCallAsync(driver, "debuggee_activity", new JsonObject { ["window_ms"] = 1500 }, owner);
        await Task.Delay(200);
        Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
        var result = await activity;
        Assert.Equal("debug session changed during activity window", result["error"]?.GetValue<string>());
        Assert.Null(result["data"]);
    }

    [Fact]
    public async Task LegacyCompat_DebuggeeActivity_CancellationDoesNotBlockStop()
    {
        var script = new JsonObject { ["launch"] = new JsonArray(new JsonObject { ["event"] = "continued", ["body"] = new JsonObject { ["threadId"] = 1, ["allThreadsContinued"] = true } }) };
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"],
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
            AdditionalEnvironment: new Dictionary<string, string?> { ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = script.ToJsonString() }));
        var owner = new JsonObject { ["muxSessionId"] = "activity-cancel-owner" };
        Assert.True((await LegacyConsoleCallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath, ["pre_build"] = false }, owner))["data"]?["success"]?.GetValue<bool>());
        using var cancel = new CancellationTokenSource();
        var activity = driver.CallToolRawAsync("debuggee_activity", new JsonObject { ["window_ms"] = 30000 }, owner, new RequestId("activity-cancel"), cancel.Token);
        await Task.Delay(200);
        cancel.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () => await activity);
        Assert.True((await LegacyConsoleCallAsync(driver, "stop_debug", new JsonObject(), owner))["data"]?["success"]?.GetValue<bool>());
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
    public void AttachCleanup_OnlyClassifiesExactUnknownToolEnvelopeAsNoDispatch()
    {
        var unknown = new JsonObject
        {
            ["resultType"] = "complete",
            ["isError"] = true,
            ["content"] = new JsonArray(new JsonObject { ["type"] = "text", ["text"] = "Unknown tool: attach_debug" }),
        };
        Assert.True(ModernMcpProcessDriver.IsUnknownAttachToolResult(unknown));
        Assert.False(ModernMcpProcessDriver.IsUnknownAttachToolResult(null));

        var missingCompletion = unknown.DeepClone().AsObject();
        missingCompletion.Remove("resultType");
        Assert.False(ModernMcpProcessDriver.IsUnknownAttachToolResult(missingCompletion));
        var notError = unknown.DeepClone().AsObject();
        notError["isError"] = false;
        Assert.False(ModernMcpProcessDriver.IsUnknownAttachToolResult(notError));
        var structured = unknown.DeepClone().AsObject();
        structured["structuredContent"] = new JsonObject();
        Assert.False(ModernMcpProcessDriver.IsUnknownAttachToolResult(structured));
        var otherTool = unknown.DeepClone().AsObject();
        otherTool["content"]![0]!["text"] = "Unknown tool: start_debug";
        Assert.False(ModernMcpProcessDriver.IsUnknownAttachToolResult(otherTool));
        var extraContent = unknown.DeepClone().AsObject();
        extraContent["content"]!.AsArray().Add(new JsonObject { ["type"] = "text", ["text"] = "extra" });
        Assert.False(ModernMcpProcessDriver.IsUnknownAttachToolResult(extraContent));
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
