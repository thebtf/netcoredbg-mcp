using System.IO;
using System.IO.Pipelines;
using ModelContextProtocol.Client;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host.Tests;

/// <summary>
/// A duplex in-memory byte channel built from two independent
/// <see cref="System.IO.Pipelines.Pipe"/> instances, one per direction, standing in for a
/// pair of real stdio streams. Pairs a real server-side <c>McpServer</c>/<c>IMcpServerBuilder</c>
/// with a real client-side <c>McpClient</c> without spawning a process. This is a transport
/// choice - every session, handler, filter, and forwarding call exercised through it is the
/// genuine SDK/relay code, never a mock of it.
/// </summary>
internal sealed class DuplexChannel
{
    private readonly Pipe _clientToServer = new();
    private readonly Pipe _serverToClient = new();

    /// <summary>What the server reads (the client's outgoing bytes).</summary>
    public Stream ServerInputStream => _clientToServer.Reader.AsStream();

    /// <summary>What the server writes (the client's incoming bytes).</summary>
    public Stream ServerOutputStream => _serverToClient.Writer.AsStream();

    /// <summary>What the client writes (the server's incoming bytes).</summary>
    public Stream ClientWriteStream => _clientToServer.Writer.AsStream();

    /// <summary>What the client reads (the server's outgoing bytes).</summary>
    public Stream ClientReadStream => _serverToClient.Reader.AsStream();

    /// <summary>A ready-made server transport, for directly constructing an <c>McpServer</c>.</summary>
    public StreamServerTransport CreateServerTransport(string? name = null) =>
        new(ServerInputStream, ServerOutputStream, name);

    /// <summary>A ready-made client transport, for <c>McpClient.CreateAsync</c>.</summary>
    public ModelContextProtocol.Protocol.StreamClientTransport CreateClientTransport() =>
        new(ClientWriteStream, ClientReadStream);

    /// <summary>
    /// Simulates the server side (a real process or this in-memory fake) exiting: completes
    /// its outgoing pipe so the client's transport observes end-of-stream, exactly as it
    /// would when a real child process closes its stdout.
    /// </summary>
    public void SimulateServerExit() => _serverToClient.Writer.Complete();
}
