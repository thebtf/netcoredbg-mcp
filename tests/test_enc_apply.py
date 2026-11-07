from __future__ import annotations

import asyncio
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

from netcoredbg_mcp.dap.protocol import DAPEvent
from netcoredbg_mcp.enc.applier import ApplyDeltasResult, apply_deltas
from netcoredbg_mcp.enc.compiler import DeltaResult
from netcoredbg_mcp.session.manager import SessionManager
from netcoredbg_mcp.session.state import DebugState, ModuleInfo, SessionState
from netcoredbg_mcp.tools.enc import apply_code_change_to_session


@pytest.mark.asyncio
async def test_apply_deltas_sends_custom_dap_request() -> None:
    client = FakeDapClient(success=True)

    result = await apply_deltas(
        client,
        dll_name="Sample.dll",
        metadata_path="Sample.metadata",
        il_path="Sample.il",
        pdb_path="Sample.pdb",
        line_updates_path=None,
    )

    assert result.success is True
    assert client.requests == [
        (
            "applyDeltas",
            {
                "dllFileName": "Sample.dll",
                "metadataPath": "Sample.metadata",
                "ilPath": "Sample.il",
                "pdbPath": "Sample.pdb",
            },
            30.0,
        )
    ]


@pytest.mark.asyncio
async def test_apply_deltas_handles_empty_response_body() -> None:
    client = FakeDapClient(success=True, empty_body=True)

    result = await apply_deltas(
        client,
        dll_name="Sample.dll",
        metadata_path="Sample.metadata",
        il_path="Sample.il",
        pdb_path="Sample.pdb",
        line_updates_path=None,
    )

    assert result.success is True
    assert result.body == {}


@pytest.mark.asyncio
async def test_apply_code_change_stopped_success_updates_source_and_restores_state(
    tmp_path: Path,
):
    project, source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)
    session.state.modules = [
        ModuleInfo(name="Sample.dll", path=str(tmp_path / "bin" / "Debug" / "Sample.dll"))
    ]
    applied: dict[str, object] = {}
    compiled: dict[str, object] = {}

    async def fake_apply(*args, **_kwargs):
        applied["dll_name"] = _kwargs["dll_name"]
        applied["line_updates_path"] = _kwargs["line_updates_path"]
        session.state_during_apply = session.state.state
        return ApplyDeltasResult(success=True, message=None, body={"ok": True})

    def fake_compiler(**kwargs):
        compiled.update(kwargs)
        return DeltaResult(
            True,
            str(tmp_path / "Sample.il"),
            str(tmp_path / "Sample.metadata"),
            str(tmp_path / "Sample.pdb"),
            (),
            (),
        )

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=fake_compiler,
        apply=fake_apply,
    )

    assert result["data"]["success"] is True
    assert compiled["module_path"] == str(tmp_path / "bin" / "Debug" / "Sample.dll")
    assert applied["dll_name"] == "Sample.dll"
    assert Path(applied["line_updates_path"]).read_bytes() == b"\x00\x00\x00\x00"
    assert "return 2;" in source.read_text(encoding="utf-8")
    assert session.state_during_apply is DebugState.APPLYING_CHANGES
    assert session.transitions == [DebugState.APPLYING_CHANGES, DebugState.STOPPED]


@pytest.mark.asyncio
async def test_apply_code_change_rejects_line_changing_edit_before_compile_or_apply(
    tmp_path: Path,
):
    project, source, netcoredbg = _write_project(tmp_path)
    original_source = source.read_text(encoding="utf-8")
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)
    compiled = False
    applied = False

    def fake_compiler(**_kwargs):
        nonlocal compiled
        compiled = True
        return DeltaResult(True, "Sample.il", "Sample.metadata", "Sample.pdb", (), ())

    async def fake_apply(*_args, **_kwargs):
        nonlocal applied
        applied = True
        return ApplyDeltasResult(success=True, message=None, body={})

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [
            {
                "start_line": 7,
                "end_line": 7,
                "new_text": "        var value = 2;\n        return value;",
            }
        ],
        compiler=fake_compiler,
        apply=fake_apply,
    )

    assert "Line-changing edits are not supported" in result["error"]
    assert compiled is False
    assert applied is False
    assert source.read_text(encoding="utf-8") == original_source
    assert session.transitions == []


@pytest.mark.asyncio
async def test_apply_code_change_treats_empty_new_text_as_blank_same_line(
    tmp_path: Path,
):
    project, source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)
    session.state.modules = [
        ModuleInfo(name="Sample.dll", path=str(tmp_path / "bin" / "Debug" / "Sample.dll"))
    ]

    async def fake_apply(*_args, **_kwargs):
        return ApplyDeltasResult(success=True, message=None, body={"ok": True})

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": ""}],
        compiler=lambda **_kwargs: DeltaResult(
            True,
            str(tmp_path / "Sample.il"),
            str(tmp_path / "Sample.metadata"),
            str(tmp_path / "Sample.pdb"),
            (),
            (),
        ),
        apply=fake_apply,
    )

    assert result["data"]["success"] is True
    assert source.read_text(encoding="utf-8").splitlines()[6] == ""


@pytest.mark.asyncio
async def test_apply_code_change_retains_source_when_apply_deltas_fails(
    tmp_path: Path,
):
    project, source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)
    session.state.modules = [
        ModuleInfo(name="Sample.dll", path=str(tmp_path / "bin" / "Debug" / "Sample.dll"))
    ]

    async def fake_apply(*_args, **_kwargs):
        return ApplyDeltasResult(success=False, message="apply failed", body={})

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=lambda **_kwargs: DeltaResult(
            True,
            str(tmp_path / "Sample.il"),
            str(tmp_path / "Sample.metadata"),
            str(tmp_path / "Sample.pdb"),
            (),
            (),
        ),
        apply=fake_apply,
    )

    assert "apply failed" in result["error"]
    assert result["state"] == DebugState.STOPPED.value
    assert "unknown" in result["error"].lower()
    assert "return 2;" in source.read_text(encoding="utf-8")
    assert session.transitions == [DebugState.APPLYING_CHANGES, DebugState.STOPPED]


@pytest.mark.asyncio
async def test_apply_code_change_running_returns_error(tmp_path: Path):
    project, _source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.RUNNING, project, netcoredbg)

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
    )

    assert "STOPPED" in result["error"]
    assert result["state"] == DebugState.RUNNING.value


@pytest.mark.asyncio
async def test_apply_code_change_without_ncdbhook_returns_setup_error(tmp_path: Path):
    project, _source, netcoredbg = _write_project(tmp_path)
    (netcoredbg.parent / "ncdbhook.dll").unlink()
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
    )

    assert "setup --enc" in result["error"]
    assert result["state"] == DebugState.STOPPED.value


@pytest.mark.asyncio
async def test_apply_code_change_rejects_out_of_range_source_edit(tmp_path: Path):
    project, source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 999, "end_line": 999, "new_text": "        return 2;"}],
    )

    assert "Invalid edit range 999..999" in result["error"]
    assert source.read_text(encoding="utf-8").count("return 1;") == 1


@pytest.mark.asyncio
async def test_apply_code_change_rude_edit_returns_restart_suggestion(tmp_path: Path):
    project, _source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)

    result = await apply_code_change_to_session(
        session,
        project,
        "Sample.cs",
        [{"start_line": 5, "end_line": 5, "new_text": "    private int _value;"}],
        compiler=lambda **_kwargs: DeltaResult(
            False,
            None,
            None,
            None,
            ("rude edit: cannot add field 'Sample._value' to an existing class",),
            (),
        ),
    )

    assert "rude edit" in result["error"]
    assert "restart_debug(rebuild=True)" in result["error"]


@pytest.mark.asyncio
async def test_second_edit_of_same_module_is_refused_before_source_or_dap_mutation(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)
    compile_count = 0
    apply_count = 0

    def compiler(**_kwargs):
        nonlocal compile_count
        compile_count += 1
        return _successful_delta(tmp_path)

    async def apply(*_args, **_kwargs):
        nonlocal apply_count
        apply_count += 1
        return ApplyDeltasResult(success=True, message=None, body={})

    first = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=compiler,
        apply=apply,
    )
    after_first = source.read_bytes()
    second = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 3;"}],
        compiler=compiler,
        apply=apply,
    )

    assert first["data"]["success"] is True
    assert "restart" in second["error"].lower()
    assert source.read_bytes() == after_first
    assert compile_count == apply_count == 1


@pytest.mark.asyncio
async def test_unconfirmed_apply_retains_source_and_blocks_another_delta(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)
    apply_count = 0

    async def apply(*_args, **_kwargs):
        nonlocal apply_count
        apply_count += 1
        return ApplyDeltasResult(success=False, message="controlled refusal", body={})

    first = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=lambda **_kwargs: _successful_delta(tmp_path),
        apply=apply,
    )
    after_first = source.read_bytes()
    second = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 3;"}],
        compiler=lambda **_kwargs: _successful_delta(tmp_path),
        apply=apply,
    )

    assert "unknown" in first["error"].lower()
    assert b"return 2;" in after_first
    assert "restart" in second["error"].lower()
    assert source.read_bytes() == after_first
    assert apply_count == 1


@pytest.mark.asyncio
async def test_concurrent_edits_do_not_dispatch_two_deltas_for_same_module(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)
    first_started = threading.Event()
    second_started = threading.Event()
    release_first = threading.Event()
    compile_count = 0
    apply_count = 0

    def compiler(**_kwargs):
        nonlocal compile_count
        compile_count += 1
        if compile_count == 1:
            first_started.set()
            if not release_first.wait(5):
                raise TimeoutError("controlled compiler was not released")
        else:
            second_started.set()
        return _successful_delta(tmp_path)

    async def apply(*_args, **_kwargs):
        nonlocal apply_count
        apply_count += 1
        return ApplyDeltasResult(success=True, message=None, body={})

    first = asyncio.create_task(
        apply_code_change_to_session(
            session,
            project,
            source,
            [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
            compiler=compiler,
            apply=apply,
        )
    )
    try:
        assert await asyncio.to_thread(first_started.wait, 2)
        second = asyncio.create_task(
            apply_code_change_to_session(
                session,
                project,
                source,
                [{"start_line": 7, "end_line": 7, "new_text": "        return 3;"}],
                compiler=compiler,
                apply=apply,
            )
        )
        await asyncio.to_thread(second_started.wait, 1)
    finally:
        release_first.set()
    results = await asyncio.gather(first, second)

    assert sum(result.get("data", {}).get("success") is True for result in results) == 1
    assert apply_count == 1
    assert "return 2;" in source.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_new_debuggee_epoch_allows_first_delta_again(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)
    apply_count = 0

    async def apply(*_args, **_kwargs):
        nonlocal apply_count
        apply_count += 1
        return ApplyDeltasResult(success=True, message=None, body={})

    first = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=lambda **_kwargs: _successful_delta(tmp_path),
        apply=apply,
    )
    session.state = SessionState(state=DebugState.STOPPED, modules=session.state.modules)
    second = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 3;"}],
        compiler=lambda **_kwargs: _successful_delta(tmp_path),
        apply=apply,
    )

    assert first["data"]["success"] is True
    assert second["data"]["success"] is True
    assert apply_count == 2
    assert "return 3;" in source.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_changed_debuggee_epoch_during_compilation_never_edits_or_dispatches(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)
    original = source.read_bytes()
    started = threading.Event()
    release = threading.Event()
    apply_count = 0

    def compiler(**_kwargs):
        started.set()
        if not release.wait(5):
            raise TimeoutError("controlled compiler was not released")
        return _successful_delta(tmp_path)

    async def apply(*_args, **_kwargs):
        nonlocal apply_count
        apply_count += 1
        return ApplyDeltasResult(success=True, message=None, body={})

    pending = asyncio.create_task(
        apply_code_change_to_session(
            session,
            project,
            source,
            [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
            compiler=compiler,
            apply=apply,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        session.state = SessionState(state=DebugState.STOPPED, modules=session.state.modules)
    finally:
        release.set()
    result = await pending

    assert "error" in result
    assert source.read_bytes() == original
    assert apply_count == 0


@pytest.mark.asyncio
async def test_changed_source_during_compilation_is_not_overwritten(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)
    started = threading.Event()
    release = threading.Event()
    apply_count = 0

    def compiler(**_kwargs):
        started.set()
        if not release.wait(5):
            raise TimeoutError("controlled compiler was not released")
        return _successful_delta(tmp_path)

    async def apply(*_args, **_kwargs):
        nonlocal apply_count
        apply_count += 1
        return ApplyDeltasResult(success=True, message=None, body={})

    pending = asyncio.create_task(
        apply_code_change_to_session(
            session,
            project,
            source,
            [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
            compiler=compiler,
            apply=apply,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        external_content = source.read_text(encoding="utf-8").replace("return 1;", "return 99;")
        source.write_text(external_content, encoding="utf-8")
    finally:
        release.set()
    result = await pending

    assert "error" in result
    assert source.read_text(encoding="utf-8") == external_content
    assert apply_count == 0


@pytest.mark.asyncio
async def test_pending_execution_intent_refuses_enc_before_source_or_dap(tmp_path: Path):
    project, source, netcoredbg = _write_project(tmp_path)
    original = source.read_bytes()
    session = SessionManager(str(netcoredbg), str(project))
    session.state.modules = [ModuleInfo(name="Sample.dll", path=str(tmp_path / "Sample.dll"))]
    session._on_stopped(DAPEvent(seq=1, event="stopped", body={"reason": "pause", "threadId": 1}))
    session.prepare_for_execution()
    dispatched = False

    async def apply(*_args, **_kwargs):
        nonlocal dispatched
        dispatched = True
        return ApplyDeltasResult(success=True, message=None, body={})

    result = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=lambda **_kwargs: _successful_delta(tmp_path),
        apply=apply,
    )

    assert "error" in result
    assert source.read_bytes() == original
    assert dispatched is False


@pytest.mark.asyncio
async def test_stop_before_execution_reply_does_not_admit_enc(tmp_path: Path):
    project, source, netcoredbg = _write_project(tmp_path)
    original = source.read_bytes()
    session = SessionManager(str(netcoredbg), str(project))
    session.state.modules = [ModuleInfo(name="Sample.dll", path=str(tmp_path / "Sample.dll"))]
    session._on_stopped(DAPEvent(seq=1, event="stopped", body={"reason": "pause", "threadId": 1}))
    session.prepare_for_execution()
    started = asyncio.Event()
    reply = asyncio.Event()

    async def delayed_continue(_thread_id):
        started.set()
        await reply.wait()
        return SimpleNamespace(success=True)

    session.client.continue_execution = delayed_continue
    pending = asyncio.create_task(session.continue_execution(1))
    await asyncio.wait_for(started.wait(), 2)
    session._on_stopped(DAPEvent(seq=2, event="stopped", body={"reason": "pause", "threadId": 1}))
    applied = False

    async def apply(*_args, **_kwargs):
        nonlocal applied
        applied = True
        return ApplyDeltasResult(success=True, message=None, body={})

    kwargs = dict(compiler=lambda **_kwargs: _successful_delta(tmp_path), apply=apply)
    try:
        first = await apply_code_change_to_session(
            session,
            project,
            source,
            [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
            **kwargs,
        )
        assert "error" in first and source.read_bytes() == original and not applied
    finally:
        reply.set()
        await pending

    session._on_stopped(DAPEvent(seq=3, event="stopped", body={"reason": "pause", "threadId": 1}))
    second = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        **kwargs,
    )
    assert second["data"]["success"] is True
    assert applied is True


@pytest.mark.asyncio
async def test_execution_is_refused_while_enc_compiles(tmp_path: Path):
    project, source, netcoredbg = _write_project(tmp_path)
    session = SessionManager(str(netcoredbg), str(project))
    session.state.modules = [ModuleInfo(name="Sample.dll", path=str(tmp_path / "Sample.dll"))]
    session._on_stopped(DAPEvent(seq=1, event="stopped", body={"reason": "pause", "threadId": 1}))
    started = threading.Event()
    release = threading.Event()
    dispatched = False

    def compiler(**_kwargs):
        started.set()
        if not release.wait(5):
            raise TimeoutError("controlled compiler was not released")
        return _successful_delta(tmp_path)

    async def continue_execution(_thread_id):
        nonlocal dispatched
        dispatched = True
        return SimpleNamespace(success=True)

    async def apply(*_args, **_kwargs):
        return ApplyDeltasResult(success=True, message=None, body={})

    session.client.continue_execution = continue_execution
    pending = asyncio.create_task(
        apply_code_change_to_session(
            session,
            project,
            source,
            [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
            compiler=compiler,
            apply=apply,
        )
    )
    try:
        assert await asyncio.to_thread(started.wait, 2)
        with pytest.raises(RuntimeError, match="Edit-and-Continue"):
            await session.continue_execution(1)
        assert dispatched is False
    finally:
        release.set()
    result = await pending
    assert result["data"]["success"] is True


@pytest.mark.asyncio
async def test_execution_changes_before_dap_write_prevent_delta_dispatch(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)
    session._execution_revision = 0
    ready = asyncio.Event()
    proceed = asyncio.Event()

    async def delayed_apply(client, **kwargs):
        ready.set()
        await proceed.wait()
        return await apply_deltas(client, **kwargs)

    pending = asyncio.create_task(
        apply_code_change_to_session(
            session,
            project,
            source,
            [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
            compiler=lambda **_kwargs: _successful_delta(tmp_path),
            apply=delayed_apply,
        )
    )
    await asyncio.wait_for(ready.wait(), 2)
    session._execution_revision += 1
    proceed.set()
    result = await pending

    assert "unknown" in result["error"].lower()
    assert session.client.requests == []
    assert "return 2;" in source.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_cancelled_dispatched_delta_blocks_retry_and_retains_source(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)
    dispatched = asyncio.Event()
    apply_count = 0

    async def apply(*_args, **_kwargs):
        nonlocal apply_count
        apply_count += 1
        dispatched.set()
        await asyncio.Event().wait()

    pending = asyncio.create_task(
        apply_code_change_to_session(
            session,
            project,
            source,
            [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
            compiler=lambda **_kwargs: _successful_delta(tmp_path),
            apply=apply,
        )
    )
    await asyncio.wait_for(dispatched.wait(), 2)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    after_cancel = source.read_bytes()
    retry = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 3;"}],
        compiler=lambda **_kwargs: _successful_delta(tmp_path),
        apply=apply,
    )

    assert "restart" in retry["error"].lower()
    assert b"return 2;" in after_cancel
    assert source.read_bytes() == after_cancel
    assert apply_count == 1


@pytest.mark.asyncio
async def test_failed_compilation_does_not_consume_first_delta(tmp_path: Path):
    project, source, session = _ready_enc_session(tmp_path)

    async def apply(*_args, **_kwargs):
        return ApplyDeltasResult(success=True, message=None, body={})

    failed = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=lambda **_kwargs: DeltaResult(False, None, None, None, (), ("compiler refused",)),
        apply=apply,
    )
    retry = await apply_code_change_to_session(
        session,
        project,
        source,
        [{"start_line": 7, "end_line": 7, "new_text": "        return 2;"}],
        compiler=lambda **_kwargs: _successful_delta(tmp_path),
        apply=apply,
    )

    assert "compiler refused" in failed["error"]
    assert retry["data"]["success"] is True
    assert "return 2;" in source.read_text(encoding="utf-8")


def _successful_delta(tmp_path: Path) -> DeltaResult:
    return DeltaResult(
        True,
        str(tmp_path / "Sample.il"),
        str(tmp_path / "Sample.metadata"),
        str(tmp_path / "Sample.pdb"),
        (),
        (),
    )


def _ready_enc_session(tmp_path: Path) -> tuple[Path, Path, FakeSession]:
    project, source, netcoredbg = _write_project(tmp_path)
    session = FakeSession(DebugState.STOPPED, project, netcoredbg)
    session.state.modules = [
        ModuleInfo(name="Sample.dll", path=str(tmp_path / "bin" / "Debug" / "Sample.dll"))
    ]
    return project, source, session


class FakeDapClient:
    def __init__(
        self,
        *,
        success: bool,
        body: dict | None = None,
        empty_body: bool = False,
    ) -> None:
        self.success = success
        self.body = None if empty_body else (body or {"applied": True})
        self.requests: list[tuple[str, dict, float]] = []

    async def send_request(self, command: str, arguments: dict, timeout: float = 30.0):
        self.requests.append((command, arguments, timeout))
        return SimpleNamespace(success=self.success, message=None, body=self.body)

    async def _send_request(
        self, command: str, arguments: dict, timeout: float = 30.0, *, admission=None
    ):
        if admission is not None and not admission():
            raise RuntimeError("Debug session changed before dispatch")
        return await self.send_request(command, arguments, timeout)


class FakeSession:
    def __init__(self, state: DebugState, project_path: Path, netcoredbg_path: Path) -> None:
        self.state = SessionState(state=state)
        self.project_path = str(project_path)
        self.netcoredbg_path = str(netcoredbg_path)
        self.client = FakeDapClient(success=True)
        self._execution_revision = 0
        self.transitions: list[DebugState] = []

    def begin_applying_changes(self) -> DebugState:
        previous = self.state.state
        self.state.state = DebugState.APPLYING_CHANGES
        self.transitions.append(DebugState.APPLYING_CHANGES)
        return previous

    def finish_applying_changes(self, previous_state: DebugState) -> None:
        self.state.state = previous_state
        self.transitions.append(previous_state)


def _write_project(tmp_path: Path) -> tuple[Path, Path, Path]:
    project = tmp_path / "Sample.csproj"
    project.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )
    source = tmp_path / "Sample.cs"
    source.write_text(
        """namespace Fixture;

public class Sample
{
    public int GetValue()
    {
        return 1;
    }
}
""",
        encoding="utf-8",
    )
    netcoredbg = tmp_path / "netcoredbg.exe"
    netcoredbg.write_text("exe", encoding="utf-8")
    (tmp_path / "ncdbhook.dll").write_text("hook", encoding="utf-8")
    return project, source, netcoredbg
