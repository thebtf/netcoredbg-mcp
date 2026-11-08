using System.Diagnostics;
using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class DapBuildWindowsRunner
{
    private static readonly TimeSpan NaturalExitGrace = TimeSpan.FromSeconds(1);
    private static readonly TimeSpan DrainTimeout = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan AccountingPollInterval = TimeSpan.FromMilliseconds(50);

    internal static async Task<DapBuildCommandResult> RunAsync(
        string executable, IReadOnlyList<string> arguments, string workingDirectory,
        TimeSpan timeout, CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(workingDirectory);
        cancellationToken.ThrowIfCancellationRequested();
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(timeout);
        deadline.Token.ThrowIfCancellationRequested();

        using var launch = NetCoreDbgSession.WindowsProcessTreeOwnership.Start(executable, arguments, workingDirectory);
        Task<string>? stdout = null;
        Task<string>? stderr = null;
        try
        {
            launch.Input.Dispose();
            stdout = DapBuildOutput.ReadAsync(launch.Output);
            stderr = DapBuildOutput.ReadAsync(launch.Error);
            await launch.Process.WaitForExitAsync(deadline.Token).ConfigureAwait(false);

            if (!await WaitForEmptyAsync(launch.Ownership, NaturalExitGrace, deadline.Token).ConfigureAwait(false))
            {
                launch.Ownership.Terminate();
                if (!await WaitForEmptyAsync(launch.Ownership, DrainTimeout, deadline.Token).ConfigureAwait(false))
                    throw new TimeoutException("The build job did not drain after termination.");
            }
            await WaitForRootSignalAsync(launch.Process, deadline.Token).ConfigureAwait(false);

            var output = await Task.WhenAll(stdout, stderr).WaitAsync(DrainTimeout, deadline.Token).ConfigureAwait(false);
            return new DapBuildCommandResult(launch.Process.ExitCode, output[0], output[1]);
        }
        catch (Exception failure)
        {
            try
            {
                launch.Ownership.Terminate();
                using var cleanupDeadline = new CancellationTokenSource(DrainTimeout);
                await launch.Process.WaitForExitAsync(cleanupDeadline.Token).ConfigureAwait(false);
                if (!await WaitForEmptyAsync(launch.Ownership, DrainTimeout, cleanupDeadline.Token).ConfigureAwait(false))
                    throw new TimeoutException("The build job did not drain after termination.");
                await WaitForRootSignalAsync(launch.Process, cleanupDeadline.Token).ConfigureAwait(false);
                if (stdout is not null && stderr is not null)
                    await Task.WhenAll(stdout, stderr).WaitAsync(cleanupDeadline.Token).ConfigureAwait(false);
            }
            catch (Exception cleanupFailure)
            {
                throw new AggregateException("Build command failed and its owned job or output drain could not be confirmed.", failure, cleanupFailure);
            }

            if (failure is OperationCanceledException && !cancellationToken.IsCancellationRequested && deadline.IsCancellationRequested)
                throw new TimeoutException("The build command timed out after its owned job drained.", failure);
            throw;
        }
    }

    private static async Task WaitForRootSignalAsync(Process process, CancellationToken cancellationToken)
    {
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            var status = WaitForSingleObject(process.SafeHandle, 0);
            if (status == 0) return;
            if (status != 258) throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not observe the build root exit.");
            await Task.Delay(TimeSpan.FromMilliseconds(10), cancellationToken).ConfigureAwait(false);
        }
    }

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(SafeProcessHandle handle, uint milliseconds);

    private static async Task<bool> WaitForEmptyAsync(
        NetCoreDbgSession.WindowsProcessTreeOwnership ownership, TimeSpan timeout, CancellationToken cancellationToken)
    {
        var elapsed = Stopwatch.StartNew();
        while (true)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (ownership.ActiveProcessCount() == 0)
                return true;
            var remaining = timeout - elapsed.Elapsed;
            if (remaining <= TimeSpan.Zero)
                return false;
            await Task.Delay(remaining < AccountingPollInterval ? remaining : AccountingPollInterval, cancellationToken)
                .ConfigureAwait(false);
        }
    }
}
