using System.Diagnostics;
using System.Reflection;
using System.Runtime.CompilerServices;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
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
    private readonly PropertyInfo _state;
    private readonly PropertyInfo _isUsable;
    private readonly Task _readerTask;
    private readonly MethodInfo _disposeAsync;

    private NetCoreDbgSessionContractDriver(
        FixtureProcess fixture,
        object session,
        MethodInfo stopAsync,
        PropertyInfo state,
        PropertyInfo isUsable,
        Task readerTask,
        MethodInfo disposeAsync)
    {
        _fixture = fixture;
        _session = session;
        _stopAsync = stopAsync;
        _state = state;
        _isUsable = isUsable;
        _readerTask = readerTask;
        _disposeAsync = disposeAsync;
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

    public Task WaitForReaderCompletionAsync(CancellationToken cancellationToken) => _readerTask.WaitAsync(cancellationToken);

    public bool CapabilitiesObserved => (bool)(RequirePrivateProperty(_session.GetType(), "CapabilitiesObserved", typeof(bool)).GetValue(_session)
        ?? throw new InvalidOperationException("NetCoreDbgSession.CapabilitiesObserved returned null."));

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
        try
        {
            var assembly = LoadProductionAssemblyOrAssert();
            var sessionType = RequireType(assembly, SessionTypeName);
            var stateType = RequireType(assembly, StateTypeName);
            var startAsync = RequireStaticStartAsync(sessionType);
            var state = RequireProperty(sessionType, "State", stateType);
            var isUsable = RequireInternalProperty(sessionType, "IsUsable", typeof(bool));
            var readerTask = RequirePrivateTaskField(sessionType, "_readerTask");
            var stopAsync = RequireTaskMethod(sessionType, "StopAsync", typeof(CancellationToken));
            var disposeAsync = RequireAsyncDisposable(sessionType);
            RequireNarrowSessionSurface(sessionType);
            RequireStateShape(stateType);
            fixture.MarkAdapterStartAttempted();

            using var startupCancellation = CancellationTokenSource.CreateLinkedTokenSource(cancellationToken);
            startupCancellation.CancelAfter(initializeTimeout + requestTimeout + TimeSpan.FromMilliseconds(500));
            var started = startAsync.Invoke(null, [
                fixture.ExecutablePath,
                programPath,
                initializeTimeout,
                requestTimeout,
                stopTimeout,
                startupCancellation.Token,
            ]);
            var session = await AwaitAsyncResult(started, "StartAsync", startupCancellation.Token);
            Assert.NotNull(session);
            var activeReaderTask = readerTask.GetValue(session) as Task
                ?? throw new InvalidOperationException("NetCoreDbgSession._readerTask returned null.");
            await fixture.WaitForStartupAsync(startupCancellation.Token);
            return new NetCoreDbgSessionContractDriver(fixture, session, stopAsync, state, isUsable, activeReaderTask, disposeAsync);
        }
        catch
        {
            if (failedTranscript is not null)
            {
                foreach (var entry in await fixture.ReadTranscriptAsync())
                {
                    failedTranscript.Add(entry);
                }
            }

            await fixture.DisposeAsync();
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

            var sessions = await Task.WhenAll(Task.Run(StartOneAsync), Task.Run(StartOneAsync));
            return new ConcurrentNetCoreDbgSessionContractDriver(fixture, sessions);
        }
        catch
        {
            await fixture.DisposeAsync();
            throw;
        }
    }


    public async Task StopAsync(CancellationToken cancellationToken) =>
        _ = await AwaitAsyncResult(_stopAsync.Invoke(_session, [cancellationToken]), "StopAsync", cancellationToken);

    public async Task DisposeSessionAsync() =>
        _ = await AwaitAsyncResult(_disposeAsync.Invoke(_session, []), "DisposeAsync", CancellationToken.None);

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
                !HasExited(processId));
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
        _fixture.ReleaseGracefulShutdown();
        try
        {
            await DisposeSessionAsync();
        }
        finally
        {
            await _fixture.DisposeAsync();
        }
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
}

internal sealed class ConcurrentNetCoreDbgSessionContractDriver(
    FixtureProcess fixture,
    object[] sessions) : IAsyncDisposable
{
    public async ValueTask DisposeAsync()
    {
        fixture.ReleaseGracefulShutdown();
        try
        {
            await Task.WhenAll(sessions.Select(static session => ((IAsyncDisposable)session).DisposeAsync().AsTask()));
        }
        finally
        {
            await fixture.DisposeAsync();
        }
    }
}


internal sealed record DapSessionSnapshot(string? Event, string? StopReason, int? ExitCode);
internal sealed record RegistryStateProbe(string? Kind, bool TokenRetained, bool AdapterAlive);


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
    bool EnableTerminateAfterInitialization = false,
    bool ExitAfterLaunchResponse = false)
{
    public string AsEnvironmentValue() => string.Join(
        ';',
        new[]
        {
            SupportsConfigurationDone ? "--supports-configuration-done" : null,
            SupportsTerminate ? "--supports-terminate" : null,
            IgnoreGracefulShutdown ? "--ignore-graceful-shutdown" : null,
            SpawnDescendant ? "--spawn-descendant" : null,
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
            EnableTerminateAfterInitialization ? "--enable-terminate-after-initialization" : null,
            ExitAfterLaunchResponse ? "--exit-after-launch-response" : null,
        }.Where(static value => value is not null));
}

internal sealed class FixtureProcess : IAsyncDisposable
{
    private readonly string _scratchDirectory;
    private readonly string _transcriptPath;
    private readonly string _releasePath;
    private readonly string? _previousTranscript;
    private readonly string? _previousOptions;
    private readonly string? _previousRelease;
    private readonly HashSet<int> _preExistingAdapterPids;
    private readonly string _executablePath;
    private const int StartupPollAttempts = 20;
    private static readonly TimeSpan ProcessCleanupTimeout = TimeSpan.FromSeconds(1);
    private bool _adapterStartAttempted;
    private bool _disposed;

    private FixtureProcess(string scratchDirectory, string transcriptPath, string releasePath, string executablePath, string? previousTranscript, string? previousOptions, string? previousRelease, HashSet<int> preExistingAdapterPids)
    {
        _scratchDirectory = scratchDirectory;
        _transcriptPath = transcriptPath;
        _releasePath = releasePath;
        _executablePath = executablePath;
        _previousTranscript = previousTranscript;
        _previousOptions = previousOptions;
        _previousRelease = previousRelease;
        _preExistingAdapterPids = preExistingAdapterPids;
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
        File.WriteAllText(transcriptPath, string.Empty, new UTF8Encoding(encoderShouldEmitUTF8Identifier: false));

        var previousTranscript = Environment.GetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT");
        var previousOptions = Environment.GetEnvironmentVariable("CONTROLLED_DAP_OPTIONS");
        var previousRelease = Environment.GetEnvironmentVariable("CONTROLLED_DAP_GRACEFUL_RELEASE");
        var preExistingAdapterPids = ExactAdapterProcessIds(executable);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT", transcriptPath);
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_OPTIONS", configuration.AsEnvironmentValue());
        Environment.SetEnvironmentVariable("CONTROLLED_DAP_GRACEFUL_RELEASE", releasePath);
        return new FixtureProcess(scratchDirectory, transcriptPath, releasePath, executable, previousTranscript, previousOptions, previousRelease, preExistingAdapterPids);
    }
    public void MarkAdapterStartAttempted() => _adapterStartAttempted = true;

    public void ReleaseGracefulShutdown() => File.WriteAllText(_releasePath, string.Empty);

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

    public async Task<int> GetProcessIdAsync(CancellationToken cancellationToken)
    {
        return (await ReadTranscriptAsync()).LastOrDefault(static entry => entry.Kind == "startup")?.ProcessId
            ?? throw new InvalidOperationException("Controlled adapter did not record its process id.");
    }
    internal async Task TerminateAdapterAsync(CancellationToken cancellationToken = default)
    {
        var processId = await GetProcessIdAsync(cancellationToken);
        await TerminateProcessTreeAsync(processId);
    }

    internal Task TerminateProcessTreeAsync(int processId) => KillAdapterTreeAsync(processId);

    public async ValueTask DisposeAsync()
    {
        if (_disposed)
        {
            return;
        }

        _disposed = true;
        Exception? cleanupFailure = null;
        try
        {
            ReleaseGracefulShutdown();
        }
        catch (Exception exception)
        {
            cleanupFailure = exception;
        }

        try
        {
            await KillRecordedAdapterTreeAsync();
        }
        catch (Exception exception)
        {
            if (cleanupFailure is null)
            {
                cleanupFailure = exception;
            }
        }
        finally
        {
            try
            {
                await DeleteScratchDirectoryAsync();
            }
            catch (Exception exception) when (cleanupFailure is null)
            {
                cleanupFailure = exception;
            }
            finally
            {
                Environment.SetEnvironmentVariable("CONTROLLED_DAP_TRANSCRIPT", _previousTranscript);
                Environment.SetEnvironmentVariable("CONTROLLED_DAP_OPTIONS", _previousOptions);
                Environment.SetEnvironmentVariable("CONTROLLED_DAP_GRACEFUL_RELEASE", _previousRelease);
            }
        }

        if (cleanupFailure is not null)
        {
            throw cleanupFailure;
        }
    }

    private async Task KillRecordedAdapterTreeAsync()
    {
        var processId = _adapterStartAttempted ? await WaitForRecordedProcessIdAsync() : null;
        var processIds = processId is { } recorded
            ? [recorded]
            : _adapterStartAttempted
                ? ExactAdapterProcessIds(ExecutablePath).Except(_preExistingAdapterPids).ToArray()
                : [];
        foreach (var ownedProcessId in processIds)
        {
            await KillAdapterTreeAsync(ownedProcessId);
        }
    }

    private static async Task KillAdapterTreeAsync(int processId)
    {
        try
        {
            using var process = Process.GetProcessById(processId);
            if (process.HasExited)
            {
                return;
            }

            process.Kill(entireProcessTree: true);
            using var cancellation = new CancellationTokenSource(ProcessCleanupTimeout);
            await process.WaitForExitAsync(cancellation.Token);
            if (!process.HasExited)
            {
                throw new InvalidOperationException($"Owned controlled adapter process tree '{processId}' could not be removed.");
            }
        }
        catch (ArgumentException)
        {
            // The owned adapter already exited.
        }
        catch (OperationCanceledException)
        {
            throw new InvalidOperationException($"Owned controlled adapter process tree '{processId}' could not be removed within {ProcessCleanupTimeout}.");
        }
    }

    private async Task<int?> WaitForRecordedProcessIdAsync()
    {
        for (var attempt = 0; attempt < StartupPollAttempts; attempt++)
        {
            try
            {
                var processId = ReadTranscriptLinesSnapshot()
                    .Select(FixtureTranscriptEntry.Parse)
                    .LastOrDefault(static entry => entry.Kind == "startup")
                    ?.ProcessId;
                if (processId is not null)
                {
                    return processId;
                }
            }
            catch (FileNotFoundException)
            {
                // The adapter may be creating its startup transcript record.
            }

            await Task.Delay(TimeSpan.FromMilliseconds(25));
        }

        return null;
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

    private static HashSet<int> ExactAdapterProcessIds(string executablePath)
    {
        var processIds = new HashSet<int>();
        foreach (var process in Process.GetProcesses())
        {
            using (process)
            {
                try
                {
                    if (string.Equals(process.MainModule?.FileName, executablePath, StringComparison.OrdinalIgnoreCase))
                    {
                        processIds.Add(process.Id);
                    }
                }
                catch (InvalidOperationException)
                {
                    // Process exited while being inspected.
                }
                catch (System.ComponentModel.Win32Exception)
                {
                    // Another user's process cannot be inspected.
                }
            }
        }

        return processIds;
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
    int? ContentLength,
    int? PayloadByteCount,
    string? Stage,
    string? Event,
    bool? SupportsTerminateRequest)
{
    public static FixtureTranscriptEntry Parse(string json)
    {
        using var document = JsonDocument.Parse(json);
        var root = document.RootElement;
        return new FixtureTranscriptEntry(
            root.GetProperty("kind").GetString() ?? throw new InvalidDataException("Fixture transcript kind is absent."),
            root.TryGetProperty("sequence", out var sequence) ? sequence.GetInt32() : null,
            root.TryGetProperty("command", out var command) ? command.GetString() : null,
            root.TryGetProperty("arguments", out var arguments) ? arguments.GetString() : null,
            root.TryGetProperty("rawPayload", out var rawPayload) ? rawPayload.GetString() : null,
            root.TryGetProperty("processId", out var processId) ? processId.GetInt32() : null,
            root.TryGetProperty("contentLength", out var contentLength) ? contentLength.GetInt32() : null,
            root.TryGetProperty("payloadByteCount", out var payloadByteCount) ? payloadByteCount.GetInt32() : null,
            root.TryGetProperty("stage", out var stage) ? stage.GetString() : null,
            root.TryGetProperty("event", out var eventName) ? eventName.GetString() : null,
            root.TryGetProperty("supportsTerminateRequest", out var supportsTerminateRequest) ? supportsTerminateRequest.GetBoolean() : null);
    }
}

internal static class RepositoryLayout
{
    public static readonly string Root = FindRoot();
    public static readonly string ControlledAdapterDirectory = Path.Combine(Root, "host", "NetCoreDbg.Mcp.Stateless.Tests", "Fixtures", "ControlledDapAdapter");
    public static readonly string ScratchRoot = Path.Combine(Root, ".agent", "tmp", "netcoredbg-mcp-stateless-tests");

    private static string FindRoot()
    {
        for (var current = new DirectoryInfo(AppContext.BaseDirectory); current is not null; current = current.Parent)
        {
            if (Directory.Exists(Path.Combine(current.FullName, ".git")) || File.Exists(Path.Combine(current.FullName, "AGENTS.md")))
            {
                return current.FullName;
            }
        }

        throw new InvalidOperationException("Repository root could not be located from the test assembly base directory.");
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
        var targetPath = Path.Combine(projectDirectory, "bin", configuration, targetFramework, $"{assemblyName}.dll");
        Assert.True(File.Exists(targetPath), $"Built test target is absent: '{targetPath}'.");
        return targetPath;
    }
}
