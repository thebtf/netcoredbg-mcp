#!/usr/bin/env python3
"""Probe a published Windows preview's embedded .NET runtime before artifact use."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

_TRACE_LIMIT = 4 * 1024 * 1024
_STDERR_LIMIT = 64 * 1024
_PROBE_SECONDS = 15


class RuntimeGuardError(RuntimeError):
    """A bounded, credential-free explanation of refused runtime evidence."""


def validate_runtime_trace(trace: str, expected_runtime: str) -> str:
    trace = trace.replace("\r\n", "\n")
    for component in ("apphost", "hostfxr_main_bundle_startupinfo", "hostpolicy"):
        versions = set(
            re.findall(rf"^--- Invoked {component} \[version: ([^\s\]]+)", trace, re.MULTILINE)
        )
        if versions != {expected_runtime}:
            raise RuntimeGuardError(f"Missing or mismatched {component} runtime evidence.")
    packs = set(
        re.findall(
            r"^Processing package runtimepack\.Microsoft\.NETCore\.App\.Runtime\.([^/\s]+)/([^\s]+)$",
            trace,
            re.MULTILINE,
        )
    )
    if packs != {("win-x64", expected_runtime)}:
        raise RuntimeGuardError("Missing or mismatched win-x64 runtime pack evidence.")
    lines = trace.splitlines()
    if not all(
        marker in lines
        for marker in (
            "Detected Single-File app bundle",
            "Using internal fxr",
            "Using internal hostpolicy",
        )
    ):
        raise RuntimeGuardError("The executable is not an embedded single-file bundle.")
    if (
        not any(
            line.startswith("Executing as a self-contained app as per config file [")
            for line in lines
        )
        or not any(
            line.startswith("Loading deps file... [") and " as framework dependent=0," in line
            for line in lines
        )
        or " as framework dependent=1," in trace
        or "Executing as a framework-dependent app" in trace
    ):
        raise RuntimeGuardError("The executable lacks consistent self-contained evidence.")
    return next(iter(packs))[1]


def probe_runtime(executable: Path, expected_runtime: str, diagnostics_directory: Path) -> str:
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", expected_runtime):
        raise RuntimeGuardError("Expected runtime must be an explicit major.minor.patch version.")
    executable = executable.resolve(strict=True)
    if not executable.is_file():
        raise RuntimeGuardError("The supplied executable is not a file.")
    diagnostics_directory.mkdir(parents=True, exist_ok=False)
    diagnostics_directory = diagnostics_directory.resolve()
    # Do not forward credentials or inherited .NET host overrides to the probe.
    environment = {
        key: value for key, value in os.environ.items() if key.upper() in {"SYSTEMROOT", "WINDIR"}
    }
    secrets = [
        value.encode("utf-8")
        for key, value in os.environ.items()
        if value and re.search(r"TOKEN|PASSWORD|SECRET|CREDENTIAL|(?:^|_)KEY$", key, re.I)
    ]
    with tempfile.TemporaryDirectory(prefix="probe-", dir=diagnostics_directory) as temporary:
        scratch = Path(temporary)
        trace_path = scratch / "host-trace.log"
        stderr_path = scratch / "stderr.log"
        bundle_directory = scratch / "bundle"
        bundle_directory.mkdir()
        trace_path.touch()
        environment.update(
            {
                "COREHOST_TRACE": "1",
                "COREHOST_TRACE_VERBOSITY": "4",
                "COREHOST_TRACEFILE": str(trace_path),
                "DOTNET_BUNDLE_EXTRACT_BASE_DIR": str(bundle_directory),
                "TEMP": temporary,
                "TMP": temporary,
                "TMPDIR": temporary,
            }
        )
        failure = None
        returncode = None
        try:
            with stderr_path.open("wb") as stderr:
                process = subprocess.Popen(
                    [str(executable), "--scope", str(scratch / "missing-root")],
                    cwd=scratch,
                    env=environment,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=stderr,
                )
                deadline = time.monotonic() + _PROBE_SECONDS
                try:
                    while True:
                        if (
                            trace_path.stat().st_size > _TRACE_LIMIT
                            or stderr_path.stat().st_size > _STDERR_LIMIT
                        ):
                            failure = "Runtime probe exceeded its diagnostic size limit."
                            break
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            failure = "Runtime probe exceeded its 15-second deadline."
                            break
                        try:
                            returncode = process.wait(timeout=min(0.05, remaining))
                            break
                        except subprocess.TimeoutExpired:
                            pass
                finally:
                    if process.poll() is None:
                        process.kill()
                    process.wait(timeout=5)
        except OSError:
            failure = "Unable to launch or capture the runtime probe."
        with trace_path.open("rb") as stream:
            trace_bytes = stream.read(_TRACE_LIMIT + 1)
        with stderr_path.open("rb") as stream:
            stderr_bytes = stream.read(_STDERR_LIMIT + 1)
        if len(trace_bytes) > _TRACE_LIMIT or len(stderr_bytes) > _STDERR_LIMIT:
            failure = "Runtime probe exceeded its diagnostic size limit."
        # Preserve bounded original diagnostics, with inherited secret values removed.
        for name, data, limit in (
            ("host-trace.log", trace_bytes, _TRACE_LIMIT),
            ("stderr.log", stderr_bytes, _STDERR_LIMIT),
        ):
            for secret in secrets:
                data = data.replace(secret, b"[REDACTED]")
            (diagnostics_directory / name).write_bytes(data[:limit])
        if failure:
            raise RuntimeGuardError(failure)
        if returncode != 64 or stderr_bytes.strip() != b"PREVIEW_ROOT_INVALID":
            raise RuntimeGuardError(
                "Preview must reject the missing scope with exit 64/PREVIEW_ROOT_INVALID."
            )
        return validate_runtime_trace(
            trace_bytes.decode("utf-8-sig", errors="replace"), expected_runtime
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--expected-runtime", required=True)
    parser.add_argument("--diagnostics-directory", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        actual_runtime = probe_runtime(
            arguments.executable, arguments.expected_runtime, arguments.diagnostics_directory
        )
    except (RuntimeGuardError, OSError, subprocess.SubprocessError) as error:
        detail = str(error) if isinstance(error, RuntimeGuardError) else type(error).__name__
        print(f"Preview runtime guard refused the executable: {detail}", file=sys.stderr)
        return 1
    print(f"Verified embedded win-x64 .NET {actual_runtime} runtime.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
