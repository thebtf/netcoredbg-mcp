using System.Diagnostics;
using System.Text.RegularExpressions;

namespace NetCoreDbg.Mcp.Stateless.DebugAdapter;

// Calls are serialized by the legacy session gate; output capture uses the buffer's own lock.
internal sealed class LegacyOutputEvidence
{
    private const int MaxCheckpoints = 1024;
    private const int MaxPatterns = 256;
    private const int MaxTextLength = 2 * 1024 * 1024;
    private readonly Dictionary<string, DapOutputBuffer.Position> _checkpoints = new(StringComparer.Ordinal);
    private readonly HashSet<string> _incomplete = new(StringComparer.Ordinal);

    internal void Clear()
    {
        _checkpoints.Clear();
        _incomplete.Clear();
    }

    internal void InvalidateForUnpublishedStart()
    {
        _incomplete.UnionWith(_checkpoints.Keys);
    }

    internal object Checkpoint(string name, DapOutputBuffer? buffer)
    {
        if (name.Length is < 1 or > 64 || name.Any(static c => !char.IsAsciiLetterOrDigit(c) && c is not ('_' or '.' or '-')))
            return new { status = "FAIL", reason = "invalid output checkpoint name", checkpoint = name };
        if (_checkpoints.ContainsKey(name))
            return new { status = "FAIL", reason = "output checkpoint already exists", checkpoint = name, existing_checkpoint = name };
        if (_checkpoints.Count >= MaxCheckpoints)
            return new { status = "FAIL", reason = "output checkpoint limit reached", checkpoint = name };
        var position = buffer?.CapturePosition() ?? new DapOutputBuffer.Position(0, 0, 0);
        _checkpoints.Add(name, position);
        return new
        {
            status = "PASS",
            reason = "output checkpoint created",
            checkpoint = name,
            entry_count = position.EntryCount,
            byte_offset = position.ByteOffset,
            evidence_refs = new[] { new { kind = "output_checkpoint", @ref = $"output:{name}", summary = "output checkpoint created" } }
        };
    }

    internal object Assert(string checkpoint, string[] required, string[] forbidden, bool regex, int maxMatches, DapOutputBuffer? buffer, CancellationToken cancellationToken)
    {
        var timer = Stopwatch.StartNew();
        CheckBudget(timer, cancellationToken);
        if (!_checkpoints.TryGetValue(checkpoint, out var position))
            return new { status = "FAIL", reason = "output checkpoint not found", checkpoint };
        if (_incomplete.Contains(checkpoint))
            return new { status = "FAIL", reason = "output checkpoint range incomplete after unpublished start", checkpoint };
        var range = buffer?.CaptureSince(position, timer, cancellationToken) ?? new DapOutputBuffer.Range(true, string.Empty, 0, 0, null);
        if (!range.Complete)
            return new { status = "FAIL", reason = "output checkpoint range trimmed", checkpoint };
        if (range.Text.Length > MaxTextLength || required.Length + forbidden.Length > MaxPatterns ||
            required.Concat(forbidden).Any(static pattern => pattern.Length > 4096) || maxMatches > 256)
            throw new InvalidOperationException("OUTPUT_ASSERT_TOO_LARGE");
        long evidenceLength = required.Sum(static value => (long)value.Length) + forbidden.Sum(static value => (long)value.Length);
        if (evidenceLength > 256 * 1024) throw new InvalidOperationException("OUTPUT_ASSERT_TOO_LARGE");
        var compiled = new List<Regex>(required.Length + forbidden.Length);
        foreach (var pattern in required.Concat(forbidden))
        {
            CheckBudget(timer, cancellationToken);
            try { compiled.Add(new Regex(regex ? pattern : Regex.Escape(pattern), RegexOptions.CultureInvariant, TimeSpan.FromMilliseconds(100))); }
            catch (ArgumentException ex)
            {
                return new
                {
                    status = "FAIL",
                    reason = "invalid regex",
                    invalid_pattern = pattern,
                    regex_error = ex.Message,
                    skipped_assertions = true,
                    forbidden_matches = Array.Empty<object>()
                };
            }
        }
        var lines = SplitLines(range.Text, timer, cancellationToken);
        var matches = new List<object>();
        var forbiddenMatches = new List<object>();
        var missing = new List<string>();
        var foundForbidden = false;
        try
        {
            for (var i = 0; i < compiled.Count; i++)
            {
                var isRequired = i < required.Length;
                var pattern = isRequired ? required[i] : forbidden[i - required.Length];
                var found = false;
                for (var line = 0; line < lines.Count; line++)
                {
                    CheckBudget(timer, cancellationToken);
                    if (!compiled[i].IsMatch(lines[line])) continue;
                    found = true;
                    if (!isRequired) foundForbidden = true;
                    var evidence = isRequired ? matches : forbiddenMatches;
                    if (evidence.Count < maxMatches)
                    {
                        evidenceLength += pattern.Length + lines[line].Length;
                        if (evidenceLength > 256 * 1024) throw new InvalidOperationException("OUTPUT_ASSERT_TOO_LARGE");
                        evidence.Add(new { pattern, line = line + 1, text = lines[line] });
                    }
                }
                if (isRequired && !found) missing.Add(pattern);
            }
        }
        catch (RegexMatchTimeoutException) { throw new InvalidOperationException("OUTPUT_ASSERT_TIMEOUT"); }
        CheckBudget(timer, cancellationToken);
        var pass = missing.Count == 0 && !foundForbidden;
        return new
        {
            status = pass ? "PASS" : "FAIL",
            reason = pass ? "output assertions passed" : "output assertions failed",
            checkpoint,
            summary = new { checkpoint, matched_line_count = matches.Count, missing_count = missing.Count, forbidden_count = forbiddenMatches.Count },
            searched_range = new
            {
                start_entry = position.EntryCount,
                end_entry = range.EndEntry,
                start_byte = position.ByteOffset,
                end_byte = range.EndByte,
                start_sequence = position.Sequence + 1,
                end_sequence = range.EndSequence,
                line_count = lines.Count
            },
            matches,
            missing_required = missing,
            forbidden_matches = forbiddenMatches,
            evidence_refs = new[] { new { kind = "output_assertion", @ref = $"output:{checkpoint}", summary = $"matched={matches.Count} missing={missing.Count} forbidden={forbiddenMatches.Count}" } }
        };
    }

    internal static void CheckBudget(Stopwatch timer, CancellationToken cancellationToken)
    {
        cancellationToken.ThrowIfCancellationRequested();
        if (timer.Elapsed > TimeSpan.FromSeconds(2)) throw new InvalidOperationException("OUTPUT_ASSERT_TIMEOUT");
    }

    private static List<string> SplitLines(string text, Stopwatch timer, CancellationToken cancellationToken)
    {
        var lines = new List<string>();
        var start = 0;
        for (var i = 0; i < text.Length; i++)
        {
            if ((i & 4095) == 0) CheckBudget(timer, cancellationToken);
            if (text[i] is not ('\n' or '\r' or '\v' or '\f' or '\u001c' or '\u001d' or '\u001e' or '\u0085' or '\u2028' or '\u2029')) continue;
            lines.Add(text[start..i]);
            if (text[i] == '\r' && i + 1 < text.Length && text[i + 1] == '\n') i++;
            start = i + 1;
        }
        CheckBudget(timer, cancellationToken);
        if (start < text.Length) lines.Add(text[start..]);
        return lines;
    }
}
