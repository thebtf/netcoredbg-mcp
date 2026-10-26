using System.Text.Json;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Stateless.Preview;

internal static class PreviewToolCatalog
{
    internal const string FindCodeSymbol = "find_code_symbol";
    internal const string FindCodeReferences = "find_code_references";
    internal const string GetSourceContext = "get_source_context";
    internal const string ServerName = "netcoredbg-mcp-stateless-preview";
    internal const string ServerVersion = "1.0.0";
    internal const int MaximumCompleteResponseFrameBytes = 256 * 1024;
    internal static readonly TimeSpan CacheLifetime = TimeSpan.FromMinutes(5);

    internal static bool IsKnownTool(string? tool) => tool is FindCodeSymbol or FindCodeReferences or GetSourceContext;

    internal static ListToolsResult List()
    {
        using var inputSchema = JsonDocument.Parse("""{"type":"object","additionalProperties":false,"required":["name"],"properties":{"name":{"type":"string","minLength":1,"maxLength":256},"kind":{"type":["string","null"],"enum":["class","method","property","field",null]}}}""");
        using var outputSchema = JsonDocument.Parse("""{"type":"object","additionalProperties":false,"required":["kind","results"],"properties":{"kind":{"const":"find_code_symbol_success"},"results":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["file","line","name","kind","context"],"properties":{"file":{"type":"string"},"line":{"type":"integer","minimum":1},"name":{"type":"string"},"kind":{"enum":["class","method","property","field"]},"context":{"type":"string"}}}}}}""");
        using var referencesInput = JsonDocument.Parse("""{"type":"object","additionalProperties":false,"required":["name"],"properties":{"name":{"type":"string","minLength":1,"maxLength":256},"max_results":{"type":"integer","minimum":1,"maximum":2147483647,"default":1000}}}""");
        using var referencesOutput = JsonDocument.Parse("""{"type":"object","additionalProperties":false,"required":["kind","results"],"properties":{"kind":{"const":"find_code_references_success"},"results":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["file","line","context"],"properties":{"file":{"type":"string"},"line":{"type":"integer","minimum":1},"context":{"type":"string"}}}}}}""");
        using var contextInput = JsonDocument.Parse("""{"type":"object","additionalProperties":false,"required":["file","line"],"properties":{"file":{"type":"string","minLength":1},"line":{"type":"integer","minimum":1,"maximum":2147483647},"radius":{"type":"integer","minimum":0,"maximum":2147483647,"default":10}}}""");
        using var contextOutput = JsonDocument.Parse("""{"type":"object","additionalProperties":false,"required":["kind","file","start_line","end_line","lines"],"properties":{"kind":{"const":"get_source_context_success"},"file":{"type":"string"},"start_line":{"type":"integer","minimum":1},"end_line":{"type":"integer","minimum":1},"lines":{"type":"array","items":{"type":"object","additionalProperties":false,"required":["line","text"],"properties":{"line":{"type":"integer","minimum":1},"text":{"type":"string"}}}}}}""");
        return new ListToolsResult
        {
            Tools =
            [
                new Tool
                {
                    Name = FindCodeSymbol,
                    Description = "Find a C# symbol definition by name and optional kind.",
                    InputSchema = inputSchema.RootElement.Clone(),
                    OutputSchema = outputSchema.RootElement.Clone(),
                    Annotations = new ToolAnnotations
                    {
                        ReadOnlyHint = true,
                        IdempotentHint = true,
                        OpenWorldHint = false,
                    },
                },
                new Tool
                {
                    Name = FindCodeReferences,
                    Description = "Find C# source references to a name within the project.",
                    InputSchema = referencesInput.RootElement.Clone(),
                    OutputSchema = referencesOutput.RootElement.Clone(),
                    Annotations = new ToolAnnotations
                    {
                        ReadOnlyHint = true,
                        IdempotentHint = true,
                        OpenWorldHint = false,
                    },
                },
                new Tool
                {
                    Name = GetSourceContext,
                    Description = "Read a numbered C# source window within the project.",
                    InputSchema = contextInput.RootElement.Clone(),
                    OutputSchema = contextOutput.RootElement.Clone(),
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
}
