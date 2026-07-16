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
    private readonly Func<ValueTask>? _awaitUpstreamHandlerDrain;
    private readonly CancellationTokenSource _sessionEndingCts = new();
    private readonly TaskCompletionSource<Task<McpClient>> _upstreamStarted =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly TaskCompletionSource _downstreamReady =
        new(TaskCreationOptions.RunContinuationsAsynchronously);
    private readonly object _bootstrapGate = new();
    private readonly object _downstreamRequestIdGate = new();
    private readonly HashSet<RequestId> _seenDownstreamRequestIds = [];
    private const string ForwardLegContextItemKey = "NetCoreDbg.Mcp.Host.ForwardLeg";
    private readonly object _forwardLegGate = new();
    private readonly Dictionary<RequestId, ForwardLeg> _forwardLegsByDownstreamId = [];
    private readonly Dictionary<RequestId, ForwardLeg> _forwardLegsByUpstreamId = [];
    private Exception? _forwardingFailure;

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
    /// <param name="awaitUpstreamHandlerDrain">
    /// Optional terminal hook for a reverse-route module that owns asynchronous callback
    /// work. It runs after SessionEndingToken is cancelled and before the upstream client
    /// and transport are disposed.
    /// </param>
    public RelaySession(
        Func<IClientTransport> createUpstreamTransport,
        IReadOnlyList<Func<ServerCapabilities?, string?>> requiredUpstreamCapabilityChecks,
        Action<McpClientHandlers>? configureUpstreamHandlers = null,
        Func<ValueTask>? awaitUpstreamHandlerDrain = null)
    {
        _createUpstreamTransport = createUpstreamTransport;
        _requiredUpstreamCapabilityChecks = requiredUpstreamCapabilityChecks;
        _configureUpstreamHandlers = configureUpstreamHandlers;
        _awaitUpstreamHandlerDrain = awaitUpstreamHandlerDrain;
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

    internal int RetainedDownstreamForwardLegCount
    {
        get
        {
            lock (_forwardLegGate)
            {
                return _forwardLegsByDownstreamId.Count;
            }
        }
    }

    internal int RetainedUpstreamForwardLegCount
    {
        get
        {
            lock (_forwardLegGate)
            {
                return _forwardLegsByUpstreamId.Count;
            }
        }
    }

    internal Exception? ForwardingFailure => Volatile.Read(ref _forwardingFailure);

    internal void CheckAddDownstreamRequestId(RequestId requestId)
    {
        lock (_downstreamRequestIdGate)
        {
            if (_seenDownstreamRequestIds.Add(requestId))
            {
                return;
            }
        }

        var duplicate = new InvalidOperationException(
            $"Duplicate downstream request ID '{requestId}' was already accepted by this relay session.");
        FailForwardingAndEndSession(duplicate);
        throw duplicate;
    }


    internal void ThrowIfForwardingFailed()
    {
        if (Volatile.Read(ref _forwardingFailure) is { } failure)
        {
            throw new InvalidOperationException("Relay forwarding has terminated.", failure);
        }
    }

    internal void FailForwarding(Exception failure)
    {
        var recordedFailure = Interlocked.CompareExchange(
            ref _forwardingFailure,
            failure,
            null) ?? failure;
        FailForwardLegs(recordedFailure);
    }

    internal void BindForwardLeg(JsonRpcRequest request)
    {
        if (request.Context?.Items?.TryGetValue(ForwardLegContextItemKey, out var marker) != true
            || marker is not ForwardLeg leg)
        {
            return;
        }

        ThrowIfForwardingFailed();

        InvalidOperationException? collision = null;
        lock (_forwardLegGate)
        {
            if (!_forwardLegsByDownstreamId.TryGetValue(leg.DownstreamRequestId, out var current)
                || !ReferenceEquals(current, leg)
                || leg.State != ForwardLegState.Active)
            {
                return;
            }

            if (leg.UpstreamRequestId is not null)
            {
                collision = new InvalidOperationException(
                    $"Forwarded downstream request '{leg.DownstreamRequestId}' was assigned more than one upstream request ID.");
            }
            else if (_forwardLegsByUpstreamId.ContainsKey(request.Id))
            {
                collision = new InvalidOperationException(
                    $"Duplicate live upstream request ID '{request.Id}' cannot replace an existing forward leg.");
            }
            else
            {
                leg.UpstreamRequestId = request.Id;
                _forwardLegsByUpstreamId.Add(request.Id, leg);
            }
        }

        if (collision is not null)
        {
            FailForwardingAndEndSession(collision);
            throw collision;
        }
    }

    internal bool TryTakeForwardLegForUpstreamTerminal(
        JsonRpcMessage message,
        out ForwardLeg? leg)
    {
        var requestId = message switch
        {
            JsonRpcResponse response => response.Id,
            JsonRpcError error => error.Id,
            _ => (RequestId?)null,
        };
        if (requestId is not { } terminalRequestId)
        {
            leg = null;
            return false;
        }

        lock (_forwardLegGate)
        {
            if (_forwardLegsByUpstreamId.Remove(terminalRequestId, out leg))
            {
                leg.UpstreamRequestId = null;
                return true;
            }
        }

        leg = null;
        return false;
    }

    internal bool TryGetForwardLegForDownstreamTerminal(
        JsonRpcMessage message,
        out ForwardLeg? leg)
    {
        var requestId = message switch
        {
            JsonRpcResponse response => response.Id,
            JsonRpcError error => error.Id,
            _ => (RequestId?)null,
        };
        if (requestId is not { } terminalRequestId)
        {
            leg = null;
            return false;
        }

        lock (_forwardLegGate)
        {
            return _forwardLegsByDownstreamId.TryGetValue(terminalRequestId, out leg);
        }
    }

    internal void CompleteForwardLegSend(ForwardLeg leg)
    {
        CancellationTokenRegistration registration = default;
        var hasRegistration = false;
        lock (_forwardLegGate)
        {
            if (!_forwardLegsByDownstreamId.TryGetValue(leg.DownstreamRequestId, out var current)
                || !ReferenceEquals(current, leg))
            {
                return;
            }

            _forwardLegsByDownstreamId.Remove(leg.DownstreamRequestId);
            if (leg.UpstreamRequestId is { } upstreamRequestId
                && _forwardLegsByUpstreamId.TryGetValue(upstreamRequestId, out var upstreamLeg)
                && ReferenceEquals(upstreamLeg, leg))
            {
                _forwardLegsByUpstreamId.Remove(upstreamRequestId);
                leg.UpstreamRequestId = null;
            }

            if (leg.State == ForwardLegState.Active)
            {
                leg.State = ForwardLegState.Sent;
                leg.Publication.TrySetResult(ForwardLegCompletion.Sent);
            }

            registration = TakeCancellationRegistrationLocked(leg, out hasRegistration);
        }

        if (hasRegistration)
        {
            registration.Dispose();
        }
    }

    internal void ObserveDownstreamCancellation(JsonRpcNotification notification)
    {
        try
        {
            var requestId = notification.Params?
                .Deserialize<CancelledNotificationParams>(McpJsonUtilities.DefaultOptions)?.RequestId;
            if (requestId is { } typedRequestId)
            {
                ForwardLeg? leg;
                lock (_forwardLegGate)
                {
                    _forwardLegsByDownstreamId.TryGetValue(typedRequestId, out leg);
                }

                if (leg is not null)
                {
                    AbandonForwardLeg(leg);
                }
            }
        }
        catch (JsonException)
        {
            // Invalid cancellation notifications are ignored by the MCP protocol.
        }
    }

    internal void CompleteDownstreamRequestHandling(RequestId requestId)
    {
        CancellationTokenRegistration registration = default;
        var hasRegistration = false;
        lock (_forwardLegGate)
        {
            if (!_forwardLegsByDownstreamId.TryGetValue(requestId, out var leg)
                || leg.State != ForwardLegState.Abandoned)
            {
                return;
            }

            _forwardLegsByDownstreamId.Remove(requestId);
            registration = TakeCancellationRegistrationLocked(leg, out hasRegistration);
        }

        if (hasRegistration)
        {
            registration.Dispose();
        }
    }

    internal void FailForwardLegs(Exception failure)
    {
        List<CancellationTokenRegistration>? registrations = null;
        lock (_forwardLegGate)
        {
            if (_forwardLegsByDownstreamId.Count == 0)
            {
                _forwardLegsByUpstreamId.Clear();
                return;
            }

            foreach (var leg in _forwardLegsByDownstreamId.Values)
            {
                if (leg.State == ForwardLegState.Active)
                {
                    leg.State = ForwardLegState.Failed;
                    leg.Publication.TrySetException(failure);
                }

                var registration = TakeCancellationRegistrationLocked(leg, out var hasRegistration);
                if (hasRegistration)
                {
                    (registrations ??= []).Add(registration);
                }
            }

            _forwardLegsByDownstreamId.Clear();
            _forwardLegsByUpstreamId.Clear();
        }

        if (registrations is not null)
        {
            foreach (var registration in registrations)
            {
                registration.Dispose();
            }
        }
    }

    private void FailForwardingAndEndSession(Exception failure)
    {
        FailForwarding(failure);
        try
        {
            _sessionEndingCts.Cancel();
        }
        catch (ObjectDisposedException)
        {
            // Disposal already completed the same terminal transition.
        }
    }

    private ForwardLeg BeginForwardLeg(
        RequestId downstreamRequestId,
        CancellationToken cancellationToken)
    {

        var leg = new ForwardLeg(downstreamRequestId);
        InvalidOperationException? collision = null;
        lock (_forwardLegGate)
        {
            ThrowIfForwardingFailed();
            if (_sessionEndingCts.IsCancellationRequested)
            {
                throw new OperationCanceledException(_sessionEndingCts.Token);
            }

            if (_forwardLegsByDownstreamId.ContainsKey(downstreamRequestId))
            {
                collision = new InvalidOperationException(
                    $"Duplicate live downstream request ID '{downstreamRequestId}' cannot replace an existing forward leg.");
            }
            else
            {
                _forwardLegsByDownstreamId.Add(downstreamRequestId, leg);
            }
        }

        if (collision is not null)
        {
            FailForwardingAndEndSession(collision);
            throw collision;
        }

        if (cancellationToken.CanBeCanceled)
        {
            var registration = cancellationToken.Register(
                static state =>
                {
                    var tuple = (Tuple<RelaySession, ForwardLeg>)state!;
                    tuple.Item1.AbandonForwardLeg(tuple.Item2);
                },
                Tuple.Create(this, leg));
            var disposeRegistration = false;
            lock (_forwardLegGate)
            {
                if (leg.State == ForwardLegState.Active
                    && _forwardLegsByDownstreamId.TryGetValue(downstreamRequestId, out var current)
                    && ReferenceEquals(current, leg))
                {
                    leg.CancellationRegistration = registration;
                    leg.HasCancellationRegistration = true;
                }
                else
                {
                    disposeRegistration = true;
                }
            }

            if (disposeRegistration)
            {
                registration.Dispose();
            }
        }

        return leg;
    }

    private void AbandonForwardLeg(ForwardLeg leg)
    {
        lock (_forwardLegGate)
        {
            if (!_forwardLegsByDownstreamId.TryGetValue(leg.DownstreamRequestId, out var current)
                || !ReferenceEquals(current, leg)
                || leg.State != ForwardLegState.Active)
            {
                return;
            }

            leg.State = ForwardLegState.Abandoned;
            if (leg.UpstreamRequestId is { } upstreamRequestId
                && _forwardLegsByUpstreamId.TryGetValue(upstreamRequestId, out var upstreamLeg)
                && ReferenceEquals(upstreamLeg, leg))
            {
                _forwardLegsByUpstreamId.Remove(upstreamRequestId);
                leg.UpstreamRequestId = null;
            }

            leg.Publication.TrySetResult(ForwardLegCompletion.Abandoned);
        }
    }

    private static CancellationTokenRegistration TakeCancellationRegistrationLocked(
        ForwardLeg leg,
        out bool hasRegistration)
    {
        hasRegistration = leg.HasCancellationRegistration;
        leg.HasCancellationRegistration = false;
        return leg.CancellationRegistration;
    }

    internal enum ForwardLegCompletion
    {
        Sent,
        Abandoned,
    }

    internal sealed class ForwardLeg(RequestId downstreamRequestId)
    {
        public RequestId DownstreamRequestId { get; } = downstreamRequestId;

        public RequestId? UpstreamRequestId { get; set; }

        public TaskCompletionSource<ForwardLegCompletion> Publication { get; } =
            new(TaskCreationOptions.RunContinuationsAsynchronously);

        public CancellationTokenRegistration CancellationRegistration { get; set; }

        public bool HasCancellationRegistration { get; set; }

        public ForwardLegState State { get; set; }
    }

    internal enum ForwardLegState
    {
        Active,
        Abandoned,
        Sent,
        Failed,
    }

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
    /// The exact prefix <c>McpSessionHandler.SendRequestAsync</c> adds when the target leg
    /// returns a JSON-RPC error (observed directly against SDK 1.4.1: e.g.
    /// <c>"Request failed (remote): Method not found"</c>). A relayed request's error
    /// therefore already carries this prefix once by the time it reaches a downstream
    /// typed handler that itself wraps a thrown exception into another SDK-generated
    /// remote-error response for its own caller, doubling the prefix. Stripped here, once,
    /// so every leg of a multi-hop relay stays one-hop-clean regardless of how many
    /// forwarding calls the error passes through.
    /// </summary>
    private const string RemoteErrorMessagePrefix = "Request failed (remote): ";

    /// <summary>
    /// Raw downstream-to-upstream forward with one exact, typed two-leg correlation marker.
    /// The target request keeps the source method/params but receives a fresh target-leg ID;
    /// its context contains only the private non-wire marker consumed by the wrapped upstream
    /// transport. The downstream request's transport context is never reused.
    /// </summary>
    public Task<JsonRpcResponse> ForwardApplicationRequestAsync(
        McpSession target,
        JsonRpcRequest source,
        CancellationToken cancellationToken)
    {
        var leg = BeginForwardLeg(source.Id, cancellationToken);
        return SendForwardedRequestAsync(
            target,
            new JsonRpcRequest
            {
                Method = source.Method,
                Params = source.Params,
                Context = new JsonRpcMessageContext
                {
                    Items = new Dictionary<string, object?>
                    {
                        [ForwardLegContextItemKey] = leg,
                    },
                },
            },
            cancellationToken);
    }

    /// <summary>
    /// Raw, direction-agnostic uncorrelated forward used by reverse routes and focused shared
    /// primitive tests. Application requests flowing downstream-to-Python use
    /// <see cref="ForwardApplicationRequestAsync"/> instead.
    /// </summary>
    public static Task<JsonRpcResponse> ForwardRequestAsync(
        McpSession target,
        JsonRpcRequest source,
        CancellationToken cancellationToken) =>
        SendForwardedRequestAsync(
            target,
            new JsonRpcRequest { Method = source.Method, Params = source.Params },
            cancellationToken);

    private static async Task<JsonRpcResponse> SendForwardedRequestAsync(
        McpSession target,
        JsonRpcRequest request,
        CancellationToken cancellationToken)
    {
        try
        {
            return await target.SendRequestAsync(request, cancellationToken).ConfigureAwait(false);
        }
        catch (McpProtocolException ex) when (ex.Message.StartsWith(RemoteErrorMessagePrefix, StringComparison.Ordinal))
        {
            var normalized = new McpProtocolException(ex.Message[RemoteErrorMessagePrefix.Length..], ex, ex.ErrorCode);

            // CreateRemoteProtocolException stores the JSON-RPC error's "data" entries on
            // ex.Data; downstream dispatch reserializes that dictionary directly, not
            // InnerException.Data, so every entry must be copied onto the normalized
            // exception explicitly or wire-level error data silently disappears.
            foreach (System.Collections.DictionaryEntry entry in ex.Data)
            {
                normalized.Data[entry.Key] = entry.Value;
            }

            throw normalized;
        }
    }

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
        if (_awaitUpstreamHandlerDrain is not null)
        {
            await _awaitUpstreamHandlerDrain().ConfigureAwait(false);
        }

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

        FailForwardLegs(new OperationCanceledException("The relay session is ending.", _sessionEndingCts.Token));
        _sessionEndingCts.Dispose();
    }
}
