using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

internal sealed class SuspendedControlledProcess : IDisposable
{
    private const uint CreateSuspended = 0x00000004;
    private const uint CreateNoWindow = 0x08000000;
    private const uint WaitObject0 = 0;
    private const uint WaitTimeout = 258;
    private readonly SafeProcessHandle _handle;
    private bool _disposed;

    private SuspendedControlledProcess(SafeProcessHandle handle, Process process)
    {
        _handle = handle;
        Process = process;
    }

    internal Process Process { get; }

    internal static SuspendedControlledProcess Start()
    {
        if (!OperatingSystem.IsWindows())
        {
            throw new PlatformNotSupportedException("The suspended initial-module fixture requires Windows.");
        }

        var executable = Environment.ProcessPath
            ?? throw new InvalidOperationException("The controlled adapter executable path is unavailable.");
        var startup = new StartupInfo { Size = checked((uint)Marshal.SizeOf<StartupInfo>()) };
        // CREATE_SUSPENDED holds the initial thread before user-mode loader initialization.
        // No ResumeThread call exists in this fixture; disposal terminates this exact process.
        // https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags
        // As in the host's Windows ownership path, the child stays in the inherited job:
        // no breakaway flag or inherited stdio/other handles are requested.
        if (!CreateProcessW(
            executable, new StringBuilder($"\"{executable}\" --controlled-dap-descendant"),
            IntPtr.Zero, IntPtr.Zero, false, CreateSuspended | CreateNoWindow,
            IntPtr.Zero, null, ref startup, out var information))
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not create the suspended controlled descendant.");
        }

        using var initialThread = new SafeWaitHandle(information.Thread, ownsHandle: true);
        var handle = new SafeProcessHandle(information.Process, ownsHandle: true);
        Process? process = null;
        try
        {
            process = System.Diagnostics.Process.GetProcessById(checked((int)information.ProcessId));
            return new SuspendedControlledProcess(handle, process);
        }
        catch
        {
            try
            {
                TerminateAndWait(handle);
            }
            finally
            {
                process?.Dispose();
                handle.Dispose();
            }
            throw;
        }
    }

    public void Dispose()
    {
        if (_disposed)
        {
            return;
        }
        _disposed = true;
        try
        {
            TerminateAndWait(_handle);
        }
        finally
        {
            Process.Dispose();
            _handle.Dispose();
        }
    }

    private static void TerminateAndWait(SafeProcessHandle handle)
    {
        var state = WaitForSingleObject(handle, 0);
        if (state == WaitObject0)
        {
            return;
        }
        if (state != WaitTimeout)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not observe the owned suspended descendant.");
        }

        if (!TerminateProcess(handle, 1))
        {
            var error = Marshal.GetLastWin32Error();
            // A concurrent host job teardown can already have terminated this same kernel object.
            if (WaitForSingleObject(handle, 0) != WaitObject0)
            {
                throw new Win32Exception(error, "Could not terminate the owned suspended descendant.");
            }
        }
        // TerminateProcess is asynchronous; closing its handle alone is not cleanup evidence.
        if (WaitForSingleObject(handle, 5_000) != WaitObject0)
        {
            throw new InvalidOperationException("The owned suspended descendant did not exit within five seconds.");
        }
    }

    [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateProcessW(
        string applicationName, StringBuilder commandLine, IntPtr processAttributes, IntPtr threadAttributes,
        [MarshalAs(UnmanagedType.Bool)] bool inheritHandles, uint creationFlags, IntPtr environment,
        string? currentDirectory, ref StartupInfo startupInfo, out ProcessInformation processInformation);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateProcess(SafeProcessHandle process, uint exitCode);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(SafeProcessHandle handle, uint milliseconds);

    [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
    private struct StartupInfo
    {
        public uint Size;
        public string? Reserved;
        public string? Desktop;
        public string? Title;
        public uint X;
        public uint Y;
        public uint XSize;
        public uint YSize;
        public uint XCountChars;
        public uint YCountChars;
        public uint FillAttribute;
        public uint Flags;
        public ushort ShowWindow;
        public ushort Reserved2Count;
        public IntPtr Reserved2;
        public IntPtr StandardInput;
        public IntPtr StandardOutput;
        public IntPtr StandardError;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct ProcessInformation
    {
        public IntPtr Process;
        public IntPtr Thread;
        public uint ProcessId;
        public uint ThreadId;
    }
}
