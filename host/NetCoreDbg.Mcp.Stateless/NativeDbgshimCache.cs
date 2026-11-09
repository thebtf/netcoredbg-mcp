using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace NetCoreDbg.Mcp.Stateless;

internal sealed record NativeDbgshimCacheResult(int Cached, int Reused, int Conflicts, IReadOnlyList<string> Warnings);

internal static class NativeDbgshimCache
{
    private const int MaxVersionsPerRoot = 1024;
    private const int MaxShimBytes = 32 * 1024 * 1024;
    private const long MaxNewCacheBytes = 256L * 1024 * 1024;
    private const long MaxScannedBytes = 512L * 1024 * 1024;
    private const int MaxWarnings = 32;
    private const uint GenericRead = 0x80000000;
    private const uint OpenExisting = 3;
    private const uint OpenReparsePoint = 0x00200000;
    private const uint FileAttributeReparsePoint = 0x400;
    private const uint FileAttributeDirectory = 0x10;

    internal static NativeDbgshimCacheResult Populate(string home) => Populate(home,
        [Environment.GetEnvironmentVariable("DOTNET_ROOT"),
         DotnetUnder(Environment.GetEnvironmentVariable("ProgramFiles") ?? Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles)),
         DotnetUnder(Environment.GetEnvironmentVariable("ProgramFiles(x86)"))], MaxNewCacheBytes);

    private static NativeDbgshimCacheResult Populate(string home, string?[] roots, long maxNewBytes) =>
        Populate(home, roots, maxNewBytes, MaxScannedBytes, MaxShimBytes);

    private static NativeDbgshimCacheResult Populate(string home, string?[] roots, long maxNewBytes, long maxScannedBytes, int maxShimBytes)
    {
        var warnings = new List<string>();
        var cached = 0;
        var reused = 0;
        var conflicts = 0;
        long newBytes = 0;
        long scannedBytes = 0;
        var scanBudgetExhausted = false;
        if (!OperatingSystem.IsWindows())
        {
            warnings.Add("dbgshim caching is unavailable on this platform: safe regular-file opening has not been established; no cache files were written.");
            return new(cached, reused, conflicts, warnings);
        }

        void Warn(string message)
        {
            if (warnings.Count < MaxWarnings - 1) warnings.Add(message);
            else if (warnings.Count == MaxWarnings - 1) warnings.Add("Further dbgshim cache warnings were suppressed.");
        }

        try
        {
            var managed = Path.Combine(Path.GetFullPath(home), ".netcoredbg-mcp");
            var cache = Path.Combine(managed, "dbgshim");
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var root in roots)
            {
                if (scanBudgetExhausted) break;
                if (string.IsNullOrWhiteSpace(root)) continue;
                try
                {
                    var shared = Path.Combine(root, "shared", "Microsoft.NETCore.App");
                    if (!TryGetAttributes(shared, out var sharedAttributes)) continue;
                    if ((sharedAttributes & FileAttributes.Directory) == 0)
                    {
                        Warn($"dbgshim runtime root '{shared}' is not a directory.");
                        continue;
                    }
                    var count = 0;
                    foreach (var versionPath in Directory.EnumerateDirectories(shared))
                    {
                        if (++count > MaxVersionsPerRoot)
                        {
                            Warn($"dbgshim scan stopped after {MaxVersionsPerRoot} versions in '{shared}'.");
                            break;
                        }
                        var version = Path.GetFileName(versionPath);
                        if (string.IsNullOrEmpty(version) || seen.Contains(version)) continue;
                        try
                        {
                            RequireDirectory(versionPath);
                            var source = Path.Combine(versionPath, "dbgshim.dll");
                            if (!TryGetAttributes(source, out _)) continue;
                            seen.Add(version);
                            using var sourceStream = OpenRegularFile(source);
                            var sourceLength = sourceStream.Length;
                            if (sourceLength > maxShimBytes)
                            {
                                Warn($"dbgshim for version '{version}' exceeds the {maxShimBytes}-byte size limit.");
                                continue;
                            }
                            if (sourceLength > maxScannedBytes - scannedBytes)
                            {
                                Warn("dbgshim scan read budget exhausted; remaining versions were not inspected.");
                                scanBudgetExhausted = true;
                                break;
                            }
                            scannedBytes += sourceLength;
                            var bytes = ReadBounded(sourceStream, maxShimBytes);
                            EnsureDirectory(managed);
                            EnsureDirectory(cache);
                            var destinationDirectory = Path.Combine(cache, version);
                            EnsureDirectory(destinationDirectory);
                            var destination = Path.Combine(destinationDirectory, "dbgshim.dll");
                            if (TryGetAttributes(destination, out _))
                            {
                                using var existing = OpenRegularFile(destination);
                                if (EqualBytes(existing, bytes)) reused++;
                                else
                                {
                                    conflicts++;
                                    Warn($"dbgshim cache conflict for version '{version}' at '{destination}'; existing file was preserved.");
                                }
                                continue;
                            }
                            if (bytes.LongLength > maxNewBytes - newBytes)
                            {
                                Warn($"dbgshim cache write budget exhausted; version '{version}' was not added.");
                                continue;
                            }
                            string? staging = null;
                            try
                            {
                                var candidate = Path.Combine(destinationDirectory, $".dbgshim-{Guid.NewGuid():N}.tmp");
                                using (var output = new FileStream(candidate, FileMode.CreateNew, FileAccess.Write, FileShare.None))
                                {
                                    staging = candidate;
                                    output.Write(bytes);
                                }
                                // These path checks reject existing reparse points, but do not pin parent
                                // directories against replacement by another process running as this user.
                                RequireDirectory(managed);
                                RequireDirectory(cache);
                                RequireDirectory(destinationDirectory);
                                try
                                {
                                    File.Move(staging, destination, false);
                                    staging = null;
                                    cached++;
                                    newBytes += bytes.LongLength;
                                }
                                catch (IOException) when (File.Exists(destination))
                                {
                                    using var existing = OpenRegularFile(destination);
                                    if (EqualBytes(existing, bytes)) reused++;
                                    else
                                    {
                                        conflicts++;
                                        Warn($"dbgshim cache conflict for version '{version}' at '{destination}'; existing file was preserved.");
                                    }
                                }
                            }
                            finally
                            {
                                if (staging is not null)
                                {
                                    try { File.Delete(staging); }
                                    catch (Exception exception) when (IsFileError(exception))
                                    {
                                        Warn($"Could not remove dbgshim staging file '{staging}': {exception.Message}; inspect and remove it manually.");
                                    }
                                }
                            }
                        }
                        catch (Exception exception) when (IsFileError(exception))
                        {
                            Warn($"Could not cache dbgshim for version '{version}': {exception.Message}");
                        }
                    }
                }
                catch (Exception exception) when (IsFileError(exception))
                {
                    Warn($"Could not scan dbgshim runtime root '{root}': {exception.Message}");
                }
            }
        }
        catch (Exception exception) when (IsFileError(exception))
        {
            Warn($"Could not prepare dbgshim cache: {exception.Message}");
        }
        return new(cached, reused, conflicts, warnings);
    }

    private static string? DotnetUnder(string? path) => string.IsNullOrWhiteSpace(path) ? null : Path.Combine(path, "dotnet");

    private static bool TryGetAttributes(string path, out FileAttributes attributes)
    {
        try { attributes = File.GetAttributes(path); return true; }
        catch (FileNotFoundException) { attributes = default; return false; }
        catch (DirectoryNotFoundException) { attributes = default; return false; }
    }

    private static void EnsureDirectory(string path)
    {
        if (!Directory.Exists(path)) Directory.CreateDirectory(path);
        RequireDirectory(path);
    }

    private static void RequireDirectory(string path)
    {
        var attributes = File.GetAttributes(path);
        if ((attributes & FileAttributes.Directory) == 0 || (attributes & FileAttributes.ReparsePoint) != 0)
            throw new IOException($"'{path}' is not a non-reparse directory.");
    }

    private static FileStream OpenRegularFile(string path)
    {
        var handle = CreateFile(path, GenericRead, (uint)FileShare.Read, IntPtr.Zero, OpenExisting, OpenReparsePoint, IntPtr.Zero);
        if (handle.IsInvalid)
        {
            var error = Marshal.GetLastWin32Error();
            handle.Dispose();
            throw new IOException($"Could not open '{path}' safely.", new Win32Exception(error));
        }
        try
        {
            if (GetFileType(handle) != 1 || !GetFileInformationByHandle(handle, out var info) ||
                (info.Attributes & (FileAttributeReparsePoint | FileAttributeDirectory)) != 0)
                throw new IOException($"'{path}' is not a regular non-reparse file.");
            return new FileStream(handle, FileAccess.Read);
        }
        catch
        {
            handle.Dispose();
            throw;
        }
    }

    private static byte[] ReadBounded(FileStream stream, int maxShimBytes)
    {
        var length = stream.Length;
        if (length > maxShimBytes) throw new IOException($"dbgshim exceeds the {maxShimBytes}-byte size limit.");
        var bytes = new byte[(int)length];
        stream.ReadExactly(bytes);
        if (stream.ReadByte() != -1) throw new IOException("dbgshim changed while being read.");
        return bytes;
    }

    private static bool EqualBytes(FileStream stream, byte[] bytes)
    {
        if (stream.Length != bytes.Length) return false;
        var buffer = new byte[8192];
        var offset = 0;
        while (offset < bytes.Length)
        {
            var read = stream.Read(buffer, 0, Math.Min(buffer.Length, bytes.Length - offset));
            if (read == 0 || !buffer.AsSpan(0, read).SequenceEqual(bytes.AsSpan(offset, read))) return false;
            offset += read;
        }
        return stream.ReadByte() == -1;
    }

    private static bool IsFileError(Exception exception) => exception is IOException or UnauthorizedAccessException
        or ArgumentException or NotSupportedException or System.Security.SecurityException;

    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        internal uint Attributes;
        internal System.Runtime.InteropServices.ComTypes.FILETIME CreationTime;
        internal System.Runtime.InteropServices.ComTypes.FILETIME LastAccessTime;
        internal System.Runtime.InteropServices.ComTypes.FILETIME LastWriteTime;
        internal uint VolumeSerialNumber;
        internal uint FileSizeHigh;
        internal uint FileSizeLow;
        internal uint NumberOfLinks;
        internal uint FileIndexHigh;
        internal uint FileIndexLow;
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr securityAttributes, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle file, out ByHandleFileInformation information);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint GetFileType(SafeFileHandle file);
}
