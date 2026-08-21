namespace NetCoreDbg.Mcp.Stateless.Preview;

internal sealed record PreviewProjectRoot(string Path);

internal static class PreviewProjectRootParser
{
    private static readonly Func<string, FileAttributes> GetFileAttributes = File.GetAttributes;

    internal static bool TryParse(string[] arguments, out PreviewProjectRoot root) =>
        TryParse(arguments, out root, GetFileAttributes);

    internal static bool TryParse(
        string[] arguments,
        out PreviewProjectRoot root,
        Func<string, FileAttributes> getAttributes)
    {
        root = default!;
        if (arguments.Length != 2
            || !string.Equals(arguments[0], "--project", StringComparison.Ordinal)
            || string.IsNullOrWhiteSpace(arguments[1])
            || !OperatingSystem.IsWindows()
            || !Path.IsPathFullyQualified(arguments[1])
            || IsDisallowedWindowsPath(arguments[1]))
        {
            return false;
        }

        try
        {
            if (HasReparsePointComponent(arguments[1], getAttributes))
            {
                return false;
            }

            var path = Path.TrimEndingDirectorySeparator(Path.GetFullPath(arguments[1]));
            if (!IsLocalDrivePath(path)
                || !Directory.Exists(path)
                || new DriveInfo(Path.GetPathRoot(path)!).DriveType != DriveType.Fixed
                || HasReparsePointComponent(path, getAttributes))
            {
                return false;
            }

            var attributes = getAttributes(path);
            if ((attributes & FileAttributes.Directory) == 0
                || (attributes & FileAttributes.ReparsePoint) != 0)
            {
                return false;
            }

            root = new PreviewProjectRoot(path);
            return true;
        }
        catch (Exception)
        {
            return false;
        }
    }

    internal static bool IsDisallowedWindowsPath(string path) =>
        path.Length >= 2
        && path[0] is '\\' or '/'
        && path[1] is '\\' or '/';

    private static bool HasReparsePointComponent(string path, Func<string, FileAttributes> getAttributes)
    {
        var root = Path.GetPathRoot(path);
        if (string.IsNullOrEmpty(root))
        {
            return true;
        }

        for (var start = root.Length; start < path.Length;)
        {
            var separator = path.IndexOf(Path.DirectorySeparatorChar, start);
            var alternateSeparator = path.IndexOf(Path.AltDirectorySeparatorChar, start);
            if (alternateSeparator >= 0 && (separator < 0 || alternateSeparator < separator))
            {
                separator = alternateSeparator;
            }
            var component = separator < 0 ? path : path[..separator];
            if ((getAttributes(component) & FileAttributes.ReparsePoint) != 0)
            {
                return true;
            }

            if (separator < 0)
            {
                return false;
            }

            start = separator + 1;
        }

        return false;
    }

    private static bool IsLocalDrivePath(string path)
    {
        if (path.Length < 3
            || !char.IsAsciiLetter(path[0])
            || path[1] != ':'
            || path[2] != Path.DirectorySeparatorChar)
        {
            return false;
        }

        return string.Equals(
            Path.GetPathRoot(path),
            string.Concat(path[0], ":", Path.DirectorySeparatorChar),
            StringComparison.OrdinalIgnoreCase);
    }
}
