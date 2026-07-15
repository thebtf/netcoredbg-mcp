using System.Text.Json;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Pairs exactly one downstream <see cref="McpServer"/> session with exactly one upstream
/// Python <see cref="McpClient"/> session for the lifetime of one host process. Owns the
/// single-flight upstream bootstrap gate, the downstream-ready signal, generic raw
/// request/notification forwarding, and idempotent disposal. Relay state here is never
/// process-global: one instance per mcp-mux-isolated host process.
///
/// Relay modules (see <c>ToolsRelay.cs</c> for the canonical example, and FD-001/FD-002's
/// future <c>RootsRelay.cs</c>/<c>ProgressLoggingRelay.cs</c>) only ever call
/// <see cref="UpstreamAsync"/> and the static forwarding helpers; only the bootstrap filter
/// built by <see cref="CreateBootstrapFilter"/> calls <see cref="EnsureUpstreamStartedAsync"/>.
/// </summary>
internal sealed class RelaySession : IAsyncDisposable
{
    private readonly Func<IClientTransport> _createUpstreamTransport;
    private readonly IReadOnlyList<Func<ServerCapabilities?, string?>> _requiredUpstreamCapabilityChecks;
    private readonly Action<McpClientHandlers>? _configureUpstreamHandlers;
    private readonly CancellationTokenSource _sessionEndingCts = new();
    private readonly TaskCompletionSource<Task<McpClient>> _upstreamStarted =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _downstreamReady =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly object _bootstrapGate = new();

    private Task<McpClient>? _upstreamInitTask;
    private int _disposed;

    /// <param name="createUpstreamTransport">
    /// Builds the transport used for the one upstream handshake this session ever performs.
    /// Production supplies <see cref="PythonBackendProcess.CreateUpstreamTransport"/>; tests
    /// may supply any real <see cref="IClientTransport"/>, including an in-memory one, since
    /// this is a transport choice, not a mock of the session logic itself.
    /// </param>
    /// <param name="requiredUpstreamCapabilityChecks">
    /// Run against Python's advertised capabilities once the handshake completes; a non-null
    /// return from any check fails the bootstrap. Driven by whichever relay modules
    /// <c>RelayComposition</c> has registered for this build.
    /// </param>
    /// <param name="configureUpstreamHandlers">
    /// Optional: wires this host's own reverse-route client handlers (roots/sampling/
    /// elicitation) before the handshake. Production passes <see langword="null"/> - FD-000
    /// registers zero reverse-route modules, so no test-only route, capability, or handler
    /// ever reaches production composition. Only test-only fixtures and (later) accepted
    /// FD-001/FD-002 modules supply this.
    /// </param>
    public RelaySession(
        Func<IClientTransport> createUpstreamTransport,
        IReadOnlyList<Func<ServerCapabilities?, string?>> requiredUpstreamCapabilityChecks,
        Action<McpClientHandlers>? configureUpstreamHandlers = null)
    {
        _createUpstreamTransport = createUpstreamTransport;
        _requiredUpstreamCapabilityChecks = requiredUpstreamCapabilityChecks;
        _configureUpstreamHandlers = configureUpstreamHandlers;
    }

    /// <summary>The downstream server for this session, bound by the bootstrap filter on first message.</summary>
    public McpServer? Downstream { get; private set; }

    /// <summary>
    /// Cancelled once this session becomes terminal (disposal begins). Reverse-route
    /// handlers should link their own cancellation token to this one so an in-flight
    /// callback is cancelled on downstream disconnect or Python's own session ending.
    /// </summary>
    public CancellationToken SessionEndingToken => _sessionEndingCts.Token;

    /// <summary>
    /// Completes once the downstream <c>notifications/initialized</c> message has passed the
    /// SDK handler. Upstream reverse-request handlers must await this before their first use
    /// of the downstream session, so Python can never call a client capability before that
    /// client is actually operational.
    /// </summary>
    public Task DownstreamReady => _downstreamReady.Task;

    public void BindDownstream(McpServer server) => Downstream ??= server;

    /// <summary>
    /// Ordinary route handlers (any direction) call this to reach the already-bootstrapped
    /// upstream Python session. The SDK's own lifecycle guarantees no non-initialize request
    /// is handled before the bootstrap filter has run, so the bootstrap is always already
    /// under way by the time this is called.
    /// </summary>
    public Task<McpClient> UpstreamAsync(CancellationToken cancellationToken)
    {
        var initTask = _upstreamInitTask
            ?? throw new InvalidOperationException(
                "The upstream Python session has not been bootstrapped yet; UpstreamAsync must " +
                "only be called from a handler that runs after the downstream initialize request.");

        return initTask.WaitAsync(cancellationToken);
    }

    /// <summary>
    /// Bootstrap-only: starts the single-flight upstream Python handshake at
    /// <paramref name="effectiveProtocolVersion"/>, advertising
    /// <paramref name="reverseRouteCapabilities"/> as this host's own client capabilities to
    /// Python. Concurrent callers share one handshake; a failed handshake (including a
    /// capability-coverage mismatch) is terminal and is never retried into a half-open
    /// session. Called only by the filter built in <see cref="CreateBootstrapFilter"/>.
    /// </summary>
    private Task<McpClient> EnsureUpstreamStartedAsync(
        string effectiveProtocolVersion,
        ClientCapabilities reverseRouteCapabilities,
        CancellationToken cancellationToken)
    {
        lock (_bootstrapGate)
        {
            if (_upstreamInitTask is null)
            {
                _upstreamInitTask = CreateUpstreamAsync(effectiveProtocolVersion, reverseRouteCapabilities, cancellationToken);
                _upstreamStarted.TrySetResult(_upstreamInitTask);
            }
        }

        return _upstreamInitTask;
    }

    private async Task<McpClient> CreateUpstreamAsync(
        string effectiveProtocolVersion,
        ClientCapabilities reverseRouteCapabilities,
        CancellationToken cancellationToken)
    {
        var transport = _createUpstreamTransport();
        var options = new McpClientOptions
        {
            ProtocolVersion = effectiveProtocolVersion,
            Capabilities = reverseRouteCapabilities,
        };
        _configureUpstreamHandlers?.Invoke(options.Handlers);

        var client = await McpClient.CreateAsync(transport, options, cancellationToken: cancellationToken)
            .ConfigureAwait(false);

        foreach (var check in _requiredUpstreamCapabilityChecks)
        {
            var failure = check(client.ServerCapabilities);
            if (failure is not null)
            {
                await client.DisposeAsync().ConfigureAwait(false);
                throw new InvalidOperationException(
                    $"Python's advertised capabilities do not cover a route this host build advertises downstream: {failure}");
            }
        }

        return client;
    }

    /// <summary>
    /// Completes only when the relay session becomes permanently unable to continue: the
    /// bootstrap handshake (including capability validation) failed, or Python's own MCP
    /// session ended for any reason after a successful handshake - a clean early EOF is
    /// still failure. Never completes while no downstream client has yet triggered the
    /// bootstrap. The composition root races this against the downstream server's run task
    /// to force deterministic shutdown with a non-zero exit.
    /// </summary>
    public async Task RunUntilSessionEndedAsync(CancellationToken cancellationToken)
    {
        var upstreamTask = await _upstreamStarted.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
        var client = await upstreamTask.ConfigureAwait(false);
        await client.Completion.ConfigureAwait(false);
        throw new InvalidOperationException("The Python backend ended before the downstream MCP session closed.");
    }

    /// <summary>
    /// Builds the incoming-message bootstrap filter (architecture.md FD-000 steps 3-6): on
    /// the downstream <c>initialize</c> request it resolves the effective protocol version
    /// and starts the upstream Python handshake before the SDK's own initialize handler
    /// runs; on <c>notifications/initialized</c> it signals downstream readiness only after
    /// the SDK handler has processed it. The filter never holds a lock while awaiting either
    /// endpoint, and nested callback flow remains re-entrant since every awaited step here is
    /// per-message, not serialized behind a shared lock.
    /// </summary>
    public McpMessageFilter CreateBootstrapFilter(Func<ClientCapabilities?, ClientCapabilities> projectReverseRouteCapabilities) =>
        next => async (context, cancellationToken) =>
        {
            BindDownstream(context.Server);

            switch (context.JsonRpcMessage)
            {
                case JsonRpcRequest { Method: RequestMethods.Initialize } initializeRequest:
                    var initParams = initializeRequest.Params?.Deserialize<InitializeRequestParams>(McpJsonUtilities.DefaultOptions);
                    var effectiveVersion = RelayRouteCatalog.ResolveEffectiveProtocolVersion(initParams?.ProtocolVersion);
                    var reverseRouteCapabilities = projectReverseRouteCapabilities(initParams?.Capabilities);
                    await EnsureUpstreamStartedAsync(effectiveVersion, reverseRouteCapabilities, cancellationToken)
                        .ConfigureAwait(false);
                    await next(context, cancellationToken).ConfigureAwait(false);
                    break;

                case JsonRpcNotification { Method: NotificationMethods.InitializedNotification }:
                    await next(context, cancellationToken).ConfigureAwait(false);
                    _downstreamReady.TrySetResult();
                    break;

                default:
                    await next(context, cancellationToken).ConfigureAwait(false);
                    break;
            }
        };

    /// <summary>
    /// Raw, direction-agnostic forward: creates a fresh request on <paramref name="target"/>
    /// preserving method and params (including <c>_meta</c> and any progress token) exactly,
    /// and returns the target's raw response without host-defined DTO conversion. The SDK
    /// assigns a fresh connection-local ID for the target leg since the forwarded request's
    /// ID is left unset; the target's own cancellation semantics (its own
    /// <c>notifications/cancelled</c>) apply to the new leg independently of the source.
    /// </summary>
    public static Task<JsonRpcResponse> ForwardRequestAsync(
        McpSession target, JsonRpcRequest source, CancellationToken cancellationToken) =>
        target.SendRequestAsync(new JsonRpcRequest { Method = source.Method, Params = source.Params }, cancellationToken);

    /// <summary>
    /// Raw, direction-agnostic one-way forward: preserves method and params exactly and
    /// awaits the send so source ordering for that route is retained. Never adds a request ID.
    /// </summary>
    public static Task ForwardNotificationAsync(
        McpSession target, JsonRpcNotification source, CancellationToken cancellationToken) =>
        target.SendMessageAsync(new JsonRpcNotification { Method = source.Method, Params = source.Params }, cancellationToken);

    /// <summary>
    /// Idempotent: cancels <see cref="SessionEndingToken"/> for any in-flight reverse
    /// callback, then disposes the upstream Python client if the bootstrap ever started. Does
    /// not touch the underlying OS process; the composition root always stops
    /// <see cref="PythonBackendProcess"/> separately regardless of which path ended the
    /// session, exactly as it did before this session type existed.
    /// </summary>
    public async ValueTask DisposeAsync()
    {
        if (Interlocked.Exchange(ref _disposed, 1) != 0)
        {
            return;
        }

        _sessionEndingCts.Cancel();
        _sessionEndingCts.Dispose();

        if (_upstreamInitTask is { } initTask)
        {
            try
            {
                var client = await initTask.ConfigureAwait(false);
                await client.DisposeAsync().ConfigureAwait(false);
            }
            catch
            {
                // The handshake itself failed; there is no client to dispose.
            }
        }
    }
}
