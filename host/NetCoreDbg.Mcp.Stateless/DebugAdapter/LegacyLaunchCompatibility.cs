using System.Diagnostics;
using System.Globalization;
using System.Numerics;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal static class LegacyLaunchCompatibility
{
    private static readonly Regex VersionPattern = new(@"^(\d+)\.(\d+)\.(\d+)(?:\.(\d+))?", RegexOptions.CultureInvariant | RegexOptions.NonBacktracking);
    private sealed record VersionValue(string Text, int Major);
    private sealed record TargetEvidence(string? Version, int? Major, string Path, string? Source, string Status);
    private sealed record ActiveEvidence(string? Version, int? Major, string? Path, string Source, string Status);
    private sealed record CandidateEvidence(string? Version, int? Major, string? Path, string? Provenance, string Selection, string Status);

    internal static object Inspect(string projectRoot, string program, string? debuggerPath)
    {
        var targetPath = ResolveProgram(projectRoot, program, out var linksResolved);

        var target = InspectTarget(targetPath);
        var active = InspectActive(debuggerPath);
        var candidate = target.Major is { } major ? InspectCandidate(major) : new CandidateEvidence(null, null, null, null, "not_attempted", "not_attempted");
        string verdict;
        bool? compatible;
        string? warning;
        if (target.Status != "known")
        {
            verdict = "unknown";
            compatible = null;
            warning = "Target runtime version is unavailable; compatibility cannot be determined.";
        }
        else if (active.Status != "known")
        {
            verdict = "unknown";
            compatible = null;
            warning = "Active dbgshim version is unavailable; native launch does not select a cached candidate.";
        }
        else if (target.Major == active.Major)
        {
            verdict = "compatible";
            compatible = true;
            warning = "Matching major versions are a heuristic; the debugger's loaded shim and runtime compatibility are not verified.";
        }
        else
        {
            verdict = "unknown";
            compatible = null;
            warning = "Target and adjacent dbgshim major versions differ; native launch does not select a cached candidate, and the loaded shim is not verified.";
        }
        if (!Path.IsPathFullyQualified(program))
            warning = (warning is null ? "" : warning + " ") + "The diagnostic resolves a relative program against the project root; native launch resolves it against the server working directory.";
        if (linksResolved)
            warning = (warning is null ? "" : warning + " ") + "The diagnostic resolves links; native pre-build launch may resolve them differently.";

        return new
        {
            verdict,
            program = targetPath,
            targetRuntime = new { version = target.Version, major = target.Major, runtimeconfigPath = target.Path, source = target.Source, status = target.Status },
            activeDbgshim = new { version = active.Version, major = active.Major, path = active.Path, source = active.Source, status = active.Status },
            cachedCandidate = new { version = candidate.Version, major = candidate.Major, path = candidate.Path, provenance = candidate.Provenance, selection = candidate.Selection, status = candidate.Status },
            compatible,
            willMutateSharedDebugger = false,
            mutationPerformed = false,
            warning,
        };
    }

    internal static string ResolveProgram(string baseDirectory, string program, out bool linksResolved)
    {
        var basePath = OperatingSystem.IsWindows() ? Path.GetFullPath(baseDirectory) : ResolvePosixPath(Environment.CurrentDirectory, baseDirectory);
        if (!Directory.Exists(basePath)) throw new ArgumentException("Resolved base directory is unavailable.");
        var requested = Path.GetFullPath(program, basePath);
        string canonical;
        if (OperatingSystem.IsWindows())
        {
            if (!NetCoreDbgSession.TryNormalizeLegacyBreakpointSource(requested, out canonical, requireExistingFile: true))
                throw new ArgumentException("Program path is unavailable.");
        }
        else
        {
            // POSIX resolves links before parent components, including through missing intermediate paths.
            canonical = ResolvePosixPath(basePath, program);
            if (!File.Exists(canonical)) throw new ArgumentException("Program path is unavailable.");
        }
        var comparison = OperatingSystem.IsWindows() ? StringComparison.OrdinalIgnoreCase : StringComparison.Ordinal;
        linksResolved = !string.Equals(requested, canonical, comparison);
        if (!DapBuildCoordinator.TryResolveBuiltProgram(canonical, out var effective))
            throw new ArgumentException("Program must be a .NET assembly (.dll/.exe).");
        if (string.Equals(canonical, effective, comparison)) return canonical;
        string resolved;
        if (OperatingSystem.IsWindows())
        {
            if (!NetCoreDbgSession.TryNormalizeLegacyBreakpointSource(effective!, out resolved, requireExistingFile: true))
                throw new ArgumentException("Resolved program path is unavailable.");
        }
        else
        {
            resolved = ResolvePosixPath(basePath, effective!);
            if (!File.Exists(resolved)) throw new ArgumentException("Resolved program path is unavailable.");
        }
        linksResolved |= !string.Equals(effective, resolved, comparison);
        return resolved;
    }

    private static string ResolvePosixPath(string basePath, string path)
    {
        var resolved = Path.IsPathRooted(path) ? "/" : basePath;
        var pending = new Stack<(string? Component, string? LinkKey)>();
        var seen = new Dictionary<string, string?>(StringComparer.Ordinal);
        void Push(string value)
        {
            var parts = value.Split('/', StringSplitOptions.RemoveEmptyEntries);
            for (var index = parts.Length - 1; index >= 0; index--) pending.Push((parts[index], null));
        }
        Push(path);
        while (pending.TryPop(out var part))
        {
            if (part.LinkKey is { } linkKey) { seen[linkKey] = resolved; continue; }
            var component = part.Component!;
            if (component == ".") continue;
            if (component == "..") { resolved = Path.GetDirectoryName(resolved) ?? "/"; continue; }
            var candidate = Path.Combine(resolved, component);
            string? link;
            try { link = new FileInfo(candidate).LinkTarget; }
            catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or System.Security.SecurityException) { link = null; }
            if (link is null) { resolved = candidate; continue; }
            if (seen.TryGetValue(candidate, out var previous))
            {
                if (previous is not null) { resolved = previous; continue; }
                var unresolved = candidate;
                foreach (var remaining in pending)
                    if (remaining.Component is { } name) unresolved = Path.Combine(unresolved, name);
                return Path.GetFullPath(unresolved);
            }
            seen[candidate] = null;
            pending.Push((null, candidate));
            if (Path.IsPathRooted(link)) resolved = "/";
            Push(link);
        }
        return Path.GetFullPath(resolved);
    }

    private static TargetEvidence InspectTarget(string program)
    {
        var configPath = Path.ChangeExtension(program, ".runtimeconfig.json");
        TargetEvidence Result(string status, VersionValue? version = null, string? source = null) =>
            new(version?.Text, version?.Major, configPath, source, status);
        try
        {
            using var stream = File.OpenRead(configPath);
            using var document = JsonDocument.Parse(stream);
            var root = document.RootElement;
            if (root.ValueKind != JsonValueKind.Object || !root.TryGetProperty("runtimeOptions", out var options) || options.ValueKind != JsonValueKind.Object)
                return Result("malformed");
            JsonElement versionElement = default;
            string? source = null;
            if (options.TryGetProperty("framework", out var framework))
            {
                if (framework.ValueKind != JsonValueKind.Object) return Result("malformed");
                if (framework.TryGetProperty("version", out var candidateVersion) && candidateVersion.ValueKind is not (JsonValueKind.Null or JsonValueKind.False)
                    && !(candidateVersion.ValueKind == JsonValueKind.String && candidateVersion.GetString() == ""))
                {
                    versionElement = candidateVersion;
                    source = "runtimeconfig_framework";
                }
            }
            if (source is null && options.TryGetProperty("frameworks", out var frameworks))
            {
                if (frameworks.ValueKind != JsonValueKind.Array) return Result("malformed");
                JsonElement selected = default;
                foreach (var item in frameworks.EnumerateArray())
                {
                    if (item.ValueKind != JsonValueKind.Object) return Result("malformed");
                    if (selected.ValueKind == JsonValueKind.Undefined) selected = item;
                    if (item.TryGetProperty("name", out var name) && name.ValueKind == JsonValueKind.String && name.GetString() == "Microsoft.NETCore.App")
                    {
                        selected = item;
                        break;
                    }
                }
                if (selected.ValueKind != JsonValueKind.Undefined)
                {
                    selected.TryGetProperty("version", out versionElement);
                    source = "runtimeconfig_frameworks";
                }
            }
            if (versionElement.ValueKind != JsonValueKind.String || ParseVersion(versionElement.GetString()) is not { } version)
                return Result("malformed");
            return Result("known", version, source);
        }
        catch (Exception exception) when (exception is FileNotFoundException or DirectoryNotFoundException) { return Result("missing"); }
        catch (JsonException) { return Result("malformed"); }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException) { return Result("unreadable"); }
    }

    private static ActiveEvidence InspectActive(string? debuggerPath)
    {
        var filename = OperatingSystem.IsWindows() ? "dbgshim.dll" : "libdbgshim.so";
        var source = OperatingSystem.IsWindows() ? "windows_file_version" : "unsupported_platform";
        if (string.IsNullOrWhiteSpace(debuggerPath)) return new(null, null, null, source, "missing");
        if (!Path.IsPathFullyQualified(debuggerPath)) return new(null, null, null, "unresolved_debugger_path", "unavailable");
        var path = Path.Combine(Path.GetDirectoryName(debuggerPath)!, filename);
        if (!OperatingSystem.IsWindows()) return new(null, null, path, source, "unsupported_platform");
        try
        {
            if ((File.GetAttributes(debuggerPath) & FileAttributes.Directory) != 0 || (File.GetAttributes(path) & FileAttributes.Directory) != 0)
                return new(null, null, path, source, "missing");
            var version = FileVersionInfo.GetVersionInfo(path);
            if (string.IsNullOrEmpty(version.FileVersion)) return new(null, null, path, source, "unreadable");
            return new($"{version.FileMajorPart}.{version.FileMinorPart}.{version.FileBuildPart}.{version.FilePrivatePart}",
                version.FileMajorPart, path, source, "known");
        }
        catch (Exception exception) when (exception is FileNotFoundException or DirectoryNotFoundException) { return new(null, null, path, source, "missing"); }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or System.ComponentModel.Win32Exception) { return new(null, null, path, source, "unreadable"); }
    }

    private static CandidateEvidence InspectCandidate(int targetMajor)
    {
        const string selection = "same_major_highest_numeric_tuple";
        CandidateEvidence Empty(string status) => new(null, null, null, null, selection, status);
        try
        {
            var home = LegacyDebuggerDiscovery.GetHomeDirectory();
            if (string.IsNullOrWhiteSpace(home)) return Empty("unreadable");
            var cache = Path.Combine(Path.GetFullPath(home), ".netcoredbg-mcp", "dbgshim");
            var filename = OperatingSystem.IsWindows() ? "dbgshim.dll" : "libdbgshim.so";
            if ((File.GetAttributes(cache) & FileAttributes.Directory) == 0) return Empty("no_match");
            string? bestPath = null;
            BigInteger[]? bestKey = null;
            foreach (var directory in Directory.EnumerateDirectories(cache))
            {
                var name = Path.GetFileName(directory);
                var parts = name.Split('.');
                if (!int.TryParse(parts[0], NumberStyles.None, CultureInfo.InvariantCulture, out var major) || major != targetMajor) continue;
                var candidate = Path.Combine(directory, filename);
                if (!File.Exists(candidate)) continue;
                var key = parts.Where(static part => part.Length != 0 && part.All(char.IsAsciiDigit))
                    .Select(static part => BigInteger.Parse(part, CultureInfo.InvariantCulture)).ToArray();
                if (bestKey is null || Compare(key, bestKey) > 0) { bestKey = key; bestPath = candidate; }
            }
            if (bestPath is null) return Empty("no_match");
            var version = ParseVersion(Path.GetFileName(Path.GetDirectoryName(bestPath)));
            return new(version?.Text, version?.Major, bestPath, "cache_directory_name", selection, version is null ? "malformed" : "known");
        }
        catch (Exception exception) when (exception is FileNotFoundException or DirectoryNotFoundException) { return Empty("missing"); }
        catch (Exception exception) when (exception is IOException or UnauthorizedAccessException or ArgumentException or NotSupportedException or System.Security.SecurityException) { return Empty("unreadable"); }
    }

    private static int Compare(BigInteger[] left, BigInteger[] right)
    {
        for (var index = 0; index < Math.Min(left.Length, right.Length); index++)
        {
            var comparison = left[index].CompareTo(right[index]);
            if (comparison != 0) return comparison;
        }
        return left.Length.CompareTo(right.Length);
    }

    private static VersionValue? ParseVersion(string? value)
    {
        if (string.IsNullOrEmpty(value)) return null;
        var match = VersionPattern.Match(value);
        if (!match.Success || !int.TryParse(match.Groups[1].Value, NumberStyles.None, CultureInfo.InvariantCulture, out var major)
            || !int.TryParse(match.Groups[2].Value, NumberStyles.None, CultureInfo.InvariantCulture, out var minor)
            || !int.TryParse(match.Groups[3].Value, NumberStyles.None, CultureInfo.InvariantCulture, out var patch)) return null;
        var text = $"{major}.{minor}.{patch}";
        if (match.Groups[4].Success)
        {
            if (!int.TryParse(match.Groups[4].Value, NumberStyles.None, CultureInfo.InvariantCulture, out var build)) return null;
            text += $".{build}";
        }
        return new(text, major);
    }
}
