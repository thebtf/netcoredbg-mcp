using System.Text.Json;
using System.Text.Json.Nodes;
using System.Threading.Channels;
using Microsoft.Extensions.DependencyInjection;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// FD-002 progress/logging relay plus the session's shared bounded upstream ingress. Forwards
/// Python progress, logging, and resource-update notifications to the real downstream client;
/// relays downstream <c>logging/setLevel</c> only when Python advertises logging; and holds each
/// wire-later item behind the exact downstream response/error send correlated to its upstream
/// terminal.
///
/// <see cref="Register"/> and <see cref="ConfigureFilters"/> own downstream routes, capability
/// projection, cancellation, and post-send acknowledgement. <see cref="WrapUpstreamTransport"/>
/// wraps <c>PythonBackendProcess.CreateUpstreamTransport</c> with the sole reader and bounded drain,
/// so progress/logging/resource ordering and response fences share one fail-closed pipeline.
/// </summary>
internal static class ProgressLoggingRelay
{
    internal sealed class NotificationState
    {
        private readonly object _gate = new();
        private readonly Dictionary<ProgressToken, ProgressRegistration> _activeProgressTokens = [];
        // RequestId equality preserves JSON-RPC type: numeric 1 and string "1" are distinct keys.
        private readonly Dictionary<RequestId, ProgressRegistration> _registrationsByRequestId = [];
        private readonly Dictionary<RequestId, ProgressRegistration> _registrationsByUpstreamRequestId = [];

        public ProgressToken? Begin(JsonRpcRequest request)
        {
            if (!TryReadMetaProgressToken(request.Params, out var progressToken))
            {
                return null;
            }

            var registration = new ProgressRegistration(progressToken);
            lock (_gate)
            {
                _activeProgressTokens[progressToken] = registration;
                _registrationsByRequestId[request.Id] = registration;
            }

            return progressToken;
        }

        public void End(RequestId requestId)
        {
            lock (_gate)
            {
                if (_registrationsByRequestId.Remove(requestId, out var registration))
                {
                    InvalidateLocked(registration);
                }
            }
        }

        public void Cancel(JsonRpcNotification notification)
        {
            try
            {
                var requestId = notification.Params?
                    .Deserialize<CancelledNotificationParams>(McpJsonUtilities.DefaultOptions)?.RequestId;
                if (requestId is { } typedRequestId)
                {
                    End(typedRequestId);
                }
            }
            catch (JsonException)
            {
                // Invalid cancellation notifications are ignored by the MCP protocol.
            }
        }

        public void TrackUpstreamRequest(JsonRpcRequest request)
        {
            if (!TryReadMetaProgressToken(request.Params, out var progressToken))
            {
                return;
            }

            lock (_gate)
            {
                if (_activeProgressTokens.TryGetValue(progressToken, out var registration))
                {
                    _registrationsByUpstreamRequestId[request.Id] = registration;
                    registration.UpstreamRequestIds.Add(request.Id);
                }
            }
        }

        public void ObserveUpstreamMessage(JsonRpcMessage message)
        {
            var requestId = message switch
            {
                JsonRpcResponse response => response.Id,
                JsonRpcError error => error.Id,
                _ => (RequestId?)null,
            };
            if (requestId is not { } terminalRequestId)
            {
                return;
            }

            lock (_gate)
            {
                if (_registrationsByUpstreamRequestId.Remove(terminalRequestId, out var registration))
                {
                    registration.UpstreamRequestIds.Remove(terminalRequestId);
                    RemoveActiveLocked(registration);
                }
            }
        }

        public bool IsActive(ProgressToken progressToken)
        {
            lock (_gate)
            {
                return _activeProgressTokens.ContainsKey(progressToken);
            }
        }

        public bool Allows(JsonRpcNotification notification) =>
            TryAuthorize(notification, out _);

        public bool TryAuthorize(
            JsonRpcNotification notification,
            out DeliveryAuthorization authorization)
        {
            if (notification.Method == NotificationMethods.ProgressNotification)
            {
                if (notification.Params is not JsonObject progressParams
                    || !TryReadProgressToken(progressParams["progressToken"], out var progressToken))
                {
                    authorization = default;
                    return false;
                }

                return TryAuthorize(progressToken, out authorization);
            }

            if (TryReadMetaProgressToken(notification.Params, out var loggingProgressToken))
            {
                return TryAuthorize(loggingProgressToken, out authorization);
            }

            authorization = default;
            return true;
        }

        public bool IsAuthorized(DeliveryAuthorization authorization) =>
            authorization.IsValid;

        private bool TryAuthorize(
            ProgressToken progressToken,
            out DeliveryAuthorization authorization)
        {
            lock (_gate)
            {
                if (_activeProgressTokens.TryGetValue(progressToken, out var registration)
                    && !registration.IsInvalidated)
                {
                    authorization = new DeliveryAuthorization(registration);
                    return true;
                }
            }

            authorization = default;
            return false;
        }

        private void InvalidateLocked(ProgressRegistration registration)
        {
            registration.Invalidate();
            RemoveActiveLocked(registration);
            foreach (var upstreamRequestId in registration.UpstreamRequestIds)
            {
                _registrationsByUpstreamRequestId.Remove(upstreamRequestId);
            }

            registration.UpstreamRequestIds.Clear();
        }

        private void RemoveActiveLocked(ProgressRegistration registration)
        {
            if (_activeProgressTokens.TryGetValue(registration.ProgressToken, out var activeRegistration)
                && ReferenceEquals(activeRegistration, registration))
            {
                _activeProgressTokens.Remove(registration.ProgressToken);
            }
        }

        private static bool TryReadMetaProgressToken(JsonNode? parameters, out ProgressToken progressToken)
        {
            if (parameters is JsonObject paramsObject
                && paramsObject["_meta"] is JsonObject meta
                && TryReadProgressToken(meta["progressToken"], out progressToken))
            {
                return true;
            }

            progressToken = default;
            return false;
        }

        private static bool TryReadProgressToken(JsonNode? value, out ProgressToken progressToken)
        {
            if (value is JsonValue jsonValue)
            {
                if (jsonValue.GetValueKind() == JsonValueKind.String)
                {
                    progressToken = new ProgressToken(jsonValue.GetValue<string>());
                    return true;
                }

                if (jsonValue.GetValueKind() == JsonValueKind.Number)
                {
                    progressToken = new ProgressToken(jsonValue.GetValue<long>());
                    return true;
                }
            }

            progressToken = default;
            return false;
        }

        public readonly struct DeliveryAuthorization
        {
            private readonly ProgressRegistration? _registration;

            internal DeliveryAuthorization(ProgressRegistration registration)
            {
                _registration = registration;
            }

            internal bool IsValid => _registration is null || !_registration.IsInvalidated;
        }

        internal sealed class ProgressRegistration(ProgressToken progressToken)
        {
            private int _invalidated;

            public ProgressToken ProgressToken { get; } = progressToken;

            public HashSet<RequestId> UpstreamRequestIds { get; } = [];

            public bool IsInvalidated => Volatile.Read(ref _invalidated) != 0;

            public void Invalidate() => Interlocked.Exchange(ref _invalidated, 1);
        }
    }

    /// <summary>
    /// Downstream route registration, called once from <c>RelayComposition.Build</c> alongside
    /// <c>ToolsRelay.Register</c>: records this module's routes in the shared catalog and answers
    /// <c>logging/setLevel</c>. A request is forwarded to Python only when Python's already-bootstrapped
    /// upstream capabilities (guaranteed resolved by the time any post-initialize request reaches this
    /// handler, per the FD-000 bootstrap-before-next ordering) advertise logging; otherwise this rejects
    /// with the exact "Method not found" error direct Python itself returns, so a client that never
    /// advertised or exercised logging observes identical, safe capability-absent behavior.
    /// </summary>
    public static void Register(IMcpServerBuilder builder, RelayRouteCatalog catalog, RelaySession session)
    {
        catalog.Add(new RelayRoute(RequestMethods.LoggingSetLevel, RelayDirection.DownstreamToUpstream, RelayRouteKind.Request));
        catalog.Add(new RelayRoute(NotificationMethods.ProgressNotification, RelayDirection.UpstreamToDownstream, RelayRouteKind.Notification));
        catalog.Add(new RelayRoute(NotificationMethods.LoggingMessageNotification, RelayDirection.UpstreamToDownstream, RelayRouteKind.Notification));

        builder.WithSetLoggingLevelHandler(async (context, cancellationToken) =>
        {
            var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
            if (upstream.ServerCapabilities?.Logging is null)
            {
                throw new McpProtocolException("Method not found", McpErrorCode.MethodNotFound);
            }

            // ForwardApplicationRequestAsync unwraps the SDK remote prefix exactly once.
            // Do not strip again: a legitimate remote message that itself begins with
            // "Request failed (remote): " must survive as remote content.
            var response = await session
                .ForwardApplicationRequestAsync(upstream, context.JsonRpcRequest, cancellationToken)
                .ConfigureAwait(false);
            return response.Result.Deserialize<EmptyResult>(McpJsonUtilities.DefaultOptions)!;
        });
    }

    /// <summary>
    /// Production capability-aware progress-token tracking plus logging capability projection:
    /// same <see cref="McpServerFilters"/> call-site shape as other composition filters (plus
    /// <paramref name="session"/>), installed from the same <c>AddMcpServer(options =&gt; ...)</c>
    /// block. Leaves the SDK-forced <c>logging</c> initialize-capability key in place only when
    /// Python actually advertises it (by the time this outgoing filter runs, the downstream
    /// <c>initialize</c> response is being sent from strictly inside the bootstrap filter's own
    /// <c>next()</c> call, which only happens after its upstream handshake completed - see
    /// <c>RelaySession.CreateBootstrapFilter</c>), stripping it otherwise.
    /// </summary>
    public static void ConfigureFilters(
        McpServerFilters filters,
        RelaySession session,
        NotificationState notificationState)
    {
        filters.Message.IncomingFilters.Add(next => async (context, cancellationToken) =>
        {
            if (context.JsonRpcMessage is JsonRpcNotification { Method: NotificationMethods.CancelledNotification } cancellation)
            {
                notificationState.Cancel(cancellation);
                session.ObserveDownstreamCancellation(cancellation);
                await next(context, cancellationToken).ConfigureAwait(false);
                return;
            }

            if (context.JsonRpcMessage is not JsonRpcRequest request)
            {
                await next(context, cancellationToken).ConfigureAwait(false);
                return;
            }

            session.CheckAddDownstreamRequestId(request.Id);
            var progressToken = notificationState.Begin(request);
            var requestId = request.Id;
            using var cancellationRegistration = progressToken is null
                ? default
                : cancellationToken.Register(() => notificationState.End(requestId));
            try
            {
                await next(context, cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                if (progressToken is not null)
                {
                    notificationState.End(requestId);
                }

                session.CompleteDownstreamRequestHandling(requestId);
            }
        });

        filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
        {
            session.ThrowIfForwardingFailed();
            if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                && result.TryGetPropertyValue("capabilities", out var capabilitiesNode)
                && capabilitiesNode is JsonObject capabilities)
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                if (upstream.ServerCapabilities?.Logging is null)
                {
                    capabilities.Remove("logging");
                }
            }

            var hasForwardLeg = session.TryGetForwardLegForDownstreamTerminal(
                context.JsonRpcMessage,
                out var forwardLeg);
            try
            {
                await next(context, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                session.FailForwarding(ex);
                throw;
            }

            if (hasForwardLeg)
            {
                session.CompleteForwardLegSend(forwardLeg!);
            }
        });
    }

    internal const int MaxPendingMessages = 64;

    private static readonly TimeSpan NotificationSendTimeout = TimeSpan.FromSeconds(5);

    /// <summary>
    /// Wraps the real upstream transport with one bounded wire-order delivery queue. The sole
    /// upstream reader classifies progress, logging, contiguous resource-update segments, exact
    /// correlated terminals, and ordinary traffic without awaiting downstream I/O. One drain sends
    /// owned notifications with a five-second bound and releases ordinary messages to the SDK in
    /// source order.
    /// <para>
    /// A mapped upstream response/error is released only after all wire-prior resource work; the
    /// drain then waits for the exact downstream response/error to finish its transport send before
    /// admitting any wire-later item. Cancellation is the only no-response completion. Saturation,
    /// send failure, timeout, disconnect, and terminal shutdown fail closed and settle retained legs.
    /// Same-URI resource updates coalesce only inside the current contiguous resource segment; every
    /// non-resource message closes that segment.
    /// </para>
    /// </summary>
    public static IClientTransport WrapUpstreamTransport(
        IClientTransport inner,
        RelaySession session,
        NotificationState notificationState) =>
        new OrderPreservingUpstreamTransport(inner, session, notificationState);

    private sealed class OrderPreservingUpstreamTransport(
        IClientTransport inner,
        RelaySession session,
        NotificationState notificationState) : IClientTransport
    {
        public string Name => inner.Name;

        public async Task<ITransport> ConnectAsync(CancellationToken cancellationToken = default)
        {
            var innerTransport = await inner.ConnectAsync(cancellationToken).ConfigureAwait(false);
            return new OrderPreservingTransport(innerTransport, session, notificationState);
        }
    }

    private sealed class OrderPreservingTransport : ITransport
    {
        private readonly ITransport _inner;
        private readonly RelaySession _session;
        private readonly NotificationState _notificationState;
        private readonly Channel<JsonRpcMessage> _passthrough =
            Channel.CreateUnbounded<JsonRpcMessage>(new UnboundedChannelOptions
            {
                SingleReader = true,
                SingleWriter = true,
            });
        private readonly Channel<DeliveryItem> _delivery =
            Channel.CreateBounded<DeliveryItem>(new BoundedChannelOptions(MaxPendingMessages)
            {
                SingleReader = true,
                SingleWriter = true,
                FullMode = BoundedChannelFullMode.Wait,
            });
        private readonly Dictionary<string, ResourceUpdateBatch> _openResourceBatches =
            new(StringComparer.Ordinal);
        private readonly CancellationTokenSource _pipelineCts;
        private readonly Task _pumpTask;
        private readonly Task _drainTask;
        private Exception? _failure;
        private int _disposed;

        public OrderPreservingTransport(
            ITransport inner,
            RelaySession session,
            NotificationState notificationState)
        {
            _inner = inner;
            _session = session;
            _notificationState = notificationState;
            _pipelineCts = CancellationTokenSource.CreateLinkedTokenSource(
                session.SessionEndingToken);
            _drainTask = DrainAsync();
            _pumpTask = PumpAsync();
        }

        public string? SessionId => _inner.SessionId;

        public ChannelReader<JsonRpcMessage> MessageReader => _passthrough.Reader;

        public async Task SendMessageAsync(
            JsonRpcMessage message,
            CancellationToken cancellationToken = default)
        {
            try
            {
                if (message is JsonRpcRequest request)
                {
                    _notificationState.TrackUpstreamRequest(request);
                    _session.BindForwardLeg(request);
                }

                await _inner.SendMessageAsync(message, cancellationToken).ConfigureAwait(false);
            }
            catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
            {
                throw;
            }
            catch (Exception ex)
            {
                Fail(ex);
                throw;
            }
        }

        private async Task PumpAsync()
        {
            try
            {
                await foreach (var message in _inner.MessageReader
                    .ReadAllAsync(_pipelineCts.Token)
                    .ConfigureAwait(false))
                {
                    _notificationState.ObserveUpstreamMessage(message);

                    if (message is JsonRpcNotification
                        {
                            Method: NotificationMethods.ResourceUpdatedNotification,
                        } resourceUpdate)
                    {
                        EnqueueResourceUpdate(resourceUpdate);
                        continue;
                    }

                    CloseResourceSegment();
                    if (message is JsonRpcNotification
                        {
                            Method: NotificationMethods.ProgressNotification
                                or NotificationMethods.LoggingMessageNotification,
                        } notification)
                    {
                        if (!_notificationState.TryAuthorize(notification, out var authorization)
                            || !await ShouldForwardNotificationAsync(notification).ConfigureAwait(false))
                        {
                            continue;
                        }

                        Enqueue(new DeliveryItem(
                            notification,
                            DeliveryKind.OwnedNotification,
                            authorization));
                        continue;
                    }

                    _session.TryTakeForwardLegForUpstreamTerminal(message, out var forwardLeg);
                    Enqueue(new DeliveryItem(
                        message,
                        DeliveryKind.Ordinary,
                        ForwardLeg: forwardLeg));
                }

                CloseResourceSegment();
                _session.FailForwarding(
                    new IOException("The upstream transport ended before all forwarded requests completed."));
                _delivery.Writer.TryComplete();
            }
            catch (OperationCanceledException) when (_pipelineCts.IsCancellationRequested)
            {
                CloseResourceSegment();
                _delivery.Writer.TryComplete(_failure);
            }
            catch (Exception ex)
            {
                CloseResourceSegment();
                Fail(ex);
            }
        }

        private async Task<bool> ShouldForwardNotificationAsync(
            JsonRpcNotification notification)
        {

            if (notification.Method == NotificationMethods.LoggingMessageNotification
                && (!_session.DownstreamReady.IsCompletedSuccessfully
                    || (await _session.UpstreamAsync(_pipelineCts.Token)
                        .ConfigureAwait(false)).ServerCapabilities?.Logging is null))
            {
                return false;
            }

            return _session.Downstream is not null;
        }

        private void EnqueueResourceUpdate(JsonRpcNotification notification)
        {
            var uri = TryGetResourceUri(notification)
                ?? throw new InvalidOperationException(
                    "Resource update rejected: params.uri must be a non-empty string.");
            if (_openResourceBatches.TryGetValue(uri, out var current)
                && current.TryReplace(notification))
            {
                return;
            }

            if (!_openResourceBatches.ContainsKey(uri)
                && _openResourceBatches.Count >= MaxPendingMessages)
            {
                throw new InvalidOperationException(
                    $"Resource update segment exceeded its {MaxPendingMessages}-URI bound.");
            }

            var batch = new ResourceUpdateBatch(notification);
            _openResourceBatches[uri] = batch;
            Enqueue(new DeliveryItem(
                Message: null,
                DeliveryKind.ResourceUpdate,
                ResourceBatch: batch));
        }

        private void CloseResourceSegment()
        {
            foreach (var batch in _openResourceBatches.Values)
            {
                batch.Close();
            }

            _openResourceBatches.Clear();
        }

        private static string? TryGetResourceUri(JsonRpcNotification notification)
        {
            try
            {
                if (notification.Params is not JsonObject parameters
                    || !parameters.TryGetPropertyValue("uri", out var uriNode)
                    || uriNode is null
                    || uriNode.GetValueKind() != JsonValueKind.String)
                {
                    return null;
                }

                var uri = uriNode.GetValue<string>();
                return string.IsNullOrWhiteSpace(uri) ? null : uri;
            }
            catch (Exception)
            {
                return null;
            }
        }

        private void Enqueue(DeliveryItem item)
        {
            if (_delivery.Writer.TryWrite(item))
            {
                return;
            }

            if (_pipelineCts.IsCancellationRequested)
            {
                throw new OperationCanceledException(_pipelineCts.Token);
            }

            var failure = new InvalidOperationException(
                $"Progress/log delivery queue exceeded its {MaxPendingMessages}-message bound.");
            Fail(failure);
            throw failure;
        }

        private async Task DrainAsync()
        {
            Exception? failure = null;
            try
            {
                await foreach (var item in _delivery.Reader
                    .ReadAllAsync(_pipelineCts.Token)
                    .ConfigureAwait(false))
                {
                    switch (item.Kind)
                    {
                        case DeliveryKind.OwnedNotification:
                            if (!_notificationState.IsAuthorized(item.Authorization))
                            {
                                continue;
                            }

                            await ForwardNotificationAsync((JsonRpcNotification)item.Message!)
                                .ConfigureAwait(false);
                            break;

                        case DeliveryKind.ResourceUpdate:
                            await _session.DownstreamReady
                                .WaitAsync(_pipelineCts.Token).ConfigureAwait(false);
                            await ForwardNotificationAsync(item.ResourceBatch!.Freeze())
                                .ConfigureAwait(false);
                            break;

                        default:
                            await _passthrough.Writer
                                .WriteAsync(item.Message!, _pipelineCts.Token)
                                .ConfigureAwait(false);
                            if (item.ForwardLeg is not null)
                            {
                                await item.ForwardLeg.Publication.Task
                                    .WaitAsync(_pipelineCts.Token).ConfigureAwait(false);
                            }

                            break;
                    }
                }
            }
            catch (OperationCanceledException) when (_pipelineCts.IsCancellationRequested)
            {
                failure = _failure;
            }
            catch (Exception ex)
            {
                failure = ex;
                Fail(ex);
            }
            finally
            {
                _passthrough.Writer.TryComplete(failure ?? _failure);
            }
        }

        private async Task ForwardNotificationAsync(JsonRpcNotification notification)
        {
            var downstream = _session.Downstream
                ?? throw new InvalidOperationException(
                    "Downstream session disappeared while a notification was queued.");
            using var sendCts = CancellationTokenSource.CreateLinkedTokenSource(
                _pipelineCts.Token);
            var sendTask = RelaySession.ForwardNotificationAsync(
                downstream,
                notification,
                sendCts.Token);

            try
            {
                await sendTask.WaitAsync(
                    NotificationSendTimeout,
                    _pipelineCts.Token).ConfigureAwait(false);
            }
            catch (TimeoutException ex)
            {
                sendCts.Cancel();
                ObserveLateFailure(sendTask);
                throw new TimeoutException(
                    $"Downstream {notification.Method} send exceeded "
                    + $"{NotificationSendTimeout.TotalSeconds:g}-second bound.",
                    ex);
            }
            catch (OperationCanceledException ex)
                when (!_pipelineCts.IsCancellationRequested)
            {
                ObserveLateFailure(sendTask);
                throw new TimeoutException(
                    $"Downstream {notification.Method} send exceeded "
                    + $"{NotificationSendTimeout.TotalSeconds:g}-second bound.",
                    ex);
            }
        }

        private static void ObserveLateFailure(Task task) =>
            _ = task.ContinueWith(
                static completed => _ = completed.Exception,
                CancellationToken.None,
                TaskContinuationOptions.OnlyOnFaulted
                    | TaskContinuationOptions.ExecuteSynchronously,
                TaskScheduler.Default);

        private void Fail(Exception failure)
        {
            var recordedFailure = Interlocked.CompareExchange(
                ref _failure,
                failure,
                null) ?? failure;
            _session.FailForwarding(recordedFailure);
            _delivery.Writer.TryComplete(recordedFailure);
            _passthrough.Writer.TryComplete(recordedFailure);
            try
            {
                _pipelineCts.Cancel();
            }
            catch (ObjectDisposedException)
            {
                // Disposal already completed the same terminal transition.
            }
        }

        public async ValueTask DisposeAsync()
        {
            if (Interlocked.Exchange(ref _disposed, 1) != 0)
            {
                return;
            }

            _pipelineCts.Cancel();
            _delivery.Writer.TryComplete();
            try
            {
                await _inner.DisposeAsync().ConfigureAwait(false);
            }
            finally
            {
                try
                {
                    await Task.WhenAll(_pumpTask, _drainTask).ConfigureAwait(false);
                }
                catch
                {
                    // Pipeline failures already complete MessageReader with their exact cause.
                }

                _passthrough.Writer.TryComplete(_failure);
                _pipelineCts.Dispose();
            }
        }

        private enum DeliveryKind
        {
            Ordinary,
            OwnedNotification,
            ResourceUpdate,
        }

        private sealed class ResourceUpdateBatch(JsonRpcNotification initial)
        {
            private readonly object _gate = new();
            private JsonRpcNotification _latest = initial;
            private bool _open = true;

            public bool TryReplace(JsonRpcNotification notification)
            {
                lock (_gate)
                {
                    if (!_open)
                    {
                        return false;
                    }

                    _latest = notification;
                    return true;
                }
            }

            public void Close()
            {
                lock (_gate)
                {
                    _open = false;
                }
            }

            public JsonRpcNotification Freeze()
            {
                lock (_gate)
                {
                    _open = false;
                    return _latest;
                }
            }
        }

        private readonly record struct DeliveryItem(
            JsonRpcMessage? Message,
            DeliveryKind Kind,
            NotificationState.DeliveryAuthorization Authorization = default,
            ResourceUpdateBatch? ResourceBatch = null,
            RelaySession.ForwardLeg? ForwardLeg = null);
    }
}
