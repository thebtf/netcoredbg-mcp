"""Focused contracts for the dependency-free SonarQube exact-head runner."""

import importlib.util
import json
import stat
import sys
from contextlib import ExitStack, nullcontext
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
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

    @staticmethod
    def context(primary_root: Path, scanner_root: Path) -> runner.GitContext:
        return runner.GitContext(
            scanner_root, primary_root / ".git", scanner_root / ".git", primary_root, "a" * 40
        )

    @staticmethod
    def patch_wave3_transaction(patches: ExitStack) -> None:
        plan = SimpleNamespace()
        patches.enter_context(patch.object(runner, "resolve_wave2_entry", return_value={}))
        patches.enter_context(patch.object(runner, "verify_wave2_entry", return_value={}))
        patches.enter_context(patch.object(runner, "preflight_coverage_toolchain", return_value={}))
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
            patch.object(runner, "collect_coverage_analysis_evidence", return_value={})
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

            with self.assertRaisesRegex(runner.RunnerError, "in-tree .env"):
                runner.load_credentials(
                    self.context(primary_root, scanner_root), self.credentials()
                )

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
                    with self.assertRaisesRegex(runner.RunnerError, "symbolic link"):
                        runner.load_credentials(
                            self.context(primary_root, scanner_root), self.credentials()
                        )
            else:
                with self.assertRaisesRegex(runner.RunnerError, "symbolic link"):
                    runner.load_credentials(
                        self.context(primary_root, scanner_root), self.credentials()
                    )

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
                side_effect=runner.CredentialsUnavailable(*runner.REQUIRED_ENV),
            ) as verified_reader:
                with self.assertRaisesRegex(
                    runner.CredentialsUnavailable, "SONAR_CREDENTIALS_UNAVAILABLE"
                ):
                    runner.load_credentials(
                        self.context(primary_root, scanner_root), self.credentials()
                    )

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
                        with self.assertRaisesRegex(
                            runner.CredentialsUnavailable, "SONAR_CREDENTIALS_UNAVAILABLE"
                        ):
                            runner.load_credentials(self.context(primary_root, scanner_root), {})

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
                        with self.assertRaisesRegex(runner.RunnerError, "SONAR_ADMIN_TOKEN"):
                            runner.load_credentials(
                                self.context(primary_root, scanner_root), process_env
                            )

    def test_load_credentials_rejects_noncanonical_case_sonar_tokens(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            primary_root.mkdir()
            scanner_root.mkdir()

            for input_name in ("sonar_token", "sonar_admin_token"):
                with self.subTest(input_name=input_name):
                    with self.assertRaises(runner.RunnerError):
                        runner.load_credentials(
                            self.context(primary_root, scanner_root),
                            {**self.credentials(), input_name: "mis-cased-token"},
                        )

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
                        with self.assertRaisesRegex(runner.RunnerError, "Unknown"):
                            runner.load_credentials(
                                self.context(primary_root, scanner_root), process_env
                            )

    def test_malformed_host_is_a_named_credential_blocker(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            primary_root = root / "primary"
            scanner_root = root / "scanner"
            primary_root.mkdir()
            scanner_root.mkdir()
            with self.assertRaisesRegex(
                runner.CredentialsUnavailable, r"^SONAR_CREDENTIALS_UNAVAILABLE: SONAR_HOST_URL\."
            ):
                runner.load_credentials(
                    self.context(primary_root, scanner_root),
                    {**self.credentials(), "SONAR_HOST_URL": "sonar.example.test"},
                )

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
                with self.assertRaisesRegex(runner.CredentialsUnavailable, "SONAR_HOST_URL"):
                    runner.credential_free_host(supplied)

    def test_scanner_auth_failure_is_a_named_credential_blocker(self):
        with TemporaryDirectory() as temporary_directory:
            with patch.object(
                runner.subprocess,
                "run",
                return_value=runner.subprocess.CompletedProcess([], 1, "HTTP 401 unauthorized"),
            ):
                with self.assertRaisesRegex(
                    runner.CredentialsUnavailable, r"^SONAR_CREDENTIALS_UNAVAILABLE: SONAR_TOKEN\."
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
            project.parent.mkdir(parents=True)
            fixture.parent.mkdir(parents=True)
            project.write_text("<Project />", encoding="utf-8")
            fixture.write_text("<Project />", encoding="utf-8")
            (root / "netcoredbg-mcp.sln").write_text(
                'Project("{guid}") = "App", "host\\App.csproj", "{id}"\nEndProject\n',
                encoding="utf-8",
            )

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
        self.assertEqual(blocked_receipt["analysis"], {})
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

            def fail_cleanup(_plan, _producer_terminal):
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
                "generated_artifacts_removed_after_scan",
                "post_scan_cleanup",
                "analysis_current_final",
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
        with self.assertRaises(ValueError):
            runner.normalize_windows_handle_for_crt(ctypes.c_void_p(-1), invalid_handle_value)

    def test_close_windows_handle_if_owned_skips_sentinels_and_closes_owned_handle_once(self):
        import ctypes

        class Kernel32:
            def __init__(self):
                self.closed_handles = []

            def CloseHandle(self, handle):
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
                receipt_path, runner.receipt_base(context, "candidate", "new-run"), ()
            )
            replacement = json.loads(receipt_path.read_text(encoding="utf-8"))

        self.assertEqual(replacement["outcome"], "BLOCKED")
        self.assertEqual(replacement["failure"]["code"], "COVERAGE_RUN_INCOMPLETE")

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
                    "coverlet_msbuild": "10.0.1",
                    "coverlet_private_assets": "all",
                    "test_sdk": "17.12.0",
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
    def _cobertura(
        filenames,
        *,
        lines_valid=2,
        branches_valid=2,
        sources=(".",),
    ) -> str:
        classes = "".join(
            f'<class name="module" filename="{filename}"><methods/><lines>'
            '<line number="1" hits="1" branch="true" '
            'condition-coverage="50% (1/2)"/></lines></class>'
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
        self, root: Path, events: list[str], failing_step: str | None = None
    ) -> None:
        context = self._context(root)
        plan = SimpleNamespace()
        claim = SimpleNamespace()

        def step(name, result=None):
            def invoke(*_args, **_kwargs):
                events.append(name)
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

        with ExitStack() as patches:
            patches.enter_context(patch.object(runner, "process_environment", return_value={}))
            patches.enter_context(patch.object(runner, "scrub_sonar_environment", return_value={}))
            patches.enter_context(patch.object(runner, "git_context", return_value=context))
            patches.enter_context(
                patch.object(runner, "receipt_path", return_value=root / "receipt.json")
            )
            patches.enter_context(patch.object(runner, "sonar_secret_values", return_value=set()))
            patches.enter_context(patch.object(runner, "project_lock", return_value=nullcontext()))
            patches.enter_context(patch.object(runner, "write_receipt"))
            patches.enter_context(
                patch.object(
                    runner,
                    "load_credentials",
                    return_value=TestSonarqubeExactHeadRunner.credentials(),
                )
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
            patches.enter_context(patch.object(runner, "cleanup_coverage_run", return_value={}))
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

            self.assertEqual(len(test_commands), 5)
            for command in test_commands:
                self.assertIn("--no-restore", command)
                self.assertNotIn("--no-build", command)
                self.assertIn("-p:CoverletOutputFormat=cobertura", command)
            filtered = [command for command in test_commands if "--filter" in command]
            self.assertEqual(len(filtered), 1)
            self.assertTrue(
                any("NetCoreDbg.Mcp.Stateless.Tests.csproj" in item for item in filtered[0])
            )
            filter_index = filtered[0].index("--filter")
            self.assertEqual(filtered[0][filter_index + 1], "Coverage!=Exclude")
            with self.assertRaisesRegex(runner.RunnerError, "COVERAGE_REPORT_MISSING"):
                runner.validate_dotnet_cobertura_inputs(context, plan)

    def test_r09b_every_stateless_process_collection_class_is_excluded_from_coverlet(self):
        tests_root = RUNNER_PATH.parents[1] / "host" / "NetCoreDbg.Mcp.Stateless.Tests"
        process_classes = 0
        missing_traits: list[str] = []
        for path in tests_root.rglob("*.cs"):
            source = path.read_text(encoding="utf-8")
            collection_count = source.count("[Collection(")
            if collection_count == 0 or "NetCoreDbgSessionProcessCollection.Name" not in source:
                continue
            process_classes += collection_count
            if source.count('[Trait("Coverage", "Exclude")]') < collection_count:
                missing_traits.append(path.relative_to(tests_root).as_posix())

        self.assertEqual(process_classes, 12)
        self.assertEqual(missing_traits, [])

    def test_r10_only_stateless_gets_include_directory_and_restoration_and_mapping_are_required(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self._context(root)
            plan = self._plan(root)
            commands = runner.dotnet_producer_commands(plan)
            include_commands = [
                command
                for command in commands
                if any("IncludeDirectory=" in argument for argument in command)
            ]
            self.assertEqual(len(include_commands), 1)
            self.assertTrue(
                any("NetCoreDbg.Mcp.Stateless.Tests.csproj" in item for item in include_commands[0])
            )
            include_directory = str(
                root / "host" / "NetCoreDbg.Mcp.Stateless" / "bin" / "Debug" / "net8.0"
            )
            self.assertTrue(any(item.endswith(include_directory) for item in include_commands[0]))

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

    def test_python_coverage_workload_uses_curated_non_live_suite(self):
        script = (RUNNER_PATH.parents[1] / "build" / "coverage.sh").read_text(encoding="utf-8")

        self.assertIn("python_test_paths=(", script)
        for path in (
            "tests/test_client.py",
            "tests/test_session.py",
            "tests/test_runtime_smoke_runner.py",
            "tests/test_stealth_mode.py",
            "tests/test_ui_evidence.py",
        ):
            self.assertIn(path, script)
        for excluded in (
            "tests/critical",
            "tests/test_wpf_runtime_workflow_fixture.py",
            "tests/test_windows_process_owner.py",
            "tests/test_sonarqube_exact_head_runner.py",
            "tests/test_stateless_preview_artifact.py",
        ):
            self.assertNotIn(excluded, script)
