using System.Diagnostics;
using System.IO;
using System.Text.Json;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Controls;
using System.Windows.Interop;
using System.Windows.Threading;

namespace WpfSmokeApp;

internal sealed class RootLifecycleControl
{
    private const int LifetimeSeconds = 360;
    private readonly Application _application;
    private readonly string _directory;
    private readonly string _processStartTimeUtc;
    private readonly Stopwatch _lifetime = Stopwatch.StartNew();
    private readonly DispatcherTimer _timer;
    private readonly HashSet<Window> _windows = new();
    private readonly Window _primary;
    private Window? _secondary;
    private Window? _tertiary;
    private int _activatedCount;
    private bool _closing;

    public RootLifecycleControl(Application application, string directory)
    {
        _application = application;
        _directory = directory;
        using var process = Process.GetCurrentProcess();
        _processStartTimeUtc = process.StartTime.ToUniversalTime().ToString("O");
        _timer = new DispatcherTimer(DispatcherPriority.Normal)
        {
            Interval = TimeSpan.FromMilliseconds(100),
        };
        _timer.Tick += PollSignals;
        _primary = CreateWindow("primary", "RootLifecycleMain", "RootLifecyclePanel", "RootLifecycleText");
    }

    public void Show()
    {
        _application.MainWindow = _primary;
        _primary.Show();
        if (!_closing)
            _timer.Start();
    }

    private Window CreateWindow(string role, string windowId, string panelId, string textId)
    {
        var duplicate = role != "secondary";
        var text = new TextBox
        {
            Text = $"root-lifecycle-{role}-expected-text",
            IsReadOnly = true,
            Focusable = false,
        };
        AutomationProperties.SetAutomationId(text, textId);
        AutomationProperties.SetName(text, duplicate
            ? "Root lifecycle duplicate text" : "Root lifecycle secondary text");
        var panel = new GroupBox { Content = text, Focusable = false };
        AutomationProperties.SetAutomationId(panel, panelId);
        AutomationProperties.SetName(panel, duplicate
            ? "Root lifecycle duplicate panel" : "Root lifecycle secondary panel");
        var window = new Window
        {
            Title = duplicate ? "Root lifecycle duplicate window" : "Root lifecycle secondary window",
            Content = panel,
            Width = 360,
            Height = 100,
            WindowStyle = WindowStyle.None,
            ResizeMode = ResizeMode.NoResize,
            WindowStartupLocation = WindowStartupLocation.Manual,
            Left = SystemParameters.VirtualScreenLeft - 360 - 4096,
            Top = SystemParameters.VirtualScreenTop - 100 - 4096,
            ShowActivated = false,
            ShowInTaskbar = false,
            Focusable = false,
            IsHitTestVisible = false,
        };
        AutomationProperties.SetAutomationId(window, windowId);
        AutomationProperties.SetName(window, window.Title);
        _windows.Add(window);
        var hwnd = IntPtr.Zero;
        var ready = false;
        window.SourceInitialized += (_, _) => hwnd = new WindowInteropHelper(window).Handle;
        window.ContentRendered += (_, _) =>
        {
            if (!_closing && !ready)
            {
                ready = true;
                Announce("ready", role, window, panel, text, hwnd);
            }
        };
        window.Activated += (_, _) =>
        {
            _activatedCount++;
            Announce("activated", role, window, panel, text, hwnd);
            CloseAll();
        };
        window.Closed += (_, _) =>
        {
            _windows.Remove(window);
            Announce("closed", role, window, panel, text, hwnd);
            if (role == "primary")
                CloseAll();
        };
        return window;
    }

    private void PollSignals(object? sender, EventArgs e)
    {
        if (_lifetime.Elapsed >= TimeSpan.FromSeconds(LifetimeSeconds) || Signaled("close-all"))
        {
            CloseAll();
            return;
        }

        if (_secondary is null && Signaled("open-secondary"))
        {
            _secondary = CreateWindow("secondary", "RootLifecycleSecondary",
                "RootLifecycleSecondaryPanel", "RootLifecycleSecondaryText");
            _secondary.Show();
        }
        if (_closing)
            return;
        if (_secondary?.IsVisible == true && Signaled("close-secondary"))
            _secondary.Close();

        if (_tertiary is null && Signaled("open-tertiary"))
        {
            _tertiary = CreateWindow("tertiary", "RootLifecycleTertiary",
                "RootLifecycleTertiaryPanel", "RootLifecycleTertiaryText");
            _tertiary.Show();
        }
        if (_closing)
            return;
        if (_tertiary?.IsVisible == true && Signaled("close-tertiary"))
            _tertiary.Close();
    }

    private bool Signaled(string name) => File.Exists(Path.Combine(_directory, name));

    private void CloseAll()
    {
        if (_closing)
            return;
        _closing = true;
        _timer.Stop();
        foreach (var window in _windows.ToArray())
            window.Close();
        _application.Shutdown(_activatedCount == 0 ? 0 : 3);
    }

    private void Announce(string eventName, string role, Window window, GroupBox panel,
        TextBox text, IntPtr hwnd)
    {
        Console.WriteLine(JsonSerializer.Serialize(new
        {
            mode = "root-lifecycle-control",
            @event = eventName,
            role,
            pid = Environment.ProcessId,
            processStartTimeUtc = _processStartTimeUtc,
            hwnd = hwnd.ToInt64(),
            automationId = AutomationProperties.GetAutomationId(window),
            name = AutomationName(window),
            panel = new
            {
                automationId = AutomationProperties.GetAutomationId(panel),
                name = AutomationName(panel),
            },
            text = new
            {
                automationId = AutomationProperties.GetAutomationId(text),
                name = AutomationName(text),
                value = text.Text,
            },
            activatedCount = _activatedCount,
            elapsedMilliseconds = _lifetime.ElapsedMilliseconds,
            lifetimeSeconds = LifetimeSeconds,
        }));
    }

    private static string AutomationName(FrameworkElement element) =>
        UIElementAutomationPeer.CreatePeerForElement(element)?.GetName() ??
        throw new InvalidOperationException("Root lifecycle control must expose an automation peer.");
}
