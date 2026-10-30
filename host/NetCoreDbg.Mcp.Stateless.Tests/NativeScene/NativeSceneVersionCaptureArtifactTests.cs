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
using Draft7 = Json.Schema;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeSceneVersionCaptureArtifactTests
{
    private const string Protocol = "native-scene-probe/1";
    private const string LegacySchema = "native-scene-probe.schema/1";
    private const string CurrentSchema = "native-scene-probe.schema/2";
    private const string ArtifactSchema = "native-scene-artifact/1";
    private const string SceneMediaType = "application/vnd.netcoredbg.native-scene+json";
    private static readonly string[] CaptureTools = ["capture_element_snapshot", "capture_native_scene", "capture_visual_evidence"];

    [Theory]
    [InlineData("capture_element_snapshot", false)]
    [InlineData("capture_element_snapshot", true)]
    [InlineData("capture_native_scene", false)]
    [InlineData("capture_native_scene", true)]
    [InlineData("capture_visual_evidence", false)]
    [InlineData("capture_visual_evidence", true)]
    public async Task MixedRevisionCaptures_KeepRequestLocalVersionsAndReadOriginalArtifactsAcrossRevisions(string tool, bool concurrent)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var contracts = await LoadContractsAsync();
        var scratch = ModernMcpScratchDirectory.Create();
        try
        {
            await using var driver = await StartFixtureDriverAsync(Path.Combine(scratch, "artifacts"), tool == "capture_visual_evidence");
            var session = await StartSessionAsync(driver, FixtureExecutablePath(), "capture-start");
            await WaitForCandidateAsync(driver, session);
            await WaitForWindowAsync(await driver.ReadDescendantProcessIdAsync());
            var candidates = new Dictionary<string, JsonObject>(StringComparer.Ordinal);
            foreach (var contract in contracts.Values)
            {
                var declaration = await CallAsync(driver, "get_ui_probe_capabilities", Arguments(session, contract.Version), $"capabilities-{contract.Version}", false);
                AssertValid(contract, "get_ui_probe_capabilities", declaration);
                var candidate = Object(declaration["candidate"]);
                Assert.Equal(await driver.ReadDescendantProcessIdAsync(), candidate["processId"]!.GetValue<int>());
                Assert.Equal(contract.Sha256, Text(candidate["contractSetHash"]));
                candidates.Add(contract.Version, candidate);
            }
            AssertSameIdentity(candidates[LegacySchema], candidates[CurrentSchema]);

            string[] revisions = [LegacySchema, CurrentSchema, CurrentSchema, LegacySchema];
            var requests = revisions.Select(revision => CaptureArguments(tool, session, revision, candidates[revision])).ToArray();
            var nativeBefore = await driver.ReadNativeActionsAsync();
            var captured = new List<CapturedArtifact>(revisions.Length);
            if (concurrent)
            {
                var manifests = await Task.WhenAll(requests.Select((request, index) =>
                    CallAsync(driver, tool, request, $"parallel-capture-{index}", false)));
                for (var index = 0; index < manifests.Length; index++)
                {
                    captured.Add(await ObserveCaptureAsync(driver, session, tool, contracts[revisions[index]], requests[index], candidates[revisions[index]], manifests[index], index));
                }
            }
            else
            {
                for (var index = 0; index < requests.Length; index++)
                {
                    var manifest = await CallAsync(driver, tool, requests[index], $"serial-capture-{index}", false);
                    // Read /1 bytes before creating /2 artifacts, then re-read them after the cutover.
                    captured.Add(await ObserveCaptureAsync(driver, session, tool, contracts[revisions[index]], requests[index], candidates[revisions[index]], manifest, index));
                }
            }

            Assert.Equal(captured.Count, captured.Select(item => Text(item.Descriptor["artifactId"])).Distinct(StringComparer.Ordinal).Count());
            Assert.Equal(captured.Count, captured.Select(item => Text(item.Manifest["captureId"])).Distinct(StringComparer.Ordinal).Count());
            foreach (var artifact in captured)
            {
                foreach (var contract in contracts.Values)
                {
                    var bytes = await ReadArtifactAsync(driver, session, contract, artifact.Descriptor, $"reread-{artifact.Index}-{contract.Version}");
                    Assert.Equal(artifact.Bytes, bytes);
                    await AssertArtifactProvenanceAsync(tool, artifact.Manifest, bytes);
                }
            }
            Assert.Equal(nativeBefore.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());

            var stopped = await CallAsync(driver, "stop_debug", new JsonObject { ["debugSessionId"] = session }, "capture-session-stop", false);
            Assert.Equal("stop_debug_success", Text(stopped["kind"]));
            var afterStop = await driver.ReadNativeActionsAsync();
            foreach (var contract in contracts.Values)
            {
                var error = await CallAsync(driver, "read_capture_artifact",
                    ArtifactArguments(session, contract.Version, Text(captured[0].Descriptor["artifactId"]), 0, 1),
                    $"retired-artifact-{contract.Version}", true);
                AssertError(contract, "read_capture_artifact", "DEBUG_SESSION_NOT_FOUND", error);
            }
            Assert.Equal(afterStop.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());
        }
        finally
        {
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    [Theory]
    [InlineData(LegacySchema)]
    [InlineData(CurrentSchema)]
    public async Task CurrentCorpus_PinnedFixturesAgreeWithDraft7AndVersionedRuntimeValidators(string revision)
    {
        var legacy = revision == LegacySchema;
        var corpusFile = legacy ? "parity-corpus.json" : "parity-corpus-v2.json";
        const string currentCorpusHash = "bfa7faa1801deb632ae242ad04cea6751744155659a7fad3aff09a28033797f8";
        var corpusHash = legacy ? "90c24f8f9706c207ca3ecf8dee93d1937c16a6be45feac65d812e48853bc4621" : currentCorpusHash;
        var catalog = NativeSceneContractCatalogDriver.Load();
        var contract = (await LoadContractsAsync())[revision];
        var corpusBytes = await File.ReadAllBytesAsync(SchemaPath(corpusFile));
        Assert.Equal(corpusHash, Convert.ToHexString(SHA256.HashData(corpusBytes)).ToLowerInvariant());
        Assert.Equal(corpusBytes, catalog.GetArtifactBytes(corpusFile));
        Assert.Equal(corpusHash, catalog.GetArtifactSha256(corpusFile));
        var schemaFile = legacy ? "native-scene-probe.schema.json" : "native-scene-probe-v2.schema.json";
        var schemaBytes = await File.ReadAllBytesAsync(SchemaPath(schemaFile));
        Assert.Equal(schemaBytes, catalog.GetArtifactBytes(schemaFile));
        var draft = new Draft7ContractValidator(schemaBytes);
        var corpus = Object(JsonNode.Parse(Encoding.UTF8.GetString(corpusBytes)));
        Assert.Equal(Protocol, Text(corpus["contract"]!["protocolVersion"]));
        Assert.Equal(revision, Text(corpus["contract"]!["schemaVersion"]));
        Assert.Equal(ArtifactSchema, Text(corpus["contract"]!["artifactSchemaVersion"]));
        var cases = Assert.IsType<JsonArray>(corpus["cases"]).Select(Object).ToArray();
        var expectedCaseCount = legacy ? 24 : 26;
        Assert.Equal(expectedCaseCount, cases.Length);
        Assert.Equal(Enumerable.Range(1, expectedCaseCount), cases.Select(item => int.Parse(Text(item["id"]).AsSpan(1, 3), System.Globalization.CultureInfo.InvariantCulture)).Order());
        var current = new CurrentCatalogValidator(revision);
        var acceptedRequests = 0;
        var rejectedRequests = 0;
        var unsupportedRequests = 0;
        var rejectedStateVariants = 0;

        foreach (var fixture in cases.Where(item => item["request"] is not null))
        {
            var id = Text(fixture["id"]);
            var number = int.Parse(id.AsSpan(1, 3), System.Globalization.CultureInfo.InvariantCulture);
            var tool = Text(fixture["primitive"]);
            var request = Object(fixture["request"]);
            var json = request.ToJsonString();
            var requestEvaluation = draft.Evaluate(request, $"#/definitions/{RequestDefinition(tool)}");
            var draftValid = requestEvaluation.IsValid;
            Assert.True((number is not (2 or 17 or 18)) == draftValid,
                $"revision={revision}; case={id}; boundary=request_definition; evaluation={JsonSerializer.Serialize(requestEvaluation)}");
            var runtime = catalog.ValidateRequest(tool, json);
            var expectedCode = number switch
            {
                2 or 17 or 18 or 19 => "INVALID_TOOL_ARGUMENTS",
                3 or 26 => "UNSUPPORTED_PROTOCOL",
                _ => null,
            };
            Assert.Equal(expectedCode is null, runtime.IsValid);
            Assert.Equal(expectedCode, runtime.Code);
            if (expectedCode is null)
            {
                acceptedRequests++;
            }
            else
            {
                Assert.Equal(Text(fixture["expected"]!["errorCode"]), runtime.Code);
                if (expectedCode == "UNSUPPORTED_PROTOCOL")
                {
                    Assert.True(draftValid, $"{id} must be a supported shape with an unsupported version pair.");
                    unsupportedRequests++;
                }
                else
                {
                    rejectedRequests++;
                }
            }

            // C024 stores a valid template plus two generated oversized state values.
            if (fixture["requestStateVariants"] is JsonArray variants)
            {
                foreach (var variant in variants.Select(Object))
                {
                    var mutated = request.DeepClone().AsObject();
                    var state = CorpusStateValue(Object(variant["valueShape"]));
                    Assert.Equal(262_145, Encoding.UTF8.GetByteCount(state.ToJsonString()));
                    mutated["sceneRequest"]![Text(variant["member"])] = state;
                    var stateEvaluation = draft.Evaluate(mutated, $"#/definitions/{RequestDefinition(tool)}");
                    Assert.True(stateEvaluation.IsValid,
                        $"revision={revision}; case={id}; variant={Text(variant["member"])}; boundary=request_definition; evaluation={JsonSerializer.Serialize(stateEvaluation)}");
                    var rejected = catalog.ValidateRequest(tool, mutated.ToJsonString());
                    Assert.False(rejected.IsValid);
                    Assert.Equal(Text(fixture["expected"]!["errorCode"]), rejected.Code);
                    rejectedStateVariants++;
                }
            }
        }
        Assert.Equal(legacy ? 16 : 17, acceptedRequests);
        Assert.Equal(4, rejectedRequests);
        Assert.Equal(legacy ? 1 : 2, unsupportedRequests);
        Assert.Equal(2, rejectedStateVariants);

        // Fragment fixtures need a real successful capability envelope for the runtime
        // result validator; do not manufacture a candidate or echo a mocked declaration.
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true)));
        var session = await StartSessionAsync(driver, driver.InertProgramPath, "parity-start");
        await WaitForCandidateAsync(driver, session);
        var declaration = await CallAsync(driver, "get_ui_probe_capabilities", Arguments(session, revision), "parity-capabilities", false);
        AssertValid(contract, "get_ui_probe_capabilities", declaration);
        Assert.True(current.ValidateResult("get_ui_probe_capabilities", declaration).IsValid);
        Assert.True(draft.Evaluate(declaration).IsValid);
        var nativeBefore = await driver.ReadNativeActionsAsync();
        var acceptedResponses = 0;
        var rejectedResponses = 0;
        foreach (var fixture in cases)
        {
            var tool = Text(fixture["primitive"]);
            if (fixture["response"] is not null)
            {
                AssertResponseFixture(fixture, Text(fixture["responseSchema"]), tool, Text(fixture["id"]), "<primary>");
            }
            if (fixture["schemaInvalidVariants"] is JsonArray variants)
            {
                foreach (var variant in variants.Select(Object))
                {
                    Assert.Equal("schema_rejection", Text(variant["expected"]!["classification"]));
                    AssertResponseFixture(variant, Text(variant["responseSchema"] ?? fixture["responseSchema"]), tool, Text(fixture["id"]), Text(variant["name"]));
                }
            }
        }
        Assert.Equal(legacy ? 0 : 1, acceptedResponses);
        Assert.Equal(18, rejectedResponses);
        Assert.True(current.ValidateCorpus().IsValid);
        Assert.Equal(nativeBefore.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());

        if (legacy)
        {
            // C025 belongs only to /2; exercise its exact fixture under /1 without
            // rewriting or claiming approval of either corpus's immutable bytes.
            var currentCorpusBytes = catalog.GetArtifactBytes("parity-corpus-v2.json");
            Assert.Equal(currentCorpusHash, Convert.ToHexString(SHA256.HashData(currentCorpusBytes)).ToLowerInvariant());
            var currentCorpus = Object(JsonNode.Parse(Encoding.UTF8.GetString(currentCorpusBytes)));
            var fixture = Assert.Single(Assert.IsType<JsonArray>(currentCorpus["cases"]).Select(Object),
                item => Text(item["id"]) == "C025-capability-no-verified-candidate-is-unsupported");
            var response = Object(fixture["response"]);
            var definitionEvaluation = draft.Evaluate(response, Text(fixture["responseSchema"]));
            var envelopeEvaluation = draft.Evaluate(response);
            var rejected = current.ValidateResult("get_ui_probe_capabilities", response);
            var diagnostic = $"revision={revision}; case={Text(fixture["id"])}; variant=legacy_cross_revision; reference={Text(fixture["responseSchema"])}; " +
                $"definition={JsonSerializer.Serialize(definitionEvaluation)}; envelope={JsonSerializer.Serialize(envelopeEvaluation)}; runtimeCode={rejected.Code}";
            Assert.False(definitionEvaluation.IsValid, diagnostic);
            Assert.False(envelopeEvaluation.IsValid, diagnostic);
            Assert.False(rejected.IsValid, diagnostic);
            Assert.Equal("INVALID_TOOL_ARGUMENTS", rejected.Code);
        }

        void AssertResponseFixture(JsonObject fixture, string reference, string tool, string caseId, string variantName)
        {
            Assert.StartsWith("#/definitions/", reference, StringComparison.Ordinal);
            var definition = reference["#/definitions/".Length..];
            var response = Object(fixture["response"]);
            var classification = Text(fixture["expected"]!["classification"]);
            var expectedValid = classification != "schema_rejection";
            var fixtureEvaluation = draft.Evaluate(response, reference);
            var draftFixtureValid = fixtureEvaluation.IsValid;
            var envelope = response;
            if (definition == "probeCapabilities")
            {
                envelope = declaration.DeepClone().AsObject();
                envelope["capabilities"] = response.DeepClone();
            }
            var envelopeEvaluation = draft.Evaluate(envelope);
            var draftEnvelopeValid = envelopeEvaluation.IsValid;
            var runtime = current.ValidateResult(tool, envelope);
            var fixtureScope = definition == "probeCapabilities" ? "capabilities_fragment" : "complete_tool_envelope";
            var diagnostic = $"revision={revision}; case={caseId}; variant={variantName}; reference={reference}; tool={tool}; fixtureScope={fixtureScope}; " +
                $"classification={classification}; expectedValid={expectedValid}; draftFixtureValid={draftFixtureValid}; " +
                $"draftEnvelopeValid={draftEnvelopeValid}; runtimeValid={runtime.IsValid}; runtimeCode={runtime.Code ?? "<null>"}; " +
                $"fixtureEvaluation={JsonSerializer.Serialize(fixtureEvaluation)}; " +
                $"envelopeEvaluation={JsonSerializer.Serialize(envelopeEvaluation)}";
            Assert.True(expectedValid == draftFixtureValid, $"Declared responseSchema boundary: {diagnostic}");
            Assert.True(expectedValid == draftEnvelopeValid, $"Full tool envelope Draft-7 boundary: {diagnostic}");
            Assert.True(expectedValid == runtime.IsValid, $"Full tool envelope runtime boundary: {diagnostic}");
            Assert.True(runtime.Code == (expectedValid ? null : "INVALID_TOOL_ARGUMENTS"), $"Runtime error-code boundary: {diagnostic}");
            if (expectedValid)
            {
                Assert.Equal(Text(fixture["expected"]!["errorCode"]), Text(response["code"]));
                Assert.Equal("UNSUPPORTED_CAPABILITY", Text(response["code"]));
                Assert.False(catalog.ValidateResult(tool, envelope.ToJsonString()).IsValid);
                acceptedResponses++;
            }
            else
            {
                rejectedResponses++;
            }
        }
    }

    [Theory]
    [InlineData(LegacySchema)]
    [InlineData(CurrentSchema)]
    public async Task StandaloneWait_PreservesSelectedRevisionAndHistoricalEvidenceAfterCapture(string revision)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var contract = (await LoadContractsAsync())[revision];
        var scratch = ModernMcpScratchDirectory.Create();
        var artifactRoot = Path.Combine(scratch, "artifacts");
        try
        {
            await using var driver = await StartFixtureDriverAsync(artifactRoot, visual: false);
            var session = await StartSessionAsync(driver, FixtureExecutablePath(), "wait-start");
            await WaitForCandidateAsync(driver, session);
            await WaitForWindowAsync(await driver.ReadDescendantProcessIdAsync());
            var declaration = await CallAsync(driver, "get_ui_probe_capabilities", Arguments(session, revision), "wait-candidate", false);
            AssertValid(contract, "get_ui_probe_capabilities", declaration);
            var candidate = Object(declaration["candidate"]);
            Assert.Equal(contract.Sha256, Text(candidate["contractSetHash"]));
            var request = CaptureArguments("wait_for_ui_stable", session, revision, candidate);
            // Zero minimum duration makes the stable fixture's explicit two samples the oracle,
            // rather than depending on pipe latency to exceed the inherited 100 ms minimum.
            request["sceneRequest"]!["settlePolicy"]!["stableForMs"] = 0;
            var nativeBefore = await driver.ReadNativeActionsAsync();

            var receipt = await CallAsync(driver, "wait_for_ui_stable", request, "standalone-wait", false);
            AssertValid(contract, "wait_for_ui_stable", receipt);
            Assert.Equal("ui_stability_receipt", Text(receipt["kind"]));
            Assert.Equal(Protocol, Text(receipt["protocolVersion"]));
            Assert.Equal(revision, Text(receipt["schemaVersion"]));
            Assert.Equal(contract.Sha256, Text(receipt["candidate"]!["contractSetHash"]));
            AssertSameIdentity(candidate, Object(receipt["candidate"]));
            var historical = Object(receipt["stability"]);
            Assert.Equal("STABLE", Text(historical["status"]));
            Assert.False(historical["revalidatedByCapture"]!.GetValue<bool>());
            Assert.Equal(77L, historical["sceneEpoch"]!.GetValue<long>());
            Assert.Equal(2L, historical["sequence"]!.GetValue<long>());
            Assert.Equal(6, Object(historical["conditions"]).Count);
            Assert.All(Object(historical["conditions"]), condition => Assert.Equal("met", Text(condition.Value!["state"])));
            Assert.False(receipt.ContainsKey("artifacts"));
            Assert.False(receipt.ContainsKey("captureId"));
            AssertNoArtifacts(artifactRoot);
            var originalReceipt = receipt.ToJsonString();

            var captured = await CallAsync(driver, "capture_native_scene", request, "capture-after-wait", false);
            AssertValid(contract, "capture_native_scene", captured);
            Assert.Equal(revision, Text(captured["schemaVersion"]));
            Assert.Equal(contract.Sha256, Text(captured["candidate"]!["contractSetHash"]));
            var revalidated = Object(captured["stability"]);
            Assert.True(revalidated["revalidatedByCapture"]!.GetValue<bool>());
            Assert.True(revalidated["sequence"]!.GetValue<long>() > historical["sequence"]!.GetValue<long>());
            Assert.Equal(originalReceipt, receipt.ToJsonString());
            Assert.False(historical["revalidatedByCapture"]!.GetValue<bool>());
            AssertValid(contract, "wait_for_ui_stable", receipt);
            var later = await CallAsync(driver, "wait_for_ui_stable", request, "standalone-wait-after-capture", false);
            AssertValid(contract, "wait_for_ui_stable", later);
            Assert.Equal(Protocol, Text(later["protocolVersion"]));
            Assert.Equal(revision, Text(later["schemaVersion"]));
            Assert.Equal(contract.Sha256, Text(later["candidate"]!["contractSetHash"]));
            Assert.False(later["stability"]!["revalidatedByCapture"]!.GetValue<bool>());
            Assert.True(later["stability"]!["sequence"]!.GetValue<long>() > revalidated["sequence"]!.GetValue<long>());
            Assert.Equal(nativeBefore.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());
        }
        finally
        {
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    [Theory]
    [InlineData(LegacySchema, false)]
    [InlineData(CurrentSchema, false)]
    [InlineData(LegacySchema, true)]
    [InlineData(CurrentSchema, true)]
    public async Task UnknownOrStoppedSession_RejectsEverySceneToolWithoutNativeActionsOrArtifacts(string revision, bool stopped)
    {
        var contract = (await LoadContractsAsync())[revision];
        var scratch = ModernMcpScratchDirectory.Create();
        var artifactRoot = Path.Combine(scratch, "artifacts");
        try
        {
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_MCP_ARTIFACT_ROOT"] = artifactRoot }));
            var liveSession = await StartSessionAsync(driver, driver.InertProgramPath, "session-start");
            await WaitForCandidateAsync(driver, liveSession);
            var declaration = await CallAsync(driver, "get_ui_probe_capabilities", Arguments(liveSession, revision), "live-candidate", false);
            AssertValid(contract, "get_ui_probe_capabilities", declaration);
            var candidate = Object(declaration["candidate"]);
            var session = new string('a', 32);
            if (stopped)
            {
                var stop = await CallAsync(driver, "stop_debug", new JsonObject { ["debugSessionId"] = liveSession }, "session-stop", false);
                Assert.Equal("stop_debug_success", Text(stop["kind"]));
                session = liveSession;
            }
            else
            {
                Assert.NotEqual(liveSession, session);
            }

            var nativeBefore = await driver.ReadNativeActionsAsync();
            foreach (var tool in CaptureTools.Append("get_ui_probe_capabilities").Append("read_capture_artifact").Append("wait_for_ui_stable"))
            {
                var request = tool switch
                {
                    "get_ui_probe_capabilities" => Arguments(session, revision),
                    "read_capture_artifact" => ArtifactArguments(session, revision, "artifact_unknown_capability_0001", 0, 1),
                    _ => CaptureArguments(tool, session, revision, candidate),
                };
                var error = await CallAsync(driver, tool, request, $"unavailable-{tool}", true);
                AssertError(contract, tool, "DEBUG_SESSION_NOT_FOUND", error);
            }
            Assert.Equal(nativeBefore.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());
            AssertNoArtifacts(artifactRoot);
            if (!stopped)
            {
                var state = await CallAsync(driver, "get_debug_state", new JsonObject { ["debugSessionId"] = liveSession }, "live-session-preserved", false);
                Assert.Equal("debug_state_success", Text(state["kind"]));
                Assert.Equal(liveSession, Text(state["debugSessionId"]));
            }
        }
        finally
        {
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    [Theory]
    [InlineData(LegacySchema)]
    [InlineData(CurrentSchema)]
    public async Task MismatchedIdentity_RejectsCapturesWithoutNativeActionsOrArtifacts(string revision)
    {
        var contract = (await LoadContractsAsync())[revision];
        var scratch = ModernMcpScratchDirectory.Create();
        var artifactRoot = Path.Combine(scratch, "artifacts");
        try
        {
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true),
                AdditionalEnvironment: new Dictionary<string, string?> { ["NETCOREDBG_MCP_ARTIFACT_ROOT"] = artifactRoot }));
            var session = await StartSessionAsync(driver, driver.InertProgramPath, "mismatch-start");
            await WaitForCandidateAsync(driver, session);
            var declaration = await CallAsync(driver, "get_ui_probe_capabilities", Arguments(session, revision), "mismatch-candidate", false);
            AssertValid(contract, "get_ui_probe_capabilities", declaration);
            var candidate = Object(declaration["candidate"]);
            var nativeBefore = await driver.ReadNativeActionsAsync();
            foreach (var tool in CaptureTools)
            {
                var request = CaptureArguments(tool, session, revision, candidate);
                var actualHash = Text(candidate["executableSha256"]);
                request["sceneRequest"]!["expectedCandidateIdentity"]!["executableSha256"] =
                    (actualHash[0] == 'a' ? "b" : "a") + actualHash[1..];
                var error = await CallAsync(driver, tool, request, $"mismatch-{tool}", true);
                AssertError(contract, tool, "CANDIDATE_MISMATCH", error);
            }
            Assert.Equal(nativeBefore.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());
            AssertNoArtifacts(artifactRoot);
            var after = await CallAsync(driver, "get_ui_probe_capabilities", Arguments(session, revision), "mismatch-session-preserved", false);
            AssertValid(contract, "get_ui_probe_capabilities", after);
            AssertSameIdentity(candidate, Object(after["candidate"]));
        }
        finally
        {
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    private static async Task<CapturedArtifact> ObserveCaptureAsync(
        ModernMcpProcessDriver driver, string session, string tool, Contract contract,
        JsonObject request, JsonObject candidate, JsonObject manifest, int index)
    {
        AssertValid(contract, tool, manifest);
        Assert.Equal(tool switch
        {
            "capture_element_snapshot" => "element_snapshot_capture",
            "capture_native_scene" => "native_scene_capture",
            _ => "visual_evidence_capture",
        }, Text(manifest["kind"]));
        Assert.Equal("COMPLETE", Text(manifest["status"]));
        Assert.Equal(Protocol, Text(manifest["protocolVersion"]));
        Assert.Equal(contract.Version, Text(manifest["schemaVersion"]));
        Assert.Equal(contract.Sha256, Text(manifest["candidate"]!["contractSetHash"]));
        AssertSameIdentity(candidate, Object(manifest["candidate"]));
        Assert.True(JsonNode.DeepEquals(request["sceneRequest"], manifest["sceneRequest"]));
        Assert.True(JsonNode.DeepEquals(request["evidenceScope"], manifest["evidenceScope"]));
        Assert.True(JsonNode.DeepEquals(request["element"], manifest["element"]));
        var visual = tool == "capture_visual_evidence";
        var descriptor = Assert.Single(Assert.IsType<JsonArray>(manifest["artifacts"]).Select(Object),
            item => Text(item["mediaType"]) == (visual ? "image/png" : SceneMediaType));
        Assert.Equal(visual ? "lossless_visual" : "observed_facts", Text(descriptor["evidenceGrade"]));
        var bytes = await ReadArtifactAsync(driver, session, contract, descriptor, $"original-{index}");
        await AssertArtifactProvenanceAsync(tool, manifest, bytes);
        return new CapturedArtifact(index, manifest, descriptor, bytes);
    }

    private static async Task AssertArtifactProvenanceAsync(string tool, JsonObject manifest, byte[] bytes)
    {
        if (tool == "capture_visual_evidence")
        {
            Assert.True(bytes.Length > 24);
            Assert.Equal(new byte[] { 137, 80, 78, 71, 13, 10, 26, 10 }, bytes[..8]);
            return;
        }

        var artifact = Object(JsonNode.Parse(Encoding.UTF8.GetString(bytes)));
        var schema = await JsonSchema.FromJsonAsync(await File.ReadAllTextAsync(SchemaPath("native-scene-artifact.schema.json")));
        Assert.Empty(schema.Validate(artifact.ToJsonString()));
        Assert.Equal("native_scene_artifact", Text(artifact["kind"]));
        Assert.Equal(ArtifactSchema, Text(artifact["schemaVersion"]));
        Assert.Equal(Protocol, Text(artifact["protocolVersion"]));
        Assert.Equal(tool == "capture_element_snapshot" ? "element_snapshot" : "native_scene", Text(artifact["observationKind"]));
        Assert.Equal(Text(manifest["captureId"]), Text(artifact["captureId"]));
        foreach (var property in new[] { "capturedAt", "status", "candidate", "sceneRequest", "atomicity", "stability", "issues" })
        {
            Assert.True(JsonNode.DeepEquals(manifest[property], artifact[property]), $"Stored artifact provenance '{property}' changed.");
        }
        var nodes = Assert.IsType<JsonArray>(artifact["graph"]!["nodes"]);
        Assert.NotEmpty(nodes);
        if (tool == "capture_element_snapshot")
        {
            Assert.Single(nodes);
        }
        else
        {
            Assert.Equal("in_process_framework_probe", Text(artifact["atomicity"]!["authority"]));
            Assert.Equal(77, artifact["atomicity"]!["layoutStateRevisionBefore"]!.GetValue<int>());
            Assert.Equal(77, artifact["atomicity"]!["layoutStateRevisionAfter"]!.GetValue<int>());
        }
        Assert.Contains("SaveButton", nodes.ToJsonString(), StringComparison.Ordinal);
        Assert.Contains("Save scene", nodes.ToJsonString(), StringComparison.Ordinal);
    }

    private static async Task<byte[]> ReadArtifactAsync(ModernMcpProcessDriver driver, string session, Contract contract, JsonObject descriptor, string id)
    {
        var length = descriptor["byteLength"]!.GetValue<int>();
        Assert.True(length >= 3);
        var chunkSize = Math.Min(65_536, Math.Max(1, length / 3));
        var bytes = new List<byte>(length);
        var reads = 0;
        while (bytes.Count < length)
        {
            var offset = bytes.Count;
            var chunk = await CallAsync(driver, "read_capture_artifact",
                ArtifactArguments(session, contract.Version, Text(descriptor["artifactId"]), offset, chunkSize), $"{id}-{offset}", false);
            AssertValid(contract, "read_capture_artifact", chunk);
            Assert.Equal("capture_artifact_chunk", Text(chunk["kind"]));
            Assert.Equal(Text(descriptor["artifactId"]), Text(chunk["artifactId"]));
            Assert.Equal(Text(descriptor["mediaType"]), Text(chunk["mediaType"]));
            Assert.Equal(Text(descriptor["sha256"]), Text(chunk["sha256"]));
            Assert.Equal(ArtifactSchema, Text(chunk["artifactSchemaVersion"]));
            Assert.Equal(length, chunk["byteLength"]!.GetValue<int>());
            Assert.Equal(offset, chunk["offset"]!.GetValue<int>());
            var data = Convert.FromBase64String(Text(chunk["dataBase64"]));
            Assert.Equal(Convert.ToBase64String(data), Text(chunk["dataBase64"]));
            Assert.InRange(data.Length, 1, chunkSize);
            Assert.Equal(data.Length, chunk["bytesRead"]!.GetValue<int>());
            Assert.Equal(offset + data.Length == length, chunk["endOfArtifact"]!.GetValue<bool>());
            bytes.AddRange(data);
            reads++;
        }
        Assert.True(reads >= 3);
        Assert.Equal(length, bytes.Count);
        var reconstructed = bytes.ToArray();
        Assert.Equal(Text(descriptor["sha256"]), Convert.ToHexString(SHA256.HashData(reconstructed)).ToLowerInvariant());
        return reconstructed;
    }

    private static async Task<Dictionary<string, Contract>> LoadContractsAsync()
    {
        var contracts = new Dictionary<string, Contract>(StringComparer.Ordinal);
        foreach (var (version, file, expectedHash) in new[]
        {
            (LegacySchema, "native-scene-probe.schema.json", "f446166f9a1062d3e1a2190327d06c04905e76a1c1f81af16c87572394f90022"),
            (CurrentSchema, "native-scene-probe-v2.schema.json", "9a9de59e1f9803b0965f07f8edcb61876cd93e18dfe121ad02b502dd724e1dfe"),
        })
        {
            var bytes = await File.ReadAllBytesAsync(SchemaPath(file));
            var hash = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant();
            Assert.Equal(expectedHash, hash);
            contracts.Add(version, new Contract(version, hash, await JsonSchema.FromJsonAsync(Encoding.UTF8.GetString(bytes))));
        }
        Assert.NotEqual(contracts[LegacySchema].Sha256, contracts[CurrentSchema].Sha256);
        return contracts;
    }

    private static void AssertValid(Contract contract, string tool, JsonObject content)
    {
        var definition = Text(content["kind"]) == "tool_error" ? tool switch
        {
            "get_ui_probe_capabilities" => "getUiProbeCapabilitiesToolError",
            "capture_element_snapshot" => "captureElementSnapshotToolError",
            "capture_native_scene" => "captureNativeSceneToolError",
            "capture_visual_evidence" => "captureVisualEvidenceToolError",
            "read_capture_artifact" => "readCaptureArtifactToolError",
            "wait_for_ui_stable" => "waitForUiStableToolError",
            _ => throw new ArgumentOutOfRangeException(nameof(tool)),
        } : tool switch
        {
            "get_ui_probe_capabilities" => "uiProbeCapabilities",
            "capture_element_snapshot" => "elementSnapshotCapture",
            "capture_native_scene" => "nativeSceneCapture",
            "capture_visual_evidence" => "visualEvidenceCapture",
            "read_capture_artifact" => "captureArtifactChunk",
            "wait_for_ui_stable" => "stabilityReceipt",
            _ => throw new ArgumentOutOfRangeException(nameof(tool)),
        };
        Assert.Empty(contract.Schema.Definitions[definition].ActualSchema.Validate(content.ToJsonString()));
        if (contract.Version == LegacySchema)
        {
            Assert.True(NativeSceneContractCatalogDriver.Load().ValidateResult(tool, content.ToJsonString()).IsValid);
        }
    }

    private static void AssertError(Contract contract, string tool, string code, JsonObject error)
    {
        Assert.Equal("tool_error", Text(error["kind"]));
        Assert.Equal(tool, Text(error["tool"]));
        Assert.Equal(code, Text(error["code"]));
        Assert.Equal(["code", "kind", "message", "tool"], error.Select(property => property.Key).Order(StringComparer.Ordinal));
        AssertValid(contract, tool, error);
    }

    private static void AssertSameIdentity(JsonObject expected, JsonObject actual)
    {
        foreach (var property in new[] { "processId", "processIdentity", "executableSha256", "assemblyVersion", "probeVersion", "source" })
        {
            Assert.True(JsonNode.DeepEquals(expected[property], actual[property]), $"Candidate identity '{property}' changed.");
        }
    }

    private static void AssertNoArtifacts(string root)
    {
        if (Directory.Exists(root))
        {
            Assert.Empty(Directory.EnumerateFileSystemEntries(root, "*", SearchOption.AllDirectories));
        }
    }

    private static Task<ModernMcpProcessDriver> StartFixtureDriverAsync(string artifactRoot, bool visual) =>
        ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(
                SpawnWindowedDescendant: true,
                WindowedDescendantExecutablePath: FixtureExecutablePath(),
                WindowedDescendantArguments: ["--native-scene-probe-test-harness", "--native-scene-probe-mode=stable"]),
            AdditionalEnvironment: new Dictionary<string, string?>
            {
                ["NETCOREDBG_MCP_ARTIFACT_ROOT"] = artifactRoot,
                ["FLAUI_BRIDGE_PATH"] = visual ? BridgeAssemblyPath() : null,
            }));

    private static string FixtureExecutablePath()
    {
        var path = Path.Combine(RepositoryLayout.Root, "host", "NetCoreDbg.Mcp.Stateless.Tests", "Fixtures", "NativeSceneProbe.WpfFixture", "bin", Configuration(), "net8.0-windows", "NativeSceneProbe.WpfFixture.exe");
        Assert.True(File.Exists(path), $"Built WPF fixture is absent: '{path}'.");
        return path;
    }

    private static string BridgeAssemblyPath()
    {
        var directory = Path.Combine(RepositoryLayout.Root, "bridge", "bin", Configuration(), "net8.0-windows");
        return new[] { Path.Combine(directory, "win-x64", "FlaUIBridge.dll"), Path.Combine(directory, "FlaUIBridge.dll") }.FirstOrDefault(File.Exists)
            ?? throw new InvalidOperationException("Built FlaUI bridge assembly is absent.");
    }

    private static string Configuration() => new DirectoryInfo(AppContext.BaseDirectory).Parent?.Name
        ?? throw new InvalidOperationException("Test output configuration is absent.");

    private static async Task<string> StartSessionAsync(ModernMcpProcessDriver driver, string program, string id)
    {
        var started = await CallAsync(driver, "start_debug", new JsonObject { ["program"] = program }, id, false);
        Assert.Equal("start_debug_success", Text(started["kind"]));
        return Text(started["debugSessionId"]);
    }

    private static async Task WaitForCandidateAsync(ModernMcpProcessDriver driver, string session)
    {
        using (var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2)))
        {
            await driver.WaitForFixtureEventAsync("process", observation.Token);
        }
        // The DAP response is the existing reader-order barrier, not a retry of capability lookup.
        var threads = await CallAsync(driver, "get_threads", new JsonObject { ["debugSessionId"] = session }, "process-reader-barrier", false);
        Assert.Equal("threads_success", Text(threads["kind"]));
    }

    private static async Task WaitForWindowAsync(int processId)
    {
        using var process = System.Diagnostics.Process.GetProcessById(processId);
        var elapsed = System.Diagnostics.Stopwatch.StartNew();
        while (elapsed.Elapsed < TimeSpan.FromSeconds(2))
        {
            process.Refresh();
            if (!process.HasExited && process.MainWindowHandle != IntPtr.Zero)
            {
                return;
            }
            await Task.Delay(TimeSpan.FromMilliseconds(25));
        }
        Assert.Fail($"Controlled WPF window did not become ready for pid={processId}.");
    }

    private static JsonObject CaptureArguments(string tool, string session, string revision, JsonObject candidate)
    {
        var request = Arguments(session, revision);
        request["sceneRequest"] = SceneRequest(candidate);
        if (tool == "capture_visual_evidence")
        {
            request["evidenceScope"] = new JsonObject { ["kind"] = "window" };
        }
        else if (tool == "capture_element_snapshot")
        {
            request["element"] = new JsonObject
            {
                ["contractId"] = "Button.Primary", ["instanceKey"] = "save", ["templatePart"] = null,
                ["storyScope"] = "Button.Primary.Default", ["componentSlot"] = "content", ["automationId"] = "SaveButton",
            };
        }
        return request;
    }

    private static JsonObject SceneRequest(JsonObject candidate) => new()
    {
        ["storyId"] = "Button.Primary", ["sceneId"] = "Default", ["fixtureId"] = "Gallery",
        ["scope"] = new JsonObject { ["kind"] = "scene" },
        ["appearance"] = "light", ["theme"] = "baseline", ["density"] = "regular", ["contrast"] = "standard",
        ["viewport"] = new JsonObject { ["policy"] = "exact", ["width"] = 1280, ["height"] = 720 },
        ["expectedDpiPolicy"] = new JsonObject { ["mode"] = "exact", ["x"] = 96, ["y"] = 96 },
        ["focusTarget"] = null, ["selectedState"] = null, ["currentState"] = null, ["scrollOffsets"] = null,
        ["animationPolicy"] = "finished",
        ["settlePolicy"] = new JsonObject
        {
            ["timeoutMs"] = 30_000, ["sampleCount"] = 2, ["stableForMs"] = 100,
            ["requireDispatcherIdle"] = true, ["requireStableLayout"] = true, ["requireAnimationState"] = true,
            ["requireWindowGeometry"] = true, ["requireContextMaterialization"] = true, ["requireAsyncLoadSettled"] = true,
        },
        ["contractSetHash"] = new string('a', 64),
        ["expectedCandidateIdentity"] = new JsonObject
        {
            ["executableSha256"] = candidate["executableSha256"]?.DeepClone(),
            ["assemblyVersion"] = candidate["assemblyVersion"]?.DeepClone(),
            ["probeVersion"] = candidate["probeVersion"]?.DeepClone(),
        },
    };

    private static JsonObject Arguments(string session, string revision) => new()
    {
        ["debugSessionId"] = session, ["protocolVersion"] = Protocol, ["schemaVersion"] = revision,
    };

    private static JsonObject ArtifactArguments(string session, string revision, string artifactId, int offset, int maximum)
    {
        var request = Arguments(session, revision);
        request["artifactId"] = artifactId;
        request["offset"] = offset;
        request["maxBytes"] = maximum;
        return request;
    }

    private static async Task<JsonObject> CallAsync(ModernMcpProcessDriver driver, string tool, JsonObject arguments, string id, bool isError)
    {
        var response = await driver.CallToolRawAsync(tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId(id), timeout: TimeSpan.FromSeconds(30));
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", Text(result["resultType"]));
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() ?? false);
        var content = Object(result["structuredContent"]);
        var block = Object(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", Text(block["type"]));
        Assert.Equal(content.ToJsonString(), Text(block["text"]));
        return content;
    }

    private static string RequestDefinition(string tool) => tool switch
    {
        "get_ui_probe_capabilities" => "getUiProbeCapabilitiesArguments",
        "capture_element_snapshot" => "captureElementSnapshotArguments",
        "capture_native_scene" => "captureNativeSceneArguments",
        "capture_visual_evidence" => "captureVisualEvidenceArguments",
        "read_capture_artifact" => "readCaptureArtifactArguments",
        "wait_for_ui_stable" => "waitForUiStableArguments",
        _ => throw new ArgumentOutOfRangeException(nameof(tool)),
    };

    private static JsonArray CorpusStateValue(JsonObject shape)
    {
        Assert.Equal("array", Text(shape["type"]));
        Assert.Equal(2, shape["depth"]!.GetValue<int>());
        var stringLengths = Object(shape["stringLengths"]);
        var longStringCount = stringLengths["256"]!.GetValue<int>();
        var shortStringCount = stringLengths["252"]!.GetValue<int>();
        var remainingLongStrings = longStringCount;
        var outerItems = shape["outerItems"]!.GetValue<int>();
        var innerItems = shape["innerItems"]!.GetValue<int>();
        Assert.Equal(outerItems * innerItems, longStringCount + shortStringCount);
        var state = new JsonArray();
        for (var outer = 0; outer < outerItems; outer++)
        {
            var inner = new JsonArray();
            for (var index = 0; index < innerItems; index++)
            {
                var useLongString = remainingLongStrings > 0;
                if (useLongString)
                {
                    remainingLongStrings--;
                }
                inner.Add(new string('x', useLongString ? 256 : 252));
            }
            state.Add(inner);
        }
        Assert.Equal(0, remainingLongStrings);
        Assert.Equal(shape["serializedUtf8Bytes"]!.GetValue<int>(), Encoding.UTF8.GetByteCount(state.ToJsonString()));
        return state;
    }

    private sealed class Draft7ContractValidator
    {
        private readonly Draft7.BuildOptions _buildOptions = new()
        {
            Dialect = Draft7.Dialect.Draft07,
            SchemaRegistry = new(),
            DialectRegistry = new(),
            VocabularyRegistry = new(),
        };
        private readonly Draft7.EvaluationOptions _evaluationOptions = new()
        {
            OutputFormat = Draft7.OutputFormat.List,
            RequireFormatValidation = true,
        };
        private readonly Draft7.JsonSchema _root;
        private readonly string _schemaId;
        private readonly Dictionary<string, Draft7.JsonSchema> _references = new(StringComparer.Ordinal);

        internal Draft7ContractValidator(byte[] schemaBytes)
        {
            using var document = JsonDocument.Parse(schemaBytes);
            Assert.Equal("http://json-schema.org/draft-07/schema#", document.RootElement.GetProperty("$schema").GetString());
            _schemaId = document.RootElement.GetProperty("$id").GetString()!;
            _root = Draft7.JsonSchema.Build(document.RootElement.Clone(), _buildOptions);
        }

        internal Draft7.EvaluationResults Evaluate(JsonObject instance, string? reference = null)
        {
            var schema = _root;
            if (reference is not null)
            {
                Assert.StartsWith("#/definitions/", reference, StringComparison.Ordinal);
                if (!_references.TryGetValue(reference, out schema))
                {
                    // Resolve the definition through the original document's actual URI.
                    // Extracting a subschema would lose the root for its internal $refs.
                    var wrapper = new JsonObject
                    {
                        ["$schema"] = "http://json-schema.org/draft-07/schema#",
                        ["$ref"] = _schemaId + reference,
                    };
                    schema = Draft7.JsonSchema.FromText(wrapper.ToJsonString(), _buildOptions);
                    _references.Add(reference, schema);
                }
            }
            return schema.Evaluate(JsonSerializer.SerializeToElement(instance), _evaluationOptions);
        }
    }

    private sealed class CurrentCatalogValidator
    {
        private readonly object _context;
        private readonly MethodInfo _validateResult;
        private readonly MethodInfo _validateCorpus;

        internal CurrentCatalogValidator(string revision)
        {
            var assemblyPath = TestOutputPathResolver.ResolveManagedAssembly(
                RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");
            var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyPath);
            var catalog = assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneContractCatalog", throwOnError: true)!;
            const BindingFlags flags = BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic;
            var select = catalog.GetMethod("SelectContract", flags, binder: null, types: [typeof(string), typeof(string)], modifiers: null)
                ?? throw new InvalidOperationException("Versioned contract selection is absent.");
            _context = select.Invoke(null, [Protocol, revision])
                ?? throw new InvalidOperationException("Versioned contract selection returned no context.");
            _validateResult = catalog.GetMethod("ValidateResult", flags, binder: null, types: [typeof(string), typeof(string), _context.GetType()], modifiers: null)
                ?? throw new InvalidOperationException("Versioned result validation is absent.");
            _validateCorpus = catalog.GetMethod("ValidateCorpus", flags, binder: null, types: [_context.GetType()], modifiers: null)
                ?? throw new InvalidOperationException("Versioned corpus validation is absent.");
        }

        internal (bool IsValid, string? Code) ValidateResult(string tool, JsonObject content) =>
            ReadValidation(_validateResult.Invoke(null, [tool, content.ToJsonString(), _context]));

        internal (bool IsValid, string? Code) ValidateCorpus() =>
            ReadValidation(_validateCorpus.Invoke(null, [_context]));

        private static (bool IsValid, string? Code) ReadValidation(object? value)
        {
            Assert.NotNull(value);
            var result = value!;
            var type = result.GetType();
            const BindingFlags flags = BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic;
            return (
                Assert.IsType<bool>(type.GetProperty("IsValid", flags)!.GetValue(result)),
                type.GetProperty("Code", flags)!.GetValue(result) as string);
        }
    }

    private static string SchemaPath(string file) => Path.Combine(RepositoryLayout.Root, "specs", "004-native-scene-probe", "contracts", file);
    private static JsonObject Object(JsonNode? node) => Assert.IsType<JsonObject>(node);
    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();
    private sealed record Contract(string Version, string Sha256, JsonSchema Schema);
    private sealed record CapturedArtifact(int Index, JsonObject Manifest, JsonObject Descriptor, byte[] Bytes);
}
