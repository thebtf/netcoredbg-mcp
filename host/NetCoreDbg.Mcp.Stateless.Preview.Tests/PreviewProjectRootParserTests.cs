using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Preview.Tests;

public sealed class PreviewProjectRootParserTests
{
    [Theory]
    [InlineData("//server/share")]
    [InlineData("//?/C:/extended")]
    [InlineData("//./C:/device")]
    [InlineData("//?/Volume{00000000-0000-0000-0000-000000000000}/")]
    [InlineData("//?/UNC/server/share")]
    public void RawForwardSlashAuthoritiesAreDisallowed(string path)
    {
        Assert.True(PreviewProjectRootParser.IsDisallowedWindowsPath(path));
    }

    [Theory]
    [InlineData(@"\\server\share")]
    [InlineData("//server/share")]
    [InlineData(@"\\?\C:\extended")]
    [InlineData("//?/C:/extended")]
    [InlineData(@"\\.\C:\device")]
    [InlineData("//./C:/device")]
    [InlineData(@"\\?\Volume{00000000-0000-0000-0000-000000000000}\")]
    [InlineData("//?/Volume{00000000-0000-0000-0000-000000000000}/")]
    [InlineData(@"\\?\UNC\server\share")]
    [InlineData("//?/UNC/server/share")]
    public void DisallowedRawAuthoritiesReturnBeforeEveryFileAttributeProbe(string path)
    {
        var probes = 0;

        var parsed = PreviewProjectRootParser.TryParse(
            ["--project", path],
            out _,
            _ =>
            {
                probes++;
                throw new InvalidOperationException("Disallowed authority reached a filesystem probe.");
            });

        Assert.False(parsed);
        Assert.Equal(0, probes);
    }

    [Fact]
    public void RegularFixedDriveControlReachesFilesystemProbes()
    {
        var probes = 0;

        var parsed = PreviewProjectRootParser.TryParse(
            ["--project", PreviewRepositoryLayout.FixtureRoot],
            out var root,
            path =>
            {
                probes++;
                return File.GetAttributes(path);
            });

        Assert.True(parsed);
        Assert.Equal(Path.TrimEndingDirectorySeparator(PreviewRepositoryLayout.FixtureRoot), root.Path);
        Assert.True(probes > 0);
    }
}
