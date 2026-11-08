using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

public sealed class LegacyPromptContractTests
{
    [Fact]
    public async Task LegacyHost_AdvertisesAndRendersExistingPromptsWithNativeLimitations()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            ClientProtocolVersion: "2025-11-25", HostArguments: ["--legacy-compat"]));

        Assert.NotNull(driver.Client.ServerCapabilities?.Prompts);
        var catalog = await driver.Client.ListPromptsAsync(new ListPromptsRequestParams());
        Assert.Equal(new[] { "debug", "debug-gui", "debug-exception", "debug-visual", "debug-mistakes", "investigate", "debug-scenario", "dap-escape-hatch" },
            catalog.Prompts.Select(prompt => prompt.Name));

        var guide = await driver.Client.GetPromptAsync("debug-gui");
        var text = string.Join("\n", guide.Messages.Select(message => Assert.IsType<TextContentBlock>(message.Content).Text));
        Assert.Contains("# Debugging WPF / Avalonia / WinForms Apps", text);
        Assert.Contains("UI tools", text, StringComparison.OrdinalIgnoreCase);
        Assert.Contains("not yet", text, StringComparison.OrdinalIgnoreCase);

        var debug = await driver.Client.GetPromptAsync("debug");
        var debugText = string.Join("\n", debug.Messages.Select(message => Assert.IsType<TextContentBlock>(message.Content).Text));
        Assert.Contains("resumes all threads", debugText);
        Assert.DoesNotContain("resume only that thread", debugText);

        var investigation = await driver.Client.GetPromptAsync("investigate", new Dictionary<string, object?>
        {
            ["symptom"] = "example symptom",
            ["app_type"] = "console",
        });
        var investigationText = string.Join("\n", investigation.Messages.Select(message => Assert.IsType<TextContentBlock>(message.Content).Text));
        Assert.Contains("example symptom", investigationText);
        await Assert.ThrowsAsync<McpProtocolException>(() => driver.Client.GetPromptAsync("investigate").AsTask());
        await Assert.ThrowsAsync<McpProtocolException>(() => driver.Client.GetPromptAsync("not-a-prompt").AsTask());
    }

    [Fact]
    public async Task ModernHost_DoesNotAdvertiseLegacyPrompts()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        Assert.Null(driver.Client.ServerCapabilities?.Prompts);
        await Assert.ThrowsAsync<McpProtocolException>(() => driver.Client.ListPromptsAsync(new ListPromptsRequestParams()).AsTask());
    }
}
