# netcoredbg-mcp v0.23.17

Prepared: 2026-10-27

## Summary

`v0.23.17` is a nonbreaking PATCH for nullable launch environments (#362).
These notes describe the prepared candidate, not a publication receipt.

## Fixed behavior

- A mixed `env` map preserves neighboring string overrides. `null` removes an
  inherited debuggee variable, including case variants on Windows, instead of
  making Samsung netcoredbg discard all supplied overrides.
- Requested removals are applied before the private debug adapter child starts.
  The DAP launch map contains only strings; ordinary string launch overrides
  remain debuggee-only. The server and other sessions' environments do not change.
- An already initialized adapter with no active debuggee uses the existing
  guarded drain/restart path. Failed drain retains its owner and blocks the next
  admission rather than losing cleanup authority.
- With `pre_build=False`, a removal request refuses an active debuggee before
  changing its state or generation. It does not silently stop or replace it.
  The existing pre-build stop/build sequence and pre-build environment are unchanged.

## Consumer actions

Upgrade through the documented package-installation route and restart the MCP
server. An installation does not hot-reload an already running shared receiver.
For a removal request with `pre_build=False`, stop an active debuggee explicitly
before starting the new target. Existing string-only overrides retain their route.

## Compatibility and limits

The Python entry point remains the default. Locked dependencies, bridge-source
packaging, and the public catalog remain unchanged: 135 tools, 8 prompts, and
4 resources. No new framework, dependency, or public tool is added.

This PATCH is limited to #362. Other Unreleased changes, the separate F09/NCD4
incidents, and A2 preview identities/history are not promoted by these notes.
There is no shared-server restart or hot-reload claim.

## Verification boundary

Source regressions and the explicit source smoke do not replace fresh built-wheel
installation and the public mixed-map consumer journey required by the
[release protocol](docs/RELEASE-PROTOCOL.md). Build/install/version preparation is
not installed-consumer acceptance, release completion, or publication.

---

# netcoredbg-mcp v0.23.16

Prepared: 2026-10-25

## Summary

`v0.23.16` is a nonbreaking PATCH for runtime-smoke v1 graceful cleanup (#354).
These notes describe the prepared candidate, not a publication receipt.

## Fixed behavior

- Graceful cleanup uses the existing debug-session owner for launched and
  attached targets instead of unconditionally issuing DAP `terminate`. An
  attached target is detached, not terminated by this cleanup path.
- A UI backend registered by an earlier runtime-smoke plan is disconnected
  before a later stop-only plan drains the debug adapter. The connection is
  retained across plan calls until cleanup; cleanup does not create or reconnect
  a backend when UI was unused.
- A UI disconnect failure remains a reported cleanup failure. Adapter drain
  still runs; the failure is not converted into a successful cleanup result.

## Consumer actions

Upgrade through the documented package-installation route and restart the MCP
server. Existing runtime-smoke v1 plans need no schema change. A later plan with
`cleanup.stop_debug` set to `graceful` now disconnects an already registered UI
backend before stopping the debug adapter.

## Compatibility and limits

The Python entry point remains the default. Locked dependency versions and the
public catalog remain unchanged: 135 tools, 8 prompts, and 4 resources. The normal
short smoke is unchanged. The stable wheel retains its existing bridge-source
packaging and does not add the opt-in modern .NET preview executable.

This PATCH is limited to #354. It does not claim a fix for the separate F09 or
NCD4 incidents, change screenshot or physical-input behavior, or promote other
Unreleased work into this release.

## Verification boundary

Source regression evidence does not replace fresh built-wheel installation and
the public installed-consumer cleanup journey required by the
[release protocol](docs/RELEASE-PROTOCOL.md). Release acceptance and publication
remain separate from metadata preparation and build/install/version proof.

---

# netcoredbg-mcp v0.23.15

Prepared: 2026-10-23

## Summary

`v0.23.15` is a nonbreaking PATCH for Windows FlaUI read/search after a selected
window closes, truthful query-window ownership, and original-process-instance
binding. These notes describe the prepared candidate, not a publication receipt.

## Fixed behavior

- The Python server's UI automation and UI evidence tools share one lazy backend
  and selected-window state. Switching to a live secondary window applies to
  both `ui_find_element` and `ui_query`; concurrent initial connection admission
  does not start independent bridge generations.
- A live selected window remains the unrooted search scope. If it closes and
  exactly one live top-level window remains in the connected process, find/query
  can reach that window in the same bridge generation without reconnecting or
  changing the selected-window pointer.
- With no live windows, or with multiple survivors and a closed selection,
  unrooted searches fail explicitly. The bridge does not choose the first or
  largest window. A unique explicit root remains available; ambiguous roots and
  windows outside the connected process are still rejected.
- Each `ui_query` result's `window` describes the resolved element's actual live
  top-level owner, including container roots and queries without an element
  selector. Unprovable ownership leaves `title` and `automation_id` empty rather
  than substituting the retained selection.
- `connect` opens and retains the original process instance before discovering
  windows. An exited or mismatched instance cannot authorize fresh discovery
  through a reused numeric PID. A legitimate fresh connection replaces the old
  capture; reconnect and shutdown release retained process handles.

## Consumer actions

1. Upgrade through the documented package-installation route and restart the MCP
   server. Use a FlaUI bridge rebuilt from the updated package source; an explicit
   `FLAUI_BRIDGE_PATH` must select that updated bridge, not an older cached binary.
2. When multiple windows remain, use `ui_get_window_tree` and select the intended
   live window with `ui_switch_window`, or provide a unique `root_id`.
3. After the original process exits, start or attach to the intended process and
   establish a fresh connection. Reusing its numeric PID does not revive the old
   bridge connection.

## Compatibility and limits

The Python entry point remains the default. Public catalog and locked dependency
versions are unchanged. The stable wheel carries FlaUI bridge source for its
existing build route; it does not include the opt-in modern .NET preview
executable. Unreleased preview work is not promoted by this PATCH.

This scoped repair does not establish the cause or resolution of the separate
original F09 incident, qualify a previously running MCP receiver, change the
screenshot backend, or claim new physical-input proof.

## Verification boundary

Controlled source-built window-lifecycle checks and the owned-process identity
regression passed. The latter uses explicit numeric-PID fault injection after an
original process exits; it is not an observation of actual OS PID reuse. These
source checks do not replace fresh built-wheel installation and public CLI/MCP
consumer proof under the [release protocol](docs/RELEASE-PROTOCOL.md).

The installed candidate passed the declared own-fixture public CLI/MCP
root/selection journey. Exact-source gate dispositions and residual risks belong
in the release report; these notes are not a publication receipt. Verify the
published package in a fresh consumer process after publication. Earlier native
failures remain retained observations, not proof of the original F09 cause or
compatibility with every application/provider.

---

# netcoredbg-mcp v0.23.14

Prepared: 2026-10-19

## Summary

`v0.23.14` is a nonbreaking PATCH for failed pre-launch restore/build diagnostics,
with bounded maintainer-script source corrections. A failed prebuild remains a
failed launch; its result is retained in the first `start_debug` error response
and through public `get_build_diagnostics`. These notes describe the prepared
candidate, not a publication receipt.

## Failed-prebuild diagnostics

- Only a completed failed restore or build is included in the first `start_debug`
  error response's `data`. Its retained result exposes the failed stage, actual
  executed argument array (`commandLine`), working directory (`workingDirectory`),
  nonzero `exitCode`, and captured `stdout`/`stderr`, even when no MSBuild
  diagnostics were parsed.
- Partial process output from timeout or cancellation is not retained by the
  existing build runner; this PATCH does not change that behavior.
- Cancelled or timed-out attempts omit first-response `data`. Explicit build
  cancellation remains cancelled even when output streams immediately reach EOF.
  Unrelated exceptions do not attach a retained failed build result.
- Validation failures and interrupted attempts do not substitute a result from
  a previous build. A prior success cannot stand in for the current failure.
- Successful-result serialization remains compact. No environment dump is added.

## Maintainer source-distribution changes

- The exact-head Sonar runner accepts paired `--published-tag` and
  `--published-commit` inputs for a real published stable release. It verifies
  the annotated tag, peeled commit, detached source HEAD, and package version
  instead of substituting a later `origin/main`. Scanner metadata, build/test/
  coverage/preview outputs, and child caches use a Git-owner-derived primary
  `.agent` run directory, separate from immutable source. Scanner import hooks
  and SDK PATH registration stay disabled. New per-run receipts preserve old
  receipts; incompatible immutable source refuses before scanning. This is a
  post-publication audit capability, not a release gate or a claimed Sonar pass.
- Preview closeout destination checks use no-follow metadata rather than
  `Path.is_junction`, retaining ancestor, symlink, reparse-point, and escape
  refusals when that API is unavailable. The retained-record resolver is a typed
  local function with unchanged behavior (#341, #342).

These are maintainer-script sources in the source-distribution scope, not new
Python MCP tools or a preview executable bundled in the stable wheel. Fresh
source-archive membership remains part of the release's package verification.

## Compatibility and scope

The Python entry point, default selector, locked dependency versions, and public
catalog remain unchanged: 135 tools, 8 prompts, and 4 resources. Existing
process-owner cleanup and explicit external-path behavior are preserved.

The .NET search-allocation and preview schema-document disposal corrections
(#343, #344) require a separate corrected preview delivery, which remains
pending. The stable Python wheel does not ship that preview executable. B3
native `step_over` work is additive modern-host source-only work, not a new
Python tool. Historical Unreleased preview work is not promoted wholesale into
this stable PATCH.

This PATCH does not change the screenshot backend and does not fix the separate
NCD4 Novascript capture that returned the desktop instead of the target window.
It does not establish a working Windows Sandbox journey.

## Verification boundary

Earlier private installed NCD3 evidence remains tied to its original payload;
it is not transferred to this candidate. Fresh built-wheel installation and
public CLI/MCP journeys must satisfy the
[release protocol](docs/RELEASE-PROTOCOL.md) before tagging. These preparation
notes claim neither fresh artifact proof nor GitHub/PyPI publication.

---

# netcoredbg-mcp v0.23.13

Prepared: 2026-10-10

## Summary

`v0.23.13` is a separate critical PATCH for Python process-cleanup ownership.
The published `v0.23.12` artifact remains unchanged.

## Process ownership and cleanup

- Numeric PIDs, DAP process observations, and saved metadata no longer authorize
  termination. Only trusted producers register process-local cleanup callbacks
  bound to their actual retained owners and launch generations.
- `cleanup_processes(force=True)` and server shutdown invoke real debugger and
  bridge finalizers. Replacing or forgetting an observation cannot transfer
  cleanup rights. Failed or cancelled cleanup retains unfinished owners and is
  not reported as successful registry removal.
- Stopping or cleaning up an attached debug session detaches from its target
  rather than terminating it. Explicit `terminate_debug` remains a separate
  requested action. The bridge does not acquire ownership of the UI app.
- After successful attach acknowledgement and configuration completion, the
  current debug generation publishes the target process ID before entering the
  running state. Lazy UI connection no longer depends on an optional or delayed
  DAP process event; a failed attach acknowledgement does not publish the ID.
- PID persistence and startup orphan sweeping are removed. Old PID files remain
  untouched and inert; they cannot restore cleanup authority after a restart.

Windows cleanup uses retained process and Job handles. POSIX cleanup uses a
private guardian that controls its own still-live process group, including
ordinary descendants that stay in that group. It does not promise cleanup of
daemonized or group-escaped descendants. Owned-root counts must not be read as
whole-tree process counts, and an empty observation registry is not native
process-drain proof.

The force-cleanup response retains `action`, `terminated`, and `processes`.
`terminated` counts only confirmed forced terminations of owned roots caused by
cleanup. Natural or graceful exits are excluded; this is not a count of callbacks,
all stopped processes, or the whole-tree population. `tree_terminated` is `null`; `complete`,
`remaining_owners`, and `errors` expose unfinished cleanup. A failure returns
an error with the same cleanup data rather than a successful empty result.

On POSIX, `terminated` remains `0` because the cleanup result does not establish
root-exit causality. Unconfirmed causality is excluded from this count; zero
does not mean that no processes were stopped. The whole-tree count is unknown.

## Compatibility

The explicit external build, DLL/EXE target, and source-path behavior from
`v0.23.12` is preserved. File and .NET target validity checks, independent
evidence, restore and source-search guards, relative-path resolution, and
runtime-smoke plan containment remain unchanged. The published Python entry
point, locked dependency versions, and tool catalog are unchanged.

## Verification and delivery status

The root release workflow observed the following bounded evidence:

- The prior v2 candidate passed 380 focused source checks. After the attach/UI
  initialization correction, the current changed-layer lifecycle/session checks
  passed 96/96. These are separate observations, not a claim that all 380 checks
  were rerun against the final payload.
- Native Linux ownership checks passed 11/11 against the unchanged POSIX owner
  module. Windows native ownership checks passed 3/3 using exact process handles.
- The current private installed payload completed two real FlaUI
  connect → force-cleanup → reattach/reconnect cycles. The independently owned
  WPF target survived. Captured root observation handles closed, controller Jobs
  drained to zero, no input methods were called, and foreground was preserved.
  The recorded GUI result is `COMPLETE` with `survival_proven=true`.
- The current v3 private installed console replay passed all five public MCP
  cases (5/5, `COMPLETE`, command exit 0), recorded in
  `.agent/reports/process-ownership-v02313-installed-consumer-v3.json`.

The current GUI evidence is recorded in
`.agent/reports/installed-bridge-survival-v02313-runtime-v4.json`. Its package
SHA-256 is `380dd57c98310be76fefcd0fc88762f6f4b391a02c42fe141339e4e190f7d0f5`.
These observations do not claim publication, coverage, a Sonar pass, or proof
for other UI families.

---

# netcoredbg-mcp v0.23.12

Prepared: 2026-10-09

## Summary

`v0.23.12` is a PATCH hotfix for explicit build, debug-target, and source paths
outside the default project or Git worktree.

## Fixed behavior

1. Explicit build-project and output paths need no separate debug permission.
2. Explicit paths to existing DLL/EXE targets and source files need no directory allowlist, including paths outside the default project or worktree.
3. Configuration and troubleshooting guidance no longer asks users to grant directory access before building or debugging an explicit target.

The removed admission check was MCP-wrapper policy, not a native Samsung
`netcoredbg` operating-system sandbox.

## Compatibility

File and .NET target validity checks remain unchanged. Independent evidence,
restore, and source-search guards are not relaxed. The published Python entry
point and locked dependency versions are unchanged.
