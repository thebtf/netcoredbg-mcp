using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed class LegacyEncSource : IAsyncDisposable
{
    private const uint GenericRead = 0x80000000;
    private const uint GenericWrite = 0x40000000;
    private const uint Delete = 0x00010000;
    private const uint ShareReadWrite = 3;
    private const uint OpenExisting = 3;
    private const uint CreateNew = 1;
    private const uint BackupSemantics = 0x02000000;
    private const uint OpenReparsePoint = 0x00200000;
    private const uint WriteThrough = 0x80000000;
    private const uint ReparsePoint = 0x400;
    private const uint DirectoryAttribute = 0x10;
    private static readonly UTF8Encoding StrictUtf8 = new(false, true);

    private readonly List<SafeFileHandle> _directories;
    private readonly FileStream _source;
    private readonly byte[] _original;
    private readonly bool _hasBom;
    private readonly SemaphoreSlim _gate = new(1, 1);
    private FileStream? _backup;
    private bool _writeStarted;
    private bool _sourceWriteStarted;
    private bool _writeSucceeded;
    private bool _restoreStarted;
    private bool _restored;
    private bool _completed;
    private bool _disposed;

    private LegacyEncSource(string targetPath, string snapshot, byte[] original, bool hasBom,
        FileStream source, List<SafeFileHandle> directories)
    {
        TargetPath = targetPath;
        Snapshot = snapshot;
        _original = original;
        _hasBom = hasBom;
        _source = source;
        _directories = directories;
    }

    internal string TargetPath { get; }
    internal string Snapshot { get; }
    internal string? BackupPath { get; private set; }

    internal static async Task<LegacyEncSource> OpenAsync(string projectRoot, string file, CancellationToken cancellationToken)
    {
        if (!OperatingSystem.IsWindows())
            throw new PlatformNotSupportedException("Source editing requires Windows.");
        if (!Path.IsPathFullyQualified(projectRoot) || projectRoot.StartsWith(@"\\", StringComparison.Ordinal))
            throw new IOException("The project root must be a local absolute drive path.");

        var root = Path.TrimEndingDirectorySeparator(Path.GetFullPath(projectRoot));
        var rootPrefix = Path.EndsInDirectorySeparator(root) ? root : root + Path.DirectorySeparatorChar;
        var target = Path.GetFullPath(file, rootPrefix);
        var volume = Path.GetPathRoot(root);
        if (volume is null || volume.Length != 3 || volume[1] != ':' || GetDriveType(volume) != 3 ||
            !target.StartsWith(rootPrefix, StringComparison.Ordinal))
            throw new IOException("The source must be inside a local fixed-drive project root.");
        ValidateComponents(root, volume);
        ValidateComponents(target, volume);

        var directories = new List<SafeFileHandle>();
        FileStream? source = null;
        try
        {
            var parent = Path.GetDirectoryName(target) ?? throw new IOException("The source parent is unavailable.");
            var current = volume;
            directories.Add(OpenChecked(current, true));
            foreach (var part in parent[volume.Length..].Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries))
            {
                current = Path.Combine(current, part);
                directories.Add(OpenChecked(current, true));
            }
            var sourceHandle = OpenChecked(target, false);
            try { source = new FileStream(sourceHandle, FileAccess.ReadWrite, 4096, false); }
            catch { sourceHandle.Dispose(); throw; }
            if (source.Length > int.MaxValue)
                throw new IOException("The source is too large.");
            var original = new byte[(int)source.Length];
            await source.ReadExactlyAsync(original, cancellationToken).ConfigureAwait(false);
            var hasBom = original.AsSpan().StartsWith(new byte[] { 0xef, 0xbb, 0xbf });
            if (original.AsSpan().IndexOf((byte)0) >= 0 ||
                original.AsSpan().StartsWith(new byte[] { 0xff, 0xfe }) ||
                original.AsSpan().StartsWith(new byte[] { 0xfe, 0xff }))
                throw new IOException("The source encoding is not supported.");
            string snapshot;
            try { snapshot = StrictUtf8.GetString(original, hasBom ? 3 : 0, original.Length - (hasBom ? 3 : 0)); }
            catch (DecoderFallbackException ex) { throw new IOException("The source encoding is not supported.", ex); }
            return new LegacyEncSource(target, snapshot, original, hasBom, source, directories);
        }
        catch
        {
            source?.Dispose();
            foreach (var directory in directories) directory.Dispose();
            throw;
        }
    }

    internal async Task WriteAsync(string editedSource, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(editedSource);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfUnavailable();
            if (_writeStarted || _restoreStarted) throw new InvalidOperationException("A source write or restoration has already been attempted.");
            var encoded = StrictUtf8.GetBytes(editedSource);
            cancellationToken.ThrowIfCancellationRequested();
            _writeStarted = true;
            var backupPath = TargetPath + "." + Guid.NewGuid().ToString("N") + ".enc-backup";
            var handle = CreateFile(backupPath, GenericRead | GenericWrite | Delete, 0, IntPtr.Zero,
                CreateNew, WriteThrough | OpenReparsePoint, IntPtr.Zero);
            if (handle.IsInvalid) { handle.Dispose(); throw NativeError("Could not create the source backup."); }
            BackupPath = backupPath;
            try { _backup = new FileStream(handle, FileAccess.ReadWrite, 4096, false); }
            catch { handle.Dispose(); throw; }
            await _backup.WriteAsync(_original, cancellationToken).ConfigureAwait(false);
            _backup.Flush(true);
            cancellationToken.ThrowIfCancellationRequested();
            _sourceWriteStarted = true;
            _source.Position = 0;
            if (_hasBom) await _source.WriteAsync(new byte[] { 0xef, 0xbb, 0xbf }, cancellationToken).ConfigureAwait(false);
            await _source.WriteAsync(encoded, cancellationToken).ConfigureAwait(false);
            _source.SetLength(_source.Position);
            _source.Flush(true);
            _writeSucceeded = true;
        }
        finally { _gate.Release(); }
    }

    internal async Task RestoreBeforeDispatchAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            ThrowIfUnavailable();
            _restoreStarted = true;
            _restored = false;
            if (!_sourceWriteStarted) { _restored = true; return; }
            _source.Position = 0;
            await _source.WriteAsync(_original).ConfigureAwait(false);
            _source.SetLength(_original.Length);
            _source.Flush(true);
            _restored = true;
        }
        finally { _gate.Release(); }
    }

    internal async Task CompleteAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            ThrowIfUnavailable();
            if ((!_writeSucceeded || _restoreStarted) && !_restored)
                throw new InvalidOperationException("The source write or restoration has not completed.");
            if (_backup is not null)
            {
                var disposition = new FileDispositionInfo { DeleteFile = true };
                if (!SetFileInformationByHandle(_backup.SafeFileHandle, 4, ref disposition, (uint)Marshal.SizeOf<FileDispositionInfo>()))
                    throw NativeError("Could not remove the source backup.");
                _backup.Dispose();
                _backup = null;
                BackupPath = null;
            }
            _completed = true;
        }
        finally { _gate.Release(); }
    }

    public async ValueTask DisposeAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_disposed) return;
            _disposed = true;
            _backup?.Dispose();
            _source.Dispose();
            foreach (var directory in _directories) directory.Dispose();
        }
        finally { _gate.Release(); }
    }

    private void ThrowIfUnavailable()
    {
        ObjectDisposedException.ThrowIf(_disposed, this);
        if (_completed) throw new InvalidOperationException("The source transaction is complete.");
    }

    private static void ValidateComponents(string path, string volume)
    {
        foreach (var part in path[volume.Length..].Split(Path.DirectorySeparatorChar, StringSplitOptions.RemoveEmptyEntries))
        {
            if (part.EndsWith(' ') || part.EndsWith('.') || part.Contains(':'))
                throw new IOException("The source path is not a normal Windows path.");
        }
    }

    private static SafeFileHandle OpenChecked(string path, bool directory)
    {
        var handle = CreateFile(path, directory ? 0 : GenericRead | GenericWrite,
            directory ? ShareReadWrite : 0, IntPtr.Zero, OpenExisting,
            OpenReparsePoint | (directory ? BackupSemantics : 0), IntPtr.Zero);
        if (handle.IsInvalid) { handle.Dispose(); throw NativeError("Could not securely open the source path."); }
        try
        {
            if (GetFileType(handle) != 1 || !GetFileInformationByHandle(handle, out var info) ||
                (info.Attributes & ReparsePoint) != 0 || ((info.Attributes & DirectoryAttribute) != 0) != directory ||
                (!directory && info.NumberOfLinks != 1))
                throw new IOException("The source path could not be verified as a regular local path.");
            var buffer = new StringBuilder(32768);
            var length = GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0);
            var expected = @"\\?\" + path;
            if (length == 0 || length >= buffer.Capacity ||
                !string.Equals(Path.TrimEndingDirectorySeparator(buffer.ToString()), Path.TrimEndingDirectorySeparator(expected), StringComparison.OrdinalIgnoreCase))
                throw new IOException("The source path could not be verified without aliases.");
            return handle;
        }
        catch { handle.Dispose(); throw; }
    }

    private static IOException NativeError(string message) => new(message, new Win32Exception(Marshal.GetLastWin32Error()));

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

    [StructLayout(LayoutKind.Sequential)]
    private struct FileDispositionInfo
    {
        [MarshalAs(UnmanagedType.U1)] internal bool DeleteFile;
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFile(string name, uint access, uint share, IntPtr securityAttributes, uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle file, out ByHandleFileInformation information);
    [DllImport("kernel32.dll", EntryPoint = "GetFinalPathNameByHandleW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(SafeFileHandle file, StringBuilder path, uint length, uint flags);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint GetFileType(SafeFileHandle file);
    [DllImport("kernel32.dll", EntryPoint = "GetDriveTypeW", CharSet = CharSet.Unicode)]
    private static extern uint GetDriveType(string root);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle file, int informationClass, ref FileDispositionInfo information, uint size);
}
