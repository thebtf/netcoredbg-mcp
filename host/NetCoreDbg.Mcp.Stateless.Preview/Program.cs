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
                options.ServerInfo = new Implementation { Name = "netcoredbg-mcp-stateless-preview", Version = "1.0.0" };
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
                    if (context.JsonRpcMessage is JsonRpcRequest incoming
                        && !IsRequestIdWithinResponseFrameLimit(incoming.Id))
                    {
                        _ = context.Server.DisposeAsync().AsTask();
                        return;
                    }

                    if (context.JsonRpcMessage is JsonRpcRequest request
                        && TryGetUnsupportedProtocolVersion(request, out var requestedVersion)
                        && !BoundedResponseFrameSerializer.FitsUnsupportedVersionErrorWithinLimit(
                            request.Id,
                            requestedVersion,
                            ProtocolVersion,
                            out _))
                    {
                        await context.Server.SendMessageAsync(new JsonRpcError
                        {
                            Id = request.Id,
                            Error = new JsonRpcErrorDetail
                            {
                                Code = -32022,
                                Message = "Unsupported protocol version",
                                Data = UnsupportedVersionData(),
                            },
                        }, cancellationToken).ConfigureAwait(false);
                        return;
                    }


                    if (context.JsonRpcMessage is JsonRpcRequest { Method: RequestMethods.Initialize } initializeRequest)
                    {
                        await context.Server.SendMessageAsync(new JsonRpcError
                        {
                            Id = initializeRequest.Id,
                            Error = new JsonRpcErrorDetail
                            {
                                Code = -32601,
                                Message = "Method not found",
                            },
                        }, cancellationToken).ConfigureAwait(false);
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

    private static bool IsRequestIdWithinResponseFrameLimit(RequestId id) =>
        BoundedResponseFrameSerializer.FitsFrameBudgetExceededResponseWithinLimit(id);

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
            case JsonRpcError error when error.Error.Code == -32022:
                error.Error.Data = UnsupportedVersionData();
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

    private static JsonElement UnsupportedVersionData() => JsonSerializer.SerializeToElement(new
    {
        requested = "unsupported",
        supported = new[] { ProtocolVersion },
    });
}
