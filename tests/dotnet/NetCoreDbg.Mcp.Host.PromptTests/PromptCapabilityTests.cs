using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Prompts capability parity: direct Python's low-level server computes
/// <c>PromptsCapability(listChanged=notification_options.prompts_changed)</c>, and that
/// option defaults to <see langword="false"/> because the prompt catalog is static and no
/// caller ever enables the <c>prompts_changed</c> notification option - so Python always
/// advertises <c>{"listChanged": false}</c>, never an absent/null capability.
/// </summary>
[Collection(PythonBaselineCollection.Name)]
public sealed class PromptCapabilityTests
{
    private readonly PythonBaselineFixture _python;

    public PromptCapabilityTests(PythonBaselineFixture python) => _python = python;

    [Fact]
    public async Task ComposedHost_AdvertisesPromptsCapability_WithListChangedFalse()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var prompts = native.Client.ServerCapabilities.Prompts;

        Assert.NotNull(prompts);
        Assert.False(prompts!.ListChanged);
    }

    [Fact]
    public async Task ComposedHost_PromptsCapability_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var nativePrompts = native.Client.ServerCapabilities.Prompts;
        var pythonPrompts = _python.Server.Client.ServerCapabilities.Prompts;

        Assert.NotNull(pythonPrompts);
        Assert.NotNull(nativePrompts);
        Assert.Equal(pythonPrompts!.ListChanged, nativePrompts!.ListChanged);
        Assert.False(nativePrompts.ListChanged);
    }

    /// <summary>
    /// Documents, and fails loudly if it ever silently stops being true, an integration
    /// pitfall for the future <c>RelayComposition.cs</c> integrator: SDK 1.4.1 infers a
    /// <see cref="PromptsCapability"/> shell purely from <c>NativePrompts.Register</c>
    /// setting <c>McpServerHandlers.ListPromptsHandler</c>/<c>GetPromptHandler</c>, but
    /// leaves <see cref="PromptsCapability.ListChanged"/> <see langword="null"/> unless the
    /// composition root also explicitly assigns
    /// <c>options.Capabilities.Prompts = new PromptsCapability { ListChanged = false }</c>
    /// (exactly what <see cref="NativePromptsHost"/> does on this project's behalf, purely
    /// for this test suite, standing in for the future composition root). Register() itself
    /// cannot do this: it only receives an <c>McpServerHandlers</c>, not the surrounding
    /// <c>McpServerOptions</c>/<c>ServerCapabilities</c>, by the fixed PR-001 contract.
    /// </summary>
    [Fact]
    public async Task Register_Alone_WithoutExplicitCapabilityDeclaration_LeavesListChangedNull()
    {
        var channel = new DuplexChannel();
        var builder = Microsoft.Extensions.Hosting.Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Services
            .AddMcpServer(options =>
            {
                options.ServerInfo = new Implementation { Name = "capability-probe", Version = "1.0.0" };
                NativePrompts.Register(options.Handlers);
            })
            .WithStreamServerTransport(channel.ServerInputStream, channel.ServerOutputStream);

        using var host = builder.Build();
        _ = host.RunAsync();
        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport());

        var prompts = client.ServerCapabilities.Prompts;

        Assert.NotNull(prompts);
        Assert.Null(prompts!.ListChanged);

        await host.StopAsync();
    }
}
