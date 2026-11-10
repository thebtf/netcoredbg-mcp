using System.ComponentModel;
using System.IO.Compression;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;

namespace NetCoreDbg.Mcp.Stateless;

internal enum LegacyReceiptOutcome { NotEligible, Existing, Verified, Unavailable }

internal static class NativeLegacyReceiptMigration
{
    private const string ReceiptName = ".native-debugger-manifest.json";
    private const string AssetName = "netcoredbg-win64.zip";
    private const int MaxConfig = 64 * 1024;
    private const int MaxManifest = 2 * 1024 * 1024;
    private const int MaxArchive = 256 * 1024 * 1024;
    private const int MaxFile = 128 * 1024 * 1024;
    private const int MaxShim = 32 * 1024 * 1024;
    private const long MaxTotal = 256L * 1024 * 1024;
    private const int MaxEntries = 4096;
    private const string ShimPath = "netcoredbg/dbgshim.dll";
    private const string Executable = "netcoredbg/netcoredbg.exe";

    internal static Func<SafeFileHandle, SafeFileHandle, string, int>? RenameForTest = null;
    internal static Func<string, bool>? OccupiedForTest = null;

    private sealed record Entry(long Size, string Hash);

    // Production callers must supply CreateHttpClient(): an injected HttpClient must
    // likewise disable automatic redirects so every Location is checked before use.
    internal static async Task<LegacyReceiptOutcome> TryUpgradeAsync(HttpClient client, string home, string debuggerPath, CancellationToken token)
    {
        token.ThrowIfCancellationRequested();
        if (!OperatingSystem.IsWindows()) return LegacyReceiptOutcome.NotEligible;
        var pins = new List<SafeFileHandle>();
        var files = new List<FileStream>();
        SafeFileHandle? temporary = null;
        string? temporaryPath = null;
        string? receiptPath = null;
        var published = false;
        var uncertainRename = false;
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(token);
        deadline.CancelAfter(client.Timeout);
        var ct = deadline.Token;
        try
        {
            if (string.IsNullOrWhiteSpace(home) || !Path.IsPathFullyQualified(home) || !Path.IsPathFullyQualified(debuggerPath))
                return LegacyReceiptOutcome.NotEligible;
            var managed = Path.Combine(Path.GetFullPath(home), ".netcoredbg-mcp");
            var install = Path.Combine(managed, "netcoredbg");
            if (!string.Equals(Path.GetFullPath(debuggerPath), Path.Combine(install, "netcoredbg", "netcoredbg.exe"), StringComparison.OrdinalIgnoreCase))
                return LegacyReceiptOutcome.NotEligible;
            pins.AddRange(NativeShimStage.PinAncestors(install));
            var installHandle = pins[^1];
            NativeShimStage.EnsureStableIdentity(installHandle);
            receiptPath = Path.Combine(install, ReceiptName);
            if (Occupied(receiptPath)) return LegacyReceiptOutcome.Existing;
            var configPath = Path.Combine(managed, "config.json");
            if (!Occupied(configPath)) return LegacyReceiptOutcome.NotEligible;
            var config = NativeDbgshimCache.OpenRegularFile(configPath);
            files.Add(config);
            var configBytes = await ReadBoundedAsync(config, MaxConfig, ct);
            using var configDocument = JsonDocument.Parse(configBytes);
            var setting = configDocument.RootElement.GetProperty("netcoredbg");
            var source = setting.GetProperty("source").GetString();
            var configured = setting.GetProperty("path").GetString();
            var tag = setting.GetProperty("version").GetString();
            if (source != "github" || !SafeTag(tag) || string.IsNullOrEmpty(configured) || !Path.IsPathFullyQualified(configured) ||
                !string.Equals(Path.GetFullPath(configured), Path.GetFullPath(debuggerPath), StringComparison.OrdinalIgnoreCase))
                return LegacyReceiptOutcome.NotEligible;
            ct.ThrowIfCancellationRequested();
            var api = new Uri("https://api.github.com/repos/Samsung/netcoredbg/releases/tags/" + tag);
            using var response = await NativeDebuggerInstaller.GetAsync(client, api, ct);
            var metadata = await NativeDebuggerInstaller.ReadBoundedAsync(response.Content, MaxManifest, ct);
            using var document = JsonDocument.Parse(metadata);
            if (document.RootElement.GetProperty("tag_name").GetString() != tag) return LegacyReceiptOutcome.NotEligible;
            var (size, expectedHash, assetUri) = NativeDebuggerInstaller.ParseManifest(metadata, AssetName);
            var expectedUrl = "https://github.com/Samsung/netcoredbg/releases/download/" + tag + "/" + AssetName;
            if (!string.Equals(assetUri.AbsoluteUri, expectedUrl, StringComparison.Ordinal)) return LegacyReceiptOutcome.NotEligible;
            using var archiveResponse = await NativeDebuggerInstaller.GetAsync(client, assetUri, ct);
            if (archiveResponse.Content.Headers.ContentLength is long length && length != size) return LegacyReceiptOutcome.NotEligible;
            var archive = await NativeDebuggerInstaller.ReadBoundedAsync(archiveResponse.Content, Math.Min(size, MaxArchive), ct);
            if (archive.LongLength != size || !CryptographicOperations.FixedTimeEquals(SHA256.HashData(archive), expectedHash))
                return LegacyReceiptOutcome.NotEligible;
            var expected = await ReadArchiveAsync(install, archive, ct);
            if (!expected.ContainsKey(Executable) || !expected.ContainsKey(ShimPath)) return LegacyReceiptOutcome.NotEligible;
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            var expectedDirectories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var path in expected.Keys)
            {
                var index = path.LastIndexOf('/');
                while (index > 0)
                {
                    expectedDirectories.Add(path[..index]);
                    index = path.LastIndexOf('/', index - 1);
                }
            }
            var count = 0;
            var scan = new Stack<(string Path, SafeFileHandle Handle, int Depth)>();
            scan.Push((install, installHandle, 0));
            while (scan.TryPop(out var directory))
            {
                ct.ThrowIfCancellationRequested();
                foreach (var path in Directory.EnumerateFileSystemEntries(directory.Path))
                {
                    ct.ThrowIfCancellationRequested();
                    if (++count > MaxEntries * 2) throw new InvalidDataException("Install contains excessive entries.");
                    var relative = Path.GetRelativePath(install, path).Replace('\\', '/');
                    var attributes = File.GetAttributes(path);
                    if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Install contains a reparse point.");
                    if ((attributes & FileAttributes.Directory) != 0)
                    {
                        if (!expectedDirectories.Contains(relative)) throw new InvalidDataException("Install contains an unlisted directory.");
                        if (directory.Depth >= 64) throw new InvalidDataException("Install directory depth exceeds limit.");
                        var nested = NativeShimStage.OpenExistingDirectory(directory.Handle, Path.GetFileName(path));
                        pins.Add(nested);
                        NativeShimStage.RequireDirectoryHandle(nested);
                        NativeShimStage.EnsureStableIdentity(nested);
                        scan.Push((path, nested, directory.Depth + 1));
                    }
                    else
                    {
                        if (relative.Equals(ReceiptName, StringComparison.OrdinalIgnoreCase)) return LegacyReceiptOutcome.Existing;
                        if (!expected.TryGetValue(relative, out var entry) || !seen.Add(relative))
                            throw new InvalidDataException("Install contains an unlisted file.");
                        var input = NativeDbgshimCache.OpenRegularFile(path);
                        files.Add(input);
                        var max = relative.Equals(ShimPath, StringComparison.OrdinalIgnoreCase) ? MaxShim : MaxFile;
                        if (input.Length > max) throw new InvalidDataException("Install file exceeds size limit.");
                        if (!relative.Equals(ShimPath, StringComparison.OrdinalIgnoreCase))
                        {
                            if (input.Length != entry.Size) throw new InvalidDataException("Install file size differs from archive.");
                            var actual = await HashStreamAsync(input, entry.Size, ct);
                            if (!string.Equals(actual, entry.Hash, StringComparison.OrdinalIgnoreCase))
                                throw new InvalidDataException("Install file differs from archive.");
                        }
                    }
                }
            }
            if (seen.Count != expected.Count) return LegacyReceiptOutcome.NotEligible;
            var receipt = JsonSerializer.SerializeToUtf8Bytes(new
            {
                schema = "netcoredbg-mcp.native-debugger-install/1",
                asset = AssetName,
                archiveSha256 = Convert.ToHexString(expectedHash).ToLowerInvariant(),
                files = expected.OrderBy(static pair => pair.Key, StringComparer.Ordinal)
                    .Select(static pair => new { path = pair.Key, size = pair.Value.Size, sha256 = pair.Value.Hash }).ToArray(),
            });
            if (receipt.Length > MaxManifest) throw new InvalidDataException("Receipt exceeds size limit.");
            ct.ThrowIfCancellationRequested();
            var name = ".native-receipt-" + Guid.NewGuid().ToString("N") + ".tmp";
            temporaryPath = Path.Combine(install, name);
            temporary = NativeShimStage.CreateReceiptTemporary(installHandle, name);
            RandomAccess.Write(temporary, receipt, 0);
            RandomAccess.FlushToDisk(temporary);
            ct.ThrowIfCancellationRequested();
            if (!ValidateSnapshot(install, expected, expectedDirectories, name, ct)) return LegacyReceiptOutcome.Existing;
            ct.ThrowIfCancellationRequested();
            // From this point a failure or exception cannot establish whether the
            // rename committed. Cleanup stays disabled until identity proves otherwise.
            uncertainRename = true;
            var renameStatus = RenameForTest?.Invoke(temporary, installHandle, ReceiptName)
                ?? RenameExclusive(temporary, installHandle, ReceiptName);
            if (renameStatus < 0)
            {
                var error = unchecked((int)RtlNtStatusToDosError(renameStatus));
                Console.Error.WriteLine($"Native receipt rename returned NTSTATUS 0x{unchecked((uint)renameStatus):X8}, Win32 error {error}.");
                var targetExists = Occupied(receiptPath);
                if (targetExists)
                {
                    var targetMatches = NativeShimStage.IsSameObjectAtPath(temporary, receiptPath);
                    if (targetMatches == true)
                    {
                        published = true;
                        uncertainRename = false;
                        return LegacyReceiptOutcome.Verified;
                    }
                    if (targetMatches is null) return LegacyReceiptOutcome.Unavailable;
                }
                // A different or absent target alone does not prove that deleting
                // this handle cannot remove a committed receipt elsewhere.
                var temporaryMatches = NativeShimStage.IsSameObjectAtPath(temporary, temporaryPath);
                if (temporaryMatches != true) return LegacyReceiptOutcome.Unavailable;
                uncertainRename = false;
                if (targetExists) return LegacyReceiptOutcome.Existing;
                throw new IOException("Could not publish receipt exclusively.", new Win32Exception(error));
            }
            // Publication is the commit point; a later cancellation must not be
            // reported as if the receipt had not been written.
            published = true;
            uncertainRename = false;
            return LegacyReceiptOutcome.Verified;
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { throw; }
        catch (Exception exception) when (exception is IOException or InvalidDataException or UnauthorizedAccessException or
            ArgumentException or NotSupportedException or System.Security.SecurityException or JsonException or InvalidOperationException or
            KeyNotFoundException or FormatException or OverflowException or HttpRequestException or OperationCanceledException)
        {
            var nativeError = (exception as IOException)?.InnerException as Win32Exception;
            Console.Error.WriteLine(nativeError is null
                ? $"Native legacy receipt migration unavailable: {exception.GetType().Name}."
                : $"Native legacy receipt migration unavailable: {exception.GetType().Name}, Win32 error {nativeError.NativeErrorCode}.");
            return LegacyReceiptOutcome.Unavailable;
        }
        finally
        {
            if (temporary is not null)
            {
                if (uncertainRename)
                    Console.Error.WriteLine($"Native legacy receipt rename outcome is unconfirmed; preserve and inspect {JsonSerializer.Serialize(temporaryPath)} and {JsonSerializer.Serialize(receiptPath)} before removal.");
                if (!published && !uncertainRename)
                {
                    try { NativeShimStage.DeleteOwned(temporary); }
                    catch (Exception exception)
                    {
                        Console.Error.WriteLine($"Native legacy receipt temporary cleanup failed at {JsonSerializer.Serialize(temporaryPath)}: {exception.GetType().Name}; inspect before removal.");
                    }
                }
                temporary.Dispose();
            }
            foreach (var stream in files.AsEnumerable().Reverse()) stream.Dispose();
            foreach (var handle in pins.AsEnumerable().Reverse()) handle.Dispose();
        }
    }

    private static bool ValidateSnapshot(string install, Dictionary<string, Entry> expected,
        HashSet<string> expectedDirectories, string temporaryName, CancellationToken token)
    {
        var pending = new Stack<string>();
        var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var count = 0;
        pending.Push(install);
        while (pending.TryPop(out var directory))
        {
            foreach (var path in Directory.EnumerateFileSystemEntries(directory))
            {
                token.ThrowIfCancellationRequested();
                if (++count > MaxEntries * 2 + 1) throw new InvalidDataException("Install contains excessive entries.");
                var relative = Path.GetRelativePath(install, path).Replace('\\', '/');
                if (relative.Equals(ReceiptName, StringComparison.OrdinalIgnoreCase)) return false;
                if (relative.Equals(temporaryName, StringComparison.OrdinalIgnoreCase)) continue;
                var attributes = File.GetAttributes(path);
                if ((attributes & FileAttributes.ReparsePoint) != 0) throw new InvalidDataException("Install contains a reparse point.");
                if ((attributes & FileAttributes.Directory) != 0)
                {
                    if (!expectedDirectories.Contains(relative)) throw new InvalidDataException("Install contains an unlisted directory.");
                    pending.Push(path);
                }
                else if (!expected.ContainsKey(relative) || !seen.Add(relative))
                    throw new InvalidDataException("Install contains an unlisted file.");
            }
        }
        if (seen.Count != expected.Count) throw new InvalidDataException("Install no longer matches archive.");
        return true;
    }

    private static async Task<Dictionary<string, Entry>> ReadArchiveAsync(string install, byte[] archive, CancellationToken token)
    {
        var entries = new Dictionary<string, Entry>(StringComparer.OrdinalIgnoreCase);
        var names = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var directories = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
        long total = 0;
        using var memory = new MemoryStream(archive, writable: false);
        using var zip = new ZipArchive(memory, ZipArchiveMode.Read);
        foreach (var entry in zip.Entries)
        {
            token.ThrowIfCancellationRequested();
            int mode = (entry.ExternalAttributes >> 16) & 0xffff;
            int type = mode & 0xf000;
            var directory = entry.FullName.EndsWith('/');
            if (type != 0 && type != (directory ? 0x4000 : 0x8000)) throw new InvalidDataException("Unsupported archive entry type.");
            var full = NativeDebuggerInstaller.EntryPath(install, entry.FullName, directory, names);
            var relative = Path.GetRelativePath(install, full).Replace('\\', '/');
            if (relative.Length > 1024 || relative.Split('/').Any(static part => part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0))
                throw new InvalidDataException("Unsafe archive path.");
            if (directory) { directories.Add(relative); continue; }
            total = NativeDebuggerInstaller.AddSize(total, entry.Length);
            if (total > MaxTotal || entry.Length > MaxFile || (relative.Equals(ShimPath, StringComparison.OrdinalIgnoreCase) && entry.Length > MaxShim))
                throw new InvalidDataException("Archive exceeds migration limits.");
            using var input = entry.Open();
            var hash = await HashStreamAsync(input, entry.Length, token);
            entries.Add(relative, new(entry.Length, hash));
        }
        // Empty or otherwise unrepresented directories cannot be attested by the receipt.
        foreach (var directory in directories)
            if (!entries.Keys.Any(path => path.StartsWith(directory + "/", StringComparison.OrdinalIgnoreCase)))
                throw new InvalidDataException("Empty archive directory is unsupported.");
        return entries;
    }

    private static async Task<string> HashStreamAsync(Stream input, long expectedLength, CancellationToken token)
    {
        using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = new byte[81920];
        long count = 0;
        int read;
        while ((read = await input.ReadAsync(buffer, token)) != 0)
        {
            count += read;
            if (count > expectedLength) throw new InvalidDataException("Stream exceeds declared length.");
            hash.AppendData(buffer, 0, read);
        }
        if (count != expectedLength) throw new InvalidDataException("Stream is truncated.");
        return Convert.ToHexString(hash.GetHashAndReset()).ToLowerInvariant();
    }

    private static async Task<byte[]> ReadBoundedAsync(Stream input, int maximum, CancellationToken token)
    {
        if (input.Length > maximum) throw new InvalidDataException("File exceeds size limit.");
        using var output = new MemoryStream();
        var buffer = new byte[8192];
        int read;
        while ((read = await input.ReadAsync(buffer, token)) != 0)
        {
            if (output.Length + read > maximum) throw new InvalidDataException("File exceeds size limit.");
            output.Write(buffer, 0, read);
        }
        return output.ToArray();
    }

    private static bool SafeTag(string? tag) => tag is { Length: > 0 and <= 64 } && char.IsAsciiLetterOrDigit(tag[0]) &&
        !tag.Contains("..", StringComparison.Ordinal) && tag.All(static c => char.IsAsciiLetterOrDigit(c) || c is '.' or '-' or '_');
    private static bool Occupied(string path)
    {
        if (OccupiedForTest is { } probe) return probe(path);
        try { _ = File.GetAttributes(path); return true; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }

    private static int RenameExclusive(SafeFileHandle file, SafeFileHandle parent, string name)
    {
        var nameBytes = Encoding.Unicode.GetBytes(name);
        var pointerOffset = IntPtr.Size == 8 ? 8 : 4;
        var lengthOffset = pointerOffset + IntPtr.Size;
        var nameOffset = lengthOffset + sizeof(uint);
        // Include structure alignment padding as required by the native contract.
        var size = checked((IntPtr.Size == 8 ? 24 : 16) + nameBytes.Length);
        var memory = Marshal.AllocHGlobal(size);
        try
        {
            Marshal.Copy(new byte[size], 0, memory, size);
            Marshal.WriteIntPtr(memory, pointerOffset, parent.DangerousGetHandle());
            Marshal.WriteInt32(memory, lengthOffset, nameBytes.Length);
            Marshal.Copy(nameBytes, 0, IntPtr.Add(memory, nameOffset), nameBytes.Length);
            return NtSetInformationFile(file, out _, memory, (uint)size, 10);
        }
        finally { Marshal.FreeHGlobal(memory); }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IoStatusBlock { internal IntPtr Status; internal UIntPtr Information; }
    [DllImport("ntdll.dll")]
    private static extern int NtSetInformationFile(SafeFileHandle file, out IoStatusBlock status,
        IntPtr information, uint size, int informationClass);
    [DllImport("ntdll.dll")]
    private static extern uint RtlNtStatusToDosError(int status);
}
