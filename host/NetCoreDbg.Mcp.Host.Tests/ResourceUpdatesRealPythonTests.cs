using System.Collections.Concurrent;
using System.Diagnostics;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using ModelContextProtocol;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Host.Tests;

[Collection("SequentialRealPythonProcess")]
public sealed class ResourceUpdatesRealPythonTests
{
    private const string StateUri = "debug://state";
    private const string BreakpointsUri = "debug://breakpoints";
    private const string OutputUri = "debug://output";
    private const string ThreadsUri = "debug://threads";

    private static readonly string RepoRoot = Path.GetFullPath(
        Path.Combine(AppContext.BaseDirectory, "..", "..", "..", "..", ".."));
    private static readonly string SmokeProject = Path.Combine(
        RepoRoot,
        "tests",
        "fixtures",
        "SmokeTestApp",
        "SmokeTestApp.csproj");
    private static readonly string SmokeDll = Path.Combine(
        RepoRoot,
        "tests",
        "fixtures",
        "SmokeTestApp",
        "bin",
        "Debug",
        "net8.0-windows",
        "SmokeTestApp.dll");

    private static PythonBackendProcess StartRealPython()
    {
        Environment.SetEnvironmentVariable("PYTHONPATH", Path.Combine(RepoRoot, "src"));
        return PythonBackendProcess.Start(["--project", RepoRoot]);
    }

    private static async Task BuildSmokeAppAsync()
    {
        using var process = Process.Start(new ProcessStartInfo
        {
            FileName = "dotnet",
            UseShellExecute = false,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            ArgumentList =
            {
                "build",
                SmokeProject,
                "-c",
                "Debug",
                "--nologo",
            },
        }) ?? throw new InvalidOperationException("Could not start dotnet build for SmokeTestApp.");
        var stdout = process.StandardOutput.ReadToEndAsync();
        var stderr = process.StandardError.ReadToEndAsync();
        await process.WaitForExitAsync();
        Assert.True(
            process.ExitCode == 0,
            $"SmokeTestApp build failed.\nstdout:\n{await stdout}\nstderr:\n{await stderr}");
        Assert.True(File.Exists(SmokeDll), $"SmokeTestApp build did not produce {SmokeDll}");
    }

    private static JsonRpcRequest SubscriptionRequest(string method, string uri, string marker) =>
        new()
        {
            Method = method,
            Params = new JsonObject
            {
                ["uri"] = uri,
                ["_meta"] = new JsonObject { ["marker"] = marker },
            },
        };

    private static async Task<CallToolResult> CallToolAsync(
        McpClient client,
        string name,
        JsonObject arguments)
    {
        var response = await client.SendRequestAsync(
            new JsonRpcRequest
            {
                Method = RequestMethods.ToolsCall,
                Params = new JsonObject
                {
                    ["name"] = name,
                    ["arguments"] = arguments,
                },
            },
            CancellationToken.None);
        return response.Result.Deserialize<CallToolResult>(McpJsonUtilities.DefaultOptions)!;
    }

    private static async Task WaitUntilAsync(
        Func<bool> predicate,
        SemaphoreSlim signal,
        TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (!predicate())
        {
            var remaining = deadline - DateTime.UtcNow;
            Assert.True(remaining > TimeSpan.Zero, "Timed out waiting for relayed resource updates.");
            Assert.True(await signal.WaitAsync(remaining), "Timed out waiting for relayed resource updates.");
        }
    }

    private static void DrainUpdates(
        ConcurrentQueue<string> updates,
        SemaphoreSlim signal)
    {
        while (updates.TryDequeue(out _))
        {
        }
        while (signal.Wait(0))
        {
        }
    }

    private static async Task<string> WaitForExecStateUpdateAsync(
        McpClient client,
        ConcurrentQueue<string> updates,
        SemaphoreSlim signal,
        IReadOnlySet<string> expected,
        TimeSpan timeout)
    {
        var deadline = DateTime.UtcNow + timeout;
        while (true)
        {
            while (updates.TryDequeue(out var uri))
            {
                if (uri != StateUri)
                {
                    continue;
                }

                var state = await client.ReadResourceAsync(StateUri);
                var content = Assert.IsType<TextResourceContents>(state.Contents[0]);
                using var payload = JsonDocument.Parse(content.Text);
                var execState = payload.RootElement.GetProperty("execState").GetString()!;
                if (expected.Contains(execState))
                {
                    return execState;
                }
            }

            var remaining = deadline - DateTime.UtcNow;
            Assert.True(remaining > TimeSpan.Zero, "Timed out waiting for a terminal state update.");
            Assert.True(await signal.WaitAsync(remaining), "Timed out waiting for a terminal state update.");
        }
    }

    [Fact]
    public async Task RealPython_SubscriptionsAndEveryUriUpdateFlowThroughTheHost()
    {
        await BuildSmokeAppAsync();
        using var backend = StartRealPython();
        try
        {
            var session = ResourcesTestComposition.CreateSession(backend.CreateUpstreamTransport);
            await using (session)
            {
                var downstream = new DuplexChannel();
                using var host = ResourcesTestComposition.BuildHost(
                    session,
                    builder => builder.WithStreamServerTransport(
                        downstream.ServerInputStream,
                        downstream.ServerOutputStream));
                _ = host.RunAsync();

                var updates = new ConcurrentQueue<string>();
                using var signal = new SemaphoreSlim(0);
                await using var client = await McpClient.CreateAsync(
                    downstream.CreateClientTransport(),
                    new McpClientOptions
                    {
                        Handlers = new McpClientHandlers
                        {
                            NotificationHandlers =
                            [
                                new(NotificationMethods.ResourceUpdatedNotification, (notification, ct) =>
                                {
                                    var uri = notification.Params?["uri"]?.GetValue<string>();
                                    if (uri is not null)
                                    {
                                        updates.Enqueue(uri);
                                        signal.Release();
                                    }
                                    return ValueTask.CompletedTask;
                                }),
                            ],
                        },
                    });

                Assert.True(client.ServerCapabilities?.Resources?.Subscribe);
                Assert.False(client.ServerCapabilities?.Resources?.ListChanged);

                var unknown = await Assert.ThrowsAsync<McpProtocolException>(async () =>
                    await client.SendRequestAsync(
                        SubscriptionRequest(
                            RequestMethods.ResourcesSubscribe,
                            "debug://unknown",
                            "unknown"),
                        CancellationToken.None));
                Assert.Equal(McpErrorCode.InvalidParams, unknown.ErrorCode);
                Assert.Contains("Unknown resource", unknown.Message);

                await client.SendRequestAsync(
                    SubscriptionRequest(RequestMethods.ResourcesSubscribe, BreakpointsUri, "first"),
                    CancellationToken.None);
                await client.SendRequestAsync(
                    SubscriptionRequest(RequestMethods.ResourcesSubscribe, BreakpointsUri, "duplicate"),
                    CancellationToken.None);

                var source = Path.Combine(RepoRoot, "tests", "fixtures", "SmokeTestApp", "Program.cs");
                var added = await CallToolAsync(
                    client,
                    "add_breakpoint",
                    new JsonObject { ["file"] = source, ["line"] = 1 });
                Assert.False(added.IsError == true);
                await WaitUntilAsync(
                    () => updates.Count(uri => uri == BreakpointsUri) == 1,
                    signal,
                    TimeSpan.FromSeconds(10));
                await Task.Delay(200);
                Assert.Equal(1, updates.Count(uri => uri == BreakpointsUri));

                await client.SendRequestAsync(
                    SubscriptionRequest(RequestMethods.ResourcesUnsubscribe, BreakpointsUri, "remove"),
                    CancellationToken.None);
                var removed = await CallToolAsync(
                    client,
                    "remove_breakpoint",
                    new JsonObject { ["file"] = source, ["line"] = 1 });
                Assert.False(removed.IsError == true);
                await Task.Delay(200);
                Assert.Equal(1, updates.Count(uri => uri == BreakpointsUri));

                foreach (var uri in new[] { StateUri, OutputUri, ThreadsUri })
                {
                    await client.SendRequestAsync(
                        SubscriptionRequest(RequestMethods.ResourcesSubscribe, uri, uri),
                        CancellationToken.None);
                }

                var started = await CallToolAsync(
                    client,
                    "start_debug",
                    new JsonObject
                    {
                        ["program"] = SmokeDll,
                        ["args"] = new JsonArray("longrun"),
                        ["pre_build"] = false,
                        ["stop_at_entry"] = false,
                    });
                Assert.False(started.IsError == true);
                await WaitUntilAsync(
                    () =>
                        updates.Contains(StateUri)
                        && updates.Contains(OutputUri)
                        && updates.Contains(ThreadsUri),
                    signal,
                    TimeSpan.FromSeconds(10));

                await client.SendRequestAsync(
                    SubscriptionRequest(RequestMethods.ResourcesUnsubscribe, OutputUri, "stop-output"),
                    CancellationToken.None);
                var outputCount = updates.Count(uri => uri == OutputUri);
                await Task.Delay(800);
                Assert.Equal(outputCount, updates.Count(uri => uri == OutputUri));

                DrainUpdates(updates, signal);
                var terminalState = await WaitForExecStateUpdateAsync(
                    client,
                    updates,
                    signal,
                    new HashSet<string>(StringComparer.Ordinal) { "terminated" },
                    TimeSpan.FromSeconds(10));
                Assert.Equal("terminated", terminalState);

                await client.SendRequestAsync(
                    SubscriptionRequest(RequestMethods.ResourcesSubscribe, OutputUri, "clear-output"),
                    CancellationToken.None);
                DrainUpdates(updates, signal);
                var cleared = await CallToolAsync(
                    client,
                    "get_output",
                    new JsonObject { ["clear"] = true });
                Assert.False(cleared.IsError == true);
                await WaitUntilAsync(
                    () => updates.Contains(OutputUri),
                    signal,
                    TimeSpan.FromSeconds(5));
                var output = await client.ReadResourceAsync(OutputUri);
                Assert.Equal("", Assert.IsType<TextResourceContents>(output.Contents[0]).Text);

                var stopped = await CallToolAsync(client, "stop_debug", new JsonObject());
                Assert.False(stopped.IsError == true);

                using var target = Process.Start(new ProcessStartInfo
                {
                    FileName = "dotnet",
                    UseShellExecute = false,
                    RedirectStandardOutput = true,
                    RedirectStandardError = true,
                    CreateNoWindow = true,
                    ArgumentList =
                    {
                        SmokeDll,
                        "longrun",
                    },
                }) ?? throw new InvalidOperationException("Could not start attach target.");
                try
                {
                    await Task.Delay(200);
                    DrainUpdates(updates, signal);
                    var attached = await CallToolAsync(
                        client,
                        "attach_debug",
                        new JsonObject { ["process_id"] = target.Id });
                    Assert.False(attached.IsError == true);
                    await WaitUntilAsync(
                        () =>
                            updates.Contains(StateUri)
                            && updates.Contains(OutputUri)
                            && updates.Contains(ThreadsUri),
                        signal,
                        TimeSpan.FromSeconds(10));

                    DrainUpdates(updates, signal);
                    var terminated = await CallToolAsync(
                        client,
                        "terminate_debug",
                        new JsonObject());
                    Assert.False(terminated.IsError == true);
                    var finalState = await WaitForExecStateUpdateAsync(
                        client,
                        updates,
                        signal,
                        new HashSet<string>(StringComparer.Ordinal) { "idle", "terminated" },
                        TimeSpan.FromSeconds(10));
                    Assert.Contains(finalState, new[] { "idle", "terminated" });
                }
                finally
                {
                    if (!target.HasExited)
                    {
                        target.Kill(entireProcessTree: true);
                    }
                    await target.WaitForExitAsync();
                }
            }
        }
        finally
        {
            await backend.StopAsync();
            await backend.WaitForStderrForwardedAsync();
        }
    }
}
