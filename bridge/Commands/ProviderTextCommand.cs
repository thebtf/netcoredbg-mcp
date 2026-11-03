using System.Globalization;
using System.IO;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using FlaUI.Core;
using FlaUI.Core.AutomationElements;
using FlaUI.Core.Definitions;
using FlaUI.Core.Patterns;
using FlaUI.UIA3;
using Microsoft.Win32.SafeHandles;

namespace FlaUIBridge.Commands;

internal static class ProviderTextCommand
{
    internal const int MaximumNodes = 4_096;

    internal static void RunStdio()
    {
        try
        {
            FlaUI.Core.Logging.Logger.Default = new FlaUI.Core.Logging.NullLogger();
            using var reader = new StreamReader(Console.OpenStandardInput(), new UTF8Encoding(false, true),
                detectEncodingFromByteOrderMarks: false, leaveOpen: true);
            RunStdio(reader, Console.Out, NativeProviderTextSession.Open);
        }
        catch (Exception)
        {
            // A lost transport has no correlated outcome. Do not log request or provider data.
        }
    }

    internal static void RunStdio(TextReader input, TextWriter output,
        Func<int, ulong, IProviderTextSession> openSession)
    {
        output.Write("{\"protocol\":\"netcoredbg.provider-text.v1\",\"ready\":true}\n");
        output.Flush();
        string? id = null;
        var result = Blocked("invalid_request");
        try
        {
            var line = new StringBuilder();
            int next;
            while ((next = input.Read()) != -1 && next != '\n')
                line.Append((char)next);
            using var document = JsonDocument.Parse(line.ToString(), new JsonDocumentOptions { MaxDepth = 8 });
            var envelope = document.RootElement;
            if (envelope.ValueKind == JsonValueKind.Object && envelope.TryGetProperty("id", out var idField) &&
                idField.ValueKind == JsonValueKind.String)
                id = idField.GetString();
            // EOF is required: a second line, even an empty one, cannot be dispatched.
            if (next == '\n' && input.Read() == -1 && TryParse(envelope, out var request))
                result = Execute(request!, openSession);
        }
        catch (Exception)
        {
            // Parsing and admission failures are pre-effect and always sanitized.
        }
        output.WriteLine(new JsonObject { ["jsonrpc"] = "2.0", ["id"] = id, ["result"] = result }.ToJsonString());
        output.Flush();
    }

    private static bool TryParse(JsonElement envelope, out ProviderTextRequest? request)
    {
        request = null;
        if (!ExactObject(envelope, "jsonrpc", "id", "method", "params") ||
            !String(envelope, "jsonrpc", out var version) || version != "2.0" ||
            !String(envelope, "id", out _) ||
            !String(envelope, "method", out var method) || method != "provider_text.set")
            return false;
        var parameters = envelope.GetProperty("params");
        if (!ExactObject(parameters, "pid", "creation_time", "selector", "text") ||
            !parameters.GetProperty("pid").TryGetInt32(out var pid) || pid <= 0 ||
            !String(parameters, "creation_time", out var creationText) || creationText.Length == 0 ||
            creationText.Any(character => character is < '0' or > '9') ||
            !ulong.TryParse(creationText, NumberStyles.None, CultureInfo.InvariantCulture, out var creation) || creation == 0 ||
            !String(parameters, "text", out var text) || !ValidLiteral(text))
            return false;
        var selector = parameters.GetProperty("selector");
        if (selector.ValueKind != JsonValueKind.Object)
            return false;
        string? automationId = null, name = null, controlType = null;
        var keys = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in selector.EnumerateObject())
        {
            if (!keys.Add(property.Name) || property.Name is not ("automation_id" or "name" or "control_type") ||
                property.Value.ValueKind != JsonValueKind.String || property.Value.GetString() is not { } value ||
                string.IsNullOrWhiteSpace(value) || !ValidLiteral(value))
                return false;
            switch (property.Name)
            {
                case "automation_id": automationId = value; break;
                case "name": name = value; break;
                case "control_type":
                    // Public TextBox is an explicit alias, not a fuzzy/case-insensitive match.
                    controlType = value == "TextBox" ? nameof(ControlType.Edit) : value;
                    if (!Enum.GetNames<ControlType>().Contains(controlType, StringComparer.Ordinal))
                        return false;
                    break;
            }
        }
        if (keys.Count == 0)
            return false;
        request = new(pid, creation, new(automationId, name, controlType), text);
        return true;
    }

    private static bool ExactObject(JsonElement value, params string[] names)
    {
        if (value.ValueKind != JsonValueKind.Object)
            return false;
        var seen = new HashSet<string>(StringComparer.Ordinal);
        foreach (var property in value.EnumerateObject())
            if (!names.Contains(property.Name, StringComparer.Ordinal) || !seen.Add(property.Name))
                return false;
        return seen.Count == names.Length;
    }

    private static bool String(JsonElement source, string name, out string value)
    {
        value = string.Empty;
        if (!source.TryGetProperty(name, out var field) || field.ValueKind != JsonValueKind.String || field.GetString() is not { } text)
            return false;
        value = text;
        return true;
    }

    private static bool ValidLiteral(string value)
    {
        for (var index = 0; index < value.Length; index++)
        {
            var character = value[index];
            if (character == '\0' || char.IsLowSurrogate(character))
                return false;
            if (char.IsHighSurrogate(character) &&
                (++index == value.Length || !char.IsLowSurrogate(value[index])))
                return false;
        }
        return true;
    }

    private static JsonObject Execute(ProviderTextRequest request, Func<int, ulong, IProviderTextSession> openSession)
    {
        IProviderTextSession? session = null;
        var effectStarted = false;
        var setterReturned = false;
        try
        {
            session = openSession(request.Pid, request.CreationTime);
            if (!session.IsCurrent())
                throw new ProviderTextRefusal("process_changed");
            var target = Resolve(session, request);
            EnsureEditable(target.Element);
            var value = target.Element.GetValue() ?? throw new ProviderTextRefusal("value_unsupported");
            if (value.IsReadOnly)
                throw new ProviderTextRefusal("value_read_only");
            _ = value.Read(); // Refuse an unreadable provider before any mutation; never retain or emit this text.
            EnsureEditable(target.Element);
            if (value.IsReadOnly)
                throw new ProviderTextRefusal("value_read_only");
            // Re-observe uniqueness after preflight; this is not an atomic UIA snapshot.
            ProviderTextTarget currentTarget;
            try
            {
                currentTarget = Resolve(session, request);
            }
            catch (ProviderTextRefusal refusal) when (refusal.Reason == "target_unavailable")
            {
                throw new ProviderTextRefusal("target_changed");
            }
            if (currentTarget.Hwnd != target.Hwnd ||
                !currentTarget.RootRuntimeId.SequenceEqual(target.RootRuntimeId) ||
                !currentTarget.ElementRuntimeId.SequenceEqual(target.ElementRuntimeId))
                throw new ProviderTextRefusal("target_changed");
            EnsureCurrentTarget(session, request, target);
            // The retained process handle is checked last, immediately before the sole provider mutation.
            if (!session.IsCurrent())
                throw new ProviderTextRefusal("process_changed");
            effectStarted = true;
            value.Set(request.Text);
            setterReturned = true;
            EnsureCurrentTarget(session, request, target);
            var actual = value.Read();
            EnsureCurrentTarget(session, request, target);
            return string.Equals(actual, request.Text, StringComparison.Ordinal)
                ? Result("PASS", "completed", true, "verified")
                : Result("FAIL", "completed", false, "readback_mismatch");
        }
        catch (ProviderTextRefusal refusal) when (!effectStarted)
        {
            return Blocked(refusal.Reason);
        }
        catch (Exception)
        {
            return effectStarted
                ? Result("FAIL", "unknown", false, setterReturned ? "readback_unavailable" : "setter_failed")
                : Blocked("target_unreadable");
        }
        finally
        {
            try { session?.Dispose(); }
            catch (Exception) { } // Only provider/handle disposal; never input or shared bridge cleanup.
        }
    }

    private static ProviderTextTarget Resolve(IProviderTextSession session, ProviderTextRequest request)
    {
        var windows = session.GetWindows();
        if (windows.Count > MaximumNodes)
            throw new ProviderTextRefusal("tree_limit_exceeded");
        var discovered = 0;
        ProviderTextTarget? target = null;
        foreach (var hwnd in windows)
        {
            if (!session.IsCurrent())
                throw new ProviderTextRefusal("process_changed");
            if (!session.IsWindowBound(hwnd))
                throw new ProviderTextRefusal("target_changed");
            var root = session.GetRoot(hwnd);
            var rootIdentity = ReadBound(root, request.Pid);
            if (rootIdentity.Hwnd != hwnd)
                throw new ProviderTextRefusal("target_changed");
            var pending = new Queue<ProviderTextNode>();
            if (++discovered > MaximumNodes)
                throw new ProviderTextRefusal("tree_limit_exceeded");
            pending.Enqueue(root);
            while (pending.Count != 0)
            {
                var current = pending.Dequeue();
                var snapshot = ReferenceEquals(current, root) ? rootIdentity : ReadBound(current, request.Pid);
                if (Matches(snapshot, request.Selector))
                {
                    if (target is not null)
                        throw new ProviderTextRefusal("target_ambiguous");
                    target = new(hwnd, rootIdentity.RuntimeId, current, snapshot.RuntimeId);
                }
                var child = current.FirstChild();
                while (child is not null)
                {
                    if (++discovered > MaximumNodes)
                        throw new ProviderTextRefusal("tree_limit_exceeded");
                    pending.Enqueue(child);
                    child = child.NextSibling();
                }
            }
        }
        return target ?? throw new ProviderTextRefusal("target_unavailable");
    }

    private static void EnsureCurrentTarget(IProviderTextSession session, ProviderTextRequest request, ProviderTextTarget target)
    {
        if (!session.IsCurrent())
            throw new ProviderTextRefusal("process_changed");
        if (!session.IsWindowBound(target.Hwnd))
            throw new ProviderTextRefusal("target_changed");
        var root = ReadBound(session.GetRoot(target.Hwnd), request.Pid);
        if (root.Hwnd != target.Hwnd || !root.RuntimeId.SequenceEqual(target.RootRuntimeId))
            throw new ProviderTextRefusal("target_changed");
        var selected = ReadBound(target.Element, request.Pid);
        if (!selected.RuntimeId.SequenceEqual(target.ElementRuntimeId) || !Matches(selected, request.Selector))
            throw new ProviderTextRefusal("target_changed");
        ProviderTextNode? ancestor = target.Element;
        for (var depth = 0; ancestor is not null && depth < MaximumNodes; depth++)
        {
            var identity = ReferenceEquals(ancestor, target.Element) ? selected : ReadBound(ancestor, request.Pid);
            if (identity.RuntimeId.SequenceEqual(target.RootRuntimeId))
            {
                if (identity.Hwnd != target.Hwnd || !session.IsWindowBound(target.Hwnd))
                    throw new ProviderTextRefusal("target_changed");
                var finalRoot = ReadBound(session.GetRoot(target.Hwnd), request.Pid);
                var finalElement = ReadBound(target.Element, request.Pid);
                if (finalRoot.Hwnd != target.Hwnd || !finalRoot.RuntimeId.SequenceEqual(target.RootRuntimeId) ||
                    !finalElement.RuntimeId.SequenceEqual(target.ElementRuntimeId) || !Matches(finalElement, request.Selector))
                    throw new ProviderTextRefusal("target_changed");
                if (!session.IsCurrent())
                    throw new ProviderTextRefusal("process_changed");
                return;
            }
            ancestor = ancestor.GetParent();
        }
        throw new ProviderTextRefusal("target_changed");
    }

    private static ProviderTextSnapshot ReadBound(ProviderTextNode element, int pid)
    {
        var snapshot = element.Read();
        if (snapshot.Pid != pid)
            throw new ProviderTextRefusal("target_changed");
        if (snapshot.RuntimeId is not { Length: >= 1 and <= 64 } ||
            snapshot.AutomationId is null || snapshot.Name is null || snapshot.ControlType is null)
            throw new ProviderTextRefusal("target_unreadable");
        return snapshot;
    }

    private static bool Matches(ProviderTextSnapshot snapshot, ProviderTextSelector selector) =>
        (selector.AutomationId is null || string.Equals(snapshot.AutomationId, selector.AutomationId, StringComparison.Ordinal)) &&
        (selector.Name is null || string.Equals(snapshot.Name, selector.Name, StringComparison.Ordinal)) &&
        (selector.ControlType is null || string.Equals(snapshot.ControlType, selector.ControlType, StringComparison.Ordinal));

    private static void EnsureEditable(ProviderTextNode element)
    {
        if (element.IsPassword())
            throw new ProviderTextRefusal("target_password");
        if (!element.IsEnabled())
            throw new ProviderTextRefusal("target_disabled");
    }

    private static JsonObject Blocked(string reason) => Result("BLOCKED", "not_started", false, reason);
    private static JsonObject Result(string status, string effect, bool verified, string reason) => new()
    {
        ["status"] = status,
        ["effect_state"] = effect,
        ["verified"] = verified,
        ["reason"] = reason,
    };

    private sealed record ProviderTextRequest(int Pid, ulong CreationTime, ProviderTextSelector Selector, string Text);
    private sealed record ProviderTextSelector(string? AutomationId, string? Name, string? ControlType);
    private sealed record ProviderTextTarget(nint Hwnd, int[] RootRuntimeId, ProviderTextNode Element, int[] ElementRuntimeId);
}

internal interface IProviderTextSession : IDisposable
{
    bool IsCurrent();
    IReadOnlyList<nint> GetWindows();
    bool IsWindowBound(nint hwnd);
    ProviderTextNode GetRoot(nint hwnd);
}

internal abstract class ProviderTextNode
{
    internal abstract ProviderTextSnapshot Read();
    internal abstract ProviderTextNode? FirstChild();
    internal abstract ProviderTextNode? NextSibling();
    internal abstract ProviderTextNode? GetParent();
    internal abstract bool IsEnabled();
    internal abstract bool IsPassword();
    internal abstract IProviderTextValue? GetValue();
}

internal sealed record ProviderTextSnapshot(int Pid, int[] RuntimeId, nint Hwnd, string AutomationId, string Name, string ControlType);

internal interface IProviderTextValue
{
    bool IsReadOnly { get; }
    string Read();
    void Set(string text);
}

internal sealed class ProviderTextRefusal(string reason) : Exception
{
    internal string Reason { get; } = reason;
}

internal sealed class NativeProviderTextSession : IProviderTextSession
{
    private const uint QueryLimitedInformation = 0x1000;
    private const uint Synchronize = 0x00100000;
    private const uint WaitTimeout = 258;
    private readonly SafeProcessHandle _process;
    private readonly int _pid;
    private readonly ulong _creationTime;
    private readonly UIA3Automation _automation;
    private readonly ITreeWalker _walker;

    private NativeProviderTextSession(SafeProcessHandle process, int pid, ulong creationTime, UIA3Automation automation)
    {
        _process = process;
        _pid = pid;
        _creationTime = creationTime;
        _automation = automation;
        _walker = automation.TreeWalkerFactory.GetRawViewWalker();
    }

    internal static IProviderTextSession Open(int pid, ulong creationTime)
    {
        var handle = OpenProcess(QueryLimitedInformation | Synchronize, false, pid);
        UIA3Automation? automation = null;
        try
        {
            if (handle.IsInvalid)
                throw new ProviderTextRefusal("process_unavailable");
            if (!IsProcessCurrent(handle, pid, creationTime))
                throw new ProviderTextRefusal("process_changed");
            automation = new UIA3Automation();
            ConfigureAutomation(automation);
            return new NativeProviderTextSession(handle, pid, creationTime, automation);
        }
        catch
        {
            try { automation?.Dispose(); }
            finally { handle.Dispose(); }
            throw;
        }
    }

    internal static void ConfigureAutomation(UIA3Automation automation)
    {
        // Disable built-in focusing on this instance, not a promise about UIA/provider focus changes.
        automation.NativeAutomation2.AutoSetFocus = 0;
        if (automation.NativeAutomation2.AutoSetFocus != 0)
            throw new ProviderTextRefusal("target_unreadable");
    }

    public bool IsCurrent() => IsProcessCurrent(_process, _pid, _creationTime);

    private static bool IsProcessCurrent(SafeProcessHandle handle, int pid, ulong expectedCreation) =>
        !handle.IsInvalid && !handle.IsClosed && WaitForSingleObject(handle, 0) == WaitTimeout &&
        GetProcessId(handle) == (uint)pid && GetProcessTimes(handle, out var creation, out _, out _, out _) &&
        (((ulong)creation.High << 32) | creation.Low) == expectedCreation && WaitForSingleObject(handle, 0) == WaitTimeout;

    public IReadOnlyList<nint> GetWindows()
    {
        var windows = new List<nint>();
        var visited = 0;
        string? refusal = null;
        var complete = EnumWindows((hwnd, _) =>
        {
            if (++visited > ProviderTextCommand.MaximumNodes)
            {
                refusal = "tree_limit_exceeded";
                return false;
            }
            if (GetWindowThreadProcessId(hwnd, out var pid) == 0)
            {
                refusal = "target_unreadable";
                return false;
            }
            if (pid == (uint)_pid)
                windows.Add(hwnd);
            return true;
        }, 0);
        if (!complete)
            throw new ProviderTextRefusal(refusal ?? "target_unreadable");
        return windows;
    }

    public bool IsWindowBound(nint hwnd) => hwnd != 0 && IsWindow(hwnd) && GetAncestor(hwnd, 2) == hwnd &&
        GetWindowThreadProcessId(hwnd, out var pid) != 0 && pid == (uint)_pid;

    public ProviderTextNode GetRoot(nint hwnd) => new NativeProviderTextNode(_automation.FromHandle(hwnd), _walker);

    public void Dispose()
    {
        try { _automation.Dispose(); }
        finally { _process.Dispose(); }
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime { public uint Low; public uint High; }
    private delegate bool EnumWindowsCallback(nint hwnd, nint parameter);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeProcessHandle OpenProcess(uint desiredAccess, [MarshalAs(UnmanagedType.Bool)] bool inheritHandle, int processId);
    [DllImport("kernel32.dll")]
    private static extern uint WaitForSingleObject(SafeProcessHandle handle, uint milliseconds);
    [DllImport("kernel32.dll")]
    private static extern uint GetProcessId(SafeProcessHandle handle);
    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetProcessTimes(SafeProcessHandle handle, out FileTime creation, out FileTime exit, out FileTime kernel, out FileTime user);
    [DllImport("user32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool EnumWindows(EnumWindowsCallback callback, nint parameter);
    [DllImport("user32.dll")]
    private static extern uint GetWindowThreadProcessId(nint hwnd, out uint processId);
    [DllImport("user32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool IsWindow(nint hwnd);
    [DllImport("user32.dll")]
    private static extern nint GetAncestor(nint hwnd, uint flags);
}

internal sealed class NativeProviderTextNode(AutomationElement element, ITreeWalker walker) : ProviderTextNode
{
    internal override ProviderTextSnapshot Read()
    {
        var properties = element.Properties;
        if (!properties.ProcessId.TryGetValue(out var pid) || !properties.RuntimeId.TryGetValue(out var runtimeId) ||
            !properties.NativeWindowHandle.TryGetValue(out var hwnd) || !properties.AutomationId.TryGetValue(out var automationId) ||
            !properties.Name.TryGetValue(out var name) || !properties.ControlType.TryGetValue(out var controlType) ||
            runtimeId is null || automationId is null || name is null)
            throw new ProviderTextRefusal("target_unreadable");
        return new(pid, runtimeId, hwnd, automationId, name, controlType.ToString());
    }

    internal override ProviderTextNode? FirstChild() => Wrap(walker.GetFirstChild(element));
    internal override ProviderTextNode? NextSibling() => Wrap(walker.GetNextSibling(element));
    internal override ProviderTextNode? GetParent() => Wrap(walker.GetParent(element));
    private NativeProviderTextNode? Wrap(AutomationElement? value) => value is null ? null : new(value, walker);
    internal override bool IsEnabled() => element.Properties.IsEnabled.TryGetValue(out var enabled)
        ? enabled : throw new ProviderTextRefusal("target_unreadable");
    internal override bool IsPassword() => element.Properties.IsPassword.TryGetValue(out var password)
        ? password : throw new ProviderTextRefusal("target_unreadable");
    internal override IProviderTextValue? GetValue() => element.Patterns.Value.TryGetPattern(out var value)
        ? new NativeProviderTextValue(value) : null;
}

internal sealed class NativeProviderTextValue(IValuePattern value) : IProviderTextValue
{
    public bool IsReadOnly => value.IsReadOnly.TryGetValue(out var readOnly)
        ? readOnly : throw new ProviderTextRefusal("target_unreadable");
    public string Read() => value.Value.TryGetValue(out var text) && text is not null
        ? text : throw new ProviderTextRefusal("target_unreadable");
    public void Set(string text) => value.SetValue(text);
}
