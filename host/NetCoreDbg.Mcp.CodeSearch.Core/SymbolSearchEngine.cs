using System.Diagnostics;
using System.Text;
using System.Text.RegularExpressions;

namespace NetCoreDbg.Mcp.CodeSearch.Core;

/// <summary>Deterministic, project-bounded source traversal and C# symbol matching.</summary>
public sealed class SymbolSearchEngine
{
    private static readonly HashSet<string> AlwaysIgnoredDirectories = new(StringComparer.Ordinal)
    {
        ".git", ".hg", ".svn",
    };
    private static readonly string[] SymbolKinds = ["class", "method", "property", "field"];
    private const string Modifiers =
        "public|private|protected|internal|static|abstract|sealed|partial|virtual|override|async|extern|readonly|const|volatile|required|new";
    private const string TypePattern = @"[\w.<>,\[\]?]+";

    private readonly string _projectRoot;
    private readonly SearchPolicySettings _settings;
    private readonly IStrictPathInspector _strictPathInspector;
    private readonly Func<long>? _timestamp;
    private readonly Func<DirectoryInfo, IEnumerable<DirectoryInfo>> _enumerateDirectories;
    private readonly Func<DirectoryInfo, IEnumerable<FileInfo>> _enumerateFiles;
    private readonly Func<Regex, string, bool>? _strictRegexMatch;
    private readonly Func<string, Stream> _openRead;

    public SymbolSearchEngine(string projectRoot, SearchPolicy policy)
        : this(projectRoot, policy, FileSystemStrictPathInspector.Instance)
    {
    }

    internal SymbolSearchEngine(
        string projectRoot,
        SearchPolicy policy,
        IStrictPathInspector strictPathInspector,
        Func<long>? timestamp = null,
        Func<DirectoryInfo, IEnumerable<DirectoryInfo>>? enumerateDirectories = null,
        Func<DirectoryInfo, IEnumerable<FileInfo>>? enumerateFiles = null,
        Func<Regex, string, bool>? strictRegexMatch = null,
        Func<string, Stream>? openRead = null,
        Func<string, bool>? directoryExists = null)
    {
        ArgumentNullException.ThrowIfNull(projectRoot);
        ArgumentNullException.ThrowIfNull(policy);
        ArgumentNullException.ThrowIfNull(strictPathInspector);
        if (!policy.Settings.Strict
            && !(directoryExists?.Invoke(projectRoot) ?? Directory.Exists(projectRoot)))
        {
            throw new DirectoryNotFoundException($"Project root is not a directory: {projectRoot}");
        }

        _projectRoot = Path.TrimEndingDirectorySeparator(Path.GetFullPath(projectRoot));
        _settings = policy.Settings;
        _strictPathInspector = strictPathInspector;
        _timestamp = timestamp;
        _enumerateDirectories = enumerateDirectories ?? (static directory => directory.EnumerateDirectories());
        _enumerateFiles = enumerateFiles ?? (static directory => directory.EnumerateFiles());
        _strictRegexMatch = strictRegexMatch;
        _openRead = openRead ?? (static path => new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.Read, 4096, FileOptions.SequentialScan));
    }

    public string ProjectRoot => _projectRoot;

    public IReadOnlyList<SymbolMatch> FindCodeSymbol(
        string name,
        string? kind = null,
        CancellationToken cancellationToken = default)
    {
        ValidateSymbolArguments(name, kind, "find_code_symbol");
        var operation = StartOperation("find_code_symbol", cancellationToken);
        var rules = LoadGitIgnoreRules(operation);
        var kinds = kind is null ? SymbolKinds : [kind];
        var patterns = kinds.Select(symbolKind => (Kind: symbolKind, Pattern: CreateSymbolPattern(symbolKind, name, _strictRegexMatch))).ToArray();
        var results = new List<SymbolMatch>();

        foreach (var path in SourceFiles("*.cs", rules, operation))
        {
            var relativeFile = RelativePath(path);
            var lines = ReadLines(path, operation);
            for (var index = 0; index < lines.Length; index++)
            {
                operation.Check();
                foreach (var (symbolKind, pattern) in patterns)
                {
                    if (!pattern.IsMatch(lines[index], _settings.Strict ? operation : null))
                    {
                        continue;
                    }

                    AddSymbolResult(results, new SymbolMatch(
                        relativeFile,
                        index + 1,
                        name,
                        symbolKind,
                        FormatContext(lines[index])), operation);
                    break;
                }
            }
        }

        if (_settings.Strict)
        {
            results.Sort(SymbolMatchComparer.Instance);
        }

        return results;
    }

    public IReadOnlyList<ReferenceMatch> FindCodeReferences(
        string name,
        int maxResults,
        CancellationToken cancellationToken = default)
    {
        ValidateReferenceArguments(name, maxResults, "find_code_references");
        var operation = StartOperation("find_code_references", cancellationToken);
        var rules = LoadGitIgnoreRules(operation);
        var limit = Math.Min(maxResults, _settings.MaximumReferenceResults);
        var policyCeiling = _settings.MaximumReferenceResults;
        var referencePattern = CreateReferencePattern(name, _strictRegexMatch);
        var results = new List<ReferenceMatch>();

        foreach (var path in SourceFiles(null, rules, operation))
        {
            var relativeFile = RelativePath(path);
            var lines = ReadLines(path, operation);
            for (var index = 0; index < lines.Length; index++)
            {
                operation.Check();
                if (!referencePattern.IsMatch(lines[index], _settings.Strict ? operation : null))
                {
                    continue;
                }

                if (_settings.Strict && results.Count == policyCeiling)
                {
                    ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(operation.Tool));
                }

                results.Add(new ReferenceMatch(relativeFile, index + 1, FormatContext(lines[index])));
                if (!_settings.Strict && results.Count >= limit)
                {
                    return results;
                }
                if (_settings.Strict && maxResults < policyCeiling && results.Count == maxResults)
                {
                    results.Sort(ReferenceMatchComparer.Instance);
                    return results;
                }
            }
        }

        if (_settings.Strict)
        {
            results.Sort(ReferenceMatchComparer.Instance);
        }

        return results;
    }

    public SourceContext GetSourceContext(
        string filePath,
        int line,
        int radius,
        CancellationToken cancellationToken = default)
    {
        ValidateContextArguments(filePath, line, radius, "get_source_context");
        var operation = StartOperation("get_source_context", cancellationToken);
        var rules = LoadGitIgnoreRules(operation);
        var path = ResolveProjectFile(filePath, rules, operation);
        var lines = ReadLines(path, operation);
        if (line > lines.Length)
        {
            ThrowArgumentOrFailure($"line {line} is outside file range 1..{lines.Length}", operation.Tool);
        }

        var startLine = (int)Math.Max(1L, (long)line - radius);
        var endLine = (int)Math.Min(lines.Length, (long)line + radius);
        var selectedLines = new List<SourceLine>(endLine - startLine + 1);
        for (var lineNumber = startLine; lineNumber <= endLine; lineNumber++)
        {
            operation.Check();
            selectedLines.Add(new SourceLine(lineNumber, FormatContext(lines[lineNumber - 1], trim: false)));
        }

        return new SourceContext(RelativePath(path), startLine, endLine, selectedLines);
    }

    private SearchOperation StartOperation(string tool, CancellationToken cancellationToken)
    {
        var operation = new SearchOperation(_settings, tool, cancellationToken, _timestamp);
        if (_settings.Strict)
        {
            operation.Check();
            VerifyStrictDirectory(new DirectoryInfo(_projectRoot), operation);
        }

        return operation;
    }

    private void ValidateSymbolArguments(string name, string? kind, string tool)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            ThrowArgumentOrFailure("Symbol name must not be empty", tool);
        }
        if (_settings.MaximumNameUtf16CodeUnits is int maximumNameUtf16CodeUnits && name.Length > maximumNameUtf16CodeUnits)
        {
            ThrowFailure(SearchFailure.InvalidToolArguments(tool));
        }
        if (kind is not null && !SymbolKinds.Contains(kind, StringComparer.Ordinal))
        {
            ThrowArgumentOrFailure(
                $"Unsupported symbol kind '{kind}'. Supported kinds: class, field, method, property",
                tool);
        }
    }

    private void ValidateReferenceArguments(string name, int maxResults, string tool)
    {
        if (string.IsNullOrWhiteSpace(name))
        {
            ThrowArgumentOrFailure("Reference name must not be empty", tool);
        }
        if (_settings.MaximumNameUtf16CodeUnits is int maximumNameUtf16CodeUnits && name.Length > maximumNameUtf16CodeUnits)
        {
            ThrowFailure(SearchFailure.InvalidToolArguments(tool));
        }
        if (maxResults < 1)
        {
            ThrowArgumentOrFailure("max_results must be at least 1", tool);
        }
    }

    private void ValidateContextArguments(string filePath, int line, int radius, string tool)
    {
        if (_settings.Strict && string.IsNullOrWhiteSpace(filePath))
        {
            ThrowFailure(SearchFailure.InvalidToolArguments(tool));
        }
        if (line < 1)
        {
            ThrowArgumentOrFailure("line must be at least 1", tool);
        }
        if (radius < 0)
        {
            ThrowArgumentOrFailure("radius must be non-negative", tool);
        }
        if (_settings.Strict && radius > int.MaxValue - line)
        {
            ThrowFailure(SearchFailure.InvalidToolArguments(tool));
        }
    }

    private void ThrowArgumentOrFailure(string message, string tool)
    {
        if (_settings.Strict)
        {
            ThrowFailure(SearchFailure.InvalidToolArguments(tool));
        }

        throw new ArgumentException(message);
    }

    private static void ThrowFailure(SearchFailure failure) => throw new SearchFailureException(failure);

    private void AddSymbolResult(List<SymbolMatch> results, SymbolMatch result, SearchOperation operation)
    {
        if (_settings.MaximumSymbolResults is int maximumResults && results.Count == maximumResults)
        {
            ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(operation.Tool));
        }

        results.Add(result);
    }

    private IEnumerable<string> SourceFiles(
        string? fileGlob,
        IReadOnlyList<GitIgnoreRule> rules,
        SearchOperation operation)
    {
        foreach (var directory in TraversedDirectories(new DirectoryInfo(_projectRoot), rules, operation))
        {
            foreach (var file in EnumerateFiles(directory, operation))
            {
                if (!_settings.Strict)
                {
                    operation.ExamineNonDirectoryEntry();
                }
                if (!IsSourceFile(file, rules, operation)
                    || (fileGlob is not null && !MatchesFileGlob(file.FullName, fileGlob)))
                {
                    continue;
                }

                yield return file.FullName;
            }
        }
    }

    private IEnumerable<DirectoryInfo> TraversedDirectories(
        DirectoryInfo directory,
        IReadOnlyList<GitIgnoreRule> rules,
        SearchOperation operation,
        bool directoryAlreadyEntered = false)
    {
        if (!directoryAlreadyEntered)
        {
            operation.EnterDirectory();
        }
        if (_settings.Strict)
        {
            VerifyStrictDirectory(directory, operation);
        }

        yield return directory;
        foreach (var child in EnumerateDirectories(directory, rules, operation))
        {
            operation.Check();
            if (!_settings.Strict
                && (IsReparsePoint(child) || ShouldPruneDirectory(child.FullName, rules, operation)))
            {
                continue;
            }

            foreach (var descendant in TraversedDirectories(child, rules, operation, _settings.Strict))
            {
                yield return descendant;
            }
        }
    }

    private bool IsSourceFile(FileInfo file, IReadOnlyList<GitIgnoreRule> rules, SearchOperation operation)
    {
        if (!_settings.SourceExtensions.Contains(file.Extension))
        {
            return false;
        }

        if (_settings.Strict)
        {
            VerifyStrictFile(file, operation);
        }
        else if (!file.Exists || !ResolvesWithinRoot(file))
        {
            return false;
        }

        return !IsIgnored(file.FullName, isDirectory: false, rules, operation);
    }

    private bool ShouldPruneDirectory(
        string path,
        IReadOnlyList<GitIgnoreRule> rules,
        SearchOperation operation) =>
        IsIgnored(path, isDirectory: true, rules, operation)
        && !HasDescendantNegation(path, rules, operation);

    private bool IsIgnored(
        string path,
        bool isDirectory,
        IReadOnlyList<GitIgnoreRule> rules,
        SearchOperation operation)
    {
        var relativePath = RelativePath(path);
        if (isDirectory && AlwaysIgnoredDirectories.Contains(Path.GetFileName(path)))
        {
            return true;
        }

        var strictOperation = _settings.Strict ? operation : null;
        strictOperation?.Check();
        var ignored = false;
        foreach (var rule in rules)
        {
            strictOperation?.Check();
            if (rule.Matches(relativePath, isDirectory, strictOperation))
            {
                ignored = !rule.Negated;
            }
        }

        return ignored;
    }

    private bool HasDescendantNegation(
        string path,
        IReadOnlyList<GitIgnoreRule> rules,
        SearchOperation operation)
    {
        var relativePath = RelativePath(path).Trim('/');
        var strictOperation = _settings.Strict ? operation : null;
        foreach (var rule in rules)
        {
            strictOperation?.Check();
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

    private string ResolveProjectFile(
        string rawPath,
        IReadOnlyList<GitIgnoreRule> rules,
        SearchOperation operation)
    {
        var expandedPath = ExpandHome(rawPath);
        if (_settings.Strict)
        {
            VerifyStrictRawParentDirectories(expandedPath, operation);
        }

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
            ThrowFileNotFoundOrFailure(rawPath, operation.Tool);
            throw;
        }

        if (!IsWithinRoot(candidate))
        {
            if (_settings.Strict)
            {
                ThrowFailure(SearchFailure.PreviewPathRefused(operation.Tool));
            }

            throw new ArgumentException($"Path is outside project root: {rawPath}");
        }

        if (_settings.Strict)
        {
            VerifyStrictParentDirectories(candidate, operation);
            var candidateInfo = InspectStrictPath(candidate, expectedDirectory: false, operation);
            if (candidateInfo.Exists)
            {
                if (candidateInfo.IsDirectory)
                {
                    ThrowFailure(SearchFailure.PreviewPathRefused(operation.Tool));
                }

                VerifyStrictFile(new FileInfo(candidate), operation);
                if (IsSourceFile(new FileInfo(candidate), rules, operation))
                {
                    return candidate;
                }

                ThrowFileNotFoundOrFailure(rawPath, operation.Tool);
            }
        }
        else
        {
            if (Directory.Exists(candidate))
            {
                throw new IOException($"Path is not a file: {rawPath}");
            }
            if (File.Exists(candidate))
            {
                var file = new FileInfo(candidate);
                if (IsSourceFile(file, rules, operation))
                {
                    return candidate;
                }

                ThrowFileNotFoundOrFailure(rawPath, operation.Tool);
            }
        }

        if (IsBasenameOnly(rawPath))
        {
            return ResolveUniqueBasename(Path.GetFileName(rawPath), rules, operation);
        }

        ThrowFileNotFoundOrFailure(rawPath, operation.Tool);
        throw new InvalidOperationException("Unreachable");
    }

    private string ResolveUniqueBasename(
        string filename,
        IReadOnlyList<GitIgnoreRule> rules,
        SearchOperation operation)
    {
        var matches = SourceFiles(null, rules, operation)
            .Where(path => string.Equals(Path.GetFileName(path), filename, StringComparison.Ordinal))
            .ToArray();
        return matches.Length switch
        {
            0 => ThrowMissingBasename(filename, operation.Tool),
            1 => matches[0],
            _ => ThrowAmbiguousBasename(filename, operation.Tool),
        };
    }

    private string ThrowMissingBasename(string filename, string tool)
    {
        ThrowFileNotFoundOrFailure(filename, tool);
        throw new InvalidOperationException("Unreachable");
    }

    private string ThrowAmbiguousBasename(string filename, string tool)
    {
        if (_settings.Strict)
        {
            ThrowFailure(SearchFailure.InvalidToolArguments(tool));
        }

        throw new ArgumentException($"Source file basename is ambiguous: {filename}");
    }

    private void ThrowFileNotFoundOrFailure(string rawPath, string tool)
    {
        if (_settings.Strict)
        {
            ThrowFailure(SearchFailure.PreviewSearchUnreadable(tool));
        }

        throw new FileNotFoundException($"Source file not found: {rawPath}");
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

    private void VerifyStrictRawParentDirectories(string rawPath, SearchOperation operation)
    {
        var rawCandidate = Path.IsPathFullyQualified(rawPath)
            ? rawPath
            : Path.Combine(_projectRoot, rawPath);
        if (Path.AltDirectorySeparatorChar != Path.DirectorySeparatorChar)
        {
            rawCandidate = rawCandidate.Replace(Path.AltDirectorySeparatorChar, Path.DirectorySeparatorChar);
        }

        var comparison = OperatingSystem.IsWindows()
            ? StringComparison.OrdinalIgnoreCase
            : StringComparison.Ordinal;
        if (!rawCandidate.StartsWith(_projectRoot, comparison)
            || rawCandidate.Length == _projectRoot.Length
            || rawCandidate[_projectRoot.Length] != Path.DirectorySeparatorChar)
        {
            return;
        }

        var components = rawCandidate[(_projectRoot.Length + 1)..].Split(
            Path.DirectorySeparatorChar,
            StringSplitOptions.RemoveEmptyEntries);
        var rawParent = _projectRoot;
        for (var index = 0; index < components.Length - 1; index++)
        {
            operation.Check();
            rawParent = Path.Combine(rawParent, components[index]);
            string parent;
            try
            {
                parent = Path.GetFullPath(rawParent);
            }
            catch (Exception ex) when (ex is ArgumentException or NotSupportedException or PathTooLongException)
            {
                ThrowFileNotFoundOrFailure(rawPath, operation.Tool);
                throw;
            }

            if (IsWithinRoot(parent))
            {
                VerifyStrictDirectory(new DirectoryInfo(parent), operation);
            }
        }
    }

    private void VerifyStrictParentDirectories(string candidate, SearchOperation operation)
    {
        for (var parent = Path.GetDirectoryName(candidate);
             parent is not null;
             parent = Path.GetDirectoryName(parent))
        {
            if (!IsWithinRoot(parent))
            {
                ThrowFailure(SearchFailure.PreviewPathRefused(operation.Tool));
            }

            VerifyStrictDirectory(new DirectoryInfo(parent), operation);
            if (string.Equals(Path.GetRelativePath(_projectRoot, parent), ".", StringComparison.Ordinal))
            {
                return;
            }
        }

        ThrowFailure(SearchFailure.PreviewPathRefused(operation.Tool));
    }

    private StrictPathInfo InspectStrictPath(string path, bool expectedDirectory, SearchOperation operation)
    {
        try
        {
            operation.Check();
            return _strictPathInspector.Inspect(path, expectedDirectory);
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException)
        {
            ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
            throw;
        }
    }

    private void VerifyStrictDirectory(DirectoryInfo directory, SearchOperation operation)
    {
        var info = InspectStrictPath(directory.FullName, expectedDirectory: true, operation);
        if (!info.Exists || !info.IsDirectory)
        {
            ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
        }
        if (info.IsReparsePoint
            || !IsWithinRoot(directory.FullName)
            || (info.FinalTarget is not null && !IsWithinRoot(info.FinalTarget)))
        {
            ThrowFailure(SearchFailure.PreviewPathRefused(operation.Tool));
        }
    }

    private void VerifyStrictFile(FileInfo file, SearchOperation operation)
    {
        var info = InspectStrictPath(file.FullName, expectedDirectory: false, operation);
        if (!info.Exists)
        {
            ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
        }
        if (info.IsDirectory
            || info.IsReparsePoint
            || !IsWithinRoot(file.FullName)
            || (info.FinalTarget is not null && !IsWithinRoot(info.FinalTarget)))
        {
            ThrowFailure(SearchFailure.PreviewPathRefused(operation.Tool));
        }
    }

    private static SearchPattern CreateSymbolPattern(string kind, string name, Func<Regex, string, bool>? strictRegexMatch)
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
        return new SearchPattern(pattern, strictRegexMatch);
    }

    private static SearchPattern CreateReferencePattern(string name, Func<Regex, string, bool>? strictRegexMatch)
    {
        var escaped = Regex.Escape(name);
        var pattern = Regex.IsMatch(name, "^[A-Za-z_][A-Za-z0-9_]*$", RegexOptions.CultureInvariant)
            ? $@"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])"
            : escaped;
        return new SearchPattern(pattern, strictRegexMatch);
    }

    private string[] ReadLines(string path, SearchOperation operation)
    {
        var bytes = ReadBytes(path, operation);
        var strictOperation = _settings.Strict ? operation : null;
        var text = new UTF8Encoding(
            encoderShouldEmitUTF8Identifier: false,
            throwOnInvalidBytes: false).GetString(bytes);
        strictOperation?.Check();
        var lines = new List<string>();
        var start = 0;
        for (var index = 0; index < text.Length; index++)
        {
            if ((index & 0x3fff) == 0)
            {
                strictOperation?.Check();
            }

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

    private byte[] ReadBytes(string path, SearchOperation operation)
    {
        if (!_settings.Strict)
        {
            return File.ReadAllBytes(path);
        }

        var file = new FileInfo(path);
        VerifyStrictFile(file, operation);
        operation.Check();
        try
        {
            using var stream = _openRead(path);
            operation.Check();
            var length = stream.Length;
            operation.ReserveOpenedBytes(length);
            var bytes = new byte[checked((int)length)];
            var offset = 0;
            while (offset < bytes.Length)
            {
                operation.Check();
                var read = stream.Read(bytes, offset, bytes.Length - offset);
                operation.Check();
                if (read == 0)
                {
                    ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
                }

                offset += read;
            }
            operation.Check();
            if (stream.ReadByte() != -1)
            {
                ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
            }
            operation.Check();

            return bytes;
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or OverflowException)
        {
            ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
            throw;
        }
    }

    private IReadOnlyList<DirectoryInfo> EnumerateDirectories(
        DirectoryInfo directory,
        IReadOnlyList<GitIgnoreRule> rules,
        SearchOperation operation)
    {
        if (!_settings.Strict)
        {
            try
            {
                return _enumerateDirectories(directory).OrderBy(static entry => entry.Name, StringComparer.Ordinal).ToArray();
            }
            catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
            {
                return [];
            }
        }

        try
        {
            var directories = new List<DirectoryInfo>();
            foreach (var child in _enumerateDirectories(directory))
            {
                operation.Check();
                if (_settings.ExcludedDirectoryNames.Contains(child.Name))
                {
                    continue;
                }

                VerifyStrictDirectory(child, operation);
                if (ShouldPruneDirectory(child.FullName, rules, operation))
                {
                    continue;
                }

                operation.EnterDirectory();
                directories.Add(child);
            }

            directories.Sort(static (left, right) => StringComparer.Ordinal.Compare(left.Name, right.Name));
            return directories;
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
        {
            ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
            throw;
        }
    }

    private IReadOnlyList<FileInfo> EnumerateFiles(DirectoryInfo directory, SearchOperation operation)
    {
        if (!_settings.Strict)
        {
            try
            {
                return _enumerateFiles(directory).OrderBy(static entry => entry.Name, StringComparer.Ordinal).ToArray();
            }
            catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
            {
                return [];
            }
        }

        try
        {
            var files = new List<FileInfo>();
            foreach (var file in _enumerateFiles(directory))
            {
                operation.ExamineNonDirectoryEntry();
                files.Add(file);
            }

            files.Sort(static (left, right) => StringComparer.Ordinal.Compare(left.Name, right.Name));
            return files;
        }
        catch (Exception ex) when (ex is UnauthorizedAccessException or IOException)
        {
            ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
            throw;
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

    private IReadOnlyList<GitIgnoreRule> LoadGitIgnoreRules(SearchOperation operation)
    {
        var gitIgnore = Path.Combine(_projectRoot, ".gitignore");
        if (!_settings.Strict)
        {
            if (!File.Exists(gitIgnore))
            {
                return [];
            }

            return ReadLines(gitIgnore, operation)
                .Select(GitIgnoreRule.Parse)
                .Where(static rule => rule is not null)
                .Select(static rule => rule!)
                .ToArray();
        }

        if (!InspectStrictPath(gitIgnore, expectedDirectory: false, operation).Exists)
        {
            return [];
        }

        VerifyStrictFile(new FileInfo(gitIgnore), operation);
        try
        {
            var rules = new List<GitIgnoreRule>();
            foreach (var line in ReadLines(gitIgnore, operation))
            {
                operation.Check();
                var rule = GitIgnoreRule.Parse(line);
                operation.Check();
                if (rule is null)
                {
                    continue;
                }

                rule.Validate(operation);
                operation.Check();
                rules.Add(rule);
            }

            return rules;
        }
        catch (ArgumentException)
        {
            ThrowFailure(SearchFailure.PreviewSearchUnreadable(operation.Tool));
            throw;
        }
    }

    private static bool GlobMatches(string value, string pattern, SearchOperation? operation = null)
    {
        operation?.Check();
        try
        {
            var matches = CreateGlobRegex(pattern, operation).IsMatch(value);
            operation?.Check();
            return matches;
        }
        catch (RegexMatchTimeoutException) when (operation is not null)
        {
            ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(operation.Tool));
            throw;
        }
    }

    private static Regex CreateGlobRegex(string pattern, SearchOperation? operation = null)
    {
        var expression = new StringBuilder("^");
        for (var index = 0; index < pattern.Length; index++)
        {
            if ((index & 0x3fff) == 0)
            {
                operation?.Check();
            }

            switch (pattern[index])
            {
                case '*':
                    expression.Append(".*");
                    break;
                case '?':
                    expression.Append('.');
                    break;
                case '[':
                    var closing = FindClosingBracket(pattern, index + 1, operation);
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

        operation?.Check();
        expression.Append('$');
        var source = expression.ToString();
        var regex = operation is null
            ? new Regex(source, RegexOptions.CultureInvariant)
            : new Regex(source, RegexOptions.CultureInvariant, operation.GetMatchTimeout());
        operation?.Check();
        return regex;
    }

    private static int FindClosingBracket(string pattern, int startIndex, SearchOperation? operation)
    {
        for (var index = startIndex; index < pattern.Length; index++)
        {
            if ((index & 0x3fff) == 0)
            {
                operation?.Check();
            }
            if (pattern[index] == ']')
            {
                return index;
            }
        }

        return -1;
    }

    private string FormatContext(string text, bool trim = true)
    {
        var value = trim ? text.Trim() : text;
        return _settings.MaximumContextScalars is int maximumContextScalars
            ? TruncateToScalars(value, maximumContextScalars)
            : value;
    }

    private static int CountScalars(string value)
    {
        var count = 0;
        foreach (var _ in value.EnumerateRunes())
        {
            count++;
        }

        return count;
    }

    private static string TruncateToScalars(string value, int maximumScalars)
    {
        var index = 0;
        var count = 0;
        foreach (var rune in value.EnumerateRunes())
        {
            if (count == maximumScalars)
            {
                return value[..index];
            }

            index += rune.Utf16SequenceLength;
            count++;
        }

        return value;
    }

    private sealed class SearchPattern
    {
        private static readonly TimeSpan StrictMatchSlice = TimeSpan.FromMilliseconds(100);

        private readonly string _pattern;
        private readonly Regex _legacyRegex;
        private readonly Func<Regex, string, bool>? _strictRegexMatch;
        private Regex? _strictRegex;
        private TimeSpan _strictTimeout;

        internal SearchPattern(string pattern, Func<Regex, string, bool>? strictRegexMatch)
        {
            _pattern = pattern;
            _legacyRegex = new Regex(pattern, RegexOptions.CultureInvariant);
            _strictRegexMatch = strictRegexMatch;
        }

        internal bool IsMatch(string value, SearchOperation? operation)
        {
            if (operation is null)
            {
                return _legacyRegex.IsMatch(value);
            }

            var timeout = operation.GetMatchTimeout();
            if (timeout > StrictMatchSlice)
            {
                timeout = StrictMatchSlice;
            }
            if (_strictRegex is null || _strictTimeout > timeout)
            {
                _strictRegex = new Regex(_pattern, RegexOptions.CultureInvariant, timeout);
                _strictTimeout = timeout;
            }

            operation.Check();
            try
            {
                var matched = _strictRegexMatch is null
                    ? _strictRegex.IsMatch(value)
                    : _strictRegexMatch(_strictRegex, value);
                operation.Check();
                return matched;
            }
            catch (RegexMatchTimeoutException)
            {
                operation.Check();
                ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(operation.Tool));
                throw;
            }
        }
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

        internal void Validate(SearchOperation operation)
        {
            for (var index = 0; index < Pattern.Length; index++)
            {
                if (Pattern[index] != '[')
                {
                    continue;
                }

                var closing = FindClosingBracket(Pattern, index + 1, operation);
                if (closing == index + 1)
                {
                    throw new ArgumentException("Empty glob character class.");
                }
                if (closing > index + 1)
                {
                    index = closing;
                }
            }

            CreateGlobRegex(Pattern, operation);
        }

        internal bool Matches(string relativePath, bool isDirectory, SearchOperation? operation)
        {
            operation?.Check();
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

                return path.Split('/').SkipLast(1).Any(part => GlobMatches(part, Pattern, operation));
            }

            if (Anchored || HasSlash)
            {
                return GlobMatches(path, Pattern, operation);
            }
            if (DirectoryOnly)
            {
                return path.Split('/').Any(part => GlobMatches(part, Pattern, operation));
            }

            return GlobMatches(Path.GetFileName(path), Pattern, operation);
        }
    }

    private sealed class SearchOperation
    {
        private readonly SearchPolicySettings _settings;
        private readonly CancellationToken _cancellationToken;
        private readonly Func<long>? _timestamp;
        private readonly long? _deadlineTimestamp;
        private int _directoriesEntered;
        private int _nonDirectoryEntriesExamined;
        private long _openedBytes;

        internal SearchOperation(
            SearchPolicySettings settings,
            string tool,
            CancellationToken cancellationToken,
            Func<long>? timestamp)
        {
            _settings = settings;
            Tool = tool;
            _cancellationToken = cancellationToken;
            _timestamp = timestamp;
            if (settings.Deadline is TimeSpan deadline)
            {
                _deadlineTimestamp = GetTimestamp() + (long)(deadline.TotalSeconds * Stopwatch.Frequency);
            }
        }

        internal string Tool { get; }

        internal void Check()
        {
            _cancellationToken.ThrowIfCancellationRequested();
            if (_deadlineTimestamp is long deadline && GetTimestamp() >= deadline)
            {
                ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(Tool));
            }
        }

        private long GetTimestamp() => _timestamp?.Invoke() ?? Stopwatch.GetTimestamp();

        internal TimeSpan GetMatchTimeout()
        {
            Check();
            if (_deadlineTimestamp is not long deadline)
            {
                return Regex.InfiniteMatchTimeout;
            }

            var remaining = deadline - GetTimestamp();
            if (remaining <= 0)
            {
                ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(Tool));
            }

            return TimeSpan.FromSeconds((double)remaining / Stopwatch.Frequency);
        }

        internal void EnterDirectory()
        {
            Check();
            if (_settings.MaximumDirectories is int maximumDirectories && ++_directoriesEntered > maximumDirectories)
            {
                ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(Tool));
            }
        }

        internal void ExamineNonDirectoryEntry()
        {
            Check();
            if (_settings.MaximumNonDirectoryEntries is int maximumEntries && ++_nonDirectoryEntriesExamined > maximumEntries)
            {
                ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(Tool));
            }
        }

        internal void ReserveOpenedBytes(long bytes)
        {
            Check();
            if (bytes < 0
                || (_settings.MaximumOpenedFileBytes is long maximumFileBytes && bytes > maximumFileBytes)
                || (_settings.MaximumAggregateOpenedBytes is long maximumAggregateBytes && _openedBytes > maximumAggregateBytes - bytes))
            {
                ThrowFailure(SearchFailure.PreviewSearchBudgetExceeded(Tool));
            }

            _openedBytes += bytes;
        }
    }
}

/// <summary>A root-relative C# declaration match.</summary>
public sealed record SymbolMatch(string File, int Line, string Name, string Kind, string Context);

/// <summary>A root-relative textual reference match.</summary>
public sealed record ReferenceMatch(string File, int Line, string Context);

/// <summary>Source context with positive line numbers and root-relative path.</summary>
public sealed record SourceContext(string File, int StartLine, int EndLine, IReadOnlyList<SourceLine> Lines);

/// <summary>One source-context line.</summary>
public sealed record SourceLine(int Line, string Text);

internal sealed class SymbolMatchComparer : IComparer<SymbolMatch>
{
    internal static SymbolMatchComparer Instance { get; } = new();

    public int Compare(SymbolMatch? left, SymbolMatch? right)
    {
        if (ReferenceEquals(left, right))
        {
            return 0;
        }
        if (left is null)
        {
            return -1;
        }
        if (right is null)
        {
            return 1;
        }

        var file = StringComparer.Ordinal.Compare(left.File, right.File);
        if (file != 0)
        {
            return file;
        }

        var line = left.Line.CompareTo(right.Line);
        return line != 0 ? line : StringComparer.Ordinal.Compare(left.Kind, right.Kind);
    }
}

internal sealed class ReferenceMatchComparer : IComparer<ReferenceMatch>
{
    internal static ReferenceMatchComparer Instance { get; } = new();

    public int Compare(ReferenceMatch? left, ReferenceMatch? right)
    {
        if (ReferenceEquals(left, right))
        {
            return 0;
        }
        if (left is null)
        {
            return -1;
        }
        if (right is null)
        {
            return 1;
        }

        var file = StringComparer.Ordinal.Compare(left.File, right.File);
        return file != 0 ? file : left.Line.CompareTo(right.Line);
    }
}
