"""Tests for build process cleanup utilities."""

import asyncio
import os
from unittest.mock import AsyncMock, patch

import pytest

from netcoredbg_mcp.build.cleanup import (
    KNOWN_DEBUGGER_PROCESSES,
    cleanup_for_build,
    kill_debugger_processes,
    kill_processes_in_directory,
)


class TestKnownDebuggerProcesses:
    """Tests for known debugger process constants."""

    def test_known_processes_includes_netcoredbg(self):
        """Test that netcoredbg is in known processes."""
        assert "netcoredbg" in KNOWN_DEBUGGER_PROCESSES
        assert "netcoredbg.exe" in KNOWN_DEBUGGER_PROCESSES

    def test_known_processes_includes_dotnet(self):
        """Test that dotnet is in known processes."""
        assert "dotnet" in KNOWN_DEBUGGER_PROCESSES
        assert "dotnet.exe" in KNOWN_DEBUGGER_PROCESSES

    def test_known_processes_is_frozenset(self):
        """Test that known processes is immutable."""
        assert isinstance(KNOWN_DEBUGGER_PROCESSES, frozenset)


class TestKillProcessesInDirectoryWindows:
    """Tests for kill_processes_in_directory on Windows."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_processes(self, tmp_path):
        """Test returns 0 when no processes found."""
        with patch("os.name", "nt"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_proc = AsyncMock()
                mock_proc.communicate = AsyncMock(return_value=(b"Node,ExecutablePath,ProcessId\n", b""))
                mock_exec.return_value = mock_proc

                result = await kill_processes_in_directory(str(tmp_path))

                assert result == 0

    @pytest.mark.asyncio
    async def test_kills_process_in_directory(self, tmp_path):
        """Test kills process whose executable is in directory."""
        dir_path = str(tmp_path.resolve())
        exe_path = str(tmp_path / "test.exe")

        wmic_output = f"Node,ExecutablePath,ProcessId\nMYPC,{exe_path},12345\n"

        with patch("os.name", "nt"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                # First call is WMIC
                wmic_proc = AsyncMock()
                wmic_proc.communicate = AsyncMock(return_value=(wmic_output.encode(), b""))

                # Second call is taskkill
                taskkill_proc = AsyncMock()
                taskkill_proc.wait = AsyncMock(return_value=0)

                mock_exec.side_effect = [wmic_proc, taskkill_proc]

                result = await kill_processes_in_directory(dir_path)

                assert result == 1
                # Verify taskkill was called with correct PID
                calls = mock_exec.call_args_list
                assert len(calls) == 2
                taskkill_call = calls[1]
                assert "taskkill" in taskkill_call[0]
                assert "12345" in taskkill_call[0]

    @pytest.mark.asyncio
    async def test_ignores_process_outside_directory(self, tmp_path):
        """Test ignores processes outside target directory."""
        wmic_output = "Node,ExecutablePath,ProcessId\nMYPC,C:\\Other\\test.exe,12345\n"

        with patch("os.name", "nt"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_proc = AsyncMock()
                mock_proc.communicate = AsyncMock(return_value=(wmic_output.encode(), b""))
                mock_exec.return_value = mock_proc

                result = await kill_processes_in_directory(str(tmp_path))

                assert result == 0

    @pytest.mark.asyncio
    async def test_handles_timeout(self, tmp_path):
        """Test handles timeout gracefully."""
        with patch("os.name", "nt"):
            with patch("asyncio.wait_for") as mock_wait:
                mock_wait.side_effect = asyncio.TimeoutError()

                result = await kill_processes_in_directory(str(tmp_path), timeout=0.1)

                assert result == 0

    @pytest.mark.asyncio
    async def test_handles_exception(self, tmp_path):
        """Test handles exceptions gracefully."""
        with patch("os.name", "nt"):
            with patch("asyncio.wait_for") as mock_wait:
                mock_wait.side_effect = Exception("Test error")

                result = await kill_processes_in_directory(str(tmp_path))

                assert result == 0


@pytest.mark.skipif(os.name == "nt", reason="Unix tests cannot run on Windows")
class TestKillProcessesInDirectoryUnix:
    """Tests for kill_processes_in_directory on Unix."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_processes(self, tmp_path):
        """Test returns 0 when no processes found."""
        with patch("asyncio.wait_for") as mock_wait:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"", b""))
            mock_wait.return_value = mock_proc

            result = await kill_processes_in_directory(str(tmp_path))

            assert result == 0

    @pytest.mark.asyncio
    async def test_kills_processes_from_lsof(self, tmp_path):
        """Test kills processes found by lsof."""
        with patch("asyncio.wait_for") as mock_wait:
            mock_proc = AsyncMock()
            mock_proc.communicate = AsyncMock(return_value=(b"12345\n", b""))
            mock_wait.return_value = mock_proc

            with patch("os.kill") as mock_kill:
                result = await kill_processes_in_directory(str(tmp_path))

                assert result == 1
                mock_kill.assert_called_once_with(12345, 9)

    @pytest.mark.asyncio
    async def test_handles_lsof_not_found(self, tmp_path):
        """Test handles missing lsof gracefully."""
        with patch("asyncio.wait_for") as mock_wait:
            mock_wait.side_effect = FileNotFoundError("lsof not found")

            result = await kill_processes_in_directory(str(tmp_path))

            assert result == 0


class TestKillDebuggerProcesses:
    """Tests for kill_debugger_processes."""

    @pytest.mark.asyncio
    async def test_returns_zero_when_no_params(self):
        """Test returns 0 when no parameters provided."""
        result = await kill_debugger_processes()
        assert result == 0

    @pytest.mark.asyncio
    async def test_kills_netcoredbg_by_pid_windows(self):
        """Test kills netcoredbg by PID on Windows."""
        with patch("os.name", "nt"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_proc = AsyncMock()
                mock_proc.wait = AsyncMock(return_value=0)
                mock_exec.return_value = mock_proc

                result = await kill_debugger_processes(netcoredbg_pid=12345)

                assert result == 1
                mock_exec.assert_called_once()
                call_args = mock_exec.call_args[0]
                assert "taskkill" in call_args
                assert "12345" in call_args

    @pytest.mark.asyncio
    async def test_kills_netcoredbg_by_pid_unix(self):
        """Test kills netcoredbg by PID on Unix."""
        with patch("os.name", "posix"):
            with patch("os.kill") as mock_kill:
                result = await kill_debugger_processes(netcoredbg_pid=12345)

                assert result == 1
                mock_kill.assert_called_once_with(12345, 9)

    @pytest.mark.asyncio
    async def test_kills_program_by_path_windows(self):
        """Test kills program by path on Windows."""
        with patch("os.name", "nt"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_proc = AsyncMock()
                mock_proc.wait = AsyncMock(return_value=0)
                mock_exec.return_value = mock_proc

                result = await kill_debugger_processes(program_path="/path/to/MyApp.exe")

                assert result == 1
                call_args = mock_exec.call_args[0]
                assert "taskkill" in call_args
                assert "MyApp.exe" in call_args

    @pytest.mark.asyncio
    async def test_kills_program_by_path_unix(self):
        """Test kills program by path on Unix."""
        with patch("os.name", "posix"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_proc = AsyncMock()
                mock_proc.wait = AsyncMock(return_value=0)
                mock_exec.return_value = mock_proc

                result = await kill_debugger_processes(program_path="/path/to/myapp")

                assert result == 1
                call_args = mock_exec.call_args[0]
                assert "pkill" in call_args

    @pytest.mark.asyncio
    async def test_handles_kill_failure(self):
        """Test handles kill failure gracefully."""
        with patch("os.name", "nt"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_exec.side_effect = Exception("Access denied")

                result = await kill_debugger_processes(netcoredbg_pid=12345)

                # Should not raise, returns 0
                assert result == 0

    @pytest.mark.asyncio
    async def test_kill_all_netcoredbg_windows(self):
        """Test kills all netcoredbg processes on Windows."""
        with patch("os.name", "nt"):
            with patch("asyncio.create_subprocess_exec") as mock_exec:
                mock_proc = AsyncMock()
                mock_proc.wait = AsyncMock(return_value=0)
                mock_exec.return_value = mock_proc

                result = await kill_debugger_processes(kill_all_netcoredbg=True)

                assert result == 1
                call_args = mock_exec.call_args[0]
                assert "taskkill" in call_args
                assert "netcoredbg.exe" in call_args


class TestCleanupForBuild:
    """Tests for cleanup_for_build."""

    @pytest.mark.asyncio
    async def test_calls_kill_debugger_processes(self, tmp_path):
        """Test calls kill_debugger_processes with provided params."""
        project = tmp_path / "Test.csproj"
        project.touch()

        with patch("netcoredbg_mcp.build.cleanup.kill_debugger_processes") as mock_kill_dbg:
            with patch("netcoredbg_mcp.build.cleanup.kill_processes_in_directory") as mock_kill_dir:
                mock_kill_dbg.return_value = 1
                mock_kill_dir.return_value = 0

                result = await cleanup_for_build(
                    str(project),
                    program_path="/path/to/app.exe",
                    netcoredbg_pid=12345,
                )

                mock_kill_dbg.assert_called_once_with(
                    program_path="/path/to/app.exe",
                    netcoredbg_pid=12345,
                    kill_all_netcoredbg=True,  # Default is True now
                )
                assert result >= 1

    @pytest.mark.asyncio
    async def test_scans_debug_and_release_directories(self, tmp_path):
        """Test scans both Debug and Release directories."""
        project = tmp_path / "Test.csproj"
        project.touch()
        (tmp_path / "bin" / "Debug").mkdir(parents=True)
        (tmp_path / "bin" / "Release").mkdir(parents=True)

        with patch("netcoredbg_mcp.build.cleanup.kill_debugger_processes") as mock_kill_dbg:
            with patch("netcoredbg_mcp.build.cleanup.kill_processes_in_directory") as mock_kill_dir:
                mock_kill_dbg.return_value = 0
                mock_kill_dir.return_value = 1

                result = await cleanup_for_build(str(project))

                # Should scan both Debug and Release
                assert mock_kill_dir.call_count >= 2

    @pytest.mark.asyncio
    async def test_scans_custom_configurations(self, tmp_path):
        """Test scans custom configurations."""
        project = tmp_path / "Test.csproj"
        project.touch()
        (tmp_path / "bin" / "CustomConfig").mkdir(parents=True)

        with patch("netcoredbg_mcp.build.cleanup.kill_debugger_processes") as mock_kill_dbg:
            with patch("netcoredbg_mcp.build.cleanup.kill_processes_in_directory") as mock_kill_dir:
                mock_kill_dbg.return_value = 0
                mock_kill_dir.return_value = 1

                result = await cleanup_for_build(
                    str(project),
                    configurations=["CustomConfig"],
                )

                # Should find CustomConfig directory
                call_args = [str(c[0][0]) for c in mock_kill_dir.call_args_list]
                assert any("CustomConfig" in arg for arg in call_args)

    @pytest.mark.asyncio
    async def test_handles_missing_directories(self, tmp_path):
        """Test handles missing bin directories gracefully."""
        project = tmp_path / "Test.csproj"
        project.touch()
        # Don't create bin directories

        with patch("netcoredbg_mcp.build.cleanup.kill_debugger_processes") as mock_kill_dbg:
            with patch("netcoredbg_mcp.build.cleanup.kill_processes_in_directory") as mock_kill_dir:
                mock_kill_dbg.return_value = 0
                mock_kill_dir.return_value = 0

                # Should not raise
                result = await cleanup_for_build(str(project))

                assert result == 0

    @pytest.mark.asyncio
    async def test_returns_total_killed_count(self, tmp_path):
        """Test returns total count of killed processes."""
        project = tmp_path / "Test.csproj"
        project.touch()
        (tmp_path / "bin" / "Debug").mkdir(parents=True)
        (tmp_path / "bin" / "Release").mkdir(parents=True)

        with patch("netcoredbg_mcp.build.cleanup.kill_debugger_processes") as mock_kill_dbg:
            with patch("netcoredbg_mcp.build.cleanup.kill_processes_in_directory") as mock_kill_dir:
                mock_kill_dbg.return_value = 2
                mock_kill_dir.return_value = 3

                result = await cleanup_for_build(
                    str(project),
                    program_path="/app.exe",
                    netcoredbg_pid=123,
                )

                # 2 from debugger + (3 * number of directories scanned)
                assert result >= 2

    @pytest.mark.asyncio
    async def test_accepts_directory_as_project_path(self, tmp_path):
        """Test accepts directory path instead of project file."""
        (tmp_path / "bin" / "Debug").mkdir(parents=True)

        with patch("netcoredbg_mcp.build.cleanup.kill_debugger_processes") as mock_kill_dbg:
            with patch("netcoredbg_mcp.build.cleanup.kill_processes_in_directory") as mock_kill_dir:
                mock_kill_dbg.return_value = 0
                mock_kill_dir.return_value = 0

                # Should not raise
                result = await cleanup_for_build(str(tmp_path))

                assert result == 0

    @pytest.mark.asyncio
    async def test_adds_delay_after_killing(self, tmp_path):
        """Test adds delay after killing processes."""
        project = tmp_path / "Test.csproj"
        project.touch()
        (tmp_path / "bin" / "Debug").mkdir(parents=True)

        with patch("netcoredbg_mcp.build.cleanup.kill_debugger_processes") as mock_kill_dbg:
            with patch("netcoredbg_mcp.build.cleanup.kill_processes_in_directory") as mock_kill_dir:
                with patch("asyncio.sleep") as mock_sleep:
                    mock_kill_dbg.return_value = 1
                    mock_kill_dir.return_value = 0

                    await cleanup_for_build(str(project))

                    # Should sleep after killing
                    mock_sleep.assert_called_once_with(0.5)


class TestBuildSessionFileLockDetection:
    """Tests for file lock error detection in BuildSession."""

    def test_detects_msb3021_error(self):
        """Test detects MSB3021 error code."""
        from netcoredbg_mcp.build.session import BuildSession

        session = BuildSession(workspace_root=".")

        stdout = "error MSB3021: Unable to copy file"
        assert session._is_file_lock_error(stdout, "")

    def test_detects_msb3026_error(self):
        """Test detects MSB3026 error code."""
        from netcoredbg_mcp.build.session import BuildSession

        session = BuildSession(workspace_root=".")

        stdout = "error MSB3026: Could not copy"
        assert session._is_file_lock_error(stdout, "")

    def test_detects_msb3027_error(self):
        """Test detects MSB3027 error code."""
        from netcoredbg_mcp.build.session import BuildSession

        session = BuildSession(workspace_root=".")

        stdout = "error MSB3027: Cannot delete"
        assert session._is_file_lock_error(stdout, "")

    def test_detects_used_by_another_process(self):
        """Test detects 'being used by another process' message."""
        from netcoredbg_mcp.build.session import BuildSession

        session = BuildSession(workspace_root=".")

        stderr = "file is being used by another process"
        assert session._is_file_lock_error("", stderr)

    def test_detects_cannot_access_file(self):
        """Test detects 'The process cannot access the file' message."""
        from netcoredbg_mcp.build.session import BuildSession

        session = BuildSession(workspace_root=".")

        stdout = "The process cannot access the file because it is locked"
        assert session._is_file_lock_error(stdout, "")

    def test_returns_false_for_normal_errors(self):
        """Test returns False for normal build errors."""
        from netcoredbg_mcp.build.session import BuildSession

        session = BuildSession(workspace_root=".")

        stdout = "error CS0246: The type or namespace name 'Foo' could not be found"
        assert not session._is_file_lock_error(stdout, "")
