using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Client;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;


[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
public sealed class ModernProtocolContractTests
{
    private const string ValidProgram = "controlled-program.dll";

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

        AssertCompleteStartEnvelope(ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(message)));
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
    public async Task ToolsList_ReturnsExactlyTheOrderedElevenToolCatalogWithRuntimeSchemas()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();

        var response = await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("catalog"));

        AssertCatalog(ModernMcpProcessDriver.RequireResult(response));
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

    [Fact]
    public async Task FailedSdkInitialization_ReportsBoundedStdioCompletionWithoutConstructingDriver()
    {
        var candidate = TestOutputPathResolver.ResolveProcess(
            RepositoryLayout.ControlledAdapterDirectory,
            "ControlledDapAdapter");
        candidate.Arguments.Add("--invalid-mcp-bootstrap-option");

        var exception = await Assert.ThrowsAsync<ModernMcpProcessStartException>(
            () => ModernMcpProcessDriver.StartAsync(
                new ModernMcpStartOptions(CandidateProcess: candidate)));

        Assert.IsType<ClientTransportClosedException>(exception.InnerException);
        Assert.Equal(ModernMcpProcessStartFailureCategory.TransportClosed, exception.Failure.Category);
        Assert.True(exception.Failure.CompletionObserved);
        Assert.NotNull(exception.Failure.ProcessId);
        Assert.True(exception.Failure.ExitCode.HasValue);
        Assert.Contains(
            exception.Failure.StandardErrorTail,
            static line => line.Contains("Unknown fixture option", StringComparison.Ordinal));
        Assert.False(exception.Failure.StandardErrorTruncated);
    }

    [Fact]
    public void FailedStartStandardErrorCollector_BoundsLinesAndCharacters()
    {
        var collector = new ModernMcpStandardErrorTail();
        for (var index = 0; index < 11; index++)
        {
            collector.Add($"{index}:{new string('x', 600)}");
        }

        var snapshot = collector.Snapshot();

        Assert.True(snapshot.Truncated);
        Assert.Equal(10, snapshot.Lines.Count);
        Assert.All(snapshot.Lines, static line => Assert.InRange(line.Length, 1, 512));
    }

    [Theory]
    [InlineData(null, 2_000)]
    [InlineData("4500", 4_500)]
    public void CoverageStartupTimeout_UsesDefaultOrValidatedOverride(
        string? rawMilliseconds,
        int expectedMilliseconds)
    {
        Assert.Equal(
            TimeSpan.FromMilliseconds(expectedMilliseconds),
            ModernMcpProcessDriver.ResolveCoverageStartupTimeout(rawMilliseconds));
    }

    [Theory]
    [InlineData("1_999")]
    [InlineData("10_001")]
    [InlineData("not-a-number")]
    public void CoverageStartupTimeout_RejectsInvalidOverride(string rawMilliseconds)
    {
        Assert.Throws<InvalidOperationException>(
            () => ModernMcpProcessDriver.ResolveCoverageStartupTimeout(rawMilliseconds));
    }

    [Fact]
    public void CoverageStartupClientOptions_PinPerRequestProtocolAndOneDeadline()
    {
        var timeout = TimeSpan.FromSeconds(10);

        var options = ModernMcpProcessDriver.CreateClientOptions(timeout);

        Assert.Equal(ModernMcpProcessDriver.CurrentProtocolVersion, options.ProtocolVersion);
        Assert.Equal(timeout, options.InitializationTimeout);
        Assert.Equal(timeout, options.DiscoverProbeTimeout);
    }

    [Fact]
    public async Task SdkStartupDeadline_ReportsDriverTimingWithoutCompletionFacts()
    {
        var expectedDeadline = ModernMcpProcessDriver.ResolveCoverageStartupTimeout(
            Environment.GetEnvironmentVariable(
                ModernMcpProcessDriver.CoverageStartupTimeoutEnvironmentVariable));
        var candidate = TestOutputPathResolver.ResolveProcess(
            RepositoryLayout.ControlledAdapterDirectory,
            "ControlledDapAdapter");
        candidate.Arguments.Add("--controlled-dap-descendant");

        var exception = await Assert.ThrowsAsync<ModernMcpPhaseTimeoutException>(
            () => ModernMcpProcessDriver.StartAsync(
                new ModernMcpStartOptions(CandidateProcess: candidate)));

        Assert.Equal(ModernMcpTimeoutPhase.SdkStartup, exception.Failure.Phase);
        Assert.Equal("test_driver", exception.Failure.Owner);
        Assert.Equal(expectedDeadline, exception.Failure.Deadline);
        Assert.True(exception.Failure.Elapsed >= TimeSpan.Zero);
        Assert.Null(exception.Failure.ToolName);
        Assert.Null(exception.Failure.Method);
        Assert.Null(exception.Failure.RequestId);
        Assert.False(exception.Failure.CompletionObserved);
        Assert.Null(exception.Failure.ProcessId);
        Assert.Null(exception.Failure.ExitCode);
        Assert.Empty(exception.Failure.StandardErrorTail);
    }

    [Fact]
    public async Task CallerCancelledStartup_RethrowsWithoutDriverTimingDiagnostic()
    {
        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();

        var exception = await Assert.ThrowsAnyAsync<OperationCanceledException>(
            () => ModernMcpProcessDriver.StartAsync(cancellationToken: cancellation.Token));

        Assert.IsNotType<ModernMcpPhaseTimeoutException>(exception);
    }

    [Fact]
    public async Task PostStartRawRequestDeadline_ReportsMethodAndRequestIdentity()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(HeldThreads());
        var debugSessionId = await StartThreadsSessionAsync(driver, "post-start-raw-start");
        var requestId = new RequestId("post-start-raw-timeout");

        var exception = await Assert.ThrowsAsync<ModernMcpPhaseTimeoutException>(
            () => driver.CallToolRawAsync(
                "get_threads",
                new JsonObject { ["debugSessionId"] = debugSessionId },
                ModernMcpProcessDriver.CurrentMeta(),
                requestId));

        Assert.Equal(ModernMcpTimeoutPhase.PostStartMcpRequest, exception.Failure.Phase);
        Assert.Equal("test_driver", exception.Failure.Owner);
        Assert.Equal(TimeSpan.FromSeconds(2), exception.Failure.Deadline);
        Assert.True(exception.Failure.Elapsed >= TimeSpan.Zero);
        Assert.Equal("tools/call", exception.Failure.Method);
        Assert.Equal("post-start-raw-timeout", exception.Failure.RequestId);
        Assert.Null(exception.Failure.ToolName);
    }

    [Fact]
    public async Task PostStartExplicitDeadline_RemainsLocalToRawRequest()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(HeldThreads());
        var debugSessionId = await StartThreadsSessionAsync(driver, "post-start-explicit-start");

        var exception = await Assert.ThrowsAsync<ModernMcpPhaseTimeoutException>(
            () => driver.CallToolRawAsync(
                "get_threads",
                new JsonObject { ["debugSessionId"] = debugSessionId },
                ModernMcpProcessDriver.CurrentMeta(),
                new RequestId("post-start-explicit-timeout"),
                timeout: TimeSpan.FromMilliseconds(500)));

        Assert.Equal(TimeSpan.FromMilliseconds(500), exception.Failure.Deadline);
        Assert.Equal("post-start-explicit-timeout", exception.Failure.RequestId);
    }

    [Fact]
    public async Task PostStartTypedCallDeadline_ReportsToolWithoutSdkRequestIdentity()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(HeldThreads());
        var debugSessionId = await StartThreadsSessionAsync(driver, "post-start-typed-start");

        var exception = await Assert.ThrowsAsync<ModernMcpPhaseTimeoutException>(
            () => driver.CallToolAsync(
                "get_threads",
                new JsonObject { ["debugSessionId"] = debugSessionId },
                ModernMcpProcessDriver.CurrentMeta()));

        Assert.Equal(ModernMcpTimeoutPhase.PostStartMcpRequest, exception.Failure.Phase);
        Assert.Equal("test_driver", exception.Failure.Owner);
        Assert.Equal("get_threads", exception.Failure.ToolName);
        Assert.Null(exception.Failure.Method);
        Assert.Null(exception.Failure.RequestId);
    }

    [Fact]
    public async Task CallerCancelledPostStartRawRequest_RethrowsWithoutDriverTimingDiagnostic()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(HeldThreads());
        var debugSessionId = await StartThreadsSessionAsync(driver, "post-start-raw-cancel-start");
        using var cancellation = new CancellationTokenSource();
        var pending = driver.CallToolRawAsync(
            "get_threads",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("post-start-raw-cancel"),
            cancellation.Token);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForThreadsRequestAsync(observation.Token);

        cancellation.Cancel();

        var exception = await Assert.ThrowsAnyAsync<OperationCanceledException>(() => pending);
        Assert.IsNotType<ModernMcpPhaseTimeoutException>(exception);
        driver.ReleaseThreadsResponse();
    }

    [Fact]
    public async Task CallerCancelledPostStartTypedCall_RethrowsWithoutDriverTimingDiagnostic()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(HeldThreads());
        var debugSessionId = await StartThreadsSessionAsync(driver, "post-start-typed-cancel-start");
        using var cancellation = new CancellationTokenSource();
        var pending = driver.CallToolAsync(
            "get_threads",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            ModernMcpProcessDriver.CurrentMeta(),
            cancellation.Token);
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForThreadsRequestAsync(observation.Token);

        cancellation.Cancel();

        var exception = await Assert.ThrowsAnyAsync<OperationCanceledException>(() => pending);
        Assert.IsNotType<ModernMcpPhaseTimeoutException>(exception);
        driver.ReleaseThreadsResponse();
    }

    [Fact]
    public async Task MissingProgram_WithFormElicitation_UsesInputRequiredAndNewIdRetryWithoutRequestState()
    {
        await using var driver = await ModernMcpProcessDriver.StartFirstWireAsync();
        var initialId = new RequestId("input-required");
        var inputRequestId = await RequestProgramElicitationAsync(driver, initialId);
        var retryId = new RequestId("input-required-retry");
        var retry = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject(),
                ["inputResponses"] = new JsonObject { [inputRequestId] = new JsonObject { ["action"] = "accept", ["content"] = new JsonObject { ["program"] = ValidProgram } } },
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(formElicitation: true),
            },
            retryId);

        Assert.NotEqual(initialId, retryId);
        AssertCompleteStartEnvelope(ModernMcpProcessDriver.RequireResult(Assert.IsType<JsonRpcResponse>(retry)));
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


    private static ModernMcpStartOptions HeldThreads() => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(
            SuppressLifecycleEvents: true,
            ThreadsResponseMode: "hold"));

    private static async Task<string> StartThreadsSessionAsync(
        ModernMcpProcessDriver driver,
        string requestId)
    {
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId)));

        AssertCompleteStartEnvelope(result);
        return Assert.IsType<string>(result["structuredContent"]?["debugSessionId"]?.GetValue<string>());
    }
    private static void AssertCacheable(JsonObject result)
    {
        Assert.True(result["ttlMs"]?.GetValue<long>() > 0);
        Assert.Equal("public", result["cacheScope"]?.GetValue<string>());
    }

    private static void AssertCatalog(JsonObject result)
    {
        var tools = Assert.IsType<JsonArray>(result["tools"]);
        Assert.Equal(11, tools.Count);
        Assert.Equal(
            [
                "start_debug",
                "get_debug_state",
                "stop_debug",
                "get_threads",
                "get_call_stack",
                "get_ui_probe_capabilities",
                "capture_visual_evidence",
                "read_capture_artifact",
                "wait_for_ui_stable",
                "capture_element_snapshot",
                "capture_native_scene",
            ],
            tools.Select(static tool => tool?["name"]?.GetValue<string>()));
        Assert.Equal(
            ["start_debug", "get_debug_state", "stop_debug", "get_threads", "get_call_stack"],
            tools.Take(5).Select(static tool => tool?["name"]?.GetValue<string>()));

        var start = Assert.IsType<JsonObject>(tools[0]);
        Assert.Equal("object", start["inputSchema"]?["type"]?.GetValue<string>());
        Assert.False(start["inputSchema"]?["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(1, Assert.IsType<JsonObject>(start["inputSchema"]?["properties"])["program"]?["minLength"]?.GetValue<int>());

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
    }

    private static async Task<string> RequestProgramElicitationAsync(ModernMcpFirstWireDriver driver, RequestId initialId)
    {
        var initial = await driver.SendFirstRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = "start_debug",
                ["arguments"] = new JsonObject(),
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
