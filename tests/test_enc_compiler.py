from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from netcoredbg_mcp.enc.compiler import SourceEdit, compile_delta


@pytest.fixture(scope="session")
def enc_compiler_dll() -> Path:
    subprocess.run(
        ["dotnet", "build", "tools/enc_compiler/", "-v", "quiet"],
        check=True,
        cwd=Path(__file__).resolve().parents[1],
    )
    return (
        Path(__file__).resolve().parents[1]
        / "tools"
        / "enc_compiler"
        / "bin"
        / "Debug"
        / "net8.0"
        / "enc_compiler.dll"
    )


def test_compile_delta_emits_files_for_method_body_change(
    tmp_path: Path, enc_compiler_dll: Path
):
    project, source = _write_fixture_project(tmp_path)
    return_line = _line_number(source, "return 1;")
    output_dir = tmp_path / "deltas"

    result = compile_delta(
        project,
        source,
        [SourceEdit(start_line=return_line, end_line=return_line, new_text="        return 2;")],
        compiler_path=enc_compiler_dll,
        output_dir=output_dir,
    )

    assert result.success, result.diagnostics
    assert result.rude_edits == ()
    for delta_path in (result.il_delta_path, result.metadata_delta_path, result.pdb_delta_path):
        assert delta_path is not None
        assert Path(delta_path).is_file()
        assert Path(delta_path).stat().st_size > 0


def test_compile_delta_uses_loaded_module_and_portable_pdb_baseline(
    tmp_path: Path, enc_compiler_dll: Path
):
    project, source = _write_fixture_project(tmp_path)
    subprocess.run(["dotnet", "build", project, "-v", "quiet"], check=True)
    module_path = tmp_path / "bin" / "Debug" / "net8.0" / "Sample.dll"
    return_line = _line_number(source, "return 1;")
    output_dir = tmp_path / "deltas"

    result = compile_delta(
        project,
        source,
        [SourceEdit(start_line=return_line, end_line=return_line, new_text="        return 2;")],
        compiler_path=enc_compiler_dll,
        module_path=module_path,
        output_dir=output_dir,
    )

    assert result.success, result.diagnostics
    assert result.rude_edits == ()
    for delta_path in (result.il_delta_path, result.metadata_delta_path, result.pdb_delta_path):
        assert delta_path is not None
        assert Path(delta_path).is_file()
        assert Path(delta_path).stat().st_size > 0


def test_compile_delta_reports_rude_edit_for_added_field(
    tmp_path: Path, enc_compiler_dll: Path
):
    project, source = _write_fixture_project(tmp_path)
    method_line = _line_number(source, "public int GetValue()")

    result = compile_delta(
        project,
        source,
        [
            SourceEdit(
                start_line=method_line,
                end_line=method_line,
                new_text="    private int _value;\n\n    public int GetValue()",
            )
        ],
        compiler_path=enc_compiler_dll,
        output_dir=tmp_path / "deltas",
    )

    assert result.success is False
    assert result.diagnostics == ()
    assert any("cannot add field" in rude_edit for rude_edit in result.rude_edits)


def test_compile_delta_returns_error_for_invalid_file(
    tmp_path: Path, enc_compiler_dll: Path
):
    project, _source = _write_fixture_project(tmp_path)

    result = compile_delta(
        project,
        tmp_path / "Missing.cs",
        [SourceEdit(start_line=1, end_line=1, new_text="")],
        compiler_path=enc_compiler_dll,
        output_dir=tmp_path / "deltas",
    )

    assert result.success is False
    assert result.rude_edits == ()
    assert any("Target file not found" in diagnostic for diagnostic in result.diagnostics)


def test_compile_delta_resolves_project_reference_metadata(
    tmp_path: Path, enc_compiler_dll: Path
):
    dependency_project = tmp_path / "Dependency" / "Dependency.csproj"
    dependency_project.parent.mkdir()
    dependency_project.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )
    (dependency_project.parent / "ExternalValue.cs").write_text(
        """namespace Dependency;

public sealed class ExternalValue
{
    public int Value { get; set; }
}
""",
        encoding="utf-8",
    )
    subprocess.run(["dotnet", "build", dependency_project, "-v", "quiet"], check=True)

    project = tmp_path / "Sample" / "Sample.csproj"
    project.parent.mkdir()
    project.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
  <ItemGroup>
    <ProjectReference Include="..\\Dependency\\Dependency.csproj" />
  </ItemGroup>
</Project>
""",
        encoding="utf-8",
    )
    source = project.parent / "Sample.cs"
    source.write_text(
        """using Dependency;

namespace Fixture;

public class Sample
{
    public int GetValue()
    {
        return new ExternalValue { Value = 1 }.Value;
    }
}
""",
        encoding="utf-8",
    )
    return_line = _line_number(source, "Value = 1")

    result = compile_delta(
        project,
        source,
        [
            SourceEdit(
                start_line=return_line,
                end_line=return_line,
                new_text="        return new ExternalValue { Value = 2 }.Value;",
            )
        ],
        compiler_path=enc_compiler_dll,
        output_dir=tmp_path / "deltas",
    )

    assert result.success, result.diagnostics


def test_enc_compiler_rejects_null_edits(enc_compiler_dll: Path, tmp_path: Path):
    project, source = _write_fixture_project(tmp_path)

    completed = subprocess.run(
        ["dotnet", str(enc_compiler_dll)],
        input=(
            "{"
            f"\"project_path\": {str(project)!r}, "
            f"\"file_path\": {str(source)!r}, "
            "\"edits\": null"
            "}"
        ).replace("'", '"'),
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 1
    assert "edits is required" in completed.stdout


def _write_fixture_project(tmp_path: Path) -> tuple[Path, Path]:
    project = tmp_path / "Sample.csproj"
    project.write_text(
        """<Project Sdk="Microsoft.NET.Sdk">
  <PropertyGroup>
    <TargetFramework>net8.0</TargetFramework>
  </PropertyGroup>
</Project>
""",
        encoding="utf-8",
    )
    source = tmp_path / "Sample.cs"
    source.write_text(
        """namespace Fixture;

public class Sample
{
    public int GetValue()
    {
        return 1;
    }
}
""",
        encoding="utf-8",
    )
    return project, source


def _line_number(path: Path, needle: str) -> int:
    for index, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if needle in line:
            return index
    raise AssertionError(f"Could not find line containing {needle!r}")
