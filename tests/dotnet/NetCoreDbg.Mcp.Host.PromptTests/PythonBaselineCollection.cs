using Xunit;

namespace NetCoreDbg.Mcp.Host.PromptTests;

/// <summary>
/// Starts one <see cref="PythonBaselineServer"/> child process for the whole parity test
/// collection instead of once per test: the direct Python server takes several seconds to
/// initialize, and every parity test in this collection issues read-only
/// <c>prompts/list</c>/<c>prompts/get</c> requests against it, so sharing one session is
/// both faster and does not affect test isolation.
/// </summary>
public sealed class PythonBaselineFixture : IAsyncLifetime
{
    public PythonBaselineServer Server { get; private set; } = null!;

    public async Task InitializeAsync() => Server = await PythonBaselineServer.StartAsync();

    public async Task DisposeAsync() => await Server.DisposeAsync();
}

[CollectionDefinition(Name)]
public sealed class PythonBaselineCollection : ICollectionFixture<PythonBaselineFixture>
{
    public const string Name = "Python baseline";
}
