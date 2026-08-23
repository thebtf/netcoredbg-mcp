"""Exact-build installed-wheel contract for typed BitBlt fallback recovery."""

from __future__ import annotations

import hashlib
import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "WpfSmokeApp"
FIXTURE_DLL = FIXTURE_ROOT / "bin" / "Debug" / "net8.0-windows" / "WpfSmokeApp.dll"
BRIDGE_PROJECT = REPO_ROOT / "bridge" / "FlaUIBridge.csproj"
PUBLISHED_BRIDGE_NAME = "FlaUIBridge.exe"
HOST_ROOT = REPO_ROOT / "tests" / "fixtures" / "TypedBitBltFallbackBridgeHost"
HOST_PROJECT = HOST_ROOT / "TypedBitBltFallbackBridgeHost.csproj"
HOST_EXE = (
    HOST_ROOT / "bin" / "Debug" / "net8.0-windows" / "win-x64" / "TypedBitBltFallbackBridgeHost.exe"
)
HOST_MANAGED_LIBRARY = HOST_EXE.with_suffix(".dll")
HOST_BRIDGE_LIBRARY = HOST_EXE.with_name("FlaUIBridge.dll")
CONSUMER_PROOF = REPO_ROOT / "tests" / "typed_bitblt_fallback_consumer.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _source_identity() -> str:
    sources = (
        BRIDGE_PROJECT,
        REPO_ROOT / "bridge" / "Program.cs",
        REPO_ROOT / "bridge" / "JsonRpcHandler.cs",
        REPO_ROOT / "bridge" / "Commands" / "ScreenshotCommands.cs",
        REPO_ROOT / "bridge" / "Commands" / "ScreenshotCaptureTransport.cs",
        REPO_ROOT / "bridge" / "BridgeTestInternals.cs",
        HOST_PROJECT,
        HOST_ROOT / "Program.cs",
    )
    digest = hashlib.sha256()
    for source in sources:
        digest.update(source.relative_to(REPO_ROOT).as_posix().encode("utf-8"))
        digest.update(b"\0")
        digest.update(source.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def _run(command: list[str], *, timeout: int = 180) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


@pytest.mark.critical
def test_installed_public_strict_bitblt_fallback_has_real_transport_proof(tmp_path: Path) -> None:
    """Run the built wheel against both production and non-shipped bridge binaries."""
    if sys.platform != "win32":
        pytest.fail("typed BitBlt fallback public proof requires Windows")

    debugger_path = os.environ.get("NETCOREDBG_PATH") or shutil.which("netcoredbg")
    assert debugger_path and Path(debugger_path).is_file(), (
        "typed BitBlt fallback public proof requires NETCOREDBG_PATH or netcoredbg on PATH"
    )
    source_identity = _source_identity()

    fixture_build = _run(["dotnet", "build", str(FIXTURE_ROOT), "-c", "Debug"])
    assert fixture_build.returncode == 0, fixture_build.stdout + fixture_build.stderr
    assert FIXTURE_DLL.is_file(), f"fixture build did not produce {FIXTURE_DLL}"

    host_clean = _run(
        ["dotnet", "clean", str(HOST_PROJECT), "-c", "Debug", "-p:BridgeTestHost=true"]
    )
    assert host_clean.returncode == 0, host_clean.stdout + host_clean.stderr
    host_build = _run(
        ["dotnet", "build", str(HOST_PROJECT), "-c", "Debug", "-p:BridgeTestHost=true"]
    )
    assert host_build.returncode == 0, host_build.stdout + host_build.stderr
    assert HOST_EXE.is_file(), f"test bridge host build did not produce {HOST_EXE}"
    assert HOST_MANAGED_LIBRARY.is_file(), f"test bridge host omitted {HOST_MANAGED_LIBRARY}"
    assert HOST_BRIDGE_LIBRARY.is_file(), f"test bridge host omitted {HOST_BRIDGE_LIBRARY}"

    published_bridge_dir = tmp_path / "published-bridge"
    production_publish = _run(
        [
            "dotnet",
            "publish",
            str(BRIDGE_PROJECT),
            "-c",
            "Release",
            "--self-contained",
            "true",
            "-p:PublishSingleFile=true",
            "-o",
            str(published_bridge_dir),
        ]
    )
    assert production_publish.returncode == 0, production_publish.stdout + production_publish.stderr
    production_bridge = published_bridge_dir / PUBLISHED_BRIDGE_NAME
    assert production_bridge.is_file(), f"publish did not produce {production_bridge}"
    assert production_bridge.with_suffix(".pdb").is_file(), "single-file publish omitted bridge PDB"
    assert not production_bridge.with_suffix(".dll").exists(), (
        "single-file publish retained bridge DLL"
    )

    wheel_dir = tmp_path / "wheel"
    wheel_build = _run(["uv", "build", "--wheel", "--out-dir", str(wheel_dir)])
    assert wheel_build.returncode == 0, wheel_build.stdout + wheel_build.stderr
    wheels = sorted(wheel_dir.glob("netcoredbg_mcp-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found: {wheels}"
    wheel = wheels[0]

    consumer_root = tmp_path / "consumer"
    create_consumer = _run(["uv", "venv", "--python", sys.executable, str(consumer_root)])
    assert create_consumer.returncode == 0, create_consumer.stdout + create_consumer.stderr
    consumer_python = consumer_root / "Scripts" / "python.exe"
    consumer_cli = consumer_root / "Scripts" / "netcoredbg-mcp.exe"
    install_wheel = _run(["uv", "pip", "install", "--python", str(consumer_python), str(wheel)])
    assert install_wheel.returncode == 0, install_wheel.stdout + install_wheel.stderr
    assert consumer_cli.is_file(), f"wheel did not install CLI: {consumer_cli}"

    temp_root = tmp_path / "evidence"
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    environment.update(
        {
            "NETCOREDBG_PATH": str(debugger_path),
            "NETCOREDBG_MCP_CONSUMER_CLI": str(consumer_cli),
            "NETCOREDBG_MCP_WPF_ROOT": str(FIXTURE_ROOT),
            "NETCOREDBG_TYPED_BITBLT_PRODUCTION_BRIDGE_PATH": str(production_bridge),
            "NETCOREDBG_TYPED_BITBLT_PRODUCTION_BRIDGE_SHA256": _sha256(production_bridge),
            "NETCOREDBG_TYPED_BITBLT_PRODUCTION_IDENTITY_PATH": str(production_bridge),
            "NETCOREDBG_TYPED_BITBLT_PRODUCTION_IDENTITY_SHA256": _sha256(production_bridge),
            "NETCOREDBG_TYPED_BITBLT_TEST_BRIDGE_PATH": str(HOST_EXE),
            "NETCOREDBG_TYPED_BITBLT_TEST_BRIDGE_SHA256": _sha256(HOST_EXE),
            "NETCOREDBG_TYPED_BITBLT_TEST_HOST_LIBRARY_PATH": str(HOST_MANAGED_LIBRARY),
            "NETCOREDBG_TYPED_BITBLT_TEST_HOST_LIBRARY_SHA256": _sha256(HOST_MANAGED_LIBRARY),
            "NETCOREDBG_TYPED_BITBLT_TEST_LIBRARY_PATH": str(HOST_BRIDGE_LIBRARY),
            "NETCOREDBG_TYPED_BITBLT_TEST_LIBRARY_SHA256": _sha256(HOST_BRIDGE_LIBRARY),
            "NETCOREDBG_TYPED_BITBLT_SOURCE_SHA256": source_identity,
            "NETCOREDBG_TYPED_BITBLT_TEMP_ROOT": str(temp_root),
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
        }
    )
    consumer = subprocess.run(
        [str(consumer_python), str(CONSUMER_PROOF)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=environment,
        timeout=420,
        check=False,
    )
    print(consumer.stdout, end="")
    assert consumer.returncode == 0, consumer.stdout + consumer.stderr
    assert "Typed BitBlt installed consumer evidence:" in consumer.stdout


@pytest.mark.asyncio
async def test_public_consumer_forces_cleanup_after_stop_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("typed_bitblt_fallback_consumer", CONSUMER_PROOF)
    assert spec is not None and spec.loader is not None
    consumer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(consumer)
    calls: list[tuple[str, dict[str, object]]] = []

    async def failing_stop(_session: object, name: str, arguments: dict[str, object]) -> dict:
        calls.append((name, arguments))
        if name == "stop_debug":
            raise RuntimeError("stop failure")
        return {"data": {"terminated": 1}}

    monkeypatch.setattr(consumer, "_call", failing_stop)
    with pytest.raises(RuntimeError, match="stop failure"):
        await consumer._stop_then_force_cleanup(object(), launched=True)

    assert calls == [
        ("stop_debug", {}),
        ("cleanup_processes", {"force": True}),
    ]


@pytest.mark.asyncio
async def test_public_consumer_records_dead_spawned_roles_when_cleanup_count_is_zero(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("typed_bitblt_fallback_consumer", CONSUMER_PROOF)
    assert spec is not None and spec.loader is not None
    consumer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(consumer)
    monkeypatch.setattr(consumer.psutil, "pid_exists", lambda _pid: False)

    proof = await consumer._prove_post_cleanup_liveness(
        {"terminated": 0},
        [
            {"role": "debuggee", "pid": 101},
            {"role": "flaui_bridge", "pid": 202},
        ],
    )

    assert proof == {
        "terminated": 0,
        "post_cleanup_liveness": [
            {"role": "debuggee", "pid": 101, "alive": False},
            {"role": "flaui_bridge", "pid": 202, "alive": False},
        ],
    }


@pytest.mark.asyncio
async def test_public_consumer_rejects_live_spawned_role_after_cleanup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spec = importlib.util.spec_from_file_location("typed_bitblt_fallback_consumer", CONSUMER_PROOF)
    assert spec is not None and spec.loader is not None
    consumer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(consumer)
    monkeypatch.setattr(consumer, "POLL_DEADLINE_SECONDS", 0.0)
    monkeypatch.setattr(consumer.psutil, "pid_exists", lambda _pid: True)

    with pytest.raises(AssertionError, match="post-cleanup liveness"):
        await consumer._prove_post_cleanup_liveness(
            {"terminated": 1}, [{"role": "debuggee", "pid": 303}]
        )
