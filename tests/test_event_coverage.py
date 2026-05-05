"""Tests for CR-002 DAP event coverage invariants."""

from __future__ import annotations

import re

from netcoredbg_mcp.dap.client import DAPClient
from netcoredbg_mcp.dap.protocol import DAPEvent, Events
from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.session.state import DebugState


def _handler_name_for_event(event_name: str) -> str:
    snake = re.sub(r"(?<!^)(?=[A-Z])", "_", event_name).lower()
    return f"_on_{snake}"


def test_all_declared_events_have_handlers():
    manager = SessionManager(netcoredbg_path="/path")
    manager._register_event_handlers()

    for constant_name, event_name in vars(Events).items():
        if not constant_name.isupper():
            continue

        handler_name = _handler_name_for_event(event_name)
        assert hasattr(manager, handler_name), f"{event_name} lacks {handler_name}"

        registered = manager.client._event_handlers.get(event_name, [])
        registered_names = {handler.__name__ for handler in registered}
        assert handler_name in registered_names, f"{event_name} not registered"


def test_unknown_event_warns(caplog):
    client = DAPClient("/path")

    caplog.set_level("WARNING", logger="netcoredbg_mcp.dap.client")
    client._handle_message({
        "seq": 1,
        "type": "event",
        "event": "unknownFutureEvent",
        "body": {"payload": "x" * 250},
    })

    assert "unknownFutureEvent" in caplog.text
    assert "body_size=" in caplog.text
    assert "body_preview=" in caplog.text


def test_capabilities_event_shallow_merges_and_logs(caplog):
    manager = SessionManager(netcoredbg_path="/path")
    manager.client._capabilities = {
        "supportsDisassembleRequest": False,
        "supportsStepInTargetsRequest": True,
    }

    caplog.set_level("INFO", logger="netcoredbg_mcp.session.manager")
    manager._on_capabilities(DAPEvent(
        seq=1,
        event=Events.CAPABILITIES,
        body={"capabilities": {"supportsDisassembleRequest": True}},
    ))

    assert manager.client.capabilities == {
        "supportsDisassembleRequest": True,
        "supportsStepInTargetsRequest": True,
    }
    assert "Capabilities updated" in caplog.text
    assert "supportsDisassembleRequest" in caplog.text


def test_invalidated_event_updates_state_and_logs(caplog):
    manager = SessionManager(netcoredbg_path="/path")

    caplog.set_level("INFO", logger="netcoredbg_mcp.session.manager")
    manager._on_invalidated(DAPEvent(
        seq=1,
        event=Events.INVALIDATED,
        body={"areas": ["variables"], "threadId": 7, "stackFrameId": 9},
    ))

    assert manager.state.last_invalidation is not None
    assert manager.state.last_invalidation.areas == ["variables"]
    assert manager.state.last_invalidation.thread_id == 7
    assert manager.state.last_invalidation.stack_frame_id == 9
    assert "Invalidated" in caplog.text


def test_loaded_source_event_adds_changes_and_removes_state(caplog):
    manager = SessionManager(netcoredbg_path="/path")
    source = {"name": "Program.cs", "path": "C:/src/Program.cs", "sourceReference": 1}

    manager._on_loaded_source(DAPEvent(
        seq=1,
        event=Events.LOADED_SOURCE,
        body={"reason": "new", "source": source},
    ))
    assert len(manager.state.loaded_sources) == 1

    manager._on_loaded_source(DAPEvent(
        seq=2,
        event=Events.LOADED_SOURCE,
        body={"reason": "changed", "source": {**source, "origin": "generated"}},
    ))
    loaded = next(iter(manager.state.loaded_sources.values()))
    assert loaded.origin == "generated"

    manager._on_loaded_source(DAPEvent(
        seq=3,
        event=Events.LOADED_SOURCE,
        body={"reason": "removed", "source": source},
    ))
    assert manager.state.loaded_sources == {}

    caplog.set_level("WARNING", logger="netcoredbg_mcp.session.manager")
    manager._on_loaded_source(DAPEvent(
        seq=4,
        event=Events.LOADED_SOURCE,
        body={"reason": "removed", "source": source},
    ))
    assert "unknown source" in caplog.text


def test_progress_events_update_state_and_unknown_end_warns(caplog):
    manager = SessionManager(netcoredbg_path="/path")

    manager._on_progress_start(DAPEvent(
        seq=1,
        event=Events.PROGRESS_START,
        body={"progressId": "p1", "title": "Loading", "cancellable": True},
    ))
    assert manager.state.active_progress["p1"].title == "Loading"

    manager._on_progress_update(DAPEvent(
        seq=2,
        event=Events.PROGRESS_UPDATE,
        body={"progressId": "p1", "message": "Half", "percentage": 50},
    ))
    assert manager.state.active_progress["p1"].message == "Half"
    assert manager.state.active_progress["p1"].percentage == 50.0

    manager._on_progress_end(DAPEvent(
        seq=3,
        event=Events.PROGRESS_END,
        body={"progressId": "p1"},
    ))
    assert manager.state.active_progress == {}

    caplog.set_level("WARNING", logger="netcoredbg_mcp.session.manager")
    manager._on_progress_end(DAPEvent(
        seq=4,
        event=Events.PROGRESS_END,
        body={"progressId": "missing"},
    ))
    assert "unknown progressId" in caplog.text


def test_memory_event_updates_state():
    manager = SessionManager(netcoredbg_path="/path")

    manager._on_memory(DAPEvent(
        seq=1,
        event=Events.MEMORY,
        body={"memoryReference": "0x1234", "offset": 4, "count": 16},
    ))

    assert manager.state.last_memory_event is not None
    assert manager.state.last_memory_event.memory_reference == "0x1234"
    assert manager.state.last_memory_event.offset == 4
    assert manager.state.last_memory_event.count == 16


def test_typed_existing_handlers_preserve_behavior():
    manager = SessionManager(netcoredbg_path="/path")
    manager.state.state = DebugState.STOPPED
    manager.state.current_thread_id = 42

    manager._on_continued(DAPEvent(
        seq=1,
        event=Events.CONTINUED,
        body={"allThreadsContinued": True},
    ))

    assert manager.state.state == DebugState.RUNNING
    assert manager.state.current_thread_id is None
