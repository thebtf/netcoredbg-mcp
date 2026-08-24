#!/usr/bin/env python3
"""Run one secret-free, exact-head SonarQube release scan."""

from __future__ import annotations

import argparse
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


def clear_generated_artifacts(context: GitContext, environment: Mapping[str, str]) -> list[str]:
    """Delete only known ignored scanner/build output from the disposable worktree."""
    candidates = [context.repository_root / name for name in GENERATED_ROOT_NAMES]
    for directory in iter_scanner_tree(context.repository_root, "*"):
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


def scanner_begin_command(
    scanner: Sequence[str], analysis_xml: Path, host: str, head: str, token: str
) -> list[str]:
    return [
        *scanner,
        "begin",
        f"/k:{PROJECT_KEY}",
        f"/s:{analysis_xml}",
        f"/d:sonar.host.url={host}",
        f"/d:sonar.scm.revision={head}",
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


def scanner_metadata(repository_root: Path, expected_head: str) -> dict[str, Any]:
    metadata_root = repository_root / ".sonarqube"
    if not metadata_root.is_dir():
        raise RunnerError("SonarScanner did not create metadata.")
    found: dict[str, list[tuple[str, str]]] = {"sonar.projectKey": [], "sonar.scm.revision": []}
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
                    credentials["SONAR_TOKEN"],
                ),
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
