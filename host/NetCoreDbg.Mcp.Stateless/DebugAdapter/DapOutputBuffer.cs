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
    private readonly Queue<(string Text, string Category, int Scalars)> _entries = new();
    private long _retainedScalars;

    internal void Append(string text, string category)
    {
        var (retainedText, scalars) = BoundEntry(text);
        if (retainedText.Length == 0)
        {
            return;
        }

        lock (_gate)
        {
            _entries.Enqueue((retainedText, category, scalars));
            _retainedScalars += scalars;
            while (_retainedScalars > MaximumTotalScalars && _entries.TryDequeue(out var removed))
            {
                _retainedScalars -= removed.Scalars;
            }
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
                _entries.Clear();
                _retainedScalars = 0;
            }

            return snapshot;
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
