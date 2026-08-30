# Feature Specification: v0.23.11 Issue #450 and Complete Sonar Remediation Program

**Feature Branch**: `work/issue450-eof-sonar-remediation`  
**Created**: 2026-08-30  
**Source baseline**: `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`  
**Status**: Planned program contract — this packet asserts no implementation, remediation, acceptance, tag, or publication result.  
**Authority**: Governor decision `agent://Issue450SonarGovernor`; baseline Sonar receipt `.agent/e/sonarqube/thebtf_netcoredbg_mcp/e95223ba1bddd7a08e440e4a0eca3db9f3c068b9/post-merge.json`; Issue #450 investigation `.agent/runs/issue450-adapter-eof-lifecycle/investigation.md`.

## Program Decision

This is one D3 program with **one public v0.23.11 release iteration** and **five internal verified waves**. Waves 1–4 have `release_intent: none`: they close only on their exact-head acceptance evidence and are not releases, tags, or public shipping moments. Wave 5 is the sole shipping moment. This wording is the PRG-001 shippability constraint, not a waiver of any release gate.

The baseline receipt is a failed but valid exact-head observation: 1,121 current issues, 1,076 blocking dispositions, 45 fixed dispositions, 172 new violations, new coverage `0.0` against the unchanged `80` threshold, and zero hotspots. It is a starting denominator, not evidence that any planned outcome has been achieved.

## Clarifications

### Governing cut

- Wave 1 addresses the source-proven Issue #450 stale-state mechanism only: adapter transport death reaches `DAPClient` but not `SessionManager`, allowing `RUNNING` and `debuggeeAlive=true` to remain visible after the adapter is dead.
- Wave 2 repairs the independently proven foreign-owner risk from default image-name pre-build cleanup. It does not claim that this mechanism caused either recorded EOF incident.
- Wave 3 establishes real, same-transaction Python and .NET coverage import for the exact-head runner without changing Sonar policy.
- Wave 4 remediates the complete fresh current finding denominator through three disjoint repair children and one integration child.
- Wave 5 may not begin until the Wave-4 integration closure is exact and clean as defined in [plan.md](plan.md#wave-5-entry-barrier).

### Immutable scope boundaries

- The public Python package, `netcoredbg-mcp` command, default route, and rollback route remain public compatibility authorities.
- The stateless-preview boundary remains unchanged.
- No suppression, `NOSONAR`, exclusion, baseline reset, WONTFIX/FALSE-POSITIVE/accepted-risk disposition, coverage-threshold change, new-code-definition change, or Sonar server-policy weakening is in scope.
- The historical cause of the adapter's stdout closure is unknown. The program must not invent it or treat any unobserved producer theory as an Issue #450 fix.

## User Scenarios & Testing *(mandatory)*

### User Story 1 — Receive truthful state after adapter transport death (Priority: P1)

A debugger user sees an adapter disappear after a run has become visible. They must no longer be told that the debuggee is still running when the adapter is no longer available; the terminal outcome must retain bounded diagnostics that can support a later producer-cause investigation.

**Why this priority**: Issue #450 is user-visible correctness failure: stale public state sends users toward commands that can only fail against the dead adapter.

**Independent Test**: A focused fake subprocess starts the manager in `RUNNING` with a debuggee PID, produces raw stdout EOF without a DAP `terminated` event, and observes one terminal or unavailable public state, no claimed live debuggee, promptly failed pending work, and bounded terminal diagnostics. A separate sequence replays `process → continued → exited` without `terminated`, then transport death, and preserves the DAP semantic distinction.

**Acceptance Scenarios**:

1. **Given** a running legacy Python debug session with a retained debuggee PID, **When** the adapter stdout reaches EOF, **Then** the public state is not `RUNNING`, `debuggeeAlive` is false or explicitly unavailable, and resource observers receive one terminal transition.
2. **Given** DAP `exited` is received without DAP `terminated`, **When** the transport subsequently dies, **Then** the stored exit fact remains distinct from protocol termination and the public state does not claim a live debugger.
3. **Given** stdout EOF, a reader fault, a process exit, and an explicit stop race, **When** they converge, **Then** exactly one guarded finalizer owns cleanup and manager publication.

**Maps to**: PRG-002, PRG-007, PRG-010. **Child**: `specs/012-adapter-transport-death-lifecycle/`.

---

### User Story 2 — Preserve another owner’s debug adapter during pre-build cleanup (Priority: P1)

Two developers or sessions own independent adapters on the same machine. One starts a pre-build cleanup. The other owner’s adapter and descendant process tree must remain untouched.

**Why this priority**: Selecting every `netcoredbg.exe` by image name is not an ownership proof; a foreign session must not pay for another owner’s cleanup.

**Independent Test**: A two-owner command-capture/process fixture proves that default pre-build cleanup selects only an acknowledged owner capability, never emits `taskkill /F /IM netcoredbg.exe`, fails closed when pre-resume ownership admission fails, and observes graceful or forced drain only for the selected tree.

**Acceptance Scenarios**:

1. **Given** two independent adapter owners, **When** owner A invokes pre-build cleanup, **Then** owner B’s adapter and descendants remain available and unselected.
2. **Given** the selected adapter cannot be admitted to its private ownership boundary before resume, **When** launch or cleanup is attempted, **Then** the unadmitted child is not allowed to run and no PID/image-name fallback is used.
3. **Given** graceful cleanup exceeds its bounded deadline, **When** forced cleanup is necessary, **Then** only the owner’s retained tree capability is terminated and the tree-drain observation completes before cleanup is reported finished.

**Maps to**: PRG-003, PRG-007, PRG-008, PRG-010. **Child**: `specs/013-owner-scoped-prebuild-cleanup/`.

---

### User Story 3 — Measure both shipped language surfaces in one exact-head scan (Priority: P1)

A quality owner needs a Sonar analysis that measures the Python and .NET code actually built and tested from one captured source head. They must be able to distinguish a valid imported report from a stale, empty, externally supplied, or wrong-head report.

**Why this priority**: The baseline’s `new_coverage=0.0` cannot be remediated honestly by changing a policy threshold or using a non-proven report path.

**Independent Test**: In a clean exact-head scanner worktree, the Wave-3 transaction generates deterministic Python and .NET Cobertura XML reports after scanner begin and before scanner end, verifies both are nonempty and tied to the captured head, imports them through the committed analysis configuration, and records analysis-bound coverage evidence. A report substitution, missing file, zero denominator, or unmapped source path is rejected.

**Acceptance Scenarios**:

1. **Given** an exact captured source head, **When** the coverage transaction runs, **Then** exactly the deterministic report paths configured for Python and .NET are generated in that transaction before scanner end.
2. **Given** either report is absent, stale, empty, unmapped, or produced outside the transaction, **When** the scanner would end, **Then** the wave remains open rather than treating coverage as imported.
3. **Given** both reports are valid, **When** Sonar evaluates the submitted analysis, **Then** the unchanged `80` coverage condition is evaluated against that analysis rather than a latest-project result.

**Maps to**: PRG-004, PRG-006, PRG-008, PRG-009, PRG-010. **Child**: `specs/014-sonarqube-cross-language-coverage/`.

---

### User Story 4 — Remove every current finding without hiding any denominator (Priority: P1)

A maintainer needs every finding in the current exact-head project inventory to have one and only one repair owner. When a scan discovers a new key during remediation, it must become an owned item in the same Wave-4 union rather than disappear into a stale partition count.

**Why this priority**: The program goal is a clean current denominator, not a clean subset or a historical-count claim.

**Independent Test**: A fresh complete diagnostic inventory is paginated and reconciled into a one-owner manifest. The Python, bridge, and host children each prove their own repaired keys absent or `FIXED_IN_CURRENT_HEAD`; the integration child proves the union has no duplicate, missing, suppressed, or accepted key and that the fresh integration analysis has no blocking finding or hotspot.

**Acceptance Scenarios**:

1. **Given** a fresh complete current inventory, **When** the Wave-4 manifest is sealed for repair, **Then** every blocking key maps to exactly one of the Python, bridge, or host repair owners.
2. **Given** a child correction creates or reveals a new key, **When** the next exact-head diagnostic scan completes, **Then** the new key is added to the existing one-owner union and routed to its owner before Wave 4 can close.
3. **Given** a proposed Wave-4 closure, **When** its integration receipt is evaluated, **Then** any duplicate owner, unowned key, accepted/suppressed key, incomplete page, current-analysis mismatch, current violation, or hotspot prevents closure.

**Maps to**: PRG-005, PRG-008, PRG-009, PRG-010. **Children**: `specs/015-sonar-python-current-findings/`, `specs/016-sonar-bridge-current-findings/`, `specs/017-sonar-host-current-findings/`, and `specs/018-sonar-zero-finding-integration/`.

---

### User Story 5 — Install one exact, clean public release (Priority: P1)

A consumer needs v0.23.11 to contain the Issue #450 repair and owner-safe cleanup while preserving the public Python/default route. The release owner must not tag or publish a candidate whose full exact-head evidence is incomplete or whose source bytes differ from the proven candidate.

**Why this priority**: The five internal waves create value only when the final customer-visible package is proven from the same clean integration head.

**Independent Test**: The release process uses the existing public package/CLI/MCP surface from a built and installed candidate, completes the stated consumer journeys, binds candidate and post-merge Sonar receipts to their exact heads, verifies the annotated tag target equals the post-merge head, and performs the post-publication canary. This is future Wave-5 evidence, not evidence created by this packet.

**Acceptance Scenarios**:

1. **Given** a Wave-4 integration closure, **When** Wave 5 is considered for entry, **Then** its closure SHA and fresh diagnostic receipt identity must match before any release-preparation or tag action can start.
2. **Given** a release candidate changes after its evidence is collected, **When** release work resumes, **Then** the old evidence is not reused and the candidate re-enters the owning earlier wave.
3. **Given** the final v0.23.11 package is installed by a consumer, **When** they use the public Python/default journey, **Then** the route remains supported and the release evidence binds the result to the annotated public tag.

**Maps to**: PRG-001, PRG-006, PRG-007, PRG-008, PRG-010. **Child**: `specs/019-v02311-issue450-sonar-release/`.

## Edge Cases

- A normal adapter exit, crash, foreign termination, malformed frame, or pipe closure can all reach the same Wave-1 transport-death seam; none may be labelled as the historical producer cause without new observed evidence.
- A DAP `terminated` event does not prove a debuggee exit, and DAP `exited` does not prove debugger-session termination; Wave 1 must retain both facts separately.
- A process PID can be reused after its process object is gone; Wave 2 must not turn PID/image-name discovery into an ownership claim.
- A coverage report with a valid XML shape but stale source files, a zero denominator, or missing analysis import is not coverage evidence.
- A finding which is `FALSE_POSITIVE`, `ACCEPTED`, `WONTFIX`, excluded, or absent only because the scope changed remains a program blocker under this contract.
- A zero-item result is valid only when the corresponding receipt says `result_empty=true` and complete pagination/current-analysis binding proves the empty set; a missing page is not a zero denominator.
- A clean Wave-4 receipt tied to a prior source head does not authorize Wave 5 after any source byte changes.

## Scope Boundaries

### In scope

- Program contracts, child ownership boundaries, exact-head evidence model, five-wave dependency order, and the final v0.23.11 release intent.
- The future Wave-1 transport-death repair, Wave-2 owner-scoped cleanup repair, Wave-3 cross-language coverage transaction, Wave-4 complete current-finding remediation, and Wave-5 release integration.
- Public route-preservation comparisons and installed-consumer proof at the final release boundary.

### Out of scope

- A speculative fix for the historical stdout-closure producer.
- A second public release, prerelease, tag, or publication for Waves 1–4.
- A broad process-management framework, a scanner rewrite, a Sonar server change, or a Python-to-native route cutover.
- Retrospective acceptance of v0.23.10 evidence, stale finding partitions, or a different project/latest analysis as program evidence.

## Requirements *(mandatory)*

| ID | Binding requirement | Observable future acceptance |
|---|---|---|
| **PRG-001** | The program MUST deliver one public v0.23.11 release iteration through five internal verified waves. Waves 1–4 MUST have `release_intent: none`; Wave 5 alone may ship publicly. | The final release record binds one annotated v0.23.11 tag to the sole Wave-5 shipping moment and names the prior four waves only as exact-head closures. |
| **PRG-002** | Adapter stdout EOF, reader failure, process exit, explicit stop, and DAP terminal signals MUST converge through one guarded transport-death finalizer that prevents stale public `RUNNING`/live-debuggee state. | Focused race and public-state evidence observes one terminal or unavailable publication, prompt pending-request failure, bounded cleanup, and no stale liveness claim. |
| **PRG-003** | Pre-build cleanup MUST act only on an owner-scoped, pre-resume acknowledged process-tree capability; global image-name, directory-discovery, and unverified PID cleanup MUST be removed from the default path. | A two-owner proof observes zero foreign termination, fail-closed admission, and selected-tree graceful/forced drain evidence. |
| **PRG-004** | The exact-head runner MUST produce, validate, and import real Python and .NET coverage reports in the same scanner transaction while the existing `80` threshold remains unchanged. | Analysis-bound evidence identifies both deterministic nonempty reports, their source/head provenance, imported mappings, and the unchanged coverage-condition result. |
| **PRG-005** | Every current finding in the fresh complete project denominator MUST be repaired in source and reconciled to zero blocking findings and zero blocking hotspots. | The exact Wave-4 integration receipt proves complete issue/hotspot paging, `blocking_count=0`, `new_violations=0`, zero blocking hotspots, and quality gate `OK`. |
| **PRG-006** | Every release-critical Sonar observation and final publication decision MUST bind to the exact candidate or post-merge head that it claims to describe. | Candidate and post-merge receipts contain matching captured/post-scan/current-analysis revisions; the annotated tag target equals the valid post-merge receipt head. |
| **PRG-007** | The public Python/default route and stateless-preview boundary MUST remain unchanged throughout Waves 1–4 and must be proven through the public surface at Wave 5. | Comparison and installed-consumer evidence uses the public Python command/default journey and shows no route-selection or preview-boundary migration in child diffs. |
| **PRG-008** | The program MUST NOT weaken any gate through suppression, exclusion, baseline reset, accepted risk, false-positive/WONTFIX disposition, threshold/new-code change, or Sonar server-policy mutation. | Every child and final receipt records the unchanged policy authority; any prohibited disposition or policy drift prevents closure. |
| **PRG-009** | After every complete diagnostic exact-head scan, the program MUST maintain a fresh manifest union assigning each blocking key to exactly one owner. | The Wave-4 integration evidence hashes/references its fresh union and proves no missing, duplicate, stale, or out-of-owner key. |
| **PRG-010** | Decisions, child contracts, and public/internal documentation MUST distinguish observed facts from inferences and cite current primary GitHub or repository evidence. | Child research and acceptance materials cite the required primary sources, current-source seams, requirement IDs, exact files, and observable proof without inventing producer causes or acceptance. |

## Success Criteria *(mandatory)*

| ID | Measurable outcome | Requirement links |
|---|---|---|
| **SC-001** | Exactly one public v0.23.11 shipping moment exists; zero Wave-1–4 public tags/releases are used as substitutes. | PRG-001 |
| **SC-002** | 100% of controlled Wave-1 terminal-signal permutations publish at most one manager-visible terminal/unavailable outcome, and 0% retain `RUNNING` plus `debuggeeAlive=true` after transport death. | PRG-002 |
| **SC-003** | In the two-owner cleanup proof, 0 foreign adapters/process trees are selected or terminated; 100% of selected trees have a pre-resume owner acknowledgement and a drain outcome. | PRG-003 |
| **SC-004** | The Wave-3 exact-head transaction records one nonempty deterministic Cobertura report for each language and evaluates new coverage against the unchanged `80` threshold on the submitted analysis. | PRG-004, PRG-006, PRG-008 |
| **SC-005** | The Wave-4 exact integration receipt reports 0 blocking current findings, 0 blocking hotspots, 0 new violations, and quality-gate `OK`; incomplete pagination is 0. | PRG-005, PRG-008, PRG-009 |
| **SC-006** | 100% of release-critical receipts have a captured head, post-scan head, and current-analysis revision equal to their stated source identity; the final tag target equals the valid post-merge head. | PRG-006 |
| **SC-007** | The final installed-consumer proof completes the public Python/default journey without a route cutover, and no child authorizes a stateless-preview boundary change. | PRG-007 |
| **SC-008** | 0 accepted-risk/suppression/exclusion/baseline-reset/policy-weakening paths are used to close Waves 3–5. | PRG-008 |
| **SC-009** | 100% of keys in every fresh Wave-4 diagnostic denominator have exactly one owner; 0 keys are missing or duplicated. | PRG-005, PRG-009 |
| **SC-010** | 100% of child requirements and tasks cite stable PRG anchors, exact owned files or a fresh-manifest routing authority, observable acceptance, and evidence provenance. | PRG-010 |

## Key Entities

- **Program Contract**: This immutable planning identity: source baseline, PRG anchors, one public release iteration, and five internal verified waves.
- **Wave Contract**: The bounded child-spec scope, `release_intent`, prerequisites, exact closure predicate, owner, and non-goals for one internal wave.
- **Transport Terminal Record**: The bounded, immutable Wave-1 record of first signal, process exit observation, return code if known, DAP termination/exited facts, last DAP event, stderr tail, reader failure, and explicit-stop fact.
- **Owned Process Capability**: The Wave-2 private retained handle/job boundary that establishes a process-tree ownership claim before the child can run.
- **Coverage Transaction**: One exact-head scanner transaction containing deterministic .NET and Python coverage report provenance, validation, import configuration, and analysis-bound outcome.
- **Finding Manifest Union**: The fresh, scan-derived set of blocking finding keys, each mapped to exactly one repair owner.
- **Exact-Head Receipt**: A secret-free evidence record whose target, scanner metadata, current-analysis binding, complete pagination, and post-scan head agree.
- **Wave Closure**: A future child acceptance artifact bound to one exact wave head. It is not a public release and cannot be inferred from a plan.
- **Release Evidence Bundle**: The future Wave-5 combination of accepted Wave-4 closure, candidate/post-merge receipts, consumer proof, annotated tag, publication result, and post-publication canary.

## Assumptions and Not Yet Specified

- The source baseline version metadata is `0.23.11`, but version metadata is not a publication result; Wave 5 alone owns release mechanics.
- The exact distribution of fresh findings across Python, bridge, and host must be generated from the current diagnostic receipt at Wave 4. Historical v0.23.10 counts are intentionally not copied as current ownership evidence.
- The concrete bounded sizes/timeouts for the Wave-1 diagnostics belong to child 012 after its D1 design pass; this parent requires boundedness and observability, not guessed constants.
- The exact Windows private-boundary implementation choice for Wave 2 belongs to child 013 after it discharges the cited Win32 research; this parent fixes the ownership invariant, not a second generic process framework.
- The full file-level repair list for 1,076 baseline blockers is deliberately deferred to the fresh one-owner manifest. Deciding those source edits before that scan would violate PRG-009.
