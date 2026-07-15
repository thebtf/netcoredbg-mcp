using ModelContextProtocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Unknown-prompt and missing-required-argument error parity: direct Python's
/// <c>FastMCP.get_prompt</c>/<c>Prompt.render</c> raise a bare <c>ValueError</c>, which the
/// low-level server turns into a JSON-RPC error with code 0 and the exact
/// <c>str(ValueError)</c> text
/// (<c>mcp.server.lowlevel.server.Server._handle_request</c>'s generic
/// <c>except Exception as err: response = types.ErrorData(code=0, message=str(err))</c>
/// path). These tests assert the exact same client-observed exception message and error
/// code from the native module, both against a fixed expectation and against the live
/// Python baseline, so a client sees no observable difference either way.
/// </summary>
[Collection(PythonBaselineCollection.Name)]
public sealed class PromptErrorParityTests
{
    private readonly PythonBaselineFixture _python;

    public PromptErrorParityTests(PythonBaselineFixture python) => _python = python;

    [Fact]
    public async Task GetPrompt_UnknownName_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var nativeException = await Assert.ThrowsAsync<McpProtocolException>(
            () => native.Client.GetPromptAsync("nonexistent-prompt").AsTask());
        var pythonException = await Assert.ThrowsAsync<McpProtocolException>(
            () => _python.Server.Client.GetPromptAsync("nonexistent-prompt").AsTask());

        Assert.Equal(pythonException.Message, nativeException.Message);
        Assert.Equal(pythonException.ErrorCode, nativeException.ErrorCode);
        Assert.Equal(0, (int)nativeException.ErrorCode);
        Assert.Contains("Unknown prompt: nonexistent-prompt", nativeException.Message);
    }

    [Fact]
    public async Task GetPrompt_UnknownName_WithArguments_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?> { ["a"] = "b" };

        var nativeException = await Assert.ThrowsAsync<McpProtocolException>(
            () => native.Client.GetPromptAsync("nonexistent-prompt", arguments).AsTask());
        var pythonException = await Assert.ThrowsAsync<McpProtocolException>(
            () => _python.Server.Client.GetPromptAsync("nonexistent-prompt", arguments).AsTask());

        Assert.Equal(pythonException.Message, nativeException.Message);
        Assert.Equal(pythonException.ErrorCode, nativeException.ErrorCode);
    }

    [Fact]
    public async Task GetPrompt_Investigate_MissingSymptom_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var nativeException = await Assert.ThrowsAsync<McpProtocolException>(
            () => native.Client.GetPromptAsync("investigate").AsTask());
        var pythonException = await Assert.ThrowsAsync<McpProtocolException>(
            () => _python.Server.Client.GetPromptAsync("investigate").AsTask());

        Assert.Equal(pythonException.Message, nativeException.Message);
        Assert.Equal(pythonException.ErrorCode, nativeException.ErrorCode);
        Assert.Contains("Missing required arguments: {'symptom'}", nativeException.Message);
    }

    [Fact]
    public async Task GetPrompt_Investigate_MissingSymptom_AppTypeOnlySupplied_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?> { ["app_type"] = "console" };

        var nativeException = await Assert.ThrowsAsync<McpProtocolException>(
            () => native.Client.GetPromptAsync("investigate", arguments).AsTask());
        var pythonException = await Assert.ThrowsAsync<McpProtocolException>(
            () => _python.Server.Client.GetPromptAsync("investigate", arguments).AsTask());

        Assert.Equal(pythonException.Message, nativeException.Message);
    }

    [Fact]
    public async Task GetPrompt_DebugScenario_MissingProblem_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var nativeException = await Assert.ThrowsAsync<McpProtocolException>(
            () => native.Client.GetPromptAsync("debug-scenario").AsTask());
        var pythonException = await Assert.ThrowsAsync<McpProtocolException>(
            () => _python.Server.Client.GetPromptAsync("debug-scenario").AsTask());

        Assert.Equal(pythonException.Message, nativeException.Message);
        Assert.Equal(pythonException.ErrorCode, nativeException.ErrorCode);
        Assert.Contains("Missing required arguments: {'problem'}", nativeException.Message);
    }

    [Fact]
    public async Task GetPrompt_DebugScenario_MissingProblem_OtherArgumentsSupplied_MatchesDirectPythonBaseline()
    {
        await using var native = await NativePromptsHost.StartAsync();
        var arguments = new Dictionary<string, object?> { ["app_type"] = "console", ["file_hint"] = "Foo.cs" };

        var nativeException = await Assert.ThrowsAsync<McpProtocolException>(
            () => native.Client.GetPromptAsync("debug-scenario", arguments).AsTask());
        var pythonException = await Assert.ThrowsAsync<McpProtocolException>(
            () => _python.Server.Client.GetPromptAsync("debug-scenario", arguments).AsTask());

        Assert.Equal(pythonException.Message, nativeException.Message);
    }

    [Theory]
    [InlineData("investigate")]
    [InlineData("debug-scenario")]
    public async Task GetPrompt_EmptyRequiredArgument_IsNotTreatedAsMissing(string promptName)
    {
        // Matches direct Python's Prompt.render(): required-argument presence is a KEY
        // membership check (`required - set(arguments or {})`), not a value-truthiness
        // check. An explicitly supplied empty string still counts as "provided" and must
        // render successfully rather than raise "Missing required arguments".
        await using var native = await NativePromptsHost.StartAsync();
        var requiredArgumentName = promptName == "investigate" ? "symptom" : "problem";
        var arguments = new Dictionary<string, object?> { [requiredArgumentName] = "" };

        var nativeResult = await native.Client.GetPromptAsync(promptName, arguments);
        var pythonResult = await _python.Server.Client.GetPromptAsync(promptName, arguments);

        Assert.Equal(pythonResult.Description, nativeResult.Description);
        Assert.Equal(pythonResult.Messages.Count, nativeResult.Messages.Count);
    }
}
