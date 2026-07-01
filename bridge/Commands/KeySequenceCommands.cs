using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.Core.Input;
using FlaUI.Core.WindowsAPI;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class KeySequenceCommands
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint SendInput(uint nInputs, INPUT[] pInputs, int cbSize);

    [DllImport("user32.dll")]
    private static extern uint MapVirtualKey(uint uCode, uint uMapType);

    private const int SW_RESTORE = 9;
    private const int INPUT_SETTLE_MS = 30;
    private const int MODIFIER_OBSERVATION_MS = 100;
    private const uint INPUT_KEYBOARD = 1;
    private const uint MAPVK_VK_TO_VSC = 0;
    private const uint KEYEVENTF_EXTENDEDKEY = 0x0001;
    private const uint KEYEVENTF_KEYUP = 0x0002;
    private const uint KEYEVENTF_SCANCODE = 0x0008;

    [StructLayout(LayoutKind.Sequential)]
    private struct INPUT
    {
        public uint type;
        public InputUnion U;
    }

    [StructLayout(LayoutKind.Explicit)]
    private struct InputUnion
    {
        [FieldOffset(0)]
        public MOUSEINPUT mi;

        [FieldOffset(0)]
        public KEYBDINPUT ki;

        [FieldOffset(0)]
        public HARDWAREINPUT hi;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct MOUSEINPUT
    {
        public int dx;
        public int dy;
        public uint mouseData;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct KEYBDINPUT
    {
        public ushort wVk;
        public ushort wScan;
        public uint dwFlags;
        public uint time;
        public IntPtr dwExtraInfo;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct HARDWAREINPUT
    {
        public uint uMsg;
        public ushort wParamL;
        public ushort wParamH;
    }

    private static readonly IReadOnlyDictionary<string, VirtualKeyShort> SpecialKeys =
        new Dictionary<string, VirtualKeyShort>(StringComparer.OrdinalIgnoreCase)
        {
            ["ENTER"] = VirtualKeyShort.RETURN,
            ["RETURN"] = VirtualKeyShort.RETURN,
            ["TAB"] = VirtualKeyShort.TAB,
            ["ESCAPE"] = VirtualKeyShort.ESCAPE,
            ["ESC"] = VirtualKeyShort.ESCAPE,
            ["BACKSPACE"] = VirtualKeyShort.BACK,
            ["DELETE"] = VirtualKeyShort.DELETE,
            ["DEL"] = VirtualKeyShort.DELETE,
            ["UP"] = VirtualKeyShort.UP,
            ["DOWN"] = VirtualKeyShort.DOWN,
            ["LEFT"] = VirtualKeyShort.LEFT,
            ["RIGHT"] = VirtualKeyShort.RIGHT,
            ["HOME"] = VirtualKeyShort.HOME,
            ["END"] = VirtualKeyShort.END,
            ["PGUP"] = VirtualKeyShort.PRIOR,
            ["PGDN"] = VirtualKeyShort.NEXT,
            ["SPACE"] = VirtualKeyShort.SPACE,
            ["F1"] = VirtualKeyShort.F1,
            ["F2"] = VirtualKeyShort.F2,
            ["F3"] = VirtualKeyShort.F3,
            ["F4"] = VirtualKeyShort.F4,
            ["F5"] = VirtualKeyShort.F5,
            ["F6"] = VirtualKeyShort.F6,
            ["F7"] = VirtualKeyShort.F7,
            ["F8"] = VirtualKeyShort.F8,
            ["F9"] = VirtualKeyShort.F9,
            ["F10"] = VirtualKeyShort.F10,
            ["F11"] = VirtualKeyShort.F11,
            ["F12"] = VirtualKeyShort.F12,
        };

    private static readonly IReadOnlyDictionary<string, VirtualKeyShort> ModifierKeys =
        new Dictionary<string, VirtualKeyShort>(StringComparer.OrdinalIgnoreCase)
        {
            ["ctrl"] = VirtualKeyShort.CONTROL,
            ["shift"] = VirtualKeyShort.SHIFT,
            ["alt"] = VirtualKeyShort.ALT,
            ["win"] = VirtualKeyShort.LWIN
        };

    public static JsonNode ScopedKeySequence(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var selector = @params?["selector"];
        var modifiers = ReadStringArray(@params?["modifiers"], "modifiers");
        var keys = ReadStringArray(@params?["keys"], "keys");
        var modifierKeys = modifiers
            .Select(ParseModifier)
            .GroupBy(static modifier => modifier.Key)
            .Select(static group => group.First())
            .ToList();
        var parsedKeys = keys.Select(ParseKey).ToList();
        var focusResult = FocusTarget(selector, automation, mainWindow);
        JsonNode releaseResult = new JsonObject { ["released"] = false };
        JsonNode finalHeld = new JsonArray();
        var sent = new JsonArray();
        var scopedHeld = new List<(string Name, VirtualKeyShort Key)>();
        var status = "PASS";
        string? failureReason = null;

        try
        {
            foreach (var modifier in modifierKeys)
            {
                if (TryAcquireScopedModifier(modifier))
                    scopedHeld.Add(modifier);
            }
            if (scopedHeld.Count > 0)
                Thread.Sleep(INPUT_SETTLE_MS);

            foreach (var key in parsedKeys)
            {
                SendSignedKeyDown(key.Key);
                SendSignedKeyUp(key.Key);
                sent.Add(key.Name);
            }
            if (scopedHeld.Count > 0 && sent.Count > 0)
                Thread.Sleep(MODIFIER_OBSERVATION_MS);
        }
        catch (Exception ex)
        {
            status = "FAIL";
            failureReason = ex.Message;
        }
        finally
        {
            var releaseFailures = new JsonArray();
            var releaseFailureNames = new List<string>();
            for (var index = scopedHeld.Count - 1; index >= 0; index--)
            {
                var modifier = scopedHeld[index];
                try
                {
                    ReleaseScopedModifier(modifier.Key);
                }
                catch (Exception ex)
                {
                    releaseFailureNames.Add(modifier.Name);
                    releaseFailures.Add($"{modifier.Name}: {ex.Message}");
                }
            }

            var released = releaseFailures.Count == 0;
            finalHeld = released
                ? new JsonArray()
                : ToJsonArray(releaseFailureNames);
            releaseResult = new JsonObject
            {
                ["released"] = released,
                ["modifiers"] = finalHeld.DeepClone()
            };
            if (!released)
                releaseResult["failures"] = releaseFailures;
        }

        var result = new JsonObject
        {
            ["status"] = status,
            ["focused"] = focusResult,
            ["sent_count"] = sent.Count,
            ["sent_keys"] = sent,
            ["held_modifiers_during_sequence"] = ToJsonArray(modifiers),
            ["release_result"] = releaseResult,
            ["final_held_modifiers"] = finalHeld
        };
        if (failureReason is not null)
            result["reason"] = failureReason;
        return result;
    }

    private static JsonObject FocusTarget(
        JsonNode? selector,
        UIA3Automation automation,
        AutomationElement mainWindow)
    {
        BringToForeground(mainWindow);
        var target = FindTarget(selector, automation, mainWindow);
        target.Focus();
        return new JsonObject
        {
            ["focused"] = true,
            ["automationId"] = SafeString(() => target.AutomationId),
            ["name"] = SafeString(() => target.Name),
            ["method"] = target.Equals(mainWindow) ? "Window.Focus" : "UIA.Focus"
        };
    }

    private static AutomationElement FindTarget(
        JsonNode? selector,
        UIA3Automation automation,
        AutomationElement mainWindow)
    {
        if (selector is null)
            return mainWindow;

        var root = ElementCommands.ResolveSearchRoot(mainWindow, selector, automation);
        var conditions = new List<ConditionBase>();
        var cf = new ConditionFactory(automation.PropertyLibrary);

        var automationId = selector["automationId"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(automationId))
            conditions.Add(cf.ByAutomationId(automationId));

        var name = selector["name"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(name))
            conditions.Add(cf.ByName(name));

        var controlType = selector["controlType"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(controlType) &&
            Enum.TryParse<ControlType>(controlType, true, out var ct))
            conditions.Add(cf.ByControlType(ct));

        if (conditions.Count == 0)
            return root;

        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());
        return root.FindFirstDescendant(condition)
            ?? throw new InvalidOperationException("Key sequence target not found.");
    }

    private static void BringToForeground(AutomationElement mainWindow)
    {
        var hwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (hwnd == IntPtr.Zero)
            return;
        ShowWindow(hwnd, SW_RESTORE);
        SetForegroundWindow(hwnd);
    }

    private static (string Name, VirtualKeyShort Key) ParseKey(string key)
    {
        var normalized = key.Trim();
        if (normalized.StartsWith('{') && normalized.EndsWith('}'))
            normalized = normalized[1..^1];
        if (SpecialKeys.TryGetValue(normalized, out var special))
            return (normalized.ToUpperInvariant(), special);
        if (normalized.Length == 1)
        {
            var character = normalized[0];
            var isAsciiLetter =
                (character >= 'a' && character <= 'z') ||
                (character >= 'A' && character <= 'Z');
            var isAsciiDigit = character >= '0' && character <= '9';
            if (isAsciiLetter || isAsciiDigit)
            {
                var upper = char.ToUpperInvariant(character);
                return (upper.ToString(), (VirtualKeyShort)upper);
            }
        }
        throw new ArgumentException($"Unknown key: {key}");
    }

    private static (string Name, VirtualKeyShort Key) ParseModifier(string modifier)
    {
        var normalized = modifier.Trim().ToLowerInvariant();
        if (ModifierKeys.TryGetValue(normalized, out var key))
            return (normalized, key);
        throw new ArgumentException(
            $"Unknown modifier names: {modifier}. Accepted values: alt, ctrl, shift, win");
    }

    private static bool TryAcquireScopedModifier((string Name, VirtualKeyShort Key) modifier)
    {
        lock (JsonRpcHandler.HeldModifiersLock)
        {
            if (JsonRpcHandler.HeldModifiers.Contains(modifier.Key))
                return false;

            SendSignedKeyDown(modifier.Key);
            JsonRpcHandler.HeldModifiers.Add(modifier.Key);
            return true;
        }
    }

    private static void ReleaseScopedModifier(VirtualKeyShort key)
    {
        lock (JsonRpcHandler.HeldModifiersLock)
        {
            SendSignedKeyUp(key);
            JsonRpcHandler.HeldModifiers.Remove(key);
        }
    }

    internal static void SendSignedKeyDown(VirtualKeyShort key)
    {
        SendKey(key, keyUp: false);
    }

    internal static void SendSignedKeyUp(VirtualKeyShort key)
    {
        SendKey(key, keyUp: true);
    }

    private static void SendKey(VirtualKeyShort key, bool keyUp)
    {
        var input = new INPUT
        {
            type = INPUT_KEYBOARD,
            U = new InputUnion
            {
                ki = new KEYBDINPUT
                {
                    wVk = 0,
                    wScan = (ushort)MapVirtualKey((uint)key, MAPVK_VK_TO_VSC),
                    dwFlags = KEYEVENTF_SCANCODE |
                              (IsExtendedKey(key) ? KEYEVENTF_EXTENDEDKEY : 0) |
                              (keyUp ? KEYEVENTF_KEYUP : 0),
                    time = 0,
                    dwExtraInfo = InputSignature.RunnerInputSignatureIntPtr
                }
            }
        };
        var sent = SendInput(1, [input], Marshal.SizeOf<INPUT>());
        if (sent != 1)
        {
            var error = Marshal.GetLastWin32Error();
            var direction = keyUp ? "up" : "down";
            throw new InvalidOperationException(
                $"SendInput failed for {key} {direction}: Win32 error {error}");
        }
    }

    private static bool IsExtendedKey(VirtualKeyShort key)
    {
        return key is VirtualKeyShort.UP or
            VirtualKeyShort.DOWN or
            VirtualKeyShort.LEFT or
            VirtualKeyShort.RIGHT or
            VirtualKeyShort.HOME or
            VirtualKeyShort.END or
            VirtualKeyShort.PRIOR or
            VirtualKeyShort.NEXT or
            VirtualKeyShort.INSERT or
            VirtualKeyShort.DELETE or
            VirtualKeyShort.RCONTROL or
            VirtualKeyShort.RMENU or
            VirtualKeyShort.LWIN;
    }

    private static List<string> ReadStringArray(JsonNode? node, string name)
    {
        var array = node?.AsArray()
            ?? throw new ArgumentException($"Missing required parameter: {name}");
        return array
            .Select(value => value?.GetValue<string>() ?? "")
            .Where(value => !string.IsNullOrWhiteSpace(value))
            .ToList();
    }

    private static JsonArray ToJsonArray(IEnumerable<string> values)
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
