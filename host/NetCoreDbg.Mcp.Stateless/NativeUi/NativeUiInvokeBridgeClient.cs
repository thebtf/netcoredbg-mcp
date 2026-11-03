using System.Buffers.Binary;
using System.IO.Pipes;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;

namespace NetCoreDbg.Mcp.Stateless.NativeUi;

// Observation is one exchange; only SendAsync can issue the separately authorized invoke commit.
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
        var observed = NativeUiDiscoveryContract.Text(request, "operation") == "prepare_observed";
        if (Interlocked.Exchange(ref _started, 1) != 0)
            return NativeUiInvokeResult.Refused("UI_INVOKE_ALREADY_USED", observed);

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
            if (TryReadRefusal(ready, observed, out var refusal))
                return refusal;
            if (ready.Count != 1 || ReadString(ready, "status") != "ready")
                throw new InvalidDataException("The invoke helper did not prepare the requested element.");

            // Preparation cannot invoke. Authorize the commit only after the potentially slow UIA reads.
            deadline.Token.ThrowIfCancellationRequested();
            if (!authorizeDispatch())
                return NativeUiInvokeResult.Refused("UI_INVOKE_TARGET_CHANGED", observed);
            deadline.Token.ThrowIfCancellationRequested();
            dispatchStarted = true;
            await WriteRequestAsync(
                authorizationNonce, correlationId, new JsonObject { ["operation"] = "invoke" }, deadline.Token).ConfigureAwait(false);
            var response = await ReadResponseAsync(authorizationNonce, correlationId, deadline.Token).ConfigureAwait(false);
            if (TryReadRefusal(response, observed, out refusal))
                return refusal;
            if (response.Count == 1 && ReadString(response, "status") == "succeeded")
                return NativeUiInvokeResult.Success(ReadString(request, "automationId"), observed);
            return NativeUiInvokeResult.Unknown(observed);
        }
        catch (Exception)
        {
            // A partial commit write, cancellation, lost reply, or provider exception can hide a completed action.
            return dispatchStarted ? NativeUiInvokeResult.Unknown(observed) : NativeUiInvokeResult.Refused("UI_INVOKE_UNAVAILABLE", observed);
        }
        finally
        {
            ClosePipe();
        }
    }

    internal async Task<JsonObject> ObserveAsync(string authorizationNonce, JsonObject request, CancellationToken cancellationToken)
    {
        if (Interlocked.Exchange(ref _started, 1) != 0)
            return NativeUiDiscoveryResult.Error("UI_DISCOVERY_UNAVAILABLE");
        try
        {
            var correlationId = Convert.ToHexString(RandomNumberGenerator.GetBytes(16));
            await _pipe.ConnectAsync(cancellationToken).ConfigureAwait(false);
            await WriteRequestAsync(authorizationNonce, correlationId, request, cancellationToken).ConfigureAwait(false);
            var response = await ReadResponseAsync(authorizationNonce, correlationId, cancellationToken,
                NativeUiDiscoveryContract.MaximumHelperBytes).ConfigureAwait(false);
            if (response.Count == 2 && NativeUiDiscoveryContract.Text(response, "status") == "refused")
            {
                var code = NativeUiDiscoveryContract.Text(response, "code");
                return NativeUiDiscoveryResult.Error(code is "UI_DISCOVERY_TARGET_UNAVAILABLE" or "UI_DISCOVERY_TARGET_CHANGED" or
                    "UI_DISCOVERY_UNREADABLE" or "UI_DISCOVERY_LIMIT_EXCEEDED" ? code : "UI_DISCOVERY_UNREADABLE");
            }
            return NativeUiDiscoveryContract.Text(response, "kind") == "ui_window_tree"
                ? response : NativeUiDiscoveryResult.Error("UI_DISCOVERY_UNREADABLE");
        }
        catch (OperationCanceledException) { throw; }
        catch (NativeUiDiscoveryLimitException)
        {
            return NativeUiDiscoveryResult.Error("UI_DISCOVERY_LIMIT_EXCEEDED");
        }
        catch (InvalidDataException)
        {
            return NativeUiDiscoveryResult.Error("UI_DISCOVERY_UNREADABLE");
        }
        catch (JsonException)
        {
            return NativeUiDiscoveryResult.Error("UI_DISCOVERY_UNREADABLE");
        }
        catch (Exception)
        {
            return NativeUiDiscoveryResult.Error("UI_DISCOVERY_UNAVAILABLE");
        }
        finally { ClosePipe(); }
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

    internal static bool IsRequestWithinBounds(JsonObject request) =>
        JsonSerializer.SerializeToUtf8Bytes(new
        {
            nonce = string.Empty,
            correlationId = string.Empty,
            request,
        }).Length <= MaximumFrameBytes - 64 - 32; // Fixed hexadecimal nonce and correlation lengths.

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

    private async Task<JsonObject> ReadResponseAsync(string nonce, string correlationId, CancellationToken cancellationToken,
        int maximumBytes = MaximumFrameBytes)
    {
        var header = new byte[sizeof(int)];
        await _pipe.ReadExactlyAsync(header, cancellationToken).ConfigureAwait(false);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length > maximumBytes)
            throw new NativeUiDiscoveryLimitException();
        if (length < 1)
            throw new InvalidDataException("The helper response frame is invalid.");
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

    private static bool TryReadRefusal(JsonObject response, bool observed, out JsonObject refusal)
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
        refusal = NativeUiInvokeResult.Refused(code, observed);
        return true;
    }

    private static string ReadString(JsonObject source, string name) =>
        source[name] is JsonValue value && value.TryGetValue<string>(out var text)
            ? text : throw new InvalidDataException("The invoke response contains an invalid string.");
}

internal static class NativeUiInvokeResult
{
    internal static JsonObject Success(string automationId, bool observed = false) => observed ? new()
    {
        ["kind"] = "ui_invoke_observed_success",
        ["selection"] = "runtime_id",
        ["invoked"] = true,
        ["method"] = "InvokePattern",
        ["atomicity"] = "unproven",
    } : new()
    {
        ["kind"] = "ui_invoke_success",
        ["automationId"] = automationId,
        ["invoked"] = true,
        ["method"] = "InvokePattern",
    };

    internal static JsonObject Refused(string code, bool observed = false) => new()
    {
        ["kind"] = "tool_error",
        ["tool"] = observed ? "ui_invoke_observed" : "ui_invoke",
        ["code"] = code,
        ["outcome"] = "not_invoked",
        ["message"] = code switch
        {
            "DEBUG_SESSION_NOT_FOUND" => "The debug session is unavailable or retired.",
            "UI_INVOKE_TARGET_UNAVAILABLE" => "The bound debuggee process or main window cannot be established.",
            "UI_INVOKE_TARGET_CHANGED" => observed
                ? "The session, process, main window, root, runtime-ID-selected element, or its exact AutomationId changed before dispatch."
                : "The session, process, window, or selected element changed before dispatch.",
            "ELEMENT_NOT_FOUND" => observed
                ? "No element matches the observed runtime ID in the bound window."
                : "No element matches the exact AutomationId in the bound window.",
            "ELEMENT_AMBIGUOUS" => observed
                ? "More than one element matches the observed runtime ID in the bound window."
                : "More than one element matches the exact AutomationId in the bound window.",
            "UI_INVOKE_SELECTION_UNREADABLE" => observed
                ? "The complete bounded runtime-ID selection, process identity, or selected element's exact AutomationId could not be read."
                : "The complete bounded selector or element identity could not be read.",
            "UI_INVOKE_PATTERN_UNSUPPORTED" => "The selected element does not support InvokePattern; no input fallback is used.",
            "UI_INVOKE_BUSY" => "Another native UI operation is already active in this session.",
            "UI_INVOKE_ALREADY_USED" => "This one-shot invoke transport cannot be reused or retried.",
            "UI_INVOKE_CLEANUP_UNCONFIRMED" => "Owned helper cleanup was not confirmed; new UI helpers remain blocked until cleanup is resolved.",
            "INVALID_TOOL_ARGUMENTS" => "The invoke request is invalid.",
            _ => "The local invoke helper is unavailable before dispatch.",
        },
    };

    internal static JsonObject CleanupUnconfirmed(JsonObject previous, bool observed = false) => new()
    {
        ["kind"] = "tool_error",
        ["tool"] = observed ? "ui_invoke_observed" : "ui_invoke",
        ["code"] = "UI_INVOKE_CLEANUP_UNCONFIRMED",
        ["outcome"] = NativeUiDiscoveryContract.Text(previous, "kind") == (observed ? "ui_invoke_observed_success" : "ui_invoke_success")
            ? "invoked" : NativeUiDiscoveryContract.Text(previous, "outcome") ?? "unknown",
        ["message"] = "Owned helper cleanup was not confirmed. The reported action outcome is preserved; do not automatically retry. New UI helpers remain blocked until cleanup is resolved.",
    };

    internal static JsonObject Unknown(bool observed = false) => new()
    {
        ["kind"] = "tool_error",
        ["tool"] = observed ? "ui_invoke_observed" : "ui_invoke",
        ["code"] = "UI_INVOKE_OUTCOME_UNKNOWN",
        ["outcome"] = "unknown",
        ["message"] = observed
            ? "InvokePattern on the runtime-ID-selected element may have run. Its result was not confirmed; do not automatically retry. Inspect the application and debugger state."
            : "InvokePattern may have run. Its result was not confirmed; do not automatically retry. Inspect the application and debugger state.",
    };
}

internal sealed class NativeUiDiscoveryLimitException : IOException { }
