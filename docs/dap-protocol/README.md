# Debug Adapter Protocol — Local Playbook

Verbatim mirror of the official Microsoft Debug Adapter Protocol
specification. Pinned here so the project always has a stable, offline
reference independent of `microsoft.github.io` availability or upstream
spec drift mid-release.

## Provenance

| Field | Value |
|-------|-------|
| Upstream site | <https://microsoft.github.io/debug-adapter-protocol/> |
| Upstream repo | <https://github.com/microsoft/debug-adapter-protocol> |
| Snapshot branch | `gh-pages` (the publishing branch — source of truth for the site) |
| Fetched at | 2026-04-19 (Europe/Moscow) |
| Protocol version at fetch | **1.71.x** (see [`changelog.md`](./changelog.md)) |
| License | Creative Commons Attribution / MIT (per upstream `License.txt`) |

The upstream `License.txt` applies to all files here. No content has been
modified; this directory is a verbatim mirror.

## Files

| File | Role |
|------|------|
| [`debugAdapterProtocol.json`](./debugAdapterProtocol.json) | **Machine-readable schema — single source of truth.** 192 definitions (requests, responses, events, types) with full JSON Schema property descriptions. Search this first for the exact shape of any DAP message. |
| [`specification.md`](./specification.md) | Human-readable narrative specification, auto-generated from the JSON schema. Useful when you want prose and section context around a command. |
| [`overview.md`](./overview.md) | Protocol overview: architecture, session startup modes, wire format, initialization sequence, capability negotiation, disconnect/terminate semantics. |
| [`changelog.md`](./changelog.md) | Full version history from 1.0.x → 1.71.x. Check this when deciding whether a feature is safe to depend on given the netcoredbg adapter surface. |
| [`contributing.md`](./contributing.md) | Upstream contribution workflow. Included for completeness; not required by consumer agents. |
| [`img/`](./img/) | Diagrams referenced from `overview.md` (DAP architecture with/without adapters, breakpoint UI, stop/continue state, Java threads, etc.) — 10 assets, verbatim from upstream `gh-pages/img/`. |

## Upstream artifacts to expect

The `specification.md` / `overview.md` / `contributing.md` files are the exact
contents of the upstream Jekyll site source. That has consequences:

- A Jekyll `---` frontmatter block at the top of each `.md` file (e.g.
  `layout: specification`). This is upstream content, not a local edit.
- Internal cross-links use extensionless Jekyll routes like
  `./specification#…` or `./changelog` (without `.md`). These render correctly
  on the upstream site but break when viewed directly on GitHub. Don't "fix"
  them — they will come back on every refresh from `gh-pages`.
- Occasional minor typographic artifacts (e.g., a stray suffix in
  `contributing.md`) that Microsoft has not corrected upstream. Leaving them
  intact keeps this directory a true mirror that diffs cleanly against future
  snapshots.

If you need a lookup that does not rely on a working link, reach for
[`debugAdapterProtocol.json`](./debugAdapterProtocol.json) — it is the source
of truth the prose is generated from.

## How to use

1. **Looking up a request / response / event shape?**
   → Open [`debugAdapterProtocol.json`](./debugAdapterProtocol.json). Search
   under `$defs` / `definitions` by name (e.g. `SetBreakpointsRequest`,
   `StoppedEvent`, `EvaluateResponse`). Property descriptions are inline.

2. **Need prose / examples / flow diagrams?**
   → Open [`specification.md`](./specification.md). It is the JSON schema
   rendered to Markdown with section anchors. Search by command name.

3. **Understanding the session lifecycle?**
   → Open [`overview.md`](./overview.md). Contains the launch-vs-attach
   decision, initialize→initialized→configurationDone sequence, the
   stop-step-continue loop, and disconnect/terminate semantics.

4. **Checking if a feature exists in a specific DAP version?**
   → Open [`changelog.md`](./changelog.md). Entries are dated by minor
   version. netcoredbg implements roughly the **1.55.x–1.60.x** surface;
   features introduced after 1.60.x require a runtime capability probe
   before you can rely on them.

## Refresh procedure

Run from the repo root when you need to pull a newer snapshot:

```powershell
$ghp = "https://raw.githubusercontent.com/microsoft/debug-adapter-protocol/gh-pages"
$dir = "docs/dap-protocol"
New-Item -ItemType Directory -Force "$dir/img" | Out-Null

# Text + schema
foreach ($f in @(
  @{url="$ghp/debugAdapterProtocol.json"; out="$dir/debugAdapterProtocol.json"},
  @{url="$ghp/specification.md";           out="$dir/specification.md"},
  @{url="$ghp/overview.md";                out="$dir/overview.md"},
  @{url="$ghp/changelog.md";               out="$dir/changelog.md"},
  @{url="$ghp/contributing.md";            out="$dir/contributing.md"}
)) { Invoke-WebRequest -Uri $f.url -OutFile $f.out -UseBasicParsing }

# Images referenced from overview.md (enumerate via GitHub API so new
# upstream assets are picked up automatically).
$imgList = Invoke-RestMethod `
  -Uri "https://api.github.com/repos/microsoft/debug-adapter-protocol/contents/img?ref=gh-pages" `
  -Headers @{ 'User-Agent' = 'netcoredbg-mcp' }
foreach ($asset in $imgList) {
  Invoke-WebRequest -Uri "$ghp/img/$($asset.name)" `
    -OutFile "$dir/img/$($asset.name)" -UseBasicParsing
}
```

Then:

1. Update the **Fetched at** and **Protocol version at fetch** rows above.
2. Review the diff in `changelog.md` to understand what shifted.
3. Commit on a dedicated branch with a message of the form
   `docs(dap): refresh mirror to <version>`.

## Why a local mirror?

- **Offline reliability** — the spec is small (~360 KB total) and
  lookups must not depend on network access during debugging sessions.
- **Version pinning** — when a bug is tied to a specific protocol feature,
  the Git blame on this directory tells you which spec revision the
  implementation was written against.
- **Diffable upgrades** — refreshing via the procedure above produces a
  clean diff, surfacing protocol changes we need to react to.

## See also

- [`AGENTS.md`](../../AGENTS.md) — project-wide agent instructions.
- `src/netcoredbg_mcp/dap/` — our DAP client implementation.
