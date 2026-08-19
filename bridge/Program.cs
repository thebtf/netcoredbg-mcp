using System.Buffers.Binary;
using System.Globalization;
using System.IO;
using System.IO.Pipes;
using System.Text.Json;
using System.Text.Json.Nodes;
using FlaUIBridge.Commands;

namespace FlaUIBridge;

public static class Program
{
    private const int MaximumPipeRequestBytes = 1 * 1024 * 1024;
    private const int MaximumPipeResponseBytes = 96 * 1024 * 1024;

    public static async Task Main(string[] args)
    {
        AppDomain.CurrentDomain.ProcessExit += (_, _) => ModifierCommands.ReleaseAllHeldModifiers();
        try
        {
            if (args.Length > 0 && string.Equals(args[0], "--native-scene-pipe", StringComparison.Ordinal))
            {
                if (TryParseNativeScenePipeArguments(args, out var pipeName, out var nonce, out var processId))
                {
                    await RunNativeScenePipeAsync(pipeName, nonce, processId);
                }

                return;
            }

            RunStdin();
        }
        finally
        {
            ModifierCommands.ReleaseAllHeldModifiers();
            JsonRpcHandler.Dispose();
        }
    }

    private static void RunStdin()
    {
        Log("FlaUIBridge started, waiting for JSON-RPC requests on stdin...");
        string? line;
        while ((line = Console.In.ReadLine()) is not null)
        {
            if (string.IsNullOrWhiteSpace(line))
                continue;

            JsonNode? response = ProcessRequest(line);
            if (response is null)
                continue;

            Console.Out.WriteLine(response.ToJsonString());
            Console.Out.Flush();
        }

        Log("stdin closed, shutting down.");
    }

    private static bool TryParseNativeScenePipeArguments(
        string[] args,
        out string pipeName,
        out string nonce,
        out int processId)
    {
        pipeName = string.Empty;
        nonce = string.Empty;
        processId = 0;
        if (args.Length != 4 || string.IsNullOrWhiteSpace(args[1]) || string.IsNullOrWhiteSpace(args[2]))
        {
            return false;
        }

        pipeName = args[1];
        nonce = args[2];
        return int.TryParse(args[3], NumberStyles.None, CultureInfo.InvariantCulture, out processId) && processId > 0;
    }

    private static async Task RunNativeScenePipeAsync(string pipeName, string nonce, int processId)
    {
        using var pipe = new NamedPipeServerStream(
            pipeName,
            PipeDirection.InOut,
            maxNumberOfServerInstances: 1,
            PipeTransmissionMode.Byte,
            PipeOptions.Asynchronous | PipeOptions.CurrentUserOnly);
        try
        {
            await pipe.WaitForConnectionAsync();
            var payload = await ReadFrameAsync(pipe, MaximumPipeRequestBytes);
            if (payload is null || JsonNode.Parse(payload) is not JsonObject envelope ||
                !string.Equals(envelope["nonce"]?.GetValue<string>(), nonce, StringComparison.Ordinal) ||
                string.IsNullOrWhiteSpace(envelope["correlationId"]?.GetValue<string>()) ||
                envelope["request"] is not JsonObject request)
            {
                return;
            }

            var response = JsonRpcHandler.HandleNativeSceneEvidence(request, processId);
            var responsePayload = JsonSerializer.SerializeToUtf8Bytes(new JsonObject
            {
                ["nonce"] = nonce,
                ["correlationId"] = envelope["correlationId"]!.DeepClone(),
                ["response"] = response,
            });
            if (responsePayload.Length > MaximumPipeResponseBytes)
            {
                return;
            }

            await WriteFrameAsync(pipe, responsePayload);
        }
        catch
        {
            // Closing the private pipe is the only failure signal; the host maps it to OBSERVER_UNAVAILABLE.
        }
    }

    private static async Task<byte[]?> ReadFrameAsync(Stream stream, int maximumPayloadBytes)
    {
        var header = new byte[sizeof(int)];
        var firstRead = await stream.ReadAsync(header.AsMemory(0, 1));
        if (firstRead == 0)
        {
            return null;
        }

        await ReadExactlyAsync(stream, header.AsMemory(firstRead));
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length <= 0 || length > maximumPayloadBytes)
        {
            throw new InvalidDataException("Native scene pipe frame length is outside the configured bound.");
        }

        var payload = new byte[length];
        await ReadExactlyAsync(stream, payload);
        return payload;
    }

    private static async Task WriteFrameAsync(Stream stream, byte[] payload)
    {
        var header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await stream.WriteAsync(header);
        await stream.WriteAsync(payload);
        await stream.FlushAsync();
    }

    private static async Task ReadExactlyAsync(Stream stream, Memory<byte> buffer)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer[offset..]);
            if (read == 0)
            {
                throw new EndOfStreamException("Native scene pipe frame ended prematurely.");
            }

            offset += read;
        }
    }

    private static JsonNode? ProcessRequest(string line)
    {
        JsonNode? id = null;
        try
        {
            var request = JsonNode.Parse(line);
            if (request is null)
                return CreateErrorResponse(null, -32700, "Parse error: null result");

            id = request["id"];
            var method = request["method"]?.GetValue<string>();
            var @params = request["params"];

            if (method is null)
                return CreateErrorResponse(id, -32600, "Invalid request: missing 'method'");

            Log($"<-- {method} (id={id})");

            if (method == "shutdown")
            {
                var shutdownResponse = CreateSuccessResponse(id, new JsonObject { ["shutdown"] = true });
                Console.Out.WriteLine(shutdownResponse.ToJsonString());
                Console.Out.Flush();
                Log("Shutdown requested, exiting.");
                JsonRpcHandler.Dispose();
                Environment.Exit(0);
                return null;
            }

            var result = JsonRpcHandler.Handle(method, @params);
            return CreateSuccessResponse(id, result);
        }
        catch (JsonException ex)
        {
            Log($"JSON parse error: {ex.Message}");
            return CreateErrorResponse(null, -32700, $"Parse error: {ex.Message}");
        }
        catch (Exception ex)
        {
            Log($"Unhandled error: {ex}");
            return CreateErrorResponse(id, -32603, $"Internal error: {ex.Message}");
        }
    }

    private static JsonNode CreateSuccessResponse(JsonNode? id, JsonNode result)
    {
        return new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = id?.DeepClone(),
            ["result"] = result
        };
    }

    private static JsonNode CreateErrorResponse(JsonNode? id, int code, string message)
    {
        return new JsonObject
        {
            ["jsonrpc"] = "2.0",
            ["id"] = id?.DeepClone(),
            ["error"] = new JsonObject
            {
                ["code"] = code,
                ["message"] = message
            }
        };
    }

    internal static void Log(string message)
    {
        Console.Error.WriteLine($"[FlaUIBridge] {message}");
    }
}
