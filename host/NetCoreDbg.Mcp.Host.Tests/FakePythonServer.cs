using System.Text.Json;
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
    /// production-composition fixtures that need a working tools/list + tools/call round trip.</summary>
    public static FakePythonServer StartWithEchoTool(DuplexChannel channel, string name = "fake-python") =>
        Start(
            channel,
            new McpServerOptions
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
            });

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
