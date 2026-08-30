---
description: "Dependency-ordered program tasks for the one-public-release, five-wave v0.23.11 Issue #450 and Sonar remediation program"
---

# Tasks: v0.23.11 Issue #450 and Complete Sonar Remediation Program

**Input**: Design documents from `/specs/011-issue450-sonar-release-program/`  
**Prerequisites**: [spec.md](spec.md), [plan.md](plan.md), [research.md](research.md), [architecture.md](architecture.md), [data-model.md](data-model.md), and [quickstart.md](quickstart.md).  
**Status**: Every checkbox is intentionally unchecked. This task list plans future work; it records no completed wave, acceptance receipt, tag, or publication.  
**Scope boundary**: The parent owns contracts, dependencies, evidence rules, and v0.23.11 release intent. Child packets own source changes. The public Python/default route and stateless-preview boundary are immutable comparison surfaces, not parent mutation scope.

## Format: `[ID] [P?] [Wave] Description`

- **[P]**: May proceed in parallel only after its stated prerequisites and only with distinct file ownership.
- **[W#]**: Maps the task to one internal verified wave. `W1`–`W4` have `release_intent: none`; only `W5` has public `release_intent: v0.23.11`.
- Every task names its requirement anchors, exact owned files or fresh-manifest routing authority, and an observable acceptance checkpoint.
- Test/proof work precedes the implementation or release action that it guards. A task cannot infer a later result from this plan.

## Binding PRG Anchor Text

The statements below are copied from `spec.md` and `plan.md`; task descriptions cite these IDs rather than redefining them.

| ID | Binding statement |
|---|---|
| **PRG-001** | The program MUST deliver one public v0.23.11 release iteration through five internal verified waves. Waves 1–4 MUST have `release_intent: none`; Wave 5 alone may ship publicly. |
| **PRG-002** | Adapter stdout EOF, reader failure, process exit, explicit stop, and DAP terminal signals MUST converge through one guarded transport-death finalizer that prevents stale public `RUNNING`/live-debuggee state. |
| **PRG-003** | Pre-build cleanup MUST act only on an owner-scoped, pre-resume acknowledged process-tree capability; global image-name, directory-discovery, and unverified PID cleanup MUST be removed from the default path. |
| **PRG-004** | The exact-head runner MUST produce, validate, and import real Python and .NET coverage reports in the same scanner transaction while the existing `80` threshold remains unchanged. |
| **PRG-005** | Every current finding in the fresh complete project denominator MUST be repaired in source and reconciled to zero blocking findings and zero blocking hotspots. |
| **PRG-006** | Every release-critical Sonar observation and final publication decision MUST bind to the exact candidate or post-merge head that it claims to describe. |
| **PRG-007** | The public Python/default route and stateless-preview boundary MUST remain unchanged throughout Waves 1–4 and must be proven through the public surface at Wave 5. |
| **PRG-008** | The program MUST NOT weaken any gate through suppression, exclusion, baseline reset, accepted risk, false-positive/WONTFIX disposition, threshold/new-code change, or Sonar server-policy mutation. |
| **PRG-009** | After every complete diagnostic exact-head scan, the program MUST maintain a fresh manifest union assigning each blocking key to exactly one owner. |
| **PRG-010** | Decisions, child contracts, and public/internal documentation MUST distinguish observed facts from inferences and cite current primary GitHub or repository evidence. |

## Requirement Coverage

| Requirement | Tasks | Exact contract / source-routing authority | Observable acceptance owner |
|---|---|---|---|
| **PRG-001 — one public v0.23.11 release iteration / five internal verified waves** | T001–T002, T020–T022 | `specs/011-issue450-sonar-release-program/{spec.md,plan.md,tasks.md,data-model.md}`; `specs/019-v02311-issue450-sonar-release/{spec.md,plan.md,tasks.md}` | Wave 5 only |
| **PRG-002 — transport-death correctness** | T003–T006 | `specs/012-adapter-transport-death-lifecycle/{spec.md,plan.md,tasks.md,acceptance-receipt.md}`; `src/netcoredbg_mcp/dap/client.py`; `src/netcoredbg_mcp/session/{manager.py,state.py}`; `tests/{test_client.py,test_debuggee_liveness.py}` | Wave 1 |
| **PRG-003 — owner-scoped cleanup** | T007–T010 | `specs/013-owner-scoped-prebuild-cleanup/{spec.md,plan.md,tasks.md,research.md,acceptance-receipt.md}`; `src/netcoredbg_mcp/build/{cleanup.py,session.py}`; `tests/{test_build_cleanup.py,test_build_session.py}` | Wave 2 |
| **PRG-004 — cross-language coverage** | T011–T014 | `specs/014-sonarqube-cross-language-coverage/{spec.md,plan.md,tasks.md,research.md,acceptance-receipt.md}`; `scripts/run_sonarqube_exact_head.py`; `SonarQube.Analysis.xml`; `pyproject.toml`; `uv.lock`; `.config/dotnet-tools.json` | Wave 3 |
| **PRG-005 — complete current finding remediation** | T015–T019 | Fresh exact-head manifest union plus specs `015` through `018`; manifest-routed source under `src/netcoredbg_mcp/**`, `tests/**`, `scripts/**`, `bridge/**`, and `host/**` | Wave 4 / spec 018 |
| **PRG-006 — exact-head release** | T011–T014, T019–T022 | Exact-head secret-free receipts in the coordination-root `.agent/e/sonarqube/thebtf_netcoredbg_mcp/` evidence tree, named by their exact head and runner identity; `docs/RELEASE-PROTOCOL.md`; spec 019 | Waves 3–5 |
| **PRG-007 — route preservation** | T003–T022 | `pyproject.toml`; `src/netcoredbg_mcp/__main__.py`; child packet route-comparison sections; final installed consumer proof | All; final proof Wave 5 |
| **PRG-008 — no gate weakening** | T011–T022 | `SonarQube.Analysis.xml`; runner/policy receipt; child non-goals; `docs/RELEASE-PROTOCOL.md` | Waves 3–5 |
| **PRG-009 — fresh one-owner manifest** | T011–T019 | Wave-3 diagnostic receipt and Wave-4 manifest union/018 integration receipt | Waves 3–4 |
| **PRG-010 — GitHub-first research/documentation quality** | T001–T022 | Parent packet; each child’s `research.md`, `plan.md`, `tasks.md`, and exact evidence references | All |

## Phase 1: Program Freeze and Child Admission

**Purpose**: Freeze the one-public-release/five-wave contract before any child starts source work. This phase is planning work only and does not create a release or acceptance result.

- [ ] **T001 [P] [W1–W5]** Reconcile the parent anchors in `specs/011-issue450-sonar-release-program/{spec.md,plan.md,tasks.md,architecture.md,data-model.md,research.md,quickstart.md,checklists/requirements.md}` against the Governor cut: one public v0.23.11 release iteration, five internal verified waves, `release_intent: none` for Waves 1–4, and Wave 5 as sole shipping moment. **Requirements**: PRG-001, PRG-006, PRG-010. **Acceptance**: every child handoff can cite the same anchor wording without treating an internal wave as a release.
- [ ] **T002 [W1–W5]** Freeze the Wave-5 entry predicate in `specs/011-issue450-sonar-release-program/{plan.md,data-model.md,quickstart.md}` so no spec-019 execution task can enter before exact spec-018 integration closure and its fresh receipt identities agree. **Requirements**: PRG-001, PRG-005, PRG-006, PRG-008, PRG-009. **Acceptance**: the written predicate rejects a clean-looking but stale, partial, mismatched, or policy-weakened Wave-4 result.

## Phase 2: Wave 1 — Adapter Transport-Death Lifecycle (`release_intent: none`)

**Goal**: Repair the accepted stale-state mechanism without guessing why the historical adapter stdout closed or changing route/build-cleanup scope.

- [ ] **T003 [W1]** Re-enter design depth for `specs/012-adapter-transport-death-lifecycle/{spec.md,plan.md,tasks.md,research.md,data-model.md,quickstart.md,checklists/requirements.md}`. Bind deterministic RED, immutable bounded terminal record, stdout/stderr/process observers, one finalizer, diagnostics, one manager transition, DAP exited/terminated semantics, and route preservation to TD-001 through TD-010. **Requirements**: PRG-002, PRG-007, PRG-010. **Acceptance**: the child maps every TD anchor and parent PRG anchor to exact files and a future observable proof before source changes begin.
- [ ] **T004 [W1]** Add focused failing behavior tests in `tests/test_client.py` and `tests/test_debuggee_liveness.py` (or child-justified exact companion test files) for raw EOF from `RUNNING` with a retained debuggee PID, exited-without-terminated followed by EOF, terminal-signal races, bounded diagnostics, and public `get_debug_state` behavior. **Requirements**: PRG-002, PRG-007. **Acceptance**: current source demonstrates the stale public contradiction before the repair; tests do not infer an unknown producer cause.
- [ ] **T005 [W1]** Implement only the child-approved transport-to-manager terminal path in `src/netcoredbg_mcp/dap/client.py`, `src/netcoredbg_mcp/session/manager.py`, and `src/netcoredbg_mcp/session/state.py`, preserving bounded diagnostic facts and one guarded finalizer. **Requirements**: PRG-002, PRG-007. **Acceptance**: the focused red scenarios turn green with one terminal/unavailable publication and no `RUNNING`/live-debuggee claim after adapter death.
- [ ] **T006 [W1]** Perform the focused behavior/public-route proof and author the future exact-head Wave-1 closure artifact at `specs/012-adapter-transport-death-lifecycle/acceptance-receipt.md` only if it binds its exact wave SHA, red→green evidence, DAP semantic preservation, bounded diagnostics, and unchanged route/preview boundaries. **Requirements**: PRG-002, PRG-007, PRG-010. **Acceptance**: a future receipt is either exact and complete or Wave 1 remains open; it is never a tag or release.

## Phase 3: Wave 2 — Owner-Scoped Pre-Build Cleanup (`release_intent: none`)

**Goal**: Remove default cross-owner cleanup without reopening the Issue #450 causal investigation.

- [ ] **T007 [W2]** Re-enter design depth for `specs/013-owner-scoped-prebuild-cleanup/{spec.md,plan.md,tasks.md,research.md,data-model.md,quickstart.md,checklists/requirements.md}` after Wave-1 exact closure. Read the carried Win32 ownership research and define the smallest private pre-resume owner capability; explicitly reject image-name, directory, post-spawn PID, and unverified discovery ownership. **Requirements**: PRG-003, PRG-008, PRG-010. **Acceptance**: the child distinguishes a retained process capability from a selector/snapshot and names the fail-closed admission path.
- [ ] **T008 [W2]** Add failing two-owner and admission-failure behavior tests in `tests/test_build_cleanup.py` and `tests/test_build_session.py` before source changes. Cover no `taskkill /F /IM netcoredbg.exe` default route, no foreign owner selection, pre-resume admission, graceful deadline, forced owner-tree drain, and no unsafe fallback. **Requirements**: PRG-003, PRG-007, PRG-008. **Acceptance**: current default behavior is shown unsafe in the controlled proof and every required owner-safety condition is observable.
- [ ] **T009 [W2]** Implement the child-approved owner-scoped cleanup transition in `src/netcoredbg_mcp/build/cleanup.py` and `src/netcoredbg_mcp/build/session.py`; delete obsolete default global image-name/PID/discovery paths once callers migrate. **Requirements**: PRG-003, PRG-007, PRG-008. **Acceptance**: the two-owner proof preserves the foreign adapter, selected-tree drain is observed, and admission failure remains fail-closed.
- [ ] **T010 [W2]** Perform focused two-owner/public-route proof and author the future exact-head Wave-2 closure at `specs/013-owner-scoped-prebuild-cleanup/acceptance-receipt.md` only if it binds exact SHA, owner admission, no foreign termination, drain behavior, and no route-preview change. **Requirements**: PRG-003, PRG-007, PRG-008, PRG-010. **Acceptance**: Wave 2 has exact internal closure or remains open; it does not create a public shipping moment.

## Phase 4: Wave 3 — Exact-Head Cross-Language Coverage (`release_intent: none`)

**Goal**: Make the existing exact-head scanner transaction produce/import valid Python and .NET coverage under unchanged gate policy.

- [ ] **T011 [W3]** Re-enter design depth for `specs/014-sonarqube-cross-language-coverage/{spec.md,plan.md,tasks.md,research.md,data-model.md,quickstart.md,checklists/requirements.md}` after Wave-2 exact closure. Re-read the current runner, analysis XML, Python configuration, tool-lock state, Sonar/coverage primary sources, and release protocol. **Requirements**: PRG-004, PRG-006, PRG-008, PRG-009, PRG-010. **Acceptance**: the child names deterministic report paths, same-transaction provenance, nonzero denominator checks, and the unchanged policy authority without a scanner replacement.
- [ ] **T012 [W3]** Add failing targeted validation tests for report absence, empty/zero denominator, stale/out-of-transaction artifact, unmapped source, wrong head, report-import omission, and generated-artifact cleanup in the exact files selected by child 014. **Requirements**: PRG-004, PRG-006, PRG-008. **Acceptance**: no malformed coverage artifact can be mistaken for a valid analysis input.
- [ ] **T013 [W3]** Implement only the child-approved coverage transaction/configuration changes in `scripts/run_sonarqube_exact_head.py`, `SonarQube.Analysis.xml`, `pyproject.toml`, `uv.lock`, and `.config/dotnet-tools.json` when required by the selected local tool. **Requirements**: PRG-004, PRG-006, PRG-008. **Acceptance**: the runner generates deterministic Python/.NET reports after begin and before end, validates them, imports them through the committed configuration, and does not alter threshold/new-code/server policy.
- [ ] **T014 [W3]** Run the child-defined clean exact-head diagnostic transaction and author the future Wave-3 closure at `specs/014-sonarqube-cross-language-coverage/acceptance-receipt.md` only if report provenance/import, complete paging, unchanged policy, exact revision binding, and nonzero denominators are recorded. **Requirements**: PRG-004, PRG-006, PRG-008, PRG-009, PRG-010. **Acceptance**: coverage evidence is analysis-bound; unresolved global findings remain explicitly blocking and Wave 3 remains `release_intent: none`.

## Phase 5: Wave 4 — Complete Current-Finding Remediation (`release_intent: none`)

**Goal**: Repair every key in the fresh complete current denominator and reconcile the full project to a zero-blocking exact integration head.

- [ ] **T015 [W4]** From the fresh Wave-3 diagnostic receipt, generate and freeze one manifest union at the Wave-4 evidence location, assigning every blocking key exactly once to `015` Python, `016` bridge, or `017` host. Record each key’s current component/path/rule and owner; do not use historical partition counts. **Requirements**: PRG-005, PRG-008, PRG-009, PRG-010. **Routing authority**: the fresh fully paginated exact-head receipt, not a guessed file list. **Acceptance**: every current blocking key is owned exactly once; zero missing/duplicate/accepted/suppressed keys exist in the union.
- [ ] **T016 [P] [W4]** Re-enter and execute the child packet `specs/015-sonar-python-current-findings/{spec.md,plan.md,tasks.md,research.md,acceptance-receipt.md}` for its manifest-owned paths under `src/netcoredbg_mcp/**`, `tests/**`, and `scripts/**` excluding Wave-3 scanner ownership. Add behavior-first regression proof before each source correction. **Requirements**: PRG-005, PRG-007, PRG-008, PRG-009, PRG-010. **Acceptance**: each assigned key is absent or `FIXED_IN_CURRENT_HEAD` on child evidence while public-route behavior remains preserved.
- [ ] **T017 [P] [W4]** Re-enter and execute the child packet `specs/016-sonar-bridge-current-findings/{spec.md,plan.md,tasks.md,research.md,acceptance-receipt.md}` for its manifest-owned `bridge/**` paths. Add behavior-first regression proof before each source correction. **Requirements**: PRG-005, PRG-008, PRG-009, PRG-010. **Acceptance**: each assigned key is absent or `FIXED_IN_CURRENT_HEAD` on child evidence with no suppression/exclusion path.
- [ ] **T018 [P] [W4]** Re-enter and execute the child packet `specs/017-sonar-host-current-findings/{spec.md,plan.md,tasks.md,research.md,acceptance-receipt.md}` for its manifest-owned `host/**` paths. Add behavior-first regression proof before each source correction. **Requirements**: PRG-005, PRG-008, PRG-009, PRG-010. **Acceptance**: each assigned key is absent or `FIXED_IN_CURRENT_HEAD` on child evidence with no unrelated route migration.
- [ ] **T019 [W4]** Re-enter and execute `specs/018-sonar-zero-finding-integration/{spec.md,plan.md,tasks.md,acceptance-receipt.md}` after T016–T018 exact child closures. Refresh the full manifest union and run the bounded integration evidence only; return any new/missing/duplicate current key to its proper 015/016/017 owner. **Requirements**: PRG-005, PRG-006, PRG-008, PRG-009, PRG-010. **Acceptance**: a future `acceptance-receipt.md` binds 015/016/017 closures and one exact integration SHA with complete current/new-code/hotspot pagination, zero blocking findings/hotspots, `new_violations=0`, unchanged-threshold coverage `OK`, and quality gate `OK`.

## Phase 6: Wave 5 — v0.23.11 Release (`release_intent: v0.23.11`)

**Goal**: Make the sole public shipping moment only from the exact clean Wave-4 integration head; no new product work enters this phase.

- [ ] **T020 [W5]** Enforce the Wave-5 entry barrier in [plan.md](plan.md#wave-5-entry-barrier) before opening or executing source/release work in `specs/019-v02311-issue450-sonar-release/`. Validate the future 018 closure against its fresh exact-head diagnostic receipt and reject any source-byte drift. **Requirements**: PRG-001, PRG-005, PRG-006, PRG-008, PRG-009. **Acceptance**: the release path either has a matching exact Wave-4 closure or remains blocked at the entry boundary; no tag/preparation action substitutes for it.
- [ ] **T021 [W5]** Re-enter design depth for `specs/019-v02311-issue450-sonar-release/{spec.md,plan.md,tasks.md,research.md,data-model.md,quickstart.md,checklists/requirements.md}` after T020. Bind final source/build/package evidence, public Python/default installed-consumer journey, candidate/post-merge scans, annotated tag, publication, and canary to exact release identities. **Requirements**: PRG-001, PRG-006, PRG-007, PRG-008, PRG-010. **Acceptance**: the release child has no new product-scope task and names every required evidence handoff.
- [ ] **T022 [W5]** Execute only the accepted release protocol from the exact Wave-4 integration head: candidate scan, review/gates, merge, fresh post-merge scan, annotated tag, publication, installed consumer proof, and post-publication canary; record the future result in `.agent/reports/release-v0.23.11.md`. **Requirements**: PRG-001, PRG-005, PRG-006, PRG-007, PRG-008, PRG-010. **Acceptance**: the sole public v0.23.11 shipping record binds tag target, candidate/post-merge receipts, public Python/default consumer result, publication, and canary; a failed gate returns to the owning prior wave instead of weakening policy.

## Dependencies and Execution Order

### Hard dependencies

1. T001 and T002 establish the only parent cut.
2. T003–T006 close Wave 1 before T007 begins Wave 2.
3. T007–T010 close Wave 2 before T011 begins Wave 3.
4. T011–T014 close Wave 3 and create the fresh manifest authority before T015 begins Wave 4.
5. T015 precedes the parallel source children T016–T018; their exact closures precede T019.
6. **T019 is the only predecessor of T020.** No Wave-5 release work may begin until T020 proves the exact Wave-4 barrier.
7. T021 follows T020; T022 follows accepted 019 planning and all current release-gate prerequisites.

### Within child work

- Child research and the exact requirement-to-files map precede RED tests.
- Deterministic RED behavior precedes the code/configuration that makes it green.
- A child’s future acceptance artifact is written only after its exact head and focused proof exist; it never authorizes a later changed head.
- A new Sonar key never becomes an implicit exception: it returns to T015’s union and the appropriate T016/T017/T018 owner.
- A change that threatens the public Python/default route or stateless-preview boundary stops that child and returns to parent architecture; it does not expand silently.

### Parallel opportunities

- T016, T017, and T018 may run in parallel only after T015’s one-owner union is frozen and each child owns disjoint manifest paths.
- Child research for 015/016/017 may be performed in parallel after manifest assignment, but source edits may not overlap an owned path.
- No Wave 1/2/3 task is parallel with a later wave because their evidence contracts are direct prerequisites.
- Wave 5 is never parallel with Wave 4 integration closure or a subsequent source correction.

## Implementation Strategy

### First executable slice

The first executable slice is Wave 1 only: design child 012, write deterministic RED tests, make the narrow transport-to-manager transition, prove focused public behavior, then bind an exact internal closure. It is intentionally not a cleanup, coverage, Sonar, tag, or release slice.

### Program progression

1. Make adapter death truthful and diagnosable.
2. Make pre-build cleanup owner-safe.
3. Measure the real C# and Python code under the existing exact-head scanner.
4. Use fresh manifest-driven partitions to eliminate the full current denominator.
5. Ship once only after exact Wave-4 closure and final consumer/evidence gates.

### Prohibited shortcuts

- Do not release Wave 1 or 2 as a workaround for Sonar.
- Do not mark a finding accepted, false-positive, WONTFIX, ignored, excluded, baselined, or outside a changed new-code period.
- Do not treat a dashboard/latest analysis or old receipt as exact-head evidence.
- Do not turn an owner selector into a process-ownership claim.
- Do not use a focused test as a substitute for the required Wave-5 customer-mode proof.
