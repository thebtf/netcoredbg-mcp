# Native Scene Probe M0/M1 Acceptance Receipt

## Candidate and approval

- Base HEAD before final-acceptance work: `c4d7a5a85655cff63856e54a4dbf14027b64d9f1`
- Candidate: exact uncommitted tree on `work/native-scene-final-acceptance` (20 tracked modifications + 1 untracked source file at fingerprint time)
- Operator reapproval: 2026-08-19, explicit approval of the corrected C015 corpus bytes
- Authoritative exact hashes:
  - `contracts/native-scene-probe.schema.json`: `f446166f9a1062d3e1a2190327d06c04905e76a1c1f81af16c87572394f90022`
  - `contracts/native-scene-artifact.schema.json`: `07c257c9b5f75c01aa4f4141968c789b045d7c831575343df429075c732f7668`
  - `contracts/parity-corpus.json`: `90c24f8f9706c207ca3ecf8dee93d1937c16a6be45feac65d812e48853bc4621`
- Superseded corpus hash: `9308da9c3807b4967b175525c9df4183593b4537a26be13cbe36bc8edd1faadf`
- C015 correction: lossless PNG is required; an optional WebP preview, if present, is independently identified, `preview_only`, and non-authoritative. No codec dependency or false preview bytes were added.
- Candidate byte identity (recorded after the final source/test edit, before the final full gate run):
  - `git diff HEAD` over every tracked path except this receipt, SHA-256: `ee6dc9e6d22e66407789613918d9222c8be14bf3e14b4e235cf45b82341a3e7a` (includes the T035 checklist closure; the prior hash `82bfcd11801f0b883ba2c37c851a868b5313337ae3f6fa0832379bd06605dc49` pinned the identical tree with T035 still unchecked and was independently recomputed by `agent://ReceiptProvenanceCriteriaCheck`)
  - sole untracked source `host/NetCoreDbg.Mcp.Stateless/NativeScene/NativeSceneProbeChannel.cs`, SHA-256: `ecd8d617bfd1f48fe86f4e149827a3c45e9ce7bae89e9581521f3929fc766c8a`
  - this receipt is the one excluded path (self-reference); every other candidate byte is pinned by the two hashes above.

## Deterministic gates

| Gate | Result | Evidence |
|---|---|---|
| Reapproved M0-G0 contract gates | PASS 42/42 | `artifact://1675` |
| C015 live lossless/optional-preview behavior | PASS 1/1 | `artifact://1679` |
| Full T032 eight-class acceptance (post-Unicode-fix tree) | PASS 112/112, 0 skipped | `artifact://1723` |
| T033 cleanup subset (post-Unicode-fix tree) | PASS 26/26, 0 skipped | `artifact://1723` |
| Exact scalar C020 containment | PASS 1/1 | `artifact://1663` |
| Int64 scene epoch | PASS 1/1 | `artifact://1657` |
| Production C014 SessionBinding stale-wait path | PASS 1/1 | `artifact://1615` |
| Guarded/cancellation/C014/C020 focused paths | PASS 4/4 | `artifact://1635` |
| T035 visual-staging + 16 MiB probe-frame regressions | PASS 4/4 | `artifact://1714` |
| T035 Unicode-safe chunk regression (non-BMP scalar across the 256-code-unit boundary, exact round-trip) | PASS 1/1 | `artifact://1721` |

## C001-C024 behavioral mapping

| Case | Runtime | Result | Owner |
|---|---:|---|---|
| C001 | yes | PASS | capability declaration live test |
| C002 | no | PASS | malformed-version contract test |
| C003 | yes | PASS | inactive-version production classifier test |
| C004 | yes | PASS | unsupported M1 declaration + live call/no native work |
| C005 | yes | PASS | live lossless PNG capture/reconstruction |
| C006 | yes | PASS | 65,536-byte bounded artifact read |
| C007 | yes | PASS | terminal offset-equals-length read |
| C008 | yes | PASS | foreign/unknown artifact non-disclosure |
| C009 | yes | PASS | post-stop unavailable artifact |
| C010 | yes | PASS | fixed-chunk and unaligned two-chunk tamper containment |
| C011 | yes | PASS | equal-revision in-process COMPLETE scene |
| C012 | yes | PASS | changed-revision PARTIAL scene |
| C013 | yes | PASS | live no-probe UIA PARTIAL; honest UNOBSERVABLE stability and guarded issues |
| C014 | yes | PASS | prior STABLE epoch 41, changed layout epoch 42, production UI_NOT_STABLE/no artifact |
| C015 | yes | PASS | required lossless PNG; optional preview invariants; no comparison authority |
| C016 | yes | PASS | incomplete element PARTIAL + ADAPTER_FACT_UNOBSERVABLE artifact |
| C017 | no | PASS | invalid artifact capability/range contract tests |
| C018 | no | PASS | settle sample-count bounds |
| C019 | yes | PASS | depth-17 schema-valid input rejected before DTO |
| C020 | yes | PASS | exact scalar 262,145-byte payload rejected/terminalized before DTO/artifact; no second request |
| C021 | no | PASS | omitted primitive schema rejection |
| C022 | no | PASS | duplicate primitive schema rejection |
| C023 | no | PASS | historical revalidation receipt schema rejection |
| C024 | yes | PASS | selectedState/currentState 262,145-byte theories rejected before observer work |

Denominators: 24/24 cases mapped; 18/18 runtime-required cases have executed behavioral owners; 6/6 contract-only cases have explicit negative/schema owners.

## Session, artifact, lifecycle, and authority evidence

- Every live observer path starts from an explicit local `debugSessionId` and positively bound process identity; no process scan or Python route is used.
- COMPLETE visual evidence includes `image/png` + `lossless_visual`; COMPLETE element/native-scene evidence includes a retrievable `application/vnd.netcoredbg.native-scene+json` + `observed_facts` descriptor.
- Artifact reads are capability/session-bound, range-bounded, hash/length checked, and touched-chunk verified before bytes are released. No response exposes a path, root, internal chunk table, or storage authority.
- Cancellation, timeout, malformed/mismatched response, and oversized custom payload terminalize the one-connection probe channel before later requests can consume stale frames.
- Every visual artifact staging handle is aborted with non-cancellable cleanup on any non-committed exit; a post-stage identity race leaves no staged file and a later capture succeeds.
- Producer and host share an exact 16,777,216-byte probe-response limit. A valid 256-node WPF response above 1 MiB crosses the real channel twice; a declared frame above 16 MiB is rejected before payload allocation.
- Long WPF text is represented in schema-valid 256-character opaque chunks in the scene artifact, preserving bounded facts without exceeding `jsonValue` scalar limits. Chunk boundaries are UTF-16-safe: a boundary that would split a surrogate pair moves one code unit earlier, and the round-trip regression covers a non-BMP scalar exactly across the 256-code-unit boundary.
- Guarded UIA never claims atomic COMPLETE or fabricated settle facts. Qualified guarded facts remain PARTIAL with `ATOMICITY_UNPROVEN_UIA_GUARDED`; unobserved stability conditions remain unobservable.
- WPF probe stability is observer-owned and materialized in the dispatcher-affine transaction. Scene epochs are non-negative Int64 end-to-end.
- C014 production evidence proves a previous wait receipt is historical only and cannot authorize later capture.
- No design comparison, DTCG/token resolution, verdict, diagnosis, repair advice, Factory/Gallery implementation, `check_element_tokens`, Python dependency/change, public route cutover, package publication, release, deployment, or M2-M5 work is included.

## Review disposition

All landed findings were fixed and rechecked. The last exact C020 correction received a CLEAN independent review (`agent://NativeSceneScalarFinalReview`) and PASS factual check (`agent://NativeSceneScalarFinalCheck`).

T035 independent review of this receipt and candidate (`agent://NativeSceneT035FinalReview`) landed one MAJOR finding: long-text chunking could split a UTF-16 surrogate pair at the 256-code-unit boundary (NSP-T035-003). Fixed in `NativeSceneCaptureCoordinator` (`FindUtf16SafeChunkEnd` moves any surrogate-splitting boundary one code unit earlier, applied to both the bounded prefix and every chunk); the large-response fixture now carries a non-BMP scalar exactly across the boundary and the test asserts exact reconstruction (PASS 1/1, `artifact://1721`); the full eight-class acceptance and cleanup subsets re-ran green on the corrected tree (`artifact://1723`). The companion adversarial verification (`agent://NativeSceneT035FinalVerifier`) verified every substantive claim and flagged only a provenance gap — receipts were not byte-bound to the uncommitted candidate — which the Candidate byte identity block above now closes. Terminal criteria verdict on the frozen candidate: PASS 3/3 (`agent://NativeSceneTerminalCriteria`) — the T035 acceptance text is satisfied by the recorded evidence and the final-checkpoint statement holds. The dedicated `nvmd-judge` pass was attempted once and degraded on provider 429; the exact-criteria terminal check is recorded as the degraded terminal evidence, not fabricated REVIEW_CLEAN. T035 is closed; this receipt authorizes no merge, publication, release, route cutover, Factory/Gallery, `check_element_tokens`, or M2-M5 work.
