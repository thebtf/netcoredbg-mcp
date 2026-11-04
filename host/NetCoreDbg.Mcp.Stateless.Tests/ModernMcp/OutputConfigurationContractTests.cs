using System.Diagnostics;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class OutputConfigurationContractTests
{
    private const string EntryLimit = "NETCOREDBG_MAX_OUTPUT_ENTRY";
    private const string TotalLimit = "NETCOREDBG_MAX_OUTPUT_BYTES";
    private const string InvalidSentinel = "invalid-output-limit-sentinel";
    private const string DiscoveryId = "output-configuration-discovery";
    private static readonly TimeSpan ProcessTimeout = TimeSpan.FromSeconds(5);

    [Theory]
    [InlineData(EntryLimit, "")]
    [InlineData(EntryLimit, InvalidSentinel)]
    [InlineData(TotalLimit, "")]
    [InlineData(TotalLimit, InvalidSentinel)]
    public async Task Startup_MalformedOutputLimit_ExitsNonzeroBeforeServingAndNamesOnlyTheKey(
        string invalidKey,
        string invalidValue)
    {
        var entry = invalidKey == EntryLimit ? invalidValue : "100000";
        var total = invalidKey == TotalLimit ? invalidValue : "10000000";
        await WithHostAsync(entry, total, async (process, standardError, token) =>
        {
            // A startup refusal may close its pipe before this first request can be written.
            try
            {
                await SendDiscoveryAsync(process, token);
            }
            catch (IOException)
            {
            }

            // Keep stdin open: EOF or client disposal must not cause the asserted startup exit.
            var standardOutput = process.StandardOutput.ReadToEndAsync(token);
            try
            {
                await process.WaitForExitAsync(token);
            }
            catch (OperationCanceledException) when (token.IsCancellationRequested)
            {
                Assert.Fail($"The ordinary MCP host did not reject malformed {invalidKey} during startup within {ProcessTimeout}.");
            }

            Assert.NotEqual(0, process.ExitCode);
            Assert.Empty(await standardOutput);
            var diagnostic = await standardError.WaitAsync(token);
            Assert.Contains(invalidKey, diagnostic, StringComparison.Ordinal);
            if (invalidValue.Length != 0)
            {
                Assert.DoesNotContain(invalidValue, diagnostic, StringComparison.Ordinal);
            }
        });
    }

    [Theory]
    [InlineData("100_000", "10_000_000")]
    [InlineData("0", "0")]
    [InlineData("-1", "-1")]
    public async Task Startup_ValidOutputLimits_ServesDiscoveryWithoutLaunchingDebugger(
        string entryLimit,
        string totalLimit)
    {
        await WithHostAsync(entryLimit, totalLimit, async (process, standardError, token) =>
        {
            await SendDiscoveryAsync(process, token);
            var line = await process.StandardOutput.ReadLineAsync(token);
            Assert.NotNull(line);
            var response = Assert.IsType<JsonObject>(JsonNode.Parse(line));
            Assert.Equal("2.0", response["jsonrpc"]?.GetValue<string>());
            Assert.Equal(DiscoveryId, response["id"]?.GetValue<string>());
            Assert.False(response.ContainsKey("error"), line);
            var result = Assert.IsType<JsonObject>(response["result"]);
            Assert.IsType<JsonObject>(result["capabilities"]?["tools"]);

            process.StandardInput.Close();
            var remainingOutput = process.StandardOutput.ReadToEndAsync(token);
            await process.WaitForExitAsync(token);
            Assert.Equal(0, process.ExitCode);
            Assert.Empty(await remainingOutput);
            var diagnostic = await standardError.WaitAsync(token);
            Assert.DoesNotContain(EntryLimit, diagnostic, StringComparison.Ordinal);
            Assert.DoesNotContain(TotalLimit, diagnostic, StringComparison.Ordinal);
        });
    }

    [Theory]
    [InlineData("session", "\\uD800")]
    [InlineData("session", "\\uDC00")]
    [InlineData("filter", "\\uD800")]
    [InlineData("filter", "\\uDC00")]
    [InlineData("session-object", "\\uD800")]
    [InlineData("filter-object", "\\uD800")]
    [InlineData("unexpected", "\\uD800")]
    public async Task ConfigureExceptions_UnpairedSurrogate_ReturnsInvalidArguments(string field, string escaped)
    {
        await WithHostAsync("100000", "10000000", async (process, standardError, token) =>
        {
            var arguments = field switch
            {
                "session" => $"{{\"debugSessionId\":\"{escaped}\",\"filters\":[]}}",
                "filter" => $"{{\"debugSessionId\":\"missing\",\"filters\":[\"{escaped}\"]}}",
                "session-object" => $"{{\"debugSessionId\":{{\"nested\":\"{escaped}\"}},\"filters\":[]}}",
                "filter-object" => $"{{\"debugSessionId\":\"missing\",\"filters\":[{{\"nested\":\"{escaped}\"}}]}}",
                _ => $"{{\"debugSessionId\":\"missing\",\"unexpected\":{{\"nested\":\"{escaped}\"}}}}",
            };
            var request = "{\"jsonrpc\":\"2.0\",\"id\":\"unicode\",\"method\":\"tools/call\",\"params\":{\"name\":\"configure_exceptions\",\"arguments\":"
                + arguments + ",\"_meta\":" + ModernMcpProcessDriver.CurrentMeta().ToJsonString() + "}}";
            await process.StandardInput.WriteLineAsync(request.AsMemory(), token);
            await process.StandardInput.FlushAsync(token);
            var line = await process.StandardOutput.ReadLineAsync(token);
            Assert.NotNull(line);
            var response = Assert.IsType<JsonObject>(JsonNode.Parse(line));
            process.StandardInput.Close();
            await process.WaitForExitAsync(token);
            var diagnostic = await standardError.WaitAsync(token);
            Assert.False(response.ContainsKey("error"), $"{line}\n{diagnostic}");
            var result = Assert.IsType<JsonObject>(response["result"]);
            Assert.True(result["isError"]?.GetValue<bool>());
            Assert.Equal("invalid_tool_arguments", result["structuredContent"]?["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", result["structuredContent"]?["error"]?.GetValue<string>());
        });
    }

    [Fact]
    public async Task ConfigureExceptions_DuplicateArgumentProperty_ReturnsInvalidArguments()
    {
        await WithHostAsync("100000", "10000000", async (process, standardError, token) =>
        {
            var request = "{\"jsonrpc\":\"2.0\",\"id\":\"duplicate\",\"method\":\"tools/call\",\"params\":{\"name\":\"configure_exceptions\",\"arguments\":{\"debugSessionId\":\"missing\",\"filters\":[],\"filters\":[]},\"_meta\":"
                + ModernMcpProcessDriver.CurrentMeta().ToJsonString() + "}}";
            await process.StandardInput.WriteLineAsync(request.AsMemory(), token);
            await process.StandardInput.FlushAsync(token);
            var line = await process.StandardOutput.ReadLineAsync(token);
            Assert.NotNull(line);
            var response = Assert.IsType<JsonObject>(JsonNode.Parse(line));
            process.StandardInput.Close();
            await process.WaitForExitAsync(token);
            var diagnostic = await standardError.WaitAsync(token);
            Assert.False(response.ContainsKey("error"), $"{line}\n{diagnostic}");
            var result = Assert.IsType<JsonObject>(response["result"]);
            Assert.True(result["isError"]?.GetValue<bool>());
            Assert.Equal("invalid_tool_arguments", result["structuredContent"]?["kind"]?.GetValue<string>());
            Assert.Equal("INVALID_TOOL_ARGUMENTS", result["structuredContent"]?["error"]?.GetValue<string>());
        });
    }

    private static async Task SendDiscoveryAsync(Process process, CancellationToken cancellationToken)
    {
        var request = new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = DiscoveryId,
            ["method"] = "server/discover",
            ["params"] = new JsonObject { ["_meta"] = ModernMcpProcessDriver.CurrentMeta() },
        };
        await process.StandardInput.WriteLineAsync(request.ToJsonString().AsMemory(), cancellationToken);
        await process.StandardInput.FlushAsync(cancellationToken);
    }

    private static async Task WithHostAsync(
        string entryLimit,
        string totalLimit,
        Func<Process, Task<string>, CancellationToken, Task> assertContract)
    {
        var candidate = TestOutputPathResolver.ResolveProcess(
            Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless"),
            "NetCoreDbg.Mcp.Stateless");
        var scratchDirectory = ModernMcpScratchDirectory.Create();
        using var process = new Process
        {
            StartInfo = new ProcessStartInfo(candidate.Command)
            {
                WorkingDirectory = RepositoryLayout.Root,
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            },
        };
        foreach (var argument in candidate.Arguments)
        {
            process.StartInfo.ArgumentList.Add(argument);
        }

        // Override both limits only on this child so unrelated inherited configuration cannot mask the key under test.
        process.StartInfo.Environment[EntryLimit] = entryLimit;
        process.StartInfo.Environment[TotalLimit] = totalLimit;
        process.StartInfo.Environment["NETCOREDBG_PATH"] = Path.Combine(scratchDirectory, "absent-debugger");
        process.StartInfo.Environment["FLAUI_BRIDGE_PATH"] = Path.Combine(scratchDirectory, "absent-bridge");
        process.StartInfo.Environment["NETCOREDBG_MCP_ARTIFACT_ROOT"] = Path.Combine(scratchDirectory, "artifacts");
        process.StartInfo.Environment["DOTNET_ADD_GLOBAL_TOOLS_TO_PATH"] = "false";
        process.StartInfo.Environment["DOTNET_NOLOGO"] = "1";
        var started = false;
        Task<string>? standardError = null;
        try
        {
            Assert.True(process.Start());
            started = true;
            standardError = process.StandardError.ReadToEndAsync();
            using var deadline = new CancellationTokenSource(ProcessTimeout);
            await assertContract(process, standardError, deadline.Token);
        }
        finally
        {
            try
            {
                if (started)
                {
                    if (!process.HasExited)
                    {
                        try
                        {
                            process.Kill(entireProcessTree: true);
                        }
                        catch (InvalidOperationException) when (process.HasExited)
                        {
                        }
                    }

                    using var cleanupDeadline = new CancellationTokenSource(ProcessTimeout);
                    await process.WaitForExitAsync(cleanupDeadline.Token);
                    if (standardError is not null)
                    {
                        await standardError.WaitAsync(cleanupDeadline.Token);
                    }
                }
            }
            finally
            {
                await ModernMcpScratchDirectory.DeleteAsync(scratchDirectory);
            }
        }
    }
}
