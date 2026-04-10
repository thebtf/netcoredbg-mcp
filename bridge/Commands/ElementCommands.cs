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

        var desktop = automation.GetDesktop();
        var windows = desktop.FindAllChildren(
            new ConditionFactory(automation.PropertyLibrary)
                .ByProcessId(pid));

        if (windows.Length == 0)
            throw new InvalidOperationException($"No window found for process {pid}");

        var window = windows[0];
        JsonRpcHandler.MainWindow = window;

        Program.Log($"Connected to window: {window.Name} (pid={pid})");

        return new JsonObject
        {
            ["connected"] = true,
            ["title"] = window.Name
        };
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
        if (controlType is not null && Enum.TryParse<ControlType>(controlType, true, out var ct))
            conditions.Add(cf.ByControlType(ct));

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
            return new JsonObject { ["found"] = false };

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

        var primaryName = SafeString(() => mainWindow.Name);

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
        var result = new List<AutomationElement> { mainWindow };
        var seen = new HashSet<IntPtr> { SafeHandle(mainWindow) };

        int pid;
        try
        {
            pid = mainWindow.Properties.ProcessId.Value;
        }
        catch
        {
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
                if (handle != IntPtr.Zero && seen.Add(handle))
                    result.Add(sibling);
            }
        }
        catch
        {
            // Desktop enumeration is best-effort; mainWindow alone is still valid output.
        }

        return result;
    }

    private static IntPtr SafeHandle(AutomationElement element)
    {
        try { return element.Properties.NativeWindowHandle.ValueOrDefault; }
        catch { return IntPtr.Zero; }
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

        foreach (var window in GetProcessTopLevelWindows(mainWindow, automation))
        {
            // The window itself may match rootId by AutomationId or by Name
            // (agents commonly pass the window title as root_id for modal dialogs).
            if (MatchesWindowIdentity(window, rootId))
                return window;

            var descendant = window.FindFirstDescendant(cf.ByAutomationId(rootId));
            if (descendant is not null)
                return descendant;
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

        AutomationElement? match = null;
        foreach (var window in GetProcessTopLevelWindows(mainWindow, automation))
        {
            if (!string.IsNullOrWhiteSpace(automationId))
            {
                try
                {
                    if (window.Properties.AutomationId.IsSupported &&
                        window.Properties.AutomationId.Value == automationId)
                    {
                        match = window;
                        break;
                    }
                }
                catch { /* continue */ }
            }

            if (!string.IsNullOrWhiteSpace(name))
            {
                try
                {
                    if (window.Properties.Name.IsSupported &&
                        window.Properties.Name.Value == name)
                    {
                        match = window;
                        break;
                    }
                }
                catch { /* continue */ }
            }
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
        AutomationElement root, JsonNode? @params, UIA3Automation automation)
    {
        var cf = new ConditionFactory(automation.PropertyLibrary);

        // Priority 1: AutomationId
        var automationId = @params?["automationId"]?.GetValue<string>();
        if (!string.IsNullOrWhiteSpace(automationId))
        {
            var element = root.FindFirstDescendant(cf.ByAutomationId(automationId));
            if (element is not null)
                return element;
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
            if (!string.IsNullOrWhiteSpace(controlType) &&
                Enum.TryParse<ControlType>(controlType, true, out var ct))
                conditions.Add(cf.ByControlType(ct));

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
        if (!string.IsNullOrWhiteSpace(controlType) && Enum.TryParse<ControlType>(controlType, true, out var ct))
            conditions.Add(cf.ByControlType(ct));

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

        var automationId = element.AutomationId ?? "";
        var controlType = element.ControlType.ToString();

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
}
