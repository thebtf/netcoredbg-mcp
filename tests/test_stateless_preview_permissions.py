"""TESTFIRST contracts for the read-only current-job permission diagnostic.

These fixtures do not prove that an in-progress job exposes its setup logs.
No network, token introspection, workflow dispatch, or promotion is exercised.
"""

from __future__ import annotations

from copy import deepcopy
from unittest import TestCase
from unittest.mock import patch

from scripts.stateless_preview_permissions import (
    _ProbeError,
    _read_context,
    _validate_redirect_url,
    parse_setup_permissions,
    select_current_job,
)

REPOSITORY = "thebtf/netcoredbg-mcp"
RUN_ID = 38832333658
RUN_ATTEMPT = 1
HEAD_SHA = "92b98f0f11b89071fef674503e175355fd42d01c"
JOB_ID = 116753679372
JOB_NAME = "Build canonical-main preview candidate"
MAX_LOG_BYTES = 1024 * 1024

# Archived gh display: latest-main-92b98f0/run.log:22-27 under
# .agent/runs/a1-artifact-20261016/. Only the two tab-separated display
# columns were removed; timestamps and runner messages are unchanged.
# Producer: actions/runner@796cc18ffaaafd5cea525d6231f5a8c93c11c603,
# src/Runner.Worker/JobExtension.cs, InitializeJob, tokenPermissions block.
SETUP_PERMISSIONS = (
    b"2026-10-16T00:56:28.3062401Z ##[group]GITHUB_TOKEN Permissions\n"
    b"2026-10-16T00:56:28.3064489Z Actions: read\n"
    b"2026-10-16T00:56:28.3065175Z Contents: read\n"
    b"2026-10-16T00:56:28.3065762Z Metadata: read\n"
    b"2026-10-16T00:56:28.3066391Z PullRequests: read\n"
    b"2026-10-16T00:56:28.3066988Z ##[endgroup]\n"
)
EXPECTED_PERMISSIONS = {
    "actions": "read",
    "contents": "read",
    "metadata": "read",
    "pullrequests": "read",
}


# Same archived run-jobs.json:5-35 establishes API field names and number=1.
# Only lifecycle state is changed to model the required in-progress input;
# the archived completed job is never accepted as current-job evidence.
def current_jobs():
    return {
        "total_count": 1,
        "jobs": [
            {
                "id": JOB_ID,
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
                "head_sha": HEAD_SHA,
                "url": f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/{JOB_ID}",
                "run_url": f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{RUN_ID}",
                "name": JOB_NAME,
                "status": "in_progress",
                "conclusion": None,
                "steps": [
                    {
                        "name": "Set up job",
                        "status": "completed",
                        "conclusion": "success",
                        "number": 1,
                    },
                    {
                        "name": "Read current-job permissions",
                        "status": "in_progress",
                        "conclusion": None,
                        "number": 2,
                    },
                ],
            }
        ],
    }


def select(jobs, **overrides):
    identity = {
        "repository": REPOSITORY,
        "run_id": RUN_ID,
        "run_attempt": RUN_ATTEMPT,
        "head_sha": HEAD_SHA,
        "job_name": JOB_NAME,
    }
    identity.update(overrides)
    return select_current_job(jobs, **identity)


class TestSelectCurrentJob(TestCase):
    def test_exact_running_job_returns_job_id_and_zero_based_setup_position(self):
        selected = select(current_jobs())
        self.assertEqual(
            (selected["job_id"], selected["setup_step_position"]),
            (JOB_ID, 0),
        )

    def test_selection_does_not_mutate_supplied_api_metadata(self):
        jobs = current_jobs()
        original = deepcopy(jobs)
        select(jobs)
        self.assertEqual(jobs, original)

    def test_other_named_job_is_not_selected_instead_of_exact_job(self):
        jobs = current_jobs()
        other = deepcopy(jobs["jobs"][0])
        other.update(id=JOB_ID + 1, name="Unrelated job", status="completed")
        other["url"] = f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/{JOB_ID + 1}"
        jobs["jobs"].insert(0, other)
        jobs["total_count"] = 2
        self.assertEqual(select(jobs)["job_id"], JOB_ID)

    def test_wrong_run_attempt_sha_or_job_name_is_rejected(self):
        for field, value in (
            ("run_id", RUN_ID + 1),
            ("run_attempt", RUN_ATTEMPT + 1),
            ("head_sha", "a" * 40),
            ("name", JOB_NAME + " (other)"),
        ):
            with self.subTest(field=field):
                jobs = current_jobs()
                jobs["jobs"][0][field] = value
                with self.assertRaises(ValueError):
                    select(jobs)

    def test_requested_identity_is_not_inferred_from_response(self):
        for field, value in (
            ("repository", "another-owner/netcoredbg-mcp"),
            ("run_id", RUN_ID + 1),
            ("run_attempt", RUN_ATTEMPT + 1),
            ("head_sha", "b" * 40),
            ("job_name", "Other job"),
        ):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    select(current_jobs(), **{field: value})

    def test_repository_and_resource_urls_cannot_disagree_with_identity(self):
        for field, value in (
            ("url", f"https://api.github.com/repos/other/repo/actions/jobs/{JOB_ID}"),
            ("run_url", f"https://api.github.com/repos/other/repo/actions/runs/{RUN_ID}"),
            ("url", f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/{JOB_ID + 1}"),
            ("run_url", f"https://api.github.com/repos/{REPOSITORY}/actions/runs/{RUN_ID + 1}"),
        ):
            with self.subTest(field=field, value=value):
                jobs = current_jobs()
                jobs["jobs"][0][field] = value
                with self.assertRaises(ValueError):
                    select(jobs)

    def test_completed_successful_job_is_not_current_evidence(self):
        jobs = current_jobs()
        jobs["jobs"][0].update(status="completed", conclusion="success")
        with self.assertRaises(ValueError):
            select(jobs)

    def test_queued_waiting_or_internally_contradictory_job_is_rejected(self):
        for changes in (
            {"status": "queued"},
            {"status": "waiting"},
            {"status": "in_progress", "conclusion": "success"},
        ):
            with self.subTest(changes=changes):
                jobs = current_jobs()
                jobs["jobs"][0].update(changes)
                with self.assertRaises(ValueError):
                    select(jobs)

    def test_missing_job_identity_is_rejected(self):
        for field in (
            "id",
            "run_id",
            "run_attempt",
            "head_sha",
            "url",
            "run_url",
            "name",
            "status",
        ):
            with self.subTest(field=field):
                jobs = current_jobs()
                del jobs["jobs"][0][field]
                with self.assertRaises(ValueError):
                    select(jobs)

    def test_invalid_numeric_job_identity_is_not_coerced(self):
        for field in ("id", "run_id", "run_attempt"):
            for value in (None, True, 0, -1, "1"):
                with self.subTest(field=field, value=value):
                    jobs = current_jobs()
                    jobs["jobs"][0][field] = value
                    with self.assertRaises(ValueError):
                        select(jobs)

    def test_empty_duplicate_or_incomplete_job_listing_is_rejected(self):
        duplicate = current_jobs()
        duplicate["jobs"].append(deepcopy(duplicate["jobs"][0]))
        duplicate["total_count"] = 2
        incomplete = current_jobs()
        incomplete["total_count"] = 2
        for jobs in ({"total_count": 0, "jobs": []}, duplicate, incomplete):
            with self.subTest(jobs=jobs):
                with self.assertRaises(ValueError):
                    select(jobs)

    def test_two_distinct_jobs_with_the_exact_name_are_ambiguous(self):
        jobs = current_jobs()
        other = deepcopy(jobs["jobs"][0])
        other["id"] = JOB_ID + 1
        other["url"] = f"https://api.github.com/repos/{REPOSITORY}/actions/jobs/{JOB_ID + 1}"
        jobs["jobs"].append(other)
        jobs["total_count"] = 2
        with self.assertRaises(ValueError):
            select(jobs)

    def test_missing_duplicate_or_nonfirst_setup_step_is_rejected(self):
        original_steps = current_jobs()["jobs"][0]["steps"]
        for steps in (
            [],
            original_steps[1:],
            [original_steps[0], deepcopy(original_steps[0]), original_steps[1]],
            [original_steps[1], original_steps[0]],
        ):
            with self.subTest(steps=steps):
                jobs = current_jobs()
                jobs["jobs"][0]["steps"] = deepcopy(steps)
                with self.assertRaises(ValueError):
                    select(jobs)

    def test_setup_must_be_completed_successful_and_unambiguous(self):
        for changes in (
            {"name": "Set up job (other)"},
            {"status": "in_progress", "conclusion": None},
            {"status": "queued", "conclusion": None},
            {"conclusion": "failure"},
            {"conclusion": "cancelled"},
            {"conclusion": "skipped"},
            {"conclusion": None},
        ):
            with self.subTest(changes=changes):
                jobs = current_jobs()
                jobs["jobs"][0]["steps"][0].update(changes)
                with self.assertRaises(ValueError):
                    select(jobs)

    def test_missing_setup_status_or_conclusion_is_rejected(self):
        for field in ("name", "status", "conclusion"):
            with self.subTest(field=field):
                jobs = current_jobs()
                del jobs["jobs"][0]["steps"][0][field]
                with self.assertRaises(ValueError):
                    select(jobs)


class TestParseSetupPermissions(TestCase):
    def test_archived_timestamped_group_returns_the_full_lowercase_map(self):
        self.assertEqual(parse_setup_permissions(SETUP_PERMISSIONS), EXPECTED_PERMISSIONS)

    def test_contents_read_is_preserved_not_upgraded_to_requested_write(self):
        self.assertEqual(parse_setup_permissions(SETUP_PERMISSIONS)["contents"], "read")

    def test_explicit_write_and_none_are_preserved(self):
        log = SETUP_PERMISSIONS.replace(b"Contents: read", b"Contents: write").replace(
            b"PullRequests: read", b"PullRequests: none"
        )
        self.assertEqual(
            parse_setup_permissions(log),
            {**EXPECTED_PERMISSIONS, "contents": "write", "pullrequests": "none"},
        )

    def test_missing_grant_is_not_inferred_or_filled_in(self):
        log = SETUP_PERMISSIONS.replace(b"2026-10-16T00:56:28.3065175Z Contents: read\n", b"")
        self.assertEqual(
            parse_setup_permissions(log),
            {key: value for key, value in EXPECTED_PERMISSIONS.items() if key != "contents"},
        )

    def test_crlf_and_initial_utf8_bom_do_not_hide_runner_records(self):
        log = b"\xef\xbb\xbf" + SETUP_PERMISSIONS.replace(b"\n", b"\r\n")
        self.assertEqual(parse_setup_permissions(log), EXPECTED_PERMISSIONS)

    def test_other_setup_groups_and_outside_grants_do_not_change_permissions(self):
        prefix = (
            b"2026-10-16T00:56:28.3052764Z ##[group]Operating System\n"
            b"2026-10-16T00:56:28.3053494Z Microsoft Windows Server 2025\n"
            b"2026-10-16T00:56:28.3055152Z ##[endgroup]\n"
        )
        suffix = (
            b"2026-10-16T00:56:28.3069189Z Secret source: Actions\n"
            b"2026-10-16T00:56:28.3070161Z Cache mode: write\n"
            b"2026-10-16T00:56:28.3071189Z Contents: write\n"
        )
        self.assertEqual(
            parse_setup_permissions(prefix + SETUP_PERMISSIONS + suffix),
            EXPECTED_PERMISSIONS,
        )

    def test_duplicate_grants_are_rejected_even_when_identical_or_case_aliased(self):
        for grant in (b"Contents: read", b"Contents: write", b"contents: read"):
            with self.subTest(grant=grant):
                log = SETUP_PERMISSIONS.replace(
                    b"2026-10-16T00:56:28.3066988Z ##[endgroup]",
                    b"2026-10-16T00:56:28.3066500Z " + grant + b"\n"
                    b"2026-10-16T00:56:28.3066988Z ##[endgroup]",
                )
                with self.assertRaises(ValueError):
                    parse_setup_permissions(log)

    def test_multiple_groups_are_rejected_regardless_of_grant_agreement(self):
        for second in (
            SETUP_PERMISSIONS,
            SETUP_PERMISSIONS.replace(b"Contents: read", b"Contents: write"),
        ):
            with self.subTest(second=second):
                with self.assertRaises(ValueError):
                    parse_setup_permissions(SETUP_PERMISSIONS + second)

    def test_missing_empty_or_unclosed_group_is_rejected(self):
        for log in (
            b"",
            b"2026-10-16T00:56:28.3065175Z Contents: write\n",
            SETUP_PERMISSIONS.replace(b"GITHUB_TOKEN Permissions", b"Other Permissions"),
            SETUP_PERMISSIONS.split(b"2026-10-16T00:56:28.3066988Z")[0],
            b"2026-10-16T00:56:28.3062401Z ##[group]GITHUB_TOKEN Permissions\n"
            b"2026-10-16T00:56:28.3066988Z ##[endgroup]\n",
        ):
            with self.subTest(log=log):
                with self.assertRaises(ValueError):
                    parse_setup_permissions(log)

    def test_malformed_grants_and_unknown_access_values_are_rejected(self):
        for grant in (
            b"Contents write",
            b"Contents:",
            b": write",
            b"Contents: admin",
            b"Contents: write: read",
            b"Contents: write trailing text",
            b"Contents: write, read",
        ):
            with self.subTest(grant=grant):
                with self.assertRaises(ValueError):
                    parse_setup_permissions(SETUP_PERMISSIONS.replace(b"Contents: read", grant))

    def test_nested_or_malformed_group_framing_is_rejected(self):
        for log in (
            SETUP_PERMISSIONS.replace(
                b"2026-10-16T00:56:28.3064489Z Actions: read\n",
                b"2026-10-16T00:56:28.3064489Z ##[group]Nested\n",
            ),
            SETUP_PERMISSIONS.replace(b"##[group]", b"##[group"),
            SETUP_PERMISSIONS.replace(b"##[endgroup]", b"##[endgroup"),
        ):
            with self.subTest(log=log):
                with self.assertRaises(ValueError):
                    parse_setup_permissions(log)

    def test_invalid_utf8_or_binary_bytes_are_rejected_even_outside_the_group(self):
        for extra in (b"\xff", b"\x00", b"\x1f\x8b"):
            for log in (extra + SETUP_PERMISSIONS, SETUP_PERMISSIONS + extra):
                with self.subTest(extra=extra, position=log.startswith(extra)):
                    with self.assertRaises(ValueError):
                        parse_setup_permissions(log)

    def test_exact_one_mib_limit_is_accepted(self):
        filler = b"2026-10-16T00:56:28.3071189Z "
        padding = filler + b"x" * (MAX_LOG_BYTES - len(SETUP_PERMISSIONS) - len(filler) - 1) + b"\n"
        self.assertEqual(parse_setup_permissions(SETUP_PERMISSIONS + padding), EXPECTED_PERMISSIONS)

    def test_more_than_one_mib_is_rejected_even_with_a_valid_group(self):
        log = SETUP_PERMISSIONS + b"x" * (MAX_LOG_BYTES + 1 - len(SETUP_PERMISSIONS))
        with self.assertRaises(ValueError):
            parse_setup_permissions(log)


class TestPermissionLogRedirect(TestCase):
    def test_https_signed_target_is_preserved_without_interpreting_the_query(self):
        url = "https://logs.example.com/setup.log?signature=synthetic-test-value"
        self.assertEqual(_validate_redirect_url(url), url)

    def test_non_https_userinfo_fragments_and_local_targets_are_refused(self):
        for url in (
            "http://logs.example.com/setup.log",
            "file:///setup.log",
            "//logs.example.com/setup.log",
            "https://user:password@logs.example.com/setup.log",
            "https://@logs.example.com/setup.log",
            "https://logs.example.com/setup.log#fragment",
            "https://logs.example.com:8443/setup.log",
            "https://localhost/setup.log",
            "https://runner.localhost/setup.log",
            "https://runner.local/setup.log",
            "https://runner.internal/setup.log",
            "https://127.0.0.1/setup.log",
            "https://127.1/setup.log",
            "https://0177.0.0.1/setup.log",
            "https://0x7f.0.0.1/setup.log",
            "https://2130706433/setup.log",
            "https://10.0.0.1/setup.log",
            "https://172.16.0.1/setup.log",
            "https://192.168.0.1/setup.log",
            "https://169.254.169.254/setup.log",
            "https://[::1]/setup.log",
            "https://[fc00::1]/setup.log",
            "https://[fe80::1]/setup.log",
            "https://[::ffff:127.0.0.1]/setup.log",
            "https://[fe80::1%25eth0]/setup.log",
        ):
            with self.subTest(url=url):
                with self.assertRaises(_ProbeError):
                    _validate_redirect_url(url)

    def test_invalid_redirect_error_never_echoes_its_signed_query(self):
        for url in (
            "https://127.0.0.1/log?signature=synthetic-sensitive-value",
            "https://logs.example.com/log?signature=synthetic-sensitive-value\n",
            "https://logs.example.com/" + "x" * 8192,
        ):
            with self.subTest(url=url):
                with self.assertRaises(_ProbeError) as raised:
                    _validate_redirect_url(url)
                self.assertEqual(str(raised.exception), "UNSAFE_LOG_REDIRECT")


class TestPermissionProbeContext(TestCase):
    @staticmethod
    def environment():
        return {
            "GITHUB_ACTIONS": "true",
            "GITHUB_EVENT_NAME": "workflow_dispatch",
            "GITHUB_REF": "refs/heads/main",
            "GITHUB_REPOSITORY": REPOSITORY,
            "GITHUB_RUN_ID": str(RUN_ID),
            "GITHUB_RUN_ATTEMPT": str(RUN_ATTEMPT),
            "GITHUB_SHA": HEAD_SHA,
            "GITHUB_JOB": "probe_permissions",
            "GITHUB_WORKFLOW_REF": (
                f"{REPOSITORY}/.github/workflows/stateless-preview.yml@refs/heads/main"
            ),
            "GITHUB_WORKFLOW_SHA": HEAD_SHA,
            "GITHUB_ACTOR": "workflow-trigger-user",
            "GITHUB_TRIGGERING_ACTOR": "workflow-rerun-user",
            "GITHUB_TOKEN": "synthetic-unit-test-token-not-a-credential",
        }

    def test_expected_job_context_returns_the_fixed_api_display_name(self):
        with patch.dict("os.environ", self.environment(), clear=True):
            identity, _ = _read_context()
        self.assertEqual(
            identity,
            {
                "repository": REPOSITORY,
                "run_id": RUN_ID,
                "run_attempt": RUN_ATTEMPT,
                "head_sha": HEAD_SHA,
                "job_name": "Probe current job permissions",
            },
        )

    def test_peer_job_id_is_refused_even_with_the_expected_display_name(self):
        for job_id in ("build_preview", "probe_permissions_peer", "Probe current job permissions"):
            with self.subTest(job_id=job_id):
                environment = self.environment()
                environment["GITHUB_JOB"] = job_id
                environment["GITHUB_JOB_NAME"] = "Probe current job permissions"
                with patch.dict("os.environ", environment, clear=True):
                    with self.assertRaises(_ProbeError):
                        _read_context()

    def test_wrong_workflow_repository_path_or_ref_is_refused(self):
        for workflow_ref in (
            "other/repository/.github/workflows/stateless-preview.yml@refs/heads/main",
            f"{REPOSITORY}/.github/workflows/other.yml@refs/heads/main",
            f"{REPOSITORY}/.github/workflows/stateless-preview.yml@refs/heads/feature",
            f"{REPOSITORY}/.github/workflows/stateless-preview.yml@refs/tags/main",
        ):
            with self.subTest(workflow_ref=workflow_ref):
                environment = self.environment()
                environment["GITHUB_WORKFLOW_REF"] = workflow_ref
                with patch.dict("os.environ", environment, clear=True):
                    with self.assertRaises(_ProbeError):
                        _read_context()

    def test_workflow_sha_must_equal_the_dispatched_source_sha(self):
        for workflow_sha in ("a" * 40, "", "not-a-sha"):
            with self.subTest(workflow_sha=workflow_sha):
                environment = self.environment()
                environment["GITHUB_WORKFLOW_SHA"] = workflow_sha
                with patch.dict("os.environ", environment, clear=True):
                    with self.assertRaises(_ProbeError):
                        _read_context()

    def test_missing_job_or_workflow_binding_is_refused(self):
        for field in ("GITHUB_JOB", "GITHUB_WORKFLOW_REF", "GITHUB_WORKFLOW_SHA"):
            with self.subTest(field=field):
                environment = self.environment()
                del environment[field]
                with patch.dict("os.environ", environment, clear=True):
                    with self.assertRaises(_ProbeError):
                        _read_context()

    def test_actor_provenance_remains_separate_from_job_selection_identity(self):
        with patch.dict("os.environ", self.environment(), clear=True):
            _, actors = _read_context()
        self.assertEqual(
            actors,
            {
                "GITHUB_ACTOR": "workflow-trigger-user",
                "GITHUB_TRIGGERING_ACTOR": "workflow-rerun-user",
            },
        )
