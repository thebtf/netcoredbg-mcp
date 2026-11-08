using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class LegacyRuntimeSmokeFileRunner
{
    private const int MaxFileBytes = 16 * 1024 * 1024;
    private static readonly Regex JsonPath = new(@"\A\$(?:\.[A-Za-z_][A-Za-z0-9_]*)+\z", RegexOptions.CultureInvariant);

    internal static JsonObject Validate(JsonNode? plan)
    {
        var result = LegacyRuntimeSmokeValidation.ValidateNative(plan);
        result["accepted_action_kinds"] = new JsonArray("ui.noop");
        result["accepted_probe_kinds"] = new JsonArray("file.json", "output.since", "output.field", "debug.evaluate");
        result["accepted_probe_phases"] = new JsonArray("after");
        result["native_execution_subset"] = "One inline v2 case with up to 16 after-only file.json (Windows fixed drive), literal output.since, or output.field probes, or one debug.evaluate probe in a selected stopped frame; optional ui.noop, default budgets, and no cleanup. Validation vocabulary does not imply broader execution support.";
        if (Text(result["status"]) == "INVALID_SETUP") return result;
        string? reason = Unsupported(plan);
        if (reason is not null)
        {
            result["status"] = "BLOCKED";
            result["can_run"] = false;
            result["reason"] = reason;
            result["blocked"] = new JsonObject
            {
                ["reason"] = reason,
                ["requested"] = new JsonObject(),
                ["accepted"] = new JsonObject { ["native_subset"] = "one inline v2 case with up to 16 supported read-only after-only probes or one debug.evaluate probe, optional ui.noop and default budgets, and no cleanup" },
                ["next_step"] = "Use a supported plan or run this plan with the Python runtime."
            };
        }
        else
        {
            result["status"] = "PASS";
            result["can_run"] = true;
        }
        return result;
    }

    private static string? Unsupported(JsonNode? plan)
    {
        if (plan is not JsonObject root || Text(root["schema"]) != "netcoredbg.runtime_smoke.v2") return "only inline v2 plans are supported";
        if (!Only(root, "schema", "name", "description", "cases", "budgets")) return "unsupported plan field";
        if (root.ContainsKey("budgets") && (root["budgets"] is not JsonObject budgets || !Only(budgets, "max_actions", "max_elapsed_seconds") || budgets.Count != 2 || !IntegerEquals(budgets["max_actions"], 1) || !IntegerEquals(budgets["max_elapsed_seconds"], 5))) return "only default budgets max_actions=1 and max_elapsed_seconds=5 are supported";
        if (root["cases"] is not JsonArray { Count: 1 } cases || cases[0] is not JsonObject item) return "exactly one inline case is supported";
        if (!Only(item, "id", "transitions")) return "unsupported case field";
        if (item["id"] is not null && Text(item["id"]) is null) return "case id must be a string";
        if (item["transitions"] is not JsonArray { Count: 1 } transitions || transitions[0] is not JsonObject transition) return "exactly one transition is supported";
        if (!Only(transition, "id", "action", "settle", "probes")) return "unsupported transition field";
        if (transition.ContainsKey("action") && (transition["action"] is not JsonObject action || !Only(action, "kind") || Text(action["kind"]) != "ui.noop")) return "only ui.noop without extra action fields is supported";
        if (transition["id"] is not null && Text(transition["id"]) is null) return "transition id must be a string";
        if (transition.ContainsKey("settle") && (transition["settle"] is not JsonObject settle || !Only(settle, "idle_ms") || (settle.ContainsKey("idle_ms") && (settle["idle_ms"] is not JsonValue idle || !idle.TryGetValue<int>(out int ms) || ms != 0)))) return "only settle idle_ms=0 or the default settle is supported";
        if (transition["probes"] is not JsonArray { Count: >= 1 and <= 16 } probes) return "one to 16 probes are supported";
        if (probes.Count > 1 && probes.Any(node => node is JsonObject candidate && Text(candidate["kind"]) == "debug.evaluate")) return "debug.evaluate must be the only probe";
        foreach (var node in probes)
        {
            if (node is not JsonObject candidate) return "each probe must be an object";
            string? reason = UnsupportedProbe(candidate);
            if (reason is not null) return reason;
        }
        return null;
    }

    private static string? UnsupportedProbe(JsonObject probe)
    {
        if (Text(probe["phase"]) != "after") return "only an explicit after-phase probe is supported";
        if (probe["name"] is not null && Text(probe["name"]) is null) return "probe name must be a string";
        if (Text(probe["kind"]) == "output.since")
        {
            if (!Only(probe, "kind", "phase", "name", "checkpoint", "required", "forbidden", "regex", "max_matches")) return "unsupported output.since field";
            if (probe["checkpoint"] is not null && Text(probe["checkpoint"]) is null) return "checkpoint must be a string or null";
            if (probe["regex"] is not JsonValue regex || !regex.TryGetValue<bool>(out var useRegex) || useRegex) return "output.since requires regex=false until Python regex compatibility is established";
            if (!Patterns(probe["required"]) || !Patterns(probe["forbidden"])) return "output.since patterns must be bounded lists of strings";
            if (probe.ContainsKey("max_matches") && (probe["max_matches"] is not JsonValue max || !max.TryGetValue<int>(out var count) || count is < 0 or > 256)) return "max_matches must be an integer between 0 and 256";
            return null;
        }
        if (Text(probe["kind"]) == "output.field")
        {
            if (!Only(probe, "kind", "phase", "name", "checkpoint", "source", "reason", "field", "expected", "expect")) return "unsupported output.field field";
            foreach (var key in new[] { "checkpoint", "source", "reason", "field" })
                if (probe[key] is not null && (Text(probe[key]) is not string text || text.Length > 4096)) return $"{key} must be a bounded string or null";
            return null;
        }
        if (Text(probe["kind"]) == "debug.evaluate")
        {
            if (!Only(probe, "kind", "phase", "name", "expression", "expected")) return "unsupported debug.evaluate field";
            if (Text(probe["expression"]) is not string evaluationExpression || evaluationExpression.Length > 4096) return "expression must be a bounded string";
            return null;
        }
        if (Text(probe["kind"]) != "file.json") return "only file.json, output.since, output.field, and debug.evaluate probes are supported";
        if (!OperatingSystem.IsWindows()) return "native file observation is currently Windows-only";
        if (!Only(probe, "kind", "phase", "name", "path", "jsonpath", "expected")) return "unsupported probe field";
        string? path = Text(probe["path"]);
        if (path is null || !LocalFilePath(path)) return "file path must be on a Windows drive reported as fixed, without device names or alternate streams";
        if (string.Equals(Path.GetFileName(path), "diagnostic-latest.json", StringComparison.Ordinal)) return "diagnostic-latest.json alias is not supported";
        string? expression = Text(probe["jsonpath"]);
        if (expression is null || !JsonPath.IsMatch(expression)) return "only simple dotted identifier JSONPath expressions are supported";
        if (probe.ContainsKey("expected") && probe["expected"] is not null && Text(probe["expected"]) is null) return "only string or null expectations are supported";
        if (Text(probe["expected"]) is string expected && expected.StartsWith("contains:", StringComparison.Ordinal)) return "contains expectations are not supported";
        return null;
    }

    private static bool LocalFilePath(string path)
    {
        if (path.Length < 4 || !char.IsAsciiLetter(path[0]) || path[1] != ':' || (path[2] != '\\' && path[2] != '/')) return false;
        foreach (string part in path[3..].Split(['\\', '/']))
        {
            if (part.Length == 0 || part is "." or ".." || part.EndsWith(' ') || part.EndsWith('.') || part.IndexOfAny(Path.GetInvalidFileNameChars()) >= 0) return false;
            string stem = part.Split('.')[0].TrimEnd(' ');
            if (Regex.IsMatch(stem, @"\A(?:CON|PRN|AUX|NUL|CONIN\$|CONOUT\$|COM[1-9¹²³]|LPT[1-9¹²³])\z", RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)) return false;
        }
        try { return new DriveInfo(path[..3]).DriveType == DriveType.Fixed; }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or ArgumentException or System.Security.SecurityException)
        { return false; }
    }

    private static JsonArray? Probes(JsonNode? plan) =>
        plan is JsonObject root && root["cases"] is JsonArray { Count: 1 } cases && cases[0] is JsonObject item
        && item["transitions"] is JsonArray { Count: 1 } transitions && transitions[0] is JsonObject transition
        && transition["probes"] is JsonArray probes ? probes : null;

    internal static bool UsesOutput(JsonNode? plan) => Probes(plan)?.Any(node =>
        node is JsonObject probe && Text(probe["kind"]) is "output.since" or "output.field") == true;
    internal static bool UsesEvaluation(JsonNode? plan) => Probes(plan) is { Count: 1 } probes &&
        probes[0] is JsonObject probe && Text(probe["kind"]) == "debug.evaluate";

    private static bool Patterns(JsonNode? node) => node is null ||
        (node is JsonArray { Count: <= 256 } items && items.All(item => Text(item) is string text && text.Length <= 4096));

    internal static Task<JsonObject> RunAsync(JsonNode? plan, CancellationToken cancellationToken) =>
        RunAsync(plan, cancellationToken, null);


    internal static async Task<JsonObject> RunAsync(JsonNode? plan, CancellationToken cancellationToken,
        Func<JsonObject, CancellationToken, Task<JsonObject>>? probeObserver, LegacyRuntimeSmokeEffect? effect = null)
    {
        cancellationToken.ThrowIfCancellationRequested();
        var started = Stopwatch.StartNew();
        JsonObject admission = Validate(plan);
        if (Text(admission["status"]) != "PASS")
        {
            admission["cleanup"] = Cleanup();
            return admission;
        }
        var root = (JsonObject)plan!;
        var item = (JsonObject)((JsonArray)root["cases"]!)[0]!;
        var transition = (JsonObject)((JsonArray)item["transitions"]!)[0]!;
        var probes = (JsonArray)transition["probes"]!;
        var idleMs = transition["settle"] is JsonObject settle && settle.ContainsKey("idle_ms") ? 0 : 250;
        bool budgeted = root.ContainsKey("budgets");
        using var budgetCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        if (budgeted)
        {
            var remaining = TimeSpan.FromSeconds(5) - started.Elapsed;
            if (remaining <= TimeSpan.Zero) budgetCancellation.Cancel();
            else budgetCancellation.CancelAfter(remaining);
        }
        var executionToken = budgetCancellation.Token;
        var actions = new JsonArray();
        var metricClock = Stopwatch.StartNew();
        var observed = new JsonArray();
        JsonObject? selectedFailure = null;
        int failureRank = 0;
        bool settled = false;
        bool timedOut = false;
        try
        {
            executionToken.ThrowIfCancellationRequested();
            if (transition.ContainsKey("action"))
                actions.Add(new JsonObject { ["status"] = "PASS", ["route"] = "noop", ["duration_ms"] = 0 });
            if (idleMs != 0) await Task.Delay(idleMs, executionToken).ConfigureAwait(false);
            settled = true;
            metricClock.Stop();
            foreach (var node in probes)
            {
                var probe = (JsonObject)node!;
                executionToken.ThrowIfCancellationRequested();
                var observation = Text(probe["kind"]) is "output.since" or "output.field" or "debug.evaluate"
                    ? probeObserver is null
                        ? new JsonObject { ["name"] = Text(probe["name"]) ?? Text(probe["kind"]), ["kind"] = Text(probe["kind"]), ["status"] = "BLOCKED", ["reason"] = "probe observation is unavailable" }
                        : await probeObserver(probe, executionToken).ConfigureAwait(false)
                    : await ObserveAsync(probe, executionToken).ConfigureAwait(false);
                cancellationToken.ThrowIfCancellationRequested();
                observed.Add(observation);
                int rank = Text(observation["status"]) switch { "FAIL" => 3, "BLOCKED" => 2, "IMPASSE" => 1, "PASS" => 0, _ => 2 };
                if (rank > failureRank) { failureRank = rank; selectedFailure = observation; }
                if (budgeted && (executionToken.IsCancellationRequested || started.Elapsed >= TimeSpan.FromSeconds(5))) { timedOut = true; break; }
            }
        }
        catch (OperationCanceledException) when (budgeted && !cancellationToken.IsCancellationRequested && executionToken.IsCancellationRequested)
        {
            timedOut = true;
        }
        metricClock.Stop();
        cancellationToken.ThrowIfCancellationRequested();
        if (timedOut) observed.Clear();
        string status = timedOut ? "IMPASSE" : failureRank switch { 3 => "FAIL", 2 => "BLOCKED", 1 => "IMPASSE", _ => "PASS" };
        string reason = timedOut ? TimeoutReason(effect) : status == "PASS" ? "transition passed" : Text(selectedFailure?["reason"]) ?? "transition failed";
        string caseReason = status == "PASS" ? "case passed" : reason;
        string resultReason = status == "PASS" ? "runtime smoke v2 scenario passed" : reason;
        var after = new JsonObject();
        var diff = new JsonObject();
        var refs = new JsonArray();
        for (int i = 0; i < observed.Count; i++)
        {
            var probe = (JsonObject)probes[i]!;
            var observation = (JsonObject)observed[i]!;
            string kind = Text(probe["kind"])!;
            string name = Text(probe["name"]) is string n && n.Length > 0 ? n : kind;
            string key = name == kind && string.IsNullOrEmpty(Text(probe["name"])) ? kind : kind + "." + name;
            observation["path"] = key;
            JsonNode? value = observation["value"];
            after[key] = value?.DeepClone();
            if (value is not null) diff[key] = new JsonObject { ["from"] = null, ["to"] = value.DeepClone() };
            if (Text(observation["evidence_ref"]) is string evidence)
                refs.Add(new JsonObject { ["case_id"] = item["id"]?.DeepClone(), ["phase"] = "after", ["probe"] = name, ["evidence_ref"] = evidence });
        }
        var metrics = Metrics((long)Math.Round(metricClock.Elapsed.TotalMilliseconds));
        var transitionResult = new JsonObject
        {
            ["id"] = transition["id"]?.DeepClone(),
            ["status"] = status,
            ["reason"] = reason,
            ["actions"] = actions.DeepClone(),
            ["metrics"] = metrics,
            ["before"] = new JsonObject(),
            ["after"] = after,
            ["diff"] = diff,
            ["probes"] = new JsonObject { ["before"] = new JsonArray(), ["after"] = observed }
        };
        if (settled) transitionResult["settle"] = new JsonObject { ["status"] = "PASS", ["idle_ms"] = idleMs };
        var caseCleanup = Cleanup();
        bool unconfirmedEffect = effect?.EvaluationDispatched == true && (timedOut || (observed.Count == 1 && observed[0]?["outcome_unknown"]?.GetValue<bool>() == true));
        if (unconfirmedEffect) MarkUnconfirmed(caseCleanup);
        caseCleanup["case_id"] = Text(item["id"]) ?? "";
        var caseResult = new JsonObject
        {
            ["id"] = item["id"]?.DeepClone(),
            ["status"] = status,
            ["reason"] = caseReason,
            ["actions"] = actions.DeepClone(),
            ["transitions"] = new JsonArray(transitionResult),
            ["before"] = new JsonObject(),
            ["after"] = after.DeepClone(),
            ["diff"] = diff.DeepClone(),
            ["metrics"] = metrics.DeepClone(),
            ["failed_assertions"] = new JsonArray(),
            ["cleanup"] = caseCleanup
        };
        var cleanup = Cleanup();
        if (unconfirmedEffect) MarkUnconfirmed(cleanup);
        cleanup["failed_case_cleanups"] = new JsonArray();
        long elapsed = started.ElapsedMilliseconds;
        var result = new JsonObject
        {
            ["status"] = status,
            ["reason"] = resultReason,
            ["elapsed_ms"] = elapsed,
            ["action_count"] = actions.Count,
            ["completed_steps"] = new JsonArray(),
            ["failed_assertions"] = new JsonArray(),
            ["cleanup"] = cleanup,
            ["evidence_refs"] = refs,
            ["generated_case_count"] = 0,
            ["cases"] = new JsonArray(caseResult),
            ["baseline"] = null,
            ["metrics_thresholds"] = null
        };
        foreach (string field in new[] { "accepted_schema_values", "accepted_top_level_keys_v2", "accepted_action_kinds", "accepted_probe_kinds", "accepted_probe_phases", "native_execution_subset" })
            if (admission.ContainsKey(field)) result[field] = admission[field]?.DeepClone();
        result["compact"] = new JsonObject
        {
            ["status"] = status,
            ["reason"] = resultReason,
            ["elapsed_ms"] = elapsed,
            ["action_count"] = actions.Count,
            ["generated_case_count"] = 0,
            ["case_count"] = 1,
            ["cleanup"] = Compact(cleanup),
            ["evidence_refs"] = Compact(refs)
        };
        cancellationToken.ThrowIfCancellationRequested();
        if (budgeted && !timedOut && (executionToken.IsCancellationRequested || started.Elapsed >= TimeSpan.FromSeconds(5)))
        {
            string timeoutReason = TimeoutReason(effect);
            if (effect?.EvaluationDispatched == true)
            {
                MarkUnconfirmed(caseCleanup);
                MarkUnconfirmed(cleanup);
                ((JsonObject)result["compact"]!)["cleanup"] = Compact(cleanup);
            }
            transitionResult["status"] = "IMPASSE";
            transitionResult["reason"] = timeoutReason;
            transitionResult["after"] = new JsonObject();
            transitionResult["diff"] = new JsonObject();
            transitionResult["probes"] = new JsonObject { ["before"] = new JsonArray(), ["after"] = new JsonArray() };
            caseResult["status"] = "IMPASSE";
            caseResult["reason"] = timeoutReason;
            caseResult["after"] = new JsonObject();
            caseResult["diff"] = new JsonObject();
            refs.Clear();
            result["status"] = "IMPASSE";
            result["reason"] = timeoutReason;
            result["elapsed_ms"] = started.ElapsedMilliseconds;
            var compact = (JsonObject)result["compact"]!;
            compact["status"] = "IMPASSE";
            compact["reason"] = timeoutReason;
            compact["elapsed_ms"] = started.ElapsedMilliseconds;
            compact["evidence_refs"] = new JsonArray();
        }
        return result;
    }

    private static async Task<JsonObject> ObserveAsync(JsonObject probe, CancellationToken cancellationToken)
    {
        string path = Text(probe["path"])!;
        string expression = Text(probe["jsonpath"])!;
        string name = Text(probe["name"]) is string n && n.Length > 0 ? n : "file.json";
        var result = new JsonObject { ["name"] = name, ["kind"] = "file.json", ["status"] = "FAIL", ["value"] = null, ["resolved_path"] = path };
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            string current = Path.GetPathRoot(path)!;
            string[] parts = path[current.Length..].Split(['\\', '/']);
            for (int i = 0; i < parts.Length; i++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                current = Path.Combine(current, parts[i]);
                var attributes = File.GetAttributes(current);
                if ((attributes & (FileAttributes.Device | FileAttributes.ReparsePoint)) != 0 ||
                    (i == parts.Length - 1 && (attributes & FileAttributes.Directory) != 0) ||
                    (i != parts.Length - 1 && (attributes & FileAttributes.Directory) == 0))
                {
                    result["reason"] = "path is not a regular file or contains a reparse point";
                    result["missing_side"] = "file";
                    return result;
                }
            }
            cancellationToken.ThrowIfCancellationRequested();
            await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete, 4096, FileOptions.Asynchronous | FileOptions.SequentialScan);
            if (!stream.CanSeek || stream.Length > MaxFileBytes)
            {
                result["reason"] = $"file is not a regular file or exceeds {MaxFileBytes} bytes";
                return result;
            }
            using var buffer = new MemoryStream();
            var bytes = new byte[8192];
            int count;
            while ((count = await stream.ReadAsync(bytes.AsMemory(), cancellationToken).ConfigureAwait(false)) != 0)
            {
                if (buffer.Length + count > MaxFileBytes)
                {
                    result["reason"] = $"file exceeds {MaxFileBytes} bytes";
                    return result;
                }
                buffer.Write(bytes, 0, count);
            }
            cancellationToken.ThrowIfCancellationRequested();
            string json = new UTF8Encoding(false, true).GetString(buffer.GetBuffer(), 0, checked((int)buffer.Length));
            JsonNode? value = JsonNode.Parse(json);
            foreach (string part in expression[2..].Split('.')) value = value is JsonObject obj && obj.TryGetPropertyValue(part, out var child) ? child : null;
            result["value"] = value?.DeepClone();
            result["jsonpath"] = expression;
            result["evidence_ref"] = $"file:{Path.GetFileName(path)}:{expression}";
            result["status"] = "PASS";
            if (probe.ContainsKey("expected"))
            {
                result["expected"] = probe["expected"]?.DeepClone();
                if (!Equal(value, probe["expected"])) { result["status"] = "FAIL"; result["reason"] = "expected value did not match"; }
            }
        }
        catch (OperationCanceledException) { throw; }
        catch (FileNotFoundException) { result["reason"] = "json file missing"; result["missing_side"] = "file"; }
        catch (DirectoryNotFoundException) { result["reason"] = "json file missing"; result["missing_side"] = "file"; }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or JsonException or DecoderFallbackException or ArgumentException or NotSupportedException)
        {
            result["reason"] = ex is JsonException or DecoderFallbackException ? "jsonpath evaluation failed" : "json file read failed";
            result["jsonpath"] = expression;
            result["error"] = ex.Message;
        }
        return result;
    }
    private static bool Equal(JsonNode? left, JsonNode? right)
    {
        if (left is null || right is null) return left is null && right is null;
        return Text(left) is string actual && Text(right) is string expected && actual == expected;
    }

    private static JsonNode? Compact(JsonNode? value)
    {
        if (value is JsonObject obj)
        {
            var result = new JsonObject();
            var omitted = new JsonArray();
            foreach (var item in obj)
            {
                if (Text(item.Value) is string text && text.EnumerateRunes().Count() is var length && length > 240)
                {
                    result[item.Key + "_length"] = length;
                    omitted.Add(item.Key);
                }
                else result[item.Key] = Compact(item.Value);
            }
            if (omitted.Count > 0) result["omitted_fields"] = omitted;
            return result;
        }
        if (value is JsonArray array)
        {
            var result = new JsonArray();
            foreach (var item in array.Take(8)) result.Add(Compact(item));
            if (array.Count > 8) result.Add(new JsonObject { ["omitted_count"] = array.Count - 8 });
            return result;
        }
        if (Text(value) is string scalar && scalar.EnumerateRunes().Count() is var count && count > 240)
            return new JsonObject { ["text_length"] = count };
        return value?.DeepClone();
    }

    private static string TimeoutReason(LegacyRuntimeSmokeEffect? effect) => effect?.EvaluationDispatched == true
        ? "elapsed time budget exhausted; evaluation may have executed. Do not retry automatically."
        : "elapsed time budget exhausted";

    private static void MarkUnconfirmed(JsonObject cleanup)
    {
        cleanup["status"] = "FAIL";
        cleanup["failures"] = new JsonArray("evaluation outcome is unconfirmed");
    }


    private static JsonObject Metrics(long latency) => new()
    {
        ["action_latency_ms"] = latency,
        ["working_set_delta_mb"] = null,
        ["private_bytes_delta_mb"] = null,
        ["partial"] = true,
        ["field_status"] = new JsonObject
        {
            ["action_latency_ms"] = new JsonObject { ["status"] = "PASS" },
            ["working_set_delta_mb"] = new JsonObject { ["status"] = "BLOCKED", ["reason"] = "target process metrics not observed" },
            ["private_bytes_delta_mb"] = new JsonObject { ["status"] = "BLOCKED", ["reason"] = "target process metrics not observed" }
        }
    };

    private static JsonObject Cleanup() => new()
    {
        ["status"] = "PASS",
        ["attempted"] = new JsonArray(),
        ["failures"] = new JsonArray(),
        ["tracepoints_removed"] = 0,
        ["isolated_profiles_torn_down"] = 0,
        ["debug_stop"] = null,
        ["process_registry_after"] = null
    };

    private static bool Only(JsonObject obj, params string[] accepted) => obj.All(pair => accepted.Contains(pair.Key, StringComparer.Ordinal));
    private static bool IntegerEquals(JsonNode? node, int expected) => node is JsonValue value && value.TryGetValue<int>(out int actual) && actual == expected;
    private static string? Text(JsonNode? node) => node is JsonValue value && value.TryGetValue<string>(out var text) ? text : null;
}
