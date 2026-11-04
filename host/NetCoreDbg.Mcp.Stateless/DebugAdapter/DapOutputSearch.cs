using System.Diagnostics;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

internal enum DapOutputSearchStatus { Success, InvalidPattern, Timeout, TooLarge }

internal sealed record DapOutputSearchResult(DapOutputSearchStatus Status, string? SerializedContent);

internal static class DapOutputSearch
{
    internal const int MaximumSerializedBytes = 256 * 1024;
    private const int MaximumMatches = 50;
    private static readonly TimeSpan RegexTimeout = TimeSpan.FromMilliseconds(100);
    private static readonly TimeSpan TotalTimeout = TimeSpan.FromSeconds(2);

    internal static DapOutputSearchResult Search(string snapshot, string pattern, long contextLines, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (pattern.Length > MaximumSerializedBytes - 2)
        {
            return new(DapOutputSearchStatus.TooLarge, null);
        }

        var budget = new WorkBudget(cancellationToken);
        Regex regex;
        try
        {
            regex = new Regex(pattern, RegexOptions.IgnoreCase | RegexOptions.CultureInvariant, RegexTimeout);
        }
        catch (RegexParseException)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return new(DapOutputSearchStatus.InvalidPattern, null);
        }

        try
        {
            budget.Check();
            var selected = new List<Line>(MaximumMatches);
            var cursor = new LineCursor(snapshot, budget);
            var totalLines = 0;
            var matchCount = 0;
            while (cursor.MoveNext(out var line))
            {
                budget.Check();
                if (regex.IsMatch(snapshot.AsSpan(line.Start, line.Length)))
                {
                    matchCount++;
                    if (selected.Count < MaximumMatches)
                    {
                        selected.Add(line);
                    }
                }
                totalLines++;
            }

            budget.Check();
            long minimumBytes = 0;
            CheckStringSize(pattern.Length, ref minimumBytes);
            var matches = new List<SearchMatch>(selected.Count);
            foreach (var line in selected)
            {
                CheckStringSize(line.Length, ref minimumBytes);
                // Python clamps explicit slice bounds after handling a negative end.
                var start = Int128.Max(0, (Int128)line.Index - contextLines);
                var end = Int128.Min(totalLines, (Int128)line.Index + contextLines + 1);
                if (end < 0)
                {
                    end += totalLines;
                }
                matches.Add(new SearchMatch(line,
                    (int)Int128.Clamp(start, 0, totalLines),
                    (int)Int128.Clamp(end, 0, totalLines)));
            }

            var pending = matches.Where(static match => match.Start < match.End)
                .OrderBy(static match => match.Start).ToArray();
            var active = new List<SearchMatch>(pending.Length);
            var next = 0;
            var contextCursor = new LineCursor(snapshot, budget);
            while ((next < pending.Length || active.Count > 0) && contextCursor.MoveNext(out var context))
            {
                while (next < pending.Length && pending[next].Start <= context.Index)
                {
                    active.Add(pending[next++]);
                }
                for (var index = active.Count - 1; index >= 0; index--)
                {
                    var match = active[index];
                    if (context.Index >= match.End)
                    {
                        active.RemoveAt(index);
                    }
                    else
                    {
                        CheckStringSize(context.Length, ref minimumBytes);
                        match.Context.Add(context);
                    }
                }
            }

            using var stream = new MemoryStream();
            using var writer = new Utf8JsonWriter(stream);
            writer.WriteStartObject();
            writer.WriteString("kind", "output_search_success");
            writer.WriteString("pattern", pattern);
            writer.WriteNumber("matchCount", matchCount);
            writer.WriteStartArray("matches");
            CheckOutput(writer, stream, budget);

            foreach (var match in matches)
            {
                budget.Check();
                writer.WriteStartObject();
                writer.WriteNumber("lineNumber", match.Line.Index + 1);
                writer.WriteString("match", snapshot.AsSpan(match.Line.Start, match.Line.Length));
                writer.WriteStartArray("context");
                CheckOutput(writer, stream, budget);
                foreach (var context in match.Context)
                {
                    writer.WriteStringValue(snapshot.AsSpan(context.Start, context.Length));
                    CheckOutput(writer, stream, budget);
                }
                writer.WriteEndArray();
                writer.WriteEndObject();
                CheckOutput(writer, stream, budget);
            }

            writer.WriteEndArray();
            writer.WriteEndObject();
            CheckOutput(writer, stream, budget);
            var content = Encoding.UTF8.GetString(stream.GetBuffer(), 0, checked((int)stream.Length));
            budget.Check();
            return new(DapOutputSearchStatus.Success, content);
        }
        catch (RegexMatchTimeoutException)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return new(DapOutputSearchStatus.Timeout, null);
        }
        catch (SearchTimeoutException)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return new(DapOutputSearchStatus.Timeout, null);
        }
        catch (SearchTooLargeException)
        {
            cancellationToken.ThrowIfCancellationRequested();
            return new(DapOutputSearchStatus.TooLarge, null);
        }
    }

    private static void CheckStringSize(int length, ref long minimumBytes)
    {
        // Every UTF-16 code unit requires at least one JSON UTF-8 byte, plus quotes.
        minimumBytes += (long)length + 2;
        if (minimumBytes > MaximumSerializedBytes)
        {
            throw new SearchTooLargeException();
        }
    }

    private static void CheckOutput(Utf8JsonWriter writer, MemoryStream stream, WorkBudget budget)
    {
        budget.Check();
        writer.Flush();
        if (stream.Length > MaximumSerializedBytes)
        {
            throw new SearchTooLargeException();
        }
    }

    private readonly record struct Line(int Index, int Start, int Length);

    private sealed class SearchMatch(Line line, int start, int end)
    {
        internal Line Line { get; } = line;
        internal int Start { get; } = start;
        internal int End { get; } = end;
        internal List<Line> Context { get; } = new();
    }

    private struct LineCursor(string text, WorkBudget budget)
    {
        private int _offset;
        private int _index;

        internal bool MoveNext(out Line line)
        {
            budget.Check();
            if (_offset == text.Length)
            {
                line = default;
                return false;
            }

            var start = _offset;
            while (_offset < text.Length && !IsLineBreak(text[_offset]))
            {
                _offset++;
                if ((_offset & 4095) == 0)
                {
                    budget.Check();
                }
            }

            line = new Line(_index++, start, _offset - start);
            if (_offset < text.Length)
            {
                var separator = text[_offset++];
                if (separator == '\r' && _offset < text.Length && text[_offset] == '\n')
                {
                    _offset++;
                }
            }
            return true;
        }
    }

    private static bool IsLineBreak(char value) => value is
        '\n' or '\r' or '\v' or '\f' or '\u001c' or '\u001d' or '\u001e' or '\u0085' or '\u2028' or '\u2029';

    private sealed class WorkBudget(CancellationToken cancellationToken)
    {
        private readonly Stopwatch _watch = Stopwatch.StartNew();

        internal void Check()
        {
            cancellationToken.ThrowIfCancellationRequested();
            if (_watch.Elapsed >= TotalTimeout)
            {
                throw new SearchTimeoutException();
            }
        }
    }

    private sealed class SearchTimeoutException : Exception { }
    private sealed class SearchTooLargeException : Exception { }
}
