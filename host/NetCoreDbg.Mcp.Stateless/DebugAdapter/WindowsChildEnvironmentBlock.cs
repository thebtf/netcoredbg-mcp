using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

/// <summary>Owns one filtered Unicode environment block for a Windows child process.</summary>
internal sealed class WindowsChildEnvironmentBlock : SafeHandleZeroOrMinusOneIsInvalid
{
    private readonly int _byteCount;
    private readonly Dictionary<string, List<string>> _inheritedSpellings;

    private WindowsChildEnvironmentBlock(int byteCount, Dictionary<string, List<string>> inheritedSpellings) : base(ownsHandle: true)
    {
        _byteCount = byteCount;
        _inheritedSpellings = inheritedSpellings;
        SetHandle(Marshal.AllocHGlobal(byteCount));
    }

    internal static WindowsChildEnvironmentBlock Create(IReadOnlySet<string>? removals, IEnumerable<string>? namesToMatch)
    {
        var source = GetEnvironmentStrings();
        if (source == IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error(), "Could not read the inherited environment.");
        try
        {
            var entries = new List<Entry>();
            var requestedNames = namesToMatch is null ? null : new HashSet<string>(namesToMatch, StringComparer.OrdinalIgnoreCase);
            var inheritedSpellings = new Dictionary<string, List<string>>(StringComparer.OrdinalIgnoreCase);
            var offset = 0;
            while (Marshal.ReadInt16(source, checked(offset * sizeof(char))) != 0)
            {
                var start = offset;
                var separator = -1;
                short character;
                while ((character = Marshal.ReadInt16(source, checked(offset * sizeof(char)))) != 0)
                {
                    if (character == '=' && offset > start && separator < 0) separator = offset;
                    offset++;
                }
                if (separator < 0) throw new InvalidDataException("The inherited environment block is invalid.");
                var name = Marshal.PtrToStringUni(IntPtr.Add(source, checked(start * sizeof(char))), separator - start)
                    ?? throw new InvalidDataException("The inherited environment name is invalid.");
                if (removals?.Contains(name) != true)
                {
                    entries.Add(new Entry(name, start, offset - start + 1));
                    if (requestedNames?.Contains(name) == true)
                    {
                        if (!inheritedSpellings.TryGetValue(name, out var spellings))
                            inheritedSpellings.Add(name, spellings = new List<string>());
                        spellings.Add(name);
                    }
                }
                offset++;
            }

            entries.Sort(static (left, right) =>
            {
                var comparison = StringComparer.OrdinalIgnoreCase.Compare(left.Name, right.Name);
                return comparison != 0 ? comparison : left.Offset.CompareTo(right.Offset);
            });
            var characters = 1;
            foreach (var entry in entries) characters = checked(characters + entry.Length);
            characters = Math.Max(characters, 2);
            var block = new WindowsChildEnvironmentBlock(checked(characters * sizeof(char)), inheritedSpellings);
            try
            {
                var destinationOffset = 0;
                foreach (var entry in entries)
                {
                    var bytes = checked(entry.Length * sizeof(char));
                    MoveMemory(IntPtr.Add(block.handle, destinationOffset),
                        IntPtr.Add(source, checked(entry.Offset * sizeof(char))), (nuint)bytes);
                    destinationOffset = checked(destinationOffset + bytes);
                }
                Marshal.WriteInt16(block.handle, destinationOffset, 0);
                if (entries.Count == 0) Marshal.WriteInt16(block.handle, sizeof(char), 0);
                return block;
            }
            catch
            {
                block.Dispose();
                throw;
            }
        }
        finally
        {
            _ = FreeEnvironmentStrings(source);
        }
    }

    internal IReadOnlyDictionary<string, string>? MatchInheritedSpellings(IReadOnlyDictionary<string, string>? environment)
    {
        if (environment is null || _inheritedSpellings.Count == 0) return environment;
        var needsRemapping = false;
        foreach (var (name, _) in environment)
        {
            if (_inheritedSpellings.TryGetValue(name, out var spellings)
                && (spellings.Count != 1 || !string.Equals(spellings[0], name, StringComparison.Ordinal)))
            {
                needsRemapping = true;
                break;
            }
        }
        if (!needsRemapping) return environment;

        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        foreach (var (name, value) in environment)
        {
            if (_inheritedSpellings.TryGetValue(name, out var spellings))
            {
                foreach (var spelling in spellings) result[spelling] = value;
            }
            else
            {
                result[name] = value;
            }
        }
        return result;
    }

    protected override bool ReleaseHandle()
    {
        ZeroMemory(handle, (nuint)_byteCount);
        Marshal.FreeHGlobal(handle);
        return true;
    }

    private readonly record struct Entry(string Name, int Offset, int Length);

    [DllImport("kernel32.dll", EntryPoint = "GetEnvironmentStringsW", SetLastError = true)]
    private static extern IntPtr GetEnvironmentStrings();

    [DllImport("kernel32.dll", EntryPoint = "FreeEnvironmentStringsW", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static extern bool FreeEnvironmentStrings(IntPtr environment);

    [DllImport("kernel32.dll", EntryPoint = "RtlMoveMemory")]
    private static extern void MoveMemory(IntPtr destination, IntPtr source, nuint length);

    [DllImport("kernel32.dll", EntryPoint = "RtlZeroMemory")]
    private static extern void ZeroMemory(IntPtr destination, nuint length);
}
