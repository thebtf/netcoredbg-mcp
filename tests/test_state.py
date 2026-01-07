"""Tests for debug session state management."""


from netcoredbg_mcp.session.state import (
    Breakpoint,
    BreakpointRegistry,
    DebugState,
    SessionState,
    StackFrame,
    ThreadInfo,
    Variable,
)


class TestDebugState:
    """Tests for DebugState enum."""

    def test_state_values(self):
        """Test state enum values."""
        assert DebugState.IDLE.value == "idle"
        assert DebugState.INITIALIZING.value == "initializing"
        assert DebugState.CONFIGURED.value == "configured"
        assert DebugState.RUNNING.value == "running"
        assert DebugState.STOPPED.value == "stopped"
        assert DebugState.TERMINATED.value == "terminated"

    def test_state_is_string_enum(self):
        """Test state enum inherits from str."""
        assert isinstance(DebugState.IDLE, str)
        assert DebugState.IDLE == "idle"


class TestBreakpoint:
    """Tests for Breakpoint dataclass."""

    def test_create_simple_breakpoint(self):
        """Test creating breakpoint with required fields only."""
        bp = Breakpoint(file="test.cs", line=10)

        assert bp.file == "test.cs"
        assert bp.line == 10
        assert bp.condition is None
        assert bp.hit_condition is None
        assert bp.log_message is None
        assert bp.verified is False
        assert bp.id is None

    def test_create_conditional_breakpoint(self):
        """Test creating breakpoint with condition."""
        bp = Breakpoint(file="test.cs", line=10, condition="x > 5")

        assert bp.condition == "x > 5"

    def test_create_breakpoint_with_hit_condition(self):
        """Test creating breakpoint with hit condition."""
        bp = Breakpoint(file="test.cs", line=10, hit_condition="3")

        assert bp.hit_condition == "3"

    def test_create_logpoint(self):
        """Test creating logpoint (breakpoint with log message)."""
        bp = Breakpoint(file="test.cs", line=10, log_message="Value of x: {x}")

        assert bp.log_message == "Value of x: {x}"

    def test_to_dap_simple(self):
        """Test converting simple breakpoint to DAP format."""
        bp = Breakpoint(file="test.cs", line=10)
        dap = bp.to_dap()

        assert dap == {"line": 10}

    def test_to_dap_with_condition(self):
        """Test converting conditional breakpoint to DAP format."""
        bp = Breakpoint(file="test.cs", line=10, condition="x > 5")
        dap = bp.to_dap()

        assert dap["line"] == 10
        assert dap["condition"] == "x > 5"

    def test_to_dap_with_all_options(self):
        """Test converting breakpoint with all options to DAP format."""
        bp = Breakpoint(
            file="test.cs",
            line=10,
            condition="x > 5",
            hit_condition="3",
            log_message="x = {x}",
        )
        dap = bp.to_dap()

        assert dap["line"] == 10
        assert dap["condition"] == "x > 5"
        assert dap["hitCondition"] == "3"
        assert dap["logMessage"] == "x = {x}"


class TestBreakpointRegistry:
    """Tests for BreakpointRegistry class."""

    def test_add_breakpoint(self):
        """Test adding a breakpoint."""
        registry = BreakpointRegistry()
        bp = Breakpoint(file="test.cs", line=10)

        registry.add(bp)

        breakpoints = registry.get_for_file("test.cs")
        assert len(breakpoints) == 1
        assert breakpoints[0].line == 10

    def test_add_multiple_breakpoints_same_file(self):
        """Test adding multiple breakpoints to same file."""
        registry = BreakpointRegistry()

        registry.add(Breakpoint(file="test.cs", line=10))
        registry.add(Breakpoint(file="test.cs", line=20))
        registry.add(Breakpoint(file="test.cs", line=30))

        breakpoints = registry.get_for_file("test.cs")
        assert len(breakpoints) == 3
        assert [bp.line for bp in breakpoints] == [10, 20, 30]

    def test_add_breakpoints_different_files(self):
        """Test adding breakpoints to different files."""
        registry = BreakpointRegistry()

        registry.add(Breakpoint(file="file1.cs", line=10))
        registry.add(Breakpoint(file="file2.cs", line=20))

        assert len(registry.get_for_file("file1.cs")) == 1
        assert len(registry.get_for_file("file2.cs")) == 1

    def test_add_duplicate_updates_existing(self):
        """Test adding breakpoint at same line updates existing."""
        registry = BreakpointRegistry()

        registry.add(Breakpoint(file="test.cs", line=10))
        registry.add(Breakpoint(file="test.cs", line=10, condition="x > 5"))

        breakpoints = registry.get_for_file("test.cs")
        assert len(breakpoints) == 1
        assert breakpoints[0].condition == "x > 5"

    def test_remove_breakpoint(self):
        """Test removing a breakpoint."""
        registry = BreakpointRegistry()
        registry.add(Breakpoint(file="test.cs", line=10))
        registry.add(Breakpoint(file="test.cs", line=20))

        removed = registry.remove("test.cs", 10)

        assert removed is True
        breakpoints = registry.get_for_file("test.cs")
        assert len(breakpoints) == 1
        assert breakpoints[0].line == 20

    def test_remove_nonexistent_breakpoint(self):
        """Test removing breakpoint that doesn't exist."""
        registry = BreakpointRegistry()
        registry.add(Breakpoint(file="test.cs", line=10))

        removed = registry.remove("test.cs", 99)

        assert removed is False
        assert len(registry.get_for_file("test.cs")) == 1

    def test_remove_from_nonexistent_file(self):
        """Test removing from file with no breakpoints."""
        registry = BreakpointRegistry()

        removed = registry.remove("nonexistent.cs", 10)

        assert removed is False

    def test_clear_file_breakpoints(self):
        """Test clearing breakpoints for a specific file."""
        registry = BreakpointRegistry()
        registry.add(Breakpoint(file="file1.cs", line=10))
        registry.add(Breakpoint(file="file1.cs", line=20))
        registry.add(Breakpoint(file="file2.cs", line=30))

        count = registry.clear("file1.cs")

        assert count == 2
        assert len(registry.get_for_file("file1.cs")) == 0
        assert len(registry.get_for_file("file2.cs")) == 1

    def test_clear_all_breakpoints(self):
        """Test clearing all breakpoints."""
        registry = BreakpointRegistry()
        registry.add(Breakpoint(file="file1.cs", line=10))
        registry.add(Breakpoint(file="file2.cs", line=20))

        count = registry.clear()

        assert count == 2
        assert len(registry.get_for_file("file1.cs")) == 0
        assert len(registry.get_for_file("file2.cs")) == 0

    def test_get_files(self):
        """Test getting list of files with breakpoints."""
        registry = BreakpointRegistry()
        registry.add(Breakpoint(file="file1.cs", line=10))
        registry.add(Breakpoint(file="file2.cs", line=20))

        files = registry.get_files()

        assert len(files) == 2

    def test_get_all(self):
        """Test getting all breakpoints."""
        registry = BreakpointRegistry()
        registry.add(Breakpoint(file="file1.cs", line=10))
        registry.add(Breakpoint(file="file2.cs", line=20))

        all_bps = registry.get_all()

        assert len(all_bps) == 2

    def test_update_from_dap(self):
        """Test updating breakpoints from DAP response."""
        registry = BreakpointRegistry()
        registry.add(Breakpoint(file="test.cs", line=10))
        registry.add(Breakpoint(file="test.cs", line=20))

        dap_response = [
            {"id": 1, "verified": True, "line": 10},
            {"id": 2, "verified": True, "line": 21},  # Line adjusted
        ]

        registry.update_from_dap("test.cs", dap_response)

        breakpoints = registry.get_for_file("test.cs")
        assert breakpoints[0].verified is True
        assert breakpoints[0].id == 1
        assert breakpoints[1].verified is True
        assert breakpoints[1].line == 21  # Updated from DAP

    def test_path_normalization(self):
        """Test that paths are normalized for lookup."""
        registry = BreakpointRegistry()

        # Add with forward slashes
        registry.add(Breakpoint(file="C:/test/file.cs", line=10))

        # Should find with backslashes (on Windows)
        import os
        if os.name == "nt":
            breakpoints = registry.get_for_file("C:\\test\\file.cs")
            assert len(breakpoints) == 1


class TestDataClasses:
    """Tests for other dataclasses."""

    def test_thread_info(self):
        """Test ThreadInfo dataclass."""
        thread = ThreadInfo(id=1, name="Main Thread")

        assert thread.id == 1
        assert thread.name == "Main Thread"

    def test_stack_frame(self):
        """Test StackFrame dataclass."""
        frame = StackFrame(
            id=0,
            name="Program.Main()",
            source="C:/test/Program.cs",
            line=10,
            column=5,
        )

        assert frame.id == 0
        assert frame.name == "Program.Main()"
        assert frame.source == "C:/test/Program.cs"
        assert frame.line == 10
        assert frame.column == 5

    def test_stack_frame_no_source(self):
        """Test StackFrame without source."""
        frame = StackFrame(
            id=1,
            name="System.Runtime",
            source=None,
            line=0,
            column=0,
        )

        assert frame.source is None

    def test_variable(self):
        """Test Variable dataclass."""
        var = Variable(
            name="x",
            value="10",
            type="int",
            variables_reference=0,
        )

        assert var.name == "x"
        assert var.value == "10"
        assert var.type == "int"
        assert var.variables_reference == 0
        assert var.named_variables == 0
        assert var.indexed_variables == 0

    def test_variable_with_children(self):
        """Test Variable with child references."""
        var = Variable(
            name="list",
            value="{List<int>}",
            type="List<int>",
            variables_reference=5,
            named_variables=2,
            indexed_variables=10,
        )

        assert var.variables_reference == 5
        assert var.named_variables == 2
        assert var.indexed_variables == 10


class TestSessionState:
    """Tests for SessionState dataclass."""

    def test_default_state(self):
        """Test default session state."""
        state = SessionState()

        assert state.state == DebugState.IDLE
        assert state.current_thread_id is None
        assert state.stop_reason is None
        assert state.threads == []
        assert state.current_frame_id is None
        assert state.output_buffer == []
        assert state.exit_code is None
        assert state.exception_info is None

    def test_to_dict(self):
        """Test converting session state to dict."""
        state = SessionState(
            state=DebugState.STOPPED,
            current_thread_id=1,
            stop_reason="breakpoint",
            threads=[ThreadInfo(id=1, name="Main")],
            current_frame_id=0,
            exit_code=None,
        )

        d = state.to_dict()

        assert d["state"] == "stopped"
        assert d["currentThreadId"] == 1
        assert d["stopReason"] == "breakpoint"
        assert len(d["threads"]) == 1
        assert d["threads"][0]["id"] == 1
        assert d["currentFrameId"] == 0
        assert d["exitCode"] is None
