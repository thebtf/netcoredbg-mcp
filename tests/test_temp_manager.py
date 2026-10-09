"""Artifact ownership, retention and evidence lifecycle in isolated scratch only."""

import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from netcoredbg_mcp.ui import temp_manager as tm


@pytest.fixture(autouse=True)
def isolated_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(tm.tempfile, "gettempdir", lambda: str(tmp_path))


@pytest.fixture
def manager():
    owner = tm.SessionTempManager()
    yield owner
    owner.cleanup_all()


def abandon(manager):
    os.close(manager._owner_lease)
    manager._owner_lease = None
    manager._owner_dir = None


def backdate(path, age=14401, now=None):
    value = (time.time() if now is None else now) - age
    os.utime(path, (value, value))


def test_bundle_bytes_and_closed_session_fencing(manager):
    bundle = manager.save_screenshot_bundle(
        "evidence", b"raw-png", "raw.png", b"crop-png", "crop.png"
    )
    assert bundle is not None
    raw, crop = bundle
    assert raw.read_bytes() == b"raw-png"
    assert crop.read_bytes() == b"crop-png"
    assert manager.get_session_dir("evidence") == raw.parent
    manager.cleanup_session("evidence")
    assert not raw.exists()
    assert not crop.exists()
    assert manager.save_screenshot_bundle("evidence", b"late", "late.png") is None
    assert not raw.parent.exists()


def test_staged_failure_exposes_no_partial_bundle(manager, monkeypatch):
    write = Path.write_bytes

    def deny_crop(path, data):
        if "crop.png" in path.name:
            raise OSError("disk full")
        return write(path, data)

    monkeypatch.setattr(Path, "write_bytes", deny_crop)
    assert (
        manager.save_screenshot_bundle("evidence", b"raw", "raw.png", b"crop", "crop.png") is None
    )
    assert list(manager.get_session_dir("evidence").iterdir()) == []


def test_normal_disposal_removes_only_exact_owned_sessions(manager, tmp_path):
    paths = [manager.save_screenshot(str(i), b"png", "shot.png") for i in range(3)]
    foreign = tmp_path / "foreign"
    foreign.mkdir()
    sentinel = foreign / "keep"
    sentinel.write_bytes(b"foreign")
    manager.cleanup_all()
    assert all(not path.exists() for path in paths)
    assert sentinel.read_bytes() == b"foreign"


def test_allocation_failure_has_no_legacy_fallback(manager, monkeypatch):
    with patch.object(tm, "_namespace", side_effect=OSError("unsafe namespace")):
        assert manager.save_screenshot_bundle("failed", b"raw", "raw.png") is None
    assert manager._owner_lease is None


def test_live_lease_protects_old_artifacts(manager):
    path = manager.save_screenshot("active", b"protected", "shot.png")
    backdate(path.parent)
    assert tm.SessionTempManager.gc_stale() == 0
    assert path.read_bytes() == b"protected"


def test_abandoned_stale_session_removed_but_fresh_sibling_kept(manager):
    stale = manager.save_screenshot("stale", b"stale", "shot.png")
    fresh = manager.save_screenshot("fresh", b"fresh", "shot.png")
    backdate(stale.parent)
    backdate(stale.parent.parent)
    abandon(manager)
    assert tm.SessionTempManager.gc_stale() == 1
    assert not stale.exists()
    assert fresh.read_bytes() == b"fresh"


def test_strict_cutoff_uses_session_not_owner_time(manager, monkeypatch):
    now = 2_000_000_000.0
    exact = manager.save_screenshot("exact", b"exact", "shot.png")
    stale = manager.save_screenshot("stale", b"stale", "shot.png")
    backdate(exact.parent, 14400, now)
    backdate(stale.parent, 14400.25, now)
    abandon(manager)
    monkeypatch.setattr(tm.time, "time", lambda: now)
    assert tm.SessionTempManager.gc_stale() == 1
    assert exact.read_bytes() == b"exact"
    assert not stale.exists()


def test_global_temp_legacy_and_unknown_entries_preserved(manager, tmp_path, monkeypatch):
    path = manager.save_screenshot("old", b"old", "shot.png")
    backdate(path.parent)
    abandon(manager)
    legacy = tmp_path / "mcp-netcoredbg-legacy"
    legacy.mkdir()
    (legacy / "keep").write_bytes(b"legacy")
    backdate(legacy)
    root = tm._namespace()
    unknown = root / "unrelated"
    unknown.mkdir()
    (unknown / "keep").write_bytes(b"unknown")
    real_iterdir = Path.iterdir

    def no_global(path):
        assert path != tmp_path, "global TEMP enumeration"
        return real_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", no_global)
    assert tm.SessionTempManager.gc_stale() == 1
    assert (legacy / "keep").read_bytes() == b"legacy"
    assert (unknown / "keep").read_bytes() == b"unknown"


def test_malformed_and_inaccessible_leases_preserve(manager, monkeypatch):
    path = manager.save_screenshot("old", b"keep", "shot.png")
    backdate(path.parent)
    lease = path.parent.parent / tm._LEASE_NAME
    abandon(manager)
    lease.write_bytes(b"unrecognized")
    assert tm.SessionTempManager.gc_stale() == 0
    lease.write_bytes(tm._LEASE_MARKER)
    real_open = os.open

    def deny_open(path, *args, **kwargs):
        if Path(path) == lease:
            raise PermissionError("lease inaccessible")
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(tm.os, "open", deny_open)
    assert tm.SessionTempManager.gc_stale() == 0
    assert path.read_bytes() == b"keep"


def test_deletion_denial_is_not_counted_and_next_pass_discovers_remainder(manager, monkeypatch):
    first = manager.save_screenshot("first", b"first", "shot.png")
    second = manager.save_screenshot("second", b"second", "shot.png")
    for path in (first, second):
        backdate(path.parent)
    abandon(manager)
    real_remove = tm.shutil.rmtree

    def deny(path, *args, **kwargs):
        raise PermissionError("deletion denied")

    with monkeypatch.context() as local:
        local.setattr(tm.shutil, "rmtree", deny)
        assert tm._collect_stale() == (0, False)
    interrupted = []

    def interrupt_after_one(path, *args, **kwargs):
        if interrupted:
            raise KeyboardInterrupt("controlled interruption")
        real_remove(path, *args, **kwargs)
        interrupted.append(path)

    with monkeypatch.context() as local:
        local.setattr(tm.shutil, "rmtree", interrupt_after_one)
        with pytest.raises(KeyboardInterrupt):
            tm.SessionTempManager.gc_stale()
    assert tm.SessionTempManager.gc_stale() == 1
    assert not first.exists()
    assert not second.exists()


def make_link(link, target):
    if os.name == "nt":
        subprocess.run(
            ["cmd.exe", "/c", "mklink", "/J", str(link), str(target)],
            check=True,
            capture_output=True,
        )
    else:
        link.symlink_to(target, target_is_directory=True)


def test_reparse_or_symlink_session_and_subtree_never_delete_outside(manager, tmp_path):
    path = manager.save_screenshot("old", b"old", "shot.png")
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep"
    sentinel.write_bytes(b"outside")
    make_link(path.parent / "linked", outside)
    backdate(path.parent)
    abandon(manager)
    assert tm.SessionTempManager.gc_stale() == 0
    assert sentinel.read_bytes() == b"outside"
    assert path.read_bytes() == b"old"
    (path.parent / "linked").rmdir() if os.name == "nt" else (path.parent / "linked").unlink()
    path.unlink()
    path.parent.rmdir()
    make_link(path.parent, outside)
    assert tm.SessionTempManager.gc_stale() == 0
    assert sentinel.read_bytes() == b"outside"
    path.parent.rmdir() if os.name == "nt" else path.parent.unlink()


def test_foreign_or_reparse_root_fails_artifact_allocation(tmp_path, manager):
    root = tm._namespace()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_bytes(b"outside")
    make_link(root, outside)
    try:
        assert manager.save_screenshot("bad", b"bad", "shot.png") is None
        with pytest.raises(OSError):
            tm.SessionTempManager.gc_stale()
        assert (outside / "keep").read_bytes() == b"outside"
    finally:
        root.rmdir() if os.name == "nt" else root.unlink()


def test_real_other_process_lease_then_abandoned_reclamation(tmp_path):
    code = """
import json, os, sys, tempfile, time
from netcoredbg_mcp.ui.temp_manager import SessionTempManager
tempfile.tempdir = sys.argv[1]
m = SessionTempManager()
raw, crop = m.save_screenshot_bundle("active", b"raw", "raw.png", b"crop", "crop.png")
fresh = m.save_screenshot("fresh", b"fresh", "fresh.png")
t = time.time() - 14401
os.utime(raw.parent, (t, t))
print(json.dumps([str(raw), str(crop), str(fresh)]), flush=True)
sys.stdin.readline()
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    process = subprocess.Popen(
        [sys.executable, "-c", code, str(tmp_path)],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=env,
    )
    try:
        paths = json.loads(process.stdout.readline())
        raw, crop, fresh = map(Path, paths)
        assert tm.SessionTempManager.gc_stale() == 0
        assert raw.read_bytes() == b"raw"
        assert crop.read_bytes() == b"crop"
        process.stdin.close()
        assert process.wait(timeout=5) == 0
        assert tm.SessionTempManager.gc_stale() == 1
        assert not raw.exists()
        assert not crop.exists()
        assert fresh.read_bytes() == b"fresh"
    finally:
        if process.poll() is None:
            process.kill()
        process.wait(timeout=5)
        process.stdout.close()
        process.stderr.close()


def test_concurrent_collectors_count_one_actual_removal(manager, tmp_path):
    stale = manager.save_screenshot("stale", b"stale", "shot.png")
    backdate(stale.parent)
    abandon(manager)
    code = """
import json, sys, tempfile
from netcoredbg_mcp.ui.temp_manager import SessionTempManager
tempfile.tempdir = sys.argv[1]
print(json.dumps(SessionTempManager.gc_stale()))
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[1] / "src"))
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", code, str(tmp_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        for _ in range(2)
    ]
    try:
        results = [process.communicate(timeout=5) for process in processes]
        assert all(process.returncode == 0 for process in processes), results
        assert sum(json.loads(output) for output, _errors in results) == 1
        assert not stale.exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
            process.wait(timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("with_crop", [False, True])
async def test_public_screenshot_stop_preserves_evidence_lifecycle(
    manager, tmp_path, monkeypatch, with_crop
):
    import io

    from mcp.types import TextContent
    from PIL import Image

    from netcoredbg_mcp import server as server_module
    from netcoredbg_mcp.session.manager import DebugState, SessionManager

    session = SessionManager("/fake/netcoredbg")
    session._temp_manager = manager
    session._session_id = "public-evidence"
    session.state.state = DebugState.RUNNING
    session.state.process_id = 42
    monkeypatch.setattr(server_module, "_session", session)
    server = server_module.create_server()
    image = Image.new("RGB", (8, 6), (240, 240, 240))
    image.putpixel((1, 1), (12, 34, 56))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    raw_png = buffer.getvalue()
    capture_metadata = {
        "method": "PrintWindow",
        "hwnd": 123,
        "client_rect": {"left": 0, "top": 0, "right": 8, "bottom": 6},
        "dpi": 96,
        "dpi_scale": 1.0,
        "physical_width": 8,
        "physical_height": 6,
        "logical_width": 8.0,
        "logical_height": 6.0,
    }
    arguments: dict[str, bool | int] = {"evidence": True}
    if with_crop:
        arguments.update(crop_x=1, crop_y=1, crop_width=3, crop_height=2)
    with (
        patch("netcoredbg_mcp.ui.screenshot.get_hwnd_for_pid", return_value=123),
        patch(
            "netcoredbg_mcp.ui.screenshot.capture_window_evidence",
            return_value=(raw_png, 8, 6, capture_metadata),
        ),
    ):
        content = await server.call_tool("ui_take_screenshot", arguments)
    assert isinstance(content, list), content
    assert isinstance(content[1], TextContent), content
    metadata = json.loads(content[1].text)
    raw = Path(metadata["raw_path"])
    hd = Path(metadata["hd_path"])
    assert raw.is_relative_to(tmp_path)
    assert raw.read_bytes() == raw_png
    assert metadata["raw_sha256"] == hashlib.sha256(raw_png).hexdigest()
    assert metadata["retention"] == "stop_cleanup_or_stale_gc_after_4h"
    with Image.open(hd) as persisted:
        assert persisted.size == (8, 6)
    paths = [raw, hd]
    if with_crop:
        crop = Path(metadata["crop_path"])
        assert metadata["crop_sha256"] == hashlib.sha256(crop.read_bytes()).hexdigest()
        with Image.open(crop) as persisted:
            assert persisted.size == (3, 2)
            assert persisted.getpixel((0, 0)) == (12, 34, 56)
        paths.append(crop)
    else:
        assert "crop_path" not in metadata
    stop_content = await server.call_tool("stop_debug", {})
    assert isinstance(stop_content, list), stop_content
    assert isinstance(stop_content[0], TextContent), stop_content
    stop_result = json.loads(stop_content[0].text)
    assert stop_result.get("data") == {"success": True}, stop_content
    assert stop_result["state"] == "idle", stop_result
    assert session.session_id is None
    assert session.state.state is DebugState.IDLE
    assert all(not path.exists() for path in paths)
    assert manager.save_screenshot_bundle("public-evidence", raw_png, "late.png") is None
    assert not raw.parent.exists()
    facts = {
        "with_crop": with_crop,
        "raw_sha256": metadata["raw_sha256"],
        "crop_sha256": metadata.get("crop_sha256"),
        "removed_paths": [str(path) for path in paths],
        "closed_session_recreation_refused": True,
        "stop_result": stop_result,
    }
    (tmp_path / "public-evidence-stop.json").write_text(json.dumps(facts, indent=2))
    print("PUBLIC_EVIDENCE_STOP " + json.dumps(facts))
