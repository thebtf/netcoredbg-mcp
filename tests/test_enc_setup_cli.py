"""Tests for EnC setup CLI wiring."""

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_parse_args_accepts_setup_enc(monkeypatch):
    from netcoredbg_mcp.__main__ import parse_args

    monkeypatch.setattr("sys.argv", ["netcoredbg-mcp", "setup", "--enc"])

    args = parse_args()

    assert args.command == "setup"
    assert args.enc is True


def test_run_setup_enc_invokes_powershell_script(monkeypatch, tmp_path):
    from netcoredbg_mcp.cli import run_setup_enc

    script = tmp_path / "build-netcoredbg-enc.ps1"
    script.write_text("Write-Host test", encoding="utf-8")
    calls = []

    monkeypatch.setattr("shutil.which", lambda name: "pwsh.exe" if name == "pwsh" else None)

    def fake_run(args):
        calls.append(args)
        return type("Completed", (), {"returncode": 0})()

    monkeypatch.setattr("subprocess.run", fake_run)

    assert run_setup_enc(script_path=script) == 0
    assert calls == [
        [
            "pwsh.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    ]


def test_run_setup_enc_returns_error_when_powershell_launch_fails(
    monkeypatch,
    tmp_path,
    capsys,
):
    from netcoredbg_mcp.cli import run_setup_enc

    script = tmp_path / "build-netcoredbg-enc.ps1"
    script.write_text("Write-Host test", encoding="utf-8")

    monkeypatch.setattr("shutil.which", lambda name: "pwsh.exe" if name == "pwsh" else None)

    def fake_run(_args):
        raise OSError("blocked")

    monkeypatch.setattr("subprocess.run", fake_run)

    assert run_setup_enc(script_path=script) == 1
    assert "Failed to launch PowerShell: blocked" in capsys.readouterr().err


def test_build_netcoredbg_enc_script_contract():
    script = PROJECT_ROOT / "scripts" / "build-netcoredbg-enc.ps1"

    text = script.read_text(encoding="utf-8")

    assert "https://github.com/thebtf/netcoredbg.git" in text
    assert "3.1.3-1062-enc.2" in text
    assert "netcoredbg-win64-3.1.3-1062-enc.2.zip" in text
    assert "208B94AEC38924ACD6580BD8FFE1E87833F9FBDEB53A95E4ED9139ED84DDE139" in text
    assert '.netcoredbg-mcp\\netcoredbg' in text
    assert "Invoke-WebRequest" in text
    assert "Get-FileHash" in text
    assert "Expand-Archive" in text
    assert "Assert-InstalledDebugger" in text
    assert "Save-NetcoredbgConfig" in text
    assert "thebtf/netcoredbg" in text
    assert "BuildFromSource" in text
    assert "git clone" in text
    assert '-DNCDB_DOTNET_STARTUP_HOOK=$ncdbhookCMakePath' in text
    assert "cmake" in text
    assert "dotnet" in text
    assert "cl.exe" in text
    assert "Copy-Item" in text or "--target install" in text
