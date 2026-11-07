using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class LegacyRuntimeSmokeV2
{
    private static readonly string[] Actions = ["noop", "ui.click", "ui.click_verified", "ui.double_click_verified", "ui.drag", "ui.grid.assert_range", "ui.grid.click_row", "ui.grid.double_click_row", "ui.grid.ensure_visible", "ui.grid.get_state", "ui.grid.right_click_row", "ui.grid.select", "ui.grid.select_row", "ui.hover", "ui.input.ensure_target", "ui.invoke", "ui.key_sequence", "ui.noop", "ui.right_click_verified", "ui.text.type_replace_selection", "wait"];
    private static readonly string[] Probes = ["app_diagnostics", "debug.evaluate", "debug.tracepoint", "file.json", "oracle_pack", "output.field", "output.since", "process.metric", "ui.grid", "ui.grid.viewport", "ui.property", "ui.text"];
    private static readonly string[] Phases = ["after", "before", "both"];
    private static readonly Regex Field = new(@"^[A-Za-z_][A-Za-z0-9_]*$", RegexOptions.CultureInvariant);
    private static readonly Regex FullField = new(@"^\{([A-Za-z_][A-Za-z0-9_]*)\}$", RegexOptions.CultureInvariant);
    private static readonly Regex SafeExpression = new("^\\s*[+-]?\\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*(?:\\[\\s*(?:\\d+|\"[^\"]*\"|'[^']*')\\s*\\])*)(?:\\.[A-Za-z_][A-Za-z0-9_]*(?:\\[\\s*(?:\\d+|\"[^\"]*\"|'[^']*')\\s*\\])*)*|(?:\\d+(?:\\.\\d+)?|true|false|null|None|\"[^\"]*\"|'[^']*'))(?:\\s*[+\\-*/]\\s*(?:(?:[A-Za-z_][A-Za-z0-9_]*(?:\\[\\s*(?:\\d+|\"[^\"]*\"|'[^']*')\\s*\\])*)(?:\\.[A-Za-z_][A-Za-z0-9_]*(?:\\[\\s*(?:\\d+|\"[^\"]*\"|'[^']*')\\s*\\])*)*|(?:\\d+(?:\\.\\d+)?|true|false|null|None|\"[^\"]*\"|'[^']*')))*\\s*\\z", RegexOptions.CultureInvariant);

    internal static JsonObject Validate(JsonObject plan)
    {
        var errors = new List<string>();
        var generated = Generate(plan, errors);
        var declared = plan["cases"] as JsonArray;
        var cases = new List<JsonNode?>();
        if (declared is not null) cases.AddRange(declared);
        cases.AddRange(generated);
        ValidateCases(cases, errors);
        int count = (declared?.Count(item => item is JsonObject) ?? 0) + generated.Count;
        if (errors.Count == 0 && count == 0 && (!plan.ContainsKey("cases") || declared is not null))
            errors.Add("runtime smoke v2 plan has no cases to execute");
        return new JsonObject
        {
            ["case_count"] = count,
            ["generated_case_count"] = generated.Count,
            ["validation_errors"] = Strings(errors),
            ["accepted_action_kinds"] = Strings(Actions),
            ["accepted_probe_kinds"] = Strings(Probes),
            ["accepted_probe_phases"] = Strings(Phases),
        };
    }

    private static void ValidateCases(List<JsonNode?> cases, List<string> errors)
    {
        var ids = new HashSet<string>(StringComparer.Ordinal);
        for (int ci = 0; ci < cases.Count; ci++)
        {
            if (cases[ci] is not JsonObject item) { errors.Add($"cases[{ci}] must be an object"); continue; }
            string id = Truth(item["id"]) ? PyString(item["id"]) : "";
            if (!ids.Add(id)) errors.Add($"duplicate case id: {id}");
            JsonNode? transitionsNode = Get(item, "transitions", new JsonArray());
            if (transitionsNode is not JsonArray transitions) { errors.Add($"cases[{ci}].transitions must be a list"); continue; }
            for (int ti = 0; ti < transitions.Count; ti++)
            {
                string prefix = $"cases[{ci}].transitions[{ti}]";
                if (transitions[ti] is not JsonObject transition) { errors.Add($"{prefix} must be an object"); continue; }
                JsonNode? actionNode = transition["action"];
                if (actionNode is not null && actionNode is not JsonObject) errors.Add($"{prefix}.action must be an object");
                else if (actionNode is JsonObject action)
                {
                    if (action.Count == 0) errors.Add($"{prefix}.action.kind is required when action is present");
                    else
                    {
                        if (!Contains(Actions, action["kind"])) errors.Add($"{prefix}.action.kind is not accepted: {PyString(action["kind"])}");
                        if (action.ContainsKey("input_policy")) LegacyRuntimeSmokeValidation.ValidateInputPolicy(prefix + ".action.input_policy", action["input_policy"], errors);
                    }
                }
                if (Get(transition, "probes", new JsonArray()) is not JsonArray probes) { errors.Add($"{prefix}.probes must be a list"); continue; }
                var paths = new HashSet<string>(StringComparer.Ordinal);
                for (int pi = 0; pi < probes.Count; pi++)
                {
                    string probePrefix = $"{prefix}.probes[{pi}]";
                    if (probes[pi] is not JsonObject probe) { errors.Add($"{probePrefix} must be an object"); continue; }
                    string? phaseError = PhaseError(probe);
                    if (phaseError is not null) { errors.Add($"{probePrefix}.{phaseError}"); continue; }
                    if (!Contains(Probes, probe["kind"])) { errors.Add($"{probePrefix}.kind is not accepted: {PyString(probe["kind"])}"); continue; }
                    string kind = PyString(probe["kind"]);
                    if (kind is "oracle_pack" or "app_diagnostics") LegacyRuntimeSmokeValidation.ValidateDiagnostic(probe, kind, errors);
                    if (kind == "debug.tracepoint")
                    {
                        string expression = Truth(probe["expression"]) ? NormalizeExpression(PyString(probe["expression"])).Trim() : "";
                        if (expression.Length == 0) errors.Add($"{probePrefix}.expression unsafe tracepoint expression: expression is required");
                        else if (!SafeExpression.IsMatch(expression)) errors.Add($"{probePrefix}.expression unsafe tracepoint expression");
                    }
                    string path = kind;
                    if (probe["name"] is not null && PyString(probe["name"]).Length != 0) path += "." + PyString(probe["name"]);
                    foreach (string phase in new[] { "before", "after" })
                    {
                        if (!RunsInPhase(probe, phase)) continue;
                        if (!paths.Add(phase + ":" + path)) errors.Add($"{prefix} has duplicate probe path for {phase}: {path}");
                    }
                }
            }
        }
    }

    private static string NormalizeExpression(string expression)
    {
        var result = new StringBuilder(expression.Length);
        foreach (var rune in expression.EnumerateRunes())
        {
            if (rune.Value is >= 0x1c and <= 0x1f) result.Append(' ');
            else if (rune.Value > 0xffff && Rune.GetUnicodeCategory(rune) == System.Globalization.UnicodeCategory.DecimalDigitNumber) result.Append('\u0660');
            else result.Append(rune.ToString());
        }
        return result.ToString();
    }

    private static string? PhaseError(JsonObject probe)
    {
        if (probe.ContainsKey("phase") && probe.ContainsKey("phases")) return "phase must not be combined with phases";
        if (probe.ContainsKey("phase") && !Phases.Contains(PyString(probe["phase"]), StringComparer.Ordinal)) return $"phase is not accepted: {PyString(probe["phase"])}";
        if (!probe.ContainsKey("phases")) return null;
        JsonNode? value = probe["phases"];
        List<string> phases;
        if (IsString(value)) phases = [PyString(value)];
        else if (value is JsonArray array) phases = array.Select(PyString).ToList();
        else return "phases must be a list of accepted phase names";
        if (phases.Count == 0) return "phases must contain at least one accepted phase name";
        var invalid = phases.Where(p => !Phases.Contains(p, StringComparer.Ordinal)).ToList();
        return invalid.Count == 0 ? null : "phases contains unaccepted values: [" + string.Join(", ", invalid.Select(Quote)) + "]";
    }

    private static bool RunsInPhase(JsonObject probe, string phase)
    {
        JsonNode? value = probe["phases"] ?? probe["phase"];
        if (value is null) return true;
        if (value is JsonArray array) return array.Any(v => PyString(v) is "both" || PyString(v) == phase);
        string text = PyString(value);
        return text == "both" || text == phase;
    }

    internal static List<JsonObject> Generate(JsonObject plan, List<string> errors)
    {
        var result = new List<JsonObject>();
        if (plan["generate"] is null) return result;
        if (plan["generate"] is not JsonObject generate) { errors.Add("generate must be an object"); return result; }
        string name = Truth(generate["template"]) ? PyString(generate["template"]) : "";
        string? defaultPattern = name switch
        {
            "novascript-action-oracle" or "state-only-file-json" => "{id}",
            "radio-group-set" or "toggle-setting-ab" => "{id}.{value}",
            "setting-ab-row-effect" => "{id}.row-{row_index}.{value}",
            _ => null,
        };
        if (defaultPattern is null) { errors.Add($"generate.template is not accepted: {name}"); return result; }
        if (Get(generate, "matrix", new JsonArray()) is not JsonArray matrix) { errors.Add("generate.matrix must be a list"); return result; }
        string pattern = Truth(generate["id_pattern"]) ? PyString(generate["id_pattern"]) : defaultPattern;
        for (int i = 0; i < matrix.Count; i++)
        {
            if (matrix[i] is not JsonObject original) { errors.Add($"generate.matrix[{i}] must be an object"); continue; }
            var record = (JsonObject)original.DeepClone();
            record["index"] = i;
            try
            {
                JsonObject rendered = RenderCase(name, record, pattern);
                rendered["rendered_from"] = original.DeepClone();
                result.Add(rendered);
            }
            catch (TemplateException ex) { errors.Add($"generate.matrix[{i}]: {ex.Message}"); }
        }
        return result;
    }

    private static JsonObject RenderCase(string template, JsonObject record, string pattern)
    {
        JsonObject transition;
        if (template is "state-only-file-json" or "novascript-action-oracle")
        {
            transition = new JsonObject { ["probes"] = FileProbes(record) };
            if (record["action"] is JsonObject action) transition["action"] = Render(action, record);
            else if (record.ContainsKey("wait_ms")) transition["action"] = new JsonObject { ["kind"] = "wait", ["idle_ms"] = Render(record["wait_ms"], record) };
            if (record["settle"] is JsonObject settle) transition["settle"] = Render(settle, record);
            else if (record.ContainsKey("settle_ms")) transition["settle"] = new JsonObject { ["idle_ms"] = Render(record["settle_ms"], record) };
        }
        else
        {
            JsonObject selector = Selector(record, "selector", "control");
            JsonNode? expected = Get(record, "expected", record["value"]);
            var probes = new JsonArray();
            if (template == "radio-group-set")
            {
                if (Truth(record["controls"]) && record["controls"] is JsonValue controlsValue && !IsString(controlsValue))
                    throw new ArgumentException($"'{PythonType(controlsValue)}' object is not iterable");
                JsonNode? target = Get(record, "target", record["value"]);
                var controls = record["controls"] is JsonArray controlArray ? controlArray.OfType<JsonObject>().ToList() : [];
                JsonObject? chosen = controls.FirstOrDefault(c => PythonEqual(c["value"], target) || PythonEqual(c["id"], target));
                selector = chosen is null ? new JsonObject { ["automation_id"] = Truth(target) ? PyString(target) : "" } : Selector(chosen, "selector", "automation_id", true);
                JsonNode? expression = Render(JsonValue.Create(TruthyString(record["setting_expression"], record["expression"], "{id}")), record);
                probes.Add(new JsonObject { ["kind"] = "debug.evaluate", ["name"] = "selected_value", ["expression"] = expression, ["expected"] = Clone(target) });
                probes.Add(PropertyProbe(PyString(target) + "_selected", selector, TruthyString(record["property"], null, "IsChecked"), JsonValue.Create(true)));
                if (!record.ContainsKey("assert_siblings") || Truth(record["assert_siblings"]))
                {
                    for (int i = 0; i < controls.Count; i++)
                    {
                        JsonObject control = controls[i];
                        if (ReferenceEquals(control, chosen)) continue;
                        JsonNode? siblingName = Get(control, "value", Get(control, "id", JsonValue.Create(i)));
                        probes.Add(PropertyProbe(PyString(siblingName) + "_selected", Selector(control, "selector", "automation_id", true), TruthyString(record["property"], null, "IsChecked"), JsonValue.Create(false)));
                    }
                }
            }
            else
            {
                JsonNode? expression = Render(JsonValue.Create(TruthyString(record["setting_expression"], record["expression"], "{id}")), record);
                probes.Add(new JsonObject { ["kind"] = "debug.evaluate", ["name"] = "setting", ["expression"] = expression, ["expected"] = Clone(expected) });
                if (template == "toggle-setting-ab")
                {
                    var consumer = record["consumer_selector"] is JsonObject consumerSelector ? (JsonObject)consumerSelector.DeepClone() : (JsonObject)selector.DeepClone();
                    probes.Add(PropertyProbe("consumer", consumer, TruthyString(record["property"], null, "ToggleState"), expected));
                }
                else
                {
                    var row = record["row_expected"] is JsonObject rowExpected
                        ? (JsonObject)rowExpected.DeepClone()
                        : new JsonObject { ["row_index"] = Clone(record["row_index"]), ["expected"] = Clone(expected) };
                    var columns = new JsonArray();
                    if (record["columns"] is JsonArray columnArray)
                    {
                        foreach (var column in columnArray) columns.Add(PyString(column));
                    }
                    else if (record["columns"] is not null) columns.Add(PyString(record["columns"]));
                    probes.Add(new JsonObject
                    {
                        ["kind"] = "ui.grid",
                        ["name"] = "row_effect",
                        ["selector"] = Selector(record, "grid_selector", "grid"),
                        ["rows"] = new JsonArray(row),
                        ["columns"] = columns,
                    });
                }
            }
            var keyboard = new JsonObject { ["kind"] = "ui.key_sequence", ["selector"] = selector.DeepClone(), ["keys"] = TruthyString(record["keys"], null, "{SPACE}") };
            if (record.ContainsKey("realize")) keyboard["realize"] = Truth(record["realize"]);
            transition = new JsonObject { ["action"] = keyboard, ["probes"] = probes };
        }
        return new JsonObject { ["id"] = PyString(Render(JsonValue.Create(pattern), record)), ["transitions"] = new JsonArray(transition) };
    }

    private static JsonObject PropertyProbe(string name, JsonObject selector, string property, JsonNode? expected) => new()
    {
        ["kind"] = "ui.property",
        ["name"] = name,
        ["selector"] = selector.DeepClone(),
        ["property"] = property,
        ["expected"] = Clone(expected),
    };

    private static JsonObject Selector(JsonObject record, string selectorKey, string automationKey, bool radio = false)
    {
        if (record[selectorKey] is JsonObject selector) return (JsonObject)selector.DeepClone();
        JsonNode? value = record[automationKey];
        if (radio) value = Truth(value) ? value : record["id"];
        else if (value is null) value = record["automation_id"];
        return new JsonObject { ["automation_id"] = Truth(value) ? PyString(value) : "" };
    }

    private static JsonArray FileProbes(JsonObject record)
    {
        JsonArray oracles;
        if (record["oracles"] is JsonArray array) oracles = array;
        else
        {
            var single = new JsonObject { ["name"] = Clone(Get(record, "name", JsonValue.Create("value"))), ["jsonpath"] = Clone(Get(record, "jsonpath", JsonValue.Create(""))) };
            if (record.ContainsKey("expected")) single["expected"] = Clone(record["expected"]);
            else if (record.ContainsKey("expect")) single["expect"] = Clone(record["expect"]);
            oracles = new JsonArray(single);
        }
        var probes = new JsonArray();
        for (int i = 0; i < oracles.Count; i++)
        {
            if (oracles[i] is not JsonObject oracle) continue;
            string kind = Truth(oracle["kind"]) ? PyString(oracle["kind"]) : "file.json";
            string phase = PyString(Get(oracle, "phase", JsonValue.Create("after")));
            string name = PyString(Get(oracle, "name", JsonValue.Create($"oracle_{i}")));
            JsonObject probe;
            if (kind != "file.json")
            {
                probe = (JsonObject)Render(oracle, record)!;
                probe["kind"] = kind;
                if (!probe.ContainsKey("phase")) probe["phase"] = phase;
                if (!probe.ContainsKey("name")) probe["name"] = name;
            }
            else
            {
                probe = new JsonObject
                {
                    ["kind"] = kind,
                    ["phase"] = phase,
                    ["name"] = name,
                    ["path"] = Render(Get(oracle, "path", Get(record, "path", Get(record, "evidence_path", JsonValue.Create("")))), record),
                    ["jsonpath"] = Render(Get(oracle, "jsonpath", Get(oracle, "path_expr", Get(record, "jsonpath", JsonValue.Create("")))), record),
                };
                if (oracle.ContainsKey("expected")) probe["expected"] = Render(oracle["expected"], record);
                else if (oracle.ContainsKey("expect")) probe["expect"] = Render(oracle["expect"], record);
            }
            probes.Add(probe);
        }
        return probes;
    }

    private static JsonNode? Render(JsonNode? value, JsonObject record)
    {
        if (value is JsonObject obj)
        {
            var result = new JsonObject();
            foreach (var pair in obj) result[pair.Key] = Render(pair.Value, record);
            return result;
        }
        if (value is JsonArray array)
        {
            var result = new JsonArray();
            foreach (var element in array) result.Add(Render(element, record));
            return result;
        }
        if (!IsString(value)) return Clone(value);
        string text = PyString(value);
        Match full = FullField.Match(text);
        if (full.Success) return FieldValue(full.Groups[1].Value, record)?.DeepClone();
        var output = new StringBuilder();
        foreach (var part in ParseTemplate(text))
        {
            output.Append(part.Literal);
            if (part.Field is null) continue;
            if (!Field.IsMatch(part.Field) || part.HasConversion || part.HasFormat)
                throw new TemplateException($"unsupported placeholder syntax: {part.Field}");
            JsonNode? replacement = FieldValue(part.Field, record);
            output.Append(IsBoolean(replacement) ? (Truth(replacement) ? "true" : "false") : PyString(replacement));
        }
        return JsonValue.Create(output.ToString());
    }

    private static List<(string Literal, string? Field, bool HasConversion, bool HasFormat)> ParseTemplate(string text)
    {
        var parts = new List<(string, string?, bool, bool)>();
        var literal = new StringBuilder();
        int index = 0;
        while (index < text.Length)
        {
            char c = text[index++];
            if (c is not ('{' or '}')) { literal.Append(c); continue; }
            if (index < text.Length && text[index] == c) { literal.Append(c); index++; continue; }
            if (c == '}') throw Malformed(text, "Single '}' encountered in format string");
            if (index == text.Length) throw Malformed(text, "Single '{' encountered in format string");
            int start = index;
            while (index < text.Length && text[index] is not ('}' or ':' or '!'))
            {
                if (text[index] == '{') throw Malformed(text, "unexpected '{' in field name");
                if (text[index] == '[')
                {
                    index++;
                    while (index < text.Length && text[index] != ']') index++;
                    if (index == text.Length) break;
                }
                index++;
            }
            if (index == text.Length) throw Malformed(text, "expected '}' before end of string");
            string field = text[start..index];
            bool conversion = false;
            bool format = false;
            if (text[index] == '!')
            {
                index++;
                if (index == text.Length) throw Malformed(text, "end of string while looking for conversion specifier");
                conversion = text[index] != '\0';
                index += Rune.TryGetRuneAt(text, index, out var conversionRune) ? conversionRune.Utf16SequenceLength : 1;
                if (index == text.Length) throw Malformed(text, "unmatched '{' in format spec");
                if (text[index] is not (':' or '}')) throw Malformed(text, "expected ':' after conversion specifier");
            }
            if (text[index] == ':')
            {
                index++;
                int formatStart = index;
                int depth = 0;
                while (index < text.Length)
                {
                    if (text[index] == '{') depth++;
                    else if (text[index] == '}')
                    {
                        if (depth == 0) break;
                        depth--;
                    }
                    index++;
                }
                if (index == text.Length) throw Malformed(text, "unmatched '{' in format spec");
                format = index != formatStart;
            }
            index++;
            parts.Add((literal.ToString(), field, conversion, format));
            literal.Clear();
        }
        if (literal.Length != 0) parts.Add((literal.ToString(), null, false, false));
        return parts;
    }

    private static TemplateException Malformed(string text, string reason) => new($"malformed template {Quote(text)}: {reason}");

    private static JsonNode? FieldValue(string name, JsonObject record)
    {
        if (!record.ContainsKey(name)) throw new TemplateException($"unknown placeholder: {name}");
        return record[name];
    }

    private sealed class TemplateException(string message) : Exception(message) { }
    private static JsonNode? Get(JsonObject obj, string key, JsonNode? fallback) => obj.TryGetPropertyValue(key, out var value) ? value : fallback;
    private static JsonNode? Clone(JsonNode? node) => node?.DeepClone();
    private static JsonArray Strings(IEnumerable<string> values) { var result = new JsonArray(); foreach (string value in values) result.Add(value); return result; }
    private static bool Contains(string[] choices, JsonNode? value)
    {
        return IsString(value) && choices.Contains(PyString(value), StringComparer.Ordinal);
    }
    private static string PythonType(JsonNode? value) => value switch
    {
        null => "NoneType",
        JsonArray => "list",
        JsonObject => "dict",
        _ when IsBoolean(value) => "bool",
        _ when IsString(value) => "str",
        JsonValue v when v.GetValueKind() == JsonValueKind.Number => Integer(v, out _) ? "int" : "float",
        _ => "unknown",
    };
    private static bool IsString(JsonNode? value) => value is JsonValue v && v.GetValueKind() == JsonValueKind.String;
    private static bool IsBoolean(JsonNode? value) => value is JsonValue v && v.GetValueKind() is JsonValueKind.True or JsonValueKind.False;
    private static bool Truth(JsonNode? value) => value switch
    {
        null => false,
        JsonArray a => a.Count != 0,
        JsonObject o => o.Count != 0,
        JsonValue v when v.GetValueKind() == JsonValueKind.String => v.GetValue<string>().Length != 0,
        JsonValue v when v.GetValueKind() == JsonValueKind.False => false,
        JsonValue v when v.GetValueKind() == JsonValueKind.Number => Number(v) != 0,
        _ => true,
    };
    private static string TruthyString(JsonNode? first, JsonNode? second, string fallback) => Truth(first) ? PyString(first) : Truth(second) ? PyString(second) : fallback;
    private static bool PythonEqual(JsonNode? left, JsonNode? right)
    {
        if (left is null || right is null) return left is null && right is null;
        if (left is JsonArray leftArray && right is JsonArray rightArray)
            return leftArray.Count == rightArray.Count && leftArray.Zip(rightArray).All(pair => PythonEqual(pair.First, pair.Second));
        if (left is JsonObject leftObject && right is JsonObject rightObject)
            return leftObject.Count == rightObject.Count && leftObject.All(pair => rightObject.TryGetPropertyValue(pair.Key, out var value) && PythonEqual(pair.Value, value));
        if (left is JsonValue leftValue && right is JsonValue rightValue &&
            (IsBoolean(left) || leftValue.GetValueKind() == JsonValueKind.Number) &&
            (IsBoolean(right) || rightValue.GetValueKind() == JsonValueKind.Number))
        {
            bool leftInteger = Integer(leftValue, out var li);
            bool rightInteger = Integer(rightValue, out var ri);
            if (leftInteger && rightInteger) return li == ri;
            double l = leftInteger ? 0 : Number(leftValue);
            double r = rightInteger ? 0 : Number(rightValue);
            if (leftInteger) return double.IsFinite(r) && Math.Truncate(r) == r && li == new System.Numerics.BigInteger(r);
            if (rightInteger) return double.IsFinite(l) && Math.Truncate(l) == l && ri == new System.Numerics.BigInteger(l);
            return l == r;
        }
        return JsonNode.DeepEquals(left, right);
    }
    private static double Number(JsonValue value) => double.Parse(value.ToJsonString(), System.Globalization.CultureInfo.InvariantCulture);
    private static bool Integer(JsonValue value, out System.Numerics.BigInteger result)
    {
        if (IsBoolean(value)) { result = Truth(value) ? 1 : 0; return true; }
        return System.Numerics.BigInteger.TryParse(value.ToJsonString(), System.Globalization.NumberStyles.AllowLeadingSign, System.Globalization.CultureInfo.InvariantCulture, out result);
    }
    private static string Quote(string value)
    {
        char quote = value.Contains('\'') && !value.Contains('"') ? '"' : '\'';
        var result = new StringBuilder().Append(quote);
        foreach (var rune in value.EnumerateRunes())
        {
            int c = rune.Value;
            if (c == '\\' || c == quote) result.Append('\\').Append((char)c);
            else if (c == '\n') result.Append("\\n");
            else if (c == '\r') result.Append("\\r");
            else if (c == '\t') result.Append("\\t");
            else
            {
                var category = Rune.GetUnicodeCategory(rune);
                bool nonprintable = category is System.Globalization.UnicodeCategory.Control or System.Globalization.UnicodeCategory.Format
                    or System.Globalization.UnicodeCategory.Surrogate or System.Globalization.UnicodeCategory.PrivateUse
                    or System.Globalization.UnicodeCategory.OtherNotAssigned or System.Globalization.UnicodeCategory.LineSeparator
                    or System.Globalization.UnicodeCategory.ParagraphSeparator || category == System.Globalization.UnicodeCategory.SpaceSeparator && c != ' ';
                if (!nonprintable) result.Append(rune.ToString());
                else if (c <= 0xff) result.Append("\\x").Append(c.ToString("x2", System.Globalization.CultureInfo.InvariantCulture));
                else if (c <= 0xffff) result.Append("\\u").Append(c.ToString("x4", System.Globalization.CultureInfo.InvariantCulture));
                else result.Append("\\U").Append(c.ToString("x8", System.Globalization.CultureInfo.InvariantCulture));
            }
        }
        return result.Append(quote).ToString();
    }
    internal static string PyString(JsonNode? node)
    {
        if (node is null) return "None";
        if (node is JsonArray array) return "[" + string.Join(", ", array.Select(PyRepr)) + "]";
        if (node is JsonObject obj) return "{" + string.Join(", ", obj.Select(p => Quote(p.Key) + ": " + PyRepr(p.Value))) + "}";
        var value = (JsonValue)node;
        return value.GetValueKind() switch
        {
            JsonValueKind.String => value.GetValue<string>(),
            JsonValueKind.True => "True",
            JsonValueKind.False => "False",
            JsonValueKind.Number => PythonNumber(value.ToJsonString()),
            _ => value.ToJsonString(),
        };
    }
    private static string PythonNumber(string raw)
    {
        if (!raw.Contains('.') && !raw.Contains('e') && !raw.Contains('E')) return raw == "-0" ? "0" : raw;
        var culture = System.Globalization.CultureInfo.InvariantCulture;
        double number = double.Parse(raw, culture);
        if (double.IsPositiveInfinity(number)) return "inf";
        if (double.IsNegativeInfinity(number)) return "-inf";
        string text = number.ToString("R", culture).Replace('E', 'e');
        bool negative = text.StartsWith('-');
        string unsigned = negative ? text[1..] : text;
        int exponentAt = unsigned.IndexOf('e');
        string mantissa = exponentAt < 0 ? unsigned : unsigned[..exponentAt];
        int exponent = exponentAt < 0 ? 0 : int.Parse(unsigned[(exponentAt + 1)..], culture);
        int point = mantissa.IndexOf('.');
        int decimalPosition = (point < 0 ? mantissa.Length : point) + exponent;
        string digits = mantissa.Replace(".", "", StringComparison.Ordinal);
        int leading = 0;
        while (leading < digits.Length - 1 && digits[leading] == '0') leading++;
        digits = digits[leading..];
        decimalPosition -= leading;
        while (digits.Length > 1 && digits[^1] == '0') digits = digits[..^1];
        int scientificExponent = decimalPosition - 1;
        string sign = negative ? "-" : "";
        if (scientificExponent < -4 || scientificExponent >= 16)
        {
            string fraction = digits.Length > 1 ? "." + digits[1..] : "";
            return sign + digits[0] + fraction + "e" + (scientificExponent < 0 ? "-" : "+") + Math.Abs(scientificExponent).ToString("D2", culture);
        }
        if (decimalPosition <= 0) return sign + "0." + new string('0', -decimalPosition) + digits;
        if (decimalPosition >= digits.Length) return sign + digits + new string('0', decimalPosition - digits.Length) + ".0";
        return sign + digits[..decimalPosition] + "." + digits[decimalPosition..];
    }
    private static string PyRepr(JsonNode? node) => IsString(node) ? Quote(PyString(node)) : PyString(node);
}
