#!/usr/bin/env python3
"""Run one secret-free, exact-head SonarQube release scan."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import xml.etree.ElementTree as ElementTree
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

PROJECT_KEY = "thebtf_netcoredbg_mcp"
REQUIRED_ENV = ("SONAR_HOST_URL", "SONAR_TOKEN", "SONAR_READ_TOKEN")
SONAR_ENV = (*REQUIRED_ENV, "SONAR_ADMIN_TOKEN")
RECEIPT_SCHEMA_VERSION = 2
CE_TIMEOUT_SECONDS = 10 * 60
INDEX_TIMEOUT_SECONDS = 2 * 60
POLL_SECONDS = 5
PAGE_SIZE = 500
RESULT_CAP = 10_000
LOCK_LEASE_SECONDS = 30 * 60
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SOLUTION_PROJECT_RE = re.compile(r'^Project\("[^"]+"\) = "([^"]+)", "([^"]+\.csproj)"', re.MULTILINE)
ISSUE_STATUSES = "OPEN,CONFIRMED,FALSE_POSITIVE,ACCEPTED,FIXED,IN_SANDBOX"
GENERATED_DIRECTORY_NAMES = {"bin", "obj"}
GENERATED_ROOT_NAMES = {".sonarqube", ".scannerwork"}


class RunnerError(RuntimeError):
    """A fail-closed release-gate error safe to place in a receipt."""


class CredentialsUnavailable(RunnerError):
    """A credential-gate blocker that never includes a credential value."""

    def __init__(self, *input_names: str) -> None:
        super().__init__("SONAR_CREDENTIALS_UNAVAILABLE: " + ", ".join(sorted(set(input_names))) + ".")


class ApiHttpError(RunnerError):
    """An API status which determines whether bounded indexing retries are allowed."""

    def __init__(self, endpoint: str, status: int, input_name: str) -> None:
        super().__init__(f"Sonar API {endpoint} returned HTTP {status}.")
        self.status = status
        self.input_name = input_name


class NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Never forward bearer authorization to an HTTP redirect target."""

    def redirect_request(self, *_: Any, **__: Any) -> None:
        return None


API_OPENER = urllib.request.build_opener(NoRedirectHandler())


@dataclass(frozen=True)
class GitContext:
    repository_root: Path
    common_dir: Path
    git_dir: Path
    coordination_root: Path
    head: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("candidate", "post-merge"))
    parser.add_argument(
        "--scanner",
        help="Optional SonarScanner for .NET executable path/name; never a shell command.",
    )
    return parser.parse_args(argv)


def credential_free_host(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
    ):
        raise CredentialsUnavailable("SONAR_HOST_URL")
    return f"{parsed.scheme}://{parsed.netloc}"

def response_origin(value: str) -> str:
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise RunnerError("Sonar API response URL has an invalid origin.")
    return f"{parsed.scheme}://{parsed.netloc}"


def assert_no_in_tree_dotenv(repository_root: Path) -> None:
    for dotenv_path in repository_root.rglob(".env"):
        if dotenv_path.exists() or dotenv_path.is_symlink():
            raise RunnerError(
                "Refusing an in-tree .env: inject Sonar credentials from the Vault into the runner process."
            )


def load_credentials(repository_root: Path, process_env: Mapping[str, str]) -> dict[str, str]:
    """Read only parent-process credentials; source-tree files are never credential inputs."""
    assert_no_in_tree_dotenv(repository_root)
    if "SONAR_ADMIN_TOKEN" in process_env:
        raise RunnerError("SONAR_ADMIN_TOKEN is forbidden; use only project-scoped credentials.")
    missing = [name for name in REQUIRED_ENV if not process_env.get(name, "").strip()]
    if missing:
        raise CredentialsUnavailable(*missing)
    credentials = {name: process_env[name].strip() for name in REQUIRED_ENV}
    credentials["SONAR_HOST_URL"] = credential_free_host(credentials["SONAR_HOST_URL"])
    return credentials


def scrub_sonar_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in source.items() if key not in SONAR_ENV}


def scanner_environment(base_environment: Mapping[str, str], credentials: Mapping[str, str]) -> dict[str, str]:
    environment = scrub_sonar_environment(base_environment)
    environment["SONAR_HOST_URL"] = credentials["SONAR_HOST_URL"]
    environment["SONAR_TOKEN"] = credentials["SONAR_TOKEN"]
    return environment


def redact(text: str, secrets: Sequence[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    secrets: Sequence[str],
    label: str,
    credential_input_names: Sequence[str] = (),
) -> None:
    print("+ " + " ".join(command), flush=True)
    try:
        completed = subprocess.run(
            list(command),
            cwd=cwd,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            check=False,
        )
    except OSError as error:
        code = error.winerror if getattr(error, "winerror", None) is not None else error.errno
        detail = redact(str(error), secrets)
        raise RunnerError(f"{label} could not start: {error.__class__.__name__} code={code}: {detail}") from error
    output = completed.stdout or ""
    if output:
        print(redact(output, secrets), end="" if output.endswith("\n") else "\n", flush=True)
    if completed.returncode:
        if credential_input_names and re.search(r"\b(?:401|403|authenticat|authoriz|forbidden|token)\b", output, re.IGNORECASE):
            raise CredentialsUnavailable(*credential_input_names)
        raise RunnerError(f"{label} failed with exit code {completed.returncode}.")


def git_result(repository_root: Path, environment: Mapping[str, str], *arguments: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            env=dict(environment),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    except OSError as error:
        raise RunnerError("Git is unavailable for exact-head validation.") from error


def git_output(repository_root: Path, environment: Mapping[str, str], *arguments: str) -> str:
    completed = git_result(repository_root, environment, *arguments)
    if completed.returncode:
        raise RunnerError("Git exact-head validation command failed.")
    return completed.stdout.strip()


def resolve_git_path(repository_root: Path, raw_path: str) -> Path:
    path = Path(raw_path)
    return (path if path.is_absolute() else repository_root / path).resolve()


def git_context(start_directory: Path, environment: Mapping[str, str]) -> GitContext:
    repository_root = Path(git_output(start_directory, environment, "rev-parse", "--show-toplevel")).resolve()
    common_dir = resolve_git_path(
        repository_root, git_output(repository_root, environment, "rev-parse", "--git-common-dir")
    )
    git_dir = resolve_git_path(
        repository_root, git_output(repository_root, environment, "rev-parse", "--git-dir")
    )
    if not common_dir.is_dir() or not git_dir.is_dir():
        raise RunnerError("Git metadata is unavailable for an exact-head scanner worktree.")
    head = git_output(repository_root, environment, "rev-parse", "HEAD")
    if not SHA_RE.fullmatch(head):
        raise RunnerError("Git HEAD is not a complete commit SHA.")

    symbolic_head = git_result(repository_root, environment, "symbolic-ref", "-q", "HEAD")
    if symbolic_head.returncode == 0:
        raise RunnerError("Exact-head scan requires a detached HEAD worktree.")
    if symbolic_head.returncode != 1:
        raise RunnerError("Git could not verify detached HEAD state.")
    if git_dir == common_dir or repository_root == common_dir.parent:
        raise RunnerError("Exact-head scan requires a linked disposable worktree, not the primary checkout.")
    return GitContext(repository_root, common_dir, git_dir, common_dir.parent, head)


def strict_cleanliness(context: GitContext, environment: Mapping[str, str], phase: str) -> dict[str, Any]:
    output = git_output(
        context.repository_root,
        environment,
        "status",
        "--porcelain=v1",
        "--untracked-files=all",
        "--ignored",
    )
    if output:
        raise RunnerError(f"Exact-head scanner worktree is not clean before {phase}.")
    return {"phase": phase, "status": "clean", "status_line_count": 0}


def assert_head_unchanged(context: GitContext, environment: Mapping[str, str]) -> None:
    actual = git_output(context.repository_root, environment, "rev-parse", "HEAD")
    if actual != context.head:
        raise RunnerError("Git HEAD changed during the exact-head scan.")


def assert_post_merge_target(context: GitContext, environment: Mapping[str, str]) -> None:
    origin_main = git_output(
        context.repository_root, environment, "rev-parse", "--verify", "origin/main^{commit}"
    )
    if origin_main != context.head:
        raise RunnerError("post-merge scans must run at the exact origin/main commit.")


def is_tracked(repository_root: Path, environment: Mapping[str, str], path: Path) -> bool:
    relative = str(path.relative_to(repository_root)).replace("\\", "/")
    return bool(git_output(repository_root, environment, "ls-files", "--", relative))


def clear_generated_artifacts(context: GitContext, environment: Mapping[str, str]) -> list[str]:
    """Delete only known ignored scanner/build output from the disposable worktree."""
    candidates = [context.repository_root / name for name in GENERATED_ROOT_NAMES]
    for directory in context.repository_root.rglob("*"):
        if directory.name in GENERATED_DIRECTORY_NAMES and directory.is_dir():
            candidates.append(directory)
    removed: list[str] = []
    for candidate in sorted(set(candidates), key=lambda path: len(path.parts), reverse=True):
        if not candidate.exists():
            continue
        if candidate.is_symlink():
            raise RunnerError("Refusing to remove generated artifacts through a symbolic link.")
        try:
            candidate.resolve().relative_to(context.repository_root)
        except ValueError as error:
            raise RunnerError("Generated artifact path escapes the scanner worktree.") from error
        if is_tracked(context.repository_root, environment, candidate):
            raise RunnerError("Refusing to remove a tracked path as generated scanner output.")
        if candidate.is_dir():
            shutil.rmtree(candidate)
        else:
            candidate.unlink()
        removed.append(str(candidate.relative_to(context.repository_root)).replace("\\", "/"))
    return sorted(removed)


def project_key_from_xml(path: Path) -> str:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise RunnerError("SonarQube.Analysis.xml is unreadable.") from error
    keys = [
        element.text.strip()
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "Property"
        and element.attrib.get("Name") == "sonar.projectKey"
        and element.text
    ]
    if keys != [PROJECT_KEY]:
        raise RunnerError("SonarQube.Analysis.xml does not contain the fixed project key.")
    return keys[0]


def discover_scanner(override: str | None) -> list[str]:
    candidates = [override] if override else [
        "dotnet-sonarscanner",
        "SonarScanner.MSBuild.exe",
        "SonarScanner.MSBuild",
    ]
    for candidate in candidates:
        if candidate:
            found = shutil.which(candidate)
            if found:
                return [found]
    raise RunnerError("SONAR_SCANNER_UNAVAILABLE: install SonarScanner for .NET or pass --scanner.")


def scanner_begin_command(scanner: Sequence[str], analysis_xml: Path, host: str, head: str) -> list[str]:
    return [
        *scanner,
        "begin",
        f"/k:{PROJECT_KEY}",
        f"/s:{analysis_xml}",
        f"/d:sonar.host.url={host}",
        f"/d:sonar.scm.revision={head}",
    ]


def project_inventory(repository_root: Path) -> tuple[Path, list[Path], list[Path]]:
    solution = repository_root / "netcoredbg-mcp.sln"
    if not solution.is_file():
        raise RunnerError("Repository solution netcoredbg-mcp.sln is missing.")
    solution_projects = [
        (repository_root / raw_path.replace("\\", "/")).resolve()
        for _, raw_path in SOLUTION_PROJECT_RE.findall(solution.read_text(encoding="utf-8"))
    ]
    if not solution_projects or any(not project.is_file() for project in solution_projects):
        raise RunnerError("Solution project inventory is incomplete.")
    excluded_parts = {".git", ".agent", ".sonarqube", "bin", "obj"}
    projects = sorted(
        (
            path.resolve()
            for path in repository_root.rglob("*.csproj")
            if not excluded_parts.intersection(path.relative_to(repository_root).parts)
        ),
        key=lambda path: path.as_posix().lower(),
    )
    if not projects or not set(solution_projects).issubset(projects):
        raise RunnerError("Solution project inventory disagrees with discovered C# projects.")
    return solution, projects, [project for project in projects if project not in set(solution_projects)]


def scanner_metadata(repository_root: Path, expected_head: str) -> dict[str, Any]:
    metadata_root = repository_root / ".sonarqube"
    if not metadata_root.is_dir():
        raise RunnerError("SonarScanner did not create metadata.")
    found: dict[str, list[tuple[str, str]]] = {"sonar.projectKey": [], "sonar.scm.revision": []}
    for path in metadata_root.rglob("*"):
        if not path.is_file() or path.is_symlink() or path.suffix.lower() not in {".xml", ".properties", ".txt"}:
            continue
        relative = str(path.relative_to(repository_root)).replace("\\", "/")
        try:
            if path.suffix.lower() == ".xml":
                root = ElementTree.parse(path).getroot()
                for element in root.iter():
                    name = element.attrib.get("Name") or element.attrib.get("name") or element.attrib.get("key")
                    if name in found and element.text:
                        found[name].append((relative, element.text.strip()))
            else:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    name, separator, value = line.partition("=")
                    if separator and name.strip() in found:
                        found[name.strip()].append((relative, value.strip()))
        except (OSError, ElementTree.ParseError) as error:
            raise RunnerError("SonarScanner metadata could not be parsed.") from error
    observed_project_keys = {value for _, value in found["sonar.projectKey"]}
    observed_revisions = {value for _, value in found["sonar.scm.revision"]}
    if observed_project_keys != {PROJECT_KEY} or observed_revisions != {expected_head}:
        raise RunnerError("Observed SonarScanner metadata does not bind the fixed project and exact HEAD.")
    return {
        "observed": True,
        "project_key": PROJECT_KEY,
        "sonar_scm_revision": expected_head,
        "files": sorted({path for values in found.values() for path, _ in values}),
    }


def report_task(repository_root: Path, expected_host: str) -> dict[str, Any]:
    path = repository_root / ".sonarqube" / "out" / ".sonar" / "report-task.txt"
    if not path.is_file():
        raise RunnerError("SonarScanner did not create report-task metadata.")
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    if values.get("projectKey") != PROJECT_KEY:
        raise RunnerError("SonarScanner report-task project key does not match the fixed project key.")
    if not values.get("ceTaskId"):
        raise RunnerError("SonarScanner report-task lacks its Compute Engine task ID.")
    if credential_free_host(values.get("serverUrl", "")) != expected_host:
        raise RunnerError("SonarScanner report-task server origin does not match SONAR_HOST_URL.")
    return {
        "observed": True,
        "path": str(path.relative_to(repository_root)).replace("\\", "/"),
        "project_key": PROJECT_KEY,
        "ce_task_id": values["ceTaskId"],
        "server_url": expected_host,
        "dashboard_url": values.get("dashboardUrl", ""),
    }


def api_json(host: str, endpoint: str, parameters: Mapping[str, str], token: str) -> dict[str, Any]:
    query = urllib.parse.urlencode(parameters)
    request = urllib.request.Request(
        f"{host}{endpoint}?{query}" if query else f"{host}{endpoint}",
        headers={"Accept": "application/json", "Authorization": f"Bearer {token}"},
    )
    try:
        with API_OPENER.open(request, timeout=30) as response:
            response_url = response.geturl()
            if response_origin(response_url) != host:
                raise RunnerError("Refusing an API response whose origin differs from SONAR_HOST_URL.")
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as error:
        input_name = "SONAR_TOKEN" if endpoint == "/api/ce/task" else "SONAR_READ_TOKEN"
        raise ApiHttpError(endpoint, error.code, input_name) from error
    except RunnerError:
        raise
    except (OSError, ValueError) as error:
        raise CredentialsUnavailable("SONAR_HOST_URL") from error
    try:
        decoded = json.loads(payload)
    except json.JSONDecodeError as error:
        raise RunnerError(f"Sonar API {endpoint} returned invalid JSON.") from error
    if not isinstance(decoded, dict):
        raise RunnerError(f"Sonar API {endpoint} returned an invalid payload.")
    return decoded


def wait_for_ce_task(host: str, task_id: str, token: str, receipt: dict[str, Any]) -> str:
    deadline = time.monotonic() + CE_TIMEOUT_SECONDS
    deadline_at = datetime.now(timezone.utc) + timedelta(seconds=CE_TIMEOUT_SECONDS)
    receipt["compute_engine"] = {
        "submitted_task_id": task_id,
        "task_id": task_id,
        "poll_started_at": utc_now(),
        "poll_deadline_at": deadline_at.isoformat().replace("+00:00", "Z"),
        "timeout_seconds": CE_TIMEOUT_SECONDS,
        "last_observed_state": "NO_RESPONSE",
        "states": [],
    }
    while True:
        response = api_json(host, "/api/ce/task", {"id": task_id}, token)
        task = response.get("task")
        if not isinstance(task, dict) or not isinstance(task.get("status"), str):
            raise RunnerError("Submitted Compute Engine task response is malformed.")
        if task.get("id") != task_id:
            raise RunnerError("Compute Engine response does not match the submitted task ID.")
        receipt["compute_engine"]["returned_task_id"] = task["id"]
        status = task["status"]
        receipt["compute_engine"]["states"].append({"at": utc_now(), "status": status})
        receipt["compute_engine"]["last_observed_state"] = status
        component_key = task.get("componentKey")
        if component_key != PROJECT_KEY:
            raise RunnerError("Submitted Compute Engine task does not prove the fixed project component key.")
        receipt["compute_engine"]["component_key"] = component_key
        if status == "SUCCESS":
            analysis_id = task.get("analysisId")
            if not isinstance(analysis_id, str) or not analysis_id:
                raise RunnerError("Successful Compute Engine task has no analysis ID.")
            receipt["compute_engine"]["analysis_id"] = analysis_id
            receipt["compute_engine"]["completed_at"] = utc_now()
            return analysis_id
        if status in {"FAILED", "CANCELED"}:
            raise RunnerError(f"Submitted Compute Engine task ended as {status}.")
        if time.monotonic() >= deadline:
            raise RunnerError("Submitted Compute Engine task did not complete before the 10-minute deadline.")
        time.sleep(POLL_SECONDS)


def current_analysis_binding(host: str, analysis_id: str, head: str, token: str) -> dict[str, Any]:
    query = {"project": PROJECT_KEY, "p": "1", "ps": "1"}
    response = api_json(
        host,
        "/api/project_analyses/search",
        query,
        token,
    )
    analyses = response.get("analyses")
    if not isinstance(analyses, list) or len(analyses) != 1 or not isinstance(analyses[0], dict):
        raise RunnerError("Current project-analysis response is incomplete.")
    analysis = analyses[0]
    if analysis.get("key") != analysis_id or analysis.get("revision") != head:
        raise RunnerError("Submitted analysis is not the current exact-head project analysis.")
    return {
        "observed": True,
        "analysis_id": analysis_id,
        "query": query,
        "revision": head,
        "date": analysis.get("date") if isinstance(analysis.get("date"), str) else None,
        "current": True,
    }


def analysis_quality_gate(host: str, analysis_id: str, token: str) -> dict[str, Any]:
    response = api_json(
        host,
        "/api/qualitygates/project_status",
        {"analysisId": analysis_id},
        token,
    )
    project_status = response.get("projectStatus")
    if not isinstance(project_status, dict) or not isinstance(project_status.get("status"), str):
        raise RunnerError("Analysis-bound quality-gate response is malformed.")
    conditions = project_status.get("conditions")
    if not isinstance(conditions, list):
        raise RunnerError("Analysis-bound quality-gate conditions are missing.")
    return {
        "analysis_id": analysis_id,
        "status": project_status["status"],
        "conditions": [
            {
                key: condition.get(key)
                for key in ("status", "metricKey", "comparator", "errorThreshold", "actualValue")
            }
            for condition in conditions
            if isinstance(condition, dict)
        ],
        "ignored_conditions": project_status.get("ignoredConditions"),
        "cayc_status": project_status.get("caycStatus"),
    }


def require_ok_quality_gate(gate: Mapping[str, Any]) -> None:
    if gate.get("status") != "OK":
        raise RunnerError(f"Analysis-bound quality gate is {gate.get('status')}; only OK passes.")


def indexed_api_json(host: str, endpoint: str, parameters: Mapping[str, str], token: str) -> dict[str, Any]:
    deadline = time.monotonic() + INDEX_TIMEOUT_SECONDS
    while True:
        try:
            return api_json(host, endpoint, parameters, token)
        except ApiHttpError as error:
            if error.status != 503 or time.monotonic() >= deadline:
                raise
            time.sleep(POLL_SECONDS)


def paginated_inventory(
    host: str,
    endpoint: str,
    collection_name: str,
    base_parameters: Mapping[str, str],
    token: str,
    fields: Sequence[str],
) -> dict[str, Any]:
    page = 1
    total: int | None = None
    pages: list[dict[str, int]] = []
    records: list[dict[str, Any]] = []
    while True:
        parameters = {**base_parameters, "p": str(page), "ps": str(PAGE_SIZE)}
        response = indexed_api_json(host, endpoint, parameters, token)
        paging = response.get("paging")
        raw_records = response.get(collection_name)
        if not isinstance(paging, dict) or not isinstance(raw_records, list):
            raise RunnerError(f"{endpoint} response is malformed.")
        page_index, page_size, page_total = paging.get("pageIndex"), paging.get("pageSize"), paging.get("total")
        if (
            not isinstance(page_index, int)
            or not isinstance(page_size, int)
            or not isinstance(page_total, int)
            or page_index != page
            or page_size <= 0
            or page_total < 0
        ):
            raise RunnerError(f"{endpoint} pagination metadata is invalid.")
        if page_total >= RESULT_CAP:
            raise RunnerError(f"{endpoint} reached its possible server result cap.")
        if total is None:
            total = page_total
        elif total != page_total:
            raise RunnerError(f"{endpoint} total changed during pagination.")
        if page_index * page_size < total and not raw_records:
            raise RunnerError(f"{endpoint} returned an empty nonterminal page.")
        pages.append({"page_index": page_index, "page_size": page_size, "total": page_total})
        for raw_record in raw_records:
            if not isinstance(raw_record, dict) or not isinstance(raw_record.get("key"), str):
                raise RunnerError(f"{endpoint} returned an invalid record.")
            records.append({field: raw_record.get(field) for field in fields})
        if page_index * page_size >= total:
            break
        page += 1
    if total is None or len(records) != total or len({record["key"] for record in records}) != len(records):
        raise RunnerError(f"{endpoint} pagination is incomplete or non-unique.")
    return {
        "endpoint": endpoint,
        "query": dict(base_parameters),
        "total": total,
        "pages": pages,
        "pagination_complete": True,
        "result_empty": total == 0,
        "records": records,
    }


def issue_inventory(host: str, token: str) -> dict[str, Any]:
    return paginated_inventory(
        host,
        "/api/issues/search",
        "issues",
        {"components": PROJECT_KEY, "issueStatuses": ISSUE_STATUSES},
        token,
        ("key", "rule", "severity", "status", "issueStatus", "resolution", "type", "component", "line", "impacts"),
    )


def hotspot_inventory(host: str, token: str) -> dict[str, Any]:
    # Live SonarQube 26.8 schema requires project; projectKey's empty response is not scope proof.
    return paginated_inventory(
        host,
        "/api/hotspots/search",
        "hotspots",
        {"project": PROJECT_KEY},
        token,
        ("key", "ruleKey", "status", "resolution", "component", "line", "vulnerabilityProbability"),
    )


def issue_dispositions(before: Mapping[str, Any], after: Mapping[str, Any]) -> dict[str, Any]:
    before_records = {record["key"]: record for record in before["records"]}
    after_records = {record["key"]: record for record in after["records"]}
    dispositions: list[dict[str, Any]] = []
    blocking = 0
    for key in sorted(set(before_records) | set(after_records)):
        current = after_records.get(key)
        if current is None:
            disposition = "FIXED_IN_CURRENT_HEAD"
        elif current.get("issueStatus") == "FIXED" and current.get("resolution") in {None, "FIXED"}:
            disposition = "FIXED_IN_CURRENT_HEAD"
        else:
            disposition = "BLOCKING_DISPOSITION"
            blocking += 1
        dispositions.append(
            {
                "key": key,
                "baseline_present": key in before_records,
                "current_present": current is not None,
                "issue_status": current.get("issueStatus") if current else None,
                "status": current.get("status") if current else None,
                "resolution": current.get("resolution") if current else None,
                "disposition": disposition,
            }
        )
    return {"blocking_count": blocking, "items": dispositions}


def hotspot_dispositions(inventory: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "blocking_count": inventory["total"],
        "items": [
            {
                "key": record["key"],
                "status": record.get("status"),
                "resolution": record.get("resolution"),
                "disposition": "BLOCKING_HOTSPOT",
            }
            for record in inventory["records"]
        ],
    }


def lock_path(coordination_root: Path) -> Path:
    return coordination_root / ".agent" / "e" / "sonarqube" / PROJECT_KEY / ".scan.lock"


def configure_windows_process_api(kernel32: Any, wintypes: Any) -> None:
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL


def windows_owner_is_alive(pid: int) -> bool | None:
    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_object_0 = 0x00000000
    wait_timeout = 0x00000102
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    configure_windows_process_api(kernel32, wintypes)
    handle = kernel32.OpenProcess(synchronize, False, pid)
    if not handle:
        error = ctypes.get_last_error()
        return True if error == 5 else False if error == 87 else None
    try:
        result = kernel32.WaitForSingleObject(handle, 0)
        return True if result == wait_timeout else False if result == wait_object_0 else None
    finally:
        kernel32.CloseHandle(handle)

def owner_is_alive(pid: int) -> bool | None:
    if pid <= 0:
        return False
    if os.name == "nt":
        return windows_owner_is_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return None
    return True


def reclaim_stale_lock(path: Path) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict) and isinstance(payload.get("pid"), int):
        alive = owner_is_alive(payload["pid"])
        if alive is not False:
            return False
    else:
        try:
            if time.time() - path.stat().st_mtime < LOCK_LEASE_SECONDS:
                return False
        except OSError:
            return False
    try:
        path.unlink()
    except FileNotFoundError:
        pass
    return True


@contextmanager
def project_lock(coordination_root: Path, role: str, head: str, run_id: str) -> Iterator[None]:
    path = lock_path(coordination_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            descriptor = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            break
        except FileExistsError as error:
            if not reclaim_stale_lock(path):
                raise RunnerError("Another exact-head SonarQube scan holds the project lock.") from error
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as lock_file:
            json.dump(
                {
                    "run_id": run_id,
                    "pid": os.getpid(),
                    "role": role,
                    "head": head,
                    "started_at": utc_now(),
                    "lease_seconds": LOCK_LEASE_SECONDS,
                },
                lock_file,
            )
        yield
    finally:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            if payload.get("run_id") == run_id:
                path.unlink()
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            pass


def receipt_path(context: GitContext, role: str) -> Path:
    return context.coordination_root / ".agent" / "e" / "sonarqube" / PROJECT_KEY / context.head / f"{role}.json"


def assert_secret_free(receipt: Mapping[str, Any], secrets: Sequence[str]) -> None:
    encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False)
    if any(secret and secret in encoded for secret in secrets):
        raise RunnerError("Refusing to write a receipt containing a SonarQube credential.")


def write_receipt(path: Path, receipt: Mapping[str, Any], secrets: Sequence[str]) -> None:
    assert_secret_free(receipt, secrets)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            json.dump(receipt, temporary, indent=2, sort_keys=True)
            temporary.write("\n")
            temporary_path = Path(temporary.name)
        os.replace(temporary_path, path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


def validate_inventory(inventory: Any, endpoint: str, expected_query: Mapping[str, str]) -> None:
    required = ("endpoint", "query", "total", "pages", "pagination_complete", "result_empty", "records")
    if (
        not isinstance(inventory, dict)
        or any(key not in inventory for key in required)
        or inventory.get("endpoint") != endpoint
        or inventory.get("query") != dict(expected_query)
        or type(inventory.get("total")) is not int
        or inventory["total"] < 0
        or not isinstance(inventory.get("pages"), list)
        or not inventory["pages"]
        or not isinstance(inventory.get("records"), list)
        or len(inventory["records"]) != inventory["total"]
        or type(inventory.get("result_empty")) is not bool
        or inventory["result_empty"] != (inventory["total"] == 0)
        or inventory.get("pagination_complete") is not True
    ):
        raise RunnerError("PASS receipt lacks complete inventory evidence.")
    keys: list[str] = []
    for record in inventory["records"]:
        if not isinstance(record, dict) or not isinstance(record.get("key"), str) or not record["key"]:
            raise RunnerError("PASS receipt inventory has an invalid record key.")
        keys.append(record["key"])
    if len(keys) != len(set(keys)):
        raise RunnerError("PASS receipt inventory has duplicate record keys.")
    for position, page in enumerate(inventory["pages"], start=1):
        if (
            not isinstance(page, dict)
            or type(page.get("page_index")) is not int
            or type(page.get("page_size")) is not int
            or type(page.get("total")) is not int
            or page["page_index"] != position
            or not 0 < page["page_size"] <= PAGE_SIZE
            or page["total"] != inventory["total"]
        ):
            raise RunnerError("PASS receipt inventory has inconsistent pagination evidence.")
        covered = page["page_index"] * page["page_size"] >= inventory["total"]
        if (position == len(inventory["pages"])) != covered:
            raise RunnerError("PASS receipt inventory lacks terminal pagination coverage.")


def validate_issue_dispositions(before: Mapping[str, Any], after: Mapping[str, Any], dispositions: Any) -> None:
    if not isinstance(dispositions, dict) or not isinstance(dispositions.get("items"), list) or type(dispositions.get("blocking_count")) is not int:
        raise RunnerError("PASS receipt lacks issue-disposition evidence.")
    before_by_key = {record["key"]: record for record in before["records"]}
    after_by_key = {record["key"]: record for record in after["records"]}
    expected_keys = set(before_by_key) | set(after_by_key)
    item_by_key: dict[str, Mapping[str, Any]] = {}
    for item in dispositions["items"]:
        if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"] or item["key"] in item_by_key:
            raise RunnerError("PASS receipt issue dispositions have invalid keys.")
        item_by_key[item["key"]] = item
    if set(item_by_key) != expected_keys:
        raise RunnerError("PASS receipt issue dispositions do not cover the exact inventory-key union.")
    expected_blocking = 0
    for key in expected_keys:
        current = after_by_key.get(key)
        expected_disposition = (
            "FIXED_IN_CURRENT_HEAD"
            if current is None or (current.get("issueStatus") == "FIXED" and current.get("resolution") in {None, "FIXED"})
            else "BLOCKING_DISPOSITION"
        )
        if item_by_key[key].get("disposition") != expected_disposition:
            raise RunnerError("PASS receipt issue disposition does not match the observed current issue.")
        expected_blocking += expected_disposition == "BLOCKING_DISPOSITION"
    if dispositions["blocking_count"] != expected_blocking:
        raise RunnerError("PASS receipt issue blocking count does not match observed dispositions.")


def validate_hotspot_dispositions(inventory: Mapping[str, Any], dispositions: Any) -> None:
    if not isinstance(dispositions, dict) or not isinstance(dispositions.get("items"), list) or type(dispositions.get("blocking_count")) is not int:
        raise RunnerError("PASS receipt lacks hotspot-disposition evidence.")
    expected_keys = {record["key"] for record in inventory["records"]}
    items = dispositions["items"]
    keys = [item.get("key") for item in items if isinstance(item, dict)]
    if len(keys) != len(items) or any(not isinstance(key, str) or not key for key in keys) or len(set(keys)) != len(keys) or set(keys) != expected_keys:
        raise RunnerError("PASS receipt hotspot dispositions do not cover the exact inventory keys.")
    if any(item.get("disposition") != "BLOCKING_HOTSPOT" for item in items) or dispositions["blocking_count"] != len(expected_keys):
        raise RunnerError("PASS receipt hotspot blocking count does not match observed hotspots.")


def validate_pass_receipt(receipt: Mapping[str, Any]) -> None:
    required = (
        "run_id", "role", "project_key", "analysis_xml_project_key", "captured_head",
        "completed_at", "worktree", "cleanliness", "scanner_metadata", "task_report",
        "compute_engine", "analysis_current_before_issues", "analysis_current_after_issues",
        "analysis_current_final", "quality_gate", "pre_scan_issues", "post_scan_issues",
        "issue_dispositions", "hotspots", "hotspot_dispositions", "post_scan_head",
    )
    if (
        receipt.get("schema_version") != RECEIPT_SCHEMA_VERSION
        or receipt.get("outcome") != "PASS"
        or receipt.get("project_key") != PROJECT_KEY
        or receipt.get("analysis_xml_project_key") != PROJECT_KEY
        or receipt.get("role") not in {"candidate", "post-merge"}
        or not isinstance(receipt.get("captured_head"), str)
        or not SHA_RE.fullmatch(receipt["captured_head"])
        or not isinstance(receipt.get("completed_at"), str)
        or not receipt["completed_at"]
        or any(key not in receipt for key in required)
    ):
        raise RunnerError("PASS receipt does not satisfy the exact-head evidence schema.")
    worktree = receipt["worktree"]
    cleanliness = receipt["cleanliness"]
    scanner = receipt["scanner_metadata"]
    task_report = receipt["task_report"]
    compute_engine = receipt["compute_engine"]
    if (
        not isinstance(worktree, dict)
        or not worktree.get("detached")
        or not worktree.get("linked")
        or not all(isinstance(worktree.get(key), str) and worktree[key] for key in ("repository_root", "git_dir", "common_dir", "coordination_root"))
    ):
        raise RunnerError("PASS receipt lacks detached linked-worktree evidence.")
    if not isinstance(cleanliness, dict) or cleanliness.get("pre", {}).get("status") != "clean" or cleanliness.get("post", {}).get("status") != "clean":
        raise RunnerError("PASS receipt lacks clean-worktree evidence.")
    if not isinstance(scanner, dict) or not scanner.get("observed") or scanner.get("project_key") != PROJECT_KEY or scanner.get("sonar_scm_revision") != receipt.get("captured_head"):
        raise RunnerError("PASS receipt lacks observed scanner project/revision evidence.")
    if not isinstance(task_report, dict) or not task_report.get("observed") or task_report.get("project_key") != PROJECT_KEY or not task_report.get("ce_task_id") or not task_report.get("server_url"):
        raise RunnerError("PASS receipt lacks observed task-report evidence.")
    if (
        not isinstance(compute_engine, dict)
        or task_report.get("ce_task_id") != compute_engine.get("submitted_task_id")
        or compute_engine.get("submitted_task_id") != compute_engine.get("task_id")
        or compute_engine.get("task_id") != compute_engine.get("returned_task_id")
        or compute_engine.get("component_key") != PROJECT_KEY
        or not isinstance(compute_engine.get("analysis_id"), str)
        or not compute_engine["analysis_id"]
        or not isinstance(compute_engine.get("poll_deadline_at"), str)
        or not compute_engine["poll_deadline_at"]
        or not isinstance(compute_engine.get("last_observed_state"), str)
        or not isinstance(compute_engine.get("states"), list)
        or not compute_engine["states"]
        or compute_engine["states"][-1].get("status") != "SUCCESS"
    ):
        raise RunnerError("PASS receipt lacks successful fixed-project submitted-task evidence.")
    quality_gate = receipt["quality_gate"]
    if not isinstance(quality_gate, dict) or quality_gate.get("status") != "OK" or quality_gate.get("analysis_id") != compute_engine.get("analysis_id"):
        raise RunnerError("PASS receipt has an invalid analysis-bound quality gate.")
    for key in ("analysis_current_before_issues", "analysis_current_after_issues", "analysis_current_final"):
        binding = receipt[key]
        if (
            not isinstance(binding, dict)
            or not binding.get("observed")
            or not binding.get("current")
            or binding.get("query") != {"project": PROJECT_KEY, "p": "1", "ps": "1"}
            or binding.get("analysis_id") != compute_engine.get("analysis_id")
            or binding.get("revision") != receipt.get("captured_head")
        ):
            raise RunnerError("PASS receipt lacks current fixed-project exact-head analysis binding evidence.")
    validate_inventory(
        receipt["pre_scan_issues"],
        "/api/issues/search",
        {"components": PROJECT_KEY, "issueStatuses": ISSUE_STATUSES},
    )
    validate_inventory(
        receipt["post_scan_issues"],
        "/api/issues/search",
        {"components": PROJECT_KEY, "issueStatuses": ISSUE_STATUSES},
    )
    validate_inventory(receipt["hotspots"], "/api/hotspots/search", {"project": PROJECT_KEY})
    validate_issue_dispositions(
        receipt["pre_scan_issues"], receipt["post_scan_issues"], receipt["issue_dispositions"]
    )
    validate_hotspot_dispositions(receipt["hotspots"], receipt["hotspot_dispositions"])
    if receipt["issue_dispositions"]["blocking_count"] != 0 or receipt["hotspot_dispositions"]["blocking_count"] != 0:
        raise RunnerError("PASS receipt has unremediated issue or hotspot evidence.")
    if receipt.get("post_scan_head") != receipt.get("captured_head"):
        raise RunnerError("PASS receipt has a post-scan HEAD mismatch.")

def receipt_base(context: GitContext, role: str, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "outcome": "RUNNING",
        "run_id": run_id,
        "project_key": PROJECT_KEY,
        "role": role,
        "captured_head": context.head,
        "started_at": utc_now(),
        "worktree": {
            "repository_root": str(context.repository_root),
            "git_dir": str(context.git_dir),
            "common_dir": str(context.common_dir),
            "coordination_root": str(context.coordination_root),
            "detached": True,
            "linked": True,
        },
    }


def execute(role: str, scanner_override: str | None) -> Path:
    inherited_environment = dict(os.environ)
    clean_environment = scrub_sonar_environment(inherited_environment)
    context = git_context(Path.cwd(), clean_environment)
    run_id = str(uuid.uuid4())
    target_receipt = receipt_path(context, role)
    receipt = receipt_base(context, role, run_id)
    secrets = tuple(
        value
        for name in ("SONAR_TOKEN", "SONAR_READ_TOKEN", "SONAR_ADMIN_TOKEN")
        if (value := inherited_environment.get(name))
    )

    with project_lock(context.coordination_root, role, context.head, run_id):
        write_receipt(target_receipt, receipt, secrets)
        try:
            credentials = load_credentials(context.repository_root, inherited_environment)
            secrets = (credentials["SONAR_TOKEN"], credentials["SONAR_READ_TOKEN"])
            receipt["credential_inputs"] = list(REQUIRED_ENV)
            if role == "post-merge":
                assert_post_merge_target(context, clean_environment)
            receipt["generated_artifacts_removed_before_scan"] = clear_generated_artifacts(
                context, clean_environment
            )
            receipt["cleanliness"] = {"pre": strict_cleanliness(context, clean_environment, "scanner begin")}
            analysis_xml = context.repository_root / "SonarQube.Analysis.xml"
            receipt["analysis_xml_project_key"] = project_key_from_xml(analysis_xml)
            scanner = discover_scanner(scanner_override)
            receipt["scanner"] = scanner

            receipt["pre_scan_issues"] = issue_inventory(
                credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"]
            )
            scanner_env = scanner_environment(inherited_environment, credentials)
            run_process(
                scanner_begin_command(scanner, analysis_xml, credentials["SONAR_HOST_URL"], context.head),
                cwd=context.repository_root,
                environment=scanner_env,
                secrets=secrets,
                label="SonarScanner begin",
                credential_input_names=("SONAR_TOKEN",),
            )
            receipt["scanner_metadata"] = scanner_metadata(context.repository_root, context.head)

            solution, projects, standalone_projects = project_inventory(context.repository_root)
            receipt["build_inventory"] = {
                "solution": str(solution.relative_to(context.repository_root)),
                "projects": [str(project.relative_to(context.repository_root)) for project in projects],
                "standalone_projects": [
                    str(project.relative_to(context.repository_root)) for project in standalone_projects
                ],
            }
            run_process(
                ["dotnet", "build", str(solution)],
                cwd=context.repository_root,
                environment=clean_environment,
                secrets=secrets,
                label="Solution build",
            )
            for project in standalone_projects:
                run_process(
                    ["dotnet", "build", str(project)],
                    cwd=context.repository_root,
                    environment=clean_environment,
                    secrets=secrets,
                    label=f"Standalone project build ({project.name})",
                )
            run_process(
                [*scanner, "end"],
                cwd=context.repository_root,
                environment=scanner_env,
                secrets=secrets,
                label="SonarScanner end",
                credential_input_names=("SONAR_TOKEN",),
            )
            receipt["task_report"] = report_task(context.repository_root, credentials["SONAR_HOST_URL"])
            analysis_id = wait_for_ce_task(
                credentials["SONAR_HOST_URL"],
                receipt["task_report"]["ce_task_id"],
                credentials["SONAR_TOKEN"],
                receipt,
            )
            receipt["analysis_current_before_issues"] = current_analysis_binding(
                credentials["SONAR_HOST_URL"], analysis_id, context.head, credentials["SONAR_READ_TOKEN"]
            )
            receipt["quality_gate"] = analysis_quality_gate(
                credentials["SONAR_HOST_URL"], analysis_id, credentials["SONAR_READ_TOKEN"]
            )
            require_ok_quality_gate(receipt["quality_gate"])
            receipt["post_scan_issues"] = issue_inventory(
                credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"]
            )
            receipt["issue_dispositions"] = issue_dispositions(
                receipt["pre_scan_issues"], receipt["post_scan_issues"]
            )
            receipt["analysis_current_after_issues"] = current_analysis_binding(
                credentials["SONAR_HOST_URL"], analysis_id, context.head, credentials["SONAR_READ_TOKEN"]
            )
            receipt["hotspots"] = hotspot_inventory(
                credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"]
            )
            receipt["hotspot_dispositions"] = hotspot_dispositions(receipt["hotspots"])
            if receipt["issue_dispositions"]["blocking_count"]:
                raise RunnerError("Current project issues include a prohibited disposition.")
            if receipt["hotspot_dispositions"]["blocking_count"]:
                raise RunnerError("Current project contains security hotspots requiring disposition.")
            assert_head_unchanged(context, clean_environment)
            receipt["generated_artifacts_removed_after_scan"] = clear_generated_artifacts(
                context, clean_environment
            )
            receipt["cleanliness"]["post"] = strict_cleanliness(context, clean_environment, "receipt publication")
            receipt["analysis_current_final"] = current_analysis_binding(
                credentials["SONAR_HOST_URL"], analysis_id, context.head, credentials["SONAR_READ_TOKEN"]
            )
            assert_head_unchanged(context, clean_environment)
            receipt["post_scan_head"] = context.head
            receipt["completed_at"] = utc_now()
            receipt["outcome"] = "PASS"
            validate_pass_receipt(receipt)
            write_receipt(target_receipt, receipt, secrets)
        except ApiHttpError as error:
            if error.status in {401, 403}:
                credential_error = CredentialsUnavailable(error.input_name)
                receipt["outcome"] = "BLOCKED"
                receipt["failure"] = str(credential_error)
                receipt["completed_at"] = utc_now()
                write_receipt(target_receipt, receipt, secrets)
                raise credential_error from error
            receipt["outcome"] = "BLOCKED"
            receipt["failure"] = str(error)
            receipt["completed_at"] = utc_now()
            write_receipt(target_receipt, receipt, secrets)
            raise
        except RunnerError as error:
            receipt["outcome"] = "BLOCKED"
            receipt["failure"] = str(error)
            receipt["completed_at"] = utc_now()
            write_receipt(target_receipt, receipt, secrets)
            raise
        except Exception as error:
            receipt["outcome"] = "BLOCKED"
            receipt["failure"] = f"Unexpected runner failure: {error.__class__.__name__}."
            receipt["completed_at"] = utc_now()
            write_receipt(target_receipt, receipt, secrets)
            raise RunnerError(receipt["failure"]) from error
    return target_receipt


def main(argv: Sequence[str] | None = None) -> int:
    arguments = parse_args(argv)
    try:
        written_receipt = execute(arguments.role, arguments.scanner)
    except RunnerError as error:
        print(f"PROJECT_RELEASE_PROTOCOL_BLOCKED: {error}", file=sys.stderr)
        return 1
    print(f"PROJECT_RELEASE_PROTOCOL_PASS: receipt {written_receipt}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
