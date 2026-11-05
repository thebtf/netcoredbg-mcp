using System.Text.Json;
using System.Text.Json.Serialization;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal enum DapLoadedSourcesOutcome { Success, Unsupported, Refused }

internal sealed record DapLoadedSourcesResult(DapLoadedSourcesOutcome Outcome, IReadOnlyList<DapLoadedSource>? Sources = null)
{
    internal static DapLoadedSourcesResult Unsupported { get; } = new(DapLoadedSourcesOutcome.Unsupported);
    internal static DapLoadedSourcesResult Refused { get; } = new(DapLoadedSourcesOutcome.Refused);
}

internal sealed record DapLoadedSource(
    [property: JsonPropertyName("name")] string? Name,
    [property: JsonPropertyName("path")] string? Path,
    [property: JsonPropertyName("sourceReference")] int? SourceReference,
    [property: JsonPropertyName("origin")] string? Origin,
    [property: JsonPropertyName("presentationHint")] string? PresentationHint,
    [property: JsonPropertyName("adapterData")] JsonElement? AdapterData);

internal static class DapLoadedSources
{
    internal static IReadOnlyList<DapLoadedSource> Parse(JsonElement body)
    {
        if (body.ValueKind != JsonValueKind.Object
            || !body.TryGetProperty("sources", out var sources)
            || sources.ValueKind != JsonValueKind.Array)
        {
            throw new InvalidDataException("DAP loadedSources response body is invalid.");
        }

        var result = new List<DapLoadedSource>();
        var positions = new Dictionary<string, int>(StringComparer.Ordinal);
        foreach (var source in sources.EnumerateArray())
        {
            var entry = ParseSource(source);
            var key = Key(entry);
            if (positions.TryGetValue(key, out var index))
            {
                result[index] = entry;
            }
            else
            {
                positions.Add(key, result.Count);
                result.Add(entry);
            }
        }
        return result;
    }

    internal static DapLoadedSource ParseSource(JsonElement source)
    {
        if (source.ValueKind != JsonValueKind.Object)
            throw new InvalidDataException("DAP loadedSources source must be an object.");
        var name = OptionalString(source, "name");
        var path = OptionalString(source, "path");
        int? reference = null;
        if (source.TryGetProperty("sourceReference", out var value) && value.ValueKind != JsonValueKind.Null)
        {
            if (value.ValueKind != JsonValueKind.Number || !value.TryGetInt32(out var number) || number < 0)
                throw new InvalidDataException("DAP sourceReference must be a nonnegative Int32.");
            reference = number;
        }
        return new DapLoadedSource(name, path, reference, OptionalString(source, "origin"),
            OptionalString(source, "presentationHint"),
            source.TryGetProperty("adapterData", out var data) && data.ValueKind != JsonValueKind.Null ? data.Clone() : null);
    }

    internal static string Key(DapLoadedSource source) =>
        !string.IsNullOrEmpty(source.Path) ? "path:" + NormalizePath(source.Path)
        : source.SourceReference is not null ? "sourceReference:" + source.SourceReference.Value.ToString(System.Globalization.CultureInfo.InvariantCulture)
        : !string.IsNullOrEmpty(source.Name) ? "name:" + NormalizePath(source.Name) : "<unknown-source>";

    private static string? OptionalString(JsonElement source, string name)
    {
        if (!source.TryGetProperty(name, out var value) || value.ValueKind == JsonValueKind.Null) return null;
        if (value.ValueKind != JsonValueKind.String) throw new InvalidDataException($"DAP source {name} must be a string.");
        return value.GetString();
    }

    private static string NormalizePath(string path)
    {
        var windows = OperatingSystem.IsWindows();
        var separator = windows ? '\\' : '/';
        if (windows) path = path.Replace('/', separator);
        var offset = 0;
        var rooted = false;
        if (windows && path.StartsWith("\\\\", StringComparison.Ordinal))
        {
            var start = path.StartsWith("\\\\?\\UNC\\", StringComparison.OrdinalIgnoreCase) ? 8 : 2;
            var serverEnd = path.IndexOf(separator, start);
            var shareEnd = serverEnd < 0 ? -1 : path.IndexOf(separator, serverEnd + 1);
            offset = shareEnd < 0 ? path.Length : shareEnd + 1;
            rooted = shareEnd >= 0;
        }
        else if (windows && path.Length >= 2 && path[1] == ':')
        {
            offset = 2;
            if (path.Length > offset && path[offset] == separator) { offset++; rooted = true; }
        }
        else if (path.Length > 0 && path[0] == separator)
        {
            rooted = true;
            offset = !windows && path.Length > 1 && path[1] == separator && (path.Length == 2 || path[2] != separator) ? 2 : 1;
        }

        var prefix = path[..offset];
        var parts = new List<string>();
        foreach (var part in path[offset..].Split(separator))
        {
            if (part.Length == 0 || part == ".") continue;
            if (part == ".." && parts.Count > 0 && parts[^1] != "..") parts.RemoveAt(parts.Count - 1);
            else if (part != ".." || !rooted) parts.Add(part);
        }
        var normalized = prefix + string.Join(separator, parts);
        if (normalized.Length == 0) normalized = ".";
        return windows ? Unicode15Lower.Lower(normalized) : normalized;
    }
}
