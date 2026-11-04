using System.Globalization;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;
using Xunit.Abstractions;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class OutputContractTests(ITestOutputHelper output)
{
    private const string Tool = "get_output";
    private const string TailTool = "get_output_tail";
    private const string TruncationSuffix = "... [truncated]";

    public static TheoryData<string, JsonObject?> InvalidArguments => new()
    {
        { "missing_arguments", null },
        { "omitted_token", new JsonObject() },
        { "null_token", new JsonObject { ["debugSessionId"] = null } },
        { "numeric_token", new JsonObject { ["debugSessionId"] = 17 } },
        { "boolean_token", new JsonObject { ["debugSessionId"] = true } },
        { "object_token", new JsonObject { ["debugSessionId"] = new JsonObject() } },
        { "array_token", new JsonObject { ["debugSessionId"] = new JsonArray() } },
        { "empty_token", new JsonObject { ["debugSessionId"] = string.Empty } },
        { "whitespace_token", new JsonObject { ["debugSessionId"] = " \t\r\n" } },
        { "null_clear", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = null } },
        { "numeric_clear", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = 1 } },
        { "string_clear", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = "true" } },
        { "object_clear", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = new JsonObject() } },
        { "array_clear", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = new JsonArray() } },
        { "numeric_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = 17 } },
        { "boolean_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = false } },
        { "object_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = new JsonObject() } },
        { "array_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = new JsonArray() } },
        { "extra_field", new JsonObject { ["debugSessionId"] = "opaque", ["unexpected"] = true } },
    };

    private static (string Scenario, JsonObject? Arguments)[] InvalidTailArguments =>
    [
        ("null_arguments", null),
        ("omitted_token", new JsonObject()),
        ("null_token", new JsonObject { ["debugSessionId"] = null }),
        ("numeric_token", new JsonObject { ["debugSessionId"] = 17 }),
        ("boolean_token", new JsonObject { ["debugSessionId"] = true }),
        ("object_token", new JsonObject { ["debugSessionId"] = new JsonObject() }),
        ("array_token", new JsonObject { ["debugSessionId"] = new JsonArray() }),
        ("empty_token", new JsonObject { ["debugSessionId"] = string.Empty }),
        ("whitespace_token", new JsonObject { ["debugSessionId"] = " \t\r\n" }),
        ("null_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = null }),
        ("boolean_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = true }),
        ("string_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = "2" }),
        ("object_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = new JsonObject() }),
        ("array_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = new JsonArray() }),
        ("positive_fraction", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("2.5") }),
        ("negative_fraction", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("-2.5") }),
        ("fractional_exponent", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("2e-1") }),
        ("underflowing_fraction", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("1e-1000") }),
        ("fraction_beyond_double_precision", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("1.0000000000000000000000000000000001") }),
        ("large_fraction_beyond_double_precision", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("9007199254740993.1") }),
        ("numeric_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = 17 }),
        ("boolean_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = false }),
        ("object_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = new JsonObject() }),
        ("array_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = new JsonArray() }),
        ("forbidden_clear_true", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = true }),
        ("forbidden_clear_false", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = false }),
        ("forbidden_clear_null", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = null }),
        ("extra_field", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = 2, ["category"] = null, ["unexpected"] = true }),
        ("mis_cased_lines", new JsonObject { ["debugSessionId"] = "opaque", ["Lines"] = 2 }),
    ];
    [Theory]
    [InlineData(Tool)]
    [InlineData(TailTool)]
    public async Task LiveHost_OutputSuccess_MatchesApplicationSchemaWithoutDapIoOrConsumingOutput(string toolName)
    {
        const string original = "first\r\nsecond\0\U0001F600\nthird\n";
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("first\r\n", "stdout"), Output("second\0\U0001F600\nthird\n", "stderr"))));
        var token = await StartSessionAsync(driver, $"schema-{toolName}-start");
        var arguments = new JsonObject { ["debugSessionId"] = token, ["category"] = null };
        if (toolName == Tool)
        {
            arguments["clear"] = false;
        }
        else
        {
            arguments["lines"] = 2;
        }
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            toolName, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"schema-{toolName}-success")), isError: false);

        Assert.Equal(toolName == Tool ? "output_success" : "output_tail_success", content["kind"]?.GetValue<string>());
        Assert.Equal(toolName == Tool ? 2 : 4, content.Count);
        Assert.Equal(toolName == Tool ? original : "second\0\U0001F600\nthird", content["output"]?.GetValue<string>());
        if (toolName == TailTool)
        {
            Assert.Equal(3, content["totalLines"]?.GetValue<int>());
            Assert.Equal(2, content["returnedLines"]?.GetValue<int>());
        }
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        Assert.Equal(original, await ReadOutputAsync(driver, token, $"schema-{toolName}-retained"));
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
    }

    [Theory]
    [InlineData(Tool)]
    [InlineData(TailTool)]
    public async Task LiveHost_OutputInvalidArguments_MatchApplicationSchemaWithoutDapIoOrConsumingOutput(string toolName)
    {
        const string original = "retained\0\U0001F600\r\n";
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(Output(original))));
        var token = await StartSessionAsync(driver, $"schema-{toolName}-invalid-start");
        var arguments = new JsonObject { ["debugSessionId"] = token };
        arguments[toolName == Tool ? "clear" : "lines"] = "not-the-advertised-type";
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            toolName, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"schema-{toolName}-invalid")), isError: true);

        Assert.Equal(3, content.Count);
        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(toolName, content["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        Assert.Equal(original, await ReadOutputAsync(driver, token, $"schema-{toolName}-invalid-retained"));
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
    }


    [Fact]
    public async Task SearchOutput_MatchesJoinedFilteredLinesWithContextWithoutConsumingOutput()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(
            Output("intro\r", "stdout"), Output("ignored\n", "stderr"), Output("\nERROR 42\nnext\nFail 7\nlast", "stdout"))));
        var token = await StartSessionAsync(driver, "search-output-start");
        var before = await driver.ReadNativeActionsAsync();
        var arguments = new JsonObject { ["debugSessionId"] = token, ["pattern"] = "error|fail", ["contextLines"] = 1, ["category"] = "stdout" };
        var result = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output", arguments,
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("search-output-context")), isError: false);
        Assert.Equal("output_search_success", result["kind"]?.GetValue<string>());
        Assert.Equal(2, result["matchCount"]?.GetValue<int>());
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""[{"lineNumber":2,"match":"ERROR 42","context":["intro","ERROR 42","next"]},{"lineNumber":4,"match":"Fail 7","context":["next","Fail 7","last"]}]"""), result["matches"]));
        StructuredContentSchemaParityTests.AssertMatchesSchema(result);
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        Assert.Equal("intro\r\nERROR 42\nnext\nFail 7\nlast", await ReadOutputAsync(driver, token, "search-output-retained", category: "stdout"));
    }

    [Fact]
    public async Task SearchOutput_CountsEveryMatchButReturnsOnlyFirstFifty()
    {
        var lines = Enumerable.Range(1, 55).Select(static index => $"hit-{index}").ToArray();
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(Output(string.Join("\n", lines)))));
        var token = await StartSessionAsync(driver, "search-limit-start");
        var result = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output",
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "hit", ["contextLines"] = 0 },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("search-limit")), isError: false);
        Assert.Equal(55, result["matchCount"]?.GetValue<int>());
        var matches = Assert.IsType<JsonArray>(result["matches"]);
        Assert.Equal(50, matches.Count);
        Assert.Equal("hit-1", matches[0]?["match"]?.GetValue<string>());
        Assert.Equal("hit-50", matches[49]?["match"]?.GetValue<string>());
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("[\"hit-50\"]"), matches[49]?["context"]));
    }

    [Fact]
    public async Task SearchOutput_InvalidPattern_ReturnsFixedErrorWithoutClearingOutput()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(Output("retained output"))));
        var token = await StartSessionAsync(driver, "search-invalid-start");
        var result = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output",
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "(" }, ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("search-invalid")), isError: true);
        Assert.Equal("output_search_invalid_pattern", result["kind"]?.GetValue<string>());
        Assert.Equal("OUTPUT_SEARCH_INVALID_PATTERN", result["error"]?.GetValue<string>());
        Assert.Equal("retained output", await ReadOutputAsync(driver, token, "search-invalid-retained"));
    }

    [Fact]
    public async Task SearchOutput_PatternThatCannotFitInAResultIsRejectedBeforeRegexParsing()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(Output("retained"))));
        var token = await StartSessionAsync(driver, "search-pattern-size-start");
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output",
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "(" + new string('x', 256 * 1024) },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("search-pattern-size")), isError: true);
        Assert.Equal("output_search_too_large", content["kind"]?.GetValue<string>());
        Assert.Equal("OUTPUT_SEARCH_TOO_LARGE", content["error"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        Assert.Equal("retained", await ReadOutputAsync(driver, token, "search-pattern-size-retained"));
    }

    [Fact]
    public async Task SearchOutput_SplitsAllLogicalLineSeparatorsAndDoesNotAddATrailingLine()
    {
        const string text = "a\r\n\nb\vc\fd\u001ce\u001df\u001eg\u0085h\u2028i\u2029j\n";
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(Output(text))));
        var token = await StartSessionAsync(driver, "search-lines-start");
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output",
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "^$|^[bdh]$", ["contextLines"] = 0 },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("search-lines")), isError: false);
        Assert.Equal(4, content["matchCount"]?.GetValue<int>());
        Assert.True(JsonNode.DeepEquals(JsonNode.Parse("""[{"lineNumber":2,"match":"","context":[""]},{"lineNumber":3,"match":"b","context":["b"]},{"lineNumber":5,"match":"d","context":["d"]},{"lineNumber":9,"match":"h","context":["h"]}]"""), content["matches"]));
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
    }

    [Fact]
    public async Task SearchOutput_SignedContextUsesPythonSliceBoundaries()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(Output("a\nb\nc\nd\n"))));
        var token = await StartSessionAsync(driver, "search-context-start");
        var cases = new (JsonNode Lines, string Contexts)[]
        {
            (JsonValue.Create(long.MinValue)!, "[[],[],[],[]]"),
            (JsonValue.Create(-2)!, "[[\"c\"],[],[],[]]"),
            (JsonValue.Create(-1)!, "[[],[],[],[]]"),
            (JsonValue.Create(0)!, "[[\"a\"],[\"b\"],[\"c\"],[\"d\"]]"),
            (JsonNode.Parse("1e0")!, "[[\"a\",\"b\"],[\"a\",\"b\",\"c\"],[\"b\",\"c\",\"d\"],[\"c\",\"d\"]]"),
            (JsonValue.Create(long.MaxValue)!, "[[\"a\",\"b\",\"c\",\"d\"],[\"a\",\"b\",\"c\",\"d\"],[\"a\",\"b\",\"c\",\"d\"],[\"a\",\"b\",\"c\",\"d\"]]"),
        };
        for (var index = 0; index < cases.Length; index++)
        {
            var content = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output",
                new JsonObject { ["debugSessionId"] = token, ["pattern"] = ".", ["contextLines"] = cases[index].Lines },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"search-context-{index}")), isError: false);
            var contexts = new JsonArray(Assert.IsType<JsonArray>(content["matches"]).Select(static match => match!["context"]!.DeepClone()).ToArray());
            Assert.True(JsonNode.DeepEquals(JsonNode.Parse(cases[index].Contexts), contexts));
        }
    }

    [Fact]
    public async Task SearchOutput_InvalidArgumentsDoNotConsumeOutputOrSendDapRequests()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(Output("retained"))));
        var token = await StartSessionAsync(driver, "search-arguments-start");
        var invalid = new[]
        {
            new JsonObject { ["debugSessionId"] = token },
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = null },
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = 1 },
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "x", ["contextLines"] = null },
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "x", ["contextLines"] = JsonNode.Parse("1.5") },
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "x", ["category"] = true },
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "x", ["clear"] = false },
        };
        var before = await driver.ReadNativeActionsAsync();
        for (var index = 0; index < invalid.Length; index++)
        {
            var content = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output", invalid[index],
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"search-arguments-{index}")), isError: true);
            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("search_output", content["tool"]?.GetValue<string>());
            StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        }
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        Assert.Equal("retained", await ReadOutputAsync(driver, token, "search-arguments-retained"));
    }

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    public async Task SearchOutput_EnforcesExactSerializedSizeWithoutPartialResults(int excessBytes)
    {
        const int limit = 256 * 1024;
        var template = new JsonObject
        {
            ["kind"] = "output_search_success",
            ["pattern"] = "!",
            ["matchCount"] = 1,
            ["matches"] = new JsonArray(new JsonObject
            {
                ["lineNumber"] = 2,
                ["match"] = "!",
                ["context"] = new JsonArray("", "!", ""),
            }),
        };
        var remaining = limit - System.Text.Encoding.UTF8.GetByteCount(template.ToJsonString());
        var first = Output("\n");
        first["prefixLength"] = 70_000;
        var middle = Output("!\n");
        middle["prefixLength"] = 50_000;
        var last = Output("\n");
        last["prefixLength"] = remaining - 170_000 + excessBytes;
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(first, middle, last)));
        var token = await StartSessionAsync(driver, $"search-size-{excessBytes}-start");
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output",
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "!", ["contextLines"] = 1 },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"search-size-{excessBytes}")), isError: excessBytes != 0);
        if (excessBytes == 0)
        {
            Assert.Equal("output_search_success", content["kind"]?.GetValue<string>());
            Assert.Equal(limit, System.Text.Encoding.UTF8.GetByteCount(content.ToJsonString()));
        }
        else
        {
            Assert.Equal("output_search_too_large", content["kind"]?.GetValue<string>());
            Assert.Equal("OUTPUT_SEARCH_TOO_LARGE", content["error"]?.GetValue<string>());
            Assert.False(content.ContainsKey("matches"));
        }
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        Assert.Equal(70_000 + 50_001 + (remaining - 170_000 + excessBytes) + 3,
            (await ReadOutputAsync(driver, token, $"search-size-{excessBytes}-retained")).Length);
    }

    [Fact]
    public async Task SearchOutput_RegexTimeoutLeavesOutputAndSessionUsable()
    {
        var entry = Output("\n");
        entry["prefixLength"] = 50_000;
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(entry)));
        var token = await StartSessionAsync(driver, "search-timeout-start");
        var timedOut = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output",
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "^(x+)+y$" },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("search-timeout")), isError: true);
        Assert.Equal("output_search_timeout", timedOut["kind"]?.GetValue<string>());
        Assert.Equal("OUTPUT_SEARCH_TIMEOUT", timedOut["error"]?.GetValue<string>());
        StructuredContentSchemaParityTests.AssertMatchesSchema(timedOut);
        var next = AssertApplicationEnvelope(await driver.CallToolRawAsync("search_output",
            new JsonObject { ["debugSessionId"] = token, ["pattern"] = "^x" },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("search-after-timeout")), isError: false);
        Assert.Equal(1, next["matchCount"]?.GetValue<int>());
        Assert.Equal(50_001, (await ReadOutputAsync(driver, token, "search-timeout-retained")).Length);
    }

    [Fact]
    public async Task GetOutput_EmptyBuffer_ReturnsCompleteEmptySuccessWithoutDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions());
        var token = await StartSessionAsync(driver, "output-empty-start");

        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, "output-empty"));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, "output-empty-clear", clear: true));
    }

    [Fact]
    public async Task GetOutput_OrderedEvents_ConcatenatesExactTextAndPreservesRepeatedReads()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(launch: MixedOutput()));
        var token = await StartSessionAsync(driver, "output-ordered-start");
        const string expected = "first\r\nwarn\nconsole\0\U0001F600metric|tail";

        Assert.Equal(expected, await ReadOutputAsync(driver, token, "output-ordered-default"));
        Assert.Equal(expected, await ReadOutputAsync(driver, token, "output-ordered-false", clear: false));
        Assert.Equal(expected, await ReadOutputAsync(driver, token, "output-ordered-null", includeCategory: true));
        Assert.Equal(expected, await ReadOutputAsync(driver, token, "output-ordered-empty", category: string.Empty));
    }

    [Fact]
    public async Task GetOutput_CategoryFilter_IsCaseSensitiveAndDefaultsOmittedEventCategoryToConsole()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(launch: MixedOutput()));
        var token = await StartSessionAsync(driver, "output-categories-start");
        var cases = new (string Category, string Expected)[]
        {
            ("stdout", "first\r\ntail"),
            ("stderr", "warn\n"),
            ("console", "console\0\U0001F600"),
            ("telemetry", "metric|"),
            ("STDOUT", string.Empty),
            ("TELEMETRY", string.Empty),
            ("Console", string.Empty),
            ("unknown", string.Empty),
            (" ", string.Empty),
        };

        for (var index = 0; index < cases.Length; index++)
        {
            var (category, expected) = cases[index];
            Assert.Equal(expected, await ReadOutputAsync(driver, token, $"output-category-{index}", category: category));
        }
    }

    [Theory]
    [InlineData("stdout", "first\r\ntail")]
    [InlineData("stderr", "warn\n")]
    [InlineData("console", "console\0\U0001F600")]
    [InlineData("telemetry", "metric|")]
    [InlineData("STDOUT", "")]
    [InlineData("unknown", "")]
    public async Task GetOutput_FilteredClear_ReturnsMatchesWithoutClearingAnyCategory(string category, string expected)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: MixedOutput()));
        var token = await StartSessionAsync(driver, "output-filtered-clear-start");

        Assert.Equal(expected, await ReadOutputAsync(driver, token, "output-filtered-clear", clear: true, category: category));
        Assert.Equal("first\r\nwarn\nconsole\0\U0001F600metric|tail", await ReadOutputAsync(driver, token, "output-filtered-clear-whole"));
        Assert.Equal(expected, await ReadOutputAsync(driver, token, "output-filtered-clear-again", category: category));
    }

    [Theory]
    [InlineData("omitted")]
    [InlineData("null")]
    [InlineData("empty")]
    public async Task GetOutput_WholeBufferClear_ReturnsSnapshotThenEmptiesAllCategories(string categoryShape)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(launch: MixedOutput()));
        var token = await StartSessionAsync(driver, $"output-whole-clear-{categoryShape}-start");
        var category = categoryShape == "empty" ? string.Empty : null;

        Assert.Equal("first\r\nwarn\nconsole\0\U0001F600metric|tail", await ReadOutputAsync(
            driver, token, "output-whole-clear", clear: true, category: category, includeCategory: categoryShape != "omitted"));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, "output-whole-clear-after"));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, "output-whole-clear-console", category: "console"));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, "output-whole-clear-repeat", clear: true));
    }

    [Theory]
    [InlineData("exited")]
    [InlineData("terminated")]
    public async Task GetOutput_TerminalEvent_PreservesOutputWhileNativeSessionRemainsAvailable(string terminalEvent)
    {
        var batch = Batch(Output("final\n", "stderr"));
        batch["terminal"] = terminalEvent;
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(Output("before\n", "stdout")),
            batches: new JsonArray(batch)));
        var token = await StartSessionAsync(driver, $"output-{terminalEvent}-start");

        await AdvanceOutputAsync(driver, token, $"output-{terminalEvent}-barrier");
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"output-{terminalEvent}-state")), isError: false);
        Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
        Assert.Equal(terminalEvent, state["state"]?["event"]?.GetValue<string>());
        Assert.Equal("before\nfinal\n", await ReadOutputAsync(driver, token, $"output-{terminalEvent}-read"));
        Assert.Equal("final\n", await ReadOutputAsync(driver, token, $"output-{terminalEvent}-stderr", category: "stderr"));
        Assert.Equal("before\nfinal\n", await ReadOutputAsync(driver, token, $"output-{terminalEvent}-clear", clear: true));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, $"output-{terminalEvent}-after-clear"));
    }

    [Fact]
    public async Task GetOutput_IndependentSessions_IsolatesReadsClearsAndLaterAppends()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(Output("initial|")),
            batches: new JsonArray(Batch(Output("append|")))));
        var first = await StartSessionAsync(driver, "output-isolation-first-start");
        var second = await StartSessionAsync(driver, "output-isolation-second-start");
        Assert.NotEqual(first, second);

        Assert.Equal("initial|", await ReadOutputAsync(driver, first, "output-isolation-first-clear", clear: true));
        Assert.Equal("initial|", await ReadOutputAsync(driver, second, "output-isolation-second-unchanged"));
        await AdvanceOutputAsync(driver, second, "output-isolation-second-append");
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, first, "output-isolation-first-still-empty"));
        Assert.Equal("initial|append|", await ReadOutputAsync(driver, second, "output-isolation-second-clear", clear: true));
        await AdvanceOutputAsync(driver, first, "output-isolation-first-append");
        Assert.Equal("append|", await ReadOutputAsync(driver, first, "output-isolation-first-later"));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, second, "output-isolation-second-still-empty"));
    }

    [Theory]
    [MemberData(nameof(InvalidArguments))]
    public async Task GetOutput_InvalidInput_ReturnsCompleteInvalidArgumentsWithoutDapIo(string scenario, JsonObject? arguments)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions());
        var before = await driver.ReadNativeActionsAsync();

        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            Tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"output-invalid-{scenario}")), isError: true);

        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(Tool, content["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task GetOutput_MissingAndRetiredSessions_ReturnUniformCompleteNotFoundWithoutDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(launch: new JsonArray(Output("retired private output"))));
        var token = await StartSessionAsync(driver, "output-retired-start");
        var stopped = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "stop_debug",
            new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("output-retire")), isError: false);
        Assert.Equal("stop_debug_success", stopped["kind"]?.GetValue<string>());
        var before = await driver.ReadNativeActionsAsync();
        JsonObject? previous = null;
        var tokens = new[] { "!", "short", new string('r', 32), token };

        for (var index = 0; index < tokens.Length; index++)
        {
            var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
                Tool,
                new JsonObject { ["debugSessionId"] = tokens[index], ["clear"] = true, ["category"] = null },
                ModernMcpProcessDriver.CurrentMeta(),
                new RequestId($"output-unavailable-{index}")), isError: true);
            Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
            Assert.Equal(2, content.Count);
            if (previous is not null)
            {
                Assert.Equal(previous.ToJsonString(), content.ToJsonString());
            }

            previous = content;
        }

        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("NETCOREDBG_MAX_OUTPUT_ENTRY", "100_000", "abcdefghi")]
    [InlineData("NETCOREDBG_MAX_OUTPUT_ENTRY", "0_003", "abcdef... [truncated]")]
    [InlineData("NETCOREDBG_MAX_OUTPUT_BYTES", "100_000", "abcdefghi")]
    [InlineData("NETCOREDBG_MAX_OUTPUT_BYTES", "0_006", "defghi")]
    [InlineData("NETCOREDBG_MAX_OUTPUT_ENTRY", "١٠٠", "abcdefghi")]
    [InlineData("NETCOREDBG_MAX_OUTPUT_ENTRY", "+1_0", "abcdefghi")]
    [InlineData("NETCOREDBG_MAX_OUTPUT_BYTES", "١٠٠", "abcdefghi")]
    [InlineData("NETCOREDBG_MAX_OUTPUT_BYTES", "+1_0", "abcdefghi")]
    public async Task GetOutput_LegacyIntegerLimits_AcceptsAndAppliesConfiguredValue(string limitName, string configured, string expected)
    {
        var options = OutputOptions(launch: new JsonArray(Output("abc"), Output("defghi")));
        var environment = options.AdditionalEnvironment!.ToDictionary(static pair => pair.Key, static pair => pair.Value, StringComparer.Ordinal);
        environment[limitName] = configured;
        await using var driver = await ModernMcpProcessDriver.StartAsync(options with { AdditionalEnvironment = environment });
        var token = await StartSessionAsync(driver, "output-legacy-integer-limit-start");

        Assert.Equal(expected, await ReadOutputAsync(driver, token, "output-legacy-integer-limit-read"));
    }

    [Theory]
    [InlineData("output-high-surrogate", "{\"category\":\"stdout\",\"output\":\"\\uD800\"}")]
    [InlineData("output-low-surrogate", "{\"category\":\"stdout\",\"output\":\"\\uDC00\"}")]
    [InlineData("category-high-surrogate", "{\"category\":\"\\uD800\",\"output\":\"discarded|\"}")]
    [InlineData("category-low-surrogate", "{\"category\":\"\\uDC00\",\"output\":\"discarded|\"}")]
    public async Task GetOutput_MalformedEscapedUnicode_DoesNotRetireSessionOrLoseLaterOutput(string scenario, string rawBody)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(Output("before|", "stdout")),
            batches: new JsonArray(Batch(new JsonObject { ["rawBody"] = rawBody }, Output("after|", "stdout")))));
        var token = await StartSessionAsync(driver, $"output-malformed-{scenario}-start");

        await AdvanceOutputAsync(driver, token, $"output-malformed-{scenario}-reader-barrier");
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state", new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"output-malformed-{scenario}-state")), isError: false);
        Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
        Assert.Equal("before|after|", await ReadOutputAsync(driver, token, $"output-malformed-{scenario}-read"));
    }

    [Theory]
    [InlineData(3, 3)]
    [InlineData(null, 100_000)]
    public async Task GetOutput_EntryLimit_CountsUnicodeScalarsAndTruncatesOnlyAboveBoundary(int? configuredLimit, int effectiveLimit)
    {
        var prefix = new string('x', effectiveLimit - 1) + "\U0001F600";
        var exact = new string('x', effectiveLimit - 1) + "\U0001D11E";
        var oversizedEntry = Output("\U0001F600!", "stdout");
        oversizedEntry["prefixLength"] = effectiveLimit - 1;
        var exactEntry = Output("\U0001D11E", "stderr");
        exactEntry["prefixLength"] = effectiveLimit - 1;
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(oversizedEntry, exactEntry),
            maximumEntry: configuredLimit));
        var token = await StartSessionAsync(driver, "output-entry-limit-start");

        Assert.Equal(prefix + TruncationSuffix, await ReadOutputAsync(driver, token, "output-entry-limit-truncated", category: "stdout"));
        Assert.Equal(exact, await ReadOutputAsync(driver, token, "output-entry-limit-exact", category: "stderr"));
        Assert.Equal(prefix + TruncationSuffix + exact, await ReadOutputAsync(driver, token, "output-entry-limit-all"));
    }

    [Fact]
    public async Task GetOutput_AggregateLimit_CountsUnicodeScalarsAndEvictsWholeOldestEntries()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(Output("A\U0001F600", "stdout"), Output("é\U0001D11E", "stderr"), Output("ZQ")),
            batches: new JsonArray(Batch(Output("!")), Batch(Output("\U0001F680\U0001F680"))),
            maximumTotal: 6));
        var token = await StartSessionAsync(driver, "output-aggregate-start");

        Assert.Equal("A\U0001F600é\U0001D11EZQ", await ReadOutputAsync(driver, token, "output-aggregate-exact"));
        await AdvanceOutputAsync(driver, token, "output-aggregate-first-append");
        Assert.Equal("é\U0001D11EZQ!", await ReadOutputAsync(driver, token, "output-aggregate-first-eviction"));
        Assert.Equal(string.Empty, await ReadOutputAsync(driver, token, "output-aggregate-evicted-category", category: "stdout"));
        await AdvanceOutputAsync(driver, token, "output-aggregate-second-append");
        Assert.Equal("ZQ!\U0001F680\U0001F680", await ReadOutputAsync(driver, token, "output-aggregate-second-eviction"));
    }

    [Theory]
    [InlineData(17, "", "Z")]
    [InlineData(18, "abc... [truncated]", "Z")]
    [InlineData(20, "xyabc... [truncated]", "abc... [truncated]Z")]
    public async Task GetOutput_AggregateLimit_IncludesTruncationSuffixAndNeverRetainsPartialEntries(
        int maximumTotal, string expectedInitial, string expectedAfterAppend)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("xy"), Output("abcdef")),
            batches: new JsonArray(Batch(Output("Z"))),
            maximumEntry: 3,
            maximumTotal: maximumTotal));
        var token = await StartSessionAsync(driver, "output-suffix-budget-start");

        Assert.Equal(expectedInitial, await ReadOutputAsync(driver, token, "output-suffix-budget-initial"));
        await AdvanceOutputAsync(driver, token, "output-suffix-budget-append");
        Assert.Equal(expectedAfterAppend, await ReadOutputAsync(driver, token, "output-suffix-budget-after"));
    }

    [Fact]
    public async Task GetOutput_WholeBufferClear_ResetsRetentionAccountingBeforeLaterAppends()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(Output("AB")),
            batches: new JsonArray(Batch(Output("\U0001F600é\U0001D11EZ")), Batch(Output("WXYZ"))),
            maximumTotal: 4));
        var token = await StartSessionAsync(driver, "output-clear-budget-start");

        Assert.Equal("AB", await ReadOutputAsync(driver, token, "output-clear-budget-first", clear: true));
        await AdvanceOutputAsync(driver, token, "output-clear-budget-first-append");
        Assert.Equal("\U0001F600é\U0001D11EZ", await ReadOutputAsync(driver, token, "output-clear-budget-second", clear: true));
        await AdvanceOutputAsync(driver, token, "output-clear-budget-second-append");
        Assert.Equal("WXYZ", await ReadOutputAsync(driver, token, "output-clear-budget-final"));
    }

    [Fact]
    public async Task GetOutput_GatedAppendOverlappingClear_ConservesEachEntryExactlyOnce()
    {
        const string before = "before|";
        const string appended = "during|";
        var batch = Batch(Output(appended));
        batch["waitForRelease"] = true;
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(Output(before)), batches: new JsonArray(batch)));
        var token = await StartSessionAsync(driver, "output-concurrent-start");
        var append = driver.CallToolRawAsync(
            "get_threads", new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("output-concurrent-append"));
        try
        {
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            await driver.WaitForFixtureRecordAsync("output-batch-gated", observation.Token);
            var clear = driver.CallToolRawAsync(
                Tool, new JsonObject { ["debugSessionId"] = token, ["clear"] = true },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("output-concurrent-clear"));
            driver.ReleaseThreadsResponse();

            var cleared = OutputText(await clear);
            Assert.Equal("threads_success", AssertApplicationEnvelope(await append, isError: false)["kind"]?.GetValue<string>());
            var remaining = await ReadOutputAsync(driver, token, "output-concurrent-remaining");

            // Either linearization order is valid; loss, duplication, or a split entry is not.
            Assert.Contains(cleared, new[] { before, before + appended });
            Assert.Equal(cleared == before ? appended : string.Empty, remaining);
            Assert.Equal(before + appended, cleared + remaining);
        }
        finally
        {
            driver.ReleaseThreadsResponse();
            _ = await append;
        }
    }

    [Fact]
    public async Task GetOutputTail_Catalog_PublishesClosedArgumentsWithUnboundedIntegerLinesAndDefault50()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions());
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("output-tail-catalog")));
        var tool = Assert.Single(
            Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == TailTool);
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId"], Assert.IsType<JsonArray>(schema["required"]).Select(static item => item?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(new[] { "category", "debugSessionId", "lines" }, properties.Select(static item => item.Key).Order(StringComparer.Ordinal));
        Assert.Equal("string", properties["debugSessionId"]?["type"]?.GetValue<string>());
        Assert.Equal(1, properties["debugSessionId"]?["minLength"]?.GetValue<int>());
        var lines = Assert.IsType<JsonObject>(properties["lines"]);
        Assert.Equal("integer", lines["type"]?.GetValue<string>());
        Assert.Equal(50, lines["default"]?.GetValue<int>());
        Assert.False(lines.ContainsKey("minimum"));
        Assert.False(lines.ContainsKey("maximum"));
        Assert.Equal(new[] { "null", "string" }, Assert.IsType<JsonArray>(properties["category"]?["type"])
            .Select(static item => item!.GetValue<string>()).Order(StringComparer.Ordinal));
    }

    [Fact]
    public async Task GetOutputTail_EmptyBuffer_ReturnsZeroCountsForDefaultAndSignedLineCounts()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions());
        var token = await StartSessionAsync(driver, "output-tail-empty-start");
        string?[] counts = [null, "1", "0", "-1", "10000000000000000000000000000000000000000", "-10000000000000000000000000000000000000000"];

        for (var index = 0; index < counts.Length; index++)
        {
            await AssertOutputTailAsync(driver, token, $"output-tail-empty-{index}", 0, 0, string.Empty, counts[index]);
        }
    }

    [Fact]
    public async Task GetOutputTail_OmittedLineCount_ReturnsLast50WithTotalCountBeforeSlicing()
    {
        var allLines = Enumerable.Range(1, 55).Select(static index => $"line-{index.ToString(CultureInfo.InvariantCulture)}").ToArray();
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output(string.Join("\n", allLines) + "\n"))));
        var token = await StartSessionAsync(driver, "output-tail-default-start");
        var expected = string.Join("\n", allLines.Skip(5));

        await AssertOutputTailAsync(driver, token, "output-tail-default", 55, 50, expected);
        await AssertOutputTailAsync(driver, token, "output-tail-explicit-50", 55, 50, expected, "50");
    }

    [Fact]
    public async Task GetOutputTail_SignedIntegralJsonLineCounts_PreservePythonSliceResults()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("one\ntwo\nthree\nfour\n"))));
        var token = await StartSessionAsync(driver, "output-tail-signed-start");
        var cases = new (string LinesJson, int ReturnedLines, string Expected)[]
        {
            ("1", 1, "four"),
            ("2", 2, "three\nfour"),
            ("4", 4, "one\ntwo\nthree\nfour"),
            ("5", 4, "one\ntwo\nthree\nfour"),
            ("0", 4, "one\ntwo\nthree\nfour"),
            ("-0", 4, "one\ntwo\nthree\nfour"),
            ("-1", 3, "two\nthree\nfour"),
            ("-2", 2, "three\nfour"),
            ("-3", 1, "four"),
            ("-4", 0, ""),
            ("-5", 0, ""),
            ("2147483647", 4, "one\ntwo\nthree\nfour"),
            ("-2147483648", 0, ""),
            ("9223372036854775807", 4, "one\ntwo\nthree\nfour"),
            ("-9223372036854775808", 0, ""),
            ("9223372036854775808", 4, "one\ntwo\nthree\nfour"),
            ("-9223372036854775809", 0, ""),
            ("1000000000000000000000000000000000000000000000000000000000000", 4, "one\ntwo\nthree\nfour"),
            ("-1000000000000000000000000000000000000000000000000000000000000", 0, ""),
            ("2.0", 2, "three\nfour"),
            ("-2.000", 2, "three\nfour"),
            ("2e0", 2, "three\nfour"),
            ("20e-1", 2, "three\nfour"),
            ("0.2e1", 2, "three\nfour"),
            ("-20e-1", 2, "three\nfour"),
            ("2000e-3", 2, "three\nfour"),
            ("-0.0", 4, "one\ntwo\nthree\nfour"),
            ("0e-1000", 4, "one\ntwo\nthree\nfour"),
            ("-0e1000", 4, "one\ntwo\nthree\nfour"),
            ("-0e-1000", 4, "one\ntwo\nthree\nfour"),
            ("1e1000", 4, "one\ntwo\nthree\nfour"),
            ("-1e1000", 0, ""),
            ("10000000000000000000000000000000000000000.000", 4, "one\ntwo\nthree\nfour"),
            ("-10000000000000000000000000000000000000000.000", 0, ""),
        };

        // These read-only vectors use identical session state; only the request arguments differ.
        for (var index = 0; index < cases.Length; index++)
        {
            var (linesJson, returnedLines, expected) = cases[index];
            output.WriteLine($"Tail line-count vector: {linesJson}");
            await AssertOutputTailAsync(driver, token, $"output-tail-signed-{index}", 4, returnedLines, expected, linesJson);
        }
    }

    [Theory]
    [InlineData("lf", "\n")]
    [InlineData("cr", "\r")]
    [InlineData("crlf", "\r\n")]
    [InlineData("vertical-tab", "\v")]
    [InlineData("form-feed", "\f")]
    [InlineData("file-separator", "\u001c")]
    [InlineData("group-separator", "\u001d")]
    [InlineData("record-separator", "\u001e")]
    [InlineData("next-line", "\u0085")]
    [InlineData("line-separator", "\u2028")]
    [InlineData("paragraph-separator", "\u2029")]
    public async Task GetOutputTail_PythonLineSeparators_SplitAndNormalizeToLf(string scenario, string separator)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output($"first{separator}second{separator}third{separator}"))));
        var token = await StartSessionAsync(driver, $"output-tail-separator-{scenario}-start");

        await AssertOutputTailAsync(driver, token, $"output-tail-separator-{scenario}-all", 3, 3, "first\nsecond\nthird", "0");
        await AssertOutputTailAsync(driver, token, $"output-tail-separator-{scenario}-last", 3, 2, "second\nthird", "2");
    }

    [Theory]
    [InlineData("", 0, "", "")]
    [InlineData("\n", 1, "", "")]
    [InlineData("\r\n", 1, "", "")]
    [InlineData("\n\n", 2, "\n", "")]
    [InlineData("a", 1, "a", "a")]
    [InlineData("a\n", 1, "a", "a")]
    [InlineData("a\n\n", 2, "a\n", "")]
    [InlineData("\na\n\n", 3, "\na\n", "")]
    [InlineData("a\r\n\r\n", 2, "a\n", "")]
    [InlineData("a\n\nb\n", 3, "a\n\nb", "b")]
    [InlineData("a\u001f\t\0\U0001F600b", 1, "a\u001f\t\0\U0001F600b", "a\u001f\t\0\U0001F600b")]
    public async Task GetOutputTail_TrailingAndEmptyLines_PreservePythonCountsWithoutInventingFinalLine(
        string text, int totalLines, string expectedAll, string expectedLast)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(Output(text))));
        var token = await StartSessionAsync(driver, "output-tail-empty-lines-start");

        await AssertOutputTailAsync(driver, token, "output-tail-empty-lines-all", totalLines, totalLines, expectedAll, "0");
        await AssertOutputTailAsync(driver, token, "output-tail-empty-lines-last", totalLines, totalLines == 0 ? 0 : 1, expectedLast, "1");
    }

    [Fact]
    public async Task GetOutputTail_CrLfAcrossEvents_RecomputesLogicalLinesAfterLaterAppends()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("first\r")),
            batches: new JsonArray(Batch(Output("\nsecond\r")), Batch(Output("\n\r")), Batch(Output("\nlast")))));
        var token = await StartSessionAsync(driver, "output-tail-crlf-start");

        await AssertOutputTailAsync(driver, token, "output-tail-crlf-initial", 1, 1, "first", "0");
        await AdvanceOutputAsync(driver, token, "output-tail-crlf-first-append");
        await AssertOutputTailAsync(driver, token, "output-tail-crlf-first", 2, 2, "first\nsecond", "0");
        await AdvanceOutputAsync(driver, token, "output-tail-crlf-empty-append");
        await AssertOutputTailAsync(driver, token, "output-tail-crlf-empty", 3, 3, "first\nsecond\n", "0");
        await AdvanceOutputAsync(driver, token, "output-tail-crlf-last-append");
        await AssertOutputTailAsync(driver, token, "output-tail-crlf-last", 4, 4, "first\nsecond\n\nlast", "0");
    }

    [Fact]
    public async Task GetOutputTail_CategoryFilter_PrecedesFragmentJoiningSplittingAndSlicing()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(launch: new JsonArray(
            Output("head", "stdout"),
            Output("\nerror\n", "stderr"),
            Output("tail\r", "stdout"),
            Output("discarded", "stderr"),
            Output("\nend\n", "stdout"),
            Output("console"),
            Output("\vlast", "console"),
            Output("\nmetric\n", "telemetry"))));
        var token = await StartSessionAsync(driver, "output-tail-category-start");
        var cases = new (string CategoryShape, int TotalLines, string ExpectedAll, string ExpectedLast)[]
        {
            ("omitted", 8, "head\nerror\ntail\ndiscarded\nend\nconsole\nlast\nmetric", "metric"),
            ("null", 8, "head\nerror\ntail\ndiscarded\nend\nconsole\nlast\nmetric", "metric"),
            ("", 8, "head\nerror\ntail\ndiscarded\nend\nconsole\nlast\nmetric", "metric"),
            ("stdout", 2, "headtail\nend", "end"),
            ("stderr", 3, "\nerror\ndiscarded", "discarded"),
            ("console", 2, "console\nlast", "last"),
            ("telemetry", 2, "\nmetric", "metric"),
            ("STDOUT", 0, "", ""),
            ("Console", 0, "", ""),
            ("unknown", 0, "", ""),
            (" ", 0, "", ""),
        };

        for (var index = 0; index < cases.Length; index++)
        {
            var (categoryShape, totalLines, expectedAll, expectedLast) = cases[index];
            var category = categoryShape is "omitted" or "null" ? null : categoryShape;
            var includeCategory = categoryShape != "omitted";
            output.WriteLine($"Tail category vector: '{categoryShape}'");

            await AssertOutputTailAsync(driver, token, $"output-tail-category-{index}-all", totalLines, totalLines, expectedAll, "0", category, includeCategory);
            await AssertOutputTailAsync(driver, token, $"output-tail-category-{index}-last", totalLines, totalLines == 0 ? 0 : 1, expectedLast, "1", category, includeCategory);
        }
    }

    [Theory]
    [InlineData("exited")]
    [InlineData("terminated")]
    public async Task GetOutputTail_TerminalSession_RemainsReadableWithoutConsumingOutput(string terminalEvent)
    {
        var batch = Batch(Output("final\n", "stderr"));
        batch["terminal"] = terminalEvent;
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("before\n", "stdout")), batches: new JsonArray(batch)));
        var token = await StartSessionAsync(driver, $"output-tail-{terminalEvent}-start");

        await AdvanceOutputAsync(driver, token, $"output-tail-{terminalEvent}-barrier");
        var state = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_debug_state", new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"output-tail-{terminalEvent}-state")), isError: false);
        Assert.Equal("debug_state_success", state["kind"]?.GetValue<string>());
        Assert.Equal(terminalEvent, state["state"]?["event"]?.GetValue<string>());
        await AssertOutputTailAsync(driver, token, $"output-tail-{terminalEvent}-all", 2, 2, "before\nfinal");
        await AssertOutputTailAsync(driver, token, $"output-tail-{terminalEvent}-stderr", 1, 1, "final", "1", "stderr");
        Assert.Equal("before\nfinal\n", await ReadOutputAsync(driver, token, $"output-tail-{terminalEvent}-retained"));
    }

    [Fact]
    public async Task GetOutputTail_RepeatedReadsAndLaterAppends_DoNotMutateRetainedOutput()
    {
        const string original = "one\r\ntwo\nthree";
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("one\r\n", "stdout"), Output("two\n", "stderr"), Output("three", "stdout")),
            batches: new JsonArray(Batch(Output("\nfour\n", "stdout")))));
        var token = await StartSessionAsync(driver, "output-tail-retained-start");

        await AssertOutputTailAsync(driver, token, "output-tail-retained-first", 3, 1, "three", "1");
        await AssertOutputTailAsync(driver, token, "output-tail-retained-filtered", 2, 2, "one\nthree", "0", "stdout");
        await AssertOutputTailAsync(driver, token, "output-tail-retained-negative", 3, 1, "three", "-2");
        await AssertOutputTailAsync(driver, token, "output-tail-retained-repeat", 3, 1, "three", "1");
        Assert.Equal(original, await ReadOutputAsync(driver, token, "output-tail-retained-raw"));
        await AdvanceOutputAsync(driver, token, "output-tail-retained-append");
        await AssertOutputTailAsync(driver, token, "output-tail-retained-appended", 4, 2, "three\nfour", "2");
        Assert.Equal(original + "\nfour\n", await ReadOutputAsync(driver, token, "output-tail-retained-clear", clear: true));
        await AssertOutputTailAsync(driver, token, "output-tail-retained-after-clear", 0, 0, string.Empty, "0");
    }

    [Fact]
    public async Task GetOutputTail_InvalidInput_ReturnsCompleteInvalidArgumentsWithoutDapIo()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions());
        foreach (var (scenario, arguments) in InvalidTailArguments)
        {
            output.WriteLine($"Tail invalid-input vector: {scenario}");
            var before = await driver.ReadNativeActionsAsync();
            var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
                TailTool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"output-tail-invalid-{scenario}")), isError: true);

            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
            Assert.Equal(TailTool, content["tool"]?.GetValue<string>());
            Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        }
    }

    [Fact]
    public async Task GetOutputTail_OmittedArguments_ReturnsCompleteInvalidArgumentsWithoutDapIo()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions());
        var before = await driver.ReadNativeActionsAsync();
        var content = AssertApplicationEnvelope(await driver.SendRawRequestAsync(
            "tools/call", new JsonObject { ["name"] = TailTool, ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
            new RequestId("output-tail-omitted-arguments")), isError: true);

        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(TailTool, content["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task GetOutputTail_MissingAndRetiredSessions_ReturnUniformCompleteNotFoundWithoutDapIo()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("retired private output\n"))));
        var token = await StartSessionAsync(driver, "output-tail-retired-start");
        var stopped = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "stop_debug", new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("output-tail-retire")), isError: false);
        Assert.Equal("stop_debug_success", stopped["kind"]?.GetValue<string>());
        var before = await driver.ReadNativeActionsAsync();
        JsonObject? previous = null;
        var tokens = new[] { "!", "short", new string('r', 32), token };

        for (var index = 0; index < tokens.Length; index++)
        {
            var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
                TailTool, new JsonObject { ["debugSessionId"] = tokens[index], ["lines"] = 0, ["category"] = null },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"output-tail-unavailable-{index}")), isError: true);
            Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
            Assert.Equal(2, content.Count);
            if (previous is not null)
            {
                Assert.Equal(previous.ToJsonString(), content.ToJsonString());
            }

            previous = content;
        }

        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("get_modules", "modules")]
    [InlineData("get_progress", "progress")]
    public async Task RuntimeInventory_EmptySession_ReturnsCompleteEmptySnapshotWithoutDapIo(string tool, string field)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions());
        var token = await StartSessionAsync(driver, $"{tool}-empty-start");

        var content = await ReadInventoryAsync(driver, token, tool, $"{tool}-empty");

        Assert.Equal($"{field}_success", content["kind"]?.GetValue<string>());
        Assert.Equal(0, content["count"]?.GetValue<int>());
        Assert.Empty(Assert.IsType<JsonArray>(content[field]));
    }

    [Fact]
    public async Task GetModules_ReaderOrderedTransitions_PreserveTypedIdsInsertionOrderAndReplacementDefaults()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(
                ModuleEvent("new", JsonValue.Create(7)!, "numeric", path: "private-path", version: "1", optimized: true, symbols: "loaded"),
                ModuleEvent("new", JsonValue.Create("7")!, "string"),
                ModuleEvent("new", JsonValue.Create("removed")!, "remove-me"),
                ModuleEvent("new", JsonValue.Create(7)!, "ignored-duplicate"),
                ModuleEvent("changed", JsonValue.Create("unknown")!, "ignored-change"),
                ModuleEvent("removed", JsonValue.Create("unknown")!, "ignored-remove")),
            batches: new JsonArray(
                Batch(ModuleEvent("changed", JsonValue.Create(7)!, "replacement"),
                    ModuleEvent("new", JsonValue.Create("7")!, "ignored-string-duplicate"),
                    ModuleEvent("removed", JsonValue.Create("removed")!, "ignored")),
                Batch(ModuleEvent("changed", JsonValue.Create("7")!, "string-replaced", path: "string-path", version: "2", optimized: true, symbols: "string-symbols"),
                    ModuleEvent("removed", JsonValue.Create(7)!, "ignored"),
                    ModuleEvent("new", JsonValue.Create(7)!, "reinserted")),
                Batch(InventoryEvent("module", new JsonObject { ["reason"] = "removed", ["module"] = new JsonObject { ["id"] = "7" } })))));
        var token = await StartSessionAsync(driver, "modules-transitions-start");
        var initial = Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_modules", "modules-initial"))["modules"]);
        Assert.Equal(3, initial.Count);
        Assert.Equal("numeric", initial[0]?["name"]?.GetValue<string>());
        Assert.Equal("private-path", initial[0]?["path"]?.GetValue<string>());
        Assert.Equal("1", initial[0]?["version"]?.GetValue<string>());
        Assert.True(initial[0]?["isOptimized"]?.GetValue<bool>());
        Assert.Equal("loaded", initial[0]?["symbolStatus"]?.GetValue<string>());

        await AdvanceOutputAsync(driver, token, "modules-change-barrier");
        var changed = Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_modules", "modules-changed"))["modules"]);
        Assert.Equal(2, changed.Count);
        Assert.Equal(7, changed[0]?["id"]?.GetValue<int>());
        Assert.Equal("7", changed[1]?["id"]?.GetValue<string>());
        Assert.Equal("replacement", changed[0]?["name"]?.GetValue<string>());
        Assert.Equal("string", changed[1]?["name"]?.GetValue<string>());
        foreach (var item in changed.Select(static node => Assert.IsType<JsonObject>(node)))
        {
            Assert.Equal(6, item.Count);
            Assert.True(item.ContainsKey("path"));
            Assert.Null(item["path"]);
            Assert.True(item.ContainsKey("version"));
            Assert.Null(item["version"]);
            Assert.False(item["isOptimized"]?.GetValue<bool>() ?? true);
            Assert.True(item.ContainsKey("symbolStatus"));
            Assert.Null(item["symbolStatus"]);
        }

        await AdvanceOutputAsync(driver, token, "modules-reinsert-barrier");
        var final = Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_modules", "modules-final"))["modules"]);
        Assert.Equal("7", final[0]?["id"]?.GetValue<string>());
        Assert.Equal(7, final[1]?["id"]?.GetValue<int>());
        Assert.Equal("reinserted", final[1]?["name"]?.GetValue<string>());
        Assert.Equal("string-replaced", final[0]?["name"]?.GetValue<string>());
        Assert.Equal("string-path", final[0]?["path"]?.GetValue<string>());
        Assert.Equal("2", final[0]?["version"]?.GetValue<string>());
        Assert.True(final[0]?["isOptimized"]?.GetValue<bool>());
        Assert.Equal("string-symbols", final[0]?["symbolStatus"]?.GetValue<string>());
        await AdvanceOutputAsync(driver, token, "modules-string-remove-barrier");
        var remaining = Assert.Single(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_modules", "modules-string-removed"))["modules"]));
        Assert.Equal(7, remaining?["id"]?.GetValue<int>());
        Assert.Equal("numeric", initial[0]?["name"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetProgress_ReaderOrderedTransitions_PreserveOmittedFieldsReplaceStartsAndRemoveEnds()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(
                InventoryEvent("progressStart", new JsonObject { ["progressId"] = "first", ["title"] = "initial" }),
                InventoryEvent("progressStart", new JsonObject
                {
                    ["progressId"] = "second",
                    ["title"] = "working",
                    ["message"] = "original",
                    ["percentage"] = 20,
                    ["cancellable"] = true,
                })),
            batches: new JsonArray(
                Batch(InventoryEvent("progressStart", new JsonObject { ["progressId"] = "first", ["title"] = "replaced" }),
                    InventoryEvent("progressUpdate", new JsonObject { ["progressId"] = "second", ["message"] = "updated", ["percentage"] = null }),
                    InventoryEvent("progressUpdate", new JsonObject { ["progressId"] = "second" }),
                    InventoryEvent("progressUpdate", new JsonObject { ["progressId"] = "unknown", ["percentage"] = 90 }),
                    InventoryEvent("progressEnd", new JsonObject { ["progressId"] = "unknown" })),
                Batch(InventoryEvent("progressEnd", new JsonObject { ["progressId"] = "first" }),
                    InventoryEvent("progressUpdate", new JsonObject { ["progressId"] = "second", ["message"] = null, ["percentage"] = 75 }),
                    InventoryEvent("progressStart", new JsonObject { ["progressId"] = "third", ["title"] = "last" })),
                Batch(InventoryEvent("progressStart", new JsonObject { ["progressId"] = "second", ["title"] = "restarted" })))));
        var token = await StartSessionAsync(driver, "progress-transitions-start");
        var initialize = Assert.Single(await driver.ReadNativeActionsAsync(), static action => action.Command == "initialize");
        Assert.True(JsonNode.Parse(initialize.Detail!)?["arguments"]?["supportsProgressReporting"]?.GetValue<bool>() ?? false);
        var initial = Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-initial"))["progress"]);
        Assert.Equal(2, initial.Count);
        var first = Assert.IsType<JsonObject>(initial[0]);
        Assert.Equal(6, first.Count);
        Assert.True(first.ContainsKey("message"));
        Assert.Null(first["message"]);
        Assert.True(first.ContainsKey("percentage"));
        Assert.Null(first["percentage"]);
        Assert.False(first["cancellable"]?.GetValue<bool>() ?? true);
        var firstStarted = first["startedAt"]!.GetValue<double>();
        var secondStarted = initial[1]!["startedAt"]!.GetValue<double>();
        Assert.True(double.IsFinite(firstStarted) && firstStarted >= 0);
        Assert.True(double.IsFinite(secondStarted) && secondStarted >= firstStarted);

        await AdvanceOutputAsync(driver, token, "progress-update-barrier");
        var changed = Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-changed"))["progress"]);
        Assert.Equal(new[] { "first", "second" }, changed.Select(static item => item!["progressId"]!.GetValue<string>()));
        Assert.Equal("replaced", changed[0]?["title"]?.GetValue<string>());
        Assert.True(changed[0]!["startedAt"]!.GetValue<double>() >= firstStarted);
        Assert.Equal("updated", changed[1]?["message"]?.GetValue<string>());
        Assert.Equal(20, changed[1]?["percentage"]?.GetValue<int>());
        Assert.True(changed[1]?["cancellable"]?.GetValue<bool>());
        Assert.Equal(secondStarted, changed[1]?["startedAt"]?.GetValue<double>());

        await AdvanceOutputAsync(driver, token, "progress-end-barrier");
        var final = Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-final"))["progress"]);
        Assert.Equal(new[] { "second", "third" }, final.Select(static item => item!["progressId"]!.GetValue<string>()));
        Assert.Equal("updated", final[0]?["message"]?.GetValue<string>());
        Assert.Equal(75, final[0]?["percentage"]?.GetValue<int>());
        Assert.Equal(secondStarted, final[0]?["startedAt"]?.GetValue<double>());
        Assert.True(final[1]!["startedAt"]!.GetValue<double>() >= changed[0]!["startedAt"]!.GetValue<double>());
        await AdvanceOutputAsync(driver, token, "progress-restart-barrier");
        var restarted = Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-restarted"))["progress"]);
        Assert.Equal(new[] { "second", "third" }, restarted.Select(static item => item!["progressId"]!.GetValue<string>()));
        Assert.Equal("restarted", restarted[0]?["title"]?.GetValue<string>());
        Assert.Null(restarted[0]?["message"]);
        Assert.Null(restarted[0]?["percentage"]);
        Assert.False(restarted[0]?["cancellable"]?.GetValue<bool>() ?? true);
        Assert.True(restarted[0]!["startedAt"]!.GetValue<double>() >= final[1]!["startedAt"]!.GetValue<double>());
    }

    [Fact]
    public async Task GetProgress_UpdateDistinguishesNewlineFromLiteralEscape()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(InventoryEvent("progressStart", new JsonObject
            {
                ["progressId"] = "edge",
                ["title"] = "working",
                ["message"] = "\n",
            })),
            batches: new JsonArray(Batch(InventoryEvent("progressUpdate", new JsonObject
            {
                ["progressId"] = "edge",
                ["message"] = "\\n",
            })))));
        var token = await StartSessionAsync(driver, "progress-escape-start");
        var initial = Assert.Single(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-escape-before"))["progress"]));
        Assert.Equal("\n", initial?["message"]?.GetValue<string>());

        await AdvanceOutputAsync(driver, token, "progress-escape-barrier");
        var updated = Assert.Single(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-escape-after"))["progress"]));
        Assert.Equal("\\n", updated?["message"]?.GetValue<string>());
    }

    [Fact]
    public async Task GetProgress_PercentageBoundaries_AcceptsZeroFractionAndOneHundred()
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(InventoryEvent("progressStart", new JsonObject { ["progressId"] = "edge", ["title"] = "working", ["percentage"] = 0 })),
            batches: new JsonArray(
                Batch(InventoryEvent("progressUpdate", new JsonObject { ["progressId"] = "edge", ["percentage"] = 0.25 })),
                Batch(InventoryEvent("progressUpdate", new JsonObject { ["progressId"] = "edge", ["percentage"] = 100 })))));
        var token = await StartSessionAsync(driver, "progress-boundaries-start");
        var initial = Assert.Single(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-zero"))["progress"]));
        Assert.Equal(0, initial?["percentage"]?.GetValue<double>());
        await AdvanceOutputAsync(driver, token, "progress-fraction-barrier");
        var fractional = Assert.Single(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-fraction"))["progress"]));
        Assert.Equal(0.25, fractional?["percentage"]?.GetValue<double>());
        await AdvanceOutputAsync(driver, token, "progress-hundred-barrier");
        var complete = Assert.Single(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, token, "get_progress", "progress-hundred"))["progress"]));
        Assert.Equal(100, complete?["percentage"]?.GetValue<double>());
    }

    [Theory]
    [InlineData("get_modules", "modules")]
    [InlineData("get_progress", "progress")]
    public async Task RuntimeInventory_IndependentSessions_IsolateTransitionsAndLeaveOutputUnchanged(string tool, string field)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("before|"), ModuleEvent("new", JsonValue.Create(1)!, "first"),
                InventoryEvent("progressStart", new JsonObject { ["progressId"] = "first", ["title"] = "working" })),
            batches: new JsonArray(Batch(Output("after|"), ModuleEvent("removed", JsonValue.Create(1)!, "ignored"),
                InventoryEvent("progressEnd", new JsonObject { ["progressId"] = "first" })))));
        var first = await StartSessionAsync(driver, $"{tool}-isolation-first");
        var second = await StartSessionAsync(driver, $"{tool}-isolation-second");
        Assert.NotEqual(first, second);
        Assert.Single(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, first, tool, $"{tool}-first-before"))[field]));
        await AdvanceOutputAsync(driver, first, $"{tool}-isolation-barrier");

        Assert.Empty(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, first, tool, $"{tool}-first-after"))[field]));
        Assert.Single(Assert.IsType<JsonArray>((await ReadInventoryAsync(driver, second, tool, $"{tool}-second-unchanged"))[field]));
        Assert.Equal("before|after|", await ReadOutputAsync(driver, first, $"{tool}-first-output"));
        Assert.Equal("before|", await ReadOutputAsync(driver, second, $"{tool}-second-output"));
        await AssertOutputTailAsync(driver, first, $"{tool}-neighbor-tail", 1, 1, "before|after|");
    }

    [Theory]
    [MemberData(nameof(InvalidArguments))]
    public async Task RuntimeInventory_InvalidArguments_ReturnCompleteErrorWithoutDapIo(string scenario, JsonObject? arguments)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions());
        var before = await driver.ReadNativeActionsAsync();
        foreach (var tool in new[] { "get_modules", "get_progress" })
        {
            var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
                tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"{tool}-invalid-{scenario}")), isError: true);
            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
            Assert.Equal(tool, content["tool"]?.GetValue<string>());
        }

        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("get_modules")]
    [InlineData("get_progress")]
    public async Task RuntimeInventory_UnknownAndRetiredSessions_ReturnUniformNotFoundWithoutDapIo(string tool)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions());
        var token = await StartSessionAsync(driver, $"{tool}-retired-start");
        AssertApplicationEnvelope(await driver.CallToolRawAsync("stop_debug", new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"{tool}-retire")), isError: false);
        var before = await driver.ReadNativeActionsAsync();
        JsonObject? previous = null;
        foreach (var unavailable in new[] { "!", "short", new string('r', 32), token })
        {
            var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(tool,
                new JsonObject { ["debugSessionId"] = unavailable }, ModernMcpProcessDriver.CurrentMeta(),
                new RequestId($"{tool}-unavailable-{unavailable}")), isError: true);
            Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
            Assert.Equal(2, content.Count);
            if (previous is not null)
            {
                Assert.Equal(previous.ToJsonString(), content.ToJsonString());
            }
            previous = content;
        }

        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("get_modules", "module", "null")]
    [InlineData("get_modules", "module", "{}")]
    [InlineData("get_modules", "module", "{\"reason\":\"new\",\"module\":{\"id\":true,\"name\":\"private-inventory-marker\"}}")]
    [InlineData("get_modules", "module", "{\"reason\":\"new\",\"module\":{\"id\":1}}")]
    [InlineData("get_modules", "module", "{\"reason\":\"changed\",\"module\":{\"id\":1,\"name\":\"private-inventory-marker\",\"isOptimized\":\"yes\"}}")]
    [InlineData("get_progress", "progressStart", "null")]
    [InlineData("get_progress", "progressStart", "{\"progressId\":\"first\",\"message\":\"private-inventory-marker\"}")]
    [InlineData("get_progress", "progressStart", "{\"progressId\":\"first\",\"title\":\"private-inventory-marker\",\"cancellable\":1}")]
    [InlineData("get_progress", "progressUpdate", "{\"progressId\":\"first\",\"message\":false}")]
    [InlineData("get_progress", "progressUpdate", "{\"progressId\":\"first\",\"percentage\":\"private-inventory-marker\"}")]
    [InlineData("get_progress", "progressStart", "{\"progressId\":\"first\",\"title\":\"bad\",\"percentage\":-0.1}")]
    [InlineData("get_progress", "progressStart", "{\"progressId\":\"first\",\"title\":\"bad\",\"percentage\":100.1}")]
    [InlineData("get_progress", "progressUpdate", "{\"progressId\":\"first\",\"percentage\":-1}")]
    [InlineData("get_progress", "progressUpdate", "{\"progressId\":\"first\",\"percentage\":101}")]
    [InlineData("get_progress", "progressEnd", "{}")]
    public async Task RuntimeInventory_MalformedEvent_ReturnsRedactedIncompleteWithoutKillingSession(
        string tool, string eventName, string bodyJson)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("retained-output|"), ModuleEvent("new", JsonValue.Create(1)!, "private-inventory-marker"),
                InventoryEvent("progressStart", new JsonObject { ["progressId"] = "first", ["title"] = "private-inventory-marker" })),
            batches: new JsonArray(Batch(InventoryEvent(eventName, JsonNode.Parse(bodyJson)), Output("still-usable|")))));
        var token = await StartSessionAsync(driver, $"{tool}-malformed-start");
        await AdvanceOutputAsync(driver, token, $"{tool}-malformed-barrier");

        var incomplete = await ReadInventoryAsync(driver, token, tool, $"{tool}-malformed-read", isError: true);
        AssertInventoryError(incomplete, tool, "runtime_inventory_incomplete");
        var repeated = await ReadInventoryAsync(driver, token, tool, $"{tool}-malformed-repeat", isError: true);
        Assert.Equal(incomplete.ToJsonString(), repeated.ToJsonString());
        var neighbor = tool == "get_modules" ? "get_progress" : "get_modules";
        Assert.Equal(1, (await ReadInventoryAsync(driver, token, neighbor, $"{tool}-malformed-neighbor"))["count"]?.GetValue<int>());
        Assert.Equal("retained-output|still-usable|", await ReadOutputAsync(driver, token, $"{tool}-malformed-output"));
        await AdvanceOutputAsync(driver, token, $"{tool}-malformed-still-usable");
    }

    [Theory]
    [InlineData("get_modules", "modules", "name", 240_000, "x")]
    [InlineData("get_progress", "progress", "title", 240_000, "x")]
    [InlineData("get_modules", "modules", "name", 40_000, "\u0001")]
    [InlineData("get_progress", "progress", "title", 40_000, "\u0001")]
    public async Task RuntimeInventory_SerializedSnapshotBelow256KiB_ReturnsEntireUntruncatedEntry(
        string tool, string field, string textField, int textLength, string character)
    {
        var entry = GeneratedInventoryEvent(tool, 1, textLength);
        entry["generatedTextCharacter"] = character;
        await using var driver = await StartTailDriverAsync(OutputOptions(batches: new JsonArray(Batch(entry))));
        var token = await StartSessionAsync(driver, $"{tool}-below-snapshot-start");
        await AdvanceOutputAsync(driver, token, $"{tool}-below-snapshot-barrier", timeout: TimeSpan.FromSeconds(10));

        var content = await ReadInventoryAsync(driver, token, tool, $"{tool}-below-snapshot-read");
        var item = Assert.Single(Assert.IsType<JsonArray>(content[field]));
        Assert.Equal(new string(character[0], textLength), item?[textField]?.GetValue<string>());
        Assert.True(System.Text.Encoding.UTF8.GetByteCount(content.ToJsonString()) <= 256 * 1024);
    }

    [Theory]
    [InlineData("get_modules", 270_000, "x")]
    [InlineData("get_progress", 270_000, "x")]
    [InlineData("get_modules", 50_000, "\u0001")]
    [InlineData("get_progress", 50_000, "\u0001")]
    public async Task RuntimeInventory_SerializedSnapshotOver256KiB_ReturnsRedactedTooLargeWithoutTruncation(
        string tool, int textLength, string character)
    {
        var large = GeneratedInventoryEvent(tool, 1, textLength);
        large["generatedTextCharacter"] = character;
        var remove = tool == "get_modules"
            ? ModuleEvent("removed", JsonValue.Create(0)!, "ignored")
            : InventoryEvent("progressEnd", new JsonObject { ["progressId"] = "generated-0" });
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("unchanged-output|")),
            batches: new JsonArray(Batch(large), Batch(remove))));
        var token = await StartSessionAsync(driver, $"{tool}-oversize-start");
        await AdvanceOutputAsync(driver, token, $"{tool}-oversize-barrier", timeout: TimeSpan.FromSeconds(10));

        var content = await ReadInventoryAsync(driver, token, tool, $"{tool}-oversize-read", isError: true);
        AssertInventoryError(content, tool, "runtime_inventory_too_large");
        Assert.Equal(content.ToJsonString(), (await ReadInventoryAsync(driver, token, tool, $"{tool}-oversize-repeat", isError: true)).ToJsonString());
        Assert.Equal("unchanged-output|", await ReadOutputAsync(driver, token, $"{tool}-oversize-output"));
        await AdvanceOutputAsync(driver, token, $"{tool}-oversize-still-usable");
        var recovered = await ReadInventoryAsync(driver, token, tool, $"{tool}-oversize-removed");
        Assert.Equal(0, recovered["count"]?.GetValue<int>());
    }

    [Theory]
    [InlineData(0)]
    [InlineData(1)]
    public async Task GetModules_ExactSerializedLimit_AcceptsLimitAndRejectsOneByteOver(int excessBytes)
    {
        const int limit = 256 * 1024;
        var template = new JsonObject
        {
            ["kind"] = "modules_success",
            ["modules"] = new JsonArray(new JsonObject
            {
                ["id"] = 0,
                ["name"] = "",
                ["path"] = null,
                ["version"] = null,
                ["isOptimized"] = false,
                ["symbolStatus"] = null,
            }),
            ["count"] = 1,
        };
        var size = limit - System.Text.Encoding.UTF8.GetByteCount(template.ToJsonString()) + excessBytes;
        await using var driver = await StartTailDriverAsync(OutputOptions(
            batches: new JsonArray(Batch(GeneratedInventoryEvent("get_modules", 1, size)))));
        var token = await StartSessionAsync(driver, $"modules-exact-limit-{excessBytes}-start");
        await AdvanceOutputAsync(driver, token, $"modules-exact-limit-{excessBytes}-barrier", timeout: TimeSpan.FromSeconds(10));
        var content = await ReadInventoryAsync(driver, token, "get_modules", $"modules-exact-limit-{excessBytes}", isError: excessBytes != 0);
        if (excessBytes == 0)
        {
            Assert.Equal(limit, System.Text.Encoding.UTF8.GetByteCount(content.ToJsonString()));
            Assert.Equal(size, Assert.Single(Assert.IsType<JsonArray>(content["modules"]))?["name"]?.GetValue<string>().Length);
        }
        else
        {
            AssertInventoryError(content, "get_modules", "runtime_inventory_too_large");
        }
    }

    [Theory]
    [InlineData("get_modules")]
    [InlineData("get_progress")]
    public async Task RuntimeInventory_RetentionOver16MiB_IsIncompleteOnlyForAffectedInventory(string tool)
    {
        const int oneMiB = 1024 * 1024;
        var neighbor = tool == "get_modules" ? "get_progress" : "get_modules";
        await using var driver = await StartTailDriverAsync(OutputOptions(
            launch: new JsonArray(Output("retained-output|")),
            batches: new JsonArray(
                Batch(GeneratedInventoryEvent(tool, 9, oneMiB), GeneratedInventoryEvent(neighbor, 9, oneMiB)),
                Batch(GeneratedInventoryEvent(tool, 8, oneMiB, offset: 9), Output("after-overflow|")))));
        var token = await StartSessionAsync(driver, $"{tool}-retention-start");
        await AdvanceOutputAsync(driver, token, $"{tool}-per-inventory-barrier", timeout: TimeSpan.FromSeconds(30));

        // Combined retention exceeds 16 MiB, but each inventory is below its own limit.
        AssertInventoryError(await ReadInventoryAsync(driver, token, tool, $"{tool}-below-retention", isError: true),
            tool, "runtime_inventory_too_large");
        AssertInventoryError(await ReadInventoryAsync(driver, token, neighbor, $"{tool}-neighbor-below-retention", isError: true),
            neighbor, "runtime_inventory_too_large");

        await AdvanceOutputAsync(driver, token, $"{tool}-overflow-barrier", timeout: TimeSpan.FromSeconds(30));
        var incomplete = await ReadInventoryAsync(driver, token, tool, $"{tool}-overflow-read", isError: true);
        AssertInventoryError(incomplete, tool, "runtime_inventory_incomplete");
        Assert.Equal(incomplete.ToJsonString(), (await ReadInventoryAsync(driver, token, tool, $"{tool}-overflow-repeat", isError: true)).ToJsonString());
        AssertInventoryError(await ReadInventoryAsync(driver, token, neighbor, $"{tool}-neighbor-still-complete", isError: true),
            neighbor, "runtime_inventory_too_large");
        Assert.Equal("retained-output|after-overflow|", await ReadOutputAsync(driver, token, $"{tool}-overflow-output"));
        await AdvanceOutputAsync(driver, token, $"{tool}-overflow-still-usable");
    }

    [Theory]
    [InlineData("get_modules")]
    [InlineData("get_progress")]
    [InlineData("search_output")]
    public async Task RuntimeInventory_NonObjectArguments_ReturnInvalidArgumentsBeforeDapIo(string tool)
    {
        await using var driver = await StartTailDriverAsync(OutputOptions());
        var before = await driver.ReadNativeActionsAsync();
        var shapes = new[] { "null", "17", "true", "[]", "\"not-an-object\"" };
        for (var index = 0; index < shapes.Length; index++)
        {
            var content = AssertApplicationEnvelope(await driver.SendRawRequestAsync("tools/call", new JsonObject
            {
                ["name"] = tool,
                ["arguments"] = JsonNode.Parse(shapes[index]),
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
            }, new RequestId($"{tool}-invalid-shape-{index}")), isError: true);
            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
            Assert.Equal(tool, content["tool"]?.GetValue<string>());
        }
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    private static JsonObject GeneratedInventoryEvent(string tool, int count, int textLength, int offset = 0)
    {
        var entry = tool == "get_modules"
            ? ModuleEvent("new", JsonValue.Create(0)!, "private-inventory-marker")
            : InventoryEvent("progressStart", new JsonObject { ["progressId"] = "generated", ["title"] = "private-inventory-marker" });
        entry["generatedCount"] = count;
        entry["generatedIdOffset"] = offset;
        entry["generatedTextLength"] = textLength;
        return entry;
    }

    private static void AssertInventoryError(JsonObject content, string tool, string kind)
    {
        Assert.Equal(kind, content["kind"]?.GetValue<string>());
        Assert.Equal(tool, content["tool"]?.GetValue<string>());
        Assert.False(content.ContainsKey("modules"));
        Assert.False(content.ContainsKey("progress"));
        Assert.False(content.ContainsKey("count"));
        Assert.DoesNotContain("private-inventory-marker", content.ToJsonString());
        Assert.DoesNotContain("retained-output", content.ToJsonString());
        Assert.DoesNotContain("generated-", content.ToJsonString());
        Assert.True(System.Text.Encoding.UTF8.GetByteCount(content.ToJsonString()) <= 256 * 1024);
        StructuredContentSchemaParityTests.AssertMatchesSchema(content);
    }

    private static JsonObject InventoryEvent(string name, JsonNode? body) => new() { ["event"] = name, ["body"] = body };

    private static JsonObject ModuleEvent(string reason, JsonNode id, string name,
        string? path = null, string? version = null, bool? optimized = null, string? symbols = null)
    {
        var module = new JsonObject { ["id"] = id, ["name"] = name };
        if (path is not null) module["path"] = path;
        if (version is not null) module["version"] = version;
        if (optimized.HasValue) module["isOptimized"] = optimized.Value;
        if (symbols is not null) module["symbolStatus"] = symbols;
        return InventoryEvent("module", new JsonObject { ["reason"] = reason, ["module"] = module });
    }

    private static async Task<JsonObject> ReadInventoryAsync(ModernMcpProcessDriver driver, string token, string tool, string requestId,
        bool isError = false)
    {
        var before = await driver.ReadNativeActionsAsync();
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(tool,
            new JsonObject { ["debugSessionId"] = token }, ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError);
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        if (!isError)
        {
            var field = tool == "get_modules" ? "modules" : "progress";
            Assert.Equal($"{field}_success", content["kind"]?.GetValue<string>());
            Assert.Equal(3, content.Count);
            Assert.Equal(Assert.IsType<JsonArray>(content[field]).Count, content["count"]?.GetValue<int>());
            StructuredContentSchemaParityTests.AssertMatchesSchema(content);
        }

        return content;
    }

    // Bound host startup/discovery separately; tool calls retain the driver's two-second deadline.
    private static Task<ModernMcpProcessDriver> StartTailDriverAsync(ModernMcpStartOptions options) =>
        ModernMcpProcessDriver.StartAsync(options, startupTimeout: TimeSpan.FromSeconds(10));

    private static ModernMcpStartOptions OutputOptions(
        JsonArray? launch = null,
        JsonArray? batches = null,
        int? maximumEntry = 100_000,
        int? maximumTotal = 10_000_000) => new(
        DisableFormElicitation: true,
        FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true),
        AdditionalEnvironment: new Dictionary<string, string?>
        {
            ["CONTROLLED_DAP_OUTPUT_SCRIPT"] = new JsonObject
            {
                ["launch"] = launch ?? new JsonArray(),
                ["threads"] = batches ?? new JsonArray(),
            }.ToJsonString(),
            ["NETCOREDBG_MAX_OUTPUT_ENTRY"] = maximumEntry?.ToString(CultureInfo.InvariantCulture),
            ["NETCOREDBG_MAX_OUTPUT_BYTES"] = maximumTotal?.ToString(CultureInfo.InvariantCulture),
        },
        InheritEnvironmentVariables: maximumEntry.HasValue && maximumTotal.HasValue);

    private static JsonObject Output(string text, string? category = null)
    {
        var body = new JsonObject { ["output"] = text };
        if (category is not null)
        {
            body["category"] = category;
        }

        return body;
    }

    private static JsonObject Batch(params JsonObject[] entries) => new() { ["entries"] = new JsonArray(entries) };

    private static JsonArray MixedOutput() => new(
        Output("first\r\n", "stdout"),
        Output("warn\n", "stderr"),
        Output("console", "console"),
        Output("\0\U0001F600"),
        Output("UPPER", "STDOUT"),
        Output("metric|", "telemetry"),
        Output(string.Empty, "stderr"),
        Output("tail", "stdout"));

    private static async Task<string> StartSessionAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var response = await driver.CallToolRawAsync(
            "start_debug", new JsonObject { ["program"] = driver.InertProgramPath },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId), timeout: TimeSpan.FromSeconds(10));
        var result = ModernMcpProcessDriver.RequireResult(response);
        if (result["isError"]?.GetValue<bool>() == true)
        {
            var structured = result["structuredContent"] as JsonObject;
            throw new Xunit.Sdk.XunitException(
                $"start_debug unexpectedly returned isError=true: kind={DiagnosticCode(structured?["kind"])}, error={DiagnosticCode(structured?["error"])}");
        }

        var content = AssertApplicationEnvelope(response, isError: false);
        Assert.Equal("start_debug_success", content["kind"]?.GetValue<string>());
        return Assert.IsType<string>(content["debugSessionId"]?.GetValue<string>());
    }

    private static string DiagnosticCode(JsonNode? node)
    {
        if (node is not JsonValue value || !value.TryGetValue<string>(out var text) || text is null)
        {
            return "<missing-or-non-string>";
        }

        return text.Length <= 128 && text.All(static character => char.IsAsciiLetterOrDigit(character) || character is '_' or '-')
            ? text
            : "<non-code-value-omitted>";
    }

    private static async Task AdvanceOutputAsync(ModernMcpProcessDriver driver, string token, string requestId, TimeSpan? timeout = null)
    {
        // The fixture writes the entire batch before this response. Reader order is the barrier.
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_threads", new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId), timeout: timeout), isError: false);
        Assert.Equal("threads_success", content["kind"]?.GetValue<string>());
    }

    private static async Task<string> ReadOutputAsync(
        ModernMcpProcessDriver driver,
        string token,
        string requestId,
        bool? clear = null,
        string? category = null,
        bool includeCategory = false)
    {
        var arguments = new JsonObject { ["debugSessionId"] = token };
        if (clear is { } shouldClear)
        {
            arguments["clear"] = shouldClear;
        }

        if (includeCategory || category is not null)
        {
            arguments["category"] = category;
        }

        var before = await driver.ReadNativeActionsAsync();
        var output = OutputText(await driver.CallToolRawAsync(
            Tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)));
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
        return output;
    }

    private static async Task AssertOutputTailAsync(
        ModernMcpProcessDriver driver,
        string token,
        string requestId,
        int totalLines,
        int returnedLines,
        string expected,
        string? linesJson = null,
        string? category = null,
        bool includeCategory = false)
    {
        var arguments = new JsonObject { ["debugSessionId"] = token };
        if (linesJson is not null)
        {
            arguments["lines"] = JsonNode.Parse(linesJson);
        }

        if (includeCategory || category is not null)
        {
            arguments["category"] = category;
        }

        var before = await driver.ReadNativeActionsAsync();
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            TailTool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: false);
        Assert.Equal("output_tail_success", content["kind"]?.GetValue<string>());
        Assert.Equal(4, content.Count);
        Assert.Equal(totalLines, content["totalLines"]?.GetValue<int>());
        Assert.Equal(returnedLines, content["returnedLines"]?.GetValue<int>());
        Assert.Equal(expected, content["output"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    private static string OutputText(JsonRpcResponse response)
    {
        var content = AssertApplicationEnvelope(response, isError: false);
        Assert.Equal("output_success", content["kind"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        return Assert.IsType<string>(content["output"]?.GetValue<string>());
    }

    private static JsonObject AssertApplicationEnvelope(JsonRpcResponse response, bool isError)
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
