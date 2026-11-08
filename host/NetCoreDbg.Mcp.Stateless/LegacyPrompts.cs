using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host;

internal static partial class NativePrompts
{
    private const string NativeLimit =
        "Native legacy mode: some UI tools and other commands or arguments may not yet be available. " +
        "Check tools/list schemas; Python and native sessions do not share debug state.\n\n";

    internal static ListPromptsResult ListLegacyPrompts() => ListPromptsResultInstance;

    internal static GetPromptResult GetLegacyPrompt(GetPromptRequestParams request)
    {
        var result = GetPrompt(request);
        if (result.Messages[0].Content is not TextContentBlock text)
            throw new InvalidOperationException("The native prompt must begin with text so its compatibility limits can be disclosed.");
        text.Text = NativeLimit + text.Text;
        return result;
    }
}
