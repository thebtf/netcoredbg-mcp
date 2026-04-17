using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Input;
using FlaUI.Core.WindowsAPI;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ModifierCommands
{
    private static readonly IReadOnlyDictionary<string, VirtualKeyShort> ModifierMap =
        new Dictionary<string, VirtualKeyShort>(StringComparer.OrdinalIgnoreCase)
        {
            ["ctrl"] = VirtualKeyShort.CONTROL,
            ["shift"] = VirtualKeyShort.SHIFT,
            ["alt"] = VirtualKeyShort.ALT,
            ["win"] = VirtualKeyShort.LWIN
        };

    private static readonly IReadOnlyDictionary<VirtualKeyShort, string> ModifierNames =
        ModifierMap.ToDictionary(static pair => pair.Value, static pair => pair.Key);

    public static JsonNode HoldModifiers(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var modifierKeys = ParseModifierArray(@params?["modifiers"]);

        lock (JsonRpcHandler.HeldModifiersLock)
        {
            foreach (var modifierKey in modifierKeys)
            {
                if (JsonRpcHandler.HeldModifiers.Contains(modifierKey))
                {
                    continue;
                }

                Keyboard.Press(modifierKey);
                JsonRpcHandler.HeldModifiers.Add(modifierKey);
            }
        }

        return new JsonObject
        {
            ["held"] = true,
            ["modifiers"] = ToJsonArray(GetHeldModifierNames())
        };
    }

    public static JsonNode ReleaseModifiers(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var modifiersNode = @params?["modifiers"]
            ?? throw new ArgumentException("Missing required parameter: modifiers");

        if (modifiersNode is JsonValue valueNode &&
            string.Equals(valueNode.GetValue<string>(), "all", StringComparison.OrdinalIgnoreCase))
        {
            ReleaseAllHeldModifiers();
            return new JsonObject
            {
                ["released"] = true,
                ["modifiers"] = new JsonArray()
            };
        }

        var modifierKeys = ParseModifierArray(modifiersNode);

        lock (JsonRpcHandler.HeldModifiersLock)
        {
            for (var index = modifierKeys.Count - 1; index >= 0; index--)
            {
                var modifierKey = modifierKeys[index];
                if (!JsonRpcHandler.HeldModifiers.Remove(modifierKey))
                {
                    continue;
                }

                Keyboard.Release(modifierKey);
            }
        }

        return new JsonObject
        {
            ["released"] = true,
            ["modifiers"] = ToJsonArray(GetHeldModifierNames())
        };
    }

    public static JsonNode GetHeldModifiers(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        return new JsonObject
        {
            ["modifiers"] = ToJsonArray(GetHeldModifierNames())
        };
    }

    public static void ReleaseAllHeldModifiers()
    {
        List<VirtualKeyShort> heldSnapshot;

        lock (JsonRpcHandler.HeldModifiersLock)
        {
            heldSnapshot = JsonRpcHandler.HeldModifiers.ToList();
            JsonRpcHandler.HeldModifiers.Clear();
        }

        for (var index = heldSnapshot.Count - 1; index >= 0; index--)
        {
            try
            {
                Keyboard.Release(heldSnapshot[index]);
            }
            catch (Exception ex)
            {
                Program.Log($"Failed to release modifier {heldSnapshot[index]} during shutdown: {ex.Message}");
            }
        }
    }

    public static List<VirtualKeyShort> GetTemporaryModifierKeys(JsonNode? modifiersNode)
    {
        if (modifiersNode is null)
        {
            return [];
        }

        var requestedModifiers = ParseModifierArray(modifiersNode);
        var heldSnapshot = GetHeldModifierKeys();

        return requestedModifiers
            .Where(modifierKey => !heldSnapshot.Contains(modifierKey))
            .ToList();
    }

    private static HashSet<VirtualKeyShort> GetHeldModifierKeys()
    {
        lock (JsonRpcHandler.HeldModifiersLock)
        {
            return new HashSet<VirtualKeyShort>(JsonRpcHandler.HeldModifiers);
        }
    }

    private static List<string> GetHeldModifierNames()
    {
        lock (JsonRpcHandler.HeldModifiersLock)
        {
            return JsonRpcHandler.HeldModifiers
                .Select(GetModifierName)
                .OrderBy(static name => name, StringComparer.Ordinal)
                .ToList();
        }
    }

    private static string GetModifierName(VirtualKeyShort modifierKey)
    {
        if (ModifierNames.TryGetValue(modifierKey, out var name))
        {
            return name;
        }

        throw new ArgumentException($"Unsupported modifier key: {modifierKey}");
    }

    private static List<VirtualKeyShort> ParseModifierArray(JsonNode? modifiersNode)
    {
        var modifiersArray = modifiersNode?.AsArray()
            ?? throw new ArgumentException(
                "Missing required parameter: modifiers (array of strings or \"all\")");

        var modifierNames = new List<string>();
        foreach (var modifierNode in modifiersArray)
        {
            var modifierName = modifierNode?.GetValue<string>()?.Trim().ToLowerInvariant();
            if (string.IsNullOrWhiteSpace(modifierName))
            {
                throw new ArgumentException("Each modifier must be a non-empty string");
            }

            modifierNames.Add(modifierName);
        }

        var unknownModifiers = modifierNames
            .Where(name => !ModifierMap.ContainsKey(name))
            .Distinct(StringComparer.Ordinal)
            .OrderBy(static name => name, StringComparer.Ordinal)
            .ToList();

        if (unknownModifiers.Count > 0)
        {
            throw new ArgumentException(
                $"Unknown modifier names: {string.Join(", ", unknownModifiers)}. Accepted values: ctrl, shift, alt, win");
        }

        return modifierNames
            .Distinct(StringComparer.Ordinal)
            .Select(name => ModifierMap[name])
            .ToList();
    }

    private static JsonArray ToJsonArray(IEnumerable<string> values)
    {
        var array = new JsonArray();
        foreach (var value in values)
        {
            array.Add(value);
        }

        return array;
    }
}
