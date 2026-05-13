using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class StealthCommands
{
    [DllImport("user32.dll")]
    private static extern IntPtr GetForegroundWindow();

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool SetForegroundWindow(IntPtr hWnd);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool ShowWindow(IntPtr hWnd, int nCmdShow);

    private const int SW_RESTORE = 9;

    public static JsonNode SaveForeground(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var hwnd = GetForegroundWindow();
        return new JsonObject
        {
            ["hwnd"] = hwnd.ToInt64()
        };
    }

    public static JsonNode RestoreForeground(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var hwndValue = @params?["hwnd"]?.GetValue<long>()
            ?? throw new ArgumentException("Missing required parameter: hwnd");
        var hwnd = new IntPtr(hwndValue);
        var restored = hwnd != IntPtr.Zero && SetForegroundWindow(hwnd);

        return new JsonObject
        {
            ["restored"] = restored
        };
    }

    public static JsonNode FlashFocusSendKeys(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var targetHwnd = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
        if (targetHwnd == IntPtr.Zero)
            throw new InvalidOperationException("Connected window has no native HWND");

        var savedForeground = GetForegroundWindow();
        var stopwatch = Stopwatch.StartNew();
        JsonObject sendResult;
        try
        {
            ShowWindow(targetHwnd, SW_RESTORE);
            SetForegroundWindow(targetHwnd);
            sendResult = InputCommands.SendKeysWithoutForeground(@params, automation, mainWindow);
        }
        finally
        {
            if (savedForeground != IntPtr.Zero)
            {
                SetForegroundWindow(savedForeground);
            }
            stopwatch.Stop();
        }

        return new JsonObject
        {
            ["sent"] = true,
            ["keys"] = sendResult["keys"]?.GetValue<string>() ?? "",
            ["flash_ms"] = (int)stopwatch.ElapsedMilliseconds
        };
    }
}
