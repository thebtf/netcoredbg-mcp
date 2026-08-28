"""Focused contracts for the dependency-free SonarQube exact-head runner."""

import importlib.util
import json
import stat
import sys
from contextlib import ExitStack, nullcontext
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


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
    def sealed_coverage_context():
        class Seal:
            evidence = {"evidence_sets": []}

            @staticmethod
            def assert_unchanged():
                return None

            def revalidate_after_close(self):
                return self.evidence

        return nullcontext(Seal())

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
            "0.23.11",
            "scan-token",
        )
        end = runner.scanner_end_command(["scanner"], "scan-token")

        self.assertIn("/d:sonar.token=scan-token", begin)
        self.assertIn("/d:sonar.projectVersion=0.23.11", begin)
        self.assertIn("/d:sonar.token=scan-token", end)
        self.assertNotIn("scan-token", runner.redact(" ".join(begin), ("scan-token",)))

    def test_project_version_reads_pyproject_authority(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "pyproject.toml").write_text(
                '[project]\nversion = "0.23.11"\n', encoding="utf-8"
            )

            self.assertEqual(runner.project_version(root), "0.23.11")

    def test_project_version_rejects_missing_authority(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "pyproject.toml").write_text(
                '[project]\nname = "netcoredbg-mcp"\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(runner.RunnerError, "release version"):
                runner.project_version(root)

    def test_project_version_rejects_nonsemantic_authority(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            (root / "pyproject.toml").write_text(
                '[project]\nversion = "development"\n', encoding="utf-8"
            )

            with self.assertRaisesRegex(runner.RunnerError, "release version"):
                runner.project_version(root)

    def test_coverage_configuration_preserves_repository_relative_source_mappings(self):
        configuration = tomllib.loads(
            (RUNNER_PATH.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
        )

        self.assertEqual(
            configuration["tool"]["coverage"]["run"],
            {
                "branch": True,
                "include": ["src/netcoredbg_mcp/*"],
                "relative_files": True,
            },
        )

    def test_coverage_plan_is_side_effect_free_and_has_import_properties(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "scanner-worktree"
            root.mkdir()
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000002")

        self.assertFalse(plan.root.exists())
        self.assertEqual(
            {report.project for report in plan.dotnet_reports},
            set(runner.CLOSED_DOTNET_COVERAGE_PROJECTS),
        )
        self.assertIn(
            "tests/dotnet/NetCoreDbg.Mcp.Host.PromptTests/NetCoreDbg.Mcp.Host.PromptTests.csproj",
            runner.CLOSED_DOTNET_COVERAGE_PROJECTS,
        )
        self.assertIn(
            runner.HOST_REAL_PYTHON_TEST_PROJECT,
            runner.CLOSED_DOTNET_COVERAGE_PROJECTS,
        )
        self.assertEqual(
            runner.CLOSED_DOTNET_COVERAGE_PROJECTS.count(runner.HOST_REAL_PYTHON_TEST_PROJECT),
            1,
        )
        self.assertEqual(
            [report.normalized_path for report in plan.dotnet_reports],
            sorted(report.normalized_path for report in plan.dotnet_reports),
        )
        self.assertEqual(
            runner.coverage_scanner_properties(plan),
            (
                f"/d:sonar.python.coverage.reportPaths={plan.python_report.normalized_path}",
                "/d:sonar.cs.opencover.reportsPaths="
                + ",".join(report.normalized_path for report in plan.dotnet_reports),
            ),
        )

    def test_coverage_producer_commands_scrub_sonar_environment(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000006")
            environment = runner.coverage_environment(
                context, plan, {"SONAR_TOKEN": "must-not-reach-child", "SAFE": "kept"}
            )
            commands = runner.coverage_producer_commands(context, plan)

        self.assertEqual(environment["SAFE"], "kept")
        self.assertNotIn("SONAR_TOKEN", environment)
        self.assertEqual(environment["COVERAGE_FILE"], str(plan.root / "python" / ".coverage"))
        self.assertEqual(
            commands[:3],
            [
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
            ],
        )
        dotnet_commands = commands[3:]
        self.assertEqual(len(dotnet_commands), len(runner.CLOSED_DOTNET_COVERAGE_PROJECTS))
        for command, report in zip(dotnet_commands, plan.dotnet_reports, strict=True):
            self.assertEqual(command[:2], ["dotnet", "test"])
            self.assertIn(str(context.repository_root / report.project), command)
            self.assertIn("/p:CollectCoverage=true", command)
            self.assertIn("/p:CoverletOutputFormat=opencover", command)
            self.assertIn(f"/p:CoverletOutput={report.absolute_path}", command)
            self.assertIn("--no-build", command)
            self.assertIn("--no-restore", command)
        stateless = next(
            command
            for command, report in zip(dotnet_commands, plan.dotnet_reports, strict=True)
            if report.project
            == "host/NetCoreDbg.Mcp.Stateless.Tests/NetCoreDbg.Mcp.Stateless.Tests.csproj"
        )
        host_output = (
            context.repository_root
            / "host"
            / "NetCoreDbg.Mcp.Stateless"
            / "bin"
            / "Debug"
            / "net8.0"
        )
        self.assertIn(f"/p:IncludeDirectory={host_output}", stateless)
        self.assertFalse(any(argument.startswith("/p:Include=") for argument in stateless))
        self.assertNotIn("--filter", stateless)
        host_real_python = next(
            command
            for command, report in zip(dotnet_commands, plan.dotnet_reports, strict=True)
            if report.project == runner.HOST_REAL_PYTHON_TEST_PROJECT
        )
        self.assertEqual(host_real_python[-2:], ["--", "xUnit.ParallelizeTestCollections=false"])
        self.assertTrue(
            all(
                "xUnit.ParallelizeTestCollections=false" not in command
                for command in dotnet_commands
                if command is not host_real_python
            )
        )
        self.assertTrue(all("--filter" not in command for command in dotnet_commands))

    def test_vstest_guard_rejects_all_mtp_opt_ins(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            project = root / "host" / "Tests" / "Tests.csproj"
            project.parent.mkdir(parents=True)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)

            for configuration_path, contents in (
                (
                    project,
                    "<Project><PropertyGroup><UseMicrosoftTestingPlatformRunner>true</UseMicrosoftTestingPlatformRunner></PropertyGroup></Project>",
                ),
                (
                    project.parent / "Directory.Build.props",
                    "<Project><PropertyGroup><TestingPlatformDotnetTestSupport>true</TestingPlatformDotnetTestSupport></PropertyGroup></Project>",
                ),
                (
                    root / "Directory.Build.props",
                    "<Project><PropertyGroup><UseMicrosoftTestingPlatformRunner>true</UseMicrosoftTestingPlatformRunner></PropertyGroup></Project>",
                ),
                (root / "global.json", '{"test":{"runner":"Microsoft.Testing.Platform"}}'),
            ):
                project.write_text("<Project />", encoding="utf-8")
                configuration_path.parent.mkdir(parents=True, exist_ok=True)
                configuration_path.write_text(contents, encoding="utf-8")
                with self.subTest(configuration_path=configuration_path):
                    with self.assertRaisesRegex(
                        runner.CoverageFailureError, "COVERAGE_MTP_UNSUPPORTED"
                    ):
                        runner.assert_vstest_compatible(context, project)
                if configuration_path != project:
                    configuration_path.unlink()

    def test_coverage_producers_check_vstest_before_dotnet_launch(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000089")
            runner.claim_coverage_run(plan, context)
            environment = runner.coverage_environment(context, plan, {"SAFE": "kept"})
            calls = []

            def record(command, **_kwargs):
                calls.append(command)
                if command[:2] == ["uv", "sync"]:
                    runner.coverage_environment_directory(context, plan).mkdir(parents=True)

            with (
                patch.object(
                    runner,
                    "assert_vstest_compatible",
                    side_effect=runner.CoverageFailureError(
                        "dotnet_producer", "dotnet", "COVERAGE_MTP_UNSUPPORTED"
                    ),
                ) as guard,
                patch.object(runner, "run_coverage_process", side_effect=record),
                self.assertRaisesRegex(runner.CoverageFailureError, "COVERAGE_MTP_UNSUPPORTED"),
            ):
                runner.run_coverage_producers(
                    context, plan, environment, (), runner.time.monotonic() + 60
                )

        self.assertEqual(calls, runner.coverage_producer_commands(context, plan)[:3])
        guard.assert_called_once()

    def test_dotnet_coverage_environment_selects_external_python(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "scanner-worktree"
            coordination_root = Path(temporary_directory) / "coordination-root"
            root.mkdir()
            coordination_root.mkdir()
            context = runner.GitContext(
                root, coordination_root / ".git", root / ".git", coordination_root, "a" * 40
            )
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000011")
            environment = runner.coverage_environment(context, plan, {"SAFE": "kept"})
            dotnet_environment = runner.coverage_dotnet_environment(context, plan, environment)

        environment_root = runner.coverage_environment_directory(context, plan)
        expected_python = str(
            environment_root
            / (Path("Scripts") / "python.exe" if runner.os.name == "nt" else Path("bin") / "python")
        )
        self.assertNotIn("NETCOREDBG_MCP_PYTHON_EXECUTABLE", environment)
        self.assertNotIn("NETCOREDBG_MCP_TEST_PYTHON_EXECUTABLE", environment)
        self.assertEqual(dotnet_environment["NETCOREDBG_MCP_PYTHON_EXECUTABLE"], expected_python)
        self.assertEqual(
            dotnet_environment["NETCOREDBG_MCP_TEST_PYTHON_EXECUTABLE"], expected_python
        )

    def test_coverage_environment_is_external_and_cleanup_is_scoped(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "scanner-worktree"
            coordination_root = Path(temporary_directory) / "coordination-root"
            root.mkdir()
            coordination_root.mkdir()
            context = runner.GitContext(
                root, coordination_root / ".git", root / ".git", coordination_root, "a" * 40
            )
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000007")
            environment = runner.coverage_environment(context, plan, {"SAFE": "kept"})
            coverage_environment = Path(environment["UV_PROJECT_ENVIRONMENT"])
            coverage_environment.mkdir(parents=True)
            (coverage_environment / "marker.txt").write_text("owned", encoding="utf-8")
            claim = runner.capture_coverage_environment_claim(context, plan)

            runner.clear_coverage_environment(context, plan, claim=claim)

            self.assertFalse(coverage_environment.exists())
            self.assertTrue((coordination_root / ".agent" / "tmp").is_dir())

    def test_coverage_environment_cleanup_refuses_same_path_replacement(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "scanner-worktree"
            coordination_root = Path(temporary_directory) / "coordination-root"
            root.mkdir()
            coordination_root.mkdir()
            context = runner.GitContext(
                root, coordination_root / ".git", root / ".git", coordination_root, "a" * 40
            )
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000071")
            environment_path = runner.coverage_environment_directory(context, plan)
            environment_path.mkdir(parents=True)
            (environment_path / "owned.txt").write_text("owned", encoding="utf-8")
            claim = runner.capture_coverage_environment_claim(context, plan)
            replacement = coordination_root / "replacement-environment"
            environment_path.rename(replacement)
            environment_path.mkdir(parents=True)
            (environment_path / "replacement.txt").write_text("replacement", encoding="utf-8")

            with self.assertRaisesRegex(
                runner.CoverageFailureError, "COVERAGE_ENVIRONMENT_IDENTITY_DRIFT"
            ):
                runner.clear_coverage_environment(context, plan, claim=claim)

            self.assertTrue((environment_path / "replacement.txt").is_file())
            self.assertTrue((replacement / "owned.txt").is_file())

    def test_produce_coverage_cleans_external_environment_after_failure(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "scanner-worktree"
            coordination_root = Path(temporary_directory) / "coordination-root"
            root.mkdir()
            coordination_root.mkdir()
            context = runner.GitContext(
                root, coordination_root / ".git", root / ".git", coordination_root, "a" * 40
            )
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000009")
            runner.claim_coverage_run(plan, context)
            coverage_environment = runner.coverage_environment_directory(context, plan)
            coverage_environment.mkdir(parents=True)

            with patch.object(
                runner,
                "run_coverage_producers",
                side_effect=runner.CoverageFailureError(
                    "python_producer", "python", "COVERAGE_PROCESS_FAILED"
                ),
            ):
                with self.assertRaisesRegex(runner.CoverageFailureError, "COVERAGE_PROCESS_FAILED"):
                    runner.produce_coverage(context, plan, {"SAFE": "kept"}, ())

            self.assertFalse(coverage_environment.exists())

    def test_produce_coverage_preserves_failure_when_environment_cleanup_fails(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "scanner-worktree"
            coordination_root = Path(temporary_directory) / "coordination-root"
            root.mkdir()
            coordination_root.mkdir()
            context = runner.GitContext(
                root, coordination_root / ".git", root / ".git", coordination_root, "a" * 40
            )
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000075")
            environment_path = runner.coverage_environment_directory(context, plan)
            environment_path.mkdir(parents=True)
            producer_failure = runner.CoverageFailureError(
                "python_producer", "python", "COVERAGE_PROCESS_FAILED"
            )
            cleanup_failure = runner.CoverageFailureError(
                "python_producer", "python", "COVERAGE_ENVIRONMENT_CLEANUP_FAILED"
            )

            with (
                patch.object(runner, "run_coverage_producers", side_effect=producer_failure),
                patch.object(runner, "clear_coverage_environment", side_effect=cleanup_failure),
                self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_PROCESS_FAILED"
                ) as raised,
            ):
                runner.produce_coverage(context, plan, {"SAFE": "kept"}, ())

            self.assertEqual(
                raised.exception.cleanup_failure,
                {
                    "path": "coverage-environment",
                    "operation": "cleanup",
                    "error_type": "COVERAGE_ENVIRONMENT_CLEANUP_FAILED",
                },
            )

    def test_produce_coverage_withholds_environment_cleanup_for_unproven_owner(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "scanner-worktree"
            coordination_root = Path(temporary_directory) / "coordination-root"
            root.mkdir()
            coordination_root.mkdir()
            context = runner.GitContext(
                root, coordination_root / ".git", root / ".git", coordination_root, "a" * 40
            )
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000076")
            environment_path = runner.coverage_environment_directory(context, plan)
            environment_path.mkdir(parents=True)
            owner_failure = runner.CoverageFailureError(
                "process_owner",
                "python",
                "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST",
                owner=runner.CoverageTreeObservation("windows", "job_object", None),
                artifact_cleanup_permitted=False,
            )

            with (
                patch.object(runner, "run_coverage_producers", side_effect=owner_failure),
                patch.object(
                    runner,
                    "clear_coverage_environment",
                    side_effect=AssertionError("cleanup must be withheld"),
                ) as cleanup,
                self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST"
                ),
            ):
                runner.produce_coverage(context, plan, {"SAFE": "kept"}, ())

            cleanup.assert_not_called()
            self.assertTrue(environment_path.is_dir())

    def test_produce_coverage_rejects_reparse_environment_ancestor_before_child(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory) / "scanner-worktree"
            coordination_root = Path(temporary_directory) / "coordination-root"
            root.mkdir()
            coordination_root.mkdir()
            context = runner.GitContext(
                root, coordination_root / ".git", root / ".git", coordination_root, "a" * 40
            )
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000080")
            agent_root = coordination_root / ".agent"
            agent_root.mkdir()
            original_lstat = type(agent_root).lstat

            class ReparseMetadata:
                def __init__(self, metadata):
                    self.st_mode = metadata.st_mode
                    self.st_dev = metadata.st_dev
                    self.st_ino = metadata.st_ino
                    self.st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

            def nonfollowing_lstat(path):
                metadata = original_lstat(path)
                return ReparseMetadata(metadata) if path == agent_root else metadata

            with (
                patch.object(type(agent_root), "lstat", new=nonfollowing_lstat),
                patch.object(
                    runner,
                    "run_coverage_producers",
                    side_effect=AssertionError("producer must not start"),
                ) as producers,
                self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_ENVIRONMENT_IDENTITY_DRIFT"
                ),
            ):
                runner.produce_coverage(context, plan, {"SAFE": "kept"}, ())

            producers.assert_not_called()

    def test_run_coverage_producers_owns_all_commands(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000008")
            runner.claim_coverage_run(plan, context)
            environment = runner.coverage_environment(
                context, plan, {"SONAR_TOKEN": "must-not-reach-child", "SAFE": "kept"}
            )
            stateless_output = (
                root / "host" / "NetCoreDbg.Mcp.Stateless" / "bin" / "Debug" / "net8.0"
            )
            stateless_output.mkdir(parents=True)
            (stateless_output / "NetCoreDbg.Mcp.Stateless.dll").write_bytes(b"dll")
            (stateless_output / "NetCoreDbg.Mcp.Stateless.pdb").write_bytes(b"pdb")

            calls = []

            def record(command, **kwargs):
                calls.append((command, kwargs))
                if command[:2] == ["uv", "sync"]:
                    runner.coverage_environment_directory(context, plan).mkdir(parents=True)

            with patch.object(runner, "run_coverage_process", side_effect=record):
                runner.run_coverage_producers(
                    context,
                    plan,
                    environment,
                    (),
                    runner.time.monotonic() + 60,
                )
            self.assertTrue(all(report.absolute_path.parent.exists() for report in plan.reports))

        self.assertEqual(
            [command for command, _ in calls], runner.coverage_producer_commands(context, plan)
        )
        self.assertTrue(
            all(
                "SONAR_TOKEN" not in kwargs["environment"]
                and kwargs["deadline"] > runner.time.monotonic()
                for _, kwargs in calls
            )
        )
        self.assertTrue(
            all(
                "NETCOREDBG_MCP_PYTHON_EXECUTABLE" not in kwargs["environment"]
                and "NETCOREDBG_MCP_TEST_PYTHON_EXECUTABLE" not in kwargs["environment"]
                for _, kwargs in calls[:3]
            )
        )
        self.assertTrue(
            all(
                kwargs["environment"]["NETCOREDBG_MCP_PYTHON_EXECUTABLE"]
                == kwargs["environment"]["NETCOREDBG_MCP_TEST_PYTHON_EXECUTABLE"]
                for _, kwargs in calls[3:]
            )
        )

    def test_stateless_producer_rejects_unrestored_host_artifacts(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000090")
            runner.claim_coverage_run(plan, context)
            environment = runner.coverage_environment(context, plan, {"SAFE": "kept"})
            host_output = root / "host" / "NetCoreDbg.Mcp.Stateless" / "bin" / "Debug" / "net8.0"
            host_output.mkdir(parents=True)
            host_dll = host_output / "NetCoreDbg.Mcp.Stateless.dll"
            host_dll.write_bytes(b"original dll")
            (host_output / "NetCoreDbg.Mcp.Stateless.pdb").write_bytes(b"original pdb")

            def record(command, **_kwargs):
                if command[:2] == ["uv", "sync"]:
                    runner.coverage_environment_directory(context, plan).mkdir(parents=True)
                if any("NetCoreDbg.Mcp.Stateless.Tests" in argument for argument in command):
                    host_dll.write_bytes(b"mutated dll")

            with (
                patch.object(runner, "run_coverage_process", side_effect=record),
                self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_STATELESS_HOST_RESTORATION_FAILED"
                ),
            ):
                runner.run_coverage_producers(
                    context, plan, environment, (), runner.time.monotonic() + 60
                )

    def test_report_parent_creation_rejects_reparse_before_producer(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000082")
            runner.claim_coverage_run(plan, context)
            claim = runner.capture_coverage_run_claim(plan, context)
            report_parent = plan.reports[0].absolute_path.parent
            report_parent.mkdir(parents=True)
            original_lstat = type(report_parent).lstat

            class ReparseMetadata:
                def __init__(self, metadata):
                    self.st_mode = metadata.st_mode
                    self.st_dev = metadata.st_dev
                    self.st_ino = metadata.st_ino
                    self.st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

            def nonfollowing_lstat(path):
                metadata = original_lstat(path)
                return ReparseMetadata(metadata) if path == report_parent else metadata

            with (
                patch.object(type(report_parent), "lstat", new=nonfollowing_lstat),
                patch.object(
                    runner,
                    "run_coverage_process",
                    side_effect=AssertionError("producer must not start"),
                ) as producer,
                self.assertRaisesRegex(runner.CoverageFailureError, "COVERAGE_SYMLINK_REJECTED"),
            ):
                runner.run_coverage_producers(
                    context,
                    plan,
                    runner.coverage_environment(context, plan, {"SAFE": "kept"}),
                    (),
                    runner.time.monotonic() + 60,
                    claim=claim,
                )

            producer.assert_not_called()

    def test_coverage_producers_refuse_claim_drift_between_children(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000073")
            runner.claim_coverage_run(plan, context)
            claim = runner.capture_coverage_run_claim(plan, context)
            environment = runner.coverage_environment(context, plan, {"SAFE": "kept"})
            calls = []

            def record(command, **kwargs):
                calls.append((command, kwargs))
                if len(calls) == 1:
                    runner.coverage_environment_directory(context, plan).mkdir(parents=True)
                    replacement = root / "replaced-coverage-run"
                    plan.root.rename(replacement)
                    plan.root.mkdir()
                    plan.marker_path.write_bytes(
                        runner.canonical_coverage_marker_bytes(plan, context)
                    )

            with patch.object(runner, "run_coverage_process", side_effect=record):
                with self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_RUN_IDENTITY_DRIFT"
                ):
                    runner.run_coverage_producers(
                        context,
                        plan,
                        environment,
                        (),
                        runner.time.monotonic() + 60,
                        claim=claim,
                    )

            self.assertEqual(len(calls), 1)

    def test_coverage_producers_refuse_environment_drift_between_children(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000074")
            runner.claim_coverage_run(plan, context)
            claim = runner.capture_coverage_run_claim(plan, context)
            environment = runner.coverage_environment(context, plan, {"SAFE": "kept"})
            environment_path = runner.coverage_environment_directory(context, plan)
            calls = []
            original_capture = runner.capture_coverage_environment_claim

            def record(command, **kwargs):
                calls.append((command, kwargs))
                if len(calls) == 1:
                    environment_path.mkdir(parents=True)

            def capture_environment(_context, _plan):
                environment_claim = original_capture(_context, _plan)
                replacement = root / "replaced-environment"
                environment_path.rename(replacement)
                environment_path.mkdir(parents=True)
                return environment_claim

            with (
                patch.object(runner, "run_coverage_process", side_effect=record),
                patch.object(
                    runner,
                    "capture_coverage_environment_claim",
                    side_effect=capture_environment,
                ),
            ):
                with self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_ENVIRONMENT_IDENTITY_DRIFT"
                ):
                    runner.run_coverage_producers(
                        context,
                        plan,
                        environment,
                        (),
                        runner.time.monotonic() + 60,
                        claim=claim,
                    )

            self.assertEqual(len(calls), 1)

    def test_produce_coverage_binds_owned_producers_and_evidence(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000009")
            expected = {"evidence_sets": [{"language": "dotnet"}, {"language": "python"}]}
            with (
                patch.object(runner, "run_coverage_producers", return_value=None) as producers,
                patch.object(runner, "validate_coverage_evidence", return_value=expected),
            ):
                evidence = runner.produce_coverage(context, plan, {"SONAR_TOKEN": "secret"}, ())

        self.assertEqual(evidence, expected)
        producers.assert_called_once()
        self.assertNotIn("SONAR_TOKEN", producers.call_args.kwargs["environment"])

    def test_produce_coverage_uses_dedicated_producer_deadline(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000010")
            with (
                patch.object(runner.time, "monotonic", return_value=100.0),
                patch.object(runner, "run_coverage_producers", return_value=None) as producers,
                patch.object(
                    runner, "validate_coverage_evidence", return_value={"evidence_sets": []}
                ),
            ):
                runner.produce_coverage(context, plan, {"SAFE": "kept"}, ())

        self.assertEqual(
            producers.call_args.kwargs["deadline"],
            100.0 + runner.COVERAGE_PRODUCER_TIMEOUT_SECONDS,
        )

    def test_coverage_owner_refuses_non_windows(self):
        current_directory = Path.cwd()
        with (
            patch.object(runner.os, "name", "posix"),
            self.assertRaisesRegex(
                runner.CoverageTreeOwnerUnavailableError,
                "COVERAGE_PROCESS_TREE_OWNER_UNAVAILABLE",
            ),
        ):
            runner.CoverageTreeOwner.start_and_ack(
                ["producer"], cwd=current_directory, environment={}
            )

    def test_windows_coverage_owner_terminates_child_tree(self):
        if runner.os.name != "nt":
            self.skipTest("Windows Job Object fixture")

        import psutil

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            child_identity = root / "child.pid"
            child_script = (
                "import os, pathlib, sys, time; "
                "pathlib.Path(sys.argv[1]).write_text(str(os.getpid()), encoding='utf-8'); "
                "time.sleep(60)"
            )
            root_script = (
                "import subprocess, sys, time; "
                f"subprocess.Popen([sys.executable, '-c', {child_script!r}, sys.argv[1]]); "
                "time.sleep(60)"
            )
            owner = runner.CoverageTreeOwner.start_and_ack(
                [sys.executable, "-c", root_script, str(child_identity)],
                cwd=root,
                environment=dict(runner.os.environ),
            )
            try:
                deadline = runner.time.monotonic() + 5
                while not child_identity.exists() and runner.time.monotonic() < deadline:
                    runner.time.sleep(0.02)
                self.assertTrue(child_identity.exists(), "owned child did not start")
                child_pid = int(child_identity.read_text(encoding="utf-8"))
                outcome = owner.abort_and_wait_empty(runner.time.monotonic() + 5)
                self.assertEqual(outcome.owner.terminal_state, "TREE_EMPTY")
                deadline = runner.time.monotonic() + 5
                while psutil.pid_exists(child_pid) and runner.time.monotonic() < deadline:
                    runner.time.sleep(0.02)
                self.assertFalse(
                    psutil.pid_exists(child_pid), "owned child survived Job Object cleanup"
                )
            finally:
                if owner.observation.terminal_state != "TREE_EMPTY":
                    try:
                        owner.abort_and_wait_empty(runner.time.monotonic() + 5)
                    except (
                        runner.CoverageTreeOwnershipLostError,
                        runner.subprocess.TimeoutExpired,
                    ):
                        owner.discard_unproven()

    def test_scanner_begin_command_includes_coverage_properties(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000005")
            command = runner.scanner_begin_command(
                ["scanner"],
                root / "SonarQube.Analysis.xml",
                "https://sonar.example.test",
                context.head,
                "0.23.11",
                "scan-token",
                plan,
            )

        self.assertEqual(
            [argument for argument in command if argument.startswith("/d:sonar.")][-3:-1],
            list(runner.coverage_scanner_properties(plan)),
        )

    def test_coverage_claim_writes_and_validates_marker(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000003")

            marker_sha256 = runner.claim_coverage_run(plan, context)

            self.assertEqual(
                plan.marker_path.read_bytes(), runner.canonical_coverage_marker_bytes(plan, context)
            )
            self.assertEqual(runner.validate_coverage_marker(plan, context), marker_sha256)
            stale_plan = runner.derive_coverage_plan(
                context, "00000000-0000-4000-8000-000000000004"
            )
            stale_plan.root.mkdir(parents=True)
            with self.assertRaisesRegex(runner.CoverageFailureError, "COVERAGE_RUN_IDENTITY_DRIFT"):
                runner.claim_coverage_run(stale_plan, context)

    def test_coverage_run_claim_rejects_reparse_parent_before_creation(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000081")
            temporary_root = root / ".tmp"
            temporary_root.mkdir()
            original_lstat = type(temporary_root).lstat

            class ReparseMetadata:
                def __init__(self, metadata):
                    self.st_mode = metadata.st_mode
                    self.st_dev = metadata.st_dev
                    self.st_ino = metadata.st_ino
                    self.st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

            def nonfollowing_lstat(path):
                metadata = original_lstat(path)
                return ReparseMetadata(metadata) if path == temporary_root else metadata

            with patch.object(type(temporary_root), "lstat", new=nonfollowing_lstat):
                with self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_RUN_IDENTITY_DRIFT"
                ):
                    runner.claim_coverage_run(plan, context)

            self.assertFalse(plan.root.parent.exists())

    def test_claimed_coverage_run_cleanup_preserves_siblings(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000012")
            runner.claim_coverage_run(plan, context)
            claim = runner.capture_coverage_run_claim(plan, context)
            sibling_run = plan.root.parent / "other-run"
            sibling_run.mkdir()
            sibling_marker = sibling_run / "marker.txt"
            sibling_marker.write_text("preserve", encoding="utf-8")
            unrelated = root / ".tmp" / "unrelated.txt"
            unrelated.write_text("preserve", encoding="utf-8")

            with patch.object(runner, "is_tracked", return_value=False):
                removed = runner.clear_claimed_coverage_run(plan, context, {}, claim=claim)

            self.assertEqual(
                removed,
                [".tmp/sonarqube-coverage/00000000-0000-4000-8000-000000000012"],
            )
            self.assertFalse(plan.root.exists())
            self.assertEqual(sibling_marker.read_text(encoding="utf-8"), "preserve")
            self.assertEqual(unrelated.read_text(encoding="utf-8"), "preserve")

    def test_claimed_coverage_run_cleanup_refuses_same_path_replacement(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000072")
            runner.claim_coverage_run(plan, context)
            claim = runner.capture_coverage_run_claim(plan, context)
            replacement = root / "replaced-coverage-run"
            plan.root.rename(replacement)
            plan.root.mkdir()
            plan.marker_path.write_bytes(runner.canonical_coverage_marker_bytes(plan, context))

            with self.assertRaisesRegex(runner.CoverageFailureError, "COVERAGE_RUN_IDENTITY_DRIFT"):
                runner.clear_claimed_coverage_run(plan, context, {}, claim=claim)

            self.assertTrue(plan.marker_path.is_file())
            self.assertTrue((replacement / "coverage-run.json").is_file())

    def test_claimed_coverage_run_refuses_replaced_transcript_parent(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000077")
            runner.claim_coverage_run(plan, context)
            claim = runner.capture_coverage_run_claim(plan, context)
            transcript_parent = plan.redacted_transcript_path.parent
            self.assertTrue(transcript_parent.is_dir())
            replacement = root / "replaced-transcript-parent"
            transcript_parent.rename(replacement)
            transcript_parent.mkdir()

            with self.assertRaisesRegex(runner.CoverageFailureError, "COVERAGE_RUN_IDENTITY_DRIFT"):
                claim.assert_current()

            self.assertTrue(transcript_parent.is_dir())
            self.assertTrue(replacement.is_dir())

    def test_claimed_path_rejects_reparse_intermediate_component(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000078")
            runner.claim_coverage_run(plan, context)
            report_parent = plan.python_report.absolute_path.parent
            report_parent.mkdir(parents=True)
            original_lstat = type(report_parent).lstat

            class ReparseMetadata:
                def __init__(self, metadata):
                    self.st_mode = metadata.st_mode
                    self.st_dev = metadata.st_dev
                    self.st_ino = metadata.st_ino
                    self.st_file_attributes = stat.FILE_ATTRIBUTE_REPARSE_POINT

            def nonfollowing_lstat(path):
                metadata = original_lstat(path)
                return ReparseMetadata(metadata) if path == report_parent else metadata

            with patch.object(type(report_parent), "lstat", new=nonfollowing_lstat):
                with self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_SYMLINK_REJECTED"
                ):
                    runner._assert_claimed_coverage_path(report_parent, plan, context)

    def test_coverage_evidence_binds_reports_sources_and_marker(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000007")
            runner.claim_coverage_run(plan, context)
            python_source = root / "src" / "netcoredbg_mcp" / "module.py"
            host_source = root / "host" / "NetCoreDbg.Mcp.Host" / "Program.cs"
            stateless_source = root / "host" / "NetCoreDbg.Mcp.Stateless" / "Program.cs"
            for source in (python_source, host_source, stateless_source):
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("source", encoding="utf-8")
            plan.python_report.absolute_path.parent.mkdir(parents=True)
            plan.python_report.absolute_path.write_text(
                '<coverage lines-valid="1"><packages><package><classes><class filename="src/netcoredbg_mcp/module.py" /></classes></package></packages></coverage>',
                encoding="utf-8",
            )
            for report in plan.dotnet_reports:
                report.absolute_path.parent.mkdir(parents=True, exist_ok=True)
                source = (
                    stateless_source
                    if report.project
                    and report.project.endswith("NetCoreDbg.Mcp.Stateless.Tests.csproj")
                    else host_source
                )
                report.absolute_path.write_text(
                    '<CoverageSession><Summary numSequencePoints="1" /><Modules><Module><Files><File uid="1" fullPath="'
                    + str(source)
                    + '" /></Files></Module></Modules></CoverageSession>',
                    encoding="utf-8",
                )

            evidence = runner.validate_coverage_evidence(plan, context)

            self.assertEqual(
                [item["language"] for item in evidence["evidence_sets"]], ["dotnet", "python"]
            )
            self.assertEqual(
                evidence["evidence_sets"][0]["marker_sha256"],
                runner.validate_coverage_marker(plan, context),
            )
            plan.python_report.absolute_path.unlink()
            with self.assertRaisesRegex(runner.CoverageFailureError, "COVERAGE_REPORT_MISSING"):
                runner.validate_coverage_evidence(plan, context)

    def test_windows_sealed_coverage_reports_block_rewrites(self):
        if runner.os.name != "nt":
            self.skipTest("Windows report sealing fixture")

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000010")
            runner.claim_coverage_run(plan, context)
            python_source = root / "src" / "netcoredbg_mcp" / "module.py"
            host_source = root / "host" / "NetCoreDbg.Mcp.Host" / "Program.cs"
            stateless_source = root / "host" / "NetCoreDbg.Mcp.Stateless" / "Program.cs"
            for source in (python_source, host_source, stateless_source):
                source.parent.mkdir(parents=True, exist_ok=True)
                source.write_text("source", encoding="utf-8")
            plan.python_report.absolute_path.parent.mkdir(parents=True)
            plan.python_report.absolute_path.write_text(
                '<coverage lines-valid="1"><packages><package><classes><class filename="src/netcoredbg_mcp/module.py" /></classes></package></packages></coverage>',
                encoding="utf-8",
            )
            for report in plan.dotnet_reports:
                report.absolute_path.parent.mkdir(parents=True, exist_ok=True)
                source = (
                    stateless_source
                    if report.project
                    and report.project.endswith("NetCoreDbg.Mcp.Stateless.Tests.csproj")
                    else host_source
                )
                report.absolute_path.write_text(
                    '<CoverageSession><Summary numSequencePoints="1" /><Modules><Module><Files><File uid="1" fullPath="'
                    + str(source)
                    + '" /></Files></Module></Modules></CoverageSession>',
                    encoding="utf-8",
                )

            original_bytes = plan.python_report.absolute_path.read_bytes()
            with runner.sealed_coverage_evidence(plan, context) as sealed:
                with self.assertRaises(OSError):
                    plan.python_report.absolute_path.write_text("forged", encoding="utf-8")
                sealed.assert_unchanged()

            replacement = plan.python_report.absolute_path.with_name("replacement.opencover.xml")
            replacement.write_bytes(original_bytes)
            plan.python_report.absolute_path.unlink()
            replacement.replace(plan.python_report.absolute_path)
            with self.assertRaisesRegex(
                runner.CoverageFailureError, "COVERAGE_REPORT_SEAL_IDENTITY_DRIFT"
            ):
                sealed.revalidate_after_close()

    def test_windows_report_seal_closes_stream_when_identity_capture_refuses(self):
        if runner.os.name != "nt":
            self.skipTest("Windows report sealing fixture")

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = runner.GitContext(root, root / "common", root / "git", root, "a" * 40)
            plan = runner.derive_coverage_plan(context, "00000000-0000-4000-8000-000000000079")
            runner.claim_coverage_run(plan, context)
            report = plan.python_report
            report.absolute_path.parent.mkdir(parents=True)
            report.absolute_path.write_bytes(b"report")
            streams = []
            original_fdopen = runner.os.fdopen
            failure = runner.CoverageFailureError(
                "report_validation", "python", "COVERAGE_REPORT_SEAL_IDENTITY_DRIFT"
            )

            def retain_fdopen(*args, **kwargs):
                stream = original_fdopen(*args, **kwargs)
                streams.append(stream)
                return stream

            try:
                with (
                    patch.object(runner.os, "fdopen", side_effect=retain_fdopen),
                    patch.object(
                        runner,
                        "_capture_coverage_path_identity",
                        side_effect=failure,
                    ),
                    self.assertRaisesRegex(
                        runner.CoverageFailureError, "COVERAGE_REPORT_SEAL_IDENTITY_DRIFT"
                    ),
                ):
                    runner._seal_coverage_report(plan, context, report)

                self.assertEqual(len(streams), 1)
                self.assertTrue(streams[0].closed)
            finally:
                for stream in streams:
                    if not stream.closed:
                        stream.close()

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
                '<Property Name="sonar.projectVersion">0.23.11</Property>'
                "</LocalSettings>"
                "</SonarQubeAnalysisConfig>",
                encoding="utf-8",
            )

            metadata = runner.scanner_metadata(root, "a" * 40, "0.23.11")

        self.assertEqual(
            (
                metadata["project_key"],
                metadata["sonar_scm_revision"],
                metadata["sonar_project_version"],
            ),
            (runner.PROJECT_KEY, "a" * 40, "0.23.11"),
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

    def test_project_inventory_excludes_virtual_environment_projects(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            solution_project = root / "host" / "App.csproj"
            standalone_project = root / "tools" / "Tool.csproj"
            virtual_environment_project = (
                root / ".venv" / "Lib" / "site-packages" / "package" / "Package.csproj"
            )
            for project in (solution_project, standalone_project, virtual_environment_project):
                project.parent.mkdir(parents=True, exist_ok=True)
                project.write_text("<Project />", encoding="utf-8")
            (root / "netcoredbg-mcp.sln").write_text(
                'Project("{guid}") = "App", "host\\App.csproj", "{id}"\nEndProject\n',
                encoding="utf-8",
            )

            _, projects, standalone_projects = runner.project_inventory(root)

        self.assertEqual(projects, [solution_project.resolve(), standalone_project.resolve()])
        self.assertEqual(standalone_projects, [standalone_project.resolve()])

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

            def wait_for_ce_task(_host, task_id, _token, _receipt, *, lease_checkpoint):
                lease_checkpoint(task_id, "2026-08-27T00:10:00Z")
                return "analysis-1"

            with ExitStack() as patches:
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
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
                    patch.object(runner, "project_version", return_value="0.23.11")
                )
                patches.enter_context(
                    patch.object(runner, "claim_coverage_run", return_value="marker")
                )
                patches.enter_context(
                    patch.object(runner, "capture_coverage_run_claim", return_value=object())
                )
                patches.enter_context(
                    patch.object(runner, "clear_claimed_coverage_run", return_value=[])
                )
                patches.enter_context(
                    patch.object(runner, "produce_coverage", return_value={"evidence_sets": []})
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "validate_coverage_evidence",
                        return_value={"evidence_sets": []},
                    )
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "sealed_coverage_evidence",
                        return_value=self.sealed_coverage_context(),
                    )
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
                    patch.object(runner, "wait_for_ce_task", side_effect=wait_for_ce_task)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "analysis_coverage_binding",
                        return_value={"analysis_id": "analysis-1"},
                    )
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
        self.assertEqual(blocked_receipt["quality_gate"], error_quality_gate)
        self.assertEqual(blocked_receipt["post_scan_issues"], full_inventory)
        self.assertEqual(blocked_receipt["new_code_issues"], new_code_inventory)
        self.assertEqual(blocked_receipt["new_code_issues"]["total"], 2)
        self.assertEqual(
            blocked_receipt["quality_gate"]["conditions"][0]["actualValue"],
            "137",
        )
        self.assertEqual(blocked_receipt["analysis_current_after_issues"], binding)
        self.assertEqual(blocked_receipt["hotspots"], {"records": []})
        self.assertEqual(blocked_receipt["generated_artifacts_removed_after_scan"], ["obj"])
        self.assertEqual(
            blocked_receipt["cleanup"],
            {"status": "PASS", "removed": ["obj"]},
        )
        self.assertEqual(blocked_receipt["analysis_current_final"], binding)
        self.assertEqual(blocked_receipt["post_scan_head"], head)

    def test_execute_binds_coverage_transaction_before_scanner_end(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            head = "a" * 40
            context = runner.GitContext(root, root / "common", root / "git", root, head)
            events = []

            class Seal:
                evidence = {"evidence_sets": ["sealed"]}

                def __enter__(self):
                    events.append("seal_enter")
                    return self

                def assert_unchanged(self):
                    events.append("seal_verify")

                def __exit__(self, *_args):
                    events.append("seal_exit")
                    return False

                def revalidate_after_close(self):
                    self_outer.assertEqual(events[-1], "seal_exit")
                    events.append("revalidate")
                    return self.evidence

            def run_process(command, **_kwargs):
                if command[:2] == ["scanner", "begin"]:
                    events.append("begin")
                elif command[:2] == ["scanner", "end"]:
                    events.append("end")

            def claim(_plan, _context):
                events.append("claim")

            coverage_claim = object()

            def produce(_context, _plan, _environment, _secrets, *, claim):
                events.append("produce")
                self.assertIs(claim, coverage_claim)
                return {"evidence_sets": ["unsealed"]}

            def seal(_plan, _context, *, claim):
                self.assertIs(claim, coverage_claim)
                return Seal()

            self_outer = self

            def cleanup_claimed_run(_plan, _context, _environment, *, claim):
                self.assertIs(claim, coverage_claim)
                self.assertEqual(events[-1], "revalidate")
                events.append("claimed_run_cleanup")
                return [".tmp/sonarqube-coverage/claimed-run"]

            original_write_scan_lease = runner.write_scan_lease

            def write_scan_lease(path, lease):
                events.append(f"lease:{lease.state}")
                original_write_scan_lease(path, lease)

            with ExitStack() as patches:
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
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
                    patch.object(runner, "project_version", return_value="0.23.11")
                )
                patches.enter_context(
                    patch.object(runner, "discover_scanner", return_value=["scanner"])
                )
                patches.enter_context(
                    patch.object(runner, "issue_inventory", return_value={"records": []})
                )
                patches.enter_context(patch.object(runner, "scanner_environment", return_value={}))
                patches.enter_context(patch.object(runner, "run_process", side_effect=run_process))
                patches.enter_context(patch.object(runner, "claim_coverage_run", side_effect=claim))
                patches.enter_context(
                    patch.object(runner, "capture_coverage_run_claim", return_value=coverage_claim)
                )
                patches.enter_context(patch.object(runner, "produce_coverage", side_effect=produce))
                patches.enter_context(
                    patch.object(
                        runner,
                        "clear_claimed_coverage_run",
                        side_effect=cleanup_claimed_run,
                        create=True,
                    )
                )
                patches.enter_context(
                    patch.object(runner, "sealed_coverage_evidence", side_effect=seal)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "scanner_metadata",
                        return_value={
                            "observed": True,
                            "project_key": runner.PROJECT_KEY,
                            "sonar_scm_revision": head,
                            "sonar_project_version": "0.23.11",
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
                    patch.object(
                        runner, "report_task", side_effect=runner.RunnerError("stop after sealing")
                    )
                )
                patches.enter_context(
                    patch.object(runner, "write_scan_lease", side_effect=write_scan_lease)
                )
                patches.enter_context(patch.object(runner, "write_receipt"))
                with self.assertRaisesRegex(runner.RunnerError, "stop after sealing"):
                    runner.execute("candidate", "scanner")

        self.assertEqual(
            events,
            [
                "lease:ACQUIRED",
                "begin",
                "claim",
                "produce",
                "seal_enter",
                "lease:SCANNER_END_IN_FLIGHT",
                "end",
                "seal_verify",
                "seal_exit",
                "revalidate",
                "claimed_run_cleanup",
            ],
        )

    def test_execute_withholds_claimed_root_cleanup_for_unproven_producer_owner(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            head = "a" * 40
            context = runner.GitContext(root, root / "common", root / "git", root, head)
            receipts = []
            coverage_claim = object()
            producer_failure = runner.CoverageFailureError(
                "process_owner",
                "python",
                "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST",
                owner=runner.CoverageTreeObservation("windows", "job_object", None),
                artifact_cleanup_permitted=False,
            )

            class Lease:
                state = "ACQUIRED"

            class Handle:
                lease = Lease()

                def write_receipt(self, _target, receipt, _secrets):
                    receipts.append(json.loads(json.dumps(receipt)))

                def checkpoint(self, *_args, **_kwargs):
                    return None

            def project_lock(*_args, **_kwargs):
                return nullcontext(Handle())

            with ExitStack() as patches:
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
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
                    patch.object(runner, "project_version", return_value="0.23.11")
                )
                patches.enter_context(
                    patch.object(runner, "discover_scanner", return_value=["scanner"])
                )
                patches.enter_context(
                    patch.object(runner, "issue_inventory", return_value={"records": []})
                )
                patches.enter_context(patch.object(runner, "scanner_environment", return_value={}))
                patches.enter_context(patch.object(runner, "run_process"))
                patches.enter_context(patch.object(runner, "claim_coverage_run"))
                patches.enter_context(
                    patch.object(runner, "capture_coverage_run_claim", return_value=coverage_claim)
                )
                patches.enter_context(
                    patch.object(runner, "scanner_metadata", return_value={"observed": True})
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "project_inventory",
                        return_value=(root / "netcoredbg-mcp.sln", [], []),
                    )
                )
                patches.enter_context(
                    patch.object(runner, "produce_coverage", side_effect=producer_failure)
                )
                cleanup = patches.enter_context(
                    patch.object(
                        runner,
                        "clear_claimed_coverage_run",
                        side_effect=AssertionError("cleanup must be withheld"),
                    )
                )
                patches.enter_context(
                    patch.object(runner, "project_lock", side_effect=project_lock)
                )
                with self.assertRaisesRegex(
                    runner.CoverageFailureError, "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST"
                ):
                    runner.execute("candidate", "scanner")

            blocked = receipts[-1]
            cleanup.assert_not_called()
            self.assertEqual(blocked["failure"], "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST")
            self.assertEqual(
                blocked["coverage_run_cleanup"],
                {
                    "status": "BLOCKED",
                    "removed": [],
                    "failure": {"error": "COVERAGE_ARTIFACT_CLEANUP_WITHHELD"},
                },
            )
            self.assertEqual(
                blocked["coverage_failure"],
                {
                    "stage": "process_owner",
                    "language": "python",
                    "failure_code": "COVERAGE_PROCESS_TREE_OWNERSHIP_LOST",
                    "owner": {
                        "target_platform": "windows",
                        "owner_kind": "job_object",
                        "kill_on_close": True,
                        "terminal_state": None,
                    },
                    "artifact_cleanup_permitted": False,
                    "cleanup_failure": None,
                },
            )

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
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
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
                    patch.object(runner, "project_version", return_value="0.23.11")
                )
                patches.enter_context(
                    patch.object(runner, "claim_coverage_run", return_value="marker")
                )
                patches.enter_context(
                    patch.object(runner, "capture_coverage_run_claim", return_value=object())
                )
                patches.enter_context(
                    patch.object(runner, "clear_claimed_coverage_run", return_value=[])
                )
                patches.enter_context(
                    patch.object(runner, "produce_coverage", return_value={"evidence_sets": []})
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "sealed_coverage_evidence",
                        return_value=self.sealed_coverage_context(),
                    )
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
        self.assertIn("/d:sonar.projectVersion=0.23.11", process_commands[0])

    def test_analysis_coverage_binding_brackets_submitted_analysis(self):
        head = "a" * 40
        binding = {
            "observed": True,
            "current": True,
            "analysis_id": "analysis",
            "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"},
            "revision": head,
        }
        response = {
            "component": {
                "measures": [
                    {"metric": "coverage", "value": "80.0"},
                    {"metric": "lines_to_cover", "value": "10"},
                    {"metric": "new_coverage", "value": "80.0"},
                    {"metric": "new_uncovered_lines", "value": "2"},
                ]
            }
        }
        with (
            patch.object(runner, "current_analysis_binding", return_value=binding),
            patch.object(runner, "api_json", return_value=response),
        ):
            result = runner.analysis_coverage_binding(
                "https://sonar.example.test", "analysis", head, "read-token"
            )

        self.assertEqual(result["before"], binding)
        self.assertEqual(result["after"], binding)
        self.assertEqual(result["metrics"]["new_coverage"], "80.0")

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
            original_clear_generated_artifacts = runner.clear_generated_artifacts

            def capture_receipt(_path, receipt, _secrets):
                captured_receipts.append(json.loads(json.dumps(receipt)))

            def clear_artifacts(cleanup_context, environment):
                nonlocal cleanup_calls
                cleanup_calls += 1
                if cleanup_calls == 2:
                    return original_clear_generated_artifacts(cleanup_context, environment)
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
                    ("analysis_current_before_issues", "analysis_current_after_issues")[
                        binding_calls - 1
                    ]
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

            def fail_removal(_path):
                events.append("post_scan_cleanup")
                raise PermissionError("access denied")

            def wait_for_ce_task(_host, task_id, _token, _receipt, *, lease_checkpoint):
                lease_checkpoint(task_id, "2026-08-27T00:10:00Z")
                return "analysis-1"

            with ExitStack() as patches:
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
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
                    patch.object(runner, "project_version", return_value="0.23.11")
                )
                patches.enter_context(
                    patch.object(runner, "claim_coverage_run", return_value="marker")
                )
                patches.enter_context(
                    patch.object(runner, "capture_coverage_run_claim", return_value=object())
                )
                patches.enter_context(
                    patch.object(runner, "clear_claimed_coverage_run", return_value=[])
                )
                patches.enter_context(
                    patch.object(runner, "produce_coverage", return_value={"evidence_sets": []})
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "validate_coverage_evidence",
                        return_value={"evidence_sets": []},
                    )
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "sealed_coverage_evidence",
                        return_value=self.sealed_coverage_context(),
                    )
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
                    patch.object(runner, "wait_for_ce_task", side_effect=wait_for_ce_task)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "analysis_coverage_binding",
                        return_value={"analysis_id": "analysis-1"},
                    )
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
                patches.enter_context(patch.object(runner, "is_tracked", return_value=False))
                patches.enter_context(
                    patch.object(runner.shutil, "rmtree", side_effect=fail_removal)
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
            blocked_receipt["failure"],
            "Analysis-bound quality gate is ERROR; only OK passes.",
        )
        self.assertEqual(blocked_receipt["quality_gate"], error_quality_gate)
        self.assertEqual(blocked_receipt["post_scan_issues"], full_inventory)
        self.assertEqual(blocked_receipt["new_code_issues"], new_code_inventory)
        self.assertEqual(blocked_receipt["hotspots"], hotspots)
        self.assertEqual(blocked_receipt["analysis_current_after_issues"], binding)
        self.assertEqual(blocked_receipt["cleanup"]["status"], "BLOCKED")
        self.assertEqual(blocked_receipt["cleanup"]["removed"], [])
        self.assertEqual(
            {
                field: blocked_receipt["cleanup"]["failure"][field]
                for field in ("path", "operation", "error_type")
            },
            {
                "path": "generated/obj",
                "operation": "rmtree",
                "error_type": "PermissionError",
            },
        )
        self.assertNotIn("post", blocked_receipt.get("cleanliness", {}))
        self.assertNotIn("analysis_current_final", blocked_receipt)
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
        root = Path("coverage-cleanup-order-test")
        context = runner.GitContext(root, root, root, root, "a" * 40)

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

    def test_diagnostic_receipt_target_is_run_namespaced_and_non_authoritative(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            run_id = "00000000-0000-4000-8000-000000000121"

            arguments = runner.parse_args(["--role", "diagnostic"])
            target = runner.receipt_target(context, arguments.role, run_id)

        self.assertEqual(target.authority, "diagnostic")
        self.assertFalse(target.authoritative)
        self.assertEqual(
            target.path,
            root
            / ".agent"
            / "e"
            / "sonarqube"
            / runner.PROJECT_KEY
            / ("a" * 40)
            / "diagnostic"
            / f"{run_id}.json",
        )

    def test_diagnostic_execution_resolves_run_namespaced_target_before_credentials(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            target_calls = []
            original_receipt_target = runner.receipt_target

            def capture_target(target_context, role, run_id):
                target_calls.append((target_context, role, run_id))
                return original_receipt_target(target_context, role, run_id)

            with (
                patch.object(runner, "process_environment", return_value={}),
                patch.object(runner, "scrub_sonar_environment", return_value={}),
                patch.object(runner, "git_context", return_value=context) as git_context,
                patch.object(runner, "receipt_target", side_effect=capture_target),
                patch.object(
                    runner,
                    "project_lock",
                    side_effect=runner.RunnerError("stop before credential load"),
                ),
                patch.object(runner, "load_credentials") as load_credentials,
                self.assertRaisesRegex(runner.RunnerError, "stop before credential load"),
            ):
                runner.execute("diagnostic", None)

        git_context.assert_called_once()
        load_credentials.assert_not_called()
        self.assertEqual(target_calls[0][:2], (context, "diagnostic"))
        self.assertEqual(str(runner.uuid.UUID(target_calls[0][2])), target_calls[0][2])

    def test_receipt_dispatch_preserves_historical_v2_and_refuses_unknown_v3_shape(self):
        historical = runner.receipt_dispatch({"schema_version": 2, "outcome": "BLOCKED"})

        self.assertTrue(historical.historical)
        self.assertEqual((historical.schema_version, historical.authority), (2, None))
        for malformed in (
            {"schema_version": 3, "authority": "unknown", "outcome": "RUNNING"},
            {"schema_version": 3, "authority": "diagnostic", "outcome": "UNKNOWN"},
            {"schema_version": 99, "outcome": "BLOCKED"},
        ):
            with self.subTest(malformed=malformed):
                with self.assertRaisesRegex(runner.RunnerError, "receipt"):
                    runner.receipt_dispatch(malformed)

    def test_terminal_and_historical_authoritative_receipts_refuse_replacement(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            candidate = runner.receipt_target(context, "candidate", None)
            candidate_running = runner.receipt_base(context, candidate, "candidate-run")
            runner.write_receipt(candidate, candidate_running, ())
            candidate_terminal = {**candidate_running, "outcome": "PASS"}
            runner.write_receipt(candidate, candidate_terminal, ())
            with self.assertRaisesRegex(runner.RunnerError, "terminal receipt"):
                runner.write_receipt(candidate, candidate_running, ())

            post_merge = runner.receipt_target(context, "post-merge", None)
            post_merge.path.parent.mkdir(parents=True, exist_ok=True)
            post_merge.path.write_text(
                json.dumps({"schema_version": 2, "outcome": "BLOCKED"}),
                encoding="utf-8",
            )
            with self.assertRaisesRegex(runner.RunnerError, "historical receipt"):
                runner.write_receipt(
                    post_merge,
                    runner.receipt_base(context, post_merge, "post-merge-run"),
                    (),
                )

    def test_stale_same_run_id_lease_handle_cannot_checkpoint_successor_generation(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            run_id = "00000000-0000-4000-8000-000000000147"
            target = runner.receipt_target(context, "diagnostic", run_id)
            lease_path = runner.lock_path(context.coordination_root)

            with runner.project_lock(context, target, run_id) as closed_handle:
                stale_handle = closed_handle
                first_lease_bytes = lease_path.read_bytes()

            self.assertFalse(lease_path.exists())

            with runner.project_lock(context, target, run_id) as successor_handle:
                successor_lease_bytes = lease_path.read_bytes()

                with self.assertRaises(runner.RunnerError):
                    stale_handle.checkpoint("SCANNER_END_IN_FLIGHT")

                self.assertEqual(lease_path.read_bytes(), successor_lease_bytes)
                self.assertEqual(runner.read_scan_lease(lease_path), successor_handle.lease)
                self.assertNotEqual(first_lease_bytes, successor_lease_bytes)

    def test_project_lock_rejects_reparse_storage_ancestor_before_c1a_mutation(self):
        class ReparseDirectoryMetadata:
            st_mode = stat.S_IFDIR
            st_file_attributes = 0x0400

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            coordination_root = root / "coordination-root"
            scanner_root = root / "scanner-worktree"
            coordination_root.mkdir()
            scanner_root.mkdir()
            context = self.context(coordination_root, scanner_root)
            run_id = "00000000-0000-4000-8000-000000000148"
            target = runner.receipt_target(context, "diagnostic", run_id)
            receipt = runner.receipt_base(context, target, run_id)
            storage_ancestor = coordination_root / ".agent"
            storage_ancestor.mkdir()
            lease_path = runner.lock_path(context.coordination_root)
            guard_path = runner.scan_transaction_guard_path(lease_path)
            outside_sentinel = root / "outside-sentinel.json"
            outside_sentinel_bytes = b"must remain outside C1a storage"
            outside_sentinel.write_bytes(outside_sentinel_bytes)
            lstat_calls = []
            guard_directory_mutations = []
            original_lstat = Path.lstat
            original_mkdir = Path.mkdir

            def nonfollowing_lstat(path):
                lstat_calls.append(path)
                if path == storage_ancestor:
                    return ReparseDirectoryMetadata()
                return original_lstat(path)

            def refuse_guard_directory_mutation(path, mode=0o777, parents=False, exist_ok=False):
                if path == guard_path.parent:
                    guard_directory_mutations.append(path)
                    raise AssertionError(
                        "guard directory mutation reached before storage ancestry admission"
                    )
                return original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

            with (
                patch.object(Path, "lstat", autospec=True, side_effect=nonfollowing_lstat),
                patch.object(
                    Path, "mkdir", autospec=True, side_effect=refuse_guard_directory_mutation
                ),
                patch.object(runner, "write_scan_lease") as write_scan_lease,
                patch.object(runner, "write_receipt") as write_receipt,
            ):
                with self.assertRaisesRegex(runner.RunnerError, "symbolic link|reparse point"):
                    with runner.project_lock(context, target, run_id) as handle:
                        handle.write_receipt(target, receipt, ())

            self.assertIn(context.coordination_root, lstat_calls)
            self.assertIn(storage_ancestor, lstat_calls)
            self.assertLess(
                lstat_calls.index(context.coordination_root), lstat_calls.index(storage_ancestor)
            )
            self.assertEqual(guard_directory_mutations, [])
            write_scan_lease.assert_not_called()
            write_receipt.assert_not_called()
            self.assertFalse(guard_path.exists())
            self.assertFalse(lease_path.exists())
            self.assertFalse(target.path.exists())
            self.assertEqual(outside_sentinel.read_bytes(), outside_sentinel_bytes)

    def test_project_lock_rejects_reparse_final_lease_leaf_before_read_or_reconciliation(
        self,
    ):
        class ReparseFileMetadata:
            st_mode = stat.S_IFREG
            st_file_attributes = 0x0400

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            coordination_root = root / "coordination-root"
            scanner_root = root / "scanner-worktree"
            coordination_root.mkdir()
            scanner_root.mkdir()
            context = self.context(coordination_root, scanner_root)
            run_id = "00000000-0000-4000-8000-000000000150"
            target = runner.receipt_target(context, "diagnostic", run_id)
            lease_path = runner.lock_path(context.coordination_root)
            lease_path.parent.mkdir(parents=True)
            lease_path.write_text("{}\n", encoding="utf-8")
            lease_bytes = lease_path.read_bytes()
            lstat_calls = []
            reconciliation_calls = []
            original_lstat = Path.lstat

            def nonfollowing_lstat(path):
                lstat_calls.append(path)
                if path == lease_path:
                    return ReparseFileMetadata()
                return original_lstat(path)

            def refuse_reconciliation(lease):
                reconciliation_calls.append(lease)
                raise AssertionError("reconciliation reached after final-leaf reparse detection")

            with (
                patch.object(Path, "lstat", autospec=True, side_effect=nonfollowing_lstat),
                patch.object(
                    runner,
                    "read_scan_lease",
                    side_effect=AssertionError(
                        "lease read reached after final-leaf reparse detection"
                    ),
                ) as read_scan_lease,
                patch.object(runner, "_create_fully_written_scan_lease") as create_scan_lease,
                patch.object(runner, "_unlink_scan_lease_for_owner") as unlink_scan_lease,
            ):
                with self.assertRaisesRegex(runner.RunnerError, "symbolic link|reparse point"):
                    with runner.project_lock(
                        context, target, run_id, reconcile=refuse_reconciliation
                    ):
                        pass

            self.assertIn(lease_path, lstat_calls)
            read_scan_lease.assert_not_called()
            self.assertEqual(reconciliation_calls, [])
            create_scan_lease.assert_not_called()
            unlink_scan_lease.assert_not_called()
            self.assertEqual(lease_path.read_bytes(), lease_bytes)

    def test_scan_lease_handle_checkpoint_rejects_reparse_storage_ancestor_before_guard_mutation(
        self,
    ):
        class ReparseDirectoryMetadata:
            st_mode = stat.S_IFDIR
            st_file_attributes = 0x0400

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            coordination_root = root / "coordination-root"
            scanner_root = root / "scanner-worktree"
            coordination_root.mkdir()
            scanner_root.mkdir()
            context = self.context(coordination_root, scanner_root)
            run_id = "00000000-0000-4000-8000-000000000149"
            target = runner.receipt_target(context, "diagnostic", run_id)
            storage_ancestor = coordination_root / ".agent"
            lease_path = runner.lock_path(context.coordination_root)
            guard_path = runner.scan_transaction_guard_path(lease_path)
            lstat_calls = []
            guard_directory_mutations = []
            original_lstat = Path.lstat
            original_mkdir = Path.mkdir

            with runner.project_lock(context, target, run_id) as handle:
                self.assertTrue(storage_ancestor.is_dir())

                def nonfollowing_lstat(path):
                    lstat_calls.append(path)
                    if path == storage_ancestor:
                        return ReparseDirectoryMetadata()
                    return original_lstat(path)

                def refuse_guard_directory_mutation(
                    path, mode=0o777, parents=False, exist_ok=False
                ):
                    if path == guard_path.parent:
                        guard_directory_mutations.append(path)
                        raise AssertionError(
                            "guard directory mutation reached before storage ancestry admission"
                        )
                    return original_mkdir(path, mode=mode, parents=parents, exist_ok=exist_ok)

                with (
                    patch.object(Path, "lstat", autospec=True, side_effect=nonfollowing_lstat),
                    patch.object(
                        Path, "mkdir", autospec=True, side_effect=refuse_guard_directory_mutation
                    ),
                    patch.object(runner, "write_scan_lease") as write_scan_lease,
                ):
                    with self.assertRaisesRegex(runner.RunnerError, "symbolic link|reparse point"):
                        handle.checkpoint("SCANNER_END_IN_FLIGHT")

                self.assertIn(context.coordination_root, lstat_calls)
                self.assertIn(storage_ancestor, lstat_calls)
                self.assertLess(
                    lstat_calls.index(context.coordination_root),
                    lstat_calls.index(storage_ancestor),
                )
                self.assertEqual(guard_directory_mutations, [])
                write_scan_lease.assert_not_called()
                self.assertEqual(handle.lease.state, "ACQUIRED")

    def test_project_lock_releases_guard_and_acquired_lease_after_target_receipt_storage_reparse(
        self,
    ):
        class ReparseDirectoryMetadata:
            st_mode = stat.S_IFDIR
            st_file_attributes = 0x0400

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            run_id = "00000000-0000-4000-8000-000000000151"
            target = runner.receipt_target(context, "diagnostic", run_id)
            receipt = runner.receipt_base(context, target, run_id)
            lease_path = runner.lock_path(context.coordination_root)
            guard_path = runner.scan_transaction_guard_path(lease_path)
            receipt_storage_ancestor = target.path.parent
            original_lstat = Path.lstat

            with runner.project_lock(context, target, run_id) as handle:
                receipt_storage_ancestor.mkdir(parents=True)

                def nonfollowing_lstat(path):
                    if path == receipt_storage_ancestor:
                        return ReparseDirectoryMetadata()
                    return original_lstat(path)

                with patch.object(Path, "lstat", autospec=True, side_effect=nonfollowing_lstat):
                    with self.assertRaisesRegex(runner.RunnerError, "symbolic link|reparse point"):
                        handle.write_receipt(target, receipt, ())

            self.assertFalse(target.path.exists())
            self.assertFalse(guard_path.exists())
            self.assertFalse(lease_path.exists())

    def test_project_lock_releases_acquired_lease_after_reparse_target_receipt_leaf(self):
        class ReparseFileMetadata:
            st_mode = stat.S_IFREG
            st_file_attributes = 0x0400

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            run_id = "00000000-0000-4000-8000-000000000152"
            target = runner.receipt_target(context, "diagnostic", run_id)
            receipt = runner.receipt_base(context, target, run_id)
            lease_path = runner.lock_path(context.coordination_root)
            guard_path = runner.scan_transaction_guard_path(lease_path)
            original_lstat = Path.lstat
            original_read_text = Path.read_text

            with runner.project_lock(context, target, run_id) as handle:
                target.path.parent.mkdir(parents=True)
                target.path.write_text("{}\n", encoding="utf-8")

                def nonfollowing_lstat(path):
                    if path == target.path:
                        return ReparseFileMetadata()
                    return original_lstat(path)

                def refuse_receipt_read(path, *args, **kwargs):
                    if path == target.path:
                        raise AssertionError(
                            "receipt read reached after final-leaf reparse detection"
                        )
                    return original_read_text(path, *args, **kwargs)

                with (
                    patch.object(Path, "lstat", autospec=True, side_effect=nonfollowing_lstat),
                    patch.object(Path, "read_text", autospec=True, side_effect=refuse_receipt_read),
                    patch.object(
                        runner,
                        "write_receipt",
                        side_effect=AssertionError(
                            "receipt write reached after final-leaf reparse detection"
                        ),
                    ) as write_receipt,
                ):
                    with self.assertRaisesRegex(
                        runner.RunnerError, "receipt storage target.*symbolic link|reparse point"
                    ):
                        handle.write_receipt(target, receipt, ())

                write_receipt.assert_not_called()
                self.assertEqual(handle.lease.state, "ACQUIRED")
                self.assertFalse(guard_path.exists())

            self.assertEqual(target.path.read_text(encoding="utf-8"), "{}\n")
            self.assertFalse(guard_path.exists())
            self.assertFalse(lease_path.exists())

    def test_scan_lease_persists_full_identity_and_reopens_atomic_bytes(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            run_id = "00000000-0000-4000-8000-000000000122"
            target = runner.receipt_target(context, "diagnostic", run_id)
            lease = runner.new_scan_lease(context, target, run_id)
            path = runner.lock_path(context.coordination_root)

            runner.write_scan_lease(path, lease)
            persisted = runner.read_scan_lease(path)

        self.assertEqual(persisted, lease)
        self.assertEqual(
            (
                persisted.run_id,
                persisted.authority,
                persisted.project_key,
                persisted.captured_head,
                persisted.receipt_identity,
                persisted.scanner_worktree_identity,
                persisted.state,
            ),
            (
                run_id,
                "diagnostic",
                runner.PROJECT_KEY,
                "a" * 40,
                target.receipt_identity,
                runner.scanner_worktree_identity(context),
                "ACQUIRED",
            ),
        )

    def test_scan_lease_reconciles_only_exact_known_submission(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            run_id = "00000000-0000-4000-8000-000000000123"
            target = runner.receipt_target(context, "diagnostic", run_id)
            lease = runner.new_scan_lease(context, target, run_id)
            scanner_end = runner.transition_scan_lease(lease, "SCANNER_END_IN_FLIGHT")
            with self.assertRaisesRegex(runner.RunnerError, "scanner-end handoff"):
                runner.reconcile_scan_lease(scanner_end, context, target, "SUCCESS")

            submitted = runner.transition_scan_lease(
                scanner_end,
                "CE_SUBMITTED",
                task_id="task-123",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            reconciled = runner.reconcile_scan_lease(submitted, context, target, "SUCCESS")
            self.assertEqual(reconciled.state, "CE_TERMINAL")
            self.assertEqual(reconciled.task_id, "task-123")
            other_target = runner.receipt_target(context, "candidate", None)
            with self.assertRaisesRegex(runner.RunnerError, "identity"):
                runner.reconcile_scan_lease(submitted, context, other_target, "SUCCESS")
            with self.assertRaisesRegex(runner.RunnerError, "known task"):
                runner.reconcile_scan_lease(submitted, context, target, "PENDING")

    def test_scan_lease_submission_admits_only_strict_utc_deadlines(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            target = runner.receipt_target(
                context,
                "diagnostic",
                "00000000-0000-4000-8000-000000000144",
            )
            scanner_end = runner.transition_scan_lease(
                runner.new_scan_lease(
                    context,
                    target,
                    "00000000-0000-4000-8000-000000000144",
                ),
                "SCANNER_END_IN_FLIGHT",
            )

            for deadline in (
                "not-a-deadline",
                "2026-08-27T00:10:00",
                "2026-08-27T00:10:00+01:00",
            ):
                with self.subTest(deadline=deadline):
                    with self.assertRaisesRegex(runner.RunnerError, "deadline"):
                        runner.transition_scan_lease(
                            scanner_end,
                            "CE_SUBMITTED",
                            task_id="task-144",
                            utc_deadline=deadline,
                        )

            accepted = runner.transition_scan_lease(
                scanner_end,
                "CE_SUBMITTED",
                task_id="task-144",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            self.assertEqual(accepted.utc_deadline, "2026-08-27T00:10:00Z")

    def test_ce_lease_checkpoint_precedes_first_compute_engine_poll(self):
        events = []

        def checkpoint(task_id, deadline):
            events.append(("checkpoint", task_id, deadline))

        def api_json(_host, _endpoint, _parameters, _token):
            self.assertEqual(events[0][0], "checkpoint")
            events.append(("poll",))
            return {
                "task": {
                    "id": "task-124",
                    "status": "SUCCESS",
                    "componentKey": runner.PROJECT_KEY,
                    "analysisId": "analysis-124",
                }
            }

        with patch.object(runner, "api_json", side_effect=api_json):
            analysis_id = runner.wait_for_ce_task(
                "https://sonar.example.test",
                "task-124",
                "scan-token",
                {},
                lease_checkpoint=checkpoint,
            )

        self.assertEqual(analysis_id, "analysis-124")
        self.assertEqual([event[0] for event in events], ["checkpoint", "poll"])

    def test_project_lock_refuses_submitted_diagnostic_receipt_with_mismatched_run_id(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            previous_run_id = "00000000-0000-4000-8000-000000000130"
            mismatched_run_id = "00000000-0000-4000-8000-000000000131"
            next_run_id = "00000000-0000-4000-8000-000000000132"
            previous_target = runner.receipt_target(context, "diagnostic", previous_run_id)
            next_target = runner.receipt_target(context, "diagnostic", next_run_id)
            mismatched_receipt = runner.receipt_base(context, previous_target, previous_run_id)
            mismatched_receipt["run_id"] = mismatched_run_id
            runner.write_receipt(previous_target, mismatched_receipt, ())
            expected_receipt_bytes = previous_target.path.read_bytes()
            submitted = runner.transition_scan_lease(
                runner.transition_scan_lease(
                    runner.new_scan_lease(context, previous_target, previous_run_id),
                    "SCANNER_END_IN_FLIGHT",
                ),
                "CE_SUBMITTED",
                task_id="task-130",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            lease_path = runner.lock_path(context.coordination_root)
            runner.write_scan_lease(lease_path, submitted)
            reconciled = []

            def reconcile(lease):
                reconciled.append(lease)
                return "SUCCESS"

            with self.assertRaisesRegex(
                runner.RunnerError, "existing diagnostic receipt cannot be reconciled"
            ):
                with runner.project_lock(
                    context,
                    next_target,
                    next_run_id,
                    reconcile=reconcile,
                ):
                    pass

            retained_receipt_bytes = previous_target.path.read_bytes()
            self.assertTrue(lease_path.exists())
            retained_lease = runner.read_scan_lease(lease_path)

        self.assertEqual(reconciled, [])
        self.assertEqual(retained_receipt_bytes, expected_receipt_bytes)
        self.assertEqual(retained_lease, submitted)

    def test_project_lock_refuses_submitted_diagnostic_receipt_with_mismatched_captured_head(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            previous_run_id = "00000000-0000-4000-8000-000000000133"
            next_run_id = "00000000-0000-4000-8000-000000000134"
            previous_target = runner.receipt_target(context, "diagnostic", previous_run_id)
            next_target = runner.receipt_target(context, "diagnostic", next_run_id)
            mismatched_receipt = runner.receipt_base(context, previous_target, previous_run_id)
            mismatched_receipt["captured_head"] = "b" * 40
            runner.write_receipt(previous_target, mismatched_receipt, ())
            expected_receipt_bytes = previous_target.path.read_bytes()
            submitted = runner.transition_scan_lease(
                runner.transition_scan_lease(
                    runner.new_scan_lease(context, previous_target, previous_run_id),
                    "SCANNER_END_IN_FLIGHT",
                ),
                "CE_SUBMITTED",
                task_id="task-133",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            lease_path = runner.lock_path(context.coordination_root)
            runner.write_scan_lease(lease_path, submitted)
            reconciled = []

            def reconcile(lease):
                reconciled.append(lease)
                return "SUCCESS"

            with self.assertRaisesRegex(
                runner.RunnerError, "existing diagnostic receipt cannot be reconciled"
            ):
                with runner.project_lock(
                    context,
                    next_target,
                    next_run_id,
                    reconcile=reconcile,
                ):
                    pass

            retained_receipt_bytes = previous_target.path.read_bytes()
            self.assertTrue(lease_path.exists())
            retained_lease = runner.read_scan_lease(lease_path)

        self.assertEqual(reconciled, [])
        self.assertEqual(retained_receipt_bytes, expected_receipt_bytes)
        self.assertEqual(retained_lease, submitted)

    def test_project_lock_refuses_persisted_candidate_or_post_merge_submission(self):
        for role, run_id in (
            ("candidate", "candidate-submission"),
            ("post-merge", "post-merge-submission"),
        ):
            with self.subTest(role=role):
                with TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    context = self.context(root, root / "scanner")
                    target = runner.receipt_target(context, role, None)
                    next_run_id = "00000000-0000-4000-8000-000000000135"
                    next_target = runner.receipt_target(context, "diagnostic", next_run_id)
                    submitted = runner.transition_scan_lease(
                        runner.transition_scan_lease(
                            runner.new_scan_lease(context, target, run_id),
                            "SCANNER_END_IN_FLIGHT",
                        ),
                        "CE_SUBMITTED",
                        task_id=f"task-{role}",
                        utc_deadline="2026-08-27T00:10:00Z",
                    )
                    lease_path = runner.lock_path(context.coordination_root)
                    runner.write_scan_lease(lease_path, submitted)
                    expected_lease_bytes = lease_path.read_bytes()
                    reconciled = []

                    def reconcile(lease):
                        reconciled.append(lease)
                        return "SUCCESS"

                    with self.assertRaises(runner.RunnerError):
                        with runner.project_lock(
                            context,
                            next_target,
                            next_run_id,
                            reconcile=reconcile,
                        ):
                            pass

                    self.assertEqual(reconciled, [])
                    self.assertTrue(lease_path.exists())
                    self.assertEqual(lease_path.read_bytes(), expected_lease_bytes)

    def test_project_lock_waits_for_persisted_deadline_before_diagnostic_recovery(self):
        for case, now_value, should_recover in (
            (
                "before_deadline",
                datetime(2026, 8, 27, 0, 9, tzinfo=timezone.utc),
                False,
            ),
            (
                "at_deadline",
                datetime(2026, 8, 27, 0, 10, tzinfo=timezone.utc),
                True,
            ),
        ):
            with self.subTest(case=case):
                with TemporaryDirectory() as temporary_directory:
                    root = Path(temporary_directory)
                    context = self.context(root, root / "scanner")
                    previous_run_id = "00000000-0000-4000-8000-000000000136"
                    next_run_id = "00000000-0000-4000-8000-000000000137"
                    previous_target = runner.receipt_target(context, "diagnostic", previous_run_id)
                    next_target = runner.receipt_target(context, "diagnostic", next_run_id)
                    previous_receipt = runner.receipt_base(
                        context, previous_target, previous_run_id
                    )
                    runner.write_receipt(previous_target, previous_receipt, ())
                    expected_receipt_bytes = previous_target.path.read_bytes()
                    submitted = runner.transition_scan_lease(
                        runner.transition_scan_lease(
                            runner.new_scan_lease(context, previous_target, previous_run_id),
                            "SCANNER_END_IN_FLIGHT",
                        ),
                        "CE_SUBMITTED",
                        task_id="task-136",
                        utc_deadline="2026-08-27T00:10:00Z",
                    )
                    lease_path = runner.lock_path(context.coordination_root)
                    runner.write_scan_lease(lease_path, submitted)
                    expected_lease_bytes = lease_path.read_bytes()
                    reconciled = []
                    now_calls = []

                    def reconcile(lease):
                        reconciled.append((lease.run_id, lease.task_id))
                        return "SUCCESS"

                    def now():
                        now_calls.append(now_value)
                        return now_value

                    if should_recover:
                        with runner.project_lock(
                            context,
                            next_target,
                            next_run_id,
                            reconcile=reconcile,
                            now=now,
                        ) as handle:
                            self.assertEqual(handle.lease.state, "ACQUIRED")

                        recovered_receipt = json.loads(
                            previous_target.path.read_text(encoding="utf-8")
                        )
                        self.assertEqual(reconciled, [(previous_run_id, "task-136")])
                        self.assertEqual(recovered_receipt["outcome"], "BLOCKED")
                        self.assertFalse(lease_path.exists())
                    else:
                        writes = []
                        write_scan_lease = runner.write_scan_lease

                        def record_write(path, lease):
                            writes.append((path, lease))
                            write_scan_lease(path, lease)

                        with patch.object(runner, "write_scan_lease", side_effect=record_write):
                            with self.assertRaisesRegex(runner.RunnerError, "deadline"):
                                with runner.project_lock(
                                    context,
                                    next_target,
                                    next_run_id,
                                    reconcile=reconcile,
                                    now=now,
                                ):
                                    pass

                        self.assertEqual(reconciled, [])
                        self.assertEqual(writes, [])
                        self.assertTrue(lease_path.exists())
                        self.assertEqual(lease_path.read_bytes(), expected_lease_bytes)
                        self.assertEqual(previous_target.path.read_bytes(), expected_receipt_bytes)

                    self.assertTrue(now_calls)

    def test_scan_transaction_guard_refuses_regular_file_replacement_without_unlinking_it(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            lease_path = runner.lock_path(root)
            guard = runner._acquire_scan_transaction_guard(lease_path)
            replacement_bytes = b"replacement scan transaction guard"

            guard.path.unlink()
            guard.path.write_bytes(replacement_bytes)

            with self.assertRaisesRegex(runner.RunnerError, "guard"):
                guard.release()

            self.assertEqual(guard.path.read_bytes(), replacement_bytes)

    def test_project_lock_releases_guard_and_acquired_lease_after_prewrite_terminal_receipt_refusal(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            run_id = "00000000-0000-4000-8000-000000000143"
            target = runner.receipt_target(context, "diagnostic", run_id)
            terminal_receipt = {
                **runner.receipt_base(context, target, run_id),
                "outcome": "BLOCKED",
            }
            runner.write_receipt(target, terminal_receipt, ())
            lease_path = runner.lock_path(context.coordination_root)
            guard_path = runner.scan_transaction_guard_path(lease_path)

            with self.assertRaisesRegex(runner.RunnerError, "terminal receipt"):
                with runner.project_lock(context, target, run_id) as handle:
                    handle.write_receipt(
                        target,
                        runner.receipt_base(context, target, run_id),
                        (),
                    )

            self.assertFalse(guard_path.exists())
            self.assertFalse(lease_path.exists())

    def test_project_lock_refuses_existing_transaction_guard_before_primary_mutation(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            previous_run_id = "00000000-0000-4000-8000-000000000138"
            next_run_id = "00000000-0000-4000-8000-000000000139"
            previous_target = runner.receipt_target(context, "diagnostic", previous_run_id)
            next_target = runner.receipt_target(context, "diagnostic", next_run_id)
            previous_receipt = runner.receipt_base(context, previous_target, previous_run_id)
            runner.write_receipt(previous_target, previous_receipt, ())
            expected_receipt_bytes = previous_target.path.read_bytes()
            submitted = runner.transition_scan_lease(
                runner.transition_scan_lease(
                    runner.new_scan_lease(context, previous_target, previous_run_id),
                    "SCANNER_END_IN_FLIGHT",
                ),
                "CE_SUBMITTED",
                task_id="task-138",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            lease_path = runner.lock_path(context.coordination_root)
            runner.write_scan_lease(lease_path, submitted)
            expected_lease_bytes = lease_path.read_bytes()
            guard_path = runner.scan_transaction_guard_path(lease_path)
            self.assertEqual(guard_path.parent, lease_path.parent)
            self.assertNotEqual(guard_path, lease_path)
            guard_bytes = b"crash-left scan transaction guard"
            guard_path.write_bytes(guard_bytes)
            reconciled = []

            def reconcile(lease):
                reconciled.append(lease)
                return "SUCCESS"

            with patch.object(
                runner,
                "read_scan_lease",
                side_effect=AssertionError("primary lease must not be inspected while guarded"),
            ) as read_scan_lease:
                with patch.object(runner, "write_scan_lease") as write_scan_lease:
                    with self.assertRaises(runner.RunnerError):
                        with runner.project_lock(
                            context,
                            next_target,
                            next_run_id,
                            reconcile=reconcile,
                        ):
                            pass

            read_scan_lease.assert_not_called()
            write_scan_lease.assert_not_called()
            self.assertEqual(reconciled, [])
            self.assertEqual(previous_target.path.read_bytes(), expected_receipt_bytes)
            self.assertEqual(lease_path.read_bytes(), expected_lease_bytes)
            self.assertEqual(guard_path.read_bytes(), guard_bytes)

    def test_project_lock_releases_transaction_guard_after_submitted_failure(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            run_id = "00000000-0000-4000-8000-000000000142"
            target = runner.receipt_target(context, "diagnostic", run_id)
            lease_path = runner.lock_path(context.coordination_root)
            submitted = None
            guard_path = runner.scan_transaction_guard_path(lease_path)

            with self.assertRaisesRegex(runner.RunnerError, "controlled submitted failure"):
                with runner.project_lock(context, target, run_id) as handle:
                    handle.checkpoint("SCANNER_END_IN_FLIGHT")
                    submitted = handle.checkpoint(
                        "CE_SUBMITTED",
                        task_id="task-142",
                        utc_deadline="2026-08-27T00:10:00Z",
                    )
                    handle.write_receipt(
                        target,
                        {
                            **runner.receipt_base(context, target, run_id),
                            "outcome": "BLOCKED",
                            "failure": "INTERRUPTED_AFTER_CE_SUBMISSION",
                        },
                        (),
                    )
                    raise runner.RunnerError("controlled submitted failure")

            retained_lease = runner.read_scan_lease(lease_path)
            retained_receipt = json.loads(target.path.read_text(encoding="utf-8"))

        self.assertEqual(retained_lease, submitted)
        self.assertEqual(retained_lease.state, "CE_SUBMITTED")
        self.assertEqual(retained_receipt["outcome"], "BLOCKED")
        self.assertFalse(guard_path.exists())

    def test_scan_lease_handle_checkpoint_requires_persisted_owner_and_preserves_memory(
        self,
    ):
        with self.subTest(case="persisted owner mismatch"):
            with TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                context = self.context(root, root / "scanner")
                run_id = "00000000-0000-4000-8000-000000000140"
                target = runner.receipt_target(context, "diagnostic", run_id)
                lease = runner.new_scan_lease(context, target, run_id)
                persisted = runner.transition_scan_lease(lease, "SCANNER_END_IN_FLIGHT")
                lease_path = runner.lock_path(context.coordination_root)
                runner.write_scan_lease(lease_path, persisted)
                handle = runner.ScanLeaseHandle(lease_path, lease, context.coordination_root)

                with patch.object(runner, "write_scan_lease") as write_scan_lease:
                    with self.assertRaises(runner.RunnerError):
                        handle.checkpoint("SCANNER_END_IN_FLIGHT")

                write_scan_lease.assert_not_called()
                self.assertEqual(handle.lease, lease)
                self.assertEqual(runner.read_scan_lease(lease_path), persisted)

        with self.subTest(case="write failure"):
            with TemporaryDirectory() as temporary_directory:
                root = Path(temporary_directory)
                context = self.context(root, root / "scanner")
                run_id = "00000000-0000-4000-8000-000000000141"
                target = runner.receipt_target(context, "diagnostic", run_id)
                lease = runner.new_scan_lease(context, target, run_id)
                lease_path = runner.lock_path(context.coordination_root)
                runner.write_scan_lease(lease_path, lease)
                handle = runner.ScanLeaseHandle(lease_path, lease, context.coordination_root)
                events = []
                read_scan_lease = runner.read_scan_lease

                def record_read(path):
                    events.append(("read", path))
                    return read_scan_lease(path)

                def fail_write(path, next_lease):
                    events.append(("write", path, next_lease))
                    raise runner.RunnerError("checkpoint write failed")

                with (
                    patch.object(runner, "read_scan_lease", side_effect=record_read),
                    patch.object(runner, "write_scan_lease", side_effect=fail_write),
                ):
                    with self.assertRaisesRegex(runner.RunnerError, "checkpoint write failed"):
                        handle.checkpoint("SCANNER_END_IN_FLIGHT")

                self.assertEqual([event[0] for event in events], ["read", "write"])
                self.assertEqual(events[0][1], lease_path)
                self.assertEqual(events[1][1], lease_path)
                self.assertEqual(events[1][2].state, "SCANNER_END_IN_FLIGHT")
                self.assertEqual(handle.lease, lease)
                self.assertEqual(runner.read_scan_lease(lease_path), lease)

    def test_scan_lease_handle_refuses_receipt_for_different_diagnostic_run_before_creation(
        self,
    ):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            owned_run_id = "00000000-0000-4000-8000-000000000145"
            other_run_id = "00000000-0000-4000-8000-000000000146"
            owned_target = runner.receipt_target(context, "diagnostic", owned_run_id)
            other_target = runner.receipt_target(context, "diagnostic", other_run_id)
            lease = runner.new_scan_lease(context, owned_target, owned_run_id)
            lease_path = runner.lock_path(context.coordination_root)
            runner.write_scan_lease(lease_path, lease)
            handle = runner.ScanLeaseHandle(lease_path, lease, context.coordination_root)
            other_receipt = runner.receipt_base(context, other_target, other_run_id)

            self.assertFalse(other_target.path.exists())
            with self.assertRaisesRegex(runner.RunnerError, "lease"):
                handle.write_receipt(other_target, other_receipt, ())

            self.assertFalse(other_target.path.exists())
            self.assertEqual(runner.read_scan_lease(lease_path), lease)

    def test_project_lock_recovery_rejects_target_only_reparse_before_receipt_read_or_mutation(
        self,
    ):
        class ReparseDirectoryMetadata:
            st_mode = stat.S_IFDIR
            st_file_attributes = 0x0400

        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            previous_run_id = "00000000-0000-4000-8000-000000000153"
            next_run_id = "00000000-0000-4000-8000-000000000154"
            previous_target = runner.receipt_target(context, "diagnostic", previous_run_id)
            next_target = runner.receipt_target(context, "diagnostic", next_run_id)
            previous_receipt = runner.receipt_base(context, previous_target, previous_run_id)
            runner.write_receipt(previous_target, previous_receipt, ())
            expected_receipt_bytes = previous_target.path.read_bytes()
            submitted = runner.transition_scan_lease(
                runner.transition_scan_lease(
                    runner.new_scan_lease(context, previous_target, previous_run_id),
                    "SCANNER_END_IN_FLIGHT",
                ),
                "CE_SUBMITTED",
                task_id="task-153",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            lease_path = runner.lock_path(context.coordination_root)
            guard_path = runner.scan_transaction_guard_path(lease_path)
            runner.write_scan_lease(lease_path, submitted)
            expected_lease_bytes = lease_path.read_bytes()
            original_lstat = Path.lstat
            original_read_text = Path.read_text
            original_unlink = Path.unlink
            unlinked_paths = []

            def nonfollowing_lstat(path):
                if path == previous_target.path.parent:
                    return ReparseDirectoryMetadata()
                return original_lstat(path)

            def refuse_target_receipt_read(path, *args, **kwargs):
                if path == previous_target.path:
                    raise AssertionError("receipt read reached after target-only reparse detection")
                return original_read_text(path, *args, **kwargs)

            def record_unlink(path, *args, **kwargs):
                unlinked_paths.append(path)
                return original_unlink(path, *args, **kwargs)

            with (
                patch.object(Path, "lstat", autospec=True, side_effect=nonfollowing_lstat),
                patch.object(
                    Path,
                    "read_text",
                    autospec=True,
                    side_effect=refuse_target_receipt_read,
                ),
                patch.object(
                    runner,
                    "write_scan_lease",
                    side_effect=AssertionError(
                        "CE transition reached after target-only reparse detection"
                    ),
                ) as write_scan_lease,
                patch.object(Path, "unlink", autospec=True, side_effect=record_unlink),
            ):
                with self.assertRaisesRegex(runner.RunnerError, "symbolic link|reparse point"):
                    with runner.project_lock(
                        context,
                        next_target,
                        next_run_id,
                        reconcile=lambda _lease: (_ for _ in ()).throw(
                            AssertionError("resolver reached after target-only reparse detection")
                        ),
                    ):
                        pass

            write_scan_lease.assert_not_called()
            self.assertEqual(previous_target.path.read_bytes(), expected_receipt_bytes)
            self.assertEqual(lease_path.read_bytes(), expected_lease_bytes)
            self.assertNotIn(lease_path, unlinked_paths)
            self.assertIn(guard_path, unlinked_paths)
            self.assertFalse(guard_path.exists())

    def test_project_lock_reconciles_exact_submitted_diagnostic_lease(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            previous_run_id = "00000000-0000-4000-8000-000000000124"
            next_run_id = "00000000-0000-4000-8000-000000000125"
            previous_target = runner.receipt_target(context, "diagnostic", previous_run_id)
            next_target = runner.receipt_target(context, "diagnostic", next_run_id)
            previous_receipt = runner.receipt_base(context, previous_target, previous_run_id)
            runner.write_receipt(previous_target, previous_receipt, ())
            submitted = runner.transition_scan_lease(
                runner.transition_scan_lease(
                    runner.new_scan_lease(context, previous_target, previous_run_id),
                    "SCANNER_END_IN_FLIGHT",
                ),
                "CE_SUBMITTED",
                task_id="task-125",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            lease_path = runner.lock_path(context.coordination_root)
            runner.write_scan_lease(lease_path, submitted)
            reconciled = []

            def reconcile(lease):
                reconciled.append((lease.run_id, lease.task_id))
                return "SUCCESS"

            with runner.project_lock(
                context,
                next_target,
                next_run_id,
                reconcile=reconcile,
            ) as handle:
                self.assertEqual(handle.lease.state, "ACQUIRED")

            recovered_receipt = json.loads(previous_target.path.read_text(encoding="utf-8"))

        self.assertEqual(reconciled, [(previous_run_id, "task-125")])
        self.assertEqual(recovered_receipt["outcome"], "BLOCKED")
        self.assertEqual(recovered_receipt["failure"], "SCAN_LEASE_RECONCILED_SUCCESS")
        self.assertFalse(lease_path.exists())

    def test_project_lock_reconciles_submitted_lease_with_existing_terminal_receipt(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            previous_run_id = "00000000-0000-4000-8000-000000000128"
            next_run_id = "00000000-0000-4000-8000-000000000129"
            previous_target = runner.receipt_target(context, "diagnostic", previous_run_id)
            next_target = runner.receipt_target(context, "diagnostic", next_run_id)
            previous_receipt = runner.receipt_base(context, previous_target, previous_run_id)
            runner.write_receipt(previous_target, previous_receipt, ())
            terminal_receipt = {
                **previous_receipt,
                "outcome": "BLOCKED",
                "failure": "INTERRUPTED_AFTER_CE_SUBMISSION",
            }
            runner.write_receipt(previous_target, terminal_receipt, ())
            expected_receipt_bytes = previous_target.path.read_bytes()
            submitted = runner.transition_scan_lease(
                runner.transition_scan_lease(
                    runner.new_scan_lease(context, previous_target, previous_run_id),
                    "SCANNER_END_IN_FLIGHT",
                ),
                "CE_SUBMITTED",
                task_id="task-128",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            lease_path = runner.lock_path(context.coordination_root)
            runner.write_scan_lease(lease_path, submitted)
            reconciled = []
            persisted_states = []
            deletion_states = []
            write_scan_lease = runner.write_scan_lease
            unlink = Path.unlink

            def reconcile(lease):
                reconciled.append((lease.run_id, lease.task_id))
                return "FAILED"

            def record_scan_lease(path, lease):
                persisted_states.append(lease.state)
                write_scan_lease(path, lease)

            def record_unlink(path, *args, **kwargs):
                if path == lease_path:
                    deletion_states.append(runner.read_scan_lease(path).state)
                return unlink(path, *args, **kwargs)

            with (
                patch.object(runner, "write_scan_lease", side_effect=record_scan_lease),
                patch.object(Path, "unlink", autospec=True, side_effect=record_unlink),
                runner.project_lock(
                    context,
                    next_target,
                    next_run_id,
                    reconcile=reconcile,
                ) as handle,
            ):
                self.assertEqual(handle.lease.state, "ACQUIRED")
                self.assertEqual(handle.lease.run_id, next_run_id)
                self.assertEqual(runner.read_scan_lease(lease_path), handle.lease)

            retained_receipt_bytes = previous_target.path.read_bytes()

        self.assertEqual(reconciled, [(previous_run_id, "task-128")])
        self.assertEqual(
            persisted_states,
            ["CE_TERMINAL", "RECEIPT_TERMINAL", "CLOSED", "ACQUIRED"],
        )
        self.assertEqual(deletion_states, ["CLOSED", "ACQUIRED"])
        self.assertEqual(retained_receipt_bytes, expected_receipt_bytes)
        self.assertFalse(lease_path.exists())

    def test_execute_wires_a_credentialed_persisted_ce_resolver(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            previous_run_id = "00000000-0000-4000-8000-000000000127"
            previous_target = runner.receipt_target(context, "diagnostic", previous_run_id)
            submitted = runner.transition_scan_lease(
                runner.transition_scan_lease(
                    runner.new_scan_lease(context, previous_target, previous_run_id),
                    "SCANNER_END_IN_FLIGHT",
                ),
                "CE_SUBMITTED",
                task_id="task-127",
                utc_deadline="2026-08-27T00:10:00Z",
            )
            events = []
            resolved_statuses = []
            receipt_outcomes = []

            def load_credentials(_context, _environment, _secrets):
                events.append("credentials")
                return self.credentials()

            def api_json(host, endpoint, parameters, token):
                events.append("ce-task")
                self.assertEqual(
                    (host, endpoint, parameters, token),
                    (
                        "https://sonar.example.test",
                        "/api/ce/task",
                        {"id": "task-127"},
                        "scan-token",
                    ),
                )
                return {
                    "task": {
                        "id": "task-127",
                        "componentKey": runner.PROJECT_KEY,
                        "status": "SUCCESS",
                    }
                }

            class CapturingLock:
                def __init__(self, target, run_id, reconcile):
                    self.target = target
                    self.run_id = run_id
                    self.reconcile = reconcile

                def __enter__(self):
                    events.append("resolver")
                    resolved_statuses.append(self.reconcile(submitted))
                    lease_path = root / ".scan.lock"
                    lease = runner.new_scan_lease(context, self.target, self.run_id)
                    runner.write_scan_lease(lease_path, lease)
                    return runner.ScanLeaseHandle(lease_path, lease, context.coordination_root)

                def __exit__(self, _type, _value, _traceback):
                    return False

            def project_lock(observed_context, target, run_id, *, reconcile):
                self.assertIs(observed_context, context)
                self.assertTrue(callable(reconcile))
                events.append("lock")
                return CapturingLock(target, run_id, reconcile)

            def write_receipt(_target, receipt, _secrets):
                receipt_outcomes.append(receipt["outcome"])
                events.append(f"receipt:{receipt['outcome']}")

            def stop_after_initial_receipt(_context, _environment):
                events.append("after-receipt")
                raise runner.RunnerError("stop after receipt")

            with ExitStack() as patches:
                patches.enter_context(patch.object(runner, "process_environment", return_value={}))
                patches.enter_context(
                    patch.object(runner, "scrub_sonar_environment", return_value={})
                )
                patches.enter_context(patch.object(runner, "git_context", return_value=context))
                patches.enter_context(
                    patch.object(runner, "sonar_secret_values", return_value=set())
                )
                patches.enter_context(
                    patch.object(runner, "project_lock", side_effect=project_lock)
                )
                credential_loader = patches.enter_context(
                    patch.object(runner, "load_credentials", side_effect=load_credentials)
                )
                patches.enter_context(patch.object(runner, "api_json", side_effect=api_json))
                patches.enter_context(
                    patch.object(runner, "write_receipt", side_effect=write_receipt)
                )
                patches.enter_context(
                    patch.object(
                        runner,
                        "clear_generated_artifacts",
                        side_effect=stop_after_initial_receipt,
                    )
                )
                with self.assertRaisesRegex(runner.RunnerError, "stop after receipt"):
                    runner.execute("candidate", "scanner")

        self.assertEqual(resolved_statuses, ["SUCCESS"])
        self.assertEqual(credential_loader.call_count, 1)
        self.assertEqual(receipt_outcomes, ["RUNNING", "BLOCKED"])
        self.assertEqual(
            events,
            [
                "lock",
                "resolver",
                "credentials",
                "ce-task",
                "receipt:RUNNING",
                "after-receipt",
                "receipt:BLOCKED",
            ],
        )

    def test_receipt_writer_refuses_existing_identity_mismatch(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            target = runner.receipt_target(context, "candidate", None)
            receipt = runner.receipt_base(context, target, "candidate-run")
            runner.write_receipt(target, receipt, ())
            existing = json.loads(target.path.read_text(encoding="utf-8"))
            existing["receipt_identity"] = ".agent/e/sonarqube/other/identity.json"
            target.path.write_text(json.dumps(existing), encoding="utf-8")

            with self.assertRaisesRegex(runner.RunnerError, "identity"):
                runner.write_receipt(target, receipt, ())

    def test_persisted_diagnostic_lease_requires_safe_run_identifier(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            target = runner.receipt_target(
                context,
                "diagnostic",
                "00000000-0000-4000-8000-000000000126",
            )
            lease = runner.new_scan_lease(
                context,
                target,
                "00000000-0000-4000-8000-000000000126",
            )
            payload = runner._scan_lease_payload(lease)
            payload["run_id"] = "unsafe/diagnostic-run"
            path = runner.lock_path(context.coordination_root)
            path.parent.mkdir(parents=True)
            path.write_text(json.dumps(payload), encoding="utf-8")

            with self.assertRaisesRegex(runner.RunnerError, "diagnostic"):
                runner.read_scan_lease(path)

    def test_receipt_dispatch_refuses_diagnostic_pass(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            target = runner.receipt_target(context, "diagnostic", "diagnostic-run")
            receipt = {
                **runner.receipt_base(context, target, "diagnostic-run"),
                "outcome": "PASS",
            }

            with self.assertRaisesRegex(runner.RunnerError, "authority or outcome"):
                runner.receipt_dispatch(receipt)

    def test_legacy_pid_only_lock_is_not_reclaimed(self):
        with TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            target = runner.receipt_target(context, "candidate", None)
            stale_path = runner.lock_path(root)
            stale_path.parent.mkdir(parents=True)
            stale_path.write_text(json.dumps({"pid": 42}), encoding="utf-8")

            with self.assertRaisesRegex(runner.RunnerError, "lease"):
                with runner.project_lock(context, target, "new-run"):
                    pass

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
        run_id = "00000000-0000-4000-8000-000000000011"
        coverage_context = runner.GitContext(
            Path("root"), Path("common-dir"), Path("git-dir"), Path("coordination-root"), head
        )
        coverage_plan = runner.derive_coverage_plan(coverage_context, run_id)
        marker_sha256 = runner.hashlib.sha256(
            runner.canonical_coverage_marker_bytes(coverage_plan, coverage_context)
        ).hexdigest()

        def report_binding(report):
            return {
                "path": report.normalized_path,
                "project": report.project,
                "sha256": "a" * 64,
                "bytes": 1,
                "xml_root": "CoverageSession" if report.language == "dotnet" else "coverage",
                "coverage_denominator": 1,
                "mapped_source_count": 1,
                "source_path_set_sha256": "b" * 64,
                "captured_head": head,
            }

        coverage = {
            "evidence_sets": [
                {
                    "language": "dotnet",
                    "format": "opencover",
                    "run_id": run_id,
                    "marker_sha256": marker_sha256,
                    "reports": [report_binding(report) for report in coverage_plan.dotnet_reports],
                },
                {
                    "language": "python",
                    "format": "cobertura",
                    "run_id": run_id,
                    "marker_sha256": marker_sha256,
                    "reports": [report_binding(coverage_plan.python_report)],
                },
            ]
        }
        analysis_binding = {
            "analysis_id": "analysis",
            "before": {
                "observed": True,
                "current": True,
                "analysis_id": "analysis",
                "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"},
                "revision": head,
            },
            "query": dict(runner.COVERAGE_ANALYSIS_QUERY),
            "metrics": {
                "coverage": "80.0",
                "lines_to_cover": "10",
                "new_coverage": "80.0",
                "new_uncovered_lines": "2",
            },
            "after": {
                "observed": True,
                "current": True,
                "analysis_id": "analysis",
                "query": {"project": runner.PROJECT_KEY, "p": "1", "ps": "1"},
                "revision": head,
            },
        }

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
            "project_version": "0.23.11",
            "analysis_xml_project_key": runner.PROJECT_KEY,
            "run_id": run_id,
            "authority": "candidate",
            "receipt_identity": (f".agent/e/sonarqube/{runner.PROJECT_KEY}/{head}/candidate.json"),
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
                "sonar_project_version": "0.23.11",
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
            "coverage": coverage,
            "analysis_coverage": analysis_binding,
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
            root = Path(temporary_directory)
            context = self.context(root, root / "scanner")
            target = runner.receipt_target(context, "diagnostic", "credential-run")
            receipt = {
                **runner.receipt_base(context, target, "credential-run"),
                "outcome": "BLOCKED",
                "failure": "scan-token",
            }
            with self.assertRaisesRegex(runner.RunnerError, "credential"):
                runner.write_receipt(target, receipt, ("scan-token", "read-token"))
