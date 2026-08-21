using ModelContextProtocol;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.CodeSearch.Core;
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
    public async Task SuccessfulFindCodeSymbolWithLongPathAndEscapedContextPreservesParityAndFrameCap()
    {
        const int maximumFrameBytes = 256 * 1024;
        var directoryName = new string('p', 120);
        var escapedContext = string.Concat(Enumerable.Repeat("\"\\", 400));
        using var root = TemporaryProject.Create(
            "LongPayloadMarker",
            $"public sealed class LongPayloadMarker {{ }} // {escapedContext}\n");
        var directory = Path.Combine(root.Path, directoryName);
        Directory.CreateDirectory(directory);
        File.Move(Path.Combine(root.Path, "Marker.cs"), Path.Combine(directory, "LongPayload.cs"));
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);

        var response = await driver.CallToolAsync(
            ToolName,
            new JsonObject { ["name"] = "LongPayloadMarker", ["kind"] = "class" },
            new RequestId("long-success"));
        var result = RequireResult(response);
        var match = Assert.IsType<JsonObject>(Assert.Single(result["structuredContent"]!["results"]!.AsArray()));

        Assert.Contains(directoryName, match["file"]!.GetValue<string>(), StringComparison.Ordinal);
        Assert.InRange(match["context"]!.GetValue<string>().EnumerateRunes().Count(), 450, 512);
        Assert.Equal(result["structuredContent"]!.ToJsonString(), SingleText(result));
        Assert.True(FrameByteCount(response) <= maximumFrameBytes);
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
    public async Task MissingOrMalformedRequestLocalMetadataIsNeverDispatched()
    {
        var malformedRequests = new JsonNode?[]
        {
            null,
            new JsonObject(),
            new JsonObject { ["_meta"] = "malformed" },
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = 1, [MetaKeys.ClientInfo] = new JsonObject { ["name"] = "client", ["version"] = "1.0" }, [MetaKeys.ClientCapabilities] = new JsonObject() } },
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion, [MetaKeys.ClientInfo] = "malformed", [MetaKeys.ClientCapabilities] = new JsonObject() } },
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion, [MetaKeys.ClientInfo] = new JsonObject { ["name"] = "client", ["version"] = "1.0" }, [MetaKeys.ClientCapabilities] = "malformed" } },
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion, [MetaKeys.ClientCapabilities] = new JsonObject() } },
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion, [MetaKeys.ClientInfo] = new JsonObject { ["name"] = "client", ["version"] = "1.0" } } },
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion, [MetaKeys.ClientInfo] = new JsonObject { ["name"] = " ", ["version"] = "1.0" }, [MetaKeys.ClientCapabilities] = new JsonObject() } },
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion, [MetaKeys.ClientInfo] = new JsonObject { ["name"] = "client", ["version"] = " " }, [MetaKeys.ClientCapabilities] = new JsonObject() } },
        };

        foreach (var parameters in malformedRequests)
        {
            await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
            await driver.SendRequestAsync("tools/list", parameters, new RequestId($"malformed-meta-{Guid.NewGuid():N}"));

            var response = await driver.TryReadMessageAsync(TimeSpan.FromSeconds(2));
            if (response is null)
            {
                Assert.True(await driver.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
                continue;
            }

            Assert.IsType<JsonRpcError>(response);
        }
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
    public async Task OmittedToolCallParams_ReturnsClosedRedactedError()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var result = RequireResult(await driver.SendAsync("tools/call", parameters: null, new RequestId("omitted-tool-call-params")));

        AssertClosedError(result, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS");
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
        var whitespaceUnknown = RequireResult(await driver.CallToolAsync(
            " ",
            new JsonObject(),
            new RequestId("whitespace-unknown-tool")));
        Assert.True(whitespaceUnknown["isError"]!.GetValue<bool>());
        Assert.False(whitespaceUnknown.ContainsKey("structuredContent"));
        Assert.Equal("Unknown tool:  ", SingleText(whitespaceUnknown));



        var excluded = await driver.SendAsync(
            "resources/list",
            new JsonObject { ["_meta"] = PreviewMcpProcessDriver.CurrentMeta() },
            new RequestId("excluded-method"));
        Assert.Equal(-32601, Assert.IsType<JsonRpcError>(excluded).Error.Code);
    }
    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public async Task MissingOrNullToolNameReturnsClosedInvalidArguments(bool includeNullName)
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var parameters = new JsonObject
        {
            ["arguments"] = new JsonObject(),
            ["_meta"] = PreviewMcpProcessDriver.CurrentMeta(),
        };
        if (includeNullName)
        {
            parameters["name"] = null;
        }

        var result = RequireResult(await driver.SendAsync("tools/call", parameters, new RequestId($"missing-tool-name-{includeNullName}")));

        AssertClosedError(result, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS");
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
        Assert.True(BoundedResponseFrameSerializer.FitsUnsupportedVersionErrorWithinLimit(
            requestId,
            requestedVersion,
            PreviewMcpProcessDriver.CurrentProtocolVersion,
            out var state));
        Assert.Equal(FrameByteCount(response), state.BytesWritten);
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
        Assert.True(BoundedResponseFrameSerializer.FitsUnknownToolResponseWithinLimit(requestId, unknownToolName, out var state));
        Assert.Equal(FrameByteCount(response), state.BytesWritten);
        Assert.True(FrameByteCount(response) <= maximumFrameBytes);
    }

    [Fact]
    public async Task NearCapIdBeyondFallbackLimitDeliversExactUnsupportedVersionResponse()
    {
        const int maximumFrameBytes = 256 * 1024;
        var fallbackIdLength = maximumFrameBytes - FrameByteCount(FrameBudgetFallback(new RequestId("base"))) + "base".Length;
        var requestId = new RequestId(new string('i', fallbackIdLength + 1));
        var requestedVersion = new string('v', 257);
        var metadata = PreviewMcpProcessDriver.CurrentMeta();
        metadata[MetaKeys.ProtocolVersion] = requestedVersion;

        Assert.False(BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(requestId));
        Assert.True(BoundedResponseFrameSerializer.FitsUnsupportedVersionErrorWithinLimit(
            requestId,
            requestedVersion,
            PreviewMcpProcessDriver.CurrentProtocolVersion,
            out var expected));

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var response = await driver.CallToolAsync(
            ToolName,
            new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "class" },
            requestId,
            metadata);

        var error = Assert.IsType<JsonRpcError>(response);
        Assert.Equal(requestId, error.Id);
        Assert.Equal(-32022, error.Error.Code);
        var data = Assert.IsType<JsonElement>(error.Error.Data);
        Assert.Equal(requestedVersion, data.GetProperty("requested").GetString());
        Assert.Equal(FrameByteCount(response), expected.BytesWritten);
        Assert.True(FrameByteCount(response) <= maximumFrameBytes);
    }
    [Fact]
    public async Task NearCapIdBeyondFallbackLimitDeliversLegacyMethodNotFound()
    {
        const int maximumFrameBytes = 256 * 1024;
        var fallbackIdLength = maximumFrameBytes - FrameByteCount(FrameBudgetFallback(new RequestId("base"))) + "base".Length;
        var requestId = new RequestId(new string('i', fallbackIdLength + 1));
        var expected = LegacyInitializeMethodNotFound(requestId);

        Assert.False(BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(requestId));
        Assert.True(FrameByteCount(expected) <= maximumFrameBytes);

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var response = await driver.SendAsync("initialize", new JsonObject(), requestId);

        var error = Assert.IsType<JsonRpcError>(response);
        Assert.Equal(requestId, error.Id);
        Assert.Equal(-32601, error.Error.Code);
        Assert.Equal("Method not found", error.Error.Message);
        Assert.Equal(FrameByteCount(expected), FrameByteCount(error));
    }


    [Fact]
    public async Task ExactCapInvalidToolArgumentsResponseIsAccepted()
    {
        const int maximumFrameBytes = 256 * 1024;
        var exactIdLength = maximumFrameBytes - FrameByteCount(InvalidToolArgumentsResponse(new RequestId("base"))) + "base".Length;
        var requestId = new RequestId(new string('i', exactIdLength));
        var expected = InvalidToolArgumentsResponse(requestId);

        Assert.True(BoundedResponseFrameSerializer.FitsInvalidToolArgumentsResponseWithinLimit(requestId, out var state));
        Assert.Equal(maximumFrameBytes, state.BytesWritten);
        Assert.Equal(maximumFrameBytes, FrameByteCount(expected));

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var response = await driver.CallToolAsync(ToolName, new JsonObject(), requestId);

        Assert.Equal(requestId, Assert.IsType<JsonRpcResponse>(response).Id);
        AssertClosedError(RequireResult(response), "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS");
        Assert.Equal(maximumFrameBytes, FrameByteCount(response));
    }

    [Fact]
    public async Task NearCapIdBeyondFallbackLimitDeliversExcludedMethodNotFound()
    {
        const int maximumFrameBytes = 256 * 1024;
        var fallbackIdLength = maximumFrameBytes - FrameByteCount(FrameBudgetFallback(new RequestId("base"))) + "base".Length;
        var requestId = new RequestId(new string('i', fallbackIdLength + 1));
        var expected = LegacyInitializeMethodNotFound(requestId);

        Assert.False(BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(requestId));
        Assert.True(FrameByteCount(expected) <= maximumFrameBytes);

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var response = await driver.SendAsync("resources/list", new JsonObject { ["_meta"] = PreviewMcpProcessDriver.CurrentMeta() }, requestId);

        var error = Assert.IsType<JsonRpcError>(response);
        Assert.Equal(requestId, error.Id);
        Assert.Equal(-32601, error.Error.Code);
        Assert.Equal("Method not found", error.Error.Message);
        Assert.Equal(FrameByteCount(expected), FrameByteCount(error));
    }

    [Fact]
    public async Task NearCapIdBeyondFallbackLimitDeliversExactUnknownToolResponse()
    {
        const int maximumFrameBytes = 256 * 1024;
        var fallbackIdLength = maximumFrameBytes - FrameByteCount(FrameBudgetFallback(new RequestId("base"))) + "base".Length;
        var requestId = new RequestId(new string('i', fallbackIdLength + 1));
        const string unknownTool = "unknown";

        Assert.False(BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(requestId));
        Assert.True(BoundedResponseFrameSerializer.FitsUnknownToolResponseWithinLimit(requestId, unknownTool, out var expected));

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        await driver.SendRequestAsync(
            RequestMethods.ToolsCall,
            new JsonObject
            {
                ["name"] = unknownTool,
                ["arguments"] = new JsonObject(),
                ["_meta"] = PreviewMcpProcessDriver.CurrentMeta(),
            },
            requestId);
        var response = Assert.IsType<JsonRpcResponse>(await driver.TryReadMessageAsync(TimeSpan.FromSeconds(2)));

        Assert.Equal(requestId, response.Id);
        var result = RequireResult(response);
        var metadata = Assert.IsType<JsonObject>(result["_meta"]);
        var serverInfo = Assert.IsType<JsonObject>(metadata["io.modelcontextprotocol/serverInfo"]);
        Assert.Equal(PreviewToolCatalog.ServerName, serverInfo["name"]!.GetValue<string>());
        Assert.Equal(PreviewToolCatalog.ServerVersion, serverInfo["version"]!.GetValue<string>());
        Assert.Equal($"Unknown tool: {unknownTool}", SingleText(result));
        Assert.Equal(FrameByteCount(response), expected.BytesWritten);
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
        if (!TryCreateSymbolicLink(() => File.CreateSymbolicLink(escapingFile, Path.Combine(outside.Path, "Marker.cs"))))
        {
            AssertInjectedReparseComponentIsRefused(escapingFile, escapingFile);
            return;
        }

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
    public async Task UnrepresentableUnknownToolClosesBeforeDispatchWithoutResponse()
    {
        const int maximumFrameBytes = 256 * 1024;
        var unknownTool = new string('t', maximumFrameBytes * 4);
        var requestId = new RequestId("huge-tool");
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        await driver.SendRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = unknownTool,
                ["arguments"] = new JsonObject(),
                ["_meta"] = PreviewMcpProcessDriver.CurrentMeta(),
            },
            requestId);

        Assert.Null(await driver.TryReadMessageAsync(TimeSpan.FromSeconds(2)));
        Assert.True(await driver.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
    }

    [Fact]
    public async Task UnrepresentableUnsupportedVersionClosesBeforeDispatchWithoutResponse()
    {
        const int maximumFrameBytes = 256 * 1024;
        var requestId = new RequestId("huge-version");
        var metadata = PreviewMcpProcessDriver.CurrentMeta();
        metadata[MetaKeys.ProtocolVersion] = new string('v', maximumFrameBytes * 4);
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        await driver.SendRequestAsync("tools/list", new JsonObject { ["_meta"] = metadata }, requestId);

        Assert.Null(await driver.TryReadMessageAsync(TimeSpan.FromSeconds(2)));
        Assert.True(await driver.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
    }

    [Fact]
    public void CompleteResultFramePreflightMatchesJsonRpcResponseWireSize()
    {
        var id = new RequestId("result-envelope");
        var result = PreviewToolHandler.FrameBudgetExceeded();
        var response = new JsonRpcResponse
        {
            Id = id,
            Result = JsonSerializer.SerializeToNode(result, McpJsonUtilities.DefaultOptions),
        };

        Assert.True(BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(id, out var state));
        Assert.Equal(FrameByteCount(response), state.BytesWritten);
    }

    [Fact]
    public void FindCodeSymbolPayloadFramePreflightMatchesJsonRpcResponseWireSize()
    {
        var id = new RequestId("payload-envelope");
        IReadOnlyList<SymbolMatch> matches =
        [
            new SymbolMatch("A/\"\\\u0001😀.cs", 3, "Preflight", "class", "public sealed class \"\\\u0001😀Preflight { }"),
        ];
        var response = FindCodeSymbolResponse(id, matches);

        Assert.True(BoundedResponseFrameSerializer.FitsFindCodeSymbolSuccessResponseWithinLimit(id, matches, out var state));
        Assert.Equal(FrameByteCount(response), state.BytesWritten);
    }

    [Fact]
    public void FindCodeSymbolEscapedContextNearCapPreservesExactWireSize()
    {
        const int maximumFrameBytes = 256 * 1024;
        var id = new RequestId("near-cap-escaped");
        var escapedUnit = "\"\\\u0001😀";
        IReadOnlyList<SymbolMatch> baselineMatches = [new SymbolMatch("NearCap.cs", 1, "NearCap", "class", string.Empty)];
        var baselineBytes = FrameByteCount(FindCodeSymbolResponse(id, baselineMatches));
        var unitBytes = FrameByteCount(FindCodeSymbolResponse(
            id,
            [new SymbolMatch("NearCap.cs", 1, "NearCap", "class", escapedUnit)])) - baselineBytes;
        var context = string.Concat(Enumerable.Repeat(escapedUnit, (maximumFrameBytes - baselineBytes) / unitBytes));
        IReadOnlyList<SymbolMatch> matches = [new SymbolMatch("NearCap.cs", 1, "NearCap", "class", context)];
        var response = FindCodeSymbolResponse(id, matches);

        Assert.InRange(maximumFrameBytes - FrameByteCount(response), 0, unitBytes - 1);
        Assert.True(BoundedResponseFrameSerializer.FitsFindCodeSymbolSuccessResponseWithinLimit(id, matches, out var state));
        Assert.Equal(FrameByteCount(response), state.BytesWritten);
    }

    [Fact]
    public void BoundedFramePreflightsStopAtTheCapWithConstantTokenSlices()
    {
        const int maximumFrameBytes = 256 * 1024;
        var oversizedValue = new string('r', maximumFrameBytes * 4);

        Assert.False(BoundedResponseFrameSerializer.FitsUnknownToolResponseWithinLimit(
            new RequestId("huge-tool"),
            oversizedValue,
            out var unknownToolState));
        Assert.Equal(maximumFrameBytes, unknownToolState.BytesWritten);
        Assert.InRange(unknownToolState.MaximumTokenUtf16CodeUnits, 1, 128);

        foreach (var matches in new IReadOnlyList<SymbolMatch>[]
        {
            [new SymbolMatch(oversizedValue, 1, "HugePath", "class", "context")],
            [new SymbolMatch("HugeContext.cs", 1, "HugeContext", "class", oversizedValue)],
        })
        {
            Assert.False(BoundedResponseFrameSerializer.FitsFindCodeSymbolSuccessResponseWithinLimit(
                new RequestId("huge-result"),
                matches,
                out var successState));
            Assert.Equal(maximumFrameBytes, successState.BytesWritten);
            Assert.InRange(successState.MaximumTokenUtf16CodeUnits, 1, 128);
        }
    }

    [Fact]
    public void BoundedFrameSerializerAcceptsAnExactCapResponse()
    {
        const int maximumFrameBytes = 256 * 1024;
        var exactIdLength = maximumFrameBytes - FrameByteCount(FrameBudgetFallback(new RequestId("base"))) + "base".Length;
        var exact = FrameBudgetFallback(new RequestId(new string('i', exactIdLength)));

        Assert.Equal(maximumFrameBytes, FrameByteCount(exact));
        Assert.True(BoundedResponseFrameSerializer.FitsWithinLimit(exact, out var state), $"Bounded writer stopped at {state.BytesWritten} bytes.");
        Assert.Equal(maximumFrameBytes, state.BytesWritten);
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
                new RequestId(new string('i', maximumFrameBytes * 4)));

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
        try
        {
            if (!TryCreateSymbolicLink(() => Directory.CreateSymbolicLink(link, target.Path)))
            {
                AssertInjectedReparseComponentIsRefused(Path.Combine(link, "Nested"), link);
                return;
            }

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
        try
        {
            if (!TryCreateSymbolicLink(() => Directory.CreateSymbolicLink(alias, target.Path)))
            {
                foreach (var rawPath in new[]
                {
                    string.Concat(alias, Path.DirectorySeparatorChar, "."),
                    string.Concat(alias, Path.DirectorySeparatorChar, "..", Path.DirectorySeparatorChar, "real"),
                })
                {
                    AssertInjectedReparseComponentIsRefused(rawPath, alias);
                }

                return;
            }

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
    public void UnavailableSymlinkPrivilegeUsesInjectedReparseSeam()
    {
        Assert.False(TryCreateSymbolicLink(static () => throw new UnauthorizedAccessException()));
        AssertInjectedReparseComponentIsRefused(
            PreviewRepositoryLayout.FixtureRoot,
            PreviewRepositoryLayout.FixtureRoot);
    }

    [Fact]
    public async Task MultipleAndReparseLaunchRoots_AreRefusedBeforeServing()
    {
        using var target = TemporaryProject.Create("ReparseRootMarker");
        var linkPath = Path.Combine(Path.GetTempPath(), $"netcoredbg-preview-link-{Guid.NewGuid():N}");
        try
        {
            await AssertInvalidLaunchAsync(
                PreviewRepositoryLayout.Root,
                "--project",
                PreviewRepositoryLayout.FixtureRoot,
                "--project",
                target.Path);
            if (!TryCreateSymbolicLink(() => Directory.CreateSymbolicLink(linkPath, target.Path)))
            {
                AssertInjectedReparseComponentIsRefused(linkPath, linkPath);
                return;
            }

            await AssertInvalidLaunchAsync(PreviewRepositoryLayout.Root, "--project", linkPath);
        }
        finally
        {
            if (Directory.Exists(linkPath))
            {
                Directory.Delete(linkPath);
            }
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

    private static JsonRpcResponse FindCodeSymbolResponse(RequestId id, IReadOnlyList<SymbolMatch> matches)
    {
        var payload = new
        {
            kind = "find_code_symbol_success",
            results = matches.Select(static match => new
            {
                file = match.File,
                line = match.Line,
                name = match.Name,
                kind = match.Kind,
                context = match.Context,
            }).ToArray(),
        };
        var result = new CallToolResult
        {
            ResultType = "complete",
            IsError = false,
            Content = [new TextContentBlock { Text = JsonSerializer.Serialize(payload) }],
            StructuredContent = JsonSerializer.SerializeToElement(payload),
        };
        return new JsonRpcResponse
        {
            Id = id,
            Result = ResultWithServerInfo(result),
        };
    }

    private static JsonObject ResultWithServerInfo(CallToolResult result)
    {
        var node = JsonSerializer.SerializeToNode(result, McpJsonUtilities.DefaultOptions)!.AsObject();
        node["_meta"] = new JsonObject
        {
            ["io.modelcontextprotocol/serverInfo"] = new JsonObject
            {
                ["name"] = PreviewToolCatalog.ServerName,
                ["version"] = PreviewToolCatalog.ServerVersion,
            },
        };
        return node;
    }

    private static int FrameByteCount(JsonRpcMessage message) =>
        JsonSerializer.SerializeToUtf8Bytes(message, McpJsonUtilities.DefaultOptions).Length;
    private static JsonRpcResponse FrameBudgetFallback(RequestId id) => new()
    {
        Id = id,
        Result = JsonSerializer.SerializeToNode(PreviewToolHandler.FrameBudgetExceeded(), McpJsonUtilities.DefaultOptions),
    };

    private static JsonRpcResponse InvalidToolArgumentsResponse(RequestId id) => new()
    {
        Id = id,
        Result = ResultWithServerInfo(PreviewToolHandler.InvalidToolArguments()),
    };

    private static JsonRpcError LegacyInitializeMethodNotFound(RequestId id) => new()
    {
        Id = id,
        Error = new JsonRpcErrorDetail
        {
            Code = -32601,
            Message = "Method not found",
        },
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

    private static bool TryCreateSymbolicLink(Action create)
    {
        try
        {
            create();
            return true;
        }
        catch (UnauthorizedAccessException)
        {
            return false;
        }
    }

    private static void AssertInjectedReparseComponentIsRefused(string rawPath, string reparseComponent)
    {
        var parsed = PreviewProjectRootParser.TryParse(
            ["--project", rawPath],
            out _,
            path => string.Equals(path, reparseComponent, StringComparison.OrdinalIgnoreCase)
                ? FileAttributes.Directory | FileAttributes.ReparsePoint
                : File.GetAttributes(path));

        Assert.False(parsed);
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
