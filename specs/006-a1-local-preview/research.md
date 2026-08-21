# Research — A1 Local Stateless Preview

## Decisions

| Topic | Decision | Rationale |
|---|---|---|
| Child boundary | Implement parent A1 T001–T005 only. | It yields the local source-run walking skeleton while excluding build/promotion/publication work. |
| Shared ownership | Extract traversal/matching to SDK-free core with `LegacySearchPolicy` and `PreviewSearchPolicy`. | One algorithm owner without changing compatibility root selection or best-effort behavior. |
| Root authority | Preview has its own exact `--project` parser. | `ProjectRootResolver` intentionally accepts environment, client-root, and CWD fallbacks required only by the legacy route. |
| Modern host | New closed process reuses modern stdio/metadata/catalog conventions but not the existing nine-tool composition. | Current Stateless host initializes DAP/Native Scene ownership that A1 excludes. |
| Tests | Reuse official SDK client/output-resolution patterns; use a preview-specific source fixture. | DAP fixtures and environment-mutating collections are not relevant to source search. |
| Workflow/publication | Exclude all workflow, artifact, tag, release, package, and selector work. | User explicitly requested this local implementation iteration without workflow. |
| Hard links | Do not claim static hard-link provenance protection in this child. | A regular hard link has no reparse target; detecting alternate file names needs a later Windows file-ID policy. A1L protects lexical/reparse/final-path authority only and records this limit honestly. |

## Current source facts

- `host/NetCoreDbg.Mcp.Host/NativeCodeSearch.cs` contains the legacy SDK adapter,
  catalog helpers, and embedded BCL `SourceSearchEngine`.
- `ProjectRootResolver.cs` owns legacy environment/explicit/client-root/CWD
  precedence and remains unchanged.
- `ToolsRelay.cs` remains the single legacy list/call relay; it locally owns three
  code-search names and relays `search_source` through Python.
- `src/netcoredbg_mcp/code_search.py` is the behavior/rollback oracle; its
  Python-regex subprocess search remains Python-owned.
- Existing parity owners are the five named tests in `tests/test_host_proxy.py`;
  `tests/test_code_search.py` gives direct search coverage.
- `host/NetCoreDbg.Mcp.Stateless/Program.cs` provides the modern MCP SDK 2.1.0
  stdio/caching/error patterns but also registers excluded stateful tools.
- `ModernMcpProcessDriver` and `ModernMcpFirstWireDriver` provide usable official
  SDK first-request, metadata, stdout, and EOF test seams.

## Alternatives considered

| Alternative | Disposition | Reason |
|---|---|---|
| Add a runtime profile to the nine-tool Stateless host | Rejected | Packaged stateful components remain reachable through composition defects. |
| Duplicate the search engine for preview | Rejected | Copies matching/traversal behavior and breaks parity ownership. |
| Use `ProjectRootResolver` for preview | Rejected | Its deliberate fallback authority violates exact `--project` scope. |
| Implement hard-link provenance detection now | Deferred | It needs a separate Windows file-ID policy and exceeds this D1 child; the limitation is explicit and blocks any stronger release claim. |

## Required verification

- Existing compatibility/Python parity: `uv run --no-sync pytest tests/test_code_search.py tests/test_host_proxy.py -q`.
- Legacy host focused suite: `dotnet test host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj -c Release`.
- New core and preview focused test projects, then local source-run quickstart.

## Constraints carried from parent A1

The local preview retains the parent’s exact modern metadata, one-tool input and
result/error shapes, strict reparse/final-path validation, limits, no-partial
rule, stdout purity, cancellation, and EOF behavior. A1 T006–T010 remain later
workflow/publication work and are not re-specified here.
