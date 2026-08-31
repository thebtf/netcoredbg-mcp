using System.Diagnostics;
using System.Text;
using System.Text.Json;

namespace OwnerScopeAdapter;

internal static class Program
{
    private static readonly string? RootMarker = Environment.GetEnvironmentVariable("OWNER_SCOPE_ROOT_MARKER");
    private static readonly string? ChildMarker = Environment.GetEnvironmentVariable("OWNER_SCOPE_CHILD_MARKER");

    private static async Task<int> Main(string[] args)
    {
        if (args.Contains("--owner-scope-child", StringComparer.Ordinal))
        {
            await WriteMarkerAsync(ChildMarker, new { pid = Environment.ProcessId });
            await Task.Delay(Timeout.InfiniteTimeSpan);
            return 0;
        }

        var executable = Environment.ProcessPath
            ?? throw new InvalidOperationException("Fixture process path is unavailable.");
        using var child = Process.Start(new ProcessStartInfo(executable, "--owner-scope-child")
        {
            UseShellExecute = false,
        }) ?? throw new InvalidOperationException("Fixture child could not be started.");

        await WriteMarkerAsync(
            RootMarker,
            new { pid = Environment.ProcessId, child_pid = child.Id }
        );

        var body = JsonSerializer.Serialize(new
        {
            seq = 1,
            type = "event",
            @event = "output",
            body = new { category = "console", output = "owner-scope-ready" },
        });
        var payload = Encoding.UTF8.GetBytes(body);
        await Console.OpenStandardOutput().WriteAsync(
            Encoding.ASCII.GetBytes($"Content-Length: {payload.Length}\r\n\r\n")
        );
        await Console.OpenStandardOutput().WriteAsync(payload);
        await Console.OpenStandardOutput().FlushAsync();
        await Console.Error.WriteLineAsync("owner-scope-stderr-ready");

        if (int.TryParse(Environment.GetEnvironmentVariable("OWNER_SCOPE_ROOT_EXIT_CODE"), out var exitCode))
        {
            return exitCode;
        }

        await Task.Delay(Timeout.InfiniteTimeSpan);
        return 0;
    }

    private static async Task WriteMarkerAsync(string? path, object value)
    {
        if (string.IsNullOrWhiteSpace(path))
        {
            throw new InvalidOperationException("Fixture marker path is unavailable.");
        }
        await File.WriteAllTextAsync(path, JsonSerializer.Serialize(value));
    }
}
