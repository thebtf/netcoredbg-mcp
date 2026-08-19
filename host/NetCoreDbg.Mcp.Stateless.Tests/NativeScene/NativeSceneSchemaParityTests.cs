using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using System.Security.Cryptography;
using System.Text.Json.Nodes;
using NJsonSchema;
using NJsonSchema.Validation;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

public sealed class NativeSceneSchemaParityTests
{
    private const string ActiveProtocolVersion = "native-scene-probe/1";
    private const string ActiveSchemaVersion = "native-scene-probe.schema/1";
    private const string ActiveArtifactSchemaVersion = "native-scene-artifact/1";

    private static readonly ContractArtifact[] ApprovedArtifacts =
    [
        new("native-scene-probe.schema.json", "f446166f9a1062d3e1a2190327d06c04905e76a1c1f81af16c87572394f90022"),
        new("native-scene-artifact.schema.json", "07c257c9b5f75c01aa4f4141968c789b045d7c831575343df429075c732f7668"),
        new("parity-corpus.json", "90c24f8f9706c207ca3ecf8dee93d1937c16a6be45feac65d812e48853bc4621"),
    ];

    [Fact]
    public void ApprovedArtifactsHaveExactHashesAndCatalogReturnsIdenticalBytes()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();

        foreach (var artifact in ApprovedArtifacts)
        {
            var expectedBytes = ReadArtifactBytes(artifact.FileName);

            Assert.Equal(artifact.Sha256, Convert.ToHexString(SHA256.HashData(expectedBytes)).ToLowerInvariant());
            Assert.Equal(expectedBytes, catalog.GetArtifactBytes(artifact.FileName));
            Assert.Equal(artifact.Sha256, catalog.GetArtifactSha256(artifact.FileName));
        }
    }

    [Fact]
    public void CatalogArtifactBytesAreMutationIsolated()
    {
        var artifact = ApprovedArtifacts[0];
        var expectedBytes = ReadArtifactBytes(artifact.FileName);
        var catalog = NativeSceneContractCatalogDriver.Load();
        var exposedBytes = catalog.GetRawArtifactBytes(artifact.FileName);

        Assert.True(MemoryMarshal.TryGetArray(exposedBytes, out ArraySegment<byte> exposedSegment));
        Assert.NotNull(exposedSegment.Array);
        Assert.True(exposedSegment.Count > 0);

        var array = exposedSegment.Array!;
        var index = exposedSegment.Offset;
        var originalByte = array[index];
        try
        {
            array[index] ^= byte.MaxValue;

            Assert.Equal(expectedBytes, catalog.GetArtifactBytes(artifact.FileName));
            Assert.Equal(artifact.Sha256, catalog.GetArtifactSha256(artifact.FileName));
        }
        finally
        {
            array[index] = originalByte;
        }
    }

    [Fact]
    public async Task SchemasParseAsDraft7AndResolveEveryInternalReference()
    {
        foreach (var fileName in new[] { "native-scene-probe.schema.json", "native-scene-artifact.schema.json" })
        {
            var source = ReadArtifactText(fileName);
            var document = ParseObject(source);
            var schema = await JsonSchema.FromJsonAsync(source);
            var definitions = Object(document["definitions"]);

            Assert.Equal("http://json-schema.org/draft-07/schema#", Text(document["$schema"]));
            Assert.Equal(definitions.Count, schema.Definitions.Count);
            Assert.All(schema.Definitions.Values, definition => Assert.NotNull(definition.ActualSchema));

            foreach (var reference in InternalReferenceNames(document).Distinct(StringComparer.Ordinal))
            {
                Assert.True(schema.Definitions.ContainsKey(reference), $"{fileName} has an unresolved internal reference '#/definitions/{reference}'.");
            }
        }
    }

    [Fact]
    public void ContractRootsAreClosed()
    {
        var probe = ParseArtifact("native-scene-probe.schema.json");
        var artifact = ParseArtifact("native-scene-artifact.schema.json");

        Assert.All(
            new[]
            {
                "getUiProbeCapabilitiesArguments",
                "captureVisualEvidenceArguments",
                "readCaptureArtifactArguments",
                "waitForUiStableArguments",
                "captureElementSnapshotArguments",
                "captureNativeSceneArguments",
                "probeCapabilities",
                "uiProbeCapabilities",
                "captureArtifactChunk",
                "toolErrorBase",
                "captureManifestBase",
            },
            definition => AssertClosed(Definition(probe, definition)));
        AssertClosed(Definition(artifact, "sceneArtifact"));
    }

    [Fact]
    public void CorpusHasContiguousCasesAndRequiredMetadataVocabulary()
    {
        var corpus = ParseArtifact("parity-corpus.json");
        var contract = Object(corpus["contract"]);
        var cases = Array(corpus["cases"]).Select(Object).ToArray();
        var expectedClassifications = new HashSet<string>(StringComparer.Ordinal)
        {
            "artifact_chunk",
            "capability_declaration",
            "complete_observation",
            "qualified_observation",
            "schema_rejection",
            "typed_error",
        };
        var expectedGateVocabularies = new HashSet<string>(StringComparer.Ordinal)
        {
            "schema_fixture_and_expected_classification_vocabulary_only",
            "negative_version_fixture_and_expected_classification_vocabulary",
            "negative_request_schema_fixture_and_expected_classification_vocabulary",
            "negative_response_schema_fixture_and_expected_classification_vocabulary",
        };

        Assert.Equal(ActiveProtocolVersion, Text(contract["protocolVersion"]));
        Assert.Equal(ActiveSchemaVersion, Text(contract["schemaVersion"]));
        Assert.Equal(ActiveArtifactSchemaVersion, Text(contract["artifactSchemaVersion"]));
        Assert.Equal(24, cases.Length);
        Assert.Equal(
            Enumerable.Range(1, 24),
            cases.Select(caseEntry => int.Parse(Text(caseEntry["id"])[1..4], System.Globalization.CultureInfo.InvariantCulture)).Order());
        Assert.Equal(24, cases.Select(caseEntry => Text(caseEntry["id"])).Distinct(StringComparer.Ordinal).Count());

        foreach (var caseEntry in cases)
        {
            Assert.Matches("^C[0-9]{3}-", Text(caseEntry["id"]));
            Assert.Contains(Text(caseEntry["contractGateExpectation"]), expectedGateVocabularies);
            _ = Boolean(caseEntry["runtimeBehaviorRequired"]);
            Assert.False(string.IsNullOrWhiteSpace(Text(caseEntry["primitive"])));

            var expected = Object(caseEntry["expected"]);
            Assert.Contains(Text(expected["classification"]), expectedClassifications);
        }

        var gate = Object(Object(corpus["gateStages"])["M0-G0"]);
        Assert.Equal("T007", Text(gate["task"]));
        Assert.Equal(
            new[]
            {
                "exact-byte Draft-7 schema loading",
                "request/result validator parity for concrete fixtures present",
                "internal reference resolution",
                "corpus syntax and integrity",
                "expected classification vocabulary",
                "negative structural and version cases",
            },
            Array(gate["validates"]).Select(Text));
    }

    [Fact]
    public void CapabilityDeclarationFixesActiveVersionsSampleBoundsAndPrimitiveMilestones()
    {
        var probe = ParseArtifact("native-scene-probe.schema.json");
        var capabilities = Definition(probe, "probeCapabilities");
        var properties = Object(capabilities["properties"]);
        var protocolVersions = Object(properties["supportedProtocolVersions"]);
        var schemaVersions = Object(properties["supportedSchemaVersions"]);
        var limits = Object(properties["limits"]);
        var limitProperties = Object(Definition(probe, Text(limits["$ref"])["#/definitions/".Length..])["properties"]);
        var primitivePairs = Array(Definition(probe, "primitiveCapability")["oneOf"])
            .Select(Object)
            .Select(branch => Object(branch["properties"]))
            .Select(properties => (Name: EnumValue(Object(properties["name"])), Milestone: EnumValue(Object(properties["milestone"]))))
            .OrderBy(pair => pair.Name, StringComparer.Ordinal)
            .ToArray();

        Assert.Equal(1, Integer(protocolVersions["minItems"]));
        Assert.True(Boolean(protocolVersions["uniqueItems"]));
        Assert.Equal(ActiveProtocolVersion, Text(Object(protocolVersions["contains"])["const"]));
        Assert.Equal(1, Integer(schemaVersions["minItems"]));
        Assert.True(Boolean(schemaVersions["uniqueItems"]));
        Assert.Equal(ActiveSchemaVersion, Text(Object(schemaVersions["contains"])["const"]));
        Assert.Equal(2, Integer(Object(limitProperties["settleSampleCountMin"])["const"]));
        Assert.Equal(16, Integer(Object(limitProperties["settleSampleCountMax"])["const"]));
        Assert.Equal(
            new[]
            {
                ("capture_element_snapshot", "M1"),
                ("capture_native_scene", "M1"),
                ("capture_visual_evidence", "M0"),
                ("get_ui_probe_capabilities", "M0"),
                ("read_capture_artifact", "M0"),
                ("wait_for_ui_stable", "M1"),
            },
            primitivePairs);
    }

    [Fact]
    public void ContractBranchesRequireSceneScopeAndBoundedCaptureMetadata()
    {
        var probe = ParseArtifact("native-scene-probe.schema.json");
        var sceneRequest = Definition(probe, "sceneRequest");
        var sceneProperties = Object(sceneRequest["properties"]);
        var evidenceScope = Definition(probe, "evidenceScope");
        var evidenceBranches = Array(evidenceScope["oneOf"]).Select(Object).ToArray();
        var visualCapture = Definition(probe, "visualEvidenceCapture");
        var elementCapture = Definition(probe, "elementSnapshotCapture");
        var nativeCapture = Definition(probe, "nativeSceneCapture");
        var chunk = Definition(probe, "captureArtifactChunk");

        AssertClosed(sceneRequest);
        Assert.Equal(
            new[]
            {
                "animationPolicy", "appearance", "contractSetHash", "contrast", "currentState", "density", "expectedCandidateIdentity",
                "expectedDpiPolicy", "fixtureId", "focusTarget", "sceneId", "scope", "scrollOffsets", "selectedState", "settlePolicy",
                "storyId", "theme", "viewport",
            },
            Array(sceneRequest["required"]).Select(Text).Order());
        Assert.Equal(new[] { "kind" }, Array(evidenceBranches[0]["required"]).Select(Text));
        Assert.Equal("window", EnumValue(Object(Object(evidenceBranches[0]["properties"])["kind"])));
        Assert.Equal(new[] { "element", "kind" }, Array(evidenceBranches[1]["required"]).Select(Text).Order());
        Assert.Equal("element", EnumValue(Object(Object(evidenceBranches[1]["properties"])["kind"])));

        var visualProperties = Object(Object(Array(visualCapture["allOf"])[1])["properties"]);
        var elementProperties = Object(Object(Array(elementCapture["allOf"])[1])["properties"]);
        Assert.Equal("#/definitions/evidenceScope", Text(Object(visualProperties["evidenceScope"])["$ref"]));
        Assert.Null(Array(Object(elementProperties["evidenceScope"])["enum"])[0]);
        Assert.Equal("#/definitions/elementSelector", Text(Object(elementProperties["element"])["$ref"]));
        Assert.Null(Array(Object(visualProperties["element"])["enum"])[0]);

        var elementComplete = Object(Array(Object(Array(elementCapture["allOf"])[3])["oneOf"])[0]);
        var elementDescriptor = Object(Object(Object(elementComplete["properties"])["artifacts"])["contains"]);
        var elementDescriptorProperties = Object(elementDescriptor["properties"]);
        Assert.Equal("COMPLETE", EnumValue(Object(Object(elementComplete["properties"])["status"])));
        Assert.Equal("application/vnd.netcoredbg.native-scene+json", EnumValue(Object(elementDescriptorProperties["mediaType"])));
        Assert.Equal("observed_facts", EnumValue(Object(elementDescriptorProperties["evidenceGrade"])));

        var nativeProperties = Object(Object(Array(nativeCapture["allOf"])[1])["properties"]);
        var partialNative = Object(Array(Object(Array(nativeCapture["allOf"])[3])["oneOf"])[1]);
        var atomicityBranches = Array(Object(nativeProperties["atomicity"])["oneOf"]).Select(Object).Select(branch => Text(branch["$ref"])).Order().ToArray();
        var uiaPartialThen = Object(Object(Array(nativeCapture["allOf"])[4])["then"]);
        var uiaAtomicityRules = Object(Array(Object(Object(uiaPartialThen["properties"])["atomicity"])["allOf"])[1]);
        var guardProperties = Object(uiaAtomicityRules["properties"]);
        var guardStates = Object(Object(guardProperties["guards"])["properties"]);
        var uiaIssue = Object(Object(Object(uiaPartialThen["properties"])["issues"])["contains"]);

        Assert.Equal("PARTIAL", EnumValue(Object(Object(partialNative["properties"])["status"])));
        Assert.Equal(new[] { "#/definitions/inProcessAtomicity", "#/definitions/uiaGuardedAtomicity" }, atomicityBranches);
        Assert.Equal("unchanged", EnumValue(Object(Object(Object(guardStates["window"])["properties"])["state"])));
        Assert.Equal("unchanged", EnumValue(Object(Object(Object(guardStates["client"])["properties"])["state"])));
        Assert.Equal("unchanged", EnumValue(Object(Object(Object(guardStates["dpi"])["properties"])["state"])));
        Assert.Equal("unchanged", EnumValue(Object(Object(Object(guardStates["visualTreeFingerprint"])["properties"])["state"])));
        Assert.Equal("ATOMICITY_UNPROVEN_UIA_GUARDED", EnumValue(Object(Object(uiaIssue["properties"])["code"])));

        var chunkProperties = Object(chunk["properties"]);
        var nativeChunkLimit = Object(Object(Array(chunk["allOf"])[0])["then"]);
        Assert.Equal(67_108_864, Integer(Object(chunkProperties["byteLength"])["maximum"]));
        Assert.Equal(16_777_216, Integer(Object(Object(nativeChunkLimit["properties"])["byteLength"])["maximum"]));
    }

    [Fact]
    public async Task CatalogAgreesWithIndependentOracleOnConcreteValidFixtures()
    {
        var probeSource = ReadArtifactText("native-scene-probe.schema.json");
        var probeSchema = await JsonSchema.FromJsonAsync(probeSource);
        var corpus = ParseArtifact("parity-corpus.json");
        var request = Case(corpus, "C005-valid-lossless-visual-capture")["request"];
        var result = CreateValidCapabilityResult();

        Assert.Empty(new JsonSchemaValidator().Validate(request!.ToJsonString(), probeSchema));
        Assert.Empty(new JsonSchemaValidator().Validate(result.ToJsonString(), probeSchema));

        var catalog = NativeSceneContractCatalogDriver.Load();
        AssertValid(catalog.ValidateRequest("capture_visual_evidence", request.ToJsonString()));
        AssertValid(catalog.ValidateResult("get_ui_probe_capabilities", result.ToJsonString()));
        AssertValid(catalog.ClassifyVersions(ActiveProtocolVersion, ActiveSchemaVersion));
        AssertValid(catalog.ValidateCorpus());
    }

    [Fact]
    public async Task OpaqueJsonNumberOutsideDecimalRangeRemainsSchemaValid()
    {
        var schema = await JsonSchema.FromJsonAsync(ReadArtifactText("native-scene-probe.schema.json"));
        var request = Case(ParseArtifact("parity-corpus.json"), "C005-valid-lossless-visual-capture")["request"]!.DeepClone().AsObject();
        request["sceneRequest"]!.AsObject()["currentState"] = JsonNode.Parse("1e100");

        Assert.Empty(schema.Validate(request.ToJsonString()));
        AssertValid(NativeSceneContractCatalogDriver.Load().ValidateRequest("capture_visual_evidence", request.ToJsonString()));
    }

    private static void AssertValid(NativeSceneContractCatalogValidationResult result)
    {
        Assert.True(result.IsValid, $"Expected valid catalog result, got {result.Code ?? "<null>"}: {result.Message ?? "<null>"}.");
    }

    private static JsonObject CreateValidCapabilityResult() => new()
    {
        ["kind"] = "ui_probe_capabilities",
        ["protocolVersion"] = ActiveProtocolVersion,
        ["schemaVersion"] = ActiveSchemaVersion,
        ["candidate"] = new JsonObject
        {
            ["processId"] = 4242,
            ["processIdentity"] = "process_4242_start_1",
            ["hwnd"] = null,
            ["executableSha256"] = new string('b', 64),
            ["assemblyVersion"] = "1.0.0",
            ["probeVersion"] = "1.0.0",
            ["observerVersions"] = new JsonArray(),
            ["contractSetHash"] = new string('a', 64),
            ["storyHash"] = null,
            ["capturedAt"] = "2026-08-18T00:00:00Z",
            ["source"] = new JsonObject
            {
                ["kind"] = "probe_manifest",
                ["verification"] = "verified",
            },
        },
        ["capabilities"] = new JsonObject
        {
            ["supportedProtocolVersions"] = new JsonArray(ActiveProtocolVersion),
            ["supportedSchemaVersions"] = new JsonArray(ActiveSchemaVersion),
            ["primitives"] = new JsonArray(
                Primitive("get_ui_probe_capabilities", "M0", "supported"),
                Primitive("capture_visual_evidence", "M0", "supported"),
                Primitive("read_capture_artifact", "M0", "supported"),
                Primitive("wait_for_ui_stable", "M1", "unsupported"),
                Primitive("capture_element_snapshot", "M1", "unsupported"),
                Primitive("capture_native_scene", "M1", "unsupported")),
            ["context"] = CapabilityMap(
                "storyId", "sceneId", "fixtureId", "scope", "appearance", "theme", "density", "contrast", "viewport", "expectedDpiPolicy",
                "focusTarget", "selectedState", "currentState", "scrollOffsets", "animationPolicy"),
            ["settleConditions"] = CapabilityMap(
                "dispatcherIdle", "stableLayout", "animationState", "windowGeometry", "contextMaterialization", "asyncLoadSettled"),
            ["atomicSceneAuthority"] = "unsupported",
            ["uiaGuardedTraversal"] = "unsupported",
            ["losslessVisualEvidence"] = "supported",
            ["customAdapterNamespaces"] = new JsonArray(),
            ["limits"] = new JsonObject
            {
                ["artifactReadMaxBytes"] = 65_536,
                ["losslessArtifactMaxBytes"] = 67_108_864,
                ["sceneArtifactMaxBytes"] = 16_777_216,
                ["structuredResponseMaxBytes"] = 262_144,
                ["sceneGraphMaxNodes"] = 4_096,
                ["issuesMaxCount"] = 256,
                ["artifactRefsMaxCount"] = 4,
                ["retentionMaxSeconds"] = 14_400,
                ["settleTimeoutMaxMs"] = 30_000,
                ["settleSampleCountMin"] = 2,
                ["settleSampleCountMax"] = 16,
            },
        },
    };

    private static JsonObject Primitive(string name, string milestone, string availability) => new()
    {
        ["name"] = name,
        ["milestone"] = milestone,
        ["availability"] = availability,
    };

    private static JsonObject CapabilityMap(params string[] names)
    {
        var capabilities = new JsonObject();
        foreach (var name in names)
        {
            capabilities[name] = "supported";
        }

        return capabilities;
    }

    private static JsonObject Case(JsonObject corpus, string id) => Assert.Single(
        Array(corpus["cases"]).Select(Object),
        caseEntry => Text(caseEntry["id"]) == id);

    private static JsonObject ParseArtifact(string fileName) => ParseObject(ReadArtifactText(fileName));

    private static JsonObject ParseObject(string json) => Object(JsonNode.Parse(json));

    private static byte[] ReadArtifactBytes(string fileName) => File.ReadAllBytes(ArtifactPath(fileName));

    private static string ReadArtifactText(string fileName) => File.ReadAllText(ArtifactPath(fileName));

    private static string ArtifactPath(string fileName) => Path.Combine(
        RepositoryLayout.Root,
        "specs",
        "004-native-scene-probe",
        "contracts",
        fileName);

    private static JsonObject Definition(JsonObject schema, string name) => Object(Object(schema["definitions"])[name]);

    private static IEnumerable<string> InternalReferenceNames(JsonNode? node)
    {
        if (node is JsonObject objectNode)
        {
            if (objectNode["$ref"] is JsonValue referenceValue)
            {
                var reference = referenceValue.GetValue<string>();
                if (reference.StartsWith("#/definitions/", StringComparison.Ordinal))
                {
                    yield return reference["#/definitions/".Length..];
                }
            }

            foreach (var property in objectNode)
            {
                foreach (var reference in InternalReferenceNames(property.Value))
                {
                    yield return reference;
                }
            }
        }
        else if (node is JsonArray array)
        {
            foreach (var item in array)
            {
                foreach (var reference in InternalReferenceNames(item))
                {
                    yield return reference;
                }
            }
        }
    }

    private static void AssertClosed(JsonObject schema)
    {
        Assert.False(Boolean(schema["additionalProperties"]));
    }

    private static string EnumValue(JsonObject schema) => Text(Array(schema["enum"])[0]);

    private static JsonObject Object(JsonNode? node) => Assert.IsType<JsonObject>(node);

    private static JsonArray Array(JsonNode? node) => Assert.IsType<JsonArray>(node);

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();

    private static int Integer(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<int>();

    private static bool Boolean(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<bool>();

    private sealed record ContractArtifact(string FileName, string Sha256);
}

internal sealed record NativeSceneContractCatalogValidationResult(bool IsValid, string? Code, string? Message);

internal sealed class NativeSceneContractCatalogDriver
{
    private const string ProductionAssemblyName = "NetCoreDbg.Mcp.Stateless";
    private const string CatalogTypeName = "NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneContractCatalog";

    private readonly MethodInfo _getArtifactBytes;
    private readonly MethodInfo _getArtifactSha256;
    private readonly MethodInfo _validateRequest;
    private readonly MethodInfo _validateResult;
    private readonly MethodInfo _classifyVersions;
    private readonly MethodInfo _validateCorpus;

    private NativeSceneContractCatalogDriver(Type catalogType)
    {
        _getArtifactBytes = RequireStaticMethod(catalogType, "GetArtifactBytes", typeof(ReadOnlyMemory<byte>), typeof(string));
        _getArtifactSha256 = RequireStaticMethod(catalogType, "GetArtifactSha256", typeof(string), typeof(string));
        _validateRequest = RequireStaticMethod(catalogType, "ValidateRequest", returnType: null, typeof(string), typeof(string));
        _validateResult = RequireStaticMethod(catalogType, "ValidateResult", returnType: null, typeof(string), typeof(string));
        _classifyVersions = RequireStaticMethod(catalogType, "ClassifyVersions", returnType: null, typeof(string), typeof(string));
        _validateCorpus = RequireStaticMethod(catalogType, "ValidateCorpus", returnType: null);

        Assert.Equal(_validateRequest.ReturnType, _validateResult.ReturnType);
        Assert.Equal(_validateRequest.ReturnType, _classifyVersions.ReturnType);
        Assert.Equal(_validateRequest.ReturnType, _validateCorpus.ReturnType);
        RequireValidationResultShape(_validateRequest.ReturnType);
    }

    public static NativeSceneContractCatalogDriver Load()
    {
        var productionProject = Path.Combine(RepositoryLayout.Root, "host", ProductionAssemblyName, $"{ProductionAssemblyName}.csproj");
        Assert.True(File.Exists(productionProject), $"Missing production project: '{productionProject}'.");

        var productionAssembly = TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root,
            Path.Combine("host", ProductionAssemblyName),
            ProductionAssemblyName);
        var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(productionAssembly);
        var catalogType = assembly.GetType(CatalogTypeName, throwOnError: false)
            ?? throw new InvalidOperationException($"Missing production contract: type '{CatalogTypeName}' is absent from '{assembly.Location}'. T006 must implement it without changing this suite.");
        return new NativeSceneContractCatalogDriver(catalogType);
    }

    public ReadOnlyMemory<byte> GetRawArtifactBytes(string fileName) => Assert.IsType<ReadOnlyMemory<byte>>(
        _getArtifactBytes.Invoke(null, new object?[] { fileName }));

    public byte[] GetArtifactBytes(string fileName) => GetRawArtifactBytes(fileName).ToArray();

    public string GetArtifactSha256(string fileName) => Assert.IsType<string>(
        _getArtifactSha256.Invoke(null, new object?[] { fileName }));

    public NativeSceneContractCatalogValidationResult ValidateRequest(string tool, string json) => InvokeValidation(_validateRequest, tool, json);

    public NativeSceneContractCatalogValidationResult ValidateResult(string tool, string json) => InvokeValidation(_validateResult, tool, json);

    public NativeSceneContractCatalogValidationResult ClassifyVersions(string protocolVersion, string schemaVersion) =>
        InvokeValidation(_classifyVersions, protocolVersion, schemaVersion);

    public NativeSceneContractCatalogValidationResult ValidateCorpus() => ReadValidationResult(_validateCorpus.Invoke(null, null));

    private static MethodInfo RequireStaticMethod(Type catalogType, string name, Type? returnType, params Type[] parameterTypes)
    {
        var method = catalogType.GetMethod(
            name,
            BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: parameterTypes,
            modifiers: null);
        Assert.NotNull(method);
        if (returnType is not null)
        {
            Assert.Equal(returnType, method!.ReturnType);
        }

        return method!;
    }

    private static void RequireValidationResultShape(Type resultType)
    {
        foreach (var (name, expectedType) in new[]
                 {
                     ("IsValid", typeof(bool)),
                     ("Code", typeof(string)),
                     ("Message", typeof(string)),
                 })
        {
            var property = resultType.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
            Assert.NotNull(property);
            Assert.True(property!.CanRead, $"NativeSceneValidationResult.{name} must be readable.");
            Assert.Equal(expectedType, property.PropertyType);
        }
    }

    private static NativeSceneContractCatalogValidationResult InvokeValidation(MethodInfo method, string first, string second) =>
        ReadValidationResult(method.Invoke(null, new object?[] { first, second }));

    private static NativeSceneContractCatalogValidationResult ReadValidationResult(object? value)
    {
        Assert.NotNull(value);
        var result = value!;
        var resultType = result.GetType();
        return new NativeSceneContractCatalogValidationResult(
            Assert.IsType<bool>(Property(resultType, "IsValid").GetValue(result)),
            Property(resultType, "Code").GetValue(result) as string,
            Property(resultType, "Message").GetValue(result) as string);
    }

    private static PropertyInfo Property(Type resultType, string name) =>
        resultType.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
        ?? throw new InvalidOperationException($"Missing NativeSceneValidationResult.{name} property.");
}
