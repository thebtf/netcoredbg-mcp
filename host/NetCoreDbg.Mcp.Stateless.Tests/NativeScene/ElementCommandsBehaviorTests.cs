using System.Buffers.Binary;
using System.Diagnostics;
using System.IO.Pipes;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class ElementCommandsBehaviorTests
{
    private static readonly TimeSpan StartupTimeout = TimeSpan.FromSeconds(45);
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan CleanupTimeout = TimeSpan.FromSeconds(5);

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

    private static async Task<JsonObject> RunExtractTextAsync(JsonObject selector, string mode = "bridge-elements")
    {
        Assert.True(OperatingSystem.IsWindows(), "ElementCommands behavioral proof requires the Windows desktop.");
        var configuration = new DirectoryInfo(AppContext.BaseDirectory).Parent?.Name
            ?? throw new InvalidOperationException("Test output configuration is absent.");
        var fixturePath = Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless.Tests", "Fixtures",
            "NativeSceneProbe.WpfFixture", "bin", configuration, "net8.0-windows", "NativeSceneProbe.WpfFixture.exe");
        Assert.True(File.Exists(fixturePath), $"Built WPF fixture is absent: '{fixturePath}'.");
        var bridgeDirectory = Path.Combine(RepositoryLayout.Root, "bridge", "bin", configuration, "net8.0-windows");
        var bridgePath = new[]
        {
            Path.Combine(bridgeDirectory, "win-x64", "FlaUIBridge.dll"),
            Path.Combine(bridgeDirectory, "FlaUIBridge.dll"),
        }.FirstOrDefault(File.Exists) ?? throw new InvalidOperationException("Built FlaUI bridge assembly is absent.");

        var readinessName = $"element-commands-window-ready-{Guid.NewGuid():N}";
        using var readiness = new NamedPipeServerStream(readinessName, PipeDirection.In, 1,
            PipeTransmissionMode.Byte, PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        var fixtureInfo = new ProcessStartInfo(fixturePath)
        {
            WorkingDirectory = RepositoryLayout.Root,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        fixtureInfo.ArgumentList.Add("--native-scene-probe-test-harness");
        fixtureInfo.ArgumentList.Add($"--native-scene-probe-mode={mode}");
        fixtureInfo.Environment["NETCOREDBG_NATIVE_SCENE_PROBE_FIXTURE_MODE"] = mode;
        fixtureInfo.Environment["CONTROLLED_DAP_WINDOWED_DESCENDANT_READINESS_PIPE"] = readinessName;
        using var fixture = Process.Start(fixtureInfo)
            ?? throw new InvalidOperationException("WPF fixture could not be started.");
        var fixtureError = fixture.StandardError.ReadToEndAsync();
        try
        {
            using (var startup = new CancellationTokenSource(StartupTimeout))
            {
                await readiness.WaitForConnectionAsync(startup.Token);
                var handle = new byte[sizeof(long)];
                await readiness.ReadExactlyAsync(handle, startup.Token);
                Assert.NotEqual(0L, BinaryPrimitives.ReadInt64LittleEndian(handle));
                Assert.False(fixture.HasExited, "WPF fixture exited before window readiness.");
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
                var connected = await CallBridgeAsync(bridge, "connect", new JsonObject
                {
                    ["pid"] = fixture.Id,
                    ["stealth"] = true,
                }, 1);
                Assert.False(connected.ContainsKey("error"), connected.ToJsonString());
                Assert.True(connected["result"]!["connected"]!.GetValue<bool>());
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
                return await CallBridgeAsync(bridge, "extract_text", selector, requestId);
            }
            finally
            {
                await StopAndDrainAsync(bridge, bridgeError, closeStandardInput: true);
            }
        }
        finally
        {
            await StopAndDrainAsync(fixture, fixtureError, closeStandardInput: false);
        }
    }

    private static async Task<JsonObject> CallBridgeAsync(Process bridge, string method, JsonObject parameters, int id)
    {
        using var request = new CancellationTokenSource(RequestTimeout);
        var payload = new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = id,
            ["method"] = method,
            ["params"] = parameters.DeepClone(),
        };
        await bridge.StandardInput.WriteLineAsync(payload.ToJsonString().AsMemory(), request.Token);
        await bridge.StandardInput.FlushAsync(request.Token);
        var line = await bridge.StandardOutput.ReadLineAsync(request.Token);
        Assert.NotNull(line);
        var response = Assert.IsType<JsonObject>(JsonNode.Parse(line));
        Assert.Equal("2.0", response["jsonrpc"]!.GetValue<string>());
        Assert.Equal(id, response["id"]!.GetValue<int>());
        return response;
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
