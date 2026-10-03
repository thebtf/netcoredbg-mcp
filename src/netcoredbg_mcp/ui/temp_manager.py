"""Per-session temporary file manager for screenshots and artifacts.

Provides isolated temp directories per debug session with automatic
cleanup on session end, server exit, and stale directory GC.
"""

from __future__ import annotations

import asyncio
import ctypes
import hashlib
import json
import logging
import os
import re
import shutil
import stat
import sys
import tempfile
import threading
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path

logger = logging.getLogger(__name__)

_LEASE_NAME = "owner-v1.lease"
_LEASE_MARKER = b"netcoredbg-mcp-session-owner-v1\n"
_OWNER_NAME = re.compile(r"owner-[0-9a-f]{32}\Z")
_SESSION_NAME = re.compile(r"session-[0-9a-f]{32}\Z")
_GC_WORK_SECONDS = 5.0
_GC_CLOSE_SECONDS = 5.0


def _windows_identity() -> str:
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    advapi.OpenProcessToken.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.HANDLE),
    ]
    advapi.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    ]
    advapi.ConvertSidToStringSidW.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.LPWSTR)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    token = wintypes.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, ctypes.byref(token)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        advapi.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not advapi.GetTokenInformation(token, 1, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.cast(data, ctypes.POINTER(ctypes.c_void_p))[0]
        text = wintypes.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            return text.value
        finally:
            kernel.LocalFree(text)
    finally:
        kernel.CloseHandle(token)


def _windows_private_root(path: Path, sid: str, create: bool) -> None:
    from ctypes import wintypes

    class SecurityAttributes(ctypes.Structure):
        _fields_ = [
            ("length", wintypes.DWORD),
            ("descriptor", ctypes.c_void_p),
            ("inherit", wintypes.BOOL),
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_void_p,
    ]
    advapi.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR,
        ctypes.c_int,
        wintypes.DWORD,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_void_p),
    ]
    advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW.argtypes = [
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.LPWSTR),
        ctypes.c_void_p,
    ]
    kernel.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(SecurityAttributes)]
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    expected = f"O:{sid}D:P(A;OICI;FA;;;{sid})"
    descriptor = ctypes.c_void_p()
    if create:
        if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            expected, 1, ctypes.byref(descriptor), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            attributes = SecurityAttributes(ctypes.sizeof(SecurityAttributes), descriptor, False)
            if not kernel.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                error = ctypes.get_last_error()
                if error != 183:
                    raise ctypes.WinError(error)
        finally:
            kernel.LocalFree(descriptor)
    error = advapi.GetNamedSecurityInfoW(
        str(path), 1, 5, None, None, None, None, ctypes.byref(descriptor)
    )
    if error:
        raise ctypes.WinError(error)
    text = wintypes.LPWSTR()
    try:
        if not advapi.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 5, ctypes.byref(text), None
        ):
            raise ctypes.WinError(ctypes.get_last_error())
        if text.value != expected:
            raise OSError("Artifact namespace is not private to the current user")
    finally:
        if text:
            kernel.LocalFree(text)
        kernel.LocalFree(descriptor)


def _safe_metadata(path: Path, *, directory: bool) -> os.stat_result:
    metadata = path.lstat()
    if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
        raise OSError("Artifact path is a link or reparse point")
    if not (stat.S_ISDIR(metadata.st_mode) if directory else stat.S_ISREG(metadata.st_mode)):
        raise OSError("Unexpected artifact path type")
    if not directory and metadata.st_nlink != 1:
        raise OSError("Artifact path is multiply linked")
    if os.name != "nt" and (
        metadata.st_uid != os.getuid() or (directory and metadata.st_mode & 0o077)
    ):
        raise OSError("Artifact path is not private to the current user")
    return metadata


def _namespace(*, create: bool = False) -> Path:
    identity = _windows_identity() if os.name == "nt" else str(os.getuid())
    suffix = hashlib.sha256(identity.encode()).hexdigest()[:16]
    root = Path(tempfile.gettempdir()).absolute() / f"netcoredbg-mcp-sessions-{suffix}"
    for parent in root.parents:
        metadata = parent.lstat()
        if stat.S_ISLNK(metadata.st_mode) or getattr(metadata, "st_file_attributes", 0) & 0x400:
            raise OSError("Artifact namespace has a link or reparse ancestor")
    if os.name == "nt":
        if not create and not root.exists():
            return root
        _windows_private_root(root, identity, create)
    elif create:
        root.mkdir(mode=0o700, exist_ok=True)
    if create or root.exists():
        _safe_metadata(root, directory=True)
    return root


def _lease(owner: Path, *, create: bool = False) -> int:
    _safe_metadata(owner, directory=True)
    path = owner / _LEASE_NAME
    before = None if create else _safe_metadata(path, directory=False)
    if before is not None and os.name != "nt" and before.st_mode & 0o077:
        raise OSError("Artifact lease is not private")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(path, flags | (os.O_CREAT | os.O_EXCL if create else 0), 0o600)
    try:
        opened = os.fstat(descriptor)
        after = _safe_metadata(path, directory=False)
        if (opened.st_dev, opened.st_ino) != (after.st_dev, after.st_ino) or (
            before is not None and (before.st_dev, before.st_ino) != (after.st_dev, after.st_ino)
        ):
            raise OSError("Artifact lease changed during open")
        if create:
            os.write(descriptor, _LEASE_MARKER)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        os.lseek(descriptor, 0, os.SEEK_SET)
        if os.read(descriptor, len(_LEASE_MARKER) + 1) != _LEASE_MARKER:
            raise OSError("Unrecognized artifact owner")
        return descriptor
    except BaseException:
        os.close(descriptor)
        raise


def _remove_session(path: Path) -> None:
    _safe_metadata(path, directory=True)
    for directory, directories, files in os.walk(path, followlinks=False):
        for name in directories:
            _safe_metadata(Path(directory) / name, directory=True)
        for name in files:
            _safe_metadata(Path(directory) / name, directory=False)
    shutil.rmtree(path)


def _try_owner_lease(owner: Path) -> int | None:
    if not _OWNER_NAME.fullmatch(owner.name):
        return None
    try:
        return _lease(owner)
    except OSError:
        return None


def _remove_stale_session(path: Path, cutoff: float) -> bool:
    if _safe_metadata(path, directory=True).st_mtime >= cutoff:
        return False
    _remove_session(path)
    return True


def _collect_stale(max_age_hours: float = 4.0) -> tuple[int, bool]:
    root = _namespace()
    if not root.exists():
        return 0, True
    cutoff = time.time() - max_age_hours * 3600
    removed = 0
    complete = True
    for owner in root.iterdir():
        descriptor = _try_owner_lease(owner)
        if descriptor is None:
            continue
        try:
            for session in owner.iterdir():
                if not _SESSION_NAME.fullmatch(session.name):
                    continue
                try:
                    if _remove_stale_session(session, cutoff):
                        removed += 1
                except OSError as error:
                    complete = False
                    logger.warning("Stale artifact preserved: %s", error)
        finally:
            os.close(descriptor)
    return removed, complete


class SessionTempManager:
    """Manages per-session temp directories for screenshot storage.

    Thread-safe. Each session gets an isolated directory that is cleaned
    up on stop_debug, atexit, or stale GC.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, Path] = {}
        self._closed_sessions: set[str] = set()
        self._lock = threading.Lock()
        self._owner_dir: Path | None = None
        self._owner_lease: int | None = None

    def _get_session_dir_locked(self, session_id: str) -> Path | None:
        if session_id in self._closed_sessions:
            logger.warning("Session temp directory is closed: %s", session_id)
            return None

        existing = self._sessions.get(session_id)
        if existing is not None and existing.exists():
            return existing

        try:
            if self._owner_dir is None:
                root = _namespace(create=True)
                owner = root / f"owner-{uuid.uuid4().hex}"
                owner.mkdir(mode=0o700)
                descriptor = _lease(owner, create=True)
                self._owner_dir, self._owner_lease = owner, descriptor
            dir_path = self._owner_dir / f"session-{uuid.uuid4().hex}"
            dir_path.mkdir(mode=0o700)
            self._sessions[session_id] = dir_path
            logger.info("Created session temp dir: %s", dir_path)
            return dir_path
        except OSError as error:
            logger.warning("Failed to create session temp dir: %s", error)
            return None

    @staticmethod
    def _safe_name(name: str) -> str | None:
        safe_name = Path(name).name
        if not safe_name or safe_name in (".", ".."):
            logger.warning("Invalid screenshot name rejected: %s", name)
            return None
        return safe_name

    def get_session_dir(self, session_id: str | None = None) -> Path | None:
        """Get or create a temp directory for the given session.

        Args:
            session_id: Session identifier. If None, generates a UUID4 prefix.

        Returns:
            Path to session temp directory, or None if creation fails.
        """
        if session_id is None:
            session_id = uuid.uuid4().hex[:12]

        with self._lock:
            return self._get_session_dir_locked(session_id)

    def save_screenshot_bundle(
        self,
        session_id: str,
        raw_data: bytes,
        raw_name: str,
        crop_data: bytes | None = None,
        crop_name: str | None = None,
    ) -> tuple[Path, Path | None] | None:
        """Persist raw evidence and its optional crop without exposing a partial bundle."""
        if (crop_data is None) != (crop_name is None):
            logger.warning("Evidence crop data and name must be supplied together")
            return None

        raw_safe_name = self._safe_name(raw_name)
        crop_safe_name = self._safe_name(crop_name) if crop_name is not None else None
        if raw_safe_name is None or (crop_name is not None and crop_safe_name is None):
            return None
        if raw_safe_name == crop_safe_name:
            logger.warning("Evidence bundle names must be distinct: %s", raw_safe_name)
            return None

        with self._lock:
            session_dir = self._get_session_dir_locked(session_id)
            if session_dir is None:
                return None

            files: list[tuple[Path, bytes]] = [(session_dir / raw_safe_name, raw_data)]
            if crop_data is not None and crop_safe_name is not None:
                files.append((session_dir / crop_safe_name, crop_data))
            return self._write_screenshot_bundle(session_dir, files)

    @staticmethod
    def _write_screenshot_bundle(
        session_dir: Path, files: list[tuple[Path, bytes]]
    ) -> tuple[Path, Path | None] | None:
        if any(path.exists() for path, _data in files):
            logger.warning("Evidence bundle destination already exists")
            return None

        staged: list[tuple[Path, Path]] = []
        written: list[Path] = []
        try:
            for destination, data in files:
                temporary = session_dir / f".{destination.name}.{uuid.uuid4().hex}.tmp"
                staged.append((temporary, destination))
                temporary.write_bytes(data)
            for temporary, destination in staged:
                temporary.replace(destination)
                written.append(destination)
        except OSError as error:
            for temporary, _destination in staged:
                temporary.unlink(missing_ok=True)
            for destination in written:
                destination.unlink(missing_ok=True)
            logger.warning("Failed to save screenshot evidence bundle: %s", error)
            return None

        crop_path = files[1][0] if len(files) == 2 else None
        return files[0][0], crop_path

    def save_screenshot(self, session_id: str, data: bytes, name: str) -> Path | None:
        """Save screenshot data to the session temp directory.

        Args:
            session_id: Session identifier.
            data: Screenshot bytes to save.
            name: Filename for the screenshot.

        Returns:
            Absolute path to saved file, or None if save fails.
        """
        session_dir = self.get_session_dir(session_id)
        if session_dir is None:
            return None

        safe_name = self._safe_name(name)
        if safe_name is None:
            return None

        file_path = session_dir / safe_name
        try:
            file_path.write_bytes(data)
            return file_path
        except OSError as e:
            logger.warning("Failed to save screenshot %s: %s", name, e)
            return None

    def cleanup_session(self, session_id: str) -> None:
        """Remove the temp directory for a specific session.

        Args:
            session_id: Session identifier to clean up.
        """
        with self._lock:
            self._closed_sessions.add(session_id)
            dir_path = self._sessions.pop(session_id, None)
            if dir_path is not None:
                try:
                    _remove_session(dir_path)
                except OSError as error:
                    logger.warning("Session artifact cleanup incomplete: %s", error)

    def cleanup_all(self) -> None:
        with self._lock:
            sessions_copy = dict(self._sessions)
            self._closed_sessions.update(sessions_copy)
            self._sessions.clear()

            for session_id, dir_path in sessions_copy.items():
                try:
                    _remove_session(dir_path)
                except OSError as error:
                    logger.warning("Session artifact cleanup incomplete: %s", error)
            if self._owner_lease is not None:
                os.close(self._owner_lease)
                self._owner_lease = None
            self._owner_dir = None

        if sessions_copy:
            logger.info("Cleaned up %d session temp directories", len(sessions_copy))

    @staticmethod
    def gc_stale(max_age_hours: float = 4.0) -> int:
        """Reclaim abandoned, recognized sessions strictly older than the cutoff.

        A live OS lease always protects an owner, regardless of artifact age.
        Discovery is confined to the private namespace: unmarked legacy flat
        TEMP directories are preserved, never scanned, adopted or migrated.
        Four hours is eligibility for a later opportunistic startup pass, not
        a deletion deadline. Return only the number of successful removals.
        """
        removed, _complete = _collect_stale(max_age_hours)
        return removed


async def _join_gc_task(task):
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.cancelled():
                raise
    return task.result()


async def _read_gc_output(stream):
    saved = bytearray()
    while chunk := await stream.read(4096):
        saved.extend(chunk[: max(0, 4096 - len(saved))])
    return bytes(saved)


async def _close_gc_worker(process, drain_status_type):
    if os.name == "nt":
        receipt = await process.aclose()
        facts = process.drain_snapshot(receipt)
        facts["root_pid"] = process.pid
        logger.info("GC worker drain receipt=%s", json.dumps(facts, sort_keys=True))
        if (
            receipt.owner != process.owner
            or receipt.status is not drain_status_type.DRAINED
            or receipt.active_processes != 0
        ):
            raise RuntimeError("GC worker owner drain failed")
    else:
        if process.returncode is None:
            process.kill()
        await asyncio.wait_for(process.wait(), _GC_CLOSE_SECONDS)
        logger.info("GC worker drain pid=%s status=drained active=0", process.pid)


def _report_gc_result(process, completed: bool, output: bytes) -> None:
    if completed:
        summary = json.loads(output)
        if summary.get("complete") is True:
            logger.info("Stale artifact sweep complete: removed=%s", summary["removed"])
        else:
            logger.warning("Stale artifact sweep incomplete: deletion failure")
    elif process.returncode is not None:
        logger.warning("Stale artifact sweep incomplete: worker exit=%s", process.returncode)


async def _gc_worker_supervisor() -> None:
    from ..windows_process_owner import DrainStatus, WindowsOwnedProcess

    async def launch():
        argv = [sys.executable, "-m", "netcoredbg_mcp.ui.temp_manager", "--gc-worker"]
        if os.name == "nt":
            return await WindowsOwnedProcess.launch(
                generation=object(), argv=argv, cwd=None, env=None, stdin_mode="devnull"
            )
        return await asyncio.create_subprocess_exec(
            *argv,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

    admission = asyncio.create_task(launch())
    process = None
    readers = []
    completed = False
    try:
        process = await asyncio.shield(admission)
        readers = [
            asyncio.create_task(_read_gc_output(stream))
            for stream in (process.stdout, process.stderr)
        ]
        deadline = time.monotonic() + _GC_WORK_SECONDS
        while process.returncode is None:
            if time.monotonic() >= deadline:
                logger.warning("Stale artifact sweep incomplete: useful-work timeout")
                break
            await asyncio.sleep(0.01)
        completed = process.returncode == 0
    except asyncio.CancelledError:
        logger.info("Stale artifact sweep incomplete: lifespan cancelled")
        raise
    except Exception:
        logger.exception("Stale artifact sweep incomplete: worker failure")
    finally:
        try:
            if process is None:
                process = await _join_gc_task(admission)
            await _join_gc_task(asyncio.create_task(_close_gc_worker(process, DrainStatus)))
            if readers:
                output, _errors = await _join_gc_task(
                    asyncio.ensure_future(asyncio.gather(*readers))
                )
                _report_gc_result(process, completed, output)
        finally:
            for reader in readers:
                if not reader.done():
                    reader.cancel()


@asynccontextmanager
async def temp_gc_lifespan(_server):
    worker = asyncio.create_task(_gc_worker_supervisor())
    try:
        yield {}
    finally:
        if not worker.done():
            worker.cancel()
        try:
            await _join_gc_task(worker)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Stale artifact sweep incomplete: owner/lifecycle failure")


if __name__ == "__main__":
    if sys.argv[1:] != ["--gc-worker"]:
        raise SystemExit(2)
    try:
        removed, complete = _collect_stale()
        print(json.dumps({"removed": removed, "complete": complete}))
        raise SystemExit(0 if complete else 1)
    except OSError as error:
        print(f"Stale artifact sweep incomplete: {error}", file=sys.stderr)
        raise SystemExit(1) from error
