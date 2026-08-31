# Wave 2 acceptance receipt: owner-scoped pre-build cleanup

## Receipt identity

- **Outcome:** `ACCEPTED_INTERNAL_WAVE`
- **Release intent:** `none`
- **Parent program:** `specs/011-issue450-sonar-release-program/`
- **Wave contract:** `specs/013-owner-scoped-prebuild-cleanup/`
- **Source base:** `1b8b2d548a45b17dde690b4cb8e4fc7153d326bc`
- **Accepted candidate:** `8f3b3b3b7c05f01736070740570567acb30f23c7`.
- **Initial independent exact check:** `agent://CheckWave2ExactCandidate` returned `PASS` for predecessor implementation `480b67509fdfd996556d7954b736c46361025b2d`.
- **PR correction verification:** `agent://VerifyWave2PrCorrection` returned `VERIFIED` for successor `598139418a40f0dc0fb07d204ac60864de104c49`.
- **Lifecycle verification:** `agent://VerifyWave2LatestFix` returned `VERIFIED` for successor `a04e9384902493264a447faa1cd9195f63db56ed`.
- **Review verification:** `agent://VerifyWave2FinalFix` returned `VERIFIED` for successor `baf2ab24f242ccd0b1b8db8a36d5efbd774b32e5`.
- **Gate verification:** `agent://VerifyWave2FinalGateCandidate` returned `VERIFIED` for successor `209d151f9f9e0cba3fc5b6740196452611060a62`.
- **Process verification:** `agent://VerifyWave2FinalImplementation` returned `VERIFIED` for successor `eeeb1b2655bcab6336b86068be5d205508d512ce`.
- **Final stop-evidence verification:** `agent://VerifyWave2StopEvidence` returned `VERIFIED` with no findings for `8f3b3b3b7c05f01736070740570567acb30f23c7`.
- **Definitive exact judgment:** `agent://JudgeWave2DefinitiveFinal` returned `ACCEPT` for exact candidate `8f3b3b3b7c05f01736070740570567acb30f23c7`.

This receipt closes Wave 2 internally. It does not authorize Wave 3 implementation, a tag, release, package publication, Sonar waiver, route cutover, or a claim that global process cleanup caused either historical issue #450 incident.

## User outcome

Pre-build no longer searches the machine for a process that looks related to the build. On Windows, every adapter and every build command is admitted into its own private Job before it executes. A pre-build operation receives either no owner or one immutable capability for the exact current adapter generation. It drains only that owner, verifies `ActiveProcesses == 0`, and then permits restore/build. A stale, failed, timed-out, or nonzero receipt starts no build command.

The former `taskkill`, WMI, PID, image-name, basename, output-directory, `lsof`, `/proc`, `pkill`, and `psutil` selection authority was deleted rather than wrapped. `ProcessRegistry` remains observational and supports explicitly separate public cleanup/shutdown compatibility; normal stop and pre-build do not use it as process-tree authority.

## Accepted architecture

`src/netcoredbg_mcp/windows_process_owner.py` is the only Windows process-tree authority for this wave. Its authority comes from retained direct handles and an unnamed private Job, not from `OwnedProcessRef` fields.

The admission sequence is:

1. create and configure an unnamed Job with kill-on-close crash protection;
2. create the child suspended and retain process and primary-thread handles;
3. assign the process to the Job;
4. verify Job membership and initial accounting;
5. wire only the intended standard handles to the child;
6. call `ResumeThread` last.

Any failure before resume cleans retained resources and never deliberately resumes the child. Resume failure terminates and drains the admitted Job. No breakaway flag, pywin32 dependency, global owner map, registry-backed capability, or unowned Windows asyncio fallback was added.

`DAPClient._DapRun` owns one adapter capability and retains its existing Wave-1 finalizer. `BuildSession` creates a fresh capability per command. `SessionManager` captures `NoOwnedAdapter | OwnedAdapterCleanup`; `BuildManager.pre_launch_build()` requires that sum type before it creates a session or starts restore/build.

## RED baseline

The retained 15-row behavior-first matrix failed against the merged Wave-1 base for the intended mechanisms:

- adapter and build children executed before ownership admission could be verified;
- assignment, membership/accounting, and resume refusal lacked a pre-execution fail-closed boundary;
- adapter and command root cleanup could leave descendants unproven;
- timeout, session cancellation, and outer cancellation could complete with live descendants;
- command completion had no zero-accounting requirement;
- a PID-only registry row could authorize termination;
- global selectors could terminate owner B and a foreign sentinel;
- default and retry pre-build reached selector cleanup;
- public/host/installed routes preserved the unsafe private implementation.

The exact combined RED command produced `20 failed` covering O1 through O11 and C1 through C4, with additional O1, O7, and O11 subcases. None failed only because a planned symbol or module was absent.

## GREEN matrix

The exact candidate owner matrix completed:

```powershell
uv run --locked --extra dev python -m pytest -q `
  tests/test_windows_process_owner.py `
  tests/test_client.py `
  tests/test_session.py `
  tests/test_build_cleanup.py `
  tests/test_build_session.py `
  tests/test_build_manager.py `
  tests/test_process_registry.py
```

Result: `190 passed`.

The independent exact checker additionally re-ran bounded controls and reported:

- combined owner matrix: `203/203`;
- `tests/test_client.py`: `55/55`;
- `tests/test_build_session.py`: `27/27`;
- `tests/test_process_registry.py`: `5/5`;
- `tests/test_host_proxy.py`: `13/13`;
- `tests/critical/test_release_critical.py`: `8/8`.

### O1 through O4: admission

The fake Win32 seam proves exact ordering and refusal behavior. Assignment, membership/accounting, and resume failure do not install a command owner or expose an executing unadmitted child. The retained process, thread, Job, and pipe resources are closed by the admission failure path.

### O5 and O6: adapter drain

A controlled real `OwnerScopeAdapter.exe` runs through the production `DAPClient` launch path. The test consumes framed DAP output and stderr, observes its controlled descendant inside the retained Job, stops through the Wave-1 finalizer, records `DRAINED` with `active_processes == 0`, and observes the descendant disappear.

### O7 and O8: command cleanup

Normal completion, build timeout, `BuildSession.cancel()`, real outer task cancellation, and accounting failure are covered. Timeout and cancellation preserve their original outcome only after the exact command owner records a drain result. A non-drained receipt cannot be represented as command completion.

### O9 through O11: authority contraction

A PID-only registry row cannot create an owner capability. The real pre-build journey starts two independent adapter owners plus a same-image foreign sentinel, captures owner A through `SessionManager`, drains A to zero, starts the build, and proves owner B and the sentinel survive. Default and lock-retry pre-build perform no discovery; each retry starts a fresh command capability.

### C1 through C4: compatibility

Direct Python/default start and restart keep their existing public inputs while using the new private handoff. Direct-versus-host parity is preserved. The focused installed-wheel test builds and installs the candidate, proves the CLI/version route, confirms `BuildManager.pre_launch_build.owner` is required and keyword-only, confirms the public `DAPClient` constructor remains unchanged, confirms the owner type is not exported from the package root, and confirms the deleted selectors are not exported.

The broad typed BitBlt UI journey is not the C3 owner gate: its recorded launches use `pre_build=False` and its contradictory failures occurred in foreground-sensitive UI tree discovery. It was replaced with the focused installed owner-contract gate rather than treating unrelated UI reliability as proof of process ownership.

## Selector and registry proof

A scoped source scan found no active `taskkill`, WMI, PID/image/directory selector, `cleanup_for_build`, `kill_debugger_processes`, `kill_processes_in_directory`, `lsof`, `/proc`, `pkill`, or normal `ProcessRegistry.cleanup_all()` path in the pre-build/normal-stop implementation.

The old selector functions, flags, exports, tests, and comments were deleted. Normal `SessionManager.stop()` joins the retained adapter finalizer, then unregisters the exact observed registry rows without reopening their PIDs as authority.

## Static and dependency proof

The candidate passed scoped Ruff, mypy, compileall, and diff checks. `pyproject.toml` and `uv.lock` are unchanged from the source base. No direct pywin32 dependency or public owner API was added.

The documentation audit `agent://AuditWave2Comments` retained the load-bearing authority, admission-order, accounting, generation-fence, cancellation, and registry rationale. Material improvements added an explicit warning against restoring selector cleanup and preserved security/output-bound reasons at the command path.

## Independent review chain

- `agent://CheckWave2AdapterOwner`: PASS for adapter admission, owner/finalizer binding, real descendant inheritance, and zero-accounting drain.
- `agent://CheckWave2BuildContainment`: PASS for fresh command capabilities, cancellation preservation, and O1-O4/O7/O8.
- `agent://CheckWave2PrebuildCutover`: PASS for owner capture, BuildManager gate, selector deletion, registry non-authority, and compatibility.
- `agent://ReviewWave2Candidate`: incomplete no-probe review. Its lack-of-reverification concern did not identify a product defect and was superseded by the next exact checker.
- `agent://CheckWave2ExactCandidate`: PASS after reading every named source owner and independently running the bounded owner, host, release-critical, selector, and static evidence.
- PR #289 review found seven unique correction criteria: child-PATH executable resolution, failed drain preservation, later force escalation after a non-drained receipt, explicit Windows-path testing, host pre-build invocation proof, cross-platform fake seam support, and current receipt wording. Commit `598139418a40f0dc0fb07d204ac60864de104c49` fixes them; all eight review threads are resolved.
- `agent://VerifyWave2PrCorrection`: independently verified all seven criteria against exact diff `711a4a5..5981394` with no contradictions or findings. Two canonical checker attempts and one generic reviewer attempt failed before execution due provider 429; those automation failures do not supersede the completed verifier evidence.
- A final PR pass found five evidence-lifetime defects: active admission could appear ownerless; timeout/cancellation could outlive a failed drain; final close could produce a newer receipt; terminal PID observations could remain registered; partial pipe allocation could leak handles. Commit `a04e9384902493264a447faa1cd9195f63db56ed` fixes all five with retained RED/GREEN regressions.
- `agent://VerifyWave2LatestFix`: independently verified all five final mechanisms against exact source/tests with no contradictions. The real fixture rerun remained locally blocked by a pre-existing foreign OwnerScopeAdapter process and was not PID-killed; these focused regressions do not depend on that residue.
- A final review found graceful-disconnect ordering, overlapping admission ownership, and the `aclose()` return contract. Commit `baf2ab24f242ccd0b1b8db8a36d5efbd774b32e5` fixes them with retained RED/GREEN regressions.
- `agent://VerifyWave2FinalFix`: independently verified all three exact final corrections with no contradictions.
- A final concurrency review required one manager-owned lifecycle gate across adapter admission and the complete owner-drain/restore/build interval, plus preservation of a captured owner receipt across rebuild restart from both running and terminal states. Commits `11e8f1eb4b1aaffbce7a10e1da3468f2d03f222a` and `209d151f9f9e0cba3fc5b6740196452611060a62` implement that cutover.
- `agent://VerifyWave2FinalGateCandidate`: verified lock coverage, running/terminal restart receipt preservation, fail-closed build gating, absence of recursive lock deadlock, and public route continuity for the exact final candidate.
- A final process-semantics correction distinguishes a live `STILL_ACTIVE` sentinel from a terminated root whose literal exit code is 259, while preserving typed fail-closed admission cleanup. Commits `3b99dfcc354dd784a7d15dc123a27888fc06ec06` and `eeeb1b2655bcab6336b86068be5d205508d512ce` implement those final bytes.
- `agent://VerifyWave2FinalImplementation`: verified all combined lifecycle/process claims for exact final implementation `eeeb1b2655bcab6336b86068be5d205508d512ce` with no findings.
- The definitive stop/attach correction preserves terminal adapter and debuggee observations, serializes the complete attach lifecycle, retains owner/generation on cancelled stop, returns and validates ordinary-stop receipts, and preserves failed pre-build receipts before generic stop. Commit `8f3b3b3b7c05f01736070740570567acb30f23c7` contains those final implementation bytes.
- `agent://VerifyWave2StopEvidence`: exact successor `VERIFIED`, 6/6 claims and no findings.
- `agent://JudgeWave2DefinitiveFinal`: definitive exact `ACCEPT`, authorizing this receipt and tracked closure artifact for `8f3b3b3b7c05f01736070740570567acb30f23c7` only.

## Requirement closure

- **WOC-001:** 15-row RED-to-GREEN matrix retained with nonzero denominators.
- **WOC-002:** one private Windows owner boundary, suspended admission, resume last.
- **WOC-003:** one owner per `_DapRun`; Wave-1 finalizer remains sole adapter cleanup/publication path.
- **WOC-004:** one fresh owner per Windows build command; normal/timeout/cancel paths drain.
- **WOC-005:** explicit generation-bound pre-build owner; BuildManager gate precedes commands.
- **WOC-006:** selector authority deleted with no compatibility shim.
- **WOC-007:** only `DRAINED` plus literal zero accounting permits continuation.
- **WOC-008:** pre-resume and cancellation failures fail closed while preserving original outcomes.
- **WOC-009:** ProcessRegistry is non-authoritative for normal/pre-build cleanup.
- **WOC-010:** explicit safe Win32 signatures, private handles, intended inheritance, no breakaway/dependency leak.
- **WOC-011:** public Python/default, host, installed CLI, dependency, Sonar, coverage, and release boundaries preserved.
- **WOC-012:** load-bearing ownership and failure rationale is documented at the source seams.
- **WOC-013:** exact final candidate `8f3b3b3b7c05f01736070740570567acb30f23c7`, the complete independent checker/verifier chain, definitive exact judgment, and this delayed non-release receipt are present.

## Handoff

Wave 2 is closed internally. Wave 3 remains a separate child under the parent program and may begin only through its own architect/specify pipeline. Nothing in this receipt changes the still-RED Sonar release authority or permits v0.23.11 publication.
