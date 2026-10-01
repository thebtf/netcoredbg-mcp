# netcoredbg-mcp v0.23.12

Prepared: 2026-09-27

## Summary

`v0.23.12` is a PATCH release for physical numeric-keypad input in the existing Windows UI tools and nonblocking Python startup artifact maintenance. The published Python server remains the default; the tool catalog stays at 135 tools, 8 prompts, and 4 resources.

## Fixed behavior

- `ui_send_keys`, `ui_send_keys_focused`, and `ui_send_keys_batch` accept `{NUMPAD0}`–`{NUMPAD9}`, `{NUMPADADD}`, `{NUMPADSUBTRACT}`, `{NUMPADMULTIPLY}`, `{NUMPADDIVIDE}`, `{NUMPADDECIMAL}`, `{NUMPADENTER}`, and `{NUMLOCK}` on both Windows FlaUI and pywinauto send paths. The FlaUI-only scoped `ui_key_sequence` accepts the same names without braces in its list.
- These names produce physical keypad key-down/key-up events rather than ordinary text or top-row digits. Keypad Enter and Divide use extended-key events; NumLock uses VK 0x90 / scan 0x45 without the extended flag.
- NumLock is pressed and released, not set to a chosen state. The resulting digit characters depend on the existing NumLock state. Consumer verification restores its original state after an intentional NumLock press.
- The opt-in native scene artifact store retains its capacity charge when expiry cannot delete a locked artifact; it retries cleanup on a later store operation. A failed commit no longer deletes an existing destination that the store did not create.
- Python server construction no longer scans temporary directories. One lifespan-owned subprocess performs an opportunistic sweep of the private artifact namespace. A blocked or failed sweep does not prevent MCP initialization. The worker has a five-second useful-work budget; EOF or cancellation initiates bounded owner drain without waiting for that budget.

## Compatibility

There are no new tool arguments or response shapes, and the Python backend selection remains unchanged. The scoped sequence still requires FlaUI; the opt-in source-only .NET preview is unchanged.

### Artifact-retention compatibility exception

New Python screenshot artifacts live beneath a private current-user namespace. A retained OS lease protects every active owner, including artifacts older than four hours. The public `stop_cleanup_or_stale_gc_after_4h` value is unchanged. After abandonment, each session directory becomes eligible for a later startup sweep only when strictly older than four hours. Four hours is not guaranteed deletion time or an active-artifact expiry. A timed-out or failed sweep is incomplete maintenance; remaining eligible data can be reclaimed on a later start.

Existing unmarked flat `mcp-netcoredbg-*` directories are deliberately preserved. This release does not scan, migrate, adopt, or automatically delete them, so abandoned legacy data may remain indefinitely. This is a material reduction in automatic legacy reclamation, accepted to avoid deleting data whose ownership cannot be established. An already-running old manager can still clean its own exact mapped directory.

Owning-session stop cleanup, normal manager cleanup, atomic raw/crop persistence, returned real paths and hashes, and closed-session fencing retain their existing contract. The separate native scene artifact capability still expires at session stop or 14,400 seconds after commit, whichever comes first. This Python correction does not change that native expiry.

Rollback preserves both layouts without moving or deleting retained evidence. Stop new sessions normally before rollback where possible. The new namespace does not match the old collector prefix; rollback restores the old startup-stall risk, not a defect-free startup path.

## Release gates and residual risks

- Installed-wheel Windows keypad journeys passed: all 17 named keys reached the WPF target, the pywinauto path passed 6/6 manual checks, and the final keypad regression suite passed 82/82. The original NumLock state was restored.
- The mandatory candidate SonarQube scan is **blocked**, not passed: the completed diagnostic at `bc4a89fe2b2f47c6d07f8569aef80b396da00e1f` reports 58.9% new-code coverage against 80% and 726 open findings, with no hotspots. This diagnostic does not authorize merging or publication. The post-merge scan has not run. Do not merge, tag, or publish this release until both exact-head scans satisfy the release protocol; replace this status with final receipts before publication.

---

# netcoredbg-mcp v0.23.11

Prepared: 2026-08-30

## Summary

`v0.23.11` is a PATCH hotfix for Engram #448 black-frame evidence repair.

## Fixed behavior

1. After a foreground transition, ordinary evidence capture reuses the live FlaUI connection.
2. A black PrintWindow capture is discarded for exactly one verified BitBlt alternate.
3. Accepted evidence carries HWND, PID, physical geometry, DPI, stability, and foreground provenance.
4. If the final capture is black, no evidence artifact is persisted and diagnostics are returned.

The existing runtime-smoke safety contract is unchanged: `search_source` now runs regex matching in a bounded dedicated Python subprocess. Source-file enumeration and waiting for that worker remain in the MCP server process. Worker failures are surfaced as tool errors.

## Compatibility

There is no intentional breaking change to the published Python API or CLI.
