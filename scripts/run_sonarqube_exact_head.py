#!/usr/bin/env python3
"""Run one secret-free, exact-head SonarQube release scan."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import locale
import os
import re
import shutil
import stat
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
from typing import Any, Collection, Iterator, Mapping, Sequence

PROJECT_KEY = "thebtf_netcoredbg_mcp"
REQUIRED_ENV = ("SONAR_HOST_URL", "SONAR_TOKEN", "SONAR_READ_TOKEN")
SONAR_ENV = (*REQUIRED_ENV, "SONAR_ADMIN_TOKEN")
SIMPLE_DOTENV_ASSIGNMENT_RE = re.compile(r"(?P<name>[A-Z_][A-Z0-9_]*)=(?P<value>[^\r\n]*)\Z")
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
COVERAGE_PROCESS_CLEANUP_SECONDS = 5
COVERAGE_FAILURE_STAGES = frozenset(
    {
        "process_owner",
        "scanner_begin",
        "python_producer",
        "dotnet_producer",
        "report_validation",
        "scanner_end",
        "analysis_metrics",
        "cleanup",
    }
)
COVERAGE_ROOT_PARTS = (".tmp", "sonarqube-coverage")
CLOSED_DOTNET_COVERAGE_PROJECTS = (
    "host/NetCoreDbg.Mcp.CodeSearch.Core.Tests/NetCoreDbg.Mcp.CodeSearch.Core.Tests.csproj",
    "host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj",
    "host/NetCoreDbg.Mcp.Stateless.Preview.Tests/NetCoreDbg.Mcp.Stateless.Preview.Tests.csproj",
    "host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj",
    "tests/dotnet/NetCoreDbg.Mcp.Host.PromptTests/NetCoreDbg.Mcp.Host.PromptTests.csproj",
)
PROJECT_VERSION_ASSIGNMENT_RE = re.compile(r'^version\s*=\s*"(?P<version>[^"]+)"\s*(?:#.*)?$')
SEMVER_RE = re.compile(
    r"^(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)\.(?:0|[1-9]\d*)(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)


class RunnerError(RuntimeError):
    """A fail-closed release-gate error safe to place in a receipt."""


class CoverageFailure(RunnerError):
    """A secret-free, receipt-bindable coverage transaction failure."""

    def __init__(
        self,
        stage: str,
        language: str | None,
        failure_code: str,
        *,
        cleanup_failure: Mapping[str, str] | None = None,
        owner: CoverageTreeObservation | None = None,
        artifact_cleanup_permitted: bool = True,
    ) -> None:
        if stage not in COVERAGE_FAILURE_STAGES:
            raise ValueError("Coverage failure stage is invalid.")
        if language not in {None, "python", "dotnet"}:
            raise ValueError("Coverage failure language is invalid.")
        if not re.fullmatch(r"COVERAGE_[A-Z0-9_]+", failure_code):
            raise ValueError("Coverage failure code is invalid.")
        if (
            owner is not None
            and owner.terminal_state != "TREE_EMPTY"
            and artifact_cleanup_permitted
        ):
            raise ValueError("Unproven process ownership cannot permit artifact cleanup.")
        self.stage = stage
        self.language = language
        self.failure_code = failure_code
        self.cleanup_failure = dict(cleanup_failure) if cleanup_failure is not None else None
        self.owner = owner
        self.artifact_cleanup_permitted = artifact_cleanup_permitted
        super().__init__(failure_code)


@dataclass(frozen=True)
class CoverageTreeObservation:
    target_platform: str
    owner_kind: str
    terminal_state: str | None


@dataclass(frozen=True)
class CoverageProcessOutcome:
    returncode: int
    output: str
    owner: CoverageTreeObservation


class CoverageTreeOwnerUnavailable(RunnerError):
    def __init__(
        self,
        observation: CoverageTreeObservation | None = None,
        artifact_cleanup_permitted: bool = True,
    ) -> None:
        self.observation = observation
        self.artifact_cleanup_permitted = artifact_cleanup_permitted
        super().__init__("COVERAGE_PROCESS_TREE_OWNER_UNAVAILABLE")


class CoverageTreeOwnershipLost(RunnerError):
    def __init__(self, observation: CoverageTreeObservation) -> None:
        self.observation = observation
        super().__init__("COVERAGE_PROCESS_TREE_OWNERSHIP_LOST")


class CoverageTreeStartCancelledError(RunnerError):
    def __init__(
        self,
        observation: CoverageTreeObservation | None,
        artifact_cleanup_permitted: bool,
        cleanup_failure: Mapping[str, str] | None = None,
    ) -> None:
        self.observation = observation
        self.artifact_cleanup_permitted = artifact_cleanup_permitted
        self.cleanup_failure = dict(cleanup_failure) if cleanup_failure is not None else None
        super().__init__("COVERAGE_PROCESS_CANCELLED")


class CoverageTreeFinalizationError(RunnerError):
    def __init__(self, outcome: CoverageProcessOutcome, error: OSError) -> None:
        self.outcome = outcome
        self.cleanup_failure = {
            "path": "coverage-process-tree",
            "operation": "close",
            "error_type": error.__class__.__name__,
        }
        super().__init__("COVERAGE_PROCESS_TREE_FINALIZATION_FAILED")


class GeneratedArtifactCleanupError(RunnerError):
    """A receipt-safe failure deleting one generated scanner artifact."""

    def __init__(
        self, path: str, operation: str, error_type: str, removed: Sequence[str]
    ) -> None:
        self.path = path
        self.operation = operation
        self.error_type = error_type
        self.removed = list(removed)
        super().__init__(
            f"Generated artifact cleanup {operation} failed for {path}: {error_type}."
        )


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


@dataclass(frozen=True)
class CoverageReportPlan:
    language: str
    format: str
    absolute_path: Path
    normalized_path: str
    project: str | None
    slug: str | None


@dataclass(frozen=True)
class CoveragePlan:
    run_id: str
    root: Path
    marker_path: Path
    reports: tuple[CoverageReportPlan, ...]

    @property
    def dotnet_reports(self) -> tuple[CoverageReportPlan, ...]:
        return tuple(report for report in self.reports if report.language == "dotnet")

    @property
    def python_report(self) -> CoverageReportPlan:
        reports = tuple(report for report in self.reports if report.language == "python")
        if len(reports) != 1:
            raise RunnerError("Coverage plan does not contain exactly one Python report.")
        return reports[0]


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


def is_sonar_environment_name(name: str) -> bool:
    return name.upper().startswith("SONAR_")


def sonar_secret_values(source: Mapping[str, str]) -> set[str]:
    return {value for name, value in source.items() if is_sonar_environment_name(name) and value}


def dotenv_secret_values(content: str) -> set[str]:
    values: set[str] = set()
    for raw_line in content.splitlines():
        name, separator, value = raw_line.strip().partition("=")
        if separator and is_sonar_environment_name(name):
            values.add(value)
            values.add(value.strip())
    values.discard("")
    return values


def credential_free_host(value: str) -> str:
    try:
        parsed = urllib.parse.urlsplit(value)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise CredentialsUnavailable("SONAR_HOST_URL") from error
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or hostname is None
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path not in {"", "/"}
        or port == 0
        or (parsed.netloc.endswith(":") and not parsed.netloc.endswith("]"))
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


def normalize_windows_handle_for_crt(handle: Any, invalid_handle_value: int | None) -> int:
    normalized_handle = getattr(handle, "value", handle)
    if (
        invalid_handle_value is None
        or type(normalized_handle) is not int
        or normalized_handle == invalid_handle_value
    ):
        raise ValueError("The primary .env file handle is invalid.")
    return normalized_handle


def close_windows_handle_if_owned(
    kernel32: Any, handle: Any | None, invalid_handle_value: int | None
) -> None:
    normalized_handle = getattr(handle, "value", handle)
    if normalized_handle is None or normalized_handle == 0 or normalized_handle == invalid_handle_value:
        return
    kernel32.CloseHandle(handle)


def _scanner_tree_metadata(path: Path) -> Any:
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as error:
        raise RunnerError("Unable to inspect scanner worktree metadata.") from error
    if stat.S_ISLNK(getattr(metadata, "st_mode", 0)):
        raise RunnerError("Refusing a symbolic link in the scanner worktree.")
    if getattr(metadata, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
        raise RunnerError("Refusing a reparse point in the scanner worktree.")
    return metadata


def iter_scanner_tree(root: Path, pattern: str) -> Iterator[Path]:
    root_metadata = _scanner_tree_metadata(root)
    if not stat.S_ISDIR(getattr(root_metadata, "st_mode", 0)):
        return
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            for path in directory.iterdir():
                metadata = _scanner_tree_metadata(path)
                if path.match(pattern):
                    yield path
                if stat.S_ISDIR(getattr(metadata, "st_mode", 0)):
                    pending.append(path)
        except OSError as error:
            raise RunnerError("Unable to enumerate the scanner worktree.") from error


def assert_no_in_tree_dotenv(repository_root: Path) -> None:
    for scanner_path in iter_scanner_tree(repository_root, "*"):
        if scanner_path.name == ".env":
            raise RunnerError("Refusing an in-tree .env in the scanner worktree.")


def _read_posix_verified_primary_dotenv(dotenv_path: Path) -> str:
    no_follow = getattr(os, "O_NOFOLLOW", None)
    if no_follow is None:
        raise OSError("The platform cannot open the primary .env without following links.")
    descriptor = os.open(dotenv_path, os.O_RDONLY | no_follow)
    try:
        file_status = os.fstat(descriptor)
        if not stat.S_ISREG(file_status.st_mode):
            raise OSError("The primary .env is not a regular file.")
        if file_status.st_uid != getattr(os, "geteuid")() or stat.S_IMODE(file_status.st_mode) & 0o077:
            raise PermissionError("The primary .env is not owner-only.")
        chunks: list[bytes] = []
        while chunk := os.read(descriptor, 64 * 1024):
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")
    finally:
        os.close(descriptor)


def _read_windows_verified_primary_dotenv(dotenv_path: Path) -> str:
    import ctypes
    import msvcrt
    from ctypes import wintypes

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = [("file_attributes", wintypes.DWORD), ("reparse_tag", wintypes.DWORD)]

    class AceHeader(ctypes.Structure):
        _fields_ = [("ace_type", ctypes.c_ubyte), ("ace_flags", ctypes.c_ubyte), ("ace_size", wintypes.WORD)]

    class AclSizeInformation(ctypes.Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        ]

    class SidAndAttributes(ctypes.Structure):
        _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

    class TokenUser(ctypes.Structure):
        _fields_ = [("user", SidAndAttributes)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    invalid_handle_value = ctypes.c_void_p(-1).value
    file_read_data = 0x0001
    read_control = 0x00020000
    file_share_read = 0x00000001
    open_existing = 3
    file_flag_open_reparse_point = 0x00200000
    file_attribute_reparse_point = 0x00000400
    file_attribute_directory = 0x00000010
    file_type_disk = 0x0001
    file_attribute_tag_info = 9
    owner_security_information = 0x00000001
    dacl_security_information = 0x00000004
    se_file_object = 1
    se_dacl_protected = 0x1000
    token_query = 0x0008
    token_user = 1
    error_insufficient_buffer = 122
    access_allowed_ace_type = 0
    access_denied_ace_types = {1, 6, 10, 12}
    inherited_ace = 0x10
    acl_size_information = 2
    sid_offset = ctypes.sizeof(AceHeader) + ctypes.sizeof(wintypes.DWORD)

    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandleEx.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
    ]
    kernel32.GetFileInformationByHandleEx.restype = wintypes.BOOL
    kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    kernel32.GetFileType.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [ctypes.c_void_p]
    kernel32.LocalFree.restype = ctypes.c_void_p
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetSecurityInfo.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi32.GetSecurityInfo.restype = wintypes.DWORD
    advapi32.GetSecurityDescriptorDacl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.BOOL),
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.POINTER(wintypes.BOOL),
    ]
    advapi32.GetSecurityDescriptorDacl.restype = wintypes.BOOL
    advapi32.GetSecurityDescriptorControl.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(wintypes.WORD),
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi32.GetSecurityDescriptorControl.restype = wintypes.BOOL
    advapi32.GetAclInformation.argtypes = [
        ctypes.c_void_p,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.c_int,
    ]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(ctypes.c_void_p)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.IsValidSid.argtypes = [ctypes.c_void_p]
    advapi32.IsValidSid.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [ctypes.c_void_p]
    advapi32.GetLengthSid.restype = wintypes.DWORD
    advapi32.EqualSid.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
    advapi32.EqualSid.restype = wintypes.BOOL

    def require(success: bool) -> None:
        if not success:
            raise ctypes.WinError(ctypes.get_last_error())

    def current_user_sid() -> tuple[ctypes.c_void_p, ctypes.Array[Any]]:
        token_handle = wintypes.HANDLE()
        require(advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), token_query, ctypes.byref(token_handle)))
        try:
            required_size = wintypes.DWORD()
            if advapi32.GetTokenInformation(token_handle, token_user, None, 0, ctypes.byref(required_size)):
                raise OSError("The current process token returned no user SID.")
            if ctypes.get_last_error() != error_insufficient_buffer or not required_size.value:
                raise ctypes.WinError(ctypes.get_last_error())
            buffer = ctypes.create_string_buffer(required_size.value)
            require(
                advapi32.GetTokenInformation(
                    token_handle,
                    token_user,
                    buffer,
                    required_size.value,
                    ctypes.byref(required_size),
                )
            )
            sid = ctypes.c_void_p(ctypes.cast(buffer, ctypes.POINTER(TokenUser)).contents.user.sid)
            if not sid.value or not advapi32.IsValidSid(sid):
                raise OSError("The current process token user SID is invalid.")
            return sid, buffer
        finally:
            require(kernel32.CloseHandle(token_handle))

    handle: Any | None = None
    try:
        handle = kernel32.CreateFileW(
            str(dotenv_path),
            file_read_data | read_control,
            file_share_read,
            None,
            open_existing,
            file_flag_open_reparse_point,
            None,
        )
        if handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            normalize_windows_handle_for_crt(handle, invalid_handle_value)
        except ValueError as error:
            raise ctypes.WinError(ctypes.get_last_error()) from error
        if kernel32.GetFileType(handle) != file_type_disk:
            raise OSError("The primary .env is not a disk file.")
        attribute_tag = FileAttributeTagInfo()
        require(
            kernel32.GetFileInformationByHandleEx(
                handle,
                file_attribute_tag_info,
                ctypes.byref(attribute_tag),
                ctypes.sizeof(attribute_tag),
            )
        )
        if attribute_tag.file_attributes & (file_attribute_reparse_point | file_attribute_directory):
            raise OSError("The primary .env is not a regular non-reparse file.")
        owner_sid = ctypes.c_void_p()
        dacl = ctypes.c_void_p()
        security_descriptor = ctypes.c_void_p()
        result = advapi32.GetSecurityInfo(
            handle,
            se_file_object,
            owner_security_information | dacl_security_information,
            ctypes.byref(owner_sid),
            None,
            ctypes.byref(dacl),
            None,
            ctypes.byref(security_descriptor),
        )
        if result:
            raise ctypes.WinError(result)
        try:
            user_sid, user_sid_buffer = current_user_sid()
            if not owner_sid.value or not advapi32.IsValidSid(owner_sid) or not advapi32.EqualSid(owner_sid, user_sid):
                raise PermissionError("The primary .env owner does not match the current token user.")
            dacl_present = wintypes.BOOL()
            dacl_defaulted = wintypes.BOOL()
            descriptor_dacl = ctypes.c_void_p()
            require(
                advapi32.GetSecurityDescriptorDacl(
                    security_descriptor,
                    ctypes.byref(dacl_present),
                    ctypes.byref(descriptor_dacl),
                    ctypes.byref(dacl_defaulted),
                )
            )
            if not dacl_present.value or not dacl.value or dacl.value != descriptor_dacl.value:
                raise PermissionError("The primary .env has no explicit DACL.")
            control = wintypes.WORD()
            revision = wintypes.DWORD()
            require(
                advapi32.GetSecurityDescriptorControl(
                    security_descriptor,
                    ctypes.byref(control),
                    ctypes.byref(revision),
                )
            )
            if not control.value & se_dacl_protected:
                raise PermissionError("The primary .env DACL is not protected from inherited access.")
            acl_information = AclSizeInformation()
            require(
                advapi32.GetAclInformation(
                    dacl,
                    ctypes.byref(acl_information),
                    ctypes.sizeof(acl_information),
                    acl_size_information,
                )
            )
            for index in range(acl_information.ace_count):
                ace = ctypes.c_void_p()
                require(advapi32.GetAce(dacl, index, ctypes.byref(ace)))
                ace_address = ace.value
                if ace_address is None:
                    raise OSError("The primary .env DACL has a null ACE pointer.")
                header = ctypes.cast(ace, ctypes.POINTER(AceHeader)).contents
                if header.ace_type in access_denied_ace_types:
                    continue
                if (
                    header.ace_type != access_allowed_ace_type
                    or header.ace_flags & inherited_ace
                    or header.ace_size < sid_offset
                ):
                    raise PermissionError("The primary .env has an unsupported effective DACL ACE.")
                ace_sid = ctypes.c_void_p(ace_address + sid_offset)
                if not advapi32.IsValidSid(ace_sid):
                    raise OSError("The primary .env DACL has an invalid allow ACE SID.")
                sid_length = advapi32.GetLengthSid(ace_sid)
                if not sid_length or header.ace_size < sid_offset + sid_length:
                    raise OSError("The primary .env DACL allow ACE is malformed.")
                if not advapi32.EqualSid(ace_sid, user_sid):
                    raise PermissionError("The primary .env grants access outside the current token user.")
            del user_sid_buffer
        finally:
            if security_descriptor.value:
                kernel32.LocalFree(security_descriptor)
        descriptor = msvcrt.open_osfhandle(
            normalize_windows_handle_for_crt(handle, invalid_handle_value),
            os.O_RDONLY | os.O_BINARY,
        )
        handle = None
        with os.fdopen(descriptor, "rb", closefd=True) as dotenv_file:
            return dotenv_file.read().decode("utf-8")
    finally:
        close_windows_handle_if_owned(kernel32, handle, invalid_handle_value)


def process_environment() -> dict[str, str]:
    if os.name != "nt":
        return dict(os.environ)
    import ctypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.GetEnvironmentStringsW.argtypes = []
    kernel32.GetEnvironmentStringsW.restype = ctypes.c_void_p
    kernel32.FreeEnvironmentStringsW.argtypes = [ctypes.c_void_p]
    kernel32.FreeEnvironmentStringsW.restype = ctypes.c_int
    environment_block = kernel32.GetEnvironmentStringsW()
    if not environment_block:
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        environment: dict[str, str] = {}
        offset = 0
        character_size = ctypes.sizeof(ctypes.c_wchar)
        while True:
            entry = ctypes.wstring_at(environment_block + offset * character_size)
            if not entry:
                return environment
            offset += len(entry) + 1
            if entry.startswith("="):
                name, separator, value = entry[1:].partition("=")
                name = "=" + name
            else:
                name, separator, value = entry.partition("=")
            if not separator:
                raise OSError("The Windows process environment contains an invalid entry.")
            environment[name] = value
    finally:
        if not kernel32.FreeEnvironmentStringsW(environment_block):
            raise ctypes.WinError(ctypes.get_last_error())


def read_verified_primary_dotenv(dotenv_path: Path) -> str:
    try:
        if os.name == "nt":
            return _read_windows_verified_primary_dotenv(dotenv_path)
        return _read_posix_verified_primary_dotenv(dotenv_path)
    except FileNotFoundError:
        raise
    except (OSError, UnicodeError) as error:
        raise CredentialsUnavailable(*REQUIRED_ENV) from error


def load_dotenv_credentials(
    coordination_root: Path, redaction_secrets: set[str] | None = None
) -> dict[str, str]:
    dotenv_path = coordination_root / ".env"
    try:
        content = read_verified_primary_dotenv(dotenv_path)
    except FileNotFoundError:
        return {}
    if redaction_secrets is not None:
        redaction_secrets.update(dotenv_secret_values(content))
    credentials: dict[str, str] = {}
    for line_number, raw_line in enumerate(content.splitlines(), start=1):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        assignment = SIMPLE_DOTENV_ASSIGNMENT_RE.fullmatch(line)
        if assignment is None:
            raise RunnerError(f"Primary .env has an invalid assignment at line {line_number}.")
        name = assignment["name"]
        if name == "SONAR_ADMIN_TOKEN":
            raise RunnerError("SONAR_ADMIN_TOKEN is forbidden; use only project-scoped credentials.")
        if name not in REQUIRED_ENV:
            raise RunnerError(f"Unknown primary .env key: {name}.")
        if name in credentials:
            raise RunnerError(f"Primary .env assigns {name} more than once.")
        credentials[name] = assignment["value"].strip()
    return credentials


def assert_allowed_sonar_inputs(process_env: Mapping[str, str]) -> None:
    if "SONAR_ADMIN_TOKEN" in process_env:
        raise RunnerError("SONAR_ADMIN_TOKEN is forbidden; use only project-scoped credentials.")
    rejected_names = sorted(
        name for name in process_env if is_sonar_environment_name(name) and name not in REQUIRED_ENV
    )
    if rejected_names:
        raise RunnerError(f"Unknown Sonar credential name: {rejected_names[0]}.")


def load_credentials(
    context: GitContext, process_env: Mapping[str, str], redaction_secrets: set[str] | None = None
) -> dict[str, str]:
    """Load only approved Sonar credentials from the primary root and process."""
    assert_no_in_tree_dotenv(context.repository_root)
    assert_allowed_sonar_inputs(process_env)
    dotenv_credentials = load_dotenv_credentials(context.coordination_root, redaction_secrets)
    credentials = {
        name: (process_env[name] if name in process_env else dotenv_credentials.get(name, "")).strip()
        for name in REQUIRED_ENV
    }
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        raise CredentialsUnavailable(*missing)
    credentials["SONAR_HOST_URL"] = credential_free_host(credentials["SONAR_HOST_URL"])
    return credentials


def scrub_sonar_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in source.items() if not is_sonar_environment_name(key)}


def scanner_environment(base_environment: Mapping[str, str], credentials: Mapping[str, str]) -> dict[str, str]:
    environment = scrub_sonar_environment(base_environment)
    environment["SONAR_HOST_URL"] = credentials["SONAR_HOST_URL"]
    environment["SONAR_TOKEN"] = credentials["SONAR_TOKEN"]
    return environment


def redact(text: str, secrets: Collection[str]) -> str:
    for secret in secrets:
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return text


def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    secrets: Collection[str],
    label: str,
    credential_input_names: Sequence[str] = (),
) -> None:
    print("+ " + redact(" ".join(command), secrets), flush=True)
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


def normalized_repository_relative_path(context: GitContext, path: Path) -> str:
    try:
        return str(path.relative_to(context.repository_root)).replace("\\", "/")
    except ValueError as error:
        raise RunnerError("Generated artifact path escapes the scanner worktree.") from error


def clear_generated_artifacts(context: GitContext, environment: Mapping[str, str]) -> list[str]:
    """Delete only known ignored scanner/build output from the disposable worktree."""
    candidates = [context.repository_root / name for name in GENERATED_ROOT_NAMES]
    for directory in iter_scanner_tree(context.repository_root, "*"):
        if directory.name in GENERATED_DIRECTORY_NAMES and directory.is_dir():
            candidates.append(directory)
    candidate_paths = [
        (candidate, normalized_repository_relative_path(context, candidate))
        for candidate in set(candidates)
    ]
    removed: list[str] = []
    for candidate, relative_path in sorted(
        candidate_paths,
        key=lambda item: (-len(item[1].split("/")), item[1].casefold(), item[1]),
    ):
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
        operation = "rmtree" if candidate.is_dir() else "unlink"
        try:
            if operation == "rmtree":
                shutil.rmtree(candidate)
            else:
                candidate.unlink()
        except OSError as error:
            raise GeneratedArtifactCleanupError(
                relative_path, operation, error.__class__.__name__, removed
            ) from None
        removed.append(relative_path)
    return removed


def _remaining_coverage_seconds(deadline: float) -> float:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise CoverageFailure("python_producer", None, "COVERAGE_PROCESS_TIMEOUT")
    return remaining


def _ownership_cleanup_detail() -> dict[str, str]:
    return {
        "path": "coverage-process-tree",
        "operation": "ownership",
        "error_type": "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST",
    }


@dataclass
class _WindowsCoverageLaunch:
    process_handle: Any
    thread_handle: Any
    output_file: Any


class _WindowsCoverageLaunchFailureError(OSError):
    def __init__(self, launch: _WindowsCoverageLaunch, cancelled: bool) -> None:
        self.launch = launch
        self.cancelled = cancelled
        super().__init__("Coverage producer launch failed after CreateProcessW.")


class _WindowsCoverageTreeApi:
    _CREATE_SUSPENDED = 0x00000004
    _CREATE_UNICODE_ENVIRONMENT = 0x00000400
    _EXTENDED_STARTUPINFO_PRESENT = 0x00080000
    _HANDLE_FLAG_INHERIT = 0x00000001
    _JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION = 1
    _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION = 9
    _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000
    _PROC_THREAD_ATTRIBUTE_HANDLE_LIST = 0x00020002
    _STARTF_USESTDHANDLES = 0x00000100
    _WAIT_FAILED = 0xFFFFFFFF
    _WAIT_OBJECT_0 = 0
    _WAIT_TIMEOUT = 258

    def __init__(self) -> None:
        from ctypes import wintypes

        self._wintypes = wintypes
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class JobObjectBasicLimitInformation(ctypes.Structure):
            _fields_ = [
                ("per_process_user_time_limit", ctypes.c_longlong),
                ("per_job_user_time_limit", ctypes.c_longlong),
                ("limit_flags", wintypes.DWORD),
                ("minimum_working_set_size", ctypes.c_size_t),
                ("maximum_working_set_size", ctypes.c_size_t),
                ("active_process_limit", wintypes.DWORD),
                ("affinity", ctypes.c_size_t),
                ("priority_class", wintypes.DWORD),
                ("scheduling_class", wintypes.DWORD),
            ]

        class IoCounters(ctypes.Structure):
            _fields_ = [
                ("read_operation_count", ctypes.c_ulonglong),
                ("write_operation_count", ctypes.c_ulonglong),
                ("other_operation_count", ctypes.c_ulonglong),
                ("read_transfer_count", ctypes.c_ulonglong),
                ("write_transfer_count", ctypes.c_ulonglong),
                ("other_transfer_count", ctypes.c_ulonglong),
            ]

        class JobObjectExtendedLimitInformation(ctypes.Structure):
            _fields_ = [
                ("basic_limit_information", JobObjectBasicLimitInformation),
                ("io_info", IoCounters),
                ("process_memory_limit", ctypes.c_size_t),
                ("job_memory_limit", ctypes.c_size_t),
                ("peak_process_memory_used", ctypes.c_size_t),
                ("peak_job_memory_used", ctypes.c_size_t),
            ]

        class JobObjectBasicAccountingInformation(ctypes.Structure):
            _fields_ = [
                ("total_user_time", ctypes.c_longlong),
                ("total_kernel_time", ctypes.c_longlong),
                ("this_period_total_user_time", ctypes.c_longlong),
                ("this_period_total_kernel_time", ctypes.c_longlong),
                ("total_page_fault_count", wintypes.DWORD),
                ("total_processes", wintypes.DWORD),
                ("active_processes", wintypes.DWORD),
                ("total_terminated_processes", wintypes.DWORD),
            ]

        class StartupInfo(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("reserved", wintypes.LPWSTR),
                ("desktop", wintypes.LPWSTR),
                ("title", wintypes.LPWSTR),
                ("x", wintypes.DWORD),
                ("y", wintypes.DWORD),
                ("x_size", wintypes.DWORD),
                ("y_size", wintypes.DWORD),
                ("x_count_chars", wintypes.DWORD),
                ("y_count_chars", wintypes.DWORD),
                ("fill_attribute", wintypes.DWORD),
                ("flags", wintypes.DWORD),
                ("show_window", wintypes.WORD),
                ("reserved2_count", wintypes.WORD),
                ("reserved2", ctypes.POINTER(ctypes.c_byte)),
                ("standard_input", wintypes.HANDLE),
                ("standard_output", wintypes.HANDLE),
                ("standard_error", wintypes.HANDLE),
            ]

        class StartupInfoEx(ctypes.Structure):
            _fields_ = [("startup_info", StartupInfo), ("attribute_list", ctypes.c_void_p)]

        class ProcessInformation(ctypes.Structure):
            _fields_ = [
                ("process", wintypes.HANDLE),
                ("thread", wintypes.HANDLE),
                ("process_id", wintypes.DWORD),
                ("thread_id", wintypes.DWORD),
            ]

        self._JobObjectBasicAccountingInformation = JobObjectBasicAccountingInformation
        self._JobObjectExtendedLimitInformation = JobObjectExtendedLimitInformation
        self._ProcessInformation = ProcessInformation
        self._StartupInfoEx = StartupInfoEx
        self._configure_functions()

    def _configure_functions(self) -> None:
        kernel32 = self._kernel32
        wintypes = self._wintypes
        kernel32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.IsProcessInJob.argtypes = [
            wintypes.HANDLE,
            wintypes.HANDLE,
            ctypes.POINTER(wintypes.BOOL),
        ]
        kernel32.IsProcessInJob.restype = wintypes.BOOL
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        kernel32.ResumeThread.argtypes = [wintypes.HANDLE]
        kernel32.ResumeThread.restype = wintypes.DWORD
        kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel32.WaitForSingleObject.restype = wintypes.DWORD
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateJobObject.restype = wintypes.BOOL
        kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
        kernel32.TerminateProcess.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.SetHandleInformation.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.DWORD,
        ]
        kernel32.SetHandleInformation.restype = wintypes.BOOL
        kernel32.InitializeProcThreadAttributeList.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.POINTER(ctypes.c_size_t),
        ]
        kernel32.InitializeProcThreadAttributeList.restype = wintypes.BOOL
        kernel32.UpdateProcThreadAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_size_t,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        kernel32.UpdateProcThreadAttribute.restype = wintypes.BOOL
        kernel32.DeleteProcThreadAttributeList.argtypes = [ctypes.c_void_p]
        kernel32.DeleteProcThreadAttributeList.restype = None
        kernel32.CreateProcessW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.LPWSTR,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.BOOL,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.LPCWSTR,
            ctypes.POINTER(self._StartupInfoEx),
            ctypes.POINTER(self._ProcessInformation),
        ]
        kernel32.CreateProcessW.restype = wintypes.BOOL

    def _error(self, operation: str) -> OSError:
        return OSError(ctypes.get_last_error(), f"{operation} failed")

    def _set_inheritable(self, handle: Any, inheritable: bool) -> None:
        flags = self._HANDLE_FLAG_INHERIT if inheritable else 0
        if not self._kernel32.SetHandleInformation(handle, self._HANDLE_FLAG_INHERIT, flags):
            raise self._error("SetHandleInformation")

    def create_job(self) -> Any:
        job = self._kernel32.CreateJobObjectW(None, None)
        if not job:
            raise self._error("CreateJobObjectW")
        return job

    def configure_kill_on_close(self, job: Any) -> None:
        limits = self._JobObjectExtendedLimitInformation()
        limits.basic_limit_information.limit_flags = self._JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not self._kernel32.SetInformationJobObject(
            job,
            self._JOB_OBJECT_EXTENDED_LIMIT_INFORMATION,
            ctypes.byref(limits),
            ctypes.sizeof(limits),
        ):
            raise self._error("SetInformationJobObject")

    def launch_suspended(
        self,
        command: Sequence[str],
        cwd: Path,
        environment: Mapping[str, str],
        launch_slot: list[_WindowsCoverageLaunch] | None = None,
    ) -> _WindowsCoverageLaunch:
        if not command:
            raise OSError("Coverage producer command is empty")
        executable = shutil.which(command[0], path=environment.get("PATH"))
        if executable is None:
            raise OSError("Coverage producer executable is unavailable")

        import msvcrt

        output_file = tempfile.TemporaryFile(mode="w+b")
        stdin_descriptor: int | None = None
        attribute_buffer: Any | None = None
        attribute_list: Any | None = None
        attributes_initialized = False
        process_information: Any | None = None
        input_handle: Any | None = None
        output_handle: Any | None = None
        try:
            stdin_descriptor = os.open(os.devnull, os.O_RDONLY)
            input_handle = msvcrt.get_osfhandle(stdin_descriptor)
            output_handle = msvcrt.get_osfhandle(output_file.fileno())
            self._set_inheritable(input_handle, True)
            self._set_inheritable(output_handle, True)

            attribute_size = ctypes.c_size_t()
            self._kernel32.InitializeProcThreadAttributeList(
                None, 1, 0, ctypes.byref(attribute_size)
            )
            if not attribute_size.value:
                raise self._error("InitializeProcThreadAttributeList")
            attribute_buffer = ctypes.create_string_buffer(attribute_size.value)
            attribute_list = ctypes.cast(attribute_buffer, ctypes.c_void_p)
            if not self._kernel32.InitializeProcThreadAttributeList(
                attribute_list, 1, 0, ctypes.byref(attribute_size)
            ):
                raise self._error("InitializeProcThreadAttributeList")
            attributes_initialized = True

            inheritable_handles = (self._wintypes.HANDLE * 2)(input_handle, output_handle)
            if not self._kernel32.UpdateProcThreadAttribute(
                attribute_list,
                0,
                self._PROC_THREAD_ATTRIBUTE_HANDLE_LIST,
                ctypes.cast(inheritable_handles, ctypes.c_void_p),
                ctypes.sizeof(inheritable_handles),
                None,
                None,
            ):
                raise self._error("UpdateProcThreadAttribute")

            command_line = ctypes.create_unicode_buffer(subprocess.list2cmdline(list(command)))
            environment_block = ctypes.create_unicode_buffer(
                "\0".join(
                    f"{name}={value}"
                    for name, value in sorted(environment.items(), key=lambda item: item[0].upper())
                )
                + "\0\0"
            )
            startup = self._StartupInfoEx()
            startup.startup_info.cb = ctypes.sizeof(self._StartupInfoEx)
            startup.startup_info.flags = self._STARTF_USESTDHANDLES
            startup.startup_info.standard_input = input_handle
            startup.startup_info.standard_output = output_handle
            startup.startup_info.standard_error = output_handle
            startup.attribute_list = attribute_list
            process_information = self._ProcessInformation()
            creation_flags = (
                self._CREATE_SUSPENDED
                | self._CREATE_UNICODE_ENVIRONMENT
                | self._EXTENDED_STARTUPINFO_PRESENT
            )
            if not self._kernel32.CreateProcessW(
                executable,
                command_line,
                None,
                None,
                True,
                creation_flags,
                environment_block,
                str(cwd),
                ctypes.byref(startup),
                ctypes.byref(process_information),
            ):
                raise self._error("CreateProcessW")
            launch = _WindowsCoverageLaunch(
                process_information.process, process_information.thread, output_file
            )
            if launch_slot is not None:
                launch_slot.append(launch)
            self._set_inheritable(input_handle, False)
            self._set_inheritable(output_handle, False)
            return launch
        except BaseException as error:
            if process_information is not None and process_information.process:
                launch = _WindowsCoverageLaunch(
                    process_information.process, process_information.thread, output_file
                )
                raise _WindowsCoverageLaunchFailureError(
                    launch, isinstance(error, KeyboardInterrupt)
                ) from error
            output_file.close()
            raise
        finally:
            if attributes_initialized and attribute_list is not None:
                self._kernel32.DeleteProcThreadAttributeList(attribute_list)
            if stdin_descriptor is not None:
                try:
                    os.close(stdin_descriptor)
                except OSError:
                    pass

    def assign_process(self, job: Any, process: Any) -> None:
        if not self._kernel32.AssignProcessToJobObject(job, process):
            raise self._error("AssignProcessToJobObject")

    def is_process_in_job(self, job: Any, process: Any) -> bool:
        in_job = self._wintypes.BOOL()
        if not self._kernel32.IsProcessInJob(process, job, ctypes.byref(in_job)):
            raise self._error("IsProcessInJob")
        return bool(in_job.value)

    def active_processes(self, job: Any) -> int:
        accounting = self._JobObjectBasicAccountingInformation()
        if not self._kernel32.QueryInformationJobObject(
            job,
            self._JOB_OBJECT_BASIC_ACCOUNTING_INFORMATION,
            ctypes.byref(accounting),
            ctypes.sizeof(accounting),
            None,
        ):
            raise self._error("QueryInformationJobObject")
        return int(accounting.active_processes)

    def resume_thread(self, thread: Any) -> int:
        result = self._kernel32.ResumeThread(thread)
        if result == 0xFFFFFFFF:
            raise self._error("ResumeThread")
        return int(result)

    def wait_process(self, process: Any, timeout_seconds: float) -> bool:
        milliseconds = max(0, min(int(timeout_seconds * 1000), self._WAIT_FAILED - 1))
        result = self._kernel32.WaitForSingleObject(process, milliseconds)
        if result == self._WAIT_OBJECT_0:
            return True
        if result == self._WAIT_TIMEOUT:
            return False
        raise self._error("WaitForSingleObject")

    def exit_code(self, process: Any) -> int:
        exit_code = self._wintypes.DWORD()
        if not self._kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            raise self._error("GetExitCodeProcess")
        return int(exit_code.value)

    def terminate_process(self, process: Any) -> None:
        if not self._kernel32.TerminateProcess(process, 1):
            raise self._error("TerminateProcess")

    def terminate_job(self, job: Any) -> None:
        if not self._kernel32.TerminateJobObject(job, 1):
            raise self._error("TerminateJobObject")

    def close_handle(self, handle: Any) -> None:
        if not self._kernel32.CloseHandle(handle):
            raise self._error("CloseHandle")


def _windows_coverage_tree_api() -> _WindowsCoverageTreeApi:
    if os.name != "nt":
        raise OSError("Windows Job Object ownership is unavailable")
    return _WindowsCoverageTreeApi()


class CoverageTreeOwner:
    def __init__(
        self,
        api: Any,
        job_handle: Any,
        launch: _WindowsCoverageLaunch,
        state: str = "OWNER_ACKED",
    ) -> None:
        self._api = api
        self._job_handle = job_handle
        self._process_handle = launch.process_handle
        self._thread_handle = launch.thread_handle
        self._output_file = launch.output_file
        self._direct_exit_code: int | None = None
        self._state = state

    @staticmethod
    def _cleanup_unpublished_start(
        api: Any | None,
        job_handle: Any | None,
        launch: _WindowsCoverageLaunch | None,
        assigned: bool,
    ) -> tuple[CoverageTreeObservation | None, bool]:
        if api is None:
            return None, launch is None
        if launch is None:
            if job_handle is not None:
                try:
                    api.close_handle(job_handle)
                except (OSError, KeyboardInterrupt):
                    pass
            return None, True

        tree_empty = False
        direct_closed = False
        try:
            if assigned and job_handle is not None:
                try:
                    api.terminate_job(job_handle)
                except (OSError, KeyboardInterrupt):
                    api.terminate_process(launch.process_handle)
            else:
                api.terminate_process(launch.process_handle)
            if not api.wait_process(launch.process_handle, COVERAGE_PROCESS_CLEANUP_SECONDS):
                raise OSError("Suspended coverage producer did not terminate")
            api.exit_code(launch.process_handle)
            api.close_handle(launch.process_handle)
            api.close_handle(launch.thread_handle)
            direct_closed = True
            if job_handle is not None:
                deadline = time.monotonic() + COVERAGE_PROCESS_CLEANUP_SECONDS
                while api.active_processes(job_handle) != 0:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(["coverage-process-tree"], 0)
                    time.sleep(min(0.05, remaining))
            tree_empty = True
        except (OSError, KeyboardInterrupt, subprocess.TimeoutExpired):
            tree_empty = False
        finally:
            if not direct_closed:
                for handle in (launch.process_handle, launch.thread_handle):
                    try:
                        api.close_handle(handle)
                    except (OSError, KeyboardInterrupt):
                        pass
            try:
                launch.output_file.close()
            except (OSError, KeyboardInterrupt):
                pass
            if job_handle is not None:
                try:
                    api.close_handle(job_handle)
                except (OSError, KeyboardInterrupt):
                    pass
        observation = CoverageTreeObservation(
            "windows", "job_object", "TREE_EMPTY" if tree_empty else None
        )
        return observation, tree_empty

    @classmethod
    def start_and_ack(
        cls,
        command: Sequence[str],
        *,
        cwd: Path,
        environment: Mapping[str, str],
        owner_slot: list[CoverageTreeOwner] | None = None,
    ) -> CoverageTreeOwner:
        if os.name != "nt":
            raise CoverageTreeOwnerUnavailable()
        api: Any | None = None
        job_handle: Any | None = None
        launch: _WindowsCoverageLaunch | None = None
        launch_slot: list[_WindowsCoverageLaunch] = []
        assigned = False
        try:
            api = _windows_coverage_tree_api()
            job_handle = api.create_job()
            api.configure_kill_on_close(job_handle)
            launch = api.launch_suspended(command, cwd, environment, launch_slot=launch_slot)
            launch_slot.clear()
            assert launch is not None
            api.assign_process(job_handle, launch.process_handle)
            assigned = True
            if not api.is_process_in_job(job_handle, launch.process_handle):
                raise OSError("Coverage producer membership was not acknowledged")
            if api.active_processes(job_handle) != 1:
                raise OSError("Coverage producer accounting was not acknowledged")
            owner = cls(api, job_handle, launch, "ADMISSION_PENDING")
            if owner_slot is not None:
                owner_slot.append(owner)
            if api.resume_thread(launch.thread_handle) != 1:
                raise OSError("Coverage producer suspend count was not acknowledged")
            owner._state = "OWNER_ACKED"
            return owner
        except _WindowsCoverageLaunchFailureError as error:
            launch = error.launch
            launch_slot.clear()
            observation, artifact_cleanup_permitted = cls._cleanup_unpublished_start(
                api, job_handle, launch, assigned
            )
            if owner_slot is not None:
                owner_slot.clear()
            if error.cancelled:
                raise CoverageTreeStartCancelledError(
                    observation,
                    artifact_cleanup_permitted,
                    _ownership_cleanup_detail() if not artifact_cleanup_permitted else None,
                ) from error
            raise CoverageTreeOwnerUnavailable(observation, artifact_cleanup_permitted) from error
        except KeyboardInterrupt:
            if launch is None and launch_slot:
                launch = launch_slot[0]
            launch_slot.clear()
            observation, artifact_cleanup_permitted = cls._cleanup_unpublished_start(
                api, job_handle, launch, assigned
            )
            if owner_slot is not None:
                owner_slot.clear()
            raise CoverageTreeStartCancelledError(
                observation,
                artifact_cleanup_permitted,
                _ownership_cleanup_detail() if not artifact_cleanup_permitted else None,
            ) from None
        except (OSError, RuntimeError, ValueError):
            if launch is None and launch_slot:
                launch = launch_slot[0]
            launch_slot.clear()
            observation, artifact_cleanup_permitted = cls._cleanup_unpublished_start(
                api, job_handle, launch, assigned
            )
            if owner_slot is not None:
                owner_slot.clear()
            raise CoverageTreeOwnerUnavailable(observation, artifact_cleanup_permitted) from None

    @property
    def observation(self) -> CoverageTreeObservation:
        return CoverageTreeObservation(
            "windows", "job_object", "TREE_EMPTY" if self._state == "TREE_EMPTY" else None
        )

    def _remaining(self, deadline: float) -> float:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise subprocess.TimeoutExpired(["coverage-process-tree"], 0)
        return remaining

    def _close_direct_handles(self) -> None:
        handles = (self._process_handle, self._thread_handle)
        self._process_handle = None
        self._thread_handle = None
        close_error: OSError | None = None
        interrupted = False
        for handle in handles:
            if handle is None:
                continue
            try:
                self._api.close_handle(handle)
            except KeyboardInterrupt:
                interrupted = True
            except OSError as error:
                close_error = error
        if close_error is not None:
            raise close_error
        if interrupted:
            raise KeyboardInterrupt

    def _capture_direct_exit(self, deadline: float) -> int:
        if self._process_handle is None:
            if self._direct_exit_code is None:
                raise RuntimeError("Coverage producer has no direct process handle")
            return self._direct_exit_code
        if not self._api.wait_process(self._process_handle, self._remaining(deadline)):
            raise subprocess.TimeoutExpired(["coverage-process-tree"], 0)
        exit_code = self._api.exit_code(self._process_handle)
        self._direct_exit_code = exit_code
        self._close_direct_handles()
        return exit_code

    def _ownership_lost(self, _error: OSError) -> CoverageTreeOwnershipLost:
        if self._state != "TREE_EMPTY":
            self._state = "OWNER_LOST"
        return CoverageTreeOwnershipLost(self.observation)

    def wait_direct_exit(self, deadline: float) -> int:
        if self._state != "OWNER_ACKED":
            raise RuntimeError("Coverage producer is not awaiting a direct exit")
        try:
            exit_code = self._capture_direct_exit(deadline)
        except OSError as error:
            raise self._ownership_lost(error) from error
        self._state = "PRODUCER_EXITED"
        return exit_code

    def _read_output(self) -> str:
        if self._output_file is None:
            raise RuntimeError("Coverage producer output handle is unavailable")
        self._output_file.seek(0)
        output = self._output_file.read()
        if isinstance(output, bytes):
            return output.decode(locale.getpreferredencoding(False), errors="replace")
        return str(output)

    def _finalize_tree_empty(self) -> CoverageProcessOutcome:
        if self._output_file is None or self._job_handle is None:
            raise RuntimeError("Coverage producer ownership is unavailable")
        output = self._read_output()
        output_file = self._output_file
        job_handle = self._job_handle
        self._output_file = None
        self._job_handle = None
        self._state = "TREE_EMPTY"
        close_error: OSError | None = None
        interrupted = False
        try:
            output_file.close()
        except KeyboardInterrupt:
            interrupted = True
        except OSError as error:
            close_error = error
        try:
            self._api.close_handle(job_handle)
        except KeyboardInterrupt:
            interrupted = True
        except OSError as error:
            close_error = error
        if close_error is not None:
            raise close_error
        if interrupted:
            raise KeyboardInterrupt
        if self._direct_exit_code is None:
            raise RuntimeError("Coverage producer exit was not captured")
        return CoverageProcessOutcome(self._direct_exit_code, output, self.observation)

    def wait_empty(self, deadline: float) -> CoverageProcessOutcome:
        if self._state != "PRODUCER_EXITED":
            raise RuntimeError("Coverage producer is not awaiting tree drain")
        try:
            while True:
                if self._api.active_processes(self._job_handle) == 0:
                    return self._finalize_tree_empty()
                time.sleep(min(0.05, self._remaining(deadline)))
        except OSError as error:
            raise self._ownership_lost(error) from error

    def abort_and_wait_empty(self, deadline: float) -> CoverageProcessOutcome:
        if self._state not in {"OWNER_ACKED", "PRODUCER_EXITED"}:
            raise RuntimeError("Coverage producer cannot be aborted from its current state")
        try:
            self._api.terminate_job(self._job_handle)
            self._capture_direct_exit(deadline)
            self._state = "PRODUCER_EXITED"
            return self.wait_empty(deadline)
        except OSError as error:
            raise self._ownership_lost(error) from error

    def discard_unproven(self) -> None:
        self._state = "OWNER_LOST"
        output_file = self._output_file
        handles = (self._process_handle, self._thread_handle, self._job_handle)
        self._output_file = None
        self._process_handle = None
        self._thread_handle = None
        self._job_handle = None
        if output_file is not None:
            try:
                output_file.close()
            except (OSError, KeyboardInterrupt):
                pass
        for handle in handles:
            if handle is None:
                continue
            try:
                self._api.close_handle(handle)
            except (OSError, KeyboardInterrupt):
                pass


def _abort_coverage_owner(owner: CoverageTreeOwner) -> CoverageProcessOutcome:
    return owner.abort_and_wait_empty(time.monotonic() + COVERAGE_PROCESS_CLEANUP_SECONDS)


def _unproven_owner_failure(
    stage: str,
    language: str | None,
    failure_code: str,
    owner: CoverageTreeOwner,
    cleanup_error: BaseException | None = None,
) -> CoverageFailure:
    return CoverageFailure(
        stage,
        language,
        failure_code,
        cleanup_failure=(_ownership_cleanup_detail() if cleanup_error is not None else None),
        owner=owner.observation,
        artifact_cleanup_permitted=False,
    )


def _cancellation_failure(
    owner: CoverageTreeOwner, stage: str, language: str | None
) -> CoverageFailure:
    if owner.observation.terminal_state == "TREE_EMPTY":
        return CoverageFailure(
            stage,
            language,
            "COVERAGE_PROCESS_CANCELLED",
            owner=owner.observation,
        )
    if owner._state == "OWNER_LOST":
        owner.discard_unproven()
        return CoverageFailure(
            "process_owner",
            language,
            "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST",
            owner=owner.observation,
            artifact_cleanup_permitted=False,
        )
    try:
        outcome = _abort_coverage_owner(owner)
    except (
        CoverageTreeOwnershipLost,
        subprocess.TimeoutExpired,
        KeyboardInterrupt,
        RuntimeError,
    ) as cleanup_error:
        owner.discard_unproven()
        return _unproven_owner_failure(
            stage, language, "COVERAGE_PROCESS_CANCELLED", owner, cleanup_error
        )
    return CoverageFailure(
        stage,
        language,
        "COVERAGE_PROCESS_CANCELLED",
        owner=outcome.owner,
    )


def run_coverage_process(
    command: Sequence[str],
    *,
    cwd: Path,
    environment: Mapping[str, str],
    secrets: Collection[str],
    label: str,
    deadline: float,
    stage: str,
    language: str | None,
) -> None:
    """Run one coverage child under the Windows Job Object ownership contract."""
    try:
        _remaining_coverage_seconds(deadline)
    except CoverageFailure as error:
        raise CoverageFailure(stage, language, error.failure_code) from error

    owner: CoverageTreeOwner | None = None
    owner_slot: list[CoverageTreeOwner] = []
    try:
        print("+ " + redact(" ".join(command), secrets), flush=True)
        try:
            owner = CoverageTreeOwner.start_and_ack(
                command,
                cwd=cwd,
                environment=environment,
                owner_slot=owner_slot,
            )
            owner_slot.clear()
        except CoverageTreeStartCancelledError as error:
            raise CoverageFailure(
                stage,
                language,
                "COVERAGE_PROCESS_CANCELLED",
                cleanup_failure=error.cleanup_failure,
                owner=error.observation,
                artifact_cleanup_permitted=error.artifact_cleanup_permitted,
            ) from error
        except CoverageTreeOwnerUnavailable as error:
            failure_code = (
                "COVERAGE_PROCESS_TREE_OWNER_UNAVAILABLE"
                if error.artifact_cleanup_permitted
                else "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST"
            )
            raise CoverageFailure(
                "process_owner",
                language,
                failure_code,
                owner=error.observation,
                artifact_cleanup_permitted=error.artifact_cleanup_permitted,
            ) from error

        if owner is None:
            raise CoverageFailure(
                "process_owner",
                language,
                "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST",
                artifact_cleanup_permitted=False,
            )

        try:
            exit_code = owner.wait_direct_exit(deadline)
        except subprocess.TimeoutExpired as error:
            try:
                outcome = _abort_coverage_owner(owner)
            except (
                CoverageTreeOwnershipLost,
                subprocess.TimeoutExpired,
                KeyboardInterrupt,
            ) as cleanup_error:
                owner.discard_unproven()
                raise _unproven_owner_failure(
                    stage, language, "COVERAGE_PROCESS_TIMEOUT", owner, cleanup_error
                ) from error
            raise CoverageFailure(
                stage, language, "COVERAGE_PROCESS_TIMEOUT", owner=outcome.owner
            ) from error
        except KeyboardInterrupt as error:
            raise _cancellation_failure(owner, stage, language) from error
        except CoverageTreeOwnershipLost as error:
            owner.discard_unproven()
            raise _unproven_owner_failure(
                "process_owner", language, "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST", owner
            ) from error

        if exit_code:
            try:
                outcome = _abort_coverage_owner(owner)
            except (
                CoverageTreeOwnershipLost,
                subprocess.TimeoutExpired,
                KeyboardInterrupt,
            ) as cleanup_error:
                owner.discard_unproven()
                raise _unproven_owner_failure(
                    stage, language, "COVERAGE_PROCESS_FAILED", owner, cleanup_error
                ) from cleanup_error
            raise CoverageFailure(stage, language, "COVERAGE_PROCESS_FAILED", owner=outcome.owner)

        try:
            outcome = owner.wait_empty(deadline)
        except subprocess.TimeoutExpired as error:
            try:
                outcome = _abort_coverage_owner(owner)
            except (
                CoverageTreeOwnershipLost,
                subprocess.TimeoutExpired,
                KeyboardInterrupt,
            ) as cleanup_error:
                owner.discard_unproven()
                raise _unproven_owner_failure(
                    stage, language, "COVERAGE_PROCESS_TIMEOUT", owner, cleanup_error
                ) from error
            raise CoverageFailure(
                stage, language, "COVERAGE_PROCESS_TIMEOUT", owner=outcome.owner
            ) from error
        except KeyboardInterrupt as error:
            raise _cancellation_failure(owner, stage, language) from error
        except CoverageTreeOwnershipLost as error:
            owner.discard_unproven()
            raise _unproven_owner_failure(
                "process_owner", language, "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST", owner
            ) from error
        if outcome.output:
            print(
                redact(outcome.output, secrets),
                end="" if outcome.output.endswith("\n") else "\n",
                flush=True,
            )
    except KeyboardInterrupt as error:
        bound_owner = owner if owner is not None else (owner_slot[0] if owner_slot else None)
        if bound_owner is None:
            raise CoverageFailure(
                stage,
                language,
                "COVERAGE_PROCESS_CANCELLED",
                artifact_cleanup_permitted=True,
            ) from error
        raise _cancellation_failure(bound_owner, stage, language) from error


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


def project_version(repository_root: Path) -> str:
    pyproject = repository_root / "pyproject.toml"
    try:
        lines = pyproject.read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise RunnerError("Authoritative release version is unavailable.") from error
    in_project_section = False
    versions: list[str] = []
    for raw_line in lines:
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            in_project_section = line == "[project]"
            continue
        if in_project_section:
            match = PROJECT_VERSION_ASSIGNMENT_RE.fullmatch(line)
            if match:
                versions.append(match["version"])
    if len(versions) != 1 or not SEMVER_RE.fullmatch(versions[0]):
        raise RunnerError("Authoritative release version is unavailable.")
    return versions[0]


def _validate_coverage_run_id(run_id: str) -> None:
    try:
        parsed = uuid.UUID(run_id)
    except (AttributeError, TypeError, ValueError) as error:
        raise RunnerError("Coverage run id is invalid.") from error
    if str(parsed) != run_id:
        raise RunnerError("Coverage run id is invalid.")


def _coverage_slug(project: str) -> str:
    return hashlib.sha256(project.encode("utf-8")).hexdigest()[:16]


def _normalized_coverage_path(path: Path, context: GitContext) -> str:
    try:
        relative = path.relative_to(context.repository_root)
    except ValueError as error:
        raise RunnerError("Coverage report path escapes the scanner worktree.") from error
    normalized = str(relative).replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise RunnerError("Coverage report path is invalid.")
    return normalized


def derive_coverage_plan(context: GitContext, run_id: str) -> CoveragePlan:
    _validate_coverage_run_id(run_id)
    if not SHA_RE.fullmatch(context.head):
        raise RunnerError("Coverage plan requires a complete captured HEAD.")
    root = context.repository_root.joinpath(*COVERAGE_ROOT_PARTS, run_id)
    reports: list[CoverageReportPlan] = []
    for project in CLOSED_DOTNET_COVERAGE_PROJECTS:
        slug = _coverage_slug(project)
        report_path = root / "dotnet" / slug / "coverage.opencover.xml"
        reports.append(
            CoverageReportPlan(
                "dotnet",
                "opencover",
                report_path,
                _normalized_coverage_path(report_path, context),
                project,
                slug,
            )
        )
    reports.sort(key=lambda report: report.normalized_path)
    python_path = root / "python" / "coverage.xml"
    reports.append(
        CoverageReportPlan(
            "python",
            "cobertura",
            python_path,
            _normalized_coverage_path(python_path, context),
            None,
            None,
        )
    )
    return CoveragePlan(run_id, root, root / "coverage-run.json", tuple(reports))


def coverage_scanner_properties(plan: CoveragePlan) -> tuple[str, str]:
    dotnet_paths = ",".join(report.normalized_path for report in plan.dotnet_reports)
    if not dotnet_paths or not plan.python_report.normalized_path:
        raise RunnerError("Coverage report import paths are invalid.")
    return (
        f"/d:sonar.python.coverage.reportPaths={plan.python_report.normalized_path}",
        f"/d:sonar.cs.opencover.reportsPaths={dotnet_paths}",
    )


def canonical_coverage_marker_payload(plan: CoveragePlan, context: GitContext) -> dict[str, Any]:
    if plan.run_id != plan.root.name:
        raise RunnerError("Coverage plan marker identity is invalid.")
    return {
        "schema": "netcoredbg-mcp.coverage-run/1",
        "run_id": plan.run_id,
        "captured_head": context.head,
        "reports": [
            {
                "language": report.language,
                "format": report.format,
                "path": report.normalized_path,
                "project": report.project,
                "slug": report.slug,
            }
            for report in plan.reports
        ],
    }


def canonical_coverage_marker_bytes(plan: CoveragePlan, context: GitContext) -> bytes:
    return (
        json.dumps(
            canonical_coverage_marker_payload(plan, context),
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        + b"\n"
    )


def _assert_coverage_path(path: Path, plan: CoveragePlan, context: GitContext) -> None:
    try:
        path.relative_to(plan.root)
        plan.root.relative_to(context.repository_root)
    except ValueError as error:
        raise RunnerError("Coverage path escapes the scanner worktree.") from error
    current = context.repository_root
    for part in path.relative_to(context.repository_root).parts:
        current /= part
        if not current.exists():
            break
        try:
            attributes = current.lstat()
        except OSError as error:
            raise RunnerError("Coverage path metadata is unavailable.") from error
        if current.is_symlink() or getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
            raise RunnerError("Coverage path must not traverse a symbolic link or reparse point.")


def _assert_claimed_coverage_path(path: Path, plan: CoveragePlan, context: GitContext) -> None:
    if path.is_symlink() or plan.root.is_symlink():
        raise CoverageFailure("report_validation", None, "COVERAGE_SYMLINK_REJECTED")
    try:
        path.relative_to(plan.root)
        resolved_repository_root = context.repository_root.resolve()
        resolved_root = plan.root.resolve()
        resolved_path = path.resolve()
        resolved_root.relative_to(resolved_repository_root)
        resolved_path.relative_to(resolved_root)
    except (OSError, ValueError) as error:
        raise CoverageFailure("report_validation", None, "COVERAGE_PATH_ESCAPE") from error


def claim_coverage_run(plan: CoveragePlan, context: GitContext) -> str:
    if plan.root.exists() or plan.root.is_symlink():
        raise RunnerError("coverage run directory is unavailable.")
    try:
        plan.root.parent.mkdir(parents=True, exist_ok=True)
        _assert_coverage_path(plan.root, plan, context)
        plan.root.mkdir(exist_ok=False)
        _assert_coverage_path(plan.marker_path, plan, context)
        plan.marker_path.write_bytes(canonical_coverage_marker_bytes(plan, context))
    except RunnerError:
        raise
    except (FileExistsError, OSError) as error:
        raise RunnerError("coverage run directory is unavailable.") from error
    return validate_coverage_marker(plan, context)


def validate_coverage_marker(plan: CoveragePlan, context: GitContext) -> str:
    _assert_coverage_path(plan.marker_path, plan, context)
    if not plan.marker_path.is_file() or plan.marker_path.is_symlink():
        raise RunnerError("Coverage marker is unavailable.")
    try:
        marker_bytes = plan.marker_path.read_bytes()
    except OSError as error:
        raise RunnerError("Coverage marker is unreadable.") from error
    if marker_bytes != canonical_coverage_marker_bytes(plan, context):
        raise RunnerError("Coverage marker does not match the current plan.")
    return hashlib.sha256(marker_bytes).hexdigest()


def _xml_local_name(value: str) -> str:
    return value.rsplit("}", 1)[-1]


def _coverage_file_bytes(
    plan: CoveragePlan, context: GitContext, report: CoverageReportPlan
) -> bytes:
    _assert_claimed_coverage_path(report.absolute_path, plan, context)
    if not report.absolute_path.exists():
        raise CoverageFailure("report_validation", report.language, "COVERAGE_REPORT_MISSING")
    try:
        file_status = report.absolute_path.lstat()
    except OSError as error:
        raise CoverageFailure(
            "report_validation", report.language, "COVERAGE_REPORT_UNREADABLE"
        ) from error
    if report.absolute_path.is_symlink():
        raise CoverageFailure("report_validation", report.language, "COVERAGE_SYMLINK_REJECTED")
    if not stat.S_ISREG(file_status.st_mode):
        raise CoverageFailure("report_validation", report.language, "COVERAGE_REPORT_NOT_REGULAR")
    try:
        contents = report.absolute_path.read_bytes()
    except OSError as error:
        raise CoverageFailure(
            "report_validation", report.language, "COVERAGE_REPORT_UNREADABLE"
        ) from error
    if not contents:
        raise CoverageFailure("report_validation", report.language, "COVERAGE_REPORT_EMPTY")
    return contents


def _normalized_relative_source_path(value: str) -> str:
    normalized = value.replace("\\", "/")
    if (
        not normalized
        or normalized.startswith("/")
        or re.match(r"^[A-Za-z]:", normalized)
        or any(part in {"", ".", ".."} for part in normalized.split("/"))
    ):
        raise CoverageFailure("report_validation", None, "COVERAGE_SOURCE_PATH_INVALID")
    return normalized


def _source_path_set_sha256(paths: Collection[str]) -> str:
    if not paths:
        raise CoverageFailure("report_validation", None, "COVERAGE_SOURCE_MAPPING_EMPTY")
    return hashlib.sha256(("\n".join(sorted(paths)) + "\n").encode("utf-8")).hexdigest()


def _validate_python_sources(root: ElementTree.Element, context: GitContext) -> list[str]:
    paths: list[str] = []
    for element in root.iter():
        if _xml_local_name(element.tag) != "class":
            continue
        filename = element.attrib.get("filename")
        if not isinstance(filename, str):
            raise CoverageFailure("report_validation", "python", "COVERAGE_SOURCE_MAPPING_INVALID")
        normalized = _normalized_relative_source_path(filename)
        if not normalized.startswith("src/netcoredbg_mcp/") or not normalized.endswith(".py"):
            raise CoverageFailure("report_validation", "python", "COVERAGE_SOURCE_MAPPING_INVALID")
        source = context.repository_root / normalized
        try:
            source.resolve().relative_to(context.repository_root.resolve())
        except (OSError, ValueError) as error:
            raise CoverageFailure(
                "report_validation", "python", "COVERAGE_SOURCE_PATH_ESCAPE"
            ) from error
        if source.is_symlink() or not source.is_file():
            raise CoverageFailure("report_validation", "python", "COVERAGE_SOURCE_MAPPING_INVALID")
        paths.append(normalized)
    if not paths or len(paths) != len(set(paths)):
        raise CoverageFailure("report_validation", "python", "COVERAGE_SOURCE_MAPPING_INVALID")
    return sorted(paths)


def _normal_dotnet_source_path(context: GitContext, value: str) -> str:
    source = Path(value)
    if not source.is_absolute():
        source = context.repository_root / source
    try:
        resolved = source.resolve()
        normalized = _normalized_relative_source_path(
            str(resolved.relative_to(context.repository_root.resolve())).replace("\\", "/")
        )
    except (OSError, ValueError) as error:
        raise CoverageFailure(
            "report_validation", "dotnet", "COVERAGE_SOURCE_PATH_ESCAPE"
        ) from error
    if source.is_symlink() or not source.is_file() or not normalized.endswith(".cs"):
        raise CoverageFailure("report_validation", "dotnet", "COVERAGE_SOURCE_MAPPING_INVALID")
    return normalized


def _is_non_test_dotnet_source(normalized: str) -> bool:
    components = normalized.casefold().split("/")
    excluded = {"test", "tests", "fixture", "fixtures", "test-app"}
    return not any(
        component in excluded or component.endswith(".tests") for component in components
    )


def _validate_dotnet_sources(
    root: ElementTree.Element, context: GitContext, report: CoverageReportPlan
) -> list[str]:
    paths: list[str] = []
    for element in root.iter():
        if _xml_local_name(element.tag) != "File":
            continue
        full_path = element.attrib.get("fullPath")
        if not isinstance(full_path, str):
            raise CoverageFailure("report_validation", "dotnet", "COVERAGE_SOURCE_MAPPING_INVALID")
        paths.append(_normal_dotnet_source_path(context, full_path))
    if (
        not paths
        or len(paths) != len(set(paths))
        or not any(_is_non_test_dotnet_source(path) for path in paths)
    ):
        raise CoverageFailure("report_validation", "dotnet", "COVERAGE_SOURCE_MAPPING_INVALID")
    if (
        report.project is not None
        and report.project.endswith("NetCoreDbg.Mcp.Stateless.Tests.csproj")
        and not any(path.startswith("host/NetCoreDbg.Mcp.Stateless/") for path in paths)
    ):
        raise CoverageFailure(
            "report_validation", "dotnet", "COVERAGE_STATELESS_HOST_MAPPING_MISSING"
        )
    return sorted(paths)


def _positive_integer(value: Any, language: str) -> int:
    if not isinstance(value, str) or not re.fullmatch(r"[1-9][0-9]*", value):
        raise CoverageFailure("report_validation", language, "COVERAGE_DENOMINATOR_INVALID")
    return int(value)


def _coverage_report_binding(
    plan: CoveragePlan, context: GitContext, report: CoverageReportPlan
) -> dict[str, Any]:
    contents = _coverage_file_bytes(plan, context, report)
    try:
        root = ElementTree.fromstring(contents)
    except ElementTree.ParseError as error:
        raise CoverageFailure(
            "report_validation", report.language, "COVERAGE_REPORT_XML_INVALID"
        ) from error
    if report.language == "python":
        if _xml_local_name(root.tag) != "coverage":
            raise CoverageFailure("report_validation", "python", "COVERAGE_XML_ROOT_INVALID")
        denominator = _positive_integer(root.attrib.get("lines-valid"), "python")
        sources = _validate_python_sources(root, context)
    elif report.language == "dotnet":
        if _xml_local_name(root.tag) != "CoverageSession":
            raise CoverageFailure("report_validation", "dotnet", "COVERAGE_XML_ROOT_INVALID")
        summary = next((child for child in root if _xml_local_name(child.tag) == "Summary"), None)
        if summary is None:
            raise CoverageFailure("report_validation", "dotnet", "COVERAGE_DENOMINATOR_INVALID")
        denominator = _positive_integer(summary.attrib.get("numSequencePoints"), "dotnet")
        sources = _validate_dotnet_sources(root, context, report)
    else:
        raise CoverageFailure("report_validation", None, "COVERAGE_REPORT_LANGUAGE_INVALID")
    return {
        "path": report.normalized_path,
        "project": report.project,
        "sha256": hashlib.sha256(contents).hexdigest(),
        "bytes": len(contents),
        "xml_root": _xml_local_name(root.tag),
        "coverage_denominator": denominator,
        "mapped_source_count": len(sources),
        "source_path_set_sha256": _source_path_set_sha256(sources),
        "captured_head": context.head,
    }


def validate_coverage_evidence(plan: CoveragePlan, context: GitContext) -> dict[str, Any]:
    marker_sha256 = validate_coverage_marker(plan, context)
    if len(plan.dotnet_reports) != len(CLOSED_DOTNET_COVERAGE_PROJECTS):
        raise CoverageFailure("report_validation", "dotnet", "COVERAGE_REPORT_SET_INVALID")
    bindings = [_coverage_report_binding(plan, context, report) for report in plan.reports]
    if len({binding["path"] for binding in bindings}) != len(bindings):
        raise CoverageFailure("report_validation", None, "COVERAGE_REPORT_PATH_DUPLICATE")
    dotnet_bindings = [binding for binding in bindings if binding["project"] is not None]
    python_bindings = [binding for binding in bindings if binding["project"] is None]
    return {
        "evidence_sets": [
            {
                "language": "dotnet",
                "format": "opencover",
                "run_id": plan.run_id,
                "marker_sha256": marker_sha256,
                "reports": dotnet_bindings,
            },
            {
                "language": "python",
                "format": "cobertura",
                "run_id": plan.run_id,
                "marker_sha256": marker_sha256,
                "reports": python_bindings,
            },
        ]
    }


def coverage_environment(
    context: GitContext, plan: CoveragePlan, clean_environment: Mapping[str, str]
) -> dict[str, str]:
    environment = scrub_sonar_environment(clean_environment)
    external_environment = (
        context.coordination_root
        / ".agent"
        / "tmp"
        / "sonarqube-coverage-python-venv"
        / context.head
        / plan.run_id
    )
    environment.update(
        {
            "UV_PROJECT_ENVIRONMENT": str(external_environment),
            "PYTHONDONTWRITEBYTECODE": "1",
            "COVERAGE_FILE": str(plan.root / "python" / ".coverage"),
        }
    )
    return environment


def coverage_producer_commands(context: GitContext, plan: CoveragePlan) -> list[list[str]]:
    commands = [
        ["uv", "sync", "--locked", "--extra", "dev"],
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "coverage",
            "run",
            "--branch",
            "-m",
            "pytest",
            "-p",
            "no:cacheprovider",
            "-q",
        ],
        [
            "uv",
            "run",
            "--no-sync",
            "python",
            "-m",
            "coverage",
            "xml",
            "-o",
            str(plan.python_report.absolute_path),
        ],
    ]
    for report in plan.dotnet_reports:
        if report.project is None:
            raise RunnerError("Coverage report project is invalid.")
        commands.append(
            [
                "dotnet",
                "test",
                str(context.repository_root / report.project),
                "--no-build",
                "--no-restore",
                "-nr:false",
                "/p:CollectCoverage=true",
                "/p:CoverletOutputFormat=opencover",
                f"/p:CoverletOutput={report.absolute_path}",
            ]
        )
    return commands


def run_coverage_producers(
    context: GitContext,
    plan: CoveragePlan,
    environment: Mapping[str, str],
    secrets: Collection[str],
    deadline: float,
) -> None:
    for report in plan.reports:
        try:
            report.absolute_path.parent.mkdir(parents=True, exist_ok=True)
            _assert_claimed_coverage_path(report.absolute_path.parent, plan, context)
        except RunnerError:
            raise
        except OSError as error:
            raise CoverageFailure(
                "report_validation", report.language, "COVERAGE_REPORT_DIRECTORY_CREATE_FAILED"
            ) from error
    commands = coverage_producer_commands(context, plan)
    metadata = (
        ("Python coverage environment sync", "python_producer", "python"),
        ("Python coverage tests", "python_producer", "python"),
        ("Python Cobertura XML", "python_producer", "python"),
    )
    for index, command in enumerate(commands):
        if index < len(metadata):
            label, stage, language = metadata[index]
        else:
            label, stage, language = (".NET OpenCover tests", "dotnet_producer", "dotnet")
        run_coverage_process(
            command,
            cwd=context.repository_root,
            environment=environment,
            secrets=secrets,
            label=label,
            deadline=deadline,
            stage=stage,
            language=language,
        )


def produce_coverage(
    context: GitContext,
    plan: CoveragePlan,
    clean_environment: Mapping[str, str],
    secrets: Collection[str],
) -> dict[str, Any]:
    environment = coverage_environment(context, plan, clean_environment)
    run_coverage_producers(
        context,
        plan,
        environment=environment,
        secrets=secrets,
        deadline=time.monotonic() + CE_TIMEOUT_SECONDS,
    )
    return validate_coverage_evidence(plan, context)


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


def scanner_begin_command(
    scanner: Sequence[str],
    analysis_xml: Path,
    host: str,
    head: str,
    project_version_value: str,
    token: str,
    coverage_plan: CoveragePlan | None = None,
) -> list[str]:
    return [
        *scanner,
        "begin",
        f"/k:{PROJECT_KEY}",
        f"/s:{analysis_xml}",
        f"/d:sonar.host.url={host}",
        f"/d:sonar.scm.revision={head}",
        f"/d:sonar.projectVersion={project_version_value}",
        *(coverage_scanner_properties(coverage_plan) if coverage_plan is not None else ()),
        f"/d:sonar.token={token}",
    ]


def scanner_end_command(scanner: Sequence[str], token: str) -> list[str]:
    return [*scanner, "end", f"/d:sonar.token={token}"]


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
    excluded_parts = {".git", ".agent", ".sonarqube", "bin", "obj", "fixtures", "test-app"}
    discovered_projects = {
        path.resolve()
        for path in iter_scanner_tree(repository_root, "*.csproj")
        if not excluded_parts.intersection(
            part.lower() for part in path.relative_to(repository_root).parts
        )
    }
    projects = sorted(
        discovered_projects | set(solution_projects),
        key=lambda path: path.as_posix().lower(),
    )
    if not projects:
        raise RunnerError("C# project inventory is empty.")
    return solution, projects, sorted(
        discovered_projects - set(solution_projects), key=lambda path: path.as_posix().lower()
    )


def scanner_metadata(
    repository_root: Path, expected_head: str, expected_project_version: str
) -> dict[str, Any]:
    metadata_root = repository_root / ".sonarqube"
    if not metadata_root.is_dir():
        raise RunnerError("SonarScanner did not create metadata.")
    found: dict[str, list[tuple[str, str]]] = {
        "sonar.projectKey": [],
        "sonar.projectVersion": [],
        "sonar.scm.revision": [],
    }
    for path in iter_scanner_tree(metadata_root, "*"):
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
                    if element.tag.rsplit("}", 1)[-1] == "SonarProjectKey" and element.text:
                        found["sonar.projectKey"].append((relative, element.text.strip()))
            else:
                for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                    name, separator, value = line.partition("=")
                    if separator and name.strip() in found:
                        found[name.strip()].append((relative, value.strip()))
        except (OSError, ElementTree.ParseError) as error:
            raise RunnerError("SonarScanner metadata could not be parsed.") from error
    observed_project_keys = {value for _, value in found["sonar.projectKey"]}
    observed_versions = {value for _, value in found["sonar.projectVersion"]}
    observed_revisions = {value for _, value in found["sonar.scm.revision"]}
    if (
        observed_project_keys != {PROJECT_KEY}
        or observed_versions != {expected_project_version}
        or observed_revisions != {expected_head}
    ):
        raise RunnerError("Observed SonarScanner metadata does not bind the fixed project, release version, and exact HEAD.")
    return {
        "observed": True,
        "project_key": PROJECT_KEY,
        "sonar_project_version": expected_project_version,
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
    server_origin_matches_configured = credential_free_host(values.get("serverUrl", "")) == expected_host
    if not server_origin_matches_configured:
        raise RunnerError("SonarScanner report-task server origin does not match SONAR_HOST_URL.")
    dashboard_url = values.get("dashboardUrl", "")
    dashboard_url_present = bool(dashboard_url)
    if not dashboard_url_present:
        raise RunnerError("SonarScanner report-task lacks its dashboard URL.")
    if response_origin(dashboard_url) != expected_host:
        raise RunnerError("SonarScanner report-task dashboard origin does not match SONAR_HOST_URL.")
    return {
        "observed": True,
        "path": str(path.relative_to(repository_root)).replace("\\", "/"),
        "project_key": PROJECT_KEY,
        "ce_task_id": values["ceTaskId"],
        "server_origin_matches_configured": server_origin_matches_configured,
        "dashboard_url_present": dashboard_url_present,
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
    if not isinstance(project_status, dict):
        raise RunnerError("Analysis-bound quality-gate response is malformed.")
    status = project_status.get("status")
    if status not in {"OK", "WARN", "ERROR", "NONE"}:
        raise RunnerError("Analysis-bound quality-gate response is malformed.")
    conditions = project_status.get("conditions")
    if not isinstance(conditions, list):
        raise RunnerError("Analysis-bound quality-gate conditions are malformed.")
    validated_conditions: list[dict[str, str]] = []
    for condition in conditions:
        if not isinstance(condition, dict):
            raise RunnerError("Analysis-bound quality-gate conditions are malformed.")
        metric_key = condition.get("metricKey")
        condition_status = condition.get("status")
        comparator = condition.get("comparator")
        if (
            not isinstance(metric_key, str)
            or not metric_key.strip()
            or condition_status not in {"OK", "WARN", "ERROR", "NONE"}
            or comparator not in {"GT", "LT", "EQ", "NE"}
        ):
            raise RunnerError("Analysis-bound quality-gate conditions are malformed.")
        validated_condition = {
            "metricKey": metric_key,
            "status": condition_status,
            "comparator": comparator,
        }
        for key in ("warningThreshold", "errorThreshold", "actualValue"):
            if key in condition:
                value = condition[key]
                if not isinstance(value, str):
                    raise RunnerError("Analysis-bound quality-gate conditions are malformed.")
                validated_condition[key] = value
        validated_conditions.append(validated_condition)
    return {
        "analysis_id": analysis_id,
        "status": status,
        "conditions": validated_conditions,
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


def new_code_issue_inventory(host: str, token: str) -> dict[str, Any]:
    return paginated_inventory(
        host,
        "/api/issues/search",
        "issues",
        {
            "components": PROJECT_KEY,
            "issueStatuses": ISSUE_STATUSES,
            "inNewCodePeriod": "true",
        },
        token,
        (
            "key", "rule", "severity", "status", "issueStatus", "resolution", "type",
            "component", "project", "line", "message", "impacts", "creationDate",
            "updateDate", "tags", "textRange", "flows",
        ),
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


def assert_secret_free(receipt: Mapping[str, Any], secrets: Collection[str]) -> None:
    encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False)
    if any(secret and secret in encoded for secret in secrets):
        raise RunnerError("Refusing to write a receipt containing a SonarQube credential.")


def write_receipt(path: Path, receipt: Mapping[str, Any], secrets: Collection[str]) -> None:
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
        "new_code_issues", "issue_dispositions", "hotspots", "hotspot_dispositions", "cleanup", "post_scan_head",
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
    cleanup = receipt["cleanup"]
    if (
        not isinstance(cleanup, dict)
        or cleanup.get("status") != "PASS"
        or not isinstance(cleanup.get("removed"), list)
        or any(not isinstance(path, str) or not path for path in cleanup["removed"])
    ):
        raise RunnerError("PASS receipt lacks successful generated-artifact cleanup evidence.")
    removed = cleanup["removed"]
    if (
        any(
            "\\" in path
            or path.startswith("/")
            or re.match(r"^[A-Za-z]:", path)
            or any(part in {"", ".", ".."} for part in path.split("/"))
            for path in removed
        )
        or len(set(removed)) != len(removed)
        or removed
        != sorted(
            removed,
            key=lambda path: (-len(path.split("/")), path.casefold(), path),
        )
    ):
        raise RunnerError("PASS receipt has invalid generated-artifact cleanup removals.")
    if not isinstance(scanner, dict) or not scanner.get("observed") or scanner.get("project_key") != PROJECT_KEY or scanner.get("sonar_scm_revision") != receipt.get("captured_head"):
        raise RunnerError("PASS receipt lacks observed scanner project/revision evidence.")
    if (
        not isinstance(task_report, dict)
        or set(task_report)
        != {
            "observed",
            "path",
            "project_key",
            "ce_task_id",
            "server_origin_matches_configured",
            "dashboard_url_present",
        }
        or task_report.get("observed") is not True
        or not isinstance(task_report.get("path"), str)
        or not task_report.get("path")
        or task_report.get("project_key") != PROJECT_KEY
        or not isinstance(task_report.get("ce_task_id"), str)
        or not task_report.get("ce_task_id")
        or task_report.get("server_origin_matches_configured") is not True
        or task_report.get("dashboard_url_present") is not True
    ):
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
    validate_inventory(
        receipt["new_code_issues"],
        "/api/issues/search",
        {
            "components": PROJECT_KEY,
            "issueStatuses": ISSUE_STATUSES,
            "inNewCodePeriod": "true",
        },
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
    inherited_environment = process_environment()
    clean_environment = scrub_sonar_environment(inherited_environment)
    context = git_context(Path.cwd(), clean_environment)
    run_id = str(uuid.uuid4())
    target_receipt = receipt_path(context, role)
    receipt = receipt_base(context, role, run_id)
    secrets = sonar_secret_values(inherited_environment)

    with project_lock(context.coordination_root, role, context.head, run_id):
        write_receipt(target_receipt, receipt, secrets)
        try:
            credentials = load_credentials(context, inherited_environment, secrets)
            secrets.update(credentials.values())
            receipt["credential_inputs"] = list(REQUIRED_ENV)
            if role == "post-merge":
                assert_post_merge_target(context, clean_environment)
            receipt["generated_artifacts_removed_before_scan"] = clear_generated_artifacts(
                context, clean_environment
            )
            receipt["cleanliness"] = {"pre": strict_cleanliness(context, clean_environment, "scanner begin")}
            analysis_xml = context.repository_root / "SonarQube.Analysis.xml"
            receipt["analysis_xml_project_key"] = project_key_from_xml(analysis_xml)
            release_version = project_version(context.repository_root)
            receipt["project_version"] = release_version
            scanner = discover_scanner(scanner_override)
            receipt["scanner"] = scanner

            receipt["pre_scan_issues"] = issue_inventory(
                credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"]
            )
            scanner_env = scanner_environment(inherited_environment, credentials)
            run_process(
                scanner_begin_command(
                    scanner,
                    analysis_xml,
                    credentials["SONAR_HOST_URL"],
                    context.head,
                    release_version,
                    credentials["SONAR_TOKEN"],
                ),
                cwd=context.repository_root,
                environment=scanner_env,
                secrets=secrets,
                label="SonarScanner begin",
                credential_input_names=("SONAR_TOKEN",),
            )
            receipt["scanner_metadata"] = scanner_metadata(
                context.repository_root, context.head, release_version
            )

            solution, projects, standalone_projects = project_inventory(context.repository_root)
            receipt["build_inventory"] = {
                "solution": str(solution.relative_to(context.repository_root)),
                "projects": [str(project.relative_to(context.repository_root)) for project in projects],
                "standalone_projects": [
                    str(project.relative_to(context.repository_root)) for project in standalone_projects
                ],
            }
            run_process(
                ["dotnet", "build", str(solution), "-nr:false"],
                cwd=context.repository_root,
                environment=clean_environment,
                secrets=secrets,
                label="Solution build",
            )
            for project in standalone_projects:
                run_process(
                    ["dotnet", "build", str(project), "-nr:false"],
                    cwd=context.repository_root,
                    environment=clean_environment,
                    secrets=secrets,
                    label=f"Standalone project build ({project.name})",
                )
            run_process(
                scanner_end_command(scanner, credentials["SONAR_TOKEN"]),
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
            receipt["post_scan_issues"] = issue_inventory(
                credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"]
            )
            receipt["new_code_issues"] = new_code_issue_inventory(
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
            assert_head_unchanged(context, clean_environment)
            post_scan_cleanup = clear_generated_artifacts(context, clean_environment)
            receipt["generated_artifacts_removed_after_scan"] = post_scan_cleanup
            receipt["cleanup"] = {"status": "PASS", "removed": post_scan_cleanup}
            receipt["cleanliness"]["post"] = strict_cleanliness(context, clean_environment, "receipt publication")
            receipt["analysis_current_final"] = current_analysis_binding(
                credentials["SONAR_HOST_URL"], analysis_id, context.head, credentials["SONAR_READ_TOKEN"]
            )
            assert_head_unchanged(context, clean_environment)
            receipt["post_scan_head"] = context.head
            require_ok_quality_gate(receipt["quality_gate"])
            if receipt["issue_dispositions"]["blocking_count"]:
                raise RunnerError("Current project issues include a prohibited disposition.")
            if receipt["hotspot_dispositions"]["blocking_count"]:
                raise RunnerError("Current project contains security hotspots requiring disposition.")
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
        except GeneratedArtifactCleanupError as error:
            receipt["outcome"] = "BLOCKED"
            receipt["cleanup"] = {
                "status": "BLOCKED",
                "removed": error.removed,
                "failure": {
                    "path": error.path,
                    "operation": error.operation,
                    "error_type": error.error_type,
                },
            }
            quality_gate = receipt.get("quality_gate")
            if isinstance(quality_gate, Mapping):
                try:
                    require_ok_quality_gate(quality_gate)
                except RunnerError as quality_gate_error:
                    receipt["failure"] = str(quality_gate_error)
                    receipt["completed_at"] = utc_now()
                    write_receipt(target_receipt, receipt, secrets)
                    raise error from quality_gate_error
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
