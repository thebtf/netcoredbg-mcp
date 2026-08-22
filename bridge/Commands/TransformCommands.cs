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

        var result = new JsonObject
        {
            ["resized"] = true,
            ["width"] = width,
            ["height"] = height,
            ["window_title"] = title,
            ["request"] = new JsonObject
            {
                ["width"] = width,
                ["height"] = height,
                ["unit"] = "uia_pattern_units",
                ["coordinate_space"] = "UIA.TransformPattern.Resize",
            }
        };

        try
        {
            var hwnd = target.Properties.NativeWindowHandle.ValueOrDefault;
            var native = ScreenshotCommands.ReadWindowGeometry(hwnd);
            var uia = target.BoundingRectangle;
            var dpiScale = native.Dpi / 96d;
            var uiaWidth = uia.Right - uia.Left;
            var uiaHeight = uia.Bottom - uia.Top;
            var windowWidth = native.WindowRight - native.WindowLeft;
            var windowHeight = native.WindowBottom - native.WindowTop;
            var mismatchFields = new JsonArray();
            if (uiaWidth != width)
                mismatchFields.Add("uia_width");
            if (uiaHeight != height)
                mismatchFields.Add("uia_height");
            if (windowWidth != width)
                mismatchFields.Add("window_width");
            if (windowHeight != height)
                mismatchFields.Add("window_height");
            result["target_comparability"] = new JsonObject
            {
                ["status"] = mismatchFields.Count == 0 ? "MATCHED" : "MISMATCH",
                ["requested"] = new JsonObject
                {
                    ["width"] = width,
                    ["height"] = height,
                    ["unit"] = "uia_pattern_units",
                    ["coordinate_space"] = "UIA.TransformPattern.Resize",
                },
                ["actual"] = new JsonObject
                {
                    ["uia_bounds"] = new JsonObject
                    {
                        ["width"] = uiaWidth,
                        ["height"] = uiaHeight,
                        ["unit"] = "physical_px",
                        ["coordinate_space"] = "screen",
                        ["source_api"] = "UIA.BoundingRectangle",
                    },
                    ["window_bounds"] = new JsonObject
                    {
                        ["width"] = windowWidth,
                        ["height"] = windowHeight,
                        ["unit"] = "physical_px",
                        ["coordinate_space"] = "screen",
                        ["source_api"] = "GetWindowRect",
                    },
                },
                ["mismatch_fields"] = mismatchFields,
            };
            var uiaBounds = Bounds(
                uia.Left, uia.Top, uia.Right, uia.Bottom, dpiScale, "screen", "UIA.BoundingRectangle");
            uiaBounds["source_coordinate_space"] = "uia_element_bounds";
            result["geometry"] = new JsonObject
            {
                ["status"] = "available",
                ["hwnd"] = native.Hwnd,
                ["uia_bounds"] = uiaBounds,
                ["window_bounds"] = Bounds(
                    native.WindowLeft, native.WindowTop, native.WindowRight, native.WindowBottom,
                    dpiScale, "screen", "GetWindowRect"),
                ["client_bounds"] = Bounds(
                    native.ClientLeft, native.ClientTop, native.ClientRight, native.ClientBottom,
                    dpiScale, "client", "GetClientRect"),
                ["dpi"] = new JsonObject
                {
                    ["value"] = native.Dpi,
                    ["unit"] = "dpi",
                    ["source_api"] = "GetDpiForWindow",
                },
                ["dpi_scale"] = new JsonObject
                {
                    ["value"] = dpiScale,
                    ["reference_dpi"] = 96,
                },
            };
        }
        catch (Exception error)
        {
            result["geometry"] = new JsonObject
            {
                ["status"] = "unavailable",
                ["code"] = "POST_RESIZE_GEOMETRY_UNAVAILABLE",
                ["reason"] = error.Message,
            };
            result["target_comparability"] = new JsonObject
            {
                ["status"] = "UNAVAILABLE",
                ["requested"] = new JsonObject
                {
                    ["width"] = width,
                    ["height"] = height,
                    ["unit"] = "uia_pattern_units",
                    ["coordinate_space"] = "UIA.TransformPattern.Resize",
                },
                ["mismatch_fields"] = new JsonArray(),
                ["code"] = "POST_RESIZE_GEOMETRY_UNAVAILABLE",
            };
        }

        return result;
    }

    private static JsonObject Bounds(
        double left,
        double top,
        double right,
        double bottom,
        double dpiScale,
        string coordinateSpace,
        string sourceApi)
    {
        return new JsonObject
        {
            ["physical_px"] = new JsonObject
            {
                ["left"] = left,
                ["top"] = top,
                ["right"] = right,
                ["bottom"] = bottom,
            },
            ["dip"] = new JsonObject
            {
                ["left"] = left / dpiScale,
                ["top"] = top / dpiScale,
                ["right"] = right / dpiScale,
                ["bottom"] = bottom / dpiScale,
            },
            ["coordinate_space"] = coordinateSpace,
            ["source_api"] = sourceApi,
        };
    }
}
