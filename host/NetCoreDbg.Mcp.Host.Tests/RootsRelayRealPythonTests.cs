using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Proves <see cref="RootsRelay"/>'s reverse <c>roots/list</c> route against the real
/// <c>netcoredbg_mcp</c> Python backend - launched with the exact same
/// <see cref="PythonBackendProcess"/> production code uses, never a fake server - so this is
/// the "real Python scoped tool" proof T-FD001-01 requires: a real downstream client's root,
/// distinct from this test process's own working directory, reaches Python's real
/// <c>find_code_symbol</c> tool through this module's composition. Python's
/// <c>get_project_root()</c> keeps operator-pinned project authority
/// (<c>--project</c> / env) over any client root; client MCP roots are a local fallback only
/// when no operator pin is configured
/// (<c>src/netcoredbg_mcp/utils/project.py</c>).
///
/// Composition here mirrors production plus this module's own registration exactly as
/// <c>RootsRelayTests</c> does for its in-memory fake-Python fixtures - the substitution
/// is only the transport plus an explicit, temporary
/// <c>NETCOREDBG_MCP_PYTHON_EXECUTABLE</c> pointing at this worktree's own environment, never
/// a mock of RootsRelay/RelaySession/RelayComposition themselves.
/// </summary>
public sealed class RootsRelayRealPythonTests
{
    private const string MarkerSymbol = "RootsRelayMarkerProbe";
    private const string ToolName = "find_code_symbol";
    private const string ProjectRootEnvironmentVariable = "NETCOREDBG_PROJECT_ROOT";

    private static readonly string RepoRoot = LocateRepoRoot();
    private static readonly string PythonExecutable = Path.Combine(RepoRoot, ".venv", "Scripts", "python.exe");

    private static string LocateRepoRoot()
    {
        for (var dir = new DirectoryInfo(AppContext.BaseDirectory); dir is not null; dir = dir.Parent!)
        {
            if (File.Exists(Path.Combine(dir.FullName, "pyproject.toml")))
            {
                return dir.FullName;
            }
        }

        throw new InvalidOperationException(
            $"Could not locate the repository root (pyproject.toml) above {AppContext.BaseDirectory}.");
    }

    private static void WriteMarkerCSharpFile(string directory)
    {
        Directory.CreateDirectory(directory);
        File.WriteAllText(
            Path.Combine(directory, "Marker.cs"),
            $"namespace RootsRelayFixture;\n\npublic class {MarkerSymbol}\n{{\n}}\n");
    }

    /// <summary>
    /// Mirrors the real production composition (real <see cref="PythonBackendProcess"/>,
    /// real <see cref="RelaySession"/>/<see cref="RelayComposition.Build"/>) plus this
    /// module's own registration - the integration hook reported with this change.
    /// </summary>
    private static (RelaySession Session, PythonBackendProcess Python, DuplexChannel Downstream)
        StartRelayedRealPython(IReadOnlyList<string> pythonArgs)
    {
        Assert.True(File.Exists(PythonExecutable), $"expected the worktree venv interpreter at {PythonExecutable}");
        Environment.SetEnvironmentVariable("NETCOREDBG_MCP_PYTHON_EXECUTABLE", PythonExecutable);
        var python = PythonBackendProcess.Start(pythonArgs);

        var downstreamChannel = new DuplexChannel();
        var rootsRelay = new RootsRelay();
        RelaySession? session = null;
        session = new RelaySession(
            python.CreateUpstreamTransport,
            RelayComposition.RequiredUpstreamCapabilityChecks,
            handlers => rootsRelay.ConfigureUpstreamHandlers(handlers, session!));

        var host = RelayComposition.Build(
            session,
            builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream),
            downstreamCapabilities => rootsRelay.ProjectCapabilities(downstreamCapabilities, new ClientCapabilities()));
        _ = host.RunAsync();

        return (session, python, downstreamChannel);
    }

    private static async Task StopRelayedRealPythonAsync(RelaySession session, PythonBackendProcess python)
    {
        await session.DisposeAsync();
        await python.StopAsync();
        await python.WaitForStderrForwardedAsync();
        python.Dispose();
    }

    private static async Task<JsonDocument> CallFindMarkerAsync(McpClient client)
    {
        var result = await client.CallToolAsync(
            ToolName,
            new Dictionary<string, object?> { ["name"] = MarkerSymbol, ["kind"] = "class" });

        Assert.False(result.IsError == true, DescribeResult(result));
        var text = Assert.IsType<TextContentBlock>(result.Content[0]);
        return JsonDocument.Parse(text.Text);
    }

    private static string DescribeResult(CallToolResult result) =>
        result.Content.Count > 0 && result.Content[0] is TextContentBlock text ? text.Text : "<no content>";

    private static void AssertSameDirectory(string expected, string actual) =>
        Assert.Equal(new DirectoryInfo(expected).FullName, new DirectoryInfo(actual).FullName, ignoreCase: true);

    [Fact]
    public async Task DownstreamRoot_DifferentFromHostCwd_ReachesRealPythonScopedTool()
    {
        var downstreamRoot = Path.Combine(Path.GetTempPath(), "roots-relay-" + Guid.NewGuid().ToString("N"));
        WriteMarkerCSharpFile(downstreamRoot);

        var (session, python, downstreamChannel) = StartRelayedRealPython(Array.Empty<string>());
        try
        {
            await using var downstreamClient = await McpClient.CreateAsync(
                downstreamChannel.CreateClientTransport(),
                new McpClientOptions
                {
                    Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                    Handlers = new McpClientHandlers
                    {
                        RootsHandler = (requestParams, ct) => ValueTask.FromResult(new ListRootsResult
                        {
                            Roots = [new Root { Uri = new Uri(downstreamRoot).AbsoluteUri, Name = "downstream" }],
                        }),
                    },
                });

            using var payload = await CallFindMarkerAsync(downstreamClient);
            var data = payload.RootElement.GetProperty("data");
            Assert.Equal(1, data.GetProperty("count").GetInt32());
            Assert.Equal(MarkerSymbol, data.GetProperty("results")[0].GetProperty("name").GetString());

            var projectRoot = data.GetProperty("project_root").GetString()!;
            AssertSameDirectory(downstreamRoot, projectRoot);
            Assert.NotEqual(
                new DirectoryInfo(Environment.CurrentDirectory).FullName,
                new DirectoryInfo(projectRoot).FullName,
                StringComparer.OrdinalIgnoreCase);

            await downstreamClient.DisposeAsync();
        }
        finally
        {
            await StopRelayedRealPythonAsync(session, python);
            Directory.Delete(downstreamRoot, recursive: true);
        }
    }

    [Fact]
    public async Task GroundTruth_DirectPythonWithNoRelayAtAll_AlsoPrioritizesRootsOverCwd()
    {
        // No RelaySession/host at all: a real McpClient connects straight to the real
        // Python backend's own stdio transport. With no operator pin configured, client
        // MCP roots remain a valid local fallback over process CWD - this module only
        // makes that fallback reachable through the host, it does not invent it.
        var rootsRoot = Path.Combine(Path.GetTempPath(), "roots-relay-direct-" + Guid.NewGuid().ToString("N"));
        WriteMarkerCSharpFile(rootsRoot);

        Environment.SetEnvironmentVariable("NETCOREDBG_MCP_PYTHON_EXECUTABLE", PythonExecutable);
        var python = PythonBackendProcess.Start(Array.Empty<string>());
        try
        {
            await using var directClient = await McpClient.CreateAsync(
                python.CreateUpstreamTransport(),
                new McpClientOptions
                {
                    Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                    Handlers = new McpClientHandlers
                    {
                        RootsHandler = (requestParams, ct) => ValueTask.FromResult(new ListRootsResult
                        {
                            Roots = [new Root { Uri = new Uri(rootsRoot).AbsoluteUri, Name = "roots-root" }],
                        }),
                    },
                });

            using var payload = await CallFindMarkerAsync(directClient);
            var data = payload.RootElement.GetProperty("data");
            Assert.Equal(1, data.GetProperty("count").GetInt32());
            AssertSameDirectory(rootsRoot, data.GetProperty("project_root").GetString()!);

            await directClient.DisposeAsync();
        }
        finally
        {
            await python.StopAsync();
            await python.WaitForStderrForwardedAsync();
            python.Dispose();
            Directory.Delete(rootsRoot, recursive: true);
        }
    }

    [Fact]
    public async Task ExplicitProjectFlag_PrecedenceUnchanged_WhenDownstreamHasNoRootsCapability()
    {
        var projectRoot = Path.Combine(Path.GetTempPath(), "roots-relay-explicit-" + Guid.NewGuid().ToString("N"));
        WriteMarkerCSharpFile(projectRoot);

        var (session, python, downstreamChannel) = StartRelayedRealPython(new[] { "--project", projectRoot });
        try
        {
            // No Roots capability at all - a real client that genuinely does not support
            // roots (the mcp SDK's own default) - so RootsRelay must not interfere with
            // Python's own explicit --project resolution at all.
            await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

            using var payload = await CallFindMarkerAsync(downstreamClient);
            var data = payload.RootElement.GetProperty("data");
            Assert.Equal(1, data.GetProperty("count").GetInt32());
            AssertSameDirectory(projectRoot, data.GetProperty("project_root").GetString()!);

            await downstreamClient.DisposeAsync();
        }
        finally
        {
            await StopRelayedRealPythonAsync(session, python);
            Directory.Delete(projectRoot, recursive: true);
        }
    }

    [Fact]
    public async Task ProjectFromCwdFlag_PrecedenceUnchanged_WhenDownstreamHasNoRootsCapability()
    {
        var projectRoot = Path.Combine(Path.GetTempPath(), "roots-relay-cwd-" + Guid.NewGuid().ToString("N"));
        WriteMarkerCSharpFile(projectRoot);
        // find_dotnet_project_root's marker search would otherwise walk up from this
        // process's own cwd into the repository's own .sln/.git; a project marker placed
        // directly in the temp cwd itself is matched on the very first ancestor check, so
        // the resolved root is deterministic regardless of the surrounding repository.
        File.WriteAllText(Path.Combine(projectRoot, "Fixture.csproj"), "<Project Sdk=\"Microsoft.NET.Sdk\" />");

        var originalCwd = Environment.CurrentDirectory;
        (RelaySession Session, PythonBackendProcess Python, DuplexChannel Downstream) started;
        Environment.CurrentDirectory = projectRoot;
        try
        {
            started = StartRelayedRealPython(new[] { "--project-from-cwd" });
        }
        finally
        {
            Environment.CurrentDirectory = originalCwd;
        }

        try
        {
            // No Roots capability at all - so RootsRelay must not interfere with Python's
            // own --project-from-cwd marker-search resolution at all.
            await using var downstreamClient = await McpClient.CreateAsync(started.Downstream.CreateClientTransport());

            using var payload = await CallFindMarkerAsync(downstreamClient);
            var data = payload.RootElement.GetProperty("data");
            Assert.Equal(1, data.GetProperty("count").GetInt32());
            AssertSameDirectory(projectRoot, data.GetProperty("project_root").GetString()!);

            await downstreamClient.DisposeAsync();
        }
        finally
        {
            await StopRelayedRealPythonAsync(started.Session, started.Python);
            Directory.Delete(projectRoot, recursive: true);
        }
    }

    [Fact]
    public async Task ProjectRootEnvironmentVariable_PrecedenceUnchanged_WhenDownstreamHasNoRootsCapability()
    {
        var projectRoot = Path.Combine(Path.GetTempPath(), "roots-relay-envvar-" + Guid.NewGuid().ToString("N"));
        WriteMarkerCSharpFile(projectRoot);

        // NETCOREDBG_PROJECT_ROOT is read once by the child Python process at its own
        // startup (inherited from this process's environment at the moment it is spawned
        // inside StartRelayedRealPython), so restoring it immediately afterward cannot
        // affect the already-started child.
        Environment.SetEnvironmentVariable(ProjectRootEnvironmentVariable, projectRoot);
        var (session, python, downstreamChannel) = StartRelayedRealPython(Array.Empty<string>());
        Environment.SetEnvironmentVariable(ProjectRootEnvironmentVariable, null);

        try
        {
            await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

            using var payload = await CallFindMarkerAsync(downstreamClient);
            var data = payload.RootElement.GetProperty("data");
            Assert.Equal(1, data.GetProperty("count").GetInt32());
            AssertSameDirectory(projectRoot, data.GetProperty("project_root").GetString()!);

            await downstreamClient.DisposeAsync();
        }
        finally
        {
            await StopRelayedRealPythonAsync(session, python);
            Directory.Delete(projectRoot, recursive: true);
        }
    }

    [Fact]
    public async Task EmptyRootsList_FallsThroughToExplicitProject_RealPython()
    {
        var projectRoot = Path.Combine(Path.GetTempPath(), "roots-relay-empty-" + Guid.NewGuid().ToString("N"));
        WriteMarkerCSharpFile(projectRoot);

        var (session, python, downstreamChannel) = StartRelayedRealPython(new[] { "--project", projectRoot });
        try
        {
            // Declares Roots (so this module correctly wires/advertises it) but returns
            // zero roots - with operator --project pinned, client roots are not consulted;
            // empty roots must not displace the explicit project path.
            await using var downstreamClient = await McpClient.CreateAsync(
                downstreamChannel.CreateClientTransport(),
                new McpClientOptions
                {
                    Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                    Handlers = new McpClientHandlers
                    {
                        RootsHandler = (requestParams, ct) => ValueTask.FromResult(new ListRootsResult { Roots = [] }),
                    },
                });

            using var payload = await CallFindMarkerAsync(downstreamClient);
            var data = payload.RootElement.GetProperty("data");
            Assert.Equal(1, data.GetProperty("count").GetInt32());
            AssertSameDirectory(projectRoot, data.GetProperty("project_root").GetString()!);

            await downstreamClient.DisposeAsync();
        }
        finally
        {
            await StopRelayedRealPythonAsync(session, python);
            Directory.Delete(projectRoot, recursive: true);
        }
    }

    [Fact]
    public async Task ExplicitProjectFlag_WinsOverHostileDownstreamRoot_RealPython()
    {
        // Operator --project plus a non-empty hostile client root through the real relay:
        // marker lives only under the pinned path so a roots-first regression would yield
        // count=0 (or the wrong project_root) instead of a silent false green.
        var projectRoot = Path.Combine(Path.GetTempPath(), "roots-relay-pinned-" + Guid.NewGuid().ToString("N"));
        var hostileRoot = Path.Combine(Path.GetTempPath(), "roots-relay-hostile-" + Guid.NewGuid().ToString("N"));
        WriteMarkerCSharpFile(projectRoot);
        Directory.CreateDirectory(hostileRoot);

        var (session, python, downstreamChannel) = StartRelayedRealPython(new[] { "--project", projectRoot });
        try
        {
            await using var downstreamClient = await McpClient.CreateAsync(
                downstreamChannel.CreateClientTransport(),
                new McpClientOptions
                {
                    Capabilities = new ClientCapabilities { Roots = new RootsCapability() },
                    Handlers = new McpClientHandlers
                    {
                        RootsHandler = (requestParams, ct) => ValueTask.FromResult(new ListRootsResult
                        {
                            Roots = [new Root { Uri = new Uri(hostileRoot).AbsoluteUri, Name = "hostile" }],
                        }),
                    },
                });

            using var payload = await CallFindMarkerAsync(downstreamClient);
            var data = payload.RootElement.GetProperty("data");
            Assert.Equal(1, data.GetProperty("count").GetInt32());
            Assert.Equal(MarkerSymbol, data.GetProperty("results")[0].GetProperty("name").GetString());
            AssertSameDirectory(projectRoot, data.GetProperty("project_root").GetString()!);
            Assert.NotEqual(
                new DirectoryInfo(hostileRoot).FullName,
                new DirectoryInfo(data.GetProperty("project_root").GetString()!).FullName,
                StringComparer.OrdinalIgnoreCase);

            await downstreamClient.DisposeAsync();
        }
        finally
        {
            await StopRelayedRealPythonAsync(session, python);
            Directory.Delete(projectRoot, recursive: true);
            Directory.Delete(hostileRoot, recursive: true);
        }
    }
}
