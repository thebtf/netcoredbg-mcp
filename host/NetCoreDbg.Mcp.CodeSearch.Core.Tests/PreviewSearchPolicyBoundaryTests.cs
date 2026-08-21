using System.Text;
using Xunit;

namespace NetCoreDbg.Mcp.CodeSearch.Core.Tests;

public sealed class PreviewSearchPolicyBoundaryTests
{
    [Fact]
    public void PreviewPolicyRejectsWhitespaceAnd257Utf16CodeUnitNamesBeforeFilesystemAccess()
    {
        using var root = TestRoot.Create();
        var inspector = new RecordingStrictPathInspector(root.Path);
        var directoriesEnumerated = 0;
        var filesEnumerated = 0;
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            inspector,
            enumerateDirectories: _ =>
            {
                directoriesEnumerated++;
                return [];
            },
            enumerateFiles: _ =>
            {
                filesEnumerated++;
                return [];
            });
        var tooLong = string.Concat(Enumerable.Repeat(char.ConvertFromUtf32(0x1f600), 128)) + "x";

        Assert.Equal(257, tooLong.Length);
        foreach (var name in new[] { " ", tooLong })
        {
            var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol(name, "class"));

            Assert.Equal(
                new SearchFailure("invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", "find_code_symbol"),
                failure.Failure);
        }

        Assert.Empty(inspector.InspectedPaths);
        Assert.Equal(0, directoriesEnumerated);
        Assert.Equal(0, filesEnumerated);
    }

    [Fact]
    public void PreviewPolicyAccepts20000NonDirectoryEntriesAndRefusesTheNextBeforeSourceProcessing()
    {
        const int maximumEntries = 20_000;
        using var root = TestRoot.Create();
        var acceptedEntries = Enumerable.Range(0, maximumEntries)
            .Select(index => new FileInfo(Path.Combine(root.Path, $"Entry{index:D5}.txt")))
            .ToArray();
        var acceptedEnumerationCount = 0;
        var acceptedEngine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            new RecordingStrictPathInspector(root.Path),
            enumerateFiles: _ => Enumerate(acceptedEntries, () => acceptedEnumerationCount++));

        var accepted = acceptedEngine.FindCodeSymbol("Absent", "class");

        Assert.Empty(accepted);
        Assert.Equal(maximumEntries, acceptedEnumerationCount);

        var next = new FileInfo(Path.Combine(root.Path, "SourceMustNotBeProcessed.cs"));
        var rejectedInspector = new RecordingStrictPathInspector(root.Path);
        var rejectedEnumerationCount = 0;
        var rejectedEngine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            rejectedInspector,
            enumerateFiles: _ => Enumerate(acceptedEntries.Append(next), () => rejectedEnumerationCount++));

        var failure = Assert.Throws<SearchFailureException>(() => rejectedEngine.FindCodeSymbol("Absent", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
        Assert.Equal(maximumEntries + 1, rejectedEnumerationCount);
        Assert.DoesNotContain(next.FullName, rejectedInspector.InspectedPaths, StringComparer.Ordinal);
    }

    [Theory]
    [InlineData(1024 * 1024, true)]
    [InlineData(1024 * 1024 + 1, false)]
    public void PreviewPolicyEnforcesExactSourceReadByteLimitBeforeContentRead(int bytes, bool accepted)
    {
        using var root = TestRoot.Create();
        var source = new FileInfo(Path.Combine(root.Path, "Sized.cs"));
        var stream = new TrackingReadStream(bytes);
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            new RecordingStrictPathInspector(root.Path),
            enumerateFiles: _ => [source],
            openRead: _ => stream);

        if (accepted)
        {
            Assert.Empty(engine.FindCodeSymbol("Absent", "class"));
            Assert.True(stream.ReadOperations > 0);
            return;
        }

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Absent", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
        Assert.Equal(0, stream.ReadOperations);
    }

    [Theory]
    [InlineData(1024 * 1024, true)]
    [InlineData(1024 * 1024 + 1, false)]
    public void PreviewPolicyEnforcesExactRootGitIgnoreByteLimitBeforeContentRead(int bytes, bool accepted)
    {
        using var root = TestRoot.Create();
        var gitIgnore = Path.Combine(root.Path, ".gitignore");
        var stream = new TrackingReadStream(bytes, (byte)'#');
        var inspector = new RecordingStrictPathInspector(root.Path, new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal)
        {
            [gitIgnore] = RegularFile,
        });
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            inspector,
            enumerateFiles: static _ => [],
            openRead: _ => stream);

        if (accepted)
        {
            Assert.Empty(engine.FindCodeSymbol("Absent", "class"));
            Assert.True(stream.ReadOperations > 0);
            return;
        }

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Absent", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
        Assert.Equal(0, stream.ReadOperations);
    }

    [Fact]
    public void PreviewPolicyAcceptsExactly16MiBAndRefusesTheNextByteBeforeItIsRead()
    {
        const int oneMiB = 1024 * 1024;
        using var root = TestRoot.Create();
        var files = Enumerable.Range(0, 16)
            .Select(index => new FileInfo(Path.Combine(root.Path, $"Full{index:D2}.cs")))
            .ToArray();
        var acceptedStreams = files.ToDictionary(static file => file.FullName, _ => new TrackingReadStream(oneMiB), StringComparer.Ordinal);
        var acceptedEngine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            new RecordingStrictPathInspector(root.Path),
            enumerateFiles: _ => files,
            openRead: path => acceptedStreams[path]);

        Assert.Empty(acceptedEngine.FindCodeSymbol("Absent", "class"));
        Assert.All(acceptedStreams.Values, static stream => Assert.True(stream.ReadOperations > 0));

        var next = new FileInfo(Path.Combine(root.Path, "NextByte.cs"));
        var rejectedStreams = files.ToDictionary(static file => file.FullName, _ => new TrackingReadStream(oneMiB), StringComparer.Ordinal);
        var nextByte = new TrackingReadStream(1);
        rejectedStreams.Add(next.FullName, nextByte);
        var rejectedEngine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            new RecordingStrictPathInspector(root.Path),
            enumerateFiles: _ => files.Append(next),
            openRead: path => rejectedStreams[path]);

        var failure = Assert.Throws<SearchFailureException>(() => rejectedEngine.FindCodeSymbol("Absent", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_budget_exceeded", "PREVIEW_SEARCH_BUDGET_EXCEEDED", "find_code_symbol"),
            failure.Failure);
        Assert.Equal(0, nextByte.ReadOperations);
    }

    [Fact]
    public void PreviewPolicyMapsPostMatchSourceReadFailureToClosedUnreadableWithoutResult()
    {
        using var root = TestRoot.Create();
        var matched = new FileInfo(Path.Combine(root.Path, "A-Matched.cs"));
        var faulted = new FileInfo(Path.Combine(root.Path, "B-Faulted.cs"));
        var opened = new List<string>();
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            new RecordingStrictPathInspector(root.Path),
            enumerateFiles: _ => [matched, faulted],
            openRead: path =>
            {
                opened.Add(path);
                return string.Equals(path, faulted.FullName, StringComparison.Ordinal)
                    ? new ThrowingReadStream()
                    : new TrackingReadStream(Encoding.UTF8.GetBytes("public sealed class PartialMarker { }\n"));
            });

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("PartialMarker", "class"));

        Assert.Equal(
            new SearchFailure("preview_search_unreadable", "PREVIEW_SEARCH_UNREADABLE", "find_code_symbol"),
            failure.Failure);
        Assert.Equal([matched.FullName, faulted.FullName], opened);
        Assert.DoesNotContain(root.Path, failure.Message, StringComparison.Ordinal);
        Assert.DoesNotContain("PartialMarker", failure.Message, StringComparison.Ordinal);
    }

    [Fact]
    public void PreviewPolicyTruncatesContextAt512UnicodeScalarsWithoutSplittingSurrogatePair()
    {
        using var root = TestRoot.Create();
        var source = Path.Combine(root.Path, "Context.cs");
        var emoji = char.ConvertFromUtf32(0x1f600);
        var sourceText = new string('a', 511) + emoji + "z";
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            new RecordingStrictPathInspector(root.Path),
            openRead: _ => new TrackingReadStream(Encoding.UTF8.GetBytes(sourceText)));

        var context = engine.GetSourceContext("Context.cs", 1, 0);
        var text = Assert.Single(context.Lines).Text;

        Assert.Equal(512, text.EnumerateRunes().Count());
        Assert.Equal(new string('a', 511) + emoji, text);
        Assert.False(char.IsHighSurrogate(text[^1]));
        Assert.False(char.IsLowSurrogate(text[0]));
    }

    [Fact]
    public void PreviewPolicyValidatesInvalidToolArgumentsBeforeConstructionFilesystemProbe()
    {
        using var root = TestRoot.Create();
        var constructionFilesystemProbes = 0;
        var inspector = new RecordingStrictPathInspector(root.Path);
        var engine = new SymbolSearchEngine(
            root.Path,
            PreviewSearchPolicy.Instance,
            inspector,
            directoryExists: _ =>
            {
                constructionFilesystemProbes++;
                return true;
            });
        var tooLong = string.Concat(Enumerable.Repeat(char.ConvertFromUtf32(0x1f600), 128)) + "x";

        foreach (var name in new[] { " ", tooLong })
        {
            var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol(name, "class"));

            Assert.Equal(
                new SearchFailure("invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", "find_code_symbol"),
                failure.Failure);
        }

        var contextFailure = Assert.Throws<SearchFailureException>(() => engine.GetSourceContext(" ", 1, 0));

        Assert.Equal(
            new SearchFailure("invalid_tool_arguments", "INVALID_TOOL_ARGUMENTS", "get_source_context"),
            contextFailure.Failure);
        Assert.Equal(0, constructionFilesystemProbes);
        Assert.Empty(inspector.InspectedPaths);
    }

    [Fact]
    public void LegacyPolicyRetainsConstructionRootValidation()
    {
        var constructionFilesystemProbes = 0;

        Assert.Throws<DirectoryNotFoundException>(() => new SymbolSearchEngine(
            "missing-project-root",
            LegacySearchPolicy.Instance,
            FileSystemStrictPathInspector.Instance,
            directoryExists: _ =>
            {
                constructionFilesystemProbes++;
                return false;
            }));

        Assert.Equal(1, constructionFilesystemProbes);
    }

    [Fact]
    public void FileSystemStrictPathInspectorRetainsDetectedDanglingReparseIdentity()
    {
        var inspector = new FileSystemStrictPathInspector(
            static _ => FileAttributes.ReparsePoint,
            static (_, _) => throw new FileNotFoundException());

        var info = inspector.Inspect("dangling", expectedDirectory: false);

        Assert.True(info.Exists);
        Assert.False(info.IsDirectory);
        Assert.True(info.IsReparsePoint);
        Assert.Null(info.FinalTarget);
    }

    [Fact]
    public void PreviewPolicyMapsDetectedDanglingReparseToPathRefused()
    {
        using var root = TestRoot.Create();
        var inspector = new FileSystemStrictPathInspector(
            path => string.Equals(path, root.Path, StringComparison.Ordinal)
                ? FileAttributes.Directory
                : FileAttributes.ReparsePoint,
            static (_, _) => throw new FileNotFoundException());
        var engine = new SymbolSearchEngine(root.Path, PreviewSearchPolicy.Instance, inspector);

        var failure = Assert.Throws<SearchFailureException>(() => engine.FindCodeSymbol("Contained", "class"));

        Assert.Equal(
            new SearchFailure("preview_path_refused", "PREVIEW_PATH_REFUSED", "find_code_symbol"),
            failure.Failure);
    }

    private static IEnumerable<FileInfo> Enumerate(IEnumerable<FileInfo> files, Action onEntry)
    {
        foreach (var file in files)
        {
            onEntry();
            yield return file;
        }
    }

    private static StrictPathInfo RegularFile { get; } = new(
        Exists: true,
        IsDirectory: false,
        IsReparsePoint: false,
        FinalTarget: null);

    private sealed class TrackingReadStream : MemoryStream
    {
        internal TrackingReadStream(int bytes, byte firstByte = 0)
            : this(CreateBytes(bytes, firstByte))
        {
        }

        internal TrackingReadStream(byte[] bytes)
            : base(bytes, writable: false)
        {
        }

        internal int ReadOperations { get; private set; }

        public override int Read(byte[] buffer, int offset, int count)
        {
            ReadOperations++;
            return base.Read(buffer, offset, count);
        }

        public override int ReadByte()
        {
            ReadOperations++;
            return base.ReadByte();
        }

        private static byte[] CreateBytes(int bytes, byte firstByte)
        {
            var contents = new byte[bytes];
            if (contents.Length > 0)
            {
                contents[0] = firstByte;
            }

            return contents;
        }
    }

    private sealed class ThrowingReadStream : MemoryStream
    {
        internal ThrowingReadStream()
            : base(new byte[1], writable: false)
        {
        }

        public override int Read(byte[] buffer, int offset, int count) => throw new IOException();
    }

    private sealed class RecordingStrictPathInspector : IStrictPathInspector
    {
        private readonly string _root;
        private readonly IReadOnlyDictionary<string, StrictPathInfo> _entries;
        private readonly List<string> _inspectedPaths = [];

        internal RecordingStrictPathInspector(
            string root,
            IReadOnlyDictionary<string, StrictPathInfo>? entries = null)
        {
            _root = root;
            _entries = entries ?? new Dictionary<string, StrictPathInfo>(StringComparer.Ordinal);
        }

        internal IReadOnlyList<string> InspectedPaths => _inspectedPaths;

        public StrictPathInfo Inspect(string path, bool expectedDirectory)
        {
            _inspectedPaths.Add(path);
            if (_entries.TryGetValue(path, out var entry))
            {
                return entry;
            }

            if (string.Equals(path, Path.Combine(_root, ".gitignore"), StringComparison.Ordinal))
            {
                return StrictPathInfo.Missing;
            }

            return new StrictPathInfo(
                Exists: true,
                IsDirectory: expectedDirectory,
                IsReparsePoint: false,
                FinalTarget: null);
        }
    }

    private sealed class TestRoot : IDisposable
    {
        private TestRoot(string path)
        {
            Path = path;
        }

        internal string Path { get; }

        internal static TestRoot Create()
        {
            var path = System.IO.Path.Combine(System.IO.Path.GetTempPath(), $"netcoredbg-code-search-{Guid.NewGuid():N}");
            Directory.CreateDirectory(path);
            return new TestRoot(path);
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
