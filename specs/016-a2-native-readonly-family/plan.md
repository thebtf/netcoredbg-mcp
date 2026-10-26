# A2 Implementation Plan

D2: extend one durable opt-in consumer boundary across adapter, bounded serialization and exact artifact proof; reuse existing strict engine and release mechanism. Wrong route/error/frame/catalog assumptions affect multiple consumers. No new host architecture is needed.

## Source map

- `host/NetCoreDbg.Mcp.CodeSearch.Core/{SymbolSearchEngine,SearchPolicy,SearchFailure}.cs`: reuse existing references/context operations and strict authority/budgets. No policy expansion.
- `host/NetCoreDbg.Mcp.Stateless.Preview/{PreviewToolCatalog,PreviewToolHandler,Program,BoundedResponseFrameSerializer}.cs`: three schemas, dispatch, argument filters, route-attributed errors and pre-materialization wire preflight.
- `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/`: focused process and cancellation/frame coverage, plus successor extracted-artifact expectations. Shared core tests remain the authority for unchanged search semantics.
- `scripts/stateless_preview_artifact.py`, `tests/preview/validate_preview_artifact.py` and existing preview workflow/contracts: versioned A2 proof/catalog using existing producer/acquisition/promotion. Keep historical A1 validation meaningful.
- `tests/test_code_search.py`, `tests/test_host_proxy.py`: existing legacy parity remains supporting proof, not a reason to copy legacy policy into preview.

Native OMP LSP readiness was observed on this workspace: PreviewToolHandler definition and 11 references, and consumer_proof_scenario_catalog Python definition. Initial C# cold-index timeout is retained as a failure, followed by successful semantic lookup. No configuration changed.

## Decisions

Use direct adapters to the existing strict engine, not relay imports or a new routing abstraction. Keep default max_results1000 for caller familiarity but explicitly retain strict128 refusal semantics. Reuse existing request-local protocol and complete typed failure envelopes, extending attribution to the actual route throughout input/output preflight. No external API shape/version changes; local source and the pinned existing protocol contract govern this iteration.

The consumer scenario contract must distinguish A1 one-tool proof from A2 three-tool proof. Retain old A1 identities and records; do not edit them to suggest later capabilities. Use the existing workflow/acquisition mechanism for successor bytes. Do not create a parallel release manager.

## Ordered work and handoff

1. Bounded design challenge: route/strict-vs-legacy/frame attribution and versioned evidence compatibility only. Resolve material contradictions before affected implementation.
2. Preview runtime and focused tests: catalog, dispatch, argument validation and bounded serializers. One child exclusively owns these C# files/tests and the narrow strict direct-context excluded-directory admission correction. Root first observes the focused regression fail before the core fix. Preserve legacy policy and symbol behavior; no wider core refactor.
3. Artifact successor support and tests: one separate child owns Python artifact tooling/proof/contracts, not C# runtime. Bind an explicit supported A1/A2 consumer-contract discriminator and exact tool list to the immutable candidate/manifest. Retained and remote sealing require candidate, receipt and catalog ID/hash agreement; historical A1 candidates retain fixed A1 semantics. Never choose proof generation from the receipt alone. Only genuinely disjoint changes run concurrently.
4. Root combines children, runs owning tests once after integration, independently inspects source and exercises real built/extracted stdio plus legacy rollback. Fix observed defects and rerun affected checks, not unchanged broad suites.
5. Commit focused proven slice, independent PR/security review and repository-required preview gates. Integrate via PR; produce/promote one exact successor and verify remote bytes and fresh consumer result. Publication approval remains exact-candidate bound.

## Proof portfolio

| Claim | Obligation and mode | Next action |
|---|---|---|
| New route values/input errors/metadata/schema/cancellation/frame correctness | REQUIRED; focused owning C# tests, runtime UNKNOWN before first run; background | Run preview owning suite after both changes integrate, inspect failures and fix only demonstrated defects |
| Unchanged strict authority and legacy semantics | REQUIRED; existing affected core/legacy parity tests, background | Run owning suites without changing policy or assertion semantics |
| Useful installed native journey and Python rollback | REQUIRED; actual build/extracted artifact, background bounded execution | Call three real tools and denial/EOF paths against exact bytes, then replay installed Python journey |
| New boundary review | REQUIRED; one independent source/security pass | Check actual changed dispatch/read/output and successor evidence, resolve blocking findings |
| Consumer delivery | REQUIRED; existing exact-candidate preview workflow | Produce candidate, retain downloaded artifact evidence, decide promotion on exact identity, verify published bytes/fresh consumer |

No runtime duration is inferred from a timeout. A failure holds its dependent claim, not independent work. Sonar is post-release and nonblocking. No GUI matrix or neighbor capture is relevant to this read-only slice.

## Migration and rollback

Parallel opt-in preview beside unchanged Python. No users, debug sessions or persisted data move. Rollback removes preview selection before any stateful session; installed Python remains default. A3/B/C remain downstream and cannot inherit acceptance from this iteration.
