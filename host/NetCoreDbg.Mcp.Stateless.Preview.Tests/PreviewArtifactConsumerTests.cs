using System.Diagnostics;
using System.IO.Compression;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Preview.Tests;

public sealed class PreviewArtifactConsumerTests
{
    private const string ArtifactPrefix = "netcoredbg-mcp-stateless-preview-win-x64-";
    private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(5);

    [Fact]
    public async Task VerifiedExtractedArtifact_UsesOnlyJsonRpcStdout_AndExitsCleanlyAfterStdinCloses()
    {
        using var artifact = VerifiedPreviewArtifact.Extract();

        await AssertJsonRpcOnlyStdoutAsync(artifact.ExecutablePath);
        await AssertCleanEofAsync(artifact.ExecutablePath);
    }

    private static async Task AssertJsonRpcOnlyStdoutAsync(string executablePath)
    {
        var transport = new StdioClientTransport(new StdioClientTransportOptions
        {
            Command = executablePath,
            Arguments = ["--project", PreviewRepositoryLayout.FixtureRoot],
            Name = "netcoredbg-mcp-stateless-preview-artifact-consumer",
            WorkingDirectory = Path.GetDirectoryName(executablePath)
                ?? throw new InvalidOperationException("Extracted preview executable has no parent directory."),
            ShutdownTimeout = TimeSpan.FromSeconds(2),
        });
        var connection = await transport.ConnectAsync();
        try
        {
            var requestId = new RequestId("artifact-stdout-purity");
            using var deadline = new CancellationTokenSource(RequestTimeout);
            await connection.SendMessageAsync(
                new JsonRpcRequest
                {
                    Id = requestId,
                    Method = "tools/list",
                    Params = new JsonObject { ["_meta"] = CurrentMetadata() },
                },
                deadline.Token);

            var response = Assert.IsType<JsonRpcResponse>(await ReadResponseAsync(connection, requestId, deadline.Token));
            var result = Assert.IsType<JsonObject>(response.Result);
            var tool = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["tools"])));
            Assert.Equal("find_code_symbol", Assert.IsAssignableFrom<JsonValue>(tool["name"]).GetValue<string>());
            Assert.False(
                connection.MessageReader.Completion.IsCompleted,
                "The extracted artifact emitted non-JSON-RPC stdout or terminated its stdio transport during a valid consumer exchange.");
        }
        finally
        {
            await connection.DisposeAsync();
        }
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

    private static JsonObject CurrentMetadata() => new()
    {
        [MetaKeys.ProtocolVersion] = "2026-07-28",
        [MetaKeys.ClientInfo] = new JsonObject { ["name"] = "preview-artifact-consumer-tests", ["version"] = "1.0" },
        [MetaKeys.ClientCapabilities] = new JsonObject(),
    };

    private static async Task<JsonRpcMessage> ReadResponseAsync(
        ITransport connection,
        RequestId requestId,
        CancellationToken cancellationToken)
    {
        while (true)
        {
            var message = await connection.MessageReader.ReadAsync(cancellationToken);
            if (message is JsonRpcMessageWithId correlated && correlated.Id == requestId)
            {
                return message;
            }
        }
    }

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
