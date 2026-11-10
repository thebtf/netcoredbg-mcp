using System.ComponentModel;
using System.Globalization;
using System.Numerics;
using System.Reflection.PortableExecutable;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;

namespace NetCoreDbg.Mcp.Stateless;

internal sealed class NativeShimStageCleanupException(NativeShimStage stage, Exception inner)
    : IOException($"Could not clean debugger stage '{stage.RootPath}'; retain its owner and retry after consumers drain.", inner)
{
    internal NativeShimStage Stage { get; } = stage;
}

internal sealed class NativeShimStage
{
    private const int MaxEntries = 4096;
    private const int MaxFile = 128 * 1024 * 1024;
    private const int MaxShim = 32 * 1024 * 1024;
    private const long MaxTotal = 256L * 1024 * 1024;
    private const int MaxJson = 2 * 1024 * 1024;
    private sealed class OwnedFile(SafeFileHandle handle, byte[] expected)
    {
        internal SafeFileHandle Handle { get; set; } = handle;
        internal SafeFileHandle? Guard { get; set; }
        internal byte[] Expected { get; } = expected;
        internal bool IsCreationHandle { get; set; } = true;
        internal bool Complete { get; set; }
    }
    private readonly Dictionary<string, OwnedFile> _files = new(StringComparer.OrdinalIgnoreCase);
    private readonly Dictionary<string, SafeFileHandle> _directories = new(StringComparer.OrdinalIgnoreCase);
    private readonly List<SafeFileHandle> _ancestors;
    private bool _rootIdentityValidated;
    private bool _cleaned;

    internal static Action<string, int>? AfterWriteBlockForTest = null;
    internal static Action<FileStream, string, int>? AfterBufferedWriteForTest = null;
    internal static Action<string>? BeforeRootCreateForTest = null;
    internal static Action<SafeFileHandle>? BeforeIdentityQueryForTest = null;
    internal static Func<bool>? FailSameObjectMetadataForTest = null;
    internal static Action<string>? BeforeCleanupForTest = null;
    internal static Action<string>? BeforeDeleteForTest = null;

    internal string ExecutablePath { get; }
    internal string RootPath { get; }

    private NativeShimStage(string root, string executable, SafeFileHandle rootHandle, List<SafeFileHandle> ancestors)
    {
        _ancestors = ancestors;
        RootPath = root;
        ExecutablePath = Path.Combine(root, executable.Replace('/', Path.DirectorySeparatorChar));
        _directories.Add(root, rootHandle);
    }

    internal static NativeShimStage? Prepare(string debuggerPath, string programPath, CancellationToken cancellationToken)
    {
        if (!OperatingSystem.IsWindows()) return null;
        cancellationToken.ThrowIfCancellationRequested();
        NativeShimStage? stage = null;
        List<SafeFileHandle>? ancestors = null;
        try
        {
            var home = LegacyDebuggerDiscovery.GetHomeDirectory();
            if (string.IsNullOrWhiteSpace(home) || !Path.IsPathFullyQualified(home) || !Path.IsPathFullyQualified(debuggerPath)) return null;
            var managed = Path.Combine(Path.GetFullPath(home), ".netcoredbg-mcp");
            var install = Path.Combine(managed, "netcoredbg");
            const string executable = "netcoredbg/netcoredbg.exe";
            const string shim = "netcoredbg/dbgshim.dll";
            if (!string.Equals(Path.GetFullPath(debuggerPath), Path.Combine(install, "netcoredbg", "netcoredbg.exe"), StringComparison.OrdinalIgnoreCase)) return null;
            ancestors = PinAncestors(managed);
            RequireAncestors(install);
            var receiptPath = Path.Combine(install, ".native-debugger-manifest.json");
            if (!Exists(receiptPath)) return null;
            var receiptBytes = ReadFile(receiptPath, MaxJson, cancellationToken);
            using var receipt = JsonDocument.Parse(receiptBytes);
            var root = receipt.RootElement;
            if (root.GetProperty("schema").GetString() != "netcoredbg-mcp.native-debugger-install/1" ||
                root.GetProperty("asset").GetString() != "netcoredbg-win64.zip" || !IsHash(root.GetProperty("archiveSha256").GetString()))
                throw new InvalidDataException("Invalid install receipt.");
            var manifest = new Dictionary<string, (long Size, string Hash)>(StringComparer.OrdinalIgnoreCase);
            long total = receiptBytes.Length;
            foreach (var entry in root.GetProperty("files").EnumerateArray())
            {
                cancellationToken.ThrowIfCancellationRequested();
                var path = entry.GetProperty("path").GetString();
                var size = entry.GetProperty("size").GetInt64();
                var hash = entry.GetProperty("sha256").GetString();
                if (!SafeRelative(path) || size < 0 || size > MaxFile || !IsHash(hash) ||
                    !manifest.TryAdd(path!, (size, hash!)) || manifest.Count > MaxEntries || (total += size) > MaxTotal)
                    throw new InvalidDataException("Unsafe or excessive install receipt.");
            }
            if (!manifest.ContainsKey(executable) || !manifest.ContainsKey(shim) || manifest.ContainsKey(".native-debugger-manifest.json"))
                throw new InvalidDataException("Unsupported install layout.");
            var major = TargetMajor(programPath, cancellationToken);
            if (major is null) return null;
            var cache = Path.Combine(managed, "dbgshim");
            if (!Exists(cache)) return null;
            RequireAncestors(cache);
            var content = new Dictionary<string, byte[]>(StringComparer.OrdinalIgnoreCase)
            {
                [".native-debugger-manifest.json"] = receiptBytes,
            };
            var visited = 0;
            var actualTotal = (long)receiptBytes.Length;
            ScanTree(install, install, manifest, content, ref visited, ref actualTotal, cancellationToken);
            if (content.Count != manifest.Count + 1) throw new InvalidDataException("Install files do not match receipt.");
            if (!IsX64Pe(content[executable])) return null;
            var replacement = SelectShim(cache, major.Value, cancellationToken);
            if (replacement is null || content[shim].AsSpan().SequenceEqual(replacement)) return null;
            actualTotal = actualTotal - content[shim].Length + replacement.Length;
            if (actualTotal > MaxTotal) throw new InvalidDataException("Stage exceeds size limit.");
            content[shim] = replacement;
            cancellationToken.ThrowIfCancellationRequested();
            _ = GetStableIdentity(ancestors[^1]);
            BeforeRootCreateForTest?.Invoke(managed);
            var stagePath = Path.Combine(managed, ".native-shim-stage-" + Guid.NewGuid().ToString("N"));
            var rootHandle = CreateOwned(ancestors[^1], Path.GetFileName(stagePath), directory: true);
            stage = new NativeShimStage(stagePath, executable, rootHandle, ancestors);
            ancestors = null;
            _ = GetStableIdentity(rootHandle);
            stage._rootIdentityValidated = true;
            foreach (var item in content)
            {
                cancellationToken.ThrowIfCancellationRequested();
                var destination = Path.Combine(stagePath, item.Key.Replace('/', Path.DirectorySeparatorChar));
                stage.MakeParents(Path.GetDirectoryName(destination)!);
                var parent = stage._directories[Path.GetDirectoryName(destination)!];
                var writer = CreateOwned(parent, Path.GetFileName(destination), directory: false);
                var owned = new OwnedFile(writer, item.Value);
                stage._files.Add(destination, owned);
                Exception? writeFailure = null;
                try
                {
                    var identity = OpenIdentity(destination, writer, directory: false);
                    owned.Handle = identity;
                    owned.IsCreationHandle = false;
                    using var output = new FileStream(writer, FileAccess.Write);
                    for (var offset = 0; offset < item.Value.Length;)
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        var length = Math.Min(81920, item.Value.Length - offset);
                        output.Write(item.Value, offset, length);
                        offset += length;
                        AfterWriteBlockForTest?.Invoke(destination, offset);
                        AfterBufferedWriteForTest?.Invoke(output, destination, offset);
                    }
                }
                catch (Exception exception) { writeFailure = exception; throw; }
                finally
                {
                    // If identity acquisition failed, keep the creation handle as the owner.
                    if (!ReferenceEquals(owned.Handle, writer)) writer.Dispose();
                    owned.Complete = writeFailure is null;
                    try
                    {
                        if (!ReferenceEquals(owned.Handle, writer))
                        {
                            owned.Guard = OpenWriteGuard(destination, owned.Handle);
                            using var read = OpenVerifiedRead(destination, owned.Handle);
                            RequireExpectedBytes(read, owned.Expected, requireComplete: owned.Complete);
                        }
                    }
                    catch when (writeFailure is not null) { /* Cleanup retries and retains the identity on failure. */ }
                }
            }
            cancellationToken.ThrowIfCancellationRequested();
            return stage;
        }
        catch (Exception exception)
        {
            if (stage is not null)
            {
                try { stage.Cleanup(); }
                catch (Exception cleanup) { throw new NativeShimStageCleanupException(stage, new AggregateException(exception, cleanup)); }
            }
            if (exception is OperationCanceledException) throw;
            if (exception is IOException or InvalidDataException or UnauthorizedAccessException or ArgumentException or NotSupportedException or System.Security.SecurityException or JsonException or InvalidOperationException or KeyNotFoundException or FormatException or OverflowException)
            {
                Console.Error.WriteLine("Native debugger shim staging skipped: installation, cache, or metadata is unsafe or unavailable.");
                return null;
            }
            throw;
        }
        finally
        {
            if (ancestors is not null)
                foreach (var handle in ancestors.AsEnumerable().Reverse()) handle.Dispose();
        }
    }

    internal void Cleanup()
    {
        if (_cleaned) return;
        BeforeCleanupForTest?.Invoke(RootPath);
        if (!_rootIdentityValidated)
        {
            // Before root identity succeeds no children are created by this owner.
            // Directory disposition is nonrecursive and fails if unknown entries exist.
            if (_files.Count != 0 || _directories.Count != 1)
                throw new IOException("Unverified stage contains owned children.");
            var root = _directories[RootPath];
            DeleteOwned(root);
            root.Dispose();
            _directories.Remove(RootPath);
            foreach (var ancestor in _ancestors.AsEnumerable().Reverse()) ancestor.Dispose();
            _ancestors.Clear();
            _cleaned = true;
            return;
        }
        RequireAncestors(RootPath);
        var foundFiles = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var foundDirectories = new HashSet<string>(StringComparer.OrdinalIgnoreCase) { RootPath };
        RequireIdentity(RootPath, _directories[RootPath], directory: true);
        var pending = new Stack<string>();
        pending.Push(RootPath);
        var count = 0;
        while (pending.TryPop(out var directory))
        {
            RequireIdentity(directory, _directories[directory], directory: true);
            foreach (var path in Directory.EnumerateFileSystemEntries(directory))
            {
                if (++count > MaxEntries * 2) throw new IOException("Stage contains excessive entries.");
                var attributes = File.GetAttributes(path);
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("Stage contains a reparse point.");
                if ((attributes & FileAttributes.Directory) != 0)
                {
                    if (!_directories.TryGetValue(path, out var handle)) throw new IOException("Stage contains an unknown directory.");
                    RequireIdentity(path, handle, directory: true);
                    foundDirectories.Add(path);
                    pending.Push(path);
                }
                else
                {
                    if (!_files.TryGetValue(path, out var owned)) throw new IOException("Stage contains an unknown file.");
                    EnsureIdentityAndGuard(path, owned);
                    using var read = OpenVerifiedRead(path, owned.Handle);
                    RequireExpectedBytes(read, owned.Expected, requireComplete: owned.Complete);
                    foundFiles.Add(path);
                }
            }
        }
        foreach (var path in foundFiles)
        {
            var owned = _files[path];
            owned.Guard?.Dispose();
            owned.Guard = null;
            BeforeDeleteForTest?.Invoke(path);
            using (var deletion = OpenForDeletion(path, owned.Handle))
            {
                RequireExpectedBytes(deletion, owned.Expected, requireComplete: owned.Complete);
                DeleteOwned(deletion);
            }
            owned.Handle.Dispose();
            _files.Remove(path);
        }
        foreach (var directory in foundDirectories.OrderByDescending(static value => value.Length))
        {
            var handle = _directories[directory];
            DeleteOwned(handle);
            handle.Dispose();
            _directories.Remove(directory);
        }
        foreach (var ancestor in _ancestors.AsEnumerable().Reverse()) ancestor.Dispose();
        _ancestors.Clear();
        _cleaned = true;
    }

    private void MakeParents(string directory)
    {
        if (_directories.ContainsKey(directory)) return;
        var parent = Path.GetDirectoryName(directory) ?? throw new IOException("Invalid stage parent.");
        MakeParents(parent);
        RequireIdentity(parent, _directories[parent], directory: true);
        var handle = CreateOwned(_directories[parent], Path.GetFileName(directory), directory: true);
        _directories.Add(directory, handle);
        _ = GetStableIdentity(handle);
    }

    private static void EnsureIdentityAndGuard(string path, OwnedFile file)
    {
        if (file.Guard is not null) return;
        // A creation handle can remain only if opening its attribute-only identity failed.
        var identity = OpenIdentity(path, file.Handle, directory: false);
        if (file.IsCreationHandle)
        {
            file.Handle.Dispose();
            file.Handle = identity;
            file.IsCreationHandle = false;
        }
        else identity.Dispose();
        file.Guard = OpenWriteGuard(path, file.Handle);
    }

    private static void RequireExpectedBytes(SafeFileHandle handle, byte[] expected, bool requireComplete)
    {
        var length = RandomAccess.GetLength(handle);
        if (length > expected.LongLength || (requireComplete && length != expected.LongLength))
            throw new IOException("Owned stage file changed.");
        var buffer = new byte[81920];
        for (long offset = 0; offset < length;)
        {
            var read = RandomAccess.Read(handle, buffer.AsSpan(0, (int)Math.Min(buffer.Length, length - offset)), offset);
            if (read == 0 || !buffer.AsSpan(0, read).SequenceEqual(expected.AsSpan((int)offset, read)))
                throw new IOException("Owned stage file changed.");
            offset += read;
        }
    }

    private static void ScanTree(string root, string directory, Dictionary<string, (long Size, string Hash)> manifest,
        Dictionary<string, byte[]> content, ref int count, ref long total, CancellationToken token)
    {
        RequireAncestors(directory);
        if (Path.GetRelativePath(root, directory).Split(Path.DirectorySeparatorChar).Length > 64)
            throw new InvalidDataException("Install directory depth exceeds limit.");
        var hadEntry = false;
        foreach (var path in Directory.EnumerateFileSystemEntries(directory))
        {
            token.ThrowIfCancellationRequested();
            hadEntry = true;
            if (++count > MaxEntries * 2) throw new InvalidDataException("Install has excessive entries.");
            var attributes = File.GetAttributes(path);
            if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Install contains a reparse point.");
            if ((attributes & FileAttributes.Directory) != 0) { ScanTree(root, path, manifest, content, ref count, ref total, token); continue; }
            var relative = Path.GetRelativePath(root, path).Replace('\\', '/');
            if (relative.Equals(".native-debugger-manifest.json", StringComparison.OrdinalIgnoreCase)) continue;
            if (!manifest.TryGetValue(relative, out var expected)) throw new InvalidDataException("Install has unlisted files.");
            var bytes = ReadFile(path, relative.Equals("netcoredbg/dbgshim.dll", StringComparison.OrdinalIgnoreCase) ? MaxShim : MaxFile, token);
            if ((total += bytes.Length) > MaxTotal) throw new InvalidDataException("Install exceeds size limit.");
            if (!relative.Equals("netcoredbg/dbgshim.dll", StringComparison.OrdinalIgnoreCase) &&
                (bytes.LongLength != expected.Size || !string.Equals(Convert.ToHexString(SHA256.HashData(bytes)), expected.Hash, StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("Install file differs from receipt.");
            if (!content.TryAdd(relative, bytes)) throw new InvalidDataException("Duplicate install file.");
        }
        if (!hadEntry) throw new InvalidDataException("Empty install directories are unsupported.");
    }

    private static int? TargetMajor(string program, CancellationToken token)
    {
        var path = Path.ChangeExtension(Path.GetFullPath(program), ".runtimeconfig.json");
        if (!Exists(path)) return null;
        var bytes = ReadFile(path, MaxJson, token);
        using var document = JsonDocument.Parse(bytes);
        var options = document.RootElement.GetProperty("runtimeOptions");
        JsonElement framework;
        if (!options.TryGetProperty("framework", out framework))
        {
            if (!options.TryGetProperty("frameworks", out var frameworks) || frameworks.ValueKind != JsonValueKind.Array) return null;
            framework = default;
            foreach (var item in frameworks.EnumerateArray())
                if (item.TryGetProperty("name", out var name) && name.GetString() == "Microsoft.NETCore.App") { framework = item; break; }
            if (framework.ValueKind == JsonValueKind.Undefined) return null;
        }
        if (framework.ValueKind != JsonValueKind.Object || !framework.TryGetProperty("name", out var frameworkName) ||
            frameworkName.ValueKind != JsonValueKind.String || frameworkName.GetString() != "Microsoft.NETCore.App" ||
            !framework.TryGetProperty("version", out var version) || version.ValueKind != JsonValueKind.String) return null;
        var text = version.GetString()!;
        var suffix = text.IndexOfAny(['-', '+']);
        var numeric = suffix < 0 ? text : text[..suffix];
        if (suffix >= 0 && (suffix == text.Length - 1 || !text[(suffix + 1)..].All(static c => char.IsAsciiLetterOrDigit(c) || c is '.' or '-' or '+'))) return null;
        var parts = numeric.Split('.');
        if (parts.Length is < 3 or > 4 || parts.Any(static part => part.Length == 0 || !part.All(char.IsAsciiDigit) ||
            !int.TryParse(part, NumberStyles.None, CultureInfo.InvariantCulture, out _))) return null;
        return int.Parse(parts[0], NumberStyles.None, CultureInfo.InvariantCulture);
    }

    private static byte[]? SelectShim(string cache, int major, CancellationToken token)
    {
        byte[]? best = null;
        BigInteger[]? bestVersion = null;
        var count = 0;
        long scannedBytes = 0;
        foreach (var directory in Directory.EnumerateFileSystemEntries(cache))
        {
            token.ThrowIfCancellationRequested();
            if (++count > 1024) throw new InvalidDataException("Cache has excessive entries.");
            var parts = Path.GetFileName(directory).Split('.');
            if (parts.Length is < 3 or > 4 || parts.Any(static p => p.Length == 0 || p.Length > 16 || !p.All(char.IsAsciiDigit))) continue;
            var numbers = parts.Select(static p => BigInteger.Parse(p, CultureInfo.InvariantCulture)).ToArray();
            if (numbers[0] != major) continue;
            RequireDirectory(directory);
            var path = Path.Combine(directory, "dbgshim.dll");
            if (!Exists(path)) continue;
            if (bestVersion is not null && CompareVersion(numbers, bestVersion) <= 0) continue;
            var bytes = ReadFile(path, MaxShim, token);
            if ((scannedBytes += bytes.Length) > 512L * 1024 * 1024) throw new InvalidDataException("Cache scan exceeds size limit.");
            if (!IsX64Pe(bytes)) continue;
            best = bytes;
            bestVersion = numbers;
        }
        return best;
    }

    private static bool IsX64Pe(byte[] bytes)
    {
        try
        {
            using var stream = new MemoryStream(bytes, writable: false);
            using var reader = new PEReader(stream);
            return reader.PEHeaders.CoffHeader.Machine == System.Reflection.PortableExecutable.Machine.Amd64 &&
                reader.PEHeaders.PEHeader?.Magic == PEMagic.PE32Plus;
        }
        catch (BadImageFormatException) { return false; }
        catch (IOException) { return false; }
    }

    private static int CompareVersion(BigInteger[] left, BigInteger[] right)
    {
        for (var i = 0; i < Math.Min(left.Length, right.Length); i++)
        {
            var result = left[i].CompareTo(right[i]);
            if (result != 0) return result;
        }
        return left.Length.CompareTo(right.Length);
    }

    private static bool SafeRelative(string? path) => !string.IsNullOrEmpty(path) && path.Length <= 1024 && !path.Contains('\\') &&
        path.Split('/').All(static part => part.Length > 0 && part != "." && part != ".." && !part.EndsWith(' ') && !part.EndsWith('.') &&
            part.IndexOfAny(Path.GetInvalidFileNameChars()) < 0 && !part.Contains(':'));
    private static bool IsHash(string? value) => value is { Length: 64 } && value.All(Uri.IsHexDigit);
    private static bool Exists(string path)
    {
        try { _ = File.GetAttributes(path); return true; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }
    private static void RequireAncestors(string path)
    {
        for (var current = Path.GetFullPath(path); current is not null; current = Path.GetDirectoryName(current)) RequireDirectory(current);
    }
    private static void RequireDirectory(string path)
    {
        var attributes = File.GetAttributes(path);
        if ((attributes & FileAttributes.Directory) == 0 || (attributes & FileAttributes.ReparsePoint) != 0) throw new IOException("Expected a non-reparse directory.");
    }
    private static byte[] ReadFile(string path, int maximum, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        using var stream = NativeDbgshimCache.OpenRegularFile(path);
        if (stream.Length > maximum) throw new InvalidDataException("File exceeds size limit.");
        var bytes = new byte[(int)stream.Length];
        var offset = 0;
        while (offset < bytes.Length)
        {
            token.ThrowIfCancellationRequested();
            var read = stream.Read(bytes, offset, Math.Min(81920, bytes.Length - offset));
            if (read == 0) throw new EndOfStreamException();
            offset += read;
        }
        if (stream.ReadByte() != -1) throw new IOException("File changed while reading.");
        return bytes;
    }
    internal static List<SafeFileHandle> PinAncestors(string path)
    {
        var handles = new List<SafeFileHandle>();
        try
        {
            var full = Path.GetFullPath(path);
            var root = Path.GetPathRoot(full) ?? throw new IOException("Managed path has no root.");
            var rootHandle = CreateFileWindows(NativeDbgshimCache.ToExtendedPath(root), 0x80 | 0x20, 1 | 2,
                IntPtr.Zero, 3, 0x00200000 | 0x02000000, IntPtr.Zero);
            if (rootHandle.IsInvalid)
            {
                var error = Marshal.GetLastPInvokeError();
                rootHandle.Dispose();
                throw new IOException("Could not pin managed root.", new Win32Exception(error));
            }
            handles.Add(rootHandle);
            RequireDirectoryHandle(rootHandle);
            var components = full[root.Length..].Split([Path.DirectorySeparatorChar, Path.AltDirectorySeparatorChar], StringSplitOptions.RemoveEmptyEntries);
            if (components.Length > 128) throw new IOException("Managed directory depth exceeds limit.");
            foreach (var component in components)
            {
                var child = OpenRelative(handles[^1], component, directory: true, create: false);
                handles.Add(child);
                RequireDirectoryHandle(child);
            }
            return handles;
        }
        catch
        {
            foreach (var handle in handles.AsEnumerable().Reverse()) handle.Dispose();
            throw;
        }
    }

    internal static void RequireDirectoryHandle(SafeFileHandle handle)
    {
        if (!GetFileInformationByHandle(handle, out var info) || (info.Attributes & 0x400) != 0 || (info.Attributes & 0x10) == 0)
            throw new IOException("Expected a pinned non-reparse directory.");
    }

    // FILE_CREATE returns the exact object handle, relative to the pinned parent.
    private static SafeFileHandle CreateOwned(SafeFileHandle parent, string name, bool directory) =>
        OpenRelative(parent, name, directory, create: true);

    internal static SafeFileHandle OpenExistingDirectory(SafeFileHandle parent, string name) =>
        OpenRelative(parent, name, directory: true, create: false);

    internal static SafeFileHandle CreateReceiptTemporary(SafeFileHandle parent, string name) =>
        OpenRelative(parent, name, directory: false, create: true, deleteAccess: true);

    internal static void EnsureStableIdentity(SafeFileHandle handle) => _ = GetStableIdentity(handle);

    private static SafeFileHandle OpenRelative(SafeFileHandle parent, string component, bool directory, bool create, bool deleteAccess = false)
    {
        if (string.IsNullOrEmpty(component) || component is "." or ".." || component.IndexOfAny(['\\', '/', ':']) >= 0 || component.Length > 32766)
            throw new IOException("Invalid relative staging component.");
        var text = Marshal.StringToHGlobalUni(component);
        var namePointer = Marshal.AllocHGlobal(Marshal.SizeOf<UnicodeString>());
        try
        {
            var name = new UnicodeString { Length = checked((ushort)(component.Length * 2)), MaximumLength = checked((ushort)((component.Length + 1) * 2)), Buffer = text };
            Marshal.StructureToPtr(name, namePointer, false);
            var attributes = new ObjectAttributes { Length = Marshal.SizeOf<ObjectAttributes>(), RootDirectory = parent.DangerousGetHandle(), ObjectName = namePointer, Attributes = 0x40 | 0x1000 };
            // Files are written through the creation handle; no DELETE access is retained.
            var access = directory ? (create ? 0x00010000u : 0u) | 0x00100000 | 0x80 | 0x20 | 0x01
                : 0x40000000u | 0x00100000 | 0x80 | (deleteAccess ? 0x00010000u : 0u);
            const uint share = 1 | 2; // Prevent replacement until the creation handle is closed.
            var options = 0x00200000u | 0x20u | (directory ? 0x01u : 0x40u);
            var status = NtCreateFile(out var handle, access, ref attributes, out _, IntPtr.Zero, 0, share, create ? 2u : 1u, options, IntPtr.Zero, 0);
            if (status < 0)
            {
                handle?.Dispose();
                throw new IOException("Could not open a pinned staging object.", new Win32Exception((int)RtlNtStatusToDosError(status)));
            }
            return handle;
        }
        finally { Marshal.FreeHGlobal(namePointer); Marshal.FreeHGlobal(text); }
    }

    private static SafeFileHandle OpenIdentity(string path, SafeFileHandle owner, bool directory)
    {
        var identity = OpenPath(path, 0x80, 1 | 2 | 4, directory);
        try { RequireSameHandle(owner, identity, directory); return identity; }
        catch { identity.Dispose(); throw; }
    }

    private static SafeFileHandle OpenWriteGuard(string path, SafeFileHandle owner)
    {
        // Attribute-only access permits ordinary FILE_SHARE_READ opens, while
        // this handle's sharing denies both new writers and name replacement.
        var guard = OpenPath(path, 0x80, 1, directory: false);
        try { RequireSameHandle(owner, guard, false); return guard; }
        catch { guard.Dispose(); throw; }
    }

    private static SafeFileHandle OpenVerifiedRead(string path, SafeFileHandle owner)
    {
        var read = OpenPath(path, 0x80000000, 1 | 2 | 4, directory: false);
        try { RequireSameHandle(owner, read, false); return read; }
        catch { read.Dispose(); throw; }
    }

    private static SafeFileHandle OpenForDeletion(string path, SafeFileHandle owner)
    {
        // No write/delete sharing: identity and bytes remain stable through disposition.
        var deletion = OpenPath(path, 0x80000000 | 0x00010000, 1, directory: false);
        try { RequireSameHandle(owner, deletion, false); return deletion; }
        catch { deletion.Dispose(); throw; }
    }

    private static SafeFileHandle OpenPath(string path, uint access, uint share, bool directory)
    {
        var handle = CreateFileWindows(NativeDbgshimCache.ToExtendedPath(path), access, share,
            IntPtr.Zero, 3, 0x00200000 | (directory ? 0x02000000u : 0), IntPtr.Zero);
        if (handle.IsInvalid)
        {
            var error = Marshal.GetLastPInvokeError();
            handle.Dispose();
            throw new IOException("Could not open a staging object.", new Win32Exception(error));
        }
        return handle;
    }

    private static void RequireIdentity(string path, SafeFileHandle owner, bool directory)
    {
        var probe = CreateFileWindows(NativeDbgshimCache.ToExtendedPath(path), 0x80, 1 | 2 | 4,
            IntPtr.Zero, 3, 0x00200000 | (directory ? 0x02000000u : 0), IntPtr.Zero);
        if (probe.IsInvalid)
        {
            var error = Marshal.GetLastPInvokeError();
            probe.Dispose();
            throw new IOException("Could not inspect a staging object.", new Win32Exception(error));
        }
        using (probe) RequireSameHandle(owner, probe, directory);
    }

    internal static bool? IsSameObjectAtPath(SafeFileHandle owner, string path)
    {
        try
        {
            using var probe = OpenPath(path, 0x80, 1 | 2 | 4, directory: false);
            if ((FailSameObjectMetadataForTest?.Invoke() ?? false) || !GetFileInformationByHandle(probe, out var attributes)) return null;
            if ((attributes.Attributes & (0x400 | 0x10)) != 0) return null;
            var left = GetStableIdentity(owner);
            var right = GetStableIdentity(probe);
            return left.VolumeSerialNumber == right.VolumeSerialNumber && left.FileIdLow == right.FileIdLow && left.FileIdHigh == right.FileIdHigh;
        }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or ArgumentException or System.Security.SecurityException)
        {
            return null;
        }
    }

    private static void RequireSameHandle(SafeFileHandle owner, SafeFileHandle probe, bool directory)
    {
        if (!GetFileInformationByHandle(probe, out var info) ||
            (info.Attributes & 0x400) != 0 || ((info.Attributes & 0x10) != 0) != directory)
            throw new IOException("Staging object type changed.");
        var left = GetStableIdentity(owner);
        var right = GetStableIdentity(probe);
        if (left.VolumeSerialNumber != right.VolumeSerialNumber || left.FileIdLow != right.FileIdLow || left.FileIdHigh != right.FileIdHigh)
            throw new IOException("Staging object identity changed.");
    }

    private static FileIdInformation GetStableIdentity(SafeFileHandle handle)
    {
        BeforeIdentityQueryForTest?.Invoke(handle);
        if (!GetFileInformationByHandleEx(handle, 18, out var info, (uint)Marshal.SizeOf<FileIdInformation>()) ||
            info.VolumeSerialNumber == 0 || (info.FileIdLow == 0 && info.FileIdHigh == 0))
            throw new IOException("Filesystem does not supply a usable staging object identity.");
        return info;
    }

    internal static void DeleteOwned(SafeFileHandle handle)
    {
        var disposition = new FileDispositionInformation { DeleteFile = true };
        if (!SetFileInformationByHandle(handle, 4, ref disposition, (uint)Marshal.SizeOf<FileDispositionInformation>()))
            throw new IOException("Could not delete an owned staging object.", new Win32Exception(Marshal.GetLastPInvokeError()));
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct UnicodeString { internal ushort Length; internal ushort MaximumLength; internal IntPtr Buffer; }
    [StructLayout(LayoutKind.Sequential)]
    private struct ObjectAttributes
    {
        internal int Length;
        internal IntPtr RootDirectory;
        internal IntPtr ObjectName;
        internal uint Attributes;
        internal IntPtr SecurityDescriptor;
        internal IntPtr SecurityQualityOfService;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock { internal IntPtr Status; internal UIntPtr Information; }
    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        internal uint Attributes;
        internal System.Runtime.InteropServices.ComTypes.FILETIME CreationTime, LastAccessTime, LastWriteTime;
        internal uint VolumeSerialNumber, FileSizeHigh, FileSizeLow, NumberOfLinks, FileIndexHigh, FileIndexLow;
    }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileIdInformation { internal ulong VolumeSerialNumber; internal ulong FileIdLow; internal ulong FileIdHigh; }
    [StructLayout(LayoutKind.Sequential)]
    private struct FileDispositionInformation { [MarshalAs(UnmanagedType.U1)] internal bool DeleteFile; }

    [DllImport("ntdll.dll")]
    private static extern int NtCreateFile(out SafeFileHandle file, uint access, ref ObjectAttributes attributes,
        out IoStatusBlock ioStatus, IntPtr allocationSize, uint fileAttributes, uint shareAccess, uint disposition,
        uint options, IntPtr extendedAttributes, uint extendedAttributesLength);
    [DllImport("ntdll.dll")]
    private static extern uint RtlNtStatusToDosError(int status);
    [DllImport("kernel32.dll", EntryPoint = "CreateFileW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern SafeFileHandle CreateFileWindows(string path, uint access, uint share, IntPtr security,
        uint disposition, uint flags, IntPtr template);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandle(SafeFileHandle file, out ByHandleFileInformation information);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetFileInformationByHandleEx(SafeFileHandle file, int informationClass,
        out FileIdInformation information, uint size);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool SetFileInformationByHandle(SafeFileHandle file, int informationClass,
        ref FileDispositionInformation information, uint size);
}
