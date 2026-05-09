from __future__ import annotations

import sys
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke_v2.actions import ActionContext
from netcoredbg_mcp.session.runtime_smoke_v2.probe_dispatcher import ProbeContext
from netcoredbg_mcp.session.runtime_smoke_v2.probes.process_metric import (
    handle_process_metric,
)

from .helpers import ProbeSmokeSession, after_probe, one_probe_plan, runner


class ProcessMetricSession(ProbeSmokeSession):
    process_id = 4242


class FakePsutil:
    class Error(Exception):
        pass

    class _AccessDeniedError(Error):
        pass

    class _NoSuchProcessError(Error):
        pass

    class _ZombieProcessError(_NoSuchProcessError):
        pass

    AccessDenied = _AccessDeniedError
    NoSuchProcess = _NoSuchProcessError
    ZombieProcess = _ZombieProcessError

    def __init__(self) -> None:
        self.samples = [
            SimpleNamespace(rss=100 * 1024 * 1024, private=40 * 1024 * 1024),
            SimpleNamespace(rss=112 * 1024 * 1024, private=45 * 1024 * 1024),
        ]
        self.pids: list[int] = []
        self.error: Exception | None = None

    def Process(self, pid: int) -> Any:  # noqa: N802 - mirrors psutil module API
        self.pids.append(pid)
        fake = self

        class FakeProcess:
            def memory_info(self) -> Any:
                if fake.error is not None:
                    raise fake.error
                if fake.samples:
                    return fake.samples.pop(0)
                return SimpleNamespace(rss=0, private=0)

        return FakeProcess()


@pytest.mark.asyncio
async def test_process_metric_probe_records_memory_deltas(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_psutil = FakePsutil()
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    session = ProcessMetricSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "process.metric",
                "name": "process_memory",
                "pid": 4242,
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "PASS"
    assert probe["status"] == "PASS"
    assert probe["value"]["working_set_delta_mb"] == 12.0
    assert probe["value"]["private_bytes_delta_mb"] in {None, 5.0}
    assert probe["value"]["action_latency_ms"] >= 0
    assert fake_psutil.pids == [4242, 4242]


@pytest.mark.asyncio
async def test_process_metric_probe_blocks_when_psutil_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(sys.modules, "psutil", None)
    session = ProcessMetricSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "process.metric",
                "name": "process_memory",
                "pid": 4242,
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "psutil is not installed"
    assert "install psutil" in probe["next_step"]


@pytest.mark.asyncio
async def test_process_metric_probe_blocks_invalid_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psutil = FakePsutil()
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    session = ProcessMetricSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "process.metric",
                "name": "process_memory",
                "pid": "not-a-pid",
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"].startswith("invalid pid")
    assert fake_psutil.pids == []


@pytest.mark.asyncio
async def test_process_metric_probe_preserves_explicit_zero_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psutil = FakePsutil()
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    session = ProcessMetricSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "process.metric",
                "name": "process_memory",
                "pid": 0,
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "invalid pid: pid must be positive"
    assert probe["requested"]["pid"] == 0
    assert fake_psutil.pids == []


@pytest.mark.asyncio
async def test_process_metric_probe_blocks_boolean_pid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psutil = FakePsutil()
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    session = ProcessMetricSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "process.metric",
                "name": "process_memory",
                "pid": True,
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "invalid pid: pid must be positive"
    assert probe["requested"]["pid"] is True
    assert fake_psutil.pids == []


@pytest.mark.asyncio
async def test_process_metric_probe_blocks_inaccessible_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psutil = FakePsutil()
    fake_psutil.error = fake_psutil.NoSuchProcess("process vanished")
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    session = ProcessMetricSession()

    result = await runner(session).run(
        one_probe_plan(
            {
                "kind": "process.metric",
                "name": "process_memory",
                "pid": 4242,
            }
        )
    )

    probe = after_probe(result)
    assert result["status"] == "BLOCKED"
    assert probe["status"] == "BLOCKED"
    assert probe["reason"] == "target process is not accessible"
    assert probe["error"] == "process vanished"


@pytest.mark.asyncio
async def test_process_metric_probe_propagates_unexpected_sample_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psutil = FakePsutil()
    fake_psutil.error = RuntimeError("internal sampler bug")
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    context = ProbeContext(
        action_context=ActionContext(
            service_adapters={},
            clock=lambda: 1.0,
            session=ProcessMetricSession(),
        ),
    )

    with pytest.raises(RuntimeError, match="internal sampler bug"):
        await handle_process_metric(
            {"kind": "process.metric", "name": "process_memory", "pid": 4242},
            context,
            phase="before",
        )


@pytest.mark.asyncio
async def test_process_metric_probe_blocks_after_without_before_baseline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_psutil = FakePsutil()
    monkeypatch.setitem(sys.modules, "psutil", fake_psutil)
    context = ProbeContext(
        action_context=ActionContext(
            service_adapters={},
            clock=lambda: 1.0,
            session=ProcessMetricSession(),
        ),
    )

    result = await handle_process_metric(
        {"kind": "process.metric", "name": "process_memory", "pid": 4242},
        context,
        phase="after",
    )

    assert result["status"] == "BLOCKED"
    assert result["reason"] == "process.metric baseline is missing"
    assert result["requested"] == {"phase": "after", "pid": 4242}
    assert "before phase" in result["next_step"]
