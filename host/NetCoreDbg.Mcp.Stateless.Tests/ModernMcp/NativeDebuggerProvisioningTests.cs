using System.Formats.Tar;
using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

public sealed class NativeDebuggerProvisioningTests
{
    private const string ApiUrl = "https://api.github.com/repos/Samsung/netcoredbg/releases/latest";

    [Fact]
    public async Task VerifiedReleaseIsInstalledWithoutTouchingOtherFiles()
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var target = Path.Combine(scratch, "managed", "netcoredbg");
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            var sentinel = Path.Combine(scratch, "managed", "keep.txt");
            await File.WriteAllTextAsync(sentinel, "preserve");
            var archive = CreateArchive();
            using var handler = new ReleaseHandler(archive);
            using var client = new HttpClient(handler);

            var executable = await install(client, new Uri(ApiUrl), target, "win64", CancellationToken.None);

            Assert.Equal(Path.Combine(target, "netcoredbg", "netcoredbg.exe"), executable);
            Assert.Equal("fixture-debugger", await File.ReadAllTextAsync(executable));
            Assert.Equal("fixture-library", await File.ReadAllTextAsync(Path.Combine(target, "netcoredbg", "ManagedPart.dll")));
            Assert.Equal("preserve", await File.ReadAllTextAsync(sentinel));
            Assert.Equal(1, handler.DownloadCount);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task ApprovedAssetRedirectIsFollowed()
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            using var handler = new ReleaseHandler(CreateArchive(), "approved-redirect");
            using var client = new HttpClient(handler);
            var executable = await install(client, new Uri(ApiUrl), Path.Combine(scratch, "managed"), "win64", CancellationToken.None);
            Assert.Equal("fixture-debugger", await File.ReadAllTextAsync(executable));
            Assert.Equal(3, handler.RequestCount);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Theory]
    [InlineData("manifest-stall")]
    [InlineData("archive-stall")]
    public async Task ClientDeadlineCancelsStalledResponseBody(string fault)
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        using var handler = new ReleaseHandler(CreateArchive(), fault);
        using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(1) };
        var target = Path.Combine(scratch, "managed");
        try
        {
            var operation = install(client, new Uri(ApiUrl), target, "win64", CancellationToken.None);
            var entered = await Task.WhenAny(handler.BodyReadStarted, operation, Task.Delay(TimeSpan.FromSeconds(10)));
            if (!ReferenceEquals(entered, handler.BodyReadStarted))
            {
                handler.ReleaseStall();
                try { await operation; } catch (Exception) { }
                Assert.Fail("The fixture did not reach the response body.");
            }
            var completed = await Task.WhenAny(operation, Task.Delay(TimeSpan.FromSeconds(10)));
            if (!ReferenceEquals(completed, operation))
            {
                handler.ReleaseStall();
                try { await operation; } catch (Exception) { }
                Assert.Fail("The response body exceeded the configured setup deadline.");
            }
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
            Assert.Equal(fault == "manifest-stall" ? 1 : 2, handler.RequestCount);
            Assert.False(Directory.Exists(target));
            Assert.Empty(Directory.GetDirectories(scratch, ".netcoredbg-stage-*"));
        }
        finally
        {
            handler.ReleaseStall();
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }


    [Fact]
    public void SetupClassifiesInvalidReleaseDataAsReportable()
    {
        var assembly = Assembly.LoadFrom(TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));
        var type = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeSetup");
        Assert.NotNull(type);
        var predicate = type!.GetMethod("IsReportableFailure", BindingFlags.Static | BindingFlags.NonPublic);
        Assert.NotNull(predicate);
        Assert.True((bool)predicate!.Invoke(null, [new InvalidDataException("invalid release")])!);
    }

    [Theory]
    [InlineData("digest")]
    [InlineData("missing-digest")]
    [InlineData("missing-assets")]
    [InlineData("wrong-assets")]
    [InlineData("missing-digest-property")]
    [InlineData("wrong-size-type")]
    [InlineData("traversal")]
    [InlineData("symlink")]
    [InlineData("duplicate")]
    [InlineData("oversize")]
    [InlineData("foreign-url")]
    [InlineData("foreign-redirect")]
    public async Task InvalidReleaseNeverPublishesAnInstallation(string fault)
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var target = Path.Combine(scratch, "managed", "netcoredbg");
            Directory.CreateDirectory(Path.GetDirectoryName(target)!);
            var archive = CreateArchive(fault);
            using var handler = new ReleaseHandler(archive, fault);
            using var client = new HttpClient(handler);

            await Assert.ThrowsAsync<InvalidDataException>(() => install(client, new Uri(ApiUrl), target, "win64", CancellationToken.None));

            Assert.False(Directory.Exists(target));
            Assert.False(File.Exists(Path.Combine(scratch, "managed", "escape.txt")));
            if (fault == "foreign-url") Assert.Equal(0, handler.DownloadCount);
            if (fault == "foreign-redirect") Assert.Equal(2, handler.RequestCount);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task ExistingDestinationIsNeverReplacedOrDownloadedOver()
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var target = Path.Combine(scratch, "netcoredbg");
            Directory.CreateDirectory(target);
            var sentinel = Path.Combine(target, "existing.txt");
            await File.WriteAllTextAsync(sentinel, "preserve");
            using var handler = new ReleaseHandler(CreateArchive());
            using var client = new HttpClient(handler);

            await Assert.ThrowsAsync<IOException>(() => install(client, new Uri(ApiUrl), target, "win64", CancellationToken.None));

            Assert.Equal("preserve", await File.ReadAllTextAsync(sentinel));
            Assert.Equal(0, handler.RequestCount);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Theory]
    [InlineData("http://api.github.com/repos/Samsung/netcoredbg/releases/latest")]
    [InlineData("https://foreign.invalid/repos/Samsung/netcoredbg/releases/latest")]
    public async Task UntrustedReleaseEndpointIsRejectedBeforeRequest(string api)
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            using var handler = new ReleaseHandler(CreateArchive());
            using var client = new HttpClient(handler);
            await Assert.ThrowsAsync<InvalidOperationException>(() => install(client, new Uri(api), Path.Combine(scratch, "managed"), "win64", CancellationToken.None));
            Assert.Equal(0, handler.RequestCount);
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Theory]
    [InlineData(null)]
    [InlineData("link")]
    [InlineData("duplicate")]
    public async Task TarEntriesAreValidatedAndExecutableModePreserved(string? fault)
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var target = Path.Combine(scratch, "managed");
            using var handler = new ReleaseHandler(CreateTar(fault), assetKey: "linux-amd64");
            using var client = new HttpClient(handler);
            if (fault is not null)
            {
                await Assert.ThrowsAsync<InvalidDataException>(() => install(client, new Uri(ApiUrl), target, "linux-amd64", CancellationToken.None));
                Assert.False(Directory.Exists(target));
            }
            else
            {
                var executable = await install(client, new Uri(ApiUrl), target, "linux-amd64", CancellationToken.None);
                Assert.Equal("fixture-debugger", await File.ReadAllTextAsync(executable));
                if (!OperatingSystem.IsWindows()) Assert.NotEqual(0, (int)(File.GetUnixFileMode(executable) & UnixFileMode.UserExecute));
            }
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task CancellationDoesNotPublish()
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var target = Path.Combine(scratch, "managed");
            using var handler = new ReleaseHandler(CreateArchive());
            using var client = new HttpClient(handler);
            using var cancellation = new CancellationTokenSource();
            cancellation.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(() => install(client, new Uri(ApiUrl), target, "win64", cancellation.Token));
            Assert.False(Path.Exists(target));
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task DestinationCreatedDuringDownloadIsPreserved()
    {
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            var target = Path.Combine(scratch, "managed");
            using var handler = new ReleaseHandler(CreateArchive(), onDownload: () =>
            {
                Directory.CreateDirectory(target);
                File.WriteAllText(Path.Combine(target, "sentinel"), "preserve");
            });
            using var client = new HttpClient(handler);
            await Assert.ThrowsAsync<IOException>(() => install(client, new Uri(ApiUrl), target, "win64", CancellationToken.None));
            Assert.Equal("preserve", await File.ReadAllTextAsync(Path.Combine(target, "sentinel")));
        }
        finally { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
    }

    [Fact]
    public async Task CleanupFailureAfterPublicationReportsTheInstalledPath()
    {
        if (!OperatingSystem.IsWindows()) return;
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        FileStream? heldFile = null;
        try
        {
            var target = Path.Combine(scratch, "managed");
            using var handler = new ReleaseHandler(CreateArchive(), onDownload: () =>
            {
                var staging = Assert.Single(Directory.GetDirectories(scratch, ".netcoredbg-stage-*"));
                heldFile = new FileStream(Path.Combine(staging, "held-by-fixture"), FileMode.CreateNew,
                    FileAccess.ReadWrite, FileShare.ReadWrite);
            });
            using var client = new HttpClient(handler);

            var error = await Assert.ThrowsAnyAsync<IOException>(() => install(client, new Uri(ApiUrl), target, "win64", CancellationToken.None));

            var executable = Path.Combine(target, "netcoredbg", "netcoredbg.exe");
            Assert.True(File.Exists(executable));
            Assert.Contains("installed", error.Message, StringComparison.OrdinalIgnoreCase);
            Assert.Contains(executable, error.Message, StringComparison.OrdinalIgnoreCase);
            Assert.Equal(executable, error.GetType().GetProperty("ExecutablePath")?.GetValue(error));
        }
        finally
        {
            heldFile?.Dispose();
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    [Fact]
    public async Task CleanupFailureBeforePublicationReportsTheStagingPath()
    {
        if (!OperatingSystem.IsWindows()) return;
        var install = Installer();
        var scratch = ModernMcpScratchDirectory.Create();
        FileStream? heldFile = null;
        try
        {
            var target = Path.Combine(scratch, "managed");
            string? staging = null;
            using var handler = new ReleaseHandler(CreateArchive(), "digest", onDownload: () =>
            {
                staging = Assert.Single(Directory.GetDirectories(scratch, ".netcoredbg-stage-*"));
                heldFile = new FileStream(Path.Combine(staging, "held-by-fixture"), FileMode.CreateNew,
                    FileAccess.ReadWrite, FileShare.ReadWrite);
            });
            using var client = new HttpClient(handler);

            var error = await Assert.ThrowsAsync<AggregateException>(() => install(client, new Uri(ApiUrl), target, "win64", CancellationToken.None));

            Assert.NotNull(staging);
            Assert.Equal(staging, error.Data["StagingPath"]);
            Assert.False(Directory.Exists(target));
            Assert.True(Directory.Exists(staging));
        }
        finally
        {
            heldFile?.Dispose();
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    private static byte[] CreateTar(string? fault)
    {
        using var output = new MemoryStream();
        using (var gzip = new GZipStream(output, CompressionMode.Compress, leaveOpen: true))
        using (var tar = new TarWriter(gzip, leaveOpen: true))
        {
            void FileEntry(string name)
            {
                var entry = new PaxTarEntry(TarEntryType.RegularFile, name)
                {
                    DataStream = new MemoryStream(Encoding.UTF8.GetBytes("fixture-debugger")),
                    Mode = UnixFileMode.UserRead | UnixFileMode.UserWrite | UnixFileMode.UserExecute,
                };
                tar.WriteEntry(entry);
                entry.DataStream!.Dispose();
            }
            FileEntry("netcoredbg/netcoredbg");
            if (fault == "duplicate") FileEntry("netcoredbg/netcoredbg");
            if (fault == "link") tar.WriteEntry(new PaxTarEntry(TarEntryType.SymbolicLink, "netcoredbg/link") { LinkName = "../escape" });
        }
        return output.ToArray();
    }

    private static Func<HttpClient, Uri, string, string, CancellationToken, Task<string>> Installer()
    {
        var assembly = Assembly.LoadFrom(TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));
        var type = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeDebuggerInstaller");
        Assert.NotNull(type);
        var method = type!.GetMethod("InstallAsync", BindingFlags.Static | BindingFlags.NonPublic);
        Assert.NotNull(method);
        return (client, api, target, platform, cancellation) =>
            (Task<string>)method!.Invoke(null, [client, api, target, platform, cancellation])!;
    }

    private static byte[] CreateArchive(string? fault = null)
    {
        using var output = new MemoryStream();
        using (var archive = new ZipArchive(output, ZipArchiveMode.Create, leaveOpen: true))
        {
            WriteEntry(archive, "netcoredbg/netcoredbg.exe", "fixture-debugger");
            WriteEntry(archive, "netcoredbg/ManagedPart.dll", "fixture-library");
            if (fault == "duplicate") WriteEntry(archive, "netcoredbg/netcoredbg.exe", "duplicate");
            if (fault == "traversal") WriteEntry(archive, "../escape.txt", "escape");
            if (fault == "symlink")
            {
                var entry = archive.CreateEntry("netcoredbg/link");
                entry.ExternalAttributes = unchecked((int)0xA1FF0000);
                using var stream = new StreamWriter(entry.Open());
                stream.Write("../escape.txt");
            }
        }
        return output.ToArray();
    }

    private static void WriteEntry(ZipArchive archive, string name, string content)
    {
        using var stream = new StreamWriter(archive.CreateEntry(name).Open());
        stream.Write(content);
    }

    private sealed class ReleaseHandler(byte[] archive, string? fault = null, string assetKey = "win64", Action? onDownload = null) : HttpMessageHandler
    {
        internal int RequestCount { get; private set; }
        internal int DownloadCount { get; private set; }
        private readonly StalledStream _stall = new();
        internal Task BodyReadStarted => _stall.Started;
        internal void ReleaseStall() => _stall.Release();

        protected override Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            RequestCount++;
            var assetName = assetKey == "win64" ? "netcoredbg-win64.zip" : "netcoredbg-linux-amd64.tar.gz";
            var assetUrl = "https://github.com/Samsung/netcoredbg/releases/download/test/" + assetName;
            var url = request.RequestUri!.AbsoluteUri;
            if (url == ApiUrl)
            {
                if (fault == "manifest-stall")
                    return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StreamContent(_stall) });
                if (fault is "missing-assets" or "wrong-assets" or "missing-digest-property" or "wrong-size-type")
                {
                    var malformed = fault switch
                    {
                        "missing-assets" => "{}",
                        "wrong-assets" => "{\"assets\":{}}",
                        "missing-digest-property" => JsonSerializer.Serialize(new { assets = new[] { new { name = assetName, browser_download_url = assetUrl, size = archive.Length } } }),
                        _ => JsonSerializer.Serialize(new { assets = new[] { new { name = assetName, browser_download_url = assetUrl, size = "invalid", digest = "sha256:" + new string('0', 64) } } }),
                    };
                    return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
                    {
                        Content = new StringContent(malformed, Encoding.UTF8, "application/json"),
                    });
                }
                var digest = fault == "digest" ? new string('0', 64) : Convert.ToHexString(SHA256.HashData(archive)).ToLowerInvariant();
                var manifest = JsonSerializer.Serialize(new
                {
                    tag_name = "test",
                    assets = new[] { new {
                        name = assetName,
                        browser_download_url = fault == "foreign-url" ? "https://foreign.invalid/archive.zip" : assetUrl,
                        size = fault == "oversize" ? 300L * 1024 * 1024 : archive.Length,
                        digest = fault == "missing-digest" ? null : "sha256:" + digest,
                    } },
                });
                return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK)
                {
                    Content = new StringContent(manifest, Encoding.UTF8, "application/json"),
                });
            }
            if (url == assetUrl)
            {
                DownloadCount++;
                if (fault == "archive-stall")
                    return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new StreamContent(_stall) });
                if (fault is "foreign-redirect" or "approved-redirect")
                {
                    var redirect = new HttpResponseMessage(HttpStatusCode.Redirect);
                    redirect.Headers.Location = new Uri(fault == "foreign-redirect"
                        ? "https://foreign.invalid/escape" : "https://release-assets.githubusercontent.com/fixture");
                    return Task.FromResult(redirect);
                }
                onDownload?.Invoke();
                return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(archive) });
            }
            if (url == "https://release-assets.githubusercontent.com/fixture" && fault == "approved-redirect")
                return Task.FromResult(new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(archive) });
            throw new InvalidOperationException("Unexpected network destination in controlled fixture.");
        }
    }
    private sealed class StalledStream : Stream
    {
        private readonly TaskCompletionSource<bool> _release = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<bool> _started = new(TaskCreationOptions.RunContinuationsAsynchronously);
        internal Task Started => _started.Task;
        internal void Release() => _release.TrySetResult(true);
        public override bool CanRead => true;
        public override bool CanSeek => false;
        public override bool CanWrite => false;
        public override long Length => throw new NotSupportedException();
        public override long Position { get => throw new NotSupportedException(); set => throw new NotSupportedException(); }
        public override void Flush() => throw new NotSupportedException();
        public override long Seek(long offset, SeekOrigin origin) => throw new NotSupportedException();
        public override void SetLength(long value) => throw new NotSupportedException();
        public override void Write(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override int Read(byte[] buffer, int offset, int count) => throw new NotSupportedException();
        public override async ValueTask<int> ReadAsync(Memory<byte> buffer, CancellationToken cancellationToken = default)
        {
            _started.TrySetResult(true);
            await _release.Task.WaitAsync(cancellationToken);
            return 0;
        }
    }
}
