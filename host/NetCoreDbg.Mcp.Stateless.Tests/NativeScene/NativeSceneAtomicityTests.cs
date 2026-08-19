using System.Reflection;
using System.Runtime.Loader;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NJsonSchema;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

/// <summary>
/// RED-first M1 contract for the T024 WPF fixture. The fixture is launched only
/// as the controlled adapter's owned local descendant, so the DAP process event
/// supplies the explicit session candidate without discovery or attachment.
/// </summary>
[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
public sealed class NativeSceneAtomicityTests
{
    private const string ActiveProtocolVersion = "native-scene-probe/1";
    private const string ActiveSchemaVersion = "native-scene-probe.schema/1";
    private const string NativeSceneMediaType = "application/vnd.netcoredbg.native-scene+json";
    private const string ObservedFactsEvidenceGrade = "observed_facts";
    private const int ArtifactReadMaxBytes = 65_536;

    [Fact]
    public async Task StableFixture_UniqueElementCapture_ReturnsACompleteOneNodeObservedFactsArtifact()
    {
        await using var driver = await StartFixtureDriverAsync("stable");
        var session = await StartBoundFixtureSessionAsync(driver, "atomic-element-complete-start");
        var element = ElementSelector(contractId: "Button.Primary", automationId: "SaveButton");
        var request = ElementCaptureArguments(session, element);

        var manifest = await CallCaptureAsync(
            driver,
            "capture_element_snapshot",
            request,
            "atomic-element-complete-capture",
            expectedError: false);

        AssertSchemaValid("capture_element_snapshot", manifest);
        AssertElementManifest(manifest, session, request, element, "COMPLETE");
        var descriptor = AssertObservedFactsDescriptor(manifest);
        var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);

        AssertSceneArtifact(artifact, manifest, "element_snapshot", "COMPLETE", maximumNodes: 1);
        var graph = Object(artifact["graph"]);
        Assert.Single(Array(graph["nodes"]));
        Assert.Single(Array(graph["rootNodeIds"]));
        AssertObservationOnly(manifest);
        AssertObservationOnly(artifact);
    }

    [Fact]
    public async Task IncompleteFixture_ElementCaptureIsPartialAndMayRetainOnlyQualifiedFacts()
    {
        await using var driver = await StartFixtureDriverAsync("incomplete");
        var session = await StartBoundFixtureSessionAsync(driver, "atomic-element-partial-start");
        var element = ElementSelector(contractId: "Button.Primary", automationId: "SaveButton");
        var request = ElementCaptureArguments(session, element);

        var manifest = await CallCaptureAsync(
            driver,
            "capture_element_snapshot",
            request,
            "atomic-element-partial-capture",
            expectedError: false);

        AssertSchemaValid("capture_element_snapshot", manifest);
        AssertElementManifest(manifest, session, request, element, "PARTIAL");
        foreach (var descriptor in ObservedFactsDescriptors(manifest))
        {
            var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);
            AssertSceneArtifact(artifact, manifest, "element_snapshot", "PARTIAL", maximumNodes: 1);
        }

        AssertObservationOnly(manifest);
    }

    [Theory]
    [InlineData("MissingButton", "ELEMENT_NOT_FOUND")]
    [InlineData("AmbiguousButton", "ELEMENT_AMBIGUOUS")]
    public async Task MissingOrAmbiguousElement_IsATypeSpecificCaptureErrorWithoutArtifactAuthority(
        string automationId,
        string expectedCode)
    {
        await using var driver = await StartFixtureDriverAsync("stable");
        var session = await StartBoundFixtureSessionAsync(driver, $"atomic-element-{expectedCode.ToLowerInvariant()}-start");
        var element = ElementSelector(contractId: null, automationId);

        var error = await CallCaptureAsync(
            driver,
            "capture_element_snapshot",
            ElementCaptureArguments(session, element),
            $"atomic-element-{expectedCode.ToLowerInvariant()}-capture",
            expectedError: true);

        AssertSchemaValid("capture_element_snapshot", error);
        Assert.Equal("tool_error", Text(error["kind"]));
        Assert.Equal("capture_element_snapshot", Text(error["tool"]));
        Assert.Equal(expectedCode, Text(error["code"]));
        Assert.DoesNotContain(
            EnumeratePropertyNames(error),
            name => name is "artifactId" or "artifacts" or "captureId" or "dataBase64" or "path" or "root");
    }

    [Fact]
    public async Task StableFixture_EqualProbeRevisionsProduceTheOnlyCompleteAtomicNativeScene()
    {
        await using var driver = await StartFixtureDriverAsync("stable");
        var session = await StartBoundFixtureSessionAsync(driver, "atomic-scene-complete-start");
        var request = NativeSceneCaptureArguments(session);

        var manifest = await CallCaptureAsync(
            driver,
            "capture_native_scene",
            request,
            "atomic-scene-complete-capture",
            expectedError: false);

        AssertSchemaValid("capture_native_scene", manifest);
        AssertNativeSceneManifest(manifest, session, request, "COMPLETE");
        AssertInProcessAtomicity(manifest, expectedRevisionBefore: 77, expectedRevisionAfter: 77);
        Assert.Empty(Array(manifest["issues"]));
        var descriptor = AssertObservedFactsDescriptor(manifest);
        var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);

        AssertSceneArtifact(artifact, manifest, "native_scene", "COMPLETE", maximumNodes: 4_096);
        AssertInProcessAtomicity(artifact, expectedRevisionBefore: 77, expectedRevisionAfter: 77);
        AssertObservationOnly(manifest);
        AssertObservationOnly(artifact);
    }

    [Theory]
    [InlineData("changed-before")]
    [InlineData("changed-after")]
    public async Task ChangedProbeRevision_NeverClaimsCompleteAtomicity(string mode)
    {
        await using var driver = await StartFixtureDriverAsync(mode);
        var session = await StartBoundFixtureSessionAsync(driver, $"atomic-scene-{mode}-start");
        var request = NativeSceneCaptureArguments(session);

        var manifest = await CallCaptureAsync(
            driver,
            "capture_native_scene",
            request,
            $"atomic-scene-{mode}-capture",
            expectedError: false);

        AssertSchemaValid("capture_native_scene", manifest);
        AssertNativeSceneManifest(manifest, session, request, "PARTIAL");
        AssertInProcessAtomicity(manifest, expectedRevisionBefore: 77, expectedRevisionAfter: 78);
        Assert.Contains(Array(manifest["issues"]).Select(Object), issue => Text(issue["code"]) == "ATOMICITY_REVISION_CHANGED");
        foreach (var descriptor in ObservedFactsDescriptors(manifest))
        {
            var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);
            AssertSceneArtifact(artifact, manifest, "native_scene", "PARTIAL", maximumNodes: 4_096);
        }

        AssertObservationOnly(manifest);
    }

    [Fact]
    public async Task IncompleteProbeEvidence_NeverClaimsACompleteNativeScene()
    {
        await using var driver = await StartFixtureDriverAsync("incomplete");
        var session = await StartBoundFixtureSessionAsync(driver, "atomic-scene-incomplete-start");
        var request = NativeSceneCaptureArguments(session);

        var manifest = await CallCaptureAsync(
            driver,
            "capture_native_scene",
            request,
            "atomic-scene-incomplete-capture",
            expectedError: false);

        AssertSchemaValid("capture_native_scene", manifest);
        AssertNativeSceneManifest(manifest, session, request, expectedStatus: null);
        Assert.NotEqual("COMPLETE", Text(manifest["status"]));
        Assert.Contains(Text(manifest["status"]), new[] { "PARTIAL", "UNOBSERVABLE" });
        foreach (var descriptor in ObservedFactsDescriptors(manifest))
        {
            var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);
            AssertSceneArtifact(artifact, manifest, "native_scene", Text(manifest["status"]), maximumNodes: 4_096);
        }

        AssertObservationOnly(manifest);
    }

    [Fact]
    public void UiaGuardedQualification_CannotClaimAtomicCompleteAndCommitsNothingWhenAGuardChangesOrIsUnusable()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var guarded = CorpusResponse(catalog, "C023-partial-capture-without-revalidation-is-schema-rejected");
        Object(guarded["stability"])["revalidatedByCapture"] = true;

        AssertSchemaValid("capture_native_scene", guarded);
        Assert.Equal("PARTIAL", Text(guarded["status"]));
        Assert.Equal("uia_guarded", Text(Object(guarded["atomicity"])["authority"]));
        Assert.Contains(Array(guarded["issues"]).Select(Object), issue => Text(issue["code"]) == "ATOMICITY_UNPROVEN_UIA_GUARDED");

        var claimedAtomic = guarded.DeepClone().AsObject();
        claimedAtomic["status"] = "COMPLETE";
        AssertCatalogRejects(catalog, "capture_native_scene", claimedAtomic);

        foreach (var guardState in new[] { "changed", "unobservable" })
        {
            var unobservable = guarded.DeepClone().AsObject();
            unobservable["status"] = "UNOBSERVABLE";
            unobservable["artifacts"] = new JsonArray();
            Object(Object(unobservable["atomicity"])["guards"])["window"] = new JsonObject { ["state"] = guardState };

            AssertSchemaValid("capture_native_scene", unobservable);
            Assert.Equal("UNOBSERVABLE", Text(unobservable["status"]));
            Assert.Empty(Array(unobservable["artifacts"]));
        }

        var zeroGuardClaim = guarded.DeepClone().AsObject();
        zeroGuardClaim["status"] = "COMPLETE";
        zeroGuardClaim["atomicity"] = new JsonObject
        {
            ["authority"] = "uia_guarded",
            ["guards"] = new JsonObject(),
        };
        AssertCatalogRejects(catalog, "capture_native_scene", zeroGuardClaim);

        var unobservableElement = CorpusVariantResponse(catalog, "C016-observation-only-result", "unobservable_with_committed_artifact");
        unobservableElement["artifacts"] = new JsonArray();
        AssertSchemaValid("capture_element_snapshot", unobservableElement);
        Assert.Equal("UNOBSERVABLE", Text(unobservableElement["status"]));
        Assert.Empty(Array(unobservableElement["artifacts"]));
        AssertObservationOnly(guarded);
    }

    [Fact]
    public async Task GuardedUiaCyclicGraphs_AreUnobservableAndNeverCommitArtifactAuthority()
    {
        await using var artifacts = ArtifactStoreTestScope.Create();
        foreach (var guarded in new[]
                 {
                     GuardedObservation(GuardedNode("root", parentId: "root")),
                     GuardedObservation(
                         GuardedNode("root", parentId: null),
                         GuardedNode("left", parentId: "right"),
                         GuardedNode("right", parentId: "left")),
                 })
        {
            Assert.True(JsonNode.DeepEquals(Object(Object(guarded["guards"])["before"]), Object(Object(guarded["guards"])["after"])));

            var manifest = await NativeSceneAtomicityReflection.CaptureGuardedNativeSceneAsync(artifacts.Store, guarded);

            Assert.Equal("UNOBSERVABLE", Text(manifest["status"]));
            Assert.Empty(Array(manifest["artifacts"]));
            Assert.Equal(0, artifacts.Store.CommittedArtifactMetadataCount);
        }
    }

    [Fact]
    public void FutureCaptureCoordinator_UsesTheBoundedT028InternalContract()
    {
        NativeSceneAtomicityReflection.RequireT028Contract();
    }

    private static JsonObject GuardedObservation(params JsonObject[] nodes)
    {
        var before = new JsonObject
        {
            ["hwnd"] = 1L,
            ["windowRect"] = new JsonObject(),
            ["clientRect"] = new JsonObject(),
            ["dpi"] = 96,
            ["visualTreeFingerprint"] = "unchanged",
        };
        return new JsonObject
        {
            ["kind"] = "uia_guarded_observation",
            ["authority"] = "uia_guarded",
            ["qualification"] = "PARTIAL",
            ["process"] = new JsonObject
            {
                ["processId"] = 73,
                ["processIdentity"] = "guarded-target",
            },
            ["rootId"] = "root",
            ["nodes"] = new JsonArray(nodes),
            ["guards"] = new JsonObject
            {
                ["before"] = before,
                ["after"] = before.DeepClone(),
            },
        };
    }

    private static JsonObject GuardedNode(string id, string? parentId)
    {
        var node = new JsonObject
        {
            ["id"] = id,
            ["identity"] = new JsonObject(),
            ["geometry"] = new JsonObject(),
        };
        if (parentId is not null)
        {
            node["parentId"] = parentId;
        }

        return node;
    }

    private static async Task<BoundFixtureSession> StartBoundFixtureSessionAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var fixtureExecutable = ResolveFixtureExecutablePath();
        var started = await CallCaptureAsync(
            driver,
            "start_debug",
            new JsonObject { ["program"] = fixtureExecutable },
            requestId,
            expectedError: false,
            requireNativeSceneSchema: false);
        Assert.Equal("start_debug_success", Text(started["kind"]));
        var debugSessionId = Text(started["debugSessionId"]);

        var declaration = await CallCaptureAsync(
            driver,
            "get_ui_probe_capabilities",
            CapabilityArguments(debugSessionId),
            $"{requestId}-capabilities",
            expectedError: false);
        var candidate = Object(declaration["candidate"]);
        Assert.Equal(await driver.ReadDescendantProcessIdAsync(), Integer(candidate["processId"]));
        return new BoundFixtureSession(debugSessionId, candidate);
    }

    private static Task<ModernMcpProcessDriver> StartFixtureDriverAsync(string mode) =>
        ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(
                    SpawnWindowedDescendant: true,
                    WindowedDescendantExecutablePath: ResolveFixtureExecutablePath(),
                    WindowedDescendantArguments:
                    [
                        "--native-scene-probe-test-harness",
                        $"--native-scene-probe-mode={mode}",
                    ])));

    private static string ResolveFixtureExecutablePath()
    {
        var configuration = new DirectoryInfo(AppContext.BaseDirectory).Parent?.Name
            ?? throw new InvalidOperationException("Test output configuration is absent.");
        var path = Path.Combine(
            RepositoryLayout.Root,
            "host",
            "NetCoreDbg.Mcp.Stateless.Tests",
            "Fixtures",
            "NativeSceneProbe.WpfFixture",
            "bin",
            configuration,
            "net8.0-windows",
            "NativeSceneProbe.WpfFixture.exe");
        Assert.True(File.Exists(path), $"Built WPF fixture is absent: '{path}'.");
        return path;
    }

    private static JsonObject ElementCaptureArguments(BoundFixtureSession session, JsonObject element) => new()
    {
        ["debugSessionId"] = session.DebugSessionId,
        ["protocolVersion"] = ActiveProtocolVersion,
        ["schemaVersion"] = ActiveSchemaVersion,
        ["sceneRequest"] = SceneRequest(session.Candidate),
        ["element"] = element.DeepClone(),
    };

    private static JsonObject NativeSceneCaptureArguments(BoundFixtureSession session) => new()
    {
        ["debugSessionId"] = session.DebugSessionId,
        ["protocolVersion"] = ActiveProtocolVersion,
        ["schemaVersion"] = ActiveSchemaVersion,
        ["sceneRequest"] = SceneRequest(session.Candidate),
    };


    private static JsonObject CapabilityArguments(string debugSessionId) => new()
    {
        ["debugSessionId"] = debugSessionId,
        ["protocolVersion"] = ActiveProtocolVersion,
        ["schemaVersion"] = ActiveSchemaVersion,
    };

    private static JsonObject ElementSelector(string? contractId, string automationId) => new()
    {
        ["contractId"] = contractId,
        ["instanceKey"] = contractId is null ? null : "save",
        ["templatePart"] = null,
        ["storyScope"] = contractId is null ? null : "Button.Primary.Default",
        ["componentSlot"] = contractId is null ? null : "content",
        ["automationId"] = automationId,
    };

    private static JsonObject SceneRequest(JsonObject candidate) => new()
    {
        ["storyId"] = "Button.Primary",
        ["sceneId"] = "Default",
        ["fixtureId"] = "Gallery",
        ["scope"] = new JsonObject { ["kind"] = "scene" },
        ["appearance"] = "light",
        ["theme"] = "baseline",
        ["density"] = "regular",
        ["contrast"] = "standard",
        ["viewport"] = new JsonObject
        {
            ["policy"] = "exact",
            ["width"] = 1280,
            ["height"] = 720,
        },
        ["expectedDpiPolicy"] = new JsonObject
        {
            ["mode"] = "exact",
            ["x"] = 96,
            ["y"] = 96,
        },
        ["focusTarget"] = null,
        ["selectedState"] = null,
        ["currentState"] = null,
        ["scrollOffsets"] = null,
        ["animationPolicy"] = "finished",
        ["settlePolicy"] = new JsonObject
        {
            ["timeoutMs"] = 30_000,
            ["sampleCount"] = 2,
            ["stableForMs"] = 100,
            ["requireDispatcherIdle"] = true,
            ["requireStableLayout"] = true,
            ["requireAnimationState"] = true,
            ["requireWindowGeometry"] = true,
            ["requireContextMaterialization"] = true,
            ["requireAsyncLoadSettled"] = true,
        },
        ["contractSetHash"] = new string('a', 64),
        ["expectedCandidateIdentity"] = new JsonObject
        {
            ["executableSha256"] = candidate["executableSha256"]?.DeepClone(),
            ["assemblyVersion"] = candidate["assemblyVersion"]?.DeepClone(),
            ["probeVersion"] = candidate["probeVersion"]?.DeepClone(),
        },
    };

    private static async Task<JsonObject> CallCaptureAsync(
        ModernMcpProcessDriver driver,
        string tool,
        JsonObject arguments,
        string requestId,
        bool expectedError,
        bool requireNativeSceneSchema = true)
    {
        var response = await driver.CallToolRawAsync(
            tool,
            arguments,
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId(requestId));
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", Text(result["resultType"]));
        var isError = result["isError"]?.GetValue<bool>() ?? false;
        var content = Object(result["structuredContent"]);
        Assert.True(
            expectedError == isError,
            $"{tool} returned an unexpected result: {content.ToJsonString()}.");
        if (requireNativeSceneSchema)
        {
            AssertSchemaValid(tool, content);
        }

        return content;
    }

    private static void AssertElementManifest(
        JsonObject manifest,
        BoundFixtureSession session,
        JsonObject request,
        JsonObject element,
        string expectedStatus)
    {
        Assert.Equal("element_snapshot_capture", Text(manifest["kind"]));
        Assert.Equal(expectedStatus, Text(manifest["status"]));
        Assert.True(JsonNode.DeepEquals(request["sceneRequest"], manifest["sceneRequest"]));
        Assert.Null(manifest["evidenceScope"]);
        Assert.True(JsonNode.DeepEquals(element, manifest["element"]));
        AssertCandidateBinding(session.Candidate, Object(manifest["candidate"]));
        AssertFreshRevalidation(manifest);
        Assert.Equal(new[] { "authority" }, Object(manifest["atomicity"]).Select(property => property.Key));
        Assert.Equal("not_applicable", Text(Object(manifest["atomicity"])["authority"]));
    }

    private static void AssertNativeSceneManifest(
        JsonObject manifest,
        BoundFixtureSession session,
        JsonObject request,
        string? expectedStatus)
    {
        Assert.Equal("native_scene_capture", Text(manifest["kind"]));
        if (expectedStatus is not null)
        {
            Assert.Equal(expectedStatus, Text(manifest["status"]));
        }

        Assert.True(JsonNode.DeepEquals(request["sceneRequest"], manifest["sceneRequest"]));
        Assert.Null(manifest["evidenceScope"]);
        Assert.Null(manifest["element"]);
        AssertCandidateBinding(session.Candidate, Object(manifest["candidate"]));
        AssertFreshRevalidation(manifest);
    }

    private static void AssertFreshRevalidation(JsonObject manifest) =>
        Assert.True(Boolean(Object(manifest["stability"])["revalidatedByCapture"]));

    private static void AssertInProcessAtomicity(JsonObject node, int expectedRevisionBefore, int expectedRevisionAfter)
    {
        var atomicity = Object(node["atomicity"]);
        Assert.Equal("in_process_framework_probe", Text(atomicity["authority"]));
        Assert.Equal("dispatcher_affine_non_yielding", Text(atomicity["transaction"]));
        Assert.True(Boolean(atomicity["immutableDto"]));
        Assert.Equal(expectedRevisionBefore, Integer(atomicity["layoutStateRevisionBefore"]));
        Assert.Equal(expectedRevisionAfter, Integer(atomicity["layoutStateRevisionAfter"]));
    }

    private static JsonObject AssertObservedFactsDescriptor(JsonObject manifest) =>
        Assert.Single(ObservedFactsDescriptors(manifest));

    private static IEnumerable<JsonObject> ObservedFactsDescriptors(JsonObject manifest) =>
        Array(manifest["artifacts"])
            .Select(Object)
            .Where(descriptor =>
                Text(descriptor["mediaType"]) == NativeSceneMediaType &&
                Text(descriptor["evidenceGrade"]) == ObservedFactsEvidenceGrade);

    private static async Task<JsonObject> ReadArtifactJsonAsync(
        ModernMcpProcessDriver driver,
        string debugSessionId,
        JsonObject descriptor)
    {
        var artifactId = Text(descriptor["artifactId"]);
        var byteLength = Integer(descriptor["byteLength"]);
        Assert.True(byteLength >= 3, "A native-scene artifact must support beginning, middle, and ending reconstruction.");
        var chunkSize = Math.Min(ArtifactReadMaxBytes, Math.Max(1, byteLength / 3));
        var bytes = new List<byte>(byteLength);
        var offset = 0;
        var reads = 0;

        while (offset < byteLength)
        {
            var chunk = await CallCaptureAsync(
                driver,
                "read_capture_artifact",
                new JsonObject
                {
                    ["debugSessionId"] = debugSessionId,
                    ["protocolVersion"] = ActiveProtocolVersion,
                    ["schemaVersion"] = ActiveSchemaVersion,
                    ["artifactId"] = artifactId,
                    ["offset"] = offset,
                    ["maxBytes"] = chunkSize,
                },
                $"atomic-artifact-read-{offset}",
                expectedError: false);
            AssertSchemaValid("read_capture_artifact", chunk);
            Assert.Equal("capture_artifact_chunk", Text(chunk["kind"]));
            Assert.Equal(artifactId, Text(chunk["artifactId"]));
            Assert.Equal(offset, Integer(chunk["offset"]));
            Assert.Equal(NativeSceneMediaType, Text(chunk["mediaType"]));
            Assert.Equal(byteLength, Integer(chunk["byteLength"]));
            Assert.Equal(Text(descriptor["sha256"]), Text(chunk["sha256"]));

            var chunkBytes = Convert.FromBase64String(Text(chunk["dataBase64"]));
            Assert.Equal(Convert.ToBase64String(chunkBytes), Text(chunk["dataBase64"]));
            Assert.Equal(chunkBytes.Length, Integer(chunk["bytesRead"]));
            Assert.InRange(chunkBytes.Length, 1, chunkSize);
            Assert.Equal(offset + chunkBytes.Length == byteLength, Boolean(chunk["endOfArtifact"]));
            bytes.AddRange(chunkBytes);
            offset += chunkBytes.Length;
            reads++;
        }

        Assert.True(reads >= 3, "The controlled read must reconstruct beginning, middle, and ending artifact chunks.");
        Assert.Equal(byteLength, bytes.Count);
        Assert.Equal(Text(descriptor["sha256"]), Convert.ToHexString(SHA256.HashData(bytes.ToArray())).ToLowerInvariant());
        return JsonNode.Parse(Encoding.UTF8.GetString(bytes.ToArray()))!.AsObject();
    }

    private static void AssertSceneArtifact(
        JsonObject artifact,
        JsonObject manifest,
        string expectedObservationKind,
        string expectedStatus,
        int maximumNodes)
    {
        AssertArtifactSchemaValid(artifact);
        Assert.Equal("native_scene_artifact", Text(artifact["kind"]));
        Assert.Equal("native-scene-artifact/1", Text(artifact["schemaVersion"]));
        Assert.Equal(expectedObservationKind, Text(artifact["observationKind"]));
        Assert.Equal(expectedStatus, Text(artifact["status"]));
        Assert.Equal(Text(manifest["captureId"]), Text(artifact["captureId"]));
        Assert.True(JsonNode.DeepEquals(manifest["sceneRequest"], artifact["sceneRequest"]));
        Assert.True(JsonNode.DeepEquals(manifest["candidate"], artifact["candidate"]));
        Assert.True(Boolean(Object(artifact["stability"])["revalidatedByCapture"]));

        var graph = Object(artifact["graph"]);
        var nodes = Array(graph["nodes"]);
        var roots = Array(graph["rootNodeIds"]);
        Assert.InRange(nodes.Count, 1, maximumNodes);
        Assert.InRange(roots.Count, 1, maximumNodes);
        Assert.All(nodes.Select(Object), node =>
        {
            Assert.NotNull(node["accessibility"]);
            Assert.NotNull(node["geometry"]);
            Assert.All(Array(node["adapterEvidence"]).Select(Object), evidence =>
            {
                Assert.Contains(Text(evidence["authority"]), new[] { "in_process_framework_probe", "uia_guarded", "adapter_reported" });
                Assert.NotNull(evidence["payload"]);
            });
        });
    }

    private static void AssertCandidateBinding(JsonObject expected, JsonObject actual)
    {
        foreach (var name in new[]
                 {
                     "processId", "processIdentity", "hwnd", "executableSha256", "assemblyVersion", "probeVersion",
                     "observerVersions", "contractSetHash", "storyHash", "source",
                 })
        {
            Assert.True(JsonNode.DeepEquals(expected[name], actual[name]), $"Capture candidate '{name}' must bind to the declared local candidate.");
        }
    }

    private static void AssertSchemaValid(string tool, JsonObject content)
    {
        var validation = NativeSceneContractCatalogDriver.Load().ValidateResult(tool, content.ToJsonString());
        Assert.True(validation.IsValid, $"Expected schema-valid {tool} content, got {validation.Code ?? "<null>"}: {validation.Message ?? "<null>"}.");
    }

    private static void AssertCatalogRejects(NativeSceneContractCatalogDriver catalog, string tool, JsonObject content)
    {
        var validation = catalog.ValidateResult(tool, content.ToJsonString());
        Assert.False(validation.IsValid);
        Assert.Equal("INVALID_TOOL_ARGUMENTS", validation.Code);
    }

    private static void AssertArtifactSchemaValid(JsonObject artifact)
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var schema = JsonSchema.FromJsonAsync(Encoding.UTF8.GetString(catalog.GetArtifactBytes("native-scene-artifact.schema.json")))
            .GetAwaiter()
            .GetResult();
        Assert.Empty(schema.Validate(artifact.ToJsonString()));
    }

    private static JsonObject CorpusResponse(NativeSceneContractCatalogDriver catalog, string caseId) =>
        JsonNode.Parse(Encoding.UTF8.GetString(catalog.GetArtifactBytes("parity-corpus.json")))!.AsObject()["cases"]!.AsArray()
            .Single(@case => Text(@case!.AsObject()["id"]) == caseId)!["response"]!.DeepClone().AsObject();

    private static JsonObject CorpusVariantResponse(NativeSceneContractCatalogDriver catalog, string caseId, string variantName) =>
        JsonNode.Parse(Encoding.UTF8.GetString(catalog.GetArtifactBytes("parity-corpus.json")))!.AsObject()["cases"]!.AsArray()
            .Single(@case => Text(@case!.AsObject()["id"]) == caseId)!["schemaInvalidVariants"]!.AsArray()
            .Single(variant => Text(variant!.AsObject()["name"]) == variantName)!["response"]!.DeepClone().AsObject();

    private static void AssertObservationOnly(JsonNode node) =>
        Assert.DoesNotContain(
            EnumeratePropertyNames(node),
            name => name.Contains("dtcg", StringComparison.OrdinalIgnoreCase) ||
                    name is "designComparison" or "tokenResolution" or "verdict" or "diagnosis" or "repairAdvice");

    private static IEnumerable<string> EnumeratePropertyNames(JsonNode? node)
    {
        if (node is JsonObject objectNode)
        {
            foreach (var property in objectNode)
            {
                yield return property.Key;
                foreach (var nested in EnumeratePropertyNames(property.Value))
                {
                    yield return nested;
                }
            }
        }
        else if (node is JsonArray arrayNode)
        {
            foreach (var item in arrayNode)
            {
                foreach (var nested in EnumeratePropertyNames(item))
                {
                    yield return nested;
                }
            }
        }
    }

    private static JsonObject Object(JsonNode? node) => Assert.IsType<JsonObject>(node);

    private static JsonArray Array(JsonNode? node) => Assert.IsType<JsonArray>(node);

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();

    private static int Integer(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<int>();

    private static bool Boolean(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<bool>();

    private sealed record BoundFixtureSession(string DebugSessionId, JsonObject Candidate);
}

/// <summary>
/// T028 alone defines the host seam. T026's Windows-only probe remains inside
/// the debuggee; its synchronous dispatcher transaction is observed only through
/// this coordinator's injected JSON producer.
/// </summary>
internal static class NativeSceneAtomicityReflection
{
    private const string CaptureCoordinatorTypeName = "NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneCaptureCoordinator";
    private const string TargetIdentityTypeName = "NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneTargetIdentity";

    public static void RequireT028Contract()
    {
        var hostAssembly = LoadHostAssembly();
        var coordinatorType = hostAssembly.GetType(CaptureCoordinatorTypeName, throwOnError: false)
            ?? throw new InvalidOperationException($"Missing T028 capture coordinator '{CaptureCoordinatorTypeName}' in '{hostAssembly.Location}'.");
        Assert.False(coordinatorType.IsPublic, "NativeSceneCaptureCoordinator must remain an internal host authority.");
        RequireTaskOfJsonObjectMethod(coordinatorType, "CaptureElementAsync", typeof(JsonElement), typeof(CancellationToken));
        RequireTaskOfJsonObjectMethod(coordinatorType, "CaptureNativeSceneAsync", typeof(JsonElement), typeof(CancellationToken));
    }

    public static async Task<JsonObject> CaptureGuardedNativeSceneAsync(
        NativeSceneArtifactStoreDriver artifactStore,
        JsonObject guarded)
    {
        var hostAssembly = LoadHostAssembly();
        var coordinatorType = hostAssembly.GetType(CaptureCoordinatorTypeName, throwOnError: false)
            ?? throw new InvalidOperationException($"Missing T028 capture coordinator '{CaptureCoordinatorTypeName}' in '{hostAssembly.Location}'.");
        var store = GetStore(artifactStore);
        var targetType = hostAssembly.GetType(TargetIdentityTypeName, throwOnError: false)
            ?? throw new InvalidOperationException($"Missing native-scene target identity '{TargetIdentityTypeName}' in '{hostAssembly.Location}'.");
        var targetConstructor = targetType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(int), typeof(string), typeof(string), typeof(string), typeof(string), typeof(string)],
            modifiers: null)
            ?? throw new InvalidOperationException($"Missing native-scene target identity constructor on '{targetType.FullName}'.");
        var target = targetConstructor.Invoke([73, "guarded-target", "fixture.exe", new string('a', 64), "1.0", "probe/1"]);
        var targetSupplier = (Delegate)typeof(NativeSceneAtomicityReflection)
            .GetMethod(nameof(CreateTargetSupplier), BindingFlags.Static | BindingFlags.NonPublic)!
            .MakeGenericMethod(targetType)
            .Invoke(null, [target])!;
        Func<CancellationToken, Task<JsonObject?>> noProbe = static _ => Task.FromResult<JsonObject?>(null);
        Func<string, JsonObject, CancellationToken, Task<JsonObject?>> guardedProducer =
            (_, _, _) => Task.FromResult<JsonObject?>(guarded.DeepClone().AsObject());
        Action<JsonObject?> setCaptureStabilityObservation = static _ => { };
        Func<JsonElement, CancellationToken, Task<JsonObject>> revalidate =
            static (_, _) => Task.FromResult(new JsonObject { ["status"] = "STABLE" });
        var constructor = coordinatorType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types:
            [
                store.GetType(),
                typeof(string),
                typeof(Func<JsonObject>),
                targetSupplier.GetType(),
                typeof(Func<CancellationToken, Task<JsonObject?>>),
                typeof(Func<string, JsonObject, CancellationToken, Task<JsonObject?>>),
                typeof(Action<JsonObject?>),
                typeof(Func<JsonElement, CancellationToken, Task<JsonObject>>),
            ],
            modifiers: null)
            ?? throw new InvalidOperationException($"Missing T028 capture coordinator injection constructor on '{coordinatorType.FullName}'.");
        Assert.False(constructor.IsPublic, "The controlled capture producers must remain an internal host seam.");
        var coordinator = constructor.Invoke(
        [
            store,
            "guarded-cycle-session",
            (Func<JsonObject>)(static () => new JsonObject()),
            targetSupplier,
            noProbe,
            guardedProducer,
            setCaptureStabilityObservation,
            revalidate,
        ]);
        Assert.NotNull(coordinator);

        var capture = RequireTaskOfJsonObjectMethod(coordinatorType, "CaptureNativeSceneAsync", typeof(JsonElement), typeof(CancellationToken));
        using var document = JsonDocument.Parse("""{"sceneRequest":{}}""");
        var task = Assert.IsAssignableFrom<Task>(capture.Invoke(coordinator, [document.RootElement, CancellationToken.None]));
        await task.ConfigureAwait(false);
        return Assert.IsType<JsonObject>(task.GetType().GetProperty("Result", BindingFlags.Instance | BindingFlags.Public)?.GetValue(task));
    }

    private static Func<T> CreateTargetSupplier<T>(object target) => () => (T)target;

    private static object GetStore(NativeSceneArtifactStoreDriver artifactStore) =>
        typeof(NativeSceneArtifactStoreDriver).GetField("_store", BindingFlags.Instance | BindingFlags.NonPublic)?.GetValue(artifactStore)
        ?? throw new InvalidOperationException("NativeSceneArtifactStoreDriver must retain its internal store instance.");

    private static Assembly LoadHostAssembly() => AssemblyLoadContext.Default.LoadFromAssemblyPath(TestOutputPathResolver.ResolveManagedAssembly(
        RepositoryLayout.Root,
        Path.Combine("host", "NetCoreDbg.Mcp.Stateless"),
        "NetCoreDbg.Mcp.Stateless"));

    private static MethodInfo RequireTaskOfJsonObjectMethod(Type type, string name, params Type[] parameterTypes)
    {
        var method = type.GetMethod(
            name,
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: parameterTypes,
            modifiers: null)
            ?? throw new InvalidOperationException($"{type.FullName} must expose {name}({string.Join(", ", parameterTypes.Select(static parameter => parameter.Name))}).");
        Assert.False(method.IsPublic, $"{type.FullName}.{name} must remain an internal host operation.");
        Assert.Equal(typeof(Task<JsonObject>), method.ReturnType);
        return method;
    }
}
