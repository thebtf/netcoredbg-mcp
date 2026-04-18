using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class TransformCommands
{
    private static AutomationElement ResolveTargetWindow(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        var windowTitle = @params?["window_title"]?.GetValue<string>();

        if (windowTitle is not null)
        {
            var processId = JsonRpcHandler.ProcessId;
            if (processId > 0)
            {
                var desktop = automation.GetDesktop();
                var children = desktop.FindAllChildren();
                foreach (var child in children)
                {
                    try
                    {
                        if (child.Properties.ProcessId.ValueOrDefault != processId)
                            continue;

                        var name = child.Name ?? "";
                        if (name.Contains(windowTitle, StringComparison.OrdinalIgnoreCase))
                            return child;
                    }
                    catch
                    {
                        // Ignore inaccessible windows
                    }
                }
            }

            throw new InvalidOperationException(
                $"No window with title containing '{windowTitle}' found for the connected process.");
        }

        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        return mainWindow;
    }

    private static string SafeGetWindowTitle(AutomationElement element)
    {
        try { return element.Name ?? ""; }
        catch { return ""; }
    }

    public static JsonNode MoveWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var x = @params?["x"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: x");
        var y = @params?["y"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: y");

        var target = ResolveTargetWindow(@params, automation, mainWindow);
        var title = SafeGetWindowTitle(target);

        if (!target.Patterns.Transform.TryGetPattern(out var pattern))
            throw new InvalidOperationException("Element does not support TransformPattern");

        if (!pattern.CanMove.Value)
        {
            Program.Log($"move_window: '{title}' is not movable");
            return new JsonObject
            {
                ["moved"] = false,
                ["reason"] = "window is not movable",
                ["window_title"] = title
            };
        }

        pattern.Move(x, y);
        Program.Log($"move_window: moved '{title}' to ({x}, {y})");

        return new JsonObject
        {
            ["moved"] = true,
            ["x"] = x,
            ["y"] = y,
            ["window_title"] = title
        };
    }

    public static JsonNode ResizeWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var width = @params?["width"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: width");
        var height = @params?["height"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: height");

        var target = ResolveTargetWindow(@params, automation, mainWindow);
        var title = SafeGetWindowTitle(target);

        if (!target.Patterns.Transform.TryGetPattern(out var pattern))
            throw new InvalidOperationException("Element does not support TransformPattern");

        if (!pattern.CanResize.Value)
        {
            Program.Log($"resize_window: '{title}' is not resizable");
            return new JsonObject
            {
                ["resized"] = false,
                ["reason"] = "window is not resizable",
                ["window_title"] = title
            };
        }

        pattern.Resize(width, height);
        Program.Log($"resize_window: resized '{title}' to {width}x{height}");

        return new JsonObject
        {
            ["resized"] = true,
            ["width"] = width,
            ["height"] = height,
            ["window_title"] = title
        };
    }
}
