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
        Assert.Equal(required, properties.Select(static property => property.Key).Order());
        Assert.Equal(properties.Select(static property => property.Key).Order(), content.Select(static property => property.Key).Order());
        Assert.All(required, property => Assert.True(content.ContainsKey(property)));

        foreach (var property in properties)
        {
            ValidateValue(definitions, Assert.IsType<JsonObject>(property.Value), content[property.Key]);
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
        if (schema["minLength"] is JsonValue minimumLength)
        {
            Assert.True(Assert.IsAssignableFrom<JsonValue>(value).GetValue<string>().Length >= minimumLength.GetValue<int>());
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
        _ => false,
    };
}
