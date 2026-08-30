# Feature Specification: Owner-Scoped Pre-Build Cleanup

**Feature branch:** `work/issue450-owner-scoped-cleanup`
**Created:** 2026-08-31
**Status:** Planned D2 implementation packet. This packet records no product implementation, test result, review verdict, acceptance receipt, tag, package, or publication.
**Source base:** `3ffaefee7d8dbd9680537804c83b96a8f836e8fe`
**Parent:** `specs/011-issue450-sonar-release-program/`, Wave 2.
**Parent anchors:** PRG-003, PRG-007, PRG-008, and PRG-010.
**Design authority:** `agent://ArchitectWave2Ownership`.
**Current-source evidence:** `agent://Wave2OwnershipSource` and `agent://Wave2OwnershipTests`.
**Primary-source synthesis:** `agent://GitHubProcessOwnership`.
**Release intent:** `none`.

## Child boundary

This Wave 2 child removes default pre-build process selection that can affect another owner. It adds one private Windows ownership boundary in `src/netcoredbg_mcp/windows_process_owner.py`. The boundary admits a process to a private Job Object before the process runs, retains the direct handles that prove ownership, and reports a bounded tree-drain result.

The boundary applies independently to two kinds of owner:

- one `DAPClient._DapRun` generation owns one adapter capability; and
- one `BuildSession` command owns a different command capability.

`SessionManager` captures an explicit generation-bound `NoOwnedAdapter | OwnedAdapterCleanup` value and passes it to `BuildManager`. `BuildManager` may proceed only after that value reports a successful owner drain or names that no admitted adapter exists. It never discovers a process by PID, image name, program path, output directory, WMI, `psutil`, or registry row.

`ProcessRegistry` remains an observation and legacy-shutdown compatibility component. It is not an ownership authority, a capability store, or a pre-build input.

This child addresses an independently observed foreign-owner risk. It does not claim that global cleanup caused either historical issue #450 EOF incident. The accepted Wave 1 transport-death finalizer and generation semantics remain intact.

## Problem and user outcome

The current Windows pre-build route can select every `netcoredbg.exe` by image name. It can also select a PID found by WMI executable-path discovery or select a program by basename. These selectors do not prove that the requesting `SessionManager` owns the selected process tree.

A developer who starts a pre-build must affect only the adapter tree that their current session admitted. Another developer's adapter, descendant, or unrelated sentinel process must remain alive. If the current adapter was not admitted, the build must fail in its ordinary typed path instead of widening cleanup to an unproven process.

## D2 calibration

**Bound unit:** `specs/013-owner-scoped-prebuild-cleanup`, not the full five-wave parent program.

A wrong boundary can terminate a foreign debugger tree or allow a child to execute before the system has established ownership. The work crosses DAP lifecycle, `SessionManager`, build orchestration, Win32 process creation, cancellation, and future maintainers. It therefore requires D2 subsystem depth. This packet does not create a D3 program plan, a release plan, or a new public route.

## User scenarios and testing

### User Story 1: Preserve another owner's adapter during pre-build cleanup (Priority: P1)

Two independent users have adapters on the same Windows machine. User A starts a pre-build while user B's adapter has a descendant process. The pre-build drains only A's admitted adapter tree. B's tree and a foreign sentinel remain alive.

**Independent test:** A controlled Windows fixture starts two separately admitted owner trees and a foreign sentinel. The test asks `BuildManager` to consume A's captured cleanup capability. It records `ActiveProcesses == 0` for A and confirms that B and the sentinel remain alive. The same test records that no image, PID, path, directory, WMI, or `taskkill` selector participated.

**Acceptance scenarios:**

1. Given two admitted adapter owners, when A runs pre-build cleanup, then A reaches a drain receipt and B remains unselected and alive.
2. Given a foreign process with the same image name as the adapter, when A runs pre-build cleanup, then the foreign process remains unselected and alive.
3. Given no owned adapter capability, when pre-build begins, then the build performs no process discovery and any remaining lock is reported through the ordinary build result.

---

### User Story 2: Refuse an unadmitted child before it can execute (Priority: P1)

A maintainer needs a Windows adapter or build command to run only after the process belongs to its private Job Object. Assignment, membership verification, accounting, I/O wiring, or resume failure must not leave an unowned child running.

**Independent test:** A fake Win32 seam injects one failure at each admission stage. For every failure before `ResumeThread`, the test proves zero resume calls, bounded termination of the retained child or Job, and closure of all opened handles. A real Windows fixture confirms the successful order.

**Acceptance scenarios:**

1. Given `CreateProcessW` returned suspended root and thread handles, when `AssignProcessToJobObject` fails, then the root never resumes and the method raises a typed admission error.
2. Given assignment returned success, when `IsProcessInJob` is false or fails, then the root never resumes and no PID-based fallback is attempted.
3. Given membership and I/O setup succeeded, when `ResumeThread` fails, then the admitted Job is forced, drained, and reported as a failed admission.

---

### User Story 3: Drain each owned tree across normal completion and cancellation (Priority: P1)

A build command can end normally, hit its timeout, receive `BuildSession.cancel()`, or lose its outer task to cancellation. Each command must drain its own Job and preserve the original command outcome. The adapter finalizer must continue to provide the one generation-scoped terminal outcome established by Wave 1.

**Independent test:** Controlled command and adapter fixtures hold a descendant alive until the owner requests graceful or forced cleanup. Tests drive normal completion, grace-deadline expiry, command timeout, session cancellation, outer task cancellation, stale adapter capture, and owner-accounting failure.

**Acceptance scenarios:**

1. Given a graceful adapter stop exceeds its bound, when the finalizer escalates, then only the retained Job receives `TerminateJobObject` and `ActiveProcesses` reaches zero before the result says `drained`.
2. Given a BuildSession command is cancelled, when cleanup runs, then its command Job drains before the original cancellation is propagated.
3. Given a captured adapter generation no longer matches the current run, when pre-build consumes it, then it returns `stale`, touches no process, and starts no build command.

---

### User Story 4: Preserve public routes and Wave 1 lifecycle meaning (Priority: P2)

A consumer continues to use the legacy Python/default route. Existing `start_debug(pre_build=True)`, `restart_debug(rebuild=True)`, direct Python calls that do not request pre-build, stateless-preview selection, package dependencies, and host parity keep their current meaning.

**Independent test:** Focused direct-route, public-tool, installed-wheel, and host-parity controls prove that the internal capability cutover did not change route selection or introduce `pywin32` as a direct dependency.

**Acceptance scenarios:**

1. Given a direct Python caller with `pre_build=False`, when the child is implemented, then no owner cleanup is requested.
2. Given a public `start_debug(pre_build=True)` or `restart_debug(rebuild=True)` call, when a current owner exists, then the call follows the explicit capability route before restore and build.
3. Given the stateless preview is selected or the legacy Python route is installed, when the focused compatibility controls run, then the selection and public envelopes remain unchanged.

## Edge cases

- A retained PID, process name, executable path, WMI row, `psutil` process object, `ProcessRegistry` row, generation, and session ID are observations. None can independently authorize termination.
- The root can exit while descendants remain in the Job. Root exit does not prove tree drain.
- `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` is a crash-safety limit. Closing a handle does not substitute for `ActiveProcesses == 0` evidence.
- A process already in an incompatible Job must fail before resume. The code must not request breakaway or run an unowned child.
- A `Win32_Process.Create` or explicit breakaway descendant is not automatically covered by the normal Job inheritance claim. If the controlled production-path proof finds one, Wave 2 remains open and re-enters design.
- A stale `OwnedAdapterCleanup` capture must not disconnect, terminate, or mutate a newer adapter generation.
- ProcessRegistry startup, explicit public force cleanup, and legacy shutdown compatibility are not made owner-safe by this child. The child must not represent them as owner evidence.
- A lock that remains after `NoOwnedAdapter` is not permission to search for or terminate an unrelated process.

## Functional requirements

- **WOC-001: Behavior-first RED matrix.** Before production code changes, add deterministic behavioral RED coverage for the fifteen required acceptance rows in [plan.md](plan.md#deterministic-red-and-green-matrix). The denominator is eleven owner-safety rows and four compatibility controls. Each row must fail against `3ffaefee7d8dbd9680537804c83b96a8f836e8fe` because current behavior permits a selector, post-start admission, missing capability threading, or missing preservation proof. A test must not fail only because a planned symbol is absent.
- **WOC-002: One private Windows owner boundary.** Windows process creation for the adapter and each `BuildSession` command MUST use `WindowsOwnedProcess` in `src/netcoredbg_mcp/windows_process_owner.py`. It MUST create an unnamed private Job, set `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE`, create the child with `CREATE_SUSPENDED`, assign the retained root handle, verify membership and accounting, attach only intended standard handles, and call `ResumeThread` last. No global lifecycle service, singleton map, or registry-backed capability may be added.
- **WOC-003: Preserve Wave 1 adapter lifecycle.** Each existing `_DapRun` generation MUST hold exactly one distinct `WindowsOwnedProcess` capability on Windows. The existing manager-issued generation, one guarded finalizer, terminal callback ordering, and stale-generation fencing MUST retain their meanings. Owner drain occurs inside the existing finalizer rather than through a second adapter shutdown path.
- **WOC-004: Own every Windows build command.** Each `BuildSession` command MUST receive a fresh capability. Normal completion, build timeout, `BuildSession.cancel()`, and outer task cancellation MUST perform the command's bounded owner cleanup and drain before they complete. The old post-spawn Job methods and PID reopen path must be deleted after callers migrate.
- **WOC-005: Make pre-build ownership explicit.** `SessionManager` MUST capture `NoOwnedAdapter | OwnedAdapterCleanup` for its exact active adapter generation. `BuildManager.pre_launch_build()` MUST require that sum type, consume it before restore or build, and abort without starting a command on `stale`, `failed`, or `timed_out`. `NoOwnedAdapter` authorizes no selection or discovery.
- **WOC-006: Remove selector authority cleanly.** After every internal caller migrates, the default and retry pre-build paths MUST not reach `taskkill`, `WMIC`, image-name, PID, basename, output-directory, `lsof`, `/proc`, `pkill`, or `psutil` selection. Delete `cleanup_for_build`, `kill_debugger_processes`, `kill_processes_in_directory`, their helpers, flags, exports, selector tests, and comments rather than retaining a wrapper or fallback.
- **WOC-007: Report truthful owner drain.** A successful Windows drain MUST require `QueryInformationJobObject(JobObjectBasicAccountingInformation)` to report `ActiveProcesses == 0` within the selected bound. Graceful cleanup precedes force. `TerminateJobObject` targets only the retained Job after the grace bound. Any accounting query failure or deadline expiry returns a non-drained result.
- **WOC-008: Fail closed and preserve outcomes.** Any failure before resume MUST terminate the retained suspended child or Job, close opened handles, and raise `ProcessAdmissionError`. Resume failure MUST terminate and drain the admitted Job. Command cancellation MUST shield owner cleanup long enough to preserve the original cancellation or timeout after the drain result is captured.
- **WOC-009: Keep ProcessRegistry non-authoritative.** The pre-build and normal/pre-build adapter stop paths MUST not call `ProcessRegistry.cleanup_all()` as owner cleanup. Registry entries may remain for read-only status, observation, unregistering, and explicitly scoped legacy compatibility. They MUST not be passed to `WindowsOwnedProcess` or `BuildManager` as authority.
- **WOC-010: Preserve the Windows security boundary.** The private boundary MUST use explicit `ctypes.WinDLL("kernel32", use_last_error=True)` signatures, non-inheritable Job/process/thread handles, an unnamed Job, and an explicit standard-handle list. It MUST not set `BREAKAWAY_OK` or `SILENT_BREAKAWAY_OK`, leak handles to children, log raw handles or environment values, add `pywin32` as a direct dependency, or fall back to an unowned asyncio spawn on Windows.
- **WOC-011: Preserve public compatibility.** The legacy Python/default route, public tool names and envelopes, stateless-preview route, package dependency set, Sonar and coverage policy, and package/release boundaries MUST remain unchanged. The child carries `release_intent: none` and creates no tag, package, prerelease, publication, or release receipt.
- **WOC-012: Explain the owner boundary.** Changed source and this packet MUST make the authority rule, admission order, generation fence, cleanup states, failure result, and registry limitation clear enough that a future maintainer does not reintroduce a selector as a fallback.
- **WOC-013: Require exact internal closure.** A Wave 2 acceptance receipt may be created only after the final focused nonzero-denominator proof, an atomic exact candidate commit, an independent exact-candidate review, and a separate acceptance judgment agree on that candidate SHA. The receipt must state `release_intent: none`; it does not authorize Wave 3, a tag, package publication, or v0.23.11 shipment.

## Success criteria

| ID | Measurable future outcome | Requirement links |
|---|---|---|
| WOC-SC-001 | The fifteen-row behavioral matrix has a nonzero denominator. Every row is first observed RED and later GREEN on the same named scenario. | WOC-001 |
| WOC-SC-002 | In a two-owner production-path proof, 0 foreign adapter roots, descendants, or sentinels are selected or terminated. | WOC-002, WOC-005, WOC-006, WOC-007 |
| WOC-SC-003 | Every successful selected Windows process is assigned, membership-verified, and I/O-ready before one successful resume. | WOC-002, WOC-010 |
| WOC-SC-004 | Every injected pre-resume admission failure records 0 resume calls and bounded cleanup of retained resources. | WOC-002, WOC-008 |
| WOC-SC-005 | Every selected adapter and build-command tree reaches `ActiveProcesses == 0` before its result reports `drained`. | WOC-003, WOC-004, WOC-007 |
| WOC-SC-006 | A stale or absent pre-build capability starts 0 cleanup selectors and 0 build commands until a legal precondition is present. | WOC-005, WOC-006, WOC-008 |
| WOC-SC-007 | The default and retry pre-build paths contain 0 reachable image, PID, path, directory, WMI, `taskkill`, `lsof`, `/proc`, `pkill`, and `psutil` selector actions. | WOC-006, WOC-009 |
| WOC-SC-008 | Focused compatibility controls preserve the public Python/default and stateless-preview routes and add 0 direct dependencies. | WOC-011 |
| WOC-SC-009 | The final internal closure binds one exact candidate SHA, the full matrix, exact-candidate review, acceptance judgment, and unchanged-route comparison. | WOC-013 |

## Integration points and exact owner map

| Existing boundary | Current fact | Required Wave 2 action | Must remain unchanged |
|---|---|---|---|
| `src/netcoredbg_mcp/dap/client.py` | `DAPClient.start(generation=...)` still launches through `asyncio.create_subprocess_exec`; `_DapRun` owns the Wave 1 observer/finalizer state. | Create and retain the per-generation adapter capability through the private boundary. Put tree drain inside the existing finalizer. | DAP wire format, manager-issued generation, observer/finalizer cardinality, and terminal callback meaning. |
| `src/netcoredbg_mcp/session/manager.py` | `start()`, `pre_launch_build()`, `launch(pre_build=True)`, `stop()`, and `restart(rebuild=True)` are the owning call paths. | Capture the current generation-bound cleanup variant, pass it to `BuildManager`, and remove normal/pre-build registry cleanup after owner drain. | Public state, DAP policy, route selection, and the Wave 1 terminal contract. |
| `src/netcoredbg_mcp/build/manager.py` | `pre_launch_build()` currently carries `cleanup_before_build=True` into `BuildSession`. | Require `PreBuildOwner`; consume it before restore/build; abort on non-drained owner result. | Restore-before-build ordering, result and output callback behavior. |
| `src/netcoredbg_mcp/build/session.py` | The current Job is created after command creation and assignment ignores failure. | Give each command its own private capability; migrate normal, timeout, cancel, and outer-cancellation cleanup. | Build policy, output limits, command serialization, and result semantics. |
| `src/netcoredbg_mcp/build/cleanup.py` and `build/__init__.py` | Global selectors currently contain `taskkill`, WMIC, directory, image, program, and Unix process discovery. | Replace the module's authority role with explicit pre-build variants, then delete the selector API and exports. | No selector behavior survives as a compatibility path. |
| `src/netcoredbg_mcp/process_registry.py` | PID-only entries support status and destructive legacy methods. | Keep observation/compatibility distinct. Do not use PID persistence in owner admission or pre-build cleanup. | Existing status projection remains a comparison surface. |
| `tests/test_client.py`, `tests/test_session.py`, `tests/test_build_cleanup.py`, `tests/test_build_session.py`, `tests/test_build_manager.py`, and `tests/test_process_registry.py` | Existing tests cover Wave 1 behavior, selector behavior, post-spawn mocks, and PID helpers. | Add and replace focused tests according to the behavioral matrix. | Existing unrelated contracts. |

## Explicit non-goals

This child does not:

- claim that global cleanup caused either historical issue #450 incident or repair the historical EOF producer;
- turn `ProcessRegistry` into a global owner service, a retained-handle store, or a durable cross-crash ownership proof;
- change the public Python/default route, console entry point, MCP tool schema, stateless-preview route, or DAP semantics;
- add a generic cross-platform process framework, a global lifecycle service, a singleton owner map, a compatibility shim, a fallback selector, or a new `pywin32` dependency;
- claim that `Win32_Process.Create`, explicit breakaway, or arbitrary externally created descendants are contained without the required real production-path proof;
- change Sonar policy, coverage, workflows, package metadata, release files, a tag, publication, or consumer-release authority; or
- create `acceptance-receipt.md` during packet authoring. That file is intentionally delayed until WOC-013's evidence exists.

## Planning limits

The exact `ctypes` structure layout, I/O adapter implementation, timeout constants, and Windows CI command selection remain implementation details. The selected implementation must meet the types, ordering, authority, failure, and proof contracts in this packet. If an exact primary-source or current-source fact disproves the selected boundary, implementation must stop before a selector or unowned spawn is added and must re-enter the design boundary with the new evidence.
