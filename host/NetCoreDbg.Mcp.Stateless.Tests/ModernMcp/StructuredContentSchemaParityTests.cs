using System.Text;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter.NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
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
    public async Task LiveHost_BreakpointLaunchStructuredContentMatchesFrozenSchemaVariants()
    {
        var schema = LoadSchema();
        var scenarios = new[]
        {
            ("success", false, "start_debug_success", (string?)null),
            ("refused", false, "dap_breakpoints_refused", "DAP_BREAKPOINTS_REFUSED"),
            ("missing-verified", false, "dap_breakpoints_protocol_error", "DAP_BREAKPOINTS_PROTOCOL_ERROR"),
            ("too-few-results", false, "dap_breakpoints_protocol_error", "DAP_BREAKPOINTS_PROTOCOL_ERROR"),
            ("success", true, "start_debug_source_unavailable", "START_DEBUG_SOURCE_UNAVAILABLE"),
        };
        foreach (var (responseMode, missingSource, expectedKind, expectedError) in scenarios)
        {
            await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(
                    SuppressLifecycleEvents: true,
                    SetBreakpointsResponseMode: responseMode)));
            var source = missingSource
                ? driver.InertProgramPath + ".missing.cs"
                : Path.Combine(RepositoryLayout.Root,
                    "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/StructuredContentSchemaParityTests.cs");
            var content = Structured(await driver.CallToolRawAsync("start_debug",
                new JsonObject
                {
                    ["program"] = driver.InertProgramPath,
                    ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2 }),
                },
                ModernMcpProcessDriver.CurrentMeta(),
                new RequestId($"breakpoints-schema-{responseMode}-{missingSource}")));

            Assert.Equal(expectedKind, content["kind"]?.GetValue<string>());
            Assert.Equal(expectedError, content["error"]?.GetValue<string>());
            if (expectedError is null)
            {
                Assert.Single(Assert.IsType<JsonArray>(content["initialBreakpoints"]));
            }
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

    [Fact]
    public async Task LiveHost_StructuredContentMatchesEveryFrozenStepOverSchemaVariant()
    {
        var schema = LoadSchema();
        var success = await StepOverContentAsync("success", "step-over-schema-success");
        var terminal = await StepOverContentAsync("terminal-before-ack", "step-over-schema-terminal");
        var timedOut = await StepOverContentAsync("timeout", "step-over-schema-timeout");
        var refused = await StepOverContentAsync("negative", "step-over-schema-refused");
        var protocolError = await StepOverContentAsync("wrong-command", "step-over-schema-protocol-error");
        var invalidArguments = await StepOverInvalidArgumentsContentAsync();

        Assert.Equal("step_over_success", success["kind"]?.GetValue<string>());
        Assert.Equal("step_over_terminal", terminal["kind"]?.GetValue<string>());
        Assert.Equal("step_over_timed_out", timedOut["kind"]?.GetValue<string>());
        Assert.Equal("dap_step_over_refused", refused["kind"]?.GetValue<string>());
        Assert.Equal("dap_step_over_protocol_error", protocolError["kind"]?.GetValue<string>());
        Assert.Equal("invalid_tool_arguments", invalidArguments["kind"]?.GetValue<string>());
        Assert.Equal("step_over", invalidArguments["tool"]?.GetValue<string>());
        foreach (var content in new[] { success, terminal, timedOut, refused, protocolError, invalidArguments })
        {
            ValidateVariant(schema, content);
        }
    }

    [Fact]
    public async Task LiveHost_RemoveBreakpointStructuredContentMatchesFrozenSchemaVariants()
    {
        var schema = LoadSchema();
        var definitions = Assert.IsType<JsonObject>(schema["$defs"]);
        var argumentsSchema = Assert.IsType<JsonObject>(definitions["removeBreakpointArguments"]);
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            FixtureConfiguration: new FixtureConfiguration(SuppressLifecycleEvents: true)));
        var meta = ModernMcpProcessDriver.CurrentMeta();
        var source = Path.Combine(RepositoryLayout.Root,
            "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/StructuredContentSchemaParityTests.cs");
        var start = Structured(await driver.CallToolRawAsync("start_debug",
            new JsonObject
            {
                ["program"] = driver.InertProgramPath,
                ["breakpoints"] = new JsonArray(new JsonObject { ["file"] = source, ["line"] = 2 }),
            },
            meta,
            new RequestId("remove-breakpoint-schema-start")));
        Assert.Equal("start_debug_success", start["kind"]?.GetValue<string>());
        var debugSessionId = Assert.IsType<string>(start["debugSessionId"]?.GetValue<string>());

        foreach (var expectedRemoved in new[] { true, false })
        {
            var arguments = new JsonObject { ["debugSessionId"] = debugSessionId, ["file"] = source, ["line"] = 2 };
            ValidateObject(definitions, argumentsSchema, JsonNode.Parse(arguments.ToJsonString())!.AsObject());
            var content = Structured(await driver.CallToolRawAsync("remove_breakpoint",
                arguments,
                meta,
                new RequestId($"remove-breakpoint-schema-{expectedRemoved}")));

            Assert.Equal("breakpoint_removed", content["kind"]?.GetValue<string>());
            Assert.Equal(Path.GetFullPath(source), content["file"]?.GetValue<string>());
            Assert.Equal(2, content["line"]?.GetValue<int>());
            Assert.Equal(expectedRemoved, content["removed"]?.GetValue<bool>());
            ValidateVariant(schema, content);
        }

        var invalidArguments = Structured(await driver.CallToolRawAsync("remove_breakpoint",
            new JsonObject { ["debugSessionId"] = debugSessionId, ["file"] = source, ["line"] = 0 },
            meta,
            new RequestId("remove-breakpoint-schema-invalid")));
        Assert.Equal("invalid_tool_arguments", invalidArguments["kind"]?.GetValue<string>());
        Assert.Equal("INVALID_TOOL_ARGUMENTS", invalidArguments["error"]?.GetValue<string>());
        Assert.Equal("remove_breakpoint", invalidArguments["tool"]?.GetValue<string>());
        ValidateVariant(schema, invalidArguments);
    }

    [Theory]
    [InlineData("ui_invoke", "uiInvokeArguments")]
    [InlineData("get_output", "getOutputArguments")]
    [InlineData("get_output_tail", "getOutputTailArguments")]
    public async Task LiveHost_InvokeAndOutputInputSchemas_MatchAdvertisedArguments(string toolName, string definitionName)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync();
        var catalog = ModernMcpProcessDriver.RequireResult(await driver.ListToolsRawAsync(
            ModernMcpProcessDriver.CurrentMeta(), new RequestId($"application-schema-{toolName}")));
        var tool = Assert.Single(
            Assert.IsType<JsonArray>(catalog["tools"]).Select(static item => Assert.IsType<JsonObject>(item)),
            item => item["name"]?.GetValue<string>() == toolName);
        var advertised = Assert.IsType<JsonObject>(tool["inputSchema"]);
        var definitions = Assert.IsType<JsonObject>(LoadSchema()["$defs"]);
        var application = Assert.IsType<JsonObject>(definitions[definitionName]);

        Assert.True(JsonNode.DeepEquals(advertised, application),
            $"The application input schema for {toolName} must match its live tools/list inputSchema. Advertised: {advertised}; application: {application}.");
        Assert.Empty(await driver.ReadNativeActionsAsync());
    }

    [Theory]
    [InlineData("whitespace")]
    [InlineData("257-ascii")]
    public void ApplicationSchema_UiInvokeSelectorRejectsWhitespaceAnd257Ascii(string scenario)
    {
        var selector = scenario == "whitespace" ? " \t\r\n" : new string('a', 257);
        var schema = LoadSchema();
        var definitions = Assert.IsType<JsonObject>(schema["$defs"]);
        var arguments = new JsonObject { ["debugSessionId"] = "opaque", ["automationId"] = selector };
        var content = new JsonObject
        {
            ["kind"] = "ui_invoke_success",
            ["automationId"] = selector,
            ["invoked"] = true,
            ["method"] = "InvokePattern",
        };

        Assert.ThrowsAny<Xunit.Sdk.XunitException>(() => ValidateObject(
            definitions, Assert.IsType<JsonObject>(definitions["uiInvokeArguments"]), arguments));
        Assert.ThrowsAny<Xunit.Sdk.XunitException>(() => ValidateVariant(schema, content));
    }

    [Fact]
    public void ApplicationSchema_UiInvokeSelectorAccepts256SupplementaryCodePoints()
    {
        var selector = string.Concat(Enumerable.Repeat("\U0001F600", 256));
        var schema = LoadSchema();
        var definitions = Assert.IsType<JsonObject>(schema["$defs"]);
        var arguments = new JsonObject { ["debugSessionId"] = "opaque", ["automationId"] = selector };
        var content = new JsonObject
        {
            ["kind"] = "ui_invoke_success",
            ["automationId"] = selector,
            ["invoked"] = true,
            ["method"] = "InvokePattern",
        };

        ValidateObject(definitions, Assert.IsType<JsonObject>(definitions["uiInvokeArguments"]), arguments);
        ValidateVariant(schema, content);
    }

    [Fact]
    public void StringLengthKeywords_CountCodePointsAndDoNotRejectNullableStrings()
    {
        var definitions = new JsonObject();
        var schema = new JsonObject
        {
            ["type"] = new JsonArray("string", "null"),
            ["minLength"] = 2,
            ["maxLength"] = 2,
        };

        ValidateValue(definitions, schema, JsonValue.Create("\U0001F600\U0001F680"));
        ValidateValue(definitions, schema, null);
        Assert.ThrowsAny<Xunit.Sdk.XunitException>(() => ValidateValue(definitions, schema, JsonValue.Create("\U0001F600")));
        Assert.ThrowsAny<Xunit.Sdk.XunitException>(() => ValidateValue(definitions, schema, JsonValue.Create("\U0001F600\U0001F680\U0001F600")));
    }

    internal static void AssertMatchesSchema(JsonObject content) => ValidateVariant(LoadSchema(), content);

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

    private static async Task<JsonObject> StepOverContentAsync(string responseMode, string requestId)
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
            DisableFormElicitation: true,
            FixtureConfiguration: new FixtureConfiguration(
                LifecycleMode: "all-stop",
                NextResponseMode: responseMode)));
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
            "step_over",
            new JsonObject { ["debugSessionId"] = debugSessionId, ["threadId"] = 1 },
            meta,
            new RequestId(requestId),
            timeout: responseMode == "timeout" ? TimeSpan.FromSeconds(35) : null));
    }

    private static async Task<JsonObject> StepOverInvalidArgumentsContentAsync()
    {
        await using var driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(DisableFormElicitation: true));
        return Structured(await driver.CallToolRawAsync(
            "step_over",
            new JsonObject { ["debugSessionId"] = "opaque" },
            ModernMcpProcessDriver.CurrentMeta(),
            new RequestId("step-over-schema-invalid")));
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
        ValidateValue(definitions, Assert.IsType<JsonObject>(definitions["toolStructuredContent"]), content);
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
            ValidateValue(definitions, ResolveReference(definitions, reference.GetValue<string>()), value);
            return;
        }

        if (schema["oneOf"] is JsonArray alternatives)
        {
            Assert.Single(alternatives, alternative => MatchesSchema(definitions, Assert.IsType<JsonObject>(alternative), value));
        }

        if (schema["const"] is JsonValue constant)
        {
            Assert.True(JsonNode.DeepEquals(constant, value), $"Expected constant {constant}, got {value}.");
            return;
        }

        if (schema["enum"] is JsonArray values)
        {
            Assert.Contains(values, candidate => JsonNode.DeepEquals(candidate, value));
            return;
        }
        if (!schema.ContainsKey("type") && schema["oneOf"] is JsonArray)
        {
            return;
        }

        if (schema["type"] is JsonValue typeValue && typeValue.TryGetValue<string>(out var objectType) && objectType == "object")
        {
            ValidateObject(definitions, schema, Assert.IsType<JsonObject>(value));
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

        if (value is JsonValue stringValue && stringValue.TryGetValue<string>(out var text))
        {
            if (schema.ContainsKey("minLength") || schema.ContainsKey("maxLength"))
            {
                var codePoints = 0;
                foreach (var rune in text.EnumerateRunes())
                {
                    codePoints++;
                }
                if (schema["minLength"] is JsonValue minimumLength)
                {
                    Assert.True(codePoints >= minimumLength.GetValue<int>());
                }
                if (schema["maxLength"] is JsonValue maximumLength)
                {
                    Assert.True(codePoints <= maximumLength.GetValue<int>());
                }
            }
            if (schema["pattern"] is JsonValue pattern)
            {
                Assert.True(Regex.IsMatch(text, pattern.GetValue<string>(), RegexOptions.CultureInvariant, TimeSpan.FromSeconds(1)));
            }
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

    private static bool MatchesSchema(JsonObject definitions, JsonObject schema, JsonNode? value)
    {
        try
        {
            ValidateValue(definitions, schema, value);
            return true;
        }
        catch (Xunit.Sdk.XunitException)
        {
            return false;
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
        "boolean" => value is JsonValue json && json.TryGetValue<bool>(out _),
        "array" => value is JsonArray,
        "object" => value is JsonObject,
        _ => false,
    };
}
