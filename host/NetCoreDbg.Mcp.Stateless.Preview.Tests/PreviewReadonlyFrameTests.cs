using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.CodeSearch.Core;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Preview.Tests;

public sealed class PreviewReadonlyFrameTests
{
    [Theory]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public void EscapedPayloadPreflightMatchesActualJsonRpcWireBytes(string tool)
    {
        const string escaped = "A/\"\\\u0001😀.cs";
        IReadOnlyList<ReferenceMatch> matches = [new(escaped, 3, "  \"\\\u0001😀 reference")];
        var context = new SourceContext(escaped, 3, 3, [new SourceLine(3, "  \"\\\u0001😀 source")]);
        var id = new RequestId("escaped-readonly");

        Assert.True(Fits(tool, id, matches, context, out var state));
        Assert.Equal(WireBytes(Response(tool, id, matches, context)), state.BytesWritten);
    }

    [Theory]
    [InlineData("find_code_references")]
    [InlineData("get_source_context")]
    public void SuccessPreflightCountsMetadataAtExactCapAndRejectsCapPlusOne(string tool)
    {
        IReadOnlyList<ReferenceMatch> matches = [new("Source.cs", 3, "Marker value;")];
        var context = new SourceContext("Source.cs", 3, 3, [new SourceLine(3, "  Marker value;")]);
        var baseline = Response(tool, new RequestId("base"), matches, context);
        var length = PreviewToolCatalog.MaximumCompleteResponseFrameBytes - WireBytes(baseline) + 4;
        var exact = new RequestId(new string('i', length));

        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, WireBytes(Response(tool, exact, matches, context)));
        Assert.True(Fits(tool, exact, matches, context, out var state));
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, state.BytesWritten);
        var beyond = new RequestId(new string('i', length + 1));
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes + 1, WireBytes(Response(tool, beyond, matches, context)));
        Assert.False(Fits(tool, beyond, matches, context, out var beyondState));
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, beyondState.BytesWritten);
    }

    [Theory]
    [InlineData("find_code_references", true)]
    [InlineData("find_code_references", false)]
    [InlineData("get_source_context", true)]
    [InlineData("get_source_context", false)]
    public void OversizedPathOrSourceIsRejectedUsingConstantTokenSlicesBeforePayloadSerialization(string tool, bool hugePath)
    {
        var oversized = new string('r', PreviewToolCatalog.MaximumCompleteResponseFrameBytes * 4);
        var file = hugePath ? oversized : "Source.cs";
        var text = hugePath ? "Marker value;" : oversized;
        IReadOnlyList<ReferenceMatch> matches = [new(file, 1, text)];
        var context = new SourceContext(file, 1, 1, [new SourceLine(1, text)]);

        Assert.False(Fits(tool, new RequestId("oversized-readonly"), matches, context, out var state));
        Assert.Equal(PreviewToolCatalog.MaximumCompleteResponseFrameBytes, state.BytesWritten);
        Assert.InRange(state.MaximumTokenUtf16CodeUnits, 1, 128);
    }

    private static bool Fits(
        string tool,
        RequestId id,
        IReadOnlyList<ReferenceMatch> matches,
        SourceContext context,
        out FramePreflightState state) => tool == PreviewToolCatalog.FindCodeReferences
            ? BoundedResponseFrameSerializer.FitsFindCodeReferencesSuccessResponseWithinLimit(id, matches, out state)
            : BoundedResponseFrameSerializer.FitsGetSourceContextSuccessResponseWithinLimit(id, context, out state);

    private static JsonRpcResponse Response(
        string tool,
        RequestId id,
        IReadOnlyList<ReferenceMatch> matches,
        SourceContext context)
    {
        object payload = tool == PreviewToolCatalog.FindCodeReferences
            ? new
            {
                kind = "find_code_references_success",
                results = matches.Select(static match => new { file = match.File, line = match.Line, context = match.Context }).ToArray(),
            }
            : new
            {
                kind = "get_source_context_success",
                file = context.File,
                start_line = context.StartLine,
                end_line = context.EndLine,
                lines = context.Lines.Select(static line => new { line = line.Line, text = line.Text }).ToArray(),
            };
        var result = JsonSerializer.SerializeToNode(new CallToolResult
        {
            ResultType = "complete",
            IsError = false,
            Content = [new TextContentBlock { Text = JsonSerializer.Serialize(payload) }],
            StructuredContent = JsonSerializer.SerializeToElement(payload),
        }, McpJsonUtilities.DefaultOptions)!.AsObject();
        result["_meta"] = new JsonObject
        {
            ["io.modelcontextprotocol/serverInfo"] = new JsonObject
            {
                ["name"] = PreviewToolCatalog.ServerName,
                ["version"] = PreviewToolCatalog.ServerVersion,
            },
        };
        return new JsonRpcResponse { Id = id, Result = result };
    }

    private static int WireBytes(JsonRpcMessage message) =>
        JsonSerializer.SerializeToUtf8Bytes(message, McpJsonUtilities.DefaultOptions).Length;
}
