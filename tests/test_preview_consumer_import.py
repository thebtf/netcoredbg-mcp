"""Standalone preview consumer startup must not depend on unrelated packages."""

import os
import subprocess
import sys
from pathlib import Path


def test_preview_consumer_help_ignores_unrelated_scripts_package(tmp_path: Path) -> None:
    fake_site = tmp_path / "fake-site"
    unrelated_package = fake_site / "scripts"
    unrelated_package.mkdir(parents=True)
    package_init = unrelated_package / "__init__.py"
    package_init.write_text('"""An unrelated installed scripts package."""\n', encoding="utf-8")
    foreign_cwd = tmp_path / "foreign-cwd"
    foreign_cwd.mkdir()
    entry = Path(__file__).resolve().parent / "preview" / "validate_preview_artifact.py"
    child_environment = os.environ.copy()
    inherited_pythonpath = child_environment.get("PYTHONPATH")
    child_environment["PYTHONPATH"] = str(fake_site) + (
        os.pathsep + inherited_pythonpath if inherited_pythonpath else ""
    )

    result = subprocess.run(
        [sys.executable, str(entry), "candidate", "--help"],
        cwd=foreign_cwd,
        env=child_environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    diagnostic = (
        f"Preview consumer exited {result.returncode}\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    assert result.returncode == 0, diagnostic
    assert "--references" in result.stdout, diagnostic
    assert "--python-rollback-command" in result.stdout, diagnostic
