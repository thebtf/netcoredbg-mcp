---
feature_id: 002
slug: dtcg-element-conformance
title: ".NET-native DTCG element conformance"
status: SUPERSEDED
superseded_by: 003-native-scene-observation
created: 2026-08-18
baseline: main@258c132ef436c4bc75383080533fc64bc767ece0
design_rung: D2
release_intent: none
---

# PRD: .NET-native DTCG element conformance

> Superseded before implementation by [Spec 003](../003-native-scene-observation/spec.md); retained as a historical record.

## Problem

`netcoredbg-mcp` can locate live Windows UI Automation elements and capture raster evidence, but it cannot deterministically answer whether an element conforms to design tokens. The existing Python route exposes identity, geometry, accessibility state, and screenshots; it does not expose computed visual style or token provenance. Screenshot inference cannot be an acceptance authority because DPI, antialiasing, font fallback, animation, color management, and capture paths change pixels without changing the design contract.

The feature closes the first bounded gap: **the source-only .NET MCP candidate cannot resolve stable DTCG 2025.10 tokens and compare them with deterministic properties of a live debuggee element.**

## Product outcome

Add a read-only `.NET` tool named `check_element_tokens` to the internal stateless candidate. It receives an explicit `debugSessionId`, a unique UIA selector, an inline DTCG 2025.10 token document, and property-to-token assertions. It returns per-assertion `PASS`, `FAIL`, or `UNOBSERVABLE` with normalized expected/actual values and provenance.

The first shipping milestone supports only live `width` and `height` checks against DTCG `dimension` tokens on Windows. It uses the existing C# FlaUI bridge as a bounded observer and does not add or modify Python product code. Later milestones add framework-computed style and binding provenance without changing the result envelope.

## Users and journeys

### US-001 — Check a live element deterministically

A client starts a debug session, then calls `check_element_tokens` with the returned `debugSessionId`, a unique `automationId`, a DTCG document, and one or more width/height assertions. The .NET host resolves the live debuggee process, asks the existing C# FlaUI bridge for the element rectangle, resolves the token, normalizes units, and returns a complete result.

### US-002 — Receive an honest unavailable result

If the debuggee PID, bridge, selector, property, token, or platform capability is unavailable, the call returns a typed complete error or `UNOBSERVABLE`. Missing evidence never becomes `PASS` or a guessed value.

### US-003 — Distinguish value from binding conformance

The first milestone reports `mode: value`. It does not claim that an equal value came from a token/resource. Future WPF/Avalonia adapters may report `mode: binding` only when they provide resource/property-system provenance.

## Public boundary

### Tool input

```json
{
  "debugSessionId": "opaque process-local capability",
  "selector": { "automationId": "PrimaryButton" },
  "tokens": {
    "control": {
      "button": {
        "width": { "$type": "dimension", "$value": { "value": 120, "unit": "px" } }
      }
    }
  },
  "assertions": [
    { "property": "width", "token": "control.button.width" }
  ]
}
```

First-milestone limits:

- selector: exactly one non-empty `automationId`;
- supported properties: `width`, `height`;
- supported token type: `dimension`;
- supported DTCG unit: `px`;
- maximum assertions: 16;
- maximum inline token document: 262,144 bytes after the canonical UTF-8 serialization defined by FR-009;
- no additional input properties.

The input schema is closed: root keys are exactly `debugSessionId`, `selector`, `tokens`, and `assertions`; `selector` contains only required `automationId`; every assertion contains only required `property` and `token`, and `property` is exactly `width` or `height`. Any other property or additional, missing, empty, wrong-typed, over-limit, or non-finite value is `INVALID_TOOL_ARGUMENTS`.

### Complete success result

```json
{
  "kind": "element_token_conformance",
  "mode": "value",
  "status": "PASS",
  "observer": {
    "name": "FlaUIBridge",
    "framework": "WPF",
    "frameworkMapping": "wpf-dip-to-dtcg-px-v1",
    "dpi": 144
  },
  "element": {
    "automationId": "PrimaryButton",
    "controlType": "Button",
    "className": "Button",
    "rectPhysicalPixels": { "x": 60, "y": 120, "width": 180, "height": 48 }
  },
  "checks": [
    {
      "property": "width",
      "token": "control.button.width",
      "expected": { "value": 120, "unit": "px" },
      "actual": { "value": 120, "unit": "px" },
      "comparator": "wpf-dip-quantized",
      "tolerance": { "value": 0.3333333333, "unit": "px" },
      "provenance": {
        "tokenProfile": "dtcg-format-2025.10-m1-dimension-profile",
        "tokenPath": "control.button.width",
        "observation": "UIA.BoundingRectangle.width",
        "normalization": "physicalPixels*96/GetDpiForWindow(topLevelHwnd)"
      },
      "status": "PASS"
    }
  ]
}
```

Aggregate status is `FAIL` when any observable assertion fails, `UNOBSERVABLE` when none fail and at least one cannot be observed, otherwise `PASS`. The structured-content schema is closed: the root has exactly `kind`, `mode`, `status`, `observer`, `element`, and `checks`; `observer` has exactly `name`, `framework`, `frameworkMapping`, and `dpi`; `element` is either `null` or an object with exactly `automationId`, `controlType`, `className`, and `rectPhysicalPixels`; every check has exactly `property`, `token`, `expected`, `actual`, `comparator`, `tolerance`, `provenance`, and `status`; provenance has exactly `tokenProfile`, `tokenPath`, `observation`, and `normalization`. For PASS/FAIL every observer, element, and provenance leaf is non-null and positively verified. For UNOBSERVABLE the keys remain required, but `element` may be `null`; unavailable `framework`, `frameworkMapping`, `dpi`, `actual`, `comparator`, or `tolerance` leaves are explicitly `null`, while provenance names the attempted observation and missing evidence. No undeclared fields are legal.

Example before any element snapshot can be acquired:

```json
{
  "kind": "element_token_conformance",
  "mode": "value",
  "status": "UNOBSERVABLE",
  "observer": {
    "name": "FlaUIBridge",
    "framework": null,
    "frameworkMapping": null,
    "dpi": null
  },
  "element": null,
  "checks": [
    {
      "property": "width",
      "token": "control.button.width",
      "expected": { "value": 120, "unit": "px" },
      "actual": null,
      "comparator": null,
      "tolerance": null,
      "provenance": {
        "tokenProfile": "dtcg-format-2025.10-m1-dimension-profile",
        "tokenPath": "control.button.width",
        "observation": "DAP.process",
        "normalization": "missing isLocalProcess/systemProcessId"
      },
      "status": "UNOBSERVABLE"
    }
  ]
}
```

Every result is an official complete `CallToolResult`; text content serializes the same object as `structuredContent`. `PASS`, `FAIL`, and `UNOBSERVABLE` use the closed non-error schema above with `isError: false`. Errors use these exact closed variants:

| Condition | `isError` | Exact `structuredContent` |
|---|---:|---|
| Invalid input | true | `{ "kind": "invalid_tool_arguments", "error": "INVALID_TOOL_ARGUMENTS", "tool": "check_element_tokens" }` |
| Session unavailable | true | `{ "kind": "debug_session_not_found", "error": "DEBUG_SESSION_NOT_FOUND" }` |
| Observer unavailable or oversized response | true | `{ "kind": "element_observer_unavailable", "error": "ELEMENT_OBSERVER_UNAVAILABLE" }` |
| No selector match | true | `{ "kind": "element_not_found", "error": "ELEMENT_NOT_FOUND" }` |
| Multiple selector matches | true | `{ "kind": "element_ambiguous", "error": "ELEMENT_AMBIGUOUS" }` |
| Malformed/invalid supported-profile token data | true | `{ "kind": "token_document_invalid", "error": "TOKEN_DOCUMENT_INVALID" }` |
| Valid DTCG feature outside M1 profile | true | `{ "kind": "dtcg_feature_unsupported", "error": "DTCG_FEATURE_UNSUPPORTED" }` |

Implementation MUST extend `specs/001-mcp-stateless-strangler/contracts/modern-front-door.schema.json` with these closed variants and add them to `toolStructuredContent`; `StructuredContentSchemaParityTests` MUST prove the runtime objects and frozen schema remain identical. `ELEMENT_UNOBSERVABLE` is not an error code: property/framework/DPI evidence gaps after a valid request use the non-error `status: UNOBSERVABLE` envelope, with `element: null` when observation never reached an element snapshot.

## Functional requirements

- **FR-001 Stable specification:** M1 implements a named `dtcg-format-2025.10-m1-dimension-profile`, not full Format conformance. The stable DTCG Format Module 2025.10 defines valid input; preview drafts are not authority. Valid standard features outside the profile are reported as unsupported, not invalid.
- **FR-002 Explicit mapping:** Every assertion names both an element property and a token path. Group names or equal values do not imply mapping.
- **FR-003 Token resolution:** The M1 profile supports explicit or nearest inherited `$type`, `$root` tokens, curly-brace aliases, and chained aliases according to the matrix below. Unresolved, circular, malformed, or type-mismatched values are invalid. Valid `$extends`, JSON Pointer `$ref`, resolver documents, modifiers, sets, and context permutations return `DTCG_FEATURE_UNSUPPORTED` until implemented.
- **FR-004 Live identity:** Resolve the element from the debuggee process associated with the explicit `debugSessionId`. The DAP `process` event MUST positively report `isLocalProcess: true` and a positive `systemProcessId`; absent, false, or unknown locality yields the closed UNOBSERVABLE result with `element: null`. No process scan or connection-global element exists.
- **FR-005 Unique selector:** Zero or multiple selector matches cannot produce conformance `PASS` and use the closed `ELEMENT_NOT_FOUND` or `ELEMENT_AMBIGUOUS` errors.
- **FR-006 Normalization:** M1 accepts only a WPF observer. DPI comes from `GetDpiForWindow` on the top-level HWND containing the selected element and MUST be an integer in `96..768`; otherwise the result is UNOBSERVABLE. Normalize with `actual = physicalPixels*96/dpi`. The `wpf-dip-quantized` comparator passes when `abs(actual-expected) <= 48/dpi`, one half physical pixel expressed in WPF/DTCG units; it fails only outside that tolerance. This is a named WPF platform policy, not universal DTCG semantics.

### M1 DTCG classification matrix

| Construct | M1 classification | Deterministic rule |
|---|---|---|
| Token explicit `$type: dimension` | allowed | Explicit token type has highest precedence. |
| Closest parent group `$type: dimension` | allowed | Used only when the token has no explicit `$type`; walk to the nearest parent. |
| Explicit token type plus different inherited type | allowed or invalid | Explicit type wins; the resolved explicit type MUST be `dimension`, otherwise `TOKEN_DOCUMENT_INVALID`. |
| No resolvable type | `TOKEN_DOCUMENT_INVALID` | M1 never infers type from names or groups. |
| `$root` token | allowed | Addressed explicitly as `group.$root`; it follows the same value/type rules as any token. |
| `$description` | ignored | Valid metadata preserved by input but irrelevant to comparison. |
| `$deprecated` | ignored | Valid metadata preserved by input but irrelevant to comparison. |
| `$extensions` | ignored | Valid extension data is not interpreted by M1 and cannot change comparison. |
| Curly-brace token alias | allowed | Resolve complete-token values transitively; cycles/unresolved targets are invalid. |
| `$extends` | `DTCG_FEATURE_UNSUPPORTED` | Valid Format feature outside the named M1 profile. |
| JSON Pointer `$ref` | `DTCG_FEATURE_UNSUPPORTED` | Valid Format feature outside the named M1 profile. |
| Resolver document/set/modifier/context | `DTCG_FEATURE_UNSUPPORTED` | Resolver Module is outside M1. |

Every matrix row has an independent RED/GREEN classification test. Type resolution order is: explicit token `$type` → resolved group `$type` (when `$extends` becomes supported) → closest parent group `$type` → invalid.
- **FR-007 Typed outcomes:** Distinguish invalid arguments, invalid token document, valid-but-unsupported DTCG feature, debug session not found, observer unavailable, element not found/ambiguous, `PASS`, `FAIL`, and `UNOBSERVABLE` through the closed envelopes above.
- **FR-008 Evidence authority:** Screenshots may be attached later as corroboration but never determine the conformance verdict.
- **FR-009 Bounded processing:** The assertion limit is 16. After MCP SDK deserialization, M1 serializes only the `tokens` value using RFC 8785 JSON Canonicalization Scheme (JCS): UTF-8 output, UTF-16 code-unit property sorting, ECMAScript number serialization, minimal required string escaping, and no Unicode normalization or BOM. More than 262,144 canonical bytes is `INVALID_TOOL_ARGUMENTS`. This check occurs before cloning, token traversal, alias resolution, or bridge launch. Boundary fixtures cover non-ASCII strings, escaped-versus-literal Unicode, and numerically equivalent `1`, `1.0`, and exponent spellings resolving to one JCS representation. A separate host-level pre-deserialization MCP frame limit is required before public exposure but is not falsely claimed by M1. `FlaUiBridgeClient` MUST reject a response line exceeding 1,048,576 bytes, including its newline, before JSON deserialization and return `ELEMENT_OBSERVER_UNAVAILABLE`.
- **FR-010 .NET-only growth:** Do not add, edit, or route new behavior through Python. The legacy Python path remains unchanged while the native route expands.
- **FR-011 Cross-platform honesty:** Non-Windows hosts return observer-unavailable without breaking the existing cross-platform debugger tools.
- **FR-012 Provenance:** Every PASS/FAIL result names observer, positively verified framework mapping, DPI, token profile/path, resolved and actual values, comparator, normalization source, and conformance mode. UNOBSERVABLE retains the same closed keys with explicit nulls for unavailable leaves and provenance that names the attempted observation and missing evidence.

## Non-functional requirements

- **NFR-001:** The tool is read-only and idempotent for a stable debuggee UI state.
- **NFR-002:** stdout remains MCP-only; bridge diagnostics remain off stdout.
- **NFR-003:** No remote listener, token-file network fetch, arbitrary URI dereference, or filesystem token import is introduced.
- **NFR-004:** The existing `start_debug`, `get_debug_state`, and `stop_debug` contracts remain unchanged.
- **NFR-005:** Bridge lifecycle and cleanup are bounded and owned by the .NET candidate.
- **NFR-006:** The result never reports more certainty than the observer provides.

## Acceptance criteria

- **AC-001:** A real candidate process advertises `check_element_tokens` after the existing three tools without changing their schemas.
- **AC-002:** A controlled live WPF fixture returns PASS/FAIL under `wpf-dip-quantized`; cases cover DPI 120 (125%), DPI 144 (150%), exact integral values, fractional normalized values inside half-pixel tolerance, and values outside tolerance.
- **AC-003:** Missing/ambiguous element, missing/remote/unknown-locality process event, non-Windows host, unavailable bridge, unknown framework, absent/out-of-range DPI, invalid input property, unsupported DTCG unit, invalid DTCG input, and valid-but-unsupported DTCG features return their exact typed outcomes. A property other than width/height is `INVALID_TOOL_ARGUMENTS`; valid DTCG features outside the profile, including `rem`, are `DTCG_FEATURE_UNSUPPORTED`. UNOBSERVABLE pre-observation cases assert the exact `element: null` example shape.
- **AC-004:** Alias resolution, inherited dimension type, unresolved alias, circular alias, non-finite number, wrong token type, `$extends`, and JSON Pointer `$ref` have RED/GREEN contract tests proving invalid versus unsupported classification.
- **AC-005:** No Python source or Python public tool contract changes in the diff.
- **AC-006:** The full existing stateless .NET suite remains green.

## Alternatives and challenge disposition

1. **Screenshot/pixel inference:** rejected; it cannot prove property values or token provenance.
2. **Embed FlaUI directly in the cross-platform host:** rejected; it would force a Windows target and duplicate the existing bridge boundary.
3. **Accept a caller-supplied style snapshot only:** rejected as the headline feature because it does not verify a live element.
4. **Start with full WPF/Avalonia computed-style instrumentation:** deferred; it is the correct later source for color/typography/binding, but it is not required to prove the first live end-to-end dimension slice.
5. **Chosen:** reuse the existing C# FlaUI bridge as an optional Windows observer and keep DTCG resolution/conformance in the cross-platform .NET host.

## Milestone map

| Milestone | Release-closing statement | Scope | Binding constraints |
|---|---|---|---|
| M1 — Live geometry | The .NET candidate could not compare any live UI element property with DTCG; width/height checks now work end to end. | DTCG dimension resolver, DAP debuggee PID, bridge reuse, `check_element_tokens`, PASS/FAIL/UNOBSERVABLE. | No Python changes; Windows observer optional; stable result envelope. |
| M2 — WPF computed value | A WPF element's color, typography, border, and spacing could not be compared deterministically; a WPF adapter now exposes effective values. | WPF property-system adapter and normalized visual types. | Preserve M1 envelope; screenshot remains non-authoritative. |
| M3 — Binding provenance | Equal values could not prove design-token use; WPF resource/value-source provenance now distinguishes value and binding conformance. | Resource key and property value-source mapping. | `mode: binding` only with positive provenance. |
| M4 — Avalonia parity | Avalonia elements could not use the conformance contract; an Avalonia adapter now emits the same canonical snapshot. | Avalonia property/resource adapter. | No WPF-specific fields in the public result. |

## Requirements-to-file map

| Requirement | First milestone file(s) |
|---|---|
| FR-001–FR-003, FR-006 | `host/NetCoreDbg.Mcp.Stateless/DesignTokens/DtcgConformance.cs` |
| FR-004–FR-005, FR-011 | `host/NetCoreDbg.Mcp.Stateless/DebugAdapter/NetCoreDbgSession.cs`, `host/NetCoreDbg.Mcp.Stateless/Ui/FlaUiBridgeClient.cs` |
| FR-007–FR-010, FR-012 | `host/NetCoreDbg.Mcp.Stateless/Program.cs`, `specs/001-mcp-stateless-strangler/contracts/modern-front-door.schema.json` |
| AC-001–AC-004 | `host/NetCoreDbg.Mcp.Stateless.Tests/DesignTokens/DtcgConformanceTests.cs`, `host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/ElementTokenContractTests.cs`, `host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/StructuredContentSchemaParityTests.cs` |
| AC-005–AC-006 | repository diff gate and full stateless test project |

## Exclusions

M1 does not support color, typography, borders, radius, spacing, opacity, shadow, transitions, `rem`, remote token documents, JSON5/JSONC, resolver documents, screenshot verdicts, token-binding provenance, WPF resource inspection, Avalonia, WinForms style inspection, public Python cutover, or Python deletion. Valid Format 2025.10 `$extends` and JSON Pointer `$ref` inputs are explicitly recognized as `DTCG_FEATURE_UNSUPPORTED`, not rejected as invalid. These are later milestones or explicit backlog, not hidden partial support.
