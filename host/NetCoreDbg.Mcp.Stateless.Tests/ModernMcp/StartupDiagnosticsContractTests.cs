using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;
using Xunit.Abstractions;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class StartupDiagnosticsContractTests : IDisposable
{
    private const string DiagnosticEnvironment = "NETCOREDBG_MCP_PRIVATE_START_DIAGNOSTICS";
    private const string PrivateMarker = "private-startup-input-must-not-be-recorded";
    private readonly List<string> _output = [];
    private readonly List<bool> _outputBeforeScratchDeletion = [];
    private readonly Action<string>? _previousOutput = FixtureProcess.StartupDiagnosticOutput.Value;

    public StartupDiagnosticsContractTests(ITestOutputHelper output)
    {
        FixtureProcess.StartupDiagnosticOutput.Value = line =>
        {
            _output.Add(line);
            var transcript = Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT");
            _outputBeforeScratchDeletion.Add(transcript is not null && File.Exists(transcript));
            output.WriteLine(line);
        };
    }

    public void Dispose() => FixtureProcess.StartupDiagnosticOutput.Value = _previousOutput;

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task UnconfiguredDebugger_RetainsEveryNonthrowClassificationOnlyWhenOptedIn(bool optIn)
    {
        var root = ModernMcpScratchDirectory.Create();
        var previous = Environment.GetEnvironmentVariable(DiagnosticEnvironment);
        try
        {
            Environment.SetEnvironmentVariable(DiagnosticEnvironment, optIn ? root : null);
            await using (var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_PATH"] = string.Empty })))
            {
                for (var request = 0; request < 2; request++)
                {
                    AssertUnchangedNotFound(await StartAsync(driver, request));
                }
            }

            var records = Directory.GetFiles(root, "host-start-*.json");
            Assert.Equal(optIn ? 2 : 0, records.Length);
            foreach (var path in records)
            {
                using var record = JsonDocument.Parse(await File.ReadAllTextAsync(path));
                Assert.Equal("debugger-unconfigured", record.RootElement.GetProperty("reason").GetString());
                Assert.Equal(JsonValueKind.Null, record.RootElement.GetProperty("exceptionClass").ValueKind);
            }
            Assert.Equal(optIn, _output.Count > 0);
            AssertSafe(string.Join(Environment.NewLine, _output));
        }
        finally
        {
            Environment.SetEnvironmentVariable(DiagnosticEnvironment, previous);
            await ModernMcpScratchDirectory.DeleteAsync(root);
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task ControlledDescendantStartFailure_RetainsSafeLaunchTranscriptBeforeScratchDeletion(bool optIn)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var root = ModernMcpScratchDirectory.Create();
        var previous = Environment.GetEnvironmentVariable(DiagnosticEnvironment);
        string? transcript = null;
        string? program = null;
        try
        {
            Environment.SetEnvironmentVariable(DiagnosticEnvironment, optIn ? root : null);
            await using (var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(
                    SpawnWindowedDescendant: true,
                    WindowedDescendantExecutablePath: Path.Combine(root, PrivateMarker + ".exe")))))
            {
                transcript = Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT");
                program = driver.InertProgramPath;
                AssertUnchangedNotFound(await StartAsync(driver, 0));
            }

            Assert.NotNull(transcript);
            Assert.False(Directory.Exists(Path.GetDirectoryName(transcript)));
            if (!optIn)
            {
                Assert.Empty(Directory.GetFiles(root));
                Assert.Empty(_output);
                return;
            }

            using var host = JsonDocument.Parse(await File.ReadAllTextAsync(Assert.Single(Directory.GetFiles(root, "host-start-*.json"))));
            Assert.Equal("startup-exception", host.RootElement.GetProperty("reason").GetString());
            Assert.Equal("adapter-start", host.RootElement.GetProperty("stage").GetString());
            Assert.Equal("IOException", host.RootElement.GetProperty("exceptionClass").GetString());

            var lines = await File.ReadAllLinesAsync(Assert.Single(Directory.GetFiles(root, "controlled-dap-*.jsonl")));
            var records = lines.Select(static line => JsonNode.Parse(line)).Select(node => Assert.IsType<JsonObject>(node)).ToArray();
            Assert.Contains(records, record => record["kind"]?.GetValue<string>() == "configuration-done");
            Assert.DoesNotContain(records, record => record["kind"]?.GetValue<string>() == "launch-released");
            var failure = Assert.Single(records, record => record["kind"]?.GetValue<string>() == "private-start-failure");
            Assert.Equal("descendant-start", failure["stage"]?.GetValue<string>());
            Assert.Equal("Win32Exception", failure["exceptionClass"]?.GetValue<string>());
            var output = string.Join(Environment.NewLine, _output);
            Assert.Contains("startup-exception", output, StringComparison.Ordinal);
            Assert.Contains("configuration-done", output, StringComparison.Ordinal);
            Assert.Contains("Win32Exception", output, StringComparison.Ordinal);
            Assert.All(_outputBeforeScratchDeletion, static exists => Assert.True(exists));
            AssertSafe(output + string.Join(Environment.NewLine, lines) + host.RootElement.GetRawText());
            Assert.DoesNotContain(program!, output + string.Join(Environment.NewLine, lines) + host.RootElement.GetRawText(), StringComparison.Ordinal);
        }
        finally
        {
            Environment.SetEnvironmentVariable(DiagnosticEnvironment, previous);
            await ModernMcpScratchDirectory.DeleteAsync(root);
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task SuccessfulStartup_PreservesPublicSuccessAndRecordsNoHostFailure(bool optIn)
    {
        var root = ModernMcpScratchDirectory.Create();
        var previous = Environment.GetEnvironmentVariable(DiagnosticEnvironment);
        try
        {
            Environment.SetEnvironmentVariable(DiagnosticEnvironment, optIn ? root : null);
            await using (var driver = await ModernMcpProcessDriver.StartAsync())
            {
                var result = ModernMcpProcessDriver.RequireResult(await StartAsync(driver, 0));
                Assert.Equal("complete", result["resultType"]?.GetValue<string>());
                Assert.False(result["isError"]?.GetValue<bool>() == true);
                Assert.Equal("start_debug_success", result["structuredContent"]!["kind"]?.GetValue<string>());
                Assert.NotNull(result["structuredContent"]!["debugSessionId"]);
            }

            Assert.Empty(Directory.GetFiles(root, "host-start-*.json"));
            if (optIn)
            {
                Assert.Contains("launch-released", string.Join(Environment.NewLine, _output), StringComparison.Ordinal);
                AssertSafe(string.Join(Environment.NewLine, _output));
            }
            else
            {
                Assert.Empty(Directory.GetFiles(root));
                Assert.Empty(_output);
            }
        }
        finally
        {
            Environment.SetEnvironmentVariable(DiagnosticEnvironment, previous);
            await ModernMcpScratchDirectory.DeleteAsync(root);
        }
    }

    [Fact]
    public async Task UnavailableDiagnosticSinks_PreservePublicErrorAndScratchCleanup()
    {
        var root = ModernMcpScratchDirectory.Create();
        var previous = Environment.GetEnvironmentVariable(DiagnosticEnvironment);
        var previousOutput = FixtureProcess.StartupDiagnosticOutput.Value;
        try
        {
            var unavailableDirectory = Path.Combine(root, "not-a-directory");
            await File.WriteAllTextAsync(unavailableDirectory, string.Empty);
            Environment.SetEnvironmentVariable(DiagnosticEnvironment, unavailableDirectory);
            var outputAttempted = false;
            FixtureProcess.StartupDiagnosticOutput.Value = _ =>
            {
                outputAttempted = true;
                throw new InvalidOperationException(PrivateMarker);
            };
            string? transcript;
            await using (var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(
                    SpawnWindowedDescendant: true,
                    WindowedDescendantExecutablePath: Path.Combine(root, PrivateMarker + ".exe")))))
            {
                transcript = Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT");
                AssertUnchangedNotFound(await StartAsync(driver, 0));
            }

            Assert.True(outputAttempted);
            Assert.NotNull(transcript);
            Assert.False(Directory.Exists(Path.GetDirectoryName(transcript)));
            AssertSafe(string.Join(Environment.NewLine, _output));
        }
        finally
        {
            FixtureProcess.StartupDiagnosticOutput.Value = previousOutput;
            Environment.SetEnvironmentVariable(DiagnosticEnvironment, previous);
            await ModernMcpScratchDirectory.DeleteAsync(root);
        }
    }

    private static Task<JsonRpcResponse> StartAsync(ModernMcpProcessDriver driver, int request) => driver.CallToolRawAsync(
        "start_debug",
        new JsonObject { ["program"] = driver.InertProgramPath },
        ModernMcpProcessDriver.CurrentMeta(extra: new JsonObject { ["privateDiagnosticInput"] = PrivateMarker }),
        new RequestId($"startup-diagnostic-{request}"));

    private static void AssertUnchangedNotFound(JsonRpcResponse response)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>());
        var expected = JsonNode.Parse("{\"kind\":\"debug_session_not_found\",\"error\":\"DEBUG_SESSION_NOT_FOUND\"}");
        Assert.True(JsonNode.DeepEquals(expected, result["structuredContent"]));
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])))["text"]?.GetValue<string>();
        Assert.True(JsonNode.DeepEquals(expected, JsonNode.Parse(text!)));
        Assert.Equal("text", result["content"]![0]!["type"]?.GetValue<string>());
    }

    private static void AssertSafe(string diagnostics)
    {
        Assert.DoesNotContain(PrivateMarker, diagnostics, StringComparison.Ordinal);
        Assert.DoesNotContain("rawPayload", diagnostics, StringComparison.Ordinal);
        Assert.DoesNotContain("arguments", diagnostics, StringComparison.Ordinal);
        Assert.DoesNotContain("message", diagnostics, StringComparison.OrdinalIgnoreCase);
    }
}
