# Requirements Checklist and Traceability — Milestone M1

A requirement completes only when its named evidence and every listed task
passes. Code is unavailable until T-002 proves the native seam and T-003/T-004
have first produced RED evidence.

| Requirement | Acceptance evidence | Design/contract anchor | Tasks |
|---|---|---|---|
| FR-001 | Server implements discover; fresh processes also accept list and valid call first, without discovery/order state. | ADR-001, `quickstart.md` | T-003, T-005, T-006 |
| FR-002 | Distinct request metadata is never retained as connection/prior-request context. | ADR-001, `data-model.md` | T-003, T-005, T-006 |
| FR-003 | Unsupported version returns `-32022` with exactly `requested`/`supported`. | ADR-001, contract guide | T-003, T-005, T-006 |
| FR-004 | Cacheable list has exact ordered three-tool catalog and schemas. | ADR-001, contract schema | T-003, T-005, T-006 |
| FR-005 | Complete tool call contains content, structured content, applicable `isError`, and no cache fields. | ADR-001, contract guide | T-003, T-005, T-006 |
| FR-006 | Start complete and input-required/new-id/inputResponses paths work with no requestState. | ADR-001, `data-model.md` | T-003, T-005, T-006 |
| FR-007 | No-program/no-elicitation is deterministic complete application error, never a server request. | ADR-001, contract schema | T-003, T-005, T-006 |
| FR-008 | Explicit opaque token works without list/current/connection ownership. | ADR-002, `data-model.md` | T-004, T-005, T-006 |
| FR-009 | Disconnect/interleaving and atomic stop prove one winner and at-most-once native stop. | ADR-002, `data-model.md` | T-004, T-005, T-006 |
| FR-010 | Four unusable handle classes and stop losers share exact not-found result. | ADR-002, contract schema | T-004, T-005, T-006 |
| FR-011 | Receipt proves exact bounded native C# seam or returns architecture blocker. | Native-seam limit | T-002 |
| FR-012 | Retained Python path stays unchanged and reaches its separate consumer outcome. | ADR-003, `quickstart.md` | T-001, T-005, T-007 |
| FR-013 | Candidate/Python `PRODUCT_WORKS` receipts and candidate-removal Python replay pass. | ADR-003, `quickstart.md` | T-001, T-007 |
| FR-014 | Empty/extra inputs return invalid arguments; malformed handles return not-found; all prove zero prohibited native side effects. | Contract schema, `data-model.md` | T-003, T-004, T-005, T-006 |
| NFR-001 | Process exchange proves stdout frame purity. | ADR-001 | T-003, T-005, T-006 |
| NFR-002 | No public release, remote/auth/durable/package/entrypoint work occurs. | ADR-003, plan | T-006, T-007 |
| NFR-003 | Interleaving has no connection ownership and native stop happens at most once. | ADR-002 | T-004, T-005, T-006 |
| NFR-004 | T-002 candidate command block is current before candidate review; T-001 retained-Python and rollback blocks plus T-002 candidate block are current before consumer proof. | `quickstart.md`, tasks | T-001, T-002, T-006, T-007 |

## Recheck criteria

- Every requirement has explicit acceptance and typed execution coverage.
- Mermaid edges and `Blocked by` fields describe the same DAG.
- `PRODUCT_WORKS` is consumer evidence, not a test-suite synonym.
- Fresh independent Challenge-FULL GO remains required before implementation
  readiness.