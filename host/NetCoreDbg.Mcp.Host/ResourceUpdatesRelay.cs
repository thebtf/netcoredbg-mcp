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
    /// contiguous stamps so callback scheduling cannot invert upstream receive order.
    /// </summary>
    internal sealed class OrderedUpstream
    {
        private readonly object _gate = new();
        private readonly SortedDictionary<long, PendingForward> _pending = [];
        private readonly ConditionalWeakTable<JsonRpcNotification, ReceiveStamp> _receiveStamps = new();
        private Task _drainTask = Task.CompletedTask;
        private long _nextReceivedSequence;
        private long _nextForwardSequence = 1;
        private bool _forwarderActive;
        private bool _terminal;

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
                            throw new InvalidOperationException(
                                "Resource update reached its callback without an upstream wire-order stamp.");
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
        /// Adds a sidecar receive stamp only to resource-updated notifications. All other
        /// messages pass through the transport wrapper as the identical object.
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

        private ValueTask ForwardInOrderAsync(
            long sequence,
            RelaySession session,
            CancellationToken sessionEndingToken,
            JsonRpcNotification notification,
            CancellationToken callbackToken)
        {
            var completion = new TaskCompletionSource(
                TaskCreationOptions.RunContinuationsAsynchronously);
            TaskCompletionSource? startDrain = null;

            lock (_gate)
            {
                if (_terminal)
                {
                    return ValueTask.CompletedTask;
                }

                if (sequence < _nextForwardSequence || _pending.ContainsKey(sequence))
                {
                    throw new InvalidOperationException(
                        $"Duplicate or stale resource update receive sequence {sequence}.");
                }

                _pending.Add(
                    sequence,
                    new PendingForward(notification, callbackToken, completion));
                if (!_forwarderActive && _pending.ContainsKey(_nextForwardSequence))
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
            return new ValueTask(completion.Task);
        }

        private async Task DrainForwardsAsync(
            RelaySession session,
            CancellationToken sessionEndingToken,
            Task startSignal)
        {
            await startSignal.ConfigureAwait(false);
            while (true)
            {
                PendingForward pending;
                lock (_gate)
                {
                    if (_terminal
                        || !_pending.Remove(_nextForwardSequence, out pending!))
                    {
                        _forwarderActive = false;
                        return;
                    }

                    _nextForwardSequence++;
                }

                try
                {
                    await ForwardUpdateAsync(
                        session,
                        sessionEndingToken,
                        pending.Notification,
                        pending.CallbackToken).ConfigureAwait(false);
                    pending.Completion.TrySetResult();
                }
                catch (Exception error)
                {
                    pending.Completion.TrySetException(error);
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
            PendingForward[] abandoned;
            lock (_gate)
            {
                _terminal = true;
                abandoned = [.. _pending.Values];
                _pending.Clear();
                _forwarderActive = false;
            }

            foreach (var pending in abandoned)
            {
                pending.Completion.TrySetResult();
            }
        }

        private sealed record ReceiveStamp(long Sequence);

        private sealed record PendingForward(
            JsonRpcNotification Notification,
            CancellationToken CallbackToken,
            TaskCompletionSource Completion);
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
