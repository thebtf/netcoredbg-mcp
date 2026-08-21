# Implementation Plan: A1 Local Stateless Preview

**Branch:** `work/a1-local-preview` | **Spec:** `specs/006-a1-local-preview/spec.md`

**Input:** `specs/006-a1-local-preview/{checklists/requirements.md,research.md,data-model.md,architecture.md,contracts/modern-preview-contract.md,quickstart.md}` and parent `specs/005-stateless-preview/plan.md`.

## Summary

Implement a source-run, one-tool local preview that is useful and safe before
A1’s later build/promotion/publication work. This child realizes parent T001–T005
plus only their direct local test, parity, rollback, and one-checker acceptance
evidence. The change extracts common search logic behind explicit policies,
preserves the legacy route, and introduces a separate strict modern preview
process.

## Design Depth

**D1 child feature:** This artifact implements one bounded child of accepted D2
A1. A wrong change can affect search containment and legacy parity, but its
scope is one local route with no state migration, public distribution, workflow,
or release. It will be consumed by maintainers and later A1 tasks; parent ADR-004
already owns the alternatives and release architecture.

## Technical Context

| Context | Decision |
|---|---|
| Language/runtime | C# / .NET 8; preview uses existing modern MCP SDK 2.1.0 patterns. |
| Core boundary | SDK-free BCL traversal/matching/policy engine; root selection stays adapters. |
| Legacy integration | Compatibility host stays SDK 1.4.1 and keeps resolver, relay, catalog, envelopes, and Python-owned search. |
| Preview integration | New process has one strict root parser, one tool catalog, and modern stdio handlers only. |
| Test strategy | RED policy/process tests first; exact legacy parity remains green; source-run local quickstart after focused tests. |
| Exclusions | No `.github/workflows`, release protocol, artifact manifest/promotion, tags, PyPI, selector, release, or S4. |

## Constitution Check

No `.specify/memory/constitution.md` exists. Repository `AGENTS.md`, parent A1,
and ADR-004 bind this child: no Python cutover, local strict root authority,
legacy behavior preservation, no stubs, regression-first tests, and no consumer
publication. **PASS for the planned local implementation.**

## Boundary and integration points

| Boundary | Child action | Must remain unchanged |
|---|---|---|
| Python route | Retain as parity/rollback oracle. | Console entrypoint, package, `search_source`, consumer selection. |
| Compatibility host | Reference shared core through `LegacySearchPolicy`. | `ProjectRootResolver`, relay catalog ownership, root precedence, public envelopes. |
| Shared core | Own traversal, ignore/matching, deterministic outputs, typed policy outcomes. | No MCP SDK or root-source selection. |
| Preview host | Parse `--project`; map core outcomes to modern one-tool MCP responses. | No debug session, bridge, Native Scene, Python relay, mux, HTTP, or remote listener. |
| Local tests | Exercise core, process contract, fixture, and Python rollback. | No workflow, package, or publication assertions. |

## Implementation phases

1. Freeze the parent contract into the local contract and write RED policy/parity
   tests, including injected filesystem failures and limit boundaries.
2. Extract the BCL core; migrate compatibility host with `LegacySearchPolicy`;
   run all existing parity owners.
3. Write preview process RED tests and implement the closed modern executable
   with `PreviewProjectRootParser` and `PreviewSearchPolicy`.
4. Run focused suites and the local quickstart, including strict-root denial,
   EOF, and Python rollback.

## Requirements-to-files map

| Requirement | Planned files |
|---|---|
| A1L-REQ-001, A1L-REQ-003 | `host/NetCoreDbg.Mcp.CodeSearch.Core/{SearchPolicy,SearchFailure,SymbolSearchEngine}.cs`; `host/NetCoreDbg.Mcp.CodeSearch.Core.Tests/` |
| A1L-REQ-004 | `host/NetCoreDbg.Mcp.Host/{NetCoreDbg.Mcp.Host.csproj,NativeCodeSearch.cs}`; existing `ProjectRootResolver.cs`, `RelayComposition.cs`, `ToolsRelay.cs` as unchanged integration owners; `tests/test_host_proxy.py` |
| A1L-REQ-001, A1L-REQ-002, A1L-REQ-005 | `host/NetCoreDbg.Mcp.Stateless.Preview/{NetCoreDbg.Mcp.Stateless.Preview.csproj,Program.cs,PreviewProjectRootParser.cs,PreviewToolCatalog.cs,PreviewToolHandler.cs}`; `host/NetCoreDbg.Mcp.Stateless.Preview.Tests/` |
| A1L-REQ-006 | `tests/fixtures/PreviewSearchApp/`; `tests/preview/`; `specs/006-a1-local-preview/quickstart.md` |

## Project Structure

```text
host/
├── NetCoreDbg.Mcp.CodeSearch.Core/             # New shared BCL engine
├── NetCoreDbg.Mcp.CodeSearch.Core.Tests/       # New policy/failure tests
├── NetCoreDbg.Mcp.Host/                        # Existing legacy core consumer
├── NetCoreDbg.Mcp.Stateless.Preview/           # New closed local preview
└── NetCoreDbg.Mcp.Stateless.Preview.Tests/     # New modern process tests

tests/
├── fixtures/PreviewSearchApp/                  # Deterministic local source fixture
├── preview/                                    # Local quickstart/rollback harness
├── test_code_search.py                         # Existing Python oracle
└── test_host_proxy.py                          # Existing legacy parity
```

## Test Plan

- Core policy tests: verified opens, reparse/final-path failure, unreadable I/O,
  limits, deadline/cancellation, redaction, and no partial results.
- Legacy parity: exact catalog/call, rollback, root precedence, order/ignore/
  symlink behavior, Python-owned timeout continuation.
- Preview process tests: first request, metadata/version, catalog/input/result/
  error contract, authority matrix, stdout/EOF, and excluded paths.
- Local smoke: valid fixture result, denial subset, EOF exit, and installed Python
  rollback. No artifact, release, or workflow validation belongs here.
