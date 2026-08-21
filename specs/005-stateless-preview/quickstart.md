# Quickstart — A1 Safe Read-Only Stateless Preview

This is a future validation playbook, not an execution receipt or publication
authorization. A1-T07 runs the source-run artifact steps before S4; A1-T10
repeats the installed journey from the published GitHub asset.

## Preconditions

1. Exact source-pinned `build` workflow run has produced archive and manifest.
2. T07 downloads that pair and verifies the manifest equations/hashes.
3. A local fixture root contains a contained `.cs` symbol, denied reparse and
   sibling-worktree escape fixtures, and resource-boundary fixtures.
4. Installed Python `netcoredbg-mcp` remains available for the rollback journey.

## Consumer journey

1. Extract the verified self-contained `win-x64` archive.
2. Launch with exactly `--project <fixture-root>` and connect a local stdio MCP
   client carrying the three required 2026-07-28 `_meta` fields.
3. Call `server/discover`, then `tools/list`; observe one cached
   `find_code_symbol` definition.
4. Call the tool with a contained symbol; verify exact result/text parity,
   root-relative output, deterministic order, and stdout JSON-RPC purity.
5. Run every row in the negative/containment matrix in [the A1 specification](./spec.md).
6. Close stdin; verify bounded process exit and no retained state.
7. Remove only preview selection and replay the installed Python consumer
   journey; it must reach `PRODUCT_WORKS`.

## Release identity journey

1. Before S4, record build-run ID, source SHA, archive/manifest/executable
   hashes, complete T07 receipt, and T08 S2/S3 receipt.
2. After explicit S4 approval, `release` runs promotion state classification.
3. Download the remote archive and manifest; prove their bytes equal T07.
4. Repeat the consumer journey above from remote release assets.
5. On API/upload failure, reclassify with
   [promotion-state-machine.md](contracts/promotion-state-machine.md); never
   rebuild, overwrite an asset, or move/delete a tag.
