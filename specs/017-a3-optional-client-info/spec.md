# A3 — Optional Client Info in the Stateless Preview

Status: contract accepted for implementation by Root under the original strangler program. No implementation, acceptance-test or delivery claim.
Baseline: `0532ba3b77daf7b1604d86d54c91b7a05a0d7e50`; branch `work/a3-optional-client-info`.
Parent: Stateless Front-Door Convergence Program A, bounded A3 slice (`.agent/runs/python-removal-strangler-program-v1/stateless-convergence-program-v4.md:85-98`). Work proceeds under the accepted original strangler program. A2 is delivered; this slice does not complete A3 or Programs B/C.

## Outcome

A modern client that omits optional `clientInfo` can discover the opt-in Windows x64 preview and obtain the exact definition of a C# symbol in its explicitly selected project. The direct acceptance oracle is a real exchange with the verified downloaded/extracted successor executable, followed by unchanged installed Python rollback. Source tests and build success support, but do not replace, delivery evidence.

## Existing behavior and selected change

At the baseline, `host/NetCoreDbg.Mcp.Stateless.Preview/Program.cs:319-340` requires nonblank client name/version and closes the transport when its metadata predicate fails. Existing process tests include absent `clientInfo` among refusals; the process driver and artifact consumer normally supply it. These are source facts, not fresh runtime proof.

Permit an omitted `io.modelcontextprotocol/clientInfo` key. If the key is present, retain existing shape/name/version validation; explicit null is not omission. Keep required `protocolVersion` and object-valued `clientCapabilities` on every request, including an empty capabilities object. Do not infer metadata from preceding requests. Client information is self-reported software information, not identity, authentication or read authority.

This supersedes only mandatory `clientInfo` in A2's inherited metadata policy (`specs/016-a2-native-readonly-family/spec.md:11-25`). Preserve unrelated malformed/required-metadata handling in this slice; do not describe that handling as fully conformant.

## Scope and invariants

- Keep modern-only `2026-07-28`, existing SDK pins, stdio, and exactly `[find_code_symbol, find_code_references, get_source_context]` in that order. No tool-schema or search-semantic change.
- Keep discovery tools-only, `listChanged:false`, and existing discovery/catalog TTL 300000 ms and public cache scope. These hints do not make source contents publicly cacheable. No optional client capability is required for this operation.
- Launch-pinned local fixed-drive `--project` remains the only read authority. CWD, environment, client information and client roots cannot select or expand it. Preserve strict containment, reparse handling, .cs-only selection, ignored directories, read/resource budgets and no partial disclosure.
- Tool-argument validation and root/frame checks remain independent of `clientInfo`. Keep existing known-tool typed complete failures, unknown-tool text-only failure, excluded-method refusal, cancellation behavior and the 256 KiB complete-response limit.
- Legacy `initialize` remains unsupported; Python remains default and rollback. No automatic legacy fallback inside preview, SDK upgrade, new API/framework, stateful owner, mux, default selection or Python retirement.

## Consumer acceptance

The new `optional-client-info-request-local` scenario uses the existing consumer process owner and completes all obligations before recording PASS:

1. In a fresh process, send `tools/call` as the literal first request without `clientInfo`: `find_code_symbol(name=PreviewMarker, kind=class)` returns `complete`, `isError:false`, and exactly one result: file `Markers.cs`, line 3, name `PreviewMarker`, kind `class`, context `public sealed class PreviewMarker { }`, under kind `find_code_symbol_success`. Validate JSON types/schema and parsed text/structured parity, not Python equality alone.
2. In another fresh process, discover, list and call without `clientInfo`. Require `supportedVersions:["2026-07-28"]`, closed tools capability/cache hints, exact three-tool catalog and the same symbol result. Discovery is useful but not a mandatory handshake.
3. On that process, a valid call with `clientInfo` followed by one without it returns the same result. The fresh-process case prevents this sequence from hiding dependence on previously supplied metadata.
4. Without `clientInfo`, otherwise valid metadata with an unsupported version returns `-32022`, message `Unsupported protocol version`, requested version and supported `["2026-07-28"]`. A subsequent valid call succeeds.
5. Without `clientInfo`, invalid known-tool arguments, a forbidden tool and an excluded method retain their established refusals. Root/path and frame protections remain enforced; inherit existing dedicated containment/frame scenarios rather than duplicating their entire matrices.
6. Close stdin and observe clean exit. Timeouts, premature EOF, exceptions, incorrect values or failed subchecks must not produce a PASS receipt. Always use existing process cleanup.

Focused process coverage retains rejection of present malformed `clientInfo` and genuinely absent required metadata without inventing a new exact error policy for the latter. Historical with-clientInfo consumers remain covered.

## Immutable successor and proof generation

Use existing manifest/Candidate schema `2.0` and receipt schema `1.0`. The closed `consumer_contract` still contains exactly `scenario_catalog_id`, `scenario_catalog_sha256` and `tools`. Introduce fixed ID `a3-preview-optional-client-info-v1`, unchanged ordered three tools, and 16 scenarios: all 15 A2 entries unchanged, with `(optional-client-info-request-local, protocol_catalog, EXPECT_SUCCESS)` inserted immediately before `rollback-python-default`. Compute its hash with existing canonical scenario serialization; do not guess a digest or change an old catalog under its old ID.

The verified immutable candidate chooses proof semantics. Schema `1.0` remains A1-only without `consumer_contract`. Schema `2.0` accepts only exact known A2 or A3 contracts; a receipt cannot select a weaker validator. Historical A1/A2 definitions, hashes, immutable records and the no-argument A1 catalog API remain unchanged. Current-source payload/fixture producers emit A3; `seal-build-records` requires the exact current A3 contract, not merely schema `2.0`.

Retained and remote sealing reject unknown/mixed contract IDs and hashes, missing/extra contract keys, wrong/reordered tools, old A2 proof for A3, cross-generation proof, missing/extra/duplicate/reordered scenario results, wrong counts, wrong outcomes or altered raw identity bindings. Reuse existing candidate/receipt/hash/ordered-matrix validators. Catalog hashes bind definitions, not execution; actual consumer runner and source/run provenance remain responsible for execution evidence.

## Delivery, lifecycle and rollback

After integration and required checks, reuse the existing canonical preview build, immutable candidate/manifest, retained acquisition, review and exact-candidate S4 promotion decision. No unknown candidate is preapproved here and no successor version is assigned in advance. Completion requires public-byte verification and a fresh full 16-scenario consumer proof with unchanged installed Python rollback; old proof cannot credit the new obligation. Preserve artifacts/evidence until their last consumer, then close owned temporary outputs through the existing lifecycle.

Rollback removes only preview selection and uses unchanged installed Python; no session or persisted-data migration exists. An older immutable preview may still require `clientInfo`. Completing this slice does not imply stateful migration or default cutover.

## Primary sources and remaining A3

Accessed 2026-10-27:

- [Official version status](https://modelcontextprotocol.io/docs/2026-07-28/learn/versioning): `2026-07-28` is Current, not inferred from a code constant.
- [Per-request metadata](https://modelcontextprotocol.io/specification/2026-07-28/basic/index#meta) and [RequestMetaObject](https://modelcontextprotocol.io/specification/2026-07-28/schema.md#requestmetaobject): `clientInfo` is optional; `protocolVersion` and `clientCapabilities` are required; missing required fields require `-32602`.
- [Versioning/legacy compatibility](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning.md): modern-only is permitted; legacy initialization diagnostics should name supported versions.
- [Roots](https://modelcontextprotocol.io/specification/2026-07-28/client/roots.md): deprecated and informational, not access control.
- [Pinned SDK 2.1.0 McpServerImpl](https://github.com/modelcontextprotocol/csharp-sdk/blob/3be47ee31ede99ef9025af02d9eb37da938d7f05/src/ModelContextProtocol.Core/Server/McpServerImpl.cs#L145-L315): built-in validation precedes user filters and permits absent `clientInfo`.
- [Pinned SDK discovery wire](https://github.com/modelcontextprotocol/csharp-sdk/blob/3be47ee31ede99ef9025af02d9eb37da938d7f05/src/ModelContextProtocol.Core/Protocol/DiscoverResult.cs#L25-L26): `supportedVersions` is the existing serialized property; the configured preview advertises the singleton protocol version. This is a source-backed contract, not a claimed wire observation.

Remaining A3 work stays named and open: required-metadata `-32602` behavior across SDK/local filters, and broader legacy-client compatibility/diagnostic policy. Current local closure and SDK earlier errors do not establish a uniform normative error contract. This slice makes no full-MCP-conformance or universal third-party-client compatibility claim.
