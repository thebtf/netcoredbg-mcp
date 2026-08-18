using System.Globalization;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.RegularExpressions;
using ModelContextProtocol.Protocol;
using ModelContextProtocol.Server;

namespace NetCoreDbg.Mcp.Host;

/// <summary>
/// BCL-only implementation of Python's three deterministic project-scoped code-search tools.
/// It is deliberately request-local: resolving a client root here never changes Python's
/// SessionManager.project_path and scanning never shares mutable state between calls.
/// </summary>
internal sealed class NativeCodeSearch
{
    private const string IdleState = "idle";
    private const string IdleMessage = "No active debug session.";
    private const string UnconfiguredProjectMessage =
        "Project root is not configured. Start with --project or --project-from-cwd.";
    private static readonly string[] NextActions =
    [
        "find_code_symbol",
        "find_code_references",
        "get_source_context",
        "search_source",
    ];

    private readonly ProjectRootResolver _projectRootResolver;
    private readonly RelaySession _session;

    internal NativeCodeSearch(ProjectRootResolver projectRootResolver, RelaySession session)
    {
        _projectRootResolver = projectRootResolver;
        _session = session;
    }

    internal static bool IsNativeTool(string name) => NativeCodeSearchCatalog.IsNativeTool(name);

    internal async ValueTask<CallToolResult> CallAsync(
        RequestContext<CallToolRequestParams> context,
        CancellationToken cancellationToken)
    {
        using var linked = CancellationTokenSource.CreateLinkedTokenSource(
            cancellationToken,
            _session.SessionEndingToken);

        try
        {
            var arguments = context.Params.Arguments;
            return context.Params.Name switch
            {
                "find_code_symbol" => await FindCodeSymbolAsync(context.Server, arguments, linked.Token).ConfigureAwait(false),
                "find_code_references" => await FindCodeReferencesAsync(context.Server, arguments, linked.Token).ConfigureAwait(false),
                "get_source_context" => await GetSourceContextAsync(context.Server, arguments, linked.Token).ConfigureAwait(false),
                _ => throw new InvalidOperationException($"Unsupported native code-search tool: {context.Params.Name}"),
            };
        }
        catch (OperationCanceledException)
        {
            // MCP cancellation is not a tool result. Propagating it lets the SDK emit the
            // matching request cancellation and interrupts both roots/list and file scanning.
            throw;
        }
        catch (Exception ex)
        {
            return Error(ex.Message);
        }
    }

    private async ValueTask<CallToolResult> FindCodeSymbolAsync(
        McpServer server,
        IDictionary<string, JsonElement>? arguments,
        CancellationToken cancellationToken)
    {
        var name = RequiredString(arguments, "name");
        var kind = OptionalString(arguments, "kind");
        var engine = await CreateEngineAsync(server, cancellationToken).ConfigureAwait(false);
        var results = engine.FindCodeSymbol(name, kind, cancellationToken);
        return Success(new JsonObject
        {
            ["results"] = results,
            ["count"] = results.Count,
            ["project_root"] = engine.ProjectRoot,
        });
    }

    private async ValueTask<CallToolResult> FindCodeReferencesAsync(
        McpServer server,
        IDictionary<string, JsonElement>? arguments,
        CancellationToken cancellationToken)
    {
        var name = RequiredString(arguments, "name");
        var maxResults = OptionalInt32(arguments, "max_results", 1000);
        var engine = await CreateEngineAsync(server, cancellationToken).ConfigureAwait(false);
        var results = engine.FindCodeReferences(name, maxResults, cancellationToken);
        return Success(new JsonObject
        {
            ["results"] = results,
            ["count"] = results.Count,
            ["project_root"] = engine.ProjectRoot,
        });
    }

    private async ValueTask<CallToolResult> GetSourceContextAsync(
        McpServer server,
        IDictionary<string, JsonElement>? arguments,
        CancellationToken cancellationToken)
    {
        var file = RequiredString(arguments, "file");
        var line = RequiredInt32(arguments, "line");
        var radius = OptionalInt32(arguments, "radius", 10);
        var engine = await CreateEngineAsync(server, cancellationToken).ConfigureAwait(false);
        var sourceContext = engine.GetSourceContext(file, line, radius, cancellationToken);
        sourceContext["project_root"] = engine.ProjectRoot;
        return Success(sourceContext);
    }


    private async ValueTask<SourceSearchEngine> CreateEngineAsync(McpServer server, CancellationToken cancellationToken)
    {
        var projectRoot = await _projectRootResolver.ResolveAsync(server, cancellationToken).ConfigureAwait(false);
        if (projectRoot is null)
        {
            throw new InvalidOperationException(UnconfiguredProjectMessage);
        }

        return new SourceSearchEngine(projectRoot);
    }

    private static string RequiredString(IDictionary<string, JsonElement>? arguments, string name)
    {
        if (arguments?.TryGetValue(name, out var value) != true || value.ValueKind != JsonValueKind.String)
        {
            throw new ArgumentException($"{name} must be a string");
        }

        return value.GetString()!;
    }

    private static string? OptionalString(IDictionary<string, JsonElement>? arguments, string name)
    {
        if (arguments?.TryGetValue(name, out var value) != true || value.ValueKind == JsonValueKind.Null)
        {
            return null;
        }

        if (value.ValueKind != JsonValueKind.String)
        {
            throw new ArgumentException($"{name} must be a string or null");
        }

        return value.GetString();
    }

    private static int RequiredInt32(IDictionary<string, JsonElement>? arguments, string name)
    {
        if (arguments?.TryGetValue(name, out var value) != true
            || value.ValueKind != JsonValueKind.Number
            || !value.TryGetInt32(out var result))
        {
            throw new ArgumentException($"{name} must be an integer");
        }

        return result;
    }

    private static int OptionalInt32(IDictionary<string, JsonElement>? arguments, string name, int defaultValue)
    {
        if (arguments?.TryGetValue(name, out var value) != true)
        {
            return defaultValue;
        }

        if (value.ValueKind != JsonValueKind.Number || !value.TryGetInt32(out var result))
        {
            throw new ArgumentException($"{name} must be an integer");
        }

        return result;
    }


    private static CallToolResult Success(JsonObject data) => Result(new JsonObject
    {
        ["state"] = IdleState,
        ["next_actions"] = ActionsArray(),
        ["message"] = IdleMessage,
        ["data"] = data,
    }, isError: false);

    private static CallToolResult Error(string error) => Result(new JsonObject
    {
        ["error"] = error,
        ["state"] = IdleState,
        ["next_actions"] = ActionsArray(),
        ["message"] = $"Error: {error}. Try one of the suggested next_actions.",
    }, isError: false);

    private static JsonArray ActionsArray()
    {
        var actions = new JsonArray();
        foreach (var action in NextActions)
        {
            actions.Add(action);
        }

        return actions;
    }

    private static CallToolResult Result(JsonObject payload, bool isError) => new()
    {
        Content = [new TextContentBlock { Text = PythonJson.Serialize(payload) }],
        StructuredContent = JsonSerializer.SerializeToElement(payload),
        IsError = isError,
    };
}

/// <summary>Deterministic, project-bounded file traversal and source search.</summary>
internal sealed class SourceSearchEngine
{
    private static readonly HashSet<string> SourceExtensions = new(StringComparer.OrdinalIgnoreCase)
    {
        ".cs", ".xaml", ".axaml", ".csproj", ".json", ".config",
    };
    private static readonly HashSet<string> AlwaysIgnoredDirectories = new(StringComparer.Ordinal)
    {
        ".git", ".hg", ".svn",
    };
    private static readonly string[] SymbolKinds = ["class", "method", "property", "field"];
    private const int MaximumResults = 1000;
    private const string Modifiers =
        "public|private|protected|internal|static|abstract|sealed|partial|virtual|override|async|extern|readonly|const|volatile|required|new";
    private const string TypePattern = @"[\w.<>,\[\]?]+";

    private readonly string _projectRoot;
    private readonly IReadOnlyList<GitIgnoreRule> _ignoreRules;

    internal SourceSearchEngine(string projectRoot)
    {
        if (!Directory.Exists(projectRoot))
        {
            throw new DirectoryNotFoundException($"Project root is not a directory: {projectRoot}");
        }

        _projectRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(projectRoot));
        _ignoreRules = LoadGitIgnoreRules(_projectRoot);
    }

    internal string ProjectRoot => _projectRoot;

    internal JsonArray FindCodeSymbol(string name, string? kind, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            throw new ArgumentException("Symbol name must not be empty");
        }

        if (kind is not null && !SymbolKinds.Contains(kind, StringComparer.Ordinal))
        {
            throw new ArgumentException(
                $"Unsupported symbol kind '{kind}'. Supported kinds: class, field, method, property");
        }

        var kinds = kind is null ? SymbolKinds : [kind];
        var patterns = kinds.Select(symbolKind => (Kind: symbolKind, Pattern: CreateSymbolPattern(symbolKind, name))).ToArray();
        var results = new JsonArray();

        foreach (var path in SourceFiles("*.cs", cancellationToken))
        {
            var relativeFile = RelativePath(path);
            var lines = ReadLines(path);
            for (var index = 0; index < lines.Length; index++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                foreach (var (symbolKind, pattern) in patterns)
                {
                    if (!pattern.IsMatch(lines[index]))
                    {
                        continue;
                    }

                    results.Add(new JsonObject
                    {
                        ["file"] = relativeFile,
                        ["line"] = index + 1,
                        ["name"] = name,
                        ["kind"] = symbolKind,
                        ["context"] = lines[index].Trim(),
                    });
                    break;
                }
            }
        }

        return results;
    }

    internal JsonArray FindCodeReferences(string name, int maxResults, CancellationToken cancellationToken)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            throw new ArgumentException("Reference name must not be empty");
        }
        if (maxResults < 1)
        {
            throw new ArgumentException("max_results must be at least 1");
        }

        var limit = Math.Min(maxResults, MaximumResults);
        var referencePattern = CreateReferencePattern(name);
        var results = new JsonArray();

        foreach (var path in SourceFiles(null, cancellationToken))
        {
            var relativeFile = RelativePath(path);
            var lines = ReadLines(path);
            for (var index = 0; index < lines.Length; index++)
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!referencePattern.IsMatch(lines[index]))
                {
                    continue;
                }

                results.Add(new JsonObject
                {
                    ["file"] = relativeFile,
                    ["line"] = index + 1,
                    ["context"] = lines[index].Trim(),
                });
                if (results.Count >= limit)
                {
                    return results;
                }
            }
        }

        return results;
    }

    internal JsonObject GetSourceContext(string filePath, int line, int radius, CancellationToken cancellationToken)
    {
        if (line < 1)
        {
            throw new ArgumentException("line must be at least 1");
        }
        if (radius < 0)
        {
            throw new ArgumentException("radius must be non-negative");
        }

        var path = ResolveProjectFile(filePath, cancellationToken);
        var lines = ReadLines(path);
        if (line > lines.Length)
        {
            throw new ArgumentException($"line {line} is outside file range 1..{lines.Length}");
        }

        var startLine = Math.Max(1, line - radius);
        var endLine = Math.Min(lines.Length, line + radius);
        var selectedLines = new JsonArray();
        for (var lineNumber = startLine; lineNumber <= endLine; lineNumber++)
        {
            cancellationToken.ThrowIfCancellationRequested();
            selectedLines.Add(new JsonObject
            {
                ["line"] = lineNumber,
                ["text"] = lines[lineNumber - 1],
            });
        }

        return new JsonObject
        {
            ["file"] = RelativePath(path),
            ["start_line"] = startLine,
            ["end_line"] = endLine,
            ["lines"] = selectedLines,
        };
    }


    private IEnumerable<string> SourceFiles(string? fileGlob, CancellationToken cancellationToken)
    {
        foreach (var directory in TraversedDirectories(new DirectoryInfo(_projectRoot), cancellationToken))
        {
            foreach (var file in EnumerateFiles(directory))
            {
                cancellationToken.ThrowIfCancellationRequested();
                if (!IsSourceFile(file) || (fileGlob is not null && !MatchesFileGlob(file.FullName, fileGlob)))
                {
                    continue;
                }

                yield return file.FullName;
            }
        }
    }

    private IEnumerable<DirectoryInfo> TraversedDirectories(DirectoryInfo directory, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        yield return directory;

        foreach (var child in EnumerateDirectories(directory))
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (IsReparsePoint(child) || ShouldPruneDirectory(child.FullName))
            {
                continue;
            }

            foreach (var descendant in TraversedDirectories(child, cancellationToken))
            {
                yield return descendant;
            }
        }
    }

    private bool IsSourceFile(FileInfo file)
    {
        if (!SourceExtensions.Contains(file.Extension) || !file.Exists || !ResolvesWithinRoot(file))
        {
            return false;
        }

        return !IsIgnored(file.FullName, isDirectory: false);
    }

    private bool ShouldPruneDirectory(string path) =>
        IsIgnored(path, isDirectory: true) && !HasDescendantNegation(path);

    private bool IsIgnored(string path, bool isDirectory)
    {
        var relativePath = RelativePath(path);
        if (isDirectory && AlwaysIgnoredDirectories.Contains(Path.GetFileName(path)))
        {
            return true;
        }

        var ignored = false;
        foreach (var rule in _ignoreRules)
        {
            if (rule.Matches(relativePath, isDirectory))
            {
                ignored = !rule.Negated;
            }
        }

        return ignored;
    }

    private bool HasDescendantNegation(string path)
    {
        var relativePath = RelativePath(path).Trim('/');
        foreach (var rule in _ignoreRules)
        {
            if (!rule.Negated)
            {
                continue;
            }
            if (!rule.Anchored && !rule.HasSlash)
            {
                return true;
            }

            var pattern = rule.Pattern.Trim('/');
            if (string.Equals(pattern, relativePath, StringComparison.Ordinal)
                || pattern.StartsWith(relativePath + "/", StringComparison.Ordinal))
            {
                return true;
            }
        }

        return false;
    }

    private string ResolveProjectFile(string rawPath, CancellationToken cancellationToken)
    {
        var expandedPath = ExpandHome(rawPath);
        string candidate;
        try
        {
            candidate = Path.GetFullPath(
                Path.IsPathFullyQualified(expandedPath)
                    ? expandedPath
                    : Path.Combine(_projectRoot, expandedPath));
        }
        catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException)
        {
            throw new FileNotFoundException($"Source file not found: {rawPath}");
        }

        if (!IsWithinRoot(candidate))
        {
            throw new ArgumentException($"Path is outside project root: {rawPath}");
        }

        if (Directory.Exists(candidate))
        {
            throw new IOException($"Path is not a file: {rawPath}");
        }
        if (File.Exists(candidate))
        {
            var file = new FileInfo(candidate);
            if (IsSourceFile(file))
            {
                return candidate;
            }

            throw new FileNotFoundException($"Source file not found: {rawPath}");
        }

        if (IsBasenameOnly(rawPath))
        {
            return ResolveUniqueBasename(Path.GetFileName(rawPath), cancellationToken);
        }

        throw new FileNotFoundException($"Source file not found: {rawPath}");
    }

    private string ResolveUniqueBasename(string filename, CancellationToken cancellationToken)
    {
        var matches = SourceFiles(null, cancellationToken)
            .Where(path => string.Equals(Path.GetFileName(path), filename, StringComparison.Ordinal))
            .ToArray();
        return matches.Length switch
        {
            0 => throw new FileNotFoundException($"Source file not found: {filename}"),
            1 => matches[0],
            _ => throw new ArgumentException($"Source file basename is ambiguous: {filename}"),
        };
    }

    private static string ExpandHome(string path)
    {
        if (path.Length == 1 && path[0] == '~')
        {
            return Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        }
        if (path.Length > 1 && path[0] == '~' && (path[1] == '/' || path[1] == '\\'))
        {
            return Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), path[2..]);
        }

        return path;
    }

    private static bool IsBasenameOnly(string path) =>
        path.IndexOf(Path.DirectorySeparatorChar) < 0
        && path.IndexOf(Path.AltDirectorySeparatorChar) < 0;

    private bool MatchesFileGlob(string path, string fileGlob)
    {
        var relativePath = RelativePath(path);
        return GlobMatches(relativePath, fileGlob) || GlobMatches(Path.GetFileName(path), fileGlob);
    }

    private string RelativePath(string path) => Path.GetRelativePath(_projectRoot, path).Replace('\\', '/');

    private bool ResolvesWithinRoot(FileInfo file)
    {
        try
        {
            var resolved = file.ResolveLinkTarget(returnFinalTarget: true)?.FullName ?? file.FullName;
            return IsWithinRoot(resolved);
        }
        catch (IOException)
        {
            return false;
        }
    }

    private bool IsWithinRoot(string path)
    {
        var relative = Path.GetRelativePath(_projectRoot, Path.GetFullPath(path));
        return !string.Equals(relative, "..", StringComparison.Ordinal)
            && !relative.StartsWith(".." + Path.DirectorySeparatorChar, StringComparison.Ordinal)
            && !Path.IsPathFullyQualified(relative);
    }

    private static Regex CreateSymbolPattern(string kind, string name)
    {
        var escapedName = Regex.Escape(name);
        var pattern = kind switch
        {
            "class" => $@"^\s*(?:(?:{Modifiers})\s+)*(?:class|record|struct|interface)\s+{escapedName}\b",
            "method" => $@"^\s*(?:(?:{Modifiers})\s+)*(?:(?:{TypePattern}\s+)+{escapedName}|{escapedName})\s*\(",
            "property" => $@"^\s*(?:(?:{Modifiers})\s+)*(?:{TypePattern}\s+)+{escapedName}\s*\{{",
            "field" => $@"^\s*(?:(?:{Modifiers})\s+)*(?:{TypePattern}\s+)+{escapedName}\s*(?:=|;)",
            _ => throw new ArgumentOutOfRangeException(nameof(kind)),
        };
        return new Regex(pattern, RegexOptions.CultureInvariant);
    }

    private static Regex CreateReferencePattern(string name)
    {
        var escaped = Regex.Escape(name);
        var pattern = Regex.IsMatch(name, "^[A-Za-z_][A-Za-z0-9_]*$", RegexOptions.CultureInvariant)
            ? $@"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
            : escaped;
        return new Regex(pattern, RegexOptions.CultureInvariant);
    }

    private static string[] ReadLines(string path)
    {
        var text = new UTF8Encoding(
            encoderShouldEmitUTF8Identifier: false,
            throwOnInvalidBytes: false).GetString(File.ReadAllBytes(path));
        var lines = new List<string>();
        var start = 0;
        for (var index = 0; index < text.Length; index++)
        {
            var character = text[index];
            var isLineBreak = character is '\n' or '\r' or '\v' or '\f'
                or '\u001c' or '\u001d' or '\u001e' or '\u0085' or '\u2028' or '\u2029';
            if (!isLineBreak)
            {
                continue;
            }

            lines.Add(text[start..index]);
            if (character == '\r' && index + 1 < text.Length && text[index + 1] == '\n')
            {
                index++;
            }
            start = index + 1;
        }

        if (start < text.Length)
        {
            lines.Add(text[start..]);
        }
        return lines.ToArray();
    }

    private static IReadOnlyList<DirectoryInfo> EnumerateDirectories(DirectoryInfo directory)
    {
        try
        {
            return directory.EnumerateDirectories().OrderBy(static entry => entry.Name, StringComparer.Ordinal).ToArray();
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
        {
            return [];
        }
    }

    private static IReadOnlyList<FileInfo> EnumerateFiles(DirectoryInfo directory)
    {
        try
        {
            return directory.EnumerateFiles().OrderBy(static entry => entry.Name, StringComparer.Ordinal).ToArray();
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
        {
            return [];
        }
    }

    private static bool IsReparsePoint(DirectoryInfo directory)
    {
        try
        {
            return (directory.Attributes & FileAttributes.ReparsePoint) != 0;
        }
        catch (IOException)
        {
            return true;
        }
    }

    private static IReadOnlyList<GitIgnoreRule> LoadGitIgnoreRules(string root)
    {
        var gitIgnore = Path.Combine(root, ".gitignore");
        if (!File.Exists(gitIgnore))
        {
            return [];
        }

        return ReadLines(gitIgnore)
            .Select(GitIgnoreRule.Parse)
            .Where(static rule => rule is not null)
            .Select(static rule => rule!)
            .ToArray();
    }

    private static bool GlobMatches(string value, string pattern)
    {
        var expression = new StringBuilder("^");
        for (var index = 0; index < pattern.Length; index++)
        {
            switch (pattern[index])
            {
                case '*':
                    expression.Append(".*");
                    break;
                case '?':
                    expression.Append('.');
                    break;
                case '[':
                    var closing = pattern.IndexOf(']', index + 1);
                    if (closing <= index + 1)
                    {
                        expression.Append("\\[");
                        break;
                    }

                    var characterClass = pattern[(index + 1)..closing];
                    expression.Append('[');
                    expression.Append(characterClass[0] == '!' ? "^" + characterClass[1..] : characterClass);
                    expression.Append(']');
                    index = closing;
                    break;
                default:
                    expression.Append(Regex.Escape(pattern[index].ToString()));
                    break;
            }
        }
        expression.Append('$');
        return Regex.IsMatch(value, expression.ToString(), RegexOptions.CultureInvariant);
    }




    private sealed record GitIgnoreRule(
        string Pattern,
        bool Negated,
        bool DirectoryOnly,
        bool Anchored,
        bool HasSlash)
    {
        internal static GitIgnoreRule? Parse(string line)
        {
            var value = line.Trim();
            if (value.Length == 0 || value.StartsWith('#'))
            {
                return null;
            }

            var negated = value.StartsWith('!');
            if (negated)
            {
                value = value[1..].Trim();
                if (value.Length == 0)
                {
                    return null;
                }
            }

            var anchored = value.StartsWith('/');
            if (anchored)
            {
                value = value.TrimStart('/');
            }

            var directoryOnly = value.EndsWith('/');
            value = value.TrimEnd('/');
            return value.Length == 0
                ? null
                : new GitIgnoreRule(value, negated, directoryOnly, anchored, value.Contains('/'));
        }

        internal bool Matches(string relativePath, bool isDirectory)
        {
            var path = relativePath.Replace('\\', '/').Trim('/');
            if (path.Length == 0)
            {
                return false;
            }

            if (DirectoryOnly && !isDirectory)
            {
                if (Anchored || HasSlash)
                {
                    return string.Equals(path, Pattern, StringComparison.Ordinal)
                        || path.StartsWith(Pattern + "/", StringComparison.Ordinal);
                }

                return path.Split('/').SkipLast(1).Any(part => GlobMatches(part, Pattern));
            }

            if (Anchored || HasSlash)
            {
                return GlobMatches(path, Pattern);
            }
            if (DirectoryOnly)
            {
                return path.Split('/').Any(part => GlobMatches(part, Pattern));
            }

            return GlobMatches(Path.GetFileName(path), Pattern);
        }
    }
}

/// <summary>Exact public definitions for the three deterministic native replacements.</summary>
internal static class NativeCodeSearchCatalog
{
    private static readonly string[] Names =
    [
        "find_code_symbol",
        "find_code_references",
        "get_source_context",
    ];

    internal static bool IsNativeTool(string name) => Names.Contains(name, StringComparer.Ordinal);

    internal static void ReplaceInCatalog(IList<Tool> tools)
    {
        var seen = new HashSet<string>(StringComparer.Ordinal);
        for (var index = 0; index < tools.Count; index++)
        {
            var name = tools[index].Name;
            if (!seen.Add(name))
            {
                throw new InvalidOperationException($"Python tools/list returned duplicate tool name '{name}'.");
            }

            if (IsNativeTool(name))
            {
                tools[index] = Definition(name);
            }
        }

    }

    private static Tool Definition(string name) => name switch
    {
        "find_code_symbol" => Tool(
            name,
            "Find a C# symbol definition by name and optional kind.",
            """{"properties":{"name":{"title":"Name","type":"string"},"kind":{"anyOf":[{"type":"string"},{"type":"null"}],"default":null,"title":"Kind"}},"required":["name"],"title":"find_code_symbolArguments","type":"object"}"""),
        "find_code_references" => Tool(
            name,
            "Find literal symbol references across project files.",
            """{"properties":{"name":{"title":"Name","type":"string"},"max_results":{"default":1000,"title":"Max Results","type":"integer"}},"required":["name"],"title":"find_code_referencesArguments","type":"object"}"""),
        "get_source_context" => Tool(
            name,
            "Read source lines around a project-scoped location.",
            """{"properties":{"file":{"title":"File","type":"string"},"line":{"title":"Line","type":"integer"},"radius":{"default":10,"title":"Radius","type":"integer"}},"required":["file","line"],"title":"get_source_contextArguments","type":"object"}"""),
        _ => throw new ArgumentOutOfRangeException(nameof(name)),
    };

    private static Tool Tool(string name, string description, string inputSchema) => new()
    {
        Name = name,
        Description = description,
        InputSchema = JsonDocument.Parse(inputSchema).RootElement.Clone(),
        OutputSchema = JsonDocument.Parse($"{{\"additionalProperties\":true,\"title\":\"{name}DictOutput\",\"type\":\"object\"}}").RootElement.Clone(),
        Annotations = new ToolAnnotations
        {
            ReadOnlyHint = true,
            IdempotentHint = true,
            OpenWorldHint = false,
        },
    };
}

/// <summary>Writes the text block with Python json.dumps(..., indent=2, ensure_ascii=True) semantics.</summary>
internal static class PythonJson
{
    internal static string Serialize(JsonNode node)
    {
        using var document = JsonDocument.Parse(node.ToJsonString());
        var result = new StringBuilder();
        Write(document.RootElement, result, 0);
        return result.ToString();
    }

    private static void Write(JsonElement element, StringBuilder output, int depth)
    {
        switch (element.ValueKind)
        {
            case JsonValueKind.Object:
                var properties = element.EnumerateObject().ToArray();
                output.Append('{');
                if (properties.Length == 0)
                {
                    output.Append('}');
                    return;
                }

                output.Append('\n');
                for (var index = 0; index < properties.Length; index++)
                {
                    Indent(output, depth + 1);
                    WriteString(properties[index].Name, output);
                    output.Append(": ");
                    Write(properties[index].Value, output, depth + 1);
                    if (index + 1 < properties.Length)
                    {
                        output.Append(',');
                    }
                    output.Append('\n');
                }
                Indent(output, depth);
                output.Append('}');
                return;

            case JsonValueKind.Array:
                var values = element.EnumerateArray().ToArray();
                output.Append('[');
                if (values.Length == 0)
                {
                    output.Append(']');
                    return;
                }

                output.Append('\n');
                for (var index = 0; index < values.Length; index++)
                {
                    Indent(output, depth + 1);
                    Write(values[index], output, depth + 1);
                    if (index + 1 < values.Length)
                    {
                        output.Append(',');
                    }
                    output.Append('\n');
                }
                Indent(output, depth);
                output.Append(']');
                return;

            case JsonValueKind.String:
                WriteString(element.GetString()!, output);
                return;
            case JsonValueKind.Number:
                output.Append(element.GetRawText());
                return;
            case JsonValueKind.True:
                output.Append("true");
                return;
            case JsonValueKind.False:
                output.Append("false");
                return;
            case JsonValueKind.Null:
                output.Append("null");
                return;
            default:
                throw new InvalidOperationException($"Unsupported JSON value kind: {element.ValueKind}");
        }
    }

    private static void Indent(StringBuilder output, int depth) => output.Append(' ', depth * 2);

    private static void WriteString(string value, StringBuilder output)
    {
        output.Append('"');
        foreach (var character in value)
        {
            switch (character)
            {
                case '"': output.Append("\\\""); break;
                case '\\': output.Append("\\\\"); break;
                case '\b': output.Append("\\b"); break;
                case '\f': output.Append("\\f"); break;
                case '\n': output.Append("\\n"); break;
                case '\r': output.Append("\\r"); break;
                case '\t': output.Append("\\t"); break;
                case var control when control < ' ':
                    output.Append("\\u");
                    output.Append(((int)control).ToString("x4", CultureInfo.InvariantCulture));
                    break;
                default:
                    output.Append(character);
                    break;
            }
        }
        output.Append('"');
    }
}
