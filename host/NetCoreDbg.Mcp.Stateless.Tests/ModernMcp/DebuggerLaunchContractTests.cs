using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class DebuggerLaunchContractTests
{
#pragma warning disable MCP9005 // Exercise roots while older clients remain supported.
    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task StartDebug_MissingProgram_ExplicitOrPinnedRootDoesNotRequestClientRoots(bool explicitRoot)
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var root = await CreateProfileRootAsync(scratch, "pinned-input");
            await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync(options: new ModernMcpStartOptions(
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["NETCOREDBG_PROJECT_ROOT"] = explicitRoot ? null : root,
                    ["MCP_PROJECT_ROOT"] = null,
                }));
            var meta = ModernMcpProcessDriver.CurrentMeta(formElicitation: true);
            meta[MetaKeys.ClientCapabilities]!["roots"] = new JsonObject();
            var arguments = new JsonObject();
            if (explicitRoot) arguments["projectRoot"] = root;
            var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync("tools/call",
                new JsonObject { ["name"] = "start_debug", ["arguments"] = arguments, ["_meta"] = meta },
                new RequestId("pinned-program-input"), timeout: TimeSpan.FromSeconds(10))));
            Assert.Equal("input_required", result["resultType"]?.GetValue<string>());
            var request = Assert.Single(Assert.IsType<JsonObject>(result["inputRequests"]));
            Assert.Equal("elicitation/create", request.Value?["method"]?.GetValue<string>());
            Assert.Empty(await driver.ReadNativeActionsAsync());
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task StartDebug_MissingProgramAndRoots_AreRequestedTogetherAndOneRetryStarts()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var startup = await CreateProfileRootAsync(scratch, "combined-startup");
            var root = await CreateProfileRootAsync(scratch, "combined-client");
            await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync(options: new ModernMcpStartOptions(
                StartupDirectory: startup,
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null }));
            var meta = ModernMcpProcessDriver.CurrentMeta(formElicitation: true);
            meta[MetaKeys.ClientCapabilities]!["roots"] = new JsonObject();
            var initial = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync("tools/call",
                new JsonObject { ["name"] = "start_debug", ["arguments"] = new JsonObject(), ["_meta"] = meta.DeepClone() },
                new RequestId("combined-inputs"), timeout: TimeSpan.FromSeconds(10))));
            Assert.Equal("input_required", initial["resultType"]?.GetValue<string>());
            var requests = Assert.IsType<JsonObject>(initial["inputRequests"]);
            Assert.Equal(2, requests.Count);
            var programRequest = Assert.Single(requests, entry => entry.Value?["method"]?.GetValue<string>() == "elicitation/create");
            var rootsRequest = Assert.Single(requests, entry => entry.Value?["method"]?.GetValue<string>() == "roots/list");
            Assert.Empty(await driver.ReadNativeActionsAsync());
            await Task.Delay(TimeSpan.FromMilliseconds(2300));
            Assert.Empty(await driver.ReadNativeActionsAsync());

            JsonObject Retry(string action) => new()
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject(),
                ["_meta"] = meta.DeepClone(),
                ["inputResponses"] = new JsonObject
                {
                    [programRequest.Key] = new JsonObject { ["action"] = action, ["content"] = new JsonObject { ["program"] = driver.InertProgramPath } },
                    [rootsRequest.Key] = new JsonObject { ["roots"] = new JsonArray(new JsonObject { ["uri"] = new Uri(root).AbsoluteUri }) },
                },
            };
            var declined = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync("tools/call", Retry("decline"),
                new RequestId("combined-declined"), timeout: TimeSpan.FromSeconds(10))));
            Assert.True(declined["isError"]?.GetValue<bool>() ?? false);
            Assert.Equal("START_DEBUG_PROGRAM_REQUIRED", declined["structuredContent"]?["error"]?.GetValue<string>());
            Assert.Empty(await driver.ReadNativeActionsAsync());

            var result = ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(await driver.SendFirstRequestAsync("tools/call", Retry("accept"),
                new RequestId("combined-accepted"), timeout: TimeSpan.FromSeconds(10))));
            Assert.Equal("complete", result["resultType"]?.GetValue<string>());
            Assert.False(result["isError"]?.GetValue<bool>() ?? true);
            var content = Assert.IsType<JsonObject>(result["structuredContent"]);
            Assert.Equal("combined-client", content["launchEnvironment"]?["profile"]?.GetValue<string>());
            var sessionId = Assert.IsType<string>(content["debugSessionId"]?.GetValue<string>());
            await driver.StopSingleControlledSessionAsync(sessionId);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task StartDebug_ModernPerRequestRoots_DoNotLeakBetweenCalls()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var startup = await CreateProfileRootAsync(scratch, "modern-startup");
            var first = await CreateProfileRootAsync(scratch, "modern-first");
            var second = await CreateProfileRootAsync(scratch, "modern-second");
            var calls = 0;
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                StartupDirectory: startup,
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null },
                RootsHandler: _ =>
                {
                    var uri = Interlocked.Increment(ref calls) switch
                    {
                        1 => new Uri(first).AbsoluteUri,
                        2 => new Uri(second).AbsoluteUri,
                        _ => "file://remote.invalid/share",
                    };
                    return ValueTask.FromResult(new ListRootsResult { Roots = [new Root { Uri = uri }] });
                }));
            for (var index = 0; index < 3; index++)
            {
                var meta = ModernMcpProcessDriver.CurrentMeta();
                meta[MetaKeys.ClientCapabilities]!["roots"] = new JsonObject();
                await AssertProfileAsync(driver, index switch { 0 => "modern-first", 1 => "modern-second", _ => "modern-startup" },
                    null, meta, $"modern-root-{index}");
            }
            Assert.Equal(3, calls);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task StartDebug_OperatorPinsAndExplicitRoot_HavePrecedenceAndInvalidPinFailsClosed()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var envRoot = await CreateProfileRootAsync(scratch, "env");
            var otherEnvRoot = await CreateProfileRootAsync(scratch, "other-env");
            var argumentRoot = await CreateProfileRootAsync(scratch, "argument");
            var explicitRoot = await CreateProfileRootAsync(scratch, "explicit");
            var rootsCalls = 0;
            var env = new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = envRoot, ["MCP_PROJECT_ROOT"] = otherEnvRoot };
            await using (var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                AdditionalEnvironment: env, HostArguments: ["--project", argumentRoot], ClientProtocolVersion: "2025-11-25",
                RootsHandler: _ => { Interlocked.Increment(ref rootsCalls); return ValueTask.FromResult(new ListRootsResult { Roots = [new Root { Uri = new Uri(argumentRoot).AbsoluteUri }] }); })))
            {
                await AssertProfileAsync(driver, "env", null, new JsonObject(), "pinned-env");
                await AssertProfileAsync(driver, "explicit", explicitRoot, new JsonObject(), "explicit-over-pin");
                Assert.Equal(0, rootsCalls);
            }

            env["NETCOREDBG_PROJECT_ROOT"] = null;
            await using (var secondary = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                AdditionalEnvironment: env, HostArguments: ["--project", argumentRoot])))
                await AssertProfileAsync(secondary, "other-env", null, ModernMcpProcessDriver.CurrentMeta(), "secondary-env");

            env["MCP_PROJECT_ROOT"] = null;
            await using (var argument = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                AdditionalEnvironment: env, HostArguments: ["--project", argumentRoot])))
                await AssertProfileAsync(argument, "argument", null, ModernMcpProcessDriver.CurrentMeta(), "project-argument");

            env["NETCOREDBG_PROJECT_ROOT"] = Path.Combine(scratch, "missing-private-pin");
            env["MCP_PROJECT_ROOT"] = null;
            await using var invalid = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                AdditionalEnvironment: env, ClientProtocolVersion: "2025-11-25",
                RootsHandler: _ => { Interlocked.Increment(ref rootsCalls); return ValueTask.FromResult(new ListRootsResult { Roots = [new Root { Uri = new Uri(argumentRoot).AbsoluteUri }] }); }));
            var rejected = ModernMcpProcessDriver.RequireResult(await invalid.CallToolRawAsync("start_debug",
                new JsonObject { ["program"] = invalid.InertProgramPath }, new JsonObject(), new RequestId("invalid-pin"), timeout: TimeSpan.FromSeconds(10)));
            Assert.True(rejected["isError"]?.GetValue<bool>() ?? false);
            Assert.Equal("START_DEBUG_PROJECT_UNAVAILABLE", rejected["structuredContent"]?["error"]?.GetValue<string>());
            Assert.DoesNotContain("missing-private-pin", rejected.ToJsonString(), StringComparison.Ordinal);
            Assert.Empty(await invalid.ReadNativeActionsAsync());
            Assert.Equal(0, rootsCalls);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task StartDebug_RootsAreResolvedPerCall_AndInvalidRootsFallBackToStartupDirectory()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var startup = await CreateProfileRootAsync(scratch, "startup");
            var first = await CreateProfileRootAsync(scratch, "first");
            var second = await CreateProfileRootAsync(scratch, "second");
            var calls = 0;
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                StartupDirectory: startup, ClientProtocolVersion: "2025-11-25",
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null },
                RootsHandler: _ =>
                {
                    var root = Interlocked.Increment(ref calls) switch { 1 => first, 2 => second, _ => null };
                    return ValueTask.FromResult(new ListRootsResult
                    {
                        Roots = root is null
                        ? [new Root { Uri = "https://example.invalid/project" }, new Root { Uri = "file://remote.invalid/share" }]
                        : [new Root { Uri = "file://remote.invalid/share" }, new Root { Uri = new Uri(root).AbsoluteUri }]
                    });
                }));
            await AssertProfileAsync(driver, "first", null, new JsonObject(), "roots-first");
            await AssertProfileAsync(driver, "second", null, new JsonObject(), "roots-second");
            await AssertProfileAsync(driver, "startup", null, new JsonObject(), "roots-invalid");
            Assert.Equal(3, calls);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task StartDebug_NoRootsCapability_UsesStartupMarker()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var root = await CreateProfileRootAsync(scratch, "marker");
            var child = Directory.CreateDirectory(Path.Combine(root, "child")).FullName;
            await File.WriteAllTextAsync(Path.Combine(root, "example.sln"), "");
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                StartupDirectory: child, HostArguments: ["--project-from-cwd"],
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null }));
            await AssertProfileAsync(driver, "marker", null, ModernMcpProcessDriver.CurrentMeta(), "no-roots");
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task StartDebug_RootsTimeoutFallsBackToStartupDirectory()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var startup = await CreateProfileRootAsync(scratch, "timeout");
            var calls = 0;
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                StartupDirectory: startup, ClientProtocolVersion: "2025-11-25",
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PROJECT_ROOT"] = null, ["MCP_PROJECT_ROOT"] = null },
                RootsHandler: async ct =>
                {
                    Interlocked.Increment(ref calls);
                    await Task.Delay(TimeSpan.FromSeconds(3), ct);
                    return new ListRootsResult { Roots = [] };
                }));
            await AssertProfileAsync(driver, "timeout", null, new JsonObject(), "roots-timeout");
            Assert.Equal(1, calls);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    private static async Task<string> CreateProfileRootAsync(string parent, string name)
    {
        var root = Directory.CreateDirectory(Path.Combine(parent, name)).FullName;
        var profile = new JsonObject { ["defaultProfile"] = name, ["profiles"] = new JsonObject { [name] = new JsonObject { ["env"] = new JsonObject { ["NCD_PROJECT_ROOT_TEST"] = "private-profile-value" } } } };
        await File.WriteAllTextAsync(Path.Combine(root, ".netcoredbg-mcp.launch.json"), profile.ToJsonString());
        return root;
    }

    private static async Task AssertProfileAsync(ModernMcpProcessDriver driver, string expected, string? explicitRoot, JsonObject meta, string id)
    {
        var arguments = new JsonObject { ["program"] = driver.InertProgramPath };
        if (explicitRoot is not null) arguments["projectRoot"] = explicitRoot;
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", arguments, meta, new RequestId(id), timeout: TimeSpan.FromSeconds(10)));
        Assert.False(result["isError"]?.GetValue<bool>() ?? true);
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal(expected, content["launchEnvironment"]?["profile"]?.GetValue<string>());
        Assert.DoesNotContain("private-profile-value", result.ToJsonString(), StringComparison.Ordinal);
        var sessionId = Assert.IsType<string>(content["debugSessionId"]?.GetValue<string>());
        var stopped = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("stop_debug", new JsonObject { ["debugSessionId"] = sessionId }, meta, new RequestId(id + "-stop"), timeout: TimeSpan.FromSeconds(10)));
        Assert.False(stopped["isError"]?.GetValue<bool>() ?? true);
    }
#pragma warning restore MCP9005

    [Fact]
    public async Task StartDebug_ProjectProfile_MergesInheritedAndDirectEnvironmentWithoutLeakingValues()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true),
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["CONTROLLED_DAP_INSPECT_ENV"] = "1",
                ["NCD_LAUNCH_TEST_INHERITED"] = "inherited-secret",
                ["NCD_LAUNCH_TEST_REMOVE"] = "removed-secret",
            }), startupTimeout: TimeSpan.FromSeconds(10));
        var root = Assert.IsType<string>(Path.GetDirectoryName(driver.InertProgramPath));
        var config = new JsonObject
        {
            ["defaultProfile"] = "demo",
            ["profiles"] = new JsonObject
            {
                ["demo"] = new JsonObject
                {
                    ["inherit"] = new JsonArray("NCD_LAUNCH_TEST_INHERITED"),
                    ["env"] = new JsonObject
                    {
                        ["NCD_LAUNCH_TEST_REMOVE"] = null,
                        ["NCD_LAUNCH_TEST_VALUE"] = "profile-secret",
                        ["NCD_LAUNCH_TEST_NEW"] = "profile-value",
                    },
                },
            },
        };
        await File.WriteAllTextAsync(Path.Combine(root, ".netcoredbg-mcp.launch.json"), config.ToJsonString());
        var started = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["projectRoot"] = root,
            ["env"] = new JsonObject { ["NCD_LAUNCH_TEST_VALUE"] = "direct-secret", ["NCD_LAUNCH_TEST_NEW"] = "" },
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("profile-start")));
        Assert.False(started["isError"]?.GetValue<bool>() ?? true);
        var content = Assert.IsType<JsonObject>(started["structuredContent"]);
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        var metadata = Assert.IsType<JsonObject>(content["launchEnvironment"]);
        Assert.Equal("project-launch-profile", metadata["source"]?.GetValue<string>());
        Assert.Equal("demo", metadata["profile"]?.GetValue<string>());
        Assert.Equal(4, metadata["appliedCount"]?.GetValue<int>());
        Assert.DoesNotContain("inherited-secret", started.ToJsonString(), StringComparison.Ordinal);
        Assert.DoesNotContain("profile-secret", started.ToJsonString(), StringComparison.Ordinal);
        Assert.DoesNotContain("direct-secret", started.ToJsonString(), StringComparison.Ordinal);
        var descendant = Assert.Single(await driver.ReadFixtureTranscriptAsync(), static entry => entry.Kind == "descendant");
        using var environment = await ReadDescendantEnvironmentAsync(Assert.IsType<int>(descendant.ProcessId));
        Assert.Equal("inherited-secret", environment.RootElement.GetProperty("unchanged").GetString());
        Assert.Equal("direct-secret", environment.RootElement.GetProperty("overridden").GetString());
        Assert.Equal("", environment.RootElement.GetProperty("added").GetString());
        Assert.Equal(System.Text.Json.JsonValueKind.Null, environment.RootElement.GetProperty("removed").ValueKind);
        await driver.StopSingleControlledSessionAsync(Assert.IsType<string>(content["debugSessionId"]?.GetValue<string>()));
    }

    [Fact]
    public async Task StartDebug_InvalidProjectProfiles_DoNotStartTheAdapter()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(10));
        var root = Assert.IsType<string>(Path.GetDirectoryName(driver.InertProgramPath));
        var profilePath = Path.Combine(root, ".netcoredbg-mcp.launch.json");
        async Task AssertRejectedAsync(string id, string projectRoot, string expectedError, string? profileName = null)
        {
            var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["projectRoot"] = projectRoot,
                ["launchProfile"] = profileName,
            }, ModernMcpProcessDriver.CurrentMeta(), new RequestId(id)));
            Assert.True(result["isError"]?.GetValue<bool>() ?? false);
            var content = Assert.IsType<JsonObject>(result["structuredContent"]);
            Assert.Equal(expectedError, content["error"]?.GetValue<string>());
            Assert.False(content.ContainsKey("debugSessionId"));
            StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        }

        await AssertRejectedAsync("profile-missing-root", root + ".missing", "START_DEBUG_PROJECT_UNAVAILABLE");
        await AssertRejectedAsync("profile-missing-file", root, "START_DEBUG_LAUNCH_PROFILE_INVALID", "requested");
        await File.WriteAllTextAsync(profilePath, "{ invalid json");
        await AssertRejectedAsync("profile-malformed", root, "START_DEBUG_LAUNCH_PROFILE_INVALID");
        await File.WriteAllTextAsync(profilePath, """{"profiles":{"default":{"inherit":["NCD_LAUNCH_TEST_ABSENT_73F2"]}}}""");
        await AssertRejectedAsync("profile-missing-inherited", root, "START_DEBUG_LAUNCH_PROFILE_INVALID");
        await File.WriteAllTextAsync(profilePath, """{"profiles":{"default":{"env":{"NETCOREDBG_MCP_NATIVE_SCENE_PROBE_NONCE":"private-value"}}}}""");
        await AssertRejectedAsync("profile-reserved", root, "START_DEBUG_LAUNCH_PROFILE_INVALID");
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }

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
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-env-first"), timeout: TimeSpan.FromSeconds(10)));
        Assert.False(first["isError"]?.GetValue<bool>() ?? true, "Valid environment overrides must start a session.");
        var firstId = Assert.IsType<string>(first["structuredContent"]?["debugSessionId"]?.GetValue<string>());
        var second = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-env-second"), timeout: TimeSpan.FromSeconds(10)));
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
            new JsonObject { ["projectRoot"] = 42 },
            new JsonObject { ["projectRoot"] = "" },
            new JsonObject { ["launchProfile"] = true },
            new JsonObject { ["launchProfile"] = "bad\0name" },
            new JsonObject { ["justMyCode"] = "false" },
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
            ["justMyCode"] = null,
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
        Assert.False(arguments.TryGetProperty("justMyCode", out _));
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
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId("launch-options-empty"), timeout: TimeSpan.FromSeconds(10)));
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
