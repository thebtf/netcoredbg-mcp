using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Proves <see cref="ResourcesRelay"/> (FD-005) using real SDK endpoints
/// (<c>McpServer</c>/<c>McpClient</c>), never mocks, over in-memory
/// <see cref="DuplexChannel"/>s. <see cref="ResourcesRelay"/> is not yet part of the
/// production <c>RelayComposition.Build</c> module list - per architecture.md, "the
/// integrator adds the accepted module to the central list after checker PASS" - so
/// <see cref="ResourcesTestComposition.BuildHost"/> is FD-005's own test-only composition,
/// the same convention <c>ReverseRouteAndLifecycleTests</c> already uses to prove FD-000's
/// reverse route without going through the production composition. It mirrors
/// <c>RelayComposition.Build</c> exactly (tools + logging suppression + the bootstrap
/// filter) and additionally registers <see cref="ResourcesRelay"/>; no line here differs
/// from what the integrator will add to the real file.
///
/// Canned resource metadata mirrors the exact four direct-Python resources (name, uri,
/// description, mimeType) captured from a real <c>python -m netcoredbg_mcp</c> process, so
/// forwarding-equality assertions below double as a documented contract snapshot;
/// <see cref="ResourcesRealPythonTests"/> proves the same module against genuine stdio
/// Python, and <c>tests/critical/test_resources_relay_critical.py</c> is the direct-Python
/// ground truth for the exact contract.
/// </summary>
public sealed class ResourcesRelayTests
{
    private const string StateUri = "debug://state";
    private const string BreakpointsUri = "debug://breakpoints";
    private const string OutputUri = "debug://output";
    private const string ThreadsUri = "debug://threads";

    private static readonly Resource DebugStateResource = new()
    {
        Name = "debug_state_resource",
        Uri = StateUri,
        Description =
            "Current debug session state (JSON).\n\n" +
            "Contains: status, stop_reason, threads, process info.\n" +
            "Updates when: session starts/stops, breakpoint hit, step completes.\n",
        MimeType = "application/json",
    };

    private static readonly Resource DebugBreakpointsResource = new()
    {
        Name = "debug_breakpoints_resource",
        Uri = BreakpointsUri,
        Description =
            "All active breakpoints (JSON).\n\n" +
            "Contains: file paths with line numbers, conditions, verified status.\n" +
            "Updates when: breakpoints added/removed/verified.\n",
        MimeType = "application/json",
    };

    private static readonly Resource DebugOutputResource = new()
    {
        Name = "debug_output_resource",
        Uri = OutputUri,
        Description =
            "Debug console output (plain text).\n\n" +
            "Contains: stdout/stderr from debugged process.\n" +
            "Updates when: new output arrives.\n",
        MimeType = "text/plain",
    };

    private static readonly Resource DebugThreadsResource = new()
    {
        Name = "debug_threads_resource",
        Uri = ThreadsUri,
        Description =
            "Current threads in the debugged process (JSON).\n\n" +
            "Contains: thread id and name for each active thread.\n" +
            "Updates when: process stops (breakpoint, step, pause).\n",
        MimeType = "application/json",
    };

    private static readonly IReadOnlyDictionary<string, ReadResourceResult> CannedReads = new Dictionary<string, ReadResourceResult>
    {
        [StateUri] = ReadResult(StateUri, "application/json", "{\"execState\":\"idle\"}"),
        [BreakpointsUri] = ReadResult(BreakpointsUri, "application/json", "{}"),
        [OutputUri] = ReadResult(OutputUri, "text/plain", ""),
        [ThreadsUri] = ReadResult(ThreadsUri, "application/json", "[]"),
    };

    private static ReadResourceResult ReadResult(string uri, string mimeType, string text) =>
        new() { Contents = [new TextResourceContents { Uri = uri, MimeType = mimeType, Text = text }] };

    private static ValueTask<ReadResourceResult> DefaultReadResourceHandler(
        RequestContext<ReadResourceRequestParams> context, CancellationToken cancellationToken) =>
        CannedReads.TryGetValue(context.Params!.Uri, out var result)
            ? ValueTask.FromResult(result)
            : throw new McpProtocolException($"Unknown resource: {context.Params!.Uri}", McpErrorCode.InvalidParams);

    private static (RelaySession Session, DuplexChannel Upstream, DuplexChannel Downstream) BuildResourcesSession()
    {
        var upstreamChannel = new DuplexChannel();
        var downstreamChannel = new DuplexChannel();
        var session = new RelaySession(upstreamChannel.CreateClientTransport, RelayComposition.RequiredUpstreamCapabilityChecks);

        var host = ResourcesTestComposition.BuildHost(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));
        _ = host.RunAsync();

        return (session, upstreamChannel, downstreamChannel);
    }

    private static McpServerOptions FakePythonWithResourcesOptions(
        McpRequestHandler<ReadResourceRequestParams, ReadResourceResult>? readHandlerOverride = null)
    {
        McpRequestHandler<ReadResourceRequestParams, ReadResourceResult> readHandler =
            readHandlerOverride is not null ? readHandlerOverride : DefaultReadResourceHandler;

        return new McpServerOptions
        {
            ServerInfo = new Implementation { Name = "fake-python-resources", Version = "1.0.0" },
            Capabilities = new ServerCapabilities
            {
                Tools = new ToolsCapability(),
                Resources = new ResourcesCapability { Subscribe = false, ListChanged = false },
            },
            Handlers = new McpServerHandlers
            {
                ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                ListResourcesHandler = (context, ct) => ValueTask.FromResult(new ListResourcesResult
                {
                    Resources = [DebugStateResource, DebugBreakpointsResource, DebugOutputResource, DebugThreadsResource],
                }),
                ListResourceTemplatesHandler = (context, ct) => ValueTask.FromResult(new ListResourceTemplatesResult
                {
                    ResourceTemplates = [],
                }),
                ReadResourceHandler = readHandler,
            },
        };
    }

    [Fact]
    public async Task ResourcesCapability_ProjectedWithSubscribeAndListChangedFalse_WhenPythonAdvertisesIt()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildResourcesSession();
        await using var fakePython = FakePythonServer.Start(upstreamChannel, FakePythonWithResourcesOptions());

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var resources = downstreamClient.ServerCapabilities?.Resources;
        Assert.NotNull(resources);
        Assert.False(resources!.Subscribe);
        Assert.False(resources.ListChanged);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ResourcesCapability_AbsentAndRequestsFailClosed_WhenPythonLacksIt()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildResourcesSession();

        // A fake Python that never advertises Resources and registers no resource handlers
        // at all - exactly the FD-003-documented shape of a backend without this
        // capability, proving projection is conditional rather than always-on.
        await using var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python-no-resources", Version = "1.0.0" },
                Capabilities = new ServerCapabilities { Tools = new ToolsCapability() },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                },
            });

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        Assert.Null(downstreamClient.ServerCapabilities?.Resources);

        // A client that calls resources/list anyway despite the missing capability still
        // reaches Python via the relay's raw forwarding - and gets Python's own natural
        // "Method not found" rejection, the same as a direct (non-proxied) Python session
        // would give it. The host never substitutes an empty result or a host-defined error.
        var error = await Assert.ThrowsAsync<McpProtocolException>(
            async () => await downstreamClient.ListResourcesAsync(new ListResourcesRequestParams()));
        Assert.Equal(McpErrorCode.MethodNotFound, error.ErrorCode);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ResourcesList_ForwardsExactFourResourcesUnchanged()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildResourcesSession();
        await using var fakePython = FakePythonServer.Start(upstreamChannel, FakePythonWithResourcesOptions());
        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var result = await downstreamClient.ListResourcesAsync(new ListResourcesRequestParams());

        Assert.Null(result.NextCursor);
        Assert.Equal(
            new[] { DebugStateResource, DebugBreakpointsResource, DebugOutputResource, DebugThreadsResource }
                .Select(r => (r.Uri, r.Name, r.Description, r.MimeType)),
            result.Resources.Select(r => (r.Uri, r.Name, r.Description, r.MimeType)));

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ResourceTemplatesList_ForwardsEmptyListUnchanged()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildResourcesSession();
        await using var fakePython = FakePythonServer.Start(upstreamChannel, FakePythonWithResourcesOptions());
        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        // A cursor-less call with no params object at all, exactly like a client that omits
        // `params` entirely - proves the null-Params-to-empty-object handling in
        // ResourcesRelay.Register does not silently swallow the empty-template contract.
        var response = await downstreamClient.SendRequestAsync(
            new JsonRpcRequest { Method = RequestMethods.ResourcesTemplatesList },
            CancellationToken.None);
        var result = response.Result.Deserialize<ListResourceTemplatesResult>(McpJsonUtilities.DefaultOptions)!;

        Assert.Empty(result.ResourceTemplates);
        Assert.Null(result.NextCursor);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ReadResource_ForwardsContentForEachKnownUri()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildResourcesSession();
        await using var fakePython = FakePythonServer.Start(upstreamChannel, FakePythonWithResourcesOptions());
        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        foreach (var (uri, expected) in CannedReads)
        {
            var result = await downstreamClient.ReadResourceAsync(uri);
            var expectedContent = Assert.IsType<TextResourceContents>(expected.Contents[0]);
            var actualContent = Assert.IsType<TextResourceContents>(result.Contents[0]);
            Assert.Equal(expectedContent.Uri, actualContent.Uri);
            Assert.Equal(expectedContent.MimeType, actualContent.MimeType);
            Assert.Equal(expectedContent.Text, actualContent.Text);
        }

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ReadResource_InvalidUri_ForwardsPythonsProtocolErrorUnchanged()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildResourcesSession();
        await using var fakePython = FakePythonServer.Start(upstreamChannel, FakePythonWithResourcesOptions());
        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var error = await Assert.ThrowsAsync<McpProtocolException>(
            async () => await downstreamClient.ReadResourceAsync("debug://not-a-real-resource"));
        Assert.Equal(McpErrorCode.InvalidParams, error.ErrorCode);
        Assert.Contains("Unknown resource", error.Message);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ResourcesList_PreservesCursorAndMetaOpaquely()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildResourcesSession();

        string? observedCursor = null;
        System.Text.Json.Nodes.JsonNode? observedMeta = null;
        await using var fakePython = FakePythonServer.Start(
            upstreamChannel,
            new McpServerOptions
            {
                ServerInfo = new Implementation { Name = "fake-python-cursor", Version = "1.0.0" },
                Capabilities = new ServerCapabilities
                {
                    Tools = new ToolsCapability(),
                    Resources = new ResourcesCapability { Subscribe = false, ListChanged = false },
                },
                Handlers = new McpServerHandlers
                {
                    ListToolsHandler = (context, ct) => ValueTask.FromResult(new ListToolsResult { Tools = [] }),
                    ListResourcesHandler = (context, ct) =>
                    {
                        observedCursor = context.Params?.Cursor;
                        observedMeta = context.Params?.Meta is { } meta
                            ? System.Text.Json.JsonSerializer.SerializeToNode(meta, McpJsonUtilities.DefaultOptions)
                            : null;
                        return ValueTask.FromResult(new ListResourcesResult
                        {
                            Resources = [],
                            NextCursor = "next-page-token",
                        });
                    },
                },
            });

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        var response = await downstreamClient.SendRequestAsync(
            new JsonRpcRequest
            {
                Method = RequestMethods.ResourcesList,
                Params = System.Text.Json.Nodes.JsonNode.Parse(
                    """{"cursor":"opaque-cursor-123","_meta":{"caller":"fd005-test"}}"""),
            },
            CancellationToken.None);
        var result = response.Result.Deserialize<ListResourcesResult>(McpJsonUtilities.DefaultOptions)!;

        Assert.Equal("opaque-cursor-123", observedCursor);
        Assert.NotNull(observedMeta);
        Assert.Equal("fd005-test", observedMeta!["caller"]!.GetValue<string>());
        Assert.Equal("next-page-token", result.NextCursor);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
    }

    [Fact]
    public async Task ReadResource_CancellationAndCallerDeadlinePropagate()
    {
        var (session, upstreamChannel, downstreamChannel) = BuildResourcesSession();
        var upstreamCallStarted = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);

        await using var fakePython = FakePythonServer.Start(
            upstreamChannel,
            FakePythonWithResourcesOptions(readHandlerOverride: async (context, ct) =>
            {
                upstreamCallStarted.TrySetResult();
                await Task.Delay(Timeout.Infinite, ct);
                return new ReadResourceResult();
            }));

        await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

        using var deadline = new CancellationTokenSource(TimeSpan.FromMilliseconds(300));
        var call = downstreamClient.ReadResourceAsync(StateUri, cancellationToken: deadline.Token).AsTask();
        await upstreamCallStarted.Task;

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => call);

        await downstreamClient.DisposeAsync();
        await session.DisposeAsync();
    }
}
