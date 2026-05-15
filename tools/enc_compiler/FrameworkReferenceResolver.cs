using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Xml.Linq;

namespace NetcoredbgMcp.EncCompiler;

internal static class FrameworkReferenceResolver
{
    public static IEnumerable<string> GetReferencePaths(string? projectFile, string? targetFramework)
    {
        if (projectFile is null)
        {
            yield break;
        }

        foreach (var frameworkReference in GetFrameworkReferenceNames(projectFile, targetFramework)
            .Distinct(StringComparer.OrdinalIgnoreCase))
        {
            var referenceDirectory = ResolveFrameworkReferenceDirectory(frameworkReference, targetFramework);
            if (referenceDirectory is null)
            {
                continue;
            }

            foreach (var referencePath in Directory.EnumerateFiles(referenceDirectory, "*.dll", SearchOption.TopDirectoryOnly)
                .Order(StringComparer.OrdinalIgnoreCase))
            {
                yield return referencePath;
            }
        }
    }

    private static IEnumerable<string> GetFrameworkReferenceNames(string projectFile, string? targetFramework)
    {
        var assetsPath = Path.Combine(Path.GetDirectoryName(projectFile)!, "obj", "project.assets.json");
        if (File.Exists(assetsPath))
        {
            using var document = JsonDocument.Parse(File.ReadAllText(assetsPath, Encoding.UTF8));
            if (document.RootElement.TryGetProperty("project", out var project)
                && project.TryGetProperty("frameworks", out var frameworks))
            {
                var foundFrameworkReferences = false;
                foreach (var framework in frameworks.EnumerateObject())
                {
                    if (!IsTargetFrameworkMatch(framework, targetFramework)
                        || !framework.Value.TryGetProperty("frameworkReferences", out var frameworkReferences))
                    {
                        continue;
                    }

                    foreach (var frameworkReference in frameworkReferences.EnumerateObject())
                    {
                        foundFrameworkReferences = true;
                        yield return frameworkReference.Name;
                    }
                }

                if (foundFrameworkReferences)
                {
                    yield break;
                }
            }
        }

        if (targetFramework?.StartsWith("net", StringComparison.OrdinalIgnoreCase) == true)
        {
            yield return "Microsoft.NETCore.App";
        }

        if (GetBooleanProjectProperty(projectFile, "UseWPF") || GetBooleanProjectProperty(projectFile, "UseWindowsForms"))
        {
            yield return "Microsoft.WindowsDesktop.App";
        }
    }

    private static bool IsTargetFrameworkMatch(JsonProperty framework, string? targetFramework)
    {
        if (targetFramework is null)
        {
            return true;
        }

        if (string.Equals(framework.Name, targetFramework, StringComparison.OrdinalIgnoreCase))
        {
            return true;
        }

        return framework.Value.TryGetProperty("targetAlias", out var targetAlias)
            && string.Equals(targetAlias.GetString(), targetFramework, StringComparison.OrdinalIgnoreCase);
    }

    private static string? ResolveFrameworkReferenceDirectory(string frameworkReference, string? targetFramework)
    {
        var dotnetRoot = ResolveDotnetRoot();
        if (dotnetRoot is null)
        {
            return null;
        }

        var packName = frameworkReference.EndsWith(".Ref", StringComparison.OrdinalIgnoreCase)
            ? frameworkReference
            : $"{frameworkReference}.Ref";
        var packRoot = Path.Combine(dotnetRoot, "packs", packName);
        if (!Directory.Exists(packRoot))
        {
            return null;
        }

        var versionPrefix = GetTargetFrameworkVersionPrefix(targetFramework);
        var packVersionDirectory = Directory.EnumerateDirectories(packRoot)
            .Where(path => versionPrefix is null || Path.GetFileName(path).StartsWith(versionPrefix, StringComparison.OrdinalIgnoreCase))
            .OrderByDescending(path => ParseVersionOrDefault(Path.GetFileName(path)))
            .FirstOrDefault();
        return packVersionDirectory is null
            ? null
            : ResolveReferenceDirectory(packVersionDirectory, targetFramework);
    }

    private static string? ResolveDotnetRoot()
    {
        var configuredRoot = Environment.GetEnvironmentVariable("DOTNET_ROOT");
        if (!string.IsNullOrWhiteSpace(configuredRoot) && Directory.Exists(configuredRoot))
        {
            return configuredRoot;
        }

        var processDirectory = Path.GetDirectoryName(Environment.ProcessPath);
        if (!string.IsNullOrWhiteSpace(processDirectory) && Directory.Exists(Path.Combine(processDirectory, "packs")))
        {
            return processDirectory;
        }

        const string defaultWindowsRoot = @"C:\Program Files\dotnet";
        if (Directory.Exists(defaultWindowsRoot))
        {
            return defaultWindowsRoot;
        }

        foreach (var candidateRoot in new[] { "/usr/share/dotnet", "/usr/local/share/dotnet", "/opt/dotnet" })
        {
            if (Directory.Exists(candidateRoot))
            {
                return candidateRoot;
            }
        }

        return null;
    }

    private static string? ResolveReferenceDirectory(string packVersionDirectory, string? targetFramework)
    {
        var referenceRoot = Path.Combine(packVersionDirectory, "ref");
        if (!Directory.Exists(referenceRoot))
        {
            return null;
        }

        foreach (var candidateFramework in GetReferenceFrameworkCandidates(targetFramework))
        {
            var candidate = Path.Combine(referenceRoot, candidateFramework);
            if (Directory.Exists(candidate))
            {
                return candidate;
            }
        }

        return Directory.EnumerateDirectories(referenceRoot)
            .OrderByDescending(path => path, StringComparer.OrdinalIgnoreCase)
            .FirstOrDefault();
    }

    private static IEnumerable<string> GetReferenceFrameworkCandidates(string? targetFramework)
    {
        if (string.IsNullOrWhiteSpace(targetFramework))
        {
            yield break;
        }

        yield return targetFramework;
        var platformIndex = targetFramework.IndexOf('-', StringComparison.Ordinal);
        if (platformIndex > 0)
        {
            yield return targetFramework[..platformIndex];
        }
    }

    private static string? GetTargetFrameworkVersionPrefix(string? targetFramework)
    {
        if (string.IsNullOrWhiteSpace(targetFramework))
        {
            return null;
        }

        var match = Regex.Match(targetFramework, "^net(?<major>\\d+)\\.(?<minor>\\d+)", RegexOptions.IgnoreCase);
        return match.Success
            ? $"{match.Groups["major"].Value}.{match.Groups["minor"].Value}."
            : null;
    }

    private static Version ParseVersionOrDefault(string value)
    {
        return Version.TryParse(value, out var version) ? version : new Version(0, 0);
    }

    private static bool GetBooleanProjectProperty(string projectFile, string propertyName)
    {
        if (!File.Exists(projectFile))
        {
            return false;
        }

        var value = XDocument.Load(projectFile)
            .Descendants()
            .FirstOrDefault(element => element.Name.LocalName == propertyName)
            ?.Value
            .Trim();
        return bool.TryParse(value, out var parsed) && parsed;
    }
}
