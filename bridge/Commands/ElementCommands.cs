using System.IO;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Conditions;
using FlaUI.Core.Definitions;
using FlaUI.UIA3;

namespace FlaUIBridge.Commands;

public static class ElementCommands
{
    public static JsonNode Connect(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        var pid = @params?["pid"]?.GetValue<int>()
            ?? throw new ArgumentException("Missing required parameter: pid");
        var requestedStealth = @params?["stealth"]?.GetValue<bool>() ?? false;

        var desktop = automation.GetDesktop();
        var windows = desktop.FindAllChildren(
            new ConditionFactory(automation.PropertyLibrary)
                .ByProcessId(pid));

        if (windows.Length == 0)
            throw new InvalidOperationException($"No window found for process {pid}");

        var window = SelectPrimaryWindow(windows)
            ?? throw new InvalidOperationException(
                $"No window found for process {pid}: no usable top-level window yet");
        JsonRpcHandler.MainWindow = window;
        // Store pid independently so window enumeration still works after
        // set_active_window switches MainWindow to a dialog that later closes.
        JsonRpcHandler.ProcessId = pid;
        JsonRpcHandler.Stealth = requestedStealth;

        Program.Log($"Connected to window: {SafeString(() => window.Name)} (pid={pid})");

        return new JsonObject
        {
            ["connected"] = true,
            ["title"] = SafeString(() => window.Name)
        };
    }

    private static AutomationElement? SelectPrimaryWindow(AutomationElement[] windows)
    {
        return windows
            .Select(window => new
            {
                Window = window,
                Score = PrimaryWindowScore(window),
                StableKey = StableWindowKey(window)
            })
            .Where(candidate => candidate.Score > int.MinValue)
            .OrderByDescending(candidate => candidate.Score)
            .ThenBy(candidate => candidate.StableKey, StringComparer.Ordinal)
            .FirstOrDefault()
            ?.Window;
    }

    private static int PrimaryWindowScore(AutomationElement window)
    {
        try
        {
            var rect = window.BoundingRectangle;
            if (rect.Width <= 0 || rect.Height <= 0)
                return int.MinValue;

            var score = Math.Min((int)(rect.Width * rect.Height / 10_000), 1_000);
            if (!string.IsNullOrWhiteSpace(SafeString(() => window.Name)))
                score += 1_000;
            if (!string.IsNullOrWhiteSpace(SafeString(() => window.AutomationId)))
                score += 100;
            if (!SafeIsOffscreen(window))
                score += 500;
            return score;
        }
        catch
        {
            return int.MinValue;
        }
    }

    private static bool SafeIsOffscreen(AutomationElement element)
    {
        try { return element.IsOffscreen; }
        catch { return true; }
    }

    private static string StableWindowKey(AutomationElement window)
    {
        var handle = SafeHandle(window);
        if (handle != IntPtr.Zero)
            return $"handle:{handle.ToInt64():D20}";

        var automationId = SafeString(() => window.AutomationId);
        if (!string.IsNullOrWhiteSpace(automationId))
            return $"automationId:{automationId}";

        var name = SafeString(() => window.Name);
        if (!string.IsNullOrWhiteSpace(name))
            return $"name:{name}";

        return "unknown";
    }

    public static JsonNode FindElement(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var searchRoot = ResolveSearchRoot(mainWindow, @params, automation);

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var conditions = new List<ConditionBase>();

        var automationId = @params?["automationId"]?.GetValue<string>();
        if (automationId is not null)
            conditions.Add(cf.ByAutomationId(automationId));

        var name = @params?["name"]?.GetValue<string>();
        if (name is not null)
            conditions.Add(cf.ByName(name));

        var controlType = @params?["controlType"]?.GetValue<string>();
        if (controlType is not null)
        {
            var ct = ParseControlType(controlType);
            conditions.Add(cf.ByControlType(ct));
        }

        if (conditions.Count == 0)
        {
            // If xpath provided without other criteria, delegate to XPath search
            var xpath = @params?["xpath"]?.GetValue<string>();
            if (!string.IsNullOrWhiteSpace(xpath))
                return FindByXPath(@params, automation, mainWindow);
            throw new ArgumentException("At least one search criterion required: automationId, name, controlType, or xpath");
        }

        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());

        var element = searchRoot.FindFirstDescendant(condition);
        if (element is null)
            return new JsonObject
            {
                ["found"] = false,
                ["searchRootName"] = SafeString(() => searchRoot.Name),
                ["searchRootAutomationId"] = SafeString(() => searchRoot.AutomationId),
                ["searchRootOffscreen"] = SafeIsOffscreen(searchRoot),
                ["processId"] = JsonRpcHandler.ProcessId,
                ["topLevelWindowCount"] = GetProcessTopLevelWindows(mainWindow, automation).Count,
            };

        return BuildElementInfo(element);
    }

    public static JsonNode FindByXPath(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var xpath = @params?["xpath"]?.GetValue<string>()
            ?? throw new ArgumentException("Missing required parameter: xpath");

        var searchRoot = ResolveSearchRoot(mainWindow, @params, automation);

        try
        {
            // Count all matches for the warning
            var allMatches = searchRoot.FindAllByXPath(xpath);
            var matchCount = allMatches?.Length ?? 0;

            var element = matchCount > 0 ? allMatches![0] : null;

            if (element is null)
                return new JsonObject
                {
                    ["found"] = false,
                    ["xpath"] = xpath,
                    ["matchCount"] = 0
                };

            var result = BuildElementInfo(element);
            result["matchCount"] = matchCount;
            if (matchCount > 1)
                result["warning"] = $"XPath matched {matchCount} elements; returning first. Use more specific XPath to avoid ambiguity.";
            return result;
        }
        catch (Exception ex) when (ex is not InvalidOperationException)
        {
            throw new ArgumentException(
                $"XPath error for expression '{xpath}': {ex.Message}. " +
                "Hint: Use //ControlType[@Property='Value'] syntax. " +
                "Example: //Button[@Name='Save']");
        }
    }

    public static JsonNode GetTree(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var maxDepth = @params?["maxDepth"]?.GetValue<int>() ?? 3;
        var maxChildren = @params?["maxChildren"]?.GetValue<int>() ?? 25;

        // Walk every top-level window owned by the target process. WPF
        // Window.ShowDialog() creates a sibling top-level window under the
        // Desktop, not a descendant of the main app window — walking only
        // mainWindow would hide modal dialogs from the caller.
        var windows = GetProcessTopLevelWindows(mainWindow, automation);

        var windowsArray = new JsonArray();
        foreach (var window in windows)
        {
            try
            {
                windowsArray.Add(BuildTree(window, maxDepth, maxChildren, 0));
            }
            catch (Exception ex)
            {
                // One window failing must not hide the others.
                windowsArray.Add(new JsonObject
                {
                    ["found"] = false,
                    ["error"] = ex.Message,
                });
            }
        }

        // "primary" reports the currently-tracked main window's title if it
        // is still readable; otherwise fall back to the first enumerated
        // live window so agents always have a non-empty anchor.
        var primaryName = SafeString(() => mainWindow.Name);
        if (string.IsNullOrEmpty(primaryName) && windows.Count > 0)
            primaryName = SafeString(() => windows[0].Name);

        return new JsonObject
        {
            ["windows"] = windowsArray,
            ["count"] = windows.Count,
            ["primary"] = primaryName,
        };
    }

    /// <summary>
    /// Enumerate every top-level window owned by the same process as mainWindow.
    /// Returns mainWindow first, followed by every sibling top-level window,
    /// deduplicated by native window handle. This is the canonical source for
    /// multi-window operations (GetTree, ResolveSearchRoot, SetActiveWindow).
    /// </summary>
    internal static List<AutomationElement> GetProcessTopLevelWindows(
        AutomationElement mainWindow, UIA3Automation automation)
    {
        // Enumerate from the ProcessId that connect() stored independently
        // of MainWindow. Relying on mainWindow.Properties.ProcessId would
        // throw after set_active_window switched to a dialog that later
        // closed, which would strand the bridge with no discoverable windows.
        var pid = JsonRpcHandler.ProcessId;
        var result = new List<AutomationElement>();
        var seen = new HashSet<IntPtr>();

        if (pid == 0)
        {
            // Fallback for the unusual case where connect() never ran —
            // try to use whatever handle mainWindow still exposes.
            result.Add(mainWindow);
            var fallbackHandle = SafeHandle(mainWindow);
            if (fallbackHandle != IntPtr.Zero)
                seen.Add(fallbackHandle);
            return result;
        }

        try
        {
            var desktop = automation.GetDesktop();
            var cf = new ConditionFactory(automation.PropertyLibrary);
            var siblings = desktop.FindAllChildren(cf.ByProcessId(pid));
            foreach (var sibling in siblings)
            {
                var handle = SafeHandle(sibling);
                if (handle == IntPtr.Zero)
                    continue;
                if (seen.Add(handle))
                    result.Add(sibling);
            }
        }
        catch
        {
            // Desktop enumeration is best-effort.
        }

        // If live enumeration returned nothing but mainWindow still looks
        // usable, surface it so callers retain at least one reference.
        if (result.Count == 0)
            result.Add(mainWindow);

        return result;
    }

    private static IntPtr SafeHandle(AutomationElement element)
    {
        try { return element.Properties.NativeWindowHandle.ValueOrDefault; }
        catch (Exception ex)
        {
            // Log when a top-level window hides its handle — without this,
            // GetProcessTopLevelWindows silently excludes every sibling whose
            // handle can't be read (because IntPtr.Zero is in the seen set)
            // and debugging an empty tree becomes much harder.
            Program.Log($"SafeHandle: NativeWindowHandle unavailable ({ex.GetType().Name}: {ex.Message})");
            return IntPtr.Zero;
        }
    }

    // ── Shared helpers (used by PatternCommands too) ──────────────────

    /// <summary>
    /// Resolve search root: if rootAutomationId is provided, find that element
    /// and use it as the search scope. Scanning is widened to every top-level
    /// window of the target process so modal dialogs (which are siblings of the
    /// main window, not descendants) can be addressed as a root.
    /// </summary>
    internal static AutomationElement ResolveSearchRoot(
        AutomationElement mainWindow, JsonNode? @params, UIA3Automation automation)
    {
        var rootId = @params?["rootAutomationId"]?.GetValue<string>();
        if (string.IsNullOrWhiteSpace(rootId))
            return mainWindow;

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var topLevel = GetProcessTopLevelWindows(mainWindow, automation);

        // Pass 1: check whether any top-level window itself matches by identity.
        // Collecting all matches first lets us warn on ambiguous names (common
        // with dialogs like "Error", "Warning", "Progress" that appear twice).
        var windowMatches = new List<AutomationElement>();
        foreach (var window in topLevel)
        {
            if (MatchesWindowIdentity(window, rootId))
                windowMatches.Add(window);
        }

        if (windowMatches.Count == 1)
            return windowMatches[0];

        if (windowMatches.Count > 1)
        {
            var titles = new List<string>();
            foreach (var w in windowMatches)
            {
                string title;
                try { title = w.Properties.Name.IsSupported ? w.Properties.Name.Value : ""; }
                catch { title = ""; }
                titles.Add($"'{title}'");
            }
            throw new InvalidOperationException(
                $"Ambiguous root '{rootId}': {windowMatches.Count} top-level windows match " +
                $"({string.Join(", ", titles)}). Use set_active_window with a more specific " +
                "criterion, or pass rootAutomationId as the unique AutomationId.");
        }

        // Pass 2: no window-level match — descend into each window looking
        // for a descendant with that AutomationId. Collect across all windows
        // so an ambiguous rootId (same AutomationId present in two windows)
        // fails loudly rather than silently resolving to whichever window
        // happens to be enumerated first.
        var descendantMatches = new List<(AutomationElement Element, string WindowTitle)>();
        foreach (var window in topLevel)
        {
            var descendant = window.FindFirstDescendant(cf.ByAutomationId(rootId));
            if (descendant is not null)
            {
                string title;
                try { title = window.Properties.Name.IsSupported ? window.Properties.Name.Value : ""; }
                catch { title = ""; }
                descendantMatches.Add((descendant, title));
            }
        }

        if (descendantMatches.Count == 1)
            return descendantMatches[0].Element;

        if (descendantMatches.Count > 1)
        {
            var windowTitles = descendantMatches.Select(m => $"'{m.WindowTitle}'");
            throw new InvalidOperationException(
                $"Ambiguous rootAutomationId '{rootId}': found in " +
                $"{descendantMatches.Count} windows ({string.Join(", ", windowTitles)}). " +
                "Use set_active_window to target a specific top-level window first, " +
                "then pass rootAutomationId without ambiguity.");
        }

        throw new InvalidOperationException(
            $"Root element not found: '{rootId}'. Use get_tree to verify the element exists " +
            "or use set_active_window to target a top-level window by name.");
    }

    private static bool MatchesWindowIdentity(AutomationElement window, string rootId)
    {
        try
        {
            if (window.Properties.AutomationId.IsSupported &&
                window.Properties.AutomationId.Value == rootId)
                return true;
        }
        catch { /* AutomationId may not be supported on this element */ }

        try
        {
            if (window.Properties.Name.IsSupported &&
                window.Properties.Name.Value == rootId)
                return true;
        }
        catch { /* Name may not be supported on this element */ }

        return false;
    }

    /// <summary>
    /// Switch the bridge's tracked main window to a different top-level window
    /// owned by the same process. Lookup priority: automationId → name. Required
    /// so agents can target WPF modal dialogs, which are sibling top-level
    /// windows rather than descendants of the original app window.
    /// </summary>
    public static JsonNode SetActiveWindow(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var automationId = @params?["automationId"]?.GetValue<string>();
        var name = @params?["name"]?.GetValue<string>();

        if (string.IsNullOrWhiteSpace(automationId) && string.IsNullOrWhiteSpace(name))
            throw new ArgumentException(
                "set_active_window requires at least one of: automationId, name");

        var topLevel = GetProcessTopLevelWindows(mainWindow, automation);

        // Two-pass scan so automationId universally wins over name across
        // the full window list, not just within a single window iteration.
        // Ambiguous matches (same automationId or same title on multiple
        // top-level windows) throw an explicit error instead of silently
        // returning the first — non-determinism on a stateful switch is the
        // worst kind of bug for downstream agents.
        var automationIdMatches = new List<AutomationElement>();
        if (!string.IsNullOrWhiteSpace(automationId))
        {
            foreach (var window in topLevel)
            {
                try
                {
                    if (window.Properties.AutomationId.IsSupported &&
                        window.Properties.AutomationId.Value == automationId)
                    {
                        automationIdMatches.Add(window);
                    }
                }
                catch { /* skip — unsupported on this window */ }
            }

            if (automationIdMatches.Count > 1)
            {
                var titles = automationIdMatches.Select(w => $"'{SafeString(() => w.Name)}'");
                throw new InvalidOperationException(
                    $"Ambiguous set_active_window(automationId='{automationId}'): " +
                    $"{automationIdMatches.Count} windows match ({string.Join(", ", titles)}). " +
                    "AutomationId should uniquely identify a top-level window.");
            }
        }

        AutomationElement? match = automationIdMatches.Count == 1 ? automationIdMatches[0] : null;

        if (match is null && !string.IsNullOrWhiteSpace(name))
        {
            var nameMatches = new List<AutomationElement>();
            foreach (var window in topLevel)
            {
                try
                {
                    if (window.Properties.Name.IsSupported &&
                        window.Properties.Name.Value == name)
                    {
                        nameMatches.Add(window);
                    }
                }
                catch { /* skip — unsupported on this window */ }
            }

            if (nameMatches.Count > 1)
            {
                var ids = nameMatches.Select(w => $"automationId='{SafeString(() => w.AutomationId)}'");
                throw new InvalidOperationException(
                    $"Ambiguous set_active_window(name='{name}'): " +
                    $"{nameMatches.Count} windows share this title ({string.Join(", ", ids)}). " +
                    "Pass a unique automationId instead, or close the duplicate window first.");
            }

            if (nameMatches.Count == 1)
                match = nameMatches[0];
        }

        if (match is null)
        {
            var criteria = new List<string>();
            if (!string.IsNullOrWhiteSpace(automationId)) criteria.Add($"automationId='{automationId}'");
            if (!string.IsNullOrWhiteSpace(name)) criteria.Add($"name='{name}'");
            throw new InvalidOperationException(
                $"No top-level window matches {string.Join(", ", criteria)} " +
                "in the target process.");
        }

        JsonRpcHandler.MainWindow = match;

        return new JsonObject
        {
            ["switched"] = true,
            ["title"] = SafeString(() => match.Name),
            ["automationId"] = SafeString(() => match.AutomationId),
        };
    }

    /// <summary>
    /// Find element using priority cascade: automationId > xpath > name+controlType.
    /// Throws if element not found.
    /// </summary>
    internal static AutomationElement FindElementCascade(
        AutomationElement root,
        JsonNode? @params,
        UIA3Automation automation,
        bool strictAutomationId = false)
    {
        var cf = new ConditionFactory(automation.PropertyLibrary);

        // Priority 1: AutomationId
        var automationId = @params?["automationId"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(automationId))
        {
            var element = root.FindFirstDescendant(cf.ByAutomationId(automationId));
            if (element is not null)
                return element;
            if (strictAutomationId)
            {
                throw new ExactAutomationIdMismatchException(
                    BuildExactAutomationIdMismatchPayload(@params));
            }
        }

        // Priority 2: XPath
        var xpath = @params?["xpath"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(xpath))
        {
            var element = root.FindFirstByXPath(xpath);
            if (element is not null)
                return element;
        }

        // Priority 3: Name + ControlType
        var name = @params?["name"]?.GetValue<string>();
        var controlType = @params?["controlType"]?.GetValue<string>();

        if (!string.IsNullOrWhiteSpace(name) || !string.IsNullOrWhiteSpace(controlType))
        {
            var conditions = new List<ConditionBase>();
            if (!string.IsNullOrWhiteSpace(name))
                conditions.Add(cf.ByName(name));
            if (!string.IsNullOrWhiteSpace(controlType))
            {
                var ct = ParseControlType(controlType);
                conditions.Add(cf.ByControlType(ct));
            }

            if (conditions.Count > 0)
            {
                var condition = conditions.Count == 1
                    ? conditions[0]
                    : new AndCondition(conditions.ToArray());
                var element = root.FindFirstDescendant(condition);
                if (element is not null)
                    return element;
            }
        }

        throw new InvalidOperationException(
            $"Element not found. Search: {DescribeSearch(@params)}");
    }

    /// <summary>
    /// Resolves one descendant under one bound-process parent without issuing input.
    /// The returned target is admitted only after two matching identity/geometry reads.
    /// </summary>
    public static JsonNode ResolveGuardedChild(
        JsonNode? @params,
        UIA3Automation automation,
        AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var request = ReadGuardedChildRequest(@params);
        var parentSelector = ReadGuardedSelectorCriteria(request.Parent);
        var predicate = ReadGuardedSelectorCriteria(request.Predicate);
        var boundProcessId = JsonRpcHandler.ProcessId;
        if (boundProcessId <= 0)
            return GuardedChildBlocked("IDENTITY_UNAVAILABLE", 0);

        if (!TryGetBoundTopLevelWindows(
                mainWindow,
                automation,
                boundProcessId,
                out var topLevelWindows,
                out var topLevelHandles))
        {
            return GuardedChildBlocked("IDENTITY_UNAVAILABLE", 0);
        }

        var parentResolution = ResolveUniqueGuardedParent(
            topLevelWindows,
            parentSelector,
            boundProcessId,
            automation);
        if (parentResolution.Outcome != GuardedChildResolutionOutcome.Unique)
        {
            return parentResolution.Outcome switch
            {
                GuardedChildResolutionOutcome.Missing or GuardedChildResolutionOutcome.Ambiguous =>
                    GuardedChildBlocked("PARENT_NOT_UNIQUE", parentResolution.MatchCount),
                GuardedChildResolutionOutcome.ProcessMismatch =>
                    GuardedChildBlocked("PROCESS_MISMATCH", parentResolution.MatchCount),
                _ => GuardedChildBlocked("IDENTITY_UNAVAILABLE", parentResolution.MatchCount),
            };
        }

        var childResolution = ResolveUniqueGuardedElement(
            new[] { parentResolution.Element! },
            includeRoots: false,
            predicate,
            boundProcessId,
            request.MaximumNodes);
        if (childResolution.Outcome != GuardedChildResolutionOutcome.Unique)
        {
            return childResolution.Outcome switch
            {
                GuardedChildResolutionOutcome.Missing =>
                    GuardedChildBlocked("CHILD_NOT_FOUND", childResolution.MatchCount),
                GuardedChildResolutionOutcome.Ambiguous =>
                    GuardedChildBlocked("CHILD_AMBIGUOUS", childResolution.MatchCount),
                GuardedChildResolutionOutcome.ProcessMismatch =>
                    GuardedChildBlocked("PROCESS_MISMATCH", childResolution.MatchCount),
                _ => GuardedChildBlocked("IDENTITY_UNAVAILABLE", childResolution.MatchCount),
            };
        }

        var before = ReadGuardedChildSnapshot(
            childResolution.Element!,
            topLevelHandles,
            boundProcessId);
        if (before.FailureReason is not null)
            return GuardedChildBlocked(before.FailureReason, childResolution.MatchCount);

        var beforeSnapshot = before.Snapshot!.Value;
        if (!MatchesGuardedChildSnapshot(beforeSnapshot, predicate))
            return GuardedChildBlocked("IDENTITY_DRIFT", childResolution.MatchCount);

        var after = ReadGuardedChildSnapshot(
            childResolution.Element!,
            topLevelHandles,
            boundProcessId);
        if (after.FailureReason is not null)
            return GuardedChildBlocked(after.FailureReason, childResolution.MatchCount);

        var afterSnapshot = after.Snapshot!.Value;
        if (JsonRpcHandler.ProcessId != boundProcessId)
            return GuardedChildBlocked("PROCESS_MISMATCH", childResolution.MatchCount);
        if (beforeSnapshot.Hwnd != afterSnapshot.Hwnd)
            return GuardedChildBlocked("HWND_MISMATCH", childResolution.MatchCount);
        if (beforeSnapshot != afterSnapshot || !MatchesGuardedChildSnapshot(afterSnapshot, predicate))
            return GuardedChildBlocked("IDENTITY_DRIFT", childResolution.MatchCount);

        return GuardedChildAdmitted(beforeSnapshot);
    }

    private const int GuardedChildMaximumNodes = 4_096;
    private const int GuardedChildMaximumAncestorDepth = 128;

    private static GuardedChildRequest ReadGuardedChildRequest(JsonNode? @params)
    {
        if (@params is not JsonObject request || request.Count != 3 ||
            request["parent"] is not JsonObject parent ||
            request["predicate"] is not JsonObject predicate ||
            request["maximumNodes"] is not JsonValue maximumNodesValue ||
            !maximumNodesValue.TryGetValue<int>(out var maximumNodes) ||
            maximumNodes is < 1 or > GuardedChildMaximumNodes)
        {
            throw new InvalidDataException(
                "resolve_guarded_child requires parent, predicate, and maximumNodes from 1 through 4096.");
        }

        return new GuardedChildRequest(parent, predicate, maximumNodes);
    }

    private static GuardedSelectorCriteria ReadGuardedSelectorCriteria(JsonObject selector)
    {
        var automationId = ReadGuardedSelectorText(selector, "automationId");
        var name = ReadGuardedSelectorText(selector, "name");
        var controlTypeText = ReadGuardedSelectorText(selector, "controlType");
        if (automationId is null && name is null && controlTypeText is null)
            throw new InvalidDataException("A guarded selector requires automationId, name, or controlType.");

        return new GuardedSelectorCriteria(
            automationId,
            name,
            controlTypeText is null ? null : ParseControlType(controlTypeText));
    }

    private static GuardedChildResolution ResolveUniqueGuardedParent(
        IReadOnlyList<AutomationElement> roots,
        GuardedSelectorCriteria selector,
        int boundProcessId,
        UIA3Automation automation)
    {
        var factory = new ConditionFactory(automation.PropertyLibrary);
        var conditions = new List<ConditionBase>();
        if (selector.AutomationId is not null)
            conditions.Add(factory.ByAutomationId(selector.AutomationId));
        if (selector.Name is not null)
            conditions.Add(factory.ByName(selector.Name));
        if (selector.ControlType is not null)
            conditions.Add(factory.ByControlType(selector.ControlType.Value));
        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());
        AutomationElement? match = null;
        var matchCount = 0;
        foreach (var root in roots)
        {
            var candidates = new List<AutomationElement>();
            if (TryMatchesGuardedSelector(root, selector, out var rootMatches) && rootMatches)
                candidates.Add(root);
            try
            {
                candidates.AddRange(root.FindAllDescendants(condition));
            }
            catch
            {
                return GuardedChildResolution.IdentityUnavailable(matchCount);
            }
            foreach (var candidate in candidates)
            {
                var processId = TryReadProcessId(candidate);
                if (processId is null)
                    return GuardedChildResolution.IdentityUnavailable(matchCount);
                if (processId != boundProcessId)
                    return GuardedChildResolution.ProcessMismatch(matchCount);
                matchCount++;
                if (matchCount == 2)
                    return GuardedChildResolution.Ambiguous(matchCount);
                match = candidate;
            }
        }
        return matchCount == 1
            ? GuardedChildResolution.Unique(match!)
            : GuardedChildResolution.Missing();
    }

    private static bool TryGetBoundTopLevelWindows(
        AutomationElement mainWindow,
        UIA3Automation automation,
        int boundProcessId,
        out List<AutomationElement> windows,
        out HashSet<IntPtr> windowHandles)
    {
        windows = new List<AutomationElement>();
        windowHandles = new HashSet<IntPtr>();
        AutomationElement[] siblings;
        try
        {
            var desktop = automation.GetDesktop();
            siblings = desktop.FindAllChildren(
                new ConditionFactory(automation.PropertyLibrary).ByProcessId(boundProcessId));
        }
        catch
        {
            return false;
        }

        if (siblings.Length == 0)
            return false;

        foreach (var sibling in siblings)
        {
            IntPtr hwnd;
            int processId;
            try
            {
                hwnd = sibling.Properties.NativeWindowHandle.ValueOrDefault;
                processId = sibling.Properties.ProcessId.ValueOrDefault;
            }
            catch
            {
                return false;
            }

            if (hwnd == IntPtr.Zero || processId != boundProcessId)
                return false;
            if (windowHandles.Add(hwnd))
                windows.Add(sibling);
        }

        try
        {
            var mainWindowHandle = mainWindow.Properties.NativeWindowHandle.ValueOrDefault;
            return mainWindowHandle != IntPtr.Zero && windowHandles.Contains(mainWindowHandle);
        }
        catch
        {
            return false;
        }
    }

    private static GuardedChildResolution ResolveUniqueGuardedElement(
        IReadOnlyList<AutomationElement> roots,
        bool includeRoots,
        GuardedSelectorCriteria selector,
        int boundProcessId,
        int maximumNodes)
    {
        var queue = new Queue<AutomationElement>();
        var discovered = 0;
        foreach (var root in roots)
        {
            if (includeRoots)
            {
                if (!TryEnqueueGuardedElement(queue, root, ref discovered, maximumNodes))
                    return GuardedChildResolution.IdentityUnavailable(0);
            }
            else if (!TryEnqueueGuardedChildren(queue, root, ref discovered, maximumNodes))
            {
                return GuardedChildResolution.IdentityUnavailable(0);
            }
        }

        AutomationElement? match = null;
        var matchCount = 0;
        while (queue.Count > 0)
        {
            var element = queue.Dequeue();
            var processId = TryReadProcessId(element);
            if (processId is null)
                return GuardedChildResolution.IdentityUnavailable(matchCount);
            if (processId != boundProcessId)
                return GuardedChildResolution.ProcessMismatch(matchCount);
            var isMatch = false;
            if (!TryMatchesGuardedSelector(element, selector, out isMatch))
                return GuardedChildResolution.IdentityUnavailable(matchCount);

            if (isMatch)
            {
                matchCount++;
                if (matchCount == 2)
                    return GuardedChildResolution.Ambiguous(matchCount);
                match = element;
            }

            if (!TryEnqueueGuardedChildren(queue, element, ref discovered, maximumNodes))
                return GuardedChildResolution.IdentityUnavailable(matchCount);
        }

        return matchCount switch
        {
            0 => GuardedChildResolution.Missing(),
            1 => GuardedChildResolution.Unique(match!),
            _ => throw new InvalidOperationException("Unexpected guarded child resolution state."),
        };
    }

    private static bool TryEnqueueGuardedElement(
        Queue<AutomationElement> queue,
        AutomationElement element,
        ref int discovered,
        int maximumNodes)
    {
        if (discovered >= maximumNodes)
            return false;

        queue.Enqueue(element);
        discovered++;
        return true;
    }

    private static bool TryEnqueueGuardedChildren(
        Queue<AutomationElement> queue,
        AutomationElement parent,
        ref int discovered,
        int maximumNodes)
    {
        AutomationElement[] children;
        try
        {
            children = parent.FindAllChildren();
        }
        catch
        {
            return false;
        }

        foreach (var child in children)
        {
            if (!TryEnqueueGuardedElement(queue, child, ref discovered, maximumNodes))
                return false;
        }

        return true;
    }

    private static bool TryMatchesGuardedSelector(
        AutomationElement element,
        GuardedSelectorCriteria selector,
        out bool isMatch)
    {
        try
        {
            var automationId = element.AutomationId ?? string.Empty;
            var name = element.Name ?? string.Empty;
            var controlType = element.ControlType;

            isMatch =
                (selector.AutomationId is null || string.Equals(
                    automationId, selector.AutomationId, StringComparison.Ordinal)) &&
                (selector.Name is null || string.Equals(name, selector.Name, StringComparison.Ordinal)) &&
                (selector.ControlType is null || controlType == selector.ControlType);
            return true;
        }
        catch
        {
            isMatch = false;
            return false;
        }
    }

    private static GuardedChildSnapshotRead ReadGuardedChildSnapshot(
        AutomationElement element,
        HashSet<IntPtr> topLevelHandles,
        int boundProcessId)
    {
        var automationId = SafeString(() => element.AutomationId);
        var name = SafeString(() => element.Name);
        var resolvedControlType = SafeControlType(element);
        if (resolvedControlType is null)
            return GuardedChildSnapshotRead.Failure("IDENTITY_UNAVAILABLE");
        var controlType = resolvedControlType.Value.ToString();

        if (string.IsNullOrEmpty(controlType))
            return GuardedChildSnapshotRead.Failure("IDENTITY_UNAVAILABLE");
        var processId = TryReadProcessId(element);
        if (processId is null)
            return GuardedChildSnapshotRead.Failure("IDENTITY_UNAVAILABLE");
        if (processId != boundProcessId)
            return GuardedChildSnapshotRead.Failure("PROCESS_MISMATCH");

        if (!TryResolveOwningTopLevelHwnd(element, topLevelHandles, out var hwnd))
            return GuardedChildSnapshotRead.Failure("HWND_MISMATCH");
        if (GetWindowThreadProcessId(hwnd, out var hwndProcessId) == 0)
            return GuardedChildSnapshotRead.Failure("HWND_MISMATCH");
        if (hwndProcessId != (uint)boundProcessId)
            return GuardedChildSnapshotRead.Failure("PROCESS_MISMATCH");

        GuardedChildRect rectangle;
        try
        {
            var bounds = element.BoundingRectangle;
            rectangle = new GuardedChildRect(bounds.Left, bounds.Top, bounds.Right, bounds.Bottom);
        }
        catch
        {
            return GuardedChildSnapshotRead.Failure("RECTANGLE_INVALID");
        }

        if (!rectangle.IsPositive)
            return GuardedChildSnapshotRead.Failure("RECTANGLE_INVALID");
        if (!TryReadScreenClientRectangle(hwnd, out var clientRectangle))
            return GuardedChildSnapshotRead.Failure("RECTANGLE_INVALID");
        if (!IsFullyContained(clientRectangle, rectangle))
            return GuardedChildSnapshotRead.Failure("CONTAINMENT_FAILURE");

        return GuardedChildSnapshotRead.Success(new GuardedChildSnapshot(
            automationId,
            name,
            controlType,
            processId.Value,
            hwnd.ToInt64(),
            rectangle,
            clientRectangle));
    }

    private static bool TryResolveOwningTopLevelHwnd(
        AutomationElement element,
        HashSet<IntPtr> topLevelHandles,
        out IntPtr hwnd)
    {
        AutomationElement? current = element;
        for (var depth = 0; current is not null && depth < GuardedChildMaximumAncestorDepth; depth++)
        {
            var candidate = SafeHandle(current);

            if (candidate != IntPtr.Zero && topLevelHandles.Contains(candidate))
            {
                hwnd = candidate;
                return true;
            }

            try
            {
                current = current.Parent;
            }
            catch
            {
                hwnd = IntPtr.Zero;
                return false;
            }
        }

        hwnd = IntPtr.Zero;
        return false;
    }

    private static bool TryReadScreenClientRectangle(IntPtr hwnd, out GuardedChildRect clientRectangle)
    {
        clientRectangle = default;
        if (!GetClientRect(hwnd, out var client) ||
            client.Right <= client.Left || client.Bottom <= client.Top)
        {
            return false;
        }

        var topLeft = new NativePoint { X = client.Left, Y = client.Top };
        var bottomRight = new NativePoint { X = client.Right, Y = client.Bottom };
        if (!ClientToScreen(hwnd, ref topLeft) || !ClientToScreen(hwnd, ref bottomRight))
            return false;

        clientRectangle = new GuardedChildRect(
            topLeft.X,
            topLeft.Y,
            bottomRight.X,
            bottomRight.Y);
        return clientRectangle.IsPositive;
    }

    private static bool IsFullyContained(GuardedChildRect container, GuardedChildRect child) =>
        child.Left >= container.Left &&
        child.Top >= container.Top &&
        child.Right <= container.Right &&
        child.Bottom <= container.Bottom;

    private static bool MatchesGuardedChildSnapshot(
        GuardedChildSnapshot snapshot,
        GuardedSelectorCriteria selector) =>
        (selector.AutomationId is null || string.Equals(
            snapshot.AutomationId, selector.AutomationId, StringComparison.Ordinal)) &&
        (selector.Name is null || string.Equals(snapshot.Name, selector.Name, StringComparison.Ordinal)) &&
        (selector.ControlType is null || string.Equals(
            snapshot.ControlType, selector.ControlType.ToString(), StringComparison.Ordinal));

    private static JsonObject GuardedChildAdmitted(GuardedChildSnapshot snapshot) => new()
    {
        ["status"] = "ADMITTED",
        ["match_count"] = 1,
        ["target"] = new JsonObject
        {
            ["automation_id"] = snapshot.AutomationId,
            ["name"] = snapshot.Name,
            ["control_type"] = snapshot.ControlType,
            ["process_id"] = snapshot.ProcessId,
            ["hwnd"] = snapshot.Hwnd,
            ["rectangle"] = GuardedChildRectangleJson(snapshot.Rectangle),
            ["center"] = new JsonObject
            {
                ["x"] = snapshot.Rectangle.Left + ((snapshot.Rectangle.Right - snapshot.Rectangle.Left) / 2),
                ["y"] = snapshot.Rectangle.Top + ((snapshot.Rectangle.Bottom - snapshot.Rectangle.Top) / 2),
            },
        },
        ["window"] = new JsonObject
        {
            ["hwnd"] = snapshot.Hwnd,
            ["process_id"] = snapshot.ProcessId,
            ["client_rectangle"] = GuardedChildRectangleJson(snapshot.ClientRectangle),
        },
        ["stability"] = new JsonObject
        {
            ["reads"] = 2,
            ["matched"] = true,
        },
    };

    private static JsonObject GuardedChildRectangleJson(GuardedChildRect rectangle) => new()
    {
        ["left"] = rectangle.Left,
        ["top"] = rectangle.Top,
        ["right"] = rectangle.Right,
        ["bottom"] = rectangle.Bottom,
        ["unit"] = "physical_px",
        ["coordinate_space"] = "screen",
    };

    private static JsonObject GuardedChildBlocked(string reason, int matchCount) => new()
    {
        ["status"] = "BLOCKED",
        ["reason"] = reason,
        ["match_count"] = matchCount,
    };

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint processId);

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool GetClientRect(IntPtr hwnd, out NativeRect rect);

    [System.Runtime.InteropServices.DllImport("user32.dll", SetLastError = true)]
    private static extern bool ClientToScreen(IntPtr hwnd, ref NativePoint point);

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct NativePoint
    {
        public int X;
        public int Y;
    }

    [System.Runtime.InteropServices.StructLayout(System.Runtime.InteropServices.LayoutKind.Sequential)]
    private struct NativeRect
    {
        public int Left;
        public int Top;
        public int Right;
        public int Bottom;
    }

    private sealed record GuardedChildRequest(
        JsonObject Parent,
        JsonObject Predicate,
        int MaximumNodes);

    private sealed record GuardedSelectorCriteria(
        string? AutomationId,
        string? Name,
        ControlType? ControlType);

    private enum GuardedChildResolutionOutcome
    {
        Unique,
        Missing,
        Ambiguous,
        IdentityUnavailable,
        ProcessMismatch,
    }

    private sealed record GuardedChildResolution(
        GuardedChildResolutionOutcome Outcome,
        AutomationElement? Element,
        int MatchCount)
    {
        internal static GuardedChildResolution Unique(AutomationElement element) =>
            new(GuardedChildResolutionOutcome.Unique, element, 1);

        internal static GuardedChildResolution Missing() =>
            new(GuardedChildResolutionOutcome.Missing, null, 0);

        internal static GuardedChildResolution Ambiguous(int matchCount) =>
            new(GuardedChildResolutionOutcome.Ambiguous, null, matchCount);

        internal static GuardedChildResolution IdentityUnavailable(int matchCount) =>
            new(GuardedChildResolutionOutcome.IdentityUnavailable, null, matchCount);

        internal static GuardedChildResolution ProcessMismatch(int matchCount) =>
            new(GuardedChildResolutionOutcome.ProcessMismatch, null, matchCount);
    }

    private readonly record struct GuardedChildRect(int Left, int Top, int Right, int Bottom)
    {
        internal bool IsPositive => Right > Left && Bottom > Top;
    }

    private readonly record struct GuardedChildSnapshot(
        string AutomationId,
        string Name,
        string ControlType,
        int ProcessId,
        long Hwnd,
        GuardedChildRect Rectangle,
        GuardedChildRect ClientRectangle);

    private sealed record GuardedChildSnapshotRead(
        GuardedChildSnapshot? Snapshot,
        string? FailureReason)
    {
        internal static GuardedChildSnapshotRead Success(GuardedChildSnapshot snapshot) =>
            new(snapshot, null);

        internal static GuardedChildSnapshotRead Failure(string reason) =>
            new(null, reason);
    }

    internal static GuardedSelectorResolution ResolveUniqueBoundElement(
        AutomationElement root,
        JsonObject selector,
        int boundProcessId,
        int maximumNodes)
    {
        if (maximumNodes is < 1 or > 4_096)
            throw new ArgumentOutOfRangeException(nameof(maximumNodes));

        var automationId = ReadGuardedSelectorText(selector, "automationId");
        var name = ReadGuardedSelectorText(selector, "name");
        var controlTypeText = ReadGuardedSelectorText(selector, "controlType");
        if (automationId is null && name is null && controlTypeText is null)
            throw new InvalidDataException("A guarded selector requires automationId, name, or controlType.");

        var controlType = controlTypeText is null ? (ControlType?)null : ParseControlType(controlTypeText);
        var queue = new Queue<AutomationElement>();
        queue.Enqueue(root);
        var visited = 0;
        var matches = new List<AutomationElement>(2);
        var truncated = false;

        while (queue.Count > 0)
        {
            var element = queue.Dequeue();
            visited++;
            var processId = TryReadProcessId(element);
            if (processId is null)
            {
                return new GuardedSelectorResolution(
                    GuardedSelectorOutcome.Unobservable, null, matches.Count,
                    "UIA process identity is unavailable while resolving the selector.");
            }

            if (processId == boundProcessId)
            {
                if (MatchesGuardedSelector(element, automationId, name, controlType))
                {
                    matches.Add(element);
                    if (matches.Count == 2)
                    {
                        return new GuardedSelectorResolution(
                            GuardedSelectorOutcome.Ambiguous, null, matches.Count,
                            "The selector matches more than one bound-process element.");
                    }
                }

                AutomationElement[] children;
                try
                {
                    children = element.FindAllChildren();
                }
                catch (Exception ex)
                {
                    return new GuardedSelectorResolution(
                        GuardedSelectorOutcome.Unobservable, null, matches.Count,
                        $"UIA child enumeration failed: {ex.GetType().Name}.");
                }

                foreach (var child in children)
                {
                    if (visited + queue.Count >= maximumNodes)
                    {
                        truncated = true;
                        break;
                    }

                    queue.Enqueue(child);
                }
            }
        }

        if (truncated)
        {
            return new GuardedSelectorResolution(
                GuardedSelectorOutcome.Unobservable, null, matches.Count,
                "The bounded selector traversal did not cover the complete bound window tree.");
        }

        return matches.Count switch
        {
            0 => new GuardedSelectorResolution(
                GuardedSelectorOutcome.Missing, null, 0,
                "The selector matches no bound-process element."),
            1 => new GuardedSelectorResolution(
                GuardedSelectorOutcome.Unique, matches[0], 1, null),
            _ => throw new InvalidOperationException("Unexpected guarded-selector state."),
        };
    }

    private static string? ReadGuardedSelectorText(JsonObject selector, string key)
    {
        foreach (var property in selector)
        {
            if (property.Key is not ("automationId" or "name" or "controlType"))
                throw new InvalidDataException($"Unsupported guarded selector property: {property.Key}.");
        }

        if (!selector.TryGetPropertyValue(key, out var node) || node is null)
            return null;
        if (node is not JsonValue value || !value.TryGetValue<string>(out var text) ||
            string.IsNullOrWhiteSpace(text) || text.Length > 256)
        {
            throw new InvalidDataException($"Guarded selector property '{key}' must be a non-empty string of at most 256 characters.");
        }

        return text;
    }

    private static bool MatchesGuardedSelector(
        AutomationElement element,
        string? automationId,
        string? name,
        ControlType? controlType)
    {
        return (automationId is null || string.Equals(SafeString(() => element.AutomationId), automationId, StringComparison.Ordinal)) &&
               (name is null || string.Equals(SafeString(() => element.Name), name, StringComparison.Ordinal)) &&
               (controlType is null || SafeControlType(element) == controlType);
    }

    private static ControlType? SafeControlType(AutomationElement element)
    {
        try { return element.ControlType; }
        catch { return null; }
    }

    private static int? TryReadProcessId(AutomationElement element)
    {
        try
        {
            if (!element.Properties.ProcessId.IsSupported)
                return null;

            var processId = element.Properties.ProcessId.Value;
            return processId > 0 ? processId : null;
        }
        catch
        {
            return null;
        }
    }

    internal static string DescribeSearch(JsonNode? @params)
    {
        var parts = new List<string>();
        var aid = @params?["automationId"]?.GetValue<string>();
        if (aid is not null) parts.Add($"automationId='{aid}'");
        var xpath = @params?["xpath"]?.GetValue<string>();
        if (xpath is not null) parts.Add($"xpath='{xpath}'");
        var name = @params?["name"]?.GetValue<string>();
        if (name is not null) parts.Add($"name='{name}'");
        var ct = @params?["controlType"]?.GetValue<string>();
        if (ct is not null) parts.Add($"controlType='{ct}'");
        return parts.Count > 0 ? string.Join(", ", parts) : "(no criteria)";
    }

    private static ControlType ParseControlType(string controlType)
    {
        if (!Enum.TryParse<ControlType>(controlType, true, out var ct) ||
            !Enum.IsDefined(typeof(ControlType), ct))
            throw new ArgumentException($"Unknown controlType: {controlType}");
        return ct;
    }

    // ── Ranked search (FR-1) ───────────────────────────────────────

    public static JsonNode FindAllCascade(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var searchRoot = ResolveSearchRoot(mainWindow, @params, automation);
        var maxResults = @params?["maxResults"]?.GetValue<int>() ?? 10;
        var cf = new ConditionFactory(automation.PropertyLibrary);

        // Build condition from name + controlType (ranking only applies to ambiguous searches)
        var name = @params?["name"]?.GetValue<string>();
        var controlType = @params?["controlType"]?.GetValue<string>();

        if (string.IsNullOrWhiteSpace(name) && string.IsNullOrWhiteSpace(controlType))
            throw new ArgumentException("find_all_cascade requires at least name or controlType");

        var conditions = new List<ConditionBase>();
        if (!string.IsNullOrWhiteSpace(name))
            conditions.Add(cf.ByName(name));
        if (!string.IsNullOrWhiteSpace(controlType))
        {
            var ct = ParseControlType(controlType);
            conditions.Add(cf.ByControlType(ct));
        }

        var condition = conditions.Count == 1
            ? conditions[0]
            : new AndCondition(conditions.ToArray());

        var allMatches = searchRoot.FindAllDescendants(condition);
        if (allMatches.Length == 0)
            return new JsonObject { ["results"] = new JsonArray(), ["totalMatches"] = 0 };

        // Rank matches
        var scored = new List<(AutomationElement Element, int Score, int Depth, string ParentDesc)>();
        foreach (var el in allMatches)
        {
            try
            {
                var depth = GetDepth(el, searchRoot);
                var score = ScoreElement(el, depth);
                var parentDesc = GetParentDescription(el);
                scored.Add((el, score, depth, parentDesc));
            }
            catch
            {
                // Skip elements that fail to score
            }
        }

        scored.Sort((a, b) => b.Score.CompareTo(a.Score));

        var results = new JsonArray();
        var count = Math.Min(scored.Count, maxResults);
        for (var i = 0; i < count; i++)
        {
            var (el, score, depth, parentDesc) = scored[i];
            var info = BuildElementInfo(el, includePatterns: false);
            info["score"] = score;
            info["depth"] = depth;
            info["parentDesc"] = parentDesc;
            results.Add(info);
        }

        return new JsonObject
        {
            ["results"] = results,
            ["totalMatches"] = allMatches.Length
        };
    }

    // ── Text extraction (FR-2) ──────────────────────────────────────

    public static JsonNode ExtractText(JsonNode? @params, UIA3Automation automation, AutomationElement? mainWindow)
    {
        if (mainWindow is null)
            throw new InvalidOperationException("Not connected. Call 'connect' first.");

        var searchRoot = ResolveSearchRoot(mainWindow, @params, automation);
        var element = FindElementCascade(searchRoot, @params, automation);

        // Strategy 1: ValuePattern
        try
        {
            if (element.Patterns.Value.IsSupported)
            {
                var val = element.Patterns.Value.Pattern.Value.ValueOrDefault;
                if (!string.IsNullOrEmpty(val))
                    return new JsonObject { ["text"] = val, ["source"] = "ValuePattern" };
            }
        }
        catch { /* fall through */ }

        // Strategy 2: TextPattern
        try
        {
            if (element.Patterns.Text.IsSupported)
            {
                var text = element.Patterns.Text.Pattern.DocumentRange.GetText(-1);
                if (!string.IsNullOrEmpty(text))
                    return new JsonObject { ["text"] = text, ["source"] = "TextPattern" };
            }
        }
        catch { /* fall through */ }

        // Strategy 3: Name property
        try
        {
            var elName = element.Name;
            if (!string.IsNullOrEmpty(elName))
            {
                // Check for CLR type name pattern
                if (IsLikelyCLRTypeName(elName))
                {
                    var descendantText = GetVisibleDescendantText(element, automation);
                    if (!string.IsNullOrEmpty(descendantText))
                        return new JsonObject { ["text"] = descendantText, ["source"] = "TextDescendants" };
                }
                return new JsonObject { ["text"] = elName, ["source"] = "Name" };
            }
        }
        catch { /* fall through */ }

        // Strategy 4: LegacyIAccessible
        try
        {
            if (element.Patterns.LegacyIAccessible.IsSupported)
            {
                var legacyName = element.Patterns.LegacyIAccessible.Pattern.Name.ValueOrDefault;
                if (!string.IsNullOrEmpty(legacyName))
                    return new JsonObject { ["text"] = legacyName, ["source"] = "LegacyIAccessible.Name" };

                var legacyValue = element.Patterns.LegacyIAccessible.Pattern.Value.ValueOrDefault;
                if (!string.IsNullOrEmpty(legacyValue))
                    return new JsonObject { ["text"] = legacyValue, ["source"] = "LegacyIAccessible.Value" };
            }
        }
        catch { /* fall through */ }

        // Strategy 5: Visible text descendants
        var descText = GetVisibleDescendantText(element, automation);
        if (!string.IsNullOrEmpty(descText))
            return new JsonObject { ["text"] = descText, ["source"] = "TextDescendants" };

        return new JsonObject { ["text"] = "", ["source"] = "None" };
    }

    // ── Scoring helpers ─────────────────────────────────────────────

    private static int ScoreElement(AutomationElement element, int depth)
    {
        var score = 0;

        // Shallower elements preferred
        score -= depth;

        // Property reads may throw on uncooperative UIA providers. Elements
        // from newly-widened ResolveSearchRoot scans can include unusual
        // top-level windows — silently dropping them from ranking would hide
        // otherwise-valid matches.
        var automationId = SafeString(() => element.AutomationId);
        var controlType = SafeString(() => element.ControlType.ToString());

        // Standard dialog accept/cancel button bonus
        if (controlType == "Button" && (automationId == "1" || automationId == "2"))
            score += 100;

        // DropDown button penalty
        if (string.Equals(automationId, "DropDown", StringComparison.OrdinalIgnoreCase))
            score -= 50;

        // ComboBox child penalty
        try
        {
            var parent = element.Parent;
            if (parent is not null && parent.ControlType == ControlType.ComboBox)
                score -= 50;
        }
        catch { /* ignore */ }

        // Enabled bonus
        try { if (element.IsEnabled) score += 10; } catch { }

        // Visible bonus
        try { if (!element.IsOffscreen) score += 10; } catch { }

        return score;
    }

    private static int GetDepth(AutomationElement element, AutomationElement root)
    {
        const int maxDepth = 20;
        var depth = 0;

        try
        {
            var rootHandle = root.Properties.NativeWindowHandle.ValueOrDefault;
            var current = element;

            while (depth < maxDepth)
            {
                var parent = current.Parent;
                if (parent is null) break;

                try
                {
                    var parentHandle = parent.Properties.NativeWindowHandle.ValueOrDefault;
                    if (parentHandle == rootHandle && rootHandle != IntPtr.Zero)
                        break;
                }
                catch { /* continue walking */ }

                depth++;
                current = parent;
            }
        }
        catch { /* neutral depth */ }

        return depth;
    }

    private static string GetParentDescription(AutomationElement element)
    {
        try
        {
            var parent = element.Parent;
            if (parent is null) return "";
            var parentType = parent.ControlType.ToString();
            var parentName = parent.Name ?? "";
            return string.IsNullOrEmpty(parentName)
                ? parentType
                : $"{parentType} \"{parentName}\"";
        }
        catch { return ""; }
    }

    private static bool IsLikelyCLRTypeName(string text)
    {
        if (string.IsNullOrWhiteSpace(text)) return false;
        if (text.Contains(' ') || !text.Contains('.')) return false;
        // Check pattern: "Namespace.SubNs.ClassName" — segments start with uppercase
        var segments = text.Split('.');
        return segments.Length >= 2 && segments.All(s =>
            s.Length > 0 && char.IsUpper(s[0]) && s.All(c => char.IsLetterOrDigit(c) || c == '_'));
    }

    private static string GetVisibleDescendantText(AutomationElement element, UIA3Automation automation)
    {
        try
        {
            var textChildren = element.FindAllDescendants(
                new ConditionFactory(automation.PropertyLibrary)
                    .ByControlType(ControlType.Text));

            var texts = new List<string>();
            foreach (var child in textChildren)
            {
                try
                {
                    var childName = child.Name;
                    if (!string.IsNullOrEmpty(childName))
                        texts.Add(childName);
                }
                catch { /* skip */ }
            }
            return texts.Count > 0 ? string.Join(" ", texts) : "";
        }
        catch { return ""; }
    }

    // ── Private helpers ──────────────────────────────────────────────

    private static JsonNode BuildTree(AutomationElement element, int maxDepth, int maxChildren, int currentDepth)
    {
        // Skip expensive GetSupportedPatterns in tree walk — only root gets patterns
        var node = BuildElementInfo(element, includePatterns: currentDepth == 0);

        if (currentDepth >= maxDepth)
            return node;

        var children = element.FindAllChildren();
        var childArray = new JsonArray();
        var count = Math.Min(children.Length, maxChildren);

        for (var i = 0; i < count; i++)
        {
            childArray.Add(BuildTree(children[i], maxDepth, maxChildren, currentDepth + 1));
        }

        if (children.Length > maxChildren)
            childArray.Add(new JsonObject { ["truncated"] = true, ["total"] = children.Length });

        node["children"] = childArray;
        return node;
    }

    internal static JsonObject BuildElementInfo(AutomationElement element, bool includePatterns = true)
    {
        // Every property access is wrapped individually because a UIA provider may
        // not implement any given property. An unsupported property throws
        // "The requested property '<Name> [#<id>]' is not supported" which would
        // otherwise abort BuildElementInfo, BuildTree, and the whole get_tree call.
        // WPF modal dialogs in particular are known to lack ClassName (#30012).
        var result = new JsonObject
        {
            ["found"] = true,
            ["automationId"] = SafeString(() => element.AutomationId),
            ["name"] = SafeString(() => element.Name),
            ["controlType"] = SafeString(() => element.ControlType.ToString()),
            ["className"] = SafeString(() => element.ClassName),
            ["rect"] = SafeRect(element),
        };

        if (includePatterns)
        {
            var patterns = new JsonArray();
            try
            {
                var supported = element.GetSupportedPatterns();
                foreach (var p in supported)
                    patterns.Add(p.Name);
            }
            catch
            {
                // Some elements may not support pattern enumeration
            }
            result["patterns"] = patterns;
        }

        return result;
    }

    private static string SafeString(Func<string?> read)
    {
        try { return read() ?? ""; }
        catch { return ""; }
    }

    private static JsonObject SafeRect(AutomationElement element)
    {
        try
        {
            var rect = element.BoundingRectangle;
            return new JsonObject
            {
                ["x"] = rect.X,
                ["y"] = rect.Y,
                ["width"] = rect.Width,
                ["height"] = rect.Height,
            };
        }
        catch
        {
            return new JsonObject
            {
                ["x"] = 0,
                ["y"] = 0,
                ["width"] = 0,
                ["height"] = 0,
            };
        }
    }

    internal static JsonObject BuildExactAutomationIdMismatchPayload(JsonNode? @params)
    {
        var requested = new JsonObject
        {
            ["automationId"] = ParamString(@params, "automationId"),
            ["name"] = ParamString(@params, "name"),
            ["controlType"] = ParamString(@params, "controlType"),
            ["rootAutomationId"] = ParamString(@params, "rootAutomationId"),
            ["xpath"] = ParamString(@params, "xpath"),
        };

        return new JsonObject
        {
            ["status"] = "BLOCKED",
            ["reason"] = "selector result did not match exact automation_id",
            ["requested"] = requested,
            ["accepted"] = new JsonObject
            {
                ["selector_policy"] = "exact automation_id match",
            },
            ["next_step"] =
                "Inspect the scoped tree with ui_get_window_tree or adjust the selector; " +
                "side-effecting UI actions require the returned element to match the " +
                "requested exact automation_id.",
            ["search"] = DescribeSearch(@params),
        };
    }

    private static string? ParamString(JsonNode? @params, string key)
    {
        try { return @params?[key]?.GetValue<string>(); }
        catch { return null; }
    }
}

internal sealed class ExactAutomationIdMismatchException : InvalidOperationException
{
    public ExactAutomationIdMismatchException(JsonObject payload)
        : base("selector result did not match exact automation_id")
    {
        Payload = payload;
    }

    public JsonObject Payload { get; }
}

internal enum GuardedSelectorOutcome
{
    Unique,
    Missing,
    Ambiguous,
    Unobservable,
}

internal sealed record GuardedSelectorResolution(
    GuardedSelectorOutcome Outcome,
    AutomationElement? Element,
    int MatchCount,
    string? Detail);
