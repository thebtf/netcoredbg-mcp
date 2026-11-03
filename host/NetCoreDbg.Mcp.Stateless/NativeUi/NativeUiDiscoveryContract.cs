using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.Stateless.NativeScene;

namespace NetCoreDbg.Mcp.Stateless.NativeUi;

internal static class NativeUiDiscoveryContract
{
    internal const int MaximumRequestBytes = 8_192;
    internal const int MaximumHelperBytes = 262_144;
    internal const int MaximumResultBytes = 1_048_576;
    internal const int MaximumNodes = 4_096;
    private static readonly string[] NodeFields = ["automationId", "name", "controlType", "runtimeId", "supportsInvoke"];

    internal static bool TryReadExpectation(JsonElement value, out JsonObject? expectation)
    {
        expectation = null;
        if (value.ValueKind != JsonValueKind.Object || value.EnumerateObject().Count() != 4 ||
            value.EnumerateObject().Any(property => property.Name is not ("processIdentity" or "hwnd" or "rootRuntimeId" or "elementRuntimeId")) ||
            !TryReadNonblank(value, "processIdentity") || !TryReadNonblank(value, "hwnd") ||
            !value.TryGetProperty("rootRuntimeId", out var root) || !IsRuntimeId(root) ||
            !value.TryGetProperty("elementRuntimeId", out var element) || !IsRuntimeId(element))
            return false;
        expectation = JsonNode.Parse(value.GetRawText())!.AsObject();
        return true;
    }

    internal static bool IsObservation(JsonObject result, NativeSceneTargetIdentity target, long hwnd, int maxDepth, int maxChildren)
    {
        if (result.Count != 8 || Text(result, "kind") != "ui_window_tree" || Text(result, "scope") != "main_window" ||
            Text(result, "atomicity") != "unproven" ||
            result["process"] is not JsonObject process || process.Count != 2 ||
            !Integer(process["processId"], out var pid) || pid != target.ProcessId ||
            Text(process, "processIdentity") != target.ProcessIdentity ||
            result["window"] is not JsonObject window || window.Count != 2 ||
            Text(window, "hwnd") != hwnd.ToString(CultureInfo.InvariantCulture) || !IsRuntimeId(window["runtimeId"]) ||
            result["limits"] is not JsonObject limits || limits.Count != 3 ||
            !Integer(limits["maxDepth"], out var depth) || depth != maxDepth ||
            !Integer(limits["maxChildren"], out var children) || children != maxChildren ||
            !Integer(limits["maxNodes"], out var maximum) || maximum != MaximumNodes ||
            !Boolean(result["truncated"], out var truncated) ||
            result["nodes"] is not JsonArray { Count: > 0 and <= MaximumNodes } nodes)
            return false;

        var depths = new int[nodes.Count];
        var childCounts = new int[nodes.Count];
        var ancestors = new List<int>(maxDepth + 1);
        for (var index = 0; index < nodes.Count; index++)
        {
            if (nodes[index] is not JsonObject node || node.Count != 10 ||
                !node.ContainsKey("parentId") || !Integer(node["nodeId"], out var nodeId) || nodeId != index ||
                !Boolean(node["childrenComplete"], out var complete) || (!complete && !truncated) ||
                !Fields(node["unavailableFields"], out var unavailable) || !Fields(node["truncatedFields"], out var limited))
                return false;
            if (index == 0)
            {
                if (node["parentId"] is not null || !JsonNode.DeepEquals(node["runtimeId"], window["runtimeId"]))
                    return false;
            }
            else
            {
                if (!Integer(node["parentId"], out var parent) || parent < 0 || parent >= index ||
                    !ancestors.Contains(parent) || ++childCounts[parent] > maxChildren)
                    return false;
                ancestors.RemoveRange(ancestors.IndexOf(parent) + 1, ancestors.Count - ancestors.IndexOf(parent) - 1);
                depths[index] = depths[parent] + 1;
                if (depths[index] > maxDepth)
                    return false;
            }
            ancestors.Add(index);
            for (var fieldIndex = 0; fieldIndex < NodeFields.Length; fieldIndex++)
            {
                var field = NodeFields[fieldIndex];
                if (!node.ContainsKey(field))
                    return false;
                var value = node[field];
                if (value is null)
                {
                    if (((unavailable | limited) & (1 << fieldIndex)) == 0)
                        return false;
                }
                else if (field == "runtimeId" ? !IsRuntimeId(value) : field == "supportsInvoke" ? !Boolean(value, out _) :
                    value is not JsonValue textValue || !textValue.TryGetValue<string>(out var text) ||
                    text.EnumerateRunes().Count() > (field == "name" ? 1024 : 256))
                    return false;
            }
        }
        return true;
    }

    internal static string? Text(JsonObject value, string name) =>
        value[name] is JsonValue field && field.TryGetValue<string>(out var text) ? text : null;

    private static bool TryReadNonblank(JsonElement value, string name) =>
        value.TryGetProperty(name, out var field) && field.ValueKind == JsonValueKind.String &&
        field.GetString() is { Length: <= MaximumRequestBytes } text && !string.IsNullOrWhiteSpace(text);

    private static bool IsRuntimeId(JsonElement value) => value.ValueKind == JsonValueKind.Array &&
        value.GetArrayLength() is >= 1 and <= 64 && value.EnumerateArray().All(item => item.ValueKind == JsonValueKind.Number && item.TryGetInt32(out _));

    private static bool IsRuntimeId(JsonNode? value) => value is JsonArray { Count: >= 1 and <= 64 } items && items.All(item => Integer(item, out _));
    private static bool Integer(JsonNode? value, out int number)
    {
        number = 0;
        return value is JsonValue field && field.TryGetValue<int>(out number);
    }
    private static bool Boolean(JsonNode? value, out bool boolean)
    {
        boolean = false;
        return value is JsonValue field && field.TryGetValue<bool>(out boolean);
    }
    private static bool Fields(JsonNode? value, out int fields)
    {
        fields = 0;
        if (value is not JsonArray items)
            return false;
        foreach (var item in items)
        {
            if (item is not JsonValue field || !field.TryGetValue<string>(out var text))
                return false;
            var bit = text switch
            {
                "automationId" => 1,
                "name" => 2,
                "controlType" => 4,
                "runtimeId" => 8,
                "supportsInvoke" => 16,
                "children" => 32,
                _ => 0,
            };
            if (bit == 0 || (fields & bit) != 0)
                return false;
            fields |= bit;
        }
        return true;
    }
}

internal static class NativeUiDiscoveryResult
{
    internal static JsonObject Error(string code) => new()
    {
        ["kind"] = "tool_error",
        ["tool"] = "ui_get_window_tree",
        ["code"] = code,
        ["message"] = code switch
        {
            "DEBUG_SESSION_NOT_FOUND" => "The debug session is unavailable or retired.",
            "UI_DISCOVERY_TARGET_UNAVAILABLE" => "The bound debuggee process or main window cannot be established.",
            "UI_DISCOVERY_TARGET_CHANGED" => "The bound process, window, or root identity changed during observation.",
            "UI_DISCOVERY_BUSY" => "Another native UI operation owns this session's helper.",
            "UI_DISCOVERY_TIMEOUT" => "The single ten-second observation deadline expired; no complete tree is available.",
            "UI_DISCOVERY_CANCELLED" => "The observation was cancelled; no complete tree is available.",
            "UI_DISCOVERY_UNREADABLE" => "A required ownership check or observation could not be read.",
            "UI_DISCOVERY_LIMIT_EXCEEDED" => "The observation exceeded a hard byte or node limit; no tree is published.",
            "UI_DISCOVERY_CLEANUP_UNCONFIRMED" => "Owned helper cleanup was not confirmed; new UI helpers remain blocked until cleanup is resolved.",
            _ => "The local discovery helper is unavailable.",
        },
    };
}
