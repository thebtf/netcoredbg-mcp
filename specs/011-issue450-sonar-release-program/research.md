# Research: v0.23.11 Issue #450 and Complete Sonar Remediation Program

**Status**: Planning research only. This file records observed inputs and explicitly marked inferences; it does not claim a remedied scan, accepted child, or published release.  
**Source baseline**: `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`  
**Governor authority**: `agent://Issue450SonarGovernor`

## Method and Evidence Labels

- **OBSERVED** means the fact was read from the cited current repository artifact, retained exact-head receipt, or primary-source research output.
- **INFERRED** means the program conclusion follows from named observed facts and must still be proved by its owning child at an exact head.
- **UNKNOWN** names an unresolved fact. It is not permission to guess a cause, lower a gate, or expand a child scope.

The required external research was sourced from primary GitHub/official documentation and is retained by the assigned research agents: `agent://GitHubDapLifecycle`, `agent://GitHubProcessOwnership`, and `agent://GitHubSonarCoverage`.

## Baseline Facts

| Fact | Classification | Evidence | Program consequence |
|---|---|---|---|
| The program source base is `e95223ba1bddd7a08e440e4a0eca3db9f3c068b9`. | **OBSERVED** | Sonar receipt `captured_head`, `post_scan_head`, scanner metadata, and current-analysis bindings. | All initial scope statements use this identity; later evidence must be freshly exact-head. |
| The exact-head post-merge receipt is valid but blocked. | **OBSERVED** | `.agent/e/sonarqube/thebtf_netcoredbg_mcp/e95223ba1bddd7a08e440e4a0eca3db9f3c068b9/post-merge.json`. | It is starting evidence, not an authorization to tag. |
| Current issue inventory is complete at 1,121 issues with three 500-item pages; 1,076 dispositions are blocking; 45 are fixed. | **OBSERVED** | Same receipt: `post_scan_issues`, `issue_dispositions`. | Wave 4 starts from a known baseline but must regenerate its fresh manifest union. |
| New-code count is 172 and new coverage is `0.0` against `80`; both quality conditions are `ERROR`. | **OBSERVED** | Same receipt: `quality_gate.conditions`. | Wave 3 must produce real coverage; no threshold/new-code policy change is allowed. |
| Hotspot inventory is empty. | **OBSERVED** | Same receipt: `hotspots.total=0`, `hotspot_dispositions.blocking_count=0`. | Later scans still require complete hotspot paging and zero blocking hotspots. |
| The current runner builds under scanner begin/end but has no coverage/test production step in that bracket. | **OBSERVED** | `scripts/run_sonarqube_exact_head.py` `execute()` path around lines 1653–1720. | Wave 3 is the narrow owner of coverage transaction work. |
| The current analysis XML has project identity and scan-all but no Python/.NET Cobertura paths. | **OBSERVED** | `SonarQube.Analysis.xml`. | Wave 3 may add only child-approved import configuration. |
| `pyproject.toml` currently has pytest but no direct Coverage.py dependency/configuration. | **OBSERVED** | `pyproject.toml` project dev dependencies and tool configuration. | Wave 3 must treat coverage setup as an explicit dependency/config choice, not an implicit global tool. |
| Version metadata currently says `0.23.11`. | **OBSERVED** | `pyproject.toml`. | This is version intent only; it is not a public release fact. |

## Issue #450: Accepted Root-Cause Boundary

### Current-source observations

| Observation | Evidence | Consequence |
|---|---|---|
| `DAPClient.start()` opens stdin/stdout/stderr pipes and starts only `_read_loop`. | `src/netcoredbg_mcp/dap/client.py:165-179`. | There is no current independent stderr/process observation in the shown lifecycle path. |
| `_read_loop` treats a falsey stdout header read as EOF and exits. | `src/netcoredbg_mcp/dap/client.py:270-307`. | Raw EOF is a definite terminal signal for the current reader. |
| `_read_loop` finalization fails pending requests and terminates a still-running process, but has no manager callback. | `src/netcoredbg_mcp/dap/client.py:308-324`. | DAP work may stop while the manager retains its last state. |
| `SessionManager` registers only parsed DAP event handlers. | `src/netcoredbg_mcp/session/manager.py:847-869`. | Transport loss has no current manager-owned route. |
| `terminated` changes state; `exited` records exit code without terminal state. | `src/netcoredbg_mcp/session/manager.py:1043-1059`. | Exited-vs-terminated must remain semantically distinct. |
| `SessionState.to_dict()` derives `debuggeeAlive` from process ID and nonterminal state. | `src/netcoredbg_mcp/session/state.py:534-554`. | Retained `RUNNING` plus PID produces the observed stale live claim. |

### Investigation conclusion

**OBSERVED**: `.agent/runs/issue450-adapter-eof-lifecycle/investigation.md` records the accepted causal chain: `DAPClient._read_loop` treats stdout EOF as terminal but does not publish transport death to `SessionManager`; the manager retains `RUNNING`; state serialization turns retained PID plus `RUNNING` into `debuggeeAlive=true`; later requests correctly encounter a dead client.

**UNKNOWN**: The historical producer reason for stdout closure was not retained. It may have been normal exit, crash, foreign termination, or pipe closure. The program does not label any of these as the cause.

**INFERRED**: The smallest correct Wave-1 shape is an idempotent client-to-manager terminal bridge with bounded diagnostics, not a speculative netcoredbg producer patch and not a state-model rewrite.

### Primary-source lifecycle research

| Source | Observed reusable property | Program disposition |
|---|---|---|
| [Debug Adapter Protocol specification](https://github.com/microsoft/debug-adapter-protocol/blob/bf8a5d27e8040044b84b863f90916e08925ee811/specification.md#L339-L364) | `exited` means debuggee exited and carries an exit code; `terminated` means debugging terminated and does not prove debuggee exit. | **KEEP** semantic separation in Wave 1. |
| [MIEngine `PipeTransport`](https://github.com/microsoft/MIEngine/blob/8ffc66d42067463d6582c8b1e3911a89b2069297/src/MICore/Transports/PipeTransport.cs#L225-L344) | Independent stderr drain, reader fan-in, and bounded stream/process join. | **ADAPT** bounded join; do not copy its callback contract as a one-shot terminal publisher. |
| [debugpy launcher process waiter](https://github.com/microsoft/debugpy/blob/e5743d3a00c6dee7d8140275c7df7e719ebb132f/src/debugpy/launcher/debuggee.py#L177-L227) and [output drain](https://github.com/microsoft/debugpy/blob/e5743d3a00c6dee7d8140275c7df7e719ebb132f/src/debugpy/launcher/output.py#L62-L112) | Wait for exit, drain output, retain exit code, and avoid a second kill of an exited process. | **ADAPT** ordering and already-exited guard with a bounded asyncio policy. |
| [VS Code `RawDebugSession`](https://github.com/microsoft/vscode/blob/004a1fbb1658e61048b29d76e2ce380adfa18680/src/vs/workbench/contrib/debug/browser/rawDebugSession.ts#L592-L645) | Multiple terminal signals converge through a guarded shutdown and one owner-visible exit event. | **ADOPT** one guarded finalizer invariant. |
| [vscode-go legacy Delve adapter](https://github.com/golang/vscode-go/blob/46048018519b6f727e920f5f5a4335acc436bdd3/extension/src/debugAdapter/goDebug.ts#L730-L771) | Socket and process close can fan into one unguarded callback. | **REJECT** as a full pattern; it illustrates why Wave 1 needs a central idempotency gate. |

The research agent recorded the proposed terminal record fields as a synthesis, not copied upstream API: first signal, process-exited fact, return code when known, protocol-terminated fact, last DAP event/preview, bounded stderr tail/truncation, stdout EOF/read error, and explicit-stop fact. Exact field names and bounds belong to child 012’s D1 contract.

## Owner-Scoped Cleanup Research

### Current-source observations

| Observation | Evidence | Consequence |
|---|---|---|
| `kill_debugger_processes(..., kill_all_netcoredbg=True)` calls `taskkill /F /IM netcoredbg.exe` on Windows. | `src/netcoredbg_mcp/build/cleanup.py:161-208`. | It selects same-image processes globally and is not owner-safe. |
| `cleanup_for_build()` defaults `kill_all_netcoredbg=True`. | `src/netcoredbg_mcp/build/cleanup.py:300-334`. | The unsafe selection is on the default route. |
| `BuildSession` creates an async subprocess before `_assign_to_job`. | `src/netcoredbg_mcp/build/session.py:213-250`. | A child can run before Job admission is established. |
| `_assign_to_job` opens by PID, calls assignment, and ignores the result. | `src/netcoredbg_mcp/build/session.py:174-197`. | Current code has neither pre-resume admission nor a trustworthy retained owner capability. |
| Investigation has no correlated global kill within 30 seconds of either recorded Issue #450 EOF. | `.agent/runs/issue450-adapter-eof-lifecycle/investigation.md`. | The risk is confirmed independently but must not be called the incident cause. |

### Primary-source ownership research

| Source | Observed property | Program disposition |
|---|---|---|
| [Windows Job Objects](https://learn.microsoft.com/en-us/windows/win32/procthread/job-objects) | A Job Object manages a group of processes; normal `CreateProcess` children join by default; `TerminateJobObject` targets current members. | **ADOPT** as the ownership direction, subject to child proof. |
| [CreateProcess / process handles and identifiers](https://learn.microsoft.com/en-us/windows/win32/procthread/process-handles-and-identifiers) | Retained handles remain valid after termination; numeric PIDs can be reused after process lifetime. | **ADOPT** retained handle as authority; **REJECT** PID as primary identity. |
| [Job accounting information](https://learn.microsoft.com/en-us/windows/win32/api/winnt/ns-winnt-jobobject_basic_accounting_information) | `ActiveProcesses` reports current processes associated with a Job. | **ADAPT** as the tree-drain observation, not an unbounded disappearance assertion. |
| [taskkill](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/taskkill) | `/im` selects by image name and `/t` follows children of selected process. | **REJECT** image-name selection as ownership. |
| [CPython `subprocess.py`](https://github.com/python/cpython/blob/3.10/Lib/subprocess.py) | CPython retains process handle but closes primary thread handle after create. | **REJECT** post-`asyncio` suspended-launch as an atomic admission implementation. |
| [psutil](https://github.com/giampaolo/psutil/blob/release-7.2.2/psutil/__init__.py) | PID plus creation-time identity is documented as inherently racy; recursive children can disappear if an intermediate parent exits. | **ADOPT** only for observation if needed; **REJECT** for tree-ownership authority. |

**INFERRED**: Wave 2 needs one small private Windows owner boundary that admits the process before it runs, retains the relevant handles/job, verifies membership, and uses graceful then bounded forced cleanup only for that retained capability. The parent deliberately does not decide its exact API, timeout values, or binding mechanism.

## Cross-Language Coverage Research

| Source | Observed property | Program disposition |
|---|---|---|
| [Sonar .NET coverage documentation](https://docs.sonarsource.com/sonarqube-server/2026.3/analyzing-source-code/test-coverage/dotnet-test-coverage.md) | `dotnet-coverage collect "dotnet test" -f cobertura -o coverage.xml` with `sonar.cs.cobertura.reportsPaths=coverage.xml` is documented. | **ADOPT** one deterministic .NET Cobertura report path in Wave 3. |
| [Sonar coverage parameters](https://docs.sonarsource.com/sonarqube-server/2026.3/analyzing-source-code/test-coverage/test-coverage-parameters.md) | C# and Python properties accept Cobertura report paths; Python has `sonar.python.coverage.reportPaths`. | **ADOPT** fixed root-relative configured paths rather than ambiguous globs. |
| [Sonar Python coverage guidance](https://docs.sonarsource.com/sonarqube-server/2026.3/analyzing-source-code/test-coverage/python-test-coverage.md) | `coverage run -m pytest`, `coverage xml`, and `relative_files=True` support parsed Python coverage. | **ADOPT** direct Coverage.py approach subject to child exact-head proof. |
| [Microsoft dotnet-coverage](https://learn.microsoft.com/en-us/dotnet/core/additional-tools/dotnet-coverage) | `collect` records coverage for a process and subprocesses and supports output format/path selection. | **ADOPT** for current tree rather than a Visual Studio-only collector. |
| [Microsoft local tool manifest guidance](https://learn.microsoft.com/en-us/dotnet/core/tools/global-tools) | A checked-in local tool manifest permits deterministic `dotnet tool restore`. | **ADAPT** if child 014 selects it; do not rely on a mutable user-global tool. |
| [Coverage.py XML reference](https://coverage.readthedocs.io/en/latest/commands/cmd_xml.html) | `coverage xml` emits Cobertura-compatible XML and supports named data/output paths. | **ADOPT** to keep data/XML under the generated report root. |
| Coverlet VSTest documentation | Collector output uses random GUID paths and current requirements exceed project `Microsoft.NET.Test.Sdk` baseline. | **REJECT** as the primary simple current-tree route. |

**OBSERVED**: `agent://GitHubSonarCoverage` recommends keeping `scripts/run_sonarqube_exact_head.py` as sole authority and generating two deterministic report files after begin/before end. It also warns that the runner currently recognizes only `.sonarqube`, `.scannerwork`, `bin`, and `obj` generated paths, so Wave 3 must deliberately own `coverage-reports` cleanup/configuration rather than leave hidden residue.

**INFERRED**: A successful report file is insufficient. Wave 3 must prove same-run provenance, source mapping, nonzero denominators, imported analysis state, exact source identity, and unchanged threshold evaluation.

## Complete-Finding Remediation Research

| Fact | Classification | Consequence |
|---|---|---|
| Exact-head receipt pagination is a full-project operation; it records current and new-code inventories plus hotspot inventory, with current-analysis bookends. | **OBSERVED** | Wave 4 must consume fresh complete inventories, never a first page or stale partition. |
| The release protocol classifies non-`FIXED` current issue dispositions as blocking and every hotspot as blocking. | **OBSERVED** | Accepted, false-positive, WONTFIX, ignored, or excluded paths cannot close the program. |
| A scan can discover new keys after source fixes. | **INFERRED** from exact-head inventory behavior | Wave 4’s manifest is refreshed, not frozen against initial counts; new keys go to existing 015/016/017 owners. |
| The exact source path for every future finding is not known from the parent’s baseline alone. | **OBSERVED** | The fresh manifest is the routing authority; guessing an exhaustive file list would violate PRG-009. |

## Release and Route-Preservation Research

| Fact | Classification | Evidence | Consequence |
|---|---|---|---|
| The public console entry point is `netcoredbg_mcp.__main__:run`. | **OBSERVED** | `pyproject.toml`. | Wave 5’s installed-consumer proof must use the public route; no child changes selection. |
| The exact-head protocol requires candidate and post-merge scans, and only the post-merge receipt can authorize a tag. | **OBSERVED** | `docs/RELEASE-PROTOCOL.md:106-173`; `docs/SONARQUBE-ONBOARDING.md:78-160`. | Wave 5 does not treat a candidate scan as tag authority. |
| Scanner worktrees must be clean, detached, and linked; credentials are primary-root local-only and receipt-safe. | **OBSERVED** | `docs/SONARQUBE-ONBOARDING.md`. | No child writes secrets or a `.env` to its source worktree/packet. |
| A changed source byte invalidates evidence tied to an earlier candidate/integration head. | **INFERRED** from exact-head contract | Wave 5 returns source corrections to the owning wave instead of recycling old evidence. |

## Research Decisions

| Decision | Rationale | Owning child |
|---|---|---|
| Keep five waves under one public release iteration. | Governor decision reconciles strict full-denominator remediation with one v0.23.11 shipping moment. | Parent / 019 |
| Repair Wave 1 before owner cleanup. | The root cause is user-visible, source-proven, and can be fixed without resolving the unrelated global-kill risk. | 012 |
| Keep owner cleanup separate. | The source risk is confirmed but the incident causal link is not. | 013 |
| Keep the exact-head runner and configure coverage inside its transaction. | Existing runner already owns source/analysis identity, serialization, safe credential handling, and receipt publication. | 014 |
| Use one fresh owner manifest for all Wave-4 keys. | Prevents stale denominator, duplicate work, and hidden unowned findings. | 015–018 |
| Release only after exact Wave-4 integration closure. | A final release cannot inherit clean evidence from a different source head or partial partition. | 019 |

## Explicit Non-Decisions / Fog of War

- **UNKNOWN producer cause**: A later observed adapter producer failure opens a separate debug child. It does not expand 012 because a hypothetical cause sounds plausible.
- **Wave-1 bounds**: Exact terminal-record byte caps, timeout values, and external diagnostic presentation remain child-012 decisions after source/primary research review.
- **Wave-2 private API**: Exact `ctypes` type layout/API shape remains child-013 design; parent only fixes the ownership invariant and prohibited fallbacks.
- **Wave-4 source fixes**: Rule-by-rule source edits, test strategy, and exact file ownership remain manifest-driven child decisions. The program does not preload an obsolete numeric partition.
- **Wave-5 mechanics**: Version/changelog/release branch details must be reread at final exact head by spec 019; this parent does not pre-authorize publication.

## Research Debt Handoff

| Child | Required pre-design research debt |
|---|---|
| **012** | Re-read current `DAPClient`, manager/state resource publication, existing tests, and the cited MIEngine/debugpy/VS Code/DAP sources; decide bounded record fields and race ordering. |
| **013** | Re-read current cleanup/build callers and the cited Windows Job/handle/CPython primary sources; decide the smallest owner capability and test its failure paths. |
| **014** | Re-read current runner/analysis XML/tool and package configuration; verify current Sonar/server semantics and the report/import path against the exact scanner environment. |
| **015** | Read only fresh-manifest-owned Python/test/tool files and their rule documentation before repair design. |
| **016** | Read only fresh-manifest-owned bridge files and their rule documentation before repair design. |
| **017** | Read only fresh-manifest-owned host files and their rule documentation before repair design. |
| **018** | Re-read all Wave-4 child closures and fresh receipt schema; no broad source repair is owned here. |
| **019** | Re-read accepted Wave-4 closure, current release protocol, version/changelog state, and public consumer journey instructions at the final candidate head. |
