"""Tests for CR-002 loaded source, disassembly, and location tools."""

from __future__ import annotations

import asyncio
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from netcoredbg_mcp.dap.protocol import DAPEvent, DAPResponse
from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.session.state import DebugState, LoadedSource, TraceEntry
from netcoredbg_mcp.session.tracepoints import TracepointManager


class ToolRegistry:
    def __init__(self):
        self.tools = {}

    def tool(self, *args, **kwargs):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


def make_response(success: bool = True, body: dict | None = None, message: str | None = None):
    return DAPResponse(
        seq=1,
        request_seq=1,
        success=success,
        command="inspection",
        body=body or {},
        message=message,
    )


def make_manager() -> SessionManager:
    manager = SessionManager(netcoredbg_path="/path")
    manager.client._capabilities = {}
    return manager


def register_tools(manager: SessionManager):
    from netcoredbg_mcp.tools.inspection import register_inspection_tools

    registry = ToolRegistry()
    register_inspection_tools(registry, manager, lambda ctx: None)
    return registry.tools


@pytest.mark.asyncio
async def test_get_loaded_sources_tool_capability_gate():
    manager = make_manager()
    manager.client._capabilities = {"supportsLoadedSourcesRequest": False}
    manager.get_loaded_sources = AsyncMock()
    tools = register_tools(manager)

    response = await tools["get_loaded_sources"]()

    assert "error" in response
    assert "supportsLoadedSourcesRequest" in response["error"]
    manager.get_loaded_sources.assert_not_called()


@pytest.mark.asyncio
async def test_get_loaded_sources_tool_success_path():
    manager = make_manager()
    manager.client._capabilities = {"supportsLoadedSourcesRequest": True}
    manager.get_loaded_sources = AsyncMock(
        return_value=[
            {
                "name": "Program.cs",
                "path": "C:/src/Program.cs",
                "sourceReference": 7,
                "origin": None,
                "presentationHint": "normal",
                "adapterData": {"kind": "source"},
            }
        ]
    )
    tools = register_tools(manager)

    response = await tools["get_loaded_sources"]()

    assert response["data"]["count"] == 1
    assert response["data"]["sources"][0]["sourceReference"] == 7
    manager.get_loaded_sources.assert_called_once_with()


@pytest.mark.asyncio
async def test_get_loaded_sources_manager_maps_and_tracks_state():
    manager = make_manager()
    manager.state.loaded_sources["c:/src/stale.cs"] = LoadedSource(
        name="Stale.cs",
        path="C:/src/Stale.cs",
    )
    manager.client.loaded_sources = AsyncMock(
        return_value=make_response(
            body={
                "sources": [
                    {
                        "name": "Program.cs",
                        "path": "C:/src/Program.cs",
                        "sourceReference": 7,
                        "origin": "generated",
                        "presentationHint": "normal",
                        "adapterData": {"kind": "source"},
                    }
                ],
            }
        )
    )

    sources = await manager.get_loaded_sources()

    assert sources[0]["sourceReference"] == 7
    assert len(manager.state.loaded_sources) == 1
    assert all(source.path != "C:/src/Stale.cs" for source in manager.state.loaded_sources.values())


@pytest.mark.asyncio
async def test_disassemble_tool_capability_gate():
    manager = make_manager()
    manager.client._capabilities = {"supportsDisassembleRequest": False}
    manager.disassemble = AsyncMock()
    tools = register_tools(manager)

    response = await tools["disassemble"]("0x1234")

    assert "error" in response
    assert "supportsDisassembleRequest" in response["error"]
    assert "--enable-disassembly" in response["error"]
    manager.disassemble.assert_not_called()


@pytest.mark.asyncio
async def test_disassemble_tool_success_path():
    manager = make_manager()
    manager.client._capabilities = {"supportsDisassembleRequest": True}
    manager.disassemble = AsyncMock(
        return_value=[
            {
                "address": "0x1234",
                "instructionBytes": "55",
                "instruction": "push rbp",
                "symbol": "main",
                "presentationHint": "normal",
            }
        ]
    )
    tools = register_tools(manager)

    response = await tools["disassemble"](
        "0x1234",
        offset=4,
        instruction_offset=-1,
        instruction_count=8,
        resolve_symbols=False,
    )

    assert response["data"]["instructions"][0]["instruction"] == "push rbp"
    manager.disassemble.assert_called_once_with(
        "0x1234",
        offset=4,
        instruction_offset=-1,
        instruction_count=8,
        resolve_symbols=False,
    )


@pytest.mark.asyncio
async def test_disassemble_rejects_invalid_args():
    manager = make_manager()

    with pytest.raises(ValueError, match="memory_reference is required"):
        await manager.disassemble("", instruction_count=1)

    with pytest.raises(ValueError, match="instruction_count must be greater than 0"):
        await manager.disassemble("0x1234", instruction_count=0)


@pytest.mark.asyncio
async def test_get_locations_tool_capability_gate():
    manager = make_manager()
    manager.client._capabilities = {"supportsLocationsRequest": False}
    manager.get_locations = AsyncMock()
    tools = register_tools(manager)

    response = await tools["get_locations"](42)

    assert "error" in response
    assert "supportsLocationsRequest" in response["error"]
    manager.get_locations.assert_not_called()


@pytest.mark.asyncio
async def test_get_locations_tool_success_path():
    manager = make_manager()
    manager.client._capabilities = {"supportsLocationsRequest": True}
    manager.get_locations = AsyncMock(
        return_value={
            "source": {"path": "C:/src/Program.cs"},
            "line": 12,
            "column": 3,
            "end_line": 12,
            "end_column": 8,
            "endLine": 12,
            "endColumn": 8,
        }
    )
    tools = register_tools(manager)

    response = await tools["get_locations"](42)

    assert response["data"]["source"]["path"] == "C:/src/Program.cs"
    assert response["data"]["end_line"] == 12
    manager.get_locations.assert_called_once_with(42)


@pytest.mark.asyncio
async def test_get_locations_rejects_invalid_args():
    manager = make_manager()

    with pytest.raises(ValueError, match="location_reference must be greater than 0"):
        await manager.get_locations(0)


@pytest.mark.asyncio
async def test_manager_disassemble_and_locations_map_dap_fields():
    manager = make_manager()
    manager.client.disassemble = AsyncMock(
        return_value=make_response(
            body={
                "instructions": [
                    {
                        "address": "0x1234",
                        "instructionBytes": "55",
                        "instruction": "push rbp",
                        "presentationHint": "normal",
                    }
                ],
            }
        )
    )
    manager.client.locations = AsyncMock(
        return_value=make_response(
            body={
                "source": {"path": "C:/src/Program.cs"},
                "line": 20,
                "column": 1,
                "endLine": 20,
                "endColumn": 5,
            }
        )
    )

    instructions = await manager.disassemble("0x1234", instruction_count=1)
    location = await manager.get_locations(99)

    assert instructions[0]["address"] == "0x1234"
    assert location["line"] == 20
    assert location["end_line"] == 20


@pytest.mark.asyncio
async def test_mark_trace_cursor_and_delta_tools_handle_missing_manager():
    manager = make_manager()
    tools = register_tools(manager)

    cursor_response = await tools["mark_trace_cursor"]()
    delta_response = await tools["get_trace_delta"](cursor_response["data"]["cursor"], limit=5)

    assert cursor_response["data"]["cursor"]["after_timestamp"] is None
    assert cursor_response["data"]["cursor"]["tracepoint_id"] is None
    assert cursor_response["data"]["buffer_size"] == 0
    assert delta_response["data"]["entries"] == []
    assert delta_response["data"]["available"] == 0
    assert delta_response["data"]["limited"] is False
    assert delta_response["data"]["stale"] is False


@pytest.mark.asyncio
async def test_trace_delta_tool_missing_manager_honors_tracepoint_override():
    manager = make_manager()
    tools = register_tools(manager)
    cursor = {
        "after_timestamp": 2.0,
        "tracepoint_id": "tp-1",
        "buffer_start_timestamp": 1.0,
        "buffer_size": 2,
    }

    response = await tools["get_trace_delta"](cursor, tracepoint_id="tp-2")

    assert response["data"]["next_cursor"]["tracepoint_id"] == "tp-2"


@pytest.mark.asyncio
async def test_trace_delta_tool_returns_bounded_entries_and_continuation_cursor():
    manager = make_manager()
    tracepoints = TracepointManager()
    tracepoints._trace_buffer.append(TraceEntry(1.0, "a.cs", 1, "a", "old", 1, "tp-1"))
    tracepoints._trace_buffer.append(TraceEntry(2.0, "a.cs", 1, "a", "boundary", 1, "tp-1"))
    tracepoints._trace_buffer.append(TraceEntry(2.5, "b.cs", 2, "b", "other", 1, "tp-2"))
    manager._tracepoint_manager = tracepoints
    tools = register_tools(manager)

    cursor_response = await tools["mark_trace_cursor"](tracepoint_id="tp-1")
    tracepoints._trace_buffer.append(TraceEntry(3.0, "a.cs", 1, "a", "new-1", 1, "tp-1"))
    tracepoints._trace_buffer.append(TraceEntry(4.0, "a.cs", 1, "a", "new-2", 1, "tp-1"))
    tracepoints._trace_buffer.append(TraceEntry(5.0, "b.cs", 2, "b", "other-new", 1, "tp-2"))

    delta_response = await tools["get_trace_delta"](cursor_response["data"]["cursor"], limit=1)

    data = delta_response["data"]
    assert [entry["value"] for entry in data["entries"]] == ["new-1"]
    assert data["entries"][0]["tracepoint_id"] == "tp-1"
    assert data["available"] == 2
    assert data["limit"] == 1
    assert data["limited"] is True
    assert data["stale"] is False
    assert data["dropped_count"] == 0
    assert data["next_cursor"]["after_timestamp"] == 3.0
    assert data["next_cursor"]["tracepoint_id"] == "tp-1"


@pytest.mark.asyncio
@pytest.mark.parametrize("user_owned", [False, True])
async def test_list_breakpoints_reports_group_or_user_condition(user_owned):
    from netcoredbg_mcp.tools.breakpoints import register_breakpoint_tools

    manager = make_manager()
    source = os.path.realpath(__file__)
    if user_owned:
        await manager.add_breakpoint(source, 10, condition="user_value > 0")
    created = (await manager.instrumentation.create_group(
        "flow", breakpoints=[{"file": source, "line": 10, "condition": "group_value > 0"}]
    )).to_dict()
    assert created["status"] == "PASS"
    registry = ToolRegistry()
    register_breakpoint_tools(registry, manager, lambda ctx: None, AsyncMock(), AsyncMock())

    result = await registry.tools["list_breakpoints"](None)

    listed = result["data"][manager.breakpoints._normalize_path(source)]
    assert len(listed) == 1
    assert listed[0]["line"] == 10
    assert listed[0]["condition"] == (
        "user_value > 0" if user_owned else "group_value > 0"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("second_condition", "expected_condition", "ambiguous"),
    [("value > 0", "value > 0", False), ("value < 0", None, True), (None, None, True)],
)
async def test_list_breakpoints_reports_shared_group_condition_ambiguity(
    second_condition, expected_condition, ambiguous
):
    from netcoredbg_mcp.tools.breakpoints import register_breakpoint_tools

    manager = make_manager()
    source = os.path.realpath(__file__)
    for name, condition in (("first", "value > 0"), ("second", second_condition)):
        created = (await manager.instrumentation.create_group(
            name, breakpoints=[{"file": source, "line": 10, "condition": condition}]
        )).to_dict()
        assert created["status"] == "PASS"
    registry = ToolRegistry()
    register_breakpoint_tools(registry, manager, lambda ctx: None, AsyncMock(), AsyncMock())

    result = await registry.tools["list_breakpoints"](None)

    listed = result["data"][manager.breakpoints._normalize_path(source)]
    assert len(listed) == 1
    assert listed[0]["condition"] == expected_condition
    assert listed[0].get("condition_ambiguous", False) is ambiguous


@pytest.mark.asyncio
async def test_group_conditions_reconcile_conservatively_and_restore_user_intent():
    manager = make_manager()
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0", hit_condition="3")
    await manager.instrumentation.create_group(
        "first", breakpoints=[{"file": source, "line": 10,
                                "condition": "value < 10", "hit_condition": "5"}]
    )
    await manager.instrumentation.create_group(
        "second", breakpoints=[{"file": source, "line": 10, "condition": "value == 0"}]
    )
    bp = manager.breakpoints.get_for_file(source)[0]
    assert bp.condition == "value > 0"
    assert bp.hit_condition == "3"
    assert set(bp.group_owners.values()) == {("value < 10", "5"), ("value == 0", None)}
    assert bp.to_dap() == {"line": 10}
    snapshot = manager.breakpoints.snapshot_for_file(source)
    assert (await manager.instrumentation.clear_group("first")).to_dict()["status"] == "PASS"
    assert len(snapshot[0].group_owners) == 2
    assert (await manager.instrumentation.clear_group("second")).to_dict()["status"] == "PASS"
    assert bp.to_dap() == {"line": 10, "condition": "value > 0", "hitCondition": "3"}


@pytest.mark.asyncio
async def test_group_dap_replacement_restores_user_condition_after_clear():
    manager = make_manager()
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0", hit_condition="3")
    manager._state.state = DebugState.RUNNING
    manager.client.set_breakpoints = AsyncMock(return_value=make_response())

    created = await manager.instrumentation.create_group(
        "flow", breakpoints=[{"file": source, "line": 10, "condition": "value < 10"}]
    )
    assert created.to_dict()["status"] == "PASS"
    manager.client.set_breakpoints.assert_awaited_with(source, [{"line": 10}])
    cleared = await manager.instrumentation.clear_group("flow")

    assert cleared.to_dict()["status"] == "PASS"
    manager.client.set_breakpoints.assert_awaited_with(
        source, [{"line": 10, "condition": "value > 0", "hitCondition": "3"}]
    )
    assert manager.client.set_breakpoints.await_count == 2


@pytest.mark.asyncio
async def test_group_clear_preserves_user_on_removed_event_during_dap_replacement():
    manager = make_manager()
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0", hit_condition="3")
    created = (await manager.instrumentation.create_group(
        "flow", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()
    assert created["status"] == "PASS"
    bp = manager.breakpoints.get_for_file(source)[0]
    bp.id = 42
    bp.verified = True
    manager._state.state = DebugState.RUNNING
    entered, release = asyncio.Event(), asyncio.Event()

    async def replace_breakpoints(file, breakpoints):
        assert file == source
        assert breakpoints == [{"line": 10, "condition": "value > 0", "hitCondition": "3"}]
        entered.set()
        await release.wait()
        return make_response(body={"breakpoints": [{"id": 43, "verified": True, "line": 10}]})

    manager.client.set_breakpoints = AsyncMock(side_effect=replace_breakpoints)
    pending = asyncio.create_task(manager.instrumentation.clear_group("flow"))
    try:
        await asyncio.wait_for(entered.wait(), timeout=2)
        manager._on_breakpoint(DAPEvent(
            seq=1, event="breakpoint", body={"reason": "removed", "breakpoint": {"id": 42}}
        ))
    finally:
        release.set()
    cleared = (await asyncio.wait_for(pending, timeout=2)).to_dict()

    assert cleared["status"] == "PASS"
    assert (await manager.instrumentation.inspect_group("flow")).to_dict()["status"] == "FAIL"
    remaining = manager.breakpoints.get_for_file(source)
    assert len(remaining) == 1
    assert remaining[0].user_owned
    assert remaining[0].condition == "value > 0"
    assert remaining[0].hit_condition == "3"
    assert not remaining[0].group_owners
    manager.client.set_breakpoints.assert_awaited_once()


@pytest.mark.asyncio
async def test_cancelled_group_clear_retains_owners_after_late_removed_event():
    manager = make_manager()
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0")
    await manager.instrumentation.create_group(
        "flow", breakpoints=[{"file": source, "line": 10}]
    )
    bp = manager.breakpoints.get_for_file(source)[0]
    bp.id = 42
    bp.verified = True
    manager._state.state = DebugState.RUNNING
    entered = asyncio.Event()

    async def pending_replacement(file, breakpoints):
        entered.set()
        await asyncio.Event().wait()

    manager.client.set_breakpoints = AsyncMock(side_effect=pending_replacement)
    pending = asyncio.create_task(manager.instrumentation.clear_group("flow"))
    await asyncio.wait_for(entered.wait(), timeout=2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    manager._on_breakpoint(DAPEvent(
        seq=1, event="breakpoint", body={"reason": "removed", "breakpoint": {"id": 42}}
    ))

    remaining = manager.breakpoints.get_for_file(source)
    assert len(remaining) == 1
    assert remaining[0].user_owned and remaining[0].condition == "value > 0"
    assert remaining[0].group_owners
    assert not remaining[0].verified and remaining[0].id is None
    assert (await manager.instrumentation.inspect_group("flow")).to_dict()["status"] == "FAIL"
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await manager.instrumentation.clear_group("flow")
    manager.client.set_breakpoints.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["removed", "changed"])
async def test_group_inspection_does_not_report_stale_verified_breakpoint(reason):
    manager = make_manager()
    source = os.path.realpath(__file__)
    manager._state.state = DebugState.RUNNING
    manager.client.set_breakpoints = AsyncMock(return_value=make_response(
        body={"breakpoints": [{"id": 42, "verified": True, "line": 11}]}
    ))
    created = (await manager.instrumentation.create_group(
        "flow", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()
    assert created["status"] == "PASS"
    assert created["breakpoints"][0]["verified"] is True
    assert created["breakpoints"][0]["id"] == 42
    manager._on_breakpoint(DAPEvent(
        seq=1, event="breakpoint",
        body={"reason": reason, "breakpoint": {"id": 42, "verified": False, "line": 12}},
    ))

    inspected = (await manager.instrumentation.inspect_group("flow")).to_dict()

    if inspected["status"] == "PASS":
        observed = inspected["breakpoints"][0]
        assert observed["verified"] is False
        assert observed["id"] == (None if reason == "removed" else 42)
        assert observed["dap_line"] == (None if reason == "removed" else 12)
    else:
        assert inspected["status"] == "FAIL"
    assert manager.breakpoints.get_for_file(source)[0].group_owners


@pytest.mark.asyncio
async def test_group_inspection_rejects_missing_owner_at_shared_location():
    manager = make_manager()
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0")
    first = (await manager.instrumentation.create_group(
        "first", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()
    owner = next(iter(manager.breakpoints.get_for_file(source)[0].group_owners))
    second = (await manager.instrumentation.create_group(
        "second", breakpoints=[{"file": source, "line": 10}]
    )).to_dict()
    assert first["status"] == second["status"] == "PASS"
    assert await manager.remove_breakpoint(source, 10, group_id=owner)

    assert (await manager.instrumentation.inspect_group("first")).to_dict()["status"] == "FAIL"
    assert (await manager.instrumentation.inspect_group("second")).to_dict()["status"] == "PASS"
    bp = manager.breakpoints.get_for_file(source)[0]
    assert bp.user_owned and bp.condition == "value > 0" and len(bp.group_owners) == 1


@pytest.mark.asyncio
async def test_group_inspection_rejects_tracepoint_removed_by_public_tool():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    created = (await manager.instrumentation.create_group(
        "flow", tracepoints=[{"file": source, "line": 10, "expression": "value"}]
    )).to_dict()
    assert created["status"] == "PASS"
    tracepoint_id = created["tracepoints"][0]["id"]

    removed = await tools["remove_tracepoint"](None, tracepoint_id)

    assert removed["data"]["removed"] == tracepoint_id
    assert (await manager.instrumentation.inspect_group("flow")).to_dict()["status"] == "FAIL"
    assert manager.breakpoints.get_for_file(source) == []


@pytest.mark.asyncio
async def test_group_create_uncertain_sync_retains_owner_and_record_without_retry():
    manager = make_manager()
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0")
    manager._state.state = DebugState.RUNNING
    manager.client.set_breakpoints = AsyncMock(side_effect=TimeoutError("uncertain"))

    with pytest.raises(TimeoutError):
        await manager.instrumentation.create_group(
            "flow", breakpoints=[{"file": source, "line": 10, "condition": "value < 10"}]
        )

    bp = manager.breakpoints.get_for_file(source)[0]
    assert bp.user_owned and bp.condition == "value > 0"
    assert list(bp.group_owners.values()) == [("value < 10", None)]
    assert (await manager.instrumentation.inspect_group("flow")).to_dict()["status"] == "FAIL"
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await manager.instrumentation.clear_group("flow")
    manager.client.set_breakpoints.assert_awaited_once()
    assert "flow" in manager.runtime_smoke.instrumentation_groups


@pytest.mark.asyncio
async def test_group_tracepoint_uncertain_creation_retains_cleanup_owner():
    manager = make_manager()
    source = os.path.realpath(__file__)
    manager._state.state = DebugState.RUNNING
    manager.client.set_breakpoints = AsyncMock(side_effect=TimeoutError("uncertain"))

    with pytest.raises(RuntimeError, match="setup failed"):
        await manager.instrumentation.create_group(
            "flow", tracepoints=[{"file": source, "line": 10, "expression": "value"}]
        )

    assert "flow" in manager.runtime_smoke.instrumentation_groups
    assert (await manager.instrumentation.inspect_group("flow")).to_dict()["status"] == "FAIL"
    tracepoints = manager._tracepoint_manager
    assert tracepoints is not None
    tracepoint_id = next(iter(tracepoints.tracepoints))
    assert tracepoint_id in manager.breakpoints.get_for_file(source)[0].tracepoint_owners
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await manager.instrumentation.clear_group("flow")
    manager.client.set_breakpoints.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_clear_uncertain_sync_restores_owner_and_does_not_retry():
    manager = make_manager()
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0")
    await manager.instrumentation.create_group(
        "flow", breakpoints=[{"file": source, "line": 10, "condition": "value < 10"}]
    )
    manager._state.state = DebugState.RUNNING
    manager.client.set_breakpoints = AsyncMock(side_effect=TimeoutError("uncertain"))

    with pytest.raises(TimeoutError):
        await manager.instrumentation.clear_group("flow")

    bp = manager.breakpoints.get_for_file(source)[0]
    assert bp.user_owned and bp.condition == "value > 0"
    assert list(bp.group_owners.values()) == [("value < 10", None)]
    assert "flow" in manager.runtime_smoke.instrumentation_groups
    assert (await manager.instrumentation.inspect_group("flow")).to_dict()["status"] == "FAIL"
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await manager.instrumentation.clear_group("flow")
    manager.client.set_breakpoints.assert_awaited_once()


@pytest.mark.asyncio
async def test_coincident_tracepoint_does_not_resume_group_breakpoint():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await manager.instrumentation.create_group(
        "flow", breakpoints=[{"file": source, "line": 10, "condition": "value > 0"}]
    )
    await tools["add_tracepoint"](None, source, 10, "value")
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    manager.client.evaluate = AsyncMock(return_value=make_response(body={"result": "1"}))
    manager.client.continue_execution = AsyncMock()

    await manager._check_tracepoint_inner(1)

    manager.client.continue_execution.assert_not_awaited()
    assert manager.state.state == DebugState.STOPPED


@pytest.mark.asyncio
@pytest.mark.parametrize("user_first", [True, False])
@pytest.mark.parametrize("adjusted", [True, False])
async def test_coincident_tracepoint_does_not_resume_user_breakpoint(
    user_first, adjusted
):
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    if user_first:
        await manager.add_breakpoint(source, 10)
    response = await tools["add_tracepoint"](None, source, 10, "value")
    if not user_first:
        await manager.add_breakpoint(source, 10)

    breakpoint = manager.breakpoints.get_for_file(source)[0]
    tracepoint = manager._tracepoint_manager.tracepoints[response["data"]["id"]]
    if adjusted:
        breakpoint.dap_line = 11
        tracepoint.dap_line = 11
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=11 if adjusted else 10, id=1)]
    )
    manager.client.evaluate = AsyncMock(
        return_value=make_response(body={"result": "1"})
    )
    manager.client.continue_execution = AsyncMock()

    await manager._check_tracepoint_inner(1)

    manager.client.continue_execution.assert_not_awaited()
    assert manager.state.state.value == "stopped"


@pytest.mark.asyncio
@pytest.mark.parametrize("remove_user", [True, False])
async def test_removing_coincident_owner_keeps_the_other_breakpoint(remove_user):
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0")
    response = await tools["add_tracepoint"](None, source, 10, "value")

    if remove_user:
        assert await manager.remove_breakpoint(source, 10)
    else:
        await tools["remove_tracepoint"](None, response["data"]["id"])

    remaining = manager.breakpoints.get_for_file(source)
    assert len(remaining) == 1
    if not remove_user:
        assert remaining[0].condition == "value > 0"
    else:
        assert response["data"]["id"] in manager._tracepoint_manager.tracepoints


@pytest.mark.asyncio
async def test_removing_one_tracepoint_keeps_another_at_the_same_location():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    first = await tools["add_tracepoint"](None, source, 10, "value")
    second = await tools["add_tracepoint"](None, source, 10, "other")

    await tools["remove_tracepoint"](None, first["data"]["id"])

    assert len(manager.breakpoints.get_for_file(source)) == 1
    assert second["data"]["id"] in manager._tracepoint_manager.tracepoints


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("condition_value", "should_continue"), [("true", False), ("false", True)]
)
async def test_coincident_conditional_breakpoint_preserves_condition(
    condition_value, should_continue
):
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0")
    await tools["add_tracepoint"](None, source, 10, "value")
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )

    async def evaluate(expression, frame_id, *, admission):
        assert admission()
        result = condition_value if expression == "value > 0" else "42"
        return make_response(
            body={
                "result": result,
                "type": "bool" if expression == "value > 0" else "int",
            }
        )

    manager.client.evaluate = AsyncMock(side_effect=evaluate)
    manager.client.continue_execution = AsyncMock(return_value=make_response())
    await manager._check_tracepoint_inner(1)

    assert manager.client.continue_execution.await_count == int(should_continue)
    assert manager.breakpoints.get_for_file(source)[0].condition == "value > 0"


@pytest.mark.asyncio
async def test_old_tracepoint_evaluation_does_not_resume_a_new_stop():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(expression, frame_id, *, admission):
        assert admission()
        entered.set()
        await release.wait()
        return make_response(body={"result": "42"})

    manager.client.evaluate = AsyncMock(side_effect=evaluate)
    manager.client.continue_execution = AsyncMock()
    pending = asyncio.create_task(manager._check_tracepoint_inner(1))
    await entered.wait()
    manager._on_continued(DAPEvent(1, "continued", {"threadId": 1}))
    manager._on_stopped(DAPEvent(2, "stopped", {"reason": "step", "threadId": 1}))
    release.set()
    await pending

    manager.client.continue_execution.assert_not_awaited()
    assert manager.state.stop_reason == "step"


@pytest.mark.asyncio
async def test_new_breakpoint_during_trace_evaluation_prevents_auto_continue():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    entered, release = asyncio.Event(), asyncio.Event()
    writes = []

    async def evaluate(expression, frame_id, *, admission):
        assert admission()
        entered.set()
        await release.wait()
        return make_response(body={"result": "42"})

    async def continue_if_admitted(thread_id, *, admission):
        if not admission():
            raise RuntimeError("stale")
        writes.append(thread_id)
        return make_response()

    manager.client.evaluate = AsyncMock(side_effect=evaluate)
    manager.client.continue_execution = AsyncMock(side_effect=continue_if_admitted)
    pending = asyncio.create_task(manager._check_tracepoint(1))
    await entered.wait()
    await manager.add_breakpoint(source, 10)
    release.set()
    await pending

    assert writes == []
    assert manager.state.state.value == "stopped"


@pytest.mark.asyncio
async def test_tracepoint_does_not_resume_an_ambiguous_function_breakpoint():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await manager.add_function_breakpoint("Example.Method")
    await tools["add_tracepoint"](None, source, 10, "value")
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    manager.client.evaluate = AsyncMock(
        return_value=make_response(body={"result": "42"})
    )
    manager.client.continue_execution = AsyncMock()

    await manager._check_tracepoint_inner(1)

    manager.client.continue_execution.assert_not_awaited()
    assert manager.state.state.value == "stopped"


@pytest.mark.asyncio
async def test_refused_tracepoint_continue_publishes_original_stop():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager._state.state = DebugState.RUNNING
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    manager.client.evaluate = AsyncMock(return_value=make_response(body={"result": "42"}))

    async def refuse(thread_id, *, admission):
        assert admission()
        return make_response(success=False, message="refused")

    manager.client.continue_execution = AsyncMock(side_effect=refuse)
    await manager._check_tracepoint(1)

    assert manager.state.state == DebugState.STOPPED
    assert manager._execution_event.is_set()


@pytest.mark.asyncio
async def test_refused_tracepoint_removal_retains_user_and_tracepoint():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0")
    trace = await tools["add_tracepoint"](None, source, 10, "value")
    manager._state.state = DebugState.RUNNING
    manager.client.set_breakpoints = AsyncMock(
        return_value=make_response(success=False, message="refused")
    )

    response = await tools["remove_tracepoint"](None, trace["data"]["id"])

    assert "error" in response
    assert trace["data"]["id"] in manager._tracepoint_manager.tracepoints
    assert manager.breakpoints.get_for_file(source)[0].condition == "value > 0"
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    manager.client.continue_execution = AsyncMock()
    await manager._check_tracepoint(1)
    manager.client.continue_execution.assert_not_awaited()
    retry = await tools["remove_tracepoint"](None, trace["data"]["id"])
    assert "error" in retry
    manager.client.set_breakpoints.assert_awaited_once()


@pytest.mark.asyncio
async def test_group_cleanup_does_not_remove_another_tracepoint_after_external_removal():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    first = (await manager.instrumentation.create_group(
        "first", tracepoints=[{"file": source, "line": 10, "expression": "value"}]
    )).to_dict()
    second = (await manager.instrumentation.create_group(
        "second", tracepoints=[{"file": source, "line": 10, "expression": "other"}]
    )).to_dict()
    await tools["remove_tracepoint"](None, first["tracepoints"][0]["id"])

    await manager.instrumentation.clear_group("first")

    assert second["tracepoints"][0]["id"] in manager._tracepoint_manager.tracepoints
    assert len(manager.breakpoints.get_for_file(source)) == 1


@pytest.mark.asyncio
async def test_waiting_user_breakpoint_prevents_tracepoint_continue():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    entered, release, requested = asyncio.Event(), asyncio.Event(), asyncio.Event()
    writes = []

    async def evaluate(expression, frame_id, *, admission):
        assert admission()
        entered.set()
        await release.wait()
        return make_response(body={"result": "42"})

    async def continue_if_admitted(thread_id, *, admission):
        if admission():
            writes.append(thread_id)
        return make_response()

    async def add_user():
        requested.set()
        await manager.add_breakpoint(source, 10)

    manager.client.evaluate = AsyncMock(side_effect=evaluate)
    manager.client.continue_execution = AsyncMock(side_effect=continue_if_admitted)
    manager.client.set_breakpoints = AsyncMock(return_value=make_response(body={"breakpoints": []}))
    pending = asyncio.create_task(manager._check_tracepoint(1))
    await entered.wait()
    async with manager._breakpoint_lock:
        user = asyncio.create_task(add_user())
        await requested.wait()
        release.set()
        await pending
    await user

    assert writes == []
    assert manager.state.state == DebugState.STOPPED


@pytest.mark.asyncio
@pytest.mark.parametrize("cancelled", [False, True])
async def test_uncertain_tracepoint_continue_is_visible_and_not_replayed(cancelled):
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager._state.state = DebugState.RUNNING
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    manager.client.evaluate = AsyncMock(return_value=make_response(body={"result": "42"}))

    async def uncertain(thread_id, *, admission):
        assert admission()
        if cancelled:
            raise asyncio.CancelledError()
        raise BrokenPipeError("uncertain write")

    manager.client.continue_execution = AsyncMock(side_effect=uncertain)
    if cancelled:
        with pytest.raises(asyncio.CancelledError):
            await manager._check_tracepoint_inner(1)
    else:
        await manager._check_tracepoint(1)

    assert manager._execution_event.is_set()
    snapshot = await manager.wait_for_stopped(timeout=1)
    assert snapshot.outcome_unknown is True
    assert manager.state.to_dict()["execState"] == "unknown"
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await manager.continue_execution(1)
    assert manager.client.continue_execution.await_count == 1
    manager.client.set_variable = AsyncMock()
    with pytest.raises(RuntimeError, match="unconfirmed"):
        await manager.set_variable(1, "value", "2")
    manager.client.set_variable.assert_not_awaited()
    manager._on_continued(DAPEvent(3, "continued", {"threadId": 1}))
    assert manager.state.to_dict()["execState"] == "unknown"
    await manager.stop()
    assert manager.state.execution_outcome_unknown is False


@pytest.mark.asyncio
async def test_uncertain_tracepoint_continue_does_not_override_a_new_stop():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    manager.client.evaluate = AsyncMock(return_value=make_response(body={"result": "42"}))

    async def uncertain(thread_id, *, admission):
        assert admission()
        manager._on_continued(DAPEvent(1, "continued", {"threadId": 1}))
        manager._on_stopped(DAPEvent(2, "stopped", {"reason": "step", "threadId": 1}))
        raise BrokenPipeError("uncertain write")

    manager.client.continue_execution = AsyncMock(side_effect=uncertain)
    await manager._check_tracepoint(1)

    assert manager.state.stop_reason == "step"
    assert manager.state.state == DebugState.STOPPED
    assert manager.state.execution_outcome_unknown is False


@pytest.mark.asyncio
@pytest.mark.parametrize("success", [True, False])
async def test_old_breakpoint_sync_does_not_modify_new_session(success):
    manager = make_manager()
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10)
    manager._state.state = DebugState.RUNNING
    manager._active_dap_run = object()
    entered, release = asyncio.Event(), asyncio.Event()

    async def sync(file, breakpoints):
        entered.set()
        await release.wait()
        return make_response(success=success, body={"breakpoints": [{"id": 1, "verified": True}]})

    manager.client.set_breakpoints = AsyncMock(side_effect=sync)
    pending = asyncio.create_task(manager._sync_file_breakpoints(source))
    await entered.wait()
    manager._begin_debuggee_epoch(lifecycle_state=DebugState.INITIALIZING)
    manager._active_dap_run = object()
    manager.breakpoints.update_from_dap(source, [{"id": 99, "verified": True, "line": 10}])
    release.set()

    with pytest.raises(RuntimeError, match="session changed"):
        await pending
    assert manager.breakpoints.get_for_file(source)[0].id == 99
    assert manager._breakpoint_sync_unknown == set()


@pytest.mark.asyncio
async def test_late_continue_error_does_not_override_confirmed_exit():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager._state.state = DebugState.RUNNING
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    manager.client.evaluate = AsyncMock(return_value=make_response(body={"result": "42"}))

    async def fail_after_exit(thread_id, *, admission):
        assert admission()
        manager._on_exited(DAPEvent(1, "exited", {"exitCode": 0}))
        raise BrokenPipeError("late failure")

    manager.client.continue_execution = AsyncMock(side_effect=fail_after_exit)
    await manager._check_tracepoint(1)

    snapshot = await manager.wait_for_stopped(timeout=1)
    assert snapshot.state == DebugState.TERMINATED
    assert snapshot.exit_code == 0
    assert snapshot.outcome_unknown is False


@pytest.mark.asyncio
async def test_tracepoint_removal_can_reconcile_after_session_change():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    first = (await tools["add_tracepoint"](None, source, 10, "value"))["data"]["id"]
    second = (await tools["add_tracepoint"](None, source, 10, "other"))["data"]["id"]
    manager._state.state = DebugState.RUNNING
    manager._active_dap_run = object()
    entered, release = asyncio.Event(), asyncio.Event()

    async def sync(file, breakpoints):
        entered.set()
        await release.wait()
        return make_response(body={"breakpoints": [{"id": 1, "verified": True}]})

    manager.client.set_breakpoints = AsyncMock(side_effect=sync)
    pending = asyncio.create_task(tools["remove_tracepoint"](None, first))
    await entered.wait()
    manager._begin_debuggee_epoch(lifecycle_state=DebugState.INITIALIZING)
    manager._active_dap_run = object()
    release.set()
    first_result = await pending
    assert "error" in first_result

    retry = await tools["remove_tracepoint"](None, first)

    assert retry["data"]["removed"] == first
    assert first not in manager._tracepoint_manager.tracepoints
    assert second in manager._tracepoint_manager.tracepoints
    assert second in manager.breakpoints.get_for_file(source)[0].tracepoint_owners


@pytest.mark.asyncio
async def test_stale_tracepoint_evaluation_is_not_written_to_dap():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    client = manager.client
    stdin = SimpleNamespace(write=MagicMock(), drain=AsyncMock())
    client._process = SimpleNamespace(pid=1, stdin=stdin, returncode=None)
    run = client._run_for_direct_reader()
    loop = asyncio.get_running_loop()

    def reply(data):
        request = json.loads(data.split(b"\r\n\r\n", 1)[1])
        loop.call_soon(client._handle_message, {
            "seq": 1, "type": "response", "request_seq": request["seq"],
            "success": True, "command": request["command"], "body": {"result": "42"},
        }, run)

    stdin.write.side_effect = reply
    entered = asyncio.Event()
    evaluate = client.evaluate

    async def marked_evaluate(expression, frame_id, **kwargs):
        entered.set()
        return await evaluate(expression, frame_id, **kwargs)

    client.evaluate = AsyncMock(side_effect=marked_evaluate)
    async with client._request_lock:
        pending = asyncio.create_task(manager._check_tracepoint(1))
        await entered.wait()
        manager._on_continued(DAPEvent(1, "continued", {"threadId": 1}))
        manager._on_stopped(DAPEvent(2, "stopped", {"reason": "step", "threadId": 1}))
    await asyncio.wait_for(pending, timeout=1)

    stdin.write.assert_not_called()
    assert manager.state.stop_reason == "step"


@pytest.mark.asyncio
async def test_timed_out_trace_evaluation_does_not_auto_continue():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager._state.state = DebugState.RUNNING
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    async def evaluate(expression, frame_id, *, admission):
        assert admission()
        raise asyncio.TimeoutError()

    manager.client.evaluate = AsyncMock(side_effect=evaluate)
    manager.client.continue_execution = AsyncMock()

    await manager._check_tracepoint(1)

    manager.client.continue_execution.assert_not_awaited()
    assert manager.state.execution_outcome_unknown is True
    assert manager._tracepoint_manager.get_log()[0].value == "<timeout>"


@pytest.mark.asyncio
async def test_timed_out_shared_condition_reports_unknown_without_resuming():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await manager.add_breakpoint(source, 10, condition="value > 0")
    await tools["add_tracepoint"](None, source, 10, "value")
    manager._state.state = DebugState.RUNNING
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )

    async def evaluate(expression, frame_id, *, admission):
        assert admission()
        raise asyncio.TimeoutError()

    manager.client.evaluate = AsyncMock(side_effect=evaluate)
    manager.client.continue_execution = AsyncMock()
    await manager._check_tracepoint(1)

    manager.client.continue_execution.assert_not_awaited()
    assert manager.state.execution_outcome_unknown is True


@pytest.mark.asyncio
async def test_timed_out_trace_after_owner_change_remains_unknown():
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager._state.state = DebugState.RUNNING
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(expression, frame_id, *, admission):
        assert admission()
        entered.set()
        await release.wait()
        raise asyncio.TimeoutError()

    manager.client.evaluate = AsyncMock(side_effect=evaluate)
    manager.client.set_breakpoints = AsyncMock(
        return_value=make_response(body={"breakpoints": [{"id": 1, "verified": True}]})
    )
    manager.client.continue_execution = AsyncMock()
    pending = asyncio.create_task(manager._check_tracepoint(1))
    await entered.wait()
    await manager.add_breakpoint(source, 10)
    release.set()
    await pending

    manager.client.continue_execution.assert_not_awaited()
    assert manager.state.execution_outcome_unknown is True


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatched", [False, True])
async def test_cancelled_trace_evaluation_retains_its_dispatch_outcome(dispatched):
    manager = make_manager()
    tools = register_tools(manager)
    source = os.path.realpath(__file__)
    await tools["add_tracepoint"](None, source, 10, "value")
    manager._state.state = DebugState.RUNNING
    manager.get_stack_trace = AsyncMock(
        return_value=[SimpleNamespace(source=source, line=10, id=1)]
    )
    entered, release = asyncio.Event(), asyncio.Event()

    async def evaluate(expression, frame_id, *, admission):
        if dispatched:
            assert admission()
        entered.set()
        await release.wait()
        return make_response(body={"result": "42"})

    manager.client.evaluate = AsyncMock(side_effect=evaluate)
    manager.client.continue_execution = AsyncMock()
    pending = asyncio.create_task(manager._check_tracepoint(1))
    await entered.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    manager.client.continue_execution.assert_not_awaited()
    assert manager.state.execution_outcome_unknown is dispatched
    if not dispatched:
        assert manager.state.state == DebugState.STOPPED
