using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Exceptions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

/// <summary>
/// UIA-based focus commands that work regardless of monitor position or DPI.
/// Uses AutomationElement.Focus() + Win32 SetForegroundWindow instead of
/// coordinate-based mouse clicks.
/// </summary>
public static class FocusCommands
{
    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_RESTORE = 9;

    /// <summary>
    /// Set focus to an element using UIA Focus() — monitor/DPI-agnostic.
    /// Also brings the parent window to foreground via Win32.
    /// </summary>
    public static JsonNode SetFocus(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>();
        var name = @params?["name"]?.GetValue<string>();

        var cf = new ConditionFactory(automation.PropertyLibrary);

        // Step 1: Bring window to foreground FIRST (unless stealth). Lazy WPF
        // subtrees (settings panels, virtualized regions) may only realize their
        // UIA peers once the window is restored/foreground — searching before
        // foregrounding made set_focus fail on elements find_element could see.
        if (JsonRpcHandler.Stealth)
        {
            Program.Log("stealth: skipping foreground");
        }
        else
        {
            var hwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
            if (hwnd != IntPtr.Zero)
            {
                ShowWindow(hwnd, SW_RESTORE);
                SetForegroundWindow(hwnd);
            }
        }

        // Step 2: Locate the target with a bounded realization retry — a single
        // FindFirstDescendant intermittently misses peers that a retried lookup
        // resolves (same class of miss the runner's ensure_target retry fixed).
        // Live measurement 2026-07-11: textBoxCue realized ~2s after foreground,
        // so the budget is 5x600ms with an initial settle sleep.
        AutomationElement? element = null;
        if (automationId is not null || name is not null)
        {
            var condition = automationId is not null
                ? cf.ByAutomationId(automationId)
                : cf.ByName(name!);
            Thread.Sleep(250);
            for (var attempt = 0; attempt < 5 && element is null; attempt++)
            {
                if (attempt > 0)
                    Thread.Sleep(600);
                element = mainWindow.FindFirstDescendant(condition);
            }
        }

        // Step 3: Set UIA focus on the target element (or window if no element specified)
        if (element is not null)
        {
            element.Focus();
            return new JsonObject
            {
                ["focused"] = true,
                ["automationId"] = automationId,
                ["name"] = name,
                ["method"] = "UIA.Focus"
            };
        }
        else if (automationId is null && name is null)
        {
            // Just bring window to foreground
            mainWindow.Focus();
            return new JsonObject
            {
                ["focused"] = true,
                ["method"] = "Window.Focus"
            };
        }
        else
        {
            throw new InvalidOperationException(
                $"Element not found: automationId={automationId}, name={name}");
        }
    }

    public static JsonNode AssertFocus(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var selector = @params?["selector"] as JsonObject
            ?? throw new ArgumentException("Missing required parameter: selector");
        var root = ElementCommands.ResolveSearchRoot(mainWindow, selector, automation);
        var expected = ElementCommands.FindElementCascade(root, selector, automation);
        var focused = automation.FocusedElement();
        var matched = focused is not null && IsSameOrDescendant(expected, focused);

        return new JsonObject
        {
            ["status"] = matched ? "PASS" : "FAIL",
            ["focused"] = matched,
            ["reason"] = matched ? "focus matched" : "focus outside selector",
            ["expected"] = ElementCommands.BuildElementInfo(expected, includePatterns: false),
            ["actual"] = focused is null
                ? null
                : ElementCommands.BuildElementInfo(focused, includePatterns: false)
        };
    }

    public static JsonNode GetFocusedElement(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        AutomationElement? focused = null;
        try
        {
            focused = automation.FocusedElement();
        }
        catch (COMException)
        {
            return EmptyFocusedElementInfo();
        }
        catch (InvalidOperationException)
        {
            return EmptyFocusedElementInfo();
        }
        catch (ElementNotAvailableException)
        {
            return EmptyFocusedElementInfo();
        }
        catch (TimeoutException)
        {
            return EmptyFocusedElementInfo();
        }

        if (focused is null)
            return EmptyFocusedElementInfo();
        if (!FocusedElementBelongsToConnectedProcess(focused, mainWindow))
            return EmptyFocusedElementInfo();

        var result = ElementCommands.BuildElementInfo(focused, includePatterns: false);
        result["focused"] = true;
        result["value"] = FocusedValue(focused);
        return result;
    }

    private static JsonObject EmptyFocusedElementInfo()
    {
        return new JsonObject
        {
            ["found"] = false,
            ["focused"] = false,
            ["automationId"] = "",
            ["name"] = "",
            ["controlType"] = "",
            ["className"] = "",
            ["value"] = "",
            ["rect"] = new JsonObject
            {
                ["x"] = 0,
                ["y"] = 0,
                ["width"] = 0,
                ["height"] = 0
            }
        };
    }

    private static bool FocusedElementBelongsToConnectedProcess(
        AutomationElement focused,
        AutomationElement mainWindow)
    {
        var expectedProcessId = JsonRpcHandler.ProcessId;
        if (expectedProcessId == 0)
        {
            try
            {
                expectedProcessId = mainWindow.Properties.ProcessId.ValueOrDefault;
            }
            catch (COMException)
            {
                return false;
            }
            catch (InvalidOperationException)
            {
                return false;
            }
            catch (ElementNotAvailableException)
            {
                return false;
            }
            catch (TimeoutException)
            {
                return false;
            }
        }

        if (expectedProcessId == 0)
            return false;

        try
        {
            return focused.Properties.ProcessId.ValueOrDefault == expectedProcessId;
        }
        catch (COMException)
        {
            return false;
        }
        catch (InvalidOperationException)
        {
            return false;
        }
        catch (ElementNotAvailableException)
        {
            return false;
        }
        catch (TimeoutException)
        {
            return false;
        }
    }

    private static string FocusedValue(AutomationElement focused)
    {
        try
        {
            if (focused.Patterns.Value.TryGetPattern(out var valuePattern))
                return valuePattern.Value.ValueOrDefault ?? "";
        }
        catch
        {
            // Unsupported or stale UIA providers should not make the identity query fail.
        }

        return "";
    }

    private static bool SameRuntimeId(AutomationElement left, AutomationElement right)
    {
        try
        {
            var leftId = left.Properties.RuntimeId.ValueOrDefault;
            var rightId = right.Properties.RuntimeId.ValueOrDefault;
            return leftId is not null && rightId is not null && leftId.SequenceEqual(rightId);
        }
        catch (COMException)
        {
            return false;
        }
        catch (InvalidOperationException)
        {
            return false;
        }
    }

    private static bool IsSameOrDescendant(AutomationElement expected, AutomationElement focused)
    {
        const int maxDepth = 50;
        var current = focused;
        for (var depth = 0; current is not null && depth < maxDepth; depth++)
        {
            if (SameRuntimeId(expected, current))
                return true;

            try
            {
                current = current.Parent;
            }
            catch (COMException)
            {
                return false;
            }
            catch (InvalidOperationException)
            {
                return false;
            }
        }

        return false;
    }
}
