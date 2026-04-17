using System.Text.Json.Nodes;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.WindowsAPI;
using FlaUI.UIA3;
using FlaUIBridge.Commands;

namespace FlaUIBridge;

public static class JsonRpcHandler
{
    private static UIA3Automation? _automation;
    private static AutomationElement? _mainWindow;
    private static int _processId;
    public static HashSet<VirtualKeyShort> HeldModifiers = new();
    public static readonly object HeldModifiersLock = new();

    internal static UIA3Automation Automation
    {
        get
        {
            _automation ??= new UIA3Automation();
            return _automation;
        }
    }

    // MainWindow is a process-wide static. This is safe because the bridge is
    // spawned as a dedicated subprocess per MCP session (see FlaUIBridgeClient
    // in src/netcoredbg_mcp/ui/flaui_client.py) — each session has its own
    // bridge process and therefore its own MainWindow. Sharing one bridge
    // across sessions would require reworking this into per-session state.
    internal static AutomationElement? MainWindow
    {
        get => _mainWindow;
        set => _mainWindow = value;
    }

    // ProcessId is captured at connect() and retained independently from
    // MainWindow so that window enumeration still works after set_active_window
    // has switched the tracked reference to a dialog that subsequently closes.
    // Reading pid from a stale AutomationElement would throw, leaving the
    // bridge unable to find any top-level window afterwards.
    internal static int ProcessId
    {
        get => _processId;
        set => _processId = value;
    }

    private static readonly IReadOnlyDictionary<string, Func<JsonNode?, UIA3Automation, AutomationElement?, JsonNode>> Handlers =
        new Dictionary<string, Func<JsonNode?, UIA3Automation, AutomationElement?, JsonNode>>
        {
            ["ping"] = PingCommand.Handle,
            ["connect"] = ElementCommands.Connect,
            ["find_element"] = ElementCommands.FindElement,
            ["get_tree"] = ElementCommands.GetTree,
            ["set_active_window"] = ElementCommands.SetActiveWindow,
            ["click"] = ClickCommands.Click,
            ["right_click"] = ClickCommands.RightClick,
            ["double_click"] = ClickCommands.DoubleClick,
            ["drag"] = ClickCommands.Drag,
            ["send_system_event"] = SystemEventCommands.SendSystemEvent,
            ["hold_modifiers"] = ModifierCommands.HoldModifiers,
            ["release_modifiers"] = ModifierCommands.ReleaseModifiers,
            ["get_held_modifiers"] = ModifierCommands.GetHeldModifiers,
            ["send_keys"] = InputCommands.SendKeys,
            ["send_keys_batch"] = InputCommands.SendKeysBatch,
            ["set_value"] = InputCommands.SetValue,
            ["multi_select"] = SelectionCommands.MultiSelect,
            ["expand_collapse"] = SelectionCommands.ExpandCollapse,
            ["screenshot"] = ScreenshotCommands.Screenshot,
            ["invoke_element"] = PatternCommands.InvokeElement,
            ["toggle_element"] = PatternCommands.ToggleElement,
            ["find_by_xpath"] = ElementCommands.FindByXPath,
            ["find_all_cascade"] = ElementCommands.FindAllCascade,
            ["extract_text"] = ElementCommands.ExtractText,
            ["set_focus"] = FocusCommands.SetFocus,
        };

    public static JsonNode Handle(string method, JsonNode? @params)
    {
        if (!Handlers.TryGetValue(method, out var handler))
        {
            throw new InvalidOperationException($"Unknown method: {method}");
        }

        try
        {
            return handler(@params, Automation, MainWindow);
        }
        catch (Exception ex)
        {
            Program.Log($"Error in handler '{method}': {ex}");
            throw;
        }
    }

    public static void Dispose()
    {
        ModifierCommands.ReleaseAllHeldModifiers();
        _automation?.Dispose();
        _automation = null;
        _mainWindow = null;
        _processId = 0;
    }
}
