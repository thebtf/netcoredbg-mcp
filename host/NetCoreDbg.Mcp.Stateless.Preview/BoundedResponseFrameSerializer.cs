using System.Buffers;
using System.Text;
using System.Text.Encodings.Web;
using System.Text.Json;
using System.Text.Json.Nodes;
using ModelContextProtocol;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.CodeSearch.Core;

namespace NetCoreDbg.Mcp.Stateless.Preview;

internal readonly record struct FramePreflightState(int BytesWritten, int MaximumTokenUtf16CodeUnits);

internal static class BoundedResponseFrameSerializer
{
    private const int MaximumTokenChunkUtf16CodeUnits = 128;
    private static readonly JavaScriptEncoder JsonEncoder = McpJsonUtilities.DefaultOptions.Encoder ?? JavaScriptEncoder.Default;

    internal static bool FitsWithinLimit(JsonRpcMessage message) => FitsWithinLimit(message, out _);

    internal static bool FitsWithinLimit(JsonRpcMessage message, out FramePreflightState state)
    {
        var counter = new BoundedJsonByteCounter(PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        switch (message)
        {
            case JsonRpcResponse response:
                WriteResponse(ref counter, response);
                break;
            case JsonRpcError error:
                WriteError(ref counter, error);
                break;
            default:
                counter.Fail();
                break;
        }

        state = counter.State;
        return counter.Fits;
    }

    internal static bool FitsFrameBudgetExceededResponseWithinLimit(RequestId id) =>
        FitsFrameBudgetExceededResponseWithinLimit(id, out _);

    internal static bool FitsFrameBudgetExceededResponseWithinLimit(RequestId id, out FramePreflightState state)
    {
        var counter = new BoundedJsonByteCounter(PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        WriteResponsePrefix(ref counter, id);
        WriteAscii(ref counter, ",\"result\":{\"resultType\":\"complete\",\"isError\":true,\"content\":[{\"type\":\"text\",\"text\":\""u8);
        WriteFailurePayload(ref counter, nestedInText: true);
        WriteAscii(ref counter, "\"}],\"structuredContent\":"u8);
        WriteFailurePayload(ref counter, nestedInText: false);
        WriteAscii(ref counter, "}}"u8);
        state = counter.State;
        return counter.Fits;
    }

    internal static bool FitsInvalidToolArgumentsResponseWithinLimit(RequestId id) =>
        FitsInvalidToolArgumentsResponseWithinLimit(id, out _);

    internal static bool FitsInvalidToolArgumentsResponseWithinLimit(RequestId id, out FramePreflightState state)
    {
        var counter = new BoundedJsonByteCounter(PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        WriteResponsePrefix(ref counter, id);
        WriteAscii(ref counter, ",\"result\":{\"resultType\":\"complete\",\"isError\":true,\"content\":[{\"type\":\"text\",\"text\":\""u8);
        WriteInvalidToolArgumentsPayload(ref counter, nestedInText: true);
        WriteAscii(ref counter, "\"}],\"structuredContent\":"u8);
        WriteInvalidToolArgumentsPayload(ref counter, nestedInText: false);
        WriteServerInfoMetadata(ref counter);
        WriteAscii(ref counter, "}}"u8);
        state = counter.State;
        return counter.Fits;
    }

    internal static bool FitsLegacyInitializeMethodNotFoundErrorWithinLimit(RequestId id)
    {
        var counter = new BoundedJsonByteCounter(PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        WriteResponsePrefix(ref counter, id);
        WriteAscii(ref counter, ",\"error\":{\"code\":-32601,\"message\":\"Method not found\"}}"u8);
        return counter.Fits;
    }

    internal static bool FitsUnsupportedVersionErrorWithinLimit(
        RequestId id,
        string requestedVersion,
        string supportedVersion,
        out FramePreflightState state)
    {
        var counter = new BoundedJsonByteCounter(PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        WriteResponsePrefix(ref counter, id);
        WriteAscii(ref counter, ",\"error\":{\"code\":-32022,\"message\":\"Unsupported protocol version\",\"data\":{\"requested\":"u8);
        WriteJsonString(ref counter, requestedVersion, nestedInText: false, trackToken: true);
        WriteAscii(ref counter, ",\"supported\":["u8);
        WriteJsonString(ref counter, supportedVersion, nestedInText: false, trackToken: false);
        WriteAscii(ref counter, "]}}}"u8);
        state = counter.State;
        return counter.Fits;
    }

    internal static bool FitsUnknownToolResponseWithinLimit(RequestId id, string tool, out FramePreflightState state)
    {
        var counter = new BoundedJsonByteCounter(PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        WriteResponsePrefix(ref counter, id);
        WriteAscii(ref counter, ",\"result\":{\"resultType\":\"complete\",\"isError\":true,\"content\":[{\"type\":\"text\",\"text\":\"Unknown tool: "u8);
        counter.WriteJsonStringContent(tool, trackToken: true);
        WriteAscii(ref counter, "\"}]"u8);
        WriteServerInfoMetadata(ref counter);
        WriteAscii(ref counter, "}}"u8);
        state = counter.State;
        return counter.Fits;
    }

    internal static bool FitsFindCodeSymbolSuccessResponseWithinLimit(
        RequestId id,
        IReadOnlyList<SymbolMatch> matches,
        out FramePreflightState state)
    {
        var counter = new BoundedJsonByteCounter(PreviewToolCatalog.MaximumCompleteResponseFrameBytes);
        WriteResponsePrefix(ref counter, id);
        WriteAscii(ref counter, ",\"result\":{\"resultType\":\"complete\",\"isError\":false,\"content\":[{\"type\":\"text\",\"text\":\""u8);
        WriteFindCodeSymbolPayload(ref counter, matches, nestedInText: true);
        WriteAscii(ref counter, "\"}],\"structuredContent\":"u8);
        WriteFindCodeSymbolPayload(ref counter, matches, nestedInText: false);
        WriteServerInfoMetadata(ref counter);
        WriteAscii(ref counter, "}}"u8);
        state = counter.State;
        return counter.Fits;
    }

    private static void WriteResponse(ref BoundedJsonByteCounter counter, JsonRpcResponse response)
    {
        WriteResponsePrefix(ref counter, response.Id);
        WriteAscii(ref counter, ",\"result\":"u8);
        WriteJsonNode(ref counter, response.Result);
        WriteAscii(ref counter, "}"u8);
    }

    private static void WriteError(ref BoundedJsonByteCounter counter, JsonRpcError error)
    {
        WriteResponsePrefix(ref counter, error.Id);
        WriteAscii(ref counter, ",\"error\":{\"code\":"u8);
        WriteInt32(ref counter, error.Error.Code, nestedInText: false);
        WriteAscii(ref counter, ",\"message\":"u8);
        WriteJsonString(ref counter, error.Error.Message, nestedInText: false, trackToken: true);
        if (error.Error.Data is not null)
        {
            WriteAscii(ref counter, ",\"data\":"u8);
            WriteUntypedJsonValue(ref counter, error.Error.Data);
        }

        WriteAscii(ref counter, "}}"u8);
    }

    private static void WriteServerInfoMetadata(ref BoundedJsonByteCounter counter)
    {
        WriteAscii(ref counter, ",\"_meta\":{\"io.modelcontextprotocol/serverInfo\":{\"name\":"u8);
        WriteJsonString(ref counter, PreviewToolCatalog.ServerName, nestedInText: false, trackToken: false);
        WriteAscii(ref counter, ",\"version\":"u8);
        WriteJsonString(ref counter, PreviewToolCatalog.ServerVersion, nestedInText: false, trackToken: false);
        WriteAscii(ref counter, "}}"u8);
    }

    private static void WriteResponsePrefix(ref BoundedJsonByteCounter counter, RequestId id)
    {
        WriteAscii(ref counter, "{\"jsonrpc\":\"2.0\",\"id\":"u8);
        switch (id.Id)
        {
            case string value:
                WriteJsonString(ref counter, value, nestedInText: false, trackToken: true);
                break;
            case long value:
                WriteInt64(ref counter, value, nestedInText: false);
                break;
            case null:
                WriteAscii(ref counter, "null"u8);
                break;
            default:
                counter.Fail();
                break;
        }
    }

    private static void WriteFailurePayload(ref BoundedJsonByteCounter counter, bool nestedInText)
    {
        WriteAscii(ref counter, "{\"kind\":"u8, nestedInText);
        WriteJsonString(ref counter, "preview_search_budget_exceeded", nestedInText, trackToken: false);
        WriteAscii(ref counter, ",\"error\":"u8, nestedInText);
        WriteJsonString(ref counter, "PREVIEW_SEARCH_BUDGET_EXCEEDED", nestedInText, trackToken: false);
        WriteAscii(ref counter, ",\"tool\":"u8, nestedInText);
        WriteJsonString(ref counter, PreviewToolCatalog.FindCodeSymbol, nestedInText, trackToken: false);
        WriteAscii(ref counter, "}"u8, nestedInText);
    }

    private static void WriteInvalidToolArgumentsPayload(ref BoundedJsonByteCounter counter, bool nestedInText)
    {
        WriteAscii(ref counter, "{\"kind\":"u8, nestedInText);
        WriteJsonString(ref counter, "invalid_tool_arguments", nestedInText, trackToken: false);
        WriteAscii(ref counter, ",\"error\":"u8, nestedInText);
        WriteJsonString(ref counter, "INVALID_TOOL_ARGUMENTS", nestedInText, trackToken: false);
        WriteAscii(ref counter, ",\"tool\":"u8, nestedInText);
        WriteJsonString(ref counter, PreviewToolCatalog.FindCodeSymbol, nestedInText, trackToken: false);
        WriteAscii(ref counter, "}"u8, nestedInText);
    }

    private static void WriteFindCodeSymbolPayload(
        ref BoundedJsonByteCounter counter,
        IReadOnlyList<SymbolMatch> matches,
        bool nestedInText)
    {
        WriteAscii(ref counter, "{\"kind\":"u8, nestedInText);
        WriteJsonString(ref counter, "find_code_symbol_success", nestedInText, trackToken: false);
        WriteAscii(ref counter, ",\"results\":["u8, nestedInText);
        for (var index = 0; index < matches.Count; index++)
        {
            if (index > 0)
            {
                WriteAscii(ref counter, ","u8, nestedInText);
            }

            var match = matches[index];
            WriteAscii(ref counter, "{\"file\":"u8, nestedInText);
            WriteJsonString(ref counter, match.File, nestedInText, trackToken: true);
            WriteAscii(ref counter, ",\"line\":"u8, nestedInText);
            WriteInt32(ref counter, match.Line, nestedInText);
            WriteAscii(ref counter, ",\"name\":"u8, nestedInText);
            WriteJsonString(ref counter, match.Name, nestedInText, trackToken: true);
            WriteAscii(ref counter, ",\"kind\":"u8, nestedInText);
            WriteJsonString(ref counter, match.Kind, nestedInText, trackToken: true);
            WriteAscii(ref counter, ",\"context\":"u8, nestedInText);
            WriteJsonString(ref counter, match.Context, nestedInText, trackToken: true);
            WriteAscii(ref counter, "}"u8, nestedInText);
        }

        WriteAscii(ref counter, "]}"u8, nestedInText);
    }

    private static void WriteJsonNode(ref BoundedJsonByteCounter counter, JsonNode? node)
    {
        switch (node)
        {
            case null:
                WriteAscii(ref counter, "null"u8);
                return;
            case JsonObject value:
                WriteJsonObject(ref counter, value);
                return;
            case JsonArray value:
                WriteJsonArray(ref counter, value);
                return;
            case JsonValue value:
                WriteJsonValue(ref counter, value);
                return;
            default:
                counter.Fail();
                return;
        }
    }

    private static void WriteJsonObject(ref BoundedJsonByteCounter counter, JsonObject value)
    {
        WriteAscii(ref counter, "{"u8);
        var first = true;
        foreach (var property in value)
        {
            if (!first)
            {
                WriteAscii(ref counter, ","u8);
            }

            WriteJsonString(ref counter, property.Key, nestedInText: false, trackToken: true);
            WriteAscii(ref counter, ":"u8);
            WriteJsonNode(ref counter, property.Value);
            first = false;
        }

        WriteAscii(ref counter, "}"u8);
    }

    private static void WriteJsonArray(ref BoundedJsonByteCounter counter, JsonArray value)
    {
        WriteAscii(ref counter, "["u8);
        for (var index = 0; index < value.Count; index++)
        {
            if (index > 0)
            {
                WriteAscii(ref counter, ","u8);
            }

            WriteJsonNode(ref counter, value[index]);
        }

        WriteAscii(ref counter, "]"u8);
    }

    private static void WriteJsonValue(ref BoundedJsonByteCounter counter, JsonValue value)
    {
        switch (value.GetValueKind())
        {
            case JsonValueKind.String:
                WriteJsonString(ref counter, value.GetValue<string>(), nestedInText: false, trackToken: true);
                return;
            case JsonValueKind.True:
                WriteAscii(ref counter, "true"u8);
                return;
            case JsonValueKind.False:
                WriteAscii(ref counter, "false"u8);
                return;
            case JsonValueKind.Number:
                WriteJsonNumber(ref counter, value);
                return;
            default:
                counter.Fail();
                return;
        }
    }

    private static void WriteJsonNumber(ref BoundedJsonByteCounter counter, JsonValue value)
    {
        if (value.TryGetValue<JsonElement>(out var element))
        {
            WriteJsonElement(ref counter, element);
        }
        else if (value.TryGetValue<int>(out var int32))
        {
            WriteInt32(ref counter, int32, nestedInText: false);
        }
        else if (value.TryGetValue<long>(out var int64))
        {
            WriteInt64(ref counter, int64, nestedInText: false);
        }
        else if (value.TryGetValue<uint>(out var uint32))
        {
            WriteUInt32(ref counter, uint32, nestedInText: false);
        }
        else if (value.TryGetValue<ulong>(out var uint64))
        {
            WriteUInt64(ref counter, uint64, nestedInText: false);
        }
        else
        {
            counter.Fail();
        }
    }

    private static void WriteUntypedJsonValue(ref BoundedJsonByteCounter counter, object value)
    {
        switch (value)
        {
            case JsonElement element:
                WriteJsonElement(ref counter, element);
                break;
            case JsonNode node:
                WriteJsonNode(ref counter, node);
                break;
            case string text:
                WriteJsonString(ref counter, text, nestedInText: false, trackToken: true);
                break;
            case bool boolean:
                WriteAscii(ref counter, boolean ? "true"u8 : "false"u8);
                break;
            case int int32:
                WriteInt32(ref counter, int32, nestedInText: false);
                break;
            case long int64:
                WriteInt64(ref counter, int64, nestedInText: false);
                break;
            default:
                counter.Fail();
                break;
        }
    }

    private static void WriteJsonElement(ref BoundedJsonByteCounter counter, JsonElement value)
    {
        switch (value.ValueKind)
        {
            case JsonValueKind.Object:
                WriteAscii(ref counter, "{"u8);
                var first = true;
                foreach (var property in value.EnumerateObject())
                {
                    if (!first)
                    {
                        WriteAscii(ref counter, ","u8);
                    }

                    WriteJsonString(ref counter, property.Name, nestedInText: false, trackToken: true);
                    WriteAscii(ref counter, ":"u8);
                    WriteJsonElement(ref counter, property.Value);
                    first = false;
                }

                WriteAscii(ref counter, "}"u8);
                return;
            case JsonValueKind.Array:
                WriteAscii(ref counter, "["u8);
                var index = 0;
                foreach (var element in value.EnumerateArray())
                {
                    if (index++ > 0)
                    {
                        WriteAscii(ref counter, ","u8);
                    }

                    WriteJsonElement(ref counter, element);
                }

                WriteAscii(ref counter, "]"u8);
                return;
            case JsonValueKind.String:
                WriteJsonString(ref counter, value.GetString()!, nestedInText: false, trackToken: true);
                return;
            case JsonValueKind.Number when value.TryGetInt64(out var int64):
                WriteInt64(ref counter, int64, nestedInText: false);
                return;
            case JsonValueKind.True:
                WriteAscii(ref counter, "true"u8);
                return;
            case JsonValueKind.False:
                WriteAscii(ref counter, "false"u8);
                return;
            case JsonValueKind.Null:
                WriteAscii(ref counter, "null"u8);
                return;
            default:
                counter.Fail();
                return;
        }
    }

    private static void WriteJsonString(
        ref BoundedJsonByteCounter counter,
        string value,
        bool nestedInText,
        bool trackToken)
    {
        WriteAscii(ref counter, "\""u8, nestedInText);
        counter.WriteJsonStringContent(value, trackToken, nestedInText);
        WriteAscii(ref counter, "\""u8, nestedInText);
    }

    private static void WriteInt32(ref BoundedJsonByteCounter counter, int value, bool nestedInText)
    {
        Span<char> buffer = stackalloc char[11];
        if (!value.TryFormat(buffer, out var written, provider: null))
        {
            counter.Fail();
            return;
        }

        WriteCharacters(ref counter, buffer[..written], nestedInText);
    }

    private static void WriteInt64(ref BoundedJsonByteCounter counter, long value, bool nestedInText)
    {
        Span<char> buffer = stackalloc char[20];
        if (!value.TryFormat(buffer, out var written, provider: null))
        {
            counter.Fail();
            return;
        }

        WriteCharacters(ref counter, buffer[..written], nestedInText);
    }

    private static void WriteUInt32(ref BoundedJsonByteCounter counter, uint value, bool nestedInText)
    {
        Span<char> buffer = stackalloc char[10];
        if (!value.TryFormat(buffer, out var written, provider: null))
        {
            counter.Fail();
            return;
        }

        WriteCharacters(ref counter, buffer[..written], nestedInText);
    }

    private static void WriteUInt64(ref BoundedJsonByteCounter counter, ulong value, bool nestedInText)
    {
        Span<char> buffer = stackalloc char[20];
        if (!value.TryFormat(buffer, out var written, provider: null))
        {
            counter.Fail();
            return;
        }

        WriteCharacters(ref counter, buffer[..written], nestedInText);
    }

    private static void WriteCharacters(ref BoundedJsonByteCounter counter, ReadOnlySpan<char> characters, bool nestedInText)
    {
        if (nestedInText)
        {
            counter.WriteJsonStringContent(characters);
        }
        else
        {
            counter.Advance(Encoding.UTF8.GetByteCount(characters));
        }
    }

    private static void WriteAscii(ref BoundedJsonByteCounter counter, ReadOnlySpan<byte> value, bool nestedInText = false)
    {
        if (nestedInText)
        {
            counter.WriteJsonStringContent(value);
        }
        else
        {
            counter.Advance(value.Length);
        }
    }

    private struct BoundedJsonByteCounter(int maximumLength)
    {
        private int _bytesWritten;
        private int _maximumTokenUtf16CodeUnits;
        private bool _overflowed;

        internal bool Fits => !_overflowed;

        internal FramePreflightState State => new(_bytesWritten, _maximumTokenUtf16CodeUnits);

        internal void Advance(int count)
        {
            if (_overflowed)
            {
                return;
            }

            if (count < 0 || count > maximumLength - _bytesWritten)
            {
                _bytesWritten = maximumLength;
                _overflowed = true;
                return;
            }

            _bytesWritten += count;
        }

        internal void Fail() => _overflowed = true;

        internal void WriteJsonStringContent(string value, bool trackToken, bool nestedInText = false) =>
            WriteJsonStringContent(value.AsSpan(), trackToken, nestedInText);

        internal void WriteJsonStringContent(ReadOnlySpan<char> value) =>
            WriteJsonStringContent(value, trackToken: false, nestedInText: false);

        internal void WriteJsonStringContent(ReadOnlySpan<byte> value)
        {
            Span<byte> encoded = stackalloc byte[6 * MaximumTokenChunkUtf16CodeUnits];
            while (!value.IsEmpty && !_overflowed)
            {
                var source = value[..Math.Min(value.Length, MaximumTokenChunkUtf16CodeUnits)];
                var status = JsonEncoder.EncodeUtf8(source, encoded, out var consumed, out var written, isFinalBlock: source.Length == value.Length);
                if (consumed == 0 || status == OperationStatus.InvalidData)
                {
                    Fail();
                    return;
                }

                Advance(written);
                value = value[consumed..];
            }
        }

        private void WriteJsonStringContent(ReadOnlySpan<char> value, bool trackToken, bool nestedInText)
        {
            Span<char> encoded = stackalloc char[6 * MaximumTokenChunkUtf16CodeUnits];
            while (!value.IsEmpty && !_overflowed)
            {
                var source = value[..Math.Min(value.Length, MaximumTokenChunkUtf16CodeUnits)];
                if (trackToken)
                {
                    _maximumTokenUtf16CodeUnits = Math.Max(_maximumTokenUtf16CodeUnits, source.Length);
                }

                var status = JsonEncoder.Encode(source, encoded, out var consumed, out var written, isFinalBlock: source.Length == value.Length);
                if (consumed == 0 || status == OperationStatus.InvalidData)
                {
                    Fail();
                    return;
                }

                if (nestedInText)
                {
                    WriteJsonStringContent(encoded[..written]);
                }
                else
                {
                    Advance(Encoding.UTF8.GetByteCount(encoded[..written]));
                }

                value = value[consumed..];
            }
        }
    }
}
