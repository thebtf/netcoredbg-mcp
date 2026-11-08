using System.Diagnostics;
using System.Globalization;
using System.Text;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal sealed class DapOutputBuffer
{
    private const string TruncationSuffix = "... [truncated]";
    // The legacy environment names say bytes, but both limits count Unicode code points.
    private static readonly long MaximumEntryScalars = ReadLimit("NETCOREDBG_MAX_OUTPUT_ENTRY", 100_000);
    private static readonly long MaximumTotalScalars = ReadLimit("NETCOREDBG_MAX_OUTPUT_BYTES", 10_000_000);
    private readonly object _gate = new();
    private readonly Queue<(string Text, string Category, int Scalars, int Bytes, long Sequence)> _entries = new();
    private long _retainedScalars;
    private long _retainedBytes;
    private long _sequence;
    private long _discardedThrough;

    internal readonly record struct Position(long Sequence, int EntryCount, long ByteOffset);
    internal readonly record struct Range(bool Complete, string Text, int EndEntry, long EndByte, long? EndSequence);

    internal Position CapturePosition()
    {
        lock (_gate)
            return new Position(_sequence, _entries.Count, _retainedBytes);
    }

    internal Range CaptureSince(Position position, Stopwatch timer, CancellationToken cancellationToken)
    {
        LegacyOutputEvidence.CheckBudget(timer, cancellationToken);
        lock (_gate)
        {
            LegacyOutputEvidence.CheckBudget(timer, cancellationToken);
            if (_discardedThrough > position.Sequence)
                return new Range(false, string.Empty, _entries.Count, 0, _entries.Count == 0 ? null : _sequence);
            if (_sequence == position.Sequence)
                return new Range(true, string.Empty, _entries.Count, _retainedBytes, _entries.Count == 0 ? null : _sequence);
            long length = 0;
            foreach (var entry in _entries)
            {
                LegacyOutputEvidence.CheckBudget(timer, cancellationToken);
                if (entry.Sequence > position.Sequence && (length += entry.Text.Length) > 2 * 1024 * 1024)
                    throw new InvalidOperationException("OUTPUT_ASSERT_TOO_LARGE");
            }
            var text = string.Create((int)length, (Entries: _entries, position.Sequence, Timer: timer, Token: cancellationToken), static (destination, state) =>
            {
                var offset = 0;
                foreach (var entry in state.Entries)
                {
                    LegacyOutputEvidence.CheckBudget(state.Timer, state.Token);
                    if (entry.Sequence <= state.Sequence) continue;
                    entry.Text.AsSpan().CopyTo(destination[offset..]);
                    offset += entry.Text.Length;
                }
            });
            LegacyOutputEvidence.CheckBudget(timer, cancellationToken);
            return new Range(true, text, _entries.Count, _retainedBytes, _entries.Count == 0 ? null : _sequence);
        }
    }

    internal bool Append(string text, string category)
    {
        var (retainedText, scalars) = BoundEntry(text);
        if (retainedText.Length == 0)
        {
            return false;
        }
        var bytes = Encoding.UTF8.GetByteCount(retainedText);

        lock (_gate)
        {
            var hadEntries = _entries.Count != 0;
            var sequence = ++_sequence;
            _entries.Enqueue((retainedText, category, scalars, bytes, sequence));
            if (!string.Equals(retainedText, text, StringComparison.Ordinal)) _discardedThrough = sequence;
            _retainedScalars += scalars;
            _retainedBytes += bytes;
            while (_retainedScalars > MaximumTotalScalars && _entries.TryDequeue(out var removed))
            {
                _retainedScalars -= removed.Scalars;
                _retainedBytes -= removed.Bytes;
                _discardedThrough = Math.Max(_discardedThrough, removed.Sequence);
            }
            return hadEntries || _entries.Count != 0;
        }
    }

    internal string Snapshot(bool clear, string? category)
    {
        lock (_gate)
        {
            var length = 0;
            foreach (var entry in _entries)
            {
                if (string.IsNullOrEmpty(category) || string.Equals(entry.Category, category, StringComparison.Ordinal))
                {
                    length = checked(length + entry.Text.Length);
                }
            }

            var snapshot = string.Create(length, (Entries: _entries, Category: category), static (destination, state) =>
            {
                var offset = 0;
                foreach (var entry in state.Entries)
                {
                    if (string.IsNullOrEmpty(state.Category) || string.Equals(entry.Category, state.Category, StringComparison.Ordinal))
                    {
                        entry.Text.AsSpan().CopyTo(destination[offset..]);
                        offset += entry.Text.Length;
                    }
                }
            });

            if (clear && string.IsNullOrEmpty(category))
            {
                if (_entries.Count != 0) _discardedThrough = Math.Max(_discardedThrough, _entries.Last().Sequence);
                _entries.Clear();
                _retainedScalars = 0;
                _retainedBytes = 0;
            }

            return snapshot;
        }
    }

    internal IReadOnlyList<(string Text, string Category)> RecentEntries(int count)
    {
        lock (_gate)
        {
            var result = new List<(string Text, string Category)>(Math.Min(count, _entries.Count));
            var skip = Math.Max(0, _entries.Count - count);
            foreach (var entry in _entries)
            {
                if (skip > 0) { skip--; continue; }
                if (!string.IsNullOrWhiteSpace(entry.Text))
                    result.Add((entry.Text, entry.Category));
            }
            return result;
        }
    }

    internal (long TotalLines, long ReturnedLines, string Output) Tail(long lines, string? category)
    {
        lock (_gate)
        {
            var cursor = new OutputCursor(_entries, category);
            long totalLines = 0;
            long contentLength = 0;
            var unterminatedLine = false;
            while (cursor.MoveNext(out var character))
            {
                if (IsLineBreak(character))
                {
                    totalLines++;
                    unterminatedLine = false;
                }
                else
                {
                    contentLength++;
                    unterminatedLine = true;
                }
            }

            if (unterminatedLine)
            {
                totalLines++;
            }

            var skippedLines = lines > 0
                ? Math.Max(0, totalLines - lines)
                : lines == long.MinValue ? totalLines : Math.Min(totalLines, -lines);
            var returnedLines = totalLines - skippedLines;
            if (returnedLines == 0)
            {
                return (totalLines, 0, string.Empty);
            }

            // Count first, then skip and copy only the selected suffix; no whole-buffer snapshot or line array.
            cursor = new OutputCursor(_entries, category);
            while (skippedLines > 0 && cursor.MoveNext(out var character))
            {
                if (IsLineBreak(character))
                {
                    skippedLines--;
                }
                else
                {
                    contentLength--;
                }
            }

            var length = checked((int)(contentLength + returnedLines - 1));
            var output = string.Create(length, cursor, static (destination, state) =>
            {
                var remaining = state;
                var offset = 0;
                var pendingSeparator = false;
                while (remaining.MoveNext(out var character))
                {
                    // Defer LF until another line starts: a final separator does not create an extra line.
                    if (pendingSeparator)
                    {
                        destination[offset++] = '\n';
                    }

                    pendingSeparator = IsLineBreak(character);
                    if (!pendingSeparator)
                    {
                        destination[offset++] = character;
                    }
                }
            });
            return (totalLines, returnedLines, output);
        }
    }

    private static bool IsLineBreak(char value) => value is
        '\n' or '\r' or '\v' or '\f' or '\u001c' or '\u001d' or '\u001e' or '\u0085' or '\u2028' or '\u2029';

    private struct OutputCursor
    {
        private Queue<(string Text, string Category, int Scalars, int Bytes, long Sequence)>.Enumerator _entries;
        private readonly string? _category;
        private string _text;
        private int _offset;
        private bool _skipLf;

        internal OutputCursor(Queue<(string Text, string Category, int Scalars, int Bytes, long Sequence)> entries, string? category)
        {
            _entries = entries.GetEnumerator();
            _category = category;
            _text = string.Empty;
            _offset = 0;
            _skipLf = false;
        }

        internal bool MoveNext(out char character)
        {
            while (true)
            {
                while (_offset == _text.Length)
                {
                    if (!_entries.MoveNext())
                    {
                        character = default;
                        return false;
                    }

                    var entry = _entries.Current;
                    if (!string.IsNullOrEmpty(_category) && !string.Equals(entry.Category, _category, StringComparison.Ordinal))
                    {
                        continue;
                    }

                    _text = entry.Text;
                    _offset = 0;
                }

                var next = _text[_offset++];
                // CRLF is one separator even across events, including fragments made adjacent by filtering.
                if (_skipLf && next == '\n')
                {
                    _skipLf = false;
                    continue;
                }

                _skipLf = next == '\r';
                character = next;
                return true;
            }
        }
    }

    private static (string Text, int Scalars) BoundEntry(string text)
    {
        var limit = MaximumEntryScalars;
        if (limit < 0)
        {
            // Preserve Python's negative slice endpoint rather than adding a configuration restriction.
            var total = 0;
            foreach (var _ in text.EnumerateRunes())
            {
                total++;
            }

            limit = Math.Max(0, limit + total);
        }

        var scalars = 0;
        var characters = 0;
        foreach (var rune in text.EnumerateRunes())
        {
            if (scalars == limit)
            {
                return (string.Concat(text.AsSpan(0, characters), TruncationSuffix), scalars + TruncationSuffix.Length);
            }

            scalars++;
            characters += rune.Utf16SequenceLength;
        }

        return MaximumEntryScalars < 0
            ? (string.Concat(text, TruncationSuffix), scalars + TruncationSuffix.Length)
            : (text, scalars);
    }

    private static long ReadLimit(string name, long defaultValue)
    {
        var configured = Environment.GetEnvironmentVariable(name);
        if (configured is null)
        {
            return defaultValue;
        }

        var value = configured.AsSpan().Trim();
        var negative = false;
        if (!value.IsEmpty && value[0] is '+' or '-')
        {
            negative = value[0] == '-';
            value = value[1..];
        }

        var maximum = negative ? (ulong)long.MaxValue + 1 : (ulong)long.MaxValue;
        var maximumPrefix = maximum / 10;
        var maximumLastDigit = maximum % 10;
        ulong magnitude = 0;
        var previousWasDigit = false;
        foreach (var rune in value.EnumerateRunes())
        {
            if (rune.Value == '_' && previousWasDigit)
            {
                previousWasDigit = false;
                continue;
            }

            var digit = rune.Value - '0';
            if ((uint)digit > 9)
            {
                if (Rune.GetUnicodeCategory(rune) != UnicodeCategory.DecimalDigitNumber)
                {
                    throw new FormatException($"{name} must be a decimal integer.");
                }

                digit = (int)Rune.GetNumericValue(rune);
            }

            previousWasDigit = true;
            // Saturate without allocating a big integer, but still validate every remaining digit and separator.
            magnitude = magnitude > maximumPrefix || (magnitude == maximumPrefix && (uint)digit > maximumLastDigit)
                ? maximum
                : magnitude * 10 + (uint)digit;
        }

        if (!previousWasDigit)
        {
            throw new FormatException($"{name} must be a decimal integer.");
        }

        if (!negative)
        {
            return (long)magnitude;
        }

        return magnitude == maximum ? long.MinValue : -(long)magnitude;
    }
}
