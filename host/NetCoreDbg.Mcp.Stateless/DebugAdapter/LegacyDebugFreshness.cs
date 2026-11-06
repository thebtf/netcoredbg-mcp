using System.Text;
using System.Text.Json;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class LegacyDebugFreshness
{
    private const int MaximumInputBytes = 64 * 1024;
    private const int MaximumPaths = 256;
    private static readonly StringComparer PathComparer = OperatingSystem.IsWindows() ? StringComparer.OrdinalIgnoreCase : StringComparer.Ordinal;

    internal sealed record Arguments(int? ProcessId, string? ProcessName, string? Workspace,
        string[] Sources, string[] Modules, string[] Artifacts, bool RequireActive);

    internal static bool TryParse(Dictionary<string, JsonElement> values, out Arguments arguments)
    {
        arguments = new(null, null, null, [], [], [], false);
        if (values.Sum(static pair => (long)Encoding.UTF8.GetByteCount(pair.Key) + Encoding.UTF8.GetByteCount(pair.Value.GetRawText())) > MaximumInputBytes)
            return false;
        int? processId = null;
        if (values.TryGetValue("expected_process_id", out var id) && id.ValueKind != JsonValueKind.Null)
        {
            if (id.ValueKind != JsonValueKind.Number || !id.TryGetInt32(out var parsed)) return false;
            processId = parsed;
        }
        if (!ReadString(values, "expected_process_name", out var name)
            || !ReadString(values, "expected_workspace", out var workspace)
            || !ReadPaths(values, "expected_sources", out var sources)
            || !ReadPaths(values, "expected_modules", out var modules)
            || !ReadPaths(values, "expected_artifacts", out var artifacts)) return false;
        var require = false;
        if (values.TryGetValue("require_active_process", out var active))
        {
            if (active.ValueKind is not (JsonValueKind.True or JsonValueKind.False)) return false;
            require = active.GetBoolean();
        }
        try
        {
            if (!string.IsNullOrEmpty(workspace)) _ = DisplayPath(workspace);
            foreach (var path in sources.Concat(modules).Concat(artifacts)) _ = DisplayPath(path);
        }
        catch (Exception exception) when (exception is ArgumentException or NotSupportedException or PathTooLongException)
        {
            return false;
        }
        arguments = new(processId, name, workspace, sources, modules, artifacts, require);
        return true;
    }

    private static bool ReadString(Dictionary<string, JsonElement> values, string key, out string? result)
    {
        result = null;
        if (!values.TryGetValue(key, out var value) || value.ValueKind == JsonValueKind.Null) return true;
        if (value.ValueKind != JsonValueKind.String) return false;
        result = value.GetString();
        return result is not null && !result.Contains('\0');
    }

    private static bool ReadPaths(Dictionary<string, JsonElement> values, string key, out string[] paths)
    {
        paths = [];
        if (!values.TryGetValue(key, out var value) || value.ValueKind == JsonValueKind.Null) return true;
        if (value.ValueKind != JsonValueKind.Array || value.GetArrayLength() > MaximumPaths) return false;
        var result = new List<string>();
        foreach (var item in value.EnumerateArray())
        {
            if (item.ValueKind != JsonValueKind.String || item.GetString() is not { } path || path.Contains('\0')) return false;
            result.Add(path);
        }
        paths = result.ToArray();
        return true;
    }

    private static string DisplayPath(string path)
    {
        if (path.Length == 0) return ".";
        var full = Path.GetFullPath(path);
        return Path.IsPathRooted(path) ? full : Path.GetRelativePath(Environment.CurrentDirectory, full);
    }

    private static bool SamePath(string left, string right) => PathComparer.Equals(Path.GetFullPath(left), Path.GetFullPath(right));
    private static bool Within(string path, string root)
    {
        var fullRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(root));
        var fullPath = Path.GetFullPath(path);
        var prefix = Path.EndsInDirectorySeparator(fullRoot) ? fullRoot : fullRoot + Path.DirectorySeparatorChar;
        return PathComparer.Equals(fullRoot, fullPath) || fullPath.StartsWith(prefix, OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal);
    }

    internal static object Verify(Arguments args, string? projectPath, int? processId, string? processName,
        IReadOnlyDictionary<string, DapLoadedSource>? sources, JsonElement modules)
    {
        var expectedWorkspace = string.IsNullOrEmpty(args.Workspace) ? null : DisplayPath(args.Workspace);
        var expectedSources = args.Sources.Select(DisplayPath).ToArray();
        var expectedModules = args.Modules.Select(DisplayPath).ToArray();
        var expectedArtifacts = args.Artifacts.Select(DisplayPath).ToArray();
        var warnings = new List<object>();
        var mismatches = new List<object>();
        if (args.ProcessId is not null || args.RequireActive)
        {
            if (processId is null) warnings.Add(new { kind = "process_id_unavailable", expected = args.ProcessId });
            else if (args.ProcessId is not null && processId != args.ProcessId) mismatches.Add(new { kind = "process_id_mismatch", expected = args.ProcessId, actual = processId });
        }
        if (!string.IsNullOrEmpty(args.ProcessName))
        {
            if (string.IsNullOrEmpty(processName)) warnings.Add(new { kind = "process_name_unavailable", expected = args.ProcessName });
            else if (!StringComparer.OrdinalIgnoreCase.Equals(processName, args.ProcessName)) mismatches.Add(new { kind = "process_name_mismatch", expected = args.ProcessName, actual = processName });
        }
        bool? matched = null;
        if (expectedWorkspace is not null)
        {
            if (string.IsNullOrEmpty(projectPath)) warnings.Add(new { kind = "workspace_unavailable", expected = expectedWorkspace });
            else
            {
                matched = SamePath(projectPath, expectedWorkspace);
                if (matched == false) mismatches.Add(new { kind = "workspace_mismatch", expected = expectedWorkspace, actual = DisplayPath(projectPath) });
            }
        }
        var sourcePaths = (sources?.Values ?? Enumerable.Empty<DapLoadedSource>()).Where(static source => !string.IsNullOrEmpty(source.Path)).Select(static source => DisplayPath(source.Path!)).ToArray();
        if (expectedSources.Length > 0 && sourcePaths.Length == 0) warnings.Add(new { kind = "loaded_sources_unavailable", expected = expectedSources });
        foreach (var path in expectedSources)
            if (sourcePaths.Length > 0 && !sourcePaths.Any(actual => SamePath(actual, path))) mismatches.Add(new { kind = "expected_source_missing", expected = path });
        if (expectedWorkspace is not null)
            foreach (var path in sourcePaths)
                if (!Within(path, expectedWorkspace)) mismatches.Add(new { kind = "source_workspace_mismatch", expected_workspace = expectedWorkspace, actual = path });
        var records = new List<Dictionary<string, object?>>();
        var modulePaths = new List<string>();
        var moduleNames = new SortedSet<string>(StringComparer.Ordinal);
        foreach (var module in modules.EnumerateArray())
        {
            var rawPath = module.TryGetProperty("path", out var p) && p.ValueKind == JsonValueKind.String ? p.GetString() : null;
            var path = string.IsNullOrEmpty(rawPath) ? null : DisplayPath(rawPath);
            var name = module.TryGetProperty("name", out var n) && n.ValueKind == JsonValueKind.String ? n.GetString() : null;
            if (string.IsNullOrEmpty(name)) name = path is null ? null : Path.GetFileName(path);
            var record = new Dictionary<string, object?> { ["name"] = name, ["path"] = path };
            foreach (var key in new[] { "id", "version", "isOptimized", "symbolStatus" })
                if (module.TryGetProperty(key, out var value) && value.ValueKind != JsonValueKind.Null) record[key] = value.Clone();
            records.Add(record);
            if (path is not null) modulePaths.Add(path);
            moduleNames.Add((name ?? string.Empty).ToLowerInvariant());
        }
        if (expectedModules.Length > 0 && records.Count == 0) warnings.Add(new { kind = "modules_unavailable", expected = expectedModules });
        foreach (var path in expectedModules)
            if (records.Count > 0 && !modulePaths.Any(actual => SamePath(actual, path)) && !moduleNames.Contains(Path.GetFileName(path).ToLowerInvariant()))
                mismatches.Add(new { kind = "expected_module_missing", expected = path });
        var existing = new List<string>();
        var missing = new List<string>();
        foreach (var path in expectedArtifacts)
        {
            if (File.Exists(path) || Directory.Exists(path)) existing.Add(path);
            else { missing.Add(path); mismatches.Add(new { kind = "artifact_missing", expected = path }); }
        }
        var status = mismatches.Count > 0 ? "FAIL" : warnings.Count > 0 ? "WARN" : "PASS";
        return new
        {
            status,
            reason = status == "PASS" ? "debug freshness verified" : status == "WARN" ? "debug freshness evidence incomplete" : "debug freshness mismatch",
            process = new { process_id = processId, process_name = processName, expected_process_id = args.ProcessId, expected_process_name = args.ProcessName },
            workspace = new { expected = expectedWorkspace, session_project_path = string.IsNullOrEmpty(projectPath) ? null : DisplayPath(projectPath), matched },
            loaded_sources = new { count = sourcePaths.Length, paths = sourcePaths, expected = expectedSources },
            modules = new { count = records.Count, paths = modulePaths, names = moduleNames, expected = expectedModules, records },
            artifacts = new { existing, missing },
            warnings,
            mismatches
        };
    }
}
