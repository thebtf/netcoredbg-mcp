# netcoredbg-mcp v0.23.12

Prepared: 2026-09-27

## Summary

`v0.23.12` is a PATCH release for physical numeric-keypad input in the existing Windows UI tools and nonblocking Python startup artifact maintenance. The published Python server remains the default; the tool catalog stays at 135 tools, 8 prompts, and 4 resources.

## Fixed behavior

- `ui_send_keys`, `ui_send_keys_focused`, and `ui_send_keys_batch` accept `{NUMPAD0}`–`{NUMPAD9}`, `{NUMPADADD}`, `{NUMPADSUBTRACT}`, `{NUMPADMULTIPLY}`, `{NUMPADDIVIDE}`, `{NUMPADDECIMAL}`, `{NUMPADENTER}`, and `{NUMLOCK}` on both Windows FlaUI and pywinauto send paths. The FlaUI-only scoped `ui_key_sequence` accepts the same names without braces in its list.
- These names produce physical keypad key-down/key-up events rather than ordinary text or top-row digits. Keypad Enter and Divide use extended-key events; NumLock uses VK 0x90 / scan 0x45 without the extended flag.
- NumLock is pressed and released, not set to a chosen state. The resulting digit characters depend on the existing NumLock state. Consumer verification restores its original state after an intentional NumLock press.
- The opt-in native scene artifact store retains its capacity charge when deletion of an expired or aborted artifact is blocked. Its existing timer now retries retained paths after the lock clears without another store operation, while preserving earlier live-artifact expiry deadlines. A failed commit does not delete an existing destination that the store did not create.
- Guarded child resolution now treats unsupported optional UIA AutomationId and Name properties as absent instead of refusing an otherwise valid scene. Process identity, unique matching, HWND ownership, physical containment, and two-read stability checks remain mandatory.
- Python server construction no longer scans temporary directories. One lifespan-owned subprocess performs an opportunistic sweep of the private artifact namespace. A blocked or failed sweep does not prevent MCP initialization. The worker has a five-second useful-work budget; EOF or cancellation initiates bounded owner drain without waiting for that budget.
- Source/developer .NET native-scene bridges launch inside dedicated Windows kill-on-close Jobs. Bridge cleanup preserves the debugger/debuggee, releases owned descendants, and retains the first failure across independent probe, artifact, and session cleanup. Windows adapter/build ownership also requires retained process-lifetime evidence, not zero Job accounting alone, before declaring an owned tree drained.

## Compatibility

There are no new tool arguments or response shapes, and the Python backend selection remains unchanged. The scoped sequence still requires FlaUI. Source/developer .NET fixes do not promote the opt-in preview or change the published Python default.

### Artifact-retention compatibility exception

New Python screenshot artifacts live beneath a private current-user namespace. A retained OS lease protects every active owner, including artifacts older than four hours. The public `stop_cleanup_or_stale_gc_after_4h` value is unchanged. After abandonment, each session directory becomes eligible for a later startup sweep only when strictly older than four hours. Four hours is not guaranteed deletion time or an active-artifact expiry. A timed-out or failed sweep is incomplete maintenance; remaining eligible data can be reclaimed on a later start.

Existing unmarked flat `mcp-netcoredbg-*` directories are deliberately preserved. This release does not scan, migrate, adopt, or automatically delete them, so abandoned legacy data may remain indefinitely. This is a material reduction in automatic legacy reclamation, accepted to avoid deleting data whose ownership cannot be established. An already-running old manager can still clean its own exact mapped directory.

Owning-session stop cleanup, normal manager cleanup, atomic raw/crop persistence, returned real paths and hashes, and closed-session fencing retain their existing contract. The separate native scene artifact capability still expires at session stop or 14,400 seconds after commit, whichever comes first. This Python correction does not change that native expiry.

Rollback preserves both layouts without moving or deleting retained evidence. Stop new sessions normally before rollback where possible. The new namespace does not match the old collector prefix; rollback restores the old startup-stall risk, not a defect-free startup path.

## Release gates and residual risks

- The current installed Python keypad journey passes all 17 named keys, grouped keypad and ordinary-versus-extended Enter, parse-error modifier cleanup and subsequent input. NumLock and the original foreground window are restored, and both owned processes are dead after cleanup. The consumer observes fixture startup separately, uses the existing public foreground-activation tool, and completes target/status observations before setting and verifying current focus immediately before dispatch; native ownership guards and exact event expectations remain unchanged. All 51 SendKeys regressions and direct 17-token success-event comparisons also pass. This is the Python keypad journey, not a passing Sonar receipt or promotion of the source/developer .NET preview.
- No passing exact-head Sonar receipt exists for the current candidate. The completed diagnostic at `dcbdcab14e6f2fe36c1095e9f4b000b2e4e2ff4d` records 70.0% new-code coverage against the unchanged 80% requirement, 13,246 new lines to cover, 488 OPEN findings and zero hotspots. All coverage producers and owned scratch cleanup passed, but the coverage condition is `ERROR` and `release_gate` is null. Independent component checks preserve all 60 native test files and all 199 mapped product paths, but show zero Python test components: inclusion patterns excluded those files from source without indexing them as tests. This incomplete classification correction is not accepted; standard scanner test ownership is being repaired without changing providers, quality profiles or release thresholds. Ten preselected source findings are explicitly FIXED on this head; disappearing test findings are not claimed as source fixes. A passing final candidate scan and the separate actual post-merge scan remain required before tagging; replace this disclosure with final receipts before publication.
- The clean accepted `06f75d0` source has a freshly built and installed wheel, with all 162 packaged members matching the accepted source and installation. Its normal self-contained Windows x64 single-file FlaUI publisher, clean WPF fixture and fresh installed CLI/MCP surface are verified. The actual default FlaUI keypad journey passes all 17 named tokens across `ui_send_keys`, `ui_send_keys_focused`, `ui_send_keys_batch` and `ui_key_sequence`: nine event groups and 60 per-observed-event guard decisions reconcile with the independent WPF packets. No ownership loss was observed; NumLock, original foreground, marker removal and birth-qualified process cleanup all pass. The suppression fence is consumer-proof infrastructure, not a new production guard or a claim of atomic HWND targeting, authenticated injection or guaranteed hook survival. This journey does not replace the required candidate/post-merge Sonar scans or publication/fresh released-package canary.

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
