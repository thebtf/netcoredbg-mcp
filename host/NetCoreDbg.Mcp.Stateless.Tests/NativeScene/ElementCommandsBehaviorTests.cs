using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
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
            Path.Combine(bridgeDirectory, "win-x64", "FlaUIBridge.dll"),
            Path.Combine(bridgeDirectory, "FlaUIBridge.dll"),
        }.FirstOrDefault(File.Exists) ?? throw new InvalidOperationException("Built FlaUI bridge assembly is absent.");

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

            var bridgeInfo = new ProcessStartInfo("dotnet")
            {
                WorkingDirectory = RepositoryLayout.Root,
                UseShellExecute = false,
                CreateNoWindow = true,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
            };
            bridgeInfo.ArgumentList.Add(bridgePath);
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
