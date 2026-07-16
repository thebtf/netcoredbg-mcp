using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Pairs the real <see cref="RelayComposition.Build"/> output against a real fake-Python
/// <see cref="FakePythonServer"/> and a real downstream <see cref="McpClient"/>, all over
/// in-memory <see cref="DuplexChannel"/>s, using the exact Program.cs upstream chain
/// (ProgressLogging wrap over ResourceUpdates wrap), shared NotificationState, Roots plus
/// ResourceUpdates handlers configured once, and <see cref="ResourceUpdatesRelay.OrderedUpstream.WaitForDrainAsync"/>
/// as the RelaySession terminal drain hook. The only substitution is the transport
/// (in-memory instead of stdio/process) and the "Python" implementation.
/// </summary>
internal sealed class ComposedFixture : IAsyncDisposable
{
    private ComposedFixture(
        FakePythonServer fakePython,
        RelaySession session,
        IHost host,
        McpClient downstreamClient,
        ResourceUpdatesRelay.OrderedUpstream resourceUpdates,
        ProgressLoggingRelay.NotificationState progressNotificationState,
        RootsRelay rootsRelay)
    {
        FakePython = fakePython;
        Session = session;
        Host = host;
        DownstreamClient = downstreamClient;
        ResourceUpdates = resourceUpdates;
        ProgressNotificationState = progressNotificationState;
        RootsRelay = rootsRelay;
    }

    public FakePythonServer FakePython { get; }

    public RelaySession Session { get; }

    public IHost Host { get; }

    public McpClient DownstreamClient { get; }

    public ResourceUpdatesRelay.OrderedUpstream ResourceUpdates { get; }

    public ProgressLoggingRelay.NotificationState ProgressNotificationState { get; }

    public RootsRelay RootsRelay { get; }

    public static async Task<ComposedFixture> StartAsync(
        Action<McpClientHandlers>? configureUpstreamHandlers = null,
        Func<DuplexChannel, FakePythonServer>? startFakePython = null,
        McpClientOptions? downstreamClientOptions = null,
        Func<IClientTransport, IClientTransport>? wrapDownstreamTransport = null)
    {
        var upstreamChannel = new DuplexChannel();
        var fakePython = (startFakePython ?? (channel => FakePythonServer.StartWithEchoTool(channel)))(upstreamChannel);

        var rootsRelay = new RootsRelay();
        var resourceUpdates = ResourceUpdatesRelay.CreateOrderedUpstream();
        var progressNotificationState = new ProgressLoggingRelay.NotificationState();
        RelaySession session = null!;
        session = new RelaySession(
            () => ProgressLoggingRelay.WrapUpstreamTransport(
                resourceUpdates.WrapTransport(upstreamChannel.CreateClientTransport()),
                session,
                progressNotificationState),
            RelayComposition.RequiredUpstreamCapabilityChecks,
            handlers =>
            {
                rootsRelay.ConfigureUpstreamHandlers(handlers, session);
                resourceUpdates.ConfigureHandlers(handlers, session);
                configureUpstreamHandlers?.Invoke(handlers);
            },
            resourceUpdates.WaitForDrainAsync);

        var downstreamChannel = new DuplexChannel();
        var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(
                downstreamChannel.ServerInputStream,
                downstreamChannel.ServerOutputStream),
            // Same Program.cs projector: one RootsRelay instance owns both projection and
            // the gated upstream RootsHandler registration.
            caps => rootsRelay.ProjectCapabilities(caps, new ClientCapabilities()),
            progressNotificationState);
        _ = host.RunAsync();

        var clientTransport = (IClientTransport)downstreamChannel.CreateClientTransport();
        if (wrapDownstreamTransport is not null)
        {
            clientTransport = wrapDownstreamTransport(clientTransport);
        }

        var downstreamClient = await McpClient.CreateAsync(clientTransport, downstreamClientOptions);

        return new ComposedFixture(
            fakePython,
            session,
            host,
            downstreamClient,
            resourceUpdates,
            progressNotificationState,
            rootsRelay);
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
