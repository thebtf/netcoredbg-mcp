# Native Scene Probe M0-G0 Approval Record

- Approval recorded at: `2026-08-18T22:44:15Z`
- Approved merged base: `9dd6f4318d310b27214212cb19872ed4df56b9ae`
- Operator decision: **APPROVED** — the operator explicitly instructed this session to begin implementation (`приступай к реализации`).
- Authorized scope: M0-G0 tasks T002–T007 only.
- Not authorized by this record: M0 primitive implementation T008+, M1, route cutover, package publication, release, deployment, Factory/Gallery work, DTCG resolution/comparison, `check_element_tokens`, or M2–M5.

## Approved exact bytes

| Authoritative repository path | SHA-256 |
|---|---|
| `specs/004-native-scene-probe/contracts/native-scene-probe.schema.json` | `f446166f9a1062d3e1a2190327d06c04905e76a1c1f81af16c87572394f90022` |
| `specs/004-native-scene-probe/contracts/native-scene-artifact.schema.json` | `07c257c9b5f75c01aa4f4141968c789b045d7c831575343df429075c732f7668` |
| `specs/004-native-scene-probe/contracts/parity-corpus.json` | `90c24f8f9706c207ca3ecf8dee93d1937c16a6be45feac65d812e48853bc4621` |

These three paths and byte hashes are the sole authoritative M0-G0 contract inputs. Copied, renamed, embedded-from-different-bytes, or duplicate schemas and corpora are not authoritative.

## 2026-08-19 C015 correction

The operator explicitly reapproved the exact three-file contract set after C015 was found to make the schema's optional WebP preview mandatory at runtime. The two schema hashes are unchanged. The corrected corpus requires the lossless PNG artifact and applies independent, `preview_only`, non-authoritative invariants only when a WebP preview is present. The superseded corpus hash is `9308da9c3807b4967b175525c9df4183593b4537a26be13cbe36bc8edd1faadf`.
