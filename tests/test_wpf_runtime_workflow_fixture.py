"""WPF runtime workflow fixture contract tests."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "WpfSmokeApp"
SUBMENU_CONSUMER_PROOF = Path(__file__).with_name("wpf_submenu_consumer.py")


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


def test_wpf_fixture_declares_cue_grid_drag_drop_bindings() -> None:
    xaml = (FIXTURE_ROOT / "MainWindow.xaml").read_text(encoding="utf-8")

    assert 'PreviewMouseLeftButtonDown="CueDataGrid_PreviewMouseLeftButtonDown"' in xaml
    assert 'PreviewMouseMove="CueDataGrid_PreviewMouseMove"' in xaml
    assert 'DragOver="CueDataGrid_DragOver"' in xaml
    assert 'Drop="CueDataGrid_Drop"' in xaml
    assert 'AllowDrop="True"' in xaml


def test_wpf_fixture_code_reports_deterministic_reorder_status() -> None:
    code = (FIXTURE_ROOT / "MainWindow.xaml.cs").read_text(encoding="utf-8")

    assert "CueDataGrid_PreviewMouseLeftButtonDown" in code
    assert "CueDataGrid_PreviewMouseMove" in code
    assert "CueDataGrid_DragOver" in code
    assert "CueDataGrid_Drop" in code
    assert "WpfWorkflow DragReorder" in code
    assert "WpfWorkflow DragEdgeScroll" in code
    assert "edgeScrollDirection=" in code
    assert "sourceIdentity=" in code
    assert "targetIdentity=" in code
    assert "orderFingerprint=" in code


def test_wpf_fixture_reports_selected_drag_payload_evidence() -> None:
    code = (FIXTURE_ROOT / "MainWindow.xaml.cs").read_text(encoding="utf-8")

    assert "CueDragPayload" in code
    assert "BuildCueDragPayload" in code
    assert "MoveCueRows" in code
    assert "selectedPayloadBefore=" in code
    assert "selectedPayloadAfter=" in code
    assert "selectedPayloadMode=" in code
    assert "IsContiguousSelection" in code


def test_wpf_fixture_seeds_enough_rows_for_edge_scroll_evidence() -> None:
    code = (FIXTURE_ROOT / "MainWindow.xaml.cs").read_text(encoding="utf-8")

    cue_rows = re.findall(r'new\("00:00:\d{2}\.0"', code)
    assert len(cue_rows) >= 20


def test_wpf_fixture_exposes_fixed_in_window_hover_overlay() -> None:
    xaml = (FIXTURE_ROOT / "MainWindow.xaml").read_text(encoding="utf-8")

    for automation_id in (
        "hoverRegion",
        "hoverTrigger",
        "hoverFlyoutSurface",
        "hoverOutsideSentinel",
        "hoverFocusSentinel",
        "hoverStatus",
    ):
        assert f'AutomationProperties.AutomationId="{automation_id}"' in xaml

    assert 'x:Name="HoverOverlay"' in xaml
    assert 'Width="260"' in xaml
    assert 'Height="220"' in xaml
    assert 'MouseEnter="HoverTrigger_MouseEnter"' in xaml
    assert 'MouseEnter="HoverFlyoutSurface_MouseEnter"' in xaml
    assert 'MouseEnter="HoverOutsideSentinel_MouseEnter"' in xaml
    assert '<GroupBox x:Name="HoverOverlay"' in xaml
    assert '<Button x:Name="HoverTrigger"' in xaml
    assert '<GroupBox x:Name="HoverFlyoutSurface"' in xaml
    assert '<Button x:Name="HoverOutsideSentinel"' in xaml
    assert xaml.count('AutomationProperties.AutomationId="hoverDuplicateRoot"') == 2
    assert xaml.count('AutomationProperties.AutomationId="hoverDuplicateTarget"') == 2
    assert '<Canvas Grid.Row="3"' in xaml
    assert 'HorizontalAlignment="Right" VerticalAlignment="Bottom"' in xaml
    assert "<Popup" not in xaml
    assert "<ContextMenu" not in xaml
    assert "<ToolTip" not in xaml


def test_wpf_fixture_hover_status_arms_post_focus_measurement_and_delayed_close() -> None:
    code = (FIXTURE_ROOT / "MainWindow.xaml.cs").read_text(encoding="utf-8")

    assert "HoverCloseDelayMs = 500" in code
    assert "HoverFocusSentinel_GotKeyboardFocus" in code
    assert "ResetAndArmHoverMeasurement" in code
    assert "measurementArmed" in code
    assert "PreviewMouseLeftButtonDownCount" in code
    assert "PreviewMouseLeftButtonUpCount" in code
    assert "ClickCount" in code
    assert "FocusChangeCount" in code
    assert 'SetHoverState("open_trigger", surfaceVisible: true)' in code
    assert 'SetHoverState("open_flyout", surfaceVisible: true)' in code
    assert 'SetHoverState("close_pending", surfaceVisible: true)' in code
    assert 'SetHoverState("closed", surfaceVisible: false)' in code


def test_wpf_submenu_fixture_uses_native_menu_keyboard_behavior() -> None:
    xaml = (FIXTURE_ROOT / "MainWindow.xaml").read_text(encoding="utf-8")
    code = (FIXTURE_ROOT / "MainWindow.xaml.cs").read_text(encoding="utf-8")

    assert 'AutomationProperties.AutomationId="submenuParent"' in xaml
    assert 'AutomationProperties.AutomationId="submenuChild"' in xaml
    assert 'PreviewKeyDown="SubmenuParent_PreviewKeyDown"' not in xaml
    assert "SubmenuParent_PreviewKeyDown" not in code


def test_wpf_submenu_parent_native_enter_rediscovers_popup_child_and_invokes_it(
    tmp_path: Path,
) -> None:
    """Exercise the built wheel through its installed CLI and MCP tools/call route."""

    if sys.platform != "win32":
        pytest.skip("WPF submenu consumer proof requires Windows")

    debugger_path = os.environ.get("NETCOREDBG_PATH")
    if not debugger_path or not Path(debugger_path).is_file():
        pytest.skip("WPF submenu consumer proof requires NETCOREDBG_PATH")

    fixture_build = subprocess.run(
        ["dotnet", "build", str(FIXTURE_ROOT), "-c", "Debug"],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert fixture_build.returncode == 0, fixture_build.stdout + fixture_build.stderr

    wheel_dir = tmp_path / "wheel"
    wheel_build = subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(wheel_dir)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert wheel_build.returncode == 0, wheel_build.stdout + wheel_build.stderr
    wheels = sorted(wheel_dir.glob("netcoredbg_mcp-*.whl"))
    assert len(wheels) == 1, f"expected one wheel, found: {wheels}"

    consumer_root = tmp_path / "consumer"
    create_consumer = subprocess.run(
        ["uv", "venv", "--python", sys.executable, str(consumer_root)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert create_consumer.returncode == 0, create_consumer.stdout + create_consumer.stderr

    scripts_dir = consumer_root / "Scripts"
    consumer_python = scripts_dir / "python.exe"
    consumer_cli = scripts_dir / "netcoredbg-mcp.exe"
    install_wheel = subprocess.run(
        ["uv", "pip", "install", "--python", str(consumer_python), str(wheels[0])],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert install_wheel.returncode == 0, install_wheel.stdout + install_wheel.stderr
    assert consumer_cli.is_file(), f"wheel did not install CLI: {consumer_cli}"
    installed_bridge_source = consumer_root / "Lib" / "site-packages" / "netcoredbg_mcp" / "bridge"
    installed_bridge_project = installed_bridge_source / "FlaUIBridge.csproj"
    assert installed_bridge_project.is_file(), (
        f"wheel did not install bridge source: {installed_bridge_project}"
    )

    bridge_output = tmp_path / "installed-wheel-bridge"
    bridge_publish = subprocess.run(
        [
            "dotnet",
            "publish",
            str(installed_bridge_project),
            "-c",
            "Release",
            "-r",
            "win-x64",
            "--self-contained",
            "-o",
            str(bridge_output),
        ],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert bridge_publish.returncode == 0, bridge_publish.stdout + bridge_publish.stderr
    bridge_path = bridge_output / "FlaUIBridge.exe"
    assert bridge_path.is_file(), f"packaged bridge publish did not produce: {bridge_path}"

    consumer_env = dict(os.environ)
    consumer_env.pop("PYTHONPATH", None)
    consumer_env.update(
        {
            "NETCOREDBG_MCP_CONSUMER_CLI": str(consumer_cli),
            "NETCOREDBG_MCP_WPF_ROOT": str(FIXTURE_ROOT),
            "FLAUI_BRIDGE_PATH": str(bridge_path),
        }
    )
    consumer = subprocess.run(
        [str(consumer_python), str(SUBMENU_CONSUMER_PROOF)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        env=consumer_env,
        timeout=120,
        check=False,
    )
    print(consumer.stdout, end="")
    assert consumer.returncode == 0, consumer.stdout + consumer.stderr
    assert "WPF installed submenu evidence:" in consumer.stdout
