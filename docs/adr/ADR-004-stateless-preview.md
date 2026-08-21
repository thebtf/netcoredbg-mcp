# ADR-004: Isolate A1 through a shared traversal core and host-specific authority policies

## Status

Proposed — A1 planning requires an exact-candidate checker after final challenger
repairs. GitHub prerelease publication is an S4 approval and release boundary.

## Context

The selected final host family, `NetCoreDbg.Mcp.Stateless`, currently composes
nine unsafe-for-A1 DAP/Native Scene routes. The compatibility host has mature
native search, but `NativeCodeSearch.cs` mixes BCL traversal/matching with
SDK-1.4 relay envelopes and `ProjectRootResolver` selection semantics.

A1 needs a built, one-tool modern preview without duplicating algorithmic
search logic or changing the compatibility host's environment/client-root/CWD
precedence and current parity behavior.

## Decision

1. Extract dependency-free `NetCoreDbg.Mcp.CodeSearch.Core`. It owns only
   traversal, `.gitignore` subset matching, C# symbol matching, deterministic
   result construction, final-target checks, and policy-driven resource/error
   enforcement. It does **not** select a root or import an MCP SDK.
2. Define immutable `LegacySearchPolicy` and `PreviewSearchPolicy` values at
   the core seam. The former preserves current compatibility-host extension,
   ordering, result, and enumeration-error behavior; the latter supplies the
   strict A1 regular-file/final-target/no-partial/budget behavior. Thus the
   algorithm has one owner while deliberately different public contracts do
   not silently overwrite each other.
3. Keep `ProjectRootResolver` and all environment/client-root/CWD authority in
   the legacy host adapter. Add a preview-only `--project` parser/validator;
   it selects exactly one strict local root before the preview serves MCP.
4. Migrate `NativeCodeSearch`, `RelayComposition`, and `ToolsRelay` to the
   core through the legacy policy. Preserve the exact parity owners in
   `tests/test_host_proxy.py`: catalog/call, Python rollback, root precedence,
   order/ignore/symlink, and timeout behavior.
5. Add `NetCoreDbg.Mcp.Stateless.Preview`, a separate SDK-2.1.0 executable.
   Its compile-time catalog contains only `find_code_symbol` and references no
   DAP, Native Scene, bridge, artifact, Python relay, mux, or HTTP component.
6. Use a manual build-once/promote-same workflow. The build run creates the
   candidate archive/manifest for an exact source SHA. Its downloaded bytes
   receive the complete local client/security/rollback proof and independent
   review **before** S4. S4 binds that build run, source SHA, and hashes.
   Promotion only uploads the approved bytes; it does not rebuild or re-test a
   different payload before creating the immutable tag/release.
7. Model promotion as a resumable remote state machine. A first attempt admits
   only an absent tag/release. A retry admits only the approved annotated tag
   target and a matching absent/draft/complete prerelease state; mismatched
   tag, target, release metadata, or asset bytes hard-refuse. A retry may
   upload only missing approved assets and never overwrites or moves a tag.

## Alternatives

| Alternative | Decision | Reason |
|---|---|---|
| Runtime `--preview` profile in current nine-tool executable | Rejected | A configuration defect can reopen packaged DAP/Native Scene/bridge/artifact surfaces. |
| Separate preview executable with shared traversal core and host-specific policies | Accepted | Compile-time catalog isolation, one algorithm owner, and preserved legacy root semantics. |
| Share one root-policy implementation between hosts | Rejected | Legacy environment/client-root/CWD precedence is deliberately incompatible with A1 launch-only authority. |
| Reuse the modern nine-tool front door as a library | Deferred | Broadens A1 into a refactor unrelated to its one-route consumer proof. |
| Separate PyPI preview package | Deferred | Adds package/default-selector/version risk before the preview is proven. |

## Security and rollback decisions

- A1 is a fresh local stdio process for one trusted OS user/client; it has no
  shared daemon, mux, remote listener, or bearer/session capability.
- Invalid launch roots cause process-start refusal before stdout MCP traffic.
  During search, observable reparse/outside-root/path-read failures fail the
  call with no partial result. A selected worktree root is legal; an escape
  into another worktree is not.
- Hostile concurrent mutation of an operator-owned local tree is outside A1's
  guarantee pending handle/file-ID validation. Observable path identity/open/
  read errors still fail closed.
- Rollback removes preview selection before state exists and replays the
  unchanged Python consumer journey. Withdrawal stops future distribution but
  cannot revoke downloaded bytes; a correction uses a new immutable preview tag.
