using System.Text.Json.Nodes;
using System.Threading;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.Core.Patterns;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class GridCommands
{
    private static readonly string[] CellPlaceholderSubstrings =
    {
        "display column index",
        "индекс отображения столбца"
    };

    public static JsonNode VisibleRows(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Grid.TryGetPattern(out var gridPattern))
            return Unsupported("GridPattern");

        var rows = GridRows(grid, automation);
        var columns = ReadColumns(@params);
        return new JsonObject
        {
            ["status"] = "PASS",
            ["row_count"] = gridPattern.RowCount.Value,
            ["visible_rows"] = BuildRows(grid, rows, columns)
        };
    }

    public static JsonNode SelectedRows(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Selection.TryGetPattern(out _))
            return Unsupported("SelectionPattern");

        var rows = GridRows(grid, automation);
        var columns = ReadColumns(@params);
        return new JsonObject
        {
            ["status"] = "PASS",
            ["selected_rows"] = BuildSelectedRows(grid, rows, columns)
        };
    }

    public static JsonNode Snapshot(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        return VisibleRows(@params, automation, mainWindow);
    }

    public static JsonNode AssertRows(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Grid.TryGetPattern(out _))
            return Unsupported("GridPattern");

        var expectedRows = @params?["rows"] as JsonArray
            ?? throw new ArgumentException("Missing required parameter: rows");
        var columns = ReadColumns(@params);
        if (columns.Count == 0)
            columns = ColumnsFromAssertions(expectedRows);
        var visibleRows = BuildRows(grid, GridRows(grid, automation), columns);
        var failures = new JsonArray();
        var matched = new JsonArray();

        foreach (var expectedNode in expectedRows)
        {
            var expected = expectedNode as JsonObject
                ?? throw new ArgumentException("Each row assertion must be an object");
            var rowIndex = expected["index"]?.GetValue<int>()
                ?? throw new ArgumentException("Row assertion requires index");
            var contains = expected["contains"] as JsonObject
                ?? throw new ArgumentException("Row assertion requires contains");
            var actual = RowObjectByIndex(visibleRows, rowIndex);
            if (actual is null)
            {
                failures.Add(new JsonObject
                {
                    ["index"] = rowIndex,
                    ["reason"] = "row not found"
                });
                continue;
            }

            var cells = actual["cells"] as JsonObject;
            if (cells is null || cells.Count == 0)
            {
                failures.Add(new JsonObject
                {
                    ["index"] = rowIndex,
                    ["reason"] = "row cell evidence unavailable"
                });
                continue;
            }

            var missing = new JsonObject();
            foreach (var expectedCell in contains)
            {
                var key = expectedCell.Key;
                var expectedValue = expectedCell.Value?.GetValue<string>() ?? "";
                var actualValue = cells[key]?.GetValue<string>() ?? "";
                if (!string.Equals(actualValue, expectedValue, StringComparison.Ordinal))
                    missing[key] = expectedValue;
            }

            if (missing.Count > 0)
            {
                failures.Add(new JsonObject
                {
                    ["index"] = rowIndex,
                    ["reason"] = "row cell assertion failed",
                    ["missing"] = missing,
                    ["actual_cells"] = CloneObject(cells)
                });
                continue;
            }

            matched.Add(rowIndex);
        }

        var reason = failures.Count == 0
            ? "row assertions passed"
            : failures.Any(failure =>
                failure?["reason"]?.GetValue<string>() == "row cell evidence unavailable")
                ? "row cell evidence unavailable"
                : "row cell assertion failed";

        return new JsonObject
        {
            ["status"] = failures.Count == 0 ? "PASS" : "FAIL",
            ["asserted"] = failures.Count == 0,
            ["reason"] = reason,
            ["matched_rows"] = matched,
            ["failed_rows"] = failures,
            ["visible_rows"] = visibleRows
        };
    }

    public static JsonNode SelectRange(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Grid.TryGetPattern(out _))
            return Unsupported("GridPattern");
        if (!grid.Patterns.Selection.TryGetPattern(out _))
            return Unsupported("SelectionPattern");

        var (start, end) = ReadRange(@params);
        var rows = GridRows(grid, automation);
        if (start < 0 || end < start || end >= rows.Length)
            return new JsonObject
            {
                ["status"] = "AMBIGUOUS",
                ["reason"] = "row range is outside visible rows",
                ["selection_mutated"] = false
            };

        var itemPatterns = new List<ISelectionItemPattern>();
        for (var i = start; i <= end; i++)
        {
            if (!rows[i].Patterns.SelectionItem.TryGetPattern(out var itemPattern))
                return Unsupported("SelectionItemPattern");
            itemPatterns.Add(itemPattern);
        }

        for (var index = 0; index < itemPatterns.Count; index++)
        {
            if (index == 0)
            {
                var itemPattern = itemPatterns[index];
                itemPattern.Select();
            }
            else
            {
                var itemPattern = itemPatterns[index];
                itemPattern.AddToSelection();
            }
        }

        return new JsonObject
        {
            ["status"] = "PASS",
            ["selected_range"] = new JsonObject { ["start"] = start, ["end"] = end },
            ["selected_rows"] = BuildSelectedRows(grid, rows, ReadColumns(@params))
        };
    }

    public static JsonNode AssertRange(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Selection.TryGetPattern(out _))
            return Unsupported("SelectionPattern");

        var (start, end) = ReadRange(@params);
        var rows = GridRows(grid, automation);
        if (start < 0 || end < start || end >= rows.Length)
            return new JsonObject
            {
                ["status"] = "AMBIGUOUS",
                ["reason"] = "row range is outside visible rows",
                ["selection_mutated"] = false
            };

        var selectedRows = SelectedRowIndices(rows);
        var expected = Enumerable.Range(start, end - start + 1).ToList();
        var passed = selectedRows.Count == expected.Count && !expected.Except(selectedRows).Any();

        return new JsonObject
        {
            ["status"] = passed ? "PASS" : "FAIL",
            ["asserted"] = passed,
            ["expected_range"] = new JsonObject { ["start"] = start, ["end"] = end },
            ["selected_indices"] = ToJsonArray(selectedRows),
            ["selected_rows"] = BuildSelectedRows(grid, rows, ReadColumns(@params))
        };
    }

    private static AutomationElement ResolveGrid(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var selector = @params?["selector"]
            ?? throw new ArgumentException("Missing required parameter: selector");
        var root = ElementCommands.ResolveSearchRoot(mainWindow, selector, automation);
        var cf = new ConditionFactory(automation.PropertyLibrary);
        var conditions = new List<ConditionBase>();

        var automationId = selector["automationId"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(automationId))
            conditions.Add(cf.ByAutomationId(automationId));

        var name = selector["name"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(name))
            conditions.Add(cf.ByName(name));

        var controlType = selector["controlType"]?.GetValue<string>() ?? "DataGrid";
        if (!Enum.TryParse<ControlType>(controlType, true, out var ct))
            throw new ArgumentException($"Unknown DataGrid controlType: {controlType}");
        conditions.Add(cf.ByControlType(ct));

        if (conditions.Count == 0)
            throw new ArgumentException("DataGrid selector requires automationId, name, or controlType");

        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());
        return FindGridWithRetry(root, condition, ReadGridFindTimeout(@params))
            ?? throw new InvalidOperationException("DataGrid target not found.");
    }

    private static AutomationElement? FindGridWithRetry(
        AutomationElement root,
        ConditionBase condition,
        TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow.Add(timeout);
        while (true)
        {
            var match = root.FindFirstDescendant(condition);
            if (match is not null)
                return match;

            var remaining = deadline - DateTime.UtcNow;
            if (remaining <= TimeSpan.Zero)
                break;

            Thread.Sleep(
                remaining < TimeSpan.FromMilliseconds(100)
                    ? remaining
                    : TimeSpan.FromMilliseconds(100));
        }

        return null;
    }

    private static TimeSpan ReadGridFindTimeout(JsonNode? @params)
    {
        var timeoutNode = @params?["timeout_ms"];
        if (timeoutNode is null)
            return TimeSpan.FromSeconds(5);

        var timeoutMs = Math.Clamp(timeoutNode.GetValue<int>(), 0, 30_000);
        return TimeSpan.FromMilliseconds(timeoutMs);
    }

    private static AutomationElement[] GridRows(AutomationElement grid, UIA3Automation automation)
    {
        var rowCondition = RowCondition(automation);
        var directRows = grid.FindAllChildren(rowCondition)
            .Where(IsRowLike)
            .ToArray();
        if (directRows.Length > 0)
            return directRows;

        var descendants = grid.FindAllDescendants(rowCondition)
            .Where(IsRowLike)
            .ToArray();
        var selectableRows = descendants
            .Where(IsSelectableRow)
            .ToArray();
        return selectableRows.Length > 0 ? selectableRows : descendants;
    }

    private static ConditionBase RowCondition(UIA3Automation automation)
    {
        var cf = new ConditionFactory(automation.PropertyLibrary);
        return new OrCondition(
            cf.ByControlType(ControlType.DataItem),
            cf.ByControlType(ControlType.Custom),
            cf.ByControlType(ControlType.ListItem));
    }

    private static bool IsSelectableRow(AutomationElement element)
    {
        return IsRowLike(element) && element.Patterns.SelectionItem.TryGetPattern(out _);
    }

    private static bool IsRowLike(AutomationElement element)
    {
        var controlType = element.ControlType;
        return controlType == ControlType.DataItem ||
               controlType == ControlType.Custom ||
               controlType == ControlType.ListItem;
    }

    private static JsonObject SafeRect(AutomationElement element)
    {
        try
        {
            var rect = element.BoundingRectangle;
            return new JsonObject
            {
                ["x"] = rect.X,
                ["y"] = rect.Y,
                ["width"] = rect.Width,
                ["height"] = rect.Height
            };
        }
        catch
        {
            return new JsonObject
            {
                ["x"] = 0,
                ["y"] = 0,
                ["width"] = 0,
                ["height"] = 0
            };
        }
    }

    private static JsonArray BuildRows(
        AutomationElement grid,
        AutomationElement[] gridRows,
        List<string> columns)
    {
        var headers = ColumnHeaders(grid);
        var rows = new JsonArray();
        var index = 0;
        foreach (var row in gridRows)
        {
            rows.Add(BuildRow(row, index, columns, headers));
            index++;
        }
        return rows;
    }

    private static JsonObject BuildRow(
        AutomationElement row,
        int index,
        List<string> columns,
        List<string> headers)
    {
        var cells = BuildCells(row, columns, headers);
        return new JsonObject
        {
            ["index"] = index,
            ["row_index"] = RowIndex(row, index),
            ["automation_id"] = SafeString(() => row.AutomationId),
            ["name"] = SafeString(() => row.Name),
            ["control_type"] = SafeString(() => row.ControlType.ToString()),
            ["bounds"] = SafeRect(row),
            ["selected"] = IsSelected(row),
            ["cells"] = cells.Object,
            ["cell_values"] = cells.Array
        };
    }

    private static JsonArray BuildSelectedRows(
        AutomationElement grid,
        AutomationElement[] gridRows,
        List<string> columns)
    {
        var headers = ColumnHeaders(grid);
        var rows = new JsonArray();
        for (var index = 0; index < gridRows.Length; index++)
        {
            var row = gridRows[index];
            if (!row.Patterns.SelectionItem.TryGetPattern(out var itemPattern))
                continue;
            if (!itemPattern.IsSelected.Value)
                continue;
            rows.Add(BuildRow(row, index, columns, headers));
        }
        return rows;
    }

    private static (JsonObject Object, JsonArray Array) BuildCells(
        AutomationElement row,
        List<string> columns,
        List<string> headers)
    {
        var patternCells = BuildPatternCells(row, columns, headers);
        if (HasCompleteCellCoverage(patternCells, columns, headers))
            return patternCells;

        var descendantCells = BuildDescendantCells(row, columns, headers);
        return MergeCellEvidence(patternCells, descendantCells);
    }

    private static bool HasCompleteCellCoverage(
        (JsonObject Object, JsonArray Array) cells,
        List<string> columns,
        List<string> headers)
    {
        var expectedColumns = Math.Max(columns.Count, headers.Count);
        return expectedColumns > 0 &&
               (cells.Object.Count >= expectedColumns || cells.Array.Count >= expectedColumns);
    }

    private static (JsonObject Object, JsonArray Array) BuildDescendantCells(
        AutomationElement row,
        List<string> columns,
        List<string> headers)
    {
        var cellMap = new JsonObject();
        var cellValues = new JsonArray();
        var candidates = row.FindAllChildren()
            .Where(IsCellTextCandidate)
            .ToArray();
        if (candidates.Length == 0)
        {
            candidates = row.FindAllDescendants()
                .Where(IsCellTextCandidate)
                .ToArray();
        }
        var ordinal = 0;

        foreach (var cell in candidates)
        {
            var currentOrdinal = ordinal;
            ordinal++;
            var text = ReadCellText(cell);
            if (string.IsNullOrWhiteSpace(text) ||
                IsLikelyClrTypeName(text) ||
                IsLikelyCellPlaceholder(text))
                continue;
            var key = CellKey(cell, currentOrdinal, columns, headers);
            cellMap[key] = text;
            cellValues.Add(new JsonObject
            {
                ["column"] = key,
                ["text"] = text,
                ["automation_id"] = SafeString(() => cell.AutomationId),
                ["name"] = SafeString(() => cell.Name),
                ["control_type"] = SafeString(() => cell.ControlType.ToString())
            });
        }

        return (cellMap, cellValues);
    }

    private static (JsonObject Object, JsonArray Array) MergeCellEvidence(
        (JsonObject Object, JsonArray Array) primary,
        (JsonObject Object, JsonArray Array) fallback)
    {
        if (primary.Array.Count == 0)
            return fallback;
        if (fallback.Array.Count == 0)
            return primary;

        var cellMap = new JsonObject();
        var cellValues = new JsonArray();
        var seenColumns = new HashSet<string>(StringComparer.Ordinal);

        AppendCellEvidence(primary, cellMap, cellValues, seenColumns);
        AppendCellEvidence(fallback, cellMap, cellValues, seenColumns);

        return (cellMap, cellValues);
    }

    private static void AppendCellEvidence(
        (JsonObject Object, JsonArray Array) source,
        JsonObject targetObject,
        JsonArray targetArray,
        HashSet<string> seenColumns)
    {
        foreach (var cell in source.Array)
        {
            var column = CellEvidenceColumn(cell);
            if (!seenColumns.Add(CellEvidenceDedupeKey(column)))
                continue;
            targetArray.Add(cell?.DeepClone());
        }

        foreach (var item in source.Object)
        {
            if (targetObject.ContainsKey(item.Key))
                continue;
            targetObject[item.Key] = item.Value?.DeepClone();
            seenColumns.Add(CellEvidenceDedupeKey(item.Key));
        }
    }

    private static string CellEvidenceDedupeKey(string column)
    {
        return string.IsNullOrWhiteSpace(column) ? "<empty-column>" : column;
    }

    private static string CellEvidenceColumn(JsonNode? cell)
    {
        try
        {
            return cell?["column"]?.GetValue<string>() ?? "";
        }
        catch
        {
            return "";
        }
    }

    private static (JsonObject Object, JsonArray Array) BuildPatternCells(
        AutomationElement row,
        List<string> columns,
        List<string> headers)
    {
        var cellMap = new JsonObject();
        var cellValues = new JsonArray();

        try
        {
            var gridRow = new GridRow(row.FrameworkAutomationElement);
            var cells = gridRow.Cells;
            for (var ordinal = 0; ordinal < cells.Length; ordinal++)
            {
                var cell = cells[ordinal];
                var columnIndex = CellColumnIndex(cell, ordinal);
                var text = SafeString(() => cell.Value);
                if (string.IsNullOrWhiteSpace(text) ||
                    IsLikelyClrTypeName(text) ||
                    IsLikelyCellPlaceholder(text))
                    text = ReadCellText(cell);
                if (string.IsNullOrWhiteSpace(text) ||
                    IsLikelyClrTypeName(text) ||
                    IsLikelyCellPlaceholder(text))
                    continue;

                var key = CellKey(cell, columnIndex, columns, headers);
                cellMap[key] = text;
                cellValues.Add(new JsonObject
                {
                    ["column"] = key,
                    ["text"] = text,
                    ["automation_id"] = SafeString(() => cell.AutomationId),
                    ["name"] = SafeString(() => cell.Name),
                    ["control_type"] = SafeString(() => cell.ControlType.ToString())
                });
            }
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine(
                $"GridCommands.BuildPatternCells fallback after GridRow.Cells failure: {ex}");
            // Fall back to descendant text evidence for providers that do not
            // expose GridRow.Cells consistently.
        }

        return (cellMap, cellValues);
    }

    private static List<string> ColumnHeaders(AutomationElement grid)
    {
        var headers = new List<string>();
        try
        {
            var gridElement = new Grid(grid.FrameworkAutomationElement);
            foreach (var header in gridElement.ColumnHeaders)
            {
                var text = ReadCellText(header);
                headers.Add(
                    IsLikelyClrTypeName(text) || IsLikelyCellPlaceholder(text)
                        ? ""
                        : text);
            }
        }
        catch (Exception ex)
        {
            System.Diagnostics.Debug.WriteLine(
                $"GridCommands.ColumnHeaders best-effort extraction failed: {ex}");
            // Header evidence is best-effort; row cell extraction still works
            // via explicit plan columns or fallback cell names.
        }
        return headers;
    }

    private static List<int> SelectedRowIndices(AutomationElement[] rows)
    {
        var selected = new List<int>();
        for (var index = 0; index < rows.Length; index++)
        {
            if (rows[index].Patterns.SelectionItem.TryGetPattern(out var itemPattern) &&
                itemPattern.IsSelected.Value)
                selected.Add(index);
        }
        return selected;
    }

    private static (int Start, int End) ReadRange(JsonNode? @params)
    {
        var start = @params?["start_index"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: start_index");
        var end = @params?["end_index"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: end_index");
        return (start, end);
    }

    private static List<string> ReadColumns(JsonNode? @params)
    {
        var columns = new List<string>();
        if (@params?["columns"] is not JsonArray array)
            return columns;
        foreach (var node in array)
        {
            var value = node?.GetValue<string>();
            if (!string.IsNullOrWhiteSpace(value))
                columns.Add(value);
        }
        return columns;
    }

    private static List<string> ColumnsFromAssertions(JsonArray expectedRows)
    {
        var columns = new List<string>();
        foreach (var expectedNode in expectedRows)
        {
            if (expectedNode is not JsonObject expected ||
                expected["contains"] is not JsonObject contains)
                continue;
            foreach (var item in contains)
            {
                if (!columns.Contains(item.Key))
                    columns.Add(item.Key);
            }
        }
        return columns;
    }

    private static JsonObject? RowObjectByIndex(JsonArray rows, int index)
    {
        foreach (var rowNode in rows)
        {
            if (rowNode is not JsonObject row)
                continue;
            if (row["index"]?.GetValue<int>() == index)
                return row;
        }
        return null;
    }

    private static JsonObject CloneObject(JsonObject source)
    {
        var clone = new JsonObject();
        foreach (var item in source)
            clone[item.Key] = item.Value?.DeepClone();
        return clone;
    }

    private static JsonObject Unsupported(string pattern)
    {
        return new JsonObject
        {
            ["status"] = "UNSUPPORTED",
            ["unsupported"] = true,
            ["reason"] = $"DataGrid target does not support {pattern}"
        };
    }

    private static JsonArray ToJsonArray(IEnumerable<int> values)
    {
        var array = new JsonArray();
        foreach (var value in values)
            array.Add(value);
        return array;
    }

    private static bool IsSelected(AutomationElement element)
    {
        try
        {
            return element.Patterns.SelectionItem.TryGetPattern(out var pattern) &&
                   pattern.IsSelected.Value;
        }
        catch { return false; }
    }

    private static bool IsCellTextCandidate(AutomationElement element)
    {
        var controlType = element.ControlType;
        return controlType == ControlType.Text ||
               controlType == ControlType.Edit ||
               controlType == ControlType.Document ||
               controlType == ControlType.Custom ||
               controlType == ControlType.DataItem;
    }

    private static string CellKey(
        AutomationElement cell,
        int columnIndex,
        List<string> columns,
        List<string> headers)
    {
        if (columnIndex < headers.Count)
        {
            var header = headers[columnIndex];
            if (!string.IsNullOrWhiteSpace(header) && !IsLikelyClrTypeName(header))
                return header;
        }

        if (columnIndex < columns.Count)
            return columns[columnIndex];
        var automationId = SafeString(() => cell.AutomationId);
        if (!string.IsNullOrWhiteSpace(automationId))
            return automationId;
        var name = SafeString(() => cell.Name);
        if (!string.IsNullOrWhiteSpace(name) && !IsLikelyClrTypeName(name))
            return name;
        return $"column_{columnIndex}";
    }

    private static int CellColumnIndex(AutomationElement cell, int fallback)
    {
        try
        {
            if (cell.Patterns.GridItem.TryGetPattern(out var pattern))
                return pattern.Column.Value;
        }
        catch { /* fallback */ }

        return fallback;
    }

    private static int RowIndex(AutomationElement row, int fallback)
    {
        try
        {
            if (row.Patterns.GridItem.TryGetPattern(out var pattern))
                return pattern.Row.Value;
        }
        catch { /* fallback */ }

        try
        {
            var gridRow = new GridRow(row.FrameworkAutomationElement);
            foreach (var cell in gridRow.Cells)
            {
                if (cell.Patterns.GridItem.TryGetPattern(out var pattern))
                    return pattern.Row.Value;
            }
        }
        catch { /* fallback */ }

        return fallback;
    }

    private static string ReadCellText(AutomationElement element)
    {
        var ownText = ReadOwnCellText(element);
        if (!string.IsNullOrWhiteSpace(ownText) &&
            !IsLikelyCellPlaceholder(ownText))
            return ownText;

        var descendantText = ReadDescendantCellText(element);
        if (!string.IsNullOrWhiteSpace(descendantText))
            return descendantText;

        return ownText;
    }

    private static string ReadOwnCellText(AutomationElement element)
    {
        try
        {
            if (element.Patterns.Value.TryGetPattern(out var valuePattern))
            {
                var value = valuePattern.Value.ValueOrDefault;
                if (!string.IsNullOrWhiteSpace(value))
                    return value;
            }
        }
        catch { /* fallback */ }

        try
        {
            if (element.Patterns.Text.TryGetPattern(out var textPattern))
            {
                var text = textPattern.DocumentRange.GetText(-1);
                if (!string.IsNullOrWhiteSpace(text))
                    return text.Trim();
            }
        }
        catch { /* fallback */ }

        return SafeString(() => element.Name);
    }

    private static string ReadDescendantCellText(AutomationElement element)
    {
        try
        {
            foreach (var descendant in element.FindAllDescendants()
                         .Where(IsCellTextCandidate))
            {
                var text = ReadOwnCellText(descendant);
                if (!string.IsNullOrWhiteSpace(text) &&
                    !IsLikelyClrTypeName(text) &&
                    !IsLikelyCellPlaceholder(text))
                    return text;
            }
        }
        catch { /* fallback */ }

        return "";
    }

    /// <summary>
    /// Filters CLR type-name fallbacks such as "System.String" from cell text.
    /// Domain-like strings, versioned identifiers, or short codes can match this
    /// heuristic; that tradeoff is acceptable for the DataGrid smoke fixtures.
    /// </summary>
    private static bool IsLikelyClrTypeName(string value)
    {
        return value.Contains('.') &&
               !value.Contains(' ') &&
               value.Any(char.IsUpper) &&
               value.Any(char.IsLower);
    }

    private static bool IsLikelyCellPlaceholder(string value)
    {
        return CellPlaceholderSubstrings.Any(
            marker => value.Contains(marker, StringComparison.OrdinalIgnoreCase));
    }

    private static string SafeString(Func<string?> read)
    {
        try { return read() ?? ""; }
        catch { return ""; }
    }
}
