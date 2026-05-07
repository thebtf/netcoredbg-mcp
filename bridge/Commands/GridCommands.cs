using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.Core.Patterns;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class GridCommands
{
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
            ["visible_rows"] = BuildRows(rows, columns)
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
            ["selected_rows"] = BuildSelectedRows(rows, columns)
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
        var visibleRows = BuildRows(GridRows(grid, automation), columns);
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
            ["selected_rows"] = BuildSelectedRows(rows, ReadColumns(@params))
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
            ["selected_rows"] = BuildSelectedRows(rows, ReadColumns(@params))
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
        return root.FindFirstDescendant(condition)
            ?? throw new InvalidOperationException("DataGrid target not found.");
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

    private static JsonArray BuildRows(AutomationElement[] gridRows, List<string> columns)
    {
        var rows = new JsonArray();
        var index = 0;
        foreach (var row in gridRows)
        {
            rows.Add(BuildRow(row, index, columns));
            index++;
        }
        return rows;
    }

    private static JsonObject BuildRow(AutomationElement row, int index, List<string> columns)
    {
        var cells = BuildCells(row, columns);
        return new JsonObject
        {
            ["index"] = index,
            ["automation_id"] = SafeString(() => row.AutomationId),
            ["name"] = SafeString(() => row.Name),
            ["control_type"] = SafeString(() => row.ControlType.ToString()),
            ["selected"] = IsSelected(row),
            ["cells"] = cells.Object,
            ["cell_values"] = cells.Array
        };
    }

    private static JsonArray BuildSelectedRows(AutomationElement[] gridRows, List<string> columns)
    {
        var rows = new JsonArray();
        for (var index = 0; index < gridRows.Length; index++)
        {
            var row = gridRows[index];
            if (!row.Patterns.SelectionItem.TryGetPattern(out var itemPattern))
                continue;
            if (!itemPattern.IsSelected.Value)
                continue;
            rows.Add(BuildRow(row, index, columns));
        }
        return rows;
    }

    private static (JsonObject Object, JsonArray Array) BuildCells(
        AutomationElement row,
        List<string> columns)
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
            var text = ReadCellText(cell);
            if (string.IsNullOrWhiteSpace(text) || IsLikelyClrTypeName(text))
                continue;
            var key = CellKey(cell, ordinal, columns);
            cellMap[key] = text;
            cellValues.Add(new JsonObject
            {
                ["column"] = key,
                ["text"] = text,
                ["automation_id"] = SafeString(() => cell.AutomationId),
                ["name"] = SafeString(() => cell.Name),
                ["control_type"] = SafeString(() => cell.ControlType.ToString())
            });
            ordinal++;
        }

        return (cellMap, cellValues);
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

    private static string CellKey(AutomationElement cell, int ordinal, List<string> columns)
    {
        if (ordinal < columns.Count)
            return columns[ordinal];
        var automationId = SafeString(() => cell.AutomationId);
        if (!string.IsNullOrWhiteSpace(automationId))
            return automationId;
        var name = SafeString(() => cell.Name);
        if (!string.IsNullOrWhiteSpace(name) && !IsLikelyClrTypeName(name))
            return name;
        return $"column_{ordinal}";
    }

    private static string ReadCellText(AutomationElement element)
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

    private static string SafeString(Func<string?> read)
    {
        try { return read() ?? ""; }
        catch { return ""; }
    }
}
