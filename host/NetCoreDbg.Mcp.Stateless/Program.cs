using System.Diagnostics;
using System.Collections.Concurrent;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Extensions.Logging;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.NativeScene;

namespace NetCoreDbg.Mcp.Stateless;

internal static class Program
{
    private const string ProtocolVersion = "2026-07-28";
    private const string UnixProcessGroupProxy = "--unix-process-group-proxy";
    private static readonly TimeSpan CacheLifetime = TimeSpan.FromMinutes(5);

    private static async Task Main(string[] arguments)
    {
        if (!OperatingSystem.IsWindows()
            && arguments.Length == 3
            && string.Equals(arguments[0], UnixProcessGroupProxy, StringComparison.Ordinal)
            && !string.IsNullOrWhiteSpace(arguments[1])
            && !string.IsNullOrWhiteSpace(arguments[2]))
        {
            await RunUnixProcessGroupProxyAsync(arguments[1], arguments[2]).ConfigureAwait(false);
            return;
        }

        var sessions = new DebugSessionRegistry(
            Environment.GetEnvironmentVariable("NETCOREDBG_PATH"),
            Environment.GetEnvironmentVariable("FLAUI_BRIDGE_PATH"),
            Environment.GetEnvironmentVariable("NETCOREDBG_MCP_ARTIFACT_ROOT"));
        var builder = Host.CreateApplicationBuilder();
        builder.Logging.ClearProviders();
        builder.Logging.AddConsole(options => options.LogToStandardErrorThreshold = LogLevel.Trace);
        builder.Services.AddSingleton(sessions);
        builder.Services.AddSingleton<IHostedService>(new SessionDisposer(sessions));

        builder.Services.AddMcpServer(options =>
            {
                options.ProtocolVersion = ProtocolVersion;
                options.ServerInfo = new Implementation { Name = "netcoredbg-mcp-stateless", Version = "1.0.0" };
                options.Capabilities = new ServerCapabilities { Tools = new ToolsCapability { ListChanged = false } };
            })
            .WithStdioServerTransport()
            .WithListToolsHandler((context, cancellationToken) =>
                ValueTask.FromResult(ToolCatalog.List()))
            .WithCallToolHandler((context, cancellationToken) =>
                sessions.CallAsync(context, cancellationToken))
            .WithMessageFilters(filters => filters.AddOutgoingFilter(next => async (context, cancellationToken) =>
            {
                if (context.JsonRpcMessage is JsonRpcResponse { Result: JsonObject result }
                    && result.ContainsKey("supportedVersions")
                    && result.ContainsKey("capabilities"))
                {
                    result["ttlMs"] = (long)CacheLifetime.TotalMilliseconds;
                    result["cacheScope"] = "public";
                }

                await next(context, cancellationToken).ConfigureAwait(false);
            }));

        using var host = builder.Build();
        await host.RunAsync().ConfigureAwait(false);
    }

    private static async Task RunUnixProcessGroupProxyAsync(string debuggerPath, string terminationControlHandle)
    {
        UnixProcessGroup.BecomeOwnProcessGroup();
        using var terminationControl = new System.IO.Pipes.AnonymousPipeClientStream(
            System.IO.Pipes.PipeDirection.In,
            terminationControlHandle);
        using var debugger = Process.Start(new ProcessStartInfo
        {
            FileName = debuggerPath,
            UseShellExecute = false,
            CreateNoWindow = true,
            ArgumentList = { "--interpreter=vscode" },
        }) ?? throw new InvalidOperationException($"Could not start debugger '{debuggerPath}'.");
        var debuggerExit = debugger.WaitForExitAsync();
        var terminationControlBuffer = new byte[1];
        var terminationRequested = terminationControl.ReadAsync(terminationControlBuffer).AsTask();
        if (await Task.WhenAny(debuggerExit, terminationRequested).ConfigureAwait(false) == terminationRequested)
        {
            if (await terminationRequested.ConfigureAwait(false) != 0)
            {
                await debuggerExit.ConfigureAwait(false);
            }

            UnixProcessGroup.TerminateOwnProcessGroup();
            return;
        }

        await debuggerExit.ConfigureAwait(false);
        await terminationRequested.ConfigureAwait(false);
        UnixProcessGroup.TerminateOwnProcessGroup();
    }


    private sealed class DebugSessionRegistry : IAsyncDisposable
    {
        private const string StartDebug = "start_debug";
        private const string GetDebugState = "get_debug_state";
        private const string StopDebug = "stop_debug";
        private const string InputRequestId = "start_debug_program";
        private static readonly TimeSpan InitializeTimeout = TimeSpan.FromSeconds(10);
        private static readonly TimeSpan RequestTimeout = TimeSpan.FromSeconds(30);
        private static readonly TimeSpan StopTimeout = TimeSpan.FromSeconds(1);

        private readonly ConcurrentDictionary<string, NetCoreDbgSession> _sessions = new(StringComparer.Ordinal);
        private readonly ConcurrentDictionary<string, NativeSceneSessionBinding> _nativeSceneBindings = new(StringComparer.Ordinal);
        private readonly string? _debuggerPath;
        private readonly string? _bridgePath;
        private readonly string? _artifactRoot;
        private readonly Func<NetCoreDbgSession, bool> _isUsable;
        private readonly Func<NetCoreDbgSession, ValueTask> _dispose;

        internal DebugSessionRegistry(string? debuggerPath)
            : this(
                debuggerPath,
                Environment.GetEnvironmentVariable("FLAUI_BRIDGE_PATH"),
                Environment.GetEnvironmentVariable("NETCOREDBG_MCP_ARTIFACT_ROOT"))
        {
        }

        internal DebugSessionRegistry(string? debuggerPath, string? bridgePath, string? artifactRoot)
            : this(debuggerPath, bridgePath, artifactRoot, static session => session.IsUsable)
        {
        }

        private DebugSessionRegistry(string? debuggerPath, Func<NetCoreDbgSession, bool> isUsable)
            : this(debuggerPath, bridgePath: null, artifactRoot: null, isUsable)
        {
        }

        private DebugSessionRegistry(
            string? debuggerPath,
            Func<NetCoreDbgSession, bool> isUsable,
            Func<NetCoreDbgSession, ValueTask> dispose)
            : this(debuggerPath, bridgePath: null, artifactRoot: null, isUsable, dispose)
        {
        }

        private DebugSessionRegistry(
            string? debuggerPath,
            string? bridgePath,
            string? artifactRoot,
            Func<NetCoreDbgSession, bool> isUsable)
            : this(debuggerPath, bridgePath, artifactRoot, isUsable, static session => session.DisposeAsync())
        {
        }

        private DebugSessionRegistry(
            string? debuggerPath,
            string? bridgePath,
            string? artifactRoot,
            Func<NetCoreDbgSession, bool> isUsable,
            Func<NetCoreDbgSession, ValueTask> dispose)
        {
            _debuggerPath = debuggerPath;
            _bridgePath = bridgePath;
            _artifactRoot = artifactRoot;
            _isUsable = isUsable;
            _dispose = dispose;
        }

        internal async ValueTask<CallToolResult> CallAsync(
            RequestContext<CallToolRequestParams> context,
            CancellationToken cancellationToken)
        {
            var request = context.Params;
            return request.Name switch
            {
                StartDebug => await StartAsync(context, cancellationToken).ConfigureAwait(false),
                GetDebugState => await GetStateAsync(request, cancellationToken).ConfigureAwait(false),
                StopDebug => await StopAsync(request, cancellationToken).ConfigureAwait(false),
                _ when IsNativeSceneTool(request.Name) => await NativeSceneToolDispatcher.DispatchAsync(
                    request.Name,
                    request.Arguments,
                    ResolveNativeSceneBindingAsync,
                    cancellationToken).ConfigureAwait(false),
                _ => UnknownTool(request.Name),
            };
        }

        private async ValueTask<CallToolResult> StartAsync(
            RequestContext<CallToolRequestParams> context,
            CancellationToken cancellationToken)
        {
            if (!TryReadProgram(context.Params.Arguments, out var program, out var hasProgram))
            {
                return InvalidArguments(StartDebug);
            }

            if (!hasProgram)
            {
                program = ReadElicitedProgram(context.Params.InputResponses, out var hasElicitedResponse);
                if (program is null)
                {
                    if (hasElicitedResponse || !context.Server.IsMrtrSupported || context.Server.ClientCapabilities?.Elicitation?.Form is null)
                    {
                        return Error("start_debug_input_unavailable", "START_DEBUG_PROGRAM_REQUIRED");
                    }

                    throw new InputRequiredException(new Dictionary<string, InputRequest>
                    {
                        [InputRequestId] = InputRequest.ForElicitation(new ElicitRequestParams
                        {
                            Mode = "form",
                            Message = "Provide the program to debug.",
                            RequestedSchema = new ElicitRequestParams.RequestSchema
                            {
                                Properties = new Dictionary<string, ElicitRequestParams.PrimitiveSchemaDefinition>
                                {
                                    ["program"] = new ElicitRequestParams.StringSchema
                                    {
                                        Description = "Path to the program to debug.",
                                        MinLength = 1,
                                    },
                                },
                                Required = ["program"],
                            },
                        }),
                    });
                }
            }

            if (string.IsNullOrWhiteSpace(_debuggerPath))
            {
                return Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
            }

            NetCoreDbgSession? session = null;
            string? registeredToken = null;
            try
            {
                session = await NetCoreDbgSession.StartAsync(
                    _debuggerPath,
                    program!,
                    InitializeTimeout,
                    RequestTimeout,
                    StopTimeout,
                    cancellationToken).ConfigureAwait(false);
                while (true)
                {
                    var token = CreateToken();
                    var binding = new NativeSceneSessionBinding(session, token, _bridgePath, _artifactRoot);
                    if (!_sessions.TryAdd(token, session))
                    {
                        await binding.DisposeAsync().ConfigureAwait(false);
                        continue;
                    }

                    if (_nativeSceneBindings.TryAdd(token, binding))
                    {
                        registeredToken = token;
                        return Success("start_debug_success", token, session.State);
                    }

                    await binding.DisposeAsync().ConfigureAwait(false);
                    _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(token, session));
                }
            }
            catch (Exception) when (!cancellationToken.IsCancellationRequested)
            {
                if (registeredToken is not null)
                {
                    if (_nativeSceneBindings.TryRemove(registeredToken, out var binding))
                    {
                        await binding.DisposeAsync().ConfigureAwait(false);
                    }

                    _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(registeredToken, session!));
                }

                if (session is not null)
                {
                    try
                    {
                        await _dispose(session).ConfigureAwait(false);
                    }
                    catch (Exception)
                    {
                    }
                }

                return Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");
            }
        }

        private async ValueTask<CallToolResult> GetStateAsync(
            CallToolRequestParams request,
            CancellationToken cancellationToken)
        {
            if (!TryReadSessionId(request.Arguments, out var sessionId, out var hasSessionId))
            {
                return InvalidArguments(GetDebugState);
            }

            if (!hasSessionId)
            {
                return NotFound();
            }

            if (!_sessions.TryGetValue(sessionId!, out var session))
            {
                await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
                return NotFound();
            }

            var isUsable = _isUsable(session);

            if (!isUsable && _sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(sessionId!, session)))
            {
                await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
                await DisposeRemovedSessionAsync(session).ConfigureAwait(false);
                return NotFound();
            }

            return isUsable
                ? Success("debug_state_success", sessionId!, session.State)
                : NotFound();
        }

        private async ValueTask<CallToolResult> StopAsync(
            CallToolRequestParams request,
            CancellationToken cancellationToken)
        {
            if (!TryReadSessionId(request.Arguments, out var sessionId, out var hasSessionId))
            {
                return InvalidArguments(StopDebug);
            }

            if (!hasSessionId)
            {
                return NotFound();
            }

            if (!_sessions.TryRemove(sessionId!, out var session))
            {
                await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);
                return NotFound();
            }

            await RemoveNativeSceneBindingAsync(sessionId!).ConfigureAwait(false);

            try
            {
                await session.StopAsync(cancellationToken).ConfigureAwait(false);
                return StopSuccess(session.State);
            }
            catch (Exception) when (!cancellationToken.IsCancellationRequested)
            {
                return NotFound();
            }
        }

        public ValueTask DisposeAsync() => DisposeAsync(CancellationToken.None);

        public async ValueTask DisposeAsync(CancellationToken cancellationToken)
        {
            var sessions = _sessions.ToArray();
            _sessions.Clear();
            var bindings = _nativeSceneBindings.ToArray();
            _nativeSceneBindings.Clear();
            await Task.WhenAll(bindings.Select(static binding => binding.Value.DisposeAsync().AsTask())).ConfigureAwait(false);
            await Task.WhenAll(sessions.Select(session => session.Value.StopAsync(cancellationToken))).ConfigureAwait(false);
        }


        private static bool IsNativeSceneTool(string tool) => NativeSceneToolDispatcher.ListTools()
            .Any(candidate => StringComparer.Ordinal.Equals(candidate.Name, tool));

        private async ValueTask<NativeSceneSessionBinding?> ResolveNativeSceneBindingAsync(string sessionId)
        {
            if (!_sessions.TryGetValue(sessionId, out var session))
            {
                await RemoveNativeSceneBindingAsync(sessionId).ConfigureAwait(false);
                return null;
            }

            if (!_isUsable(session))
            {
                if (_sessions.TryRemove(new KeyValuePair<string, NetCoreDbgSession>(sessionId, session)))
                {
                    await RemoveNativeSceneBindingAsync(sessionId).ConfigureAwait(false);
                    await DisposeRemovedSessionAsync(session).ConfigureAwait(false);
                }

                return null;
            }

            return _nativeSceneBindings.TryGetValue(sessionId, out var binding) ? binding : null;
        }

        private async ValueTask RemoveNativeSceneBindingAsync(string sessionId)
        {
            if (_nativeSceneBindings.TryRemove(sessionId, out var binding))
            {
                await binding.DisposeAsync().ConfigureAwait(false);
            }
        }

        private async Task DisposeRemovedSessionAsync(NetCoreDbgSession session)
        {
            try
            {
                await _dispose(session).ConfigureAwait(false);
            }
            catch (Exception)
            {
            }
        }

        private static bool TryReadProgram(
            IDictionary<string, JsonElement>? arguments,
            out string? program,
            out bool hasProgram)
        {
            program = null;
            hasProgram = false;
            if (arguments is null)
            {
                return true;
            }

            if (arguments.Count > 1 || arguments.Keys.Any(static name => name != "program"))
            {
                return false;
            }

            if (!arguments.TryGetValue("program", out var element))
            {
                return true;
            }

            hasProgram = true;
            if (element.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(program = element.GetString()))
            {
                return false;
            }

            return true;
        }

        private static bool TryReadSessionId(
            IDictionary<string, JsonElement>? arguments,
            out string? sessionId,
            out bool hasSessionId)
        {
            sessionId = null;
            hasSessionId = false;
            if (arguments is null)
            {
                return true;
            }

            if (arguments.Count > 1 || arguments.Keys.Any(static name => name != "debugSessionId"))
            {
                return false;
            }

            if (!arguments.TryGetValue("debugSessionId", out var element))
            {
                return true;
            }

            hasSessionId = true;
            if (element.ValueKind != JsonValueKind.String || string.IsNullOrWhiteSpace(sessionId = element.GetString()))
            {
                return false;
            }

            return true;
        }

        private static string? ReadElicitedProgram(IDictionary<string, InputResponse>? responses, out bool hasResponse)
        {
            if (responses is null || !responses.TryGetValue(InputRequestId, out var response))
            {
                hasResponse = false;
                return null;
            }

            hasResponse = true;

            var result = response.Deserialize(InputResponse.ElicitResultJsonTypeInfo);
            return result is { IsAccepted: true, Content: { } content }
                && content.TryGetValue("program", out var program)
                && program.ValueKind == JsonValueKind.String
                && !string.IsNullOrWhiteSpace(program.GetString())
                    ? program.GetString()
                    : null;
        }

        private static string CreateToken()
        {
            Span<byte> bytes = stackalloc byte[32];
            RandomNumberGenerator.Fill(bytes);
            var base64 = Convert.ToBase64String(bytes);
            CryptographicOperations.ZeroMemory(bytes);
            return base64.TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }

        private static CallToolResult Success(string kind, string sessionId, DapSessionState state) => Result(new
        {
            kind,
            debugSessionId = sessionId,
            state = LifecycleState(state),
        }, isError: false);

        private static CallToolResult StopSuccess(DapSessionState state) => Result(new
        {
            kind = "stop_debug_success",
            state = LifecycleState(state),
        }, isError: false);

        private static object LifecycleState(DapSessionState state) => new
        {
            @event = state.Event,
            stopReason = state.StopReason,
            exitCode = state.ExitCode,
        };

        private static CallToolResult NotFound() => Error("debug_session_not_found", "DEBUG_SESSION_NOT_FOUND");

        private static CallToolResult InvalidArguments(string tool) => Result(new
        {
            kind = "invalid_tool_arguments",
            error = "INVALID_TOOL_ARGUMENTS",
            tool,
        }, isError: true);

        private static CallToolResult Error(string kind, string error) => Result(new { kind, error }, isError: true);
        private static CallToolResult UnknownTool(string tool) => new()
        {
            ResultType = "complete",
            IsError = true,
            Content = [new TextContentBlock { Text = $"Unknown tool: {tool}" }],
        };


        private static CallToolResult Result<T>(T content, bool isError) => new()
        {
            ResultType = "complete",
            IsError = isError,
            Content = [new TextContentBlock { Text = JsonSerializer.Serialize(content) }],
            StructuredContent = JsonSerializer.SerializeToElement(content),
        };
    }

    private static class ToolCatalog
    {
        internal static ListToolsResult List() => new()
        {
            Tools =
            [
                Tool("start_debug", "Start debugging a program.", "{\"type\":\"object\",\"properties\":{\"program\":{\"type\":\"string\",\"minLength\":1}},\"additionalProperties\":false}"),
                Tool("get_debug_state", "Get the state of a debug session.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":32}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                Tool("stop_debug", "Stop a debug session.", "{\"type\":\"object\",\"properties\":{\"debugSessionId\":{\"type\":\"string\",\"minLength\":32}},\"required\":[\"debugSessionId\"],\"additionalProperties\":false}"),
                .. NativeSceneToolDispatcher.ListTools(),
            ],
            TimeToLive = CacheLifetime,
            CacheScope = CacheScope.Public,
        };

        private static Tool Tool(string name, string description, string schema) => new()
        {
            Name = name,
            Description = description,
            InputSchema = JsonDocument.Parse(schema).RootElement.Clone(),
        };
    }

    private sealed class SessionDisposer(DebugSessionRegistry sessions) : IHostedService
    {
        public Task StartAsync(CancellationToken cancellationToken) => Task.CompletedTask;

        public Task StopAsync(CancellationToken cancellationToken) => sessions.DisposeAsync(cancellationToken).AsTask();
    }
}
