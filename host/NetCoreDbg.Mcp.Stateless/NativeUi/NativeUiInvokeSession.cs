using System.Diagnostics;
using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.NativeScene;

namespace NetCoreDbg.Mcp.Stateless.NativeUi;

internal sealed class NativeUiInvokeSession : IAsyncDisposable
{
    private static readonly TimeSpan ActionTimeout = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan CleanupTimeout = TimeSpan.FromSeconds(1);
    private readonly object _gate = new();
    private CancellationTokenSource? _activeCancellation;
    private TaskCompletionSource<bool>? _activeCompletion;
    private bool _retired;

    internal async Task<JsonObject> InvokeAsync(
        string bridgePath,
        NativeSceneTargetIdentity target,
        long hwnd,
        string automationId,
        Func<bool> authorizeDispatch,
        CancellationToken cancellationToken)
    {
        CancellationTokenSource operation;
        TaskCompletionSource<bool> completion;
        lock (_gate)
        {
            if (_retired)
                return NativeUiInvokeResult.Refused("DEBUG_SESSION_NOT_FOUND");
            if (_activeCancellation is not null)
                return NativeUiInvokeResult.Refused("UI_INVOKE_BUSY");
            operation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            operation.CancelAfter(ActionTimeout);
            completion = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            _activeCancellation = operation;
            _activeCompletion = completion;
        }

        NetCoreDbgSession.WindowsProcessTreeLaunch? launch = null;
        Task stdoutDrain = Task.CompletedTask;
        Task stderrDrain = Task.CompletedTask;
        try
        {
            operation.Token.ThrowIfCancellationRequested();
            if (!authorizeDispatch())
                return NativeUiInvokeResult.Refused("UI_INVOKE_TARGET_CHANGED");
            var pipeName = "native-ui-invoke-" + CreateToken();
            var nonce = CreateToken();
            var isAssembly = bridgePath.EndsWith(".dll", StringComparison.OrdinalIgnoreCase);
            var executable = isAssembly
                ? Environment.GetEnvironmentVariable("DOTNET_HOST_PATH") is { Length: > 0 } dotnetHost ? dotnetHost : "dotnet.exe"
                : bridgePath;
            var processId = target.ProcessId.ToString(CultureInfo.InvariantCulture);
            string[] arguments = isAssembly
                ? [bridgePath, "--native-ui-invoke-pipe", pipeName, nonce, processId]
                : ["--native-ui-invoke-pipe", pipeName, nonce, processId];
            // JOB_LIST binds the helper to its host-owned kill-on-close job during CreateProcess itself.
            launch = NetCoreDbgSession.WindowsProcessTreeOwnership.Start(executable, arguments);
            stdoutDrain = DrainHelperOutputAsync(launch.Output);
            stderrDrain = DrainHelperOutputAsync(launch.Error);
            launch.Input.Dispose();
            await using var client = new NativeUiInvokeBridgeClient(pipeName, ActionTimeout);
            return await client.SendAsync(
                nonce,
                new JsonObject
                {
                    ["operation"] = "prepare",
                    ["processId"] = target.ProcessId,
                    ["processIdentity"] = target.ProcessIdentity,
                    ["hwnd"] = hwnd,
                    ["automationId"] = automationId,
                },
                authorizeDispatch,
                operation.Token).ConfigureAwait(false);
        }
        catch (Exception)
        {
            // SendAsync contains all possibly-dispatched failures; this path has sent no commit.
            return NativeUiInvokeResult.Refused("UI_INVOKE_UNAVAILABLE");
        }
        finally
        {
            try
            {
                if (launch is not null)
                {
                    try
                    {
                        launch.Ownership.Dispose();
                    }
                    catch (Exception)
                    {
                    }
                    await StopHelperAsync(launch.Process).ConfigureAwait(false);
                    try
                    {
                        await Task.WhenAll(stdoutDrain, stderrDrain).WaitAsync(CleanupTimeout).ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                    }
                }
            }
            finally
            {
                try
                {
                    launch?.Dispose();
                }
                catch (Exception)
                {
                    // Cleanup cannot replace a known or uncertain invocation result.
                }
                lock (_gate)
                {
                    _activeCancellation = null;
                    _activeCompletion = null;
                }
                operation.Dispose();
                completion.TrySetResult(true);
            }
        }
    }

    internal void Retire()
    {
        CancellationTokenSource? cancellation;
        lock (_gate)
        {
            _retired = true;
            cancellation = _activeCancellation;
        }
        try
        {
            cancellation?.Cancel();
        }
        catch (ObjectDisposedException)
        {
        }
    }

    public async ValueTask DisposeAsync()
    {
        Retire();
        Task? completion;
        lock (_gate)
            completion = _activeCompletion?.Task;
        if (completion is null)
            return;
        try
        {
            await completion.WaitAsync(CleanupTimeout + CleanupTimeout).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            // The operation retains its own finally/owned helper cleanup; DAP teardown must not wait indefinitely.
        }
    }

    private static async Task DrainHelperOutputAsync(Stream stream)
    {
        try
        {
            await stream.CopyToAsync(Stream.Null, 4_096).ConfigureAwait(false);
        }
        catch (Exception)
        {
        }
    }

    private static async Task StopHelperAsync(Process? process)
    {
        if (process is null)
            return;
        try
        {
            if (!process.HasExited)
            {
                process.Kill();
                using var timeout = new CancellationTokenSource(CleanupTimeout);
                await process.WaitForExitAsync(timeout.Token).ConfigureAwait(false);
            }
        }
        catch (Exception)
        {
        }
        finally
        {
            process.Dispose();
        }
    }

    private static string CreateToken() => Convert.ToHexString(RandomNumberGenerator.GetBytes(32));
}
