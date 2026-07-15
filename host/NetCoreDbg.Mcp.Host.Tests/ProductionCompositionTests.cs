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
/// forced logging, tools/list and tools/call still forward unchanged, ping is answered by
/// the SDK itself with zero upstream paired, and a Python backend missing a required
/// capability fails the bootstrap cleanly instead of serving a partial catalog. Every
/// fixture here pairs the real <see cref="RelayComposition.Build"/> output against a real
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
}
