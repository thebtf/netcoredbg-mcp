using System.Collections.ObjectModel;
using System.Text.Json.Serialization;

namespace NetCoreDbg.Mcp.DesignProbe.Wpf;

public interface IWpfProbeSnapshotSource
{
    long ReadProbeOwnedRevision();

    WpfProbeMaterialization MaterializeProbeOwnedFacts();
}

public sealed class WpfProbeMaterialization
{
    public WpfProbeMaterialization(
        string rootId,
        bool requiredFactsComplete,
        IReadOnlyList<WpfSceneNodeFactDto> nodes,
        WpfProbeStabilityObservationDto stability)
    {
        RootId = rootId ?? throw new ArgumentNullException(nameof(rootId));
        RequiredFactsComplete = requiredFactsComplete;
        Nodes = nodes ?? throw new ArgumentNullException(nameof(nodes));
        Stability = stability ?? throw new ArgumentNullException(nameof(stability));
    }

    public string RootId { get; }

    public bool RequiredFactsComplete { get; }

    public IReadOnlyList<WpfSceneNodeFactDto> Nodes { get; }

    public WpfProbeStabilityObservationDto Stability { get; }
}

public sealed class WpfProbeStabilityObservationDto
{
    public WpfProbeStabilityObservationDto(long sceneEpoch, WpfProbeStabilityConditionsDto conditions)
    {
        if (sceneEpoch < 0)
        {
            throw new ArgumentOutOfRangeException(nameof(sceneEpoch));
        }

        SceneEpoch = sceneEpoch;
        Conditions = conditions ?? throw new ArgumentNullException(nameof(conditions));
    }

    [JsonPropertyName("sceneEpoch")]
    public long SceneEpoch { get; }

    [JsonPropertyName("conditions")]
    public WpfProbeStabilityConditionsDto Conditions { get; }
}

public sealed class WpfProbeStabilityConditionsDto
{
    public WpfProbeStabilityConditionsDto(
        WpfProbeConditionObservationDto dispatcherIdle,
        WpfProbeConditionObservationDto stableLayout,
        WpfProbeConditionObservationDto animationState,
        WpfProbeConditionObservationDto windowGeometry,
        WpfProbeConditionObservationDto contextMaterialization,
        WpfProbeConditionObservationDto asyncLoadSettled)
    {
        DispatcherIdle = dispatcherIdle ?? throw new ArgumentNullException(nameof(dispatcherIdle));
        StableLayout = stableLayout ?? throw new ArgumentNullException(nameof(stableLayout));
        AnimationState = animationState ?? throw new ArgumentNullException(nameof(animationState));
        WindowGeometry = windowGeometry ?? throw new ArgumentNullException(nameof(windowGeometry));
        ContextMaterialization = contextMaterialization ?? throw new ArgumentNullException(nameof(contextMaterialization));
        AsyncLoadSettled = asyncLoadSettled ?? throw new ArgumentNullException(nameof(asyncLoadSettled));
    }

    [JsonPropertyName("dispatcherIdle")]
    public WpfProbeConditionObservationDto DispatcherIdle { get; }

    [JsonPropertyName("stableLayout")]
    public WpfProbeConditionObservationDto StableLayout { get; }

    [JsonPropertyName("animationState")]
    public WpfProbeConditionObservationDto AnimationState { get; }

    [JsonPropertyName("windowGeometry")]
    public WpfProbeConditionObservationDto WindowGeometry { get; }

    [JsonPropertyName("contextMaterialization")]
    public WpfProbeConditionObservationDto ContextMaterialization { get; }

    [JsonPropertyName("asyncLoadSettled")]
    public WpfProbeConditionObservationDto AsyncLoadSettled { get; }
}

public sealed class WpfProbeConditionObservationDto
{
    public WpfProbeConditionObservationDto(string state)
    {
        if (state is not ("met" or "not_met" or "unsupported" or "unobservable"))
        {
            throw new ArgumentOutOfRangeException(nameof(state));
        }

        State = state;
    }

    [JsonPropertyName("state")]
    public string State { get; }
}

public sealed class WpfSceneNodeFactDto
{
    public WpfSceneNodeFactDto(
        string id,
        string? automationId,
        string? accessibleName,
        double x,
        double y,
        double width,
        double height,
        string? text)
    {
        Id = id ?? throw new ArgumentNullException(nameof(id));
        AutomationId = automationId;
        AccessibleName = accessibleName;
        X = x;
        Y = y;
        Width = width;
        Height = height;
        Text = text;
    }

    [JsonPropertyName("id")]
    public string Id { get; }

    [JsonPropertyName("automationId")]
    public string? AutomationId { get; }

    [JsonPropertyName("accessibleName")]
    public string? AccessibleName { get; }

    [JsonPropertyName("x")]
    public double X { get; }

    [JsonPropertyName("y")]
    public double Y { get; }

    [JsonPropertyName("width")]
    public double Width { get; }

    [JsonPropertyName("height")]
    public double Height { get; }

    [JsonPropertyName("text")]
    public string? Text { get; }
}

public sealed class WpfSceneCandidateFactsDto
{
    internal WpfSceneCandidateFactsDto(int processId, string processIdentity)
    {
        ProcessId = processId;
        ProcessIdentity = processIdentity;
    }

    [JsonPropertyName("processId")]
    public int ProcessId { get; }

    [JsonPropertyName("processIdentity")]
    public string ProcessIdentity { get; }
}

public sealed class WpfSceneProcessFactsDto
{
    internal WpfSceneProcessFactsDto(int processId, string processName, DateTimeOffset startedAtUtc)
    {
        ProcessId = processId;
        ProcessName = processName;
        StartedAtUtc = startedAtUtc;
    }

    [JsonPropertyName("processId")]
    public int ProcessId { get; }

    [JsonPropertyName("processName")]
    public string ProcessName { get; }

    [JsonPropertyName("startedAtUtc")]
    public DateTimeOffset StartedAtUtc { get; }
}

public sealed class WpfSceneSnapshotDto
{
    internal WpfSceneSnapshotDto(
        long revisionBefore,
        long revisionAfter,
        bool complete,
        string rootId,
        WpfSceneCandidateFactsDto candidate,
        WpfSceneProcessFactsDto process,
        WpfProbeStabilityObservationDto stability,
        WpfSceneNodeFactDto[] nodes)
    {
        RevisionBefore = revisionBefore;
        RevisionAfter = revisionAfter;
        Complete = complete;
        RootId = rootId;
        Candidate = candidate;
        Process = process;
        Stability = stability ?? throw new ArgumentNullException(nameof(stability));
        Nodes = Array.AsReadOnly(nodes);
    }

    [JsonPropertyName("revisionBefore")]
    public long RevisionBefore { get; }

    [JsonPropertyName("revisionAfter")]
    public long RevisionAfter { get; }

    [JsonPropertyName("complete")]
    public bool Complete { get; }

    [JsonPropertyName("nodes")]
    public IReadOnlyList<WpfSceneNodeFactDto> Nodes { get; }

    [JsonPropertyName("rootId")]
    public string RootId { get; }

    [JsonPropertyName("candidate")]
    public WpfSceneCandidateFactsDto Candidate { get; }

    [JsonPropertyName("process")]
    public WpfSceneProcessFactsDto Process { get; }

    [JsonPropertyName("stability")]
    public WpfProbeStabilityObservationDto Stability { get; }

    [JsonPropertyName("authority")]
    public string Authority => "in_process_probe";
}
