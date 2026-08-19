using System.Buffers.Binary;
using System.IO.Pipes;
using System.Reflection;
using System.Runtime.Loader;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

public sealed class NativeSceneBridgeLifecycleTests
{
    private const string AuthorizationNonce = "native-scene-test-nonce";
    private const int MaximumRequestBytes = 1_024;
    private const int MaximumResponseBytes = 2_048;
    private const int FakeFrameLimit = 8_192;
    private static readonly TimeSpan ConnectTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan WriteTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan ReadTimeout = TimeSpan.FromSeconds(2);
    private static readonly TimeSpan TestTimeout = TimeSpan.FromSeconds(5);

    [Fact]
    public async Task SendAsync_SerializesConcurrentNonceAuthorizedRequests_AndWritesNothingToStdout()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var originalStdout = Console.Out;
        using var stdout = new StringWriter();
        Console.SetOut(stdout);
        try
        {
            var first = client.SendAsync(AuthorizationNonce, Request("first"), deadline.Token);
            var firstRequest = await observer.ReadRequestAsync(deadline.Token);

            Assert.Equal(AuthorizationNonce, firstRequest.Nonce);
            Assert.False(string.IsNullOrWhiteSpace(firstRequest.CorrelationId));
            Assert.NotEqual(AuthorizationNonce, firstRequest.CorrelationId);
            Assert.Equal("first", Text(firstRequest.Request["operation"]));

            var secondRead = observer.ReadRequestAsync(deadline.Token);
            var second = client.SendAsync(AuthorizationNonce, Request("second"), deadline.Token);

            await Task.Yield();
            Assert.False(secondRead.IsCompleted, "The second request reached the synchronous bridge before the first response was written.");

            await observer.WriteResponseAsync(ResponseFor(firstRequest, Result("first")), deadline.Token);
            var secondRequest = await secondRead;
            await observer.WriteResponseAsync(ResponseFor(secondRequest, Result("second")), deadline.Token);

            Assert.Equal(1, observer.RequestsObservedBeforeFirstResponse);
            AssertAvailable(await first, "first");
            AssertAvailable(await second, "second");
        }
        finally
        {
            Console.SetOut(originalStdout);
        }

        Assert.Equal(string.Empty, stdout.ToString());
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenConnectCannotBeEstablishedWithinItsBound()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var client = NativeSceneBridgeClientDriver.Create($"native-scene-missing-{Guid.NewGuid():N}");

        AssertUnavailable(await client.SendAsync(AuthorizationNonce, Request("connect"), deadline.Token));
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheRequestExceedsTheWriteFrameLimit_WithoutWritingIt()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        using var observerStop = new CancellationTokenSource();
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var observedRequest = observer.TryReadRequestUntilStoppedAsync(observerStop.Token);
        var result = await client.SendAsync(
            AuthorizationNonce,
            new JsonObject { ["operation"] = new string('x', MaximumRequestBytes) },
            deadline.Token);

        observerStop.Cancel();
        AssertUnavailable(result);
        Assert.Null(await observedRequest);
    }

    [Theory]
    [InlineData("nonce")]
    [InlineData("correlationId")]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheResponseAuthorizationOrCorrelationDoesNotMatch(string mismatchedMember)
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request(mismatchedMember), deadline.Token);
        var request = await observer.ReadRequestAsync(deadline.Token);
        var response = mismatchedMember switch
        {
            "nonce" => ResponseFor(request, Result("ignored"), nonce: "wrong-nonce"),
            "correlationId" => ResponseFor(request, Result("ignored"), correlationId: "wrong-correlation"),
            _ => throw new InvalidOperationException($"Unexpected mismatch selector '{mismatchedMember}'."),
        };

        await observer.WriteResponseAsync(response, deadline.Token);

        AssertUnavailable(await pending);
        await observer.AssertClientDisconnectedAsync(deadline.Token);
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheObserverDisconnectsBeforeResponding()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("disconnect"), deadline.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        await observer.DisposeAsync();

        AssertUnavailable(await pending);
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheResponseFrameExceedsTheReadLimit_AndClosesThePipe()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("oversized-response"), deadline.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        await observer.WriteResponseLengthAsync(MaximumResponseBytes + 1, deadline.Token);

        AssertUnavailable(await pending);
        await observer.AssertClientDisconnectedAsync(deadline.Token);
    }

    [Fact]
    public async Task SendAsync_ReturnsObserverUnavailable_WhenTheObserverNeverCompletesTheBoundedRead_AndClosesThePipe()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("read-timeout"), deadline.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        var disconnected = observer.AssertClientDisconnectedAsync(deadline.Token);

        AssertUnavailable(await pending);
        await disconnected;
    }

    [Fact]
    public async Task SendAsync_CancellationReturnsObserverUnavailable_AndClosesThePipe()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        using var cancellation = CancellationTokenSource.CreateLinkedTokenSource(deadline.Token);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("cancel"), cancellation.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        var disconnected = observer.AssertClientDisconnectedAsync(deadline.Token);
        cancellation.Cancel();

        AssertUnavailable(await pending);
        await disconnected;
    }

    [Fact]
    public async Task DisposeAsync_ClosesAnInflightPipeRequest_AndReturnsObserverUnavailable()
    {
        using var deadline = new CancellationTokenSource(TestTimeout);
        await using var observer = new LocalNamedPipeObserver();
        await using var client = NativeSceneBridgeClientDriver.Create(observer.PipeName);

        var pending = client.SendAsync(AuthorizationNonce, Request("dispose"), deadline.Token);
        _ = await observer.ReadRequestAsync(deadline.Token);
        var disconnected = observer.AssertClientDisconnectedAsync(deadline.Token);
        var disposal = client.DisposeAsync().AsTask();

        AssertUnavailable(await pending);
        await disposal;
        await disconnected;
    }

    private static JsonObject Request(string operation) => new()
    {
        ["operation"] = operation,
        ["candidate"] = new JsonObject
        {
            ["processId"] = 4242,
            ["windowHandle"] = "0x0000000000001234",
        },
    };

    private static JsonObject Result(string operation) => new()
    {
        ["operation"] = operation,
        ["state"] = "observed",
    };

    private static JsonObject ResponseFor(BridgeRequest request, JsonObject payload, string? nonce = null, string? correlationId = null) => new()
    {
        ["nonce"] = nonce ?? request.Nonce,
        ["correlationId"] = correlationId ?? request.CorrelationId,
        ["response"] = payload,
    };

    private static void AssertAvailable(NativeSceneBridgeCallResult result, string expectedOperation)
    {
        Assert.True(result.IsAvailable);
        Assert.Null(result.Code);
        Assert.NotNull(result.Payload);
        Assert.Equal(expectedOperation, Text(result.Payload!["operation"]));
        Assert.Equal("observed", Text(result.Payload["state"]));
    }

    private static void AssertUnavailable(NativeSceneBridgeCallResult result)
    {
        Assert.False(result.IsAvailable);
        Assert.Equal("OBSERVER_UNAVAILABLE", result.Code);
        Assert.Null(result.Payload);
    }

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();

    private sealed class LocalNamedPipeObserver : IAsyncDisposable
    {
        private readonly NamedPipeServerStream _server;
        private Task? _connection;
        private int _requestsObserved;

        public LocalNamedPipeObserver()
        {
            PipeName = $"native-scene-bridge-{Guid.NewGuid():N}";
            _server = new NamedPipeServerStream(
                PipeName,
                PipeDirection.InOut,
                maxNumberOfServerInstances: 1,
                PipeTransmissionMode.Byte,
                PipeOptions.Asynchronous);
        }

        public string PipeName { get; }

        public int RequestsObservedBeforeFirstResponse { get; private set; }

        public bool HasWrittenResponse { get; private set; }

        public async Task<BridgeRequest> ReadRequestAsync(CancellationToken cancellationToken)
        {
            var request = await ReadRequestOrEndAsync(cancellationToken);
            return request ?? throw new EndOfStreamException("The bridge closed its pipe before writing a complete request frame.");
        }

        public async Task<BridgeRequest?> TryReadRequestUntilStoppedAsync(CancellationToken stopToken)
        {
            try
            {
                return await ReadRequestOrEndAsync(stopToken);
            }
            catch (OperationCanceledException) when (stopToken.IsCancellationRequested)
            {
                return null;
            }
        }

        public async Task WriteResponseAsync(JsonObject response, CancellationToken cancellationToken)
        {
            HasWrittenResponse = true;
            await WriteFrameAsync(_server, JsonSerializer.SerializeToUtf8Bytes(response), cancellationToken);
        }

        public async Task WriteResponseLengthAsync(int length, CancellationToken cancellationToken)
        {
            HasWrittenResponse = true;
            var header = new byte[sizeof(int)];
            BinaryPrimitives.WriteInt32LittleEndian(header, length);
            await _server.WriteAsync(header, cancellationToken);
            await _server.FlushAsync(cancellationToken);
        }

        public async Task AssertClientDisconnectedAsync(CancellationToken cancellationToken)
        {
            var buffer = new byte[1];
            var read = await _server.ReadAsync(buffer, cancellationToken);
            Assert.Equal(0, read);
        }

        public ValueTask DisposeAsync()
        {
            _server.Dispose();
            return ValueTask.CompletedTask;
        }

        private async Task<BridgeRequest?> ReadRequestOrEndAsync(CancellationToken cancellationToken)
        {
            await EnsureConnectionAsync(cancellationToken);
            var payload = await ReadFrameOrEndAsync(_server, FakeFrameLimit, cancellationToken);
            if (payload is null)
            {
                return null;
            }

            var document = JsonNode.Parse(payload) as JsonObject
                ?? throw new InvalidDataException("The bridge request frame must be a JSON object.");
            var request = new BridgeRequest(
                Text(document["nonce"]),
                Text(document["correlationId"]),
                document["request"] as JsonObject
                    ?? throw new InvalidDataException("The bridge request frame is missing its JSON object request."));

            _requestsObserved++;
            if (!HasWrittenResponse)
            {
                RequestsObservedBeforeFirstResponse++;
            }

            return request;
        }

        private Task EnsureConnectionAsync(CancellationToken cancellationToken) =>
            _connection ??= _server.WaitForConnectionAsync(cancellationToken);
    }

    private sealed class NativeSceneBridgeClientDriver : IAsyncDisposable
    {
        private const string ProductionAssemblyName = "NetCoreDbg.Mcp.Stateless";
        private const string ClientTypeName = "NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneBridgeClient";

        private readonly IAsyncDisposable _client;
        private readonly object _instance;
        private readonly MethodInfo _sendAsync;

        private NativeSceneBridgeClientDriver(IAsyncDisposable client, object instance, MethodInfo sendAsync)
        {
            _client = client;
            _instance = instance;
            _sendAsync = sendAsync;
        }

        public static NativeSceneBridgeClientDriver Create(string pipeName)
        {
            var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(
                TestOutputPathResolver.ResolveManagedAssembly(
                    RepositoryLayout.Root,
                    Path.Combine("host", ProductionAssemblyName),
                    ProductionAssemblyName));
            var clientType = assembly.GetType(ClientTypeName, throwOnError: false)
                ?? throw new InvalidOperationException(
                    $"Missing production contract: type '{ClientTypeName}' is absent from '{assembly.Location}'. " +
                    "T017 must implement it without changing this RED suite.");
            var constructor = clientType.GetConstructor(
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                binder: null,
                types:
                [
                    typeof(string),
                    typeof(TimeSpan),
                    typeof(TimeSpan),
                    typeof(TimeSpan),
                    typeof(int),
                    typeof(int),
                ],
                modifiers: null)
                ?? throw new InvalidOperationException(
                    "NativeSceneBridgeClient must accept pipeName, connect timeout, write timeout, read timeout, maximum request bytes, and maximum response bytes.");
            var sendAsync = clientType.GetMethod(
                "SendAsync",
                BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                binder: null,
                types: [typeof(string), typeof(JsonObject), typeof(CancellationToken)],
                modifiers: null)
                ?? throw new InvalidOperationException(
                    "NativeSceneBridgeClient must expose SendAsync(string authorizationNonce, JsonObject request, CancellationToken cancellationToken).");
            var instance = constructor.Invoke(
            [
                pipeName,
                ConnectTimeout,
                WriteTimeout,
                ReadTimeout,
                MaximumRequestBytes,
                MaximumResponseBytes,
            ]);
            var client = instance as IAsyncDisposable
                ?? throw new InvalidOperationException("NativeSceneBridgeClient must implement IAsyncDisposable for bounded pipe cleanup.");

            return new NativeSceneBridgeClientDriver(client, instance, sendAsync);
        }

        public async Task<NativeSceneBridgeCallResult> SendAsync(string authorizationNonce, JsonObject request, CancellationToken cancellationToken)
        {
            var value = await AwaitResultAsync(
                _sendAsync.Invoke(_instance, [authorizationNonce, request, cancellationToken]),
                "NativeSceneBridgeClient.SendAsync");
            var resultType = value.GetType();
            var isAvailable = RequireReadableProperty(resultType, "IsAvailable", typeof(bool));
            var code = RequireReadableProperty(resultType, "Code", typeof(string));
            var payload = RequireReadableProperty(resultType, "Payload", typeof(JsonObject));

            return new NativeSceneBridgeCallResult(
                Assert.IsType<bool>(isAvailable.GetValue(value)),
                code.GetValue(value) as string,
                payload.GetValue(value) as JsonObject);
        }

        public ValueTask DisposeAsync() => _client.DisposeAsync();

        private static async Task<object> AwaitResultAsync(object? pending, string memberName)
        {
            if (pending is null)
            {
                throw new InvalidOperationException($"{memberName} returned null instead of Task<T> or ValueTask<T>.");
            }

            Task task;
            if (pending is Task directTask)
            {
                task = directTask;
            }
            else
            {
                var asTask = pending.GetType().GetMethod("AsTask", Type.EmptyTypes)
                    ?? throw new InvalidOperationException($"{memberName} must return Task<T> or ValueTask<T>.");
                task = asTask.Invoke(pending, []) as Task
                    ?? throw new InvalidOperationException($"{memberName}.AsTask() did not return a Task.");
            }

            await task.ConfigureAwait(false);
            var result = task.GetType().GetProperty("Result", BindingFlags.Instance | BindingFlags.Public)?.GetValue(task);
            return result ?? throw new InvalidOperationException($"{memberName} must return Task<T> or ValueTask<T>.");
        }

        private static PropertyInfo RequireReadableProperty(Type resultType, string name, Type expectedType)
        {
            var property = resultType.GetProperty(name, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)
                ?? throw new InvalidOperationException($"NativeSceneBridgeCallResult is missing readable {name}.");
            if (!property.CanRead || property.PropertyType != expectedType)
            {
                throw new InvalidOperationException($"NativeSceneBridgeCallResult.{name} must be readable {expectedType.Name}.");
            }

            return property;
        }
    }

    private sealed record BridgeRequest(string Nonce, string CorrelationId, JsonObject Request);

    private sealed record NativeSceneBridgeCallResult(bool IsAvailable, string? Code, JsonObject? Payload);

    private static async Task<byte[]?> ReadFrameOrEndAsync(Stream stream, int maximumPayloadBytes, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        var firstRead = await stream.ReadAsync(header.AsMemory(0, 1), cancellationToken);
        if (firstRead == 0)
        {
            return null;
        }

        await ReadExactlyAsync(stream, header.AsMemory(firstRead), cancellationToken);
        var length = BinaryPrimitives.ReadInt32LittleEndian(header);
        if (length <= 0 || length > maximumPayloadBytes)
        {
            throw new InvalidDataException($"Pipe frame length {length} is outside 1..{maximumPayloadBytes}.");
        }

        var payload = new byte[length];
        await ReadExactlyAsync(stream, payload, cancellationToken);
        return payload;
    }

    private static async Task WriteFrameAsync(Stream stream, byte[] payload, CancellationToken cancellationToken)
    {
        var header = new byte[sizeof(int)];
        BinaryPrimitives.WriteInt32LittleEndian(header, payload.Length);
        await stream.WriteAsync(header, cancellationToken);
        await stream.WriteAsync(payload, cancellationToken);
        await stream.FlushAsync(cancellationToken);
    }

    private static async Task ReadExactlyAsync(Stream stream, Memory<byte> buffer, CancellationToken cancellationToken)
    {
        var offset = 0;
        while (offset < buffer.Length)
        {
            var read = await stream.ReadAsync(buffer[offset..], cancellationToken);
            if (read == 0)
            {
                throw new EndOfStreamException("Pipe frame ended before all declared bytes were received.");
            }

            offset += read;
        }
    }
}
