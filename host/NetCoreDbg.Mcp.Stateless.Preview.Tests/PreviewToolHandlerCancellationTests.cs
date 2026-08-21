using System.Text.Json;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.CodeSearch.Core;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Preview.Tests;

public sealed class PreviewToolHandlerCancellationTests
{
    [Fact]
    public async Task UnknownToolCancellationBeforeResponse_ReturnsNoToolResult_AndLaterRequestWorks()
    {
        using var root = TestRoot.Create("FollowupMarker");
        await AssertCancellationBeforeResponseAsync(root.Path, "unknown", new Dictionary<string, JsonElement>());
    }

    [Fact]
    public async Task InvalidArgumentsCancellationBeforeResponse_ReturnsNoToolResult_AndLaterRequestWorks()
    {
        using var root = TestRoot.Create("FollowupMarker");
        await AssertCancellationBeforeResponseAsync(root.Path, PreviewToolCatalog.FindCodeSymbol, arguments: null);
    }

    [Fact]
    public async Task SuccessCancellationBeforeResponse_ReturnsNoToolResult_AndLaterRequestWorks()
    {
        using var root = TestRoot.Create("SuccessMarker");
        await AssertCancellationBeforeResponseAsync(
            root.Path,
            PreviewToolCatalog.FindCodeSymbol,
            Arguments("SuccessMarker", "class"));
    }

    [Fact]
    public async Task FrameCapCancellationBeforeResponse_ReturnsNoToolResult_AndLaterRequestWorks()
    {
        var source = string.Concat(Enumerable.Repeat(
            "public sealed class FrameLimitMarker { } // " + string.Concat(Enumerable.Repeat(char.ConvertFromUtf32(0x1F600), 500)) + "\n",
            128));
        using var root = TestRoot.Create("FollowupMarker", source);
        await AssertCancellationBeforeResponseAsync(
            root.Path,
            PreviewToolCatalog.FindCodeSymbol,
            Arguments("FrameLimitMarker", "class"),
            expectedFailure: "preview_search_budget_exceeded");
    }

    [Fact]
    public async Task SearchFailureCancellationBeforeResponse_ReturnsNoToolResult_AndLaterRequestWorks()
    {
        using var root = TestRoot.Create("FailureMarker");
        using var outside = TestRoot.Create("EscapingMarker");
        File.CreateSymbolicLink(
            Path.Combine(root.Path, "ZEscaping.cs"),
            Path.Combine(outside.Path, "Marker.cs"));

        await AssertCancellationBeforeResponseAsync(
            root.Path,
            PreviewToolCatalog.FindCodeSymbol,
            Arguments("FailureMarker", "class"),
            expectedFailure: "preview_path_refused");
    }

    private static async Task AssertCancellationBeforeResponseAsync(
        string root,
        string name,
        IDictionary<string, JsonElement>? arguments,
        string? expectedFailure = null)
    {
        if (expectedFailure is not null)
        {
            var normal = await new PreviewToolHandler(CreateSearch(root)).CallAsync(CreateRequest(name, arguments), NewRequestId(), CancellationToken.None);
            Assert.Equal(expectedFailure, normal.StructuredContent!.Value.GetProperty("kind").GetString());
        }

        using var cancellation = new CancellationTokenSource();
        var handler = new PreviewToolHandler(CreateSearch(root), cancellation.Cancel);
        await Assert.ThrowsAsync<OperationCanceledException>(async () =>
            await handler.CallAsync(CreateRequest(name, arguments), NewRequestId(), cancellation.Token));

        var next = await handler.CallAsync(
            CreateRequest("later", new Dictionary<string, JsonElement>()),
            NewRequestId(),
            CancellationToken.None);
        Assert.True(next.IsError);
    }

    private static SymbolSearchEngine CreateSearch(string root) => new(root, PreviewSearchPolicy.Instance);

    private static CallToolRequestParams CreateRequest(
        string name,
        IDictionary<string, JsonElement>? arguments) => new() { Name = name, Arguments = arguments };

    private static RequestId NewRequestId() => new(Guid.NewGuid().ToString("N"));

    private static Dictionary<string, JsonElement> Arguments(string name, string kind) => new()
    {
        ["name"] = JsonSerializer.SerializeToElement(name),
        ["kind"] = JsonSerializer.SerializeToElement(kind),
    };

    private sealed class TestRoot : IDisposable
    {
        private TestRoot(string path)
        {
            Path = path;
        }

        internal string Path { get; }

        internal static TestRoot Create(string marker, string? source = null)
        {
            var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"netcoredbg-preview-handler-{Guid.NewGuid():N}");
            Directory.CreateDirectory(path);
            File.WriteAllText(
                System.IO.Path.Combine(path, "Marker.cs"),
                source ?? $"public sealed class {marker} {{ }}\n");
            return new TestRoot(path);
        }

        public void Dispose()
        {
            if (Directory.Exists(Path))
            {
                Directory.Delete(Path, recursive: true);
            }
        }
    }
}
