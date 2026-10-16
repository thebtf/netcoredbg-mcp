"""Post-hoc regression promotion for the existing one-use timing diagnostic.

Adapted from .agent/runs/a1-artifact-20261016/permission-timing-diagnostic-selfcheck.py
after the driver was implemented; this suite makes no historical RED/GREEN claim.
Controlled collector bytes and HTTP responses exercise the real observer, metadata
JSON parser, and same-job selector without network, child processes, or real sleeps.
The physical Windows --timeout-smoke recipe remains private and opt-in.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from scripts import stateless_preview_permission_timing_diagnostic as driver

IDENTITY = {
    "repository": "thebtf/netcoredbg-mcp",
    "run_id": 101,
    "run_attempt": 1,
    "head_sha": "a" * 40,
    "job_key": "probe_permissions",
    "workflow_sha": "a" * 40,
    "job_name": "Probe current job permissions",
}
BINDING = {**IDENTITY, "job_id": 202, "setup_step_position": 0}
JOB = {
    "id": 202,
    "name": IDENTITY["job_name"],
    "run_id": 101,
    "run_attempt": 1,
    "head_sha": "a" * 40,
    "status": "in_progress",
    "conclusion": None,
    "url": "https://api.github.com/repos/thebtf/netcoredbg-mcp/actions/jobs/202",
    "run_url": "https://api.github.com/repos/thebtf/netcoredbg-mcp/actions/runs/101",
    "steps": [{"name": "Set up job", "status": "completed", "conclusion": "success"}],
}


def report(reason="CURRENT_JOB_LOGS_UNAVAILABLE", *, status="UNAVAILABLE", job_id=202):
    value = {
        "meaning": "RUNNER_REPORTED_GRANT_NOT_TOKEN_INTROSPECTION",
        "binding": {**BINDING, "job_id": job_id},
        "reason_code": reason,
        "status": status,
        "availability": "UNAVAILABLE",
        "grants": {},
        "log_sha256": None,
        "log_source": None,
    }
    if status in {"QUALIFIED", "INSUFFICIENT"}:
        value.update(availability="AVAILABLE", log_sha256="b" * 64, log_source="setup_step_logs")
        value["grants"] = {
            "actions": "read",
            "contents": "write" if status == "QUALIFIED" else "read",
        }
    if reason == "RUNNER_PERMISSION_LOG_INVALID":
        value.update(log_sha256="b" * 64, log_source="job_logs")
    return value


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    current = [1000.0]
    monkeypatch.setattr(driver.time, "monotonic", lambda: current[0])
    monkeypatch.setenv("GITHUB_TOKEN", "timing-test-token-never-emitted")

    def forbidden_effect(*args, **kwargs):
        pytest.fail("Timing unit tests must not start processes or sleep in real time")

    monkeypatch.setattr(driver.subprocess, "run", forbidden_effect)
    monkeypatch.setattr(driver.subprocess, "Popen", forbidden_effect)
    monkeypatch.setattr(driver.time, "sleep", forbidden_effect)

    def run(
        reports,
        *,
        jobs=None,
        durations=None,
        oversleep=0.0,
        metadata_seconds=0.5,
        http_status=200,
        exit_codes=None,
        collector_error=None,
        first_checkpoint_delay=0.0,
        second_checkpoint_delay=0.0,
    ):
        directory = tmp_path / "observation"
        starts, sleeps, requests, originals = [], [], [], []
        jobs = deepcopy(jobs if jobs is not None else [JOB, JOB])
        durations = durations if durations is not None else [2.0, 3.0]

        def collect(path):
            index = len(starts)
            starts.append(current[0] - 1000.0)
            value = reports[index]
            raw = (
                value if isinstance(value, bytes) else (json.dumps(value, indent=2) + "\n").encode()
            )
            originals.append(raw)
            with path.open("xb") as stream:
                stream.write(raw)
            current[0] += durations[index]
            if collector_error is not None:
                raise collector_error
            if exit_codes is not None:
                return exit_codes[index]
            return 0 if isinstance(value, dict) and value["status"] == "QUALIFIED" else 1

        def sleep(seconds):
            assert seconds >= 0
            sleeps.append(seconds)
            current[0] += seconds + oversleep

        def get(url, *, authenticated, phase, record, deadline):
            assert authenticated is True
            assert phase == "timing_current_job_after"
            assert deadline == current[0] + 10.0
            requests.append(url)
            current[0] += metadata_seconds
            job = jobs[len(requests) - 1]
            record["requests"].append({"phase": phase, "http_status": http_status})
            return {
                "status": http_status,
                "body": job if isinstance(job, bytes) else json.dumps(job).encode(),
                "location": None,
            }

        def metadata(output):
            first = driver._report(output / driver._FIRST)
            return driver._metadata_record(first, IDENTITY)

        real_save = driver._save
        checkpoint_delayed = False
        first_pending_checkpoint = None

        def save(output, summary):
            nonlocal checkpoint_delayed, first_pending_checkpoint
            real_save(output, summary)
            if (
                first_checkpoint_delay
                and first_pending_checkpoint is None
                and len(summary["samples"]) == 1
                and summary["samples"][0]["actual_start_offset_seconds"] is None
            ):
                first_pending_checkpoint = json.loads((output / driver._SUMMARY).read_bytes())
                assert not (output / driver._FIRST).exists()
                current[0] += first_checkpoint_delay
            if second_checkpoint_delay and not checkpoint_delayed and current[0] >= 1061.0:
                current[0] += second_checkpoint_delay
                checkpoint_delayed = True

        monkeypatch.setattr(driver, "_save", save)

        monkeypatch.setattr(driver.permissions, "_get", get)
        code = driver.observe(
            directory,
            deepcopy(IDENTITY),
            collect=collect,
            metadata=metadata,
            clock=lambda: current[0],
            sleep=sleep,
        )
        summary = json.loads((directory / driver._SUMMARY).read_bytes())
        for sample, raw in zip(summary["samples"], originals, strict=True):
            assert (directory / sample["report_file"]).read_bytes() == raw
            assert sample["report_sha256"] == hashlib.sha256(raw).hexdigest()
        assert summary["promotion_authority"] == "NONE"
        assert all(url == JOB["url"] for url in requests)
        assert (directory / driver._SECOND).exists() == (len(starts) == 2)
        return SimpleNamespace(
            code=code,
            summary=summary,
            starts=starts,
            sleeps=sleeps,
            requests=requests,
            originals=originals,
            directory=directory,
            first_pending_checkpoint=first_pending_checkpoint,
        )

    return run


def test_exact_unavailable_reason_resamples_at_t60_and_can_qualify(scenario):
    result = scenario([report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED")])

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (
        0,
        [0.0, 60.0],
        [57.5],
        2,
    )
    summary = result.summary
    assert (summary["observation"], summary["capability"], summary["second_sample"]) == (
        "COMPLETE",
        "OBSERVED_QUALIFIED",
        "EXECUTED",
    )
    assert summary["identity"] == IDENTITY
    assert summary["binding"] == BINDING
    first, second = summary["samples"]
    assert (first["permission_status"], first["exit_code"], first["parsed_grants"]) == (
        "UNAVAILABLE",
        1,
        {},
    )
    assert (second["permission_status"], second["exit_code"], second["parsed_grants"]) == (
        "QUALIFIED",
        0,
        {"actions": "read", "contents": "write"},
    )
    assert first["actual_start_offset_seconds"] == 0.0
    assert second["planned_start_offset_seconds"] == second["actual_start_offset_seconds"] == 60.0
    for sample in (first, second):
        metadata = sample["terminal_metadata"]
        assert metadata["binding_confirmed"] is True
        assert metadata["facts"]["status"] == "in_progress"
        assert metadata["facts"]["conclusion_is_null"] is True


def test_first_pending_checkpoint_io_delay_keeps_t60_from_first_collector_start(scenario):
    result = scenario(
        [report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED")],
        first_checkpoint_delay=5.0,
    )

    pending = result.first_pending_checkpoint
    assert pending is not None
    assert pending["observation"] == "INCOMPLETE"
    assert len(pending["samples"]) == 1
    assert pending["samples"][0]["report_file"] == driver._FIRST
    for field in (
        "actual_start_offset_seconds",
        "finished_offset_seconds",
        "exit_code",
        "report_sha256",
        "terminal_metadata",
    ):
        assert pending["samples"][0][field] is None
    assert result.code == 0
    assert result.starts == [5.0, 65.0]
    assert result.starts[1] - result.starts[0] == 60.0
    assert result.sleeps == [57.5]
    first, second = result.summary["samples"]
    assert first["actual_start_offset_seconds"] == 0.0
    assert second["planned_start_offset_seconds"] == 60.0
    assert second["actual_start_offset_seconds"] == 60.0
    assert result.summary["origin_monotonic_seconds"] == 1005.0
    assert result.summary["deadline_offset_seconds"] == 120.0
    assert result.summary["latest_second_start_offset_seconds"] == 61.0


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        ("REQUIRED_RUNNER_GRANTS_REPORTED", "QUALIFIED"),
        ("REQUIRED_RUNNER_GRANTS_NOT_REPORTED", "INSUFFICIENT"),
        ("RUNNER_PERMISSION_LOG_INVALID", "UNAVAILABLE"),
        ("UNSAFE_LOG_REDIRECT", "UNAVAILABLE"),
        ("CURRENT_JOB_BINDING_CHANGED", "UNAVAILABLE"),
        ("JOB_METADATA_UNAVAILABLE", "UNAVAILABLE"),
        ("CURRENT_JOB_LOGS_UNAVAILABLE_EXTRA", "UNAVAILABLE"),
        ("current_job_logs_unavailable", "UNAVAILABLE"),
        ("CURRENT_JOB_LOGS_UNAVAILABLE", "INSUFFICIENT"),
        ("CURRENT_JOB_LOGS_UNAVAILABLE", "QUALIFIED"),
    ],
)
def test_first_terminal_result_never_takes_a_second_sample(scenario, reason, status):
    value = report(reason, status=status)
    result = scenario([value])

    assert (result.starts, result.sleeps, len(result.requests)) == ([0.0], [], 1)
    assert result.code == (0 if status == "QUALIFIED" else 1)
    assert result.summary["observation"] == "COMPLETE"
    assert result.summary["reason_code"] == reason
    assert result.summary["second_sample"] == "NOT_EXECUTED"
    assert result.summary["capability"] == (
        "OBSERVED_QUALIFIED" if status == "QUALIFIED" else "NOT_ESTABLISHED"
    )
    sample = result.summary["samples"][0]
    assert sample["parsed_grants"] == value["grants"]
    if reason == "RUNNER_PERMISSION_LOG_INVALID":
        assert sample["log_bytes_observed"] is True
        assert sample["permission_status"] == "UNAVAILABLE"
        assert sample["parsed_grants"] == {}


@pytest.mark.parametrize(
    "reason", ["CURRENT_JOB_LOGS_UNAVAILABLE", "RUNNER_PERMISSION_LOG_INVALID"]
)
def test_second_negative_result_is_complete_without_establishing_capability(scenario, reason):
    result = scenario([report(), report(reason)])

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (
        1,
        [0.0, 60.0],
        [57.5],
        2,
    )
    assert result.summary["observation"] == "COMPLETE"
    assert result.summary["capability"] == "NOT_ESTABLISHED"
    assert result.summary["reason_code"] == reason
    assert "not proof of universal unavailability" in result.summary["meaning"]
    assert [sample["exit_code"] for sample in result.summary["samples"]] == [1, 1]


@pytest.mark.parametrize(
    "change",
    [
        {"id": 203, "url": JOB["url"].replace("/202", "/203")},
        {"run_id": 102},
        {"run_attempt": 2},
        {"head_sha": "c" * 40},
        {"name": "Other job"},
        {"status": "completed"},
        {"conclusion": "success"},
        {"url": JOB["url"].replace("/202", "/203")},
        {"run_url": JOB["run_url"].replace("/101", "/102")},
    ],
    ids=[
        "job-id",
        "run",
        "attempt",
        "sha",
        "name",
        "completed",
        "conclusion",
        "job-url",
        "run-url",
    ],
)
def test_first_metadata_identity_mismatch_stops_before_waiting(scenario, change):
    result = scenario([report()], jobs=[{**JOB, **change}])

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (1, [0.0], [], 1)
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == "CURRENT_JOB_BINDING_CHANGED"
    assert result.summary["capability"] == "NOT_ESTABLISHED"
    assert result.summary["samples"][0]["terminal_metadata"]["binding_confirmed"] is False


def test_missing_collector_job_binding_stops_before_metadata(scenario):
    value = report()
    value["binding"] = deepcopy(IDENTITY)
    result = scenario([value])

    assert (result.code, result.starts, result.sleeps, result.requests) == (1, [0.0], [], [])
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == "COLLECTOR_BINDING_INVALID"


def test_second_collector_job_mismatch_cannot_establish_capability(scenario):
    result = scenario(
        [report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED", job_id=203)]
    )

    assert (result.code, result.starts, len(result.requests)) == (1, [0.0, 60.0], 2)
    assert result.summary["binding"] == BINDING
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == "CURRENT_JOB_BINDING_CHANGED"
    assert result.summary["capability"] == "NOT_ESTABLISHED"
    assert result.summary["samples"][1]["exit_code"] == 0


def test_second_terminal_metadata_change_cannot_establish_capability(scenario):
    result = scenario(
        [report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED")],
        jobs=[JOB, {**JOB, "status": "completed"}],
    )

    assert (result.code, result.starts, len(result.requests)) == (1, [0.0, 60.0], 2)
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == "CURRENT_JOB_BINDING_CHANGED"
    assert result.summary["capability"] == "NOT_ESTABLISHED"


@pytest.mark.parametrize(
    ("body", "http_status", "reason"),
    [
        (b"{not-json", 200, "JOB_METADATA_INVALID"),
        (b"[]", 200, "JOB_METADATA_INVALID"),
        (b"null", 200, "JOB_METADATA_INVALID"),
        (b"{}", 403, "JOB_METADATA_UNAVAILABLE"),
    ],
)
def test_metadata_transport_or_json_refusal_is_not_resampled(scenario, body, http_status, reason):
    result = scenario([report()], jobs=[body], http_status=http_status)

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (1, [0.0], [], 1)
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == reason
    assert result.summary["samples"][0]["terminal_metadata"]["reason_code"] == reason


@pytest.mark.parametrize("exit_code", [0, 7, True])
def test_exit_report_mismatch_keeps_original_exit_and_raw_negative(scenario, exit_code):
    result = scenario([report()], exit_codes=[exit_code])

    assert (result.code, result.starts, result.sleeps, result.requests) == (1, [0.0], [], [])
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == "COLLECTOR_EXIT_REPORT_MISMATCH"
    sample = result.summary["samples"][0]
    assert sample["exit_code"] == exit_code
    assert type(sample["exit_code"]) is type(exit_code)
    assert json.loads(result.originals[0])["reason_code"] == "CURRENT_JOB_LOGS_UNAVAILABLE"


@pytest.mark.parametrize(
    ("duration", "oversleep", "expected_sleeps"),
    [(2.0, 2.0, [57.5]), (2.0, -0.5, [57.5])],
    ids=["late-wakeup", "early-wakeup"],
)
def test_missed_second_start_never_extends_observation_window(
    scenario, duration, oversleep, expected_sleeps
):
    result = scenario([report()], durations=[duration], oversleep=oversleep)

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (
        1,
        [0.0],
        expected_sleeps,
        1,
    )
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == "SECOND_SAMPLE_SCHEDULE_MISSED"
    assert result.summary["deadline_offset_seconds"] == 120.0


def test_one_second_second_start_lateness_is_allowed_without_moving_deadline(scenario):
    result = scenario(
        [report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED")],
        oversleep=1.0,
    )

    assert (result.code, result.starts, result.sleeps) == (0, [0.0, 61.0], [57.5])
    assert result.summary["samples"][1]["planned_start_offset_seconds"] == 60.0
    assert result.summary["deadline_offset_seconds"] == 120.0


def test_second_actual_start_after_guard_lateness_allowance_refuses_collector(scenario):
    result = scenario(
        [report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED")],
        oversleep=1.0,
        second_checkpoint_delay=0.5,
    )

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (
        1,
        [0.0],
        [57.5],
        1,
    )
    summary = result.summary
    assert summary["observation"] == "INCOMPLETE"
    assert summary["reason_code"] == "SECOND_SAMPLE_SCHEDULE_MISSED"
    assert summary["capability"] == "NOT_ESTABLISHED"
    assert summary["binding"] == BINDING
    first = summary["samples"][0]
    assert first["terminal_metadata"]["binding_confirmed"] is True
    assert (result.directory / driver._FIRST).read_bytes() == result.originals[0]
    assert first["report_sha256"] == hashlib.sha256(result.originals[0]).hexdigest()
    assert not (result.directory / driver._SECOND).exists()


@pytest.mark.parametrize("finished_at", [60.0, 60.5, 61.0], ids=["t60", "t60_5", "t61"])
def test_first_confirmed_metadata_within_lateness_allowance_resamples_immediately(
    scenario, finished_at
):
    result = scenario(
        [report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED")],
        durations=[finished_at - 0.5, 3.0],
    )

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (
        0,
        [0.0, finished_at],
        [],
        2,
    )
    summary = result.summary
    assert (summary["observation"], summary["capability"], summary["second_sample"]) == (
        "COMPLETE",
        "OBSERVED_QUALIFIED",
        "EXECUTED",
    )
    assert summary["binding"] == BINDING
    assert summary["deadline_offset_seconds"] == 120.0
    assert summary["latest_second_start_offset_seconds"] == 61.0
    first, second = summary["samples"]
    assert first["terminal_metadata"]["finished_offset_seconds"] == finished_at
    assert second["planned_start_offset_seconds"] == 60.0
    for sample, raw in zip((first, second), result.originals, strict=True):
        assert sample["terminal_metadata"]["binding_confirmed"] is True
        assert json.loads(raw)["binding"] == BINDING
        assert (result.directory / sample["report_file"]).read_bytes() == raw
        assert sample["report_sha256"] == hashlib.sha256(raw).hexdigest()


def test_first_confirmed_metadata_after_lateness_allowance_refuses_second_sample(scenario):
    result = scenario([report()], durations=[61.0])

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (
        1,
        [0.0],
        [],
        1,
    )
    summary = result.summary
    assert summary["observation"] == "INCOMPLETE"
    assert summary["reason_code"] == "SECOND_SAMPLE_SCHEDULE_MISSED"
    assert summary["capability"] == "NOT_ESTABLISHED"
    assert summary["binding"] == BINDING
    assert summary["deadline_offset_seconds"] == 120.0
    assert summary["latest_second_start_offset_seconds"] == 61.0
    metadata = summary["samples"][0]["terminal_metadata"]
    assert metadata["binding_confirmed"] is True
    assert metadata["finished_offset_seconds"] == 61.5


@pytest.mark.parametrize(
    ("durations", "expected_starts", "metadata_calls"),
    [([110.0], [0.0], 0), ([2.0, 50.0], [0.0, 60.0], 1), ([2.0, 60.0], [0.0, 60.0], 1)],
    ids=["first-at-metadata-budget-limit", "second-at-budget-limit", "second-at-overall-deadline"],
)
def test_metadata_does_not_start_without_full_budget_inside_deadline(
    scenario, durations, expected_starts, metadata_calls
):
    result = scenario(
        [report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED")],
        durations=durations,
    )

    assert (result.code, result.starts, len(result.requests)) == (
        1,
        expected_starts,
        metadata_calls,
    )
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == "METADATA_CANNOT_FINISH_WITHIN_WINDOW"
    assert result.summary["capability"] == "NOT_ESTABLISHED"
    assert result.summary["samples"][-1]["terminal_metadata"] is None


def test_metadata_finishing_at_overall_deadline_is_incomplete(scenario):
    result = scenario([report()], metadata_seconds=118.0)

    assert (result.code, result.starts, result.sleeps, len(result.requests)) == (1, [0.0], [], 1)
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == "OBSERVATION_DEADLINE_EXCEEDED"
    assert result.summary["capability"] == "NOT_ESTABLISHED"
    metadata = result.summary["samples"][0]["terminal_metadata"]
    assert metadata["binding_confirmed"] is True
    assert metadata["finished_offset_seconds"] == 120.0


@pytest.mark.parametrize("interrupted", [False, True], ids=["partial-return", "interrupted-write"])
def test_partial_report_and_timeout_finalization_preserve_evidence(scenario, interrupted):
    raw = b'{"diagnostic":'
    error = driver._IncompleteError("OWNED_PROCESS_HARD_DEADLINE_EXCEEDED") if interrupted else None
    result = scenario([raw], collector_error=error)
    sample_before = deepcopy(result.summary["samples"][0])

    assert result.code == 1
    assert result.summary["observation"] == "INCOMPLETE"
    assert result.summary["reason_code"] == (
        "OWNED_PROCESS_HARD_DEADLINE_EXCEEDED" if interrupted else "COLLECTOR_REPORT_INCOMPLETE"
    )
    assert (result.starts, result.sleeps, result.requests) == ([0.0], [], [])
    assert sample_before["exit_code"] == (None if interrupted else 1)
    assert sample_before["finished_offset_seconds"] == (None if interrupted else 2.0)

    assert driver.finalize_timeout(result.directory, -1) == 1
    final = json.loads((result.directory / driver._SUMMARY).read_bytes())
    assert final["observation"] == "INCOMPLETE"
    assert final["reason_code"] == "OWNED_PROCESS_HARD_DEADLINE_EXCEEDED"
    assert final["observation_process_exit_code"] == -1
    assert final["capability"] == "NOT_ESTABLISHED"
    assert final["promotion_authority"] == "NONE"
    assert final["watchdog_finalized_offset_seconds"] == 2.0
    assert final["samples"] == [sample_before]
    assert (result.directory / sample_before["report_file"]).read_bytes() == raw
    assert sample_before["report_sha256"] == hashlib.sha256(raw).hexdigest()
    assert not (result.directory / driver._SECOND).exists()


def test_timeout_finalization_revokes_qualification_without_rewriting_reports(scenario):
    result = scenario([report(), report("REQUIRED_RUNNER_GRANTS_REPORTED", status="QUALIFIED")])
    assert result.code == 0
    samples_before = deepcopy(result.summary["samples"])

    assert driver.finalize_timeout(result.directory, -1) == 1
    final = json.loads((result.directory / driver._SUMMARY).read_bytes())
    assert final["observation"] == "INCOMPLETE"
    assert final["capability"] == "NOT_ESTABLISHED"
    assert final["reason_code"] == "OWNED_PROCESS_HARD_DEADLINE_EXCEEDED"
    assert final["observation_process_exit_code"] == -1
    assert final["identity"] == IDENTITY
    assert final["binding"] == BINDING
    assert final["promotion_authority"] == "NONE"
    assert final["samples"] == samples_before
    assert "not proof of universal unavailability" in final["meaning"]
    for sample, raw in zip(final["samples"], result.originals, strict=True):
        assert (result.directory / sample["report_file"]).read_bytes() == raw
        assert sample["report_sha256"] == hashlib.sha256(raw).hexdigest()


def test_timeout_before_initial_summary_records_incomplete_context(tmp_path, monkeypatch):
    directory = tmp_path / "not-started"
    monkeypatch.setattr(driver.permissions, "_read_context", lambda: (deepcopy(IDENTITY), {}))

    assert driver.finalize_timeout(directory, -1) == 1
    final = json.loads((directory / driver._SUMMARY).read_bytes())
    assert final["identity"] == IDENTITY
    assert final["binding"] is None
    assert final["samples"] == []
    assert final["origin_monotonic_seconds"] is None
    assert final["watchdog_finalized_offset_seconds"] is None
    assert final["observation"] == "INCOMPLETE"
    assert final["reason_code"] == "OWNED_PROCESS_HARD_DEADLINE_EXCEEDED"
    assert final["capability"] == "NOT_ESTABLISHED"
    assert final["promotion_authority"] == "NONE"
    assert final["observation_process_exit_code"] == -1
