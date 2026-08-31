using System.Buffers.Binary;
using System.Collections.Immutable;
using System.IO.Pipes;
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
[Trait("Coverage", "Exclude")]
public sealed class NativeSceneAtomicityTests
{
    private const string ActiveProtocolVersion = "native-scene-probe/1";
    private const string ActiveSchemaVersion = "native-scene-probe.schema/1";
    private const string NativeSceneMediaType = "application/vnd.netcoredbg.native-scene+json";
    private const string ObservedFactsEvidenceGrade = "observed_facts";
    private const int ArtifactReadMaxBytes = 65_536;
    private const int MaximumProbeResponseBytes = 16 * 1024 * 1024;
    private static readonly ImmutableArray<string> IncompleteCaptureStatuses = ImmutableArray.Create(
        "PARTIAL",
        "UNOBSERVABLE");
    private static readonly ImmutableArray<string> AtomicityPropertyNames = ImmutableArray.Create("authority");
    private static readonly ImmutableArray<string> AdapterEvidenceAuthorities = ImmutableArray.Create(
        "in_process_framework_probe",
        "uia_guarded",
        "adapter_reported");

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
    public async Task IncompleteFixture_ElementCaptureIsPartialWithAdapterFactUnobservable()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var expected = CorpusExpected(catalog, "C016-observation-only-result");
        await using var driver = await StartFixtureDriverAsync("incomplete");
        var session = await StartBoundFixtureSessionAsync(driver, "atomic-element-partial-start");
        var request = BoundCorpusRequest(catalog, "C016-observation-only-result", session);
        var element = Object(request["element"]);

        var manifest = await CallCaptureAsync(
            driver,
            "capture_element_snapshot",
            request,
            "atomic-element-partial-capture",
            expectedError: false);

        AssertSchemaValid("capture_element_snapshot", manifest);
        AssertElementManifest(manifest, session, request, element, Text(expected["status"]));
        Assert.Contains(Array(manifest["issues"]).Select(Object), issue => Text(issue["code"]) == Text(expected["requiredIssue"]));
        var descriptor = AssertObservedFactsDescriptor(manifest);
        var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);

        AssertSceneArtifact(artifact, manifest, "element_snapshot", Text(expected["status"]), maximumNodes: 1);
        Assert.Contains(Array(artifact["issues"]).Select(Object), issue => Text(issue["code"]) == Text(expected["requiredIssue"]));
        AssertObservationOnly(manifest);
        AssertObservationOnly(artifact);
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
        Assert.Contains(Text(manifest["status"]), IncompleteCaptureStatuses);
        foreach (var descriptor in ObservedFactsDescriptors(manifest))
        {
            var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);
            AssertSceneArtifact(artifact, manifest, "native_scene", Text(manifest["status"]), maximumNodes: 4_096);
        }

        AssertObservationOnly(manifest);
    }

    [Fact]
    public async Task C013_LiveNoProbeWindowWithFlaUiGuardedCaptureIsPartialAndRetrievable()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var catalog = NativeSceneContractCatalogDriver.Load();
        var expected = CorpusExpected(catalog, "C013-uia-guarded-scene-is-qualified");
        await using var driver = await StartWindowedDescendantDriverAsync();
        var session = await StartBoundWindowedSessionAsync(driver, "atomic-uia-guarded-start");
        var request = BoundCorpusRequest(catalog, "C013-uia-guarded-scene-is-qualified", session);

        var manifest = await CallCaptureAsync(
            driver,
            "capture_native_scene",
            request,
            "atomic-uia-guarded-capture",
            expectedError: false);

        AssertSchemaValid("capture_native_scene", manifest);
        AssertNativeSceneManifest(manifest, session, request, Text(expected["status"]));
        AssertUiaGuardedAtomicity(manifest);
        Assert.Equal("UNOBSERVABLE", Text(Object(manifest["stability"])["status"]));
        Assert.Contains(Array(manifest["issues"]).Select(Object), issue => Text(issue["code"]) == "CAPTURE_REVALIDATION_FAILED");
        Assert.Contains(Array(manifest["issues"]).Select(Object), issue => Text(issue["code"]) == "ATOMICITY_UNPROVEN_UIA_GUARDED");
        Assert.Contains(Array(manifest["issues"]).Select(Object), issue => Text(issue["code"]) == Text(expected["requiredIssue"]));
        var descriptor = AssertObservedFactsDescriptor(manifest);
        var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);

        AssertSceneArtifact(artifact, manifest, "native_scene", Text(expected["status"]), maximumNodes: 4_096);
        AssertUiaGuardedAtomicity(artifact);
        Assert.Equal("UNOBSERVABLE", Text(Object(artifact["stability"])["status"]));
        Assert.Contains(Array(artifact["issues"]).Select(Object), issue => Text(issue["code"]) == "CAPTURE_REVALIDATION_FAILED");
        Assert.Contains(Array(artifact["issues"]).Select(Object), issue => Text(issue["code"]) == "ATOMICITY_UNPROVEN_UIA_GUARDED");
        AssertObservationOnly(manifest);
        AssertObservationOnly(artifact);
    }

    [Fact]
    public async Task C020_OversizedRawProbeAdapterPayloadIsRejectedBeforeFurtherObservationOrArtifactWork()
    {
        var catalog = NativeSceneContractCatalogDriver.Load();
        var @case = CorpusCase(catalog, "C020-oversized-observer-output-is-contained-before-commit");
        var expected = Object(@case["expected"]);
        var payload = OversizedAdapterPayload(@case);
        Assert.Equal(Integer(Object(@case["adapterPayloadFixture"])["serializedUtf8Bytes"]), Encoding.UTF8.GetByteCount(payload.GetValue<string>()));
        Assert.True(Encoding.UTF8.GetByteCount(payload.ToJsonString()) > 262_144);

        var guardedCalls = 0;
        var revalidationCalls = 0;
        await using var artifacts = ArtifactStoreTestScope.Create();
        await using var probe = NativeSceneAtomicityReflection.CreateRawProbeChannel();
        var responseTask = probe.RespondAsync(RawProbeResponse(payload));
        var result = await NativeSceneAtomicityReflection.CaptureNativeSceneAsync(
            artifacts.Store,
            CorpusRequest(catalog, "C020-oversized-observer-output-is-contained-before-commit"),
            probe.CaptureAsync,
            (_, _, _) =>
            {
                guardedCalls++;
                return Task.FromResult<JsonObject?>(null);
            },
            static _ => { },
            (_, _) =>
            {
                revalidationCalls++;
                return Task.FromResult(new JsonObject { ["status"] = "STABLE" });
            });
        await responseTask;

        Assert.InRange(probe.ResponseFrameByteCount, 1, (1024 * 1024) - 1);
        AssertSchemaValid("capture_native_scene", result);
        Assert.Equal(Text(Object(expected["requiredResponse"])["kind"]), Text(result["kind"]));
        Assert.Equal(Text(Object(expected["requiredResponse"])["tool"]), Text(result["tool"]));
        Assert.Equal(Text(expected["errorCode"]), Text(result["code"]));
        Assert.Equal(0, guardedCalls);
        Assert.Equal(0, revalidationCalls);
        Assert.Equal(0, artifacts.Store.StagedArtifactMetadataCount);
        Assert.Equal(0, artifacts.Store.CommittedArtifactMetadataCount);
        Assert.Empty(Directory.EnumerateFiles(artifacts.Root, "*", SearchOption.AllDirectories));
        Assert.Null(await probe.CaptureAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(1)));
        await probe.AssertChannelClosedWithoutSecondRequestAsync();
    }

    [Fact]
    public async Task PostConnectCallerCancellation_TerminalizesProbeAndRejectsStaleResponseWithoutSendingASecondRequest()
    {
        await using var probe = NativeSceneAtomicityReflection.CreateRawProbeChannel();
        using var cancellation = new CancellationTokenSource();
        var firstCapture = probe.CaptureAsync(cancellation.Token);
        await probe.ConnectAndReadRequestAsync();

        cancellation.Cancel();
        await Assert.ThrowsAnyAsync<OperationCanceledException>(async () => await firstCapture);

        await probe.AttemptStaleResponseAsync(RawProbeResponse(new JsonObject()));
        Assert.Null(await probe.CaptureAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(1)));
        await probe.AssertChannelClosedWithoutSecondRequestAsync();
    }

    [Fact]
    public async Task WpfProbeResponseOverOneMiBTraversesTheRealChannelWithoutTerminalizing()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        Assert.Equal(MaximumProbeResponseBytes, NativeSceneAtomicityReflection.GetWpfProbeDefaultMaximumResponseBytes());
        NativeSceneAtomicityReflection.AssertWpfProbeOptionsRejectOversizedResponse(MaximumProbeResponseBytes);
        NativeSceneAtomicityReflection.AssertWpfProbeWriterRejectsOversizedPayload(MaximumProbeResponseBytes);

        await using var driver = await StartFixtureDriverAsync("large-response");
        var session = await StartBoundFixtureSessionAsync(driver, "atomic-scene-large-response-start");
        var request = NativeSceneCaptureArguments(session);
        var first = await CallCaptureAsync(
            driver,
            "capture_native_scene",
            request,
            "atomic-scene-large-response-first-capture",
            expectedError: false);

        AssertNativeSceneManifest(first, session, request, "COMPLETE");
        AssertInProcessAtomicity(first, expectedRevisionBefore: 77, expectedRevisionAfter: 77);
        Assert.Empty(Array(first["issues"]));
        var descriptor = AssertObservedFactsDescriptor(first);
        Assert.InRange(Integer(descriptor["byteLength"]), (1024 * 1024) + 1, MaximumProbeResponseBytes - 1);
        var artifact = await ReadArtifactJsonAsync(driver, session.DebugSessionId, descriptor);

        AssertSceneArtifact(artifact, first, "native_scene", "COMPLETE", maximumNodes: 256);
        Assert.Equal(256, Array(Object(artifact["graph"])["nodes"]).Count);
        AssertInProcessAtomicity(artifact, expectedRevisionBefore: 77, expectedRevisionAfter: 77);
        var firstNode = Object(Array(Object(artifact["graph"])["nodes"])[0]);
        var firstEvidence = Object(Assert.Single(Array(firstNode["adapterEvidence"])));
        var firstPayload = Object(firstEvidence["payload"]);
        Assert.Equal(new string('x', 255), Text(firstPayload["text"]));
        Assert.Equal(LargeResponseText(), string.Concat(Array(firstPayload["textChunks"]).Select(Text)));

        var second = await CallCaptureAsync(
            driver,
            "capture_native_scene",
            request,
            "atomic-scene-large-response-second-capture",
            expectedError: false);

        AssertNativeSceneManifest(second, session, request, "COMPLETE");
        Assert.NotEqual(Text(first["captureId"]), Text(second["captureId"]));
        AssertInProcessAtomicity(second, expectedRevisionBefore: 77, expectedRevisionAfter: 77);
        Assert.Empty(Array(second["issues"]));
        Assert.InRange(Integer(AssertObservedFactsDescriptor(second)["byteLength"]), (1024 * 1024) + 1, MaximumProbeResponseBytes - 1);
    }

    private static string LargeResponseText() => new string('x', 255) + "😀" + new string('x', 4096 - 257);

    [Fact]
    public async Task ProbeResponseAbove16MiBIsRejectedBeforePayloadAllocation()
    {
        await using var probe = NativeSceneAtomicityReflection.CreateRawProbeChannel();
        var capture = probe.CaptureAsync(CancellationToken.None);
        await probe.ConnectAndReadRequestAsync();
        await probe.WriteResponseLengthAsync(MaximumProbeResponseBytes + 1, CancellationToken.None);

        Assert.Null(await capture.WaitAsync(TimeSpan.FromSeconds(5)));
        Assert.Null(await probe.CaptureAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(1)));
        await probe.AssertChannelClosedWithoutSecondRequestAsync();
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
            ["stability"] = CreateObserverStability(sceneEpoch: 0L),
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

    private static Task<ModernMcpProcessDriver> StartWindowedDescendantDriverAsync() =>
        ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(SpawnWindowedDescendant: true),
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["FLAUI_BRIDGE_PATH"] = ResolveBridgeAssemblyPath(),
                }));

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

    private static async Task<BoundFixtureSession> StartBoundWindowedSessionAsync(ModernMcpProcessDriver driver, string requestId)
    {
        var started = await CallCaptureAsync(
            driver,
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
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
        var processId = await driver.ReadDescendantProcessIdAsync();
        Assert.Equal(processId, Integer(candidate["processId"]));
        await WaitForMainWindowAsync(processId);
        return new BoundFixtureSession(debugSessionId, candidate);
    }

    private static async Task WaitForMainWindowAsync(int processId)
    {
        using var process = System.Diagnostics.Process.GetProcessById(processId);
        var timeout = System.Diagnostics.Stopwatch.StartNew();
        while (timeout.Elapsed < TimeSpan.FromSeconds(2))
        {
            process.Refresh();
            if (!process.HasExited && process.MainWindowHandle != IntPtr.Zero)
            {
                return;
            }

            await Task.Delay(TimeSpan.FromMilliseconds(25));
        }

        process.Refresh();
        Assert.Fail($"Controlled C013 window did not become ready; pid={processId}, exited={process.HasExited}, hwnd={process.MainWindowHandle.ToInt64()}.");
    }

    private static string ResolveBridgeAssemblyPath()
    {
        var configuration = new DirectoryInfo(AppContext.BaseDirectory).Parent?.Name
            ?? throw new InvalidOperationException("Test output configuration is absent.");
        var bridgeDirectory = Path.Combine(RepositoryLayout.Root, "bridge", "bin", configuration, "net8.0-windows");
        return new[]
            {
                Path.Combine(bridgeDirectory, "win-x64", "FlaUIBridge.dll"),
                Path.Combine(bridgeDirectory, "FlaUIBridge.dll"),
            }
            .FirstOrDefault(File.Exists)
            ?? throw new InvalidOperationException("Built FlaUI bridge assembly is absent.");
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
        Assert.Equal(AtomicityPropertyNames, Object(manifest["atomicity"]).Select(property => property.Key));
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

    private static void AssertUiaGuardedAtomicity(JsonObject node)
    {
        var atomicity = Object(node["atomicity"]);
        Assert.Equal("uia_guarded", Text(atomicity["authority"]));
        var guards = Object(atomicity["guards"]);
        foreach (var name in new[] { "window", "client", "dpi", "visualTreeFingerprint" })
        {
            Assert.Equal("unchanged", Text(Object(guards[name])["state"]));
        }

        Assert.Equal(4, guards.Count);
        Assert.DoesNotContain(
            atomicity.Select(property => property.Key),
            name => name is "transaction" or "immutableDto" or "layoutStateRevisionBefore" or "layoutStateRevisionAfter" or "sceneEpoch" or "sequence");
        var stability = Object(node["stability"]);
        Assert.IsAssignableFrom<JsonValue>(stability["sceneEpoch"]);
        Assert.IsAssignableFrom<JsonValue>(stability["sequence"]);
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
                Assert.Contains(Text(evidence["authority"]), AdapterEvidenceAuthorities);
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

    private static JsonObject CorpusCase(NativeSceneContractCatalogDriver catalog, string caseId) =>
        JsonNode.Parse(Encoding.UTF8.GetString(catalog.GetArtifactBytes("parity-corpus.json")))!.AsObject()["cases"]!.AsArray()
            .Single(@case => Text(@case!.AsObject()["id"]) == caseId)!.AsObject();

    private static JsonObject CorpusRequest(NativeSceneContractCatalogDriver catalog, string caseId) =>
        CorpusCase(catalog, caseId)["request"]!.DeepClone().AsObject();

    private static JsonObject CorpusExpected(NativeSceneContractCatalogDriver catalog, string caseId) =>
        CorpusCase(catalog, caseId)["expected"]!.DeepClone().AsObject();

    private static JsonObject BoundCorpusRequest(NativeSceneContractCatalogDriver catalog, string caseId, BoundFixtureSession session)
    {
        var request = CorpusRequest(catalog, caseId);
        request["debugSessionId"] = session.DebugSessionId;
        var expectedCandidate = Object(Object(request["sceneRequest"])["expectedCandidateIdentity"]);
        foreach (var name in new[] { "executableSha256", "assemblyVersion", "probeVersion" })
        {
            expectedCandidate[name] = session.Candidate[name]?.DeepClone();
        }

        return request;
    }

    private static JsonObject CorpusResponse(NativeSceneContractCatalogDriver catalog, string caseId) =>
        CorpusCase(catalog, caseId)["response"]!.DeepClone().AsObject();

    private static JsonObject CorpusVariantResponse(NativeSceneContractCatalogDriver catalog, string caseId, string variantName) =>
        CorpusCase(catalog, caseId)["schemaInvalidVariants"]!.AsArray()
            .Single(variant => Text(variant!.AsObject()["name"]) == variantName)!["response"]!.DeepClone().AsObject();

    private static JsonValue OversizedAdapterPayload(JsonObject @case)
    {
        var fixture = Object(@case["adapterPayloadFixture"]);
        var scalar = Text(fixture["scalar"]);
        Assert.Single(scalar);
        return JsonValue.Create(new string(scalar[0], Integer(fixture["repeatCount"])))!;
    }

    private static JsonObject RawProbeResponse(JsonNode payload) => new()
    {
        ["authority"] = "in_process_probe",
        ["candidate"] = new JsonObject
        {
            ["processId"] = 73,
            ["processIdentity"] = "guarded-target",
        },
        ["process"] = new JsonObject { ["processId"] = 73 },
        ["revisionBefore"] = 77,
        ["revisionAfter"] = 77,
        ["complete"] = true,
        ["rootId"] = "Button.Primary",
        ["nodes"] = new JsonArray
        {
            new JsonObject
            {
                ["id"] = "Button.Primary",
                ["x"] = 0,
                ["y"] = 0,
                ["width"] = 1,
                ["height"] = 1,
                ["automationId"] = "SaveButton",
                ["accessibleName"] = "Save",
                ["text"] = "Save",
                ["adapterEvidence"] = new JsonArray
                {
                    new JsonObject
                    {
                        ["namespace"] = "example.waveform",
                        ["schemaVersion"] = "1",
                        ["authority"] = "adapter_reported",
                        ["payload"] = payload.DeepClone(),
                    },
                },
            },
        },
        ["stability"] = CreateObserverStability(sceneEpoch: 77L),
    };

    private static JsonObject CreateObserverStability(long sceneEpoch) => new()
    {
        ["sceneEpoch"] = sceneEpoch,
        ["conditions"] = new JsonObject
        {
            ["dispatcherIdle"] = new JsonObject { ["state"] = "met" },
            ["stableLayout"] = new JsonObject { ["state"] = "met" },
            ["animationState"] = new JsonObject { ["state"] = "met" },
            ["windowGeometry"] = new JsonObject { ["state"] = "met" },
            ["contextMaterialization"] = new JsonObject { ["state"] = "met" },
            ["asyncLoadSettled"] = new JsonObject { ["state"] = "met" },
        },
    };

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
    private const string ProbeChannelTypeName = "NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneProbeChannel";


    public static void RequireT028Contract()
    {
        var hostAssembly = LoadHostAssembly();
        var coordinatorType = hostAssembly.GetType(CaptureCoordinatorTypeName, throwOnError: false)
            ?? throw new InvalidOperationException($"Missing T028 capture coordinator '{CaptureCoordinatorTypeName}' in '{hostAssembly.Location}'.");
        Assert.False(coordinatorType.IsPublic, "NativeSceneCaptureCoordinator must remain an internal host authority.");
        RequireTaskOfJsonObjectMethod(coordinatorType, "CaptureElementAsync", typeof(JsonElement), typeof(CancellationToken));
        RequireTaskOfJsonObjectMethod(coordinatorType, "CaptureNativeSceneAsync", typeof(JsonElement), typeof(CancellationToken));
    }

    public static RawProbeChannelDriver CreateRawProbeChannel() =>
        RawProbeChannelDriver.Create(LoadHostAssembly(), ProbeChannelTypeName);

    public static int GetWpfProbeDefaultMaximumResponseBytes()
    {
        var optionsType = RequireWpfProbeType("NetCoreDbg.Mcp.DesignProbe.Wpf.LocalProbeClientOptions");
        var options = Activator.CreateInstance(optionsType)
            ?? throw new InvalidOperationException("Could not create local WPF probe client options.");
        return Assert.IsType<int>(RequireProperty(optionsType, "MaximumResponseBytes").GetValue(options));
    }

    public static void AssertWpfProbeOptionsRejectOversizedResponse(int maximumResponseBytes)
    {
        var optionsType = RequireWpfProbeType("NetCoreDbg.Mcp.DesignProbe.Wpf.LocalProbeClientOptions");
        var options = Activator.CreateInstance(optionsType)
            ?? throw new InvalidOperationException("Could not create local WPF probe client options.");
        RequireProperty(optionsType, "MaximumResponseBytes").SetValue(options, maximumResponseBytes + 1);
        var clientType = RequireWpfProbeType("NetCoreDbg.Mcp.DesignProbe.Wpf.LocalProbeClient");
        var validateOptions = clientType.GetMethod("ValidateOptions", BindingFlags.Static | BindingFlags.NonPublic)
            ?? throw new InvalidOperationException("The local WPF probe option validation seam is unavailable.");

        var exception = Assert.Throws<TargetInvocationException>(() => validateOptions.Invoke(null, [options]));
        Assert.IsType<ArgumentOutOfRangeException>(exception.InnerException);
    }

    public static void AssertWpfProbeWriterRejectsOversizedPayload(int maximumResponseBytes)
    {
        var clientType = RequireWpfProbeType("NetCoreDbg.Mcp.DesignProbe.Wpf.LocalProbeClient");
        var writerType = clientType.GetNestedType("BoundedBufferWriter", BindingFlags.NonPublic)
            ?? throw new InvalidOperationException("The local WPF probe response writer is unavailable.");
        var writer = Activator.CreateInstance(
            writerType,
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            args: [maximumResponseBytes],
            culture: null)
            ?? throw new InvalidOperationException("Could not create the local WPF probe response writer.");
        var getMemory = writerType.GetMethod("GetMemory", BindingFlags.Public | BindingFlags.Instance)
            ?? throw new InvalidOperationException("The local WPF probe response writer allocation seam is unavailable.");

        var exception = Assert.Throws<TargetInvocationException>(() => getMemory.Invoke(writer, [maximumResponseBytes + 1]));
        Assert.IsType<InvalidDataException>(exception.InnerException);
    }

    public static async Task<JsonObject> CaptureGuardedNativeSceneAsync(
        NativeSceneArtifactStoreDriver artifactStore,
        JsonObject guarded) =>
        await CaptureNativeSceneAsync(
            artifactStore,
            new JsonObject { ["sceneRequest"] = new JsonObject() },
            static _ => Task.FromResult<JsonObject?>(null),
            (_, _, _) => Task.FromResult<JsonObject?>(guarded.DeepClone().AsObject()),
            static _ => { },
            static (_, _) => Task.FromResult(new JsonObject { ["status"] = "STABLE" }));

    public static async Task<JsonObject> CaptureNativeSceneAsync(
        NativeSceneArtifactStoreDriver artifactStore,
        JsonObject request,
        Func<CancellationToken, Task<JsonObject?>> probeProducer,
        Func<string, JsonObject, CancellationToken, Task<JsonObject?>> guardedProducer,
        Action<JsonObject?> setCaptureStabilityObservation,
        Func<JsonElement, CancellationToken, Task<JsonObject>> revalidate)
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
            probeProducer,
            guardedProducer,
            setCaptureStabilityObservation,
            revalidate,
        ]);
        Assert.NotNull(coordinator);

        var capture = RequireTaskOfJsonObjectMethod(coordinatorType, "CaptureNativeSceneAsync", typeof(JsonElement), typeof(CancellationToken));
        using var document = JsonDocument.Parse(request.ToJsonString());
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

    private static Assembly LoadWpfProbeAssembly()
    {
        var configuration = new DirectoryInfo(AppContext.BaseDirectory).Parent?.Name
            ?? throw new InvalidOperationException("The test configuration is unavailable.");
        var assemblyPath = Path.Combine(
            RepositoryLayout.Root,
            "host",
            "NetCoreDbg.Mcp.DesignProbe.Wpf",
            "bin",
            configuration,
            "net8.0-windows",
            "NetCoreDbg.Mcp.DesignProbe.Wpf.dll");
        Assert.True(File.Exists(assemblyPath), $"Built WPF probe assembly is absent: '{assemblyPath}'.");
        return AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyPath);
    }

    private static Type RequireWpfProbeType(string name) =>
        LoadWpfProbeAssembly().GetType(name, throwOnError: false)
        ?? throw new InvalidOperationException($"The WPF probe type '{name}' is unavailable.");

    private static PropertyInfo RequireProperty(Type type, string name) =>
        type.GetProperty(name, BindingFlags.Public | BindingFlags.Instance)
        ?? throw new InvalidOperationException($"The WPF probe property '{type.FullName}.{name}' is unavailable.");

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

internal sealed class RawProbeChannelDriver : IAsyncDisposable
{
    private readonly object _channel;
    private readonly MethodInfo _captureAsync;
    private NamedPipeClientStream? _pipe;
    private string? _correlationId;

    private RawProbeChannelDriver(object channel, MethodInfo captureAsync, string pipeName, string nonce)
    {
        _channel = channel;
        _captureAsync = captureAsync;
        PipeName = pipeName;
        Nonce = nonce;
    }

    public string PipeName { get; }

    public string Nonce { get; }

    public int ResponseFrameByteCount { get; private set; }

    public static RawProbeChannelDriver Create(Assembly hostAssembly, string channelTypeName)
    {
        var channelType = hostAssembly.GetType(channelTypeName, throwOnError: false)
            ?? throw new InvalidOperationException($"Missing native-scene probe channel '{channelTypeName}' in '{hostAssembly.Location}'.");
        Assert.False(channelType.IsPublic, "NativeSceneProbeChannel must remain an internal host authority.");
        var channel = Activator.CreateInstance(channelType, nonPublic: true)
            ?? throw new InvalidOperationException($"Could not create native-scene probe channel '{channelType.FullName}'.");
        var captureAsync = channelType.GetMethod("CaptureAsync", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
            ?? throw new InvalidOperationException($"Missing CaptureAsync on '{channelType.FullName}'.");
        var pipeName = Assert.IsType<string>(channelType.GetProperty("PipeName", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(channel));
        var nonce = Assert.IsType<string>(channelType.GetProperty("Nonce", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)?.GetValue(channel));
        return new RawProbeChannelDriver(channel, captureAsync, pipeName, nonce);
    }

    public async Task<JsonObject?> CaptureAsync(CancellationToken cancellationToken)
    {
        var task = Assert.IsAssignableFrom<Task>(_captureAsync.Invoke(_channel, [cancellationToken]));
        await task.ConfigureAwait(false);
        return task.GetType().GetProperty("Result", BindingFlags.Instance | BindingFlags.Public)?.GetValue(task) as JsonObject;
    }

    public async Task ConnectAndReadRequestAsync()
    {
        Assert.Null(_pipe);
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        var pipe = new NamedPipeClientStream(".", PipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
        try
        {
            await pipe.ConnectAsync(deadline.Token);
            using var request = JsonDocument.Parse(await ReadFrameAsync(pipe, deadline.Token));
            Assert.Equal(Nonce, request.RootElement.GetProperty("nonce").GetString());
            var correlationId = request.RootElement.GetProperty("correlationId").GetString();
            Assert.False(string.IsNullOrWhiteSpace(correlationId));
            _pipe = pipe;
            _correlationId = correlationId;
        }
        catch
        {
            await pipe.DisposeAsync();
            throw;
        }
    }

    public async Task RespondAsync(JsonObject response)
    {
        await ConnectAndReadRequestAsync();
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(5));
        await WriteResponseAsync(response, deadline.Token);
    }

    public async Task WriteResponseLengthAsync(int length, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, length);
        await RequirePipe().WriteAsync(header, cancellationToken);
        await RequirePipe().FlushAsync(cancellationToken);
    }

    public async Task AttemptStaleResponseAsync(JsonObject response)
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(1));
        try
        {
            await WriteResponseAsync(response, deadline.Token);
        }
        catch (IOException)
        {
        }
        catch (ObjectDisposedException)
        {
        }
    }

    public async Task AssertChannelClosedWithoutSecondRequestAsync()
    {
        using var deadline = new CancellationTokenSource(TimeSpan.FromSeconds(1));
        try
        {
            var read = await RequirePipe().ReadAsync(new byte[1], deadline.Token);
            Assert.Equal(0, read);
        }
        catch (IOException)
        {
        }
    }

    public async ValueTask DisposeAsync()
    {
        await DisposePipeAsync();
        await ((IAsyncDisposable)_channel).DisposeAsync();
    }

    private async Task WriteResponseAsync(JsonObject response, CancellationToken cancellationToken)
    {
        var envelope = new JsonObject
        {
            ["nonce"] = Nonce,
            ["correlationId"] = RequireCorrelationId(),
            ["response"] = response.DeepClone(),
        };
        var bytes = Encoding.UTF8.GetBytes(envelope.ToJsonString());
        ResponseFrameByteCount = checked(bytes.Length + sizeof(int));
        await WriteFrameAsync(RequirePipe(), bytes, cancellationToken);
    }

    private NamedPipeClientStream RequirePipe() =>
        _pipe ?? throw new InvalidOperationException("The raw probe client is not connected.");

    private string RequireCorrelationId() =>
        _correlationId ?? throw new InvalidOperationException("The raw probe request has not been read.");

    private async ValueTask DisposePipeAsync()
    {
        var pipe = _pipe;
        _pipe = null;
        _correlationId = null;
        if (pipe is not null)
        {
            await pipe.DisposeAsync();
        }
    }

    private static async Task<byte[]> ReadFrameAsync(Stream stream, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        await ReadExactlyAsync(stream, header, cancellationToken);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        Assert.InRange(length, 1, 64 * 1024);
        var frame = new byte[length];
        await ReadExactlyAsync(stream, frame, cancellationToken);
        return frame;
    }

    private static async Task WriteFrameAsync(Stream stream, byte[] payload, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await stream.WriteAsync(header, cancellationToken);
        await stream.WriteAsync(payload, cancellationToken);
        await stream.FlushAsync(cancellationToken);
    }

    private static async Task ReadExactlyAsync(Stream stream, Memory<byte> destination, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < destination.Length)
        {
            var read = await stream.ReadAsync(destination[offset..], cancellationToken);
            if (read == 0)
            {
                throw new EndOfStreamException("The native-scene probe pipe closed before a complete frame arrived.");
            }

            offset += read;
        }
    }
}


