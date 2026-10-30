using System.Diagnostics;
using System.Globalization;
using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

internal sealed class NativeUiInvokeCommand : IDisposable
{
    private const int MaximumNodes = 4_096;
    private readonly Process _process;
    private readonly string _processIdentity;
    private readonly IntPtr _hwnd;
    private readonly string _automationId;
    private readonly UIA3Automation _automation;
    private readonly int[] _rootRuntimeId;
    private readonly int[] _elementRuntimeId;

    private NativeUiInvokeCommand(
        Process process, string processIdentity, IntPtr hwnd, string automationId, UIA3Automation automation,
        int[] rootRuntimeId, int[] elementRuntimeId)
    {
        _process = process;
        _processIdentity = processIdentity;
        _hwnd = hwnd;
        _automationId = automationId;
        _automation = automation;
        _rootRuntimeId = rootRuntimeId;
        _elementRuntimeId = elementRuntimeId;
    }

    internal static JsonObject Prepare(
        JsonObject request, int boundProcessId, UIA3Automation automation, out NativeUiInvokeCommand? command)
    {
        command = null;
        if (request.Count != 5 || ReadString(request, "operation") != "prepare" ||
            request["processId"] is not JsonValue pidValue || !pidValue.TryGetValue<int>(out var pid) || pid != boundProcessId ||
            ReadString(request, "processIdentity") is not { Length: > 0 } identity ||
            request["hwnd"] is not JsonValue hwndValue || !hwndValue.TryGetValue<long>(out var hwnd) || hwnd == 0 ||
            ReadString(request, "automationId") is not { } automationId || !ElementCommands.IsValidGuardedSelectorText(automationId))
        {
            return Refused("INVALID_TOOL_ARGUMENTS");
        }

        Process? process = null;
        try
        {
            process = Process.GetProcessById(pid);
            // Keep the actual process handle open, not merely its reusable numeric PID.
            _ = process.SafeHandle;
            var handle = new IntPtr(hwnd);
            if (!IsTargetCurrent(process, identity, handle))
                return Refused("UI_INVOKE_TARGET_UNAVAILABLE");
            var root = automation.FromHandle(handle);
            if (!IsRootBound(root, pid, handle))
                return Refused("UI_INVOKE_TARGET_UNAVAILABLE");
            var resolution = Resolve(root, automationId, pid);
            if (resolution.Outcome != GuardedSelectorOutcome.Unique)
                return SelectorRefusal(resolution.Outcome);
            var element = resolution.Element!;
            if (!element.Patterns.Invoke.TryGetPattern(out _))
                return Refused("UI_INVOKE_PATTERN_UNSUPPORTED");
            var rootId = ReadRuntimeId(root);
            var elementId = ReadRuntimeId(element);
            if (!IsElementBound(element, pid, handle, automationId) || !IsTargetCurrent(process, identity, handle))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            command = new NativeUiInvokeCommand(process, identity, handle, automationId, automation, rootId, elementId);
            process = null;
            return new JsonObject { ["status"] = "ready" };
        }
        catch (Exception)
        {
            return Refused("UI_INVOKE_SELECTION_UNREADABLE");
        }
        finally
        {
            process?.Dispose();
        }
    }

    internal JsonObject Invoke()
    {
        try
        {
            if (!IsTargetCurrent(_process, _processIdentity, _hwnd))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            var root = _automation.FromHandle(_hwnd);
            if (!IsRootBound(root, _process.Id, _hwnd) || !ReadRuntimeId(root).SequenceEqual(_rootRuntimeId))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            var resolution = Resolve(root, _automationId, _process.Id);
            if (resolution.Outcome != GuardedSelectorOutcome.Unique)
                return SelectorRefusal(resolution.Outcome);
            var element = resolution.Element!;
            if (!ReadRuntimeId(element).SequenceEqual(_elementRuntimeId))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            if (!element.Patterns.Invoke.TryGetPattern(out var pattern))
                return Refused("UI_INVOKE_PATTERN_UNSUPPORTED");
            if (!IsElementBound(element, _process.Id, _hwnd, _automationId) ||
                !IsRootBound(root, _process.Id, _hwnd) || !IsTargetCurrent(_process, _processIdentity, _hwnd))
            {
                return Refused("UI_INVOKE_TARGET_CHANGED");
            }

            // No physical input, input-idle wait, property reads, or retries after this call.
            try
            {
                pattern.Invoke();
                return new JsonObject { ["status"] = "succeeded" };
            }
            catch (Exception)
            {
                return new JsonObject { ["status"] = "unknown" };
            }
        }
        catch (Exception)
        {
            return Refused("UI_INVOKE_SELECTION_UNREADABLE");
        }
    }

    public void Dispose() => _process.Dispose();

    private static GuardedSelectorResolution Resolve(AutomationElement root, string automationId, int pid) =>
        ElementCommands.ResolveUniqueBoundElement(
            root, new JsonObject { ["automationId"] = automationId }, pid, MaximumNodes, requireReadableAutomationId: true);

    private static bool IsRootBound(AutomationElement root, int pid, IntPtr hwnd) =>
        root.Properties.ProcessId.IsSupported && root.Properties.ProcessId.Value == pid &&
        root.Properties.NativeWindowHandle.IsSupported && root.Properties.NativeWindowHandle.Value == hwnd;

    private static bool IsElementBound(AutomationElement element, int pid, IntPtr hwnd, string automationId) =>
        element.Properties.ProcessId.IsSupported && element.Properties.ProcessId.Value == pid &&
        element.Properties.AutomationId.IsSupported &&
        StringComparer.Ordinal.Equals(element.Properties.AutomationId.Value, automationId) &&
        ElementCommands.TryResolveOwningTopLevelHwnd(element, new[] { hwnd }, out var actualHwnd) && actualHwnd == hwnd;

    private static int[] ReadRuntimeId(AutomationElement element)
    {
        if (!element.Properties.RuntimeId.IsSupported || element.Properties.RuntimeId.Value is not { Length: > 0 } runtimeId)
            throw new InvalidOperationException("UIA runtime identity is unavailable.");
        return runtimeId;
    }

    private static bool IsTargetCurrent(Process process, string identity, IntPtr hwnd)
    {
        process.Refresh();
        return !process.HasExited &&
            StringComparer.Ordinal.Equals(identity, string.Concat(
                "process_", process.Id.ToString(CultureInfo.InvariantCulture), "_start_",
                process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture))) &&
            process.MainWindowHandle == hwnd &&
            ElementCommands.GetWindowThreadProcessId(hwnd, out var pid) != 0 && pid == (uint)process.Id;
    }

    private static JsonObject SelectorRefusal(GuardedSelectorOutcome outcome) => Refused(outcome switch
    {
        GuardedSelectorOutcome.Missing => "ELEMENT_NOT_FOUND",
        GuardedSelectorOutcome.Ambiguous => "ELEMENT_AMBIGUOUS",
        _ => "UI_INVOKE_SELECTION_UNREADABLE",
    });

    private static JsonObject Refused(string code) => new() { ["status"] = "refused", ["code"] = code };

    private static string? ReadString(JsonObject source, string name) =>
        source[name] is JsonValue value && value.TryGetValue<string>(out var text) ? text : null;
}
