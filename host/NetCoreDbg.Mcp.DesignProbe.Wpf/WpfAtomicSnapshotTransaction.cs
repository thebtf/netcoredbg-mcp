using System.Diagnostics;
using System.Globalization;
using System.IO;
using System.Windows.Threading;

namespace NetCoreDbg.Mcp.DesignProbe.Wpf;

public sealed class WpfAtomicSnapshotTransaction
{
    private const int MaximumNodeCount = 256;
    private const int MaximumStringLength = 4096;

    private readonly Dispatcher _dispatcher;
    private readonly IWpfProbeSnapshotSource _source;

    public WpfAtomicSnapshotTransaction(Dispatcher dispatcher, IWpfProbeSnapshotSource source)
    {
        _dispatcher = dispatcher ?? throw new ArgumentNullException(nameof(dispatcher));
        _source = source ?? throw new ArgumentNullException(nameof(source));
    }

    public WpfSceneSnapshotDto Capture()
    {
        if (_dispatcher.HasShutdownStarted || _dispatcher.HasShutdownFinished)
        {
            throw new InvalidOperationException("The WPF dispatcher is unavailable for a probe capture.");
        }

        return _dispatcher.CheckAccess() ? CaptureOnDispatcher() : _dispatcher.Invoke(CaptureOnDispatcher);
    }

    private WpfSceneSnapshotDto CaptureOnDispatcher()
    {
        if (!_dispatcher.CheckAccess())
        {
            throw new InvalidOperationException("The WPF probe capture must execute on its dispatcher.");
        }

        var revisionBefore = _source.ReadProbeOwnedRevision();
        var materialization = _source.MaterializeProbeOwnedFacts()
            ?? throw new InvalidDataException("The WPF probe materialization is required.");
        var revisionAfter = _source.ReadProbeOwnedRevision();

        var nodes = CopyBoundedNodes(materialization);
        var (candidate, process) = ReadCurrentProcessFacts();
        return new WpfSceneSnapshotDto(
            revisionBefore,
            revisionAfter,
            materialization.RequiredFactsComplete && revisionBefore == revisionAfter,
            EnsureRequiredString(materialization.RootId, "rootId"),
            candidate,
            process,
            materialization.Stability,
            nodes);
    }

    private static WpfSceneNodeFactDto[] CopyBoundedNodes(WpfProbeMaterialization materialization)
    {
        var sourceNodes = materialization.Nodes;
        if (sourceNodes.Count is < 1 or > MaximumNodeCount)
        {
            throw new InvalidDataException($"The WPF probe node count must be in 1..{MaximumNodeCount}.");
        }

        var rootId = EnsureRequiredString(materialization.RootId, "rootId");
        var nodes = new WpfSceneNodeFactDto[sourceNodes.Count];
        var foundRoot = false;
        for (var index = 0; index < sourceNodes.Count; index++)
        {
            var source = sourceNodes[index] ?? throw new InvalidDataException("A WPF probe node is required.");
            var id = EnsureRequiredString(source.Id, "node.id");
            foundRoot |= StringComparer.Ordinal.Equals(rootId, id);
            ValidateOptionalString(source.AutomationId, "node.automationId");
            ValidateOptionalString(source.AccessibleName, "node.accessibleName");
            ValidateOptionalString(source.Text, "node.text");
            ValidateGeometry(source);
            nodes[index] = new WpfSceneNodeFactDto(
                id,
                source.AutomationId,
                source.AccessibleName,
                source.X,
                source.Y,
                source.Width,
                source.Height,
                source.Text);
        }

        if (!foundRoot)
        {
            throw new InvalidDataException("The WPF probe root must be represented by a node fact.");
        }

        return nodes;
    }

    private static void ValidateGeometry(WpfSceneNodeFactDto source)
    {
        if (!double.IsFinite(source.X) ||
            !double.IsFinite(source.Y) ||
            !double.IsFinite(source.Width) ||
            !double.IsFinite(source.Height) ||
            source.Width < 0 ||
            source.Height < 0)
        {
            throw new InvalidDataException("The WPF probe geometry must be finite with non-negative dimensions.");
        }
    }

    private static (WpfSceneCandidateFactsDto Candidate, WpfSceneProcessFactsDto Process) ReadCurrentProcessFacts()
    {
        using var currentProcess = Process.GetCurrentProcess();
        var processId = currentProcess.Id;
        var startedAtUtc = currentProcess.StartTime.ToUniversalTime();
        var processName = EnsureRequiredString(currentProcess.ProcessName, "processName");
        var processIdentity = string.Concat(
            "process_",
            processId.ToString(CultureInfo.InvariantCulture),
            "_start_",
            startedAtUtc.Ticks.ToString(CultureInfo.InvariantCulture));
        return (
            new WpfSceneCandidateFactsDto(processId, processIdentity),
            new WpfSceneProcessFactsDto(processId, processName, new DateTimeOffset(startedAtUtc)));
    }

    private static string EnsureRequiredString(string? value, string name)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > MaximumStringLength)
        {
            throw new InvalidDataException($"The WPF probe {name} must be a non-empty string no longer than {MaximumStringLength} characters.");
        }

        return value;
    }

    private static void ValidateOptionalString(string? value, string name)
    {
        if (value is { Length: > MaximumStringLength })
        {
            throw new InvalidDataException($"The WPF probe {name} exceeds {MaximumStringLength} characters.");
        }
    }
}
