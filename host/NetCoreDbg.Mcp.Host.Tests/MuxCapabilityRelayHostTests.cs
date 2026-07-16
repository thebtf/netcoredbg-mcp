using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// FD-007: proves <see cref="MuxCapabilityRelay"/>'s allowlisted projection through the
/// host via production <see cref="RelayComposition.Build"/> - a real downstream
/// <see cref="McpClient"/> reading a real <c>initialize</c> response produced by the
/// genuine SDK/relay machinery, over an in-memory <see cref="DuplexChannel"/>, paired
/// with a real (not mocked) <see cref="FakePythonServer"/> whose advertised
/// <c>experimental</c> capability is toggled per fixture.
///
/// <para>
/// These tests call <see cref="RelayComposition.Build"/> directly. That production path
/// already registers <see cref="MuxCapabilityRelay.RegisterCapabilityProjectionFilter"/>
/// (and omits any hardcoded upfront <c>Experimental</c> value), so no parallel test-only
/// composition helper is required.
/// </para>
/// </summary>
public sealed class MuxCapabilityRelayHostTests
{

    private static async Task<(RelaySession Session, IHost Host, McpClient DownstreamClient, FakePythonServer FakePython)> StartAsync(
        ServerCapabilities upstreamCapabilities)
    {
        var upstreamChannel = new DuplexChannel();
        var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python", Version = "1.0.0" },
                Capabilities = upstreamCapabilities,
            });

        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);

        var downstreamChannel = new DuplexChannel();
        var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            static _ => new ClientCapabilities());
        _ = host.RunAsync();

        var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());
        return (session, host, downstreamClient, fakePython);
    }

    private static async Task StopAsync(RelaySession session, IHost host, McpClient downstreamClient, FakePythonServer fakePython)
    {
        await downstreamClient.DisposeAsync();
        await host.StopAsync();
        host.Dispose();
        await session.DisposeAsync();
        await fakePython.DisposeAsync();
    }

    [Fact]
    public async Task PythonAdvertisesExactMuxSharing_HostProjectsOnlyThatKeyAndValue()
    {
        var (session, host, downstreamClient, fakePython) = await StartAsync(
            new ServerCapabilities
            {
                Tools = new ToolsCapability(),
                Experimental = new Dictionary<string, object>
                {
                    ["x-mux"] = System.Text.Json.JsonDocument.Parse("""{"sharing":"isolated"}""").RootElement,
                },
            });

        var experimental = downstreamClient.ServerCapabilities?.Experimental;
        Assert.NotNull(experimental);
        var key = Assert.Single(experimental!.Keys);
        Assert.Equal("x-mux", key);

        await StopAsync(session, host, downstreamClient, fakePython);
    }

    [Fact]
    public async Task PythonDoesNotAdvertiseXMuxAtAll_HostProjectsNoExperimentalCapability()
    {
        var (session, host, downstreamClient, fakePython) = await StartAsync(
            new ServerCapabilities { Tools = new ToolsCapability() });

        Assert.Null(downstreamClient.ServerCapabilities?.Experimental);

        await StopAsync(session, host, downstreamClient, fakePython);
    }

    [Fact]
    public async Task PythonAdvertisesWrongSharingValue_HostProjectsNoExperimentalCapability()
    {
        var (session, host, downstreamClient, fakePython) = await StartAsync(
            new ServerCapabilities
            {
                Tools = new ToolsCapability(),
                Experimental = new Dictionary<string, object>
                {
                    ["x-mux"] = System.Text.Json.JsonDocument.Parse("""{"sharing":"shared"}""").RootElement,
                },
            });

        Assert.Null(downstreamClient.ServerCapabilities?.Experimental);

        await StopAsync(session, host, downstreamClient, fakePython);
    }

    [Fact]
    public async Task PythonAdvertisesSiblingExperimentalCapabilities_HostNeverLeaksThemDownstream()
    {
        var (session, host, downstreamClient, fakePython) = await StartAsync(
            new ServerCapabilities
            {
                Tools = new ToolsCapability(),
                Experimental = new Dictionary<string, object>
                {
                    ["x-mux"] = System.Text.Json.JsonDocument.Parse("""{"sharing":"isolated"}""").RootElement,
                    ["y-other-experimental"] = System.Text.Json.JsonDocument.Parse("""{"secret":true}""").RootElement,
                },
            });

        var experimental = downstreamClient.ServerCapabilities?.Experimental;
        Assert.NotNull(experimental);
        Assert.True(experimental!.ContainsKey("x-mux"));
        Assert.False(experimental.ContainsKey("y-other-experimental"));
        Assert.Single(experimental);

        await StopAsync(session, host, downstreamClient, fakePython);
    }

}
