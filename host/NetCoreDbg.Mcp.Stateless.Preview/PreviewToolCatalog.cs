using System.Text.Json;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Stateless.Preview;

internal static class PreviewToolCatalog
{
    internal const string FindCodeSymbol = "find_code_symbol";
    internal const string ServerName = "netcoredbg-mcp-stateless-preview";
    internal const string ServerVersion = "1.0.0";
    internal const int MaximumCompleteResponseFrameBytes = 256 * 1024;
    internal static readonly TimeSpan CacheLifetime = TimeSpan.FromMinutes(5);

    internal static ListToolsResult List() => new()
    {
        Tools =
        [
            new Tool
            {
                Name = FindCodeSymbol,
                Description = "Find a C# symbol definition by name and optional kind.",
                InputSchema = JsonDocument.Parse("""{"type":"object","additionalProperties":false,"required":["name"],"properties":{"name":{"type":"string","minLength":1,"maxLength":256},"kind":{"type":["string","null"],"enum":["class","method","property","field",null]}}}""").RootElement.Clone(),
                OutputSchema = JsonDocument.Parse("""{"type":"object","additionalProperties":false,"required":["kind","results"],"properties":{"kind":{"const":"find_code_symbol_success"},"results":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["file","line","name","kind","context"],"properties":{"file":{"type":"string"},"line":{"type":"integer","minimum":1},"name":{"type":"string"},"kind":{"enum":["class","method","property","field"]},"context":{"type":"string"}}}}}}""").RootElement.Clone(),
                Annotations = new ToolAnnotations
                {
                    ReadOnlyHint = true,
                    IdempotentHint = true,
                    OpenWorldHint = false,
                },
            },
        ],
        TimeToLive = CacheLifetime,
        CacheScope = CacheScope.Public,
    };
}
