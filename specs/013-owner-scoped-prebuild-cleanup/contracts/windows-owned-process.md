# Private contract: WindowsOwnedProcess

**Status:** Planned internal contract. This file defines no public Python API and records no implementation result.
**Owner:** Wave 2 only.
**Release intent:** `none`.

## Purpose

`WindowsOwnedProcess` is the only planned Windows boundary that can claim authority over an adapter or build-command tree. Its authority comes from retained direct handles and an admitted private Job. A PID, image name, path, directory, WMI result, `psutil` object, ProcessRegistry record, session ID, or generation cannot substitute for the capability.

## Caller contract

```python
@dataclass(frozen=True, slots=True)
class OwnedProcessRef:
    owner_id: str
    generation: object
    root_pid: int


class DrainStatus(str, Enum):
    DRAINED = "drained"
    TIMED_OUT = "timed_out"
    FAILED = "failed"
    STALE = "stale"


@dataclass(frozen=True, slots=True)
class OwnerDrainReceipt:
    owner: OwnedProcessRef
    status: DrainStatus
    forced: bool
    root_returncode: int | None
    active_processes: int | None
    failure_stage: AdmissionStage | None = None
    winerror: int | None = None
    root_was_forced: bool | None = None


class AdmissionCleanupError(RuntimeError):
    admission_stage: AdmissionStage | None
    admission_winerror: int | None
    cleanup_stage: AdmissionStage
    cleanup_winerror: int | None
    controlling_handles_retained: Literal[True]


class WindowsOwnedProcess:
    @classmethod
    async def launch(
        cls,
        *,
        generation: object,
        argv: Sequence[str],
        cwd: str | None,
        env: Mapping[str, str] | None,
        stdin_mode: Literal["pipe", "devnull"],
    ) -> "WindowsOwnedProcess": ...

    async def wait_root(self) -> int: ...
    async def drain_after_grace(
        self,
        *,
        grace_timeout: float,
        force_timeout: float,
    ) -> OwnerDrainReceipt: ...
    async def force_and_drain(self, *, timeout: float) -> OwnerDrainReceipt: ...
    async def aclose(self) -> OwnerDrainReceipt: ...
```

`DAPClient` owns one instance inside one `_DapRun`. `BuildSession` owns a separate instance for each command. `BuildManager` does not receive an instance. It receives only a one-shot `OwnedAdapterCleanup` that can validate and drain the current adapter owner.

## Launch preconditions and postconditions

| Precondition | Boundary action | Postcondition |
|---|---|---|
| Valid Windows executable, argv, working directory, and environment are supplied. | Build only private parent/child pipe handles and an explicit environment block. | No Job/process/thread handle is inheritable by the child. |
| The host can create a private Job. | Use an unnamed non-inheritable Job with `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` and an associated private completion port. | The Job and port have no global name or shared owner map; notifications are advisory, not proof of drain. |
| A child has not executed. | Call `CreateProcessW` with `CREATE_SUSPENDED`; retain both returned process and primary-thread handles. | The state is `suspended_unadmitted`. |
| The retained root handle is valid. | Call `AssignProcessToJobObject`, `IsProcessInJob`, and initial accounting query. | Only successful assignment, membership, and verification reach I/O setup. |
| Parent I/O adapters are ready. | Use `PROC_THREAD_ATTRIBUTE_HANDLE_LIST` so only standard input, output, and error child ends are inherited. | The Job/process/thread handles remain private. |
| Every prior condition succeeded. | Call `ResumeThread` once. | The state is `running` and the capability can drain its own tree. |

## Admission failures

`ProcessAdmissionError` includes the stage and optional Windows error. If its
cleanup cannot prove root exit, `AdmissionCleanupError` instead preserves both
the admission fact and the cleanup failure fact. It has these required effects:

| Failure stage | Resume permitted | Cleanup action |
|---|---:|---|
| `create_job` or `set_limits` | No | Close partial resources. No child exists. |
| `create_process` | No | Close Job, attribute list, and pipe handles. |
| `assign`, `verify`, or `wire_io` | No | Terminate the retained suspended root, wait within the bound for its exit, then close controlling handles. A terminate error or wait failure raises `AdmissionCleanupError` and retains Job/process/thread handles. |
| `resume` | No successful resume | Terminate the admitted Job, wait for root exit, then close handles and report the original admission error. If root termination fails, the admitted Job may be the fallback; a Job termination or bounded-wait failure raises `AdmissionCleanupError` and retains controlling handles. |

The boundary never invokes asyncio process launch as a Windows fallback. It never requests breakaway to bypass a parent Job conflict.

## Drain contract

1. `drain_after_grace()` permits the caller's graceful shutdown policy only for the configured grace bound.
2. If the tree remains active after that bound, it calls `TerminateJobObject` once for this capability's Job.
3. `force_and_drain()` may skip the grace wait only for a build-command cancellation or another explicit force policy that the caller already selected.
4. `DRAINED` requires `active_processes == 0`, a signaled retained root handle, and signaled handles for all retained members. Reconcile the Job's lifetime total against recorded member births; each member without a retained handle needs proven retirement. A Job-member retirement notification can prove that fact when a handle cannot be retained, but notifications alone never establish drain. Zero accounting or root exit alone is insufficient.
   Both `JOB_OBJECT_MSG_EXIT_PROCESS` and `JOB_OBJECT_MSG_ABNORMAL_EXIT_PROCESS` retire a recorded member birth through the same identity/handle checks. Ignoring an abnormal exit must not leave a phantom live birth or reject a later legitimate birth with the recycled PID. Missing or ambiguous lifetime evidence still fails closed.
5. `forced` records Job-wide escalation. `root_was_forced` records the root outcome separately: `False` means no Job force included the root, `True` means the root was observed active immediately before a successful Job force, and `None` is a legacy or unavailable observation. DAP terminal cleanup maps from this root fact, never from `forced` alone.
6. A query, member observation, or lifetime-reconciliation failure returns `FAILED`; an unsignaled process at the deadline returns `TIMED_OUT`. Neither permits pre-build continuation, producer-terminal evidence, or run-root cleanup.
7. Repeated callers join an in-flight operation. Only a proven `DRAINED` receipt memoizes completion; a later explicit force call may retry a non-drained outcome.
8. `aclose() -> OwnerDrainReceipt` keeps the Job, port, and process/member handles on failed close and retries the same owner. It releases them only after proven drain. `KILL_ON_JOB_CLOSE` is crash protection, not a substitute for a drain receipt.

### Collector-only synchronous process-capability capture

`WindowsOwnedProcess.launch(capture_process_handles=True)` is an opt-in for the
outer first-party coverage collector only. The default is `False`; adapter,
build-command, and nested owners retain their existing launch behavior. The
receipt and public MCP contracts do not change.

One owner thread performs `CreateProcess`, `WaitForDebugEvent`, and
`ContinueDebugEvent`. The collector adds `DEBUG_PROCESS`, not
`DEBUG_ONLY_THIS_PROCESS`, while preserving suspended admission, private Job
containment, restricted handle inheritance, and I/O-before-resume ordering.
Before continuing a process-creation event, duplicate its process handle as a
private capability and positively verify membership in this exact Job. Count
distinct process objects, not PIDs or handle values; the root is counted once.
Retain every lifetime capability until final exit proof. Close image/DLL file
handles and respect the system-owned debug process/thread handle lifecycle.

Let `C` be the distinct membership-qualified process-capability count and `T`
the final raw Job lifetime total. Collector-mode `DRAINED` requires `C == T`,
zero active processes, a signaled root and every retained member handle, and
no unresolved admission, capture, identity, or native API failure. Serialize
the final ledger/accounting observation. `T` can include failed associations;
equality is a conservative sufficient condition, not a theorem that every
increment represents an admitted member. A discrepancy, counter ambiguity,
capacity violation, or broken descendant debug chain remains non-drained.
Never subtract `TotalTerminatedProcesses` or forgive an unexplained gap.

In this mode, completion-port birth/retirement packets remain diagnostics,
not the authoritative capability ledger. Missing packets cannot override
complete direct-handle proof, and received packets cannot fill a missing
capability. Clause 4's notification reconciliation remains unchanged for the
default mode, including both ordinary and abnormal retirement handling.

Continue an exit debug event before waiting for its retained process handle.
Keep the pump operational through root exit, Job termination, cancellation,
and final handle waits. Forward application exceptions with
`DBG_EXCEPTION_NOT_HANDLED`; handle only identified debugger-initialization
events. Do not swallow failures, detach, disable debugger kill-on-thread-exit,
stop the pump before proven drain, or let the collector close private Job or
pump resources. Failed cleanup retains the owner and first causal error;
producer-terminal evidence, scanner completion, and run-root deletion stay
closed. Debugger/Job crash protection is not an exit receipt.

`drain_snapshot(receipt)` returns a serialized, bounded, secret-free diagnostic
value in either mode; it returns no handles and never independently admits
drain. The collector uses owner operations for cleanup rather than private
field manipulation.

Acceptance exercises the existing nested-Job fixture without its cooperative
barrier, delayed event consumption, exact-identity deduplication, missing
capabilities/count gaps, native capture/continue failures, suspended-child
termination, and cancellation joins. Adoption also requires the pinned
first-party provider's filtered compatibility replay and complete unfiltered
collection with unchanged DLL/PDB bytes and proven whole-tree drainage.
Spec 014's coverage scope, producer-terminal ordering, and strict Sonar policy
remain unchanged.

## Adapter and pre-build integration

```python
owner = session_manager.capture_prebuild_owner()
result = await build_manager.pre_launch_build(
    workspace_root=workspace_root,
    project_path=project_path,
    owner=owner,
)
```

`capture_prebuild_owner()` returns one of these values:

| Variant | Meaning | Required BuildManager action |
|---|---|---|
| `NoOwnedAdapter` | No current admitted adapter capability exists. | Do not select or discover any process. Continue to restore/build according to ordinary policy. |
| `OwnedAdapterCleanup` | A source client, current generation, and owner ref were captured. | Validate all three immediately before drain. Continue only after that owner's proven `DRAINED` receipt. |

A mismatched source client, generation, or owner ref returns `STALE`. It performs no disconnect, termination, or build command.

`BuildSession` retains a failed command owner and retries its drain before another command; `BuildManager` cannot discard that session while ownership remains. `DAPClient` retains a failed adapter owner and refuses a new adapter generation until same-owner stop recovery drains it. Neither path may turn a failed close into successful cleanup.

## Forbidden authority paths

The implementation must not add any of the following to launch, drain, retry, pre-build, or normal adapter stop:

- `taskkill`, WMI, `lsof`, `/proc`, `pkill`, image-name, program-name, output-directory, or PID discovery;
- a zero-return compatibility wrapper around an old selector;
- a singleton, global map, or ProcessRegistry lookup that retrieves an owner;
- direct `pywin32` use or a new dependency declaration;
- `BREAKAWAY_OK`, `SILENT_BREAKAWAY_OK`, leaked Job/process/thread handles, or an unbounded inherited-handle set; or
- a claim that a root PID, root exit, zero Job accounting, an advisory completion-port notification, or Job-handle close proves the tree drained.

The admitted Job may enumerate its own member PIDs to retain process handles. Those PIDs do not authorize a lookup or termination outside that Job.

## Observability and privacy

The boundary may report bounded owner ID, generation, root PID, admission stage, cleanup failure stage, forced markers, known root return code, active-process count, drain status, selected bounds, and Windows error code. `AdmissionCleanupError` may report that controlling handles are retained, but never their values. It must not emit raw handles, full environment values, command secrets, or unbounded stream content.

## Contract proof

The required evidence is O1 through O11 and C1 through C4 in [plan.md](../plan.md#deterministic-red-and-green-matrix). Fake Win32 cases prove ordering and failure behavior. A controlled real Windows production-path fixture proves two-owner isolation, adapter descendant admission, and accounting drain. This document does not replace that proof.