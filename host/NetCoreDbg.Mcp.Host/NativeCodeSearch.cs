using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using NetCoreDbg.Mcp.CodeSearch.Core;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// BCL-only implementation of Python's three deterministic project-scoped code-search tools.
/// It is deliberately request-local: resolving a client root here never changes Python's
/// SessionManager.project_path and scanning never shares mutable state between calls.
/// </summary>
internal sealed class NativeCodeSearch
{
    private const string IdleState = "idle";
    private const string IdleMessage = "No active debug session.";
    private const string UnconfiguredProjectMessage =
        "Project root is not configured. Start with --project or --project-from-cwd.";
    private static readonly string[] NextActions =
    [
        "find_code_symbol",
        "find_code_references",
        "get_source_context",
        "search_source",
    ];

    private readonly ProjectRootResolver _projectRootResolver;
    private readonly RelaySession _session;

    internal NativeCodeSearch(ProjectRootResolver projectRootResolver, RelaySession session)
    {
        _projectRootResolver = projectRootResolver;
        _session = session;
    }

    internal static bool IsNativeTool(string name) => NativeCodeSearchCatalog.IsNativeTool(name);

    internal async ValueTask<CallToolResult> CallAsync(
        RequestContext<CallToolRequestParams> context,
        CancellationToken cancellationToken)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken,
            _session.SessionEndingToken);

        try
        {
            var arguments = context.Params.Arguments;
            return context.Params.Name switch
            {
                "find_code_symbol" => await FindCodeSymbolAsync(context.Server, arguments, linked.Token).ConfigureAwait(false),
                "find_code_references" => await FindCodeReferencesAsync(context.Server, arguments, linked.Token).ConfigureAwait(false),
                "get_source_context" => await GetSourceContextAsync(context.Server, arguments, linked.Token).ConfigureAwait(false),
                _ => throw new InvalidOperationException($"Unsupported native code-search tool: {context.Params.Name}"),
            };
        }
        catch (OperationCanceledException)
        {
            // MCP cancellation is not a tool result. Propagating it lets the SDK emit the
            // matching request cancellation and interrupts both roots/list and file scanning.
            throw;
        }
        catch (Exception ex)
        {
            return Error(ex.Message);
        }
    }

    private async ValueTask<CallToolResult> FindCodeSymbolAsync(
        McpServer server,
        IDictionary<string, JsonElement>? arguments,
        CancellationToken cancellationToken)
    {
        var name = RequiredString(arguments, "name");
        var kind = OptionalString(arguments, "kind");
        var engine = await CreateEngineAsync(server, cancellationToken).ConfigureAwait(false);
        var results = ToJson(engine.FindCodeSymbol(name, kind, cancellationToken));
        return Success(new JsonObject
        {
            ["results"] = results,
            ["count"] = results.Count,
            ["project_root"] = engine.ProjectRoot,
        });
    }

    private async ValueTask<CallToolResult> FindCodeReferencesAsync(
        McpServer server,
        IDictionary<string, JsonElement>? arguments,
        CancellationToken cancellationToken)
    {
        var name = RequiredString(arguments, "name");
        var maxResults = OptionalInt32(arguments, "max_results", 1000);
        var engine = await CreateEngineAsync(server, cancellationToken).ConfigureAwait(false);
        var results = ToJson(engine.FindCodeReferences(name, maxResults, cancellationToken));
        return Success(new JsonObject
        {
            ["results"] = results,
            ["count"] = results.Count,
            ["project_root"] = engine.ProjectRoot,
        });
    }

    private async ValueTask<CallToolResult> GetSourceContextAsync(
        McpServer server,
        IDictionary<string, JsonElement>? arguments,
        CancellationToken cancellationToken)
    {
        var file = RequiredString(arguments, "file");
        var line = RequiredInt32(arguments, "line");
        var radius = OptionalInt32(arguments, "radius", 10);
        var engine = await CreateEngineAsync(server, cancellationToken).ConfigureAwait(false);
        var sourceContext = ToJson(engine.GetSourceContext(file, line, radius, cancellationToken));
        sourceContext["project_root"] = engine.ProjectRoot;
        return Success(sourceContext);
    }


    private async ValueTask<SymbolSearchEngine> CreateEngineAsync(McpServer server, CancellationToken cancellationToken)
    {
        var projectRoot = await _projectRootResolver.ResolveAsync(server, cancellationToken).ConfigureAwait(false);
        if (projectRoot is null)
        {
            throw new InvalidOperationException(UnconfiguredProjectMessage);
        }

        return new SymbolSearchEngine(projectRoot, LegacySearchPolicy.Instance);
    }

    private static JsonArray ToJson(IReadOnlyList<SymbolMatch> matches)
    {
        var results = new JsonArray();
        foreach (var match in matches)
        {
            results.Add(new JsonObject
            {
                ["file"] = match.File,
                ["line"] = match.Line,
                ["name"] = match.Name,
                ["kind"] = match.Kind,
                ["context"] = match.Context,
            });
        }

        return results;
    }

    private static JsonArray ToJson(IReadOnlyList<ReferenceMatch> matches)
    {
        var results = new JsonArray();
        foreach (var match in matches)
        {
            results.Add(new JsonObject
            {
                ["file"] = match.File,
                ["line"] = match.Line,
                ["context"] = match.Context,
            });
        }

        return results;
    }

    private static JsonObject ToJson(SourceContext sourceContext)
    {
        var lines = new JsonArray();
        foreach (var line in sourceContext.Lines)
        {
            lines.Add(new JsonObject
            {
                ["line"] = line.Line,
                ["text"] = line.Text,
            });
        }

        return new JsonObject
        {
            ["file"] = sourceContext.File,
            ["start_line"] = sourceContext.StartLine,
            ["end_line"] = sourceContext.EndLine,
            ["lines"] = lines,
        };
    }

    private static string RequiredString(IDictionary<string, JsonElement>? arguments, string name)
    {
        if (arguments?.TryGetValue(name, out var value) != true || value.ValueKind != JsonValueKind.String)
        {
            throw new ArgumentException($"{name} must be a string");
        }

        return value.GetString()!;
    }

    private static string? OptionalString(IDictionary<string, JsonElement>? arguments, string name)
    {
        if (arguments?.TryGetValue(name, out var value) != true || value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }

        if (value.ValueKind != JsonValueKind.String)
        {
            throw new ArgumentException($"{name} must be a string or null");
        }

        return value.GetString();
    }

    private static int RequiredInt32(IDictionary<string, JsonElement>? arguments, string name)
    {
        if (arguments?.TryGetValue(name, out var value) != true
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt32(out var result))
        {
            throw new ArgumentException($"{name} must be an integer");
        }

        return result;
    }

    private static int OptionalInt32(IDictionary<string, JsonElement>? arguments, string name, int defaultValue)
    {
        if (arguments?.TryGetValue(name, out var value) != true)
        {
            return defaultValue;
        }

        if (value.ValueKind != JsonValueKind.Number || !value.TryGetInt32(out var result))
        {
            throw new ArgumentException($"{name} must be an integer");
        }

        return result;
    }


    private static CallToolResult Success(JsonObject data) => Result(new JsonObject
    {
        ["state"] = IdleState,
        ["next_actions"] = ActionsArray(),
        ["message"] = IdleMessage,
        ["data"] = data,
    }, isError: false);

    private static CallToolResult Error(string error) => Result(new JsonObject
    {
        ["error"] = error,
        ["state"] = IdleState,
        ["next_actions"] = ActionsArray(),
        ["message"] = $"Error: {error}. Try one of the suggested next_actions.",
    }, isError: false);

    private static JsonArray ActionsArray()
    {
        var actions = new JsonArray();
        foreach (var action in NextActions)
        {
            actions.Add(action);
        }

        return actions;
    }

    private static CallToolResult Result(JsonObject payload, bool isError) => new()
    {
        Content = [new TextContentBlock { Text = PythonJson.Serialize(payload) }],
        StructuredContent = JsonSerializer.SerializeToElement(payload),
        IsError = isError,
    };
}


/// <summary>Exact public definitions for the three deterministic native replacements.</summary>
internal static class NativeCodeSearchCatalog
{
    private static readonly string[] Names =
    [
        "find_code_symbol",
        "find_code_references",
        "get_source_context",
    ];

    internal static bool IsNativeTool(string name) => Names.Contains(name, StringComparer.Ordinal);

    internal static void ReplaceInCatalog(IList<Tool> tools)
    {
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (var index = 0; index < tools.Count; index++)
        {
            var name = tools[index].Name;
            if (!seen.Add(name))
            {
                throw new InvalidOperationException($"Python tools/list returned duplicate tool name '{name}'.");
            }

            if (IsNativeTool(name))
            {
                tools[index] = Definition(name);
            }
        }

    }

    private static Tool Definition(string name) => name switch
    {
        "find_code_symbol" => Tool(
            name,
            "Find a C# symbol definition by name and optional kind.",
            """{"properties":{"name":{"title":"Name","type":"string"},"kind":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Kind"}},"required":["name"],"title":"find_code_symbolArguments","type":"object"}"""),
        "find_code_references" => Tool(
            name,
            "Find literal symbol references across project files.",
            """{"properties":{"name":{"title":"Name","type":"string"},"max_results":{"default":1000,"title":"Max Results","type":"integer"}},"required":["name"],"title":"find_code_referencesArguments","type":"object"}"""),
        "get_source_context" => Tool(
            name,
            "Read source lines around a project-scoped location.",
            """{"properties":{"file":{"title":"File","type":"string"},"line":{"title":"Line","type":"integer"},"radius":{"default":10,"title":"Radius","type":"integer"}},"required":["file","line"],"title":"get_source_contextArguments","type":"object"}"""),
        _ => throw new ArgumentOutOfRangeException(nameof(name)),
    };

    private static Tool Tool(string name, string description, string inputSchema) => new()
    {
        Name = name,
        Description = description,
        InputSchema = JsonDocument.Parse(inputSchema).RootElement.Clone(),
        OutputSchema = JsonDocument.Parse($"{{\"additionalProperties\":true,\"title\":\"{name}DictOutput\",\"type\":\"object\"}}").RootElement.Clone(),
        Annotations = new ToolAnnotations
        {
            ReadOnlyHint = true,
            IdempotentHint = true,
            OpenWorldHint = false,
        },
    };
}

/// <summary>Writes the text block with Python json.dumps(..., indent=2, ensure_ascii=True) semantics.</summary>
internal static class PythonJson
{
    internal static string Serialize(JsonNode node)
    {
        using var document = JsonDocument.Parse(node.ToJsonString());
        var result = new StringBuilder();
        Write(document.RootElement, result, 0);
        return result.ToString();
    }

    private static void Write(JsonElement element, StringBuilder output, int depth)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                var properties = element.EnumerateObject().ToArray();
                output.Append('{');
                if (properties.Length == 0)
                {
                    output.Append('}');
                    return;
                }

                output.Append('\n');
                for (var index = 0; index < properties.Length; index++)
                {
                    Indent(output, depth + 1);
                    WriteString(properties[index].Name, output);
                    output.Append(": ");
                    Write(properties[index].Value, output, depth + 1);
                    if (index + 1 < properties.Length)
                    {
                        output.Append(',');
                    }
                    output.Append('\n');
                }
                Indent(output, depth);
                output.Append('}');
                return;

            case JsonValueKind.Array:
                var values = element.EnumerateArray().ToArray();
                output.Append('[');
                if (values.Length == 0)
                {
                    output.Append(']');
                    return;
                }

                output.Append('\n');
                for (var index = 0; index < values.Length; index++)
                {
                    Indent(output, depth + 1);
                    Write(values[index], output, depth + 1);
                    if (index + 1 < values.Length)
                    {
                        output.Append(',');
                    }
                    output.Append('\n');
                }
                Indent(output, depth);
                output.Append(']');
                return;

            case JsonValueKind.String:
                WriteString(element.GetString()!, output);
                return;
            case JsonValueKind.Number:
                output.Append(element.GetRawText());
                return;
            case JsonValueKind.True:
                output.Append("true");
                return;
            case JsonValueKind.False:
                output.Append("false");
                return;
            case JsonValueKind.Null:
                output.Append("null");
                return;
            default:
                throw new InvalidOperationException($"Unsupported JSON value kind: {element.ValueKind}");
        }
    }

    private static void Indent(StringBuilder output, int depth) => output.Append(' ', depth * 2);

    private static void WriteString(string value, StringBuilder output)
    {
        output.Append('"');
        foreach (var character in value)
        {
            switch (character)
            {
                case '"': output.Append("\\\""); break;
                case '\\': output.Append("\\\\"); break;
                case '\b': output.Append("\\b"); break;
                case '\f': output.Append("\\f"); break;
                case '\n': output.Append("\\n"); break;
                case '\r': output.Append("\\r"); break;
                case '\t': output.Append("\\t"); break;
                case var control when control < ' ':
                    output.Append("\\u");
                    output.Append(((int)control).ToString("x4", CultureInfo.InvariantCulture));
                    break;
                default:
                    output.Append(character);
                    break;
            }
        }
        output.Append('"');
    }
}
