using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Xml.Linq;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

public sealed class NativeBundleProducerTests
{
    [WindowsBundleProducerTheory]
    [InlineData(false, false)]
    [InlineData(true, false)]
    [InlineData(false, true)]
    [InlineData(true, true)]
    public async Task Publish_UsesOwnedOutputAndExplicitChildProperties(bool restore, bool apostrophe)
    {
        await using var sandbox = await Sandbox.CreateAsync(apostrophe);
        var result = await sandbox.RunAsync(restore);

        Assert.True(result.ExitCode == 0, $"stdout: {result.Output}\nstderr: {result.Error}");
        var log = await File.ReadAllLinesAsync(sandbox.Log);
        Assert.Equal(restore ? new[] { "host Restore", "bridge Restore", "host Publish", "bridge Publish" }
            : new[] { "host Publish", "bridge Publish" }, log);
        Assert.Equal("host executable", (await File.ReadAllTextAsync(Path.Combine(sandbox.Output, "NetCoreDbg.Mcp.Stateless.exe"))).TrimEnd('\r', '\n'));
        Assert.Equal("host runtime", (await File.ReadAllTextAsync(Path.Combine(sandbox.Output, "coreclr.dll"))).TrimEnd('\r', '\n'));
        Assert.Equal("bridge executable", (await File.ReadAllTextAsync(Path.Combine(sandbox.Output, "bridge", "FlaUIBridge.exe"))).TrimEnd('\r', '\n'));
        Assert.False(Directory.Exists(sandbox.ConflictingOutput));
    }

    [WindowsBundleProducerFact]
    public async Task Publish_CheckoutPathWithApostrophe_ResolvesBothProjects()
    {
        await using var sandbox = await Sandbox.CreateAsync(sourceApostrophe: true);
        var result = await sandbox.RunAsync(restore: false);

        Assert.True(result.ExitCode == 0, $"stdout: {result.Output}\nstderr: {result.Error}");
        Assert.Equal(new[] { "host Publish", "bridge Publish" }, await File.ReadAllLinesAsync(sandbox.Log));
        Assert.True(File.Exists(Path.Combine(sandbox.Output, "bridge", "FlaUIBridge.exe")));
    }

    [WindowsBundleProducerFact]
    public async Task Publish_RefusesExistingOutputWithoutTouchingSentinel()
    {
        await using var sandbox = await Sandbox.CreateAsync();
        Directory.CreateDirectory(sandbox.Output);
        var sentinel = Path.Combine(sandbox.Output, "sentinel.bin");
        var bytes = new byte[] { 1, 3, 5, 7 };
        await File.WriteAllBytesAsync(sentinel, bytes);
        var timestamp = File.GetLastWriteTimeUtc(sentinel);

        var result = await sandbox.RunAsync(restore: false);

        Assert.NotEqual(0, result.ExitCode);
        Assert.Contains("refusing to overwrite", result.Output + result.Error, StringComparison.OrdinalIgnoreCase);
        Assert.False(File.Exists(sandbox.Log));
        Assert.Equal(bytes, await File.ReadAllBytesAsync(sentinel));
        Assert.Equal(timestamp, File.GetLastWriteTimeUtc(sentinel));
        Assert.Equal(new[] { sentinel }, Directory.GetFileSystemEntries(sandbox.Output));
        Assert.False(Directory.Exists(sandbox.ConflictingOutput));
    }

    private sealed record ProcessResult(int ExitCode, string Output, string Error);

    private sealed class Sandbox : IAsyncDisposable
    {
        private bool _preserve;
        private readonly bool _apostrophe;
        private readonly bool _sourceApostrophe;
        private string Root { get; }
        private string Coordinator => _sourceApostrophe
            ? Path.Combine(Root, "source's checkout", "tools", "native-bundle", "NativeBundle.proj")
            : Path.Combine(Root, "NativeBundle.proj");
        internal string Log => Path.Combine(Root, "calls.log");
        internal string Output => Path.Combine(Root, _apostrophe ? "published O'Brien output" : "published output with spaces");
        internal string ConflictingOutput => Path.Combine(Root, "wrong output");
        private string Dotnet { get; }

        private Sandbox(string root, bool apostrophe, bool sourceApostrophe)
        {
            Root = root;
            _apostrophe = apostrophe;
            _sourceApostrophe = sourceApostrophe;
            var runtime = new DirectoryInfo(RuntimeEnvironment.GetRuntimeDirectory());
            var dotnetRoot = runtime.Parent?.Parent?.Parent?.FullName
                ?? throw new InvalidOperationException("Could not locate the executing dotnet host.");
            Dotnet = Path.Combine(dotnetRoot, "dotnet.exe");
            if (!File.Exists(Dotnet)) throw new FileNotFoundException("The executing runtime's dotnet host is required.", Dotnet);
        }

        internal static async Task<Sandbox> CreateAsync(bool apostrophe = false, bool sourceApostrophe = false)
        {
            var sandbox = new Sandbox(ModernMcpScratchDirectory.Create(), apostrophe, sourceApostrophe);
            try
            {
                var sourceRoot = Path.Combine(sandbox.Root, "source's checkout");
                var host = sourceApostrophe ? Path.Combine(sourceRoot, "host", "NetCoreDbg.Mcp.Stateless", "NetCoreDbg.Mcp.Stateless.csproj") : Path.Combine(sandbox.Root, "FakeHost.proj");
                var bridge = sourceApostrophe ? Path.Combine(sourceRoot, "bridge", "FlaUIBridge.csproj") : Path.Combine(sandbox.Root, "FakeBridge.proj");
                Directory.CreateDirectory(Path.GetDirectoryName(host)!);
                Directory.CreateDirectory(Path.GetDirectoryName(bridge)!);
                Directory.CreateDirectory(Path.GetDirectoryName(sandbox.Coordinator)!);
                await File.WriteAllTextAsync(host, FakeProject("host"));
                await File.WriteAllTextAsync(bridge, FakeProject("bridge"));
                var source = Path.Combine(RepositoryLayout.Root, "tools", "native-bundle", "NativeBundle.proj");
                var project = XDocument.Load(source, LoadOptions.PreserveWhitespace);
                var properties = project.Root?.Element("PropertyGroup")
                    ?? throw new InvalidOperationException("Coordinator property group is missing.");
                var hostProperty = properties.Element("_HostProject")
                    ?? throw new InvalidOperationException("Coordinator host project property is missing.");
                var bridgeProperty = properties.Element("_BridgeProject")
                    ?? throw new InvalidOperationException("Coordinator bridge project property is missing.");
                if (!sourceApostrophe)
                {
                    hostProperty.Value = EscapeMsBuild(host);
                    bridgeProperty.Value = EscapeMsBuild(bridge);
                }
                project.Save(sandbox.Coordinator);
                return sandbox;
            }
            catch
            {
                await sandbox.DisposeAsync();
                throw;
            }
        }

        private static string EscapeMsBuild(string path) => path.Replace("%", "%25", StringComparison.Ordinal)
            .Replace(";", "%3B", StringComparison.Ordinal).Replace("$", "%24", StringComparison.Ordinal)
            .Replace("@", "%40", StringComparison.Ordinal).Replace("'", "%27", StringComparison.Ordinal)
            .Replace("(", "%28", StringComparison.Ordinal).Replace(")", "%29", StringComparison.Ordinal);

        private static string FakeProject(string kind) => $$"""
            <Project>
              <PropertyGroup>
                <_FixtureKind>{{kind}}</_FixtureKind>
                <_ExpectedDir Condition="'$(_FixtureKind)' == 'host'">$(_ExpectedHostDir)</_ExpectedDir>
                <_ExpectedDir Condition="'$(_FixtureKind)' == 'bridge'">$(_ExpectedBridgeDir)</_ExpectedDir>
              </PropertyGroup>
              <Target Name="ValidateFixture">
                <Error Condition="'$(RuntimeIdentifier)' != 'win-x64'" Text="Wrong RID: $(RuntimeIdentifier)" />
                <Error Condition="'$(SelfContained)' != 'true'" Text="Wrong SelfContained: $(SelfContained)" />
                <Error Condition="'$(PublishSelfContained)' != 'true'" Text="Wrong PublishSelfContained: $(PublishSelfContained)" />
                <Error Condition="'$(PublishDir)' != '$(_ExpectedDir)'" Text="Wrong PublishDir: $(PublishDir); expected $(_ExpectedDir)" />
              </Target>
              <Target Name="Restore" DependsOnTargets="ValidateFixture">
                <WriteLinesToFile File="$(_FixtureLog)" Lines="$(_FixtureKind) Restore" Overwrite="false" />
              </Target>
              <Target Name="Publish" DependsOnTargets="ValidateFixture">
                <WriteLinesToFile File="$(_FixtureLog)" Lines="$(_FixtureKind) Publish" Overwrite="false" />
                <MakeDir Directories="$(PublishDir)" />
                <WriteLinesToFile Condition="'$(_FixtureKind)' == 'host'" File="$(PublishDir)NetCoreDbg.Mcp.Stateless.exe" Lines="host executable" Overwrite="false" />
                <WriteLinesToFile Condition="'$(_FixtureKind)' == 'host'" File="$(PublishDir)coreclr.dll" Lines="host runtime" Overwrite="false" />
                <WriteLinesToFile Condition="'$(_FixtureKind)' == 'bridge'" File="$(PublishDir)FlaUIBridge.exe" Lines="bridge executable" Overwrite="false" />
              </Target>
            </Project>
            """;

        internal async Task<ProcessResult> RunAsync(bool restore)
        {
            using var process = new Process { StartInfo = new ProcessStartInfo(Dotnet)
            {
                WorkingDirectory = Root, UseShellExecute = false, RedirectStandardInput = true,
                RedirectStandardOutput = true, RedirectStandardError = true, CreateNoWindow = true,
            } };
            var args = process.StartInfo.ArgumentList;
            args.Add("msbuild");
            args.Add(Coordinator);
            if (restore) args.Add("-restore");
            args.Add("-target:Publish");
            args.Add("-nologo");
            args.Add("-nodeReuse:false");
            args.Add("-maxCpuCount:1");
            args.Add($"-property:NativeBundleOutput={Output}");
            args.Add("-property:RuntimeIdentifier=wrong-rid");
            args.Add("-property:SelfContained=false");
            args.Add("-property:PublishSelfContained=false");
            args.Add($"-property:PublishDir={ConflictingOutput}{Path.DirectorySeparatorChar}");
            args.Add($"-property:_FixtureLog={Log}");
            args.Add($"-property:_ExpectedHostDir={Output}{Path.DirectorySeparatorChar}");
            args.Add($"-property:_ExpectedBridgeDir={Path.Combine(Output, "bridge")}{Path.DirectorySeparatorChar}");
            var environment = process.StartInfo.Environment;
            environment["HOME"] = Root;
            environment["USERPROFILE"] = Root;
            environment["DOTNET_ADD_GLOBAL_TOOLS_TO_PATH"] = "false";
            environment["DOTNET_CLI_TELEMETRY_OPTOUT"] = "1";
            environment["DOTNET_NOLOGO"] = "1";
            environment.Remove("MSBUILD_EXE_PATH");
            environment.Remove("MSBuildSDKsPath");
            environment.Remove("DOTNET_HOST_PATH");

            var started = false;
            var exited = false;
            try
            {
                Assert.True(process.Start());
                started = true;
                var stdout = process.StandardOutput.ReadToEndAsync();
                var stderr = process.StandardError.ReadToEndAsync();
                process.StandardInput.Close();
                using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(30));
                await process.WaitForExitAsync(deadline.Token);
                exited = true;
                return new ProcessResult(process.ExitCode, await stdout.WaitAsync(deadline.Token), await stderr.WaitAsync(deadline.Token));
            }
            finally
            {
                if (started && !exited)
                {
                    _preserve = true;
                    try
                    {
                        if (!process.HasExited) process.Kill();
                        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(10));
                        await process.WaitForExitAsync(deadline.Token);
                    }
                    catch (InvalidOperationException) { }
                    catch (OperationCanceledException) { }
                }
            }
        }

        public async ValueTask DisposeAsync()
        {
            if (!_preserve) await ModernMcpScratchDirectory.DeleteAsync(Root);
        }
    }
}

public sealed class WindowsBundleProducerFactAttribute : FactAttribute
{
    public WindowsBundleProducerFactAttribute()
    {
        if (!OperatingSystem.IsWindows()) Skip = "Native bundle publication requires Windows.";
    }
}

public sealed class WindowsBundleProducerTheoryAttribute : TheoryAttribute
{
    public WindowsBundleProducerTheoryAttribute()
    {
        if (!OperatingSystem.IsWindows()) Skip = "Native bundle publication requires Windows.";
    }
}
