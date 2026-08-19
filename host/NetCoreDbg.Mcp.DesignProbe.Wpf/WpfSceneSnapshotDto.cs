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
        IReadOnlyList<WpfSceneNodeFactDto> nodes)
    {
        RootId = rootId ?? throw new ArgumentNullException(nameof(rootId));
        RequiredFactsComplete = requiredFactsComplete;
        Nodes = nodes ?? throw new ArgumentNullException(nameof(nodes));
    }

    public string RootId { get; }

    public bool RequiredFactsComplete { get; }

    public IReadOnlyList<WpfSceneNodeFactDto> Nodes { get; }
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
        WpfSceneNodeFactDto[] nodes)
    {
        RevisionBefore = revisionBefore;
        RevisionAfter = revisionAfter;
        Complete = complete;
        RootId = rootId;
        Candidate = candidate;
        Process = process;
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

    [JsonPropertyName("authority")]
    public string Authority => "in_process_probe";
}
