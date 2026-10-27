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
        var builder = Host.CreateEmptyApplicationBuilder(new HostApplicationBuilderSettings());
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
                        && HasInvalidPresentClientInfo(metadataRequest))
                    {
                        _ = context.Server.DisposeAsync().AsTask();
                        return;
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest invalidToolArgumentsRequest
                        && (HasInvalidToolCallName(invalidToolArgumentsRequest)
                            || HasInvalidKnownToolArguments(invalidToolArgumentsRequest)))
                    {
                        var tool = KnownToolOrDefault(invalidToolArgumentsRequest);
                        if (!BoundedResponseFrameSerializer.FitsInvalidToolArgumentsResponseWithinLimit(invalidToolArgumentsRequest.Id, tool))
                        {
                            _ = context.Server.DisposeAsync().AsTask();
                            return;
                        }

                        await context.Server.SendMessageAsync(
                            InvalidToolArgumentsResponse(invalidToolArgumentsRequest.Id, tool),
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
                            _ = context.Server.DisposeAsync().AsTask();
                            return;
                        }
                    }

                    await next(context, cancellationToken).ConfigureAwait(false);
                });
            });

        using var host = builder.Build();
        await host.RunAsync().ConfigureAwait(false);
    }

    private static JsonRpcResponse InvalidToolArgumentsResponse(RequestId id, string tool = PreviewToolCatalog.FindCodeSymbol) => new()
    {
        Id = id,
        Result = JsonSerializer.SerializeToNode(
            PreviewToolHandler.InvalidToolArguments(tool),
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
            || !PreviewToolCatalog.IsKnownTool(tool))
        {
            return false;
        }

        if (parameters["arguments"] is not JsonObject arguments)
        {
            return true;
        }

        switch (tool)
        {
            case PreviewToolCatalog.FindCodeSymbol:
                if (arguments.Count is < 1 or > 2
                    || arguments.Any(static entry => entry.Key is not "name" and not "kind")
                    || !HasSearchName(arguments))
                {
                    return true;
                }
                return arguments["kind"] is not null
                    && (arguments["kind"] is not JsonValue kind
                        || !kind.TryGetValue<string>(out var kindValue)
                        || kindValue is not "class" and not "method" and not "property" and not "field");
            case PreviewToolCatalog.FindCodeReferences:
                return arguments.Count is < 1 or > 2
                    || arguments.Any(static entry => entry.Key is not "name" and not "max_results")
                    || !HasSearchName(arguments)
                    || (arguments.ContainsKey("max_results") && !HasInteger(arguments, "max_results", 1));
            case PreviewToolCatalog.GetSourceContext:
                return arguments.Count is < 2 or > 3
                    || arguments.Any(static entry => entry.Key is not "file" and not "line" and not "radius")
                    || !HasNonBlankString(arguments, "file")
                    || !HasInteger(arguments, "line", 1)
                    || (arguments.ContainsKey("radius") && !HasInteger(arguments, "radius", 0));
            default:
                return false;
        }
    }

    private static bool HasSearchName(JsonObject arguments) =>
        arguments["name"] is JsonValue name
        && name.TryGetValue<string>(out var value)
        && !string.IsNullOrWhiteSpace(value)
        && value.Length <= 256;

    private static bool HasInteger(JsonObject arguments, string name, int minimum) =>
        arguments[name] is JsonValue value && value.TryGetValue<int>(out var number) && number >= minimum;

    private static string KnownToolOrDefault(JsonRpcRequest request) =>
        request.Params is JsonObject parameters
        && parameters["name"] is JsonValue name
        && name.TryGetValue<string>(out var tool)
        && PreviewToolCatalog.IsKnownTool(tool)
            ? tool!
            : PreviewToolCatalog.FindCodeSymbol;

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
        if (TryGetUnknownToolCall(request, out var tool))
        {
            return BoundedResponseFrameSerializer.FitsUnknownToolResponseWithinLimit(request.Id, tool, out _);
        }

        return BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(request.Id, KnownToolOrDefault(request));
    }

    private static bool TryGetUnknownToolCall(JsonRpcRequest request, out string tool)
    {
        tool = string.Empty;
        if (request.Method != RequestMethods.ToolsCall
            || request.Params is not JsonObject parameters
            || parameters["name"] is not JsonValue name
            || !name.TryGetValue<string>(out var candidate)
            || candidate is null
            || PreviewToolCatalog.IsKnownTool(candidate))
        {
            return false;
        }

        tool = candidate;
        return true;
    }

    private static bool HasInvalidPresentClientInfo(JsonRpcRequest request) =>
        request.Params is JsonObject parameters
        && parameters["_meta"] is JsonObject metadata
        && metadata.TryGetPropertyValue(MetaKeys.ClientInfo, out var clientInfoValue)
        && (clientInfoValue is not JsonObject clientInfo
            || !HasNonBlankString(clientInfo, "name")
            || !HasNonBlankString(clientInfo, "version"));

    private static bool HasNonBlankString(JsonObject value, string property) =>
        value[property] is JsonValue candidate
        && candidate.TryGetValue<string>(out var text)
        && !string.IsNullOrWhiteSpace(text);

    private static void ReplaceOversizedResponse(JsonRpcMessage message)
    {
        switch (message)
        {
            case JsonRpcResponse response:
                response.Result = JsonSerializer.SerializeToNode(
                    PreviewToolHandler.FrameBudgetExceeded(ResponseToolOrDefault(response.Result)),
                    McpJsonUtilities.DefaultOptions);
                break;
            case JsonRpcError { Error.Code: -32602 } invalidParams:
                invalidParams.Error.Message = string.Empty;
                invalidParams.Error.Data = null;
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

    private static string ResponseToolOrDefault(JsonNode? result)
    {
        if (result is not JsonObject envelope || envelope["structuredContent"] is not JsonObject payload)
        {
            return PreviewToolCatalog.FindCodeSymbol;
        }

        if (payload["tool"] is JsonValue toolName
            && toolName.TryGetValue<string>(out var tool)
            && PreviewToolCatalog.IsKnownTool(tool))
        {
            return tool!;
        }

        return payload["kind"]?.GetValue<string>() switch
        {
            "find_code_references_success" => PreviewToolCatalog.FindCodeReferences,
            "get_source_context_success" => PreviewToolCatalog.GetSourceContext,
            _ => PreviewToolCatalog.FindCodeSymbol,
        };
    }

}
