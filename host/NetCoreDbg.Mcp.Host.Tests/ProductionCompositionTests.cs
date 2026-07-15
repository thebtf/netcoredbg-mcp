using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Proves RelayComposition's actual production wiring: duplicate route ownership fails
/// fast, the composed downstream host advertises no test-only reverse-route capability or
/// forced logging, native prompts are advertised and served, tools/list and tools/call still
/// forward unchanged, ping is answered by the SDK itself with zero upstream paired, and a
/// Python backend missing a required capability fails bootstrap cleanly instead of serving a partial catalog.
/// Every fixture here pairs the real <see cref="RelayComposition.Build"/> output against a real
/// McpClient/McpServer over an in-memory <see cref="DuplexChannel"/> - the same production
/// code path, a different transport.
/// </summary>
public sealed class ProductionCompositionTests
{
    [Fact]
    public void DuplicateRouteRegistration_Throws()
    {
        var catalog = new RelayRouteCatalog();
        catalog.Add(new RelayRoute(RequestMethods.ToolsList, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request));

        var duplicate = Assert.Throws<InvalidOperationException>(() =>
            catalog.Add(new RelayRoute(RequestMethods.ToolsList, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request)));
        Assert.Contains("Duplicate relay route ownership", duplicate.Message);

        // The same method name in the opposite direction is a distinct, allowed route.
        catalog.Add(new RelayRoute(RequestMethods.ToolsList, RelayDirection.UpstreamToDownstream, RelayRouteKind.Request));
    }

    [Fact]
    public async Task ProductionComposition_AdvertisesNoTestOnlyReverseCapability()
    {
        await using var fixture = await ComposedFixture.StartAsync();

        // The production projector (RelayComposition.RunAsync's static _ => new
        // ClientCapabilities()) is exactly what Build uses here too, so the capabilities our
        // host advertised to "Python" must carry nothing from Roots/Sampling/Elicitation -
        // no test-only route or capability ever reaches this composition.
        var advertisedToPython = fixture.FakePython.Server.ClientCapabilities;
        Assert.NotNull(advertisedToPython);
        Assert.Null(advertisedToPython!.Roots);
        Assert.Null(advertisedToPython.Sampling);
        Assert.Null(advertisedToPython.Elicitation);
    }

    [Fact]
    public async Task ProductionComposition_DoesNotAdvertiseLoggingAndRejectsSetLevel()
    {
        await using var fixture = await ComposedFixture.StartAsync();

        Assert.Null(fixture.DownstreamClient.ServerCapabilities?.Logging);

        var error = await Assert.ThrowsAsync<McpProtocolException>(
            () => fixture.DownstreamClient.SetLoggingLevelAsync(LoggingLevel.Info));
        Assert.Contains("Method not found", error.Message);
    }

    [Fact]
    public async Task ProductionComposition_AdvertisesAndServesNativePrompts()
    {
        await using var fixture = await ComposedFixture.StartAsync();

        var promptsCapability = fixture.DownstreamClient.ServerCapabilities.Prompts;
        Assert.NotNull(promptsCapability);
        Assert.False(promptsCapability!.ListChanged);

        var prompts = await fixture.DownstreamClient.ListPromptsAsync(new ListPromptsRequestParams());
        Assert.Equal(
            new[]
            {
                "debug", "debug-gui", "debug-exception", "debug-visual", "debug-mistakes",
                "investigate", "debug-scenario", "dap-escape-hatch",
            },
            prompts.Prompts.Select(prompt => prompt.Name));

        var rendered = await fixture.DownstreamClient.GetPromptAsync("debug");
        Assert.False(string.IsNullOrWhiteSpace(rendered.Description));
        Assert.NotEmpty(rendered.Messages);
    }

    [Fact]
    public async Task ToolsListAndCallTool_ForwardUnchanged()
    {
        await using var fixture = await ComposedFixture.StartAsync();

        var tools = await fixture.DownstreamClient.ListToolsAsync(new ListToolsRequestParams());
        Assert.Contains(tools.Tools, tool => tool.Name == "echo");

        var callResult = await fixture.DownstreamClient.CallToolAsync(
            new CallToolRequestParams { Name = "echo", Arguments = new Dictionary<string, System.Text.Json.JsonElement>() });
        Assert.True(callResult.IsError is null or false);
    }

    [Fact]
    public async Task Ping_IsAnsweredWithNoUpstreamPairedAtAll()
    {
        // ping is core SDK infrastructure, never a relay route: a bare downstream McpServer
        // built with zero RelaySession/bootstrap wiring still answers it.
        var channel = new DuplexChannel();
        var server = McpServer.Create(
            channel.CreateServerTransport("ping-only"),
            new McpServerOptions { ServerInfo = new Implementation { Name = "ping-only", Version = "1.0.0" } });
        _ = server.RunAsync();

        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport());
        var pingResult = await client.PingAsync();

        Assert.NotNull(pingResult);
        await server.DisposeAsync();
    }

    [Fact]
    public async Task MissingUpstreamCapability_FailsBootstrapAndEndsSession()
    {
        var upstreamChannel = new DuplexChannel();
        await using var fakePython = FakePythonServer.StartWithoutTools(upstreamChannel);

        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
        var downstreamChannel = new DuplexChannel();
        using var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities());
        _ = host.RunAsync();

        await Assert.ThrowsAsync<McpProtocolException>(
            () => McpClient.CreateAsync(downstreamChannel.CreateClientTransport()));

        var sessionEndedFailure = await Assert.ThrowsAsync<InvalidOperationException>(
            () => session.RunUntilSessionEndedAsync(CancellationToken.None));
        Assert.Contains("do not cover a route", sessionEndedFailure.Message);

        await host.StopAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ForwardRequestAsync_NormalizesRemoteErrorToOneHopCleanMessageAndCode()
    {
        // FD-004/FD-005 proved McpSession.SendRequestAsync itself already wraps a remote
        // JSON-RPC error as "Request failed (remote): <message>", and a downstream typed
        // handler that rethrows a forwarded failure gets wrapped again by the SDK for its
        // own caller - doubling the prefix across a multi-hop relay. This proves the shared
        // primitive itself stays one-hop-clean: a real McpServer with no prompts handler at
        // all produces a genuine SDK remote error, and ForwardRequestAsync's result must
        // carry the prefix exactly once, with the original ErrorCode preserved and the raw
        // SDK exception kept as InnerException.
        var channel = new DuplexChannel();
        var server = McpServer.Create(
            channel.CreateServerTransport("no-handlers"),
            new McpServerOptions { ServerInfo = new Implementation { Name = "no-handlers", Version = "1.0.0" } });
        _ = server.RunAsync();

        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport());

        var request = new JsonRpcRequest { Method = RequestMethods.PromptsList };
        var error = await Assert.ThrowsAsync<McpProtocolException>(
            () => RelaySession.ForwardRequestAsync(client, request, CancellationToken.None));

        Assert.DoesNotContain("Request failed (remote): Request failed (remote):", error.Message);
        Assert.False(error.Message.StartsWith("Request failed (remote): ", StringComparison.Ordinal), error.Message);
        var inner = Assert.IsType<McpProtocolException>(error.InnerException);
        Assert.StartsWith("Request failed (remote): ", inner.Message, StringComparison.Ordinal);
        Assert.Equal(inner.ErrorCode, error.ErrorCode);

        await server.DisposeAsync();
    }

    [Fact]
    public async Task ForwardRequestAsync_PreservesPrimitiveAndNestedDataEntriesAcrossNormalization()
    {
        // McpSessionHandler.ProcessMessagesCoreAsync only calls ConvertExceptionData in the
        // McpProtocolException branch (a plain McpException or CLR exception is sanitized to
        // a bare InternalError with no data) - so the target must throw McpProtocolException
        // with Data populated to produce a genuine wire-level error.data payload. The
        // upstream client's own CreateRemoteProtocolException repopulates Exception.Data
        // from that payload; ForwardRequestAsync must carry every entry onto the normalized
        // exception it throws, not just the inner exception, since downstream dispatch
        // reserializes Data directly.
        var channel = new DuplexChannel();
        var server = McpServer.Create(
            channel.CreateServerTransport("throws-with-data"),
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "throws-with-data", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, cancellationToken) =>
                    {
                        var ex = new McpProtocolException("boom", McpErrorCode.InvalidParams);
                        ex.Data["reason"] = "bad";
                        ex.Data["count"] = 3d;
                        ex.Data["nested"] = System.Text.Json.JsonDocument.Parse("{\"a\":[1,true]}").RootElement.Clone();
                        throw ex;
                    },
                    CallToolHandler = (context, cancellationToken) => ValueTask.FromResult(new CallToolResult()),
                },
            });
        _ = server.RunAsync();

        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport());

        var request = new JsonRpcRequest { Method = RequestMethods.ToolsList };
        var error = await Assert.ThrowsAsync<McpProtocolException>(
            () => RelaySession.ForwardRequestAsync(client, request, CancellationToken.None));

        Assert.Equal("boom", error.Message);
        Assert.Equal(McpErrorCode.InvalidParams, error.ErrorCode);
        Assert.Equal("bad", Assert.IsType<string>(error.Data["reason"]));
        Assert.Equal(3d, Assert.IsType<double>(error.Data["count"]));
        var nested = Assert.IsType<System.Text.Json.JsonElement>(error.Data["nested"]);
        Assert.Equal(System.Text.Json.JsonValueKind.Object, nested.ValueKind);
        Assert.Equal("{\"a\":[1,true]}", nested.GetRawText());

        await server.DisposeAsync();
    }
}
