using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class RemoveBreakpointContractTests
{
    private const string Tool = "remove_breakpoint";

    [Fact]
    public async Task ToolsList_RemoveBreakpointPublishesClosedSourceAndPositiveInt32Input()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("remove-catalog")));
        var tool = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(catalog["tools"]),
            static entry => entry?["name"]?.GetValue<string>() == Tool));
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId", "file", "line"], Assert.IsType<JsonArray>(schema["required"])
            .Select(static value => value?.GetValue<string>()).Order());
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
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }

    [Theory]
    [InlineData("success")]
    [InlineData("mixed")]
    public async Task RemoveBreakpoint_ExistingLine_RemovesAllInitialDuplicatesAndPreservesOrder(string responseMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions(responseMode));
        var relativeSource = Path.GetRelativePath(RepositoryLayout.Root, SourcePath);
        var session = await StartSessionAsync(driver, "remove-duplicates-start",
            (SourcePath, 2), (SourcePath, 6), (relativeSource, 2), (SourcePath, 8),
            (LexicalAlias(SourcePath), 2), (SourcePath, 10));
        AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver)), SourcePath, [2, 6, 2, 8, 2, 10]);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, session, LexicalAlias(SourcePath), 2, "remove-duplicates"), SourcePath, 2, true);
        var afterRemoval = (await driver.ReadNativeActionsAsync()).Count;
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 2, "remove-duplicates-again"), SourcePath, 2, false);
        Assert.Equal(afterRemoval, (await driver.ReadNativeActionsAsync()).Count);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 2, "remove-duplicates-readd"), SourcePath, 2);
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 8, "remove-duplicates-middle"), SourcePath, 8, true);

        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(3, writes.Length);
        AssertReplacement(writes[0], SourcePath, [6, 8, 10]);
        AssertReplacement(writes[1], SourcePath, [6, 8, 10, 2]);
        AssertReplacement(writes[2], SourcePath, [6, 10, 2]);
    }

    [Fact]
    public async Task RemoveBreakpoint_LastEntry_SendsEmptyReplacementThenAllowsReaddingAtInt32Boundary()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var session = await StartSessionAsync(driver, "remove-last-start", (SourcePath, int.MaxValue));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, int.MaxValue, "remove-last"), SourcePath, int.MaxValue, true);
        AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver, before)), SourcePath, []);
        var afterRemoval = (await driver.ReadNativeActionsAsync()).Count;
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, int.MaxValue, "remove-last-again"), SourcePath, int.MaxValue, false);
        Assert.Equal(afterRemoval, (await driver.ReadNativeActionsAsync()).Count);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, int.MaxValue, "remove-last-readd"), SourcePath, int.MaxValue);
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, int.MaxValue, "remove-last-after-readd"), SourcePath, int.MaxValue, true);

        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(3, writes.Length);
        AssertReplacement(writes[0], SourcePath, []);
        AssertReplacement(writes[1], SourcePath, [int.MaxValue]);
        AssertReplacement(writes[2], SourcePath, []);
    }

    [Fact]
    public async Task RemoveBreakpoint_AbsentLine_ReturnsFalseWithoutDapWriteAndPreservesRequestedSet()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var session = await StartSessionAsync(driver, "remove-absent-line-start", (SourcePath, 2), (SourcePath, 6));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, session, LexicalAlias(SourcePath), 4, "remove-absent-line"), SourcePath, 4, false);
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 8, "remove-absent-line-add"), SourcePath, 8);
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 6, "remove-existing-after-absent-line"), SourcePath, 6, true);

        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(2, writes.Length);
        AssertReplacement(writes[0], SourcePath, [2, 6, 8]);
        AssertReplacement(writes[1], SourcePath, [2, 8]);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task RemoveBreakpoint_AbsentFile_ReturnsFalseWithoutDapWrite(bool fileExists)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var session = await StartSessionAsync(driver, "remove-absent-file-start", (SourcePath, 2));
        var file = fileExists ? OtherSourcePath : driver.InertProgramPath + ".missing.cs";
        Assert.Equal(fileExists, File.Exists(file));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, session, LexicalAlias(file), 2, "remove-absent-file"), file, 2, false);
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 6, "remove-absent-file-add"), SourcePath, 6);
        AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver, before)), SourcePath, [2, 6]);
    }

    [Fact]
    public async Task RemoveBreakpoint_KnownSourceDeletedAfterLaunch_RemovesWithoutRequiringFileExistence()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var source = driver.InertProgramPath + ".source.cs";
        await File.WriteAllTextAsync(source, "class RemovedSource\n{\n    static void Main() { }\n}\n");
        var session = await StartSessionAsync(driver, "remove-deleted-source-start", (source, 2), (source, 6), (source, 2));
        File.Delete(source);
        Assert.False(File.Exists(source));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, session, LexicalAlias(source), 2, "remove-deleted-source"), source, 2, true);
        AssertRemoved(await CallAsync(driver, Tool, session, source, 6, "remove-deleted-source-last"), source, 6, true);
        var afterRemoval = (await driver.ReadNativeActionsAsync()).Count;
        AssertRemoved(await CallAsync(driver, Tool, session, source, 6, "remove-deleted-source-again"), source, 6, false);
        Assert.Equal(afterRemoval, (await driver.ReadNativeActionsAsync()).Count);

        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(2, writes.Length);
        AssertReplacement(writes[0], source, [6]);
        AssertReplacement(writes[1], source, []);
    }

    [Fact]
    public async Task RemoveBreakpoint_AdapterRelocatedLine_DoesNotMatchRequestedLine()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions("mixed"));
        var session = await StartSessionAsync(driver, "remove-relocated-start", (SourcePath, 2), (SourcePath, 6));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 7, "remove-relocated-actual-line"), SourcePath, 7, false);
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 8, "remove-relocated-add"), SourcePath, 8);
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 6, "remove-relocated-requested-line"), SourcePath, 6, true);

        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(2, writes.Length);
        AssertReplacement(writes[0], SourcePath, [2, 6, 8]);
        AssertReplacement(writes[1], SourcePath, [2, 8]);
    }

    [Fact]
    public async Task RemoveBreakpoint_SeparateFiles_DoNotShareRequestedConfiguration()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var session = await StartSessionAsync(driver, "remove-files-start",
            (SourcePath, 2), (OtherSourcePath, 2), (SourcePath, 6), (OtherSourcePath, 8));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 2, "remove-files-first"), SourcePath, 2, true);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, OtherSourcePath, 10, "remove-files-add-second"), OtherSourcePath, 10);
        AssertRemoved(await CallAsync(driver, Tool, session, OtherSourcePath, 2, "remove-files-second"), OtherSourcePath, 2, true);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 12, "remove-files-add-first"), SourcePath, 12);

        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(4, writes.Length);
        AssertReplacement(writes[0], SourcePath, [6]);
        AssertReplacement(writes[1], OtherSourcePath, [2, 8, 10]);
        AssertReplacement(writes[2], OtherSourcePath, [8, 10]);
        AssertReplacement(writes[3], SourcePath, [6, 12]);
    }

    [Fact]
    public async Task RemoveBreakpoint_SeparateSessions_DoNotShareRequestedConfiguration()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var first = await StartSessionAsync(driver, "remove-sessions-first-start", (SourcePath, 2), (SourcePath, 6));
        var second = await StartSessionAsync(driver, "remove-sessions-second-start", (SourcePath, 2), (SourcePath, 8));
        Assert.NotEqual(first, second);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, first, SourcePath, 2, "remove-sessions-first"), SourcePath, 2, true);
        AssertAdded(await CallAsync(driver, "add_breakpoint", second, SourcePath, 10, "remove-sessions-add-second"), SourcePath, 10);
        AssertRemoved(await CallAsync(driver, Tool, second, SourcePath, 8, "remove-sessions-second"), SourcePath, 8, true);
        AssertAdded(await CallAsync(driver, "add_breakpoint", first, SourcePath, 12, "remove-sessions-add-first"), SourcePath, 12);

        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(4, writes.Length);
        AssertReplacement(writes[0], SourcePath, [6]);
        AssertReplacement(writes[1], SourcePath, [2, 8, 10]);
        AssertReplacement(writes[2], SourcePath, [2, 10]);
        AssertReplacement(writes[3], SourcePath, [6, 12]);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task RemoveBreakpoint_RunningOrStoppedSession_UsesSameReplacementBehavior(bool stopped)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: stopped ? "all-stop" : "all-stop-continued-true")));
        var session = await StartSessionAsync(driver, "remove-lifecycle-start", (SourcePath, 2), (SourcePath, 6));
        var expectedEvent = stopped ? "stopped" : "continued";
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureEventAsync(expectedEvent, observation.Token);
        await ReaderBarrierAsync(driver, session, "remove-lifecycle-reader-barrier", observation.Token);
        var state = await ReadStateAsync(driver, session, "remove-lifecycle-before");
        Assert.Equal(expectedEvent, state["state"]?["event"]?.GetValue<string>());
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 2, "remove-lifecycle"), SourcePath, 2, true);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 8, "remove-lifecycle-add"), SourcePath, 8);
        var laterState = await ReadStateAsync(driver, session, "remove-lifecycle-after");
        Assert.Equal(expectedEvent, laterState["state"]?["event"]?.GetValue<string>());
        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(2, writes.Length);
        AssertReplacement(writes[0], SourcePath, [6]);
        AssertReplacement(writes[1], SourcePath, [6, 8]);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task RemoveBreakpoint_RefusedReplacement_RetainsRequestedStateForLaterAddAndRemove(bool lastEntry)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions("refuse-first-runtime"));
        (string File, int Line)[] seeds = lastEntry
            ? [(SourcePath, 2)]
            : [(SourcePath, 2), (SourcePath, 6), (SourcePath, 2), (SourcePath, 8)];
        var session = await StartSessionAsync(driver, "remove-refused-start", seeds);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var refused = AssertEnvelope(await CallAsync(driver, Tool, session, SourcePath, 2, "remove-refused"), isError: true);
        AssertError(refused, "dap_set_breakpoints_refused", "DAP_SET_BREAKPOINTS_REFUSED");
        _ = await ReadStateAsync(driver, session, "remove-refused-retained-state");
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 10, "remove-refused-add"), SourcePath, 10);
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 2, "remove-after-refused"), SourcePath, 2, true);
        var afterRemoval = (await driver.ReadNativeActionsAsync()).Count;
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 2, "remove-after-refused-again"), SourcePath, 2, false);
        Assert.Equal(afterRemoval, (await driver.ReadNativeActionsAsync()).Count);

        var writes = await ReadReplacementsAsync(driver, before);
        Assert.Equal(3, writes.Length);
        AssertReplacement(writes[0], SourcePath, lastEntry ? [] : [6, 8]);
        AssertReplacement(writes[1], SourcePath, lastEntry ? [2, 10] : [2, 6, 2, 8, 10]);
        AssertReplacement(writes[2], SourcePath, lastEntry ? [10] : [6, 8, 10]);
    }

    [Theory]
    [InlineData("missing-verified-first-runtime")]
    [InlineData("too-few-results-first-runtime")]
    [InlineData("reader-failure-first-runtime")]
    public async Task RemoveBreakpoint_MalformedOrUncertainResponse_ReturnsRedactedProtocolErrorAndDrains(string responseMode)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions(responseMode));
        var session = await StartSessionAsync(driver, "remove-malformed-start",
            (SourcePath, 2), (SourcePath, 6), (SourcePath, 2), (SourcePath, 8));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var failed = AssertEnvelope(await CallAsync(driver, Tool, session, SourcePath, 2, "remove-malformed"), isError: true);
        AssertError(failed, "dap_set_breakpoints_protocol_error", "DAP_SET_BREAKPOINTS_PROTOCOL_ERROR");
        AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver, before)), SourcePath, [6, 8]);
        await AssertSessionNotFoundAsync(driver, session, "remove-malformed-state");
        var afterDrain = (await driver.ReadNativeActionsAsync()).Count;
        AssertNotFound(AssertEnvelope(await CallAsync(driver, Tool, session, SourcePath, 6, "remove-malformed-reuse"), isError: true));
        AssertNotFound(AssertEnvelope(await CallAsync(driver, "add_breakpoint", session, SourcePath, 10, "remove-malformed-add"), isError: true));
        Assert.Equal(afterDrain, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Fact]
    public async Task RemoveBreakpoint_PostWriteCallerCancellation_DrainsBeforeReuse()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions("hold-first-runtime"));
        var session = await StartSessionAsync(driver, "remove-cancel-start", (SourcePath, 2), (SourcePath, 6));
        var before = (await driver.ReadNativeActionsAsync()).Count;
        using var cancellation = new CancellationTokenSource();
        var operation = driver.CallToolRawAsync(Tool, Arguments(session, SourcePath, 2),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("remove-cancel"), cancellation.Token, timeout: TimeSpan.FromSeconds(10));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("runtime-breakpoints-response-gated", observation.Token);
            await ReaderBarrierAsync(driver, session, "remove-cancel-reader-barrier", observation.Token);
            Assert.False(operation.IsCompleted);
            AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver, before)), SourcePath, [6]);
            cancellation.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
            await driver.WaitForFixtureRecordAsync("disconnect-response", observation.Token);
            await AssertSessionNotFoundAsync(driver, session, "remove-cancel-state");

            var afterDrain = (await driver.ReadNativeActionsAsync()).Count;
            AssertNotFound(AssertEnvelope(await CallAsync(driver, Tool, session, SourcePath, 6, "remove-cancel-reuse"), isError: true));
            AssertNotFound(AssertEnvelope(await CallAsync(driver, "add_breakpoint", session, SourcePath, 8, "remove-cancel-add"), isError: true));
            Assert.Equal(afterDrain, (await driver.ReadNativeActionsAsync()).Count);
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task RemoveBreakpoint_ConcurrentAddAndRemove_SerializeAcknowledgedReplacementWithoutLostUpdates(bool removeFirst)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions("hold-first-runtime"));
        var session = await StartSessionAsync(driver, "remove-concurrent-start",
            (SourcePath, 2), (SourcePath, 6), (SourcePath, 2), (SourcePath, 8));
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var first = driver.CallToolRawAsync(removeFirst ? Tool : "add_breakpoint",
            Arguments(session, SourcePath, removeFirst ? 2 : 10), ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("remove-concurrent-first"), timeout: TimeSpan.FromSeconds(10));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("runtime-breakpoints-response-gated", observation.Token);
            var second = driver.CallToolRawAsync(removeFirst ? "add_breakpoint" : Tool,
                Arguments(session, SourcePath, removeFirst ? 10 : 2), ModernMcpProcessDriver.CurrentMeta(),
                new RequestId("remove-concurrent-second"), timeout: TimeSpan.FromSeconds(10));
            await ReaderBarrierAsync(driver, session, "remove-concurrent-reader-barrier", observation.Token);
            Assert.False(first.IsCompleted);
            Assert.False(second.IsCompleted);
            AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver, before)), SourcePath,
                removeFirst ? [6, 8] : [2, 6, 2, 8, 10]);

            driver.ReleaseNextResponse();
            var results = await Task.WhenAll(first, second).WaitAsync(observation.Token);
            AssertRemoved(results[removeFirst ? 0 : 1], SourcePath, 2, true);
            AssertAdded(results[removeFirst ? 1 : 0], SourcePath, 10);
            var concurrentWrites = await ReadReplacementsAsync(driver, before);
            Assert.Equal(2, concurrentWrites.Length);
            AssertReplacement(concurrentWrites[0], SourcePath, removeFirst ? [6, 8] : [2, 6, 2, 8, 10]);
            AssertReplacement(concurrentWrites[1], SourcePath, [6, 8, 10]);

            AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 10, "remove-concurrent-later-remove"), SourcePath, 10, true);
            AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 12, "remove-concurrent-later-add"), SourcePath, 12);
            var writes = await ReadReplacementsAsync(driver, before);
            Assert.Equal(4, writes.Length);
            AssertReplacement(writes[2], SourcePath, [6, 8]);
            AssertReplacement(writes[3], SourcePath, [6, 8, 12]);
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task RemoveBreakpoint_InvalidArguments_RejectsClosedInputBeforeDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var session = await StartSessionAsync(driver, "remove-invalid-start", (SourcePath, 2));
        var invalid = new List<JsonObject?> { null };
        foreach (var field in new[] { "debugSessionId", "file", "line" })
        {
            var arguments = Arguments(session, SourcePath, 2);
            arguments.Remove(field);
            invalid.Add(arguments);
        }
        foreach (var field in new[] { "debugSessionId", "file" })
        {
            foreach (var value in new JsonNode?[] { JsonValue.Create(""), JsonValue.Create(" \t "), JsonValue.Create(1), JsonValue.Create(true), new JsonArray(), new JsonObject(), null })
            {
                var arguments = Arguments(session, SourcePath, 2);
                arguments[field] = value?.DeepClone();
                invalid.Add(arguments);
            }
        }
        foreach (var value in new JsonNode?[] { JsonValue.Create(0), JsonValue.Create(-1), JsonValue.Create(1.5), JsonValue.Create(2_147_483_648L), JsonValue.Create("2"), JsonValue.Create(true), new JsonArray(), new JsonObject(), null })
        {
            var arguments = Arguments(session, SourcePath, 2);
            arguments["line"] = value?.DeepClone();
            invalid.Add(arguments);
        }
        foreach (var field in new[] { "condition", "hitCondition", "unexpected" })
        {
            var arguments = Arguments(session, SourcePath, 2);
            arguments[field] = "true";
            invalid.Add(arguments);
        }
        var before = (await driver.ReadNativeActionsAsync()).Count;

        for (var index = 0; index < invalid.Count; index++)
        {
            var content = AssertEnvelope(await driver.CallToolRawAsync(Tool, invalid[index],
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"remove-invalid-{index}")), isError: true);
            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
            Assert.Equal(Tool, content["tool"]?.GetValue<string>());
            Assert.Equal(3, content.Count);
        }
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 6, "remove-invalid-retained-add"), SourcePath, 6);
        AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver, before)), SourcePath, [2, 6]);
    }

    [Fact]
    public async Task RemoveBreakpoint_InvalidLexicalSource_ReturnsSourceUnavailableWithoutDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var session = await StartSessionAsync(driver, "remove-invalid-source-start", (SourcePath, 2));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var content = AssertEnvelope(await CallAsync(driver, Tool, session, "invalid\0source.cs", 2, "remove-invalid-source"), isError: true);
        AssertError(content, "breakpoint_source_unavailable", "BREAKPOINT_SOURCE_UNAVAILABLE");
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        AssertRemoved(await CallAsync(driver, Tool, session, SourcePath, 2, "remove-valid-after-invalid-source"), SourcePath, 2, true);
        AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver, before)), SourcePath, []);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task RemoveBreakpoint_UnknownOrRetiredSession_ReturnsNotFoundWithoutDapIo(bool retired)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(LaunchOptions());
        var session = await StartSessionAsync(driver, "remove-retired-start", (SourcePath, 2));
        if (retired)
        {
            var stopped = AssertEnvelope(await driver.CallToolRawAsync("stop_debug", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("remove-retire")), isError: false);
            Assert.Equal("stop_debug_success", stopped["kind"]?.GetValue<string>());
        }
        var token = retired ? session : "unknown-opaque-remove-token";
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertNotFound(AssertEnvelope(await CallAsync(driver, Tool, token, SourcePath, 2, "remove-retired-existing"), isError: true));
        AssertNotFound(AssertEnvelope(await CallAsync(driver, Tool, token, driver.InertProgramPath + ".missing.cs", 6, "remove-retired-absent"), isError: true));
        AssertNotFound(AssertEnvelope(await CallAsync(driver, "add_breakpoint", token, SourcePath, 8, "remove-retired-add"), isError: true));
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("all-stop-exited", "exited")]
    [InlineData("all-stop-terminated", "terminated")]
    public async Task RemoveBreakpoint_ObservedTerminalSession_MatchesAddRefusalWithoutDapIo(string lifecycleMode, string terminal)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(LifecycleMode: lifecycleMode)));
        var session = await StartSessionAsync(driver, "remove-terminal-start", (SourcePath, 2), (SourcePath, 6));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureEventAsync(terminal, observation.Token);
        await ReaderBarrierAsync(driver, session, "remove-terminal-reader-barrier", observation.Token);
        var state = await ReadStateAsync(driver, session, "remove-terminal-state");
        Assert.Equal(terminal, state["state"]?["event"]?.GetValue<string>());
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var add = AssertEnvelope(await CallAsync(driver, "add_breakpoint", session, SourcePath, 8, "remove-terminal-add-comparison"), isError: true);
        Assert.NotEqual("breakpoint_added", add["kind"]?.GetValue<string>());
        foreach (var line in new[] { 2, 42 })
        {
            var remove = AssertEnvelope(await CallAsync(driver, Tool, session, SourcePath, line, $"remove-terminal-{line}"), isError: true);
            Assert.Equal(add.ToJsonString(), remove.ToJsonString());
        }
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("omitted")]
    [InlineData("null")]
    [InlineData("empty")]
    [InlineData("ordered")]
    public async Task ConfigureExceptions_OptionalAndOrderedFilters_ForwardsExactlyOneReplacement(string shape)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions(), startupTimeout: TimeSpan.FromSeconds(10));
        var session = await StartSessionAsync(driver, "filters-shape-start");
        string[] filters = shape == "ordered" ? ["user-unhandled", "adapter-specific", "all", "all", ""] : [];
        var arguments = ExceptionFiltersArguments(session, filters);
        if (shape == "omitted")
        {
            arguments.Remove("filters");
        }
        else if (shape == "null")
        {
            arguments["filters"] = null;
        }
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertExceptionFiltersAcknowledged(await driver.CallToolRawAsync("configure_exceptions", arguments,
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"filters-shape-{shape}")), filters, verified: null);

        AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), filters);
        var state = await ReadStateAsync(driver, session, "filters-shape-state");
        Assert.NotEqual("stopped", state["state"]?["event"]?.GetValue<string>());
    }

    [Theory]
    [InlineData("absent-body", false)]
    [InlineData("missing-breakpoints", false)]
    [InlineData("verified", true)]
    public async Task ConfigureExceptions_OptionalVerification_ReportsOnlyAdapterBooleansOrNull(string mode, bool hasVerification)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions(mode));
        var session = await StartSessionAsync(driver, "filters-verification-start");
        string[] filters = ["all", "user-unhandled", "all"];
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertExceptionFiltersAcknowledged(await CallExceptionFiltersAsync(driver, session, filters, "filters-verification"),
            filters, hasVerification ? [true, false, true] : null);

        AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), filters);
        _ = await ReadStateAsync(driver, session, "filters-verification-retained");
    }

    [Theory]
    [InlineData("omit", "none", false)]
    [InlineData("empty", "none", false)]
    [InlineData("supported", "none", true)]
    [InlineData("empty", "enable-before-initialize", false)]
    [InlineData("supported", "disable-before-initialize", true)]
    [InlineData("empty", "enable-after-initialize", true)]
    [InlineData("supported", "disable-after-initialize", false)]
    public async Task ConfigureExceptions_InitializeBaselineAndOrderedDelta_DefineAdmission(string baseline, string delta, bool supported)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions(baseline: baseline, delta: delta));
        var session = await StartSessionAsync(driver, "filters-baseline-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await ReaderBarrierAsync(driver, session, "filters-baseline-reader", observation.Token);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var response = await CallExceptionFiltersAsync(driver, session, ["all"], "filters-baseline");
        if (supported)
        {
            AssertExceptionFiltersAcknowledged(response, ["all"], verified: null);
            AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), ["all"]);
        }
        else
        {
            AssertError(AssertEnvelope(response, isError: true), "dap_exception_filters_unsupported", "DAP_EXCEPTION_FILTERS_UNSUPPORTED");
            Assert.Empty(await ReadExceptionFilterWritesAsync(driver, before));
        }
        _ = await ReadStateAsync(driver, session, "filters-baseline-retained");
    }

    [Theory]
    [InlineData("empty", "enable-on-threads", true)]
    [InlineData("supported", "disable-on-threads", false)]
    [InlineData("supported", "unrelated-on-threads", true)]
    public async Task ConfigureExceptions_LiveCapabilityDelta_IsAppliedBeforeFollowingReaderBarrier(string baseline, string delta, bool supported)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions(baseline: baseline, delta: delta));
        var session = await StartSessionAsync(driver, "filters-delta-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await ReaderBarrierAsync(driver, session, "filters-delta-reader", observation.Token);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var response = await CallExceptionFiltersAsync(driver, session, ["all"], "filters-delta");
        if (supported)
        {
            AssertExceptionFiltersAcknowledged(response, ["all"], verified: null);
            AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), ["all"]);
        }
        else
        {
            AssertError(AssertEnvelope(response, isError: true), "dap_exception_filters_unsupported", "DAP_EXCEPTION_FILTERS_UNSUPPORTED");
            Assert.Empty(await ReadExceptionFilterWritesAsync(driver, before));
        }
    }

    [Fact]
    public async Task ConfigureExceptions_InvalidArguments_RejectsBeforeDapIoAndKeepsSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions());
        var session = await StartSessionAsync(driver, "filters-invalid-start");
        var invalid = new List<JsonObject?> { null, new() };
        foreach (var value in new JsonNode?[] { null, JsonValue.Create(""), JsonValue.Create(" \t "), JsonValue.Create(17), new JsonArray(), new JsonObject() })
        {
            var arguments = ExceptionFiltersArguments(session, []);
            arguments["debugSessionId"] = value?.DeepClone();
            invalid.Add(arguments);
        }
        foreach (var value in new JsonNode[] { JsonValue.Create("all")!, JsonValue.Create(17)!, JsonValue.Create(true)!, new JsonObject(), new JsonArray(17), new JsonArray((JsonNode?)null), new JsonArray("all", false) })
        {
            var arguments = ExceptionFiltersArguments(session, []);
            arguments["filters"] = value;
            invalid.Add(arguments);
        }
        foreach (var field in new[] { "filterOptions", "exceptionOptions", "unexpected" })
        {
            var arguments = ExceptionFiltersArguments(session, []);
            arguments[field] = new JsonArray();
            invalid.Add(arguments);
        }
        invalid.Add(ExceptionFiltersArguments(session, [new string('x', 256 * 1024)]));
        var before = (await driver.ReadNativeActionsAsync()).Count;

        for (var index = 0; index < invalid.Count; index++)
        {
            var content = AssertEnvelope(await driver.CallToolRawAsync("configure_exceptions", invalid[index],
                ModernMcpProcessDriver.CurrentMeta(), new RequestId($"filters-invalid-{index}")), isError: true);
            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
            Assert.Equal("configure_exceptions", content["tool"]?.GetValue<string>());
            Assert.Equal(3, content.Count);
        }
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
        AssertExceptionFiltersAcknowledged(await CallExceptionFiltersAsync(driver, session, [], "filters-valid-after-invalid"), [], verified: null);
    }

    [Fact]
    public async Task ConfigureExceptions_NonObjectArguments_ReturnInvalidArgumentsBeforeDapIo()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions());
        var before = await driver.ReadNativeActionsAsync();
        var shapes = new[] { "null", "17", "true", "[]", "\"not-an-object\"" };
        for (var index = 0; index < shapes.Length; index++)
        {
            var response = await driver.SendRawRequestAsync("tools/call", new JsonObject
            {
                ["name"] = "configure_exceptions",
                ["arguments"] = JsonNode.Parse(shapes[index]),
                ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
            }, new RequestId($"filters-invalid-shape-{index}"));
            var content = AssertEnvelope(response, isError: true);
            Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
            Assert.Equal("configure_exceptions", content["tool"]?.GetValue<string>());
        }
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("refused", false)]
    [InlineData("null-breakpoints", true)]
    [InlineData("non-object-body", true)]
    [InlineData("non-array-breakpoints", true)]
    [InlineData("missing-verified", true)]
    [InlineData("non-boolean-verified", true)]
    [InlineData("null-entry", true)]
    [InlineData("too-few-results", true)]
    [InlineData("too-many-results", true)]
    [InlineData("wrong-command", true)]
    [InlineData("reader-failure", true)]
    [InlineData("eof", true)]
    [InlineData("timeout", true)]
    public async Task ConfigureExceptions_RefusedOrMalformedResponse_RedactsWithoutRetryAndPreservesOrDrains(string mode, bool protocolError)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions(mode));
        var session = await StartSessionAsync(driver, "filters-failure-start");
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var response = await driver.CallToolRawAsync("configure_exceptions", ExceptionFiltersArguments(session, ["all", "user-unhandled"]),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("filters-failure"), timeout: TimeSpan.FromSeconds(mode == "timeout" ? 45 : 10));
        AssertError(AssertEnvelope(response, isError: true),
            protocolError ? "dap_exception_filters_protocol_error" : "dap_exception_filters_refused",
            protocolError ? "DAP_EXCEPTION_FILTERS_PROTOCOL_ERROR" : "DAP_EXCEPTION_FILTERS_REFUSED");
        AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), ["all", "user-unhandled"]);
        if (protocolError)
        {
            await AssertSessionNotFoundAsync(driver, session, "filters-failure-retired");
            var afterDrain = (await driver.ReadNativeActionsAsync()).Count;
            AssertNotFound(AssertEnvelope(await CallExceptionFiltersAsync(driver, session, [], "filters-failure-reuse"), isError: true));
            Assert.Equal(afterDrain, (await driver.ReadNativeActionsAsync()).Count);
        }
        else
        {
            _ = await ReadStateAsync(driver, session, "filters-refusal-retained");
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task ConfigureExceptions_RunningOrStopped_DoesNotChangeObservedStateOrSourceBreakpoints(bool stopped)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(ExceptionBreakpointFilters: "supported", LifecycleMode: stopped ? "all-stop" : "all-stop-continued-true")));
        var session = await StartSessionAsync(driver, "filters-state-start", (SourcePath, 2));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureEventAsync(stopped ? "stopped" : "continued", observation.Token);
        await ReaderBarrierAsync(driver, session, "filters-state-reader", observation.Token);
        var beforeState = await ReadStateAsync(driver, session, "filters-state-before");
        var before = (await driver.ReadNativeActionsAsync()).Count;

        AssertExceptionFiltersAcknowledged(await CallExceptionFiltersAsync(driver, session, ["all"], "filters-state"), ["all"], verified: null);
        var afterState = await ReadStateAsync(driver, session, "filters-state-after");
        Assert.Equal(beforeState["state"]!.ToJsonString(), afterState["state"]!.ToJsonString());
        AssertAdded(await CallAsync(driver, "add_breakpoint", session, SourcePath, 6, "filters-state-add"), SourcePath, 6);
        AssertReplacement(Assert.Single(await ReadReplacementsAsync(driver, before)), SourcePath, [2, 6]);
        AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), ["all"]);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task ConfigureExceptions_UnknownOrRetiredSession_ReturnsNotFoundWithoutDapIo(bool retired)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions());
        var session = await StartSessionAsync(driver, "filters-retired-start");
        if (retired)
        {
            _ = AssertEnvelope(await driver.CallToolRawAsync("stop_debug", new JsonObject { ["debugSessionId"] = session },
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("filters-retire")), isError: false);
        }
        var before = (await driver.ReadNativeActionsAsync()).Count;
        AssertNotFound(AssertEnvelope(await CallExceptionFiltersAsync(driver, retired ? session : "unknown-opaque-filters-session", [], "filters-not-found"), isError: true));
        Assert.Equal(before, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [InlineData("all-stop-exited", "exited")]
    [InlineData("all-stop-terminated", "terminated")]
    public async Task ConfigureExceptions_TerminalAdmission_DoesNotWriteOrReopenSession(string lifecycle, string terminal)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(ExceptionBreakpointFilters: "supported", LifecycleMode: lifecycle)));
        var session = await StartSessionAsync(driver, "filters-terminal-start");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureEventAsync(terminal, observation.Token);
        await ReaderBarrierAsync(driver, session, "filters-terminal-reader", observation.Token);
        var before = (await driver.ReadNativeActionsAsync()).Count;

        var rejected = AssertEnvelope(await CallExceptionFiltersAsync(driver, session, ["all"], "filters-terminal"), isError: true);
        Assert.NotEqual("exception_filters_acknowledged", rejected["kind"]?.GetValue<string>());
        _ = AssertEnvelope(await CallExceptionFiltersAsync(driver, session, [], "filters-terminal-again"), isError: true);
        Assert.Empty(await ReadExceptionFilterWritesAsync(driver, before));
    }

    [Fact]
    public async Task ConfigureExceptions_CompetingUpdates_WaitsForFirstAcknowledgementBeforeSecondWrite()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions("hold-first"));
        var session = await StartSessionAsync(driver, "filters-concurrent-start");
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var first = CallExceptionFiltersAsync(driver, session, ["all"], "filters-concurrent-first");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("exception-filters-response-gated", observation.Token);
            var second = CallExceptionFiltersAsync(driver, session, ["user-unhandled", "all", "all"], "filters-concurrent-second");
            await ReaderBarrierAsync(driver, session, "filters-concurrent-reader", observation.Token);
            Assert.False(first.IsCompleted);
            Assert.False(second.IsCompleted);
            AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), ["all"]);
            driver.ReleaseNextResponse();
            var results = await Task.WhenAll(first, second).WaitAsync(observation.Token);
            AssertExceptionFiltersAcknowledged(results[0], ["all"], [true]);
            AssertExceptionFiltersAcknowledged(results[1], ["user-unhandled", "all", "all"], [true, false, true]);
            var writes = await ReadExceptionFilterWritesAsync(driver, before);
            Assert.Equal(2, writes.Length);
            AssertExceptionFiltersWrite(writes[0], ["all"]);
            AssertExceptionFiltersWrite(writes[1], ["user-unhandled", "all", "all"]);
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task ConfigureExceptions_ReaderOrderedRevocationDuringUpdate_RefusesNextWrite()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions("hold-first", delta: "disable-on-threads"));
        var session = await StartSessionAsync(driver, "filters-revoke-start");
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var first = CallExceptionFiltersAsync(driver, session, ["all"], "filters-revoke-first");
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("exception-filters-response-gated", observation.Token);
            await ReaderBarrierAsync(driver, session, "filters-revoke-reader", observation.Token);
            var second = CallExceptionFiltersAsync(driver, session, ["user-unhandled"], "filters-revoke-second");
            driver.ReleaseNextResponse();
            AssertExceptionFiltersAcknowledged(await first.WaitAsync(observation.Token), ["all"], [true]);
            AssertError(AssertEnvelope(await second.WaitAsync(observation.Token), isError: true),
                "dap_exception_filters_unsupported", "DAP_EXCEPTION_FILTERS_UNSUPPORTED");
            AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), ["all"]);
            _ = await ReadStateAsync(driver, session, "filters-revoke-retained");
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task ConfigureExceptions_QueuedCallerCancellation_EmitsNoSecondWriteAndKeepsSession()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions("hold-first"));
        var session = await StartSessionAsync(driver, "filters-queued-cancel-start");
        var before = (await driver.ReadNativeActionsAsync()).Count;
        var first = CallExceptionFiltersAsync(driver, session, ["all"], "filters-queued-first");
        using var cancellation = new CancellationTokenSource();
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("exception-filters-response-gated", observation.Token);
            var queued = driver.CallToolRawAsync("configure_exceptions", ExceptionFiltersArguments(session, ["user-unhandled"]),
                ModernMcpProcessDriver.CurrentMeta(), new RequestId("filters-queued-cancel"), cancellation.Token, timeout: TimeSpan.FromSeconds(10));
            await ReaderBarrierAsync(driver, session, "filters-queued-reader", observation.Token);
            Assert.False(queued.IsCompleted);
            cancellation.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => queued);
            driver.ReleaseNextResponse();
            AssertExceptionFiltersAcknowledged(await first.WaitAsync(observation.Token), ["all"], [true]);
            await ReaderBarrierAsync(driver, session, "filters-queued-after-reader", observation.Token);
            AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), ["all"]);
            AssertExceptionFiltersAcknowledged(await CallExceptionFiltersAsync(driver, session, [], "filters-queued-later"), [], []);
            Assert.Equal(2, (await ReadExceptionFilterWritesAsync(driver, before)).Length);
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    [Fact]
    public async Task ConfigureExceptions_PostWriteCancellation_DrainsWithoutRetryBeforeReuse()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(ExceptionFiltersOptions("hold-first"));
        var session = await StartSessionAsync(driver, "filters-postwrite-start");
        var before = (await driver.ReadNativeActionsAsync()).Count;
        using var cancellation = new CancellationTokenSource();
        var operation = driver.CallToolRawAsync("configure_exceptions", ExceptionFiltersArguments(session, ["all"]),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId("filters-postwrite-cancel"), cancellation.Token, timeout: TimeSpan.FromSeconds(10));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        try
        {
            await driver.WaitForFixtureRecordAsync("exception-filters-response-gated", observation.Token);
            await ReaderBarrierAsync(driver, session, "filters-postwrite-reader", observation.Token);
            AssertExceptionFiltersWrite(Assert.Single(await ReadExceptionFilterWritesAsync(driver, before)), ["all"]);
            cancellation.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
            await driver.WaitForFixtureRecordAsync("disconnect-response", observation.Token);
            await AssertSessionNotFoundAsync(driver, session, "filters-postwrite-retired");
            driver.ReleaseNextResponse();
            var afterDrain = (await driver.ReadNativeActionsAsync()).Count;
            AssertNotFound(AssertEnvelope(await CallExceptionFiltersAsync(driver, session, [], "filters-postwrite-reuse"), isError: true));
            Assert.Equal(afterDrain, (await driver.ReadNativeActionsAsync()).Count);
        }
        finally
        {
            driver.ReleaseNextResponse();
        }
    }

    private static ModernMcpStartOptions ExceptionFiltersOptions(string responseMode = "absent-body", string baseline = "supported", string delta = "none") => new(
        FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, ExceptionBreakpointFilters: baseline,
            ExceptionFiltersCapabilityDelta: delta, SetExceptionBreakpointsResponseMode: responseMode));

    private static JsonObject ExceptionFiltersArguments(string session, string[] filters) => new()
    {
        ["debugSessionId"] = session,
        ["filters"] = new JsonArray(filters.Select(static filter => (JsonNode)JsonValue.Create(filter)!).ToArray()),
    };

    private static Task<JsonRpcResponse> CallExceptionFiltersAsync(ModernMcpProcessDriver driver, string session, string[] filters, string requestId) =>
        driver.CallToolRawAsync("configure_exceptions", ExceptionFiltersArguments(session, filters),
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId), timeout: TimeSpan.FromSeconds(10));

    private static void AssertExceptionFiltersAcknowledged(JsonRpcResponse response, string[] filters, bool[]? verified)
    {
        var content = AssertEnvelope(response, isError: false);
        Assert.Equal("exception_filters_acknowledged", content["kind"]?.GetValue<string>());
        Assert.Equal(filters, Assert.IsType<JsonArray>(content["filters"]).Select(static entry => entry!.GetValue<string>()));
        Assert.Equal(3, content.Count);
        Assert.True(content.ContainsKey("breakpoints"));
        if (verified is null)
        {
            Assert.Null(content["breakpoints"]);
        }
        else
        {
            var breakpoints = Assert.IsType<JsonArray>(content["breakpoints"]);
            Assert.Equal(verified, breakpoints.Select(static entry => Assert.IsType<JsonObject>(entry)["verified"]!.GetValue<bool>()));
            Assert.All(breakpoints, static entry => Assert.Single(Assert.IsType<JsonObject>(entry)));
        }
    }

    private static async Task<ModernNativeAction[]> ReadExceptionFilterWritesAsync(ModernMcpProcessDriver driver, int after = 0) =>
        (await driver.ReadNativeActionsAsync()).Skip(after).Where(static action => action.Command == "setExceptionBreakpoints").ToArray();

    private static void AssertExceptionFiltersWrite(ModernNativeAction action, string[] filters)
    {
        var arguments = Assert.IsType<JsonObject>(JsonNode.Parse(Assert.IsType<string>(action.Detail))?["arguments"]);
        Assert.Single(arguments);
        Assert.Equal(filters, Assert.IsType<JsonArray>(arguments["filters"]).Select(static entry => entry!.GetValue<string>()));
    }

    private static string SourcePath => Path.Combine(RepositoryLayout.Root,
        "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/RemoveBreakpointContractTests.cs");

    private static string OtherSourcePath => Path.Combine(RepositoryLayout.Root,
        "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs");

    private static string LexicalAlias(string file)
    {
        var directory = Path.GetDirectoryName(file)!;
        return Path.Combine(Path.GetRelativePath(RepositoryLayout.Root, directory), ".", "..",
            Path.GetFileName(directory), Path.GetFileName(file));
    }

    private static ModernMcpStartOptions LaunchOptions(string responseMode = "success") => new(
        FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true, SetBreakpointsResponseMode: responseMode));

    private static JsonObject Arguments(string session, string file, int line) => new()
    {
        ["debugSessionId"] = session,
        ["file"] = file,
        ["line"] = line,
    };

    private static Task<JsonRpcResponse> CallAsync(ModernMcpProcessDriver driver, string tool, string session, string file, int line, string requestId) =>
        driver.CallToolRawAsync(tool, Arguments(session, file, line), ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId));

    private static async Task<string> StartSessionAsync(ModernMcpProcessDriver driver, string requestId, params (string File, int Line)[] breakpoints)
    {
        var content = AssertEnvelope(await driver.CallToolRawAsync("start_debug", new JsonObject
        {
            ["program"] = driver.InertProgramPath,
            ["breakpoints"] = new JsonArray(breakpoints.Select(static entry => (JsonNode)new JsonObject
            { ["file"] = entry.File, ["line"] = entry.Line }).ToArray()),
        }, ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: false);
        Assert.Equal("start_debug_success", content["kind"]?.GetValue<string>());
        return Assert.IsType<string>(content["debugSessionId"]?.GetValue<string>());
    }

    private static async Task ReaderBarrierAsync(ModernMcpProcessDriver driver, string session, string requestId, CancellationToken cancellationToken)
    {
        var content = AssertEnvelope(await driver.CallToolRawAsync("get_threads", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId), cancellationToken), isError: false);
        Assert.Equal("threads_success", content["kind"]?.GetValue<string>());
    }

    private static async Task<JsonObject> ReadStateAsync(ModernMcpProcessDriver driver, string session, string requestId)
    {
        var content = AssertEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: false);
        Assert.Equal("debug_state_success", content["kind"]?.GetValue<string>());
        return content;
    }

    private static async Task AssertSessionNotFoundAsync(ModernMcpProcessDriver driver, string session, string requestId)
    {
        var content = AssertEnvelope(await driver.CallToolRawAsync("get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(requestId)), isError: true);
        AssertNotFound(content);
    }

    private static JsonObject AssertEnvelope(JsonRpcResponse response, bool isError)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() == true);
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", text["type"]?.GetValue<string>());
        Assert.Equal(content.ToJsonString(), text["text"]?.GetValue<string>());
        Assert.DoesNotContain("private controlled breakpoint payload", result.ToJsonString());
        Assert.DoesNotContain("private controlled exception filter payload", result.ToJsonString());
        return content;
    }

    private static void AssertRemoved(JsonRpcResponse response, string file, int line, bool removed)
    {
        var content = AssertEnvelope(response, isError: false);
        Assert.Equal("breakpoint_removed", content["kind"]?.GetValue<string>());
        Assert.Equal(Path.GetFullPath(file), content["file"]?.GetValue<string>());
        Assert.Equal(line, content["line"]?.GetValue<int>());
        Assert.Equal(removed, content["removed"]?.GetValue<bool>());
        Assert.Equal(4, content.Count);
    }

    private static void AssertAdded(JsonRpcResponse response, string file, int line)
    {
        var content = AssertEnvelope(response, isError: false);
        Assert.Equal("breakpoint_added", content["kind"]?.GetValue<string>());
        Assert.Equal(Path.GetFullPath(file), content["file"]?.GetValue<string>());
        Assert.Equal(line, content["line"]?.GetValue<int>());
    }

    private static void AssertError(JsonObject content, string kind, string error)
    {
        Assert.Equal(kind, content["kind"]?.GetValue<string>());
        Assert.Equal(error, content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
    }

    private static void AssertNotFound(JsonObject content) => AssertError(content, "debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");

    private static async Task<ModernNativeAction[]> ReadReplacementsAsync(ModernMcpProcessDriver driver, int after = 0) =>
        (await driver.ReadNativeActionsAsync()).Skip(after).Where(static action => action.Command == "setBreakpoints").ToArray();

    private static void AssertReplacement(ModernNativeAction action, string source, int[] lines)
    {
        var arguments = Assert.IsType<JsonObject>(JsonNode.Parse(Assert.IsType<string>(action.Detail))?["arguments"]);
        Assert.Equal(Path.GetFullPath(source), arguments["source"]?["path"]?.GetValue<string>());
        Assert.Equal(lines, Assert.IsType<JsonArray>(arguments["breakpoints"]).Select(static entry => entry!["line"]!.GetValue<int>()));
    }
}
