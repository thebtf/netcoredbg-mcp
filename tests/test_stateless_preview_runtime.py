"""Behavioral checks for the published preview's native runtime evidence."""

from __future__ import annotations

from unittest import TestCase

from scripts.stateless_preview_runtime import RuntimeGuardError, validate_runtime_trace


# These are the .NET 8 native host's metadata records, not application stdout.
TRACE = """--- Invoked apphost [version: 8.0.32 @Commit: b9a49e4]
Detected Single-File app bundle
Using internal fxr
--- Invoked hostfxr_main_bundle_startupinfo [version: 8.0.32 @Commit: b9a49e4]
Executing as a self-contained app as per config file [preview.runtimeconfig.json]
Using internal hostpolicy
--- Invoked hostpolicy [version: 8.0.32 @Commit: b9a49e4] corehost_main = {
Loading deps file... [preview.deps.json] as framework dependent=0, use_fallback_graph=0
Processing package runtimepack.Microsoft.NETCore.App.Runtime.win-x64/8.0.32
"""


class TestStatelessPreviewRuntime(TestCase):
    def test_matching_embedded_runtime_is_accepted(self):
        self.assertEqual(validate_runtime_trace(TRACE, "8.0.32"), "8.0.32")

    def test_windows_trace_line_endings_are_accepted(self):
        self.assertEqual(validate_runtime_trace(TRACE.replace("\n", "\r\n"), "8.0.32"), "8.0.32")

    def test_old_runtime_is_rejected_by_independent_expected_patch(self):
        with self.assertRaises(RuntimeGuardError):
            validate_runtime_trace(TRACE.replace("8.0.32", "8.0.31"), "8.0.32")

    def test_each_host_component_must_be_present_and_match(self):
        for component in ("apphost", "hostfxr_main_bundle_startupinfo", "hostpolicy"):
            line = next(
                line
                for line in TRACE.splitlines(True)
                if line.startswith(f"--- Invoked {component} [")
            )
            for replacement in (
                "",
                line.replace("8.0.32", "8.0.31"),
                line + line.replace("8.0.32", "8.0.31"),
            ):
                with self.subTest(component=component, replacement=replacement):
                    with self.assertRaises(RuntimeGuardError):
                        validate_runtime_trace(TRACE.replace(line, replacement), "8.0.32")

    def test_runtime_pack_must_be_present_match_and_target_win_x64(self):
        line = "Processing package runtimepack.Microsoft.NETCore.App.Runtime.win-x64/8.0.32\n"
        for replacement in (
            "",
            line.replace("8.0.32", "8.0.31"),
            line + line.replace("8.0.32", "8.0.31"),
            line.replace("win-x64", "linux-x64"),
            line.replace("Microsoft.NETCore.App", "Microsoft.AspNetCore.App"),
        ):
            with self.subTest(replacement=replacement):
                with self.assertRaises(RuntimeGuardError):
                    validate_runtime_trace(TRACE.replace(line, replacement), "8.0.32")

    def test_nonbundle_or_external_host_components_are_rejected(self):
        for marker in (
            "Detected Single-File app bundle\n",
            "Using internal fxr\n",
            "Using internal hostpolicy\n",
        ):
            with self.subTest(marker=marker):
                with self.assertRaises(RuntimeGuardError):
                    validate_runtime_trace(TRACE.replace(marker, ""), "8.0.32")

    def test_framework_dependent_or_missing_self_contained_evidence_is_rejected(self):
        self_contained = (
            "Executing as a self-contained app as per config file [preview.runtimeconfig.json]\n"
        )
        deps = "Loading deps file... [preview.deps.json] as framework dependent=0, use_fallback_graph=0\n"
        for trace in (
            TRACE.replace(self_contained, ""),
            TRACE.replace(
                self_contained,
                "Executing as a framework-dependent app as per config file "
                "[preview.runtimeconfig.json]\n",
            ),
            TRACE.replace(deps, ""),
            TRACE.replace("framework dependent=0", "framework dependent=1"),
            TRACE + deps.replace("framework dependent=0", "framework dependent=1"),
        ):
            with self.subTest(trace=trace):
                with self.assertRaises(RuntimeGuardError):
                    validate_runtime_trace(trace, "8.0.32")

    def test_empty_trace_and_version_mentions_without_native_records_are_rejected(self):
        for trace in ("", "8.0.32 apphost hostfxr_main_bundle_startupinfo hostpolicy win-x64"):
            with self.subTest(trace=trace):
                with self.assertRaises(RuntimeGuardError):
                    validate_runtime_trace(trace, "8.0.32")
