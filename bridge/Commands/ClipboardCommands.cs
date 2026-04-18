using System.Text.Json.Nodes;
using System.Threading;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

/// <summary>
/// Clipboard read/write commands using System.Windows.Clipboard.
/// System.Windows.Clipboard requires an STA (Single-Threaded Apartment) thread.
/// All clipboard calls are wrapped in RunSta to satisfy this requirement.
/// </summary>
public static class ClipboardCommands
{
    /// <summary>
    /// Execute an action on an STA thread and return its result.
    /// Exceptions from the action are re-thrown on the calling thread.
    /// </summary>
    private static JsonObject RunSta(Func<JsonObject> action)
    {
        JsonObject? result = null;
        Exception? capturedException = null;

        var thread = new Thread(() =>
        {
            try
            {
                result = action();
            }
            catch (Exception ex)
            {
                capturedException = ex;
            }
        });

        thread.SetApartmentState(ApartmentState.STA);
        thread.Start();
        thread.Join();

        if (capturedException is not null)
        {
            System.Runtime.ExceptionServices.ExceptionDispatchInfo
                .Capture(capturedException)
                .Throw();
        }

        return result!;
    }

    public static JsonNode ReadClipboard(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var resultObj = RunSta(() =>
        {
            var hasText = System.Windows.Clipboard.ContainsText();
            var text = hasText ? System.Windows.Clipboard.GetText() : string.Empty;
            return new JsonObject
            {
                ["text"] = text,
                ["has_text"] = hasText
            };
        });

        Program.Log($"clipboard_read: has_text={resultObj["has_text"]}, length={((string?)resultObj["text"] ?? "").Length}");
        return resultObj;
    }

    public static JsonNode WriteClipboard(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var text = @params?["text"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: text");

        RunSta(() =>
        {
            System.Windows.Clipboard.SetText(text);
            return new JsonObject(); // placeholder; ignored
        });

        Program.Log($"clipboard_write: wrote {text.Length} chars");

        return new JsonObject
        {
            ["written"] = true,
            ["length"] = text.Length
        };
    }
}
