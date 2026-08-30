using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
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

    private sealed class VerifiedPreviewArtifact : IDisposable
    {
        private VerifiedPreviewArtifact(string extractionDirectory, string executablePath)
        {
            ExtractionDirectory = extractionDirectory;
            ExecutablePath = executablePath;
        }

        internal string ExtractionDirectory { get; }

        internal string ExecutablePath { get; }

        internal static VerifiedPreviewArtifact Extract()
        {
            var artifactDirectory = Path.Combine(PreviewRepositoryLayout.Root, "artifacts", "stateless-preview");
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
