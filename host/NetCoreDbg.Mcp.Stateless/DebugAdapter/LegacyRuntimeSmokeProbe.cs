using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class LegacyRuntimeSmokeProbe
{
    private const string Schema = "netcoredbg.runtime_smoke.v2";

    internal static JsonObject Build(JsonObject probe, JsonObject options)
    {
        var payload = (JsonObject)probe.DeepClone();
        if (!payload.ContainsKey("phase") && !payload.ContainsKey("phases"))
            payload["phase"] = PythonString(Or(options["phase"], "after"));

        var kind = PythonString(Or(payload["kind"], ""));
        var probeName = PythonString(Or(payload["name"], Or(payload["kind"], "probe")));
        var planName = PythonString(Or(options["name"], $"run-probe-{probeName}"));
        var diagnosticLaunch = kind == "app_diagnostics"
            ? LegacyRuntimeSmokeValidation.AppDiagnosticsLaunchContract(planName)
            : null;
        var cleanup = TracepointCleanup(payload, options["tracepoint_guard"]);
        var caseObject = new JsonObject
        {
            ["id"] = "run_probe",
            ["transitions"] = new JsonArray(new JsonObject
            {
                ["id"] = "probe",
                ["action"] = new JsonObject { ["kind"] = "ui.noop" },
                ["settle"] = new JsonObject { ["idle_ms"] = 0 },
                ["probes"] = new JsonArray(payload),
            }),
        };
        if (cleanup.Count > 0)
            caseObject["cleanup"] = cleanup;

        var budgets = options["budgets"];
        var plan = new JsonObject
        {
            ["schema"] = Schema,
            ["name"] = planName,
            ["cases"] = new JsonArray(caseObject),
            ["budgets"] = Truthy(budgets)
                ? DictionaryCopy(budgets!)
                : new JsonObject { ["max_actions"] = 1, ["max_elapsed_seconds"] = 5 },
        };
        if (diagnosticLaunch is not null)
            plan["diagnostics"] = new JsonObject
            {
                ["app_diagnostics"] = new JsonObject { ["diagnostic_launch"] = diagnosticLaunch.DeepClone() },
            };
        var preflight = Truthy(options["debug_preflight"]);
        if (preflight)
            plan["baseline"] = new JsonObject
            {
                ["steps"] = new JsonArray(new JsonObject
                {
                    ["id"] = "debug_preflight",
                    ["kind"] = "debug_hygiene_preflight",
                    ["file"] = payload["file"]?.DeepClone(),
                    ["clear_breakpoints"] = true,
                    ["clear_trace_log"] = true,
                    ["clear_exception_filters"] = false,
                }),
            };

        var generated = new JsonObject
        {
            ["schema"] = Schema,
            ["plan_name"] = planName,
            ["case_count"] = 1,
            ["transition_count"] = 1,
            ["action_kind"] = "ui.noop",
            ["probe_kind"] = kind,
            ["probe_name"] = probeName,
            ["probe_phase"] = ProbePhase(payload),
        };
        if (preflight)
            generated["debug_preflight"] = true;
        if (diagnosticLaunch is not null)
            generated["diagnostic_launch"] = diagnosticLaunch;
        if (cleanup.Count > 0)
        {
            var operations = new JsonArray();
            for (var index = cleanup.Count - 1; index >= 0; index--)
                operations.Add(PythonString(Or(cleanup[index]!["kind"], "")));
            generated["tracepoint_guard"] = new JsonObject { ["cleanup_operations"] = operations };
        }
        return new JsonObject
        {
            ["plan"] = plan,
            ["generated_plan"] = generated,
            ["probe"] = new JsonObject
            {
                ["kind"] = PythonString(Or(probe["kind"], "")),
                ["name"] = PythonString(Or(probe["name"], Or(probe["kind"], ""))),
            },
        };
    }

    internal static (JsonObject Plan, JsonObject Validation) Prepare(JsonObject probe, JsonObject options)
    {
        var generated = Build(probe, options);
        var plan = (JsonObject)generated["plan"]!;
        var validation = LegacyRuntimeSmokeFileRunner.Validate(plan);
        validation["run_created"] = false;
        validation["probe"] = generated["probe"]?.DeepClone();
        validation["generated_plan"] = generated["generated_plan"]?.DeepClone();
        return (plan, validation);
    }

    private static JsonArray TracepointCleanup(JsonObject probe, JsonNode? guard)
    {
        var steps = new JsonArray();
        if (PythonString(Or(probe["kind"], "")) != "debug.tracepoint")
            return steps;
        if (!Truthy(guard))
            return steps;
        if (guard is not JsonObject guardObject)
            throw new ArgumentException($"'{PythonType(guard)}' object has no attribute 'get'");
        var rawCleanup = guardObject["cleanup"];
        var cleanup = Truthy(rawCleanup) ? DictionaryCopy(rawCleanup!) : new JsonObject();
        if (cleanup["operations"] is not JsonArray rawOperations)
            return steps;
        var operations = rawOperations.Select(PythonString).ToArray();
        if (operations.Contains("debug.trace_log.clear", StringComparer.Ordinal))
            steps.Add(new JsonObject { ["kind"] = "debug.trace_log.clear" });
        if (operations.Contains("debug.tracepoint.remove", StringComparer.Ordinal))
        {
            var file = PythonString(Or(probe["file"], "tracepoint")).Replace('\\', '/');
            file = file[(file.LastIndexOf('/') + 1)..];
            if (file.Length == 0)
                file = "tracepoint";
            steps.Add(new JsonObject
            {
                ["kind"] = "debug.tracepoint.remove",
                ["id"] = $"{file}:{PythonString(probe["line"])}",
                ["file"] = probe["file"]?.DeepClone(),
                ["line"] = probe["line"]?.DeepClone(),
            });
        }
        return steps;
    }

    private static string ProbePhase(JsonObject probe)
    {
        if (!probe.ContainsKey("phases"))
            return PythonString(Or(probe["phase"], "both"));
        var phases = probe["phases"];
        return phases is JsonArray array
            ? string.Join(",", array.Select(PythonString))
            : PythonString(phases);
    }

    private static JsonObject DictionaryCopy(JsonNode node)
    {
        if (node is JsonObject obj)
            return (JsonObject)obj.DeepClone();
        IEnumerable<JsonNode?> entries;
        if (node is JsonArray array) entries = array;
        else if (StringValue(node) is string text) entries = PythonCharacters(text);
        else throw new ArgumentException($"'{PythonType(node)}' object is not iterable");

        var result = new JsonObject();
        var index = 0;
        foreach (var entry in entries)
        {
            JsonNode?[] pair;
            if (entry is JsonArray elements) pair = elements.ToArray();
            else if (entry is JsonObject mapping) pair = mapping.Select(item => (JsonNode?)JsonValue.Create(item.Key)).ToArray();
            else if (StringValue(entry) is string str) pair = PythonCharacters(str);
            else throw new ArgumentException($"cannot convert dictionary update sequence element #{index} to a sequence");
            if (pair.Length != 2)
                throw new ArgumentException($"dictionary update sequence element #{index} has length {pair.Length}; 2 is required");
            if (pair[0] is JsonObject or JsonArray)
                throw new ArgumentException($"unhashable type: '{PythonType(pair[0])}'");
            if (StringValue(pair[0]) is string key) result[key] = pair[1]?.DeepClone();
            index++;
        }
        return result;
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

    private static string? StringValue(JsonNode? node) => node is not null && node.GetValueKind() == JsonValueKind.String
        ? node.GetValue<string>() : null;

    private static string PythonType(JsonNode? node)
    {
        if (node is null) return "NoneType";
        if (node is JsonObject) return "dict";
        if (node is JsonArray) return "list";
        return node.GetValueKind() switch
        {
            JsonValueKind.Null => "NoneType",
            JsonValueKind.String => "str",
            JsonValueKind.True or JsonValueKind.False => "bool",
            JsonValueKind.Number when !node.ToJsonString().Contains('.') && !node.ToJsonString().Contains('e') && !node.ToJsonString().Contains('E') => "int",
            _ => "float",
        };
    }

    private static JsonNode Or(JsonNode? value, string fallback) => Truthy(value) ? value! : JsonValue.Create(fallback)!;
    private static JsonNode Or(JsonNode? value, JsonNode fallback) => Truthy(value) ? value! : fallback;

    private static bool Truthy(JsonNode? value)
    {
        if (value is null) return false;
        if (value is JsonObject obj) return obj.Count != 0;
        if (value is JsonArray array) return array.Count != 0;
        return value.GetValueKind() switch
        {
            JsonValueKind.Null or JsonValueKind.False => false,
            JsonValueKind.String => value.GetValue<string>().Length != 0,
            JsonValueKind.Number => double.Parse(value.ToJsonString(), CultureInfo.InvariantCulture) != 0,
            _ => true,
        };
    }

    private static string PythonString(JsonNode? value) => LegacyRuntimeSmokeV2.PyString(value);

}
