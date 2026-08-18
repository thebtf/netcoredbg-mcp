using System.Text;
using System.Text.Json.Nodes;
using NJsonSchema;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

public sealed class NativeSceneNegativeWireTests
{
    private const string ActiveProtocolVersion = "native-scene-probe/1";
    private const string ActiveSchemaVersion = "native-scene-probe.schema/1";
    private const string InvalidToolArguments = "INVALID_TOOL_ARGUMENTS";
    private const string UnsupportedProtocol = "UNSUPPORTED_PROTOCOL";

    [Theory]
    [InlineData("native-scene-probe/0", ActiveSchemaVersion)]
    [InlineData("native-scene-probe/x", ActiveSchemaVersion)]
    [InlineData(ActiveProtocolVersion, "native-scene-probe.schema/0")]
    [InlineData(ActiveProtocolVersion, "native-scene-probe.schema/x")]
    public void MalformedVersionSyntax_IsInvalidToolArguments(string protocolVersion, string schemaVersion)
    {
        var result = NativeSceneContractCatalogDriver.Load().ClassifyVersions(protocolVersion, schemaVersion);

        AssertRejected(result, InvalidToolArguments);
    }

    [Theory]
    [InlineData("native-scene-probe/2", ActiveSchemaVersion)]
    [InlineData(ActiveProtocolVersion, "native-scene-probe.schema/2")]
    [InlineData("native-scene-probe/2", "native-scene-probe.schema/2")]
    public void RecognizedButInactiveVersions_AreUnsupported(string protocolVersion, string schemaVersion)
    {
        var result = NativeSceneContractCatalogDriver.Load().ClassifyVersions(protocolVersion, schemaVersion);

        AssertRejected(result, UnsupportedProtocol);
    }

    [Fact]
    public void ActiveVersionPair_IsAccepted()
    {
        var result = NativeSceneContractCatalogDriver.Load().ClassifyVersions(ActiveProtocolVersion, ActiveSchemaVersion);

        Assert.True(result.IsValid);
        Assert.Null(result.Code);
    }

    [Theory]
    [InlineData("supportedProtocolVersions", ActiveProtocolVersion, false)]
    [InlineData("supportedProtocolVersions", ActiveProtocolVersion, true)]
    [InlineData("supportedSchemaVersions", ActiveSchemaVersion, false)]
    [InlineData("supportedSchemaVersions", ActiveSchemaVersion, true)]
    public void CapabilityDeclarations_RejectOmittedOrDuplicateActiveVersions(
        string member,
        string activeVersion,
        bool duplicate)
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var response = ValidCapabilityResponse(catalog);
        var versions = new JsonArray();
        if (duplicate)
        {
            versions.Add(activeVersion);
            versions.Add(activeVersion);
        }

        response["capabilities"]!.AsObject()[member] = versions;

        AssertInvalidResult(catalog, "get_ui_probe_capabilities", response);
    }

    [Fact]
    public void CapabilityCandidateWithMalformedCapturedAt_IsInvalidToolArguments()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var response = ValidCapabilityResponse(catalog);
        response["candidate"]!.AsObject()["capturedAt"] = "not-an-rfc3339-date-time";

        AssertInvalidResult(catalog, "get_ui_probe_capabilities", response);
    }

    [Fact]
    public void ClosedRequestObject_IsInvalidToolArguments()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var request = Request(catalog, "C018-sample-count-above-bound-is-invalid");
        request["sceneRequest"]!.AsObject()["settlePolicy"]!.AsObject()["sampleCount"] = 2;
        request["unexpected"] = true;

        AssertInvalidRequest(catalog, "wait_for_ui_stable", request);
    }

    [Fact]
    public void InvalidArtifactRange_IsInvalidToolArguments()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var request = Request(catalog, "C017-short-artifact-capability-is-invalid");
        request["artifactId"] = "artifact_capability_000001";
        request["maxBytes"] = 65_537;

        AssertInvalidRequest(catalog, "read_capture_artifact", request);
    }

    [Fact]
    public void InvalidArtifactIdentifier_IsInvalidToolArguments()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var request = Request(catalog, "C017-short-artifact-capability-is-invalid");
        request["artifactId"] = "short";

        AssertInvalidRequest(catalog, "read_capture_artifact", request);
    }

    [Fact]
    public void VisualCapture_RejectsNullEvidenceScope()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var response = ValidVisualResponse(catalog);
        response["evidenceScope"] = null;

        AssertInvalidResult(catalog, "capture_visual_evidence", response);
    }

    [Fact]
    public void NativeSceneCapture_RejectsNonNullEvidenceScope()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var response = ValidNativeSceneResponse(catalog);
        response["evidenceScope"] = new JsonObject { ["kind"] = "window" };

        AssertInvalidResult(catalog, "capture_native_scene", response);
    }

    [Fact]
    public void ToolError_RejectsWrongToolErrorCodePair()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var response = new JsonObject
        {
            ["kind"] = "tool_error",
            ["tool"] = "get_ui_probe_capabilities",
            ["code"] = "ARTIFACT_INTEGRITY_FAILED",
            ["message"] = "Not permitted for this primitive.",
        };

        AssertCatalogRejectsInvalidResult(catalog, "get_ui_probe_capabilities", response);
    }

    [Theory]
    [InlineData("application/vnd.netcoredbg.native-scene+json", 16_777_217)]
    [InlineData("image/png", 67_108_865)]
    public void ArtifactChunk_RejectsMediaTypeSpecificByteCeiling(string mediaType, int byteLength)
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var response = ArtifactChunk(mediaType, byteLength);

        AssertCatalogRejectsInvalidResult(catalog, "read_capture_artifact", response);
    }

    [Theory]
    [InlineData(1)]
    [InlineData(17)]
    public void RequestSampleCountOutsideInclusiveRange_IsInvalidToolArguments(int sampleCount)
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var request = Request(catalog, "C018-sample-count-above-bound-is-invalid");
        request["sceneRequest"]!.AsObject()["settlePolicy"]!.AsObject()["sampleCount"] = sampleCount;

        AssertInvalidRequest(catalog, "wait_for_ui_stable", request);
    }

    [Theory]
    [InlineData("settleSampleCountMin", 1)]
    [InlineData("settleSampleCountMax", 15)]
    public void CapabilityDeclaredSampleBounds_MustBeExactlyTwoAndSixteen(string member, int value)
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var response = ValidCapabilityResponse(catalog);
        response["capabilities"]!.AsObject()["limits"]!.AsObject()[member] = value;

        AssertCatalogRejectsInvalidResult(catalog, "get_ui_probe_capabilities", response);
    }

    [Fact]
    public void RequestDepthSeventeen_IsInvalidToolArgumentsEvenThoughDraft7ShapeIsValid()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var request = Request(catalog, "C019-depth-seventeen-json-input-is-invalid");

        AssertSchemaAccepts(ProbeSchema(catalog), request);
        AssertRejected(catalog.ValidateRequest("capture_native_scene", request.ToJsonString()), InvalidToolArguments);
    }

    [Fact]
    public void RequestWithMoreThanTwoHundredFiftySixMembers_IsInvalidToolArguments()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var request = Request(catalog, "C018-sample-count-above-bound-is-invalid");
        request["sceneRequest"]!.AsObject()["settlePolicy"]!.AsObject()["sampleCount"] = 2;
        var members = new JsonObject();
        for (var index = 0; index < 257; index++)
        {
            members[$"member{index:D3}"] = index;
        }

        request["sceneRequest"]!.AsObject()["currentState"] = members;

        AssertInvalidRequest(catalog, "wait_for_ui_stable", request);
    }

    [Fact]
    public void CustomPayloadOfTwoHundredSixtyTwoThousandOneHundredFortyFiveBytes_IsRecordedWithoutSchemaEscapeHatches()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var payload = CustomPayload();
        var evidence = new JsonObject
        {
            ["namespace"] = "example.waveform",
            ["schemaVersion"] = "1",
            ["authority"] = "adapter_reported",
            ["payload"] = payload,
        };

        Assert.Equal(262_145, Encoding.UTF8.GetByteCount(payload.ToJsonString()));
        AssertSchemaAccepts(ArtifactDefinitionSchema(catalog, "customAdapterEvidence"), evidence);

        var corpus = catalog.ValidateCorpus();
        Assert.True(corpus.IsValid);
        Assert.Null(corpus.Code);
    }

    [Theory]
    [InlineData("omitted")]
    [InlineData("duplicate")]
    [InlineData("cross-milestone")]
    public void CapabilityPrimitives_RejectOmittedDuplicateAndCrossMilestonePairs(string mutation)
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var response = ValidCapabilityResponse(catalog);
        var primitives = response["capabilities"]!.AsObject()["primitives"]!.AsArray();

        switch (mutation)
        {
            case "omitted":
                primitives.RemoveAt(5);
                break;
            case "duplicate":
                primitives[5] = primitives[3]!.DeepClone();
                break;
            case "cross-milestone":
                primitives[5]!.AsObject()["milestone"] = "M0";
                break;
            default:
                throw new ArgumentOutOfRangeException(nameof(mutation), mutation, null);
        }

        AssertCatalogRejectsInvalidResult(catalog, "get_ui_probe_capabilities", response);
    }

    private static void AssertInvalidRequest(NativeSceneContractCatalogDriver catalog, string tool, JsonObject request)
    {
        AssertSchemaRejects(ProbeSchema(catalog), request);
        AssertRejected(catalog.ValidateRequest(tool, request.ToJsonString()), InvalidToolArguments);
    }

    private static void AssertInvalidResult(NativeSceneContractCatalogDriver catalog, string tool, JsonObject response)
    {
        AssertSchemaRejects(ProbeSchema(catalog), response);
        AssertRejected(catalog.ValidateResult(tool, response.ToJsonString()), InvalidToolArguments);
    }

    private static void AssertCatalogRejectsInvalidResult(NativeSceneContractCatalogDriver catalog, string tool, JsonObject response) =>
        AssertRejected(catalog.ValidateResult(tool, response.ToJsonString()), InvalidToolArguments);

    private static void AssertRejected(NativeSceneContractCatalogValidationResult result, string code)
    {
        Assert.False(result.IsValid);
        Assert.Equal(code, result.Code);
    }

    private static void AssertSchemaAccepts(JsonSchema schema, JsonObject value) =>
        Assert.Empty(schema.Validate(value.ToJsonString()));

    private static void AssertSchemaRejects(JsonSchema schema, JsonObject value) =>
        Assert.NotEmpty(schema.Validate(value.ToJsonString()));

    private static JsonSchema ProbeSchema(NativeSceneContractCatalogDriver catalog) =>
        JsonSchema.FromJsonAsync(ArtifactText(catalog, "native-scene-probe.schema.json")).GetAwaiter().GetResult();

    private static JsonSchema ArtifactDefinitionSchema(NativeSceneContractCatalogDriver catalog, string definition)
    {
        var artifactSchema = JsonSchema.FromJsonAsync(ArtifactText(catalog, "native-scene-artifact.schema.json"))
            .GetAwaiter()
            .GetResult();

        return artifactSchema.Definitions[definition].ActualSchema;
    }

    private static JsonObject Request(NativeSceneContractCatalogDriver catalog, string caseId) =>
        Case(catalog, caseId)["request"]!.DeepClone().AsObject();

    private static JsonObject ValidCapabilityResponse(NativeSceneContractCatalogDriver catalog)
    {
        var capabilities = VariantResponse(
            catalog,
            "C021-capability-omission-is-schema-rejected",
            "capture_native_scene_with_m0_milestone");
        capabilities["primitives"]!.AsArray()[5]!.AsObject()["milestone"] = "M1";
        var response = new JsonObject
        {
            ["kind"] = "ui_probe_capabilities",
            ["protocolVersion"] = ActiveProtocolVersion,
            ["schemaVersion"] = ActiveSchemaVersion,
            ["candidate"] = VariantResponse(
                catalog,
                "C005-valid-lossless-visual-capture",
                "capture_manifest_without_scene_request")["candidate"]!.DeepClone(),
            ["capabilities"] = capabilities,
        };

        AssertSchemaAccepts(ProbeSchema(catalog), response);
        return response;
    }

    private static JsonObject ValidVisualResponse(NativeSceneContractCatalogDriver catalog)
    {
        var response = VariantResponse(catalog, "C005-valid-lossless-visual-capture", "capture_manifest_without_scene_request");
        response["sceneRequest"] = FixtureSceneRequest(catalog);

        AssertSchemaAccepts(ProbeSchema(catalog), response);
        return response;
    }

    private static JsonObject ValidNativeSceneResponse(NativeSceneContractCatalogDriver catalog)
    {
        var response = VariantResponse(catalog, "C011-atomic-in-process-scene", "complete_without_native_scene_artifact");
        response["status"] = "PARTIAL";

        AssertSchemaAccepts(ProbeSchema(catalog), response);
        return response;
    }

    private static JsonObject ArtifactChunk(string mediaType, int byteLength) =>
        new()
        {
            ["kind"] = "capture_artifact_chunk",
            ["artifactId"] = "artifact_chunk_capability",
            ["offset"] = 0,
            ["bytesRead"] = 0,
            ["dataBase64"] = string.Empty,
            ["endOfArtifact"] = true,
            ["mediaType"] = mediaType,
            ["byteLength"] = byteLength,
            ["sha256"] = new string('a', 64),
            ["artifactSchemaVersion"] = "native-scene-artifact/1",
        };

    private static JsonObject CustomPayload()
    {
        var payload = new JsonObject();
        for (var group = 0; group < 4; group++)
        {
            var values = new JsonArray();
            for (var index = 0; index < 256; index++)
            {
                var length = group == 3 && index < 12
                    ? 1
                    : group == 3 && index == 12
                        ? 216
                        : 256;
                values.Add(new string('x', length));
            }

            payload[$"p{group}"] = values;
        }

        return payload;
    }

    private static JsonObject FixtureSceneRequest(NativeSceneContractCatalogDriver catalog) =>
        Corpus(catalog)["fixtures"]!["sceneRequest"]!.DeepClone().AsObject();

    private static JsonObject VariantResponse(NativeSceneContractCatalogDriver catalog, string caseId, string name) =>
        Case(catalog, caseId)["schemaInvalidVariants"]!.AsArray()
            .Single(variant => variant!["name"]?.GetValue<string>() == name)!["response"]!.DeepClone().AsObject();

    private static JsonObject Case(NativeSceneContractCatalogDriver catalog, string caseId) =>
        Corpus(catalog)["cases"]!.AsArray()
            .Single(@case => @case!["id"]?.GetValue<string>() == caseId)!.AsObject();

    private static JsonObject Corpus(NativeSceneContractCatalogDriver catalog) =>
        JsonNode.Parse(ArtifactText(catalog, "parity-corpus.json"))!.AsObject();

    private static string ArtifactText(NativeSceneContractCatalogDriver catalog, string fileName) =>
        Encoding.UTF8.GetString(catalog.GetArtifactBytes(fileName));
}
