using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Wires <see cref="NativePrompts.Register"/> into a real in-process MCP server, reachable
/// over an in-memory <see cref="DuplexChannel"/> by a real <see cref="McpClient"/>. No
/// Python process, no <c>RelaySession</c>, no <c>RelayComposition</c> - this is
/// deliberately the smallest possible host that exercises <c>NativePrompts.cs</c> through
/// genuine MCP request/response plumbing.
///
/// The one thing this fixture does that <see cref="NativePrompts.Register"/> itself does
/// NOT do is declare <see cref="PromptsCapability"/> on <c>ServerCapabilities</c>. That is
/// deliberate: <c>Register</c> only receives an <c>McpServerHandlers</c>, not the
/// surrounding <c>McpServerOptions</c>, by the fixed PR-001 contract - capability
/// declaration is the future <c>RelayComposition.cs</c> integrator's job. This fixture
/// supplies the same capability declaration that integrator will need
/// (<c>listChanged: false</c>, matching direct Python's low-level server, which computes
/// <c>PromptsCapability(listChanged=notification_options.prompts_changed)</c> with that
/// option defaulting to <see langword="false"/>) so the composed behavior can be verified
/// end-to-end today. Without an explicit capability object, this SDK version still infers
/// a <c>PromptsCapability</c> shell purely from handler presence, but leaves
/// <c>ListChanged</c> null instead of false - see
/// <see cref="PromptCapabilityTests"/>.
/// </summary>
internal sealed class NativePromptsHost : IAsyncDisposable
{
    private readonly IHost _host;

    private NativePromptsHost(IHost host, McpClient client)
    {
        _host = host;
        Client = client;
    }

    public McpClient Client { get; }

    public static async Task<NativePromptsHost> StartAsync()
    {
        var channel = new DuplexChannel();

        var builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Services
            .AddMcpServer(options =>
            {
                options.ServerInfo = new Implementation { Name = "native-prompts-test-host", Version = "1.0.0" };
                options.Capabilities = new ServerCapabilities
                {
                    Prompts = new PromptsCapability { ListChanged = false },
                };
                NativePrompts.Register(options.Handlers);
            })
            .WithStreamServerTransport(channel.ServerInputStream, channel.ServerOutputStream);

        var host = builder.Build();
        _ = host.RunAsync();

        var client = await McpClient.CreateAsync(channel.CreateClientTransport());

        return new NativePromptsHost(host, client);
    }

    public async ValueTask DisposeAsync()
    {
        await Client.DisposeAsync();
        await _host.StopAsync();
        _host.Dispose();
    }
}
