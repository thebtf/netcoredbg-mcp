# Quickstart: Verify Adapter Transport-Death Lifecycle

This implementation-phase focused verification guide does not itself perform a build, formatter, linter, project-wide suite, commit, review, release, Sonar scan, package publication, or route selection change. `acceptance-receipt.md` records the completed Wave 1 candidate and proof; a later run creates fresh verification evidence.

## Preconditions

1. Work from the exact Wave 1 candidate based on `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9` or its later atomic candidate commit.
2. Confirm that the candidate changes only the planned DAP client, session manager/state, focused existing tests, and documentation surfaces.
3. Confirm that TD-001 was observed RED before product implementation. Retain the exact failing assertion or test output for the later acceptance receipt.
4. Do not add a source change, coverage exemption, Sonar disposition, build-cleanup fix, workflow change, package/default-route change, or stateless-preview change while using this guide.

## Confirm the frozen design

The selected arena base is the client-owned per-run `_DapRun` capsule, not the arena's swapped proposal label. Before process startup, `SessionManager` issues and binds the generation, installs the one terminal sink, and passes the same generation to `DAPClient.start()`. The client must use that identity for its `_DapRun`, observers, finalizer, and frozen terminal fact, then return the same identity.

Confirm that the finalizer election has no `await` between phase check, first-trigger assignment, and sole-finalizer-task assignment. Confirm that the manager compares active and stopping generations only when it consumes the callback. The implementation must record matching stop facts without fabricating an explicit-stop transport cause.

The synthesis grafts manager-issued pre-start generation, active/stopping generation comparison, named callback dispositions, and projection-before-transition ordering. It rejects manager-owned subprocess finalization, an async terminal queue, derived manager phases, and a PID-plus-generation return record.

## Run the focused lifecycle group

Run only the existing focused files that own the child contract:

```powershell
uv run --no-sync pytest -q `
  tests/test_client.py `
  tests/test_session.py `
  tests/test_debuggee_liveness.py `
  tests/test_resource_updates.py
```

Expected result: the command has a nonzero denominator and every added Wave 1 case passes. A zero-test collection, a skipped replacement for the controlled subprocess, or a broader suite result does not satisfy TD-010.

## Check the deterministic EOF journey

The T001 fake subprocess must demonstrate this exact causal path:

1. Construct the real `DAPClient` and existing `SessionManager` seam.
2. Have the manager issue and bind one generation before it starts the client. Confirm that the client returns the same run identity. Set manager state to `RUNNING`, set a debuggee PID, and register existing state-listener plus state/thread resource observers.
3. Insert at least one unsettled DAP request.
4. Make fake stdout return `b""` immediately. Do not send a DAP `terminated` event.
5. Await the reader/finalizer completion.
6. Read the existing `SessionState.to_dict()` projection, and if the focused test registers the tool registry, read the existing `get_debug_state` result.

Expected result after the repair:

- state is terminal or explicitly unavailable, never `running`;
- `debuggeeAlive` is false or explicitly unavailable;
- the pending request has one prompt terminal failure;
- existing manager observers see one terminal state transition and one logical state/thread resource-notification path; and
- the terminal summary identifies the current run generation and EOF as the first trigger without inventing a crash or debuggee exit.

Expected result before the repair: the desired assertions fail because the current manager retains `RUNNING`, derives `debuggeeAlive=true` from the old debuggee PID, and makes no state/resource transition.

## Check diagnostic bounds

Exercise the cases added to `tests/test_client.py`:

1. A process exits with a known adapter return code and emits stderr before completion.
2. A process reaches terminalization without an observed return code.
3. Stderr exceeds the configured tail capacity and has no newline at the truncation point.
4. A DAP event arrives before EOF.
5. A malformed/incomplete DAP frame or another reader fault occurs.

Expected result:

- adapter PID and observed-or-unknown adapter exit facts are represented separately;
- stderr retains only the configured tail and records truncation;
- last-DAP-event data is bounded and captured before handlers;
- reader error data is bounded; and
- no case calls EOF a crash, turns adapter return code into a debuggee exit code, or waits indefinitely for a stream/process join.

## Check DAP semantic separation

Exercise these event/lifecycle sequences through the existing client and manager test seams:

| Sequence | Required result |
|---|---|
| `process` → `continued` → `exited`, then EOF; omit `terminated`. | The DAP debuggee exit code is retained. Final transport death makes the public session terminal, but the terminal record does not claim `protocol terminated` merely because `exited` occurred. |
| DAP `terminated`, then delayed adapter process completion. | The terminal record marks protocol termination. It leaves adapter return code unknown until the process waiter observes it and does not fabricate a debuggee exit. |
| Adapter process completion, then stdout EOF. | One finalizer and one manager callback occur. Adapter return code remains an adapter fact. |
| Truthy non-DAP stdout text. | It is not classified as EOF. Existing parser behavior remains intact. |

## Check terminal races

Run the deterministic race cases in the focused client/manager/resource files. They must cover:

1. DAP `terminated` → EOF → process exit.
2. Process exit → EOF.
3. EOF while the process remains live during the bounded natural-exit window.
4. Reader fault before process completion.
5. Explicit manager stop marks its stopping generation before snapshot publication while EOF/process completion races.
6. An unrequested terminal snapshot publishes before a later user stop begins.
7. A prior-generation snapshot arrives after a newer adapter run becomes active.
8. Competing terminal observers reach the election boundary. Assert that the phase check, first-trigger assignment, and sole-finalizer-task assignment run with no `await`, so only one observer wins.

## Check public route containment

Inspect the candidate diff before committing. The only expected production sources are:

```text
src/netcoredbg_mcp/dap/client.py
src/netcoredbg_mcp/session/manager.py
src/netcoredbg_mcp/session/state.py
```

`src/netcoredbg_mcp/tools/debug.py` is an unchanged public projection comparison surface unless a focused source fact requires only documentation clarification. The expected focused test files are:

```text
tests/test_client.py
tests/test_session.py
tests/test_debuggee_liveness.py
tests/test_resource_updates.py
```

No file below may be changed by this child:

```text
src/netcoredbg_mcp/build/**
scripts/run_sonarqube_exact_head.py
SonarQube.Analysis.xml
.github/**
pyproject.toml
uv.lock
docs/RELEASE-PROTOCOL.md
```

The public Python package, console entrypoint, default selection, and stateless-preview route must remain unchanged. The Wave 2 owner-scoped pre-build cleanup work remains excluded.

## Recheck after a source correction

The original Wave 1 candidate is internally accepted. If a source correction changes that candidate:

1. Run the focused proof against the successor source SHA.
2. Give an independent reviewer that exact source SHA, the changed scope, and the focused proof.
3. Update `acceptance-receipt.md` only after the focused proof and exact-head review pass.

The receipt remains an internal Wave 1 closure artifact. It does not authorize a tag or release v0.23.11.
