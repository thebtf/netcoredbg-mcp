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

        AssertPreviewMarkerResult(result);
    }

    [Fact]
    public async Task OmittedClientInfo_ValidCallIsSupportedAsLiteralFirstFreshProcessRequest()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var result = RequireResult(await driver.CallToolAsync(
            ToolName,
            FixtureArguments(ToolName),
            new RequestId("call-first-without-client-info"),
            MetadataWithoutClientInfo()));

        AssertPreviewMarkerResult(result);
    }

    [Fact]
    public async Task OmittedClientInfo_DiscoveryAndCallsRemainRequestLocal()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var discovery = RequireResult(await driver.SendAsync(
            "server/discover",
            new JsonObject { ["_meta"] = MetadataWithoutClientInfo() },
            new RequestId("discover-without-client-info")));

        Assert.Equal(["2026-07-28"], discovery["supportedVersions"]!.AsArray().Select(static value => value!.GetValue<string>()));
        var capabilities = discovery["capabilities"]!.AsObject();
        Assert.Equal(["tools"], capabilities.Select(static property => property.Key));
        Assert.Equal(["listChanged"], capabilities["tools"]!.AsObject().Select(static property => property.Key));
        Assert.False(capabilities["tools"]!["listChanged"]!.GetValue<bool>());
        Assert.Equal(300_000L, discovery["ttlMs"]!.GetValue<long>());
        Assert.Equal("public", discovery["cacheScope"]!.GetValue<string>());
        AssertCatalog(RequireResult(await driver.ListToolsAsync(
            new RequestId("list-without-client-info"), MetadataWithoutClientInfo())));

        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId("call-without-client-info"), MetadataWithoutClientInfo())));
        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId("call-with-client-info"))));
        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId("call-without-client-info-again"), MetadataWithoutClientInfo())));
    }

    [Theory]
    [InlineData("1900-01-01", false)]
    [InlineData("1900-01-01", true)]
    [InlineData("", false)]
    [InlineData("", true)]
    [InlineData(" \t", false)]
    [InlineData(" \t", true)]
    [InlineData(" 2026-07-28 ", false)]
    [InlineData(" 2026-07-28 ", true)]
    public async Task OmittedClientInfo_UnsupportedVersionReturnsExactErrorAndValidCallRecovers(
        string requestedVersion, bool afterSuccess)
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        if (afterSuccess)
        {
            AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
                ToolName, FixtureArguments(ToolName), new RequestId("valid-before-unsupported"), MetadataWithoutClientInfo())));
        }
        var unsupported = MetadataWithoutClientInfo();
        unsupported[MetaKeys.ProtocolVersion] = requestedVersion;
        var id = afterSuccess ? new RequestId(42) : new RequestId("unsupported-without-client-info");

        AssertUnsupportedVersion(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), id, unsupported), id, requestedVersion);
        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId("supported-without-client-info"), MetadataWithoutClientInfo())));
    }

    [Fact]
    public async Task OmittedClientInfo_InvalidArgumentsUnknownToolAndExcludedMethodRetainRefusals()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var invalid = RequireResult(await driver.CallToolAsync(
            ToolName, new JsonObject(), new RequestId("invalid-arguments-without-client-info"), MetadataWithoutClientInfo()));
        AssertClosedError(invalid, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS");

        var unknown = RequireResult(await driver.CallToolAsync(
            "start_debug", new JsonObject(), new RequestId("unknown-tool-without-client-info"), MetadataWithoutClientInfo()));
        Assert.Equal("complete", unknown["resultType"]!.GetValue<string>());
        Assert.True(unknown["isError"]!.GetValue<bool>());
        Assert.False(unknown.ContainsKey("structuredContent"));
        Assert.Equal("Unknown tool: start_debug", SingleText(unknown));

        var excluded = await driver.SendAsync(
            "resources/list",
            new JsonObject { ["_meta"] = MetadataWithoutClientInfo() },
            new RequestId("excluded-method-without-client-info"));
        Assert.Equal(-32601, Assert.IsType<JsonRpcError>(excluded).Error.Code);
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

        AssertUnsupportedVersion(rejected, new RequestId("unsupported"), "1900-01-01");
        AssertCatalog(accepted);
    }

    [Fact]
    public async Task PresentMalformedClientInfoIsNeverDispatched()
    {
        var malformedRequests = new JsonNode?[]
        {
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion, [MetaKeys.ClientInfo] = "malformed", [MetaKeys.ClientCapabilities] = new JsonObject() } },
            new JsonObject { ["_meta"] = new JsonObject { [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion, [MetaKeys.ClientInfo] = null, [MetaKeys.ClientCapabilities] = new JsonObject() } },
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
    public async Task MissingMetadata_ValidArgumentsReturnCorrelatedInvalidParamsAndRecoverBeforeCleanEof()
    {
        using var process = PreviewOutputPathResolver.StartDirect("--project", PreviewRepositoryLayout.FixtureRoot);
        var standardError = process.StandardError.ReadToEndAsync();
        try
        {
            var id = new RequestId("missing-meta-valid-arguments");
            AssertInvalidParams(await SendAsync(new JsonRpcRequest
            {
                Id = id,
                Method = "tools/call",
                Params = new JsonObject { ["name"] = ToolName, ["arguments"] = FixtureArguments(ToolName) },
            }), id);

            var correctedId = new RequestId(43);
            var corrected = await SendAsync(new JsonRpcRequest
            {
                Id = correctedId,
                Method = "tools/call",
                Params = new JsonObject
                {
                    ["name"] = ToolName,
                    ["arguments"] = FixtureArguments(ToolName),
                    ["_meta"] = MetadataWithoutClientInfo(),
                },
            });
            Assert.Equal(correctedId, Assert.IsType<JsonRpcResponse>(corrected).Id);
            AssertPreviewMarkerResult(RequireResult(corrected));

            process.StandardInput.Close();
            await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(2));
            Assert.Equal(0, process.ExitCode);
            Assert.Equal("", await process.StandardOutput.ReadToEndAsync().WaitAsync(TimeSpan.FromSeconds(2)));
            Assert.Equal("", await standardError.WaitAsync(TimeSpan.FromSeconds(2)));
        }
        finally
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(2));
            }
        }

        async Task<JsonRpcMessage> SendAsync(JsonRpcRequest request)
        {
            await process.StandardInput.WriteLineAsync(JsonSerializer.Serialize<JsonRpcMessage>(request, McpJsonUtilities.DefaultOptions));
            await process.StandardInput.FlushAsync();
            var line = await process.StandardOutput.ReadLineAsync().WaitAsync(TimeSpan.FromSeconds(5));
            Assert.True(line is not null, $"EOF before the correlated JSON-RPC response for '{request.Id}'.");
            return Assert.IsAssignableFrom<JsonRpcMessage>(JsonSerializer.Deserialize<JsonRpcMessage>(line!, McpJsonUtilities.DefaultOptions));
        }
    }

    [Theory]
    [InlineData("_meta", null)]
    [InlineData("_meta", "null")]
    [InlineData("_meta", "42")]
    [InlineData("_meta", "true")]
    [InlineData("_meta", "\"malformed\"")]
    [InlineData("_meta", "[]")]
    [InlineData("_meta", "{}")]
    [InlineData("version", null)]
    [InlineData("version", "null")]
    [InlineData("version", "42")]
    [InlineData("version", "true")]
    [InlineData("version", "[]")]
    [InlineData("version", "{}")]
    [InlineData("capabilities", null)]
    [InlineData("capabilities", "null")]
    [InlineData("capabilities", "42")]
    [InlineData("capabilities", "true")]
    [InlineData("capabilities", "\"malformed\"")]
    [InlineData("capabilities", "[]")]
    public async Task RequiredMetadataErrorsAreCorrelatedBeforeAndAfterSuccessAndRecover(string field, string? json)
    {
        var metadata = MetadataWithoutClientInfo();
        var parameters = new JsonObject
        {
            ["name"] = ToolName,
            ["arguments"] = FixtureArguments(ToolName),
            ["_meta"] = metadata,
        };
        var owner = field == "_meta" ? parameters : metadata;
        var key = field switch
        {
            "version" => MetaKeys.ProtocolVersion,
            "capabilities" => MetaKeys.ClientCapabilities,
            _ => field,
        };
        if (json is null)
        {
            owner.Remove(key);
        }
        else
        {
            owner[key] = JsonNode.Parse(json);
        }

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var beforeId = new RequestId(41);
        AssertInvalidParams(await driver.SendAsync("tools/call", parameters, beforeId), beforeId);
        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId("valid-between-errors"), MetadataWithoutClientInfo())));
        var afterId = new RequestId("required-metadata-after-success");
        AssertInvalidParams(await driver.SendAsync("tools/call", parameters, afterId), afterId);
        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId(44), MetadataWithoutClientInfo())));
    }

    [Fact]
    public async Task MissingMetadataPrecedesMalformedToolArguments()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var id = new RequestId("missing-meta-invalid-arguments");
        AssertInvalidParams(await driver.SendAsync(
            "tools/call",
            new JsonObject { ["name"] = ToolName, ["arguments"] = new JsonObject() },
            id), id);

        var validMetadata = RequireResult(await driver.CallToolAsync(
            ToolName, new JsonObject(), new RequestId("metadata-corrected-invalid-arguments"), MetadataWithoutClientInfo()));
        AssertClosedError(validMetadata, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS");
    }

    [Fact]
    public async Task UnsupportedVersionPrecedesMalformedToolArguments()
    {
        var metadata = PreviewMcpProcessDriver.CurrentMeta();
        metadata[MetaKeys.ProtocolVersion] = "1900-01-01";
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        var response = await driver.SendAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = ToolName,
                ["arguments"] = new JsonObject(),
                ["_meta"] = metadata,
            },
            new RequestId("unsupported-invalid-arguments"));

        AssertUnsupportedVersion(response, new RequestId("unsupported-invalid-arguments"), "1900-01-01");
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
    public async Task OmittedToolCallParamsReturnCorrelatedInvalidParamsAndRecover()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var id = new RequestId("omitted-tool-call-params");
        AssertInvalidParams(await driver.SendAsync("tools/call", parameters: null, id), id);
        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId("valid-after-omitted-params"), MetadataWithoutClientInfo())));
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

        AssertUnsupportedVersion(response, requestId, requestedVersion);
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

        AssertUnsupportedVersion(response, requestId, requestedVersion);
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


    [Theory]
    [InlineData("find_code_symbol")]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task ExactCapInvalidToolArgumentsResponseIsAccepted(string tool)
    {
        const int maximumFrameBytes = 256 * 1024;
        var exactIdLength = maximumFrameBytes - FrameByteCount(InvalidToolArgumentsResponse(new RequestId("base"), tool)) + "base".Length;
        var requestId = new RequestId(new string('i', exactIdLength));
        var expected = InvalidToolArgumentsResponse(requestId, tool);

        Assert.True(BoundedResponseFrameSerializer.FitsInvalidToolArgumentsResponseWithinLimit(requestId, out var state, tool));
        Assert.Equal(maximumFrameBytes, state.BytesWritten);
        Assert.Equal(maximumFrameBytes, FrameByteCount(expected));

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var response = await driver.CallToolAsync(tool, new JsonObject(), requestId);

        Assert.Equal(requestId, Assert.IsType<JsonRpcResponse>(response).Id);
        AssertClosedError(RequireResult(response), "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", tool);
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
        File.WriteAllText(Path.Combine(hostileWorkingDirectory, "appsettings.json"), "{");
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

    [Theory]
    [InlineData("find_code_symbol")]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task UnreadableSourceAfterAnEarlierMatchReturnsClosedErrorWithoutPartialResult(string tool)
    {
        using var root = TemporaryProject.Create("PartialMarker");
        var locked = Path.Combine(root.Path, "ZLocked.cs");
        File.WriteAllText(locked, "public sealed class LockedMarker { }\n", new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));
        using var lockHandle = new FileStream(locked, FileMode.Open, FileAccess.ReadWrite, FileShare.None);
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);

        var arguments = tool switch
        {
            "get_source_context" => new JsonObject { ["file"] = "ZLocked.cs", ["line"] = 1 },
            "find_code_references" => new JsonObject { ["name"] = "PartialMarker" },
            _ => new JsonObject { ["name"] = "PartialMarker", ["kind"] = "class" },
        };
        var result = RequireResult(await driver.CallToolAsync(
            tool,
            arguments,
            new RequestId("locked-source")));

        AssertClosedError(result, "preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", tool);
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
    public async Task RequiredMetadataErrorWithEscapedIdRetainsCodeAndCorrelationAtMinimalFrameCapAndRecovers()
    {
        var detail = new JsonRpcErrorDetail { Code = -32602, Message = "" };
        var id = EscapedIdAtErrorFrameSize(detail, PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        var minimal = new JsonRpcError { Id = id, Error = detail };
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, FrameByteCount(minimal));
        Assert.True(BoundedResponseFrameSerializer.FitsWithinLimit(minimal));

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var response = await driver.SendAsync("tools/call", new JsonObject
        {
            ["name"] = ToolName,
            ["arguments"] = FixtureArguments(ToolName),
        }, id);

        AssertInvalidParams(response, id);
        Assert.InRange(FrameByteCount(response), 1, PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId("valid-after-bounded-metadata-error"), MetadataWithoutClientInfo())));
    }

    [Fact]
    public async Task RequiredMetadataErrorWithUnrepresentableEscapedIdClosesWithoutResponse()
    {
        var detail = new JsonRpcErrorDetail { Code = -32602, Message = "" };
        var id = EscapedIdAtErrorFrameSize(detail, PreviewToolCatalog.MaximumCompleteResponseFrameBytes + 1);
        var minimal = new JsonRpcError { Id = id, Error = detail };
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes + 1, FrameByteCount(minimal));
        Assert.False(BoundedResponseFrameSerializer.FitsWithinLimit(minimal));

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        await driver.SendRequestAsync("tools/call", new JsonObject
        {
            ["name"] = ToolName,
            ["arguments"] = FixtureArguments(ToolName),
        }, id);

        Assert.Null(await driver.TryReadMessageAsync(TimeSpan.FromSeconds(2)));
        Assert.True(await driver.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task UnsupportedVersionWithEscapedIdPreservesExactFrameOrClosesWhenUnrepresentable(bool beyondCap)
    {
        const string requestedVersion = "unsupported-\"\\\u0001😀";
        var detail = UnsupportedVersionErrorDetail(requestedVersion);
        var frameBytes = PreviewToolCatalog.MaximumCompleteResponseFrameBytes + (beyondCap ? 1 : 0);
        var id = EscapedIdAtErrorFrameSize(detail, frameBytes);
        Assert.Equal(frameBytes, FrameByteCount(new JsonRpcError { Id = id, Error = detail }));
        var metadata = MetadataWithoutClientInfo();
        metadata[MetaKeys.ProtocolVersion] = requestedVersion;
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);

        if (beyondCap)
        {
            await driver.SendRequestAsync("tools/call", new JsonObject
            {
                ["name"] = ToolName,
                ["arguments"] = FixtureArguments(ToolName),
                ["_meta"] = metadata,
            }, id);
            Assert.Null(await driver.TryReadMessageAsync(TimeSpan.FromSeconds(2)));
            Assert.True(await driver.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
            return;
        }

        var response = await driver.CallToolAsync(ToolName, FixtureArguments(ToolName), id, metadata);
        AssertUnsupportedVersion(response, id, requestedVersion);
        Assert.Equal(frameBytes, FrameByteCount(response));
        AssertPreviewMarkerResult(RequireResult(await driver.CallToolAsync(
            ToolName, FixtureArguments(ToolName), new RequestId("valid-after-bounded-version-error"), MetadataWithoutClientInfo())));
    }

    [Theory]
    [InlineData(-32602)]
    [InlineData(-32022)]
    public void CorrelatedErrorFramePreflightMatchesEscapedWireBytesAtCapAndRejectsCapPlusOne(int code)
    {
        var detail = code == -32602
            ? new JsonRpcErrorDetail
            {
                Code = code,
                Message = "diagnostic-\"\\\u0001😀",
                Data = JsonSerializer.SerializeToElement(new { detail = "\"\\\u0001😀" }),
            }
            : UnsupportedVersionErrorDetail("unsupported-\"\\\u0001😀");
        var exact = new JsonRpcError
        {
            Id = EscapedIdAtErrorFrameSize(detail, PreviewToolCatalog.MaximumCompleteResponseFrameBytes),
            Error = detail,
        };
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, FrameByteCount(exact));
        Assert.True(BoundedResponseFrameSerializer.FitsWithinLimit(exact, out var state));
        Assert.Equal(FrameByteCount(exact), state.BytesWritten);

        var beyond = new JsonRpcError
        {
            Id = EscapedIdAtErrorFrameSize(detail, PreviewToolCatalog.MaximumCompleteResponseFrameBytes + 1),
            Error = detail,
        };
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes + 1, FrameByteCount(beyond));
        Assert.False(BoundedResponseFrameSerializer.FitsWithinLimit(beyond, out var beyondState));
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, beyondState.BytesWritten);
        Assert.InRange(beyondState.MaximumTokenUtf16CodeUnits, 1, 128);
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

    [Theory]
    [InlineData("find_code_symbol")]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task CancellationNotificationWinsBeforeAnyToolResultAndTheProcessRemainsUsable(string tool)
    {
        using var root = TemporaryProject.CreateCancellationLoad();
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        var id = new RequestId("cancelled-call");
        var arguments = tool switch
        {
            "get_source_context" => new JsonObject { ["file"] = "Load15.cs", ["line"] = 1, ["radius"] = 0 },
            "find_code_references" => new JsonObject { ["name"] = "CancellationMarker" },
            _ => new JsonObject { ["name"] = "CancellationMarker", ["kind"] = "class" },
        };

        await driver.SendRequestAsync(
            "tools/call",
            new JsonObject
            {
                ["name"] = tool,
                ["arguments"] = arguments,
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
        Assert.False(TryCreateSymbolicLink(static () => throw new IOException()));
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

    [Fact]
    public async Task ReferencesFirstCallReturnsDeclarationAndRealUseWithDefaultAndRequestedLimits()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var result = RequireResult(await driver.CallToolAsync("find_code_references",
            new JsonObject { ["name"] = "PreviewMarker" }, new RequestId("references-first")));

        Assert.Equal("complete", result["resultType"]!.GetValue<string>());
        Assert.False(result["isError"]!.GetValue<bool>());
        Assert.Equal("""{"kind":"find_code_references_success","results":[{"file":"Markers.cs","line":3,"context":"public sealed class PreviewMarker { }"},{"file":"PreviewReferences.cs","line":5,"context":"public PreviewMarker Value { get; } = new PreviewMarker();"}]}""",
            result["structuredContent"]!.ToJsonString());
        Assert.Equal(result["structuredContent"]!.ToJsonString(), SingleText(result));

        var limited = RequireResult(await driver.CallToolAsync("find_code_references",
            new JsonObject { ["name"] = "PreviewMarker", ["max_results"] = 1 }, new RequestId("references-limit")));
        var match = Assert.Single(limited["structuredContent"]!["results"]!.AsArray());
        Assert.Equal("Markers.cs", match!["file"]!.GetValue<string>());
        Assert.Equal(3, match["line"]!.GetValue<int>());
    }

    [Fact]
    public async Task ReferencesPreserveIdentifierBoundariesAndLiteralNameMatching()
    {
        using var root = TemporaryProject.Create("Marker",
            "public sealed class Marker { }\npublic sealed class MarkerSuffix { }\npublic sealed class LongMarker { }\n"
            + "public sealed class Uses { public Marker Value { get; } = new Marker(); }\n");
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        var identifiers = RequireResult(await driver.CallToolAsync("find_code_references",
            new JsonObject { ["name"] = "Marker" }, new RequestId("identifier-boundary")));
        Assert.Equal([1, 4], identifiers["structuredContent"]!["results"]!.AsArray().Select(static match => match!["line"]!.GetValue<int>()));
        var literal = RequireResult(await driver.CallToolAsync("find_code_references",
            new JsonObject { ["name"] = "Marker()" }, new RequestId("literal-reference")));
        var match = Assert.Single(literal["structuredContent"]!["results"]!.AsArray());
        Assert.Equal(4, match!["line"]!.GetValue<int>());
        Assert.Equal("public sealed class Uses { public Marker Value { get; } = new Marker(); }", match["context"]!.GetValue<string>());
    }

    [Theory]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task ReadonlyRoutesReturnTypedByteBudgetRefusalWithoutPartialSourceAndRecover(string tool)
    {
        using var root = TemporaryProject.Create("ByteBudgetMarker",
            "public sealed class ByteBudgetMarker { }\n" + new string('x', 1024 * 1024));
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        var arguments = tool == "get_source_context"
            ? new JsonObject { ["file"] = "Marker.cs", ["line"] = 1, ["radius"] = 0 }
            : new JsonObject { ["name"] = "ByteBudgetMarker" };
        var refused = RequireResult(await driver.CallToolAsync(tool, arguments, new RequestId("readonly-byte-budget")));
        AssertClosedError(refused, "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", tool);
        Assert.DoesNotContain("ByteBudgetMarker", SingleText(refused), StringComparison.Ordinal);
        File.WriteAllText(Path.Combine(root.Path, "Marker.cs"), "public sealed class ByteBudgetMarker { }\n");
        var recovered = RequireResult(await driver.CallToolAsync(tool, arguments, new RequestId("after-byte-budget")));
        Assert.False(recovered["isError"]!.GetValue<bool>());
        Assert.Equal(recovered["structuredContent"]!.ToJsonString(), SingleText(recovered));
    }

    [Fact]
    public async Task ContextFirstCallPreservesRealUseWindowIndentationAndDefaultRadius()
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var result = RequireResult(await driver.CallToolAsync("get_source_context",
            new JsonObject { ["file"] = "PreviewReferences.cs", ["line"] = 5, ["radius"] = 1 },
            new RequestId("context-first")));

        Assert.Equal("complete", result["resultType"]!.GetValue<string>());
        Assert.False(result["isError"]!.GetValue<bool>());
        Assert.Equal("""{"kind":"get_source_context_success","file":"PreviewReferences.cs","start_line":4,"end_line":6,"lines":[{"line":4,"text":"{"},{"line":5,"text":"    public PreviewMarker Value { get; } = new PreviewMarker();"},{"line":6,"text":"}"}]}""",
            result["structuredContent"]!.ToJsonString());
        Assert.Equal(result["structuredContent"]!.ToJsonString(), SingleText(result));

        var defaultWindow = RequireResult(await driver.CallToolAsync("get_source_context",
            new JsonObject { ["file"] = "PreviewReferences.cs", ["line"] = 5 }, new RequestId("context-default")));
        var payload = defaultWindow["structuredContent"]!;
        Assert.Equal(1, payload["start_line"]!.GetValue<int>());
        Assert.Equal(6, payload["end_line"]!.GetValue<int>());
        Assert.Equal(Enumerable.Range(1, 6), payload["lines"]!.AsArray().Select(static line => line!["line"]!.GetValue<int>()));
        Assert.Equal(["namespace PreviewSearchApp;", "", "public sealed class PreviewReferences", "{",
            "    public PreviewMarker Value { get; } = new PreviewMarker();", "}"],
            payload["lines"]!.AsArray().Select(static line => line!["text"]!.GetValue<string>()));
        Assert.Equal(payload.ToJsonString(), SingleText(defaultWindow));
    }

    [Theory]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task ReadonlyRoutesRejectMalformedArgumentsWithCorrectRouteAndRemainUsable(string tool)
    {
        var tooLong = new string('x', 257);
        JsonObject?[] invalid = tool == "find_code_references"
            ? [null, new(), new() { ["name"] = null }, new() { ["name"] = 1 }, new() { ["name"] = " " },
                new() { ["name"] = tooLong }, new() { ["name"] = "PreviewMarker", ["max_results"] = null },
                new() { ["name"] = "PreviewMarker", ["max_results"] = "1" },
                new() { ["name"] = "PreviewMarker", ["max_results"] = 0 },
                new() { ["name"] = "PreviewMarker", ["max_results"] = -1 },
                new() { ["name"] = "PreviewMarker", ["max_results"] = 1.5 },
                new() { ["name"] = "PreviewMarker", ["max_results"] = 2147483648L },
                new() { ["name"] = "PreviewMarker", ["kind"] = "class" }]
            : [null, new(), new() { ["file"] = "Markers.cs" }, new() { ["file"] = null, ["line"] = 1 },
                new() { ["file"] = 1, ["line"] = 1 }, new() { ["file"] = " ", ["line"] = 1 },
                new() { ["file"] = "Markers.cs", ["line"] = null },
                new() { ["file"] = "Markers.cs", ["line"] = "1" },
                new() { ["file"] = "Markers.cs", ["line"] = 0 },
                new() { ["file"] = "Markers.cs", ["line"] = 1.5 },
                new() { ["file"] = "Markers.cs", ["line"] = 2147483648L },
                new() { ["file"] = "Markers.cs", ["line"] = 1, ["radius"] = null },
                new() { ["file"] = "Markers.cs", ["line"] = 1, ["radius"] = "0" },
                new() { ["file"] = "Markers.cs", ["line"] = 1, ["radius"] = -1 },
                new() { ["file"] = "Markers.cs", ["line"] = 1, ["radius"] = 0.5 },
                new() { ["file"] = "Markers.cs", ["line"] = 1, ["radius"] = 2147483648L },
                new() { ["file"] = "Markers.cs", ["line"] = 1, ["extra"] = true },
                new() { ["file"] = "Markers.cs", ["line"] = 1, ["radius"] = int.MaxValue }];
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        foreach (var arguments in invalid)
        {
            var result = RequireResult(await driver.CallToolAsync(tool, arguments, new RequestId(Guid.NewGuid().ToString("N"))));
            AssertClosedError(result, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", tool);
        }
        foreach (var arguments in new JsonNode[] { JsonValue.Create("not an object")!, new JsonArray() })
        {
            var result = RequireResult(await driver.SendAsync("tools/call", new JsonObject
            {
                ["name"] = tool,
                ["arguments"] = arguments,
                ["_meta"] = PreviewMcpProcessDriver.CurrentMeta(),
            }, new RequestId(Guid.NewGuid().ToString("N"))));
            AssertClosedError(result, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", tool);
        }
        var followup = RequireResult(await driver.CallToolAsync(tool, FixtureArguments(tool), new RequestId("valid-after-invalid")));
        Assert.False(followup["isError"]!.GetValue<bool>());
    }

    [Theory]
    [InlineData(128, 0)]
    [InlineData(129, 0)]
    [InlineData(129, 1)]
    [InlineData(129, 127)]
    [InlineData(128, 128)]
    [InlineData(129, 128)]
    [InlineData(129, 1000)]
    [InlineData(129, int.MaxValue)]
    public async Task ReferencesDefault1000PreservesStrict128ResultBudget(int total, int requested)
    {
        using var root = TemporaryProject.Create("BudgetMarker", string.Concat(Enumerable.Repeat("BudgetMarker use;\n", total)));
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        var arguments = new JsonObject { ["name"] = "BudgetMarker" };
        if (requested != 0)
        {
            arguments["max_results"] = requested;
        }
        var result = RequireResult(await driver.CallToolAsync("find_code_references", arguments, new RequestId("reference-budget")));
        if (total > 128 && (requested == 0 || requested >= 128))
        {
            AssertClosedError(result, "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_references");
        }
        else
        {
            Assert.False(result["isError"]!.GetValue<bool>());
            Assert.Equal(requested == 0 ? total : Math.Min(total, requested), result["structuredContent"]!["results"]!.AsArray().Count);
            Assert.Equal(result["structuredContent"]!.ToJsonString(), SingleText(result));
        }
    }

    [Fact]
    public async Task ContextPathReadAndRangeFailuresAreClosedAndDoNotBreakLaterValidCalls()
    {
        using var root = TemporaryProject.Create("ContainedMarker");
        using var outside = TemporaryProject.Create("ExternalMarker");
        foreach (var relative in new[] { "bin/Hidden.cs", "obj/Hidden.cs", ".git/Hidden.cs", ".hg/Hidden.cs", ".svn/Hidden.cs",
            "src/bin/Hidden.cs", "src/obj/Hidden.cs", "src/.git/Hidden.cs", "src/.hg/Hidden.cs", "src/.svn/Hidden.cs",
            "ignored/Hidden.cs", "A/Duplicate.cs", "B/Duplicate.cs", "Unsupported.xaml" })
        {
            var path = Path.Combine(root.Path, relative);
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllText(path, "private-source-sentinel\n");
        }
        File.WriteAllText(Path.Combine(root.Path, ".gitignore"), "ignored/\n");
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        foreach (var file in new[] { "bin/Hidden.cs", "obj/Hidden.cs", ".git/Hidden.cs", ".hg/Hidden.cs", ".svn/Hidden.cs",
            "src/bin/Hidden.cs", "src/obj/Hidden.cs", "src/.git/Hidden.cs", "src/.hg/Hidden.cs", "src/.svn/Hidden.cs",
            "ignored/Hidden.cs", "Missing.cs", "Unsupported.xaml" })
        {
            foreach (var candidate in new[] { file, Path.GetFullPath(Path.Combine(root.Path, file)) })
            {
                var result = RequireResult(await driver.CallToolAsync("get_source_context",
                    new JsonObject { ["file"] = candidate, ["line"] = 1 }, new RequestId(Guid.NewGuid().ToString("N"))));
                AssertClosedError(result, "preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", "get_source_context");
                Assert.DoesNotContain(root.Path, SingleText(result), StringComparison.Ordinal);
                Assert.DoesNotContain("private-source-sentinel", SingleText(result), StringComparison.Ordinal);
            }
        }
        var escaped = RequireResult(await driver.CallToolAsync("get_source_context",
            new JsonObject { ["file"] = Path.Combine(outside.Path, "Marker.cs"), ["line"] = 1 }, new RequestId("outside-context")));
        AssertClosedError(escaped, "preview_path_refused", "PREVIEW_PATH_REFUSED", "get_source_context");
        foreach (var arguments in new[] { new JsonObject { ["file"] = "Duplicate.cs", ["line"] = 1 },
            new JsonObject { ["file"] = "Marker.cs", ["line"] = 2 } })
        {
            var result = RequireResult(await driver.CallToolAsync("get_source_context", arguments, new RequestId(Guid.NewGuid().ToString("N"))));
            AssertClosedError(result, "invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", "get_source_context");
        }
        var valid = RequireResult(await driver.CallToolAsync("get_source_context",
            new JsonObject { ["file"] = "Marker.cs", ["line"] = 1, ["radius"] = 0 }, new RequestId("context-after-refusal")));
        Assert.False(valid["isError"]!.GetValue<bool>());
        Assert.Equal("public sealed class ContainedMarker { }", valid["structuredContent"]!["lines"]![0]!["text"]!.GetValue<string>());
    }

    [Fact]
    public async Task ContextCaseAliasesCannotReadPhysicallyIgnoredWindowsSourceAndAllowedNegationStillWorks()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        using var root = TemporaryProject.Create("CaseAliasControl");
        Directory.CreateDirectory(Path.Combine(root.Path, "ignored"));
        Directory.CreateDirectory(Path.Combine(root.Path, "private"));
        File.WriteAllText(Path.Combine(root.Path, ".gitignore"), "ignored/\n!ignored/Allowed.cs\nprivate/Secret.cs\n");
        File.WriteAllText(Path.Combine(root.Path, "ignored", "Secret.cs"), "private-source-sentinel\n");
        File.WriteAllText(Path.Combine(root.Path, "private", "Secret.cs"), "private-source-sentinel\n");
        File.WriteAllText(Path.Combine(root.Path, "ignored", "Allowed.cs"), "  allowed-source-control\n");
        if (!File.Exists(Path.Combine(root.Path, "IGNORED", "Secret.cs")))
        {
            return;
        }

        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        foreach (var alias in new[] { "IGNORED/Secret.cs", "private/SECRET.CS", "PRIVATE/secret.cs" })
        {
            foreach (var file in new[] { alias, Path.GetFullPath(Path.Combine(root.Path, alias)) })
            {
                var result = RequireResult(await driver.CallToolAsync("get_source_context",
                    new JsonObject { ["file"] = file, ["line"] = 1, ["radius"] = 0 }, new RequestId(Guid.NewGuid().ToString("N"))));
                AssertClosedError(result, "preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", "get_source_context");
                Assert.DoesNotContain("private-source-sentinel", SingleText(result), StringComparison.Ordinal);
                Assert.DoesNotContain(root.Path, SingleText(result), StringComparison.Ordinal);
            }
        }
        foreach (var file in new[] { "IGNORED/ALLOWED.CS", Path.Combine(root.Path, "IGNORED/ALLOWED.CS") })
        {
            var allowed = RequireResult(await driver.CallToolAsync("get_source_context",
                new JsonObject { ["file"] = file, ["line"] = 1, ["radius"] = 0 }, new RequestId(Guid.NewGuid().ToString("N"))));
            Assert.False(allowed["isError"]!.GetValue<bool>());
            Assert.Equal("ignored/Allowed.cs", allowed["structuredContent"]!["file"]!.GetValue<string>());
            Assert.Equal("  allowed-source-control", Assert.Single(allowed["structuredContent"]!["lines"]!.AsArray())!["text"]!.GetValue<string>());
            Assert.Equal(allowed["structuredContent"]!.ToJsonString(), SingleText(allowed));
        }
    }

    [Theory]
    [InlineData("find_code_symbol")]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task InvalidArgumentsResponseAtCapPlusOneClosesWithoutEmittingOversizedFrame(string tool)
    {
        var length = PreviewToolCatalog.MaximumCompleteResponseFrameBytes
            - FrameByteCount(InvalidToolArgumentsResponse(new RequestId("base"), tool)) + 5;
        var id = new RequestId(new string('i', length));
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes + 1, FrameByteCount(InvalidToolArgumentsResponse(id, tool)));
        Assert.False(BoundedResponseFrameSerializer.FitsInvalidToolArgumentsResponseWithinLimit(id, tool));
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        await driver.SendRequestAsync("tools/call", new JsonObject
        {
            ["name"] = tool,
            ["arguments"] = new JsonObject(),
            ["_meta"] = PreviewMcpProcessDriver.CurrentMeta(),
        }, id);
        Assert.Null(await driver.TryReadMessageAsync(TimeSpan.FromSeconds(2)));
        Assert.True(await driver.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
    }

    [Theory]
    [InlineData("find_code_symbol")]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task RouteCorrectBudgetFallbackAcceptsExactCapAndClosesAtCapPlusOne(string tool)
    {
        var length = PreviewToolCatalog.MaximumCompleteResponseFrameBytes
            - FrameByteCount(FrameBudgetFallback(new RequestId("base"), tool)) + 4;
        var exact = new RequestId(new string('i', length));
        Assert.True(BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(exact, out var state, tool));
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, state.BytesWritten);
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var response = await driver.CallToolAsync(tool, FixtureArguments(tool), exact);
        Assert.Equal(exact, Assert.IsType<JsonRpcResponse>(response).Id);
        AssertClosedError(RequireResult(response), "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", tool);
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, FrameByteCount(response));
        Assert.False(RequireResult(response).ContainsKey("_meta"));

        var beyond = new RequestId(new string('i', length + 1));
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes + 1, FrameByteCount(FrameBudgetFallback(beyond, tool)));
        Assert.False(BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(beyond, tool));
        await driver.SendRequestAsync("tools/call", new JsonObject
        {
            ["name"] = tool,
            ["arguments"] = FixtureArguments(tool),
            ["_meta"] = PreviewMcpProcessDriver.CurrentMeta(),
        }, beyond);
        Assert.Null(await driver.TryReadMessageAsync(TimeSpan.FromSeconds(2)));
        Assert.True(await driver.WaitForTransportClosureAsync(TimeSpan.FromSeconds(2)));
    }

    [Theory]
    [InlineData("find_code_symbol")]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task SuccessResponseIncludesMetadataAtExactCapAndBecomesRouteCorrectBudgetErrorAtCapPlusOne(string tool)
    {
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(PreviewRepositoryLayout.FixtureRoot);
        var baseline = await driver.CallToolAsync(tool, FixtureArguments(tool), new RequestId("base"));
        var length = PreviewToolCatalog.MaximumCompleteResponseFrameBytes - FrameByteCount(baseline) + 4;
        var exactId = new RequestId(new string('i', length));
        var response = await driver.CallToolAsync(tool, FixtureArguments(tool), exactId);
        var result = RequireResult(response);
        Assert.Equal(exactId, Assert.IsType<JsonRpcResponse>(response).Id);
        Assert.False(result["isError"]!.GetValue<bool>());
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, FrameByteCount(response));
        Assert.Equal(result["structuredContent"]!.ToJsonString(), SingleText(result));
        Assert.Equal(PreviewToolCatalog.ServerName, result["_meta"]!["io.modelcontextprotocol/serverInfo"]!["name"]!.GetValue<string>());
        Assert.Equal(PreviewToolCatalog.ServerVersion, result["_meta"]!["io.modelcontextprotocol/serverInfo"]!["version"]!.GetValue<string>());
        var beyondId = new RequestId(new string('i', length + 1));
        var beyond = await driver.CallToolAsync(tool, FixtureArguments(tool), beyondId);
        Assert.Equal(beyondId, Assert.IsType<JsonRpcResponse>(beyond).Id);
        AssertClosedError(RequireResult(beyond), "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", tool);
        Assert.True(FrameByteCount(beyond) <= PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
    }

    [Theory]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public async Task ReadonlyOversizedEscapedSuccessMapsToRouteCorrectBudgetWithoutPartialSource(string tool)
    {
        using var root = TemporaryProject.Create("FrameLimitMarker", string.Concat(Enumerable.Repeat(
            "public sealed class FrameLimitMarker { } // " + string.Concat(Enumerable.Repeat(char.ConvertFromUtf32(0x1F600), 500)) + "\n", 128)));
        await using var driver = await PreviewMcpProcessDriver.StartRawAsync(root.Path);
        var arguments = tool == "get_source_context"
            ? new JsonObject { ["file"] = "Marker.cs", ["line"] = 64, ["radius"] = 63 }
            : new JsonObject { ["name"] = "FrameLimitMarker" };
        var response = await driver.CallToolAsync(tool, arguments, new RequestId("oversized-readonly-success"));
        AssertClosedError(RequireResult(response), "preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", tool);
        Assert.DoesNotContain("FrameLimitMarker", SingleText(RequireResult(response)), StringComparison.Ordinal);
        Assert.True(FrameByteCount(response) <= PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
    }

    private static JsonObject MetadataWithoutClientInfo() => new()
    {
        [MetaKeys.ProtocolVersion] = "2026-07-28",
        [MetaKeys.ClientCapabilities] = new JsonObject(),
    };

    private static void AssertInvalidParams(JsonRpcMessage response, RequestId id)
    {
        var error = Assert.IsType<JsonRpcError>(response);
        Assert.Equal(id, error.Id);
        Assert.Equal(-32602, error.Error.Code);
    }

    private static void AssertUnsupportedVersion(JsonRpcMessage response, RequestId id, string requestedVersion)
    {
        var error = Assert.IsType<JsonRpcError>(response);
        Assert.Equal(id, error.Id);
        Assert.Equal(-32022, error.Error.Code);
        Assert.Equal("Unsupported protocol version", error.Error.Message);
        var data = Assert.IsType<JsonElement>(error.Error.Data);
        Assert.Equal(["requested", "supported"], data.EnumerateObject().Select(static property => property.Name).Order());
        Assert.Equal(requestedVersion, data.GetProperty("requested").GetString());
        Assert.Equal(["2026-07-28"], data.GetProperty("supported").EnumerateArray().Select(static value => value.GetString()));
    }

    private static void AssertPreviewMarkerResult(JsonObject result)
    {
        Assert.Equal("complete", result["resultType"]!.GetValue<string>());
        Assert.False(result["isError"]!.GetValue<bool>());
        var structured = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal(
            "{\"kind\":\"find_code_symbol_success\",\"results\":[{\"file\":\"Markers.cs\",\"line\":3,\"name\":\"PreviewMarker\",\"kind\":\"class\",\"context\":\"public sealed class PreviewMarker { }\"}]}",
            structured.ToJsonString());
        Assert.Equal(structured.ToJsonString(), SingleText(result));
    }

    private static JsonObject FixtureArguments(string tool) => tool switch
    {
        "find_code_symbol" => new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "class" },
        "find_code_references" => new JsonObject { ["name"] = "PreviewMarker" },
        "get_source_context" => new JsonObject { ["file"] = "PreviewReferences.cs", ["line"] = 5, ["radius"] = 1 },
        _ => throw new ArgumentOutOfRangeException(nameof(tool)),
    };

    private static JsonObject RequireResult(JsonRpcMessage message) =>
        Assert.IsType<JsonObject>(Assert.IsType<JsonRpcResponse>(message).Result);

    private static void AssertCatalog(JsonObject result)
    {
        var tools = result["tools"]!.AsArray();
        Assert.Equal([ToolName, "find_code_references", "get_source_context"],
            tools.Select(static value => value!["name"]!.GetValue<string>()));
        Assert.Equal(300_000L, result["ttlMs"]!.GetValue<long>());
        Assert.Equal("public", result["cacheScope"]!.GetValue<string>());
        foreach (var entry in tools)
        {
            var annotations = entry!["annotations"]!.AsObject();
            Assert.True(annotations["readOnlyHint"]!.GetValue<bool>());
            Assert.True(annotations["idempotentHint"]!.GetValue<bool>());
            Assert.False(annotations["openWorldHint"]!.GetValue<bool>());
            Assert.False(entry["inputSchema"]!["additionalProperties"]!.GetValue<bool>());
            Assert.False(entry["outputSchema"]!["additionalProperties"]!.GetValue<bool>());
            Assert.Equal(entry["name"]!.GetValue<string>() + "_success",
                entry["outputSchema"]!["properties"]!["kind"]!["const"]!.GetValue<string>());
        }
        var tool = tools[0]!.AsObject();
        var schema = tool["inputSchema"]!.AsObject();
        Assert.Equal("object", schema["type"]!.GetValue<string>());
        Assert.False(schema["additionalProperties"]!.GetValue<bool>());
        Assert.Equal(["name"], schema["required"]!.AsArray().Select(static value => value!.GetValue<string>()));
        var properties = schema["properties"]!.AsObject();
        Assert.Equal(1, properties["name"]!["minLength"]!.GetValue<int>());
        Assert.Equal(256, properties["name"]!["maxLength"]!.GetValue<int>());
        Assert.Equal(["string", "null"], properties["kind"]!["type"]!.AsArray().Select(static value => value!.GetValue<string>()));
        Assert.Equal(["class", "method", "property", "field", null], properties["kind"]!["enum"]!.AsArray().Select(static value => value?.GetValue<string>()));
        var references = tools[1]!["inputSchema"]!.AsObject();
        Assert.Equal(["name"], references["required"]!.AsArray().Select(static value => value!.GetValue<string>()));
        Assert.Equal(["name", "max_results"], references["properties"]!.AsObject().Select(static property => property.Key));
        Assert.Equal(1000, references["properties"]!["max_results"]!["default"]!.GetValue<int>());
        Assert.Equal(1, references["properties"]!["max_results"]!["minimum"]!.GetValue<int>());
        Assert.Equal(int.MaxValue, references["properties"]!["max_results"]!["maximum"]!.GetValue<int>());
        var referenceItem = tools[1]!["outputSchema"]!["properties"]!["results"]!["items"]!;
        Assert.False(referenceItem["additionalProperties"]!.GetValue<bool>());
        Assert.Equal(["file", "line", "context"], referenceItem["required"]!.AsArray().Select(static value => value!.GetValue<string>()));
        var context = tools[2]!["inputSchema"]!;
        Assert.Equal(["file", "line"], context["required"]!.AsArray().Select(static value => value!.GetValue<string>()));
        Assert.Equal(["file", "line", "radius"], context["properties"]!.AsObject().Select(static property => property.Key));
        Assert.Equal(10, context["properties"]!["radius"]!["default"]!.GetValue<int>());
        Assert.Equal(0, context["properties"]!["radius"]!["minimum"]!.GetValue<int>());
        var contextOutput = tools[2]!["outputSchema"]!;
        Assert.Equal(["kind", "file", "start_line", "end_line", "lines"],
            contextOutput["required"]!.AsArray().Select(static value => value!.GetValue<string>()));
        var line = contextOutput["properties"]!["lines"]!["items"]!;
        Assert.False(line["additionalProperties"]!.GetValue<bool>());
        Assert.Equal(["line", "text"], line["required"]!.AsArray().Select(static value => value!.GetValue<string>()));
    }

    private static void AssertClosedError(JsonObject result, string kind, string error, string tool = ToolName)
    {
        Assert.Equal("complete", result["resultType"]!.GetValue<string>());
        Assert.True(result["isError"]!.GetValue<bool>());
        var structured = Assert.IsType<JsonObject>(result["structuredContent"]);
        Assert.Equal(["kind", "error", "tool"], structured.Select(static property => property.Key));
        Assert.Equal(kind, structured["kind"]!.GetValue<string>());
        Assert.Equal(error, structured["error"]!.GetValue<string>());
        Assert.Equal(tool, structured["tool"]!.GetValue<string>());
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

    private static RequestId EscapedIdAtErrorFrameSize(JsonRpcErrorDetail detail, int frameBytes)
    {
        const string prefix = "escaped-\"\\\u0001😀";
        var baseline = FrameByteCount(new JsonRpcError { Id = new RequestId(prefix), Error = detail });
        var unitBytes = FrameByteCount(new JsonRpcError { Id = new RequestId(prefix + "\u0001"), Error = detail }) - baseline;
        var paddingBytes = frameBytes - baseline;
        Assert.True(paddingBytes >= 0);
        return new RequestId(prefix + new string('\u0001', paddingBytes / unitBytes) + new string('i', paddingBytes % unitBytes));
    }

    private static JsonRpcErrorDetail UnsupportedVersionErrorDetail(string requestedVersion) => new()
    {
        Code = -32022,
        Message = "Unsupported protocol version",
        Data = JsonSerializer.SerializeToElement(new { requested = requestedVersion, supported = new[] { "2026-07-28" } }),
    };
    private static JsonRpcResponse FrameBudgetFallback(RequestId id, string tool = ToolName) => new()
    {
        Id = id,
        Result = JsonSerializer.SerializeToNode(PreviewToolHandler.FrameBudgetExceeded(tool), McpJsonUtilities.DefaultOptions),
    };

    private static JsonRpcResponse InvalidToolArgumentsResponse(RequestId id, string tool = ToolName) => new()
    {
        Id = id,
        Result = ResultWithServerInfo(PreviewToolHandler.InvalidToolArguments(tool)),
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
        catch (IOException)
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
