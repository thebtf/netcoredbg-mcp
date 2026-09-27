# Tasks: Physical Numeric-Keypad Input

**Input:** [spec.md](spec.md), [plan.md](plan.md). **Base:** `9cc0d0985a76e8d90b08c4afcd46623e3443ad61`. **Release:** planned PATCH after installed consumer proof; no external effect belongs to this packet.

## P1 — keypad shortcuts in focused Windows apps

- [ ] T001 [US1] Record the baseline `NUMPAD1` rejection from linked source `src/netcoredbg_mcp/ui/key_sequence.py` (not an ambient installed-package import); add a focused behavior regression in an existing suitable `tests/` or `bridge/` test owner that observes target physical key-down/up and distinguishes keypad `1`, keypad Add/Subtract, keypad Enter from ordinary `1`/Enter. Run it RED on unchanged production source and retain the failure receipt.
- [ ] T002 [US1] Implement the 17 canonical keypad tokens on existing single/focused/batch and scoped parser routes in `bridge/Commands/InputCommands.cs`, `bridge/Commands/KeySequenceCommands.cs`, `src/netcoredbg_mcp/ui/automation.py`, `src/netcoredbg_mcp/ui/key_sequence.py`; touch `src/netcoredbg_mcp/ui/pywinauto_backend.py` only if needed for the selector route. Preserve existing physical input signature, correct keypad extended-key flags, ordinary text and modifiers, focus, and no-fake-success errors. Document the syntax in `src/netcoredbg_mcp/tools/ui.py` and `src/netcoredbg_mcp/tools/ui_evidence.py`.
- [ ] T003 [US1] Run the focused regression GREEN against `tests/` or `bridge/` test owner chosen in T001; compare actual target events for all digits, Add/Subtract/Multiply/Divide/Decimal, keypad Enter and NumLock; verify a modifier sequence, brace concatenation, batch order, scoped cleanup, and unchanged non-keypad key events. A `sent` response alone cannot close this task.
- [ ] T004 [US1] Install the candidate and exercise `ui_send_keys`, `ui_send_keys_focused`, and `ui_send_keys_batch` via a fresh default-Python MCP consumer against a real focused Windows event observer; separately exercise `ui_key_sequence` and shared bridge single/batch routes via FlaUI. Capture requested-versus-observed down/up VK, scan and extended flags; distinguish keypad Add/Subtract from ordinary character keys. Restore initial NumLock state. The default Python scoped backend remains `BLOCKED` rather than faking success. Any unsupported claimed route or only echoed keys is a failing journey.

## Integration and release boundary

- [ ] T005 From the accepted candidate, root runs the required repository gates and updates existing user-facing keyboard syntax documentation/changelog as appropriate; compare changed behavior against [spec.md](spec.md), commit the verified slice, and route accepted shipped-surface work through the existing release flow. T004's installed consumer evidence is mandatory before a release-complete claim; external writes/publication await their own authority.

**Order:** T001 RED → T002 implementation → T003 GREEN → T004 installed event proof → T005 integration/release. This is one consumer-facing slice; there are no parallel implementation tasks sharing input ownership, no new public endpoint or subsystem, and no completed verification claimed by this packet.
