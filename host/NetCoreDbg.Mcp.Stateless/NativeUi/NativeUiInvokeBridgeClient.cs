using System.Buffers.Binary;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.NativeUi;

// This transport is for the separately authorized invoke helper, never the scene observer.
internal sealed class NativeUiInvokeBridgeClient : IAsyncDisposable
{
    private const int MaximumFrameBytes = 8_192;
    private readonly NamedPipeClientStream _pipe;
    private readonly TimeSpan _timeout;
    private int _started;

    internal NativeUiInvokeBridgeClient(string pipeName, TimeSpan timeout)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(pipeName);
        if (timeout <= TimeSpan.Zero || timeout == Timeout.InfiniteTimeSpan)
            throw new ArgumentOutOfRangeException(nameof(timeout));
        _timeout = timeout;
        _pipe = new NamedPipeClientStream(".", pipeName, PipeDirection.InOut, PipeOptions.Asynchronous);
    }

    internal async Task<JsonObject> SendAsync(
        string authorizationNonce,
        JsonObject request,
        Func<bool> authorizeDispatch,
        CancellationToken cancellationToken)
    {
        if (Interlocked.Exchange(ref _started, 1) != 0)
            return NativeUiInvokeResult.Refused("UI_INVOKE_ALREADY_USED");

        var dispatchStarted = false;
        using var deadline = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
        deadline.CancelAfter(_timeout);
        try
        {
            ArgumentException.ThrowIfNullOrWhiteSpace(authorizationNonce);
            var correlationId = Convert.ToHexString(RandomNumberGenerator.GetBytes(16));
            await _pipe.ConnectAsync(deadline.Token).ConfigureAwait(false);
            await WriteRequestAsync(authorizationNonce, correlationId, request, deadline.Token).ConfigureAwait(false);
            var ready = await ReadResponseAsync(authorizationNonce, correlationId, deadline.Token).ConfigureAwait(false);
            if (TryReadRefusal(ready, out var refusal))
                return refusal;
            if (ready.Count != 1 || ReadString(ready, "status") != "ready")
                throw new InvalidDataException("The invoke helper did not prepare the requested element.");

            // Preparation cannot invoke. Authorize the commit only after the potentially slow UIA reads.
            deadline.Token.ThrowIfCancellationRequested();
            if (!authorizeDispatch())
                return NativeUiInvokeResult.Refused("UI_INVOKE_TARGET_CHANGED");
            deadline.Token.ThrowIfCancellationRequested();
            dispatchStarted = true;
            await WriteRequestAsync(
                authorizationNonce, correlationId, new JsonObject { ["operation"] = "invoke" }, deadline.Token).ConfigureAwait(false);
            var response = await ReadResponseAsync(authorizationNonce, correlationId, deadline.Token).ConfigureAwait(false);
            if (TryReadRefusal(response, out refusal))
                return refusal;
            if (response.Count == 1 && ReadString(response, "status") == "succeeded")
                return NativeUiInvokeResult.Success(ReadString(request, "automationId"));
            return NativeUiInvokeResult.Unknown();
        }
        catch (Exception)
        {
            // A partial commit write, cancellation, lost reply, or provider exception can hide a completed action.
            return dispatchStarted ? NativeUiInvokeResult.Unknown() : NativeUiInvokeResult.Refused("UI_INVOKE_UNAVAILABLE");
        }
        finally
        {
            ClosePipe();
        }
    }

    public ValueTask DisposeAsync()
    {
        ClosePipe();
        return ValueTask.CompletedTask;
    }

    private void ClosePipe()
    {
        try
        {
            _pipe.Dispose();
        }
        catch (Exception)
        {
            // Cleanup cannot replace a known result with a claim that no invocation occurred.
        }
    }

    private async Task WriteRequestAsync(string nonce, string correlationId, JsonObject request, CancellationToken cancellationToken)
    {
        var payload = JsonSerializer.SerializeToUtf8Bytes(new { nonce, correlationId, request });
        if (payload.Length > MaximumFrameBytes)
            throw new InvalidDataException("The invoke request exceeds its frame bound.");
        var header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await _pipe.WriteAsync(header, cancellationToken).ConfigureAwait(false);
        await _pipe.WriteAsync(payload, cancellationToken).ConfigureAwait(false);
        await _pipe.FlushAsync(cancellationToken).ConfigureAwait(false);
    }

    private async Task<JsonObject> ReadResponseAsync(string nonce, string correlationId, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        await _pipe.ReadExactlyAsync(header, cancellationToken).ConfigureAwait(false);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length is < 1 or > MaximumFrameBytes)
            throw new InvalidDataException("The invoke response exceeds its frame bound.");
        var payload = new byte[length];
        await _pipe.ReadExactlyAsync(payload, cancellationToken).ConfigureAwait(false);
        if (JsonNode.Parse(payload) is not JsonObject envelope || envelope.Count != 3 ||
            !StringComparer.Ordinal.Equals(ReadString(envelope, "nonce"), nonce) ||
            !StringComparer.Ordinal.Equals(ReadString(envelope, "correlationId"), correlationId) ||
            envelope["response"] is not JsonObject response)
        {
            throw new InvalidDataException("The invoke response does not match the authorized request.");
        }
        return response;
    }

    private static bool TryReadRefusal(JsonObject response, out JsonObject refusal)
    {
        refusal = null!;
        if (response.Count != 2 || ReadString(response, "status") != "refused")
            return false;
        var code = ReadString(response, "code");
        if (code is not ("INVALID_TOOL_ARGUMENTS" or "UI_INVOKE_TARGET_UNAVAILABLE" or "UI_INVOKE_TARGET_CHANGED" or
            "ELEMENT_NOT_FOUND" or "ELEMENT_AMBIGUOUS" or "UI_INVOKE_SELECTION_UNREADABLE" or "UI_INVOKE_PATTERN_UNSUPPORTED"))
        {
            throw new InvalidDataException("The invoke helper returned an unknown refusal.");
        }
        refusal = NativeUiInvokeResult.Refused(code);
        return true;
    }

    private static string ReadString(JsonObject source, string name) =>
        source[name] is JsonValue value && value.TryGetValue<string>(out var text)
            ? text : throw new InvalidDataException("The invoke response contains an invalid string.");
}

internal static class NativeUiInvokeResult
{
    internal static JsonObject Success(string automationId) => new()
    {
        ["kind"] = "ui_invoke_success",
        ["automationId"] = automationId,
        ["invoked"] = true,
        ["method"] = "InvokePattern",
    };

    internal static JsonObject Refused(string code) => new()
    {
        ["kind"] = "tool_error",
        ["tool"] = "ui_invoke",
        ["code"] = code,
        ["outcome"] = "not_invoked",
        ["message"] = code switch
        {
            "DEBUG_SESSION_NOT_FOUND" => "The debug session is unavailable or retired.",
            "UI_INVOKE_TARGET_UNAVAILABLE" => "The bound debuggee process or main window cannot be established.",
            "UI_INVOKE_TARGET_CHANGED" => "The session, process, window, or selected element changed before dispatch.",
            "ELEMENT_NOT_FOUND" => "No element matches the exact AutomationId in the bound window.",
            "ELEMENT_AMBIGUOUS" => "More than one element matches the exact AutomationId in the bound window.",
            "UI_INVOKE_SELECTION_UNREADABLE" => "The complete bounded selector or element identity could not be read.",
            "UI_INVOKE_PATTERN_UNSUPPORTED" => "The selected element does not support InvokePattern; no input fallback is used.",
            "UI_INVOKE_BUSY" => "Another invoke operation is already active in this session.",
            "UI_INVOKE_ALREADY_USED" => "This one-shot invoke transport cannot be reused or retried.",
            "INVALID_TOOL_ARGUMENTS" => "The invoke request is invalid.",
            _ => "The local invoke helper is unavailable before dispatch.",
        },
    };

    internal static JsonObject Unknown() => new()
    {
        ["kind"] = "tool_error",
        ["tool"] = "ui_invoke",
        ["code"] = "UI_INVOKE_OUTCOME_UNKNOWN",
        ["outcome"] = "unknown",
        ["message"] = "InvokePattern may have run. Its result was not confirmed; do not automatically retry. Inspect the application and debugger state.",
    };
}
