"""Short installed desktop smoke plus explicitly selected capture regression."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from tests.test_host_proxy import (
    _dotnet_test_args,
    _dotnet_test_output,
    _test_child_env,
    _test_scratch_directory,
)

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_ROOT = REPO_ROOT / "tests" / "fixtures" / "WpfSmokeApp"
FIXTURE_PROJECT = FIXTURE_ROOT / "WpfSmokeApp.csproj"
FIXTURE_DLL = _dotnet_test_output(FIXTURE_PROJECT, "Debug", "WpfSmokeApp.dll")
BRIDGE_PROJECT = REPO_ROOT / "bridge" / "FlaUIBridge.csproj"
PUBLISHED_BRIDGE_NAME = "FlaUIBridge.exe"
HOST_ROOT = REPO_ROOT / "tests" / "fixtures" / "TypedBitBltFallbackBridgeHost"
HOST_PROJECT = HOST_ROOT / "TypedBitBltFallbackBridgeHost.csproj"
HOST_EXE = _dotnet_test_output(HOST_PROJECT, "Debug", "TypedBitBltFallbackBridgeHost.exe")
HOST_MANAGED_LIBRARY = HOST_EXE.with_suffix(".dll")
HOST_BRIDGE_LIBRARY = HOST_EXE.with_name("FlaUIBridge.dll")
CONSUMER_PROOF = REPO_ROOT / "tests" / "typed_bitblt_fallback_consumer.py"
AVALONIA_PROJECT = REPO_ROOT / "tests" / "fixtures" / "AvaloniaSmokeApp" / "AvaloniaSmokeApp.csproj"
AVALONIA_DLL = _dotnet_test_output(AVALONIA_PROJECT, "Debug", "AvaloniaSmokeApp.dll")


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
        env=_test_child_env(),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def run_two_application_verification(tmp_path: Path, *, extended_gui: bool = False) -> dict:
    """Install one wheel; run short core journeys unless regression is explicit."""
    if sys.platform != "win32":
        pytest.fail("installed two-application GUI proof requires Windows")

    debugger_path = os.environ.get("NETCOREDBG_PATH") or shutil.which("netcoredbg")
    assert debugger_path and Path(debugger_path).is_file(), (
        "installed two-application GUI proof requires NETCOREDBG_PATH or netcoredbg on PATH"
    )
    source_identity = _source_identity() if extended_gui else None
    run_root = os.environ.get("NETCOREDBG_TWO_GUI_RUN_ROOT")
    if run_root:
        root = Path(run_root).resolve()
        assert root.is_relative_to((REPO_ROOT / ".agent").resolve()), root
        root.mkdir(parents=True, exist_ok=True)
        prefix = "gui-regression-" if extended_gui else "core-smoke-"
        tmp_path = Path(tempfile.mkdtemp(prefix=prefix, dir=root))
    else:
        tmp_path = _test_scratch_directory(f"{tmp_path.name}-")

    fixture_build = _run(
        ["dotnet", "build", str(FIXTURE_PROJECT), "-c", "Debug", *_dotnet_test_args("Debug")]
    )
    assert fixture_build.returncode == 0, fixture_build.stdout + fixture_build.stderr
    assert FIXTURE_DLL.is_file(), f"fixture build did not produce {FIXTURE_DLL}"
    if not extended_gui:
        avalonia_build = _run(
            ["dotnet", "build", str(AVALONIA_PROJECT), "-c", "Debug", *_dotnet_test_args("Debug")]
        )
        assert avalonia_build.returncode == 0, avalonia_build.stdout + avalonia_build.stderr
        assert AVALONIA_DLL.is_file(), f"fixture build did not produce {AVALONIA_DLL}"

    if extended_gui:
        host_clean = _run(
            [
                "dotnet",
                "clean",
                str(HOST_PROJECT),
                "-c",
                "Debug",
                "-p:BridgeTestHost=true",
                *_dotnet_test_args("Debug"),
            ]
        )
        assert host_clean.returncode == 0, host_clean.stdout + host_clean.stderr
        host_build = _run(
            [
                "dotnet",
                "build",
                str(HOST_PROJECT),
                "-c",
                "Debug",
                "-p:BridgeTestHost=true",
                *_dotnet_test_args("Debug"),
            ]
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
            *_dotnet_test_args("Release"),
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
    environment = {
        key: value
        for key, value in _test_child_env().items()
        if key != "PYTHONPATH"
        and not key.startswith(("NETCOREDBG_GUI_", "NETCOREDBG_TYPED_BITBLT_", "NETCOREDBG_TEST_"))
    }
    environment.update(
        {
            "NETCOREDBG_PATH": str(debugger_path),
            "NETCOREDBG_MCP_CONSUMER_CLI": str(consumer_cli),
            "NETCOREDBG_MCP_WPF_ROOT": str(FIXTURE_ROOT),
            "NETCOREDBG_MCP_WPF_PROGRAM": str(FIXTURE_DLL),
            "NETCOREDBG_GUI_SCOPE": "regression" if extended_gui else "core",
            "NETCOREDBG_GUI_AVALONIA_PROGRAM": str(AVALONIA_DLL),
            "NETCOREDBG_TYPED_BITBLT_PRODUCTION_BRIDGE_PATH": str(production_bridge),
            "NETCOREDBG_TYPED_BITBLT_PRODUCTION_BRIDGE_SHA256": _sha256(production_bridge),
            "NETCOREDBG_TYPED_BITBLT_PRODUCTION_IDENTITY_PATH": str(production_bridge),
            "NETCOREDBG_TYPED_BITBLT_PRODUCTION_IDENTITY_SHA256": _sha256(production_bridge),
            "NETCOREDBG_TYPED_BITBLT_TEMP_ROOT": str(temp_root),
            "TEMP": str(temp_root),
            "TMP": str(temp_root),
        }
    )
    if extended_gui:
        assert isinstance(source_identity, str), "regression source identity is required"
        environment.update(
            {
                "NETCOREDBG_TYPED_BITBLT_TEST_BRIDGE_PATH": str(HOST_EXE),
                "NETCOREDBG_TYPED_BITBLT_TEST_BRIDGE_SHA256": _sha256(HOST_EXE),
                "NETCOREDBG_TYPED_BITBLT_TEST_HOST_LIBRARY_PATH": str(HOST_MANAGED_LIBRARY),
                "NETCOREDBG_TYPED_BITBLT_TEST_HOST_LIBRARY_SHA256": _sha256(HOST_MANAGED_LIBRARY),
                "NETCOREDBG_TYPED_BITBLT_TEST_LIBRARY_PATH": str(HOST_BRIDGE_LIBRARY),
                "NETCOREDBG_TYPED_BITBLT_TEST_LIBRARY_SHA256": _sha256(HOST_BRIDGE_LIBRARY),
                "NETCOREDBG_TYPED_BITBLT_SOURCE_SHA256": source_identity,
            }
        )
    consumer = subprocess.run(
        [str(consumer_python), str(CONSUMER_PROOF)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=environment,
        timeout=1800 if extended_gui else 180,
        check=False,
    )
    print(consumer.stdout, end="")
    (tmp_path / "consumer.stdout.txt").write_text(consumer.stdout, encoding="utf-8")
    (tmp_path / "consumer.stderr.txt").write_text(consumer.stderr, encoding="utf-8")
    assert consumer.returncode == 0, consumer.stdout + consumer.stderr
    prefix = (
        "Typed BitBlt installed consumer evidence: "
        if extended_gui
        else "Two-application GUI evidence: "
    )
    records = [
        line[len(prefix) :] for line in consumer.stdout.splitlines() if line.startswith(prefix)
    ]
    assert len(records) == 1, consumer.stdout
    if extended_gui:
        captures = json.loads(records[0])
        assert len(captures) == 9, captures
        evidence = {"mode": "regression", "status": "PASS", "capture_cases": captures}
    else:
        evidence = json.loads(records[0])
        assert evidence["mode"] == "core" and evidence["status"] == "PASS", evidence
        assert len(evidence["gui"]["cases"]) == 2, evidence
        assert {row["framework"] for row in evidence["gui"]["cases"]} == {"WPF", "Avalonia"}, (
            evidence
        )
        assert all(row["status"] == "PASS" for row in evidence["gui"]["cases"]), evidence
        assert len(evidence["applications"]) == 2, evidence
        assert all(
            app["launches"] == 1 and app["terminated"] for app in evidence["applications"]
        ), evidence
    (tmp_path / "two-application-evidence.json").write_text(
        json.dumps(evidence, indent=2, sort_keys=True), encoding="utf-8"
    )
    return evidence


@pytest.mark.gui_regression
def test_installed_public_strict_bitblt_fallback_has_real_transport_proof(tmp_path: Path) -> None:
    evidence = run_two_application_verification(tmp_path, extended_gui=True)
    assert len(evidence["capture_cases"]) == 9
    assert evidence["status"] == "PASS"


@pytest.mark.critical
@pytest.mark.parametrize("framework", ["WPF", "Avalonia"])
def test_installed_public_core_gui_journey(
    framework: str,
    two_application_gui_evidence: dict,
) -> None:
    evidence = two_application_gui_evidence
    case = next(row for row in evidence["gui"]["cases"] if row["framework"] == framework)
    assert evidence["mode"] == "core" and case["status"] == "PASS", evidence
    assert case["invoke_state"] == "Invoked 1x", case
    assert case["screenshot"]["width"] > 0 and case["screenshot"]["height"] > 0, case
    assert case["application"]["launches"] == 1 and case["application"]["terminated"], case
    assert len(case["cleanup"]["post_cleanup_liveness"]) == 3, case
    assert all(not row["alive"] for row in case["cleanup"]["post_cleanup_liveness"]), case


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


def test_core_screenshot_decodes_real_image_without_retaining_payload(tmp_path: Path) -> None:
    import base64
    import hashlib
    import io
    import json

    from mcp.types import CallToolResult, ImageContent, TextContent
    from PIL import Image
    from tests import typed_bitblt_fallback_consumer as consumer

    encoded = io.BytesIO()
    Image.new("RGB", (8, 6), (18, 192, 222)).save(encoded, format="WEBP")
    image_bytes = encoded.getvalue()
    result = CallToolResult(
        isError=False,
        content=[
            ImageContent(
                type="image", mimeType="image/webp", data=base64.b64encode(image_bytes).decode()
            ),
            TextContent(
                type="text", text=json.dumps({"preview_width": 8, "width": 8, "height": 6})
            ),
        ],
    )

    evidence = consumer._core_screenshot(result, tmp_path)

    assert (evidence["width"], evidence["height"]) == (8, 6)
    assert Path(evidence["path"]).read_bytes() == image_bytes
    assert evidence["sha256"] == hashlib.sha256(image_bytes).hexdigest()
    assert base64.b64encode(image_bytes).decode() not in json.dumps(evidence)


def test_core_screenshot_rejects_non_image_bytes(tmp_path: Path) -> None:
    import base64
    import json

    from mcp.types import CallToolResult, ImageContent, TextContent
    from PIL import UnidentifiedImageError
    from tests import typed_bitblt_fallback_consumer as consumer

    result = CallToolResult(
        isError=False,
        content=[
            ImageContent(
                type="image", mimeType="image/webp", data=base64.b64encode(b"not an image").decode()
            ),
            TextContent(
                type="text", text=json.dumps({"preview_width": 8, "width": 8, "height": 6})
            ),
        ],
    )
    with pytest.raises(UnidentifiedImageError):
        consumer._core_screenshot(result, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_short_installed_setup_never_builds_or_configures_capture_testhost(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json
    import sys

    from tests.critical import test_typed_bitblt_fallback_public as producer

    monkeypatch.setattr(
        producer, "sys", SimpleNamespace(platform="win32", executable=sys.executable)
    )

    commands = []
    fixture = tmp_path / "fixture.dll"
    fixture.write_bytes(b"fixture")
    monkeypatch.setattr(producer, "FIXTURE_DLL", fixture)
    monkeypatch.setattr(producer, "AVALONIA_DLL", fixture)
    monkeypatch.setattr(producer, "_test_scratch_directory", lambda _prefix: tmp_path / "run")
    monkeypatch.setattr(
        producer, "_source_identity", lambda: pytest.fail("core needs no testhost identity")
    )
    monkeypatch.setenv("NETCOREDBG_PATH", str(fixture))
    monkeypatch.delenv("NETCOREDBG_TWO_GUI_RUN_ROOT", raising=False)

    def run(command, **_kwargs):
        commands.append(command)
        if command[:2] == ["dotnet", "publish"]:
            folder = Path(command[-1])
            folder.mkdir(parents=True)
            (folder / "FlaUIBridge.exe").write_bytes(b"bridge")
            (folder / "FlaUIBridge.pdb").write_bytes(b"pdb")
        elif command[:2] == ["uv", "build"]:
            folder = Path(command[-1])
            folder.mkdir(parents=True)
            (folder / "netcoredbg_mcp-test.whl").write_bytes(b"wheel")
        elif command[:2] == ["uv", "venv"]:
            folder = Path(command[-1]) / "Scripts"
            folder.mkdir(parents=True)
            (folder / "netcoredbg-mcp.exe").write_bytes(b"cli")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    evidence = {
        "mode": "core",
        "status": "PASS",
        "gui": {"cases": [{"framework": name, "status": "PASS"} for name in ("WPF", "Avalonia")]},
        "applications": [{"launches": 1, "terminated": True} for _ in range(2)],
    }

    def consumer_run(command, *, env, **_kwargs):
        assert env["NETCOREDBG_GUI_SCOPE"] == "core"
        assert not any(name.startswith("NETCOREDBG_TYPED_BITBLT_TEST_") for name in env)
        assert "NETCOREDBG_GUI_NORMAL" not in env
        assert Path(command[1]).is_absolute()
        return SimpleNamespace(
            returncode=0,
            stdout="Two-application GUI evidence: " + json.dumps(evidence) + "\n",
            stderr="",
        )

    monkeypatch.setattr(producer, "_run", run)
    monkeypatch.setattr(producer.subprocess, "run", consumer_run)
    assert producer.run_two_application_verification(tmp_path)["status"] == "PASS"
    assert len([c for c in commands if c[0] == "dotnet"]) == 3
    assert all(str(producer.HOST_PROJECT) not in c and "clean" not in c for c in commands)


@pytest.mark.asyncio
async def test_core_dispatch_never_enters_calibration_or_manual_regression(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from tests import typed_bitblt_fallback_consumer as consumer

    calls = []

    async def core():
        calls.append("core")

    monkeypatch.setenv("NETCOREDBG_GUI_SCOPE", "core")
    monkeypatch.setattr(consumer, "_run_core_verification", core)
    monkeypatch.setattr(
        consumer,
        "_enable_per_monitor_dpi_coordinates",
        lambda: pytest.fail("normal core must not enter the native calibration matrix"),
    )
    await consumer.main()
    assert calls == ["core"]


@pytest.mark.asyncio
async def test_short_consumer_failure_never_retries_or_starts_next_app(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture,
) -> None:
    from tests import typed_bitblt_fallback_consumer as consumer

    calls = []

    async def fail(**kwargs):
        calls.append(kwargs["framework"])
        raise AssertionError("retained core failure")

    monkeypatch.setattr(consumer, "_run_core_case", fail)
    monkeypatch.setattr(consumer, "_environment", lambda name: str(tmp_path / name))
    monkeypatch.setattr(
        consumer, "_verify_core_bridge", lambda: (tmp_path / "bridge.exe", "digest")
    )
    with pytest.raises(AssertionError, match="retained core failure"):
        await consumer._run_core_verification()
    assert calls == ["WPF"]
    assert '"status": "FAIL"' in capsys.readouterr().out
