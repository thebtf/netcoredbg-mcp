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

    public Task InitializeAsync() => InitializeAsync(PythonBaselineServer.StartAsync());

    internal async Task InitializeAsync(Task<PythonBaselineServer> startup) => Server = await startup;

    public async Task DisposeAsync()
    {
        if (Server is not null)
        {
            await Server.DisposeAsync();
        }
    }
}

[CollectionDefinition(Name)]
public sealed class PythonBaselineCollection : ICollectionFixture<PythonBaselineFixture>
{
    public const string Name = "Python baseline";
}
