"""Deterministic one-use watchdog policy checks; no real processes or sleeps."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.windows_process_owner import (
    AdmissionStage,
    DrainStatus,
    OwnedProcessRef,
    _Win32CallError,
)
from scripts import stateless_preview_permission_watchdog as watchdog


def receipt(status=DrainStatus.DRAINED, *, active=0, forced=False, code=0):
    return SimpleNamespace(
        status=status, active_processes=active, forced=forced, root_returncode=code
    )


@pytest.fixture
def owned_run(monkeypatch, tmp_path):
    events = []
    now = [1000.0]
    observation = receipt()
    closure = receipt()

    async def read(_size):
        return b""

    stream = SimpleNamespace(read=read)
    owner = SimpleNamespace(stdout=stream, stderr=stream)

    async def launch(**kwargs):
        events.append(("launch", kwargs))
        now[0] += 2.0
        return owner

    async def observe(**kwargs):
        events.append(("observe", kwargs))
        return observation

    async def close():
        events.append(("close",))
        return closure

    def finalize(directory, code):
        events.append(("finalize", directory, code))
        return 1

    monkeypatch.setattr(watchdog._DiagnosticProcess, "launch", launch)
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: now[0])
    monkeypatch.setattr(watchdog, "_finalize_timeout", finalize)
    monkeypatch.setattr(watchdog, "_allow_artifacts", lambda: events.append(("allow",)))
    owner.drain_after_grace = observe
    owner.aclose = close
    return SimpleNamespace(
        owner=owner,
        events=events,
        now=now,
        observation=observation,
        closure=closure,
        directory=tmp_path,
        run=lambda: asyncio.run(watchdog._run(tmp_path)),
    )


def test_root_exit_does_not_replace_job_drain_and_preserves_driver_exit(owned_run):
    owned_run.observation.root_returncode = 1
    owned_run.closure.root_returncode = 1

    assert owned_run.run() == 1
    assert [event[0] for event in owned_run.events] == ["launch", "observe", "close", "allow"]
    assert owned_run.events[1] == ("observe", {"grace_timeout": 118.0, "force_timeout": 0.0})
    launch = owned_run.events[0][1]
    assert launch["argv"] == [
        watchdog.sys.executable,
        str(owned_run.directory / "scripts/stateless_preview_permission_timing_diagnostic.py"),
    ]
    assert (launch["cwd"], launch["env"], launch["stdin_mode"]) == (
        str(owned_run.directory),
        None,
        "devnull",
    )


def test_deadline_finalizes_only_after_close_proves_orphan_descendant_drain(owned_run):
    owned_run.observation.status = DrainStatus.TIMED_OUT
    owned_run.observation.active_processes = 1
    owned_run.observation.forced = True
    # The retained root may already have exited successfully while its child lives.
    owned_run.observation.root_returncode = 0

    assert owned_run.run() == 1
    assert owned_run.events[-3:] == [("close",), ("finalize", owned_run.directory, 0), ("allow",)]


@pytest.mark.parametrize(
    ("status", "active", "code"),
    [
        (DrainStatus.TIMED_OUT, 1, 1),
        (DrainStatus.FAILED, None, 1),
        (DrainStatus.DRAINED, None, 1),
        (DrainStatus.DRAINED, 1, 1),
        (DrainStatus.DRAINED, 0, None),
    ],
)
def test_unverified_cleanup_never_finalizes_or_authorizes_artifact_reads(
    owned_run, status, active, code
):
    owned_run.closure.status = status
    owned_run.closure.active_processes = active
    owned_run.closure.root_returncode = code

    assert owned_run.run() == 1
    assert [event[0] for event in owned_run.events] == ["launch", "observe", "close"]


def test_failed_observation_accounting_remains_failure_even_if_close_drains(owned_run):
    owned_run.observation.status = DrainStatus.FAILED
    owned_run.observation.active_processes = None

    assert owned_run.run() == 1
    assert [event[0] for event in owned_run.events] == ["launch", "observe", "close"]


def test_observation_failure_still_closes_owner_without_reading_artifacts(owned_run):
    async def fail(**kwargs):
        raise OSError("synthetic accounting failure")

    owned_run.owner.drain_after_grace = fail
    with pytest.raises(OSError):
        owned_run.run()
    assert [event[0] for event in owned_run.events] == ["launch", "close"]


def test_close_failure_never_finalizes_or_authorizes_artifact_reads(owned_run):
    async def fail():
        raise OSError("synthetic close failure")

    owned_run.owner.aclose = fail
    with pytest.raises(OSError):
        owned_run.run()
    assert [event[0] for event in owned_run.events] == ["launch", "observe"]


def test_timeout_finalization_failure_never_authorizes_artifact_reads(owned_run, monkeypatch):
    owned_run.observation.forced = True

    def fail(*args):
        raise ValueError("synthetic partial summary failure")

    monkeypatch.setattr(watchdog, "_finalize_timeout", fail)
    with pytest.raises(ValueError):
        owned_run.run()
    assert [event[0] for event in owned_run.events] == ["launch", "observe", "close"]


def test_exit_observed_at_deadline_cannot_preserve_qualification(owned_run):
    async def observe(**kwargs):
        owned_run.now[0] = 1120.0
        return owned_run.observation

    owned_run.owner.drain_after_grace = observe
    assert owned_run.run() == 1
    assert owned_run.events[-2:] == [("finalize", owned_run.directory, 0), ("allow",)]


def test_artifact_gate_is_a_fixed_value_written_only_by_verified_completion(tmp_path, monkeypatch):
    output = tmp_path / "outputs"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))

    watchdog._allow_artifacts()

    assert output.read_text(encoding="utf-8") == "cleanup_verified=true\n"


@pytest.fixture
def native_owner():
    events = []
    state = {"active": 1, "child_exited": False}

    def signaled(handle, _timeout):
        events.append(("wait", handle))
        return handle == 107 or state["child_exited"]

    api = MagicMock(spec=watchdog._DiagnosticApi)
    api.active_processes = lambda _job: state["active"]
    api.members_signaled = lambda _job: signaled(107, 0) and signaled(108, 0)
    api.exit_code = lambda _handle: 0
    owner = watchdog._DiagnosticProcess(
        owner=OwnedProcessRef("fixture", object(), 7),
        api=api,
        job_handle=11,
        process_handle=107,
        stdin=None,
        stdout=MagicMock(spec=asyncio.StreamReader),
        stderr=MagicMock(spec=asyncio.StreamReader),
        transports=(),
    )
    owner.wait_root = AsyncMock(side_effect=AssertionError("No infinite root wait is allowed"))
    return SimpleNamespace(owner=owner, api=api, state=state, events=events)


def test_zero_job_accounting_cannot_finalize_while_held_child_handle_is_unsignaled(native_owner):
    native_owner.state["active"] = 0
    result = asyncio.run(native_owner.owner._wait_for_zero(0, forced=True))

    assert (result.status, result.active_processes) == (DrainStatus.TIMED_OUT, 0)
    assert ("wait", 107) in native_owner.events
    assert ("wait", 108) in native_owner.events
    native_owner.owner.wait_root.assert_not_called()


def test_all_held_handles_must_signal_after_accounting_zero_before_drain(native_owner):
    native_owner.state.update(active=0, child_exited=True)
    result = asyncio.run(native_owner.owner._wait_for_zero(0, forced=True))

    assert (result.status, result.active_processes, result.root_returncode) == (
        DrainStatus.DRAINED,
        0,
        0,
    )
    native_owner.owner.wait_root.assert_not_called()


@pytest.fixture
def terminating_api():
    api: Any = object.__new__(watchdog._DiagnosticApi)
    api._members = {}
    api._termination_started = False
    api.capture_enabled = True
    api._coverage_verified = True
    api._sealed_total = None
    api.cleanup_deadline = float("inf")
    events = []
    state = {"snapshots": [(2, (7, 8)), (2, ())], "total": 2, "signaled": False}

    def snapshot(_job):
        events.append("snapshot")
        return state["snapshots"].pop(0)

    def capture(_job, pids):
        events.append(("capture", pids))
        identities = {(pid, pid * 1000) for pid in pids}
        for identity in identities:
            api._members.setdefault(identity, identity[0] + 100)
        return identities

    def terminate(_job, _code):
        events.append("terminate")
        return True

    api._snapshot = snapshot
    api._capture = capture
    api._terminate_job = terminate
    api._total_processes = lambda _job: state["total"]
    api.wait_for_process = lambda _handle, _timeout: state["signaled"]
    return SimpleNamespace(api=api, state=state, events=events)


def test_current_member_handles_are_captured_before_job_termination(terminating_api):
    terminating_api.api.terminate_job(11)

    assert terminating_api.events == [
        "snapshot",
        ("capture", (7, 8)),
        "terminate",
        "snapshot",
        ("capture", ()),
    ]
    assert terminating_api.api.members_signaled(11) is False
    terminating_api.state["signaled"] = True
    assert terminating_api.api.members_signaled(11) is True


def test_newly_admitted_race_member_requires_a_retained_post_termination_handle(terminating_api):
    terminating_api.state.update(snapshots=[(2, (7, 8)), (3, (9,))], total=3, signaled=True)
    terminating_api.api.terminate_job(11)

    assert (9, 9000) in terminating_api.api._members
    assert terminating_api.api.members_signaled(11) is True


def test_uncovered_new_process_delta_refuses_cleanup_even_after_handles_signal(terminating_api):
    terminating_api.state.update(snapshots=[(2, (7, 8)), (3, ())], total=3, signaled=True)
    terminating_api.api.terminate_job(11)

    with pytest.raises(_Win32CallError):
        terminating_api.api.members_signaled(11)


def test_growth_after_the_termination_snapshot_refuses_finalization(terminating_api):
    terminating_api.api.terminate_job(11)
    terminating_api.state.update(total=3, signaled=True)

    with pytest.raises(_Win32CallError):
        terminating_api.api.members_signaled(11)


def test_failed_enumeration_still_terminates_only_the_owned_job_and_refuses_proof(terminating_api):
    def fail(_job):
        raise _Win32CallError(AdmissionStage.DRAIN, 5)

    terminating_api.api._snapshot = fail
    terminating_api.api.terminate_job(11)

    assert terminating_api.events == ["terminate"]
    with pytest.raises(_Win32CallError):
        terminating_api.api.members_signaled(11)


def test_failed_member_verification_refuses_drain(native_owner):
    def refuse(_job):
        raise _Win32CallError(AdmissionStage.DRAIN, 5)

    native_owner.state["active"] = 0
    native_owner.api.members_signaled = refuse
    result = asyncio.run(native_owner.owner._wait_for_zero(0, forced=True))

    assert (result.status, result.active_processes) == (DrainStatus.FAILED, 0)


def test_bounded_wait_observes_child_signal_instead_of_using_root_exit(native_owner, monkeypatch):
    current = [1000.0]
    native_owner.state["active"] = 0

    async def sleep(seconds):
        current[0] += seconds
        native_owner.state["child_exited"] = True

    monkeypatch.setattr(watchdog.time, "monotonic", lambda: current[0])
    monkeypatch.setattr(watchdog.asyncio, "sleep", sleep)
    result = asyncio.run(native_owner.owner._wait_for_zero(5, forced=True))

    assert result.status is DrainStatus.DRAINED
    assert current[0] - 1000.0 <= 5.0


def test_expired_cleanup_bound_cannot_wait_another_five_seconds(native_owner, monkeypatch):
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 1000.0)
    native_owner.owner._cleanup_deadline = 1000.0
    native_owner.state["active"] = 0
    result = asyncio.run(native_owner.owner._wait_for_zero(5, forced=True))

    assert result.status is DrainStatus.TIMED_OUT


def test_joining_cleanup_does_not_repeat_the_already_successful_job_termination(terminating_api):
    terminating_api.api.terminate_job(11)
    terminating_api.api.terminate_job(11)

    assert terminating_api.events.count("terminate") == 1


@pytest.mark.parametrize("operation", ["snapshot", "capture", "accounting", "wait"])
def test_expired_shared_deadline_refuses_native_acquisition_and_waits(operation, monkeypatch):
    api = object.__new__(watchdog._DiagnosticApi)
    api.cleanup_deadline = 1000.0
    monkeypatch.setattr(watchdog.time, "monotonic", lambda: 1000.0)
    with pytest.raises(_Win32CallError):
        if operation == "snapshot":
            api._snapshot(11)
        elif operation == "capture":
            api._capture(11, (8,))
        elif operation == "accounting":
            api._total_processes(11)
        else:
            api.members_signaled(11)


def test_failed_admission_does_not_create_member_handles_outside_the_house_reaper(terminating_api):
    terminating_api.api.capture_enabled = False
    terminating_api.api.terminate_job(11)

    assert terminating_api.events == ["terminate"]
    assert terminating_api.api._members == {}
