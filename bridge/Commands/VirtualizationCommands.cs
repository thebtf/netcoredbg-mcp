using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Identifiers;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class VirtualizationCommands
{
    public static JsonNode RealizeVirtualizedItem(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var containerAutomationId = @params?["container_automation_id"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: container_automation_id");
        var propertyName = @params?["property"]?.GetValue<string>() ?? "AutomationId";
        var value = @params?["value"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: value");

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var container = mainWindow.FindFirstDescendant(cf.ByAutomationId(containerAutomationId))
            ?? throw new InvalidOperationException($"Container not found: {containerAutomationId}");

        // Check ItemContainerPattern support
        if (!container.Patterns.ItemContainer.TryGetPattern(out var itemContainerPattern))
        {
            Program.Log($"realize_virtualized_item: container '{containerAutomationId}' does not support ItemContainerPattern");
            return new JsonObject
            {
                ["realized"] = false,
                ["reason"] = "container does not support ItemContainerPattern"
            };
        }

        // Map property name to PropertyId via the automation's property library
        PropertyId? propertyId = ResolvePropertyId(automation, propertyName);
        if (propertyId is null)
        {
            throw new ArgumentException(
                $"Unknown property '{propertyName}'. Supported values: AutomationId, Name, ClassName");
        }

        // FindItemByProperty: start from null to scan from the beginning
        AutomationElement? foundItem;
        try
        {
            foundItem = itemContainerPattern.FindItemByProperty(null, propertyId, value);
        }
        catch (Exception ex)
        {
            Program.Log($"realize_virtualized_item: FindItemByProperty failed: {ex.Message}");
            return new JsonObject
            {
                ["realized"] = false,
                ["reason"] = $"item not found (search error: {ex.Message})"
            };
        }

        if (foundItem is null)
        {
            Program.Log($"realize_virtualized_item: item not found (property={propertyName}, value={value})");
            return new JsonObject
            {
                ["realized"] = false,
                ["reason"] = "item not found"
            };
        }

        // Get element ID before realize (may fail for virtualized items)
        string elementId;
        try { elementId = foundItem.AutomationId ?? value; }
        catch { elementId = value; }

        // Try VirtualizedItemPattern.Realize
        if (foundItem.Patterns.VirtualizedItem.TryGetPattern(out var virtualizedPattern))
        {
            virtualizedPattern.Realize();
            Program.Log($"realize_virtualized_item: realized '{elementId}'");
        }
        else
        {
            // Item exists in the UI tree but was not virtualized — still a success
            Program.Log($"realize_virtualized_item: item '{elementId}' was already in visual tree (not virtualized)");
        }

        // After realization, re-read element id and bounding rect.
        // BoundingRectangle can throw ElementNotAvailableException for items
        // that are realized but temporarily outside the viewport — return null
        // in that case rather than letting the exception escape.
        string finalElementId;
        try { finalElementId = foundItem.AutomationId ?? value; }
        catch { finalElementId = value; }

        JsonObject? boundingRectNode = null;
        string? boundingRectWarning = null;
        try
        {
            var rect = foundItem.BoundingRectangle;
            boundingRectNode = new JsonObject
            {
                ["x"] = (int)rect.X,
                ["y"] = (int)rect.Y,
                ["width"] = (int)rect.Width,
                ["height"] = (int)rect.Height
            };
        }
        catch (Exception ex)
        {
            Program.Log($"realize_virtualized_item: BoundingRectangle unavailable for '{finalElementId}': {ex.Message}");
            boundingRectWarning = "bounding rect unavailable";
        }

        var resultNode = new JsonObject
        {
            ["realized"] = true,
            ["element_id"] = finalElementId,
            ["bounding_rect"] = boundingRectNode
        };

        if (boundingRectWarning is not null)
            resultNode["warning"] = boundingRectWarning;

        return resultNode;
    }

    private static PropertyId? ResolvePropertyId(UIA3Automation automation, string propertyName)
    {
        // AutomationProperty<T> exposes its underlying PropertyId via implicit conversion
        // and via the Id property. We use the property library for correct UIA3 registrations.
        return propertyName.ToLowerInvariant() switch
        {
            "automationid" => automation.PropertyLibrary.Element.AutomationId,
            "name"         => automation.PropertyLibrary.Element.Name,
            "classname"    => automation.PropertyLibrary.Element.ClassName,
            _              => null
        };
    }
}
