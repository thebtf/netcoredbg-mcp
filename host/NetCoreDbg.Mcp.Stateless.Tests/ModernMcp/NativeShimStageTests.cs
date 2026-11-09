using System.Buffers.Binary;
using System.Reflection;
using System.Runtime.InteropServices;
using System.Text;
using Microsoft.Win32.SafeHandles;
using System.Security.Cryptography;
using System.Text.Json;
using NetCoreDbg.Mcp.Stateless.Tests.DebugAdapter;
using Xunit;

namespace NetCoreDbg.Mcp.Stateless.Tests.ModernMcp;

// PE32/PE32+ headers and one initialized section, with a distinct payload per fixture.
internal static class NativeShimPeFixture
{
    internal static byte[] Create(bool x64, byte marker, int length = 1024)
    {
        var bytes = new byte[Math.Max(length, 1024)];
        void U16(int offset, ushort value) => BinaryPrimitives.WriteUInt16LittleEndian(bytes.AsSpan(offset), value);
        void U32(int offset, uint value) => BinaryPrimitives.WriteUInt32LittleEndian(bytes.AsSpan(offset), value);
        U16(0, 0x5a4d);
        U32(0x3c, 0x80);
        U32(0x80, 0x00004550);
        U16(0x84, x64 ? (ushort)0x8664 : (ushort)0x014c);
        U16(0x86, 1);
        U16(0x94, x64 ? (ushort)0xf0 : (ushort)0xe0);
        U16(0x96, 0x2102);
        var optional = 0x98;
        U16(optional, x64 ? (ushort)0x20b : (ushort)0x10b);
        U32(optional + 4, 0x200);
        U32(optional + 16, 0x1000);
        U32(optional + 20, 0x1000);
        if (!x64) U32(optional + 28, 0x10000000);
        else U32(optional + 24, 0x10000000);
        U32(optional + 32, 0x1000);
        U32(optional + 36, 0x200);
        U16(optional + 40, 6);
        U16(optional + 48, 6);
        U32(optional + 56, 0x2000);
        U32(optional + 60, 0x200);
        U16(optional + 68, 3);
        U32(optional + (x64 ? 108 : 92), 16);
        var section = optional + (x64 ? 0xf0 : 0xe0);
        bytes[section] = (byte)'.'; bytes[section + 1] = (byte)'t'; bytes[section + 2] = (byte)'e'; bytes[section + 3] = (byte)'x'; bytes[section + 4] = (byte)'t';
        U32(section + 8, 0x200); U32(section + 12, 0x1000);
        U32(section + 16, 0x200); U32(section + 20, 0x200); U32(section + 36, 0x60000020);
        bytes[0x200] = marker;
        return bytes;
    }
}

[Collection(NetCoreDbgSessionProcessCollection.Name)]
[Trait("Coverage", "Exclude")]
public sealed class NativeShimStageTests
{
    private static readonly Type StageType = Assembly.LoadFrom(TestOutputPathResolver.ResolveManagedAssembly(
        RepositoryLayout.Root, Path.Combine("host", "NetCoreDbg.Mcp.Stateless"), "NetCoreDbg.Mcp.Stateless"))
        .GetType("NetCoreDbg.Mcp.Stateless.NativeShimStage", throwOnError: true)!;
    private const BindingFlags StaticPrivate = BindingFlags.Static | BindingFlags.NonPublic;
    private const BindingFlags InstancePrivate = BindingFlags.Instance | BindingFlags.NonPublic;

    [Fact]
    public void Cleanup_RejectsIdenticalReplacementOfStageRoot()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var stage = fixture.Prepare();
        var root = Root(stage);
        var original = root + "-original";
        try { Directory.Move(root, original); }
        catch (IOException)
        {
            Assert.True(Directory.Exists(root));
            Assert.False(Directory.Exists(original));
            Cleanup(stage);
            return;
        }
        CopyTree(original, root);
        var replacementFile = Path.Combine(root, "netcoredbg", "dbgshim.dll");
        var replacementBytes = File.ReadAllBytes(replacementFile);
        var failure = Assert.Throws<TargetInvocationException>(() => Cleanup(stage));
        Assert.IsAssignableFrom<IOException>(failure.InnerException);
        Assert.True(Directory.Exists(root));
        Assert.Equal(replacementBytes, File.ReadAllBytes(replacementFile));
        Assert.True(Directory.Exists(original));
    }

    [Fact]
    public void Cleanup_RejectsReplacementBetweenInspectionAndDeletion()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var stage = fixture.Prepare();
        string? replaced = null;
        string? original = null;
        var blocked = false;
        Hook("BeforeDeleteForTest", (Action<string>)(path =>
        {
            if (replaced is not null) return;
            replaced = path;
            original = path + ".original";
            try { File.Move(path, original); }
            catch (IOException) { blocked = true; throw; }
            File.Copy(original, path);
        }));
        try
        {
            var failure = Assert.Throws<TargetInvocationException>(() => Cleanup(stage));
            Assert.IsAssignableFrom<IOException>(failure.InnerException);
            Assert.NotNull(replaced);
            Assert.True(File.Exists(replaced));
            if (blocked) Assert.False(File.Exists(original));
            else
            {
                Assert.True(File.Exists(original));
                Assert.Equal(File.ReadAllBytes(original!), File.ReadAllBytes(replaced!));
            }
        }
        finally
        {
            Hook("BeforeDeleteForTest", null);
            if (!blocked && original is not null && replaced is not null && File.Exists(original))
            {
                File.Delete(replaced);
                File.Move(original, replaced);
            }
            Cleanup(stage);
        }
    }

    [Fact]
    public void CancelledPartialWrite_CanRetryCleanupOfSameOwnedFile()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture(largeExecutable: true);
        using var cancellation = new CancellationTokenSource();
        string? partial = null;
        var rejectCleanup = true;
        Hook("AfterWriteBlockForTest", (Action<string, int>)((path, written) =>
        {
            if (path.EndsWith("netcoredbg.exe", StringComparison.OrdinalIgnoreCase) && written == 81920)
            {
                partial = path;
                cancellation.Cancel();
            }
        }));
        Hook("BeforeCleanupForTest", (Action<string>)(_ =>
        {
            if (rejectCleanup) { rejectCleanup = false; throw new IOException("Controlled cleanup failure."); }
        }));
        try
        {
            var failure = Assert.Throws<TargetInvocationException>(() => fixture.InvokePrepare(cancellation.Token));
            var cleanupFailure = Assert.IsAssignableFrom<IOException>(failure.InnerException);
            Assert.Equal("NativeShimStageCleanupException", cleanupFailure.GetType().Name);
            var stage = cleanupFailure.GetType().GetProperty("Stage", InstancePrivate)!.GetValue(cleanupFailure)!;
            Assert.NotNull(partial);
            Assert.True(File.Exists(partial));
            Assert.Equal(81920, new FileInfo(partial!).Length);
            var root = Root(stage);
            Cleanup(stage);
            Assert.False(Directory.Exists(root));
            Cleanup(stage);
        }
        finally { Hook("AfterWriteBlockForTest", null); Hook("BeforeCleanupForTest", null); }
    }

    [Fact]
    public void CancelledPartialWrite_RejectsIdenticalReplacementOnRetry()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture(largeExecutable: true);
        using var cancellation = new CancellationTokenSource();
        string? partial = null;
        string? original = null;
        string? retained = null;
        object? stage = null;
        var originalMoved = false;
        var replacementCreated = false;
        Exception? primaryFailure = null;
        fixture.PreserveScratch = true;
        Hook("AfterWriteBlockForTest", (Action<string, int>)((path, written) =>
        {
            if (path.EndsWith("netcoredbg.exe", StringComparison.OrdinalIgnoreCase) && written == 81920)
            {
                partial = path;
                cancellation.Cancel();
            }
        }));
        Hook("BeforeCleanupForTest", (Action<string>)(_ => throw new IOException("Controlled cleanup failure.")));
        try
        {
            var failure = Assert.Throws<TargetInvocationException>(() => fixture.InvokePrepare(cancellation.Token));
            var cleanupFailure = Assert.IsAssignableFrom<IOException>(failure.InnerException);
            Assert.Equal("NativeShimStageCleanupException", cleanupFailure.GetType().Name);
            stage = cleanupFailure.GetType().GetProperty("Stage", InstancePrivate)!.GetValue(cleanupFailure)!;
            Assert.NotNull(partial);
            original = partial! + ".original";
            try { File.Move(partial!, original); }
            catch (IOException)
            {
                Assert.True(File.Exists(partial));
                Assert.False(File.Exists(original));
                return;
            }
            originalMoved = true;
            File.Copy(original, partial!);
            replacementCreated = true;
            retained = Path.Combine(fixture.Scratch, "retained-partial");
            File.Move(original, retained);
            Hook("BeforeCleanupForTest", null);
            var retry = Assert.Throws<TargetInvocationException>(() => Cleanup(stage));
            Assert.IsAssignableFrom<IOException>(retry.InnerException);
            Assert.True(File.Exists(partial));
            Assert.Equal(File.ReadAllBytes(retained), File.ReadAllBytes(partial!));
        }
        catch (Exception exception) { primaryFailure = exception; throw; }
        finally
        {
            Hook("AfterWriteBlockForTest", null);
            Hook("BeforeCleanupForTest", null);
            try
            {
                if (stage is not null)
                {
                    if (originalMoved)
                    {
                        var ownedOriginal = retained is not null && File.Exists(retained) ? retained : original!;
                        if (replacementCreated)
                        {
                            if (!File.ReadAllBytes(partial!).AsSpan().SequenceEqual(File.ReadAllBytes(ownedOriginal)))
                                throw new IOException("Test replacement changed; scratch must be retained.");
                            File.Delete(partial!);
                        }
                        File.Move(ownedOriginal, partial!);
                    }
                    var root = Root(stage);
                    Cleanup(stage);
                    if (Directory.Exists(root)) throw new IOException("Stage cleanup did not remove its root.");
                    fixture.PreserveScratch = primaryFailure is not null;
                }
            }
            catch (Exception cleanupFailure)
            {
                fixture.PreserveScratch = true;
                var context = new IOException($"Stage cleanup unconfirmed; scratch retained at '{fixture.Scratch}'.", cleanupFailure);
                throw primaryFailure is null ? context : new AggregateException("Test and owned-stage cleanup failed.", primaryFailure, context);
            }
        }
    }

    [Fact]
    public void BufferedWriteFailure_WithPartialSideEffect_CleansSameOwnedPrefixOnRetry()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture(largeExecutable: true);
        string? partial = null;
        Hook("AfterBufferedWriteForTest", (Action<FileStream, string, int>)((stream, path, written) =>
        {
            if (!path.EndsWith("netcoredbg.exe", StringComparison.OrdinalIgnoreCase) || written != 81920) return;
            stream.Flush();
            stream.SetLength(40960);
            partial = path;
            throw new IOException("Controlled flush failure after a partial side effect.");
        }));
        Hook("BeforeCleanupForTest", (Action<string>)(_ => throw new IOException("Controlled cleanup failure.")));
        try
        {
            var failure = Assert.Throws<TargetInvocationException>(() => fixture.InvokePrepare(CancellationToken.None));
            var cleanupFailure = Assert.IsAssignableFrom<IOException>(failure.InnerException);
            Assert.Equal("NativeShimStageCleanupException", cleanupFailure.GetType().Name);
            var stage = cleanupFailure.GetType().GetProperty("Stage", InstancePrivate)!.GetValue(cleanupFailure)!;
            Assert.NotNull(partial);
            Assert.Equal(NativeShimPeFixture.Create(true, 0x11, 180000).AsSpan(0, 40960).ToArray(), ReadShared(partial!));
            Hook("BeforeCleanupForTest", null);
            var retry = Record.Exception(() => Cleanup(stage));
            if (retry is not null)
            {
                fixture.PreserveScratch = true;
                throw new Xunit.Sdk.XunitException($"Same-file prefix cleanup failed; scratch retained at '{fixture.Scratch}': {retry}");
            }
            Assert.False(Directory.Exists(Root(stage)));
        }
        finally { Hook("AfterBufferedWriteForTest", null); Hook("BeforeCleanupForTest", null); }
    }

    [Fact]
    public void Prepare_RejectsOrPreventsManagedParentReplacementBeforeRootCreate()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var original = Path.Combine(fixture.Scratch, "retained-managed");
        string? replacement = null;
        var blocked = false;
        object? stage = null;
        Hook("BeforeRootCreateForTest", (Action<string>)(managed =>
        {
            try { Directory.Move(managed, original); }
            catch (IOException) { blocked = true; return; }
            Directory.CreateDirectory(managed);
            replacement = managed;
            File.WriteAllText(Path.Combine(managed, "foreign-sentinel"), "preserve");
        }));
        try
        {
            stage = fixture.InvokePrepare(CancellationToken.None);
            if (blocked)
            {
                Assert.NotNull(stage);
                Assert.False(Directory.Exists(original));
            }
            else
            {
                Assert.NotNull(replacement);
                Assert.Equal("preserve", File.ReadAllText(Path.Combine(replacement!, "foreign-sentinel")));
                Assert.Empty(Directory.EnumerateDirectories(replacement!, ".native-shim-stage-*"));
                Assert.Null(stage);
            }
        }
        finally
        {
            Hook("BeforeRootCreateForTest", null);
            if (stage is not null)
            {
                var cleanup = Record.Exception(() => Cleanup(stage));
                if (cleanup is not null) fixture.PreserveScratch = true;
            }
        }
    }

    [Fact]
    public void UnsupportedManagedParentIdentity_SkipsBeforeCreatingStageAndReleasesPins()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        object? stage = null;
        var managed = Path.Combine(fixture.Scratch, "home", ".netcoredbg-mcp");
        Hook("BeforeIdentityQueryForTest", (Action<SafeFileHandle>)(handle =>
        {
            if (HandlePath(handle).EndsWith(@"\.netcoredbg-mcp", StringComparison.OrdinalIgnoreCase))
                throw new IOException("Controlled unavailable parent file ID.");
        }));
        try
        {
            stage = fixture.InvokePrepare(CancellationToken.None);
            Assert.Null(stage);
            Assert.Empty(Directory.EnumerateDirectories(managed, ".native-shim-stage-*"));
            var moved = Path.Combine(fixture.Scratch, "managed-after-fallback");
            Directory.Move(managed, moved);
            Directory.Move(moved, managed);
        }
        finally
        {
            Hook("BeforeIdentityQueryForTest", null);
            if (stage is not null)
            {
                try { Cleanup(stage); }
                catch { fixture.PreserveScratch = true; throw; }
            }
        }
    }

    [Fact]
    public void UnsupportedNewRootIdentity_RollsBackEmptyOwnedRootWithoutIdentity()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        object? stage = null;
        Hook("BeforeIdentityQueryForTest", (Action<SafeFileHandle>)(handle =>
        {
            if (Path.GetFileName(HandlePath(handle)).StartsWith(".native-shim-stage-", StringComparison.OrdinalIgnoreCase))
                throw new IOException("Controlled unavailable root file ID.");
        }));
        try
        {
            var failure = Record.Exception(() => stage = fixture.InvokePrepare(CancellationToken.None));
            if (failure is TargetInvocationException { InnerException: IOException inner } &&
                inner.GetType().Name == "NativeShimStageCleanupException")
                stage = inner.GetType().GetProperty("Stage", InstancePrivate)!.GetValue(inner);
            Assert.Null(failure);
            Assert.Null(stage);
            var managed = Path.Combine(fixture.Scratch, "home", ".netcoredbg-mcp");
            Assert.Empty(Directory.EnumerateDirectories(managed, ".native-shim-stage-*"));
        }
        finally
        {
            Hook("BeforeIdentityQueryForTest", null);
            if (stage is not null)
            {
                try { Cleanup(stage); }
                catch { fixture.PreserveScratch = true; throw; }
            }
        }
    }

    [Fact]
    public void UnsupportedNewRootIdentity_WithUnknownEntry_RetainsOwnerUntilSafeRetry()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        string? unknown = null;
        object? stage = null;
        Hook("BeforeIdentityQueryForTest", (Action<SafeFileHandle>)(handle =>
        {
            var path = HandlePath(handle);
            if (!Path.GetFileName(path).StartsWith(".native-shim-stage-", StringComparison.OrdinalIgnoreCase)) return;
            if (unknown is null)
            {
                unknown = Path.Combine(path, "foreign-sentinel");
                File.WriteAllText(unknown, "preserve");
            }
            throw new IOException("Controlled unavailable root file ID.");
        }));
        try
        {
            var failure = Assert.Throws<TargetInvocationException>(() => fixture.InvokePrepare(CancellationToken.None));
            var cleanupFailure = Assert.IsAssignableFrom<IOException>(failure.InnerException);
            Assert.Equal("NativeShimStageCleanupException", cleanupFailure.GetType().Name);
            stage = cleanupFailure.GetType().GetProperty("Stage", InstancePrivate)!.GetValue(cleanupFailure)!;
            Assert.NotNull(unknown);
            Assert.Equal("preserve", File.ReadAllText(unknown));
        }
        finally
        {
            Hook("BeforeIdentityQueryForTest", null);
            if (stage is not null)
            {
                try
                {
                    if (unknown is not null && File.Exists(unknown)) File.Delete(unknown);
                    var root = Root(stage);
                    Cleanup(stage);
                    Assert.False(Directory.Exists(root));
                }
                catch { fixture.PreserveScratch = true; throw; }
            }
        }
    }

    [Fact]
    public void PrepareAndCleanup_SupportsLongOwnedStagePaths()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture(longHome: true);
        var stage = fixture.Prepare();
        var root = Root(stage);
        var receipt = Path.Combine(root, ".native-debugger-manifest.json");
        Assert.True(receipt.Length > 260);
        Assert.True(File.Exists(receipt));
        Cleanup(stage);
        Assert.False(Directory.Exists(root));
    }

    [Fact]
    public void CacheSelection_IgnoresHigherX86AndStagesLowerX64()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture();
        var x64 = NativeShimPeFixture.Create(true, 0x31);
        fixture.Cache("12345.7.9", x64);
        fixture.Cache("12345.7.10", NativeShimPeFixture.Create(false, 0x32));
        var stage = fixture.Prepare();
        // Ordinary FileShare.Read is required by the .NET apphost, not just loader-style delete sharing.
        try { Assert.Equal(x64, File.ReadAllBytes(Path.Combine(Path.GetDirectoryName(Executable(stage))!, "dbgshim.dll"))); }
        finally { Cleanup(stage); }
    }

    [Fact]
    public void CacheSelection_X86OnlyLeavesOriginalUntouched()
    {
        if (!OperatingSystem.IsWindows()) return;
        using var fixture = new Fixture(defaultCache: false);
        fixture.Cache("12345.7.10", NativeShimPeFixture.Create(false, 0x32));
        var before = File.ReadAllBytes(fixture.OriginalShim);
        Assert.Null(fixture.InvokePrepare(CancellationToken.None));
        Assert.Equal(before, File.ReadAllBytes(fixture.OriginalShim));
    }

    private static byte[] ReadShared(string path)
    {
        using var stream = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
        using var buffer = new MemoryStream();
        stream.CopyTo(buffer);
        return buffer.ToArray();
    }
    private static string HandlePath(SafeFileHandle handle)
    {
        var buffer = new StringBuilder(32768);
        var length = GetFinalPathNameByHandle(handle, buffer, (uint)buffer.Capacity, 0);
        if (length == 0 || length >= buffer.Capacity) throw new IOException("Could not inspect test handle path.");
        return buffer.ToString();
    }
    [DllImport("kernel32.dll", EntryPoint = "GetFinalPathNameByHandleW", CharSet = CharSet.Unicode, SetLastError = true)]
    private static extern uint GetFinalPathNameByHandle(SafeFileHandle file, StringBuilder path, uint length, uint flags);

    private static void Hook(string name, Delegate? value) => StageType.GetField(name, StaticPrivate)!.SetValue(null, value);
    private static string Root(object stage) => (string)StageType.GetProperty("RootPath", InstancePrivate)!.GetValue(stage)!;
    private static string Executable(object stage) => (string)StageType.GetProperty("ExecutablePath", InstancePrivate)!.GetValue(stage)!;
    private static void Cleanup(object stage) => StageType.GetMethod("Cleanup", InstancePrivate)!.Invoke(stage, null);
    private static void CopyTree(string source, string destination)
    {
        Directory.CreateDirectory(destination);
        foreach (var file in Directory.EnumerateFiles(source)) File.Copy(file, Path.Combine(destination, Path.GetFileName(file)));
        foreach (var child in Directory.EnumerateDirectories(source)) CopyTree(child, Path.Combine(destination, Path.GetFileName(child)));
    }

    private sealed class Fixture : IDisposable
    {
        private readonly string _scratch = ModernMcpScratchDirectory.Create();
        private readonly string? _previousHome = Environment.GetEnvironmentVariable("USERPROFILE");
        private readonly string _home;
        private readonly string _executable;
        private readonly string _program;
        internal bool PreserveScratch { get; set; }
        internal string Scratch => _scratch;
        internal string OriginalShim { get; }

        internal Fixture(bool largeExecutable = false, bool defaultCache = true, bool longHome = false)
        {
            _home = longHome ? Path.Combine(_scratch, new string('a', 70), new string('b', 70), new string('c', 70), "home") : Path.Combine(_scratch, "home");
            var install = Path.Combine(_home, ".netcoredbg-mcp", "netcoredbg");
            var bin = Path.Combine(install, "netcoredbg");
            Directory.CreateDirectory(bin);
            _executable = Path.Combine(bin, "netcoredbg.exe");
            OriginalShim = Path.Combine(bin, "dbgshim.dll");
            File.WriteAllBytes(_executable, NativeShimPeFixture.Create(true, 0x11, largeExecutable ? 180000 : 1024));
            File.WriteAllBytes(OriginalShim, NativeShimPeFixture.Create(true, 0x12));
            var files = Directory.GetFiles(install, "*", SearchOption.AllDirectories).Select(path =>
            {
                var bytes = File.ReadAllBytes(path);
                return new { path = Path.GetRelativePath(install, path).Replace('\\', '/'), size = bytes.Length, sha256 = Convert.ToHexString(SHA256.HashData(bytes)).ToLowerInvariant() };
            }).OrderBy(item => item.path, StringComparer.Ordinal).ToArray();
            File.WriteAllText(Path.Combine(install, ".native-debugger-manifest.json"), JsonSerializer.Serialize(new
            {
                schema = "netcoredbg-mcp.native-debugger-install/1",
                asset = "netcoredbg-win64.zip",
                archiveSha256 = new string('0', 64),
                files,
            }));
            _program = Path.Combine(_scratch, "target.dll");
            File.WriteAllBytes(_program, []);
            File.WriteAllText(Path.ChangeExtension(_program, ".runtimeconfig.json"), "{\"runtimeOptions\":{\"framework\":{\"name\":\"Microsoft.NETCore.App\",\"version\":\"12345.0.0\"}}}");
            if (defaultCache) Cache("12345.7.8", NativeShimPeFixture.Create(true, 0x20));
            Environment.SetEnvironmentVariable("USERPROFILE", _home);
        }
        internal void Cache(string version, byte[] content)
        {
            var path = Path.Combine(_home, ".netcoredbg-mcp", "dbgshim", version, "dbgshim.dll");
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            File.WriteAllBytes(path, content);
        }
        internal object? InvokePrepare(CancellationToken token) => StageType.GetMethod("Prepare", StaticPrivate)!.Invoke(null, [_executable, _program, token]);
        internal object Prepare() => InvokePrepare(CancellationToken.None) ?? throw new Xunit.Sdk.XunitException("Expected a prepared stage.");
        public void Dispose()
        {
            Environment.SetEnvironmentVariable("USERPROFILE", _previousHome);
            if (!PreserveScratch) Directory.Delete(_scratch, recursive: true);
        }
    }
}
