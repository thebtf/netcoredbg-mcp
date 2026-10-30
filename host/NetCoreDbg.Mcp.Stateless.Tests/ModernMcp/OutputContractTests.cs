using System.Globalization;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class OutputContractTests
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

    public static TheoryData<string, JsonObject?> InvalidTailArguments => new()
    {
        { "null_arguments", null },
        { "omitted_token", new JsonObject() },
        { "null_token", new JsonObject { ["debugSessionId"] = null } },
        { "numeric_token", new JsonObject { ["debugSessionId"] = 17 } },
        { "boolean_token", new JsonObject { ["debugSessionId"] = true } },
        { "object_token", new JsonObject { ["debugSessionId"] = new JsonObject() } },
        { "array_token", new JsonObject { ["debugSessionId"] = new JsonArray() } },
        { "empty_token", new JsonObject { ["debugSessionId"] = string.Empty } },
        { "whitespace_token", new JsonObject { ["debugSessionId"] = " \t\r\n" } },
        { "null_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = null } },
        { "boolean_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = true } },
        { "string_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = "2" } },
        { "object_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = new JsonObject() } },
        { "array_lines", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = new JsonArray() } },
        { "positive_fraction", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("2.5") } },
        { "negative_fraction", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("-2.5") } },
        { "fractional_exponent", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("2e-1") } },
        { "underflowing_fraction", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("1e-1000") } },
        { "fraction_beyond_double_precision", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("1.0000000000000000000000000000000001") } },
        { "large_fraction_beyond_double_precision", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = JsonNode.Parse("9007199254740993.1") } },
        { "numeric_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = 17 } },
        { "boolean_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = false } },
        { "object_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = new JsonObject() } },
        { "array_category", new JsonObject { ["debugSessionId"] = "opaque", ["category"] = new JsonArray() } },
        { "forbidden_clear_true", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = true } },
        { "forbidden_clear_false", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = false } },
        { "forbidden_clear_null", new JsonObject { ["debugSessionId"] = "opaque", ["clear"] = null } },
        { "extra_field", new JsonObject { ["debugSessionId"] = "opaque", ["lines"] = 2, ["category"] = null, ["unexpected"] = true } },
        { "mis_cased_lines", new JsonObject { ["debugSessionId"] = "opaque", ["Lines"] = 2 } },
    };

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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(launch: MixedOutput()));
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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions());
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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions());
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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(Output(string.Join("\n", allLines) + "\n"))));
        var token = await StartSessionAsync(driver, "output-tail-default-start");
        var expected = string.Join("\n", allLines.Skip(5));

        await AssertOutputTailAsync(driver, token, "output-tail-default", 55, 50, expected);
        await AssertOutputTailAsync(driver, token, "output-tail-explicit-50", 55, 50, expected, "50");
    }

    [Theory]
    [InlineData("1", 1, "four")]
    [InlineData("2", 2, "three\nfour")]
    [InlineData("4", 4, "one\ntwo\nthree\nfour")]
    [InlineData("5", 4, "one\ntwo\nthree\nfour")]
    [InlineData("0", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-0", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-1", 3, "two\nthree\nfour")]
    [InlineData("-2", 2, "three\nfour")]
    [InlineData("-3", 1, "four")]
    [InlineData("-4", 0, "")]
    [InlineData("-5", 0, "")]
    [InlineData("2147483647", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-2147483648", 0, "")]
    [InlineData("9223372036854775807", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-9223372036854775808", 0, "")]
    [InlineData("9223372036854775808", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-9223372036854775809", 0, "")]
    [InlineData("1000000000000000000000000000000000000000000000000000000000000", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-1000000000000000000000000000000000000000000000000000000000000", 0, "")]
    [InlineData("2.0", 2, "three\nfour")]
    [InlineData("-2.000", 2, "three\nfour")]
    [InlineData("2e0", 2, "three\nfour")]
    [InlineData("20e-1", 2, "three\nfour")]
    [InlineData("0.2e1", 2, "three\nfour")]
    [InlineData("-20e-1", 2, "three\nfour")]
    [InlineData("2000e-3", 2, "three\nfour")]
    [InlineData("-0.0", 4, "one\ntwo\nthree\nfour")]
    [InlineData("0e-1000", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-0e1000", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-0e-1000", 4, "one\ntwo\nthree\nfour")]
    [InlineData("1e1000", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-1e1000", 0, "")]
    [InlineData("10000000000000000000000000000000000000000.000", 4, "one\ntwo\nthree\nfour")]
    [InlineData("-10000000000000000000000000000000000000000.000", 0, "")]
    public async Task GetOutputTail_SignedIntegralJsonLineCounts_PreservePythonSliceResults(string linesJson, int returnedLines, string expected)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
            launch: new JsonArray(Output("one\ntwo\nthree\nfour\n"))));
        var token = await StartSessionAsync(driver, "output-tail-signed-start");

        await AssertOutputTailAsync(driver, token, "output-tail-signed", 4, returnedLines, expected, linesJson);
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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(launch: new JsonArray(Output(text))));
        var token = await StartSessionAsync(driver, "output-tail-empty-lines-start");

        await AssertOutputTailAsync(driver, token, "output-tail-empty-lines-all", totalLines, totalLines, expectedAll, "0");
        await AssertOutputTailAsync(driver, token, "output-tail-empty-lines-last", totalLines, totalLines == 0 ? 0 : 1, expectedLast, "1");
    }

    [Fact]
    public async Task GetOutputTail_CrLfAcrossEvents_RecomputesLogicalLinesAfterLaterAppends()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
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

    [Theory]
    [InlineData("omitted", 8, "head\nerror\ntail\ndiscarded\nend\nconsole\nlast\nmetric", "metric")]
    [InlineData("null", 8, "head\nerror\ntail\ndiscarded\nend\nconsole\nlast\nmetric", "metric")]
    [InlineData("", 8, "head\nerror\ntail\ndiscarded\nend\nconsole\nlast\nmetric", "metric")]
    [InlineData("stdout", 2, "headtail\nend", "end")]
    [InlineData("stderr", 3, "\nerror\ndiscarded", "discarded")]
    [InlineData("console", 2, "console\nlast", "last")]
    [InlineData("telemetry", 2, "\nmetric", "metric")]
    [InlineData("STDOUT", 0, "", "")]
    [InlineData("Console", 0, "", "")]
    [InlineData("unknown", 0, "", "")]
    [InlineData(" ", 0, "", "")]
    public async Task GetOutputTail_CategoryFilter_PrecedesFragmentJoiningSplittingAndSlicing(
        string categoryShape, int totalLines, string expectedAll, string expectedLast)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(launch: new JsonArray(
            Output("head", "stdout"),
            Output("\nerror\n", "stderr"),
            Output("tail\r", "stdout"),
            Output("discarded", "stderr"),
            Output("\nend\n", "stdout"),
            Output("console"),
            Output("\vlast", "console"),
            Output("\nmetric\n", "telemetry"))));
        var token = await StartSessionAsync(driver, "output-tail-category-start");
        var category = categoryShape is "omitted" or "null" ? null : categoryShape;
        var includeCategory = categoryShape != "omitted";

        await AssertOutputTailAsync(driver, token, "output-tail-category-all", totalLines, totalLines, expectedAll, "0", category, includeCategory);
        await AssertOutputTailAsync(driver, token, "output-tail-category-last", totalLines, totalLines == 0 ? 0 : 1, expectedLast, "1", category, includeCategory);
    }

    [Theory]
    [InlineData("exited")]
    [InlineData("terminated")]
    public async Task GetOutputTail_TerminalSession_RemainsReadableWithoutConsumingOutput(string terminalEvent)
    {
        var batch = Batch(Output("final\n", "stderr"));
        batch["terminal"] = terminalEvent;
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
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

    [Theory]
    [MemberData(nameof(InvalidTailArguments))]
    public async Task GetOutputTail_InvalidInput_ReturnsCompleteInvalidArgumentsWithoutDapIo(string scenario, JsonObject? arguments)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions());
        var before = await driver.ReadNativeActionsAsync();
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            TailTool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId($"output-tail-invalid-{scenario}")), isError: true);

        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(TailTool, content["tool"]?.GetValue<string>());
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task GetOutputTail_OmittedArguments_ReturnsCompleteInvalidArgumentsWithoutDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions());
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
        await using var driver = await ModernMcpProcessDriver.StartAsync(OutputOptions(
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
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId));
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

    private static async Task AdvanceOutputAsync(ModernMcpProcessDriver driver, string token, string requestId)
    {
        // The fixture writes the entire batch before this response. Reader order is the barrier.
        var content = AssertApplicationEnvelope(await driver.CallToolRawAsync(
            "get_threads", new JsonObject { ["debugSessionId"] = token },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: false);
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
