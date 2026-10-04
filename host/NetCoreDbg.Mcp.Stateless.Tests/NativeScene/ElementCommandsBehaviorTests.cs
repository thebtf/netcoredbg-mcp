using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;
using Xunit.Abstractions;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class ElementCommandsBehaviorTests
{
    private static readonly TimeSpan StartupTimeout = TimeSpan.FromSeconds(45);
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan CleanupTimeout = TimeSpan.FromSeconds(5);
    private readonly ITestOutputHelper _output;

    public ElementCommandsBehaviorTests(ITestOutputHelper output) => _output = output;

    private enum BridgeFixture
    {
        NativeSceneProbe,
        WpfSmokeApp,
    }

    [Fact]
    public async Task ExtractText_AutomationIdWinsOverConflictingXPathAndName()
    {
        var response = await RunExtractTextAsync(new JsonObject
        {
            ["automationId"] = "SaveButton",
            ["xpath"] = "//Button[@Name='Ambiguous action one']",
            ["name"] = "Ambiguous action two",
            ["controlType"] = "Button",
        });

        AssertText(response, "Save scene");
    }

    [Fact]
    public async Task ExtractText_AutomationIdMissFallsBackToXPathBeforeName()
    {
        var response = await RunExtractTextAsync(new JsonObject
        {
            ["automationId"] = "MissingButton",
            ["xpath"] = "//Button[@AutomationId='SaveButton']",
            ["name"] = "Ambiguous action one",
            ["controlType"] = "Button",
        });

        AssertText(response, "Save scene");
    }

    [Fact]
    public async Task ExtractText_AutomationIdAndXPathMissesFallBackToNameAndControlType()
    {
        var response = await RunExtractTextAsync(new JsonObject
        {
            ["automationId"] = "MissingButton",
            ["xpath"] = "//Button[@AutomationId='MissingButton']",
            ["name"] = "Save scene",
            ["controlType"] = "Button",
        });

        AssertText(response, "Save scene");
    }

    [Fact]
    public async Task ExtractText_NameFallbackDoesNotIgnoreConflictingControlType()
    {
        var response = await RunExtractTextAsync(new JsonObject
        {
            ["name"] = "Save scene",
            ["controlType"] = "Edit",
        });

        AssertError(response, "Element not found.");
    }

    [Theory]
    [InlineData("Gallery")]
    [InlineData("NativeSceneProbeWindow")]
    [InlineData("Native Scene Probe Fixture")]
    public async Task ExtractText_ExactDescendantOrWindowRootFindsVisibleText(string root)
    {
        var response = await RunExtractTextAsync(new JsonObject
        {
            ["rootAutomationId"] = root,
            ["automationId"] = "SaveButton",
        });

        AssertText(response, "Save scene");
    }

    [Fact]
    public async Task ExtractText_RootIdentityPrefixIsRejectedInsteadOfSearchingTheMainWindow()
    {
        var response = await RunExtractTextAsync(new JsonObject
        {
            ["rootAutomationId"] = "Galler",
            ["automationId"] = "SaveButton",
        });

        AssertError(response, "Root element not found: 'Galler'.");
    }

    [Fact]
    public async Task ExtractText_DescendantRootDoesNotEscapeToAnOutsideElement()
    {
        var response = await RunExtractTextAsync(new JsonObject
        {
            ["rootAutomationId"] = "Gallery",
            ["automationId"] = "SceneHeading",
        });

        AssertError(response, "Element not found.");
    }

    [Theory]
    [InlineData("NativeSceneProbeWindow", "2 top-level windows match")]
    [InlineData("Gallery", "found in 2 windows")]
    public async Task ExtractText_MatchingRootsInTwoTopLevelWindowsAreRejected(string root, string ambiguity)
    {
        var response = await RunExtractTextAsync(new JsonObject
        {
            ["rootAutomationId"] = root,
            ["automationId"] = "SaveButton",
        }, "bridge-elements-ambiguous-roots");

        AssertError(response, ambiguity);
    }

    [Fact]
    public async Task FindElement_GalleryMissReportsActualScopeAndSameConnectionFindsSaveButton()
    {
        await RunBridgeAsync(async (bridge, processId, requestId) =>
        {
            var missing = await CallBridgeAsync(bridge, "find_element", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["automationId"] = "MissingButton",
                ["controlType"] = "Button",
            }, requestId++);
            Assert.False(missing.ContainsKey("error"), missing.ToJsonString());
            var miss = Assert.IsType<JsonObject>(missing["result"]);
            Assert.False(miss["found"]!.GetValue<bool>());
            Assert.Equal("Gallery fixture root", miss["searchRootName"]!.GetValue<string>());
            Assert.Equal("Gallery", miss["searchRootAutomationId"]!.GetValue<string>());
            Assert.False(miss["searchRootOffscreen"]!.GetValue<bool>());
            Assert.Equal(processId, miss["processId"]!.GetValue<int>());
            Assert.Equal(1, miss["topLevelWindowCount"]!.GetValue<int>());
            Assert.False(miss.ContainsKey("automationId"));
            Assert.False(miss.ContainsKey("name"));
            Assert.False(miss.ContainsKey("controlType"));
            Assert.False(miss.ContainsKey("rect"));

            var found = await CallBridgeAsync(bridge, "find_element", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["automationId"] = "SaveButton",
                ["controlType"] = "Button",
            }, requestId++);
            Assert.False(found.ContainsKey("error"), found.ToJsonString());
            var match = Assert.IsType<JsonObject>(found["result"]);
            Assert.True(match["found"]!.GetValue<bool>());
            Assert.Equal("SaveButton", match["automationId"]!.GetValue<string>());
            Assert.Equal("Save scene", match["name"]!.GetValue<string>());
            Assert.Equal("Button", match["controlType"]!.GetValue<string>());

            var outside = await CallBridgeAsync(bridge, "find_element", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["automationId"] = "SceneHeading",
            }, requestId);
            Assert.False(outside.ContainsKey("error"), outside.ToJsonString());
            var outsideMiss = Assert.IsType<JsonObject>(outside["result"]);
            Assert.True(JsonNode.DeepEquals(miss, outsideMiss), outside.ToJsonString());
            return found;
        });
    }

    [Fact]
    public async Task FindByXPath_GalleryAmbiguityThenUniqueAndMissesStayScoped()
    {
        await RunBridgeAsync(async (bridge, _, requestId) =>
        {
            var ambiguous = await CallBridgeAsync(bridge, "find_by_xpath", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["xpath"] = "//Button[@AutomationId='AmbiguousButton']",
            }, requestId++);
            Assert.False(ambiguous.ContainsKey("error"), ambiguous.ToJsonString());
            var first = Assert.IsType<JsonObject>(ambiguous["result"]);
            Assert.True(first["found"]!.GetValue<bool>());
            Assert.Equal(2, first["matchCount"]!.GetValue<int>());
            Assert.Equal("Ambiguous action one", first["name"]!.GetValue<string>());
            Assert.Equal("AmbiguousButton", first["automationId"]!.GetValue<string>());
            Assert.Equal("Button", first["controlType"]!.GetValue<string>());
            Assert.Equal("XPath matched 2 elements; returning first. Use more specific XPath to avoid ambiguity.",
                first["warning"]!.GetValue<string>());
            var bounds = Assert.IsType<JsonObject>(first["rect"]);
            Assert.Equal(4, bounds.Count);
            foreach (var coordinate in new[] { "x", "y", "width", "height" })
            {
                Assert.True(double.IsFinite(bounds[coordinate]!.GetValue<double>()), bounds.ToJsonString());
            }
            Assert.True(bounds["width"]!.GetValue<double>() > 0, bounds.ToJsonString());
            Assert.True(bounds["height"]!.GetValue<double>() > 0, bounds.ToJsonString());

            var unique = await CallBridgeAsync(bridge, "find_by_xpath", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["xpath"] = "//Button[@AutomationId='SaveButton']",
            }, requestId++);
            Assert.False(unique.ContainsKey("error"), unique.ToJsonString());
            var save = Assert.IsType<JsonObject>(unique["result"]);
            Assert.True(save["found"]!.GetValue<bool>());
            Assert.Equal(1, save["matchCount"]!.GetValue<int>());
            Assert.Equal("Save scene", save["name"]!.GetValue<string>());
            Assert.Equal("SaveButton", save["automationId"]!.GetValue<string>());
            Assert.Equal("Button", save["controlType"]!.GetValue<string>());
            Assert.False(save.ContainsKey("warning"), unique.ToJsonString());

            const string missingXPath = "//Button[@AutomationId='MissingButton']";
            var missing = await CallBridgeAsync(bridge, "find_by_xpath", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["xpath"] = missingXPath,
            }, requestId++);
            Assert.False(missing.ContainsKey("error"), missing.ToJsonString());
            var miss = Assert.IsType<JsonObject>(missing["result"]);
            Assert.True(JsonNode.DeepEquals(new JsonObject
            {
                ["found"] = false,
                ["xpath"] = missingXPath,
                ["matchCount"] = 0,
            }, miss), missing.ToJsonString());

            const string outsideXPath = "//*[@AutomationId='SceneHeading']";
            var outside = await CallBridgeAsync(bridge, "find_by_xpath", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["xpath"] = outsideXPath,
            }, requestId);
            Assert.False(outside.ContainsKey("error"), outside.ToJsonString());
            var outsideMiss = Assert.IsType<JsonObject>(outside["result"]);
            Assert.True(JsonNode.DeepEquals(new JsonObject
            {
                ["found"] = false,
                ["xpath"] = outsideXPath,
                ["matchCount"] = 0,
            }, outsideMiss), outside.ToJsonString());
            return outside;
        });
    }

    [Fact]
    public async Task FindAllCascade_SameConnectionPreservesTotalsAndCombinesFiltersWithinRoot()
    {
        await RunBridgeAsync(async (bridge, _, requestId) =>
        {
            var parameters = new JsonObject
            {
                ["rootAutomationId"] = "AmbiguousIdentityRegion",
                ["controlType"] = "Button",
                ["maxResults"] = 10,
            };
            var all = await CallBridgeAsync(bridge, "find_all_cascade", parameters, requestId++);
            Assert.False(all.ContainsKey("error"), all.ToJsonString());
            var allResult = Assert.IsType<JsonObject>(all["result"]);
            Assert.Equal(2, allResult["totalMatches"]!.GetValue<int>());
            var matches = Assert.IsType<JsonArray>(allResult["results"]);
            Assert.Equal(2, matches.Count);
            var expectedNames = new[] { "Ambiguous action one", "Ambiguous action two" };
            Assert.All(matches, node =>
            {
                var match = Assert.IsType<JsonObject>(node);
                Assert.Equal("AmbiguousButton", match["automationId"]!.GetValue<string>());
                Assert.Equal("Button", match["controlType"]!.GetValue<string>());
            });
            Assert.Equal(expectedNames, matches.Select(node => node!["name"]!.GetValue<string>())
                .OrderBy(name => name, StringComparer.Ordinal).ToArray());

            parameters["maxResults"] = 1;
            var capped = await CallBridgeAsync(bridge, "find_all_cascade", parameters, requestId++);
            Assert.False(capped.ContainsKey("error"), capped.ToJsonString());
            var cappedResult = Assert.IsType<JsonObject>(capped["result"]);
            Assert.Equal(2, cappedResult["totalMatches"]!.GetValue<int>());
            var cappedMatch = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(cappedResult["results"])));
            Assert.Equal("AmbiguousButton", cappedMatch["automationId"]!.GetValue<string>());
            Assert.Contains(cappedMatch["name"]!.GetValue<string>(), expectedNames);
            Assert.Equal("Button", cappedMatch["controlType"]!.GetValue<string>());

            parameters["name"] = "Ambiguous action two";
            var unique = await CallBridgeAsync(bridge, "find_all_cascade", parameters, requestId++);
            Assert.False(unique.ContainsKey("error"), unique.ToJsonString());
            var uniqueResult = Assert.IsType<JsonObject>(unique["result"]);
            Assert.Equal(1, uniqueResult["totalMatches"]!.GetValue<int>());
            var second = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(uniqueResult["results"])));
            Assert.Equal("AmbiguousButton", second["automationId"]!.GetValue<string>());
            Assert.Equal("Ambiguous action two", second["name"]!.GetValue<string>());
            Assert.Equal("Button", second["controlType"]!.GetValue<string>());

            parameters["controlType"] = "Window";
            var conflicting = await CallBridgeAsync(bridge, "find_all_cascade", parameters, requestId++);
            Assert.False(conflicting.ContainsKey("error"), conflicting.ToJsonString());
            var conflictingResult = Assert.IsType<JsonObject>(conflicting["result"]);
            Assert.Equal(0, conflictingResult["totalMatches"]!.GetValue<int>());
            Assert.Empty(Assert.IsType<JsonArray>(conflictingResult["results"]));

            var outside = await CallBridgeAsync(bridge, "find_all_cascade", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["name"] = "Gallery Button Primary",
            }, requestId);
            Assert.False(outside.ContainsKey("error"), outside.ToJsonString());
            var outsideResult = Assert.IsType<JsonObject>(outside["result"]);
            Assert.Equal(0, outsideResult["totalMatches"]!.GetValue<int>());
            Assert.Empty(Assert.IsType<JsonArray>(outsideResult["results"]));
            return outside;
        });
    }

    [Fact]
    public async Task GetTree_DepthZeroAndOneRemainBoundedAndSameConnectionFindsSaveButton()
    {
        await RunBridgeAsync(async (bridge, _, requestId) =>
        {
            // FlaUI treats the selected window as the XPath root, so /* selects its direct children.
            var directChildren = await CallBridgeAsync(bridge, "find_by_xpath", new JsonObject
            {
                ["rootAutomationId"] = "NativeSceneProbeWindow",
                ["xpath"] = "/*",
            }, requestId++);
            Assert.False(directChildren.ContainsKey("error"), directChildren.ToJsonString());
            var firstMatch = Assert.IsType<JsonObject>(directChildren["result"]);
            Assert.True(firstMatch["found"]!.GetValue<bool>(), firstMatch.ToJsonString());
            var directChildCount = firstMatch["matchCount"]!.GetValue<int>();
            Assert.True(directChildCount > 1, directChildren.ToJsonString());

            var depthZero = await CallBridgeAsync(bridge, "get_tree", new JsonObject
            {
                ["maxDepth"] = 0,
                ["maxChildren"] = 1,
            }, requestId++);
            Assert.False(depthZero.ContainsKey("error"), depthZero.ToJsonString());
            var rootOnly = Assert.IsType<JsonObject>(depthZero["result"]);
            Assert.Equal(1, rootOnly["count"]!.GetValue<int>());
            Assert.Equal("Native Scene Probe Fixture", rootOnly["primary"]!.GetValue<string>());
            var rootWindows = Assert.IsType<JsonArray>(rootOnly["windows"]);
            var root = Assert.IsType<JsonObject>(Assert.Single(rootWindows));
            Assert.True(root["found"]!.GetValue<bool>(), root.ToJsonString());
            Assert.Equal("NativeSceneProbeWindow", root["automationId"]!.GetValue<string>());
            Assert.Equal("Native Scene Probe Fixture", root["name"]!.GetValue<string>());
            Assert.Equal("Window", root["controlType"]!.GetValue<string>());
            Assert.False(root.ContainsKey("children"), root.ToJsonString());
            Assert.IsType<JsonArray>(root["patterns"]);
            var bounds = Assert.IsType<JsonObject>(root["rect"]);
            foreach (var coordinate in new[] { "x", "y", "width", "height" })
            {
                Assert.True(double.IsFinite(bounds[coordinate]!.GetValue<double>()), bounds.ToJsonString());
            }
            Assert.True(bounds["width"]!.GetValue<double>() > 0, bounds.ToJsonString());
            Assert.True(bounds["height"]!.GetValue<double>() > 0, bounds.ToJsonString());

            var depthOne = await CallBridgeAsync(bridge, "get_tree", new JsonObject
            {
                ["maxDepth"] = 1,
                ["maxChildren"] = 1,
            }, requestId++);
            Assert.False(depthOne.ContainsKey("error"), depthOne.ToJsonString());
            var shallow = Assert.IsType<JsonObject>(depthOne["result"]);
            var shallowWindows = Assert.IsType<JsonArray>(shallow["windows"]);
            var shallowRoot = Assert.IsType<JsonObject>(Assert.Single(shallowWindows));
            Assert.Equal("NativeSceneProbeWindow", shallowRoot["automationId"]!.GetValue<string>());
            Assert.IsType<JsonArray>(shallowRoot["patterns"]);
            var children = Assert.IsType<JsonArray>(shallowRoot["children"]);
            Assert.Equal(2, children.Count);
            var firstChild = Assert.IsType<JsonObject>(children[0]);
            Assert.True(firstChild["found"]!.GetValue<bool>(), firstChild.ToJsonString());
            Assert.Equal(firstMatch["automationId"]!.GetValue<string>(), firstChild["automationId"]!.GetValue<string>());
            Assert.Equal(firstMatch["name"]!.GetValue<string>(), firstChild["name"]!.GetValue<string>());
            Assert.Equal(firstMatch["controlType"]!.GetValue<string>(), firstChild["controlType"]!.GetValue<string>());
            Assert.False(firstChild.ContainsKey("children"), firstChild.ToJsonString());
            Assert.False(firstChild.ContainsKey("patterns"), firstChild.ToJsonString());
            var truncation = Assert.IsType<JsonObject>(children[1]);
            Assert.True(JsonNode.DeepEquals(new JsonObject
            {
                ["truncated"] = true,
                ["total"] = directChildCount,
            }, truncation), truncation.ToJsonString());

            var unique = await CallBridgeAsync(bridge, "find_by_xpath", new JsonObject
            {
                ["rootAutomationId"] = "Gallery",
                ["xpath"] = "//Button[@AutomationId='SaveButton']",
            }, requestId);
            Assert.False(unique.ContainsKey("error"), unique.ToJsonString());
            var save = Assert.IsType<JsonObject>(unique["result"]);
            Assert.True(save["found"]!.GetValue<bool>());
            Assert.Equal(1, save["matchCount"]!.GetValue<int>());
            Assert.Equal("SaveButton", save["automationId"]!.GetValue<string>());
            Assert.Equal("Save scene", save["name"]!.GetValue<string>());
            Assert.Equal("Button", save["controlType"]!.GetValue<string>());
            Assert.False(save.ContainsKey("warning"), unique.ToJsonString());
            return unique;
        });
    }

    [Fact]
    public async Task GridSelectRange_ReadsExactlyTwoCueRows()
    {
        await RunBridgeAsync(async (bridge, _, requestId) =>
        {
            var gallery = await CallBridgeAsync(bridge, "extract_text", new JsonObject
            {
                ["automationId"] = "smokeGalleryStatus",
            }, requestId++);
            Assert.False(gallery.ContainsKey("error"), gallery.ToJsonString());
            var galleryResult = Assert.IsType<JsonObject>(gallery["result"]);
            var readiness = Assert.IsType<JsonObject>(JsonNode.Parse(galleryResult["text"]!.GetValue<string>()));
            Assert.Equal("ready", readiness["state"]!.GetValue<string>());
            Assert.True(readiness["generation"]!.GetValue<long>() > 0, readiness.ToJsonString());

            var selected = await CallBridgeAsync(bridge, "grid_select_range", new JsonObject
            {
                ["selector"] = new JsonObject
                {
                    ["automationId"] = "dataGrid",
                    ["controlType"] = "DataGrid",
                },
                ["start_index"] = 0,
                ["end_index"] = 1,
                ["columns"] = new JsonArray("Phrase"),
            }, requestId++);
            AssertSelectedCueRows(selected);
            var range = Assert.IsType<JsonObject>(selected["result"]!["selected_range"]);
            Assert.Equal(0, range["start"]!.GetValue<int>());
            Assert.Equal(1, range["end"]!.GetValue<int>());

            var readback = await CallBridgeAsync(bridge, "grid_selected_rows", new JsonObject
            {
                ["selector"] = new JsonObject
                {
                    ["automationId"] = "dataGrid",
                    ["controlType"] = "DataGrid",
                },
                ["columns"] = new JsonArray("Phrase"),
            }, requestId);
            AssertSelectedCueRows(readback);
            return readback;
        }, fixtureChoice: BridgeFixture.WpfSmokeApp);
    }

    [Fact]
    public async Task ExpandCollapse_WpfSmokeApp_TransitionsAreIdempotentAndUnsupportedPatternKeepsConnectionUsable()
    {
        await RunBridgeAsync(async (bridge, _, requestId) =>
        {
            var selector = new JsonObject { ["automationId"] = "patternDetails" };
            var baseline = await CallBridgeAsync(bridge, "collapse", selector, requestId++);
            Assert.False(baseline.ContainsKey("error"), baseline.ToJsonString());
            var baselineResult = Assert.IsType<JsonObject>(baseline["result"]);
            Assert.True(baselineResult["collapsed"]!.GetValue<bool>());
            Assert.Equal("patternDetails", baselineResult["automation_id"]!.GetValue<string>());

            var unsupported = await CallBridgeAsync(bridge, "expand", new JsonObject
            {
                ["automationId"] = "btnInvoke",
            }, requestId++);
            Assert.False(unsupported.ContainsKey("result"), unsupported.ToJsonString());
            var error = Assert.IsType<JsonObject>(unsupported["error"]);
            Assert.Equal(-32603, error["code"]!.GetValue<int>());
            Assert.Equal("Internal error: Element 'btnInvoke' does not support ExpandCollapsePattern",
                error["message"]!.GetValue<string>());

            var expanded = await CallBridgeAsync(bridge, "expand", selector, requestId++);
            Assert.False(expanded.ContainsKey("error"), expanded.ToJsonString());
            var expandedResult = Assert.IsType<JsonObject>(expanded["result"]);
            Assert.True(expandedResult["expanded"]!.GetValue<bool>());
            Assert.Equal("patternDetails", expandedResult["automation_id"]!.GetValue<string>());
            Assert.False(expandedResult["was_already"]!.GetValue<bool>());

            var expandedAgain = await CallBridgeAsync(bridge, "expand", selector, requestId++);
            Assert.False(expandedAgain.ContainsKey("error"), expandedAgain.ToJsonString());
            var expandedAgainResult = Assert.IsType<JsonObject>(expandedAgain["result"]);
            Assert.True(expandedAgainResult["expanded"]!.GetValue<bool>());
            Assert.Equal("patternDetails", expandedAgainResult["automation_id"]!.GetValue<string>());
            Assert.True(expandedAgainResult["was_already"]!.GetValue<bool>());

            var collapsed = await CallBridgeAsync(bridge, "collapse", selector, requestId++);
            Assert.False(collapsed.ContainsKey("error"), collapsed.ToJsonString());
            var collapsedResult = Assert.IsType<JsonObject>(collapsed["result"]);
            Assert.True(collapsedResult["collapsed"]!.GetValue<bool>());
            Assert.Equal("patternDetails", collapsedResult["automation_id"]!.GetValue<string>());
            Assert.False(collapsedResult["was_already"]!.GetValue<bool>());

            var collapsedAgain = await CallBridgeAsync(bridge, "collapse", selector, requestId++);
            Assert.False(collapsedAgain.ContainsKey("error"), collapsedAgain.ToJsonString());
            var collapsedAgainResult = Assert.IsType<JsonObject>(collapsedAgain["result"]);
            Assert.True(collapsedAgainResult["collapsed"]!.GetValue<bool>());
            Assert.Equal("patternDetails", collapsedAgainResult["automation_id"]!.GetValue<string>());
            Assert.True(collapsedAgainResult["was_already"]!.GetValue<bool>());
            return collapsedAgain;
        }, fixtureChoice: BridgeFixture.WpfSmokeApp);
    }

    [Fact]
    public async Task Screenshot_WpfSmokeApp_PngAndEvidenceMatchIndependentWin32Facts()
    {
        await RunBridgeAsync(async (bridge, processId, requestId) =>
        {
            using var fixture = Process.GetProcessById(processId);
            var target = ObserveWindow(fixture);
            Assert.Equal(checked((uint)processId), target.ProcessId);

            var missingTarget = await CallBridgeAsync(bridge, "screenshot", new JsonObject
            {
                ["evidence"] = true,
                ["typed_bitblt_fallback"] = true,
            }, requestId++);
            AssertError(missingTarget,
                "Typed BitBlt fallback requires an expected HWND, active process ID, and positive physical dimensions.");

            var zeroTarget = await CallBridgeAsync(bridge, "screenshot", new JsonObject
            {
                ["evidence"] = true,
                ["typed_bitblt_fallback"] = true,
                ["expected_hwnd"] = 0L,
                ["expected_process_id"] = processId,
                ["expected_physical_width"] = target.Window.Right - target.Window.Left,
                ["expected_physical_height"] = target.Window.Bottom - target.Window.Top,
            }, requestId++);
            AssertError(zeroTarget,
                "Typed BitBlt fallback requires an expected HWND, active process ID, and positive physical dimensions.");

            Assert.NotEqual(processId, bridge.Id);
            var wrongProcess = await CallBridgeAsync(bridge, "screenshot", new JsonObject
            {
                ["evidence"] = true,
                ["typed_bitblt_fallback"] = true,
                ["hwnd"] = target.Hwnd.ToInt64(),
                ["expected_hwnd"] = target.Hwnd.ToInt64(),
                ["expected_process_id"] = bridge.Id,
                ["expected_physical_width"] = target.Window.Right - target.Window.Left,
                ["expected_physical_height"] = target.Window.Bottom - target.Window.Top,
            }, requestId++);
            AssertError(wrongProcess, "Capture target does not belong to the active debuggee process.");

            // Both capture paths may flash focus before these PrintWindow assertions can fail.
            var ordinaryBefore = ObserveWindow(fixture);
            Assert.Equal(target, ordinaryBefore);
            var ordinary = await CallBridgeAsync(bridge, "screenshot", new JsonObject(), requestId++);
            var ordinaryAfter = ObserveWindow(fixture);
            Assert.Equal(ordinaryBefore, ordinaryAfter);
            AssertScreenshotPng(ordinary, ordinaryBefore);

            var evidenceBefore = ObserveWindow(fixture);
            Assert.Equal(ordinaryAfter, evidenceBefore);
            var evidence = await CallBridgeAsync(bridge, "screenshot", new JsonObject
            {
                ["evidence"] = true,
            }, requestId);
            var evidenceAfter = ObserveWindow(fixture);
            Assert.Equal(evidenceBefore, evidenceAfter);
            var result = AssertScreenshotPng(evidence, evidenceBefore);
            Assert.Equal(evidenceBefore.Hwnd.ToInt64(), result["hwnd"]!.GetValue<long>());
            Assert.Equal(checked((int)evidenceBefore.ProcessId), result["process_id"]!.GetValue<int>());
            Assert.Equal(checked((int)evidenceBefore.Dpi), result["dpi"]!.GetValue<int>());
            AssertScreenshotRect(result["client_rect"], evidenceBefore.Client, "client", "GetClientRect");
            AssertScreenshotRect(result["window_bounds"], evidenceBefore.Window, "screen", "GetWindowRect");
            return evidence;
        }, fixtureChoice: BridgeFixture.WpfSmokeApp);
    }

    private JsonObject AssertScreenshotPng(JsonObject response, WindowObservation observation)
    {
        Assert.False(response.ContainsKey("error"), response["error"]?.ToJsonString());
        var result = Assert.IsType<JsonObject>(response["result"]);
        _output.WriteLine($"Screenshot classification: method={result["method"]} flags={result["flags"]} " +
            $"fallback={result["fallback"]} printwindow_classification={result["printwindow_classification"]} " +
            $"foreground={result["foreground"]?.ToJsonString()}");
        Assert.Equal("PrintWindow", result["method"]!.GetValue<string>());
        Assert.Equal(2, result["flags"]!.GetValue<int>());
        Assert.False(result.ContainsKey("fallback"));
        Assert.False(result.ContainsKey("foreground"));

        var png = Convert.FromBase64String(result["base64"]!.GetValue<string>());
        Assert.True(png.Length >= 33, "PNG must contain its signature and complete IHDR chunk.");
        ReadOnlySpan<byte> signature = [137, 80, 78, 71, 13, 10, 26, 10];
        Assert.True(png.AsSpan(0, 8).SequenceEqual(signature), "Screenshot is not a PNG.");
        Assert.Equal(13u, BinaryPrimitives.ReadUInt32BigEndian(png.AsSpan(8, 4)));
        Assert.True(png.AsSpan(12, 4).SequenceEqual("IHDR"u8), "PNG first chunk must be IHDR.");
        var width = BinaryPrimitives.ReadUInt32BigEndian(png.AsSpan(16, 4));
        var height = BinaryPrimitives.ReadUInt32BigEndian(png.AsSpan(20, 4));
        Assert.Equal(checked((uint)result["width"]!.GetValue<int>()), width);
        Assert.Equal(checked((uint)result["height"]!.GetValue<int>()), height);
        Assert.Equal(checked((uint)(observation.Window.Right - observation.Window.Left)), width);
        Assert.Equal(checked((uint)(observation.Window.Bottom - observation.Window.Top)), height);
        return result;
    }

    private static void AssertScreenshotRect(JsonNode? node, RECT expected, string coordinateSpace, string sourceApi)
    {
        var rect = Assert.IsType<JsonObject>(node);
        Assert.Equal(expected.Left, rect["left"]!.GetValue<int>());
        Assert.Equal(expected.Top, rect["top"]!.GetValue<int>());
        Assert.Equal(expected.Right, rect["right"]!.GetValue<int>());
        Assert.Equal(expected.Bottom, rect["bottom"]!.GetValue<int>());
        Assert.Equal("physical_px", rect["unit"]!.GetValue<string>());
        Assert.Equal(coordinateSpace, rect["coordinate_space"]!.GetValue<string>());
        Assert.Equal(sourceApi, rect["source_api"]!.GetValue<string>());
    }

    private static WindowObservation ObserveWindow(Process fixture)
    {
        // Keep physical-coordinate observation and DPI-context restoration on this synchronous thread.
        var previous = SetThreadDpiAwarenessContext(new IntPtr(-4));
        Assert.NotEqual(IntPtr.Zero, previous);
        try
        {
            fixture.Refresh();
            Assert.False(fixture.HasExited, "WPF screenshot fixture exited before observation.");
            var hwnd = fixture.MainWindowHandle;
            Assert.NotEqual(IntPtr.Zero, hwnd);
            Assert.True(GetWindowRect(hwnd, out var window), $"GetWindowRect failed: {Marshal.GetLastWin32Error()}");
            Assert.True(GetClientRect(hwnd, out var client), $"GetClientRect failed: {Marshal.GetLastWin32Error()}");
            var dpi = GetDpiForWindow(hwnd);
            Assert.NotEqual(0u, dpi);
            Assert.NotEqual(0u, GetWindowThreadProcessId(hwnd, out var ownerProcessId));
            Assert.Equal(checked((uint)fixture.Id), ownerProcessId);
            Assert.True(window.Right > window.Left && window.Bottom > window.Top);
            Assert.True(client.Right > client.Left && client.Bottom > client.Top);
            return new WindowObservation(hwnd, ownerProcessId, window, client, dpi);
        }
        finally
        {
            Assert.NotEqual(IntPtr.Zero, SetThreadDpiAwarenessContext(previous));
        }
    }

    private readonly record struct WindowObservation(IntPtr Hwnd, uint ProcessId, RECT Window, RECT Client, uint Dpi);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetWindowRect(IntPtr hwnd, out RECT rect);

    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetClientRect(IntPtr hwnd, out RECT rect);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetDpiForWindow(IntPtr hwnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern IntPtr SetThreadDpiAwarenessContext(IntPtr dpiContext);

    private static void AssertSelectedCueRows(JsonObject response)
    {
        Assert.False(response.ContainsKey("error"), response.ToJsonString());
        var result = Assert.IsType<JsonObject>(response["result"]);
        Assert.Equal("PASS", result["status"]!.GetValue<string>());
        var rows = Assert.IsType<JsonArray>(result["selected_rows"]);
        Assert.Equal(2, rows.Count);
        for (var index = 0; index < rows.Count; index++)
        {
            var row = Assert.IsType<JsonObject>(rows[index]);
            Assert.Equal(index, row["index"]!.GetValue<int>());
            Assert.Equal(index, row["row_index"]!.GetValue<int>());
            Assert.True(row["selected"]!.GetValue<bool>(), row.ToJsonString());
            var cells = Assert.IsType<JsonObject>(row["cells"]);
            Assert.Equal(index == 0 ? "Fixture cue one" : "Fixture cue two", cells["Phrase"]!.GetValue<string>());
        }
    }

    private static void AssertText(JsonObject response, string expected)
    {
        Assert.False(response.ContainsKey("error"), response.ToJsonString());
        var result = Assert.IsType<JsonObject>(response["result"]);
        Assert.Equal(expected, result["text"]!.GetValue<string>());
    }

    private static void AssertError(JsonObject response, string expectedMessage)
    {
        Assert.False(response.ContainsKey("result"), response.ToJsonString());
        var error = Assert.IsType<JsonObject>(response["error"]);
        Assert.Equal(-32603, error["code"]!.GetValue<int>());
        Assert.Contains(expectedMessage, error["message"]!.GetValue<string>(), StringComparison.Ordinal);
    }

    private Task<JsonObject> RunExtractTextAsync(JsonObject selector, string mode = "bridge-elements") =>
        RunBridgeAsync((bridge, _, requestId) => CallBridgeAsync(bridge, "extract_text", selector, requestId), mode);

    private async Task<JsonObject> RunBridgeAsync(
        Func<Process, int, int, Task<JsonObject>> exercise, string mode = "bridge-elements",
        BridgeFixture fixtureChoice = BridgeFixture.NativeSceneProbe)
    {
        Assert.True(OperatingSystem.IsWindows(), "ElementCommands behavioral proof requires the Windows desktop.");
        var configuration = new DirectoryInfo(AppContext.BaseDirectory).Parent?.Name
            ?? throw new InvalidOperationException("Test output configuration is absent.");
        var fixturePath = fixtureChoice == BridgeFixture.NativeSceneProbe
            ? Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless.Tests", "Fixtures",
                "NativeSceneProbe.WpfFixture", "bin", configuration, "net8.0-windows", "NativeSceneProbe.WpfFixture.exe")
            : Path.Combine(RepositoryLayout.Root, "tests", "fixtures", "WpfSmokeApp", "bin", configuration,
                "net8.0-windows", "WpfSmokeApp.exe");
        Assert.True(File.Exists(fixturePath), $"Built WPF fixture is absent: '{fixturePath}'.");
        var bridgeDirectory = Path.Combine(RepositoryLayout.Root, "bridge", "bin", configuration, "net8.0-windows");
        var bridgePath = new[]
        {
            Path.Combine(bridgeDirectory, "win-x64", "FlaUIBridge.exe"),
            Path.Combine(bridgeDirectory, "FlaUIBridge.exe"),
        }.FirstOrDefault(File.Exists) ?? throw new InvalidOperationException("Built FlaUI bridge apphost is absent.");

        using var fixture = new Process();
        Task<string>? fixtureError = null;
        try
        {
            var readinessName = fixtureChoice == BridgeFixture.NativeSceneProbe
                ? $"element-commands-window-ready-{Guid.NewGuid():N}"
                : null;
            using var readiness = readinessName is null ? null : new NamedPipeServerStream(readinessName, PipeDirection.In, 1,
                PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
            var fixtureInfo = new ProcessStartInfo(fixturePath)
            {
                WorkingDirectory = RepositoryLayout.Root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            if (readinessName is not null)
            {
                fixtureInfo.ArgumentList.Add("--native-scene-probe-test-harness");
                fixtureInfo.ArgumentList.Add($"--native-scene-probe-mode={mode}");
                fixtureInfo.Environment["NETCOREDBG_NATIVE_SCENE_PROBE_FIXTURE_MODE"] = mode;
                fixtureInfo.Environment["CONTROLLED_DAP_WINDOWED_DESCENDANT_READINESS_PIPE"] = readinessName;
            }
            fixture.StartInfo = fixtureInfo;
            if (!fixture.Start())
            {
                throw new InvalidOperationException("WPF fixture could not be started.");
            }
            fixtureError = fixture.StandardError.ReadToEndAsync();
            if (readiness is not null)
            {
                using var startup = new CancellationTokenSource(StartupTimeout);
                await readiness.WaitForConnectionAsync(startup.Token);
                var handle = new byte[sizeof(long)];
                await readiness.ReadExactlyAsync(handle, startup.Token);
                Assert.NotEqual(0L, BinaryPrimitives.ReadInt64LittleEndian(handle));
                Assert.False(fixture.HasExited, "WPF fixture exited before window readiness.");
            }
            else
            {
                await NativeSceneAtomicityTests.WaitForMainWindowAsync(fixture.Id);
            }

            var bridgeInfo = new ProcessStartInfo(bridgePath)
            {
                WorkingDirectory = RepositoryLayout.Root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            using var bridge = Process.Start(bridgeInfo)
                ?? throw new InvalidOperationException("FlaUI bridge could not be started.");
            var bridgeError = bridge.StandardError.ReadToEndAsync();
            try
            {
                var connectStarted = Stopwatch.GetTimestamp();
                JsonObject connected;
                try
                {
                    connected = await CallBridgeAsync(bridge, "connect", new JsonObject
                    {
                        ["pid"] = fixture.Id,
                        ["stealth"] = true,
                    }, 1);
                }
                finally
                {
                    _output.WriteLine($"Bridge RPC method=connect id=1 elapsed={Stopwatch.GetElapsedTime(connectStarted)} " +
                        $"fixturePid={fixture.Id} fixtureHasExited={fixture.HasExited} " +
                        $"bridgePid={bridge.Id} bridgeHasExited={bridge.HasExited} bridgePath='{bridgePath}'");
                }
                Assert.False(connected.ContainsKey("error"), connected.ToJsonString());
                Assert.True(connected["result"]!["connected"]!.GetValue<bool>());
                if (fixtureChoice == BridgeFixture.WpfSmokeApp)
                {
                    Assert.Equal("WPF Smoke Test", connected["result"]!["title"]!.GetValue<string>());
                }
                var requestId = 2;
                if (mode == "bridge-elements-ambiguous-roots")
                {
                    var tree = await CallBridgeAsync(bridge, "get_tree", new JsonObject
                    {
                        ["maxDepth"] = 4,
                        ["maxChildren"] = 25,
                    }, requestId++);
                    Assert.False(tree.ContainsKey("error"), tree.ToJsonString());
                    var result = Assert.IsType<JsonObject>(tree["result"]);
                    var windows = Assert.IsType<JsonArray>(result["windows"]);
                    Assert.True(result["count"]!.GetValue<int>() == 2 && windows.Count == 2,
                        $"Ambiguity fixture must expose two UIA top-level windows for connected pid {fixture.Id}: {tree.ToJsonString()}");
                    Assert.All(windows, node =>
                    {
                        var window = Assert.IsType<JsonObject>(node);
                        Assert.Equal("Window", window["controlType"]!.GetValue<string>());
                        Assert.Equal("NativeSceneProbeWindow", window["automationId"]!.GetValue<string>());
                        Assert.True(TreeContainsAutomationId(window, "Gallery"), window.ToJsonString());
                    });
                }
                return await exercise(bridge, fixture.Id, requestId);
            }
            catch (Exception exception)
            {
                _output.WriteLine("Bridge operation failed before teardown:");
                _output.WriteLine(exception.ToString());
                throw;
            }
            finally
            {
                try
                {
                    await StopAndDrainAsync(bridge, bridgeError, closeStandardInput: true);
                }
                catch (Exception exception)
                {
                    _output.WriteLine("Bridge cleanup failed before fixture teardown:");
                    _output.WriteLine(exception.ToString());
                    throw;
                }
            }
        }
        finally
        {
            if (fixtureError is not null)
            {
                try
                {
                    await StopAndDrainAsync(fixture, fixtureError, closeStandardInput: false);
                }
                catch (Exception exception)
                {
                    _output.WriteLine("Fixture cleanup failed:");
                    _output.WriteLine(exception.ToString());
                    throw;
                }
            }
        }
    }

    private async Task<JsonObject> CallBridgeAsync(Process bridge, string method, JsonObject parameters, int id)
    {
        using var request = new CancellationTokenSource(RequestTimeout);
        var started = Stopwatch.GetTimestamp();
        var phase = "serialize";
        try
        {
            var payload = new JsonObject
            {
                ["jsonrpc"] = "2.0",
                ["id"] = id,
                ["method"] = method,
                ["params"] = parameters.DeepClone(),
            };
            var lineToWrite = payload.ToJsonString();
            phase = "write";
            await bridge.StandardInput.WriteLineAsync(lineToWrite.AsMemory(), request.Token);
            phase = "flush";
            await bridge.StandardInput.FlushAsync(request.Token);
            phase = "read";
            var line = await bridge.StandardOutput.ReadLineAsync(request.Token);
            phase = "assert-response-present";
            Assert.NotNull(line);
            phase = "parse";
            var response = Assert.IsType<JsonObject>(JsonNode.Parse(line));
            phase = "assert-envelope";
            Assert.Equal("2.0", response["jsonrpc"]!.GetValue<string>());
            Assert.Equal(id, response["id"]!.GetValue<int>());
            return response;
        }
        catch (Exception exception)
        {
            _output.WriteLine($"Bridge RPC method={method} id={id} phase={phase} " +
                $"elapsed={Stopwatch.GetElapsedTime(started)} requestCancelled={request.IsCancellationRequested}");
            _output.WriteLine(exception.ToString());
            throw;
        }
    }

    private static bool TreeContainsAutomationId(JsonObject node, string automationId) =>
        node["automationId"]?.GetValue<string>() == automationId ||
        node["children"] is JsonArray children &&
        children.OfType<JsonObject>().Any(child => TreeContainsAutomationId(child, automationId));

    private static async Task StopAndDrainAsync(Process process, Task<string> standardError, bool closeStandardInput)
    {
        var forced = false;
        if (!process.HasExited)
        {
            if (closeStandardInput)
            {
                process.StandardInput.Close();
            }
            else
            {
                process.CloseMainWindow();
            }

            try
            {
                await process.WaitForExitAsync().WaitAsync(CleanupTimeout);
            }
            catch (TimeoutException)
            {
                forced = true;
                if (!process.HasExited)
                {
                    process.Kill(entireProcessTree: true);
                }
                await process.WaitForExitAsync().WaitAsync(CleanupTimeout);
            }
        }

        await process.StandardOutput.ReadToEndAsync().WaitAsync(CleanupTimeout);
        var error = await standardError.WaitAsync(CleanupTimeout);
        Assert.False(forced, $"Owned process {process.Id} required forced cleanup: {error}");
        Assert.Equal(0, process.ExitCode);
    }
}
