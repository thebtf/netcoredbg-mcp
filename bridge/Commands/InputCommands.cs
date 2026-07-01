using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Input;
using FlaUI.Core.WindowsAPI;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static partial class InputCommands
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_RESTORE = 9;

    /// <summary>
    /// Ensure the main window is foreground before any SendInput operation.
    /// Without this, keyboard/mouse input goes to the terminal instead.
    /// </summary>
    private static void EnsureForeground(AutomationElement? mainWindow)
    {
        if (JsonRpcHandler.Stealth)
        {
            Program.Log("stealth: skipping foreground");
            return;
        }

        if (mainWindow is null) return;
        var hwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (hwnd != IntPtr.Zero)
        {
            ShowWindow(hwnd, SW_RESTORE);
            SetForegroundWindow(hwnd);
        }
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

    private static readonly IReadOnlyDictionary<string, string> LiteralSpecialKeys =
        new Dictionary<string, string>(StringComparer.Ordinal)
        {
            ["+"] = "+",
            ["^"] = "^",
            ["%"] = "%",
            ["{"] = "{",
            ["}"] = "}",
            ["("] = "(",
            [")"] = ")",
            ["~"] = "~",
        };

    public static JsonNode SendKeys(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (JsonRpcHandler.Stealth)
        {
            return StealthCommands.FlashFocusSendKeys(@params, automation, mainWindow);
        }

        // Ensure target window is foreground before sending keyboard input.
        // Without this, SendInput goes to the terminal/IDE instead of the app.
        EnsureForeground(mainWindow);

        return SendKeysWithoutForeground(@params, automation, mainWindow);
    }

    internal static JsonObject SendKeysWithoutForeground(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        var keys = @params?["keys"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: keys");

        // If automationId provided, focus that specific element via UIA
        var automationId = @params?["automationId"]?.GetValue<string>();
        if (automationId is not null && mainWindow is not null)
        {
            var cf = new ConditionFactory(automation.PropertyLibrary);
            var element = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId));
            element?.Focus();
        }

        var sent = SendKeySequence(keys);

        return new JsonObject
        {
            ["sent"] = true,
            ["keys"] = string.Join("", sent)
        };
    }

    private static List<string> SendKeySequence(string keys)
    {
        var sent = new List<string>();
        var i = 0;

        while (i < keys.Length)
        {
            switch (keys[i])
            {
                case '^': // Ctrl modifier
                    i++;
                    var ctrlTarget = ConsumeNextToken(keys, ref i);
                    KeySequenceCommands.SendSignedKeyDown(VirtualKeyShort.CONTROL);
                    try { TypeToken(ctrlTarget); }
                    finally { KeySequenceCommands.SendSignedKeyUp(VirtualKeyShort.CONTROL); }
                    sent.Add($"Ctrl+{ctrlTarget}");
                    break;

                case '%': // Alt modifier
                    i++;
                    var altTarget = ConsumeNextToken(keys, ref i);
                    KeySequenceCommands.SendSignedKeyDown(VirtualKeyShort.ALT);
                    try { TypeToken(altTarget); }
                    finally { KeySequenceCommands.SendSignedKeyUp(VirtualKeyShort.ALT); }
                    sent.Add($"Alt+{altTarget}");
                    break;

                case '+': // Shift modifier
                    i++;
                    var shiftTarget = ConsumeNextToken(keys, ref i);
                    KeySequenceCommands.SendSignedKeyDown(VirtualKeyShort.SHIFT);
                    try { TypeToken(shiftTarget); }
                    finally { KeySequenceCommands.SendSignedKeyUp(VirtualKeyShort.SHIFT); }
                    sent.Add($"Shift+{shiftTarget}");
                    break;

                case '{': // Special key
                    if (i + 2 < keys.Length && keys[i + 1] == '}' && keys[i + 2] == '}')
                    {
                        KeySequenceCommands.SendSignedText("}");
                        sent.Add("}");
                        i += 3;
                        break;
                    }
                    var closeBrace = keys.IndexOf('}', i);
                    if (closeBrace < 0)
                        throw new ArgumentException($"Unclosed brace at position {i}");

                    var keyName = keys[(i + 1)..closeBrace];
                    i = closeBrace + 1;

                    if (LiteralSpecialKeys.TryGetValue(keyName, out var literal))
                    {
                        KeySequenceCommands.SendSignedText(literal);
                        sent.Add(literal);
                    }
                    else if (SpecialKeys.TryGetValue(keyName, out var vk))
                    {
                        KeySequenceCommands.SendSignedKeyDown(vk);
                        KeySequenceCommands.SendSignedKeyUp(vk);
                        sent.Add($"{{{keyName}}}");
                    }
                    else
                    {
                        throw new ArgumentException($"Unknown special key: {{{keyName}}}");
                    }
                    break;

                default: // Regular character
                    KeySequenceCommands.SendSignedText(keys[i].ToString());
                    sent.Add(keys[i].ToString());
                    i++;
                    break;
            }
        }

        return sent;
    }

    /// <summary>
    /// Send a batch of key sequences with delay between each, holding foreground
    /// and element focus for the entire batch. Single MCP round-trip for N keystrokes.
    /// </summary>
    public static JsonNode SendKeysBatch(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (JsonRpcHandler.Stealth)
        {
            return StealthCommands.FlashFocusSendKeysBatch(@params, automation, mainWindow);
        }

        // Foreground once for entire batch
        EnsureForeground(mainWindow);

        return SendKeysBatchWithoutForeground(
            @params,
            automation,
            mainWindow,
            ensureForegroundBeforeEach: false);
    }

    internal static JsonObject SendKeysBatchWithoutForeground(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow,
        bool ensureForegroundBeforeEach = false)
    {
        var keysArray = @params?["keys"]?.AsArray()
            ?? throw new ArgumentException("Missing required parameter: keys (array of strings)");
        var delayMs = @params?["delay_ms"]?.GetValue<int>() ?? 50;
        var automationId = @params?["automationId"]?.GetValue<string>();

        // Focus target element once
        AutomationElement? targetElement = null;
        if (automationId is not null && mainWindow is not null)
        {
            var cf = new ConditionFactory(automation.PropertyLibrary);
            targetElement = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId));
            targetElement?.Focus();
        }

        var sentCount = 0;
        foreach (var keyNode in keysArray)
        {
            var keyStr = keyNode?.GetValue<string>();
            if (string.IsNullOrEmpty(keyStr)) continue;

            if (ensureForegroundBeforeEach)
            {
                EnsureForeground(mainWindow);
            }
            targetElement?.Focus();
            SendKeySequence(keyStr);

            sentCount++;
            if (delayMs > 0 && sentCount < keysArray.Count)
                Thread.Sleep(delayMs);
        }

        return new JsonObject
        {
            ["sent"] = true,
            ["count"] = sentCount,
            ["delay_ms"] = delayMs
        };
    }

    public static JsonNode SetValue(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: automationId");
        var value = @params?["value"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: value");

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var element = mainWindow.FindFirstDescendant(cf.ByAutomationId(automationId))
            ?? throw new InvalidOperationException($"Element not found: {automationId}");

        if (!element.Patterns.Value.TryGetPattern(out var valuePattern))
            throw new InvalidOperationException($"Element '{automationId}' does not support ValuePattern");

        valuePattern.SetValue(value);

        return new JsonObject
        {
            ["set"] = true,
            ["automationId"] = automationId,
            ["value"] = value
        };
    }

    private static string ConsumeNextToken(string keys, ref int i)
    {
        if (i >= keys.Length)
            throw new ArgumentException("Modifier at end of string with no target key");

        if (keys[i] == '{')
        {
            var closeBrace = keys.IndexOf('}', i);
            if (closeBrace < 0)
                throw new ArgumentException($"Unclosed brace at position {i}");

            var token = keys[(i + 1)..closeBrace];
            i = closeBrace + 1;
            return token;
        }

        var ch = keys[i].ToString();
        i++;
        return ch;
    }

    private static void TypeToken(string token)
    {
        if (SpecialKeys.TryGetValue(token, out var vk))
        {
            KeySequenceCommands.SendSignedKeyDown(vk);
            KeySequenceCommands.SendSignedKeyUp(vk);
        }
        else
        {
            KeySequenceCommands.SendSignedText(token);
        }
    }
    }
