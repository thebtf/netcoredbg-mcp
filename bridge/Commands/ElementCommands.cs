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
            throw new ArgumentException("At least one search criterion required: automationId, name, or controlType");

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

        return BuildTree(mainWindow, maxDepth, maxChildren, 0);
    }

    // ── Shared helpers (used by PatternCommands too) ──────────────────

    /// <summary>
    /// Resolve search root: if rootAutomationId is provided, find that element
    /// and use it as the search scope. Otherwise return the mainWindow.
    /// </summary>
    internal static AutomationElement ResolveSearchRoot(
        AutomationElement mainWindow, JsonNode? @params, UIA3Automation automation)
    {
        var rootId = @params?["rootAutomationId"]?.GetValue<string>();
        if (string.IsNullOrWhiteSpace(rootId))
            return mainWindow;

        var cf = new ConditionFactory(automation.PropertyLibrary);
        var root = mainWindow.FindFirstDescendant(cf.ByAutomationId(rootId));
        if (root is null)
            throw new InvalidOperationException(
                $"Root element not found: '{rootId}'. Use get_tree to verify the element exists.");

        return root;
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
                    var descendantText = GetVisibleDescendantText(element);
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
        var descText = GetVisibleDescendantText(element);
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

    private static string GetVisibleDescendantText(AutomationElement element)
    {
        try
        {
            var textChildren = element.FindAllDescendants(
                new ConditionFactory(new UIA3Automation().PropertyLibrary)
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
        var rect = element.BoundingRectangle;

        var result = new JsonObject
        {
            ["found"] = true,
            ["automationId"] = element.AutomationId,
            ["name"] = element.Name,
            ["controlType"] = element.ControlType.ToString(),
            ["className"] = element.ClassName,
            ["rect"] = new JsonObject
            {
                ["x"] = rect.X,
                ["y"] = rect.Y,
                ["width"] = rect.Width,
                ["height"] = rect.Height
            },
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
}
