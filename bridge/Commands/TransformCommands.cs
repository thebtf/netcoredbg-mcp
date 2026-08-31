using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class TransformCommands
{

    public static JsonNode MoveWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var x = @params?["x"]?.GetValue<int>()
            ?? throw new ArgumentException(MissingRequiredParameterMessagePrefix + "x");
        var y = @params?["y"]?.GetValue<int>()
            ?? throw new ArgumentException(MissingRequiredParameterMessagePrefix + "y");

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
                [WindowTitleKey] = title
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
            [WindowTitleKey] = title
        };
    }

    public static JsonNode ResizeWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var width = @params?["width"]?.GetValue<int>()
            ?? throw new ArgumentException(MissingRequiredParameterMessagePrefix + "width");
        var height = @params?["height"]?.GetValue<int>()
            ?? throw new ArgumentException(MissingRequiredParameterMessagePrefix + "height");

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
                [WindowTitleKey] = title
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
            [WindowTitleKey] = title,
            ["request"] = new JsonObject
            {
                ["width"] = width,
                ["height"] = height,
                ["unit"] = UiaPatternUnits,
                [CoordinateSpaceKey] = UiaTransformPatternResizeCoordinateSpace,
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
                ["status"] = mismatchFields.Count == 0 ? MatchedStatus : MismatchStatus,
                ["requested"] = new JsonObject
                {
                    ["width"] = width,
                    ["height"] = height,
                    ["unit"] = UiaPatternUnits,
                    [CoordinateSpaceKey] = UiaTransformPatternResizeCoordinateSpace,
                },
                ["actual"] = new JsonObject
                {
                    ["uia_bounds"] = new JsonObject
                    {
                        ["width"] = uiaWidth,
                        ["height"] = uiaHeight,
                        ["unit"] = PhysicalPixelsKey,
                        [CoordinateSpaceKey] = ScreenCoordinateSpace,
                        [SourceApiKey] = "UIA.BoundingRectangle",
                    },
                    ["window_bounds"] = new JsonObject
                    {
                        ["width"] = windowWidth,
                        ["height"] = windowHeight,
                        ["unit"] = PhysicalPixelsKey,
                        [CoordinateSpaceKey] = ScreenCoordinateSpace,
                        [SourceApiKey] = "GetWindowRect",
                    },
                },
                ["mismatch_fields"] = mismatchFields,
            };
            var uiaBounds = Bounds(
                uia.Left, uia.Top, uia.Right, uia.Bottom, dpiScale, ScreenCoordinateSpace, "UIA.BoundingRectangle");
            uiaBounds["source_coordinate_space"] = "uia_element_bounds";
            result["geometry"] = new JsonObject
            {
                ["status"] = "available",
                ["hwnd"] = native.Hwnd,
                ["uia_bounds"] = uiaBounds,
                ["window_bounds"] = Bounds(
                    native.WindowLeft, native.WindowTop, native.WindowRight, native.WindowBottom,
                    dpiScale, ScreenCoordinateSpace, "GetWindowRect"),
                ["client_bounds"] = Bounds(
                    native.ClientLeft, native.ClientTop, native.ClientRight, native.ClientBottom,
                    dpiScale, "client", "GetClientRect"),
                ["dpi"] = new JsonObject
                {
                    ["value"] = native.Dpi,
                    ["unit"] = "dpi",
                    [SourceApiKey] = "GetDpiForWindow",
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
                    ["unit"] = UiaPatternUnits,
                    [CoordinateSpaceKey] = UiaTransformPatternResizeCoordinateSpace,
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
            [PhysicalPixelsKey] = new JsonObject
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
            [CoordinateSpaceKey] = coordinateSpace,
            [SourceApiKey] = sourceApi,
        };
    }

    private const string WindowTitleKey = "window_title";
    private const string UiaPatternUnits = "uia_pattern_units";
    private const string CoordinateSpaceKey = "coordinate_space";
    private const string UiaTransformPatternResizeCoordinateSpace = "UIA.TransformPattern.Resize";
    private const string PhysicalPixelsKey = "physical_px";
    private const string ScreenCoordinateSpace = "screen";
    private const string SourceApiKey = "source_api";
    private const string MissingRequiredParameterMessagePrefix = "Missing required parameter: ";
    private const string MatchedStatus = "MATCHED";
    private const string MismatchStatus = "MISMATCH";

}
