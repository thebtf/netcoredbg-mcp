using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.NativeScene;

namespace NetCoreDbg.Mcp.Stateless.NativeUi;

internal sealed class NativeUiInvokeSession : IAsyncDisposable
{
    private static readonly TimeSpan OperationTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan CleanupTimeout = TimeSpan.FromSeconds(2);
    private readonly object _gate = new();
    private readonly Func<Task, Task>? _drainCompletion;
    private CancellationTokenSource? _activeCancellation;
    private TaskCompletionSource<bool>? _activeCompletion;
    private NetCoreDbgSession.WindowsProcessTreeLaunch? _pendingCleanup;
    private Task _pendingDrains = Task.CompletedTask;
    private bool _retired;

    internal NativeUiInvokeSession() { }

    internal NativeUiInvokeSession(Func<Task, Task> drainCompletion)
    {
        ArgumentNullException.ThrowIfNull(drainCompletion);
        _drainCompletion = drainCompletion;
    }

    internal Task<JsonObject> InvokeAsync(
        string bridgePath, NativeSceneTargetIdentity target, long hwnd, string automationId,
        Func<bool> authorizeDispatch, CancellationToken cancellationToken, JsonObject? expectedTarget = null,
        bool observed = false)
    {
        if (observed && expectedTarget is null)
            return Task.FromResult(NativeUiInvokeResult.Refused("INVALID_TOOL_ARGUMENTS", observed));
        var request = TargetRequest(observed ? "prepare_observed" : "prepare", target, hwnd);
        request["automationId"] = automationId;
        if (expectedTarget is not null)
            request["expectedTarget"] = expectedTarget.DeepClone();
        if (observed && !NativeUiInvokeBridgeClient.IsRequestWithinBounds(request))
            return Task.FromResult(NativeUiInvokeResult.Refused("INVALID_TOOL_ARGUMENTS", observed));
        return RunAsync(bridgePath, target, hwnd, request, observation: false, authorizeDispatch, cancellationToken, observed);
    }

    internal Task<JsonObject> ObserveAsync(
        string bridgePath, NativeSceneTargetIdentity target, long hwnd, int maxDepth, int maxChildren,
        Func<bool> authorizeDispatch, CancellationToken cancellationToken)
    {
        var request = TargetRequest("get_window_tree", target, hwnd);
        request["maxDepth"] = maxDepth;
        request["maxChildren"] = maxChildren;
        return RunAsync(bridgePath, target, hwnd, request, observation: true, authorizeDispatch, cancellationToken);
    }

    private async Task<JsonObject> RunAsync(
        string bridgePath, NativeSceneTargetIdentity target, long hwnd, JsonObject request, bool observation,
        Func<bool> authorizeDispatch, CancellationToken cancellationToken, bool observedInvoke = false)
    {
        JsonObject Refused(string suffix) => observation
            ? NativeUiDiscoveryResult.Error("UI_DISCOVERY_" + suffix)
            : NativeUiInvokeResult.Refused("UI_INVOKE_" + suffix, observedInvoke);
        CancellationTokenSource operation;
        TaskCompletionSource<bool> completion;
        lock (_gate)
        {
            if (_retired)
                return observation ? NativeUiDiscoveryResult.Error("DEBUG_SESSION_NOT_FOUND") : NativeUiInvokeResult.Refused("DEBUG_SESSION_NOT_FOUND", observedInvoke);
            if (_activeCancellation is not null)
                return Refused("BUSY");
            if (!TryFinishRetainedCleanup())
                return Refused("CLEANUP_UNCONFIRMED");
            operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            operation.CancelAfter(OperationTimeout);
            completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            _activeCancellation = operation;
            _activeCompletion = completion;
        }

        NetCoreDbgSession.WindowsProcessTreeLaunch? launch = null;
        Task drains = Task.CompletedTask;
        JsonObject result;
        try
        {
            operation.Token.ThrowIfCancellationRequested();
            if (!authorizeDispatch())
            {
                result = Refused("TARGET_CHANGED");
            }
            else
            {
                var pipeName = (observation ? "native-ui-observe-" : "native-ui-invoke-") + CreateToken();
                var nonce = CreateToken();
                var isAssembly = bridgePath.EndsWith(".dll", StringComparison.OrdinalIgnoreCase);
                var executable = isAssembly
                    ? Environment.GetEnvironmentVariable("DOTNET_HOST_PATH") is { Length: > 0 } dotnetHost ? dotnetHost : "dotnet.exe"
                    : bridgePath;
                var processId = target.ProcessId.ToString(CultureInfo.InvariantCulture);
                var mode = observation ? "--native-ui-observe-pipe" : "--native-ui-invoke-pipe";
                string[] arguments = isAssembly
                    ? [bridgePath, mode, pipeName, nonce, processId]
                    : [mode, pipeName, nonce, processId];
                // Both read-only observation and two-phase invoke use this same owned helper slot.
                launch = NetCoreDbgSession.WindowsProcessTreeOwnership.Start(executable, arguments);
                drains = Task.WhenAll(DrainHelperOutputAsync(launch.Output), DrainHelperOutputAsync(launch.Error));
                if (_drainCompletion is { } drainCompletion)
                    drains = CompleteDrainsAsync(drains, drainCompletion);
                launch.Input.Dispose();
                await using var client = new NativeUiInvokeBridgeClient(pipeName, OperationTimeout);
                result = observation
                    ? await client.ObserveAsync(nonce, request, operation.Token).ConfigureAwait(false)
                    : await client.SendAsync(nonce, request, authorizeDispatch, operation.Token).ConfigureAwait(false);
                if (observation)
                {
                    operation.Token.ThrowIfCancellationRequested();
                    if (!authorizeDispatch())
                        result = Refused("TARGET_CHANGED");
                    else if (NativeUiDiscoveryContract.Text(result, "kind") == "ui_window_tree" &&
                        !NativeUiDiscoveryContract.IsObservation(result, target, hwnd,
                            request["maxDepth"]!.GetValue<int>(), request["maxChildren"]!.GetValue<int>()))
                        result = Refused("UNREADABLE");
                }
            }
        }
        catch (OperationCanceledException)
        {
            result = observation
                ? Refused(cancellationToken.IsCancellationRequested || IsRetired() ? "CANCELLED" : "TIMEOUT")
                : NativeUiInvokeResult.Refused("UI_INVOKE_UNAVAILABLE", observedInvoke);
        }
        catch (Exception)
        {
            // The invoke transport contains all possibly-dispatched failures; this catch sent no commit.
            result = Refused("UNAVAILABLE");
        }

        var cleaned = launch is null || await CleanupHelperAsync(launch, drains).ConfigureAwait(false);
        lock (_gate)
        {
            if (!cleaned)
            {
                _pendingCleanup = launch;
                _pendingDrains = drains;
            }
            _activeCancellation = null;
            _activeCompletion = null;
        }
        operation.Dispose();
        completion.TrySetResult(cleaned);
        if (!cleaned)
            return observation ? Refused("CLEANUP_UNCONFIRMED") : NativeUiInvokeResult.CleanupUnconfirmed(result, observedInvoke);
        return result;
    }

    internal void Retire()
    {
        CancellationTokenSource? cancellation;
        lock (_gate)
        {
            _retired = true;
            cancellation = _activeCancellation;
        }
        try { cancellation?.Cancel(); }
        catch (ObjectDisposedException) { }
    }

    public async ValueTask DisposeAsync()
    {
        Retire();
        Task? completion;
        lock (_gate)
            completion = _activeCompletion?.Task;
        if (completion is not null)
        {
            try { await completion.WaitAsync(CleanupTimeout).ConfigureAwait(false); }
            catch (TimeoutException) { }
        }
        lock (_gate)
            TryFinishRetainedCleanup();
    }

    private bool IsRetired()
    {
        lock (_gate)
            return _retired;
    }

    // Called only under _gate. Never release the slot merely because a kill was requested.
    private bool TryFinishRetainedCleanup()
    {
        if (_pendingCleanup is not { } pending)
            return true;
        try
        {
            pending.Ownership.Dispose();
            if (!pending.Process.HasExited || !_pendingDrains.IsCompleted)
                return false;
            pending.Dispose();
            _pendingCleanup = null;
            _pendingDrains = Task.CompletedTask;
            return true;
        }
        catch (Exception) { return false; }
    }

    private static async Task<bool> CleanupHelperAsync(NetCoreDbgSession.WindowsProcessTreeLaunch launch, Task drains)
    {
        using var deadline = new CancellationTokenSource(CleanupTimeout);
        try
        {
            launch.Ownership.Dispose();
            await launch.Process.WaitForExitAsync(deadline.Token).ConfigureAwait(false);
            await drains.WaitAsync(deadline.Token).ConfigureAwait(false);
            launch.Dispose();
            return true;
        }
        catch (Exception)
        {
            // Keep the exact process handle and drains for later confirmed cleanup/admission.
            return false;
        }
    }

    private static async Task CompleteDrainsAsync(Task drains, Func<Task, Task> drainCompletion)
    {
        await drains.ConfigureAwait(false);
        await drainCompletion(drains).ConfigureAwait(false);
    }

    private static async Task DrainHelperOutputAsync(Stream stream)
    {
        try { await stream.CopyToAsync(Stream.Null, 4_096).ConfigureAwait(false); }
        catch (Exception) { }
    }

    private static JsonObject TargetRequest(string operation, NativeSceneTargetIdentity target, long hwnd) => new()
    {
        ["operation"] = operation,
        ["processId"] = target.ProcessId,
        ["processIdentity"] = target.ProcessIdentity,
        ["hwnd"] = hwnd,
    };

    private static string CreateToken() => Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
}
