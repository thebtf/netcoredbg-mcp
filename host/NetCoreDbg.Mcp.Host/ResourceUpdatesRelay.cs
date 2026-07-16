using System.Runtime.CompilerServices;
using System.Text.Json;
using System.Threading.Channels;
using Microsoft.Extensions.DependencyInjection;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// FD-006 relay for resource subscription requests and Python-owned update notifications.
/// Python remains the sole subscription authority; this module stores no URI set.
/// </summary>
internal static class ResourceUpdatesRelay
{
    /// <summary>
    /// Fixed bound on distinct resource URIs retained while a slow downstream subscriber
    /// blocks ordered drain. Notifications coalesce per URI (latest payload only; coalesced
    /// duplicates complete immediately), so retained work scales with unique pending URIs
    /// rather than notification count. Exceeding this bound fails closed instead of growing
    /// memory.
    /// </summary>
    public const int MaxPendingUris = 64;

    public static OrderedUpstream CreateOrderedUpstream() => new();

    public static void Register(IMcpServerBuilder builder, RelayRouteCatalog catalog, RelaySession session)
    {
        catalog.Add(new RelayRoute(
            RequestMethods.ResourcesSubscribe,
            RelayDirection.DownstreamToUpstream,
            RelayRouteKind.Request));
        catalog.Add(new RelayRoute(
            RequestMethods.ResourcesUnsubscribe,
            RelayDirection.DownstreamToUpstream,
            RelayRouteKind.Request));

        builder
            .WithSubscribeToResourcesHandler(async (context, cancellationToken) =>
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                var response = await RelaySession
                    .ForwardRequestAsync(upstream, context.JsonRpcRequest, cancellationToken)
                    .ConfigureAwait(false);
                return response.Result.Deserialize<EmptyResult>(McpJsonUtilities.DefaultOptions)!;
            })
            .WithUnsubscribeFromResourcesHandler(async (context, cancellationToken) =>
            {
                var upstream = await session.UpstreamAsync(cancellationToken).ConfigureAwait(false);
                var response = await RelaySession
                    .ForwardRequestAsync(upstream, context.JsonRpcRequest, cancellationToken)
                    .ConfigureAwait(false);
                return response.Result.Deserialize<EmptyResult>(McpJsonUtilities.DefaultOptions)!;
            });
    }

    /// <summary>
    /// Admits and orders resource updates while the SDK consumes its ordered transport reader,
    /// before McpSessionHandler force-yields concurrent callbacks. A weak object-identity
    /// sidecar leaves the notification object graph untouched.
    /// <para>
    /// Wire order and coalescing are owned at stamp time: each accepted distinct URI gets one
    /// pending slot keyed by its first wire sequence; later same-URI messages replace only the
    /// latest payload/sequence and reset readiness. Callbacks only mark the current content
    /// ready (or complete as stale) and may start drain. Drain walks the smallest pending order
    /// key and stops on an unready head — no contiguous sequence cursor and no per-message
    /// accounted/waiter state.
    /// </para>
    /// </summary>
    internal sealed class OrderedUpstream
    {
        private readonly object _gate = new();
        /// <summary>OrderSequence (first accepted sequence for a pending URI) → entry.</summary>
        private readonly SortedDictionary<long, PendingUri> _pendingByOrder = [];
        /// <summary>URI → pending entry (at most one per distinct URI).</summary>
        private readonly Dictionary<string, PendingUri> _pendingByUri =
            new(StringComparer.Ordinal);
        private readonly ConditionalWeakTable<JsonRpcNotification, ReceiveStamp> _receiveStamps = new();
        private Task _drainTask = Task.CompletedTask;
        private long _nextReceivedSequence;
        private int _forwardAttempts;
        private bool _forwarderActive;
        private bool _terminal;

        /// <summary>
        /// Number of distinct URIs currently waiting to be forwarded (test/diagnostic surface).
        /// </summary>
        internal int PendingUriCount
        {
            get
            {
                lock (_gate)
                {
                    return _pendingByUri.Count;
                }
            }
        }

        /// <summary>
        /// Total pending forward slots. Equal to <see cref="PendingUriCount"/> because
        /// duplicates coalesce into one slot per URI.
        /// </summary>
        internal int PendingSlotCount
        {
            get
            {
                lock (_gate)
                {
                    return _pendingByOrder.Count;
                }
            }
        }

        /// <summary>
        /// Retained backpressure bookkeeping objects: pending URI entries and order index
        /// entries. Must stay O(unique pending URIs), never O(notification count).
        /// </summary>
        internal int RetainedBackpressureObjectCount
        {
            get
            {
                lock (_gate)
                {
                    return _pendingByUri.Count + _pendingByOrder.Count;
                }
            }
        }

        /// <summary>How many pending entries the drain has taken for forward (test surface).</summary>
        internal int ForwardAttempts => Volatile.Read(ref _forwardAttempts);

        public IClientTransport WrapTransport(IClientTransport transport) =>
            new SequencedClientTransport(transport, StampReceivedMessage);

        public void ConfigureHandlers(McpClientHandlers handlers, RelaySession session)
        {
            var existing = (handlers.NotificationHandlers ?? []).ToArray();
            if (existing.Any(pair => pair.Key == NotificationMethods.ResourceUpdatedNotification))
            {
                throw new InvalidOperationException(
                    $"{NotificationMethods.ResourceUpdatedNotification} already has an upstream handler.");
            }

            var sessionEndingToken = session.SessionEndingToken;
            sessionEndingToken.Register(MarkTerminal);
            handlers.NotificationHandlers =
            [
                .. existing,
                new(
                    NotificationMethods.ResourceUpdatedNotification,
                    (notification, cancellationToken) =>
                    {
                        if (!TryGetReceiveStamp(notification, out var stamp))
                        {
                            // Missing/malformed URI is never stamped, so it never enters pending.
                            throw new InvalidOperationException(
                                "Resource update rejected: missing or malformed params.uri, "
                                + "or reached its callback without an upstream wire-order stamp.");
                        }

                        if (stamp.Disposition == StampDisposition.BoundRejected)
                        {
                            throw new InvalidOperationException(
                                $"Resource update pending-URI bound exceeded ({MaxPendingUris}). "
                                + "Slow subscribers must not retain unbounded host state.");
                        }

                        return MarkReadyAndMaybeDrainAsync(
                            stamp,
                            session,
                            sessionEndingToken,
                            cancellationToken);
                    }),
            ];
        }

        /// <summary>
        /// Validates URI, assigns wire sequence, and under lock records a weak stamp outcome
        /// plus at most one pending slot per accepted URI. Malformed updates stay unstamped.
        /// Bound rejection is stamped without creating pending/order state.
        /// </summary>
        internal void StampReceivedMessage(JsonRpcMessage message)
        {
            if (message is not JsonRpcNotification
                {
                    Method: NotificationMethods.ResourceUpdatedNotification,
                } notification)
            {
                return;
            }

            if (TryGetResourceUri(notification) is not { } uri)
            {
                return;
            }

            lock (_gate)
            {
                var sequence = ++_nextReceivedSequence;

                if (_terminal)
                {
                    // Keep a stamp so the late callback does not throw as malformed; no pending.
                    _receiveStamps.Add(
                        notification,
                        new ReceiveStamp(sequence, StampDisposition.Accepted, uri));
                    return;
                }

                if (_pendingByUri.TryGetValue(uri, out var existing))
                {
                    // Keep the first-wire order slot; replace only latest payload/sequence.
                    existing.Notification = notification;
                    existing.ContentSequence = sequence;
                    existing.Ready = false;
                    existing.CallbackToken = default;
                    _receiveStamps.Add(
                        notification,
                        new ReceiveStamp(sequence, StampDisposition.Accepted, uri));
                    return;
                }

                if (_pendingByUri.Count >= MaxPendingUris)
                {
                    // Bound-rejected stamp only — no pending/order state.
                    _receiveStamps.Add(
                        notification,
                        new ReceiveStamp(sequence, StampDisposition.BoundRejected, uri));
                    return;
                }

                var pending = new PendingUri(uri, sequence, notification);
                _pendingByUri.Add(uri, pending);
                _pendingByOrder.Add(sequence, pending);
                _receiveStamps.Add(
                    notification,
                    new ReceiveStamp(sequence, StampDisposition.Accepted, uri));
            }
        }

        private bool TryGetReceiveStamp(
            JsonRpcNotification notification,
            out ReceiveStamp stamp)
        {
            if (_receiveStamps.TryGetValue(notification, out stamp!))
            {
                return true;
            }

            stamp = null!;
            return false;
        }

        private static string? TryGetResourceUri(JsonRpcNotification notification)
        {
            try
            {
                if (notification.Params is null
                    || !notification.Params.AsObject().TryGetPropertyValue("uri", out var uriNode)
                    || uriNode is null)
                {
                    return null;
                }

                // Reject non-string JSON values (numbers, objects, arrays, null).
                if (uriNode.GetValueKind() != JsonValueKind.String)
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

        /// <summary>
        /// Consumes stamp only: marks the current content ready, records at most one
        /// cancellation token, and starts drain when the smallest order slot is ready.
        /// Stale callbacks (not matching current ContentSequence) complete without state.
        /// </summary>
        private ValueTask MarkReadyAndMaybeDrainAsync(
            ReceiveStamp stamp,
            RelaySession session,
            CancellationToken sessionEndingToken,
            CancellationToken callbackToken)
        {
            TaskCompletionSource? startDrain = null;

            lock (_gate)
            {
                if (_terminal)
                {
                    return ValueTask.CompletedTask;
                }

                if (stamp.Uri is not { } uri)
                {
                    throw new InvalidOperationException(
                        "Accepted resource update stamp is missing its validated URI.");
                }

                if (!_pendingByUri.TryGetValue(uri, out var pending))
                {
                    // Already drained or never admitted — retain nothing.
                    return ValueTask.CompletedTask;
                }

                if (stamp.Sequence != pending.ContentSequence)
                {
                    // Stale callback for a superseded payload — complete immediately.
                    return ValueTask.CompletedTask;
                }

                pending.Ready = true;
                pending.CallbackToken = callbackToken;

                if (!_forwarderActive && IsHeadReadyLocked())
                {
                    _forwarderActive = true;
                    startDrain = new TaskCompletionSource(
                        TaskCreationOptions.RunContinuationsAsynchronously);
                    _drainTask = DrainForwardsAsync(
                        session,
                        sessionEndingToken,
                        startDrain.Task);
                }
            }

            startDrain?.TrySetResult();
            return ValueTask.CompletedTask;
        }

        /// <summary>
        /// True when the smallest accepted order slot exists and is ready.
        /// </summary>
        private bool IsHeadReadyLocked() =>
            _pendingByOrder.Count > 0 && _pendingByOrder.First().Value.Ready;

        private async Task DrainForwardsAsync(
            RelaySession session,
            CancellationToken sessionEndingToken,
            Task startSignal)
        {
            await startSignal.ConfigureAwait(false);
            while (true)
            {
                PendingUri? pending = null;
                var skipCancelled = false;
                lock (_gate)
                {
                    if (_terminal)
                    {
                        _forwarderActive = false;
                        return;
                    }

                    if (_pendingByOrder.Count == 0)
                    {
                        _forwarderActive = false;
                        return;
                    }

                    // SortedDictionary enumerates keys ascending — take the smallest order slot.
                    var head = _pendingByOrder.First();
                    if (!head.Value.Ready)
                    {
                        // Unready head stops without removal.
                        _forwarderActive = false;
                        return;
                    }

                    pending = head.Value;
                    _pendingByOrder.Remove(head.Key);
                    _pendingByUri.Remove(pending.Uri);
                    Interlocked.Increment(ref _forwardAttempts);

                    if (pending.CallbackToken.IsCancellationRequested)
                    {
                        // Current callback cancelled: suppress send, continue to next head.
                        skipCancelled = true;
                    }
                }

                if (skipCancelled || pending is null)
                {
                    continue;
                }

                try
                {
                    await ForwardUpdateAsync(
                        session,
                        sessionEndingToken,
                        pending.Notification,
                        pending.CallbackToken).ConfigureAwait(false);
                }
                catch (Exception)
                {
                    // Downstream forward failures must not stall the ordered drain; the
                    // one-way notification is best-effort once accepted into the pipeline.
                }
            }
        }

        public ValueTask WaitForDrainAsync()
        {
            lock (_gate)
            {
                return new ValueTask(_drainTask);
            }
        }

        private void MarkTerminal()
        {
            lock (_gate)
            {
                _terminal = true;
                _pendingByOrder.Clear();
                _pendingByUri.Clear();
                _forwarderActive = false;
            }
        }

        private enum StampDisposition
        {
            Accepted,
            BoundRejected,
        }

        private sealed record ReceiveStamp(
            long Sequence,
            StampDisposition Disposition,
            string? Uri);

        /// <summary>
        /// One retained pending URI: order is fixed at first acceptance; payload tracks the
        /// newest content sequence; readiness and the sole current cancellation token are set
        /// only by the matching callback.
        /// </summary>
        private sealed class PendingUri(
            string uri,
            long orderSequence,
            JsonRpcNotification notification)
        {
            public string Uri { get; } = uri;
            public long OrderSequence { get; } = orderSequence;
            public long ContentSequence { get; set; } = orderSequence;
            public JsonRpcNotification Notification { get; set; } = notification;
            public CancellationToken CallbackToken { get; set; }
            public bool Ready { get; set; }
        }
    }

    private sealed class SequencedClientTransport(
        IClientTransport inner,
        Action<JsonRpcMessage> stampReceivedMessage) : IClientTransport
    {
        public string Name => inner.Name;

        public async Task<ITransport> ConnectAsync(CancellationToken cancellationToken = default) =>
            new SequencedTransport(
                await inner.ConnectAsync(cancellationToken).ConfigureAwait(false),
                stampReceivedMessage);
    }

    private sealed class SequencedTransport : ITransport
    {
        private readonly ITransport _inner;

        public SequencedTransport(
            ITransport inner,
            Action<JsonRpcMessage> stampReceivedMessage)
        {
            _inner = inner;
            MessageReader = new SequencedMessageReader(
                inner.MessageReader,
                stampReceivedMessage);
        }

        public string? SessionId => _inner.SessionId;

        public ChannelReader<JsonRpcMessage> MessageReader { get; }

        public Task SendMessageAsync(
            JsonRpcMessage message,
            CancellationToken cancellationToken = default) =>
            _inner.SendMessageAsync(message, cancellationToken);

        public ValueTask DisposeAsync() => _inner.DisposeAsync();
    }

    private sealed class SequencedMessageReader(
        ChannelReader<JsonRpcMessage> inner,
        Action<JsonRpcMessage> stampReceivedMessage) : ChannelReader<JsonRpcMessage>
    {
        public override Task Completion => inner.Completion;

        public override bool TryRead(out JsonRpcMessage item)
        {
            if (!inner.TryRead(out item!))
            {
                return false;
            }

            stampReceivedMessage(item);
            return true;
        }

        public override ValueTask<bool> WaitToReadAsync(
            CancellationToken cancellationToken = default) =>
            inner.WaitToReadAsync(cancellationToken);
    }

    private static async ValueTask ForwardUpdateAsync(
        RelaySession session,
        CancellationToken sessionEndingToken,
        JsonRpcNotification notification,
        CancellationToken callbackToken)
    {
        using var linkedCancellation = CancellationTokenSource.CreateLinkedTokenSource(
            callbackToken,
            sessionEndingToken);
        var cancellationToken = linkedCancellation.Token;
        if (cancellationToken.IsCancellationRequested)
        {
            return;
        }

        try
        {
            await session.DownstreamReady.WaitAsync(cancellationToken).ConfigureAwait(false);
            if (cancellationToken.IsCancellationRequested || session.Downstream is not { } downstream)
            {
                return;
            }

            await RelaySession
                .ForwardNotificationAsync(downstream, notification, cancellationToken)
                .ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            // Cancellation or terminal session: the one-way update is intentionally suppressed.
        }
    }
}
