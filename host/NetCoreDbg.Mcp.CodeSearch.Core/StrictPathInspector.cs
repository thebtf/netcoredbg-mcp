using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace NetCoreDbg.Mcp.CodeSearch.Core;

internal sealed record StrictPathInfo(
    bool Exists,
    bool IsDirectory,
    bool IsReparsePoint,
    string? FinalTarget)
{
    internal static StrictPathInfo Missing { get; } = new(
        Exists: false,
        IsDirectory: false,
        IsReparsePoint: false,
        FinalTarget: null);
}

internal interface IStrictPathInspector
{
    StrictPathInfo Inspect(string path, bool expectedDirectory);
    string ResolveFilePath(string path);
}

internal sealed class FileSystemStrictPathInspector : IStrictPathInspector
{
    internal static FileSystemStrictPathInspector Instance { get; } = new();

    private const uint OpenExisting = 3;
    private const uint OpenReparsePoint = 0x00200000;
    private const uint BackupSemantics = 0x02000000;

    private readonly Func<string, FileAttributes> _getAttributes;
    private readonly Func<string, bool, string?> _resolveLinkTarget;

    private FileSystemStrictPathInspector()
        : this(
            File.GetAttributes,
            static (path, expectedDirectory) => expectedDirectory
                ? new DirectoryInfo(path).ResolveLinkTarget(returnFinalTarget: true)?.FullName
                : new FileInfo(path).ResolveLinkTarget(returnFinalTarget: true)?.FullName)
    {
    }

    internal FileSystemStrictPathInspector(
        Func<string, FileAttributes> getAttributes,
        Func<string, bool, string?> resolveLinkTarget)
    {
        _getAttributes = getAttributes;
        _resolveLinkTarget = resolveLinkTarget;
    }

    public StrictPathInfo Inspect(string path, bool expectedDirectory)
    {
        FileAttributes attributes;
        try
        {
            attributes = _getAttributes(path);
        }
        catch (Exception ex) when (ex is FileNotFoundException or DirectoryNotFoundException)
        {
            return StrictPathInfo.Missing;
        }

        var isDirectory = (attributes & FileAttributes.Directory) != 0;
        var isReparsePoint = (attributes & FileAttributes.ReparsePoint) != 0;
        string? finalTarget = null;
        if (isReparsePoint)
        {
            try
            {
                finalTarget = _resolveLinkTarget(path, expectedDirectory);
            }
            catch (Exception ex) when (ex is FileNotFoundException or DirectoryNotFoundException)
            {
            }
        }

        return new StrictPathInfo(
            Exists: true,
            IsDirectory: isDirectory,
            IsReparsePoint: isReparsePoint,
            FinalTarget: finalTarget);
    }

    public string ResolveFilePath(string path)
    {
        if (!OperatingSystem.IsWindows())
        {
            return path;
        }

        var nativePath = path.StartsWith(@"\\?\", StringComparison.Ordinal)
            ? path
            : path.StartsWith(@"\\", StringComparison.Ordinal)
                ? @"\\?\UNC\" + path[2..]
                : @"\\?\" + path;
        // Zero access queries only metadata; BackupSemantics also permits project-directory handles.
        using var handle = CreateFile(nativePath, 0, FileShare.ReadWrite | FileShare.Delete,
            IntPtr.Zero, OpenExisting, OpenReparsePoint | BackupSemantics, IntPtr.Zero);
        if (handle.IsInvalid)
        {
            throw new IOException("Source path metadata is unavailable.", new Win32Exception(Marshal.GetLastPInvokeError()));
        }

        var buffer = new StringBuilder(260);
        var length = GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0);
        if (length >= buffer.Capacity && length <= 32768)
        {
            buffer.EnsureCapacity((int)length + 1);
            length = GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0);
        }
        if (length == 0 || length >= buffer.Capacity)
        {
            throw new IOException("Source path metadata is unavailable.", new Win32Exception(Marshal.GetLastPInvokeError()));
        }

        var finalPath = buffer.ToString();
        return finalPath.StartsWith(@"\\?\UNC\", StringComparison.OrdinalIgnoreCase)
            ? @"\\" + finalPath[8..]
            : finalPath.StartsWith(@"\\?\", StringComparison.Ordinal) ? finalPath[4..] : finalPath;
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(
        string path, uint desiredAccess, FileShare sharing, IntPtr securityAttributes,
        uint creationDisposition, uint flagsAndAttributes, IntPtr templateFile);

    [DllImport("kernel32.dll", EntryPoint = "GetFinalPathNameByHandleW", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(SafeFileHandle handle, StringBuilder path, uint capacity, uint flags);
}
