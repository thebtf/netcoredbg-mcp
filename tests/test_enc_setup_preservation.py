"""Exercise the real EnC setup script against an owned local release archive."""

import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import zipfile

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build-netcoredbg-enc.ps1"
WINDOWS_POWERSHELL = shutil.which("powershell")
POWERSHELL = shutil.which("pwsh") or WINDOWS_POWERSHELL
pytestmark = pytest.mark.skipif(os.name != "nt" or POWERSHELL is None, reason="Windows PowerShell required")


def run_setup(tmp_path: Path, *, include_hook: bool = True, checksum: str | None = None,
              fault: str = "none", build_from_source: bool = False, entries=None,
              shell: str | None = None):
    shell = shell or POWERSHELL
    assert shell is not None
    archive = tmp_path / "fixture.zip"
    with zipfile.ZipFile(archive, "w") as bundle:
        bundle.writestr("netcoredbg.exe", b"new-debugger")
        if include_hook:
            bundle.writestr("ncdbhook.dll", b"new-hook")
        for name, data in entries or []:
            bundle.writestr(name, data)
    actual = hashlib.sha256(archive.read_bytes()).hexdigest()
    wrapper = tmp_path / "wrapper.ps1"
    wrapper.write_text(
        r"""
param($Script, $Archive, $InstallDir, $WorkDir, $ExpectedHash, $Fault, $Source)
function Invoke-WebRequest {
    param($Uri, $OutFile, [switch]$UseBasicParsing)
    Copy-Item -LiteralPath $Archive -Destination $OutFile
}
function Expand-Archive {
    param($LiteralPath, $DestinationPath)
    Microsoft.PowerShell.Archive\Expand-Archive -LiteralPath $LiteralPath -DestinationPath $DestinationPath
    if ($Fault -eq 'config-change') {
        [IO.File]::WriteAllText((Join-Path (Split-Path $InstallDir) 'config.json'), '{"external":true}')
    }
}
function Write-Host {
    param($Object)
    Microsoft.PowerShell.Utility\Write-Host $Object
    if ($Fault -eq 'destination-conflict' -and "$Object" -like 'Previous installation retained:*') {
        [IO.Directory]::CreateDirectory($InstallDir) | Out-Null
        [IO.File]::WriteAllText((Join-Path $InstallDir 'external.txt'), 'external')
    }
}
if ($Source -eq 'yes') {
    function git {
        Add-Content -LiteralPath (Join-Path (Split-Path $Archive) 'git-args.txt') -Value ($args -join '|') -Encoding UTF8
        if ($args[0] -eq 'clone') { [IO.Directory]::CreateDirectory($args[2]) | Out-Null }
        $global:LASTEXITCODE = 0
    }
    function cmake {
        if ($args[0] -eq '--build') {
            if ($Fault -eq 'source-build-fail') { $global:LASTEXITCODE = 1; return }
            [IO.File]::WriteAllText((Join-Path $global:FixtureStage 'netcoredbg.exe'), 'source-debugger')
            if ($Fault -ne 'source-no-hook') {
                [IO.File]::WriteAllText((Join-Path $global:FixtureStage 'ncdbhook.dll'), 'source-hook')
            }
        } else {
            ConvertTo-Json -InputObject @($args) | Set-Content -LiteralPath (Join-Path (Split-Path $Archive) 'cmake-args.json') -Encoding UTF8
            $prefix = @($args | Where-Object { $_ -like '-DCMAKE_INSTALL_PREFIX=*' })[0]
            $global:FixtureStage = $prefix.Substring('-DCMAKE_INSTALL_PREFIX='.Length)
        }
        $global:LASTEXITCODE = 0
    }
    function dotnet { $global:LASTEXITCODE = 0 }
    Set-Item -Path Function:\cl.exe -Value { $global:LASTEXITCODE = 0 }
}
if ($Source -eq 'yes') {
    & $Script -InstallDir $InstallDir -WorkDir $WorkDir -BuildFromSource
} else {
    & $Script -InstallDir $InstallDir -WorkDir $WorkDir -AssetName 'fixture.zip' -AssetSha256 $ExpectedHash
}
""",
        encoding="utf-8",
    )
    result = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-File", str(wrapper),
         str(SCRIPT), str(archive), str(tmp_path / "netcoredbg"), str(tmp_path / "work"), checksum or actual,
         fault, "yes" if build_from_source else "no"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=30,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    return result


def test_success_preserves_existing_installation_and_other_configuration(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"previous-debugger")
    (install / "operator-data.txt").write_text("keep me", encoding="utf-8")
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"other": {"preserve": True}, "netcoredbg": {"version": "old"}}))

    result = run_setup(tmp_path)

    assert result.returncode == 0, result.stderr
    assert (install / "netcoredbg.exe").read_bytes() == b"new-debugger"
    assert (install / "ncdbhook.dll").read_bytes() == b"new-hook"
    backups = list(tmp_path.glob("netcoredbg.backup-*"))
    assert len(backups) == 1
    assert (backups[0] / "netcoredbg.exe").read_bytes() == b"previous-debugger"
    assert (backups[0] / "operator-data.txt").read_text(encoding="utf-8") == "keep me"
    config = json.loads(config_path.read_text(encoding="utf-8-sig"))
    assert config["other"] == {"preserve": True}
    assert Path(config["netcoredbg"]["path"]) == install / "netcoredbg.exe"
    assert not list((tmp_path / "work").glob("enc-setup-*"))
    assert not list(tmp_path.glob("netcoredbg.staging-*"))
    assert not (tmp_path / "config.json.enc-setup.lock").exists()


@pytest.mark.skipif(WINDOWS_POWERSHELL is None, reason="Windows PowerShell is unavailable")
def test_windows_powershell_preserves_existing_data(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    config = tmp_path / "config.json"
    config.write_text('{"date":"2026-10-30T12:34:56.1234567Z","netcoredbg":{}}')

    result = run_setup(tmp_path, shell=WINDOWS_POWERSHELL)

    assert result.returncode == 0, result.stderr
    assert json.loads(config.read_text(encoding="utf-8"))["date"] == "2026-10-30T12:34:56.1234567Z"
    assert (next(tmp_path.glob("netcoredbg.backup-*")) / "netcoredbg.exe").read_bytes() == b"old"


def test_invalid_archive_does_not_replace_existing_installation(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"previous-debugger")

    result = run_setup(tmp_path, include_hook=False)

    assert result.returncode != 0
    assert (install / "netcoredbg.exe").read_bytes() == b"previous-debugger"


def test_unreadable_configuration_is_not_overwritten(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"previous-debugger")
    config_path = tmp_path / "config.json"
    config_path.write_bytes(b"{not-valid-json")

    result = run_setup(tmp_path)

    assert result.returncode != 0
    assert config_path.read_bytes() == b"{not-valid-json"
    assert (install / "netcoredbg.exe").read_bytes() == b"previous-debugger"


def test_checksum_failure_leaves_existing_installation_untouched(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"previous-debugger")

    result = run_setup(tmp_path, checksum="0" * 64)

    assert result.returncode != 0
    assert (install / "netcoredbg.exe").read_bytes() == b"previous-debugger"


@pytest.mark.parametrize("contents", ["null", "[]", '"string"', '{"netcoredbg":42}'])
def test_invalid_config_shape_is_not_replaced(tmp_path, contents):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    config = tmp_path / "config.json"
    config.write_text(contents, encoding="utf-8")
    result = run_setup(tmp_path)
    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == contents
    assert (install / "netcoredbg.exe").read_bytes() == b"old"


def test_preexisting_workspace_files_and_nested_config_survive(tmp_path):
    work = tmp_path / "work"
    work.mkdir()
    (work / "fixture.zip").write_bytes(b"operator archive")
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"netcoredbg": {"custom": {"keep": 1}}}))
    result = run_setup(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (work / "fixture.zip").read_bytes() == b"operator archive"
    assert json.loads(config.read_text(encoding="utf-8-sig"))["netcoredbg"]["custom"] == {"keep": 1}
    assert json.loads(next(tmp_path.glob("config.json.backup-*")).read_text(encoding="utf-8-sig")) == {"netcoredbg": {"custom": {"keep": 1}}}


def test_existing_lock_is_not_removed_or_overwritten(tmp_path):
    lock = tmp_path / "config.json.enc-setup.lock"
    lock.write_text("other owner")
    result = run_setup(tmp_path)
    assert result.returncode != 0
    assert lock.read_text() == "other owner"
    assert not (tmp_path / "netcoredbg").exists()


def test_config_changed_during_download_aborts_before_replacement(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    (tmp_path / "config.json").write_text('{}')
    result = run_setup(tmp_path, fault="config-change")
    assert result.returncode != 0
    assert (install / "netcoredbg.exe").read_bytes() == b"old"
    assert json.loads((tmp_path / "config.json").read_text()) == {"external": True}
    assert not (tmp_path / "config.json.enc-setup.lock").exists()


def test_partial_replacement_retains_backup_and_reports_paths(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    result = run_setup(tmp_path, fault="destination-conflict")
    assert result.returncode != 0
    backup = next(tmp_path.glob("netcoredbg.backup-*"))
    assert (backup / "netcoredbg.exe").read_bytes() == b"old"
    assert (install / "external.txt").read_text() == "external"
    assert str(backup) in result.stdout
    assert "Staged installation:" in result.stdout


def test_archive_traversal_is_rejected(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    result = run_setup(tmp_path, entries=[("../escape.txt", b"escape")])
    assert result.returncode != 0
    assert (install / "netcoredbg.exe").read_bytes() == b"old"
    assert not (tmp_path / "escape.txt").exists()


def test_source_build_uses_fresh_checkout_and_staged_install(tmp_path):
    checkout = tmp_path / "work" / "netcoredbg"
    checkout.mkdir(parents=True)
    (checkout / "local.txt").write_text("local changes")
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    result = run_setup(tmp_path, build_from_source=True)
    assert result.returncode == 0, result.stderr
    assert (checkout / "local.txt").read_text() == "local changes"
    assert (install / "netcoredbg.exe").read_text() == "source-debugger"
    assert (install / "ncdbhook.dll").read_text() == "source-hook"
    assert (next(tmp_path.glob("netcoredbg.backup-*")) / "netcoredbg.exe").read_bytes() == b"old"
    args = json.loads((tmp_path / "cmake-args.json").read_text(encoding="utf-8-sig"))
    assert f"-DNCDB_DOTNET_STARTUP_HOOK={(install / 'ncdbhook.dll').as_posix()}" in args
    stage = next(arg.split('=', 1)[1] for arg in args if arg.startswith('-DCMAKE_INSTALL_PREFIX='))
    assert Path(stage).parent == tmp_path
    assert Path(stage).name.startswith("netcoredbg.staging-")
    assert not Path(stage).exists()
    git_args = (tmp_path / "git-args.txt").read_text(encoding="utf-8-sig")
    assert "checkout|--force" not in git_args
    assert "enc-setup-" in git_args
    assert not list((tmp_path / "work").glob("enc-setup-*"))


@pytest.mark.parametrize("fault", ["source-build-fail", "source-no-hook"])
def test_source_build_failure_keeps_old_install_and_workspace(tmp_path, fault):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    result = run_setup(tmp_path, build_from_source=True, fault=fault)
    assert result.returncode != 0
    assert (install / "netcoredbg.exe").read_bytes() == b"old"
    assert list((tmp_path / "work").glob("enc-setup-*"))
    assert "Download workspace:" in result.stdout


def test_install_reparse_point_is_refused(tmp_path):
    target = tmp_path / "operator-data"
    target.mkdir()
    (target / "netcoredbg.exe").write_bytes(b"old")
    try:
        (tmp_path / "netcoredbg").symlink_to(target, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"Cannot create directory symlink: {exc}")
    result = run_setup(tmp_path)
    assert result.returncode != 0
    assert (target / "netcoredbg.exe").read_bytes() == b"old"
    assert (tmp_path / "netcoredbg").is_symlink()


def test_deep_configuration_is_refused_before_install_replacement(tmp_path):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    config = tmp_path / "config.json"
    original = ('{"other":' + '[' * 110 + '0' + ']' * 110 + '}').encode()
    config.write_bytes(original)
    result = run_setup(tmp_path)
    assert result.returncode != 0
    assert config.read_bytes() == original
    assert (install / "netcoredbg.exe").read_bytes() == b"old"
    assert not list(tmp_path.glob("netcoredbg.backup-*"))


def test_date_strings_and_large_numbers_preserve_exact_json_tokens(tmp_path):
    config = tmp_path / "config.json"
    unrelated = ('"date":"2026-10-30T12:34:56.1234567Z",'
                 '"integer":900719925474099312345678901234567890,'
                 '"decimal":1.2345678901234567890123456789e+100')
    original = '{' + unrelated + ',"netcoredbg":{"extra":{' + unrelated + '},"version":"old"}}'
    config.write_text(original, encoding="utf-8")
    result = run_setup(tmp_path)
    assert result.returncode == 0, result.stderr
    updated = config.read_text(encoding="utf-8-sig")
    assert updated.count(unrelated) == 2
    assert json.loads(updated)["netcoredbg"]["version"] != "old"
    assert next(tmp_path.glob("config.json.backup-*")).read_text(encoding="utf-8-sig") == original


def test_case_distinct_json_keys_are_preserved(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(
        json.dumps({"NETCOREDBG": {"version": "keep"},
                    "netcoredbg": {"Version": "keep", "path": "old"}}),
        encoding="utf-8",
    )
    result = run_setup(tmp_path)
    assert result.returncode == 0, result.stderr
    updated = json.loads(config.read_text(encoding="utf-8"))
    assert updated["NETCOREDBG"] == {"version": "keep"}
    assert updated["netcoredbg"]["Version"] == "keep"
    assert updated["netcoredbg"]["version"] != "keep"


@pytest.mark.parametrize("contents", [
    '{"netcoredbg":{},"netcoredbg":{}}',
    '{"netcoredbg":{"path":"one","path":"two"}}',
    '{"unrelated":01}',
])
def test_ambiguous_or_invalid_json_is_refused(tmp_path, contents):
    install = tmp_path / "netcoredbg"
    install.mkdir()
    (install / "netcoredbg.exe").write_bytes(b"old")
    config = tmp_path / "config.json"
    config.write_text(contents, encoding="utf-8")
    result = run_setup(tmp_path)
    assert result.returncode != 0
    assert config.read_text(encoding="utf-8") == contents
    assert (install / "netcoredbg.exe").read_bytes() == b"old"
