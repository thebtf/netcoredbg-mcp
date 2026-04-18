using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class TransformCommands
{
    public static JsonNode MoveWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var x = @params?["x"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: x");
        var y = @params?["y"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: y");

        var target = WindowResolver.Resolve(@params, automation, mainWindow);
        var title = WindowResolver.SafeGetTitle(target);

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

        // Foreground the window before invoking TransformPattern so the OS
        // does not silently ignore the move for background windows.
        WindowCommands.EnsureForeground(target);
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

        var target = WindowResolver.Resolve(@params, automation, mainWindow);
        var title = WindowResolver.SafeGetTitle(target);

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

        // Foreground the window before invoking TransformPattern so the OS
        // does not silently ignore the resize for background windows.
        WindowCommands.EnsureForeground(target);
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
