namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed record DapSessionState(string? Event, string? StopReason, int? ExitCode);
