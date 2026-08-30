---

description: "Dependency-ordered implementation tasks for the A1 opt-in preview artifact runway"
---

# Tasks: A1 Opt-In Preview Artifact Runway

**Input**: Design documents from `/specs/010-a1-preview-artifact/`

**Prerequisites**: `plan.md`, `spec.md`, `research.md`, `data-model.md`, `contracts/`, and `quickstart.md`

**Tests**: Required for every new implementation path by `.agent/guides/TESTING_GUIDELINES.md`. Test tasks below are behavior-oriented and precede the implementation they specify.

**Scope boundary**: This plan adds an opt-in Windows x64 preview artifact runway only. `pyproject.toml`, `src/netcoredbg_mcp/**`, `.github/workflows/publish.yml`, the public Python command/default selector, and Program B/C remain unchanged or separately authorized; no task below changes them.

## Format: `[ID] [P?] [Story] Description`

- **[P]**: Can proceed after its stated prerequisites alongside another task because it owns different files.
- **[US#]**: Maps the task to one user story in `spec.md`.
- Each description names the exact implementation, test, contract, or documentation path it owns.

## Phase 1: Setup (Shared Artifact-Test Infrastructure)

**Purpose**: Establish the behavior-first test seams and preview-only configuration on top of the existing A1 preview project. No new project scaffold or Python-route change is required.

- [ ] T001 [P] Add RED pytest coverage for canonical-main provenance, retained archive/manifest/executable hash equations, and closed release-gate-catalog authority snapshots in `tests/test_stateless_preview_artifact.py`.
- [ ] T002 [P] Add RED xUnit coverage for launching a verified extracted artifact rather than repository `bin/**` output, JSON-RPC-only stdout, and clean stdin-close EOF in `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/PreviewArtifactConsumerTests.cs`.
- [ ] T003 [P] Declare deterministic `win-x64` self-contained publish inputs required by the artifact manifest in `host/NetCoreDbg.Mcp.Stateless.Preview/NetCoreDbg.Mcp.Stateless.Preview.csproj` without changing the accepted runtime route.
- [ ] T004 [P] Add only preview-channel candidate-identity, collision, matching-retry, and fresh-remote-byte proof rules in `docs/RELEASE-PROTOCOL.md`, preserving every existing Python/PyPI release rule.

---

## Phase 2: Foundational (Candidate Formation and Shared Admission)

**Purpose**: Build the immutable candidate and the shared artifact-process seam required by every user story.

**Critical**: No user-story task may treat a local rebuild, source-tree executable, caller-supplied gate list, or Python publication route as candidate authority.

- [ ] T005 [P] Implement canonical-main Candidate Identity Record assembly, inherited-manifest equality checks, raw-byte hashing, and fixed Release Gate Catalog resolution in `scripts/stateless_preview_artifact.py` so the RED cases in `tests/test_stateless_preview_artifact.py` pass.
- [x] T006 Implement manual `build` admission in `.github/workflows/stateless-preview.yml`: require canonical merged `main`, verify trusted source/build provenance, publish the self-contained preview payload, and refuse Python-channel inputs.
- [ ] T007 [P] Extend `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/PreviewMcpProcessDriver.cs` with an explicit extracted-executable launch path while preserving the existing source-output resolver and every current source-run caller.
- [x] T008 Wire `.github/workflows/stateless-preview.yml` to invoke `scripts/stateless_preview_artifact.py` and retain separate non-overwritable post-merge exact-head receipt, archive/raw manifest payload, Candidate Identity Record, and Release Gate Catalog artifacts that later proof stages can download by exact reference.

**Checkpoint**: A canonical-main retained candidate can be formed, identified, and launched through a dedicated artifact path; the existing Python route remains untouched.

---

## Phase 3: User Story 1 - Use a Safe Opt-In Preview (Priority: P1)

**Goal**: A developer can verify and select the exact retained preview for one permitted project, exercise its sole read-only capability, then remove preview selection and continue with the unchanged Python journey.

**Independent Test**: An independent consumer downloads the recorded preview package, verifies its Candidate Identity Record, completes the allowed `--project` journey and every denial case from extracted bytes, removes preview selection, and completes the existing Python journey without reinstalling or changing the Python route.

- [x] T009 [US1] Add RED receipt and denial-matrix cases for retained-artifact identity verification, explicit `--project` authority, one-tool discovery/call, no partial output, EOF, and Python rollback in `tests/test_stateless_preview_artifact.py`.
- [x] T010 [US1] Implement Consumer Proof Receipt validation and sealing for retained-artifact evidence in `scripts/stateless_preview_artifact.py`, rejecting local rebuilds, wrong hashes, incomplete matrices, leaked paths, and invalid rollback results.
- [x] T011 [US1] Implement the `candidate` mode of `tests/preview/validate_preview_artifact.py` to download the record/payload, verify archive/manifest/executable bytes before extraction, execute the inherited denial matrix, seal the receipt, and invoke the unchanged Python rollback oracle.
- [x] T012 [P] [US1] Complete real extracted-artifact MCP assertions for discovery, the closed one-tool catalog, valid call, stdout purity, and EOF in `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/PreviewArtifactConsumerTests.cs` using the new driver path.
- [x] T013 [US1] Align executable retained-artifact commands, required evidence inputs, and rollback instructions in `specs/010-a1-preview-artifact/quickstart.md` with `tests/preview/validate_preview_artifact.py` without converting the guide into publication authority.
- [ ] T014 [US1] Execute the retained-candidate consumer journey and full denial/EOF/Python-rollback matrix through `tests/preview/validate_preview_artifact.py` against the artifact emitted by `.github/workflows/stateless-preview.yml`; seal only a schema-valid retained-artifact receipt.

**Checkpoint**: US1 is independently demonstrable from retained downloaded bytes, and rollback returns to the unchanged Python journey.

---

## Phase 4: User Story 2 - Prove the Exact Candidate Before Distribution (Priority: P1)

**Goal**: A release owner can prove that consumer, security, and review evidence all belong to the same retained candidate rather than a later build or local substitute.

**Independent Test**: Starting from one Candidate Identity Record, a reviewer downloads the retained package, verifies all bound identities, replays the complete positive/denial matrix, and confirms that review eligibility rejects missing, duplicate, stale, zero-denominator, candidate-mismatched, unredacted, or non-independent evidence.

- [ ] T015 [US2] Add RED semantic fixtures for the ordered seven-lens S2/S3 aggregate, the distinct independent PR review, and exact Stage Gate Evidence closure for every pre-decision, pre-publication, and post-publication catalog subset in `tests/test_stateless_preview_artifact.py`.
- [ ] T016 [US2] Implement `seal_s2_s3_lens_result`, `seal_s2_s3_review_aggregate`, and `seal_independent_pr_review` semantic admission in `scripts/stateless_preview_artifact.py`, deriving the required `7/7/0` coverage rather than trusting supplied counts.
- [ ] T017 [US2] Implement ordered Stage Gate Evidence derivation and outcome recomputation for `pre-decision`, `pre-publication`, and `post-publication` in `scripts/stateless_preview_artifact.py` from `specs/010-a1-preview-artifact/contracts/stage-gate-evidence.schema.json` and the closed catalog.
- [ ] T018 [US2] Run the seven independent S2/S3 lenses defined by `specs/010-a1-preview-artifact/contracts/s2-s3-review.schema.json` and the distinct review defined by `specs/010-a1-preview-artifact/contracts/independent-pr-review.schema.json` against the exact retained candidate; preserve their receipts outside tracked source.
- [ ] T019 [US2] Seal candidate-bound pre-decision Stage Gate Evidence with `scripts/stateless_preview_artifact.py` only after the retained proof, seven-lens aggregate, independent PR review, and candidate exact-head Sonar receipt pass.
- [ ] T020 [US2] Run the semantic refusal and closure suite in `tests/test_stateless_preview_artifact.py` after T016 through T019, including policy-snapshot drift and every required pre-decision evidence mutation.

**Checkpoint**: US2 produces a closed exact-candidate review/evidence set that can support one later decision but cannot authorize a different candidate or dispatcher.

---

## Phase 5: User Story 3 - Make an Explicit, Recoverable Promotion Decision (Priority: P2)

**Goal**: An authorized release owner can record one `APPROVE` or `DECLINE`, admit only a fresh matching promotion attempt, recover safely from an interrupted prerelease state, and prove the published bytes before sealing the non-authorizing Program B handoff.

**Independent Test**: A decision record is evaluated for approve and decline outcomes, and the `unstarted`, `tag_only`, draft, published, unreadable, expired, and collision remote states prove that only the exact approved candidate can promote or recover without replacing or deleting public artifacts.

- [ ] T021 [US3] Add RED tests for candidate-bound `APPROVE`/`DECLINE`, current dispatcher/run/attempt/permission equality, pre-publication admission, and post-publication evidence being refused until matching remote proof exists in `tests/test_stateless_preview_artifact.py`.
- [ ] T022 [US3] Implement Promotion Decision and fresh Promotion Attempt semantic admission in `scripts/stateless_preview_artifact.py` using `specs/010-a1-preview-artifact/contracts/promotion-decision.schema.json` and `promotion-attempt.schema.json`.
- [ ] T023 [US3] Implement the manual `promote` path in `.github/workflows/stateless-preview.yml` so it validates the current attempt and passing pre-decision/pre-publication stages before any tag, release, or asset mutation.
- [ ] T024 [US3] Add focused classifier tests for `unstarted`, `tag_only`, `draft_empty`, `draft_partial`, `draft_complete`, `published_complete`, collision, unreadable, expired, and identity-mismatched remote observations in `tests/test_stateless_preview_artifact.py`.
- [ ] T025 [US3] Implement sealed remote observation, matching-only recovery classification, and refusal of rebuild, overwrite, delete, replay, and unreadable-as-absence paths in `scripts/stateless_preview_artifact.py` according to `specs/010-a1-preview-artifact/contracts/promotion-recovery.md`.
- [ ] T026 [US3] Wire the legal classified recovery actions and fresh remote readback into `.github/workflows/stateless-preview.yml`, preserving non-cancelling tag concurrency and refusing every collision or mismatched asset state.
- [ ] T027 [P] [US3] Add fresh remote downloaded-asset MCP/EOF/identity assertions in `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/PreviewArtifactConsumerTests.cs` without allowing the retained download or source-tree executable to substitute for remote bytes.
- [ ] T028 [P] [US3] Implement Remote Verification and Program B Handoff semantic sealing in `scripts/stateless_preview_artifact.py`, requiring all three stage records, `published_complete`, matching remote proof, and literal separate Program B/C authorization flags.
- [ ] T029 [P] [US3] Implement the `remote` mode of `tests/preview/validate_preview_artifact.py` to use a fresh directory and fresh public archive/manifest download before sealing remote Consumer Proof.
- [ ] T030 [US3] Add the exact preview artifact consumer journey and non-mutating rollback verification to `docs/PRODUCTION-TESTING-PLAYBOOK.md`, keeping Python/PyPI deployment instructions intact.
- [ ] T031 [US3] At the exact candidate release boundary, record one `APPROVE` or `DECLINE` through `specs/010-a1-preview-artifact/contracts/promotion-decision.schema.json`; treat `DECLINE` as a safe terminal outcome with no publication.
- [ ] T032 [US3] After an approved decision and the matching post-merge exact-head Sonar receipt, seal passing pre-publication Stage Gate Evidence through `scripts/stateless_preview_artifact.py` before any remote mutation.
- [ ] T033 [US3] Only after passing pre-decision and pre-publication Stage Gate Evidence, execute the matching promotion attempt defined by `.github/workflows/stateless-preview.yml`; do not overwrite tags, releases, or assets.
- [ ] T034 [US3] After a published matching prerelease exists, run fresh remote proof through `tests/preview/validate_preview_artifact.py` and seal the matching Remote Verification record.
- [ ] T035 [US3] Seal passing post-publication Stage Gate Evidence and the non-authorizing Program B Handoff through `scripts/stateless_preview_artifact.py` and `specs/010-a1-preview-artifact/contracts/program-b-handoff.schema.json` only after T034 passes.

**Checkpoint**: US3 admits only the exact approved candidate, refuses unsafe recovery, proves fresh public bytes, and records an A1 closeout that does not start Program B or Program C.

---

## Phase 6: Polish & Cross-Cutting Verification

**Purpose**: Prove the completed implementation and documentation preserve the artifact boundary, existing Python route, and exact record contracts.

- [ ] T036 [P] Run the focused semantic regression suite in `tests/test_stateless_preview_artifact.py` with the repository’s documented pytest invocation.
- [ ] T037 [P] Run the real-stdio extracted-artifact suite in `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/NetCoreDbg.Mcp.Stateless.Preview.Tests.csproj` with `dotnet test` using the project’s configured target framework.
- [ ] T038 [P] Run the unchanged Python comparison journeys in `tests/test_host_proxy.py` and `tests/critical/test_host_proxy_critical.py` to prove the default Python route was not altered.
- [ ] T039 Rehearse the candidate and remote validation commands in `specs/010-a1-preview-artifact/quickstart.md` against `tests/preview/validate_preview_artifact.py` without performing a publication or Program B/C action.

---

## Dependencies & Execution Order

### Phase Dependencies

- **Setup (Phase 1)**: T001 through T004 may start immediately and own separate files.
- **Foundational (Phase 2)**: T005 and T007 start after their RED tests; T006 depends on T005 and T003; T008 depends on T005 and T006. The phase blocks every user story.
- **US1 (Phase 3)**: Starts after T005 through T008. T009 precedes T010 and T011; T012 depends on T007; T013 follows the runner interface; T014 needs a retained artifact from T008.
- **US2 (Phase 4)**: Semantic work T015 through T017 follows the foundation. Its live review/evidence closeout T018 through T020 requires the retained proof from T014.
- **US3 (Phase 5)**: T021 through T030 require the closed US2 semantics. T031 records the decision; T032 seals fresh pre-publication evidence after the matching post-merge receipt; T033 mutates only after both prior stages pass; T034 seals fresh remote proof; T035 seals post-publication evidence and the A1-only handoff.
- **Polish (Phase 6)**: T036 through T039 run after the desired implementation and applicable conditional evidence stages are complete.

### User Story Dependencies

- **US1 (P1)**: Independently demonstrable after the shared candidate foundation; it does not need an approval, publication, or Program B/C action.
- **US2 (P1)**: Its typed semantic validators are independently testable after the foundation; its live review closure consumes US1’s retained proof so the evidence binds a real downloaded candidate.
- **US3 (P2)**: Depends on US2’s closed pre-decision evidence. It is the only story that reaches an external promotion boundary, and its published-byte proof remains a prerequisite rather than an implicit Program B authorization.

### Within Each User Story

- Write behavior-oriented tests before the code that makes them pass.
- Do not parallelize edits to `scripts/stateless_preview_artifact.py`, `.github/workflows/stateless-preview.yml`, or `tests/test_stateless_preview_artifact.py`.
- Use only downloaded archive bytes for consumer proof; existing source-run preview tests remain inherited coverage rather than a substitute.
- Any changed candidate/version restarts at T005; it never inherits proof, review, decision, promotion attempt, or remote evidence.

### Parallel Opportunities

- T001 through T004 are independent setup tasks.
- After T001/T002, T005 and T007 can proceed in parallel because they own different files.
- After T009/T010/T007, T011 and T012 can proceed in parallel.
- After the remote classifier and workflow admission are complete, T027, T028, and T029 own separate files and can proceed in parallel.
- T036, T037, and T038 can run in parallel after the corresponding implementation is complete.

---

## Parallel Example: User Story 1

```text
After T009, T010, and T007 complete, run the independent artifact-consumer surfaces in parallel:

Task: "Implement candidate-mode downloaded-artifact proof in tests/preview/validate_preview_artifact.py" (T011)
Task: "Complete extracted-artifact MCP and EOF assertions in host/NetCoreDbg.Mcp.Stateless.Preview.Tests/PreviewArtifactConsumerTests.cs" (T012)
```

## Parallel Example: User Story 2

```text
After T017 and a retained candidate proof exist, run the independent review inputs concurrently before T019:

Task: "Run the seven S2/S3 lenses specified by specs/010-a1-preview-artifact/contracts/s2-s3-review.schema.json" (part of T018)
Task: "Run the distinct review specified by specs/010-a1-preview-artifact/contracts/independent-pr-review.schema.json" (part of T018)
```

## Parallel Example: User Story 3

```text
After T025 and T026 complete, run separate remote-proof implementations in parallel:

Task: "Add remote downloaded-asset assertions in host/NetCoreDbg.Mcp.Stateless.Preview.Tests/PreviewArtifactConsumerTests.cs" (T027)
Task: "Implement Remote Verification and Program B Handoff sealing in scripts/stateless_preview_artifact.py" (T028)
Task: "Implement fresh remote-download proof in tests/preview/validate_preview_artifact.py" (T029)
```

---

## Implementation Strategy

### MVP First: Shared Foundation plus User Story 1

1. Complete T001 through T008 to form a retained canonical candidate and an extracted-artifact process seam.
2. Complete T009 through T014 to prove one downloaded preview journey, all denials, EOF, and the unchanged Python rollback.
3. Stop and validate the retained artifact proof before beginning review, decision, promotion, or remote publication work.

### Incremental Delivery

1. **Foundation**: Build once, retain exact bytes, and preserve the Python/default route.
2. **US1**: Demonstrate safe opt-in artifact use and rollback from retained bytes.
3. **US2**: Make exact-candidate review and pre-decision gate closure mechanically enforceable.
4. **US3**: Add one decision, matching-only promotion/recovery, fresh remote proof, and a non-authorizing A1 handoff.
5. **Program B/C**: Remain outside this plan until separately authorized after US3’s exact published-byte handoff condition is satisfied.

### Parallel Team Strategy

1. Split only the `[P]` tasks with distinct file ownership after their stated prerequisites.
2. Keep one owner at a time for the semantic helper, the manual workflow, and the Python semantic regression file.
3. Treat the seven review lenses and the distinct PR review as independent external inputs that converge only at pre-decision Stage Gate Evidence.
