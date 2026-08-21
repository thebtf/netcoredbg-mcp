---
feature_id: 007
slug: native-dap-thread-inspection
title: Native DAP thread inspection walking skeleton
status: PLANNED
design_rung: D2
release_intent: none
---

# Native DAP thread inspection

## Outcome
An opt-in modern native MCP client can start a local debug session, call `get_threads` with its explicit `debugSessionId`, receive a bounded normalized DAP thread list, then stop the same live session. The default Python `netcoredbg-mcp` consumer remains unchanged and reaches `PRODUCT_WORKS` independently.

## Requirements
- **B1-REQ-001:** `get_threads` has a closed input object requiring exactly `debugSessionId`, published as a string with `minLength: 1` and valid at the registry only when `debugSessionId.Trim().Length > 0`. A missing arguments object, omitted `debugSessionId`, non-string, empty, whitespace-only, or extra-field input returns `invalid_tool_arguments` before DAP I/O. Every valid string is an opaque lookup value: short, non-base64url, and unavailable values uniformly return `DEBUG_SESSION_NOT_FOUND` without revealing token grammar.
- **B1-REQ-002:** The host resolves only a live opaque native session token; every unavailable token whose `Trim().Length > 0` returns existing `DEBUG_SESSION_NOT_FOUND` without scanning or creator/connection ownership.
- **B1-REQ-003:** The `internal` typed session operation sends only DAP `threads`; a success response requires the correlated command and `body.threads` entries containing int32 `id` and string `name` (including empty names, which remain DAP-valid).
- **B1-REQ-004:** The public success result is normalized to `{kind:"threads_success",threads:[{id,name}]}` and includes no raw DAP envelope or adapter error body. A correlated `success:false` response returns exactly `{kind:"dap_threads_refused",error:"DAP_THREADS_REFUSED"}` and keeps the session usable. Wrong-command, malformed, timeout, or bound-exceeding responses return exactly `{kind:"dap_threads_protocol_error",error:"DAP_THREADS_PROTOCOL_ERROR"}`, expose no partial list, remove the token, and invoke bounded cleanup.
- **B1-REQ-005:** Before public normalization, `threads` has at most 256 entries; every UTF-8 encoded name is at most 1,024 bytes; and the UTF-8 serialization of the complete structured success object is at most 262,144 bytes. All maxima are inclusive. The existing 16 MiB DAP-frame bound remains an earlier transport guard; every 256/257, 1,024/1,025, and final-serialization boundary is tested with no raw body or partial list released.
- **B1-REQ-006:** The registry owns one `SessionSlot` for each live opaque token and each DAP operation acquires that slot's lease before DAP I/O. Cleanup from an admitted call records its reason, releases that call's own lease, then invokes close-and-drain; close-and-drain closes admission, waits remaining admitted leases, removes the token, and elects one winner to call `StopAsync` and dispose. No new operation acquires after admission closes. Explicit stop, host shutdown, unusable-session eviction, reader failure, timeout, and protocol-error cleanup use that path. `NetCoreDbgSession` notifies the registry of reader failure even with no active request, so that path also closes the owned slot.
- **B1-REQ-007:** The exact candidate proves a native process journey plus the retained installed-Python `PRODUCT_WORKS` non-selection journey from `specs/006-a1-local-preview/quickstart.md`, and records an S2 review using the installed platform source `C:/Users/btf/.omp/profiles/nvmd-selfhost/plugins/cache/plugins/nvmd-ai___nvmd-ai___0.9.19/wiki/security-review.md` before merge. Its durable receipt target is `.agent/runs/b1-native-thread-inspection/security-review.md` in the primary repository; no package/default-selector/release action belongs to this feature.

## Scenarios
1. Valid live token → one normalized thread result and session remains usable for `get_debug_state` then `stop_debug`.
2. A missing arguments object, omitted `debugSessionId`, non-string, empty, whitespace-only, or extra-field token input → `invalid_tool_arguments`, zero DAP requests. Any unavailable string whose `Trim().Length > 0` → `DEBUG_SESSION_NOT_FOUND`, zero DAP requests.
3. Explicit stop, host shutdown, unusable-session eviction, reader failure (both with no in-flight request and during `get_threads`), timeout, and protocol-error cleanup close operation admission. A cleanup-triggering admitted call releases its own lease before close-and-drain; close-and-drain awaits remaining in-flight `get_threads`, removes the token, and runs one bounded `StopAsync`/disposal; later calls return `DEBUG_SESSION_NOT_FOUND`.
4. DAP refusal → exact redacted `DAP_THREADS_REFUSED` and a follow-up `get_debug_state` succeeds; malformed/wrong-command/timeout/bound excess → exact redacted `DAP_THREADS_PROTOCOL_ERROR`, no partial list, bounded cleanup, then `DEBUG_SESSION_NOT_FOUND`.
5. The native executable is not selected by the Python CLI; replay the existing installed-Python `PRODUCT_WORKS` journey named in `specs/006-a1-local-preview/quickstart.md` without a selector/package/configuration reversal.

## Exclusions
Call stack, scopes, variables, execution control, breakpoints, attach, generic DAP forwarding, UI/bridge work, Python cutover, publication, and release.
