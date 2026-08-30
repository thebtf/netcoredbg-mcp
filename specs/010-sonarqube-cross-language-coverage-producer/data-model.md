# Coverage evidence data model

## Canonical run marker

The runner derives the report path set before scanner begin. It claims the report root only after scanner begin succeeds because SonarScanner for .NET clears `.sonarqube` during begin.

```text
.tmp/sonarqube-coverage/<run_id>/
├── coverage-run.json
├── python/coverage.xml
└── dotnet/<project-slug>/coverage.opencover.xml
```

`project-slug` is the first 16 lowercase hexadecimal characters of `SHA-256(normalized-relative-csproj-path UTF-8)`. The runner rejects a slug collision. The marker records every .NET `project`, `slug`, and `path`, so a reviewer can map each report to the closed inventory.

| Field | Type | Rules |
| --- | --- | --- |
| `schema` | string | Exactly `netcoredbg-mcp.sonar-coverage-run/1`. |
| `run_id` | string | Lowercase UUID that equals receipt `run_id`. |
| `captured_head` | string | Lowercase 40-character Git SHA. |
| `sets` | array | Exactly `dotnet` then `python`. Each set contains `language`, `format`, and normalized sorted report entries. .NET entries also contain `project` and `slug`. |

The runner serializes the marker with `json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))`, encodes UTF-8, and appends exactly one LF byte. It hashes those exact bytes with SHA-256. Validation parses the marker, rebuilds the same canonical bytes, requires byte-for-byte equality, then compares the digest. A reordered, re-encoded, stale, or altered marker blocks the run.

## Coverage evidence set

A successful receipt contains `coverage.evidence_sets` in exactly this order: `dotnet`, then `python`.

| Field | Type | Rules |
| --- | --- | --- |
| `language` | string | Exactly `python` or `dotnet`. Each language appears once. |
| `format` | string | `cobertura` for Python. `opencover` for .NET. |
| `run_id` | string | Equals the receipt run identifier. |
| `marker_sha256` | string | Lowercase SHA-256 of the canonical run marker bytes. |
| `reports` | array | Nonempty. The report list is sorted by normalized relative path. |

## Report binding

Each `reports` entry identifies one regular XML file.

| Field | Type | Rules |
| --- | --- | --- |
| `path` | string | Normalized slash-separated path below `.tmp/sonarqube-coverage/<run_id>`. It cannot be absolute, duplicated, or contain `.` or `..` components. |
| `project` | string or null | A normalized closed-inventory `.csproj` path for .NET. `null` for Python. |
| `sha256` | string | Lowercase SHA-256 for the final report bytes. |
| `bytes` | integer | Greater than zero. |
| `xml_root` | string | `coverage` for Python or `CoverageSession` for .NET. |
| `coverage_denominator` | integer | Greater than zero. Python equals root `coverage/@lines-valid`. .NET equals direct root `CoverageSession/Summary/@numSequencePoints`. |
| `mapped_source_count` | integer | Greater than zero. |
| `source_path_set_sha256` | string | SHA-256 of the accepted normalized paths sorted lexicographically, joined with LF, encoded UTF-8, and terminated with one LF byte. The path set is nonempty. |
| `captured_head` | string | Equals the receipt `captured_head` and the marker captured head. |

Python mappings must be normalized relative `.py` paths beginning `src/netcoredbg_mcp/`. .NET mappings must resolve to regular `.cs` files under the scanner worktree and include at least one non-test, non-fixture source path. The runner sorts the accepted normalized path set, serializes it as the UTF-8 LF-terminated sequence defined above, and hashes the exact bytes before cleanup.

## Analysis coverage binding

The runner queries `/api/measures/component` with this exact query after scanner end:

```json
{"component":"thebtf_netcoredbg_mcp","metricKeys":"coverage,lines_to_cover,new_coverage,uncovered_lines"}
```

The receipt stores `analysis_coverage`.

| Field | Type | Rules |
| --- | --- | --- |
| `analysis_id` | string | Equals the submitted Compute Engine analysis identifier. |
| `before` | object | A current-analysis binding whose analysis identifier and revision equal `analysis_id` and `captured_head`. |
| `query` | object | Exactly the fixed component and metric keys above. |
| `metrics` | object | `coverage`, `new_coverage`, `lines_to_cover`, and `uncovered_lines` are canonical decimal strings from the response. |
| `after` | object | A second current-analysis binding equal to `before` for analysis identifier and revision. |

`coverage` and `lines_to_cover` must parse to values greater than zero in a successful coverage-import proof. `new_coverage` and `uncovered_lines` must parse as nonnegative values. The two current-analysis bindings prevent a latest-analysis read from being attributed to a different submitted analysis.

## Blocked coverage outcome

A coverage-related blocked receipt stores `coverage` with this minimum shape:

| Field | Type | Rules |
| --- | --- | --- |
| `status` | string | Exactly `BLOCKED`. |
| `stage` | string | One of `scanner_begin`, `python_producer`, `dotnet_producer`, `report_validation`, `scanner_end`, `analysis_metrics`, or `cleanup`. |
| `language` | string or null | `python`, `dotnet`, or `null` when no language owns the failure. |
| `failure_code` | string | A safe `COVERAGE_*` code. It does not contain a credential, raw report body, or unvalidated absolute path. |
| `cleanup` | object | `status`, normalized removed paths, and optional typed `failure` with path, operation, and error type. |

The root receipt `failure` retains the first causal failure. A later cleanup failure appears only under `coverage.cleanup.failure`.

## State transitions

```text
PRE_CLEANED
  -> CLEANUP_ARMED
  -> IMPORT_PATHS_PREPARED
  -> SCANNER_BEGUN
  -> RUN_DIRECTORY_CLAIMED
  -> MARKER_WRITTEN
  -> PRODUCED
  -> VALIDATED
  -> SCANNER_ENDED
  -> ANALYSIS_COVERAGE_BOUND
  -> RECEIPT_BOUND
  -> CLEANED

Any scanner, producer, validation, timeout, cancellation, API, or cleanup failure -> BLOCKED
```

The runner may call scanner end only after every required evidence set reaches `VALIDATED`. It arms cleanup before scanner begin and cleans generated artifacts after every outcome. A successful receipt may record only `RECEIPT_BOUND` entries.

## Retention

The scanner worktree owns the marker and reports. It deletes them through generated-artifact cleanup after every run. The receipt retains only the fields above. It never retains report contents, credentials, an origin, or a test process command line.
