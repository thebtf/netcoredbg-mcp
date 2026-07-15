using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Load-bearing CRLF regression coverage. C# raw string literals preserve the PHYSICAL
/// line-ending bytes of their .cs source file verbatim; this repo has core.autocrlf=true
/// and no .gitattributes override, so a plain Windows <c>git checkout</c>/worktree/
/// cherry-pick materializes <c>NativePromptsContent.cs</c>/<c>NativePromptsPlaybooks.cs</c>
/// as CRLF even though direct Python's own triple-quoted prompt/playbook strings are
/// always LF. Without <see cref="NativePrompts.NormalizeSourceOwnedText"/>, that leaks
/// "\r\n" into every static prompt and exception playbook's rendered text on a fresh
/// checkout, even though the very same source compiled fine (and these tests passed)
/// from a working tree whose files happened to already be LF. These tests assert the
/// LF-only invariant directly, so they fail on this class of regression regardless of
/// which line-ending style the working tree happens to have when they run - and prove
/// the fix does not overreach into caller-supplied argument values, which must survive
/// untouched even when they contain a literal CRLF.
/// </summary>
[Collection(PythonBaselineCollection.Name)]
public sealed class PromptLineEndingTests
{
    private readonly PythonBaselineFixture _python;

    public PromptLineEndingTests(PythonBaselineFixture python) => _python = python;

    [Theory]
    [InlineData("debug")]
    [InlineData("debug-gui")]
    [InlineData("debug-exception")]
    [InlineData("debug-visual")]
    [InlineData("debug-mistakes")]
    [InlineData("dap-escape-hatch")]
    public async Task GetPrompt_ArgumentLessPrompt_RenderedTextHasNoCarriageReturn(string name)
    {
        await using var native = await NativePromptsHost.StartAsync();

        var result = await native.Client.GetPromptAsync(name);

        AssertNoCarriageReturn(result);
    }

    [Theory]
    [InlineData("NullReferenceException")]
    [InlineData("InvalidOperationException")]
    [InlineData("ObjectDisposedException")]
    [InlineData("deadlock")]
    [InlineData("crash")]
    [InlineData("performance issue")]
    [InlineData("something totally unknown xyz")]
    public async Task GetPrompt_Investigate_RenderedTextHasNoCarriageReturn(string symptom)
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?> { ["symptom"] = symptom };

        var result = await native.Client.GetPromptAsync("investigate", arguments);

        AssertNoCarriageReturn(result);
    }

    [Fact]
    public async Task GetPrompt_DebugScenario_RenderedTextHasNoCarriageReturn()
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?> { ["problem"] = "button click doesn't save" };

        var result = await native.Client.GetPromptAsync("debug-scenario", arguments);

        AssertNoCarriageReturn(result);
    }

    /// <summary>
    /// Direct Python renders these prompts identically on every platform because its
    /// triple-quoted strings are always LF in the running interpreter regardless of the
    /// source .py file's checked-out line endings. This asserts the native module matches
    /// that same LF-only baseline (not merely "no \r locally"), so a regression here would
    /// also show up as a parity failure against the live baseline.
    /// </summary>
    [Theory]
    [InlineData("debug")]
    [InlineData("debug-exception")]
    public async Task GetPrompt_RenderedText_MatchesPythonBaseline_NoCarriageReturn(string name)
    {
        await using var native = await NativePromptsHost.StartAsync();

        var nativeResult = await native.Client.GetPromptAsync(name);
        var pythonResult = await _python.Server.Client.GetPromptAsync(name);

        AssertNoCarriageReturn(nativeResult);
        AssertNoCarriageReturn(pythonResult);
        for (var i = 0; i < pythonResult.Messages.Count; i++)
        {
            var expectedText = ((TextContentBlock)pythonResult.Messages[i].Content).Text;
            var actualText = ((TextContentBlock)nativeResult.Messages[i].Content).Text;
            Assert.Equal(expectedText, actualText);
        }
    }

    /// <summary>
    /// The fix must be scoped to source-owned template text only: a caller-supplied
    /// argument value that itself contains a literal CRLF is opaque user data (direct
    /// Python does not touch it either) and must survive byte-for-byte, not get
    /// blanket-normalized away.
    /// </summary>
    [Fact]
    public async Task GetPrompt_Investigate_PreservesCallerSuppliedCarriageReturnInSymptom()
    {
        await using var native = await NativePromptsHost.StartAsync();
        const string symptom = "custom crash\r\nwith an embedded CRLF the caller supplied";
        var arguments = new Dictionary<string, object?> { ["symptom"] = symptom };

        var result = await native.Client.GetPromptAsync("investigate", arguments);

        var text = ((TextContentBlock)result.Messages[0].Content).Text;
        Assert.Contains(symptom, text);
    }

    [Fact]
    public async Task GetPrompt_DebugScenario_PreservesCallerSuppliedCarriageReturnInFileHint()
    {
        await using var native = await NativePromptsHost.StartAsync();
        const string fileHint = "Weird\r\nPath.cs";
        var arguments = new Dictionary<string, object?> { ["problem"] = "x", ["file_hint"] = fileHint };

        var result = await native.Client.GetPromptAsync("debug-scenario", arguments);

        var text = ((TextContentBlock)result.Messages[0].Content).Text;
        Assert.Contains(fileHint, text);
    }

    private static void AssertNoCarriageReturn(GetPromptResult result)
    {
        foreach (var message in result.Messages)
        {
            var text = ((TextContentBlock)message.Content).Text;
            Assert.DoesNotContain('\r', text);
        }
    }
}
