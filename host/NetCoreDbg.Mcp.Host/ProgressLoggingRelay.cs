using System.Collections.Concurrent;
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
/// FD-002: progress-token context, <c>notifications/progress</c>, <c>notifications/message</c>, and
/// <c>logging/setLevel</c>. Forwards Python's upstream progress/log notifications to the real
/// downstream client, and relays downstream <c>logging/setLevel</c> to Python only when Python's
/// already-negotiated upstream capabilities actually advertise logging.
///
/// Follows the <c>ToolsRelay</c> module convention for its downstream half: <see cref="Register"/> is
/// the one call <c>RelayComposition.Build</c> adds (alongside <c>ToolsRelay.Register</c>), and
/// <see cref="ConfigureFilters"/> is the production capability-aware logging/progress filter pair
/// installed in that same <c>AddMcpServer(options =&gt; ...)</c> block. Its upstream half is
/// <see cref="WrapUpstreamTransport"/>, which <c>Program.cs</c> wraps around
/// <c>PythonBackendProcess.CreateUpstreamTransport</c> before constructing <see cref="RelaySession"/>
/// - see that method's own doc comment for why progress/logging forwarding must happen at the
/// transport layer rather than through <see cref="McpClientHandlers.NotificationHandlers"/>.
/// </summary>
internal static class ProgressLoggingRelay
{
    internal sealed class NotificationState
    {
        private readonly ConcurrentDictionary<ProgressToken, byte> _activeProgressTokens = new();
        // RequestId equality preserves JSON-RPC type: numeric 1 and string "1" are distinct keys.
        private readonly ConcurrentDictionary<RequestId, ProgressToken> _progressTokensByRequestId = new();

        public ProgressToken? Begin(JsonRpcRequest request)
        {
            if (!TryReadMetaProgressToken(request.Params, out var progressToken))
            {
                return null;
            }

            _activeProgressTokens[progressToken] = 0;
            _progressTokensByRequestId[request.Id] = progressToken;
            return progressToken;
        }

        public void End(RequestId requestId, ProgressToken? progressToken)
        {
            _progressTokensByRequestId.TryRemove(requestId, out _);
            if (progressToken is { } activeProgressToken)
            {
                _activeProgressTokens.TryRemove(activeProgressToken, out _);
            }
        }

        public void Cancel(JsonRpcNotification notification)
        {
            try
            {
                var requestId = notification.Params?
                    .Deserialize<CancelledNotificationParams>(McpJsonUtilities.DefaultOptions)?.RequestId;
                if (requestId is { } typedRequestId
                    && _progressTokensByRequestId.TryRemove(typedRequestId, out var progressToken))
                {
                    _activeProgressTokens.TryRemove(progressToken, out _);
                }
            }
            catch (JsonException)
            {
                // Invalid cancellation notifications are ignored by the MCP protocol.
            }
        }

        public bool IsActive(ProgressToken progressToken) => _activeProgressTokens.ContainsKey(progressToken);

        public bool Allows(JsonRpcNotification notification)
        {
            if (notification.Method == NotificationMethods.ProgressNotification)
            {
                return notification.Params is JsonObject progressParams
                    && TryReadProgressToken(progressParams["progressToken"], out var progressToken)
                    && _activeProgressTokens.ContainsKey(progressToken);
            }

            return !TryReadMetaProgressToken(notification.Params, out var loggingProgressToken)
                || _activeProgressTokens.ContainsKey(loggingProgressToken);
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

            // ForwardRequestAsync already unwraps the SDK remote prefix exactly once.
            // Do not strip again: a legitimate remote message that itself begins with
            // "Request failed (remote): " must survive as remote content.
            var response = await RelaySession.ForwardRequestAsync(upstream, context.JsonRpcRequest, cancellationToken)
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
                await next(context, cancellationToken).ConfigureAwait(false);
                return;
            }

            if (context.JsonRpcMessage is not JsonRpcRequest request)
            {
                await next(context, cancellationToken).ConfigureAwait(false);
                return;
            }

            var progressToken = notificationState.Begin(request);
            if (progressToken is null)
            {
                await next(context, cancellationToken).ConfigureAwait(false);
                return;
            }

            var requestId = request.Id;
            using var cancellationRegistration = cancellationToken.Register(
                () => notificationState.End(requestId, progressToken));
            try
            {
                await next(context, cancellationToken).ConfigureAwait(false);
            }
            finally
            {
                notificationState.End(requestId, progressToken);
            }
        });

        filters.Message.OutgoingFilters.Add(next => async (context, cancellationToken) =>
        {
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

            await next(context, cancellationToken).ConfigureAwait(false);
        });
    }

    /// <summary>
    /// Wraps the real upstream transport (production: <c>PythonBackendProcess.CreateUpstreamTransport()</c>;
    /// tests: any real <see cref="IClientTransport"/>) so that <c>notifications/progress</c> and
    /// <c>notifications/message</c> are forwarded downstream synchronously, in the exact order the
    /// wrapped transport delivered them - never through
    /// <see cref="McpClientHandlers.NotificationHandlers"/>.
    ///
    /// This closes a real reordering hazard verified directly against SDK 1.4.1's
    /// <c>McpSessionHandler.ProcessMessagesCoreAsync</c>: every incoming message on a session - request,
    /// response, or notification alike - is dispatched via an unawaited <c>_ = ProcessMessageAsync()</c>
    /// so the transport's own read loop is never blocked. That is safe for the SDK's own request/response
    /// correlation (each carries its own ID), but it means two *notifications* for the same or different
    /// progress tokens, or a notification and the eventual response to the request it was progressing,
    /// can have their <em>handler dispatch</em> - and therefore any relay forward triggered from within
    /// that handler - complete out of the order they were actually read off the wire. No message filter
    /// or notification-handler registration runs earlier than that dispatch, so no fix inside those
    /// extension points can observe true wire order.
    ///
    /// The one place wire order is still guaranteed is the sequential reader loop that produces each
    /// <see cref="ITransport.MessageReader"/> element in the first place. This wrapper's own single
    /// reader loop consumes exactly that: for the two methods this module owns, it awaits the downstream
    /// forward to completion before reading the next upstream message at all, which is what actually
    /// proves both "per-token source order" and "no progress after the owning call's terminal result" -
    /// the latter follows for free, since the response message itself cannot reach the wrapped channel
    /// (and therefore cannot reach <c>ToolsRelay</c>'s pending request) until every progress notification
    /// that precedes it on the wire has already been fully forwarded. Every other message - requests,
    /// responses, and any notification method this module does not own - passes through completely
    /// unchanged for the SDK's own normal (fire-and-forget) processing; this module claims no route
    /// other than the two it registers in <see cref="Register"/>.
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
            Channel.CreateUnbounded<JsonRpcMessage>(new UnboundedChannelOptions { SingleReader = true, SingleWriter = true });
        private readonly Task _pumpTask;

        public OrderPreservingTransport(ITransport inner, RelaySession session, NotificationState notificationState)
        {
            _inner = inner;
            _session = session;
            _notificationState = notificationState;
            _pumpTask = PumpAsync();
        }

        public string? SessionId => _inner.SessionId;

        public ChannelReader<JsonRpcMessage> MessageReader => _passthrough.Reader;

        public Task SendMessageAsync(JsonRpcMessage message, CancellationToken cancellationToken = default) =>
            _inner.SendMessageAsync(message, cancellationToken);

        /// <summary>
        /// The one sequential reader of <see cref="_inner"/>'s <see cref="ITransport.MessageReader"/>:
        /// forwards this module's two owned notification methods downstream and awaits completion before
        /// reading the next upstream message at all; every other message is handed off to
        /// <see cref="_passthrough"/> unchanged for the SDK's own normal processing.
        /// </summary>
        private async Task PumpAsync()
        {
            Exception? failure = null;
            try
            {
                await foreach (var message in _inner.MessageReader.ReadAllAsync().ConfigureAwait(false))
                {
                    if (message is JsonRpcNotification
                        {
                            Method: NotificationMethods.ProgressNotification or NotificationMethods.LoggingMessageNotification,
                        } notification)
                    {
                        if (!_notificationState.Allows(notification))
                        {
                            continue;
                        }

                        if (notification.Method == NotificationMethods.LoggingMessageNotification
                            && (!_session.DownstreamReady.IsCompletedSuccessfully
                                || (await _session.UpstreamAsync(CancellationToken.None).ConfigureAwait(false))
                                    .ServerCapabilities?.Logging is null))
                        {
                            continue;
                        }

                        if (_session.Downstream is { } downstream)
                        {
                            try
                            {
                                await RelaySession.ForwardNotificationAsync(downstream, notification, _session.SessionEndingToken)
                                    .ConfigureAwait(false);
                            }
                            catch
                            {
                                // A downstream send failure for one notification must not stop the
                                // upstream pump or corrupt session teardown; downstream disconnect and
                                // session-ending cleanup are RelaySession's own responsibility, not this
                                // pump's.
                            }
                        }

                        continue;
                    }

                    await _passthrough.Writer.WriteAsync(message).ConfigureAwait(false);
                }
            }
            catch (Exception ex)
            {
                failure = ex;
            }
            finally
            {
                _passthrough.Writer.TryComplete(failure);
            }
        }

        public async ValueTask DisposeAsync()
        {
            await _inner.DisposeAsync().ConfigureAwait(false);

            try
            {
                await _pumpTask.ConfigureAwait(false);
            }
            catch
            {
                // The pump's own failure (if any) already completed _passthrough with it; nothing
                // further to propagate from disposal itself.
            }
        }
    }
}
