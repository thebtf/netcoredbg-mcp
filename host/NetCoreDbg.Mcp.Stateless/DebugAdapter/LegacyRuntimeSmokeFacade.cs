using System.Text.Json;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed class LegacyRuntimeSmokeFacade
{
    private readonly LegacyRuntimeSmokeRuns _runs;

    internal LegacyRuntimeSmokeFacade() : this(new LegacyRuntimeSmokeRuns()) { }
    internal LegacyRuntimeSmokeFacade(LegacyRuntimeSmokeRuns runs) => _runs = runs;

    internal sealed record Prepared(JsonObject? Plan, JsonObject Data);

    internal static bool IsTool(string name) => name is "runtime_smoke_validate_probe" or "runtime_smoke_run_probe"
        or "runtime_smoke_run_plan" or "runtime_smoke_get_result" or "runtime_smoke_wait_for_result" or "runtime_smoke_stop";

    internal static bool RequiresOwner(string name) => name != "runtime_smoke_validate_probe" && IsTool(name);

    internal static bool ValidArguments(string name, IReadOnlyDictionary<string, JsonElement> arguments)
    {
        foreach (var key in arguments.Keys)
        {
            var valid = name switch
            {
                "runtime_smoke_validate_probe" or "runtime_smoke_run_probe" => key is "probe" or "name" or "phase" or "budgets" or "debug_preflight" or "tracepoint_guard" or "agent_mode",
                "runtime_smoke_run_plan" => key is "plan" or "plan_path" or "agent_mode",
                "runtime_smoke_get_result" or "runtime_smoke_stop" => key == "run_id",
                "runtime_smoke_wait_for_result" => key is "run_id" or "timeout_ms" or "poll_interval_ms" or "after_cursor" or "event_limit" or "agent_mode",
                _ => false,
            };
            if (!valid) return false;
        }
        return true;
    }

    internal Prepared Prepare(string name, IReadOnlyDictionary<string, JsonElement> arguments)
    {
        if (arguments.TryGetValue("agent_mode", out var agentMode) && agentMode.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            return new(null, Invalid("agent_mode must be a boolean"));
        if (agentMode.ValueKind == JsonValueKind.True)
            return new(null, Blocked("Native agent_mode guidance is not yet supported."));

        if (name is "runtime_smoke_validate_probe" or "runtime_smoke_run_probe")
        {
            foreach (var (key, value) in arguments)
            {
                var valid = key switch
                {
                    "name" => value.ValueKind is JsonValueKind.String or JsonValueKind.Null,
                    "phase" => value.ValueKind == JsonValueKind.String,
                    "budgets" or "tracepoint_guard" => value.ValueKind is JsonValueKind.Object or JsonValueKind.Null,
                    "debug_preflight" => value.ValueKind is JsonValueKind.True or JsonValueKind.False,
                    _ => true,
                };
                if (!valid) return new(null, Invalid($"{key} has an invalid type"));
            }
            if (!arguments.TryGetValue("probe", out var rawProbe) || rawProbe.ValueKind != JsonValueKind.Object)
                return new(null, Invalid("probe must be an object"));
            var options = new JsonObject();
            foreach (var key in new[] { "name", "phase", "budgets", "debug_preflight", "tracepoint_guard" })
                if (arguments.TryGetValue(key, out var value)) options[key] = JsonNode.Parse(value.GetRawText());
            try
            {
                var probe = JsonNode.Parse(rawProbe.GetRawText())!.AsObject();
                var (plan, validation) = LegacyRuntimeSmokeProbe.Prepare(probe, options);
                return new(name == "runtime_smoke_run_probe" && CanRun(validation) ? plan : null, validation);
            }
            catch (ArgumentException exception)
            {
                return new(null, Invalid(exception.Message));
            }
        }

        var hasPlan = arguments.TryGetValue("plan", out var rawPlan) && rawPlan.ValueKind != JsonValueKind.Null;
        var hasPath = arguments.TryGetValue("plan_path", out var rawPath) && rawPath.ValueKind == JsonValueKind.String && !string.IsNullOrEmpty(rawPath.GetString());
        if ((arguments.ContainsKey("plan_path") && rawPath.ValueKind is not (JsonValueKind.String or JsonValueKind.Null)) ||
            (hasPlan && rawPlan.ValueKind != JsonValueKind.Object))
            return new(null, Invalid("plan must be an object and plan_path must be a string"));
        if (hasPlan == hasPath)
            return new(null, Invalid("provide exactly one runtime smoke plan input: plan or plan_path"));
        if (hasPath)
            return new(null, Blocked("Native plan_path loading is not supported; provide an inline plan."));
        var inlinePlan = JsonNode.Parse(rawPlan.GetRawText())!.AsObject();
        var result = LegacyRuntimeSmokeFileRunner.Validate(inlinePlan);
        return new(CanRun(result) ? inlinePlan : null, result);
    }

    internal JsonObject Start(string name, Prepared prepared, string? owner)
    {
        if (prepared.Plan is null) return prepared.Data;
        var result = _runs.Start(prepared.Plan, owner);
        result["validation"] = ValidationSummary(prepared.Data);
        if (name == "runtime_smoke_run_probe")
        {
            result["run_created"] = result["run_id"] is not null;
            result["probe"] = prepared.Data["probe"]?.DeepClone();
            result["generated_plan"] = prepared.Data["generated_plan"]?.DeepClone();
        }
        return result;
    }

    internal JsonObject GetResult(string runId, string? owner) => _runs.GetResult(runId, owner);

    internal async Task<JsonObject> WaitAsync(string runId, string? owner, IReadOnlyDictionary<string, JsonElement> arguments, CancellationToken cancellationToken)
    {
        if (arguments.TryGetValue("agent_mode", out var mode) && mode.ValueKind is not (JsonValueKind.True or JsonValueKind.False))
            return Invalid("agent_mode must be a boolean");
        if (mode.ValueKind == JsonValueKind.True) return Blocked("Native agent_mode guidance is not yet supported.");
        if (!Number(arguments, "timeout_ms", 1000, 30000, out var timeout) ||
            !Number(arguments, "poll_interval_ms", 100, 30000, out var interval) ||
            !Number(arguments, "after_cursor", 0, int.MaxValue, out var cursor) ||
            !Number(arguments, "event_limit", 50, 128, out var limit))
            return Invalid("wait arguments must be non-negative integers within native limits");
        return await _runs.WaitForResultAsync(runId, owner, timeout, interval, cursor, limit, cancellationToken).ConfigureAwait(false);
    }

    internal Task<JsonObject> StopAsync(string runId, string? owner, CancellationToken cancellationToken) => _runs.StopAsync(runId, owner, cancellationToken);
    internal Task<JsonObject?> StopActiveAsync(string? owner, CancellationToken cancellationToken) => _runs.StopActiveAsync(owner, cancellationToken);
    internal Task CloseAsync(CancellationToken cancellationToken) => _runs.CloseAsync(cancellationToken);

    private static bool Number(IReadOnlyDictionary<string, JsonElement> arguments, string name, int fallback, int maximum, out int value)
    {
        value = fallback;
        return !arguments.TryGetValue(name, out var raw) ||
            (raw.ValueKind == JsonValueKind.Number && raw.TryGetInt32(out value) && value >= 0 && value <= maximum);
    }

    private static bool CanRun(JsonObject data) => data["can_run"] is JsonValue value && value.TryGetValue<bool>(out var canRun) && canRun;

    private static JsonObject ValidationSummary(JsonObject data)
    {
        var summary = new JsonObject();
        foreach (var key in new[] { "status", "can_run", "validation_errors", "case_count", "generated_case_count", "evidence_contract", "plan_source" })
            if (data.ContainsKey(key)) summary[key] = data[key]?.DeepClone();
        return summary;
    }

    private static JsonObject Invalid(string reason) => new()
    {
        ["status"] = "INVALID_SETUP",
        ["can_run"] = false,
        ["run_created"] = false,
        ["validation_errors"] = new JsonArray(reason),
        ["reason"] = reason,
    };

    private static JsonObject Blocked(string reason) => new()
    {
        ["status"] = "BLOCKED",
        ["can_run"] = false,
        ["run_created"] = false,
        ["reason"] = reason,
    };
}
