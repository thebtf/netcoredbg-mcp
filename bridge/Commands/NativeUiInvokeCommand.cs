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
    private readonly SelectionMode _selectionMode;
    private int _commitStarted;

    private enum SelectionMode { Legacy, StrictId, Observed }

    private NativeUiInvokeCommand(
        Process process, string processIdentity, IntPtr hwnd, string automationId, UIA3Automation automation,
        int[] rootRuntimeId, int[] elementRuntimeId, SelectionMode selectionMode)
    {
        _process = process;
        _processIdentity = processIdentity;
        _hwnd = hwnd;
        _automationId = automationId;
        _automation = automation;
        _rootRuntimeId = rootRuntimeId;
        _elementRuntimeId = elementRuntimeId;
        _selectionMode = selectionMode;
    }

    internal static JsonObject Prepare(
        JsonObject request, int boundProcessId, UIA3Automation automation, out NativeUiInvokeCommand? command)
    {
        command = null;
        var hasExpected = request.TryGetPropertyValue("expectedTarget", out var expectedNode);
        var operation = ReadString(request, "operation");
        var observed = operation == "prepare_observed";
        if (request.Count != (hasExpected ? 6 : 5) ||
            request.Any(property => property.Key is not ("operation" or "processId" or "processIdentity" or "hwnd" or "automationId" or "expectedTarget")) ||
            (hasExpected && !IsExpectation(expectedNode)) ||
            operation is not ("prepare" or "prepare_observed") || (observed && !hasExpected) ||
            request["processId"] is not JsonValue pidValue || !pidValue.TryGetValue<int>(out var pid) || pid != boundProcessId ||
            ReadString(request, "processIdentity") is not { Length: > 0 } identity ||
            request["hwnd"] is not JsonValue hwndValue || !hwndValue.TryGetValue<long>(out var hwnd) || hwnd == 0 ||
            ReadString(request, "automationId") is not { } automationId || !ElementCommands.IsValidGuardedSelectorText(automationId))
        {
            return Refused("INVALID_TOOL_ARGUMENTS");
        }
        var selectionMode = observed ? SelectionMode.Observed : hasExpected ? SelectionMode.StrictId : SelectionMode.Legacy;

        Process? process = null;
        try
        {
            process = Process.GetProcessById(pid);
            // Keep the actual process handle open, not merely its reusable numeric PID.
            _ = process.SafeHandle;
            var handle = new IntPtr(hwnd);
            if (!IsTargetCurrent(process, identity, handle))
                return Refused(hasExpected ? "UI_INVOKE_TARGET_CHANGED" : "UI_INVOKE_TARGET_UNAVAILABLE");
            var root = automation.FromHandle(handle);
            if (!IsRootBound(root, pid, handle))
                return Refused(hasExpected ? "UI_INVOKE_TARGET_CHANGED" : "UI_INVOKE_TARGET_UNAVAILABLE");
            var expected = expectedNode as JsonObject;
            int[]? rootId = null;
            if (expected is not null)
            {
                rootId = ReadActionRuntimeId(root, selectionMode);
                if (ReadString(expected, "processIdentity") != identity ||
                    ReadString(expected, "hwnd") != hwnd.ToString(CultureInfo.InvariantCulture) ||
                    !RuntimeIdMatches(expected["rootRuntimeId"], rootId))
                    return Refused("UI_INVOKE_TARGET_CHANGED");
            }
            var expectedElementId = observed
                ? ((JsonArray)expected!["elementRuntimeId"]!).Select(value => value!.GetValue<int>()).ToArray()
                : null;
            var resolution = Resolve(root, automationId, pid, selectionMode, expectedElementId);
            if (resolution.Outcome != GuardedSelectorOutcome.Unique)
                return SelectorRefusal(resolution.Outcome, selectionMode);
            var element = resolution.Element!;
            if (selectionMode != SelectionMode.Legacy && !HasExpectedAutomationId(element, automationId))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            int[]? elementId = null;
            if (expected is not null)
            {
                elementId = ReadActionRuntimeId(element, selectionMode);
                if (!RuntimeIdMatches(expected["elementRuntimeId"], elementId))
                    return Refused("UI_INVOKE_TARGET_CHANGED");
            }
            if (!element.Patterns.Invoke.TryGetPattern(out _))
                return Refused("UI_INVOKE_PATTERN_UNSUPPORTED");
            rootId ??= ReadActionRuntimeId(root, selectionMode);
            elementId ??= ReadActionRuntimeId(element, selectionMode);
            if ((selectionMode != SelectionMode.Legacy &&
                    (!HasExpectedAutomationId(element, automationId) || !IsRootBound(root, pid, handle) ||
                     !ReadActionRuntimeId(root, selectionMode).SequenceEqual(rootId) ||
                     !ReadActionRuntimeId(element, selectionMode).SequenceEqual(elementId))) ||
                !IsElementBound(element, pid, handle, automationId) || !IsTargetCurrent(process, identity, handle))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            command = new NativeUiInvokeCommand(process, identity, handle, automationId, automation, rootId, elementId, selectionMode);
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
        if (Interlocked.Exchange(ref _commitStarted, 1) != 0)
            return new JsonObject { ["status"] = "unknown" };
        try
        {
            if (!IsTargetCurrent(_process, _processIdentity, _hwnd))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            var root = _automation.FromHandle(_hwnd);
            if (!IsRootBound(root, _process.Id, _hwnd) || !ReadActionRuntimeId(root, _selectionMode).SequenceEqual(_rootRuntimeId))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            var resolution = Resolve(root, _automationId, _process.Id, _selectionMode, _elementRuntimeId);
            if (resolution.Outcome != GuardedSelectorOutcome.Unique)
                return SelectorRefusal(resolution.Outcome, _selectionMode);
            var element = resolution.Element!;
            if (!ReadActionRuntimeId(element, _selectionMode).SequenceEqual(_elementRuntimeId) ||
                (_selectionMode != SelectionMode.Legacy && !HasExpectedAutomationId(element, _automationId)))
                return Refused("UI_INVOKE_TARGET_CHANGED");
            if (!element.Patterns.Invoke.TryGetPattern(out var pattern))
                return Refused("UI_INVOKE_PATTERN_UNSUPPORTED");
            if ((_selectionMode != SelectionMode.Legacy &&
                    (!HasExpectedAutomationId(element, _automationId) ||
                     !ReadActionRuntimeId(root, _selectionMode).SequenceEqual(_rootRuntimeId) ||
                     !ReadActionRuntimeId(element, _selectionMode).SequenceEqual(_elementRuntimeId))) ||
                !IsElementBound(element, _process.Id, _hwnd, _automationId) ||
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

    private static GuardedSelectorResolution Resolve(
        AutomationElement root, string automationId, int pid, SelectionMode mode, int[]? expectedRuntimeId)
    {
        if (mode == SelectionMode.Legacy)
            return ElementCommands.ResolveUniqueBoundElement(
                root, new JsonObject { ["automationId"] = automationId }, pid, MaximumNodes, requireReadableAutomationId: true);

        var walker = root.Automation.TreeWalkerFactory.GetRawViewWalker();
        var pending = new Queue<AutomationElement>();
        pending.Enqueue(root);
        var observedSelection = mode == SelectionMode.Observed ? new ObservedIdentitySelection(pid, expectedRuntimeId!) : default;
        AutomationElement? match = null;
        var discovered = 1;
        var unreadableId = false;
        while (pending.Count != 0)
        {
            var current = pending.Dequeue();
            int? processId = current.Properties.ProcessId.TryGetValue(out var observedPid) ? observedPid : null;

            bool matches;
            if (mode == SelectionMode.Observed)
            {
                if (!observedSelection.TryVisit(processId, TryReadSelectionRuntimeId(current), out matches))
                    return observedSelection.Complete(traversalComplete: false) == GuardedSelectorOutcome.Ambiguous
                        ? Ambiguous() : Unreadable(match);
            }
            else if (processId != pid)
                return Unreadable(match);
            else if (TryReadAutomationId(current, out var id) && NativeUiWindowTreeCommand.FitsText(id!, 256))
            {
                matches = StringComparer.Ordinal.Equals(id, automationId);
            }
            else
            {
                // Unknown IDs cannot prove uniqueness, but do not erase two known equal IDs.
                unreadableId = true;
                matches = false;
            }
            if (matches)
            {
                if (match is not null)
                    return Ambiguous();
                match = current;
            }

            // Enumerate incrementally; neither a wide nor a deep provider tree is materialized unboundedly.
            var child = walker.GetFirstChild(current);
            while (child is not null)
            {
                if (discovered == MaximumNodes)
                    return Unreadable(match);
                pending.Enqueue(child);
                discovered++;
                child = walker.GetNextSibling(child);
            }
        }
        if (unreadableId)
            return Unreadable(match);
        var outcome = mode == SelectionMode.Observed
            ? observedSelection.Complete(traversalComplete: true)
            : match is null ? GuardedSelectorOutcome.Missing : GuardedSelectorOutcome.Unique;
        return new GuardedSelectorResolution(outcome, match, match is null ? 0 : 1, null);
    }

    private static GuardedSelectorResolution Unreadable(AutomationElement? match) =>
        new(GuardedSelectorOutcome.Unobservable, null, match is null ? 0 : 1, "The complete bounded selection could not be read.");

    private static GuardedSelectorResolution Ambiguous() =>
        new(GuardedSelectorOutcome.Ambiguous, null, 2, "The requested selection is not unique.");

    private static bool TryReadAutomationId(AutomationElement element, out string? id)
    {
        id = null;
        try { return element.Properties.AutomationId.TryGetValue(out id) && id is not null; }
        catch (Exception) { return false; }
    }

    private static bool HasExpectedAutomationId(AutomationElement element, string expectedId)
    {
        if (!TryReadAutomationId(element, out var id))
            throw new InvalidOperationException("The selected element's AutomationId is unreadable.");
        return StringComparer.Ordinal.Equals(id, expectedId);
    }

    private static int[]? TryReadSelectionRuntimeId(AutomationElement element)
    {
        try { return ReadRuntimeId(element); }
        catch (Exception) { return null; }
    }

    internal struct ObservedIdentitySelection
    {
        private readonly int _pid;
        private readonly int[] _expectedRuntimeId;
        private int _visited;
        private bool _matched;
        private GuardedSelectorOutcome? _refusal;

        internal ObservedIdentitySelection(int pid, int[] expectedRuntimeId)
        {
            _pid = pid;
            _expectedRuntimeId = expectedRuntimeId;
            _visited = 0;
            _matched = false;
            _refusal = null;
        }

        internal bool TryVisit(int? processId, int[]? runtimeId, out bool matches)
        {
            matches = false;
            if (_refusal.HasValue)
                return false;
            if (++_visited > MaximumNodes || processId != _pid || runtimeId is not { Length: >= 1 and <= 64 })
            {
                _refusal = GuardedSelectorOutcome.Unobservable;
                return false;
            }
            var isMatch = runtimeId.AsSpan().SequenceEqual(_expectedRuntimeId);
            if (isMatch && _matched)
            {
                _refusal = GuardedSelectorOutcome.Ambiguous;
                return false;
            }
            matches = isMatch;
            _matched |= matches;
            return true;
        }

        internal GuardedSelectorOutcome Complete(bool traversalComplete) => _refusal ??
            (!traversalComplete ? GuardedSelectorOutcome.Unobservable :
             _matched ? GuardedSelectorOutcome.Unique : GuardedSelectorOutcome.Missing);
    }


    private static bool IsExpectation(JsonNode? node) => node is JsonObject expected && expected.Count == 4 &&
        expected.All(property => property.Key is "processIdentity" or "hwnd" or "rootRuntimeId" or "elementRuntimeId") &&
        !string.IsNullOrWhiteSpace(ReadString(expected, "processIdentity")) && !string.IsNullOrWhiteSpace(ReadString(expected, "hwnd")) &&
        IsRuntimeId(expected["rootRuntimeId"]) && IsRuntimeId(expected["elementRuntimeId"]);

    private static bool IsRuntimeId(JsonNode? node) => node is JsonArray { Count: >= 1 and <= 64 } values &&
        values.All(value => value is JsonValue number && number.TryGetValue<int>(out _));

    private static bool RuntimeIdMatches(JsonNode? node, int[] runtimeId) => node is JsonArray values &&
        values.Count == runtimeId.Length && values.Select(value => value!.GetValue<int>()).SequenceEqual(runtimeId);

    internal static bool IsRootBound(AutomationElement root, int pid, IntPtr hwnd) =>
        root.Properties.ProcessId.IsSupported && root.Properties.ProcessId.Value == pid &&
        root.Properties.NativeWindowHandle.IsSupported && root.Properties.NativeWindowHandle.Value == hwnd;

    private static bool IsElementBound(AutomationElement element, int pid, IntPtr hwnd, string automationId) =>
        element.Properties.ProcessId.IsSupported && element.Properties.ProcessId.Value == pid &&
        element.Properties.AutomationId.IsSupported &&
        StringComparer.Ordinal.Equals(element.Properties.AutomationId.Value, automationId) &&
        ElementCommands.TryResolveOwningTopLevelHwnd(element, new[] { hwnd }, out var actualHwnd) && actualHwnd == hwnd;

    internal static int[] ReadRuntimeId(AutomationElement element)
    {
        if (!element.Properties.RuntimeId.IsSupported || element.Properties.RuntimeId.Value is not { Length: >= 1 and <= 64 } runtimeId)
            throw new InvalidOperationException("UIA runtime identity is unavailable.");
        return runtimeId;
    }

    private static int[] ReadActionRuntimeId(AutomationElement element, SelectionMode mode)
    {
        if (mode != SelectionMode.Legacy)
            return ReadRuntimeId(element);
        if (!element.Properties.RuntimeId.IsSupported || element.Properties.RuntimeId.Value is not { Length: > 0 } runtimeId)
            throw new InvalidOperationException("UIA runtime identity is unavailable.");
        return runtimeId;
    }

    internal static bool IsTargetCurrent(Process process, string identity, IntPtr hwnd)
    {
        process.Refresh();
        return !process.HasExited &&
            StringComparer.Ordinal.Equals(identity, string.Concat(
                "process_", process.Id.ToString(CultureInfo.InvariantCulture), "_start_",
                process.StartTime.ToUniversalTime().Ticks.ToString(CultureInfo.InvariantCulture))) &&
            process.MainWindowHandle == hwnd &&
            ElementCommands.GetWindowThreadProcessId(hwnd, out var pid) != 0 && pid == (uint)process.Id;
    }

    private static JsonObject SelectorRefusal(GuardedSelectorOutcome outcome, SelectionMode mode) => Refused(outcome switch
    {
        GuardedSelectorOutcome.Missing => mode == SelectionMode.Legacy ? "ELEMENT_NOT_FOUND" : "UI_INVOKE_TARGET_CHANGED",
        GuardedSelectorOutcome.Ambiguous => "ELEMENT_AMBIGUOUS",
        _ => "UI_INVOKE_SELECTION_UNREADABLE",
    });

    private static JsonObject Refused(string code) => new() { ["status"] = "refused", ["code"] = code };

    private static string? ReadString(JsonObject source, string name) =>
        source[name] is JsonValue value && value.TryGetValue<string>(out var text) ? text : null;
}
