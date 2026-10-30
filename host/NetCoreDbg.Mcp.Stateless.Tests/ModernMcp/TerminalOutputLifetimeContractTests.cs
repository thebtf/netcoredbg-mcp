using System.Diagnostics;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class TerminalOutputLifetimeContractTests
{
    private const string Before = "before-stdout\nbefore-stderr\nbefore-console\n";
    private const string AtTerminal = "at-terminal\n";
    private const string Late = "late-stderr\nlate-stdout\nlate-console\n";
    private const string CompleteOutput = Before + AtTerminal + Late;

    [Theory]
    [InlineData("exited", "get_output")]
    [InlineData("exited", "get_output_tail")]
    [InlineData("terminated", "get_output")]
    [InlineData("terminated", "get_output_tail")]
    public async Task TerminalContinue_AfterNativeCleanup_RetainsOrderedOutputForBothToolsAndOneExplicitStop(
        string terminal, string firstOutputTool)
    {
        await using var driver = await StartDriverAsync(OutputOptions(terminal));
        var token = await StartStoppedSessionAsync(driver, "terminal-start");
        using var adapter = Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());

        await ContinueToTerminalAsync(driver, token, adapter, terminal, "terminal-continue");
        var cleaned = await driver.ReadNativeActionsAsync();
        Assert.Single(cleaned, static action => action.Command == "disconnect");

        // Start each vector at its output tool so baseline RED identifies the lost output lifetime.
        await AssertRetainedOutputAsync(driver, token, firstOutputTool, CompleteOutput, 7, "terminal-first-read");
        Assert.Equal(CompleteOutput, await ReadOutputAsync(driver, token, "terminal-repeat"));
        await AssertTailAsync(driver, token, "at-terminal\nlate-stderr\nlate-stdout\nlate-console", 7, 4,
            "terminal-tail-four", lines: 4);
        Assert.Equal("before-stderr\nlate-stderr\n", await ReadOutputAsync(
            driver, token, "terminal-filtered-clear", clear: true, category: "stderr"));
        Assert.Equal(CompleteOutput, await ReadOutputAsync(driver, token, "terminal-filter-did-not-clear"));
        await AssertTailAsync(driver, token, "before-stderr\nlate-stderr", 2, 2,
            "terminal-filtered-tail", category: "stderr");
        Assert.Equal("before-console\nlate-console\n", await ReadOutputAsync(
            driver, token, "terminal-default-category", category: "console"));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, "terminal-case-sensitive", category: "STDOUT"));
        await AssertTailAsync(driver, token, string.Empty, 0, 0, "terminal-tail-case-sensitive", category: "STDOUT");

        await AssertControlRetiredAsync(driver, token, "terminal-control");
        Assert.Equal(CompleteOutput, await ReadOutputAsync(driver, token, "terminal-control-did-not-release-output"));
        Assert.Equal(CompleteOutput, await ReadOutputAsync(driver, token, "terminal-clear", clear: true));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, "terminal-cleared"));
        await AssertTailAsync(driver, token, string.Empty, 0, 0, "terminal-tail-cleared");
        await AssertStopSuccessAsync(driver, token, "terminal-release");
        await AssertReleasedAndRandomUniformAsync(driver, token, "terminal-released");
        Assert.Equal(cleaned, await driver.ReadNativeActionsAsync());
    }

    [Theory]
    [InlineData("omitted")]
    [InlineData("null")]
    [InlineData("empty")]
    public async Task TerminalContinue_WholeBufferClearAfterRetirement_ClearsEveryCategoryWithoutReleasingHandle(string categoryShape)
    {
        await using var driver = await StartDriverAsync(OutputOptions("terminated"));
        var token = await StartStoppedSessionAsync(driver, "terminal-clear-shape-start");
        using var adapter = Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        await ContinueToTerminalAsync(driver, token, adapter, "terminated", "terminal-clear-shape-continue");

        var arguments = SessionArguments(token);
        arguments["clear"] = true;
        if (categoryShape != "omitted")
        {
            arguments["category"] = categoryShape == "empty" ? JsonValue.Create(string.Empty) : null;
        }

        var cleared = AssertEnvelope(await CallAsync(driver, "get_output", arguments, "terminal-clear-shape"));
        Assert.Equal("output_success", cleared["kind"]?.GetValue<string>());
        Assert.Equal(CompleteOutput, cleared["output"]?.GetValue<string>());
        foreach (var category in new[] { "stdout", "stderr", "console" })
        {
            Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, $"terminal-cleared-{category}", category: category));
            await AssertTailAsync(driver, token, string.Empty, 0, 0, $"terminal-cleared-tail-{category}", category: category);
        }
        await AssertStopSuccessAsync(driver, token, "terminal-clear-shape-release");
        await AssertReleasedAndRandomUniformAsync(driver, token, "terminal-clear-shape-released");
    }

    [Theory]
    [InlineData("get_output")]
    [InlineData("get_output_tail")]
    public async Task UnexpectedAdapterEof_WithoutTerminalEvent_RetainsKnownAcceptedMarkerForBothOutputTools(string firstOutputTool)
    {
        await using var driver = await StartDriverAsync(OutputOptions(terminal: null));
        var token = await StartStoppedSessionAsync(driver, "eof-start");
        using var adapter = Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        Assert.Equal(Before, await ReadOutputAsync(driver, token, "eof-known-marker-before"));

        var continued = AssertEnvelope(await CallAsync(driver, "continue_execution", ContinueArguments(token), "eof-continue"), isError: true);
        Assert.Equal("dap_continue_protocol_error", continued["kind"]?.GetValue<string>());
        Assert.Equal("DAP_CONTINUE_PROTOCOL_ERROR", continued["error"]?.GetValue<string>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await adapter.WaitForExitAsync(observation.Token);
        await driver.WaitForFixtureRecordAsync("output-unexpected-eof", observation.Token);
        Assert.True(adapter.HasExited);
        var cleaned = await driver.ReadNativeActionsAsync();
        Assert.DoesNotContain(cleaned, static action => action.Command == "disconnect");

        await AssertRetainedOutputAsync(driver, token, firstOutputTool, Before + AtTerminal, 4, "eof-first-read");
        Assert.Equal(Before + AtTerminal, await ReadOutputAsync(driver, token, "eof-repeat"));
        await AssertTailAsync(driver, token, "at-terminal", 4, 1, "eof-tail-one", lines: 1);
        await AssertControlRetiredAsync(driver, token, "eof-control");
        await AssertStopSuccessAsync(driver, token, "eof-release");
        await AssertReleasedAndRandomUniformAsync(driver, token, "eof-released");
        Assert.Equal(cleaned, await driver.ReadNativeActionsAsync());
    }

    [Fact]
    public async Task AutomaticallyRetiredTokens_IsolateRetainedReadsClearsAndExplicitRelease()
    {
        await using var driver = await StartDriverAsync(OutputOptions("terminated"));
        var first = await StartStoppedSessionAsync(driver, "isolation-first-start");
        using var firstAdapter = Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        Assert.Equal(Before, await ReadOutputAsync(driver, first, "isolation-first-initial-clear", clear: true));
        var second = await StartStoppedSessionAsync(driver, "isolation-second-start");
        using var secondAdapter = Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        Assert.NotEqual(first, second);
        Assert.NotEqual(firstAdapter.Id, secondAdapter.Id);

        await ContinueToTerminalAsync(driver, first, firstAdapter, "terminated", "isolation-first-continue");
        Assert.Equal(AtTerminal + Late, await ReadOutputAsync(driver, first, "isolation-first-retained"));
        Assert.Equal(Before, await ReadOutputAsync(driver, second, "isolation-second-still-active"));
        await ContinueToTerminalAsync(driver, second, secondAdapter, "terminated", "isolation-second-continue");
        Assert.Equal(CompleteOutput, await ReadOutputAsync(driver, second, "isolation-second-retained"));
        await AssertTailAsync(driver, first, "at-terminal\nlate-stderr\nlate-stdout\nlate-console", 4, 4, "isolation-first-tail");
        await AssertTailAsync(driver, second, CompleteOutput.TrimEnd('\n'), 7, 7, "isolation-second-tail");

        Assert.Equal(AtTerminal + Late, await ReadOutputAsync(driver, first, "isolation-first-clear", clear: true));
        Assert.Equal(CompleteOutput, await ReadOutputAsync(driver, second, "isolation-second-unaffected-by-clear"));
        await AssertStopSuccessAsync(driver, first, "isolation-first-release");
        await AssertReleasedAndRandomUniformAsync(driver, first, "isolation-first-released");
        Assert.Equal(CompleteOutput, await ReadOutputAsync(driver, second, "isolation-second-unaffected-by-release"));
        await AssertStopSuccessAsync(driver, second, "isolation-second-release");
        await AssertReleasedAndRandomUniformAsync(driver, second, "isolation-second-released");
        Assert.Equal(2, (await driver.ReadNativeActionsAsync()).Count(static action => action.Command == "disconnect"));
    }

    [Fact]
    public async Task StopDuringAutomaticRetirement_ReleasesOutputOnceWithoutResurrectionOrDuplicateCleanup()
    {
        await using var driver = await StartDriverAsync(OutputOptions("terminated", holdDisconnect: true));
        var token = await StartStoppedSessionAsync(driver, "retirement-race-start");
        using var adapter = Process.GetProcessById(await driver.ReadAdapterProcessIdAsync());
        var continued = CallAsync(driver, "continue_execution", ContinueArguments(token), "retirement-race-continue");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        Task<JsonRpcResponse>? stop = null;
        Task<JsonRpcResponse>? competingStop = null;
        try
        {
            // The terminal event has caused automatic retirement, but late output and cleanup are still held.
            await driver.WaitForFixtureRecordAsync("output-disconnect-gated", observation.Token);
            Assert.False(continued.IsCompleted, "Automatic cleanup must remain in flight at the disconnect barrier.");
            Assert.False(adapter.HasExited);
            stop = CallAsync(driver, "stop_debug", SessionArguments(token), "retirement-race-stop");

            // A second stop observes the explicit release while the first may still be joining the held drain.
            // At least one stop must claim the handle, and exactly one may report success.
            competingStop = CallAsync(driver, "stop_debug", SessionArguments(token), "retirement-race-second-stop");
            var claimed = await Task.WhenAny(stop, competingStop).WaitAsync(observation.Token);
            var claimedResponse = await claimed;
            var firstCompleted = AssertEnvelope(claimedResponse,
                isError: ModernMcpProcessDriver.RequireResult(claimedResponse)["isError"]?.GetValue<bool>() == true);
            Assert.Contains(firstCompleted["kind"]?.GetValue<string>(), new[] { "stop_debug_success", "debug_session_not_found" });
            foreach (var tool in new[] { "get_output", "get_output_tail" })
            {
                AssertNotFound(AssertEnvelope(await CallAsync(driver, tool, SessionArguments(token), "retirement-race-released-" + tool), isError: true));
            }
            driver.ReleaseThreadsResponse();
            var stops = await Task.WhenAll(stop, competingStop);
            Assert.Single(stops, static response => ModernMcpProcessDriver.RequireResult(response)["isError"]?.GetValue<bool>() != true);
            Assert.Single(stops, static response => ModernMcpProcessDriver.RequireResult(response)["isError"]?.GetValue<bool>() == true);
            foreach (var response in stops)
            {
                var isError = ModernMcpProcessDriver.RequireResult(response)["isError"]?.GetValue<bool>() == true;
                var content = AssertEnvelope(response, isError);
                if (isError)
                {
                    AssertNotFound(content);
                }
                else
                {
                    Assert.Equal("stop_debug_success", content["kind"]?.GetValue<string>());
                }
            }

            Assert.Equal("continue_terminal", AssertEnvelope(await continued)["kind"]?.GetValue<string>());
            await adapter.WaitForExitAsync(observation.Token);
            await driver.WaitForFixtureRecordAsync("output-disconnect-written", observation.Token);
            var cleaned = await driver.ReadNativeActionsAsync();
            Assert.Single(cleaned, static action => action.Command == "disconnect");
            Assert.DoesNotContain(cleaned, static action => action.Command == "terminate");
            await AssertReleasedAndRandomUniformAsync(driver, token, "retirement-race-after-late-output");
            await AssertControlRetiredAsync(driver, token, "retirement-race-control");
            await AssertReleasedAndRandomUniformAsync(driver, token, "retirement-race-after-control");
            Assert.Equal(cleaned, await driver.ReadNativeActionsAsync());
        }
        finally
        {
            driver.ReleaseThreadsResponse();
            // Observe all started operations even if an assertion fails while a fixture barrier is held.
            await continued;
            if (stop is not null)
            {
                await stop;
            }
            if (competingStop is not null)
            {
                await competingStop;
            }
        }
    }

    private static Task<ModernMcpProcessDriver> StartDriverAsync(ModernMcpStartOptions options) =>
        ModernMcpProcessDriver.StartAsync(options, startupTimeout: TimeSpan.FromSeconds(10));

    private static ModernMcpStartOptions OutputOptions(string? terminal, bool holdDisconnect = false)
    {
        var continued = new JsonObject
        {
            ["entries"] = new JsonArray(Entry(AtTerminal, "stdout")),
        };
        if (terminal is null)
        {
            continued["eof"] = true;
        }
        else
        {
            continued["terminal"] = terminal;
        }

        return new ModernMcpStartOptions(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"),
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = new JsonObject
                {
                    ["launch"] = new JsonArray(Entry("before-stdout\n", "stdout"), Entry("before-stderr\n", "stderr"), Entry("before-console\n")),
                    ["continue"] = continued,
                    ["disconnect"] = new JsonObject
                    {
                        ["waitForRelease"] = holdDisconnect,
                        ["entries"] = new JsonArray(Entry("late-stderr\n", "stderr"), Entry("late-stdout\n", "stdout"), Entry("late-console\n")),
                    },
                }.ToJsonString(),
                ["NETCOREDBG_MAX_OUTPUT_ENTRY"] = "100000",
                ["NETCOREDBG_MAX_OUTPUT_BYTES"] = "10000000",
            });
    }

    private static JsonObject Entry(string text, string? category = null)
    {
        var body = new JsonObject { ["output"] = text };
        if (category is not null)
        {
            body["category"] = category;
        }
        return body;
    }

    private static JsonObject SessionArguments(string token) => new() { ["debugSessionId"] = token };

    private static JsonObject ContinueArguments(string token)
    {
        var arguments = SessionArguments(token);
        arguments["threadId"] = 1;
        return arguments;
    }

    private static Task<JsonRpcResponse> CallAsync(ModernMcpProcessDriver driver, string tool, JsonObject arguments, string id) =>
        driver.CallToolRawAsync(tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId(id), timeout: TimeSpan.FromSeconds(10));

    private static async Task<string> StartStoppedSessionAsync(ModernMcpProcessDriver driver, string id)
    {
        var started = AssertEnvelope(await CallAsync(driver, "start_debug", new JsonObject { ["program"] = driver.InertProgramPath }, id));
        Assert.Equal("start_debug_success", started["kind"]?.GetValue<string>());
        var token = Assert.IsType<string>(started["debugSessionId"]?.GetValue<string>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        var barrier = AssertEnvelope(await CallAsync(driver, "get_threads", SessionArguments(token), id + "-reader-barrier"));
        Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
        return token;
    }

    private static async Task ContinueToTerminalAsync(ModernMcpProcessDriver driver, string token, Process adapter, string terminal, string id)
    {
        var continued = AssertEnvelope(await CallAsync(driver, "continue_execution", ContinueArguments(token), id));
        Assert.Equal("continue_terminal", continued["kind"]?.GetValue<string>());
        Assert.Equal(terminal, continued["terminal"]?.GetValue<string>());
        Assert.Equal(terminal == "exited" ? 3 : 2, continued.Count);
        if (terminal == "exited")
        {
            Assert.Equal(23, continued["exitCode"]?.GetValue<int>());
        }
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await adapter.WaitForExitAsync(observation.Token);
        Assert.True(adapter.HasExited, "Retaining output must not keep the owned adapter process alive.");
    }

    private static async Task AssertRetainedOutputAsync(ModernMcpProcessDriver driver, string token, string tool, string expected, int totalLines, string id)
    {
        if (tool == "get_output_tail")
        {
            await AssertTailAsync(driver, token, expected.TrimEnd('\n'), totalLines, totalLines, id);
        }
        else
        {
            Assert.Equal(expected, await ReadOutputAsync(driver, token, id));
        }
    }

    private static async Task<string> ReadOutputAsync(ModernMcpProcessDriver driver, string token, string id, bool clear = false, string? category = null)
    {
        var arguments = SessionArguments(token);
        arguments["clear"] = clear;
        if (category is not null)
        {
            arguments["category"] = category;
        }
        var content = AssertEnvelope(await CallAsync(driver, "get_output", arguments, id));
        Assert.Equal("output_success", content["kind"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        return Assert.IsType<string>(content["output"]?.GetValue<string>());
    }

    private static async Task AssertTailAsync(ModernMcpProcessDriver driver, string token, string expected, int totalLines, int returnedLines,
        string id, int lines = 50, string? category = null)
    {
        var arguments = SessionArguments(token);
        arguments["lines"] = lines;
        if (category is not null)
        {
            arguments["category"] = category;
        }
        var content = AssertEnvelope(await CallAsync(driver, "get_output_tail", arguments, id));
        Assert.Equal("output_tail_success", content["kind"]?.GetValue<string>());
        Assert.Equal(4, content.Count);
        Assert.Equal(expected, content["output"]?.GetValue<string>());
        Assert.Equal(totalLines, content["totalLines"]?.GetValue<int>());
        Assert.Equal(returnedLines, content["returnedLines"]?.GetValue<int>());
    }

    private static async Task AssertStopSuccessAsync(ModernMcpProcessDriver driver, string token, string id)
    {
        var content = AssertEnvelope(await CallAsync(driver, "stop_debug", SessionArguments(token), id));
        Assert.Equal("stop_debug_success", content["kind"]?.GetValue<string>());
    }

    private static async Task AssertControlRetiredAsync(ModernMcpProcessDriver driver, string token, string id)
    {
        var calls = new (string Tool, JsonObject Arguments)[]
        {
            ("get_debug_state", SessionArguments(token)),
            ("get_threads", SessionArguments(token)),
            ("continue_execution", ContinueArguments(token)),
            ("step_over", ContinueArguments(token)),
            ("ui_invoke", new JsonObject { ["debugSessionId"] = token, ["automationId"] = "retired-output-control" }),
        };
        foreach (var (tool, arguments) in calls)
        {
            AssertNotFound(AssertEnvelope(await CallAsync(driver, tool, arguments, id + "-" + tool), isError: true));
        }

        var capabilities = AssertEnvelope(await CallAsync(driver, "get_ui_probe_capabilities", new JsonObject
        {
            ["debugSessionId"] = token,
            ["protocolVersion"] = "native-scene-probe/1",
            ["schemaVersion"] = "native-scene-probe.schema/1",
        }, id + "-native-scene"), isError: true);
        Assert.Equal("tool_error", capabilities["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", capabilities["code"]?.GetValue<string>());
    }

    private static async Task AssertReleasedAndRandomUniformAsync(ModernMcpProcessDriver driver, string token, string id)
    {
        JsonObject? previous = null;
        foreach (var candidate in new[] { token, "random-unissued-output-handle" })
        {
            foreach (var tool in new[] { "get_output", "get_output_tail", "stop_debug" })
            {
                var content = AssertEnvelope(await CallAsync(driver, tool, SessionArguments(candidate), $"{id}-{candidate}-{tool}"), isError: true);
                AssertNotFound(content);
                if (previous is not null)
                {
                    Assert.Equal(previous.ToJsonString(), content.ToJsonString());
                }
                previous = content;
            }
        }
    }

    private static void AssertNotFound(JsonObject content)
    {
        Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static JsonObject AssertEnvelope(JsonRpcResponse response, bool isError = false)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() == true);
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", text["type"]?.GetValue<string>());
        Assert.Equal(content.ToJsonString(), text["text"]?.GetValue<string>());
        return content;
    }
}
