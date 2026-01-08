"""Tests for build state and result types."""


from netcoredbg_mcp.build.state import (
    BuildDiagnostic,
    BuildError,
    BuildErrorSeverity,
    BuildResult,
    BuildState,
    parse_msbuild_output,
)


class TestBuildState:
    """Tests for BuildState enum."""

    def test_state_values(self):
        """Test state enum values."""
        assert BuildState.IDLE.value == "idle"
        assert BuildState.BUILDING.value == "building"
        assert BuildState.READY.value == "ready"
        assert BuildState.FAILED.value == "failed"
        assert BuildState.CANCELLED.value == "cancelled"

    def test_state_is_string(self):
        """Test state is string enum."""
        assert isinstance(BuildState.IDLE, str)
        assert BuildState.IDLE == "idle"


class TestBuildErrorSeverity:
    """Tests for BuildErrorSeverity enum."""

    def test_severity_values(self):
        """Test severity enum values."""
        assert BuildErrorSeverity.ERROR.value == "error"
        assert BuildErrorSeverity.WARNING.value == "warning"
        assert BuildErrorSeverity.INFO.value == "info"


class TestBuildDiagnostic:
    """Tests for BuildDiagnostic dataclass."""

    def test_create_error(self):
        """Test creating error diagnostic."""
        diag = BuildDiagnostic(
            severity=BuildErrorSeverity.ERROR,
            code="CS0103",
            message="The name 'x' does not exist",
            file="Test.cs",
            line=10,
            column=5,
        )

        assert diag.severity == BuildErrorSeverity.ERROR
        assert diag.code == "CS0103"
        assert diag.file == "Test.cs"
        assert diag.line == 10

    def test_to_dict(self):
        """Test converting diagnostic to dict."""
        diag = BuildDiagnostic(
            severity=BuildErrorSeverity.ERROR,
            code="CS0103",
            message="Error message",
            file="Test.cs",
            line=10,
            column=5,
            project="Test.csproj",
        )

        d = diag.to_dict()
        assert d["severity"] == "error"
        assert d["code"] == "CS0103"
        assert d["message"] == "Error message"
        assert d["file"] == "Test.cs"
        assert d["line"] == 10
        assert d["column"] == 5
        assert d["project"] == "Test.csproj"

    def test_to_dict_minimal(self):
        """Test converting minimal diagnostic to dict."""
        diag = BuildDiagnostic(
            severity=BuildErrorSeverity.WARNING,
            code="CS0168",
            message="Variable unused",
        )

        d = diag.to_dict()
        assert "file" not in d
        assert "line" not in d


class TestParseMsbuildOutput:
    """Tests for MSBuild output parsing."""

    def test_parse_detailed_error(self):
        """Test parsing detailed error format."""
        output = "C:\\Project\\Test.cs(10,5): error CS0103: The name 'x' does not exist [C:\\Project\\Test.csproj]"

        diagnostics = parse_msbuild_output(output)

        assert len(diagnostics) == 1
        assert diagnostics[0].severity == BuildErrorSeverity.ERROR
        assert diagnostics[0].code == "CS0103"
        assert diagnostics[0].file == "C:\\Project\\Test.cs"
        assert diagnostics[0].line == 10
        assert diagnostics[0].column == 5
        assert diagnostics[0].project == "C:\\Project\\Test.csproj"

    def test_parse_warning(self):
        """Test parsing warning."""
        output = "C:\\Project\\Test.cs(15,1): warning CS0168: The variable 'x' is declared but never used [C:\\Project\\Test.csproj]"

        diagnostics = parse_msbuild_output(output)

        assert len(diagnostics) == 1
        assert diagnostics[0].severity == BuildErrorSeverity.WARNING
        assert diagnostics[0].code == "CS0168"

    def test_parse_simple_error(self):
        """Test parsing simple error format."""
        output = "error CS1002: ; expected"

        diagnostics = parse_msbuild_output(output)

        assert len(diagnostics) == 1
        assert diagnostics[0].severity == BuildErrorSeverity.ERROR
        assert diagnostics[0].code == "CS1002"
        assert diagnostics[0].file is None

    def test_parse_multiple_diagnostics(self):
        """Test parsing multiple diagnostics."""
        output = """
C:\\Project\\A.cs(1,1): error CS0001: Error 1 [C:\\Project\\Test.csproj]
C:\\Project\\B.cs(2,2): warning CS0002: Warning 1 [C:\\Project\\Test.csproj]
C:\\Project\\C.cs(3,3): error CS0003: Error 2 [C:\\Project\\Test.csproj]
        """

        diagnostics = parse_msbuild_output(output)

        assert len(diagnostics) == 3
        errors = [d for d in diagnostics if d.severity == BuildErrorSeverity.ERROR]
        warnings = [d for d in diagnostics if d.severity == BuildErrorSeverity.WARNING]
        assert len(errors) == 2
        assert len(warnings) == 1

    def test_parse_empty_output(self):
        """Test parsing empty output."""
        diagnostics = parse_msbuild_output("")
        assert diagnostics == []

    def test_parse_non_diagnostic_lines(self):
        """Test non-diagnostic lines are ignored."""
        output = """
Microsoft (R) Build Engine version 17.0.0
Build started 1/1/2024 12:00:00.
Build succeeded.
0 Warning(s)
0 Error(s)
        """

        diagnostics = parse_msbuild_output(output)
        assert diagnostics == []


class TestBuildError:
    """Tests for BuildError exception."""

    def test_create_error(self):
        """Test creating build error."""
        error = BuildError("Build failed", exit_code=1)

        assert str(error) == "Build failed"
        assert error.exit_code == 1
        assert error.diagnostics == []

    def test_create_with_diagnostics(self):
        """Test creating error with diagnostics."""
        diags = [
            BuildDiagnostic(
                severity=BuildErrorSeverity.ERROR,
                code="CS0001",
                message="Test error",
            )
        ]
        error = BuildError("Build failed", diagnostics=diags, exit_code=1)

        assert len(error.diagnostics) == 1

    def test_to_dict(self):
        """Test converting error to dict."""
        diags = [
            BuildDiagnostic(
                severity=BuildErrorSeverity.ERROR,
                code="CS0001",
                message="Test error",
            )
        ]
        error = BuildError("Build failed", diagnostics=diags, exit_code=1)

        d = error.to_dict()
        assert d["error"] == "Build failed"
        assert d["exitCode"] == 1
        assert len(d["diagnostics"]) == 1


class TestBuildResult:
    """Tests for BuildResult dataclass."""

    def test_create_success_result(self):
        """Test creating successful result."""
        result = BuildResult(
            success=True,
            state=BuildState.READY,
            command="build",
            project_path="/test/Project.csproj",
            configuration="Debug",
            exit_code=0,
            duration_ms=1500.5,
        )

        assert result.success is True
        assert result.state == BuildState.READY
        assert result.exit_code == 0
        assert result.error_count == 0

    def test_create_failed_result(self):
        """Test creating failed result."""
        result = BuildResult(
            success=False,
            state=BuildState.FAILED,
            command="build",
            project_path="/test/Project.csproj",
            configuration="Release",
            exit_code=1,
            stdout="C:\\Test.cs(1,1): error CS0001: Error [C:\\Test.csproj]",
        )

        assert result.success is False
        assert result.error_count == 1

    def test_diagnostics_parsed_from_output(self):
        """Test diagnostics are parsed from stdout/stderr."""
        result = BuildResult(
            success=False,
            state=BuildState.FAILED,
            command="build",
            project_path="/test/Project.csproj",
            configuration="Debug",
            stdout="C:\\Test.cs(10,5): error CS0103: Name does not exist [C:\\Test.csproj]",
        )

        assert len(result.diagnostics) == 1
        assert result.diagnostics[0].code == "CS0103"

    def test_error_and_warning_counts(self):
        """Test error and warning count properties."""
        result = BuildResult(
            success=False,
            state=BuildState.FAILED,
            command="build",
            project_path="/test/Project.csproj",
            configuration="Debug",
            stdout="""
C:\\A.cs(1,1): error CS0001: Error 1 [C:\\Test.csproj]
C:\\B.cs(2,2): warning CS0002: Warning 1 [C:\\Test.csproj]
C:\\C.cs(3,3): error CS0003: Error 2 [C:\\Test.csproj]
            """,
        )

        assert result.error_count == 2
        assert result.warning_count == 1

    def test_to_dict(self):
        """Test converting result to dict."""
        result = BuildResult(
            success=True,
            state=BuildState.READY,
            command="build",
            project_path="/test/Project.csproj",
            configuration="Debug",
            exit_code=0,
            duration_ms=1234.56,
        )

        d = result.to_dict()
        assert d["success"] is True
        assert d["state"] == "ready"
        assert d["command"] == "build"
        assert d["projectPath"] == "/test/Project.csproj"
        assert d["configuration"] == "Debug"
        assert d["exitCode"] == 0
        assert d["durationMs"] == 1234.56

    def test_to_summary_success(self):
        """Test summary for successful build."""
        result = BuildResult(
            success=True,
            state=BuildState.READY,
            command="build",
            project_path="/test/Project.csproj",
            configuration="Debug",
            duration_ms=1000,
        )

        summary = result.to_summary()
        assert "succeeded" in summary
        assert "/test/Project.csproj" in summary

    def test_to_summary_failed(self):
        """Test summary for failed build."""
        result = BuildResult(
            success=False,
            state=BuildState.FAILED,
            command="build",
            project_path="/test/Project.csproj",
            configuration="Debug",
            stdout="C:\\Test.cs(1,1): error CS0001: Test error [C:\\Test.csproj]",
        )

        summary = result.to_summary()
        assert "failed" in summary
        assert "CS0001" in summary

    def test_to_summary_cancelled(self):
        """Test summary for cancelled build."""
        result = BuildResult(
            success=False,
            state=BuildState.CANCELLED,
            command="build",
            project_path="/test/Project.csproj",
            configuration="Debug",
            cancelled=True,
        )

        summary = result.to_summary()
        assert "cancelled" in summary
