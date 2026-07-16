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
    /// Stamps resource updates while the SDK consumes its ordered transport reader, before
    /// McpSessionHandler force-yields concurrent callbacks. A weak object-identity sidecar
    /// leaves the notification object graph untouched. Callback forwarding then drains
    /// by wire-order of each URI's first pending slot so callback scheduling cannot invert
    /// upstream receive order across distinct URIs.
    /// Pending work is coalesced per URI: only the latest payload/token is retained, and
    /// coalesced duplicate callbacks complete immediately with no per-message waiter.
    /// </summary>
    internal sealed class OrderedUpstream
    {
        private readonly object _gate = new();
        /// <summary>OrderSequence (first accepted sequence for a pending URI) → entry.</summary>
        private readonly SortedDictionary<long, PendingUri> _pendingByOrder = [];
        /// <summary>URI → pending entry (at most one per distinct URI).</summary>
        private readonly Dictionary<string, PendingUri> _pendingByUri =
            new(StringComparer.Ordinal);
        /// <summary>
        /// Interval-merged set of sequences whose callbacks have finished registration
        /// (accepted, coalesced, or bound-rejected). Enables ordered drain without an
        /// O(messages) skip list: same-URI floods collapse to one interval.
        /// </summary>
        private readonly AccountedSequenceSet _accounted = new();
        private readonly ConditionalWeakTable<JsonRpcNotification, ReceiveStamp> _receiveStamps = new();
        private readonly System.Collections.Concurrent.ConcurrentQueue<string> _forwardedMarkers = new();
        private Task _drainTask = Task.CompletedTask;
        private long _nextReceivedSequence;
        private long _nextForwardSequence = 1;
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
        /// Retained backpressure bookkeeping objects: pending URI entries, order index
        /// entries, and accounted sequence intervals. Must stay O(unique pending URIs /
        /// gap intervals), never O(notification count).
        /// </summary>
        internal int RetainedBackpressureObjectCount
        {
            get
            {
                lock (_gate)
                {
                    return _pendingByUri.Count
                        + _pendingByOrder.Count
                        + _accounted.IntervalCount;
                }
            }
        }

        /// <summary>
        /// Number of interval segments used to track accounted (non-pending) sequences.
        /// Same-URI coalesced floods must merge into a small interval count.
        /// </summary>
        internal int AccountedIntervalCount
        {
            get
            {
                lock (_gate)
                {
                    return _accounted.IntervalCount;
                }
            }
        }

        /// <summary>How many pending entries the drain has taken for forward (test surface).</summary>
        internal int ForwardAttempts => Volatile.Read(ref _forwardAttempts);

        /// <summary>Markers observed on drain-selected payloads (test surface).</summary>
        internal System.Collections.Concurrent.ConcurrentQueue<string> ForwardedMarkers => _forwardedMarkers;

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
                        if (!TryGetReceiveSequence(notification, out var sequence))
                        {
                            // Missing/malformed URI is rejected at stamp time without a
                            // sequence, so it never enters pending or creates a drain hole.
                            throw new InvalidOperationException(
                                "Resource update rejected: missing or malformed params.uri, "
                                + "or reached its callback without an upstream wire-order stamp.");
                        }

                        return ForwardInOrderAsync(
                            sequence,
                            session,
                            sessionEndingToken,
                            notification,
                            cancellationToken);
                    }),
            ];
        }

        /// <summary>
        /// Adds a sidecar receive stamp only to resource-updated notifications that carry a
        /// valid <c>params.uri</c>. Malformed updates are not stamped and never enter the
        /// pending pipeline. All other messages pass through as the identical object.
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

            // Validate/extract URI before accepting into the resource-update pipeline.
            if (TryGetResourceUri(notification) is null)
            {
                return;
            }

            var sequence = Interlocked.Increment(ref _nextReceivedSequence);
            _receiveStamps.Add(notification, new ReceiveStamp(sequence));
        }

        private bool TryGetReceiveSequence(
            JsonRpcNotification notification,
            out long sequence)
        {
            if (_receiveStamps.TryGetValue(notification, out var stamp))
            {
                sequence = stamp.Sequence;
                return true;
            }

            sequence = 0;
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

        private ValueTask ForwardInOrderAsync(
            long sequence,
            RelaySession session,
            CancellationToken sessionEndingToken,
            JsonRpcNotification notification,
            CancellationToken callbackToken)
        {
            TaskCompletionSource? startDrain = null;

            lock (_gate)
            {
                if (_terminal)
                {
                    return ValueTask.CompletedTask;
                }

                var uri = TryGetResourceUri(notification);
                if (uri is null)
                {
                    // Should not reach here for stamped messages; fail closed without state.
                    throw new InvalidOperationException(
                        "Resource update rejected: missing or malformed params.uri.");
                }

                if (_pendingByUri.TryGetValue(uri, out var existing))
                {
                    // Sequence-aware coalesce: only a newer sequence may replace payload/token.
                    // Older/late callbacks (including cancelled ones) cannot overwrite newer state.
                    if (sequence > existing.ContentSequence)
                    {
                        existing.Notification = notification;
                        existing.CallbackToken = callbackToken;
                        existing.ContentSequence = sequence;
                    }

                    _accounted.Add(sequence);
                    if (!_forwarderActive && CanDrainNextLocked())
                    {
                        _forwarderActive = true;
                        startDrain = new TaskCompletionSource(
                            TaskCreationOptions.RunContinuationsAsynchronously);
                        _drainTask = DrainForwardsAsync(
                            session,
                            sessionEndingToken,
                            startDrain.Task);
                    }

                    startDrain?.TrySetResult();
                    // Coalesced duplicates complete immediately: no per-message waiter.
                    return ValueTask.CompletedTask;
                }

                // Bound new distinct URIs, but always admit the current head sequence so a
                // missing predecessor can unblock drain even when the queue is full. Without
                // this, a gap at _nextForwardSequence would strand every later update.
                var admitsHeadGap = sequence == _nextForwardSequence;
                if (_pendingByUri.Count >= MaxPendingUris && !admitsHeadGap)
                {
                    // Account the sequence so bound rejection cannot create a drain hole
                    // that strands later valid updates.
                    _accounted.Add(sequence);
                    if (!_forwarderActive && CanDrainNextLocked())
                    {
                        _forwarderActive = true;
                        startDrain = new TaskCompletionSource(
                            TaskCreationOptions.RunContinuationsAsynchronously);
                        _drainTask = DrainForwardsAsync(
                            session,
                            sessionEndingToken,
                            startDrain.Task);
                    }

                    startDrain?.TrySetResult();
                    throw new InvalidOperationException(
                        $"Resource update pending-URI bound exceeded ({MaxPendingUris}). "
                        + "Slow subscribers must not retain unbounded host state.");
                }

                var pending = new PendingUri(uri, sequence, notification, callbackToken);
                _pendingByUri.Add(uri, pending);
                _pendingByOrder.Add(sequence, pending);
                _accounted.Add(sequence);

                if (!_forwarderActive && CanDrainNextLocked())
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
            // Accepted work is retained only as the pending URI slot; the callback itself
            // does not wait. Drain forwards asynchronously in wire order.
            return ValueTask.CompletedTask;
        }

        /// <summary>
        /// True when the next forward sequence is either a pending URI order slot or an
        /// accounted coalesce/reject hole that can be skipped.
        /// </summary>
        private bool CanDrainNextLocked() =>
            _pendingByOrder.ContainsKey(_nextForwardSequence)
            || _accounted.Contains(_nextForwardSequence);

        private async Task DrainForwardsAsync(
            RelaySession session,
            CancellationToken sessionEndingToken,
            Task startSignal)
        {
            await startSignal.ConfigureAwait(false);
            while (true)
            {
                PendingUri? pending = null;
                lock (_gate)
                {
                    if (_terminal)
                    {
                        _forwarderActive = false;
                        return;
                    }

                    // Skip sequences that were coalesced or bound-rejected (accounted but
                    // not owning a pending order slot).
                    while (!_pendingByOrder.ContainsKey(_nextForwardSequence)
                        && _accounted.Contains(_nextForwardSequence))
                    {
                        _nextForwardSequence++;
                    }

                    _accounted.PruneBefore(_nextForwardSequence);

                    if (!_pendingByOrder.Remove(_nextForwardSequence, out pending))
                    {
                        _forwarderActive = false;
                        return;
                    }

                    _pendingByUri.Remove(pending.Uri);
                    _nextForwardSequence++;
                    _accounted.PruneBefore(_nextForwardSequence);
                    Interlocked.Increment(ref _forwardAttempts);
                    var marker = pending.Notification.Params?["_meta"]?["marker"]?.GetValue<string>();
                    if (marker is not null)
                    {
                        _forwardedMarkers.Enqueue(marker);
                    }
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
                _accounted.Clear();
                _forwarderActive = false;
            }
        }

        private sealed record ReceiveStamp(long Sequence);

        /// <summary>
        /// One retained pending URI: order is fixed at first acceptance; payload/token track
        /// the newest sequence only.
        /// </summary>
        private sealed class PendingUri(
            string uri,
            long orderSequence,
            JsonRpcNotification notification,
            CancellationToken callbackToken)
        {
            public string Uri { get; } = uri;
            public long OrderSequence { get; } = orderSequence;
            public long ContentSequence { get; set; } = orderSequence;
            public JsonRpcNotification Notification { get; set; } = notification;
            public CancellationToken CallbackToken { get; set; } = callbackToken;
        }

        /// <summary>
        /// Sorted non-overlapping inclusive intervals of accounted sequences. Merges on
        /// insert so a same-URI flood of N messages becomes one interval, not N entries.
        /// </summary>
        private sealed class AccountedSequenceSet
        {
            private readonly List<(long Start, long End)> _intervals = [];

            public int IntervalCount => _intervals.Count;

            public void Clear() => _intervals.Clear();

            public bool Contains(long sequence)
            {
                var index = FindIntervalIndex(sequence);
                if (index < 0)
                {
                    return false;
                }

                var (start, end) = _intervals[index];
                return sequence >= start && sequence <= end;
            }

            public void Add(long sequence)
            {
                if (_intervals.Count == 0)
                {
                    _intervals.Add((sequence, sequence));
                    return;
                }

                var index = FindInsertionIndex(sequence);
                // Merge with previous if adjacent/overlapping.
                if (index > 0)
                {
                    var prev = _intervals[index - 1];
                    if (sequence <= prev.End + 1 && sequence >= prev.Start)
                    {
                        // Already covered or extend previous end.
                        if (sequence > prev.End)
                        {
                            prev.End = sequence;
                            _intervals[index - 1] = prev;
                            MergeForwardFrom(index - 1);
                        }

                        return;
                    }
                }

                if (index < _intervals.Count)
                {
                    var next = _intervals[index];
                    if (sequence >= next.Start - 1 && sequence <= next.End)
                    {
                        // Already covered or extend next start.
                        if (sequence < next.Start)
                        {
                            next.Start = sequence;
                            _intervals[index] = next;
                            if (index > 0)
                            {
                                MergeForwardFrom(index - 1);
                            }
                        }

                        return;
                    }
                }

                _intervals.Insert(index, (sequence, sequence));
                if (index > 0)
                {
                    MergeForwardFrom(index - 1);
                }
                else
                {
                    MergeForwardFrom(0);
                }
            }

            public void PruneBefore(long before)
            {
                if (before <= long.MinValue + 1 || _intervals.Count == 0)
                {
                    return;
                }

                var keepFrom = 0;
                while (keepFrom < _intervals.Count && _intervals[keepFrom].End < before)
                {
                    keepFrom++;
                }

                if (keepFrom > 0)
                {
                    _intervals.RemoveRange(0, keepFrom);
                }

                if (_intervals.Count > 0 && _intervals[0].Start < before)
                {
                    var first = _intervals[0];
                    first.Start = before;
                    if (first.Start > first.End)
                    {
                        _intervals.RemoveAt(0);
                    }
                    else
                    {
                        _intervals[0] = first;
                    }
                }
            }

            private void MergeForwardFrom(int index)
            {
                while (index + 1 < _intervals.Count)
                {
                    var current = _intervals[index];
                    var next = _intervals[index + 1];
                    if (next.Start > current.End + 1)
                    {
                        break;
                    }

                    current.End = Math.Max(current.End, next.End);
                    current.Start = Math.Min(current.Start, next.Start);
                    _intervals[index] = current;
                    _intervals.RemoveAt(index + 1);
                }
            }

            private int FindIntervalIndex(long sequence)
            {
                var lo = 0;
                var hi = _intervals.Count - 1;
                while (lo <= hi)
                {
                    var mid = lo + ((hi - lo) / 2);
                    var (start, end) = _intervals[mid];
                    if (sequence < start)
                    {
                        hi = mid - 1;
                    }
                    else if (sequence > end)
                    {
                        lo = mid + 1;
                    }
                    else
                    {
                        return mid;
                    }
                }

                return -1;
            }

            private int FindInsertionIndex(long sequence)
            {
                var lo = 0;
                var hi = _intervals.Count;
                while (lo < hi)
                {
                    var mid = lo + ((hi - lo) / 2);
                    if (_intervals[mid].Start < sequence)
                    {
                        lo = mid + 1;
                    }
                    else
                    {
                        hi = mid;
                    }
                }

                return lo;
            }
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
