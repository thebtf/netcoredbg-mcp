# Research — A1 Safe Read-Only Stateless Preview

## Current source facts

| Topic | Evidence | Decision consequence |
|---|---|---|
| Python release channel | `pyproject.toml` exposes the sole `netcoredbg-mcp` script; `.github/workflows/publish.yml` handles `v*`/PyPI. | Preview uses manual build/promote flow and `stateless-preview-v*`; it cannot touch PyPI/default selection. |
| Modern host | `host/NetCoreDbg.Mcp.Stateless/Program.cs` provides SDK-2.1 modern behavior but composes three DAP and six Native Scene routes. | Separate compile-time one-tool preview, not a runtime flag. |
| MCP metadata | MCP 2026-07-28 versioning/tools require namespaced protocol/client metadata per request; `ModernMcpProcessDriver.CurrentMeta` constructs it. | Freeze namespaced request-local metadata, first-call semantics, `-32022`, cache, and literal outputs/errors. |
| Native search | `NativeCodeSearch.cs` embeds BCL traversal/matching; `ProjectRootResolver.cs` owns SDK-coupled environment/client/CWD root selection. | Share policy-driven traversal/matching; keep legacy root selection and preview parser strict/separate. |
| Legacy parity | `tests/test_host_proxy.py` owns exact catalog/call, rollback, root-precedence, order/ignore/symlink, and timeout cases. | Core extraction preserves legacy behavior through `LegacySearchPolicy`, then reruns named parity tests. |
| Current root gap | Existing resolver accepts normalized existing directories; engine silently converts some enumeration failures to empty output. | `PreviewSearchPolicy` is strict/no-partial and cannot overwrite legacy policy. |
| Release recovery | `AGENTS.md` and `docs/RELEASE-PROTOCOL.md` require immutable tag recovery; `gh release create` can leave a draft after partial upload. | Promotion needs first-attempt versus matching-partial-state admission, not one categorical existing-release rule. |

## Artifact/verifier equations

The JSON Schema validates independent fields. The preview manifest verifier must
also enforce these cross-field equations, which JSON Schema cannot express:

1. `tag == "stateless-preview-v" + version`.
2. `archive.name == "netcoredbg-mcp-stateless-preview-win-x64-" + version + ".zip"`.
3. Uploaded manifest filename equals the same prefix/version plus `.manifest.json`.
4. Archive and extracted executable size/SHA-256 equal their manifest fields.
5. Build workflow source SHA, manifest `commit`, T07 proof receipt, S4 approval,
   and promotion checkout target are byte-identical.

## Exact-candidate evidence sequence

1. Manual **build** pins one source commit and retains archive+manifest bytes;
   it records run ID and archive/manifest/executable hashes.
2. T07 downloads those exact run bytes and runs the complete matrix, installed
   client, EOF, manifest, and Python-rollback proof. No local rebuild may
   substitute for this receipt.
3. T08 security/code/workflow review inspects the exact T07 run/SHA/hashes.
4. S4 approval names that same run ID, commit, tag, hashes, and destination.
5. **Promote** downloads only that retained pair. It creates or resumes according
   to `contracts/promotion-state-machine.md`, then T10 proves published bytes
   equal T07 and replays the consumer journey from GitHub assets.

## Required test matrix

| Input/surface | Expected observable result |
|---|---|
| Invalid `--project` CLI | Exit 64, `PREVIEW_ROOT_INVALID\n` stderr, zero stdout, no root read. |
| Valid selected worktree | Accepted as root; only an entry escaping to sibling worktree is denied. |
| CWD/env/client URI/root attempt | No authority change from explicit `--project`. |
| Reparse/outside final target | `PREVIEW_PATH_REFUSED`, no target content or partial output. |
| Invalid arguments | Closed `INVALID_TOOL_ARGUMENTS` triple. |
| Attribute/open/read failure | Closed `PREVIEW_SEARCH_UNREADABLE` triple. |
| Every count/size/deadline limit | Closed `PREVIEW_SEARCH_BUDGET_EXCEEDED` triple. |
| Legacy/forbidden methods and names | Method-not-found or text-only unknown-tool; no side effect. |
| Valid search/EOF/rollback | Exact modern output, bounded exit, installed Python remains `PRODUCT_WORKS`. |
