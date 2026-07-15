using System.Diagnostics;
using ModelContextProtocol.Protocol;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// Owns the Python backend child process: shell-free launch via
/// <see cref="ProcessStartInfo.ArgumentList"/>, the upstream MCP transport built directly
/// from its redirected stdio streams, the byte-preserved stderr pump, and deterministic
/// shutdown (stdin close, bounded wait, process-tree kill). Extracted unchanged from the
/// former monolithic <c>Program.cs</c>; <see cref="RelaySession"/> decides *when* to build
/// the upstream MCP client from <see cref="CreateUpstreamTransport"/>, but this class owns
/// every OS-process-level detail.
/// </summary>
internal sealed class PythonBackendProcess : IDisposable
{
    /// <summary>
    /// Overrides only the Python executable used to launch the backend. Never forwarded
    /// to the child as a CLI argument.
    /// </summary>
    private const string PythonExecutableEnvironmentVariable = "NETCOREDBG_MCP_PYTHON_EXECUTABLE";

    private const string DefaultPythonExecutable = "python";

    private readonly Process _process;
    private readonly Task _stderrPump;

    private PythonBackendProcess(Process process, Task stderrPump)
    {
        _process = process;
        _stderrPump = stderrPump;
    }

    /// <summary>
    /// Starts the Python backend directly and begins forwarding its stderr. Throws if the
    /// process cannot be started; the caller reports this as a startup failure.
    /// </summary>
    public static PythonBackendProcess Start(IReadOnlyList<string> hostArguments)
    {
        var process = StartPythonProcess(hostArguments);
        // Preserve the child diagnostics byte-for-byte while keeping stdout exclusively
        // available for the downstream MCP JSON-RPC transport.
        var stderrPump = process.StandardError.BaseStream.CopyToAsync(Console.OpenStandardError());
        return new PythonBackendProcess(process, stderrPump);
    }

    /// <summary>
    /// Builds the shell-free upstream transport from the child's already-redirected stdio
    /// streams. Never starts a new process; callable exactly once per bootstrap per the
    /// paired-session contract, but harmless to call more than once since it only wraps the
    /// existing streams.
    /// </summary>
    public StreamClientTransport CreateUpstreamTransport() =>
        new(_process.StandardInput.BaseStream, _process.StandardOutput.BaseStream);

    /// <summary>Closes the child's stdin, waits up to five seconds, then kills the process tree.</summary>
    public async Task StopAsync()
    {
        try
        {
            _process.StandardInput.Close();
        }
        catch (IOException)
        {
            // The child may have already closed its input while terminating.
        }
        catch (InvalidOperationException)
        {
            // The child already exited before its redirected input was observed.
        }

        if (_process.HasExited)
        {
            return;
        }

        try
        {
            await _process.WaitForExitAsync().WaitAsync(TimeSpan.FromSeconds(5));
        }
        catch (TimeoutException)
        {
            _process.Kill(entireProcessTree: true);
            await _process.WaitForExitAsync();
        }
    }

    /// <summary>
    /// Awaits the stderr pump. The caller only awaits this after <see cref="StopAsync"/>
    /// has completed, so a byte-preserved forward races the child's own shutdown rather
    /// than an unbounded read against a still-running process.
    /// </summary>
    public Task WaitForStderrForwardedAsync() => _stderrPump;

    public void Dispose() => _process.Dispose();

    /// <summary>Resolves the executable used to launch the Python backend.</summary>
    private static string ResolvePythonExecutable()
    {
        var overridden = Environment.GetEnvironmentVariable(PythonExecutableEnvironmentVariable);
        return string.IsNullOrEmpty(overridden) ? DefaultPythonExecutable : overridden;
    }

    /// <summary>
    /// Builds the full child process argument list: the fixed <c>-m netcoredbg_mcp</c> module
    /// invocation followed by every ordinary host CLI argument. Values remain discrete process
    /// arguments and must come only from trusted launch-time configuration.
    /// </summary>
    private static List<string> BuildPythonArguments(IReadOnlyList<string> hostArguments)
    {
        var arguments = new List<string>(hostArguments.Count + 2) { "-m", "netcoredbg_mcp" };
        arguments.AddRange(hostArguments);
        return arguments;
    }

    /// <summary>
    /// Starts the Python backend directly. <see cref="ProcessStartInfo.ArgumentList"/> keeps
    /// every value as a process argument on every supported platform; no command shell parses,
    /// expands, or rewrites the executable or arguments.
    /// </summary>
    private static Process StartPythonProcess(IReadOnlyList<string> hostArguments)
    {
        var startInfo = new ProcessStartInfo
        {
            FileName = ResolvePythonExecutable(),
            WorkingDirectory = Environment.CurrentDirectory,
            RedirectStandardInput = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            UseShellExecute = false,
            CreateNoWindow = true,
        };

        foreach (var argument in BuildPythonArguments(hostArguments))
        {
            startInfo.ArgumentList.Add(argument);
        }

        var process = new Process { StartInfo = startInfo };
        try
        {
            if (!process.Start())
            {
                throw new InvalidOperationException("The Python backend process did not start.");
            }

            return process;
        }
        catch
        {
            process.Dispose();
            throw;
        }
    }
}
