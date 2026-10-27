# A3 Optional Client Info Implementation Plan

Status: contract accepted for implementation by Root; runtime and delivery proof pending.
Baseline: `0532ba3b77daf7b1604d86d54c91b7a05a0d7e50`; branch `work/a3-optional-client-info`.

D1: remove one unnecessary request-admission requirement and bind its real consumer proof to a successor of the existing preview. Reuse existing owners; no new host, compatibility layer, schema generation or release manager. Root owns contract verification, integration and release decisions under the accepted original strangler program.

## Source map and bounded changes

| Owner | Existing fact and implementation boundary |
|---|---|
| `host/NetCoreDbg.Mcp.Stateless.Preview/Program.cs:43-47,319-340` | Local predicate requires `clientInfo`. Make omission valid while retaining validation when present. Keep `HasNonBlankString`: it also checks the file argument. Do not broaden required-metadata error handling. |
| `PreviewProjectRootParser.cs`, `PreviewToolCatalog.cs`, `PreviewToolHandler.cs` in the same project | Authority, catalog, dispatch and result owners remain unchanged. Optional client information neither changes root/argument checks nor exempts requests from bounded output. |
| `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/{PreviewProcessContractTests,PreviewMcpProcessDriver,PreviewArtifactConsumerTests}.cs` | Move omitted `clientInfo` from malformed-input expectations to positive coverage; retain explicit null/malformed cases and with-clientInfo coverage. Use existing driver metadata overrides rather than removing client information from shared defaults. Update current artifact-contract expectation and exercise the extracted executable. |
| `scripts/stateless_preview_artifact.py:1329-1439` | Add fixed A3 ID/catalog to existing explicit dispatch. Schema 1 remains A1; schema 2 accepts only exact A2/A3 ID/hash/tools objects. No-argument catalog API stays A1. |
| `scripts/stateless_preview_artifact.py:911-913,1218-1220`, `build/prepare_preview_fixture.py` | Current-source producers emit A3. `seal-build-records` must compare the complete current A3 contract, not just schema `2.0`; historical A2 admission must not permit current-producer downgrade. |
| `scripts/stateless_preview_artifact.py:1849-1941,2078-2082,2553-2557` | Reuse candidate-bound fixture/hash/matrix validation in both retained and remote sealing. Exact IDs/order/count/outcomes remain mandatory. |
| `tests/preview/validate_preview_artifact.py` | Add one composed optional-client-info runner and handler entry. Existing `JsonRpcProcess` performs actual exchange/frame checks. Construct separate no-clientInfo metadata; leave historical defaults/runners unchanged. Record PASS only after all subchecks complete. |
| `specs/005-stateless-preview/contracts/preview-manifest.schema.json`; `specs/010-a1-preview-artifact/contracts/{candidate-identity,artifact-consumer-proof}.schema.json` | Keep closed shapes and versions. Add known A3 ID to schema-2 manifest admission. Receipt branches bind fixture ID to candidate contract ID, not just schema 2. Update candidate description without changing schema equality. Semantic validators remain authoritative for exact catalog hashes/matrices. |
| `tests/test_stateless_preview_{artifact,a2_artifact}.py` and existing remote acquisition tests | Preserve historical A1/A2 evidence; extend meaningful generation-substitution, current-producer downgrade, matrix-integrity and actual-runner false-pass checks. Update current-producer expectations, not historical fixtures to pretend they are A3. |

The existing preview workflow, public acquisition/publisher, reference bundle, three tool schemas and fixture source contents require no architectural change. Update existing preview user documentation/changelog during integration; keep historical A2 records accurate.

## Decisions and subtraction

Choose omission support plus a new fixed scenario ID/hash over documentation-only: documentation cannot make the rejected request work. Reject dual-era preview and SDK upgrade because neither is needed for this operation. Reuse schema `2.0` rather than introduce schema `3.0`: the existing closed `consumer_contract` already supplies a candidate-bound discriminator.

Subtract mandatory self-description, not request-local validation. Distinguish historical validators from current-source producers: old identities remain readable under their original obligations, while newly produced A3 identities cannot select A2 proof.

The A3 list is the unchanged A2 list with `optional-client-info-request-local` inserted before rollback; count 16. It is one composed scenario with first-call, discover/list/call, with-then-without, version recovery, negative boundaries and EOF obligations. Its canonical hash is computed by the existing catalog owner. Do not mutate the A2 list or accept definitions supplied by a receipt.

## Ordered work and handoff

1. Root verifies and commits this contract against the assigned baseline. Document authoring is not implementation evidence.
2. Observe focused process RED for a valid first call without client information before changing the predicate. Then apply the minimal runtime correction and demonstrate specified process outcomes, including present-invalid rejection. Avoid a complete cross-product of tools and metadata shapes: admission is shared; existing route-specific suites retain their role.
3. Extend artifact generation and the real consumer runner through existing owners. Retain A1/A2 semantics and add A3 fail-closed proof. Runtime and artifact owners may overlap in C# artifact expectations; assign that file explicitly rather than concurrently editing it.
4. Root integrates both surfaces and runs owning checks once on combined source. Use existing semantic sealing cases and meaningful false-pass/failure-boundary tests, not permanent mock echo, copied-default or per-statement wiring tests. Fix observed failures and rerun affected checks.
5. Exercise exact frozen-source built/extracted bytes and installed Python rollback, update existing user documentation/changelog, complete required review/integration gates and freeze the proven source. Local success is not public delivery.
6. Use the existing canonical successor producer and retained 16-scenario consumer proof. Root makes the existing S4 promotion decision for that exact candidate; publish without rebuilding only after that decision, then verify public bytes and fresh 16-scenario/Python rollback proof. Do not guess a version or preapprove unknown bytes.

## Proof portfolio

| Claim | Required evidence |
|---|---|
| Omission is genuinely supported, not remembered metadata | Observed pre-fix RED; fresh first-call and fresh discovery/list/call; with-then-without process exchanges with exact result/schema parity |
| Omission does not weaken boundaries | Existing malformed-present and required-metadata refusal checks; no-info argument/method/tool refusals; existing root/frame/cancellation owners remain applicable |
| New proof cannot be downgraded | Both retained and remote semantic sealing reject cross-generation ID/hash/matrix/count/outcome substitutions; current producer rejects A1 and A2 contracts |
| Historical compatibility remains meaningful | Unchanged A1/A2 catalog definitions/hashes/default API and historical valid receipts; unknown contracts still fail closed |
| Runner cannot claim work it did not complete | Meaningful failed-result/early-failure checks prevent PASS/sealing; real extracted executable satisfies every new scenario obligation |
| Consumer delivery | Exact candidate, manifest/executable binding, retained proof, S4 decision, verified public bytes, fresh full consumer proof and installed Python rollback |

Keep required-metadata `-32602` behavior and legacy compatibility policy open as remaining A3 work. SDK filter ordering prevents deriving every wire error from the local predicate alone. No full protocol-compliance claim follows from this slice.

## Migration, rollback and lifecycle

No state or stored data migrates. Remove preview selection to return to unchanged installed Python; previous immutable preview remains available with its historical requirement. Preserve source, immutable assets and original evidence. Close only owned consumed temporary outputs after their last real consumer; a failed tool step does not authorize destructive cleanup. Completion is this optional-clientInfo successor only, not all A3 or Programs B/C.
