# netcoredbg-mcp v0.23.12

Prepared: 2026-09-27

## Summary

`v0.23.12` is a PATCH release for physical numeric-keypad input in the existing Windows UI tools. The published Python server remains the default; the tool catalog stays at 135 tools, 8 prompts, and 4 resources.

## Fixed behavior

- `ui_send_keys`, `ui_send_keys_focused`, and `ui_send_keys_batch` accept `{NUMPAD0}`–`{NUMPAD9}`, `{NUMPADADD}`, `{NUMPADSUBTRACT}`, `{NUMPADMULTIPLY}`, `{NUMPADDIVIDE}`, `{NUMPADDECIMAL}`, `{NUMPADENTER}`, and `{NUMLOCK}` on both Windows FlaUI and pywinauto send paths. The FlaUI-only scoped `ui_key_sequence` accepts the same names without braces in its list.
- These names produce physical keypad key-down/key-up events rather than ordinary text or top-row digits. Keypad Enter and Divide use extended-key events; NumLock uses VK 0x90 / scan 0x45 without the extended flag.
- NumLock is pressed and released, not set to a chosen state. The resulting digit characters depend on the existing NumLock state. Consumer verification restores its original state after an intentional NumLock press.

## Compatibility

There are no new tool arguments or response shapes, and the Python backend selection remains unchanged. The scoped sequence still requires FlaUI; the opt-in source-only .NET preview is unchanged.

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
