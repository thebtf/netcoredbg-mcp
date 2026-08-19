using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

internal static class NativeSceneEvidenceCommands
{
    private const string CaptureVisualEvidenceOperation = "capture_visual_evidence";
    private const string CaptureElementSnapshotOperation = "capture_element_snapshot";
    private const string CaptureNativeSceneOperation = "capture_native_scene";
    private const int MaximumPngBytes = 64 * 1024 * 1024;
    private const int MaximumSceneNodes = 4_096;
    private const int MaximumObservedTextLength = 256;

    internal static JsonObject Handle(JsonObject request, int boundProcessId, UIA3Automation automation)
    {
        if (request["operation"] is not JsonValue operationValue ||
            !operationValue.TryGetValue<string>(out var operation))
        {
            throw new InvalidDataException("Native scene evidence request is missing its operation.");
        }

        if (string.Equals(operation, CaptureVisualEvidenceOperation, StringComparison.Ordinal))
            return CaptureVisualEvidence(request, boundProcessId);

        if (!string.Equals(operation, CaptureElementSnapshotOperation, StringComparison.Ordinal) &&
            !string.Equals(operation, CaptureNativeSceneOperation, StringComparison.Ordinal))
        {
            throw new InvalidDataException("Native scene evidence operation is unsupported.");
        }

        var target = ReadBoundTarget(request, boundProcessId);
        var maximumNodes = ReadMaximumNodes(request);
        return CaptureGuardedUia(operation, request, target, maximumNodes, automation);
    }

    private static JsonObject CaptureVisualEvidence(JsonObject request, int boundProcessId)
    {
        if (request.Count != 3 ||
            request["processId"] is not JsonValue processIdValue ||
            !processIdValue.TryGetValue<int>(out var processId) ||
            request["processIdentity"] is not JsonValue processIdentityValue ||
            !processIdentityValue.TryGetValue<string>(out var processIdentity) ||
            string.IsNullOrWhiteSpace(processIdentity) ||
            processId != boundProcessId)
        {
            throw new InvalidDataException("Native scene evidence request is not authorized for the bound process.");
        }

        using var process = Process.GetProcessById(processId);
        process.Refresh();
        if (process.HasExited ||
            !string.Equals(processIdentity, CreateProcessIdentity(process), StringComparison.Ordinal))
        {
            throw new InvalidOperationException("Bound process identity is unavailable.");
        }

        var hwnd = process.MainWindowHandle;
        if (hwnd == IntPtr.Zero || GetWindowThreadProcessId(hwnd, out var hwndProcessId) == 0 || hwndProcessId != (uint)processId)
        {
            throw new InvalidOperationException("Bound process does not expose an owned top-level window.");
        }

        var capture = ScreenshotCommands.CaptureLosslessPng(hwnd);
        process.Refresh();
        if (process.HasExited ||
            !string.Equals(processIdentity, CreateProcessIdentity(process), StringComparison.Ordinal) ||
            GetWindowThreadProcessId(hwnd, out hwndProcessId) == 0 ||
            hwndProcessId != (uint)processId)
        {
            throw new InvalidOperationException("Bound process or window changed during evidence capture.");
        }
        if (capture.Bytes.Length > MaximumPngBytes)
        {
            throw new InvalidDataException("Lossless PNG exceeds the bounded bridge response capacity.");
        }

        return new JsonObject
        {
            ["pngBase64"] = Convert.ToBase64String(capture.Bytes),
            ["byteLength"] = capture.Bytes.Length,
            ["sha256"] = Convert.ToHexString(SHA256.HashData(capture.Bytes)).ToLowerInvariant(),
            ["provenance"] = new JsonObject
            {
                ["processId"] = processId,
                ["processIdentity"] = processIdentity,
                ["hwnd"] = capture.Hwnd,
                ["width"] = capture.Width,
                ["height"] = capture.Height,
                ["clientRect"] = new JsonObject
                {
                    ["left"] = capture.ClientLeft,
                    ["top"] = capture.ClientTop,
                    ["right"] = capture.ClientRight,
                    ["bottom"] = capture.ClientBottom,
                },
                ["dpi"] = capture.Dpi,
                ["captureMethod"] = "PrintWindow",
                ["printWindowFlags"] = 2,
            },
        };
    }

    private static NativeSceneTarget ReadBoundTarget(JsonObject request, int boundProcessId)
    {
        if (request["processId"] is not JsonValue processIdValue ||
            !processIdValue.TryGetValue<int>(out var processId) ||
            processId != boundProcessId ||
            request["processIdentity"] is not JsonValue processIdentityValue ||
            !processIdentityValue.TryGetValue<string>(out var processIdentity) ||
            string.IsNullOrWhiteSpace(processIdentity) ||
            request["hwnd"] is not JsonValue hwndValue ||
            !hwndValue.TryGetValue<long>(out var hwndValue64) ||
            hwndValue64 == 0)
        {
            throw new InvalidDataException("Guarded UIA capture requires the bound process identity and an explicit HWND.");
        }

        return new NativeSceneTarget(processId, processIdentity, new IntPtr(hwndValue64));
    }

    private static int ReadMaximumNodes(JsonObject request)
    {
        if (!request.TryGetPropertyValue("maxNodes", out var node) || node is null)
            return MaximumSceneNodes;
        if (node is not JsonValue value || !value.TryGetValue<int>(out var maximumNodes) ||
            maximumNodes is < 1 or > MaximumSceneNodes)
        {
            throw new InvalidDataException($"maxNodes must be an integer from 1 through {MaximumSceneNodes}.");
        }

        return maximumNodes;
    }

    private static JsonObject CaptureGuardedUia(
        string operation,
        JsonObject request,
        NativeSceneTarget target,
        int maximumNodes,
        UIA3Automation automation)
    {
        try
        {
            using var process = Process.GetProcessById(target.ProcessId);
            if (!IsBoundTargetCurrent(process, target))
                return Unobservable(operation, "BOUND_TARGET_UNAVAILABLE", null, null, null);

            var before = TryReadGuard(automation, target);
            var beforeSnapshot = before.Snapshot;
            var beforeRoot = before.Root;
            if (beforeSnapshot is null || beforeRoot is null)
                return Unobservable(operation, before.IssueCode!, null, null, null);

            GuardedSelectorResolution? selectorResolution = null;
            SceneCaptureResult sceneCapture;
            if (string.Equals(operation, CaptureElementSnapshotOperation, StringComparison.Ordinal))
            {
                var selector = ReadRequiredSelector(request);
                selectorResolution = ElementCommands.ResolveUniqueBoundElement(
                    beforeRoot, selector, target.ProcessId, MaximumSceneNodes);
                sceneCapture = selectorResolution.Outcome == GuardedSelectorOutcome.Unique
                    ? SceneCaptureResult.Success(
                        BuildNode(selectorResolution.Element!, "n0", null, beforeSnapshot.Value.Dpi),
                        "n0")
                    : SceneCaptureResult.Failure("SELECTOR_" + selectorResolution.Outcome.ToString().ToUpperInvariant());
            }
            else
            {
                var sceneRoot = beforeRoot;
                if (TryReadSelector(request, out var selector))
                {
                    selectorResolution = ElementCommands.ResolveUniqueBoundElement(
                        sceneRoot, selector!, target.ProcessId, MaximumSceneNodes);
                    if (selectorResolution.Outcome != GuardedSelectorOutcome.Unique)
                    {
                        sceneCapture = SceneCaptureResult.Failure(
                            "SELECTOR_" + selectorResolution.Outcome.ToString().ToUpperInvariant());
                    }
                    else
                    {
                        sceneCapture = CaptureBoundScene(
                            selectorResolution.Element!, target.ProcessId, beforeSnapshot.Value.Dpi, maximumNodes);
                    }
                }
                else
                {
                    sceneCapture = CaptureBoundScene(sceneRoot, target.ProcessId, beforeSnapshot.Value.Dpi, maximumNodes);
                }
            }

            var after = TryReadGuard(automation, target);
            var selectorPayload = selectorResolution is null ? null : ToSelectorPayload(selectorResolution);
            if (after.Snapshot is null)
                return Unobservable(operation, after.IssueCode!, beforeSnapshot, null, selectorPayload);
            if (!IsBoundTargetCurrent(process, target))
                return Unobservable(operation, "BOUND_TARGET_CHANGED", beforeSnapshot, after.Snapshot, selectorPayload);
            if (beforeSnapshot != after.Snapshot)
                return Unobservable(operation, "UIA_GUARDS_CHANGED", beforeSnapshot, after.Snapshot, selectorPayload);
            if (!sceneCapture.IsSuccess)
                return Unobservable(operation, sceneCapture.IssueCode!, beforeSnapshot, after.Snapshot, selectorPayload);

            return new JsonObject
            {
                ["kind"] = "uia_guarded_observation",
                ["operation"] = operation,
                ["qualification"] = "PARTIAL",
                ["authority"] = "uia_guarded",
                ["atomicity"] = "unproven",
                ["process"] = new JsonObject
                {
                    ["processId"] = target.ProcessId,
                    ["processIdentity"] = target.ProcessIdentity,
                    ["hwnd"] = target.Hwnd.ToInt64(),
                },
                ["guards"] = ToGuardsPayload(beforeSnapshot, after.Snapshot),
                ["stability"] = ToUnobservableGuardedStabilityPayload(),
                ["selector"] = selectorPayload,
                ["rootId"] = sceneCapture.RootId,
                ["nodes"] = sceneCapture.Nodes,
                ["issues"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["code"] = "ATOMICITY_UNPROVEN_UIA_GUARDED",
                        ["message"] = "UIA reads are independently timed and cannot prove an atomic framework scene.",
                    },
                },
            };
        }
        catch (Exception ex)
        {
            Program.Log($"Guarded UIA observation failed: {ex.GetType().Name}: {ex.Message}");
            return Unobservable(operation, "OBSERVER_UNAVAILABLE", null, null, null);
        }
    }

    private static JsonObject ReadRequiredSelector(JsonObject request)
    {
        if (!TryReadSelector(request, out var selector))
            throw new InvalidDataException("capture_element_snapshot requires a selector object.");
        return selector!;
    }

    private static bool TryReadSelector(JsonObject request, out JsonObject? selector)
    {
        selector = null;
        if (!request.TryGetPropertyValue("selector", out var node) || node is null)
            return false;
        selector = node as JsonObject
            ?? throw new InvalidDataException("selector must be an object.");
        return true;
    }

    private static GuardReadResult TryReadGuard(UIA3Automation automation, NativeSceneTarget target)
    {
        try
        {
            var root = automation.FromHandle(target.Hwnd);
            if (!TryReadProcessId(root, out var processId) || processId != target.ProcessId)
                return GuardReadResult.Failure("BOUND_WINDOW_UNAVAILABLE");
            if (!TryCreateVisualTreeFingerprint(root, target.ProcessId, out var fingerprint))
                return GuardReadResult.Failure("UIA_GUARD_UNUSABLE");
            if (!GetWindowRect(target.Hwnd, out var windowRect) ||
                !GetClientRect(target.Hwnd, out var clientRect) ||
                windowRect.Right <= windowRect.Left || windowRect.Bottom <= windowRect.Top ||
                clientRect.Right <= clientRect.Left || clientRect.Bottom <= clientRect.Top)
            {
                return GuardReadResult.Failure("WINDOW_GUARD_UNUSABLE");
            }

            var dpi = GetDpiForWindow(target.Hwnd);
            if (dpi == 0)
                return GuardReadResult.Failure("WINDOW_GUARD_UNUSABLE");

            return GuardReadResult.Success(root, new WindowGuardSnapshot(
                target.Hwnd.ToInt64(),
                windowRect.Left, windowRect.Top, windowRect.Right, windowRect.Bottom,
                clientRect.Left, clientRect.Top, clientRect.Right, clientRect.Bottom,
                checked((int)dpi),
                fingerprint));
        }
        catch (Exception ex)
        {
            Program.Log($"UIA guard read failed: {ex.GetType().Name}: {ex.Message}");
            return GuardReadResult.Failure("UIA_GUARD_UNUSABLE");
        }
    }

    private static bool TryCreateVisualTreeFingerprint(
        AutomationElement root,
        int boundProcessId,
        out string fingerprint)
    {
        var queue = new Queue<AutomationElement>();
        queue.Enqueue(root);
        var visited = 0;
        var truncated = false;
        var builder = new StringBuilder();

        while (queue.Count > 0)
        {
            var element = queue.Dequeue();
            visited++;
            if (!TryReadProcessId(element, out var processId) || processId != boundProcessId)
            {
                fingerprint = string.Empty;
                return false;
            }

            AppendFingerprintFact(builder, ReadBoundedText(() => element.AutomationId).Value);
            AppendFingerprintFact(builder, ReadBoundedText(() => element.Name).Value);
            AppendFingerprintFact(builder, ReadBoundedText(() => element.ControlType.ToString()).Value);
            AppendFingerprintFact(builder, ReadBoundedText(() => element.ClassName).Value);
            AppendFingerprintFact(builder, SerializeRect(TryReadRect(element)));

            AutomationElement[] children;
            try
            {
                children = element.FindAllChildren();
            }
            catch
            {
                fingerprint = string.Empty;
                return false;
            }

            foreach (var child in children)
            {
                if (visited + queue.Count >= MaximumSceneNodes)
                {
                    truncated = true;
                    break;
                }

                queue.Enqueue(child);
            }
        }

        if (truncated)
        {
            fingerprint = string.Empty;
            return false;
        }

        fingerprint = Convert.ToHexString(SHA256.HashData(Encoding.UTF8.GetBytes(builder.ToString()))).ToLowerInvariant();
        return true;
    }

    private static void AppendFingerprintFact(StringBuilder builder, string? value)
    {
        var text = value ?? "<unavailable>";
        builder.Append(text.Length.ToString(CultureInfo.InvariantCulture));
        builder.Append(':');
        builder.Append(text);
        builder.Append('|');
    }

    private static SceneCaptureResult CaptureBoundScene(
        AutomationElement root,
        int boundProcessId,
        int dpi,
        int maximumNodes)
    {
        var queue = new Queue<(AutomationElement Element, string Id, string? ParentId)>();
        queue.Enqueue((root, "n0", null));
        var nodes = new JsonArray();
        var nextId = 1;
        var truncated = false;

        while (queue.Count > 0)
        {
            var (element, id, parentId) = queue.Dequeue();
            if (!TryReadProcessId(element, out var processId) || processId != boundProcessId)
                return SceneCaptureResult.Failure("BOUND_ELEMENT_UNAVAILABLE");

            nodes.Add(BuildNode(element, id, parentId, dpi));

            AutomationElement[] children;
            try
            {
                children = element.FindAllChildren();
            }
            catch
            {
                return SceneCaptureResult.Failure("UIA_TREE_UNAVAILABLE");
            }

            foreach (var child in children)
            {
                if (nodes.Count + queue.Count >= maximumNodes)
                {
                    truncated = true;
                    break;
                }

                queue.Enqueue((child, $"n{nextId++}", id));
            }
        }

        return truncated
            ? SceneCaptureResult.Failure("SCENE_NODE_BOUND_EXCEEDED")
            : SceneCaptureResult.Success(nodes, "n0");
    }

    private static JsonObject BuildNode(AutomationElement element, string id, string? parentId, int dpi)
    {
        var automationId = ReadBoundedText(() => element.AutomationId);
        var name = ReadBoundedText(() => element.Name);
        var controlType = ReadBoundedText(() => element.ControlType.ToString());
        var className = ReadBoundedText(() => element.ClassName);
        var physical = TryReadRect(element);
        return new JsonObject
        {
            ["id"] = id,
            ["parentId"] = parentId,
            ["identity"] = new JsonObject
            {
                ["automationId"] = automationId.Value,
                ["name"] = name.Value,
                ["controlType"] = controlType.Value,
                ["className"] = className.Value,
                ["nativeWindowHandle"] = ReadNativeWindowHandle(element),
                ["textTruncated"] = automationId.Truncated || name.Truncated || controlType.Truncated || className.Truncated,
            },
            ["accessibility"] = new JsonObject
            {
                ["isEnabled"] = TryReadBoolean(() => element.IsEnabled),
                ["isOffscreen"] = TryReadBoolean(() => element.IsOffscreen),
            },
            ["geometry"] = new JsonObject
            {
                ["physical"] = physical,
                ["logical"] = ToLogicalRect(physical, dpi),
                ["dpi"] = dpi,
            },
            ["transform"] = ReadTransform(element),
            ["clip"] = new JsonObject
            {
                ["status"] = "unobservable",
                ["reason"] = "UIA does not expose an effective clip region.",
            },
        };
    }

    private static JsonObject ReadTransform(AutomationElement element)
    {
        try
        {
            if (!element.Patterns.Transform.TryGetPattern(out var transform))
                return new JsonObject { ["supported"] = false };

            return new JsonObject
            {
                ["supported"] = true,
                ["canMove"] = transform.CanMove.ValueOrDefault,
                ["canResize"] = transform.CanResize.ValueOrDefault,
                ["canRotate"] = transform.CanRotate.ValueOrDefault,
            };
        }
        catch
        {
            return new JsonObject { ["supported"] = null };
        }
    }

    private static JsonObject? TryReadRect(AutomationElement element)
    {
        try
        {
            var rect = element.BoundingRectangle;
            return new JsonObject
            {
                ["x"] = (double)rect.X,
                ["y"] = (double)rect.Y,
                ["width"] = (double)rect.Width,
                ["height"] = (double)rect.Height,
            };
        }
        catch
        {
            return null;
        }
    }

    private static JsonObject? ToLogicalRect(JsonObject? physical, int dpi)
    {
        if (physical is null || dpi <= 0)
            return null;
        return new JsonObject
        {
            ["x"] = physical["x"]!.GetValue<double>() * 96.0 / dpi,
            ["y"] = physical["y"]!.GetValue<double>() * 96.0 / dpi,
            ["width"] = physical["width"]!.GetValue<double>() * 96.0 / dpi,
            ["height"] = physical["height"]!.GetValue<double>() * 96.0 / dpi,
        };
    }

    private static string SerializeRect(JsonObject? rect) => rect is null
        ? "<unavailable>"
        : string.Concat(
            rect["x"]!.GetValue<double>().ToString("R", CultureInfo.InvariantCulture), ",",
            rect["y"]!.GetValue<double>().ToString("R", CultureInfo.InvariantCulture), ",",
            rect["width"]!.GetValue<double>().ToString("R", CultureInfo.InvariantCulture), ",",
            rect["height"]!.GetValue<double>().ToString("R", CultureInfo.InvariantCulture));

    private static BoundedText ReadBoundedText(Func<string?> read)
    {
        try
        {
            var value = read();
            if (value is null)
                return new BoundedText(null, false);
            return value.Length <= MaximumObservedTextLength
                ? new BoundedText(value, false)
                : new BoundedText(value[..MaximumObservedTextLength], true);
        }
        catch
        {
            return new BoundedText(null, false);
        }
    }

    private static bool? TryReadBoolean(Func<bool> read)
    {
        try { return read(); }
        catch { return null; }
    }

    private static long? ReadNativeWindowHandle(AutomationElement element)
    {
        try
        {
            var handle = element.Properties.NativeWindowHandle.ValueOrDefault;
            return handle == IntPtr.Zero ? null : handle.ToInt64();
        }
        catch
        {
            return null;
        }
    }

    private static bool TryReadProcessId(AutomationElement element, out int processId)
    {
        try
        {
            if (element.Properties.ProcessId.IsSupported)
            {
                processId = element.Properties.ProcessId.Value;
                return processId > 0;
            }
        }
        catch
        {
            // Handled below.
        }

        processId = 0;
        return false;
    }

    private static bool IsBoundTargetCurrent(Process process, NativeSceneTarget target)
    {
        try
        {
            process.Refresh();
            return !process.HasExited &&
                   string.Equals(target.ProcessIdentity, CreateProcessIdentity(process), StringComparison.Ordinal) &&
                   GetWindowThreadProcessId(target.Hwnd, out var processId) != 0 &&
                   processId == (uint)target.ProcessId;
        }
        catch
        {
            return false;
        }
    }

    private static JsonObject Unobservable(
        string operation,
        string issueCode,
        WindowGuardSnapshot? before,
        WindowGuardSnapshot? after,
        JsonObject? selector)
    {
        return new JsonObject
        {
            ["kind"] = "uia_guarded_observation",
            ["operation"] = operation,
            ["qualification"] = "UNOBSERVABLE",
            ["authority"] = "uia_guarded",
            ["atomicity"] = "unproven",
            ["guards"] = ToGuardsPayload(before, after),
            ["selector"] = selector,
            ["issues"] = new JsonArray
            {
                new JsonObject { ["code"] = issueCode },
            },
        };
    }

    private static JsonObject ToGuardsPayload(WindowGuardSnapshot? before, WindowGuardSnapshot? after) => new()
    {
        ["before"] = before?.ToJson(),
        ["after"] = after?.ToJson(),
    };

    private static JsonObject ToUnobservableGuardedStabilityPayload() => new()
    {
        ["sceneEpoch"] = 0,
        ["conditions"] = new JsonObject
        {
            ["dispatcherIdle"] = new JsonObject { ["state"] = "unobservable" },
            ["stableLayout"] = new JsonObject { ["state"] = "unobservable" },
            ["animationState"] = new JsonObject { ["state"] = "unobservable" },
            ["windowGeometry"] = new JsonObject { ["state"] = "unobservable" },
            ["contextMaterialization"] = new JsonObject { ["state"] = "unobservable" },
            ["asyncLoadSettled"] = new JsonObject { ["state"] = "unobservable" },
        },
    };

    private static JsonObject ToSelectorPayload(GuardedSelectorResolution resolution) => new()
    {
        ["outcome"] = resolution.Outcome.ToString().ToLowerInvariant(),
        ["matchCount"] = resolution.MatchCount,
        ["detail"] = resolution.Detail,
    };

    private static string CreateProcessIdentity(Process process) => string.Concat(
        "process_",
        process.Id.ToString(CultureInfo.InvariantCulture),
        "_start_",
        process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture));

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(IntPtr window, out uint processId);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetWindowRect(IntPtr window, out RECT rect);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetClientRect(IntPtr window, out RECT rect);

    [DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetDpiForWindow(IntPtr window);

    [StructLayout(LayoutKind.Sequential)]
    private struct RECT
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    private readonly record struct NativeSceneTarget(int ProcessId, string ProcessIdentity, IntPtr Hwnd);

    private readonly record struct WindowGuardSnapshot(
        long Hwnd,
        int WindowLeft,
        int WindowTop,
        int WindowRight,
        int WindowBottom,
        int ClientLeft,
        int ClientTop,
        int ClientRight,
        int ClientBottom,
        int Dpi,
        string VisualTreeFingerprint)
    {
        internal JsonObject ToJson() => new()
        {
            ["hwnd"] = Hwnd,
            ["windowRect"] = new JsonObject
            {
                ["left"] = WindowLeft,
                ["top"] = WindowTop,
                ["right"] = WindowRight,
                ["bottom"] = WindowBottom,
            },
            ["clientRect"] = new JsonObject
            {
                ["left"] = ClientLeft,
                ["top"] = ClientTop,
                ["right"] = ClientRight,
                ["bottom"] = ClientBottom,
            },
            ["dpi"] = Dpi,
            ["visualTreeFingerprint"] = VisualTreeFingerprint,
        };
    }

    private sealed record GuardReadResult(
        AutomationElement? Root,
        WindowGuardSnapshot? Snapshot,
        string? IssueCode)
    {
        internal static GuardReadResult Success(AutomationElement root, WindowGuardSnapshot snapshot) =>
            new(root, snapshot, null);

        internal static GuardReadResult Failure(string issueCode) =>
            new(null, null, issueCode);
    }

    private sealed record SceneCaptureResult(JsonArray Nodes, string? RootId, string? IssueCode)
    {
        internal bool IsSuccess => IssueCode is null;

        internal static SceneCaptureResult Success(JsonObject node, string rootId) =>
            new(new JsonArray { node }, rootId, null);

        internal static SceneCaptureResult Success(JsonArray nodes, string rootId) =>
            new(nodes, rootId, null);

        internal static SceneCaptureResult Failure(string issueCode) =>
            new(new JsonArray(), null, issueCode);
    }

    private readonly record struct BoundedText(string? Value, bool Truncated);
}
