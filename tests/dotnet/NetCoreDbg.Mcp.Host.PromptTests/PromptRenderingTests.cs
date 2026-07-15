using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Fixed and parameterized prompts/get rendering for every one of the eight prompts,
/// proven against the direct Python server baseline: same description, same message
/// count, same roles, and same rendered text - a change to any prompt's name, schema, or
/// rendered content fails these tests because they diff against a live baseline rather
/// than a stale hardcoded copy.
/// </summary>
[Collection(PythonBaselineCollection.Name)]
public sealed class PromptRenderingTests
{
    private readonly PythonBaselineFixture _python;

    public PromptRenderingTests(PythonBaselineFixture python) => _python = python;

    [Theory]
    [InlineData("debug")]
    [InlineData("debug-gui")]
    [InlineData("debug-exception")]
    [InlineData("debug-visual")]
    [InlineData("debug-mistakes")]
    [InlineData("dap-escape-hatch")]
    public async Task GetPrompt_ArgumentLessPrompt_MatchesDirectPythonBaseline(string name)
    {
        await using var native = await NativePromptsHost.StartAsync();

        var nativeResult = await native.Client.GetPromptAsync(name);
        var pythonResult = await _python.Server.Client.GetPromptAsync(name);

        AssertResultsEqual(pythonResult, nativeResult);
    }

    [Theory]
    [InlineData("NullReferenceException", "gui")]
    [InlineData("NullReferenceException", "console")]
    [InlineData("something totally unknown xyz", "console")]
    [InlineData("something totally unknown xyz", "gui")]
    [InlineData("Deadlock detected in UI thread", "gui")]
    [InlineData("high CPU usage under load", "console")]
    public async Task GetPrompt_Investigate_MatchesDirectPythonBaseline(string symptom, string appType)
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?> { ["symptom"] = symptom, ["app_type"] = appType };

        var nativeResult = await native.Client.GetPromptAsync("investigate", arguments);
        var pythonResult = await _python.Server.Client.GetPromptAsync("investigate", arguments);

        AssertResultsEqual(pythonResult, nativeResult);
    }

    [Fact]
    public async Task GetPrompt_Investigate_DefaultAppType_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?> { ["symptom"] = "ObjectDisposedException" };

        var nativeResult = await native.Client.GetPromptAsync("investigate", arguments);
        var pythonResult = await _python.Server.Client.GetPromptAsync("investigate", arguments);

        AssertResultsEqual(pythonResult, nativeResult);
    }

    [Theory]
    [InlineData("button click doesn't save", "gui", "")]
    [InlineData("x", "console", "Foo.cs")]
    [InlineData("crashes on startup", "console", "")]
    [InlineData("grid selection lost", "gui", "MainViewModel.cs")]
    public async Task GetPrompt_DebugScenario_MatchesDirectPythonBaseline(string problem, string appType, string fileHint)
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?>
        {
            ["problem"] = problem,
            ["app_type"] = appType,
            ["file_hint"] = fileHint,
        };

        var nativeResult = await native.Client.GetPromptAsync("debug-scenario", arguments);
        var pythonResult = await _python.Server.Client.GetPromptAsync("debug-scenario", arguments);

        AssertResultsEqual(pythonResult, nativeResult);
    }

    [Fact]
    public async Task GetPrompt_DebugScenario_DefaultsOnly_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?> { ["problem"] = "layout looks wrong on resize" };

        var nativeResult = await native.Client.GetPromptAsync("debug-scenario", arguments);
        var pythonResult = await _python.Server.Client.GetPromptAsync("debug-scenario", arguments);

        AssertResultsEqual(pythonResult, nativeResult);
    }

    [Fact]
    public async Task GetPrompt_DebugException_HasThreeMessagesWithAlternatingRoles()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var result = await native.Client.GetPromptAsync("debug-exception");

        Assert.Collection(
            result.Messages,
            m => Assert.Equal(Role.User, m.Role),
            m => Assert.Equal(Role.Assistant, m.Role),
            m => Assert.Equal(Role.User, m.Role));
        Assert.Equal("The debugger stopped on an exception.", ((TextContentBlock)result.Messages[0].Content).Text);
        Assert.Equal("I'll investigate. Let me gather details.", ((TextContentBlock)result.Messages[1].Content).Text);
    }

    private static void AssertResultsEqual(GetPromptResult expected, GetPromptResult actual)
    {
        Assert.Equal(expected.Description, actual.Description);
        Assert.Equal(expected.Messages.Count, actual.Messages.Count);
        for (var i = 0; i < expected.Messages.Count; i++)
        {
            Assert.Equal(expected.Messages[i].Role, actual.Messages[i].Role);
            var expectedText = Assert.IsType<TextContentBlock>(expected.Messages[i].Content).Text;
            var actualText = Assert.IsType<TextContentBlock>(actual.Messages[i].Content).Text;
            Assert.Equal(expectedText, actualText);
        }
    }
}
