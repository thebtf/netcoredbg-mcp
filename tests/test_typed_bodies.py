"""Tests for CR-002 DAP event body dataclasses."""

from __future__ import annotations

from netcoredbg_mcp.dap.events import (
    CapabilitiesEventBody,
    ContinuedEventBody,
    InitializedEventBody,
    InvalidatedEventBody,
    LoadedSourceEventBody,
    MemoryEventBody,
    ProcessEventBody,
    ProgressEndEventBody,
    ProgressStartEventBody,
    ProgressUpdateEventBody,
    TerminatedEventBody,
)


def test_initialized_body_round_trip():
    body = InitializedEventBody.from_dict({})

    assert body.to_dict() == {}


def test_continued_body_populates_fields_and_defaults():
    body = ContinuedEventBody.from_dict({"threadId": 7, "allThreadsContinued": False})

    assert body.thread_id == 7
    assert body.all_threads_continued is False
    assert body.to_dict() == {"threadId": 7, "allThreadsContinued": False}
    assert ContinuedEventBody.from_dict({}).all_threads_continued is True


def test_terminated_body_populates_restart_and_defaults():
    body = TerminatedEventBody.from_dict({"restart": {"program": "app.dll"}})

    assert body.restart == {"program": "app.dll"}
    assert body.to_dict() == {"restart": {"program": "app.dll"}}
    assert TerminatedEventBody.from_dict({}).to_dict() == {}


def test_process_body_populates_fields_and_defaults():
    body = ProcessEventBody.from_dict({
        "name": "App",
        "systemProcessId": "123",
        "isLocalProcess": False,
        "startMethod": "attach",
        "pointerSize": 8,
    })

    assert body.name == "App"
    assert body.system_process_id == 123
    assert body.is_local_process is False
    assert body.start_method == "attach"
    assert body.pointer_size == 8
    assert body.to_dict()["systemProcessId"] == 123
    assert ProcessEventBody.from_dict({}).system_process_id is None


def test_capabilities_body_populates_fields_and_defaults():
    body = CapabilitiesEventBody.from_dict({
        "capabilities": {"supportsDisassembleRequest": True},
    })

    assert body.capabilities == {"supportsDisassembleRequest": True}
    assert body.to_dict() == {"capabilities": {"supportsDisassembleRequest": True}}
    assert CapabilitiesEventBody.from_dict({}).capabilities == {}


def test_invalidated_body_populates_fields_and_defaults():
    body = InvalidatedEventBody.from_dict({
        "areas": ["variables", "threads"],
        "threadId": 3,
        "stackFrameId": 9,
    })

    assert body.areas == ["variables", "threads"]
    assert body.thread_id == 3
    assert body.stack_frame_id == 9
    assert body.to_dict() == {
        "areas": ["variables", "threads"],
        "threadId": 3,
        "stackFrameId": 9,
    }
    assert InvalidatedEventBody.from_dict({}).areas == []


def test_loaded_source_body_populates_fields_and_defaults():
    body = LoadedSourceEventBody.from_dict({
        "reason": "changed",
        "source": {"name": "Program.cs", "path": "C:/src/Program.cs"},
    })

    assert body.reason == "changed"
    assert body.source["path"] == "C:/src/Program.cs"
    assert body.to_dict()["reason"] == "changed"
    assert LoadedSourceEventBody.from_dict({}).reason == "new"


def test_progress_start_body_populates_fields_and_defaults():
    body = ProgressStartEventBody.from_dict({
        "progressId": "p1",
        "title": "Loading symbols",
        "requestId": 5,
        "cancellable": True,
        "message": "halfway",
        "percentage": 50,
    })

    assert body.progress_id == "p1"
    assert body.title == "Loading symbols"
    assert body.request_id == 5
    assert body.cancellable is True
    assert body.message == "halfway"
    assert body.percentage == 50.0
    assert body.to_dict()["percentage"] == 50.0
    assert ProgressStartEventBody.from_dict({}).cancellable is False


def test_progress_update_body_populates_fields_and_defaults():
    body = ProgressUpdateEventBody.from_dict({
        "progressId": "p1",
        "message": "almost done",
        "percentage": "90.5",
    })

    assert body.progress_id == "p1"
    assert body.message == "almost done"
    assert body.percentage == 90.5
    assert body.to_dict() == {
        "progressId": "p1",
        "message": "almost done",
        "percentage": 90.5,
    }
    assert ProgressUpdateEventBody.from_dict({"progressId": "p1"}).message is None


def test_progress_end_body_populates_fields_and_defaults():
    body = ProgressEndEventBody.from_dict({"progressId": "p1", "message": "done"})

    assert body.progress_id == "p1"
    assert body.message == "done"
    assert body.to_dict() == {"progressId": "p1", "message": "done"}
    assert ProgressEndEventBody.from_dict({"progressId": "p1"}).message is None


def test_memory_body_populates_fields_and_defaults():
    body = MemoryEventBody.from_dict({
        "memoryReference": "0x1234",
        "offset": 4,
        "count": 16,
    })

    assert body.memory_reference == "0x1234"
    assert body.offset == 4
    assert body.count == 16
    assert body.to_dict() == {
        "memoryReference": "0x1234",
        "offset": 4,
        "count": 16,
    }
    assert MemoryEventBody.from_dict({}).memory_reference == ""
