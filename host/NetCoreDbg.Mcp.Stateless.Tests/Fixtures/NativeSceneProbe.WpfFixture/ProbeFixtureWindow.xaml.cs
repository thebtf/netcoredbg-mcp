using System.Globalization;
using System.Windows;
using System.Windows.Automation;
using System.Windows.Controls;

using NetCoreDbg.Mcp.DesignProbe.Wpf;

namespace NativeSceneProbe.WpfFixture;

public partial class ProbeFixtureWindow : Window, IWpfProbeSnapshotSource
{
    private const long InitialRevision = 77;
    private readonly ProbeFixtureMode _mode;
    private long _revision = InitialRevision;

    internal ProbeFixtureWindow(ProbeFixtureMode mode)
    {
        _mode = mode;
        InitializeComponent();
        RefreshProbeState();
    }

    internal long ReadProbeOwnedRevision() => _revision;

    internal ProbeFixtureMaterialization MaterializeProbeOwnedFacts()
    {
        if (!Dispatcher.CheckAccess())
        {
            throw new InvalidOperationException("Probe facts must be materialized on the fixture dispatcher.");
        }

        UpdateLayout();
        var revisionBefore = _revision;
        if (_mode == ProbeFixtureMode.ChangedBeforeMaterialization)
        {
            AdvanceRevision();
        }

        var elements = new[]
        {
            DescribeElement(SceneRoot, "Gallery"),
            DescribeElement(SceneHeading, "Gallery.Heading"),
            DescribeElement(SaveButton, "Button.Primary"),
            DescribeElement(FirstAmbiguousButton, "Button.Duplicate"),
            DescribeElement(SecondAmbiguousButton, "Button.Duplicate"),
            DescribeElement(RevisionValue, "Probe.Revision"),
        };

        if (_mode == ProbeFixtureMode.IncompleteEvidence)
        {
            elements[2] = elements[2] with { Text = null };
        }

        if (_mode == ProbeFixtureMode.ChangedAfterMaterialization)
        {
            AdvanceRevision();
        }

        return new ProbeFixtureMaterialization(
            StoryId: "Button.Primary",
            FixtureId: "Gallery",
            RevisionBefore: revisionBefore,
            RevisionAfter: _revision,
            RequiredFactsComplete: _mode != ProbeFixtureMode.IncompleteEvidence,
            Elements: Array.AsReadOnly(elements));
    }

    long IWpfProbeSnapshotSource.ReadProbeOwnedRevision() => ReadProbeOwnedRevision();

    WpfProbeMaterialization IWpfProbeSnapshotSource.MaterializeProbeOwnedFacts()
    {
        var materialization = MaterializeProbeOwnedFacts();
        var nodes = new WpfSceneNodeFactDto[materialization.Elements.Count];
        for (var index = 0; index < materialization.Elements.Count; index++)
        {
            var element = materialization.Elements[index];
            nodes[index] = new WpfSceneNodeFactDto(
                element.ContractId,
                element.AutomationId,
                element.AccessibleName,
                element.X,
                element.Y,
                element.Width,
                element.Height,
                element.Text);
        }

        return new WpfProbeMaterialization(materialization.FixtureId, materialization.RequiredFactsComplete, nodes);
    }

    private void AdvanceRevision()
    {
        checked
        {
            _revision++;
        }

        RefreshProbeState();
    }

    private void RefreshProbeState()
    {
        RevisionValue.Text = _revision.ToString(CultureInfo.InvariantCulture);
        ModeValue.Text = $"Fixture mode: {ProbeFixtureModeParser.ToDisplayText(_mode)}";
        EvidenceValue.Text = _mode == ProbeFixtureMode.IncompleteEvidence
            ? "Required evidence: incomplete"
            : "Required evidence: complete";
    }

    private ProbeFixtureElementFact DescribeElement(FrameworkElement element, string contractId)
    {
        var bounds = element.TransformToAncestor(this).TransformBounds(new Rect(element.RenderSize));
        return new ProbeFixtureElementFact(
            ContractId: contractId,
            AutomationId: AutomationProperties.GetAutomationId(element),
            AccessibleName: AutomationProperties.GetName(element),
            X: bounds.X,
            Y: bounds.Y,
            Width: bounds.Width,
            Height: bounds.Height,
            Text: GetText(element));
    }

    private static string? GetText(FrameworkElement element) => element switch
    {
        TextBlock textBlock => textBlock.Text,
        TextBox textBox => textBox.Text,
        ContentControl { Content: string content } => content,
        _ => null,
    };
}

internal enum ProbeFixtureMode
{
    Stable,
    ChangedBeforeMaterialization,
    ChangedAfterMaterialization,
    IncompleteEvidence,
}

internal static class ProbeFixtureModeParser
{
    public static bool TryParse(string value, out ProbeFixtureMode mode)
    {
        switch (value)
        {
            case "stable":
                mode = ProbeFixtureMode.Stable;
                return true;
            case "changed-before":
                mode = ProbeFixtureMode.ChangedBeforeMaterialization;
                return true;
            case "changed-after":
                mode = ProbeFixtureMode.ChangedAfterMaterialization;
                return true;
            case "incomplete":
                mode = ProbeFixtureMode.IncompleteEvidence;
                return true;
            default:
                mode = default;
                return false;
        }
    }

    public static string ToDisplayText(ProbeFixtureMode mode) => mode switch
    {
        ProbeFixtureMode.Stable => "stable",
        ProbeFixtureMode.ChangedBeforeMaterialization => "changed-before",
        ProbeFixtureMode.ChangedAfterMaterialization => "changed-after",
        ProbeFixtureMode.IncompleteEvidence => "incomplete",
        _ => throw new InvalidOperationException($"Unsupported fixture mode '{mode}'."),
    };
}

internal sealed record ProbeFixtureMaterialization(
    string StoryId,
    string FixtureId,
    long RevisionBefore,
    long RevisionAfter,
    bool RequiredFactsComplete,
    IReadOnlyList<ProbeFixtureElementFact> Elements);

internal sealed record ProbeFixtureElementFact(
    string ContractId,
    string AutomationId,
    string AccessibleName,
    double X,
    double Y,
    double Width,
    double Height,
    string? Text);
