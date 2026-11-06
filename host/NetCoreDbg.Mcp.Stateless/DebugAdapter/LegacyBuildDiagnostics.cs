using System.Diagnostics;
using System.Globalization;
using System.Text.RegularExpressions;
using System.Text.Json;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed class LegacyBuildDiagnosticsException(string message) : Exception(message) { }

internal sealed record LegacyBuildSnapshot(DapBuildPlan Plan, DapBuildResult? Result, string? Stage, string? Interruption, double DurationMs)
{
    private static readonly Regex Detailed = new(
        @"^(?<file>[^\(]+)\((?<line>\d+),(?<column>\d+)\):\s*(?<severity>error|warning|info)\s+(?<code>\w+):\s*(?<message>.+?)(?:\s+\[(?<project>[^\]]+)\])?$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(100));
    private static readonly Regex Simple = new(
        @"^(?<severity>error|warning|info)\s+(?<code>\w+):\s*(?<message>.+)$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(100));

    internal Dictionary<string, object> ToData(bool includeWarnings, int maximumBytes, CancellationToken cancellationToken, out string message)
    {
        var command = Result?.Command;
        var started = Stopwatch.GetTimestamp();
        void CheckBudget()
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (Stopwatch.GetElapsedTime(started) > TimeSpan.FromSeconds(2))
                throw new LegacyBuildDiagnosticsException("Build diagnostics exceeded the native processing time limit.");
        }
        CheckBudget();
        var success = command?.ExitCode == 0;
        var data = new Dictionary<string, object>
        {
            ["success"] = success,
            ["state"] = success ? "ready" : Interruption == "cancelled" ? "cancelled" : "failed",
            ["command"] = Result?.Stage ?? Stage ?? "build",
            ["projectPath"] = Plan.ProjectPath,
            ["configuration"] = Plan.Configuration,
            ["durationMs"] = Math.Round(Result?.DurationMs ?? DurationMs, 2),
        };
        if (command is not null)
        {
            data["exitCode"] = command.ExitCode;
            if (!success)
            {
                if ((long)command.Stdout.Length + command.Stderr.Length > maximumBytes)
                    throw new LegacyBuildDiagnosticsException("Build diagnostics exceed the native 256 KiB response limit.");
                data["stdout"] = command.Stdout;
                data["stderr"] = command.Stderr;
                data["commandLine"] = Result!.CommandLine;
                data["workingDirectory"] = Result.WorkingDirectory;
            }
        }
        else
        {
            data["stdout"] = "";
            data["stderr"] = "";
            if (Interruption == "cancelled") data["cancelled"] = true;
        }

        if (command is null || command.Stdout.Length >= DapBuildOutput.MaximumRetainedCharacters || command.Stderr.Length >= DapBuildOutput.MaximumRetainedCharacters)
        {
            message = command is null
                ? "Build output and diagnostic counts are unavailable for the interrupted attempt."
                : "Build output may be truncated; diagnostic counts are unavailable.";
            data["diagnosticsIncomplete"] = true;
            data["diagnosticsLimitation"] = message;
            return data;
        }

        var diagnostics = new List<Dictionary<string, object>>();
        var errors = 0;
        var warnings = 0;
        var retainedBytes = 0;
        try
        {
            foreach (var output in new[] { command.Stdout, command.Stderr })
            {
                var offset = 0;
                while (offset < output.Length)
                {
                    CheckBudget();
                    var remaining = output.AsSpan(offset);
                    var end = remaining.IndexOfAny('\r', '\n');
                    var length = end < 0 ? remaining.Length : end;
                    if (length > maximumBytes)
                        throw new LegacyBuildDiagnosticsException("Build diagnostics exceed the native processing line limit.");
                    var line = remaining[..length].Trim().ToString();
                    offset += length;
                    if (offset < output.Length && output[offset++] == '\r' && offset < output.Length && output[offset] == '\n') offset++;
                    var match = Detailed.Match(line);
                    if (!match.Success) match = Simple.Match(line);
                    if (!match.Success) continue;
                    var severity = match.Groups["severity"].Value.ToLowerInvariant();
                    if (severity == "error") errors++;
                    if (severity == "warning") warnings++;
                    if (!includeWarnings && severity != "error") continue;
                    var diagnostic = new Dictionary<string, object>
                    {
                        ["severity"] = severity,
                        ["code"] = match.Groups["code"].Value,
                        ["message"] = match.Groups["message"].Value,
                    };
                    var file = match.Groups["file"];
                    if (file.Success)
                    {
                        diagnostic["file"] = file.Value;
                        if (long.TryParse(match.Groups["line"].Value, NumberStyles.None, CultureInfo.InvariantCulture, out var number)) diagnostic["line"] = number;
                        if (long.TryParse(match.Groups["column"].Value, NumberStyles.None, CultureInfo.InvariantCulture, out number)) diagnostic["column"] = number;
                        if (match.Groups["project"].Success) diagnostic["project"] = match.Groups["project"].Value;
                    }
                    retainedBytes += JsonSerializer.SerializeToUtf8Bytes(diagnostic).Length + 1;
                    if (retainedBytes > maximumBytes)
                        throw new LegacyBuildDiagnosticsException("Build diagnostics exceed the native 256 KiB response limit.");
                    diagnostics.Add(diagnostic);
                }
            }
            CheckBudget();
        }
        catch (RegexMatchTimeoutException)
        {
            throw new LegacyBuildDiagnosticsException("Build diagnostic parsing exceeded the native processing time limit.");
        }
        data["errorCount"] = errors;
        data["warningCount"] = warnings;
        if (diagnostics.Count != 0) data["diagnostics"] = diagnostics;
        if (!includeWarnings && warnings > 0) data["warningHint"] = $"{warnings} warnings hidden. Call get_build_diagnostics() to see them.";
        message = $"Build: {errors} errors, {warnings} warnings";
        return data;
    }
}
