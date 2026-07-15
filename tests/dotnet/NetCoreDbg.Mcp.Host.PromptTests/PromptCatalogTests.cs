using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Exact prompts/list contract: the eight names, their declaration order, descriptions,
/// and argument schemas (name + required), proven both as fixed literal expectations and
/// as a live diff against the direct Python server baseline.
/// </summary>
[Collection(PythonBaselineCollection.Name)]
public sealed class PromptCatalogTests
{
    private static readonly string[] ExpectedNamesInOrder =
    {
        "debug", "debug-gui", "debug-exception", "debug-visual", "debug-mistakes",
        "investigate", "debug-scenario", "dap-escape-hatch",
    };

    private readonly PythonBaselineFixture _python;

    public PromptCatalogTests(PythonBaselineFixture python) => _python = python;

    [Fact]
    public async Task ListPrompts_ReturnsExactlyEightNamesInDeclarationOrder()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var prompts = await native.Client.ListPromptsAsync();

        Assert.Equal(ExpectedNamesInOrder, prompts.Select(p => p.Name));
    }

    [Fact]
    public async Task ListPrompts_MatchesDirectPythonBaseline_NameOrderDescriptionAndArguments()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var nativeResult = await native.Client.ListPromptsAsync(new ListPromptsRequestParams());
        var pythonResult = await _python.Server.Client.ListPromptsAsync(new ListPromptsRequestParams());

        Assert.Equal(pythonResult.Prompts.Count, nativeResult.Prompts.Count);
        for (var i = 0; i < pythonResult.Prompts.Count; i++)
        {
            var expected = pythonResult.Prompts[i];
            var actual = nativeResult.Prompts[i];

            Assert.Equal(expected.Name, actual.Name);
            Assert.Equal(expected.Title, actual.Title);
            Assert.Equal(expected.Description, actual.Description);
            AssertArgumentsEqual(expected.Arguments, actual.Arguments);
        }
    }

    [Theory]
    [InlineData(
        "debug",
        "Complete guide to debugging .NET apps. Start here before your first debug session. Covers state machine, tool usage, anti-patterns, workflows.")]
    [InlineData(
        "debug-gui",
        "WPF and Avalonia Desktop UI debugging workflow. Use when debugging GUI apps — critical breakpoint timing and UI interaction rules that differ from console apps.")]
    [InlineData(
        "debug-exception",
        "Step-by-step exception investigation. Use when the debugger stops on an exception or app crashes.")]
    [InlineData(
        "debug-visual",
        "Visual UI inspection via screenshots and Set-of-Mark annotation. Use when you need to SEE the app UI, verify layout, or click elements by visual position.")]
    [InlineData(
        "debug-mistakes",
        "Common debugging anti-patterns with WRONG/CORRECT examples. Use as a checklist to avoid known pitfalls.")]
    [InlineData(
        "investigate",
        "Targeted investigation for a specific exception type or symptom. Pass the exception name or symptom description to get a focused debugging plan with exact tools and steps.")]
    [InlineData(
        "debug-scenario",
        "Get a step-by-step debugging plan for a specific scenario. Pass a description of the problem and get exact tool calls to execute.")]
    [InlineData(
        "dap-escape-hatch",
        "Unwrapped DAP command reference. Use when a specific DAP command has no first-class MCP tool yet.")]
    public async Task ListPrompts_DescriptionMatchesFixedExpectation(string name, string expectedDescription)
    {
        await using var native = await NativePromptsHost.StartAsync();
        var prompts = await native.Client.ListPromptsAsync();

        var prompt = Assert.Single(prompts, p => p.Name == name);
        Assert.Equal(expectedDescription, prompt.Description);
    }

    [Fact]
    public async Task ListPrompts_InvestigateArguments_SymptomRequiredAppTypeOptional()
    {
        await using var native = await NativePromptsHost.StartAsync();
        var prompts = await native.Client.ListPromptsAsync(new ListPromptsRequestParams());

        var investigate = prompts.Prompts.Single(p => p.Name == "investigate");
        Assert.NotNull(investigate.Arguments);
        Assert.Collection(
            investigate.Arguments!,
            arg =>
            {
                Assert.Equal("symptom", arg.Name);
                Assert.True(arg.Required);
            },
            arg =>
            {
                Assert.Equal("app_type", arg.Name);
                Assert.False(arg.Required);
            });
    }

    [Fact]
    public async Task ListPrompts_DebugScenarioArguments_ProblemRequiredRestOptional()
    {
        await using var native = await NativePromptsHost.StartAsync();
        var prompts = await native.Client.ListPromptsAsync(new ListPromptsRequestParams());

        var debugScenario = prompts.Prompts.Single(p => p.Name == "debug-scenario");
        Assert.NotNull(debugScenario.Arguments);
        Assert.Collection(
            debugScenario.Arguments!,
            arg =>
            {
                Assert.Equal("problem", arg.Name);
                Assert.True(arg.Required);
            },
            arg =>
            {
                Assert.Equal("app_type", arg.Name);
                Assert.False(arg.Required);
            },
            arg =>
            {
                Assert.Equal("file_hint", arg.Name);
                Assert.False(arg.Required);
            });
    }

    [Theory]
    [InlineData("debug")]
    [InlineData("debug-gui")]
    [InlineData("debug-exception")]
    [InlineData("debug-visual")]
    [InlineData("debug-mistakes")]
    [InlineData("dap-escape-hatch")]
    public async Task ListPrompts_ArgumentLessPrompts_HaveNoArguments(string name)
    {
        await using var native = await NativePromptsHost.StartAsync();
        var prompts = await native.Client.ListPromptsAsync(new ListPromptsRequestParams());

        var prompt = prompts.Prompts.Single(p => p.Name == name);
        Assert.True(prompt.Arguments is null || prompt.Arguments.Count == 0);
    }

    private static void AssertArgumentsEqual(IList<PromptArgument>? expected, IList<PromptArgument>? actual)
    {
        var expectedList = expected ?? new List<PromptArgument>();
        var actualList = actual ?? new List<PromptArgument>();

        Assert.Equal(expectedList.Count, actualList.Count);
        for (var i = 0; i < expectedList.Count; i++)
        {
            Assert.Equal(expectedList[i].Name, actualList[i].Name);
            Assert.Equal(expectedList[i].Title, actualList[i].Title);
            Assert.Equal(expectedList[i].Description, actualList[i].Description);
            Assert.Equal(expectedList[i].Required, actualList[i].Required);
        }
    }
}
