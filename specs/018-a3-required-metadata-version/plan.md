# A3 Required Metadata / Version Plan

Status: accepted D1; no code or runtime proof yet.
Baseline: `cbc306de36c162930bd83b2bd0424ca06e82eba9`.

## Existing owners and affected consumers

| Owner | Bounded change |
|---|---|
| Preview and preview-test `.csproj` | Pin MCP 2.3.0 only here; preserve other host pins and runtime 8.0.32. Verify actual restored package identity. |
| `host/NetCoreDbg.Mcp.Stateless.Preview/Program.cs` | Let the SDK own required metadata/version; retain present-clientInfo validation and unrelated method/tool/root checks. Remove dead duplicated incoming branches. |
| `BoundedResponseFrameSerializer.cs` and existing outgoing filter | Preserve exact `-32602`/ID when shortening an oversized SDK error; remeasure the full serialized frame, close without output if even minimal correlation cannot fit. Preserve exact `-32022` or closure. Reuse existing serializer/limits. |
| `PreviewProcessContractTests.cs`, existing driver/frame tests | First prove missing-metadata RED on unchanged runtime/pins. Then verify recovery, precedence, both version sequences and error-frame boundaries, retaining unrelated refusal controls. No new test host. |
| `scripts/stateless_preview_artifact.py`, `build/prepare_preview_fixture.py` | Add one fixed 17-case contract and exact current-producer pin; preserve historical A1/A2/optional-A3 meanings/default historical API. |
| Existing manifest/candidate/consumer-proof schemas | Admit only the exact known new ID/hash/tool/ordered-matrix binding. Schema versions and publisher remain unchanged. |
| `tests/preview/validate_preview_artifact.py`, existing artifact Python/C# tests | Add the real composed recovery scenario; keep all prior scenarios and rollback. Test downgrade/mixed/missing-proof refusal, not test wiring. |
| Existing README, workflow contract and changelog | Document only observed new behavior and limitations; do not rewrite predecessor delivery evidence. |

## Execution and ownership

Root owns the contract, baseline, RED/GREEN admission, integration, commits and release. Artifact work uses the isolated `a3ma` source tree. C# work uses the sole-writer primary feature branch after actual native semantic readiness there; the unused clean C# worktree was retired when its native language-server coverage could not be established. The attempted typed LSP configuration correction was restored; no profile patch or readiness waiver remains. Each worker writes only its leased surfaces, uses primary `.agent` outputs, and does not mutate linked-worktree `.agent`, continuity, profiles or publication.

1. Test/reproducer-only stages return their exact focused check and intended failure. No production/pin edits or mid-flight build/lint/test/formatter runs.
2. Root reviews both scoped diffs and unchanged production/pins. A bounded finite verification child runs each focused RED with exact source/cwd/output/exit; fix a wrong-reason harness failure before product edits.
3. Before runtime logic edits, verify restored 2.3.0 identity and the pin-only process discriminator. Then implement the smallest filter/frame correction and observe focused GREEN and actual recovery.
4. Combine runtime and artifact changes without losing their distinct source/evidence identities; commit each focused-proven contribution. Run affected owning checks and one actual new scenario against the extracted combined executable; then the existing canonical functional producers and full retained/public release path.

For preview process checks using `--artifacts-path`, the existing driver requires **both** `NETCOREDBG_MCP_TEST_ARTIFACTS_PATH=<absolute primary-owned output>` and `NETCOREDBG_MCP_TEST_SOURCE_ROOT=<actual compiled checkout root>`. Artifact consumers additionally require `NETCOREDBG_PREVIEW_ARTIFACT_ROOT=<verified successor fixture>`. A directory argument alone does not select the test launcher output. Keep original wrong-routing failures and rerun the same compiled check with the supported environment; do not patch the resolver or weaken its source/root guards.

## Proof portfolio

- REQUIRED, BACKGROUND: existing process regression RED/GREEN/recovery; runtime UNKNOWN until observed. Use a finite deadline and retain TRX/log/source identity. At an overrun, preserve outputs and inspect the failure signature; do not turn missing output into PASS or repeat unchanged indefinitely.
- REQUIRED, BACKGROUND: focused artifact generation/downgrade/historical-compatibility checks and actual 17th runner; runtime UNKNOWN. No quiet-dot pass credit or service-mode timeout substitution. Native finite commands use an empty service name.
- REQUIRED, BACKGROUND: canonical owner build/functional producers, retained 17-case consumer, candidate-bound channel evidence, exact S4, same-byte publication and fresh public 17-case/Python rollback. Reuse unchanged applicable evidence; source success is not delivery.
- RISK_SELECTED: one independent implementation-specific trace of SDK exception → outgoing filter → bounded correlated wire response; its inputs are the actual restored package and combined source. Existing channel review is separate release proof, not a new design panel.

The primary risks are earlier SDK validation bypassing local incoming preflight, accidental replacement of exact error codes/IDs, and old-generation proof substitution. Direct process/frame and generation checks distinguish each. Broad schema/legacy/default/runtime-servicing work remains outside this slice. Rollback removes opt-in preview selection and uses unchanged installed Python; no data migration occurs.
