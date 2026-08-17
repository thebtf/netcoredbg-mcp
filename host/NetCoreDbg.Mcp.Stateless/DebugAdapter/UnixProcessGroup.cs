using System.ComponentModel;
using System.Runtime.InteropServices;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class UnixProcessGroup
{
    private const int SigKill = 9;
    private const int NoSuchProcess = 3;

    public static void BecomeOwnProcessGroup()
    {
        if (SetProcessGroup(0, 0) != 0)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not create the debugger process group.");
        }
    }

    public static void Terminate(int processGroupId)
    {
        if (Kill(-processGroupId, SigKill) != 0 && Marshal.GetLastWin32Error() != NoSuchProcess)
        {
            throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not terminate the debugger process group.");
        }
    }

    [DllImport("libc", SetLastError = true, EntryPoint = "setpgid")]
    private static extern int SetProcessGroup(int processId, int processGroupId);

    [DllImport("libc", SetLastError = true, EntryPoint = "kill")]
    private static extern int Kill(int processId, int signal);
}
