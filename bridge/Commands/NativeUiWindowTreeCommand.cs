using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

internal static class NativeUiWindowTreeCommand
{
    private const int MaximumNodes = 4_096;
    private const int MaximumNodeBytes = 262_144 - 8_192;

    internal static JsonObject Observe(JsonObject request, int boundProcessId, UIA3Automation automation)
    {
        if (request.Count != 6 || request.Any(property => property.Key is not
                ("operation" or "processId" or "processIdentity" or "hwnd" or "maxDepth" or "maxChildren")) ||
            Text(request, "operation") != "get_window_tree" ||
            !Integer(request["processId"], out var pid) || pid != boundProcessId ||
            Text(request, "processIdentity") is not { Length: > 0 } identity ||
            request["hwnd"] is not JsonValue hwndValue || !hwndValue.TryGetValue<long>(out var hwnd) || hwnd == 0 ||
            !Integer(request["maxDepth"], out var maxDepth) || maxDepth is < 0 or > 32 ||
            !Integer(request["maxChildren"], out var maxChildren) || maxChildren is < 1 or > 256)
            return Refused("UI_DISCOVERY_UNREADABLE");

        try
        {
            using var process = Process.GetProcessById(pid);
            _ = process.SafeHandle;
            var handle = new IntPtr(hwnd);
            if (!NativeUiInvokeCommand.IsTargetCurrent(process, identity, handle))
                return Refused("UI_DISCOVERY_TARGET_UNAVAILABLE");
            var root = automation.FromHandle(handle);
            if (!NativeUiInvokeCommand.IsRootBound(root, pid, handle))
                return Refused("UI_DISCOVERY_TARGET_UNAVAILABLE");
            var rootId = NativeUiInvokeCommand.ReadRuntimeId(root);
            var walker = automation.TreeWalkerFactory.GetRawViewWalker();
            var nodes = new JsonArray();
            var truncated = false;
            var nodeBytes = 0;

            void Visit(AutomationElement element, int? parent, int depth)
            {
                if (!element.Properties.ProcessId.TryGetValue(out var observedPid))
                    throw new InvalidDataException("An element's process ownership is unreadable.");
                if (observedPid != pid)
                    throw new TargetChangedException();
                var nodeId = nodes.Count;
                var unavailable = new JsonArray();
                var limited = new JsonArray();
                var node = new JsonObject
                {
                    ["nodeId"] = nodeId,
                    ["parentId"] = parent,
                    ["automationId"] = ReadText(element.Properties.AutomationId, "automationId", 256, unavailable, limited),
                    ["name"] = ReadText(element.Properties.Name, "name", 1024, unavailable, limited),
                    ["controlType"] = null,
                    ["runtimeId"] = null,
                    ["supportsInvoke"] = null,
                    ["childrenComplete"] = true,
                    ["unavailableFields"] = unavailable,
                    ["truncatedFields"] = limited,
                };
                try
                {
                    if (element.Properties.ControlType.TryGetValue(out var controlType))
                        node["controlType"] = controlType.ToString();
                    else
                        unavailable.Add("controlType");
                }
                catch (Exception) { unavailable.Add("controlType"); }
                try
                {
                    if (!element.Properties.RuntimeId.TryGetValue(out var runtimeId) || runtimeId is null || runtimeId.Length == 0)
                        unavailable.Add("runtimeId");
                    else if (runtimeId.Length > 64)
                        limited.Add("runtimeId");
                    else
                        node["runtimeId"] = RuntimeId(runtimeId);
                }
                catch (Exception) { unavailable.Add("runtimeId"); }
                try { node["supportsInvoke"] = element.Patterns.Invoke.TryGetPattern(out _); }
                catch (Exception) { unavailable.Add("supportsInvoke"); }
                nodes.Add(node);

                AutomationElement? child;
                try { child = walker.GetFirstChild(element); }
                catch (Exception)
                {
                    unavailable.Add("children");
                    node["childrenComplete"] = false;
                    truncated = true;
                    child = null;
                }
                var count = 0;
                while (child is not null)
                {
                    if (depth == maxDepth || count == maxChildren || nodes.Count == MaximumNodes)
                    {
                        node["childrenComplete"] = false;
                        limited.Add("children");
                        truncated = true;
                        break;
                    }
                    Visit(child, nodeId, depth + 1);
                    count++;
                    try { child = walker.GetNextSibling(child); }
                    catch (Exception)
                    {
                        unavailable.Add("children");
                        node["childrenComplete"] = false;
                        truncated = true;
                        break;
                    }
                }
                if (unavailable.Count != 0 || limited.Count != 0)
                    truncated = true;
                nodeBytes += JsonSerializer.SerializeToUtf8Bytes(node).Length + 1;
                if (nodeBytes > MaximumNodeBytes)
                    throw new ObservationLimitException();
            }

            Visit(root, null, 0);
            var recheckedRoot = automation.FromHandle(handle);
            if (!NativeUiInvokeCommand.IsRootBound(recheckedRoot, pid, handle) ||
                !NativeUiInvokeCommand.ReadRuntimeId(recheckedRoot).SequenceEqual(rootId) ||
                !NativeUiInvokeCommand.IsTargetCurrent(process, identity, handle))
                return Refused("UI_DISCOVERY_TARGET_CHANGED");
            if (nodes[0] is not JsonObject rootNode || !JsonNode.DeepEquals(rootNode["runtimeId"], RuntimeId(rootId)))
                return Refused("UI_DISCOVERY_TARGET_CHANGED");
            return new JsonObject
            {
                ["kind"] = "ui_window_tree",
                ["scope"] = "main_window",
                ["atomicity"] = "unproven",
                ["process"] = new JsonObject { ["processId"] = pid, ["processIdentity"] = identity },
                ["window"] = new JsonObject { ["hwnd"] = hwnd.ToString(CultureInfo.InvariantCulture), ["runtimeId"] = RuntimeId(rootId) },
                ["nodes"] = nodes,
                ["limits"] = new JsonObject { ["maxDepth"] = maxDepth, ["maxChildren"] = maxChildren, ["maxNodes"] = MaximumNodes },
                ["truncated"] = truncated,
            };
        }
        catch (TargetChangedException) { return Refused("UI_DISCOVERY_TARGET_CHANGED"); }
        catch (ObservationLimitException) { return Refused("UI_DISCOVERY_LIMIT_EXCEEDED"); }
        catch (ArgumentException) { return Refused("UI_DISCOVERY_TARGET_UNAVAILABLE"); }
        catch (Exception) { return Refused("UI_DISCOVERY_UNREADABLE"); }
    }

    internal static bool FitsText(string text, int maximumScalars)
    {
        if (text.Length <= maximumScalars)
            return true;
        var count = 0;
        foreach (var _ in text.EnumerateRunes())
            if (++count > maximumScalars)
                return false;
        return true;
    }

    private static string? ReadText(IAutomationProperty<string> property, string field, int maximumScalars,
        JsonArray unavailable, JsonArray limited)
    {
        try
        {
            if (!property.TryGetValue(out var text) || text is null)
                unavailable.Add(field);
            else if (!FitsText(text, maximumScalars))
                limited.Add(field);
            else
                return text;
        }
        catch (Exception) { unavailable.Add(field); }
        return null;
    }

    private static JsonArray RuntimeId(int[] values)
    {
        var result = new JsonArray();
        foreach (var value in values)
            result.Add(value);
        return result;
    }
    private static bool Integer(JsonNode? value, out int number)
    {
        number = 0;
        return value is JsonValue field && field.TryGetValue<int>(out number);
    }
    private static string? Text(JsonObject value, string name) =>
        value[name] is JsonValue field && field.TryGetValue<string>(out var text) ? text : null;
    private static JsonObject Refused(string code) => new() { ["status"] = "refused", ["code"] = code };
    private sealed class TargetChangedException : Exception { }
    private sealed class ObservationLimitException : Exception { }
}
