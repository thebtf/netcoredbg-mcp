using System.Globalization;
using System.Security.Cryptography;
using System.Text.Json;
using NetCoreDbg.Mcp.Stateless.DebugAdapter;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

internal sealed record NativeSceneTargetIdentity(
    int ProcessId,
    string ProcessIdentity,
    string ExecutablePath,
    string ExecutableSha256,
    string? AssemblyVersion,
    string? ProbeVersion);

internal sealed class NativeSceneSessionBinding
{
    private const string ProbeSchemaArtifactName = "native-scene-probe.schema.json";

    private readonly NetCoreDbgSession _session;

    internal NativeSceneSessionBinding(NetCoreDbgSession session)
    {
        _session = session ?? throw new ArgumentNullException(nameof(session));
        AuthorizationNonce = CreateAuthorizationNonce();
    }

    internal string AuthorizationNonce { get; }

    internal bool TryGetCandidate(out JsonElement candidate)
    {
        if (!_session.TryGetNativeSceneTargetIdentity(out var targetIdentity))
        {
            candidate = default;
            return false;
        }

        candidate = JsonSerializer.SerializeToElement(new
        {
            processId = targetIdentity.ProcessId,
            processIdentity = targetIdentity.ProcessIdentity,
            hwnd = (string?)null,
            executableSha256 = targetIdentity.ExecutableSha256,
            assemblyVersion = targetIdentity.AssemblyVersion,
            probeVersion = targetIdentity.ProbeVersion,
            observerVersions = Array.Empty<object>(),
            contractSetHash = NativeSceneContractCatalog.GetArtifactSha256(ProbeSchemaArtifactName),
            storyHash = (string?)null,
            capturedAt = DateTimeOffset.UtcNow.ToString("O", CultureInfo.InvariantCulture),
            source = new
            {
                kind = "launch_manifest",
                verification = "verified",
            },
        });
        return true;
    }

    internal bool MatchesExpectedCandidateIdentity(JsonElement expectedCandidateIdentity)
    {
        return _session.TryGetNativeSceneTargetIdentity(out var targetIdentity)
            && (expectedCandidateIdentity.ValueKind == JsonValueKind.Null ||
                (expectedCandidateIdentity.ValueKind == JsonValueKind.Object &&
                 MatchesExpectedValue(expectedCandidateIdentity, "executableSha256", targetIdentity.ExecutableSha256) &&
                 MatchesExpectedValue(expectedCandidateIdentity, "assemblyVersion", targetIdentity.AssemblyVersion) &&
                 MatchesExpectedValue(expectedCandidateIdentity, "probeVersion", targetIdentity.ProbeVersion)));
    }

    private static bool MatchesExpectedValue(JsonElement expectedCandidateIdentity, string propertyName, string? actual)
    {
        if (!expectedCandidateIdentity.TryGetProperty(propertyName, out var expected) || expected.ValueKind == JsonValueKind.Null)
        {
            return true;
        }

        return expected.ValueKind == JsonValueKind.String &&
               actual is not null &&
               StringComparer.Ordinal.Equals(expected.GetString(), actual);
    }

    private static string CreateAuthorizationNonce()
    {
        Span<byte> bytes = stackalloc byte[32];
        RandomNumberGenerator.Fill(bytes);
        var base64 = Convert.ToBase64String(bytes);
        CryptographicOperations.ZeroMemory(bytes);
        return base64.TrimEnd('=').Replace('+', '-').Replace('/', '_');
    }
}
