using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class PauseContractTests
{
    private const string Tool = "pause_execution";

    public static TheoryData<string, string> ValidUnavailableSessionArguments => new()
    {
        { "omitted_thread", """{"debugSessionId":"not-a-live-session"}""" },
        { "null_thread", """{"debugSessionId":"not-a-live-session","threadId":null}""" },
        { "zero_thread", """{"debugSessionId":"not-a-live-session","threadId":0}""" },
        { "negative_thread", """{"debugSessionId":"not-a-live-session","threadId":-1}""" },
        { "positive_thread", """{"debugSessionId":"not-a-live-session","threadId":1}""" },
        { "minimum_thread", """{"debugSessionId":"not-a-live-session","threadId":-2147483648}""" },
        { "maximum_thread", """{"debugSessionId":"not-a-live-session","threadId":2147483647}""" },
    };

    public static TheoryData<string, string?> InvalidArguments => new()
    {
        { "omitted_arguments", null },
        { "null_arguments", "null" },
        { "string_arguments", "\"not-an-object\"" },
        { "boolean_arguments", "true" },
        { "number_arguments", "17" },
        { "array_arguments", "[]" },
        { "omitted_debug_session_id", "{}" },
        { "null_debug_session_id", """{"debugSessionId":null}""" },
        { "empty_debug_session_id", """{"debugSessionId":""}""" },
        { "whitespace_debug_session_id", """{"debugSessionId":" \t\r\n"}""" },
        { "numeric_debug_session_id", """{"debugSessionId":17}""" },
        { "boolean_debug_session_id", """{"debugSessionId":true}""" },
        { "object_debug_session_id", """{"debugSessionId":{}}""" },
        { "array_debug_session_id", """{"debugSessionId":[]}""" },
        { "true_thread", """{"debugSessionId":"not-a-live-session","threadId":true}""" },
        { "false_thread", """{"debugSessionId":"not-a-live-session","threadId":false}""" },
        { "string_thread", """{"debugSessionId":"not-a-live-session","threadId":"1"}""" },
        { "fractional_thread", """{"debugSessionId":"not-a-live-session","threadId":1.5}""" },
        { "underflow_thread", """{"debugSessionId":"not-a-live-session","threadId":-2147483649}""" },
        { "overflow_thread", """{"debugSessionId":"not-a-live-session","threadId":2147483648}""" },
        { "object_thread", """{"debugSessionId":"not-a-live-session","threadId":{}}""" },
        { "array_thread", """{"debugSessionId":"not-a-live-session","threadId":[]}""" },
        { "extra_field", """{"debugSessionId":"not-a-live-session","threadId":null,"unexpected":true}""" },
        { "single_thread_is_not_input", """{"debugSessionId":"not-a-live-session","threadId":1,"singleThread":true}""" },
    };

    [Fact]
    public async Task ToolsList_PauseExecution_PublishesExactlyOneClosedOptionalNullableInt32Input()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(30));

        var result = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("pause-catalog")));

        var tool = Assert.Single(
            Assert.IsType<JsonArray>(result["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            static item => item["name"]?.GetValue<string>() == Tool);
        var schema = Assert.IsType<JsonObject>(tool["inputSchema"]);
        Assert.Equal("object", schema["type"]?.GetValue<string>());
        Assert.False(schema["additionalProperties"]?.GetValue<bool>() ?? true);
        Assert.Equal(["debugSessionId"], Assert.IsType<JsonArray>(schema["required"])
            .Select(static item => item?.GetValue<string>()));
        var properties = Assert.IsType<JsonObject>(schema["properties"]);
        Assert.Equal(new[] { "debugSessionId", "threadId" },
            properties.Select(static item => item.Key).Order(StringComparer.Ordinal));
        var sessionId = Assert.IsType<JsonObject>(properties["debugSessionId"]);
        Assert.Equal("string", sessionId["type"]?.GetValue<string>());
        Assert.Equal(1, sessionId["minLength"]?.GetValue<int>());
        var threadId = Assert.IsType<JsonObject>(properties["threadId"]);
        Assert.Equal(new[] { "integer", "null" }, Assert.IsType<JsonArray>(threadId["type"])
            .Select(static item => item!.GetValue<string>()).Order(StringComparer.Ordinal));
        Assert.Equal(int.MinValue, threadId["minimum"]?.GetValue<int>());
        Assert.Equal(int.MaxValue, threadId["maximum"]?.GetValue<int>());
    }

    [Theory]
    [MemberData(nameof(ValidUnavailableSessionArguments))]
    public async Task PauseExecution_ValidInputWithUnavailableSession_ReturnsCompleteNotFoundWithoutDapIo(
        string scenario,
        string argumentsJson)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(30));
        var before = await driver.ReadNativeActionsAsync();

        var response = await driver.CallToolRawAsync(
            Tool,
            JsonNode.Parse(argumentsJson)!.AsObject(),
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId($"pause-unavailable-{scenario}"));

        var content = AssertCompleteErrorEnvelope(response);
        Assert.Equal("debug_session_not_found", content["kind"]?.GetValue<string>());
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", content["error"]?.GetValue<string>());
        Assert.Equal(2, content.Count);
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    [Theory]
    [MemberData(nameof(InvalidArguments))]
    public async Task PauseExecution_InvalidInput_ReturnsCompleteInvalidArgumentsBeforeSessionLookup(
        string scenario,
        string? argumentsJson)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(startupTimeout: TimeSpan.FromSeconds(30));
        var before = await driver.ReadNativeActionsAsync();
        var parameters = new JsonObject
        {
            ["name"] = Tool,
            ["_meta"] = ModernMcpProcessDriver.CurrentMeta(),
        };
        if (argumentsJson is not null)
        {
            parameters["arguments"] = JsonNode.Parse(argumentsJson);
        }

        var response = await driver.SendRawRequestAsync(
            "tools/call",
            parameters,
            new RequestId($"pause-invalid-{scenario}"));

        var content = AssertCompleteErrorEnvelope(response);
        Assert.Equal("invalid_tool_arguments", content["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", content["error"]?.GetValue<string>());
        Assert.Equal(Tool, content["tool"]?.GetValue<string>());
        Assert.Equal(3, content.Count);
        Assert.Equal(before.Count, (await driver.ReadNativeActionsAsync()).Count);
    }

    private static JsonObject AssertCompleteErrorEnvelope(JsonRpcResponse response)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", result["resultType"]?.GetValue<string>());
        Assert.True(result["isError"]?.GetValue<bool>() ?? false);
        var structuredContent = Assert.IsType<JsonObject>(result["structuredContent"]);
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", text["type"]?.GetValue<string>());
        Assert.Equal(structuredContent.ToJsonString(), text["text"]?.GetValue<string>());
        return structuredContent;
    }
}
