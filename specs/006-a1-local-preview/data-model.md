# Data Model: A1 Local Stateless Preview

| Entity | Fields | Invariants |
|---|---|---|
| `PreviewProjectRoot` | canonical absolute drive-local path; containment representation | Created only by exact `--project`; root is existing directory and non-reparse. |
| `SearchPolicy` | allowed extensions; path behavior; I/O behavior; limits; deadline | Immutable. Legacy and preview policies differ explicitly. |
| `VerifiedEntry` | lexical path; final path; kind; byte length | Preview contents are used only after regular/non-reparse/final-under-root validation. |
| `SearchBudget` | directories; entries; file bytes; aggregate bytes; matches; response bytes; deadline | Each limit fails before the next unit and clears accumulated result data. |
| `SymbolMatch` | relative file; line; requested name; matched kind; context | File uses forward slashes; ordering is file/line/kind; context is bounded. |
| `SearchOutcome` | success matches or typed failure | Failure has no root, exception, counter, or partial match. |
| `PreviewRequestMetadata` | protocol version; client info; capabilities | Validated per request and never retained. |

No release, artifact, promotion, approval, tag, or package entity exists in this
child iteration.
