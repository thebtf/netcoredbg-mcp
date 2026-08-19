using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

/// <summary>
/// Qualifies bounded native-scene observations before a server-owned artifact is committed.
/// The binding supplies local probe and guarded-UIA producers; this type owns no process discovery.
/// </summary>
internal sealed class NativeSceneCaptureCoordinator
{
    private const string ArtifactSchemaVersion = "native-scene-artifact/1";
    private const string NativeSceneMediaType = "application/vnd.netcoredbg.native-scene+json";
    private const int MaximumNodes = 4_096;
    private const int MaximumArtifactBytes = 16 * 1024 * 1024;
    private static readonly TimeSpan ArtifactRetention = TimeSpan.FromHours(4);
    private static readonly string[] StabilityConditionNames =
    [
        "dispatcherIdle",
        "stableLayout",
        "animationState",
        "windowGeometry",
        "contextMaterialization",
        "asyncLoadSettled",
    ];

    private readonly NativeSceneArtifactStore _artifactStore;
    private readonly string _debugSessionId;
    private readonly Func<JsonObject> _candidate;
    private readonly Func<NativeSceneTargetIdentity?> _target;
    private readonly Func<CancellationToken, Task<JsonObject?>> _probeProducer;
    private readonly Func<string, JsonObject, CancellationToken, Task<JsonObject?>> _guardedProducer;
    private readonly Action<JsonObject?> _setCaptureStabilityObservation;
    private readonly Func<JsonElement, CancellationToken, Task<JsonObject>> _revalidate;

    internal NativeSceneCaptureCoordinator(
        NativeSceneArtifactStore artifactStore,
        string debugSessionId,
        Func<JsonObject> candidate,
        Func<NativeSceneTargetIdentity?> target,
        Func<CancellationToken, Task<JsonObject?>> probeProducer,
        Func<string, JsonObject, CancellationToken, Task<JsonObject?>> guardedProducer,
        Action<JsonObject?> setCaptureStabilityObservation,
        Func<JsonElement, CancellationToken, Task<JsonObject>> revalidate)
    {
        _artifactStore = artifactStore ?? throw new ArgumentNullException(nameof(artifactStore));
        ArgumentException.ThrowIfNullOrWhiteSpace(debugSessionId);
        _debugSessionId = debugSessionId;
        _candidate = candidate ?? throw new ArgumentNullException(nameof(candidate));
        _target = target ?? throw new ArgumentNullException(nameof(target));
        _probeProducer = probeProducer ?? throw new ArgumentNullException(nameof(probeProducer));
        _guardedProducer = guardedProducer ?? throw new ArgumentNullException(nameof(guardedProducer));
        _setCaptureStabilityObservation = setCaptureStabilityObservation ?? throw new ArgumentNullException(nameof(setCaptureStabilityObservation));
        _revalidate = revalidate ?? throw new ArgumentNullException(nameof(revalidate));
    }

    internal Task<JsonObject> CaptureElementAsync(JsonElement request, CancellationToken cancellationToken) =>
        CaptureAsync(request, isElement: true, cancellationToken);

    internal Task<JsonObject> CaptureNativeSceneAsync(JsonElement request, CancellationToken cancellationToken) =>
        CaptureAsync(request, isElement: false, cancellationToken);

    private async Task<JsonObject> CaptureAsync(JsonElement request, bool isElement, CancellationToken cancellationToken)
    {
        var tool = isElement ? "capture_element_snapshot" : "capture_native_scene";
        var sceneRequest = request.GetProperty("sceneRequest");
        var selector = isElement ? request.GetProperty("element") : default;
        var target = _target();
        if (target is null)
        {
            return ToolError(tool, "CANDIDATE_MISMATCH", "Candidate identity does not match.");
        }

        var probe = await TryProduceAsync(_probeProducer, cancellationToken).ConfigureAwait(false);
        if (NativeSceneProbeChannel.IsTransportFailure(probe, out var code))
        {
            return ToolError(tool, code, "Native-scene probe transport is unavailable.");
        }

        var capturedAt = DateTimeOffset.UtcNow;
        var captureId = CreateOpaqueId();
        var candidate = _candidate();

        if (probe is not null && TryNormalizeProbe(probe, target, out var normalized))
        {
            return await CaptureQualifiedAsync(
                tool,
                sceneRequest,
                selector,
                candidate,
                target,
                captureId,
                capturedAt,
                normalized,
                isElement,
                cancellationToken).ConfigureAwait(false);
        }

        var guardedRequest = CreateGuardedRequest(tool, target, isElement ? selector : default);
        var guarded = await TryProduceAsync(
            token => _guardedProducer(tool, guardedRequest, token),
            cancellationToken).ConfigureAwait(false);
        if (guarded is not null && TryNormalizeGuarded(guarded, target, out var guardedNormalized))
        {
            return await CaptureQualifiedAsync(
                tool,
                sceneRequest,
                selector,
                candidate,
                target,
                captureId,
                capturedAt,
                guardedNormalized,
                isElement,
                cancellationToken).ConfigureAwait(false);
        }

        var unavailableStability = await _revalidate(sceneRequest, cancellationToken).ConfigureAwait(false);
        return CreateUnobservableManifest(
            isElement,
            sceneRequest,
            selector,
            candidate,
            captureId,
            capturedAt,
            isElement
                ? new JsonObject { ["authority"] = "not_applicable" }
                : UnobservableGuardedAtomicity(),
            new JsonArray
            {
                Issue("CONDITION_UNOBSERVABLE", "No qualified local observation channel is available."),
            },
            unavailableStability);
    }

    private async Task<JsonObject> CaptureQualifiedAsync(
        string tool,
        JsonElement sceneRequest,
        JsonElement selector,
        JsonObject candidate,
        NativeSceneTargetIdentity target,
        string captureId,
        DateTimeOffset capturedAt,
        NormalizedCapture normalized,
        bool isElement,
        CancellationToken cancellationToken)
    {
        if (isElement)
        {
            var selected = SelectElement(normalized.Nodes, selector);
            if (selected.Count == 0)
            {
                return ToolError(tool, "ELEMENT_NOT_FOUND", "No element matches the requested selector.");
            }

            if (selected.Count > 1)
            {
                return ToolError(tool, "ELEMENT_AMBIGUOUS", "The requested selector matches multiple elements.");
            }

            normalized = normalized with
            {
                Nodes = new JsonArray(DeepClone(selected[0])),
                RootId = selected[0]!["nodeId"]!.GetValue<string>(),
                Atomicity = new JsonObject { ["authority"] = "not_applicable" },
            };
        }

        _setCaptureStabilityObservation(normalized.StabilityObservation);
        JsonObject stability;
        try
        {
            stability = await _revalidate(sceneRequest, cancellationToken).ConfigureAwait(false);
        }
        finally
        {
            _setCaptureStabilityObservation(null);
        }

        if (!TargetIsCurrent(target))
        {
            return ToolError(tool, "CANDIDATE_MISMATCH", "Candidate identity does not match.");
        }

        if (normalized.Authority == CaptureAuthority.InProcess &&
            !StringComparer.Ordinal.Equals(ReadString(stability, "status"), "STABLE"))
        {
            return ToolError(tool, "UI_NOT_STABLE", "Capture-time stability requirements are not met.");
        }

        var status = ClassifyStatus(normalized, stability, isElement);
        var issues = BuildIssues(normalized, stability, isElement);
        if (status == "UNOBSERVABLE")
        {
            return CreateUnobservableManifest(
                isElement,
                sceneRequest,
                selector,
                candidate,
                captureId,
                capturedAt,
                normalized.Atomicity,
                issues,
                stability);
        }

        var artifact = CreateArtifact(
            isElement ? "element_snapshot" : "native_scene",
            status,
            sceneRequest,
            candidate,
            stability,
            normalized.Atomicity,
            normalized.Nodes,
            normalized.RootId,
            issues,
            captureId,
            capturedAt);
        var artifactBytes = Encoding.UTF8.GetBytes(artifact.ToJsonString());
        if (artifactBytes.Length > MaximumArtifactBytes)
        {
            return CreateUnobservableManifest(
                isElement,
                sceneRequest,
                selector,
                candidate,
                captureId,
                capturedAt,
                normalized.Atomicity,
                new JsonArray
                {
                    Issue("SCENE_GRAPH_INCOMPLETE", "The qualified scene exceeds the native-scene artifact bound."),
                },
                stability);
        }

        NativeSceneArtifactStaging? staged = null;
        NativeSceneArtifactCommitResult commit;
        try
        {
            staged = await _artifactStore.StageAsync(
                _debugSessionId,
                captureId,
                NativeSceneMediaType,
                ArtifactSchemaVersion,
                artifactBytes,
                cancellationToken).ConfigureAwait(false);
            if (!TargetIsCurrent(target))
            {
                return ToolError(tool, "CANDIDATE_MISMATCH", "Candidate identity does not match.");
            }

            commit = await staged.CommitAsync(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException)
        {
            throw;
        }
        catch (Exception)
        {
            return ToolError(tool, "ARTIFACT_WRITE_FAILED", "Artifact could not be committed.");
        }
        finally
        {
            if (staged is { CommitResult: null })
            {
                await staged.AbortAsync(CancellationToken.None).ConfigureAwait(false);
            }
        }

        if (commit.Descriptor is not { } descriptor)
        {
            return ToolError(tool, "ARTIFACT_WRITE_FAILED", "Artifact could not be committed.");
        }

        return CreateManifest(
            isElement,
            status,
            sceneRequest,
            selector,
            candidate,
            stability,
            normalized.Atomicity,
            captureId,
            capturedAt,
            new JsonArray(CreateObservedFactsDescriptor(descriptor, captureId, capturedAt)),
            issues);
    }

    private static async Task<JsonObject?> TryProduceAsync(
        Func<CancellationToken, Task<JsonObject?>> producer,
        CancellationToken cancellationToken)
    {
        try
        {
            return await producer(cancellationToken).ConfigureAwait(false);
        }
        catch (OperationCanceledException) when (cancellationToken.IsCancellationRequested)
        {
            throw;
        }
        catch (OperationCanceledException)
        {
            return null;
        }
        catch (Exception)
        {
            return null;
        }
    }

    private static string ClassifyStatus(NormalizedCapture capture, JsonObject stability, bool isElement)
    {
        if (!StringComparer.Ordinal.Equals(ReadString(stability, "status"), "STABLE"))
        {
            return capture.Nodes.Count == 0 ? "UNOBSERVABLE" : "PARTIAL";
        }

        if (isElement)
        {
            return capture.Complete ? "COMPLETE" : "PARTIAL";
        }

        return capture.Authority == CaptureAuthority.InProcess &&
               capture.Complete &&
               capture.RevisionBefore == capture.RevisionAfter
            ? "COMPLETE"
            : "PARTIAL";
    }

    private static JsonArray BuildIssues(NormalizedCapture capture, JsonObject stability, bool isElement)
    {
        var issues = DeepClone(capture.Issues) as JsonArray ?? new JsonArray();
        if (!StringComparer.Ordinal.Equals(ReadString(stability, "status"), "STABLE"))
        {
            AddIssueOnce(issues, "CAPTURE_REVALIDATION_FAILED", "Capture-time stability could not be fully revalidated.");
        }

        if (capture.Authority == CaptureAuthority.InProcess)
        {
            if (capture.RevisionBefore != capture.RevisionAfter)
            {
                AddIssueOnce(issues, "ATOMICITY_REVISION_CHANGED", "Probe-owned layout/state revision changed during materialization.");
            }

            if (!capture.Complete)
            {
                AddIssueOnce(issues, "SCENE_GRAPH_INCOMPLETE", "The in-process probe did not report complete required facts.");
            }
            if (isElement &&
                capture.RevisionBefore == capture.RevisionAfter &&
                !capture.Complete)
            {
                AddIssueOnce(issues, "ADAPTER_FACT_UNOBSERVABLE", "No qualified element fact could be committed.");
            }
        }

        if (capture.Authority == CaptureAuthority.Guarded)
        {
            AddIssueOnce(issues, "ATOMICITY_UNPROVEN_UIA_GUARDED", "UIA reads are independently timed and cannot prove an atomic framework scene.");
        }

        return issues;
    }

    private static JsonObject CreateManifest(
        bool isElement,
        string status,
        JsonElement sceneRequest,
        JsonElement selector,
        JsonObject candidate,
        JsonObject stability,
        JsonObject atomicity,
        string captureId,
        DateTimeOffset capturedAt,
        JsonArray artifacts,
        JsonArray issues) =>
        new()
        {
            ["kind"] = isElement ? "element_snapshot_capture" : "native_scene_capture",
            ["status"] = status,
            ["captureId"] = captureId,
            ["protocolVersion"] = "native-scene-probe/1",
            ["schemaVersion"] = "native-scene-probe.schema/1",
            ["sceneRequest"] = CloneObject(sceneRequest),
            ["evidenceScope"] = null,
            ["capturedAt"] = Timestamp(capturedAt),
            ["candidate"] = DeepClone(candidate),
            ["stability"] = DeepClone(stability),
            ["atomicity"] = DeepClone(atomicity),
            ["element"] = isElement ? CloneObject(selector) : null,
            ["artifacts"] = artifacts,
            ["issues"] = issues,
        };

    private static JsonObject CreateUnobservableManifest(
        bool isElement,
        JsonElement sceneRequest,
        JsonElement selector,
        JsonObject candidate,
        string captureId,
        DateTimeOffset capturedAt,
        JsonObject atomicity,
        JsonArray issues,
        JsonObject? stability = null) =>
        CreateManifest(
            isElement,
            "UNOBSERVABLE",
            sceneRequest,
            selector,
            candidate,
            stability ?? CreateUnobservableStability(),
            atomicity,
            captureId,
            capturedAt,
            new JsonArray(),
            issues);

    private static JsonObject CreateArtifact(
        string observationKind,
        string status,
        JsonElement sceneRequest,
        JsonObject candidate,
        JsonObject stability,
        JsonObject atomicity,
        JsonArray nodes,
        string rootId,
        JsonArray issues,
        string captureId,
        DateTimeOffset capturedAt) =>
        new()
        {
            ["kind"] = "native_scene_artifact",
            ["schemaVersion"] = ArtifactSchemaVersion,
            ["protocolVersion"] = "native-scene-probe/1",
            ["captureId"] = captureId,
            ["capturedAt"] = Timestamp(capturedAt),
            ["status"] = status,
            ["observationKind"] = observationKind,
            ["sceneRequest"] = CloneObject(sceneRequest),
            ["candidate"] = DeepClone(candidate),
            ["stability"] = DeepClone(stability),
            ["atomicity"] = DeepClone(atomicity),
            ["graph"] = new JsonObject
            {
                ["nodes"] = DeepClone(nodes),
                ["rootNodeIds"] = new JsonArray(rootId),
                ["dependencies"] = new JsonArray(),
            },
            ["issues"] = DeepClone(issues),
        };

    private static JsonObject CreateObservedFactsDescriptor(
        NativeSceneArtifactDescriptor descriptor,
        string captureId,
        DateTimeOffset capturedAt) =>
        new()
        {
            ["artifactId"] = descriptor.ArtifactId,
            ["mediaType"] = descriptor.MediaType,
            ["byteLength"] = descriptor.ByteLength,
            ["sha256"] = descriptor.Sha256,
            ["artifactSchemaVersion"] = descriptor.ArtifactSchemaVersion,
            ["captureId"] = captureId,
            ["retention"] = new JsonObject
            {
                ["endsOn"] = "session_stop_or_expiry",
                ["maximumAgeSeconds"] = (int)ArtifactRetention.TotalSeconds,
                ["expiresAt"] = Timestamp(capturedAt.Add(ArtifactRetention)),
            },
            ["evidenceGrade"] = "observed_facts",
            ["capturedAt"] = Timestamp(capturedAt),
            ["rasterCaptureId"] = null,
            ["adjacentToCaptureId"] = null,
        };

    private static JsonObject CreateGuardedRequest(string tool, NativeSceneTargetIdentity target, JsonElement selector)
    {
        var request = new JsonObject
        {
            ["operation"] = tool,
            ["processId"] = target.ProcessId,
            ["processIdentity"] = target.ProcessIdentity,
            ["hwnd"] = target.ProcessId,
            ["maxNodes"] = MaximumNodes,
        };
        if (selector.ValueKind == JsonValueKind.Object &&
            selector.TryGetProperty("automationId", out var automationId) &&
            automationId.ValueKind == JsonValueKind.String &&
            automationId.GetString() is { Length: > 0 } value)
        {
            request["selector"] = new JsonObject { ["automationId"] = value };
        }

        return request;
    }

    private bool TargetIsCurrent(NativeSceneTargetIdentity expected) =>
        _target() is { } current && current.Equals(expected);

    private static bool TryNormalizeProbe(JsonObject source, NativeSceneTargetIdentity target, out NormalizedCapture capture)
    {
        capture = default!;
        if (!StringComparer.Ordinal.Equals(ReadString(source, "authority"), "in_process_probe") ||
            source["candidate"] is not JsonObject candidate ||
            !TryReadInt32(candidate, "processId", out var processId) || processId != target.ProcessId ||
            !StringComparer.Ordinal.Equals(ReadString(candidate, "processIdentity"), target.ProcessIdentity) ||
            source["process"] is not JsonObject process ||
            !TryReadInt32(process, "processId", out var observedProcessId) || observedProcessId != target.ProcessId ||
            !TryReadInt64(source, "revisionBefore", out var revisionBefore) || revisionBefore < 0 ||
            !TryReadInt64(source, "revisionAfter", out var revisionAfter) || revisionAfter < 0 ||
            !TryReadBoolean(source, "complete", out var complete) ||
            !TryReadNodes(source["nodes"] as JsonArray, MaximumNodes, CaptureAuthority.InProcess, out var nodes) ||
            !TryReadLabel(source, "rootId", out var sourceRootId) ||
            !TryFindNodeIdByContractId(nodes, sourceRootId, out var rootId) ||
            !TryCloneStabilityObservation(source["stability"] as JsonObject, out var stabilityObservation))
        {
            return false;
        }

        capture = new NormalizedCapture(
            nodes,
            rootId,
            complete && revisionBefore == revisionAfter,
            revisionBefore,
            revisionAfter,
            CaptureAuthority.InProcess,
            new JsonObject
            {
                ["authority"] = "in_process_framework_probe",
                ["transaction"] = "dispatcher_affine_non_yielding",
                ["immutableDto"] = true,
                ["layoutStateRevisionBefore"] = revisionBefore,
                ["layoutStateRevisionAfter"] = revisionAfter,
            },
            new JsonArray(),
            stabilityObservation);
        return true;
    }

    private static bool TryNormalizeGuarded(JsonObject source, NativeSceneTargetIdentity target, out NormalizedCapture capture)
    {
        capture = default!;
        if (!StringComparer.Ordinal.Equals(ReadString(source, "kind"), "uia_guarded_observation") ||
            !StringComparer.Ordinal.Equals(ReadString(source, "authority"), "uia_guarded") ||
            !StringComparer.Ordinal.Equals(ReadString(source, "qualification"), "PARTIAL") ||
            source["process"] is not JsonObject process ||
            !TryReadInt32(process, "processId", out var processId) || processId != target.ProcessId ||
            !StringComparer.Ordinal.Equals(ReadString(process, "processIdentity"), target.ProcessIdentity) ||
            !TryReadLabel(source, "rootId", out var rootId) ||
            !TryReadGuardedNodes(source["nodes"] as JsonArray, rootId, out var nodes) ||
            !ContainsNode(nodes, rootId) ||
            !HasUnchangedGuards(source["guards"] as JsonObject) ||
            !TryCloneStabilityObservation(source["stability"] as JsonObject, out var stabilityObservation))
        {
            return false;
        }

        capture = new NormalizedCapture(
            Nodes: nodes,
            RootId: rootId,
            Complete: false,
            RevisionBefore: 0,
            RevisionAfter: 0,
            Authority: CaptureAuthority.Guarded,
            Atomicity: GuardedAtomicity("unchanged"),
            Issues: new JsonArray
            {
                Issue("ATOMICITY_UNPROVEN_UIA_GUARDED", "UIA reads are independently timed and cannot prove an atomic framework scene."),
            },
            StabilityObservation: stabilityObservation);
        return true;
    }

    private static bool TryReadNodes(JsonArray? source, int maximumNodes, CaptureAuthority authority, out JsonArray nodes)
    {
        nodes = new JsonArray();
        if (source is null || source.Count is < 1 or > MaximumNodes || source.Count > maximumNodes)
        {
            return false;
        }

        var ids = new HashSet<string>(StringComparer.Ordinal);
        var index = 0;
        foreach (var item in source)
        {
            if (item is not JsonObject node ||
                !TryReadLabel(node, "id", out var id) ||
                !TryReadFiniteNumber(node, "x", out var x) ||
                !TryReadFiniteNumber(node, "y", out var y) ||
                !TryReadFiniteNumber(node, "width", out var width) || width < 0 ||
                !TryReadFiniteNumber(node, "height", out var height) || height < 0)
            {
                return false;
            }

            var nodeId = CreateNodeId(id, index++);
            if (!ids.Add(nodeId))
            {
                return false;
            }

            nodes.Add(new JsonObject
            {
                ["nodeId"] = nodeId,
                ["relations"] = new JsonArray(),
                ["identity"] = new JsonObject { ["contractId"] = id },
                ["accessibility"] = new JsonObject
                {
                    ["automationId"] = CloneBoundedArtifactString(node["automationId"]),
                    ["name"] = CloneBoundedArtifactString(node["accessibleName"]),
                    ["controlType"] = null,
                    ["visibility"] = "visible",
                },
                ["geometry"] = new JsonObject
                {
                    ["logicalBounds"] = Rect(x, y, width, height),
                    ["physicalBounds"] = null,
                    ["dpi"] = null,
                    ["transform"] = null,
                    ["clip"] = null,
                },
                ["adapterEvidence"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["namespace"] = "netcoredbg.wpf.probe",
                        ["schemaVersion"] = "1",
                        ["authority"] = authority == CaptureAuthority.InProcess ? "in_process_framework_probe" : "uia_guarded",
                        ["payload"] = CreateProbeAdapterPayload(node),
                    },
                },
            });
        }

        return true;
    }

    private static JsonObject CreateProbeAdapterPayload(JsonObject node)
    {
        var payload = new JsonObject
        {
            ["text"] = CloneBoundedArtifactString(node["text"]),
        };
        AddStringChunks(payload, "automationIdChunks", node["automationId"]);
        AddStringChunks(payload, "accessibleNameChunks", node["accessibleName"]);
        AddStringChunks(payload, "textChunks", node["text"]);
        return payload;
    }

    private static JsonNode? CloneBoundedArtifactString(JsonNode? source)
    {
        if (source is not JsonValue value || !value.TryGetValue<string>(out var text))
        {
            return null;
        }

        var end = FindUtf16SafeChunkEnd(text, 0, 256);
        return end == text.Length ? text : text[..end];
    }

    private static void AddStringChunks(JsonObject payload, string propertyName, JsonNode? source)
    {
        if (source is not JsonValue value || !value.TryGetValue<string>(out var text) || text.Length <= 256)
        {
            return;
        }
        var chunks = new JsonArray();
        for (var offset = 0; offset < text.Length;)
        {
            var end = FindUtf16SafeChunkEnd(text, offset, 256);
            chunks.Add(text[offset..end]);
            offset = end;
        }

        payload[propertyName] = chunks;
    }

    private static int FindUtf16SafeChunkEnd(string text, int start, int maximumCodeUnits)
    {
        var end = Math.Min(text.Length, start + maximumCodeUnits);
        if (end < text.Length && end > start && char.IsHighSurrogate(text[end - 1]) && char.IsLowSurrogate(text[end]))
        {
            end--;
        }

        return end;
    }


    private static bool TryReadGuardedNodes(JsonArray? source, string rootId, out JsonArray nodes)
    {
        nodes = new JsonArray();
        if (source is null || source.Count is < 1 or > MaximumNodes)
        {
            return false;
        }

        var ids = new HashSet<string>(StringComparer.Ordinal);
        foreach (var item in source)
        {
            if (item is not JsonObject node ||
                !TryReadLabel(node, "id", out var id) ||
                !ids.Add(id) ||
                node["identity"] is not JsonObject identity ||
                node["geometry"] is not JsonObject geometry)
            {
                return false;
            }

            var relations = new JsonArray();
            if (node["parentId"] is JsonValue parent && parent.TryGetValue<string>(out var parentId))
            {
                if (!IsLabel(parentId))
                {
                    return false;
                }

                relations.Add(new JsonObject { ["kind"] = "parent", ["targetNodeId"] = parentId });
            }

            var visibility = node["accessibility"] is JsonObject accessibility &&
                             accessibility["isOffscreen"] is JsonValue offscreen &&
                             offscreen.TryGetValue<bool>(out var isOffscreen)
                ? isOffscreen ? "hidden" : "visible"
                : "unobservable";
            nodes.Add(new JsonObject
            {
                ["nodeId"] = id,
                ["relations"] = relations,
                ["identity"] = null,
                ["accessibility"] = new JsonObject
                {
                    ["automationId"] = identity["automationId"]?.DeepClone(),
                    ["name"] = identity["name"]?.DeepClone(),
                    ["controlType"] = identity["controlType"]?.DeepClone(),
                    ["visibility"] = visibility,
                },
                ["geometry"] = new JsonObject
                {
                    ["logicalBounds"] = geometry["logical"]?.DeepClone(),
                    ["physicalBounds"] = geometry["physical"]?.DeepClone(),
                    ["dpi"] = TryReadInt32(geometry, "dpi", out var dpi) && dpi > 0
                        ? new JsonObject { ["x"] = dpi, ["y"] = dpi }
                        : null,
                    ["transform"] = null,
                    ["clip"] = null,
                },
                ["adapterEvidence"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["namespace"] = "netcoredbg.uia",
                        ["schemaVersion"] = "1",
                        ["authority"] = "uia_guarded",
                        ["payload"] = new JsonObject
                        {
                            ["transform"] = node["transform"]?.DeepClone(),
                            ["clip"] = node["clip"]?.DeepClone(),
                        },
                    },
                },
            });
        }

        var knownIds = new HashSet<string>(nodes.Select(static node => node!["nodeId"]!.GetValue<string>()), StringComparer.Ordinal);
        var parentByNodeId = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var node in nodes.OfType<JsonObject>())
        {
            var nodeId = node["nodeId"]!.GetValue<string>();
            foreach (var relation in node["relations"]!.AsArray().OfType<JsonObject>())
            {
                var parentId = relation["targetNodeId"]!.GetValue<string>();
                if (!knownIds.Contains(parentId) || !parentByNodeId.TryAdd(nodeId, parentId))
                {
                    return false;
                }
            }
        }

        if (parentByNodeId.ContainsKey(rootId))
        {
            return false;
        }

        foreach (var nodeId in knownIds)
        {
            var visited = new HashSet<string>(StringComparer.Ordinal);
            var current = nodeId;
            while (parentByNodeId.TryGetValue(current, out var parentId))
            {
                if (!visited.Add(current))
                {
                    return false;
                }

                current = parentId;
            }

            if (!StringComparer.Ordinal.Equals(current, rootId))
            {
                return false;
            }
        }

        return true;
    }

    private static List<JsonObject?> SelectElement(JsonArray nodes, JsonElement selector)
    {
        var matches = new List<JsonObject?>();
        var contractId = ReadOptionalString(selector, "contractId");
        var automationId = ReadOptionalString(selector, "automationId");
        foreach (var node in nodes.OfType<JsonObject>())
        {
            var nodeContractId = node["identity"] is JsonObject identity ? ReadString(identity, "contractId") : null;
            var nodeAutomationId = node["accessibility"] is JsonObject accessibility ? ReadString(accessibility, "automationId") : null;
            if ((contractId is null || StringComparer.Ordinal.Equals(contractId, nodeContractId)) &&
                (automationId is null || StringComparer.Ordinal.Equals(automationId, nodeAutomationId)))
            {
                var selected = DeepClone(node) as JsonObject;
                if (selected is not null)
                {
                    selected["relations"] = new JsonArray();
                    matches.Add(selected);
                }
            }
        }

        return matches;
    }

    private static bool HasUnchangedGuards(JsonObject? guards)
    {
        if (guards?["before"] is not JsonObject before || guards["after"] is not JsonObject after || !JsonNode.DeepEquals(before, after))
        {
            return false;
        }

        return TryReadInt64(before, "hwnd", out var hwnd) && hwnd != 0 &&
               before["windowRect"] is JsonObject &&
               before["clientRect"] is JsonObject &&
               TryReadInt32(before, "dpi", out var dpi) && dpi > 0 &&
               TryReadLabel(before, "visualTreeFingerprint", out _);
    }

    private static JsonObject GuardedAtomicity(string state) => new()
    {
        ["authority"] = "uia_guarded",
        ["guards"] = GuardStates(state),
    };

    private static JsonObject UnobservableGuardedAtomicity() => GuardedAtomicity("unobservable");

    private static JsonObject GuardStates(string state) => new()
    {
        ["window"] = new JsonObject { ["state"] = state },
        ["client"] = new JsonObject { ["state"] = state },
        ["dpi"] = new JsonObject { ["state"] = state },
        ["visualTreeFingerprint"] = new JsonObject { ["state"] = state },
    };

    internal static bool TryCloneStabilityObservation(JsonObject? source, out JsonObject observation)
    {
        observation = default!;
        if (source is null ||
            source.Count != 2 ||
            !TryReadSceneEpoch(source, out var sceneEpoch) ||
            source["conditions"] is not JsonObject conditions ||
            conditions.Count != StabilityConditionNames.Length)
        {
            return false;
        }

        var clonedConditions = new JsonObject();
        foreach (var name in StabilityConditionNames)
        {
            if (conditions[name] is not JsonObject condition ||
                condition.Count != 1 ||
                !TryReadStabilityState(condition, out var state))
            {
                return false;
            }

            clonedConditions[name] = new JsonObject { ["state"] = state };
        }

        observation = new JsonObject
        {
            ["sceneEpoch"] = sceneEpoch,
            ["conditions"] = clonedConditions,
        };
        return true;
    }

    private static bool TryReadStabilityState(JsonObject source, out string state)
    {
        state = ReadString(source, "state") ?? string.Empty;
        return state is "met" or "not_met" or "unsupported" or "unobservable";
    }

    private static JsonObject CreateUnobservableStability() => new()
    {
        ["status"] = "UNOBSERVABLE",
        ["revalidatedByCapture"] = true,
        ["conditions"] = new JsonObject
        {
            ["dispatcherIdle"] = new JsonObject { ["state"] = "unobservable" },
            ["stableLayout"] = new JsonObject { ["state"] = "unobservable" },
            ["animationState"] = new JsonObject { ["state"] = "unobservable" },
            ["windowGeometry"] = new JsonObject { ["state"] = "unobservable" },
            ["contextMaterialization"] = new JsonObject { ["state"] = "unobservable" },
            ["asyncLoadSettled"] = new JsonObject { ["state"] = "unobservable" },
        },
        ["settleDurationMs"] = 0,
        ["observedAt"] = Timestamp(DateTimeOffset.UtcNow),
        ["sceneEpoch"] = 0L,
        ["sequence"] = 0,
    };

    private static JsonObject Issue(string code, string message) => new()
    {
        ["code"] = code,
        ["message"] = message,
    };

    private static void AddIssueOnce(JsonArray issues, string code, string message)
    {
        if (!issues.OfType<JsonObject>().Any(issue => StringComparer.Ordinal.Equals(ReadString(issue, "code"), code)))
        {
            issues.Add(Issue(code, message));
        }
    }

    private static JsonObject ToolError(string tool, string code, string message) => new()
    {
        ["kind"] = "tool_error",
        ["tool"] = tool,
        ["code"] = code,
        ["message"] = message,
    };

    private static bool TryFindNodeIdByContractId(JsonArray nodes, string contractId, out string nodeId)
    {
        foreach (var node in nodes.OfType<JsonObject>())
        {
            if (node["identity"] is JsonObject identity &&
                StringComparer.Ordinal.Equals(ReadString(identity, "contractId"), contractId) &&
                ReadString(node, "nodeId") is { } matched)
            {
                nodeId = matched;
                return true;
            }
        }

        nodeId = string.Empty;
        return false;
    }

    private static string CreateNodeId(string source, int index)
    {
        var builder = new StringBuilder(source.Length + 12);
        foreach (var character in source)
        {
            builder.Append(char.IsAsciiLetterOrDigit(character) || character is '_' or '-' ? character : '_');
        }

        builder.Append('_').Append(index);
        return builder.ToString();
    }

    private static bool ContainsNode(JsonArray nodes, string rootId) =>
        nodes.OfType<JsonObject>().Any(node => StringComparer.Ordinal.Equals(ReadString(node, "nodeId"), rootId));

    private static JsonObject Rect(double x, double y, double width, double height) => new()
    {
        ["x"] = x,
        ["y"] = y,
        ["width"] = width,
        ["height"] = height,
    };

    private static JsonObject CloneObject(JsonElement source) => JsonNode.Parse(source.GetRawText())!.AsObject();

    private static JsonNode? DeepClone(JsonNode? source) => source?.DeepClone();

    private static string Timestamp(DateTimeOffset value) => value.ToString("O", System.Globalization.CultureInfo.InvariantCulture);

    private static string CreateOpaqueId()
    {
        Span<byte> bytes = stackalloc byte[16];
        try
        {
            RandomNumberGenerator.Fill(bytes);
            return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }
        finally
        {
            CryptographicOperations.ZeroMemory(bytes);
        }
    }

    private static string? ReadOptionalString(JsonElement source, string name) =>
        source.TryGetProperty(name, out var value) && value.ValueKind == JsonValueKind.String
            ? value.GetString()
            : null;

    private static string? ReadString(JsonObject source, string name) =>
        source[name] is JsonValue value && value.TryGetValue<string>(out var text) ? text : null;

    private static bool TryReadBoolean(JsonObject source, string name, out bool value)
    {
        value = false;
        return source[name] is JsonValue node && node.TryGetValue(out value);
    }

    private static bool TryReadInt32(JsonObject source, string name, out int value)
    {
        value = 0;
        return source[name] is JsonValue node && node.TryGetValue(out value);
    }

    private static bool TryReadInt64(JsonObject source, string name, out long value)
    {
        value = 0;
        return source[name] is JsonValue node && node.TryGetValue(out value);
    }

    private static bool TryReadSceneEpoch(JsonObject source, out long value)
    {
        if (source["sceneEpoch"] is JsonValue node)
        {
            if (node.TryGetValue<long>(out value) && value >= 0)
            {
                return true;
            }

            if (node.TryGetValue<int>(out var intValue) && intValue >= 0)
            {
                value = intValue;
                return true;
            }
        }

        value = 0;
        return false;
    }

    private static bool TryReadFiniteNumber(JsonObject source, string name, out double value)
    {
        value = 0;
        return source[name] is JsonValue node && node.TryGetValue(out value) && double.IsFinite(value);
    }

    private static bool TryReadLabel(JsonObject source, string name, out string value)
    {
        value = ReadString(source, name) ?? string.Empty;
        return IsLabel(value);
    }

    private static bool IsLabel(string value)
    {
        if (string.IsNullOrWhiteSpace(value) || value.Length > 256)
        {
            return false;
        }

        return value.All(static character => !char.IsControl(character));
    }

    private enum CaptureAuthority
    {
        InProcess,
        Guarded,
    }

    private sealed record NormalizedCapture(
        JsonArray Nodes,
        string RootId,
        bool Complete,
        long RevisionBefore,
        long RevisionAfter,
        CaptureAuthority Authority,
        JsonObject Atomicity,
        JsonArray Issues,
        JsonObject StabilityObservation);
}
