using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class GridCommands
{
    public static JsonNode VisibleRows(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Grid.TryGetPattern(out var gridPattern))
            return Unsupported("GridPattern");

        return new JsonObject
        {
            ["status"] = "PASS",
            ["row_count"] = gridPattern.RowCount.Value,
            ["visible_rows"] = BuildRows(grid)
        };
    }

    public static JsonNode SelectedRows(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Selection.TryGetPattern(out _))
            return Unsupported("SelectionPattern");

        return new JsonObject
        {
            ["status"] = "PASS",
            ["selected_rows"] = BuildSelectedRows(grid)
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
        var rows = GridRows(grid);
        if (start < 0 || end < start || end >= rows.Length)
            return new JsonObject
            {
                ["status"] = "AMBIGUOUS",
                ["reason"] = "row range is outside visible rows",
                ["selection_mutated"] = false
            };

        for (var i = start; i <= end; i++)
        {
            if (!rows[i].Patterns.SelectionItem.TryGetPattern(out var itemPattern))
                return Unsupported("SelectionItemPattern");
            if (i == start)
                itemPattern.Select();
            else
                itemPattern.AddToSelection();
        }

        return new JsonObject
        {
            ["status"] = "PASS",
            ["selected_range"] = new JsonObject { ["start"] = start, ["end"] = end },
            ["selected_rows"] = BuildSelectedRows(grid)
        };
    }

    public static JsonNode AssertRange(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Selection.TryGetPattern(out _))
            return Unsupported("SelectionPattern");

        var (start, end) = ReadRange(@params);
        var selectedRows = SelectedRowIndices(grid);
        var expected = Enumerable.Range(start, end - start + 1).ToList();
        var passed = expected.All(selectedRows.Contains);

        return new JsonObject
        {
            ["status"] = passed ? "PASS" : "FAIL",
            ["asserted"] = passed,
            ["expected_range"] = new JsonObject { ["start"] = start, ["end"] = end },
            ["selected_indices"] = ToJsonArray(selectedRows),
            ["selected_rows"] = BuildSelectedRows(grid)
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
        if (Enum.TryParse<ControlType>(controlType, true, out var ct))
            conditions.Add(cf.ByControlType(ct));

        if (conditions.Count == 0)
            throw new ArgumentException("DataGrid selector requires automationId, name, or controlType");

        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());
        return root.FindFirstDescendant(condition)
            ?? throw new InvalidOperationException("DataGrid target not found.");
    }

    private static AutomationElement[] GridRows(AutomationElement grid)
    {
        var descendants = grid.FindAllDescendants();
        var selectableRows = descendants
            .Where(IsSelectableRow)
            .ToArray();
        if (selectableRows.Length > 0)
            return selectableRows;

        var directRows = grid.FindAllChildren()
            .Where(IsRowLike)
            .ToArray();
        if (directRows.Length > 0)
            return directRows;

        return descendants
            .Where(IsRowLike)
            .ToArray();
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

    private static JsonArray BuildRows(AutomationElement grid)
    {
        var rows = new JsonArray();
        var index = 0;
        foreach (var row in GridRows(grid))
        {
            rows.Add(new JsonObject
            {
                ["index"] = index,
                ["automation_id"] = SafeString(() => row.AutomationId),
                ["name"] = SafeString(() => row.Name),
                ["control_type"] = SafeString(() => row.ControlType.ToString())
            });
            index++;
        }
        return rows;
    }

    private static JsonArray BuildSelectedRows(AutomationElement grid)
    {
        var rows = new JsonArray();
        var gridRows = GridRows(grid);
        for (var index = 0; index < gridRows.Length; index++)
        {
            var row = gridRows[index];
            if (!row.Patterns.SelectionItem.TryGetPattern(out var itemPattern))
                continue;
            if (!itemPattern.IsSelected.Value)
                continue;
            rows.Add(new JsonObject
            {
                ["index"] = index,
                ["automation_id"] = SafeString(() => row.AutomationId),
                ["name"] = SafeString(() => row.Name)
            });
        }
        return rows;
    }

    private static List<int> SelectedRowIndices(AutomationElement grid)
    {
        var selected = new List<int>();
        var rows = GridRows(grid);
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

    private static string SafeString(Func<string?> read)
    {
        try { return read() ?? ""; }
        catch { return ""; }
    }
}
