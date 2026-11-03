using System.Reflection;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class ObservedIdentitySelectionTests
{
    private const int ProcessId = 4242;
    private const int MaximumVisits = 4096;
    private static readonly int[] RequestedRuntimeId = [7, 42];
    private static readonly int[] UnrelatedRuntimeId = [7, 99];

    [Fact]
    public void ObservedIdentitySelection_RequestedMatchAfterReadableNodes_CompletesUnique()
    {
        var selection = new SelectionDriver(RequestedRuntimeId);

        Assert.Equal((true, false), selection.TryVisit(ProcessId, [7, 1]));
        Assert.Equal((true, false), selection.TryVisit(ProcessId, [7, 2]));
        Assert.Equal((true, true), selection.TryVisit(ProcessId, RequestedRuntimeId));
        Assert.Equal((true, false), selection.TryVisit(ProcessId, [7, 3]));

        Assert.Equal("Unique", selection.Complete(traversalComplete: true));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void ObservedIdentitySelection_NoRequestedMatch_CompletesMissing(bool visitReadableNodes)
    {
        var selection = new SelectionDriver(RequestedRuntimeId);
        if (visitReadableNodes)
        {
            Assert.Equal((true, false), selection.TryVisit(ProcessId, [7, 1]));
            Assert.Equal((true, false), selection.TryVisit(ProcessId, [7, 2]));
        }

        Assert.Equal("Missing", selection.Complete(traversalComplete: true));
    }

    [Fact]
    public void ObservedIdentitySelection_RequestedIdTwice_LatchesAmbiguous()
    {
        var selection = new SelectionDriver(RequestedRuntimeId);
        Assert.Equal((true, true), selection.TryVisit(ProcessId, RequestedRuntimeId));
        Assert.Equal((true, false), selection.TryVisit(ProcessId, UnrelatedRuntimeId));

        Assert.Equal((false, false), selection.TryVisit(ProcessId, [7, 42]));
        Assert.Equal("Ambiguous", selection.Complete(traversalComplete: true));
        Assert.Equal("Ambiguous", selection.Complete(traversalComplete: false));
        Assert.Equal((false, false), selection.TryVisit(null, null));
        Assert.Equal((false, false), selection.TryVisit(ProcessId, UnrelatedRuntimeId));
        Assert.Equal((false, false), selection.TryVisit(ProcessId, RequestedRuntimeId));
        Assert.Equal("Ambiguous", selection.Complete(traversalComplete: true));
    }

    [Theory]
    [InlineData(null, 2, false)]
    [InlineData(null, 2, true)]
    [InlineData(4243, 2, false)]
    [InlineData(4243, 2, true)]
    [InlineData(ProcessId, null, false)]
    [InlineData(ProcessId, null, true)]
    [InlineData(ProcessId, 0, false)]
    [InlineData(ProcessId, 0, true)]
    [InlineData(ProcessId, 65, false)]
    [InlineData(ProcessId, 65, true)]
    public void ObservedIdentitySelection_UnreadableOrForeignIdentity_LatchesUnobservable(
        int? processId, int? runtimeIdLength, bool matchFirst)
    {
        var selection = new SelectionDriver(RequestedRuntimeId);
        if (matchFirst)
            Assert.Equal((true, true), selection.TryVisit(ProcessId, RequestedRuntimeId));
        var runtimeId = runtimeIdLength.HasValue ? new int[runtimeIdLength.Value] : null;

        Assert.Equal((false, false), selection.TryVisit(processId, runtimeId));
        Assert.Equal("Unobservable", selection.Complete(traversalComplete: true));
        Assert.Equal("Unobservable", selection.Complete(traversalComplete: false));
        Assert.Equal((false, false), selection.TryVisit(ProcessId, RequestedRuntimeId));
        Assert.Equal((false, false), selection.TryVisit(ProcessId, RequestedRuntimeId));
        Assert.Equal((false, false), selection.TryVisit(ProcessId, UnrelatedRuntimeId));
        Assert.Equal("Unobservable", selection.Complete(traversalComplete: true));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void ObservedIdentitySelection_IncompleteTraversal_CompletesUnobservable(bool matchFirst)
    {
        var selection = new SelectionDriver(RequestedRuntimeId);
        Assert.Equal((true, matchFirst), selection.TryVisit(
            ProcessId, matchFirst ? RequestedRuntimeId : UnrelatedRuntimeId));

        Assert.Equal("Unobservable", selection.Complete(traversalComplete: false));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void ObservedIdentitySelection_Exactly4096Visits_CanComplete(bool includeRequestedMatch)
    {
        var selection = new SelectionDriver(RequestedRuntimeId);
        for (var visit = 1; visit < MaximumVisits; visit++)
            Assert.Equal((true, false), selection.TryVisit(ProcessId, UnrelatedRuntimeId));

        Assert.Equal((true, includeRequestedMatch), selection.TryVisit(
            ProcessId, includeRequestedMatch ? RequestedRuntimeId : UnrelatedRuntimeId));
        Assert.Equal(includeRequestedMatch ? "Unique" : "Missing", selection.Complete(traversalComplete: true));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void ObservedIdentitySelection_Visit4097_LatchesUnobservable(bool matchWithinLimit)
    {
        var selection = new SelectionDriver(RequestedRuntimeId);
        Assert.Equal((true, matchWithinLimit), selection.TryVisit(
            ProcessId, matchWithinLimit ? RequestedRuntimeId : UnrelatedRuntimeId));
        for (var visit = 1; visit < MaximumVisits; visit++)
            Assert.Equal((true, false), selection.TryVisit(ProcessId, UnrelatedRuntimeId));
        Assert.Equal(matchWithinLimit ? "Unique" : "Missing", selection.Complete(traversalComplete: true));

        Assert.Equal((false, false), selection.TryVisit(ProcessId, RequestedRuntimeId));
        Assert.Equal("Unobservable", selection.Complete(traversalComplete: true));
        Assert.Equal("Unobservable", selection.Complete(traversalComplete: false));
        Assert.Equal((false, false), selection.TryVisit(ProcessId, RequestedRuntimeId));
        Assert.Equal((false, false), selection.TryVisit(ProcessId, UnrelatedRuntimeId));
        Assert.Equal("Unobservable", selection.Complete(traversalComplete: true));
    }

    [Fact]
    public void ObservedIdentitySelection_DuplicateUnrelatedIds_DoesNotBlockUniqueRequestedId()
    {
        var selection = new SelectionDriver(RequestedRuntimeId);
        Assert.Equal((true, false), selection.TryVisit(ProcessId, UnrelatedRuntimeId));
        Assert.Equal((true, false), selection.TryVisit(ProcessId, [7, 99]));
        Assert.Equal((true, true), selection.TryVisit(ProcessId, RequestedRuntimeId));
        Assert.Equal((true, false), selection.TryVisit(ProcessId, [7, 99]));

        Assert.Equal("Unique", selection.Complete(traversalComplete: true));
    }

    [Theory]
    [InlineData(1)]
    [InlineData(64)]
    public void ObservedIdentitySelection_RuntimeIdAtValidLengthBoundary_CompletesUnique(int length)
    {
        var runtimeId = Enumerable.Range(1, length).ToArray();
        var selection = new SelectionDriver(runtimeId);

        Assert.Equal((true, true), selection.TryVisit(ProcessId, runtimeId.ToArray()));
        Assert.Equal("Unique", selection.Complete(traversalComplete: true));
    }

    private sealed class SelectionDriver
    {
        private readonly object _selection;
        private readonly MethodInfo _tryVisit;
        private readonly MethodInfo _complete;

        public SelectionDriver(int[] requestedRuntimeId)
        {
            var assembly = Assembly.LoadFrom(ResolveBridgeAssemblyPath());
            var commandType = assembly.GetType("FlaUIBridge.Commands.NativeUiInvokeCommand", throwOnError: true)!;
            var selectionType = commandType.GetNestedType("ObservedIdentitySelection", BindingFlags.NonPublic)
                ?? throw new InvalidOperationException("The production observed-identity selection accumulator is absent.");
            _selection = Activator.CreateInstance(
                selectionType, BindingFlags.Instance | BindingFlags.NonPublic,
                binder: null, args: [ProcessId, requestedRuntimeId], culture: null)
                ?? throw new InvalidOperationException("Could not create the production observed-identity selection accumulator.");
            _tryVisit = selectionType.GetMethod(
                "TryVisit", BindingFlags.Instance | BindingFlags.NonPublic,
                binder: null, types: [typeof(int?), typeof(int[]), typeof(bool).MakeByRefType()], modifiers: null)
                ?? throw new InvalidOperationException("The production observed-identity visit operation is absent.");
            _complete = selectionType.GetMethod(
                "Complete", BindingFlags.Instance | BindingFlags.NonPublic,
                binder: null, types: [typeof(bool)], modifiers: null)
                ?? throw new InvalidOperationException("The production observed-identity completion operation is absent.");
        }

        public (bool Accepted, bool Matches) TryVisit(int? processId, int[]? runtimeId)
        {
            object?[] arguments = [processId, runtimeId, false];
            var accepted = Assert.IsType<bool>(_tryVisit.Invoke(_selection, arguments));
            return (accepted, Assert.IsType<bool>(arguments[2]));
        }

        public string Complete(bool traversalComplete) =>
            Assert.IsAssignableFrom<Enum>(_complete.Invoke(_selection, [traversalComplete])).ToString();
    }

    private static string ResolveBridgeAssemblyPath()
    {
        var configuration = new DirectoryInfo(AppContext.BaseDirectory).Parent?.Name
            ?? throw new InvalidOperationException("Test output configuration is absent.");
        var bridgeDirectory = Path.Combine(RepositoryLayout.Root, "bridge", "bin", configuration, "net8.0-windows");
        return new[]
            {
                Path.Combine(bridgeDirectory, "win-x64", "FlaUIBridge.dll"),
                Path.Combine(bridgeDirectory, "FlaUIBridge.dll"),
            }
            .FirstOrDefault(File.Exists)
            ?? throw new InvalidOperationException($"Built candidate FlaUI bridge assembly is absent beneath {bridgeDirectory}.");
    }
}
