namespace NetCoreDbg.Mcp.CodeSearch.Core;

/// <summary>Closed, redacted failure payload for strict policy callers.</summary>
public sealed record SearchFailure(string Kind, string Error, string Tool)
{
    public static SearchFailure InvalidToolArguments(string tool) =>
        new("invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", tool);

    public static SearchFailure PreviewPathRefused(string tool) =>
        new("preview_path_refused", "PREVIEW_PATH_REFUSED", tool);

    public static SearchFailure PreviewSearchUnreadable(string tool) =>
        new("preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", tool);

    public static SearchFailure PreviewSearchBudgetExceeded(string tool) =>
        new("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", tool);
}

/// <summary>Raises a typed strict-policy refusal without leaking filesystem detail.</summary>
public sealed class SearchFailureException : Exception
{
    public SearchFailureException(SearchFailure failure)
        : base(failure.Error)
    {
        Failure = failure;
    }

    public SearchFailure Failure { get; }
}
