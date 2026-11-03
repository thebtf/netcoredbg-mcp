using System.IO;
using System.Text.Json;
using System.Text.Json.Nodes;
using FlaUIBridge.Commands;

var passed = 0;
void Check(string name, Action action)
{
    try { action(); passed++; Console.WriteLine($"PASS {name}"); }
    catch { Console.Error.WriteLine($"FAIL {name}"); Environment.ExitCode = 1; }
}
void Require(bool value) { if (!value) throw new InvalidOperationException(); }
JsonObject Request(string text = "replacement") => new()
{
    ["jsonrpc"] = "2.0", ["id"] = "test-correlation", ["method"] = "provider_text.set",
    ["params"] = new JsonObject
    {
        ["pid"] = 42, ["creation_time"] = "133700000000000000",
        ["selector"] = new JsonObject { ["automation_id"] = "field", ["name"] = "Exact field", ["control_type"] = "TextBox" },
        ["text"] = text,
    },
};
JsonObject Envelope(string wire)
{
    var lines = wire.Split('\n');
    Require(lines.Length == 3 && lines[2] == "");
    var ready = JsonNode.Parse(lines[0])!.AsObject();
    Require(ready.Count == 2 && ready["protocol"]!.GetValue<string>() == "netcoredbg.provider-text.v1" &&
        ready["ready"]!.GetValue<bool>() && !lines[0].EndsWith('\r'));
    return JsonNode.Parse(lines[1])!.AsObject();
}
JsonObject Run(JsonObject request, FakeSession session, string suffix = "")
{
    var output = new FlushProbeWriter();
    var input = new ReadProbeReader(request.ToJsonString() + "\n" + suffix, output);
    ProviderTextCommand.RunStdio(input, output, (pid, creation) =>
    {
        session.OpenCalls++;
        Require(pid == 42 && creation == 133700000000000000);
        return session;
    });
    Require(input.ReadCalls > 0 && input.BeforeFirstRead == "{\"protocol\":\"netcoredbg.provider-text.v1\",\"ready\":true}\n");
    var envelope = Envelope(output.ToString());
    Require(envelope.Count == 3 && envelope["jsonrpc"]!.GetValue<string>() == "2.0" &&
        envelope["id"]!.GetValue<string>() == "test-correlation");
    Require(!output.ToString().Contains("replacement", StringComparison.Ordinal));
    Require(!output.ToString().Contains("private provider exception", StringComparison.Ordinal));
    Require(!output.ToString().Contains("original private field", StringComparison.Ordinal));
    return envelope["result"]!.AsObject();
}
void Outcome(JsonObject result, string status, string effect, bool verified) =>
    Require(result["status"]!.GetValue<string>() == status && result["effect_state"]!.GetValue<string>() == effect &&
        result["verified"]!.GetValue<bool>() == verified && result.Count == 4);
void Blocked(FakeSession session, string reason)
{
    var result = Run(Request(), session);
    Outcome(result, "BLOCKED", "not_started", false);
    Require(result["reason"]!.GetValue<string>() == reason && session.Field.Value.SetCalls == 0);
}

Check("ready banner flushed before first input read", () =>
{
    var session = new FakeSession();
    Outcome(Run(Request(), session), "PASS", "completed", true);
    Require(session.Field.Value.SetCalls == 1 && session.Disposed);
});
foreach (var text in new[] { "", "replacement", "a\r\nb\tЖ😀e\u0301\u2028", new string('x', 100_000) })
    Check($"literal text {text.Length}", () =>
    {
        var session = new FakeSession();
        Outcome(Run(Request(text), session), "PASS", "completed", true);
        Require(session.Field.Value.SetCalls == 1 && session.Field.Value.Text == text && session.Disposed);
    });
Check("native Edit selector", () =>
{
    var request = Request(); request["params"]!["selector"]!["control_type"] = "Edit";
    var session = new FakeSession(); Outcome(Run(request, session), "PASS", "completed", true);
    Require(session.Field.Value.SetCalls == 1);
});
Check("nonmatching native control type", () =>
{
    var request = Request(); request["params"]!["selector"]!["control_type"] = "Button";
    var session = new FakeSession(); Outcome(Run(request, session), "BLOCKED", "not_started", false);
    Require(session.Field.Value.SetCalls == 0);
});
Check("unique target in nonfirst window", () =>
{
    var session = new FakeSession(); session.Field.Name = "Different";
    var second = new FakeNode(3, "field", "Exact field", "Edit"); session.AddWindow(200, second);
    Outcome(Run(Request(), session), "PASS", "completed", true);
    Require(session.Field.Value.SetCalls == 0 && second.Value.SetCalls == 1);
});
Check("selector name remains exact", () =>
{
    var request = Request(); request["params"]!["selector"]!["name"] = " Exact field ";
    var session = new FakeSession(); Outcome(Run(request, session), "BLOCKED", "not_started", false);
    Require(session.Field.Value.SetCalls == 0);
});
Check("selector name remains case sensitive", () =>
{
    var request = Request(); request["params"]!["selector"]!["name"] = "exact field";
    var session = new FakeSession(); Outcome(Run(request, session), "BLOCKED", "not_started", false);
    Require(session.Field.Value.SetCalls == 0);
});
Check("automatic focus disabled only on isolated UIA instance", () =>
{
    using var isolated = new FlaUI.UIA3.UIA3Automation();
    using var other = new FlaUI.UIA3.UIA3Automation();
    var otherBefore = other.NativeAutomation2.AutoSetFocus;
    NativeProviderTextSession.ConfigureAutomation(isolated);
    Require(isolated.NativeAutomation2.AutoSetFocus == 0 && other.NativeAutomation2.AutoSetFocus == otherBefore);
});
Check("wrong native process creation time", () =>
{
    try { using var session = NativeProviderTextSession.Open(Environment.ProcessId, 1); Require(false); }
    catch (ProviderTextRefusal refusal) { Require(refusal.Reason == "process_changed"); }
});
Check("missing native process", () =>
{
    try { using var session = NativeProviderTextSession.Open(int.MaxValue, 1); Require(false); }
    catch (ProviderTextRefusal refusal) { Require(refusal.Reason == "process_unavailable"); }
});
Check("initial process no longer current", () =>
{
    var session = new FakeSession { Current = false }; Blocked(session, "process_changed");
});
Check("request without final newline refused", () =>
{
    var output = new StringWriter(); var session = new FakeSession();
    ProviderTextCommand.RunStdio(new StringReader(Request().ToJsonString()), output, (_, _) => { session.OpenCalls++; return session; });
    Outcome(Envelope(output.ToString())["result"]!.AsObject(), "BLOCKED", "not_started", false);
    Require(session.OpenCalls == 0);
});
Check("additional empty line refused", () =>
{
    var session = new FakeSession(); Outcome(Run(Request(), session, "\n"), "BLOCKED", "not_started", false);
    Require(session.OpenCalls == 0);
});
Check("unsupported provider", () => { var session = new FakeSession(); session.Field.Supported = false; Blocked(session, "value_unsupported"); });
Check("read only", () => { var session = new FakeSession(); session.Field.Value.IsReadOnly = true; Blocked(session, "value_read_only"); });
Check("password", () => { var session = new FakeSession(); session.Field.Password = true; Blocked(session, "target_password"); });
Check("disabled", () => { var session = new FakeSession(); session.Field.Enabled = false; Blocked(session, "target_disabled"); });
Check("ambiguous across windows", () =>
{
    var session = new FakeSession(); session.AddWindow(200, new FakeNode(3, "field", "Exact field", "Edit"));
    Blocked(session, "target_ambiguous");
});
Check("matching sibling appears during preflight", () =>
{
    var session = new FakeSession();
    var added = new FakeNode(3, "field", "Exact field", "Edit");
    session.Field.Value.OnPreflight = () =>
    {
        session.Root.Add(added);
        session.Field.Value.OnPreflight = null;
    };
    var result = Run(Request(), session);
    Outcome(result, "BLOCKED", "not_started", false);
    Require(result["reason"]!.GetValue<string>() == "target_ambiguous" &&
        session.Field.Value.SetCalls == 0 && added.Value.SetCalls == 0);
});
Check("matching window appears during preflight", () =>
{
    var session = new FakeSession();
    var added = new FakeNode(3, "field", "Exact field", "Edit");
    session.Field.Value.OnPreflight = () =>
    {
        session.AddWindow(200, added);
        session.Field.Value.OnPreflight = null;
    };
    var result = Run(Request(), session);
    Outcome(result, "BLOCKED", "not_started", false);
    Require(result["reason"]!.GetValue<string>() == "target_ambiguous" &&
        session.Field.Value.SetCalls == 0 && added.Value.SetCalls == 0);
});
Check("unreadable nonmatching node", () =>
{
    var session = new FakeSession(); session.Root.Add(new FakeNode(3, "other", "Other", "Edit") { Unreadable = true });
    Blocked(session, "target_unreadable");
});
Check("foreign process child", () =>
{
    var session = new FakeSession(); session.Root.Add(new FakeNode(3, "other", "Other", "Edit") { Pid = 43 });
    Blocked(session, "target_changed");
});
Check("wide tree refuses without setter", () =>
{
    var session = new FakeSession();
    for (var i = 3; i < 4_100; i++) session.Root.Add(new FakeNode(i, "other", "Other", "Edit"));
    Blocked(session, "tree_limit_exceeded");
});
Check("unreadable child traversal", () =>
{
    var session = new FakeSession(); session.Field.ThrowOnChildren = true;
    Blocked(session, "target_unreadable");
});
Check("stale element runtime id", () =>
{
    var session = new FakeSession(); session.Field.Value.OnPreflight = () => session.Field.Id++;
    Blocked(session, "target_changed");
});
Check("stale root runtime id", () =>
{
    var session = new FakeSession(); session.Field.Value.OnPreflight = () => session.Root.Id++;
    Blocked(session, "target_changed");
});
Check("selector changed before setter", () =>
{
    var session = new FakeSession(); session.Field.Value.OnPreflight = () => session.Field.Name = "Changed";
    Blocked(session, "target_changed");
});
Check("element moved outside root", () =>
{
    var session = new FakeSession(); session.Field.Value.OnPreflight = () => session.Field.Parent = null;
    Blocked(session, "target_changed");
});
Check("process changed before setter", () =>
{
    var session = new FakeSession(); session.Field.Value.OnPreflight = () => session.Current = false;
    Blocked(session, "process_changed");
});
Check("original window no longer bound", () =>
{
    var session = new FakeSession(); session.Field.Value.OnPreflight = () => session.WindowBound = false;
    Blocked(session, "target_changed");
});
Check("completed mismatch", () =>
{
    var session = new FakeSession(); session.Field.Value.KeepOriginal = true;
    var result = Run(Request(), session);
    Outcome(result, "FAIL", "completed", false);
    Require(result["reason"]!.GetValue<string>() == "readback_mismatch" && session.Field.Value.SetCalls == 1);
});
Check("setter exception uncertain no retry", () =>
{
    var session = new FakeSession(); session.Field.Value.ThrowOnSet = true;
    var result = Run(Request(), session);
    Outcome(result, "FAIL", "unknown", false);
    Require(result["reason"]!.GetValue<string>() == "setter_failed" && session.Field.Value.SetCalls == 1);
});
Check("setter mutates then throws remains uncertain without retry", () =>
{
    var session = new FakeSession(); session.Field.Value.ThrowAfterSet = true;
    var result = Run(Request(), session);
    Outcome(result, "FAIL", "unknown", false);
    Require(result["reason"]!.GetValue<string>() == "setter_failed" &&
        session.Field.Value.SetCalls == 1 && session.Field.Value.Text == "replacement");
});
Check("lost readback uncertain", () =>
{
    var session = new FakeSession(); session.Field.Value.ThrowOnReadback = true;
    Outcome(Run(Request(), session), "FAIL", "unknown", false);
    Require(session.Field.Value.SetCalls == 1);
});
Check("different target after setter cannot verify", () =>
{
    var session = new FakeSession(); session.Field.Value.OnSet = () => session.Field.Id++;
    Outcome(Run(Request(), session), "FAIL", "unknown", false);
});
Check("target changes during readback cannot verify", () =>
{
    var session = new FakeSession(); session.Field.Value.OnReadback = () => session.Field.Name = "Changed";
    Outcome(Run(Request(), session), "FAIL", "unknown", false);
});
Check("full selector conjunctive", () =>
{
    var request = Request(); request["params"]!["selector"]!["name"] = "Different";
    var session = new FakeSession();
    Outcome(Run(request, session), "BLOCKED", "not_started", false);
    Require(session.Field.Value.SetCalls == 0);
});
foreach (var key in new[] { "root_id", "xpath", "unknown" })
    Check($"reject selector key {key}", () =>
    {
        var request = Request(); request["params"]!["selector"]![key] = "";
        var session = new FakeSession(); Outcome(Run(request, session), "BLOCKED", "not_started", false);
        Require(session.OpenCalls == 0);
    });
foreach (var key in new[] { "automation_id", "name", "control_type" })
    foreach (var bad in new string?[] { null, "", " \t", "a\0b" })
        Check($"reject blank or invalid criterion {key}", () =>
        {
            var request = Request(); request["params"]!["selector"]![key] = bad;
            var session = new FakeSession(); Outcome(Run(request, session), "BLOCKED", "not_started", false);
            Require(session.OpenCalls == 0);
        });
foreach (var bad in new[] { "", " \t", "NotARealControlType537", "50004" })
    Check($"reject invalid selector type {bad.Length}", () =>
    {
        var request = Request(); request["params"]!["selector"]!["control_type"] = bad;
        var session = new FakeSession(); Outcome(Run(request, session), "BLOCKED", "not_started", false);
        Require(session.OpenCalls == 0);
    });
Check("second request refused before open", () =>
{
    var session = new FakeSession();
    Outcome(Run(Request(), session, Request().ToJsonString() + "\n"), "BLOCKED", "not_started", false);
    Require(session.OpenCalls == 0);
});
Check("other method never dispatched", () =>
{
    var request = Request(); request["method"] = "send_keys";
    var session = new FakeSession(); Outcome(Run(request, session), "BLOCKED", "not_started", false);
    Require(session.OpenCalls == 0);
});
Check("NUL refused before open", () =>
{
    var session = new FakeSession(); Outcome(Run(Request("a\0b"), session), "BLOCKED", "not_started", false);
    Require(session.OpenCalls == 0);
});
foreach (var escaped in new[] { "\\ud800", "\\udc00" })
    Check("unpaired surrogate refused before open", () =>
    {
        var session = new FakeSession(); var output = new StringWriter();
        var source = Request().ToJsonString().Replace("replacement", escaped, StringComparison.Ordinal);
        ProviderTextCommand.RunStdio(new StringReader(source + "\n"), output, (_, _) => { session.OpenCalls++; return session; });
        Outcome(Envelope(output.ToString())["result"]!.AsObject(), "BLOCKED", "not_started", false);
        Require(session.OpenCalls == 0);
    });
Check("duplicate parameter refused before open", () =>
{
    var session = new FakeSession(); var output = new StringWriter();
    var source = Request().ToJsonString().Replace("\"pid\":42", "\"pid\":42,\"pid\":42", StringComparison.Ordinal);
    ProviderTextCommand.RunStdio(new StringReader(source + "\n"), output, (_, _) => { session.OpenCalls++; return session; });
    Outcome(Envelope(output.ToString())["result"]!.AsObject(), "BLOCKED", "not_started", false);
    Require(session.OpenCalls == 0);
});
Check("cleanup exception remains sanitized", () =>
{
    var session = new FakeSession { ThrowOnDispose = true };
    Outcome(Run(Request(), session), "PASS", "completed", true);
});
Console.WriteLine($"Provider text behavior checks passed: {passed}");

sealed class FlushProbeWriter : StringWriter
{
    public string FlushedText = "";
    public override void Flush()
    {
        base.Flush();
        FlushedText = ToString();
    }
}
sealed class ReadProbeReader(string source, FlushProbeWriter output) : StringReader(source)
{
    public int ReadCalls;
    public string? BeforeFirstRead;
    public override int Read()
    {
        if (ReadCalls++ == 0)
            BeforeFirstRead = output.FlushedText;
        return base.Read();
    }
}
sealed class FakeSession : IProviderTextSession
{
    public readonly FakeNode Root = new(1, "root", "Root", "Window") { Hwnd = 100 };
    public readonly FakeNode Field = new(2, "field", "Exact field", "Edit");
    private readonly Dictionary<nint, FakeNode> _roots = new();
    public bool Current = true, WindowBound = true, Disposed, ThrowOnDispose;
    public int OpenCalls;
    public FakeSession() { Root.Add(Field); _roots.Add(100, Root); }
    public void AddWindow(nint hwnd, FakeNode field)
    {
        var root = new FakeNode(1_000, "root2", "Root2", "Window") { Hwnd = hwnd };
        root.Add(field); _roots.Add(hwnd, root);
    }
    public bool IsCurrent() => Current;
    public IReadOnlyList<nint> GetWindows() => _roots.Keys.ToArray();
    public bool IsWindowBound(nint hwnd) => WindowBound && _roots.ContainsKey(hwnd);
    public ProviderTextNode GetRoot(nint hwnd) => _roots[hwnd];
    public void Dispose() { Disposed = true; if (ThrowOnDispose) throw new InvalidOperationException("private provider exception"); }
}
sealed class FakeNode(int id, string automationId, string name, string controlType) : ProviderTextNode
{
    public int Id = id, Pid = 42;
    public nint Hwnd;
    public string Name = name;
    public bool Enabled = true, Password, Supported = true, Unreadable, ThrowOnChildren;
    public FakeNode? Parent;
    public List<FakeNode> Children = new();
    public FakeValue Value = new();
    public void Add(FakeNode child) { child.Parent = this; Children.Add(child); }
    internal override ProviderTextSnapshot Read()
    {
        if (Unreadable) throw new InvalidOperationException("private provider exception");
        return new(Pid, new[] { Id }, Hwnd, automationId, Name, controlType);
    }
    internal override ProviderTextNode? FirstChild() => ThrowOnChildren
        ? throw new InvalidOperationException("private provider exception") : Children.FirstOrDefault();
    internal override ProviderTextNode? NextSibling()
    {
        if (Parent is null) return null;
        var index = Parent.Children.IndexOf(this) + 1;
        return index < Parent.Children.Count ? Parent.Children[index] : null;
    }
    internal override ProviderTextNode? GetParent() => Parent;
    internal override bool IsEnabled() => Enabled;
    internal override bool IsPassword() => Password;
    internal override IProviderTextValue? GetValue() => Supported ? Value : null;
}
sealed class FakeValue : IProviderTextValue
{
    public string Text = "original private field";
    public int SetCalls;
    public bool IsReadOnly { get; set; }
    public bool KeepOriginal, ThrowOnSet, ThrowAfterSet, ThrowOnReadback;
    public Action? OnPreflight, OnSet, OnReadback;
    public string Read()
    {
        if (SetCalls == 0) { OnPreflight?.Invoke(); return Text; }
        if (ThrowOnReadback) throw new InvalidOperationException("private provider exception");
        OnReadback?.Invoke(); return Text;
    }
    public void Set(string text)
    {
        SetCalls++;
        if (ThrowOnSet) throw new InvalidOperationException("private provider exception");
        if (!KeepOriginal) Text = text;
        if (ThrowAfterSet) throw new InvalidOperationException("private provider exception");
        OnSet?.Invoke();
    }
}
