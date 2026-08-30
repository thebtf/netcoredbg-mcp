# Implementation Plan: Owner-Scoped Pre-Build Cleanup

**Branch:** `work/issue450-owner-scoped-cleanup`
**Date:** 2026-08-31
**Spec:** [spec.md](spec.md)
**Source base:** `b4259ff9bde52755c1cecccbf4ce980f2292a5ac`
**Parent:** `specs/011-issue450-sonar-release-program/`, Wave 2 internal verification only.
**Release intent:** `none`

## Summary

Replace global pre-build process selection with a private retained Windows owner capability. The adapter's existing Wave 1 `_DapRun` generation and each `BuildSession` command receive distinct capabilities. `SessionManager` supplies `NoOwnedAdapter | OwnedAdapterCleanup` to `BuildManager`, which drains only the selected current capability before restore and build. The final cutover deletes selector authority instead of preserving a fallback.

The work leaves the public Python/default route, stateless preview, DAP behavior, Sonar and coverage policy, package dependency set, and release boundaries unchanged. It does not name a cause for the historical Issue #450 EOF incidents.

## Technical context

| Concern | Planned decision |
|---|---|
| Runtime | Existing Python async DAP, session, and build route. No public runtime or route is added. |
| Windows owner boundary | `windows_process_owner.py` uses `ctypes` and an unnamed Job. It creates suspended processes, assigns and verifies before resume, and reports accounting-based drain. |
| Adapter ownership | One capability belongs inside the existing `_DapRun` generation. The existing finalizer calls its drain path. |
| Build ownership | One capability belongs to each `BuildSession` command. Its timeout and cancellation cleanup owns that command Job only. |
| Pre-build contract | `BuildManager.pre_launch_build()` requires `PreBuildOwner`; no boolean cleanup flag remains. |
| Registry | PID status and explicit legacy compatibility stay separate from pre-build authority. |
| Testing | The first production-facing work is behavior-first RED with a fifteen-row nonzero denominator. Real Windows two-owner proof supplements fake Win32 order tests. |
| Review | An independent reviewer examines the exact final candidate, then an independent acceptance judgment precedes the later receipt. |

## Current source and caller map

| Owner or caller | Current entry point | Planned responsibility |
|---|---|---|
| `DAPClient` | `start(generation=...)`, `_DapRun`, `_finalize_run()`, `stop()` in `src/netcoredbg_mcp/dap/client.py` | Replace Windows asyncio adapter creation with one per-generation capability while keeping Wave 1 generation/finalizer semantics. |
| `SessionManager` | `start()`, `pre_launch_build()`, `launch(pre_build=True)`, `stop()`, `restart(rebuild=True)` in `src/netcoredbg_mcp/session/manager.py` | Capture the current owner variant, pass it to BuildManager, use a matching owner drain, and stop calling registry cleanup on the normal/pre-build path. |
| `BuildManager` | `pre_launch_build()` in `src/netcoredbg_mcp/build/manager.py` | Require and consume `PreBuildOwner` before restore/build. |
| `BuildSession` | `_run_command()`, `_run_build_with_retry()`, `build()`, `cancel()` in `src/netcoredbg_mcp/build/session.py` | Create a distinct command capability and drain it through normal, timeout, retry, session-cancel, and outer-cancel paths. |
| Selector module | `cleanup_for_build()`, `kill_debugger_processes()`, directory helpers in `src/netcoredbg_mcp/build/cleanup.py` | Hold only explicit owner-value definitions during migration, then delete all selection behavior and exports. |
| `ProcessRegistry` | PID entry, `cleanup_all()`, persisted reaping in `src/netcoredbg_mcp/process_registry.py` | Remain observation/explicit legacy compatibility. It is not supplied to pre-build or owner cleanup. |
| Public callers | `start_debug(pre_build=True)`, `restart_debug(rebuild=True)`, direct Python pre-build and host forwarding paths | Preserve envelopes, routing, restore/build ordering, and failure behavior while they pass an explicit internal variant. |

## D2 milestone map

These are independently valuable internal delivery slices. Each has `release_intent: none` and no tag or publication authority. A slice must reach a focused, demonstrable state before the next slice begins.

| Slice | Value closed | Dependencies | Main files | Internal acceptance checkpoint |
|---|---|---|---|---|
| **S1: Adapter owner walking skeleton** | A Windows adapter cannot execute before Job assignment, membership verification, accounting, and I/O setup succeed. | Wave 1 closure and the RED matrix. | `windows_process_owner.py`, `dap/client.py`, `tests/test_client.py`, focused fixture. | Fake order/failure cases plus real adapter I/O prove pre-resume admission, no resume on rejection, one `_DapRun` capability, and `ActiveProcesses == 0` drain. |
| **S2: Build-command containment** | A Windows dotnet command does not outlive normal completion, timeout, session cancellation, or outer cancellation. | S1 private primitive. | `build/session.py`, `tests/test_build_session.py`. | Each command owns a distinct Job. Timeout/cancel cleanup drains before the original outcome completes. |
| **S3: Call-scoped pre-build** | One SessionManager can drain its current adapter before a build without discovering another owner's process. | S1 and S2. | `build/cleanup.py`, `build/manager.py`, `session/manager.py`, `tests/test_build_manager.py`, `tests/test_session.py`. | Two-manager proof drains A, preserves B, refuses stale ownership, and starts no command after non-drained owner results. |
| **S4: Authority contraction and exact closure** | The selector escape hatch is absent, and exact safety evidence is bound to the candidate. | S3 complete caller migration. | `build/cleanup.py`, `build/__init__.py`, `session/manager.py`, focused tests, this packet. | Selector APIs, flags, exports, and normal registry cleanup are removed. Real two-owner proof, route/dependency comparison, exact-candidate review, and later receipt requirements are satisfied. |

## Deterministic RED and GREEN matrix

The acceptance denominator is **15 behavior rows**. Static scans support the rows but never replace them. Parameterized Win32 admission failures each produce a separately recorded row.

| ID | Scenario and initial RED observation | Green owner | Future test home |
|---|---|---|---|
| O1 | Successful ordered admission. Current source starts through asyncio before Job assignment. | S1 | `tests/test_client.py` and `tests/test_build_session.py` |
| O2 | Assignment failure. Current source can run the child before assignment and ignores assignment failure. | S1 | `tests/test_client.py` and fake Win32 seam |
| O3 | Membership or accounting verification failure. Current source has no verification barrier. | S1 | `tests/test_client.py` and fake Win32 seam |
| O4 | Resume failure. Current source has no retained primary-thread failure path. | S1 | `tests/test_client.py` and fake Win32 seam |
| O5 | Graceful owner drain. Current root-process cleanup does not prove the tree drained. | S1 | controlled Windows adapter fixture |
| O6 | Grace deadline then owner-only force. Current code can call root kill or selector cleanup. | S1 | controlled Windows adapter fixture |
| O7 | Build command timeout, session cancellation, and outer cancellation. Current command cleanup kills only the root. | S2 | `tests/test_build_session.py` |
| O8 | Owner/accounting loss. Current code can claim completion without accounting. | S1 and S2 | `tests/test_client.py`, `tests/test_build_session.py` |
| O9 | PID reuse or unproven PID mismatch. Current registry/PID shape cannot authorize termination safely. | S4 | `tests/test_process_registry.py` only to prove non-authority |
| O10 | Two independent owners plus foreign sentinel. Current image selector can terminate a foreign adapter. | S3 and S4 | controlled Windows production-path fixture and `tests/test_build_manager.py` |
| O11 | Default and retry pre-build contain no selector action. Current cleanup defaults to `/IM`, WMI, PID, basename, and directory scans. | S4 | `tests/test_build_cleanup.py` plus static check |
| C1 | Direct Python `pre_build=False` and resource/catalog route are unchanged. | S4 | focused direct-route tests |
| C2 | Public `start_debug(pre_build=True)` and `restart_debug(rebuild=True)` preserve their route and ordering. | S3 and S4 | `tests/test_session.py` and public tool tests |
| C3 | Installed wheel and CLI behavior remain unchanged while the private contract remains private. | S4 | `tests/critical/test_typed_bitblt_fallback_public.py` or its focused successor |
| C4 | Direct Python and compatibility-host behavior remain equal. | S4 | `tests/test_host_proxy.py`, `tests/test_host_mux_ownership.py`, and focused critical controls |

A completed result records every row ID, command, platform, exact candidate SHA, and pass/fail outcome. A skipped, zero-item, or static-only result does not close a row.

## Requirements-to-files map

| Requirement | Slice and tasks | Planned source files | Planned test or evidence files | Observable acceptance |
|---|---|---|---|---|
| WOC-001 | S1-S4; T001-T003, T010, T014 | none before RED | focused tests and receipt evidence | 15 separately recorded behavior rows are RED then GREEN. |
| WOC-002 | S1; T004 | `src/netcoredbg_mcp/windows_process_owner.py`, `src/netcoredbg_mcp/dap/client.py` | `tests/test_client.py`, controlled fixture | Pre-resume admission succeeds in order or fails closed. |
| WOC-003 | S1; T004, T010 | `src/netcoredbg_mcp/dap/client.py` | `tests/test_client.py`, `tests/test_session.py` | Wave 1 finalizer and generation contracts retain their meaning. |
| WOC-004 | S2; T005 | `src/netcoredbg_mcp/build/session.py` | `tests/test_build_session.py` | Every Windows command owns and drains its Job. |
| WOC-005 | S3; T006 | `src/netcoredbg_mcp/build/cleanup.py`, `src/netcoredbg_mcp/build/manager.py`, `src/netcoredbg_mcp/session/manager.py` | `tests/test_build_manager.py`, `tests/test_session.py` | Correct owner drains; stale/failed results stop pre-build. |
| WOC-006 | S4; T007, T010 | `src/netcoredbg_mcp/build/cleanup.py`, `src/netcoredbg_mcp/build/__init__.py`, callers | `tests/test_build_cleanup.py`, static supporting check | No selector API or retry route remains. |
| WOC-007 | S1-S3; T004-T006, T010 | owner boundary, DAP and BuildSession callers | fake seam and controlled Windows fixture | Only zero Job accounting is `drained`. |
| WOC-008 | S1-S2; T004-T005 | owner boundary, `build/session.py` | admission/cancel tests | Fail closed and preserve timeout/cancellation outcome. |
| WOC-009 | S3-S4; T006-T007 | `session/manager.py`, `process_registry.py` only if a narrow observation edit is required | `tests/test_process_registry.py`, `tests/test_session.py` | Registry is absent from pre-build and normal owner cleanup. |
| WOC-010 | S1; T004, T009 | owner boundary and comments | fake Win32 seam, source review | Explicit safe Win32 use and inheritance constraints hold. |
| WOC-011 | S3-S4; T003, T010 | no route/config/dependency mutation | public route, host, installed consumer controls | Selection and dependency set remain unchanged. |
| WOC-012 | S4; T009, T012 | changed source and this packet | exact-candidate review | Ownership and failure logic is legible without selector folklore. |
| WOC-013 | S4; T011-T014 | later `acceptance-receipt.md` only after evidence | exact candidate, review, acceptance judgment | Receipt binds exact evidence and remains non-release. |

This table is the living file map. Re-derive it if the current trunk renames, splits, or moves a listed file before implementation.

## Migration and rollback

| Step | Migration action | Safety condition |
|---|---|---|
| 1 | Add the owner primitive and move adapter launch/finalization onto it. | Preserve existing Wave 1 `_DapRun`, generation, and terminal callback semantics. |
| 2 | Move BuildSession command launch and cancellation onto a distinct capability. | Remove post-spawn PID reopen and local Job helper methods in the same slice. |
| 3 | Thread required `PreBuildOwner` through `SessionManager` and `BuildManager`. | Every repository caller passes `NoOwnedAdapter` or a current `OwnedAdapterCleanup`. |
| 4 | Remove registry cleanup from normal/pre-build adapter stop after current owner cleanup is present. | Registry stays observable but cannot become an authority fallback. |
| 5 | Delete selectors, flags, helpers, exports, tests, and comments. | A source scan and production-path test show no selector escape path. |

There is no persisted migration. Rollback reverts the complete proven Wave 2 candidate to the accepted Wave 1 base. A partial rollback that restores selectors beside the new capability contract is prohibited.

## Compatibility proof

The focused compatibility group proves only the boundaries this child promises:

1. direct Python callers that do not ask for pre-build do not gain cleanup work;
2. existing public pre-build and restart routes pass the required internal variant without changing tools, envelopes, or restore/build ordering;
3. public package/CLI behavior remains intact from installed bytes; and
4. direct Python and compatibility-host controls preserve equivalent behavior.

The comparison also confirms that `pyproject.toml`, `uv.lock`, public route selection, stateless preview, Sonar files, coverage configuration, release files, and workflows are unchanged. These controls are future implementation-phase proof. Packet authoring does not run them.

## Exact candidate review and delayed receipt

A green focused matrix is necessary but not enough. The final sequence is:

1. create the one complete Wave 2 candidate commit after all 15 rows are green and the scoped diff is complete;
2. give an independent reviewer that exact SHA, this packet, the retained RED observations, the GREEN matrix, real Windows fixture output, and route comparison;
3. correct any in-scope finding and recheck the exact changed candidate; and
4. obtain a separate acceptance judgment before creating `specs/013-owner-scoped-prebuild-cleanup/acceptance-receipt.md`.

The receipt is intentionally absent during packet authoring. It must name the exact candidate SHA, all 15 denominator rows, review and judgment identities, route/dependency comparison, `release_intent: none`, and no publication authority.

## D2 planning challenge

The accepted D2 authority reached GO after checking premise, existing-code leverage, alternatives, scope, staleness, false dependencies, complexity, value, assumptions, bias, security, and source cross references. The accepted shape is smaller than a ProcessRegistry redesign and stronger than a selector patch. A future implementation finding that contradicts Job inheritance, direct pipe wiring, or current source must re-enter this packet's boundary before new process authority is added.