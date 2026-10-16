"""One nonpublishing permission timing experiment; retire after its single use.

Only probe_permissions calls this driver. The workflow owns the 120-second
process-tree deadline; collector workers keep their existing 45-second limit.
Metadata uses the collector's authenticated transport and safe JSON boundary in
an owned, ten-second child. No log/parser/identity refusal is retried. A second
sample is planned at 60 seconds, with at most one second of scheduler lateness;
a missed schedule is INCOMPLETE, never a reason to extend the observation window.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import stateless_preview_permissions as permissions

_DIRECTORY = Path("permission-timing-diagnostic")
_FIRST = "current-job-permissions-t0.json"
_SECOND = "current-job-permissions-t60.json"
_SUMMARY = "timing-summary.json"
_NEGATIVE_MEANING = (
    "Supported same-current-job readback was not qualified in the observed window; "
    "dependent admission and remote mutation remain unauthorized. "
    "This is not proof of universal unavailability or completion-only semantics."
)


class _IncompleteError(ValueError):
    pass


def _save(directory: Path, summary: dict) -> None:
    value = permissions._safe_record(summary)
    if value is not summary:
        raise _IncompleteError("UNSAFE_DIAGNOSTIC_VALUE")
    temporary = directory / "timing-summary.tmp"
    temporary.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    temporary.replace(directory / _SUMMARY)


def _report(path: Path) -> dict:
    try:
        raw = path.read_bytes()
        if not raw or len(raw) > 64 * 1024:
            raise ValueError
        value = json.loads(raw)
        if (
            not isinstance(value, dict)
            or value.get("meaning") != "RUNNER_REPORTED_GRANT_NOT_TOKEN_INTROSPECTION"
            or value.get("status") not in {"QUALIFIED", "INSUFFICIENT", "UNAVAILABLE"}
            or permissions._safe_record(value) is not value
        ):
            raise ValueError
        return value
    except (OSError, ValueError, UnicodeDecodeError):
        raise _IncompleteError("COLLECTOR_REPORT_INCOMPLETE") from None


def _binding(report: dict, identity: dict) -> dict:
    binding = report.get("binding")
    if not isinstance(binding, dict):
        raise _IncompleteError("COLLECTOR_BINDING_INVALID")
    job_id = binding.get("job_id")
    if (
        type(job_id) is not int
        or job_id <= 0
        or binding != {**identity, "job_id": job_id, "setup_step_position": 0}
    ):
        raise _IncompleteError("COLLECTOR_BINDING_INVALID")
    return binding.copy()


def _metadata_record(first: dict, identity: dict, *, get_json=permissions._get_json) -> dict:
    expected = _binding(first, identity)
    record = {
        "binding_confirmed": False,
        "reason_code": "METADATA_NOT_COMPLETED",
        "facts": {},
        "requests": [],
    }
    try:
        job = get_json(
            f"https://api.github.com/repos/{identity['repository']}/actions/jobs/{expected['job_id']}",
            phase="timing_current_job_after",
            record=record,
            deadline=time.monotonic() + 10.0,
        )
        record["facts"] = {
            name: job.get(name) if type(job.get(name)) is int and job[name] > 0 else None
            for name in ("id", "run_id", "run_attempt")
        }
        record["facts"].update(
            head_sha=(
                job.get("head_sha")
                if isinstance(job.get("head_sha"), str)
                and re.fullmatch(r"[0-9a-fA-F]{40}", job["head_sha"])
                else None
            ),
            name_matches=job.get("name") == identity["job_name"],
            status=(
                job.get("status")
                if job.get("status")
                in ("queued", "in_progress", "completed", "waiting", "pending", "requested")
                else None
            ),
            conclusion_is_null="conclusion" in job and job["conclusion"] is None,
            conclusion=(
                job.get("conclusion")
                if job.get("conclusion")
                in (
                    "success",
                    "failure",
                    "neutral",
                    "cancelled",
                    "skipped",
                    "timed_out",
                    "action_required",
                    "stale",
                    "startup_failure",
                )
                else None
            ),
        )
        selected = permissions.select_current_job(
            {"total_count": 1, "jobs": [job]},
            repository=identity["repository"],
            run_id=identity["run_id"],
            run_attempt=identity["run_attempt"],
            head_sha=identity["head_sha"],
            job_name=identity["job_name"],
        )
        if selected != {"job_id": expected["job_id"], "setup_step_position": 0}:
            raise ValueError
        record.update(binding_confirmed=True, reason_code="SAME_CURRENT_JOB_CONFIRMED")
    except permissions._ProbeError as error:
        record["reason_code"] = str(error)
    except ValueError:
        record["reason_code"] = "CURRENT_JOB_BINDING_CHANGED"
    except Exception:
        record["reason_code"] = "METADATA_INTERNAL_ERROR"
    return permissions._safe_record(record)


def _metadata(directory: Path) -> dict:
    try:
        process = subprocess.run(
            [sys.executable, "-I", str(Path(__file__).resolve()), "--_metadata"],
            cwd=directory,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=10.0,
            check=False,
        )
        if process.returncode != 0 or len(process.stdout) > 64 * 1024:
            raise ValueError
        value = json.loads(process.stdout)
        if not isinstance(value, dict) or permissions._safe_record(value) is not value:
            raise ValueError
        return value
    except subprocess.TimeoutExpired:
        return {
            "binding_confirmed": False,
            "reason_code": "METADATA_DEADLINE_EXCEEDED",
            "facts": {},
            "requests": [],
        }
    except (OSError, ValueError, UnicodeDecodeError):
        return {
            "binding_confirmed": False,
            "reason_code": "METADATA_WORKER_INVALID",
            "facts": {},
            "requests": [],
        }


def _collect(path: Path) -> int:
    return permissions.main(["--output", str(path)])


def _refresh_reports(directory: Path, summary: dict) -> None:
    for sample in summary["samples"]:
        path = directory / sample["report_file"]
        sample["report_sha256"] = (
            hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None
        )


def observe(
    directory: Path,
    identity: dict,
    *,
    collect=_collect,
    metadata=_metadata,
    clock=time.monotonic,
    sleep=time.sleep,
) -> int:
    directory.mkdir(exist_ok=False)
    summary = {
        "diagnostic": "one-off-current-job-permission-timing",
        "observation": "INCOMPLETE",
        "reason_code": "OBSERVATION_NOT_FINISHED",
        "capability": "NOT_ESTABLISHED",
        "meaning": _NEGATIVE_MEANING,
        "promotion_authority": "NONE",
        "identity": identity,
        "binding": None,
        "origin_monotonic_seconds": clock(),
        "deadline_offset_seconds": 120.0,
        "latest_second_start_offset_seconds": 61.0,
        "samples": [],
        "second_sample": "NOT_EXECUTED",
    }
    origin = summary["origin_monotonic_seconds"]
    _save(directory, summary)

    def elapsed() -> float:
        return clock() - origin

    def checkpoint() -> None:
        summary["last_observed_offset_seconds"] = elapsed()
        _refresh_reports(directory, summary)
        _save(directory, summary)

    def sample(filename: str, planned: float) -> tuple[dict, dict]:
        nonlocal origin
        if not summary["samples"]:
            origin = clock()
            summary["origin_monotonic_seconds"] = origin
        if elapsed() >= 120.0:
            raise _IncompleteError("OBSERVATION_DEADLINE_EXCEEDED")
        item = {
            "planned_start_offset_seconds": planned,
            "actual_start_offset_seconds": elapsed(),
            "finished_offset_seconds": None,
            "exit_code": None,
            "report_file": filename,
            "report_sha256": None,
            "terminal_metadata": None,
        }
        summary["samples"].append(item)
        checkpoint()
        item["exit_code"] = collect(directory / filename)
        item["finished_offset_seconds"] = elapsed()
        checkpoint()
        report = _report(directory / filename)
        item.update(
            permission_status=report["status"],
            collector_reason_code=report["reason_code"],
            log_bytes_observed=report.get("log_sha256") is not None,
            parsed_grants=report["grants"],
        )
        if type(item["exit_code"]) is not int or item["exit_code"] != (
            0 if report["status"] == "QUALIFIED" else 1
        ):
            raise _IncompleteError("COLLECTOR_EXIT_REPORT_MISMATCH")
        return report, item

    def after(item: dict) -> None:
        # Reserve the full ten-second metadata budget; never extend the window.
        if elapsed() >= 110.0:
            raise _IncompleteError("METADATA_CANNOT_FINISH_WITHIN_WINDOW")
        item["terminal_metadata"] = {
            "binding_confirmed": False,
            "reason_code": "METADATA_IN_PROGRESS",
            "started_offset_seconds": elapsed(),
        }
        checkpoint()
        observation = metadata(directory)
        item["terminal_metadata"] = {
            **observation,
            "started_offset_seconds": item["terminal_metadata"]["started_offset_seconds"],
            "finished_offset_seconds": elapsed(),
        }
        checkpoint()
        if elapsed() >= 120.0:
            raise _IncompleteError("OBSERVATION_DEADLINE_EXCEEDED")
        if observation.get("binding_confirmed") is not True:
            raise _IncompleteError(observation.get("reason_code", "METADATA_WORKER_INVALID"))

    try:
        first, item = sample(_FIRST, 0.0)
        summary["binding"] = _binding(first, identity)
        after(item)
        last = first
        if (
            first["status"] == "UNAVAILABLE"
            and first["reason_code"] == "CURRENT_JOB_LOGS_UNAVAILABLE"
        ):
            if elapsed() >= 60.0:
                raise _IncompleteError("SECOND_SAMPLE_SCHEDULE_MISSED")
            summary["second_sample"] = "WAITING_FOR_T60"
            checkpoint()
            sleep(max(0.0, 60.0 - elapsed()))
            if elapsed() < 60.0 or elapsed() > 61.0:
                raise _IncompleteError("SECOND_SAMPLE_SCHEDULE_MISSED")
            summary["second_sample"] = "EXECUTED"
            last, item = sample(_SECOND, 60.0)
            after(item)
            if _binding(last, identity) != summary["binding"]:
                raise _IncompleteError("CURRENT_JOB_BINDING_CHANGED")
        summary["observation"] = "COMPLETE"
        summary["reason_code"] = last["reason_code"]
        if last["status"] == "QUALIFIED":
            summary["capability"] = "OBSERVED_QUALIFIED"
            summary["meaning"] = (
                "Runner grants were parsed for the same in-progress job. "
                "This diagnostic grants no promotion authority and establishes no SLA. "
                "A future promote job still requires its own qualified readback."
            )
    except _IncompleteError as error:
        summary["reason_code"] = str(error)
    except Exception:
        summary["reason_code"] = "DIAGNOSTIC_INTERNAL_ERROR"
    checkpoint()
    return (
        0
        if summary["observation"] == "COMPLETE" and summary["capability"] == "OBSERVED_QUALIFIED"
        else 1
    )


def finalize_timeout(directory: Path, process_exit_code: int) -> int:
    path = directory / _SUMMARY
    if path.is_file():
        summary = json.loads(path.read_bytes())
    else:
        directory.mkdir(exist_ok=True)
        identity, _ = permissions._read_context()
        summary = {
            "diagnostic": "one-off-current-job-permission-timing",
            "identity": identity,
            "binding": None,
            "origin_monotonic_seconds": None,
            "deadline_offset_seconds": 120.0,
            "latest_second_start_offset_seconds": 61.0,
            "samples": [],
            "second_sample": "NOT_EXECUTED",
        }
    origin = summary["origin_monotonic_seconds"]
    summary.update(
        observation="INCOMPLETE",
        reason_code="OWNED_PROCESS_HARD_DEADLINE_EXCEEDED",
        capability="NOT_ESTABLISHED",
        meaning=_NEGATIVE_MEANING,
        promotion_authority="NONE",
        observation_process_exit_code=process_exit_code,
        watchdog_finalized_offset_seconds=time.monotonic() - origin if origin is not None else None,
    )
    _refresh_reports(directory, summary)
    _save(directory, summary)
    return 1


def main() -> int:
    if len(sys.argv) == 3 and sys.argv[1] == "--_timeout":
        return finalize_timeout(_DIRECTORY, int(sys.argv[2]))
    if sys.argv[1:] == ["--_metadata"]:
        identity, _ = permissions._read_context()
        record = _metadata_record(_report(Path(_FIRST)), identity)
        print(json.dumps(permissions._safe_record(record), sort_keys=True))
        return 0
    if sys.argv[1:]:
        return 1
    identity, _ = permissions._read_context()
    if identity["job_key"] != "probe_permissions" or identity["run_attempt"] != 1:
        return 1
    return observe(_DIRECTORY, identity)


if __name__ == "__main__":
    raise SystemExit(main())
