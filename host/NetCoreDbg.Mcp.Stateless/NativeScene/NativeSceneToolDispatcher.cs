using System.Buffers;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

/// <summary>
/// Validates and dispatches the closed native-scene front door without owning session lookup or native observation.
/// </summary>
internal static class NativeSceneToolDispatcher
{
    private const string ProbeSchemaArtifact = "native-scene-probe.schema.json";
    private const string UnsupportedCapability = "UNSUPPORTED_CAPABILITY";
    private const string DebugSessionNotFound = "DEBUG_SESSION_NOT_FOUND";
    private const string CandidateMismatch = "CANDIDATE_MISMATCH";

    private static readonly FrozenContract Contract = FrozenContract.Load();
    private static readonly IReadOnlyList<Tool> Tools = CreateTools();

    internal static IReadOnlyList<Tool> ListTools() => Tools;

    internal static async ValueTask<CallToolResult> DispatchAsync(
        string tool,
        IDictionary<string, JsonElement>? arguments,
        Func<string, ValueTask<NativeSceneSessionBinding?>> resolveBinding,
        CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(tool);
        ArgumentNullException.ThrowIfNull(resolveBinding);
        if (!Contract.IsKnownTool(tool))
        {
            throw new ArgumentOutOfRangeException(nameof(tool), tool, "Unknown native scene tool.");
        }

        var requestJson = SerializeArguments(arguments);
        var validation = NativeSceneContractCatalog.ValidateRequest(tool, requestJson);
        if (!validation.IsValid)
        {
            return ToolError(tool, validation.Code!, validation.Message!);
        }

        using var requestDocument = JsonDocument.Parse(requestJson);
        var request = requestDocument.RootElement;
        var debugSessionId = request.GetProperty("debugSessionId").GetString()!;
        var binding = await resolveBinding(debugSessionId).ConfigureAwait(false);
        if (binding is null)
        {
            return ToolError(tool, DebugSessionNotFound, "Debug session is not available.");
        }

        if (!binding.TryGetCandidate(out var candidate))
        {
            return ToolError(tool, UnsupportedCapability, "Native scene capability is unsupported because debuggee identity is unavailable.");
        }

        if (request.TryGetProperty("sceneRequest", out var sceneRequest) &&
            !binding.MatchesExpectedCandidateIdentity(sceneRequest.GetProperty("expectedCandidateIdentity")))
        {
            return ToolError(tool, CandidateMismatch, "Candidate identity does not match.");
        }

        return tool switch
        {
            "get_ui_probe_capabilities" => Success(tool, CapabilityDeclaration(candidate, binding.SupportsVisualEvidence)),
            "capture_visual_evidence" => await CaptureVisualEvidenceAsync(binding, request, candidate, cancellationToken).ConfigureAwait(false),
            "read_capture_artifact" => await ReadCaptureArtifactAsync(binding, request, cancellationToken).ConfigureAwait(false),
            "wait_for_ui_stable" or "capture_element_snapshot" or "capture_native_scene" =>
                ToolError(tool, UnsupportedCapability, "Native scene capability is unsupported."),
            _ => throw new InvalidOperationException("Known native scene tool has no dispatch branch."),
        };
    }

    private static IReadOnlyList<Tool> CreateTools()
    {
        var tools = new Tool[Contract.ToolSchemas.Count];
        for (var index = 0; index < tools.Length; index++)
        {
            var tool = Contract.ToolSchemas[index];
            tools[index] = new Tool
            {
                Name = tool.Name,
                Description = $"Native scene probe operation: {tool.Name}.",
                InputSchema = CreateInputSchema(tool.Definition),
            };
        }

        return Array.AsReadOnly(tools);
    }

    private static JsonElement CreateInputSchema(string definition)
    {
        using var frozenSchema = JsonDocument.Parse(Contract.ProbeSchemaBytes);
        var buffer = new ArrayBufferWriter<byte>();
        using (var writer = new Utf8JsonWriter(buffer))
        {
            writer.WriteStartObject();
            writer.WriteString("type", "object");
            var selectedDefinition = false;
            foreach (var property in frozenSchema.RootElement.EnumerateObject())
            {
                if (property.NameEquals("anyOf"))
                {
                    writer.WriteString("$ref", $"#/definitions/{definition}");
                    selectedDefinition = true;
                }
                else
                {
                    property.WriteTo(writer);
                }
            }

            writer.WriteEndObject();
            if (!selectedDefinition)
            {
                throw new InvalidOperationException("Frozen native scene schema has no root selector.");
            }
        }

        using var schema = JsonDocument.Parse(buffer.WrittenMemory);
        return schema.RootElement.Clone();
    }

    private static string SerializeArguments(IDictionary<string, JsonElement>? arguments)
    {
        if (arguments is null)
        {
            return "{}";
        }

        try
        {
            return JsonSerializer.Serialize(arguments);
        }
        catch (ArgumentException)
        {
            return "{}";
        }
        catch (NotSupportedException)
        {
            return "{}";
        }
    }

    private static JsonObject CapabilityDeclaration(JsonElement candidate, bool supportsVisualEvidence) => new()
    {
        ["kind"] = "ui_probe_capabilities",
        ["protocolVersion"] = Contract.ProtocolVersion,
        ["schemaVersion"] = Contract.SchemaVersion,
        ["candidate"] = CloneCandidate(candidate),
        ["capabilities"] = new JsonObject
        {
            ["supportedProtocolVersions"] = new JsonArray(JsonValue.Create(Contract.ProtocolVersion)),
            ["supportedSchemaVersions"] = new JsonArray(JsonValue.Create(Contract.SchemaVersion)),
            ["primitives"] = PrimitiveCapabilities(supportsVisualEvidence),
            ["context"] = CapabilityStates(Contract.ContextNames),
            ["settleConditions"] = CapabilityStates(Contract.SettleConditionNames),
            ["atomicSceneAuthority"] = "unsupported",
            ["uiaGuardedTraversal"] = "unsupported",
            ["losslessVisualEvidence"] = supportsVisualEvidence ? "supported" : "unsupported",
            ["customAdapterNamespaces"] = new JsonArray(),
            ["limits"] = NegotiatedLimits(),
        },
    };

    private static JsonObject CloneCandidate(JsonElement candidate) =>
        JsonNode.Parse(candidate.GetRawText()) as JsonObject
        ?? throw new InvalidOperationException("Native scene binding supplied an invalid candidate projection.");

    private static JsonArray PrimitiveCapabilities(bool supportsVisualEvidence)
    {
        var capabilities = new JsonArray();
        foreach (var primitive in Contract.Primitives)
        {
            capabilities.Add(new JsonObject
            {
                ["name"] = primitive.Name,
                ["milestone"] = primitive.Milestone,
                ["availability"] = StringComparer.Ordinal.Equals(primitive.Name, "get_ui_probe_capabilities") ||
                                   StringComparer.Ordinal.Equals(primitive.Name, "read_capture_artifact") ||
                                   (supportsVisualEvidence && StringComparer.Ordinal.Equals(primitive.Name, "capture_visual_evidence"))
                    ? "supported"
                    : "unsupported",
            });
        }

        return capabilities;
    }

    private static JsonObject CapabilityStates(IEnumerable<string> names)
    {
        var states = new JsonObject();
        foreach (var name in names)
        {
            states[name] = "unsupported";
        }

        return states;
    }

    private static JsonObject NegotiatedLimits()
    {
        var limits = new JsonObject();
        foreach (var (name, value) in Contract.Limits)
        {
            limits[name] = value;
        }

        return limits;
    }

    private static async Task<CallToolResult> CaptureVisualEvidenceAsync(
        NativeSceneSessionBinding binding,
        JsonElement request,
        JsonElement candidate,
        CancellationToken cancellationToken)
    {
        if (!binding.SupportsVisualEvidence)
        {
            return ToolError("capture_visual_evidence", UnsupportedCapability, "Native scene capability is unsupported.");
        }

        var result = await binding.CaptureVisualEvidenceAsync(
            request.GetProperty("sceneRequest"),
            request.GetProperty("evidenceScope"),
            candidate,
            cancellationToken).ConfigureAwait(false);
        return result.Manifest is { } manifest
            ? Success("capture_visual_evidence", manifest)
            : ToolError("capture_visual_evidence", result.Code!, result.Message!);
    }

    private static async Task<CallToolResult> ReadCaptureArtifactAsync(
        NativeSceneSessionBinding binding,
        JsonElement request,
        CancellationToken cancellationToken)
    {

        var result = await binding.ReadCaptureArtifactAsync(
            request.GetProperty("artifactId").GetString()!,
            request.GetProperty("offset").GetInt64(),
            request.GetProperty("maxBytes").GetInt32(),
            cancellationToken).ConfigureAwait(false);
        return result switch
        {
            NativeSceneArtifactReadChunk chunk => Success("read_capture_artifact", new JsonObject
            {
                ["kind"] = chunk.Kind,
                ["artifactId"] = chunk.ArtifactId,
                ["offset"] = chunk.Offset,
                ["bytesRead"] = chunk.BytesRead,
                ["dataBase64"] = chunk.DataBase64,
                ["endOfArtifact"] = chunk.EndOfArtifact,
                ["mediaType"] = chunk.MediaType,
                ["byteLength"] = chunk.ByteLength,
                ["sha256"] = chunk.Sha256,
                ["artifactSchemaVersion"] = chunk.ArtifactSchemaVersion,
            }),
            NativeSceneArtifactReadError error => ToolError("read_capture_artifact", error.Code, error.Message),
            _ => throw new InvalidOperationException("Native scene artifact store returned an unknown result."),
        };
    }

    private static CallToolResult ToolError(string tool, string code, string message) => Emit(
        tool,
        new JsonObject
        {
            ["kind"] = "tool_error",
            ["tool"] = tool,
            ["code"] = code,
            ["message"] = message,
        },
        isError: true);

    private static CallToolResult Success(string tool, JsonObject payload) => Emit(tool, payload, isError: false);

    private static CallToolResult Emit(string tool, JsonObject payload, bool isError)
    {
        var json = payload.ToJsonString();
        if (!NativeSceneContractCatalog.ValidateResult(tool, json).IsValid)
        {
            throw new InvalidOperationException("Native scene result construction drift was detected.");
        }

        return new CallToolResult
        {
            ResultType = "complete",
            IsError = isError,
            Content = [new TextContentBlock { Text = json }],
            StructuredContent = JsonSerializer.SerializeToElement(payload),
        };
    }

    private sealed class FrozenContract
    {
        private FrozenContract(
            byte[] probeSchemaBytes,
            string protocolVersion,
            string schemaVersion,
            IReadOnlyList<Primitive> primitives,
            IReadOnlyList<ToolSchema> toolSchemas,
            IReadOnlyList<string> contextNames,
            IReadOnlyList<string> settleConditionNames,
            IReadOnlyDictionary<string, int> limits)
        {
            ProbeSchemaBytes = probeSchemaBytes;
            ProtocolVersion = protocolVersion;
            SchemaVersion = schemaVersion;
            Primitives = primitives;
            ToolSchemas = toolSchemas;
            ContextNames = contextNames;
            SettleConditionNames = settleConditionNames;
            Limits = limits;
        }

        internal byte[] ProbeSchemaBytes { get; }
        internal string ProtocolVersion { get; }
        internal string SchemaVersion { get; }
        internal IReadOnlyList<Primitive> Primitives { get; }
        internal IReadOnlyList<ToolSchema> ToolSchemas { get; }
        internal IReadOnlyList<string> ContextNames { get; }
        internal IReadOnlyList<string> SettleConditionNames { get; }
        internal IReadOnlyDictionary<string, int> Limits { get; }

        internal static FrozenContract Load()
        {
            var bytes = NativeSceneContractCatalog.GetArtifactBytes(ProbeSchemaArtifact).ToArray();
            using var schema = JsonDocument.Parse(bytes);
            var definitions = schema.RootElement.GetProperty("definitions");
            var primitives = ReadPrimitives(definitions.GetProperty("primitiveCapability"));
            var toolSchemas = new[]
            {
                new ToolSchema("get_ui_probe_capabilities", "getUiProbeCapabilitiesArguments"),
                new ToolSchema("capture_visual_evidence", "captureVisualEvidenceArguments"),
                new ToolSchema("read_capture_artifact", "readCaptureArtifactArguments"),
                new ToolSchema("wait_for_ui_stable", "waitForUiStableArguments"),
                new ToolSchema("capture_element_snapshot", "captureElementSnapshotArguments"),
                new ToolSchema("capture_native_scene", "captureNativeSceneArguments"),
            };

            if (primitives.Count != 6 ||
                primitives.Select(static primitive => primitive.Name).Distinct(StringComparer.Ordinal).Count() != 6 ||
                primitives.Count(static primitive => StringComparer.Ordinal.Equals(primitive.Milestone, "M0")) != 3 ||
                primitives.Count(static primitive => StringComparer.Ordinal.Equals(primitive.Milestone, "M1")) != 3 ||
                !primitives.Select(static primitive => primitive.Name).Order(StringComparer.Ordinal)
                    .SequenceEqual(toolSchemas.Select(static tool => tool.Name).Order(StringComparer.Ordinal)) ||
                toolSchemas.Any(tool => !definitions.TryGetProperty(tool.Definition, out _)))
            {
                throw new InvalidOperationException("Frozen native scene primitive contract drift was detected.");
            }

            var limits = ReadLimits(definitions.GetProperty("negotiatedLimits"));
            if (limits.Count != 11)
            {
                throw new InvalidOperationException("Frozen native scene limit contract drift was detected.");
            }

            return new FrozenContract(
                bytes,
                ReadSingleEnum(definitions.GetProperty("currentProtocolVersion")),
                ReadSingleEnum(definitions.GetProperty("currentSchemaVersion")),
                primitives,
                Array.AsReadOnly(toolSchemas),
                ReadRequiredNames(definitions.GetProperty("contextCapabilities")),
                ReadRequiredNames(definitions.GetProperty("settleConditionCapabilities")),
                limits);
        }

        internal bool IsKnownTool(string tool) => ToolSchemas.Any(candidate => StringComparer.Ordinal.Equals(candidate.Name, tool));

        private static IReadOnlyList<Primitive> ReadPrimitives(JsonElement primitiveCapability)
        {
            var pairs = new List<Primitive>();
            foreach (var branch in primitiveCapability.GetProperty("oneOf").EnumerateArray())
            {
                var properties = branch.GetProperty("properties");
                pairs.Add(new Primitive(
                    ReadSingleEnum(properties.GetProperty("name")),
                    ReadSingleEnum(properties.GetProperty("milestone"))));
            }

            return Array.AsReadOnly(pairs.ToArray());
        }

        private static IReadOnlyList<string> ReadRequiredNames(JsonElement definition)
        {
            var names = new List<string>();
            foreach (var name in definition.GetProperty("required").EnumerateArray())
            {
                names.Add(name.GetString() ?? throw new InvalidOperationException("Frozen native scene capability member is invalid."));
            }

            return Array.AsReadOnly(names.ToArray());
        }

        private static IReadOnlyDictionary<string, int> ReadLimits(JsonElement negotiatedLimits)
        {
            var limits = new Dictionary<string, int>(StringComparer.Ordinal);
            var properties = negotiatedLimits.GetProperty("properties");
            foreach (var name in ReadRequiredNames(negotiatedLimits))
            {
                var limit = properties.GetProperty(name);
                var value = limit.TryGetProperty("const", out var constant)
                    ? constant.GetInt32()
                    : limit.GetProperty("maximum").GetInt32();
                limits.Add(name, value);
            }

            return limits;
        }

        private static string ReadConst(JsonElement definition) => definition.GetProperty("const").GetString()
            ?? throw new InvalidOperationException("Frozen native scene version is invalid.");

        private static string ReadSingleEnum(JsonElement definition)
        {
            var values = definition.GetProperty("enum");
            if (values.GetArrayLength() != 1)
            {
                throw new InvalidOperationException("Frozen native scene enumeration is invalid.");
            }

            return values[0].GetString() ?? throw new InvalidOperationException("Frozen native scene enumeration member is invalid.");
        }
    }

    private sealed record ToolSchema(string Name, string Definition);
    private sealed record Primitive(string Name, string Milestone);
}
