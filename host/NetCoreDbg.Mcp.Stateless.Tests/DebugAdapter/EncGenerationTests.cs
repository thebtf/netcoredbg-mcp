using System.Collections.Immutable;
using System.Reflection.Metadata;
using NetcoredbgMcp.EncCompiler;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;

public sealed class EncGenerationTests
{
    [Fact]
    public async Task CommittedEdits_UseConsecutiveMetadataGenerationsAndRejectStaleSource()
    {
        var project = Path.Combine(RepositoryLayout.Root, "tests", "fixtures", "NativePauseApp");
        var sourcePath = Path.Combine(project, "Program.cs");
        var modulePath = TestOutputPathResolver.ResolveManagedAssembly(RepositoryLayout.Root,
            Path.Combine("tests", "fixtures", "NativePauseApp"), "NativePauseApp");
        var original = await File.ReadAllTextAsync(sourcePath);
        var lines = original.Replace("\r\n", "\n", StringComparison.Ordinal).Split('\n');
        var line = Array.FindIndex(lines, text => text.Contains("iteration = unchecked(iteration + 1);", StringComparison.Ordinal)) + 1;
        Assert.True(line > 0);
        var firstEdit = new[] { new SourceEdit(line, line, "            iteration = unchecked(iteration + 100);") };
        var secondEdit = new[] { new SourceEdit(line, line, "            iteration = unchecked(iteration + 200);") };
        var scratch = Path.Combine(RepositoryLayout.ScratchRoot, $"enc-generation-{Guid.NewGuid():N}");
        Directory.CreateDirectory(scratch);
        try
        {
            using var generation = new DeltaEmitter().StartGeneration(project, sourcePath, original, modulePath);
            var first = await generation.ProposeAsync(sourcePath, original, firstEdit, Path.Combine(scratch, "first"));
            Assert.True(first.Result.Success, string.Join("; ", first.Result.Diagnostics.Concat(first.Result.RudeEdits)));
            var uncommitted = await generation.ProposeAsync(sourcePath, original, secondEdit, Path.Combine(scratch, "uncommitted"));
            Assert.True(uncommitted.Result.Success, string.Join("; ", uncommitted.Result.Diagnostics.Concat(uncommitted.Result.RudeEdits)));
            generation.Commit(first);
            Assert.Throws<InvalidOperationException>(() => generation.Commit(uncommitted));
            var stale = await generation.ProposeAsync(sourcePath, original, secondEdit, Path.Combine(scratch, "stale"));
            Assert.False(stale.Result.Success);
            var current = DeltaEmitter.ApplyEdits(original, firstEdit);
            var invalid = await generation.ProposeAsync(sourcePath, current,
                [new SourceEdit(line, line, "            iteration = ;")], Path.Combine(scratch, "invalid"));
            Assert.False(invalid.Result.Success);
            Assert.NotEmpty(invalid.Result.Diagnostics);
            var second = await generation.ProposeAsync(sourcePath, current, secondEdit, Path.Combine(scratch, "second"));
            Assert.True(second.Result.Success, string.Join("; ", second.Result.Diagnostics.Concat(second.Result.RudeEdits)));
            Assert.Equal(1, ReadGeneration(first.Result.MetadataDeltaPath!));
            Assert.Equal(2, ReadGeneration(second.Result.MetadataDeltaPath!));
            generation.Commit(second);
        }
        finally
        {
            Directory.Delete(scratch, recursive: true);
        }
    }

    private static int ReadGeneration(string path)
    {
        using var provider = MetadataReaderProvider.FromMetadataImage(ImmutableArray.Create(File.ReadAllBytes(path)));
        return provider.GetMetadataReader().GetModuleDefinition().Generation;
    }
}
