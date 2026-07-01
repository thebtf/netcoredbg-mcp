using System.Diagnostics;
using System.Runtime.ExceptionServices;
using System.Text.Json.Nodes;
using System.Threading;
using System.Drawing;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Input;
using FlaUI.Core.WindowsAPI;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static partial class GridCommands
{
    [System.Runtime.InteropServices.DllImport("user32.dll")]
    private static extern void mouse_event(
        uint dwFlags,
        uint dx,
        uint dy,
        uint dwData,
        UIntPtr dwExtraInfo);

    private const int RowDragThresholdPixels = 6;
    private const int RowDragPointerDownSettleMs = 100;
    private const int RowDragThresholdMoveSettleMs = 60;
    private const int RowDragFinalDropSettleMs = 180;
    private const int RowDragEdgeHoldPulseMs = 120;
    private const double RowDragEdgeScrollDownRatio = 0.96;
    private const double RowDragEdgeScrollUpRatio = 0.25;
    private const double RowDragNeutralBandTopRatio = 0.33;
    private const double RowDragNeutralBandBottomRatio = 0.81;
    private const uint MOUSEEVENTF_LEFTDOWN = 0x0002;
    private const uint MOUSEEVENTF_LEFTUP = 0x0004;

    public static JsonNode DragRowToRow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (JsonRpcHandler.Stealth)
        {
            return Blocked(
                "grid row drag requires foreground mouse input",
                new JsonObject { ["mode"] = "stealth" },
                new JsonObject { ["mode"] = "non-stealth FlaUI bridge" },
                "Disable stealth mode before source-anchor preserving row drag.");
        }

        var grid = ResolveGrid(@params, automation, mainWindow);
        if (!grid.Patterns.Grid.TryGetPattern(out _))
            return Unsupported("GridPattern");
        var gridBounds = SafeRect(grid);

        var sourceRowKey = StringValue(@params?["source_row_key"]);
        var sourceRowIndex = ReadOptionalInt(@params, "source_row_index");
        var targetRowKey = StringValue(@params?["target_row_key"]);
        var targetRowIndex = ReadOptionalInt(@params, "target_row_index");
        if (sourceRowIndex is null && string.IsNullOrWhiteSpace(sourceRowKey))
        {
            return Blocked(
                "grid drag request missing source row",
                new JsonObject { ["source_row_index"] = sourceRowIndex, ["source_row_key"] = sourceRowKey },
                new JsonObject { ["source"] = "source_row_index or source_row_key" },
                "Provide a visible source row for grid_drag_row_to_row.");
        }
        if (targetRowIndex is null && string.IsNullOrWhiteSpace(targetRowKey))
        {
            return Blocked(
                "grid drag request missing target row",
                new JsonObject { ["target_row_index"] = targetRowIndex, ["target_row_key"] = targetRowKey },
                new JsonObject { ["target"] = "target_row_index or target_row_key" },
                "Provide a target row for grid_drag_row_to_row.");
        }

        var columns = ReadColumns(@params);
        var headers = ColumnHeaders(grid);
        var maxScrolls = ReadBoundedInt(
            @params,
            "max_scrolls",
            DefaultMaxEnsureVisibleScrolls,
            min: 0,
            max: 250);
        var settleMs = ReadBoundedInt(
            @params,
            "scroll_settle_ms",
            DefaultEnsureVisibleSettleMs,
            min: 0,
            max: 2_000);
        var speedMs = ReadBoundedInt(
            @params,
            "speed_ms",
            200,
            min: 20,
            max: 10_000);

        var sourceSearch = SearchCurrentRows(
            grid,
            automation,
            columns,
            headers,
            sourceRowIndex,
            sourceRowKey,
            "source_current");
        if (sourceSearch.Blocked is not null)
            return sourceSearch.Blocked;
        if (sourceSearch.Match is null)
        {
            return Blocked(
                "grid drag source is not visible",
                RequestedRow(sourceRowIndex, sourceRowKey),
                new JsonObject { ["source"] = "currently visible DataGrid row" },
                "Use source ensure-visible before the row-to-row drag.");
        }

        var sourceMatch = sourceSearch.Match;
        var sourcePointResult = ClickPoint(sourceMatch.Element);
        if (sourcePointResult.Blocked is not null)
            return sourcePointResult.Blocked;

        var sourcePoint = sourcePointResult.Point;
        var sourceBounds = SafeRect(sourceMatch.Element);
        var sourceThresholdPoint = DragThresholdPoint(sourceBounds, sourcePoint);
        var targetDirection = RowDragDirection(sourceMatch.Row, targetRowIndex);
        var resolvedTargetDirection = targetDirection;
        var targetSearchBeforeDrag = SearchCurrentRows(
            grid,
            automation,
            columns,
            headers,
            targetRowIndex,
            targetRowKey,
            "target_current");
        if (targetSearchBeforeDrag.Blocked is not null)
            return targetSearchBeforeDrag.Blocked;

        var temporaryModifiers = ModifierCommands.GetTemporaryModifierKeys(@params?["hold_modifiers"]);
        var pressedTemporaryModifiers = new List<VirtualKeyShort>();
        var mouseButtonDown = false;
        ExceptionDispatchInfo? capturedException = null;
        Exception? cleanupException = null;
        JsonObject? blockedResult = null;
        RowMatch? targetMatch = targetSearchBeforeDrag.Match;
        JsonNode? targetEnsureVisibleResult = null;
        var targetWasAlreadyVisible = targetMatch is not null;
        JsonArray edgeScanAttempts = targetSearchBeforeDrag.Attempts.DeepClone() as JsonArray ?? new JsonArray();
        JsonArray stabilizationAttempts = new JsonArray();
        Point? actualDropPoint = null;
        JsonObject? preReleaseTargetBounds = null;
        var dropPointStrategy = targetWasAlreadyVisible ? "visible-edge-fallback" : "neutral-band";

        ClickCommands.EnsureForeground(mainWindow);
        var stopwatch = Stopwatch.StartNew();
        try
        {
            foreach (var modifier in temporaryModifiers)
            {
                Keyboard.Press(modifier);
                pressedTemporaryModifiers.Add(modifier);
            }

            ClickCommands.MoveCursor(sourcePoint.X, sourcePoint.Y);
            Thread.Sleep(RowDragThresholdMoveSettleMs);
            mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, InputSignature.RunnerInputSignature);
            mouseButtonDown = true;
            Thread.Sleep(RowDragPointerDownSettleMs);

            ClickCommands.MoveCursor(sourceThresholdPoint.X, sourceThresholdPoint.Y);
            Thread.Sleep(RowDragThresholdMoveSettleMs);

            if (targetMatch is null)
            {
                var targetScan = ScanForRowWithHeldDrag(
                    grid,
                    gridBounds,
                    automation,
                    columns,
                    headers,
                    targetRowIndex,
                    targetRowKey,
                    targetDirection,
                    maxScrolls,
                    Math.Max(settleMs, 300),
                    out resolvedTargetDirection);
                if (targetScan.Blocked is not null)
                {
                    blockedResult = targetScan.Blocked;
                }
                else if (targetScan.Match is null)
                {
                    blockedResult = new JsonObject
                    {
                        ["status"] = "BLOCKED",
                        ["reason"] = "target row was not found during active drag edge scroll",
                        ["requested"] = RequestedRow(targetRowIndex, targetRowKey),
                        ["accepted"] = new JsonObject
                        {
                            ["target"] = "row that becomes visible through held drag edge scroll",
                            ["max_scrolls"] = maxScrolls
                        },
                        ["attempts"] = targetScan.Attempts,
                        ["next_step"] = "Use a nearer drop target or keep the cursor on the drag edge longer."
                    };
                }
                else
                {
                    targetMatch = targetScan.Match;
                    edgeScanAttempts = targetScan.Attempts.DeepClone() as JsonArray ?? new JsonArray();
                    targetEnsureVisibleResult = new JsonObject
                    {
                        ["status"] = "PASS",
                        ["already_visible"] = false,
                        ["resolved_row"] = CompactRow(targetMatch.Row),
                        ["row"] = targetMatch.Row.DeepClone(),
                        ["attempts"] = targetScan.Attempts.DeepClone()
                    };
                }
            }
            else
            {
                targetEnsureVisibleResult = new JsonObject
                {
                    ["status"] = "PASS",
                    ["already_visible"] = true,
                    ["resolved_row"] = CompactRow(targetMatch.Row),
                    ["row"] = targetMatch.Row.DeepClone(),
                    ["attempts"] = targetSearchBeforeDrag.Attempts.DeepClone()
                };
            }

            if (blockedResult is null && targetMatch is not null)
            {
                if (!targetWasAlreadyVisible)
                {
                    var stabilizedTarget = StabilizeHeldDragTarget(
                        grid,
                        gridBounds,
                        automation,
                        columns,
                        headers,
                        targetRowIndex,
                        targetRowKey,
                        resolvedTargetDirection,
                        Math.Max(settleMs, RowDragFinalDropSettleMs));
                    stabilizationAttempts = stabilizedTarget.Attempts.DeepClone() as JsonArray ?? new JsonArray();
                    if (stabilizedTarget.Blocked is not null)
                    {
                        blockedResult = stabilizedTarget.Blocked;
                    }
                    else if (stabilizedTarget.Match is null)
                    {
                        blockedResult = new JsonObject
                        {
                            ["status"] = "BLOCKED",
                            ["reason"] = "target row could not be stabilized before mouse-up",
                            ["requested"] = RequestedRow(targetRowIndex, targetRowKey),
                            ["attempts"] = stabilizationAttempts.DeepClone(),
                            ["next_step"] = "Capture stabilization attempts and adjust the final drop strategy."
                        };
                    }
                    else
                    {
                        targetMatch = stabilizedTarget.Match;
                        dropPointStrategy = "stabilized-neutral-band";
                        targetEnsureVisibleResult = new JsonObject
                        {
                            ["status"] = "PASS",
                            ["already_visible"] = false,
                            ["resolved_row"] = CompactRow(targetMatch.Row),
                            ["row"] = targetMatch.Row.DeepClone(),
                            ["attempts"] = edgeScanAttempts.DeepClone(),
                            ["stabilization_attempts"] = stabilizationAttempts.DeepClone()
                        };
                    }
                }
            }

            if (blockedResult is null && targetMatch is not null)
            {
                var liveTargetRow = RefreshRowBounds(targetMatch);
                var allowStabilizedFallback = !targetWasAlreadyVisible && stabilizationAttempts.Count > 0;
                var targetPoint = TargetDropPoint(
                    liveTargetRow,
                    gridBounds,
                    resolvedTargetDirection,
                    allowFallback: targetWasAlreadyVisible || allowStabilizedFallback);
                if (targetPoint is not null && allowStabilizedFallback)
                {
                    dropPointStrategy = "stabilized-band-adjacent-fallback";
                }
                preReleaseTargetBounds = liveTargetRow["bounds"] as JsonObject;
                if (targetPoint is null)
                {
                    dropPointStrategy = "neutral-band-blocked";
                    var dropBandDiagnostics = DropBandDiagnostics(liveTargetRow, gridBounds);
                    blockedResult = new JsonObject
                    {
                        ["status"] = "BLOCKED",
                        ["reason"] = "target row remained inside the drag edge-scroll zone",
                        ["requested"] = RequestedRow(targetRowIndex, targetRowKey),
                        ["accepted"] = new JsonObject
                        {
                            ["target"] = "row whose bounds intersect the neutral viewport drop band"
                        },
                        ["resolved_direction"] = resolvedTargetDirection.ToString().ToLowerInvariant(),
                        ["drop_point_strategy"] = dropPointStrategy,
                        ["target_row"] = liveTargetRow.DeepClone(),
                        ["pre_release_target_bounds"] = preReleaseTargetBounds?.DeepClone(),
                        ["drop_band_diagnostics"] = dropBandDiagnostics,
                        ["edge_scan_attempts"] = edgeScanAttempts.DeepClone(),
                        ["stabilization_attempts"] = stabilizationAttempts.DeepClone(),
                        ["next_step"] = "Keep dragging until the target row settles farther from the viewport edge."
                    };
                }
                if (blockedResult is null && targetPoint is not null)
                {
                    actualDropPoint = targetPoint.Value;
                    ClickCommands.MoveCursor(targetPoint.Value.X, targetPoint.Value.Y);
                    Thread.Sleep(Math.Max(RowDragFinalDropSettleMs, speedMs / 10));
                }
            }
        }
        catch (Exception ex)
        {
            capturedException = ExceptionDispatchInfo.Capture(ex);
        }
        finally
        {
            stopwatch.Stop();
            if (mouseButtonDown)
            {
                try
                {
                    mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, InputSignature.RunnerInputSignature);
                }
                catch (Exception ex)
                {
                    cleanupException ??= new InvalidOperationException(
                        "Grid row drag cleanup failed to release left mouse button.",
                        ex);
                }
            }

            for (var index = pressedTemporaryModifiers.Count - 1; index >= 0; index--)
            {
                try
                {
                    Keyboard.Release(pressedTemporaryModifiers[index]);
                }
                catch (Exception ex)
                {
                    cleanupException ??= new InvalidOperationException(
                        $"Grid row drag cleanup failed to release modifier {pressedTemporaryModifiers[index]}.",
                        ex);
                }
            }
        }

        capturedException?.Throw();
        if (cleanupException is not null)
            throw cleanupException;

        var cleanup = new JsonObject
        {
            ["modifier_cleanup"] = new JsonObject
            {
                ["released"] = new JsonArray(temporaryModifiers.Select(m => (JsonNode?)m.ToString()).ToArray())
            },
            ["pointer_cleanup"] = new JsonObject { ["left_button_released"] = true }
        };

        if (blockedResult is not null)
        {
            blockedResult["cleanup"] = cleanup;
            return blockedResult;
        }

        var finalTarget = targetMatch!;
        var finalTargetPointResult = ClickPoint(finalTarget.Element);
        if (finalTargetPointResult.Blocked is not null)
        {
            finalTargetPointResult.Blocked["cleanup"] = cleanup.DeepClone();
            return finalTargetPointResult.Blocked;
        }
        var finalTargetPoint = finalTargetPointResult.Point;
        var routePoints = new JsonArray
        {
            DragPointJson(sourcePoint),
            DragPointJson(sourceThresholdPoint),
            DragPointJson(finalTargetPoint)
        };
        return new JsonObject
        {
            ["status"] = "PASS",
            ["dragged"] = true,
            ["path_points"] = routePoints.DeepClone(),
            ["final_pointer"] = DragPointJson(finalTargetPoint),
            ["drop_ensure_visible_result"] = targetEnsureVisibleResult?.DeepClone(),
            ["cleanup"] = cleanup.DeepClone(),
            ["route_evidence"] = new JsonObject
            {
                ["source_bounds"] = sourceBounds,
                ["target_bounds"] = SafeRect(finalTarget.Element),
                ["source_identity"] = RowIdentity(sourceMatch.Row),
                ["target_identity"] = RowIdentity(finalTarget.Row),
                ["move_points"] = routePoints.DeepClone(),
                ["actual_drop_point"] = actualDropPoint is null ? null : DragPointJson(actualDropPoint.Value),
                ["pre_release_target_bounds"] = preReleaseTargetBounds?.DeepClone(),
                ["resolved_direction"] = resolvedTargetDirection.ToString().ToLowerInvariant(),
                ["drop_point_strategy"] = dropPointStrategy,
                ["edge_scan_attempts"] = edgeScanAttempts.DeepClone(),
                ["stabilization_attempts"] = stabilizationAttempts.DeepClone(),
                ["final_pointer"] = DragPointJson(finalTargetPoint),
                ["target_ensure_visible_result"] = targetEnsureVisibleResult?.DeepClone(),
                ["source_anchor_preserved"] = true
            },
            ["duration_ms"] = stopwatch.ElapsedMilliseconds
        };
    }

    private static Point DragThresholdPoint(JsonObject sourceBounds, Point sourcePoint)
    {
        var top = sourceBounds["y"]?.GetValue<int>() ?? sourcePoint.Y;
        var height = sourceBounds["height"]?.GetValue<int>() ?? 0;
        var bottom = top + Math.Max(1, height) - 1;
        var targetY = Math.Min(bottom, sourcePoint.Y + RowDragThresholdPixels);
        if (targetY == sourcePoint.Y && sourcePoint.Y > top)
            targetY = Math.Max(top, sourcePoint.Y - RowDragThresholdPixels);
        return new Point(sourcePoint.X, targetY);
    }

    private static JsonObject DragPointJson(Point point)
    {
        return new JsonObject
        {
            ["x"] = point.X,
            ["y"] = point.Y
        };
    }

    private enum DragScrollDirection
    {
        Up,
        Down
    }

    private static DragScrollDirection RowDragDirection(JsonObject sourceRow, int? targetRowIndex)
    {
        var sourceRowIndex = sourceRow["row_index"]?.GetValue<int?>();
        if (sourceRowIndex is not null && targetRowIndex is not null && targetRowIndex.Value < sourceRowIndex.Value)
            return DragScrollDirection.Up;
        return DragScrollDirection.Down;
    }

    private static RowSearchResult ScanForRowWithHeldDragNearEdge(
        AutomationElement grid,
        JsonObject gridBounds,
        UIA3Automation automation,
        List<string> columns,
        List<string> headers,
        int? rowIndex,
        string? rowKey,
        DragScrollDirection direction,
        int maxScrolls,
        int holdMs)
    {
        var attempts = new JsonArray();
        var edgePoint = EdgeScrollPoint(SafeRect(grid), direction);
        ClickCommands.MoveCursor(edgePoint.X, edgePoint.Y);
        Thread.Sleep(RowDragThresholdMoveSettleMs);

        var phase = direction == DragScrollDirection.Down
            ? "held_drag_edge_down"
            : "held_drag_edge_up";

        for (var step = 0; step <= maxScrolls; step++)
        {
            var result = SearchCurrentRows(
                grid,
                automation,
                columns,
                headers,
                rowIndex,
                rowKey,
                phase,
                step);
            AppendAttempts(attempts, result.Attempts);
            if (result.Blocked is not null)
                return new RowSearchResult(null, result.Blocked, attempts);
            if (result.Match is not null)
            {
                var liveRow = RefreshRowBounds(result.Match);
                if (TargetDropPoint(liveRow, gridBounds, direction) is not null)
                    return new RowSearchResult(result.Match, null, attempts);
            }
            if (step >= maxScrolls)
                break;
            PulseHeldDragPoint(edgePoint, holdMs);
        }

        return new RowSearchResult(null, null, attempts);
    }

    private static RowSearchResult ScanForRowWithHeldDrag(
        AutomationElement grid,
        JsonObject gridBounds,
        UIA3Automation automation,
        List<string> columns,
        List<string> headers,
        int? rowIndex,
        string? rowKey,
        DragScrollDirection preferredDirection,
        int maxScrolls,
        int holdMs,
        out DragScrollDirection resolvedDirection)
    {
        resolvedDirection = preferredDirection;
        var primary = ScanForRowWithHeldDragNearEdge(
            grid,
            gridBounds,
            automation,
            columns,
            headers,
            rowIndex,
            rowKey,
            preferredDirection,
            maxScrolls,
            holdMs);
        if (primary.Blocked is not null || primary.Match is not null)
            return primary;

        if (rowIndex is not null || string.IsNullOrWhiteSpace(rowKey))
            return primary;

        var secondary = ScanForRowWithHeldDragNearEdge(
            grid,
            gridBounds,
            automation,
            columns,
            headers,
            rowIndex,
            rowKey,
            ReverseDragScrollDirection(preferredDirection),
            maxScrolls,
            holdMs);
        resolvedDirection = ReverseDragScrollDirection(preferredDirection);
        AppendAttempts(primary.Attempts, secondary.Attempts);
        if (secondary.Blocked is not null)
            return new RowSearchResult(null, secondary.Blocked, primary.Attempts);
        if (secondary.Match is not null)
            return new RowSearchResult(secondary.Match, null, primary.Attempts);
        return new RowSearchResult(null, null, primary.Attempts);
    }

    private static Point EdgeScrollPoint(JsonObject gridBounds, DragScrollDirection direction)
    {
        var x = gridBounds["x"]?.GetValue<int>() ?? 0;
        var y = gridBounds["y"]?.GetValue<int>() ?? 0;
        var width = Math.Max(1, gridBounds["width"]?.GetValue<int>() ?? 1);
        var height = Math.Max(1, gridBounds["height"]?.GetValue<int>() ?? 1);
        var centerX = x + (width / 2);
        var ratio = direction == DragScrollDirection.Down
            ? RowDragEdgeScrollDownRatio
            : RowDragEdgeScrollUpRatio;
        var edgeY = y + (int)Math.Round((height - 1) * ratio);
        return new Point(centerX, edgeY);
    }

    private static DragScrollDirection ReverseDragScrollDirection(DragScrollDirection direction)
    {
        return direction == DragScrollDirection.Down
            ? DragScrollDirection.Up
            : DragScrollDirection.Down;
    }

    private static Point NeutralViewportPoint(JsonObject gridBounds)
    {
        var x = gridBounds["x"]?.GetValue<int>() ?? 0;
        var y = gridBounds["y"]?.GetValue<int>() ?? 0;
        var width = Math.Max(1, gridBounds["width"]?.GetValue<int>() ?? 1);
        var height = Math.Max(1, gridBounds["height"]?.GetValue<int>() ?? 1);
        return new Point(
            x + (width / 2),
            y + (height / 2));
    }

    private static RowSearchResult StabilizeHeldDragTarget(
        AutomationElement grid,
        JsonObject gridBounds,
        UIA3Automation automation,
        List<string> columns,
        List<string> headers,
        int? rowIndex,
        string? rowKey,
        DragScrollDirection direction,
        int settleMs)
    {
        var attempts = new JsonArray();
        var neutralPoint = NeutralViewportPoint(SafeRect(grid));
        ClickCommands.MoveCursor(neutralPoint.X, neutralPoint.Y);

        string? previousSignature = null;
        for (var attempt = 0; attempt < 5; attempt++)
        {
            Thread.Sleep(settleMs);
            var result = SearchCurrentRows(
                grid,
                automation,
                columns,
                headers,
                rowIndex,
                rowKey,
                "held_drag_target_stabilized",
                attempt);
            AppendAttempts(attempts, result.Attempts);
            if (result.Blocked is not null)
                return new RowSearchResult(null, result.Blocked, attempts);
            if (result.Match is null)
                return new RowSearchResult(null, null, attempts);

            var liveRow = RefreshRowBounds(result.Match);
            var stableDropPoint = TargetDropPoint(
                liveRow,
                gridBounds,
                direction);
            var signature = RowBoundsSignature(liveRow);
            if (
                stableDropPoint is not null
                && !string.IsNullOrWhiteSpace(signature)
                && signature == previousSignature
            )
                return new RowSearchResult(result.Match, null, attempts);
            previousSignature = signature;
        }

        var final = SearchCurrentRows(
            grid,
            automation,
            columns,
            headers,
            rowIndex,
            rowKey,
            "held_drag_target_stabilized_final");
        AppendAttempts(attempts, final.Attempts);
        if (final.Blocked is not null)
            return new RowSearchResult(null, final.Blocked, attempts);
        if (final.Match is null)
            return new RowSearchResult(null, null, attempts);
        return new RowSearchResult(final.Match, null, attempts);
    }

    private static JsonObject RefreshRowBounds(RowMatch match)
    {
        var row = match.Row.DeepClone() as JsonObject ?? new JsonObject();
        row["bounds"] = SafeRect(match.Element);
        return row;
    }

    private static Point? TargetDropPoint(
        JsonObject row,
        JsonObject gridBounds,
        DragScrollDirection direction,
        bool allowFallback = false)
    {
        if (row["bounds"] is not JsonObject bounds)
            return null;

        var x = bounds["x"]?.GetValue<int>() ?? 0;
        var y = bounds["y"]?.GetValue<int>() ?? 0;
        var width = Math.Max(1, bounds["width"]?.GetValue<int>() ?? 1);
        var height = Math.Max(1, bounds["height"]?.GetValue<int>() ?? 1);
        var dropX = x + (width / 2);
        var gridY = gridBounds["y"]?.GetValue<int>() ?? y;
        var gridHeight = Math.Max(1, gridBounds["height"]?.GetValue<int>() ?? height);
        var neutralTop = gridY + (int)Math.Round(gridHeight * RowDragNeutralBandTopRatio);
        var neutralBottom = gridY + (int)Math.Round(gridHeight * RowDragNeutralBandBottomRatio);
        var rowTop = y;
        var rowBottom = y + Math.Max(1, height) - 1;
        var safeTop = Math.Max(rowTop, neutralTop);
        var safeBottom = Math.Min(rowBottom, neutralBottom);
        if (safeBottom < safeTop)
        {
            if (!allowFallback)
                return null;

            var inset = Math.Max(3, Math.Min(8, height / 4));
            var fallbackY = rowBottom < neutralTop
                ? Math.Max(rowTop, rowBottom - inset)
                : Math.Min(rowBottom, rowTop + inset);
            return new Point(dropX, fallbackY);
        }

        var overlapHeight = safeBottom - safeTop;
        if (overlapHeight < 4)
        {
            var inset = Math.Max(3, Math.Min(8, height / 4));
            var edgeSafeY = direction == DragScrollDirection.Down
                ? Math.Min(rowBottom, safeTop + inset)
                : Math.Max(rowTop, safeBottom - inset);
            return new Point(dropX, edgeSafeY);
        }

        var dropY = safeTop + (overlapHeight / 2);
        return new Point(dropX, dropY);
    }

    private static JsonObject DropBandDiagnostics(JsonObject row, JsonObject gridBounds)
    {
        var output = new JsonObject();
        if (row["bounds"] is not JsonObject bounds)
            return output;

        var y = bounds["y"]?.GetValue<int>() ?? 0;
        var height = Math.Max(1, bounds["height"]?.GetValue<int>() ?? 1);
        var gridY = gridBounds["y"]?.GetValue<int>() ?? y;
        var gridHeight = Math.Max(1, gridBounds["height"]?.GetValue<int>() ?? height);
        var neutralTop = gridY + (int)Math.Round(gridHeight * RowDragNeutralBandTopRatio);
        var neutralBottom = gridY + (int)Math.Round(gridHeight * RowDragNeutralBandBottomRatio);
        var rowTop = y;
        var rowBottom = y + Math.Max(1, height) - 1;
        var safeTop = Math.Max(rowTop, neutralTop);
        var safeBottom = Math.Min(rowBottom, neutralBottom);

        output["grid_top"] = gridY;
        output["grid_bottom"] = gridY + gridHeight - 1;
        output["neutral_top"] = neutralTop;
        output["neutral_bottom"] = neutralBottom;
        output["row_top"] = rowTop;
        output["row_bottom"] = rowBottom;
        output["safe_top"] = safeTop;
        output["safe_bottom"] = safeBottom;
        output["overlap_height"] = safeBottom - safeTop;
        return output;
    }

    private static string RowBoundsSignature(JsonObject row)
    {
        if (row["bounds"] is not JsonObject bounds)
            return string.Empty;
        return string.Join(
            "|",
            row["row_index"]?.GetValue<int?>(),
            bounds["x"]?.GetValue<int?>(),
            bounds["y"]?.GetValue<int?>(),
            bounds["width"]?.GetValue<int?>(),
            bounds["height"]?.GetValue<int?>());
    }

    private static void PulseHeldDragPoint(Point point, int holdMs)
    {
        var elapsedMs = 0;
        var offset = 1;
        while (elapsedMs < holdMs)
        {
            var sleepMs = Math.Min(RowDragEdgeHoldPulseMs, holdMs - elapsedMs);
            Thread.Sleep(sleepMs);
            elapsedMs += sleepMs;
            if (elapsedMs >= holdMs)
                break;

            ClickCommands.MoveCursor(point.X + offset, point.Y);
            offset = -offset;
        }

        ClickCommands.MoveCursor(point.X, point.Y);
    }
}
