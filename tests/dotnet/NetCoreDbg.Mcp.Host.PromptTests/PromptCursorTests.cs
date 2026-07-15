using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Cursor/pagination parity: direct Python's low-level <c>list_prompts()</c> decorator
/// (<c>mcp.server.lowlevel.server.Server.list_prompts</c>) calls the zero-argument
/// <c>FastMCP.list_prompts()</c> handler regardless of the request's cursor and always
/// returns a plain <c>ListPromptsResult(prompts=result)</c> with no <c>nextCursor</c> - the
/// cursor is accepted syntactically but never consulted. The native module reproduces
/// this by ignoring <see cref="ListPromptsRequestParams.Cursor"/> entirely and always
/// returning the full static catalog with <see cref="ListPromptsResult.NextCursor"/> left
/// unset.
/// </summary>
[Collection(PythonBaselineCollection.Name)]
public sealed class PromptCursorTests
{
    private readonly PythonBaselineFixture _python;

    public PromptCursorTests(PythonBaselineFixture python) => _python = python;

    [Fact]
    public async Task ListPrompts_NoParams_ReturnsAllEightAndNoNextCursor()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var result = await native.Client.ListPromptsAsync(new ListPromptsRequestParams());

        Assert.Equal(8, result.Prompts.Count);
        Assert.Null(result.NextCursor);
    }

    [Theory]
    [InlineData(null)]
    [InlineData("")]
    [InlineData("some-bogus-cursor-value")]
    [InlineData("dW5rbm93bg==")]
    public async Task ListPrompts_AnyCursorValue_IsIgnored_MatchesDirectPythonBaseline(string? cursor)
    {
        await using var native = await NativePromptsHost.StartAsync();
        var requestParams = new ListPromptsRequestParams { Cursor = cursor };

        var nativeResult = await native.Client.ListPromptsAsync(requestParams);
        var pythonResult = await _python.Server.Client.ListPromptsAsync(requestParams);

        Assert.Equal(pythonResult.Prompts.Count, nativeResult.Prompts.Count);
        Assert.Equal(pythonResult.NextCursor, nativeResult.NextCursor);
        Assert.Null(nativeResult.NextCursor);
        Assert.Equal(pythonResult.Prompts.Select(p => p.Name), nativeResult.Prompts.Select(p => p.Name));
    }

    [Fact]
    public async Task ListPromptsAsync_AutoPaginatingOverload_ReturnsExactlyEight()
    {
        await using var native = await NativePromptsHost.StartAsync();

        var prompts = await native.Client.ListPromptsAsync();

        Assert.Equal(8, prompts.Count);
    }
}
