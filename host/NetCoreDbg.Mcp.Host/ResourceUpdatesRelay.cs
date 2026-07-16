using System.Text.Json;
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

    public static void ConfigureUpstreamHandlers(McpClientHandlers handlers, RelaySession session)
    {
        var existing = (handlers.NotificationHandlers ?? []).ToArray();
        if (existing.Any(pair => pair.Key == NotificationMethods.ResourceUpdatedNotification))
        {
            throw new InvalidOperationException(
                $"{NotificationMethods.ResourceUpdatedNotification} already has an upstream handler.");
        }

        var sessionEndingToken = session.SessionEndingToken;
        var orderingGate = new object();
        Task previousForward = Task.CompletedTask;
        handlers.NotificationHandlers =
        [
            .. existing,
            new(
                NotificationMethods.ResourceUpdatedNotification,
                (notification, cancellationToken) =>
                {
                    Task currentForward;
                    lock (orderingGate)
                    {
                        currentForward = ForwardAfterAsync(
                            previousForward,
                            session,
                            sessionEndingToken,
                            notification,
                            cancellationToken);
                        previousForward = currentForward;
                    }

                    return new ValueTask(currentForward);
                }),
        ];
    }

    private static async Task ForwardAfterAsync(
        Task previousForward,
        RelaySession session,
        CancellationToken sessionEndingToken,
        JsonRpcNotification notification,
        CancellationToken callbackToken)
    {
        try
        {
            await previousForward.ConfigureAwait(false);
        }
        catch (Exception)
        {
            // A failed earlier send must not permanently poison the per-session ordering chain.
        }

        await ForwardUpdateAsync(
            session,
            sessionEndingToken,
            notification,
            callbackToken).ConfigureAwait(false);
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
