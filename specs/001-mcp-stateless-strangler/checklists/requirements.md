# Requirements Checklist and Traceability — Milestone M1

A requirement completes only when its named evidence and every listed task
passes. Native lifecycle Code is unavailable until T-002's ownership packet and
T-008's runnable RED suite complete; that suite owns its test project, controlled
executable adapter fixture, and reflection/process driver, and reports absent
production behavior at runtime rather than compilation. Modern MCP Code is unavailable
until T-009 creates the production seam; independent T-009 acceptance hardens incomplete
discriminators, proves the corrected lifecycle cases RED against frozen production, then
the same final 11-case T-008 suite goes GREEN. T-009 records lifecycle-only build/test
readiness; T-005 materializes candidate commands after it makes the front door real.

| Requirement | Acceptance evidence | Design/contract anchor | Tasks |
|---|---|---|---|
| FR-001 | Server implements discover; fresh processes also accept list and valid call first, without discovery/order state. | `spec.md` wire boundary | T-003, T-005, T-006 |
| FR-002 | Distinct request metadata is never retained as connection/prior-request context. | `spec.md` wire boundary | T-003, T-005, T-006 |
| FR-003 | Unsupported version returns `-32022` with exactly `requested`/`supported`. | `spec.md` requirements | T-003, T-005, T-006 |
| FR-004 | Cacheable list has exact ordered three-tool catalog and schemas. | `spec.md` tool contract | T-003, T-005, T-006 |
| FR-005 | Complete tool call contains content, structured content, applicable `isError`, and no cache fields. | `spec.md` tool contract | T-003, T-005, T-006 |
| FR-006 | Start complete and input-required/new-id/inputResponses paths work with no requestState. | `spec.md` tool contract | T-003, T-005, T-006 |
| FR-007 | No-program/no-elicitation is deterministic complete application error, never a server request. | `spec.md` tool contract | T-003, T-005, T-006 |
| FR-008 | Explicit opaque token works without list/current/connection ownership. | `architecture.md` retained M1 decisions | T-004, T-005, T-006 |
| FR-009 | Live-host interleaving proves no creator/client/connection ownership; separate stdio client-close proof ends the candidate; atomic stop proves one winner and at-most-once native cleanup. | `architecture.md` retained M1 decisions; `spec.md` corrected stdio lifetime boundary | T-004, T-005, T-006 |
| FR-010 | Four unusable handle classes and stop losers share exact not-found result. | `spec.md` requirements | T-004, T-005, T-006 |
| FR-011 | T-008's runnable sibling 11-case test suite owns its fixture and complete reflection/process driver; every named lifecycle assertion fails behaviorally before production. Independent T-009 acceptance hardened incomplete discriminators, proved the corrected cases RED against frozen production, then the same final suite went GREEN after T-009 supplied the real `NetCoreDbgSession`. No missing component is called an existing seam. | D1 amendment packet; `spec.md` native lifecycle | T-002, T-008, T-009, T-006 |
| FR-012 | Retained Python script/package/legacy relay are unchanged and Python reaches a separate consumer outcome. | `architecture.md` scope and rollback | T-001, T-006, T-007 |
| FR-013 | Candidate/Python `PRODUCT_WORKS` receipts and candidate-removal Python replay pass. | `quickstart.md` | T-001, T-007 |
| FR-014 | Empty/extra inputs return invalid arguments; malformed handles return not-found; all prove zero prohibited native side effects. | `spec.md` requirements | T-003, T-004, T-005, T-006 |
| NFR-001 | Process exchange proves stdout frame purity. | `spec.md` non-functional requirements | T-003, T-005, T-006 |
| NFR-002 | No public release, remote/auth/durable/package/entrypoint/legacy-relay work occurs. | `plan.md` M1 boundary | T-001, T-005, T-006, T-007 |
| NFR-003 | Interleaving has no connection ownership and native stop happens at most once. | `spec.md` non-functional requirements | T-004, T-005, T-006 |
| NFR-004 | T-009's lifecycle-only build/test, readiness, and cleanup receipt is current; T-001 retained-Python and rollback blocks plus T-005's final modern candidate block are current before consumer proof. | `quickstart.md`; `tasks.md` | T-001, T-005, T-006, T-007 |
| NFR-005 | T-008's runnable 11-case suite covers UTF-8 byte framing, response gate, event-backed state, and one bounded async cleanup owner. Independent T-009 acceptance hardened incomplete discriminators, proved the corrected cases RED against frozen production, then the same final suite went GREEN; T-009 creates only the production component/discovery wiring and records its lifecycle receipt. | D1 amendment packet; `research.md` owned lifecycle facts | T-002, T-008, T-009, T-006 |

## Recheck criteria

- Every requirement has explicit acceptance and typed execution coverage.
- Mermaid edges and `Blocked by` fields describe the same DAG.
- T-008 precedes T-009; T-009 precedes both modern RED tasks; both modern RED
  tasks precede T-005.
- `PRODUCT_WORKS` is consumer evidence, not a test-suite synonym.
- T-006 is the only independent checker commitment.
