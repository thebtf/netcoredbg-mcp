using System.Diagnostics;
using System.IO.Compression;
using System.Net;
using System.Net.Sockets;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Preview.Tests;

public sealed class PreviewArtifactConsumerTests
{
    private const string ArtifactPrefix = "netcoredbg-mcp-stateless-preview-win-x64-";
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(5);
    private static readonly TimeSpan TransportOpenProbeTimeout = TimeSpan.FromMilliseconds(250);

    [Fact]
    public async Task VerifiedExtractedArtifact_DiscoverListAndCall_StaysOpenAndExitsCleanlyAfterStdinCloses()
    {
        using var artifact = VerifiedPreviewArtifact.Extract();

        await AssertMcpConsumerContractAsync(artifact.ExecutablePath);
        await AssertCleanEofAsync(artifact.ExecutablePath);
    }

    [Fact]
    public async Task LoopbackDownloadedArtifact_DiscoverListAndCall_StaysOpenAndExitsCleanlyAfterStdinCloses()
    {
        await using var server = new LoopbackArtifactServer(ArtifactInputDirectory);
        await server.DownloadAsync();
        using var artifact = VerifiedPreviewArtifact.Extract(server.DownloadDirectory);

        await AssertMcpConsumerContractAsync(artifact.ExecutablePath);
        await AssertCleanEofAsync(artifact.ExecutablePath);
    }

    [Fact]
    public async Task LoopbackDownloadedArtifact_TamperedArchive_RefusesIdentityBeforeLaunch()
    {
        await using var server = new LoopbackArtifactServer(ArtifactInputDirectory, tamperArchive: true);
        await server.DownloadAsync();

        Assert.Throws<Xunit.Sdk.EqualException>(() =>
        {
            using var artifact = VerifiedPreviewArtifact.Extract(server.DownloadDirectory);
        });
    }

    private static async Task AssertMcpConsumerContractAsync(string executablePath)
    {
        await using var driver = await PreviewMcpProcessDriver.StartVerifiedExtractedExecutableAsync(
            executablePath,
            PreviewRepositoryLayout.FixtureRoot);

        var discovery = RequireResult(await driver.DiscoverAsync(new RequestId("artifact-discover")));
        Assert.Equal(["tools"], discovery["capabilities"]!.AsObject().Select(static property => property.Key));

        var catalog = RequireResult(await driver.ListToolsAsync(new RequestId("artifact-list")));
        var tool = Assert.IsType<JsonObject>(Assert.Single(catalog["tools"]!.AsArray()));
        Assert.Equal("find_code_symbol", tool["name"]!.GetValue<string>());

        var call = RequireResult(await driver.CallToolAsync(
            "find_code_symbol",
            new JsonObject
            {
                ["name"] = "PreviewMarker",
                ["kind"] = "class",
            },
            new RequestId("artifact-call")));
        Assert.Equal("complete", call["resultType"]!.GetValue<string>());
        Assert.False(call["isError"]!.GetValue<bool>());
        var structured = Assert.IsType<JsonObject>(call["structuredContent"]);
        Assert.Equal("find_code_symbol_success", structured["kind"]!.GetValue<string>());
        var match = Assert.IsType<JsonObject>(Assert.Single(structured["results"]!.AsArray()));
        Assert.Equal("Markers.cs", match["file"]!.GetValue<string>());
        Assert.Equal(3, match["line"]!.GetValue<int>());
        Assert.Equal("PreviewMarker", match["name"]!.GetValue<string>());
        Assert.Equal("class", match["kind"]!.GetValue<string>());
        Assert.Equal("public sealed class PreviewMarker { }", match["context"]!.GetValue<string>());
        Assert.False(
            await driver.WaitForTransportClosureAsync(TransportOpenProbeTimeout),
            "The extracted artifact emitted non-JSON-RPC stdout or terminated its stdio transport during a valid consumer exchange.");
    }

    private static async Task AssertCleanEofAsync(string executablePath)
    {
        using var process = StartProcess(executablePath);
        var standardOutput = process.StandardOutput.ReadToEndAsync();
        var standardError = process.StandardError.ReadToEndAsync();
        try
        {
            process.StandardInput.Close();
            await process.WaitForExitAsync().WaitAsync(RequestTimeout);

            Assert.Equal(0, process.ExitCode);
            Assert.Equal(string.Empty, await standardOutput);
            Assert.Equal(string.Empty, await standardError);
        }
        finally
        {
            if (!process.HasExited)
            {
                process.Kill(entireProcessTree: true);
                await process.WaitForExitAsync();
            }
        }
    }

    private static Process StartProcess(string executablePath)
    {
        var start = new ProcessStartInfo(executablePath)
        {
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WorkingDirectory = Path.GetDirectoryName(executablePath)
                ?? throw new InvalidOperationException("Extracted preview executable has no parent directory."),
        };
        start.ArgumentList.Add("--project");
        start.ArgumentList.Add(PreviewRepositoryLayout.FixtureRoot);
        return Process.Start(start) ?? throw new InvalidOperationException("Extracted preview artifact did not start.");
    }

    private static JsonObject RequireResult(JsonRpcMessage message) =>
        Assert.IsType<JsonObject>(Assert.IsType<JsonRpcResponse>(message).Result);

    private static string RequiredString(JsonElement value, string propertyName) =>
        Assert.IsType<string>(value.GetProperty(propertyName).GetString());

    private static string ArtifactInputDirectory =>
        Environment.GetEnvironmentVariable("NETCOREDBG_PREVIEW_ARTIFACT_ROOT")
        ?? Path.Combine(PreviewRepositoryLayout.Root, "artifacts", "stateless-preview");

    // Controlled HTTP transfer of source-run fixtures, not a public GitHub release proof.
    private sealed class LoopbackArtifactServer : IAsyncDisposable
    {
        private readonly TcpListener _listener = new(IPAddress.Loopback, 0);
        private readonly CancellationTokenSource _stop = new();
        private readonly Task _serve;
        private readonly string _manifestPath;
        private readonly string _archivePath;
        private readonly byte[] _manifestBytes;
        private readonly bool _tamperArchive;

        internal LoopbackArtifactServer(string sourceDirectory, bool tamperArchive = false)
        {
            _manifestPath = Assert.Single(Directory.EnumerateFiles(
                sourceDirectory, $"{ArtifactPrefix}*.manifest.json", SearchOption.TopDirectoryOnly));
            _manifestBytes = File.ReadAllBytes(_manifestPath);
            using var manifest = JsonDocument.Parse(_manifestBytes);
            ArchiveName = RequiredString(manifest.RootElement.GetProperty("archive"), "name");
            Assert.Equal(Path.GetFileName(ArchiveName), ArchiveName);
            _archivePath = Path.Combine(sourceDirectory, ArchiveName);
            Assert.True(File.Exists(_archivePath), $"Controlled fixture archive is absent: '{_archivePath}'.");
            _tamperArchive = tamperArchive;
            DownloadDirectory = Path.Combine(Path.GetTempPath(), $"netcoredbg-preview-download-{Guid.NewGuid():N}");
            Directory.CreateDirectory(DownloadDirectory);
            try
            {
                _listener.Start();
                _stop.CancelAfter(TimeSpan.FromSeconds(60));
                _serve = ServeAsync();
            }
            catch
            {
                _listener.Stop();
                _stop.Dispose();
                Directory.Delete(DownloadDirectory, recursive: true);
                throw;
            }
        }

        internal string DownloadDirectory { get; }

        internal string ArchiveName { get; }

        internal async Task DownloadAsync()
        {
            var endpoint = (IPEndPoint)_listener.LocalEndpoint;
            using var handler = new HttpClientHandler
            {
                UseProxy = false,
                AllowAutoRedirect = false,
                UseCookies = false,
                UseDefaultCredentials = false,
            };
            using var client = new HttpClient(handler)
            {
                BaseAddress = new Uri($"http://127.0.0.1:{endpoint.Port}/"),
                Timeout = TimeSpan.FromSeconds(60),
            };
            foreach (var name in new[] { Path.GetFileName(_manifestPath), ArchiveName })
            {
                using var response = await client.GetAsync(name, HttpCompletionOption.ResponseHeadersRead);
                Assert.Equal(HttpStatusCode.OK, response.StatusCode);
                await using var destination = new FileStream(
                    Path.Combine(DownloadDirectory, name), FileMode.CreateNew, FileAccess.Write, FileShare.None);
                await response.Content.CopyToAsync(destination, _stop.Token);
                Assert.Equal(response.Content.Headers.ContentLength, destination.Length);
            }

            await _serve.WaitAsync(TimeSpan.FromSeconds(60));
            var receivedManifest = File.ReadAllBytes(Path.Combine(DownloadDirectory, Path.GetFileName(_manifestPath)));
            Assert.Equal(_manifestBytes.Length, receivedManifest.Length);
            Assert.Equal(SHA256.HashData(_manifestBytes), SHA256.HashData(receivedManifest));
        }

        private async Task ServeAsync()
        {
            foreach (var path in new[] { _manifestPath, _archivePath })
            {
                using var connection = await _listener.AcceptTcpClientAsync(_stop.Token);
                await using var stream = connection.GetStream();
                using var reader = new StreamReader(
                    stream, Encoding.ASCII, detectEncodingFromByteOrderMarks: false, bufferSize: 1024, leaveOpen: true);
                Assert.Equal($"GET /{Path.GetFileName(path)} HTTP/1.1", await reader.ReadLineAsync(_stop.Token));
                string? header;
                while (!string.IsNullOrEmpty(header = await reader.ReadLineAsync(_stop.Token)))
                {
                    Assert.False(header.StartsWith("Authorization:", StringComparison.OrdinalIgnoreCase));
                    Assert.False(header.StartsWith("Proxy-Authorization:", StringComparison.OrdinalIgnoreCase));
                    Assert.False(header.StartsWith("Cookie:", StringComparison.OrdinalIgnoreCase));
                }

                await using var source = File.OpenRead(path);
                var headers = Encoding.ASCII.GetBytes(
                    $"HTTP/1.1 200 OK\r\nContent-Length: {source.Length}\r\nConnection: close\r\n\r\n");
                await stream.WriteAsync(headers, _stop.Token);
                if (_tamperArchive && path == _archivePath)
                {
                    var firstByte = source.ReadByte();
                    Assert.NotEqual(-1, firstByte);
                    await stream.WriteAsync(new[] { (byte)(firstByte ^ 1) }, _stop.Token);
                }
                await source.CopyToAsync(stream, _stop.Token);
            }
        }

        public async ValueTask DisposeAsync()
        {
            await _stop.CancelAsync();
            _listener.Stop();
            try
            {
                await _serve;
            }
            catch (OperationCanceledException) when (_stop.IsCancellationRequested)
            {
            }
            finally
            {
                _stop.Dispose();
                Directory.Delete(DownloadDirectory, recursive: true);
            }
        }
    }

    private sealed class VerifiedPreviewArtifact : IDisposable
    {
        private VerifiedPreviewArtifact(string extractionDirectory, string executablePath)
        {
            ExtractionDirectory = extractionDirectory;
            ExecutablePath = executablePath;
        }

        internal string ExtractionDirectory { get; }

        internal string ExecutablePath { get; }

        internal static VerifiedPreviewArtifact Extract() => Extract(ArtifactInputDirectory);

        internal static VerifiedPreviewArtifact Extract(string artifactDirectory)
        {
            Assert.True(
                Directory.Exists(artifactDirectory),
                $"Required source-run preview artifact input is absent: '{artifactDirectory}'. Consumer proof must use a downloaded archive and manifest, never repository bin output.");

            var manifestPaths = Directory
                .EnumerateFiles(artifactDirectory, $"{ArtifactPrefix}*.manifest.json", SearchOption.TopDirectoryOnly)
                .ToArray();
            var manifestPath = Assert.Single(manifestPaths);
            using var manifestDocument = JsonDocument.Parse(File.ReadAllText(manifestPath));
            var manifest = manifestDocument.RootElement;
            var version = RequiredString(manifest, "version");
            Assert.Equal("1.0", RequiredString(manifest, "schema_version"));
            Assert.Equal("win-x64", RequiredString(manifest, "rid"));
            Assert.Equal($"stateless-preview-v{version}", RequiredString(manifest, "tag"));
            Assert.Matches("^[0-9a-f]{40}$", RequiredString(manifest, "commit"));

            var archive = manifest.GetProperty("archive");
            var archiveName = RequiredString(archive, "name");
            Assert.Equal($"{ArtifactPrefix}{version}.zip", archiveName);
            Assert.Equal($"{ArtifactPrefix}{version}.manifest.json", Path.GetFileName(manifestPath));
            var archivePath = Path.Combine(artifactDirectory, archiveName);
            Assert.True(File.Exists(archivePath), $"Manifest-declared source-run archive is absent: '{archivePath}'.");
            AssertHasManifestDigest(archivePath, archive);

            var executable = manifest.GetProperty("executable");
            var executableName = RequiredString(executable, "name");
            Assert.Equal("netcoredbg-mcp-stateless-preview.exe", executableName);

            var extractionDirectory = Path.Combine(Path.GetTempPath(), $"netcoredbg-preview-artifact-{Guid.NewGuid():N}");
            Directory.CreateDirectory(extractionDirectory);
            try
            {
                ZipFile.ExtractToDirectory(archivePath, extractionDirectory);
                var executablePath = Assert.Single(Directory.EnumerateFiles(
                    extractionDirectory,
                    executableName,
                    SearchOption.AllDirectories));
                AssertHasManifestDigest(executablePath, executable);
                Assert.False(
                    IsWithinDirectory(
                        executablePath,
                        Path.Combine(
                            PreviewRepositoryLayout.Root,
                            "host",
                            "NetCoreDbg.Mcp.Stateless.Preview",
                            "bin")),
                    "Consumer proof must launch bytes extracted from the verified archive, not repository build output.");
                return new VerifiedPreviewArtifact(extractionDirectory, executablePath);
            }
            catch
            {
                Directory.Delete(extractionDirectory, recursive: true);
                throw;
            }
        }

        public void Dispose()
        {
            if (Directory.Exists(ExtractionDirectory))
            {
                Directory.Delete(ExtractionDirectory, recursive: true);
            }
        }

        private static void AssertHasManifestDigest(string path, JsonElement manifestEntry)
        {
            Assert.Equal(manifestEntry.GetProperty("size_bytes").GetInt64(), new FileInfo(path).Length);
            using var stream = File.OpenRead(path);
            var actualSha256 = Convert.ToHexString(SHA256.HashData(stream)).ToLowerInvariant();
            Assert.Equal(RequiredString(manifestEntry, "sha256"), actualSha256);
        }

        private static bool IsWithinDirectory(string candidatePath, string directoryPath)
        {
            var relativePath = Path.GetRelativePath(Path.GetFullPath(directoryPath), Path.GetFullPath(candidatePath));
            return !Path.IsPathRooted(relativePath) &&
                   relativePath != ".." &&
                   !relativePath.StartsWith($"..{Path.DirectorySeparatorChar}", StringComparison.Ordinal);
        }
    }
}
