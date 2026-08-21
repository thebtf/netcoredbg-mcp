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
        CallToolRequestParams request,
        RequestId requestId,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (!string.Equals(request.Name, PreviewToolCatalog.FindCodeSymbol, StringComparison.Ordinal))
        {
            return Respond(
                BoundedResponseFrameSerializer.FitsUnknownToolResponseWithinLimit(requestId, request.Name, out _)
                    ? UnknownTool(request.Name)
                    : FrameBudgetExceeded(),
                cancellationToken);
        }

        if (!TryReadArguments(request.Arguments, out var name, out var kind))
        {
            return Respond(Error(SearchFailure.InvalidToolArguments(PreviewToolCatalog.FindCodeSymbol)), cancellationToken);
        }

        try
        {
            var matches = _search.FindCodeSymbol(name, kind, cancellationToken);
            return Respond(FindCodeSymbolResultOrFrameBudgetExceeded(requestId, matches), cancellationToken);
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

    internal static CallToolResult FrameBudgetExceeded() =>
        Error(SearchFailure.PreviewSearchBudgetExceeded(PreviewToolCatalog.FindCodeSymbol));

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
