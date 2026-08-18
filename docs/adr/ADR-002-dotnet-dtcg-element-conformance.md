# ADR-002: Resolve DTCG in the .NET host and observe live elements through the existing FlaUI bridge

## Status

Accepted

## Context

The source-only stateless .NET MCP candidate now owns a modern MCP front door and native debugger lifecycle, but it has no UI inspection tools. The repository already carries a separate Windows-only C# FlaUI bridge that can connect to a process, locate UIA elements, and return stable identity plus bounding rectangles. The public Python host currently owns that bridge client, but adding another Python feature would deepen the route the project is replacing.

The stable DTCG Format Module 2025.10 provides the M1 token-document contract; the Resolver Module defines a separate context-permutation surface that M1 deliberately does not claim. DTCG does not define which token applies to which UI property, and Windows UI Automation does not expose general computed color, typography, spacing, border, or token provenance. Raster screenshots cannot close that gap deterministically.

The first increment must be a truthful, releasable vertical slice that expands the .NET route without claiming universal visual conformance.

## Decision

Add a `.NET`-native `check_element_tokens` tool to the stateless candidate. Keep the named `dtcg-format-2025.10-m1-dimension-profile`, alias resolution, normalization, comparison, typed outcomes, and MCP envelopes in the cross-platform `net8.0` host. M1 does not claim full Format or Resolver conformance; valid `$extends`, JSON Pointer `$ref`, resolver documents, and context permutations receive the explicit `DTCG_FEATURE_UNSUPPORTED` outcome.

For Windows live-element observation, launch and own a dedicated `FlaUIBridge.exe` subprocess from each .NET debug session that needs observation, using a new bounded C# JSON-lines client. Never share a Python-owned running bridge. Resolve the executable path at runtime from an explicit environment/configuration seam; never hardcode a build, cache, profile, or workspace path. Reuse the bridge binary and selector logic, but add a narrow geometry response that reports unique-match evidence, raw UIA rectangle, positive WPF framework identity, and window DPI provenance.

Track the debuggee operating-system PID and locality from the DAP `process` event in `NetCoreDbgSession`; never infer either from descendant scans. A local bridge connection is authorized only when `isLocalProcess` is explicitly `true` and `systemProcessId` is positive. Unavailable bridge or unsupported platform uses the PRD's closed `ELEMENT_OBSERVER_UNAVAILABLE` error; zero and multiple matches use `ELEMENT_NOT_FOUND` and `ELEMENT_AMBIGUOUS`. Non-error `UNOBSERVABLE` is reserved for post-observation evidence gaps such as absent/unknown locality or PID, missing WPF identity or DPI, unsupported property mapping, or failed property read; none may become `FAIL`.

Milestone M1 supports only rendered WPF bounding `width` and `height` against the M1 DTCG `dimension` profile in `px`. Its named platform policy normalizes physical UIA geometry to WPF device-independent units with reported DPI; this is not claimed as universal DTCG semantics. It reports `mode: value`; it does not claim Resolver Module support or token/resource binding. Screenshots are outside the verdict path.

Do not modify Python source or expose a new Python tool. The retained Python route continues unchanged while the .NET candidate gains the capability.

## Alternatives considered

### Embed FlaUI.UIA3 directly in the stateless host

Rejected. `FlaUI.UIA3` and the existing bridge target `net8.0-windows`; embedding them would either make the cross-platform debugger host Windows-only or require a second platform-specific host composition. It would also duplicate bridge lifecycle and UIA behavior already present in the repository.

### Infer conformance from screenshots

Rejected. Pixel values vary with DPI, font rasterization, animation, antialiasing, color management, and capture method. A screenshot can corroborate a failure but cannot prove property-level or binding conformance.

### Accept a caller-supplied computed-style snapshot

Rejected as the headline slice. It can test the comparison engine but cannot establish that the values came from the named live element. Such snapshots remain useful as unit-test fixtures.

### Implement full WPF/Avalonia instrumentation first

Deferred. Framework adapters are required for color, typography, spacing, templates, and binding provenance, but starting there would make the first increment too broad. The geometry slice proves the MCP, capability, observer, DTCG, and result boundaries first.

## Consequences

- The cross-platform .NET host remains free of Windows UI packages.
- Windows UI observation remains a separately bounded optional capability.
- The first feature is deliberately narrow but honest: live element geometry only.
- A second process exists during checks, so startup, framing, response-size, cancellation, and cleanup ownership require tests.
- DAP process-event handling becomes part of native session state without changing existing tool envelopes.
- Future WPF and Avalonia adapters plug into the canonical observation/result boundary rather than changing the MCP tool.
- Equal values do not prove token use; binding conformance remains unavailable until an adapter supplies resource/property-system provenance.

## Rollback

Remove `check_element_tokens`, its DTCG/bridge components, and DAP debuggee-PID observation. The existing three .NET tools and retained Python consumer remain unchanged. No data migration or public package rollback is required.

## Related records

- `specs/002-dtcg-element-conformance/spec.md`
- `docs/adr/ADR-001-stateless-dotnet-strangler.md`
- `specs/001-mcp-stateless-strangler/spec.md`
- DTCG Format Module 2025.10: <https://www.designtokens.org/TR/2025.10/format/>
- DTCG Resolver Module 2025.10: <https://www.designtokens.org/TR/2025.10/resolver/>
