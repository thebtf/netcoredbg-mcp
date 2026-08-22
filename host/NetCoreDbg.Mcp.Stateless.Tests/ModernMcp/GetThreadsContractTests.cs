using System.Text;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class GetThreadsContractTests
{
    private const string Tool = "get_threads";

    public static TheoryData<string, JsonObject?> InvalidArguments => new()
    {
        { "missing_arguments", null },
        { "omitted_debug_session_id", new JsonObject() },
        { "non_string", new JsonObject { ["debugSessionId"] = JsonValue.Create(17) } },
        { "empty", new JsonObject { ["debugSessionId"] = string.Empty } },
        { "whitespace_only", new JsonObject { ["debugSessionId"] = " \t" } },
        { "extra_field", new JsonObject { ["debugSessionId"] = "opaque", ["unexpected"] = true } },
    };

    [Fact]
    public async Task ToolsList_GetThreadsPublishesClosedMinLengthOneInputAfterStopDebug()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        // Act
        var result = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("threads-catalog")));

        // Assert
        var tools = Assert.IsType<JsonArray>(result["tools"]);
        Assert.Equal(
            [
                "start_debug",
                "get_debug_state",
                "stop_debug",
                Tool,
                "get_ui_probe_capabilities",
                "capture_visual_evidence",
                "read_capture_artifact",
                "wait_for_ui_stable",
                "capture_element_snapshot",
                "capture_native_scene",
            ],
            tools.Select(static tool => tool?["name"]?.GetValue<string>()));
        var schema = Assert.IsType<JsonObject>(Assert.IsType<JsonObject>(tools[3])["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId"], Assert.IsType<JsonArray>(schema["required"]).Select(static value => value?.GetValue<string>()));
        var sessionId = Assert.IsType<JsonObject>(schema["properties"])["debugSessionId"];
        Assert.Equal("string", sessionId?["type"]?.GetValue<string>());
        Assert.Equal(1, sessionId?["minLength"]?.GetValue<int>());
    }

    [Theory]
    [MemberData(nameof(InvalidArguments))]
    public async Task GetThreads_InvalidInput_ReturnsCompleteInvalidArgumentsWithoutDapIo(string scenario, JsonObject? arguments)
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var before = await driver.ReadNativeActionsAsync();

        // Act
        var response = await driver.CallToolRawAsync(
            Tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"threads-invalid-{scenario}"));

        // Assert
        var content = AssertApplicationEnvelope(response, isError: true);
        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(Tool, content["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("short")]
    [InlineData("!")]
    [InlineData("not-a-capability")]
    public async Task GetThreads_UnavailableOpaqueToken_ReturnsCompleteNotFoundWithoutDapIo(string token)
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var before = await driver.ReadNativeActionsAsync();

        // Act
        var response = await driver.CallToolRawAsync(
            Tool,
            new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"threads-unavailable-{token}"));

        // Assert
        AssertNotFound(AssertApplicationEnvelope(response, isError: true));
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task GetThreads_LiveSession_ReturnsBoundedNormalizedSuccessAndKeepsSessionUsable()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads("success"));
        var debugSessionId = await StartSessionAsync(driver, "threads-success-start");
        var before = await driver.ReadNativeActionsAsync();

        // Act
        var response = await driver.CallToolRawAsync(
            Tool,
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("threads-success"));

        // Assert
        var content = AssertApplicationEnvelope(response, isError: false);
        Assert.Equal("threads_success", content["kind"]?.GetValue<string>());
        var threads = Assert.IsType<JsonArray>(content["threads"]);
        Assert.Collection(
            threads,
            thread => AssertThread(thread, 1, "worker"),
            thread => AssertThread(thread, 2, "worker"));
        Assert.Single((await driver.ReadNativeActionsAsync()).Skip(before.Count), static action => action.Command == "threads");
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("threads-success-state")), isError: false);
        Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetThreads_DapRefusal_ReturnsRedactedRefusalAndKeepsSessionUsable()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads("refused"));
        var debugSessionId = await StartSessionAsync(driver, "threads-refusal-start");

        // Act
        var response = await driver.CallToolRawAsync(
            Tool,
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("threads-refusal"));

        // Assert
        var content = AssertApplicationEnvelope(response, isError: true);
        Assert.Equal("dap_threads_refused", content["kind"]?.GetValue<string>());
        Assert.Equal("DAP_THREADS_REFUSED", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.False(content.ContainsKey("body"));
        Assert.False(content.ContainsKey("message"));
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("threads-refusal-state")), isError: false);
        Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
    }

    [Theory]
    [InlineData("wrong-command")]
    [InlineData("malformed-body")]
    [InlineData("reader-failure")]
    public async Task GetThreads_ProtocolFailure_ReturnsRedactedProtocolErrorAndEvictsToken(string fixtureMode)
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads(fixtureMode));
        var debugSessionId = await StartSessionAsync(driver, $"threads-{fixtureMode}-start");

        // Act
        var response = await driver.CallToolRawAsync(
            Tool,
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"threads-{fixtureMode}"));

        // Assert
        AssertProtocolError(AssertApplicationEnvelope(response, isError: true));
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"threads-{fixtureMode}-state")), isError: true));
    }

    [Fact]
    public async Task GetThreads_Timeout_ReturnsRedactedProtocolErrorAndEvictsToken()
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads("timeout"));
        var debugSessionId = await StartSessionAsync(driver, "threads-timeout-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        var response = driver.CallToolRawAsync(
            Tool,
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("threads-timeout"),
            timeout: TimeSpan.FromSeconds(35));
        await driver.WaitForThreadsRequestAsync(observation.Token);
        await driver.WaitForFixtureRecordAsync("threads-response-timeout", observation.Token);

        // Act
        var result = await response;

        // Assert
        AssertProtocolError(AssertApplicationEnvelope(result, isError: true));
        AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("threads-timeout-state")), isError: true));
        Assert.Equal(
            ["terminate", "disconnect"],
            (await driver.ReadNativeActionsAsync())
                .Where(static action => action.Command is "terminate" or "disconnect")
                .Select(static action => action.Command));
    }

    [Theory]
    [InlineData("max-threads", false, 256, null)]
    [InlineData("too-many-threads", true, 0, null)]
    [InlineData("max-name-bytes", false, 1, null)]
    [InlineData("too-many-name-bytes", true, 0, null)]
    [InlineData("structured-content-at-limit", false, 256, 262144)]
    [InlineData("structured-content-over-limit", true, 0, null)]
    public async Task GetThreads_BoundaryResponse_ReturnsExactSuccessOrProtocolError(
        string fixtureMode,
        bool isError,
        int expectedThreadCount,
        int? expectedStructuredContentBytes)
    {
        // Arrange
        await using var driver = await ModernMcpProcessDriver.StartAsync(Threads(fixtureMode));
        var debugSessionId = await StartSessionAsync(driver, $"threads-{fixtureMode}-start");

        // Act
        var response = await driver.CallToolRawAsync(
            Tool,
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"threads-{fixtureMode}"));

        // Assert
        var content = AssertApplicationEnvelope(response, isError);
        if (isError)
        {
            AssertProtocolError(content);
            AssertNotFound(AssertApplicationEnvelope(await driver.CallToolRawAsync(
                "get_debug_state",
                new JsonObject { ["debugSessionId"] = debugSessionId },
                ModernMcpProcessDriver.CurrentMeta(),
                new RequestId($"threads-{fixtureMode}-state")), isError: true));
            return;
        }

        Assert.Equal("threads_success", content["kind"]?.GetValue<string>());
        Assert.Equal(expectedThreadCount, Assert.IsType<JsonArray>(content["threads"]).Count);
        if (expectedStructuredContentBytes is { } expected)
        {
            Assert.Equal(expected, Encoding.UTF8.GetByteCount(content.ToJsonString()));
        }
    }

    private static ModernMcpStartOptions Threads(string responseMode) => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(
            SuppressLifecycleEvents: true,
            ThreadsResponseMode: responseMode));

    private static async Task<string> StartSessionAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId)), isError: false);
        Assert.Equal("start_debug_success", content["kind"]?.GetValue<string>());
        return Assert.IsType<string>(content["debugSessionId"]?.GetValue<string>());
    }

    private static JsonObject AssertApplicationEnvelope(JsonRpcResponse response, bool isError)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() == true);
        var structuredContent = Assert.IsType<JsonObject>(result["structuredContent"]);
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", text["type"]?.GetValue<string>());
        Assert.Equal(structuredContent.ToJsonString(), text["text"]?.GetValue<string>());
        return structuredContent;
    }

    private static void AssertThread(JsonNode? node, int expectedId, string expectedName)
    {
        var thread = Assert.IsType<JsonObject>(node);
        Assert.Equal(expectedId, Assert.IsAssignableFrom<JsonValue>(thread["id"]).GetValue<int>());
        Assert.Equal(expectedName, Assert.IsAssignableFrom<JsonValue>(thread["name"]).GetValue<string>());
    }

    private static void AssertNotFound(JsonObject content)
    {
        Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static void AssertProtocolError(JsonObject content)
    {
        Assert.Equal("dap_threads_protocol_error", content["kind"]?.GetValue<string>());
        Assert.Equal("DAP_THREADS_PROTOCOL_ERROR", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.False(content.ContainsKey("body"));
    }
}
