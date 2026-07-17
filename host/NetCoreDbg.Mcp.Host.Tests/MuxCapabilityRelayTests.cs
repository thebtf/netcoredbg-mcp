using System.Text.Json;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// FD-007: deterministic absent/present-capability fixture for
/// <see cref="MuxCapabilityRelay.ProjectExperimentalCapabilities"/>, exercised directly
/// against every shape Python's advertised <c>experimental</c> dictionary might take. Every
/// <c>object</c> value here is a real deserialized <see cref="JsonElement"/> (via
/// <see cref="JsonDocument"/>), matching exactly what <c>ServerCapabilities.Experimental</c>
/// contains after a genuine wire round trip - never a hand-typed CLR object masquerading as
/// wire data.
/// </summary>
public sealed class MuxCapabilityRelayTests
{
    private static IDictionary<string, object> Experimental(string json) =>
        new Dictionary<string, object> { ["x-mux"] = JsonDocument.Parse(json).RootElement };

    [Fact]
    public void AbsentUpstreamExperimental_ProjectsNothing()
    {
        Assert.Null(MuxCapabilityRelay.ProjectExperimentalCapabilities(null));
    }

    [Fact]
    public void UpstreamExperimentalWithoutXMuxKey_ProjectsNothing()
    {
        var experimental = new Dictionary<string, object>
        {
            ["some-other-cap"] = JsonDocument.Parse("""{"anything":true}""").RootElement,
        };

        Assert.Null(MuxCapabilityRelay.ProjectExperimentalCapabilities(experimental));
    }

    [Fact]
    public void ExactAllowedXMuxValue_ProjectsOnlyThatKeyAndValue()
    {
        var projected = MuxCapabilityRelay.ProjectExperimentalCapabilities(
            Experimental("""{"sharing":"isolated"}"""));

        Assert.NotNull(projected);
        var property = Assert.Single(projected!);
        Assert.Equal("x-mux", property.Key);
        Assert.Equal("""{"sharing":"isolated"}""", property.Value!.ToJsonString());
    }

    [Theory]
    [InlineData("""{"sharing":"shared"}""")]
    [InlineData("""{"sharing":"ISOLATED"}""")]
    [InlineData("""{"Sharing":"isolated"}""")]
    [InlineData("""{}""")]
    [InlineData("""{"sharing":"isolated","extra":"field"}""")]
    [InlineData("""{"sharing":true}""")]
    [InlineData("""["sharing","isolated"]""")]
    [InlineData("\"isolated\"")]
    [InlineData("null")]
    public void AnyNonExactXMuxValue_ProjectsNothing(string muxValueJson)
    {
        Assert.Null(MuxCapabilityRelay.ProjectExperimentalCapabilities(Experimental(muxValueJson)));
    }

    [Fact]
    public void SiblingExperimentalKeys_NeverLeakPastTheAllowlist()
    {
        var experimental = new Dictionary<string, object>
        {
            ["x-mux"] = JsonDocument.Parse("""{"sharing":"isolated"}""").RootElement,
            ["y-other-experimental"] = JsonDocument.Parse("""{"secret":true}""").RootElement,
        };

        var projected = MuxCapabilityRelay.ProjectExperimentalCapabilities(experimental);

        Assert.NotNull(projected);
        Assert.Equal(["x-mux"], projected!.Select(p => p.Key));
        Assert.False(projected.ContainsKey("y-other-experimental"));
    }

    [Fact]
    public void NonJsonElementXMuxValue_ProjectsNothing()
    {
        // Defensive: only a real deserialized JsonElement is ever trusted, never an
        // arbitrary CLR object that happened to land in the dictionary.
        var experimental = new Dictionary<string, object> { ["x-mux"] = "isolated" };

        Assert.Null(MuxCapabilityRelay.ProjectExperimentalCapabilities(experimental));
    }

    [Fact]
    public void RepeatedCalls_EachReturnAFreshUnparentedNode()
    {
        // JsonNode instances cannot be reused across multiple parents; every call must
        // parse its own instance rather than sharing one cached node.
        var first = MuxCapabilityRelay.ProjectExperimentalCapabilities(Experimental("""{"sharing":"isolated"}"""));
        var second = MuxCapabilityRelay.ProjectExperimentalCapabilities(Experimental("""{"sharing":"isolated"}"""));

        Assert.NotSame(first, second);

        var parent = new System.Text.Json.Nodes.JsonObject { ["experimental"] = first };
        Assert.Equal(first, parent["experimental"]);
        // Attaching `second` to a different parent must not throw despite both
        // representing the same logical value.
        var otherParent = new System.Text.Json.Nodes.JsonObject { ["experimental"] = second };
        Assert.Equal(second, otherParent["experimental"]);
    }
}
