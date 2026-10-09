# Controlled startup deadline classification

## Scope and retained failure

This repair changes the controlled test driver, not consumer timeout or
cancellation classification. Candidate root:
`D:/Dev/netcoredbg-mcp/.agent/worktrees/release-v02312-keypad-prep`, based on
`a1e2cd9116ef19a5f06d47db57742636b2bf23e6`.

The retained run `b1b2ea97-b2a9-4dfe-b797-9be64bc44611` failed only
`StartAsync_IgnoresUnmatchedResponseAndRejectsEarlyInitializedEvent`:
283 passed, one failed, zero skipped. Its TRX recorded a 7.9226321-second case,
with exact expected type `TimeoutException` and actual type
`TaskCanceledException`. It did not retain the caught exception's original
stack or per-phase timestamps. The exact historically slow subphase therefore
remains unknown; collector overhead is not an established cause.

Retained TRX, relative to the coordination root:
`.agent/worktrees/sonar-v02312-diagnostic-b10217c/.tmp/sonarqube-coverage/b1b2ea97-b2a9-4dfe-b797-9be64bc44611/dotnet/inputs/stateless/collector-results/collector.trx`.

## Competing clocks and causal boundary

| Owner | Clock origin | Bound | Expiry behavior |
| --- | --- | --- | --- |
| Original test driver | Before process launch | 5s initialize + 2s request + 500ms = 7.5s | Cancels both the product's supplied token and the driver's outer task wait |
| Product initialize write | Admission of the initialize write | 5s | Linked write cancellation; separate from response/event waits |
| Product initialize response | After the initialize write | 5s | `TimeoutException` unless its supplied caller token cancels |
| Product initialized event | After the correlated response and held continuation are released | A fresh 5s | `TimeoutException` unless its supplied caller token cancels |
| Controlled adapter | Each pre-response/pre-initialized observation gate | 75ms | Records whether an unexpected request arrived; does not cancel the product |

The original driver clock does not cover two sequential initialize waits or
process/write overhead. A valid response arriving more than 2.5s after the
driver starts can leave its fresh five-second event wait competing with the
7.5-second driver cancellation. Accepting cancellation as timeout would hide
that fixture error and erase the caller-cancellation distinction.

The repaired negative test reuses the existing private initialize-response
continuation gate. Before releasing it, the driver observes the correlated
response, the adapter's `before-initialized-event` gate, and an incomplete
product `_initialized` completion. Only then does it disarm its readiness
watchdog, release the continuation, and await the product task itself. The
real event timeout remains five seconds. The caller token remains linked and
can still cancel it. No product API, deadline, coverage provider, or timeout
exception mapping changes.

## Controlled RED/GREEN

The already-installed `FakeTimeProvider` drives only the test driver's
7.5-second watchdog. At the synchronized response boundary the test advances
that clock by exactly 7.5s; no sleeps or retry are needed to expire it.

- RED (`artifact://1861`): one failure, one pass. The non-cancelled-caller case
  reproduced the exact `TimeoutException` versus `TaskCanceledException`
  failure. Output records response ready, driver time `00:00:07.5000000`,
  caller cancellation false, and the exception stack at the driver's
  `AwaitAsyncResult` cancellation wrapper.
- GREEN (`artifact://1863`): three passed, zero skipped. The same two negative
  cases plus the existing initialize-baseline/capability-delta gate test pass.
  The non-cancelled case now throws exact `TimeoutException` from
  `NetCoreDbgSession.StartProtocolAsync`, line 334. Explicit caller
  cancellation throws exact `TaskCanceledException` from that same product
  event wait. Advancing the test clock does not advance the product clock.
- Same pinned collector replay, once: two passed, zero skipped. The unchanged
  retained runsettings and Microsoft.CodeCoverage **17.14.1** were used.
  The collector diagnostic confirms loading that package's
  `Microsoft.VisualStudio.TraceDataCollector.dll`. The timeout case took
  5.6394882s; the caller-cancelled case took 0.7398203s.

Both cases preserve unmatched-response/early-event/correlated-response ordering,
assert that no launch was sent, and assert the recorded adapter process has
exited before the driver returns. Driver disposal releases the continuation,
disposes the session and fixture, removes owned scratch, and restores fixture
environment variables. Test/production DLL and PDB SHA-256 values matched
before and after collection.

Candidate-local raw evidence:

- `.tmp/startup-deadline-classification/red/startup-red.trx`
- `.tmp/startup-deadline-classification/green/startup-green.trx`
- `.tmp/startup-deadline-classification/collector/startup-collector.trx`
- `.tmp/startup-deadline-classification/collector.diag.datacollector.26-10-02_08-11-09_66898_5.log`

## Root-owner replay

Run from the explicit candidate root. This focused command builds only the
Stateless test project and its dependencies, then exercises the changed path:

```powershell
dotnet test host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj --no-restore --filter "FullyQualifiedName~StartAsync_IgnoresUnmatchedResponseAndRejectsEarlyInitializedEvent|FullyQualifiedName~StartAsync_AppliesInitializeBaselineBeforeConfigurationDoneCapabilityDelta" --logger "trx;LogFileName=startup-green.trx" --results-directory .tmp/startup-deadline-classification/root-replay -nr:false
```

The one completed pinned replay used:

```powershell
dotnet vstest host/NetCoreDbg.Mcp.Stateless.Tests/bin/Debug/net8.0/NetCoreDbg.Mcp.Stateless.Tests.dll "/TestAdapterPath:C:/Users/btf/.nuget/packages/microsoft.codecoverage/17.14.1/build/netstandard2.0" "/Settings:D:/Dev/netcoredbg-mcp/.agent/worktrees/sonar-v02312-diagnostic-b10217c/.tmp/sonarqube-coverage/b1b2ea97-b2a9-4dfe-b797-9be64bc44611/dotnet/inputs/stateless/collector.runsettings" "/TestCaseFilter:FullyQualifiedName~StartAsync_IgnoresUnmatchedResponseAndRejectsEarlyInitializedEvent" "/Collect:Code Coverage;Format=cobertura" "/Logger:trx;LogFileName=startup-collector.trx" "/ResultsDirectory:D:/Dev/netcoredbg-mcp/.agent/worktrees/release-v02312-keypad-prep/.tmp/startup-deadline-classification/collector" "/Diag:D:/Dev/netcoredbg-mcp/.agent/worktrees/release-v02312-keypad-prep/.tmp/startup-deadline-classification/collector.diag.log"
```

This is focused fixture evidence, not full-scan or release acceptance. The root
owner owns the next exact-head full replay, commit, and product decision.
