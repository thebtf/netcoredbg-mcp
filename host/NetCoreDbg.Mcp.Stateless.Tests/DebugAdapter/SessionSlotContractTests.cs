using System.Reflection;
using System.Runtime.Loader;
using System.Runtime.CompilerServices;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class SessionSlotContractTests
{
    private const string ProductionAssemblyName = "NetCoreDbg.Mcp.Stateless";
    private const string RegistryTypeName = "NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry";
    private const string SessionTypeName = "NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession";

    [Fact]
    public void NetCoreDbgSession_GetThreadsAsync_IsAnInternalTypedOperation()
    {
        // Arrange
        var sessionType = ProductionAssembly().GetType(SessionTypeName, throwOnError: false);

        // Act
        var method = Assert.Single(Assert.IsAssignableFrom<Type>(sessionType).GetMethods(
            BindingFlags.Instance | BindingFlags.NonPublic | BindingFlags.DeclaredOnly),
            static candidate => candidate.Name == "GetThreadsAsync");

        // Assert
        Assert.True(method.IsAssembly, "GetThreadsAsync must remain internal to the native debug-session boundary.");
        Assert.Equal([typeof(CancellationToken)], method.GetParameters().Select(static parameter => parameter.ParameterType));
        var resultType = UnwrapAsyncResult(method.ReturnType);
        Assert.False(resultType.IsPublic || resultType.IsNestedPublic, "The typed thread operation result must not become a public DAP payload surface.");
        Assert.True(resultType.IsValueType || resultType.IsSealed, "The typed thread operation result must be immutable.");
        Assert.All(resultType.GetProperties(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic), static property =>
            Assert.True(IsImmutable(property), $"{property.Name} must not have a mutable public or internal setter."));
    }

    [Fact]
    public void DebugSessionRegistry_SessionSlotOwnsAdmissionAndSharedCloseDrain()
    {
        // Arrange
        var registryType = ProductionAssembly().GetType(RegistryTypeName, throwOnError: false);

        // Act
        var slotType = Assert.IsAssignableFrom<Type>(registryType).GetNestedType("SessionSlot", BindingFlags.NonPublic);

        // Assert
        Assert.NotNull(slotType);
        Assert.True(slotType!.IsNestedPrivate, "SessionSlot must remain registry-owned lifecycle state.");
        var methods = slotType.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly);
        Assert.Contains(methods, static method => method.Name == "TryAcquire");
        var close = Assert.Single(methods, static method => method.Name == "CloseAndDrainAsync");
        Assert.True(IsTaskLike(close.ReturnType), "CloseAndDrainAsync must provide the one shared cleanup completion.");
        Assert.DoesNotContain(methods, static method =>
            method.GetParameters().Any(static parameter => parameter.ParameterType == typeof(string)) &&
            method.Name.Contains("Request", StringComparison.Ordinal));
    }

    [Fact]
    public async Task SessionSlot_FaultingStop_DisposesOnceAfterReleasedLeaseAndRemoval()
    {
        // Arrange
        var registryType = ProductionAssembly().GetType(RegistryTypeName, throwOnError: false);
        var slotType = Assert.IsAssignableFrom<Type>(Assert.IsAssignableFrom<Type>(registryType)
            .GetNestedType("SessionSlot", BindingFlags.NonPublic));
        var stopCalls = 0;
        var disposeCalls = 0;
        var removalCalls = 0;
        Func<CancellationToken, Task> stop = _ =>
        {
            stopCalls++;
            return Task.FromException(new InvalidOperationException("controlled stop fault"));
        };
        Func<ValueTask> dispose = () =>
        {
            disposeCalls++;
            return ValueTask.CompletedTask;
        };
        Action remove = () => removalCalls++;
        var constructor = Assert.Single(slotType.GetConstructors(BindingFlags.Instance | BindingFlags.NonPublic), candidate =>
        {
            var parameters = candidate.GetParameters();
            return parameters.Any(static parameter => parameter.ParameterType == typeof(Func<CancellationToken, Task>)) &&
                parameters.Any(static parameter => parameter.ParameterType == typeof(Func<ValueTask>)) &&
                parameters.Any(static parameter => parameter.ParameterType == typeof(Action));
        });
        var slot = constructor.Invoke(constructor.GetParameters().Select(parameter => ConstructorArgument(parameter, stop, dispose, remove)).ToArray());
        var acquire = Assert.Single(slotType.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly), static method => method.Name == "TryAcquire");
        var lease = Invoke(acquire, slot, stop, dispose, remove);
        Assert.NotNull(lease);
        await ReleaseLeaseAsync(lease!);
        var close = Assert.Single(slotType.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly), static method => method.Name == "CloseAndDrainAsync");

        // Act
        var closing = Invoke(close, slot, stop, dispose, remove);

        // Assert
        await Assert.ThrowsAsync<InvalidOperationException>(() => AwaitTaskLikeAsync(closing));
        Assert.Equal(1, removalCalls);
        Assert.Equal(1, stopCalls);
        Assert.Equal(1, disposeCalls);
    }

    [Fact]
    public async Task SessionSlot_DrainDeadline_PassesCancelledAbortTokenToStop()
    {
        // Arrange
        var registryType = ProductionAssembly().GetType(RegistryTypeName, throwOnError: false);
        var slotType = Assert.IsAssignableFrom<Type>(Assert.IsAssignableFrom<Type>(registryType)
            .GetNestedType("SessionSlot", BindingFlags.NonPublic));
        CancellationToken? stopToken = null;
        Func<CancellationToken, Task> stop = token =>
        {
            stopToken = token;
            return Task.CompletedTask;
        };
        var constructor = Assert.Single(slotType.GetConstructors(BindingFlags.Instance | BindingFlags.NonPublic), candidate =>
        {
            var parameters = candidate.GetParameters();
            return parameters.Any(static parameter => parameter.ParameterType == typeof(Func<CancellationToken, Task>)) &&
                parameters.Any(static parameter => parameter.ParameterType == typeof(Func<ValueTask>)) &&
                parameters.Any(static parameter => parameter.ParameterType == typeof(Action));
        });
        var slot = constructor.Invoke(constructor.GetParameters().Select(parameter => ConstructorArgument(parameter, stop, () => ValueTask.CompletedTask, static () => { })).ToArray());
        var acquire = Assert.Single(slotType.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly), static method => method.Name == "TryAcquire");
        var lease = Invoke(acquire, slot, stop, () => ValueTask.CompletedTask, static () => { });
        var abortToken = Assert.IsType<CancellationToken>(Assert.Single(lease!.GetType().GetProperties(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic), static property => property.Name == "AbortToken").GetValue(lease));
        using var releaseOnAbort = abortToken.Register(static state => ((IDisposable)state!).Dispose(), lease);
        var close = Assert.Single(slotType.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly), static method => method.Name == "CloseAndDrainAsync");

        // Act
        await AwaitTaskLikeAsync(Invoke(close, slot, stop, () => ValueTask.CompletedTask, static () => { })).WaitAsync(TimeSpan.FromSeconds(2));

        // Assert
        Assert.True(stopToken is { CanBeCanceled: true, IsCancellationRequested: true });
    }

    [Fact]
    public async Task SessionSlot_LateHostDisposalJoinAfterExplicitCleanup_ReturnsSharedCloseTask()
    {
        // A host-disposal snapshot can retain this slot after an explicit closer has
        // completed disposal. Its already-cancelled shutdown token must only join.
        var registryType = ProductionAssembly().GetType(RegistryTypeName, throwOnError: false);
        var slotType = Assert.IsAssignableFrom<Type>(Assert.IsAssignableFrom<Type>(registryType)
            .GetNestedType("SessionSlot", BindingFlags.NonPublic));
        var stopCalls = 0;
        var disposeCalls = 0;
        var removeCalls = 0;
        Func<CancellationToken, Task> stop = _ =>
        {
            stopCalls++;
            return Task.CompletedTask;
        };
        Func<ValueTask> dispose = () =>
        {
            disposeCalls++;
            return ValueTask.CompletedTask;
        };
        Action remove = () => removeCalls++;
        var constructor = Assert.Single(slotType.GetConstructors(BindingFlags.Instance | BindingFlags.NonPublic), candidate =>
        {
            var parameters = candidate.GetParameters();
            return parameters.Any(static parameter => parameter.ParameterType == typeof(Func<CancellationToken, Task>)) &&
                parameters.Any(static parameter => parameter.ParameterType == typeof(Func<ValueTask>)) &&
                parameters.Any(static parameter => parameter.ParameterType == typeof(Action));
        });
        var slot = constructor.Invoke(constructor.GetParameters().Select(parameter => ConstructorArgument(parameter, stop, dispose, remove)).ToArray());
        var close = Assert.Single(slotType.GetMethods(BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic | BindingFlags.DeclaredOnly), static method => method.Name == "CloseAndDrainAsync");
        var explicitClose = Assert.IsAssignableFrom<Task>(Invoke(close, slot, stop, dispose, remove));
        await explicitClose.WaitAsync(TimeSpan.FromSeconds(2));
        using var hostDisposal = new CancellationTokenSource();
        hostDisposal.Cancel();

        // Act
        var joined = Assert.IsAssignableFrom<Task>(Invoke(close, slot, stop, dispose, remove, hostDisposal.Token));

        // Assert
        Assert.Same(explicitClose, joined);
        await joined.WaitAsync(TimeSpan.FromSeconds(2));
        Assert.Equal(1, stopCalls);
        Assert.Equal(1, disposeCalls);
        Assert.Equal(1, removeCalls);
    }

    private static object? ConstructorArgument(
        ParameterInfo parameter,
        Func<CancellationToken, Task> stop,
        Func<ValueTask> dispose,
        Action remove) => parameter.ParameterType switch
    {
        var type when type == typeof(Func<CancellationToken, Task>) => stop,
        var type when type == typeof(Func<ValueTask>) => dispose,
        var type when type == typeof(Action) => remove,
        var type when type == typeof(TimeSpan) => TimeSpan.FromMilliseconds(100),
        var type when type == typeof(CancellationToken) => CancellationToken.None,
        var type when type == typeof(string) => "faulting-stop-token",
        var type when type == typeof(bool) => false,
        var type when type.IsEnum => Enum.GetValues(type).GetValue(0),
        _ => throw new Xunit.Sdk.XunitException($"SessionSlot constructor parameter '{parameter.Name}' does not have a test-safe default."),
    };

    private static object? Invoke(
        MethodInfo method,
        object target,
        Func<CancellationToken, Task> stop,
        Func<ValueTask> dispose,
        Action remove,
        CancellationToken cancellationToken = default)
    {
        var arguments = method.GetParameters()
            .Select(parameter => parameter.IsOut
                ? null
                : parameter.ParameterType == typeof(CancellationToken)
                    ? cancellationToken
                    : ConstructorArgument(parameter, stop, dispose, remove))
            .ToArray();
        var result = method.Invoke(target, arguments);
        return result ?? arguments
            .Where((_, index) => method.GetParameters()[index].IsOut)
            .Select(static argument => argument)
            .SingleOrDefault();
    }

    private static async Task ReleaseLeaseAsync(object lease)
    {
        switch (lease)
        {
            case IAsyncDisposable asyncDisposable:
                await asyncDisposable.DisposeAsync();
                return;
            case IDisposable disposable:
                disposable.Dispose();
                return;
        }

        var release = lease.GetType().GetMethod("Release", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic);
        Assert.NotNull(release);
        await AwaitTaskLikeAsync(release!.Invoke(lease, []));
    }

    private static async Task AwaitTaskLikeAsync(object? taskLike)
    {
        switch (taskLike)
        {
            case Task task:
                await task;
                return;
            case ValueTask valueTask:
                await valueTask;
                return;
        }

        var asTask = taskLike?.GetType().GetMethod("AsTask", BindingFlags.Instance | BindingFlags.Public, Type.EmptyTypes);
        Assert.NotNull(asTask);
        await Assert.IsAssignableFrom<Task>(asTask!.Invoke(taskLike, []));
    }

    private static Assembly ProductionAssembly() => AssemblyLoadContext.Default.LoadFromAssemblyPath(
        TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root,
            Path.Combine("host", ProductionAssemblyName),
            ProductionAssemblyName));

    private static Type UnwrapAsyncResult(Type returnType)
    {
        if (returnType.IsGenericType &&
            (returnType.GetGenericTypeDefinition() == typeof(Task<>) || returnType.GetGenericTypeDefinition() == typeof(ValueTask<>)))
        {
            return returnType.GetGenericArguments()[0];
        }

        throw new Xunit.Sdk.XunitException("GetThreadsAsync must return Task<T> or ValueTask<T> for its typed operation result.");
    }

    private static bool IsImmutable(PropertyInfo property) =>
        property.SetMethod is null ||
        property.SetMethod.IsPrivate ||
        property.SetMethod.ReturnParameter.GetRequiredCustomModifiers().Contains(typeof(IsExternalInit));

    private static bool IsTaskLike(Type type) =>
        typeof(Task).IsAssignableFrom(type) ||
        type == typeof(ValueTask) ||
        (type.IsGenericType && type.GetGenericTypeDefinition() == typeof(ValueTask<>));
}
