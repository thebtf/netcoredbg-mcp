using System.Text.Json.Nodes;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

[Trait("Coverage", "Exclude")]
public sealed class FixtureTranscriptEntryTests
{
    private const string RawPayload = "{ \"type\": \"event\", \"event\": \"stopped\", \"body\": { \"reason\": \"step\", \"threadId\": 1 } }\n";

    [Theory]
    [InlineData(null)]
    [InlineData("null")]
    [InlineData("\"not-an-int32\"")]
    [InlineData("true")]
    [InlineData("[]")]
    [InlineData("{}")]
    [InlineData("1.5")]
    [InlineData("2147483648")]
    [InlineData("-2147483649")]
    public void Parse_MissingNullOrNonInt32Sequence_RemainsUnknownAndPreservesMalformedRawPayload(string? sequenceJson)
    {
        var transcript = new JsonObject
        {
            ["kind"] = "wire-event",
            ["rawPayload"] = RawPayload,
        };
        if (sequenceJson is not null) transcript["sequence"] = JsonNode.Parse(sequenceJson);

        var entry = FixtureTranscriptEntry.Parse(transcript.ToJsonString());

        Assert.Null(entry.Sequence);
        Assert.Equal("wire-event", entry.Kind);
        Assert.Equal(RawPayload, entry.RawPayload);
    }

    [Theory]
    [InlineData(int.MinValue)]
    [InlineData(-1)]
    [InlineData(0)]
    [InlineData(1)]
    [InlineData(int.MaxValue)]
    public void Parse_Int32Sequence_PreservesObservedValueWithoutRewritingProtocolEvidence(int sequence)
    {
        var transcript = new JsonObject
        {
            ["kind"] = "wire-event",
            ["sequence"] = sequence,
            ["rawPayload"] = RawPayload,
        };

        var entry = FixtureTranscriptEntry.Parse(transcript.ToJsonString());

        Assert.Equal(sequence, entry.Sequence);
        Assert.Equal(RawPayload, entry.RawPayload);
    }
}
