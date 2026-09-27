---
title: "Physical Numeric-Keypad Input"
feature: "015-fix-numpad-input"
design_depth: "D1"
status: "planned"
source_base: "9cc0d0985a76e8d90b08c4afcd46623e3443ad61"
release_intent: "planned PATCH after accepted consumer proof, subject to release flow"
---

# Feature Specification: Physical Numeric-Keypad Input

## Outcome and D1 boundary

A Windows desktop automation consumer can send a **physical keypad key** to the focused app through the existing `ui_send_keys`, `ui_send_keys_focused`, `ui_send_keys_batch`, or scoped `ui_key_sequence` tool. A keypad digit must arrive as a keypad event, not Unicode text, a top-row digit, or an echoed `sent` label. This is a reversible extension of the existing keyboard-input vocabulary, not a new tool or backend selection. Python remains the default and FlaUI remains an existing optional backend. The boundary is consumer-visible but limited to the existing parsers and send paths; D1, not a new subsystem or ADR, is appropriate.

## P1 user story: keypad-specific shortcut

**Given** a running, focused NovaScript-like Windows app that distinguishes keypad `1` and keypad Add/Subtract from ordinary text and top-row digits, **when** a consumer calls `ui_send_keys_focused(keys="{NUMPAD1}")`, **then** the app observes the keypad `1` physical key-down and key-up, not either other input. The corresponding selector-based call focuses its target first; batch delivery keeps its existing focus behavior; scoped FlaUI delivery retains its verified-target and modifier-cleanup semantics. Existing Python scoped-backend `BLOCKED` behavior is not replaced by fake success.

### Acceptance cases

1. Each existing route accepts the same brace-delimited names: `{NUMPAD0}` through `{NUMPAD9}`, `{NUMPADADD}`, `{NUMPADSUBTRACT}`, `{NUMPADMULTIPLY}`, `{NUMPADDIVIDE}`, `{NUMPADDECIMAL}`, `{NUMPADENTER}`, and `{NUMLOCK}`. Case-insensitivity follows the existing special-key convention. For scoped `keys: list[str]`, the normalized named entries reach the scoped bridge parser; unbraced named entries follow its current special-key convention.
2. A target recording actual Windows keyboard events distinguishes `{NUMPAD1}` from text `1` and the top-row `1` virtual key. All ten digits and five arithmetic/decimal keypad keys deliver their corresponding key-down/key-up events. `{NUMPADENTER}` is the extended keypad Enter event rather than the ordinary Enter event; `{NUMPADDIVIDE}` uses its extended-key encoding. `{NUMLOCK}` delivers VK 0x90 with scan 0x45 in nonextended scancode mode: the observed WPF target resolved the E045 variant to VK 255 instead of NumLock. This criterion concerns the event, not whatever character a focused control displays.
3. Modifiers and concatenated tokens retain existing sequencing (`^{NUMPAD1}`, `{NUMPAD1}{NUMPAD2}`); batch sends each item in order; scoped sequences release acquired modifiers even on failure. Named keypad tokens are never silently converted into Unicode characters.
4. `{NUMLOCK}` requests a physical NumLock press/release, not a promised lock-state setting. The consumer proof observes the event and restores the initial lock state after testing; digit text output may depend on the user's NumLock state and must not serve as the event oracle.
5. Existing `{ENTER}`, `{DOWN}`, ordinary text, top-row digits, unsupported names, and current backend-selection/focus behavior remain unchanged. Unknown names still fail rather than being relabeled or reported as successful delivery.

## Requirements

| ID | Consumer contract |
|---|---|
| NUM-001 | The existing four public tools MUST accept the one canonical keypad vocabulary above without new public arguments, aliases, APIs, or changed response shape. |
| NUM-002 | All supported keypad tokens MUST produce the actual distinct Windows keypad key-down/key-up route, including the extended-key distinctions, on every backend path that claims support. An unavailable backend MUST not fabricate success. |
| NUM-003 | Existing modifier, focus, signed-input, error, and batch/scoped cleanup behavior MUST remain intact; ordinary characters and non-keypad keys MUST retain their previous event behavior. |
| NUM-004 | A regression check MUST fail on the original parser for `{NUMPAD1}` before implementation, then prove the resulting physical event (not only `sent`/`sent_keys`) and contrasting ordinary `1`/Enter events after implementation. |
| NUM-005 | Before a release claim, a fresh installed default-Python MCP consumer session MUST exercise representative keypad digits, Add/Subtract, keypad Enter, and NumLock against a real focused Windows event observer, including selector/focused/batch paths; a separate FlaUI route MUST exercise scoped delivery and the shared bridge single/batch sender. Record observed versus expected events and restore NumLock. |

## Integration and exclusions

Affected consumers are the public `tools/ui.py` single/focused/batch operations, `tools/ui_evidence.py` scoped operation, Python `ui/automation.py` and `ui/pywinauto_backend.py`, scoped validation `ui/key_sequence.py`, bridge `Commands/InputCommands.cs`, `Commands/KeySequenceCommands.cs`, and the existing FlaUI bridge callers. The source of truth is the event at the target, not the wrapper's success fields. No fixture-specific key handler, new keyboard API, backend cutover, text-key rewriting, unrelated validation or telemetry, or change to the Python default is in scope. Existing source and test files are unchanged by this planning packet.

## Success and release intent

All 17 named physical tokens route correctly (10 digits, five arithmetic/decimal keys, keypad Enter, and NumLock), with keypad `1` and keypad Enter demonstrably distinguishable from their ordinary counterparts; all four public routes have exercised nonzero consumer evidence. This is accepted shipped-surface work: once integrated, root follows the repository release flow including installed consumer proof and required release gates. This packet authorizes no external writes or publication; root owns those later boundaries.
