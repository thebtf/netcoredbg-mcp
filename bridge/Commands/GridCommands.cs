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

        var rows = GridRows(grid, automation);
        return new JsonObject
        {
            ["status"] = "PASS",
            ["row_count"] = gridPattern.RowCount.Value,
            ["visible_rows"] = BuildRows(rows)
        };
    }

    public static JsonNode SelectedRows(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Selection.TryGetPattern(out _))
            return Unsupported("SelectionPattern");

        var rows = GridRows(grid, automation);
        return new JsonObject
        {
            ["status"] = "PASS",
            ["selected_rows"] = BuildSelectedRows(rows)
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
            ["selected_rows"] = BuildSelectedRows(rows)
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
            ["selected_rows"] = BuildSelectedRows(rows)
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

    private static JsonArray BuildRows(AutomationElement[] gridRows)
    {
        var rows = new JsonArray();
        var index = 0;
        foreach (var row in gridRows)
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

    private static JsonArray BuildSelectedRows(AutomationElement[] gridRows)
    {
        var rows = new JsonArray();
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
