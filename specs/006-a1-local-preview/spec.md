# Feature Specification: A1 Local Stateless Preview

**Feature Branch:** `work/a1-local-preview`

**Created:** 2026-08-21

**Status:** Draft — implementation follows this full planning cycle.

**Parent:** `specs/005-stateless-preview/spec.md`

## User Scenarios & Testing

### User Story 1 — Search an explicitly chosen local project (Priority: P1)

An opt-in developer needs to run a local preview process against one project
chosen at launch and discover exactly one read-only symbol-search capability.
The preview must return deterministic, project-relative symbol results without
changing the installed Python command.

**Why this priority:** This is the first working proof that the selected
Stateless direction can serve a useful native route without inheriting its
stateful surfaces.

**Independent Test:** Launch the local process with a valid project root; send
modern discovery, tool-list, and symbol-search requests; confirm one listed tool,
a deterministic matching result, JSON-RPC-only stdout, and bounded exit after
stdin closes.

**Acceptance Scenarios:**

1. **Given** a valid local project containing a C# symbol, **When** a client
   discovers tools and searches that symbol, **Then** it sees exactly one
   read-only tool and receives root-relative deterministic results.
2. **Given** a client sends discovery, listing, or a valid call as its first
   request, **When** it uses the supported modern protocol version, **Then**
   the process responds without prior initialization state.
3. **Given** preview selection is removed, **When** the developer uses the
   existing installed command, **Then** the Python consumer journey remains
   usable and unchanged.

---

### User Story 2 — Receive containment-safe refusals (Priority: P2)

A security-conscious developer needs invalid roots, detectable reparse/final-path
escapes, invalid input, unreadable files, excessive requests, and excluded
operations to stop without returning partial results or reading a target reached
through the rejected authority path.

**Why this priority:** A useful local search tool is unsafe if a malformed
project or request can silently widen its detectable filesystem authority.

**Independent Test:** Exercise each defined launch, configuration, contained
fixture, tool-input, file-access, resource, protocol, and transport denial case;
observe its exact refusal and no leaked result or content from a rejected
reparse/final-target path.

**Acceptance Scenarios:**

1. **Given** a missing, network, device, reparse, or malformed project root,
   **When** the process starts, **Then** it refuses before serving protocol
   traffic or reading project content.
2. **Given** a reparse entry or an entry whose final target resolves outside the
   chosen root, **When** the tool traverses it, **Then** it refuses the request
   without reading that target or preserving an earlier partial match.
3. **Given** a request violates an input or resource limit, **When** the tool
   handles it, **Then** it returns the documented closed error outcome with no
   partial result.

---

### User Story 3 — Preserve existing behavior while the local route evolves (Priority: P3)

A maintainer needs the shared search behavior to have one implementation owner
while the existing compatibility route retains its root-selection and public
behavior, so the new local preview can be removed without migrating users.

**Why this priority:** A parallel migration only remains reversible if the old
route stays a trustworthy rollback and parity reference.

**Independent Test:** Run the existing Python-versus-compatibility search parity
suite before and after the extraction, then remove preview selection and replay
the Python consumer path.

**Acceptance Scenarios:**

1. **Given** the shared search portion is extracted, **When** existing native
   compatibility searches run, **Then** their catalog, ordering, ignore, root
   precedence, and Python-owned timeout behavior remain unchanged.
2. **Given** the preview's strict project selection differs from the legacy
   route, **When** environment, client-root, or working-directory inputs are
   supplied, **Then** they do not change preview authority and do not alter
   legacy precedence.

## Edge Cases

- A client names an unsupported protocol version, calls a legacy initialization
  method, or calls an excluded tool. Missing or malformed metadata is outside
  this child’s frozen observable contract and must not be treated as a supported
  request.
- The root contains a reparse directory, an escaping reparse/final-target source
  entry, a reparse root ignore file, an unreadable file, a changed file identity,
  or more input than the bounded search accepts.
- The client cancels a search or closes stdin while a process is active.
- A normal worktree root is selected; a reparse or final-target escape into a
  sibling worktree is not accepted as a source target. Static hard-link
  provenance is explicitly outside this child’s guarantee.

## Functional Requirements

- **A1L-REQ-001:** The local preview MUST require exactly one explicit local
  project root and MUST reject invalid roots plus lexical, reparse, or final-path
  authority expansion detectable before it reads the affected content or serves
  protocol traffic.
- **A1L-REQ-002:** The preview MUST expose exactly one read-only,
  idempotent symbol-search capability through the supported modern protocol;
  no stateful, bridge, relay, remote, or configuration capability may be
  registered or dispatched.
- **A1L-REQ-003:** Symbol search MUST return deterministic root-relative
  results and closed success/error shapes; invalid input, detected containment
  failure, unreadable paths, and bounded-resource failure MUST not return partial
  data.
- **A1L-REQ-004:** The shared traversal/matching implementation MUST accept
  explicit behavior policy while the compatibility route preserves its existing
  root selection, routing, catalog, and Python-owned search behavior.
- **A1L-REQ-005:** The local process MUST accept discovery, listing, or a valid
  call as its first supported request, keep request metadata request-local,
  emit protocol traffic only on stdout, and exit in a bounded manner after EOF.
- **A1L-REQ-006:** The iteration MUST prove the complete local positive and
  denial matrix plus existing compatibility parity and Python rollback without
  modifying any automation workflow, package publication, tag, release,
  default selector, or consumer distribution.

## Success Criteria

- **A1L-SC-001:** All local positive journeys expose exactly one tool, return
  deterministic root-relative symbol data, and exit cleanly after client EOF.
- **A1L-SC-002:** Every defined denial-matrix journey returns only its documented
  launch or tool refusal, discloses no content from a rejected reparse/final-path
  target, and returns no partial result.
- **A1L-SC-003:** All five existing compatibility/Python parity owners remain
  green after extraction, including the Python-owned timeout and follow-up path.
- **A1L-SC-004:** The preview process tests prove first-request behavior,
  unsupported-version handling, request metadata non-retention, stdout purity,
  and excluded-route absence with nonzero test denominators.
- **A1L-SC-005:** The source change set contains no automation workflow,
  release, publication, tag, package, or default-selector modification, and
  Python rollback remains usable.

## Assumptions

- This is the first local source-run child of the accepted A1 plan. It realizes
  parent T001–T005 plus their direct local test, parity, rollback, and one-checker
  acceptance evidence; it does not execute parent T006–T010.
- The user authorized autonomous implementation after this planning cycle; no
  consumer release or distribution is authorized by this feature.
- The parent A1 contract remains authoritative for exact protocol, path, and
  budget values; this child narrows execution to its local implementation.
- Static hard links are treated as in-root regular paths; this child makes no
  file-provenance or alternate-name containment claim for them.

## Exclusions

- `.github/workflows/**`, `docs/RELEASE-PROTOCOL.md`, build/promotion artifacts,
  tags, releases, PyPI, package/default-selector changes, approval records, and
  all S4 activity.
- DAP, Native Scene, UI/bridge, artifacts, Python relay, mux, HTTP/remote
  transport, downloader/setup, signing/SBOM, and non-Windows distribution.
