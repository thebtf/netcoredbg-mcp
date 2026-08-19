using System.Globalization;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

internal sealed class NativeSceneStabilityCoordinator
{
    private static readonly string[] ConditionNames =
    [
        "dispatcherIdle",
        "stableLayout",
        "animationState",
        "windowGeometry",
        "contextMaterialization",
        "asyncLoadSettled",
    ];

    private readonly TimeProvider _timeProvider;
    private readonly Func<JsonElement, CancellationToken, Task<JsonObject>> _observeAsync;
    private readonly SemaphoreSlim _gate = new(initialCount: 1, maxCount: 1);
    private int _sceneEpoch;
    private int _sequence;

    internal NativeSceneStabilityCoordinator(
        TimeProvider timeProvider,
        Func<JsonElement, CancellationToken, Task<JsonObject>> observeAsync)
    {
        _timeProvider = timeProvider ?? throw new ArgumentNullException(nameof(timeProvider));
        _observeAsync = observeAsync ?? throw new ArgumentNullException(nameof(observeAsync));
    }

    internal Task<JsonObject> WaitForStableAsync(JsonElement sceneRequest, CancellationToken cancellationToken) =>
        ObserveAsync(sceneRequest, revalidatedByCapture: false, cancellationToken);

    internal Task<JsonObject> RevalidateForCaptureAsync(JsonElement sceneRequest, CancellationToken cancellationToken) =>
        ObserveAsync(sceneRequest, revalidatedByCapture: true, cancellationToken);

    private async Task<JsonObject> ObserveAsync(
        JsonElement sceneRequest,
        bool revalidatedByCapture,
        CancellationToken cancellationToken)
    {
        var policy = ReadSettlePolicy(sceneRequest);
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            cancellationToken.ThrowIfCancellationRequested();
            var startedAt = _timeProvider.GetUtcNow();
            using var observerCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            var deadline = Task.Delay(
                TimeSpan.FromMilliseconds(policy.TimeoutMs),
                _timeProvider,
                observerCancellation.Token);
            try
            {
                var lastConditions = CreateUnobservableConditions();
                string[]? baseline = null;
                var changed = false;

                for (var sampleIndex = 0; sampleIndex < policy.SampleCount; sampleIndex++)
                {
                    observerCancellation.Token.ThrowIfCancellationRequested();
                    var observationTask = _observeAsync(sceneRequest, observerCancellation.Token)
                        ?? throw new InvalidOperationException("Native-scene stability observer returned no observation.");
                    if (!ReferenceEquals(await Task.WhenAny(observationTask, deadline).ConfigureAwait(false), observationTask))
                    {
                        await deadline.ConfigureAwait(false);
                        observerCancellation.Cancel();
                        return CreateEvidence(policy, lastConditions, changed, startedAt, revalidatedByCapture, timedOut: true);
                    }

                    var observation = await observationTask.ConfigureAwait(false);
                    var conditions = NormalizeConditions(observation);
                    var states = ReadStates(conditions);
                    if (baseline is null)
                    {
                        baseline = states;
                    }
                    else
                    {
                        for (var conditionIndex = 0; conditionIndex < ConditionNames.Length; conditionIndex++)
                        {
                            if (policy.IsRequired(conditionIndex) &&
                                !StringComparer.Ordinal.Equals(baseline[conditionIndex], states[conditionIndex]))
                            {
                                changed = true;
                            }
                        }
                    }

                    _sceneEpoch = Math.Max(_sceneEpoch, ReadSceneEpoch(observation));
                    _sequence = checked(_sequence + 1);
                    lastConditions = conditions;
                }

                return CreateEvidence(policy, lastConditions, changed, startedAt, revalidatedByCapture, timedOut: false);
            }
            finally
            {
                observerCancellation.Cancel();
            }
        }
        finally
        {
            _gate.Release();
        }
    }
    private JsonObject CreateEvidence(
        SettlePolicy policy,
        JsonObject conditions,
        bool changed,
        DateTimeOffset startedAt,
        bool revalidatedByCapture,
        bool timedOut)
    {
        var observedAt = _timeProvider.GetUtcNow();
        var elapsedMilliseconds = ElapsedMilliseconds(startedAt, observedAt);
        return new JsonObject
        {
            ["status"] = Classify(policy, conditions, changed, elapsedMilliseconds, timedOut),
            ["revalidatedByCapture"] = revalidatedByCapture,
            ["conditions"] = conditions,
            ["settleDurationMs"] = Math.Min(elapsedMilliseconds, 30_000),
            ["observedAt"] = observedAt.ToString("O", CultureInfo.InvariantCulture),
            ["sceneEpoch"] = _sceneEpoch,
            ["sequence"] = _sequence,
        };
    }


    private static SettlePolicy ReadSettlePolicy(JsonElement sceneRequest)
    {
        if (sceneRequest.ValueKind != JsonValueKind.Object ||
            !sceneRequest.TryGetProperty("settlePolicy", out var policy) ||
            policy.ValueKind != JsonValueKind.Object)
        {
            throw new ArgumentException("The native-scene request has no settle policy.", nameof(sceneRequest));
        }

        var timeoutMs = ReadBoundedInteger(policy, "timeoutMs", minimum: 1, maximum: 30_000);
        var sampleCount = ReadBoundedInteger(policy, "sampleCount", minimum: 2, maximum: 16);
        var stableForMs = ReadBoundedInteger(policy, "stableForMs", minimum: 0, maximum: 30_000);
        return new SettlePolicy(
            timeoutMs,
            sampleCount,
            stableForMs,
            ReadBoolean(policy, "requireDispatcherIdle"),
            ReadBoolean(policy, "requireStableLayout"),
            ReadBoolean(policy, "requireAnimationState"),
            ReadBoolean(policy, "requireWindowGeometry"),
            ReadBoolean(policy, "requireContextMaterialization"),
            ReadBoolean(policy, "requireAsyncLoadSettled"));
    }

    private static int ReadBoundedInteger(JsonElement source, string name, int minimum, int maximum)
    {
        if (!source.TryGetProperty(name, out var value) ||
            value.ValueKind != JsonValueKind.Number ||
            !value.TryGetInt32(out var parsed) ||
            parsed < minimum ||
            parsed > maximum)
        {
            throw new ArgumentException($"The native-scene settle policy has an invalid '{name}'.", nameof(source));
        }

        return parsed;
    }

    private static bool ReadBoolean(JsonElement source, string name)
    {
        if (!source.TryGetProperty(name, out var value) ||
            (value.ValueKind != JsonValueKind.True && value.ValueKind != JsonValueKind.False))
        {
            throw new ArgumentException($"The native-scene settle policy has an invalid '{name}'.", nameof(source));
        }

        return value.GetBoolean();
    }

    private static JsonObject NormalizeConditions(JsonObject observation)
    {
        var observed = observation["conditions"] as JsonObject;
        var normalized = new JsonObject();
        foreach (var name in ConditionNames)
        {
            normalized[name] = new JsonObject { ["state"] = ReadState(observed?[name]) };
        }

        return normalized;
    }
    private static JsonObject CreateUnobservableConditions()
    {
        var conditions = new JsonObject();
        foreach (var name in ConditionNames)
        {
            conditions[name] = new JsonObject { ["state"] = "unobservable" };
        }

        return conditions;
    }


    private static string[] ReadStates(JsonObject conditions)
    {
        var states = new string[ConditionNames.Length];
        for (var index = 0; index < ConditionNames.Length; index++)
        {
            states[index] = ReadState(conditions[ConditionNames[index]]);
        }

        return states;
    }

    private static string ReadState(JsonNode? node)
    {
        if (node is JsonObject condition &&
            condition["state"] is JsonValue value &&
            value.TryGetValue<string>(out var state) &&
            state is "met" or "not_met" or "unsupported" or "unobservable")
        {
            return state;
        }

        return "unobservable";
    }

    private static int ReadSceneEpoch(JsonObject observation) =>
        observation["sceneEpoch"] is JsonValue value &&
        value.TryGetValue<int>(out var sceneEpoch) &&
        sceneEpoch >= 0
            ? sceneEpoch
            : 0;

    private static int ElapsedMilliseconds(DateTimeOffset startedAt, DateTimeOffset observedAt)
    {
        var elapsedTicks = observedAt.UtcDateTime.Ticks - startedAt.UtcDateTime.Ticks;
        return elapsedTicks <= 0
            ? 0
            : (int)Math.Min(elapsedTicks / TimeSpan.TicksPerMillisecond, int.MaxValue);
    }

    private static string Classify(
        SettlePolicy policy,
        JsonObject conditions,
        bool changed,
        int elapsedMilliseconds,
        bool timedOut)
    {
        var hasUnobservable = false;
        var hasPartialCondition = timedOut ||
                                  changed ||
                                  elapsedMilliseconds < policy.StableForMs ||
                                  elapsedMilliseconds > policy.TimeoutMs;
        for (var conditionIndex = 0; conditionIndex < ConditionNames.Length; conditionIndex++)
        {
            if (!policy.IsRequired(conditionIndex))
            {
                continue;
            }

            switch (ReadState(conditions[ConditionNames[conditionIndex]]))
            {
                case "unobservable":
                    hasUnobservable = true;
                    break;
                case "met":
                    break;
                default:
                    hasPartialCondition = true;
                    break;
            }
        }

        return hasUnobservable ? "UNOBSERVABLE" : hasPartialCondition ? "PARTIAL" : "STABLE";
    }

    private sealed record SettlePolicy(
        int TimeoutMs,
        int SampleCount,
        int StableForMs,
        bool RequireDispatcherIdle,
        bool RequireStableLayout,
        bool RequireAnimationState,
        bool RequireWindowGeometry,
        bool RequireContextMaterialization,
        bool RequireAsyncLoadSettled)
    {
        internal bool IsRequired(int conditionIndex) => conditionIndex switch
        {
            0 => RequireDispatcherIdle,
            1 => RequireStableLayout,
            2 => RequireAnimationState,
            3 => RequireWindowGeometry,
            4 => RequireContextMaterialization,
            5 => RequireAsyncLoadSettled,
            _ => throw new ArgumentOutOfRangeException(nameof(conditionIndex)),
        };
    }
}
