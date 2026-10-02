"""Native claim deletion against isolated Windows files; no third-party dependencies."""

import ast
import ctypes
import importlib.util
import json
import struct
import subprocess
import sys
import unittest
from ctypes import wintypes
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_sonarqube_exact_head.py"
SPEC = importlib.util.spec_from_file_location("windows_claim_leaf_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


@unittest.skipUnless(sys.platform == "win32", "Windows native claim cleanup")
class TestWindowsClaimLeaf(unittest.TestCase):
    def setUp(self):
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repository = Path(self.temporary.name).resolve()
        context = runner.GitContext(*(self.repository,) * 4, "a" * 40)
        self.plan = runner.derive_coverage_plan(context, "123e4567-e89b-12d3-a456-426614174000")
        entry = dict(
            source_sha256="b" * 64,
            accepted_candidate_sha="a" * 40,
            pull_request_head_ref="work/owned-fixture",
            pull_request_head_sha="c" * 40,
            artifact_commit_sha="d" * 40,
            merge_commit_sha="d" * 40,
            integrated_tree_sha="e" * 40,
            observed_main_sha="f" * 40,
        )
        self.claim = runner.claim_coverage_run(context, self.plan, entry)
        scratch = self.plan.root / "python" / "pytest" / "real-git"
        scratch.mkdir(parents=True)
        subprocess.run(
            ["git", "-c", "core.longpaths=true", "init", "--quiet", str(scratch)], check=True
        )
        result = subprocess.run(
            ["git", "-C", str(scratch), "hash-object", "-w", "--stdin"],
            input=b"owned Git object bytes\n",
            check=True,
            capture_output=True,
        )
        oid = result.stdout.decode().strip()
        self.leaf = scratch / ".git" / "objects" / oid[:2] / oid[2:]
        self.original = self.leaf.read_bytes()
        self.attributes = self.leaf.stat(follow_symlinks=False).st_file_attributes
        self.external = self.repository / "external"
        self.external.mkdir()
        self.sentinel = self.external / "sentinel"
        self.sentinel.write_bytes(b"external value")
        self.external_attributes = self.sentinel.stat(follow_symlinks=False).st_file_attributes
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.kernel.CreateFileW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.HANDLE,
        ]
        self.kernel.CreateFileW.restype = wintypes.HANDLE
        self.kernel.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
        self.kernel.GetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel.SetFileInformationByHandle.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        self.kernel.SetFileInformationByHandle.restype = wintypes.BOOL
        self.kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        self.kernel.CloseHandle.restype = wintypes.BOOL
        self.ntdll = ctypes.WinDLL("ntdll", use_last_error=True)
        self.ntdll.NtCreateFile.argtypes = [
            ctypes.c_void_p,
            wintypes.ULONG,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            wintypes.ULONG,
            ctypes.c_void_p,
            wintypes.ULONG,
        ]
        self.ntdll.NtCreateFile.restype = wintypes.LONG
        self.native_paths = {}
        self.native_opened = set()
        self.addCleanup(self.assertEqual, self.native_opened, set())

    def proxy(self, **overrides):
        names = (
            "CreateFileW",
            "GetFileType",
            "GetFileInformationByHandle",
            "GetFileInformationByHandleEx",
            "SetFileInformationByHandle",
            "CloseHandle",
        )

        def create(*args):
            handle = overrides.get("CreateFileW", self.kernel.CreateFileW)(*args)
            if handle not in (None, ctypes.c_void_p(-1).value):
                self.native_paths[handle] = Path(args[0])
                self.native_opened.add(handle)
            return handle

        def relative_create(*args):
            result = self.ntdll.NtCreateFile(*args)
            if result >= 0:
                handle = args[0]._obj.value
                attributes = args[2]._obj
                self.native_paths[handle] = (
                    self.native_paths[attributes.root] / attributes.name.contents.buffer
                )
                self.native_opened.add(handle)
            return result

        def close(handle):
            result = overrides.get("CloseHandle", self.kernel.CloseHandle)(handle)
            if result:
                self.native_opened.discard(handle)
                del self.native_paths[handle]
            return result

        return SimpleNamespace(
            **{
                name: overrides.get(name, getattr(self.kernel, name))
                for name in names
                if name not in {"CreateFileW", "CloseHandle"}
            },
            CreateFileW=create,
            NtCreateFile=relative_create,
            CloseHandle=close,
            RtlNtStatusToDosError=self.ntdll.RtlNtStatusToDosError,
        )

    def test_directory_pin_blocks_in_place_junction_before_enumeration(self):
        control = self.repository / "junction-control"
        control.mkdir()
        try:
            self.set_junction(control)
            self.assertEqual((control / "sentinel").read_bytes(), b"external value")
        finally:
            control.rmdir()
        victim = self.plan.root / "mutation-target"
        victim.mkdir()
        attempted = []

        def information(handle, kind, data, size):
            if kind == 20 and self.native_paths.get(handle) == victim:
                with self.assertRaises(OSError) as caught:
                    self.set_junction(victim)
                self.assertEqual(caught.exception.winerror, 32)
                self.set_junction(victim, 0x100)
                self.assertEqual((victim / "sentinel").read_bytes(), b"external value")
                result = self.kernel.GetFileInformationByHandleEx(handle, kind, data, size)
                error = ctypes.get_last_error()
                names = []
                if result:
                    offset = 0
                    while True:
                        next_offset = struct.unpack_from("<I", data, offset)[0]
                        length = struct.unpack_from("<I", data, offset + 60)[0]
                        names.append(
                            bytes(data[offset + 88 : offset + 88 + length]).decode("utf-16-le")
                        )
                        if not next_offset:
                            break
                        offset += next_offset
                    self.assertFalse(set(names) - {".", ".."})
                else:
                    self.assertEqual(error, 18)
                attempted.append(names)
                print(
                    "NATIVE_JUNCTION_HANDLE_BOUND",
                    "WRITE_ATTRIBUTES mutation succeeded; original directory entries",
                    names,
                )
                ctypes.set_last_error(error)
                return result
            return self.kernel.GetFileInformationByHandleEx(handle, kind, data, size)

        with (
            patch.object(
                ctypes, "WinDLL", return_value=self.proxy(GetFileInformationByHandleEx=information)
            ),
            patch.object(
                Path, "iterdir", side_effect=AssertionError("pathname enumeration forbidden")
            ),
        ):
            outcome = runner.cleanup_coverage_run(self.plan, True, self.claim)
        self.assertEqual(len(attempted), 1)
        self.assertEqual(outcome["status"], "FAILED", outcome)
        self.assertEqual(outcome["failure"]["message"], "RunnerError")
        self.assertEqual(self.native_opened, set())
        self.assert_external_preserved()

    def assert_external_preserved(self):
        self.assertEqual(self.sentinel.read_bytes(), b"external value")
        self.assertEqual(
            self.sentinel.stat(follow_symlinks=False).st_file_attributes, self.external_attributes
        )

    def set_junction(self, path, access=0x40000000):
        self.kernel.DeviceIoControl.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
            ctypes.c_void_p,
        ]
        self.kernel.DeviceIoControl.restype = wintypes.BOOL
        substitute = ("\\??\\" + str(self.external)).encode("utf-16-le")
        printed = str(self.external).encode("utf-16-le")
        names = substitute + b"\0\0" + printed + b"\0\0"
        data = (
            struct.pack(
                "<IHHHHHH",
                0xA0000003,
                8 + len(names),
                0,
                0,
                len(substitute),
                len(substitute) + 2,
                len(printed),
            )
            + names
        )
        buffer = ctypes.create_string_buffer(data)
        handle = self.kernel.CreateFileW(str(path), access, 7, None, 3, 0x02200000, None)
        if handle in (None, ctypes.c_void_p(-1).value):
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            returned = wintypes.DWORD()
            if not self.kernel.DeviceIoControl(
                handle, 0x000900A4, buffer, len(data), None, 0, ctypes.byref(returned), None
            ):
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self.assertTrue(self.kernel.CloseHandle(handle))

    def test_native_cleanup_failure_receipt_retains_redacted_discriminator(self):
        secret = "controlled-provider-secret"
        calls = []

        def set_info(handle, kind, data, size):
            if kind == 21 and self.native_paths[handle] == self.leaf:
                flags = ctypes.cast(data, ctypes.POINTER(wintypes.DWORD))[0]
                calls.append(flags)
                if flags:
                    error = ctypes.WinError(5)
                    error.strerror = f"{secret}: {self.repository}"
                    error.filename = str(self.repository / secret)
                    raise error
                ctypes.set_last_error(87)
                return 0
            return self.kernel.SetFileInformationByHandle(handle, kind, data, size)

        with (
            patch.object(
                ctypes, "WinDLL", return_value=self.proxy(SetFileInformationByHandle=set_info)
            ),
            patch.object(Path, "chmod", side_effect=AssertionError("pathname chmod forbidden")),
        ):
            outcome = runner.cleanup_coverage_run(self.plan, True, self.claim)
        self.assertEqual(calls, [0x11, 0])
        self.assertEqual(outcome["status"], "FAILED")
        self.assertEqual(outcome["failure"]["code"], "COVERAGE_CLEANUP_FAILED")
        self.assertEqual(outcome["failure"]["message"], "PermissionError")
        self.assertEqual(self.native_opened, set())
        self.assertEqual(self.leaf.read_bytes(), self.original)
        self.assertEqual(self.leaf.stat(follow_symlinks=False).st_file_attributes, self.attributes)
        self.assert_external_preserved()
        self.assertEqual(
            outcome["failure"].get("native"),
            {
                "operation": "SetFileInformationByHandle(FileDispositionInfoEx)",
                "stage": "DISPOSITION",
                "entry": self.leaf.relative_to(self.plan.root).as_posix(),
                "winerror": 5,
                "errno": 13,
            },
        )
        receipt = {
            "schema_version": runner.EXACT_HEAD_RECEIPT_V3_SCHEMA_VERSION,
            "role": "diagnostic",
            "outcome": "BLOCKED",
            "release_intent": "none",
            "identity": {
                "captured_head": "a" * 40,
                "project_key": runner.PROJECT_KEY,
                "analysis_id": None,
            },
            "coverage": None,
            "analysis": None,
            "global_inventory": None,
            "release_gate": None,
            "cleanup": outcome,
            "failure": runner._blocked_failure(
                "ANALYSIS_BOUND",
                runner.RunnerError("COVERAGE_CLEANUP_FAILED: claimed run cleanup failed."),
            ),
        }
        runner.validate_exact_head_receipt_v3(receipt)
        receipt_path = self.repository / "receipt.json"
        runner.write_receipt(receipt_path, receipt, (secret,))
        raw = receipt_path.read_text(encoding="utf-8")
        self.assertEqual(json.loads(raw), receipt)
        self.assertNotIn(secret, raw)
        self.assertNotIn(str(self.repository), raw)
        self.assertNotIn(self.repository.as_posix(), raw)
        print(
            "NATIVE_FAILURE_DISCRIMINATOR",
            outcome["failure"],
            "OPEN_HANDLES",
            len(self.native_opened),
        )

    def test_real_git_readonly_cleanup_without_path_chmod(self):
        ast.parse(RUNNER_PATH.read_text(encoding="utf-8"), feature_version=(3, 10))
        with self.assertRaises(PermissionError) as denied:
            self.leaf.unlink()
        self.assertEqual(denied.exception.winerror, 5)
        (self.plan.root / "directory-link").symlink_to(self.external, target_is_directory=True)
        (self.plan.root / "file-link").symlink_to(self.sentinel)
        junction = self.plan.root / "directory-junction"
        junction.mkdir()
        self.set_junction(junction)
        closed = []

        def close(handle):
            path = self.native_paths[handle]
            result = self.kernel.CloseHandle(handle)
            if result:
                entry = (
                    path.relative_to(self.plan.root).as_posix()
                    if path.is_relative_to(self.plan.root)
                    else "@ancestor"
                )
                closed.append((handle, entry))
            return result

        with (
            patch.object(Path, "chmod", side_effect=NotImplementedError("no path chmod")),
            patch.object(ctypes, "WinDLL", return_value=self.proxy(CloseHandle=close)),
        ):
            outcome = runner.cleanup_coverage_run(self.plan, True, self.claim)
        self.assertEqual(outcome["status"], "OK", outcome)
        self.assertFalse(self.plan.root.exists())
        self.assert_external_preserved()
        self.assertEqual(self.native_opened, set())
        self.assertEqual(self.native_paths, {})
        print(
            "NATIVE_CLEANUP_SMOKE",
            outcome["status"],
            "UNLINK_WINERROR",
            denied.exception.winerror,
            "CLOSED_EXACT_HANDLES",
            closed,
            "EXTERNAL_BYTES_ATTRIBUTES",
            "PRESERVED",
        )

    def test_claim_and_ancestors_are_pinned_before_validation_through_final_delete(self):
        validate = runner.validate_coverage_marker
        checked = []
        handles = self.native_paths
        final_directories = []

        def create(*args):
            handle = self.kernel.CreateFileW(*args)
            handles[handle] = Path(args[0])
            return handle

        def set_info(handle, kind, data, size):
            target = handles.get(handle)
            if (
                kind == 21
                and ctypes.cast(data, ctypes.POINTER(wintypes.DWORD))[0] & 1
                and target is not None
                and target.is_dir()
            ):
                with self.assertRaises(PermissionError):
                    target.rename(target.with_name(target.name + "-unclaimed"))
                final_directories.append(str(target))
            return self.kernel.SetFileInformationByHandle(handle, kind, data, size)

        def guarded_validate(*args):
            for target in (self.plan.root, self.plan.root.parent, self.repository):
                with self.assertRaises(PermissionError):
                    target.rename(target.with_name(target.name + "-unclaimed"))
                checked.append(str(target))
            return validate(*args)

        with (
            patch.object(runner, "validate_coverage_marker", side_effect=guarded_validate),
            patch.object(
                ctypes,
                "WinDLL",
                return_value=self.proxy(CreateFileW=create, SetFileInformationByHandle=set_info),
            ),
            patch.object(
                runner.shutil,
                "rmtree",
                side_effect=AssertionError("pathname deletion is forbidden"),
            ),
        ):
            outcome = runner.cleanup_coverage_run(self.plan, True, self.claim)
        self.assertEqual(outcome["status"], "OK", outcome)
        self.assertEqual(len(checked), 3)
        self.assertIn(str(self.leaf.parent), final_directories)
        self.assertIn(str(self.plan.root), final_directories)
        self.assertIn(str(self.plan.root.parent), final_directories)
        self.assert_external_preserved()
        print("NATIVE_PINNED_VALIDATION", checked)

    def test_late_alias_after_last_observation_preserves_bytes_and_shared_attributes(self):
        handles = self.native_paths
        shared = self.external / "late-alias"

        def create(*args):
            handle = self.kernel.CreateFileW(*args)
            handles[handle] = Path(args[0])
            return handle

        def set_info(handle, kind, data, size):
            if handles.get(handle) == self.leaf and kind in (4, 21) and not shared.exists():
                runner.os.link(self.leaf, shared)
            return self.kernel.SetFileInformationByHandle(handle, kind, data, size)

        with patch.object(
            ctypes,
            "WinDLL",
            return_value=self.proxy(CreateFileW=create, SetFileInformationByHandle=set_info),
        ):
            outcome = runner.cleanup_coverage_run(self.plan, True, self.claim)
        self.assertEqual(outcome["status"], "OK", outcome)
        self.assertEqual(shared.read_bytes(), self.original)
        self.assertEqual(shared.stat(follow_symlinks=False).st_file_attributes, self.attributes)
        self.assert_external_preserved()
        print(
            "NATIVE_LATE_ALIAS", "created after final observation; bytes and attributes unchanged"
        )

    def test_alias_before_last_observation_blocks_without_attribute_mutation(self):
        shared = self.external / "early-alias"
        handles = self.native_paths
        injected = False

        def create(*args):
            handle = self.kernel.CreateFileW(*args)
            handles[handle] = Path(args[0])
            return handle

        def information(handle, data):
            nonlocal injected
            result = self.kernel.GetFileInformationByHandle(handle, data)
            if result and handles.get(handle) == self.leaf and not injected:
                runner.os.link(self.leaf, shared)
                injected = True
            return result

        with patch.object(
            ctypes,
            "WinDLL",
            return_value=self.proxy(CreateFileW=create, GetFileInformationByHandle=information),
        ):
            outcome = runner.cleanup_coverage_run(self.plan, True, self.claim)
        self.assertTrue(injected)
        self.assertEqual(outcome["status"], "FAILED", outcome)
        self.assertEqual(shared.read_bytes(), self.original)
        self.assertEqual(shared.stat(follow_symlinks=False).st_file_attributes, self.attributes)
        self.assert_external_preserved()

    def test_unsupported_disposition_and_interruption_close_handles_without_fallback(self):
        for interrupted in (False, True):
            with self.subTest(interrupted=interrupted):
                if interrupted:
                    self.setUp()
                opened = self.native_opened
                injected = False
                interruption = KeyboardInterrupt("controlled native interruption")

                def create(*args):
                    handle = self.kernel.CreateFileW(*args)
                    if handle not in (None, ctypes.c_void_p(-1).value):
                        opened.add(handle)
                    return handle

                def close(handle):
                    result = self.kernel.CloseHandle(handle)
                    if result:
                        opened.remove(handle)
                    return result

                def set_info(handle, kind, data, size):
                    nonlocal injected
                    if not injected and kind == 21:
                        injected = True
                        if interrupted:
                            raise interruption
                        ctypes.set_last_error(87)
                        return 0
                    return self.kernel.SetFileInformationByHandle(handle, kind, data, size)

                with patch.object(
                    ctypes,
                    "WinDLL",
                    return_value=self.proxy(
                        CreateFileW=create, SetFileInformationByHandle=set_info, CloseHandle=close
                    ),
                ):
                    if interrupted:
                        with self.assertRaises(KeyboardInterrupt) as caught:
                            runner.cleanup_coverage_run(self.plan, True, self.claim)
                        self.assertIs(caught.exception, interruption)
                    else:
                        outcome = runner.cleanup_coverage_run(self.plan, True, self.claim)
                        self.assertEqual(outcome["status"], "FAILED", outcome)
                self.assertTrue(injected)
                self.assertEqual(opened, set())
                self.assertEqual(self.leaf.read_bytes(), self.original)
                self.assertEqual(
                    self.leaf.stat(follow_symlinks=False).st_file_attributes, self.attributes
                )
                self.assert_external_preserved()


if __name__ == "__main__":
    print("CANDIDATE", RUNNER_PATH, "RUNTIME", sys.version, flush=True)
    unittest.main()
