# A3 — Recoverable Required Metadata and Version Errors

Status: source implemented and locally verified: 167/167 preview tests, the real new scenario on verified extracted self-contained bytes, installed Python rollback and one independent error-to-wire trace. Canonical immutable candidate and public 17-case delivery remain pending.
Baseline: `cbc306de36c162930bd83b2bd0424ca06e82eba9`.
Parent: Stateless Front-Door Convergence Program A3. The optional-clientInfo predecessor is delivered as `stateless-preview-v0.23.17-preview.1`; its source, evidence and tag remain immutable.

## Consumer outcome

A modern client calls `find_code_symbol(name=PreviewMarker, kind=class)` directly on the opt-in Windows x64 preview. Missing or malformed required metadata produces an exact correlated `-32602`, not transport disposal. An unsupported string version produces the exact `-32022` contract, including after a valid request. The client corrects the request and obtains the exact symbol in the same process, then completes clean EOF. Python stays default and rollback.

## Boundary

- For a valid JSON-RPC request with absent `params` or object-valued `params`, reject absent/non-object `_meta`, absent/null/non-string `io.modelcontextprotocol/protocolVersion`, and absent/null/non-object `io.modelcontextprotocol/clientCapabilities` with `-32602` and the original string or numeric ID. Do not dispatch the tool or inherit metadata from a preceding request. Empty capabilities `{}` is valid.
- Exactly one version is supported: `2026-07-28`. Any other string, including empty or whitespace, with otherwise valid metadata returns `-32022`, message `Unsupported protocol version`, and exact `data.requested` / `data.supported:["2026-07-28"]`. Prove both unsupported-first → valid and valid → unsupported → valid. Do not normalize the requested version or promise switching among multiple supported versions.
- Metadata validation precedes tool-argument validation. With valid metadata, existing typed argument failures remain unchanged.
- Omitted `clientInfo` remains valid. Present malformed/null/blank-name/version values still refuse; do not turn invalid presence into omission or self-reported information into identity/read authority. This slice does not promise one universal error/EOF policy for every optional metadata error.
- SDK-thrown errors must traverse the existing outgoing size owner. Preserve the complete serialized 256 KiB JSON response limit (excluding the transport LF), original ID and exact error code. An oversized `-32602` may shorten optional details/message, then must be remeasured; the mandatory message field remains a string, including `""` in the minimal envelope. If even the minimal correlated error cannot fit, close through the existing fail-closed owner without sending an oversized response. For `-32022`, retain exact requested/supported data or fail closed.
- Keep the explicit launch-pinned root, three ordered read-only tools and schemas, source/read/result/traversal budgets, text/structured parity, cancellation and EOF ownership. No new authority, dependency framework, transport or session manager.

## Selected supported mechanism

Pin only the preview and its test project to ModelContextProtocol `2.3.0` (primary source tag commit `9a327829ef066c44a0d737c58bf3c7617d7c701c`). The SDK owns required-metadata parsing, supported-version ordering and correlated errors. Narrow the local predicate to the retained present-clientInfo policy; remove dead duplicate checks rather than translating unrelated error codes. Existing public message filters/serializer keep output bounds.

Root read the exact SDK metadata and exception/output owners on 2026-10-27. Source ordering is not package/runtime proof: verify the restored package identity and a pin-only process discriminator before changing runtime logic. The retained SDK session guard remains; singleton supported-version policy makes supported-version switching irrelevant.

## Direct proof

Use `tests/fixtures/PreviewSearchApp` and the existing process driver. Establish meaningful RED on the unchanged product/pins before implementation: missing `_meta` should return `-32602` and permit corrected same-process success. Preserve the predecessor's supported-then-unsupported `-32600` failure as original evidence.

GREEN covers required-field absence/type cases before and after success, numeric/string IDs, both version sequences, metadata-before-arguments, omitted/present-invalid client information, exact `PreviewMarker`, existing argument/root controls, and representable/unrepresentable escaped-ID error frames. Do not count EOF, timeout or an arbitrary exception as the expected response.

## Artifact and release

Use a new fixed contract `a3-preview-required-metadata-version-v1`: the existing 16 ordered scenarios plus `required-metadata-version-recovery` immediately before rollback, totaling 17. Compute its hash through the existing canonical serializer. Preserve all historical A1/A2/optional-A3 definitions and hashes. Current producers emit only the complete new contract; old 16-case or mixed-generation proof cannot satisfy it.

Delivery uses the existing canonical build, full retained 17-case consumer and unchanged installed Python rollback, candidate-bound review and explicit Root S4, same-byte publication without rebuilding, fresh public 17-case consumer and existing closeout. No version/tag or unknown candidate is preapproved.

## Explicit remaining scope

Malformed JSON/envelope/ID or null/scalar/array `params` policy, complete capabilities-schema validation, legacy `initialize` diagnostics, broader compatibility/capability/roots disposition, Program B/C/default selection and Python retirement are not closed here. The transport parse-error path may bypass application outgoing filters; no new full-envelope/frame claim is made. `runtime8-security-servicing` remains a separate follow-on; this contract does not change the runtime pin or unrelated native-host SDKs.

Primary sources: [required per-request fields](https://modelcontextprotocol.io/specification/2026-07-28/basic/index.md#meta), [independent version checking](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning.md), [SDK metadata owner](https://github.com/modelcontextprotocol/csharp-sdk/blob/9a327829ef066c44a0d737c58bf3c7617d7c701c/src/ModelContextProtocol.Core/Server/McpServerImpl.cs#L208-L358), [SDK exception/output owner](https://github.com/modelcontextprotocol/csharp-sdk/blob/9a327829ef066c44a0d737c58bf3c7617d7c701c/src/ModelContextProtocol.Core/McpSessionHandler.cs#L272-L328). Accessed 2026-10-27.
