using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Capturing;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ScreenshotCommands
{
    public static JsonNode Screenshot(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var hwndValue = @params?["hwnd"]?.GetValue<long>();

        CaptureImage capture;

        if (hwndValue is not null)
        {
            var hwnd = new IntPtr(hwndValue.Value);
            capture = Capture.Screen((int)hwnd);
        }
        else
        {
            var rect = mainWindow.BoundingRectangle;
            capture = Capture.Rectangle(rect);
        }

        using (capture)
        {
            using var ms = new MemoryStream();
            capture.Bitmap.Save(ms, System.Drawing.Imaging.ImageFormat.Png);
            var bytes = ms.ToArray();
            var base64 = Convert.ToBase64String(bytes);

            return new JsonObject
            {
                ["base64"] = base64,
                ["width"] = capture.Bitmap.Width,
                ["height"] = capture.Bitmap.Height
            };
        }
    }
}
