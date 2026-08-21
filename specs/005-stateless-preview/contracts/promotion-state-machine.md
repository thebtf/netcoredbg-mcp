# Preview Promotion State Machine

This contract governs promotion of one S4-approved build artifact. Inputs are
`run_id`, commit, tag, archive hash, manifest hash, executable hash, and the
GitHub prerelease destination. No state admits a rebuild or changed input.

| State observed before action | Admission rule | Allowed action | Terminal/result |
|---|---|---|---|
| `unstarted` | Preview tag and release are both absent. | Verify retained source-run artifact and all approved equations; create annotated tag at approved commit; create draft release. | Advance to `draft_empty`. |
| `draft_empty` | Annotated tag resolves to approved commit; matching draft release exists with no assets. | Upload the two approved assets only. | Advance to `draft_complete`. |
| `draft_partial` | Tag/commit/draft metadata match; every existing asset has expected name, size, and SHA-256. | Upload only missing approved asset(s). | Advance to `draft_complete`. |
| `draft_complete` | Tag/commit/draft metadata and both exact assets match. | Re-verify remote bytes and publish prerelease. | Advance to `published_complete`. |
| `published_complete` | Tag/commit/prerelease metadata and both exact assets match. | Do not mutate; run post-publication consumer proof and emit receipt. | Success. |
| collision | Existing tag target differs; release targets a different tag; existing release is non-draft but incomplete; metadata differs; any expected asset differs; extra collision asset uses an expected name; source artifact expired. | Hard refuse; record evidence. | Blocked; corrected source requires a new version/tag. |

`tag` means an annotated tag whose peeled target is the approved commit. A draft
or complete release must use that exact tag, be marked prerelease, and preserve
the approved destination metadata. Remote byte verification downloads each
asset and compares SHA-256/size with the approved manifest. Any action may be
retried after a transport/API failure by reclassifying state; tags and matching
assets are never deleted, moved, or overwritten.
