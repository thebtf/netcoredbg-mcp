using System.Buffers;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using Microsoft.Win32.SafeHandles;

namespace NetCoreDbg.Mcp.Stateless.NativeScene;

internal sealed class NativeSceneArtifactStore : IAsyncDisposable
{
    private const int InternalChunkBytes = 65_536;
    private const int MaximumReadBytes = 65_536;
    private const int MaximumSceneArtifactBytes = 16_777_216;
    private const int MaximumRasterArtifactBytes = 67_108_864;
    private const int DefaultMaximumArtifactCount = 256;
    private const long DefaultMaximumAggregateBytes = 268_435_456;
    private static readonly TimeSpan Retention = TimeSpan.FromHours(4);
    private static readonly NativeSceneArtifactReadError Unavailable = new(
        "ARTIFACT_NOT_FOUND",
        "Artifact is not available.");
    private static readonly NativeSceneArtifactReadError IntegrityFailed = new(
        "ARTIFACT_INTEGRITY_FAILED",
        "Artifact integrity verification failed.");

    private readonly SemaphoreSlim _gate = new(1, 1);
    private readonly TimeProvider _timeProvider;
    private readonly int _maximumArtifactCount;
    private readonly long _maximumAggregateBytes;
    private readonly string _ownedRoot;
    private readonly Dictionary<string, ArtifactSession> _sessions = new(StringComparer.Ordinal);
    private readonly Dictionary<string, NativeSceneArtifactStaging> _staged = new(StringComparer.Ordinal);
    private readonly Dictionary<string, CommittedArtifact> _artifacts = new(StringComparer.Ordinal);
    private ITimer? _expiryTimer;
    private long _aggregateBytes;
    private bool _disposed;

    internal NativeSceneArtifactStore(string root, TimeProvider timeProvider)
        : this(root, timeProvider, DefaultMaximumArtifactCount, DefaultMaximumAggregateBytes)
    {
    }

    internal NativeSceneArtifactStore(
        string root,
        TimeProvider timeProvider,
        int maximumArtifactCount,
        long maximumAggregateBytes)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(root);
        ArgumentNullException.ThrowIfNull(timeProvider);
        if (maximumArtifactCount <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maximumArtifactCount));
        }

        if (maximumAggregateBytes <= 0)
        {
            throw new ArgumentOutOfRangeException(nameof(maximumAggregateBytes));
        }

        Directory.CreateDirectory(root);
        _timeProvider = timeProvider;
        _maximumArtifactCount = maximumArtifactCount;
        _maximumAggregateBytes = maximumAggregateBytes;
        _ownedRoot = CreateOwnedRoot(root);
        _expiryTimer = _timeProvider.CreateTimer(
            static state => ((NativeSceneArtifactStore)state!).ExpireFromTimer(),
            this,
            Timeout.InfiniteTimeSpan,
            Timeout.InfiniteTimeSpan);
    }

    internal async Task<NativeSceneArtifactStaging> StageAsync(
        string debugSessionId,
        string captureId,
        string mediaType,
        string artifactSchemaVersion,
        ReadOnlyMemory<byte> bytes,
        CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(debugSessionId);
        ArgumentException.ThrowIfNullOrWhiteSpace(captureId);
        ArgumentException.ThrowIfNullOrWhiteSpace(mediaType);
        ArgumentException.ThrowIfNullOrWhiteSpace(artifactSchemaVersion);
        if (bytes.Length > GetMaximumArtifactBytes(mediaType))
        {
            throw new ArgumentOutOfRangeException(nameof(bytes));
        }

        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            ThrowIfDisposed();
            var now = _timeProvider.GetUtcNow();
            PruneExpiredArtifacts(now);
            ScheduleExpiryTimer(now);
            EnsureCapacityFor(bytes.Length);

            var session = GetOrCreateSession(debugSessionId);
            var artifactId = CreateArtifactId();
            var stagingPath = Path.Combine(session.StagingDirectory, artifactId);
            var staged = new NativeSceneArtifactStaging(
                this,
                session,
                artifactId,
                captureId,
                mediaType,
                artifactSchemaVersion,
                bytes.Length,
                stagingPath);

            try
            {
                await WriteStagedBytesAsync(stagingPath, bytes, cancellationToken).ConfigureAwait(false);
                _staged.Add(artifactId, staged);
                session.StagedArtifactIds.Add(artifactId);
                _aggregateBytes += staged.ByteLength;
                return staged;
            }
            catch
            {
                TryDeleteFile(stagingPath);
                RemoveEmptySession(session);
                throw;
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task<NativeSceneArtifactReadResult> ReadAsync(
        string debugSessionId,
        string artifactId,
        long offset,
        int maxBytes,
        CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(debugSessionId);
        ArgumentException.ThrowIfNullOrWhiteSpace(artifactId);
        if (offset < 0)
        {
            throw new ArgumentOutOfRangeException(nameof(offset));
        }

        if (maxBytes is < 1 or > MaximumReadBytes)
        {
            throw new ArgumentOutOfRangeException(nameof(maxBytes));
        }

        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (_disposed ||
                !_artifacts.TryGetValue(artifactId, out var artifact) ||
                !StringComparer.Ordinal.Equals(artifact.Session.DebugSessionId, debugSessionId))
            {
                return Unavailable;
            }

            if (artifact.ExpiresAt <= _timeProvider.GetUtcNow())
            {
                ExpireArtifact(artifact);
                ScheduleExpiryTimer(_timeProvider.GetUtcNow());
                return Unavailable;
            }

            if (offset > artifact.Descriptor.ByteLength)
            {
                throw new ArgumentOutOfRangeException(nameof(offset));
            }

            if (artifact.IsContained)
            {
                return IntegrityFailed;
            }

            FileStream stream;
            try
            {
                stream = new FileStream(
                    artifact.Path,
                    FileMode.Open,
                    FileAccess.Read,
                    FileShare.ReadWrite | FileShare.Delete,
                    InternalChunkBytes,
                    FileOptions.Asynchronous | FileOptions.RandomAccess);
            }
            catch (FileNotFoundException)
            {
                ExpireArtifact(artifact);
                return Unavailable;
            }
            catch (DirectoryNotFoundException)
            {
                ExpireArtifact(artifact);
                return Unavailable;
            }
            catch (UnauthorizedAccessException)
            {
                ExpireArtifact(artifact);
                return Unavailable;
            }
            catch (IOException)
            {
                ExpireArtifact(artifact);
                return Unavailable;
            }

            await using (stream.ConfigureAwait(false))
            {
                try
                {
                    var bytesToRead = (int)Math.Min(
                        (long)maxBytes,
                        artifact.Descriptor.ByteLength - offset);
                    var verifiedBytes = await ReadVerifiedRangeAsync(
                        artifact,
                        stream,
                        offset,
                        bytesToRead,
                        cancellationToken).ConfigureAwait(false);
                    if (verifiedBytes is null)
                    {
                        artifact.IsContained = true;
                        return IntegrityFailed;
                    }

                    return new NativeSceneArtifactReadChunk(
                        artifact.Descriptor,
                        offset,
                        bytesToRead,
                        bytesToRead == 0 ? string.Empty : Convert.ToBase64String(verifiedBytes),
                        offset + bytesToRead == artifact.Descriptor.ByteLength);
                }
                catch (EndOfStreamException)
                {
                    artifact.IsContained = true;
                    return IntegrityFailed;
                }
                catch (IOException)
                {
                    artifact.IsContained = true;
                    return IntegrityFailed;
                }
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task StopSessionAsync(string debugSessionId, CancellationToken cancellationToken)
    {
        ArgumentException.ThrowIfNullOrWhiteSpace(debugSessionId);

        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (_disposed || !_sessions.TryGetValue(debugSessionId, out var session))
            {
                return;
            }

            ExpireSession(session);
            ScheduleExpiryTimer(_timeProvider.GetUtcNow());
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task AbortAsync(NativeSceneArtifactStaging staged, CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (_disposed ||
                staged.CommitResult is not null ||
                !_staged.TryGetValue(staged.ArtifactId, out var activeStaging) ||
                !ReferenceEquals(activeStaging, staged))
            {
                return;
            }

            TryDeleteFile(staged.StagingPath);
            _staged.Remove(staged.ArtifactId);
            staged.Session.StagedArtifactIds.Remove(staged.ArtifactId);
            _aggregateBytes -= staged.ByteLength;
            staged.Complete(NativeSceneArtifactCommitResult.WriteFailed());
            RemoveEmptySession(staged.Session);
        }
        finally
        {
            _gate.Release();
        }
    }

    public async ValueTask DisposeAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_disposed)
            {
                return;
            }

            _disposed = true;
            _expiryTimer?.Dispose();
            _expiryTimer = null;
            foreach (var staged in _staged.Values)
            {
                staged.Complete(NativeSceneArtifactCommitResult.WriteFailed());
            }

            _staged.Clear();
            _artifacts.Clear();
            _sessions.Clear();
            _aggregateBytes = 0;
            TryDeleteDirectory(_ownedRoot);
        }
        finally
        {
            _gate.Release();
        }
    }

    internal async Task<NativeSceneArtifactCommitResult> CommitAsync(
        NativeSceneArtifactStaging staged,
        CancellationToken cancellationToken)
    {
        await _gate.WaitAsync(cancellationToken).ConfigureAwait(false);
        try
        {
            if (staged.CommitResult is { } priorResult)
            {
                return priorResult;
            }

            if (_disposed ||
                !_staged.TryGetValue(staged.ArtifactId, out var activeStaging) ||
                !ReferenceEquals(activeStaging, staged))
            {
                return staged.Complete(NativeSceneArtifactCommitResult.WriteFailed());
            }

            var committedPath = Path.Combine(staged.Session.CommittedDirectory, staged.ArtifactId);
            try
            {
                cancellationToken.ThrowIfCancellationRequested();
                File.Move(staged.StagingPath, committedPath, overwrite: false);
                await using var stream = new FileStream(
                    committedPath,
                    FileMode.Open,
                    FileAccess.Read,
                    FileShare.Read,
                    InternalChunkBytes,
                    FileOptions.Asynchronous | FileOptions.SequentialScan);
                var digest = await DescribeCommittedFileAsync(stream, committedPath, cancellationToken).ConfigureAwait(false);
                if (digest.ByteLength > GetMaximumArtifactBytes(staged.MediaType) ||
                    digest.ByteLength - staged.ByteLength > _maximumAggregateBytes - _aggregateBytes)
                {
                    throw new IOException("Committed artifact exceeds its storage ceiling.");
                }

                var descriptor = new NativeSceneArtifactDescriptor(
                    staged.ArtifactId,
                    staged.CaptureId,
                    staged.MediaType,
                    digest.ByteLength,
                    digest.Sha256,
                    staged.ArtifactSchemaVersion);
                var artifact = new CommittedArtifact(
                    staged.Session,
                    committedPath,
                    descriptor,
                    digest.Identity,
                    digest.ChunkHashes,
                    _timeProvider.GetUtcNow() + Retention);

                _staged.Remove(staged.ArtifactId);
                staged.Session.StagedArtifactIds.Remove(staged.ArtifactId);
                _artifacts.Add(staged.ArtifactId, artifact);
                staged.Session.CommittedArtifactIds.Add(staged.ArtifactId);
                _aggregateBytes += digest.ByteLength - staged.ByteLength;
                ScheduleExpiryTimer(_timeProvider.GetUtcNow());
                return staged.Complete(NativeSceneArtifactCommitResult.Succeeded(descriptor));
            }
            catch (OperationCanceledException)
            {
                FailStaging(staged, committedPath);
                throw;
            }
            catch (IOException)
            {
                return FailStaging(staged, committedPath);
            }
            catch (UnauthorizedAccessException)
            {
                return FailStaging(staged, committedPath);
            }
        }
        finally
        {
            _gate.Release();
        }
    }

    private static async Task WriteStagedBytesAsync(
        string path,
        ReadOnlyMemory<byte> bytes,
        CancellationToken cancellationToken)
    {
        await using var stream = new FileStream(
            path,
            FileMode.CreateNew,
            FileAccess.Write,
            FileShare.None,
            InternalChunkBytes,
            FileOptions.Asynchronous | FileOptions.SequentialScan);
        await stream.WriteAsync(bytes, cancellationToken).ConfigureAwait(false);
        await stream.FlushAsync(cancellationToken).ConfigureAwait(false);
        stream.Flush(flushToDisk: true);
    }

    private static async Task<CommittedArtifactDigest> DescribeCommittedFileAsync(
        FileStream stream,
        string path,
        CancellationToken cancellationToken)
    {
        var identity = GetFileIdentity(stream, path);
        var byteLength = stream.Length;
        var chunkHashes = new List<byte[]>();
        using var fullHash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        using var chunkHash = IncrementalHash.CreateHash(HashAlgorithmName.SHA256);
        var buffer = ArrayPool<byte>.Shared.Rent(InternalChunkBytes);
        try
        {
            stream.Position = 0;
            long remaining = byteLength;
            while (remaining > 0)
            {
                var chunkLength = (int)Math.Min(remaining, InternalChunkBytes);
                await ReadExactlyAsync(stream, buffer.AsMemory(0, chunkLength), cancellationToken).ConfigureAwait(false);
                fullHash.AppendData(buffer, 0, chunkLength);
                chunkHash.AppendData(buffer, 0, chunkLength);
                chunkHashes.Add(chunkHash.GetHashAndReset());
                remaining -= chunkLength;
            }

            return new CommittedArtifactDigest(
                byteLength,
                Convert.ToHexString(fullHash.GetHashAndReset()).ToLowerInvariant(),
                identity,
                CopyChunkHashes(chunkHashes));
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(buffer, clearArray: true);
        }
    }

    private static async Task<byte[]?> ReadVerifiedRangeAsync(
        CommittedArtifact artifact,
        FileStream stream,
        long offset,
        int bytesToRead,
        CancellationToken cancellationToken)
    {
        if (stream.Length != artifact.Descriptor.ByteLength ||
            !artifact.Identity.Equals(GetFileIdentity(stream, artifact.Path)))
        {
            return null;
        }

        if (bytesToRead == 0)
        {
            return Array.Empty<byte>();
        }

        var firstChunk = checked((int)(offset / InternalChunkBytes));
        var lastChunk = checked((int)((offset + bytesToRead - 1) / InternalChunkBytes));
        if (firstChunk < 0 || lastChunk >= artifact.ChunkHashes.Length)
        {
            return null;
        }

        var range = new byte[bytesToRead];
        var buffer = ArrayPool<byte>.Shared.Rent(InternalChunkBytes);
        try
        {
            var requestedEnd = offset + bytesToRead;
            for (var index = firstChunk; index <= lastChunk; index++)
            {
                var chunkOffset = (long)index * InternalChunkBytes;
                var chunkLength = (int)Math.Min(InternalChunkBytes, artifact.Descriptor.ByteLength - chunkOffset);
                stream.Position = chunkOffset;
                await ReadExactlyAsync(stream, buffer.AsMemory(0, chunkLength), cancellationToken).ConfigureAwait(false);
                var actualHash = SHA256.HashData(buffer.AsSpan(0, chunkLength));
                if (!CryptographicOperations.FixedTimeEquals(actualHash, artifact.ChunkHashes[index]))
                {
                    return null;
                }

                var copyStart = Math.Max(offset, chunkOffset);
                var copyEnd = Math.Min(requestedEnd, chunkOffset + chunkLength);
                var copyLength = checked((int)(copyEnd - copyStart));
                buffer.AsSpan(checked((int)(copyStart - chunkOffset)), copyLength)
                    .CopyTo(range.AsSpan(checked((int)(copyStart - offset)), copyLength));
            }

            return range;
        }
        finally
        {
            ArrayPool<byte>.Shared.Return(buffer, clearArray: true);
        }
    }

    private static async Task ReadExactlyAsync(Stream stream, Memory<byte> destination, CancellationToken cancellationToken)
    {
        var totalRead = 0;
        while (totalRead < destination.Length)
        {
            var read = await stream.ReadAsync(destination[totalRead..], cancellationToken).ConfigureAwait(false);
            if (read == 0)
            {
                throw new EndOfStreamException();
            }

            totalRead += read;
        }
    }

    private static ArtifactFileIdentity GetFileIdentity(FileStream stream, string path)
    {
        if (OperatingSystem.IsWindows())
        {
            if (stream.SafeFileHandle.IsInvalid ||
                !NativeMethods.GetFileInformationByHandle(stream.SafeFileHandle, out var information))
            {
                throw new IOException("Unable to inspect committed artifact identity.");
            }

            return new ArtifactFileIdentity(
                information.VolumeSerialNumber,
                ((ulong)information.FileIndexHigh << 32) | information.FileIndexLow,
                0,
                0);
        }

        var file = new FileInfo(path);
        file.Refresh();
        if (!file.Exists)
        {
            throw new FileNotFoundException("Committed artifact is unavailable.", path);
        }

        return new ArtifactFileIdentity(0, 0, file.CreationTimeUtc.Ticks, file.LastWriteTimeUtc.Ticks);
    }

    private static byte[][] CopyChunkHashes(List<byte[]> hashes)
    {
        var copy = new byte[hashes.Count][];
        for (var index = 0; index < hashes.Count; index++)
        {
            copy[index] = hashes[index].AsSpan().ToArray();
        }

        return copy;
    }

    private NativeSceneArtifactCommitResult FailStaging(NativeSceneArtifactStaging staged, string committedPath)
    {
        TryDeleteFile(staged.StagingPath);
        TryDeleteFile(committedPath);
        if (_staged.Remove(staged.ArtifactId))
        {
            _aggregateBytes -= staged.ByteLength;
            staged.Session.StagedArtifactIds.Remove(staged.ArtifactId);
        }

        RemoveEmptySession(staged.Session);
        return staged.Complete(NativeSceneArtifactCommitResult.WriteFailed());
    }

    private void ExpireArtifact(CommittedArtifact artifact)
    {
        if (_artifacts.Remove(artifact.Descriptor.ArtifactId))
        {
            _aggregateBytes -= artifact.Descriptor.ByteLength;
            artifact.Session.CommittedArtifactIds.Remove(artifact.Descriptor.ArtifactId);
        }

        TryDeleteFile(artifact.Path);
        RemoveEmptySession(artifact.Session);
    }

    private void ExpireSession(ArtifactSession session)
    {
        foreach (var artifactId in session.StagedArtifactIds.ToArray())
        {
            if (_staged.Remove(artifactId, out var staged))
            {
                _aggregateBytes -= staged.ByteLength;
                TryDeleteFile(staged.StagingPath);
                staged.Complete(NativeSceneArtifactCommitResult.WriteFailed());
            }
        }

        foreach (var artifactId in session.CommittedArtifactIds.ToArray())
        {
            if (_artifacts.Remove(artifactId, out var artifact))
            {
                _aggregateBytes -= artifact.Descriptor.ByteLength;
                TryDeleteFile(artifact.Path);
            }
        }

        session.StagedArtifactIds.Clear();
        session.CommittedArtifactIds.Clear();
        _sessions.Remove(session.DebugSessionId);
        TryDeleteDirectory(session.Root);
    }

    private ArtifactSession GetOrCreateSession(string debugSessionId)
    {
        if (_sessions.TryGetValue(debugSessionId, out var existing))
        {
            return existing;
        }

        var root = Path.Combine(_ownedRoot, CreateOpaqueId());
        var stagingDirectory = Path.Combine(root, "staging");
        var committedDirectory = Path.Combine(root, "committed");
        Directory.CreateDirectory(stagingDirectory);
        Directory.CreateDirectory(committedDirectory);
        var session = new ArtifactSession(debugSessionId, root, stagingDirectory, committedDirectory);
        _sessions.Add(debugSessionId, session);
        return session;
    }

    private string CreateArtifactId()
    {
        string artifactId;
        do
        {
            artifactId = CreateOpaqueId();
        }
        while (_staged.ContainsKey(artifactId) || _artifacts.ContainsKey(artifactId));

        return artifactId;
    }

    private static string CreateOwnedRoot(string root)
    {
        var ownedRoot = Path.Combine(root, ".native-scene-artifact-store-" + CreateOpaqueId());
        Directory.CreateDirectory(ownedRoot);
        return ownedRoot;
    }

    private static string CreateOpaqueId()
    {
        Span<byte> bytes = stackalloc byte[32];
        try
        {
            RandomNumberGenerator.Fill(bytes);
            return Convert.ToBase64String(bytes).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        }
        finally
        {
            CryptographicOperations.ZeroMemory(bytes);
        }
    }

    private static int GetMaximumArtifactBytes(string mediaType) => mediaType switch
    {
        "application/vnd.netcoredbg.native-scene+json" => MaximumSceneArtifactBytes,
        "image/png" or "image/webp" => MaximumRasterArtifactBytes,
        _ => throw new ArgumentOutOfRangeException(nameof(mediaType)),
    };
    private void EnsureCapacityFor(int byteLength)
    {
        if (_staged.Count + _artifacts.Count >= _maximumArtifactCount ||
            byteLength > _maximumAggregateBytes - _aggregateBytes)
        {
            throw new IOException("Artifact store capacity is exhausted.");
        }
    }

    private void ExpireFromTimer() => _ = PruneExpiredFromTimerAsync();

    private async Task PruneExpiredFromTimerAsync()
    {
        await _gate.WaitAsync().ConfigureAwait(false);
        try
        {
            if (_disposed)
            {
                return;
            }

            var now = _timeProvider.GetUtcNow();
            PruneExpiredArtifacts(now);
            ScheduleExpiryTimer(now);
        }
        finally
        {
            _gate.Release();
        }
    }

    private void PruneExpiredArtifacts(DateTimeOffset now)
    {
        foreach (var artifact in _artifacts.Values.Where(artifact => artifact.ExpiresAt <= now).ToArray())
        {
            ExpireArtifact(artifact);
        }
    }

    private void ScheduleExpiryTimer(DateTimeOffset now)
    {
        if (_disposed || _expiryTimer is null)
        {
            return;
        }

        if (_artifacts.Count == 0)
        {
            _expiryTimer.Change(Timeout.InfiniteTimeSpan, Timeout.InfiniteTimeSpan);
            return;
        }

        var deadline = _artifacts.Values.Min(static artifact => artifact.ExpiresAt);
        _expiryTimer.Change(
            deadline <= now ? TimeSpan.Zero : deadline - now,
            Timeout.InfiniteTimeSpan);
    }

    private void RemoveEmptySession(ArtifactSession session)
    {
        if (session.StagedArtifactIds.Count != 0 || session.CommittedArtifactIds.Count != 0)
        {
            return;
        }

        _sessions.Remove(session.DebugSessionId);
        TryDeleteDirectory(session.Root);
    }

    private void ThrowIfDisposed()
    {
        if (_disposed)
        {
            throw new ObjectDisposedException(nameof(NativeSceneArtifactStore));
        }
    }

    private static void TryDeleteFile(string path)
    {
        try
        {
            File.Delete(path);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }

    private static void TryDeleteDirectory(string path)
    {
        try
        {
            Directory.Delete(path, recursive: true);
        }
        catch (IOException)
        {
        }
        catch (UnauthorizedAccessException)
        {
        }
    }

    internal sealed class ArtifactSession
    {
        internal ArtifactSession(string debugSessionId, string root, string stagingDirectory, string committedDirectory)
        {
            DebugSessionId = debugSessionId;
            Root = root;
            StagingDirectory = stagingDirectory;
            CommittedDirectory = committedDirectory;
        }

        internal string DebugSessionId { get; }

        internal string Root { get; }

        internal string StagingDirectory { get; }

        internal string CommittedDirectory { get; }

        internal HashSet<string> StagedArtifactIds { get; } = new(StringComparer.Ordinal);

        internal HashSet<string> CommittedArtifactIds { get; } = new(StringComparer.Ordinal);
    }

    private sealed class CommittedArtifact
    {
        internal CommittedArtifact(
            ArtifactSession session,
            string path,
            NativeSceneArtifactDescriptor descriptor,
            ArtifactFileIdentity identity,
            byte[][] chunkHashes,
            DateTimeOffset expiresAt)
        {
            Session = session;
            Path = path;
            Descriptor = descriptor;
            Identity = identity;
            ChunkHashes = chunkHashes;
            ExpiresAt = expiresAt;
        }

        internal ArtifactSession Session { get; }

        internal string Path { get; }

        internal NativeSceneArtifactDescriptor Descriptor { get; }

        internal ArtifactFileIdentity Identity { get; }

        internal byte[][] ChunkHashes { get; }

        internal DateTimeOffset ExpiresAt { get; }

        internal bool IsContained { get; set; }
    }

    private sealed class CommittedArtifactDigest
    {
        internal CommittedArtifactDigest(
            long byteLength,
            string sha256,
            ArtifactFileIdentity identity,
            byte[][] chunkHashes)
        {
            ByteLength = byteLength;
            Sha256 = sha256;
            Identity = identity;
            ChunkHashes = chunkHashes;
        }

        internal long ByteLength { get; }

        internal string Sha256 { get; }

        internal ArtifactFileIdentity Identity { get; }

        internal byte[][] ChunkHashes { get; }
    }

    private readonly record struct ArtifactFileIdentity(
        ulong VolumeSerialNumber,
        ulong FileIndex,
        long CreationTimeUtcTicks,
        long LastWriteTimeUtcTicks);

    [StructLayout(LayoutKind.Sequential)]
    private struct ByHandleFileInformation
    {
        internal uint FileAttributes;
        internal FileTime CreationTime;
        internal FileTime LastAccessTime;
        internal FileTime LastWriteTime;
        internal uint VolumeSerialNumber;
        internal uint FileSizeHigh;
        internal uint FileSizeLow;
        internal uint NumberOfLinks;
        internal uint FileIndexHigh;
        internal uint FileIndexLow;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct FileTime
    {
        internal uint LowDateTime;
        internal uint HighDateTime;
    }

    private static class NativeMethods
    {
        [DllImport("kernel32.dll", SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        internal static extern bool GetFileInformationByHandle(
            SafeFileHandle file,
            out ByHandleFileInformation information);
    }
}

internal sealed class NativeSceneArtifactStaging
{
    private readonly NativeSceneArtifactStore _store;

    internal NativeSceneArtifactStaging(
        NativeSceneArtifactStore store,
        NativeSceneArtifactStore.ArtifactSession session,
        string artifactId,
        string captureId,
        string mediaType,
        string artifactSchemaVersion,
        int byteLength,
        string stagingPath)
    {
        _store = store;
        Session = session;
        ArtifactId = artifactId;
        CaptureId = captureId;
        MediaType = mediaType;
        ArtifactSchemaVersion = artifactSchemaVersion;
        ByteLength = byteLength;
        StagingPath = stagingPath;
    }

    public string ArtifactId { get; }

    internal NativeSceneArtifactStore.ArtifactSession Session { get; }

    internal string CaptureId { get; }

    internal string MediaType { get; }

    internal string ArtifactSchemaVersion { get; }
    internal int ByteLength { get; }

    internal string StagingPath { get; }

    internal NativeSceneArtifactCommitResult? CommitResult { get; private set; }

    internal Task<NativeSceneArtifactCommitResult> CommitAsync(CancellationToken cancellationToken) =>
        _store.CommitAsync(this, cancellationToken);
    internal Task AbortAsync(CancellationToken cancellationToken) =>
        _store.AbortAsync(this, cancellationToken);

    internal NativeSceneArtifactCommitResult Complete(NativeSceneArtifactCommitResult result) =>
        CommitResult ??= result;
}

internal sealed class NativeSceneArtifactCommitResult
{
    private NativeSceneArtifactCommitResult(
        NativeSceneArtifactDescriptor? descriptor,
        string? code,
        string? message)
    {
        Descriptor = descriptor;
        Code = code;
        Message = message;
    }

    public NativeSceneArtifactDescriptor? Descriptor { get; }

    public string? Code { get; }

    public string? Message { get; }

    internal static NativeSceneArtifactCommitResult Succeeded(NativeSceneArtifactDescriptor descriptor) =>
        new(descriptor, null, null);

    internal static NativeSceneArtifactCommitResult WriteFailed() =>
        new(null, "ARTIFACT_WRITE_FAILED", "Artifact could not be committed.");
}

internal sealed class NativeSceneArtifactDescriptor
{
    internal NativeSceneArtifactDescriptor(
        string artifactId,
        string captureId,
        string mediaType,
        long byteLength,
        string sha256,
        string artifactSchemaVersion)
    {
        ArtifactId = artifactId;
        CaptureId = captureId;
        MediaType = mediaType;
        ByteLength = byteLength;
        Sha256 = sha256;
        ArtifactSchemaVersion = artifactSchemaVersion;
    }

    public string ArtifactId { get; }

    public string CaptureId { get; }

    public string MediaType { get; }

    public long ByteLength { get; }

    public string Sha256 { get; }

    public string ArtifactSchemaVersion { get; }
}

internal abstract class NativeSceneArtifactReadResult
{
}

internal sealed class NativeSceneArtifactReadChunk : NativeSceneArtifactReadResult
{
    internal NativeSceneArtifactReadChunk(
        NativeSceneArtifactDescriptor descriptor,
        long offset,
        int bytesRead,
        string dataBase64,
        bool endOfArtifact)
    {
        ArtifactId = descriptor.ArtifactId;
        Offset = offset;
        BytesRead = bytesRead;
        DataBase64 = dataBase64;
        EndOfArtifact = endOfArtifact;
        MediaType = descriptor.MediaType;
        ByteLength = descriptor.ByteLength;
        Sha256 = descriptor.Sha256;
        ArtifactSchemaVersion = descriptor.ArtifactSchemaVersion;
    }

    public string Kind => "capture_artifact_chunk";

    public string ArtifactId { get; }

    public long Offset { get; }

    public int BytesRead { get; }

    public string DataBase64 { get; }

    public bool EndOfArtifact { get; }

    public string MediaType { get; }

    public long ByteLength { get; }

    public string Sha256 { get; }

    public string ArtifactSchemaVersion { get; }
}

internal sealed class NativeSceneArtifactReadError : NativeSceneArtifactReadResult
{
    internal NativeSceneArtifactReadError(string code, string message)
    {
        Code = code;
        Message = message;
    }

    public string Kind => "tool_error";

    public string Tool => "read_capture_artifact";

    public string Code { get; }

    public string Message { get; }
}
