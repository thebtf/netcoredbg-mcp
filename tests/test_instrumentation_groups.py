"""Runtime smoke instrumentation group tests."""

from __future__ import annotations

import asyncio
import os
import time
from collections import deque
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.server import create_server
from netcoredbg_mcp.session.instrumentation import InstrumentationGroupService
from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeSession
from netcoredbg_mcp.session.state import (
    Breakpoint,
    BreakpointRegistry,
    DebugState,
    TraceEntry,
)
from netcoredbg_mcp.session.tracepoints import TracepointManager
from netcoredbg_mcp.tools.runtime_smoke import register_runtime_smoke_tools


class FakeInstrumentationSession:
    def __init__(self) -> None:
        self.breakpoints = BreakpointRegistry()
        self.runtime_smoke = RuntimeSmokeSession()
        self.state = SimpleNamespace(
            state=DebugState.STOPPED,
            hit_counts={},
            output_buffer=deque(),
        )
        self._tracepoint_manager = TracepointManager()
        self.leak_breakpoint_removal = False
        self.validation_failure: str | None = None
        self._next_breakpoint_id = 100

    async def add_breakpoint(
        self,
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
        *,
        tracepoint_id: str | None = None,
        group_id: str | None = None,
    ) -> Breakpoint:
        self._next_breakpoint_id += 1
        bp = Breakpoint(
            file=file,
            line=line,
            condition=condition,
            hit_condition=hit_condition,
            verified=True,
            id=self._next_breakpoint_id,
            dap_line=line + 1,
        )
        return self.breakpoints.add(bp, tracepoint_id=tracepoint_id, group_id=group_id)

    async def remove_breakpoint(
        self, file: str, line: int, *, tracepoint_id: str | None = None,
        group_id: str | None = None,
    ) -> bool:
        if self.leak_breakpoint_removal:
            return False
        return self.breakpoints.remove(file, line, tracepoint_id=tracepoint_id, group_id=group_id)

    def validate_path(self, file: str, must_exist: bool = True) -> str:
        if self.validation_failure:
            raise ValueError(self.validation_failure)
        return file


class FailingAddInstrumentationSession(FakeInstrumentationSession):
    def __init__(self, failing_line: int) -> None:
        super().__init__()
        self.failing_line = failing_line

    async def add_breakpoint(
        self,
        file: str,
        line: int,
        condition: str | None = None,
        hit_condition: str | None = None,
        *,
        tracepoint_id: str | None = None,
        group_id: str | None = None,
    ) -> Breakpoint:
        if line == self.failing_line:
            raise RuntimeError("breakpoint setup failed")
        return await super().add_breakpoint(
            file, line, condition, hit_condition, tracepoint_id=tracepoint_id, group_id=group_id
        )


async def _noop_resolve_project_root(ctx: Any, session: Any) -> None:
    pass

@pytest.mark.asyncio
async def test_clear_during_group_creation_does_not_lose_ownership(monkeypatch) -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    entered, release = asyncio.Event(), asyncio.Event()
    original_add = session.add_breakpoint

    async def held_add(*args: Any, **kwargs: Any) -> Breakpoint:
        result = await original_add(*args, **kwargs)
        entered.set()
        await release.wait()
        return result

    monkeypatch.setattr(session, "add_breakpoint", held_add)
    create = asyncio.create_task(service.create_group(
        "flow", breakpoints=[{"file": "C:/repo/Program.cs", "line": 10}]
    ))
    await asyncio.wait_for(entered.wait(), timeout=2)
    clear = asyncio.create_task(service.clear_group("flow"))
    await asyncio.sleep(0)
    early = clear.done()
    try:
        if early:
            assert clear.result().to_dict()["status"] == "FAIL"
    finally:
        release.set()
        created = (await create).to_dict()
    assert created["status"] == "PASS"
    if early:
        assert (await service.inspect_group("flow")).to_dict()["status"] == "PASS"
        assert session.breakpoints.get_for_file("C:/repo/Program.cs")
    else:
        assert (await clear).to_dict()["status"] == "PASS"
        assert (await service.inspect_group("flow")).to_dict()["status"] == "FAIL"
        assert not session.breakpoints.get_for_file("C:/repo/Program.cs")



@pytest.mark.asyncio
async def test_create_and_inspect_group_returns_hits_and_trace_logs() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"

    created = await service.create_group(
        "flow",
        breakpoints=[{"file": source, "line": 10}],
        tracepoints=[{"file": source, "line": 20, "expression": "i"}],
    )
    created_data = created.to_dict()
    tracepoint_id = created_data["tracepoints"][0]["id"]

    norm = session.breakpoints._normalize_path(source)
    session.state.hit_counts[(norm, 10)] = 3
    session._tracepoint_manager._trace_buffer.append(
        TraceEntry(time.monotonic(), source, 20, "i", "1", 1, tracepoint_id)
    )
    session._tracepoint_manager._trace_buffer.append(
        TraceEntry(time.monotonic(), source, 20, "i", "2", 1, tracepoint_id)
    )

    inspected = (await service.inspect_group("flow")).to_dict()

    assert created_data["status"] == "PASS"
    assert created_data["summary"]["group"] == "flow"
    assert created_data["summary"]["breakpoint_count"] == 1
    assert created_data["summary"]["tracepoint_count"] == 1
    assert created_data["breakpoints"][0]["dap_line"] == 11
    assert inspected["status"] == "PASS"
    assert inspected["summary"]["hit_count"] == 3
    assert inspected["summary"]["trace_log_count"] == 2
    assert inspected["tracepoints"][0]["logs"] == [
        {"line": 20, "expression": "i", "value": "1", "tracepoint_id": tracepoint_id},
        {"line": 20, "expression": "i", "value": "2", "tracepoint_id": tracepoint_id},
    ]


@pytest.mark.asyncio
async def test_duplicate_group_rejected_without_mutating_existing_group() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)

    first = await service.create_group(
        "flow",
        breakpoints=[{"file": "C:/repo/Program.cs", "line": 10}],
    )
    duplicate = await service.create_group(
        "flow",
        breakpoints=[{"file": "C:/repo/Other.cs", "line": 20}],
    )

    assert first.to_dict()["status"] == "PASS"
    assert duplicate.to_dict()["status"] == "FAIL"
    assert duplicate.to_dict()["reason"] == "instrumentation group already exists"
    assert (await service.inspect_group("flow")).to_dict()["breakpoints"][0]["line"] == 10


@pytest.mark.asyncio
async def test_group_creation_preserves_existing_user_breakpoint_intent() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"
    await session.add_breakpoint(source, 10, condition="value > 0")

    created = (await service.create_group(
        "flow", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()

    assert created["status"] == "PASS"
    assert (await service.inspect_group("flow")).to_dict()["status"] == "PASS"
    breakpoints = session.breakpoints.get_for_file(source)
    assert len(breakpoints) == 1
    assert breakpoints[0].line == 10
    assert breakpoints[0].user_owned
    assert breakpoints[0].condition == "value > 0"


@pytest.mark.asyncio
async def test_group_cleanup_preserves_existing_user_breakpoint_intent() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"
    await session.add_breakpoint(source, 10, condition="value > 0")
    created = (await service.create_group(
        "flow", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()

    cleared = (await service.clear_group("flow")).to_dict()

    assert created["status"] == "PASS"
    assert cleared["status"] == "PASS"
    assert (await service.inspect_group("flow")).to_dict()["status"] == "FAIL"
    breakpoints = session.breakpoints.get_for_file(source)
    assert len(breakpoints) == 1
    assert breakpoints[0].line == 10
    assert breakpoints[0].user_owned
    assert breakpoints[0].condition == "value > 0"


@pytest.mark.asyncio
async def test_shared_groups_clear_independently_and_preserve_user_breakpoint() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"
    await session.add_breakpoint(source, 10, condition="value > 0")
    first = (await service.create_group(
        "first", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()
    second = (await service.create_group(
        "second", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()

    first_clear = (await service.clear_group("first")).to_dict()

    assert first["status"] == "PASS"
    assert second["status"] == "PASS"
    assert first_clear["status"] == "PASS"
    assert (await service.inspect_group("first")).to_dict()["status"] == "FAIL"
    assert (await service.inspect_group("second")).to_dict()["status"] == "PASS"
    breakpoints = session.breakpoints.get_for_file(source)
    assert len(breakpoints) == 1
    assert breakpoints[0].line == 10
    assert breakpoints[0].user_owned
    assert breakpoints[0].condition == "value > 0"

    assert await session.remove_breakpoint(source, 10)
    breakpoints = session.breakpoints.get_for_file(source)
    assert len(breakpoints) == 1
    assert breakpoints[0].line == 10
    assert not breakpoints[0].user_owned
    assert (await service.inspect_group("second")).to_dict()["status"] == "PASS"
    await session.add_breakpoint(source, 10, condition="value > 0")

    second_clear = (await service.clear_group("second")).to_dict()

    assert second_clear["status"] == "PASS"
    assert (await service.inspect_group("second")).to_dict()["status"] == "FAIL"
    breakpoints = session.breakpoints.get_for_file(source)
    assert len(breakpoints) == 1
    assert breakpoints[0].line == 10
    assert breakpoints[0].user_owned
    assert breakpoints[0].condition == "value > 0"


@pytest.mark.asyncio
async def test_direct_user_owner_can_be_added_and_removed_while_group_remains() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"
    created = (await service.create_group(
        "flow", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()

    await session.add_breakpoint(source, 10, condition="value > 0")

    assert created["status"] == "PASS"
    breakpoints = session.breakpoints.get_for_file(source)
    assert len(breakpoints) == 1
    assert breakpoints[0].user_owned
    assert breakpoints[0].condition == "value > 0"
    assert await session.remove_breakpoint(source, 10)
    assert (await service.inspect_group("flow")).to_dict()["status"] == "PASS"
    breakpoints = session.breakpoints.get_for_file(source)
    assert len(breakpoints) == 1
    assert breakpoints[0].line == 10
    assert not breakpoints[0].user_owned

    cleared = (await service.clear_group("flow")).to_dict()

    assert cleared["status"] == "PASS"
    assert (await service.inspect_group("flow")).to_dict()["status"] == "FAIL"
    assert session.breakpoints.get_for_file(source) == []


@pytest.mark.asyncio
async def test_clear_group_reports_leaked_group_owned_breakpoints() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"

    await service.create_group("flow", breakpoints=[{"file": source, "line": 10}])
    session.leak_breakpoint_removal = True

    cleared = (await service.clear_group("flow")).to_dict()

    assert cleared["status"] == "FAIL"
    assert cleared["reason"] == "instrumentation group cleanup leaked state"
    assert cleared["leaks"] == [
        {"kind": "breakpoint", "file": os.path.normpath(source), "line": 10}
    ]
    assert (await service.inspect_group("flow")).to_dict()["status"] == "FAIL"
    assert "flow" in session.runtime_smoke.instrumentation_groups


@pytest.mark.asyncio
@pytest.mark.parametrize("leak", [False, True])
async def test_group_tracepoint_cleanup_preserves_user_and_detects_leak(
    leak: bool,
) -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"
    await session.add_breakpoint(source, 10)
    await service.create_group(
        "flow", tracepoints=[{"file": source, "line": 10, "expression": "i"}]
    )
    session.leak_breakpoint_removal = leak

    cleared = (await service.clear_group("flow")).to_dict()

    assert cleared["status"] == ("FAIL" if leak else "PASS")
    assert session.breakpoints.get_for_file(source)[0].user_owned

@pytest.mark.asyncio
async def test_unknown_group_clear_fails_without_mutating_other_groups() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)

    await service.create_group(
        "flow",
        breakpoints=[{"file": "C:/repo/Program.cs", "line": 10}],
    )
    unknown = (await service.clear_group("missing")).to_dict()

    assert unknown["status"] == "FAIL"
    assert unknown["reason"] == "instrumentation group not found"
    assert (await service.inspect_group("flow")).to_dict()["status"] == "PASS"


@pytest.mark.asyncio
async def test_create_group_rolls_back_partial_breakpoints_and_tracepoints() -> None:
    session = FailingAddInstrumentationSession(failing_line=20)
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"

    with pytest.raises(RuntimeError, match="breakpoint setup failed"):
        await service.create_group(
            "flow",
            breakpoints=[{"file": source, "line": 10}],
            tracepoints=[{"file": source, "line": 20, "expression": "i"}],
        )

    assert session.runtime_smoke.instrumentation_groups == {}
    assert session.breakpoints.get_for_file(source) == []
    assert session._tracepoint_manager.tracepoints == {}


@pytest.mark.asyncio
async def test_runtime_smoke_reset_retains_groups_until_async_cleanup() -> None:
    session = FakeInstrumentationSession()
    service = InstrumentationGroupService(session)
    source = "C:/repo/Program.cs"
    await service.create_group(
        "flow", breakpoints=[{"file": source, "line": 10}],
        tracepoints=[{"file": source, "line": 20, "expression": "i"}],
    )

    first = session.runtime_smoke.reset()
    second = session.runtime_smoke.reset()

    assert first and second
    assert "flow" in session.runtime_smoke.instrumentation_groups
    assert len(session.breakpoints.get_for_file(source)) == 2
    assert session._tracepoint_manager.tracepoints
    assert (await service.clear_group("flow")).to_dict()["status"] == "PASS"
    assert session.runtime_smoke.reset() == ()
    assert session.breakpoints.get_for_file(source) == []
    assert session._tracepoint_manager.tracepoints == {}


@pytest.mark.asyncio
async def test_instrumentation_group_tools_validate_names_and_register(
    mock_netcoredbg_path,
    capturing_mcp,
) -> None:
    server = create_server(str(os.getcwd()))
    tools = await server.list_tools()
    tool_names = {tool.name for tool in tools}

    assert {
        "instrumentation_group_create",
        "instrumentation_group_inspect",
        "instrumentation_group_clear",
    }.issubset(tool_names)

    mcp = capturing_mcp
    session = FakeInstrumentationSession()
    register_runtime_smoke_tools(
        mcp=mcp,
        session=session,
        check_session_access=lambda ctx: None,
        resolve_project_root=_noop_resolve_project_root,
    )

    response = await mcp.tools["instrumentation_group_create"](
        ctx=None,
        name="bad group name",
        breakpoints=[{"file": "C:/repo/Program.cs", "line": 10}],
    )

    assert response["data"]["status"] == "FAIL"
    assert response["data"]["reason"] == "invalid instrumentation group name"
