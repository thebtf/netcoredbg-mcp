namespace NetCoreDbg.Mcp.CodeSearch.Core;

internal sealed record StrictPathInfo(
    bool Exists,
    bool IsDirectory,
    bool IsReparsePoint,
    string? FinalTarget)
{
    internal static StrictPathInfo Missing { get; } = new(
        Exists: false,
        IsDirectory: false,
        IsReparsePoint: false,
        FinalTarget: null);
}

internal interface IStrictPathInspector
{
    StrictPathInfo Inspect(string path, bool expectedDirectory);
}

internal sealed class FileSystemStrictPathInspector : IStrictPathInspector
{
    internal static FileSystemStrictPathInspector Instance { get; } = new();

    private readonly Func<string, FileAttributes> _getAttributes;
    private readonly Func<string, bool, string?> _resolveLinkTarget;

    private FileSystemStrictPathInspector()
        : this(
            File.GetAttributes,
            static (path, expectedDirectory) => expectedDirectory
                ? new DirectoryInfo(path).ResolveLinkTarget(returnFinalTarget: true)?.FullName
                : new FileInfo(path).ResolveLinkTarget(returnFinalTarget: true)?.FullName)
    {
    }

    internal FileSystemStrictPathInspector(
        Func<string, FileAttributes> getAttributes,
        Func<string, bool, string?> resolveLinkTarget)
    {
        _getAttributes = getAttributes;
        _resolveLinkTarget = resolveLinkTarget;
    }

    public StrictPathInfo Inspect(string path, bool expectedDirectory)
    {
        FileAttributes attributes;
        try
        {
            attributes = _getAttributes(path);
        }
        catch (Exception ex) when (ex is FileNotFoundException or DirectoryNotFoundException)
        {
            return StrictPathInfo.Missing;
        }

        var isDirectory = (attributes & FileAttributes.Directory) != 0;
        var isReparsePoint = (attributes & FileAttributes.ReparsePoint) != 0;
        string? finalTarget = null;
        if (isReparsePoint)
        {
            try
            {
                finalTarget = _resolveLinkTarget(path, expectedDirectory);
            }
            catch (Exception ex) when (ex is FileNotFoundException or DirectoryNotFoundException)
            {
            }
        }

        return new StrictPathInfo(
            Exists: true,
            IsDirectory: isDirectory,
            IsReparsePoint: isReparsePoint,
            FinalTarget: finalTarget);
    }
}
