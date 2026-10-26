# A2 — Native Read-Only Code-Search Family

Status: implementation in progress; no runtime or delivery claim.
Parent: Stateless Front-Door Convergence Program A, iteration A2 (`.agent/runs/python-removal-strangler-program-v1/stateless-convergence-program-v4.md:85-96`). A1 opt-in preview.2 is delivered. Original operator continuation includes prior refactoring and .NET strangler; this is not a neighboring feature request.

## Outcome

A developer opts into the Windows x64 native stateless preview for one explicit local project, finds a C# symbol, finds its references, and reads the relevant source window. The preview never selects or launches Python, DAP or a UI bridge. Removing preview selection leaves the installed Python command and its existing useful journey unchanged.

The direct acceptance oracle is the complete journey through the actual extracted/downloaded successor artifact, its verified manifest and executable, followed by Python rollback. Source tests and local build success support that oracle but do not establish public delivery.

## Scope and compatibility

- Extend the existing preview adapter, not the legacy compatibility host or modern stateful host.
- Keep `find_code_symbol` behavior and schema unchanged. Add `find_code_references` and `get_source_context`, in that catalog order after the symbol tool. Exactly these three tools are registered, read-only, idempotent and closed-world.
- Retain the existing MCP 2026-07-28 request-local metadata, supported methods, stdio transport, explicit `--project`, parser, strict traversal and frame limits. No SDK/protocol upgrade, relay, roots fallback, stateful ownership, default selector or Python deletion.
- Use `SymbolSearchEngine` with `PreviewSearchPolicy`; never import `LegacySearchPolicy` or compatibility `ProjectRootResolver`. The Python/legacy result values are a parity oracle where policy overlaps, not authority to weaken preview refusals.
- Permit one narrow strict-engine correction discovered during the independent contract challenge: direct `get_source_context` file paths must honor the same bin/obj/VCS directory exclusions as traversal. Prove refusal before file-content read for relative and absolute in-root paths; preserve legacy policy behavior. Do not broaden this into a shared-engine refactor.

## User scenarios

1. In a project containing a declaration and its uses, a fresh preview process accepts discovery/list/call without legacy initialization. The developer obtains the declaration, exact reference locations and a correctly numbered source window, then closes stdin and observes exit 0 and no child process.
2. An invalid argument, unreadable/ignored/unsupported file, ambiguous basename, outside-root/reparse target, exhausted budget or oversized response returns the established typed complete failure, attributed to the requested route, without partial source disclosure. A later valid independent call remains correct where the existing protocol permits it.
3. The same consumer removes only preview selection and completes the installed Python journey without reinstalling or changing the default command.

## Route contracts

### find_code_references

Input object forbids unknown properties. `name` is required, a nonblank string of at most 256 UTF-16 code units. `max_results` is optional, a positive JSON int32, default 1000; explicit null, non-integral numbers and wrong types are invalid.

Success structured content:

```json
{"kind":"find_code_references_success","results":[{"file":"Markers.cs","line":3,"context":"public sealed class PreviewMarker { }"}]}
```

Matching and ordering are the existing engine's semantics: ASCII identifier boundaries for identifiers and literal substring for other names; root-relative file paths and trimmed/clipped context. The strict policy ceiling remains 128 results. Requests below 128 can stop at the requested count; requests at or above 128 fail with a budget error if the policy ceiling is exceeded. The legacy default does not imply a 1000-result preview entitlement.

### get_source_context

Input object forbids unknown properties. `file` is a required nonblank string; `line` is a required positive JSON int32; `radius` is an optional nonnegative JSON int32, default 10. Explicit null and wrong types are invalid.

Success structured content:

```json
{"kind":"get_source_context_success","file":"Markers.cs","start_line":1,"end_line":3,"lines":[{"line":1,"text":"namespace PreviewSearchApp;"},{"line":2,"text":""},{"line":3,"text":"public sealed class PreviewMarker { }"}]}
```

The window is 1-based, clipped at file boundaries; indentation is preserved with existing scalar clipping. Path admission, unique basename resolution and supported/ignored file rules remain owned by the strict engine. A line beyond EOF is the existing typed invalid-arguments failure. Results do not expose an absolute project root.

### Shared wire/error contract

Success uses `resultType:"complete"`, `isError:false`, exactly matching parsed text and structured content and a closed output schema for that route. Failures use `resultType:"complete"`, `isError:true`, `{kind,error,tool}` with the actual known tool name. Missing/malformed tool names keep the established fallback. Unknown tool names remain unknown and cannot dispatch any other operation.

The complete JSON-RPC response, including request ID, duplicated text/structured data and server metadata, remains at most 256 KiB. Preflight occurs before constructing an oversized serialized success. Frame refusal for a known route must name that route, not `find_code_symbol`. Existing behavior for a request ID too large even for a refusal remains closed; no output may exceed the limit. Cancellation must not emit a stale successful response.

## Authority and resource invariants

The launch-pinned explicit local fixed-drive project root is the only read authority. Environment variables, CWD and client roots do not select it. Existing component/final-path reparse checks, .cs-only scope, ignored bin/obj/VCS directories, unreadable-file refusal, byte/entry/directory/deadline budgets and no-partial-failure semantics remain intact. No process, network, debugger, bridge, artifact store or stateful route is enabled by these tools.

## Artifact successor and delivery

A1's published one-tool bytes and historical records remain immutable. A2 requires a versioned successor scenario/catalog/proof contract: three real route calls, inherited authority/protocol/EOF denials, new route input/path/budget/frame refusals, exact source/build/manifest/executable binding and actual installed Python rollback. Old A1 records must not be accepted as three-tool evidence, and unchanged historical A1 validation remains available for its original identities.

The immutable successor candidate/manifest must bind its expected consumer-contract ID and exact tool list. Both retained and remote sealing require candidate, receipt and scenario catalog ID/hash agreement. Historical A1 identities have a fixed A1 interpretation; receipt-provided IDs must never select a weaker validator for an A2 candidate. Reject unknown discriminators and A1 proof submitted for A2 bytes. Asset version and serverInfo version are not sufficient proof-generation discriminators.

Reuse the existing preview workflow, acquisition, manifest and candidate/promotion mechanism. This specification does not grant a publication approval for an unknown future candidate. Before distribution, the root records the existing preview promotion decision for the exact proven successor; use a noncolliding immutable prerelease and remote-byte verification. No PyPI/default release or tag reuse.

## Completion criteria

- All three routes work through the fresh built and extracted preview entry point with exact expected values and text/structured schema parity.
- Route-specific validation, containment/read/budget/frame failures are closed, correctly attributed and do not expose partial results.
- Original symbol/protocol/cancellation/frame/root tests remain valid except the explicitly changed successor catalog cardinality.
- Existing legacy code-search parity and installed Python rollback remain unchanged.
- Independent code/security review covers the new dispatch/read/frame boundaries; required preview producer and consumer gates pass for one exact successor identity.
- Changes are integrated through a reviewed PR; actual successor consumer delivery and fresh public-byte verification complete before claiming release completion.

Not included: Program A3, Program B/C, Python retirement, branch-only PR313 refinement, neighboring application requests or Sonar as a release gate.
