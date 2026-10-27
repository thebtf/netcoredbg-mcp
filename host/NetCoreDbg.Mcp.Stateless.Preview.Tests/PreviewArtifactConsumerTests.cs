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
        await AssertRequiredMetadataVersionRecoveryAndCleanEofAsync(artifact.ExecutablePath);
    }

    [Fact]
    public async Task LoopbackDownloadedArtifact_DiscoverListAndCall_StaysOpenAndExitsCleanlyAfterStdinCloses()
    {
        await using var server = new LoopbackArtifactServer(ArtifactInputDirectory);
        await server.DownloadAsync();
        using var artifact = VerifiedPreviewArtifact.Extract(server.DownloadDirectory);

        await AssertMcpConsumerContractAsync(artifact.ExecutablePath);
        await AssertRequiredMetadataVersionRecoveryAndCleanEofAsync(artifact.ExecutablePath);
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
        var metadata = new JsonObject
        {
            [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion,
            [MetaKeys.ClientCapabilities] = new JsonObject(),
        };
        await using (var first = await PreviewMcpProcessDriver.StartVerifiedExtractedExecutableAsync(
            executablePath, PreviewRepositoryLayout.FixtureRoot))
        {
            AssertMarkerResult(RequireResult(await first.CallToolAsync(
                "find_code_symbol",
                new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "class" },
                new RequestId("artifact-first-without-client-info"), metadata)));
        }

        await using var driver = await PreviewMcpProcessDriver.StartVerifiedExtractedExecutableAsync(
            executablePath,
            PreviewRepositoryLayout.FixtureRoot);

        var discovery = RequireResult(await driver.SendAsync(
            "server/discover", new JsonObject { ["_meta"] = metadata.DeepClone() },
            new RequestId("artifact-discover")));
        Assert.Equal(["tools"], discovery["capabilities"]!.AsObject().Select(static property => property.Key));
        Assert.Equal(["2026-07-28"], discovery["supportedVersions"]!.AsArray().Select(static value => value!.GetValue<string>()));
        Assert.False(discovery["capabilities"]!["tools"]!["listChanged"]!.GetValue<bool>());
        Assert.Equal(300_000L, discovery["ttlMs"]!.GetValue<long>());
        Assert.Equal("public", discovery["cacheScope"]!.GetValue<string>());

        var catalog = RequireResult(await driver.ListToolsAsync(new RequestId("artifact-list"), metadata));
        Assert.Equal(["find_code_symbol", "find_code_references", "get_source_context"],
            catalog["tools"]!.AsArray().Select(static tool => tool!["name"]!.GetValue<string>()));

        var call = RequireResult(await driver.CallToolAsync(
            "find_code_symbol",
            new JsonObject
            {
                ["name"] = "PreviewMarker",
                ["kind"] = "class",
            },
            new RequestId("artifact-call"), metadata));
        AssertMarkerResult(call);
        AssertMarkerResult(RequireResult(await driver.CallToolAsync(
            "find_code_symbol",
            new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "class" },
            new RequestId("artifact-with-client-info"))));
        AssertMarkerResult(RequireResult(await driver.CallToolAsync(
            "find_code_symbol",
            new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "class" },
            new RequestId("artifact-without-client-info-again"), metadata)));

        var references = RequireResult(await driver.CallToolAsync("find_code_references",
            new JsonObject { ["name"] = "PreviewMarker" }, new RequestId("artifact-references")));
        Assert.Equal("complete", references["resultType"]!.GetValue<string>());
        Assert.False(references["isError"]!.GetValue<bool>());
        Assert.Equal("""{"kind":"find_code_references_success","results":[{"file":"Markers.cs","line":3,"context":"public sealed class PreviewMarker { }"},{"file":"PreviewReferences.cs","line":5,"context":"public PreviewMarker Value { get; } = new PreviewMarker();"}]}""",
            references["structuredContent"]!.ToJsonString());
        Assert.Equal(references["structuredContent"]!.ToJsonString(), SingleText(references));

        var context = RequireResult(await driver.CallToolAsync("get_source_context",
            new JsonObject { ["file"] = "PreviewReferences.cs", ["line"] = 5, ["radius"] = 1 }, new RequestId("artifact-context")));
        Assert.Equal("complete", context["resultType"]!.GetValue<string>());
        Assert.False(context["isError"]!.GetValue<bool>());
        Assert.Equal("""{"kind":"get_source_context_success","file":"PreviewReferences.cs","start_line":4,"end_line":6,"lines":[{"line":4,"text":"{"},{"line":5,"text":"    public PreviewMarker Value { get; } = new PreviewMarker();"},{"line":6,"text":"}"}]}""",
            context["structuredContent"]!.ToJsonString());
        Assert.Equal(context["structuredContent"]!.ToJsonString(), SingleText(context));
        Assert.False(
            await driver.WaitForTransportClosureAsync(TransportOpenProbeTimeout),
            "The extracted artifact emitted non-JSON-RPC stdout or terminated its stdio transport during a valid consumer exchange.");
    }

    private static async Task AssertRequiredMetadataVersionRecoveryAndCleanEofAsync(string executablePath)
    {
        using var process = StartProcess(executablePath);
        var standardError = process.StandardError.ReadToEndAsync();
        try
        {
            var legacyId = new RequestId("artifact-legacy-initialize");
            var legacy = Assert.IsType<JsonRpcError>(await SendAsync(legacyId, "initialize", new JsonObject()));
            Assert.Equal(legacyId, legacy.Id);
            Assert.Equal(-32601, legacy.Error.Code);
            Assert.Equal("Only MCP 2026-07-28 is supported. Use server/discover and per-request metadata instead.", legacy.Error.Message);

            var missingId = new RequestId(51);
            var missing = Assert.IsType<JsonRpcError>(await CallAsync(missingId, metadata: null));
            Assert.Equal(missingId, missing.Id);
            Assert.Equal(-32602, missing.Error.Code);

            var metadata = new JsonObject
            {
                [MetaKeys.ProtocolVersion] = PreviewMcpProcessDriver.CurrentProtocolVersion,
                [MetaKeys.ClientCapabilities] = new JsonObject(),
            };
            var discovery = RequireResult(await SendAsync(new RequestId("artifact-legacy-discover"), "server/discover",
                new JsonObject { ["_meta"] = metadata.DeepClone() }));
            Assert.Equal(["2026-07-28"], discovery["supportedVersions"]!.AsArray().Select(static value => value!.GetValue<string>()));
            var capabilities = discovery["capabilities"]!.AsObject();
            Assert.Equal(["tools"], capabilities.Select(static property => property.Key));
            Assert.Equal(["listChanged"], capabilities["tools"]!.AsObject().Select(static property => property.Key));
            Assert.False(capabilities["tools"]!["listChanged"]!.GetValue<bool>());
            Assert.Equal(300_000L, discovery["ttlMs"]!.GetValue<long>());
            Assert.Equal("public", discovery["cacheScope"]!.GetValue<string>());
            var catalog = RequireResult(await SendAsync(new RequestId("artifact-legacy-list"), "tools/list",
                new JsonObject { ["_meta"] = metadata.DeepClone() }));
            Assert.Equal(["find_code_symbol", "find_code_references", "get_source_context"],
                catalog["tools"]!.AsArray().Select(static tool => tool!["name"]!.GetValue<string>()));
            var recoveredId = new RequestId("artifact-required-metadata-recovered");
            var recovered = await CallAsync(recoveredId, metadata);
            Assert.Equal(recoveredId, Assert.IsType<JsonRpcResponse>(recovered).Id);
            AssertMarkerResult(RequireResult(recovered));

            var modernId = new RequestId(53);
            var modern = Assert.IsType<JsonRpcError>(await SendAsync(modernId, "initialize",
                new JsonObject { ["_meta"] = metadata.DeepClone() }));
            Assert.Equal(modernId, modern.Id);
            Assert.Equal(-32601, modern.Error.Code);
            Assert.Equal("Method 'initialize' is not available on protocol version '2026-07-28'. Use 'server/discover' and per-request metadata instead.", modern.Error.Message);

            const string requestedVersion = "1900-01-01";
            var unsupportedMetadata = metadata.DeepClone().AsObject();
            unsupportedMetadata[MetaKeys.ProtocolVersion] = requestedVersion;
            var unsupportedId = new RequestId(52);
            var unsupported = Assert.IsType<JsonRpcError>(await CallAsync(unsupportedId, unsupportedMetadata));
            Assert.Equal(unsupportedId, unsupported.Id);
            Assert.Equal(-32022, unsupported.Error.Code);
            Assert.Equal("Unsupported protocol version", unsupported.Error.Message);
            var data = Assert.IsType<JsonElement>(unsupported.Error.Data);
            Assert.Equal(["requested", "supported"], data.EnumerateObject().Select(static property => property.Name).Order());
            Assert.Equal(requestedVersion, data.GetProperty("requested").GetString());
            Assert.Equal(["2026-07-28"], data.GetProperty("supported").EnumerateArray().Select(static value => value.GetString()));

            var correctedId = new RequestId("artifact-version-recovered");
            var corrected = await CallAsync(correctedId, metadata);
            Assert.Equal(correctedId, Assert.IsType<JsonRpcResponse>(corrected).Id);
            AssertMarkerResult(RequireResult(corrected));

            process.StandardInput.Close();
            await process.WaitForExitAsync().WaitAsync(RequestTimeout);

            Assert.Equal(0, process.ExitCode);
            Assert.Equal(string.Empty, await process.StandardOutput.ReadToEndAsync().WaitAsync(RequestTimeout));
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

        async Task<JsonRpcMessage> CallAsync(RequestId id, JsonObject? metadata)
        {
            var parameters = new JsonObject
            {
                ["name"] = "find_code_symbol",
                ["arguments"] = new JsonObject { ["name"] = "PreviewMarker", ["kind"] = "class" },
            };
            if (metadata is not null)
            {
                parameters["_meta"] = metadata.DeepClone();
            }
            return await SendAsync(id, "tools/call", parameters);
        }

        async Task<JsonRpcMessage> SendAsync(RequestId id, string method, JsonObject parameters)
        {
            var request = new JsonRpcRequest { Id = id, Method = method, Params = parameters };
            await process.StandardInput.WriteLineAsync(JsonSerializer.Serialize<JsonRpcMessage>(request, McpJsonUtilities.DefaultOptions));
            await process.StandardInput.FlushAsync();
            var line = await process.StandardOutput.ReadLineAsync().WaitAsync(RequestTimeout);
            Assert.True(line is not null, $"Extracted preview reached EOF before the correlated response for '{id}'.");
            Assert.InRange(Encoding.UTF8.GetByteCount(line!), 1, 256 * 1024);
            var response = Assert.IsAssignableFrom<JsonRpcMessageWithId>(JsonSerializer.Deserialize<JsonRpcMessage>(line!, McpJsonUtilities.DefaultOptions));
            Assert.Equal(id, response.Id);
            return response;
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

    private static void AssertMarkerResult(JsonObject call)
    {
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
        Assert.Equal(structured.ToJsonString(), SingleText(call));
    }

    private static JsonObject RequireResult(JsonRpcMessage message) =>
        Assert.IsType<JsonObject>(Assert.IsType<JsonRpcResponse>(message).Result);

    private static string SingleText(JsonObject result)
    {
        var content = Assert.IsType<JsonObject>(Assert.Single(result["content"]!.AsArray()));
        Assert.Equal("text", content["type"]!.GetValue<string>());
        return content["text"]!.GetValue<string>();
    }

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
            Assert.Equal("2.0", RequiredString(manifest, "schema_version"));
            var consumerContract = manifest.GetProperty("consumer_contract");
            Assert.Equal(["scenario_catalog_id", "scenario_catalog_sha256", "tools"],
                consumerContract.EnumerateObject().Select(static property => property.Name).Order());
            Assert.Equal("a3-preview-legacy-diagnostic-v1", RequiredString(consumerContract, "scenario_catalog_id"));
            Assert.Equal("31589f89efc7c0453357bd3cee7216448ec1332e57d135d555b2c897a0bc855d",
                RequiredString(consumerContract, "scenario_catalog_sha256"));
            Assert.Equal(["find_code_symbol", "find_code_references", "get_source_context"],
                consumerContract.GetProperty("tools").EnumerateArray().Select(static tool => tool.GetString()));
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

            var extractionDirectory = Path.Combine(Path.GetTempPath(), $"preview-{Guid.NewGuid():N}");
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
