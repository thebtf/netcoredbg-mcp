using System.Text.Json;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using NetCoreDbg.Mcp.CodeSearch.Core;

namespace NetCoreDbg.Mcp.Stateless.Preview;

internal sealed class PreviewToolHandler
{
    private readonly SymbolSearchEngine _search;
    private readonly Action? _beforeResponse;

    internal PreviewToolHandler(SymbolSearchEngine search, Action? beforeResponse = null)
    {
        _search = search;
        _beforeResponse = beforeResponse;
    }

    internal ValueTask<CallToolResult> CallAsync(
        RequestContext<CallToolRequestParams> context,
        CancellationToken cancellationToken) =>
        CallAsync(context.Params, context.JsonRpcRequest.Id, cancellationToken);

    internal ValueTask<CallToolResult> CallAsync(
        CallToolRequestParams? request,
        RequestId requestId,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (request is null)
        {
            return Respond(InvalidToolArguments(), cancellationToken);
        }
        if (request.Name is null)
        {
            return Respond(InvalidToolArguments(), cancellationToken);
        }

        if (!PreviewToolCatalog.IsKnownTool(request.Name))
        {
            return Respond(
                BoundedResponseFrameSerializer.FitsUnknownToolResponseWithinLimit(requestId, request.Name, out _)
                    ? UnknownTool(request.Name)
                    : FrameBudgetExceeded(),
                cancellationToken);
        }

        try
        {
            switch (request.Name)
            {
                case PreviewToolCatalog.FindCodeSymbol:
                    if (!TryReadArguments(request.Arguments, out var name, out var kind))
                    {
                        return Respond(InvalidToolArguments(request.Name), cancellationToken);
                    }
                    var matches = _search.FindCodeSymbol(name, kind, cancellationToken);
                    return Respond(FindCodeSymbolResultOrFrameBudgetExceeded(requestId, matches), cancellationToken);
                case PreviewToolCatalog.FindCodeReferences:
                    if (!TryReadReferenceArguments(request.Arguments, out var referenceName, out var maximum))
                    {
                        return Respond(InvalidToolArguments(request.Name), cancellationToken);
                    }
                    var references = _search.FindCodeReferences(referenceName, maximum, cancellationToken);
                    return Respond(FindCodeReferencesResultOrFrameBudgetExceeded(requestId, references), cancellationToken);
                case PreviewToolCatalog.GetSourceContext:
                    if (!TryReadContextArguments(request.Arguments, out var file, out var line, out var radius))
                    {
                        return Respond(InvalidToolArguments(request.Name), cancellationToken);
                    }
                    var context = _search.GetSourceContext(file, line, radius, cancellationToken);
                    return Respond(GetSourceContextResultOrFrameBudgetExceeded(requestId, context), cancellationToken);
                default:
                    throw new InvalidOperationException("Known preview tool has no adapter.");
            }
        }
        catch (SearchFailureException exception)
        {
            return Respond(Error(exception.Failure), cancellationToken);
        }
    }

    private ValueTask<CallToolResult> Respond(CallToolResult result, CancellationToken cancellationToken)
    {
        _beforeResponse?.Invoke();
        cancellationToken.ThrowIfCancellationRequested();
        return ValueTask.FromResult(result);
    }

    private static bool TryReadArguments(IDictionary<string, JsonElement>? arguments, out string name, out string? kind)
    {
        name = string.Empty;
        kind = null;
        if (arguments is null
            || arguments.Count is < 1 or > 2
            || arguments.Keys.Any(static key => key is not "name" and not "kind")
            || !arguments.TryGetValue("name", out var nameElement)
            || nameElement.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        name = nameElement.GetString()!;
        if (string.IsNullOrWhiteSpace(name) || name.Length > 256)
        {
            return false;
        }

        if (!arguments.TryGetValue("kind", out var kindElement) || kindElement.ValueKind == JsonValueKind.Null)
        {
            return true;
        }

        if (kindElement.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        kind = kindElement.GetString();
        return kind is "class" or "method" or "property" or "field";
    }

    private static bool TryReadReferenceArguments(
        IDictionary<string, JsonElement>? arguments,
        out string name,
        out int maximum)
    {
        name = string.Empty;
        maximum = 1000;
        if (arguments is null
            || arguments.Count is < 1 or > 2
            || arguments.Keys.Any(static key => key is not "name" and not "max_results")
            || !arguments.TryGetValue("name", out var nameElement)
            || nameElement.ValueKind != JsonValueKind.String)
        {
            return false;
        }

        name = nameElement.GetString()!;
        return !string.IsNullOrWhiteSpace(name) && name.Length <= 256
            && (!arguments.TryGetValue("max_results", out var maximumElement)
                || (maximumElement.ValueKind == JsonValueKind.Number
                    && maximumElement.TryGetInt32(out maximum) && maximum > 0));
    }

    private static bool TryReadContextArguments(
        IDictionary<string, JsonElement>? arguments,
        out string file,
        out int line,
        out int radius)
    {
        file = string.Empty;
        line = 0;
        radius = 10;
        if (arguments is null
            || arguments.Count is < 2 or > 3
            || arguments.Keys.Any(static key => key is not "file" and not "line" and not "radius")
            || !arguments.TryGetValue("file", out var fileElement)
            || fileElement.ValueKind != JsonValueKind.String
            || !arguments.TryGetValue("line", out var lineElement)
            || lineElement.ValueKind != JsonValueKind.Number
            || !lineElement.TryGetInt32(out line)
            || line <= 0)
        {
            return false;
        }

        file = fileElement.GetString()!;
        return !string.IsNullOrWhiteSpace(file)
            && (!arguments.TryGetValue("radius", out var radiusElement)
                || (radiusElement.ValueKind == JsonValueKind.Number
                    && radiusElement.TryGetInt32(out radius) && radius >= 0));
    }

    private static CallToolResult FindCodeSymbolResultOrFrameBudgetExceeded(
        RequestId id,
        IReadOnlyList<SymbolMatch> matches)
    {
        if (!BoundedResponseFrameSerializer.FitsFindCodeSymbolSuccessResponseWithinLimit(id, matches, out _))
        {
            return FrameBudgetExceeded();
        }

        return Result(new
        {
            kind = "find_code_symbol_success",
            results = matches.Select(static match => new
            {
                file = match.File,
                line = match.Line,
                name = match.Name,
                kind = match.Kind,
                context = match.Context,
            }).ToArray(),
        }, isError: false);
    }

    private static CallToolResult FindCodeReferencesResultOrFrameBudgetExceeded(
        RequestId id,
        IReadOnlyList<ReferenceMatch> matches)
    {
        if (!BoundedResponseFrameSerializer.FitsFindCodeReferencesSuccessResponseWithinLimit(id, matches, out _))
        {
            return FrameBudgetExceeded(PreviewToolCatalog.FindCodeReferences);
        }

        return Result(new
        {
            kind = "find_code_references_success",
            results = matches.Select(static match => new
            {
                file = match.File,
                line = match.Line,
                context = match.Context,
            }).ToArray(),
        }, isError: false);
    }

    private static CallToolResult GetSourceContextResultOrFrameBudgetExceeded(RequestId id, SourceContext context)
    {
        if (!BoundedResponseFrameSerializer.FitsGetSourceContextSuccessResponseWithinLimit(id, context, out _))
        {
            return FrameBudgetExceeded(PreviewToolCatalog.GetSourceContext);
        }

        return Result(new
        {
            kind = "get_source_context_success",
            file = context.File,
            start_line = context.StartLine,
            end_line = context.EndLine,
            lines = context.Lines.Select(static line => new { line = line.Line, text = line.Text }).ToArray(),
        }, isError: false);
    }

    internal static CallToolResult FrameBudgetExceeded(string tool = PreviewToolCatalog.FindCodeSymbol) =>
        Error(SearchFailure.PreviewSearchBudgetExceeded(tool));

    internal static CallToolResult InvalidToolArguments(string tool = PreviewToolCatalog.FindCodeSymbol) =>
        Error(SearchFailure.InvalidToolArguments(tool));

    private static CallToolResult UnknownTool(string tool) => new()
    {
        ResultType = "complete",
        IsError = true,
        Content = [new TextContentBlock
        {
            Text = $"Unknown tool: {tool}",
        }],
    };

    private static CallToolResult Error(SearchFailure failure) => Result(new
    {
        kind = failure.Kind,
        error = failure.Error,
        tool = failure.Tool,
    }, isError: true);

    private static CallToolResult Result<T>(T payload, bool isError)
    {
        var text = JsonSerializer.Serialize(payload);
        return new CallToolResult
        {
            ResultType = "complete",
            IsError = isError,
            Content = [new TextContentBlock { Text = text }],
            StructuredContent = JsonSerializer.SerializeToElement(payload),
        };
    }
}
