using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// Proves <see cref="ResourcesRelay"/> against the real, unmodified
/// <c>python -m netcoredbg_mcp</c> backend - not <see cref="FakePythonServer"/> - using the
/// same <see cref="PythonBackendProcess"/> production launches, over its real stdio
/// transport. Only the downstream leg is in-memory (a transport choice, matching every other
/// test in this project); both upstream legs are genuine child processes: one reached
/// directly (no relay, the ground truth) and a second, separate process instance reached
/// through <see cref="ResourcesTestComposition.BuildHost"/>. This is the "real module-enabled
/// host client" reading real Python resources required by T-FD005-01, and the direct-vs-host
/// equality check T-FD005-01/FD-003 require - not just a hardcoded approximation of Python's
/// contract, but a live diff against it, mirroring <c>tests/test_host_proxy.py</c>'s existing
/// direct-vs-proxied tool-schema parity technique for FD-000/ToolsRelay.
///
/// The idle-state debuggee here has no active DAP session, so <c>debug://threads</c>
/// naturally returns Python's own real error in this state on both paths (proving error
/// forwarding is also unchanged) rather than a contrived host-side failure. A live-session
/// successful <c>debug://threads</c> read is proven directly against Python (ground truth, no
/// host) in <c>tests/critical/test_resources_relay_critical.py</c>, which is also this
/// repository's existing convention for driving a real debug session (SmokeTestApp +
/// netcoredbg).
/// </summary>
[Collection("SequentialRealPythonProcess")]
public sealed class ResourcesRealPythonTests
{
    // host/NetCoreDbg.Mcp.Host.Tests/bin/Release/net8.0 -> ... -> repo root.
    private static readonly string RepoRoot = Path.GetFullPath(
        Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));

    private static PythonBackendProcess StartRealPython()
    {
        Environment.SetEnvironmentVariable("PYTHONPATH", Path.Combine(RepoRoot, "src"));
        return PythonBackendProcess.Start(["--project", RepoRoot]);
    }

    private static async Task<McpProtocolException> ExpectProtocolErrorAsync(Func<Task> call)
    {
        var exception = await Record.ExceptionAsync(call);
        return Assert.IsType<McpProtocolException>(exception);
    }

    private static (string Uri, string? MimeType, string Text) TextOf(ReadResourceResult result)
    {
        var content = Assert.IsType<TextResourceContents>(result.Contents[0]);
        return (content.Uri, content.MimeType, content.Text);
    }

    [Fact]
    public async Task RealPython_ResourcesMatchDirectPythonGroundTruthThroughTheRelay()
    {
        // Ground truth: a client talking directly to a real Python process, no relay/host
        // in between at all.
        using var directPythonBackend = StartRealPython();
        try
        {
            await using var directClient = await McpClient.CreateAsync(directPythonBackend.CreateUpstreamTransport());

            var directResources = directClient.ServerCapabilities?.Resources;
            var directList = await directClient.ListResourcesAsync(new ListResourcesRequestParams());
            var directTemplates = await directClient.ListResourceTemplatesAsync(new ListResourceTemplatesRequestParams());
            var directState = TextOf(await directClient.ReadResourceAsync("debug://state"));
            var directBreakpoints = TextOf(await directClient.ReadResourceAsync("debug://breakpoints"));
            var directOutput = TextOf(await directClient.ReadResourceAsync("debug://output"));
            var directThreadsError = await ExpectProtocolErrorAsync(() => directClient.ReadResourceAsync("debug://threads").AsTask());
            var directInvalidUriError = await ExpectProtocolErrorAsync(() => directClient.ReadResourceAsync("debug://not-a-real-resource").AsTask());

            // A second, separate real Python process, reached only through ResourcesRelay.
            using var relayedPythonBackend = StartRealPython();
            try
            {
                var session = new RelaySession(relayedPythonBackend.CreateUpstreamTransport, RelayComposition.RequiredUpstreamCapabilityChecks);
                await using (session)
                {
                    var downstreamChannel = new DuplexChannel();
                    using var host = ResourcesTestComposition.BuildHost(
                        session,
                        builder => builder.WithStreamServerTransport(downstreamChannel.ServerInputStream, downstreamChannel.ServerOutputStream));
                    _ = host.RunAsync();

                    await using var downstreamClient = await McpClient.CreateAsync(downstreamChannel.CreateClientTransport());

                    var hostResources = downstreamClient.ServerCapabilities?.Resources;
                    Assert.NotNull(hostResources);
                    Assert.Equal(directResources?.Subscribe, hostResources!.Subscribe);
                    Assert.Equal(directResources?.ListChanged, hostResources.ListChanged);

                    var hostList = await downstreamClient.ListResourcesAsync(new ListResourcesRequestParams());
                    Assert.Equal(directList.NextCursor, hostList.NextCursor);
                    Assert.Equal(
                        directList.Resources.Select(r => (r.Uri, r.Name, r.Description, r.MimeType)),
                        hostList.Resources.Select(r => (r.Uri, r.Name, r.Description, r.MimeType)));

                    var hostTemplates = await downstreamClient.ListResourceTemplatesAsync(new ListResourceTemplatesRequestParams());
                    Assert.Equal(directTemplates.ResourceTemplates, hostTemplates.ResourceTemplates);
                    Assert.Equal(directTemplates.NextCursor, hostTemplates.NextCursor);
                    Assert.Empty(hostTemplates.ResourceTemplates);

                    Assert.Equal(directState, TextOf(await downstreamClient.ReadResourceAsync("debug://state")));
                    Assert.Equal(directBreakpoints, TextOf(await downstreamClient.ReadResourceAsync("debug://breakpoints")));
                    Assert.Equal(directOutput, TextOf(await downstreamClient.ReadResourceAsync("debug://output")));

                    // Message equality end to end is not asserted here: the SDK's own
                    // client-side error conversion prepends a generic "Request failed
                    // (remote): " wrapper at *every* hop that uses SendRequestAsync, so an
                    // error that already crossed one Python hop picks up a second wrapper
                    // crossing the relay hop too - a property of the shared FD-000
                    // SendRequestAsync primitive every forwarded route hits identically
                    // (RelaySession.cs is not owned by this module), not a resources-specific
                    // behavior. What must survive unchanged is Python's own distinctive
                    // message content, and it does, on both paths.
                    var hostThreadsError = await ExpectProtocolErrorAsync(() => downstreamClient.ReadResourceAsync("debug://threads").AsTask());
                    Assert.Contains("DAP client not running", directThreadsError.Message);
                    Assert.Contains("DAP client not running", hostThreadsError.Message);

                    var hostInvalidUriError = await ExpectProtocolErrorAsync(() => downstreamClient.ReadResourceAsync("debug://not-a-real-resource").AsTask());
                    Assert.Contains("Unknown resource", directInvalidUriError.Message);
                    Assert.Contains("Unknown resource", hostInvalidUriError.Message);
                }
            }
            finally
            {
                await relayedPythonBackend.StopAsync();
                await relayedPythonBackend.WaitForStderrForwardedAsync();
            }
        }
        finally
        {
            await directPythonBackend.StopAsync();
            await directPythonBackend.WaitForStderrForwardedAsync();
        }
    }
}
