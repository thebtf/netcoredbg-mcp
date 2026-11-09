using System.Text.Json.Nodes;
using ModelContextProtocol.Protocol;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

public class ModernMcpLegacyCleanupAccountingTests
{
    [Fact]
    public void OrderedSameMuxForceCleanupProvesRecovery()
    {
        var (start, stop, cleanup) = History();
        cleanup.Parameters!["_meta"]!["other"] = "different metadata";
        Assert.True(ModernMcpProcessDriver.IsLegacyCleanupRecovery(start, stop, cleanup));
    }

    [Theory]
    [InlineData("wrong-owner")]
    [InlineData("force-false")]
    [InlineData("force-missing")]
    [InlineData("force-string")]
    [InlineData("status")]
    [InlineData("before-stop")]
    [InlineData("overlap-stop")]
    [InlineData("unfinished")]
    [InlineData("rpc-error")]
    [InlineData("tool-error")]
    [InlineData("error")]
    [InlineData("incomplete")]
    [InlineData("remaining")]
    [InlineData("missing-remaining")]
    [InlineData("malformed-remaining")]
    [InlineData("not-unconfirmed")]
    public void InvalidRecoveryDoesNotProveCleanup(string variant)
    {
        var (start, stop, cleanup) = History();
        var arguments = cleanup.Parameters!["arguments"]!.AsObject();
        var result = cleanup.Result!;
        var payload = result["structuredContent"]!.AsObject();
        var data = payload["data"]!.AsObject();
        switch (variant)
        {
            case "wrong-owner": cleanup.Parameters["_meta"]!["muxSessionId"] = "other"; break;
            case "force-false": arguments["force"] = false; break;
            case "force-missing": arguments.Remove("force"); break;
            case "force-string": arguments["force"] = "true"; break;
            case "status": data["action"] = "status"; break;
            case "before-stop": cleanup.StartedOrder = 1; cleanup.CompletedOrder = 2; break;
            case "overlap-stop": cleanup.StartedOrder = 3; break;
            case "unfinished": cleanup.CompletedOrder = 0; break;
            case "rpc-error": cleanup.Result = null; break;
            case "tool-error": result["isError"] = true; break;
            case "error": payload["error"] = "cleanup failed"; break;
            case "incomplete": data["complete"] = false; break;
            case "remaining": data["remaining_owners"] = 1; break;
            case "missing-remaining": data.Remove("remaining_owners"); break;
            case "malformed-remaining": data["remaining_owners"] = "0"; break;
            case "not-unconfirmed": stop.Result!["structuredContent"]!["error"] = "unrelated failure"; break;
        }
        Assert.False(ModernMcpProcessDriver.IsLegacyCleanupRecovery(start, stop, cleanup));
    }

    [Fact]
    public async Task LifecycleRecoveryAcceptsCleanupAfterRestart()
    {
        var (start, stop, cleanup) = History();
        start.Result = StartedResult();
        var restart = new ModernMcpRequestObservation(new RequestId("restart"), "tools/call", new JsonObject
        {
            ["name"] = "restart_debug",
            ["arguments"] = new JsonObject(),
            ["_meta"] = new JsonObject { ["muxSessionId"] = "owner" },
        }) { StartedOrder = 3, CompletedOrder = 4, Result = StartedResult() };
        stop.StartedOrder = 5;
        stop.CompletedOrder = 6;
        cleanup.StartedOrder = 7;
        cleanup.CompletedOrder = 8;

        Assert.True(await ReconstructCleanupAsync(start, restart, stop, cleanup));
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public async Task LifecycleRecoveryMatchesMuxRatherThanUnrelatedMetadata(bool foreignMux)
    {
        var (start, stop, cleanup) = History();
        start.Result = StartedResult();
        start.Parameters!["_meta"]!["extra"] = "start";
        stop.Parameters!["_meta"]!["extra"] = "stop";
        if (foreignMux) stop.Parameters["_meta"]!["muxSessionId"] = "other";

        Assert.Equal(!foreignMux, await ReconstructCleanupAsync(start, stop, cleanup));
    }

    [Fact]
    public async Task LifecycleRecoveryRejectsForeignCleanupMux()
    {
        var (start, stop, cleanup) = History();
        start.Result = StartedResult();
        cleanup.Parameters!["_meta"]!["muxSessionId"] = "other";
        Assert.False(await ReconstructCleanupAsync(start, stop, cleanup));
    }

    private static JsonObject StartedResult() => new()
    {
        ["resultType"] = "complete",
        ["structuredContent"] = new JsonObject { ["data"] = new JsonObject { ["success"] = true } },
    };

    private static Task<bool> ReconstructCleanupAsync(params ModernMcpRequestObservation[] history)
    {
        var driver = (ModernMcpProcessDriver)System.Runtime.CompilerServices.RuntimeHelpers.GetUninitializedObject(typeof(ModernMcpProcessDriver));
        const System.Reflection.BindingFlags flags = System.Reflection.BindingFlags.Instance | System.Reflection.BindingFlags.NonPublic;
        typeof(ModernMcpProcessDriver).GetField("_clientClosed", flags)!.SetValue(driver, true);
        var method = typeof(ModernMcpProcessDriver).GetMethod("StopKnownLegacySessionsAsync", flags)!;
        return Assert.IsType<Task<bool>>(method.Invoke(driver, [history]));
    }

    private static (ModernMcpRequestObservation Start, ModernMcpRequestObservation Stop, ModernMcpRequestObservation Cleanup) History()
    {
        static ModernMcpRequestObservation Request(string name, long started, long completed, JsonObject? arguments = null) =>
            new(new RequestId(name), "tools/call", new JsonObject
            {
                ["name"] = name,
                ["arguments"] = arguments ?? new JsonObject(),
                ["_meta"] = new JsonObject { ["muxSessionId"] = "owner" },
            }) { StartedOrder = started, CompletedOrder = completed };
        var start = Request("start_debug", 1, 2);
        var stop = Request("stop_debug", 3, 4);
        stop.Result = new JsonObject
        {
            ["resultType"] = "complete",
            ["structuredContent"] = new JsonObject { ["error"] = "Debug session cleanup is unconfirmed." },
        };
        var cleanup = Request("cleanup_processes", 5, 6, new JsonObject { ["force"] = true });
        cleanup.Result = new JsonObject
        {
            ["resultType"] = "complete",
            ["structuredContent"] = new JsonObject
            {
                ["data"] = new JsonObject { ["action"] = "cleanup", ["complete"] = true, ["remaining_owners"] = 0 },
            },
        };
        return (start, stop, cleanup);
    }
}
