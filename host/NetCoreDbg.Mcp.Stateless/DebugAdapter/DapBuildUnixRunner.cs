using System.Buffers.Binary;
using System.ComponentModel;
using System.Diagnostics;
using System.IO.Pipes;
using System.Runtime.ExceptionServices;
using System.Runtime.InteropServices;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class DapBuildUnixRunner
{
    private static readonly TimeSpan CleanupDeadline = TimeSpan.FromSeconds(10);
    private static readonly TimeSpan GuardianDeadline = TimeSpan.FromSeconds(45);

    internal static async Task<DapBuildCommandResult> RunAsync(
        string executable,
        IReadOnlyList<string> arguments,
        string workingDirectory,
        TimeSpan timeout,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        workingDirectory = Path.GetFullPath(workingDirectory);
        using var timeoutSource = new CancellationTokenSource(timeout);
        using var stopSource = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken, timeoutSource.Token);
        using var control = new AnonymousPipeServerStream(PipeDirection.Out, HandleInheritability.Inheritable);
        using var status = new AnonymousPipeServerStream(PipeDirection.In, HandleInheritability.Inheritable);

        var assemblyPath = typeof(DapBuildUnixRunner).Assembly.Location;
        if (string.IsNullOrWhiteSpace(assemblyPath))
            throw new InvalidOperationException("The stateless proxy assembly path is unavailable.");
        var appHost = Path.ChangeExtension(assemblyPath, extension: null);
        var useAppHost = File.Exists(appHost);
        var dotnetHost = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
        var startInfo = new ProcessStartInfo
        {
            FileName = useAppHost ? appHost : string.IsNullOrWhiteSpace(dotnetHost) ? "dotnet" : dotnetHost,
            WorkingDirectory = workingDirectory,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
        };
        if (!useAppHost) startInfo.ArgumentList.Add(assemblyPath);
        startInfo.ArgumentList.Add("--unix-build-command-proxy");
        startInfo.ArgumentList.Add(control.GetClientHandleAsString());
        startInfo.ArgumentList.Add(status.GetClientHandleAsString());
        startInfo.ArgumentList.Add(workingDirectory);
        startInfo.ArgumentList.Add(executable);
        foreach (var argument in arguments) startInfo.ArgumentList.Add(argument);

        using var guardian = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Could not start the build command guardian.");
        var stdout = DapBuildOutput.ReadAsync(guardian.StandardOutput.BaseStream);
        var stderr = DapBuildOutput.ReadAsync(guardian.StandardError.BaseStream);
        var childExit = new TaskCompletionSource<int>(TaskCreationOptions.RunContinuationsAsynchronously);
        var terminating = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        var statusReader = ReadStatusAsync(status, childExit, terminating);
        var guardianExit = guardian.WaitForExitAsync();
        ExceptionDispatchInfo? failure = null;
        int exitCode = 0;
        try
        {
            guardian.StandardInput.Close();
            control.DisposeLocalCopyOfClientHandle();
            status.DisposeLocalCopyOfClientHandle();
            var completed = await Task.WhenAny(childExit.Task, guardianExit)
                .WaitAsync(stopSource.Token).ConfigureAwait(false);
            if (completed == guardianExit && !childExit.Task.IsCompletedSuccessfully)
                throw new InvalidOperationException("The build command guardian exited before reporting the child status.");
            exitCode = await childExit.Task.WaitAsync(stopSource.Token).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (stopSource.IsCancellationRequested)
        {
            failure = ExceptionDispatchInfo.Capture(cancellationToken.IsCancellationRequested
                ? new OperationCanceledException(cancellationToken)
                : new TimeoutException("The build command timed out."));
        }
        catch (Exception exception)
        {
            failure = ExceptionDispatchInfo.Capture(exception);
        }

        control.Dispose();
        try
        {
            await Task.WhenAll(terminating.Task, guardianExit).WaitAsync(GuardianDeadline).ConfigureAwait(false);
            if (guardian.ExitCode != 0)
                throw new InvalidOperationException($"The build command guardian exited unexpectedly ({guardian.ExitCode}); group cleanup is unconfirmed.");
            await Task.WhenAll(stdout, stderr).WaitAsync(CleanupDeadline).ConfigureAwait(false);
        }
        catch (Exception exception)
        {
            throw new DapBuildCleanupException("The build command process group or output streams could not be confirmed closed.", exception);
        }
        finally
        {
            status.Dispose();
        }

        await statusReader.ConfigureAwait(false);
        failure?.Throw();
        return new DapBuildCommandResult(exitCode, await stdout.ConfigureAwait(false), await stderr.ConfigureAwait(false));
    }

    internal static async Task RunGuardianAsync(string[] arguments)
    {
        UnixProcessGroup.BecomeOwnProcessGroup();
        var groupId = Environment.ProcessId;
        using var control = new AnonymousPipeClientStream(PipeDirection.In, arguments[1]);
        using var status = new AnonymousPipeClientStream(PipeDirection.Out, arguments[2]);
        SetCloseOnExec(control);
        SetCloseOnExec(status);
        var controlRead = control.ReadAsync(new byte[1]).AsTask();
        using var anchorControl = new AnonymousPipeServerStream(PipeDirection.Out, HandleInheritability.Inheritable);
        using var anchorReady = new AnonymousPipeServerStream(PipeDirection.In, HandleInheritability.Inheritable);
        var assemblyPath = typeof(DapBuildUnixRunner).Assembly.Location;
        if (string.IsNullOrWhiteSpace(assemblyPath))
            throw new InvalidOperationException("The stateless proxy assembly path is unavailable.");
        var appHost = Path.ChangeExtension(assemblyPath, extension: null);
        var useAppHost = File.Exists(appHost);
        var dotnetHost = Environment.GetEnvironmentVariable("DOTNET_HOST_PATH");
        var anchorStart = new ProcessStartInfo
        {
            FileName = useAppHost ? appHost : string.IsNullOrWhiteSpace(dotnetHost) ? "dotnet" : dotnetHost,
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        if (!useAppHost) anchorStart.ArgumentList.Add(assemblyPath);
        anchorStart.ArgumentList.Add("--unix-build-group-anchor");
        anchorStart.ArgumentList.Add(anchorControl.GetClientHandleAsString());
        anchorStart.ArgumentList.Add(anchorReady.GetClientHandleAsString());
        using var anchor = Process.Start(anchorStart)
            ?? throw new InvalidOperationException("Could not start the build group anchor.");
        var anchorExit = anchor.WaitForExitAsync();
        try
        {
            anchorControl.DisposeLocalCopyOfClientHandle();
            anchorReady.DisposeLocalCopyOfClientHandle();
            SetCloseOnExec(anchorControl);
            SetCloseOnExec(anchorReady);
            var ready = new byte[1];
            var readyRead = anchorReady.ReadExactlyAsync(ready).AsTask();
            var first = await Task.WhenAny(readyRead, controlRead).WaitAsync(CleanupDeadline).ConfigureAwait(false);
            if (first == controlRead || controlRead.IsCompleted)
            {
                await controlRead.ConfigureAwait(false);
            }
            else
            {
                await readyRead.ConfigureAwait(false);
                if (ready[0] != 1 || anchor.HasExited)
                    throw new InvalidOperationException("The build group anchor did not remain ready.");
                if (controlRead.IsCompleted)
                    await controlRead.ConfigureAwait(false);
                else
                    await RunCommandInGroupAsync(arguments, groupId, anchor, anchorControl, anchorReady, controlRead, status).ConfigureAwait(false);
            }
        }
        finally
        {
            anchorControl.Dispose();
            await anchorExit.WaitAsync(CleanupDeadline).ConfigureAwait(false);
        }
        await status.WriteAsync(new byte[] { 2 }).ConfigureAwait(false);
        await status.FlushAsync().ConfigureAwait(false);
    }

    private static async Task RunCommandInGroupAsync(
        string[] arguments, int groupId, Process anchor, Stream anchorControl, Stream anchorReady, Task<int> controlRead, Stream status)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = arguments[4],
            WorkingDirectory = arguments[3],
            UseShellExecute = false,
            CreateNoWindow = true,
        };
        for (var i = 5; i < arguments.Length; i++) startInfo.ArgumentList.Add(arguments[i]);
        if (controlRead.IsCompleted)
        {
            await controlRead.ConfigureAwait(false);
            return;
        }
        var moved = false;
        using var command = Process.Start(startInfo)
            ?? throw new InvalidOperationException("Could not start the build command.");
        try
        {
            var commandExit = command.WaitForExitAsync();
            if (SetProcessGroup(0, anchor.Id) != 0)
                throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not move the build guardian to its anchor group.");
            moved = true;
            await anchorControl.WriteAsync(new byte[] { 1 }).ConfigureAwait(false);
            await anchorControl.FlushAsync().ConfigureAwait(false);
            var confirmation = new byte[1];
            await anchorReady.ReadExactlyAsync(confirmation).AsTask().WaitAsync(CleanupDeadline).ConfigureAwait(false);
            if (confirmation[0] != 2)
                throw new InvalidOperationException("The build group anchor identity was not confirmed.");
            if (await Task.WhenAny(commandExit, controlRead).ConfigureAwait(false) == commandExit)
            {
                await commandExit.ConfigureAwait(false);
                var message = new byte[5];
                message[0] = 1;
                BinaryPrimitives.WriteInt32LittleEndian(message.AsSpan(1), command.ExitCode);
                await status.WriteAsync(message).ConfigureAwait(false);
                await status.FlushAsync().ConfigureAwait(false);
                await controlRead.ConfigureAwait(false);
            }
            else
            {
                await controlRead.ConfigureAwait(false);
            }
        }
        finally
        {
            if (!moved)
                UnixProcessGroup.TerminateOwnProcessGroup();
            await TerminateAndDrainGroupAsync(groupId, command).ConfigureAwait(false);
        }
    }

    internal static async Task RunAnchorAsync(string[] arguments)
    {
        UnixProcessGroup.BecomeOwnProcessGroup();
        using var control = new AnonymousPipeClientStream(PipeDirection.In, arguments[1]);
        using var ready = new AnonymousPipeClientStream(PipeDirection.Out, arguments[2]);
        await ready.WriteAsync(new byte[] { 1 }).ConfigureAwait(false);
        await ready.FlushAsync().ConfigureAwait(false);
        var request = new byte[1];
        while (await control.ReadAsync(request).ConfigureAwait(false) != 0)
        {
            if (request[0] != 1) throw new InvalidDataException("Invalid build group anchor request.");
            await ready.WriteAsync(new byte[] { 2 }).ConfigureAwait(false);
            await ready.FlushAsync().ConfigureAwait(false);
        }
    }

    private static async Task TerminateAndDrainGroupAsync(int groupId, Process command)
    {
        const int noSuchProcess = 3;
        if (Kill(-groupId, 9) != 0 && Marshal.GetLastWin32Error() != noSuchProcess)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not terminate the build process group.");
        await command.WaitForExitAsync().WaitAsync(CleanupDeadline).ConfigureAwait(false);
        var elapsed = Stopwatch.StartNew();
        while (true)
        {
            if (Kill(-groupId, 0) != 0)
            {
                var error = Marshal.GetLastWin32Error();
                if (error == noSuchProcess) return;
                throw new Win32Exception(error, "Could not observe the build process group.");
            }
            if (elapsed.Elapsed >= CleanupDeadline)
                throw new TimeoutException("The build process group did not drain.");
            await Task.Delay(25).ConfigureAwait(false);
        }
    }

    [DllImport("libc", EntryPoint = "setpgid", SetLastError = true)]
    private static extern int SetProcessGroup(int processId, int groupId);

    [DllImport("libc", EntryPoint = "kill", SetLastError = true)]
    private static extern int Kill(int processId, int signal);

    private static void SetCloseOnExec(PipeStream stream)
    {
        const int getDescriptorFlags = 1;
        const int setDescriptorFlags = 2;
        const int closeOnExec = 1;
        var descriptor = stream.SafePipeHandle.DangerousGetHandle().ToInt32();
        var flags = Fcntl(descriptor, getDescriptorFlags, 0);
        if (flags == -1 || Fcntl(descriptor, setDescriptorFlags, flags | closeOnExec) == -1)
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not protect the build guardian pipe.");
    }

    [DllImport("libc", EntryPoint = "fcntl", SetLastError = true)]
    private static extern int Fcntl(int descriptor, int command, int argument);

    private static async Task ReadStatusAsync(
        Stream status,
        TaskCompletionSource<int> childExit,
        TaskCompletionSource<bool> terminating)
    {
        try
        {
            var kind = new byte[1];
            while (true)
            {
                await status.ReadExactlyAsync(kind).ConfigureAwait(false);
                if (kind[0] == 1)
                {
                    var value = new byte[4];
                    await status.ReadExactlyAsync(value).ConfigureAwait(false);
                    if (!childExit.TrySetResult(BinaryPrimitives.ReadInt32LittleEndian(value)))
                        throw new InvalidDataException("Duplicate build command status.");
                }
                else if (kind[0] == 2)
                {
                    terminating.TrySetResult(true);
                    return;
                }
                else
                {
                    throw new InvalidDataException("Invalid build guardian status.");
                }
            }
        }
        catch (Exception exception)
        {
            childExit.TrySetException(exception);
            terminating.TrySetException(exception);
        }
    }
}
