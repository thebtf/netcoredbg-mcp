# Research: Owner-Scoped Pre-Build Cleanup

**Scope:** Wave 2 Windows ownership and pre-build cleanup only.
**Source base:** `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`
**Status:** Planning research. It records no implemented boundary, passing Windows proof, accepted candidate, or release.
**Primary-source synthesis:** `agent://GitHubProcessOwnership`, investigated 2026-08-30.
**Current source/test evidence:** `agent://Wave2OwnershipSource` and `agent://Wave2OwnershipTests`.
**Release intent:** `none`.

## Evidence labels

- **OBSERVED** means a fact was read from the current candidate source, a named evidence artifact, or a cited primary source.
- **INFERRED** means a required design conclusion follows from named observations and must be proved by this child on its exact candidate.
- **ADOPT**, **ADAPT**, and **REJECT** state the disposition selected for this child.

## Decision record

| Topic | Decision | Evidence and rationale |
|---|---|---|
| Ownership authority | Retain a private Job and direct process/thread handles. | **OBSERVED:** process IDs, images, paths, and registry rows are selectors or observations. Retained handles name one process object. |
| Windows launch | Use one suspended `CreateProcessW` admission sequence. | **OBSERVED:** the current `asyncio` route loses the primary thread handle and current assignment occurs after execution can begin. |
| Adapter lifecycle | Store one capability inside the existing `_DapRun` generation. | **OBSERVED:** Wave 1 already makes `_DapRun` and manager-issued generation the adapter lifecycle authority. |
| Build command lifecycle | Create one capability per `BuildSession` command. | **OBSERVED:** current commands use a shared post-spawn `_job_handle`; timeout and cancellation kill only the root process. |
| Pre-build boundary | Require `NoOwnedAdapter | OwnedAdapterCleanup` at `BuildManager.pre_launch_build()`. | **INFERRED:** a required sum type makes the no-owner and current-owner cases explicit without teaching BuildManager DAP or Win32 details. |
| Selector cleanup | Remove it with no fallback. | **OBSERVED:** `taskkill /IM`, WMI PID selection, directory scanning, program basenames, `lsof`, and `pkill` are not retained ownership evidence. |
| Registry role | Keep observation and explicit legacy compatibility separate from owner cleanup. | **OBSERVED:** ProcessRegistry entries retain only PID, role, program, session ID, and registration time. |
| Library choice | Use existing `ctypes`, not a direct `pywin32` dependency. | **OBSERVED:** the project already uses ctypes. `pywin32` is transitive, not a declared direct dependency. |
| Historical incident | Do not assert a causal link. | **OBSERVED:** the Issue #450 investigation found no correlated global cleanup around either recorded EOF. |

## Current-source observations

| Classification | Observation | Evidence | Consequence |
|---|---|---|---|
| OBSERVED | `DAPClient.start(generation=...)` creates the adapter through `asyncio.create_subprocess_exec`. | `src/netcoredbg_mcp/dap/client.py:416-457` | The Windows adapter launch must move behind the private admission boundary. |
| OBSERVED | `_DapRun` already owns generation, observers, a guarded finalizer, pending requests, and terminal facts. | `src/netcoredbg_mcp/dap/client.py:204-234, 493-634` | The owner capability belongs in this existing capsule. |
| OBSERVED | `SessionManager.start()` issues/binds a generation, and `stop()` invokes `DAPClient.stop()` then `ProcessRegistry.cleanup_all()`. | `src/netcoredbg_mcp/session/manager.py:830-953, 1813-1852` | Preserve generation/finalizer semantics and remove the normal/pre-build registry cleanup dependency. |
| OBSERVED | `SessionManager.pre_launch_build()` passes no owner capability; `launch(pre_build=True)` stops an active session and then sleeps before building. | `src/netcoredbg_mcp/session/manager.py:1571-1679` | Capture a generation-bound capability explicitly rather than infer liveness from state or delay. |
| OBSERVED | `BuildManager.pre_launch_build()` defaults `cleanup_before_build=True` and passes that boolean to `BuildSession`. | `src/netcoredbg_mcp/build/manager.py:108-173` | Replace the boolean with a required ownership variant. |
| OBSERVED | `BuildSession` creates a Job, starts an asyncio process, then reopens its PID and ignores `AssignProcessToJobObject`'s result. | `src/netcoredbg_mcp/build/session.py:97-318` | Post-start admission is unsafe and must be deleted. |
| OBSERVED | Build timeout and `cancel()` call root `kill()`; retry calls `cleanup_for_build()`. | `src/netcoredbg_mcp/build/session.py:341-397, 507-563` | Command timeout and cancellation need capability force-and-drain. |
| OBSERVED | `cleanup_for_build()` defaults to global `netcoredbg.exe` image termination and also uses WMI/PID/path/directory selectors. | `src/netcoredbg_mcp/build/cleanup.py:27-363` | Remove the selector API and its retry route. |
| OBSERVED | ProcessRegistry persists PID-only records and reopens PIDs for liveness and termination. | `src/netcoredbg_mcp/process_registry.py:22-457` | It cannot authorize owner cleanup. |
| OBSERVED | Existing build cleanup tests assert selector behavior; build-session and manager mocks use `pid=None` and skip the Job path. | `tests/test_build_cleanup.py`, `tests/test_build_session.py`, `tests/test_build_manager.py` | Replace selector expectations and add real/fake admission coverage before source changes. |

## Primary-source ownership facts

| Source | Observed property | Child disposition |
|---|---|---|
| [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) | A Job manages a group of processes. Normal `CreateProcess` descendants join by default, and `TerminateJobObject` targets current members. | **ADOPT** one private Job per owner capability. |
| [CreateProcessW](https://learn.microsoft.com/en-us/windows/win32/api/processthreadsapi/nf-processthreadsapi-createprocessw) and [process creation flags](https://learn.microsoft.com/en-us/windows/win32/procthread/process-creation-flags) | `CREATE_SUSPENDED` keeps the primary thread from running until `ResumeThread`. | **ADOPT** assign and verify before resume. |
| [Process handles and identifiers](https://learn.microsoft.com/en-us/windows/win32/procthread/process-handles-and-identifiers) | A retained handle stays valid after termination while a PID can be reused. | **ADOPT** retained handles as authority. **REJECT** numeric PID as primary identity. |
| [AssignProcessToJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-assignprocesstojobobject) and [IsProcessInJob](https://learn.microsoft.com/en-us/windows/win32/api/jobapi/nf-jobapi-isprocessinjob) | Assignment and membership are explicit Win32 calls with success/failure results. | **ADOPT** both as admission predicates. |
| [QueryInformationJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-queryinformationjobobject) and [JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information) | `ActiveProcesses` reports the current number of associated Job processes. | **ADAPT** as the bounded drain barrier. |
| [TerminateJobObject](https://learn.microsoft.com/en-us/windows/win32/api/jobapi2/nf-jobapi2-terminatejobobject) | The call terminates all processes associated with the Job. | **ADOPT** only after the owner grace bound or command cancellation. |
| [PROC_THREAD_ATTRIBUTE_HANDLE_LIST](https://learn.microsoft.com/en-us/windows/win32/procthread/proc-thread-attribute-list) | A process creation attribute list can restrict inherited handles. | **ADOPT** explicit standard-handle inheritance. |
| [taskkill](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill) | `/im` selects by image name. `/t` follows children of the selected root. | **REJECT** image-name selection and its tree claim. |
| [CPython 3.10 subprocess implementation](https://github.com/python/cpython/blob/3.10/Lib/subprocess.py) | Windows subprocess code retains the process handle and closes the primary thread handle after process creation. | **REJECT** an asyncio post-create suspended admission approach. |
| [psutil 7.2.2](https://github.com/giampaolo/psutil/blob/release-7.2.2/psutil/__init__.py) | PID plus creation-time checks remain inherently racy. Recursive descendants may disappear from a snapshot when an intermediate parent exits. | **ADOPT** only for bounded observation if a test needs it. **REJECT** for authority. |

The links above were accessed through the named primary-source synthesis on 2026-08-30 and reconfirmed by the D2 authority on 2026-08-31. The implementation reviewer must recheck facts whose APIs or platform behavior could have changed.

## Library and platform disposition

| Candidate | Decision | Reason |
|---|---|---|
| Existing Python `ctypes` | ADOPT | It can express the Win32 sequence without adding a direct dependency. |
| `pywin32` b311 | REJECT for this child | It is a viable binding but currently transitive. Promoting it changes the declared dependency contract without reducing the required lifecycle proof. |
| `asyncio.create_subprocess_exec` for Windows owner admission | REJECT | Its thread-handle behavior prevents the required admit-before-resume sequence. It may remain behind the private boundary for non-Windows behavior. |
| `taskkill`, WMI, program basename, output-directory search, `lsof`, `/proc`, `pkill` | REJECT | They select candidates. They do not prove one caller's ownership. |
| PID plus creation time or image path | REJECT as authority | They are weaker observations and can race or be reused. |
| ProcessRegistry | ADAPT for observation only | It remains useful for status and explicitly scoped legacy compatibility. It is not passed to pre-build or owner cleanup. |

## Derived invariants

The following are **INFERRED** from the observations above and are the implementation contract:

1. Only a retained `WindowsOwnedProcess` may force or drain its Job.
2. A child reaches `running` only after Job creation, limit configuration, suspended creation, assignment, membership verification, accounting verification, and I/O wiring succeed.
3. One adapter generation and one build command use distinct capability instances. Their Jobs, direct handles, and receipts never cross.
4. A build sees exactly one explicit pre-build variant. `NoOwnedAdapter` does not permit discovery, and `OwnedAdapterCleanup` must validate the captured generation before it acts.
5. A successful Windows owner drain requires `ActiveProcesses == 0`. Root exit and Job-handle close are insufficient.
6. A non-drained, stale, or failed owner result prevents restore and build. The code reports the resulting build failure rather than widening process selection.
7. No default or retry pre-build path can reach a selector after the clean cutover.
8. Wave 1's generation fence and guarded finalizer remain the only adapter terminal authority. Wave 2 adds a tree-drain fact inside that finalization path.

## Rejected approaches

| Approach | Rejection reason |
|---|---|
| Add a global dictionary from PID to owner. | A PID map is still discovery after direct handles are gone. It also introduces global lifetime and cross-owner coupling. |
| Keep `cleanup_for_build()` as a compatibility wrapper that returns zero. | It preserves a misleading selector-shaped API and invites a future fallback. Callers must migrate and the obsolete API must disappear. |
| Use `ProcessRegistry.cleanup_all()` after a successful owner drain as defense in depth. | It changes ownership from a retained capability back to PID entries and can affect a foreign or attached process. |
| Use `psutil.children(recursive=True)` for force cleanup. | Descendant snapshots are incomplete after parent exit and do not prove authority. |
| Ask the Job to allow breakaway if nested assignment fails. | It trades containment for execution and violates fail-closed admission. |
| Treat the old Issue #450 EOF as proof of a global cleanup kill. | The investigation found no correlation. The foreign-owner risk is sufficient reason for this child. |

## Proof debt carried into implementation

| Debt | Owning slice | Exit condition |
|---|---|---|
| Validate the direct Win32 pipe adapter on every supported Windows Python line. | S1 | Focused I/O proof demonstrates the public adapter stream contract without private CPython assumptions. |
| Prove that the actual netcoredbg debuggee joins the adapter Job and does not use a breakaway or `Win32_Process.Create` path. | S1 and S3 | The production-path fixture observes the debuggee descendant in the admitted Job and drains it. A failure re-enters architecture. |
| Select the committed Windows CI or release-gate home for the real two-owner test. | S4 | The final candidate has one repeatable Windows command and receipt evidence. |
| Reassess explicit `cleanup_processes(force=True)`, startup reaping, and legacy shutdown PID behavior. | Separate future child | This Wave 2 child makes no expanded safety claim for those routes. |

## Limits preserved intentionally

This research does not choose timeout constants, exact ctypes member names, or an implementation of async stream adaptation. Those are private implementation decisions. It does require observable admission, ownership, and drain results. It does not treat ignored coordination probes or sibling-worktree code as acceptance evidence, and it does not create an acceptance receipt.