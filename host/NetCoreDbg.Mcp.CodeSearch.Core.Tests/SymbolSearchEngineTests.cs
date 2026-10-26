using NetCoreDbg.Mcp.CodeSearch.Core;
using Xunit;

namespace NetCoreDbg.Mcp.CodeSearch.Core.Tests;

public sealed class SymbolSearchEngineTests
{
    [Fact]
    public void LegacyPolicyPreservesIgnoreOrderExtensionsAndArgumentErrors()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", "ignored/\n");
        root.Write("A/First.cs", "public sealed class LegacyMarker { }\n");
        root.Write("Z/Last.cs", "public sealed class LegacyMarker { }\n");
        root.Write("ignored/Hidden.cs", "public sealed class LegacyMarker { }\n");
        root.Write("Views/View.xaml", "<LegacyMarker />\n");
        var engine = new SymbolSearchEngine(root.Path, LegacySearchPolicy.Instance);

        var symbols = engine.FindCodeSymbol("LegacyMarker", "class");
        var context = engine.GetSourceContext("View.xaml", 1, 0);
        var argument = Assert.Throws<ArgumentException>(() => engine.FindCodeSymbol(" "));

        Assert.Equal(["A/First.cs", "Z/Last.cs"], symbols.Select(static match => match.File));
        Assert.Equal("Views/View.xaml", context.File);
        Assert.Contains("Symbol name must not be empty", argument.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void BothPoliciesPreserveUnicodeLineSeparatorsBomAndExactResultLineNumbers(bool preview)
    {
        using var root = TestRoot.Create();
        root.Write("Lines.cs", "\uFEFF// source BOM\n  public sealed class LineMarker { }  \r\v LineMarker second; \f"
            + "gap\u001cLineMarker third;\u001dgap\u001eLineMarker fourth;\u0085"
            + "gap\u2028LineMarker fifth;\u2029last\r\nLineMarker sixth;");
        SearchPolicy policy = preview ? PreviewSearchPolicy.Instance : LegacySearchPolicy.Instance;
        var engine = new SymbolSearchEngine(root.Path, policy);

        var context = engine.GetSourceContext("Lines.cs", 6, 6);
        var symbol = Assert.Single(engine.FindCodeSymbol("LineMarker", "class"));
        var references = engine.FindCodeReferences("LineMarker", 100);

        Assert.Equal(1, context.StartLine);
        Assert.Equal(12, context.EndLine);
        Assert.Equal(Enumerable.Range(1, 12), context.Lines.Select(static line => line.Line));
        Assert.Equal(
            ["\uFEFF// source BOM", "  public sealed class LineMarker { }  ", "", " LineMarker second; ",
                "gap", "LineMarker third;", "gap", "LineMarker fourth;", "gap", "LineMarker fifth;", "last", "LineMarker sixth;"],
            context.Lines.Select(static line => line.Text));
        Assert.Equal(2, symbol.Line);
        Assert.Equal("public sealed class LineMarker { }", symbol.Context);
        Assert.Equal([2, 4, 6, 8, 10, 12], references.Select(static match => match.Line));
        Assert.Equal(
            ["public sealed class LineMarker { }", "LineMarker second;", "LineMarker third;",
                "LineMarker fourth;", "LineMarker fifth;", "LineMarker sixth;"],
            references.Select(static match => match.Context));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void BothPoliciesPreserveEmptyAndTrailingSourceLinesWithoutPhantomLine(bool preview)
    {
        using var root = TestRoot.Create();
        SearchPolicy policy = preview ? PreviewSearchPolicy.Instance : LegacySearchPolicy.Instance;
        var engine = new SymbolSearchEngine(root.Path, policy);
        (string Text, string[] Lines)[] cases =
        [
            ("", []),
            ("\n", [""]),
            ("\r\n", [""]),
            ("\n\n", ["", ""]),
            ("a", ["a"]),
            ("a\n", ["a"]),
            ("a\r\n", ["a"]),
            ("a\n\n", ["a", ""]),
        ];

        foreach (var (text, lines) in cases)
        {
            root.Write("Lines.cs", text);
            if (lines.Length > 0)
            {
                var context = engine.GetSourceContext("Lines.cs", 1, 10);

                Assert.Equal(1, context.StartLine);
                Assert.Equal(lines.Length, context.EndLine);
                Assert.Equal(Enumerable.Range(1, lines.Length), context.Lines.Select(static line => line.Line));
                Assert.Equal(lines, context.Lines.Select(static line => line.Text));
            }

            if (preview)
            {
                var failure = Assert.Throws<SearchFailureException>(
                    () => engine.GetSourceContext("Lines.cs", lines.Length + 1, 0));

                Assert.Equal(
                    new SearchFailure("invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", "get_source_context"),
                    failure.Failure);
            }
            else
            {
                var failure = Assert.Throws<ArgumentException>(
                    () => engine.GetSourceContext("Lines.cs", lines.Length + 1, 0));

                Assert.Equal($"line {lines.Length + 1} is outside file range 1..{lines.Length}", failure.Message);
            }
        }
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void BothPoliciesPreserveGitIgnoreSeparatorsAndPolicySpecificLeadingBom(bool preview)
    {
        using var root = TestRoot.Create();
        string[] separators = ["\n", "\r", "\v", "\f", "\u001c", "\u001d", "\u001e", "\u0085", "\u2028", "\u2029"];
        root.Write(".gitignore", "\uFEFF" + string.Concat(separators.Select(
            static (separator, index) => $"Ignored{index}.cs{separator}")));
        root.Write("Allowed.cs", "public sealed class IgnoreLineMarker { }\n");
        for (var index = 0; index < separators.Length; index++)
        {
            root.Write($"Ignored{index}.cs", "public sealed class IgnoreLineMarker { }\n");
        }
        SearchPolicy policy = preview ? PreviewSearchPolicy.Instance : LegacySearchPolicy.Instance;
        var engine = new SymbolSearchEngine(root.Path, policy);

        var symbols = engine.FindCodeSymbol("IgnoreLineMarker", "class");

        string[] expectedFiles = preview ? ["Allowed.cs"] : ["Allowed.cs", "Ignored0.cs"];
        Assert.Equal(expectedFiles, symbols.Select(static match => match.File).OrderBy(static file => file, StringComparer.Ordinal));
    }

    [Fact]
    public void LegacyPolicyClampsSourceContextRadiusWithoutIntegerOverflow()
    {
        using var root = TestRoot.Create();
        root.Write("Source.cs", "first\nsecond\n");
        var engine = new SymbolSearchEngine(root.Path, LegacySearchPolicy.Instance);

        var context = engine.GetSourceContext("Source.cs", line: 1, radius: int.MaxValue);

        Assert.Equal(1, context.StartLine);
        Assert.Equal(2, context.EndLine);
        Assert.Equal([1, 2], context.Lines.Select(static line => line.Line));
    }

    [Fact]
    public void PreviewPolicyUsesCsOnlyDeterministicResultsAndClosedArgumentFailure()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", "ignored/\n");
        root.Write("Z/Last.cs", "public sealed class PreviewMarker { }\n");
        root.Write("A/First.cs", "public sealed class PreviewMarker { }\n");
        root.Write("ignored/Hidden.cs", "public sealed class PreviewMarker { }\n");
        root.Write("Views/View.xaml", "public sealed class PreviewMarker { }\n");
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var symbols = engine.FindCodeSymbol("PreviewMarker", "class");
        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol(" "));

        Assert.Equal(["A/First.cs", "Z/Last.cs"], symbols.Select(static match => match.File));
        Assert.Equal(
            new SearchFailure("invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", "find_code_symbol"),
            failure.Failure);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void PreviewPolicyAlwaysPrunesVcsMetadataDespiteDescendantNegation()
    {
        using var root = TestRoot.Create();
        const string marker = "VcsMetadataMarker";
        root.Write(".gitignore", "!keep.cs\n");
        root.Write(".git/keep.cs", $"public sealed class {marker} {{ }}\n");
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var symbols = engine.FindCodeSymbol(marker, "class");

        Assert.Empty(symbols);
    }

    [Fact]
    public void LegacyPolicyRetainsVcsDescendantNegationBehavior()
    {
        using var root = TestRoot.Create();
        const string marker = "LegacyVcsMetadataMarker";
        root.Write(".gitignore", "!keep.cs\n");
        root.Write(".git/keep.cs", $"public sealed class {marker} {{ }}\n");
        var engine = new SymbolSearchEngine(root.Path, LegacySearchPolicy.Instance);

        var symbols = engine.FindCodeSymbol(marker, "class");

        Assert.Equal([".git/keep.cs"], symbols.Select(static match => match.File));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void BothPoliciesPreserveExplicitDescendantNegationWithoutReturningIgnoredSibling(bool preview)
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", "private/\n!private/Allowed.cs\n");
        root.Write("private/Allowed.cs", "public sealed class NegationMarker { }\n");
        root.Write("private/Secret.cs", "public sealed class NegationMarker { }\n");
        SearchPolicy policy = preview ? PreviewSearchPolicy.Instance : LegacySearchPolicy.Instance;
        var engine = new SymbolSearchEngine(root.Path, policy);

        var symbols = engine.FindCodeSymbol("NegationMarker", "class");

        Assert.Equal(["private/Allowed.cs"], symbols.Select(static match => match.File));
    }

    [Fact]
    public void PreviewPolicyPrunesCaseVariantVcsMetadata()
    {
        using var root = TestRoot.Create();
        const string marker = "CaseVariantVcsMetadataMarker";
        root.Write(".gitignore", "!keep.cs\n");
        root.Write(".GIT/keep.cs", $"public sealed class {marker} {{ }}\n");
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var symbols = engine.FindCodeSymbol(marker, "class");

        Assert.Empty(symbols);
    }

    [Fact]
    public void PreviewPolicyExcludesBinAndObjWhileLegacyKeepsBuildOutput()
    {
        using var root = TestRoot.Create();
        const string marker = "BuildOutputMarker";
        root.Write("Root.cs", $"public sealed class {marker} {{ }}\n");
        root.Write("bin/Debug/net8.0/Generated.cs", $"public sealed class {marker} {{ }}\n");
        root.Write("obj/Debug/net8.0/Generated.cs", $"public sealed class {marker} {{ }}\n");

        var preview = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance)
            .FindCodeSymbol(marker, "class");
        var legacy = new SymbolSearchEngine(root.Path, LegacySearchPolicy.Instance)
            .FindCodeSymbol(marker, "class");

        Assert.Equal(["Root.cs"], preview.Select(static match => match.File));
        Assert.Contains("bin/Debug/net8.0/Generated.cs", legacy.Select(static match => match.File));
        Assert.Contains("obj/Debug/net8.0/Generated.cs", legacy.Select(static match => match.File));
    }

    [Theory]
    [InlineData("bin/Generated.cs", false)]
    [InlineData("bin/Generated.cs", true)]
    [InlineData("obj/Generated.cs", false)]
    [InlineData("obj/Generated.cs", true)]
    [InlineData(".git/Generated.cs", false)]
    [InlineData(".git/Generated.cs", true)]
    [InlineData(".hg/Generated.cs", false)]
    [InlineData(".hg/Generated.cs", true)]
    [InlineData(".svn/Generated.cs", false)]
    [InlineData(".svn/Generated.cs", true)]
    [InlineData("src/bin/Debug/Generated.cs", false)]
    [InlineData("src/bin/Debug/Generated.cs", true)]
    [InlineData("src/obj/Debug/Generated.cs", false)]
    [InlineData("src/obj/Debug/Generated.cs", true)]
    [InlineData("src/.git/metadata/Generated.cs", false)]
    [InlineData("src/.git/metadata/Generated.cs", true)]
    [InlineData("src/.hg/metadata/Generated.cs", false)]
    [InlineData("src/.hg/metadata/Generated.cs", true)]
    [InlineData("src/.svn/metadata/Generated.cs", false)]
    [InlineData("src/.svn/metadata/Generated.cs", true)]
    public void PreviewPolicyRejectsDirectContextInExcludedDirectoriesBeforeOpeningContent(
        string relativePath,
        bool absolute)
    {
        using var root = TestRoot.Create();
        root.Write(relativePath, "public sealed class ExcludedContextMarker { }\n");
        var source = Path.GetFullPath(Path.Combine(root.Path, relativePath));
        var openedPaths = new List<string>();
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            new TestStrictPathInspector(),
            timestamp: static () => 0,
            openRead: path =>
            {
                openedPaths.Add(path);
                return File.OpenRead(path);
            });
        Assert.True(File.Exists(source));
        Assert.False(File.Exists(Path.Combine(root.Path, ".gitignore")));

        var exception = Record.Exception(
            () => engine.GetSourceContext(absolute ? source : relativePath, 1, 0));

        Assert.Empty(openedPaths);
        var failure = Assert.IsType<SearchFailureException>(exception);
        Assert.Equal(
            new SearchFailure("preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", "get_source_context"),
            failure.Failure);
        Assert.DoesNotContain(root.Path, failure.ToString(), StringComparison.Ordinal);
    }

    [Theory]
    [InlineData("src/Allowed.cs", false)]
    [InlineData("src/Allowed.cs", true)]
    [InlineData("src/binocular/objects/.github/Allowed.cs", false)]
    [InlineData("src/binocular/objects/.github/Allowed.cs", true)]
    public void PreviewPolicyReadsDirectContextOutsideExcludedDirectories(string relativePath, bool absolute)
    {
        using var root = TestRoot.Create();
        const string text = "  public sealed class AllowedContextMarker { }";
        root.Write(relativePath, text + "\n");
        var source = Path.GetFullPath(Path.Combine(root.Path, relativePath));
        var openedPaths = new List<string>();
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            new TestStrictPathInspector(),
            timestamp: static () => 0,
            openRead: path =>
            {
                openedPaths.Add(path);
                return File.OpenRead(path);
            });

        var context = engine.GetSourceContext(absolute ? source : relativePath, 1, 0);

        Assert.Equal([source], openedPaths);
        Assert.Equal(relativePath, context.File);
        Assert.Equal(1, context.StartLine);
        Assert.Equal(1, context.EndLine);
        var line = Assert.Single(context.Lines);
        Assert.Equal(1, line.Line);
        Assert.Equal(text, line.Text);
        Assert.DoesNotContain(root.Path, context.File, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void PreviewPolicyAbsoluteContextRequiresPhysicalRootIdentity(bool distinctPhysicalSibling)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        var parent = Path.GetFullPath("physical-root-admission");
        var projectRoot = Path.Combine(parent, "Project");
        var requestedRoot = Path.Combine(parent, "project");
        var physicalRoot = distinctPhysicalSibling ? requestedRoot : projectRoot;
        var canonicalSource = Path.Combine(projectRoot, "Contained.cs");
        var requestedSource = Path.Combine(requestedRoot, "Contained.cs");
        var physicalSource = Path.Combine(physicalRoot, "Contained.cs");
        // Metadata models distinct physical directories or one directory's case alias; no disk is consulted.
        var inspector = new MappedPhysicalPathInspector(new Dictionary<string, (string CanonicalPath, bool IsDirectory)>(StringComparer.Ordinal)
        {
            [projectRoot] = (projectRoot, true),
            [requestedRoot] = (physicalRoot, true),
            [canonicalSource] = (canonicalSource, false),
            [requestedSource] = (physicalSource, false),
        });
        var openedPaths = new List<string>();
        var engine = new SymbolSearchEngine(projectRoot, PreviewSearchPolicy.Instance, inspector,
            timestamp: static () => 0,
            openRead: path =>
            {
                openedPaths.Add(path);
                return new MemoryStream("physical-root-source-control\n"u8.ToArray());
            });
        SourceContext? context = null;

        var exception = Record.Exception(() => context = engine.GetSourceContext(requestedSource, 1, 0));

        if (distinctPhysicalSibling)
        {
            Assert.Equal(
                (new SearchFailure("preview_path_refused", "PREVIEW_PATH_REFUSED", "get_source_context"), 0),
                ((exception as SearchFailureException)?.Failure, openedPaths.Count));
            return;
        }

        Assert.Null(exception);
        Assert.NotNull(context);
        Assert.Equal([canonicalSource], openedPaths);
        Assert.Equal("Contained.cs", context.File);
        Assert.Equal(1, context.StartLine);
        Assert.Equal(1, context.EndLine);
        Assert.Equal("physical-root-source-control", Assert.Single(context.Lines).Text);
    }

    [Fact]
    public void PreviewPolicyReadsContextWithProjectRootCaseAlias()
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        using var root = TestRoot.Create();
        root.Write("Contained.cs", "project-root-case-alias-control\n");
        var engine = new SymbolSearchEngine(root.Path.ToUpperInvariant(), PreviewSearchPolicy.Instance);

        var context = engine.GetSourceContext(Path.Combine(root.Path, "CONTAINED.CS"), 1, 0);

        Assert.Equal("Contained.cs", context.File);
        Assert.Equal("project-root-case-alias-control", Assert.Single(context.Lines).Text);
    }

    [Theory]
    [InlineData("IGNORED/Secret.cs", false)]
    [InlineData("IGNORED/Secret.cs", true)]
    [InlineData("private/SECRET.CS", false)]
    [InlineData("private/SECRET.CS", true)]
    [InlineData("PRIVATE/secret.cs", false)]
    [InlineData("PRIVATE/secret.cs", true)]
    public void PreviewPolicyRejectsPhysicalCaseAliasesOfIgnoredContextBeforeOpeningSource(string relativePath, bool absolute)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        using var root = TestRoot.Create();
        root.Write(".gitignore", "ignored/\n!ignored/Allowed.cs\nprivate/Secret.cs\n");
        root.Write("ignored/Secret.cs", "private-source-sentinel\n");
        root.Write("private/Secret.cs", "private-source-sentinel\n");
        root.Write("ignored/Allowed.cs", "  allowed-source-control\n");
        var source = Path.GetFullPath(Path.Combine(root.Path, relativePath));
        if (!File.Exists(source))
        {
            return;
        }

        var openedSources = new List<string>();
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance,
            new TestStrictPathInspector(), timestamp: static () => 0,
            openRead: path =>
            {
                if (string.Equals(Path.GetExtension(path), ".cs", StringComparison.OrdinalIgnoreCase))
                {
                    openedSources.Add(path);
                }
                return File.OpenRead(path);
            });

        var exception = Record.Exception(() => engine.GetSourceContext(absolute ? source : relativePath, 1, 0));

        Assert.Empty(openedSources);
        var failure = Assert.IsType<SearchFailureException>(exception);
        Assert.Equal(new SearchFailure("preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", "get_source_context"), failure.Failure);
        Assert.DoesNotContain(root.Path, failure.ToString(), StringComparison.Ordinal);
        Assert.DoesNotContain("private-source-sentinel", failure.ToString(), StringComparison.Ordinal);

        const string allowedAlias = "IGNORED/ALLOWED.CS";
        var allowed = engine.GetSourceContext(absolute ? Path.Combine(root.Path, allowedAlias) : allowedAlias, 1, 0);
        Assert.Single(openedSources);
        Assert.Equal("ignored/Allowed.cs", allowed.File);
        Assert.Equal("  allowed-source-control", Assert.Single(allowed.Lines).Text);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void LegacyPolicyRetainsDirectContextCaseAliasIgnoreMatching(bool absolute)
    {
        if (!OperatingSystem.IsWindows())
        {
            return;
        }

        using var root = TestRoot.Create();
        root.Write(".gitignore", "ignored/\n");
        root.Write("ignored/Secret.cs", "legacy-case-alias-control\n");
        const string relativePath = "IGNORED/Secret.cs";
        var source = Path.Combine(root.Path, relativePath);
        if (!File.Exists(source))
        {
            return;
        }

        var context = new SymbolSearchEngine(root.Path, LegacySearchPolicy.Instance)
            .GetSourceContext(absolute ? source : relativePath, 1, 0);

        Assert.Equal(relativePath, context.File);
        Assert.Equal("legacy-case-alias-control", Assert.Single(context.Lines).Text);
    }

    [Theory]
    [InlineData(false)]
    [InlineData(true)]
    public void BothPoliciesPreserveDistinctCaseSensitiveIgnorePathsWhenFilesystemSupportsThem(bool preview)
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", "ignored/\n");
        root.Write("ignored/Secret.cs", "ignored-lowercase-source\n");
        if (File.Exists(Path.Combine(root.Path, "IGNORED/Secret.cs")))
        {
            return;
        }
        root.Write("IGNORED/Secret.cs", "allowed-uppercase-source\n");
        SearchPolicy policy = preview ? PreviewSearchPolicy.Instance : LegacySearchPolicy.Instance;
        var engine = new SymbolSearchEngine(root.Path, policy);

        var context = engine.GetSourceContext("IGNORED/Secret.cs", 1, 0);

        Assert.Equal("IGNORED/Secret.cs", context.File);
        Assert.Equal("allowed-uppercase-source", Assert.Single(context.Lines).Text);
        if (preview)
        {
            var failure = Assert.Throws<SearchFailureException>(() => engine.GetSourceContext("ignored/Secret.cs", 1, 0));
            Assert.Equal(new SearchFailure("preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", "get_source_context"), failure.Failure);
        }
        else
        {
            Assert.Throws<FileNotFoundException>(() => engine.GetSourceContext("ignored/Secret.cs", 1, 0));
        }
    }

    [Theory]
    [InlineData("bin/Generated.cs", false)]
    [InlineData("bin/Generated.cs", true)]
    [InlineData("obj/Generated.cs", false)]
    [InlineData("obj/Generated.cs", true)]
    [InlineData("src/bin/Debug/Generated.cs", false)]
    [InlineData("src/bin/Debug/Generated.cs", true)]
    [InlineData("src/obj/Debug/Generated.cs", false)]
    [InlineData("src/obj/Debug/Generated.cs", true)]
    public void LegacyPolicyKeepsDirectContextInBuildOutputDirectories(string relativePath, bool absolute)
    {
        using var root = TestRoot.Create();
        const string text = "public sealed class LegacyBuildContextMarker { }";
        root.Write(relativePath, text + "\n");
        var source = Path.GetFullPath(Path.Combine(root.Path, relativePath));
        var engine = new SymbolSearchEngine(root.Path, LegacySearchPolicy.Instance);

        var context = engine.GetSourceContext(absolute ? source : relativePath, 1, 0);

        Assert.Equal(relativePath, context.File);
        Assert.Equal(1, context.StartLine);
        Assert.Equal(1, context.EndLine);
        var line = Assert.Single(context.Lines);
        Assert.Equal(1, line.Line);
        Assert.Equal(text, line.Text);
    }

    [Fact]
    public void PreviewPolicySkipsExcludedReparseDirectoriesBeforeStrictInspection()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", string.Empty);
        var excludedBin = new DirectoryInfo(Path.Combine(root.Path, "bin"));
        var inspector = new TestStrictPathInspector(new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal)
        {
            [excludedBin.FullName] = new StrictPathInfo(Exists: true, IsDirectory: true, IsReparsePoint: true, FinalTarget: null),
        });
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            inspector,
            enumerateDirectories: directory => string.Equals(directory.FullName, root.Path, StringComparison.Ordinal) ? [excludedBin] : [],
            enumerateFiles: static _ => []);

        var symbols = engine.FindCodeSymbol("Missing", "class");

        Assert.Empty(symbols);
        Assert.DoesNotContain(excludedBin.FullName, inspector.InspectedPaths);
    }

    [Fact]
    public void PreviewPolicyExcludedDirectoriesDoNotConsumeStrictTraversalBudget()
    {
        const int excludedDirectoryCount = 2048;
        using var root = TestRoot.Create();
        root.Write(".gitignore", string.Empty);
        var excludedDirectories = Enumerable.Range(0, excludedDirectoryCount)
            .Select(index => new DirectoryInfo(Path.Combine(root.Path, $"Parent{index:D4}", "bin")))
            .ToArray();
        var inspector = new TestStrictPathInspector(excludedDirectories.ToDictionary(
            static directory => directory.FullName,
            static _ => new StrictPathInfo(Exists: true, IsDirectory: true, IsReparsePoint: false, FinalTarget: null),
            StringComparer.Ordinal));
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            inspector,
            enumerateDirectories: directory => string.Equals(directory.FullName, root.Path, StringComparison.Ordinal) ? excludedDirectories : [],
            enumerateFiles: static _ => []);

        var symbols = engine.FindCodeSymbol("Missing", "class");

        Assert.Empty(symbols);
        Assert.DoesNotContain(inspector.InspectedPaths, path => excludedDirectories.Any(directory => string.Equals(directory.FullName, path, StringComparison.Ordinal)));
    }

    [Theory]
    [InlineData(true)]
    [InlineData(false)]
    public void PreviewPolicyRejectsDirectContextThroughEscapingParent(bool isReparsePoint)
    {
        using var root = TestRoot.Create();
        using var external = TestRoot.Create();
        root.Write("Escaping/Contained.cs", "first\n");
        var escapingParent = Path.Combine(root.Path, "Escaping");
        var inspector = new TestStrictPathInspector(new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal)
        {
            [escapingParent] = new(
                Exists: true,
                IsDirectory: true,
                IsReparsePoint: isReparsePoint,
                FinalTarget: Path.Combine(external.Path, "Escaping")),
        });
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance, inspector);

        var failure = Assert.Throws<SearchFailureException>(
            () => engine.GetSourceContext("Escaping/Contained.cs", 1, 0));

        Assert.Equal(
            new SearchFailure("preview_path_refused", "PREVIEW_PATH_REFUSED", "get_source_context"),
            failure.Failure);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
        Assert.DoesNotContain(external.Path, failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void PreviewPolicyRefusesRawReparseComponentBeforeDotNormalization()
    {
        using var root = TestRoot.Create();
        root.Write("real/Marker.cs", "first\n");
        var rawReparse = Path.Combine(root.Path, "reparseAlias");
        var inspector = new TestStrictPathInspector(new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal)
        {
            [rawReparse] = new(
                Exists: true,
                IsDirectory: true,
                IsReparsePoint: true,
                FinalTarget: root.Path),
        });
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance, inspector);
        var rawPath = string.Concat(
            "reparseAlias",
            Path.DirectorySeparatorChar,
            "..",
            Path.DirectorySeparatorChar,
            "real",
            Path.DirectorySeparatorChar,
            "Marker.cs");

        var failure = Assert.Throws<SearchFailureException>(() => engine.GetSourceContext(rawPath, 1, 0));

        Assert.Equal(
            new SearchFailure("preview_path_refused", "PREVIEW_PATH_REFUSED", "get_source_context"),
            failure.Failure);
        Assert.Contains(rawReparse, inspector.InspectedPaths, StringComparer.Ordinal);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void PreviewPolicyRefusesEscapingRootBeforeInspectingTargetGitIgnore()
    {
        using var root = TestRoot.Create();
        using var external = TestRoot.Create();
        root.Write("Contained.cs", "public sealed class Contained { }\n");
        external.Write(".gitignore", "[z-a]\n");
        var rootGitIgnore = Path.Combine(root.Path, ".gitignore");
        var externalGitIgnore = Path.Combine(external.Path, ".gitignore");
        var inspector = new TestStrictPathInspector(new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal)
        {
            [root.Path] = new(
                Exists: true,
                IsDirectory: true,
                IsReparsePoint: true,
                FinalTarget: external.Path),
        });
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance, inspector);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Contained", "class"));

        Assert.Equal(
            new SearchFailure("preview_path_refused", "PREVIEW_PATH_REFUSED", "find_code_symbol"),
            failure.Failure);
        Assert.Equal([root.Path], inspector.InspectedPaths);
        Assert.DoesNotContain(rootGitIgnore, inspector.InspectedPaths, StringComparer.Ordinal);
        Assert.DoesNotContain(externalGitIgnore, inspector.InspectedPaths, StringComparer.Ordinal);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
        Assert.DoesNotContain(external.Path, failure.Message, StringComparison.Ordinal);
    }

    [Theory]
    [InlineData(StrictRootIgnoreFault.Directory, "preview_path_refused", "PREVIEW_PATH_REFUSED")]
    [InlineData(StrictRootIgnoreFault.DanglingLink, "preview_path_refused", "PREVIEW_PATH_REFUSED")]
    [InlineData(StrictRootIgnoreFault.Inaccessible, "preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE")]
    public void PreviewPolicyRejectsInvalidRootGitIgnoreMetadata(
        StrictRootIgnoreFault fault,
        string kind,
        string error)
    {
        using var root = TestRoot.Create();
        root.Write("Contained.cs", "public sealed class Contained { }\n");
        var gitIgnore = Path.Combine(root.Path, ".gitignore");
        var inspector = fault switch
        {
            StrictRootIgnoreFault.Directory => new TestStrictPathInspector(new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal)
            {
                [gitIgnore] = new(Exists: true, IsDirectory: true, IsReparsePoint: false, FinalTarget: null),
            }),
            StrictRootIgnoreFault.DanglingLink => new TestStrictPathInspector(new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal)
            {
                [gitIgnore] = new(Exists: true, IsDirectory: false, IsReparsePoint: true, FinalTarget: null),
            }),
            StrictRootIgnoreFault.Inaccessible => new TestStrictPathInspector(inaccessiblePath: gitIgnore),
            _ => throw new ArgumentOutOfRangeException(nameof(fault)),
        };
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance, inspector);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Contained", "class"));

        Assert.Equal(new SearchFailure(kind, error, "find_code_symbol"), failure.Failure);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void PreviewPolicyAllowsMissingRootGitIgnore()
    {
        using var root = TestRoot.Create();
        root.Write("Contained.cs", "public sealed class Contained { }\n");
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var symbols = engine.FindCodeSymbol("Contained", "class");

        Assert.Single(symbols);
    }

    [Fact]
    public void PreviewPolicyRedactsMalformedRootGitIgnore()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", "[z-a]\n");
        root.Write("Contained.cs", "public sealed class Contained { }\n");
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Contained", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", "find_code_symbol"),
            failure.Failure);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void PreviewPolicyRedactsEmptyCharacterClassRootGitIgnore()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", "[]\n");
        root.Write("Contained.cs", "public sealed class Contained { }\n");
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Contained", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", "find_code_symbol"),
            failure.Failure);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void PreviewPolicyRedactsOverflowingContextRadius()
    {
        using var root = TestRoot.Create();
        root.Write("Contained.cs", "first\n");
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var failure = Assert.Throws<SearchFailureException>(
            () => engine.GetSourceContext("Contained.cs", 1, int.MaxValue));

        Assert.Equal(
            new SearchFailure("invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", "get_source_context"),
            failure.Failure);
    }

    [Fact]
    public void PreviewPolicyHonorsReferenceMaxResultsBelowPolicyCeiling()
    {
        using var root = TestRoot.Create();
        root.Write("References.cs", string.Concat(Enumerable.Repeat("ReferenceLimitMarker\n", 3)));
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var references = engine.FindCodeReferences("ReferenceLimitMarker", 2);

        Assert.Equal([1, 2], references.Select(static match => match.Line));
    }

    [Fact]
    public void PreviewPolicyReservesReferenceBudgetFailureForPolicyCeiling()
    {
        using var root = TestRoot.Create();
        root.Write("References.cs", string.Concat(Enumerable.Repeat("ReferenceLimitMarker\n", 129)));
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var failure = Assert.Throws<SearchFailureException>(
            () => engine.FindCodeReferences("ReferenceLimitMarker", 1000));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_references"),
            failure.Failure);
    }

    [Fact]
    public void PreviewPolicyRejectsThe129thMatchInsteadOfReturningPartialResults()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", string.Empty);
        root.Write(
            "Matches.cs",
            string.Concat(Enumerable.Repeat("public sealed class ResultLimitMarker { }\n", 129)));
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("ResultLimitMarker", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void PreviewPolicyPropagatesCancellationWithoutAResult()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", string.Empty);
        root.Write("Cancelled.cs", "public sealed class Cancelled { }\n");
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance);
        using var cancellation = new CancellationTokenSource();
        cancellation.Cancel();

        Assert.Throws<OperationCanceledException>(
            () => engine.FindCodeSymbol("Cancelled", "class", cancellation.Token));
    }

    [Fact]
    public void PreviewPolicyStopsBeforeInspectingRootGitIgnoreAfterDeadline()
    {
        using var root = TestRoot.Create();
        var clock = new ManualClock();
        var inspector = new TestStrictPathInspector(onInspect: (path, expectedDirectory) =>
        {
            if (expectedDirectory && string.Equals(path, root.Path, StringComparison.Ordinal))
            {
                clock.Expire();
            }
        });
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance, inspector, clock.GetTimestamp);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Missing", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
        Assert.Equal([root.Path], inspector.InspectedPaths);
    }

    [Fact]
    public void PreviewPolicyStopsBeforeInspectingRootGitIgnoreWhenCancelled()
    {
        using var root = TestRoot.Create();
        using var cancellation = new CancellationTokenSource();
        var inspector = new TestStrictPathInspector(onInspect: (path, expectedDirectory) =>
        {
            if (expectedDirectory && string.Equals(path, root.Path, StringComparison.Ordinal))
            {
                cancellation.Cancel();
            }
        });
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance, inspector);

        Assert.Throws<OperationCanceledException>(() => engine.FindCodeSymbol("Missing", "class", cancellation.Token));
        Assert.Equal([root.Path], inspector.InspectedPaths);
    }

    [Fact]
    public void PreviewPolicyStopsParsingLargeRootGitIgnoreAtDeadline()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", LargeGitIgnore());
        root.Write("Contained.cs", "public sealed class Contained { }\n");
        var clock = new CallLimitedClock(allowedTimestampReads: 80, expiredTimestamp: 6L * System.Diagnostics.Stopwatch.Frequency);
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            FileSystemStrictPathInspector.Instance,
            clock.GetTimestamp);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Contained", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
    }

    [Fact]
    public void PreviewPolicyStopsParsingLargeRootGitIgnoreWhenCancelled()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", LargeGitIgnore());
        root.Write("Contained.cs", "public sealed class Contained { }\n");
        using var cancellation = new CancellationTokenSource();
        var clock = new CallLimitedClock(allowedTimestampReads: 80, expiredTimestamp: 0, cancellation.Cancel);
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            FileSystemStrictPathInspector.Instance,
            clock.GetTimestamp);

        Assert.Throws<OperationCanceledException>(
            () => engine.FindCodeSymbol("Contained", "class", cancellation.Token));
    }

    [Fact]
    public void PreviewPolicyStopsApplyingRootGitIgnoreRulesAtDeadline()
    {
        using var root = TestRoot.Create();
        root.Write(".gitignore", "not-a-match\n");
        var source = Path.Combine(root.Path, "Contained.cs");
        root.Write("Contained.cs", "public sealed class Contained { }\n");
        var clock = new ManualClock();
        var sourceMetadataReads = 0;
        var inspector = new TestStrictPathInspector(onInspect: (path, expectedDirectory) =>
        {
            if (!expectedDirectory && string.Equals(path, source, StringComparison.Ordinal) && ++sourceMetadataReads == 1)
            {
                clock.Expire();
            }
        });
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance, inspector, clock.GetTimestamp);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Contained", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
        Assert.Equal(1, sourceMetadataReads);
    }

    [Fact]
    public void PreviewPolicyStopsDirectoryEnumerationBeforeAllEntriesAtDirectoryLimit()
    {
        const int totalDirectories = 2050;
        using var root = TestRoot.Create();
        var directories = Enumerable.Range(0, totalDirectories)
            .Select(index => new DirectoryInfo(Path.Combine(root.Path, $"Directory{index:D4}")))
            .ToArray();
        var inspector = new TestStrictPathInspector(directories.ToDictionary(
            static directory => directory.FullName,
            static _ => new StrictPathInfo(Exists: true, IsDirectory: true, IsReparsePoint: false, FinalTarget: null),
            StringComparer.Ordinal));
        var enumerated = 0;

        IEnumerable<DirectoryInfo> EnumerateDirectories(DirectoryInfo directory)
        {
            if (!string.Equals(directory.FullName, root.Path, StringComparison.Ordinal))
            {
                yield break;
            }

            foreach (var child in directories)
            {
                enumerated++;
                yield return child;
            }
        }

        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            inspector,
            enumerateDirectories: EnumerateDirectories,
            enumerateFiles: static _ => []);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Missing", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
        Assert.Equal(2048, enumerated);
        Assert.True(enumerated < totalDirectories);
    }

    [Fact]
    public void PreviewPolicyStopsFileEnumerationBeforeAllEntriesAtDeadline()
    {
        const int totalFiles = 8;
        using var root = TestRoot.Create();
        var clock = new ManualClock();
        var enumerated = 0;

        IEnumerable<FileInfo> EnumerateFiles(DirectoryInfo _)
        {
            for (var index = 0; index < totalFiles; index++)
            {
                enumerated++;
                if (enumerated == 2)
                {
                    clock.Set(6L * System.Diagnostics.Stopwatch.Frequency);
                }

                yield return new FileInfo(Path.Combine(root.Path, $"Entry{index:D4}.txt"));
            }
        }

        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            FileSystemStrictPathInspector.Instance,
            clock.GetTimestamp,
            enumerateFiles: EnumerateFiles);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeReferences("Missing", 1));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_references"),
            failure.Failure);
        Assert.Equal(2, enumerated);
        Assert.True(enumerated < totalFiles);
    }

    [Fact]
    public void PreviewPolicyRejectsDeadlineEqualityBeforeAnotherFileEntryIsAdmitted()
    {
        const int totalFiles = 8;
        using var root = TestRoot.Create();
        var clock = new ManualClock();
        var enumerated = 0;

        IEnumerable<FileInfo> EnumerateFiles(DirectoryInfo _)
        {
            for (var index = 0; index < totalFiles; index++)
            {
                enumerated++;
                if (enumerated == 2)
                {
                    clock.Set(5L * System.Diagnostics.Stopwatch.Frequency);
                }

                yield return new FileInfo(Path.Combine(root.Path, $"Entry{index:D4}.txt"));
            }
        }

        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            FileSystemStrictPathInspector.Instance,
            clock.GetTimestamp,
            enumerateFiles: EnumerateFiles);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeReferences("Missing", 1));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_references"),
            failure.Failure);
        Assert.Equal(2, enumerated);
        Assert.True(enumerated < totalFiles);
    }

    [Fact]
    public void PreviewPolicyStopsFileEnumerationBeforeAllEntriesWhenCancelled()
    {
        const int totalFiles = 8;
        using var root = TestRoot.Create();
        using var cancellation = new CancellationTokenSource();
        var enumerated = 0;

        IEnumerable<FileInfo> EnumerateFiles(DirectoryInfo _)
        {
            for (var index = 0; index < totalFiles; index++)
            {
                enumerated++;
                if (enumerated == 2)
                {
                    cancellation.Cancel();
                }

                yield return new FileInfo(Path.Combine(root.Path, $"Entry{index:D4}.txt"));
            }
        }

        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            FileSystemStrictPathInspector.Instance,
            enumerateFiles: EnumerateFiles);

        Assert.Throws<OperationCanceledException>(() => engine.FindCodeReferences("Missing", 1, cancellation.Token));
        Assert.Equal(2, enumerated);
        Assert.True(enumerated < totalFiles);
    }

    [Theory]
    [InlineData("find_code_symbol")]
    [InlineData("find_code_references")]
    public void PreviewPolicyMapsSourceRegexTimeoutToBudgetFailure(string tool)
    {
        using var root = TestRoot.Create();
        root.Write("Large.cs", "no matching reference\n");
        System.Text.RegularExpressions.Regex? strictRegex = null;
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            FileSystemStrictPathInspector.Instance,
            strictRegexMatch: (regex, value) =>
            {
                strictRegex = regex;
                throw new System.Text.RegularExpressions.RegexMatchTimeoutException(value.ToString(), regex.ToString(), regex.MatchTimeout);
            });

        var failure = tool == "find_code_symbol"
            ? Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("RegexBudgetMarker", "class"))
            : Assert.Throws<SearchFailureException>(() => engine.FindCodeReferences("RegexBudgetMarker", 1));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", tool),
            failure.Failure);
        Assert.NotNull(strictRegex);
        Assert.NotEqual(System.Text.RegularExpressions.Regex.InfiniteMatchTimeout, strictRegex!.MatchTimeout);
        Assert.True(strictRegex.MatchTimeout > TimeSpan.Zero);
        Assert.True(strictRegex.MatchTimeout <= TimeSpan.FromMilliseconds(100));
    }

    [Fact]
    public void PreviewPolicyPropagatesCancellationDuringSourceRegexMatch()
    {
        using var root = TestRoot.Create();
        using var cancellation = new CancellationTokenSource();
        root.Write("Large.cs", "no matching reference\n");
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            FileSystemStrictPathInspector.Instance,
            strictRegexMatch: (regex, value) =>
            {
                cancellation.Cancel();
                throw new System.Text.RegularExpressions.RegexMatchTimeoutException(value.ToString(), regex.ToString(), regex.MatchTimeout);
            });

        Assert.Throws<OperationCanceledException>(
            () => engine.FindCodeReferences("RegexBudgetMarker", 1, cancellation.Token));
    }

    public enum StrictRootIgnoreFault
    {
        Directory,
        DanglingLink,
        Inaccessible,
    }

    private sealed class MappedPhysicalPathInspector(
        IReadOnlyDictionary<string, (string CanonicalPath, bool IsDirectory)> entries) : IStrictPathInspector
    {
        public StrictPathInfo Inspect(string path, bool expectedDirectory) => entries.TryGetValue(path, out var entry)
            ? new StrictPathInfo(Exists: true, IsDirectory: entry.IsDirectory, IsReparsePoint: false, FinalTarget: null)
            : StrictPathInfo.Missing;

        public string ResolveFilePath(string path) => entries[path].CanonicalPath;
    }

    private sealed class TestStrictPathInspector : IStrictPathInspector
    {
        private readonly IReadOnlyDictionary<string, StrictPathInfo> _entries;
        private readonly string? _inaccessiblePath;
        private readonly List<string> _inspectedPaths = [];
        private readonly Action<string, bool>? _onInspect;

        internal IReadOnlyList<string> InspectedPaths => _inspectedPaths;


        internal TestStrictPathInspector(
            IReadOnlyDictionary<string, StrictPathInfo>? entries = null,
            string? inaccessiblePath = null,
            Action<string, bool>? onInspect = null)
        {
            _entries = entries ?? new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal);
            _inaccessiblePath = inaccessiblePath;
            _onInspect = onInspect;
        }

        public string ResolveFilePath(string path) => FileSystemStrictPathInspector.Instance.ResolveFilePath(path);

        public StrictPathInfo Inspect(string path, bool expectedDirectory)
        {
            _inspectedPaths.Add(path);
            _onInspect?.Invoke(path, expectedDirectory);

            if (string.Equals(path, _inaccessiblePath, StringComparison.Ordinal))
            {
                throw new UnauthorizedAccessException();
            }

            return _entries.TryGetValue(path, out var entry)
                ? entry
                : FileSystemStrictPathInspector.Instance.Inspect(path, expectedDirectory);
        }
    }

    private static string LargeGitIgnore() => string.Concat(Enumerable.Repeat(new string('x', 255) + "\n", 4096));

    private sealed class CallLimitedClock
    {
        private readonly int _allowedTimestampReads;
        private readonly long _expiredTimestamp;
        private readonly Action? _onExceeded;
        private int _timestampReads;

        internal CallLimitedClock(int allowedTimestampReads, long expiredTimestamp, Action? onExceeded = null)
        {
            _allowedTimestampReads = allowedTimestampReads;
            _expiredTimestamp = expiredTimestamp;
            _onExceeded = onExceeded;
        }

        internal long GetTimestamp()
        {
            if (++_timestampReads <= _allowedTimestampReads)
            {
                return 0;
            }

            _onExceeded?.Invoke();
            return _expiredTimestamp;
        }
    }

    private sealed class ManualClock
    {
        private long _timestamp;

        internal long GetTimestamp() => _timestamp;

        internal void Expire() => _timestamp = 6L * System.Diagnostics.Stopwatch.Frequency;
        internal void Set(long timestamp) => _timestamp = timestamp;
    }

    private sealed class TestRoot : IDisposable
    {
        private TestRoot(string path) => Path = path;

        internal string Path { get; }

        internal static TestRoot Create()
        {
            var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"netcoredbg-code-search-{Guid.NewGuid():N}");
            Directory.CreateDirectory(path);
            return new TestRoot(path);
        }

        internal void Write(string relativePath, string contents)
        {
            var path = System.IO.Path.Combine(Path, relativePath.Replace('/', System.IO.Path.DirectorySeparatorChar));
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
            File.WriteAllText(path, contents);
        }

        public void Dispose()
        {
            if (Directory.Exists(Path))
            {
                Directory.Delete(Path, recursive: true);
            }
        }
    }
}
