"""Diagnose runner-reported permissions for the current GitHub Actions job.

The two evidence functions remain pure. The CLI performs bounded HTTPS GETs
in one disposable worker process. It is restricted to job probe_permissions
(Probe current job permissions) in the canonical stateless-preview.yml workflow;
no job-name override exists. Socket operations time out after ten seconds.
DNS is bounded by the outer 45-second worker deadline, not separately.
Reported grants establish neither token identity nor artifact-promotion authority.
"""

from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

_MAX_LOG_BYTES = 1024 * 1024
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,7})?Z ")
_GRANT = re.compile(r"([A-Za-z][A-Za-z0-9_-]*): (none|read|write)")
_PERMISSION_GROUP = "GITHUB_TOKEN Permissions"
_PROBE_JOB_ID = "probe_permissions"
_PROBE_JOB_NAME = "Probe current job permissions"


def select_current_job(
    jobs: Mapping,
    *,
    repository: str,
    run_id: int,
    run_attempt: int,
    head_sha: str,
    job_name: str,
) -> Mapping:
    """Return a new job/zero-based setup selector or refuse incomplete identity."""
    if (
        not isinstance(repository, str)
        or re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        or type(run_id) is not int
        or run_id <= 0
        or type(run_attempt) is not int
        or run_attempt <= 0
        or not isinstance(head_sha, str)
        or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None
        or not isinstance(job_name, str)
        or not job_name.strip()
    ):
        raise ValueError("Current-job identity is missing or invalid.")
    if not isinstance(jobs, Mapping):
        raise ValueError("The job listing must be an API response mapping.")
    entries = jobs.get("jobs")
    total = jobs.get("total_count")
    if not isinstance(entries, list) or type(total) is not int or total != len(entries):
        raise ValueError("The job listing is malformed or incomplete.")
    if any(
        not isinstance(entry, Mapping) or not isinstance(entry.get("name"), str)
        for entry in entries
    ):
        raise ValueError("The job listing contains malformed job metadata.")
    matches = [entry for entry in entries if entry["name"] == job_name]
    if len(matches) != 1:
        raise ValueError("The current job name must identify exactly one job.")
    job = matches[0]
    job_id = job.get("id")
    if (
        type(job_id) is not int
        or job_id <= 0
        or type(job.get("run_id")) is not int
        or job["run_id"] != run_id
        or type(job.get("run_attempt")) is not int
        or job["run_attempt"] != run_attempt
        or job.get("head_sha") != head_sha
        or job.get("status") != "in_progress"
        or "conclusion" not in job
        or job["conclusion"] is not None
        or job.get("url") != f"https://api.github.com/repos/{repository}/actions/jobs/{job_id}"
        or job.get("run_url") != f"https://api.github.com/repos/{repository}/actions/runs/{run_id}"
    ):
        raise ValueError("Job metadata does not bind to the requested running job.")
    steps = job.get("steps")
    if not isinstance(steps, list) or any(
        not isinstance(step, Mapping) or not isinstance(step.get("name"), str) for step in steps
    ):
        raise ValueError("The current job has malformed setup metadata.")
    positions = [index for index, step in enumerate(steps) if step["name"] == "Set up job"]
    if positions != [0]:
        raise ValueError("The setup step must occur exactly once at array position zero.")
    setup = steps[0]
    if setup.get("status") != "completed" or setup.get("conclusion") != "success":
        raise ValueError("The setup step has not completed successfully.")
    # The step-logs endpoint uses array position, not the API step's number.
    return {"job_id": job_id, "setup_step_position": positions[0]}


def parse_setup_permissions(log: bytes) -> Mapping[str, str]:
    """Return every explicit grant in exactly one native runner setup group."""
    if not isinstance(log, bytes) or not log or len(log) > _MAX_LOG_BYTES:
        raise ValueError("Setup log bytes are missing or exceed the one-MiB limit.")
    try:
        text = log.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise ValueError("The setup log is not valid UTF-8 text.") from None
    if any(
        ord(character) < 32 and character not in "\t\r\n" or ord(character) == 127
        for character in text
    ):
        raise ValueError("The setup log contains binary control bytes.")
    text = text.replace("\r\n", "\n")
    if "\r" in text:
        raise ValueError("The setup log contains malformed line endings.")

    permissions: dict[str, str] = {}
    depth = 0
    seen_permissions = False
    in_permissions = False
    # JobExtension.InitializeJob emits this group directly from
    # system.github.token.permissions; application stdout is not its authority.
    for line in text.split("\n"):
        message = _TIMESTAMP.sub("", line, count=1)
        if message.startswith("##[group]"):
            title = message[len("##[group]") :]
            if in_permissions or not title:
                raise ValueError("The setup log contains ambiguous group framing.")
            if title == _PERMISSION_GROUP:
                if seen_permissions or depth:
                    raise ValueError("The permission group is duplicated or nested.")
                seen_permissions = True
                in_permissions = True
            depth += 1
        elif message == "##[endgroup]":
            if depth == 0:
                raise ValueError("The setup log contains an unmatched group terminator.")
            if in_permissions:
                in_permissions = False
            depth -= 1
        elif "##[group" in message or "##[endgroup" in message:
            raise ValueError("The setup log contains malformed group framing.")
        elif in_permissions:
            grant = _GRANT.fullmatch(message)
            if grant is None:
                raise ValueError("The permission group contains a malformed grant.")
            name, access = grant.groups()
            name = name.lower()
            if name in permissions:
                raise ValueError("The permission group contains a duplicate grant.")
            permissions[name] = access
    if depth or in_permissions or not seen_permissions or not permissions:
        raise ValueError("Exactly one complete nonempty permission group is required.")
    return permissions


class _ProbeError(ValueError):
    """A closed reason code, never a URL, response body, or provider exception."""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):
        return None


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_redirect_url(url: str) -> str:
    if (
        not isinstance(url, str)
        or len(url) > 8192
        or any(ord(character) < 33 or ord(character) > 126 for character in url)
    ):
        raise _ProbeError("UNSAFE_LOG_REDIRECT")
    try:
        parsed = urllib.parse.urlsplit(url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        raise _ProbeError("UNSAFE_LOG_REDIRECT") from None
    if (
        parsed.scheme != "https"
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
        or port not in (None, 443)
        or "%" in host
    ):
        raise _ProbeError("UNSAFE_LOG_REDIRECT")
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        # Reject local names and alternate numeric IPv4 spellings as well.
        if (
            "." not in host
            or host.endswith((".localhost", ".local", ".internal", ".localdomain"))
            or re.fullmatch(r"[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+", host) is None
            or re.fullmatch(r"(?:0x[0-9a-f]+|[0-9]+)(?:\.(?:0x[0-9a-f]+|[0-9]+))*", host, re.I)
        ):
            raise _ProbeError("UNSAFE_LOG_REDIRECT") from None
    else:
        if not address.is_global:
            raise _ProbeError("UNSAFE_LOG_REDIRECT")
    return url


def _read_context() -> tuple[dict, dict]:
    if (
        os.environ.get("GITHUB_ACTIONS") != "true"
        or os.environ.get("GITHUB_EVENT_NAME") != "workflow_dispatch"
        or os.environ.get("GITHUB_REF") != "refs/heads/main"
        or os.environ.get("GITHUB_JOB") != _PROBE_JOB_ID
    ):
        raise _ProbeError("ACTIONS_CONTEXT_REQUIRED")
    repository = os.environ.get("GITHUB_REPOSITORY", "")
    run_id = os.environ.get("GITHUB_RUN_ID", "")
    run_attempt = os.environ.get("GITHUB_RUN_ATTEMPT", "")
    head_sha = os.environ.get("GITHUB_SHA", "")
    if (
        re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) is None
        or any(part in {".", ".."} for part in repository.split("/"))
        or re.fullmatch(r"[1-9][0-9]{0,19}", run_id) is None
        or re.fullmatch(r"[1-9][0-9]{0,19}", run_attempt) is None
        or re.fullmatch(r"[0-9a-fA-F]{40}", head_sha) is None
        or os.environ.get("GITHUB_WORKFLOW_REF")
        != f"{repository}/.github/workflows/stateless-preview.yml@refs/heads/main"
        or os.environ.get("GITHUB_WORKFLOW_SHA") != head_sha
    ):
        raise _ProbeError("ACTIONS_IDENTITY_INVALID")
    token = os.environ.get("GITHUB_TOKEN", "")
    if not token or any(ord(character) < 33 or ord(character) > 126 for character in token):
        raise _ProbeError("GITHUB_TOKEN_UNAVAILABLE")
    identity = {
        "repository": repository,
        "run_id": int(run_id),
        "run_attempt": int(run_attempt),
        "head_sha": head_sha,
        "job_name": _PROBE_JOB_NAME,
    }
    actors = {name: os.environ.get(name) for name in ("GITHUB_ACTOR", "GITHUB_TRIGGERING_ACTOR")}
    if any(
        value is not None
        and (len(value) > 100 or re.fullmatch(r"[A-Za-z0-9_.\[\]-]+", value) is None)
        for value in actors.values()
    ) or any(
        token in value for value in (*identity.values(), *actors.values()) if isinstance(value, str)
    ):
        raise _ProbeError("UNSAFE_PROVENANCE_VALUE")
    return identity, actors


def _new_record() -> dict:
    return {
        "diagnostic": "current-job-permissions",
        "meaning": "RUNNER_REPORTED_GRANT_NOT_TOKEN_INTROSPECTION",
        "status": "UNAVAILABLE",
        "availability": "UNAVAILABLE",
        "reason_code": "PROBE_NOT_RUN",
        "started_at": _utc_now(),
        "finished_at": None,
        "binding": {},
        "actor_provenance": {},
        "grants": {},
        "log_sha256": None,
        "log_source": None,
        "requests": [],
    }


def _get(url: str, *, authenticated: bool, phase: str, record: dict, deadline: float) -> dict:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise _ProbeError("PROBE_DEADLINE_EXCEEDED")
    headers = {
        "Accept": "application/vnd.github+json" if authenticated else "application/octet-stream"
    }
    if authenticated:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.github.com":
            raise _ProbeError("API_ORIGIN_REFUSED")
        headers.update(
            {
                "Authorization": "Bearer " + os.environ["GITHUB_TOKEN"],
                "X-GitHub-Api-Version": "2026-03-10",
            }
        )
    observation = {
        "phase": phase,
        "http_status": None,
        "reason_code": "NETWORK_UNAVAILABLE",
        "started_at": _utc_now(),
    }
    record["requests"].append(observation)
    try:
        opener = urllib.request.build_opener(_NoRedirectHandler())
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = opener.open(request, timeout=min(10.0, remaining))
        except urllib.error.HTTPError as error:
            response = error
        with response:
            status = response.status
            observation["http_status"] = status
            result = {"status": status, "body": b"", "location": None}
            if status == 302:
                locations = response.headers.get_all("Location", [])
                if len(locations) != 1:
                    raise _ProbeError("LOG_REDIRECT_MISSING_OR_AMBIGUOUS")
                result["location"] = locations[0]
            elif status == 200:
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    raise _ProbeError("RESPONSE_ENCODING_UNSUPPORTED")
                raw = response.read(_MAX_LOG_BYTES + 1)
                if len(raw) > _MAX_LOG_BYTES:
                    raise _ProbeError("RESPONSE_SIZE_EXCEEDED")
                result["body"] = raw
            observation["reason_code"] = "HTTP_RESPONSE"
            return result
    except _ProbeError as error:
        observation["reason_code"] = str(error)
        raise
    except TimeoutError:
        observation["reason_code"] = "SOCKET_TIMEOUT"
        raise _ProbeError("SOCKET_TIMEOUT") from None
    except (urllib.error.URLError, OSError, ValueError):
        raise _ProbeError("NETWORK_UNAVAILABLE") from None
    finally:
        observation["finished_at"] = _utc_now()


def _get_json(url: str, *, phase: str, record: dict, deadline: float) -> Mapping:
    response = _get(url, authenticated=True, phase=phase, record=record, deadline=deadline)
    if response["status"] != 200:
        raise _ProbeError("JOB_METADATA_UNAVAILABLE")
    try:
        value = json.loads(response["body"])
    except (ValueError, UnicodeDecodeError):
        raise _ProbeError("JOB_METADATA_INVALID") from None
    if not isinstance(value, Mapping):
        raise _ProbeError("JOB_METADATA_INVALID")
    return value


def _get_log(url: str, *, phase: str, record: dict, deadline: float) -> bytes | None:
    response = _get(url, authenticated=True, phase=phase, record=record, deadline=deadline)
    if response["status"] == 302:
        location = _validate_redirect_url(response["location"])
        # A fresh request has no Authorization header, even for a GitHub host.
        response = _get(
            location,
            authenticated=False,
            phase=phase + "_redirect",
            record=record,
            deadline=deadline,
        )
    if response["status"] == 200 and response["body"]:
        return response["body"]
    return None


def _run_probe() -> dict:
    record = _new_record()
    deadline = time.monotonic() + 45.0
    try:
        identity, actors = _read_context()
        record["binding"] = identity.copy()
        record["actor_provenance"] = actors
        repository = identity["repository"]
        base = f"https://api.github.com/repos/{repository}/actions"
        jobs = _get_json(
            f"{base}/runs/{identity['run_id']}/attempts/{identity['run_attempt']}/jobs?per_page=100",
            phase="current_attempt_jobs",
            record=record,
            deadline=deadline,
        )
        try:
            selected = select_current_job(jobs, **identity)
        except ValueError:
            raise _ProbeError("CURRENT_JOB_NOT_UNAMBIGUOUS") from None
        record["binding"].update(selected)
        job_url = f"{base}/jobs/{selected['job_id']}"
        raw = _get_log(
            f"{job_url}/steps/{selected['setup_step_position']}/logs",
            phase="setup_step_logs",
            record=record,
            deadline=deadline,
        )
        record["log_source"] = "setup_step_logs"
        if raw is None:
            raw = _get_log(job_url + "/logs", phase="job_logs", record=record, deadline=deadline)
            record["log_source"] = "job_logs"
        if raw is None:
            raise _ProbeError("CURRENT_JOB_LOGS_UNAVAILABLE")
        record["log_sha256"] = hashlib.sha256(raw).hexdigest()
        after = _get_json(job_url, phase="current_job_after", record=record, deadline=deadline)
        try:
            after_selected = select_current_job({"total_count": 1, "jobs": [after]}, **identity)
        except ValueError:
            raise _ProbeError("CURRENT_JOB_BINDING_CHANGED") from None
        if after_selected != selected:
            raise _ProbeError("CURRENT_JOB_BINDING_CHANGED")
        try:
            grants = dict(parse_setup_permissions(raw))
        except ValueError:
            raise _ProbeError("RUNNER_PERMISSION_LOG_INVALID") from None
        record["grants"] = grants
        record["availability"] = "AVAILABLE"
        if grants.get("actions") == "read" and grants.get("contents") == "write":
            record.update(status="QUALIFIED", reason_code="REQUIRED_RUNNER_GRANTS_REPORTED")
        else:
            record.update(status="INSUFFICIENT", reason_code="REQUIRED_RUNNER_GRANTS_NOT_REPORTED")
    except _ProbeError as error:
        record["reason_code"] = str(error)
    except Exception:
        record["reason_code"] = "PROBE_INTERNAL_ERROR"
    record["finished_at"] = _utc_now()
    return record


def _safe_record(record: dict) -> dict:
    token = os.environ.get("GITHUB_TOKEN", "")
    if token and token in json.dumps(record, sort_keys=True):
        record = _new_record()
        record.update(reason_code="UNSAFE_DIAGNOSTIC_VALUE", finished_at=_utc_now())
    return record


def _run_worker() -> dict:
    process = subprocess.Popen(
        [
            sys.executable,
            "-I",
            "-u",
            str(Path(__file__).resolve()),
            "--_worker",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        stdout, _ = process.communicate(timeout=45)
    except BaseException as error:
        if process.poll() is None:
            process.kill()
        process.communicate()
        if isinstance(error, subprocess.TimeoutExpired):
            raise _ProbeError("PROBE_DEADLINE_EXCEEDED") from None
        raise
    if process.returncode != 0 or len(stdout) > 64 * 1024:
        raise _ProbeError("PROBE_WORKER_UNAVAILABLE")
    try:
        result = json.loads(stdout)
    except (ValueError, UnicodeDecodeError):
        raise _ProbeError("PROBE_WORKER_INVALID") from None
    if (
        not isinstance(result, dict)
        or result.get("status") not in {"QUALIFIED", "INSUFFICIENT", "UNAVAILABLE"}
        or result.get("meaning") != "RUNNER_REPORTED_GRANT_NOT_TOKEN_INTROSPECTION"
    ):
        raise _ProbeError("PROBE_WORKER_INVALID")
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--_worker", action="store_true", help=argparse.SUPPRESS)
    arguments = parser.parse_args(argv)
    if arguments._worker:
        # Both process boundaries exclude credentials before serialization.
        print(json.dumps(_safe_record(_run_probe()), sort_keys=True))
        return 0
    if arguments.output is None:
        parser.error("--output is required")
    record = _new_record()
    try:
        stream = arguments.output.open("x", encoding="utf-8", newline="\n")
    except OSError:
        print("UNAVAILABLE OUTPUT_CREATE_FAILED")
        return 1
    try:
        with stream:
            try:
                identity, actors = _read_context()
                record["binding"] = identity
                record["actor_provenance"] = actors
                record = _run_worker()
            except _ProbeError as error:
                record["reason_code"] = str(error)
            except Exception:
                record["reason_code"] = "PROBE_INTERNAL_ERROR"
            record["finished_at"] = _utc_now()
            record = _safe_record(record)
            stream.write(json.dumps(record, indent=2, sort_keys=True) + "\n")
    except OSError:
        print("UNAVAILABLE OUTPUT_WRITE_FAILED")
        return 1
    print(record["status"] + " " + record["reason_code"])
    return 0 if record["status"] == "QUALIFIED" else 1


if __name__ == "__main__":
    raise SystemExit(main())
