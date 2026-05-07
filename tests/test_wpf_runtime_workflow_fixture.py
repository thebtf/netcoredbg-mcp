"""WPF runtime workflow fixture contract tests."""

from __future__ import annotations

from pathlib import Path

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "WpfSmokeApp"


def test_wpf_fixture_exposes_cue_grid_character_list_and_undo_contract() -> None:
    xaml = (FIXTURE_ROOT / "MainWindow.xaml").read_text(encoding="utf-8")

    assert 'AutomationProperties.AutomationId="dataGrid"' in xaml
    assert 'Header="Start"' in xaml
    assert 'Header="End"' in xaml
    assert 'Header="Character"' in xaml
    assert 'Header="Phrase"' in xaml
    assert 'AutomationProperties.AutomationId="CharactersListBox"' in xaml
    assert 'Property="AutomationProperties.Name" Value="{Binding Name}"' in xaml
    assert 'AutomationProperties.AutomationId="CharGender"' in xaml
    assert 'AutomationProperties.AutomationId="menuItemUndo"' in xaml
    assert 'AutomationProperties.AutomationId="genderStatus"' in xaml


def test_wpf_fixture_code_provides_assignment_toggle_undo_and_output_markers() -> None:
    code = (FIXTURE_ROOT / "MainWindow.xaml.cs").read_text(encoding="utf-8")

    assert 'new("00:00:01.0", "00:00:03.0", "Narrator", "Fixture cue one")' in code
    assert 'new("00:00:04.0", "00:00:06.0", "Narrator", "Fixture cue two")' in code
    assert 'new("ALICE"' in code
    assert 'new("BOB"' in code
    assert "AssignCharacter" in code
    assert "ToggleGender" in code
    assert "UndoLatest" in code
    assert "WpfWorkflow AssignCharacter route=ListInvoke selectedCount=" in code
    assert "WpfWorkflow ToggleGender character=" in code
    assert "WpfWorkflow Undo route=Menu" in code
    assert "WPF_SMOKE_MUTABLE_FILE" in code
    assert "CueDataGrid.Focus()" in code
