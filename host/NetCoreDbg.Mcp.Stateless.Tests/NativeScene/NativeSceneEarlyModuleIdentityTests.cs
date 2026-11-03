using System.ComponentModel;
using System.Diagnostics;
using System.Globalization;
using System.Runtime.InteropServices;
using System.Runtime.InteropServices.ComTypes;
using System.Security.Cryptography;
using System.Text;
using System.Text.Json.Nodes;
using Microsoft.Win32.SafeHandles;
using ModelContextProtocol.Protocol;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;
using Xunit;
using Xunit.Abstractions;
using Xunit.Sdk;

namespace NetCoreDbg.Mcp.Stateless.Tests.NativeScene;

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeSceneEarlyModuleIdentityTests(ITestOutputHelper output)
{
    private const string Tool = "get_ui_probe_capabilities";
    private const string Protocol = "native-scene-probe/1";
    private static readonly TimeSpan OperationTimeout = TimeSpan.FromSeconds(30);

    [WindowsInitialModuleFact]
    public async Task Capability_LocalProcessBeforeInitialModuleLoad_ReturnsExactIdentityAcrossBothRevisions()
    {
        var scratch = ModernMcpScratchDirectory.Create();
        var releasePath = Path.Combine(scratch, "suspended-process-event.release");
        ModernMcpProcessDriver? driver = null;
        SafeProcessHandle? queryHandle = null;
        string? session = null;
        try
        {
            output.WriteLine("OS={0}; runtime={1}; observer={2}; OS architecture={3}",
                Environment.OSVersion.VersionString, RuntimeInformation.FrameworkDescription,
                RuntimeInformation.ProcessArchitecture, RuntimeInformation.OSArchitecture);
            driver = await ModernMcpProcessDriver.StartAsync(new ModernMcpStartOptions(
                FixtureConfiguration: new FixtureConfiguration(SpawnDescendant: true, LifecycleMode: "all-stop"),
                AdditionalEnvironment: new Dictionary<string, string?>
                {
                    ["CONTROLLED_DAP_SUSPENDED_DESCENDANT_PROCESS_EVENT_RELEASE"] = releasePath,
                }), startupTimeout: OperationTimeout);
            var start = Application(await CallAsync(driver, "start_debug",
                new JsonObject { ["program"] = driver.InertProgramPath }, "early-module-start"));
            Assert.Equal("start_debug_success", Text(start["kind"]));
            session = Text(start["debugSessionId"]);

            using (var deadline = new CancellationTokenSource(OperationTimeout))
            {
                await driver.WaitForFixtureRecordAsync("suspended-descendant-process-event-gated", deadline.Token);
            }
            var processId = await driver.ReadDescendantProcessIdAsync();
            Assert.True(processId > 0, "FIXTURE_SETUP: the controlled adapter did not publish its owned descendant PID.");
            queryHandle = Native.OpenProcess(Native.ProcessQueryLimitedInformation, false, checked((uint)processId));
            RequireNative(!queryHandle.IsInvalid, "OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION)");
            var expected = ObserveIdentity(queryHandle, processId, "before-process-event");
            var expectedExecutable = TestOutputPathResolver.ResolveProcess(
                RepositoryLayout.ControlledAdapterDirectory, "ControlledDapAdapter").Command;
            Assert.True(string.Equals(Path.GetFullPath(expectedExecutable), Path.GetFullPath(expected.ImagePath), StringComparison.OrdinalIgnoreCase),
                $"FIXTURE_SETUP: the query-limited image '{expected.ImagePath}' is not the controlled fixture '{expectedExecutable}'.");
            RequireNative(Native.IsWow64Process2(queryHandle, out var processMachine, out var nativeMachine), "IsWow64Process2");
            Assert.True(processMachine == 0 && nativeMachine == 0x8664,
                $"FIXTURE_LIMITATION: expected a native x64 fixture, not a cross-bitness module lookup; processMachine=0x{processMachine:x4}, nativeMachine=0x{nativeMachine:x4}.");
            ObserveUnavailableMainModule(processId, "before-process-event");
            Assert.Equal(expected, ObserveIdentity(queryHandle, processId, "after-initial-module-observation"));

            // This hash comes from an independently queried image, never from the product or fixture transcript.
            string executableSha256;
            using (var executable = File.OpenRead(expected.ImagePath))
            {
                executableSha256 = Convert.ToHexString(SHA256.HashData(executable)).ToLowerInvariant();
            }
            output.WriteLine("INDEPENDENT_IMAGE_SHA256={0}", executableSha256);
            var expectedProcessIdentity = string.Concat("process_", processId.ToString(CultureInfo.InvariantCulture),
                "_start_", expected.CreationUtcTicks.ToString(CultureInfo.InvariantCulture));

            // Release only DAP publication. The fixture's primary thread remains suspended until stop/teardown.
            await File.WriteAllTextAsync(releasePath, string.Empty);
            using (var deadline = new CancellationTokenSource(OperationTimeout))
            {
                await driver.WaitForFixtureEventAsync("process", deadline.Token);
            }
            // Transcript publication alone cannot prove host consumption. This real DAP response is reader-ordered.
            var barrier = Application(await CallAsync(driver, "get_threads",
                new JsonObject { ["debugSessionId"] = session }, "early-module-reader-barrier"));
            Assert.Equal("threads_success", Text(barrier["kind"]));
            Assert.Equal(expected, ObserveIdentity(queryHandle, processId, "after-reader-barrier"));
            ObserveUnavailableMainModule(processId, "after-reader-barrier");

            string[] schemas = ["native-scene-probe.schema/1", "native-scene-probe.schema/2"];
            var responses = new JsonObject[schemas.Length];
            // Call both revisions exactly once, even when /1 refuses the candidate. No capability retry or republish.
            for (var index = 0; index < schemas.Length; index++)
            {
                responses[index] = ModernMcpProcessDriver.RequireResult(await CallAsync(driver, Tool, new JsonObject
                {
                    ["debugSessionId"] = session,
                    ["protocolVersion"] = Protocol,
                    ["schemaVersion"] = schemas[index],
                }, $"early-module-capability-{index + 1}"));
                output.WriteLine("CAPABILITY_WIRE {0}: {1}", schemas[index], responses[index].ToJsonString());
            }
            Assert.Equal(expected, ObserveIdentity(queryHandle, processId, "after-capability-responses"));
            ObserveUnavailableMainModule(processId, "after-capability-responses");

            for (var index = 0; index < schemas.Length; index++)
            {
                var result = responses[index];
                Assert.Equal("complete", Text(result["resultType"]));
                Assert.False(result["isError"]?.GetValue<bool>() ?? false,
                    $"LIVE_CANDIDATE_CAPABILITY_REFUSED: {schemas[index]} must identify the independently observed live owned process {expectedProcessIdentity} " +
                    $"with SHA256 {executableSha256} despite its unavailable initial MainModule; actual wire={result.ToJsonString()}");
                var capability = Content(result);
                Assert.Equal("ui_probe_capabilities", Text(capability["kind"]));
                Assert.Equal(Protocol, Text(capability["protocolVersion"]));
                Assert.Equal(schemas[index], Text(capability["schemaVersion"]));
                var candidate = Assert.IsType<JsonObject>(capability["candidate"]);
                Assert.Equal(processId, candidate["processId"]!.GetValue<int>());
                Assert.Equal(expectedProcessIdentity, Text(candidate["processIdentity"]));
                Assert.Equal(executableSha256, Text(candidate["executableSha256"]));
            }
        }
        finally
        {
            try
            {
                if (driver is not null && session is not null)
                {
                    var stop = Application(await CallAsync(driver, "stop_debug",
                        new JsonObject { ["debugSessionId"] = session }, "early-module-stop"));
                    Assert.Equal("stop_debug_success", Text(stop["kind"]));
                }
            }
            finally
            {
                try
                {
                    if (driver is not null)
                    {
                        // The SDK/host job path also owns the child if setup, an assertion, or explicit stop fails.
                        await driver.DisposeAsync();
                    }
                }
                finally
                {
                    try
                    {
                        if (queryHandle is { IsInvalid: false })
                        {
                            RequireNative(Native.GetExitCodeProcess(queryHandle, out var exitCode), "GetExitCodeProcess after teardown");
                            output.WriteLine("OWNED_PROCESS_CLEANUP exitCode={0}", exitCode);
                            Assert.NotEqual(Native.StillActive, exitCode);
                        }
                    }
                    finally
                    {
                        queryHandle?.Dispose();
                        await ModernMcpScratchDirectory.DeleteAsync(scratch);
                    }
                }
            }
        }
    }

    private IdentityObservation ObserveIdentity(SafeProcessHandle process, int expectedProcessId, string phase)
    {
        RequireNative(Native.GetExitCodeProcess(process, out var exitCode), "GetExitCodeProcess");
        Assert.Equal(Native.StillActive, exitCode);
        var processId = Native.GetProcessId(process);
        RequireNative(processId != 0, "GetProcessId");
        Assert.Equal(checked((uint)expectedProcessId), processId);
        // These APIs explicitly accept PROCESS_QUERY_LIMITED_INFORMATION; no module enumeration or VM_READ is used.
        // https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-getprocesstimes
        // https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-queryfullprocessimagenamew
        RequireNative(Native.GetProcessTimes(process, out var creation, out _, out _, out _), "GetProcessTimes");
        var fileTime = ((long)(uint)creation.dwHighDateTime << 32) | (uint)creation.dwLowDateTime;
        Assert.True(fileTime > 0, "FIXTURE_SETUP: the owned process creation FILETIME is absent.");
        var creationUtcTicks = DateTime.FromFileTimeUtc(fileTime).Ticks;
        var image = new StringBuilder(32_768);
        var characters = checked((uint)image.Capacity);
        RequireNative(Native.QueryFullProcessImageNameW(process, 0, image, ref characters), "QueryFullProcessImageNameW");
        var imagePath = image.ToString();
        Assert.True(characters > 0 && !string.IsNullOrWhiteSpace(imagePath), "FIXTURE_LIMITATION: the query-limited executable image path is absent.");
        output.WriteLine("INDEPENDENT_IDENTITY phase={0}; PID={1}; creationFILETIME={2}; creationUtcTicks={3}; image={4}; exitCode={5}",
            phase, processId, fileTime, creationUtcTicks, imagePath, exitCode);
        return new IdentityObservation(expectedProcessId, creationUtcTicks, imagePath);
    }

    private void ObserveUnavailableMainModule(int processId, string phase)
    {
        // MainModule documents a null result before the main module has finished loading.
        // EnumProcessModules also documents failure for an uninitialized module list.
        // https://learn.microsoft.com/en-us/dotnet/api/system.diagnostics.process.mainmodule?view=net-8.0
        // https://learn.microsoft.com/en-us/windows/win32/api/psapi/nf-psapi-enumprocessmodules
        using var process = Process.GetProcessById(processId);
        try
        {
            var modulePath = process.MainModule?.FileName;
            output.WriteLine("INITIAL_MODULE phase={0}; PID={1}; MainModule.FileName={2}", phase, processId, modulePath ?? "<null>");
            Assert.True(string.IsNullOrWhiteSpace(modulePath),
                $"FIXTURE_LIMITATION: {phase}: this Windows/runtime exposes MainModule.FileName='{modulePath}' even while the initial thread is held. " +
                "The requested early-module condition was not produced; do not interpret this as product RED.");
        }
        catch (Win32Exception exception) when (exception.NativeErrorCode == 299)
        {
            // Same-bitness was established through IsWow64Process2 before this call. The initial thread never ran;
            // neither WOW64 mismatch nor an exited process can stand in for the early module-list condition.
            output.WriteLine("INITIAL_MODULE phase={0}; PID={1}; Win32Error={2}; message={3}",
                phase, processId, exception.NativeErrorCode, exception.Message);
        }
        catch (Win32Exception exception)
        {
            throw new XunitException($"FIXTURE_LIMITATION: {phase}: MainModule lookup failed for an unaccepted reason, " +
                $"Win32Error={exception.NativeErrorCode}: {exception.Message}. This is not product RED.");
        }
    }

    private static void RequireNative(bool succeeded, string operation)
    {
        if (!succeeded)
        {
            var error = Marshal.GetLastWin32Error();
            throw new XunitException($"FIXTURE_LIMITATION: {operation} failed, Win32Error={error}: {new Win32Exception(error).Message}. " +
                "Independent live process identity was not established; this is not product RED.");
        }
    }

    private static Task<JsonRpcResponse> CallAsync(ModernMcpProcessDriver driver, string tool, JsonObject arguments, string id) =>
        driver.CallToolRawAsync(tool, arguments, ModernMcpProcessDriver.CurrentMeta(), new RequestId(id), timeout: OperationTimeout);

    private static JsonObject Application(JsonRpcResponse response)
    {
        var result = ModernMcpProcessDriver.RequireResult(response);
        Assert.Equal("complete", Text(result["resultType"]));
        Assert.False(result["isError"]?.GetValue<bool>() ?? false, $"FIXTURE_SESSION_SETUP: {result.ToJsonString()}");
        return Content(result);
    }

    private static JsonObject Content(JsonObject result)
    {
        var content = Assert.IsType<JsonObject>(result["structuredContent"]);
        var text = Assert.IsType<JsonObject>(Assert.Single(Assert.IsType<JsonArray>(result["content"])));
        Assert.Equal("text", Text(text["type"]));
        Assert.Equal(content.ToJsonString(), Text(text["text"]));
        return content;
    }

    private static string Text(JsonNode? node) => Assert.IsAssignableFrom<JsonValue>(node).GetValue<string>();

    private readonly record struct IdentityObservation(int ProcessId, long CreationUtcTicks, string ImagePath);

    private static class Native
    {
        internal const uint ProcessQueryLimitedInformation = 0x1000;
        internal const uint StillActive = 259;

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern SafeProcessHandle OpenProcess(uint desiredAccess, [MarshalAs(UnmanagedType.Bool)] bool inheritHandle, uint processId);

        [DllImport("kernel32.dll", SetLastError = true)]
        internal static extern uint GetProcessId(SafeProcessHandle process);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetExitCodeProcess(SafeProcessHandle process, out uint exitCode);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetProcessTimes(SafeProcessHandle process, out FILETIME creationTime, out FILETIME exitTime, out FILETIME kernelTime, out FILETIME userTime);

        [DllImport("kernel32.dll", CharSet = CharSet.Unicode, ExactSpelling = true, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool QueryFullProcessImageNameW(SafeProcessHandle process, uint flags, StringBuilder executableName, ref uint characters);

        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool IsWow64Process2(SafeProcessHandle process, out ushort processMachine, out ushort nativeMachine);
    }
}

public sealed class WindowsInitialModuleFactAttribute : FactAttribute
{
    public WindowsInitialModuleFactAttribute()
    {
        if (!OperatingSystem.IsWindowsVersionAtLeast(10, 0, 16299) ||
            RuntimeInformation.ProcessArchitecture != Architecture.X64 || RuntimeInformation.OSArchitecture != Architecture.X64)
        {
            Skip = "FIXTURE_LIMITATION: the initial-module regression requires native Windows x64, Windows 10 1709+ and IsWow64Process2; other platforms cannot establish this OS condition.";
        }
    }
}
