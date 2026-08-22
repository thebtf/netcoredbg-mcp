"""Focused contracts for the dependency-free SonarQube exact-head runner."""

import importlib.util
import json
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch


RUNNER_PATH = Path(__file__).resolve().parents[1] / "scripts" / "run_sonarqube_exact_head.py"
SPEC = importlib.util.spec_from_file_location("sonarqube_exact_head_runner", RUNNER_PATH)
assert SPEC is not None and SPEC.loader is not None
runner = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = runner
SPEC.loader.exec_module(runner)


class TestSonarqubeExactHeadRunner(TestCase):
    @staticmethod
    def credentials():
        return {
            "SONAR_HOST_URL": "https://sonar.example.test",
            "SONAR_TOKEN": "scan-token",
            "SONAR_READ_TOKEN": "read-token",
        }

    def test_build_environment_scrubs_all_sonar_credentials(self):
        build_environment = runner.scrub_sonar_environment(
            {**self.credentials(), "SONAR_ADMIN_TOKEN": "admin-token", "SAFE_VALUE": "kept"}
        )

        self.assertFalse(set(build_environment).intersection(runner.SONAR_ENV))

    def test_scanner_environment_exposes_only_scan_credential(self):
        scanner_environment = runner.scanner_environment(
            {**self.credentials(), "SONAR_ADMIN_TOKEN": "admin-token"}, self.credentials()
        )

        self.assertEqual(
            {key: scanner_environment[key] for key in runner.SONAR_ENV if key in scanner_environment},
            {"SONAR_HOST_URL": "https://sonar.example.test", "SONAR_TOKEN": "scan-token"},
        )

    def test_scanner_commands_supply_token_but_render_redacted(self):
        begin = runner.scanner_begin_command(
            ["scanner"], Path("SonarQube.Analysis.xml"), "https://sonar.example.test", "a" * 40, "scan-token"
        )
        end = runner.scanner_end_command(["scanner"], "scan-token")

        self.assertIn("/d:sonar.token=scan-token", begin)
        self.assertIn("/d:sonar.token=scan-token", end)
        self.assertNotIn("scan-token", runner.redact(" ".join(begin), ("scan-token",)))

    def test_in_tree_dotenv_is_rejected_before_source_children_run(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            nested_directory = root / "candidate-controlled"
            nested_directory.mkdir()
            (nested_directory / ".env").write_text("SONAR_TOKEN=synthetic", encoding="utf-8")

            with self.assertRaisesRegex(runner.RunnerError, "in-tree .env"):
                runner.load_credentials(root, self.credentials())

    def test_malformed_host_is_a_named_credential_blocker(self):
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(runner.CredentialsUnavailable, r"^SONAR_CREDENTIALS_UNAVAILABLE: SONAR_HOST_URL\."):
                runner.load_credentials(
                    Path(temporary_directory),
                    {**self.credentials(), "SONAR_HOST_URL": "sonar.example.test"},
                )

    def test_scanner_auth_failure_is_a_named_credential_blocker(self):
        with TemporaryDirectory() as temporary_directory:
            with patch.object(
                runner.subprocess,
                "run",
                return_value=runner.subprocess.CompletedProcess([], 1, "HTTP 401 unauthorized"),
            ):
                with self.assertRaisesRegex(runner.CredentialsUnavailable, r"^SONAR_CREDENTIALS_UNAVAILABLE: SONAR_TOKEN\."):
                    runner.run_process(
                        ["scanner", "begin"],
                        cwd=Path(temporary_directory),
                        environment={},
                        secrets=("scan-token",),
                        label="SonarScanner begin",
                        credential_input_names=("SONAR_TOKEN",),
                    )

    def test_ce_http_auth_error_is_attributed_to_scan_credential(self):
        class Opener:
            def open(self, *_args, **_kwargs):
                raise runner.urllib.error.HTTPError("https://sonar.example.test", 401, "Unauthorized", None, None)

        with patch.object(runner, "API_OPENER", Opener()):
            with self.assertRaises(runner.ApiHttpError) as raised:
                runner.api_json("https://sonar.example.test", "/api/ce/task", {"id": "task"}, "scan-token")

        self.assertEqual((raised.exception.status, raised.exception.input_name), (401, "SONAR_TOKEN"))

    def test_admin_token_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(runner.RunnerError, "SONAR_ADMIN_TOKEN"):
                runner.load_credentials(
                    Path(temporary_directory), {**self.credentials(), "SONAR_ADMIN_TOKEN": "admin-token"}
                )

    def test_head_drift_after_scan_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory)
            context = runner.GitContext(path, path, path, path, "a" * 40)
            with patch.object(runner, "git_output", return_value="b" * 40):
                with self.assertRaisesRegex(runner.RunnerError, "HEAD changed"):
                    runner.assert_head_unchanged(context, {})

    def test_ignored_worktree_state_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            path = Path(temporary_directory)
            context = runner.GitContext(path, path, path, path, "a" * 40)
            with patch.object(runner, "git_output", return_value="!! bin/"):
                with self.assertRaisesRegex(runner.RunnerError, "not clean"):
                    runner.strict_cleanliness(context, {}, "scanner begin")
    def test_attached_worktree_is_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            common_dir = root / "common"
            git_dir = root / "linked"
            common_dir.mkdir()
            git_dir.mkdir()
            with patch.object(
                runner,
                "git_output",
                side_effect=[str(root), str(common_dir), str(git_dir), "a" * 40],
            ):
                with patch.object(
                    runner,
                    "git_result",
                    return_value=runner.subprocess.CompletedProcess([], 0, "refs/heads/main", ""),
                ):
                    with self.assertRaisesRegex(runner.RunnerError, "detached HEAD"):
                        runner.git_context(root, {})
    def test_project_inventory_in_agent_hosted_worktree_keeps_projects(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / ".agent" / "worktrees" / "scanner"
            project = root / "host" / "App.csproj"
            project.parent.mkdir(parents=True)
            project.write_text("<Project />", encoding="utf-8")
            (root / "netcoredbg-mcp.sln").write_text(
                'Project("{guid}") = "App", "host\\App.csproj", "{id}"\nEndProject\n',
                encoding="utf-8",
            )
            solution, projects, standalone_projects = runner.project_inventory(root)

        self.assertEqual((solution.name, projects, standalone_projects), ("netcoredbg-mcp.sln", [project.resolve()], []))



    def test_compute_engine_readback_uses_submitted_task_and_scan_credential(self):
        calls = []

        def fake_api(host, endpoint, parameters, token):
            calls.append((host, endpoint, parameters, token))
            return {
                "task": {
                    "id": "task-1",
                    "status": "SUCCESS",
                    "componentKey": runner.PROJECT_KEY,
                    "analysisId": "analysis-1",
                }
            }

        receipt = {}
        with patch.object(runner, "api_json", side_effect=fake_api):
            analysis_id = runner.wait_for_ce_task(
                "https://sonar.example.test", "task-1", "scan-token", receipt
            )

        self.assertEqual(
            (
                analysis_id,
                receipt["compute_engine"]["submitted_task_id"],
                receipt["compute_engine"]["task_id"],
                receipt["compute_engine"]["returned_task_id"],
                receipt["compute_engine"]["analysis_id"],
                receipt["compute_engine"]["component_key"],
                receipt["compute_engine"]["last_observed_state"],
                calls,
            ),
            (
                "analysis-1", "task-1", "task-1", "task-1", "analysis-1", runner.PROJECT_KEY, "SUCCESS",
                [("https://sonar.example.test", "/api/ce/task", {"id": "task-1"}, "scan-token")],
            ),
        )

    def test_compute_engine_timeout_preserves_deadline_and_last_state(self):
        receipt = {}
        with (
            patch.object(
                runner,
                "api_json",
                return_value={"task": {"id": "task-1", "status": "PENDING", "componentKey": runner.PROJECT_KEY}},
            ),
            patch.object(runner.time, "monotonic", side_effect=[0, runner.CE_TIMEOUT_SECONDS + 1]),
        ):
            with self.assertRaisesRegex(runner.RunnerError, "10-minute deadline"):
                runner.wait_for_ce_task("https://sonar.example.test", "task-1", "scan-token", receipt)

        self.assertEqual(
            (receipt["compute_engine"]["last_observed_state"], bool(receipt["compute_engine"]["poll_deadline_at"])),
            ("PENDING", True),
        )

    def test_compute_engine_no_response_preserves_marker_and_deadline(self):
        receipt = {}
        with patch.object(runner, "api_json", side_effect=runner.ApiHttpError("/api/ce/task", 503, "SONAR_TOKEN")):
            with self.assertRaises(runner.ApiHttpError):
                runner.wait_for_ce_task("https://sonar.example.test", "task-1", "scan-token", receipt)

        self.assertEqual(
            (receipt["compute_engine"]["last_observed_state"], bool(receipt["compute_engine"]["poll_deadline_at"])),
            ("NO_RESPONSE", True),
        )

    def test_current_analysis_rejects_a_concurrent_newer_analysis(self):
        def fake_api(*_):
            return {"analyses": [{"key": "concurrent-analysis", "revision": "b" * 40}]}

        with patch.object(runner, "api_json", side_effect=fake_api):
            with self.assertRaisesRegex(runner.RunnerError, "not the current"):
                runner.current_analysis_binding(
                    "https://sonar.example.test", "submitted-analysis", "a" * 40, "read-token"
                )

    def test_quality_gate_readback_is_bound_to_analysis_and_reader(self):
        calls = []

        def fake_api(host, endpoint, parameters, token):
            calls.append((host, endpoint, parameters, token))
            return {"projectStatus": {"status": "OK", "conditions": []}}

        with patch.object(runner, "api_json", side_effect=fake_api):
            gate = runner.analysis_quality_gate("https://sonar.example.test", "analysis-1", "read-token")

        self.assertEqual(
            (gate["analysis_id"], calls),
            (
                "analysis-1",
                [
                    (
                        "https://sonar.example.test",
                        "/api/qualitygates/project_status",
                        {"analysisId": "analysis-1"},
                        "read-token",
                    )
                ],
            ),
        )


    def test_hotspot_inventory_binds_live_project_filter(self):
        calls = []

        def fake_api(host, endpoint, parameters, token):
            calls.append((host, endpoint, parameters, token))
            return {"paging": {"pageIndex": 1, "pageSize": 500, "total": 0}, "hotspots": []}

        with patch.object(runner, "api_json", side_effect=fake_api):
            inventory = runner.hotspot_inventory("https://sonar.example.test", "read-token")

        self.assertEqual(
            (inventory["query"], calls[0][2]),
            (
                {"project": runner.PROJECT_KEY},
                {"project": runner.PROJECT_KEY, "p": "1", "ps": "500"},
            ),
        )
    def test_warn_error_and_none_quality_gates_are_rejected(self):
        for status in ("WARN", "ERROR", "NONE"):
            with self.subTest(status=status):
                with self.assertRaisesRegex(runner.RunnerError, status):
                    runner.require_ok_quality_gate({"status": status})

    def test_issue_inventory_queries_accepted_and_fixed_dispositions(self):
        calls = []

        def fake_api(host, endpoint, parameters, token):
            calls.append((host, endpoint, parameters, token))
            return {
                "paging": {"pageIndex": 1, "pageSize": 500, "total": 1},
                "issues": [{"key": "accepted-1", "issueStatus": "ACCEPTED", "resolution": "WONTFIX"}],
            }

        with patch.object(runner, "api_json", side_effect=fake_api):
            inventory = runner.issue_inventory("https://sonar.example.test", "read-token")

        self.assertEqual(
            (inventory["query"], calls[0][2]),
            (
                {"components": runner.PROJECT_KEY, "issueStatuses": "OPEN,CONFIRMED,FALSE_POSITIVE,ACCEPTED,FIXED,IN_SANDBOX"},
                {"components": runner.PROJECT_KEY, "issueStatuses": "OPEN,CONFIRMED,FALSE_POSITIVE,ACCEPTED,FIXED,IN_SANDBOX", "p": "1", "ps": "500"},
            ),
        )

    def test_accepted_issue_disposition_blocks_release(self):
        before = {"records": []}
        after = {
            "records": [
                {
                    "key": "accepted-1",
                    "issueStatus": "ACCEPTED",
                    "status": "RESOLVED",
                    "resolution": "WONTFIX",
                }
            ]
        }

        self.assertEqual(runner.issue_dispositions(before, after)["blocking_count"], 1)

    def test_false_positive_issue_disposition_blocks_release(self):
        disposition = runner.issue_dispositions(
            {"records": []},
            {"records": [{"key": "false-positive-1", "issueStatus": "FALSE_POSITIVE", "resolution": "FALSE-POSITIVE"}]},
        )

        self.assertEqual(disposition["blocking_count"], 1)

    def test_any_hotspot_is_a_conservative_release_block(self):
        inventory = {
            "total": 1,
            "records": [{"key": "hotspot-1", "status": "REVIEWED", "resolution": "SAFE"}],
        }

        self.assertEqual(runner.hotspot_dispositions(inventory)["blocking_count"], 1)

    def test_stale_dead_owner_lock_is_reclaimed(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            stale_path = runner.lock_path(root)
            stale_path.parent.mkdir(parents=True)
            stale_path.write_text(json.dumps({"pid": 42}), encoding="utf-8")
            with patch.object(runner, "owner_is_alive", return_value=False):
                with runner.project_lock(root, "candidate", "a" * 40, "new-run"):
                    acquired = stale_path.exists()

        self.assertTrue(acquired)

    def test_windows_owner_probe_never_calls_os_kill(self):
        with (
            patch.object(runner.os, "name", "nt"),
            patch.object(runner, "windows_owner_is_alive", return_value=True) as windows_probe,
            patch.object(runner.os, "kill") as kill,
        ):
            self.assertTrue(runner.owner_is_alive(42))

        windows_probe.assert_called_once_with(42)
        kill.assert_not_called()

    def test_windows_api_handles_use_pointer_safe_prototypes(self):
        class Function:
            argtypes: object
            restype: object

            def __call__(self, *_args):
                return 0

        class Kernel32:
            OpenProcess = Function()
            WaitForSingleObject = Function()
            CloseHandle = Function()

        class WinTypes:
            DWORD = object()
            BOOL = object()
            HANDLE = object()

        kernel32 = Kernel32()
        runner.configure_windows_process_api(kernel32, WinTypes)

        self.assertEqual(
            (kernel32.OpenProcess.argtypes, kernel32.OpenProcess.restype, kernel32.WaitForSingleObject.argtypes, kernel32.CloseHandle.argtypes),
            ([WinTypes.DWORD, WinTypes.BOOL, WinTypes.DWORD], WinTypes.HANDLE, [WinTypes.HANDLE, WinTypes.DWORD], [WinTypes.HANDLE]),
        )

    def test_running_receipt_replaces_prior_pass_before_work(self):
        with TemporaryDirectory() as temporary_directory:
            receipt_path = Path(temporary_directory) / "candidate.json"
            runner.write_receipt(receipt_path, {"outcome": "PASS"}, ())
            context = runner.GitContext(Path(temporary_directory), Path(temporary_directory), Path(temporary_directory), Path(temporary_directory), "a" * 40)
            runner.write_receipt(receipt_path, runner.receipt_base(context, "candidate", "new-run"), ())
            replacement = json.loads(receipt_path.read_text(encoding="utf-8"))

        self.assertEqual(replacement["outcome"], "RUNNING")

    def test_cross_origin_api_response_is_rejected(self):
        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *_):
                return False

            def geturl(self):
                return "https://other.example.test/api/ce/task"

            def read(self):
                return b"{}"

        class Opener:
            def open(self, *_args, **_kwargs):
                return Response()

        with patch.object(runner, "API_OPENER", Opener()):
            with self.assertRaisesRegex(runner.RunnerError, "origin differs"):
                runner.api_json("https://sonar.example.test", "/api/ce/task", {"id": "task"}, "scan-token")
    def test_redirect_handler_never_constructs_a_redirect_request(self):
        handler = runner.NoRedirectHandler()

        self.assertIsNone(handler.redirect_request(None, None, 302, "https://other.example.test", {}, None))

    def test_pass_receipt_schema_rejects_missing_observed_evidence(self):
        with self.assertRaisesRegex(runner.RunnerError, "evidence schema"):
            runner.validate_pass_receipt({"schema_version": runner.RECEIPT_SCHEMA_VERSION})

    def test_pass_receipt_requires_each_observed_evidence_owner(self):
        head = "a" * 40

        def inventory(endpoint):
            query = (
                {"components": runner.PROJECT_KEY, "issueStatuses": runner.ISSUE_STATUSES}
                if endpoint == "/api/issues/search"
                else {"project": runner.PROJECT_KEY}
            )
            return {
                "endpoint": endpoint,
                "query": query,
                "total": 0,
                "pages": [{"page_index": 1, "page_size": 500, "total": 0}],
                "pagination_complete": True,
                "result_empty": True,
                "records": [],
            }

        receipt = {
            "schema_version": runner.RECEIPT_SCHEMA_VERSION,
            "outcome": "PASS",
            "project_key": runner.PROJECT_KEY,
            "analysis_xml_project_key": runner.PROJECT_KEY,
            "run_id": "run",
            "role": "candidate",
            "captured_head": head,
            "completed_at": "2026-08-23T00:00:00Z",
            "post_scan_head": head,
            "worktree": {
                "repository_root": "root",
                "git_dir": "git-dir",
                "common_dir": "common-dir",
                "coordination_root": "coordination-root",
                "detached": True,
                "linked": True,
            },
            "cleanliness": {"pre": {"status": "clean"}, "post": {"status": "clean"}},
            "scanner_metadata": {"observed": True, "project_key": runner.PROJECT_KEY, "sonar_scm_revision": head},
            "task_report": {"observed": True, "project_key": runner.PROJECT_KEY, "ce_task_id": "task", "server_url": "https://sonar.example.test"},
            "compute_engine": {
                "submitted_task_id": "task",
                "task_id": "task",
                "returned_task_id": "task",
                "component_key": runner.PROJECT_KEY,
                "analysis_id": "analysis",
                "poll_deadline_at": "2026-08-23T00:10:00Z",
                "last_observed_state": "SUCCESS",
                "states": [{"status": "SUCCESS"}],
            },
            "analysis_current_before_issues": {"observed": True, "current": True, "analysis_id": "analysis", "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"}, "revision": head},
            "analysis_current_after_issues": {"observed": True, "current": True, "analysis_id": "analysis", "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"}, "revision": head},
            "analysis_current_final": {"observed": True, "current": True, "analysis_id": "analysis", "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"}, "revision": head},
            "quality_gate": {"analysis_id": "analysis", "status": "OK"},
            "pre_scan_issues": inventory("/api/issues/search"),
            "post_scan_issues": inventory("/api/issues/search"),
            "hotspots": inventory("/api/hotspots/search"),
            "issue_dispositions": {"blocking_count": 0, "items": []},
            "hotspot_dispositions": {"blocking_count": 0, "items": []},
        }
        runner.validate_pass_receipt(receipt)

        for path, value in (
            (("compute_engine", "submitted_task_id"), "wrong-task"),
            (("compute_engine", "analysis_id"), "wrong-analysis"),
            (("compute_engine", "poll_deadline_at"), ""),
            (("compute_engine", "last_observed_state"), 1),
            (("issue_dispositions", "blocking_count"), True),
        ):
            with self.subTest(path=path):
                original = receipt[path[0]][path[1]]
                receipt[path[0]][path[1]] = value
                with self.assertRaises(runner.RunnerError):
                    runner.validate_pass_receipt(receipt)
                receipt[path[0]][path[1]] = original

        forged_cases = (
            ("project_key", "wrong-project"),
            ("analysis_xml_project_key", "wrong-project"),
            ("pre_scan_issues.query", {"componentKeys": runner.PROJECT_KEY, "issueStatuses": runner.ISSUE_STATUSES}),
            ("pre_scan_issues.pages", []),
            ("post_scan_issues.result_empty", False),
            ("hotspots.total", 1),
            ("issue_dispositions.items", [{"key": "forged", "disposition": "FIXED_IN_CURRENT_HEAD"}]),
            ("hotspot_dispositions.items", [{"key": "forged", "disposition": "BLOCKING_HOTSPOT"}]),
        )
        for dotted_path, value in forged_cases:
            with self.subTest(forged_path=dotted_path):
                target = receipt
                *parents, key = dotted_path.split(".")
                for parent in parents:
                    target = target[parent]
                original = target[key]
                target[key] = value
                with self.assertRaises(runner.RunnerError):
                    runner.validate_pass_receipt(receipt)
                target[key] = original

        original_inventory = receipt["pre_scan_issues"]
        receipt["pre_scan_issues"] = {
            **original_inventory,
            "total": 2,
            "result_empty": False,
            "pages": [{"page_index": 1, "page_size": 500, "total": 2}],
            "records": [{"key": "duplicate"}, {"key": "duplicate"}],
        }
        with self.assertRaisesRegex(runner.RunnerError, "duplicate record keys"):
            runner.validate_pass_receipt(receipt)
        receipt["pre_scan_issues"] = original_inventory

        receipt["scanner_metadata"].pop("observed")
        with self.assertRaisesRegex(runner.RunnerError, "scanner project/revision"):
            runner.validate_pass_receipt(receipt)

    def test_disposition_counts_are_recomputed_from_inventory(self):
        issue_before = {"records": []}
        issue_after = {"records": [{"key": "open-1", "issueStatus": "OPEN", "resolution": None}]}
        issue_dispositions = {
            "blocking_count": 0,
            "items": [{"key": "open-1", "disposition": "BLOCKING_DISPOSITION"}],
        }
        hotspot_inventory = {"records": [{"key": "hotspot-1"}]}
        hotspot_dispositions = {
            "blocking_count": 0,
            "items": [{"key": "hotspot-1", "disposition": "BLOCKING_HOTSPOT"}],
        }

        with self.assertRaisesRegex(runner.RunnerError, "issue blocking count"):
            runner.validate_issue_dispositions(issue_before, issue_after, issue_dispositions)
        with self.assertRaisesRegex(runner.RunnerError, "hotspot blocking count"):
            runner.validate_hotspot_dispositions(hotspot_inventory, hotspot_dispositions)

    def test_receipt_rejects_credential_content(self):
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(runner.RunnerError, "credential"):
                runner.write_receipt(
                    Path(temporary_directory) / "receipt.json",
                    {"failure": "scan-token"},
                    ("scan-token", "read-token"),
                )
