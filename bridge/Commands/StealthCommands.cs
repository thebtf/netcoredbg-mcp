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
}
