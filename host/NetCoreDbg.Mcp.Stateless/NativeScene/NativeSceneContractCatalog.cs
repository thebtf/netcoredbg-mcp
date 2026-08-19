using System.Globalization;
using System.Reflection;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

internal static class NativeSceneContractCatalog
{
    private const int MaxInputUtf8Bytes = 262_144;
    private const int MaxJsonNesting = 16;
    private const int MaxContainerMembers = 256;
    private const string ManifestPrefix = "NetCoreDbg.Mcp.Stateless.NativeScene.";
    private const string ActiveProtocolVersion = "native-scene-probe/1";
    private const string ActiveSchemaVersion = "native-scene-probe.schema/1";

    private static readonly NativeSceneValidationResult Valid = new(true, null, null);
    private static readonly NativeSceneValidationResult InvalidRequest = new(false, "INVALID_TOOL_ARGUMENTS", "Native scene request is invalid.");
    private static readonly NativeSceneValidationResult InvalidResult = new(false, "INVALID_TOOL_ARGUMENTS", "Native scene result is invalid.");
    private static readonly NativeSceneValidationResult InvalidVersion = new(false, "INVALID_TOOL_ARGUMENTS", "Native scene version syntax is invalid.");
    private static readonly NativeSceneValidationResult UnsupportedVersion = new(false, "UNSUPPORTED_PROTOCOL", "Native scene version is unsupported.");
    private static readonly NativeSceneValidationResult InvalidCorpus = new(false, "INVALID_TOOL_ARGUMENTS", "Native scene corpus is invalid.");
    private static readonly ContractState State = LoadState();

    internal static ReadOnlyMemory<byte> GetArtifactBytes(string fileName) => State.Artifacts.TryGetValue(fileName, out var artifact)
        ? new ReadOnlyMemory<byte>(artifact.Bytes.ToArray())
        : throw new InvalidOperationException("Unknown native scene contract artifact.");

    internal static string GetArtifactSha256(string fileName) => State.Artifacts.TryGetValue(fileName, out var artifact)
        ? artifact.Sha256
        : throw new InvalidOperationException("Unknown native scene contract artifact.");

    internal static NativeSceneValidationResult ValidateRequest(string tool, string json)
    {
        if (!TryGetRequestDefinition(tool, out var definition) || !TryParseBounded(json, out var instance))
        {
            return InvalidRequest;
        }

        if (!ValidatesDefinition(State.ProbeDefinitions, instance, definition) || instance is not JsonObject request ||
            !TryGetString(request["protocolVersion"], out var protocolVersion) ||
            !TryGetString(request["schemaVersion"], out var schemaVersion))
        {
            return InvalidRequest;
        }

        return ClassifyVersions(protocolVersion, schemaVersion);
    }

    internal static NativeSceneValidationResult ValidateResult(string tool, string json)
    {
        if (!TryGetResultDefinitions(tool, out var definitions) || !TryParseBounded(json, out var instance))
        {
            return InvalidResult;
        }

        var matches = 0;
        foreach (var definition in definitions)
        {
            if (ValidatesDefinition(State.ProbeDefinitions, instance, definition))
            {
                matches++;
            }
        }

        return matches == 1 ? Valid : InvalidResult;
    }

    internal static NativeSceneValidationResult ClassifyVersions(string protocolVersion, string schemaVersion)
    {
        if (!HasPositiveVersion(protocolVersion, "native-scene-probe/") ||
            !HasPositiveVersion(schemaVersion, "native-scene-probe.schema/"))
        {
            return InvalidVersion;
        }

        return StringComparer.Ordinal.Equals(protocolVersion, ActiveProtocolVersion) &&
               StringComparer.Ordinal.Equals(schemaVersion, ActiveSchemaVersion)
            ? Valid
            : UnsupportedVersion;
    }

    internal static NativeSceneValidationResult ValidateCorpus()
    {
        try
        {
            return IsApprovedCorpus(State) ? Valid : InvalidCorpus;
        }
        catch (JsonException)
        {
            return InvalidCorpus;
        }
        catch (InvalidOperationException)
        {
            return InvalidCorpus;
        }
    }

    private static ContractState LoadState()
    {
        var artifacts = new Dictionary<string, ContractArtifact>(StringComparer.Ordinal)
        {
            ["native-scene-probe.schema.json"] = LoadArtifact("native-scene-probe.schema.json", "f446166f9a1062d3e1a2190327d06c04905e76a1c1f81af16c87572394f90022"),
            ["native-scene-artifact.schema.json"] = LoadArtifact("native-scene-artifact.schema.json", "07c257c9b5f75c01aa4f4141968c789b045d7c831575343df429075c732f7668"),
            ["parity-corpus.json"] = LoadArtifact("parity-corpus.json", "90c24f8f9706c207ca3ecf8dee93d1937c16a6be45feac65d812e48853bc4621"),
        };

        var probeSchema = ParseObject(artifacts["native-scene-probe.schema.json"].Bytes, "native-scene-probe.schema.json");
        var artifactSchema = ParseObject(artifacts["native-scene-artifact.schema.json"].Bytes, "native-scene-artifact.schema.json");
        var corpus = ParseObject(artifacts["parity-corpus.json"].Bytes, "parity-corpus.json");
        var probeDefinitions = RequiredObject(probeSchema["definitions"]);
        var artifactDefinitions = RequiredObject(artifactSchema["definitions"]);

        if (!IsDraft7Schema(probeSchema) || !IsDraft7Schema(artifactSchema) ||
            !HasResolvedLocalReferences(probeSchema, probeDefinitions) ||
            !HasResolvedLocalReferences(artifactSchema, artifactDefinitions))
        {
            throw new InvalidOperationException("Native scene contract resources are invalid.");
        }

        return new ContractState(artifacts, probeSchema, probeDefinitions, artifactSchema, artifactDefinitions, corpus);
    }

    private static ContractArtifact LoadArtifact(string fileName, string expectedSha256)
    {
        using var stream = Assembly.GetExecutingAssembly().GetManifestResourceStream(ManifestPrefix + fileName)
            ?? throw new InvalidOperationException("Native scene contract resource is missing.");
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        var bytes = buffer.ToArray();
        var actualSha256 = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
        if (!StringComparer.Ordinal.Equals(actualSha256, expectedSha256))
        {
            throw new InvalidOperationException("Native scene contract resource hash drift was detected.");
        }

        return new ContractArtifact(bytes, actualSha256);
    }

    private static JsonObject ParseObject(ReadOnlyMemory<byte> bytes, string artifactName) =>
        JsonNode.Parse(Encoding.UTF8.GetString(bytes.Span)) as JsonObject
        ?? throw new InvalidOperationException($"Native scene contract artifact '{artifactName}' is not an object.");

    private static bool TryGetRequestDefinition(string tool, out string definition)
    {
        definition = tool switch
        {
            "get_ui_probe_capabilities" => "getUiProbeCapabilitiesArguments",
            "capture_visual_evidence" => "captureVisualEvidenceArguments",
            "read_capture_artifact" => "readCaptureArtifactArguments",
            "wait_for_ui_stable" => "waitForUiStableArguments",
            "capture_element_snapshot" => "captureElementSnapshotArguments",
            "capture_native_scene" => "captureNativeSceneArguments",
            _ => string.Empty,
        };
        return definition.Length != 0;
    }

    private static bool TryGetResultDefinitions(string tool, out string[] definitions)
    {
        definitions = tool switch
        {
            "get_ui_probe_capabilities" => ["uiProbeCapabilities", "getUiProbeCapabilitiesToolError"],
            "capture_visual_evidence" => ["visualEvidenceCapture", "captureVisualEvidenceToolError"],
            "read_capture_artifact" => ["captureArtifactChunk", "readCaptureArtifactToolError", "artifactNotFoundError"],
            "wait_for_ui_stable" => ["stabilityReceipt", "waitForUiStableToolError"],
            "capture_element_snapshot" => ["elementSnapshotCapture", "captureElementSnapshotToolError"],
            "capture_native_scene" => ["nativeSceneCapture", "captureNativeSceneToolError"],
            _ => [],
        };
        return definitions.Length != 0;
    }

    private static bool TryParseBounded(string? json, out JsonNode? instance)
    {
        instance = null;
        if (json is null || Encoding.UTF8.GetByteCount(json) > MaxInputUtf8Bytes)
        {
            return false;
        }

        try
        {
            instance = JsonNode.Parse(json);
        }
        catch (JsonException)
        {
            return false;
        }

        return instance is not null && HasRuntimeBounds(instance, 0) && HasBoundedStateValues(instance);
    }

    private static bool HasRuntimeBounds(JsonNode? node, int depth)
    {
        switch (node)
        {
            case JsonObject objectNode:
                if (depth > MaxJsonNesting || objectNode.Count > MaxContainerMembers)
                {
                    return false;
                }

                foreach (var property in objectNode)
                {
                    if (!HasRuntimeBounds(property.Value, depth + 1))
                    {
                        return false;
                    }
                }

                return true;
            case JsonArray arrayNode:
                if (depth > MaxJsonNesting || arrayNode.Count > MaxContainerMembers)
                {
                    return false;
                }

                foreach (var item in arrayNode)
                {
                    if (!HasRuntimeBounds(item, depth + 1))
                    {
                        return false;
                    }
                }

                return true;
            default:
                return true;
        }
    }

    private static bool HasBoundedStateValues(JsonNode? node)
    {
        switch (node)
        {
            case JsonObject objectNode:
                foreach (var property in objectNode)
                {
                    if ((StringComparer.Ordinal.Equals(property.Key, "selectedState") || StringComparer.Ordinal.Equals(property.Key, "currentState")) &&
                        property.Value is not null && Encoding.UTF8.GetByteCount(property.Value.ToJsonString()) > MaxInputUtf8Bytes)
                    {
                        return false;
                    }

                    if (!HasBoundedStateValues(property.Value))
                    {
                        return false;
                    }
                }

                return true;
            case JsonArray arrayNode:
                foreach (var item in arrayNode)
                {
                    if (!HasBoundedStateValues(item))
                    {
                        return false;
                    }
                }

                return true;
            default:
                return true;
        }
    }

    private static bool HasPositiveVersion(string? value, string prefix)
    {
        if (value is null || !value.StartsWith(prefix, StringComparison.Ordinal) || value.Length == prefix.Length)
        {
            return false;
        }

        var firstDigit = value[prefix.Length];
        if (firstDigit is < '1' or > '9')
        {
            return false;
        }

        for (var index = prefix.Length + 1; index < value.Length; index++)
        {
            if (value[index] is < '0' or > '9')
            {
                return false;
            }
        }

        return true;
    }

    private static bool IsApprovedCorpus(ContractState state)
    {
        if (!HasResolvedLocalReferences(state.ProbeSchema, state.ProbeDefinitions) ||
            !HasResolvedLocalReferences(state.ArtifactSchema, state.ArtifactDefinitions) ||
            !TryGetObject(state.Corpus["contract"], out var contract) ||
            !TryGetString(contract["protocolVersion"], out var protocolVersion) ||
            !TryGetString(contract["schemaVersion"], out var schemaVersion) ||
            !TryGetString(contract["artifactSchemaVersion"], out var artifactSchemaVersion) ||
            !StringComparer.Ordinal.Equals(protocolVersion, ActiveProtocolVersion) ||
            !StringComparer.Ordinal.Equals(schemaVersion, ActiveSchemaVersion) ||
            !StringComparer.Ordinal.Equals(artifactSchemaVersion, "native-scene-artifact/1") ||
            !TryGetObject(state.Corpus["fixtures"], out var fixtures) ||
            !TryGetString(fixtures["sessionId"], out var sessionId) ||
            !ValidatesDefinition(state.ProbeDefinitions, JsonValue.Create(sessionId), "debugSessionId") ||
            !ValidatesDefinition(state.ProbeDefinitions, fixtures["sceneRequest"], "sceneRequest") ||
            !TryGetObject(state.Corpus["gateStages"], out var gateStages) ||
            !HasExpectedGateStage(gateStages) ||
            !TryGetArray(state.Corpus["cases"], out var cases) || cases.Count != 24)
        {
            return false;
        }

        var caseNumbers = new bool[25];
        foreach (var item in cases)
        {
            if (!TryGetObject(item, out var caseEntry) || !IsApprovedCase(state, caseEntry, caseNumbers))
            {
                return false;
            }
        }

        for (var number = 1; number <= 24; number++)
        {
            if (!caseNumbers[number])
            {
                return false;
            }
        }

        return true;
    }

    private static bool IsApprovedCase(ContractState state, JsonObject caseEntry, bool[] caseNumbers)
    {
        if (!TryGetString(caseEntry["id"], out var id) || !TryGetCaseNumber(id, out var number) || caseNumbers[number] ||
            !TryGetString(caseEntry["contractGateExpectation"], out var gateExpectation) || !IsGateExpectation(gateExpectation) ||
            !TryGetBoolean(caseEntry["runtimeBehaviorRequired"], out _) ||
            !TryGetString(caseEntry["primitive"], out var primitive) || !TryGetRequestDefinition(primitive, out _) ||
            !TryGetObject(caseEntry["expected"], out var expected) || !TryGetString(expected["classification"], out var classification) ||
            !IsExpectedClassification(classification))
        {
            return false;
        }

        caseNumbers[number] = true;
        if (caseEntry.TryGetPropertyValue("request", out var request) && request is not null)
        {
            var isShapeValid = ValidatesRequestShape(state, primitive, request);
            var expectsInvalidShape = StringComparer.Ordinal.Equals(gateExpectation, "negative_request_schema_fixture_and_expected_classification_vocabulary") ||
                                      StringComparer.Ordinal.Equals(gateExpectation, "negative_version_fixture_and_expected_classification_vocabulary");
            if (isShapeValid == expectsInvalidShape)
            {
                return false;
            }
        }

        if (!ValidateCorpusResponse(state, caseEntry, caseEntry["responseSchema"], caseEntry["response"]))
        {
            return false;
        }

        if (caseEntry.TryGetPropertyValue("schemaInvalidVariants", out var variantsNode))
        {
            if (!TryGetArray(variantsNode, out var variants) || variants.Count == 0)
            {
                return false;
            }

            foreach (var variantNode in variants)
            {
                if (!TryGetObject(variantNode, out var variant) ||
                    !TryGetString(variant["name"], out _) ||
                    !TryGetObject(variant["expected"], out var variantExpected) ||
                    !TryGetString(variantExpected["classification"], out var variantClassification) ||
                    !StringComparer.Ordinal.Equals(variantClassification, "schema_rejection") ||
                    !ValidateCorpusResponse(state, variant, variant["responseSchema"] ?? caseEntry["responseSchema"], variant["response"]))
                {
                    return false;
                }
            }
        }

        return true;
    }

    private static bool ValidateCorpusResponse(ContractState state, JsonObject owner, JsonNode? schemaReference, JsonNode? response)
    {
        if (schemaReference is null && response is null)
        {
            return true;
        }

        if (!TryGetString(schemaReference, out var reference) || !TryResolveDefinition(state.ProbeDefinitions, reference, out var definition) || response is null ||
            !TryGetObject(owner["expected"], out var expected) || !TryGetString(expected["classification"], out var classification))
        {
            return false;
        }

        var valid = new SchemaValidator(state.ProbeDefinitions).Validate(response, definition);
        return StringComparer.Ordinal.Equals(classification, "schema_rejection") ? !valid : valid;
    }

    private static bool ValidatesRequestShape(ContractState state, string tool, JsonNode instance) =>
        TryGetRequestDefinition(tool, out var definition) && ValidatesDefinition(state.ProbeDefinitions, instance, definition);

    private static bool ValidatesDefinition(JsonObject definitions, JsonNode? instance, string definition) =>
        definitions.TryGetPropertyValue(definition, out var schema) && schema is JsonObject schemaObject &&
        new SchemaValidator(definitions).Validate(instance, schemaObject);

    private static bool IsDraft7Schema(JsonObject schema) =>
        TryGetString(schema["$schema"], out var draft) &&
        StringComparer.Ordinal.Equals(draft, "http://json-schema.org/draft-07/schema#");

    private static bool HasResolvedLocalReferences(JsonNode? node, JsonObject definitions)
    {
        switch (node)
        {
            case JsonObject objectNode:
                if (objectNode.TryGetPropertyValue("$ref", out var referenceNode) &&
                    (!TryGetString(referenceNode, out var reference) || !TryResolveDefinition(definitions, reference, out _)))
                {
                    return false;
                }

                foreach (var property in objectNode)
                {
                    if (!HasResolvedLocalReferences(property.Value, definitions))
                    {
                        return false;
                    }
                }

                return true;
            case JsonArray arrayNode:
                foreach (var item in arrayNode)
                {
                    if (!HasResolvedLocalReferences(item, definitions))
                    {
                        return false;
                    }
                }

                return true;
            default:
                return true;
        }
    }

    private static bool TryResolveDefinition(JsonObject definitions, string reference, out JsonObject definition)
    {
        definition = null!;
        const string prefix = "#/definitions/";
        return reference.StartsWith(prefix, StringComparison.Ordinal) &&
               definitions.TryGetPropertyValue(reference[prefix.Length..], out var node) &&
               node is JsonObject objectNode &&
               AssignDefinition(objectNode, out definition);
    }

    private static bool AssignDefinition(JsonObject source, out JsonObject definition)
    {
        definition = source;
        return true;
    }

    private static bool HasExpectedGateStage(JsonObject gateStages)
    {
        if (!TryGetObject(gateStages["M0-G0"], out var gate) || !TryGetString(gate["task"], out var task) ||
            !StringComparer.Ordinal.Equals(task, "T007") || !TryGetArray(gate["validates"], out var validates) || validates.Count != 6)
        {
            return false;
        }

        var expected = new[]
        {
            "exact-byte Draft-7 schema loading",
            "request/result validator parity for concrete fixtures present",
            "internal reference resolution",
            "corpus syntax and integrity",
            "expected classification vocabulary",
            "negative structural and version cases",
        };

        for (var index = 0; index < expected.Length; index++)
        {
            if (!TryGetString(validates[index], out var value) || !StringComparer.Ordinal.Equals(value, expected[index]))
            {
                return false;
            }
        }

        return true;
    }

    private static bool TryGetCaseNumber(string id, out int number)
    {
        number = 0;
        if (id.Length < 6 || id[0] != 'C' || id[4] != '-' ||
            id[1] is < '0' or > '9' || id[2] is < '0' or > '9' || id[3] is < '0' or > '9')
        {
            return false;
        }

        number = ((id[1] - '0') * 100) + ((id[2] - '0') * 10) + (id[3] - '0');
        return number is >= 1 and <= 24;
    }

    private static bool IsGateExpectation(string value) =>
        StringComparer.Ordinal.Equals(value, "schema_fixture_and_expected_classification_vocabulary_only") ||
        StringComparer.Ordinal.Equals(value, "negative_version_fixture_and_expected_classification_vocabulary") ||
        StringComparer.Ordinal.Equals(value, "negative_request_schema_fixture_and_expected_classification_vocabulary") ||
        StringComparer.Ordinal.Equals(value, "negative_response_schema_fixture_and_expected_classification_vocabulary");

    private static bool IsExpectedClassification(string value) =>
        StringComparer.Ordinal.Equals(value, "artifact_chunk") ||
        StringComparer.Ordinal.Equals(value, "capability_declaration") ||
        StringComparer.Ordinal.Equals(value, "complete_observation") ||
        StringComparer.Ordinal.Equals(value, "qualified_observation") ||
        StringComparer.Ordinal.Equals(value, "schema_rejection") ||
        StringComparer.Ordinal.Equals(value, "typed_error");

    private static bool TryGetObject(JsonNode? node, out JsonObject value)
    {
        value = node as JsonObject ?? null!;
        return value is not null;
    }

    private static bool TryGetArray(JsonNode? node, out JsonArray value)
    {
        value = node as JsonArray ?? null!;
        return value is not null;
    }

    private static bool TryGetString(JsonNode? node, out string value)
    {
        value = string.Empty;
        if (node is not JsonValue jsonValue || !jsonValue.TryGetValue<string>(out var candidate) || candidate is null)
        {
            return false;
        }

        value = candidate;
        return true;
    }

    private static bool TryGetBoolean(JsonNode? node, out bool value)
    {
        value = false;
        return node is JsonValue jsonValue && jsonValue.TryGetValue<bool>(out value);
    }

    private static JsonObject RequiredObject(JsonNode? node) => node as JsonObject
        ?? throw new InvalidOperationException("Native scene contract artifact has no definitions.");

    private sealed record ContractArtifact(byte[] Bytes, string Sha256);

    private sealed record ContractState(
        IReadOnlyDictionary<string, ContractArtifact> Artifacts,
        JsonObject ProbeSchema,
        JsonObject ProbeDefinitions,
        JsonObject ArtifactSchema,
        JsonObject ArtifactDefinitions,
        JsonObject Corpus);

    private sealed class SchemaValidator
    {
        private const int MaxEvaluationSteps = 1_000_000;
        private readonly JsonObject _definitions;
        private int _remainingSteps = MaxEvaluationSteps;

        internal SchemaValidator(JsonObject definitions)
        {
            _definitions = definitions;
        }

        internal bool Validate(JsonNode? instance, JsonObject schema)
        {
            if (--_remainingSteps < 0)
            {
                return false;
            }

            if (schema.TryGetPropertyValue("$ref", out var referenceNode))
            {
                if (!TryGetString(referenceNode, out var reference) || !TryResolveDefinition(_definitions, reference, out var referenceSchema) ||
                    !Validate(instance, referenceSchema))
                {
                    return false;
                }
            }

            if (schema.TryGetPropertyValue("type", out var typeNode) && !MatchesType(instance, typeNode))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("enum", out var enumNode))
            {
                if (!TryGetArray(enumNode, out var values) || !MatchesEnum(instance, values))
                {
                    return false;
                }
            }

            if (schema.TryGetPropertyValue("const", out var constant) && !JsonNode.DeepEquals(instance, constant))
            {
                return false;
            }

            if (instance is JsonObject objectNode && !ValidateObject(objectNode, schema))
            {
                return false;
            }

            if (instance is JsonArray arrayNode && !ValidateArray(arrayNode, schema))
            {
                return false;
            }

            if (TryGetString(instance, out var stringValue) && !ValidateString(stringValue, schema))
            {
                return false;
            }

            if (IsJsonNumber(instance) && HasNumericConstraints(schema) &&
                (!TryGetDoubleNumber(instance, out var numberValue) || !ValidateNumber(numberValue, schema)))
            {
                return false;
            }

            if (!ValidateCombinators(instance, schema))
            {
                return false;
            }

            return ValidateConditional(instance, schema);
        }

        private bool ValidateObject(JsonObject instance, JsonObject schema)
        {
            if (schema.TryGetPropertyValue("minProperties", out var minProperties) &&
                (!TryGetWholeNumber(minProperties, out var minimum) || instance.Count < minimum))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("maxProperties", out var maxProperties) &&
                (!TryGetWholeNumber(maxProperties, out var maximum) || instance.Count > maximum))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("required", out var requiredNode))
            {
                if (!TryGetArray(requiredNode, out var required))
                {
                    return false;
                }

                foreach (var requiredName in required)
                {
                    if (!TryGetString(requiredName, out var name) || !instance.ContainsKey(name))
                    {
                        return false;
                    }
                }
            }

            JsonObject? properties = null;
            if (schema.TryGetPropertyValue("properties", out var propertiesNode))
            {
                if (!TryGetObject(propertiesNode, out properties))
                {
                    return false;
                }
            }

            JsonObject? patternProperties = null;
            if (schema.TryGetPropertyValue("patternProperties", out var patternPropertiesNode))
            {
                if (!TryGetObject(patternPropertiesNode, out patternProperties))
                {
                    return false;
                }
            }

            var additionalPropertiesFalse = schema.TryGetPropertyValue("additionalProperties", out var additionalProperties) &&
                                            TryGetBoolean(additionalProperties, out var additionalAllowed) && !additionalAllowed;

            foreach (var property in instance)
            {
                var matched = false;
                if (properties is not null && properties.TryGetPropertyValue(property.Key, out var propertySchema))
                {
                    if (propertySchema is not JsonObject propertyObject || !Validate(property.Value, propertyObject))
                    {
                        return false;
                    }

                    matched = true;
                }

                if (patternProperties is not null)
                {
                    foreach (var patternProperty in patternProperties)
                    {
                        if (!Regex.IsMatch(property.Key, patternProperty.Key, RegexOptions.CultureInvariant))
                        {
                            continue;
                        }

                        if (patternProperty.Value is not JsonObject patternSchema || !Validate(property.Value, patternSchema))
                        {
                            return false;
                        }

                        matched = true;
                    }
                }

                if (!matched && additionalPropertiesFalse)
                {
                    return false;
                }
            }

            return true;
        }

        private bool ValidateArray(JsonArray instance, JsonObject schema)
        {
            if (schema.TryGetPropertyValue("minItems", out var minItems) &&
                (!TryGetWholeNumber(minItems, out var minimum) || instance.Count < minimum))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("maxItems", out var maxItems) &&
                (!TryGetWholeNumber(maxItems, out var maximum) || instance.Count > maximum))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("items", out var items) && !ValidateItems(instance, items))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("uniqueItems", out var uniqueItems) && TryGetBoolean(uniqueItems, out var unique) && unique &&
                !HasUniqueItems(instance))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("contains", out var contains) &&
                (contains is not JsonObject containsSchema || !instance.Any(item => Validate(item, containsSchema))))
            {
                return false;
            }

            return true;
        }

        private bool ValidateItems(JsonArray instance, JsonNode? items)
        {
            switch (items)
            {
                case JsonObject itemSchema:
                    foreach (var item in instance)
                    {
                        if (!Validate(item, itemSchema))
                        {
                            return false;
                        }
                    }

                    return true;
                case JsonArray tupleSchemas:
                    for (var index = 0; index < instance.Count && index < tupleSchemas.Count; index++)
                    {
                        if (tupleSchemas[index] is not JsonObject tupleSchema || !Validate(instance[index], tupleSchema))
                        {
                            return false;
                        }
                    }

                    return true;
                default:
                    return false;
            }
        }

        private bool ValidateString(string value, JsonObject schema)
        {
            var length = ScalarLength(value);
            if (schema.TryGetPropertyValue("minLength", out var minLength) &&
                (!TryGetWholeNumber(minLength, out var minimum) || length < minimum))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("maxLength", out var maxLength) &&
                (!TryGetWholeNumber(maxLength, out var maximum) || length > maximum))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("pattern", out var patternNode) &&
                (!TryGetString(patternNode, out var pattern) || !Regex.IsMatch(value, pattern, RegexOptions.CultureInvariant)))
            {
                return false;
            }

            return !schema.TryGetPropertyValue("format", out var formatNode) ||
                   (TryGetString(formatNode, out var format) &&
                    (format != "date-time" || IsRfc3339DateTime(value)));
        }

        private static bool IsRfc3339DateTime(string value)
        {
            const string pattern = @"^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])T([01]\d|2[0-3]):[0-5]\d:[0-5]\d(?:\.\d+)?(?:Z|[+-](?:[01]\d|2[0-3]):[0-5]\d)$";
            return Regex.IsMatch(value, pattern, RegexOptions.CultureInvariant) &&
                   DateTimeOffset.TryParse(value, CultureInfo.InvariantCulture, DateTimeStyles.None, out _);
        }

        private static bool ValidateNumber(double value, JsonObject schema)
        {
            if (schema.TryGetPropertyValue("minimum", out var minimumNode) &&
                (!TryGetDoubleNumber(minimumNode, out var minimum) || value < minimum))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("maximum", out var maximumNode) &&
                (!TryGetDoubleNumber(maximumNode, out var maximum) || value > maximum))
            {
                return false;
            }

            if (schema.TryGetPropertyValue("exclusiveMinimum", out var exclusiveMinimumNode) &&
                (!TryGetDoubleNumber(exclusiveMinimumNode, out var exclusiveMinimum) || value <= exclusiveMinimum))
            {
                return false;
            }

            return !schema.TryGetPropertyValue("exclusiveMaximum", out var exclusiveMaximumNode) ||
                   (TryGetDoubleNumber(exclusiveMaximumNode, out var exclusiveMaximum) && value < exclusiveMaximum);
        }

        private static bool HasNumericConstraints(JsonObject schema) =>
            schema.ContainsKey("minimum") || schema.ContainsKey("maximum") ||
            schema.ContainsKey("exclusiveMinimum") || schema.ContainsKey("exclusiveMaximum");

        private bool ValidateCombinators(JsonNode? instance, JsonObject schema)
        {
            if (schema.TryGetPropertyValue("allOf", out var allOfNode))
            {
                if (!TryGetArray(allOfNode, out var allOf))
                {
                    return false;
                }

                foreach (var branch in allOf)
                {
                    if (branch is not JsonObject branchSchema || !Validate(instance, branchSchema))
                    {
                        return false;
                    }
                }
            }

            if (schema.TryGetPropertyValue("anyOf", out var anyOfNode))
            {
                if (!TryGetArray(anyOfNode, out var anyOf) || !anyOf.Any(branch => branch is JsonObject branchSchema && Validate(instance, branchSchema)))
                {
                    return false;
                }
            }

            if (schema.TryGetPropertyValue("oneOf", out var oneOfNode))
            {
                if (!TryGetArray(oneOfNode, out var oneOf))
                {
                    return false;
                }

                var matches = 0;
                foreach (var branch in oneOf)
                {
                    if (branch is JsonObject branchSchema && Validate(instance, branchSchema))
                    {
                        matches++;
                    }
                }

                if (matches != 1)
                {
                    return false;
                }
            }

            return true;
        }

        private bool ValidateConditional(JsonNode? instance, JsonObject schema)
        {
            if (!schema.TryGetPropertyValue("if", out var ifNode))
            {
                return true;
            }

            if (ifNode is not JsonObject ifSchema)
            {
                return false;
            }

            var branchName = Validate(instance, ifSchema) ? "then" : "else";
            return !schema.TryGetPropertyValue(branchName, out var branchNode) ||
                   (branchNode is JsonObject branchSchema && Validate(instance, branchSchema));
        }

        private static bool MatchesType(JsonNode? instance, JsonNode? typeNode)
        {
            if (TryGetString(typeNode, out var type))
            {
                return MatchesSingleType(instance, type);
            }

            if (!TryGetArray(typeNode, out var types))
            {
                return false;
            }

            foreach (var typeValue in types)
            {
                if (TryGetString(typeValue, out var candidate) && MatchesSingleType(instance, candidate))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool MatchesSingleType(JsonNode? instance, string type) => type switch
        {
            "null" => instance is null,
            "object" => instance is JsonObject,
            "array" => instance is JsonArray,
            "string" => TryGetString(instance, out _),
            "boolean" => TryGetBoolean(instance, out _),
            "number" => IsJsonNumber(instance),
            "integer" => TryGetNumber(instance, out var value) && decimal.Truncate(value) == value,
            _ => false,
        };

        private static bool MatchesEnum(JsonNode? instance, JsonArray values)
        {
            foreach (var value in values)
            {
                if (JsonNode.DeepEquals(instance, value))
                {
                    return true;
                }
            }

            return false;
        }

        private static bool HasUniqueItems(JsonArray items)
        {
            for (var left = 0; left < items.Count; left++)
            {
                for (var right = left + 1; right < items.Count; right++)
                {
                    if (JsonNode.DeepEquals(items[left], items[right]))
                    {
                        return false;
                    }
                }
            }

            return true;
        }

        private static int ScalarLength(string value)
        {
            var length = 0;
            foreach (var _ in value.EnumerateRunes())
            {
                length++;
            }

            return length;
        }

        private static bool IsJsonNumber(JsonNode? node) =>
            node is JsonValue jsonValue &&
            jsonValue.TryGetValue<JsonElement>(out var element) &&
            element.ValueKind == JsonValueKind.Number;

        private static bool TryGetDoubleNumber(JsonNode? node, out double value)
        {
            value = 0;
            return node is JsonValue jsonValue &&
                   jsonValue.TryGetValue<JsonElement>(out var element) &&
                   element.ValueKind == JsonValueKind.Number &&
                   element.TryGetDouble(out value) && double.IsFinite(value);
        }

        private static bool TryGetNumber(JsonNode? node, out decimal value)
        {
            value = 0;
            return node is JsonValue jsonValue &&
                   jsonValue.TryGetValue<JsonElement>(out var element) &&
                   element.ValueKind == JsonValueKind.Number &&
                   element.TryGetDecimal(out value);
        }

        private static bool TryGetWholeNumber(JsonNode? node, out int value)
        {
            value = 0;
            return TryGetNumber(node, out var number) && decimal.Truncate(number) == number &&
                   number is >= int.MinValue and <= int.MaxValue && (value = decimal.ToInt32(number)) == number;
        }
    }
}

internal sealed record NativeSceneValidationResult(bool IsValid, string? Code, string? Message);
