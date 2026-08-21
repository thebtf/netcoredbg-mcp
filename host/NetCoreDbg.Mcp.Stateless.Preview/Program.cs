using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.CodeSearch.Core;

namespace NetCoreDbg.Mcp.Stateless.Preview;

internal static class Program
{
    private const string ProtocolVersion = "2026-07-28";

    private static async Task Main(string[] arguments)
    {
        if (!PreviewProjectRootParser.TryParse(arguments, out var root))
        {
            await Console.OpenStandardError().WriteAsync("PREVIEW_ROOT_INVALID\n"u8.ToArray()).ConfigureAwait(false);
            Environment.ExitCode = 64;
            return;
        }

        var search = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);
        var tools = new PreviewToolHandler(search);
        var builder = Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Services.AddMcpServer(options =>
            {
                options.ProtocolVersion = ProtocolVersion;
                options.ServerInfo = new Implementation { Name = PreviewToolCatalog.ServerName, Version = PreviewToolCatalog.ServerVersion };
                options.Capabilities = new ServerCapabilities { Tools = new ToolsCapability { ListChanged = false } };
            })
            .WithStdioServerTransport()
            .WithListToolsHandler((context, cancellationToken) =>
                ValueTask.FromResult(PreviewToolCatalog.List()))
            .WithCallToolHandler(tools.CallAsync)
            .WithMessageFilters(filters =>
            {
                filters.AddIncomingFilter(next => async (context, cancellationToken) =>
                {
                    if (context.JsonRpcMessage is JsonRpcRequest { Method: not RequestMethods.Initialize } metadataRequest
                        && !HasRequiredRequestMetadata(metadataRequest))
                    {
                        _ = context.Server.DisposeAsync().AsTask();
                        return;
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest versionRequest
                        && TryGetUnsupportedProtocolVersion(versionRequest, out var requestedVersion))
                    {
                        if (!BoundedResponseFrameSerializer.FitsUnsupportedVersionErrorWithinLimit(
                                versionRequest.Id,
                                requestedVersion,
                                ProtocolVersion,
                                out _))
                        {
                            _ = context.Server.DisposeAsync().AsTask();
                            return;
                        }

                        await next(context, cancellationToken).ConfigureAwait(false);
                        return;
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest { Method: RequestMethods.ToolsCall, Params: null } nullParametersRequest)
                    {
                        if (!BoundedResponseFrameSerializer.FitsInvalidToolArgumentsResponseWithinLimit(nullParametersRequest.Id))
                        {
                            _ = context.Server.DisposeAsync().AsTask();
                            return;
                        }

                        await context.Server.SendMessageAsync(
                            InvalidToolArgumentsResponse(nullParametersRequest.Id),
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest invalidToolArgumentsRequest
                        && (HasInvalidToolCallName(invalidToolArgumentsRequest)
                            || HasInvalidKnownToolArguments(invalidToolArgumentsRequest)))
                    {
                        if (!BoundedResponseFrameSerializer.FitsInvalidToolArgumentsResponseWithinLimit(invalidToolArgumentsRequest.Id))
                        {
                            _ = context.Server.DisposeAsync().AsTask();
                            return;
                        }

                        await context.Server.SendMessageAsync(
                            InvalidToolArgumentsResponse(invalidToolArgumentsRequest.Id),
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest { Method: RequestMethods.Initialize } initializeRequest)
                    {
                        if (!BoundedResponseFrameSerializer.FitsLegacyInitializeMethodNotFoundErrorWithinLimit(initializeRequest.Id))
                        {
                            _ = context.Server.DisposeAsync().AsTask();
                            return;
                        }

                        await context.Server.SendMessageAsync(
                            MethodNotFoundResponse(initializeRequest.Id),
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest excludedMethodRequest
                        && IsExcludedMethod(excludedMethodRequest))
                    {
                        if (!BoundedResponseFrameSerializer.FitsLegacyInitializeMethodNotFoundErrorWithinLimit(excludedMethodRequest.Id))
                        {
                            _ = context.Server.DisposeAsync().AsTask();
                            return;
                        }

                        await context.Server.SendMessageAsync(
                            MethodNotFoundResponse(excludedMethodRequest.Id),
                            cancellationToken).ConfigureAwait(false);
                        return;
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest incoming
                        && !IsRequestResponseWithinFrameLimit(incoming))
                    {
                        _ = context.Server.DisposeAsync().AsTask();
                        return;
                    }


                    await next(context, cancellationToken).ConfigureAwait(false);
                });
                filters.AddOutgoingFilter(next => async (context, cancellationToken) =>
                {
                    if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                        && result.ContainsKey("supportedVersions")
                        && result.ContainsKey("capabilities"))
                    {
                        result["capabilities"]!.AsObject().Remove("logging");
                        result["ttlMs"] = (long)PreviewToolCatalog.CacheLifetime.TotalMilliseconds;
                        result["cacheScope"] = "public";
                    }

                    NormalizeUnsupportedVersion(context.JsonRpcMessage);
                    if (!IsCompleteResponseFrameWithinLimit(context.JsonRpcMessage))
                    {
                        if (context.JsonRpcMessage is JsonRpcError { Error.Code: -32022 })
                        {
                            _ = context.Server.DisposeAsync().AsTask();
                            return;
                        }

                        ReplaceOversizedResponse(context.JsonRpcMessage);
                        if (!IsCompleteResponseFrameWithinLimit(context.JsonRpcMessage))
                        {
                            return;
                        }
                    }

                    await next(context, cancellationToken).ConfigureAwait(false);
                });
            });

        using var host = builder.Build();
        await host.RunAsync().ConfigureAwait(false);
    }

    private static JsonRpcResponse InvalidToolArgumentsResponse(RequestId id) => new()
    {
        Id = id,
        Result = JsonSerializer.SerializeToNode(
            PreviewToolHandler.InvalidToolArguments(),
            McpJsonUtilities.DefaultOptions),
    };

    private static bool HasInvalidToolCallName(JsonRpcRequest request)
    {
        if (request.Method != RequestMethods.ToolsCall
            || request.Params is not JsonObject parameters)
        {
            return false;
        }

        return parameters["name"] is not JsonValue name
            || !name.TryGetValue<string>(out var candidate)
            || candidate is null;
    }

    private static bool HasInvalidKnownToolArguments(JsonRpcRequest request)
    {
        if (request.Method != RequestMethods.ToolsCall
            || request.Params is not JsonObject parameters
            || parameters["name"] is not JsonValue toolName
            || !toolName.TryGetValue<string>(out var tool)
            || !string.Equals(tool, PreviewToolCatalog.FindCodeSymbol, StringComparison.Ordinal))
        {
            return false;
        }

        if (parameters["arguments"] is not JsonObject arguments
            || arguments.Count is < 1 or > 2
            || arguments.Any(static entry => entry.Key is not "name" and not "kind")
            || arguments["name"] is not JsonValue name
            || !name.TryGetValue<string>(out var value)
            || string.IsNullOrWhiteSpace(value)
            || value.Length > 256)
        {
            return true;
        }

        if (arguments["kind"] is null)
        {
            return false;
        }

        return arguments["kind"] is not JsonValue kind
            || !kind.TryGetValue<string>(out var kindValue)
            || kindValue is not "class" and not "method" and not "property" and not "field";
    }

    private static bool IsExcludedMethod(JsonRpcRequest request) =>
        request.Method != "server/discover"
        && request.Method != "tools/list"
        && request.Method != RequestMethods.ToolsCall;

    private static JsonRpcError MethodNotFoundResponse(RequestId id) => new()
    {
        Id = id,
        Error = new JsonRpcErrorDetail
        {
            Code = -32601,
            Message = "Method not found",
        },
    };

    private static void NormalizeUnsupportedVersion(JsonRpcMessage message)
    {
        if (message is not JsonRpcError { Error.Code: -32022 } unsupported)
        {
            return;
        }

        unsupported.Error.Message = "Unsupported protocol version";
    }

    private static bool IsCompleteResponseFrameWithinLimit(JsonRpcMessage message) =>
        BoundedResponseFrameSerializer.FitsWithinLimit(message);

    private static bool IsRequestResponseWithinFrameLimit(JsonRpcRequest request)
    {
        if (TryGetUnsupportedProtocolVersion(request, out var requestedVersion))
        {
            return BoundedResponseFrameSerializer.FitsUnsupportedVersionErrorWithinLimit(
                request.Id,
                requestedVersion,
                ProtocolVersion,
                out _);
        }

        if (TryGetUnknownToolCall(request, out var tool))
        {
            return BoundedResponseFrameSerializer.FitsUnknownToolResponseWithinLimit(request.Id, tool, out _);
        }

        return BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(request.Id);
    }

    private static bool TryGetUnknownToolCall(JsonRpcRequest request, out string tool)
    {
        tool = string.Empty;
        if (request.Method != RequestMethods.ToolsCall
            || request.Params is not JsonObject parameters
            || parameters["name"] is not JsonValue name
            || !name.TryGetValue<string>(out var candidate)
            || candidate is null
            || string.Equals(candidate, PreviewToolCatalog.FindCodeSymbol, StringComparison.Ordinal))
        {
            return false;
        }

        tool = candidate;
        return true;
    }

    private static bool HasRequiredRequestMetadata(JsonRpcRequest request)
    {
        if (request.Params is not JsonObject parameters
            || parameters["_meta"] is not JsonObject metadata
            || metadata[MetaKeys.ProtocolVersion] is not JsonValue protocolVersion
            || !protocolVersion.TryGetValue<string>(out var version)
            || string.IsNullOrWhiteSpace(version)
            || metadata[MetaKeys.ClientInfo] is not JsonObject clientInfo
            || !HasNonBlankString(clientInfo, "name")
            || !HasNonBlankString(clientInfo, "version")
            || metadata[MetaKeys.ClientCapabilities] is not JsonObject)
        {
            return false;
        }

        return true;
    }

    private static bool HasNonBlankString(JsonObject value, string property) =>
        value[property] is JsonValue candidate
        && candidate.TryGetValue<string>(out var text)
        && !string.IsNullOrWhiteSpace(text);

    private static bool TryGetUnsupportedProtocolVersion(JsonRpcRequest request, out string requestedVersion)
    {
        requestedVersion = string.Empty;
        if (request.Params is not JsonObject parameters
            || parameters["_meta"] is not JsonObject metadata
            || metadata[MetaKeys.ProtocolVersion] is not JsonValue version
            || !version.TryGetValue<string>(out var candidate)
            || candidate is null)
        {
            return false;
        }

        requestedVersion = candidate;
        return !string.Equals(requestedVersion, ProtocolVersion, StringComparison.Ordinal);
    }

    private static void ReplaceOversizedResponse(JsonRpcMessage message)
    {
        switch (message)
        {
            case JsonRpcResponse response:
                response.Result = JsonSerializer.SerializeToNode(
                    PreviewToolHandler.FrameBudgetExceeded(),
                    McpJsonUtilities.DefaultOptions);
                break;
            case JsonRpcError error:
                error.Error = new JsonRpcErrorDetail
                {
                    Code = -32000,
                    Message = "Response exceeds frame limit",
                };
                break;
        }
    }

}
