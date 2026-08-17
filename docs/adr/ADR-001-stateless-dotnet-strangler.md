# ADR-001: Keep the public Python route while adding an internal stateless .NET MCP candidate

## Status
Accepted

## Context
The published `netcoredbg-mcp` route is Python-backed and must remain usable while the repository proves a modern MCP 2026-07-28 implementation. The .NET candidate needs native ownership of a `netcoredbg --interpreter=vscode` process, DAP framing, request correlation, state observation, and bounded cleanup. No existing native C# seam owns that lifecycle.

## Decision
Add a separate internal .NET executable at `host/NetCoreDbg.Mcp.Stateless/`. It uses the official C# `ModelContextProtocol` SDK for the modern MCP front door and a narrow BCL-only `NetCoreDbgSession` for the owned DAP lifecycle. It exposes only the M1 `start_debug`, `get_debug_state`, and `stop_debug` tool catalog and keeps the Python package, console entrypoint, legacy relay, and consumer selection unchanged.

The candidate is a reversible strangler step: rollback is non-selection or removal of the candidate followed by replay of the retained Python journey. This ADR does not authorize package publication, public-entrypoint cutover, or legacy-route deletion; those require their own accepted scope and evidence. External review is nonblocking evidence: its availability and non-critical later findings do not delay continued development, merge, or an otherwise consumer-proven release, and deferred findings belong to a named next patch.

## Consequences
- Modern MCP behavior can be proved without changing current consumers.
- The candidate owns process-tree cleanup and DAP mechanics rather than adding a general DAP framework or extending the legacy relay.
- Two routes coexist temporarily, so candidate and retained-Python evidence remain separate.
- Future cutover, package, and public-consumer decisions remain deliberately undecided.

## Related records
- `specs/001-mcp-stateless-strangler/spec.md`
- `specs/001-mcp-stateless-strangler/architecture.md`
- `specs/001-mcp-stateless-strangler/plan.md`
