# netcoredbg-mcp v0.23.10

Prepared: 2026-08-24

## Summary

`v0.23.10` is a PATCH release that adds fail-closed same-media correlation to
runtime-smoke v2 evidence. It lets a consumer prove that a UI action,
tracepoint, `debug.evaluate`, and acquired app-diagnostic sample belong to the
same media engine and media instance.

## Consumer Claims

1. Runtime-smoke samples now carry a correlation envelope with debug-session,
   debuggee epoch, run/case/transition/action, and available thread/frame
   provenance.
2. Consumers provide media identity only through the documented `correlation`
   payload. The release emits SHA-256 identity fingerprints, not the raw
   correlation identifiers.
3. A requested same-media comparison returns `SAME_MEDIA_INSTANCE` only when
   all required sources have matching execution provenance and media identity.
   Missing, malformed, duplicate, or unequal evidence returns
   `NOT_COMPARABLE`; it cannot support a product-pass claim.
4. App-diagnostic correlation identity is trusted only when it is acquired from
   product JSON. A plan literal is configuration, not evidence.
5. Existing runtime-smoke v2 plans without a correlation policy retain their
   prior behavior.

## Compatibility and Upgrade

There is no intentional breaking change to the published Python API or CLI.

`search_source` now runs regex matching in a bounded dedicated Python subprocess. Source-file enumeration and waiting for that worker remain in the MCP server process. Worker failures are surfaced as tool errors rather than crashing the MCP server process.

Upgrade an existing installation:

```powershell
python -m pip install --upgrade netcoredbg-mcp==0.23.10
# or
pipx upgrade netcoredbg-mcp
```

For a new workstation:

```powershell
pipx install netcoredbg-mcp==0.23.10
netcoredbg-mcp --setup
```

## Known Residuals

- A consumer must explicitly emit the documented media correlation payload;
  netcoredbg-mcp never infers media identity from process, module, workspace,
  or UI state alone.

## Release Evidence

The release report records the exact wheel, installed CLI/import smoke,
correlation regression suite, runtime-smoke consumer proof, review, and
exact-head SonarQube receipts.