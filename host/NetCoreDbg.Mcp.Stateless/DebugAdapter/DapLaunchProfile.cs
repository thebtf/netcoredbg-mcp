using System.Text.Json;
using NetCoreDbg.Mcp.Stateless.NativeScene;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed record DapLaunchProfileResult(
    IReadOnlyDictionary<string, string?>? Environment,
    DapLaunchProfileMetadata? Metadata);

internal sealed record DapLaunchProfileMetadata(
    string Source,
    string? Profile,
    string? Path,
    IReadOnlyList<string> VariableNames,
    int AppliedCount);

internal sealed class DapLaunchProfileException : Exception
{
    internal DapLaunchProfileException() : base("Launch profile could not be resolved safely.") { }
}

internal static class DapLaunchProfile
{
    private const string FileName = ".netcoredbg-mcp.launch.json";
    private const int MaximumProfileBytes = 16 * 1024 * 1024;

    internal static async Task<DapLaunchProfileResult> ResolveAsync(
        string? projectRoot,
        string? profileName,
        IReadOnlyDictionary<string, string?>? explicitEnvironment,
        CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (explicitEnvironment is not null)
        {
            foreach (var (name, value) in explicitEnvironment)
            {
                cancellationToken.ThrowIfCancellationRequested();
                Validate(name, value);
            }
        }

        var path = projectRoot is null ? null : Path.Combine(projectRoot, FileName);
        JsonDocument? document = null;
        if (path is not null)
        {
            try
            {
                await using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 4096, FileOptions.Asynchronous);
                var length = stream.Length;
                if (length > MaximumProfileBytes) throw new DapLaunchProfileException();
                var bytes = new byte[checked((int)length)];
                await stream.ReadExactlyAsync(bytes, cancellationToken).ConfigureAwait(false);
                if (await stream.ReadAsync(new byte[1], cancellationToken).ConfigureAwait(false) != 0)
                    throw new DapLaunchProfileException();
                document = JsonDocument.Parse(bytes);
            }
            catch (FileNotFoundException) { }
            catch (DirectoryNotFoundException) { }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or JsonException or ArgumentException or NotSupportedException)
            {
                throw new DapLaunchProfileException();
            }
        }

        if (document is null)
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (!string.IsNullOrEmpty(profileName)) throw new DapLaunchProfileException();
            return new DapLaunchProfileResult(
                explicitEnvironment,
                explicitEnvironment is null ? null : Metadata("tool-argument", null, null, explicitEnvironment));
        }

        using (document)
        {
            var config = document.RootElement;
            if (config.ValueKind != JsonValueKind.Object ||
                !config.TryGetProperty("profiles", out var profiles) || profiles.ValueKind != JsonValueKind.Object)
                throw new DapLaunchProfileException();

            var selected = profileName;
            if (string.IsNullOrEmpty(selected))
            {
                selected = "default";
                if (config.TryGetProperty("defaultProfile", out var defaultProfile))
                {
                    if (defaultProfile.ValueKind != JsonValueKind.String) throw new DapLaunchProfileException();
                    selected = defaultProfile.GetString()!;
                }
            }

            if (!profiles.TryGetProperty(selected, out var profile) || profile.ValueKind != JsonValueKind.Object)
                throw new DapLaunchProfileException();

            var resolved = new Dictionary<string, string?>(DapLaunchEnvironment.NameComparer);
            if (profile.TryGetProperty("inherit", out var inherit))
            {
                if (inherit.ValueKind != JsonValueKind.Array) throw new DapLaunchProfileException();
                foreach (var item in inherit.EnumerateArray())
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (item.ValueKind != JsonValueKind.String) throw new DapLaunchProfileException();
                    var name = item.GetString()!;
                    Validate(name, null);
                    var value = System.Environment.GetEnvironmentVariable(name);
                    if (value is null) throw new DapLaunchProfileException();
                    Validate(name, value);
                    resolved[name] = value;
                }
            }

            if (profile.TryGetProperty("env", out var environment))
            {
                if (environment.ValueKind != JsonValueKind.Object) throw new DapLaunchProfileException();
                foreach (var property in environment.EnumerateObject())
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    if (property.Value.ValueKind is not (JsonValueKind.String or JsonValueKind.Null))
                        throw new DapLaunchProfileException();
                    var value = property.Value.GetString();
                    Validate(property.Name, value);
                    resolved[property.Name] = value;
                }
            }

            if (explicitEnvironment is not null)
            {
                foreach (var (name, value) in explicitEnvironment)
                {
                    cancellationToken.ThrowIfCancellationRequested();
                    resolved[name] = value;
                }
            }

            cancellationToken.ThrowIfCancellationRequested();
            return new DapLaunchProfileResult(resolved, Metadata("project-launch-profile", selected, path, resolved));
        }
    }

    private static void Validate(string name, string? value)
    {
        if (string.IsNullOrEmpty(name) || name.IndexOfAny(['=', '\0']) >= 0 ||
            value?.IndexOf('\0') >= 0 ||
            DapLaunchEnvironment.NameComparer.Equals(name, NativeSceneProbeChannel.PipeEnvironmentName) ||
            DapLaunchEnvironment.NameComparer.Equals(name, NativeSceneProbeChannel.NonceEnvironmentName))
            throw new DapLaunchProfileException();
    }

    private static DapLaunchProfileMetadata Metadata(
        string source, string? profile, string? path, IReadOnlyDictionary<string, string?> environment)
    {
        var names = environment.Keys.ToArray();
        return new DapLaunchProfileMetadata(source, profile, path, names, names.Length);
    }
}
