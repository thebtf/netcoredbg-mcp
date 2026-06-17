using System.Text.Json.Nodes;
using System.Threading;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Definitions;
using FlaUI.Core.Patterns;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static partial class GridCommands
{
    private const int DefaultMaxEnsureVisibleScrolls = 40;
    private const int DefaultEnsureVisibleSettleMs = 80;
    private const double ScrollPercentEpsilon = 0.01;

    public static JsonNode EnsureVisible(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Grid.TryGetPattern(out _))
            return Unsupported("GridPattern");

        var rowKey = StringValue(@params?["row_key"]);
        var rowIndex = ReadOptionalInt(@params, "row_index");
        if (rowIndex is null && string.IsNullOrWhiteSpace(rowKey))
        {
            return Blocked(
                "grid ensure-visible request missing",
                new JsonObject { ["row_index"] = null, ["row_key"] = rowKey },
                new JsonObject { ["row"] = "row_index or row_key" },
                "Provide row_key for a unique DataGrid row identity.");
        }

        var columns = ReadColumns(@params);
        var headers = ColumnHeaders(grid);
        var maxScrolls = ReadBoundedInt(
            @params,
            "max_scrolls",
            DefaultMaxEnsureVisibleScrolls,
            min: 0,
            max: 250);
        var settleMs = ReadBoundedInt(
            @params,
            "scroll_settle_ms",
            DefaultEnsureVisibleSettleMs,
            min: 0,
            max: 2_000);
        var search = SearchCurrentRows(grid, automation, columns, headers, rowIndex, rowKey, "current");
        if (search.Blocked is not null)
            return search.Blocked;
        if (search.Match is not null)
            return CompleteEnsureVisible(grid, search.Match, alreadyVisible: true, search.Attempts);

        var scan = ScanForRowWithBoundedScroll(
            grid,
            automation,
            columns,
            headers,
            rowIndex,
            rowKey,
            maxScrolls,
            settleMs);
        if (scan.Blocked is not null)
            return scan.Blocked;
        if (scan.Match is not null)
            return CompleteEnsureVisible(grid, scan.Match, alreadyVisible: false, scan.Attempts);

        return new JsonObject
        {
            ["status"] = "BLOCKED",
            ["reason"] = "grid row identity is not present after bounded scroll scan",
            ["requested"] = RequestedRow(rowIndex, rowKey),
            ["accepted"] = new JsonObject
            {
                ["row"] = "unique DataGrid row identity discoverable through bounded scroll scan",
                ["max_scrolls"] = maxScrolls
            },
            ["attempts"] = scan.Attempts,
            ["next_step"] = "Use a DataGrid that exposes the requested row through UIA row evidence, increase max_scrolls, or provide a visible setup step."
        };
    }

    private sealed record RowMatch(AutomationElement Element, JsonObject Row);

    private sealed record RowSearchResult(
        RowMatch? Match,
        JsonObject? Blocked,
        JsonArray Attempts);

    private static RowSearchResult SearchCurrentRows(
        AutomationElement grid,
        UIA3Automation automation,
        List<string> columns,
        List<string> headers,
        int? rowIndex,
        string? rowKey,
        string phase,
        int scrollStep = 0)
    {
        var attempts = new JsonArray();
        var rows = GridRows(grid, automation);
        var matches = MatchingRows(rows, columns, headers, rowIndex, rowKey).ToList();
        attempts.Add(ScanAttempt(grid, rows, columns, headers, phase, scrollStep, matches));
        if (matches.Count == 1)
            return new RowSearchResult(matches[0], null, attempts);
        if (matches.Count > 1)
        {
            return new RowSearchResult(
                null,
                new JsonObject
                {
                    ["status"] = "AMBIGUOUS",
                    ["reason"] = "grid row identity is ambiguous",
                    ["requested"] = RequestedRow(rowIndex, rowKey),
                    ["matches"] = CompactRows(matches),
                    ["attempts"] = attempts,
                    ["next_step"] = "Disambiguate the DataGrid row identity before ensure_visible."
                },
                attempts);
        }
        return new RowSearchResult(null, null, attempts);
    }

    private static RowSearchResult ScanForRowWithBoundedScroll(
        AutomationElement grid,
        UIA3Automation automation,
        List<string> columns,
        List<string> headers,
        int? rowIndex,
        string? rowKey,
        int maxScrolls,
        int settleMs)
    {
        var attempts = new JsonArray();
        if (!grid.Patterns.Scroll.TryGetPattern(out var scrollPattern))
        {
            return new RowSearchResult(
                null,
                Blocked(
                    "grid bounded ensure-visible scan requires ScrollPattern",
                    RequestedRow(rowIndex, rowKey),
                    new JsonObject { ["pattern"] = "ScrollPattern" },
                    "Use a scrollable DataGrid provider or keep the requested row visible before acting."),
                attempts);
        }

        var verticallyScrollable = SafeVerticallyScrollable(scrollPattern);
        if (verticallyScrollable is null)
        {
            return new RowSearchResult(
                null,
                Blocked(
                    "grid vertical scrollability evidence unavailable",
                    RequestedRow(rowIndex, rowKey),
                    new JsonObject { ["vertically_scrollable"] = true },
                    "Use a DataGrid provider that exposes ScrollPattern.VerticalScrollability or keep the requested row visible before acting."),
                attempts);
        }

        if (verticallyScrollable is false)
        {
            return new RowSearchResult(
                null,
                Blocked(
                    "grid is not vertically scrollable",
                    RequestedRow(rowIndex, rowKey),
                    new JsonObject { ["vertically_scrollable"] = true },
                    "Use a scrollable DataGrid or choose a currently visible row."),
                attempts);
        }

        var currentScan = ScanDownward(
            grid,
            automation,
            scrollPattern,
            columns,
            headers,
            rowIndex,
            rowKey,
            attempts,
            "current_downward",
            maxScrolls,
            settleMs,
            out var currentDownwardScrolls);
        if (currentScan.Blocked is not null || currentScan.Match is not null)
            return currentScan;

        var rewound = TryScrollToVerticalStart(scrollPattern, settleMs);
        attempts.Add(new JsonObject
        {
            ["phase"] = "rewind_to_start",
            ["succeeded"] = rewound,
            ["vertical_scroll_percent"] = ScrollPercent(grid)
        });

        for (var rewindStep = 0; !rewound && rewindStep < maxScrolls; rewindStep++)
        {
            if (!ScrollOneViewport(scrollPattern, ScrollAmount.LargeDecrement, settleMs))
                break;
            var percent = SafeVerticalScrollPercent(scrollPattern);
            rewound = percent is not null && percent.Value <= ScrollPercentEpsilon;
        }

        if (!rewound)
        {
            return new RowSearchResult(
                null,
                Blocked(
                    "grid rewind-to-start failed before bounded scroll scan",
                    RequestedRow(rowIndex, rowKey),
                    new JsonObject { ["vertical_scroll_percent"] = ScrollPercent(grid) },
                    "Use a DataGrid provider that supports ScrollPattern.SetScrollPercent, increase setup reliability, or choose a currently visible row."),
                attempts);
        }

        return ScanDownward(
            grid,
            automation,
            scrollPattern,
            columns,
            headers,
            rowIndex,
            rowKey,
            attempts,
            "rewound_downward",
            maxScrolls,
            settleMs,
            out _);
    }

    private static RowSearchResult ScanDownward(
        AutomationElement grid,
        UIA3Automation automation,
        IScrollPattern scrollPattern,
        List<string> columns,
        List<string> headers,
        int? rowIndex,
        string? rowKey,
        JsonArray attempts,
        string phase,
        int maxScrolls,
        int settleMs,
        out int scrollsMoved)
    {
        scrollsMoved = 0;
        for (var step = 0; step <= maxScrolls; step++)
        {
            var result = SearchCurrentRows(
                grid,
                automation,
                columns,
                headers,
                rowIndex,
                rowKey,
                phase,
                step);
            AppendAttempts(attempts, result.Attempts);
            if (result.Blocked is not null)
                return new RowSearchResult(null, result.Blocked, attempts);
            if (result.Match is not null)
                return new RowSearchResult(result.Match, null, attempts);
            if (step >= maxScrolls)
                break;
            if (!ScrollOneViewport(scrollPattern, ScrollAmount.LargeIncrement, settleMs))
                break;
            scrollsMoved++;
        }

        return new RowSearchResult(null, null, attempts);
    }

    private static bool ScrollOneViewport(
        IScrollPattern scrollPattern,
        ScrollAmount verticalAmount,
        int settleMs)
    {
        var before = SafeVerticalScrollPercent(scrollPattern);
        try
        {
            scrollPattern.Scroll(ScrollAmount.NoAmount, verticalAmount);
        }
        catch
        {
            return false;
        }
        if (settleMs > 0)
            Thread.Sleep(settleMs);
        var after = SafeVerticalScrollPercent(scrollPattern);
        if (before is null || after is null)
            return false;
        return Math.Abs(after.Value - before.Value) > ScrollPercentEpsilon;
    }

    private static bool TryScrollToVerticalStart(IScrollPattern scrollPattern, int settleMs)
    {
        try
        {
            scrollPattern.SetScrollPercent(ScrollPatternConstants.NoScroll, 0);
        }
        catch
        {
            return false;
        }
        if (settleMs > 0)
            Thread.Sleep(settleMs);
        var after = SafeVerticalScrollPercent(scrollPattern);
        if (after is null)
            return false;
        return after.Value <= ScrollPercentEpsilon;
    }

    private static double? SafeVerticalScrollPercent(IScrollPattern scrollPattern)
    {
        try
        {
            return scrollPattern.VerticalScrollPercent.Value;
        }
        catch
        {
            return null;
        }
    }

    private static bool? SafeVerticallyScrollable(IScrollPattern scrollPattern)
    {
        try
        {
            return scrollPattern.VerticallyScrollable.Value;
        }
        catch
        {
            return null;
        }
    }

    private static JsonObject ScanAttempt(
        AutomationElement grid,
        AutomationElement[] rows,
        List<string> columns,
        List<string> headers,
        string phase,
        int scrollStep,
        List<RowMatch> matches)
    {
        return new JsonObject
        {
            ["phase"] = phase,
            ["scroll_step"] = scrollStep,
            ["visible_count"] = rows.Length,
            ["first_row"] = rows.Length > 0
                ? CompactRow(BuildRow(rows[0], 0, columns, headers))
                : null,
            ["last_row"] = rows.Length > 0
                ? CompactRow(BuildRow(rows[^1], rows.Length - 1, columns, headers))
                : null,
            ["matches"] = CompactRows(matches),
            ["vertical_scroll_percent"] = ScrollPercent(grid)
        };
    }

    private static double? ScrollPercent(AutomationElement grid)
    {
        try
        {
            if (grid.Patterns.Scroll.TryGetPattern(out var scrollPattern))
                return SafeVerticalScrollPercent(scrollPattern);
        }
        catch { /* best-effort evidence */ }
        return null;
    }

    private static void AppendAttempts(JsonArray target, JsonArray source)
    {
        foreach (var attempt in source)
            target.Add(attempt?.DeepClone());
    }

    private static JsonObject CompleteEnsureVisible(
        AutomationElement grid,
        RowMatch match,
        bool alreadyVisible,
        JsonArray attempts)
    {
        var usedScrollItem = false;
        if (match.Element.Patterns.ScrollItem.TryGetPattern(out var scrollItemPattern))
        {
            try
            {
                scrollItemPattern.ScrollIntoView();
            }
            catch
            {
                return Blocked(
                    "grid row ScrollItemPattern failed",
                    RequestedRow(
                        IntValue(match.Row["row_index"]) ?? IntValue(match.Row["index"]),
                        RowIdentity(match.Row)),
                    new JsonObject { ["pattern"] = "working ScrollItemPattern" },
                    "Use a DataGrid row whose ScrollItemPattern can scroll into view or rely on bounded visible row evidence.");
            }
            usedScrollItem = true;
        }
        else if (!RowIntersectsGridBounds(grid, match.Element))
        {
            return Blocked(
                "grid row does not support ScrollItemPattern and is not visibly bounded",
                RequestedRow(
                    IntValue(match.Row["row_index"]) ?? IntValue(match.Row["index"]),
                    RowIdentity(match.Row)),
                new JsonObject
                {
                    ["pattern"] = "ScrollItemPattern",
                    ["bounds"] = "row bounds intersect DataGrid bounds"
                },
                "Use a DataGrid row that supports UIA ScrollItemPattern or a provider that exposes visible row bounds.");
        }

        return new JsonObject
        {
            ["status"] = "PASS",
            ["already_visible"] = alreadyVisible,
            ["realized"] = true,
            ["scroll_item_used"] = usedScrollItem,
            ["resolved_row"] = CompactRow(match.Row),
            ["row"] = match.Row.DeepClone(),
            ["attempts"] = attempts
        };
    }

    private static bool RowIntersectsGridBounds(AutomationElement grid, AutomationElement row)
    {
        try
        {
            var gridRect = grid.BoundingRectangle;
            var rowRect = row.BoundingRectangle;
            if (gridRect.Width <= 0 || gridRect.Height <= 0 ||
                rowRect.Width <= 0 || rowRect.Height <= 0)
                return false;
            return rowRect.X < gridRect.X + gridRect.Width &&
                   rowRect.X + rowRect.Width > gridRect.X &&
                   rowRect.Y < gridRect.Y + gridRect.Height &&
                   rowRect.Y + rowRect.Height > gridRect.Y;
        }
        catch
        {
            return false;
        }
    }

    private static IEnumerable<RowMatch> MatchingRows(
        AutomationElement[] rows,
        List<string> columns,
        List<string> headers,
        int? rowIndex,
        string? rowKey)
    {
        for (var index = 0; index < rows.Length; index++)
        {
            var row = rows[index];
            var rowObject = BuildRow(row, index, columns, headers);
            if (rowIndex is not null && RowIndex(row, index) == rowIndex.Value)
            {
                yield return new RowMatch(row, rowObject);
                continue;
            }
            if (!string.IsNullOrWhiteSpace(rowKey) && RowMatchesKey(rowObject, rowKey))
                yield return new RowMatch(row, rowObject);
        }
    }

    private static bool RowMatchesKey(JsonObject row, string rowKey)
    {
        foreach (var candidate in RowIdentityCandidates(row))
        {
            if (string.Equals(candidate, rowKey, StringComparison.Ordinal))
                return true;
        }
        return false;
    }

    private static IEnumerable<string> RowIdentityCandidates(JsonObject row)
    {
        foreach (var key in new[] { "automation_id", "name" })
        {
            var value = StringValue(row[key]);
            if (!string.IsNullOrWhiteSpace(value))
                yield return value;
        }

        var cells = row["cells"] as JsonObject;
        if (cells is not null)
        {
            foreach (var cell in cells)
            {
                var value = StringValue(cell.Value);
                if (!string.IsNullOrWhiteSpace(value))
                    yield return value;
            }
        }

        var cellValues = row["cell_values"] as JsonArray;
        if (cellValues is not null)
        {
            foreach (var item in cellValues)
            {
                if (item is not JsonObject cell)
                    continue;
                var text = StringValue(cell["text"]);
                if (!string.IsNullOrWhiteSpace(text))
                    yield return text;
                var value = StringValue(cell["value"]);
                if (!string.IsNullOrWhiteSpace(value))
                    yield return value;
            }
        }
    }

    private static JsonObject RequestedRow(int? rowIndex, string? rowKey)
    {
        return new JsonObject
        {
            ["row_index"] = rowIndex,
            ["row_key"] = rowKey
        };
    }

    private static JsonArray CompactRows(IEnumerable<RowMatch> matches)
    {
        var rows = new JsonArray();
        foreach (var match in matches)
            rows.Add(CompactRow(match.Row));
        return rows;
    }

    private static JsonObject CompactRow(JsonObject row)
    {
        var result = new JsonObject
        {
            ["index"] = row["index"]?.DeepClone(),
            ["identity"] = RowIdentity(row)
        };
        if (row["row_index"] is not null)
            result["row_index"] = row["row_index"]?.DeepClone();
        return result;
    }

    private static string RowIdentity(JsonObject row)
    {
        var identity = RowIdentityCandidates(row).FirstOrDefault();
        if (!string.IsNullOrWhiteSpace(identity))
            return identity;
        var rowIndex = IntValue(row["row_index"]) ?? IntValue(row["index"]);
        return $"row:{rowIndex}";
    }

    private static int? IntValue(JsonNode? node)
    {
        if (node is null)
            return null;
        try
        {
            return node.GetValue<int>();
        }
        catch
        {
            return null;
        }
    }

    private static string? StringValue(JsonNode? node)
    {
        if (node is null)
            return null;
        try
        {
            return node.GetValue<string>();
        }
        catch
        {
            return null;
        }
    }
}
