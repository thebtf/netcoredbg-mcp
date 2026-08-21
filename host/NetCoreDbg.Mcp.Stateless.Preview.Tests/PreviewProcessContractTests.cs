using ModelContextProtocol;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Preview.Tests;

public sealed class PreviewProcessContractTests
{
    private const string ToolName = "find_code_symbol";

    [Fact]
    public async Task Discover_IsSupportedAsTheLiteralFirstFreshProcessRequest()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var result = RequireResult(await driver.DiscoverAsync(new RequestId("discover-first")));

        Assert.Equal(["tools"], result["capabilities"]!.AsObject().Select(static property => property.Key));
        Assert.Equal(300_000L, result["ttlMs"]!.GetValue<long>());
        Assert.Equal("public", result["cacheScope"]!.GetValue<string>());
    }

    [Fact]
    public async Task ListTools_IsSupportedAsTheLiteralFirstFreshProcessRequest_AndIsClosed()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var result = RequireResult(await driver.ListToolsAsync(new RequestId("list-first")));

        AssertCatalog(result);
    }

    [Fact]
    public async Task ValidCall_IsSupportedAsTheLiteralFirstFreshProcessRequest_WithExactStructuredTextParity()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var result = RequireResult(await driver.CallToolAsync(
            ToolName,
            new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "class" },
            new RequestId("call-first")));

        Assert.Equal("complete", result["resultType"]!.GetValue<string>());
        Assert.False(result["isError"]!.GetValue<bool>());
        var structured = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal(
            "{\"kind\":\"find_code_symbol_success\",\"results\":[{\"file\":\"Markers.cs\",\"line\":3,\"name\":\"PreviewMarker\",\"kind\":\"class\",\"context\":\"public sealed class PreviewMarker { }\"}]}",
            structured.ToJsonString());
        Assert.Equal(structured.ToJsonString(), SingleText(result));
    }

    [Fact]
    public async Task SupportedMetadataIsPerRequest_AndUnsupportedVersionReturnsExactJsonRpcError()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var unsupported = PreviewMcpProcessDriver.CurrentMeta();
        unsupported["io.modelcontextprotocol/protocolVersion"] = "1900-01-01";

        var rejected = await driver.ListToolsAsync(new RequestId("unsupported"), unsupported);
        var accepted = RequireResult(await driver.ListToolsAsync(new RequestId("supported")));

        var error = Assert.IsType<JsonRpcError>(rejected);
        Assert.Equal(-32022, error.Error.Code);
        Assert.Equal("Unsupported protocol version", error.Error.Message);
        var data = Assert.IsType<JsonElement>(error.Error.Data);
        Assert.Equal(["requested", "supported"], data.EnumerateObject().Select(static property => property.Name).Order());
        Assert.Equal("1900-01-01", data.GetProperty("requested").GetString());
        Assert.Equal([PreviewMcpProcessDriver.CurrentProtocolVersion], data.GetProperty("supported").EnumerateArray().Select(static value => value.GetString()));
        AssertCatalog(accepted);
    }

    [Fact]
    public async Task InvalidToolArguments_ReturnClosedRedactedErrorsWithoutPartialResults()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var tooLong = string.Concat(Enumerable.Repeat(char.ConvertFromUtf32(0x1F600), 128)) + "x";
        var invalidArguments = new JsonObject?[]
        {
            null,
            new JsonObject(),
            new JsonObject { ["name"] = null },
            new JsonObject { ["name"] = 3 },
            new JsonObject { ["name"] = " " },
            new JsonObject { ["name"] = tooLong },
            new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "event" },
            new JsonObject { ["name"] = "PreviewMarker", ["kind"] = 4 },
            new JsonObject { ["name"] = "PreviewMarker", ["extra"] = true },
        };

        foreach (var arguments in invalidArguments)
        {
            var result = RequireResult(await driver.CallToolAsync(
                ToolName,
                arguments,
                new RequestId($"invalid-{Guid.NewGuid():N}")));
            AssertClosedError(result, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS");
        }
    }

    [Fact]
    public async Task UnknownAndExcludedRoutes_AreNotRegisteredOrDispatched()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var unknown = RequireResult(await driver.CallToolAsync(
            "start_debug",
            new JsonObject(),
            new RequestId("unknown-tool")));
        Assert.Equal("complete", unknown["resultType"]!.GetValue<string>());
        Assert.True(unknown["isError"]!.GetValue<bool>());
        Assert.False(unknown.ContainsKey("structuredContent"));
        Assert.Equal("Unknown tool: start_debug", SingleText(unknown));

        var excluded = await driver.SendAsync(
            "resources/list",
            new JsonObject { ["_meta"] = PreviewMcpProcessDriver.CurrentMeta() },
            new RequestId("excluded-method"));
        Assert.Equal(-32601, Assert.IsType<JsonRpcError>(excluded).Error.Code);
    }
    [Fact]
    public async Task InBudget257CharacterUnsupportedVersionPreservesRequestedValueCorrelationAndFrameCap()
    {
        const int maximumFrameBytes = 256 * 1024;
        var requestedVersion = new string('v', 257);
        var requestId = new RequestId("in-budget-version");
        var metadata = PreviewMcpProcessDriver.CurrentMeta();
        metadata[MetaKeys.ProtocolVersion] = requestedVersion;

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var response = await driver.ListToolsAsync(requestId, metadata);

        var error = Assert.IsType<JsonRpcError>(response);
        Assert.Equal(requestId, error.Id);
        Assert.Equal(-32022, error.Error.Code);
        Assert.Equal("Unsupported protocol version", error.Error.Message);
        var data = Assert.IsType<JsonElement>(error.Error.Data);
        Assert.Equal(requestedVersion, data.GetProperty("requested").GetString());
        Assert.Equal([PreviewMcpProcessDriver.CurrentProtocolVersion], data.GetProperty("supported").EnumerateArray().Select(static value => value.GetString()));
        Assert.True(FrameByteCount(response) <= maximumFrameBytes);
    }

    [Fact]
    public async Task InBudget257CharacterUnknownToolPreservesFullTextCorrelationAndFrameCap()
    {
        const int maximumFrameBytes = 256 * 1024;
        var unknownToolName = new string('t', 257);
        var requestId = new RequestId("in-budget-unknown-tool");
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var response = await driver.CallToolAsync(unknownToolName, new JsonObject(), requestId);

        var result = RequireResult(response);
        Assert.Equal(requestId, Assert.IsType<JsonRpcResponse>(response).Id);
        Assert.Equal($"Unknown tool: {unknownToolName}", SingleText(result));
        Assert.True(FrameByteCount(response) <= maximumFrameBytes);
    }

    [Fact]
    public async Task ProjectArgumentIsTheOnlyAuthority_AndClientRootsRemainExcluded()
    {
        using var outside = TemporaryProject.Create("OutsideMarker");
        var hostileWorkingDirectory = outside.Path;
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(
            PreviewRepositoryLayout.FixtureRoot,
            hostileWorkingDirectory,
            new Dictionary<string, string?>
            {
                ["NETCOREDBG_MCP_PROJECT"] = outside.Path,
                ["PREVIEW_PROJECT"] = outside.Path,
            });

        var selected = RequireResult(await driver.CallToolAsync(
            ToolName,
            new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "class" },
            new RequestId("selected-root")));
        Assert.Equal("find_code_symbol_success", selected["structuredContent"]!["kind"]!.GetValue<string>());

        var outsideResult = RequireResult(await driver.CallToolAsync(
            ToolName,
            new JsonObject { ["name"] = "OutsideMarker", ["kind"] = "class" },
            new RequestId("hostile-root")));
        Assert.Equal("find_code_symbol_success", outsideResult["structuredContent"]!["kind"]!.GetValue<string>());
        Assert.Empty(outsideResult["structuredContent"]!["results"]!.AsArray());

        var roots = await driver.SendAsync(
            "roots/list",
            new JsonObject { ["_meta"] = PreviewMcpProcessDriver.CurrentMeta() },
            new RequestId("client-roots"));
        Assert.Equal(-32601, Assert.IsType<JsonRpcError>(roots).Error.Code);
    }

    [Fact]
    public async Task EscapingReparseEntryReturnsPathRefusedWithoutEarlierPartialResult()
    {
        using var root = TemporaryProject.Create("ContainedMarker");
        using var outside = TemporaryProject.Create("EscapingMarker");
        var escapingFile = Path.Combine(root.Path, "ZEscaping.cs");
        File.CreateSymbolicLink(escapingFile, Path.Combine(outside.Path, "Marker.cs"));

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        var result = RequireResult(await driver.CallToolAsync(
            ToolName,
            new JsonObject { ["name"] = "ContainedMarker", ["kind"] = "class" },
            new RequestId("escaping-entry")));

        AssertClosedError(result, "preview_path_refused", "PREVIEW_PATH_REFUSED");
        Assert.DoesNotContain("ContainedMarker", SingleText(result), StringComparison.Ordinal);
        Assert.DoesNotContain(outside.Path, SingleText(result), StringComparison.Ordinal);
    }

    [Fact]
    public async Task UnreadableSourceAfterAnEarlierMatchReturnsClosedErrorWithoutPartialResult()
    {
        using var root = TemporaryProject.Create("PartialMarker");
        var locked = Path.Combine(root.Path, "ZLocked.cs");
        File.WriteAllText(locked, "public sealed class LockedMarker { }\n", new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        using var lockHandle = new FileStream(locked, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);

        var result = RequireResult(await driver.CallToolAsync(
            ToolName,
            new JsonObject { ["name"] = "PartialMarker", ["kind"] = "class" },
            new RequestId("locked-source")));

        AssertClosedError(result, "preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE");
        Assert.DoesNotContain("PartialMarker", SingleText(result), StringComparison.Ordinal);
        Assert.DoesNotContain(root.Path, SingleText(result), StringComparison.Ordinal);
    }

    [Fact]
    public async Task EveryResponsePathKeepsHugeToolAndVersionInputsWithinTheCompleteFrameCap()
    {
        const int maximumFrameBytes = 256 * 1024;
        var unboundedTool = new string('t', maximumFrameBytes + 1);
        var unboundedVersion = new string('v', maximumFrameBytes + 1);
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var unsupportedMetadata = PreviewMcpProcessDriver.CurrentMeta();
        unsupportedMetadata[MetaKeys.ProtocolVersion] = unboundedVersion;
        var unsupportedVersion = await driver.ListToolsAsync(new RequestId("huge-version"), unsupportedMetadata);
        var unknownTool = await driver.CallToolAsync(
            unboundedTool,
            new JsonObject(),
            new RequestId("huge-tool"));

        Assert.True(FrameByteCount(unknownTool) <= maximumFrameBytes);
        Assert.True(FrameByteCount(unsupportedVersion) <= maximumFrameBytes);
        var oversizedUnknown = Assert.IsType<JsonRpcResponse>(unknownTool);
        Assert.Equal(new RequestId("huge-tool"), oversizedUnknown.Id);
        AssertClosedError(RequireResult(oversizedUnknown), "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED");
        Assert.DoesNotContain(unboundedTool, JsonSerializer.Serialize(unknownTool, McpJsonUtilities.DefaultOptions), StringComparison.Ordinal);
        var oversizedVersion = Assert.IsType<JsonRpcError>(unsupportedVersion);
        Assert.Equal(new RequestId("huge-version"), oversizedVersion.Id);
        Assert.Equal("Unsupported protocol version", oversizedVersion.Error.Message);
        Assert.Equal(-32022, oversizedVersion.Error.Code);
        var oversizedVersionData = Assert.IsType<JsonElement>(oversizedVersion.Error.Data);
        Assert.Equal("unsupported", oversizedVersionData.GetProperty("requested").GetString());
        Assert.Equal([PreviewMcpProcessDriver.CurrentProtocolVersion], oversizedVersionData.GetProperty("supported").EnumerateArray().Select(static value => value.GetString()));
        Assert.DoesNotContain(unboundedVersion, JsonSerializer.Serialize(unsupportedVersion, McpJsonUtilities.DefaultOptions), StringComparison.Ordinal);
    }

    [Fact]
    public async Task Exact256KiBCompleteJsonRpcFrameIsAccepted()
    {
        const int maximumFrameBytes = 256 * 1024;
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var exactIdLength = maximumFrameBytes - FrameByteCount(FrameBudgetFallback(new RequestId("base"))) + "base".Length;
        var exact = await driver.ListToolsAsync(new RequestId(new string('i', exactIdLength)));

        Assert.Equal(maximumFrameBytes, FrameByteCount(exact));
    }

    [Fact]
    public async Task NearCapRequestIdsUseSameIdFallbackOrCloseBeforeDispatchAndFreshProcessRemainsUsable()
    {
        const int maximumFrameBytes = 256 * 1024;
        var fallbackIdLength = maximumFrameBytes - FrameByteCount(FrameBudgetFallback(new RequestId("base"))) + "base".Length;
        var fallbackId = new RequestId(new string('i', fallbackIdLength));
        var unfitFallbackId = new RequestId(new string('i', fallbackIdLength + 1));

        Assert.Equal(maximumFrameBytes, FrameByteCount(FrameBudgetFallback(fallbackId)));
        Assert.True(FrameByteCount(new JsonRpcResponse { Id = unfitFallbackId, Result = null }) <= maximumFrameBytes);
        Assert.True(FrameByteCount(FrameBudgetFallback(unfitFallbackId)) > maximumFrameBytes);

        await using (var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot))
        {
            var fallback = await driver.ListToolsAsync(fallbackId);
            Assert.Equal(fallbackId, Assert.IsType<JsonRpcResponse>(fallback).Id);
            AssertClosedError(RequireResult(fallback), "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED");

            await driver.SendRequestAsync(
                "tools/list",
                new JsonObject { ["_meta"] = PreviewMcpProcessDriver.CurrentMeta() },
                unfitFallbackId);
            Assert.Null(await driver.TryReadMessageAsync(TimeSpan.FromSeconds(1)));
            Assert.True(await driver.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
        }

        await using var fresh = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        AssertCatalog(RequireResult(await fresh.ListToolsAsync(new RequestId("fresh-after-near-cap-id"))));
    }

    [Fact]
    public async Task OversizedRequestIdIsRefusedWithoutAnyResponseAndAFreshProcessRemainsUsable()
    {
        const int maximumFrameBytes = 256 * 1024;
        await using (var rejected = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot))
        {
            await rejected.SendRequestAsync(
                "tools/list",
                new JsonObject { ["_meta"] = PreviewMcpProcessDriver.CurrentMeta() },
                new RequestId(new string('i', maximumFrameBytes + 1)));

            Assert.Null(await rejected.TryReadMessageAsync(TimeSpan.FromSeconds(1)));
            Assert.True(await rejected.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
        }

        await using var fresh = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        AssertCatalog(RequireResult(await fresh.ListToolsAsync(new RequestId("fresh-after-oversized-id"))));
    }

    [Fact]
    public async Task LegacyInitializeIsMethodNotFound()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var response = await driver.SendAsync("initialize", new JsonObject(), new RequestId("legacy-initialize"));

        Assert.Equal(-32601, Assert.IsType<JsonRpcError>(response).Error.Code);
    }

    [Fact]
    public async Task CancellationNotificationWinsBeforeAnyToolResultAndTheProcessRemainsUsable()
    {
        using var root = TemporaryProject.CreateCancellationLoad();
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        var id = new RequestId("cancelled-call");

        await driver.SendRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = ToolName,
                ["arguments"] = new JsonObject { ["name"] = "CancellationMarker", ["kind"] = "class" },
                ["_meta"] = PreviewMcpProcessDriver.CurrentMeta(),
            },
            id);
        await driver.SendCancellationAsync(id);

        Assert.Null(await driver.TryReadResponseAsync(id, TimeSpan.FromSeconds(2)));
        AssertCatalog(RequireResult(await driver.ListToolsAsync(new RequestId("after-cancellation"))));
    }

    [Fact]
    public async Task CompleteJsonRpcResponseFrameCapMapsOversizedResultToBudgetError()
    {
        using var root = TemporaryProject.Create("FrameLimitMarker", string.Concat(Enumerable.Repeat(
            "public sealed class FrameLimitMarker { } // " + string.Concat(Enumerable.Repeat(char.ConvertFromUtf32(0x1F600), 500)) + "\n",
            128)));
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);

        var result = RequireResult(await driver.CallToolAsync(
            ToolName,
            new JsonObject { ["name"] = "FrameLimitMarker", ["kind"] = "class" },
            new RequestId("frame-cap")));

        AssertClosedError(result, "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED");
    }

    [Theory]
    [InlineData()]
    [InlineData("--project")]
    [InlineData("relative")]
    [InlineData("C:\\does-not-exist")]
    [InlineData("\\\\localhost\\share")]
    [InlineData("\\\\?\\C:\\extended")]
    [InlineData("\\\\.\\C:\\device")]
    [InlineData("\\\\?\\Volume{00000000-0000-0000-0000-000000000000}\\")]
    [InlineData("//localhost/share")]
    [InlineData("//?/C:/extended")]
    [InlineData("//./C:/device")]
    [InlineData("//?/Volume{00000000-0000-0000-0000-000000000000}/")]
    [InlineData("//?/UNC/localhost/share")]
    public async Task InvalidLaunchAuthority_Exits64WithExactStderrAndNoStdout(params string[] arguments)
    {
        if (arguments.Length == 1 && arguments[0] == "relative")
        {
            arguments = ["--project", arguments[0]];
        }
        else if (arguments.Length == 1 && arguments[0] != "--project")
        {
            arguments = ["--project", arguments[0]];
        }

        using var process = PreviewOutputPathResolver.StartDirect(arguments);
        var standardOutput = process.StandardOutput.ReadToEndAsync();
        var standardError = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(2));

        Assert.Equal(64, process.ExitCode);
        Assert.Equal("", await standardOutput);
        Assert.Equal("PREVIEW_ROOT_INVALID\n", await standardError);
    }

    [Fact]
    public async Task RawRelativeDriveRelativeAndRootRelativeProjectArgumentsAreRefusedBeforeCanonicalization()
    {
        var root = PreviewRepositoryLayout.FixtureRoot;
        var driveRelative = string.Concat(root.AsSpan(0, 2), ".");
        var rootRelative = root[2..];

        foreach (var rawPath in new[] { ".", driveRelative, rootRelative })
        {
            await AssertInvalidLaunchAsync(root, "--project", rawPath);
        }
    }

    [Fact]
    public async Task ReparseComponentWithinSelectedProjectPathIsRefusedBeforePreviewStartup()
    {
        using var target = TemporaryProject.Create("AliasedRootMarker");
        var nested = Path.Combine(target.Path, "Nested");
        Directory.CreateDirectory(nested);
        var container = Path.Combine(Path.GetTempPath(), $"netcoredbg-preview-alias-{Guid.NewGuid():N}");
        Directory.CreateDirectory(container);
        var link = Path.Combine(container, "alias");
        Directory.CreateSymbolicLink(link, target.Path);
        try
        {
            await AssertInvalidLaunchAsync(PreviewRepositoryLayout.Root, "--project", Path.Combine(link, "Nested"));
        }
        finally
        {
            Directory.Delete(container, recursive: true);
        }
    }

    [Fact]
    public async Task RawReparseAliasBeforeDotNormalizationIsRefusedBeforePreviewStartup()
    {
        using var target = TemporaryProject.Create("RawAliasTargetMarker");
        var container = Path.Combine(Path.GetTempPath(), $"netcoredbg-preview-raw-alias-{Guid.NewGuid():N}");
        var alias = Path.Combine(container, "reparseAlias");
        var real = Path.Combine(container, "real");
        Directory.CreateDirectory(real);
        File.WriteAllText(Path.Combine(real, "Marker.cs"), "public sealed class RealMarker { }\n");
        Directory.CreateSymbolicLink(alias, target.Path);
        try
        {
            foreach (var rawPath in new[]
            {
                string.Concat(alias, Path.DirectorySeparatorChar, "."),
                string.Concat(alias, Path.DirectorySeparatorChar, "..", Path.DirectorySeparatorChar, "real"),
            })
            {
                await AssertInvalidLaunchAsync(PreviewRepositoryLayout.Root, "--project", rawPath);
            }
        }
        finally
        {
            Directory.Delete(container, recursive: true);
        }
    }

    [Fact]
    public async Task MultipleAndReparseLaunchRoots_AreRefusedBeforeServing()
    {
        using var target = TemporaryProject.Create("ReparseRootMarker");
        var linkPath = Path.Combine(Path.GetTempPath(), $"netcoredbg-preview-link-{Guid.NewGuid():N}");
        Directory.CreateSymbolicLink(linkPath, target.Path);
        try
        {
            foreach (var arguments in new[]
            {
                new[] { "--project", PreviewRepositoryLayout.FixtureRoot, "--project", target.Path },
                new[] { "--project", linkPath },
            })
            {
                using var process = PreviewOutputPathResolver.StartDirect(arguments);
                var output = process.StandardOutput.ReadToEndAsync();
                var error = process.StandardError.ReadToEndAsync();
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(2));
                Assert.Equal(64, process.ExitCode);
                Assert.Equal("", await output);
                Assert.Equal("PREVIEW_ROOT_INVALID\n", await error);
            }
        }
        finally
        {
            Directory.Delete(linkPath);
        }
    }

    [Fact]
    public async Task EndOfFile_ExitsBoundedlyWithoutStdoutOrStderr()
    {
        using var process = PreviewOutputPathResolver.StartDirect("--project", PreviewRepositoryLayout.FixtureRoot);
        var output = process.StandardOutput.ReadToEndAsync();
        var error = process.StandardError.ReadToEndAsync();
        process.StandardInput.Close();

        await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(2));

        Assert.Equal(0, process.ExitCode);
        Assert.Equal("", await output);
        Assert.Equal("", await error);
    }

    private static JsonObject RequireResult(JsonRpcMessage message) =>
        Assert.IsType<JsonObject>(Assert.IsType<JsonRpcResponse>(message).Result);

    private static void AssertCatalog(JsonObject result)
    {
        var tool = Assert.IsType<JsonObject>(Assert.Single(result["tools"]!.AsArray()));
        Assert.Equal(ToolName, tool["name"]!.GetValue<string>());
        Assert.Equal(300_000L, result["ttlMs"]!.GetValue<long>());
        Assert.Equal("public", result["cacheScope"]!.GetValue<string>());
        var annotations = tool["annotations"]!.AsObject();
        Assert.True(annotations["readOnlyHint"]!.GetValue<bool>());
        Assert.True(annotations["idempotentHint"]!.GetValue<bool>());
        Assert.False(annotations["openWorldHint"]!.GetValue<bool>());
        var schema = tool["inputSchema"]!.AsObject();
        Assert.Equal("object", schema["type"]!.GetValue<string>());
        Assert.False(schema["additionalProperties"]!.GetValue<bool>());
        Assert.Equal(["name"], schema["required"]!.AsArray().Select(static value => value!.GetValue<string>()));
        var properties = schema["properties"]!.AsObject();
        Assert.Equal(1, properties["name"]!["minLength"]!.GetValue<int>());
        Assert.Equal(256, properties["name"]!["maxLength"]!.GetValue<int>());
        Assert.Equal(["string", "null"], properties["kind"]!["type"]!.AsArray().Select(static value => value!.GetValue<string>()));
        Assert.Equal(["class", "method", "property", "field", null], properties["kind"]!["enum"]!.AsArray().Select(static value => value?.GetValue<string>()));
    }

    private static void AssertClosedError(JsonObject result, string kind, string error)
    {
        Assert.Equal("complete", result["resultType"]!.GetValue<string>());
        Assert.True(result["isError"]!.GetValue<bool>());
        var structured = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal(["kind", "error", "tool"], structured.Select(static property => property.Key));
        Assert.Equal(kind, structured["kind"]!.GetValue<string>());
        Assert.Equal(error, structured["error"]!.GetValue<string>());
        Assert.Equal(ToolName, structured["tool"]!.GetValue<string>());
        Assert.Equal(structured.ToJsonString(), SingleText(result));
    }

    private static string SingleText(JsonObject result)
    {
        var content = Assert.IsType<JsonObject>(Assert.Single(result["content"]!.AsArray()));
        Assert.Equal("text", content["type"]!.GetValue<string>());
        return content["text"]!.GetValue<string>();
    }

    private static int FrameByteCount(JsonRpcMessage message) =>
        JsonSerializer.SerializeToUtf8Bytes(message, McpJsonUtilities.DefaultOptions).Length;
    private static JsonRpcResponse FrameBudgetFallback(RequestId id) => new()
    {
        Id = id,
        Result = JsonSerializer.SerializeToNode(PreviewToolHandler.FrameBudgetExceeded(), McpJsonUtilities.DefaultOptions),
    };


    private static async Task AssertInvalidLaunchAsync(string workingDirectory, params string[] arguments)
    {
        using var process = PreviewOutputPathResolver.StartDirectIn(workingDirectory, arguments);
        var standardOutput = process.StandardOutput.ReadToEndAsync();
        var standardError = process.StandardError.ReadToEndAsync();
        try
        {
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(2));
        }
        finally
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync();
            }
        }

        Assert.Equal(64, process.ExitCode);
        Assert.Equal("", await standardOutput);
        Assert.Equal("PREVIEW_ROOT_INVALID\n", await standardError);
    }

    private sealed class TemporaryProject : IDisposable
    {
        private TemporaryProject(string path)
        {
            Path = path;
        }

        internal string Path { get; }

        internal static TemporaryProject Create(string marker, string? source = null)
        {
            var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"netcoredbg-preview-{Guid.NewGuid():N}");
            Directory.CreateDirectory(path);
            File.WriteAllText(
                System.IO.Path.Combine(path, "Marker.cs"),
                source ?? $"public sealed class {marker} {{ }}\n",
                new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            return new TemporaryProject(path);
        }

        internal static TemporaryProject CreateCancellationLoad()
        {
            var project = Create("CancellationMarker", source: string.Empty);
            var source = string.Concat(Enumerable.Repeat("// cancellation load padding\n", 32_768));
            for (var index = 0; index < 16; index++)
            {
                File.WriteAllText(
                    System.IO.Path.Combine(project.Path, $"Load{index:D2}.cs"),
                    source,
                    new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
            }

            return project;
        }

        public void Dispose()
        {
            if (Directory.Exists(Path))
            {
                Directory.Delete(Path, recursive: true);
            }
        }
    }
}
