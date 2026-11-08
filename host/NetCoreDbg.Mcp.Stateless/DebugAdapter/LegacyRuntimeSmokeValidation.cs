using System.Globalization;
using System.Numerics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class LegacyRuntimeSmokeValidation
{
    private static bool PythonWhitespace(char c) => char.IsWhiteSpace(c) || c is >= '\u001c' and <= '\u001f';
    private static bool PythonBlank(string value) => value.All(PythonWhitespace);

    private const string V1 = "netcoredbg.runtime_smoke.v1", V2 = "netcoredbg.runtime_smoke.v2", Diagnostic = "netcoredbg.runtime_smoke.diagnostics.v1";
    private static readonly string[] Top = ["schema", "name", "description", "preflight", "launch", "freshness", "steps", "actions", "assertions", "evidence", "cleanup", "teardown", "budgets", "stop_on_first_failed_assertion"];
    private static readonly string[] V2Only = ["baseline", "generate", "cases", "diagnostics", "metrics_thresholds", "input_policy", "run_confidence"];
    private static readonly string[] Probes = ["app_diagnostics", "debug.evaluate", "debug.tracepoint", "file.json", "oracle_pack", "output.field", "output.since", "process.metric", "ui.grid", "ui.grid.viewport", "ui.property", "ui.text"];
    private static readonly string[] Statuses = ["PASS", "BLOCKED", "FAIL"];
    private static readonly Dictionary<string, string[]> Operations = new(StringComparer.Ordinal)
    {
        ["launch"] = [],
        ["debug.hygiene_preflight"] = [],
        ["debug.freshness.verify"] = [],
        ["debug.output_checkpoint"] = [],
        ["debug.output_assert_since"] = ["checkpoint"],
        ["instrumentation.group_clear"] = ["name"],
        ["ui.key_sequence"] = ["selector", "keys"],
        ["ui.grid.snapshot"] = ["selector"],
        ["ui.grid.get_state"] = ["selector"],
        ["ui.grid.ensure_visible"] = ["selector", "row"],
        ["ui.grid.select_range"] = ["selector", "start_index", "end_index"],
        ["ui.grid.assert_range"] = ["selector", "start_index", "end_index"],
        ["ui.grid.select_row"] = ["selector", "row"],
        ["ui.grid.click_row"] = ["selector", "row"],
        ["ui.grid.right_click_row"] = ["selector", "row"],
        ["ui.grid.double_click_row"] = ["selector", "row"],
        ["ui.grid.assert_rows"] = ["selector", "rows"],
        ["ui.list.invoke_item"] = ["selector", "item"],
        ["ui.list.toggle_item_child"] = ["selector", "item", "child"],
        ["ui.focus.assert"] = ["selector"],
        ["ui.text.assert"] = ["selector"],
        ["ui.text.read"] = ["selector"],
        ["ui.text.get_state"] = ["selector"],
        ["ui.text.set_text"] = ["selector", "text"],
        ["ui.text.assert_selection"] = ["selector"],
        ["ui.get_property"] = ["selector"],
        ["ui.invoke"] = ["selector"],
        ["fixture.restore"] = ["path"]
    };
    private static readonly Dictionary<string, string[]> Required = new(StringComparer.Ordinal)
    {
        ["oracle_pack"] = ["schema", "id", "status", "checks", "limits"],
        ["app_diagnostics"] = ["schema", "app", "status", "observations", "redaction", "limits"],
        ["semantic_probe"] = ["schema", "kind", "status", "selector", "expect", "on_blocked"],
        ["tracepoint_guardrail"] = ["schema", "id", "status", "mode", "allowed_when", "blocked_when", "unsafe_when", "cleanup"]
    };
    private static readonly string[] Omitted = ["access_token", "api_key", "password", "raw_tree", "screenshot_base64", "secret", "ui_tree", "window_tree"];
    private static readonly string[] Summarized = ["backend_result", "exception", "raw_output", "stack"];
    private static JsonArray ArrayOf(IEnumerable<string> values) => new(values.Select(x => (JsonNode?)JsonValue.Create(x)).ToArray());
    private static string? Str(JsonNode? n) => n is JsonValue v && v.TryGetValue<string>(out var s) ? s : null;
    private static bool Bool(JsonNode? n) => n is JsonValue v && v.GetValueKind() is JsonValueKind.True or JsonValueKind.False;
    private static bool Int(JsonNode? n, out BigInteger value)
    {
        value = default;
        return n is JsonValue v && v.GetValueKind() == JsonValueKind.Number && BigInteger.TryParse(v.ToJsonString(), NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture, out value);
    }
    private static bool Number(JsonNode? n, out double value)
    {
        value = default;
        return n is JsonValue v && v.GetValueKind() == JsonValueKind.Number && double.TryParse(v.ToJsonString(), NumberStyles.Float, CultureInfo.InvariantCulture, out value);
    }
    private static bool Strings(JsonNode? n) => n is JsonArray a && a.All(x => Str(x) is not null);
    private static bool Truth(JsonNode? n) => n switch { null => false, JsonObject o => o.Count != 0, JsonArray a => a.Count != 0, _ when Str(n) is string s => s.Length != 0, _ when Bool(n) => n!.GetValue<bool>(), _ when Number(n, out var d) => d != 0, _ => true };
    private static bool Eq(JsonNode? node, string value) => Str(node) == value;
    private static string Alias(string name) => name switch { "debug.hygiene_preflight" => "debug_hygiene_preflight", "debug.freshness.verify" => "verify_debug_freshness", "debug.output_checkpoint" => "output_checkpoint", "debug.output_assert_since" => "output_assert_since", "instrumentation.group_clear" => "instrumentation_group_clear", "ui.key_sequence" => "ui_key_sequence", _ => name };

    internal static JsonObject Validate(JsonNode? plan) => ValidateCore(plan, native: false);
    internal static JsonObject ValidateNative(JsonNode? plan) => ValidateCore(plan, native: true);

    private static JsonObject ValidateCore(JsonNode? plan, bool native)
    {
        var errors = new List<string>();
        if (plan is JsonObject obj) ValidatePlan(obj, errors, native); else errors.Add("plan must be an object");
        var result = Help(plan as JsonObject);
        if (plan is JsonObject v2 && Eq(v2["schema"], V2))
        {
            var contract = LegacyRuntimeSmokeV2.Validate(v2);
            foreach (var item in contract) if (item.Key != "validation_errors") result[item.Key] = item.Value?.DeepClone();
            if (contract["validation_errors"] is JsonArray extra) foreach (var error in extra) if (Str(error) is string text) errors.Add(text);
        }
        else { result["case_count"] = 0; result["generated_case_count"] = 0; }
        result["validation_errors"] = ArrayOf(errors);
        result["can_run"] = errors.Count == 0;
        result["status"] = errors.Count == 0 ? "PASS" : "INVALID_SETUP";
        result["evidence_contract"] = EvidenceContract(native);
        return result;
    }

    private static JsonObject Help(JsonObject? plan)
    {
        var aliases = new JsonObject(); var required = new JsonObject();
        foreach (var name in Operations.Keys.OrderBy(x => x, StringComparer.Ordinal)) { aliases[name] = Alias(name); required[name] = ArrayOf(Operations[name]); }
        var result = new JsonObject { ["accepted_schema_values"] = ArrayOf([V1, V2]), ["accepted_top_level_keys"] = ArrayOf(Top), ["accepted_operation_names"] = ArrayOf(Operations.Keys.OrderBy(x => x, StringComparer.Ordinal)), ["operation_aliases"] = aliases, ["operation_required_fields"] = required };
        if (plan is not null && (Eq(plan["schema"], V2) || V2Only.Any(plan.ContainsKey))) result["accepted_top_level_keys_v2"] = ArrayOf(Top.Concat(V2Only));
        return result;
    }

    private static void ValidatePlan(JsonObject plan, List<string> errors, bool native)
    {
        var accepted = Eq(plan["schema"], V2) ? Top.Concat(V2Only).ToArray() : Top;
        foreach (var item in plan) if (!accepted.Contains(item.Key)) errors.Add($"unexpected top-level key: {item.Key}; expected one of: {string.Join(", ", accepted)}");
        var v2keys = V2Only.Where(plan.ContainsKey).OrderBy(x => x, StringComparer.Ordinal).ToArray();
        var schema = plan["schema"];
        if (schema is null && v2keys.Length > 0) errors.Add("schema is required when using v2-only keys: " + string.Join(", ", v2keys));
        if (schema is not null && !Eq(schema, V1) && !Eq(schema, V2)) errors.Add($"schema must be one of: {V1}, {V2}");
        var mixed = new[] { "steps", "actions", "assertions", "evidence" }.Where(plan.ContainsKey).OrderBy(x => x, StringComparer.Ordinal).ToArray();
        if (Eq(schema, V2) && mixed.Length > 0 && v2keys.Length > 0) errors.Add("v2 plans cannot mix legacy execution keys with v2 case keys: " + string.Join(", ", mixed));
        foreach (var key in new[] { "actions", "assertions", "evidence", "steps", "cases" }) if (plan.ContainsKey(key) && plan[key] is not JsonArray) errors.Add($"{key} must be a list");
        if (plan.ContainsKey("preflight") && plan["preflight"] is not JsonObject and not JsonArray && !Bool(plan["preflight"])) errors.Add("preflight must be a boolean, object, or list");
        foreach (var key in new[] { "launch", "freshness", "baseline", "generate" }) if (plan.ContainsKey(key) && plan[key] is not JsonObject) errors.Add($"{key} must be an object");
        if (plan.ContainsKey("diagnostics"))
        {
            if (plan["diagnostics"] is not JsonObject diag) errors.Add("diagnostics must be an object");
            else if (diag["app_diagnostics"] is not null)
            {
                if (diag["app_diagnostics"] is not JsonObject app) errors.Add("diagnostics.app_diagnostics must be an object");
                else if (app.ContainsKey("diagnostic_launch") && app["diagnostic_launch"] is not JsonObject) errors.Add("diagnostics.app_diagnostics.diagnostic_launch must be an object");
            }
        }
        foreach (var key in new[] { "metrics_thresholds", "cleanup", "teardown" }) if (plan.ContainsKey(key) && plan[key] is not JsonObject) errors.Add($"{key} must be an object");
        if (plan["budgets"] is not null && plan["budgets"] is not JsonObject) errors.Add("budgets must be an object");
        if (plan["budgets"] is JsonObject budgets)
        {
            if (budgets.ContainsKey("max_actions")) { if (!Int(budgets["max_actions"], out var n)) errors.Add("budgets.max_actions must be an integer"); else if (n < 1) errors.Add("budgets.max_actions must be at least 1"); }
            if (budgets.ContainsKey("max_elapsed_seconds")) { if (!Number(budgets["max_elapsed_seconds"], out var n)) errors.Add("budgets.max_elapsed_seconds must be a number"); else if (!double.IsFinite(n) || n <= 0) errors.Add("budgets.max_elapsed_seconds must be positive"); }
        }
        foreach (var key in new[] { "steps", "actions", "assertions", "evidence" }) if (plan[key] is JsonArray items) for (var i = 0; i < items.Count; i++) Step($"{key}[{i}]", items[i], errors, native);
        foreach (var key in new[] { "cleanup", "teardown" }) if (plan[key] is JsonObject config && config["restore_files"] is not null)
        {
            if (config["restore_files"] is not JsonArray files) errors.Add($"{key}.restore_files must be a list");
            else for (var i = 0; i < files.Count; i++) Restore($"{key}.restore_files[{i}]", files[i], errors);
        }
        if (plan.ContainsKey("input_policy")) ValidateInputPolicy("input_policy", plan["input_policy"], errors);
        if (plan.ContainsKey("run_confidence")) ValidatePolicy("run_confidence", plan["run_confidence"], "no_operator", errors);
    }

    internal static void ValidateInputPolicy(string prefix, JsonNode? payload, List<string> errors) => ValidatePolicy(prefix, payload, "no_global_input", errors);
    private static void ValidatePolicy(string prefix, JsonNode? payload, string allowed, List<string> errors)
    {
        if (payload is not JsonObject obj) { errors.Add($"{prefix} must be an object"); return; }
        foreach (var item in obj) if (item.Key != allowed) errors.Add($"{prefix}.{item.Key} is not accepted; expected one of: {allowed}");
        if (obj.ContainsKey(allowed) && !Bool(obj[allowed])) errors.Add($"{prefix}.{allowed} must be a boolean");
    }
    private static void Restore(string prefix, JsonNode? value, List<string> errors)
    {
        if (value is not JsonObject obj) { errors.Add($"{prefix} must be an object"); return; }
        if (Str(obj["path"]) is not string path || path.Length == 0) errors.Add($"{prefix}.path is required");
        bool text = obj.ContainsKey("baseline_text"), file = obj.ContainsKey("baseline_file");
        if (text == file) { errors.Add($"{prefix} requires exactly one of baseline_text or baseline_file"); return; }
        if (text && Str(obj["baseline_text"]) is null) errors.Add($"{prefix}.baseline_text must be a string");
        if (file && (Str(obj["baseline_file"]) is not string s || s.Length == 0)) errors.Add($"{prefix}.baseline_file must be a non-empty string");
    }
    private static JsonNode?[] PythonCharacters(string text)
    {
        var characters = new List<JsonNode?>();
        for (var i = 0; i < text.Length; i++)
        {
            var length = char.IsHighSurrogate(text[i]) && i + 1 < text.Length && char.IsLowSurrogate(text[i + 1]) ? 2 : 1;
            characters.Add(JsonValue.Create(text.Substring(i, length)));
            i += length - 1;
        }
        return characters.ToArray();
    }
    private static JsonObject OperationArgs(JsonNode? value)
    {
        if (!Truth(value)) return new JsonObject();
        if (value is JsonObject mapping) return (JsonObject)mapping.DeepClone();
        IEnumerable<JsonNode?> entries;
        if (value is JsonArray array) entries = array;
        else if (Str(value) is string text) entries = PythonCharacters(text);
        else throw new ArgumentException($"'{PythonType(value)}' object is not iterable");

        var result = new JsonObject();
        var index = 0;
        foreach (var entry in entries)
        {
            JsonNode?[] pair;
            if (entry is JsonArray elements) pair = elements.ToArray();
            else if (entry is JsonObject obj) pair = obj.Select(item => (JsonNode?)JsonValue.Create(item.Key)).ToArray();
            else if (Str(entry) is string str) pair = PythonCharacters(str);
            else throw new ArgumentException($"cannot convert dictionary update sequence element #{index} to a sequence");
            if (pair.Length != 2) throw new ArgumentException($"dictionary update sequence element #{index} has length {pair.Length}; 2 is required");
            if (pair[0] is JsonObject or JsonArray) throw new ArgumentException($"unhashable type: '{PythonType(pair[0])}'");
            // Non-string JSON keys cannot match any of the string schema fields.
            if (Str(pair[0]) is string key) result[key] = pair[1]?.DeepClone();
            index++;
        }
        return result;
    }
    private static string PythonType(JsonNode? value) => value switch
    {
        null => "NoneType",
        JsonObject => "dict",
        JsonArray => "list",
        _ when Str(value) is not null => "str",
        _ when Bool(value) => "bool",
        _ when Int(value, out _) => "int",
        _ => "float"
    };
    private static void Step(string prefix, JsonNode? value, List<string> errors, bool native)
    {
        if (value is not JsonObject raw) { errors.Add($"{prefix} must be an object"); return; }
        if (raw.ContainsKey("args") && raw["args"] is not JsonObject)
        {
            errors.Add($"{prefix}.args must be an object");
            if (native) return;
        }
        if (!raw.ContainsKey("op")) return;
        if (Str(raw["op"]) is not string op) { errors.Add($"{prefix}.op must be a string"); return; }
        if (!Operations.TryGetValue(op, out var fields)) { errors.Add($"{prefix}.op is not accepted: {op}"); return; }
        var args = OperationArgs(raw["args"]);
        foreach (var item in raw) if (item.Key is not ("id" or "op" or "args")) args[item.Key] = item.Value?.DeepClone();
        foreach (var field in fields) if (args[field] is null) errors.Add($"{prefix}.{field} is required for op {op}");
        void Shape(string key, bool valid, string expected) { if (args.ContainsKey(key) && !valid) errors.Add($"{prefix}.{key} must be {expected} for op {op}"); }
        void Integer(string key) => Shape(key, Int(args[key], out _), "an integer");
        Shape("selector", args["selector"] is JsonObject, "an object");
        string[] rows = ["ui.grid.ensure_visible", "ui.grid.select_row", "ui.grid.click_row", "ui.grid.right_click_row", "ui.grid.double_click_row"];
        if (rows.Contains(op) || op is "ui.grid.snapshot" or "ui.grid.get_state")
        {
            Shape("rows", args["rows"] is JsonObject, "an object"); Shape("columns", Strings(args["columns"]), "a list of strings"); Shape("identity", args["identity"] is JsonObject, "an object");
            if (rows.Contains(op)) Shape("row", args["row"] is JsonObject, "an object");
            if (rows.Contains(op)) { if (op != "ui.grid.ensure_visible") Shape("ensure_visible", Bool(args["ensure_visible"]), "a boolean"); Integer("max_scrolls"); Integer("scroll_settle_ms"); }
            if (op is "ui.grid.click_row" or "ui.grid.right_click_row" or "ui.grid.double_click_row") Shape("column", Str(args["column"]) is not null, "a string");
        }
        else if (op is "ui.grid.select_range" or "ui.grid.assert_range")
        {
            Integer("start_index"); Integer("end_index");
            bool hasStart = Int(args["start_index"], out var start), hasEnd = Int(args["end_index"], out var end);
            if (hasStart && start < 0) errors.Add($"{prefix}.start_index must be non-negative for op {op}");
            if (hasEnd && end < 0) errors.Add($"{prefix}.end_index must be non-negative for op {op}");
            if (hasStart && hasEnd && start >= 0 && end >= 0 && end < start) errors.Add($"{prefix}.end_index must be greater than or equal to start_index for op {op}");
        }
        else if (op == "ui.grid.assert_rows")
        {
            if (args.ContainsKey("rows") && args["rows"] is not JsonArray) errors.Add($"{prefix}.rows must be a list for op {op}");
            else if (args["rows"] is JsonArray list) for (int i = 0; i < list.Count; i++) { var p = $"{prefix}.rows[{i}]"; if (list[i] is not JsonObject row) errors.Add($"{p} must be an object for op {op}"); else { if (!Int(row["index"], out var index) || index < 0) errors.Add($"{p}.index must be a non-negative integer for op {op}"); if (row["contains"] is not JsonObject) errors.Add($"{p}.contains must be an object for op {op}"); } }
        }
        else if (op is "ui.list.invoke_item" or "ui.list.toggle_item_child")
        {
            Shape("item", args["item"] is JsonObject, "an object");
            if (args["item"] is JsonObject item && item.ContainsKey("index") && (!Int(item["index"], out var index) || index < 0)) errors.Add($"{prefix}.item.index must be a non-negative integer for op {op}");
            if (op == "ui.list.toggle_item_child") { Shape("child", args["child"] is JsonObject, "an object"); if (args["child"] is JsonObject child && !new[] { "automation_id", "automationId", "name", "control_type", "controlType" }.Any(k => Str(child[k]) is string s && !PythonBlank(s))) errors.Add($"{prefix}.child must include automation_id, name, or control_type for op {op}"); if (args["target_state"] is not null) Shape("target_state", Str(args["target_state"]) is not null, "a string"); }
        }
        else if (op == "ui.key_sequence") Shape("keys", args["keys"] is JsonArray, "a list");
        else if (op == "ui.text.assert_selection") { if (args["selection_start"] is null || args["selection_end"] is null) errors.Add($"{prefix}.selection_start and selection_end are required for op {op}"); Integer("selection_start"); Integer("selection_end"); }
        else if (op == "ui.text.set_text") Shape("text", Str(args["text"]) is not null, "a string");
        else if (op == "ui.get_property")
        {
            if (args["property"] is null && args["property_name"] is null) errors.Add($"{prefix}.property or property_name is required for op {op}");
            foreach (var key in new[] { "property", "property_name" }) if (args[key] is not null) { if (Str(args[key]) is not string s) errors.Add($"{prefix}.{key} must be a string for op {op}"); else if (PythonBlank(s)) errors.Add($"{prefix}.{key} must be a non-empty string for op {op}"); }
        }
        if (op == "fixture.restore") Restore(prefix, args, errors);
    }

    internal static JsonObject AppDiagnosticsLaunchContract(string? name)
    {
        const string baseDir = ".agent/runtime-smoke/app-diagnostics";
        var directory = baseDir;
        if (!string.IsNullOrEmpty(name))
        {
            var part = name.Replace('\\', '/').Split('/')[^1];
            var builder = new StringBuilder(part.Length);
            foreach (var rune in part.EnumerateRunes())
            {
                var category = Rune.GetUnicodeCategory(rune);
                if (Rune.IsLetterOrDigit(rune) || category is UnicodeCategory.LetterNumber or UnicodeCategory.OtherNumber || rune.Value is '-' or '_' or '.') builder.Append(rune.ToString());
                else builder.Append('-');
            }
            var safe = builder.ToString().Trim('.', '-');
            directory += "/" + (safe.Length == 0 ? "app-diagnostics" : safe);
        }
        return new JsonObject { ["kind"] = "app_diagnostics", ["schema"] = Diagnostic, ["env_var_names"] = EnvNames(), ["evidence"] = new JsonObject { ["directory"] = directory, ["path"] = directory + "/app-diagnostics.json" }, ["redacted_env_values"] = true };
    }
    private static JsonObject EnvNames() => new() { ["directory"] = "NETCOREDBG_MCP_APP_DIAGNOSTICS_DIR", ["path"] = "NETCOREDBG_MCP_APP_DIAGNOSTICS_PATH", ["schema"] = "NETCOREDBG_MCP_APP_DIAGNOSTICS_SCHEMA" };
    private static JsonObject Limits() => new() { ["max_text_length"] = 240, ["max_list_items"] = 8, ["max_json_bytes"] = 32768 };
    private static JsonObject EvidenceContract(bool native)
    {
        var diagnostics = DiagnosticContract(native);
        if (native) diagnostics["tracepoint_guardrail"]!["runtime_limits_note"] = "Native tracepoint limits are not derived from Python environment settings.";
        return new JsonObject { ["result_keys"] = ArrayOf(["status", "reason", "elapsed_ms", "action_count", "cleanup", "evidence_refs", "compact", "debug_preflight"]), ["compact_limits"] = Limits(), ["diagnostics"] = diagnostics };
    }
    private static JsonObject DiagnosticContract(bool native) => new()
    {
        ["schema"] = Diagnostic,
        ["status_values"] = ArrayOf(Statuses),
        ["evidence_limits"] = Limits(),
        ["redaction"] = new JsonObject { ["omit_fields"] = ArrayOf(Omitted), ["summarize_fields"] = ArrayOf(Summarized) },
        ["oracle_pack"] = new JsonObject { ["required_fields"] = ArrayOf(Required["oracle_pack"]), ["optional_fields"] = ArrayOf(["description", "probes", "sources", "failure_modes", "redaction"]), ["failure_modes"] = ArrayOf(["missing_probe", "unsupported_probe", "unsafe_evidence", "DISAGREEING_SOURCES"]) },
        ["app_diagnostics"] = new JsonObject
        {
            ["required_fields"] = ArrayOf(Required["app_diagnostics"]),
            ["optional_fields"] = ArrayOf(["workspace", "artifacts", "process", "modules", "wait_json", "poll", "diagnostic_launch"]),
            ["launch_contract"] = new JsonObject { ["env_var_names"] = EnvNames(), ["default_evidence_directory"] = ".agent/runtime-smoke/app-diagnostics", ["default_file_name"] = "app-diagnostics.json", ["redacted_env_values"] = true },
            ["freshness_contract"] = new JsonObject { ["app_fields"] = ArrayOf(["process_id", "process_name", "expected_modules", "require_active_process"]), ["top_level_fields"] = ArrayOf(["workspace", "artifacts", "process", "modules", "loaded_sources"]), ["aliases"] = new JsonObject { ["process"] = ArrayOf(["id", "name", "expected_id", "expected_name", "require_active"]) }, ["status_policy"] = new JsonObject { ["FAIL"] = "force diagnostic FAIL when live target contradicts declared app", ["WARN"] = "preserve diagnostic status and include incomplete evidence warning", ["PASS"] = "preserve diagnostic status with freshness proof" } },
            ["wait_json_condition_contract"] = new JsonObject { ["fields"] = ArrayOf(["jsonpath", "expected"]), ["matching"] = "wait until the JSONPath value equals expected", ["failure_mode"] = "diagnostic JSON condition not satisfied" },
            ["failure_modes"] = ArrayOf(["stale_process", "missing_artifact", "redacted_evidence", "diagnostic JSON not observed", "diagnostic JSON condition not satisfied"])
        },
        ["semantic_probe"] = new JsonObject { ["required_fields"] = ArrayOf(Required["semantic_probe"]), ["optional_fields"] = ArrayOf(["backend_result", "metrics", "route"]), ["failure_modes"] = ArrayOf(["unknown_probe", "selector_blocked", "unsafe_evidence"]), ["probe_kinds"] = ArrayOf(Probes) },
        ["tracepoint_guardrail"] = new JsonObject { ["required_fields"] = ArrayOf(Required["tracepoint_guardrail"]), ["optional_fields"] = ArrayOf(["owner", "ttl_seconds", "evidence_refs"]), ["mode_values"] = ArrayOf(["allow", "block", "unsafe"]), ["failure_modes"] = ArrayOf(["unsafe_expression", "unbounded_log", "cleanup_unowned"]), ["cleanup_operations"] = ArrayOf(["debug.tracepoint.remove", "debug.trace_log.clear"]), ["runtime_limits"] = native ? null : new JsonObject { ["max_trace_entries"] = EnvInt("NETCOREDBG_MAX_TRACE_ENTRIES", 1000), ["evaluate_timeout_seconds"] = EnvDouble("NETCOREDBG_EVALUATE_TIMEOUT", 0.5), ["rate_limit_interval_seconds"] = EnvDouble("NETCOREDBG_RATE_LIMIT_INTERVAL", 0.1) } },
        ["evidence_pack_manifest"] = new JsonObject { ["schema"] = "netcoredbg.runtime_smoke.evidence_pack", ["schema_version"] = "1.0", ["file_name"] = "pack-manifest.json", ["required_fields"] = ArrayOf(["schema", "schema_version", "pack_id", "run_id", "evidence_dir", "sources", "rollups"]), ["source_required_fields"] = ArrayOf(["id", "kind", "classification", "status"]), ["source_ref_fields"] = ArrayOf(["artifact_path"]), ["rollup_fields"] = ArrayOf(["cleanup", "freshness", "redaction", "limits"]), ["ref_policy"] = "refs are relative to evidence_dir and must stay inside it" }
    };
    private static string NormalizePythonNumber(string value)
    {
        var start = 0;
        var end = value.Length;
        while (start < end && char.IsWhiteSpace(value[start])) start++;
        while (end > start && char.IsWhiteSpace(value[end - 1])) end--;
        var builder = new StringBuilder(end - start);
        foreach (var rune in value[start..end].EnumerateRunes())
        {
            if (Rune.GetUnicodeCategory(rune) == UnicodeCategory.DecimalDigitNumber)
                builder.Append((char)('0' + (int)Rune.GetNumericValue(rune)));
            else builder.Append(rune.ToString());
        }
        return builder.ToString();
    }
    private static JsonNode EnvInt(string name, int fallback)
    {
        var value = Environment.GetEnvironmentVariable(name);
        if (value is null) return JsonValue.Create(fallback)!;
        var normalized = NormalizePythonNumber(value);
        if (!System.Text.RegularExpressions.Regex.IsMatch(normalized, @"\A[+-]?[0-9](?:_?[0-9])*\z", System.Text.RegularExpressions.RegexOptions.CultureInvariant))
            throw new FormatException($"Invalid integer in environment variable {name}.");
        var number = BigInteger.Parse(normalized.Replace("_", ""), NumberStyles.AllowLeadingSign, CultureInfo.InvariantCulture);
        return JsonNode.Parse(number.ToString(CultureInfo.InvariantCulture))!;
    }
    private static double EnvDouble(string name, double fallback)
    {
        var value = Environment.GetEnvironmentVariable(name);
        if (value is null) return fallback;
        var normalized = NormalizePythonNumber(value);
        const string digits = @"[0-9](?:_?[0-9])*";
        var pattern = $@"\A[+-]?(?:(?:{digits}(?:\.(?:{digits})?)?|\.{digits})(?:[eE][+-]?{digits})?|inf(?:inity)?|nan)\z";
        if (!System.Text.RegularExpressions.Regex.IsMatch(normalized, pattern, System.Text.RegularExpressions.RegexOptions.IgnoreCase | System.Text.RegularExpressions.RegexOptions.CultureInvariant) ||
            !double.TryParse(normalized.Replace("_", ""), NumberStyles.Float, CultureInfo.InvariantCulture, out var number))
            throw new FormatException($"Invalid float in environment variable {name}.");
        if (!double.IsFinite(number)) throw new InvalidOperationException($"Non-finite float in environment variable {name} cannot be represented in JSON.");
        return number;
    }

    internal static void ValidateDiagnostic(JsonObject payload, string kind, List<string> errors)
    {
        if (!Required.TryGetValue(kind, out var required)) { errors.Add("diagnostic kind must be one of: " + string.Join(", ", Required.Keys)); return; }
        Status(kind, payload["status"], errors);
        if (payload["schema"] is not null && !Eq(payload["schema"], Diagnostic)) errors.Add($"{kind}.schema must be {Diagnostic}");
        Require(kind, payload, required, errors);
        string[] objects = kind switch { "app_diagnostics" => ["app", "redaction", "limits"], "oracle_pack" => ["limits"], "semantic_probe" => ["selector", "expect", "on_blocked"], _ => ["cleanup"] };
        string[] lists = kind switch { "app_diagnostics" => ["observations"], "oracle_pack" => ["checks"], "tracepoint_guardrail" => ["allowed_when", "blocked_when", "unsafe_when"], _ => [] };
        foreach (var key in objects) if (payload.ContainsKey(key) && payload[key] is not JsonObject) errors.Add($"{kind}.{key} must be an object");
        foreach (var key in lists) if (payload.ContainsKey(key) && payload[key] is not JsonArray) errors.Add($"{kind}.{key} must be a list");
        if (payload["limits"] is JsonObject limits) foreach (var (key, maximum) in new[] { ("max_text_length", 240), ("max_list_items", 8), ("max_json_bytes", 32768) })
        {
            if (limits[key] is null) errors.Add($"{kind}.limits.{key} is required"); else if (!Int(limits[key], out var n)) errors.Add($"{kind}.limits.{key} must be an integer"); else if (n < 0) errors.Add($"{kind}.limits.{key} must be >= 0"); else if (n > maximum) errors.Add($"{kind}.limits.{key} must be <= {maximum}");
        }
        Unsafe(payload, kind, errors);
        switch (kind) { case "oracle_pack": Oracle(payload, errors); break; case "app_diagnostics": App(payload, errors); break; case "semantic_probe": ProbeName("semantic_probe.kind", payload["kind"], errors); if (Eq(payload["status"], "BLOCKED")) { if (payload["on_blocked"] is JsonObject blocked) Blocked("semantic_probe.on_blocked", blocked, errors); else errors.Add("semantic_probe.on_blocked must be an object for BLOCKED"); } break; case "tracepoint_guardrail": Guardrail(payload, errors); break; }
        if (kind == "tracepoint_guardrail" && payload.ContainsKey("mode") && !new[] { "allow", "block", "unsafe" }.Contains(Str(payload["mode"]))) errors.Add("tracepoint_guardrail.mode must be one of: allow, block, unsafe");
    }
    private static void Require(string prefix, JsonObject obj, IEnumerable<string> fields, List<string> errors) { foreach (var field in fields) if (!obj.ContainsKey(field)) errors.Add($"{prefix}.{field} is required"); }
    private static void Status(string prefix, JsonNode? status, List<string> errors) { if (status is not null && !Statuses.Contains(Str(status))) errors.Add($"{prefix}.status must be one of: {string.Join(", ", Statuses)}"); }
    private static void ProbeName(string path, JsonNode? value, List<string> errors) { if (value is not null && !Probes.Contains(Str(value))) errors.Add($"{path} must be one of: {string.Join(", ", Probes)}"); }
    private static void Blocked(string prefix, JsonObject obj, List<string> errors) { foreach (var key in new[] { "reason", "requested", "accepted", "next_step" }) if (obj[key] is null || Eq(obj[key], "")) errors.Add($"{prefix}.{key} is required for BLOCKED"); }
    private static void Unsafe(JsonNode? node, string path, List<string> errors)
    {
        if (node is JsonObject obj) foreach (var item in obj) { var p = path + "." + item.Key; if (Omitted.Contains(item.Key)) errors.Add($"{p} must be omitted or summarized"); Unsafe(item.Value, p, errors); }
        else if (node is JsonArray list) for (var i = 0; i < list.Count; i++) Unsafe(list[i], $"{path}[{i}]", errors);
    }
    private static void Oracle(JsonObject payload, List<string> errors)
    {
        if (payload["checks"] is JsonArray checks) for (var i = 0; i < checks.Count; i++)
        {
            var p = $"oracle_pack.checks[{i}]"; if (checks[i] is not JsonObject check) { errors.Add($"{p} must be an object"); continue; }
            Require(p, check, ["id", "probe", "expect", "on_blocked"], errors); ProbeName(p + ".probe", check["probe"], errors);
            foreach (var key in new[] { "expect", "on_blocked" }) if (check.ContainsKey(key) && check[key] is not JsonObject) errors.Add($"{p}.{key} must be an object");
            if (check["on_blocked"] is JsonObject blocked && (Str(blocked["next_step"]) is not string next || next.Length == 0)) errors.Add($"{p}.on_blocked.next_step is required");
        }
        if (payload["sources"] is null) return;
        if (payload["sources"] is not JsonArray sources) { errors.Add("oracle_pack.sources must be a list"); return; }
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (var i = 0; i < sources.Count; i++) { var p = $"oracle_pack.sources[{i}]"; if (sources[i] is not JsonObject source) { errors.Add($"{p} must be an object"); continue; } var id = Str(source["id"]); if (string.IsNullOrEmpty(id)) errors.Add($"{p}.id is required"); else if (!seen.Add(id)) errors.Add($"{p}.id duplicates earlier source id: {id}"); if (source["probe"] is not JsonObject probe) { errors.Add($"{p}.probe must be an object"); continue; } if (probe["kind"] is null) errors.Add($"{p}.probe.kind is required"); else if (Eq(probe["kind"], "oracle_pack")) errors.Add($"{p}.probe.kind must not be oracle_pack"); else ProbeName(p + ".probe.kind", probe["kind"], errors); }
    }
    private static void App(JsonObject payload, List<string> errors)
    {
        if (payload["observations"] is JsonArray observations) for (var i = 0; i < observations.Count; i++) { var p = $"app_diagnostics.observations[{i}]"; if (observations[i] is not JsonObject observation) { errors.Add($"{p} must be an object"); continue; } Status(p, observation["status"], errors); if (Eq(observation["status"], "BLOCKED")) Blocked(p, observation, errors); }
        if (payload["app"] is JsonObject app)
        {
            var pid = Truth(app["process_id"]) ? app["process_id"] : app["expected_process_id"];
            if (pid is not null && !Int(pid, out _)) errors.Add("app_diagnostics.app.process_id must be an integer");
            if (app["expected_modules"] is not null && !Strings(app["expected_modules"])) errors.Add("app_diagnostics.app.expected_modules must be a list of strings");
            if (app["require_active_process"] is not null && !Bool(app["require_active_process"])) errors.Add("app_diagnostics.app.require_active_process must be a boolean");
        }
        if (payload["workspace"] is not null && payload["workspace"] is not JsonObject && Str(payload["workspace"]) is null) errors.Add("app_diagnostics.workspace must be a string or object");
        if (payload["process"] is not null && payload["process"] is not JsonObject) errors.Add("app_diagnostics.process must be an object");
        if (payload["modules"] is not null && payload["modules"] is not JsonObject and not JsonArray) errors.Add("app_diagnostics.modules must be a list or object");
        if (payload["loaded_sources"] is not null && !Strings(payload["loaded_sources"])) errors.Add("app_diagnostics.loaded_sources must be a list of strings");
        if (payload["artifacts"] is not null && payload["artifacts"] is not JsonObject and not JsonArray) errors.Add("app_diagnostics.artifacts must be a list or object");
        foreach (var key in new[] { "modules", "artifacts" }) if (payload[key] is JsonObject field && field["expected"] is not null && !Strings(field["expected"])) errors.Add($"app_diagnostics.{key}.expected must be a list of strings");
        if (payload["wait_json"] is not null && payload["poll"] is not null) errors.Add("app_diagnostics.wait_json and app_diagnostics.poll are mutually exclusive");
        WaitJson(payload, "wait_json", errors); WaitJson(payload, "poll", errors);
    }
    private static void WaitJson(JsonObject payload, string key, List<string> errors)
    {
        if (payload[key] is null) return; var p = "app_diagnostics." + key;
        if (payload[key] is not JsonObject wait) { errors.Add($"{p} must be an object"); return; }
        if (Str(wait["path"]) is not string path || path.Length == 0) errors.Add($"{p}.path is required");
        if (wait["pattern"] is not null) { if (Str(wait["pattern"]) is not string pattern || pattern.Length == 0) errors.Add($"{p}.pattern must be a string"); else if (pattern.Contains('/') || pattern.Contains('\\')) errors.Add($"{p}.pattern must be a file-name pattern"); }
        if (wait["since"] is not null) { if (key != "poll") errors.Add($"{p}.since is not accepted"); else if (wait["since"] is not JsonObject since) errors.Add("app_diagnostics.poll.since must be an object"); else { if (!Int(since["mtime_ns"], out var n)) errors.Add("app_diagnostics.poll.since.mtime_ns must be an integer"); else if (n < 0) errors.Add("app_diagnostics.poll.since.mtime_ns must be >= 0"); if (Str(since["name"]) is not string name || name.Length == 0) errors.Add("app_diagnostics.poll.since.name must be a non-empty string"); } }
        if (wait["condition"] is not null) { if (key != "wait_json") errors.Add("app_diagnostics.poll.condition is not accepted"); else if (wait["condition"] is not JsonObject condition) errors.Add("app_diagnostics.wait_json.condition must be an object"); else { if (Str(condition["jsonpath"]) is not string jsonpath || jsonpath.Length == 0) errors.Add("app_diagnostics.wait_json.condition.jsonpath is required"); if (!condition.ContainsKey("expected")) errors.Add("app_diagnostics.wait_json.condition.expected is required"); } }
        foreach (var field in new[] { "timeout_ms", "poll_interval_ms" }) if (wait.ContainsKey(field)) { if (!Int(wait[field], out var n)) errors.Add($"{p}.{field} must be an integer"); else if (n < 0) errors.Add($"{p}.{field} must be >= 0"); }
    }
    private static void Guardrail(JsonObject payload, List<string> errors)
    {
        foreach (var key in new[] { "allowed_when", "blocked_when", "unsafe_when" }) if (payload[key] is JsonArray list) { if (list.Count == 0) errors.Add($"tracepoint_guardrail.{key} must not be empty"); else if (!Strings(list)) errors.Add($"tracepoint_guardrail.{key} must be a list of strings"); }
        if (payload["cleanup"] is not JsonObject cleanup) return;
        if (Str(cleanup["owner"]) is not string owner || owner.Length == 0) errors.Add("tracepoint_guardrail.cleanup.owner is required");
        if (!Strings(cleanup["operations"])) errors.Add("tracepoint_guardrail.cleanup.operations must be a list of strings");
        else if (!cleanup["operations"]!.AsArray().Any(n => Eq(n, "debug.tracepoint.remove"))) errors.Add("tracepoint_guardrail.cleanup.operations must include debug.tracepoint.remove");
    }
}
