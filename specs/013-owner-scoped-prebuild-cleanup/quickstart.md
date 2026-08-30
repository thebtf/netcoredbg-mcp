# Quickstart: Verify Owner-Scoped Pre-Build Cleanup

This is an implementation-phase focused verification guide for Wave 2. Packet authoring did not run these commands. This guide does not by itself build a release, run a formatter or linter, publish a package, create a tag, alter a route, or create an acceptance receipt.

**Source base:** `b4259ff9bde52755c1cecccbf4ce980f2292a5ac`
**Release intent:** `none`

## Preconditions

1. Work from the exact Wave 2 candidate based on the stated source base or its later complete candidate SHA.
2. Confirm that Wave 1's accepted generation/finalizer contract is present before modifying DAP launch or stop behavior.
3. Retain the behavior-first RED output for O1 through O11 and C1 through C4. Each row must identify why the source base fails.
4. Run real Windows owner tests on Windows. A mocked success or static scan does not prove Job admission, handle inheritance, two-owner isolation, or accounting drain.
5. Do not add a selector, ProcessRegistry owner map, pywin32 dependency, public route change, Sonar/coverage change, package/release edit, or historical EOF causality claim while using this guide.

## Confirm the frozen owner contract

Before testing, inspect the candidate against [contracts/windows-owned-process.md](contracts/windows-owned-process.md).

- The adapter and each build command use separate `WindowsOwnedProcess` instances.
- Windows creation follows `create Job -> set limits -> create suspended -> assign -> verify -> wire I/O -> resume`.
- The `DAPClient._DapRun` finalizer remains the sole adapter cleanup and terminal-publication owner.
- `SessionManager` captures a generation-bound `PreBuildOwner` and `BuildManager` consumes it before restore/build.
- `NoOwnedAdapter` performs no process selection.
- `drained` means `ActiveProcesses == 0`; root exit and Job-handle close do not mean drain.
- `ProcessRegistry` is not passed to pre-build or owner cleanup.

## Run the focused matrix

Run only the test files that own the Wave 2 contract. The implementation may add a focused fixture file. Include it in the command when its path is known.

```powershell
uv run --locked --extra dev python -m pytest -q `
  tests/test_client.py `
  tests/test_session.py `
  tests/test_build_cleanup.py `
  tests/test_build_session.py `
  tests/test_build_manager.py `
  tests/test_process_registry.py
```

Expected result: the output maps each of the following 15 behavior rows to at least one executed assertion.

| Group | Required rows | Denominator |
|---|---|---:|
| Owner admission and drain | O1 through O11 | 11 |
| Route and compatibility preservation | C1 through C4 | 4 |
| Total | O1 through O11 plus C1 through C4 | 15 |

A zero-test collection, skipped owner fixture, a single aggregate assertion with no row mapping, or a broad-suite result does not satisfy WOC-001.

## Check the admission sequence

Drive the fake Win32 seam through O1 through O4. It must observe:

1. a private unnamed Job is created and configured before a child exists;
2. `CreateProcessW` creates the root suspended;
3. assignment succeeds before membership and accounting verification;
4. parent I/O is ready before `ResumeThread`; and
5. every prior-stage failure leaves the resume-call count at zero.

For each rejected case, assert that the retained root or Job is terminated within the named bound, every opened handle is closed, and `ProcessAdmissionError` carries the failing stage. Do not accept an image, PID, path, WMI, directory, or registry fallback.

## Check the real two-owner journey

Run the controlled Windows fixture through the production adapter/pre-build path.

1. Start owner A with a gated adapter descendant.
2. Start owner B with a separate gated adapter descendant.
3. Start a foreign sentinel with the same image or an otherwise tempting selector identity.
4. Capture A's `OwnedAdapterCleanup` through `SessionManager` and invoke `BuildManager.pre_launch_build()`.
5. Release or exceed A's configured grace path.
6. Assert that A reports `ActiveProcesses == 0` before a `drained` receipt.
7. Assert that B's root and descendant remain alive and the foreign sentinel remains alive.
8. Complete B through B's own capability so fixture cleanup does not confuse A's result.

The test records whether a force path was needed. A force is valid only for A's retained Job. Neither root exit nor closing the Job handle is a substitute for the accounting assertion.

## Check command cancellation

Drive O7 and O8 through `BuildSession`:

- normal command completion;
- build timeout;
- `BuildSession.cancel()` while output blocks;
- outer task cancellation while a command descendant remains alive; and
- accounting query failure or drain deadline expiry.

Expected result: each command has its own capability. Cleanup joins or completes within the selected bound, the result records the drain status, and the original timeout or cancellation remains observable after cleanup. A non-drained command does not start a retry selector.

## Check pre-build variants and selector removal

Drive O9 through O11:

1. With a stale capture, `BuildManager` aborts before restore/build and calls no process termination API.
2. With `NoOwnedAdapter`, BuildManager performs no process selection. A lock reaches the ordinary typed build failure path.
3. With a current A capability, BuildManager drains A before restore/build and never learns B's PID or Job.
4. Trigger a build-lock retry. It must not invoke a selector.

Run this supporting source check after behavioral proof. It is a complement, not a denominator row.

```powershell
Select-String -Path `
  src/netcoredbg_mcp/build/cleanup.py, `
  src/netcoredbg_mcp/build/session.py, `
  src/netcoredbg_mcp/build/manager.py, `
  src/netcoredbg_mcp/session/manager.py `
  -Pattern 'taskkill|wmic|kill_all_netcoredbg|cleanup_for_build|kill_debugger_processes|kill_processes_in_directory|lsof|pkill|/proc'
```

Expected result after clean cutover: no reachable selector authority remains. Inspect a match before deciding whether it is a test string, historical documentation, or an obsolete production path. A source scan never proves two-owner safety on its own.

## Check compatibility controls

Run the focused tests selected for C1 through C4. They include direct Python behavior, public pre-build/restart calls, installed-consumer behavior, and direct-versus-host parity.

```powershell
uv run --locked --extra dev python -m pytest -q `
  tests/test_host_proxy.py `
  tests/test_host_mux_ownership.py `
  tests/critical/test_release_critical.py `
  tests/critical/test_typed_bitblt_fallback_public.py `
  tests/critical/test_resources_relay_critical.py
```

Expected result: the public Python/default route and stateless preview remain unchanged, and the candidate does not add a direct `pywin32` dependency. Scope the command to exact test nodes if unrelated existing cases are not part of the child contract. Do not substitute a release, package publication, or broad suite for these controls.

## Freeze and accept only after proof

After all 15 rows are GREEN on one exact candidate:

1. inspect the scoped diff and create the complete Wave 2 candidate commit;
2. obtain an independent exact-candidate review that re-derives the ownership, admission, drain, selector-removal, registry, security, and compatibility claims;
3. obtain a separate acceptance judgment for the reviewed candidate; and
4. only then create `acceptance-receipt.md` with the exact candidate SHA, 15-row matrix, fixture evidence, review/judgment identities, unchanged-route comparison, and `release_intent: none`.

The receipt is intentionally absent now. It is an internal Wave 2 closure artifact, not authority to start a release, create a tag, publish a package, or treat the candidate as v0.23.11.