using System.Diagnostics;
using System.Text;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed class LegacyRuntimeSmokeRuns
{
    private const int MaxCompletedRuns = 8;
    private const int MaxResultBytes = 256 * 1024;
    private const int StopTimeoutMs = 5000;
    private readonly Func<JsonObject, CancellationToken, Task<JsonObject>> _runner;
    private readonly object _gate = new();
    private readonly Dictionary<string, Run> _runs = new(StringComparer.Ordinal);
    private readonly Queue<string> _completed = new();
    private Run? _active;
    private bool _closed;
    private Run? _cleanupUnconfirmed;

    internal LegacyRuntimeSmokeRuns() : this(LegacyRuntimeSmokeFileRunner.RunAsync) { }
    internal LegacyRuntimeSmokeRuns(Func<JsonObject, CancellationToken, Task<JsonObject>> runner) => _runner = runner;

    private sealed class Run
    {
        internal readonly string Id = Guid.NewGuid().ToString("N");
        internal readonly long Started = Stopwatch.GetTimestamp();
        internal readonly string? Owner;
        internal readonly string PlanName;
        internal readonly CancellationTokenSource Cancellation = new();
        internal readonly TaskCompletionSource Completion = new(TaskCreationOptions.RunContinuationsAsynchronously);
        internal readonly List<JsonObject> Events = new();
        internal string Status = "RUNNING";
        internal JsonObject? Result;
        internal int CancelUsers;
        internal bool CancellationDisposed;

        internal Run(string? owner, string planName)
        {
            Owner = owner;
            PlanName = planName;
        }
    }

    public JsonObject Start(JsonObject plan, string? owner)
    {
        lock (_gate)
        {
            if (_closed)
                return new JsonObject { ["status"] = "BLOCKED", ["reason"] = "runtime smoke registry is closed", ["run_created"] = false, ["final"] = false };
            if (_cleanupUnconfirmed is not null)
                return new JsonObject { ["status"] = "BLOCKED", ["reason"] = "runtime smoke cleanup is unconfirmed", ["run_created"] = false, ["final"] = false };
            if (_active is not null)
            {
                var blocked = new JsonObject { ["status"] = "BLOCKED", ["reason"] = "runtime smoke run already active", ["run_created"] = false, ["final"] = false };
                if (SameOwner(_active, owner))
                {
                    blocked["active_run_id"] = _active.Id;
                    blocked["active_status"] = _active.Status;
                }
                return blocked;
            }

            var copy = (JsonObject)plan.DeepClone();
            string planName = Text(copy["name"]) ?? Text(copy["id"]) ?? "runtime-smoke";
            if (planName.Length > 512) planName = planName[..512] + "…";
            var run = new Run(owner, planName);
            AppendEvent(run, "started", "runtime smoke run started");
            _runs.Add(run.Id, run);
            _active = run;
            _ = Task.Run(() => ExecuteAsync(run, copy));
            return RunningPayload(run);
        }
    }

    public JsonObject GetResult(string runId, string? owner)
    {
        lock (_gate)
            return Find(runId, owner) is Run run ? Payload(run) : NotFound(runId);
    }

    public JsonObject TailEvents(string runId, string? owner, int afterCursor, int limit)
    {
        lock (_gate)
        {
            Run? run = Find(runId, owner);
            return run is null ? NotFound(runId) : Tail(run, afterCursor, limit);
        }
    }

    public JsonObject EvidenceBundle(string runId, string? owner, int afterCursor, int limit)
    {
        lock (_gate)
        {
            Run? run = Find(runId, owner);
            return run is null ? MissingBundle(runId, afterCursor, limit) : Bundle(run, afterCursor, limit);
        }
    }

    public JsonObject MarkEventCursor(string runId, string? owner)
    {
        lock (_gate)
        {
            Run? run = Find(runId, owner);
            if (run is null) return MissingDelta(runId, 0, 0);
            return new JsonObject
            {
                ["status"] = "PASS",
                ["reason"] = "runtime smoke event cursor marked",
                ["run_id"] = run.Id,
                ["cursor"] = Cursor(run, run.Events.Count),
                ["final"] = run.Result is not null,
                ["next_actions"] = new JsonArray("runtime_smoke_get_event_delta", "runtime_smoke_evidence_bundle", "runtime_smoke_tail_events")
            };
        }
    }

    public JsonObject GetEventDelta(string runId, string? owner, int afterCursor, int limit)
    {
        lock (_gate)
        {
            Run? run = Find(runId, owner);
            if (run is null) return MissingDelta(runId, afterCursor, limit);
            afterCursor = Math.Max(0, afterCursor);
            limit = Math.Clamp(limit, 0, 128);
            JsonObject tail = Tail(run, afterCursor, limit);
            var events = (JsonArray)tail["events"]!;
            int continuation = events.Count > 0 ? events[events.Count - 1]!["cursor"]!.GetValue<int>() : run.Events.Count;
            return new JsonObject
            {
                ["status"] = "PASS",
                ["reason"] = "runtime smoke event delta read",
                ["run_id"] = run.Id,
                ["events"] = events.DeepClone(),
                ["event_cursor"] = EventCursor(afterCursor, run.Events.Count, limit),
                ["cursor"] = Cursor(run, continuation),
                ["final"] = run.Result is not null,
                ["next_actions"] = new JsonArray("runtime_smoke_get_event_delta", "runtime_smoke_mark_event_cursor", "runtime_smoke_evidence_bundle", "runtime_smoke_tail_events")
            };
        }
    }

    public JsonObject CleanupContract(string? owner)
    {
        lock (_gate)
        {
            bool active = _active is not null;
            bool unconfirmed = _cleanupUnconfirmed is not null;
            string status = active ? "BLOCKED" : unconfirmed ? "FAIL" : "PASS";
            string reason = active ? "runtime smoke run is still active" :
                unconfirmed ? "runtime smoke cleanup is unconfirmed" : "runtime smoke cleanup contract already clean";
            var response = new JsonObject
            {
                ["status"] = status,
                ["reason"] = reason,
                ["contaminated"] = unconfirmed,
                ["scope"] = "current_host_process",
                ["cleanup_contract"] = new JsonObject
                {
                    ["status"] = status,
                    ["required_before"] = unconfirmed,
                    ["attempted"] = new JsonArray(),
                    ["failures"] = new JsonArray()
                }
            };
            if (_active is Run run && SameOwner(run, owner)) response["run_id"] = run.Id;
            else if (_cleanupUnconfirmed is Run failed && SameOwner(failed, owner)) response["run_id"] = failed.Id;
            if (unconfirmed) response["next_step"] = "Inspect the failed run and close the native host after owned work drains; a new host does not prove previous cleanup.";
            return response;
        }
    }

    public async Task<JsonObject> WaitForResultAsync(string runId, string? owner, int timeoutMs, int pollIntervalMs, int afterCursor, int eventLimit, CancellationToken cancellationToken)
    {
        Run? run;
        lock (_gate)
            run = Find(runId, owner);
        if (run is null)
            return MissingBundle(runId, afterCursor, eventLimit);

        bool timedOut = false;
        try
        {
            await run.Completion.Task.WaitAsync(TimeSpan.FromMilliseconds(Math.Max(0, timeoutMs)), cancellationToken).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            timedOut = true;
        }

        lock (_gate)
        {
            JsonObject bundle = Bundle(run, afterCursor, eventLimit);
            if (timedOut && run.Result is null)
            {
                bundle["status"] = "BLOCKED";
                bundle["reason"] = "runtime smoke wait timed out";
                bundle["next_step"] = "Poll again with runtime_smoke_wait_for_result or increase timeout_ms.";
            }
            return bundle;
        }
    }

    public Task<JsonObject> StopAsync(string runId, string? owner, CancellationToken cancellationToken)
    {
        Run? run;
        lock (_gate) run = Find(runId, owner);
        return run is null ? Task.FromResult(NotFound(runId)) : StopRunAsync(run, cancellationToken);
    }

    public async Task<JsonObject?> StopActiveAsync(string? owner, CancellationToken cancellationToken)
    {
        Run? run;
        lock (_gate) run = _active is Run active && SameOwner(active, owner) ? active : null;
        return run is null ? null : await StopRunAsync(run, cancellationToken).ConfigureAwait(false);
    }

    private async Task<JsonObject> StopRunAsync(Run run, CancellationToken cancellationToken)
    {
        bool cancel = false;
        lock (_gate)
        {
            if (run.Result is not null) return Payload(run);
            if (run.Status != "STOPPING")
            {
                run.Status = "STOPPING";
                AppendEvent(run, "stop_requested", "runtime smoke stop requested");
                run.CancelUsers++;
                cancel = true;
            }
        }
        if (cancel) Cancel(run);
        try
        {
            await run.Completion.Task.WaitAsync(TimeSpan.FromMilliseconds(StopTimeoutMs), cancellationToken).ConfigureAwait(false);
        }
        catch (TimeoutException)
        {
            // The run remains active until the file operation actually drains.
        }
        lock (_gate) return Payload(run);
    }

    public async Task CloseAsync(CancellationToken cancellationToken)
    {
        Run? run;
        bool cancel = false;
        lock (_gate)
        {
            _closed = true;
            run = _active;
            if (run is not null && run.Status != "STOPPING")
            {
                run.Status = "STOPPING";
                AppendEvent(run, "stop_requested", "runtime smoke registry closing");
                run.CancelUsers++;
                cancel = true;
            }
        }
        if (run is null) return;
        if (cancel) Cancel(run);
        await run.Completion.Task.WaitAsync(TimeSpan.FromMilliseconds(StopTimeoutMs), cancellationToken).ConfigureAwait(false);
    }

    private async Task ExecuteAsync(Run run, JsonObject plan)
    {
        JsonObject result;
        string eventKind;
        try
        {
            result = await _runner(plan, run.Cancellation.Token).ConfigureAwait(false);
            eventKind = "completed";
        }
        catch (OperationCanceledException) when (run.Cancellation.IsCancellationRequested)
        {
            var cleanup = new JsonObject
            {
                ["status"] = "PASS",
                ["attempted"] = new JsonArray(),
                ["failures"] = new JsonArray(),
                ["failed_case_cleanups"] = new JsonArray(),
                ["tracepoints_removed"] = 0,
                ["isolated_profiles_torn_down"] = 0,
                ["debug_stop"] = null,
                ["process_registry_after"] = null,
            };
            var elapsed = (long)Stopwatch.GetElapsedTime(run.Started).TotalMilliseconds;
            result = new JsonObject
            {
                ["status"] = "IMPASSE",
                ["reason"] = "runtime smoke run stopped",
                ["elapsed_ms"] = elapsed,
                ["action_count"] = 0,
                ["completed_steps"] = new JsonArray(),
                ["failed_assertions"] = new JsonArray(),
                ["cleanup"] = cleanup,
                ["evidence_refs"] = new JsonArray(),
                ["cases"] = new JsonArray(),
                ["stopped"] = true,
                ["compact"] = new JsonObject
                {
                    ["status"] = "IMPASSE",
                    ["reason"] = "runtime smoke run stopped",
                    ["elapsed_ms"] = elapsed,
                    ["action_count"] = 0,
                    ["case_count"] = 0,
                    ["cleanup"] = cleanup.DeepClone(),
                    ["evidence_refs"] = new JsonArray()
                },
            };
            eventKind = "stopped";
        }
        catch (Exception)
        {
            result = new JsonObject { ["status"] = "FAIL", ["reason"] = "runtime smoke execution failed", ["cleanup"] = null, ["evidence_refs"] = new JsonArray() };
            eventKind = "failed";
        }

        lock (_gate)
        {
            string? cleanupStatus = Text((result["cleanup"] as JsonObject)?["status"]);
            if (cleanupStatus != "PASS") _cleanupUnconfirmed = run;
            run.Status = eventKind == "stopped" ? "STOPPED" : Text(result["status"]) ?? "FAIL";
            result["run_id"] = run.Id;
            result["lifecycle_status"] = run.Status;
            result["next_cursor"] = run.Events.Count + 1;
            result["oldest_cursor"] = 1;
            result["dropped_count"] = 0;
            result["final"] = true;
            if (Encoding.UTF8.GetByteCount(result.ToJsonString()) > MaxResultBytes)
            {
                run.Status = "FAIL";
                result = new JsonObject
                {
                    ["status"] = "FAIL",
                    ["reason"] = "runtime smoke result exceeds 256 KiB retention limit",
                    ["run_id"] = run.Id,
                    ["lifecycle_status"] = "FAIL",
                    ["next_cursor"] = run.Events.Count + 1,
                    ["oldest_cursor"] = 1,
                    ["dropped_count"] = 0,
                    ["final"] = true,
                    ["cleanup"] = cleanupStatus is { Length: <= 512 } ? new JsonObject { ["status"] = cleanupStatus } : null,
                    ["evidence_refs"] = new JsonArray()
                };
                eventKind = "failed";
            }
            AppendEvent(run, eventKind, Text(result["reason"]) is string reason && reason.Length > 512 ? reason[..512] + "…" : Text(result["reason"]));
            run.Result = result;
            _active = null;
            _completed.Enqueue(run.Id);
            while (_completed.Count > MaxCompletedRuns) _runs.Remove(_completed.Dequeue());
            DisposeCancellationIfSafe(run);
            run.Completion.TrySetResult();
        }
    }

    private void Cancel(Run run)
    {
        try { run.Cancellation.Cancel(); }
        finally
        {
            lock (_gate)
            {
                run.CancelUsers--;
                DisposeCancellationIfSafe(run);
            }
        }
    }

    private static void DisposeCancellationIfSafe(Run run)
    {
        if (run.Result is not null && run.CancelUsers == 0 && !run.CancellationDisposed)
        {
            run.CancellationDisposed = true;
            run.Cancellation.Dispose();
        }
    }

    private Run? Find(string runId, string? owner) =>
        _runs.TryGetValue(runId, out Run? run) && SameOwner(run, owner) ? run : null;

    private static bool SameOwner(Run run, string? owner) => string.Equals(run.Owner, owner, StringComparison.Ordinal);
    private static string? Text(JsonNode? node) => node is JsonValue value && value.TryGetValue<string>(out string? text) && !string.IsNullOrEmpty(text) ? text : null;

    private static void AppendEvent(Run run, string kind, string? reason)
    {
        run.Events.Add(new JsonObject
        {
            ["cursor"] = run.Events.Count + 1,
            ["kind"] = kind,
            ["status"] = run.Status,
            ["run_id"] = run.Id,
            ["timestamp"] = DateTimeOffset.UtcNow.ToUnixTimeMilliseconds(),
            ["reason"] = reason
        });
    }

    private static JsonObject Payload(Run run) => run.Result is null ? RunningPayload(run) : (JsonObject)run.Result.DeepClone();

    private static JsonObject RunningPayload(Run run) => new()
    {
        ["status"] = run.Status,
        ["reason"] = "runtime smoke run is still running",
        ["run_id"] = run.Id,
        ["plan_name"] = run.PlanName,
        ["next_cursor"] = run.Events.Count,
        ["oldest_cursor"] = 1,
        ["dropped_count"] = 0,
        ["final"] = false
    };

    private static JsonObject NotFound(string runId) => new()
    {
        ["status"] = "FAIL",
        ["reason"] = "runtime smoke run not found",
        ["run_id"] = runId
    };

    private static JsonObject Tail(Run run, int afterCursor, int limit)
    {
        afterCursor = Math.Max(0, afterCursor);
        limit = Math.Clamp(limit, 0, 128);
        var events = new JsonArray();
        foreach (JsonObject item in run.Events)
        {
            if (item["cursor"]!.GetValue<int>() > afterCursor && events.Count < limit)
                events.Add(item.DeepClone());
        }
        return new JsonObject
        {
            ["status"] = run.Status,
            ["run_id"] = run.Id,
            ["events"] = events,
            ["next_cursor"] = run.Events.Count,
            ["oldest_cursor"] = 1,
            ["dropped_count"] = 0,
            ["stale_cursor"] = false,
            ["final"] = run.Result is not null
        };
    }

    private static JsonObject Cursor(Run run, int afterCursor) => new()
    {
        ["run_id"] = run.Id,
        ["after_cursor"] = afterCursor,
        ["next_cursor"] = afterCursor,
        ["oldest_cursor"] = 1,
        ["dropped_count"] = 0,
        ["stale_cursor"] = false
    };

    private static JsonObject EventCursor(int afterCursor, int highWatermark, int limit) => new()
    {
        ["after_cursor"] = afterCursor,
        ["next_cursor"] = highWatermark,
        ["oldest_cursor"] = 1,
        ["dropped_count"] = 0,
        ["stale_cursor"] = false,
        ["limit"] = limit
    };

    private static JsonObject MissingDelta(string runId, int afterCursor, int limit)
    {
        afterCursor = Math.Max(0, afterCursor);
        limit = Math.Clamp(limit, 0, 128);
        return new JsonObject
        {
            ["status"] = "FAIL",
            ["reason"] = "runtime smoke run not found",
            ["run_id"] = runId,
            ["events"] = new JsonArray(),
            ["event_cursor"] = new JsonObject { ["after_cursor"] = afterCursor, ["next_cursor"] = afterCursor, ["oldest_cursor"] = null, ["dropped_count"] = 0, ["stale_cursor"] = false, ["limit"] = limit },
            ["final"] = true,
            ["next_actions"] = new JsonArray("runtime_smoke_run_plan")
        };
    }

    private static JsonObject Bundle(Run run, int afterCursor, int eventLimit)
    {
        afterCursor = Math.Max(0, afterCursor);
        eventLimit = Math.Clamp(eventLimit, 0, 128);
        var events = new JsonArray();
        foreach (JsonObject item in run.Events)
        {
            if (item["cursor"]!.GetValue<int>() > afterCursor && events.Count < eventLimit)
                events.Add(item.DeepClone());
        }
        JsonObject payload = Payload(run);
        bool final = run.Result is not null;
        return new JsonObject
        {
            ["status"] = payload["status"]?.DeepClone(),
            ["reason"] = payload["reason"]?.DeepClone(),
            ["run_id"] = run.Id,
            ["plan_name"] = run.PlanName,
            ["lifecycle_status"] = run.Status,
            ["final"] = final,
            ["events"] = events,
            ["event_cursor"] = new JsonObject { ["after_cursor"] = afterCursor, ["next_cursor"] = run.Events.Count, ["oldest_cursor"] = 1, ["dropped_count"] = 0, ["stale_cursor"] = false, ["limit"] = eventLimit },
            ["result"] = final ? BoundedResult(payload) : null,
            ["evidence_refs"] = (payload["compact"]?["evidence_refs"] ?? payload["evidence_refs"])?.DeepClone() ?? new JsonArray(),
            ["cleanup"] = (payload["compact"]?["cleanup"] ?? payload["cleanup"])?.DeepClone(),
            ["next_actions"] = final ? new JsonArray("runtime_smoke_get_result", "runtime_smoke_run_plan") : new JsonArray("runtime_smoke_wait_for_result", "runtime_smoke_get_result", "runtime_smoke_stop")
        };
    }

    private static JsonObject BoundedResult(JsonObject payload)
    {
        var result = new JsonObject();
        foreach (var key in new[] { "status", "reason", "elapsed_ms", "action_count", "failed_assertions" })
            if (payload.ContainsKey(key)) result[key] = payload[key]?.DeepClone();
        var compact = payload["compact"] as JsonObject;
        result["cleanup"] = (compact?["cleanup"] ?? payload["cleanup"])?.DeepClone();
        result["evidence_refs"] = (compact?["evidence_refs"] ?? payload["evidence_refs"])?.DeepClone();
        if (compact is not null) result["compact"] = compact.DeepClone();
        return result;
    }

    private static JsonObject MissingBundle(string runId, int afterCursor, int eventLimit)
    {
        afterCursor = Math.Max(0, afterCursor);
        eventLimit = Math.Clamp(eventLimit, 0, 128);
        return new JsonObject
        {
            ["status"] = "FAIL",
            ["reason"] = "runtime smoke run not found",
            ["run_id"] = runId,
            ["final"] = true,
            ["events"] = new JsonArray(),
            ["event_cursor"] = new JsonObject { ["after_cursor"] = afterCursor, ["next_cursor"] = afterCursor, ["oldest_cursor"] = null, ["dropped_count"] = 0, ["stale_cursor"] = false, ["limit"] = eventLimit },
            ["result"] = null,
            ["evidence_refs"] = new JsonArray(),
            ["cleanup"] = null,
            ["next_actions"] = new JsonArray("runtime_smoke_run_plan")
        };
    }
}
