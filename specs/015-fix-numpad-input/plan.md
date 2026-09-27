# Implementation Plan: Physical Numeric-Keypad Input

**Spec:** [spec.md](spec.md)  
**Depth:** D1 — one existing public input boundary, no new subsystem.  
**Base:** `9cc0d0985a76e8d90b08c4afcd46623e3443ad61`  
**Release intent:** planned PATCH after accepted installed consumer proof; root owns release/authority decisions.

## Caller-first boundary

A caller of `ui_send_keys_focused(keys="{NUMPAD1}")` expects the already-focused Windows target to receive one physical keypad-1 down/up pair. A selector-based `ui_send_keys(keys="{NUMPAD1}", automation_id=...)` must target that element; `ui_send_keys_batch(keys=["{NUMPAD1}", "{NUMPADADD}"])` must preserve the batch's focus and order; `ui_key_sequence(modifiers=["ctrl"], keys=["NUMPAD1"], ...)` must keep its focus verification and cleanup contract. Bad tokens keep existing failure semantics. Inputs, tool names and output envelopes do not change; the observed physical target event is the oracle, not `sent`, `keys`, or `sent_count`.

## Existing integration map

| Existing path | Current relationship | Required change or preservation |
|---|---|---|
| `src/netcoredbg_mcp/tools/ui.py` | Single/focused/batch tools choose FlaUI bridge or default Python; FlaUI sends `send_keys`/`send_keys_batch`. | No new tool/response; only document the newly supported syntax. |
| `src/netcoredbg_mcp/ui/automation.py`, `ui/pywinauto_backend.py` | Python selector path focuses then `element.type_keys`; focused and batch fallback call the Win32 `_send_keys_via_input` parser. | Extend the exact keypad-token path on both Python routes; keep regular `type_keys`, character and shortcut handling unchanged. Check the actual pywinauto element-route capability before choosing the smallest focused implementation. |
| `bridge/Commands/InputCommands.cs` | Single/batch and stealth variants share `SendKeySequence`; brace and modifier target handling are separate branches. | Recognize the canonical names in both branches, preserving ordinary text/modifier behavior and signing. |
| `bridge/Commands/KeySequenceCommands.cs` | Scoped parser has its own named-key table and uses signed scancode `SendInput`; `IsExtendedKey` selects E0-style extended keys. | Parse the same keypad names and encode keypad Enter/divide as extended, NumLock as nonextended scan 0x45 (VK 0x90); ordinary Enter remains non-extended. |
| `src/netcoredbg_mcp/ui/key_sequence.py`, `tools/ui_evidence.py` | Scoped names are validated before bridge; unsupported Python scoped backend returns blocked. | Admit only the canonical keypad names; keep existing normalization, unsupported status, focus and modifier cleanup. |
| `src/netcoredbg_mcp/ui/flaui_client.py`, `bridge/Commands/StealthCommands.cs`, `bridge/JsonRpcHandler.cs` | Existing transport and stealth dispatch call the same bridge senders. | No new route; exercise as integration witnesses. |

## Implementation decision and material guarantee

Use the established brace-special-key grammar, one canonical spelling per physical key: `NUMPAD0`–`NUMPAD9`, `NUMPADADD`, `NUMPADSUBTRACT`, `NUMPADMULTIPLY`, `NUMPADDIVIDE`, `NUMPADDECIMAL`, `NUMPADENTER`, `NUMLOCK`. Do not translate to text or alias top-row digit/ordinary Enter. Existing `SendInput`/signed input is the mechanism; verify keypad Enter has Enter's VK with the extended scancode, divide remains extended, and NumLock delivers VK 0x90/scan 0x45 without E0, while digits and other keypad operations retain keypad identity. The observed WPF event for E045 resolved to VK 255 rather than NumLock, so physical delivery, not an E0 bit, determines acceptance. Preserve the existing runner input signature and failure propagation. Input routes may use different implementations, but only the observed down/up physical key event satisfies the shared contract. Do not alter installed/default backend choice. For NumLock, preserve the user's initial toggle state in a `finally`-equivalent consumer verification cleanup.

A dropped/incorrect event or parser rejection is a failure even if a wrapper reports `sent: true`; unknown tokens must stay rejected; a scoped backend incapable of delivery remains `BLOCKED` rather than faking success. No data, authority, persistent state, or new recovery mechanism is introduced.

## RED → GREEN → installed proof

1. **RED observed by root on unchanged linked source:** `python -c "import sys; sys.path.insert(0, 'src'); from netcoredbg_mcp.ui.key_sequence import validate_scoped_key_sequence; print(validate_scoped_key_sequence([], ['NUMPAD1']))"` from the linked worktree printed `status: FAIL`, `reason: unknown key`, `invalid_key: NUMPAD1`; root verified the module came from linked `src/netcoredbg_mcp/ui/key_sequence.py`, not the ambient installed package. This is parser-rejection evidence, **not** physical delivery evidence. Before source changes, add a focused behavioral check with real target key-down/up distinguishing keypad `1`, Add/Subtract and keypad Enter; record RED at the appropriate unimplemented route. Avoid source-text-only assertions and mock forwarding as the oracle.
2. **GREEN:** extend the existing parser/key tables and physical encoding in `InputCommands.cs`, `KeySequenceCommands.cs`, `automation.py`, `key_sequence.py`, and only the minimum selector-route change if its existing `type_keys` path cannot deliver the canonical tokens. Document names on the existing public tools. Prove digits, five arithmetic/decimal keys, keypad Enter and NumLock; contrast ordinary `1` and `ENTER`; prove modifier, concatenated, batch and scoped behavior. Keep default-Python scoped `BLOCKED` behavior; scoped delivery uses the existing FlaUI route.
3. **Candidate smoke:** launch an actual Windows event-observer app and invoke selector/focused/batch public tools through a fresh installed default-Python MCP consumer; then exercise scoped and shared bridge sender through FlaUI. Compare target key-down/up, VK, scan and extended flags with requested keypad identity, notably Add/Subtract. Restore initial NumLock state. Record exact installed artifact, command/inputs, observed events and failing route if any. The root runs scoped tests/build/release gates after siblings land; this planning packet runs none.

## Assurance, rollback, release

The scope-matching regression plus target-observed installed journey are the acceptance owners; an independent checker should examine the D1 physical key/extended-bit risk against the candidate only if that risk is not resolved by the observed event trace. The principal alternative—relabel text `1` or send ordinary Enter—is rejected because it cannot produce the requested physical event. Roll back the scoped token handling without altering default backend selection; no migration is needed. Accepted shipped work enters the project's existing release flow; local source integration is not a release claim, and this packet performs no external write.

The local linked `.specify` contains only `feature.json`: there are no templates, scripts, constitution or extension hooks to run. The owning Spec Kit commands were read from the primary checkout; this compact D1 packet follows their spec→plan→tasks ordering without manufacturing unneeded research, data-model, contract or quickstart artifacts.
