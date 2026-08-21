namespace NetCoreDbg.Mcp.CodeSearch.Core;

/// <summary>Controls traversal, matching, and failure semantics without selecting a project root.</summary>
public abstract class SearchPolicy
{
    internal abstract SearchPolicySettings Settings { get; }
}

/// <summary>Compatibility policy for the existing native host search tools.</summary>
public sealed class LegacySearchPolicy : SearchPolicy
{
    private static readonly SearchPolicySettings SettingsValue = new(
        SourceExtensions: new HashSet<string>(StringComparer.OrdinalIgnoreCase)
        {
            ".cs", ".xaml", ".axaml", ".csproj", ".json", ".config",
        },
        MaximumSymbolResults: null,
        MaximumReferenceResults: 1000,
        Strict: false,
        MaximumNameUtf16CodeUnits: null,
        MaximumContextScalars: null,
        MaximumDirectories: null,
        MaximumNonDirectoryEntries: null,
        MaximumOpenedFileBytes: null,
        MaximumAggregateOpenedBytes: null,
        Deadline: null);

    private LegacySearchPolicy()
    {
    }

    public static LegacySearchPolicy Instance { get; } = new();

    internal override SearchPolicySettings Settings => SettingsValue;
}

/// <summary>Fail-closed policy for the opt-in local preview route.</summary>
public sealed class PreviewSearchPolicy : SearchPolicy
{
    private static readonly SearchPolicySettings SettingsValue = new(
        SourceExtensions: new HashSet<string>(StringComparer.OrdinalIgnoreCase) { ".cs" },
        MaximumSymbolResults: 128,
        MaximumReferenceResults: 128,
        Strict: true,
        MaximumNameUtf16CodeUnits: 256,
        MaximumContextScalars: 512,
        MaximumDirectories: 2048,
        MaximumNonDirectoryEntries: 20000,
        MaximumOpenedFileBytes: 1024 * 1024,
        MaximumAggregateOpenedBytes: 16 * 1024 * 1024,
        Deadline: TimeSpan.FromSeconds(5));

    private PreviewSearchPolicy()
    {
    }

    public static PreviewSearchPolicy Instance { get; } = new();

    internal override SearchPolicySettings Settings => SettingsValue;
}

internal sealed record SearchPolicySettings(
    HashSet<string> SourceExtensions,
    int? MaximumSymbolResults,
    int MaximumReferenceResults,
    bool Strict,
    int? MaximumNameUtf16CodeUnits,
    int? MaximumContextScalars,
    int? MaximumDirectories,
    int? MaximumNonDirectoryEntries,
    long? MaximumOpenedFileBytes,
    long? MaximumAggregateOpenedBytes,
    TimeSpan? Deadline);
