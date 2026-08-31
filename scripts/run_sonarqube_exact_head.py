#!/usr/bin/env python3
"""Run one secret-free, exact-head SonarQube release scan."""

from __future__ import annotations

import argparse
import hashlib
import json
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
from collections.abc import Collection, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, NoReturn

PROJECT_KEY = "thebtf_netcoredbg_mcp"
REQUIRED_ENV = ("SONAR_HOST_URL", "SONAR_TOKEN", "SONAR_READ_TOKEN")
SONAR_ENV = (*REQUIRED_ENV, "SONAR_ADMIN_TOKEN")
SIMPLE_DOTENV_ASSIGNMENT_RE = re.compile(r"(?P<name>[A-Z_][A-Z0-9_]*)=(?P<value>[^\r\n]*)\Z")
RECEIPT_SCHEMA_VERSION = 2
EXACT_HEAD_RECEIPT_V3_SCHEMA_VERSION = 3
CE_TIMEOUT_SECONDS = 10 * 60
INDEX_TIMEOUT_SECONDS = 2 * 60
POLL_SECONDS = 5
PAGE_SIZE = 500
RESULT_CAP = 10_000
LOCK_LEASE_SECONDS = 30 * 60
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SOLUTION_PROJECT_RE = re.compile(
    r'^Project\("[^"]+"\) = "([^"]+)", "([^"]+\.csproj)"', re.MULTILINE
)
ISSUE_STATUSES = "OPEN,CONFIRMED,FALSE_POSITIVE,ACCEPTED,FIXED,IN_SANDBOX"
GENERATED_DIRECTORY_NAMES = {"bin", "obj"}
GENERATED_ROOT_NAMES = {".sonarqube", ".scannerwork"}

WAVE2_ENTRY_RELATIVE_PATH = "specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json"
WAVE2_RECEIPT_RELATIVE_PATH = "specs/013-owner-scoped-prebuild-cleanup/acceptance-receipt.md"
COVERAGE_PARENT_RELATIVE_PATH = ".tmp/sonarqube-coverage"
COVERAGE_PY_VERSION = "7.15.4"
COVERLET_MSBUILD_VERSION = "10.0.1"
TEST_SDK_VERSION = "17.12.0"
COBERTURA_NORMALIZER = "cobertura-merge-normalize-v1"
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
RELATIVE_PATH_RE = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._/-]+$")
WINDOWS_ABSOLUTE_PATH_RE = re.compile(r"^[A-Za-z]:[\\/]")
FIXED_COVERAGE_PROJECTS = (
    (
        "codesearch-core",
        "host/NetCoreDbg.Mcp.CodeSearch.Core.Tests/NetCoreDbg.Mcp.CodeSearch.Core.Tests.csproj",
        None,
    ),
    (
        "host",
        "host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj",
        None,
    ),
    (
        "stateless-preview",
        "host/NetCoreDbg.Mcp.Stateless.Preview.Tests/NetCoreDbg.Mcp.Stateless.Preview.Tests.csproj",
        None,
    ),
    (
        "stateless",
        "host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj",
        "host/NetCoreDbg.Mcp.Stateless/bin/Debug/net8.0",
    ),
    (
        "host-prompts",
        "tests/dotnet/NetCoreDbg.Mcp.Host.PromptTests/NetCoreDbg.Mcp.Host.PromptTests.csproj",
        None,
    ),
)


class RunnerError(RuntimeError):
    """A fail-closed release-gate error safe to place in a receipt."""


class GeneratedArtifactCleanupError(RunnerError):
    """A receipt-safe failure deleting one generated scanner artifact."""

    def __init__(self, path: str, operation: str, error_type: str, removed: Sequence[str]) -> None:
        self.path = path
        self.operation = operation
        self.error_type = error_type
        self.removed = list(removed)
        super().__init__(f"Generated artifact cleanup {operation} failed for {path}: {error_type}.")


class CredentialsUnavailable(RunnerError):
    """A credential-gate blocker that never includes a credential value."""

    def __init__(self, *input_names: str) -> None:
        super().__init__(
            "SONAR_CREDENTIALS_UNAVAILABLE: " + ", ".join(sorted(set(input_names))) + "."
        )


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
class CoverageProjectSpec:
    """One closed private VSTest producer input."""

    id: str
    project: Path
    raw_cobertura_input: Path
    include_directory: str | None


@dataclass(frozen=True)
class CoveragePlan:
    """Pure, head-bound paths for one coverage transaction."""

    run_id: str
    head: str
    repository_root: Path
    root: Path
    marker: Path
    tracked_wave2_entry: Path
    resolved_wave2_entry: Path
    python_data: Path
    python_report: Path
    dotnet_report: Path
    dotnet_inputs: tuple[
        CoverageProjectSpec,
        CoverageProjectSpec,
        CoverageProjectSpec,
        CoverageProjectSpec,
        CoverageProjectSpec,
    ]


@dataclass(frozen=True)
class CoverageRunClaim:
    """The exclusively-created run root and its immutable marker."""

    root: Path
    marker: Path
    resolved_wave2_entry: Path
    marker_sha256: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--role", required=True, choices=("diagnostic", "candidate", "post-merge"))
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
    if (
        normalized_handle is None
        or normalized_handle == 0
        or normalized_handle == invalid_handle_value
    ):
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
        if (
            file_status.st_uid != getattr(os, "geteuid")()
            or stat.S_IMODE(file_status.st_mode) & 0o077
        ):
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
        _fields_ = [
            ("ace_type", ctypes.c_ubyte),
            ("ace_flags", ctypes.c_ubyte),
            ("ace_size", wintypes.WORD),
        ]

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
    advapi32.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
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
        require(
            advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(), token_query, ctypes.byref(token_handle)
            )
        )
        try:
            required_size = wintypes.DWORD()
            if advapi32.GetTokenInformation(
                token_handle, token_user, None, 0, ctypes.byref(required_size)
            ):
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
        if attribute_tag.file_attributes & (
            file_attribute_reparse_point | file_attribute_directory
        ):
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
            if (
                not owner_sid.value
                or not advapi32.IsValidSid(owner_sid)
                or not advapi32.EqualSid(owner_sid, user_sid)
            ):
                raise PermissionError(
                    "The primary .env owner does not match the current token user."
                )
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
                raise PermissionError(
                    "The primary .env DACL is not protected from inherited access."
                )
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
                    raise PermissionError(
                        "The primary .env grants access outside the current token user."
                    )
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
            raise RunnerError(
                "SONAR_ADMIN_TOKEN is forbidden; use only project-scoped credentials."
            )
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
        name: (
            process_env[name] if name in process_env else dotenv_credentials.get(name, "")
        ).strip()
        for name in REQUIRED_ENV
    }
    missing = [name for name, value in credentials.items() if not value]
    if missing:
        raise CredentialsUnavailable(*missing)
    credentials["SONAR_HOST_URL"] = credential_free_host(credentials["SONAR_HOST_URL"])
    return credentials


def scrub_sonar_environment(source: Mapping[str, str]) -> dict[str, str]:
    return {key: value for key, value in source.items() if not is_sonar_environment_name(key)}


def scanner_environment(
    base_environment: Mapping[str, str], credentials: Mapping[str, str]
) -> dict[str, str]:
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
        raise RunnerError(
            f"{label} could not start: {error.__class__.__name__} code={code}: {detail}"
        ) from error
    output = completed.stdout or ""
    if output:
        print(redact(output, secrets), end="" if output.endswith("\n") else "\n", flush=True)
    if completed.returncode:
        if credential_input_names and re.search(
            r"\b(?:401|403|authenticat|authoriz|forbidden|token)\b", output, re.IGNORECASE
        ):
            raise CredentialsUnavailable(*credential_input_names)
        raise RunnerError(f"{label} failed with exit code {completed.returncode}.")


def git_result(
    repository_root: Path, environment: Mapping[str, str], *arguments: str
) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            ["git", *arguments],
            cwd=repository_root,
            env=dict(environment),
            capture_output=True,
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
    repository_root = Path(
        git_output(start_directory, environment, "rev-parse", "--show-toplevel")
    ).resolve()
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
        raise RunnerError(
            "Exact-head scan requires a linked disposable worktree, not the primary checkout."
        )
    return GitContext(repository_root, common_dir, git_dir, common_dir.parent, head)


def strict_cleanliness(
    context: GitContext, environment: Mapping[str, str], phase: str
) -> dict[str, Any]:
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


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json_bytes(value))


def _is_sha256(value: Any) -> bool:
    return isinstance(value, str) and bool(SHA256_RE.fullmatch(value)) and value != "0" * 64


def _is_relative_path(value: Any) -> bool:
    return isinstance(value, str) and bool(RELATIVE_PATH_RE.fullmatch(value))


def _load_json_object(raw: bytes, label: str) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise RunnerError(f"{label} contains duplicate key {key!r}.")
            result[key] = value
        return result

    try:
        decoded = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, RunnerError) as error:
        if isinstance(error, RunnerError):
            raise
        raise RunnerError(f"{label} is not valid UTF-8 JSON.") from error
    if not isinstance(decoded, dict):
        raise RunnerError(f"{label} must contain a JSON object.")
    return decoded


def _wave2_unverified(detail: str) -> NoReturn:
    raise RunnerError(f"WAVE2_CLOSURE_UNVERIFIED: {detail}")


def git_blob_bytes(
    repository_root: Path,
    environment: Mapping[str, str],
    revision: str,
    relative_path: str,
) -> bytes:
    if not _is_relative_path(relative_path):
        raise RunnerError("Git blob path is invalid.")
    try:
        completed = subprocess.run(
            ["git", "show", f"{revision}:{relative_path}"],
            cwd=repository_root,
            env=dict(environment),
            capture_output=True,
            check=False,
        )
    except OSError as error:
        raise RunnerError("Git is unavailable for canonical blob validation.") from error
    if completed.returncode:
        raise RunnerError("Git canonical blob validation command failed.")
    return completed.stdout


def _git_is_ancestor(
    repository_root: Path, environment: Mapping[str, str], ancestor: str, descendant: str
) -> bool:
    if not SHA_RE.fullmatch(ancestor) or not SHA_RE.fullmatch(descendant):
        return False
    return (
        git_result(
            repository_root, environment, "merge-base", "--is-ancestor", ancestor, descendant
        ).returncode
        == 0
    )


def _ensure_git_object(
    repository_root: Path, environment: Mapping[str, str], object_id: str
) -> None:
    if not SHA_RE.fullmatch(object_id):
        _wave2_unverified("first-party PR evidence has an invalid Git object ID")
    if (
        git_result(
            repository_root, environment, "cat-file", "-e", f"{object_id}^{{commit}}"
        ).returncode
        == 0
    ):
        return
    fetched = git_result(repository_root, environment, "fetch", "--no-tags", "origin", object_id)
    if (
        fetched.returncode
        or git_result(
            repository_root, environment, "cat-file", "-e", f"{object_id}^{{commit}}"
        ).returncode
    ):
        _wave2_unverified("required first-party PR Git object is unavailable locally")


def _github_pull_request_evidence(
    entry: Mapping[str, Any], environment: Mapping[str, str]
) -> dict[str, Any]:
    integration = entry.get("integration")
    number = integration.get("pull_request") if isinstance(integration, Mapping) else None
    if type(number) is not int or number <= 0:
        _wave2_unverified("source does not name a valid pull request")
    endpoint = f"repos/thebtf/netcoredbg-mcp/pulls/{number}"
    token = environment.get("GITHUB_TOKEN")
    if token:
        request = urllib.request.Request(
            f"https://api.github.com/{endpoint}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with API_OPENER.open(request, timeout=30) as response:
                if response_origin(response.geturl()) != "https://api.github.com":
                    _wave2_unverified("first-party PR response has an unexpected origin")
                payload = response.read()
        except (OSError, urllib.error.HTTPError, urllib.error.URLError) as error:
            _wave2_unverified("first-party PR evidence is unavailable")
            raise AssertionError("unreachable") from error
    else:
        gh = shutil.which("gh")
        if not gh:
            _wave2_unverified("first-party PR evidence is unavailable")
        try:
            completed = subprocess.run(
                [gh, "api", endpoint],
                env=dict(environment),
                capture_output=True,
                check=False,
            )
        except OSError as error:
            _wave2_unverified("first-party PR evidence is unavailable")
            raise AssertionError("unreachable") from error
        if completed.returncode:
            _wave2_unverified("first-party PR evidence is unavailable")
        payload = completed.stdout
    try:
        response = _load_json_object(payload, "first-party pull-request evidence")
        head = response["head"]
        if not isinstance(head, Mapping):
            raise KeyError("head")
        return {
            "number": response.get("number"),
            "head_ref": head.get("ref"),
            "head_sha": head.get("sha"),
            "merge_commit_sha": response.get("merge_commit_sha"),
            "merged": response.get("merged"),
        }
    except (KeyError, TypeError):
        _wave2_unverified("first-party PR evidence has an invalid shape")
    raise AssertionError("unreachable")


def resolve_wave2_entry(
    context: GitContext, environment: Mapping[str, str] | None = None
) -> dict[str, Any]:
    """Read only the tracked Wave-2 entry from its canonical Git blob."""

    environment = (
        dict(environment)
        if environment is not None
        else scrub_sonar_environment(process_environment())
    )
    path = context.repository_root / WAVE2_ENTRY_RELATIVE_PATH
    try:
        metadata = _scanner_tree_metadata(path)
    except RunnerError as error:
        _wave2_unverified(str(error))
        raise AssertionError("unreachable") from error
    if not stat.S_ISREG(getattr(metadata, "st_mode", 0)) or not is_tracked(
        context.repository_root, environment, path
    ):
        _wave2_unverified("tracked Wave-2 closure entry is absent or untracked")
    try:
        blob = git_blob_bytes(
            context.repository_root, environment, context.head, WAVE2_ENTRY_RELATIVE_PATH
        )
        return _load_json_object(blob, "tracked Wave-2 closure entry")
    except RunnerError as error:
        _wave2_unverified(str(error))
        raise AssertionError("unreachable") from error


def _runtime_wave2_evidence(
    entry: Mapping[str, Any], context: GitContext, environment: Mapping[str, str]
) -> dict[str, Any]:
    source_blob = git_blob_bytes(
        context.repository_root, environment, context.head, WAVE2_ENTRY_RELATIVE_PATH
    )
    closure_blob = git_blob_bytes(
        context.repository_root, environment, context.head, WAVE2_RECEIPT_RELATIVE_PATH
    )
    pull_request = _github_pull_request_evidence(entry, environment)
    head_sha = pull_request.get("head_sha")
    merge_sha = pull_request.get("merge_commit_sha")
    if not isinstance(head_sha, str) or not isinstance(merge_sha, str):
        _wave2_unverified("first-party PR evidence omits a head or merge commit")
    _ensure_git_object(context.repository_root, environment, head_sha)
    _ensure_git_object(context.repository_root, environment, merge_sha)
    observed_main = git_output(
        context.repository_root, environment, "rev-parse", "--verify", "origin/main^{commit}"
    )
    artifact_commit = git_output(
        context.repository_root,
        environment,
        "log",
        "-1",
        "--format=%H",
        "--follow",
        "--",
        WAVE2_ENTRY_RELATIVE_PATH,
    )
    try:
        head_tree = git_output(
            context.repository_root, environment, "rev-parse", f"{head_sha}^{{tree}}"
        )
        merge_tree = git_output(
            context.repository_root, environment, "rev-parse", f"{merge_sha}^{{tree}}"
        )
        pr_entry_blob = git_blob_bytes(
            context.repository_root, environment, head_sha, WAVE2_ENTRY_RELATIVE_PATH
        )
        artifact_blob = git_blob_bytes(
            context.repository_root, environment, artifact_commit, WAVE2_ENTRY_RELATIVE_PATH
        )
    except RunnerError as error:
        _wave2_unverified(str(error))
        raise AssertionError("unreachable") from error
    return {
        "tracked": is_tracked(
            context.repository_root,
            environment,
            context.repository_root / WAVE2_ENTRY_RELATIVE_PATH,
        ),
        "source_blob": {"bytes": source_blob, "sha256": _sha256_bytes(source_blob)},
        "closure_receipt_blob": {"bytes": closure_blob, "sha256": _sha256_bytes(closure_blob)},
        "first_party_pull_request": pull_request,
        "candidate_is_ancestor_of_pr_head": _git_is_ancestor(
            context.repository_root,
            environment,
            str(entry.get("accepted_candidate_sha", "")),
            head_sha,
        ),
        "pull_request_head_tree_sha": head_tree,
        "merge_tree_sha": merge_tree,
        "artifact_blob_at_pr_head_matches": source_blob == pr_entry_blob,
        "artifact_commit_sha": artifact_commit,
        "artifact_path_history_valid": artifact_blob == source_blob,
        "merge_is_ancestor_of_observed_main": _git_is_ancestor(
            context.repository_root, environment, merge_sha, observed_main
        ),
        "observed_main_sha": observed_main,
    }


def verify_wave2_entry(
    entry: Mapping[str, Any],
    evidence_or_context: Mapping[str, Any] | GitContext,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fail closed unless reviewed source and squash integration bind one lineage."""

    if not isinstance(entry, Mapping):
        _wave2_unverified("closure entry is not an object")
    expected_entry_keys = {
        "schema_version",
        "wave",
        "closure_status",
        "release_intent",
        "tracked_relative_path",
        "accepted_candidate_sha",
        "closure_receipt",
        "integration",
    }
    if set(entry) != expected_entry_keys:
        _wave2_unverified("closure entry schema keys are invalid")
    closure_receipt = entry.get("closure_receipt")
    integration = entry.get("integration")
    if (
        entry.get("schema_version") != 1
        or entry.get("wave") != 2
        or entry.get("closure_status") != "EXACT_CLOSED"
        or entry.get("release_intent") != "none"
        or entry.get("tracked_relative_path") != WAVE2_ENTRY_RELATIVE_PATH
        or not isinstance(entry.get("accepted_candidate_sha"), str)
        or not SHA_RE.fullmatch(entry["accepted_candidate_sha"])
        or not isinstance(closure_receipt, Mapping)
        or set(closure_receipt) != {"relative_path", "sha256"}
        or closure_receipt.get("relative_path") != WAVE2_RECEIPT_RELATIVE_PATH
        or not _is_sha256(closure_receipt.get("sha256"))
        or not isinstance(integration, Mapping)
        or set(integration) != {"kind", "pull_request", "head_ref", "head_sha"}
        or integration.get("kind") != "pull_request_head"
        or integration.get("pull_request") != 289
        or not isinstance(integration.get("head_ref"), str)
        or not integration["head_ref"]
        or integration.get("head_sha") != entry.get("accepted_candidate_sha")
    ):
        _wave2_unverified("closure entry source schema or reviewed-head binding is invalid")
    if isinstance(evidence_or_context, GitContext):
        runtime_environment = (
            dict(environment)
            if environment is not None
            else scrub_sonar_environment(process_environment())
        )
        try:
            evidence: Mapping[str, Any] = _runtime_wave2_evidence(
                entry, evidence_or_context, runtime_environment
            )
        except RunnerError as error:
            if str(error).startswith("WAVE2_CLOSURE_UNVERIFIED:"):
                raise
            _wave2_unverified(str(error))
            raise AssertionError("unreachable") from error
    elif isinstance(evidence_or_context, Mapping):
        evidence = evidence_or_context
    else:
        _wave2_unverified("closure evidence is unavailable")
    source_blob = evidence.get("source_blob")
    receipt_blob = evidence.get("closure_receipt_blob")
    pull_request = evidence.get("first_party_pull_request")
    if (
        evidence.get("tracked") is not True
        or not isinstance(source_blob, Mapping)
        or not isinstance(source_blob.get("bytes"), bytes)
        or not _is_sha256(source_blob.get("sha256"))
        or source_blob.get("sha256") != _sha256_bytes(source_blob["bytes"])
        or not isinstance(receipt_blob, Mapping)
        or not isinstance(receipt_blob.get("bytes"), bytes)
        or not _is_sha256(receipt_blob.get("sha256"))
        or receipt_blob.get("sha256") != _sha256_bytes(receipt_blob["bytes"])
        or receipt_blob.get("sha256") != closure_receipt.get("sha256")
        or not isinstance(pull_request, Mapping)
        or set(pull_request) != {"number", "head_ref", "head_sha", "merge_commit_sha", "merged"}
        or pull_request.get("number") != integration.get("pull_request")
        or pull_request.get("head_ref") != integration.get("head_ref")
        or not isinstance(pull_request.get("head_sha"), str)
        or not SHA_RE.fullmatch(pull_request["head_sha"])
        or not isinstance(pull_request.get("merge_commit_sha"), str)
        or not SHA_RE.fullmatch(pull_request["merge_commit_sha"])
        or pull_request.get("merged") is not True
        or evidence.get("candidate_is_ancestor_of_pr_head") is not True
        or evidence.get("pull_request_head_tree_sha") != evidence.get("merge_tree_sha")
        or not isinstance(evidence.get("pull_request_head_tree_sha"), str)
        or not SHA_RE.fullmatch(evidence["pull_request_head_tree_sha"])
        or evidence.get("artifact_blob_at_pr_head_matches") is not True
        or not isinstance(evidence.get("artifact_commit_sha"), str)
        or not SHA_RE.fullmatch(evidence["artifact_commit_sha"])
        or evidence.get("artifact_path_history_valid") is not True
        or evidence.get("merge_is_ancestor_of_observed_main") is not True
        or not isinstance(evidence.get("observed_main_sha"), str)
        or not SHA_RE.fullmatch(evidence["observed_main_sha"])
    ):
        _wave2_unverified(
            "closure source, canonical blob, PR, tree, or main-lineage evidence is invalid"
        )
    return {
        "source_sha256": source_blob["sha256"],
        "accepted_candidate_sha": entry["accepted_candidate_sha"],
        "pull_request_head_ref": pull_request["head_ref"],
        "pull_request_head_sha": pull_request["head_sha"],
        "artifact_commit_sha": evidence["artifact_commit_sha"],
        "merge_commit_sha": pull_request["merge_commit_sha"],
        "integrated_tree_sha": evidence["pull_request_head_tree_sha"],
        "observed_main_sha": evidence["observed_main_sha"],
    }


def validate_coverage_project_inventory(projects: Sequence[Any]) -> None:
    observed: list[tuple[str, str, str | None]] = []
    for item in projects:
        if isinstance(item, CoverageProjectSpec):
            observed.append((item.id, item.project.as_posix(), item.include_directory))
        elif isinstance(item, tuple) and len(item) == 3:
            identifier, project, include_directory = item
            observed.append((str(identifier), str(project).replace("\\", "/"), include_directory))
        else:
            raise RunnerError(
                "COVERAGE_VSTEST_INCOMPATIBLE: invalid coverage project inventory item."
            )
    if tuple(observed) != FIXED_COVERAGE_PROJECTS:
        raise RunnerError(
            "COVERAGE_VSTEST_INCOMPATIBLE: coverage project inventory is not the fixed ordered set."
        )


def _coverage_executable(name: str) -> str | None:
    if name == "bash" and os.name == "nt":
        git_executable = shutil.which("git")
        if git_executable:
            git_bash = Path(git_executable).resolve().parent.parent / "bin" / "bash.exe"
            if git_bash.is_file():
                return str(git_bash)
    return shutil.which(name)


def _runtime_coverage_toolchain(
    context: GitContext, environment: Mapping[str, str]
) -> dict[str, Any]:
    executables: dict[str, str | None] = {
        name: _coverage_executable(name) for name in ("uv", "bash", "dotnet")
    }
    for name, executable in executables.items():
        if not executable:
            continue
        try:
            completed = subprocess.run(
                [executable, "--version"],
                cwd=context.repository_root,
                env=dict(environment),
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError:
            executables[name] = None
            continue
        if completed.returncode:
            executables[name] = None
    projects: list[dict[str, Any]] = []
    for identifier, project_relative, _ in FIXED_COVERAGE_PROJECTS:
        project = context.repository_root / project_relative
        try:
            root = ElementTree.parse(project).getroot()
        except (OSError, ElementTree.ParseError) as error:
            raise RunnerError(
                "COVERAGE_VSTEST_INCOMPATIBLE: project evaluation is unavailable."
            ) from error
        target_framework = next(
            (
                element.text.strip()
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] == "TargetFramework" and element.text
            ),
            None,
        )
        testing_platform_property = next(
            (
                (element.text or "").strip().casefold()
                for element in root.iter()
                if element.tag.rsplit("}", 1)[-1] == "TestingPlatformDotnetTestSupport"
            ),
            "",
        )
        packages = [
            {
                "include": element.attrib.get("Include", ""),
                "version": element.attrib.get("Version", ""),
                "private_assets": element.attrib.get("PrivateAssets", ""),
            }
            for element in root.iter()
            if element.tag.rsplit("}", 1)[-1] == "PackageReference"
        ]
        by_name = {str(package["include"]).casefold(): package for package in packages}
        coverlet = by_name.get("coverlet.msbuild")
        test_sdk = by_name.get("microsoft.net.test.sdk")
        mtp_active = testing_platform_property in {"true", "1", "yes"} or any(
            "microsoft.testing.platform" in str(package["include"]).casefold()
            for package in packages
        )
        projects.append(
            {
                "id": identifier,
                "project": project_relative,
                "target_framework": target_framework,
                "coverlet_msbuild": coverlet["version"] if coverlet else None,
                "coverlet_private_assets": coverlet["private_assets"] if coverlet else None,
                "test_sdk": test_sdk["version"] if test_sdk else None,
                "test_platform": "vstest",
                "mtp_active": mtp_active,
            }
        )
    return {"executables": executables, "projects": projects}


def preflight_coverage_toolchain(
    toolchain: Mapping[str, Any] | None = None,
    context: GitContext | None = None,
    environment: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Validate the exact fixed VSTest/Coverlet tuple before scanner begin."""

    if toolchain is None:
        if context is None:
            raise RunnerError("COVERAGE_TOOL_UNAVAILABLE: no toolchain context was supplied.")
        toolchain = _runtime_coverage_toolchain(
            context,
            dict(environment)
            if environment is not None
            else scrub_sonar_environment(process_environment()),
        )
    if not isinstance(toolchain, Mapping):
        raise RunnerError("COVERAGE_TOOL_UNAVAILABLE: invalid toolchain evidence.")
    executables = toolchain.get("executables")
    projects = toolchain.get("projects")
    if not isinstance(executables, Mapping):
        raise RunnerError("COVERAGE_TOOL_UNAVAILABLE: executable evidence is missing.")
    if any(
        not isinstance(executables.get(name), str) or not executables[name]
        for name in ("uv", "bash", "dotnet")
    ):
        raise RunnerError("COVERAGE_TOOL_UNAVAILABLE: uv, bash, and dotnet are required.")
    if not isinstance(projects, list):
        raise RunnerError("COVERAGE_VSTEST_INCOMPATIBLE: project evaluation evidence is missing.")
    if len(projects) != len(FIXED_COVERAGE_PROJECTS) or any(
        not isinstance(project, Mapping) for project in projects
    ):
        raise RunnerError(
            "COVERAGE_VSTEST_INCOMPATIBLE: project evaluation evidence is incomplete."
        )
    inventory = [
        (project.get("id"), project.get("project"), FIXED_COVERAGE_PROJECTS[index][2])
        for index, project in enumerate(projects)
    ]
    try:
        validate_coverage_project_inventory(inventory)
    except RunnerError:
        raise
    for project in projects:
        if not isinstance(project, Mapping):
            raise RunnerError("COVERAGE_VSTEST_INCOMPATIBLE: invalid project evidence.")
        if project.get("mtp_active") is True:
            raise RunnerError(
                "COVERAGE_MTP_INCOMPATIBLE: Microsoft Testing Platform is not supported "
                "by coverlet.msbuild."
            )
        if (
            project.get("target_framework") != "net8.0"
            or project.get("coverlet_msbuild") != COVERLET_MSBUILD_VERSION
            or str(project.get("coverlet_private_assets", "")).casefold() != "all"
            or project.get("test_sdk") != TEST_SDK_VERSION
            or str(project.get("test_platform", "")).casefold() != "vstest"
        ):
            raise RunnerError(
                "COVERAGE_VSTEST_INCOMPATIBLE: project does not satisfy the fixed VSTest tuple."
            )
    return {"executables": dict(executables), "projects": [dict(project) for project in projects]}


def _coverage_relative(plan: CoveragePlan, path: Path) -> str:
    try:
        relative = path.relative_to(plan.repository_root).as_posix()
    except ValueError as error:
        raise RunnerError(
            "COVERAGE_MARKER_INVALID: coverage path escapes the repository."
        ) from error
    if not _is_relative_path(relative):
        raise RunnerError("COVERAGE_MARKER_INVALID: coverage path is not repository-relative.")
    return relative


def derive_coverage_plan(context: GitContext, run_id: str) -> CoveragePlan:
    """Derive paths only; no directory or report is created by this function."""

    try:
        normalized_run_id = str(uuid.UUID(run_id))
    except (ValueError, AttributeError) as error:
        raise RunnerError("COVERAGE_MARKER_INVALID: run ID must be a UUID.") from error
    if not SHA_RE.fullmatch(context.head):
        raise RunnerError(
            "COVERAGE_HEAD_MISMATCH: coverage plan requires a complete captured head."
        )
    root = context.repository_root / COVERAGE_PARENT_RELATIVE_PATH / normalized_run_id
    inputs = tuple(
        CoverageProjectSpec(
            identifier,
            Path(project_relative),
            root / "dotnet" / "inputs" / identifier / "coverage.cobertura.xml",
            include_directory,
        )
        for identifier, project_relative, include_directory in FIXED_COVERAGE_PROJECTS
    )
    validate_coverage_project_inventory(inputs)
    return CoveragePlan(
        normalized_run_id,
        context.head,
        context.repository_root,
        root,
        root / "coverage-run.json",
        context.repository_root / WAVE2_ENTRY_RELATIVE_PATH,
        root / "wave2-entry.json",
        root / "python" / ".coverage",
        root / "python" / "coverage.xml",
        root / "dotnet" / "coverage.xml",
        inputs,  # type: ignore[arg-type]
    )


def coverage_scanner_properties(plan: CoveragePlan) -> tuple[str, str]:
    python_path = _coverage_relative(plan, plan.python_report)
    dotnet_path = _coverage_relative(plan, plan.dotnet_report)
    return (
        f"/d:sonar.python.coverage.reportPaths={python_path}",
        f"/d:sonar.cs.cobertura.reportsPaths={dotnet_path}",
    )


def _path_sha256_or_empty(path: Path) -> str:
    try:
        return _sha256_bytes(path.read_bytes())
    except OSError:
        # Derivation fixtures have no producer source; real claims bind source bytes.
        return _sha256_bytes(b"")


def _resolved_wave2_copy(plan: CoveragePlan, resolved_entry: Mapping[str, Any]) -> dict[str, Any]:
    expected = {
        "source_sha256",
        "accepted_candidate_sha",
        "pull_request_head_ref",
        "pull_request_head_sha",
        "artifact_commit_sha",
        "merge_commit_sha",
        "integrated_tree_sha",
        "observed_main_sha",
    }
    if set(resolved_entry) != expected or not all(
        _is_sha256(resolved_entry.get("source_sha256"))
        if key == "source_sha256"
        else isinstance(resolved_entry.get(key), str)
        and (
            bool(SHA_RE.fullmatch(resolved_entry[key]))
            if key.endswith("sha")
            else bool(resolved_entry[key])
        )
        for key in expected
    ):
        raise RunnerError("COVERAGE_MARKER_INVALID: resolved Wave-2 entry is invalid.")
    return {
        "schema_version": 1,
        "source_kind": "pull_request_head",
        "tracked_relative_path": WAVE2_ENTRY_RELATIVE_PATH,
        **dict(resolved_entry),
        "resolved_relative_path": _coverage_relative(plan, plan.resolved_wave2_entry),
    }


def _coverage_marker(plan: CoveragePlan, resolved_entry: Mapping[str, Any]) -> dict[str, Any]:
    resolved_copy = _resolved_wave2_copy(plan, resolved_entry)
    return {
        "schema_version": 1,
        "run_id": plan.run_id,
        "captured_head": plan.head,
        "project_key": PROJECT_KEY,
        "tool_versions": {
            "coverage_py": COVERAGE_PY_VERSION,
            "coverlet_msbuild": COVERLET_MSBUILD_VERSION,
            "test_sdk": TEST_SDK_VERSION,
        },
        "wave2_entry": resolved_copy,
        "final_reports": [
            {
                "id": "python",
                "language": "python",
                "format": "cobertura",
                "relative_path": _coverage_relative(plan, plan.python_report),
            },
            {
                "id": "dotnet",
                "language": "dotnet",
                "format": "cobertura",
                "relative_path": _coverage_relative(plan, plan.dotnet_report),
            },
        ],
        "dotnet_producers": [
            {
                "id": spec.id,
                "project": spec.project.as_posix(),
                "raw_cobertura_path": _coverage_relative(plan, spec.raw_cobertura_input),
                "include_directory": spec.include_directory,
            }
            for spec in plan.dotnet_inputs
        ],
        "normalizer": {
            "algorithm": COBERTURA_NORMALIZER,
            "output_report_id": "dotnet",
            "input_order": [spec.id for spec in plan.dotnet_inputs],
        },
        "producer_sha256": _path_sha256_or_empty(plan.repository_root / "build" / "coverage.sh"),
        "coveragerc_sha256": _path_sha256_or_empty(plan.repository_root / ".coveragerc"),
    }


def validate_coverage_marker(plan: CoveragePlan, marker: Mapping[str, Any]) -> None:
    expected_reports = [
        ("python", "python", _coverage_relative(plan, plan.python_report)),
        ("dotnet", "dotnet", _coverage_relative(plan, plan.dotnet_report)),
    ]
    expected_producers = [
        (
            spec.id,
            spec.project.as_posix(),
            _coverage_relative(plan, spec.raw_cobertura_input),
            spec.include_directory,
        )
        for spec in plan.dotnet_inputs
    ]
    reports = marker.get("final_reports")
    producers = marker.get("dotnet_producers")
    wave2 = marker.get("wave2_entry")
    normalizer = marker.get("normalizer")
    if (
        set(marker)
        != {
            "schema_version",
            "run_id",
            "captured_head",
            "project_key",
            "tool_versions",
            "wave2_entry",
            "final_reports",
            "dotnet_producers",
            "normalizer",
            "producer_sha256",
            "coveragerc_sha256",
        }
        or marker.get("schema_version") != 1
        or marker.get("run_id") != plan.run_id
        or marker.get("captured_head") != plan.head
        or marker.get("project_key") != PROJECT_KEY
        or not isinstance(reports, list)
        or [
            (item.get("id"), item.get("language"), item.get("relative_path"))
            for item in reports
            if isinstance(item, Mapping)
        ]
        != expected_reports
        or not isinstance(producers, list)
        or [
            (
                item.get("id"),
                item.get("project"),
                item.get("raw_cobertura_path"),
                item.get("include_directory"),
            )
            for item in producers
            if isinstance(item, Mapping)
        ]
        != expected_producers
        or not isinstance(wave2, Mapping)
        or wave2.get("merge_commit_sha") is None
        or wave2.get("integrated_tree_sha") is None
        or not isinstance(normalizer, Mapping)
        or normalizer.get("algorithm") != COBERTURA_NORMALIZER
        or normalizer.get("output_report_id") != "dotnet"
        or normalizer.get("input_order") != [spec.id for spec in plan.dotnet_inputs]
        or not _is_sha256(marker.get("producer_sha256"))
        or not _is_sha256(marker.get("coveragerc_sha256"))
    ):
        raise RunnerError("COVERAGE_MARKER_INVALID: marker does not bind the fixed coverage plan.")


def claim_coverage_run(
    context: GitContext, plan: CoveragePlan, resolved_entry: Mapping[str, Any]
) -> CoverageRunClaim:
    if context.repository_root != plan.repository_root or context.head != plan.head:
        raise RunnerError(
            "COVERAGE_MARKER_INVALID: plan does not belong to the captured exact head."
        )
    try:
        plan.root.parent.mkdir(parents=True, exist_ok=True)
        plan.root.mkdir()
    except FileExistsError as error:
        raise RunnerError(
            "COVERAGE_RUN_ROOT_EXISTS: claimed coverage root already exists."
        ) from error
    except OSError as error:
        raise RunnerError("COVERAGE_MARKER_INVALID: coverage root cannot be created.") from error
    marker = _coverage_marker(plan, resolved_entry)
    validate_coverage_marker(plan, marker)
    try:
        plan.resolved_wave2_entry.write_bytes(_canonical_json_bytes(marker["wave2_entry"]) + b"\n")
        plan.marker.write_bytes(_canonical_json_bytes(marker) + b"\n")
    except OSError as error:
        raise RunnerError("COVERAGE_MARKER_INVALID: coverage marker cannot be written.") from error
    return CoverageRunClaim(
        plan.root,
        plan.marker,
        plan.resolved_wave2_entry,
        _sha256_bytes(plan.marker.read_bytes()),
    )


def _cobertura_output_prefix(path: Path) -> Path:
    suffix = ".cobertura.xml"
    if not path.name.endswith(suffix):
        raise RunnerError("COVERAGE_MARKER_INVALID: private Cobertura path has an invalid suffix.")
    return path.with_name(path.name[: -len(suffix)])


def dotnet_producer_commands(plan: CoveragePlan) -> list[list[str]]:
    commands: list[list[str]] = []
    for spec in plan.dotnet_inputs:
        project = plan.repository_root / spec.project
        commands.append(["dotnet", "restore", str(project), "-nr:false"])
        command = [
            "dotnet",
            "test",
            str(project),
            "--configuration",
            "Debug",
            "--no-restore",
            "-nr:false",
            "-p:CollectCoverage=true",
            "-p:CoverletOutputFormat=cobertura",
            f"-p:CoverletOutput={_cobertura_output_prefix(spec.raw_cobertura_input)}",
        ]
        if spec.include_directory is not None:
            command.append(
                f"-p:IncludeDirectory={plan.repository_root / Path(spec.include_directory)}"
            )
        if spec.id == "stateless":
            command.extend(["--filter", "Coverage!=Exclude"])
        commands.append(command)
    return commands


def run_coverage_producer(plan: CoveragePlan, environment: Mapping[str, str]) -> None:
    """Invoke the sole producer with enumerated absolute paths and no Sonar input."""

    producer = plan.repository_root / "build" / "coverage.sh"
    command = [
        "uv",
        "run",
        "--project",
        str(plan.repository_root),
        "--isolated",
        "--locked",
        "--extra",
        "dev",
        "--with",
        f"coverage=={COVERAGE_PY_VERSION}",
        "--",
        _coverage_executable("bash") or "bash",
        str(producer),
        "--repo-root",
        str(plan.repository_root),
        "--python-data",
        str(plan.python_data),
        "--python-report",
        str(plan.python_report),
    ]
    for spec in plan.dotnet_inputs:
        command.extend(
            [
                "--dotnet-project",
                spec.id,
                str(plan.repository_root / spec.project),
                str(_cobertura_output_prefix(spec.raw_cobertura_input)),
                str(plan.repository_root / spec.include_directory)
                if spec.include_directory is not None
                else "-",
            ]
        )
    clean_environment = scrub_sonar_environment(environment)
    run_process(
        command,
        cwd=plan.repository_root,
        environment=clean_environment,
        secrets=sonar_secret_values(environment),
        label="Coverage producer",
    )


def _coverage_failure(code: str, detail: str) -> NoReturn:
    raise RunnerError(f"{code}: {detail}")


def _coverage_environment() -> dict[str, str]:
    return scrub_sonar_environment(process_environment())


def _safe_coverage_source(context: GitContext, filename: Any, language: str) -> str:
    if not isinstance(filename, str) or not filename:
        _coverage_failure("COVERAGE_SOURCE_MAPPING_INVALID", "Cobertura class filename is absent")
    normalized = filename.replace("\\", "/")
    if (
        "://" in normalized
        or normalized.startswith("file:")
        or normalized.startswith("/")
        or WINDOWS_ABSOLUTE_PATH_RE.match(normalized)
    ):
        _coverage_failure(
            "COVERAGE_SOURCE_MAPPING_INVALID", "Cobertura source path is absolute or a URI"
        )
    parts = normalized.split("/")
    if any(part in {"", ".", "..", "bin", "obj"} for part in parts):
        _coverage_failure("COVERAGE_SOURCE_MAPPING_INVALID", "Cobertura source path is unsafe")
    candidate = context.repository_root.joinpath(*parts)
    try:
        candidate.relative_to(context.repository_root)
        metadata = _scanner_tree_metadata(candidate)
    except (ValueError, RunnerError) as error:
        _coverage_failure("COVERAGE_SOURCE_MAPPING_INVALID", str(error))
        raise AssertionError("unreachable") from error
    attributes = int(getattr(metadata, "st_file_attributes", 0) or 0)
    if (
        not stat.S_ISREG(getattr(metadata, "st_mode", 0))
        or attributes & 0x0400
        or not is_tracked(context.repository_root, _coverage_environment(), candidate)
    ):
        _coverage_failure(
            "COVERAGE_SOURCE_MAPPING_INVALID",
            "Cobertura source is missing, reparse-backed, nonregular, or untracked",
        )
    relative = candidate.relative_to(context.repository_root).as_posix()
    if language == "python":
        if not relative.startswith("src/netcoredbg_mcp/") or not relative.endswith(".py"):
            _coverage_failure(
                "COVERAGE_SOURCE_MAPPING_INVALID",
                "Python coverage source is outside src/netcoredbg_mcp",
            )
    elif language == "dotnet":
        lowered = [part.casefold() for part in relative.split("/")]
        if (
            not relative.endswith(".cs")
            or any(part in {"test", "tests", "fixture", "fixtures"} for part in lowered)
            or any(".tests" in part for part in lowered)
        ):
            _coverage_failure(
                "COVERAGE_SOURCE_MAPPING_INVALID", "Dotnet coverage source is not production source"
            )
    else:
        _coverage_failure("COVERAGE_SOURCE_MAPPING_INVALID", "coverage language is unknown")
    return relative


def _positive_int(value: Any, code: str, detail: str, *, allow_zero: bool = False) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as error:
        _coverage_failure(code, detail)
        raise AssertionError("unreachable") from error
    if number < 0 or (number == 0 and not allow_zero):
        _coverage_failure(code, detail)
    return number


def _condition_totals(line: ElementTree.Element) -> tuple[int, int]:
    if str(line.attrib.get("branch", "")).casefold() != "true":
        return 0, 0
    coverage = line.attrib.get("condition-coverage")
    match = re.search(r"\((\d+)\s*/\s*(\d+)\)", coverage or "")
    if match is None:
        _coverage_failure("COVERAGE_REPORT_INVALID", "branch line lacks condition coverage")
    covered, valid = int(match.group(1)), int(match.group(2))
    if valid <= 0 or covered < 0 or covered > valid:
        _coverage_failure("COVERAGE_REPORT_INVALID", "branch condition denominator is invalid")
    return covered, valid


def _parse_cobertura(
    context: GitContext,
    report: Path,
    language: str,
    *,
    require_branches: bool,
) -> dict[str, Any]:
    if not report.exists():
        _coverage_failure("COVERAGE_REPORT_MISSING", "Cobertura report is missing")
    try:
        metadata = _scanner_tree_metadata(report)
        if not stat.S_ISREG(getattr(metadata, "st_mode", 0)):
            _coverage_failure("COVERAGE_REPORT_INVALID", "Cobertura report is not a regular file")
        raw = report.read_bytes()
    except FileNotFoundError as error:
        _coverage_failure("COVERAGE_REPORT_MISSING", "Cobertura report is missing")
        raise AssertionError("unreachable") from error
    except (OSError, RunnerError) as error:
        _coverage_failure("COVERAGE_REPORT_INVALID", str(error))
        raise AssertionError("unreachable") from error
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError as error:
        _coverage_failure("COVERAGE_REPORT_INVALID", "Cobertura report is malformed XML")
        raise AssertionError("unreachable") from error
    if root.tag.rsplit("}", 1)[-1] != "coverage":
        _coverage_failure("COVERAGE_REPORT_INVALID", "Cobertura root must be coverage")
    lines_valid = _positive_int(
        root.attrib.get("lines-valid"), "COVERAGE_DENOMINATOR_INVALID", "line denominator is absent"
    )
    lines_covered = _positive_int(
        root.attrib.get("lines-covered"),
        "COVERAGE_REPORT_INVALID",
        "covered line count is invalid",
        allow_zero=True,
    )
    branches_valid = _positive_int(
        root.attrib.get("branches-valid"),
        "COVERAGE_DENOMINATOR_INVALID",
        "branch denominator is absent",
        allow_zero=not require_branches,
    )
    branches_covered = _positive_int(
        root.attrib.get("branches-covered"),
        "COVERAGE_REPORT_INVALID",
        "covered branch count is invalid",
        allow_zero=True,
    )
    if lines_covered > lines_valid or branches_covered > branches_valid:
        _coverage_failure("COVERAGE_REPORT_INVALID", "Cobertura covered counts exceed denominators")
    source_paths: list[str] = []
    facts: list[dict[str, Any]] = []
    for class_element in root.iter():
        if class_element.tag.rsplit("}", 1)[-1] != "class":
            continue
        source_path = _safe_coverage_source(context, class_element.attrib.get("filename"), language)
        if source_path in source_paths:
            _coverage_failure(
                "COVERAGE_SOURCE_MAPPING_INVALID", "Cobertura source maps more than once"
            )
        source_paths.append(source_path)
        line_facts: list[dict[str, int]] = []
        for line in class_element.iter():
            if line.tag.rsplit("}", 1)[-1] != "line":
                continue
            number = _positive_int(
                line.attrib.get("number"), "COVERAGE_REPORT_INVALID", "line number is invalid"
            )
            hits = _positive_int(
                line.attrib.get("hits"),
                "COVERAGE_REPORT_INVALID",
                "line hits are invalid",
                allow_zero=True,
            )
            branch_covered, branch_valid = _condition_totals(line)
            line_facts.append(
                {
                    "number": number,
                    "hits": hits,
                    "branches_covered": branch_covered,
                    "branches_valid": branch_valid,
                }
            )
        facts.append({"source_path": source_path, "lines": line_facts})
    if not source_paths:
        _coverage_failure(
            "COVERAGE_SOURCE_MAPPING_INVALID", "Cobertura report has no mapped source"
        )
    source_paths.sort()
    return {
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
        "xml_root": "coverage",
        "lines_valid": lines_valid,
        "lines_covered": lines_covered,
        "branches_valid": branches_valid,
        "branches_covered": branches_covered,
        "source_paths": source_paths,
        "source_set_sha256": _sha256_json(source_paths),
        "facts": facts,
    }


def _final_report_evidence(
    plan: CoveragePlan, report_id: str, parsed: Mapping[str, Any]
) -> dict[str, Any]:
    report = plan.python_report if report_id == "python" else plan.dotnet_report
    return {
        "id": report_id,
        "language": report_id,
        "format": "cobertura",
        "relative_path": _coverage_relative(plan, report),
        **{
            key: parsed[key]
            for key in (
                "sha256",
                "bytes",
                "xml_root",
                "lines_valid",
                "lines_covered",
                "branches_valid",
                "branches_covered",
                "source_paths",
                "source_set_sha256",
            )
        },
    }


def validate_python_cobertura(context: GitContext, report: Path) -> dict[str, Any]:
    return _parse_cobertura(context, report, "python", require_branches=True)


def validate_dotnet_cobertura_input(
    context: GitContext, spec: CoverageProjectSpec | Any, report: Path
) -> dict[str, Any]:
    parsed = _parse_cobertura(context, report, "dotnet", require_branches=False)
    if getattr(spec, "id", None) == "stateless" and not any(
        path.startswith("host/NetCoreDbg.Mcp.Stateless/") for path in parsed["source_paths"]
    ):
        _coverage_failure(
            "COVERAGE_SOURCE_MAPPING_INVALID",
            "Stateless coverage must map production Stateless source",
        )
    return parsed


def _dotnet_input_evidence(
    plan: CoveragePlan, spec: CoverageProjectSpec, parsed: Mapping[str, Any]
) -> dict[str, Any]:
    return {
        "id": spec.id,
        "project": spec.project.as_posix(),
        "relative_path": _coverage_relative(plan, spec.raw_cobertura_input),
        **{
            key: parsed[key]
            for key in (
                "sha256",
                "bytes",
                "lines_valid",
                "lines_covered",
                "branches_valid",
                "branches_covered",
                "source_paths",
                "source_set_sha256",
            )
        },
        "facts": parsed["facts"],
    }


def validate_dotnet_cobertura_inputs(
    context: GitContext, plan: CoveragePlan
) -> list[dict[str, Any]]:
    inputs: list[dict[str, Any]] = []
    total_branches = 0
    for spec in plan.dotnet_inputs:
        parsed = validate_dotnet_cobertura_input(context, spec, spec.raw_cobertura_input)
        inputs.append(_dotnet_input_evidence(plan, spec, parsed))
        total_branches += int(parsed["branches_valid"])
    if total_branches <= 0:
        _coverage_failure(
            "COVERAGE_DENOMINATOR_INVALID", "private .NET inputs have no branch denominator"
        )
    return inputs


def _receipt_dotnet_input(input_evidence: Mapping[str, Any]) -> dict[str, Any]:
    return {
        key: input_evidence[key]
        for key in (
            "id",
            "project",
            "relative_path",
            "sha256",
            "bytes",
            "lines_valid",
            "lines_covered",
            "branches_valid",
            "branches_covered",
            "source_paths",
            "source_set_sha256",
        )
    }


def normalize_dotnet_cobertura(
    plan: CoveragePlan, inputs: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    if [item.get("id") for item in inputs] != [spec.id for spec in plan.dotnet_inputs]:
        _coverage_failure(
            "COVERAGE_DOTNET_NORMALIZATION_FAILED", "input order does not match marker order"
        )
    line_hits: dict[tuple[str, int], int] = {}
    branch_hits: dict[tuple[str, int, int], bool] = {}
    source_union: set[str] = set()
    for input_evidence in inputs:
        facts = input_evidence.get("facts")
        if not isinstance(facts, list):
            _coverage_failure(
                "COVERAGE_DOTNET_NORMALIZATION_FAILED", "validated input facts are absent"
            )
        for source in facts:
            if not isinstance(source, Mapping) or not isinstance(source.get("source_path"), str):
                _coverage_failure(
                    "COVERAGE_DOTNET_NORMALIZATION_FAILED", "validated input facts are malformed"
                )
            source_path = source["source_path"]
            source_union.add(source_path)
            lines = source.get("lines")
            if not isinstance(lines, list):
                _coverage_failure(
                    "COVERAGE_DOTNET_NORMALIZATION_FAILED", "validated line facts are absent"
                )
            for line in lines:
                if not isinstance(line, Mapping):
                    _coverage_failure(
                        "COVERAGE_DOTNET_NORMALIZATION_FAILED", "validated line fact is malformed"
                    )
                number = int(line.get("number", 0))
                hits = int(line.get("hits", 0))
                branch_valid = int(line.get("branches_valid", 0))
                branch_covered = int(line.get("branches_covered", 0))
                if (
                    number <= 0
                    or hits < 0
                    or branch_valid < 0
                    or branch_covered < 0
                    or branch_covered > branch_valid
                ):
                    _coverage_failure(
                        "COVERAGE_DOTNET_NORMALIZATION_FAILED", "validated line fact is invalid"
                    )
                key = (source_path, number)
                line_hits[key] = max(line_hits.get(key, 0), hits)
                for ordinal in range(branch_valid):
                    branch_key = (source_path, number, ordinal)
                    branch_hits[branch_key] = (
                        branch_hits.get(branch_key, False) or ordinal < branch_covered
                    )
    if not line_hits or not branch_hits:
        _coverage_failure(
            "COVERAGE_DOTNET_NORMALIZATION_FAILED", "normalized report has a zero denominator"
        )
    root = ElementTree.Element(
        "coverage",
        {
            "lines-valid": str(len(line_hits)),
            "lines-covered": str(sum(hits > 0 for hits in line_hits.values())),
            "branches-valid": str(len(branch_hits)),
            "branches-covered": str(sum(branch_hits.values())),
        },
    )
    sources = ElementTree.SubElement(root, "sources")
    ElementTree.SubElement(sources, "source").text = "."
    packages = ElementTree.SubElement(root, "packages")
    package = ElementTree.SubElement(packages, "package", {"name": "normalized"})
    classes = ElementTree.SubElement(package, "classes")
    for source_path in sorted(source_union):
        class_element = ElementTree.SubElement(
            classes, "class", {"name": source_path.replace("/", "."), "filename": source_path}
        )
        ElementTree.SubElement(class_element, "methods")
        lines_element = ElementTree.SubElement(class_element, "lines")
        for source, number in sorted(key for key in line_hits if key[0] == source_path):
            branch_values = [
                branch_hits[(source, number, ordinal)]
                for ordinal in range(
                    sum(1 for item in branch_hits if item[0] == source and item[1] == number)
                )
            ]
            attributes = {"number": str(number), "hits": str(line_hits[(source, number)])}
            if branch_values:
                covered = sum(branch_values)
                condition_coverage = (
                    f"{round(covered * 100 / len(branch_values))}% ({covered}/{len(branch_values)})"
                )
                attributes.update({"branch": "true", "condition-coverage": condition_coverage})
            ElementTree.SubElement(lines_element, "line", attributes)
    try:
        plan.dotnet_report.parent.mkdir(parents=True, exist_ok=True)
        ElementTree.ElementTree(root).write(
            plan.dotnet_report, encoding="utf-8", xml_declaration=True
        )
    except OSError as error:
        _coverage_failure("COVERAGE_DOTNET_NORMALIZATION_FAILED", "cannot write normalized report")
        raise AssertionError("unreachable") from error
    return {
        "algorithm": COBERTURA_NORMALIZER,
        "input_set_sha256": _sha256_json([_receipt_dotnet_input(item) for item in inputs]),
        "output_report_id": "dotnet",
        "source_union_complete": True,
    }


def validate_final_dotnet_cobertura(
    context: GitContext,
    plan: CoveragePlan,
    inputs: Sequence[Mapping[str, Any]],
    normalization: Mapping[str, Any],
) -> dict[str, Any]:
    try:
        parsed = _parse_cobertura(context, plan.dotnet_report, "dotnet", require_branches=True)
    except RunnerError as error:
        _coverage_failure("COVERAGE_DOTNET_NORMALIZATION_FAILED", str(error))
        raise AssertionError("unreachable") from error
    expected_sources = sorted(
        {source for input_evidence in inputs for source in input_evidence.get("source_paths", [])}
    )
    expected_input_hash = _sha256_json([_receipt_dotnet_input(item) for item in inputs])
    if (
        normalization.get("algorithm") != COBERTURA_NORMALIZER
        or normalization.get("output_report_id") != "dotnet"
        or normalization.get("source_union_complete") is not True
        or normalization.get("input_set_sha256") != expected_input_hash
        or parsed.get("source_paths") != expected_sources
    ):
        _coverage_failure(
            "COVERAGE_DOTNET_NORMALIZATION_FAILED", "normalized source union is not exact"
        )
    return parsed


def capture_stateless_binary_hashes(plan: CoveragePlan) -> dict[str, str]:
    directory = plan.repository_root / "host/NetCoreDbg.Mcp.Stateless/bin/Debug/net8.0"
    dll = directory / "NetCoreDbg.Mcp.Stateless.dll"
    pdb = directory / "NetCoreDbg.Mcp.Stateless.pdb"
    try:
        return {
            "dll_sha256": _sha256_bytes(dll.read_bytes()),
            "pdb_sha256": _sha256_bytes(pdb.read_bytes()),
        }
    except OSError as error:
        _coverage_failure(
            "COVERAGE_INSTRUMENTATION_NOT_RESTORED", "Stateless production binary is unavailable"
        )
        raise AssertionError("unreachable") from error


def validate_stateless_restoration(
    plan: CoveragePlan, expected: Mapping[str, Any]
) -> dict[str, Any]:
    observed = capture_stateless_binary_hashes(plan)
    if (
        not _is_sha256(expected.get("dll_sha256"))
        or not _is_sha256(expected.get("pdb_sha256"))
        or observed != {"dll_sha256": expected["dll_sha256"], "pdb_sha256": expected["pdb_sha256"]}
    ):
        _coverage_failure(
            "COVERAGE_INSTRUMENTATION_NOT_RESTORED",
            "Stateless DLL/PDB bytes changed during coverage",
        )
    return {**observed, "restored": True}


def validate_coverage_reports(
    context: GitContext,
    plan: CoveragePlan,
    claim: CoverageRunClaim | None = None,
    stateless_before: Mapping[str, Any] | None = None,
    *,
    dotnet_inputs: Sequence[Mapping[str, Any]] | None = None,
    normalization: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    if claim is not None:
        try:
            validate_coverage_marker(
                plan, _load_json_object(claim.marker.read_bytes(), "coverage marker")
            )
        except (OSError, RunnerError) as error:
            _coverage_failure("COVERAGE_MARKER_INVALID", str(error))
    python = validate_python_cobertura(context, plan.python_report)
    inputs = (
        list(dotnet_inputs)
        if dotnet_inputs is not None
        else validate_dotnet_cobertura_inputs(context, plan)
    )
    normalized = (
        dict(normalization)
        if normalization is not None
        else normalize_dotnet_cobertura(plan, inputs)
    )
    dotnet = validate_final_dotnet_cobertura(context, plan, inputs, normalized)
    stateless = (
        validate_stateless_restoration(plan, stateless_before)
        if stateless_before is not None
        else {**capture_stateless_binary_hashes(plan), "restored": True}
    )
    marker_reference = {
        "relative_path": _coverage_relative(plan, plan.marker),
        "sha256": _sha256_bytes(plan.marker.read_bytes()),
        "bytes": len(plan.marker.read_bytes()),
    }
    return {
        "run_id": plan.run_id,
        "marker": marker_reference,
        "final_reports": [
            _final_report_evidence(plan, "python", python),
            _final_report_evidence(plan, "dotnet", dotnet),
        ],
        "dotnet_producers": [_receipt_dotnet_input(item) for item in inputs],
        "normalization": normalized,
        "stateless_host_binary": stateless,
    }


def cleanup_coverage_run(plan: CoveragePlan, producer_terminal: bool) -> dict[str, Any]:
    root_relative = _coverage_relative(plan, plan.root)
    cleanup = {
        "claimed_root": root_relative,
        "producer_terminal": producer_terminal,
        "removed_paths": [],
        "parent_removed_if_empty": False,
        "status": "OK",
        "failure": None,
    }
    if not producer_terminal:
        cleanup["status"] = "FAILED"
        cleanup["failure"] = {
            "code": "COVERAGE_CLEANUP_FAILED",
            "message": "producer is not terminal",
        }
        return cleanup
    try:
        if plan.root.exists():
            metadata = _scanner_tree_metadata(plan.root)
            if not stat.S_ISDIR(getattr(metadata, "st_mode", 0)):
                raise OSError("claimed root is not a directory")
            shutil.rmtree(plan.root)
            cleanup["removed_paths"] = [root_relative]
        parent = plan.root.parent
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
            cleanup["parent_removed_if_empty"] = True
    except (OSError, RunnerError) as error:
        cleanup["status"] = "FAILED"
        cleanup["failure"] = {
            "code": "COVERAGE_CLEANUP_FAILED",
            "message": error.__class__.__name__,
        }
    return cleanup


def project_key_from_xml(path: Path) -> str:
    try:
        root = ElementTree.parse(path).getroot()
    except (OSError, ElementTree.ParseError) as error:
        raise RunnerError("SonarQube.Analysis.xml is unreadable.") from error
    properties = [
        (element.attrib.get("Name"), element.text.strip() if element.text else None)
        for element in root.iter()
        if element.tag.rsplit("}", 1)[-1] == "Property"
    ]
    keys = [value for name, value in properties if name == "sonar.projectKey" and value]
    if keys != [PROJECT_KEY]:
        raise RunnerError("SonarQube.Analysis.xml does not contain the fixed project key.")
    if any(
        isinstance(name, str)
        and ("coverage.report" in name.casefold() or "cobertura.reports" in name.casefold())
        for name, _ in properties
    ):
        raise RunnerError(
            "COVERAGE_SCANNER_PROPERTIES_INVALID: static XML coverage properties are forbidden."
        )
    return keys[0]


def discover_scanner(override: str | None) -> list[str]:
    candidates = (
        [override]
        if override
        else [
            "dotnet-sonarscanner",
            "SonarScanner.MSBuild.exe",
            "SonarScanner.MSBuild",
        ]
    )
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
    token: str,
    *,
    coverage_properties: Sequence[str] = (),
) -> list[str]:
    properties = tuple(coverage_properties)
    if properties and len(properties) != 2:
        raise RunnerError(
            "COVERAGE_SCANNER_PROPERTIES_INVALID: exactly two final report properties are required."
        )
    return [
        *scanner,
        "begin",
        f"/k:{PROJECT_KEY}",
        f"/s:{analysis_xml}",
        f"/d:sonar.host.url={host}",
        f"/d:sonar.scm.revision={head}",
        f"/d:sonar.token={token}",
        *properties,
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
    return (
        solution,
        projects,
        sorted(
            discovered_projects - set(solution_projects), key=lambda path: path.as_posix().lower()
        ),
    )


def scanner_metadata(repository_root: Path, expected_head: str) -> dict[str, Any]:
    metadata_root = repository_root / ".sonarqube"
    if not metadata_root.is_dir():
        raise RunnerError("SonarScanner did not create metadata.")
    found: dict[str, list[tuple[str, str]]] = {"sonar.projectKey": [], "sonar.scm.revision": []}
    for path in iter_scanner_tree(metadata_root, "*"):
        if (
            not path.is_file()
            or path.is_symlink()
            or path.suffix.lower() not in {".xml", ".properties", ".txt"}
        ):
            continue
        relative = str(path.relative_to(repository_root)).replace("\\", "/")
        try:
            if path.suffix.lower() == ".xml":
                root = ElementTree.parse(path).getroot()
                for element in root.iter():
                    name = (
                        element.attrib.get("Name")
                        or element.attrib.get("name")
                        or element.attrib.get("key")
                    )
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
    observed_revisions = {value for _, value in found["sonar.scm.revision"]}
    if observed_project_keys != {PROJECT_KEY} or observed_revisions != {expected_head}:
        raise RunnerError(
            "Observed SonarScanner metadata does not bind the fixed project and exact HEAD."
        )
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
        raise RunnerError(
            "SonarScanner report-task project key does not match the fixed project key."
        )
    if not values.get("ceTaskId"):
        raise RunnerError("SonarScanner report-task lacks its Compute Engine task ID.")
    server_origin_matches_configured = (
        credential_free_host(values.get("serverUrl", "")) == expected_host
    )
    if not server_origin_matches_configured:
        raise RunnerError("SonarScanner report-task server origin does not match SONAR_HOST_URL.")
    dashboard_url = values.get("dashboardUrl", "")
    dashboard_url_present = bool(dashboard_url)
    if not dashboard_url_present:
        raise RunnerError("SonarScanner report-task lacks its dashboard URL.")
    if response_origin(dashboard_url) != expected_host:
        raise RunnerError(
            "SonarScanner report-task dashboard origin does not match SONAR_HOST_URL."
        )
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
                raise RunnerError(
                    "Refusing an API response whose origin differs from SONAR_HOST_URL."
                )
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
            raise RunnerError(
                "Submitted Compute Engine task does not prove the fixed project component key."
            )
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
            raise RunnerError(
                "Submitted Compute Engine task did not complete before the 10-minute deadline."
            )
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


def indexed_api_json(
    host: str, endpoint: str, parameters: Mapping[str, str], token: str
) -> dict[str, Any]:
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
        page_index, page_size, page_total = (
            paging.get("pageIndex"),
            paging.get("pageSize"),
            paging.get("total"),
        )
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
    if (
        total is None
        or len(records) != total
        or len({record["key"] for record in records}) != len(records)
    ):
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
        (
            "key",
            "rule",
            "severity",
            "status",
            "issueStatus",
            "resolution",
            "type",
            "component",
            "line",
            "impacts",
        ),
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
            "key",
            "rule",
            "severity",
            "status",
            "issueStatus",
            "resolution",
            "type",
            "component",
            "project",
            "line",
            "message",
            "impacts",
            "creationDate",
            "updateDate",
            "tags",
            "textRange",
            "flows",
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
                raise RunnerError(
                    "Another exact-head SonarQube scan holds the project lock."
                ) from error
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
    return (
        context.coordination_root
        / ".agent"
        / "e"
        / "sonarqube"
        / PROJECT_KEY
        / context.head
        / f"{role}.json"
    )


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
    required = (
        "endpoint",
        "query",
        "total",
        "pages",
        "pagination_complete",
        "result_empty",
        "records",
    )
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
        if (
            not isinstance(record, dict)
            or not isinstance(record.get("key"), str)
            or not record["key"]
        ):
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


def validate_issue_dispositions(
    before: Mapping[str, Any], after: Mapping[str, Any], dispositions: Any
) -> None:
    if (
        not isinstance(dispositions, dict)
        or not isinstance(dispositions.get("items"), list)
        or type(dispositions.get("blocking_count")) is not int
    ):
        raise RunnerError("PASS receipt lacks issue-disposition evidence.")
    before_by_key = {record["key"]: record for record in before["records"]}
    after_by_key = {record["key"]: record for record in after["records"]}
    expected_keys = set(before_by_key) | set(after_by_key)
    item_by_key: dict[str, Mapping[str, Any]] = {}
    for item in dispositions["items"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("key"), str)
            or not item["key"]
            or item["key"] in item_by_key
        ):
            raise RunnerError("PASS receipt issue dispositions have invalid keys.")
        item_by_key[item["key"]] = item
    if set(item_by_key) != expected_keys:
        raise RunnerError(
            "PASS receipt issue dispositions do not cover the exact inventory-key union."
        )
    expected_blocking = 0
    for key in expected_keys:
        current = after_by_key.get(key)
        expected_disposition = (
            "FIXED_IN_CURRENT_HEAD"
            if current is None
            or (
                current.get("issueStatus") == "FIXED"
                and current.get("resolution") in {None, "FIXED"}
            )
            else "BLOCKING_DISPOSITION"
        )
        if item_by_key[key].get("disposition") != expected_disposition:
            raise RunnerError(
                "PASS receipt issue disposition does not match the observed current issue."
            )
        expected_blocking += expected_disposition == "BLOCKING_DISPOSITION"
    if dispositions["blocking_count"] != expected_blocking:
        raise RunnerError("PASS receipt issue blocking count does not match observed dispositions.")


def validate_hotspot_dispositions(inventory: Mapping[str, Any], dispositions: Any) -> None:
    if (
        not isinstance(dispositions, dict)
        or not isinstance(dispositions.get("items"), list)
        or type(dispositions.get("blocking_count")) is not int
    ):
        raise RunnerError("PASS receipt lacks hotspot-disposition evidence.")
    expected_keys = {record["key"] for record in inventory["records"]}
    items = dispositions["items"]
    keys = [item.get("key") for item in items if isinstance(item, dict)]
    if (
        len(keys) != len(items)
        or any(not isinstance(key, str) or not key for key in keys)
        or len(set(keys)) != len(keys)
        or set(keys) != expected_keys
    ):
        raise RunnerError(
            "PASS receipt hotspot dispositions do not cover the exact inventory keys."
        )
    if any(item.get("disposition") != "BLOCKING_HOTSPOT" for item in items) or dispositions[
        "blocking_count"
    ] != len(expected_keys):
        raise RunnerError("PASS receipt hotspot blocking count does not match observed hotspots.")


def validate_pass_receipt(receipt: Mapping[str, Any]) -> None:
    required = (
        "run_id",
        "role",
        "project_key",
        "analysis_xml_project_key",
        "captured_head",
        "completed_at",
        "worktree",
        "cleanliness",
        "scanner_metadata",
        "task_report",
        "compute_engine",
        "analysis_current_before_issues",
        "analysis_current_after_issues",
        "analysis_current_final",
        "quality_gate",
        "pre_scan_issues",
        "post_scan_issues",
        "new_code_issues",
        "issue_dispositions",
        "hotspots",
        "hotspot_dispositions",
        "cleanup",
        "post_scan_head",
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
        or not all(
            isinstance(worktree.get(key), str) and worktree[key]
            for key in ("repository_root", "git_dir", "common_dir", "coordination_root")
        )
    ):
        raise RunnerError("PASS receipt lacks detached linked-worktree evidence.")
    if (
        not isinstance(cleanliness, dict)
        or cleanliness.get("pre", {}).get("status") != "clean"
        or cleanliness.get("post", {}).get("status") != "clean"
    ):
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
    if (
        not isinstance(scanner, dict)
        or not scanner.get("observed")
        or scanner.get("project_key") != PROJECT_KEY
        or scanner.get("sonar_scm_revision") != receipt.get("captured_head")
    ):
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
    if (
        not isinstance(quality_gate, dict)
        or quality_gate.get("status") != "OK"
        or quality_gate.get("analysis_id") != compute_engine.get("analysis_id")
    ):
        raise RunnerError("PASS receipt has an invalid analysis-bound quality gate.")
    for key in (
        "analysis_current_before_issues",
        "analysis_current_after_issues",
        "analysis_current_final",
    ):
        binding = receipt[key]
        if (
            not isinstance(binding, dict)
            or not binding.get("observed")
            or not binding.get("current")
            or binding.get("query") != {"project": PROJECT_KEY, "p": "1", "ps": "1"}
            or binding.get("analysis_id") != compute_engine.get("analysis_id")
            or binding.get("revision") != receipt.get("captured_head")
        ):
            raise RunnerError(
                "PASS receipt lacks current fixed-project exact-head analysis binding evidence."
            )
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
    if (
        receipt["issue_dispositions"]["blocking_count"] != 0
        or receipt["hotspot_dispositions"]["blocking_count"] != 0
    ):
        raise RunnerError("PASS receipt has unremediated issue or hotspot evidence.")
    if receipt.get("post_scan_head") != receipt.get("captured_head"):
        raise RunnerError("PASS receipt has a post-scan HEAD mismatch.")


def validate_coverage_analysis_evidence(
    identity: Mapping[str, Any], observations: Mapping[str, Any]
) -> dict[str, Any]:
    """Require one canonical analysis identity and positive two-language import proof."""

    expected_identity = {"captured_head", "project_key", "analysis_id"}
    if (
        set(identity) != expected_identity
        or not isinstance(identity.get("captured_head"), str)
        or not SHA_RE.fullmatch(identity["captured_head"])
        or identity.get("project_key") != PROJECT_KEY
        or not isinstance(identity.get("analysis_id"), str)
        or not identity["analysis_id"]
    ):
        _coverage_failure("COVERAGE_ANALYSIS_MISMATCH", "canonical analysis identity is invalid")
    for field in (
        "submitted",
        "current_before_measures",
        "current_after_measures",
        "current_final",
    ):
        if observations.get(field) != identity:
            _coverage_failure(
                "COVERAGE_ANALYSIS_MISMATCH", f"{field} does not match canonical identity"
            )
    aggregate = observations.get("aggregate")
    condition = observations.get("new_coverage_condition")
    if (
        not isinstance(aggregate, Mapping)
        or type(aggregate.get("coverage")) not in {int, float}
        or not 0 < float(aggregate["coverage"]) <= 100
        or type(aggregate.get("lines_to_cover")) is not int
        or aggregate["lines_to_cover"] <= 0
        or type(aggregate.get("new_coverage")) not in {int, float}
        or type(aggregate.get("new_lines_to_cover")) is not int
        or aggregate["new_lines_to_cover"] <= 0
        or not isinstance(condition, Mapping)
        or condition.get("status") not in {"OK", "ERROR"}
        or condition.get("threshold") != 80
        or type(condition.get("actual_value")) not in {int, float}
    ):
        _coverage_failure("COVERAGE_MEASURES_INVALID", "aggregate coverage measures are invalid")
    for language in ("python", "dotnet"):
        component = observations.get(f"{language}_components")
        if (
            not isinstance(component, Mapping)
            or component.get("complete") is not True
            or type(component.get("page_count")) is not int
            or component["page_count"] <= 0
            or type(component.get("mapped_path_count")) is not int
            or component["mapped_path_count"] <= 0
            or type(component.get("lines_to_cover")) is not int
            or component["lines_to_cover"] <= 0
            or type(component.get("covered_lines")) is not int
            or component["covered_lines"] <= 0
            or type(component.get("branch_measure_path_count")) is not int
            or component["branch_measure_path_count"] <= 0
        ):
            _coverage_failure(
                "COVERAGE_IMPORT_UNPROVEN", f"{language} component import is incomplete"
            )
    return {
        "observations": {
            "submitted": True,
            "current_before_measures": True,
            "current_after_measures": True,
            "current_final": True,
        },
        "aggregate": dict(aggregate),
        "new_coverage_condition": dict(condition),
        "python_components": dict(observations["python_components"]),
        "dotnet_components": dict(observations["dotnet_components"]),
    }


def _v3_fail(detail: str) -> NoReturn:
    raise RunnerError(f"EXACT_HEAD_RECEIPT_V3_INVALID: {detail}")


def _v3_artifact(value: Any, expected_path: str | None = None) -> Mapping[str, Any]:
    if (
        not isinstance(value, Mapping)
        or set(value) != {"relative_path", "sha256", "bytes"}
        or not _is_relative_path(value.get("relative_path"))
        or not _is_sha256(value.get("sha256"))
        or type(value.get("bytes")) is not int
        or value["bytes"] <= 0
        or (expected_path is not None and value.get("relative_path") != expected_path)
    ):
        _v3_fail("artifact reference is invalid or unbound")
    return value


def _v3_source_set(value: Any) -> None:
    if (
        not isinstance(value, list)
        or not value
        or any(not _is_relative_path(path) for path in value)
        or len(set(value)) != len(value)
    ):
        _v3_fail("coverage source set is incomplete")


def _v3_final_report(value: Any, run_id: str, report_id: str) -> Mapping[str, Any]:
    expected_path = f".tmp/sonarqube-coverage/{run_id}/{report_id}/coverage.xml"
    required = {
        "id",
        "language",
        "format",
        "relative_path",
        "sha256",
        "bytes",
        "xml_root",
        "lines_valid",
        "lines_covered",
        "branches_valid",
        "branches_covered",
        "source_paths",
        "source_set_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or value.get("id") != report_id
        or value.get("language") != report_id
        or value.get("format") != "cobertura"
        or value.get("relative_path") != expected_path
        or not _is_sha256(value.get("sha256"))
        or type(value.get("bytes")) is not int
        or value["bytes"] <= 0
        or value.get("xml_root") != "coverage"
        or type(value.get("lines_valid")) is not int
        or value["lines_valid"] <= 0
        or type(value.get("lines_covered")) is not int
        or value["lines_covered"] < 0
        or type(value.get("branches_valid")) is not int
        or value["branches_valid"] <= 0
        or type(value.get("branches_covered")) is not int
        or value["branches_covered"] < 0
        or not _is_sha256(value.get("source_set_sha256"))
    ):
        _v3_fail("final coverage report linkage is invalid")
    _v3_source_set(value.get("source_paths"))
    return value


def _v3_dotnet_input(
    value: Any, run_id: str, expected: tuple[str, str, str | None]
) -> Mapping[str, Any]:
    identifier, project, _ = expected
    expected_path = (
        f".tmp/sonarqube-coverage/{run_id}/dotnet/inputs/{identifier}/coverage.cobertura.xml"
    )
    required = {
        "id",
        "project",
        "relative_path",
        "sha256",
        "bytes",
        "lines_valid",
        "lines_covered",
        "branches_valid",
        "branches_covered",
        "source_paths",
        "source_set_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or value.get("id") != identifier
        or value.get("project") != project
        or value.get("relative_path") != expected_path
        or not _is_sha256(value.get("sha256"))
        or type(value.get("bytes")) is not int
        or value["bytes"] <= 0
        or any(
            type(value.get(field)) is not int or value[field] < 0
            for field in ("lines_covered", "branches_valid", "branches_covered")
        )
        or type(value.get("lines_valid")) is not int
        or value["lines_valid"] <= 0
        or not _is_sha256(value.get("source_set_sha256"))
    ):
        _v3_fail("private .NET coverage input linkage is invalid")
    _v3_source_set(value.get("source_paths"))
    return value


def _v3_inventory_summary(value: Any) -> None:
    required = {
        "complete",
        "result_empty",
        "page_count",
        "total",
        "record_count",
        "keys_sha256",
        "blocking_key_count",
        "blocking_keys_sha256",
    }
    if (
        not isinstance(value, Mapping)
        or set(value) != required
        or value.get("complete") is not True
        or type(value.get("result_empty")) is not bool
        or any(
            type(value.get(field)) is not int or value[field] < 0
            for field in ("page_count", "total", "record_count", "blocking_key_count")
        )
        or value["result_empty"] != (value["total"] == 0)
        or value["record_count"] != value["total"]
        or (value["result_empty"] and value["blocking_key_count"] != 0)
        or not _is_sha256(value.get("keys_sha256"))
        or not _is_sha256(value.get("blocking_keys_sha256"))
    ):
        _v3_fail("global inventory is incomplete or count-only")


def validate_exact_head_receipt_v3(receipt: Mapping[str, Any]) -> None:
    """Pure v3 discriminator shared by every exact-head receipt consumer."""

    required = {
        "schema_version",
        "role",
        "outcome",
        "release_intent",
        "identity",
        "coverage",
        "analysis",
        "global_inventory",
        "release_gate",
        "cleanup",
        "failure",
    }
    if (
        not isinstance(receipt, Mapping)
        or set(receipt) != required
        or receipt.get("schema_version") != EXACT_HEAD_RECEIPT_V3_SCHEMA_VERSION
    ):
        _v3_fail("receipt is not schema version 3")
    role = receipt.get("role")
    outcome = receipt.get("outcome")
    intent = receipt.get("release_intent")
    if role == "diagnostic":
        if (
            outcome not in {"DIAGNOSTIC_COMPLETE", "BLOCKED"}
            or intent != "none"
            or receipt.get("release_gate") is not None
        ):
            _v3_fail("diagnostic role has illegal outcome or release authority")
    elif role in {"candidate", "post-merge"}:
        if outcome not in {"PASS", "BLOCKED"} or intent != "v0.23.11":
            _v3_fail("release role has illegal outcome or intent")
    else:
        _v3_fail("receipt role is invalid")
    identity = receipt.get("identity")
    if (
        not isinstance(identity, Mapping)
        or set(identity) != {"captured_head", "project_key", "analysis_id"}
        or not isinstance(identity.get("captured_head"), str)
        or not SHA_RE.fullmatch(identity["captured_head"])
        or identity.get("project_key") != PROJECT_KEY
        or (
            identity.get("analysis_id") is not None
            and (not isinstance(identity.get("analysis_id"), str) or not identity["analysis_id"])
        )
    ):
        _v3_fail("receipt identity is invalid")
    if outcome == "BLOCKED":
        failure = receipt.get("failure")
        if (
            not isinstance(failure, Mapping)
            or set(failure) != {"code", "stage", "language", "project_id", "safe_message"}
            or not isinstance(failure.get("code"), str)
            or not re.fullmatch(r"(?:COVERAGE|WAVE2)_[A-Z0-9_]+", failure["code"])
            or failure.get("stage")
            not in {
                "PLANNED",
                "TOOLCHAIN_READY",
                "SCANNER_BEGUN",
                "RUN_CLAIMED",
                "PRODUCING",
                "REPORTS_VALIDATED",
                "SCANNER_ENDED",
                "ANALYSIS_BOUND",
                "CLEANED",
                "BLOCKED",
            }
            or failure.get("language") not in {"python", "dotnet", None}
            or failure.get("project_id")
            not in {"python", *[item[0] for item in FIXED_COVERAGE_PROJECTS], None}
            or not isinstance(failure.get("safe_message"), str)
            or not failure["safe_message"]
        ):
            _v3_fail("blocked receipt lacks typed failure")
        return
    if receipt.get("failure") is not None or not isinstance(identity.get("analysis_id"), str):
        _v3_fail("completed receipt has no canonical analysis identity")
    coverage = receipt.get("coverage")
    if not isinstance(coverage, Mapping) or set(coverage) != {
        "run_id",
        "marker",
        "final_reports",
        "dotnet_producers",
        "normalization",
        "stateless_host_binary",
    }:
        _v3_fail("completed receipt lacks coverage linkage")
    try:
        run_id = str(uuid.UUID(str(coverage.get("run_id"))))
    except (ValueError, AttributeError):
        _v3_fail("coverage run ID is invalid")
    _v3_artifact(coverage.get("marker"), f".tmp/sonarqube-coverage/{run_id}/coverage-run.json")
    reports = coverage.get("final_reports")
    producers = coverage.get("dotnet_producers")
    if (
        not isinstance(reports, list)
        or len(reports) != 2
        or not isinstance(producers, list)
        or len(producers) != 5
    ):
        _v3_fail("coverage report or private-input cardinality is invalid")
    _v3_final_report(reports[0], run_id, "python")
    _v3_final_report(reports[1], run_id, "dotnet")
    for item, expected in zip(producers, FIXED_COVERAGE_PROJECTS, strict=True):
        _v3_dotnet_input(item, run_id, expected)
    normalization = coverage.get("normalization")
    stateless = coverage.get("stateless_host_binary")
    if (
        not isinstance(normalization, Mapping)
        or set(normalization)
        != {"algorithm", "input_set_sha256", "output_report_id", "source_union_complete"}
        or normalization.get("algorithm") != COBERTURA_NORMALIZER
        or not _is_sha256(normalization.get("input_set_sha256"))
        or normalization.get("output_report_id") != "dotnet"
        or normalization.get("source_union_complete") is not True
        or not isinstance(stateless, Mapping)
        or set(stateless) != {"dll_sha256", "pdb_sha256", "restored"}
        or not _is_sha256(stateless.get("dll_sha256"))
        or not _is_sha256(stateless.get("pdb_sha256"))
        or stateless.get("restored") is not True
    ):
        _v3_fail("coverage normalization or Stateless restoration is invalid")
    analysis = receipt.get("analysis")
    if not isinstance(analysis, Mapping):
        _v3_fail("completed receipt lacks analysis evidence")
    expected_analysis = {
        "observations",
        "aggregate",
        "new_coverage_condition",
        "python_components",
        "dotnet_components",
    }
    if set(analysis) != expected_analysis:
        _v3_fail("analysis evidence has invalid fields")
    observations = analysis["observations"]
    aggregate = analysis["aggregate"]
    condition = analysis["new_coverage_condition"]
    if (
        not isinstance(observations, Mapping)
        or set(observations)
        != {"submitted", "current_before_measures", "current_after_measures", "current_final"}
        or any(value is not True for value in observations.values())
        or not isinstance(aggregate, Mapping)
        or set(aggregate) != {"coverage", "lines_to_cover", "new_coverage", "new_lines_to_cover"}
        or type(aggregate.get("coverage")) not in {int, float}
        or float(aggregate["coverage"]) <= 0
        or type(aggregate.get("lines_to_cover")) is not int
        or aggregate["lines_to_cover"] <= 0
        or type(aggregate.get("new_lines_to_cover")) is not int
        or aggregate["new_lines_to_cover"] <= 0
        or not isinstance(condition, Mapping)
        or set(condition) != {"status", "threshold", "actual_value"}
        or condition.get("status") not in {"OK", "ERROR"}
        or condition.get("threshold") != 80
        or type(condition.get("actual_value")) not in {int, float}
    ):
        _v3_fail("analysis coverage evidence is invalid")
    for component in (analysis["python_components"], analysis["dotnet_components"]):
        if (
            not isinstance(component, Mapping)
            or set(component)
            != {
                "source_set_sha256",
                "page_count",
                "complete",
                "mapped_path_count",
                "lines_to_cover",
                "covered_lines",
                "branch_measure_path_count",
                "mapped_paths_sha256",
            }
            or not _is_sha256(component.get("source_set_sha256"))
            or not _is_sha256(component.get("mapped_paths_sha256"))
            or component.get("complete") is not True
            or any(
                type(component.get(field)) is not int or component[field] <= 0
                for field in (
                    "page_count",
                    "mapped_path_count",
                    "lines_to_cover",
                    "covered_lines",
                    "branch_measure_path_count",
                )
            )
        ):
            _v3_fail("two-language component evidence is incomplete")
    inventory = receipt.get("global_inventory")
    inventory_path = (
        f".agent/e/sonarqube/{PROJECT_KEY}/{identity['captured_head']}/diagnostic/"
        f"{run_id}.inventory.json"
    )
    if (
        not isinstance(inventory, Mapping)
        or set(inventory) != {"artifact", "artifact_schema_version", "issues", "hotspots"}
        or inventory.get("artifact_schema_version") != 1
    ):
        _v3_fail("global inventory linkage is invalid")
    _v3_artifact(inventory.get("artifact"), inventory_path)
    _v3_inventory_summary(inventory.get("issues"))
    _v3_inventory_summary(inventory.get("hotspots"))
    cleanup = receipt.get("cleanup")
    if (
        not isinstance(cleanup, Mapping)
        or set(cleanup)
        != {
            "claimed_root",
            "producer_terminal",
            "removed_paths",
            "parent_removed_if_empty",
            "status",
            "failure",
        }
        or cleanup.get("claimed_root") != f".tmp/sonarqube-coverage/{run_id}"
        or cleanup.get("producer_terminal") is not True
        or cleanup.get("removed_paths") != [cleanup.get("claimed_root")]
        or type(cleanup.get("parent_removed_if_empty")) is not bool
        or cleanup.get("status") != "OK"
        or cleanup.get("failure") is not None
    ):
        _v3_fail("completed receipt cleanup is not successful")
    gate = receipt.get("release_gate")
    if role == "diagnostic":
        if gate is not None:
            _v3_fail("diagnostic completion has release authority")
    elif (
        not isinstance(gate, Mapping)
        or set(gate) != {"quality_gate_status", "blocking_issue_count", "blocking_hotspot_count"}
        or gate.get("quality_gate_status") != "OK"
        or gate.get("blocking_issue_count") != 0
        or gate.get("blocking_hotspot_count") != 0
    ):
        _v3_fail("PASS receipt has a nonzero or invalid release gate")


def _sonar_measure_values(measures: Any) -> dict[str, float]:
    if not isinstance(measures, list):
        _coverage_failure("COVERAGE_MEASURES_INVALID", "Sonar measure list is absent")
    values: dict[str, float] = {}
    for measure in measures:
        if not isinstance(measure, Mapping) or not isinstance(measure.get("metric"), str):
            _coverage_failure("COVERAGE_MEASURES_INVALID", "Sonar measure is malformed")
        raw_value = measure.get("value")
        if raw_value is None and isinstance(measure.get("period"), Mapping):
            raw_value = measure["period"].get("value")
        if not isinstance(raw_value, (str, int, float)):
            _coverage_failure("COVERAGE_MEASURES_INVALID", "Sonar measure value is invalid")
        try:
            values[measure["metric"]] = float(raw_value)
        except (TypeError, ValueError) as error:
            raise RunnerError(
                "COVERAGE_MEASURES_INVALID: Sonar measure value is invalid"
            ) from error
    return values


def _component_coverage_summary(
    host: str,
    token: str,
    expected_paths: Sequence[str],
) -> dict[str, Any]:
    records: dict[str, Mapping[str, Any]] = {}
    page = 1
    page_count = 0
    while True:
        response = api_json(
            host,
            "/api/measures/component_tree",
            {
                "component": PROJECT_KEY,
                "metricKeys": (
                    "lines_to_cover,uncovered_lines,conditions_to_cover,uncovered_conditions"
                ),
                "qualifiers": "FIL",
                "p": str(page),
                "ps": str(PAGE_SIZE),
            },
            token,
        )
        components = response.get("components")
        paging = response.get("paging")
        if not isinstance(components, list) or not isinstance(paging, Mapping):
            _coverage_failure("COVERAGE_IMPORT_UNPROVEN", "component pagination is malformed")
        page_count += 1
        for component in components:
            if isinstance(component, Mapping) and isinstance(component.get("path"), str):
                records[component["path"].replace("\\", "/")] = component
        total = paging.get("total")
        if type(total) is not int or total < 0:
            _coverage_failure("COVERAGE_IMPORT_UNPROVEN", "component total is invalid")
        if len(records) >= total:
            break
        page += 1
        if page > RESULT_CAP:
            _coverage_failure("COVERAGE_IMPORT_UNPROVEN", "component pagination exceeded limit")
    normalized_paths = sorted(set(expected_paths))
    if len(normalized_paths) != len(expected_paths) or any(
        path not in records for path in normalized_paths
    ):
        _coverage_failure("COVERAGE_IMPORT_UNPROVEN", "coverage source is not mapped by Sonar")
    lines_to_cover = 0
    covered_lines = 0
    branch_measure_paths = 0
    for path in normalized_paths:
        values = _sonar_measure_values(records[path].get("measures"))
        lines = int(values.get("lines_to_cover", 0))
        uncovered = int(values.get("uncovered_lines", lines))
        conditions = int(values.get("conditions_to_cover", 0))
        if lines <= 0 or uncovered < 0 or uncovered >= lines:
            _coverage_failure("COVERAGE_IMPORT_UNPROVEN", "mapped component lacks covered lines")
        lines_to_cover += lines
        covered_lines += lines - uncovered
        if conditions > 0:
            branch_measure_paths += 1
    if branch_measure_paths <= 0:
        _coverage_failure("COVERAGE_IMPORT_UNPROVEN", "mapped components lack branch measures")
    path_hash = _sha256_json(normalized_paths)
    return {
        "source_set_sha256": path_hash,
        "page_count": page_count,
        "complete": True,
        "mapped_path_count": len(normalized_paths),
        "lines_to_cover": lines_to_cover,
        "covered_lines": covered_lines,
        "branch_measure_path_count": branch_measure_paths,
        "mapped_paths_sha256": path_hash,
    }


def collect_coverage_analysis_evidence(
    host: str,
    token: str,
    identity: Mapping[str, Any],
    quality_gate: Mapping[str, Any],
    coverage: Mapping[str, Any],
    observations: Mapping[str, Any],
) -> dict[str, Any]:
    aggregate_response = api_json(
        host,
        "/api/measures/component",
        {
            "component": PROJECT_KEY,
            "metricKeys": "coverage,lines_to_cover,new_coverage,new_lines_to_cover",
        },
        token,
    )
    component = aggregate_response.get("component")
    if not isinstance(component, Mapping):
        _coverage_failure("COVERAGE_MEASURES_INVALID", "aggregate component is absent")
    aggregate_values = _sonar_measure_values(component.get("measures"))
    aggregate = {
        "coverage": aggregate_values.get("coverage"),
        "lines_to_cover": int(aggregate_values.get("lines_to_cover", 0)),
        "new_coverage": aggregate_values.get("new_coverage"),
        "new_lines_to_cover": int(aggregate_values.get("new_lines_to_cover", 0)),
    }
    conditions = quality_gate.get("conditions")
    if not isinstance(conditions, list):
        _coverage_failure("COVERAGE_MEASURES_INVALID", "quality-gate conditions are absent")
    new_coverage = next(
        (
            condition
            for condition in conditions
            if isinstance(condition, Mapping) and condition.get("metricKey") == "new_coverage"
        ),
        None,
    )
    if not isinstance(new_coverage, Mapping):
        _coverage_failure("COVERAGE_MEASURES_INVALID", "new-coverage condition is absent")
    threshold_value = new_coverage.get("errorThreshold")
    actual_value = new_coverage.get("actualValue")
    if not isinstance(threshold_value, (str, int, float)) or not isinstance(
        actual_value, (str, int, float)
    ):
        _coverage_failure("COVERAGE_MEASURES_INVALID", "new-coverage condition is invalid")
    try:
        condition = {
            "status": new_coverage.get("status"),
            "threshold": int(float(threshold_value)),
            "actual_value": float(actual_value),
        }
    except (TypeError, ValueError) as error:
        raise RunnerError("COVERAGE_MEASURES_INVALID: new-coverage condition is invalid") from error
    reports = coverage.get("final_reports")
    if not isinstance(reports, list) or len(reports) != 2:
        _coverage_failure("COVERAGE_IMPORT_UNPROVEN", "final coverage reports are absent")
    combined = {
        **observations,
        "aggregate": aggregate,
        "new_coverage_condition": condition,
        "python_components": _component_coverage_summary(
            host, token, list(reports[0].get("source_paths", []))
        ),
        "dotnet_components": _component_coverage_summary(
            host, token, list(reports[1].get("source_paths", []))
        ),
    }
    return validate_coverage_analysis_evidence(identity, combined)


def _inventory_summary(
    inventory: Mapping[str, Any], blocking_keys: Sequence[str]
) -> dict[str, Any]:
    records = inventory.get("records")
    pages = inventory.get("pages")
    if not isinstance(records, list) or not isinstance(pages, list):
        raise RunnerError("COVERAGE_INVENTORY_INCOMPLETE: inventory records are incomplete")
    keys = sorted(
        record["key"]
        for record in records
        if isinstance(record, Mapping) and isinstance(record.get("key"), str)
    )
    if len(keys) != len(records):
        raise RunnerError("COVERAGE_INVENTORY_INCOMPLETE: inventory key is missing")
    blocked = sorted(set(blocking_keys))
    return {
        "complete": inventory.get("pagination_complete") is True,
        "result_empty": inventory.get("result_empty") is True,
        "page_count": len(pages),
        "total": inventory.get("total"),
        "record_count": len(records),
        "keys_sha256": _sha256_json(keys),
        "blocking_key_count": len(blocked),
        "blocking_keys_sha256": _sha256_json(blocked),
    }


def write_diagnostic_inventory(
    context: GitContext,
    run_id: str,
    identity: Mapping[str, Any],
    issues: Mapping[str, Any],
    hotspots: Mapping[str, Any],
    issue_result: Mapping[str, Any],
    hotspot_result: Mapping[str, Any],
) -> dict[str, Any]:
    issue_summary = _inventory_summary(
        issues,
        [
            item["key"]
            for item in issue_result.get("items", [])
            if item.get("disposition") != "FIXED_IN_CURRENT_HEAD"
        ],
    )
    hotspot_summary = _inventory_summary(
        hotspots,
        [item["key"] for item in hotspot_result.get("items", [])],
    )
    document = {
        "schema_version": 1,
        "write_mode": "create_new",
        "identity": dict(identity),
        "issues": {
            "pagination": {"page_size": PAGE_SIZE, **issue_summary},
            "records": issues["records"],
        },
        "hotspots": {
            "pagination": {"page_size": PAGE_SIZE, **hotspot_summary},
            "records": hotspots["records"],
        },
    }
    raw = json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    relative = f".agent/e/sonarqube/{PROJECT_KEY}/{context.head}/diagnostic/{run_id}.inventory.json"
    target = context.coordination_root / relative
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("xb") as stream:
            stream.write(raw)
    except OSError as error:
        raise RunnerError(
            "COVERAGE_INVENTORY_WRITE_FAILED: diagnostic inventory cannot be created"
        ) from error
    return {
        "artifact": {"relative_path": relative, "sha256": _sha256_bytes(raw), "bytes": len(raw)},
        "artifact_schema_version": 1,
        "issues": issue_summary,
        "hotspots": hotspot_summary,
    }


def _release_intent_for_role(role: str) -> str:
    return "none" if role == "diagnostic" else "v0.23.11"


def _blocked_failure(stage: str, error: BaseException) -> dict[str, Any]:
    message = str(error) or error.__class__.__name__
    prefix = message.split(":", 1)[0]
    code = (
        prefix if re.fullmatch(r"(?:COVERAGE|WAVE2)_[A-Z0-9_]+", prefix) else "COVERAGE_RUN_BLOCKED"
    )
    return {
        "code": code,
        "stage": stage,
        "language": None,
        "project_id": None,
        "safe_message": message,
    }


def receipt_base(context: GitContext, role: str, run_id: str) -> dict[str, Any]:
    return {
        "schema_version": EXACT_HEAD_RECEIPT_V3_SCHEMA_VERSION,
        "role": role,
        "outcome": "BLOCKED",
        "release_intent": _release_intent_for_role(role),
        "identity": {
            "captured_head": context.head,
            "project_key": PROJECT_KEY,
            "analysis_id": None,
        },
        "coverage": None,
        "analysis": None,
        "global_inventory": None,
        "release_gate": None,
        "cleanup": None,
        "failure": {
            "code": "COVERAGE_RUN_INCOMPLETE",
            "stage": "PLANNED",
            "language": None,
            "project_id": None,
            "safe_message": "exact-head coverage transaction has not completed",
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
    stage = "PLANNED"
    plan: CoveragePlan | None = None
    claim: CoverageRunClaim | None = None
    producer_terminal = False

    with project_lock(context.coordination_root, role, context.head, run_id):
        validate_exact_head_receipt_v3(receipt)
        write_receipt(target_receipt, receipt, secrets)
        try:
            entry = resolve_wave2_entry(context, clean_environment)
            resolved_wave2 = verify_wave2_entry(entry, context, clean_environment)
            preflight_coverage_toolchain(context=context, environment=clean_environment)
            stage = "TOOLCHAIN_READY"
            plan = derive_coverage_plan(context, run_id)
            credentials = load_credentials(context, inherited_environment, secrets)
            secrets.update(credentials.values())
            if role == "post-merge":
                assert_post_merge_target(context, clean_environment)
            clear_generated_artifacts(context, clean_environment)
            strict_cleanliness(context, clean_environment, "scanner begin")
            analysis_xml = context.repository_root / "SonarQube.Analysis.xml"
            if project_key_from_xml(analysis_xml) != PROJECT_KEY:
                raise RunnerError("COVERAGE_PROJECT_IDENTITY_INVALID: project key mismatch")
            scanner = discover_scanner(scanner_override)
            pre_scan_issues = issue_inventory(
                credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"]
            )
            scanner_env = scanner_environment(inherited_environment, credentials)
            run_process(
                scanner_begin_command(
                    scanner,
                    analysis_xml,
                    credentials["SONAR_HOST_URL"],
                    context.head,
                    credentials["SONAR_TOKEN"],
                    coverage_properties=coverage_scanner_properties(plan),
                ),
                cwd=context.repository_root,
                environment=scanner_env,
                secrets=secrets,
                label="SonarScanner begin",
                credential_input_names=("SONAR_TOKEN",),
            )
            stage = "SCANNER_BEGUN"
            claim = claim_coverage_run(context, plan, resolved_wave2)
            stage = "RUN_CLAIMED"
            solution, projects, standalone_projects = project_inventory(context.repository_root)
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
            stateless_before = capture_stateless_binary_hashes(plan)
            run_coverage_producer(plan, inherited_environment)
            producer_terminal = True
            stage = "PRODUCING"
            if isinstance(plan, CoveragePlan):
                dotnet_inputs = validate_dotnet_cobertura_inputs(context, plan)
            else:
                dotnet_inputs = []
            normalization = normalize_dotnet_cobertura(plan, dotnet_inputs)
            coverage = validate_coverage_reports(
                context,
                plan,
                claim,
                stateless_before,
                dotnet_inputs=dotnet_inputs,
                normalization=normalization,
            )
            stage = "REPORTS_VALIDATED"
            assert_head_unchanged(context, clean_environment)
            run_process(
                scanner_end_command(scanner, credentials["SONAR_TOKEN"]),
                cwd=context.repository_root,
                environment=scanner_env,
                secrets=secrets,
                label="SonarScanner end",
                credential_input_names=("SONAR_TOKEN",),
            )
            stage = "SCANNER_ENDED"
            scanner_metadata(context.repository_root, context.head)
            task_report = report_task(context.repository_root, credentials["SONAR_HOST_URL"])
            analysis_id = wait_for_ce_task(
                credentials["SONAR_HOST_URL"],
                task_report["ce_task_id"],
                credentials["SONAR_TOKEN"],
                {},
            )
            identity = {
                "captured_head": context.head,
                "project_key": PROJECT_KEY,
                "analysis_id": analysis_id,
            }
            current_before = current_analysis_binding(
                credentials["SONAR_HOST_URL"],
                analysis_id,
                context.head,
                credentials["SONAR_READ_TOKEN"],
            )
            quality_gate = analysis_quality_gate(
                credentials["SONAR_HOST_URL"], analysis_id, credentials["SONAR_READ_TOKEN"]
            )
            post_scan_issues = issue_inventory(
                credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"]
            )
            new_code_issue_inventory(credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"])
            issue_result = issue_dispositions(pre_scan_issues, post_scan_issues)
            current_after = current_analysis_binding(
                credentials["SONAR_HOST_URL"],
                analysis_id,
                context.head,
                credentials["SONAR_READ_TOKEN"],
            )
            hotspots = hotspot_inventory(
                credentials["SONAR_HOST_URL"], credentials["SONAR_READ_TOKEN"]
            )
            hotspot_result = hotspot_dispositions(hotspots)
            inventory = write_diagnostic_inventory(
                context,
                run_id,
                identity,
                post_scan_issues,
                hotspots,
                issue_result,
                hotspot_result,
            )
            clear_generated_artifacts(context, clean_environment)
            cleanup = cleanup_coverage_run(plan, producer_terminal)
            stage = "CLEANED"
            strict_cleanliness(context, clean_environment, "receipt publication")
            assert_head_unchanged(context, clean_environment)
            current_final = current_analysis_binding(
                credentials["SONAR_HOST_URL"],
                analysis_id,
                context.head,
                credentials["SONAR_READ_TOKEN"],
            )
            analysis = collect_coverage_analysis_evidence(
                credentials["SONAR_HOST_URL"],
                credentials["SONAR_READ_TOKEN"],
                identity,
                quality_gate,
                coverage,
                {
                    "submitted": identity,
                    "current_before_measures": {
                        "captured_head": current_before.get("revision"),
                        "project_key": PROJECT_KEY,
                        "analysis_id": current_before.get("analysis_id"),
                    },
                    "current_after_measures": {
                        "captured_head": current_after.get("revision"),
                        "project_key": PROJECT_KEY,
                        "analysis_id": current_after.get("analysis_id"),
                    },
                    "current_final": {
                        "captured_head": current_final.get("revision"),
                        "project_key": PROJECT_KEY,
                        "analysis_id": current_final.get("analysis_id"),
                    },
                },
            )
            stage = "CLEANED"
            release_gate = None
            outcome = "DIAGNOSTIC_COMPLETE"
            gate_error: RunnerError | None = None
            if role != "diagnostic":
                release_gate = {
                    "quality_gate_status": quality_gate.get("status"),
                    "blocking_issue_count": issue_result.get("blocking_count"),
                    "blocking_hotspot_count": hotspot_result.get("blocking_count"),
                }
                outcome = "PASS"
                if release_gate != {
                    "quality_gate_status": "OK",
                    "blocking_issue_count": 0,
                    "blocking_hotspot_count": 0,
                }:
                    gate_status = quality_gate.get("status")
                    gate_error = RunnerError(
                        f"Analysis-bound quality gate is {gate_status}; only OK passes."
                    )
            receipt = {
                "schema_version": EXACT_HEAD_RECEIPT_V3_SCHEMA_VERSION,
                "role": role,
                "outcome": outcome,
                "release_intent": _release_intent_for_role(role),
                "identity": identity,
                "coverage": coverage,
                "analysis": analysis,
                "global_inventory": inventory,
                "release_gate": release_gate,
                "cleanup": cleanup,
                "failure": None,
            }
            if gate_error is not None:
                raise gate_error
            validate_exact_head_receipt_v3(receipt)
            write_receipt(target_receipt, receipt, secrets)
        except Exception as error:
            cleanup_result: Any
            if plan is not None and claim is not None and stage != "CLEANED":
                cleanup_result = cleanup_coverage_run(plan, producer_terminal)
            else:
                cleanup_result = receipt.get("cleanup")
            blocked = {
                "schema_version": EXACT_HEAD_RECEIPT_V3_SCHEMA_VERSION,
                "role": role,
                "outcome": "BLOCKED",
                "release_intent": _release_intent_for_role(role),
                "identity": receipt.get(
                    "identity",
                    {
                        "captured_head": context.head,
                        "project_key": PROJECT_KEY,
                        "analysis_id": None,
                    },
                ),
                "coverage": receipt.get("coverage"),
                "analysis": receipt.get("analysis"),
                "global_inventory": receipt.get("global_inventory"),
                "release_gate": receipt.get("release_gate"),
                "cleanup": cleanup_result,
                "failure": _blocked_failure(stage, error),
            }
            validate_exact_head_receipt_v3(blocked)
            write_receipt(target_receipt, blocked, secrets)
            if isinstance(error, RunnerError):
                raise
            raise RunnerError(blocked["failure"]["safe_message"]) from error
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
