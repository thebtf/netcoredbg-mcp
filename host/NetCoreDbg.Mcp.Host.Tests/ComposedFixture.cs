using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Pairs the real <see cref="RelayComposition.Build"/> output against a real fake-Python
/// <see cref="FakePythonServer"/> and a real downstream <see cref="McpClient"/>, all over
/// in-memory <see cref="DuplexChannel"/>s. This is the production composition path end to
/// end - the only substitution is the transport (in-memory instead of stdio/process) and
/// the "Python" implementation (a fake with a real "echo" tool instead of netcoredbg_mcp).
/// </summary>
internal sealed class ComposedFixture : IAsyncDisposable
{
    private ComposedFixture(FakePythonServer fakePython, RelaySession session, IHost host, McpClient downstreamClient)
    {
        FakePython = fakePython;
        Session = session;
        Host = host;
        DownstreamClient = downstreamClient;
    }

    public FakePythonServer FakePython { get; }

    public RelaySession Session { get; }

    public IHost Host { get; }

    public McpClient DownstreamClient { get; }

    public static async Task<ComposedFixture> StartAsync(
        Func<ClientCapabilities?, ClientCapabilities>? projectReverseRouteCapabilities = null,
        Action<McpClientHandlers>? configureUpstreamHandlers = null)
    {
        var upstreamChannel = new DuplexChannel();
        var fakePython = FakePythonServer.StartWithEchoTool(upstreamChannel);

        var session = new RelaySession(
            upstreamChannel.CreateClientTransport,
            RelayComposition.RequiredUpstreamCapabilityChecks,
            configureUpstreamHandlers);

        var downstreamChannel = new DuplexChannel();
        var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            projectReverseRouteCapabilities ?? (static _ => new ClientCapabilities()));
        _ = host.RunAsync();

        var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        return new ComposedFixture(fakePython, session, host, downstreamClient);
    }

    public async ValueTask DisposeAsync()
    {
        await DownstreamClient.DisposeAsync();
        await Host.StopAsync();
        Host.Dispose();
        await Session.DisposeAsync();
        await FakePython.DisposeAsync();
    }
}
