using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
public sealed class StructuredContentSchemaParityTests
{
    [Fact]
    public async Task LiveHost_StructuredContentMatchesEveryFrozenApplicationSchemaVariant()
    {
        var schema = JsonNode.Parse(File.ReadAllText(Path.Combine(
            RepositoryLayout.Root,
            "specs",
            "001-mcp-stateless-strangler",
            "contracts",
            "modern-front-door.schema.json")))!.AsObject();
        await using var driver = await ModernMcpProcessDriver.StartAsync(
            new ModernMcpStartOptions(DisableFormElicitation: true));
        var meta = ModernMcpProcessDriver.CurrentMeta();

        var start = Structured(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            meta,
            new RequestId("schema-start")));
        var debugSessionId = Assert.IsType<string>(start["debugSessionId"]?.GetValue<string>());
        var state = Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            meta,
            new RequestId("schema-state")));
        var stop = Structured(await driver.CallToolRawAsync(
            "stop_debug",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            meta,
            new RequestId("schema-stop")));
        var notFound = Structured(await driver.CallToolRawAsync(
            "get_debug_state",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            meta,
            new RequestId("schema-not-found")));
        var missingProgramCapability = Structured(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject(),
            meta,
            new RequestId("schema-missing-program")));
        var invalidArguments = Structured(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = "" },
            meta,
            new RequestId("schema-invalid-arguments")));

        var contents = new[]
        {
            start,
            state,
            stop,
            notFound,
            missingProgramCapability,
            invalidArguments,
        };
        Assert.Equal(6, contents.Select(static content => content["kind"]?.GetValue<string>()).Distinct(StringComparer.Ordinal).Count());
        foreach (var content in contents)
        {
            ValidateVariant(schema, content);
        }
    }

    [Fact]
    public async Task LiveHost_StructuredContentMatchesEveryFrozenThreadSchemaVariant()
    {
        // Arrange
        var schema = LoadSchema();

        // Act
        var success = await ThreadContentAsync("success", "threads-schema-success");
        var refused = await ThreadContentAsync("refused", "threads-schema-refused");
        var protocolError = await ThreadContentAsync("malformed-body", "threads-schema-protocol-error");

        // Assert
        Assert.Equal("threads_success", success["kind"]?.GetValue<string>());
        Assert.Equal("dap_threads_refused", refused["kind"]?.GetValue<string>());
        Assert.Equal("dap_threads_protocol_error", protocolError["kind"]?.GetValue<string>());
        ValidateVariant(schema, success);
        ValidateVariant(schema, refused);
        ValidateVariant(schema, protocolError);
    }
    [Fact]
    public async Task LiveHost_StructuredContentMatchesEveryFrozenCallStackSchemaVariant()
    {
        // Arrange
        var schema = LoadSchema();

        // Act
        var success = await CallStackContentAsync("success-with-total-frames", "call-stack-schema-success");
        var refused = await CallStackContentAsync("refused", "call-stack-schema-refused");
        var protocolError = await CallStackContentAsync("malformed-body", "call-stack-schema-protocol-error");

        // Assert
        Assert.Equal("call_stack_success", success["kind"]?.GetValue<string>());
        Assert.Equal(2, success["totalFrames"]?.GetValue<int>());
        Assert.Equal("dap_stack_trace_refused", refused["kind"]?.GetValue<string>());
        Assert.Equal("dap_stack_trace_protocol_error", protocolError["kind"]?.GetValue<string>());
        ValidateVariant(schema, success);
        ValidateVariant(schema, refused);
        ValidateVariant(schema, protocolError);
    }


    private static JsonObject LoadSchema() => JsonNode.Parse(File.ReadAllText(Path.Combine(
        RepositoryLayout.Root,
        "specs",
        "001-mcp-stateless-strangler",
        "contracts",
        "modern-front-door.schema.json")))!.AsObject();

    private static async Task<JsonObject> ThreadContentAsync(string responseMode, string requestId)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(
                SuppressLifecycleEvents: true,
                ThreadsResponseMode: responseMode)));
        var meta = ModernMcpProcessDriver.CurrentMeta();
        var start = Structured(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            meta,
            new RequestId($"{requestId}-start")));
        var debugSessionId = Assert.IsType<string>(start["debugSessionId"]?.GetValue<string>());
        return Structured(await driver.CallToolRawAsync(
            "get_threads",
            new JsonObject { ["debugSessionId"] = debugSessionId },
            meta,
            new RequestId(requestId)));
    }
    private static async Task<JsonObject> CallStackContentAsync(string responseMode, string requestId)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(
                SupportsDelayedStackTraceLoading: true,
                LifecycleMode: "all-stop",
                StackTraceResponseMode: responseMode)));
        var meta = ModernMcpProcessDriver.CurrentMeta();
        var start = Structured(await driver.CallToolRawAsync(
            "start_debug",
            new JsonObject { ["program"] = driver.InertProgramPath },
            meta,
            new RequestId($"{requestId}-start")));
        var debugSessionId = Assert.IsType<string>(start["debugSessionId"]?.GetValue<string>());
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(2));
        await driver.WaitForFixtureEventAsync("stopped", observation.Token);
        return Structured(await driver.CallToolRawAsync(
            "get_call_stack",
            new JsonObject { ["debugSessionId"] = debugSessionId, ["threadId"] = 1 },
            meta,
            new RequestId(requestId)));
    }


    private static JsonObject Structured(JsonRpcResponse response)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        return Assert.IsType<JsonObject>(result["structuredContent"]);
    }

    private static void ValidateVariant(JsonObject schema, JsonObject content)
    {
        var definitions = Assert.IsType<JsonObject>(schema["$defs"]);
        var variants = Assert.IsType<JsonArray>(definitions["toolStructuredContent"]?["oneOf"]);
        var kind = Assert.IsType<string>(content["kind"]?.GetValue<string>());
        var variant = Assert.Single(
            variants.Select(variant => ResolveReference(definitions, Assert.IsType<JsonObject>(variant)["$ref"]?.GetValue<string>())),
            candidate => candidate["properties"]?["kind"]?["const"]?.GetValue<string>() == kind);

        ValidateObject(definitions, variant, content);
    }

    private static void ValidateObject(JsonObject definitions, JsonObject schema, JsonObject content)
    {
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);

        var required = Assert.IsType<JsonArray>(schema["required"])
            .Select(static property => Assert.IsType<string>(property?.GetValue<string>()))
            .Order()
            .ToArray();
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.All(content, property => Assert.True(properties.ContainsKey(property.Key)));
        Assert.All(required, property => Assert.True(content.ContainsKey(property)));

        foreach (var property in content)
        {
            ValidateValue(definitions, Assert.IsType<JsonObject>(properties[property.Key]), property.Value);
        }
    }

    private static void ValidateValue(JsonObject definitions, JsonObject schema, JsonNode? value)
    {
        if (schema["$ref"] is JsonValue reference)
        {
            var resolved = ResolveReference(definitions, reference.GetValue<string>());
            if (resolved["type"]?.GetValue<string>() == "object")
            {
                ValidateObject(definitions, resolved, Assert.IsType<JsonObject>(value));
                return;
            }

            ValidateValue(definitions, resolved, value);
            return;
        }

        if (schema["const"] is JsonValue constant)
        {
            Assert.Equal(constant.GetValue<string>(), Assert.IsAssignableFrom<JsonValue>(value).GetValue<string>());
            return;
        }

        if (schema["enum"] is JsonArray values)
        {
            Assert.Contains(values, candidate => JsonNode.DeepEquals(candidate, value));
            return;
        }

        var types = schema["type"] is JsonArray typeArray
            ? typeArray.Select(static type => Assert.IsType<string>(type?.GetValue<string>()))
            : [Assert.IsType<string>(schema["type"]?.GetValue<string>())];
        Assert.Contains(types, type => HasType(value, type));
        if (value is JsonArray array)
        {
            if (schema["maxItems"] is JsonValue maximumItems)
            {
                Assert.True(array.Count <= maximumItems.GetValue<int>());
            }

            var itemSchema = Assert.IsType<JsonObject>(schema["items"]);
            foreach (var item in array)
            {
                ValidateValue(definitions, itemSchema, item);
            }
        }

        if (schema["minLength"] is JsonValue minimumLength)
        {
            Assert.True(Assert.IsAssignableFrom<JsonValue>(value).GetValue<string>().Length >= minimumLength.GetValue<int>());
        }
        if (schema["minimum"] is JsonValue minimum)
        {
            Assert.True(Assert.IsAssignableFrom<JsonValue>(value).GetValue<long>() >= minimum.GetValue<long>());
        }
        if (schema["maximum"] is JsonValue maximum)
        {
            Assert.True(Assert.IsAssignableFrom<JsonValue>(value).GetValue<long>() <= maximum.GetValue<long>());
        }
    }

    private static JsonObject ResolveReference(JsonObject definitions, string? reference)
    {
        var localReference = Assert.IsType<string>(reference);
        Assert.StartsWith("#/$defs/", localReference);
        return Assert.IsType<JsonObject>(definitions[localReference["#/$defs/".Length..]]);
    }

    private static bool HasType(JsonNode? value, string type) => type switch
    {
        "null" => value is null,
        "string" => value is JsonValue json && json.TryGetValue<string>(out _),
        "integer" => value is JsonValue json && json.TryGetValue<long>(out _),
        "array" => value is JsonArray,
        _ => false,
    };
}
