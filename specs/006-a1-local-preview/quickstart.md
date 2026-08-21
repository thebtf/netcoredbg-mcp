# Quickstart: A1 Local Stateless Preview

This is a future local source-run validation guide. It does not build a release
asset, invoke a workflow, publish a package, create a tag, or contact a release
service.

## Prerequisites

1. Build the shared core, compatibility host, preview process, and their focused
   tests as specified by the eventual tasks.
2. Create the deterministic `PreviewSearchApp` fixture root plus contained and
   escaping-link denial fixtures.
3. Keep the installed Python consumer command available for rollback proof.

## Local positive journey

1. Start the preview with exactly `--project <fixture-root>`.
2. Connect a local stdio modern client with all three required metadata fields.
3. Call `server/discover`, `tools/list`, and `find_code_symbol` for a contained
   fixture symbol.
4. Assert exactly one tool, root-relative ordered result, text/structured parity,
   cache metadata, and protocol-only stdout.
5. Close stdin and assert bounded process completion.

## Local denial journey

Exercise invalid root startup, alternate authority inputs, reparse/outside-root
entry, malformed arguments, unreadable/injected I/O, each budget boundary,
unsupported version, excluded tool/method, cancellation, and EOF. Assert only
the contract’s named launch/error outcome, no partial result, and no secret/root
content.

## Compatibility and rollback

1. Run the five named Python/compatibility parity owners and focused core tests.
2. Remove preview selection without touching the Python installation.
3. Replay the installed Python consumer journey and record `PRODUCT_WORKS`.

No preview release or consumer distribution is part of this quickstart.
