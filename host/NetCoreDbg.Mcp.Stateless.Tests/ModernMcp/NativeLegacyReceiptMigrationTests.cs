using System.IO.Compression;
using System.Net;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using Microsoft.Win32.SafeHandles;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
public sealed class NativeLegacyReceiptMigrationTests
{
    private const string Tag = "3.1.2-1054";
    private const string Asset = "netcoredbg-win64.zip";
    private const string Receipt = ".native-debugger-manifest.json";
    private const string Api = "https://api.github.com/repos/Samsung/netcoredbg/releases/tags/" + Tag;
    private const string Download = "https://github.com/Samsung/netcoredbg/releases/download/" + Tag + "/" + Asset;
    private static readonly byte[] Debugger = Encoding.UTF8.GetBytes("official debugger bytes");
    private static readonly byte[] Library = Encoding.UTF8.GetBytes("official library bytes");
    private static readonly byte[] OfficialShim = Encoding.UTF8.GetBytes("official shim bytes");
    private static readonly byte[] SwitchedShim = Encoding.UTF8.GetBytes("python selected a different shim");

    [Fact]
    public async Task VerifiedLegacyInstallPublishesCompatibleReceiptWithoutChangingInputs()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var before = fixture.Snapshot();
        using var handler = new ReleaseHandler(fixture.Archive);
        using var client = new HttpClient(handler);

        var outcome = await Upgrade(client, fixture.Home, fixture.DebuggerPath);

        Assert.True(File.Exists(fixture.ReceiptPath), $"Outcome: {outcome}; requests: {string.Join(", ", handler.Requests)}");
        fixture.AssertUnchanged(before, allowReceipt: true);
        using var receipt = JsonDocument.Parse(File.ReadAllBytes(fixture.ReceiptPath));
        var root = receipt.RootElement;
        Assert.Equal("netcoredbg-mcp.native-debugger-install/1", root.GetProperty("schema").GetString());
        Assert.Equal(Asset, root.GetProperty("asset").GetString());
        Assert.Equal(Hash(fixture.Archive), root.GetProperty("archiveSha256").GetString());
        var files = root.GetProperty("files").EnumerateArray().ToDictionary(x => x.GetProperty("path").GetString()!, StringComparer.Ordinal);
        var expected = new Dictionary<string, byte[]>
        {
            ["netcoredbg/netcoredbg.exe"] = Debugger,
            ["netcoredbg/ManagedPart.dll"] = Library,
            ["netcoredbg/dbgshim.dll"] = OfficialShim,
        };
        Assert.Equal(expected.Keys.OrderBy(x => x), files.Keys.OrderBy(x => x));
        foreach (var (path, bytes) in expected)
        {
            Assert.Equal(bytes.LongLength, files[path].GetProperty("size").GetInt64());
            Assert.Equal(Hash(bytes), files[path].GetProperty("sha256").GetString());
        }
        Assert.Equal(SwitchedShim, File.ReadAllBytes(fixture.ShimPath));
        Assert.Equal(new[] { Api, Download }, handler.Requests);

        var receiptBytes = File.ReadAllBytes(fixture.ReceiptPath);
        await Upgrade(client, fixture.Home, fixture.DebuggerPath);
        Assert.Equal(receiptBytes, File.ReadAllBytes(fixture.ReceiptPath));
        fixture.AssertUnchanged(before, allowReceipt: true);
        Assert.InRange(handler.Requests.Count, 2, 4);
    }

    [Theory]
    [InlineData("missing-config")]
    [InlineData("malformed-config")]
    [InlineData("non-github")]
    [InlineData("unsafe-tag")]
    [InlineData("long-tag")]
    [InlineData("wrong-config-path")]
    [InlineData("wrong-debugger-path")]
    [InlineData("null-digest")]
    [InlineData("bad-digest")]
    [InlineData("bad-size")]
    [InlineData("wrong-asset-url")]
    [InlineData("wrong-release-tag")]
    [InlineData("changed-file")]
    [InlineData("missing-file")]
    [InlineData("extra-file")]
    [InlineData("missing-shim")]
    [InlineData("offline")]
    [InlineData("existing-receipt")]
    public async Task IneligibleOrUnavailableInstallFallsBackWithoutChangingAnyInput(string fault)
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var debuggerPath = fixture.DebuggerPath;
        switch (fault)
        {
            case "missing-config": File.Delete(fixture.ConfigPath); break;
            case "malformed-config": File.WriteAllText(fixture.ConfigPath, "{ broken"); break;
            case "non-github": fixture.Config(source: "other"); break;
            case "unsafe-tag": fixture.Config(tag: "../other"); break;
            case "long-tag": fixture.Config(tag: new string('a', 4096)); break;
            case "wrong-config-path": fixture.Config(path: Path.Combine(fixture.Home, "other.exe")); break;
            case "wrong-debugger-path": debuggerPath = Path.Combine(fixture.Home, "other.exe"); File.WriteAllBytes(debuggerPath, Debugger); break;
            case "changed-file": File.WriteAllText(fixture.LibraryPath, "tampered"); break;
            case "missing-file": File.Delete(fixture.LibraryPath); break;
            case "extra-file": File.WriteAllText(Path.Combine(fixture.Install, "netcoredbg", "unknown.dll"), "unknown"); break;
            case "missing-shim": File.Delete(fixture.ShimPath); break;
            case "existing-receipt": File.WriteAllText(fixture.ReceiptPath, "existing receipt must survive"); break;
        }
        var before = fixture.Snapshot();
        using var handler = new ReleaseHandler(fixture.Archive, fault);
        using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(2) };

        await Upgrade(client, fixture.Home, debuggerPath);

        fixture.AssertUnchanged(before, allowReceipt: false);
        Assert.InRange(handler.Requests.Count, 0, 3);
    }

    [Fact]
    public async Task CallerCancellationPropagatesWithoutPublishing()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var before = fixture.Snapshot();
        using var handler = new ReleaseHandler(fixture.Archive, "stall");
        using var client = new HttpClient(handler) { Timeout = TimeSpan.FromSeconds(30) };
        using var cancellation = new CancellationTokenSource();
        var operation = Upgrade(client, fixture.Home, fixture.DebuggerPath, cancellation.Token);
        try
        {
            await handler.RequestStarted.Task.WaitAsync(TimeSpan.FromSeconds(5));
        }
        finally { cancellation.Cancel(); }

        await Assert.ThrowsAnyAsync<OperationCanceledException>(() => operation);
        fixture.AssertUnchanged(before, allowReceipt: false);
    }

    [Theory]
    [InlineData("symlink")]
    [InlineData("duplicate")]
    [InlineData("traversal")]
    public async Task UnsafeArchiveEntryNeverPublishesReceipt(string fault)
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture(fault);
        var before = fixture.Snapshot();
        using var handler = new ReleaseHandler(fixture.Archive);
        using var client = new HttpClient(handler);

        await Upgrade(client, fixture.Home, fixture.DebuggerPath);

        fixture.AssertUnchanged(before, allowReceipt: false);
        Assert.Equal(new[] { Api, Download }, handler.Requests);
    }

    [Fact]
    public async Task StalledNetworkIsBoundedAndLeavesNoReceipt()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var before = fixture.Snapshot();
        using var handler = new ReleaseHandler(fixture.Archive, "stall");
        using var client = new HttpClient(handler) { Timeout = TimeSpan.FromMilliseconds(200) };
        using var watchdog = new CancellationTokenSource(TimeSpan.FromSeconds(5));

        await Upgrade(client, fixture.Home, fixture.DebuggerPath, watchdog.Token);

        Assert.False(watchdog.IsCancellationRequested);
        fixture.AssertUnchanged(before, allowReceipt: false);
        Assert.Single(handler.Requests);
    }

    [Theory]
    [InlineData("occupied-throws")]
    [InlineData("metadata-fails")]
    public async Task UncertainRenameNeverDeletesPublishedReceiptOrReportsExisting(string fault)
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var before = fixture.Snapshot();
        using var handler = new ReleaseHandler(fixture.Archive);
        using var client = new HttpClient(handler);
        var assembly = Assembly.LoadFrom(TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));
        var migration = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeLegacyReceiptMigration", throwOnError: true)!;
        var stage = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeShimStage", throwOnError: true)!;
        const BindingFlags flags = BindingFlags.Static | BindingFlags.NonPublic;
        var renameHook = migration.GetField("RenameForTest", flags)!;
        var occupiedHook = migration.GetField("OccupiedForTest", flags)!;
        var metadataHook = stage.GetField("FailSameObjectMetadataForTest", flags)!;
        var rename = migration.GetMethod("RenameExclusive", flags)!;
        Assert.NotNull(renameHook);
        Assert.NotNull(occupiedHook);
        Assert.NotNull(metadataHook);
        Assert.NotNull(rename);
        var renamed = false;
        var probeCalled = false;
        var metadataCalled = false;
        try
        {
            renameHook.SetValue(null, (Func<SafeFileHandle, SafeFileHandle, string, int>)((file, parent, name) =>
            {
                var status = (int)rename.Invoke(null, new object[] { file, parent, name })!;
                Assert.True(status >= 0, $"Fixture rename failed: 0x{unchecked((uint)status):X8}");
                renamed = true;
                return unchecked((int)0xC0000001);
            }));
            occupiedHook.SetValue(null, (Func<string, bool>)(path =>
            {
                if (renamed && string.Equals(path, fixture.ReceiptPath, StringComparison.OrdinalIgnoreCase))
                {
                    probeCalled = true;
                    if (fault == "occupied-throws") throw new IOException("Synthetic receipt probe failure");
                    return true;
                }
                return File.Exists(path) || Directory.Exists(path);
            }));
            metadataHook.SetValue(null, (Func<bool>)(() =>
            {
                metadataCalled = true;
                return true;
            }));

            var outcome = await Upgrade(client, fixture.Home, fixture.DebuggerPath);

            Assert.True(renamed);
            Assert.True(probeCalled);
            Assert.Equal(fault == "metadata-fails", metadataCalled);
            Assert.Equal("Unavailable", outcome);
            Assert.True(File.Exists(fixture.ReceiptPath));
            fixture.AssertUnchanged(before, allowReceipt: true);
            using var receipt = JsonDocument.Parse(File.ReadAllBytes(fixture.ReceiptPath));
            Assert.Equal("netcoredbg-mcp.native-debugger-install/1", receipt.RootElement.GetProperty("schema").GetString());
            Assert.Equal(new[] { Api, Download }, handler.Requests);
        }
        finally
        {
            renameHook.SetValue(null, null);
            occupiedHook.SetValue(null, null);
            metadataHook.SetValue(null, null);
        }
    }

    private static async Task<string> Upgrade(HttpClient client, string home, string debuggerPath, CancellationToken token = default)
    {
        var assembly = Assembly.LoadFrom(TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"));
        var type = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeLegacyReceiptMigration");
        Assert.True(type is not null, "NativeLegacyReceiptMigration is not implemented yet.");
        var method = type!.GetMethod("TryUpgradeAsync", BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.Public,
            null, new[] { typeof(HttpClient), typeof(string), typeof(string), typeof(CancellationToken) }, null);
        Assert.True(method is not null, "Expected TryUpgradeAsync(HttpClient, string, string, CancellationToken).");
        var result = method!.Invoke(null, new object[] { client, home, debuggerPath, token });
        await Assert.IsAssignableFrom<Task>(result);
        return result!.GetType().GetProperty("Result")?.GetValue(result)?.ToString() ?? "no outcome";
    }

    private static string Hash(byte[] bytes) => Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();

    private sealed class Fixture : IDisposable
    {
        public string Home { get; } = ModernMcpScratchDirectory.Create();
        public string Install => Path.Combine(Home, ".netcoredbg-mcp", "netcoredbg");
        public string ConfigPath => Path.Combine(Home, ".netcoredbg-mcp", "config.json");
        public string DebuggerPath => Path.Combine(Install, "netcoredbg", "netcoredbg.exe");
        public string LibraryPath => Path.Combine(Install, "netcoredbg", "ManagedPart.dll");
        public string ShimPath => Path.Combine(Install, "netcoredbg", "dbgshim.dll");
        public string ReceiptPath => Path.Combine(Install, Receipt);
        public byte[] Archive { get; }

        public Fixture(string archiveFault = "")
        {
            Directory.CreateDirectory(Path.GetDirectoryName(DebuggerPath)!);
            File.WriteAllBytes(DebuggerPath, Debugger);
            File.WriteAllBytes(LibraryPath, Library);
            File.WriteAllBytes(ShimPath, SwitchedShim);
            File.WriteAllText(Path.Combine(Home, ".netcoredbg-mcp", "unrelated.txt"), "keep");
            Config();
            using var buffer = new MemoryStream();
            using (var zip = new ZipArchive(buffer, ZipArchiveMode.Create, leaveOpen: true))
            {
                Add(zip, "netcoredbg/netcoredbg.exe", Debugger);
                Add(zip, "netcoredbg/ManagedPart.dll", Library);
                Add(zip, "netcoredbg/dbgshim.dll", OfficialShim);
                if (archiveFault == "duplicate") Add(zip, "netcoredbg/ManagedPart.dll", Library);
                if (archiveFault == "traversal") Add(zip, "netcoredbg/../../escape.dll", Library);
                if (archiveFault == "symlink")
                {
                    var entry = zip.CreateEntry("netcoredbg/link.dll");
                    entry.ExternalAttributes = unchecked((int)0xA1FF0000);
                    using var stream = entry.Open();
                    stream.Write(Encoding.UTF8.GetBytes("ManagedPart.dll"));
                }
            }
            Archive = buffer.ToArray();
        }

        public void Config(string source = "github", string tag = Tag, string? path = null) =>
            File.WriteAllText(ConfigPath, JsonSerializer.Serialize(new { netcoredbg = new { version = tag, source, path = path ?? DebuggerPath }, unrelated = "keep" }));

        public Dictionary<string, byte[]> Snapshot() => Directory.GetFiles(Home, "*", SearchOption.AllDirectories)
            .ToDictionary(path => Path.GetRelativePath(Home, path), File.ReadAllBytes, StringComparer.OrdinalIgnoreCase);

        public void AssertUnchanged(Dictionary<string, byte[]> before, bool allowReceipt)
        {
            var after = Snapshot();
            if (allowReceipt) Assert.True(after.Remove(Path.GetRelativePath(Home, ReceiptPath)));
            Assert.Equal(before.Keys.OrderBy(x => x, StringComparer.OrdinalIgnoreCase), after.Keys.OrderBy(x => x, StringComparer.OrdinalIgnoreCase));
            foreach (var (path, bytes) in before) Assert.Equal(bytes, after[path]);
        }

        public void Dispose() => ModernMcpScratchDirectory.DeleteAsync(Home).GetAwaiter().GetResult();

        private static void Add(ZipArchive zip, string path, byte[] bytes)
        {
            using var stream = zip.CreateEntry(path).Open();
            stream.Write(bytes);
        }
    }

    private sealed class ReleaseHandler(byte[] archive, string fault = "") : HttpMessageHandler
    {
        public List<string> Requests { get; } = new();
        public TaskCompletionSource<bool> RequestStarted { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            var uri = request.RequestUri!.AbsoluteUri;
            Requests.Add(uri);
            RequestStarted.TrySetResult(true);
            Assert.Equal(HttpMethod.Get, request.Method);
            if (fault == "offline") throw new HttpRequestException("synthetic offline");
            if (fault == "stall") await Task.Delay(Timeout.InfiniteTimeSpan, cancellationToken);
            if (uri == Api)
            {
                var digest = fault == "null-digest" ? null : "sha256:" + (fault == "bad-digest" ? new string('0', 64) : Hash(archive));
                var manifest = JsonSerializer.Serialize(new
                {
                    tag_name = fault == "wrong-release-tag" ? "different" : Tag,
                    assets = new[] { new { name = Asset, size = archive.Length + (fault == "bad-size" ? 1 : 0), digest,
                        browser_download_url = fault == "wrong-asset-url" ? "https://example.com/" + Asset : Download } },
                });
                return new HttpResponseMessage(HttpStatusCode.OK) { Content = new StringContent(manifest, Encoding.UTF8, "application/json") };
            }
            if (uri == Download) return new HttpResponseMessage(HttpStatusCode.OK) { Content = new ByteArrayContent(archive) };
            throw new HttpRequestException("Unexpected request: " + uri);
        }
    }
}
