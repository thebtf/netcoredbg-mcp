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
