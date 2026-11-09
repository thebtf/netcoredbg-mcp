using System.Security.Cryptography;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeShimLaunchTests
{
    private static readonly byte[] Original = NativeShimPeFixture.Create(true, 1);
    private static readonly byte[] Lower = NativeShimPeFixture.Create(true, 2);
    private static readonly byte[] Higher = NativeShimPeFixture.Create(true, 3);

    [Theory]
    [InlineData("legacy-match")]
    [InlineData("legacy-explicit-match")]
    [InlineData("legacy-shim-mutated")]
    [InlineData("legacy-frameworks-match")]
    [InlineData("legacy-malformed-version")]
    [InlineData("legacy-unrelated-framework")]
    [InlineData("legacy-no-match")]
    [InlineData("legacy-missing-runtime")]
    [InlineData("legacy-unverified")]
    [InlineData("legacy-unlisted")]
    [InlineData("legacy-changed")]
    [InlineData("legacy-identical")]
    [InlineData("modern-match")]
    public async Task Launch_UsesCachedShimOnlyForMatchingLegacyDiscovery(string scenario)
    {
        if (!OperatingSystem.IsWindows()) return;
        var scratch = ModernMcpScratchDirectory.Create();
        var startAttempted = false;
        var cleanupConfirmed = false;
        var stopSucceeded = false;
        Exception? testFailure = null;
        try
        {
            var home = Path.Combine(scratch, "home");
            var managed = Path.Combine(home, ".netcoredbg-mcp", "netcoredbg");
            var pathDirectory = Path.Combine(scratch, "path");
            Directory.CreateDirectory(pathDirectory);
            var debuggerDirectory = Path.Combine(managed, "netcoredbg");
            CopyAdapter(debuggerDirectory);
            var originalExecutable = Path.Combine(debuggerDirectory, "netcoredbg.exe");
            var originalShim = Path.Combine(debuggerDirectory, "dbgshim.dll");
            await File.WriteAllBytesAsync(originalShim, Original);
            var customSidecar = Path.Combine(debuggerDirectory, "custom-sidecar.dat");
            if (scenario == "legacy-changed") await File.WriteAllTextAsync(customSidecar, "before");
            if (scenario != "legacy-unverified") await WriteManifestAsync(managed);
            var sourceShim = scenario == "legacy-shim-mutated" ? NativeShimPeFixture.Create(true, 4) : Original;
            if (scenario == "legacy-shim-mutated") await File.WriteAllBytesAsync(originalShim, sourceShim);
            if (scenario is "legacy-unlisted" or "legacy-changed") await File.WriteAllTextAsync(customSidecar, "preserve");
            var major = scenario == "legacy-no-match" ? "12346" : "12345";
            await WriteCacheAsync(home, "12345.7.9", Lower);
            var highest = scenario == "legacy-identical" ? Original : Higher;
            await WriteCacheAsync(home, "12345.7.10", highest);
            var program = Path.Combine(scratch, "target.dll");
            await File.WriteAllBytesAsync(program, []);
            if (scenario != "legacy-missing-runtime")
            {
                var version = scenario == "legacy-malformed-version" ? major + ".foo.foo" : major + ".0.0";
                var frameworkName = scenario == "legacy-unrelated-framework" ? "Other.Framework" : "Microsoft.NETCore.App";
                var framework = new JsonObject { ["name"] = frameworkName, ["version"] = version };
                var options = scenario == "legacy-frameworks-match"
                    ? new JsonObject { ["frameworks"] = new JsonArray(new JsonObject { ["name"] = "Microsoft.WindowsDesktop.App", ["version"] = "54321.0.0" }, framework) }
                    : new JsonObject { ["framework"] = framework };
                await File.WriteAllTextAsync(Path.ChangeExtension(program, ".runtimeconfig.json"),
                    new JsonObject { ["runtimeOptions"] = options }.ToJsonString());
            }

            var legacy = scenario != "modern-match";
            var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: legacy ? "2025-11-25" : null,
                HostArguments: legacy ? ["--legacy-compat"] : [],
                InheritEnvironmentVariables: false,
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["HOME"] = home,
                    ["USERPROFILE"] = home,
                    ["HOMEDRIVE"] = null,
                    ["HOMEPATH"] = null,
                    ["PATH"] = pathDirectory,
                    ["NETCOREDBG_PATH"] = scenario is "modern-match" or "legacy-explicit-match" ? originalExecutable : null,
                    ["CONTROLLED_DAP_OBSERVE_SHIM"] = "1",
                }));
            Exception? driverFailure = null;
            try
            {
                var owner = legacy ? new JsonObject { ["muxSessionId"] = "shim-launch-test" } : ModernMcpProcessDriver.CurrentMeta();
                if (legacy)
                {
                    var inspection = await LegacyCallAsync(driver, "inspect_debug_launch_compatibility", new JsonObject { ["program"] = program }, owner);
                    var expectedCandidate = scenario switch { "legacy-no-match" => "no_match", "legacy-missing-runtime" or "legacy-malformed-version" => "not_attempted", _ => "known" };
                    Assert.Equal(expectedCandidate, inspection["data"]?["cachedCandidate"]?["status"]?.GetValue<string>());
                    if (expectedCandidate == "known")
                        Assert.Equal("12345.7.10", inspection["data"]?["cachedCandidate"]?["version"]?.GetValue<string>());
                }

                startAttempted = true;
                var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync("start_debug", new JsonObject
                {
                    ["program"] = program,
                    ["pre_build"] = false,
                    ["stop_at_entry"] = false,
                }, owner, new RequestId("shim-start"), timeout: TimeSpan.FromSeconds(15)));
                string? id = null;
                if (legacy)
                {
                    var started = Assert.IsType<JsonObject>(ModernMcpProcessDriver.LegacyPayload(result));
                    Assert.True(started["data"]?["success"]?.GetValue<bool>(), started.ToJsonString());
                }
                else
                {
                    Assert.False(result["isError"]?.GetValue<bool>() ?? true, result.ToJsonString());
                    id = Assert.IsType<string>(result["structuredContent"]?["debugSessionId"]?.GetValue<string>());
                }

                JsonObject observation;
                byte[] originalDuring;
                Exception? observationFailure = null;
                try
                {
                    var entry = Assert.Single(await driver.ReadFixtureTranscriptAsync(), static entry => entry.Kind == "shim-observation");
                    observation = Assert.IsType<JsonObject>(JsonNode.Parse(Assert.IsType<string>(entry.Arguments)));
                    originalDuring = await File.ReadAllBytesAsync(originalShim);
                    Assert.Contains(await driver.ReadNativeActionsAsync(), static action => action.Command == "launch");
                }
                catch (Exception exception) { observationFailure = exception; throw; }
                finally
                {
                    await PreserveFailureAsync(observationFailure, async () =>
                    {
                        if (legacy)
                        {
                            var stopped = await LegacyCallAsync(driver, "stop_debug", new JsonObject(), owner);
                            Assert.True(stopped["data"]?["success"]?.GetValue<bool>(), stopped.ToJsonString());
                        }
                        else await driver.StopSingleControlledSessionAsync(id!);
                        stopSucceeded = true;
                    });
                }

                var observedExecutable = Assert.IsType<string>(observation["executable"]?.GetValue<string>());
                var observedBytes = Convert.FromBase64String(Assert.IsType<string>(observation["shimBytes"]?.GetValue<string>()));
                Assert.Equal(sourceShim, originalDuring);
                Assert.Equal(sourceShim, await File.ReadAllBytesAsync(originalShim));
                if (scenario is "legacy-match" or "legacy-explicit-match" or "legacy-shim-mutated" or "legacy-frameworks-match")
                {
                    Assert.False(string.Equals(originalExecutable, observedExecutable, StringComparison.OrdinalIgnoreCase));
                    Assert.Equal(Higher, observedBytes);
                    Assert.False(File.Exists(observedExecutable));
                    Assert.False(Directory.Exists(Path.GetDirectoryName(observedExecutable)!));
                }
                else
                {
                    Assert.Equal(originalExecutable, observedExecutable, ignoreCase: true);
                    Assert.Equal(Original, observedBytes);
                    Assert.True(File.Exists(originalExecutable));
                }
                Assert.Equal(Lower, await File.ReadAllBytesAsync(CachePath(home, "12345.7.9")));
                Assert.Equal(highest, await File.ReadAllBytesAsync(CachePath(home, "12345.7.10")));
                if (scenario is "legacy-unlisted" or "legacy-changed")
                    Assert.Equal("preserve", await File.ReadAllTextAsync(customSidecar));
            }
            catch (Exception exception)
            {
                driverFailure = WithHostDiagnostics(driver, exception);
                throw driverFailure;
            }
            finally
            {
                await PreserveFailureAsync(driverFailure, async () =>
                {
                    await driver.DisposeAsync();
                    cleanupConfirmed = stopSucceeded;
                });
            }
        }
        catch (Exception exception) { testFailure = exception; throw; }
        finally
        {
            if (!startAttempted || cleanupConfirmed)
                await PreserveFailureAsync(testFailure, () => ModernMcpScratchDirectory.DeleteAsync(scratch));
        }
    }

    [Fact]
    public async Task LegacyLaunch_StageCleanupFailureRetainsOwnedStageForRetry()
    {
        if (!OperatingSystem.IsWindows()) return;
        var scratch = ModernMcpScratchDirectory.Create();
        var startAttempted = false;
        var cleanupConfirmed = false;
        ModernMcpProcessDriver? driver = null;
        string? stageInterference = null;
        Exception? testFailure = null;
        try
        {
            var home = Path.Combine(scratch, "home");
            var managed = Path.Combine(home, ".netcoredbg-mcp", "netcoredbg");
            var debuggerDirectory = Path.Combine(managed, "netcoredbg");
            CopyAdapter(debuggerDirectory);
            var originalExecutable = Path.Combine(debuggerDirectory, "netcoredbg.exe");
            await File.WriteAllBytesAsync(Path.Combine(debuggerDirectory, "dbgshim.dll"), Original);
            await WriteManifestAsync(managed);
            await WriteCacheAsync(home, "12345.7.10", Higher);
            var program = Path.Combine(scratch, "target.dll");
            await File.WriteAllBytesAsync(program, []);
            await File.WriteAllTextAsync(Path.ChangeExtension(program, ".runtimeconfig.json"),
                "{\"runtimeOptions\":{\"framework\":{\"name\":\"Microsoft.NETCore.App\",\"version\":\"12345.0.0\"}}}");
            var pathDirectory = Path.Combine(scratch, "path");
            Directory.CreateDirectory(pathDirectory);
            driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                ClientProtocolVersion: "2025-11-25",
                HostArguments: ["--legacy-compat"],
                InheritEnvironmentVariables: false,
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["HOME"] = home,
                    ["USERPROFILE"] = home,
                    ["HOMEDRIVE"] = null,
                    ["HOMEPATH"] = null,
                    ["PATH"] = pathDirectory,
                    ["NETCOREDBG_PATH"] = originalExecutable,
                    ["CONTROLLED_DAP_OBSERVE_SHIM"] = "1",
                }));
            var owner = new JsonObject { ["muxSessionId"] = "stage-cleanup-owner" };
            var other = new JsonObject { ["muxSessionId"] = "stage-cleanup-other" };
            startAttempted = true;
            var started = await LegacyCallAsync(driver, "start_debug", new JsonObject
            {
                ["program"] = program,
                ["pre_build"] = false,
                ["stop_at_entry"] = false,
            }, owner);
            Assert.True(started["data"]?["success"]?.GetValue<bool>(), started.ToJsonString());
            var entry = Assert.Single(await driver.ReadFixtureTranscriptAsync(), static item => item.Kind == "shim-observation");
            var observation = Assert.IsType<JsonObject>(JsonNode.Parse(Assert.IsType<string>(entry.Arguments)));
            var stagedExecutable = Assert.IsType<string>(observation["executable"]?.GetValue<string>());
            Assert.False(string.Equals(originalExecutable, stagedExecutable, StringComparison.OrdinalIgnoreCase));
            var stageRoot = Directory.GetParent(Path.GetDirectoryName(stagedExecutable)!)!.FullName;
            stageInterference = Path.Combine(stageRoot, "controlled-unexpected-entry");
            await File.WriteAllTextAsync(stageInterference, "owned test entry");

            var stop = await LegacyCallAsync(driver, "stop_debug", new JsonObject(), owner);
            Assert.NotNull(stop["error"]);
            Assert.True(Directory.Exists(stageRoot));
            var denied = await LegacyCallAsync(driver, "cleanup_processes", new JsonObject { ["force"] = true }, other);
            Assert.NotNull(denied["error"]);
            Assert.True(Directory.Exists(stageRoot));
            var lockedRetry = await LegacyCallAsync(driver, "cleanup_processes", new JsonObject { ["force"] = true }, owner);
            Assert.NotNull(lockedRetry["error"]);
            Assert.False(lockedRetry["data"]?["complete"]?.GetValue<bool>() ?? true);
            Assert.True(Directory.Exists(stageRoot));
            File.Delete(stageInterference);
            stageInterference = null;
            var retry = await LegacyCallAsync(driver, "cleanup_processes", new JsonObject { ["force"] = true }, owner);
            Assert.Null(retry["error"]);
            Assert.True(retry["data"]?["complete"]?.GetValue<bool>());
            Assert.False(Directory.Exists(stageRoot));
            Assert.True(File.Exists(originalExecutable));
            cleanupConfirmed = true;
        }
        catch (Exception exception)
        {
            testFailure = driver is null ? exception : WithHostDiagnostics(driver, exception);
            throw testFailure;
        }
        finally
        {
            Exception? disposalFailure = null;
            try
            {
                if (driver is not null) await driver.DisposeAsync();
            }
            catch (Exception exception) { disposalFailure = exception; }
            if (!startAttempted || cleanupConfirmed)
            {
                try { await ModernMcpScratchDirectory.DeleteAsync(scratch); }
                catch (Exception exception)
                {
                    disposalFailure = disposalFailure is null ? exception : new AggregateException(disposalFailure, exception);
                }
            }
            if (disposalFailure is not null)
                throw testFailure is null ? disposalFailure : new AggregateException("Test failed and cleanup also failed.", testFailure, disposalFailure);
        }
    }

    private static Exception WithHostDiagnostics(ModernMcpProcessDriver driver, Exception primary)
    {
        string diagnostic;
        try
        {
            var method = typeof(ModernMcpProcessDriver).GetMethod("StandardErrorLines",
                System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic);
            var lines = method?.Invoke(driver, null) as IReadOnlyList<string>;
            diagnostic = lines is null ? "unavailable" : string.Join("\n", lines.TakeLast(16).Select(static line =>
                line.Length <= 512 ? line : line[..512]));
            if (diagnostic.Length > 4096) diagnostic = diagnostic[^4096..];
        }
        catch (Exception exception) { diagnostic = "unavailable (" + exception.GetType().Name + ")"; }
        return new AggregateException("Launch test failed. Bounded host stderr:\n" + diagnostic, primary);
    }

    private static async Task PreserveFailureAsync(Exception? primary, Func<Task> cleanup)
    {
        try { await cleanup(); }
        catch (Exception exception) when (primary is not null)
        {
            throw new AggregateException("Test failed and cleanup also failed.", primary, exception);
        }
    }

    private static string CachePath(string home, string version) =>
        Path.Combine(home, ".netcoredbg-mcp", "dbgshim", version, "dbgshim.dll");

    private static async Task WriteCacheAsync(string home, string version, byte[] bytes)
    {
        var path = CachePath(home, version);
        Directory.CreateDirectory(Path.GetDirectoryName(path)!);
        await File.WriteAllBytesAsync(path, bytes);
    }

    private static async Task WriteManifestAsync(string installationRoot)
    {
        var files = new JsonArray();
        var paths = Directory.GetFiles(installationRoot, "*", SearchOption.AllDirectories)
            .Select(path => (Full: path, Relative: Path.GetRelativePath(installationRoot, path).Replace('\\', '/')))
            .OrderBy(file => file.Relative, StringComparer.Ordinal);
        foreach (var file in paths)
        {
            var bytes = await File.ReadAllBytesAsync(file.Full);
            files.Add(new JsonObject
            {
                ["path"] = file.Relative,
                ["size"] = bytes.LongLength,
                ["sha256"] = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant(),
            });
        }
        var receipt = new JsonObject
        {
            ["schema"] = "netcoredbg-mcp.native-debugger-install/1",
            ["asset"] = "netcoredbg-win64.zip",
            ["archiveSha256"] = Convert.ToHexString(SHA256.HashData(Array.Empty<byte>())).ToLowerInvariant(),
            ["files"] = files,
        };
        await File.WriteAllTextAsync(Path.Combine(installationRoot, ".native-debugger-manifest.json"), receipt.ToJsonString());
    }

    private static void CopyAdapter(string destination)
    {
        var adapter = TestOutputPathResolver.ResolveProcess(RepositoryLayout.ControlledAdapterDirectory, "ControlledDapAdapter");
        Assert.Empty(adapter.Arguments);
        Directory.CreateDirectory(destination);
        var source = Path.GetDirectoryName(adapter.Command)!;
        foreach (var name in new[] { "ControlledDapAdapter.dll", "ControlledDapAdapter.deps.json", "ControlledDapAdapter.runtimeconfig.json" })
            File.Copy(Path.Combine(source, name), Path.Combine(destination, name));
        File.Copy(adapter.Command, Path.Combine(destination, "netcoredbg.exe"));
    }

    private static async Task<JsonObject> LegacyCallAsync(ModernMcpProcessDriver driver, string name, JsonObject arguments, JsonObject owner)
    {
        var result = ModernMcpProcessDriver.RequireResult(await driver.CallToolRawAsync(name, arguments, owner,
            new RequestId($"shim-{Guid.NewGuid():N}"), timeout: TimeSpan.FromSeconds(15)));
        return Assert.IsType<JsonObject>(ModernMcpProcessDriver.LegacyPayload(result));
    }
}
