using System.Diagnostics;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
using Microsoft.Extensions.Hosting;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

internal sealed class NetCoreDbgSessionContractDriver : IAsyncDisposable
{
    private const string ProductionAssemblyName = "NetCoreDbg.Mcp.Stateless";
    private const string SessionTypeName = "NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession";
    private const string StateTypeName = "NetCoreDbg.Mcp.Stateless.DebugAdapter.DapSessionState";

    private readonly FixtureProcess _fixture;
    private readonly object _session;
    private readonly MethodInfo _stopAsync;
    private readonly MethodInfo _getCallStackAsync;
    private readonly SemaphoreSlim _writeGate;
    private readonly FieldInfo _pending;
    private readonly PropertyInfo _state;
    private readonly PropertyInfo _isUsable;
    private readonly Task _readerTask;
    private readonly MethodInfo _disposeAsync;
    private readonly DapWriteBarrierStream? _dapWriteBarrier;
    private readonly PreHeaderWriteGate? _preHeaderWriteGate;

    private sealed class InitializeResponseContinuationGate
    {
        public TaskCompletionSource<bool> Reached { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
        public TaskCompletionSource<bool> Release { get; } = new(TaskCreationOptions.RunContinuationsAsynchronously);
    }

    private NetCoreDbgSessionContractDriver(
        FixtureProcess fixture,
        object session,
        MethodInfo stopAsync,
        MethodInfo getCallStackAsync,
        SemaphoreSlim writeGate,
        FieldInfo pending,
        PropertyInfo state,
        PropertyInfo isUsable,
        Task readerTask,
        MethodInfo disposeAsync,
        DapWriteBarrierStream? dapWriteBarrier = null,
        PreHeaderWriteGate? preHeaderWriteGate = null)
    {
        _fixture = fixture;
        _session = session;
        _stopAsync = stopAsync;
        _getCallStackAsync = getCallStackAsync;
        _writeGate = writeGate;
        _pending = pending;
        _state = state;
        _isUsable = isUsable;
        _readerTask = readerTask;
        _disposeAsync = disposeAsync;
        _dapWriteBarrier = dapWriteBarrier;
        _preHeaderWriteGate = preHeaderWriteGate;
    }
    public FixtureProcess Fixture => _fixture;
    public int OwnedProcessId => (RequirePrivateProcessField(_session.GetType(), "_process").GetValue(_session)
        ?? throw new InvalidOperationException("NetCoreDbgSession._process returned null.")) is Process process
            ? process.Id
            : throw new InvalidOperationException("NetCoreDbgSession._process has an unexpected type.");

    public static void AssertUnixGuardianOwnershipContract()
    {
        var sessionType = RequireType(LoadProductionAssemblyOrAssert(), SessionTypeName);
        var ownershipType = sessionType.GetNestedType("UnixProcessGroupOwnership", BindingFlags.NonPublic);
        Assert.NotNull(ownershipType);
        var fields = ownershipType!.GetFields(BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        Assert.DoesNotContain(fields, static field => field.FieldType == typeof(int));
        Assert.Contains(fields, static field => field.FieldType == typeof(System.IO.Pipes.AnonymousPipeServerStream));
    }

    public DapSessionSnapshot State
    {
        get
        {
            var value = _state.GetValue(_session)
                ?? throw new InvalidOperationException("NetCoreDbgSession.State returned null.");
            var stateType = value.GetType();
            return new DapSessionSnapshot(
                (string?)RequireProperty(stateType, "Event", typeof(string)).GetValue(value),
                (string?)RequireProperty(stateType, "StopReason", typeof(string)).GetValue(value),
                (int?)RequireProperty(stateType, "ExitCode", typeof(int?)).GetValue(value));
        }
    }

    public bool IsUsable => (bool)(_isUsable.GetValue(_session)
        ?? throw new InvalidOperationException("NetCoreDbgSession.IsUsable returned null."));
    public bool StepOverDrainRequired => (int)(RequirePrivateField(_session.GetType(), "_stepOverDrainRequired", typeof(int)).GetValue(_session)
        ?? throw new InvalidOperationException("NetCoreDbgSession._stepOverDrainRequired returned null.")) != 0;
    public bool HasActiveStepOver => RequirePrivateField(_session.GetType(), "_activeStepOver").GetValue(_session) is not null;
    public int PendingRequestCount => Assert.IsAssignableFrom<System.Collections.IDictionary>(_pending.GetValue(_session)).Count;
    public int DapWriteCalls => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).WriteCalls;
    public bool IsExecutionClosing => string.Equals(
        RequirePrivateField(_session.GetType(), "_executionAdmission").GetValue(_session)?.ToString(),
        "Closing", StringComparison.Ordinal);

    public Task WaitForReaderCompletionAsync(CancellationToken cancellationToken) => _readerTask.WaitAsync(cancellationToken);

    public bool CapabilitiesObserved => (bool)(RequirePrivateProperty(_session.GetType(), "CapabilitiesObserved", typeof(bool)).GetValue(_session)
        ?? throw new InvalidOperationException("NetCoreDbgSession.CapabilitiesObserved returned null."));
    public async Task<bool> GetCallStackIsRefusedAsync(
        int threadId,
        uint startFrame,
        uint levels,
        CancellationToken cancellationToken)
    {
        var result = await AwaitAsyncResult(
            _getCallStackAsync.Invoke(_session, [threadId, startFrame, levels, cancellationToken]),
            "GetCallStackAsync",
            cancellationToken);
        Assert.NotNull(result);
        return Assert.IsType<bool>(RequireProperty(result!.GetType(), "IsRefused", typeof(bool)).GetValue(result));
    }

    public async Task<string> GetInspectionContextAsync(CancellationToken cancellationToken)
    {
        var method = RequireTaskMethod(_session.GetType(), "GetCallStackAsync",
            typeof(int), typeof(uint), typeof(uint), typeof(bool), typeof(CancellationToken));
        var result = await AwaitAsyncResult(method.Invoke(_session, [1, 0u, 20u, true, cancellationToken]),
            "GetCallStackAsync", cancellationToken);
        Assert.NotNull(result);
        Assert.False(Assert.IsType<bool>(result!.GetType().GetProperty("IsRefused")?.GetValue(result)));
        return Assert.IsType<string>(result.GetType().GetProperty("InspectionContextId")?.GetValue(result));
    }

    public async Task StepOverAsync(int threadId, CancellationToken cancellationToken)
    {
        var stepOverAsync = RequireTaskMethod(_session.GetType(), "StepOverAsync", typeof(int), typeof(CancellationToken));
        _ = await AwaitAsyncResult(
            stepOverAsync.Invoke(_session, [threadId, cancellationToken]),
            "StepOverAsync",
            cancellationToken);
    }
    public async Task ContinueAsync(int threadId, CancellationToken cancellationToken)
    {
        var method = RequireTaskMethod(_session.GetType(), "ContinueAsync", typeof(int), typeof(CancellationToken));
        _ = await AwaitAsyncResult(method.Invoke(_session, [threadId, cancellationToken]), "ContinueAsync", CancellationToken.None);
    }

    public async Task<bool> AddBreakpointIsAddedAsync(string file, int line, CancellationToken cancellationToken)
    {
        var add = RequireTaskMethod(_session.GetType(), "AddBreakpointAsync", typeof(string), typeof(int), typeof(CancellationToken));
        var result = await AwaitAsyncResult(add.Invoke(_session, [file, line, cancellationToken]), "AddBreakpointAsync", cancellationToken);
        Assert.NotNull(result);
        var outcome = result!.GetType().GetProperty("Outcome", BindingFlags.Instance | BindingFlags.Public);
        Assert.NotNull(outcome);
        return string.Equals(outcome!.GetValue(result)?.ToString(), "Added", StringComparison.Ordinal);
    }

    public async Task WaitForReaderBarrierAsync(CancellationToken cancellationToken)
    {
        var getThreads = RequireTaskMethod(_session.GetType(), "GetThreadsAsync", typeof(CancellationToken));
        _ = await AwaitAsyncResult(getThreads.Invoke(_session, [cancellationToken]), "GetThreadsAsync", cancellationToken);
    }

    public async Task<int[]> GetThreadIdsAsync(CancellationToken cancellationToken)
    {
        var method = RequireTaskMethod(_session.GetType(), "GetThreadsAsync", typeof(CancellationToken));
        var result = await AwaitAsyncResult(method.Invoke(_session, [cancellationToken]), "GetThreadsAsync", CancellationToken.None);
        Assert.NotNull(result);
        Assert.False(Assert.IsType<bool>(result!.GetType().GetProperty("IsRefused")?.GetValue(result)));
        var threads = Assert.IsAssignableFrom<System.Collections.IEnumerable>(result.GetType().GetProperty("Threads")?.GetValue(result));
        return threads.Cast<object>().Select(thread => Assert.IsType<int>(thread.GetType().GetProperty("Id")?.GetValue(thread))).ToArray();
    }

    public async Task WaitForObservedStopReasonAsync(string reason, CancellationToken cancellationToken)
    {
        while (true)
        {
            var observed = State;
            if (observed.Event == "stopped" && string.Equals(observed.StopReason, reason, StringComparison.Ordinal))
            {
                return;
            }
            await Task.Delay(TimeSpan.FromMilliseconds(10), cancellationToken);
        }
    }

    public Task<StepOverRegistryProbe> StepOverThroughRegistryAsync(
        int threadId,
        CancellationToken cancellationToken,
        bool inspectCancellation = false,
        Func<Task>? beforeCancellationInspection = null,
        Func<Func<string, CancellationToken, Task<ModelContextProtocol.Protocol.CallToolResult>>, Task<ModelContextProtocol.Protocol.CallToolResult>>? invokeSteps = null,
        Func<Task>? beforeStop = null,
        Func<Task>? beforeDispose = null,
        Action<string?, bool>? afterUsabilityObservation = null) =>
        InvokeThroughRegistryAsync(threadId, cancellationToken, inspectCancellation, beforeCancellationInspection,
            invokeSteps, beforeStop, beforeDispose, afterUsabilityObservation);

    public Task<StepOverRegistryProbe> ContinueThroughRegistryAsync(
        int threadId,
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null,
        Func<Func<string, CancellationToken, Task<ModelContextProtocol.Protocol.CallToolResult>>, Task<ModelContextProtocol.Protocol.CallToolResult>>? invokeContinue = null) =>
        InvokeThroughRegistryAsync(threadId, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, invokeSteps: invokeContinue, continueExecution: true);

    public Task<StepOverRegistryProbe> AddBreakpointThroughRegistryAsync(
        string file,
        int line,
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null) =>
        InvokeThroughRegistryAsync(0, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, breakpoint: (file, line));

    public Task<StepOverRegistryProbe> GetCallStackThroughRegistryAsync(
        int threadId,
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null) =>
        InvokeThroughRegistryAsync(threadId, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, inspectCallStack: true);

    public Task<StepOverRegistryProbe> PauseThroughRegistryAsync(
        int? threadId,
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null,
        Func<Func<string, CancellationToken, Task<ModelContextProtocol.Protocol.CallToolResult>>, Task<ModelContextProtocol.Protocol.CallToolResult>>? invokePause = null,
        Func<Task>? beforeStop = null,
        Func<Task>? beforeDispose = null) =>
        InvokeThroughRegistryAsync(threadId ?? 0, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, invokeSteps: invokePause,
            beforeStop: beforeStop, beforeDispose: beforeDispose,
            pauseExecution: true, omitThreadId: threadId is null);

    public Task<StepOverRegistryProbe> StepPairThroughRegistryAsync(
        string tool,
        int threadId,
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null) =>
        InvokeThroughRegistryAsync(threadId, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, stepTool: tool);

    public Task<StepOverRegistryProbe> EvaluateThroughRegistryAsync(
        string inspectionContextId,
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null,
        Func<Func<string, CancellationToken, Task<ModelContextProtocol.Protocol.CallToolResult>>, Task<ModelContextProtocol.Protocol.CallToolResult>>? invokeEvaluate = null) =>
        InvokeThroughRegistryAsync(0, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, invokeSteps: invokeEvaluate,
            evaluateContext: inspectionContextId);

    public Task<StepOverRegistryProbe> SetVariableThroughRegistryAsync(
        string inspectionContextId,
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null,
        Func<Func<string, CancellationToken, Task<ModelContextProtocol.Protocol.CallToolResult>>, Task<ModelContextProtocol.Protocol.CallToolResult>>? invokeSetVariable = null,
        string variableName = "x") =>
        InvokeThroughRegistryAsync(0, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, invokeSteps: invokeSetVariable,
            setVariableContext: inspectionContextId, setVariableName: variableName);

    public Task<StepOverRegistryProbe> ExceptionInfoThroughRegistryAsync(
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null,
        Func<Func<string, CancellationToken, Task<ModelContextProtocol.Protocol.CallToolResult>>, Task<ModelContextProtocol.Protocol.CallToolResult>>? invokeInspection = null) =>
        InvokeThroughRegistryAsync(1, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, invokeSteps: invokeInspection,
            inspectExceptionInfo: true);

    public Task<StepOverRegistryProbe> ConfigureExceptionsThroughRegistryAsync(
        string[] filters,
        CancellationToken cancellationToken,
        Func<Task>? beforeCancellationInspection = null,
        Func<Func<string, CancellationToken, Task<ModelContextProtocol.Protocol.CallToolResult>>, Task<ModelContextProtocol.Protocol.CallToolResult>>? invokeConfiguration = null) =>
        InvokeThroughRegistryAsync(0, cancellationToken, inspectCancellation: true,
            beforeCancellationInspection: beforeCancellationInspection, invokeSteps: invokeConfiguration,
            exceptionFilters: filters);

    private async Task<StepOverRegistryProbe> InvokeThroughRegistryAsync(
        int threadId,
        CancellationToken cancellationToken,
        bool inspectCancellation = false,
        Func<Task>? beforeCancellationInspection = null,
        Func<Func<string, CancellationToken, Task<ModelContextProtocol.Protocol.CallToolResult>>, Task<ModelContextProtocol.Protocol.CallToolResult>>? invokeSteps = null,
        Func<Task>? beforeStop = null,
        Func<Task>? beforeDispose = null,
        Action<string?, bool>? afterUsabilityObservation = null,
        bool inspectCallStack = false,
        bool continueExecution = false,
        (string File, int Line)? breakpoint = null,
        bool pauseExecution = false,
        bool omitThreadId = false,
        string? stepTool = null,
        string? evaluateContext = null,
        bool inspectExceptionInfo = false,
        string[]? exceptionFilters = null,
        string? setVariableContext = null,
        string setVariableName = "x")
    {
        const string sessionId = "step-over-clock-session";
        var registryType = RequireType(_session.GetType().Assembly, "NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry");
        var invokingRequestId = new AsyncLocal<string?>();
        var evaluatorType = typeof(Func<,>).MakeGenericType(_session.GetType(), typeof(bool));
        var constructor = registryType.GetConstructor(BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null, types: afterUsabilityObservation is null ? [typeof(string)] : [typeof(string), evaluatorType], modifiers: null);
        Assert.NotNull(constructor);
        Func<object, bool> observeUsability = target =>
        {
            var usable = Assert.IsType<bool>(_isUsable.GetValue(target));
            afterUsabilityObservation?.Invoke(invokingRequestId.Value, usable);
            return usable;
        };
        var registry = constructor!.Invoke(afterUsabilityObservation is null ? [null] : [null, observeUsability]);
        try
        {
            var sessions = RequirePrivateField(registryType, "_sessions").GetValue(registry)!;
            var slots = RequirePrivateField(registryType, "_slots").GetValue(registry)!;
            var slotType = Assert.IsAssignableFrom<Type>(registryType.GetNestedType("SessionSlot", BindingFlags.NonPublic));
            var removeSession = sessions.GetType().GetMethod("TryRemove", [typeof(string), _session.GetType().MakeByRefType()]);
            var removeSlot = slots.GetType().GetMethod("TryRemove", [typeof(string), slotType.MakeByRefType()]);
            Assert.NotNull(removeSession);
            Assert.NotNull(removeSlot);
            object? slot = null;
            bool? leaseReleasedBeforeRemoval = null;
            Action remove = () =>
            {
                leaseReleasedBeforeRemoval = Assert.IsType<int>(RequirePrivateField(slotType, "_leases", typeof(int)).GetValue(slot)) == 0;
                _ = removeSlot!.Invoke(slots, [sessionId, null]);
                _ = removeSession!.Invoke(sessions, [sessionId, null]);
            };
            var slotConstructor = slotType.GetConstructor(BindingFlags.Instance | BindingFlags.NonPublic,
                binder: null, types: [typeof(TimeSpan), typeof(Func<CancellationToken, Task>), typeof(Func<ValueTask>), typeof(Action)], modifiers: null);
            Assert.NotNull(slotConstructor);
            slot = slotConstructor!.Invoke([
                TimeSpan.FromSeconds(2),
                (Func<CancellationToken, Task>)(async token =>
                {
                    if (beforeStop is not null)
                    {
                        await beforeStop();
                    }
                    await StopAsync(token);
                }),
                (Func<ValueTask>)(async () =>
                {
                    if (beforeDispose is not null)
                    {
                        await beforeDispose();
                    }
                    await DisposeSessionAsync();
                }),
                remove,
            ]);
            var addSession = sessions.GetType().GetMethod("TryAdd", [typeof(string), _session.GetType()]);
            var addSlot = slots.GetType().GetMethod("TryAdd", [typeof(string), slotType]);
            Assert.NotNull(addSession);
            Assert.NotNull(addSlot);
            Assert.True((bool)addSession!.Invoke(sessions, [sessionId, _session])!);
            Assert.True((bool)addSlot!.Invoke(slots, [sessionId, slot])!);

            var stepMethod = stepTool switch
            {
                null => "StepOverAsync",
                "step_into" => "StepIntoAsync",
                "step_out" => "StepOutAsync",
                _ => throw new ArgumentOutOfRangeException(nameof(stepTool)),
            };
            var operationMethod = inspectCallStack || evaluateContext is not null || inspectExceptionInfo || setVariableContext is not null
                ? registryType.GetMethod(setVariableContext is not null ? "SetVariableAsync" : inspectExceptionInfo ? "GetExceptionInfoAsync" : evaluateContext is null ? "GetCallStackAsync" : "EvaluateExpressionAsync", BindingFlags.Instance | BindingFlags.NonPublic,
                    binder: null, types: [typeof(ModelContextProtocol.Protocol.CallToolRequestParams), typeof(CancellationToken)], modifiers: null)
                : registryType.GetMethod(exceptionFilters is not null ? "ConfigureExceptionsAsync" : pauseExecution ? "PauseAsync" : breakpoint.HasValue ? "AddBreakpointAsync" : continueExecution ? "ContinueAsync" : stepMethod, BindingFlags.Instance | BindingFlags.NonPublic,
                    binder: null, types: [typeof(ModelContextProtocol.Protocol.CallToolRequestParams), typeof(ModelContextProtocol.Protocol.RequestId), typeof(CancellationToken)], modifiers: null);
            Assert.NotNull(operationMethod);
            var request = new ModelContextProtocol.Protocol.CallToolRequestParams
            {
                Name = setVariableContext is not null ? "set_variable" : exceptionFilters is not null ? "configure_exceptions" : pauseExecution ? "pause_execution" : inspectCallStack ? "get_call_stack" : inspectExceptionInfo ? "get_exception_info" : evaluateContext is not null ? "evaluate_expression" : breakpoint.HasValue ? "add_breakpoint" : continueExecution ? "continue_execution" : stepTool ?? "step_over",
                Arguments = new Dictionary<string, JsonElement>
                {
                    ["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId),
                    ["threadId"] = JsonSerializer.SerializeToElement(threadId),
                },
            };
            if (omitThreadId)
            {
                request.Arguments.Remove("threadId");
            }
            if (inspectCallStack)
            {
                request.Arguments["includeInspectionContext"] = JsonSerializer.SerializeToElement(true);
            }
            if (evaluateContext is not null)
            {
                request.Arguments.Remove("threadId");
                request.Arguments["inspectionContextId"] = JsonSerializer.SerializeToElement(evaluateContext);
                request.Arguments["frameId"] = JsonSerializer.SerializeToElement(1);
                request.Arguments["expression"] = JsonSerializer.SerializeToElement("6 * 7");
            }
            if (setVariableContext is not null)
            {
                request.Arguments.Remove("threadId");
                request.Arguments["inspectionContextId"] = JsonSerializer.SerializeToElement(setVariableContext);
                request.Arguments["variablesReference"] = JsonSerializer.SerializeToElement(1001);
                request.Arguments["name"] = JsonSerializer.SerializeToElement(setVariableName);
                request.Arguments["value"] = JsonSerializer.SerializeToElement("1");
            }
            if (breakpoint is { } targetBreakpoint)
            {
                request.Arguments.Remove("threadId");
                request.Arguments["file"] = JsonSerializer.SerializeToElement(targetBreakpoint.File);
                request.Arguments["line"] = JsonSerializer.SerializeToElement(targetBreakpoint.Line);
            }
            if (exceptionFilters is not null)
            {
                request.Arguments.Remove("threadId");
                request.Arguments["filters"] = JsonSerializer.SerializeToElement(exceptionFilters);
            }
            Task<ModelContextProtocol.Protocol.CallToolResult> InvokeStep(string requestId, CancellationToken token)
            {
                var previousRequestId = invokingRequestId.Value;
                invokingRequestId.Value = requestId;
                try
                {
                    object?[] invocationArguments = inspectCallStack || evaluateContext is not null || inspectExceptionInfo || setVariableContext is not null
                        ? [request, token]
                        : [request, new ModelContextProtocol.Protocol.RequestId(requestId), token];
                    var invocation = operationMethod!.Invoke(registry, invocationArguments);
                    return Assert.IsType<ValueTask<ModelContextProtocol.Protocol.CallToolResult>>(invocation).AsTask();
                }
                finally
                {
                    invokingRequestId.Value = previousRequestId;
                }
            }
            ModelContextProtocol.Protocol.CallToolResult result;
            var cancellationObserved = false;
            ModelContextProtocol.Protocol.CallToolResult? retainedThreadsResult = null;
            try
            {
                result = await (invokeSteps is null
                    ? InvokeStep("step-over-clock", cancellationToken)
                    : invokeSteps(InvokeStep));
            }
            catch (OperationCanceledException) when (inspectCancellation && cancellationToken.IsCancellationRequested)
            {
                cancellationObserved = true;
                if (beforeCancellationInspection is not null)
                {
                    await beforeCancellationInspection();
                }
                var getState = registryType.GetMethod("GetStateAsync", BindingFlags.Instance | BindingFlags.NonPublic,
                    binder: null, types: [typeof(ModelContextProtocol.Protocol.CallToolRequestParams), typeof(CancellationToken)], modifiers: null);
                Assert.NotNull(getState);
                var stateRequest = new ModelContextProtocol.Protocol.CallToolRequestParams
                {
                    Name = "get_debug_state",
                    Arguments = new Dictionary<string, JsonElement>
                    {
                        ["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId),
                    },
                };
                result = Assert.IsType<ModelContextProtocol.Protocol.CallToolResult>(await AwaitAsyncResult(
                    getState!.Invoke(registry, [stateRequest, CancellationToken.None]), "GetStateAsync", CancellationToken.None));
                if (result.StructuredContent is JsonElement stateContent
                    && stateContent.GetProperty("kind").GetString() == "debug_state_success")
                {
                    Assert.Same(_session, sessions.GetType().GetProperty("Item")!.GetValue(sessions, [sessionId]));
                    Assert.Same(slot, slots.GetType().GetProperty("Item")!.GetValue(slots, [sessionId]));
                    var getThreads = registryType.GetMethod("GetThreadsAsync", BindingFlags.Instance | BindingFlags.NonPublic,
                        binder: null, types: [typeof(ModelContextProtocol.Protocol.CallToolRequestParams), typeof(CancellationToken)], modifiers: null);
                    Assert.NotNull(getThreads);
                    using var inspection = new CancellationTokenSource(TimeSpan.FromSeconds(5));
                    var threadsRequest = new ModelContextProtocol.Protocol.CallToolRequestParams
                    {
                        Name = "get_threads",
                        Arguments = stateRequest.Arguments,
                    };
                    retainedThreadsResult = Assert.IsType<ModelContextProtocol.Protocol.CallToolResult>(await AwaitAsyncResult(
                        getThreads!.Invoke(registry, [threadsRequest, inspection.Token]), "GetThreadsAsync", inspection.Token));
                }
            }
            var containsSession = sessions.GetType().GetMethod("ContainsKey", [typeof(string)]);
            var containsSlot = slots.GetType().GetMethod("ContainsKey", [typeof(string)]);
            Assert.NotNull(containsSession);
            Assert.NotNull(containsSlot);
            return new StepOverRegistryProbe(result,
                (bool)containsSession!.Invoke(sessions, [sessionId])!,
                (bool)containsSlot!.Invoke(slots, [sessionId])!,
                !HasExited(await _fixture.GetProcessIdAsync(CancellationToken.None)),
                cancellationObserved,
                retainedThreadsResult,
                leaseReleasedBeforeRemoval,
                Assert.IsAssignableFrom<System.Collections.IDictionary>(RequirePrivateField(registryType, "_stepOverRequests").GetValue(registry)).Count);
        }
        finally
        {
            _ = await AwaitAsyncResult(RequireAsyncDisposable(registryType).Invoke(registry, []), "DisposeAsync", CancellationToken.None);
        }
    }

    public async Task WaitForPendingCommandAsync(string command, CancellationToken cancellationToken)
    {
        while (!PendingRequests().Any(request =>
            string.Equals(request.GetType().GetProperty("Command")?.GetValue(request) as string, command, StringComparison.Ordinal)))
        {
            await Task.Delay(TimeSpan.FromMilliseconds(10), cancellationToken);
        }
    }

    public async Task WaitForPendingResponseAsync(string command, CancellationToken cancellationToken)
    {
        await WaitForPendingCommandAsync(command, cancellationToken);
        var request = Assert.Single(PendingRequests(), entry =>
            string.Equals(entry.GetType().GetProperty("Command")?.GetValue(entry) as string, command, StringComparison.Ordinal));
        var completion = request.GetType().GetProperty("Completion")?.GetValue(request);
        Assert.NotNull(completion);
        var response = Assert.IsAssignableFrom<Task>(completion!.GetType().GetProperty("Task")?.GetValue(completion));
        await response.WaitAsync(cancellationToken);
    }

    public Task GetPendingResponseTask(string command)
    {
        var request = Assert.Single(PendingRequests(), entry =>
            string.Equals(entry.GetType().GetProperty("Command")?.GetValue(entry) as string, command, StringComparison.Ordinal));
        var completion = request.GetType().GetProperty("Completion")?.GetValue(request);
        Assert.NotNull(completion);
        return Assert.IsAssignableFrom<Task>(completion!.GetType().GetProperty("Task")?.GetValue(completion));
    }

    public void FailPendingAsReaderFailure(IOException failure)
    {
        var sessionType = _session.GetType();
        RequirePrivateField(sessionType, "_readerFailure").SetValue(_session, failure);
        var failPending = sessionType.GetMethod("FailPending", BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null, types: [typeof(Exception)], modifiers: null);
        Assert.NotNull(failPending);
        _ = failPending!.Invoke(_session, [failure]);
    }
    public void SimulateReaderFailureMissingPendingRequest(IOException failure) =>
        RequirePrivateField(_session.GetType(), "_readerFailure").SetValue(_session, failure);


    public Task DispatchStaleSetVariableResponseAsync(Action afterDispatch, CancellationToken cancellationToken)
    {
        var request = Assert.Single(PendingRequests(), entry =>
            string.Equals(entry.GetType().GetProperty("Command")?.GetValue(entry) as string, "setVariable", StringComparison.Ordinal));
        var sequence = Assert.IsType<int>(request.GetType().GetProperty("Sequence")?.GetValue(request));
        var invalidated = JsonSerializer.SerializeToElement(new
        {
            seq = 1_000_001,
            type = "event",
            @event = "invalidated",
            body = new { areas = new[] { "variables" } },
        });
        var response = JsonSerializer.SerializeToElement(new
        {
            seq = 1_000_002,
            type = "response",
            request_seq = sequence,
            success = true,
            command = "setVariable",
            body = new { value = "updated", variablesReference = 1002 },
        });
        return DispatchMessagesUnderStateGateAsync([invalidated, response], releaseWriter: false, cancellationToken, afterDispatch);
    }

    public bool CanCaptureInspectionTarget()
    {
        var method = _session.GetType().GetMethod("CaptureCallStackTarget", BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null, types: [typeof(int), typeof(bool)], modifiers: null);
        Assert.NotNull(method);
        return method!.Invoke(_session, [1, true]) is not null;
    }

    private IEnumerable<object> PendingRequests()
    {
        var pending = _pending.GetValue(_session)
            ?? throw new InvalidOperationException("NetCoreDbgSession._pending returned null.");
        var values = Assert.IsAssignableFrom<System.Collections.IEnumerable>(_pending.FieldType.GetProperty("Values")?.GetValue(pending));
        return values.Cast<object>();
    }

    public Task DispatchPauseAcknowledgementThenCancelAsync(int requestSequence, Action cancel, CancellationToken cancellationToken)
    {
        var acknowledgement = JsonSerializer.SerializeToElement(new
        {
            seq = 1_000_001,
            type = "response",
            request_seq = requestSequence,
            success = true,
            command = "pause",
        });
        return DispatchMessagesUnderStateGateAsync([acknowledgement], releaseWriter: false, cancellationToken, afterDispatch: cancel);
    }

    public async Task WaitForPendingNextAsync(CancellationToken cancellationToken)
    {
        while (true)
        {
            var pending = _pending.GetValue(_session)
                ?? throw new InvalidOperationException("NetCoreDbgSession._pending returned null.");
            var values = _pending.FieldType.GetProperty("Values")?.GetValue(pending) as System.Collections.IEnumerable
                ?? throw new InvalidOperationException("NetCoreDbgSession._pending.Values returned null.");
            if (values.Cast<object>().Any(request =>
                    string.Equals(
                        RequireProperty(request.GetType(), "Command", typeof(string)).GetValue(request) as string,
                        "next",
                        StringComparison.Ordinal)))
            {
                return;
            }

            await Task.Delay(TimeSpan.FromMilliseconds(10), cancellationToken);
        }
    }

    public Task DispatchContinueStopThenContinuedAsync(int requestSequence, bool stoppedBeforeAcknowledgement, CancellationToken cancellationToken)
    {
        var acknowledgement = JsonSerializer.SerializeToElement(new
        {
            seq = stoppedBeforeAcknowledgement ? 1_000_002 : 1_000_001,
            type = "response",
            request_seq = requestSequence,
            success = true,
            command = "continue",
            body = new { allThreadsContinued = true },
        });
        var stopped = JsonSerializer.SerializeToElement(new
        {
            seq = stoppedBeforeAcknowledgement ? 1_000_001 : 1_000_002,
            type = "event",
            @event = "stopped",
            body = new { reason = "obsolete-breakpoint", threadId = 2, allThreadsStopped = false },
        });
        var continued = JsonSerializer.SerializeToElement(new
        {
            seq = 1_000_003,
            type = "event",
            @event = "continued",
            body = new { threadId = 2, allThreadsContinued = true },
        });
        return DispatchMessagesUnderStateGateAsync(
            [stoppedBeforeAcknowledgement ? stopped : acknowledgement,
                stoppedBeforeAcknowledgement ? acknowledgement : stopped, continued],
            releaseWriter: false, cancellationToken);
    }

    public Task DispatchContinueResponseFaultAndTerminalAsync(int requestSequence, bool terminalFirst, bool invalidSuccess, CancellationToken cancellationToken)
    {
        var fault = JsonSerializer.SerializeToElement(new
        {
            seq = terminalFirst ? 1_000_002 : 1_000_001,
            type = "response",
            request_seq = requestSequence,
            success = invalidSuccess ? (object)"invalid" : true,
            command = invalidSuccess ? "continue" : "next",
            body = new { message = "private controlled continue protocol detail" },
        });
        var terminal = JsonSerializer.SerializeToElement(new
        {
            seq = terminalFirst ? 1_000_001 : 1_000_002,
            type = "event",
            @event = "terminated",
        });
        return DispatchMessagesUnderStateGateAsync(
            terminalFirst ? [terminal, fault] : [fault, terminal], releaseWriter: true, cancellationToken);
    }

    public Task DispatchBreakpointAcknowledgementThenCancelAsync(int requestSequence, int actualLine, Action cancel, CancellationToken cancellationToken)
    {
        var acknowledgement = JsonSerializer.SerializeToElement(new
        {
            seq = 1_000_001,
            type = "response",
            request_seq = requestSequence,
            success = true,
            command = "setBreakpoints",
            body = new { breakpoints = new[] { new { verified = true, line = actualLine } } },
        });
        return DispatchMessagesUnderStateGateAsync([acknowledgement], releaseWriter: false, cancellationToken, afterDispatch: cancel);
    }

    public Task DispatchExceptionFiltersAcknowledgementThenCancelAsync(int requestSequence, Action cancel, CancellationToken cancellationToken)
    {
        var acknowledgement = JsonSerializer.SerializeToElement(new
        {
            seq = 1_000_001,
            type = "response",
            request_seq = requestSequence,
            success = true,
            command = "setExceptionBreakpoints",
            body = new { breakpoints = new[] { new { verified = true } } },
        });
        return DispatchMessagesUnderStateGateAsync([acknowledgement], releaseWriter: false, cancellationToken, afterDispatch: cancel);
    }

    private Task DispatchMessagesUnderStateGateAsync(JsonElement[] messages, bool releaseWriter, CancellationToken cancellationToken, Action? afterDispatch = null)
    {
        var stateGate = RequirePrivateField(_session.GetType(), "_stateGate").GetValue(_session)
            ?? throw new InvalidOperationException("NetCoreDbgSession._stateGate returned null.");
        var dispatch = _session.GetType().GetMethod("DispatchMessage", BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null, types: [typeof(JsonElement)], modifiers: null);
        Assert.NotNull(dispatch);
        return Task.Factory.StartNew(() =>
        {
            if (!Monitor.TryEnter(stateGate, TimeSpan.FromSeconds(2)))
            {
                throw new TimeoutException("The regression could not acquire NetCoreDbgSession._stateGate.");
            }
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (releaseWriter)
                {
                    // Commit and admission release do not require this monitor; the first outcome check does.
                    ReleaseDapWriteBarrier();
                }
                foreach (var message in messages)
                {
                    _ = dispatch!.Invoke(_session, [message]);
                }
                afterDispatch?.Invoke();
            }
            finally
            {
                Monitor.Exit(stateGate);
            }
        }, CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
    }

    public Task HoldStateGateAsync(TaskCompletionSource<bool> entered, ManualResetEventSlim release, CancellationToken cancellationToken)
    {
        var stateGate = RequirePrivateField(_session.GetType(), "_stateGate").GetValue(_session)
            ?? throw new InvalidOperationException("NetCoreDbgSession._stateGate returned null.");
        return Task.Factory.StartNew(() =>
        {
            if (!Monitor.TryEnter(stateGate, TimeSpan.FromSeconds(2)))
            {
                throw new TimeoutException("The regression could not acquire NetCoreDbgSession._stateGate.");
            }
            try
            {
                entered.TrySetResult(true);
                release.Wait(cancellationToken);
            }
            finally
            {
                Monitor.Exit(stateGate);
            }
        }, CancellationToken.None, TaskCreationOptions.LongRunning, TaskScheduler.Default);
    }
    public Task HoldDapWriteAsync(CancellationToken cancellationToken) => _writeGate.WaitAsync(cancellationToken);

    public void ReleaseDapWrite() => _writeGate.Release();

    public void ArmDapWriteBeforeHeaderBarrier() => (_preHeaderWriteGate
        ?? throw new InvalidOperationException("This session has no deterministic pre-header write gate.")).Arm();

    public Task WaitForDapPreHeaderWriteAsync(CancellationToken cancellationToken) => (_preHeaderWriteGate
        ?? throw new InvalidOperationException("This session has no deterministic pre-header write gate.")).WaitAsync(cancellationToken);

    public void ArmDapWriteAfterHeaderBarrier() => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).ArmAfterHeader();

    public Task WaitForDapHeaderWriteAsync(CancellationToken cancellationToken) => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).WaitForHeaderWriteAsync(cancellationToken);

    public void ArmDapWriteAfterFlushBarrier() => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).ArmAfterFlush();

    public Task WaitForDapFlushAsync(CancellationToken cancellationToken) => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).WaitForFlushAsync(cancellationToken);

    public void ArmDapWriteAtHeaderEntryBarrier(int prefixBytes = 0, bool opaqueCancellation = false, Action? cancelOnEntry = null) => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).ArmBeforeUnderlyingHeader(prefixBytes, opaqueCancellation, cancelOnEntry);

    public Task WaitForDapHeaderEntryAsync(CancellationToken cancellationToken) => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).WaitForHeaderEntryAsync(cancellationToken);

    public int ArmedHeaderUnderlyingWriteCalls => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).UnderlyingHeaderWriteCalls;

    public long ArmedHeaderBytesForwarded => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).HeaderBytesForwarded;

    public string DapUnderlyingInputStreamType => (_dapWriteBarrier
        ?? throw new InvalidOperationException("This session has no deterministic DAP write barrier.")).UnderlyingStreamType;

    public void ReleaseDapWriteBarrier()
    {
        _preHeaderWriteGate?.Release();
        _dapWriteBarrier?.Release();
    }

    public async Task WaitForPendingStackTraceAsync(CancellationToken cancellationToken)
    {
        while (true)
        {
            var pending = _pending.GetValue(_session)
                ?? throw new InvalidOperationException("NetCoreDbgSession._pending returned null.");
            var values = _pending.FieldType.GetProperty("Values")?.GetValue(pending) as System.Collections.IEnumerable
                ?? throw new InvalidOperationException("NetCoreDbgSession._pending.Values returned null.");
            if (values.Cast<object>().Any(request =>
                    string.Equals(
                        RequireProperty(request.GetType(), "Command", typeof(string)).GetValue(request) as string,
                        "stackTrace",
                        StringComparison.Ordinal)))
            {
                return;
            }

            await Task.Delay(TimeSpan.FromMilliseconds(10), cancellationToken);
        }
    }


    public static async Task<NetCoreDbgSessionContractDriver> StartAsync(
        FixtureConfiguration configuration,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        CancellationToken cancellationToken,
        ICollection<FixtureTranscriptEntry>? failedTranscript = null)
    {
        var fixture = FixtureProcess.Create(configuration);
        object? session = null;
        try
        {
            var assembly = LoadProductionAssemblyOrAssert();
            var sessionType = RequireType(assembly, SessionTypeName);
            var stateType = RequireType(assembly, StateTypeName);
            var breakpointType = RequireType(assembly, "NetCoreDbg.Mcp.Stateless.DebugAdapter.DapSourceBreakpoint");
            var startAsync = sessionType.GetMethod("StartAsync", BindingFlags.Static | BindingFlags.NonPublic,
                binder: null, types: [typeof(string), typeof(string), typeof(TimeSpan), typeof(TimeSpan), typeof(TimeSpan),
                    typeof(IReadOnlyDictionary<string, string>), typeof(IReadOnlyList<>).MakeGenericType(breakpointType),
                    typeof(Action), typeof(CancellationToken)], modifiers: null);
            Assert.NotNull(startAsync);
            var state = RequireProperty(sessionType, "State", stateType);
            var isUsable = RequireInternalProperty(sessionType, "IsUsable", typeof(bool));
            var readerTask = RequirePrivateTaskField(sessionType, "_readerTask");
            var getCallStackAsync = RequireTaskMethod(sessionType, "GetCallStackAsync", typeof(int), typeof(uint), typeof(uint), typeof(CancellationToken));
            var writeGate = RequirePrivateField(sessionType, "_writeGate", typeof(SemaphoreSlim));
            var pending = RequirePrivateField(sessionType, "_pending");
            var stopAsync = RequireTaskMethod(sessionType, "StopAsync", typeof(CancellationToken));
            var disposeAsync = RequireAsyncDisposable(sessionType);
            RequireNarrowSessionSurface(sessionType);
            RequireStateShape(stateType);
            fixture.MarkAdapterStartAttempted();

            using var startupCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            startupCancellation.CancelAfter(initializeTimeout + requestTimeout + TimeSpan.FromMilliseconds(500));
            var started = startAsync!.Invoke(null, [
                fixture.ExecutablePath,
                programPath,
                initializeTimeout,
                requestTimeout,
                stopTimeout,
                null,
                Array.CreateInstance(breakpointType, 0),
                (Action)fixture.MarkCleanupConfirmed,
                startupCancellation.Token,
            ]);
            session = await AwaitAsyncResult(started, "StartAsync", startupCancellation.Token);
            Assert.NotNull(session);
            var activeReaderTask = readerTask.GetValue(session) as Task
                ?? throw new InvalidOperationException("NetCoreDbgSession._readerTask returned null.");
            var activeWriteGate = Assert.IsType<SemaphoreSlim>(writeGate.GetValue(session));
            await fixture.WaitForStartupAsync(startupCancellation.Token);
            return new NetCoreDbgSessionContractDriver(fixture, session, stopAsync, getCallStackAsync, activeWriteGate, pending, state, isUsable, activeReaderTask, disposeAsync);
        }
        catch (Exception failure)
        {
            await CaptureFailedTranscriptAsync(fixture, failedTranscript, failure);
            await DisposeFailedSessionAsync(fixture, session, failure);
            await fixture.DisposePreservingFailureAsync(failure);
            throw;
        }
    }

    public static async Task<NetCoreDbgSessionContractDriver> StartWithInitializeContinuationGateAsync(
        FixtureConfiguration configuration,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        CancellationToken cancellationToken,
        ICollection<FixtureTranscriptEntry>? failedTranscript = null)
    {
        if (configuration.EnableConfigurationDoneAfterInitialization)
        {
            Assert.True(configuration.HoldConfigurationDoneCapabilityDeltaUntilRelease);
        }

        var fixture = FixtureProcess.Create(configuration);
        object? session = null;
        var gate = new InitializeResponseContinuationGate();
        try
        {
            var assembly = LoadProductionAssemblyOrAssert();
            var sessionType = RequireType(assembly, SessionTypeName);
            var stateType = RequireType(assembly, StateTypeName);
            var constructor = sessionType.GetConstructors(BindingFlags.Instance | BindingFlags.NonPublic)
                .SingleOrDefault(candidate =>
                {
                    var parameters = candidate.GetParameters();
                    return parameters.Length >= 9
                        && parameters.Skip(9).All(static parameter => parameter.IsOptional)
                        && parameters[0].ParameterType == typeof(Process)
                        && parameters[1].ParameterType == typeof(Stream)
                        && parameters[2].ParameterType == typeof(Stream)
                        && parameters[3].ParameterType == typeof(Stream)
                        && parameters[4].ParameterType == typeof(TimeSpan)
                        && parameters[5].ParameterType == typeof(TimeSpan)
                        && parameters[7].ParameterType == typeof(TaskCompletionSource<bool>)
                        && parameters[8].ParameterType == typeof(Task);
                });
            Assert.NotNull(constructor);
            var startProtocolAsync = RequireTaskMethod(sessionType, "StartProtocolAsync", typeof(string), typeof(TimeSpan), typeof(CancellationToken));
            var state = RequireProperty(sessionType, "State", stateType);
            var isUsable = RequireInternalProperty(sessionType, "IsUsable", typeof(bool));
            var readerTask = RequirePrivateTaskField(sessionType, "_readerTask");
            var getCallStackAsync = RequireTaskMethod(sessionType, "GetCallStackAsync", typeof(int), typeof(uint), typeof(uint), typeof(CancellationToken));
            var writeGate = RequirePrivateField(sessionType, "_writeGate", typeof(SemaphoreSlim));
            var pending = RequirePrivateField(sessionType, "_pending");
            var supportsConfigurationDone = RequirePrivateField(sessionType, "_supportsConfigurationDone", typeof(bool));
            var stopAsync = RequireTaskMethod(sessionType, "StopAsync", typeof(CancellationToken));
            var disposeAsync = RequireAsyncDisposable(sessionType);
            RequireNarrowSessionSurface(sessionType);
            RequireStateShape(stateType);
            fixture.MarkAdapterStartAttempted();

            using var startupCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            startupCancellation.CancelAfter(initializeTimeout + requestTimeout + TimeSpan.FromMilliseconds(500));
            var startInfo = new ProcessStartInfo(fixture.ExecutablePath)
            {
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            startInfo.ArgumentList.Add("--interpreter=vscode");
            var process = Process.Start(startInfo) ?? throw new InvalidOperationException("Could not start the controlled adapter.");
            await fixture.WaitForStartupAsync(startupCancellation.Token);
            session = constructor!.Invoke(new object?[]
            {
                process,
                process.StandardInput.BaseStream,
                process.StandardOutput.BaseStream,
                process.StandardError.BaseStream,
                requestTimeout,
                stopTimeout,
                null,
                gate.Reached,
                gate.Release.Task,
            }.Concat(constructor.GetParameters().Skip(9).Select(static parameter => parameter.DefaultValue)).ToArray());
            var started = startProtocolAsync.Invoke(session, [programPath, initializeTimeout, startupCancellation.Token]);
            var protocol = AwaitAsyncResult(started, "StartProtocolAsync", startupCancellation.Token);
            if (ReferenceEquals(await Task.WhenAny(gate.Reached.Task, protocol).WaitAsync(startupCancellation.Token), protocol))
            {
                _ = await protocol;
                throw new InvalidOperationException("The controlled initialize protocol completed without reaching its response continuation gate.");
            }

            await fixture.WaitForTranscriptKindAsync("initialize-response", startupCancellation.Token);
            if (configuration.EnableConfigurationDoneAfterInitialization)
            {
                fixture.ReleaseConfigurationDoneCapabilityDelta();
                while (!(bool)(supportsConfigurationDone.GetValue(session) ?? false))
                {
                    await Task.Delay(TimeSpan.FromMilliseconds(10), startupCancellation.Token);
                }
            }

            gate.Release.TrySetResult(true);
            _ = await protocol;
            var activeReaderTask = readerTask.GetValue(session) as Task
                ?? throw new InvalidOperationException("NetCoreDbgSession._readerTask returned null.");
            var activeWriteGate = Assert.IsType<SemaphoreSlim>(writeGate.GetValue(session));
            await fixture.WaitForStartupAsync(startupCancellation.Token);
            return new NetCoreDbgSessionContractDriver(fixture, session, stopAsync, getCallStackAsync, activeWriteGate, pending, state, isUsable, activeReaderTask, disposeAsync);
        }
        catch (Exception failure)
        {
            gate.Release.TrySetResult(true);
            await CaptureFailedTranscriptAsync(fixture, failedTranscript, failure);
            await DisposeFailedSessionAsync(fixture, session, failure);
            await fixture.DisposePreservingFailureAsync(failure);
            throw;
        }
    }
    public static async Task<NetCoreDbgSessionContractDriver> StartWithDapWriteBarrierAsync(
        FixtureConfiguration configuration,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        CancellationToken cancellationToken,
        TimeProvider? stepOverTimeProvider = null)
    {
        var fixture = FixtureProcess.Create(configuration);
        object? session = null;
        DapWriteBarrierStream? writeBarrier = null;
        PreHeaderWriteGate? preHeaderWriteGate = null;
        try
        {
            var assembly = LoadProductionAssemblyOrAssert();
            var sessionType = RequireType(assembly, SessionTypeName);
            var stateType = RequireType(assembly, StateTypeName);
            _ = RequireTaskMethod(sessionType, "StepOverAsync", typeof(int), typeof(CancellationToken));
            var constructor = sessionType.GetConstructors(BindingFlags.Instance | BindingFlags.NonPublic)
                .SingleOrDefault(candidate =>
                {
                    var parameters = candidate.GetParameters();
                    return parameters.Length >= 10
                        && parameters.Skip(10).All(static parameter => parameter.IsOptional)
                        && parameters[0].ParameterType == typeof(Process)
                        && parameters[1].ParameterType == typeof(Stream)
                        && parameters[2].ParameterType == typeof(Stream)
                        && parameters[3].ParameterType == typeof(Stream)
                        && parameters[4].ParameterType == typeof(TimeSpan)
                        && parameters[5].ParameterType == typeof(TimeSpan)
                        && parameters[9].ParameterType == typeof(Func<CancellationToken, Task>);
                });
            Assert.NotNull(constructor);
            var startProtocolAsync = RequireTaskMethod(sessionType, "StartProtocolAsync", typeof(string), typeof(TimeSpan), typeof(CancellationToken));
            var state = RequireProperty(sessionType, "State", stateType);
            var isUsable = RequireInternalProperty(sessionType, "IsUsable", typeof(bool));
            var readerTask = RequirePrivateTaskField(sessionType, "_readerTask");
            var getCallStackAsync = RequireTaskMethod(sessionType, "GetCallStackAsync", typeof(int), typeof(uint), typeof(uint), typeof(CancellationToken));
            var writeGate = RequirePrivateField(sessionType, "_writeGate", typeof(SemaphoreSlim));
            var pending = RequirePrivateField(sessionType, "_pending");
            var stopAsync = RequireTaskMethod(sessionType, "StopAsync", typeof(CancellationToken));
            var disposeAsync = RequireAsyncDisposable(sessionType);
            RequireNarrowSessionSurface(sessionType);
            RequireStateShape(stateType);
            fixture.MarkAdapterStartAttempted();

            using var startupCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            startupCancellation.CancelAfter(initializeTimeout + requestTimeout + TimeSpan.FromMilliseconds(500));
            var startInfo = new ProcessStartInfo(fixture.ExecutablePath)
            {
                UseShellExecute = false,
                RedirectStandardInput = true,
                RedirectStandardOutput = true,
                RedirectStandardError = true,
                CreateNoWindow = true,
            };
            startInfo.ArgumentList.Add("--interpreter=vscode");
            var process = Process.Start(startInfo) ?? throw new InvalidOperationException("Could not start the controlled adapter.");
            writeBarrier = new DapWriteBarrierStream(process.StandardInput.BaseStream);
            preHeaderWriteGate = new PreHeaderWriteGate();
            var constructorArguments = new object?[]
            {
                process,
                writeBarrier,
                process.StandardOutput.BaseStream,
                process.StandardError.BaseStream,
                requestTimeout,
                stopTimeout,
                null,
                null,
                null,
                (Func<CancellationToken, Task>)preHeaderWriteGate.WaitIfArmedAsync,
            }.Concat(constructor.GetParameters().Skip(10).Select(static parameter => parameter.DefaultValue)).ToArray();
            if (stepOverTimeProvider is not null)
            {
                Assert.True(constructorArguments.Length > 10);
                Assert.Equal(typeof(TimeProvider), constructor.GetParameters()[10].ParameterType);
                constructorArguments[10] = stepOverTimeProvider;
            }
            session = constructor.Invoke(constructorArguments);
            var started = startProtocolAsync.Invoke(session, [programPath, initializeTimeout, startupCancellation.Token]);
            await AwaitAsyncResult(started, "StartProtocolAsync", startupCancellation.Token);
            var activeReaderTask = readerTask.GetValue(session) as Task
                ?? throw new InvalidOperationException("NetCoreDbgSession._readerTask returned null.");
            var activeWriteGate = Assert.IsType<SemaphoreSlim>(writeGate.GetValue(session));
            await fixture.WaitForStartupAsync(startupCancellation.Token);
            return new NetCoreDbgSessionContractDriver(fixture, session, stopAsync, getCallStackAsync, activeWriteGate, pending, state, isUsable, activeReaderTask, disposeAsync, writeBarrier, preHeaderWriteGate);
        }
        catch (Exception failure)
        {
            writeBarrier?.Release();
            preHeaderWriteGate?.Release();
            await DisposeFailedSessionAsync(fixture, session, failure);
            await fixture.DisposePreservingFailureAsync(failure);
            throw;
        }
    }
    public static async Task<ConcurrentNetCoreDbgSessionContractDriver> StartConcurrentAsync(
        FixtureConfiguration configuration,
        string programPath,
        TimeSpan initializeTimeout,
        TimeSpan requestTimeout,
        TimeSpan stopTimeout,
        CancellationToken cancellationToken)
    {
        var fixture = FixtureProcess.Create(configuration);
        Task<object>? first = null;
        Task<object>? second = null;
        try
        {
            var assembly = LoadProductionAssemblyOrAssert();
            var sessionType = RequireType(assembly, SessionTypeName);
            var startAsync = RequireStaticStartAsync(sessionType);
            fixture.MarkAdapterStartAttempted();
            using var startupCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            startupCancellation.CancelAfter(initializeTimeout + requestTimeout + TimeSpan.FromMilliseconds(500));

            async Task<object> StartOneAsync()
            {
                var started = startAsync.Invoke(null, [
                    fixture.ExecutablePath,
                    programPath,
                    initializeTimeout,
                    requestTimeout,
                    stopTimeout,
                    startupCancellation.Token,
                ]);
                return await AwaitAsyncResult(started, "StartAsync", startupCancellation.Token)
                    ?? throw new InvalidOperationException("NetCoreDbgSession.StartAsync returned null.");
            }

            first = Task.Run(StartOneAsync);
            second = Task.Run(StartOneAsync);
            var sessions = await Task.WhenAll(first, second);
            return new ConcurrentNetCoreDbgSessionContractDriver(fixture, sessions);
        }
        catch (Exception failure)
        {
            foreach (var task in new[] { first, second })
            {
                if (task is { IsCompletedSuccessfully: true })
                {
                    try
                    {
                        await ((IAsyncDisposable)task.Result).DisposeAsync();
                    }
                    catch (Exception exception)
                    {
                        failure.Data[$"SessionCleanupFailure:{task.Id}"] = exception;
                    }
                }
            }

            await fixture.DisposePreservingFailureAsync(failure);
            throw;
        }
    }


    public async Task StopAsync(CancellationToken cancellationToken) =>
        _ = await AwaitAsyncResult(_stopAsync.Invoke(_session, [cancellationToken]), "StopAsync", cancellationToken);

    public async Task DisposeSessionAsync()
    {
        _ = await AwaitAsyncResult(_disposeAsync.Invoke(_session, []), "DisposeAsync", CancellationToken.None);
        _fixture.MarkCleanupConfirmed();
    }

    private static async Task DisposeFailedSessionAsync(FixtureProcess fixture, object? session, Exception failure)
    {
        if (session is null)
        {
            return;
        }

        try
        {
            var dispose = RequireAsyncDisposable(session.GetType());
            _ = await AwaitAsyncResult(dispose.Invoke(session, []), "DisposeAsync", CancellationToken.None);
            fixture.MarkCleanupConfirmed();
        }
        catch (Exception exception)
        {
            failure.Data["SessionCleanupFailure"] = exception;
        }
    }

    private static async Task CaptureFailedTranscriptAsync(FixtureProcess fixture, ICollection<FixtureTranscriptEntry>? transcript, Exception failure)
    {
        if (transcript is null)
        {
            return;
        }

        try
        {
            foreach (var entry in await fixture.ReadTranscriptAsync())
            {
                transcript.Add(entry);
            }
        }
        catch (Exception exception)
        {
            failure.Data["TranscriptReadFailure"] = exception;
        }
    }

    public Task StopHostedRegistryAsync(CancellationToken cancellationToken)
    {
        const string sessionId = "host-stop-session";
        var registryType = RequireType(_session.GetType().Assembly, "NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry");
        var registryConstructor = registryType.GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(string)],
            modifiers: null);
        Assert.NotNull(registryConstructor);
        var registry = registryConstructor!.Invoke([null]);
        var sessionsField = registryType.GetField("_sessions", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(sessionsField);
        var sessions = sessionsField!.GetValue(registry)
            ?? throw new InvalidOperationException("DebugSessionRegistry._sessions returned null.");
        var tryAdd = sessions.GetType().GetMethod("TryAdd", [typeof(string), _session.GetType()]);
        Assert.NotNull(tryAdd);
        Assert.True((bool)tryAdd!.Invoke(sessions, [sessionId, _session])!);

        var disposerType = RequireType(_session.GetType().Assembly, "NetCoreDbg.Mcp.Stateless.Program+SessionDisposer");
        var disposerConstructor = disposerType.GetConstructor(
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: [registryType],
            modifiers: null);
        Assert.NotNull(disposerConstructor);
        var disposer = disposerConstructor!.Invoke([registry]);
        var stopAsync = disposerType.GetMethod(
            "StopAsync",
            BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(CancellationToken)],
            modifiers: null);
        Assert.NotNull(stopAsync);
        return Assert.IsAssignableFrom<Task>(stopAsync!.Invoke(disposer, [cancellationToken]));
    }

    public Task<HostedThreadsLease> StartHostedThreadsLeaseAsync(CancellationToken cancellationToken) =>
        StartHostedLeaseAsync(cancellationToken);

    public Task<HostedThreadsLease> StartHostedPauseLeaseAsync(int? threadId, CancellationToken cancellationToken) =>
        StartHostedLeaseAsync(cancellationToken, pauseExecution: true, pauseThreadId: threadId);

    private async Task<HostedThreadsLease> StartHostedLeaseAsync(
        CancellationToken cancellationToken, bool pauseExecution = false, int? pauseThreadId = null)
    {
        const string sessionId = "host-drain-session";
        var assembly = _session.GetType().Assembly;
        var registryType = RequireType(assembly, "NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry");
        var registryConstructor = registryType.GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(string)],
            modifiers: null);
        Assert.NotNull(registryConstructor);
        var registry = registryConstructor!.Invoke([null]);
        var sessionsField = registryType.GetField("_sessions", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(sessionsField);
        var sessions = sessionsField!.GetValue(registry)
            ?? throw new InvalidOperationException("DebugSessionRegistry._sessions returned null.");
        var slotType = Assert.IsAssignableFrom<Type>(registryType.GetNestedType("SessionSlot", BindingFlags.NonPublic));
        var slotsField = registryType.GetField("_slots", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(slotsField);
        var slots = slotsField!.GetValue(registry)
            ?? throw new InvalidOperationException("DebugSessionRegistry._slots returned null.");
        var removeSession = sessions.GetType().GetMethod("TryRemove", [typeof(string), _session.GetType().MakeByRefType()]);
        var removeSlot = slots.GetType().GetMethod("TryRemove", [typeof(string), slotType.MakeByRefType()]);
        var addSession = sessions.GetType().GetMethod("TryAdd", [typeof(string), _session.GetType()]);
        var addSlot = slots.GetType().GetMethod("TryAdd", [typeof(string), slotType]);
        Assert.NotNull(removeSession);
        Assert.NotNull(removeSlot);
        Assert.NotNull(addSession);
        Assert.NotNull(addSlot);

        var admissionClosed = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        Action remove = () =>
        {
            _ = removeSlot!.Invoke(slots, [sessionId, null]);
            _ = removeSession!.Invoke(sessions, [sessionId, null]);
            admissionClosed.TrySetResult(true);
        };
        object? slot = null;
        int? leaseCountAtStop = null;
        Func<CancellationToken, Task> stop = token =>
        {
            leaseCountAtStop = Assert.IsType<int>(RequirePrivateField(slotType, "_leases", typeof(int)).GetValue(slot));
            return StopAsync(token);
        };
        Func<ValueTask> dispose = () => new ValueTask(DisposeSessionAsync());
        var slotConstructor = slotType.GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(TimeSpan), typeof(Func<CancellationToken, Task>), typeof(Func<ValueTask>), typeof(Action)],
            modifiers: null);
        Assert.NotNull(slotConstructor);
        slot = slotConstructor!.Invoke([TimeSpan.FromSeconds(2), stop, dispose, remove]);
        Assert.True((bool)addSession!.Invoke(sessions, [sessionId, _session])!);
        Assert.True((bool)addSlot!.Invoke(slots, [sessionId, slot])!);

        IHost? host = null;
        try
        {
            var programType = RequireType(assembly, "NetCoreDbg.Mcp.Stateless.Program");
            var buildHost = programType.GetMethod(
                "BuildHost",
                BindingFlags.Static | BindingFlags.NonPublic,
                binder: null,
                types: [registryType],
                modifiers: null);
            Assert.NotNull(buildHost);
            host = Assert.IsAssignableFrom<IHost>(buildHost!.Invoke(null, [registry]));
            await host.StartAsync(cancellationToken).ConfigureAwait(false);

            var getThreadsAsync = registryType.GetMethod(
                pauseExecution ? "PauseAsync" : "GetThreadsAsync",
                BindingFlags.Instance | BindingFlags.NonPublic,
                binder: null,
                types: pauseExecution
                    ? [typeof(ModelContextProtocol.Protocol.CallToolRequestParams), typeof(ModelContextProtocol.Protocol.RequestId), typeof(CancellationToken)]
                    : [typeof(ModelContextProtocol.Protocol.CallToolRequestParams), typeof(CancellationToken)],
                modifiers: null);
            Assert.NotNull(getThreadsAsync);
            var request = new ModelContextProtocol.Protocol.CallToolRequestParams
            {
                Name = pauseExecution ? "pause_execution" : "get_threads",
                Arguments = new Dictionary<string, JsonElement>
                {
                    ["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId),
                },
            };
            if (pauseThreadId.HasValue)
            {
                request.Arguments["threadId"] = JsonSerializer.SerializeToElement(pauseThreadId.Value);
            }
            object?[] arguments = pauseExecution
                ? [request, new ModelContextProtocol.Protocol.RequestId("host-pause-lease"), cancellationToken]
                : [request, cancellationToken];
            var pending = getThreadsAsync!.Invoke(registry, arguments);
            var asTask = pending?.GetType().GetMethod("AsTask", BindingFlags.Instance | BindingFlags.Public, Type.EmptyTypes);
            Assert.NotNull(asTask);
            var threads = Assert.IsAssignableFrom<Task<ModelContextProtocol.Protocol.CallToolResult>>(asTask!.Invoke(pending, []));
            return new HostedThreadsLease(host, threads, admissionClosed.Task, () => leaseCountAtStop);
        }
        catch
        {
            if (host is not null)
            {
                try
                {
                    await host.StopAsync(CancellationToken.None).ConfigureAwait(false);
                }
                finally
                {
                    host.Dispose();
                }
            }
            else
            {
                remove();
            }

            throw;
        }
    }
    public async Task<RegistryStateProbe> GetStateThroughRegistryAsync(Func<object, bool> isUsable)
    {
        const string sessionId = "test-session";
        var registryType = RequireType(_session.GetType().Assembly, "NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry");
        var evaluatorType = typeof(Func<,>).MakeGenericType(_session.GetType(), typeof(bool));
        var constructor = registryType.GetConstructor(
            BindingFlags.Instance | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(string), evaluatorType],
            modifiers: null);
        Assert.NotNull(constructor);
        var registry = constructor!.Invoke([null, isUsable]);
        var sessionsField = registryType.GetField("_sessions", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(sessionsField);
        var sessions = sessionsField!.GetValue(registry)
            ?? throw new InvalidOperationException("DebugSessionRegistry._sessions returned null.");
        var tryAdd = sessions.GetType().GetMethod("TryAdd", [typeof(string), _session.GetType()]);
        Assert.NotNull(tryAdd);
        Assert.True((bool)tryAdd!.Invoke(sessions, [sessionId, _session])!);
        var containsKey = sessions.GetType().GetMethod("ContainsKey", [typeof(string)]);
        var getStateAsync = registryType.GetMethod("GetStateAsync", BindingFlags.Instance | BindingFlags.NonPublic);
        Assert.NotNull(containsKey);
        Assert.NotNull(getStateAsync);
        var request = new ModelContextProtocol.Protocol.CallToolRequestParams
        {
            Name = "get_debug_state",
            Arguments = new Dictionary<string, JsonElement>
            {
                ["debugSessionId"] = JsonSerializer.SerializeToElement(sessionId),
            },
        };

        try
        {
            var result = (ModelContextProtocol.Protocol.CallToolResult?)await AwaitAsyncResult(
                getStateAsync!.Invoke(registry, [request, CancellationToken.None]),
                "GetStateAsync",
                CancellationToken.None);
            Assert.NotNull(result);
            var structured = Assert.IsType<JsonElement>(result!.StructuredContent);
            var processId = await _fixture.GetProcessIdAsync(CancellationToken.None);
            return new RegistryStateProbe(
                structured.GetProperty("kind").GetString(),
                (bool)containsKey!.Invoke(sessions, [sessionId])!,
                !HasExited(processId), result.ResultType, result.IsError == true, structured.Clone());
        }
        finally
        {
            var tryRemove = sessions.GetType().GetMethod("TryRemove", [typeof(string), _session.GetType().MakeByRefType()]);
            Assert.NotNull(tryRemove);
            _ = tryRemove!.Invoke(sessions, [sessionId, null]);
        }
    }

    private static bool HasExited(int processId)
    {
        try
        {
            using var process = Process.GetProcessById(processId);
            return process.HasExited;
        }
        catch (ArgumentException)
        {
            return true;
        }
    }


    async ValueTask IAsyncDisposable.DisposeAsync()
    {
        _dapWriteBarrier?.Release();
        _preHeaderWriteGate?.Release();
        try
        {
            _fixture.ReleaseGracefulShutdown();
            await DisposeSessionAsync();
        }
        catch (Exception failure)
        {
            await _fixture.DisposePreservingFailureAsync(failure);
            throw;
        }
        await _fixture.DisposeAsync();
    }

    private static Assembly LoadProductionAssemblyOrAssert()
    {
        var productionProject = Path.Combine(RepositoryLayout.Root, "host", ProductionAssemblyName, $"{ProductionAssemblyName}.csproj");
        Assert.True(
            File.Exists(productionProject),
            $"Missing production contract: expected future project '{productionProject}'. T-009 must create it without changing this suite.");

        var productionAssembly = TestOutputPathResolver.ResolveManagedAssembly(RepositoryLayout.Root, Path.Combine("host", ProductionAssemblyName), ProductionAssemblyName);
        return AssemblyLoadContext.Default.LoadFromAssemblyPath(productionAssembly);
    }

    private static Type RequireType(Assembly assembly, string fullName) =>
        assembly.GetType(fullName, throwOnError: false)
        ?? throw new InvalidOperationException($"Missing production contract: type '{fullName}' is absent from '{assembly.Location}'.");

    private static MethodInfo RequireStaticStartAsync(Type sessionType)
    {
        var method = sessionType.GetMethod(
            "StartAsync",
            BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic,
            binder: null,
            types: [typeof(string), typeof(string), typeof(TimeSpan), typeof(TimeSpan), typeof(TimeSpan), typeof(CancellationToken)],
            modifiers: null);
        Assert.NotNull(method);
        Assert.True(IsTaskLike(method!.ReturnType), "Missing production contract: NetCoreDbgSession.StartAsync must return Task or ValueTask.");
        return method;
    }

    private static PropertyInfo RequireProperty(Type type, string name, Type expectedType)
    {
        var property = type.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        Assert.NotNull(property);
        Assert.Equal(expectedType, property!.PropertyType);
        Assert.True(IsImmutable(property), $"Missing production contract: {type.FullName}.{name} must be immutable.");
        return property;
    }

    private static MethodInfo RequireTaskMethod(Type type, string name, params Type[] parameters)
    {
        var method = type.GetMethod(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic, binder: null, parameters, modifiers: null);
        Assert.NotNull(method);
        Assert.True(IsTaskLike(method!.ReturnType), $"Missing production contract: {type.FullName}.{name} must return Task or ValueTask.");
        return method;
    }

    private static MethodInfo RequireAsyncDisposable(Type type)
    {
        Assert.True(typeof(IAsyncDisposable).IsAssignableFrom(type), $"Missing production contract: {type.FullName} must implement IAsyncDisposable.");
        return typeof(IAsyncDisposable).GetMethod(nameof(IAsyncDisposable.DisposeAsync))
            ?? throw new InvalidOperationException("IAsyncDisposable.DisposeAsync could not be reflected.");
    }

    private static PropertyInfo RequirePrivateProperty(Type type, string name, Type expectedType)
    {
        var property = type.GetProperty(name, BindingFlags.Instance | BindingFlags.Static | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        Assert.NotNull(property);
        Assert.True(property!.GetMethod?.IsPrivate, $"Missing production contract: {type.FullName}.{name} getter must be private.");
        Assert.Equal(expectedType, property.PropertyType);
        Assert.True(property.SetMethod is null || property.SetMethod.IsPrivate, $"Missing production contract: {type.FullName}.{name} must be immutable externally.");
        return property;
    }

    private static PropertyInfo RequireInternalProperty(Type type, string name, Type expectedType)
    {
        var property = type.GetProperty(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        Assert.NotNull(property);
        Assert.True(property!.GetMethod?.IsAssembly, $"Missing production contract: {type.FullName}.{name} getter must be internal.");
        Assert.Equal(expectedType, property.PropertyType);
        Assert.True(IsImmutable(property), $"Missing production contract: {type.FullName}.{name} must be immutable.");
        return property;
    }

    private static FieldInfo RequirePrivateTaskField(Type type, string name)
    {
        var field = type.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        Assert.NotNull(field);
        Assert.True(field!.IsPrivate, $"Missing production contract: {type.FullName}.{name} must be private.");
        Assert.Equal(typeof(Task), field.FieldType);
        return field;
    }

    private static FieldInfo RequirePrivateProcessField(Type type, string name)
    {
        var field = type.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        Assert.NotNull(field);
        Assert.True(field!.IsPrivate, $"Missing production contract: {type.FullName}.{name} must be private.");
        Assert.Equal(typeof(Process), field.FieldType);
        return field;
    }

    private static FieldInfo RequirePrivateField(Type type, string name, Type expectedType)
    {
        var field = type.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        Assert.NotNull(field);
        Assert.True(field!.IsPrivate, $"Missing production contract: {type.FullName}.{name} must be private.");
        Assert.Equal(expectedType, field.FieldType);
        return field;
    }
    private static FieldInfo RequirePrivateField(Type type, string name)
    {
        var field = type.GetField(name, BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        Assert.NotNull(field);
        Assert.True(field!.IsPrivate, $"Missing production contract: {type.FullName}.{name} must be private.");
        return field;
    }


    private static void RequireStateShape(Type stateType)
    {
        Assert.True(stateType.IsValueType || stateType.IsSealed, "Missing production contract: DapSessionState must be immutable.");
        _ = RequireProperty(stateType, "Event", typeof(string));
        _ = RequireProperty(stateType, "StopReason", typeof(string));
        _ = RequireProperty(stateType, "ExitCode", typeof(int?));
    }

    private static bool IsImmutable(PropertyInfo property) =>
        property.SetMethod is null || property.SetMethod.ReturnParameter.GetRequiredCustomModifiers().Contains(typeof(IsExternalInit));

    private static bool IsTaskLike(Type type) =>
        typeof(Task).IsAssignableFrom(type) || type == typeof(ValueTask) || (type.IsGenericType && type.GetGenericTypeDefinition() == typeof(ValueTask<>));

    private static void RequireNarrowSessionSurface(Type sessionType)
    {
        foreach (var field in sessionType.GetFields(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
        {
            Assert.False(IsAssemblyVisible(field) && ExposesStream(field.FieldType), $"NetCoreDbgSession must not expose raw Stream field '{field.Name}'.");
        }

        foreach (var property in sessionType.GetProperties(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly))
        {
            Assert.False(IsAssemblyVisible(property) && (ExposesStream(property.PropertyType) || property.GetIndexParameters().Any(static parameter => ExposesStream(parameter.ParameterType))), $"NetCoreDbgSession must not expose raw Stream property '{property.Name}'.");
        }

        foreach (var method in sessionType.GetMethods(BindingFlags.Instance | BindingFlags.Static | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly).Where(static method => !method.IsConstructor))
        {
            if (!IsAssemblyVisible(method))
            {
                continue;
            }

            Assert.False(ExposesStream(method.ReturnType) || method.GetParameters().Any(static parameter => ExposesStream(parameter.ParameterType)), $"NetCoreDbgSession must not expose raw Stream method '{method.Name}'.");
            Assert.False(IsGenericDapRequestSurface(method), $"NetCoreDbgSession must not expose generic DAP request transport '{method.Name}'.");
        }
    }

    private static bool IsAssemblyVisible(FieldInfo field) => !field.IsPrivate;

    private static bool IsAssemblyVisible(PropertyInfo property) =>
        (property.GetMethod is not null && IsAssemblyVisible(property.GetMethod)) ||
        (property.SetMethod is not null && IsAssemblyVisible(property.SetMethod));

    private static bool IsAssemblyVisible(MethodBase method) => !method.IsPrivate;

    private static bool ExposesStream(Type type) =>
        typeof(Stream).IsAssignableFrom(type) ||
        type.HasElementType && ExposesStream(type.GetElementType()!) ||
        type.IsGenericType && type.GetGenericArguments().Any(ExposesStream);

    private static bool IsGenericDapRequestSurface(MethodInfo method)
    {
        var parameters = method.GetParameters();
        var exposesOperation = parameters.Any(static parameter => parameter.ParameterType == typeof(string));
        var exposesPayload = parameters.Any(static parameter => ExposesArbitraryPayload(parameter.ParameterType));
        var exposesGenericResult = ExposesArbitraryPayload(method.ReturnType) || ExposesDapResponse(method.ReturnType);

        return exposesOperation && (exposesPayload || exposesGenericResult);
    }

    private static bool ExposesArbitraryPayload(Type type) =>
        type == typeof(object) || type == typeof(JsonElement) || type == typeof(JsonDocument) ||
        type.HasElementType && ExposesArbitraryPayload(type.GetElementType()!) ||
        type.IsGenericType && type.GetGenericArguments().Any(ExposesArbitraryPayload);

    private static bool ExposesDapResponse(Type type) =>
        type.Name == "DapResponse" ||
        type.HasElementType && ExposesDapResponse(type.GetElementType()!) ||
        type.IsGenericType && type.GetGenericArguments().Any(ExposesDapResponse);

    private static async Task<object?> AwaitAsyncResult(object? value, string member, CancellationToken cancellationToken)
    {
        Assert.NotNull(value);
        if (value is Task task)
        {
            await task.WaitAsync(cancellationToken).ConfigureAwait(false);
            return task.GetType().IsGenericType ? task.GetType().GetProperty("Result")!.GetValue(task) : null;
        }

        var asTask = value!.GetType().GetMethod("AsTask", BindingFlags.Instance | BindingFlags.Public, Type.EmptyTypes);
        Assert.NotNull(asTask);
        return await AwaitAsyncResult(asTask!.Invoke(value, []), member, cancellationToken);
    }
    private sealed class PreHeaderWriteGate
    {
        private TaskCompletionSource<bool>? _reached;
        private TaskCompletionSource<bool>? _release;

        public void Arm()
        {
            _reached = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
            _release = new TaskCompletionSource<bool>(TaskCreationOptions.RunContinuationsAsynchronously);
        }

        public Task WaitIfArmedAsync(CancellationToken cancellationToken)
        {
            var reached = _reached;
            if (reached is null)
            {
                return Task.CompletedTask;
            }

            reached.TrySetResult(true);
            return (_release ?? throw new InvalidOperationException("The pre-header write gate has no release task.")).Task.WaitAsync(cancellationToken);
        }

        public Task WaitAsync(CancellationToken cancellationToken) =>
            (_reached ?? throw new InvalidOperationException("The pre-header write gate is not armed.")).Task.WaitAsync(cancellationToken);

        public void Release() => _release?.TrySetResult(true);
    }

    private sealed class DapWriteBarrierStream(Stream inner) : Stream
    {
        private readonly TaskCompletionSource<bool> _headerWritten = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<bool> _headerEntered = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<bool> _flushed = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private readonly TaskCompletionSource<bool> _release = new(TaskCreationOptions.RunContinuationsAsynchronously);
        private int _armed;
        private int _underlyingHeaderWriteCalls;
        private long _headerBytesForwarded;
        private int _writeCalls;
        private int _prefixBytes;
        private bool _opaqueCancellation;
        private Action? _cancelOnEntry;

        public override bool CanRead => inner.CanRead;
        public override bool CanSeek => inner.CanSeek;
        public override bool CanWrite => inner.CanWrite;
        public override long Length => inner.Length;
        public override long Position { get => inner.Position; set => inner.Position = value; }
        public int WriteCalls => Volatile.Read(ref _writeCalls);
        public int UnderlyingHeaderWriteCalls => Volatile.Read(ref _underlyingHeaderWriteCalls);
        public long HeaderBytesForwarded => Interlocked.Read(ref _headerBytesForwarded);
        public string UnderlyingStreamType => inner.GetType().FullName
            ?? throw new InvalidOperationException("The underlying stdin stream has no runtime type name.");

        public void ArmBeforeUnderlyingHeader(int prefixBytes, bool opaqueCancellation, Action? cancelOnEntry)
        {
            Volatile.Write(ref _underlyingHeaderWriteCalls, 0);
            _prefixBytes = prefixBytes;
            _opaqueCancellation = opaqueCancellation;
            _cancelOnEntry = cancelOnEntry;
            Interlocked.Exchange(ref _headerBytesForwarded, 0);
            Volatile.Write(ref _armed, 3);
        }

        public Task WaitForHeaderEntryAsync(CancellationToken cancellationToken) => _headerEntered.Task.WaitAsync(cancellationToken);

        public void ArmAfterHeader() => Volatile.Write(ref _armed, 1);
        public void Release() => _release.TrySetResult(true);
        public Task WaitForHeaderWriteAsync(CancellationToken cancellationToken) => _headerWritten.Task.WaitAsync(cancellationToken);
        public void ArmAfterFlush() => Volatile.Write(ref _armed, 4);
        public Task WaitForFlushAsync(CancellationToken cancellationToken) => _flushed.Task.WaitAsync(cancellationToken);

        public override void Flush() => inner.Flush();
        public override async Task FlushAsync(CancellationToken cancellationToken)
        {
            await inner.FlushAsync(cancellationToken).ConfigureAwait(false);
            if (Interlocked.CompareExchange(ref _armed, 0, 4) == 4)
            {
                _flushed.TrySetResult(true);
                await _release.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
            }
        }
        public override int Read(byte[] buffer, int offset, int count) => inner.Read(buffer, offset, count);
        public override long Seek(long offset, SeekOrigin origin) => inner.Seek(offset, origin);
        public override void SetLength(long value) => inner.SetLength(value);
        public override void Write(byte[] buffer, int offset, int count) => inner.Write(buffer, offset, count);
        public override Task WriteAsync(byte[] buffer, int offset, int count, CancellationToken cancellationToken) =>
            WriteAsync(buffer.AsMemory(offset, count), cancellationToken).AsTask();

        public override async ValueTask WriteAsync(ReadOnlyMemory<byte> buffer, CancellationToken cancellationToken = default)
        {
            Interlocked.Increment(ref _writeCalls);
            if (Interlocked.CompareExchange(ref _armed, 0, 3) == 3)
            {
                _cancelOnEntry?.Invoke();
                if (_prefixBytes > 0)
                {
                    Interlocked.Increment(ref _underlyingHeaderWriteCalls);
                    await inner.WriteAsync(buffer[.._prefixBytes], cancellationToken).ConfigureAwait(false);
                    Interlocked.Add(ref _headerBytesForwarded, _prefixBytes);
                }
                _headerEntered.TrySetResult(true);
                try
                {
                    await _release.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
                    cancellationToken.ThrowIfCancellationRequested();
                }
                catch (OperationCanceledException) when (_opaqueCancellation)
                {
                    throw new OperationCanceledException();
                }
                Interlocked.Increment(ref _underlyingHeaderWriteCalls);
                await inner.WriteAsync(buffer[_prefixBytes..], cancellationToken).ConfigureAwait(false);
                Interlocked.Add(ref _headerBytesForwarded, buffer.Length - _prefixBytes);
                return;
            }

            if (Interlocked.CompareExchange(ref _armed, 2, 1) == 1)
            {
                await inner.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
                return;
            }

            if (Interlocked.CompareExchange(ref _armed, 0, 2) == 2)
            {
                _headerWritten.TrySetResult(true);
                await _release.Task.WaitAsync(cancellationToken).ConfigureAwait(false);
            }

            await inner.WriteAsync(buffer, cancellationToken).ConfigureAwait(false);
        }
    }
    internal sealed class HostedThreadsLease(
        IHost host,
        Task<ModelContextProtocol.Protocol.CallToolResult> threads,
        Task admissionClosed,
        Func<int?> leaseCountAtStop) : IAsyncDisposable
    {
        private bool _stopped;

        public Task<ModelContextProtocol.Protocol.CallToolResult> Threads { get; } = threads;
        public Task<ModelContextProtocol.Protocol.CallToolResult> Operation => Threads;
        public Task AdmissionClosed => admissionClosed;
        public int? LeaseCountAtStop => leaseCountAtStop();

        public async Task StopAsync(CancellationToken cancellationToken)
        {
            await host.StopAsync(cancellationToken).ConfigureAwait(false);
            _stopped = true;
        }

        public async ValueTask DisposeAsync()
        {
            try
            {
                if (!_stopped)
                {
                    await host.StopAsync(CancellationToken.None).ConfigureAwait(false);
                }
            }
            finally
            {
                host.Dispose();
            }
        }
    }

}

internal sealed class ConcurrentNetCoreDbgSessionContractDriver(
    FixtureProcess fixture,
    object[] sessions) : IAsyncDisposable
{
    public async ValueTask DisposeAsync()
    {
        try
        {
            fixture.ReleaseGracefulShutdown();
            await Task.WhenAll(sessions.Select(static session => ((IAsyncDisposable)session).DisposeAsync().AsTask()));
            fixture.MarkCleanupConfirmed();
        }
        catch (Exception failure)
        {
            await fixture.DisposePreservingFailureAsync(failure);
            throw;
        }
        await fixture.DisposeAsync();
    }
}


internal sealed record DapSessionSnapshot(string? Event, string? StopReason, int? ExitCode);
internal sealed record RegistryStateProbe(string? Kind, bool TokenRetained, bool AdapterAlive, string? ResultType, bool IsError, JsonElement Content);
internal sealed record StepOverRegistryProbe(
    ModelContextProtocol.Protocol.CallToolResult Result,
    bool TokenRetained,
    bool SlotRetained,
    bool AdapterAlive,
    bool CancellationObserved = false,
    ModelContextProtocol.Protocol.CallToolResult? RetainedThreadsResult = null,
    bool? LeaseReleasedBeforeRemoval = null,
    int ActiveRequestCount = 0);


internal sealed record FixtureConfiguration(
    bool SupportsConfigurationDone = true,
    bool SupportsTerminate = true,
    bool IgnoreGracefulShutdown = false,
    bool SpawnDescendant = false,
    bool InitializedBeforeCorrectInitializeResponse = false,
    bool SuppressLifecycleEvents = false,
    string StopReason = "entry",
    int ExitCode = 23,
    bool BlockGracefulShutdown = false,
    bool HoldExitAfterDisconnectResponse = false,
    bool SuppressInitializedAfterInitializeResponse = false,
    bool MalformedCapabilitiesEvent = false,
    bool SendMalformedDapFrameAfterStartup = false,
    bool DelayLaunchResponseForStartupTimeout = false,
    bool OmitInitializeResponseBody = false,
    bool EnableConfigurationDoneAfterInitialization = false,
    bool HoldConfigurationDoneCapabilityDeltaUntilRelease = false,
    bool EnableTerminateAfterInitialization = false,
    bool ExitAfterLaunchResponse = false,
    bool SpawnWindowedDescendant = false,
    bool PublishSecondWindowedDescendantAfterRelease = false,
    string? WindowedDescendantExecutablePath = null,
    IReadOnlyList<string>? WindowedDescendantArguments = null,
    string ThreadsResponseMode = "success",
    bool SupportsDelayedStackTraceLoading = false,
    bool OmitDelayedStackTraceLoading = false,
    string LifecycleMode = "default",
    bool SuppressContinuedAfterStopped = false,
    string StackTraceResponseMode = "success",
    string NextResponseMode = "success",
    string SetBreakpointsResponseMode = "success",
    string ScopesResponseMode = "success",
    string VariablesResponseMode = "success",
    string PauseResponseMode = "ack-only",
    int? PauseEventThreadId = 1,
    bool? PauseAllThreadsStopped = true,
    bool? SupportsExceptionInfoRequest = null,
    string ExceptionInfoResponseMode = "success",
    string ExceptionInfoCapabilityDelta = "none",
    string ExceptionBreakpointFilters = "omit",
    string ExceptionFiltersCapabilityDelta = "none",
    string SetExceptionBreakpointsResponseMode = "absent-body",
    bool? SupportsSetVariable = null,
    string SetVariableCapabilityDelta = "none")
{
    public string AsEnvironmentValue() => string.Join(
        ';',
        new[]
        {
            SupportsConfigurationDone ? "--supports-configuration-done" : null,
            SupportsTerminate ? "--supports-terminate" : null,
            IgnoreGracefulShutdown ? "--ignore-graceful-shutdown" : null,
            SpawnWindowedDescendant ? "--spawn-windowed-descendant" : SpawnDescendant ? "--spawn-descendant" : null,
            PublishSecondWindowedDescendantAfterRelease ? "--publish-second-windowed-descendant-after-release" : null,
            InitializedBeforeCorrectInitializeResponse ? "--initialized-before-correct-initialize-response" : null,
            SuppressLifecycleEvents ? "--suppress-lifecycle-events" : null,
            $"--stop-reason={StopReason}",
            $"--exit-code={ExitCode}",
            BlockGracefulShutdown ? "--block-graceful-shutdown" : null,
            HoldExitAfterDisconnectResponse ? "--hold-exit-after-disconnect-response" : null,
            SuppressInitializedAfterInitializeResponse ? "--suppress-initialized-after-initialize-response" : null,
            MalformedCapabilitiesEvent ? "--malformed-capabilities-event" : null,
            SendMalformedDapFrameAfterStartup ? "--send-malformed-dap-frame-after-startup" : null,
            DelayLaunchResponseForStartupTimeout ? "--delay-launch-response-for-startup-timeout" : null,
            OmitInitializeResponseBody ? "--omit-initialize-response-body" : null,
            EnableConfigurationDoneAfterInitialization ? "--enable-configuration-done-after-initialization" : null,
            HoldConfigurationDoneCapabilityDeltaUntilRelease ? "--hold-configuration-done-capabilities-delta-until-release" : null,
            EnableTerminateAfterInitialization ? "--enable-terminate-after-initialization" : null,
            ExitAfterLaunchResponse ? "--exit-after-launch-response" : null,
            $"--threads-response={ThreadsResponseMode}",
            SupportsDelayedStackTraceLoading ? "--supports-delayed-stack-trace-loading" : null,
            OmitDelayedStackTraceLoading ? "--omit-delayed-stack-trace-loading" : null,
            $"--lifecycle-mode={LifecycleMode}",
            SuppressContinuedAfterStopped ? "--suppress-continued-after-stopped" : null,
            $"--stack-trace-response={StackTraceResponseMode}",
            $"--next-response={NextResponseMode}",
            $"--set-breakpoints-response={SetBreakpointsResponseMode}",
            $"--scopes-response={ScopesResponseMode}",
            $"--variables-response={VariablesResponseMode}",
            $"--pause-response={PauseResponseMode}",
            $"--pause-event-thread-id={PauseEventThreadId?.ToString(System.Globalization.CultureInfo.InvariantCulture) ?? "omit"}",
            $"--pause-all-threads-stopped={PauseAllThreadsStopped?.ToString() ?? "omit"}",
            $"--supports-exception-info={SupportsExceptionInfoRequest?.ToString() ?? "omit"}",
            $"--exception-info-response={ExceptionInfoResponseMode}",
            $"--exception-info-capability-delta={ExceptionInfoCapabilityDelta}",
            $"--exception-breakpoint-filters={ExceptionBreakpointFilters}",
            $"--exception-filters-capability-delta={ExceptionFiltersCapabilityDelta}",
            $"--set-exception-breakpoints-response={SetExceptionBreakpointsResponseMode}",
            $"--supports-set-variable={SupportsSetVariable?.ToString() ?? "omit"}",
            $"--set-variable-capability-delta={SetVariableCapabilityDelta}",
        }.Where(static value => value is not null));
}

internal sealed class FixtureProcess : IAsyncDisposable
{
    private readonly string _scratchDirectory;
    private readonly string _transcriptPath;
    private readonly string _releasePath;
    private readonly string _configurationDoneCapabilityDeltaReleasePath;
    private readonly string _secondWindowedDescendantReleasePath;
    private readonly string _threadsResponseReleasePath;
    private readonly string _stackTraceResponseReleasePath;
    private readonly string _nextResponseReleasePath;
    private readonly string? _previousTranscript;
    private readonly string? _previousOptions;
    private readonly string? _previousRelease;
    private readonly string? _previousConfigurationDoneCapabilityDeltaRelease;
    private readonly string? _previousSecondWindowedDescendantRelease;
    private readonly string? _previousThreadsResponseRelease;
    private readonly string? _previousStackTraceResponseRelease;
    private readonly string? _previousNextResponseRelease;
    private readonly string? _previousWindowedDescendantExecutable;
    private readonly string? _previousWindowedDescendantArguments;
    private readonly string _executablePath;
    private static readonly TimeSpan ProcessCleanupTimeout = TimeSpan.FromSeconds(1);
    private bool _adapterStartAttempted;
    private bool _cleanupConfirmed;
    private readonly object _disposeGate = new();
    private Task? _disposeTask;

    private FixtureProcess(
        string scratchDirectory,
        string transcriptPath,
        string releasePath,
        string configurationDoneCapabilityDeltaReleasePath,
        string secondWindowedDescendantReleasePath,
        string threadsResponseReleasePath,
        string stackTraceResponseReleasePath,
        string nextResponseReleasePath,
        string executablePath,
        string? previousTranscript,
        string? previousOptions,
        string? previousRelease,
        string? previousConfigurationDoneCapabilityDeltaRelease,
        string? previousSecondWindowedDescendantRelease,
        string? previousWindowedDescendantExecutable,
        string? previousThreadsResponseRelease,
        string? previousStackTraceResponseRelease,
        string? previousNextResponseRelease,
        string? previousWindowedDescendantArguments)
    {
        _scratchDirectory = scratchDirectory;
        _transcriptPath = transcriptPath;
        _releasePath = releasePath;
        _configurationDoneCapabilityDeltaReleasePath = configurationDoneCapabilityDeltaReleasePath;
        _secondWindowedDescendantReleasePath = secondWindowedDescendantReleasePath;
        _threadsResponseReleasePath = threadsResponseReleasePath;
        _stackTraceResponseReleasePath = stackTraceResponseReleasePath;
        _nextResponseReleasePath = nextResponseReleasePath;
        _executablePath = executablePath;
        _previousTranscript = previousTranscript;
        _previousOptions = previousOptions;
        _previousRelease = previousRelease;
        _previousConfigurationDoneCapabilityDeltaRelease = previousConfigurationDoneCapabilityDeltaRelease;
        _previousSecondWindowedDescendantRelease = previousSecondWindowedDescendantRelease;
        _previousWindowedDescendantExecutable = previousWindowedDescendantExecutable;
        _previousThreadsResponseRelease = previousThreadsResponseRelease;
        _previousWindowedDescendantArguments = previousWindowedDescendantArguments;
        _previousStackTraceResponseRelease = previousStackTraceResponseRelease;
        _previousNextResponseRelease = previousNextResponseRelease;
    }

    public string ExecutablePath => _executablePath;

    public static FixtureProcess Create(FixtureConfiguration configuration)
    {
        var adapter = TestOutputPathResolver.ResolveProcess(RepositoryLayout.ControlledAdapterDirectory, "ControlledDapAdapter");
        Assert.Empty(adapter.Arguments);
        var executable = adapter.Command;
        var scratchDirectory = Path.Combine(RepositoryLayout.ScratchRoot, $"controlled-dap-{Guid.NewGuid():N}");
        Directory.CreateDirectory(scratchDirectory);
        var transcriptPath = Path.Combine(scratchDirectory, "transcript.jsonl");
        var releasePath = Path.Combine(scratchDirectory, "graceful-shutdown.release");
        var configurationDoneCapabilityDeltaReleasePath = Path.Combine(scratchDirectory, "configuration-done-capabilities-delta.release");
        var secondWindowedDescendantReleasePath = Path.Combine(scratchDirectory, "second-windowed-descendant.release");
        var threadsResponseReleasePath = Path.Combine(scratchDirectory, "threads-response.release");
        var stackTraceResponseReleasePath = Path.Combine(scratchDirectory, "stack-trace-response.release");
        var nextResponseReleasePath = Path.Combine(scratchDirectory, "next-response.release");
        File.WriteAllText(transcriptPath, string.Empty, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        var previousTranscript = Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT");
        var previousOptions = Environment.GetEnvironmentVariable("CONTROLLED_DAP_OPTIONS");
        var previousRelease = Environment.GetEnvironmentVariable("CONTROLLED_DAP_GRACEFUL_RELEASE");
        var previousConfigurationDoneCapabilityDeltaRelease = Environment.GetEnvironmentVariable("CONTROLLED_DAP_CONFIGURATION_DONE_CAPABILITY_DELTA_RELEASE");
        var previousSecondWindowedDescendantRelease = Environment.GetEnvironmentVariable("CONTROLLED_DAP_SECOND_WINDOWED_DESCENDANT_RELEASE");
        var previousThreadsResponseRelease = Environment.GetEnvironmentVariable("CONTROLLED_DAP_THREADS_RESPONSE_RELEASE");
        var previousStackTraceResponseRelease = Environment.GetEnvironmentVariable("CONTROLLED_DAP_STACK_TRACE_RESPONSE_RELEASE");
        var previousNextResponseRelease = Environment.GetEnvironmentVariable("CONTROLLED_DAP_NEXT_RESPONSE_RELEASE");
        var previousWindowedDescendantExecutable = Environment.GetEnvironmentVariable("CONTROLLED_DAP_WINDOWED_DESCENDANT_EXECUTABLE");
        var previousWindowedDescendantArguments = Environment.GetEnvironmentVariable("CONTROLLED_DAP_WINDOWED_DESCENDANT_ARGUMENTS");
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT", transcriptPath);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_OPTIONS", configuration.AsEnvironmentValue());
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_GRACEFUL_RELEASE", releasePath);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_CONFIGURATION_DONE_CAPABILITY_DELTA_RELEASE", configurationDoneCapabilityDeltaReleasePath);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_SECOND_WINDOWED_DESCENDANT_RELEASE", secondWindowedDescendantReleasePath);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_THREADS_RESPONSE_RELEASE", threadsResponseReleasePath);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_STACK_TRACE_RESPONSE_RELEASE", stackTraceResponseReleasePath);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_NEXT_RESPONSE_RELEASE", nextResponseReleasePath);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_WINDOWED_DESCENDANT_EXECUTABLE", configuration.WindowedDescendantExecutablePath);
        Environment.SetEnvironmentVariable(
            "CONTROLLED_DAP_WINDOWED_DESCENDANT_ARGUMENTS",
            configuration.WindowedDescendantArguments is null ? null : JsonSerializer.Serialize(configuration.WindowedDescendantArguments));
        return new FixtureProcess(
            scratchDirectory,
            transcriptPath,
            releasePath,
            configurationDoneCapabilityDeltaReleasePath,
            secondWindowedDescendantReleasePath,
            threadsResponseReleasePath,
            stackTraceResponseReleasePath,
            nextResponseReleasePath,
            executable,
            previousTranscript,
            previousOptions,
            previousRelease,
            previousConfigurationDoneCapabilityDeltaRelease,
            previousSecondWindowedDescendantRelease,
            previousWindowedDescendantExecutable,
            previousThreadsResponseRelease,
            previousStackTraceResponseRelease,
            previousNextResponseRelease,
            previousWindowedDescendantArguments);
    }
    public void MarkAdapterStartAttempted()
    {
        _adapterStartAttempted = true;
        _cleanupConfirmed = false;
    }
    public void MarkCleanupConfirmed() => _cleanupConfirmed = true;

    public void ReleaseGracefulShutdown() => File.WriteAllText(_releasePath, string.Empty);
    public void ReleaseConfigurationDoneCapabilityDelta() => File.WriteAllText(_configurationDoneCapabilityDeltaReleasePath, string.Empty);
    public void ReleaseSecondWindowedDescendant() => File.WriteAllText(_secondWindowedDescendantReleasePath, string.Empty);
    public void ReleaseThreadsResponse() => File.WriteAllText(_threadsResponseReleasePath, string.Empty);
    public void ReleaseStackTraceResponse() => File.WriteAllText(_stackTraceResponseReleasePath, string.Empty);
    public void ReleaseNextResponse() => File.WriteAllText(_nextResponseReleasePath, string.Empty);
    public void ReleasePauseResponse() => ReleasePauseControl("pause-response");
    public void ReleasePauseStoppedEvent() => ReleasePauseControl("pause-stop");
    public void ReleasePauseStop() => ReleasePauseStoppedEvent();
    public void ReleaseContinueResponse() => ReleasePauseControl("continue-response");
    public void ReleaseThreadsResponse(int requestSequence) =>
        ReleasePauseControl("threads-response-" + requestSequence.ToString(System.Globalization.CultureInfo.InvariantCulture));

    private void ReleasePauseControl(string control) =>
        File.WriteAllText(_transcriptPath + "." + control + ".release", string.Empty);

    public async Task WaitForStartupAsync(CancellationToken cancellationToken)
    {
        await WaitUntilAsync(() => ReadTranscriptLinesSnapshot().Any(static line => line.Contains("\"kind\":\"startup\"", StringComparison.Ordinal)), cancellationToken);
    }

    public async Task<IReadOnlyList<FixtureTranscriptEntry>> ReadTranscriptAsync()
    {
        await Task.Delay(TimeSpan.FromMilliseconds(25));
        return ReadTranscriptLinesSnapshot()
            .Select(FixtureTranscriptEntry.Parse)
            .ToArray();
    }

    public Task WaitForPauseRequestAsync(CancellationToken cancellationToken) =>
        WaitForDapRequestCountAsync("pause", 1, cancellationToken);

    public Task WaitForDapRequestCountAsync(string command, int count, CancellationToken cancellationToken) => WaitUntilAsync(
        () => ReadTranscriptLinesSnapshot()
            .Select(FixtureTranscriptEntry.Parse)
            .Count(entry => entry.Kind == "request" && entry.Command == command) >= count,
        cancellationToken);

    public Task WaitForDapResponseAsync(string command, int requestSequence, CancellationToken cancellationToken = default) => WaitUntilAsync(
        () => ReadTranscriptLinesSnapshot()
            .Select(FixtureTranscriptEntry.Parse)
            .Any(entry => entry.Kind == "response" && entry.Command == command && entry.Sequence == requestSequence),
        cancellationToken);

    public Task WaitForThreadsRequestAsync(CancellationToken cancellationToken) => WaitUntilAsync(
        () => ReadTranscriptLinesSnapshot()
            .Select(FixtureTranscriptEntry.Parse)
            .Any(static entry => entry.Kind == "request" && entry.Command == "threads"),
        cancellationToken);

    public Task WaitForStackTraceRequestAsync(CancellationToken cancellationToken) => WaitUntilAsync(
        () => ReadTranscriptLinesSnapshot()
            .Select(FixtureTranscriptEntry.Parse)
            .Any(static entry => entry.Kind == "request" && entry.Command == "stackTrace"),
        cancellationToken);
    public Task WaitForNextRequestAsync(CancellationToken cancellationToken) => WaitUntilAsync(
        () => ReadTranscriptLinesSnapshot()
            .Select(FixtureTranscriptEntry.Parse)
            .Any(static entry => entry.Kind == "request" && entry.Command == "next"),
        cancellationToken);

    public Task WaitForEventAsync(string eventName, CancellationToken cancellationToken) => WaitUntilAsync(
        () => ReadTranscriptLinesSnapshot()
            .Select(FixtureTranscriptEntry.Parse)
            .Any(entry => entry.Kind == "event" && entry.Event == eventName),
        cancellationToken);

    public Task WaitForTranscriptKindAsync(string kind, CancellationToken cancellationToken) => WaitUntilAsync(
        () => ReadTranscriptLinesSnapshot()
            .Select(FixtureTranscriptEntry.Parse)
            .Any(entry => entry.Kind == kind),
        cancellationToken);

    public async Task<int> GetProcessIdAsync(CancellationToken cancellationToken)
    {
        return (await ReadTranscriptAsync()).LastOrDefault(static entry => entry.Kind == "startup")?.ProcessId
            ?? throw new InvalidOperationException("Controlled adapter did not record its process id.");
    }

    public async Task<int> WaitForSecondWindowedDescendantPublicationAsync(CancellationToken cancellationToken)
    {
        FixtureTranscriptEntry? published = null;
        await WaitUntilAsync(
            () =>
            {
                published = ReadTranscriptLinesSnapshot()
                    .Select(FixtureTranscriptEntry.Parse)
                    .SingleOrDefault(static entry => entry.Kind == "second-windowed-descendant-published");
                return published is not null;
            },
            cancellationToken);
        return published?.ProcessId
            ?? throw new InvalidOperationException("Controlled adapter did not publish its second windowed descendant.");
    }


    public async Task DisposePreservingFailureAsync(Exception failure)
    {
        try
        {
            await DisposeAsync();
        }
        catch (Exception exception)
        {
            failure.Data["FixtureCleanupFailure"] = exception;
            throw new AggregateException("Fixture cleanup failed after an earlier failure.", failure, exception);
        }
    }

    public ValueTask DisposeAsync()
    {
        lock (_disposeGate)
        {
            return new ValueTask(_disposeTask ??= DisposeCoreAsync());
        }
    }

    private async Task DisposeCoreAsync()
    {
        Exception? cleanupFailure = null;
        try
        {
            ReleaseGracefulShutdown();
            ReleaseConfigurationDoneCapabilityDelta();
            ReleaseThreadsResponse();
            ReleaseStackTraceResponse();
            ReleaseNextResponse();
        }
        catch (Exception exception)
        {
            cleanupFailure = exception;
        }

        try
        {
            if (!_adapterStartAttempted && !_cleanupConfirmed)
            {
                try
                {
                    _adapterStartAttempted = ReadTranscriptLinesSnapshot()
                        .Select(FixtureTranscriptEntry.Parse)
                        .Any(static entry => entry.Kind == "startup");
                }
                catch (Exception exception)
                {
                    cleanupFailure ??= exception;
                }
            }

            if (_adapterStartAttempted && !_cleanupConfirmed)
            {
                cleanupFailure ??= new InvalidOperationException($"Controlled adapter cleanup is unresolved; scratch retained at '{_scratchDirectory}'.");
            }
            else if (cleanupFailure is null)
            {
                try
                {
                    await DeleteScratchDirectoryAsync();
                }
                catch (Exception exception)
                {
                    cleanupFailure = exception;
                }
            }
        }
        finally
        {
            void Restore(string name, string? value)
            {
                try
                {
                    Environment.SetEnvironmentVariable(name, value);
                }
                catch (Exception exception)
                {
                    if (cleanupFailure is null)
                    {
                        cleanupFailure = exception;
                    }
                    else
                    {
                        cleanupFailure.Data[$"Restore:{name}"] = exception;
                    }
                }
            }

            Restore("CONTROLLED_DAP_TRANSCRIPT", _previousTranscript);
            Restore("CONTROLLED_DAP_OPTIONS", _previousOptions);
            Restore("CONTROLLED_DAP_GRACEFUL_RELEASE", _previousRelease);
            Restore("CONTROLLED_DAP_CONFIGURATION_DONE_CAPABILITY_DELTA_RELEASE", _previousConfigurationDoneCapabilityDeltaRelease);
            Restore("CONTROLLED_DAP_SECOND_WINDOWED_DESCENDANT_RELEASE", _previousSecondWindowedDescendantRelease);
            Restore("CONTROLLED_DAP_THREADS_RESPONSE_RELEASE", _previousThreadsResponseRelease);
            Restore("CONTROLLED_DAP_WINDOWED_DESCENDANT_EXECUTABLE", _previousWindowedDescendantExecutable);
            Restore("CONTROLLED_DAP_WINDOWED_DESCENDANT_ARGUMENTS", _previousWindowedDescendantArguments);
            Restore("CONTROLLED_DAP_STACK_TRACE_RESPONSE_RELEASE", _previousStackTraceResponseRelease);
            Restore("CONTROLLED_DAP_NEXT_RESPONSE_RELEASE", _previousNextResponseRelease);
        }

        if (cleanupFailure is not null)
        {
            throw cleanupFailure;
        }
    }


    private IReadOnlyList<string> ReadTranscriptLinesSnapshot()
    {
        using var stream = new FileStream(_transcriptPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        using var reader = new StreamReader(stream, Encoding.UTF8, detectEncodingFromByteOrderMarks: true);
        var contents = reader.ReadToEnd();
        var lastNewline = contents.LastIndexOf('\n');
        return lastNewline < 0
            ? []
            : contents[..lastNewline]
                .Split('\n')
                .Select(static line => line.EndsWith('\r') ? line[..^1] : line)
                .Where(static line => !string.IsNullOrWhiteSpace(line))
                .ToArray();
    }

    private async Task DeleteScratchDirectoryAsync()
    {
        var startedAt = Stopwatch.GetTimestamp();
        Exception? lastFailure = null;
        while (Directory.Exists(_scratchDirectory))
        {
            try
            {
                Directory.Delete(_scratchDirectory, recursive: true);
                return;
            }
            catch (IOException exception)
            {
                lastFailure = exception;
            }
            catch (UnauthorizedAccessException exception)
            {
                lastFailure = exception;
            }

            if (Stopwatch.GetElapsedTime(startedAt) >= ProcessCleanupTimeout)
            {
                throw new InvalidOperationException($"Owned controlled adapter scratch directory '{_scratchDirectory}' could not be removed within {ProcessCleanupTimeout}.", lastFailure);
            }

            await Task.Delay(TimeSpan.FromMilliseconds(25));
        }
    }


    private static async Task WaitUntilAsync(Func<bool> condition, CancellationToken cancellationToken)
    {
        while (!condition())
        {
            await Task.Delay(TimeSpan.FromMilliseconds(25), cancellationToken);
        }
    }
}

internal sealed record FixtureTranscriptEntry(
    string Kind,
    int? Sequence,
    string? Command,
    string? Arguments,
    string? RawPayload,
    int? ProcessId,
    long? ProcessStartTimeUtcTicks,
    bool? SimpleControlledTree,
    int? ContentLength,
    int? PayloadByteCount,
    string? Stage,
    string? Event,
    bool? BodyOmitted,
    bool? SupportsConfigurationDoneRequest,
    bool? SupportsTerminateRequest)
{
    public static FixtureTranscriptEntry Parse(string json)
    {
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        return new FixtureTranscriptEntry(
            root.GetProperty("kind").GetString() ?? throw new InvalidDataException("Fixture transcript kind is absent."),
            root.TryGetProperty("sequence", out var sequence)
                && sequence.ValueKind == JsonValueKind.Number
                && sequence.TryGetInt32(out var sequenceValue) ? sequenceValue : null,
            root.TryGetProperty("command", out var command) ? command.GetString() : null,
            root.TryGetProperty("arguments", out var arguments) ? arguments.GetString() : null,
            root.TryGetProperty("rawPayload", out var rawPayload) ? rawPayload.GetString() : null,
            root.TryGetProperty("processId", out var processId) ? processId.GetInt32() : null,
            root.TryGetProperty("processStartTimeUtcTicks", out var processStartTimeUtcTicks) && processStartTimeUtcTicks.TryGetInt64(out var ticks) ? ticks : null,
            root.TryGetProperty("simpleControlledTree", out var simpleControlledTree) ? simpleControlledTree.GetBoolean() : null,
            root.TryGetProperty("contentLength", out var contentLength) ? contentLength.GetInt32() : null,
            root.TryGetProperty("payloadByteCount", out var payloadByteCount) ? payloadByteCount.GetInt32() : null,
            root.TryGetProperty("stage", out var stage) ? stage.GetString() : null,
            root.TryGetProperty("event", out var eventName) ? eventName.GetString() : null,
            root.TryGetProperty("bodyOmitted", out var bodyOmitted) ? bodyOmitted.GetBoolean() : null,
            root.TryGetProperty("supportsConfigurationDoneRequest", out var supportsConfigurationDoneRequest) ? supportsConfigurationDoneRequest.GetBoolean() : null,
            root.TryGetProperty("supportsTerminateRequest", out var supportsTerminateRequest) ? supportsTerminateRequest.GetBoolean() : null);
    }
}

internal static class RepositoryLayout
{
    public static readonly string Root = FindRoot();
    public static readonly string ControlledAdapterDirectory = Path.Combine(Root, "host", "NetCoreDbg.Mcp.Stateless.Tests", "Fixtures", "ControlledDapAdapter");
    public static readonly string ScratchCoordinationRoot = ResolveScratchCoordinationRoot(Root);
    public static readonly string ScratchRoot = OutputRoot("NETCOREDBG_MCP_TEST_SCRATCH_ROOT")
        ?? Path.Combine(ScratchCoordinationRoot, ".agent", "tmp", "netcoredbg-mcp-stateless-tests");

    internal static string ResolveScratchCoordinationRoot(string sourceRoot)
    {
        sourceRoot = Path.GetFullPath(sourceRoot);
        var gitDirectory = Path.Combine(sourceRoot, ".git");
        if (File.Exists(gitDirectory))
        {
            const string prefix = "gitdir:";
            var pointer = File.ReadAllText(gitDirectory).Trim();
            if (!pointer.StartsWith(prefix, StringComparison.Ordinal)
                || string.IsNullOrWhiteSpace(pointer[prefix.Length..]))
            {
                throw new InvalidDataException("The test source root has an invalid Git directory pointer.");
            }

            gitDirectory = Path.GetFullPath(pointer[prefix.Length..].Trim(), sourceRoot);
        }

        var commonDirectory = gitDirectory;
        var commonDirectoryFile = Path.Combine(gitDirectory, "commondir");
        if (File.Exists(commonDirectoryFile))
        {
            var pointer = File.ReadAllText(commonDirectoryFile).Trim();
            if (string.IsNullOrWhiteSpace(pointer))
            {
                throw new InvalidDataException("The test source root has an empty Git common-directory pointer.");
            }

            commonDirectory = Path.GetFullPath(pointer, gitDirectory);
        }

        if (!Directory.Exists(Path.Combine(commonDirectory, "objects")))
        {
            throw new InvalidDataException("The shared Git common directory is unavailable for test scratch.");
        }

        return Directory.GetParent(Path.TrimEndingDirectorySeparator(commonDirectory))?.FullName
            ?? throw new InvalidDataException("The shared Git common directory has no coordination parent.");
    }

    private static string FindRoot([System.Runtime.CompilerServices.CallerFilePath] string sourceFile = "")
    {
        if (Environment.GetEnvironmentVariable("NETCOREDBG_MCP_TEST_SOURCE_ROOT") is { } sourceRoot)
        {
            var compiledRoot = Path.GetFullPath(Path.Combine(Path.GetDirectoryName(sourceFile)!, "..", "..", ".."));
            if (!Path.IsPathFullyQualified(sourceRoot) || !Directory.Exists(sourceRoot)
                || !string.Equals(Path.TrimEndingDirectorySeparator(Path.GetFullPath(sourceRoot)), compiledRoot,
                    OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal))
            {
                throw new InvalidOperationException("Explicit test source root does not match the compiled source checkout.");
            }
            return compiledRoot;
        }

        for (var current = new DirectoryInfo(AppContext.BaseDirectory); current is not null; current = current.Parent)
        {
            if (Directory.Exists(Path.Combine(current.FullName, ".git")) || File.Exists(Path.Combine(current.FullName, "AGENTS.md")))
            {
                return current.FullName;
            }
        }

        throw new InvalidOperationException("Repository root could not be located from the test assembly base directory.");
    }

    internal static string? OutputRoot(string variable)
    {
        var value = Environment.GetEnvironmentVariable(variable);
        if (value is null)
        {
            return null;
        }
        if (Environment.GetEnvironmentVariable("NETCOREDBG_MCP_TEST_SOURCE_ROOT") is null)
        {
            throw new InvalidOperationException("Explicit test output requires an explicit compiled-source root.");
        }
        var owner = ScratchCoordinationRoot;
        if (!Path.IsPathFullyQualified(value))
        {
            throw new InvalidOperationException($"{variable} must be an absolute primary-owned output path.");
        }
        var path = Path.GetFullPath(value);
        var prefix = Path.Combine(owner, ".agent") + Path.DirectorySeparatorChar;
        if (!path.StartsWith(prefix, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal))
        {
            throw new InvalidOperationException($"{variable} must stay beneath the Git coordination owner's .agent directory.");
        }
        for (var current = new DirectoryInfo(path); current is not null && current.FullName.Length > owner.Length; current = current.Parent)
        {
            if (current.Exists && (current.Attributes & FileAttributes.ReparsePoint) != 0)
            {
                throw new InvalidOperationException($"{variable} contains a reparse point.");
            }
        }
        return path;
    }
}

internal sealed record TestOutputProcess(string Command, List<string> Arguments, string TargetPath);

internal static class TestOutputPathResolver
{
    public static string ResolveManagedAssembly(string repositoryRoot, string projectRelativePath, string assemblyName) =>
        ResolveTargetPath(repositoryRoot, projectRelativePath, assemblyName);

    public static TestOutputProcess ResolveProcess(string projectDirectory, string assemblyName)
    {
        var targetPath = ResolveTargetPath(projectDirectory, projectRelativePath: null, assemblyName);
        var appHost = Path.Combine(
            Path.GetDirectoryName(targetPath) ?? throw new InvalidOperationException($"Output directory is absent for '{targetPath}'."),
            OperatingSystem.IsWindows() ? $"{assemblyName}.exe" : assemblyName);
        return File.Exists(appHost)
            ? new TestOutputProcess(appHost, [], targetPath)
            : new TestOutputProcess("dotnet", [targetPath], targetPath);
    }

    private static string ResolveTargetPath(string projectDirectoryOrRepositoryRoot, string? projectRelativePath, string assemblyName)
    {
        var outputDirectory = new DirectoryInfo(AppContext.BaseDirectory);
        var targetFramework = outputDirectory.Name;
        var configuration = outputDirectory.Parent?.Name
            ?? throw new InvalidOperationException($"Test output configuration is absent from '{AppContext.BaseDirectory}'.");
        var projectDirectory = projectRelativePath is null
            ? projectDirectoryOrRepositoryRoot
            : Path.Combine(projectDirectoryOrRepositoryRoot, projectRelativePath);
        var artifactsPath = RepositoryLayout.OutputRoot("NETCOREDBG_MCP_TEST_ARTIFACTS_PATH");
        var targetPath = string.IsNullOrEmpty(artifactsPath)
            ? Path.Combine(projectDirectory, "bin", configuration, targetFramework, $"{assemblyName}.dll")
            : Path.Combine(artifactsPath, "bin", Path.GetFileName(projectDirectory), outputDirectory.Name, $"{assemblyName}.dll");
        Assert.True(File.Exists(targetPath), $"Built test target is absent: '{targetPath}'.");
        return targetPath;
    }
}
