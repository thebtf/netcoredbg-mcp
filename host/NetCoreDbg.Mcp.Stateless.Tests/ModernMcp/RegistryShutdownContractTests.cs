using System.Diagnostics;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Runtime.Loader;
using System.Text.Json;
using System.Text.Json.Nodes;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using Microsoft.Win32.SafeHandles;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;
using NetCoreDbg.Mcp.Host.Tests;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class RegistryShutdownContractTests
{
    private static readonly TimeSpan ObservationTimeout = TimeSpan.FromSeconds(5);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern SafeProcessHandle OpenProcess(uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint pid);

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool GetProcessTimes(SafeProcessHandle process, out long creation, out long exit, out long kernel, out long user);

    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern uint WaitForSingleObject(SafeProcessHandle process, uint milliseconds);

    [DllImport("kernel32.dll")]
    private static extern IntPtr GetCurrentProcess();

    [DllImport("kernel32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool DuplicateHandle(IntPtr sourceProcess, IntPtr sourceHandle, IntPtr targetProcess,
        out IntPtr targetHandle, uint access, [MarshalAs(UnmanagedType.Bool)] bool inherit, uint options);

    [DllImport("kernel32.dll", EntryPoint = "TerminateProcess", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool TerminateOwnedWitness(SafeProcessHandle process, uint exitCode);

    private static SafeProcessHandle OpenControlledWitness(int pid, long? expectedStartTicks)
    {
        var handle = OpenProcess(0x00101000, false, checked((uint)pid));
        try
        {
            Assert.False(handle.IsInvalid);
            Assert.True(GetProcessTimes(handle, out var creation, out _, out _, out _));
            Assert.Equal(Assert.IsType<long>(expectedStartTicks), DateTime.FromFileTimeUtc(creation).Ticks);
            return handle;
        }
        catch
        {
            handle.Dispose();
            throw;
        }
    }

    [Fact]
    public async Task LegacyLaunchCompatibility_InspectsWithoutClaimingAShimSwap()
    {
        if (!OperatingSystem.IsWindows()) return;
        var versionedAssembly = typeof(RegistryShutdownContractTests).Assembly.Location;
        var activeMajor = FileVersionInfo.GetVersionInfo(versionedAssembly).FileMajorPart;
        var targetMajor = activeMajor == 8 ? 9 : 8;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(), debuggerPathFactory: scratch =>
        {
            var shim = Path.Combine(scratch, "dbgshim.dll");
            var bytes = File.ReadAllBytes(versionedAssembly);
            var fileVersion = Assert.IsType<string>(FileVersionInfo.GetVersionInfo(versionedAssembly).FileVersion);
            var original = System.Text.Encoding.Unicode.GetBytes(fileVersion + "\0");
            var replacement = System.Text.Encoding.Unicode.GetBytes(fileVersion.Replace('.', ',') + "\0");
            Assert.NotEqual(original, replacement);
            var replacements = 0;
            for (var offset = 0; offset <= bytes.Length - original.Length;)
            {
                var index = bytes.AsSpan(offset).IndexOf(original);
                if (index < 0) break;
                replacement.CopyTo(bytes.AsSpan(offset + index));
                offset += index + original.Length;
                replacements++;
            }
            Assert.True(replacements > 0);
            File.WriteAllBytes(shim, bytes);
            Assert.Contains(',', FileVersionInfo.GetVersionInfo(shim).FileVersion!);
            File.WriteAllBytes(Path.Combine(scratch, "debugger.exe"), []);
            return Path.Combine(scratch, "debugger.exe");
        });
        harness.EnableLegacyCompat();
        var runtimeConfig = Path.ChangeExtension(harness.ProgramPath, ".runtimeconfig.json");
        await File.WriteAllTextAsync(runtimeConfig, JsonSerializer.Serialize(new { runtimeOptions = new { framework = new { name = "Microsoft.NETCore.App", version = $"{targetMajor}.0.0" } } }));
        var originalProgram = await File.ReadAllBytesAsync(harness.ProgramPath);
        var originalConfig = await File.ReadAllBytesAsync(runtimeConfig);

        var response = Content(await harness.CallLegacyAsync("inspect_debug_launch_compatibility",
            new JsonObject { ["program"] = harness.ProgramPath }, "launch-compatibility"));

        Assert.Null(response["error"]);
        Assert.Equal("idle", response["state"]?.GetValue<string>());
        var data = Assert.IsType<JsonObject>(response["data"]);
        Assert.Equal(harness.ProgramPath, data["program"]?.GetValue<string>());
        Assert.Equal("known", data["targetRuntime"]?["status"]?.GetValue<string>());
        Assert.Equal(targetMajor, data["targetRuntime"]?["major"]?.GetValue<int>());
        Assert.Equal("known", data["activeDbgshim"]?["status"]?.GetValue<string>());
        Assert.Equal(activeMajor, data["activeDbgshim"]?["major"]?.GetValue<int>());
        Assert.Equal("unknown", data["verdict"]?.GetValue<string>());
        Assert.Null(data["compatible"]);
        Assert.Contains("does not select a cached candidate", data["warning"]?.GetValue<string>());
        Assert.False(data["willMutateSharedDebugger"]?.GetValue<bool>());
        Assert.False(data["mutationPerformed"]?.GetValue<bool>());
        Assert.NotEqual("compatible_after_swap", data["verdict"]?.GetValue<string>());
        Assert.Equal(originalProgram, await File.ReadAllBytesAsync(harness.ProgramPath));
        Assert.Equal(originalConfig, await File.ReadAllBytesAsync(runtimeConfig));
        await File.WriteAllTextAsync(runtimeConfig, JsonSerializer.Serialize(new { runtimeOptions = new { framework = new { name = "Microsoft.NETCore.App", version = $"{activeMajor}.0.0" } } }));
        var matching = Content(await harness.CallLegacyAsync("inspect_debug_launch_compatibility",
            new JsonObject { ["program"] = harness.ProgramPath }, "launch-compatibility-matching"));
        Assert.Equal("compatible", matching["data"]?["verdict"]?.GetValue<string>());
        Assert.True(matching["data"]?["compatible"]?.GetValue<bool>());
        Assert.Contains("loaded shim", matching["data"]?["warning"]?.GetValue<string>());

        await File.WriteAllTextAsync(runtimeConfig, "{invalid");
        var malformed = Content(await harness.CallLegacyAsync("inspect_debug_launch_compatibility",
            new JsonObject { ["program"] = harness.ProgramPath }, "launch-compatibility-malformed"));
        Assert.Equal("malformed", malformed["data"]?["targetRuntime"]?["status"]?.GetValue<string>());
        Assert.Equal("unknown", malformed["data"]?["verdict"]?.GetValue<string>());
        Assert.Null(malformed["data"]?["compatible"]);
        Assert.Empty(await harness.Fixture.ReadTranscriptAsync());
    }

    [Fact]
    public async Task LegacyLaunchCompatibility_DoesNotBindPathOnlyDebuggerToAnotherExecutable()
    {
        if (!OperatingSystem.IsWindows()) return;
        var previousPath = Environment.GetEnvironmentVariable("PATH");
        var versionedAssembly = typeof(RegistryShutdownContractTests).Assembly.Location;
        var activeMajor = FileVersionInfo.GetVersionInfo(versionedAssembly).FileMajorPart;
        try
        {
            await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(), debuggerPathFactory: scratch =>
            {
                var searchDirectory = Path.Combine(scratch, "path-only-debugger");
                Directory.CreateDirectory(searchDirectory);
                File.WriteAllBytes(Path.Combine(searchDirectory, "decoy-debugger.exe"), []);
                File.Copy(versionedAssembly, Path.Combine(searchDirectory, "dbgshim.dll"));
                Environment.SetEnvironmentVariable("PATH", searchDirectory + Path.PathSeparator + previousPath);
                return "decoy-debugger";
            });
            harness.EnableLegacyCompat();
            var runtimeConfig = Path.ChangeExtension(harness.ProgramPath, ".runtimeconfig.json");
            await File.WriteAllTextAsync(runtimeConfig, JsonSerializer.Serialize(new { runtimeOptions = new { framework = new { name = "Microsoft.NETCore.App", version = $"{activeMajor}.0.0" } } }));

            var response = Content(await harness.CallLegacyAsync("inspect_debug_launch_compatibility",
                new JsonObject { ["program"] = harness.ProgramPath }, "launch-compatibility-path"));
            var data = Assert.IsType<JsonObject>(response["data"]);
            Assert.Equal("unavailable", data["activeDbgshim"]?["status"]?.GetValue<string>());
            Assert.Null(data["activeDbgshim"]?["path"]);
            Assert.Equal("unknown", data["verdict"]?.GetValue<string>());
            Assert.Null(data["compatible"]);
            Assert.Empty(await harness.Fixture.ReadTranscriptAsync());
        }
        finally { Environment.SetEnvironmentVariable("PATH", previousPath); }
    }

    [Fact]
    public async Task LegacyCleanupProcesses_StatusAndAccessChecksDoNotMutateSession()
    {
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(SpawnDescendant: true, LifecycleMode: "all-stop"));
        harness.EnableLegacyCompat();
        const string owner = "cleanup-status-owner";
        try
        {
            var started = Content(await harness.CallLegacyWithMetaAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
            }, "cleanup-status-start", owner).WaitAsync(ObservationTimeout));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
            await harness.Fixture.WaitForEventAsync("process", observation.Token);
            _ = await harness.CallLegacyWithMetaAsync("get_threads", new JsonObject(), "cleanup-status-barrier", owner);
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            var startup = Assert.Single(transcript, e => e.Kind == "startup");
            var child = Assert.Single(transcript, e => e.Kind == "descendant");
            var adapterPid = Assert.IsType<int>(startup.ProcessId);
            var childPid = Assert.IsType<int>(child.ProcessId);
            using var witness = OperatingSystem.IsWindows() ? OpenControlledWitness(childPid, child.ProcessStartTimeUtcTicks) : null;
            var before = transcript.Count;
            foreach (var arguments in new[] { new JsonObject(), new JsonObject { ["force"] = false } })
            {
                var status = Content(await harness.CallLegacyWithMetaAsync("cleanup_processes", arguments, "cleanup-status", "cleanup-other"));
                Assert.Null(status["error"]);
                var data = Assert.IsType<JsonObject>(status["data"]);
                Assert.Equal("status", data["action"]?.GetValue<string>());
                var processes = Assert.IsType<JsonArray>(data["processes"]);
                Assert.Equal(processes.Count, data["total"]?.GetValue<int>());
                Assert.Equal(processes.Count(p => p?["alive"]?.GetValue<bool>() == true), data["alive"]?.GetValue<int>());
                Assert.Contains(processes, p => (OperatingSystem.IsWindows()
                    ? p?["pid"]?.GetValue<int>() == adapterPid && p?["role"]?.GetValue<string>() == "netcoredbg"
                    : p?["role"]?.GetValue<string>() == "adapter_guardian") && p?["alive"]?.GetValue<bool>() == true);
                Assert.Contains(processes, p => p?["pid"]?.GetValue<int>() == childPid && p?["role"]?.GetValue<string>() == "debuggee" && p?["alive"]?.GetValue<bool>() == true);
            }
            var denied = Content(await harness.CallLegacyWithMetaAsync("cleanup_processes", new JsonObject { ["force"] = true }, "cleanup-denied", "cleanup-other"));
            Assert.Contains("owned by another agent", denied["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            var malformed = Content(await harness.CallLegacyWithMetaAsync("cleanup_processes", new JsonObject { ["force"] = "true" }, "cleanup-malformed", owner));
            Assert.NotNull(malformed["error"]);
            Assert.DoesNotContain((await harness.Fixture.ReadTranscriptAsync()).Skip(before), e => e.Kind == "request" && e.Command is "terminate" or "disconnect");
        }
        finally
        {
            await Record.ExceptionAsync(async () => await harness.CallLegacyWithMetaAsync("stop_debug", new JsonObject(), "cleanup-status-finally", owner).WaitAsync(TimeSpan.FromSeconds(20)));
        }
    }

    [Fact]
    public async Task LegacyCleanupProcesses_ForceStopsOwnedJobWithoutStoppingOtherTarget()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true, SpawnDescendant: true, HoldExitAfterDisconnectResponse: true));
        harness.EnableLegacyCompat();
        using var unrelated = harness.Fixture.StartOwnedTarget();
        const string owner = "cleanup-force-owner";
        try
        {
            var started = Content(await harness.CallLegacyWithMetaAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
            }, "cleanup-force-start", owner).WaitAsync(ObservationTimeout));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            var startup = Assert.Single(transcript, e => e.Kind == "startup");
            var descendant = Assert.Single(transcript, e => e.Kind == "descendant");
            using var adapter = OpenControlledWitness(Assert.IsType<int>(startup.ProcessId), startup.ProcessStartTimeUtcTicks);
            using var child = OpenControlledWitness(Assert.IsType<int>(descendant.ProcessId), descendant.ProcessStartTimeUtcTicks);
            Assert.Equal(258u, WaitForSingleObject(adapter, 0));
            Assert.Equal(258u, WaitForSingleObject(child, 0));
            var result = Content(await harness.CallLegacyWithMetaAsync("cleanup_processes", new JsonObject { ["force"] = true }, "cleanup-force", owner).WaitAsync(TimeSpan.FromSeconds(20)));
            Assert.Null(result["error"]);
            var data = Assert.IsType<JsonObject>(result["data"]);
            Assert.Equal("cleanup", data["action"]?.GetValue<string>());
            Assert.True(data["complete"]?.GetValue<bool>());
            Assert.Equal(1, data["terminated"]?.GetValue<int>());
            Assert.Equal(0, data["remaining_owners"]?.GetValue<int>());
            Assert.Empty(Assert.IsType<JsonArray>(data["errors"]));
            Assert.Null(data["tree_terminated"]);
            Assert.Null(data["tree_drained"]);
            Assert.Equal(0u, WaitForSingleObject(adapter, 5000));
            Assert.Equal(0u, WaitForSingleObject(child, 5000));
            Assert.False(unrelated.HasExited);
            Assert.Single((await harness.Fixture.ReadTranscriptAsync()).Skip(transcript.Count), e => e.Kind == "request" && e.Command == "disconnect");
        }
        finally
        {
            await FixtureProcess.StopOwnedTargetAsync(unrelated);
            await Record.ExceptionAsync(async () => await harness.CallLegacyWithMetaAsync("stop_debug", new JsonObject(), "cleanup-force-finally", owner).WaitAsync(TimeSpan.FromSeconds(20)));
        }
    }

    [Fact]
    public async Task LegacyCleanupProcesses_AttachedTargetSurvivesUnconfirmedDetachAndRetry()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(SupportsTerminate: true, SuppressLifecycleEvents: true));
        harness.EnableLegacyCompat();
        using var target = harness.Fixture.StartOwnedTarget();
        const string owner = "cleanup-attach-owner";
        try
        {
            var attached = Content(await harness.CallLegacyWithMetaAsync("attach_debug", new JsonObject { ["process_id"] = target.Id }, "cleanup-attach-start", owner).WaitAsync(ObservationTimeout));
            Assert.True(attached["data"]?["success"]?.GetValue<bool>());
            var before = (await harness.Fixture.ReadTranscriptAsync()).Count;
            for (var attempt = 0; attempt < 2; attempt++)
            {
                var result = Content(await harness.CallLegacyWithMetaAsync("cleanup_processes", new JsonObject { ["force"] = true }, $"cleanup-attach-{attempt}", owner).WaitAsync(TimeSpan.FromSeconds(20)));
                Assert.NotNull(result["error"]);
                var data = Assert.IsType<JsonObject>(result["data"]);
                Assert.False(data["complete"]?.GetValue<bool>() ?? true);
                Assert.True(data["remaining_owners"]?.GetValue<int>() > 0);
                Assert.NotEmpty(Assert.IsType<JsonArray>(data["errors"]));
                Assert.Null(data["tree_terminated"]);
                Assert.False(target.HasExited);
            }
            var actions = (await harness.Fixture.ReadTranscriptAsync()).Skip(before).Where(e => e.Kind == "request").ToArray();
            Assert.DoesNotContain(actions, e => e.Command == "terminate");
            var disconnect = JsonNode.Parse(Assert.Single(actions, e => e.Command == "disconnect").RawPayload!)!;
            Assert.False(disconnect["arguments"]?["terminateDebuggee"]?.GetValue<bool>());
        }
        finally
        {
            await FixtureProcess.StopOwnedTargetAsync(target);
            await Record.ExceptionAsync(async () => await harness.CallLegacyWithMetaAsync("stop_debug", new JsonObject(), "cleanup-attach-finally", owner).WaitAsync(TimeSpan.FromSeconds(20)));
            await Record.ExceptionAsync(async () => await harness.CallLegacyWithMetaAsync("stop_debug", new JsonObject(), "cleanup-attach-finally-retry", owner).WaitAsync(TimeSpan.FromSeconds(20)));
        }
    }

    [Fact]
    public async Task LegacyCleanupProcesses_AlreadyExitedRootIsNotCountedAsForced()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration());
        using var target = harness.Fixture.StartOwnedTarget();
        var handle = target.SafeHandle;
        await FixtureProcess.StopOwnedTargetAsync(target);
        var assemblyPath = TestOutputPathResolver.ResolveManagedAssembly(
            RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");
        var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyPath);
        var owner = assembly.GetType("NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession+WindowsProcessTreeOwnership", throwOnError: true)!;
        var terminate = owner.GetMethod("TryTerminateRoot", BindingFlags.Static | BindingFlags.NonPublic)!;
        Assert.False(Assert.IsType<bool>(terminate.Invoke(null, [handle])));
        Assert.Equal(0u, WaitForSingleObject(handle, 5000));
    }

    [Fact]
    public async Task LegacyCleanupProcesses_RootTerminationFailureRetainsOwnerUntilRetry()
    {
        if (!OperatingSystem.IsWindows()) return;
        var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true, HoldExitAfterDisconnectResponse: true));
        harness.EnableLegacyCompat();
        Process? adapter = null;
        FieldInfo? handleField = null;
        SafeProcessHandle? original = null;
        SafeProcessHandle? restricted = null;
        SafeProcessHandle? witness = null;
        string? token = null;
        Exception? testFailure = null;
        try
        {
            var started = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
            }, "cleanup-root-start").WaitAsync(ObservationTimeout));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            token = harness.LegacySessionId;
            var slot = harness.OwnedSlot(token);
            Assert.NotNull(slot);
            var binding = harness.SceneBinding(token);
            var session = binding.GetType().GetField("_session", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(binding)!;
            adapter = Assert.IsType<Process>(session.GetType().GetField("_process", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(session));
            session.GetType().GetField("_stopTimeout", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(session, TimeSpan.FromMilliseconds(150));
            handleField = typeof(Process).GetField("_processHandle", BindingFlags.Instance | BindingFlags.NonPublic)!;
            original = adapter.SafeHandle;
            var self = GetCurrentProcess();
            Assert.True(DuplicateHandle(self, original.DangerousGetHandle(), self, out var witnessHandle, 0, false, 2));
            witness = new SafeProcessHandle(witnessHandle, ownsHandle: true);
            Assert.True(DuplicateHandle(self, original.DangerousGetHandle(), self, out var limitedHandle, 0x00101000, false, 0));
            restricted = new SafeProcessHandle(limitedHandle, ownsHandle: true);
            handleField.SetValue(adapter, restricted);

            var first = Content(await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true }, "cleanup-root-first").WaitAsync(TimeSpan.FromSeconds(20)));
            Assert.NotNull(first["error"]);
            Assert.False(first["data"]?["complete"]?.GetValue<bool>() ?? true);
            Assert.Same(slot, harness.OwnedSlot(token));
            Assert.False(restricted.IsClosed);
            Assert.Equal(258u, WaitForSingleObject(witness, 0));
            handleField.SetValue(adapter, original);
            restricted.Dispose();
            restricted = null;

            var retry = Content(await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true }, "cleanup-root-retry").WaitAsync(TimeSpan.FromSeconds(20)));
            Assert.Null(retry["error"]);
            Assert.True(retry["data"]?["complete"]?.GetValue<bool>());
            Assert.Equal(1, retry["data"]?["terminated"]?.GetValue<int>());
            Assert.Null(harness.OwnedSlot(token));
            Assert.Equal(0u, WaitForSingleObject(witness, 5000));
            Assert.Single(await harness.Fixture.ReadTranscriptAsync(), e => e.Kind == "request" && e.Command == "disconnect");
        }
        catch (Exception exception)
        {
            testFailure = exception;
            throw;
        }
        finally
        {
            if (adapter is not null && handleField is not null && original is not null && !original.IsClosed)
                handleField.SetValue(adapter, original);
            restricted?.Dispose();
            if (token is not null)
                await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true }, "cleanup-root-finally").WaitAsync(TimeSpan.FromSeconds(20)));
            if (witness is not null)
            {
                if (WaitForSingleObject(witness, 0) != 0) _ = TerminateOwnedWitness(witness, 1);
                Assert.Equal(0u, WaitForSingleObject(witness, 5000));
                witness.Dispose();
            }
            if (token is not null && harness.OwnedSlot(token) is null) original?.Dispose();
            var cleanupFailure = await Record.ExceptionAsync(async () => await harness.DisposeAsync().AsTask().WaitAsync(TimeSpan.FromSeconds(20)));
            if (cleanupFailure is not null)
            {
                if (testFailure is null) throw cleanupFailure;
                testFailure.Data["HarnessCleanupFailure"] = cleanupFailure;
            }
        }
    }

    [Fact]
    public async Task LegacyCleanupProcesses_JobQueryFailureRetainsOwnerUntilVerifiedRetry()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true));
        harness.EnableLegacyCompat();
        object? owner = null;
        FieldInfo? jobField = null;
        SafeHandle? originalJob = null;
        SafeHandle? restrictedJob = null;
        string? token = null;
        try
        {
            var started = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
            }, "cleanup-job-query-start").WaitAsync(ObservationTimeout));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            token = harness.LegacySessionId;
            var slot = harness.OwnedSlot(token);
            Assert.NotNull(slot);
            var binding = harness.SceneBinding(token);
            var session = binding.GetType().GetField("_session", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(binding)!;
            owner = session.GetType().GetField("_processTreeOwnership", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(session)!;
            jobField = owner.GetType().GetField("_job", BindingFlags.Instance | BindingFlags.NonPublic)!;
            originalJob = Assert.IsAssignableFrom<SafeHandle>(jobField.GetValue(owner));
            var self = GetCurrentProcess();
            Assert.True(DuplicateHandle(self, originalJob.DangerousGetHandle(), self, out var restricted, 0x0008, false, 0));
            restrictedJob = Assert.IsAssignableFrom<SafeHandle>(Activator.CreateInstance(originalJob.GetType(), restricted));
            jobField.SetValue(owner, restrictedJob);

            var first = Content(await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true }, "cleanup-job-query-first").WaitAsync(TimeSpan.FromSeconds(20)));
            Assert.NotNull(first["error"]);
            Assert.False(first["data"]?["complete"]?.GetValue<bool>() ?? true);
            Assert.Same(slot, harness.OwnedSlot(token));
            Assert.False(restrictedJob.IsClosed);
            jobField.SetValue(owner, originalJob);
            restrictedJob.Dispose();
            restrictedJob = null;

            var retry = Content(await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true }, "cleanup-job-query-retry").WaitAsync(TimeSpan.FromSeconds(20)));
            Assert.Null(retry["error"]);
            Assert.True(retry["data"]?["complete"]?.GetValue<bool>());
            Assert.Null(harness.OwnedSlot(token));
            Assert.Single(await harness.Fixture.ReadTranscriptAsync(), e => e.Kind == "request" && e.Command == "disconnect");
        }
        finally
        {
            if (owner is not null && jobField is not null && originalJob is not null && !originalJob.IsClosed)
                jobField.SetValue(owner, originalJob);
            restrictedJob?.Dispose();
            if (token is not null)
                await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true }, "cleanup-job-query-finally").WaitAsync(TimeSpan.FromSeconds(20)));
            if (token is not null && harness.OwnedSlot(token) is null) originalJob?.Dispose();
        }
    }

    [Fact]
    public async Task LegacyCleanupProcesses_IncompleteObservationsDoNotBlockOwnerCleanup()
    {
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true));
        harness.EnableLegacyCompat();
        try
        {
            var started = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
            }, "legacy-cleanup-overflow-start").WaitAsync(ObservationTimeout));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            var token = harness.LegacySessionId;
            var binding = harness.SceneBinding(token);
            var session = binding.GetType().GetField("_session", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(binding)!;
            session.GetType().GetField("_legacyProcessObservationsIncomplete", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(session, true);

            var cleanup = Content(await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true },
                "legacy-cleanup-overflow-force").WaitAsync(TimeSpan.FromSeconds(20)));
            Assert.Null(cleanup["error"]);
            Assert.True(cleanup["data"]?["complete"]?.GetValue<bool>());
            Assert.False(cleanup["data"]?["observations_complete"]?.GetValue<bool>() ?? true);
            Assert.Equal(0, cleanup["data"]?["remaining_owners"]?.GetValue<int>());
            Assert.Null(harness.OwnedSlot(token));
            Assert.Single(await harness.Fixture.ReadTranscriptAsync(), e => e.Kind == "request" && e.Command == "disconnect");
        }
        finally
        {
            await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(),
                "legacy-cleanup-overflow-finally").WaitAsync(TimeSpan.FromSeconds(20)));
        }
    }

    [Fact]
    public async Task LegacyCleanupProcesses_DisposalFailureRetainsOwnerAndRetryCompletesWithoutRepeatingStop()
    {
        var disposeAttempts = 0;
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: true, SuppressLifecycleEvents: true),
            disposeSession: session =>
            {
                if (Interlocked.Increment(ref disposeAttempts) == 1)
                    return ValueTask.FromException(new InvalidOperationException("controlled disposal failure"));
                return RegistryHostHarness.DisposeSessionAsync(session);
            });
        harness.EnableLegacyCompat();
        try
        {
            var started = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
            }, "legacy-cleanup-retry-start").WaitAsync(ObservationTimeout));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            var token = harness.LegacySessionId;
            Assert.NotNull(harness.OwnedSlot(token));

            var first = Content(await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true },
                "legacy-cleanup-retry-first").WaitAsync(TimeSpan.FromSeconds(20)));
            Assert.NotNull(first["error"]);
            Assert.Equal("cleanup", first["data"]?["action"]?.GetValue<string>());
            Assert.False(first["data"]?["complete"]?.GetValue<bool>() ?? true);
            Assert.True(first["data"]?["remaining_owners"]?.GetValue<int>() > 0);
            Assert.NotEmpty(Assert.IsType<JsonArray>(first["data"]?["errors"]));
            Assert.NotNull(harness.OwnedSlot(token));
            Assert.Equal(1, Volatile.Read(ref disposeAttempts));
            var firstTranscript = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(firstTranscript, e => e.Kind == "request" && e.Command == "disconnect");

            var retry = Content(await harness.CallLegacyAsync("cleanup_processes", new JsonObject { ["force"] = true },
                "legacy-cleanup-retry-second").WaitAsync(TimeSpan.FromSeconds(20)));
            Assert.Null(retry["error"]);
            Assert.Equal("cleanup", retry["data"]?["action"]?.GetValue<string>());
            Assert.True(retry["data"]?["complete"]?.GetValue<bool>());
            Assert.Equal(0, retry["data"]?["remaining_owners"]?.GetValue<int>());
            Assert.Empty(Assert.IsType<JsonArray>(retry["data"]?["errors"]));
            Assert.Equal(2, Volatile.Read(ref disposeAttempts));
            Assert.Null(harness.OwnedSlot(token));
            var retryTranscript = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(retryTranscript, e => e.Kind == "request" && e.Command == "disconnect");
            Assert.Equal(firstTranscript.Count(e => e.Kind == "request" && e.Command == "terminate"),
                retryTranscript.Count(e => e.Kind == "request" && e.Command == "terminate"));
        }
        finally
        {
            await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(),
                "legacy-cleanup-retry-finally").WaitAsync(TimeSpan.FromSeconds(20)));
        }
    }
    [Fact]
    public async Task StopDebug_CleanupFailureRetainsOwnerForRetryWithoutRepeatingDebuggerStop()
    {
        var disposeAttempts = 0;
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: true, SuppressLifecycleEvents: true),
            disposeSession: session =>
            {
                if (Interlocked.Increment(ref disposeAttempts) == 1)
                    return ValueTask.FromException(new InvalidOperationException("controlled disposal failure"));
                return RegistryHostHarness.DisposeSessionAsync(session);
            });
        var started = Content(await harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath }, "cleanup-retry-start").WaitAsync(ObservationTimeout));
        Assert.Equal("start_debug_success", started["kind"]?.GetValue<string>());
        var token = Assert.IsType<string>(started["debugSessionId"]?.GetValue<string>());
        var owner = harness.OwnedSlot(token);
        Assert.NotNull(owner);

        var first = await harness.CallAsync("stop_debug", SessionArguments(token), "cleanup-retry-first").WaitAsync(ObservationTimeout);
        Assert.True(first.IsError == true);
        Assert.Contains("unconfirmed", Content(first).ToJsonString(), StringComparison.OrdinalIgnoreCase);
        Assert.Same(owner, harness.OwnedSlot(token));
        Assert.Equal(1, Volatile.Read(ref disposeAttempts));
        var afterFirst = await harness.Fixture.ReadTranscriptAsync();
        Assert.Single(afterFirst, static entry => entry.Kind == "request" && entry.Command == "terminate");
        Assert.Single(afterFirst, static entry => entry.Kind == "request" && entry.Command == "disconnect");

        var retry = await harness.CallAsync("stop_debug", SessionArguments(token), "cleanup-retry-second").WaitAsync(ObservationTimeout);
        Assert.False(retry.IsError == true);
        Assert.Equal("stop_debug_success", Content(retry)["kind"]?.GetValue<string>());
        Assert.Equal(2, Volatile.Read(ref disposeAttempts));
        Assert.Null(harness.OwnedSlot(token));
        var afterRetry = await harness.Fixture.ReadTranscriptAsync();
        Assert.Single(afterRetry, static entry => entry.Kind == "request" && entry.Command == "terminate");
        Assert.Single(afterRetry, static entry => entry.Kind == "request" && entry.Command == "disconnect");
        var missing = await harness.CallAsync("stop_debug", SessionArguments(token), "cleanup-retry-missing").WaitAsync(ObservationTimeout);
        Assert.True(missing.IsError == true);
        Assert.Equal("DEBUG_SESSION_NOT_FOUND", Content(missing)["error"]?.GetValue<string>());
    }

    [Fact]
    public async Task StopDebug_ObserverTerminationFailureRetainsOwnerUntilRetry()
    {
        var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true));
        Process? target = null;
        Process? observer = null;
        string? token = null;
        try
        {
            var started = Content(await harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath }, "observer-retry-start").WaitAsync(ObservationTimeout));
            Assert.Equal("start_debug_success", started["kind"]?.GetValue<string>());
            token = Assert.IsType<string>(started["debugSessionId"]?.GetValue<string>());
            var slot = harness.OwnedSlot(token);
            Assert.NotNull(slot);
            var binding = harness.SceneBinding(token);
            var bindingType = binding.GetType();
            var processField = bindingType.GetField("_bridgeProcess", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var terminateField = bindingType.GetField("_terminateBridge", BindingFlags.Instance | BindingFlags.NonPublic)!;
            target = harness.Fixture.StartOwnedTarget();
            _ = target.SafeHandle;
            var startTime = target.StartTime.ToUniversalTime();
            observer = Process.GetProcessById(target.Id);
            var handle = observer.SafeHandle;
            Assert.Equal(startTime, observer.StartTime.ToUniversalTime());
            Assert.False(target.HasExited);
            var attempts = 0;
            Action<Process> terminate = process =>
            {
                Assert.Same(observer, process);
                if (Interlocked.Increment(ref attempts) == 1)
                    throw new InvalidOperationException("controlled observer termination failure");
                process.Kill();
            };
            terminateField.SetValue(binding, terminate);
            processField.SetValue(binding, observer);

            var first = await harness.CallAsync("stop_debug", SessionArguments(token), "observer-retry-first").WaitAsync(ObservationTimeout);
            Assert.True(first.IsError == true);
            Assert.Equal("DEBUG_SESSION_CLEANUP_UNCONFIRMED", Content(first)["error"]?.GetValue<string>());
            Assert.Same(slot, harness.OwnedSlot(token));
            Assert.Same(observer, processField.GetValue(binding));
            Assert.False(handle.IsClosed);
            Assert.False(target.HasExited);
            Assert.Equal(1, Volatile.Read(ref attempts));
            var afterFirst = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(afterFirst, static entry => entry.Kind == "request" && entry.Command == "terminate");
            Assert.Single(afterFirst, static entry => entry.Kind == "request" && entry.Command == "disconnect");

            var retry = await harness.CallAsync("stop_debug", SessionArguments(token), "observer-retry-second").WaitAsync(ObservationTimeout);
            Assert.False(retry.IsError == true);
            Assert.Equal("stop_debug_success", Content(retry)["kind"]?.GetValue<string>());
            Assert.Equal(2, Volatile.Read(ref attempts));
            Assert.True(target.HasExited);
            Assert.True(handle.IsClosed);
            Assert.Null(harness.OwnedSlot(token));
            var afterRetry = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(afterRetry, static entry => entry.Kind == "request" && entry.Command == "terminate");
            Assert.Single(afterRetry, static entry => entry.Kind == "request" && entry.Command == "disconnect");
        }
        finally
        {
            if (target is not null) await Record.ExceptionAsync(() => FixtureProcess.StopOwnedTargetAsync(target));
            if (token is not null) await Record.ExceptionAsync(async () => await harness.CallAsync("stop_debug", SessionArguments(token), "observer-retry-cleanup").WaitAsync(ObservationTimeout));
            if (observer is not null) Record.Exception(observer.Dispose);
            if (target is not null) Record.Exception(target.Dispose);
            await Record.ExceptionAsync(async () => await harness.DisposeAsync().AsTask().WaitAsync(ObservationTimeout));
        }
    }

    [Fact]
    public async Task StopDebug_ObserverExitConfirmedDespiteTerminationException()
    {
        var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true));
        Process? target = null;
        Process? observer = null;
        string? token = null;
        try
        {
            var started = Content(await harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath }, "observer-exit-race-start").WaitAsync(ObservationTimeout));
            Assert.Equal("start_debug_success", started["kind"]?.GetValue<string>());
            token = Assert.IsType<string>(started["debugSessionId"]?.GetValue<string>());
            Assert.NotNull(harness.OwnedSlot(token));
            var binding = harness.SceneBinding(token);
            var bindingType = binding.GetType();
            var processField = bindingType.GetField("_bridgeProcess", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var terminateField = bindingType.GetField("_terminateBridge", BindingFlags.Instance | BindingFlags.NonPublic)!;
            target = harness.Fixture.StartOwnedTarget();
            _ = target.SafeHandle;
            var startTime = target.StartTime.ToUniversalTime();
            observer = Process.GetProcessById(target.Id);
            var handle = observer.SafeHandle;
            Assert.Equal(startTime, observer.StartTime.ToUniversalTime());
            Assert.False(target.HasExited);
            var attempts = 0;
            var exitConfirmed = false;
            Action<Process> terminate = process =>
            {
                Assert.Same(observer, process);
                Interlocked.Increment(ref attempts);
                process.Kill();
                exitConfirmed = process.WaitForExit(3000);
                if (!exitConfirmed) throw new TimeoutException("controlled observer did not exit");
                throw new InvalidOperationException("controlled failure after observer exit");
            };
            terminateField.SetValue(binding, terminate);
            processField.SetValue(binding, observer);

            var stopped = await harness.CallAsync("stop_debug", SessionArguments(token), "observer-exit-race-stop").WaitAsync(ObservationTimeout);
            Assert.False(stopped.IsError == true);
            Assert.Equal("stop_debug_success", Content(stopped)["kind"]?.GetValue<string>());
            Assert.Equal(1, Volatile.Read(ref attempts));
            Assert.True(exitConfirmed);
            Assert.True(target.HasExited);
            Assert.True(handle.IsClosed);
            Assert.Null(harness.OwnedSlot(token));
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "terminate");
            Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "disconnect");
        }
        finally
        {
            if (target is not null) await Record.ExceptionAsync(() => FixtureProcess.StopOwnedTargetAsync(target));
            if (token is not null) await Record.ExceptionAsync(async () => await harness.CallAsync("stop_debug", SessionArguments(token), "observer-exit-race-cleanup").WaitAsync(ObservationTimeout));
            if (observer is not null) Record.Exception(observer.Dispose);
            if (target is not null) Record.Exception(target.Dispose);
            await Record.ExceptionAsync(async () => await harness.DisposeAsync().AsTask().WaitAsync(ObservationTimeout));
        }
    }

    [Theory]
    [InlineData("CaptureNativeSceneAsync", "capture_native_scene")]
    [InlineData("CaptureElementAsync", "capture_element_snapshot")]
    public async Task Capture_RetainedObserverRefusesBeforeCoordinator(string method, string tool)
    {
        var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true));
        Process? target = null;
        Process? observer = null;
        string? token = null;
        try
        {
            var started = Content(await harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath }, "observer-capture-start").WaitAsync(ObservationTimeout));
            Assert.Equal("start_debug_success", started["kind"]?.GetValue<string>());
            token = Assert.IsType<string>(started["debugSessionId"]?.GetValue<string>());
            var slot = harness.OwnedSlot(token);
            Assert.NotNull(slot);
            var binding = harness.SceneBinding(token);
            var bindingType = binding.GetType();
            var processField = bindingType.GetField("_bridgeProcess", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var terminateField = bindingType.GetField("_terminateBridge", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var coordinatorField = bindingType.GetField("_captureCoordinator", BindingFlags.Instance | BindingFlags.NonPublic)!;
            target = harness.Fixture.StartOwnedTarget();
            _ = target.SafeHandle;
            var startTime = target.StartTime.ToUniversalTime();
            observer = Process.GetProcessById(target.Id);
            var handle = observer.SafeHandle;
            Assert.Equal(startTime, observer.StartTime.ToUniversalTime());
            Assert.False(target.HasExited);
            Action<Process> terminate = process =>
            {
                Assert.Same(observer, process);
                throw new InvalidOperationException("controlled observer termination failure");
            };
            terminateField.SetValue(binding, terminate);
            processField.SetValue(binding, observer);
            Assert.Null(coordinatorField.GetValue(binding));

            var capture = bindingType.GetMethod(method, BindingFlags.Instance | BindingFlags.NonPublic,
                binder: null, types: [typeof(JsonElement), typeof(CancellationToken)], modifiers: null)!;
            var request = JsonSerializer.SerializeToElement(new JsonObject { ["sceneRequest"] = new JsonObject(), ["element"] = new JsonObject() });
            var result = await Assert.IsAssignableFrom<Task<JsonObject>>(capture.Invoke(binding, [request, CancellationToken.None])).WaitAsync(ObservationTimeout);
            Assert.Equal("tool_error", result["kind"]?.GetValue<string>());
            Assert.Equal(tool, result["tool"]?.GetValue<string>());
            Assert.Equal("OBSERVER_UNAVAILABLE", result["code"]?.GetValue<string>());
            Assert.Null(coordinatorField.GetValue(binding));
            Assert.Same(slot, harness.OwnedSlot(token));
            Assert.Same(observer, processField.GetValue(binding));
            Assert.False(handle.IsClosed);
            Assert.False(target.HasExited);
            Assert.Null(result["artifacts"]);
            Assert.Null(result["captureId"]);
        }
        finally
        {
            if (target is not null) await Record.ExceptionAsync(() => FixtureProcess.StopOwnedTargetAsync(target));
            if (token is not null) await Record.ExceptionAsync(async () => await harness.CallAsync("stop_debug", SessionArguments(token), "observer-capture-cleanup").WaitAsync(ObservationTimeout));
            if (observer is not null) Record.Exception(observer.Dispose);
            if (target is not null) Record.Exception(target.Dispose);
            await Record.ExceptionAsync(async () => await harness.DisposeAsync().AsTask().WaitAsync(ObservationTimeout));
        }
    }

    [Fact]
    public async Task StopDebug_RealUiHelperDrainRetainsOwnerUntilRetry()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true));
        var reached = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        var release = new TaskCompletionSource(TaskCreationOptions.RunContinuationsAsynchronously);
        using var cancellation = new CancellationTokenSource();
        string? token = null;
        IAsyncDisposable? uiOwner = null;
        Task<JsonObject>? pending = null;
        try
        {
            var started = Content(await harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath }, "real-helper-start").WaitAsync(ObservationTimeout));
            token = Assert.IsType<string>(started["debugSessionId"]?.GetValue<string>());
            var slot = harness.OwnedSlot(token);
            Assert.NotNull(slot);
            var ui = harness.UiOwner(token);
            uiOwner = Assert.IsAssignableFrom<IAsyncDisposable>(ui);
            var uiType = ui.GetType();
            async Task HoldDrain(Task drained)
            {
                Assert.True(drained.IsCompletedSuccessfully);
                reached.TrySetResult();
                await release.Task;
            }
            uiType.GetField("_drainCompletion", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(ui, (Func<Task, Task>)HoldDrain);
            var identityType = uiType.Assembly.GetType("NetCoreDbg.Mcp.Stateless.NativeScene.NativeSceneTargetIdentity", throwOnError: true)!;
            var identity = Activator.CreateInstance(identityType, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                binder: null, args: [1, "controlled-target", harness.Fixture.ExecutablePath, new string('0', 64), null, null], culture: null)!;
            var observe = uiType.GetMethod("ObserveAsync", BindingFlags.Instance | BindingFlags.NonPublic)!;
            pending = Assert.IsAssignableFrom<Task<JsonObject>>(observe.Invoke(ui,
                [harness.Fixture.ExecutablePath, identity, 1L, 1, 1, (Func<bool>)(() => { cancellation.Cancel(); return true; }), cancellation.Token]));
            await reached.Task.WaitAsync(TimeSpan.FromSeconds(15));
            Assert.Equal("UI_DISCOVERY_CLEANUP_UNCONFIRMED", (await pending.WaitAsync(TimeSpan.FromSeconds(15)))["code"]?.GetValue<string>());
            var retained = uiType.GetField("_pendingCleanup", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(ui)!;
            var helper = Assert.IsType<Process>(retained.GetType().GetProperty("Process", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)!.GetValue(retained));
            Assert.True(helper.HasExited);
            var handle = helper.SafeHandle;
            Assert.False(handle.IsClosed);

            var first = await harness.CallAsync("stop_debug", SessionArguments(token), "real-helper-first").WaitAsync(ObservationTimeout);
            Assert.True(first.IsError == true);
            Assert.Equal("DEBUG_SESSION_CLEANUP_UNCONFIRMED", Content(first)["error"]?.GetValue<string>());
            Assert.Same(slot, harness.OwnedSlot(token));
            Assert.False(handle.IsClosed);
            release.TrySetResult();
            var drains = Assert.IsAssignableFrom<Task>(uiType.GetField("_pendingDrains", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(ui));
            await drains.WaitAsync(ObservationTimeout);
            var retry = await harness.CallAsync("stop_debug", SessionArguments(token), "real-helper-retry").WaitAsync(ObservationTimeout);
            Assert.False(retry.IsError == true);
            Assert.Equal("stop_debug_success", Content(retry)["kind"]?.GetValue<string>());
            Assert.Null(harness.OwnedSlot(token));
            Assert.True(handle.IsClosed);
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "terminate");
            Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "disconnect");
        }
        finally
        {
            release.TrySetResult();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(15)));
            if (uiOwner is not null) await Record.ExceptionAsync(async () => await uiOwner.DisposeAsync().AsTask().WaitAsync(ObservationTimeout));
            if (token is not null) await Record.ExceptionAsync(async () => await harness.CallAsync("stop_debug", SessionArguments(token), "real-helper-cleanup").WaitAsync(ObservationTimeout));
        }
    }

    [Fact]
    public async Task HostShutdown_CleanupFailureRetainsOwnerForRetryWithoutRepeatingDebuggerStop()
    {
        var disposeAttempts = 0;
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: true, SuppressLifecycleEvents: true),
            disposeSession: session =>
            {
                if (Interlocked.Increment(ref disposeAttempts) == 1)
                    return ValueTask.FromException(new InvalidOperationException("controlled disposal failure"));
                return RegistryHostHarness.DisposeSessionAsync(session);
            });
        var started = Content(await harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath }, "shutdown-retry-start").WaitAsync(ObservationTimeout));
        Assert.Equal("start_debug_success", started["kind"]?.GetValue<string>());
        var token = Assert.IsType<string>(started["debugSessionId"]?.GetValue<string>());
        var owner = harness.OwnedSlot(token);
        Assert.NotNull(owner);

        var failure = await Record.ExceptionAsync(() => harness.StopAsync(CancellationToken.None).WaitAsync(ObservationTimeout));
        Assert.NotNull(failure);
        Assert.IsNotType<TimeoutException>(failure);
        Assert.Same(owner, harness.OwnedSlot(token));
        Assert.Equal(1, Volatile.Read(ref disposeAttempts));
        var afterFirst = await harness.Fixture.ReadTranscriptAsync();
        Assert.Single(afterFirst, static entry => entry.Kind == "request" && entry.Command == "terminate");
        Assert.Single(afterFirst, static entry => entry.Kind == "request" && entry.Command == "disconnect");

        await harness.StopAsync(CancellationToken.None).WaitAsync(ObservationTimeout);
        Assert.Equal(2, Volatile.Read(ref disposeAttempts));
        Assert.Null(harness.OwnedSlot(token));
        var afterRetry = await harness.Fixture.ReadTranscriptAsync();
        Assert.Single(afterRetry, static entry => entry.Kind == "request" && entry.Command == "terminate");
        Assert.Single(afterRetry, static entry => entry.Kind == "request" && entry.Command == "disconnect");
    }

    [Fact]
    public async Task HostShutdown_AttachedDrainFailureRetainsOwnerAndRetryDetachesOnce()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true));
        harness.EnableLegacyCompat();
        using var target = harness.Fixture.StartOwnedTarget();
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(15));
        object? slot = null;
        Process? adapter = null;
        Task? shutdown = null;
        try
        {
            var attached = Content(await harness.CallLegacyAsync("attach_debug", new JsonObject { ["process_id"] = target.Id }, "attach-drain-failure-start").WaitAsync(observation.Token));
            Assert.True(attached["data"]?["success"]?.GetValue<bool>());
            var token = harness.LegacySessionId;
            slot = harness.OwnedSlot(token);
            Assert.NotNull(slot);
            var startup = Assert.Single(await harness.Fixture.ReadTranscriptAsync(), static entry => entry.Kind == "startup");
            adapter = Process.GetProcessById(Assert.IsType<int>(startup.ProcessId));
            Assert.Equal(startup.ProcessStartTimeUtcTicks, adapter.StartTime.ToUniversalTime().Ticks);
            var drainField = slot!.GetType().GetField("_attachDrain", BindingFlags.Instance | BindingFlags.NonPublic)!;
            var originalDrain = Assert.IsAssignableFrom<Func<Task>>(drainField.GetValue(slot));
            var drainAttempts = 0;
            Func<Task> failOnce = () => Interlocked.Increment(ref drainAttempts) == 1
                ? Task.FromException(new InvalidOperationException("controlled attached drain failure"))
                : originalDrain();
            drainField.SetValue(slot, failOnce);

            shutdown = harness.StopAsync(CancellationToken.None);
            var failure = await Record.ExceptionAsync(() => shutdown.WaitAsync(observation.Token));
            Assert.NotNull(failure);
            Assert.IsNotType<OperationCanceledException>(failure);
            harness.AcceptObservedShutdownFailure(failure!);
            Assert.Same(slot, harness.OwnedSlot(token));
            Assert.Equal(1, Volatile.Read(ref drainAttempts));
            Assert.DoesNotContain(await harness.Fixture.ReadTranscriptAsync(), static entry => entry.Kind == "request" && entry.Command == "disconnect");

            await FixtureProcess.StopOwnedTargetAsync(target);
            await harness.StopAsync(CancellationToken.None).WaitAsync(observation.Token);
            Assert.Null(harness.OwnedSlot(token));
            Assert.Equal(2, Volatile.Read(ref drainAttempts));
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "disconnect");
            Assert.DoesNotContain(transcript, static entry => entry.Kind == "request" && entry.Command == "terminate");
        }
        finally
        {
            await FixtureProcess.StopOwnedTargetAsync(target);
            if (slot is not null)
            {
                var close = slot.GetType().GetMethod("CloseAndDrainAsync", BindingFlags.Instance | BindingFlags.NonPublic)!;
                var cleanup = Assert.IsAssignableFrom<Task>(close.Invoke(slot, [CancellationToken.None]));
                await cleanup.WaitAsync(TimeSpan.FromSeconds(15));
                if (adapter is not null) await adapter.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(15));
                harness.Fixture.MarkCleanupConfirmed();
            }
            if (shutdown is not null)
            {
                var shutdownFailure = await Record.ExceptionAsync(() => shutdown.WaitAsync(TimeSpan.FromSeconds(15)));
                if (shutdownFailure is not null and not TimeoutException)
                    harness.AcceptObservedShutdownFailure(shutdownFailure);
            }
            adapter?.Dispose();
        }
    }

    [Fact]
    public async Task LegacyAttach_ConcurrentStopAndShutdownJoinOneDetach()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true, BlockGracefulShutdown: true));
        harness.EnableLegacyCompat();
        using var target = harness.Fixture.StartOwnedTarget();
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        using var shutdownCancellation = new CancellationTokenSource();
        Task? shutdown = null;
        try
        {
            var attached = Content(await harness.CallLegacyAsync("attach_debug", new JsonObject { ["process_id"] = target.Id }, "attach-shared-start"));
            Assert.True(attached["data"]?["success"]?.GetValue<bool>());
            using var adapter = Process.GetProcessById(await harness.Fixture.GetProcessIdAsync(observation.Token));
            var startup = Assert.Single(await harness.Fixture.ReadTranscriptAsync(), static entry => entry.Kind == "startup");
            Assert.Equal(startup.ProcessStartTimeUtcTicks, adapter.StartTime.ToUniversalTime().Ticks);
            var stop = harness.CallLegacyAsync("stop_debug", new JsonObject(), "attach-shared-stop");
            while (!(await harness.Fixture.ReadTranscriptAsync()).Any(static entry => entry.Kind == "request" && entry.Command == "disconnect"))
                await Task.Delay(10, observation.Token);
            shutdown = harness.StopAsync(shutdownCancellation.Token);
            shutdownCancellation.Cancel();
            Assert.False(adapter.HasExited);
            var pending = await harness.Fixture.ReadTranscriptAsync();
            var detach = JsonNode.Parse(Assert.Single(pending, static entry => entry.Kind == "request" && entry.Command == "disconnect").RawPayload!)!;
            Assert.False(detach["arguments"]?["terminateDebuggee"]?.GetValue<bool>());
            Assert.DoesNotContain(pending, static entry => entry.Kind == "request" && entry.Command == "terminate");
            harness.Fixture.ReleaseGracefulShutdown();
            var stopped = Content(await stop.WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.Contains("unconfirmed", stopped["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            Assert.False(target.HasExited);
            Assert.False(shutdown.IsCompleted);
            await FixtureProcess.StopOwnedTargetAsync(target);
            try { await shutdown.WaitAsync(TimeSpan.FromSeconds(10)); }
            catch (OperationCanceledException) when (shutdownCancellation.IsCancellationRequested) { }
            await adapter.WaitForExitAsync(observation.Token);
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "disconnect");
            Assert.DoesNotContain(transcript, static entry => entry.Kind == "request" && entry.Command == "terminate");
        }
        finally
        {
            harness.Fixture.ReleaseGracefulShutdown();
            await FixtureProcess.StopOwnedTargetAsync(target);
            if (shutdown is not null) await Record.ExceptionAsync(async () => await shutdown.WaitAsync(TimeSpan.FromSeconds(10)));
        }
    }

    [Fact]
    public async Task LegacyAttach_TargetExitDuringPendingHandshakeDoesNotForceAdapter()
    {
        if (!OperatingSystem.IsWindows()) return;
        var script = new JsonObject
        {
            ["disconnect"] = new JsonObject
            {
                ["entries"] = new JsonArray(),
                ["waitForRelease"] = true,
                ["releaseGate"] = "stackTrace",
            },
        };
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: true, SuppressLifecycleEvents: true),
            holdLaunchResponse: true, outputScript: script);
        harness.EnableLegacyCompat();
        using var target = harness.Fixture.StartOwnedTarget();
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        using var shutdownCancellation = new CancellationTokenSource();
        var attach = harness.CallLegacyAsync("attach_debug", new JsonObject { ["process_id"] = target.Id }, "attach-pending-start");
        Task? shutdown = null;
        Process? adapter = null;
        try
        {
            await harness.Fixture.WaitForTranscriptKindAsync("attach-gated", observation.Token);
            await harness.Fixture.WaitForTranscriptKindAsync("launch-response-gated", observation.Token);
            var startup = Assert.Single(await harness.Fixture.ReadTranscriptAsync(), static entry => entry.Kind == "startup");
            adapter = Process.GetProcessById(Assert.IsType<int>(startup.ProcessId));
            Assert.Equal(startup.ProcessStartTimeUtcTicks, adapter.StartTime.ToUniversalTime().Ticks);
            await FixtureProcess.StopOwnedTargetAsync(target);
            shutdown = harness.StopAsync(shutdownCancellation.Token);
            shutdownCancellation.Cancel();
            Assert.False(adapter.HasExited);
            harness.Fixture.ReleaseThreadsResponse();
            await harness.Fixture.WaitForTranscriptKindAsync("output-disconnect-gated", observation.Token);
            Assert.False(adapter.HasExited);
            var pending = await harness.Fixture.ReadTranscriptAsync();
            var detach = JsonNode.Parse(Assert.Single(pending, static entry => entry.Kind == "request" && entry.Command == "disconnect").RawPayload!)!;
            Assert.False(detach["arguments"]?["terminateDebuggee"]?.GetValue<bool>());
            Assert.DoesNotContain(pending, static entry => entry.Kind == "request" && entry.Command == "terminate");
            harness.Fixture.ReleaseStackTraceResponse();
            await harness.Fixture.WaitForTranscriptKindAsync("disconnect-response", observation.Token);
            await adapter.WaitForExitAsync(observation.Token);
            var completed = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(completed, static entry => entry.Kind == "request" && entry.Command == "disconnect");
        }
        finally
        {
            harness.Fixture.ReleaseThreadsResponse();
            harness.Fixture.ReleaseStackTraceResponse();
            await FixtureProcess.StopOwnedTargetAsync(target);
            await Record.ExceptionAsync(async () => await attach.WaitAsync(TimeSpan.FromSeconds(10)));
            if (shutdown is not null) await Record.ExceptionAsync(async () => await shutdown.WaitAsync(TimeSpan.FromSeconds(10)));
            adapter?.Dispose();
        }
    }

    [Fact]
    public async Task LegacyWriteMemory_AttachedStaleWriteReturnsUnknownWhileTargetLives()
    {
        if (!OperatingSystem.IsWindows()) return;
        var script = new JsonObject
        {
            ["breakpointCapabilities"] = new JsonObject { ["supportsWriteMemoryRequest"] = true },
            ["memoryWriteMode"] = "stale",
        };
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(LifecycleMode: "all-stop"), outputScript: script);
        harness.EnableLegacyCompat();
        using var target = harness.Fixture.StartOwnedTarget();
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        Task<CallToolResult>? pending = null;
        try
        {
            var attached = Content(await harness.CallLegacyAsync("attach_debug", new JsonObject { ["process_id"] = target.Id }, "write-attach-start"));
            Assert.True(attached["data"]?["success"]?.GetValue<bool>());
            await harness.Fixture.WaitForEventAsync("stopped", observation.Token);
            _ = await harness.CallLegacyAsync("get_threads", new JsonObject(), "write-attach-barrier");
            var before = (await harness.Fixture.ReadTranscriptAsync()).Count;
            pending = harness.CallLegacyAsync("write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID" }, "write-attach-stale");
            await harness.Fixture.WaitForTranscriptKindAsync("memory-write-response-gated", observation.Token);
            _ = await harness.CallLegacyAsync("get_threads", new JsonObject(), "write-attach-stale-barrier");
            harness.Fixture.ReleaseStackTraceResponse();
            var result = Content(await pending.WaitAsync(TimeSpan.FromSeconds(3)));
            var error = Assert.IsType<string>(result["error"]?.GetValue<string>());
            Assert.Contains("unknown", error, StringComparison.OrdinalIgnoreCase);
            Assert.Contains("do not retry", error, StringComparison.OrdinalIgnoreCase);
            Assert.False(result.ContainsKey("data"));
            Assert.False(target.HasExited);
            Assert.Single((await harness.Fixture.ReadTranscriptAsync()).Skip(before), static entry => entry.Kind == "request" && entry.Command == "writeMemory");
        }
        finally
        {
            harness.Fixture.ReleaseStackTraceResponse();
            await FixtureProcess.StopOwnedTargetAsync(target);
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "write-attach-drain").WaitAsync(TimeSpan.FromSeconds(10)));
            await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "write-attach-cleanup").WaitAsync(TimeSpan.FromSeconds(10)));
        }
    }

    [Fact]
    public async Task LegacyWriteMemory_CancelledBeforeAdmissionKeepsSessionAndDoesNotWrite()
    {
        var script = new JsonObject { ["breakpointCapabilities"] = new JsonObject { ["supportsWriteMemoryRequest"] = true } };
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(LifecycleMode: "all-stop"), outputScript: script);
        harness.EnableLegacyCompat();
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        using var caller = new CancellationTokenSource();
        Task<CallToolResult>? pending = null;
        SemaphoreSlim? admission = null;
        var held = false;
        try
        {
            var started = Content(await harness.CallLegacyAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath, ["pre_build"] = false }, "write-cancel-start"));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            await harness.Fixture.WaitForEventAsync("stopped", observation.Token);
            _ = await harness.CallLegacyAsync("get_threads", new JsonObject(), "write-cancel-barrier");
            var before = (await harness.Fixture.ReadTranscriptAsync()).Count;
            admission = harness.LegacySessionAdmissionGate;
            await admission.WaitAsync(observation.Token);
            held = true;
            pending = harness.CallLegacyAsync("write_memory", new JsonObject { ["memory_reference"] = "0x1234", ["data"] = "AQID" }, "write-cancel-before-admission", caller.Token);
            Assert.False(pending.IsCompleted);
            caller.Cancel();
            admission.Release();
            held = false;
            await Assert.ThrowsAnyAsync<OperationCanceledException>(async () => await pending.WaitAsync(observation.Token));
            var state = Content(await harness.CallLegacyAsync("get_threads", new JsonObject(), "write-cancel-still-usable").WaitAsync(observation.Token));
            Assert.NotNull(state["data"]);
            var after = (await harness.Fixture.ReadTranscriptAsync()).Skip(before).ToArray();
            Assert.DoesNotContain(after, static entry => entry.Kind == "request" && entry.Command is "writeMemory" or "terminate" or "disconnect");
        }
        finally
        {
            if (held) admission!.Release();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "write-cancel-cleanup").WaitAsync(TimeSpan.FromSeconds(10)));
        }
    }

    [Fact]
    public async Task LegacyAttach_CancelledBeforeAdmissionNeverWritesAttachLater()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            SupportsTerminate: true, SuppressLifecycleEvents: true, SetExceptionBreakpointsResponseMode: "hold-first"));
        harness.EnableLegacyCompat();
        using var target = harness.Fixture.StartOwnedTarget();
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        using var caller = new CancellationTokenSource();
        var attach = harness.CallLegacyAsync("attach_debug", new JsonObject { ["process_id"] = target.Id }, "attach-cancel-before-write", caller.Token);
        try
        {
            await harness.Fixture.WaitForTranscriptKindAsync("exception-filters-response-gated", observation.Token);
            caller.Cancel();
            var cancellation = await Record.ExceptionAsync(async () =>
            {
                var result = Content(await attach.WaitAsync(TimeSpan.FromSeconds(10)));
                Assert.NotNull(result["error"]);
            });
            Assert.True(cancellation is null or OperationCanceledException);
            harness.Fixture.ReleaseNextResponse();
            await harness.StopAsync(CancellationToken.None).WaitAsync(TimeSpan.FromSeconds(10));
            Assert.False(target.HasExited);
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            Assert.DoesNotContain(transcript, static entry => entry.Kind == "request" && entry.Command == "attach");
        }
        finally
        {
            harness.Fixture.ReleaseNextResponse();
            await FixtureProcess.StopOwnedTargetAsync(target);
            await Record.ExceptionAsync(async () => await attach.WaitAsync(TimeSpan.FromSeconds(10)));
        }
    }

    [Fact]
    public async Task LegacyAttach_ConfigurationRefused_RetainsUnconfirmedOwner()
    {
        if (!OperatingSystem.IsWindows()) return;
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: true, SuppressLifecycleEvents: true),
            outputScript: new JsonObject { ["configurationDoneRefused"] = true });
        harness.EnableLegacyCompat();
        using var target = harness.Fixture.StartOwnedTarget();
        try
        {
            var result = Content(await harness.CallLegacyAsync("attach_debug",
                new JsonObject { ["process_id"] = target.Id }, "attach-config-refused"));
            Assert.False(string.IsNullOrWhiteSpace(result["error"]?.GetValue<string>()));
            Assert.Null(result["data"]);
            Assert.False(target.HasExited);
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "attach");
            var detach = JsonNode.Parse(Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "disconnect").RawPayload!)!;
            Assert.False(detach["arguments"]?["terminateDebuggee"]?.GetValue<bool>());
            Assert.DoesNotContain(transcript, static entry => entry.Kind == "request" && entry.Command == "terminate");
        }
        finally
        {
            await FixtureProcess.StopOwnedTargetAsync(target);
            await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "attach-config-cleanup"));
        }
    }

    [Fact]
    public async Task LegacyResources_BlockedDeliveryCarriesClearAndDoesNotHoldLifecycle()
    {
        var script = new JsonObject
        {
            ["threads"] = new JsonArray(new JsonObject
            {
                ["entries"] = new JsonArray(new JsonObject { ["category"] = "stdout", ["output"] = "before-clear\n" }),
            })
        };
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(LifecycleMode: "all-stop"), outputScript: script);
        harness.EnableLegacyCompat();
        var channel = new DuplexChannel();
        using var updates = new SemaphoreSlim(0);
        var serverOptions = new McpServerOptions
        {
            ServerInfo = new Implementation { Name = "resource-regression", Version = "1.0.0" },
            Capabilities = new ServerCapabilities { Resources = new ResourcesCapability { Subscribe = true } },
            Handlers = new McpServerHandlers
            {
                ReadResourceHandler = (context, token) => harness.ReadResourceAsync(context.Params.Uri, token),
                SubscribeToResourcesHandler = (context, token) => harness.SubscribeResourceAsync(context.Server, context.Params.Uri, token),
            },
        };
        await using var resourceServer = McpServer.Create(channel.CreateServerTransport(), serverOptions);
        _ = resourceServer.RunAsync();
        var clientOptions = new McpClientOptions
        {
            ProtocolVersion = "2025-11-25",
            Handlers = new McpClientHandlers
            {
                NotificationHandlers = [new(NotificationMethods.ResourceUpdatedNotification, (notification, _) =>
                {
                    if (notification.Params?["uri"]?.GetValue<string>() == "debug://output") updates.Release();
                    return ValueTask.CompletedTask;
                })],
            },
        };
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(20));
        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport(), clientOptions, cancellationToken: observation.Token);
        async Task<JsonObject> RequestAsync(string method, string id) => ModernMcpProcessDriver.RequireResult(await client.SendRequestAsync(
            new JsonRpcRequest { Id = new RequestId(id), Method = method, Params = new JsonObject { ["uri"] = "debug://output" } }, observation.Token));
        _ = await RequestAsync("resources/subscribe", "resource-subscribe");
        var active = false;
        var gateHeld = false;
        try
        {
            var started = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, "resource-gated-start").WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            active = true;
            _ = await harness.CallLegacyAsync("get_threads", new JsonObject(), "resource-gated-output");
            Assert.True(await updates.WaitAsync(TimeSpan.FromSeconds(10)), "The subscriber must observe the initial output.");
            var before = await RequestAsync("resources/read", "resource-before-clear");
            Assert.Equal("before-clear\n", before["contents"]?[0]?["text"]?.GetValue<string>());

            await harness.ResourceSendGate.WaitAsync(observation.Token);
            gateHeld = true;
            while (updates.Wait(0)) { }
            var cleared = Content(await harness.CallLegacyAsync("get_output", new JsonObject { ["clear"] = true }, "resource-gated-clear"));
            Assert.Equal("before-clear\n", cleared["data"]?["output"]?.GetValue<string>());
            var stopped = Content(await harness.CallLegacyAsync("stop_debug", new JsonObject(), "resource-gated-stop").WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.True(stopped["data"]?["success"]?.GetValue<bool>());
            active = false;
            Environment.SetEnvironmentVariable("CONTROLLED_DAP_OUTPUT_SCRIPT", new JsonObject
            {
                ["launch"] = new JsonArray(new JsonObject { ["category"] = "stdout", ["output"] = "failed-launch\n" }),
                ["launchRefused"] = true,
            }.ToJsonString());
            var failed = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, "resource-gated-failed-start").WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.NotNull(failed["error"]);
            Assert.Equal("idle", Content(await harness.CallLegacyAsync("get_debug_state", new JsonObject(), "resource-gated-failed-idle"))["state"]?.GetValue<string>());
            Assert.False(updates.Wait(0), "No invalidation can be delivered while the send gate is held.");
            harness.ResourceSendGate.Release();
            gateHeld = false;
            Assert.True(await updates.WaitAsync(TimeSpan.FromSeconds(10)), "The subscriber must receive the carried output invalidation after the failed start.");
            var after = await RequestAsync("resources/read", "resource-after-failed-start");
            Assert.Equal(string.Empty, after["contents"]?[0]?["text"]?.GetValue<string>());
            Assert.False(await updates.WaitAsync(TimeSpan.FromSeconds(1)), "The refused launch must not publish its output.");
        }
        finally
        {
            if (gateHeld) harness.ResourceSendGate.Release();
            if (active) await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "resource-gated-cleanup").WaitAsync(TimeSpan.FromSeconds(10)));
        }
    }

    [Fact]
    public async Task LegacyState_StaleReaderFailureCallbackCannotInvalidateReplacement()
    {
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(SuppressLifecycleEvents: true));
        harness.EnableLegacyCompat();
        var channel = new DuplexChannel();
        using var updates = new SemaphoreSlim(0);
        var options = new McpServerOptions
        {
            ServerInfo = new Implementation { Name = "state-generation-regression", Version = "1.0.0" },
            Capabilities = new ServerCapabilities { Resources = new ResourcesCapability { Subscribe = true } },
            Handlers = new McpServerHandlers
            {
                ReadResourceHandler = (context, token) => harness.ReadResourceAsync(context.Params.Uri, token),
                SubscribeToResourcesHandler = (context, token) => harness.SubscribeResourceAsync(context.Server, context.Params.Uri, token),
            },
        };
        await using var resourceServer = McpServer.Create(channel.CreateServerTransport(), options);
        _ = resourceServer.RunAsync();
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(20));
        await using var client = await McpClient.CreateAsync(channel.CreateClientTransport(), new McpClientOptions
        {
            ProtocolVersion = "2025-11-25",
            Handlers = new McpClientHandlers
            {
                NotificationHandlers = [new(NotificationMethods.ResourceUpdatedNotification, (notification, _) =>
            {
                if (notification.Params?["uri"]?.GetValue<string>() == "debug://state") updates.Release();
                return ValueTask.CompletedTask;
            })]
            },
        }, cancellationToken: observation.Token);
        var launch = new JsonObject { ["program"] = harness.ProgramPath, ["pre_build"] = false };
        var active = false;
        try
        {
            Assert.True(Content(await harness.CallLegacyAsync("start_debug", launch, "stale-state-first"))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var oldObserver = harness.CaptureResourceObserver();
            Assert.True(Content(await harness.CallLegacyAsync("stop_debug", new JsonObject(), "stale-state-stop"))["data"]?["success"]?.GetValue<bool>());
            active = false;
            Assert.True(Content(await harness.CallLegacyAsync("start_debug", launch, "stale-state-replacement"))["data"]?["success"]?.GetValue<bool>());
            active = true;
            _ = ModernMcpProcessDriver.RequireResult(await client.SendRequestAsync(new JsonRpcRequest
            {
                Id = new RequestId("stale-state-subscribe"),
                Method = "resources/subscribe",
                Params = new JsonObject { ["uri"] = "debug://state" },
            }, observation.Token));
            oldObserver("state");
            Assert.False(await updates.WaitAsync(TimeSpan.FromSeconds(1)), "A late old-generation reader failure must not notify the replacement subscriber.");
            var state = await harness.ReadResourceAsync("debug://state", observation.Token);
            var text = Assert.IsType<TextResourceContents>(Assert.Single(state.Contents));
            Assert.NotEqual("idle", JsonNode.Parse(text.Text)!["state"]?.GetValue<string>());
        }
        finally
        {
            if (active) await harness.CallLegacyAsync("stop_debug", new JsonObject(), "stale-state-cleanup");
        }
    }

    [Fact]
    public async Task LegacyState_NaturalExitRemainsTerminalUntilExplicitStop()
    {
        var script = new JsonObject
        {
            ["continue"] = new JsonObject { ["entries"] = new JsonArray(), ["terminal"] = "exited" },
        };
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop", ExitCode: 23), outputScript: script);
        using var observation = new CancellationTokenSource(ObservationTimeout);
        var started = false;
        var stopped = false;
        try
        {
            var launch = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, "legacy-terminal-start"));
            Assert.True(launch["data"]?["success"]?.GetValue<bool>());
            started = true;
            var token = harness.LegacySessionId;
            await harness.Fixture.WaitForEventAsync("stopped", observation.Token);
            Assert.Equal("threads_success", Content(await harness.CallAsync("get_threads", SessionArguments(token), "legacy-terminal-reader-barrier"))["kind"]?.GetValue<string>());

            var continued = Content(await harness.CallLegacyAsync("continue_execution", new JsonObject(), "legacy-terminal-continue").WaitAsync(ObservationTimeout));
            Assert.Equal("terminated", continued["state"]?.GetValue<string>());
            Assert.Equal(23, continued["exit_code"]?.GetValue<int>());
            var retired = await harness.CallAsync("get_threads", SessionArguments(token), "legacy-terminal-retired");
            Assert.True(retired.IsError == true);
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", Content(retired)["error"]?.GetValue<string>());

            var terminal = Content(await harness.CallLegacyAsync("get_debug_state", new JsonObject(), "legacy-terminal-state"));
            Assert.Equal("terminated", terminal["state"]?.GetValue<string>());
            Assert.Equal("terminated", terminal["data"]?["state"]?.GetValue<string>());
            Assert.Equal("terminated", terminal["data"]?["execState"]?.GetValue<string>());
            Assert.Equal(23, terminal["data"]?["exitCode"]?.GetValue<int>());

            var stop = Content(await harness.CallLegacyAsync("stop_debug", new JsonObject(), "legacy-terminal-stop"));
            Assert.True(stop["data"]?["success"]?.GetValue<bool>());
            stopped = true;
            var idle = Content(await harness.CallLegacyAsync("get_debug_state", new JsonObject(), "legacy-terminal-idle"));
            Assert.Equal("idle", idle["state"]?.GetValue<string>());
            Assert.Equal("idle", idle["data"]?["state"]?.GetValue<string>());
            Assert.Null(idle["data"]?["exitCode"]);
        }
        finally
        {
            if (started && !stopped)
                await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "legacy-terminal-cleanup").WaitAsync(ObservationTimeout));
        }
    }

    [Fact]
    public async Task LegacyState_ObservedExitRemainsTerminalWhileCleanupIsHeld()
    {
        var script = new JsonObject
        {
            ["continue"] = new JsonObject { ["entries"] = new JsonArray(), ["terminal"] = "exited" },
            ["disconnect"] = new JsonObject { ["entries"] = new JsonArray(), ["waitForRelease"] = true },
        };
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop", ExitCode: 23), outputScript: script);
        using var observation = new CancellationTokenSource(ObservationTimeout);
        Task<CallToolResult>? continued = null;
        var started = false;
        try
        {
            var launch = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, "legacy-held-terminal-start"));
            Assert.True(launch["data"]?["success"]?.GetValue<bool>());
            started = true;
            var token = harness.LegacySessionId;
            await harness.Fixture.WaitForEventAsync("stopped", observation.Token);
            Assert.Equal("threads_success", Content(await harness.CallAsync("get_threads", SessionArguments(token), "legacy-held-terminal-reader"))["kind"]?.GetValue<string>());
            continued = harness.CallLegacyAsync("continue_execution", new JsonObject(), "legacy-held-terminal-continue");
            await harness.Fixture.WaitForTranscriptKindAsync("output-disconnect-gated", observation.Token);
            Assert.False(continued.IsCompleted);
            var retired = await harness.CallAsync("get_threads", SessionArguments(token), "legacy-held-terminal-retired");
            Assert.True(retired.IsError == true);
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", Content(retired)["error"]?.GetValue<string>());
            var state = Content(await harness.CallLegacyAsync("get_debug_state", new JsonObject(), "legacy-held-terminal-state"));
            Assert.Equal("terminated", state["state"]?.GetValue<string>());
            Assert.Equal(23, state["data"]?["exitCode"]?.GetValue<int>());
        }
        finally
        {
            harness.Fixture.ReleaseThreadsResponse();
            if (continued is not null) await Record.ExceptionAsync(async () => await continued.WaitAsync(ObservationTimeout));
            if (started) await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "legacy-held-terminal-cleanup").WaitAsync(ObservationTimeout));
        }
    }

    [Fact]
    public async Task LegacyState_UnexpectedAdapterEofDoesNotInventTermination()
    {
        var script = new JsonObject
        {
            ["continue"] = new JsonObject { ["entries"] = new JsonArray(), ["eof"] = true },
        };
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"), outputScript: script);
        using var observation = new CancellationTokenSource(ObservationTimeout);
        var started = false;
        try
        {
            var launch = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, "legacy-eof-start"));
            Assert.True(launch["data"]?["success"]?.GetValue<bool>());
            started = true;
            var token = harness.LegacySessionId;
            await harness.Fixture.WaitForEventAsync("stopped", observation.Token);
            Assert.Equal("threads_success", Content(await harness.CallAsync("get_threads", SessionArguments(token), "legacy-eof-reader"))["kind"]?.GetValue<string>());
            var continued = Content(await harness.CallLegacyAsync("continue_execution", new JsonObject(), "legacy-eof-continue").WaitAsync(ObservationTimeout));
            Assert.NotNull(continued["error"]);
            var retired = await harness.CallAsync("get_threads", SessionArguments(token), "legacy-eof-retired");
            Assert.True(retired.IsError == true);
            var state = Content(await harness.CallLegacyAsync("get_debug_state", new JsonObject(), "legacy-eof-state"));
            Assert.Equal("unknown", state["state"]?.GetValue<string>());
            Assert.Null(state["data"]?["exitCode"]);
        }
        finally
        {
            if (started) await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "legacy-eof-cleanup").WaitAsync(ObservationTimeout));
        }
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LegacyBreakpoint_CancelBeforeAck_RetainsAdmittedIntentOnRestart(bool remove)
    {
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            LifecycleMode: "all-stop", SetBreakpointsResponseMode: "hold-first-runtime"));
        using var observation = new CancellationTokenSource(TimeSpan.FromSeconds(10));
        using var caller = new CancellationTokenSource();
        var active = false;
        Task<CallToolResult>? pending = null;
        try
        {
            var source = harness.ProgramPath;
            Assert.False(Content(await harness.CallLegacyAsync("add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 2 }, "cancel-unknown-idle"))["data"]?["verified"]?.GetValue<bool>());
            if (remove)
                Assert.False(Content(await harness.CallLegacyAsync("add_breakpoint", new JsonObject { ["file"] = source, ["line"] = 6 }, "cancel-unknown-idle-6"))["data"]?["verified"]?.GetValue<bool>());
            var launch = new JsonObject { ["program"] = source, ["pre_build"] = false, ["stop_at_entry"] = true };
            Assert.True(Content(await harness.CallLegacyAsync("start_debug", launch, "cancel-unknown-start"))["data"]?["success"]?.GetValue<bool>());
            active = true;
            pending = harness.CallLegacyAsync(remove ? "remove_breakpoint" : "add_breakpoint", new JsonObject { ["file"] = source, ["line"] = remove ? 2 : 6 }, "cancel-unknown-update", caller.Token);
            await harness.Fixture.WaitForTranscriptKindAsync("runtime-breakpoints-response-gated", observation.Token);
            caller.Cancel();
            await Assert.ThrowsAnyAsync<OperationCanceledException>(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            Assert.True(Content(await harness.CallLegacyAsync("stop_debug", new JsonObject(), "cancel-unknown-stop"))["data"]?["success"]?.GetValue<bool>());
            active = false;
            var beforeRestart = (await harness.Fixture.ReadTranscriptAsync()).Count;
            Assert.True(Content(await harness.CallLegacyAsync("start_debug", launch, "cancel-unknown-restart"))["data"]?["success"]?.GetValue<bool>());
            active = true;
            var replay = Assert.Single((await harness.Fixture.ReadTranscriptAsync()).Skip(beforeRestart),
                static entry => entry.Kind == "request" && entry.Command == "setBreakpoints");
            using var message = JsonDocument.Parse(Assert.IsType<string>(replay.RawPayload));
            Assert.Equal(remove ? new[] { 6 } : new[] { 2, 6 }, message.RootElement.GetProperty("arguments").GetProperty("breakpoints")
                .EnumerateArray().Select(static breakpoint => breakpoint.GetProperty("line").GetInt32()));
        }
        finally
        {
            harness.Fixture.ReleaseNextResponse();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(TimeSpan.FromSeconds(10)));
            if (active) await Record.ExceptionAsync(async () => await harness.CallLegacyAsync("stop_debug", new JsonObject(), "cancel-unknown-cleanup").WaitAsync(ObservationTimeout));
        }
    }

    [Theory]
    [InlineData("get_call_stack", false)]
    [InlineData("get_call_stack", true)]
    [InlineData("get_scopes", false)]
    [InlineData("get_variables", false)]
    [InlineData("evaluate_expression", false)]
    [InlineData("get_variables", true)]
    public async Task LegacyInspection_ReaderAcceptedResponseBeforeStep_IsNotPublishedAfterEpochChanges(string tool, bool nativeStep)
    {
        await using var harness = await RegistryHostHarness.CreateAsync(new FixtureConfiguration(
            LifecycleMode: "all-stop", SupportsDelayedStackTraceLoading: true,
            StackTraceResponseMode: tool == "get_call_stack" ? "hold" : "success",
            ScopesResponseMode: tool == "get_scopes" ? "hold" : "success",
            VariablesResponseMode: tool == "get_variables" ? "hold" : "success", PauseResponseMode: "negative"));
        using var observation = new CancellationTokenSource(ObservationTimeout);
        Task<CallToolResult>? pending = null;
        Task<CallToolResult>? step = null;
        var gateHeld = false;
        try
        {
            var started = Content(await harness.CallLegacyAsync("start_debug", new JsonObject
            {
                ["program"] = harness.ProgramPath,
                ["pre_build"] = false,
                ["stop_at_entry"] = true,
            }, "legacy-epoch-start"));
            Assert.True(started["data"]?["success"]?.GetValue<bool>());
            var token = harness.LegacySessionId;
            await harness.Fixture.WaitForEventAsync("stopped", observation.Token);
            Assert.Equal("threads_success", Content(await harness.CallAsync("get_threads", SessionArguments(token), "epoch-initial-barrier"))["kind"]?.GetValue<string>());
            var frame = 0;
            if (tool != "get_call_stack")
            {
                var stack = Content(await harness.CallLegacyAsync("get_call_stack", new JsonObject(), "epoch-stack"));
                var frames = Assert.IsType<JsonArray>(stack["data"]?["frames"]);
                Assert.NotEmpty(frames);
                frame = frames[0]!["id"]!.GetValue<int>();
            }
            var command = tool switch { "get_call_stack" => "stackTrace", "get_scopes" => "scopes", "get_variables" => "variables", _ => "evaluate" };
            var arguments = new JsonObject();
            if (tool == "get_variables")
            {
                var scopes = Content(await harness.CallLegacyAsync("get_scopes", new JsonObject(), "epoch-scopes"));
                arguments["variables_reference"] = Assert.IsType<JsonArray>(scopes["data"])[0]!["variablesReference"]!.GetValue<int>();
            }
            else if (tool == "evaluate_expression")
            {
                arguments["expression"] = "fixture:hold";
                arguments["frame_id"] = frame;
            }
            pending = harness.CallLegacyAsync(tool, arguments, "epoch-inspection");
            var gated = tool switch { "get_call_stack" => "stack-trace-response-gated", "evaluate_expression" => "evaluate-response-gated", _ => "inspection-response-gated" };
            await harness.Fixture.WaitForTranscriptKindAsync(gated, observation.Token);
            var request = Assert.Single(await harness.Fixture.ReadTranscriptAsync(), entry => entry.Kind == "request" && entry.Command == command);
            var sequence = Assert.IsType<int>(request.Sequence);

            await harness.LegacyGate.WaitAsync(observation.Token);
            gateHeld = true;
            if (!nativeStep)
            {
                step = harness.CallLegacyAsync("step_over", new JsonObject(), "epoch-step");
                Assert.False(step.IsCompleted);
            }
            harness.Fixture.ReleaseStackTraceResponse();
            await harness.Fixture.WaitForDapResponseAsync(command, sequence, observation.Token);
            var barrier = Content(await harness.CallAsync("get_threads", SessionArguments(token), "epoch-reader-barrier", observation.Token));
            Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
            Assert.False(pending.IsCompleted, "The accepted E1 response must still be waiting for legacy admission.");
            if (nativeStep)
            {
                step = harness.CallAsync("step_over", SessionArguments(token), "native-epoch-step", observation.Token);
                Assert.Equal("step_over_success", Content(await step.WaitAsync(ObservationTimeout))["kind"]?.GetValue<string>());
            }
            else
            {
                Assert.False(step!.IsCompleted, "Step admission must remain queued ahead of publication.");
            }
            harness.LegacyGate.Release();
            gateHeld = false;

            var result = Content(await pending.WaitAsync(ObservationTimeout));
            var stepped = Content(await step!.WaitAsync(ObservationTimeout));
            if (!nativeStep) Assert.Equal("step", stepped["reason"]?.GetValue<string>());
            Assert.NotNull(result["error"]);
            Assert.False(result.ContainsKey("data"), "E1 inspection data must not be published after E2 admission.");
            if (tool == "evaluate_expression")
            {
                Assert.Contains("may have executed", result["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
                Assert.Contains("do not retry", result["error"]?.GetValue<string>(), StringComparison.OrdinalIgnoreCase);
            }
        }
        finally
        {
            if (gateHeld) harness.LegacyGate.Release();
            harness.Fixture.ReleaseStackTraceResponse();
            if (pending is not null) await Record.ExceptionAsync(async () => await pending.WaitAsync(ObservationTimeout));
            if (step is not null) await Record.ExceptionAsync(async () => await step.WaitAsync(ObservationTimeout));
            await harness.StopAsync(CancellationToken.None);
        }
    }

    [Fact]
    public async Task ShutdownCancellation_DuringUnpublishedStart_CancelsStartupAndWaitsForOwnedAdapterExit()
    {
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SuppressLifecycleEvents: true), holdLaunchResponse: true);
        using var shutdownCancellation = new CancellationTokenSource();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        var start = harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath }, "unpublished-start", CancellationToken.None);
        Task? shutdown = null;
        Task<ShutdownObservation>? shutdownObservation = null;
        Process? adapter = null;
        try
        {
            await harness.Fixture.WaitForTranscriptKindAsync("launch-gated", observation.Token);
            await harness.Fixture.WaitForTranscriptKindAsync("launch-response-gated", observation.Token);
            adapter = Process.GetProcessById(await harness.Fixture.GetProcessIdAsync(observation.Token));
            Assert.False(adapter.HasExited);
            Assert.False(start.IsCompleted, "The admitted start must remain unpublished while its launch response is held.");

            // This is the actual SessionDisposer. Its token is independent of the uncancelled MCP caller.
            shutdown = harness.StopAsync(shutdownCancellation.Token);
            Assert.False(shutdown.IsCompleted, "Shutdown must join the admitted start before reporting completion.");
            shutdownObservation = ObserveShutdownAsync(shutdown, adapter);
            shutdownCancellation.Cancel();

            var observationFailure = await Record.ExceptionAsync(async () =>
            {
                await shutdownObservation.WaitAsync(ObservationTimeout);
            });
            Assert.False(observationFailure is TimeoutException,
                "Shutdown ignored its cancelled token while an admitted, unpublished start remained at the launch-response barrier.");
            Assert.Null(observationFailure);
            var completed = await shutdownObservation;
            Assert.IsAssignableFrom<OperationCanceledException>(completed.Failure);
            Assert.True(shutdown.IsCanceled, "The actual shutdown task must be cancelled, not merely its observation.");
            Assert.True(completed.AdapterExited,
                "Shutdown cancellation must not be reported until the exact owned adapter has exited.");
            Assert.DoesNotContain(await harness.Fixture.ReadTranscriptAsync(), static entry => entry.Kind == "launch-released");

            await AssertStartDidNotPublishAsync(start);
        }
        finally
        {
            // Baseline RED times out above. Releasing launch lets normal startup and unpublished cleanup finish.
            harness.Fixture.ReleaseThreadsResponse();
            await Record.ExceptionAsync(async () => { await start; });
            await Record.ExceptionAsync(() => shutdown ?? harness.StopAsync(CancellationToken.None));
            if (shutdownObservation is not null)
            {
                await shutdownObservation;
            }
            if (adapter is not null)
            {
                try
                {
                    await adapter.WaitForExitAsync().WaitAsync(ObservationTimeout);
                }
                finally
                {
                    adapter.Dispose();
                }
            }
        }
    }

    [Fact]
    public async Task Shutdown_AfterAutomaticRetirementClosesAdmission_JoinsHeldCleanupBeforeOwnedAdapterExit()
    {
        var script = new JsonObject
        {
            ["continue"] = new JsonObject { ["entries"] = new JsonArray(), ["terminal"] = "terminated" },
            ["disconnect"] = new JsonObject { ["entries"] = new JsonArray(), ["waitForRelease"] = true },
        };
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: false, LifecycleMode: "all-stop"), outputScript: script);
        using var observation = new CancellationTokenSource(ObservationTimeout);
        var started = Content(await harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath }, "retiring-start"));
        Assert.Equal("start_debug_success", started["kind"]?.GetValue<string>());
        var token = Assert.IsType<string>(started["debugSessionId"]?.GetValue<string>());
        using var adapter = Process.GetProcessById(await harness.Fixture.GetProcessIdAsync(observation.Token));
        Task<CallToolResult>? continued = null;
        Task? shutdown = null;
        Task<ShutdownObservation>? shutdownObservation = null;
        try
        {
            await harness.Fixture.WaitForEventAsync("stopped", observation.Token);
            var barrier = Content(await harness.CallAsync("get_threads", SessionArguments(token), "retiring-reader-barrier"));
            Assert.Equal("threads_success", barrier["kind"]?.GetValue<string>());
            var arguments = SessionArguments(token);
            arguments["threadId"] = 1;
            continued = harness.CallAsync("continue_execution", arguments, "retiring-continue");
            await harness.Fixture.WaitForTranscriptKindAsync("output-disconnect-gated", observation.Token);
            Assert.False(continued.IsCompleted);
            Assert.False(adapter.HasExited);
            var retired = await harness.CallAsync("get_threads", SessionArguments(token), "retiring-closed-admission");
            Assert.True(retired.IsError == true);
            Assert.Equal("DEBUG_SESSION_NOT_FOUND", Content(retired)["error"]?.GetValue<string>());

            // No SDK transport or in-flight-handler disposal participates in this join.
            shutdown = harness.StopAsync(CancellationToken.None);
            Assert.False(shutdown.IsCompleted,
                "SessionDisposer must join auto-retiring cleanup even after live debugger admission has closed.");
            shutdownObservation = ObserveShutdownAsync(shutdown, adapter);
            harness.Fixture.ReleaseThreadsResponse();

            Assert.Equal("continue_terminal", Content(await continued.WaitAsync(ObservationTimeout))["kind"]?.GetValue<string>());
            var completed = await shutdownObservation.WaitAsync(ObservationTimeout);
            Assert.Null(completed.Failure);
            Assert.True(completed.AdapterExited, "Shutdown must finish only after the exact owned adapter exits.");
            var transcript = await harness.Fixture.ReadTranscriptAsync();
            Assert.Single(transcript, static entry => entry.Kind == "request" && entry.Command == "disconnect");
            Assert.DoesNotContain(transcript, static entry => entry.Kind == "request" && entry.Command == "terminate");
        }
        finally
        {
            harness.Fixture.ReleaseThreadsResponse();
            if (continued is not null)
            {
                await Record.ExceptionAsync(async () => { await continued; });
            }
            await Record.ExceptionAsync(() => shutdown ?? harness.StopAsync(CancellationToken.None));
            if (shutdownObservation is not null)
            {
                await shutdownObservation;
            }
            await adapter.WaitForExitAsync().WaitAsync(ObservationTimeout);
        }
    }

    [Fact]
    public async Task ShutdownCancellation_AfterRejectedPublication_ForcesHeldUnpublishedSessionCleanup()
    {
        var script = new JsonObject
        {
            ["disconnect"] = new JsonObject
            {
                ["entries"] = new JsonArray(),
                ["waitForRelease"] = true,
                ["releaseGate"] = "stackTrace",
            },
        };
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SupportsTerminate: false, SuppressLifecycleEvents: true),
            holdLaunchResponse: true, outputScript: script);
        using var shutdownCancellation = new CancellationTokenSource();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        var start = harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath },
            "rejected-publication-start", CancellationToken.None);
        Task? shutdown = null;
        Task<ShutdownObservation>? shutdownObservation = null;
        Process? adapter = null;
        try
        {
            await harness.Fixture.WaitForTranscriptKindAsync("launch-response-gated", observation.Token);
            adapter = Process.GetProcessById(await harness.Fixture.GetProcessIdAsync(observation.Token));
            Assert.False(adapter.HasExited);
            Assert.False(start.IsCompleted);
            shutdown = harness.StopAsync(shutdownCancellation.Token);
            Assert.False(shutdown.IsCompleted);

            // Startup finishes with no cancellation, but shutdown has already barred publication.
            harness.Fixture.ReleaseThreadsResponse();
            await harness.Fixture.WaitForTranscriptKindAsync("launch-released", observation.Token);
            await harness.Fixture.WaitForTranscriptKindAsync("output-disconnect-gated", observation.Token);
            Assert.False(start.IsCompleted, "The rejected start must still own its held unpublished cleanup.");
            Assert.False(shutdown.IsCompleted, "Shutdown must join the unpublished session's cleanup.");
            Assert.False(adapter.HasExited);

            shutdownObservation = ObserveShutdownAsync(shutdown, adapter);
            shutdownCancellation.Cancel();
            await AssertCancelledAfterOwnedExitAsync(shutdown, shutdownObservation,
                "Shutdown cancellation did not force cleanup after native startup returned and publication was rejected.");
            Assert.DoesNotContain(await harness.Fixture.ReadTranscriptAsync(),
                static entry => entry.Kind == "output-disconnect-written");
            await AssertStartDidNotPublishAsync(start);
        }
        finally
        {
            // The independent cleanup barrier remains closed throughout the cancellation assertion.
            harness.Fixture.ReleaseThreadsResponse();
            harness.Fixture.ReleaseStackTraceResponse();
            await Record.ExceptionAsync(async () => { await start; });
            await Record.ExceptionAsync(() => shutdown ?? harness.StopAsync(CancellationToken.None));
            if (shutdownObservation is not null)
            {
                await shutdownObservation;
            }
            if (adapter is not null)
            {
                try
                {
                    await adapter.WaitForExitAsync().WaitAsync(ObservationTimeout);
                }
                finally
                {
                    adapter.Dispose();
                }
            }
        }
    }

    [Fact]
    public async Task ShutdownCancellation_FromSecondCaller_CancelsSharedShutdownAndUnpublishedStart()
    {
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SuppressLifecycleEvents: true), holdLaunchResponse: true);
        using var firstCancellation = new CancellationTokenSource();
        using var secondCancellation = new CancellationTokenSource();
        secondCancellation.Cancel();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        var start = harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath },
            "second-shutdown-caller-start", CancellationToken.None);
        Task? firstShutdown = null;
        Task? secondShutdown = null;
        Task<ShutdownObservation>? firstObservation = null;
        Task<ShutdownObservation>? secondObservation = null;
        Process? adapter = null;
        try
        {
            await harness.Fixture.WaitForTranscriptKindAsync("launch-response-gated", observation.Token);
            adapter = Process.GetProcessById(await harness.Fixture.GetProcessIdAsync(observation.Token));
            Assert.False(adapter.HasExited);
            Assert.False(start.IsCompleted);
            firstShutdown = harness.StopAsync(firstCancellation.Token);
            Assert.False(firstShutdown.IsCompleted);
            firstObservation = ObserveShutdownAsync(firstShutdown, adapter);

            // The first token is cancellable but remains uncancelled; only the joining caller requests cancellation.
            Assert.False(firstCancellation.IsCancellationRequested);
            secondShutdown = harness.StopAsync(secondCancellation.Token);
            secondObservation = ObserveShutdownAsync(secondShutdown, adapter);
            await AssertCancelledAfterOwnedExitAsync(firstShutdown, firstObservation,
                "The already-cancelled second caller did not cancel the shared shutdown or its unpublished start.");
            await AssertCancelledAfterOwnedExitAsync(secondShutdown, secondObservation,
                "The joining shutdown caller did not observe cancellation after owned process cleanup.");
            Assert.False(firstCancellation.IsCancellationRequested);
            Assert.DoesNotContain(await harness.Fixture.ReadTranscriptAsync(), static entry => entry.Kind == "launch-released");
            await AssertStartDidNotPublishAsync(start);
        }
        finally
        {
            harness.Fixture.ReleaseThreadsResponse();
            await Record.ExceptionAsync(async () => { await start; });
            await Record.ExceptionAsync(() => firstShutdown ?? harness.StopAsync(CancellationToken.None));
            if (secondShutdown is not null)
            {
                await Record.ExceptionAsync(() => secondShutdown);
            }
            if (firstObservation is not null)
            {
                await firstObservation;
            }
            if (secondObservation is not null)
            {
                await secondObservation;
            }
            if (adapter is not null)
            {
                try
                {
                    await adapter.WaitForExitAsync().WaitAsync(ObservationTimeout);
                }
                finally
                {
                    adapter.Dispose();
                }
            }
        }
    }

    [Fact]
    public async Task ShutdownCancellation_FailedNativeStartupCleanupFault_PreservesCancellationAndExactCleanupFailure()
    {
        Process? adapter = null;
        FixtureProcess? completedFixture = null;
        var cleanupCompleted = false;
        var cleanupFailure = new IOException("controlled failed-start cleanup sentinel");
        Action afterFailedStartCleanup = () =>
        {
            var ownedAdapter = Assert.IsType<Process>(adapter);
            ownedAdapter.Refresh();
            Assert.True(ownedAdapter.HasExited, "The failure seam must run only after real owned adapter cleanup completes.");
            cleanupCompleted = true;
            (completedFixture ?? throw new InvalidOperationException("The owned fixture is unavailable.")).MarkCleanupConfirmed();
            throw cleanupFailure;
        };
        await using var harness = await RegistryHostHarness.CreateAsync(
            new FixtureConfiguration(SuppressLifecycleEvents: true), holdLaunchResponse: true,
            failedStartCleanupCompleted: afterFailedStartCleanup);
        completedFixture = harness.Fixture;
        using var shutdownCancellation = new CancellationTokenSource();
        using var observation = new CancellationTokenSource(ObservationTimeout);
        var start = harness.CallAsync("start_debug", new JsonObject { ["program"] = harness.ProgramPath },
            "failed-start-cleanup-fault", CancellationToken.None);
        Task? shutdown = null;
        Task<ShutdownObservation>? shutdownObservation = null;
        try
        {
            await harness.Fixture.WaitForTranscriptKindAsync("launch-response-gated", observation.Token);
            adapter = Process.GetProcessById(await harness.Fixture.GetProcessIdAsync(observation.Token));
            Assert.False(adapter.HasExited);
            Assert.False(start.IsCompleted);
            shutdown = harness.StopAsync(shutdownCancellation.Token);
            Assert.False(shutdown.IsCompleted);
            shutdownObservation = ObserveShutdownAsync(shutdown, adapter);
            shutdownCancellation.Cancel();

            var observationFailure = await Record.ExceptionAsync(async () =>
            {
                await shutdownObservation.WaitAsync(ObservationTimeout);
            });
            Assert.Null(observationFailure);
            var completed = await shutdownObservation;
            Assert.True(cleanupCompleted, "The raw cleanup sentinel must be thrown after real failed-start cleanup.");
            Assert.True(completed.AdapterExited);
            Assert.True(shutdown.IsFaulted,
                "A failed native-start cleanup must fault shared shutdown, not be replaced by shutdown cancellation.");
            Assert.False(shutdown.IsCanceled);
            var failures = FailureTree(Assert.IsType<AggregateException>(shutdown.Exception)).ToArray();
            Assert.Contains(failures, failure => ReferenceEquals(failure, cleanupFailure));
            Assert.Contains(failures, static failure => failure is OperationCanceledException cancellation
                && cancellation.CancellationToken.IsCancellationRequested);
            Assert.DoesNotContain(await harness.Fixture.ReadTranscriptAsync(), static entry => entry.Kind == "launch-released");
            await AssertStartDidNotPublishAsync(start);
            harness.AcceptObservedShutdownFailure(Assert.IsAssignableFrom<Exception>(completed.Failure));
        }
        finally
        {
            harness.Fixture.ReleaseThreadsResponse();
            await Record.ExceptionAsync(async () => { await start; });
            await Record.ExceptionAsync(() => shutdown ?? harness.StopAsync(CancellationToken.None));
            if (shutdownObservation is not null)
            {
                await shutdownObservation;
            }
            if (adapter is not null)
            {
                try
                {
                    await adapter.WaitForExitAsync().WaitAsync(ObservationTimeout);
                }
                finally
                {
                    adapter.Dispose();
                }
            }
        }
    }

    private static IEnumerable<Exception> FailureTree(Exception failure)
    {
        yield return failure;
        if (failure is AggregateException aggregate)
        {
            foreach (var inner in aggregate.InnerExceptions)
            {
                foreach (var nested in FailureTree(inner))
                {
                    yield return nested;
                }
            }
        }
        else if (failure.InnerException is { } inner)
        {
            foreach (var nested in FailureTree(inner))
            {
                yield return nested;
            }
        }
    }

    private static async Task AssertCancelledAfterOwnedExitAsync(
        Task shutdown, Task<ShutdownObservation> observation, string timeoutMessage)
    {
        var observationFailure = await Record.ExceptionAsync(async () =>
        {
            await observation.WaitAsync(ObservationTimeout);
        });
        Assert.False(observationFailure is TimeoutException, timeoutMessage);
        Assert.Null(observationFailure);
        var completed = await observation;
        Assert.IsAssignableFrom<OperationCanceledException>(completed.Failure);
        Assert.True(shutdown.IsCanceled, "The shared shutdown task must itself be cancelled.");
        Assert.True(completed.AdapterExited, "Shutdown must not report cancellation before the exact owned adapter exits.");
    }

    private static async Task AssertStartDidNotPublishAsync(Task<CallToolResult> start)
    {
        try
        {
            var result = await start.WaitAsync(ObservationTimeout);
            Assert.True(result.IsError == true, "Shutdown must prevent a successful start result.");
            var content = Content(result);
            Assert.NotEqual("start_debug_success", content["kind"]?.GetValue<string>());
            Assert.False(content.ContainsKey("debugSessionId"));
        }
        catch (OperationCanceledException)
        {
            // Linked registry cancellation can abort NetCoreDbgSession.StartAsync before it returns a session.
        }
    }

    private static async Task<ShutdownObservation> ObserveShutdownAsync(Task shutdown, Process adapter)
    {
        var failure = await Record.ExceptionAsync(() => shutdown);
        adapter.Refresh();
        return new ShutdownObservation(failure, adapter.HasExited);
    }

    private static JsonObject SessionArguments(string token) => new() { ["debugSessionId"] = token };

    private static JsonObject Content(CallToolResult result) =>
        Assert.IsType<JsonObject>(JsonSerializer.SerializeToNode(result.StructuredContent));

    private sealed record ShutdownObservation(Exception? Failure, bool AdapterExited);

    private sealed class RegistryHostHarness(
        FixtureProcess fixture,
        IHost host,
        object registry,
        MethodInfo call,
        McpServer server,
        IHostedService disposer,
        string scratchDirectory,
        string programPath,
        string? previousLaunchGate,
        string? previousOutputScript) : IAsyncDisposable
    {
        private const string LaunchGateVariable = "CONTROLLED_DAP_HOLD_LAUNCH_RESPONSE";
        private const string OutputScriptVariable = "CONTROLLED_DAP_OUTPUT_SCRIPT";
        private Exception? _acceptedShutdownFailure;

        public void AcceptObservedShutdownFailure(Exception failure) => _acceptedShutdownFailure = failure;

        public FixtureProcess Fixture { get; } = fixture;
        public string ProgramPath { get; } = programPath;
        public object? OwnedSlot(string token)
        {
            var type = registry.GetType();
            var gate = type.GetField("_lifetimeGate", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(registry)!;
            lock (gate)
            {
                var owners = Assert.IsAssignableFrom<System.Collections.IDictionary>(type.GetField("_ownedSlots", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(registry));
                var ownership = owners[token];
                return ownership?.GetType().GetField("Item1")!.GetValue(ownership);
            }
        }

        public object SceneBinding(string token)
        {
            var bindings = registry.GetType().GetField("_nativeSceneBindings", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(registry)!;
            return bindings.GetType().GetProperty("Item")!.GetValue(bindings, [token])!;
        }

        public object UiOwner(string token)
        {
            var binding = SceneBinding(token);
            return binding.GetType().GetField("_uiInvoke", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(binding)!;
        }

        public static ValueTask DisposeSessionAsync(object session) =>
            (ValueTask)session.GetType().GetMethod("DisposeAsync", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)!.Invoke(session, [])!;

        public SemaphoreSlim LegacyGate => Assert.IsType<SemaphoreSlim>(registry.GetType()
            .GetField("_legacyGate", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(registry));
        public SemaphoreSlim ResourceSendGate
        {
            get
            {
                var owner = registry.GetType().GetField("_legacyResources", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(registry)!;
                return Assert.IsType<SemaphoreSlim>(owner.GetType().GetField("_sendGate", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(owner));
            }
        }
        public Action<string> CaptureResourceObserver()
        {
            var owner = registry.GetType().GetField("_legacyResources", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(registry)!;
            var generation = owner.GetType().GetField("_current", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(owner)!;
            var observe = generation.GetType().GetMethod("Observe", BindingFlags.Instance | BindingFlags.NonPublic)!;
            return name => observe.Invoke(generation, [name]);
        }
        public void EnableLegacyCompat() => registry.GetType().GetProperty("LegacyCompat", BindingFlags.Instance | BindingFlags.NonPublic)!.SetValue(registry, true);
        public ValueTask<ReadResourceResult> ReadResourceAsync(string uri, CancellationToken cancellationToken) =>
            (ValueTask<ReadResourceResult>)registry.GetType().GetMethod("ReadLegacyResourceAsync", BindingFlags.Instance | BindingFlags.NonPublic)!
                .Invoke(registry, [uri, cancellationToken])!;
        public ValueTask<EmptyResult> SubscribeResourceAsync(McpServer resourceServer, string uri, CancellationToken cancellationToken) =>
            (ValueTask<EmptyResult>)registry.GetType().GetMethod("SubscribeLegacyResourceAsync", BindingFlags.Instance | BindingFlags.NonPublic)!
                .Invoke(registry, [resourceServer, uri, true, cancellationToken])!;
        public string LegacySessionId => Assert.IsType<string>(registry.GetType()
            .GetField("_legacySessionId", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(registry));
        public SemaphoreSlim LegacySessionAdmissionGate
        {
            get
            {
                var sessions = registry.GetType().GetField("_sessions", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(registry)!;
                var session = sessions.GetType().GetProperty("Item")!.GetValue(sessions, [LegacySessionId])!;
                return Assert.IsType<SemaphoreSlim>(session.GetType().GetField("_callStackAdmissionGate", BindingFlags.Instance | BindingFlags.NonPublic)!.GetValue(session));
            }
        }

        public static async Task<RegistryHostHarness> CreateAsync(
            FixtureConfiguration configuration, bool holdLaunchResponse = false, JsonObject? outputScript = null,
            Action? failedStartCleanupCompleted = null, Func<object, ValueTask>? disposeSession = null, Func<string, string>? debuggerPathFactory = null)
        {
            var previousLaunchGate = Environment.GetEnvironmentVariable(LaunchGateVariable);
            var previousOutputScript = Environment.GetEnvironmentVariable(OutputScriptVariable);
            var scratch = ModernMcpScratchDirectory.Create();
            FixtureProcess? fixture = null;
            IHost? host = null;
            try
            {
                Environment.SetEnvironmentVariable(LaunchGateVariable, holdLaunchResponse ? "1" : null);
                Environment.SetEnvironmentVariable(OutputScriptVariable, outputScript?.ToJsonString());
                fixture = FixtureProcess.Create(configuration);
                var program = Path.Combine(scratch, "controlled-program.dll");
                await File.WriteAllBytesAsync(program, []);
                var debuggerPath = debuggerPathFactory?.Invoke(scratch) ?? fixture.ExecutablePath;
                var assemblyPath = TestOutputPathResolver.ResolveManagedAssembly(
                    RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless");
                var assembly = AssemblyLoadContext.Default.LoadFromAssemblyPath(assemblyPath);
                var programType = assembly.GetType("NetCoreDbg.Mcp.Stateless.Program", throwOnError: true)!;
                var registryType = assembly.GetType("NetCoreDbg.Mcp.Stateless.Program+DebugSessionRegistry", throwOnError: true)!;
                object registry;
                if (disposeSession is not null)
                {
                    Assert.Null(failedStartCleanupCompleted);
                    var sessionType = assembly.GetType("NetCoreDbg.Mcp.Stateless.DebugAdapter.NetCoreDbgSession", throwOnError: true)!;
                    var bridge = typeof(ModernMcpRegistryContractDriver);
                    Func<object, bool> isUsable = session => (bool)sessionType.GetProperty("IsUsable", BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic)!.GetValue(session)!;
                    var usableDelegate = bridge.GetMethod("CreateIsUsableDelegate", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, [sessionType, isUsable]);
                    var disposerDelegate = bridge.GetMethod("CreateDisposerDelegate", BindingFlags.Static | BindingFlags.NonPublic)!.Invoke(null, [sessionType, disposeSession]);
                    registry = registryType.GetConstructor(BindingFlags.Instance | BindingFlags.NonPublic, binder: null,
                        types: [typeof(string), typeof(Func<,>).MakeGenericType(sessionType, typeof(bool)), typeof(Func<,>).MakeGenericType(sessionType, typeof(ValueTask))], modifiers: null)!
                        .Invoke([debuggerPath, usableDelegate, disposerDelegate]);
                }
                else
                {
                    registry = failedStartCleanupCompleted is null
                    ? Activator.CreateInstance(registryType, BindingFlags.Instance | BindingFlags.NonPublic,
                        binder: null, args: [debuggerPath], culture: null)!
                    : registryType.GetConstructor(BindingFlags.Instance | BindingFlags.NonPublic,
                        binder: null, types: [typeof(string), typeof(Action)], modifiers: null)!
                        .Invoke([debuggerPath, failedStartCleanupCompleted]);
                }
                var buildHost = programType.GetMethod("BuildHost", BindingFlags.Static | BindingFlags.NonPublic,
                    binder: null, types: [registryType], modifiers: null)!;
                host = (IHost)buildHost.Invoke(null, [registry])!;
                // Resolve the real SDK server without starting the stdio transport or consuming test-runner stdin.
                var server = host.Services.GetRequiredService<McpServer>();
                var call = registryType.GetMethod("CallAsync", BindingFlags.Instance | BindingFlags.NonPublic,
                    binder: null, types: [typeof(RequestContext<CallToolRequestParams>), typeof(CancellationToken)], modifiers: null)!;
                var disposerType = assembly.GetType("NetCoreDbg.Mcp.Stateless.Program+SessionDisposer", throwOnError: true)!;
                var disposer = (IHostedService)Activator.CreateInstance(disposerType, BindingFlags.Instance | BindingFlags.Public | BindingFlags.NonPublic,
                    binder: null, args: [registry], culture: null)!;
                return new RegistryHostHarness(fixture, host, registry, call, server, disposer, scratch, program,
                    previousLaunchGate, previousOutputScript);
            }
            catch
            {
                try
                {
                    try
                    {
                        host?.Dispose();
                    }
                    finally
                    {
                        try
                        {
                            if (fixture is not null)
                            {
                                await fixture.DisposeAsync();
                            }
                        }
                        finally
                        {
                            await ModernMcpScratchDirectory.DeleteAsync(scratch);
                        }
                    }
                }
                finally
                {
                    Environment.SetEnvironmentVariable(LaunchGateVariable, previousLaunchGate);
                    Environment.SetEnvironmentVariable(OutputScriptVariable, previousOutputScript);
                }
                throw;
            }
        }

        public Task<CallToolResult> CallAsync(string tool, JsonObject arguments, string id, CancellationToken callerCancellation = default) =>
            CallCoreAsync(call, tool, arguments, id, callerCancellation);

        public Task<CallToolResult> CallLegacyAsync(string tool, JsonObject arguments, string id, CancellationToken callerCancellation = default) =>
            CallCoreAsync(registry.GetType().GetMethod("CallLegacyAsync", BindingFlags.Instance | BindingFlags.NonPublic)!,
                tool, arguments, id, callerCancellation);

        public Task<CallToolResult> CallLegacyWithMetaAsync(string tool, JsonObject arguments, string id, string muxId) =>
            CallCoreAsync(registry.GetType().GetMethod("CallLegacyAsync", BindingFlags.Instance | BindingFlags.NonPublic)!,
                tool, arguments, id, default, muxId);

        private Task<CallToolResult> CallCoreAsync(MethodInfo method, string tool, JsonObject arguments, string id, CancellationToken callerCancellation, string? muxId = null)
        {
            var parameters = new CallToolRequestParams
            {
                Name = tool,
                Arguments = arguments.ToDictionary(static property => property.Key,
                    static property => JsonSerializer.SerializeToElement(property.Value)),
            };
            var raw = JsonSerializer.SerializeToNode(parameters)!.AsObject();
            if (muxId is not null) raw["_meta"] = new JsonObject { ["muxSessionId"] = muxId };
            var request = new JsonRpcRequest
            {
                Id = new RequestId(id),
                Method = "tools/call",
                Params = raw,
            };
            var context = new RequestContext<CallToolRequestParams>(server, request, parameters);
            if (tool is "start_debug" or "attach_debug")
            {
                Fixture.MarkAdapterStartAttempted();
            }
            return ((ValueTask<CallToolResult>)method.Invoke(registry, [context, callerCancellation])!).AsTask();
        }

        public async Task StopAsync(CancellationToken cancellationToken)
        {
            var shutdown = disposer.StopAsync(cancellationToken);
            try
            {
                await shutdown;
            }
            catch (OperationCanceledException) when (shutdown.IsCanceled)
            {
                Fixture.MarkCleanupConfirmed();
                throw;
            }
            Fixture.MarkCleanupConfirmed();
        }

        public async ValueTask DisposeAsync()
        {
            var fixtureCleaned = false;
            Exception? shutdownFailure = null;
            try
            {
                Fixture.ReleaseThreadsResponse();
                Fixture.ReleaseStackTraceResponse();
                var failure = await Record.ExceptionAsync(() => StopAsync(CancellationToken.None));
                if (failure is not null and not OperationCanceledException && !ReferenceEquals(failure, _acceptedShutdownFailure))
                {
                    shutdownFailure = failure;
                }
            }
            finally
            {
                try
                {
                    try
                    {
                        host.Dispose();
                    }
                    finally
                    {
                        await Fixture.DisposeAsync();
                        fixtureCleaned = true;
                    }
                }
                catch (Exception exception)
                {
                    if (shutdownFailure is not null) exception.Data["ShutdownFailure"] = shutdownFailure;
                    throw;
                }
                finally
                {
                    try
                    {
                        if (fixtureCleaned) await ModernMcpScratchDirectory.DeleteAsync(scratchDirectory);
                    }
                    finally
                    {
                        Environment.SetEnvironmentVariable(LaunchGateVariable, previousLaunchGate);
                        Environment.SetEnvironmentVariable(OutputScriptVariable, previousOutputScript);
                    }
                }
            }
            if (shutdownFailure is not null) throw shutdownFailure;
        }
    }
}
