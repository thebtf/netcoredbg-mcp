using System.Text.Json.Nodes;
using System.Text.Json;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
public sealed class CapabilityLifecycleContractTests
{
    [Fact]
    public async Task LiveHost_ResolvesOnlyExplicitOpaqueTokensAcrossIndependentInterleavedRequests()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var tokens = new List<string>
        {
            await StartAsync(driver, new RequestId("creator-a"), Meta("creator-a")),
            await StartAsync(driver, new RequestId("creator-b"), Meta("creator-b")),
        };
        for (var creator = 2; creator < 8; creator++)
        {
            tokens.Add(await StartAsync(driver, new RequestId($"creator-{creator}"), Meta($"creator-{creator}")));
        }

        Assert.All(tokens, static token =>
        {
            Assert.False(string.IsNullOrEmpty(token));
            Assert.True(token.Length >= 32);
        });
        Assert.Equal(tokens.Count, tokens.Distinct(StringComparer.Ordinal).Count());
        var first = tokens[0];
        var second = tokens[1];

        var firstState = Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = first },
            Meta("caller-b"),
            new RequestId("independent-b")));
        var secondState = Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = second },
            Meta("caller-a"),
            new RequestId("independent-a")));
        var firstAgain = Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = first },
            Meta("caller-c"),
            new RequestId("interleaved-c")));

        AssertSuccess(firstState, "debug_state_success");
        AssertSuccess(secondState, "debug_state_success");
        AssertSuccess(firstAgain, "debug_state_success");
        Assert.Equal(first, firstState["debugSessionId"]?.GetValue<string>());
        Assert.Equal(second, secondState["debugSessionId"]?.GetValue<string>());
        Assert.Equal(first, firstAgain["debugSessionId"]?.GetValue<string>());

        var requests = driver.Requests.Where(static request => request.Method == "tools/call").ToArray();
        Assert.Contains(requests, request => request.Id == new RequestId("creator-a"));
        Assert.Contains(requests, request => request.Id == new RequestId("creator-b"));
        Assert.Contains(requests, request => request.Id == new RequestId("independent-a"));
        Assert.Contains(requests, request => request.Id == new RequestId("independent-b"));
    }

    [Fact]
    public async Task StopDebug_AtomicallyAllowsOneWinnerAndOneNativeCleanup()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var token = await StartAsync(driver, new RequestId("atomic-start"));
        var calls = Enumerable.Range(0, 4)
            .Select(index => driver.CallToolRawAsync(
                "stop_debug",
                new JsonObject { ["debugSessionId"] = token },
                Meta(),
                new RequestId($"atomic-stop-{index}")))
            .ToArray();

        var responses = (await Task.WhenAll(calls)).Select(Structured).ToArray();
        var winner = Assert.Single(responses, static content => Kind(content) == "stop_debug_success");
        AssertSuccess(winner, "stop_debug_success");
        var losers = responses.Where(static content => Kind(content) != "stop_debug_success").ToArray();
        Assert.Equal(3, losers.Length);
        foreach (var loser in losers)
        {
            AssertNotFound(loser);
        }

        AssertNotFound(Structured(await driver.CallToolRawAsync(
            "stop_debug",
            new JsonObject { ["debugSessionId"] = token },
            Meta(),
            new RequestId("atomic-later"))));

        var actions = await driver.ReadNativeActionsAsync();
        Assert.Equal(1, actions.Count(static action => action.Command == "terminate"));
        Assert.Equal(1, actions.Count(static action => action.Command == "disconnect"));
    }

    [Fact]
    public async Task GetDebugState_UsesOneUsabilityObservationPerRequest()
    {
        await using var session = await NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionContractDriver.StartAsync(
            new NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.FixtureConfiguration(),
            "D:\\fixtures\\program.dll",
            TimeSpan.FromSeconds(2),
            TimeSpan.FromSeconds(2),
            TimeSpan.FromMilliseconds(300),
            CancellationToken.None);
        var evaluatorCalls = 0;

        var result = await session.GetStateThroughRegistryAsync(_ => evaluatorCalls++ == 0);

        Assert.Equal(
            (Kind: "debug_state_success", TokenRetained: true, AdapterAlive: true, EvaluatorCalls: 1),
            (result.Kind, result.TokenRetained, result.AdapterAlive, EvaluatorCalls: evaluatorCalls));
    }

    [Fact]
    public async Task InvalidOrUnusableTokens_AreUniformNotFoundOrInvalidArgumentsWithoutProhibitedNativeAction()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        await AssertNoNativeActionAsync(driver, async () => AssertNotFound(Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = new string('r', 32) },
            Meta(),
            new RequestId("random")))));
        await AssertNoNativeActionAsync(driver, async () => AssertNotFound(Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = new string('!', 32) },
            Meta(),
            new RequestId("malformed")))));
        await AssertNoNativeActionAsync(driver, async () => AssertNotFound(Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = "short" },
            Meta(),
            new RequestId("short")))));
        await AssertNoNativeActionAsync(driver, async () => AssertNotFound(Structured(await driver.CallToolRawAsync(
            "stop_debug",
            new JsonObject(),
            Meta(),
            new RequestId("missing")))));
        await AssertNoNativeActionAsync(driver, () => AssertInvalidArgumentsAsync(driver, "get_debug_state", new JsonObject
        {
            ["debugSessionId"] = new string('x', 32),
            ["unexpected"] = true,
        }, "state-extra"));
        await AssertNoNativeActionAsync(driver, () => AssertInvalidArgumentsAsync(driver, "stop_debug", new JsonObject
        {
            ["debugSessionId"] = new string('x', 32),
            ["unexpected"] = true,
        }, "stop-extra"));

        var stoppedToken = await StartAsync(driver, new RequestId("stopped-start"));
        _ = Structured(await driver.CallToolRawAsync(
            "stop_debug",
            new JsonObject { ["debugSessionId"] = stoppedToken },
            Meta(),
            new RequestId("stopped-stop")));
        await AssertNoNativeActionAsync(driver, async () => AssertNotFound(Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = stoppedToken },
            Meta(),
            new RequestId("stopped-state")))));

        var unavailableToken = await StartAsync(driver, new RequestId("unavailable-start"));
        var actionsBeforeTermination = await driver.ReadNativeActionsAsync();
        await driver.TerminateControlledAdapterAsync();
        AssertNotFound(Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = unavailableToken },
            Meta(),
            new RequestId("native-unavailable"))));
        var actionsAfterUnavailableState = await driver.ReadNativeActionsAsync();
        Assert.Equal(NativeActionCount(actionsBeforeTermination), NativeActionCount(actionsAfterUnavailableState));
    }

    [Fact]
    public async Task PriorProcessToken_IsUniformNotFoundWithoutNativeAction()
    {
        string token;
        await using (var prior = await ModernMcpProcessDriver.StartAsync())
        {
            token = await StartAsync(prior, new RequestId("prior-start"));
        }

        await using var current = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(PriorProcessToken: token));
        await AssertNoNativeActionAsync(current, async () => AssertNotFound(Structured(await current.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = token },
            Meta(),
            new RequestId("prior-process")))));
    }

    [Fact]
    public async Task GetDebugState_UnusableSessionCleanupFailure_ReturnsUniformCompleteNotFound()
    {
        var observation = await ModernMcpRegistryContractDriver.ObserveUnusableSessionCleanupFailureAsync();

        Assert.Equal(1, observation.CleanupAttempts);
        Assert.False(observation.TokenRetained);
        Assert.Equal("complete", observation.ResultType);
        Assert.True(observation.IsError);
        Assert.Equal("debug_session_not_found", observation.UnusableContent.GetProperty("kind").GetString());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", observation.UnusableContent.GetProperty("error").GetString());
        Assert.Equal(observation.MissingContent.GetRawText(), observation.UnusableContent.GetRawText());
    }

    [Fact]
    public void Quickstart_RetainedPythonConsumerUsesBuiltWheelInDisposableEnvironment()
    {
        var quickstart = File.ReadAllText(Path.Combine(
            RepositoryLayout.Root,
            "specs",
            "001-mcp-stateless-strangler",
            "quickstart.md"));

        Assert.Contains("uv build --wheel --clear --out-dir $wheelDirectory", quickstart, StringComparison.Ordinal);
        Assert.Contains("uv venv --clear --no-project $consumerEnvironment", quickstart, StringComparison.Ordinal);
        Assert.Contains("uv pip install --python $consumerPython $wheel", quickstart, StringComparison.Ordinal);
        Assert.Contains("& $consumerPython .agent/tmp/t001-retained-python-consumer.py", quickstart, StringComparison.Ordinal);
        Assert.DoesNotContain("uv sync --locked --project .", quickstart, StringComparison.Ordinal);
        Assert.DoesNotContain("uv run --no-sync --project . python .agent/tmp/t001-retained-python-consumer.py", quickstart, StringComparison.Ordinal);
    }

    [Fact]
    public async Task ClosingOfficialStdioClient_ReportsBoundedCandidateTransportCompletionOnly()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        _ = await StartAsync(driver, new RequestId("close-start"));

        using var cancellation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        var completion = await driver.CloseClientAsync(cancellation.Token);

        Assert.NotNull(completion);
        Assert.True(completion.ExitCode.HasValue);
        var toolRequests = driver.Requests.Where(static request => request.Method == "tools/call").ToArray();
        Assert.Single(toolRequests);
    }

    private static JsonObject Meta(string? clientName = null)
    {
        var meta = ModernMcpProcessDriver.CurrentMeta();
        if (clientName is not null)
        {
            meta[MetaKeys.ClientInfo] = new JsonObject { ["name"] = clientName, ["version"] = "1.0" };
        }

        return meta;
    }

    private static async Task<string> StartAsync(ModernMcpProcessDriver driver, RequestId id, JsonObject? meta = null)
    {
        var content = Structured(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            meta ?? Meta(),
            id));
        AssertSuccess(content, "start_debug_success");
        return Assert.IsType<string>(content["debugSessionId"]?.GetValue<string>());
    }
    private static JsonObject Structured(JsonRpcResponse response)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal(Kind(content) == "debug_session_not_found", result["isError"]?.GetValue<bool>() == true);
        return content;
    }

    private static async Task AssertInvalidArgumentsAsync(
        ModernMcpProcessDriver driver,
        string tool,
        JsonObject arguments,
        string requestId)
    {
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(tool, arguments, Meta(), new RequestId(requestId)));
        Assert.True(result["isError"]?.GetValue<bool>() == true);
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal("invalid_tool_arguments", Kind(content));
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(tool, content["tool"]?.GetValue<string>());
    }

    private static async Task AssertNoNativeActionAsync(ModernMcpProcessDriver driver, Func<Task> assertion)
    {
        var before = await driver.ReadNativeActionsAsync();
        await assertion();
        var after = await driver.ReadNativeActionsAsync();
        Assert.Equal(NativeActionCount(before), NativeActionCount(after));
    }

    private static int NativeActionCount(IEnumerable<ModernNativeAction> actions) =>
        actions.Count();

    private static void AssertSuccess(JsonObject content, string expectedKind)
    {
        Assert.Equal(expectedKind, Kind(content));
        Assert.Null(content["error"]);
    }

    private static void AssertNotFound(JsonObject content)
    {
        Assert.Equal("debug_session_not_found", Kind(content));
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static string? Kind(JsonObject content) => content["kind"]?.GetValue<string>();
}
