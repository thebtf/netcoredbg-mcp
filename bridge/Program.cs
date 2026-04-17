using System.Text.Json;
using System.Text.Json.Nodes;
using FlaUIBridge.Commands;

namespace FlaUIBridge;

public static class Program
{
    public static void Main()
    {
        AppDomain.CurrentDomain.ProcessExit += (_, _) => ModifierCommands.ReleaseAllHeldModifiers();
        Log("FlaUIBridge started, waiting for JSON-RPC requests on stdin...");

        try
        {
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
        }
        finally
        {
            ModifierCommands.ReleaseAllHeldModifiers();
        }

        Log("stdin closed, shutting down.");
        JsonRpcHandler.Dispose();
    }

    private static JsonNode? ProcessRequest(string line)
    {
        try
        {
            var request = JsonNode.Parse(line);
            if (request is null)
                return CreateErrorResponse(null, -32700, "Parse error: null result");

            var id = request["id"];
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
            return CreateErrorResponse(null, -32603, $"Internal error: {ex.Message}");
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
