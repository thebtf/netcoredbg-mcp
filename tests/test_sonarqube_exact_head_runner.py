"""Focused contracts for the dependency-free SonarQube exact-head runner."""

import asyncio
import importlib.util
import json
import stat
import sys
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Any
from unittest import TestCase
from unittest.mock import patch

from jsonschema import Draft202012Validator, FormatChecker

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

    @staticmethod
    def context(primary_root: Path, scanner_root: Path) -> runner.GitContext:
        return runner.GitContext(
            scanner_root, primary_root / ".git", scanner_root / ".git", primary_root, "a" * 40
        )

    @staticmethod
    def analysis_evidence():
        from tests.test_stateless_preview_artifact import _complete_v3_exact_head_receipt

        analysis = _complete_v3_exact_head_receipt(
            "a" * 40, role="diagnostic", outcome="DIAGNOSTIC_COMPLETE", release_intent="none"
        )["analysis"]
        analysis["status"] = "INCOMPLETE"
        analysis["observations"]["current_after_measures"] = None
        analysis["observations"]["current_final"] = None
        return analysis

    @staticmethod
    def patch_wave3_transaction(patches: ExitStack) -> None:
        plan = SimpleNamespace()
        patches.enter_context(patch.object(runner, "resolve_wave2_entry", return_value={}))
        patches.enter_context(patch.object(runner, "verify_wave2_entry", return_value={}))
        patches.enter_context(patch.object(runner, "preflight_coverage_toolchain", return_value={}))
        patches.enter_context(
            patch.object(runner, "release_intent_at_head", return_value="v0.23.12")
        )
        patches.enter_context(patch.object(runner, "derive_coverage_plan", return_value=plan))
        patches.enter_context(patch.object(runner, "coverage_scanner_properties", return_value=()))
        patches.enter_context(
            patch.object(runner, "claim_coverage_run", return_value=SimpleNamespace())
        )
        patches.enter_context(patch.object(runner, "run_coverage_producer"))
        patches.enter_context(patch.object(runner, "normalize_dotnet_cobertura", return_value={}))
        patches.enter_context(patch.object(runner, "validate_coverage_reports", return_value={}))
        patches.enter_context(patch.object(runner, "assert_head_unchanged"))
        patches.enter_context(
            patch.object(
                runner,
                "capture_stateless_binary_hashes",
                return_value={"dll_sha256": "a" * 64, "pdb_sha256": "b" * 64},
            )
        )
        patches.enter_context(patch.object(runner, "cleanup_coverage_run", return_value={}))
        patches.enter_context(
            patch.object(
                runner,
                "collect_coverage_analysis_evidence",
                return_value=TestSonarqubeExactHeadRunner.analysis_evidence(),
            )
        )
        patches.enter_context(patch.object(runner, "write_diagnostic_inventory", return_value={}))

    def test_build_environment_scrubs_all_sonar_credentials(self):
        build_environment = runner.scrub_sonar_environment(
            {
                **self.credentials(),
                "SONAR_ADMIN_TOKEN": "admin-token",
                "SONAR_UNKNOWN_CREDENTIAL": "unknown-token",
                "SAFE_VALUE": "kept",
            }
        )

        self.assertEqual(build_environment, {"SAFE_VALUE": "kept"})

    def test_scrub_sonar_environment_removes_every_case_variant(self):
        build_environment = runner.scrub_sonar_environment(
            {
                "SONAR_TOKEN": "canonical-token",
                "sonar_token": "lowercase-token",
                "Sonar_Admin_Token": "mixed-admin-token",
                "sOnAr_Unknown_Credential": "mixed-unknown-token",
                "SAFE_VALUE": "kept",
            }
        )

        self.assertEqual(build_environment, {"SAFE_VALUE": "kept"})

    def test_scanner_environment_exposes_only_scan_credential(self):
        scanner_environment = runner.scanner_environment(
            {**self.credentials(), "SONAR_ADMIN_TOKEN": "admin-token"}, self.credentials()
        )

        self.assertEqual(
            {
                key: scanner_environment[key]
                for key in runner.SONAR_ENV
                if key in scanner_environment
            },
            {"SONAR_HOST_URL": "https://sonar.example.test", "SONAR_TOKEN": "scan-token"},
        )

    def test_scanner_commands_supply_token_but_render_redacted(self):
        begin = runner.scanner_begin_command(
            ["scanner"],
            Path("SonarQube.Analysis.xml"),
            "https://sonar.example.test",
            "a" * 40,
            "scan-token",
        )
        end = runner.scanner_end_command(["scanner"], "scan-token")

        self.assertIn("/d:sonar.token=scan-token", begin)
        self.assertIn("/d:sonar.token=scan-token", end)
        self.assertNotIn("scan-token", runner.redact(" ".join(begin), ("scan-token",)))

    def test_scanner_metadata_reads_dotnet_analysis_config(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            config = root / ".sonarqube" / "conf" / "SonarQubeAnalysisConfig.xml"
            config.parent.mkdir(parents=True)
            config.write_text(
                "<SonarQubeAnalysisConfig>"
                f"<SonarProjectKey>{runner.PROJECT_KEY}</SonarProjectKey>"
                "<LocalSettings>"
                f'<Property Name="sonar.scm.revision">{"a" * 40}</Property>'
                "</LocalSettings>"
                "</SonarQubeAnalysisConfig>",
                encoding="utf-8",
            )

            metadata = runner.scanner_metadata(root, "a" * 40)

        self.assertEqual(
            (metadata["project_key"], metadata["sonar_scm_revision"]),
            (runner.PROJECT_KEY, "a" * 40),
        )

    def test_scanner_worktree_dotenv_is_rejected_before_primary_root_loading(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            nested_directory = scanner_root / "candidate-controlled"
            primary_root.mkdir()
            nested_directory.mkdir(parents=True)
            (nested_directory / ".env").write_text("SONAR_TOKEN=synthetic", encoding="utf-8")

            scanner_context = self.context(primary_root, scanner_root)
            credentials = self.credentials()
            with self.assertRaisesRegex(runner.RunnerError, "in-tree .env"):
                runner.load_credentials(scanner_context, credentials)

    def test_scanner_tree_symlink_directory_is_rejected_before_dotenv_scanning(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            external_root = root / "external"
            primary_root.mkdir()
            scanner_root.mkdir()
            external_root.mkdir()
            scanner_tree_link = scanner_root / "candidate-controlled"

            try:
                scanner_tree_link.symlink_to(external_root, target_is_directory=True)
            except OSError:

                class ScannerTreeSymlinkMetadata:
                    st_mode = stat.S_IFLNK

                original_stat = Path.stat

                def nonfollowing_stat(path, *, follow_symlinks=True):
                    if path == scanner_tree_link:
                        self.assertFalse(follow_symlinks)
                        return ScannerTreeSymlinkMetadata()
                    return original_stat(path, follow_symlinks=follow_symlinks)

                with patch.object(Path, "stat", autospec=True, side_effect=nonfollowing_stat):
                    scanner_context = self.context(primary_root, scanner_root)
                    credentials = self.credentials()
                    with self.assertRaisesRegex(runner.RunnerError, "symbolic link"):
                        runner.load_credentials(scanner_context, credentials)
            else:
                scanner_context = self.context(primary_root, scanner_root)
                credentials = self.credentials()
                with self.assertRaisesRegex(runner.RunnerError, "symbolic link"):
                    runner.load_credentials(scanner_context, credentials)

    def test_scanner_tree_iterator_visits_normal_nested_paths(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            nested_directory = root / "nested"
            nested_file = nested_directory / "artifact.txt"
            nested_directory.mkdir()
            nested_file.write_text("content", encoding="utf-8")

            discovered = list(runner.iter_scanner_tree(root, "*.txt"))

        self.assertEqual(discovered, [nested_file])

    def test_scanner_tree_iterator_rejects_fake_windows_reparse_point(self):
        class FileMetadata:
            st_file_attributes = 0x0400

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            reparse_directory = root / "candidate-controlled"
            reparse_directory.mkdir()
            original_stat = Path.stat
            stat_calls = []

            def nonfollowing_stat(path, *, follow_symlinks=True):
                if path == reparse_directory:
                    stat_calls.append(follow_symlinks)
                    return FileMetadata()
                return original_stat(path, follow_symlinks=follow_symlinks)

            with patch.object(Path, "stat", autospec=True, side_effect=nonfollowing_stat):
                with self.assertRaisesRegex(runner.RunnerError, "reparse point"):
                    list(runner.iter_scanner_tree(root, "*"))

        self.assertEqual(stat_calls, [False])

    def test_primary_root_dotenv_provides_credentials(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            primary_root.mkdir()
            scanner_root.mkdir()

            with patch.object(
                runner,
                "read_verified_primary_dotenv",
                return_value=(
                    "SONAR_HOST_URL=https://sonar.example.test\n"
                    "SONAR_TOKEN=scan-token\n"
                    "SONAR_READ_TOKEN=read-token\n"
                ),
            ) as verified_reader:
                credentials = runner.load_credentials(self.context(primary_root, scanner_root), {})

        verified_reader.assert_called_once_with(primary_root / ".env")
        self.assertEqual(credentials, self.credentials())

    def test_load_credentials_uses_verified_dotenv_reader_and_preserves_failure(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            primary_root.mkdir()
            scanner_root.mkdir()
            dotenv_path = primary_root / ".env"

            with patch.object(
                runner,
                "read_verified_primary_dotenv",
                create=True,
                side_effect=runner.CredentialsUnavailableError(*runner.REQUIRED_ENV),
            ) as verified_reader:
                scanner_context = self.context(primary_root, scanner_root)
                credentials = self.credentials()
                with self.assertRaisesRegex(
                    runner.CredentialsUnavailableError, "SONAR_CREDENTIALS_UNAVAILABLE"
                ):
                    runner.load_credentials(scanner_context, credentials)

        verified_reader.assert_called_once_with(dotenv_path)

    def test_process_credentials_override_primary_root_dotenv(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            primary_root.mkdir()
            scanner_root.mkdir()

            with patch.object(
                runner,
                "read_verified_primary_dotenv",
                return_value=(
                    "SONAR_HOST_URL=https://sonar.example.test\n"
                    "SONAR_TOKEN=file-token\n"
                    "SONAR_READ_TOKEN=read-token\n"
                ),
            ) as verified_reader:
                credentials = runner.load_credentials(
                    self.context(primary_root, scanner_root), {"SONAR_TOKEN": "process-token"}
                )

        verified_reader.assert_called_once_with(primary_root / ".env")
        self.assertEqual(credentials, {**self.credentials(), "SONAR_TOKEN": "process-token"})

    def test_missing_primary_root_dotenv_or_value_is_a_named_blocker(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for content in (
                None,
                "SONAR_HOST_URL=https://sonar.example.test\nSONAR_TOKEN=scan-token\n",
            ):
                with self.subTest(content=content):
                    primary_root = root / ("missing" if content is None else "incomplete")
                    scanner_root = root / (
                        "missing-scanner" if content is None else "incomplete-scanner"
                    )
                    primary_root.mkdir()
                    scanner_root.mkdir()
                    reader = (
                        patch.object(
                            runner, "read_verified_primary_dotenv", side_effect=FileNotFoundError
                        )
                        if content is None
                        else patch.object(
                            runner, "read_verified_primary_dotenv", return_value=content
                        )
                    )

                    with reader:
                        scanner_context = self.context(primary_root, scanner_root)
                        with self.assertRaisesRegex(
                            runner.CredentialsUnavailableError, "SONAR_CREDENTIALS_UNAVAILABLE"
                        ):
                            runner.load_credentials(scanner_context, {})

    def test_admin_token_is_rejected_from_primary_root_dotenv_and_process(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for source, content, process_env in (
                ("file", "SONAR_ADMIN_TOKEN=admin-token\n", {}),
                ("process", None, {**self.credentials(), "SONAR_ADMIN_TOKEN": "admin-token"}),
            ):
                with self.subTest(source=source):
                    primary_root = root / source
                    scanner_root = root / f"{source}-scanner"
                    primary_root.mkdir()
                    scanner_root.mkdir()

                    with patch.object(
                        runner, "read_verified_primary_dotenv", return_value=content or ""
                    ):
                        scanner_context = self.context(primary_root, scanner_root)
                        with self.assertRaisesRegex(runner.RunnerError, "SONAR_ADMIN_TOKEN"):
                            runner.load_credentials(scanner_context, process_env)

    def test_load_credentials_rejects_noncanonical_case_sonar_tokens(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            primary_root.mkdir()
            scanner_root.mkdir()

            for input_name in ("sonar_token", "sonar_admin_token"):
                with self.subTest(input_name=input_name):
                    scanner_context = self.context(primary_root, scanner_root)
                    credentials = self.credentials()
                    process_env = {**credentials, input_name: "mis-cased-token"}
                    with self.assertRaises(runner.RunnerError):
                        runner.load_credentials(scanner_context, process_env)

    def test_unknown_credential_names_are_rejected(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            for source, content, process_env in (
                ("file-sonar", "SONAR_UNKNOWN_CREDENTIAL=value\n", {}),
                ("file-other", "FOO=value\n", {}),
                ("process", None, {**self.credentials(), "SONAR_UNKNOWN_CREDENTIAL": "value"}),
            ):
                with self.subTest(source=source):
                    primary_root = root / source
                    scanner_root = root / f"{source}-scanner"
                    primary_root.mkdir()
                    scanner_root.mkdir()

                    with patch.object(
                        runner, "read_verified_primary_dotenv", return_value=content or ""
                    ):
                        scanner_context = self.context(primary_root, scanner_root)
                        with self.assertRaisesRegex(runner.RunnerError, "Unknown"):
                            runner.load_credentials(scanner_context, process_env)

    def test_malformed_host_is_a_named_credential_blocker(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            primary_root.mkdir()
            scanner_root.mkdir()
            scanner_context = self.context(primary_root, scanner_root)
            credentials = self.credentials()
            process_env = {**credentials, "SONAR_HOST_URL": "sonar.example.test"}
            with self.assertRaisesRegex(
                runner.CredentialsUnavailableError,
                r"^SONAR_CREDENTIALS_UNAVAILABLE: SONAR_HOST_URL\.",
            ):
                runner.load_credentials(scanner_context, process_env)

    def test_credential_free_host_accepts_http_and_https_authorities(self):
        for supplied, expected in (
            ("https://sonar.example.test:9000", "https://sonar.example.test:9000"),
            ("http://sonar.example.test:9000", "http://sonar.example.test:9000"),
            ("https://sonar.example.test/", "https://sonar.example.test"),
            ("http://sonar.example.test/", "http://sonar.example.test"),
        ):
            with self.subTest(supplied=supplied):
                self.assertEqual(runner.credential_free_host(supplied), expected)

        for supplied in (
            "https://user@sonar.example.test",
            "https://user:password@sonar.example.test",
            "https://sonar.example.test?query=value",
            "https://sonar.example.test#fragment",
            "https://sonar.example.test:not-a-port",
            "https://sonar.example.test:65536",
            "https://sonar.example.test/path",
        ):
            with self.subTest(supplied=supplied):
                with self.assertRaisesRegex(runner.CredentialsUnavailableError, "SONAR_HOST_URL"):
                    runner.credential_free_host(supplied)

    def test_scanner_auth_failure_is_a_named_credential_blocker(self):
        with TemporaryDirectory() as temporary_directory:
            with patch.object(
                runner.subprocess,
                "run",
                return_value=runner.subprocess.CompletedProcess([], 1, "HTTP 401 unauthorized"),
            ):
                with self.assertRaisesRegex(
                    runner.CredentialsUnavailableError,
                    r"^SONAR_CREDENTIALS_UNAVAILABLE: SONAR_TOKEN\.",
                ):
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
                raise runner.urllib.error.HTTPError(
                    "https://sonar.example.test", 401, "Unauthorized", None, None
                )

        with patch.object(runner, "API_OPENER", Opener()):
            with self.assertRaises(runner.ApiHttpError) as raised:
                runner.api_json(
                    "https://sonar.example.test", "/api/ce/task", {"id": "task"}, "scan-token"
                )

        self.assertEqual(
            (raised.exception.status, raised.exception.input_name), (401, "SONAR_TOKEN")
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

        self.assertEqual(
            (solution.name, projects, standalone_projects),
            ("netcoredbg-mcp.sln", [project.resolve()], []),
        )

    def test_project_inventory_excludes_fixture_projects_outside_scan_scope(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "host" / "App.csproj"
            fixture = root / "tests" / "fixtures" / "BrokenFixture.csproj"
            venv_project = root / ".venv" / "Lib" / "site-packages" / "Owned.csproj"
            project.parent.mkdir(parents=True)
            fixture.parent.mkdir(parents=True)
            venv_project.parent.mkdir(parents=True)
            project.write_text("<Project />", encoding="utf-8")
            fixture.write_text("<Project />", encoding="utf-8")
            venv_project.write_text("<Project />", encoding="utf-8")
            (root / "netcoredbg-mcp.sln").write_text(
                'Project("{guid}") = "App", "host\\App.csproj", "{id}"\nEndProject\n',
                encoding="utf-8",
            )
            original_metadata = runner._scanner_tree_metadata

            def metadata(path):
                if ".venv" in path.relative_to(root).parts:
                    raise AssertionError("project inventory entered .venv")
                return original_metadata(path)

            with patch.object(runner, "_scanner_tree_metadata", side_effect=metadata):
                _, projects, standalone_projects = runner.project_inventory(root)

        self.assertEqual((projects, standalone_projects), ([project.resolve()], []))

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
                "analysis-1",
                "task-1",
                "task-1",
                "task-1",
                "analysis-1",
                runner.PROJECT_KEY,
                "SUCCESS",
                [("https://sonar.example.test", "/api/ce/task", {"id": "task-1"}, "scan-token")],
            ),
        )

    def test_compute_engine_timeout_preserves_deadline_and_last_state(self):
        receipt = {}
        with (
            patch.object(
                runner,
                "api_json",
                return_value={
                    "task": {
                        "id": "task-1",
                        "status": "PENDING",
                        "componentKey": runner.PROJECT_KEY,
                    }
                },
            ),
            patch.object(runner.time, "monotonic", side_effect=[0, runner.CE_TIMEOUT_SECONDS + 1]),
        ):
            with self.assertRaisesRegex(runner.RunnerError, "10-minute deadline"):
                runner.wait_for_ce_task(
                    "https://sonar.example.test", "task-1", "scan-token", receipt
                )

        self.assertEqual(
            (
                receipt["compute_engine"]["last_observed_state"],
                bool(receipt["compute_engine"]["poll_deadline_at"]),
            ),
            ("PENDING", True),
        )

    def test_compute_engine_no_response_preserves_marker_and_deadline(self):
        receipt = {}
        with patch.object(
            runner, "api_json", side_effect=runner.ApiHttpError("/api/ce/task", 503, "SONAR_TOKEN")
        ):
            with self.assertRaises(runner.ApiHttpError):
                runner.wait_for_ce_task(
                    "https://sonar.example.test", "task-1", "scan-token", receipt
                )

        self.assertEqual(
            (
                receipt["compute_engine"]["last_observed_state"],
                bool(receipt["compute_engine"]["poll_deadline_at"]),
            ),
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
            gate = runner.analysis_quality_gate(
                "https://sonar.example.test", "analysis-1", "read-token"
            )

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

    def test_analysis_quality_gate_rejects_empty_and_malformed_condition_dictionaries(self):
        valid_condition = {"metricKey": "coverage", "status": "OK", "comparator": "LT"}
        malformed_conditions = (
            ("empty", {}),
            ("missing-metric-key", {"status": "OK", "comparator": "LT"}),
            ("blank-metric-key", {**valid_condition, "metricKey": ""}),
            ("non-string-metric-key", {**valid_condition, "metricKey": 1}),
            ("unknown-status", {**valid_condition, "status": "UNKNOWN"}),
            ("non-string-status", {**valid_condition, "status": 1}),
            ("unknown-comparator", {**valid_condition, "comparator": "GTE"}),
            ("non-string-comparator", {**valid_condition, "comparator": 1}),
            ("non-string-error-threshold", {**valid_condition, "errorThreshold": 1}),
            ("non-string-warning-threshold", {**valid_condition, "warningThreshold": 1}),
            ("non-string-actual-value", {**valid_condition, "actualValue": 1}),
        )

        for case, condition in malformed_conditions:
            with self.subTest(case=case):
                with patch.object(
                    runner,
                    "api_json",
                    return_value={"projectStatus": {"status": "OK", "conditions": [condition]}},
                ):
                    with self.assertRaises(runner.RunnerError):
                        runner.analysis_quality_gate(
                            "https://sonar.example.test", "analysis-1", "read-token"
                        )

    def test_analysis_quality_gate_rejects_non_dictionary_condition(self):
        with patch.object(
            runner,
            "api_json",
            return_value={
                "projectStatus": {
                    "status": "OK",
                    "conditions": [{"status": "OK", "metricKey": "coverage"}, "malformed"],
                }
            },
        ):
            with self.assertRaisesRegex(runner.RunnerError, "conditions"):
                runner.analysis_quality_gate(
                    "https://sonar.example.test", "analysis-1", "read-token"
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
                "issues": [
                    {"key": "accepted-1", "issueStatus": "ACCEPTED", "resolution": "WONTFIX"}
                ],
            }

        with patch.object(runner, "api_json", side_effect=fake_api):
            inventory = runner.issue_inventory("https://sonar.example.test", "read-token")

        self.assertEqual(
            (inventory["query"], calls[0][2]),
            (
                {
                    "components": runner.PROJECT_KEY,
                    "issueStatuses": "OPEN,CONFIRMED,FALSE_POSITIVE,ACCEPTED,FIXED,IN_SANDBOX",
                },
                {
                    "components": runner.PROJECT_KEY,
                    "issueStatuses": "OPEN,CONFIRMED,FALSE_POSITIVE,ACCEPTED,FIXED,IN_SANDBOX",
                    "p": "1",
                    "ps": "500",
                },
            ),
        )

    def test_new_code_issue_inventory_pages_complete_all_statuses_without_legacy_date_filters(self):
        calls = []

        def fake_api(host, endpoint, parameters, token):
            calls.append((host, endpoint, parameters, token))
            page = int(parameters["p"])
            records = (
                [{"key": f"new-code-{index}"} for index in range(runner.PAGE_SIZE)]
                if page == 1
                else [{"key": "new-code-final"}]
            )
            return {
                "paging": {
                    "pageIndex": page,
                    "pageSize": runner.PAGE_SIZE,
                    "total": runner.PAGE_SIZE + 1,
                },
                "issues": records,
            }

        with patch.object(runner, "indexed_api_json", side_effect=fake_api):
            inventory = runner.new_code_issue_inventory("https://sonar.example.test", "read-token")

        expected_query = {
            "components": runner.PROJECT_KEY,
            "issueStatuses": runner.ISSUE_STATUSES,
            "inNewCodePeriod": "true",
        }
        self.assertEqual(inventory["query"], expected_query)
        self.assertEqual(
            [call[2] for call in calls],
            [
                {**expected_query, "p": "1", "ps": str(runner.PAGE_SIZE)},
                {**expected_query, "p": "2", "ps": str(runner.PAGE_SIZE)},
            ],
        )
        self.assertEqual(
            (
                inventory["endpoint"],
                inventory["total"],
                inventory["pagination_complete"],
                inventory["result_empty"],
                len(inventory["records"]),
            ),
            ("/api/issues/search", runner.PAGE_SIZE + 1, True, False, runner.PAGE_SIZE + 1),
        )
        for _, endpoint, parameters, _ in calls:
            self.assertEqual(endpoint, "/api/issues/search")
            self.assertNotIn("createdAfter", parameters)
            self.assertNotIn("sinceLeakPeriod", parameters)

    def test_error_quality_gate_defers_until_post_scan_diagnostics_are_captured(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            head = "a" * 40
            context = runner.GitContext(root, root / "common", root / "git", root, head)
            captured_receipts = []
            events = []
            full_inventory_calls = 0
            cleanup_calls = 0
            binding_calls = 0
            full_inventory = {
                "endpoint": "/api/issues/search",
                "query": {"components": runner.PROJECT_KEY, "issueStatuses": runner.ISSUE_STATUSES},
                "total": 0,
                "pages": [{"page_index": 1, "page_size": runner.PAGE_SIZE, "total": 0}],
                "pagination_complete": True,
                "result_empty": True,
                "records": [],
            }
            new_code_inventory = {
                "endpoint": "/api/issues/search",
                "query": {
                    "components": runner.PROJECT_KEY,
                    "issueStatuses": runner.ISSUE_STATUSES,
                    "inNewCodePeriod": "true",
                },
                "total": 2,
                "pages": [{"page_index": 1, "page_size": runner.PAGE_SIZE, "total": 2}],
                "pagination_complete": True,
                "result_empty": False,
                "records": [{"key": "new-code-1"}, {"key": "new-code-2"}],
            }
            error_quality_gate = {
                "analysis_id": "analysis-1",
                "status": "ERROR",
                "conditions": [
                    {
                        "metricKey": "new_violations",
                        "status": "ERROR",
                        "comparator": "GT",
                        "errorThreshold": "0",
                        "actualValue": "137",
                    }
                ],
            }
            binding = {
                "observed": True,
                "current": True,
                "analysis_id": "analysis-1",
                "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"},
                "revision": head,
            }

            def capture_receipt(_path, receipt, _secrets):
                captured_receipts.append(json.loads(json.dumps(receipt)))

            def full_issue_inventory(_host, _token):
                nonlocal full_inventory_calls
                full_inventory_calls += 1
                events.append(
                    "pre_scan_issues" if full_inventory_calls == 1 else "post_scan_issues"
                )
                return full_inventory

            def new_code_issue_inventory(_host, _token):
                events.append("new_code_issues")
                return new_code_inventory

            def current_binding(_host, _analysis_id, _head, _token):
                nonlocal binding_calls
                binding_calls += 1
                events.append(
                    (
                        "analysis_current_before_issues",
                        "analysis_current_after_issues",
                        "analysis_current_before_measures",
                        "analysis_current_after_measures",
                        "analysis_current_final",
                    )[binding_calls - 1]
                )
                return binding

            def clear_artifacts(_context, _environment):
                nonlocal cleanup_calls
                cleanup_calls += 1
                if cleanup_calls == 2:
                    events.append("generated_artifacts_removed_after_scan")
                    return ["obj"]
                return []

            def quality_gate(_host, _analysis_id, _token):
                events.append("quality_gate")
                return error_quality_gate

            def coverage_measures(*_args, **_kwargs):
                events.append("coverage_measures")
                return self.analysis_evidence()

            def hotspot_inventory(_host, _token):
                events.append("hotspots")
                return {"records": []}

            with ExitStack() as patches:
                self.patch_wave3_transaction(patches)
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
                patches.enter_context(
                    patch.object(runner, "receipt_path", return_value=root / "candidate.json")
                )
                patches.enter_context(
                    patch.object(runner, "sonar_secret_values", return_value=set())
                )
                patches.enter_context(
                    patch.object(runner, "load_credentials", return_value=self.credentials())
                )
                patches.enter_context(
                    patch.object(runner, "clear_generated_artifacts", side_effect=clear_artifacts)
                )
                patches.enter_context(
                    patch.object(runner, "strict_cleanliness", return_value={"status": "clean"})
                )
                patches.enter_context(
                    patch.object(runner, "project_key_from_xml", return_value=runner.PROJECT_KEY)
                )
                patches.enter_context(
                    patch.object(runner, "discover_scanner", return_value=["scanner"])
                )
                patches.enter_context(
                    patch.object(runner, "issue_inventory", side_effect=full_issue_inventory)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "new_code_issue_inventory",
                        side_effect=new_code_issue_inventory,
                    )
                )
                patches.enter_context(patch.object(runner, "scanner_environment", return_value={}))
                patches.enter_context(patch.object(runner, "run_process"))
                patches.enter_context(
                    patch.object(
                        runner,
                        "scanner_metadata",
                        return_value={
                            "observed": True,
                            "project_key": runner.PROJECT_KEY,
                            "sonar_scm_revision": head,
                        },
                    )
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "project_inventory",
                        return_value=(root / "netcoredbg-mcp.sln", [], []),
                    )
                )
                patches.enter_context(
                    patch.object(runner, "report_task", return_value={"ce_task_id": "task-1"})
                )
                patches.enter_context(
                    patch.object(runner, "wait_for_ce_task", return_value="analysis-1")
                )
                patches.enter_context(
                    patch.object(runner, "current_analysis_binding", side_effect=current_binding)
                )
                patches.enter_context(
                    patch.object(runner, "analysis_quality_gate", side_effect=quality_gate)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "collect_coverage_analysis_evidence",
                        side_effect=coverage_measures,
                    )
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "issue_dispositions",
                        return_value={"blocking_count": 0, "items": []},
                    )
                )
                patches.enter_context(
                    patch.object(runner, "hotspot_inventory", side_effect=hotspot_inventory)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "hotspot_dispositions",
                        return_value={"blocking_count": 0, "items": []},
                    )
                )
                patches.enter_context(patch.object(runner, "assert_head_unchanged"))
                patches.enter_context(
                    patch.object(runner, "write_receipt", side_effect=capture_receipt)
                )
                with self.assertRaisesRegex(runner.RunnerError, "quality gate is ERROR"):
                    runner.execute("candidate", "scanner")

        blocked_receipt = captured_receipts[-1]
        self.assertEqual(blocked_receipt["outcome"], "BLOCKED")
        self.assertEqual(
            events,
            [
                "pre_scan_issues",
                "analysis_current_before_issues",
                "quality_gate",
                "post_scan_issues",
                "new_code_issues",
                "analysis_current_after_issues",
                "hotspots",
                "analysis_current_before_measures",
                "coverage_measures",
                "analysis_current_after_measures",
                "generated_artifacts_removed_after_scan",
                "analysis_current_final",
            ],
        )
        self.assertEqual(
            blocked_receipt["failure"]["safe_message"],
            "Analysis-bound quality gate is ERROR; only OK passes.",
        )
        self.assertEqual(
            blocked_receipt["release_gate"],
            {
                "quality_gate_status": "ERROR",
                "blocking_issue_count": 0,
                "blocking_hotspot_count": 0,
            },
        )
        self.assertEqual(blocked_receipt["identity"]["analysis_id"], "analysis-1")
        self.assertEqual(blocked_receipt["coverage"], {})
        self.assertEqual(blocked_receipt["global_inventory"], {})
        self.assertEqual(blocked_receipt["cleanup"], {})
        self.assertEqual(new_code_inventory["total"], 2)

    def test_execute_disables_msbuild_node_reuse_for_solution_and_standalone_builds(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            head = "a" * 40
            context = runner.GitContext(root, root / "common", root / "git", root, head)
            solution = root / "netcoredbg-mcp.sln"
            standalone_project = root / "tools" / "Standalone.csproj"
            process_commands = []

            def run_process(command, **_kwargs):
                process_commands.append(command)
                if command[:2] == ["scanner", "end"]:
                    raise runner.RunnerError("stop after build command capture")

            with ExitStack() as patches:
                self.patch_wave3_transaction(patches)
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
                patches.enter_context(
                    patch.object(runner, "receipt_path", return_value=root / "candidate.json")
                )
                patches.enter_context(
                    patch.object(runner, "sonar_secret_values", return_value=set())
                )
                patches.enter_context(
                    patch.object(runner, "load_credentials", return_value=self.credentials())
                )
                patches.enter_context(
                    patch.object(runner, "clear_generated_artifacts", return_value=[])
                )
                patches.enter_context(
                    patch.object(runner, "strict_cleanliness", return_value={"status": "clean"})
                )
                patches.enter_context(
                    patch.object(runner, "project_key_from_xml", return_value=runner.PROJECT_KEY)
                )
                patches.enter_context(
                    patch.object(runner, "discover_scanner", return_value=["scanner"])
                )
                patches.enter_context(
                    patch.object(runner, "issue_inventory", return_value={"records": []})
                )
                patches.enter_context(patch.object(runner, "scanner_environment", return_value={}))
                patches.enter_context(patch.object(runner, "run_process", side_effect=run_process))
                patches.enter_context(
                    patch.object(
                        runner,
                        "scanner_metadata",
                        return_value={
                            "observed": True,
                            "project_key": runner.PROJECT_KEY,
                            "sonar_scm_revision": head,
                        },
                    )
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "project_inventory",
                        return_value=(solution, [solution], [standalone_project]),
                    )
                )
                patches.enter_context(patch.object(runner, "write_receipt"))
                with self.assertRaisesRegex(runner.RunnerError, "stop after build command capture"):
                    runner.execute("candidate", "scanner")

        self.assertEqual(
            [command for command in process_commands if command[:2] == ["dotnet", "build"]],
            [
                ["dotnet", "build", str(solution), "-nr:false"],
                ["dotnet", "build", str(standalone_project), "-nr:false"],
            ],
        )

    def test_generated_artifact_permission_error_is_typed_and_path_aware(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifact = root / "generated" / "obj"
            artifact.mkdir(parents=True)
            context = runner.GitContext(root, root, root, root, "a" * 40)

            with (
                patch.object(runner, "is_tracked", return_value=False),
                patch.object(runner.shutil, "rmtree", side_effect=PermissionError("access denied")),
                self.assertRaises(runner.RunnerError) as raised,
            ):
                runner.clear_generated_artifacts(context, {})

        failure = raised.exception
        self.assertEqual(failure.__class__.__name__, "GeneratedArtifactCleanupError")
        self.assertEqual(
            (failure.operation, failure.path, failure.error_type),
            ("rmtree", "generated/obj", "PermissionError"),
        )

    def test_post_scan_cleanup_failure_preserves_error_gate_diagnostics(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            artifact = root / "generated" / "obj"
            artifact.mkdir(parents=True)
            head = "a" * 40
            context = runner.GitContext(root, root / "common", root / "git", root, head)
            captured_receipts = []
            cleanup_calls = 0
            binding_calls = 0
            events = []
            full_inventory = {"records": [{"key": "post-scan-issue"}]}
            new_code_inventory = {"records": [{"key": "new-code-issue"}]}
            hotspots = {"records": [{"key": "hotspot-1"}]}
            error_quality_gate = {"analysis_id": "analysis-1", "status": "ERROR", "conditions": []}
            binding = {
                "observed": True,
                "current": True,
                "analysis_id": "analysis-1",
                "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"},
                "revision": head,
            }

            def capture_receipt(_path, receipt, _secrets):
                captured_receipts.append(json.loads(json.dumps(receipt)))

            def clear_artifacts(_cleanup_context, _environment):
                nonlocal cleanup_calls
                cleanup_calls += 1
                if cleanup_calls == 2:
                    events.append("generated_artifacts_removed_after_scan")
                return []

            def full_issue_inventory(_host, _token):
                events.append(
                    "pre_scan_issues"
                    if len([event for event in events if event.endswith("issues")]) == 0
                    else "post_scan_issues"
                )
                return full_inventory

            def current_binding(_host, _analysis_id, _head, _token):
                nonlocal binding_calls
                binding_calls += 1
                events.append(
                    (
                        "analysis_current_before_issues",
                        "analysis_current_after_issues",
                        "analysis_current_before_measures",
                        "analysis_current_after_measures",
                        "analysis_current_final",
                    )[binding_calls - 1]
                )
                return binding

            def quality_gate(_host, _analysis_id, _token):
                events.append("quality_gate")
                return error_quality_gate

            def new_code_issue_inventory(_host, _token):
                events.append("new_code_issues")
                return new_code_inventory

            def hotspot_inventory(_host, _token):
                events.append("hotspots")
                return hotspots

            def fail_cleanup(_plan, _producer_terminal, _claim, **_kwargs):
                events.append("post_scan_cleanup")
                return {
                    "claimed_root": ".tmp/sonarqube-coverage/fixture",
                    "producer_terminal": True,
                    "removed_paths": [],
                    "parent_removed_if_empty": False,
                    "status": "FAILED",
                    "failure": {
                        "code": "COVERAGE_CLEANUP_FAILED",
                        "message": "PermissionError",
                    },
                }

            with ExitStack() as patches:
                self.patch_wave3_transaction(patches)
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
                patches.enter_context(
                    patch.object(runner, "receipt_path", return_value=root / "candidate.json")
                )
                patches.enter_context(
                    patch.object(runner, "sonar_secret_values", return_value=set())
                )
                patches.enter_context(
                    patch.object(runner, "load_credentials", return_value=self.credentials())
                )
                patches.enter_context(
                    patch.object(runner, "clear_generated_artifacts", side_effect=clear_artifacts)
                )
                patches.enter_context(
                    patch.object(runner, "strict_cleanliness", return_value={"status": "clean"})
                )
                patches.enter_context(
                    patch.object(runner, "project_key_from_xml", return_value=runner.PROJECT_KEY)
                )
                patches.enter_context(
                    patch.object(runner, "discover_scanner", return_value=["scanner"])
                )
                patches.enter_context(
                    patch.object(runner, "issue_inventory", side_effect=full_issue_inventory)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "new_code_issue_inventory",
                        side_effect=new_code_issue_inventory,
                    )
                )
                patches.enter_context(patch.object(runner, "scanner_environment", return_value={}))
                patches.enter_context(patch.object(runner, "run_process"))
                patches.enter_context(
                    patch.object(
                        runner,
                        "scanner_metadata",
                        return_value={
                            "observed": True,
                            "project_key": runner.PROJECT_KEY,
                            "sonar_scm_revision": head,
                        },
                    )
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "project_inventory",
                        return_value=(root / "netcoredbg-mcp.sln", [], []),
                    )
                )
                patches.enter_context(
                    patch.object(runner, "report_task", return_value={"ce_task_id": "task-1"})
                )
                patches.enter_context(
                    patch.object(runner, "wait_for_ce_task", return_value="analysis-1")
                )
                patches.enter_context(
                    patch.object(runner, "current_analysis_binding", side_effect=current_binding)
                )
                patches.enter_context(
                    patch.object(runner, "analysis_quality_gate", side_effect=quality_gate)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "issue_dispositions",
                        return_value={"blocking_count": 0, "items": []},
                    )
                )
                patches.enter_context(
                    patch.object(runner, "hotspot_inventory", side_effect=hotspot_inventory)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "hotspot_dispositions",
                        return_value={"blocking_count": 0, "items": []},
                    )
                )
                patches.enter_context(patch.object(runner, "assert_head_unchanged"))
                patches.enter_context(
                    patch.object(runner, "cleanup_coverage_run", side_effect=fail_cleanup)
                )
                patches.enter_context(
                    patch.object(runner, "write_receipt", side_effect=capture_receipt)
                )
                with self.assertRaises(runner.RunnerError) as raised:
                    runner.execute("candidate", "scanner")

        blocked_receipt = captured_receipts[-1]
        self.assertNotIn("Unexpected runner failure", str(raised.exception))
        self.assertEqual(blocked_receipt["outcome"], "BLOCKED")
        self.assertEqual(
            blocked_receipt["failure"]["safe_message"],
            "Analysis-bound quality gate is ERROR; only OK passes.",
        )
        self.assertEqual(
            blocked_receipt["release_gate"],
            {
                "quality_gate_status": "ERROR",
                "blocking_issue_count": 0,
                "blocking_hotspot_count": 0,
            },
        )
        self.assertEqual(blocked_receipt["cleanup"]["status"], "FAILED")
        self.assertEqual(
            blocked_receipt["cleanup"]["failure"],
            {"code": "COVERAGE_CLEANUP_FAILED", "message": "PermissionError"},
        )
        self.assertNotIn("post_scan_head", blocked_receipt)
        self.assertEqual(
            events,
            [
                "pre_scan_issues",
                "analysis_current_before_issues",
                "quality_gate",
                "post_scan_issues",
                "new_code_issues",
                "analysis_current_after_issues",
                "hotspots",
                "analysis_current_before_measures",
                "analysis_current_after_measures",
                "post_scan_cleanup",
            ],
        )

    def test_generated_artifact_cleanup_orders_by_depth_then_normalized_path(self):
        class ArtifactPath:
            def __init__(self, relative_path, hash_value):
                self.relative_path = relative_path
                self.hash_value = hash_value
                self.name = relative_path.rsplit("/", 1)[-1]
                self.parts = tuple(relative_path.split("/"))

            def __hash__(self):
                return self.hash_value

            def __eq__(self, other):
                return isinstance(other, ArtifactPath) and self.relative_path == other.relative_path

            def exists(self):
                return True

            def is_symlink(self):
                return False

            def resolve(self):
                return self

            def relative_to(self, _root):
                return self.relative_path

            def is_dir(self):
                return True

        alpha = ArtifactPath("alpha/obj", 2)
        zulu = ArtifactPath("zulu/obj", 1)
        nested = ArtifactPath("nested/deep/obj", 0)
        removed = []
        context = runner.GitContext(object(), object(), object(), object(), "a" * 40)

        with (
            patch.object(runner, "GENERATED_ROOT_NAMES", set()),
            patch.object(runner, "iter_scanner_tree", return_value=[alpha, zulu, nested]),
            patch.object(runner, "is_tracked", return_value=False),
            patch.object(runner.shutil, "rmtree", side_effect=removed.append),
        ):
            runner.clear_generated_artifacts(context, {})

        self.assertEqual(removed, [nested, alpha, zulu])

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
            {
                "records": [
                    {
                        "key": "false-positive-1",
                        "issueStatus": "FALSE_POSITIVE",
                        "resolution": "FALSE-POSITIVE",
                    }
                ]
            },
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
            (
                kernel32.OpenProcess.argtypes,
                kernel32.OpenProcess.restype,
                kernel32.WaitForSingleObject.argtypes,
                kernel32.CloseHandle.argtypes,
            ),
            (
                [WinTypes.DWORD, WinTypes.BOOL, WinTypes.DWORD],
                WinTypes.HANDLE,
                [WinTypes.HANDLE, WinTypes.DWORD],
                [WinTypes.HANDLE],
            ),
        )

    def test_windows_handle_is_normalized_before_crt_conversion_and_invalid_value_is_rejected(self):
        import ctypes

        invalid_handle_value = ctypes.c_void_p(-1).value

        self.assertEqual(
            runner.normalize_windows_handle_for_crt(ctypes.c_void_p(123), invalid_handle_value), 123
        )
        invalid_handle = ctypes.c_void_p(-1)
        with self.assertRaises(ValueError):
            runner.normalize_windows_handle_for_crt(invalid_handle, invalid_handle_value)

    def test_close_windows_handle_if_owned_skips_sentinels_and_closes_owned_handle_once(self):
        import ctypes

        class Kernel32:
            def __init__(self):
                self.closed_handles = []

            def CloseHandle(self, handle):  # noqa: N802 - matches the Win32 API name
                self.closed_handles.append(handle)
                return True

        kernel32 = Kernel32()
        invalid_handle_value = ctypes.c_void_p(-1).value
        for handle in (None, 0, ctypes.c_void_p(), ctypes.c_void_p(-1), invalid_handle_value):
            with self.subTest(handle=handle):
                runner.close_windows_handle_if_owned(kernel32, handle, invalid_handle_value)

        self.assertEqual(kernel32.closed_handles, [])

        owned_handle = ctypes.c_void_p(123)
        runner.close_windows_handle_if_owned(kernel32, owned_handle, invalid_handle_value)

        self.assertEqual(kernel32.closed_handles, [owned_handle])

    def test_incomplete_v3_receipt_replaces_prior_pass_before_work(self):
        with TemporaryDirectory() as temporary_directory:
            receipt_path = Path(temporary_directory) / "candidate.json"
            runner.write_receipt(receipt_path, {"outcome": "PASS"}, ())
            context = runner.GitContext(
                Path(temporary_directory),
                Path(temporary_directory),
                Path(temporary_directory),
                Path(temporary_directory),
                "a" * 40,
            )
            runner.write_receipt(
                receipt_path, runner.receipt_base(context, "candidate", "v0.23.12"), ()
            )
            replacement = json.loads(receipt_path.read_text(encoding="utf-8"))

        self.assertEqual(replacement["outcome"], "BLOCKED")
        self.assertEqual(replacement["failure"]["code"], "COVERAGE_RUN_INCOMPLETE")

    def test_v3_failure_code_schema_and_validator_reject_non_strings(self):
        schema_path = (
            RUNNER_PATH.parents[1]
            / "specs/014-sonarqube-coverage-producer/contracts/exact-head-receipt-v3.schema.json"
        )
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        self.assertEqual(schema["$defs"]["failure"]["properties"]["code"]["type"], "string")

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root, root, root, "a" * 40)
            for invalid_code in (1, True):
                with self.subTest(invalid_code=invalid_code):
                    receipt = runner.receipt_base(context, "candidate", "v0.23.12")
                    receipt["failure"]["code"] = invalid_code
                    with self.assertRaisesRegex(
                        runner.RunnerError, "EXACT_HEAD_RECEIPT_V3_INVALID"
                    ):
                        runner.validate_exact_head_receipt_v3(receipt)

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
                runner.api_json(
                    "https://sonar.example.test", "/api/ce/task", {"id": "task"}, "scan-token"
                )

    def test_report_task_receipt_contains_only_non_sensitive_url_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            report_path = root / ".sonarqube" / "out" / ".sonar" / "report-task.txt"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                "\n".join(
                    (
                        f"projectKey={runner.PROJECT_KEY}",
                        "ceTaskId=task-1",
                        "serverUrl=https://sonar.example.test",
                        f"dashboardUrl=https://sonar.example.test/dashboard?id={runner.PROJECT_KEY}",
                    )
                ),
                encoding="utf-8",
            )

            task_report = runner.report_task(root, "https://sonar.example.test")

        self.assertEqual(
            task_report,
            {
                "observed": True,
                "path": ".sonarqube/out/.sonar/report-task.txt",
                "project_key": runner.PROJECT_KEY,
                "ce_task_id": "task-1",
                "server_origin_matches_configured": True,
                "dashboard_url_present": True,
            },
        )

    def test_report_task_accepts_root_slash_server_url_against_canonical_origin(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            report_path = root / ".sonarqube" / "out" / ".sonar" / "report-task.txt"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                "\n".join(
                    (
                        f"projectKey={runner.PROJECT_KEY}",
                        "ceTaskId=task-1",
                        "serverUrl=https://sonar.example.test/",
                        f"dashboardUrl=https://sonar.example.test/dashboard?id={runner.PROJECT_KEY}",
                    )
                ),
                encoding="utf-8",
            )

            task_report = runner.report_task(root, "https://sonar.example.test")

        self.assertTrue(task_report["server_origin_matches_configured"])

    def test_report_task_accepts_http_server_url_against_configured_http_origin(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            report_path = root / ".sonarqube" / "out" / ".sonar" / "report-task.txt"
            report_path.parent.mkdir(parents=True)
            report_path.write_text(
                "\n".join(
                    (
                        f"projectKey={runner.PROJECT_KEY}",
                        "ceTaskId=task-1",
                        "serverUrl=http://sonar.example.test",
                        f"dashboardUrl=http://sonar.example.test/dashboard?id={runner.PROJECT_KEY}",
                    )
                ),
                encoding="utf-8",
            )

            task_report = runner.report_task(root, "http://sonar.example.test")

        self.assertTrue(task_report["server_origin_matches_configured"])

    def test_redirect_handler_never_constructs_a_redirect_request(self):
        handler = runner.NoRedirectHandler()

        self.assertIsNone(
            handler.redirect_request(None, None, 302, "https://other.example.test", {}, None)
        )

    def test_pass_receipt_schema_rejects_missing_observed_evidence(self):
        with self.assertRaisesRegex(runner.RunnerError, "evidence schema"):
            runner.validate_pass_receipt({"schema_version": runner.RECEIPT_SCHEMA_VERSION})

    def test_pass_receipt_requires_each_observed_evidence_owner(self):
        head = "a" * 40

        def inventory(endpoint, query=None):
            query = query or (
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
            "cleanup": {"status": "PASS", "removed": []},
            "scanner_metadata": {
                "observed": True,
                "project_key": runner.PROJECT_KEY,
                "sonar_scm_revision": head,
            },
            "task_report": {
                "observed": True,
                "path": ".sonarqube/out/.sonar/report-task.txt",
                "project_key": runner.PROJECT_KEY,
                "ce_task_id": "task",
                "server_origin_matches_configured": True,
                "dashboard_url_present": True,
            },
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
            "analysis_current_before_issues": {
                "observed": True,
                "current": True,
                "analysis_id": "analysis",
                "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"},
                "revision": head,
            },
            "analysis_current_after_issues": {
                "observed": True,
                "current": True,
                "analysis_id": "analysis",
                "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"},
                "revision": head,
            },
            "analysis_current_final": {
                "observed": True,
                "current": True,
                "analysis_id": "analysis",
                "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"},
                "revision": head,
            },
            "quality_gate": {"analysis_id": "analysis", "status": "OK"},
            "pre_scan_issues": inventory("/api/issues/search"),
            "post_scan_issues": inventory("/api/issues/search"),
            "new_code_issues": inventory(
                "/api/issues/search",
                {
                    "components": runner.PROJECT_KEY,
                    "issueStatuses": runner.ISSUE_STATUSES,
                    "inNewCodePeriod": "true",
                },
            ),
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
            (
                "pre_scan_issues.query",
                {"componentKeys": runner.PROJECT_KEY, "issueStatuses": runner.ISSUE_STATUSES},
            ),
            ("pre_scan_issues.pages", []),
            ("post_scan_issues.result_empty", False),
            (
                "new_code_issues.query",
                {"components": runner.PROJECT_KEY, "issueStatuses": runner.ISSUE_STATUSES},
            ),
            ("hotspots.total", 1),
            (
                "issue_dispositions.items",
                [{"key": "forged", "disposition": "FIXED_IN_CURRENT_HEAD"}],
            ),
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
        new_code_issues = receipt.pop("new_code_issues")
        with self.assertRaises(runner.RunnerError):
            runner.validate_pass_receipt(receipt)
        receipt["new_code_issues"] = new_code_issues

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
        receipt["scanner_metadata"]["observed"] = True
        invalid_cleanup_removals = (
            ["/absolute/obj"],
            ["C:/absolute/obj"],
            ["../escaped/obj"],
            ["nested/../not-normalized"],
            ["nested/obj", "nested/obj"],
            ["zulu", "nested/deep/obj"],
        )
        for removed in invalid_cleanup_removals:
            with self.subTest(removed=removed):
                receipt["cleanup"]["removed"] = removed
                with self.assertRaises(runner.RunnerError):
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


class TestWave3CoverageProducerRedContracts(TestCase):
    """Behavior-first RED contracts for the Wave-3 coverage transaction."""

    HEAD = "a" * 40
    RUN_ID = "123e4567-e89b-12d3-a456-426614174000"
    SHA256 = "b" * 64
    DOTNET_PROJECTS = (
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

    @classmethod
    def _context(cls, root: Path):
        return runner.GitContext(root, root / "common", root / "git", root, cls.HEAD)

    @classmethod
    def _wave2_entry(cls) -> dict:
        source_blob = b'{"wave2":"tracked canonical blob"}\n'
        receipt_blob = b"Wave 2 closure receipt\n"
        return {
            "schema_version": 1,
            "wave": 2,
            "closure_status": "EXACT_CLOSED",
            "release_intent": "none",
            "tracked_relative_path": "specs/013-owner-scoped-prebuild-cleanup/wave-closure-v1.json",
            "accepted_candidate_sha": cls.HEAD,
            "closure_receipt": {
                "relative_path": "specs/013-owner-scoped-prebuild-cleanup/acceptance-receipt.md",
                "sha256": sha256(receipt_blob).hexdigest(),
            },
            "integration": {
                "kind": "pull_request_head",
                "pull_request": 289,
                "head_ref": "work/issue450-owner-scoped-cleanup",
                "head_sha": cls.HEAD,
            },
            "_canonical_source_blob": source_blob,
            "_canonical_receipt_blob": receipt_blob,
        }

    @classmethod
    def _wave2_evidence(cls, entry: dict) -> dict:
        source_blob = entry.pop("_canonical_source_blob")
        receipt_blob = entry.pop("_canonical_receipt_blob")
        return {
            "tracked": True,
            "source_blob": {"bytes": source_blob, "sha256": sha256(source_blob).hexdigest()},
            "closure_receipt_blob": {
                "bytes": receipt_blob,
                "sha256": sha256(receipt_blob).hexdigest(),
            },
            "first_party_pull_request": {
                "number": 289,
                "head_ref": "work/issue450-owner-scoped-cleanup",
                "head_sha": "c" * 40,
                "merge_commit_sha": "d" * 40,
                "merged": True,
            },
            "candidate_is_ancestor_of_pr_head": True,
            "pull_request_head_tree_sha": "e" * 40,
            "merge_tree_sha": "e" * 40,
            "artifact_blob_at_pr_head_matches": True,
            "artifact_commit_sha": "d" * 40,
            "artifact_path_history_valid": True,
            "merge_is_ancestor_of_observed_main": True,
            "observed_main_sha": "f" * 40,
        }

    @classmethod
    def _resolved_wave2_entry(cls) -> dict:
        return {
            "source_sha256": cls.SHA256,
            "accepted_candidate_sha": cls.HEAD,
            "pull_request_head_ref": "work/issue450-owner-scoped-cleanup",
            "pull_request_head_sha": "c" * 40,
            "artifact_commit_sha": "d" * 40,
            "merge_commit_sha": "d" * 40,
            "integrated_tree_sha": "e" * 40,
            "observed_main_sha": "f" * 40,
        }

    @classmethod
    def _toolchain(cls) -> dict:
        return {
            "executables": {"uv": "uv", "bash": "bash", "dotnet": "dotnet"},
            "projects": [
                {
                    "id": project_id,
                    "project": project,
                    "target_framework": "net8.0",
                    "coverlet_msbuild": None if project_id == "stateless" else "10.0.1",
                    "coverlet_private_assets": None if project_id == "stateless" else "all",
                    "test_sdk": "17.12.0",
                    "code_coverage": "17.14.1" if project_id == "stateless" else None,
                    "code_coverage_private_assets": "all" if project_id == "stateless" else None,
                    "test_platform": "vstest",
                    "mtp_active": False,
                }
                for project_id, project, _ in cls.DOTNET_PROJECTS
            ],
        }

    @staticmethod
    def _absolute(value) -> Path:
        absolute = getattr(value, "absolute", value)
        return Path(absolute() if callable(absolute) else absolute)

    @classmethod
    def _plan(cls, root: Path):
        return runner.derive_coverage_plan(cls._context(root), cls.RUN_ID)

    @staticmethod
    def _pytest_git_scratch(plan):
        scratch = plan.root / "python" / "pytest" / "test_git_fixture0"
        scratch.mkdir(parents=True)
        runner.subprocess.run(
            ["git", "-c", "core.longpaths=true", "init", "--quiet", str(scratch)],
            check=True,
            capture_output=True,
        )
        result = runner.subprocess.run(
            ["git", "-C", str(scratch), "hash-object", "-w", "--stdin"],
            input=b"owned pytest fixture object\n",
            check=True,
            capture_output=True,
        )
        object_id = result.stdout.decode().strip()
        return scratch / ".git" / "objects" / object_id[:2] / object_id[2:]

    def test_terminal_claim_cleanup_removes_pytest_links_and_readonly_git_objects(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            plan = self._plan(root)
            claim = runner.claim_coverage_run(context, plan, self._resolved_wave2_entry())
            external = root / "external"
            external.mkdir()
            sentinel = external / "sentinel"
            sentinel.write_bytes(b"external value")
            sibling = plan.root.parent / "unclaimed"
            sibling.mkdir()
            (sibling / "sentinel").write_bytes(b"unclaimed value")
            git_object = self._pytest_git_scratch(plan)
            (git_object.parents[3] / "external-link").symlink_to(external, target_is_directory=True)
            (plan.root / "file-link").symlink_to(sentinel)
            (plan.root / "dangling-link").symlink_to(external / "missing")
            if runner.os.name == "nt":
                self.assertTrue(
                    git_object.stat(follow_symlinks=False).st_file_attributes
                    & stat.FILE_ATTRIBUTE_READONLY
                )
                with self.assertRaises(PermissionError) as denied:
                    git_object.unlink()
                print("CONTROLLED_PERMISSION_ERROR", denied.exception.filename)
            with self.assertRaisesRegex(runner.RunnerError, "symbolic link"):
                runner.clear_generated_artifacts(context, {})
            cleanup = runner.cleanup_coverage_run(plan, True, claim)
            if runner.os.name == "nt":
                self.assertEqual(cleanup["status"], "OK", cleanup)
                self.assertFalse(plan.root.exists())
            else:
                self.assertEqual(cleanup["status"], "FAILED", cleanup)
                self.assertTrue(plan.root.exists())
                self.assertTrue(git_object.exists())
            self.assertEqual(sentinel.read_bytes(), b"external value")
            self.assertEqual((sibling / "sentinel").read_bytes(), b"unclaimed value")
            self.assertFalse(cleanup["parent_removed_if_empty"])

    def test_cleanup_preserves_active_and_changed_marker_claims(self):
        for terminal, corrupt_marker in ((False, False), (True, True)):
            with self.subTest(terminal=terminal), TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                plan = self._plan(root)
                claim = runner.claim_coverage_run(
                    self._context(root), plan, self._resolved_wave2_entry()
                )
                if corrupt_marker:
                    plan.marker.write_text("{}", encoding="utf-8")
                cleanup = runner.cleanup_coverage_run(plan, terminal, claim)
                self.assertEqual(cleanup["status"], "FAILED")
                self.assertTrue(plan.root.exists())

    def test_cleanup_refuses_unclaimed_redirected_or_replaced_roots(self):
        cases = (
            "unclaimed",
            "missing-marker",
            "resolved-entry",
            "replaced-root",
            "root-link",
            "ancestor-link",
            "forged-plan",
            "forged-marker",
            "marker-link",
        )
        for case in cases:
            with self.subTest(case=case), TemporaryDirectory() as temporary_directory:
                repository = Path(temporary_directory) / "repository"
                repository.mkdir()
                plan = self._plan(repository)
                claim = runner.claim_coverage_run(
                    self._context(repository), plan, self._resolved_wave2_entry()
                )
                external = repository.parent / "external"
                external.mkdir()
                sentinel = external / "sentinel"
                sentinel.write_bytes(b"external value")
                marker_bytes = plan.marker.read_bytes()
                if case == "unclaimed":
                    claim = None
                elif case == "missing-marker":
                    plan.marker.unlink()
                elif case == "resolved-entry":
                    plan.resolved_wave2_entry.write_bytes(b"{}")
                elif case == "replaced-root":
                    plan.root.rename(plan.root.with_name("preserved-original"))
                    plan.root.mkdir()
                    plan.marker.write_bytes(marker_bytes)
                    plan.resolved_wave2_entry.write_bytes(b"{}")
                elif case == "root-link":
                    plan.root.rename(plan.root.with_name("preserved-original"))
                    plan.root.symlink_to(external, target_is_directory=True)
                elif case == "ancestor-link":
                    parent = plan.root.parent
                    parent.rename(parent.with_name("preserved-original"))
                    parent.symlink_to(external, target_is_directory=True)
                elif case == "forged-plan":
                    plan = replace(plan, root=repository / "valuable")
                    plan.root.mkdir()
                elif case == "forged-marker":
                    plan.marker.write_bytes(b"{}")
                    claim = replace(claim, marker_sha256=sha256(b"{}").hexdigest())
                elif case == "marker-link":
                    plan.marker.unlink()
                    plan.marker.symlink_to(sentinel)
                result = runner.cleanup_coverage_run(plan, True, claim)
                self.assertEqual(result["status"], "FAILED", (case, result))
                self.assertEqual(result["removed_paths"], [])
                self.assertEqual(sentinel.read_bytes(), b"external value")

    def test_readonly_cleanup_refuses_to_change_shared_external_hardlink(self):
        if runner.os.name != "nt":
            self.skipTest("Windows read-only file deletion")
        with TemporaryDirectory() as temporary_directory:
            repository = Path(temporary_directory) / "repository"
            repository.mkdir()
            plan = self._plan(repository)
            claim = runner.claim_coverage_run(
                self._context(repository), plan, self._resolved_wave2_entry()
            )
            git_object = self._pytest_git_scratch(plan)
            external = repository.parent / "external-object"
            runner.os.link(git_object, external)
            metadata = external.stat(follow_symlinks=False)
            original = external.read_bytes()
            result = runner.cleanup_coverage_run(plan, True, claim)
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(external.read_bytes(), original)
            self.assertEqual(
                external.stat(follow_symlinks=False).st_file_attributes, metadata.st_file_attributes
            )

    def test_readonly_cleanup_without_native_handle_capability_remains_blocked(self):
        if runner.os.name != "nt":
            self.skipTest("Windows read-only file deletion")
        import ctypes

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan = self._plan(root)
            claim = runner.claim_coverage_run(
                self._context(root), plan, self._resolved_wave2_entry()
            )
            git_object = self._pytest_git_scratch(plan)
            attributes = git_object.stat(follow_symlinks=False).st_file_attributes
            original = git_object.read_bytes()
            with patch.object(
                ctypes, "WinDLL", side_effect=NotImplementedError("native handle API unavailable")
            ):
                cleanup = runner.cleanup_coverage_run(plan, True, claim)
            self.assertEqual(cleanup["status"], "FAILED")
            self.assertEqual(
                cleanup["failure"],
                {"code": "COVERAGE_CLEANUP_FAILED", "message": "NotImplementedError"},
            )
            self.assertTrue(plan.root.exists())
            self.assertEqual(git_object.read_bytes(), original)
            self.assertEqual(git_object.stat(follow_symlinks=False).st_file_attributes, attributes)

    def test_post_end_failures_preserve_durable_evidence_and_exact_cleanup_authority(self):
        for failure in ("guard", "cleanup", "end"):
            if failure == "cleanup" and runner.os.name != "nt":
                continue
            with self.subTest(failure=failure), TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                context = self._context(root)
                plan = self._plan(root)
                receipts = []
                sentinel = root / "external-value"
                sentinel.write_bytes(b"preserved")
                external_object = root / "external-object"
                coverage = {"run_id": plan.run_id, "observed_reports": ["python", "dotnet"]}
                analysis = TestSonarqubeExactHeadRunner.analysis_evidence()
                claim_coverage_run = runner.claim_coverage_run
                cleanup_coverage_run = runner.cleanup_coverage_run
                write_receipt = runner.write_receipt

                def capture_receipt(path, receipt, secrets):
                    write_receipt(path, receipt, secrets)
                    receipts.append((deepcopy(receipt), plan.root.exists()))

                def produce(_plan, _environment):
                    git_object = self._pytest_git_scratch(plan)
                    (plan.root / "pytest-current").symlink_to(
                        plan.root / "python", target_is_directory=True
                    )
                    if failure == "guard":
                        (root / "unknown-link").symlink_to(sentinel)
                    elif failure == "cleanup":
                        runner.os.link(git_object, external_object)

                with ExitStack() as patches:
                    TestSonarqubeExactHeadRunner.patch_wave3_transaction(patches)
                    values = {
                        "process_environment": {},
                        "git_context": context,
                        "receipt_path": root / "receipt.json",
                        "sonar_secret_values": set(),
                        "load_credentials": TestSonarqubeExactHeadRunner.credentials(),
                        "derive_coverage_plan": plan,
                        "verify_wave2_entry": self._resolved_wave2_entry(),
                        "strict_cleanliness": {},
                        "project_key_from_xml": runner.PROJECT_KEY,
                        "discover_scanner": ["scanner"],
                        "scanner_environment": {},
                        "project_inventory": (root / "solution.sln", [], []),
                        "issue_inventory": {"records": []},
                        "new_code_issue_inventory": {},
                        "report_task": {"ce_task_id": "task-1"},
                        "wait_for_ce_task": "analysis-1",
                        "current_analysis_binding": {
                            "revision": self.HEAD,
                            "analysis_id": "analysis-1",
                        },
                        "analysis_quality_gate": {"status": "OK"},
                        "issue_dispositions": {"blocking_count": 0},
                        "hotspot_inventory": {},
                        "hotspot_dispositions": {"blocking_count": 0},
                        "validate_coverage_reports": coverage,
                        "validate_dotnet_cobertura_inputs": [],
                        "collect_coverage_analysis_evidence": analysis,
                    }
                    for name, value in values.items():
                        patches.enter_context(patch.object(runner, name, return_value=value))
                    patches.enter_context(
                        patch.object(runner, "project_lock", return_value=nullcontext())
                    )
                    patches.enter_context(patch.object(runner, "run_process"))
                    patches.enter_context(
                        patch.object(runner, "prepare_worktree_python_environment")
                    )
                    patches.enter_context(
                        patch.object(
                            runner,
                            "scanner_metadata",
                            side_effect=runner.RunnerError(
                                "COVERAGE_METADATA_INVALID: injected post-end failure"
                            )
                            if failure == "end"
                            else None,
                        )
                    )
                    patches.enter_context(patch.object(runner, "is_tracked", return_value=False))
                    patches.enter_context(
                        patch.object(runner, "run_coverage_producer", side_effect=produce)
                    )
                    patches.enter_context(
                        patch.object(runner, "claim_coverage_run", wraps=claim_coverage_run)
                    )
                    patches.enter_context(
                        patch.object(runner, "cleanup_coverage_run", wraps=cleanup_coverage_run)
                    )
                    patches.enter_context(
                        patch.object(runner, "write_receipt", side_effect=capture_receipt)
                    )
                    with self.assertRaises(runner.RunnerError):
                        runner.execute("diagnostic", "scanner")
                blocked = json.loads((root / "receipt.json").read_bytes())
                self.assertEqual(blocked, receipts[-1][0])
                self.assertEqual(blocked["outcome"], "BLOCKED")
                self.assertEqual(blocked["coverage"], coverage)
                self.assertTrue(
                    any(item["coverage"] == coverage and exists for item, exists in receipts)
                )
                self.assertEqual(sentinel.read_bytes(), b"preserved")
                if failure == "cleanup":
                    self.assertEqual(blocked["cleanup"]["status"], "FAILED")
                    self.assertEqual(blocked["failure"]["code"], "COVERAGE_CLEANUP_FAILED")
                    self.assertTrue(plan.root.exists())
                    self.assertTrue(
                        external_object.stat().st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
                    )
                elif runner.os.name != "nt":
                    self.assertEqual(blocked["cleanup"]["status"], "FAILED")
                    self.assertTrue(plan.root.exists())
                else:
                    self.assertEqual(blocked["cleanup"]["status"], "OK")
                    self.assertFalse(plan.root.exists())
                if failure == "end":
                    self.assertIsNone(blocked["analysis"])
                    self.assertIsNone(blocked["identity"]["analysis_id"])
                    self.assertEqual(blocked["failure"]["code"], "COVERAGE_METADATA_INVALID")
                else:
                    self.assertEqual(blocked["identity"]["analysis_id"], "analysis-1")
                    self.assertEqual(blocked["analysis"], analysis)
                    self.assertTrue(
                        any(item["analysis"] == analysis and exists for item, exists in receipts)
                    )
                    self.assertEqual(blocked["global_inventory"], {})
                if failure == "guard":
                    self.assertTrue((root / "unknown-link").is_symlink())

    def test_posix_claim_cleanup_blocks_before_any_removal(self):
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            plan = self._plan(root)
            claim = runner.claim_coverage_run(
                self._context(root), plan, self._resolved_wave2_entry()
            )
            marker = plan.marker.read_bytes()
            with (
                patch.object(runner.os, "name", "posix"),
                patch.object(
                    runner.shutil,
                    "rmtree",
                    side_effect=AssertionError("unclaimed pathname removal"),
                ),
            ):
                result = runner.cleanup_coverage_run(plan, True, claim)
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["failure"]["message"], "NotImplementedError")
            self.assertEqual(result["removed_paths"], [])
            self.assertEqual(plan.marker.read_bytes(), marker)

    def test_cleanup_native_failure_schema_and_consumer_preserve_blocked_status(self):
        from tests.test_stateless_preview_artifact import _complete_v3_exact_head_receipt

        schema_path = (
            RUNNER_PATH.parents[1]
            / "specs/014-sonarqube-coverage-producer/contracts/exact-head-receipt-v3.schema.json"
        )
        schema = Draft202012Validator(json.loads(schema_path.read_bytes()))
        receipt = _complete_v3_exact_head_receipt(
            self.HEAD, role="diagnostic", outcome="DIAGNOSTIC_COMPLETE", release_intent="none"
        )
        receipt["outcome"] = "BLOCKED"
        receipt["failure"] = runner._blocked_failure(
            "ANALYSIS_BOUND", runner.RunnerError("COVERAGE_CLEANUP_FAILED: controlled failure")
        )
        receipt["cleanup"]["status"] = "FAILED"
        receipt["cleanup"]["removed_paths"] = []
        native = {
            "operation": "SetFileInformationByHandle(FileDispositionInfoEx)",
            "stage": "DISPOSITION",
            "entry": "python/pytest/real-git/.git/objects/ab/object",
            "winerror": 5,
            "errno": 13,
        }
        receipt["cleanup"]["failure"] = {
            "code": "COVERAGE_CLEANUP_FAILED",
            "message": "PermissionError",
            "native": native,
        }
        for entry in (native["entry"], ".", "@parent", "@ancestor/3", None):
            with self.subTest(entry=entry):
                native["entry"] = entry
                schema.validate(receipt)
                runner.validate_exact_head_receipt_v3(receipt)
        for field, value in (
            ("entry", "D:/private/provider-secret"),
            ("entry", "/private/provider-secret"),
            ("entry", "../external"),
            ("entry", "python\\provider-secret"),
            ("entry", "python\nprovider-secret"),
            ("operation", "unknown-provider-content"),
            ("operation", []),
            ("stage", []),
            ("winerror", True),
            ("errno", "13"),
            ("unexpected", "provider-secret"),
        ):
            with self.subTest(field=field, value=value):
                invalid = deepcopy(receipt)
                invalid["cleanup"]["failure"]["native"][field] = value
                self.assertFalse(schema.is_valid(invalid))
                with self.assertRaises(runner.RunnerError):
                    runner.validate_exact_head_receipt_v3(invalid)
        receipt["outcome"] = "DIAGNOSTIC_COMPLETE"
        receipt["failure"] = None
        self.assertFalse(schema.is_valid(receipt))
        with self.assertRaises(runner.RunnerError):
            runner.validate_exact_head_receipt_v3(receipt)

    def test_incomplete_analysis_is_typed_and_cannot_authorize_completion(self):
        from tests.test_stateless_preview_artifact import _complete_v3_exact_head_receipt

        schema_path = (
            RUNNER_PATH.parents[1]
            / "specs/014-sonarqube-coverage-producer/contracts/exact-head-receipt-v3.schema.json"
        )
        schema = Draft202012Validator(json.loads(schema_path.read_bytes()))
        complete = _complete_v3_exact_head_receipt(
            self.HEAD, role="diagnostic", outcome="DIAGNOSTIC_COMPLETE", release_intent="none"
        )
        partial = deepcopy(complete)
        partial["analysis"] = TestSonarqubeExactHeadRunner.analysis_evidence()
        with self.assertRaises(runner.RunnerError):
            runner.validate_exact_head_receipt_v3(partial)
        self.assertFalse(schema.is_valid(partial))
        for role in ("candidate", "post-merge"):
            invalid = _complete_v3_exact_head_receipt(
                self.HEAD, role=role, outcome="PASS", release_intent="v0.23.12"
            )
            invalid["analysis"] = TestSonarqubeExactHeadRunner.analysis_evidence()
            with self.assertRaises(runner.RunnerError):
                runner.validate_exact_head_receipt_v3(invalid)
            self.assertFalse(schema.is_valid(invalid))
        partial["outcome"] = "BLOCKED"
        partial["failure"] = runner._blocked_failure(
            "ANALYSIS_BOUND", runner.RunnerError("COVERAGE_CLEANUP_FAILED: controlled failure")
        )
        runner.validate_exact_head_receipt_v3(partial)
        schema.validate(partial)
        for field, value in (
            ("current_final", True),
            ("current_after_measures", False),
            ("current_after_measures", 1),
        ):
            with self.subTest(field=field, value=value):
                invalid = deepcopy(partial)
                invalid["analysis"]["observations"][field] = value
                with self.assertRaises(runner.RunnerError):
                    runner.validate_exact_head_receipt_v3(invalid)
                self.assertFalse(schema.is_valid(invalid))
        for field in (
            "submitted",
            "current_before_measures",
            "current_after_measures",
            "current_final",
        ):
            invalid = deepcopy(partial)
            del invalid["analysis"]["observations"][field]
            with self.assertRaises(runner.RunnerError):
                runner.validate_exact_head_receipt_v3(invalid)
            self.assertFalse(schema.is_valid(invalid))
        invalid = deepcopy(partial)
        invalid["identity"]["analysis_id"] = None
        with self.assertRaises(runner.RunnerError):
            runner.validate_exact_head_receipt_v3(invalid)
        self.assertFalse(schema.is_valid(invalid))

    def test_cleanup_security_transaction_reaches_only_actual_analysis_bookends(self):
        if runner.os.name != "nt":
            self.skipTest("Windows native transaction proof")
        import ctypes
        from ctypes import wintypes
        from tests.test_stateless_preview_artifact import _complete_v3_exact_head_receipt

        schema_path = (
            RUNNER_PATH.parents[1]
            / "specs/014-sonarqube-coverage-producer/contracts/exact-head-receipt-v3.schema.json"
        )
        validator = Draft202012Validator(json.loads(schema_path.read_bytes()))
        for failure in ("interrupt", "finalizer-interrupt", "cleanup", "after-measures", None):
            with self.subTest(failure=failure), TemporaryDirectory() as temporary:
                root = Path(temporary).resolve()
                context = self._context(root)
                fixture = _complete_v3_exact_head_receipt(
                    self.HEAD,
                    role="diagnostic",
                    outcome="DIAGNOSTIC_COMPLETE",
                    release_intent="none",
                )
                plan = runner.derive_coverage_plan(context, fixture["coverage"]["run_id"])
                analysis_id = fixture["identity"]["analysis_id"]
                events = []
                interrupt = KeyboardInterrupt("controlled native transaction interruption")
                cleanup_calls = []
                receipts = []
                native_cleanup = runner.cleanup_coverage_run
                claim_run = runner.claim_coverage_run
                persist = runner.write_receipt
                collect = runner.collect_coverage_analysis_evidence
                leaf = None

                def produce(*_args):
                    nonlocal leaf
                    leaf = self._pytest_git_scratch(plan)
                    if failure == "cleanup":
                        runner.os.link(leaf, root / "external-alias")

                def cleanup(*args, **kwargs):
                    cleanup_calls.append(True)
                    return native_cleanup(*args, **kwargs)

                def capture(path, receipt, secrets):
                    persist(path, receipt, secrets)
                    receipts.append(deepcopy(receipt))

                def binding(*_args):
                    after = "dotnet-components" in events
                    events.append("binding-after" if after else "binding-before")
                    if after and failure in {"after-measures", "finalizer-interrupt"}:
                        raise runner.RunnerError(
                            "COVERAGE_ANALYSIS_MISMATCH: controlled supersession after reads"
                        )
                    return {"revision": self.HEAD, "analysis_id": analysis_id}

                def api(_host, endpoint, parameters, _token):
                    if endpoint == "/api/measures/component":
                        events.append("aggregate")
                        return {
                            "component": {
                                "measures": [
                                    {"metric": name, "value": str(value)}
                                    for name, value in fixture["analysis"]["aggregate"].items()
                                ]
                            }
                        }
                    events.append(
                        "python-components"
                        if "python-components" not in events
                        else "dotnet-components"
                    )
                    paths = [
                        path
                        for report in fixture["coverage"]["final_reports"]
                        for path in report["source_paths"]
                    ]
                    return {
                        "paging": {"total": len(paths)},
                        "components": [
                            {
                                "path": path,
                                "measures": [
                                    {"metric": "lines_to_cover", "value": "10"},
                                    {"metric": "uncovered_lines", "value": "2"},
                                    {"metric": "conditions_to_cover", "value": "4"},
                                    {"metric": "uncovered_conditions", "value": "1"},
                                ],
                            }
                            for path in paths
                        ],
                    }

                native_dll = ctypes.WinDLL
                kernel = native_dll("kernel32", use_last_error=True)
                kernel.SetFileInformationByHandle.argtypes = [
                    wintypes.HANDLE,
                    ctypes.c_int,
                    ctypes.c_void_p,
                    wintypes.DWORD,
                ]
                kernel.SetFileInformationByHandle.restype = wintypes.BOOL
                interrupted = False

                def setter(handle, kind, data, size):
                    nonlocal interrupted
                    result = kernel.SetFileInformationByHandle(handle, kind, data, size)
                    if (
                        failure in {"interrupt", "finalizer-interrupt"}
                        and not interrupted
                        and kind == 21
                        and ctypes.cast(data, ctypes.POINTER(wintypes.DWORD))[0] & 1
                    ):
                        interrupted = True
                        raise interrupt
                    return result

                proxy = SimpleNamespace(
                    **{
                        name: getattr(kernel, name)
                        for name in (
                            "CreateFileW",
                            "GetFileType",
                            "GetFileInformationByHandle",
                            "GetFileInformationByHandleEx",
                            "CloseHandle",
                        )
                    },
                    SetFileInformationByHandle=setter,
                )
                with ExitStack() as patches:
                    TestSonarqubeExactHeadRunner.patch_wave3_transaction(patches)
                    values = {
                        "process_environment": {},
                        "git_context": context,
                        "receipt_path": root / "receipt.json",
                        "sonar_secret_values": set(),
                        "load_credentials": TestSonarqubeExactHeadRunner.credentials(),
                        "derive_coverage_plan": plan,
                        "verify_wave2_entry": self._resolved_wave2_entry(),
                        "strict_cleanliness": {},
                        "project_key_from_xml": runner.PROJECT_KEY,
                        "discover_scanner": ["scanner"],
                        "scanner_environment": {},
                        "project_inventory": (root / "solution.sln", [], []),
                        "issue_inventory": {"records": []},
                        "new_code_issue_inventory": {},
                        "report_task": {"ce_task_id": "task"},
                        "wait_for_ce_task": analysis_id,
                        "issue_dispositions": {"blocking_count": 0},
                        "hotspot_inventory": {},
                        "hotspot_dispositions": {"blocking_count": 0},
                        "validate_coverage_reports": fixture["coverage"],
                        "validate_dotnet_cobertura_inputs": [],
                        "write_diagnostic_inventory": fixture["global_inventory"],
                        "analysis_quality_gate": {
                            "status": "OK",
                            "conditions": [
                                {
                                    "metricKey": "new_coverage",
                                    "status": "OK",
                                    "errorThreshold": "80",
                                    "actualValue": "85",
                                }
                            ],
                        },
                    }
                    for name, value in values.items():
                        patches.enter_context(patch.object(runner, name, return_value=value))
                    patches.enter_context(
                        patch.object(
                            runner.uuid, "uuid4", return_value=runner.uuid.UUID(plan.run_id)
                        )
                    )
                    patches.enter_context(
                        patch.object(runner, "project_lock", return_value=nullcontext())
                    )
                    for name in (
                        "run_process",
                        "prepare_worktree_python_environment",
                        "scanner_metadata",
                        "clear_generated_artifacts",
                    ):
                        patches.enter_context(patch.object(runner, name))
                    patches.enter_context(
                        patch.object(runner, "claim_coverage_run", wraps=claim_run)
                    )
                    patches.enter_context(
                        patch.object(runner, "run_coverage_producer", side_effect=produce)
                    )
                    patches.enter_context(
                        patch.object(
                            runner,
                            "collect_coverage_analysis_evidence",
                            wraps=collect,
                        )
                    )
                    patches.enter_context(
                        patch.object(runner, "current_analysis_binding", side_effect=binding)
                    )
                    patches.enter_context(patch.object(runner, "api_json", side_effect=api))
                    patches.enter_context(
                        patch.object(runner, "cleanup_coverage_run", side_effect=cleanup)
                    )
                    patches.enter_context(
                        patch.object(
                            ctypes,
                            "WinDLL",
                            side_effect=lambda name, **kwargs: proxy
                            if name == "kernel32"
                            else native_dll(name, **kwargs),
                        )
                    )
                    patches.enter_context(
                        patch.object(runner, "write_receipt", side_effect=capture)
                    )
                    if failure in {"interrupt", "finalizer-interrupt"}:
                        with self.assertRaises(KeyboardInterrupt) as caught:
                            runner.execute("diagnostic", "scanner")
                        self.assertIs(caught.exception, interrupt)
                    elif failure is not None:
                        with self.assertRaises(runner.RunnerError):
                            runner.execute("diagnostic", "scanner")
                    else:
                        runner.execute("diagnostic", "scanner")
                result = json.loads((root / "receipt.json").read_bytes())
                validator.validate(result)
                runner.validate_exact_head_receipt_v3(result)
                self.assertEqual(cleanup_calls, [True])
                self.assertEqual(result["coverage"], fixture["coverage"])
                self.assertEqual(result["analysis"]["aggregate"], fixture["analysis"]["aggregate"])
                observations = result["analysis"]["observations"]
                if failure is None:
                    self.assertEqual(result["outcome"], "DIAGNOSTIC_COMPLETE")
                    self.assertTrue(all(value is True for value in observations.values()))
                else:
                    self.assertEqual(result["outcome"], "BLOCKED")
                    self.assertEqual(result["analysis"]["status"], "INCOMPLETE")
                    self.assertIsNone(observations["current_final"])
                    self.assertIs(
                        observations["current_after_measures"],
                        None if failure in {"after-measures", "finalizer-interrupt"} else True,
                    )
                    if failure in {"interrupt", "finalizer-interrupt", "cleanup"}:
                        self.assertEqual(result["cleanup"]["status"], "FAILED")
                        self.assertEqual(
                            result["failure"]["code"],
                            "COVERAGE_ANALYSIS_MISMATCH"
                            if failure == "finalizer-interrupt"
                            else "COVERAGE_CLEANUP_FAILED",
                        )
                        self.assertTrue(plan.root.exists())
                self.assertGreater(events.index("binding-after"), events.index("dotnet-components"))
                print("CONTROLLED_TRANSACTION_RECEIPT", failure, json.dumps(result, sort_keys=True))

    @staticmethod
    def _cobertura(
        filenames,
        *,
        lines_valid=2,
        branches_valid=2,
        sources=(".",),
        line_xml: str | None = None,
    ) -> str:
        line = line_xml or (
            '<line number="1" hits="1" branch="true" condition-coverage="50% (1/2)"/>'
        )
        classes = "".join(
            f'<class name="module" filename="{filename}"><methods/><lines>{line}</lines></class>'
            for filename in filenames
        )
        source_xml = "".join(f"<source>{source}</source>" for source in sources)
        return (
            f'<coverage lines-valid="{lines_valid}" lines-covered="1" '
            f'branches-valid="{branches_valid}" branches-covered="1">'
            f'<sources>{source_xml}</sources><packages><package name="coverage">'
            f"<classes>{classes}</classes></package></packages></coverage>"
        )

    @staticmethod
    def _write_source(root: Path, relative_path: str) -> Path:
        source = root / relative_path
        source.parent.mkdir(parents=True, exist_ok=True)
        source.write_text("# source\n", encoding="utf-8")
        return source

    def _transaction_events(
        self,
        root: Path,
        events: list[str],
        failing_step: str | None = None,
        producer_terminals: list[bool] | None = None,
        generated_cleanup_calls: list[str] | None = None,
        real_cleanup: bool = False,
        receipts: list[dict[str, Any]] | None = None,
        producer_error: Exception | None = None,
    ) -> None:
        context = self._context(root)
        plan = SimpleNamespace(repository_root=root, root=root / ".tmp/sonarqube-coverage/claimed")
        cleanup_coverage_run = runner.cleanup_coverage_run
        claim = SimpleNamespace()

        def step(name, result=None):
            def invoke(*_args, **_kwargs):
                events.append(name)
                if name == "produce" and producer_error is not None:
                    raise producer_error
                if failing_step == name:
                    raise runner.RunnerError(f"injected {name} failure")
                return result

            return invoke

        def process(command, **_kwargs):
            if command[:2] == ["scanner", "begin"]:
                return step("begin")()
            if command[:2] == ["dotnet", "build"]:
                return step("build")()
            if command[:2] == ["scanner", "end"]:
                events.append("end")
                if failing_step == "end":
                    raise runner.RunnerError("injected end failure")
                raise runner.RunnerError("stop after transaction event capture")

        def cleanup(_plan, producer_terminal, _claim, **kwargs):
            if producer_terminals is not None:
                producer_terminals.append(producer_terminal)
            if real_cleanup:
                return cleanup_coverage_run(_plan, producer_terminal, _claim, **kwargs)
            return {}

        def clear_generated(_context, _environment):
            if generated_cleanup_calls is not None:
                generated_cleanup_calls.append("clear")
            return []

        with ExitStack() as patches:
            patches.enter_context(patch.object(runner, "process_environment", return_value={}))
            patches.enter_context(patch.object(runner, "scrub_sonar_environment", return_value={}))
            patches.enter_context(patch.object(runner, "git_context", return_value=context))
            patches.enter_context(
                patch.object(runner, "release_intent_at_head", return_value="v0.23.12")
            )
            patches.enter_context(
                patch.object(runner, "receipt_path", return_value=root / "receipt.json")
            )
            patches.enter_context(patch.object(runner, "sonar_secret_values", return_value=set()))
            patches.enter_context(patch.object(runner, "project_lock", return_value=nullcontext()))
            patches.enter_context(
                patch.object(
                    runner,
                    "write_receipt",
                    side_effect=lambda _path, receipt, _secrets: (
                        receipts.append(deepcopy(receipt)) if receipts is not None else None
                    ),
                )
            )
            patches.enter_context(
                patch.object(
                    runner,
                    "load_credentials",
                    return_value=TestSonarqubeExactHeadRunner.credentials(),
                )
            )
            patches.enter_context(
                patch.object(runner, "clear_generated_artifacts", side_effect=clear_generated)
            )
            patches.enter_context(
                patch.object(runner, "strict_cleanliness", return_value={"status": "clean"})
            )
            patches.enter_context(
                patch.object(runner, "project_key_from_xml", return_value=runner.PROJECT_KEY)
            )
            patches.enter_context(
                patch.object(runner, "discover_scanner", return_value=["scanner"])
            )
            patches.enter_context(
                patch.object(runner, "issue_inventory", return_value={"records": []})
            )
            patches.enter_context(patch.object(runner, "scanner_environment", return_value={}))
            patches.enter_context(
                patch.object(runner, "scanner_begin_command", return_value=["scanner", "begin"])
            )
            patches.enter_context(
                patch.object(runner, "scanner_end_command", return_value=["scanner", "end"])
            )
            patches.enter_context(
                patch.object(
                    runner, "project_inventory", return_value=(root / "netcoredbg-mcp.sln", [], [])
                )
            )
            patches.enter_context(patch.object(runner, "run_process", side_effect=process))
            patches.enter_context(
                patch.object(runner, "resolve_wave2_entry", return_value=self._wave2_entry())
            )
            patches.enter_context(
                patch.object(
                    runner,
                    "verify_wave2_entry",
                    side_effect=step("entry", self._resolved_wave2_entry()),
                )
            )
            patches.enter_context(
                patch.object(
                    runner,
                    "preflight_coverage_toolchain",
                    side_effect=step("preflight", self._toolchain()),
                )
            )
            patches.enter_context(patch.object(runner, "derive_coverage_plan", return_value=plan))
            patches.enter_context(
                patch.object(runner, "coverage_scanner_properties", return_value=())
            )
            patches.enter_context(
                patch.object(runner, "claim_coverage_run", side_effect=step("claim", claim))
            )
            patches.enter_context(
                patch.object(
                    runner,
                    "prepare_worktree_python_environment",
                    side_effect=step("python-env"),
                )
            )
            patches.enter_context(
                patch.object(runner, "run_coverage_producer", side_effect=step("produce"))
            )
            patches.enter_context(
                patch.object(
                    runner, "normalize_dotnet_cobertura", side_effect=step("normalize", {})
                )
            )
            patches.enter_context(
                patch.object(runner, "validate_coverage_reports", side_effect=step("validate", {}))
            )
            patches.enter_context(
                patch.object(
                    runner,
                    "capture_stateless_binary_hashes",
                    return_value={"dll_sha256": "a" * 64, "pdb_sha256": "b" * 64},
                )
            )
            patches.enter_context(patch.object(runner, "cleanup_coverage_run", side_effect=cleanup))
            patches.enter_context(
                patch.object(runner, "assert_head_unchanged", side_effect=step("head-check"))
            )
            runner.execute("candidate", "scanner")

    def test_r01_squash_aware_wave2_entry_fails_closed_before_preflight_begin_and_claim(self):
        invalid_cases = (
            ("untracked", lambda entry, evidence: evidence.__setitem__("tracked", False)),
            (
                "source-kind",
                lambda entry, evidence: entry["integration"].__setitem__("kind", "merge_commit"),
            ),
            (
                "release-intent",
                lambda entry, evidence: entry.__setitem__("release_intent", "v0.23.11"),
            ),
            (
                "source-blob-hash",
                lambda entry, evidence: evidence["source_blob"].__setitem__("sha256", "0" * 64),
            ),
            (
                "receipt-blob-hash",
                lambda entry, evidence: evidence["closure_receipt_blob"].__setitem__(
                    "sha256", "0" * 64
                ),
            ),
            (
                "reviewed-head",
                lambda entry, evidence: entry["integration"].__setitem__("head_sha", "1" * 40),
            ),
            (
                "first-party-pr-head",
                lambda entry, evidence: evidence["first_party_pull_request"].__setitem__(
                    "head_sha", "not-a-sha"
                ),
            ),
            (
                "merge-binding",
                lambda entry, evidence: evidence["first_party_pull_request"].__setitem__(
                    "merged", False
                ),
            ),
            (
                "candidate-lineage",
                lambda entry, evidence: evidence.__setitem__(
                    "candidate_is_ancestor_of_pr_head", False
                ),
            ),
            (
                "tree-equality",
                lambda entry, evidence: evidence.__setitem__("merge_tree_sha", "1" * 40),
            ),
            (
                "artifact-blob",
                lambda entry, evidence: evidence.__setitem__(
                    "artifact_blob_at_pr_head_matches", False
                ),
            ),
            (
                "artifact-history",
                lambda entry, evidence: evidence.__setitem__("artifact_path_history_valid", False),
            ),
            (
                "merge-to-main",
                lambda entry, evidence: evidence.__setitem__(
                    "merge_is_ancestor_of_observed_main", False
                ),
            ),
        )
        for name, mutate in invalid_cases:
            with self.subTest(name=name):
                entry = self._wave2_entry()
                evidence = self._wave2_evidence(entry)
                mutate(entry, evidence)
                with self.assertRaisesRegex(runner.RunnerError, "WAVE2_CLOSURE_UNVERIFIED"):
                    runner.verify_wave2_entry(entry, evidence)

        entry = self._wave2_entry()
        evidence = self._wave2_evidence(entry)
        resolved = runner.verify_wave2_entry(entry, evidence)
        self.assertEqual(resolved["accepted_candidate_sha"], self.HEAD)
        self.assertEqual(resolved["pull_request_head_sha"], "c" * 40)
        self.assertEqual(resolved["merge_commit_sha"], "d" * 40)
        self.assertEqual(resolved["integrated_tree_sha"], "e" * 40)

        with TemporaryDirectory() as temporary_directory:
            events: list[str] = []
            with self.assertRaisesRegex(runner.RunnerError, "injected entry failure"):
                self._transaction_events(Path(temporary_directory), events, "entry")
        self.assertEqual(events, ["entry"])

    def test_r02_preflight_refuses_unsafe_toolchain_before_begin_and_claim(self):
        invalid_cases = []
        for tool in ("uv", "bash", "dotnet"):
            invalid_cases.append(
                (
                    f"missing-{tool}",
                    lambda value, tool=tool: value["executables"].__setitem__(tool, None),
                    "COVERAGE_TOOL_UNAVAILABLE",
                )
            )
        invalid_cases.extend(
            (
                (
                    "coverlet",
                    lambda value: value["projects"][0].__setitem__("coverlet_msbuild", "9.0.0"),
                    "COVERAGE_VSTEST_INCOMPATIBLE",
                ),
                (
                    "test-sdk",
                    lambda value: value["projects"][0].__setitem__("test_sdk", "17.11.0"),
                    "COVERAGE_VSTEST_INCOMPATIBLE",
                ),
                (
                    "collector",
                    lambda value: value["projects"][3].__setitem__("code_coverage", "17.12.0"),
                    "COVERAGE_VSTEST_INCOMPATIBLE",
                ),
                (
                    "mtp",
                    lambda value: value["projects"][0].__setitem__("mtp_active", True),
                    "COVERAGE_MTP_INCOMPATIBLE",
                ),
            )
        )
        for name, mutate, code in invalid_cases:
            with self.subTest(name=name):
                toolchain = self._toolchain()
                mutate(toolchain)
                with self.assertRaisesRegex(runner.RunnerError, code):
                    runner.preflight_coverage_toolchain(toolchain)

        self.assertIsNotNone(runner.preflight_coverage_toolchain(self._toolchain()))
        with TemporaryDirectory() as temporary_directory:
            events: list[str] = []
            with self.assertRaisesRegex(runner.RunnerError, "injected preflight failure"):
                self._transaction_events(Path(temporary_directory), events, "preflight")
        self.assertEqual(events, ["entry", "preflight"])

    def test_r03_python_producer_uses_isolated_locked_uv_and_scrubs_sonar_environment(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "build").mkdir()
            (root / "build" / "coverage.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
            plan = self._plan(root)
            calls = []

            def capture(command, **kwargs):
                calls.append((command, kwargs))

            with patch.object(runner, "run_process", side_effect=capture):
                runner.run_coverage_producer(
                    plan,
                    {
                        "SAFE_VALUE": "kept",
                        "SONAR_TOKEN": "must-not-reach-producer",
                        "SONAR_READ_TOKEN": "must-not-reach-producer",
                    },
                )

        self.assertEqual(len(calls), 1)
        command, kwargs = calls[0]
        self.assertEqual(command[:2], ["uv", "run"])
        self.assertEqual(
            command[2:11],
            [
                "--project",
                str(root),
                "--isolated",
                "--locked",
                "--extra",
                "dev",
                "--with",
                "coverage==7.15.4",
                "--",
            ],
        )
        self.assertIn(Path(command[11]).name.casefold(), {"bash", "bash.exe"})
        self.assertEqual(command.count("--dotnet-project"), 5)
        self.assertEqual(kwargs["environment"], {"SAFE_VALUE": "kept"})

    def test_r03b_worktree_python_environment_is_locked_secret_free_and_cleanup_owned(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            captured: dict[str, Any] = {}

            def capture(command, **kwargs):
                captured["command"] = command
                captured.update(kwargs)

            with patch.object(runner, "run_process", side_effect=capture):
                runner.prepare_worktree_python_environment(
                    context,
                    {
                        "SAFE_VALUE": "kept",
                        "SONAR_HOST_URL": "https://sonar.example.test",
                        "SONAR_TOKEN": "secret",
                        "UV_PROJECT_ENVIRONMENT": "foreign-environment",
                        "VIRTUAL_ENV": "foreign-venv",
                    },
                    {"secret"},
                )

            self.assertEqual(captured["command"], ["uv", "sync", "--locked", "--extra", "dev"])
            self.assertEqual(captured["cwd"], root)
            self.assertEqual(
                captured["environment"],
                {
                    "SAFE_VALUE": "kept",
                    "UV_PROJECT_ENVIRONMENT": str(root / ".venv"),
                },
            )
            self.assertEqual(captured["label"], "Worktree Python environment")

            venv = root / ".venv"
            venv.mkdir()
            (venv / "owned.txt").write_text("generated", encoding="utf-8")
            bytecode = root / "src" / "netcoredbg_mcp" / "__pycache__"
            bytecode.mkdir(parents=True)
            (bytecode / "module.pyc").write_bytes(b"generated")
            original_metadata = runner._scanner_tree_metadata

            def cleanup_metadata(path):
                if ".venv" in path.relative_to(root).parts:
                    raise AssertionError("cleanup traversal entered .venv")
                return original_metadata(path)

            with (
                patch.object(runner, "is_tracked", return_value=False),
                patch.object(runner, "_scanner_tree_metadata", side_effect=cleanup_metadata),
            ):
                removed = runner.clear_generated_artifacts(context, {})

            self.assertIn(".venv", removed)
            self.assertFalse(venv.exists())
            self.assertIn("src/netcoredbg_mcp/__pycache__", removed)
            self.assertFalse(bytecode.exists())

    def test_r04_python_cobertura_requires_root_and_positive_line_and_branch_denominators(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "src/netcoredbg_mcp/module.py")
            report = root / "coverage.xml"
            report.write_text(self._cobertura(["src/netcoredbg_mcp/module.py"]), encoding="utf-8")
            with patch.object(
                runner, "is_tracked", side_effect=lambda _root, _env, path: path == source
            ):
                self.assertIsNotNone(runner.validate_python_cobertura(context, report))
                for name, payload in (
                    ("missing", None),
                    ("malformed", "<coverage"),
                    (
                        "line-only",
                        self._cobertura(["src/netcoredbg_mcp/module.py"], branches_valid=0),
                    ),
                    (
                        "zero-lines",
                        self._cobertura(["src/netcoredbg_mcp/module.py"], lines_valid=0),
                    ),
                ):
                    with self.subTest(name=name):
                        if payload is None:
                            report.unlink()
                        else:
                            report.write_text(payload, encoding="utf-8")
                        with self.assertRaisesRegex(
                            runner.RunnerError, "COVERAGE_(?:REPORT|DENOMINATOR)_"
                        ):
                            runner.validate_python_cobertura(context, report)
                        if payload is None:
                            report.write_text(
                                self._cobertura(["src/netcoredbg_mcp/module.py"]), encoding="utf-8"
                            )

    def test_r05_python_cobertura_accepts_only_unique_tracked_src_mappings(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "src/netcoredbg_mcp/module.py")
            reparse_source = self._write_source(root, "src/netcoredbg_mcp/reparse.py")
            test_source = self._write_source(root, "tests/test_only.py")
            report = root / "coverage.xml"
            original_metadata = runner._scanner_tree_metadata

            def source_is_tracked(_root, _env, path):
                return path in {source, reparse_source, test_source}

            cases = (
                ("absolute", [str(source.resolve())], False),
                ("uri", ["file:///source.py"], False),
                ("escape", ["../source.py"], False),
                ("missing", ["src/netcoredbg_mcp/missing.py"], False),
                ("test-only", ["tests/test_only.py"], False),
                ("reparse", ["src/netcoredbg_mcp/reparse.py"], True),
            )
            with patch.object(runner, "is_tracked", side_effect=source_is_tracked):
                for name, filenames, fake_reparse in cases:
                    with self.subTest(name=name):
                        report.write_text(self._cobertura(filenames), encoding="utf-8")

                        def metadata(
                            path, *, original=original_metadata, fake_reparse=fake_reparse
                        ):
                            if fake_reparse and path == reparse_source:
                                return SimpleNamespace(
                                    st_mode=stat.S_IFREG, st_file_attributes=0x0400
                                )
                            return original(path)

                        with patch.object(runner, "_scanner_tree_metadata", side_effect=metadata):
                            with self.assertRaisesRegex(
                                runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"
                            ):
                                runner.validate_python_cobertura(context, report)

            report.write_text(
                self._cobertura(["src/netcoredbg_mcp/module.py", "src/netcoredbg_mcp/module.py"]),
                encoding="utf-8",
            )
            with patch.object(runner, "is_tracked", side_effect=source_is_tracked):
                parsed = runner.validate_python_cobertura(context, report)
            self.assertEqual(parsed["source_paths"], ["src/netcoredbg_mcp/module.py"])

    def test_r05c_python_cobertura_accepts_only_the_two_release_scripts(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            runner_script = self._write_source(root, "scripts/run_sonarqube_exact_head.py")
            artifact_script = self._write_source(root, "scripts/stateless_preview_artifact.py")
            other_script = self._write_source(root, "scripts/other.py")
            test_source = self._write_source(root, "tests/test_only.py")
            report = root / "coverage.xml"
            trusted = {runner_script, artifact_script, other_script, test_source}
            with patch.object(
                runner, "is_tracked", side_effect=lambda _root, _env, path: path in trusted
            ):
                report.write_text(
                    self._cobertura(
                        [
                            "scripts/run_sonarqube_exact_head.py",
                            "scripts/stateless_preview_artifact.py",
                        ]
                    ),
                    encoding="utf-8",
                )
                self.assertEqual(
                    runner.validate_python_cobertura(context, report)["source_paths"],
                    [
                        "scripts/run_sonarqube_exact_head.py",
                        "scripts/stateless_preview_artifact.py",
                    ],
                )
                for name, filename in (
                    ("other-script", "scripts/other.py"),
                    ("test-only", "tests/test_only.py"),
                    ("escape", "scripts/../scripts/run_sonarqube_exact_head.py"),
                    ("uri", "file:///scripts/run_sonarqube_exact_head.py"),
                    ("absolute", str(runner_script.resolve())),
                ):
                    with self.subTest(name=name):
                        report.write_text(self._cobertura([filename]), encoding="utf-8")
                        with self.assertRaisesRegex(
                            runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"
                        ):
                            runner.validate_python_cobertura(context, report)

            report.write_text(
                self._cobertura(["scripts/stateless_preview_artifact.py"]), encoding="utf-8"
            )
            with patch.object(runner, "is_tracked", return_value=False):
                with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"):
                    runner.validate_python_cobertura(context, report)

            original_metadata = runner._scanner_tree_metadata

            def metadata(path):
                if path == artifact_script:
                    return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=0x0400)
                return original_metadata(path)

            with (
                patch.object(runner, "is_tracked", return_value=True),
                patch.object(runner, "_scanner_tree_metadata", side_effect=metadata),
            ):
                with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"):
                    runner.validate_python_cobertura(context, report)

    def test_r05b_cobertura_source_roots_canonicalize_relative_and_absolute_inputs(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "src/netcoredbg_mcp/module.py")
            report = root / "coverage.xml"
            with patch.object(runner, "is_tracked", return_value=True):
                for source_root in (
                    "src/netcoredbg_mcp",
                    source.parent.as_posix(),
                ):
                    with self.subTest(source_root=source_root):
                        report.write_text(
                            self._cobertura(["module.py"], sources=(source_root,)),
                            encoding="utf-8",
                        )
                        parsed = runner.validate_python_cobertura(context, report)
                        self.assertEqual(parsed["source_paths"], ["src/netcoredbg_mcp/module.py"])

                report.write_text(
                    self._cobertura(["module.py"], sources=(root.parent.as_posix(),)),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"):
                    runner.validate_python_cobertura(context, report)

    def test_r06_plan_is_pure_and_claim_marker_binds_reports_inputs_and_squash_identity(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            plan = runner.derive_coverage_plan(context, self.RUN_ID)
            self.assertFalse((root / ".tmp").exists())
            self.assertEqual(
                self._absolute(plan.python_report).relative_to(root).as_posix(),
                f".tmp/sonarqube-coverage/{self.RUN_ID}/python/coverage.xml",
            )
            self.assertEqual(
                self._absolute(plan.dotnet_report).relative_to(root).as_posix(),
                f".tmp/sonarqube-coverage/{self.RUN_ID}/dotnet/coverage.xml",
            )
            claim = runner.claim_coverage_run(context, plan, self._resolved_wave2_entry())
            marker_path = self._absolute(getattr(claim, "marker", plan.marker))
            marker = json.loads(marker_path.read_text(encoding="utf-8"))
            schema = json.loads(
                (
                    RUNNER_PATH.parents[1]
                    / "specs/014-sonarqube-coverage-producer/contracts/coverage-run-marker.schema.json"
                ).read_text(encoding="utf-8")
            )
            Draft202012Validator(schema, format_checker=FormatChecker()).validate(marker)

        self.assertEqual([report["id"] for report in marker["final_reports"]], ["python", "dotnet"])
        self.assertEqual(
            [input_["id"] for input_ in marker["dotnet_producers"]],
            [item[0] for item in self.DOTNET_PROJECTS],
        )
        self.assertEqual(
            marker["normalizer"]["input_order"], [item[0] for item in self.DOTNET_PROJECTS]
        )
        self.assertEqual(marker["wave2_entry"]["merge_commit_sha"], "d" * 40)
        self.assertEqual(marker["wave2_entry"]["integrated_tree_sha"], "e" * 40)
        forged = deepcopy(marker)
        forged["final_reports"].reverse()
        with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_MARKER_INVALID"):
            runner.validate_coverage_marker(plan, forged)

    def test_r07_scanner_begin_receives_exactly_two_runtime_cobertura_properties(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            plan = self._plan(root)
            properties = runner.coverage_scanner_properties(plan)
            begin = runner.scanner_begin_command(
                ["scanner"],
                root / "SonarQube.Analysis.xml",
                "https://sonar.example.test",
                self.HEAD,
                "scan-token",
                coverage_properties=properties,
            )

        self.assertEqual(
            properties,
            (
                f"/d:sonar.python.coverage.reportPaths=.tmp/sonarqube-coverage/{self.RUN_ID}/python/coverage.xml",
                f"/d:sonar.cs.cobertura.reportsPaths=.tmp/sonarqube-coverage/{self.RUN_ID}/dotnet/coverage.xml",
            ),
        )
        self.assertEqual(
            [
                argument
                for argument in begin
                if "coverage.report" in argument or "cobertura.reports" in argument
            ],
            list(properties),
        )

    def test_r08_coverage_inventory_is_exactly_the_five_ordered_private_projects(self):
        with TemporaryDirectory() as temporary_directory:
            plan = self._plan(Path(temporary_directory))
            observed = [
                (item.id, str(item.project).replace("\\", "/"), item.include_directory)
                for item in plan.dotnet_inputs
            ]

        self.assertEqual(observed, list(self.DOTNET_PROJECTS))
        invalid_sets = (
            list(self.DOTNET_PROJECTS[:-1]),
            [self.DOTNET_PROJECTS[1], *self.DOTNET_PROJECTS[1:]],
            list(reversed(self.DOTNET_PROJECTS)),
            [*self.DOTNET_PROJECTS, ("fixture", "tests/fixtures/Fixture.csproj", None)],
        )
        for invalid in invalid_sets:
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_VSTEST_INCOMPATIBLE"):
                    runner.validate_coverage_project_inventory(invalid)

    def test_r09_dotnet_producers_never_use_no_build_and_missing_private_input_blocks(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            plan = self._plan(root)
            commands = runner.dotnet_producer_commands(plan)
            test_commands = [command for command in commands if command[:2] == ["dotnet", "test"]]
            collector_commands = [
                command for command in commands if "collector-stateless" in command
            ]

            self.assertEqual(len(test_commands), 4)
            self.assertEqual(len(collector_commands), 1)
            self.assertEqual(
                collector_commands[0][-1],
                str(root / "host/NetCoreDbg.Mcp.Stateless/bin/Debug/net8.0"),
            )
            for command in test_commands:
                self.assertIn("--no-restore", command)
                self.assertNotIn("--no-build", command)
                self.assertIn("-p:CoverletOutputFormat=cobertura", command)
                self.assertNotIn("--filter", command)
            with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_REPORT_MISSING"):
                runner.validate_dotnet_cobertura_inputs(context, plan)

    def test_r10_only_stateless_gets_include_directory_and_restoration_and_mapping_are_required(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            plan = self._plan(root)
            commands = runner.dotnet_producer_commands(plan)
            self.assertTrue(
                any(
                    command[:2] == ["dotnet", "build"]
                    and "NetCoreDbg.Mcp.Stateless.Tests.csproj" in command[2]
                    for command in commands
                )
            )
            self.assertEqual(
                sum(
                    "IncludeDirectory=" in argument for command in commands for argument in command
                ),
                0,
            )

            stateless = plan.dotnet_inputs[3]
            report = self._absolute(stateless.raw_cobertura_input)
            report.parent.mkdir(parents=True)
            report.write_text(
                self._cobertura(["host/NetCoreDbg.Mcp.Stateless.Tests/Test.cs"]), encoding="utf-8"
            )
            self._write_source(root, "host/NetCoreDbg.Mcp.Stateless.Tests/Test.cs")
            with patch.object(runner, "is_tracked", return_value=True):
                with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"):
                    runner.validate_dotnet_cobertura_input(context, stateless, report)
            with self.assertRaisesRegex(
                runner.RunnerError, "COVERAGE_INSTRUMENTATION_NOT_RESTORED"
            ):
                runner.validate_stateless_restoration(
                    plan, {"dll_sha256": "0" * 64, "pdb_sha256": "0" * 64}
                )

    def test_stateless_collector_projects_absolute_production_and_excludes_test_classes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "host/NetCoreDbg.Mcp.Stateless/Program.cs")
            test_source = self._write_source(
                root, "host/NetCoreDbg.Mcp.Stateless.Tests/ProgramTests.cs"
            )
            raw = root / "raw.xml"
            projected = root / "projected.xml"
            line = (
                '<line number="26" hits="1" branch="true" condition-coverage="50% (1/2)">'
                '<conditions><condition number="0" type="jump" coverage="100%"/>'
                '<condition number="1" type="jump" coverage="0%"/></conditions></line>'
            )
            raw.write_text(
                '<coverage><packages><package name="NetCoreDbg.Mcp.Stateless"><classes>'
                f'<class name="NetCoreDbg.Mcp.Stateless.Program" filename="{source}"><lines>{line}</lines></class>'
                '</classes></package><package name="NetCoreDbg.Mcp.Stateless.Tests"><classes>'
                f'<class name="NetCoreDbg.Mcp.Stateless.Tests.ProgramTests" filename="{test_source}"><lines>'
                '<line number="1" hits="1"/></lines></class></classes></package></packages></coverage>',
                encoding="utf-8",
            )
            with patch.object(runner, "is_tracked", return_value=True):
                observed = runner.project_stateless_collector(context, raw, projected)
            self.assertEqual(observed["source_paths"], ["host/NetCoreDbg.Mcp.Stateless/Program.cs"])
            self.assertEqual((observed["branches_covered"], observed["branches_valid"]), (1, 2))
            self.assertEqual(
                runner.ElementTree.parse(projected).getroot().attrib["lines-valid"], "1"
            )

    def test_stateless_collector_global_startup_hook_is_test_source_not_production(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "host/NetCoreDbg.Mcp.Stateless/Program.cs")
            hook = self._write_source(
                root, "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/StartupHook.cs"
            )
            hook.write_text(
                "internal static class StartupHook { public static void Initialize() {} }\n",
                encoding="utf-8",
            )
            raw = root / "raw.xml"
            projected = root / "projected.xml"
            raw.write_text(
                '<coverage><packages><package name="NetCoreDbg.Mcp.Stateless"><classes>'
                f'<class name="NetCoreDbg.Mcp.Stateless.Program" filename="{source}"><lines>'
                '<line number="26" hits="0" branch="true" condition-coverage="0% (0/2)"/>'
                "</lines></class></classes></package>"
                '<package name="NetCoreDbg.Mcp.Stateless.Tests"><classes>'
                f'<class name="StartupHook" filename="{hook}"><lines>'
                '<line number="1" hits="1" branch="true" condition-coverage="100% (2/2)"/>'
                "</lines></class></classes></package></packages></coverage>",
                encoding="utf-8",
            )
            with patch.object(runner, "is_tracked", return_value=True):
                parsed = runner.project_stateless_collector(context, raw, projected)
                plan = replace(self._plan(root), dotnet_inputs=(self._plan(root).dotnet_inputs[3],))
                inputs = [runner._dotnet_input_evidence(plan, plan.dotnet_inputs[0], parsed)]
                normalization = runner.normalize_dotnet_cobertura(plan, inputs)
                final = runner.validate_final_dotnet_cobertura(context, plan, inputs, normalization)
            self.assertEqual(final["source_paths"], ["host/NetCoreDbg.Mcp.Stateless/Program.cs"])
            self.assertEqual(
                tuple(
                    final[key]
                    for key in (
                        "lines_valid",
                        "lines_covered",
                        "branches_valid",
                        "branches_covered",
                    )
                ),
                (1, 0, 2, 0),
            )
            self.assertEqual(final["sha256"], sha256(plan.dotnet_report.read_bytes()).hexdigest())

    def test_stateless_collector_test_origin_requires_source_and_module_identity(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "host/NetCoreDbg.Mcp.Stateless/Program.cs")
            hook = "host/NetCoreDbg.Mcp.Stateless.Tests/ModernMcp/StartupHook.cs"
            test_source = "host/NetCoreDbg.Mcp.Stateless.Tests/ProgramTests.cs"
            module = "NetCoreDbg.Mcp.Stateless.Tests"
            raw = root / "raw.xml"
            cases = (
                ("wrong-module", hook, "StartupHook", "NetCoreDbg.Mcp.Stateless"),
                ("foreign-module", hook, "StartupHook", "Foreign.Tests"),
                ("missing-module", hook, "StartupHook", ""),
                ("other-test-source", test_source, "StartupHook", module),
                (
                    "production-source",
                    "host/NetCoreDbg.Mcp.Stateless/StartupHook.cs",
                    "StartupHook",
                    module,
                ),
                ("foreign-source", "host/Foreign/StartupHook.cs", "StartupHook", module),
                (
                    "fixture-source",
                    "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/StartupHook.cs",
                    "StartupHook",
                    "ControlledDapAdapter",
                ),
                ("misreported-hook", hook, module + ".ProgramTests", module),
                ("production-class", test_source, "NetCoreDbg.Mcp.Stateless.Program", module),
                ("foreign-class", test_source, "Foreign.Tests.ProgramTests", module),
                (
                    "namespaced-wrong-module",
                    test_source,
                    module + ".ProgramTests",
                    "NetCoreDbg.Mcp.Stateless",
                ),
            )
            with patch.object(runner, "is_tracked", return_value=True):
                for name, relative, class_name, package in cases:
                    with self.subTest(name=name):
                        reported_source = self._write_source(root, relative)
                        raw.write_text(
                            '<coverage><packages><package name="NetCoreDbg.Mcp.Stateless"><classes>'
                            f'<class name="NetCoreDbg.Mcp.Stateless.Program" filename="{source}"><lines>'
                            '<line number="26" hits="0" branch="true" condition-coverage="0% (0/2)"/>'
                            "</lines></class></classes></package>"
                            f'<package name="{package}"><classes><class name="{class_name}" '
                            f'filename="{reported_source}"><lines><line number="1" hits="1"/>'
                            "</lines></class></classes></package></packages></coverage>",
                            encoding="utf-8",
                        )
                        with self.assertRaisesRegex(
                            runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"
                        ):
                            runner.project_stateless_collector(context, raw, root / "projected.xml")

    def test_stateless_collector_refuses_foreign_and_duplicate_spellings(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "host/NetCoreDbg.Mcp.Stateless/Program.cs")
            foreign = self._write_source(root, "host/Fixture/Foreign.cs")
            raw = root / "raw.xml"
            line = '<lines><line number="1" hits="1" branch="true" condition-coverage="100% (1/1)"/></lines>'
            with patch.object(runner, "is_tracked", return_value=True):
                for other in (foreign, str(source).replace("\\", "/")):
                    raw.write_text(
                        "<coverage><packages><package><classes>"
                        f'<class name="NetCoreDbg.Mcp.Stateless.Program" filename="{source}">{line}</class>'
                        f'<class name="NetCoreDbg.Mcp.Stateless.Other" filename="{other}">{line}</class>'
                        "</classes></package></packages></coverage>",
                        encoding="utf-8",
                    )
                    with self.assertRaisesRegex(
                        runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"
                    ):
                        runner.project_stateless_collector(context, raw, root / "projected.xml")

    def test_stateless_collector_full_run_maps_bridge_wpf_and_excludes_fixture_and_virtual_obj(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            stateless = self._write_source(root, "host/NetCoreDbg.Mcp.Stateless/Program.cs")
            bridge = self._write_source(root, "bridge/Commands/NativeSceneEvidenceCommands.cs")
            wpf = self._write_source(
                root, "host/NetCoreDbg.Mcp.DesignProbe.Wpf/LocalProbeClient.cs"
            )
            fixture = self._write_source(
                root, "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/ControlledDapAdapter/Program.cs"
            )
            generated = root / (
                "bridge/obj/Debug/net8.0-windows/win-x64/Microsoft.Interop.LibraryImportGenerator/"
                "Microsoft.Interop.LibraryImportGenerator/LibraryImports.g.cs"
            )
            sources = (
                (stateless, "NetCoreDbg.Mcp.Stateless.Program"),
                (bridge, "FlaUIBridge.Commands.NativeSceneEvidenceCommands"),
                (wpf, "NetCoreDbg.Mcp.DesignProbe.Wpf.LocalProbeClient"),
                (fixture, "ControlledEvidenceWindow"),
                (generated, "FlaUIBridge.Commands.ClickCommands"),
            )
            classes = "".join(
                f'<class name="{name}" filename="{path}"><lines><line number="1" hits="1" '
                'branch="true" condition-coverage="100% (1/1)"/></lines></class>'
                for path, name in sources
            )
            raw = root / "full.xml"
            full_xml = f"<coverage><packages><package><classes>{classes}</classes></package></packages></coverage>"
            raw.write_text(full_xml, encoding="utf-8")
            with patch.object(
                runner, "is_tracked", side_effect=lambda _root, _env, path: path != generated
            ):
                parsed = runner.project_stateless_collector(context, raw, root / "projected.xml")
            self.assertEqual(
                parsed["source_paths"],
                sorted(
                    (
                        "host/NetCoreDbg.Mcp.Stateless/Program.cs",
                        "bridge/Commands/NativeSceneEvidenceCommands.cs",
                        "host/NetCoreDbg.Mcp.DesignProbe.Wpf/LocalProbeClient.cs",
                    )
                ),
            )
            self.assertEqual((parsed["lines_covered"], parsed["branches_covered"]), (3, 3))
            self.assertGreater(
                sum(
                    line["hits"]
                    for source in parsed["facts"]
                    if source["source_path"].startswith("bridge/")
                    for line in source["lines"]
                ),
                0,
            )
            self.assertFalse(generated.exists())
            raw.write_text(
                full_xml.replace("LibraryImports.g.cs", "Injected.g.cs"), encoding="utf-8"
            )
            with patch.object(
                runner, "is_tracked", side_effect=lambda _root, _env, path: path != generated
            ):
                with self.assertRaisesRegex(runner.RunnerError, "unrecognized generated"):
                    runner.project_stateless_collector(context, raw, root / "projected.xml")
            raw.write_text(full_xml, encoding="utf-8")
            with patch.object(
                runner,
                "is_tracked",
                side_effect=lambda _root, _env, path: path not in {generated, bridge},
            ):
                with self.assertRaisesRegex(runner.RunnerError, "collector source is untracked"):
                    runner.project_stateless_collector(context, raw, root / "projected.xml")

    def test_stateless_collector_refuses_relative_escape_and_fixture_paths(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "checkout"
            root.mkdir()
            context = self._context(root)
            foreign = self._write_source(root.parent, "outside/Foreign.cs")
            fixture = self._write_source(
                root, "host/NetCoreDbg.Mcp.Stateless.Tests/Fixtures/Fixture.cs"
            )
            raw = root / "raw.xml"
            for filename in ("host/NetCoreDbg.Mcp.Stateless/Program.cs", foreign, fixture):
                raw.write_text(
                    "<coverage><packages><package><classes>"
                    f'<class name="NetCoreDbg.Mcp.Stateless.Tests.Fixture" filename="{filename}">'
                    '<lines><line number="1" hits="1" branch="true" '
                    'condition-coverage="100% (1/1)"/></lines></class>'
                    "</classes></package></packages></coverage>",
                    encoding="utf-8",
                )
                with patch.object(runner, "is_tracked", return_value=True):
                    with self.assertRaisesRegex(
                        runner.RunnerError, "COVERAGE_SOURCE_MAPPING_INVALID"
                    ):
                        runner.project_stateless_collector(context, raw, root / "projected.xml")

    def test_stateless_collector_trx_deployment_resolves_two_copy_layout(self):
        with TemporaryDirectory() as temporary_directory:
            results = Path(temporary_directory)
            trx = runner.ElementTree.fromstring(
                '<TestRun><TestSettings><Deployment runDeploymentRoot="deployment"/>'
                "</TestSettings></TestRun>"
            )
            deployment_copy = results / "deployment/In/HOST/attached.cobertura.xml"
            attachment_source = results / "1234/attached.cobertura.xml"
            for path in (deployment_copy, attachment_source):
                path.parent.mkdir(parents=True)
                path.write_text("<coverage/>", encoding="utf-8")
            self.assertEqual(
                runner.resolve_collector_attachment(results, trx, "HOST\\attached.cobertura.xml"),
                deployment_copy,
            )
            for href in ("../1234/attached.cobertura.xml", "HOST/../attached.cobertura.xml"):
                with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_REPORT_INVALID"):
                    runner.resolve_collector_attachment(results, trx, href)

    def test_stateless_collector_failed_cleanup_retains_same_owner_without_private_bypass(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object ownership only")
        sys.path.insert(0, str(RUNNER_PATH.parents[1] / "src"))
        owner_module = importlib.import_module("netcoredbg_mcp.windows_process_owner")

        class Owner:
            def __init__(self, wait_error, close_error, force_error):
                self.wait_error = wait_error
                self.close_error = close_error
                self.force_error = force_error
                self.close_calls = 0
                self.force_calls = 0
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_eof()

            def __getattr__(self, name):
                raise AssertionError(f"collector bypassed owner interface: {name}")

            async def wait_root(self):
                if self.wait_error is not None:
                    raise self.wait_error
                return 0

            async def drain_after_grace(self, **_kwargs):
                return SimpleNamespace(
                    status=owner_module.DrainStatus.FAILED, forced=False, active_processes=0
                )

            def drain_snapshot(self, receipt):
                return {
                    "status": receipt.status.value,
                    "forced": receipt.forced,
                    "root_was_forced": False,
                    "active_processes": receipt.active_processes,
                    "total_processes": 2,
                    "birth_notifications": 2,
                    "exit_notifications": 1,
                    "unverified_membership": False,
                    "root_birth_seen": True,
                    "live_members_without_handle": 0,
                    "retained_exact_handles": 1,
                    "signaled_exact_handles": 1,
                    "handle_probe_failed": False,
                    "failure_stage": "drain",
                    "winerror": None,
                }

            async def force_and_drain(self, **_kwargs):
                self.force_calls += 1
                if self.force_error:
                    raise RuntimeError("private force detail")
                return await self.drain_after_grace()

            async def aclose(self):
                self.close_calls += 1
                if self.close_error is not None:
                    raise self.close_error
                return await self.drain_after_grace()

        for wait_error, close_error, force_error in (
            (None, None, False),
            (None, RuntimeError("private cleanup detail"), False),
            (asyncio.CancelledError(), None, False),
            (TimeoutError(), None, False),
            (TimeoutError(), RuntimeError("private cleanup detail"), True),
        ):
            with self.subTest(wait=type(wait_error).__name__, force_error=force_error):

                async def exercise():
                    owner = Owner(wait_error, close_error, force_error)
                    with patch.object(
                        owner_module.WindowsOwnedProcess, "launch", return_value=owner
                    ):
                        with self.assertRaisesRegex(
                            runner.RunnerError, "COVERAGE_PROCESS_TREE_NOT_DRAINED"
                        ) as raised:
                            await asyncio.wait_for(
                                runner._run_owned_vstest(["collector"], RUNNER_PATH.parents[1]), 2
                            )
                    return owner, raised.exception

                with patch.object(runner, "_retained_collector_owners", []):
                    owner, observed = asyncio.run(exercise())
                    self.assertEqual(runner._retained_collector_owners, [owner])
                self.assertEqual((owner.force_calls, owner.close_calls), (1, 2))
                self.assertNotIn("private cleanup detail", str(observed))
                self.assertNotIn("private force detail", str(observed))
                if force_error:
                    with patch.object(
                        runner.subprocess,
                        "run",
                        return_value=SimpleNamespace(
                            returncode=1,
                            stdout=f"PROJECT_RELEASE_PROTOCOL_BLOCKED: {observed}\n",
                        ),
                    ):
                        with self.assertRaisesRegex(
                            runner.RunnerError, "COVERAGE_PROCESS_TREE_NOT_DRAINED"
                        ):
                            runner.run_process(
                                ["coverage-producer"],
                                cwd=RUNNER_PATH.parents[1],
                                environment={},
                                secrets=(),
                                label="Coverage producer",
                            )
                else:
                    diagnostic = json.loads(str(observed).split("owner_drain=", 1)[1])
                    self.assertEqual(diagnostic["first"]["total_processes"], 2)
                    self.assertEqual(diagnostic["first"]["retained_exact_handles"], 1)

    def test_stateless_collector_lifetime_reconciliation_failure_keeps_first_owner_evidence(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object ownership only")
        sys.path.insert(0, str(RUNNER_PATH.parents[1] / "src"))
        owner_module = importlib.import_module("netcoredbg_mcp.windows_process_owner")
        secret_path = r"C:\private\scan-token\test.dll"

        class Owner:
            def __init__(self):
                self.births = 2
                self.close_calls = 0
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_eof()
                self.secret_path = secret_path

            async def wait_root(self):
                return 0

            async def drain_after_grace(self, **_kwargs):
                return SimpleNamespace(
                    status=owner_module.DrainStatus.FAILED,
                    forced=True,
                    active_processes=0,
                )

            def drain_snapshot(self, receipt):
                return {
                    "status": receipt.status.value,
                    "forced": receipt.forced,
                    "root_was_forced": False,
                    "active_processes": receipt.active_processes,
                    "total_processes": 3,
                    "birth_notifications": self.births,
                    "exit_notifications": self.births,
                    "unverified_membership": False,
                    "root_birth_seen": True,
                    "live_members_without_handle": 0,
                    "retained_exact_handles": 2,
                    "signaled_exact_handles": 2,
                    "handle_probe_failed": False,
                    "failure_stage": "drain",
                    "winerror": None,
                }

            async def force_and_drain(self, **_kwargs):
                self.births = 0
                return await self.drain_after_grace()

            async def aclose(self):
                self.close_calls += 1
                if self.close_calls == 1:
                    raise RuntimeError("later cleanup failure")
                return SimpleNamespace(status=owner_module.DrainStatus.DRAINED, active_processes=0)

        with (
            patch.object(runner, "_retained_collector_owners", []),
            patch.object(
                owner_module.WindowsOwnedProcess, "launch", side_effect=lambda **_: Owner()
            ),
        ):
            with self.assertRaises(runner.RunnerError) as raised:
                asyncio.run(runner._run_owned_vstest(["collector"], RUNNER_PATH.parents[1]))
        message = str(raised.exception)
        self.assertIn("COVERAGE_PROCESS_TREE_NOT_DRAINED", message)
        self.assertNotIn("later cleanup failure", message)
        diagnostics = json.loads(message.split("owner_drain=", 1)[1])
        self.assertEqual(diagnostics["invariant"], "lifetime_accounting_mismatch")
        self.assertEqual(diagnostics["first"]["status"], "failed")
        self.assertEqual(diagnostics["first"]["total_processes"], 3)
        self.assertEqual(diagnostics["first"]["birth_notifications"], 2)
        self.assertEqual(diagnostics["first"]["exit_notifications"], 2)
        self.assertEqual(diagnostics["first"]["active_processes"], 0)
        self.assertEqual(diagnostics["first"]["retained_exact_handles"], 2)
        self.assertEqual(diagnostics["first"]["signaled_exact_handles"], 2)
        self.assertEqual(diagnostics["first"]["failure_stage"], "drain")

        class ActiveOwner(Owner):
            async def drain_after_grace(self, **_kwargs):
                return SimpleNamespace(
                    status=owner_module.DrainStatus.TIMED_OUT, forced=True, active_processes=1
                )

        with (
            patch.object(runner, "_retained_collector_owners", []),
            patch.object(
                owner_module.WindowsOwnedProcess, "launch", side_effect=lambda **_: ActiveOwner()
            ),
        ):
            with self.assertRaises(runner.RunnerError) as active:
                asyncio.run(runner._run_owned_vstest(["collector"], RUNNER_PATH.parents[1]))
        live_diagnostic = json.loads(str(active.exception).split("owner_drain=", 1)[1])
        self.assertEqual(live_diagnostic["invariant"], "active_processes_nonzero")
        self.assertEqual(live_diagnostic["first"]["active_processes"], 1)
        self.assertNotIn(secret_path, message)
        self.assertTrue(diagnostics["first"]["forced"])
        self.assertFalse(diagnostics["first"]["root_was_forced"])
        self.assertIsNone(diagnostics["first"]["winerror"])
        with patch.object(
            runner.subprocess,
            "run",
            return_value=SimpleNamespace(
                returncode=1,
                stdout=f"PROJECT_RELEASE_PROTOCOL_BLOCKED: {message}\n",
            ),
        ):
            with self.assertRaises(runner.RunnerError) as producer:
                runner.run_process(
                    ["coverage-producer"],
                    cwd=RUNNER_PATH.parents[1],
                    environment={},
                    secrets=(),
                    label="Coverage producer",
                )
        self.assertIn("lifetime_accounting_mismatch", str(producer.exception))
        self.assertNotIn(secret_path, str(producer.exception))

        for key, value in (("winerror", secret_path), ("root_pid", 4242)):
            with self.subTest(injected=key):
                forged = deepcopy(diagnostics)
                forged["first"][key] = value
                output = (
                    "PROJECT_RELEASE_PROTOCOL_BLOCKED: COVERAGE_PROCESS_TREE_NOT_DRAINED: "
                    f"collector Job failed; owner_drain={json.dumps(forged)}\n"
                )
                with patch.object(
                    runner.subprocess,
                    "run",
                    return_value=SimpleNamespace(returncode=1, stdout=output),
                ):
                    with self.assertRaises(runner.RunnerError) as refused:
                        runner.run_process(
                            ["coverage-producer"],
                            cwd=RUNNER_PATH.parents[1],
                            environment={},
                            secrets=(secret_path,),
                            label="Coverage producer",
                        )
                self.assertEqual(
                    str(refused.exception), "Coverage producer failed with exit code 1."
                )

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            claimed = root / ".tmp/sonarqube-coverage/claimed"
            claimed.mkdir(parents=True)
            evidence = claimed / "coverage-run.json"
            evidence.write_text("retained", encoding="utf-8")
            events: list[str] = []
            receipts: list[dict[str, Any]] = []
            with self.assertRaises(runner.RunnerError):
                self._transaction_events(
                    root,
                    events,
                    failing_step="produce",
                    producer_error=producer.exception,
                    real_cleanup=True,
                    receipts=receipts,
                )
            self.assertEqual(evidence.read_text(encoding="utf-8"), "retained")
        self.assertNotIn("end", events)
        blocked = receipts[-1]
        self.assertEqual(blocked["outcome"], "BLOCKED")
        self.assertEqual(blocked["failure"]["code"], "COVERAGE_PROCESS_TREE_NOT_DRAINED")
        self.assertEqual(blocked["cleanup"]["status"], "FAILED")
        self.assertFalse(blocked["cleanup"]["producer_terminal"])
        self.assertEqual(blocked["cleanup"]["claimed_root"], ".tmp/sonarqube-coverage/claimed")
        self.assertIn("lifetime_accounting_mismatch", blocked["failure"]["safe_message"])
        self.assertNotIn(secret_path, json.dumps(blocked))

    def test_stateless_collector_direct_capture_returns_only_after_owner_close(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object ownership only")
        sys.path.insert(0, str(RUNNER_PATH.parents[1] / "src"))
        owner_module = importlib.import_module("netcoredbg_mcp.windows_process_owner")

        class Owner:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_eof()
                self.close_entered = asyncio.Event()
                self.resume_close = asyncio.Event()
                self.closed = False

            async def wait_root(self):
                return 7

            async def drain_after_grace(self, **_kwargs):
                return SimpleNamespace(
                    status=owner_module.DrainStatus.DRAINED, forced=False, active_processes=0
                )

            async def aclose(self):
                self.close_entered.set()
                await self.resume_close.wait()
                self.closed = True
                return await self.drain_after_grace()

        async def exercise(cancel):
            owner = Owner()

            async def launch(*, capture_process_handles, env, **_kwargs):
                self.assertTrue(capture_process_handles)
                self.assertFalse(any(runner.is_sonar_environment_name(name) for name in env))
                return owner

            with (
                patch.dict(runner.os.environ, {"SONAR_TOKEN": "not-for-the-child"}),
                patch.object(owner_module.WindowsOwnedProcess, "launch", launch),
            ):
                task = asyncio.create_task(
                    runner._run_owned_vstest(["collector"], RUNNER_PATH.parents[1])
                )
                try:
                    await asyncio.wait_for(owner.close_entered.wait(), 2)
                    self.assertFalse(task.done(), "collector returned before owner close completed")
                    if cancel:
                        task.cancel()
                        await asyncio.sleep(0)
                        self.assertFalse(task.done(), "cleanup cancellation escaped its join")
                    owner.resume_close.set()
                    if cancel:
                        with self.assertRaises(asyncio.CancelledError):
                            await asyncio.wait_for(task, 2)
                    else:
                        self.assertEqual(await asyncio.wait_for(task, 2), 7)
                    self.assertTrue(owner.closed)
                finally:
                    owner.resume_close.set()
                    await asyncio.gather(task, return_exceptions=True)

        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                asyncio.run(exercise(cancel))

    def test_stateless_collector_cancellation_preserves_cancelled_error(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object ownership only")
        sys.path.insert(0, str(RUNNER_PATH.parents[1] / "src"))
        owner_module = importlib.import_module("netcoredbg_mcp.windows_process_owner")

        class Owner:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_eof()
                self.entered = asyncio.Event()

            async def wait_root(self):
                self.entered.set()
                await asyncio.Event().wait()

            async def force_and_drain(self, **_kwargs):
                return SimpleNamespace(status=owner_module.DrainStatus.DRAINED, active_processes=0)

            async def aclose(self):
                cleanup_calls.append(True)
                return SimpleNamespace(status=owner_module.DrainStatus.DRAINED, active_processes=0)

        cleanup_calls = []

        async def exercise():
            owner = Owner()
            with patch.object(owner_module.WindowsOwnedProcess, "launch", return_value=owner):
                task = asyncio.create_task(
                    runner._run_owned_vstest(["collector"], RUNNER_PATH.parents[1])
                )
                await owner.entered.wait()
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)

        asyncio.run(exercise())
        self.assertEqual(cleanup_calls, [True])

    def test_stateless_collector_drained_cleanup_preserves_original_failure_priority(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object ownership only")
        sys.path.insert(0, str(RUNNER_PATH.parents[1] / "src"))
        owner_module = importlib.import_module("netcoredbg_mcp.windows_process_owner")

        class Owner:
            def __init__(self, wait_error, close_error):
                self.wait_error = wait_error
                self.close_error = close_error
                self.close_calls = 0
                self.force_calls = 0
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_eof()

            async def wait_root(self):
                raise self.wait_error

            async def force_and_drain(self, **_kwargs):
                self.force_calls += 1
                return SimpleNamespace(status=owner_module.DrainStatus.DRAINED, active_processes=0)

            async def aclose(self):
                self.close_calls += 1
                if self.close_error is not None and self.close_calls == 1:
                    raise self.close_error
                return SimpleNamespace(status=owner_module.DrainStatus.DRAINED, active_processes=0)

        for wait_error, close_error in (
            (asyncio.CancelledError(), None),
            (TimeoutError(), None),
            (asyncio.CancelledError(), RuntimeError("owned close failure")),
            (TimeoutError(), RuntimeError("owned close failure")),
        ):
            with self.subTest(wait=type(wait_error).__name__, close=close_error is not None):

                async def exercise():
                    owner = Owner(wait_error, close_error)
                    with patch.object(
                        owner_module.WindowsOwnedProcess, "launch", return_value=owner
                    ):
                        with self.assertRaises(type(close_error or wait_error)) as raised:
                            await runner._run_owned_vstest(["collector"], RUNNER_PATH.parents[1])
                    return owner, raised.exception

                owner, observed = asyncio.run(exercise())
                self.assertIs(observed, close_error or wait_error)
                self.assertEqual(owner.force_calls, 1)
                self.assertEqual(owner.close_calls, 2 if close_error else 1)

    def test_stateless_collector_timeout_drains_owned_descendant(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object ownership only")
        import _winapi
        import psutil

        sys.path.insert(0, str(RUNNER_PATH.parents[1] / "src"))
        owner_module = importlib.import_module("netcoredbg_mcp.windows_process_owner")
        original_launch = owner_module.WindowsOwnedProcess.launch
        original_force = owner_module.WindowsOwnedProcess.force_and_drain

        for synchronize in (False, True):
            with self.subTest(readiness=synchronize), TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                pid_file = root / "descendant.pid"
                startup_gate = root / "startup.release"
                child = root / "child.py"
                child.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
                parent = root / "parent.py"
                parent.write_text(
                    "import subprocess, sys, time\n"
                    "from pathlib import Path\n"
                    "print('starting', flush=True)\n"
                    f"while not Path({str(startup_gate)!r}).is_file(): time.sleep(0.01)\n"
                    f"child = subprocess.Popen([sys.executable, {str(child)!r}], creationflags=0)\n"
                    f"Path({str(pid_file)!r}).write_text(str(child.pid), encoding='utf-8')\n"
                    "print('ready', flush=True)\n"
                    "time.sleep(30)\n",
                    encoding="utf-8",
                )
                identity = {}
                receipts = []

                async def ready_launch(**kwargs):
                    owner = await original_launch(**kwargs)
                    identity["owner"] = owner
                    try:
                        self.assertEqual(
                            (await asyncio.wait_for(owner.stdout.readline(), 10)).strip(),
                            b"starting",
                        )
                        # Job admission is not descendant readiness; hold startup past expiry
                        # or release it and observe the spawn before starting the same 2s wait.
                        if synchronize:
                            startup_gate.touch()
                            self.assertEqual(
                                (await asyncio.wait_for(owner.stdout.readline(), 10)).strip(),
                                b"ready",
                            )
                            self.assertTrue(pid_file.is_file())
                            identity["pid"] = int(pid_file.read_text(encoding="utf-8"))
                            identity["handle"] = _winapi.OpenProcess(
                                0x101001, False, identity["pid"]
                            )
                            self.assertTrue(
                                owner._api.is_process_in_job(identity["handle"], owner._job_handle)
                            )
                            self.assertFalse(owner._api.wait_for_process(identity["handle"], 0))
                            self.assertTrue(psutil.pid_exists(identity["pid"]))
                        self.assertTrue(
                            owner._api.is_process_in_job(owner._process_handle, owner._job_handle)
                        )
                        self.assertFalse(owner._api.wait_for_process(owner._process_handle, 0))
                        return owner
                    except BaseException:
                        await owner.aclose()
                        raise

                async def record_force(owner, *, timeout):
                    receipt = await original_force(owner, timeout=timeout)
                    receipts.append(owner.drain_snapshot(receipt))
                    return receipt

                async def exercise():
                    try:
                        with (
                            patch.object(owner_module.WindowsOwnedProcess, "launch", ready_launch),
                            patch.object(
                                owner_module.WindowsOwnedProcess, "force_and_drain", record_force
                            ),
                            self.assertRaises(TimeoutError) as raised,
                        ):
                            await runner._run_owned_vstest(
                                [sys.executable, str(parent)],
                                RUNNER_PATH.parents[1],
                                timeout_seconds=2,
                            )
                        self.assertIs(type(raised.exception), TimeoutError)
                    finally:
                        if "owner" in identity:
                            closed = await identity["owner"].aclose()
                            self.assertIs(closed.status, owner_module.DrainStatus.DRAINED)

                try:
                    asyncio.run(exercise())
                    if synchronize:
                        self.assertTrue(pid_file.is_file())
                        self.assertEqual(_winapi.WaitForSingleObject(identity["handle"], 0), 0)
                        self.assertFalse(psutil.pid_exists(identity["pid"]))
                    else:
                        self.assertFalse(pid_file.is_file(), "startup was held until timeout")
                    self.assertEqual(len(receipts), 1)
                    facts = receipts[0]
                    self.assertEqual(facts["status"], "drained", facts)
                    self.assertTrue(facts["forced"], facts)
                    self.assertEqual(facts["active_processes"], 0, facts)
                    self.assertGreaterEqual(
                        facts["total_processes"], 2 if synchronize else 1, facts
                    )
                    self.assertEqual(
                        facts["total_processes"], facts["retained_exact_handles"], facts
                    )
                    self.assertEqual(
                        facts["retained_exact_handles"], facts["signaled_exact_handles"], facts
                    )
                    self.assertTrue(facts["root_birth_seen"], facts)
                    self.assertFalse(facts["unverified_membership"], facts)
                    self.assertFalse(facts["handle_probe_failed"], facts)
                    self.assertEqual(facts["live_members_without_handle"], 0, facts)
                    print(
                        f"collector timeout readiness={synchronize} root={identity['owner'].pid} "
                        f"child={identity.get('pid')} marker={pid_file.is_file()} "
                        f"drain={json.dumps(facts, sort_keys=True)}"
                    )
                finally:
                    if "handle" in identity:
                        _winapi.CloseHandle(identity["handle"])

    def test_stateless_collector_interruption_drains_owned_descendant(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object ownership only")
        import _winapi
        import psutil

        sys.path.insert(0, str(RUNNER_PATH.parents[1] / "src"))
        owner_module = importlib.import_module("netcoredbg_mcp.windows_process_owner")

        receipts = []
        membership = []
        owners = []
        original_launch = owner_module.WindowsOwnedProcess.launch

        async def record_launch(**kwargs):
            owner = await original_launch(**kwargs)
            owners.append(owner)
            identity["root_handle"] = _winapi.OpenProcess(0x101001, False, owner.pid)
            self.assertEqual(
                (await asyncio.wait_for(owner.stdout.readline(), 10)).strip(), b"starting"
            )
            loop = asyncio.get_running_loop()
            held_since = loop.time()
            await asyncio.sleep(2)
            self.assertFalse(pid_file.is_file(), "startup is still held past the old 2s assumption")
            identity["startup_hold_seconds"] = loop.time() - held_since
            startup_gate.touch()
            readiness = (await asyncio.wait_for(owner.stdout.readline(), 10)).split()
            self.assertEqual(len(readiness), 2)
            self.assertEqual(readiness[0], b"ready")
            identity["interpreter_pid"] = int(readiness[1])
            identity["interpreter_handle"] = _winapi.OpenProcess(
                0x101001, False, identity["interpreter_pid"]
            )
            self.assertTrue(pid_file.is_file())
            identity["marker_before_cancel"] = pid_file.read_text(encoding="utf-8")
            identity["pid"] = int(identity["marker_before_cancel"])
            identity["process"] = psutil.Process(identity["pid"])
            identity["born"] = identity["process"].create_time()
            identity["before_status"] = identity["process"].status()
            identity["root_born"] = psutil.Process(owner.pid).create_time()
            identity["child_parent"] = identity["process"].ppid()
            identity["handle"] = _winapi.OpenProcess(0x101001, False, identity["pid"])
            self.assertEqual(psutil.Process(identity["pid"]).create_time(), identity["born"])
            self.assertEqual(identity["child_parent"], identity["interpreter_pid"])
            identity["known_pids"] = {owner.pid, identity["interpreter_pid"], identity["pid"]}
            for key in ("root_handle", "interpreter_handle", "handle"):
                self.assertTrue(owner._api.is_process_in_job(identity[key], owner._job_handle))
                self.assertEqual(_winapi.WaitForSingleObject(identity[key], 0), 258)
            snapshot("before_cancel")
            print(
                f"collector ready root={owner.pid} interpreter={identity['interpreter_pid']} "
                f"child={identity['pid']} startup_hold_seconds={identity['startup_hold_seconds']:.3f} "
                f"marker_before_cancel={identity['marker_before_cancel']} "
                f"exact_processes_live={len(identity['known_pids'])}"
            )
            loop.call_soon(asyncio.current_task().cancel)
            return owner

        def snapshot(label):
            if "handle" not in identity:
                return
            owner = owners[0]
            child_handle = identity["handle"]
            membership.append(
                (
                    label,
                    owner.pid,
                    owner._api.is_process_in_job(owner._process_handle, owner._job_handle),
                    owner._api.is_process_in_job(child_handle, owner._job_handle),
                    owner._api.is_process_in_job(child_handle, 0),
                    owner._query_active_processes(),
                    owner._api.wait_for_process(child_handle, 0),
                    owner._api.exit_code(child_handle),
                    identity["process"].is_running(),
                )
            )

        original_force = owner_module.WindowsOwnedProcess.force_and_drain
        original_close = owner_module.WindowsOwnedProcess.aclose

        async def record_force(owner, *, timeout):
            snapshot("before_force")
            receipt = await original_force(owner, timeout=timeout)
            snapshot("after_force")
            diagnostic = owner.drain_snapshot(receipt)
            receipts.append(("force", diagnostic))
            return receipt

        async def record_close(owner):
            snapshot("before_close")
            receipt = await original_close(owner)
            receipts.append(("close", owner.drain_snapshot(receipt)))
            return receipt

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            pid_file = root / "descendant.pid"
            startup_gate = root / "startup.release"
            child = root / "child.py"
            child.write_text("import time\ntime.sleep(40)\n", encoding="utf-8")
            parent = root / "parent.py"
            parent.write_text(
                "import os, subprocess, sys, time\n"
                "from pathlib import Path\n"
                "print('starting', flush=True)\n"
                f"while not Path({str(startup_gate)!r}).is_file(): time.sleep(0.01)\n"
                f"child = subprocess.Popen([sys.executable, {str(child)!r}], creationflags=0)\n"
                f"Path({str(pid_file)!r}).write_text(str(child.pid), encoding='utf-8')\n"
                "print(f'ready {os.getpid()}', flush=True)\n"
                "time.sleep(30)\n",
                encoding="utf-8",
            )

            identity = {}

            async def interrupt() -> None:
                task = asyncio.create_task(
                    runner._run_owned_vstest(
                        [sys.executable, str(parent)],
                        RUNNER_PATH.parents[1],
                        timeout_seconds=30,
                    )
                )
                try:
                    with self.assertRaises(asyncio.CancelledError) as raised:
                        await task
                    self.assertIs(type(raised.exception), asyncio.CancelledError)
                finally:
                    first_failure = sys.exc_info()[1]
                    if not task.done():
                        task.cancel()
                    outcome = (await asyncio.gather(task, return_exceptions=True))[0]
                    try:
                        if owners:
                            closed = await original_close(owners[0])
                            self.assertIs(closed.status, owner_module.DrainStatus.DRAINED)
                            self.assertEqual(closed.active_processes, 0)
                            self.assertTrue(owners[0]._closed)
                            for key in ("root_handle", "interpreter_handle", "handle"):
                                if key in identity:
                                    self.assertEqual(
                                        _winapi.WaitForSingleObject(identity[key], 0), 0
                                    )
                            print(
                                "collector fixture joined before asyncio shutdown "
                                f"root={owners[0].pid} interpreter={identity.get('interpreter_pid')} "
                                f"marker={pid_file.is_file()} "
                                f"drain={json.dumps(owners[0].drain_snapshot(closed), sort_keys=True)}"
                            )
                        if (
                            isinstance(outcome, BaseException)
                            and not isinstance(outcome, asyncio.CancelledError)
                            and outcome is not first_failure
                        ):
                            raise outcome
                    except BaseException as cleanup_error:
                        if first_failure is None:
                            raise
                        if hasattr(first_failure, "add_note"):
                            first_failure.add_note(f"fixture cleanup: {cleanup_error!r}")

            try:
                with (
                    patch.object(owner_module.WindowsOwnedProcess, "launch", record_launch),
                    patch.object(owner_module.WindowsOwnedProcess, "force_and_drain", record_force),
                    patch.object(owner_module.WindowsOwnedProcess, "aclose", record_close),
                ):
                    asyncio.run(interrupt())
                self.assertEqual(
                    pid_file.read_text(encoding="utf-8"), identity["marker_before_cancel"]
                )
                self.assertEqual([label for label, _ in receipts], ["force", "close"])
                self.assertGreaterEqual(identity["startup_hold_seconds"], 2)
                pid = identity["pid"]
                original_child_alive = identity["process"].is_running()
                try:
                    process = psutil.Process(pid)
                    observed = (process.create_time(), process.status(), process.is_running())
                except psutil.NoSuchProcess:
                    observed = None
                self.assertEqual(
                    [item[0] for item in membership],
                    ["before_cancel", "before_force", "after_force", "before_close"],
                )
                self.assertTrue(all(item[2] for item in membership[:2]), membership)
                self.assertTrue(all(item[3] for item in membership[:2]), membership)
                self.assertFalse(membership[0][6], membership)
                self.assertFalse(membership[1][6], membership)
                self.assertTrue(membership[2][6], membership)
                self.assertTrue(membership[3][6], membership)
                for label, facts in receipts:
                    self.assertEqual(facts["status"], "drained", (label, facts))
                    self.assertTrue(facts["forced"], (label, facts))
                    self.assertTrue(facts["root_was_forced"], (label, facts))
                    self.assertEqual(facts["active_processes"], 0, (label, facts))
                    self.assertGreaterEqual(
                        facts["total_processes"], len(identity["known_pids"]), (label, facts)
                    )
                    self.assertEqual(
                        facts["total_processes"], facts["retained_exact_handles"], (label, facts)
                    )
                    self.assertEqual(
                        facts["retained_exact_handles"],
                        facts["signaled_exact_handles"],
                        (label, facts),
                    )
                    self.assertTrue(facts["root_birth_seen"], (label, facts))
                    self.assertFalse(facts["unverified_membership"], (label, facts))
                    self.assertFalse(facts["handle_probe_failed"], (label, facts))
                    self.assertEqual(facts["live_members_without_handle"], 0, (label, facts))
                self.assertFalse(
                    original_child_alive,
                    f"original child remains active: root={owners[0].pid} "
                    f"root_born={identity['root_born']} child={pid} born={identity['born']} "
                    f"ppid={identity['child_parent']} before={identity['before_status']} "
                    f"after={observed} job={receipts} membership={membership}",
                )
            finally:
                for key in ("handle", "interpreter_handle", "root_handle"):
                    if key in identity:
                        _winapi.CloseHandle(identity[key])

    def test_stateless_collector_repeated_cancellation_joins_owned_close(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object ownership only")
        sys.path.insert(0, str(RUNNER_PATH.parents[1] / "src"))
        owner_module = importlib.import_module("netcoredbg_mcp.windows_process_owner")

        class Owner:
            def __init__(self):
                self.stdout = asyncio.StreamReader()
                self.stdout.feed_eof()
                self.stderr = asyncio.StreamReader()
                self.stderr.feed_eof()
                self.wait_entered = asyncio.Event()
                self.force_entered = asyncio.Event()
                self.resume_force = asyncio.Event()
                self.close_entered = asyncio.Event()
                self.resume_close = asyncio.Event()
                self.force_calls = 0
                self.close_calls = 0
                self.closed = False

            async def wait_root(self):
                self.wait_entered.set()
                await asyncio.Event().wait()

            async def force_and_drain(self, **_kwargs):
                self.force_calls += 1
                self.force_entered.set()
                await self.resume_force.wait()
                return SimpleNamespace(status=owner_module.DrainStatus.DRAINED, active_processes=0)

            async def aclose(self):
                self.close_calls += 1
                self.close_entered.set()
                await self.resume_close.wait()
                self.closed = True
                return SimpleNamespace(status=owner_module.DrainStatus.DRAINED, active_processes=0)

        async def exercise():
            owner = Owner()
            with patch.object(owner_module.WindowsOwnedProcess, "launch", return_value=owner):
                task = asyncio.create_task(
                    runner._run_owned_vstest(["collector"], RUNNER_PATH.parents[1])
                )
                try:
                    await asyncio.wait_for(owner.wait_entered.wait(), 2)
                    task.cancel()
                    await asyncio.wait_for(owner.force_entered.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done(), "second cancel escaped owned force")
                    owner.resume_force.set()
                    await asyncio.wait_for(owner.close_entered.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done(), "close cancellation lost the owner join")
                    self.assertFalse(owner.closed)
                    owner.resume_close.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                    self.assertTrue(owner.closed)
                    self.assertEqual((owner.force_calls, owner.close_calls), (1, 1))
                finally:
                    owner.resume_force.set()
                    owner.resume_close.set()
                    await asyncio.gather(task, return_exceptions=True)

        asyncio.run(exercise())

    def test_stateless_collector_rejects_changed_test_pdb_before_accepting_attachment(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / runner.FIXED_COVERAGE_PROJECTS[3][1]
            test_output = project.parent / "bin/Debug/net8.0"
            production_output = root / "host/NetCoreDbg.Mcp.Stateless/bin/Debug/net8.0"
            for directory, name in (
                (test_output, "NetCoreDbg.Mcp.Stateless.Tests"),
                (production_output, "NetCoreDbg.Mcp.Stateless"),
            ):
                directory.mkdir(parents=True)
                for suffix in (".dll", ".pdb"):
                    (directory / f"{name}{suffix}").write_bytes(b"before")
            adapter = root / "microsoft.codecoverage/17.14.1/build/netstandard2.0"
            adapter.mkdir(parents=True)
            (adapter / "Microsoft.VisualStudio.TraceDataCollector.dll").write_bytes(b"collector")

            async def mutate_binary(_command, _root):
                (test_output / "NetCoreDbg.Mcp.Stateless.Tests.pdb").write_bytes(b"after")
                return 0

            with (
                patch.dict(runner.os.environ, {"NUGET_PACKAGES": str(root)}),
                patch.object(runner, "_run_owned_vstest", side_effect=mutate_binary),
            ):
                with self.assertRaisesRegex(
                    runner.RunnerError, "COVERAGE_INSTRUMENTATION_NOT_RESTORED"
                ):
                    runner.produce_stateless_collector(
                        root,
                        project,
                        root / "dotnet/inputs/stateless/coverage.cobertura.xml",
                        production_output,
                    )

    def test_stateless_collector_never_unions_cross_provider_branch_ordinals(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            plan = self._plan(root)
            for index, spec in enumerate(plan.dotnet_inputs):
                source = (
                    "host/NetCoreDbg.Mcp.Stateless/Program.cs"
                    if spec.id in {"host", "stateless"}
                    else f"host/Production{index}/Source.cs"
                )
                self._write_source(root, source)
                report = self._absolute(spec.raw_cobertura_input)
                report.parent.mkdir(parents=True)
                report.write_text(self._cobertura([source]), encoding="utf-8")
            with patch.object(runner, "is_tracked", return_value=True):
                inputs = runner.validate_dotnet_cobertura_inputs(context, plan)
            with self.assertRaisesRegex(runner.RunnerError, "cross-provider"):
                runner.normalize_dotnet_cobertura(plan, inputs)

    def test_r11_private_dotnet_cobertura_inputs_require_safe_xml_sources_and_denominators(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "host/NetCoreDbg.Mcp.Host/Program.cs")
            spec = SimpleNamespace(
                id="host",
                project="host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj",
                include_directory=None,
            )
            report = root / "input.xml"
            invalid_cases = (
                ("malformed", "<coverage"),
                ("wrong-root", "<not-coverage/>"),
                (
                    "zero-lines",
                    self._cobertura(["host/NetCoreDbg.Mcp.Host/Program.cs"], lines_valid=0),
                ),
                ("unsafe-source", self._cobertura(["../outside.cs"])),
            )
            with patch.object(
                runner, "is_tracked", side_effect=lambda _root, _env, path: path == source
            ):
                for name, payload in invalid_cases:
                    with self.subTest(name=name):
                        report.write_text(payload, encoding="utf-8")
                        with self.assertRaisesRegex(
                            runner.RunnerError, "COVERAGE_(?:REPORT|DENOMINATOR|SOURCE)_"
                        ):
                            runner.validate_dotnet_cobertura_input(context, spec, report)

    def test_r11b_dotnet_cobertura_rejects_ambiguous_condition_identities(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source_path = "host/NetCoreDbg.Mcp.Host/Program.cs"
            source = self._write_source(root, source_path)
            spec = SimpleNamespace(
                id="host",
                project="host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj",
                include_directory=None,
            )
            report = root / "input.xml"
            report.write_text(
                self._cobertura(
                    [source_path],
                    line_xml=(
                        '<line number="1" hits="1" branch="true" '
                        'condition-coverage="50% (1/2)"><conditions>'
                        '<condition number="0" type="jump" coverage="100%"/>'
                        '<condition number="0" type="jump" coverage="0%"/>'
                        "</conditions></line>"
                    ),
                ),
                encoding="utf-8",
            )
            with patch.object(
                runner, "is_tracked", side_effect=lambda _root, _env, path: path == source
            ):
                with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_REPORT_INVALID"):
                    runner.validate_dotnet_cobertura_input(context, spec, report)

    def test_r11c_dotnet_cobertura_keeps_grouped_conditions_as_safe_aggregate(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source_path = "host/NetCoreDbg.Mcp.Host/Program.cs"
            source = self._write_source(root, source_path)
            spec = SimpleNamespace(
                id="host",
                project="host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj",
                include_directory=None,
            )
            report = root / "input.xml"
            report.write_text(
                self._cobertura(
                    [source_path],
                    line_xml=(
                        '<line number="1" hits="1" branch="true" '
                        'condition-coverage="94.11% (16/17)"><conditions>'
                        '<condition number="117" type="jump" coverage="100%"/>'
                        '<condition number="124" type="switch" coverage="80%"/>'
                        '<condition number="178" type="jump" coverage="100%"/>'
                        '<condition number="189" type="jump" coverage="100%"/>'
                        '<condition number="152" type="switch" coverage="100%"/>'
                        '<condition number="200" type="jump" coverage="100%"/>'
                        "</conditions></line>"
                    ),
                ),
                encoding="utf-8",
            )
            with patch.object(
                runner, "is_tracked", side_effect=lambda _root, _env, path: path == source
            ):
                parsed = runner.validate_dotnet_cobertura_input(context, spec, report)

        line = parsed["facts"][0]["lines"][0]
        self.assertEqual((line["branches_covered"], line["branches_valid"]), (16, 17))
        self.assertEqual(line["conditions"], [])

    def test_dotnet_cobertura_rejects_rounded_up_condition_percentage(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source_path = "host/NetCoreDbg.Mcp.Host/Program.cs"
            source = self._write_source(root, source_path)
            report = root / "input.xml"
            report.write_text(
                self._cobertura(
                    [source_path],
                    branches_valid=1,
                    line_xml=(
                        '<line number="1" hits="1" branch="true" '
                        'condition-coverage="100% (1/1)"><conditions>'
                        '<condition number="0" type="jump" '
                        'coverage="99.999999999999999999999999999999999999999999999%"/>'
                        "</conditions></line>"
                    ),
                ),
                encoding="utf-8",
            )
            spec = SimpleNamespace(
                id="host",
                project="host/NetCoreDbg.Mcp.Host.Tests/NetCoreDbg.Mcp.Host.Tests.csproj",
                include_directory=None,
            )
            with patch.object(
                runner, "is_tracked", side_effect=lambda _root, _env, path: path == source
            ):
                with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_REPORT_INVALID"):
                    runner.validate_dotnet_cobertura_input(context, spec, report)

    def test_stateless_collector_class_summary_preserves_independent_same_line_branches(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "host/NetCoreDbg.Mcp.Stateless/Program.cs")
            raw = root / "first-party.xml"
            projected = root / "projected.xml"

            def branch(valid: int, identities: int) -> str:
                conditions = "".join(
                    f'<condition number="{index}" type="jump" coverage="0%"/>'
                    for index in range(identities)
                )
                return (
                    f'<line number="1" hits="0" branch="true" '
                    f'condition-coverage="0% (0/{valid})"><conditions>{conditions}'
                    "</conditions></line>"
                )

            raw.write_text(
                "<coverage><packages><package><classes>"
                f'<class name="NetCoreDbg.Mcp.Stateless.Program" filename="{source}">'
                f'<methods><method name="lambda1"><lines>{branch(6, 3)}</lines></method>'
                f'<method name="lambda2"><lines>{branch(2, 1)}</lines></method></methods>'
                f"<lines>{branch(8, 4)}</lines></class>"
                "</classes></package></packages></coverage>",
                encoding="utf-8",
            )
            with patch.object(runner, "is_tracked", return_value=True):
                parsed = runner.project_stateless_collector(context, raw, projected)
            plan = self._plan(root)
            spec = plan.dotnet_inputs[3]
            isolated = replace(plan, dotnet_inputs=(spec,))
            runner.normalize_dotnet_cobertura(
                isolated, [runner._dotnet_input_evidence(isolated, spec, parsed)]
            )
            self.assertEqual((parsed["lines_valid"], parsed["branches_valid"]), (1, 8))
            self.assertEqual(
                runner.ElementTree.parse(isolated.dotnet_report).getroot().get("branches-valid"),
                "8",
            )
            raw.write_text(
                raw.read_text(encoding="utf-8").replace("0% (0/8)", "0% (0/7)"), encoding="utf-8"
            )
            with patch.object(runner, "is_tracked", return_value=True):
                with self.assertRaisesRegex(
                    runner.RunnerError, "class summary disagrees with methods"
                ):
                    runner.project_stateless_collector(context, raw, projected)

    def test_stateless_collector_distinct_classes_same_line_keep_both_branch_sets(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            source = self._write_source(root, "bridge/Commands/ClickCommands.cs")
            stateless = self._write_source(root, "host/NetCoreDbg.Mcp.Stateless/Program.cs")
            branch = (
                '<lines><line number="521" hits="0" branch="true" '
                'condition-coverage="0% (0/2)"><conditions>'
                '<condition number="0" type="jump" coverage="0%"/>'
                "</conditions></line></lines>"
            )
            classes = "".join(
                f'<class name="{name}" filename="{source}">{branch}</class>'
                for name in (
                    "FlaUIBridge.Commands.ClickCommands",
                    "FlaUIBridge.Commands.ClickCommands.&lt;&gt;c__DisplayClass41_0",
                )
            )
            classes += (
                f'<class name="NetCoreDbg.Mcp.Stateless.Program" filename="{stateless}">'
                '<lines><line number="1" hits="1"/></lines></class>'
            )
            raw = root / "collector.xml"
            raw.write_text(
                f"<coverage><packages><package><classes>{classes}</classes></package></packages></coverage>",
                encoding="utf-8",
            )
            with patch.object(runner, "is_tracked", return_value=True):
                parsed = runner.project_stateless_collector(context, raw, root / "projected.xml")
            plan = self._plan(root)
            spec = plan.dotnet_inputs[3]
            isolated = replace(plan, dotnet_inputs=(spec,))
            runner.normalize_dotnet_cobertura(
                isolated, [runner._dotnet_input_evidence(isolated, spec, parsed)]
            )
            self.assertEqual(parsed["branches_valid"], 4)
            self.assertEqual(
                runner.ElementTree.parse(isolated.dotnet_report).getroot().get("branches-valid"),
                "4",
            )

    def test_r12_dotnet_normalization_is_deterministic_and_final_output_must_equal_input_union(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            plan = self._plan(root)
            for index, spec in enumerate(plan.dotnet_inputs):
                source_path = (
                    "host/NetCoreDbg.Mcp.Stateless/Source3.cs"
                    if spec.id == "stateless"
                    else f"host/Production{index}/Source{index}.cs"
                )
                self._write_source(root, source_path)
                report = self._absolute(spec.raw_cobertura_input)
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(self._cobertura([source_path]), encoding="utf-8")
            with patch.object(runner, "is_tracked", return_value=True):
                inputs = runner.validate_dotnet_cobertura_inputs(context, plan)
                normalization = runner.normalize_dotnet_cobertura(plan, inputs)
                final_report = self._absolute(plan.dotnet_report)
                final_bytes = final_report.read_bytes()
                self.assertEqual(final_bytes, final_report.read_bytes())
                self.assertIsNotNone(normalization)
                final_report.write_text(
                    self._cobertura(["host/Production0/Source0.cs"], lines_valid=0),
                    encoding="utf-8",
                )
                with self.assertRaisesRegex(
                    runner.RunnerError, "COVERAGE_DOTNET_NORMALIZATION_FAILED"
                ):
                    runner.validate_final_dotnet_cobertura(context, plan, inputs, normalization)

    def test_r12a_dotnet_normalization_unions_distinct_condition_identities(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            plan = self._plan(root)
            shared_source = "host/Shared/Shared.cs"
            first_branch = (
                '<line number="1" hits="1" branch="true" condition-coverage="50% (1/2)">'
                "<conditions>"
                '<condition number="0" type="jump" coverage="100%"/>'
                '<condition number="1" type="jump" coverage="0%"/>'
                "</conditions></line>"
            )
            second_branch = (
                '<line number="1" hits="1" branch="true" condition-coverage="50% (1/2)">'
                "<conditions>"
                '<condition number="0" type="jump" coverage="0%"/>'
                '<condition number="1" type="jump" coverage="100%"/>'
                "</conditions></line>"
            )
            for index, spec in enumerate(plan.dotnet_inputs):
                if spec.id == "codesearch-core":
                    source_path, line_xml = shared_source, first_branch
                elif spec.id == "host":
                    source_path, line_xml = shared_source, second_branch
                elif spec.id == "stateless":
                    source_path, line_xml = "host/NetCoreDbg.Mcp.Stateless/Source3.cs", None
                else:
                    source_path, line_xml = f"host/Production{index}/Source{index}.cs", None
                self._write_source(root, source_path)
                report = self._absolute(spec.raw_cobertura_input)
                report.parent.mkdir(parents=True, exist_ok=True)
                report.write_text(
                    self._cobertura([source_path], line_xml=line_xml), encoding="utf-8"
                )
            with patch.object(runner, "is_tracked", return_value=True):
                inputs = runner.validate_dotnet_cobertura_inputs(context, plan)
                runner.normalize_dotnet_cobertura(plan, inputs)
            normalized = runner.ElementTree.parse(self._absolute(plan.dotnet_report)).getroot()
            shared_class = next(
                element
                for element in normalized.iter()
                if element.tag == "class" and element.attrib.get("filename") == shared_source
            )
            normalized_line = next(
                element
                for element in shared_class.iter()
                if element.tag == "line" and element.attrib.get("number") == "1"
            )
            normalized_conditions = next(
                element for element in normalized_line if element.tag == "conditions"
            )

        self.assertEqual(normalized_line.attrib["condition-coverage"], "100% (2/2)")
        self.assertEqual(
            [
                (element.attrib["number"], element.attrib["type"], element.attrib["coverage"])
                for element in normalized_conditions
            ],
            [("0", "jump", "100%"), ("1", "jump", "100%")],
        )

    def test_r13_transaction_orders_all_barriers_and_never_ends_after_prior_failure(self):
        with TemporaryDirectory() as temporary_directory:
            events: list[str] = []
            with self.assertRaisesRegex(runner.RunnerError, "stop after transaction event capture"):
                self._transaction_events(Path(temporary_directory), events)
        self.assertEqual(
            events,
            [
                "entry",
                "preflight",
                "begin",
                "claim",
                "python-env",
                "build",
                "produce",
                "normalize",
                "validate",
                "head-check",
                "end",
            ],
        )

        for failing_step in (
            "entry",
            "preflight",
            "begin",
            "claim",
            "python-env",
            "build",
            "produce",
            "normalize",
            "validate",
            "head-check",
        ):
            with (
                self.subTest(failing_step=failing_step),
                TemporaryDirectory() as temporary_directory,
            ):
                events = []
                with self.assertRaisesRegex(runner.RunnerError, f"injected {failing_step} failure"):
                    self._transaction_events(Path(temporary_directory), events, failing_step)
                self.assertNotIn("end", events)

    def test_r13a_unproven_producer_failure_preserves_claimed_root(self):
        terminals: list[bool] = []
        receipts: list[dict[str, Any]] = []
        events: list[str] = []
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            claimed_root = root / ".tmp/sonarqube-coverage/claimed"
            claimed_root.mkdir(parents=True)
            generated_file = claimed_root / "coverage-run.json"
            generated_file.write_text("evidence", encoding="utf-8")
            with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_PROCESS_TREE_NOT_DRAINED"):
                self._transaction_events(
                    root,
                    events,
                    failing_step="produce",
                    producer_error=runner.RunnerError(
                        "COVERAGE_PROCESS_TREE_NOT_DRAINED: "
                        "collector Job closed without verified drain"
                    ),
                    producer_terminals=terminals,
                    real_cleanup=True,
                    receipts=receipts,
                )
            self.assertEqual(terminals, [False])
            self.assertEqual(generated_file.read_text(encoding="utf-8"), "evidence")
        self.assertNotIn("end", events)
        blocked = receipts[-1]
        self.assertEqual(blocked["outcome"], "BLOCKED")
        self.assertEqual(blocked["failure"]["code"], "COVERAGE_PROCESS_TREE_NOT_DRAINED")
        self.assertEqual(blocked["cleanup"]["claimed_root"], ".tmp/sonarqube-coverage/claimed")
        self.assertEqual(blocked["cleanup"]["status"], "FAILED")
        self.assertFalse(blocked["cleanup"]["producer_terminal"])

    def test_r13b_failure_after_venv_creation_runs_generated_cleanup(self):
        generated_cleanup_calls: list[str] = []
        with TemporaryDirectory() as temporary_directory:
            with self.assertRaisesRegex(runner.RunnerError, "injected build failure"):
                self._transaction_events(
                    Path(temporary_directory),
                    [],
                    failing_step="build",
                    generated_cleanup_calls=generated_cleanup_calls,
                )

        self.assertEqual(generated_cleanup_calls, ["clear", "clear"])

    def test_r14_analysis_evidence_requires_canonical_two_language_components(self):
        identity = {
            "captured_head": self.HEAD,
            "project_key": runner.PROJECT_KEY,
            "analysis_id": "analysis-1",
        }
        component = {
            "complete": True,
            "page_count": 1,
            "mapped_path_count": 1,
            "lines_to_cover": 2,
            "covered_lines": 1,
            "branch_measure_path_count": 1,
        }
        observations = {
            "submitted": deepcopy(identity),
            "current_before_measures": deepcopy(identity),
            "current_after_measures": deepcopy(identity),
            "current_final": deepcopy(identity),
            "aggregate": {
                "coverage": 50.0,
                "lines_to_cover": 4,
                "new_coverage": 80.0,
                "new_lines_to_cover": 2,
            },
            "new_coverage_condition": {"status": "OK", "threshold": 80, "actual_value": 80.0},
            "python_components": deepcopy(component),
            "dotnet_components": deepcopy(component),
        }
        self.assertIsNotNone(runner.validate_coverage_analysis_evidence(identity, observations))
        invalid_cases = (
            (
                "analysis-id",
                lambda value: value["current_final"].__setitem__("analysis_id", "other-analysis"),
            ),
            (
                "revision",
                lambda value: value["current_after_measures"].__setitem__(
                    "captured_head", "c" * 40
                ),
            ),
            (
                "incomplete-pages",
                lambda value: value["python_components"].__setitem__("complete", False),
            ),
            (
                "python-unmapped",
                lambda value: value["python_components"].__setitem__("mapped_path_count", 0),
            ),
            (
                "dotnet-unmapped",
                lambda value: value["dotnet_components"].__setitem__("covered_lines", 0),
            ),
        )
        for name, mutate in invalid_cases:
            with self.subTest(name=name):
                forged = deepcopy(observations)
                mutate(forged)
                with self.assertRaisesRegex(
                    runner.RunnerError,
                    "COVERAGE_(?:ANALYSIS_MISMATCH|IMPORT_UNPROVEN|MEASURES_INVALID)",
                ):
                    runner.validate_coverage_analysis_evidence(identity, forged)

    def test_wave3_analysis_collection_proves_both_language_source_sets(self):
        identity = {
            "captured_head": self.HEAD,
            "project_key": runner.PROJECT_KEY,
            "analysis_id": "analysis-1",
        }
        coverage = {
            "final_reports": [
                {"source_paths": ["src/netcoredbg_mcp/server.py"]},
                {"source_paths": ["host/NetCoreDbg.Mcp.Host/Program.cs"]},
            ]
        }
        observations = {
            "submitted": identity,
            "current_before_measures": identity,
            "current_after_measures": identity,
            "current_final": identity,
        }
        quality_gate = {
            "conditions": [
                {
                    "metricKey": "new_coverage",
                    "status": "ERROR",
                    "errorThreshold": "80",
                    "actualValue": "79.5",
                }
            ]
        }
        tree_response = {
            "paging": {"total": 2},
            "components": [
                {
                    "path": "src/netcoredbg_mcp/server.py",
                    "measures": [
                        {"metric": "lines_to_cover", "value": "10"},
                        {"metric": "uncovered_lines", "value": "2"},
                        {"metric": "conditions_to_cover", "value": "4"},
                        {"metric": "uncovered_conditions", "value": "1"},
                    ],
                },
                {
                    "path": "host/NetCoreDbg.Mcp.Host/Program.cs",
                    "measures": [
                        {"metric": "lines_to_cover", "value": "12"},
                        {"metric": "uncovered_lines", "value": "3"},
                        {"metric": "conditions_to_cover", "value": "2"},
                        {"metric": "uncovered_conditions", "value": "1"},
                    ],
                },
            ],
        }

        def api_response(_host, endpoint, _parameters, _token):
            if endpoint == "/api/measures/component":
                return {
                    "component": {
                        "measures": [
                            {"metric": "coverage", "value": "80"},
                            {"metric": "lines_to_cover", "value": "22"},
                            {"metric": "new_coverage", "value": "79.5"},
                            {"metric": "new_lines_to_cover", "value": "8"},
                        ]
                    }
                }
            return tree_response

        with patch.object(runner, "api_json", side_effect=api_response):
            result = runner.collect_coverage_analysis_evidence(
                "https://sonar.example.test",
                "read-token",
                identity,
                quality_gate,
                coverage,
                observations,
            )

        self.assertEqual(result["new_coverage_condition"]["status"], "ERROR")
        self.assertEqual(result["python_components"]["mapped_path_count"], 1)
        self.assertEqual(result["dotnet_components"]["covered_lines"], 9)

    def test_component_coverage_accepts_uncovered_and_empty_files_when_language_is_covered(self):
        expected_paths = [
            "src/netcoredbg_mcp/covered.py",
            "src/netcoredbg_mcp/uncovered.py",
            "src/netcoredbg_mcp/empty.py",
        ]
        response = {
            "paging": {"total": 3},
            "components": [
                {
                    "path": expected_paths[0],
                    "measures": [
                        {"metric": "lines_to_cover", "value": "10"},
                        {"metric": "uncovered_lines", "value": "2"},
                        {"metric": "conditions_to_cover", "value": "2"},
                    ],
                },
                {
                    "path": expected_paths[1],
                    "measures": [
                        {"metric": "lines_to_cover", "value": "5"},
                        {"metric": "uncovered_lines", "value": "5"},
                    ],
                },
                {
                    "path": expected_paths[2],
                    "measures": [],
                },
            ],
        }

        with patch.object(runner, "api_json", return_value=response):
            summary = runner._component_coverage_summary(
                "https://sonar.example.test", "read-token", expected_paths
            )

        self.assertEqual(summary["mapped_path_count"], 3)
        self.assertEqual(summary["lines_to_cover"], 15)
        self.assertEqual(summary["covered_lines"], 8)
        self.assertEqual(summary["branch_measure_path_count"], 1)

    def test_wave3_inventory_is_create_new_and_hash_bound(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            identity = {
                "captured_head": self.HEAD,
                "project_key": runner.PROJECT_KEY,
                "analysis_id": "analysis-1",
            }
            issues = {
                "total": 1,
                "pages": [{"page_index": 1, "page_size": runner.PAGE_SIZE, "total": 1}],
                "pagination_complete": True,
                "result_empty": False,
                "records": [{"key": "issue-1"}],
            }
            hotspots = {
                "total": 0,
                "pages": [{"page_index": 1, "page_size": runner.PAGE_SIZE, "total": 0}],
                "pagination_complete": True,
                "result_empty": True,
                "records": [],
            }
            reference = runner.write_diagnostic_inventory(
                context,
                self.RUN_ID,
                identity,
                issues,
                hotspots,
                {
                    "blocking_count": 1,
                    "items": [{"key": "issue-1", "disposition": "BLOCKING_DISPOSITION"}],
                },
                {"blocking_count": 0, "items": []},
            )
            artifact = root / reference["artifact"]["relative_path"]
            raw = artifact.read_bytes()
            self.assertEqual(sha256(raw).hexdigest(), reference["artifact"]["sha256"])
            self.assertEqual(reference["issues"]["blocking_key_count"], 1)
            with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_INVENTORY_WRITE_FAILED"):
                runner.write_diagnostic_inventory(
                    context,
                    self.RUN_ID,
                    identity,
                    issues,
                    hotspots,
                    {"blocking_count": 0, "items": []},
                    {"blocking_count": 0, "items": []},
                )
