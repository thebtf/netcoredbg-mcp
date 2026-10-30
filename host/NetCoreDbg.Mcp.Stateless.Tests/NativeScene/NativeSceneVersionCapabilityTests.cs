using System.Security.Cryptography;
using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NJsonSchema;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeSceneVersionCapabilityTests
{
    private const string Tool = "get_ui_probe_capabilities";
    private const string Protocol = "native-scene-probe/1";
    private const string LegacySchema = "native-scene-probe.schema/1";
    private const string CurrentSchema = "native-scene-probe.schema/2";
    private const string LegacySchemaFile = "native-scene-probe.schema.json";
    private const string CurrentSchemaFile = "native-scene-probe-v2.schema.json";

    [Fact]
    public async Task LegacyCapability_LiveSessionWithoutProcessEvent_ReturnsIntentionalTransportErrorWithoutStructuredContent()
    {
        await WithNoCandidateSessionAsync(async (driver, session) =>
        {
            var result = Complete(await CapabilityAsync(driver, session, LegacySchema, "legacy-no-candidate"), isError: true);

            Assert.False(result.ContainsKey("structuredContent"), "The frozen /1 capability result cannot represent unavailable identity as an application payload.");
            var explanation = TextContent(result);
            Assert.Contains("identity", explanation, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("unavailable", explanation, StringComparison.OrdinalIgnoreCase);
            Assert.Contains(LegacySchema, explanation, StringComparison.Ordinal);
            Assert.Contains(CurrentSchema, explanation, StringComparison.Ordinal);
            Assert.DoesNotContain("An error occurred invoking", explanation, StringComparison.OrdinalIgnoreCase);
            Assert.DoesNotContain("InvalidOperationException", explanation, StringComparison.Ordinal);
            Assert.DoesNotContain("violated its frozen", explanation, StringComparison.OrdinalIgnoreCase);
        });
    }

    [Fact]
    public async Task CurrentCapability_LiveSessionWithoutProcessEvent_ReturnsTypedUnsupportedCapabilityWithoutCandidate()
    {
        await WithNoCandidateSessionAsync(async (driver, session) =>
        {
            var content = Application(await CapabilityAsync(driver, session, CurrentSchema, "current-no-candidate"), isError: true);

            Assert.Equal("tool_error", Text(content["kind"]));
            Assert.Equal(Tool, Text(content["tool"]));
            Assert.Equal("UNSUPPORTED_CAPABILITY", Text(content["code"]));
            var message = Text(content["message"]);
            Assert.False(string.IsNullOrWhiteSpace(message));
            Assert.Contains("identity", message, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("unavailable", message, StringComparison.OrdinalIgnoreCase);
            Assert.Equal(["code", "kind", "message", "tool"], content.Select(static property => property.Key).Order(StringComparer.Ordinal));

            var legacy = NativeSceneContractCatalogDriver.Load().ValidateResult(Tool, content.ToJsonString());
            Assert.False(legacy.IsValid, "The new capability refusal must not become valid under the frozen /1 contract.");
            Assert.Equal("INVALID_TOOL_ARGUMENTS", legacy.Code);

            // Consumer-visible code and envelope assertions precede any dependency on the new schema file.
            var current = await LoadSchemaAsync(CurrentSchemaFile);
            Assert.Empty(current.Definitions["getUiProbeCapabilitiesToolError"].ActualSchema.Validate(content.ToJsonString()));
            var wrongPair = content.DeepClone().AsObject();
            wrongPair["code"] = "ARTIFACT_INTEGRITY_FAILED";
            Assert.NotEmpty(current.Definitions["getUiProbeCapabilitiesToolError"].ActualSchema.Validate(wrongPair.ToJsonString()));
        });
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task Capability_SameLiveCandidateAcrossRevisions_KeepsRequestLocalVersionAndContractHash(bool concurrent)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true, LifecycleMode: "all-stop")));
        var session = await StartSessionAsync(driver, "version-isolation-start");
        using (var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2)))
        {
            await driver.WaitForFixtureEventAsync("process", observation.Token);
        }

        // The process event's fixture publication alone does not prove that the host consumed it.
        // A subsequent DAP response supplies the existing reader-order barrier without a retry.
        await ReaderBarrierAsync(driver, session, "version-isolation-process-reader-barrier");
        await AssertLiveAsync(driver, session, "version-isolation-before");
        var before = await driver.ReadNativeActionsAsync();
        var descendant = await driver.ReadDescendantProcessIdAsync();

        string[] revisions = [LegacySchema, CurrentSchema, CurrentSchema, LegacySchema];
        JsonRpcResponse[] responses;
        if (concurrent)
        {
            responses = await Task.WhenAll(revisions.Select((revision, index) =>
                CapabilityAsync(driver, session, revision, $"parallel-revision-{index}")));
        }
        else
        {
            responses = new JsonRpcResponse[revisions.Length];
            for (var index = 0; index < revisions.Length; index++)
            {
                responses[index] = await CapabilityAsync(driver, session, revisions[index], $"serial-revision-{index}");
            }
        }

        JsonObject? firstCandidate = null;
        for (var index = 0; index < responses.Length; index++)
        {
            // In particular, /2 must succeed on the wire before its as-yet-unimplemented schema is read.
            var content = Application(responses[index], isError: false);
            Assert.Equal("ui_probe_capabilities", Text(content["kind"]));
            Assert.Equal(Protocol, Text(content["protocolVersion"]));
            Assert.Equal(revisions[index], Text(content["schemaVersion"]));
            var candidate = Object(content["candidate"]);
            Assert.Equal(descendant, candidate["processId"]!.GetValue<int>());
            Assert.Matches("^[a-f0-9]{64}$", Text(candidate["executableSha256"]));
            Assert.False(string.IsNullOrWhiteSpace(Text(candidate["processIdentity"])));
            Assert.Equal("launch_manifest", Text(candidate["source"]?["kind"]));
            Assert.Equal("verified", Text(candidate["source"]?["verification"]));
            Assert.Empty(Assert.IsType<JsonArray>(candidate["observerVersions"]));
            if (firstCandidate is null)
            {
                firstCandidate = candidate;
            }
            else
            {
                foreach (var property in new[] { "processId", "processIdentity", "executableSha256", "assemblyVersion", "probeVersion" })
                {
                    Assert.True(JsonNode.DeepEquals(firstCandidate[property], candidate[property]), $"Candidate identity field '{property}' changed between requests to the same live session.");
                }
            }

            var fileName = revisions[index] == LegacySchema ? LegacySchemaFile : CurrentSchemaFile;
            var schemaBytes = await File.ReadAllBytesAsync(SchemaPath(fileName));
            Assert.Equal(Convert.ToHexString(SHA256.HashData(schemaBytes)).ToLowerInvariant(), Text(candidate["contractSetHash"]));
            var schema = await JsonSchema.FromJsonAsync(System.Text.Encoding.UTF8.GetString(schemaBytes));
            Assert.Empty(schema.Definitions["uiProbeCapabilities"].ActualSchema.Validate(content.ToJsonString()));
            var capabilities = Object(content["capabilities"]);
            Assert.Equal([Protocol], Strings(capabilities["supportedProtocolVersions"]));
            if (revisions[index] == LegacySchema)
            {
                Assert.Equal([LegacySchema], Strings(capabilities["supportedSchemaVersions"]));
                Assert.True(NativeSceneContractCatalogDriver.Load().ValidateResult(Tool, content.ToJsonString()).IsValid);
            }
            else
            {
                Assert.Contains(CurrentSchema, Strings(capabilities["supportedSchemaVersions"]));
                Assert.False(NativeSceneContractCatalogDriver.Load().ValidateResult(Tool, content.ToJsonString()).IsValid);
            }
        }

        Assert.Equal(before.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());
        await AssertLiveAsync(driver, session, "version-isolation-after");
    }

    [Theory]
    [InlineData("native-scene-probe/2", LegacySchema)]
    [InlineData("native-scene-probe/2", CurrentSchema)]
    [InlineData(Protocol, "native-scene-probe.schema/3")]
    public async Task Capability_UnsupportedVersionCrossPairs_StayRejectedBeforeSessionLookupOrNativeIo(string protocol, string schema)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var before = await driver.ReadNativeActionsAsync();
        var content = Application(await driver.CallToolRawAsync(
            Tool, Arguments(new string('a', 32), schema, protocol), ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("unsupported-scene-cross-pair")), isError: true);

        Assert.Equal("tool_error", Text(content["kind"]));
        Assert.Equal(Tool, Text(content["tool"]));
        Assert.Equal("UNSUPPORTED_PROTOCOL", Text(content["code"]));
        Assert.Equal(before.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());
        Assert.True(NativeSceneContractCatalogDriver.Load().ValidateResult(Tool, content.ToJsonString()).IsValid);
    }

    private static async Task WithNoCandidateSessionAsync(Func<ModernMcpProcessDriver, string, Task> assertion)
    {
        var scratch = ModernMcpScratchDirectory.Create();
        var artifacts = Path.Combine(scratch, "artifacts");
        try
        {
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true, SuppressLifecycleEvents: true),
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["NETCOREDBG_MCP_ARTIFACT_ROOT"] = artifacts,
                }));
            var session = await StartSessionAsync(driver, "no-candidate-start");
            await ReaderBarrierAsync(driver, session, "no-candidate-launch-reader-barrier");
            await AssertLiveAsync(driver, session, "no-candidate-before");
            Assert.True(await driver.ReadDescendantProcessIdAsync() > 0);
            var before = await driver.ReadNativeActionsAsync();
            var transcriptPath = Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT");
            Assert.False(string.IsNullOrWhiteSpace(transcriptPath));
            var transcript = await File.ReadAllLinesAsync(transcriptPath!);
            Assert.DoesNotContain(transcript.Select(static line => JsonNode.Parse(line)!),
                static entry => entry["kind"]?.GetValue<string>() == "event" && entry["event"]?.GetValue<string>() == "process");
            Assert.False(Directory.Exists(artifacts));

            try
            {
                await assertion(driver, session);
            }
            finally
            {
                // Refusal must not evict a live session or produce a DAP request, process event, or artifact.
                await AssertLiveAsync(driver, session, "no-candidate-after");
                Assert.Equal(before.ToArray(), (await driver.ReadNativeActionsAsync()).ToArray());
                Assert.Equal(transcript, await File.ReadAllLinesAsync(transcriptPath!));
                Assert.False(Directory.Exists(artifacts));
            }
        }
        finally
        {
            await ModernMcpScratchDirectory.DeleteAsync(scratch);
        }
    }

    private static async Task<string> StartSessionAsync(ModernMcpProcessDriver driver, string id)
    {
        var content = Application(await driver.CallToolRawAsync(
            "start_debug", new JsonObject { ["program"] = driver.InertProgramPath },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(id)), isError: false);
        Assert.Equal("start_debug_success", Text(content["kind"]));
        return Text(content["debugSessionId"]);
    }

    private static async Task AssertLiveAsync(ModernMcpProcessDriver driver, string session, string id)
    {
        var content = Application(await driver.CallToolRawAsync(
            "get_debug_state", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(id)), isError: false);
        Assert.Equal("debug_state_success", Text(content["kind"]));
        Assert.Equal(session, Text(content["debugSessionId"]));
        var state = Object(content["state"]);
        Assert.False(state["event"]?.GetValue<string>() is "exited" or "terminated");
    }

    private static async Task ReaderBarrierAsync(ModernMcpProcessDriver driver, string session, string id)
    {
        var content = Application(await driver.CallToolRawAsync(
            "get_threads", new JsonObject { ["debugSessionId"] = session },
            ModernMcpProcessDriver.CurrentMeta(), new RequestId(id)), isError: false);
        Assert.Equal("threads_success", Text(content["kind"]));
    }

    private static Task<JsonRpcResponse> CapabilityAsync(ModernMcpProcessDriver driver, string session, string schema, string id) =>
        driver.CallToolRawAsync(Tool, Arguments(session, schema), ModernMcpProcessDriver.CurrentMeta(), new RequestId(id));

    private static JsonObject Arguments(string session, string schema, string protocol = Protocol) => new()
    {
        ["debugSessionId"] = session,
        ["protocolVersion"] = protocol,
        ["schemaVersion"] = schema,
    };

    private static JsonObject Complete(JsonRpcResponse response, bool isError)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", Text(result["resultType"]));
        Assert.Equal(isError, result["isError"]?.GetValue<bool>() ?? false);
        return result;
    }

    private static JsonObject Application(JsonRpcResponse response, bool isError)
    {
        var result = Complete(response, isError);
        var content = Object(result["structuredContent"]);
        Assert.Equal(content.ToJsonString(), TextContent(result));
        return content;
    }

    private static string TextContent(JsonObject result)
    {
        var text = Object(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", Text(text["type"]));
        return Text(text["text"]);
    }

    private static async Task<JsonSchema> LoadSchemaAsync(string fileName) =>
        await JsonSchema.FromJsonAsync(await File.ReadAllTextAsync(SchemaPath(fileName)));

    private static string SchemaPath(string fileName) => Path.Combine(
        RepositoryLayout.Root, "specs", "004-native-scene-probe", "contracts", fileName);

    private static string[] Strings(JsonNode? node) => Assert.IsType<JsonArray>(node).Select(Text).ToArray();

    private static JsonObject Object(JsonNode? node) => Assert.IsType<JsonObject>(node);

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();
}
