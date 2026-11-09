using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Formats.Tar;
using System.IO.Compression;
using System.Net;
using System.Security.Cryptography;
using System.Text.Json;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;

namespace NetCoreDbg.Mcp.Stateless;

internal sealed class NativeDebuggerInstalledCleanupException(string executablePath, string stagingPath, Exception inner)
    : IOException($"Debugger was installed at '{executablePath}', but staging cleanup failed at '{stagingPath}'. Inspect it before removal.", inner)
{
    public string ExecutablePath { get; } = executablePath;
    public string StagingPath { get; } = stagingPath;
}

internal static class NativeDebuggerInstaller
{
    // Limits deliberately exceed current releases but bound hostile metadata and archives.
    private const long MaxManifest = 2 * 1024 * 1024;
    private const long MaxArchive = 256L * 1024 * 1024;
    private const long MaxEntry = 256L * 1024 * 1024;
    private const long MaxTotal = 1024L * 1024 * 1024;
    private const int MaxEntries = 4096;
    private const int MaxRedirects = 5;
    private const string ApiPath = "/repos/Samsung/netcoredbg/releases/latest";
    private static readonly SemaphoreSlim ProvisionGate = new(1, 1);

    // Callers must use this factory in production: automatic redirects would bypass
    // validation before the redirected request. Injected clients are for controlled tests.
    internal static HttpClient CreateHttpClient() => new(new HttpClientHandler { AllowAutoRedirect = false });

    internal static async Task<string?> ProvisionAsync(CancellationToken cancellationToken)
    {
        await ProvisionGate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            var existing = LegacyDebuggerDiscovery.Resolve(null);
            if (existing is not null) return existing;
            var home = LegacyDebuggerDiscovery.GetHomeDirectory();
            var asset = NativeSetup.CurrentAsset();
            if (string.IsNullOrWhiteSpace(home) || asset is null) return null;
            var managedRoot = Path.Combine(Path.GetFullPath(home), ".netcoredbg-mcp");
            Directory.CreateDirectory(managedRoot);
            using var client = CreateHttpClient();
            client.Timeout = TimeSpan.FromSeconds(120);
            return await InstallAsync(client, new Uri("https://api.github.com/repos/Samsung/netcoredbg/releases/latest"),
                Path.Combine(managedRoot, "netcoredbg"), asset, cancellationToken).ConfigureAwait(false);
        }
        finally { ProvisionGate.Release(); }
    }

    internal static async Task<string> InstallAsync(HttpClient client, Uri releaseApi, string installDirectory, string assetKey, CancellationToken cancellationToken)
    {
        ArgumentNullException.ThrowIfNull(client);
        ArgumentNullException.ThrowIfNull(releaseApi);
        ArgumentException.ThrowIfNullOrWhiteSpace(installDirectory);
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(client.Timeout);
        cancellationToken = deadline.Token;
        var (assetName, executable, isTar) = assetKey switch
        {
            "win64" => ("netcoredbg-win64.zip", "netcoredbg.exe", false),
            "linux-amd64" => ("netcoredbg-linux-amd64.tar.gz", "netcoredbg", true),
            "linux-arm64" => ("netcoredbg-linux-arm64.tar.gz", "netcoredbg", true),
            "osx-arm64" => ("netcoredbg-osx-arm64.zip", "netcoredbg", false),
            _ => throw new ArgumentException("Unsupported debugger asset.", nameof(assetKey)),
        };
        var target = Path.GetFullPath(installDirectory);
        if (PathOccupied(target)) throw new IOException("Installation destination already exists.");
        if (!IsHttpsHost(releaseApi, "api.github.com") || releaseApi.AbsolutePath != ApiPath || releaseApi.Query.Length != 0 || releaseApi.Fragment.Length != 0)
            throw new InvalidOperationException("Release API must be the official Samsung GitHub endpoint.");
        var parent = Path.GetDirectoryName(target) ?? throw new ArgumentException("Destination needs a parent directory.");
        if (!Directory.Exists(parent)) throw new DirectoryNotFoundException("Installation parent does not exist.");

        using var manifestResponse = await GetAsync(client, releaseApi, cancellationToken);
        var manifestBytes = await ReadBoundedAsync(manifestResponse.Content, MaxManifest, cancellationToken);
        var (size, expectedHash, assetUri) = ParseManifest(manifestBytes, assetName);

        string stage = Path.Combine(parent, ".netcoredbg-stage-" + Guid.NewGuid().ToString("N"));
        bool created = false;
        Exception? failure = null;
        string? result = null;
        try
        {
            // Parent must not be writable by an adversary who can swap staging paths.
            CreateExclusiveDirectory(stage);
            created = true;
            string content = Path.Combine(stage, "content");
            Directory.CreateDirectory(content);
            string archivePath = Path.Combine(stage, "archive");
            using (var response = await GetAsync(client, assetUri, cancellationToken))
            {
                if (response.Content.Headers.ContentLength is long length && length != size)
                    throw new InvalidDataException("Archive content length differs from release metadata.");
                await using var source = await response.Content.ReadAsStreamAsync(cancellationToken);
                await using var output = new FileStream(archivePath, FileMode.CreateNew, FileAccess.Write, FileShare.None, 81920, true);
                using var hash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
                var buffer = new byte[81920];
                long count = 0;
                int read;
                while ((read = await source.ReadAsync(buffer, cancellationToken)) != 0)
                {
                    count += read;
                    if (count > size || count > MaxArchive) throw new InvalidDataException("Archive exceeds declared size.");
                    hash.AppendData(buffer, 0, read);
                    await output.WriteAsync(buffer.AsMemory(0, read), cancellationToken);
                }
                if (count != size || !CryptographicOperations.FixedTimeEquals(hash.GetHashAndReset(), expectedHash))
                    throw new InvalidDataException("Archive size or digest mismatch.");
            }
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            long total = 0;
            using (var file = File.OpenRead(archivePath))
            {
                if (isTar)
                {
                    using var gzip = new GZipStream(file, CompressionMode.Decompress);
                    using var bounded = new LimitedReadStream(gzip, MaxTotal + 16 * 1024 * 1024);
                    using var tar = new TarReader(bounded);
                    TarEntry? entry;
                    while ((entry = tar.GetNextEntry()) is not null)
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        bool directory = entry.EntryType == TarEntryType.Directory;
                        if (directory && entry.Name is "." or "./")
                        {
                            if (!seen.Add("") || seen.Count > MaxEntries) throw new InvalidDataException("Duplicate or excessive archive entries.");
                            continue;
                        }
                        if (!directory && entry.EntryType != TarEntryType.RegularFile && entry.EntryType != TarEntryType.V7RegularFile)
                            throw new InvalidDataException("Unsupported tar entry type.");
                        var path = EntryPath(content, entry.Name, directory, seen);
                        if (directory) Directory.CreateDirectory(path);
                        else
                        {
                            total = AddSize(total, entry.Length);
                            await ExtractAsync(entry.DataStream ?? Stream.Null, path, entry.Length, cancellationToken);
                            if (!OperatingSystem.IsWindows()) File.SetUnixFileMode(path, entry.Mode & (UnixFileMode)0x1FF);
                        }
                    }
                }
                else
                {
                    using var zip = new ZipArchive(file, ZipArchiveMode.Read);
                    foreach (var entry in zip.Entries)
                    {
                        cancellationToken.ThrowIfCancellationRequested();
                        int mode = (entry.ExternalAttributes >> 16) & 0xFFFF;
                        int type = mode & 0xF000;
                        bool directory = entry.FullName.EndsWith('/');
                        if (type != 0 && type != (directory ? 0x4000 : 0x8000)) throw new InvalidDataException("Unsupported zip entry type.");
                        var path = EntryPath(content, entry.FullName, directory, seen);
                        if (directory) Directory.CreateDirectory(path);
                        else
                        {
                            total = AddSize(total, entry.Length);
                            using var input = entry.Open();
                            await ExtractAsync(input, path, entry.Length, cancellationToken);
                            if (!OperatingSystem.IsWindows() && mode != 0) File.SetUnixFileMode(path, (UnixFileMode)(mode & 0x1FF));
                        }
                    }
                }
            }
            string relativeExecutable = Path.Combine("netcoredbg", executable);
            if (!File.Exists(Path.Combine(content, relativeExecutable))) throw new InvalidDataException("Archive lacks the expected debugger executable.");
            if (!OperatingSystem.IsWindows() && assetKey != "win64")
            {
                var mode = File.GetUnixFileMode(Path.Combine(content, relativeExecutable));
                if ((mode & (UnixFileMode.UserExecute | UnixFileMode.GroupExecute | UnixFileMode.OtherExecute)) == 0)
                    throw new InvalidDataException("Debugger executable has no execute permission.");
            }
            cancellationToken.ThrowIfCancellationRequested();
            PublishWithoutReplacement(content, target);
            result = Path.Combine(target, relativeExecutable);
        }
        catch (Exception ex) { failure = ex; }
        if (created)
        {
            try { Directory.Delete(stage, recursive: true); }
            catch (Exception cleanup)
            {
                if (result is not null)
                    throw new NativeDebuggerInstalledCleanupException(result, stage, cleanup);
                var unconfirmed = new AggregateException($"Could not clean task-owned staging directory '{stage}'; inspect it before removal.", failure is null ? [cleanup] : [failure, cleanup]);
                unconfirmed.Data["StagingPath"] = stage;
                throw unconfirmed;
            }
        }
        if (failure is not null) System.Runtime.ExceptionServices.ExceptionDispatchInfo.Capture(failure).Throw();
        return result!;
    }

    private static (long Size, byte[] Hash, Uri AssetUri) ParseManifest(byte[] bytes, string assetName)
    {
        try
        {
            using var document = JsonDocument.Parse(bytes);
            JsonElement selected = default;
            var matches = 0;
            foreach (var asset in document.RootElement.GetProperty("assets").EnumerateArray())
            {
                if (asset.GetProperty("name").GetString() == assetName) { selected = asset; matches++; }
            }
            if (matches != 1) throw new InvalidDataException("Expected exactly one matching release asset.");
            var size = selected.GetProperty("size").GetInt64();
            if (size <= 0 || size > MaxArchive) throw new InvalidDataException("Invalid declared archive size.");
            var digest = selected.GetProperty("digest").GetString();
            if (digest is null || digest.Length != 71 || !digest.StartsWith("sha256:", StringComparison.Ordinal) || !digest.AsSpan(7).ToString().All(Uri.IsHexDigit))
                throw new InvalidDataException("Missing or invalid SHA-256 digest.");
            var hash = Convert.FromHexString(digest[7..]);
            if (!Uri.TryCreate(selected.GetProperty("browser_download_url").GetString(), UriKind.Absolute, out var uri) ||
                !IsHttpsHost(uri, "github.com") || !uri.AbsolutePath.StartsWith("/Samsung/netcoredbg/releases/download/", StringComparison.Ordinal) ||
                !uri.AbsolutePath.EndsWith("/" + assetName, StringComparison.Ordinal) || uri.Query.Length != 0 || uri.Fragment.Length != 0)
                throw new InvalidDataException("Invalid release asset URL.");
            return (size, hash, uri);
        }
        catch (Exception exception) when (exception is JsonException or KeyNotFoundException or InvalidOperationException or FormatException or OverflowException)
        {
            throw new InvalidDataException("The debugger release manifest is malformed.", exception);
        }
    }

    private static bool PathOccupied(string path)
    {
        try { _ = File.GetAttributes(path); return true; }
        catch (FileNotFoundException) { return false; }
        catch (DirectoryNotFoundException) { return false; }
    }

    private static void CreateExclusiveDirectory(string path)
    {
        bool success = OperatingSystem.IsWindows() ? CreateDirectoryWindows(path, IntPtr.Zero) :
            (OperatingSystem.IsLinux() || OperatingSystem.IsMacOS()) ? Mkdir(path, 448) == 0 :
            throw new PlatformNotSupportedException("Exclusive staging is unavailable.");
        if (!success) throw new IOException("Could not create exclusive staging directory.", new Win32Exception(Marshal.GetLastPInvokeError()));
    }

    [DllImport("kernel32.dll", EntryPoint = "CreateDirectoryW", CharSet = CharSet.Unicode, SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool CreateDirectoryWindows(string path, IntPtr securityAttributes);

    [DllImport("libc", EntryPoint = "mkdir", SetLastError = true)]
    private static extern int Mkdir(string path, uint mode);

    // POSIX rename can replace an empty directory, so use each platform's
    // exclusive rename primitive; unsupported kernels fail closed.
    private static void PublishWithoutReplacement(string source, string destination)
    {
        if (OperatingSystem.IsWindows()) { Directory.Move(source, destination); return; }
        int result = OperatingSystem.IsLinux() ? RenameAt2(-100, source, -100, destination, 1) :
            OperatingSystem.IsMacOS() ? RenameExclusive(source, destination, 4) :
            throw new PlatformNotSupportedException("Exclusive directory rename is unavailable.");
        if (result != 0) throw new IOException("Exclusive installation publication failed.", new Win32Exception(Marshal.GetLastPInvokeError()));
    }

    [DllImport("libc", EntryPoint = "renameat2", SetLastError = true)]
    private static extern int RenameAt2(int oldDirectory, string oldPath, int newDirectory, string newPath, uint flags);

    [DllImport("libc", EntryPoint = "renamex_np", SetLastError = true)]
    private static extern int RenameExclusive(string oldPath, string newPath, uint flags);

    private static bool IsHttpsHost(Uri uri, string host) => uri.IsAbsoluteUri && uri.Scheme == Uri.UriSchemeHttps && uri.Host.Equals(host, StringComparison.OrdinalIgnoreCase) && uri.IsDefaultPort && uri.UserInfo.Length == 0;

    private static async Task<HttpResponseMessage> GetAsync(HttpClient client, Uri uri, CancellationToken cancellationToken)
    {
        for (int redirects = 0; ; redirects++)
        {
            using var request = new HttpRequestMessage(HttpMethod.Get, uri);
            request.Headers.UserAgent.ParseAdd("netcoredbg-mcp-native-setup");
            var response = await client.SendAsync(request, HttpCompletionOption.ResponseHeadersRead, cancellationToken);
            if ((int)response.StatusCode is >= 300 and < 400)
            {
                var location = response.Headers.Location;
                response.Dispose();
                if (redirects >= MaxRedirects || location is null) throw new InvalidDataException("Invalid or excessive redirects.");
                uri = location.IsAbsoluteUri ? location : new Uri(uri, location);
                if (!IsHttpsHost(uri, "github.com") && !IsHttpsHost(uri, "api.github.com") &&
                    !IsHttpsHost(uri, "release-assets.githubusercontent.com") && !IsHttpsHost(uri, "objects.githubusercontent.com"))
                    throw new InvalidDataException("Redirect is not an approved GitHub HTTPS endpoint.");
                continue;
            }
            try { response.EnsureSuccessStatusCode(); return response; }
            catch { response.Dispose(); throw; }
        }
    }

    private static async Task<byte[]> ReadBoundedAsync(HttpContent content, long limit, CancellationToken cancellationToken)
    {
        if (content.Headers.ContentLength > limit) throw new InvalidDataException("Response exceeds size limit.");
        await using var input = await content.ReadAsStreamAsync(cancellationToken);
        using var output = new MemoryStream();
        var buffer = new byte[81920];
        int read;
        while ((read = await input.ReadAsync(buffer, cancellationToken)) != 0)
        {
            if (output.Length + read > limit) throw new InvalidDataException("Response exceeds size limit.");
            await output.WriteAsync(buffer.AsMemory(0, read), cancellationToken);
        }
        return output.ToArray();
    }

    private sealed class LimitedReadStream(Stream inner, long limit) : Stream
    {
        private long _read;
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        private int Count(int n)
        {
            _read += n;
            if (_read > limit) throw new InvalidDataException("Decompressed tar exceeds size limit.");
            return n;
        }
        public override int Read(byte[] buffer, int offset, int count) => Count(inner.Read(buffer, offset, count));
        public override int Read(Span<byte> buffer) => Count(inner.Read(buffer));
        public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default) => Count(await inner.ReadAsync(buffer, cancellationToken));
    }

    private static string EntryPath(string root, string name, bool directory, HashSet<string> seen)
    {
        while (name.StartsWith("./", StringComparison.Ordinal)) name = name[2..];
        if (directory) name = name.TrimEnd('/');
        if (name.Length == 0 || name.Contains('\\') || name.StartsWith('/') || name.Contains(':')) throw new InvalidDataException("Invalid archive path.");
        var parts = name.Split('/');
        if (parts.Any(part => part.Length == 0 || part is "." or ".." || part.EndsWith(' ') || part.EndsWith('.'))) throw new InvalidDataException("Invalid archive path component.");
        if (!seen.Add(name) || seen.Count > MaxEntries) throw new InvalidDataException("Duplicate or excessive archive entries.");
        var path = Path.GetFullPath(Path.Combine(root, Path.Combine(parts)));
        if (!path.StartsWith(root + Path.DirectorySeparatorChar, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal))
            throw new InvalidDataException("Archive path escapes staging.");
        return path;
    }

    private static long AddSize(long total, long length)
    {
        if (length < 0 || length > MaxEntry || total > MaxTotal - length) throw new InvalidDataException("Archive extraction exceeds size limit.");
        return total + length;
    }

    private static async Task ExtractAsync(Stream input, string path, long length, CancellationToken cancellationToken)
    {
        if (length < 0 || length > MaxEntry) throw new InvalidDataException("Archive entry exceeds size limit.");
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        await using var output = new FileStream(path, FileMode.CreateNew, FileAccess.Write, FileShare.None, 81920, true);
        var buffer = new byte[81920];
        long count = 0;
        int read;
        while ((read = await input.ReadAsync(buffer, cancellationToken)) != 0)
        {
            count += read;
            if (count > length) throw new InvalidDataException("Archive entry exceeds declared size.");
            await output.WriteAsync(buffer.AsMemory(0, read), cancellationToken);
        }
        if (count != length) throw new InvalidDataException("Archive entry is truncated.");
    }
}
