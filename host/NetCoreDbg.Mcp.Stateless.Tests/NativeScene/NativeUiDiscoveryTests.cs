using System.Diagnostics;
using System.Globalization;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Win32.SafeHandles;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeUiDiscoveryTests
{
    private const string DiscoveryTool = "ui_get_window_tree";
    private const string ActionName = "Controlled discovery action";
    private static readonly TimeSpan OperationAndCleanupBound = TimeSpan.FromSeconds(12);

    [Fact]
    public async Task Catalog_ExposesClosedDiscoveryAndOptionalClosedInvokeExpectation()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(30));
        var response = await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("discovery-catalog"));
        var tools = Array(ModernMcpProcessDriver.RequireResult(response)["tools"]).Select(Object).ToArray();
        var discovery = Assert.Single(tools, tool => Text(tool["name"]) == DiscoveryTool);
        var schema = Object(discovery["inputSchema"]);
        Assert.False(Bool(schema["additionalProperties"]));
        Assert.Equal(new[] { "debugSessionId" }, Array(schema["required"]).Select(Text));
        var properties = Object(schema["properties"]);
        Assert.Equal(new[] { "debugSessionId", "maxChildren", "maxDepth" }, properties.Select(item => item.Key).OrderBy(key => key, StringComparer.Ordinal));
        AssertIntegerRange(Object(properties["maxDepth"]), 0, 32, 3);
        AssertIntegerRange(Object(properties["maxChildren"]), 1, 256, 50);
        var invoke = Assert.Single(tools, tool => Text(tool["name"]) == "ui_invoke");
        var invokeSchema = Object(invoke["inputSchema"]);
        Assert.DoesNotContain("expectedTarget", Array(invokeSchema["required"]).Select(Text));
        var expectation = Object(Object(invokeSchema["properties"])["expectedTarget"]);
        Assert.False(Bool(expectation["additionalProperties"]));
        var fields = new[] { "elementRuntimeId", "hwnd", "processIdentity", "rootRuntimeId" };
        Assert.Equal(fields, Object(expectation["properties"]).Select(item => item.Key).OrderBy(key => key, StringComparer.Ordinal));
        Assert.Equal(fields, Array(expectation["required"]).Select(Text).OrderBy(key => key, StringComparer.Ordinal));
    }

    [Fact]
    public async Task Catalog_ObservedInvoke_ExposesClosedRequiredTargetAndExpectedAutomationId()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(30));
        var response = await driver.ListToolsRawAsync(ModernMcpProcessDriver.CurrentMeta(), new RequestId("observed-invoke-catalog"));
        var tools = Array(ModernMcpProcessDriver.RequireResult(response)["tools"]).Select(Object);
        var tool = Assert.Single(tools, item => Text(item["name"]) == "ui_invoke_observed");
        var schema = Object(tool["inputSchema"]);
        Assert.False(Bool(schema["additionalProperties"]));
        var fields = new[] { "debugSessionId", "expectedAutomationId", "target" };
        Assert.Equal(fields, Object(schema["properties"]).Select(item => item.Key).OrderBy(key => key, StringComparer.Ordinal));
        Assert.Equal(fields, Array(schema["required"]).Select(Text).OrderBy(key => key, StringComparer.Ordinal));
        var properties = Object(schema["properties"]);
        Assert.Equal("string", Text(Object(properties["debugSessionId"])["type"]));
        var expectedId = Object(properties["expectedAutomationId"]);
        Assert.Equal("string", Text(expectedId["type"]));
        Assert.Equal(1, Int(expectedId["minLength"]));
        Assert.Equal(256, Int(expectedId["maxLength"]));
        var target = Object(properties["target"]);
        Assert.Equal("object", Text(target["type"]));
        Assert.False(Bool(target["additionalProperties"]));
        var identityFields = new[] { "elementRuntimeId", "hwnd", "processIdentity", "rootRuntimeId" };
        var identityProperties = Object(target["properties"]);
        Assert.Equal(identityFields, identityProperties.Select(item => item.Key).OrderBy(key => key, StringComparer.Ordinal));
        Assert.Equal(identityFields, Array(target["required"]).Select(Text).OrderBy(key => key, StringComparer.Ordinal));
        foreach (var field in new[] { "hwnd", "processIdentity" })
            Assert.Equal("string", Text(Object(identityProperties[field])["type"]));
        foreach (var field in new[] { "elementRuntimeId", "rootRuntimeId" })
        {
            var runtimeId = Object(identityProperties[field]);
            Assert.Equal("array", Text(runtimeId["type"]));
            Assert.Equal(1, Int(runtimeId["minItems"]));
            Assert.Equal(64, Int(runtimeId["maxItems"]));
            var item = Object(runtimeId["items"]);
            Assert.Equal("integer", Text(item["type"]));
            Assert.Equal(int.MinValue, Int(item["minimum"]));
            Assert.Equal(int.MaxValue, Int(item["maximum"]));
        }
    }

    [Fact]
    public async Task Discovery_ObservedUnknownSelector_StrictRefusesAndObservedInvokeActsOnceOnOnlyTheBoundFixture()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls);
        var session = await StartSessionAsync(driver, controls);
        using var otherControls = new DiscoveryFixtureControls();
        await using var other = await StartDriverAsync(otherControls);
        var otherSession = await StartSessionAsync(other, otherControls);
        var foreground = GetForegroundWindow();
        Assert.NotEqual(IntPtr.Zero, foreground);

        var observation = await ObserveAsync(driver, session, maxDepth: 32, maxChildren: 256);

        AssertTree(observation.Content, session, 32, 256);
        Assert.Equal(foreground, GetForegroundWindow());
        AssertCounter(observation.Content, 0);
        var action = FindAction(observation.Content);
        var otherTree = await ObserveAsync(other, otherSession, 32, 256);
        Assert.NotEqual(Text(action["automationId"]), Text(FindAction(otherTree.Content)["automationId"]));
        AssertRefused(await CallAsync(driver, "ui_invoke", InvokeArguments(session, observation.Content, action)), "UI_INVOKE_SELECTION_UNREADABLE");
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
        AssertObservedSuccess(await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, observation.Content, action)));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
        AssertCounter((await ObserveAsync(other, otherSession, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task GuardedInvoke_DemonstrablyReadableCompleteWindow_InvokesTheUniqueObservedAutomationIdOnce()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "readable");
        var session = await StartSessionAsync(driver, controls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        AssertTree(tree, session, 32, 256);
        Assert.False(Bool(tree["truncated"]));
        Assert.All(Nodes(tree), node =>
        {
            Assert.True(Bool(node["childrenComplete"]));
            Assert.NotNull(node["automationId"]);
            Assert.DoesNotContain("automationId", Array(node["unavailableFields"]).Select(Text));
            Assert.DoesNotContain("automationId", Array(node["truncatedFields"]).Select(Text));
        });
        AssertCounter(tree, 0);
        var action = FindAction(tree);
        Assert.Single(Nodes(tree), node => Text(node["automationId"]) == Text(action["automationId"]));

        var result = await CallAsync(driver, "ui_invoke", InvokeArguments(session, tree, action));

        Assert.False(result.IsError, result.Result.ToJsonString());
        Assert.Equal("ui_invoke_success", Text(result.Content["kind"]));
        Assert.True(Bool(result.Content["invoked"]));
        Assert.Equal("InvokePattern", Text(result.Content["method"]));
        Assert.Equal(Text(action["automationId"]), Text(result.Content["automationId"]));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Fact]
    public async Task Discovery_DefaultLimits_AreExplicitAndObservationDoesNotInvoke()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls);
        var session = await StartSessionAsync(driver, controls);
        var observed = await CallAsync(driver, DiscoveryTool, new JsonObject { ["debugSessionId"] = session.Id });
        Assert.False(observed.IsError, observed.Result.ToJsonString());
        AssertTree(observed.Content, session, 3, 50);
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
    }

    [Theory]
    [InlineData("processIdentity")]
    [InlineData("hwnd")]
    [InlineData("rootRuntimeId")]
    [InlineData("elementRuntimeId")]
    public async Task GuardedInvoke_ExpectationFromWrongProcessWindowRootOrElement_RefusesWithoutAction(string field)
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "readable");
        var session = await StartSessionAsync(driver, controls);
        using var otherControls = new DiscoveryFixtureControls();
        await using var other = await StartDriverAsync(otherControls, "readable");
        var otherSession = await StartSessionAsync(other, otherControls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        var otherTree = (await ObserveAsync(other, otherSession, 32, 256)).Content;
        foreach (var observation in new[] { tree, otherTree })
        {
            Assert.False(Bool(observation["truncated"]));
            Assert.All(Nodes(observation), node =>
            {
                Assert.True(Bool(node["childrenComplete"]));
                Assert.NotNull(node["automationId"]);
                Assert.DoesNotContain("automationId", Array(node["unavailableFields"]).Select(Text));
                Assert.DoesNotContain("automationId", Array(node["truncatedFields"]).Select(Text));
            });
        }
        var arguments = InvokeArguments(session, tree, FindAction(tree));
        var expected = Object(arguments["expectedTarget"]);
        var wrong = field switch
        {
            "processIdentity" => Object(otherTree["process"])["processIdentity"],
            "hwnd" => Object(otherTree["window"])["hwnd"],
            "rootRuntimeId" => FindAction(tree)["runtimeId"],
            "elementRuntimeId" => Object(tree["window"])["runtimeId"],
            _ => throw new ArgumentOutOfRangeException(nameof(field)),
        };
        Assert.NotEqual(expected[field]!.ToJsonString(), wrong!.ToJsonString());
        expected[field] = wrong.DeepClone();

        var result = await CallAsync(driver, "ui_invoke", arguments);

        AssertRefused(result, "UI_INVOKE_TARGET_CHANGED");
        await controls.DispatcherBarrierAsync();
        await otherControls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
        AssertCounter((await ObserveAsync(other, otherSession, 32, 256)).Content, 0);
        AssertObservedSuccess(await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, tree, FindAction(tree))));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Fact]
    public async Task GuardedInvoke_ElementReplacedAfterObservation_DoesNotInvokeTheReplacement()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "readable");
        var session = await StartSessionAsync(driver, controls);
        var before = (await ObserveAsync(driver, session, 32, 256)).Content;
        var previous = FindAction(before);
        await controls.MutateAsync("replace", "replaced");
        var after = (await ObserveAsync(driver, session, 32, 256)).Content;
        foreach (var observation in new[] { before, after })
        {
            Assert.False(Bool(observation["truncated"]));
            Assert.All(Nodes(observation), node =>
            {
                Assert.True(Bool(node["childrenComplete"]));
                Assert.NotNull(node["automationId"]);
                Assert.DoesNotContain("automationId", Array(node["unavailableFields"]).Select(Text));
                Assert.DoesNotContain("automationId", Array(node["truncatedFields"]).Select(Text));
            });
        }
        var replacement = FindAction(after);
        Assert.Equal(Text(previous["automationId"]), Text(replacement["automationId"]));
        Assert.NotEqual(Array(previous["runtimeId"]).ToJsonString(), Array(replacement["runtimeId"]).ToJsonString());

        AssertRefused(await CallAsync(driver, "ui_invoke", InvokeArguments(session, before, previous)), "UI_INVOKE_TARGET_CHANGED");

        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
        AssertObservedSuccess(await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, after, replacement)));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Fact]
    public async Task GuardedInvoke_LateDuplicateOutsideThePartialObservation_StillRunsFullUniquenessCheck()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "wide");
        var session = await StartSessionAsync(driver, controls);
        var partial = (await ObserveAsync(driver, session, 32, 12)).Content;
        var action = FindAction(partial);
        Assert.True(Bool(partial["truncated"]));
        Assert.Single(Nodes(partial), node => node["automationId"]?.GetValue<string>() == Text(action["automationId"]));
        await controls.MutateAsync("duplicate", "duplicated");

        AssertRefused(await CallAsync(driver, "ui_invoke", InvokeArguments(session, partial, action)), "ELEMENT_AMBIGUOUS");

        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task Discovery_DuplicateIdsRemainDistinctObservations_AndCannotAuthorizeInvoke()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls);
        var session = await StartSessionAsync(driver, controls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        var duplicates = Nodes(tree).Where(node => node["automationId"]?.GetValue<string>() == "DiscoveryDuplicate").ToArray();
        Assert.Equal(2, duplicates.Length);
        Assert.NotEqual(Int(duplicates[0]["nodeId"]), Int(duplicates[1]["nodeId"]));
        Assert.NotEqual(Array(duplicates[0]["runtimeId"]).ToJsonString(), Array(duplicates[1]["runtimeId"]).ToJsonString());
        AssertRefused(await CallAsync(driver, "ui_invoke", InvokeArguments(session, tree, duplicates[0])), "ELEMENT_AMBIGUOUS");
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task Discovery_OverlongIdsAreNotShortenedIntoActionableSelectors_UnicodeLimitCountsScalars()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "selectors");
        var session = await StartSessionAsync(driver, controls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        foreach (var name in new[] { "Overlong discovery selector", "Overlong supplementary selector" })
        {
            var node = RequireSingleNamedNode(tree, name, "Button");
            Assert.Null(node["automationId"]);
            Assert.Contains("automationId", Array(node["truncatedFields"]).Select(Text));
        }
        var exact = RequireSingleNamedNode(tree, "Supplementary discovery selector", "Button");
        Assert.Equal(string.Concat(Enumerable.Repeat("\U0001F600", 256)), Text(exact["automationId"]));
        Assert.DoesNotContain("automationId", Array(exact["truncatedFields"]).Select(Text));
        AssertRefused(await CallAsync(driver, "ui_invoke", new JsonObject
        {
            ["debugSessionId"] = session.Id,
            ["automationId"] = new string('x', 256),
            ["expectedTarget"] = ExpectedTarget(tree, FindAction(tree)),
        }), "UI_INVOKE_SELECTION_UNREADABLE");
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task Discovery_UnreadableIdIsExplicit_AndInvokeCannotAssumeCompleteSelection()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "unreadable");
        var session = await StartSessionAsync(driver, controls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        var unreadable = RequireSingleNamedNode(tree, "Unreadable selector", "Button");
        Assert.Null(unreadable["automationId"]);
        Assert.Contains("automationId", Array(unreadable["unavailableFields"]).Select(Text));
        AssertRefused(await CallAsync(driver, "ui_invoke", InvokeArguments(session, tree, FindAction(tree))), "UI_INVOKE_SELECTION_UNREADABLE");
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task ObservedInvoke_UnrelatedUnreadableAutomationId_InvokesTheObservedReadableActionExactlyOnce()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "unreadable");
        var session = await StartSessionAsync(driver, controls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        var unreadable = RequireSingleNamedNode(tree, "Unreadable selector", "Button");
        Assert.Null(unreadable["automationId"]);
        Assert.Contains("automationId", Array(unreadable["unavailableFields"]).Select(Text));
        AssertCounter(tree, 0);
        var action = FindAction(tree);

        var result = await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, tree, action));

        AssertObservedSuccess(result);
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Theory]
    [InlineData("Duplicate action one", "Duplicate action two")]
    [InlineData("Duplicate action two", "Duplicate action one")]
    public async Task ObservedInvoke_DuplicateAutomationIds_OnlyTheSelectedRuntimeIdChangesItsOwnStatus(string selectedName, string otherName)
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "duplicate-status");
        var session = await StartSessionAsync(driver, controls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        var selected = RequireSingleNamedNode(tree, selectedName, "Button");
        var other = RequireSingleNamedNode(tree, otherName, "Button");
        Assert.Equal(Text(selected["automationId"]), Text(other["automationId"]));
        Assert.NotEqual(Array(selected["runtimeId"]).ToJsonString(), Array(other["runtimeId"]).ToJsonString());
        AssertCounter(tree, 0);
        _ = RequireSingleNamedNode(tree, $"{selectedName} count: 0", "Text");
        _ = RequireSingleNamedNode(tree, $"{otherName} count: 0", "Text");

        var result = await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, tree, selected));

        AssertObservedSuccess(result);
        await controls.DispatcherBarrierAsync();
        var after = (await ObserveAsync(driver, session, 32, 256)).Content;
        AssertCounter(after, 1);
        _ = RequireSingleNamedNode(after, $"{selectedName} count: 1", "Text");
        _ = RequireSingleNamedNode(after, $"{otherName} count: 0", "Text");
        Assert.DoesNotContain(Nodes(after), node => node["name"]?.GetValue<string>() == $"{selectedName} count: 0");
        Assert.DoesNotContain(Nodes(after), node => node["name"]?.GetValue<string>() == $"{otherName} count: 1");
    }

    [Theory]
    [InlineData("replace", "replaced")]
    [InlineData("rename-action", "action-renamed")]
    [InlineData("replace-window", "window-replaced")]
    public async Task ObservedInvoke_StaleElementChangedIdOrWindow_RefusesWithoutAction(string mutation, string completed)
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "unreadable");
        var session = await StartSessionAsync(driver, controls);
        var before = (await ObserveAsync(driver, session, 32, 256)).Content;
        var previous = FindAction(before);
        await controls.MutateAsync(mutation, completed);
        var after = (await ObserveAsync(driver, session, 32, 256)).Content;
        var current = FindAction(after);
        if (mutation == "replace")
        {
            Assert.Equal(Text(previous["automationId"]), Text(current["automationId"]));
            Assert.NotEqual(Array(previous["runtimeId"]).ToJsonString(), Array(current["runtimeId"]).ToJsonString());
        }
        else if (mutation == "rename-action")
        {
            Assert.NotEqual(Text(previous["automationId"]), Text(current["automationId"]));
            Assert.Equal(Array(previous["runtimeId"]).ToJsonString(), Array(current["runtimeId"]).ToJsonString());
        }
        else
        {
            Assert.NotEqual(Text(Object(before["window"])["hwnd"]), Text(Object(after["window"])["hwnd"]));
            Assert.NotEqual(Array(Object(before["window"])["runtimeId"]).ToJsonString(), Array(Object(after["window"])["runtimeId"]).ToJsonString());
        }

        AssertObservedTargetChanged(await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, before, previous)));

        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
        AssertObservedSuccess(await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, after, current)));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Fact]
    public async Task ObservedInvoke_TargetReplacedAfterRealHelperReady_CommitRefusesWithoutAction()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "unreadable");
        var session = await StartSessionAsync(driver, controls);
        var before = (await ObserveAsync(driver, session, 32, 256)).Content;
        var previous = FindAction(before);
        AssertCounter(before, 0);
        var request = new JsonObject
        {
            ["operation"] = "prepare_observed",
            ["processId"] = session.ProcessId,
            ["processIdentity"] = Text(Object(before["process"])["processIdentity"]),
            ["hwnd"] = long.Parse(Text(Object(before["window"])["hwnd"]), CultureInfo.InvariantCulture),
            ["automationId"] = Text(previous["automationId"]),
            ["expectedTarget"] = ExpectedTarget(before, previous),
        };
        var pipeName = "native-ui-commit-recheck-" + Guid.NewGuid().ToString("N");
        var nonce = Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
        var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));
        var clientType = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeUi.NativeUiInvokeBridgeClient", throwOnError: true)!;
        await using var client = Assert.IsAssignableFrom<IAsyncDisposable>(Activator.CreateInstance(
            clientType, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null, args: [pipeName, TimeSpan.FromSeconds(10)], culture: null));
        var sendAsync = clientType.GetMethod("SendAsync", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?? throw new InvalidOperationException("Missing real one-shot observed invoke transport.");
        var startInfo = new ProcessStartInfo(Environment.GetEnvironmentVariable("DOTNET_HOST_PATH") is { Length: > 0 } dotnetHost
            ? dotnetHost : "dotnet.exe")
        {
            WorkingDirectory = RepositoryLayout.Root,
            UseShellExecute = false,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            CreateNoWindow = true,
        };
        startInfo.Environment["DOTNET_ADD_GLOBAL_TOOLS_TO_PATH"] = "false";
        foreach (var argument in new[] { ResolveBridge(), "--native-ui-invoke-pipe", pipeName, nonce, session.ProcessId.ToString(CultureInfo.InvariantCulture) })
            startInfo.ArgumentList.Add(argument);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        using var commit = new ManualResetEventSlim(false);
        var prepared = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var commitAuthorizations = 0;
        Func<bool> authorizeCommit = () =>
        {
            Interlocked.Increment(ref commitAuthorizations);
            // The production client calls this only after validating the real helper's ready frame.
            prepared.TrySetResult(true);
            commit.Wait(deadline.Token);
            return true;
        };
        using var helper = new Process { StartInfo = startInfo };
        Assert.True(helper.Start());
        Task drains = Task.CompletedTask;
        Task<JsonObject>? pending = null;
        try
        {
            _ = helper.SafeHandle;
            drains = Task.WhenAll(helper.StandardOutput.BaseStream.CopyToAsync(Stream.Null), helper.StandardError.BaseStream.CopyToAsync(Stream.Null));
            helper.StandardInput.Dispose();
            // Even synchronous pipe completions must not block the test before it can mutate the fixture.
            pending = Task.Run(() => Assert.IsAssignableFrom<Task<JsonObject>>(
                sendAsync.Invoke(client, [nonce, request, authorizeCommit, deadline.Token])), deadline.Token);
            var first = await Task.WhenAny(prepared.Task, pending).WaitAsync(deadline.Token);
            var earlyFailure = "The real transport ended before helper readiness.";
            if (first != prepared.Task && pending.IsCompletedSuccessfully)
            {
                var earlyResult = await pending;
                earlyFailure = $"The real helper did not prepare: {earlyResult.ToJsonString()}";
            }
            Assert.True(first == prepared.Task, earlyFailure);
            await prepared.Task.WaitAsync(deadline.Token);
            Assert.False(pending.IsCompleted);

            await controls.MutateAsync("replace", "replaced");
            commit.Set();
            var result = await pending.WaitAsync(deadline.Token);

            Assert.Equal(1, Volatile.Read(ref commitAuthorizations));
            Assert.Equal("tool_error", Text(result["kind"]));
            Assert.Equal("ui_invoke_observed", Text(result["tool"]));
            Assert.Equal("UI_INVOKE_TARGET_CHANGED", ErrorCode(result));
            Assert.Equal("not_invoked", Text(result["outcome"]));
            Assert.False(result.ContainsKey("invoked"));
        }
        finally
        {
            deadline.Cancel();
            commit.Set();
            await client.DisposeAsync();
            using var cleanup = new CancellationTokenSource(TimeSpan.FromSeconds(2));
            if (!helper.HasExited)
                helper.Kill();
            await helper.WaitForExitAsync(cleanup.Token);
            await drains.WaitAsync(cleanup.Token);
            if (pending is not null)
                await pending.WaitAsync(cleanup.Token);
        }

        await controls.DispatcherBarrierAsync();
        var after = (await ObserveAsync(driver, session, 32, 256)).Content;
        var replacement = FindAction(after);
        Assert.Equal(Text(previous["automationId"]), Text(replacement["automationId"]));
        Assert.NotEqual(Array(previous["runtimeId"]).ToJsonString(), Array(replacement["runtimeId"]).ToJsonString());
        AssertCounter(after, 0);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task ObservedInvoke_UnconfirmedRealHelperDrain_PreservesOutcomeBlocksAdmissionAndRecovers(bool invokeBeforeCleanup)
    {
        const BindingFlags instanceMembers = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
        var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));
        var sessionType = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeUi.NativeUiInvokeSession", throwOnError: true)!;
        var constructor = sessionType.GetConstructor(instanceMembers, binder: null, types: [typeof(Func<Task, Task>)], modifiers: null);
        Assert.True(constructor is not null,
            "Missing internal NativeUiInvokeSession(Func<Task, Task> drainCompletion) constructor for holding completion after the real helper stdout/stderr drains finish.");
        Assert.True(constructor!.IsAssembly, "The drain-completion seam must remain internal.");
        Assert.NotNull(sessionType.GetConstructor(instanceMembers, binder: null, types: Type.EmptyTypes, modifiers: null));
        var invokeAsync = sessionType.GetMethod("InvokeAsync", instanceMembers)
            ?? throw new InvalidOperationException("Missing real NativeUiInvokeSession.InvokeAsync.");
        var retainedLaunchField = sessionType.GetField("_pendingCleanup", instanceMembers)
            ?? throw new InvalidOperationException("Missing retained real helper ownership.");
        var retainedDrainField = sessionType.GetField("_pendingDrains", instanceMembers)
            ?? throw new InvalidOperationException("Missing retained helper drains.");

        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "unreadable");
        var session = await StartSessionAsync(driver, controls);
        var before = (await ObserveAsync(driver, session, 32, 256)).Content;
        AssertCounter(before, 0);
        var action = FindAction(before);
        var identityType = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneTargetIdentity", throwOnError: true)!;
        var executable = ResolveFixtureExecutable();
        string executableHash;
        using (var executableStream = File.OpenRead(executable))
            executableHash = Convert.ToHexString(SHA256.HashData(executableStream));
        var identity = Activator.CreateInstance(identityType, instanceMembers, binder: null,
            args: [session.ProcessId, Text(Object(before["process"])["processIdentity"]), executable, executableHash, null, null], culture: null)
            ?? throw new InvalidOperationException("Could not construct the observed owned fixture identity.");
        var hwnd = long.Parse(Text(Object(before["window"])["hwnd"]), CultureInfo.InvariantCulture);
        var bridgeExecutable = Path.ChangeExtension(ResolveBridge(), ".exe");
        Assert.True(File.Exists(bridgeExecutable), $"Built candidate bridge apphost is absent: {bridgeExecutable}");
        var helperPrepared = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        using var releaseFirstCommit = new ManualResetEventSlim(false);
        HashSet<int>? childrenBeforeLaunch = null;
        Process? observedHelper = null;
        SafeProcessHandle? observedHelperHandle = null;
        var abandonCommit = 0;
        var reachedDrainBarrier = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var releaseDrainBarrier = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        using var barrierSafety = new CancellationTokenSource(TimeSpan.FromSeconds(60));
        Task? firstRealDrain = null;
        Task? firstHeldDrain = null;
        var helperDrainsCreated = 0;
        var authorizations = 0;

        async Task HoldDrainCompletionAsync(Task realDrain)
        {
            await realDrain.WaitAsync(barrierSafety.Token).ConfigureAwait(false);
            reachedDrainBarrier.TrySetResult(true);
            await releaseDrainBarrier.Task.WaitAsync(barrierSafety.Token).ConfigureAwait(false);
        }

        Task WrapRealDrain(Task realDrain)
        {
            Interlocked.Increment(ref helperDrainsCreated);
            var held = HoldDrainCompletionAsync(realDrain);
            firstRealDrain ??= realDrain;
            firstHeldDrain ??= held;
            return held;
        }

        bool AuthorizeDispatch()
        {
            var authorization = Interlocked.Increment(ref authorizations);
            if (authorization == 1)
                childrenBeforeLaunch = OwnedHelperObservation.CaptureDirectChildIds(Environment.ProcessId);
            if (authorization == 2)
            {
                try
                {
                    Assert.NotNull(childrenBeforeLaunch);
                    observedHelper = OwnedHelperObservation.CaptureNewBridgeHelper(Environment.ProcessId, childrenBeforeLaunch, bridgeExecutable);
                    observedHelperHandle = observedHelper.SafeHandle;
                    helperPrepared.TrySetResult(true);
                    releaseFirstCommit.Wait(barrierSafety.Token);
                }
                catch (Exception exception)
                {
                    helperPrepared.TrySetException(exception);
                    throw;
                }
            }
            return Volatile.Read(ref abandonCommit) == 0;
        }

        var owner = Assert.IsAssignableFrom<IAsyncDisposable>(constructor!.Invoke([(Func<Task, Task>)WrapRealDrain]));
        Task<JsonObject> InvokeObserved(JsonObject tree) => Assert.IsAssignableFrom<Task<JsonObject>>(invokeAsync.Invoke(owner,
        [
            bridgeExecutable, identity, hwnd, Text(FindAction(tree)["automationId"]), (Func<bool>)AuthorizeDispatch,
            CancellationToken.None, ExpectedTarget(tree, FindAction(tree)), true,
        ]));

        static void AssertCleanupUnconfirmed(JsonObject result, string outcome)
        {
            Assert.Equal("tool_error", Text(result["kind"]));
            Assert.Equal("ui_invoke_observed", Text(result["tool"]));
            Assert.Equal("UI_INVOKE_CLEANUP_UNCONFIRMED", ErrorCode(result));
            Assert.Equal(outcome, Text(result["outcome"]));
            Assert.False(result.ContainsKey("invoked"));
        }

        Task<JsonObject>? pending = null;
        try
        {
            pending = Task.Run(() => InvokeObserved(before));
            var preparation = await Task.WhenAny(helperPrepared.Task, pending).WaitAsync(OperationAndCleanupBound);
            if (preparation != helperPrepared.Task)
            {
                var earlyResult = await pending.WaitAsync(OperationAndCleanupBound);
                Assert.Fail($"The real session ended before its ready callback could pin the live helper: {earlyResult.ToJsonString()}");
            }
            await helperPrepared.Task.WaitAsync(OperationAndCleanupBound);
            Assert.NotNull(observedHelper);
            Assert.False(observedHelper.HasExited);
            if (!invokeBeforeCleanup)
                await controls.MutateAsync("replace", "replaced");
            releaseFirstCommit.Set();
            var first = await Task.WhenAny(reachedDrainBarrier.Task, pending).WaitAsync(OperationAndCleanupBound);
            if (first != reachedDrainBarrier.Task)
            {
                var earlyResult = await pending.WaitAsync(OperationAndCleanupBound);
                Assert.Fail($"The real session completed before its actual drains reached the completion barrier: {earlyResult.ToJsonString()}");
            }
            await reachedDrainBarrier.Task.WaitAsync(OperationAndCleanupBound);
            var result = await pending.WaitAsync(OperationAndCleanupBound);
            AssertCleanupUnconfirmed(result, invokeBeforeCleanup ? "invoked" : "not_invoked");
            Assert.Equal(1, Volatile.Read(ref helperDrainsCreated));
            Assert.NotNull(firstRealDrain);
            Assert.True(firstRealDrain.IsCompletedSuccessfully);
            Assert.NotNull(firstHeldDrain);
            Assert.False(firstHeldDrain.IsCompleted);
            var retainedLaunch = retainedLaunchField.GetValue(owner);
            Assert.NotNull(retainedLaunch);
            var helper = Assert.IsType<Process>(retainedLaunch.GetType().GetProperty("Process", instanceMembers)?.GetValue(retainedLaunch));
            Assert.Equal(observedHelper.Id, helper.Id);
            var helperHandle = observedHelperHandle;
            Assert.NotNull(helperHandle);
            Assert.False(helperHandle.IsClosed);
            Assert.True(helper.HasExited, "The actual owned helper must exit before only its drain confirmation is held.");
            Assert.True(observedHelper.HasExited);
            var retainedDrain = Assert.IsAssignableFrom<Task>(retainedDrainField.GetValue(owner));
            Assert.False(retainedDrain.IsCompleted);

            var authorizationsBeforeBlockedCall = Volatile.Read(ref authorizations);
            pending = InvokeObserved(before);
            AssertCleanupUnconfirmed(await pending.WaitAsync(TimeSpan.FromSeconds(2)), "not_invoked");
            Assert.Equal(authorizationsBeforeBlockedCall, Volatile.Read(ref authorizations));
            Assert.Equal(1, Volatile.Read(ref helperDrainsCreated));
            Assert.Same(retainedLaunch, retainedLaunchField.GetValue(owner));
            Assert.Same(retainedDrain, retainedDrainField.GetValue(owner));
            Assert.False(retainedDrain.IsCompleted);
            Assert.False(helperHandle.IsClosed);
            Assert.False(firstHeldDrain.IsCompleted);

            // This separate MCP owner only observes the fixture; it cannot release the tested session's retained slot.
            await controls.DispatcherBarrierAsync();
            var afterFirstCall = (await ObserveAsync(driver, session, 32, 256)).Content;
            var expectedFirstCount = invokeBeforeCleanup ? 1 : 0;
            AssertCounter(afterFirstCall, expectedFirstCount);
            if (!invokeBeforeCleanup)
            {
                var replacement = FindAction(afterFirstCall);
                Assert.Equal(Text(action["automationId"]), Text(replacement["automationId"]));
                Assert.NotEqual(Array(action["runtimeId"]).ToJsonString(), Array(replacement["runtimeId"]).ToJsonString());
            }

            releaseDrainBarrier.TrySetResult(true);
            await retainedDrain.WaitAsync(TimeSpan.FromSeconds(2));
            pending = InvokeObserved(afterFirstCall);
            var recovered = await pending.WaitAsync(OperationAndCleanupBound);
            Assert.Equal("ui_invoke_observed_success", Text(recovered["kind"]));
            Assert.Equal("runtime_id", Text(recovered["selection"]));
            Assert.True(Bool(recovered["invoked"]));
            Assert.Equal("InvokePattern", Text(recovered["method"]));
            Assert.Equal("unproven", Text(recovered["atomicity"]));
            Assert.Equal(2, Volatile.Read(ref helperDrainsCreated));
            Assert.False(helperHandle.IsClosed, "The independently captured creation-bound handle stays open until the test disposes it.");
            Assert.Throws<InvalidOperationException>(() => { _ = helper.Id; });
            Assert.Null(retainedLaunchField.GetValue(owner));
            Assert.NotSame(retainedDrain, retainedDrainField.GetValue(owner));
            Assert.True(Assert.IsAssignableFrom<Task>(retainedDrainField.GetValue(owner)).IsCompletedSuccessfully);
            await controls.DispatcherBarrierAsync();
            AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, expectedFirstCount + 1);
        }
        finally
        {
            Interlocked.Exchange(ref abandonCommit, 1);
            releaseFirstCommit.Set();
            releaseDrainBarrier.TrySetResult(true);
            try
            {
                if (pending is not null)
                    await pending.WaitAsync(OperationAndCleanupBound);
            }
            finally
            {
                try
                {
                    await owner.DisposeAsync().AsTask().WaitAsync(OperationAndCleanupBound);
                }
                finally
                {
                    observedHelper?.Dispose();
                }
            }
        }
    }

    [Theory]
    [InlineData("processIdentity")]
    [InlineData("hwnd")]
    [InlineData("rootRuntimeId")]
    [InlineData("elementRuntimeId")]
    public async Task ObservedInvoke_WrongProcessWindowRootOrElement_RefusesWithoutAffectingEitherFixture(string field)
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "unreadable");
        var session = await StartSessionAsync(driver, controls);
        using var otherControls = new DiscoveryFixtureControls();
        await using var other = await StartDriverAsync(otherControls, "unreadable");
        var otherSession = await StartSessionAsync(other, otherControls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        var otherTree = (await ObserveAsync(other, otherSession, 32, 256)).Content;
        var arguments = ObservedInvokeArguments(session, tree, FindAction(tree));
        var target = Object(arguments["target"]);
        var wrong = field switch
        {
            "processIdentity" => Object(otherTree["process"])["processIdentity"],
            "hwnd" => Object(otherTree["window"])["hwnd"],
            "rootRuntimeId" => FindAction(tree)["runtimeId"],
            "elementRuntimeId" => Object(tree["window"])["runtimeId"],
            _ => throw new ArgumentOutOfRangeException(nameof(field)),
        };
        Assert.NotEqual(target[field]!.ToJsonString(), wrong!.ToJsonString());
        target[field] = wrong.DeepClone();

        AssertObservedTargetChanged(await CallAsync(driver, "ui_invoke_observed", arguments));

        await controls.DispatcherBarrierAsync();
        await otherControls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
        AssertCounter((await ObserveAsync(other, otherSession, 32, 256)).Content, 0);
        AssertObservedSuccess(await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, tree, FindAction(tree))));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
        AssertCounter((await ObserveAsync(other, otherSession, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task ObservedInvoke_MalformedExtraOrMissingArguments_RejectsBeforeAction()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "unreadable");
        var session = await StartSessionAsync(driver, controls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        var valid = ObservedInvokeArguments(session, tree, FindAction(tree));
        controls.Arm();
        try
        {
            foreach (var arguments in InvalidObservedInvokeArguments(valid))
            {
                AssertInvalidArguments(await CallAsync(driver, "ui_invoke_observed", arguments), "ui_invoke_observed");
                Assert.False(controls.HasEntered);
            }
        }
        finally
        {
            controls.Release();
        }
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
        AssertObservedSuccess(await CallAsync(driver, "ui_invoke_observed", valid));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Theory]
    [InlineData("wide", 32, 1)]
    [InlineData("deep", 0, 256)]
    [InlineData("deep", 32, 256)]
    public async Task Discovery_DepthAndPerParentLimits_AreExplicitAndCannotImplyAbsence(string mode, int depth, int children)
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, mode);
        var session = await StartSessionAsync(driver, controls);
        var reply = await ObserveAsync(driver, session, depth, children);
        AssertTree(reply.Content, session, depth, children);
        Assert.True(Bool(reply.Content["truncated"]));
        Assert.Contains(Nodes(reply.Content), node => !Bool(node["childrenComplete"]));
        if (depth == 0)
            Assert.Single(Nodes(reply.Content));
        if (mode == "deep")
            Assert.DoesNotContain(Nodes(reply.Content), node => node["automationId"]?.GetValue<string>() == "DeepHiddenAction");
        await controls.DispatcherBarrierAsync();
    }

    [Theory]
    [InlineData("overflow")]
    [InlineData("large-fields")]
    public async Task Discovery_LargeRealProvider_ProducesBoundedExplicitPartialResultOrTruthfulLimitError(string mode)
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, mode);
        var session = await StartSessionAsync(driver, controls);
        var reply = await CallAsync(driver, DiscoveryTool, DiscoveryArguments(session, 32, 256));
        Assert.InRange(Encoding.UTF8.GetByteCount(reply.Result.ToJsonString()), 1, 1_048_576);
        if (reply.IsError)
        {
            AssertDiscoveryError(reply, "LIMIT");
            Assert.False(reply.Content.ContainsKey("nodes"));
        }
        else
        {
            AssertTree(reply.Content, session, 32, 256);
            Assert.True(Bool(reply.Content["truncated"]) || Nodes(reply.Content).Any(node => Array(node["truncatedFields"]).Count != 0));
        }
        await controls.DispatcherBarrierAsync();
        Assert.False((await CallAsync(driver, "get_debug_state", new JsonObject { ["debugSessionId"] = session.Id })).IsError);
    }

    [Fact]
    public async Task Discovery_InvalidArgumentsAndUnknownSession_NeverReadProviderOrActOnLiveFixture()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls);
        var session = await StartSessionAsync(driver, controls);
        controls.Arm();
        foreach (var arguments in InvalidDiscoveryArguments(session))
        {
            var result = await CallAsync(driver, DiscoveryTool, arguments);
            AssertInvalidArguments(result, DiscoveryTool);
            Assert.False(controls.HasEntered, "Invalid arguments reached the live UIA provider.");
        }
        var unknown = await CallAsync(driver, DiscoveryTool, new JsonObject { ["debugSessionId"] = "unknown-session" });
        Assert.True(unknown.IsError);
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", ErrorCode(unknown.Content));
        Assert.False(controls.HasEntered);
        controls.Release();
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task GuardedInvoke_MalformedOrIncompleteExpectation_IsRejectedWithoutAction()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "readable");
        var session = await StartSessionAsync(driver, controls);
        var tree = (await ObserveAsync(driver, session, 32, 256)).Content;
        Assert.False(Bool(tree["truncated"]));
        Assert.All(Nodes(tree), node =>
        {
            Assert.True(Bool(node["childrenComplete"]));
            Assert.NotNull(node["automationId"]);
            Assert.DoesNotContain("automationId", Array(node["unavailableFields"]).Select(Text));
            Assert.DoesNotContain("automationId", Array(node["truncatedFields"]).Select(Text));
        });
        var valid = InvokeArguments(session, tree, FindAction(tree));
        foreach (var expected in InvalidExpectations(Object(valid["expectedTarget"])))
        {
            var arguments = valid.DeepClone().AsObject();
            arguments["expectedTarget"] = expected?.DeepClone();
            AssertInvalidArguments(await CallAsync(driver, "ui_invoke", arguments), "ui_invoke");
        }
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
        foreach (var length in new[] { 1, 64 })
        {
            var arguments = valid.DeepClone().AsObject();
            Object(arguments["expectedTarget"])["elementRuntimeId"] = new JsonArray(Enumerable.Range(0, length)
                .Select(index => (JsonNode?)JsonValue.Create(index % 2 == 0 ? int.MinValue : int.MaxValue)).ToArray());
            AssertRefused(await CallAsync(driver, "ui_invoke", arguments), "UI_INVOKE_TARGET_CHANGED");
        }
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task GuardedInvoke_MainWindowReplacedAfterObservation_DoesNotActOnTheNewWindow()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls);
        var session = await StartSessionAsync(driver, controls);
        var before = (await ObserveAsync(driver, session, 32, 256)).Content;
        await controls.MutateAsync("replace-window", "window-replaced");
        var after = (await ObserveAsync(driver, session, 32, 256)).Content;
        Assert.NotEqual(Text(Object(before["window"])["hwnd"]), Text(Object(after["window"])["hwnd"]));
        Assert.NotEqual(Array(Object(before["window"])["runtimeId"]).ToJsonString(), Array(Object(after["window"])["runtimeId"]).ToJsonString());
        AssertRefused(await CallAsync(driver, "ui_invoke", InvokeArguments(session, before, FindAction(before))), "UI_INVOKE_TARGET_CHANGED");
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 0);
        AssertObservedSuccess(await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, after, FindAction(after))));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Fact]
    public async Task Discovery_AbsentHelper_IsAnUnavailableErrorNotAnEmptySuccess()
    {
        using var controls = new DiscoveryFixtureControls();
        var absent = Path.Combine(RepositoryLayout.Root, "bridge", "missing-discovery-helper-" + Guid.NewGuid().ToString("N") + ".dll");
        Assert.False(File.Exists(absent));
        await using var driver = await StartDriverAsync(controls, bridgePath: absent);
        var session = await StartSessionAsync(driver, controls);
        var result = await CallAsync(driver, DiscoveryTool, DiscoveryArguments(session, 32, 256));
        AssertDiscoveryError(result, "UNAVAILABLE");
        Assert.False(controls.HasEntered);
        Assert.False((await CallAsync(driver, "get_debug_state", new JsonObject { ["debugSessionId"] = session.Id })).IsError);
        AssertInvalidArguments(await CallAsync(driver, DiscoveryTool, new JsonObject { ["debugSessionId"] = session.Id, ["maxDepth"] = -1 }), DiscoveryTool);
    }

    [Fact]
    public async Task Discovery_BoundProcessExited_DoesNotObserveAnotherLiveFixture()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls);
        var session = await StartSessionAsync(driver, controls);
        using var otherControls = new DiscoveryFixtureControls();
        await using var other = await StartDriverAsync(otherControls);
        var otherSession = await StartSessionAsync(other, otherControls);
        using var process = Process.GetProcessById(session.ProcessId);
        _ = process.SafeHandle;
        process.Kill();
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await process.WaitForExitAsync(deadline.Token);
        AssertDiscoveryError(await CallAsync(driver, DiscoveryTool, DiscoveryArguments(session, 32, 256)), "TARGET");
        AssertCounter((await ObserveAsync(other, otherSession, 32, 256)).Content, 0);
    }

    [Fact]
    public async Task UiInvoke_ExistingTwoArgumentCaller_KeepsItsSuccessShape()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls);
        var session = await StartSessionAsync(driver, controls);
        var observed = (await ObserveAsync(driver, session, 32, 256)).Content;
        var result = await CallAsync(driver, "ui_invoke", new JsonObject
        {
            ["debugSessionId"] = session.Id,
            ["automationId"] = Text(FindAction(observed)["automationId"]),
        });
        Assert.False(result.IsError, result.Result.ToJsonString());
        Assert.Equal(4, result.Content.Count);
        Assert.Equal("ui_invoke_success", Text(result.Content["kind"]));
        Assert.True(Bool(result.Content["invoked"]));
        Assert.Equal("InvokePattern", Text(result.Content["method"]));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Fact]
    public async Task UiInvoke_ExistingTwoArgumentCaller_WithUnrelatedOverlongIds_KeepsItsSuccessShapeAndInvokesOnce()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls, "selectors");
        var session = await StartSessionAsync(driver, controls);
        var observed = (await ObserveAsync(driver, session, 32, 256)).Content;
        AssertCounter(observed, 0);
        var automationId = Text(FindAction(observed)["automationId"]);

        var result = await CallAsync(driver, "ui_invoke", new JsonObject
        {
            ["debugSessionId"] = session.Id,
            ["automationId"] = automationId,
        });

        Assert.False(result.IsError, result.Result.ToJsonString());
        Assert.Equal(4, result.Content.Count);
        Assert.Equal("ui_invoke_success", Text(result.Content["kind"]));
        Assert.Equal(automationId, Text(result.Content["automationId"]));
        Assert.True(Bool(result.Content["invoked"]));
        Assert.Equal("InvokePattern", Text(result.Content["method"]));
        await controls.DispatcherBarrierAsync();
        AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
    }

    [Fact]
    public async Task Discovery_AfterRetirement_RejectsWithoutFallingBackToAnotherWindow()
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls);
        var session = await StartSessionAsync(driver, controls);
        Assert.False((await CallAsync(driver, "stop_debug", new JsonObject { ["debugSessionId"] = session.Id })).IsError);
        var retired = await CallAsync(driver, DiscoveryTool, DiscoveryArguments(session, 32, 256));
        Assert.True(retired.IsError);
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", ErrorCode(retired.Content));
        Assert.False(retired.Content.ContainsKey("nodes"));
    }

    [Theory]
    [InlineData("deadline")]
    [InlineData("cancellation")]
    [InlineData("continue")]
    [InlineData("retirement")]
    [InlineData("transport-owner-exit")]
    public async Task Discovery_BlockedProvider_KeepsDebuggerResponsiveAndCleansItsOwnedHelper(string ending)
    {
        using var controls = new DiscoveryFixtureControls();
        await using var driver = await StartDriverAsync(controls,
            lifecycleMode: ending == "continue" ? "all-stop" : "all-stop-continued-true",
            nextResponseMode: ending == "continue" ? "continue-breakpoint-terminal" : "success");
        var session = await StartSessionAsync(driver, controls);
        var initial = (await ObserveAsync(driver, session, 32, 256)).Content;
        var requestId = new RequestId("blocked-discovery");
        controls.Arm();
        var elapsed = Stopwatch.StartNew();
        var pending = driver.CallToolRawAsync(DiscoveryTool, DiscoveryArguments(session, 32, 256),
            ModernMcpProcessDriver.CurrentMeta(), requestId, timeout: TimeSpan.FromSeconds(15));
        var cleanup = new Stopwatch();
        try
        {
            await controls.AssertEnteredBeforeCompletionAsync(pending);
            using var helpers = OwnedHelperObservation.Capture(await driver.ReadAdapterProcessIdAsync());
            Assert.False(pending.IsCompleted, "The provider-entry handshake must precede a still-pending MCP operation.");
            var state = await CallAsync(driver, "get_debug_state", new JsonObject { ["debugSessionId"] = session.Id }, TimeSpan.FromSeconds(2));
            Assert.False(state.IsError, state.Result.ToJsonString());
            Assert.False(pending.IsCompleted, "A debugger read must not need to unblock UIA.");
            var busyTree = await CallAsync(driver, DiscoveryTool, DiscoveryArguments(session, 32, 256), TimeSpan.FromSeconds(2));
            AssertDiscoveryError(busyTree, "BUSY");
            var busyInvoke = await CallAsync(driver, "ui_invoke", InvokeArguments(session, initial, FindAction(initial)), TimeSpan.FromSeconds(2));
            AssertRefused(busyInvoke, "UI_INVOKE_BUSY");
            Assert.False(pending.IsCompleted);
            if (ending == "continue")
            {
                var continued = await CallAsync(driver, "continue_execution",
                    new JsonObject { ["debugSessionId"] = session.Id, ["threadId"] = 1 }, TimeSpan.FromSeconds(2));
                Assert.False(continued.IsError, continued.Result.ToJsonString());
                Assert.Equal("continue_stopped", Text(continued.Content["kind"]));
                Assert.Contains(await driver.ReadNativeActionsAsync(), action => action.Command == "continue");
                Assert.False(pending.IsCompleted, "The controlled continue-stop cycle must finish while UIA is still blocked.");
            }

            cleanup.Start();
            if (ending is "cancellation" or "continue")
            {
                await driver.Client.SendNotificationAsync(NotificationMethods.CancelledNotification,
                    new CancelledNotificationParams { RequestId = requestId }, cancellationToken: CancellationToken.None);
                await Assert.ThrowsAnyAsync<OperationCanceledException>(() => pending.WaitAsync(TimeSpan.FromSeconds(3)));
                Assert.True(pending.IsCanceled);
            }
            else if (ending == "retirement")
            {
                var stopped = await CallAsync(driver, "stop_debug", new JsonObject { ["debugSessionId"] = session.Id }, TimeSpan.FromSeconds(3));
                Assert.False(stopped.IsError, stopped.Result.ToJsonString());
            }
            else if (ending == "transport-owner-exit")
            {
                // SDK close may terminate its stdio child. This proves owned-job cleanup
                // on host loss, not graceful generic-host disposal.
                using var shutdown = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                await driver.CloseClientAsync(shutdown.Token);
                await helpers.AssertExitedAsync(TimeSpan.FromSeconds(2));
                await Record.ExceptionAsync(async () => await pending);
                return;
            }

            if (ending is not ("cancellation" or "continue"))
            {
                var raw = await pending.WaitAsync(ending == "deadline" ? OperationAndCleanupBound : TimeSpan.FromSeconds(3));
                var reply = ParseReply(raw);
                Assert.True(reply.IsError, "An unreleased provider cannot yield a complete observation.");
                Assert.False(reply.Content.ContainsKey("nodes"));
                if (ending == "deadline")
                {
                    AssertDiscoveryError(reply, "TIMEOUT");
                    Assert.True(elapsed.Elapsed <= OperationAndCleanupBound, $"Discovery exceeded its single deadline plus cleanup bound: {elapsed.Elapsed}.");
                }
                else
                {
                    Assert.True(ErrorCode(reply.Content) == "DEBUG_SESSION_NOT_FOUND" ||
                        new[] { "TARGET", "CANCEL", "SESSION" }.Any(category => ErrorCode(reply.Content).Contains(category, StringComparison.Ordinal)), reply.Result.ToJsonString());
                }
            }
            var remaining = ending == "deadline" ? OperationAndCleanupBound - elapsed.Elapsed : TimeSpan.FromSeconds(2) - cleanup.Elapsed;
            Assert.True(remaining > TimeSpan.Zero, "The operation exhausted its bound before owned-helper cleanup was confirmed.");
            await helpers.AssertExitedAsync(remaining);
        }
        finally
        {
            controls.Release();
        }
        if (ending is "deadline" or "cancellation" or "continue")
        {
            if (ending is "cancellation" or "continue")
                await WaitForDiscoveryAdmissionAsync(driver, session, cleanup);
            await controls.DispatcherBarrierAsync();
            var recovered = (await ObserveAsync(driver, session, 32, 256)).Content;
            AssertCounter(recovered, 0);
            AssertObservedSuccess(await CallAsync(driver, "ui_invoke_observed", ObservedInvokeArguments(session, recovered, FindAction(recovered))));
            await controls.DispatcherBarrierAsync();
            AssertCounter((await ObserveAsync(driver, session, 32, 256)).Content, 1);
        }
    }

    private static IEnumerable<JsonObject?> InvalidDiscoveryArguments(BoundSession session)
    {
        yield return null;
        yield return new JsonObject();
        foreach (var value in new JsonNode?[] { null, JsonValue.Create(1), JsonValue.Create(true), JsonValue.Create(""), JsonValue.Create(" \t"), new JsonArray() })
            yield return new JsonObject { ["debugSessionId"] = value?.DeepClone() };
        foreach (var field in new[] { "maxDepth", "maxChildren" })
        {
            foreach (var value in new JsonNode?[] { null, JsonValue.Create("3"), JsonValue.Create(true), JsonValue.Create(1.5), JsonValue.Create(-1), JsonValue.Create(int.MaxValue), new JsonObject(), new JsonArray() })
            {
                var arguments = DiscoveryArguments(session, 3, 50);
                arguments[field] = value?.DeepClone();
                yield return arguments;
            }
        }
        foreach (var (field, value) in new[] { ("maxDepth", 33), ("maxChildren", 0), ("maxChildren", 257) })
        {
            var arguments = DiscoveryArguments(session, 3, 50);
            arguments[field] = value;
            yield return arguments;
        }
        foreach (var field in new[] { "processId", "hwnd", "window", "unexpected" })
        {
            var arguments = DiscoveryArguments(session, 3, 50);
            arguments[field] = 123;
            yield return arguments;
        }
        // Even a small JSON character count can exceed the UTF-8 request byte bound.
        yield return new JsonObject { ["debugSessionId"] = new string('\u0800', 8192 / 3 + 1) };
    }

    private static IEnumerable<JsonNode?> InvalidExpectations(JsonObject valid)
    {
        yield return null;
        yield return JsonValue.Create("not-an-object");
        yield return new JsonArray();
        yield return new JsonObject();
        foreach (var field in valid.Select(item => item.Key))
        {
            var missing = valid.DeepClone().AsObject();
            missing.Remove(field);
            yield return missing;
            var nullMember = valid.DeepClone().AsObject();
            nullMember[field] = null;
            yield return nullMember;
        }
        var extra = valid.DeepClone().AsObject();
        extra["processId"] = 123;
        yield return extra;
        foreach (var field in new[] { "processIdentity", "hwnd" })
        {
            foreach (var value in new JsonNode?[] { JsonValue.Create(""), JsonValue.Create(" \t"), JsonValue.Create(42), JsonValue.Create(true) })
            {
                var malformed = valid.DeepClone().AsObject();
                malformed[field] = value;
                yield return malformed;
            }
        }
        foreach (var field in new[] { "rootRuntimeId", "elementRuntimeId" })
        {
            foreach (var value in new JsonNode?[]
            {
                JsonValue.Create("1,2"), new JsonArray(), new JsonArray(1, null), new JsonArray(1.5),
                new JsonArray(true), new JsonArray("1"), new JsonArray(2147483648L), new JsonArray(-2147483649L),
                new JsonArray(Enumerable.Range(0, 65).Select(number => (JsonNode?)JsonValue.Create(number)).ToArray()),
            })
            {
                var malformed = valid.DeepClone().AsObject();
                malformed[field] = value;
                yield return malformed;
            }
        }
    }

    private static IEnumerable<JsonObject?> InvalidObservedInvokeArguments(JsonObject valid)
    {
        yield return null;
        yield return new JsonObject();
        foreach (var field in new[] { "debugSessionId", "target", "expectedAutomationId" })
        {
            var missing = valid.DeepClone().AsObject();
            missing.Remove(field);
            yield return missing;
        }
        foreach (var target in InvalidExpectations(Object(valid["target"])))
        {
            var malformed = valid.DeepClone().AsObject();
            malformed["target"] = target?.DeepClone();
            yield return malformed;
        }
        foreach (var field in new[] { "debugSessionId", "expectedAutomationId" })
        {
            foreach (var value in new JsonNode?[]
            {
                null, JsonValue.Create(""), JsonValue.Create(" \t"), JsonValue.Create(true),
                JsonValue.Create(42), new JsonArray(), new JsonObject(),
            })
            {
                var malformed = valid.DeepClone().AsObject();
                malformed[field] = value?.DeepClone();
                yield return malformed;
            }
        }
        foreach (var value in new[] { new string('x', 257), string.Concat(Enumerable.Repeat("\U0001F600", 257)) })
        {
            var overlong = valid.DeepClone().AsObject();
            overlong["expectedAutomationId"] = value;
            yield return overlong;
        }
        foreach (var field in new[] { "unexpected", "automationId", "expectedTarget", "processId", "hwnd" })
        {
            var extra = valid.DeepClone().AsObject();
            extra[field] = 123;
            yield return extra;
        }
    }

    private static Task<ModernMcpProcessDriver> StartDriverAsync(DiscoveryFixtureControls controls, string mode = "basic",
        string? bridgePath = null, string lifecycleMode = "all-stop-continued-true", string nextResponseMode = "success") =>
        ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(
                LifecycleMode: lifecycleMode,
                NextResponseMode: nextResponseMode,
                SpawnWindowedDescendant: true,
                WindowedDescendantExecutablePath: ResolveFixtureExecutable(),
                WindowedDescendantArguments:
                [
                    "--native-scene-probe-test-harness", "--native-scene-probe-mode=stable",
                    $"--native-ui-discovery-mode={mode}", $"--native-ui-discovery-events={controls.Prefix}",
                ]),
            AdditionalEnvironment: new Dictionary<string, string?> { ["FLAUI_BRIDGE_PATH"] = bridgePath ?? ResolveBridge() }),
            startupTimeout: TimeSpan.FromSeconds(30));

    private static async Task<BoundSession> StartSessionAsync(ModernMcpProcessDriver driver, DiscoveryFixtureControls controls)
    {
        var start = await CallAsync(driver, "start_debug", new JsonObject { ["program"] = ResolveFixtureExecutable() });
        Assert.False(start.IsError, start.Result.ToJsonString());
        var id = Text(start.Content["debugSessionId"]);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await driver.WaitForFixtureEventAsync("process", deadline.Token);
        var barrier = await CallAsync(driver, "get_threads", new JsonObject { ["debugSessionId"] = id });
        Assert.False(barrier.IsError, barrier.Result.ToJsonString());
        var processId = await driver.ReadDescendantProcessIdAsync();
        await controls.WaitForReadyAsync(processId);
        using var process = Process.GetProcessById(processId);
        process.Refresh();
        Assert.NotEqual(IntPtr.Zero, process.MainWindowHandle);
        return new BoundSession(id, processId);
    }

    private static string ResolveFixtureExecutable()
    {
        var configuration = new DirectoryInfo(AppContext.BaseDirectory).Parent!.Name;
        var path = Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless.Tests", "Fixtures", "NativeSceneProbe.WpfFixture", "bin", configuration, "net8.0-windows", "NativeSceneProbe.WpfFixture.exe");
        Assert.True(File.Exists(path), $"Built controlled WPF fixture is absent: {path}");
        return path;
    }

    private static string ResolveBridge()
    {
        var configuration = new DirectoryInfo(AppContext.BaseDirectory).Parent!.Name;
        var directory = Path.Combine(RepositoryLayout.Root, "bridge", "bin", configuration, "net8.0-windows");
        return new[] { Path.Combine(directory, "win-x64", "FlaUIBridge.dll"), Path.Combine(directory, "FlaUIBridge.dll") }
            .FirstOrDefault(File.Exists) ?? throw new InvalidOperationException("Built FlaUI bridge is absent.");
    }

    private static JsonObject DiscoveryArguments(BoundSession session, int maxDepth, int maxChildren) => new()
    {
        ["debugSessionId"] = session.Id,
        ["maxDepth"] = maxDepth,
        ["maxChildren"] = maxChildren,
    };

    private static async Task<Reply> ObserveAsync(ModernMcpProcessDriver driver, BoundSession session, int maxDepth, int maxChildren)
    {
        var result = await CallAsync(driver, DiscoveryTool, DiscoveryArguments(session, maxDepth, maxChildren));
        Assert.False(result.IsError, $"The public native discovery route must return an observation: {result.Result.ToJsonString()}");
        return result;
    }

    private static async Task WaitForDiscoveryAdmissionAsync(ModernMcpProcessDriver driver, BoundSession session, Stopwatch cleanup)
    {
        var cleanupBound = TimeSpan.FromSeconds(2);
        var lastBusy = "<no probe>";
        while (true)
        {
            var sentAt = cleanup.Elapsed;
            Assert.True(sentAt < cleanupBound,
                $"Discovery admission did not recover within the original cleanup bound; elapsed={sentAt}; last BUSY={lastBusy}.");
            // Only BUSY polling uses the original cleanup clock. An admitted read
            // retains its own operation deadline; response time is not admission time.
            var probe = await CallAsync(driver, DiscoveryTool, DiscoveryArguments(session, 0, 1));
            if (!probe.IsError)
            {
                Assert.Equal("ui_window_tree", Text(probe.Content["kind"]));
                return;
            }

            Assert.True(ErrorCode(probe.Content) == "UI_DISCOVERY_BUSY", probe.Result.ToJsonString());
            lastBusy = $"sent={sentAt}, received={cleanup.Elapsed}";
            var remaining = cleanupBound - cleanup.Elapsed;
            Assert.True(remaining > TimeSpan.Zero,
                $"Discovery still returned UI_DISCOVERY_BUSY after the original cleanup bound; {lastBusy}.");
            // Throttle condition polling; elapsed time alone never establishes availability.
            var interval = TimeSpan.FromMilliseconds(25);
            await Task.Delay(remaining < interval ? remaining : interval);
        }
    }

    private static JsonObject InvokeArguments(BoundSession session, JsonObject tree, JsonObject node) => new()
    {
        ["debugSessionId"] = session.Id,
        ["automationId"] = Text(node["automationId"]),
        ["expectedTarget"] = ExpectedTarget(tree, node),
    };

    private static JsonObject ObservedInvokeArguments(BoundSession session, JsonObject tree, JsonObject node) => new()
    {
        ["debugSessionId"] = session.Id,
        ["target"] = ExpectedTarget(tree, node),
        ["expectedAutomationId"] = Text(node["automationId"]),
    };

    private static JsonObject ExpectedTarget(JsonObject tree, JsonObject node) => new()
    {
        ["processIdentity"] = Object(tree["process"])["processIdentity"]!.DeepClone(),
        ["hwnd"] = Object(tree["window"])["hwnd"]!.DeepClone(),
        ["rootRuntimeId"] = Object(tree["window"])["runtimeId"]!.DeepClone(),
        ["elementRuntimeId"] = node["runtimeId"]!.DeepClone(),
    };

    private static async Task<Reply> CallAsync(ModernMcpProcessDriver driver, string tool, JsonObject? arguments, TimeSpan? timeout = null) =>
        ParseReply(await driver.CallToolRawAsync(tool, arguments, ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(Guid.NewGuid().ToString("N")), timeout: timeout ?? TimeSpan.FromSeconds(15)));

    private static Reply ParseReply(JsonRpcResponse response)
    {
        Assert.True(response.Result is JsonObject, $"Expected a complete public MCP tool result, not an absent route or RPC error: {response}");
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.InRange(Encoding.UTF8.GetByteCount(result.ToJsonString()), 1, 1_048_576);
        Assert.Equal("complete", Text(result["resultType"]));
        var content = Object(result["structuredContent"]);
        var mirror = Object(Assert.Single(Array(result["content"])));
        Assert.Equal("text", Text(mirror["type"]));
        Assert.Equal(content.ToJsonString(), Text(mirror["text"]));
        return new Reply(result, content, result["isError"]?.GetValue<bool>() ?? false);
    }

    private static void AssertTree(JsonObject tree, BoundSession session, int maxDepth, int maxChildren)
    {
        Assert.Equal("ui_window_tree", Text(tree["kind"]));
        Assert.Equal("main_window", Text(tree["scope"]));
        Assert.Equal("unproven", Text(tree["atomicity"]));
        var process = Object(tree["process"]);
        Assert.Equal(session.ProcessId, Int(process["processId"]));
        Assert.False(string.IsNullOrWhiteSpace(Text(process["processIdentity"])));
        var window = Object(tree["window"]);
        Assert.False(string.IsNullOrWhiteSpace(Text(window["hwnd"])));
        AssertRuntimeId(window["runtimeId"]);
        var limits = Object(tree["limits"]);
        Assert.Equal(maxDepth, Int(limits["maxDepth"]));
        Assert.Equal(maxChildren, Int(limits["maxChildren"]));
        Assert.Equal(4096, Int(limits["maxNodes"]));
        var nodes = Nodes(tree).ToArray();
        Assert.InRange(nodes.Length, 1, 4096);
        var depths = new Dictionary<int, int>();
        var childCounts = new Dictionary<int, int>();
        var ancestors = new List<int>();
        for (var index = 0; index < nodes.Length; index++)
        {
            var node = nodes[index];
            Assert.Equal(index, Int(node["nodeId"]));
            if (index == 0)
            {
                Assert.Null(node["parentId"]);
                depths[index] = 0;
                ancestors.Add(index);
                Assert.Equal(Array(window["runtimeId"]).ToJsonString(), Array(node["runtimeId"]).ToJsonString());
            }
            else
            {
                var parent = Int(node["parentId"]);
                Assert.InRange(parent, 0, index - 1);
                Assert.Contains(parent, ancestors);
                ancestors.RemoveRange(ancestors.IndexOf(parent) + 1, ancestors.Count - ancestors.IndexOf(parent) - 1);
                ancestors.Add(index);
                depths[index] = depths[parent] + 1;
                childCounts[parent] = childCounts.GetValueOrDefault(parent) + 1;
                Assert.InRange(childCounts[parent], 1, maxChildren);
            }
            Assert.InRange(depths[index], 0, maxDepth);
            var unavailable = Array(node["unavailableFields"]).Select(Text).ToArray();
            var truncated = Array(node["truncatedFields"]).Select(Text).ToArray();
            foreach (var field in new[] { "automationId", "name", "controlType", "runtimeId", "supportsInvoke" })
            {
                Assert.True(node.ContainsKey(field), $"Node {index} omits {field}.");
                if (node[field] is null)
                    Assert.Contains(field, unavailable.Concat(truncated));
            }
            if (node["automationId"] is not null)
                Assert.InRange(Text(node["automationId"]).EnumerateRunes().Count(), 0, 256);
            if (node["runtimeId"] is not null)
                AssertRuntimeId(node["runtimeId"]);
            if (node["supportsInvoke"] is not null)
                _ = Bool(node["supportsInvoke"]);
            _ = Bool(node["childrenComplete"]);
        }
        if (nodes.Any(node => !Bool(node["childrenComplete"])))
            Assert.True(Bool(tree["truncated"]));
        StructuredContentSchemaParityTests.AssertMatchesSchema(tree);
    }

    private static void AssertCounter(JsonObject tree, int value) =>
        Assert.Single(Nodes(tree), node => node["name"]?.GetValue<string>() == $"Controlled discovery count: {value}");

    private static JsonObject RequireSingleNamedNode(JsonObject tree, string name, string controlType)
    {
        var namedMatches = Nodes(tree).Where(node => node["name"]?.GetValue<string>() == name).ToArray();
        var matches = namedMatches.Where(node => node["controlType"]?.GetValue<string>() == controlType).ToArray();
        if (matches.Length != 1)
        {
            static string Field(JsonNode? value)
            {
                var text = value?.ToJsonString() ?? "null";
                return text.Length <= 128 ? text : text[..128] + "...";
            }

            var details = string.Join("; ", namedMatches.Take(8).Select(node =>
                $"nodeId={Field(node["nodeId"])}, parentId={Field(node["parentId"])}, controlType={Field(node["controlType"])}"));
            Assert.Fail($"Expected exactly one '{controlType}' node named '{name}', found {matches.Length}; same-name count={namedMatches.Length}; first eight same-name matches: {details}.");
        }

        return matches[0];
    }

    private static JsonObject FindAction(JsonObject tree)
    {
        var action = Assert.Single(Nodes(tree), node => node["name"]?.GetValue<string>() == ActionName &&
            node["controlType"]?.GetValue<string>()?.EndsWith("Button", StringComparison.Ordinal) == true);
        Assert.True(Bool(action["supportsInvoke"]));
        Assert.False(string.IsNullOrEmpty(Text(action["automationId"])));
        AssertRuntimeId(action["runtimeId"]);
        return action;
    }

    private static void AssertRuntimeId(JsonNode? node)
    {
        var values = Array(node);
        Assert.InRange(values.Count, 1, 64);
        foreach (var value in values)
            _ = Int(value);
    }

    private static void AssertInvalidArguments(Reply reply, string tool)
    {
        Assert.True(reply.IsError, reply.Result.ToJsonString());
        Assert.Equal("invalid_tool_arguments", Text(reply.Content["kind"]));
        Assert.Equal("INVALID_TOOL_ARGUMENTS", ErrorCode(reply.Content));
        Assert.Equal(tool, Text(reply.Content["tool"]));
    }

    private static void AssertObservedSuccess(Reply reply)
    {
        Assert.False(reply.IsError, reply.Result.ToJsonString());
        Assert.Equal("ui_invoke_observed_success", Text(reply.Content["kind"]));
        Assert.Equal("runtime_id", Text(reply.Content["selection"]));
        Assert.True(Bool(reply.Content["invoked"]));
        Assert.Equal("InvokePattern", Text(reply.Content["method"]));
        Assert.Equal("unproven", Text(reply.Content["atomicity"]));
    }

    private static void AssertObservedTargetChanged(Reply reply)
    {
        Assert.True(reply.IsError, reply.Result.ToJsonString());
        Assert.Equal("tool_error", Text(reply.Content["kind"]));
        Assert.Equal("ui_invoke_observed", Text(reply.Content["tool"]));
        Assert.Contains("TARGET_CHANGED", ErrorCode(reply.Content), StringComparison.Ordinal);
        Assert.Equal("not_invoked", Text(reply.Content["outcome"]));
        Assert.False(reply.Content.ContainsKey("invoked"));
    }

    private static void AssertRefused(Reply reply, string code)
    {
        Assert.True(reply.IsError, reply.Result.ToJsonString());
        Assert.Equal("tool_error", Text(reply.Content["kind"]));
        Assert.Equal("ui_invoke", Text(reply.Content["tool"]));
        Assert.Equal(code, ErrorCode(reply.Content));
        Assert.Equal("not_invoked", Text(reply.Content["outcome"]));
        Assert.False(reply.Content.ContainsKey("invoked"));
    }

    private static void AssertDiscoveryError(Reply reply, string category)
    {
        Assert.True(reply.IsError, reply.Result.ToJsonString());
        Assert.Equal(DiscoveryTool, Text(reply.Content["tool"]));
        var code = ErrorCode(reply.Content);
        Assert.True(code.Contains(category, StringComparison.OrdinalIgnoreCase) ||
            category == "TIMEOUT" && code.Contains("TIMED_OUT", StringComparison.OrdinalIgnoreCase), reply.Result.ToJsonString());
        Assert.False(reply.Content.ContainsKey("nodes"));
    }

    private static void AssertIntegerRange(JsonObject schema, int minimum, int maximum, int defaultValue)
    {
        Assert.Equal("integer", Text(schema["type"]));
        Assert.Equal(minimum, Int(schema["minimum"]));
        Assert.Equal(maximum, Int(schema["maximum"]));
        Assert.Equal(defaultValue, Int(schema["default"]));
    }

    private static IEnumerable<JsonObject> Nodes(JsonObject tree) => Array(tree["nodes"]).Select(Object);
    private static string ErrorCode(JsonObject content) => Text(content["code"] ?? content["error"]);
    private static JsonObject Object(JsonNode? node) => Assert.IsType<JsonObject>(node);
    private static JsonArray Array(JsonNode? node) => Assert.IsType<JsonArray>(node);
    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();
    private static int Int(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<int>();
    private static bool Bool(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<bool>();
    private sealed record BoundSession(string Id, int ProcessId);
    private sealed record Reply(JsonObject Result, JsonObject Content, bool IsError);

    [DllImport("user32.dll", ExactSpelling = true)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    private static extern IntPtr GetForegroundWindow();
}

internal sealed class DiscoveryFixtureControls : IDisposable
{
    private readonly Dictionary<string, EventWaitHandle> _events = new(StringComparer.Ordinal);
    internal string Prefix { get; } = "Local\\native-ui-discovery-" + Guid.NewGuid().ToString("N");

    internal DiscoveryFixtureControls()
    {
        foreach (var name in new[] { "ready", "scene-setup-complete", "window-loaded", "arm", "entered", "release", "replace", "replaced", "duplicate", "duplicated", "barrier", "barrier-complete", "replace-window", "window-replaced", "rename-action", "action-renamed" })
            _events.Add(name, new EventWaitHandle(false, name is "barrier" or "barrier-complete" ? EventResetMode.AutoReset : EventResetMode.ManualReset, Prefix + "-" + name));
    }

    internal bool HasEntered => _events["entered"].WaitOne(0);
    internal void Arm() => _events["arm"].Set();
    internal void Release()
    {
        _events["arm"].Reset();
        _events["release"].Set();
    }

    internal async Task WaitAsync(string name)
    {
        Assert.True(await Task.Run(() => _events[name].WaitOne(TimeSpan.FromSeconds(5))), $"Controlled WPF handshake '{name}' was not observed.");
    }

    internal async Task WaitForReadyAsync(int fixtureProcessId)
    {
        Process? fixture = null;
        string? probeError = null;
        try
        {
            try
            {
                fixture = Process.GetProcessById(fixtureProcessId);
                _ = fixture.SafeHandle;
            }
            catch (Exception exception) when (exception is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception)
            {
                fixture?.Dispose();
                fixture = null;
                probeError = exception.GetType().Name;
            }

            var ready = await Task.Run(() => _events["ready"].WaitOne(TimeSpan.FromSeconds(5)));
            if (ready)
                return;

            var alive = "unknown";
            var hwnd = "unknown";
            if (fixture is not null)
            {
                try
                {
                    alive = fixture.HasExited ? "false" : "true";
                    fixture.Refresh();
                    var handle = fixture.MainWindowHandle;
                    hwnd = handle == IntPtr.Zero ? "not_observed" : $"0x{handle.ToInt64():X}";
                }
                catch (Exception exception) when (exception is ArgumentException or InvalidOperationException or System.ComponentModel.Win32Exception)
                {
                    probeError = exception.GetType().Name;
                }
            }

            Assert.Fail($"Controlled WPF handshake 'ready' was not observed within 5 seconds. " +
                $"Diagnostic snapshot: scene_setup_complete={_events["scene-setup-complete"].WaitOne(0)}; " +
                $"window_loaded={_events["window-loaded"].WaitOne(0)}; content_rendered_ready={_events["ready"].WaitOne(0)}; " +
                $"owned_fixture_pid={fixtureProcessId}; process_alive={alive}; main_hwnd={hwnd}; process_probe_error={probeError ?? "none"}.");
        }
        finally
        {
            fixture?.Dispose();
        }
    }

    internal async Task AssertEnteredBeforeCompletionAsync(Task pending)
    {
        using var finished = new ManualResetEvent(false);
        var entered = Task.Run(() => WaitHandle.WaitAny([_events["entered"], finished], TimeSpan.FromSeconds(5)) == 0);
        var first = await Task.WhenAny(entered, pending);
        if (first == pending)
            finished.Set();
        var observedEntry = await entered;
        Assert.True(first == entered && observedEntry,
            "Discovery completed or failed before entering the controlled UIA provider; this is not blocked-helper evidence.");
    }

    internal async Task MutateAsync(string request, string completed)
    {
        _events[request].Set();
        await WaitAsync(completed);
    }

    internal Task DispatcherBarrierAsync() => MutateAsync("barrier", "barrier-complete");

    public void Dispose()
    {
        Release();
        foreach (var handle in _events.Values)
            handle.Dispose();
    }
}

// Observe only direct children of the exact host identified by its controlled adapter.
// Holding Process handles prevents cleanup evidence from accidentally following a reused PID.
internal sealed class OwnedHelperObservation : IDisposable
{
    private readonly Process[] _helpers;
    private OwnedHelperObservation(Process[] helpers) => _helpers = helpers;

    internal static OwnedHelperObservation Capture(int adapterProcessId)
    {
        var parents = ReadParents();
        Assert.True(parents.TryGetValue(adapterProcessId, out var hostId), "Controlled adapter has no observable owning host.");
        var ids = parents.Where(pair => pair.Value == hostId && pair.Key != adapterProcessId).Select(pair => pair.Key).ToArray();
        Assert.Single(ids);
        var helpers = new List<Process>();
        try
        {
            foreach (var id in ids)
            {
                var process = Process.GetProcessById(id);
                helpers.Add(process);
                _ = process.SafeHandle;
                Assert.False(process.HasExited);
            }
            return new OwnedHelperObservation(helpers.ToArray());
        }
        catch
        {
            foreach (var process in helpers)
                process.Dispose();
            throw;
        }
    }

    internal static HashSet<int> CaptureDirectChildIds(int parentProcessId) =>
        ReadParents().Where(pair => pair.Value == parentProcessId).Select(pair => pair.Key).ToHashSet();

    internal static Process CaptureNewBridgeHelper(int parentProcessId, IReadOnlySet<int> previousChildren, string bridgeExecutable)
    {
        var id = Assert.Single(CaptureDirectChildIds(parentProcessId).Except(previousChildren));
        var process = Process.GetProcessById(id);
        try
        {
            _ = process.SafeHandle;
            Assert.False(process.HasExited, "The exact helper must still be alive in its post-ready authorization callback.");
            var image = process.MainModule?.FileName;
            Assert.NotNull(image);
            Assert.True(string.Equals(Path.GetFullPath(bridgeExecutable), Path.GetFullPath(image), StringComparison.OrdinalIgnoreCase),
                "The new direct child is not the exact candidate bridge apphost.");
            return process;
        }
        catch
        {
            process.Dispose();
            throw;
        }
    }

    internal async Task AssertExitedAsync(TimeSpan timeout)
    {
        using var deadline = new CancellationTokenSource(timeout);
        foreach (var helper in _helpers)
            await helper.WaitForExitAsync(deadline.Token);
        Assert.All(_helpers, helper => Assert.True(helper.HasExited, "The host-owned UI helper survived operation cleanup."));
    }

    public void Dispose()
    {
        foreach (var process in _helpers)
            process.Dispose();
    }

    private static Dictionary<int, int> ReadParents()
    {
        using var snapshot = CreateToolhelp32Snapshot(2, 0);
        Assert.False(snapshot.IsInvalid);
        var entry = new ProcessEntry { Size = (uint)Marshal.SizeOf<ProcessEntry>(), ExeFile = string.Empty };
        var result = new Dictionary<int, int>();
        Assert.True(Process32FirstW(snapshot, ref entry));
        do
        {
            result[checked((int)entry.ProcessId)] = checked((int)entry.ParentProcessId);
        } while (Process32NextW(snapshot, ref entry));
        Assert.Equal(18, Marshal.GetLastWin32Error()); // ERROR_NO_MORE_FILES, not an incomplete snapshot.
        return result;
    }

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct ProcessEntry
    {
        public uint Size;
        public uint Usage;
        public uint ProcessId;
        public UIntPtr DefaultHeapId;
        public uint ModuleId;
        public uint Threads;
        public uint ParentProcessId;
        public int Priority;
        public uint Flags;
        [MarshalAs(UnmanagedType.ByValTStr, SizeConst = 260)]
        public string ExeFile;
    }

    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    private static extern SafeFileHandle CreateToolhelp32Snapshot(uint flags, uint processId);
    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32FirstW(SafeFileHandle snapshot, ref ProcessEntry entry);
    [DllImport("kernel32.dll", ExactSpelling = true, SetLastError = true)]
    [DefaultDllImportSearchPaths(DllImportSearchPath.System32)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool Process32NextW(SafeFileHandle snapshot, ref ProcessEntry entry);
}
