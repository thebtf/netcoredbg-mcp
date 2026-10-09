"""Real stdio startup/EOF checks with filesystem faults confined to owned scratch."""

import asyncio
import json
import os
import sys
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from unittest.mock import patch

from netcoredbg_mcp.windows_process_owner import DrainStatus, WindowsOwnedProcess
from netcoredbg_mcp.ui import temp_manager as tm


INJECTOR = r"""
import os
import sys
import time
from pathlib import Path
root = os.path.normcase(os.path.abspath(os.environ["GC_FIXTURE_ROOT"]))
real_scandir = os.scandir
mode = os.environ.get("GC_FIXTURE_MODE", "")
def scandir(path):
    candidate = os.path.normcase(os.path.abspath(os.fspath(path))) if not isinstance(path, int) else ""
    is_global = candidate == root
    is_namespace = os.path.dirname(candidate) == root and os.path.basename(candidate).startswith("netcoredbg-mcp-sessions-")
    if is_global:
        Path(root, "global-scan").write_text(str(os.getpid()))
    if (is_global and mode == "old-block") or (is_namespace and mode == "block"):
        Path(root, "blocked").write_text(str(os.getpid()))
        while True:
            time.sleep(1)
    if is_global:
        raise RuntimeError("GLOBAL_TEMP_ENUMERATION_FORBIDDEN")
    return real_scandir(path)
os.scandir = scandir
if "--gc-worker" in sys.argv:
    Path(root, "worker-started").write_text(str(os.getpid()))
else:
    Path(root, "server-interpreter").write_text(str(os.getpid()))
"""


@asynccontextmanager
async def cli(tmp_path, mode="block"):
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(INJECTOR)
    scratch = tmp_path / "temp"
    scratch.mkdir()
    with patch.object(tm.tempfile, "gettempdir", return_value=str(scratch)):
        tm._namespace(create=True)
        if mode == "healthy":
            owner = tm.SessionTempManager()
            stale = owner.save_screenshot("stale", b"stale", "stale.png")
            fresh = owner.save_screenshot("fresh", b"fresh", "fresh.png")
            old = time.time() - 14401
            os.utime(stale.parent, (old, old))
            os.close(owner._owner_lease)
            owner._owner_lease = None
            (scratch / "expected-paths.json").write_text(json.dumps([str(stale), str(fresh)]))
            legacy = scratch / "mcp-netcoredbg-legacy"
            legacy.mkdir()
            (legacy / "keep").write_bytes(b"legacy")
    env = dict(os.environ)
    env.update(
        TEMP=str(scratch),
        TMP=str(scratch),
        TMPDIR=str(scratch),
        GC_FIXTURE_ROOT=str(scratch),
        GC_FIXTURE_MODE=mode,
        NETCOREDBG_PATH="D:/Bin/netcoredbg/netcoredbg.exe",
    )
    python = os.environ.get("TEMP_GC_TEST_PYTHON", sys.executable)
    source = Path(__file__).resolve().parents[1] / "src"
    env["PYTHONPATH"] = str(injection) + (
        os.pathsep + str(source) if "TEMP_GC_TEST_PYTHON" not in os.environ else ""
    )
    command = [python, "-m", "netcoredbg_mcp"]
    if "TEMP_GC_TEST_CLI" in os.environ:
        command = [os.environ["TEMP_GC_TEST_CLI"]]
    if os.name == "nt":
        process = await WindowsOwnedProcess.launch(
            generation=object(),
            argv=command,
            cwd=str(tmp_path),
            env=env,
            stdin_mode="pipe",
            capture_process_handles=True,
        )
    else:
        process = await asyncio.create_subprocess_exec(
            *command,
            cwd=tmp_path,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    process.stdout._limit = 16 * 1024 * 1024
    errors = asyncio.create_task(process.stderr.read())
    try:
        yield process, scratch, errors
    finally:
        if process.stdin:
            process.stdin.close()
        if os.name == "nt":
            capture = process._debug_capture
            identities = sorted(capture.handles) if capture is not None else []
            receipt = await process.aclose()
            facts = process.drain_snapshot(receipt)
            facts.update(root_pid=process.pid, captured_pids=identities)
            (tmp_path / "harness-drain.json").write_text(json.dumps(facts, indent=2))
            print("HARNESS_DRAIN " + json.dumps(facts))
            assert receipt.status is DrainStatus.DRAINED, facts
            assert facts["active_processes"] == 0, facts
            assert facts["total_processes"] == facts["retained_exact_handles"], facts
            assert facts["retained_exact_handles"] == facts["signaled_exact_handles"], facts
            assert not facts["unverified_membership"], facts
            assert not facts["handle_probe_failed"], facts
        else:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.wait(), 5)
        stderr = await asyncio.wait_for(errors, 5)
        (tmp_path / "server-stderr.txt").write_bytes(stderr)


async def until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() >= deadline:
            raise TimeoutError("fixture readiness/exit deadline")
        await asyncio.sleep(0.01)


def worker_receipt(stderr, barrier_pid):
    matches = re.findall(r"GC worker drain receipt=(\{[^\n]+\})", stderr)
    assert len(matches) == 1, stderr
    facts = json.loads(matches[0])
    assert facts["total_processes"] == facts["birth_notifications"], facts
    assert facts["root_birth_seen"], facts
    assert facts["live_members_without_handle"] == 0, facts
    facts["barrier_interpreter_pid"] = barrier_pid
    print("PRODUCTION_DRAIN " + json.dumps(facts))
    assert facts["status"] == "drained", facts
    assert facts["active_processes"] == 0, facts
    assert not facts["unverified_membership"], facts
    assert not facts["handle_probe_failed"], facts
    return facts


async def request(process, method, request_id, params=None):
    message = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = params
    process.stdin.write((json.dumps(message) + "\n").encode())
    await process.stdin.drain()
    while True:
        line = await asyncio.wait_for(process.stdout.readline(), 5)
        assert line, "server exited before response"
        response = json.loads(line)
        if response.get("id") == request_id:
            assert "error" not in response, response
            return response["result"]


async def initialize(process):
    result = await request(
        process,
        "initialize",
        1,
        {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "isolated-temp-gc-proof", "version": "1"},
        },
    )
    process.stdin.write(b'{"jsonrpc":"2.0","method":"notifications/initialized"}\n')
    await process.stdin.drain()
    assert result["capabilities"]["experimental"]["x-mux"] == {"sharing": "isolated"}
    assert "tools" in result["capabilities"]
    return result


@pytest.mark.asyncio
async def test_initialize_and_tools_list_never_enumerate_global_temp(tmp_path):
    async with cli(tmp_path, mode="old-block") as (process, scratch, _errors):
        await asyncio.wait_for(initialize(process), 5)
        tools = await request(process, "tools/list", 2)
        names = {tool["name"] for tool in tools["tools"]}
        assert {
            "start_debug",
            "stop_debug",
            "get_call_stack",
            "ui_take_screenshot",
            "run_runtime_smoke",
        } <= names
        assert not (scratch / "global-scan").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("initialized", [False, True])
async def test_blocked_worker_does_not_block_requests_or_eof(tmp_path, initialized):
    async with cli(tmp_path) as (process, scratch, errors):
        await until(lambda: (scratch / "blocked").exists())
        worker_pid = int((scratch / "blocked").read_text())
        assert worker_pid != int((scratch / "server-interpreter").read_text())
        if os.name == "nt":
            assert worker_pid in process._debug_capture.handles
        if initialized:
            started = time.monotonic()
            await initialize(process)
            await request(process, "tools/list", 2)
            await request(process, "ping", 3)
            assert time.monotonic() - started < 5
        started = time.monotonic()
        process.stdin.close()
        await until(lambda: process.returncode is not None, timeout=6)
        assert time.monotonic() - started < 6
        assert process.returncode == 0
        assert await asyncio.wait_for(process.stdout.read(), 1) == b""
        stderr = (await asyncio.wait_for(errors, 1)).decode()
        (tmp_path / "production-drain.json").write_text(
            json.dumps(worker_receipt(stderr, worker_pid), indent=2)
        )
        assert "sweep complete:" not in stderr
        print(
            json.dumps(
                {
                    "initialized": initialized,
                    "worker_pid": worker_pid,
                    "eof_seconds": time.monotonic() - started,
                    "drain": "drained",
                }
            )
        )


@pytest.mark.asyncio
async def test_useful_work_timeout_drains_worker_without_ending_mcp(tmp_path):
    async with cli(tmp_path) as (process, scratch, errors):
        await until(lambda: (scratch / "blocked").exists())
        worker_pid = int((scratch / "blocked").read_text())
        await initialize(process)
        await asyncio.sleep(5.25)
        await request(process, "ping", 2)
        process.stdin.close()
        await until(lambda: process.returncode is not None, timeout=6)
        stderr = (await asyncio.wait_for(errors, 1)).decode()
        assert "useful-work timeout" in stderr
        worker_receipt(stderr, worker_pid)
        assert "sweep complete:" not in stderr


@pytest.mark.asyncio
async def test_healthy_worker_reclaims_abandoned_data_without_stdout_pollution(tmp_path):
    fixture = tmp_path / "fixture"
    fixture.mkdir()
    async with cli(fixture, mode="healthy") as (process, scratch, errors):
        await initialize(process)
        await request(process, "tools/list", 2)
        await until(lambda: (scratch / "worker-started").exists())
        stale, fresh = map(Path, json.loads((scratch / "expected-paths.json").read_text()))
        await until(lambda: not stale.exists())
        assert fresh.read_bytes() == b"fresh"
        assert (scratch / "mcp-netcoredbg-legacy" / "keep").read_bytes() == b"legacy"
        await asyncio.sleep(0.05)
        process.stdin.close()
        await until(lambda: process.returncode is not None, timeout=6)
        assert await asyncio.wait_for(process.stdout.read(), 1) == b""
        stderr = (await asyncio.wait_for(errors, 1)).decode()
        worker_receipt(stderr, int((scratch / "worker-started").read_text()))
        assert "sweep complete: removed=1" in stderr
        assert not (scratch / "global-scan").exists()


@pytest.mark.asyncio
async def test_construction_neither_discovers_nor_launches_gc(monkeypatch):
    from netcoredbg_mcp.server import create_server

    with (
        patch.object(
            tm, "_namespace", side_effect=AssertionError("construction performed discovery")
        ),
        patch.object(
            WindowsOwnedProcess, "launch", side_effect=AssertionError("construction launched GC")
        ),
    ):
        server = create_server()
        names = {tool.name for tool in await server.list_tools()}
        assert "ui_take_screenshot" in names


@pytest.mark.skipif(os.name != "nt", reason="Windows real admission/cancellation proof")
@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["admission", "running"])
async def test_repeated_cancellation_joins_real_worker_owner(tmp_path, monkeypatch, stage):
    injection = tmp_path / "injection"
    injection.mkdir()
    (injection / "sitecustomize.py").write_text(INJECTOR)
    scratch = tmp_path / "temp"
    scratch.mkdir()
    with patch.object(tm.tempfile, "gettempdir", return_value=str(scratch)):
        tm._namespace(create=True)
    for key in ("TEMP", "TMP", "TMPDIR", "GC_FIXTURE_ROOT"):
        monkeypatch.setenv(key, str(scratch))
    monkeypatch.setenv("GC_FIXTURE_MODE", "block")
    source = Path(__file__).resolve().parents[1] / "src"
    monkeypatch.setenv("PYTHONPATH", str(injection) + os.pathsep + str(source))
    admitted = asyncio.Event()
    release_admission = asyncio.Event()
    closing = asyncio.Event()
    release_close = asyncio.Event()
    launch = WindowsOwnedProcess.launch
    close = WindowsOwnedProcess.aclose
    receipts = []

    async def controlled_launch(**kwargs):
        if stage == "admission":
            admitted.set()
            await release_admission.wait()
        process = await launch(**kwargs)
        admitted.set()
        return process

    async def controlled_close(process):
        closing.set()
        await release_close.wait()
        receipt = await close(process)
        facts = process.drain_snapshot(receipt)
        facts["root_pid"] = process.pid
        receipts.append(facts)
        return receipt

    monkeypatch.setattr(WindowsOwnedProcess, "launch", controlled_launch)
    monkeypatch.setattr(WindowsOwnedProcess, "aclose", controlled_close)
    task = asyncio.create_task(tm._gc_worker_supervisor())
    try:
        await asyncio.wait_for(admitted.wait(), 5)
        if stage == "running":
            await until(lambda: (scratch / "blocked").exists())
        task.cancel()
        release_admission.set()
        await asyncio.wait_for(closing.wait(), 5)
        task.cancel()
        release_close.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 6)
        assert len(receipts) == 1
        facts = receipts[0]
        (tmp_path / "production-cancellation-drain.json").write_text(json.dumps(facts, indent=2))
        print("CANCELLATION_DRAIN " + json.dumps(dict(stage=stage, **facts)))
        assert facts["status"] == "drained", facts
        assert facts["active_processes"] == 0, facts
        assert facts["total_processes"] == facts["birth_notifications"], facts
        assert facts["retained_exact_handles"] == facts["signaled_exact_handles"], facts
        assert not facts["unverified_membership"], facts
        assert not facts["handle_probe_failed"], facts
    finally:
        release_admission.set()
        release_close.set()
        if not task.done():
            task.cancel()
            try:
                await asyncio.wait_for(task, 6)
            except asyncio.CancelledError:
                pass
