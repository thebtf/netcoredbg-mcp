using System.Windows;
using System.Windows.Automation;
using System.Windows.Automation.Peers;
using System.Windows.Controls;

namespace NativeSceneProbe.WpfFixture;

// This scene is opt-in; existing atomic probe and two-argument invoke fixtures are unchanged.
internal sealed class DiscoveryFixtureScene : IDisposable
{
    private const string ModePrefix = "--native-ui-discovery-mode=";
    private const string EventsPrefix = "--native-ui-discovery-events=";
    private Window _window;
    private readonly StackPanel _root = new();
    private readonly TextBlock _counter = new();
    private readonly List<EventWaitHandle> _events = [];
    private readonly CancellationTokenSource _closed = new();
    private Button _action = null!;
    private int _count;

    private DiscoveryFixtureScene(Window window, string mode, string? eventPrefix)
    {
        _window = window;
        if (mode == "readable")
        {
            window.WindowStyle = WindowStyle.None;
            window.ResizeMode = ResizeMode.NoResize;
            AutomationProperties.SetAutomationId(window, "DiscoveryReadableWindow");
        }
        AutomationProperties.SetAutomationId(_root, "DiscoveryRoot");
        AutomationProperties.SetName(_root, "Discovery fixture root");
        _action = CreateAction("discovered-" + Guid.NewGuid().ToString("N"));
        _root.Children.Add(_action);
        UpdateCounter();
        AutomationProperties.SetAutomationId(_counter, "DiscoveryCounter");
        _root.Children.Add(_counter);

        foreach (var name in new[] { "Duplicate action one", "Duplicate action two" })
        {
            var button = CreateButton(name, "DiscoveryDuplicate");
            _root.Children.Add(button);
            if (mode == "duplicate-status")
            {
                var count = 0;
                var status = new TextBlock { Text = $"{name} count: {count}" };
                AutomationProperties.SetName(status, status.Text);
                button.Click += (_, _) =>
                {
                    count = checked(count + 1);
                    status.Text = $"{name} count: {count}";
                    AutomationProperties.SetName(status, status.Text);
                };
                _root.Children.Add(status);
            }
        }

        if (mode == "selectors")
        {
            _root.Children.Add(CreateButton("Overlong discovery selector", new string('x', 257)));
            _root.Children.Add(CreateButton("Supplementary discovery selector", string.Concat(Enumerable.Repeat("\U0001F600", 256))));
            _root.Children.Add(CreateButton("Overlong supplementary selector", string.Concat(Enumerable.Repeat("\U0001F600", 257))));
        }

        if (mode == "wide")
        {
            for (var index = 0; index < 300; index++)
                _root.Children.Add(CreateButton($"Wide node {index}", $"WideNode{index}"));
        }
        else if (mode == "deep")
        {
            var parent = _root;
            for (var index = 0; index < 40; index++)
            {
                var child = new GroupBox { Header = $"Depth {index}" };
                AutomationProperties.SetAutomationId(child, $"DepthNode{index}");
                var next = new StackPanel();
                child.Content = next;
                parent.Children.Add(child);
                parent = next;
            }
            parent.Children.Add(CreateButton("Deep hidden action", "DeepHiddenAction"));
        }
        else if (mode is "overflow" or "large-fields")
        {
            for (var groupIndex = 0; groupIndex < (mode == "overflow" ? 24 : 4); groupIndex++)
            {
                var group = new GroupBox { Header = $"Group {groupIndex}" };
                var children = new StackPanel();
                group.Content = children;
                _root.Children.Add(group);
                for (var index = 0; index < 200; index++)
                {
                    var name = mode == "large-fields" ? new string('x', 4096) + $"-{groupIndex}-{index}" : $"Node {groupIndex}-{index}";
                    children.Children.Add(CreateButton(name, $"LargeNode{groupIndex}_{index}"));
                }
            }
        }
        else if (mode == "unreadable")
        {
            _root.Children.Add(new DiscoveryUnreadableButton { Content = "Unreadable selector" });
        }

        if (eventPrefix is not null)
        {
            var arm = OpenEvent(eventPrefix, "arm");
            var entered = OpenEvent(eventPrefix, "entered");
            var release = OpenEvent(eventPrefix, "release");
            var replace = OpenEvent(eventPrefix, "replace");
            var replaced = OpenEvent(eventPrefix, "replaced");
            var duplicate = OpenEvent(eventPrefix, "duplicate");
            var duplicated = OpenEvent(eventPrefix, "duplicated");
            var ready = OpenEvent(eventPrefix, "ready");
            var loaded = OpenEvent(eventPrefix, "window-loaded");
            window.Loaded += (_, _) => loaded.Set();
            window.ContentRendered += (_, _) => ready.Set();
            _ = RunMutationAsync(OpenEvent(eventPrefix, "barrier"), OpenEvent(eventPrefix, "barrier-complete"), () => { }, repeat: true);
            _root.Children.Add(new DiscoveryBlockedButton(arm, entered, release) { Content = "Provider gate" });
            _ = RunMutationAsync(replace, replaced, () =>
            {
                var index = _root.Children.IndexOf(_action);
                var automationId = AutomationProperties.GetAutomationId(_action);
                _root.Children.RemoveAt(index);
                _action = CreateAction(automationId);
                _root.Children.Insert(index, _action);
            });
            _ = RunMutationAsync(OpenEvent(eventPrefix, "rename-action"), OpenEvent(eventPrefix, "action-renamed"), () =>
                AutomationProperties.SetAutomationId(_action, "renamed-" + Guid.NewGuid().ToString("N")));
            _ = RunMutationAsync(duplicate, duplicated, () =>
                _root.Children.Add(CreateButton("Late duplicate action", AutomationProperties.GetAutomationId(_action))));
            _ = RunMutationAsync(OpenEvent(eventPrefix, "replace-window"), OpenEvent(eventPrefix, "window-replaced"), () =>
            {
                var previous = _window;
                var content = previous.Content;
                previous.Content = null;
                var replacement = new Window
                {
                    Title = "Replacement discovery window",
                    Width = 900,
                    Height = 640,
                    ShowActivated = false,
                    Content = content,
                };
                AutomationProperties.SetName(replacement, "Replacement discovery window");
                Application.Current.MainWindow = replacement;
                _window = replacement;
                replacement.Show();
                previous.Close();
            });
        }

        window.Content = mode == "readable" ? _root : new ScrollViewer { Content = _root };
        if (eventPrefix is not null)
            OpenEvent(eventPrefix, "scene-setup-complete").Set();
    }

    internal static DiscoveryFixtureScene? Attach(Window window, string[] arguments)
    {
        var mode = arguments.SingleOrDefault(value => value.StartsWith(ModePrefix, StringComparison.Ordinal));
        if (mode is null)
            return null;
        var selected = mode[ModePrefix.Length..];
        if (selected is not ("basic" or "wide" or "deep" or "overflow" or "large-fields" or "unreadable" or "selectors" or "duplicate-status" or "readable"))
            throw new ArgumentException("Unknown discovery fixture mode.");
        var events = arguments.SingleOrDefault(value => value.StartsWith(EventsPrefix, StringComparison.Ordinal));
        return new DiscoveryFixtureScene(window, selected, events?[EventsPrefix.Length..]);
    }

    private Button CreateAction(string automationId) => CreateButton("Controlled discovery action", automationId);

    private Button CreateButton(string name, string automationId)
    {
        var button = new Button { Content = name, Height = 24 };
        AutomationProperties.SetName(button, name);
        AutomationProperties.SetAutomationId(button, automationId);
        button.Click += (_, _) =>
        {
            _count = checked(_count + 1);
            UpdateCounter();
        };
        return button;
    }

    private void UpdateCounter()
    {
        _counter.Text = $"Controlled discovery count: {_count}";
        AutomationProperties.SetName(_counter, _counter.Text);
    }

    private EventWaitHandle OpenEvent(string prefix, string suffix)
    {
        var handle = EventWaitHandle.OpenExisting(prefix + "-" + suffix);
        _events.Add(handle);
        return handle;
    }

    private async Task RunMutationAsync(EventWaitHandle requested, EventWaitHandle completed, Action mutation, bool repeat = false)
    {
        do
        {
            var selected = await Task.Run(() => WaitHandle.WaitAny([requested, _closed.Token.WaitHandle]));
            if (selected != 0 || _closed.IsCancellationRequested)
                return;
            try
            {
                // ApplicationIdle runs after input-priority InvokePattern handlers. The
                // acknowledgement is an ordered counter barrier, not a sleep-based oracle.
                await _window.Dispatcher.InvokeAsync(() =>
                {
                    mutation();
                    _window.UpdateLayout();
                    completed.Set();
                }, System.Windows.Threading.DispatcherPriority.ApplicationIdle);
            }
            catch (TaskCanceledException) when (_closed.IsCancellationRequested)
            {
                return;
            }
        } while (repeat);
    }

    public void Dispose()
    {
        _closed.Cancel();
        // Provider and mutation callbacks can still own these handles during shutdown;
        // process exit closes them after the outstanding callbacks stop.
    }
}

internal sealed class DiscoveryBlockedButton(EventWaitHandle arm, EventWaitHandle entered, EventWaitHandle release) : Button
{
    protected override AutomationPeer OnCreateAutomationPeer() => new Peer(this, arm, entered, release);

    private sealed class Peer(DiscoveryBlockedButton owner, EventWaitHandle arm, EventWaitHandle entered, EventWaitHandle release)
        : ButtonAutomationPeer(owner)
    {
        protected override string GetNameCore()
        {
            if (arm.WaitOne(0))
            {
                entered.Set();
                // The test owns release. No time delay substitutes for a pending provider call.
                release.WaitOne();
            }
            return "Provider gate";
        }
    }
}

internal sealed class DiscoveryUnreadableButton : Button
{
    protected override AutomationPeer OnCreateAutomationPeer() => new Peer(this);

    private sealed class Peer(DiscoveryUnreadableButton owner) : ButtonAutomationPeer(owner)
    {
        protected override string GetAutomationIdCore() => throw new InvalidOperationException("Controlled unreadable AutomationId.");
        protected override string GetNameCore() => "Unreadable selector";
    }
}
