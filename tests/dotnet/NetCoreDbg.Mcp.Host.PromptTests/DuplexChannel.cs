using System.IO.Pipelines;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// A duplex in-memory byte channel built from two independent <see cref="Pipe"/>
/// instances, one per direction, standing in for a pair of real stdio streams. Pairs a
/// real server-side <c>McpServer</c>/<c>IMcpServerBuilder</c> with a real client-side
/// <c>McpClient</c> without spawning a process, so every session/handler/serialization
/// step exercised through it is genuine SDK code, not a mock of it.
/// </summary>
internal sealed class DuplexChannel
{
    private readonly Pipe _clientToServer = new();
    private readonly Pipe _serverToClient = new();

    public Stream ServerInputStream => _clientToServer.Reader.AsStream();

    public Stream ServerOutputStream => _serverToClient.Writer.AsStream();

    public Stream ClientWriteStream => _clientToServer.Writer.AsStream();

    public Stream ClientReadStream => _serverToClient.Reader.AsStream();

    public StreamServerTransport CreateServerTransport(string? name = null) =>
        new(ServerInputStream, ServerOutputStream, name);

    public StreamClientTransport CreateClientTransport() =>
        new(ClientWriteStream, ClientReadStream);
}
