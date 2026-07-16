using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// A real (not mocked) minimal MCP server standing in for the Python backend in tests:
/// constructed directly via <see cref="McpServer.Create"/>, exercising the genuine SDK
/// server-side session/transport/handler machinery, just with test-controlled behavior
/// instead of the real netcoredbg_mcp implementation.
/// </summary>
internal sealed class FakePythonServer : IAsyncDisposable
{
    private static readonly JsonElement EmptyObjectSchema = JsonDocument.Parse("{\"type\":\"object\"}").RootElement;

    private FakePythonServer(McpServer server, Task runTask)
    {
        Server = server;
        RunTask = runTask;
    }

    public McpServer Server { get; }

    public Task RunTask { get; }

    /// <summary>A fake Python advertising Tools with one real "echo" tool, used by the
    /// production-composition fixtures that need a working tools/list + tools/call round trip.
    /// Strips the SDK-forced <c>logging</c> capability key from its own initialize response
    /// (see <see cref="StripSdkForcedLoggingCapability"/>) so it accurately represents real,
    /// unmodified netcoredbg_mcp, which genuinely advertises no logging capability - without
    /// this, every capability-aware host-side logging projection would observe a Logging
    /// capability no real Python backend actually has.</summary>
    public static FakePythonServer StartWithEchoTool(DuplexChannel channel, string name = "fake-python")
    {
        var options = new McpServerOptions
        {
            ServerInfo = new Implementation { Name = name, Version = "1.0.0" },
            Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
            Handlers = new McpServerHandlers
            {
                ListToolsHandler = (context, cancellationToken) => ValueTask.FromResult(new ListToolsResult
                {
                    Tools = [new Tool { Name = "echo", InputSchema = EmptyObjectSchema }],
                }),
                CallToolHandler = (context, cancellationToken) => ValueTask.FromResult(new CallToolResult
                {
                    Content = [new TextContentBlock { Text = "ok" }],
                }),
            },
        };
        StripSdkForcedLoggingCapability(options);
        return Start(channel, options);
    }

    /// <summary>
    /// SDK 1.4.1's <c>McpServerImpl.ConfigureLogging</c> unconditionally sets
    /// <see cref="ServerCapabilities.Logging"/> regardless of <c>options.Capabilities</c>
    /// (verified directly against the compiled SDK, same as
    /// <c>RelayRouteCatalog.SuppressUnregisteredLogging</c>'s own baseline finding for the
    /// host itself). Adds an outgoing filter that strips the same JSON key from this fake's
    /// own initialize response so it accurately represents "Python has no logging
    /// capability" the way real, unmodified netcoredbg_mcp genuinely does not.
    /// </summary>
    private static void StripSdkForcedLoggingCapability(McpServerOptions options)
    {
        options.Filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
        {
            if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                && result.TryGetPropertyValue("capabilities", out var capabilitiesNode)
                && capabilitiesNode is JsonObject capabilities)
            {
                capabilities.Remove("logging");
            }

            await next(context, cancellationToken).ConfigureAwait(false);
        });
    }

    /// <summary>A fake Python that never advertises Tools, for the missing-capability fixture.</summary>
    public static FakePythonServer StartWithoutTools(DuplexChannel channel, string name = "fake-python-no-tools") =>
        Start(channel, new McpServerOptions { ServerInfo = new Implementation { Name = name, Version = "1.0.0" } });

    /// <summary>A fake Python with custom options, for reverse-route and lifecycle fixtures.</summary>
    public static FakePythonServer Start(DuplexChannel channel, McpServerOptions options)
    {
        var server = McpServer.Create(channel.CreateServerTransport(options.ServerInfo?.Name), options);
        return new FakePythonServer(server, server.RunAsync());
    }

    public ValueTask DisposeAsync() => Server.DisposeAsync();
}
