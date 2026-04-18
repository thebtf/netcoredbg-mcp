using System.Runtime.InteropServices;
using System.Text.Json.Nodes;
using System.Threading;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

/// <summary>
/// Clipboard read/write commands using System.Windows.Clipboard.
/// System.Windows.Clipboard requires an STA (Single-Threaded Apartment) thread.
/// All clipboard calls are wrapped in RunSta to satisfy this requirement.
/// Clipboard access is retried up to 3 times with 25 ms delays to handle
/// transient "Clipboard open by another process" COMExceptions.
/// </summary>
public static class ClipboardCommands
{
    private const int ClipboardRetryCount = 3;
    private const int ClipboardRetryDelayMs = 25;

    /// <summary>
    /// The HRESULT returned by Windows when the clipboard is open by another
    /// process: 0x800401D0 (CLIPBRD_E_CANT_OPEN).
    /// </summary>
    private const int ClipboardOpenError = unchecked((int)0x800401D0);

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

    /// <summary>
    /// Run <paramref name="action"/> on an STA thread, retrying up to
    /// <see cref="ClipboardRetryCount"/> times on clipboard-busy COMExceptions.
    /// Returns <c>{success: false, reason: "clipboard busy"}</c> after exhaustion.
    /// </summary>
    private static JsonObject RunStaWithRetry(Func<JsonObject> action)
    {
        for (var attempt = 1; attempt <= ClipboardRetryCount; attempt++)
        {
            JsonObject? staResult = null;
            COMException? busyException = null;

            var thread = new Thread(() =>
            {
                try
                {
                    staResult = action();
                }
                catch (COMException comEx) when (comEx.HResult == ClipboardOpenError)
                {
                    busyException = comEx;
                }
            });

            thread.SetApartmentState(ApartmentState.STA);
            thread.Start();
            thread.Join();

            if (staResult is not null)
                return staResult;

            if (busyException is not null)
            {
                if (attempt < ClipboardRetryCount)
                {
                    Program.Log($"clipboard: busy on attempt {attempt}/{ClipboardRetryCount}, retrying in {ClipboardRetryDelayMs} ms");
                    Thread.Sleep(ClipboardRetryDelayMs);
                    continue;
                }

                Program.Log($"clipboard: still busy after {ClipboardRetryCount} attempts, giving up");
                return new JsonObject
                {
                    ["success"] = false,
                    ["reason"] = "clipboard busy"
                };
            }

            // Unexpected null (action threw non-COM exception) — re-run via
            // standard RunSta to re-throw on this thread.
            break;
        }

        // Fallback: let RunSta propagate the original exception.
        return RunSta(action);
    }

    public static JsonNode ReadClipboard(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var resultObj = RunStaWithRetry(() =>
        {
            var hasText = System.Windows.Clipboard.ContainsText();
            var text = hasText ? System.Windows.Clipboard.GetText() : string.Empty;
            return new JsonObject
            {
                ["text"] = text,
                ["has_text"] = hasText
            };
        });

        if (resultObj["success"] is { } s && (bool?)s == false)
        {
            Program.Log($"clipboard_read: {resultObj["reason"]}");
            return resultObj;
        }

        Program.Log($"clipboard_read: has_text={resultObj["has_text"]}, length={((string?)resultObj["text"] ?? "").Length}");
        return resultObj;
    }

    public static JsonNode WriteClipboard(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var text = @params?["text"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: text");

        var writeResult = RunStaWithRetry(() =>
        {
            System.Windows.Clipboard.SetText(text);
            return new JsonObject { ["ok"] = true };
        });

        if (writeResult["success"] is { } s && (bool?)s == false)
        {
            Program.Log($"clipboard_write: {writeResult["reason"]}");
            return new JsonObject
            {
                ["written"] = false,
                ["reason"] = (string?)writeResult["reason"] ?? "clipboard busy"
            };
        }

        Program.Log($"clipboard_write: wrote {text.Length} chars");

        return new JsonObject
        {
            ["written"] = true,
            ["length"] = text.Length
        };
    }
}
