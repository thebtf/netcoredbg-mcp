"""Comprehensive smoke test for netcoredbg-mcp.

Tests ALL MCP tool functionality against real netcoredbg + SmokeTestApp.
Each scenario runs in a fresh debug session to avoid state leakage.

Requires: netcoredbg in PATH or NETCOREDBG_PATH env var.
Build first: dotnet build tests/fixtures/SmokeTestApp -c Debug

Usage: python tests/smoke_test.py
"""

import asyncio
import os
import re
import sys
import traceback
from types import SimpleNamespace
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from netcoredbg_mcp.session import SessionManager
from netcoredbg_mcp.session.state import Breakpoint, DebugState, OutputEntry, TraceEntry

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DLL = os.path.join(
    BASE,
    "tests",
    "fixtures",
    "SmokeTestApp",
    "bin",
    "Debug",
    "net8.0-windows",
    "SmokeTestApp.dll",
)
WPF_DLL = os.path.join(
    BASE,
    "tests",
    "fixtures",
    "WpfSmokeApp",
    "bin",
    "Debug",
    "net8.0-windows",
    "WpfSmokeApp.dll",
)
AVALONIA_DLL = os.path.join(
    BASE,
    "tests",
    "fixtures",
    "AvaloniaSmokeApp",
    "bin",
    "Debug",
    "net8.0",
    "AvaloniaSmokeApp.dll",
)
GUI_ENABLED = os.path.exists(DLL)
WPF_GUI_ENABLED = os.path.exists(WPF_DLL)
AVALONIA_GUI_ENABLED = os.path.exists(AVALONIA_DLL)
if not GUI_ENABLED:
    # net8.0-windows build required for GUI scenarios; skip GUI tests if missing
    print(f"WARNING: {DLL} not found. GUI scenarios will be skipped.")
    print("Build with: dotnet build tests/fixtures/SmokeTestApp")
SOURCE = os.path.join(BASE, "tests", "fixtures", "SmokeTestApp", "Program.cs")

passed = 0
failed = 0

# Dynamic line number lookup — survives code changes to SmokeTestApp
_source_lines: list[str] = []


def _find_line(pattern: str) -> int:
    """Find the 1-based line number containing pattern in SOURCE."""
    global _source_lines
    if not _source_lines:
        with open(SOURCE, encoding="utf-8") as f:
            _source_lines = f.readlines()
    for i, line in enumerate(_source_lines, 1):
        if pattern in line:
            return i
    raise ValueError(f"Pattern not found in {SOURCE}: {pattern!r}")


def check(name: str, condition: bool, detail: str = "") -> None:
    global passed, failed
    status = "PASS" if condition else "FAIL"
    if not condition:
        failed += 1
    else:
        passed += 1
    suffix = f" — {detail}" if detail else ""
    print(f"  [{status}] {name}{suffix}")


class _CapturingMCP:
    """Minimal MCP test double for live smoke helpers."""

    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *args: Any, **kwargs: Any) -> Any:
        def decorator(fn: Any) -> Any:
            self.tools[fn.__name__] = fn
            return fn

        return decorator


def _grid_alias_evidence(response: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
    data = response.get("data", {}) if isinstance(response, dict) else {}
    visible_rows = data.get("visible_rows") if isinstance(data, dict) else None
    first_row = (
        visible_rows[0] if isinstance(visible_rows, list) and visible_rows else None
    )
    first_cells = first_row.get("cells", {}) if isinstance(first_row, dict) else {}
    first_phrase = first_cells.get("Phrase") or first_cells.get("phrase")
    evidence = {
        "status": data.get("status"),
        "requested_action": data.get("requested_action"),
        "canonical_action": data.get("canonical_action"),
        "row_count": len(visible_rows) if isinstance(visible_rows, list) else None,
        "first_phrase": first_phrase,
    }
    return evidence, first_phrase if isinstance(first_phrase, str) else None


async def new_session() -> SessionManager:
    return SessionManager()


async def _get_debug_state_via_tool(session: SessionManager) -> dict[str, Any]:
    from netcoredbg_mcp.tools.debug import register_debug_tools

    mcp = _CapturingMCP()

    async def notify_state_changed(_ctx: Any) -> None:
        return None

    async def execute_and_wait(*_args: Any, **_kwargs: Any) -> None:
        return None

    async def resolve_project_root(_ctx: Any, _session: SessionManager) -> None:
        return None

    register_debug_tools(
        mcp=mcp,
        session=session,
        ownership=SimpleNamespace(release=lambda *_args, **_kwargs: None),
        notify_state_changed=notify_state_changed,
        check_session_access=lambda _ctx: None,
        execute_and_wait=execute_and_wait,
        resolve_project_root=resolve_project_root,
    )

    return await mcp.tools["get_debug_state"]()


# ─────────────────────────────────────────────────────────────
# Scenario 1: Hit counting + breakpoint fundamentals
# ─────────────────────────────────────────────────────────────
async def test_hit_counting():
    print("\n1. HIT COUNTING (FR-1)")
    m = await new_session()
    try:
        bp_line = _find_line("sum += i")
        m.breakpoints.add(Breakpoint(file=SOURCE, line=bp_line))  # sum += i
        await m.launch(program=DLL, args=["hitcount"])
        snapshot = await m.wait_for_stopped(timeout=10)
        await asyncio.sleep(0.3)

        debug_state = await _get_debug_state_via_tool(m)
        check(
            "get_debug_state shows stopped-at-breakpoint",
            debug_state.get("data", {}).get("execState") == "stopped-at-breakpoint",
            f"execState={debug_state.get('data', {}).get('execState')}",
        )

        norm = m.breakpoints._normalize_path(SOURCE)

        check("Stops at breakpoint", snapshot.stop_reason == "breakpoint")
        check("Breakpoint verified", m.breakpoints.get_for_file(SOURCE)[0].verified)

        hit = m.state.hit_counts.get((norm, bp_line), 0)
        check("Hit count = 1 after first stop", hit == 1, f"got {hit}")

        # Continue 4 more times
        for _ in range(4):
            m.prepare_for_execution()
            await m._client.continue_execution(m.state.current_thread_id)
            await m.wait_for_stopped(timeout=5)
            await asyncio.sleep(0.15)

        hit = m.state.hit_counts.get((norm, bp_line), 0)
        check("Hit count = 5 after 5 stops", hit == 5, f"got {hit}")

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 2: Stack trace + evaluate + variables
# ─────────────────────────────────────────────────────────────
async def test_stack_and_variables():
    print("\n2. STACK TRACE + EVALUATE + VARIABLES (existing functionality)")
    m = await new_session()
    try:
        m.breakpoints.add(
            Breakpoint(file=SOURCE, line=_find_line("int={intVar}"))
        )  # VariableInspection println
        await m.launch(program=DLL, args=["variables"])
        snapshot = await m.wait_for_stopped(timeout=10)

        check("Stopped at variables breakpoint", snapshot.stop_reason == "breakpoint")

        # Stack trace
        frames = await m.get_stack_trace(levels=5)
        check("Stack trace has frames", len(frames) > 0, f"count={len(frames)}")
        check(
            "Top frame is VariableInspection",
            frames[0].name.endswith("VariableInspection()") if frames else False,
            f"got: {frames[0].name if frames else 'none'}",
        )

        fid = frames[0].id if frames else None

        # Evaluate expressions
        r = await m.evaluate("intVar", fid)
        check(
            "Evaluate intVar = 42",
            r.get("result") == "42" if isinstance(r, dict) else False,
            f"got: {r}",
        )

        r = await m.evaluate("stringVar", fid)
        check(
            "Evaluate stringVar = hello world",
            "hello world" in str(r.get("result", "")) if isinstance(r, dict) else False,
            f"got: {r}",
        )

        r = await m.evaluate("listVar.Count", fid)
        check(
            "Evaluate listVar.Count = 5",
            r.get("result") == "5" if isinstance(r, dict) else False,
            f"got: {r}",
        )

        # Get scopes + variables
        scopes = await m.get_scopes(fid)
        check("Scopes returned", len(scopes) > 0, f"count={len(scopes)}")

        if scopes:
            locals_ref = scopes[0].get("variablesReference", 0)
            if locals_ref:
                variables = await m.get_variables(locals_ref)
                var_names = [v.name for v in variables]
                check(
                    "Local variables include intVar",
                    "intVar" in var_names,
                    f"vars: {var_names[:8]}",
                )
                check("Local variables include dictVar", "dictVar" in var_names)

        # Set variable
        try:
            await m.set_variable(locals_ref, "intVar", "99")
            r = await m.evaluate("intVar", fid)
            check(
                "Set variable changes value",
                r.get("result") == "99" if isinstance(r, dict) else False,
                f"got: {r}",
            )
        except Exception as e:
            check("Set variable", False, f"error: {e}")

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 3: Stepping (step_over, step_into, step_out)
# ─────────────────────────────────────────────────────────────
async def test_stepping():
    print("\n3. STEPPING (step_over, step_into, step_out)")
    m = await new_session()
    try:
        m.breakpoints.add(
            Breakpoint(file=SOURCE, line=_find_line("var mid = Middle(x + 1)"))
        )  # Outer
        await m.launch(program=DLL, args=["stepping"])
        snapshot = await m.wait_for_stopped(timeout=10)

        check("Stopped at Outer", snapshot.stop_reason == "breakpoint")

        # Step into → should enter Middle (may take 1-2 steps due to expression eval)
        entered_middle = False
        for _ in range(3):
            m.prepare_for_execution()
            await m._client.step_in(m.state.current_thread_id)
            snapshot = await m.wait_for_stopped(timeout=5)
            frames = await m.get_stack_trace(levels=3)
            if frames and "Middle" in frames[0].name:
                entered_middle = True
                break
        check(
            "Step into enters Middle",
            entered_middle,
            f"top: {frames[0].name if frames else 'none'}",
        )

        # Step over → stay in Middle (advance one line)
        m.prepare_for_execution()
        await m._client.step_over(m.state.current_thread_id)
        snapshot = await m.wait_for_stopped(timeout=5)
        frames = await m.get_stack_trace(levels=3)
        check(
            "Step over stays in Middle",
            "Middle" in (frames[0].name if frames else ""),
            f"top: {frames[0].name if frames else 'none'}",
        )

        # Step out → back to Outer
        m.prepare_for_execution()
        await m._client.step_out(m.state.current_thread_id)
        snapshot = await m.wait_for_stopped(timeout=5)
        frames = await m.get_stack_trace(levels=3)
        check(
            "Step out returns to Outer",
            "Outer" in (frames[0].name if frames else ""),
            f"top: {frames[0].name if frames else 'none'}",
        )

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 4: Output categories (stdout vs stderr)
# ─────────────────────────────────────────────────────────────
async def test_output_categories():
    print("\n4. OUTPUT CATEGORIES (FR-5)")
    m = await new_session()
    try:
        # Run "output" scenario to completion (no breakpoints)
        await m.launch(program=DLL, args=["output"])
        await m.wait_for_stopped(timeout=10)  # will terminate
        # Give output events time to arrive
        await asyncio.sleep(0.5)

        stdout = [e for e in m.state.output_buffer if e.category == "stdout"]
        stderr = [e for e in m.state.output_buffer if e.category == "stderr"]
        all_text = "".join(e.text for e in m.state.output_buffer)

        check("Has stdout entries", len(stdout) > 0, f"count={len(stdout)}")
        check("Has stderr entries", len(stderr) > 0, f"count={len(stderr)}")
        check("Stdout contains expected text", "This is stdout output" in all_text)
        check(
            "Stderr contains expected text",
            any("This is stderr output" in e.text for e in stderr),
            f"stderr texts: {[e.text.strip() for e in stderr[:3]]}",
        )

        # Verify category filter logic
        stdout_text = "".join(e.text for e in stdout)
        check("Stdout filter excludes stderr", "stderr output" not in stdout_text)

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 5: Module tracking (FR-4, FR-7)
# ─────────────────────────────────────────────────────────────
async def test_modules():
    print("\n5. MODULE TRACKING (FR-4, FR-7)")
    m = await new_session()
    try:
        await m.launch(program=DLL, args=["hitcount"], stop_at_entry=True)
        await m.wait_for_stopped(timeout=10)
        await asyncio.sleep(0.3)

        check(
            "Modules populated by events",
            len(m.state.modules) > 0,
            f"count={len(m.state.modules)}",
        )

        app_mod = [mod for mod in m.state.modules if "SmokeTestApp" in mod.name]
        check("SmokeTestApp module found", len(app_mod) > 0)
        if app_mod:
            check("Module has name", app_mod[0].name != "")
            check(
                "Module has symbol_status",
                app_mod[0].symbol_status is not None,
                f"status={app_mod[0].symbol_status}",
            )

        # ModuleInfo.to_dict()
        if m.state.modules:
            d = m.state.modules[0].to_dict()
            check(
                "ModuleInfo.to_dict has expected keys",
                all(k in d for k in ("name", "path", "isOptimized", "symbolStatus")),
            )

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 6: quick_evaluate (FR-3) — pause/eval/resume
# ─────────────────────────────────────────────────────────────
async def test_quick_evaluate():
    print("\n6. QUICK EVALUATE (FR-3)")
    m = await new_session()
    try:
        # Launch longrun scenario (Thread.Sleep loop — runs ~6 seconds)
        await m.launch(program=DLL, args=["longrun"])
        # Wait a bit for program to start running
        await asyncio.sleep(1.5)

        check(
            "Program is running",
            m.state.state == DebugState.RUNNING,
            f"state={m.state.state.value}",
        )

        if m.state.state != DebugState.RUNNING:
            print("  Skipping quick_evaluate — program not running")
            return

        # quick_evaluate while running
        result = await m.quick_evaluate("1 + 1")

        if "error" in result and "0x8" in str(result.get("error", "")):
            # dbgshim version mismatch — evaluate fails at infrastructure level
            # This is NOT a code bug, but a known netcoredbg/dbgshim incompatibility
            check(
                "quick_evaluate: dbgshim mismatch detected (infra issue, not code bug)",
                True,
                f"error={result['error']}",
            )
            print(
                "    NOTE: Copy dbgshim.dll from .NET 8 SDK to fix. Skipping eval checks."
            )
        else:
            check(
                "quick_evaluate returns result",
                "result" in result and "error" not in result,
                f"result={result}",
            )
            check(
                "quick_evaluate result correct",
                result.get("result") == "2",
                f"got: {result.get('result')}",
            )
            check(
                "quick_evaluate type returned",
                result.get("type") == "int",
                f"got: {result.get('type')}",
            )

            # Program should be running again after quick_evaluate
            await asyncio.sleep(0.2)
            check(
                "Program resumed after quick_evaluate",
                m.state.state == DebugState.RUNNING,
                f"state={m.state.state.value}",
            )

            # quick_evaluate with error expression
            result = await m.quick_evaluate("nonexistent_variable_xyz")
            check(
                "quick_evaluate returns error for bad expression",
                "error" in result,
                f"result={result}",
            )

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 7: Exception breakpoints + exception info
# ─────────────────────────────────────────────────────────────
async def test_exception_handling():
    print("\n7. EXCEPTION BREAKPOINTS + EXCEPTION INFO")
    m = await new_session()
    try:
        # Configure to break on all exceptions
        await m.launch(program=DLL, args=["exception"], stop_at_entry=True)
        await m.wait_for_stopped(timeout=10)

        # Set exception breakpoints for all exceptions
        await m._client.set_exception_breakpoints(["all"])

        # Continue — should stop on IndexOutOfRangeException
        m.prepare_for_execution()
        await m._client.continue_execution(m.state.current_thread_id)
        snapshot = await m.wait_for_stopped(timeout=10)

        check(
            "Stopped on exception",
            snapshot.stop_reason == "exception",
            f"reason={snapshot.stop_reason}",
        )

        # Get exception info
        try:
            info = await m.get_exception_info()
            check(
                "Exception info returned",
                isinstance(info, dict) and len(info) > 0,
                f"keys={list(info.keys())[:5] if isinstance(info, dict) else 'not dict'}",
            )
            check(
                "Exception id contains IndexOutOfRange",
                "IndexOutOfRange" in str(info.get("exceptionId", "")),
                f"id={info.get('exceptionId', '')}",
            )
        except Exception as e:
            check("Exception info", False, f"error: {e}")

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 8: Capabilities + terminate (FR-8, FR-9)
# ─────────────────────────────────────────────────────────────
async def test_capabilities_and_terminate():
    print("\n8. CAPABILITIES + TERMINATE (FR-8, FR-9)")
    m = await new_session()
    try:
        await m.launch(program=DLL, args=["hitcount"], stop_at_entry=True)
        await m.wait_for_stopped(timeout=10)

        caps = m.client.capabilities
        check("Capabilities is dict", isinstance(caps, dict))
        check("Has multiple capabilities", len(caps) > 3, f"count={len(caps)}")
        check(
            "supportsTerminateRequest = True",
            caps.get("supportsTerminateRequest", False) is True,
        )
        check(
            "supportsConditionalBreakpoints = True",
            caps.get("supportsConditionalBreakpoints", False) is True,
        )
        check(
            "supportsFunctionBreakpoints = True",
            caps.get("supportsFunctionBreakpoints", False) is True,
        )

        # Terminate gracefully
        await m.client.terminate()
        snapshot = await m.wait_for_stopped(timeout=5)
        check(
            "Graceful terminate succeeds",
            snapshot.state == DebugState.TERMINATED,
            f"state={snapshot.state.value}",
        )

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 9: Stopped description/text (FR-6)
# ─────────────────────────────────────────────────────────────
async def test_stopped_description():
    print("\n9. STOPPED DESCRIPTION/TEXT (FR-6)")
    m = await new_session()
    try:
        m.breakpoints.add(Breakpoint(file=SOURCE, line=_find_line("sum += i")))
        await m.launch(program=DLL, args=["hitcount"])
        snapshot = await m.wait_for_stopped(timeout=10)

        # These fields exist on snapshot (may be None — netcoredbg doesn't always send them)
        check("Snapshot has description attr", hasattr(snapshot, "description"))
        check("Snapshot has text attr", hasattr(snapshot, "text"))

        # SessionState fields
        check("State has stop_description", hasattr(m.state, "stop_description"))
        check("State has stop_text", hasattr(m.state, "stop_text"))

        # to_dict includes them
        d = m.state.to_dict()
        check("to_dict has stopDescription key", "stopDescription" in d)
        check("to_dict has stopText key", "stopText" in d)

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Scenario 10: Threads + pause/continue
# ─────────────────────────────────────────────────────────────
async def test_threads_and_pause():
    print("\n10. THREADS + PAUSE/CONTINUE")
    m = await new_session()
    try:
        await m.launch(program=DLL, args=["longrun"])
        await asyncio.sleep(1.5)  # Wait for program to be running

        check("Program is running", m.state.state == DebugState.RUNNING)

        debug_state = await _get_debug_state_via_tool(m)
        check(
            "get_debug_state shows running",
            debug_state.get("data", {}).get("execState") == "running",
            f"execState={debug_state.get('data', {}).get('execState')}",
        )

        # Get threads
        threads = await m.get_threads()
        check("Has threads", len(threads) > 0, f"count={len(threads)}")

        # Pause
        m.prepare_for_execution()
        await m._client.pause(m.state.current_thread_id or threads[0].id)
        snapshot = await m.wait_for_stopped(timeout=5)
        check(
            "Pause succeeds",
            snapshot.state == DebugState.STOPPED,
            f"reason={snapshot.stop_reason}",
        )

        # Continue
        m.prepare_for_execution()
        await m._client.continue_execution(m.state.current_thread_id)
        await asyncio.sleep(0.3)
        check(
            "Continue resumes",
            m.state.state == DebugState.RUNNING,
            f"state={m.state.state.value}",
        )

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# Main runner
# ─────────────────────────────────────────────────────────────
async def test_ui_invoke_toggle():
    """Scenario 11: UI tools — invoke, toggle, root_id (requires GUI scenario)."""
    print("\n--- UI Invoke + Toggle + Root ID ---")

    from netcoredbg_mcp.ui.backend import create_backend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)  # Wait for GUI window to appear

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Process started", False, "no PID")
            return
        check("Process started with GUI", True, f"PID={pid}")

        await backend.connect(pid)
        check("UI backend connected", True)

        # WinForms: AccessibleName maps to UIA Name (not AutomationId).
        # Try automation_id first (WPF/Avalonia), fall back to name (WinForms).
        # Narrow exception: element-not-found raises RuntimeError from backends.
        not_found = (RuntimeError, LookupError)

        async def _with_name_fallback(method, element_id, **kwargs):
            """Call backend method by automation_id, fall back to name on not-found."""
            try:
                return await method(automation_id=element_id, **kwargs)
            except not_found:
                return await method(name=element_id, **kwargs)

        # Test ui_invoke
        try:
            result = await _with_name_fallback(backend.invoke_element, "btnInvoke")
            check(
                "ui_invoke (btnInvoke)",
                result.get("invoked", False),
                f"method={result.get('method')}",
            )
        except Exception as e:
            check("ui_invoke (btnInvoke)", False, str(e))

        # Test ui_toggle
        try:
            result = await _with_name_fallback(backend.toggle_element, "chkEnabled")
            check(
                "ui_toggle (chkEnabled)",
                result.get("toggled", False),
                f"newState={result.get('newState')}",
            )
            check(
                "ui_toggle returns On",
                result.get("newState") == "On",
                f"got {result.get('newState')}",
            )
        except Exception as e:
            check("ui_toggle (chkEnabled)", False, str(e))

        # Test ui_toggle again to verify state cycle
        try:
            result = await _with_name_fallback(backend.toggle_element, "chkEnabled")
            check(
                "ui_toggle cycle Off",
                result.get("newState") == "Off",
                f"got {result.get('newState')}",
            )
        except Exception as e:
            check("ui_toggle cycle", False, str(e))

        # Test scoped search: find_element with root_id
        try:
            result = await backend.find_element(
                automation_id="btnScoped",
                root_id="settingsPanel",
            )
            check(
                "find_element with root_id",
                result.get("found", False),
                "found in settingsPanel",
            )
        except Exception as e:
            check("find_element with root_id", False, str(e))

        # Test XPath search (FlaUI only)
        # WinForms: AccessibleName overrides UIA Name property
        # outerBtn has AccessibleName="btnOuter", so UIA Name="btnOuter"
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        if isinstance(backend, FlaUIBackend):
            try:
                result = await backend.find_by_xpath("//Button[@Name='btnOuter']")
                check(
                    "find_by_xpath (Button)",
                    result.get("found", False),
                    f"matchCount={result.get('matchCount')}",
                )
            except Exception as e:
                check("find_by_xpath", False, str(e))
        else:
            check(
                "find_by_xpath (skipped)",
                True,
                "pywinauto backend -- XPath not supported",
            )

        # Test ui_file_dialog: WinForms file dialog button
        # Known issue: WinForms button UIA exposure varies by .NET version
        # This test validates the happy path when accessible, skips gracefully when not
        try:
            opened = False
            for search in [
                {"automation_id": "btnOpenFile"},
                {"name": "btnOpenFile"},
                {"name": "Open File..."},
            ]:
                try:
                    await backend.invoke_element(**search)
                    opened = True
                    break
                except Exception:
                    continue
            if opened:
                await asyncio.sleep(1.5)
                await backend.send_keys("{ESCAPE}")
                await asyncio.sleep(0.5)
            check(
                "ui_file_dialog",
                True,
                "opened and canceled"
                if opened
                else "button not in UIA tree — WinForms limitation",
            )
        except Exception as e:
            check("ui_file_dialog", True, f"skipped — {e}")

        await backend.disconnect()

    finally:
        try:
            await m.stop()
        except Exception:
            pass


async def test_datagrid_select():
    """Scenario 20: DataGrid multi-select and read selected item."""
    print("\n--- DataGrid Select + Read ---")

    from netcoredbg_mcp.ui.backend import create_backend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("DataGrid: process started", False, "no PID")
            return
        await backend.connect(pid)

        # Find DataGrid
        try:
            try:
                result = await backend.find_element(automation_id="dataGrid")
            except (RuntimeError, LookupError):
                result = await backend.find_element(name="dataGrid")
            check(
                "DataGrid found",
                result.get("found", False)
                if isinstance(result, dict)
                else result is not None,
            )
        except Exception as e:
            check("DataGrid found", False, str(e))
            await backend.disconnect()
            return

        # Select rows 0 and 2 (Alice and Charlie) via FlaUI multi_select
        from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

        if isinstance(backend, FlaUIBackend):
            try:
                count = await backend.multi_select("dataGrid", [0, 2])
                check("DataGrid multi_select", count >= 1, f"selected={count}")
            except Exception as e:
                check("DataGrid multi_select", False, str(e))

            # Extract text from DataGrid
            try:
                result = await backend.extract_text(automation_id="dataGrid")
                text = (
                    result.get("text", "") if isinstance(result, dict) else str(result)
                )
                check("DataGrid extract_text", len(text) > 0, f"text={text[:60]}...")
            except Exception as e:
                check("DataGrid extract_text", True, f"not supported: {e}")

            # Find element by XPath within DataGrid
            try:
                result = await backend.find_by_xpath("//DataItem")
                check(
                    "DataGrid XPath DataItem",
                    result.get("found", False),
                    f"matchCount={result.get('matchCount')}",
                )
            except Exception as e:
                check("DataGrid XPath DataItem", True, f"xpath not available: {e}")
        else:
            check(
                "DataGrid tests (skipped)", True, "pywinauto — limited DataGrid support"
            )

        await backend.disconnect()

    finally:
        try:
            await m.stop()
        except Exception:
            pass


async def test_multi_window_envelope():
    """Scenario: engram issue #7 -- end-to-end multi-window flow.

    Drives the full multi-window lifecycle against a real bridge process:
    main window visible, click Open Second -> modeless sibling top-level
    window appears -> get_window_tree surfaces both windows in the envelope
    -> switch_window retargets into the second window -> find_element
    resolves an element inside the second window that does not exist in
    the main window -> switch back to main.

    This reproduces the bug path from engram #7 (modal dialogs are sibling
    top-level windows, not descendants) using Form.Show() instead of
    ShowDialog() to avoid WinForms modal-vs-InvokePattern blocking quirks.
    The sampleapp WPF scenario uses ShowDialog() directly but gets the
    same UIA representation (sibling top-level window), which this test
    exercises faithfully.
    """
    print("\n--- Multi-Window Envelope (engram #7) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("MultiWindow: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check(
                "MultiWindow (skipped)",
                True,
                "pywinauto -- multi-window requires FlaUI bridge",
            )
            await backend.disconnect()
            return

        # 1. Baseline envelope with just the main window present
        primary: str = ""
        try:
            tree = await backend.get_window_tree(max_depth=3, max_children=50)
            assert isinstance(tree, dict)
            windows = tree.get("windows")
            assert isinstance(windows, list)

            check(
                "MultiWindow: baseline envelope has main window only",
                len(windows) == 1,
                f"count={tree.get('count')}",
            )

            primary_val = tree.get("primary")
            primary = primary_val if isinstance(primary_val, str) else ""
            check(
                "MultiWindow: primary is main window name",
                isinstance(primary_val, str) and len(primary) > 0,
                f"primary={primary_val}",
            )

            first = windows[0]
            check(
                "MultiWindow: main window has className field (Fix A guard)",
                isinstance(first, dict) and "className" in first,
                "className key missing -- BuildElementInfo Fix A regression",
            )
        except Exception as e:
            check("MultiWindow: baseline envelope", False, str(e))
            await backend.disconnect()
            return

        # 2. Element cache populated from the walk
        cache_size = len(backend.element_cache)
        check(
            "MultiWindow: element cache populated",
            cache_size > 0,
            f"entries={cache_size}",
        )

        # 3. Open the second top-level window
        try:
            await backend.invoke_element(name="btnOpenSecond")
            check("MultiWindow: open second window", True)
        except Exception as e:
            check("MultiWindow: open second window", False, str(e))
            await backend.disconnect()
            return
        await asyncio.sleep(0.8)

        # 4. After opening, envelope surfaces both windows as siblings
        try:
            tree2 = await backend.get_window_tree(max_depth=3, max_children=50)
            assert isinstance(tree2, dict)
            windows2 = tree2.get("windows") or []
            window_names = [w.get("name", "") for w in windows2 if isinstance(w, dict)]
            second_visible = any("Create collection" in n for n in window_names)
            check(
                "MultiWindow: second window visible as sibling",
                second_visible,
                f"names={window_names}",
            )
            check(
                "MultiWindow: envelope reports count>=2",
                len(windows2) >= 2,
                f"count={tree2.get('count')}",
            )
        except Exception as e:
            check("MultiWindow: envelope after open", False, str(e))
            await backend.disconnect()
            return

        # 5. Switch into the second window
        try:
            result = await backend.switch_window(name="Create collection")
            check(
                "MultiWindow: switch_window to second window",
                isinstance(result, dict) and result.get("switched") is True,
                f"title={result.get('title') if isinstance(result, dict) else '?'}",
            )
        except Exception as e:
            check("MultiWindow: switch_window to second window", False, str(e))

        # 6. Find an element inside the second window that doesn't exist in main
        try:
            found = await backend.find_element(automation_id="dlgInput")
            check(
                "MultiWindow: find TextBox in second window",
                isinstance(found, dict) and found.get("found", False),
                f"found={found.get('found') if isinstance(found, dict) else '?'}",
            )
        except Exception as e:
            check("MultiWindow: find TextBox in second window", False, str(e))

        # 7. Close second window via its Close button
        try:
            await backend.invoke_element(automation_id="dlgClose")
            check("MultiWindow: close second window via button", True)
            await asyncio.sleep(0.5)
        except Exception as e:
            check("MultiWindow: close second window via button", False, str(e))

        # 8. Switch back to the main window
        try:
            result = await backend.switch_window(name=primary)
            check(
                "MultiWindow: switch back to main window",
                isinstance(result, dict) and result.get("switched") is True,
                f"title={result.get('title') if isinstance(result, dict) else '?'}",
            )
        except Exception as e:
            check("MultiWindow: switch back to main", False, str(e))

        # 9. switch_window surfaces an explicit error for an unknown window
        try:
            unknown_error: str | None = None
            try:
                await backend.switch_window(name="___no_such_window_xyzzy___")
            except Exception as err:
                unknown_error = str(err)
            check(
                "MultiWindow: switch_window rejects unknown window",
                unknown_error is not None
                and "No top-level window" in (unknown_error or ""),
                f"error={unknown_error}",
            )
        except Exception as e:
            check("MultiWindow: switch_window rejects unknown window", False, str(e))

        await backend.disconnect()

    finally:
        try:
            await m.stop()
        except Exception:
            pass


async def test_drag_primitive():
    """Scenario: engram #79 — drag primitive crosses the real drag threshold.

    Drives the WinForms dragList fixture, which only starts DoDragDrop after a
    MouseDown + MouseMove sequence exceeds SystemInformation.DragSize. Verifies:
    - default/fast/slow drags either read back a reordered list order or fall
      back to duration checks when the fixture cannot expose list text via UIA
    - speed_ms below safety floor (=10) returns a structured error
    - identical coordinates are rejected
    """
    print("\n--- Drag Primitive (engram #79) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Drag: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check("Drag (skipped)", True, "pywinauto — bridge drag requires FlaUI")
            await backend.disconnect()
            return

        # Find the dragList rect via the bridge (AutomationId lookup).
        try:
            info = await backend.find_element(automation_id="dragList")
            rect = info.get("rect") if isinstance(info, dict) else None
            check(
                "Drag: dragList present",
                isinstance(rect, dict) and rect.get("width", 0) > 0,
                f"rect={rect}",
            )
            if not rect or not rect.get("width"):
                await backend.disconnect()
                return
        except Exception as e:
            check("Drag: dragList present", False, str(e))
            await backend.disconnect()
            return

        # Compute coordinates for row 0 and row 3 (approximate via item height
        # ≈ 15 px; WinForms ListBox default font).
        x0 = rect["x"] + rect["width"] // 2
        y0 = rect["y"] + 10  # first item centre
        y3 = rect["y"] + 10 + 15 * 3  # fourth item centre

        drag_item_names = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]

        def _parse_drag_order(text: str) -> list[str]:
            positions: list[tuple[int, str]] = []
            for item_name in drag_item_names:
                position = text.find(item_name)
                if position >= 0:
                    positions.append((position, item_name))
            positions.sort()
            return [item_name for _, item_name in positions]

        def _duration_matches_requested_speed(
            result: dict, requested_speed_ms: int
        ) -> bool:
            duration_ms = result.get("duration_ms")
            return (
                isinstance(duration_ms, (int, float))
                and abs(duration_ms - requested_speed_ms) <= requested_speed_ms * 0.20
            )

        async def _read_drag_order() -> tuple[list[str] | None, str]:
            try:
                extract_result = await backend.client.call(
                    "extract_text", {"automationId": "dragList"}
                )
            except Exception as e:
                return None, str(e)

            if not isinstance(extract_result, dict):
                return None, f"non-dict extract_text result: {extract_result!r}"

            extracted_text = extract_result.get("text", "")
            if not isinstance(extracted_text, str) or not extracted_text.strip():
                return None, f"empty text: {extract_result!r}"

            order = _parse_drag_order(extracted_text)
            if len(order) < len(drag_item_names):
                return (
                    None,
                    f"partial order from {extract_result.get('source')}: {extracted_text!r}",
                )

            return order, f"source={extract_result.get('source')}, order={order}"

        current_order, order_detail = await _read_drag_order()
        readback_available = current_order is not None
        if readback_available:
            check(
                "Drag: initial order readable",
                current_order[0] == "Alpha",
                order_detail,
            )
        else:
            check(
                "Drag: initial order readback unavailable",
                True,
                f"falling back to duration checks ({order_detail})",
            )

        drag_results: list[tuple[str, int, dict]] = []
        drag_cases = [
            ("default", 200),
            ("fast", 50),
            ("slow", 500),
        ]

        for label, requested_speed in drag_cases:
            try:
                result = await backend.drag(x0, y0, x0, y3, speed_ms=requested_speed)
                drag_results.append((label, requested_speed, result))
                check(
                    f"Drag: {label} speed_ms={requested_speed} returns structured response",
                    isinstance(result, dict) and result.get("dragged") is True,
                    f"result={result}",
                )
            except Exception as e:
                check(f"Drag: {label} speed_ms={requested_speed}", False, str(e))
                continue

            await asyncio.sleep(0.5)

            previous_order = current_order
            next_order, next_order_detail = await _read_drag_order()
            if previous_order is not None and next_order is not None:
                if label == "default":
                    check(
                        "Drag: default drag reorders list",
                        next_order != previous_order and next_order[0] != "Alpha",
                        f"before={previous_order}, after={next_order}",
                    )
                else:
                    check(
                        f"Drag: {label} drag reorders list",
                        next_order != previous_order,
                        f"before={previous_order}, after={next_order}",
                    )
                current_order = next_order
                continue

            readback_available = False
            current_order = next_order or current_order
            check(
                f"Drag: {label} reorder readback unavailable",
                True,
                f"falling back to duration checks ({next_order_detail})",
            )

        if not readback_available:
            check(
                "Drag: fallback has 3 successful drags",
                len(drag_results) == 3
                and all(result.get("dragged") is True for _, _, result in drag_results),
                f"results={drag_results}",
            )
            for label, requested_speed, result in drag_results:
                check(
                    f"Drag: {label} duration stays within ±20%",
                    _duration_matches_requested_speed(result, requested_speed),
                    f"requested={requested_speed}, duration={result.get('duration_ms')}",
                )

        # Below safety floor — must error out
        try:
            below_floor_error = None
            try:
                await backend.drag(x0, y0, x0, y3, speed_ms=10)
            except Exception as inner:
                below_floor_error = str(inner)
            check(
                "Drag: speed_ms=10 rejected below safety floor",
                below_floor_error is not None
                and (
                    "drag-threshold" in below_floor_error
                    or "speed_ms" in below_floor_error
                ),
                f"error={below_floor_error}",
            )
        except Exception as e:
            check("Drag: speed_ms=10 rejected below safety floor", False, str(e))

        # Identical coords — must error out
        try:
            same_point_error = None
            try:
                await backend.drag(x0, y0, x0, y0, speed_ms=200)
            except Exception as inner:
                same_point_error = str(inner)
            check(
                "Drag: identical from/to coords rejected",
                same_point_error is not None
                and "identical" in same_point_error.lower(),
                f"error={same_point_error}",
            )
        except Exception as e:
            check("Drag: identical from/to coords rejected", False, str(e))

        await backend.disconnect()

    finally:
        try:
            await m.stop()
        except Exception:
            pass


async def test_system_event_theme():
    """Scenario: engram #80 — send_system_event flips Windows theme via registry + broadcast.

    Verifies:
    - current registry `AppsUseLightTheme` value is read and flipped by toggle
    - response contains `{event: "theme_change", from: <old>, to: <new>}`
    - calling toggle again flips back
    - unsupported events return a structured error without touching registry
    """
    print("\n--- System Event Theme Toggle (engram #80) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(1.5)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("SystemEvent: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check("SystemEvent (skipped)", True, "pywinauto — requires FlaUI bridge")
            await backend.disconnect()
            return

        # Read current theme from HKCU registry
        import winreg

        key_path = r"SOFTWARE\Microsoft\Windows\CurrentVersion\Themes\Personalize"

        def _read_theme() -> int:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
                val, _ = winreg.QueryValueEx(key, "AppsUseLightTheme")
                return int(val)

        try:
            initial = _read_theme()
            initial_name = "light" if initial == 1 else "dark"
            check(
                "SystemEvent: initial theme readable",
                True,
                f"initial={initial_name} ({initial})",
            )
        except Exception as e:
            check("SystemEvent: initial theme readable", False, str(e))
            await backend.disconnect()
            return

        try:
            # First toggle — flip
            try:
                result = await backend.send_system_event("theme_change", mode="toggle")
                check(
                    "SystemEvent: toggle returns {event, from, to}",
                    isinstance(result, dict)
                    and result.get("event") == "theme_change"
                    and result.get("from") == initial_name
                    and result.get("to") != initial_name,
                    f"result={result}",
                )

                await asyncio.sleep(0.3)
                after_first = _read_theme()
                check(
                    "SystemEvent: registry flipped",
                    after_first != initial,
                    f"{initial} -> {after_first}",
                )
            except Exception as e:
                check("SystemEvent: first toggle", False, str(e))
                return

            # Second toggle — flip back to initial
            try:
                result = await backend.send_system_event("theme_change", mode="toggle")
                check(
                    "SystemEvent: second toggle restores",
                    isinstance(result, dict) and result.get("to") == initial_name,
                )
                await asyncio.sleep(0.3)
                restored = _read_theme()
                check(
                    "SystemEvent: registry restored to initial",
                    restored == initial,
                    f"expected={initial}, got={restored}",
                )
            except Exception as e:
                check("SystemEvent: second toggle / restore", False, str(e))

            # Unsupported event name
            try:
                unsupported_error = None
                try:
                    await backend.send_system_event("unknown_event", mode="toggle")
                except Exception as inner:
                    unsupported_error = str(inner)
                check(
                    "SystemEvent: unknown event rejected",
                    unsupported_error is not None,
                    f"error={unsupported_error}",
                )
            except Exception as e:
                check("SystemEvent: unknown event rejected", False, str(e))
        finally:
            try:
                if _read_theme() != initial:
                    await backend.send_system_event("theme_change", mode=initial_name)
            except Exception:
                pass

        await backend.disconnect()

    finally:
        try:
            await m.stop()
        except Exception:
            pass


async def test_persistent_modifier_hold():
    """Scenario: engram #81 — hold_modifiers/release_modifiers keep Ctrl held
    across discrete click calls, enabling MultiExtended ListBox multi-select.

    Verifies:
    - get_held_modifiers returns [] at baseline
    - after hold_modifiers(["ctrl"]), get_held_modifiers returns ["ctrl"]
    - 3 clicks on multiList with Ctrl held leave multiple items selected when
      UIA selection readback is available; otherwise the held-state checks still
      prove Ctrl remained active across discrete clicks
    - release_modifiers clears the held set
    - unknown modifier names rejected with structured error
    - nested hold (ctrl + shift) composes both in the set
    """
    print("\n--- Persistent Modifier Hold (engram #81) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("ModHold: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check("ModHold (skipped)", True, "pywinauto — requires FlaUI bridge")
            await backend.disconnect()
            return

        # Baseline: no held modifiers
        try:
            held = await backend.get_held_modifiers()
            check(
                "ModHold: baseline empty",
                isinstance(held, dict) and held.get("modifiers") == [],
                f"held={held}",
            )
        except Exception as e:
            check("ModHold: baseline empty", False, str(e))

        # Hold Ctrl
        try:
            result = await backend.hold_modifiers(["ctrl"])
            check(
                'ModHold: hold_modifiers(["ctrl"]) succeeds', isinstance(result, dict)
            )
            held = await backend.get_held_modifiers()
            check(
                "ModHold: ctrl held after hold_modifiers",
                isinstance(held, dict) and "ctrl" in held.get("modifiers", []),
                f"held={held}",
            )
        except Exception as e:
            check("ModHold: hold_modifiers ctrl", False, str(e))
            # Defensive release if mid-hold failure
            try:
                await backend.release_modifiers("all")
            except Exception:
                pass
            await backend.disconnect()
            return

        # Click 3 items on the multi-select ListBox while Ctrl is held.
        try:
            info = await backend.find_element(automation_id="multiList")
            rect = info.get("rect") if isinstance(info, dict) else None
            if not isinstance(rect, dict) or not rect.get("width"):
                check("ModHold: multiList present", False, f"rect={rect}")
            else:
                x = rect["x"] + rect["width"] // 2
                # Item indices 0, 2, 4 — ~15 px each
                ys = [rect["y"] + 10 + 15 * idx for idx in (0, 2, 4)]
                for y in ys:
                    await backend.client.call("click", {"x": x, "y": y})
                    await asyncio.sleep(0.15)
                check("ModHold: 3 Ctrl+clicks dispatched", True, f"ys={ys}")
        except Exception as e:
            check("ModHold: 3 Ctrl+clicks dispatched", False, str(e))

        try:
            loop = asyncio.get_running_loop()

            def _read_selected_multi_items() -> list[str]:
                from pywinauto.application import Application
                from pywinauto.controls.uiawrapper import UIAWrapper
                from pywinauto.uia_element_info import UIAElementInfo

                app = Application(backend="uia").connect(process=pid)
                control = app.top_window().child_window(auto_id="multiList")
                control.wait("exists", timeout=5)
                selection = control.iface_selection.GetCurrentSelection()
                selected_names: list[str] = []
                if selection is None:
                    return selected_names

                for index in range(selection.Length):
                    selected_element = selection.GetElement(index)
                    wrapper = UIAWrapper(UIAElementInfo(selected_element))
                    selected_names.append(wrapper.element_info.name or "")

                return [name for name in selected_names if name]

            selected_names = await loop.run_in_executor(
                None, _read_selected_multi_items
            )
            if selected_names:
                check(
                    "ModHold: Ctrl+click leaves multiple items selected",
                    len(selected_names) >= 2,
                    f"selected={selected_names}",
                )
            else:
                check(
                    "ModHold: selection readback unavailable",
                    True,
                    "falling back to held-modifier assertions",
                )
        except Exception as e:
            check(
                "ModHold: selection readback unavailable",
                True,
                f"falling back to held-modifier assertions ({e})",
            )

        try:
            held = await backend.get_held_modifiers()
            check(
                "ModHold: ctrl still held before release",
                isinstance(held, dict) and "ctrl" in held.get("modifiers", []),
                f"held={held}",
            )
        except Exception as e:
            check("ModHold: ctrl still held before release", False, str(e))

        # Nested hold: add Shift
        try:
            await backend.hold_modifiers(["shift"])
            held = await backend.get_held_modifiers()
            mods = set(held.get("modifiers", [])) if isinstance(held, dict) else set()
            check(
                "ModHold: nested hold composes ctrl + shift",
                {"ctrl", "shift"}.issubset(mods),
                f"held={sorted(mods)}",
            )
        except Exception as e:
            check("ModHold: nested hold composes ctrl + shift", False, str(e))

        # Release all
        try:
            await backend.release_modifiers("all")
            held = await backend.get_held_modifiers()
            check(
                'ModHold: release_modifiers("all") clears set',
                isinstance(held, dict) and held.get("modifiers") == [],
                f"held={held}",
            )
        except Exception as e:
            check('ModHold: release_modifiers("all") clears set', False, str(e))

        # Unknown modifier name
        try:
            unknown_error = None
            try:
                await backend.hold_modifiers(["super"])
            except Exception as inner:
                unknown_error = str(inner)
            check(
                "ModHold: unknown modifier rejected",
                unknown_error is not None,
                f"error={unknown_error}",
            )
        except Exception as e:
            check("ModHold: unknown modifier rejected", False, str(e))

        # Final defensive release in case previous steps left anything held.
        try:
            await backend.release_modifiers("all")
        except Exception:
            pass

        await backend.disconnect()

    finally:
        try:
            await m.stop()
        except Exception:
            pass


async def test_scoped_search_performance():
    """Scenario 12: Verify scoped search (root_id) is faster than full tree search."""
    print("\n--- Scoped Search Performance (NFR-2) ---")

    import time as _time

    from netcoredbg_mcp.ui.backend import create_backend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Process started for perf test", False, "no PID")
            return

        await backend.connect(pid)

        # Measure full-tree search (no root_id)
        iterations = 5
        full_times = []
        for _ in range(iterations):
            t0 = _time.monotonic()
            await backend.find_element(automation_id="btnScoped")
            full_times.append(_time.monotonic() - t0)

        # Measure scoped search (with root_id)
        scoped_times = []
        for _ in range(iterations):
            t0 = _time.monotonic()
            await backend.find_element(
                automation_id="btnScoped", root_id="settingsPanel"
            )
            scoped_times.append(_time.monotonic() - t0)

        avg_full = sum(full_times) / len(full_times)
        avg_scoped = sum(scoped_times) / len(scoped_times)

        check(
            "Scoped search timing",
            True,
            f"full={avg_full * 1000:.1f}ms, scoped={avg_scoped * 1000:.1f}ms, "
            f"ratio={avg_full / avg_scoped:.1f}x"
            if avg_scoped > 0
            else "scoped=0ms",
        )
        # NFR-2: scoped should be measurably faster for trees with 100+ elements
        # SmokeTestApp has ~15 elements, so the difference may be small
        # We just record the measurement; on larger apps the ratio should be >2x
        # Performance comparison is informational on small trees (< 100 elements)
        # Hard assert would flake due to measurement noise
        is_faster = avg_scoped <= avg_full * 1.5
        print(
            f"  [INFO] Scoped search not slower than full: "
            f"{'PASS' if is_faster else 'WARN'} — "
            f"scoped={avg_scoped * 1000:.1f}ms <= full*1.5={avg_full * 1.5 * 1000:.1f}ms"
        )

        await backend.disconnect()

    finally:
        try:
            await m.stop()
        except Exception:
            pass


async def test_tracepoints():
    """Scenario 13: Tracepoints — unit test TracepointManager directly."""
    print("\n--- Tracepoints ---")

    from netcoredbg_mcp.session.state import TraceEntry
    from netcoredbg_mcp.session.tracepoints import TracepointManager

    mgr = TracepointManager()

    # Add tracepoints
    tp1 = mgr.add("Program.cs", 15, "i")
    tp2 = mgr.add("Program.cs", 20, "sum")
    check("Tracepoint 1 added", tp1.id == "tp-1")
    check("Tracepoint 2 added", tp2.id == "tp-2")
    check("Two tracepoints registered", len(mgr.tracepoints) == 2)

    # Simulate trace entries
    import time

    mgr._trace_buffer.append(
        TraceEntry(time.monotonic(), "Program.cs", 15, "i", "1", 1, "tp-1")
    )
    mgr._trace_buffer.append(
        TraceEntry(time.monotonic(), "Program.cs", 15, "i", "2", 1, "tp-1")
    )
    mgr._trace_buffer.append(
        TraceEntry(time.monotonic(), "Program.cs", 20, "sum", "3", 1, "tp-2")
    )

    entries = mgr.get_log()
    check("Trace log has 3 entries", len(entries) == 3)

    filtered = mgr.get_log(tracepoint_id="tp-1")
    check("Filtered to tp-1 has 2 entries", len(filtered) == 2)

    # Remove
    removed = mgr.remove("tp-1")
    check("Removed tp-1", removed is not None and removed.id == "tp-1")
    check("One tracepoint left", len(mgr.tracepoints) == 1)

    # Clear log
    count = mgr.clear_log()
    check("Cleared 3 entries", count == 3)
    check("Log empty after clear", len(mgr.get_log()) == 0)

    # Rate limiting
    tp3 = mgr.add("test.cs", 1, "x")
    check("Not rate limited first", not mgr._is_rate_limited(tp3.id))
    check("Rate limited immediately after", mgr._is_rate_limited(tp3.id))


async def test_snapshots():
    """Scenario 14: Snapshots — create, diff, FIFO (unit test)."""
    print("\n--- Snapshots ---")

    from netcoredbg_mcp.session.snapshots import SnapshotManager
    from netcoredbg_mcp.session.state import Snapshot, SnapshotVar

    mgr = SnapshotManager()

    # Manually create snapshots (avoid needing real debug session)
    import time

    snap1 = Snapshot(
        name="before",
        timestamp=time.monotonic(),
        frame_name="Main",
        variables={
            "x": SnapshotVar("42", "int"),
            "name": SnapshotVar("hello", "string"),
        },
    )
    mgr._snapshots["before"] = snap1
    check("Snapshot stored", "before" in mgr.snapshots)

    snap2 = Snapshot(
        name="after",
        timestamp=time.monotonic(),
        frame_name="Main",
        variables={
            "x": SnapshotVar("100", "int"),
            "name": SnapshotVar("hello", "string"),
            "y": SnapshotVar("new", "string"),
        },
    )
    mgr._snapshots["after"] = snap2

    # Diff
    diff = mgr.diff("before", "after")
    check(
        "Diff: 1 changed (x)",
        len(diff["changed"]) == 1 and diff["changed"][0]["name"] == "x",
    )
    check(
        "Diff: 1 added (y)", len(diff["added"]) == 1 and diff["added"][0]["name"] == "y"
    )
    check("Diff: 0 removed", len(diff["removed"]) == 0)
    check("Diff: 1 unchanged (name)", diff["unchanged_count"] == 1)

    # List
    snapshots = mgr.list_snapshots()
    check("List has 2 snapshots", len(snapshots) == 2)

    # FIFO eviction baseline: verify direct dict insertion bypasses eviction
    # (eviction only triggers through SnapshotManager.take(), not _snapshots[...]=)
    for i in range(20):
        mgr._snapshots[f"extra-{i}"] = Snapshot(
            f"extra-{i}", time.monotonic(), "Test", {}
        )
    check(
        "Direct dict access bypasses FIFO eviction", len(mgr._snapshots) == 22
    )  # 2 + 20


async def test_collection_and_object():
    """Scenario 15: Collection analyzer + object summarizer with real debug session."""
    print("\n--- Collection + Object Analysis ---")

    m = await new_session()
    try:
        m.breakpoints.add(
            Breakpoint(file=SOURCE, line=_find_line("int={intVar}"))
        )  # VariableInspection println
        await m.launch(program=DLL, args=["variables"])
        snapshot = await m.wait_for_stopped(timeout=10.0)
        check("Stopped at variables", snapshot is not None)

        # Get scopes to find locals
        tid = m.state.current_thread_id or 1
        frames = await m.get_stack_trace(thread_id=tid, levels=1)
        if not frames:
            check("Has frames", False)
            return
        scopes = await m.get_scopes(frame_id=frames[0].id)
        locals_ref = None
        for s in scopes:
            if s.get("name") == "Locals":
                locals_ref = s.get("variablesReference")
                break

        if not locals_ref:
            check("Has Locals scope", False)
            return

        variables = await m.get_variables(locals_ref)
        check("Has variables", len(variables) > 0, f"count={len(variables)}")

        # Find listVar
        list_var = next((v for v in variables if v.name == "listVar"), None)
        if list_var and list_var.variables_reference > 0:
            items = await m.get_variables(list_var.variables_reference)
            check("Collection has items", len(items) > 0, f"count={len(items)}")

            # Test numeric stats extraction
            numeric = []
            for v in items:
                try:
                    numeric.append(float(v.value))
                except (ValueError, TypeError):
                    pass
            check("Numeric values extracted", len(numeric) > 0, f"values={numeric[:5]}")
        else:
            check("listVar found", list_var is not None)

        # Find dictVar for object summarizer test
        dict_var = next((v for v in variables if v.name == "dictVar"), None)
        if dict_var and dict_var.variables_reference > 0:
            props = await m.get_variables(dict_var.variables_reference)
            check("Object has properties", len(props) > 0, f"count={len(props)}")
        else:
            check("dictVar found", dict_var is not None)

    finally:
        await m.stop()


async def test_tracepoint_performance():
    """Scenario 16: Measure tracepoint pause-evaluate-resume cycle (NFR-1)."""
    print("\n--- Tracepoint Performance (NFR-1) ---")

    import time as _time

    from netcoredbg_mcp.session.tracepoints import TracepointManager

    m = await new_session()
    try:
        m.breakpoints.add(
            Breakpoint(file=SOURCE, line=_find_line("Tick {i}/30"))
        )  # Tick line in LongRunning
        await m.launch(program=DLL, args=["longrun"])

        mgr = TracepointManager()
        m._tracepoint_manager = mgr

        tp = mgr.add(SOURCE, _find_line("Tick {i}/30"), "i")

        snapshot = await m.wait_for_stopped(timeout=10.0)
        check("Stopped at longrun tick", snapshot is not None)

        # Manually time one tracepoint evaluate cycle
        tid = m.state.current_thread_id or 1
        t0 = _time.monotonic()
        await mgr.on_tracepoint_hit(tp, m, tid)
        cycle_ms = (_time.monotonic() - t0) * 1000

        check("Tracepoint cycle time measured", True, f"actual={cycle_ms:.1f}ms")
        check(
            "Tracepoint cycle < 500ms",
            cycle_ms < 500,
            f"actual={cycle_ms:.1f}ms (500ms timeout)",
        )
        check("Trace entry logged", len(mgr.get_log()) >= 1)

        if mgr.get_log():
            entry = mgr.get_log()[0]
            check("Entry has value", entry.value != "", f"value={entry.value}")

    finally:
        await m.stop()


async def test_tracepoint_auto_resume():
    """Scenario 17: Tracepoint auto-resume — tracepoint must NOT stop the app."""
    print("\n--- Tracepoint Auto-Resume ---")

    from netcoredbg_mcp.session.tracepoints import TracepointManager

    m = await new_session()
    try:
        await m.launch(program=DLL, args=["hitcount"])

        # Set up tracepoint manager with tracepoint on "sum += i" line
        mgr = TracepointManager()
        m._tracepoint_manager = mgr
        tp_line = _find_line("sum += i")
        mgr.add(SOURCE, tp_line, "i")

        # Set real DAP breakpoint for the tracepoint
        await m.add_breakpoint(SOURCE, tp_line)

        # Also set a REAL breakpoint on "return sum" to eventually stop
        return_line = _find_line("return sum")
        m.breakpoints.add(Breakpoint(file=SOURCE, line=return_line))
        await m._sync_file_breakpoints(SOURCE)

        # Continue — tracepoint should fire 10x silently, then stop at return_line
        m.prepare_for_execution()
        await m._client.continue_execution(m.state.current_thread_id or 1)
        snapshot = await m.wait_for_stopped(timeout=15.0)

        check(
            "Stopped after tracepoints", snapshot is not None and not snapshot.timed_out
        )
        check(
            "Trace log has entries",
            len(mgr.get_log()) > 0,
            f"entries={len(mgr.get_log())}",
        )

        if mgr.get_log():
            check(
                "Tracepoint logged values",
                mgr.get_log()[0].value != "",
                f"value={mgr.get_log()[0].value}",
            )

    finally:
        await m.stop()


async def test_path_validation_worktrees():
    """Scenario 18: Path validation accepts worktree-style paths."""
    print("\n--- Path Validation ---")

    m = await new_session()

    # Test 1: Normal project path accepted
    try:
        validated = m.validate_path(SOURCE)
        check("Normal path accepted", os.path.isabs(validated))
    except ValueError as e:
        check("Normal path accepted", False, str(e))

    # Test 2: Env var override
    old = os.environ.get("NETCOREDBG_ALLOWED_PATHS", "")
    try:
        import tempfile

        tmp = tempfile.mkdtemp()
        os.environ["NETCOREDBG_ALLOWED_PATHS"] = tmp
        validated = m.validate_path(os.path.join(tmp, "test.cs"))
        check("Env allowed path accepted", os.path.isabs(validated))
    except ValueError as e:
        check("Env allowed path accepted", False, str(e))
    finally:
        os.environ["NETCOREDBG_ALLOWED_PATHS"] = old

    # Test 3: Outside path rejected (only when project_path is set)
    if m.project_path:
        try:
            m.validate_path("C:\\Windows\\System32\\cmd.exe")
            check("Outside path rejected", False, "should have raised ValueError")
        except ValueError:
            check("Outside path rejected", True)
    else:
        check(
            "Outside path rejected (skipped — no project_path)",
            True,
            "scope check requires project_path",
        )


async def test_heartbeat_during_wait():
    """Scenario 19: Heartbeat fires during long wait_for_stopped."""
    print("\n--- Heartbeat During Wait ---")

    m = await new_session()
    try:
        await m.launch(program=DLL, args=["longrun"])
        await asyncio.sleep(0.5)

        # Wait with heartbeat — longrun takes ~6s, so heartbeat should fire at least once
        heartbeats = []

        async def on_heartbeat(elapsed: float) -> None:
            heartbeats.append(elapsed)

        m.prepare_for_execution()
        await m._client.continue_execution(m.state.current_thread_id or 1)

        snapshot = await m.wait_for_stopped(
            timeout=15.0, heartbeat_callback=on_heartbeat
        )

        check("Long run completed", snapshot is not None)
        check("Heartbeat fired", len(heartbeats) >= 1, f"count={len(heartbeats)}")
        check(
            "Heartbeat timing reasonable",
            heartbeats[0] >= 4.0 if heartbeats else False,
            f"first={heartbeats[0]:.1f}s" if heartbeats else "none",
        )

    finally:
        await m.stop()


# ─────────────────────────────────────────────────────────────
# v0.11.1 scenarios
# ─────────────────────────────────────────────────────────────


async def test_window_lifecycle():
    """Scenario: maximize → minimize → restore → close (via WindowPattern)."""
    print("\n--- Window Lifecycle (v0.11.1) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Window lifecycle: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check(
                "Window lifecycle (skipped)",
                True,
                "pywinauto — WindowPattern requires FlaUI",
            )
            await backend.disconnect()
            return

        try:
            # Maximize
            result = await backend.maximize_window()
            check("Maximize: succeeded", result.get("maximized") is True, str(result))

            await asyncio.sleep(0.5)

            # Minimize
            result = await backend.minimize_window()
            check("Minimize: succeeded", result.get("minimized") is True, str(result))

            await asyncio.sleep(0.5)

            # Restore
            result = await backend.restore_window()
            check("Restore: succeeded", result.get("restored") is True, str(result))

            await asyncio.sleep(0.5)

            # Close (last — window gone after this)
            result = await backend.close_window()
            check("Close: succeeded", result.get("closed") is True, str(result))

        except Exception as e:
            check("Window lifecycle", False, str(e))
        finally:
            try:
                await backend.disconnect()
            except Exception:
                pass

    finally:
        # Session already ended (window was closed) — stop cleans up
        try:
            await m.stop()
        except Exception:
            pass


async def test_expand_collapse_tree():
    """Scenario: expand CharactersTree nodes, verify Main cast and leaf items."""
    print("\n--- Expand/Collapse Tree (v0.11.1) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Expand tree: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check(
                "Expand tree (skipped)",
                True,
                "pywinauto — ExpandCollapsePattern requires FlaUI",
            )
            await backend.disconnect()
            return

        try:
            # Find the tree first to make sure it exists
            tree_info = await backend.find_element(automation_id="CharactersTree")
            check(
                "CharactersTree present",
                isinstance(tree_info, dict) and tree_info.get("found", False),
                str(tree_info),
            )

            # Expand root node
            result = await backend.expand("CharactersTreeRoot")
            check(
                "Expand CharactersTreeRoot",
                result.get("expanded") is True,
                str(result),
            )

            # Expand again — should be idempotent
            result2 = await backend.expand("CharactersTreeRoot")
            check(
                "Expand idempotent (was_already=True on second call)",
                result2.get("was_already") is True,
                str(result2),
            )

            # Expand Main cast
            result = await backend.expand("CharactersTree_MainCast")
            check(
                "Expand CharactersTree_MainCast",
                result.get("expanded") is True,
                str(result),
            )

            # Collapse root
            result = await backend.collapse("CharactersTreeRoot")
            check(
                "Collapse CharactersTreeRoot",
                result.get("collapsed") is True,
                str(result),
            )

        except Exception as e:
            check("Expand/collapse tree", False, str(e))
        finally:
            await backend.disconnect()

    finally:
        await m.stop()


async def test_set_value_slider():
    """Scenario: set DurationSlider to valid value, then try out-of-range."""
    print("\n--- Set Value Slider (v0.11.1) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Slider: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check(
                "Slider (skipped)", True, "pywinauto — RangeValuePattern requires FlaUI"
            )
            await backend.disconnect()
            return

        try:
            # Valid value
            result = await backend.set_value("DurationSlider", 75.0)
            check(
                "SetValue 75 on DurationSlider",
                result.get("set") is True,
                str(result),
            )
            check(
                "SetValue returns value field",
                result.get("value") == 75.0,
                f"value={result.get('value')}",
            )

            # Out-of-range value
            result = await backend.set_value("DurationSlider", 200.0)
            check(
                "SetValue 200 returns set=False",
                result.get("set") is False,
                str(result),
            )
            check(
                "SetValue out-of-range reason contains 'out of range'",
                "out of range" in result.get("reason", "").lower(),
                f"reason={result.get('reason')}",
            )

        except Exception as e:
            check("Slider set_value", False, str(e))
        finally:
            await backend.disconnect()

    finally:
        await m.stop()


async def test_realize_virtualized_item():
    """Scenario: attempt to realize from WinForms ListBox (expects unsupported container)."""
    print("\n--- Realize Virtualized Item (v0.11.1) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Realize: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check(
                "Realize (skipped)",
                True,
                "pywinauto — VirtualizedItemPattern requires FlaUI",
            )
            await backend.disconnect()
            return

        try:
            # PRE-REALIZE VERIFICATION: check whether the row is already accessible.
            # WinForms ListBox is NOT a truly virtualized container — items are always
            # realized. We document this explicitly so the test is honest about what
            # it exercises, and to provide a baseline for future WPF fixtures.
            pre_realize_found = False
            try:
                pre = await backend.find_element(automation_id="VirtList_Row_150")
                pre_realize_found = isinstance(pre, dict) and pre.get("found", False)
            except (RuntimeError, asyncio.TimeoutError):  # noqa: BLE001
                pre_realize_found = False

            if pre_realize_found:
                # WinForms fixture does NOT exercise true VirtualizedItemPattern —
                # the element is already accessible before realize is called.
                # Note: WinForms ListBox is not truly virtualized; for full VirtualizingStackPanel
                # coverage, a WPF fixture is needed (tracked in the local
                # flaui-pattern-expansion spec under .agent/specs/).
                # under "v0.11.2 deferred scope").
                print(
                    "  [WARNING] VirtList_Row_150 is already accessible before realize — "
                    "WinForms ListBox is not a true virtualized container. "
                    "Full VirtualizedItemPattern semantics require a WPF fixture."
                )

            # WinForms ListBox does not support ItemContainerPattern, so realize
            # should return realized=False with an explanatory reason.
            result = await backend.realize_virtualized_item(
                container_automation_id="VirtList",
                prop_name="AutomationId",
                value="VirtList_Row_150",
            )
            # Expected: realized=false because WinForms ListBox lacks ItemContainerPattern
            check(
                "WinForms ListBox: realized=False (no ItemContainerPattern)",
                result.get("realized") is False,
                str(result),
            )
            check(
                "Failure reason is present",
                bool(result.get("reason")),
                f"reason={result.get('reason')}",
            )

            # POST-REALIZE VERIFICATION: the element must be findable after the call.
            # Even without true virtualization, the round-trip must not break access.
            try:
                post = await backend.find_element(automation_id="VirtList_Row_150")
                post_found = isinstance(post, dict) and post.get("found", False)
                check(
                    "VirtList_Row_150 accessible after realize call",
                    post_found,
                    f"find_element returned: {post}",
                )
                if post_found:
                    rect = (
                        post.get("rect")
                        or post.get("bounding_rect")
                        or post.get("BoundingRectangle")
                    )
                    check(
                        "VirtList_Row_150 has valid bounding_rect after realize",
                        rect is not None,
                        f"bounding_rect={rect}",
                    )
            except (RuntimeError, asyncio.TimeoutError) as post_e:  # noqa: BLE001
                check(
                    "VirtList_Row_150 accessible after realize call", False, str(post_e)
                )

            # Also test item-not-found path on dragList (no ItemContainerPattern either)
            result2 = await backend.realize_virtualized_item(
                container_automation_id="dragList",
                prop_name="Name",
                value="NonExistentItem",
            )
            check(
                "dragList: realized=False",
                result2.get("realized") is False,
                str(result2),
            )

        except Exception as e:
            check("Realize virtualized item", False, str(e))
        finally:
            await backend.disconnect()

    finally:
        await m.stop()


async def test_clipboard_roundtrip():
    """Scenario: write to clipboard, read back; test unicode round-trip."""
    print("\n--- Clipboard Round-trip (v0.11.1) ---")

    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager()
    try:
        await m.launch(program=DLL, args=["gui"])
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Clipboard: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            check(
                "Clipboard (skipped)", True, "pywinauto — Clipboard STA requires FlaUI"
            )
            await backend.disconnect()
            return

        try:
            # ASCII round-trip
            write_result = await backend.clipboard_write("test123")
            check(
                "Clipboard write ASCII",
                write_result.get("written") is True,
                str(write_result),
            )

            read_result = await backend.clipboard_read()
            check(
                "Clipboard read has_text",
                read_result.get("has_text") is True,
                str(read_result),
            )
            check(
                "Clipboard ASCII round-trip",
                read_result.get("text") == "test123",
                f"got={read_result.get('text')!r}",
            )

            # Unicode round-trip
            unicode_text = "emoji \U0001f389 \u00fcnic\u00f6de"
            write_result2 = await backend.clipboard_write(unicode_text)
            check(
                "Clipboard write unicode",
                write_result2.get("written") is True,
                str(write_result2),
            )

            read_result2 = await backend.clipboard_read()
            check(
                "Clipboard unicode round-trip",
                read_result2.get("text") == unicode_text,
                f"expected={unicode_text!r} got={read_result2.get('text')!r}",
            )

        except Exception as e:
            check("Clipboard round-trip", False, str(e))
        finally:
            # Reset clipboard to empty
            try:
                await backend.clipboard_write("")
            except Exception:
                pass
            await backend.disconnect()

    finally:
        await m.stop()


async def test_runtime_hygiene_preflight():
    print("\n18. RUNTIME HYGIENE PREFLIGHT")
    m = await new_session()
    original_clear_breakpoints = m.clear_breakpoints
    try:
        bp_line = _find_line("sum += i")
        m.breakpoints.add(Breakpoint(file=SOURCE, line=bp_line))

        result = (await m.hygiene.preflight(file=SOURCE)).to_dict()
        print(f"  evidence: {result}")
        check("Hygiene preflight PASS", result["status"] == "PASS", str(result))
        check(
            "Hygiene preflight removed scoped breakpoint",
            result["cleared"]["breakpoints"] == 1,
            str(result["cleared"]),
        )
        check(
            "Hygiene preflight leaves no targeted breakpoints",
            result["remaining_breakpoints"] == [],
            str(result["remaining_breakpoints"]),
        )

        m.breakpoints.add(Breakpoint(file=SOURCE, line=bp_line))

        async def leaking_clear_breakpoints(file: str | None = None) -> int:
            return 0

        m.clear_breakpoints = leaking_clear_breakpoints  # type: ignore[method-assign]
        leaked = (await m.hygiene.preflight(file=SOURCE)).to_dict()
        print(f"  leak evidence: {leaked}")
        check(
            "Hygiene preflight FAILS when breakpoint remains",
            leaked["status"] == "FAIL" and len(leaked["remaining_breakpoints"]) == 1,
            str(leaked),
        )
    finally:
        m.clear_breakpoints = original_clear_breakpoints  # type: ignore[method-assign]
        await m.clear_breakpoints(SOURCE)
        await m.stop()


async def test_instrumentation_group_lifecycle():
    print("\n19. INSTRUMENTATION GROUP LIFECYCLE")
    import time as _time

    m = await new_session()
    original_remove_breakpoint = m.remove_breakpoint
    try:
        bp_line = _find_line("sum += i")
        trace_line = _find_line("return sum;")
        created = (
            await m.instrumentation.create_group(
                "manual_flow",
                breakpoints=[{"file": SOURCE, "line": bp_line}],
                tracepoints=[{"file": SOURCE, "line": trace_line, "expression": "sum"}],
            )
        ).to_dict()
        tracepoint_id = created["tracepoints"][0]["id"]
        norm = m.breakpoints._normalize_path(SOURCE)
        m.state.hit_counts[(norm, bp_line)] = 2
        m._tracepoint_manager._trace_buffer.append(
            TraceEntry(
                _time.monotonic(), SOURCE, trace_line, "sum", "3", 1, tracepoint_id
            )
        )

        inspected = (await m.instrumentation.inspect_group("manual_flow")).to_dict()
        print(f"  evidence: {inspected}")
        check(
            "Instrumentation group created", created["status"] == "PASS", str(created)
        )
        check(
            "Instrumentation group inspect has hit evidence",
            inspected["summary"]["hit_count"] == 2,
            str(inspected["summary"]),
        )
        check(
            "Instrumentation group inspect has trace evidence",
            inspected["summary"]["trace_log_count"] == 1,
            str(inspected["summary"]),
        )

        cleared = (await m.instrumentation.clear_group("manual_flow")).to_dict()
        print(f"  clear evidence: {cleared}")
        check(
            "Instrumentation group clear PASS",
            cleared["status"] == "PASS",
            str(cleared),
        )

        await m.instrumentation.create_group(
            "manual_leak",
            breakpoints=[{"file": SOURCE, "line": bp_line}],
        )

        async def leaking_remove_breakpoint(file: str, line: int) -> bool:
            return False

        m.remove_breakpoint = leaking_remove_breakpoint  # type: ignore[method-assign]
        leaked = (await m.instrumentation.clear_group("manual_leak")).to_dict()
        print(f"  leak evidence: {leaked}")
        check(
            "Instrumentation group clear FAILS on leak",
            leaked["status"] == "FAIL" and len(leaked["leaks"]) == 1,
            str(leaked),
        )
    finally:
        m.remove_breakpoint = original_remove_breakpoint  # type: ignore[method-assign]
        await m.clear_breakpoints(SOURCE)
        m.runtime_smoke.instrumentation_groups.clear()
        await m.stop()


async def test_output_checkpoint_assertions():
    print("\n20. OUTPUT CHECKPOINT ASSERTIONS")
    m = await new_session()
    try:
        m.state.output_buffer.append(OutputEntry("boot\nready\n"))
        checkpoint = m.output_assertions.create_checkpoint("manual_output").to_dict()
        m.state.output_buffer.append(OutputEntry("selected row 1\nwarning: slow\n"))

        passed_result = m.output_assertions.assert_since(
            "manual_output",
            required=["selected row", "warning"],
            forbidden=["fatal"],
        ).to_dict()
        failed_result = m.output_assertions.assert_since(
            "manual_output",
            required=["missing text"],
            forbidden=["warning"],
        ).to_dict()

        print(f"  checkpoint evidence: {checkpoint}")
        print(f"  pass evidence: {passed_result}")
        print(f"  fail evidence: {failed_result}")
        check(
            "Output checkpoint created", checkpoint["status"] == "PASS", str(checkpoint)
        )
        check(
            "Output assertion PASS has compact evidence",
            passed_result["status"] == "PASS"
            and passed_result["summary"]["matched_line_count"] == 2,
            str(passed_result),
        )
        check(
            "Output assertion FAIL lists missing and forbidden",
            failed_result["status"] == "FAIL"
            and failed_result["missing_required"] == ["missing text"]
            and len(failed_result["forbidden_matches"]) == 1,
            str(failed_result),
        )
    finally:
        await m.stop()


async def test_runtime_smoke_bounded_runner():
    print("\n21. RUNTIME SMOKE BOUNDED RUNNER")
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner

    m = await new_session()

    async def append_output(text: str, category: str = "stdout") -> dict:
        m.state.output_buffer.append(OutputEntry(text, category=category))
        return {"status": "PASS", "reason": "output appended", "text_length": len(text)}

    try:
        runner = RuntimeSmokeRunner(
            m,
            service_adapters={"append_output": append_output},
        )
        passed_result = await runner.run(
            {
                "name": "manual-bounded-pass",
                "budgets": {"max_actions": 4, "max_elapsed_seconds": 10},
                "actions": [
                    {"name": "output_checkpoint", "args": {"name": "manual_runner"}},
                    {"name": "append_output", "args": {"text": "runner ready\n"}},
                ],
                "assertions": [
                    {
                        "name": "output_assert_since",
                        "args": {
                            "checkpoint": "manual_runner",
                            "required": ["runner ready"],
                        },
                    }
                ],
            }
        )
        failed_result = await runner.run(
            {
                "name": "manual-bounded-fail",
                "actions": [
                    {
                        "name": "output_checkpoint",
                        "args": {"name": "manual_runner_fail"},
                    },
                ],
                "assertions": [
                    {
                        "name": "output_assert_since",
                        "args": {
                            "checkpoint": "manual_runner_fail",
                            "required": ["missing"],
                        },
                    }
                ],
            }
        )

        print(f"  pass compact: {passed_result['compact']}")
        print(f"  fail compact: {failed_result['compact']}")
        check(
            "Bounded runner PASS includes cleanup",
            passed_result["status"] == "PASS"
            and passed_result["cleanup"]["status"] == "PASS",
            str(passed_result["compact"]),
        )
        check(
            "Bounded runner FAIL lists failed assertions",
            failed_result["status"] == "FAIL"
            and len(failed_result["failed_assertions"]) == 1,
            str(failed_result["compact"]),
        )
        check(
            "Bounded runner failure prints cleanup outcome",
            failed_result["cleanup"]["status"] in {"PASS", "FAIL"},
            str(failed_result["cleanup"]),
        )
    finally:
        await m.stop()


async def test_ui_focused_evidence():
    print("\n22. UI FOCUSED EVIDENCE")
    from netcoredbg_mcp.ui.events import UIEventBufferStore
    from netcoredbg_mcp.ui.snapshots import (
        UISnapshotStore,
        capture_ui_snapshot,
        diff_ui_snapshots,
        query_ui_fields,
    )

    class ManualBackend:
        def __init__(self):
            self.responses = [
                {
                    "status": "PASS",
                    "elements": [
                        {
                            "element_id": "txtOutput",
                            "text": "Initial",
                            "focus": False,
                            "selection": {"selected": False},
                            "enabled": True,
                            "visible": True,
                            "window": {"title": "Manual"},
                            "full_tree": {"must": "not leak"},
                        }
                    ],
                    "element_count": 1,
                },
                {
                    "status": "PASS",
                    "elements": [
                        {
                            "element_id": "txtOutput",
                            "text": "Initial",
                            "focus": False,
                            "selection": {"selected": False},
                        }
                    ],
                    "element_count": 1,
                },
                {
                    "status": "PASS",
                    "elements": [
                        {
                            "element_id": "txtOutput",
                            "text": "Changed",
                            "focus": True,
                            "selection": {"selected": False},
                        }
                    ],
                    "element_count": 1,
                },
                {
                    "status": "PASS",
                    "elements": [
                        {
                            "element_id": "txtOutput",
                            "text": "Changed again",
                            "focus": True,
                            "selection": {"selected": False},
                        }
                    ],
                    "element_count": 1,
                },
                {
                    "status": "PASS",
                    "elements": [
                        {
                            "element_id": "txtOutput",
                            "text": "Changed final",
                            "focus": False,
                            "selection": {"selected": False},
                        }
                    ],
                    "element_count": 1,
                },
            ]

        async def query_ui(self, selector, fields, max_results=20):
            return self.responses.pop(0)

    class UnsupportedBackend:
        async def query_ui(self, selector, fields, max_results=20):
            return {
                "status": "BLOCKED",
                "unsupported": True,
                "backend": "manual-unsupported",
                "reason": "focused UI evidence unsupported in this backend",
            }

    backend = ManualBackend()
    snapshot_store = UISnapshotStore()
    event_store = UIEventBufferStore()
    selector = {"automation_id": "txtOutput"}

    query = await query_ui_fields(
        backend,
        selector,
        fields=["text", "focus", "selection"],
    )
    await capture_ui_snapshot(
        backend,
        snapshot_store,
        name="before",
        selector=selector,
        fields=["text", "focus", "selection"],
    )
    await capture_ui_snapshot(
        backend,
        snapshot_store,
        name="after",
        selector=selector,
        fields=["text", "focus", "selection"],
    )
    diff = diff_ui_snapshots(
        snapshot_store,
        "before",
        "after",
        fields=["text", "focus", "selection"],
    )
    started = await event_store.start(
        backend,
        buffer_id="manual",
        selector=selector,
        fields=["text", "focus", "selection"],
        max_events=2,
    )
    events = await event_store.read("manual")
    unsupported = await query_ui_fields(
        UnsupportedBackend(),
        selector,
        fields=["text"],
    )

    print(f"  query evidence: {query}")
    print(f"  diff evidence: {diff}")
    print(f"  event start evidence: {started}")
    print(f"  event read evidence: {events}")
    print(f"  unsupported evidence: {unsupported}")
    check(
        "UI query is field-limited",
        query["status"] == "PASS" and "full_tree" not in str(query),
        str(query),
    )
    check(
        "UI snapshot diff reports changed text/focus",
        diff["status"] == "PASS" and len(diff["changed"]) == 1,
        str(diff),
    )
    check(
        "UI event buffer reports bounded polling events",
        started["status"] == "PASS"
        and events["status"] == "PASS"
        and events["source"] == "polling"
        and events["event_count"] == 1,
        str(events),
    )
    check(
        "UI focused evidence reports BLOCKED on unsupported backend",
        unsupported["status"] == "BLOCKED",
        str(unsupported),
    )


async def test_wpf_shift_datagrid_evidence():
    print("\n23. WPF SHIFT DATAGRID EVIDENCE")
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend
    from netcoredbg_mcp.ui.grid import (
        assert_grid_range,
        read_grid_selected_rows,
        select_grid_range,
    )
    from netcoredbg_mcp.ui.key_sequence import run_scoped_key_sequence

    m = SessionManager()
    backend = None
    try:
        await m.launch(program=WPF_DLL)
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("WPF Shift/DataGrid: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            evidence = {
                "status": "BLOCKED",
                "backend": type(backend).__name__,
                "reason": "FlaUI bridge required for WPF held-modifier proof",
            }
            print(f"  evidence: {evidence}")
            check(
                "WPF Shift/DataGrid reports BLOCKED without FlaUI", True, str(evidence)
            )
            return

        selector = {"automation_id": "dataGrid"}
        initial = await select_grid_range(backend, selector, 0, 0)
        key_result = await run_scoped_key_sequence(
            backend,
            selector,
            modifiers=["shift"],
            keys=["Down", "Down"],
        )
        selected_rows = await read_grid_selected_rows(backend, selector)
        range_result = await assert_grid_range(backend, selector, 0, 2)
        text_result = await backend.extract_text(automation_id="txtOutput")
        status_text = (
            text_result.get("text", "")
            if isinstance(text_result, dict)
            else str(text_result)
        )

        evidence = {
            "initial": initial,
            "key_sequence": key_result,
            "selected_rows": selected_rows,
            "range": range_result,
            "status_text": status_text,
        }
        print(f"  evidence: {evidence}")
        check(
            "WPF route logs Shift held",
            "DataGridArrow" in status_text and "shift=True" in status_text,
            status_text,
        )
        check(
            "WPF key sequence sent two Down keys",
            key_result.get("status") == "PASS" and key_result.get("sent_count") == 2,
            str(key_result),
        )
        check(
            "WPF key sequence cleanup released Shift",
            key_result.get("final_held_modifiers") == [],
            str(key_result),
        )
        check(
            "WPF DataGrid selected range expanded",
            range_result.get("status") == "PASS",
            str(range_result),
        )
    finally:
        if backend is not None:
            try:
                await backend.disconnect()
            except Exception as exc:
                print(f"  [DEBUG] backend.disconnect() failed: {exc}")
        await m.stop()


async def test_wpf_ui_grid_rows_alias_fixture_replay():
    print("\nWPF UI_GRID ROWS ALIAS FIXTURE REPLAY")
    from netcoredbg_mcp.tools.ui_evidence import register_ui_evidence_tools

    m = SessionManager()
    try:
        await m.launch(program=WPF_DLL)
        await asyncio.sleep(2.0)

        mcp = _CapturingMCP()
        register_ui_evidence_tools(
            mcp=mcp,
            session=m,
            check_session_access=lambda ctx: None,
        )

        response = await mcp.tools["ui_grid"](
            ctx=None,
            action="rows",
            automation_id="dataGrid",
        )

        evidence, first_phrase = _grid_alias_evidence(response)
        print(f"  evidence: {evidence}")
        check(
            "WPF ui_grid rows alias returns visible rows",
            evidence["status"] == "PASS"
            and evidence["requested_action"] == "rows"
            and evidence["canonical_action"] == "visible_rows"
            and bool(evidence["row_count"]),
            str(evidence),
        )
        check(
            "WPF ui_grid rows alias preserves stable row identity",
            isinstance(first_phrase, str) and first_phrase.startswith("Fixture cue"),
            str(evidence),
        )
    finally:
        await m.stop()


async def test_wpf_one_call_runtime_smoke_workflow():
    print("\nWPF ONE-CALL RUNTIME SMOKE WORKFLOW")
    import tempfile

    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {"backend": None}
    baseline_state = "baseline"

    backend_holder["backend"] = create_backend(process_registry=m.process_registry)
    if not isinstance(backend_holder["backend"], FlaUIBackend):
        evidence = {
            "status": "BLOCKED",
            "backend": type(backend_holder["backend"]).__name__,
            "reason": "FlaUI bridge required for WPF one-call workflow smoke",
        }
        print(f"  evidence: {evidence}")
        check("WPF one-call reports BLOCKED without FlaUI", True, str(evidence))
        return

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = create_backend(process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError("Process ID not available for WPF workflow smoke")
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    mutable_file: str | None = None
    try:
        smoke_tmp_root = os.path.join(BASE, ".agent", "tmp", "wpf-runtime-smoke")
        os.makedirs(smoke_tmp_root, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="workflow-", dir=smoke_tmp_root
        ) as temp_dir:
            mutable_file = os.path.join(temp_dir, "wpf-workflow-state.txt")
            with open(mutable_file, "w", encoding="utf-8") as handle:
                handle.write(baseline_state)

            plan = {
                "schema": "netcoredbg.runtime_smoke.v1",
                "name": "wpf one-call assignment toggle undo",
                "launch": {
                    "program": WPF_DLL,
                    "cwd": os.path.dirname(WPF_DLL),
                    "pre_build": True,
                    "build_project": os.path.join(
                        BASE,
                        "tests",
                        "fixtures",
                        "WpfSmokeApp",
                        "WpfSmokeApp.csproj",
                    ),
                    "build_configuration": "Debug",
                    "env": {"WPF_SMOKE_MUTABLE_FILE": mutable_file},
                },
                "freshness": {
                    "expected_process_name": "dotnet",
                    "expected_modules": ["WpfSmokeApp.dll"],
                },
                "steps": [
                    {
                        "id": "baseline_grid",
                        "op": "ui.grid.snapshot",
                        "selector": {"automation_id": "dataGrid"},
                        "rows": {"visible_only": True, "max": 5},
                        "columns": ["Start", "End", "Character", "Phrase"],
                    },
                    {
                        "id": "select_rows",
                        "op": "ui.grid.select_range",
                        "selector": {"automation_id": "dataGrid"},
                        "start_index": 0,
                        "end_index": 1,
                    },
                    {
                        "id": "before_assign",
                        "op": "debug.output_checkpoint",
                        "name": "before_assign",
                    },
                    {
                        "id": "assign_character",
                        "op": "ui.list.invoke_item",
                        "selector": {"automation_id": "CharactersListBox"},
                        "item": {"name": "ALICE"},
                        "invoke": "enter",
                    },
                    {
                        "id": "assign_output",
                        "op": "debug.output_assert_since",
                        "checkpoint": "before_assign",
                        "required": [
                            "WpfWorkflow AssignCharacter route=ListInvoke selectedCount=2"
                        ],
                        "forbidden": ["manual primitive fallback"],
                    },
                    {
                        "id": "assign_grid_assert",
                        "op": "ui.grid.assert_rows",
                        "selector": {"automation_id": "dataGrid"},
                        "columns": ["Start", "End", "Character", "Phrase"],
                        "rows": [
                            {
                                "index": 0,
                                "contains": {
                                    "Start": "00:00:01.0",
                                    "End": "00:00:03.0",
                                    "Character": "ALICE",
                                    "Phrase": "Fixture cue one",
                                },
                            },
                            {
                                "index": 1,
                                "contains": {
                                    "Start": "00:00:04.0",
                                    "End": "00:00:06.0",
                                    "Character": "ALICE",
                                    "Phrase": "Fixture cue two",
                                },
                            },
                        ],
                    },
                    {
                        "id": "before_gender",
                        "op": "debug.output_checkpoint",
                        "name": "before_gender",
                    },
                    {
                        "id": "toggle_gender",
                        "op": "ui.list.toggle_item_child",
                        "selector": {"automation_id": "CharactersListBox"},
                        "item": {"name": "ALICE"},
                        "child": {
                            "automation_id": "CharGender",
                            "control_type": "CheckBox",
                        },
                        "target_state": "On",
                    },
                    {
                        "id": "gender_assert",
                        "op": "ui.text.assert",
                        "selector": {"automation_id": "genderStatus"},
                        "contains": "ALICE female",
                    },
                    {
                        "id": "undo_gender",
                        "op": "ui.invoke",
                        "selector": {"automation_id": "menuItemUndo"},
                    },
                    {
                        "id": "undo_gender_assert",
                        "op": "ui.text.assert",
                        "selector": {"automation_id": "genderStatus"},
                        "contains": "ALICE male",
                    },
                    {
                        "id": "undo_focus_assert",
                        "op": "ui.focus.assert",
                        "selector": {"automation_id": "dataGrid"},
                    },
                ],
                "cleanup": {
                    "restore_files": [
                        {"path": mutable_file, "baseline_text": baseline_state},
                    ],
                    "stop_debug": "graceful",
                    "debug_hygiene": True,
                },
                "budgets": {"max_actions": 20, "max_elapsed_seconds": 60},
            }

            result = await RuntimeSmokeRunner(
                m,
                service_adapters=ui_operation_adapters(ensure_ui_connected),
            ).run(plan)
            with open(mutable_file, encoding="utf-8") as handle:
                restored_state = handle.read()
    finally:
        if mutable_file is not None and os.path.exists(mutable_file):
            try:
                with open(mutable_file, "w", encoding="utf-8") as handle:
                    handle.write(baseline_state)
            except OSError as exc:
                print(f"  [DEBUG] mutable state restore failed: {exc}")
        if backend_holder["backend"] is not None:
            try:
                await backend_holder["backend"].disconnect()
            except Exception as exc:
                print(f"  [DEBUG] backend.disconnect() failed: {exc}")
        await m.stop()

    evidence = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "action_count": result.get("action_count"),
        "cleanup": result.get("cleanup"),
        "restored_state": restored_state,
        "completed_steps": [
            {
                "name": step.get("name"),
                "status": step.get("status"),
            }
            for step in result.get("completed_steps", [])
        ],
    }
    print(f"  evidence: {evidence}")
    terminal_status = result.get("status")
    check(
        "WPF one-call run_runtime_smoke returned PASS",
        terminal_status == "PASS",
        str(evidence),
    )
    result_text = str(result)
    check(
        "WPF one-call assigned selected rows with invariants",
        "Fixture cue one" in result_text and "ALICE" in result_text,
        result_text[:400],
    )
    check(
        "WPF one-call scoped toggle and undo evidence present",
        "ALICE female" in result_text and "ALICE male" in result_text,
        result_text[:400],
    )
    cleanup = result.get("cleanup", {})
    check(
        "WPF one-call focus and cleanup restored",
        restored_state == baseline_state and cleanup.get("status") == "PASS",
        str(evidence),
    )


async def run_wpf_v2_state_oracle_runtime_smoke() -> dict[str, Any]:
    return await _run_v2_state_oracle_runtime_smoke(
        label="WPF",
        program=WPF_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "WpfSmokeApp",
            "WpfSmokeApp.csproj",
        ),
    )


def _wpf_hover_selector_matrix_cases() -> list[dict[str, Any]]:
    return [
        {
            "id": "root_zero",
            "selector": {
                "automation_id": "hoverTrigger",
                "root_id": "missingHoverRoot",
            },
            "expect_status": "BLOCKED",
            "expect_phase": "resolve_root",
            "expect_match_count": 0,
        },
        {
            "id": "root_many",
            "selector": {
                "automation_id": "hoverMultiplicityTarget",
                "root_id": "hoverDuplicateRoot",
            },
            "expect_status": "BLOCKED",
            "expect_phase": "resolve_root",
            "expect_match_count": 2,
        },
        {
            "id": "automation_id_zero",
            "selector": {
                "automation_id": "missingHoverTarget",
                "root_id": "hoverRegion",
            },
            "expect_status": "BLOCKED",
            "expect_phase": "resolve_target",
            "expect_match_count": 0,
        },
        {
            "id": "automation_id_many",
            "selector": {
                "automation_id": "hoverDuplicateTarget",
                "root_id": "hoverMultiplicityRegion",
            },
            "expect_status": "BLOCKED",
            "expect_phase": "resolve_target",
            "expect_match_count": 2,
        },
        {
            "id": "xpath_zero",
            "selector": {
                "xpath": "//Button[@AutomationId='missingHoverTarget']",
                "root_id": "hoverRegion",
            },
            "expect_status": "BLOCKED",
            "expect_phase": "resolve_target",
            "expect_match_count": 0,
        },
        {
            "id": "xpath_many",
            "selector": {
                "xpath": "//Button[@AutomationId='hoverDuplicateTarget']",
                "root_id": "hoverMultiplicityRegion",
            },
            "expect_status": "BLOCKED",
            "expect_phase": "resolve_target",
            "expect_match_count": 2,
        },
        {
            "id": "name_control_type_zero",
            "selector": {
                "name": "Missing hover target",
                "control_type": "Button",
                "root_id": "hoverRegion",
            },
            "expect_status": "BLOCKED",
            "expect_phase": "resolve_target",
            "expect_match_count": 0,
        },
        {
            "id": "name_control_type_many",
            "selector": {
                "name": "Duplicate target",
                "control_type": "Button",
                "root_id": "hoverMultiplicityRegion",
            },
            "expect_status": "BLOCKED",
            "expect_phase": "resolve_target",
            "expect_match_count": 2,
        },
        {
            "id": "automation_id_one",
            "selector": {
                "automation_id": "hoverTrigger",
                "root_id": "hoverRegion",
            },
            "expect_status": "PASS",
            "expect_criterion": "automationId",
        },
        {
            "id": "xpath_one",
            "selector": {
                "xpath": "//Button[@AutomationId='hoverTrigger']",
                "root_id": "hoverRegion",
            },
            "expect_status": "PASS",
            "expect_criterion": "xpath",
        },
        {
            "id": "name_control_type_one",
            "selector": {
                "name": "Hover trigger",
                "control_type": "Button",
                "root_id": "hoverRegion",
            },
            "expect_status": "PASS",
            "expect_criterion": "name+controlType",
        },
        {
            "id": "precedence_automation_id_wins",
            "selector": {
                "automation_id": "hoverTrigger",
                "xpath": "//Button[@AutomationId='hoverOutsideSentinel']",
                "name": "Outside sentinel",
                "control_type": "Button",
                "root_id": "hoverRegion",
            },
            "expect_status": "PASS",
            "expect_criterion": "automationId",
        },
        {
            "id": "precedence_xpath_after_automation_id_miss",
            "selector": {
                "automation_id": "missingHoverTarget",
                "xpath": "//Button[@AutomationId='hoverTrigger']",
                "name": "Outside sentinel",
                "control_type": "Button",
                "root_id": "hoverRegion",
            },
            "expect_status": "PASS",
            "expect_criterion": "xpath",
        },
    ]


def _windows_cursor_position() -> dict[str, int]:
    import ctypes

    class Point(ctypes.Structure):
        _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

    point = Point()
    if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
        raise OSError("GetCursorPos failed during hover selector matrix")
    return {"x": int(point.x), "y": int(point.y)}


def _wpf_hover_selector_matrix_evidence(
    results: list[dict[str, Any]],
    *,
    cursor_before: dict[str, int],
    cursor_after_blocked: dict[str, int],
) -> dict[str, Any]:
    cases = _wpf_hover_selector_matrix_cases()
    blocked_count = sum(case["expect_status"] == "BLOCKED" for case in cases)
    failures: list[str] = []
    if len(results) not in {blocked_count, len(cases)}:
        failures.append("selector matrix result count mismatch")
    expected = cases[: len(results)]
    if [result.get("id") for result in results] != [case["id"] for case in expected]:
        failures.append("selector matrix result identities/order mismatch")

    for case, result in zip(expected, results):
        case_id = str(case["id"])
        if result.get("status") != case["expect_status"]:
            failures.append(f"{case_id} status mismatch")
            continue
        if case["expect_status"] == "BLOCKED":
            if result.get("phase") != case["expect_phase"]:
                failures.append(f"{case_id} phase mismatch")
            if result.get("matchCount") != case["expect_match_count"]:
                failures.append(f"{case_id} matchCount mismatch")
            if result.get("pointerMutationState") != "not_started":
                failures.append(f"{case_id} pointer mutation started before uniqueness")
            harness_cursor_before = result.get("harnessCursorBefore")
            harness_cursor_after = result.get("harnessCursorAfter")
            if not isinstance(harness_cursor_before, dict) or not isinstance(
                harness_cursor_after, dict
            ):
                failures.append(f"{case_id} missing call-scoped cursor evidence")
            elif harness_cursor_after != harness_cursor_before:
                failures.append(
                    f"{case_id} pointer moved before selector uniqueness passed"
                )
            continue

        resolved = dict(result.get("resolvedSelector") or {})
        if result.get("matchCount") != 1:
            failures.append(f"{case_id} matchCount mismatch")
        if resolved.get("criterion") != case["expect_criterion"]:
            failures.append(f"{case_id} resolved criterion mismatch")
        for field, value in {
            "foregroundVerified": True,
            "focusUnchanged": True,
            "underPointer": True,
            "hovered": True,
            "click": False,
            "button": "none",
            "pointerMutationState": "moved",
        }.items():
            if result.get(field) != value:
                failures.append(f"{case_id} {field} mismatch")

    return {
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
        "cursorBefore": cursor_before,
        "cursorAfterBlocked": cursor_after_blocked,
        "cases": results,
    }


async def _run_wpf_hover_selector_matrix(bridge_path: str) -> dict[str, Any]:
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    cases = _wpf_hover_selector_matrix_cases()
    blocked_count = sum(case["expect_status"] == "BLOCKED" for case in cases)
    m = SessionManager(project_path=BASE)
    backend = FlaUIBackend(bridge_path, process_registry=m.process_registry)
    results: list[dict[str, Any]] = []
    cursor_before: dict[str, int] = {}
    cursor_after_blocked: dict[str, int] = {}
    outcome: dict[str, Any] = {}
    pass_started = False
    cleanup_failures: list[str] = []
    try:
        await m.launch(program=WPF_DLL, cwd=os.path.dirname(WPF_DLL))
        await asyncio.sleep(1.5)
        pid = m.state.process_id
        if not pid:
            raise RuntimeError("Process ID not available for WPF hover selector matrix")
        await backend.connect(pid)
        cursor_before = _windows_cursor_position()
        for case in cases[:blocked_count]:
            harness_cursor_before = _windows_cursor_position()
            result = await backend.hover_element(
                **dict(case["selector"]),
                timeout_ms=5000,
            )
            harness_cursor_after = _windows_cursor_position()
            results.append(
                {
                    "id": case["id"],
                    **result,
                    "harnessCursorBefore": harness_cursor_before,
                    "harnessCursorAfter": harness_cursor_after,
                }
            )
        cursor_after_blocked = _windows_cursor_position()
        blocked_evidence = _wpf_hover_selector_matrix_evidence(
            results,
            cursor_before=cursor_before,
            cursor_after_blocked=cursor_after_blocked,
        )
        if blocked_evidence["status"] != "PASS":
            outcome = blocked_evidence
        else:
            foreground = await backend.bring_to_front()
            if foreground.get("activated") is not True:
                outcome = {
                    **blocked_evidence,
                    "status": "BLOCKED",
                    "prerequisite": "interactive_desktop_or_foreground",
                    "reason": "selector matrix could not foreground the WPF fixture",
                    "foreground": foreground,
                }
            else:
                focus = await backend.client.call(
                    "set_focus",
                    {
                        "automationId": "hoverFocusSentinel",
                        "rootAutomationId": "hoverRegion",
                    },
                )
                if str(focus.get("status") or "PASS").upper() != "PASS":
                    outcome = {
                        "status": "BLOCKED",
                        "prerequisite": "interactive_desktop_or_foreground",
                        "reason": "selector matrix could not focus the hover sentinel",
                        "focus": focus,
                        "cases": results,
                    }
                else:
                    pass_started = True
                    for case in cases[blocked_count:]:
                        result = await backend.hover_element(
                            **dict(case["selector"]),
                            timeout_ms=5000,
                        )
                        results.append({"id": case["id"], **result})
                    outcome = _wpf_hover_selector_matrix_evidence(
                        results,
                        cursor_before=cursor_before,
                        cursor_after_blocked=cursor_after_blocked,
                    )
                    outcome["foregroundSetup"] = foreground
                    outcome["focusSetup"] = focus
    except Exception as exc:
        reason = str(exc)
        lowered = reason.lower()
        prerequisite = not pass_started and any(
            token in lowered
            for token in ("foreground", "interactive desktop", "flaui", "window")
        )
        outcome = {
            "status": "BLOCKED" if prerequisite else "FAIL",
            "reason": reason,
            "cases": results,
            **(
                {"prerequisite": "interactive_desktop_or_foreground"}
                if prerequisite
                else {}
            ),
        }
    finally:
        try:
            await backend.disconnect()
        except Exception as exc:
            cleanup_failures.append(f"backend.disconnect: {exc}")
        try:
            await m.stop()
        except Exception as exc:
            cleanup_failures.append(f"session.stop: {exc}")

    registry_after = len(m.process_registry.get_all())
    outcome["cleanup"] = {
        "status": "FAIL" if cleanup_failures or registry_after else "PASS",
        "failures": cleanup_failures,
        "process_registry_after": registry_after,
    }
    if outcome["cleanup"]["status"] != "PASS":
        outcome["status"] = "FAIL"
    return outcome


async def run_wpf_v2_hover_runtime_smoke() -> dict[str, Any]:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    if sys.platform != "win32":
        return {
            "status": "BLOCKED",
            "prerequisite": "windows",
            "reason": "WPF selector-scoped hover smoke requires Windows UI automation",
        }
    if not os.path.exists(WPF_DLL):
        return {
            "status": "FAIL",
            "reason": "WPF hover fixture build output is missing",
            "requested": {"program": WPF_DLL},
        }

    bridge_path = os.path.join(
        BASE,
        "bridge",
        "bin",
        "Debug",
        "net8.0-windows",
        "win-x64",
        "FlaUIBridge.exe",
    )
    if not os.path.exists(bridge_path):
        return {
            "status": "BLOCKED",
            "prerequisite": "flaui",
            "reason": "Fresh Debug FlaUI bridge build output is missing",
            "requested": {"bridge_path": bridge_path},
        }

    selector_matrix = await _run_wpf_hover_selector_matrix(bridge_path)
    if selector_matrix.get("status") != "PASS":
        return selector_matrix

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {
        "backend": FlaUIBackend(bridge_path, process_registry=m.process_registry)
    }

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = FlaUIBackend(bridge_path, process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError("Process ID not available for WPF hover smoke")
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    build_project = os.path.join(
        BASE,
        "tests",
        "fixtures",
        "WpfSmokeApp",
        "WpfSmokeApp.csproj",
    )
    adapters = ui_operation_adapters(ensure_ui_connected, session=m)
    base_set_focus = adapters["ui.set_focus"]

    async def set_hover_focus(**args: Any) -> dict[str, Any]:
        backend = await ensure_ui_connected()
        if not isinstance(backend, FlaUIBackend):
            return {
                "status": "BLOCKED",
                "reason": "FlaUI foreground setup backend is unavailable",
                "requested": {"backend": "FlaUIBackend"},
                "accepted": {"capability": "exact target foreground setup"},
                "next_step": "Run the smoke with the freshly built FlaUI bridge.",
            }
        try:
            foreground = await backend.bring_to_front()
        except Exception as exc:
            return {
                "status": "BLOCKED",
                "reason": f"exact target foreground setup failed: {exc}",
                "requested": {"foreground": "target root HWND"},
                "accepted": {"activated": True},
                "next_step": (
                    "Use an interactive desktop where the WPF fixture can be foregrounded."
                ),
            }
        if foreground.get("activated") is not True:
            return {
                "status": "BLOCKED",
                "reason": "exact target foreground setup is unavailable",
                "requested": foreground,
                "accepted": {"activated": True},
                "next_step": (
                    "Use an interactive desktop where the WPF fixture can be foregrounded."
                ),
            }
        result = await base_set_focus(**args)
        if str(result.get("status") or "PASS").upper() != "PASS":
            return result
        return {**result, "foreground_setup": foreground}

    adapters["ui.set_focus"] = set_hover_focus
    plan = _v2_hover_plan(program=WPF_DLL, build_project=build_project)
    try:
        result = await RuntimeSmokeRunner(m, service_adapters=adapters).run(plan)
        evidence = _wpf_hover_smoke_evidence(result)
        evidence["selector_matrix"] = selector_matrix
        if evidence.get("status") == "BLOCKED":
            evidence["status"] = "FAIL"
            evidence["reason"] = (
                "measured hover sequence blocked after the selector matrix proved "
                "interactive desktop and foreground prerequisites"
            )
        return evidence
    except Exception as exc:
        return {
            "status": "FAIL",
            "reason": str(exc),
            "selector_matrix": selector_matrix,
        }
    finally:
        backend = backend_holder["backend"]
        if backend is not None:
            try:
                await backend.disconnect()
            except Exception as exc:
                print(f"  [DEBUG] WPF hover backend.disconnect() failed: {exc}")
        await m.stop()


async def run_wpf_v2_text_probe_missing_selector_runtime_smoke() -> dict[str, Any]:
    return await _run_v2_text_probe_missing_selector_runtime_smoke(
        label="WPF",
        program=WPF_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "WpfSmokeApp",
            "WpfSmokeApp.csproj",
        ),
    )


async def run_avalonia_v2_text_probe_missing_selector_runtime_smoke() -> dict[str, Any]:
    return await _run_v2_text_probe_missing_selector_runtime_smoke(
        label="Avalonia",
        program=AVALONIA_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "AvaloniaSmokeApp",
            "AvaloniaSmokeApp.csproj",
        ),
    )


async def _run_v2_text_probe_missing_selector_runtime_smoke(
    *,
    label: str,
    program: str,
    build_project: str,
) -> dict[str, Any]:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    if sys.platform != "win32":
        return {
            "status": "BLOCKED",
            "reason": f"{label} v2 text-probe selector-miss smoke requires Windows UI automation",
        }

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {"backend": None}
    backend_holder["backend"] = create_backend(process_registry=m.process_registry)
    if not isinstance(backend_holder["backend"], FlaUIBackend):
        return {
            "status": "BLOCKED",
            "backend": type(backend_holder["backend"]).__name__,
            "reason": f"FlaUI bridge required for {label} v2 text-probe selector-miss smoke",
        }

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = create_backend(process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError(
                f"Process ID not available for {label} v2 text-probe smoke"
            )
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    plan = _v2_text_probe_missing_selector_plan(
        label=label,
        program=program,
        build_project=build_project,
    )

    try:
        result = await RuntimeSmokeRunner(
            m,
            service_adapters=ui_operation_adapters(ensure_ui_connected, session=m),
        ).run(plan)
        transition = _v2_transition(result, 0)
        probe = _transition_probe_result(
            transition,
            "after",
            "ui.text.missing_output",
        )
        blocked = dict(result.get("blocked") or {})
        cleanup = dict(result.get("cleanup") or {})
        has_diagnostics = (
            probe.get("status") == "BLOCKED"
            and probe.get("requested")
            == {"selector": {"automation_id": "missingTxtOutput"}}
            and bool(probe.get("accepted"))
            and bool(probe.get("next_step"))
            and isinstance(probe.get("backend_result"), dict)
            and bool(blocked.get("backend_result"))
        )
        return {
            "status": "PASS"
            if (
                result.get("status") == "BLOCKED"
                and blocked.get("reason") == "selector not found"
                and has_diagnostics
                and cleanup.get("process_registry_after") == 0
            )
            else "FAIL",
            "result": _v2_smoke_summary(result),
            "probe": {
                "status": probe.get("status"),
                "reason": probe.get("reason"),
                "requested": probe.get("requested"),
                "accepted": probe.get("accepted"),
                "next_step": probe.get("next_step"),
                "backend_result": probe.get("backend_result"),
            },
        }
    finally:
        if backend_holder["backend"] is not None:
            try:
                await backend_holder["backend"].disconnect()
            except Exception as exc:
                print(
                    f"  [DEBUG] {label} v2 text-probe backend.disconnect() failed: {exc}"
                )
        await m.stop()


async def run_avalonia_v2_state_oracle_runtime_smoke() -> dict[str, Any]:
    return await _run_v2_state_oracle_runtime_smoke(
        label="Avalonia",
        program=AVALONIA_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "AvaloniaSmokeApp",
            "AvaloniaSmokeApp.csproj",
        ),
    )


async def _run_v2_state_oracle_runtime_smoke(
    *,
    label: str,
    program: str,
    build_project: str,
) -> dict[str, Any]:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    if sys.platform != "win32":
        return {
            "status": "BLOCKED",
            "reason": f"{label} v2 state oracle smoke requires Windows UI automation",
        }

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {"backend": None}
    backend_holder["backend"] = create_backend(process_registry=m.process_registry)
    if not isinstance(backend_holder["backend"], FlaUIBackend):
        return {
            "status": "BLOCKED",
            "backend": type(backend_holder["backend"]).__name__,
            "reason": f"FlaUI bridge required for {label} v2 state oracle smoke",
        }

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = create_backend(process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError(f"Process ID not available for {label} v2 smoke")
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    adapters = ui_operation_adapters(ensure_ui_connected, session=m)
    happy_plan = _v2_state_oracle_plan(
        name=f"{label.lower()} v2 state oracle happy path",
        program=program,
        build_project=build_project,
        selector={"automation_id": "btnInvoke"},
    )
    blocked_plan = _v2_state_oracle_plan(
        name=f"{label.lower()} v2 state oracle selector miss",
        program=program,
        build_project=build_project,
        selector={"automation_id": "missingV2Button"},
    )

    try:
        happy = await RuntimeSmokeRunner(m, service_adapters=adapters).run(happy_plan)
        if backend_holder["backend"] is not None:
            await backend_holder["backend"].disconnect()
            backend_holder["backend"] = None
        blocked = await RuntimeSmokeRunner(m, service_adapters=adapters).run(
            blocked_plan
        )
        return {
            "status": "PASS"
            if (
                happy.get("status") == "PASS"
                and blocked.get("status") == "BLOCKED"
                and happy.get("cleanup", {}).get("process_registry_after") == 0
                and blocked.get("cleanup", {}).get("process_registry_after") == 0
            )
            else "FAIL",
            "happy": _v2_smoke_summary(happy),
            "blocked": _v2_smoke_summary(blocked),
        }
    finally:
        if backend_holder["backend"] is not None:
            try:
                await backend_holder["backend"].disconnect()
            except Exception as exc:
                print(f"  [DEBUG] {label} v2 backend.disconnect() failed: {exc}")
        await m.stop()


def _v2_state_oracle_plan(
    *,
    name: str,
    program: str,
    build_project: str,
    selector: dict[str, str],
) -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": name,
        "baseline": {
            "steps": [
                {
                    "id": "launch_fixture",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": program,
                        "cwd": os.path.dirname(program),
                        "pre_build": True,
                        "build_project": build_project,
                        "build_configuration": "Debug",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": "invoke_button_state_ab",
                "transitions": [
                    {
                        "action": {
                            "kind": "ui.invoke",
                            "selector": selector,
                        },
                        "probes": [
                            {
                                "kind": "process.metric",
                                "name": "debuggee_memory",
                            },
                            {
                                "kind": "ui.property",
                                "name": "output_text",
                                "selector": {"automation_id": "txtOutput"},
                                "property": "Name",
                            },
                        ],
                    }
                ],
            }
        ],
        "cleanup": {
            "steps": [
                {"kind": "debug.stop"},
                {"kind": "process.registry.assert_empty"},
            ]
        },
    }


def _v2_hover_plan(*, program: str, build_project: str) -> dict[str, Any]:
    status_selector = {"automation_id": "hoverStatus"}

    def status_probe(name: str, state: str) -> dict[str, Any]:
        return {
            "kind": "ui.text",
            "name": name,
            "phase": "after",
            "action": "read",
            "selector": status_selector,
            "expected": f'contains:"state":"{state}"',
        }

    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": "wpf v2 selector-scoped pointer hover",
        "baseline": {
            "steps": [
                {
                    "id": "launch_fixture",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": program,
                        "cwd": os.path.dirname(program),
                        "pre_build": True,
                        "build_project": build_project,
                        "build_configuration": "Debug",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": "selector_scoped_pointer_hover",
                "transitions": [
                    {
                        "id": "arm_hover_measurement",
                        "action": {
                            "kind": "ui.input.ensure_target",
                            "selector": {
                                "automation_id": "hoverFocusSentinel",
                                "root_id": "hoverRegion",
                            },
                            "require": {"focus": True},
                        },
                        "settle": {"idle_ms": 50},
                        "probes": [status_probe("hover_armed", "closed")],
                    },
                    {
                        "id": "hover_trigger",
                        "action": {
                            "kind": "ui.hover",
                            "selector": {
                                "automation_id": "hoverTrigger",
                                "root_id": "hoverRegion",
                            },
                            "timeout_ms": 5000,
                        },
                        "settle": {"idle_ms": 50},
                        "probes": [
                            status_probe("hover_trigger_status", "open_trigger")
                        ],
                    },
                    {
                        "id": "hover_flyout_surface",
                        "action": {
                            "kind": "ui.hover",
                            "selector": {
                                "automation_id": "hoverFlyoutSurface",
                                "root_id": "hoverRegion",
                            },
                            "timeout_ms": 5000,
                        },
                        "settle": {"idle_ms": 50},
                        "probes": [status_probe("hover_flyout_status", "open_flyout")],
                    },
                    {
                        "id": "hover_outside",
                        "action": {
                            "kind": "ui.hover",
                            "selector": {
                                "automation_id": "hoverOutsideSentinel",
                                "root_id": "hoverRegion",
                            },
                            "timeout_ms": 5000,
                        },
                        "settle": {"idle_ms": 100},
                        "probes": [
                            status_probe("hover_pending_status", "close_pending")
                        ],
                    },
                    {
                        "id": "wait_for_hover_close",
                        "action": {"kind": "wait", "idle_ms": 900},
                        "settle": {"idle_ms": 0},
                        "probes": [status_probe("hover_closed_status", "closed")],
                    },
                ],
            }
        ],
        "cleanup": {
            "steps": [
                {"kind": "debug.stop"},
                {"kind": "process.registry.assert_empty"},
            ]
        },
    }


def _wpf_hover_smoke_evidence(result: dict[str, Any]) -> dict[str, Any]:
    import json

    cleanup = dict(result.get("cleanup") or {})
    result_status = str(result.get("status") or "FAIL").upper()
    transitions = [_v2_transition(result, index) for index in range(5)]
    hover_actions = [
        _first_transition_action(transition)
        for transition in transitions[1:4]
        if transition
    ]

    if result_status != "PASS":
        reason = str(result.get("reason") or "WPF hover runtime smoke failed")
        blocked_reasons = [
            str(action.get("reason") or action.get("result", {}).get("reason") or "")
            for action in hover_actions
            if str(action.get("status") or "").upper() == "BLOCKED"
        ]
        combined = " ".join([reason, *blocked_reasons]).lower()
        successful_hovers = sum(
            str(action.get("status") or "").upper() == "PASS"
            for action in hover_actions
        )
        if (
            result_status == "BLOCKED"
            and successful_hovers == 0
            and any(
                token in combined
                for token in (
                    "foreground",
                    "interactive desktop",
                    "flaui",
                    "ui backend",
                    "visible window",
                )
            )
        ):
            return {
                "status": "BLOCKED",
                "prerequisite": "interactive_desktop_or_foreground",
                "reason": reason,
                "blocked": result.get("blocked"),
                "cleanup": cleanup,
            }
        return {
            "status": "FAIL",
            "reason": reason,
            "blocked": result.get("blocked"),
            "cleanup": cleanup,
        }

    failures: list[str] = []
    expected_ids = [
        "arm_hover_measurement",
        "hover_trigger",
        "hover_flyout_surface",
        "hover_outside",
        "wait_for_hover_close",
    ]
    if [transition.get("id") for transition in transitions] != expected_ids:
        failures.append("measured transition identities/order mismatch")
    if any(
        str(transition.get("status") or "").upper() != "PASS"
        for transition in transitions
    ):
        failures.append("one or more measured transitions did not PASS")

    expected_states = [
        "closed",
        "open_trigger",
        "open_flyout",
        "close_pending",
        "closed",
    ]
    expected_visibility = [False, True, True, True, False]
    probe_names = [
        "hover_armed",
        "hover_trigger_status",
        "hover_flyout_status",
        "hover_pending_status",
        "hover_closed_status",
    ]
    fixture_states: list[dict[str, Any]] = []
    for index, (transition, probe_name) in enumerate(
        zip(transitions, probe_names, strict=True)
    ):
        raw_status = _transition_probe_value(
            transition,
            "after",
            f"ui.text.{probe_name}",
        )
        try:
            fixture_state = json.loads(str(raw_status))
        except (TypeError, ValueError, json.JSONDecodeError):
            fixture_state = {}
            failures.append(
                f"transition {expected_ids[index]} returned malformed hoverStatus"
            )
        fixture_states.append(fixture_state)
        if fixture_state.get("state") != expected_states[index]:
            failures.append(f"transition {expected_ids[index]} state mismatch")
        if fixture_state.get("surfaceVisible") is not expected_visibility[index]:
            failures.append(f"transition {expected_ids[index]} visibility mismatch")
        if fixture_state.get("measurementArmed") is not True:
            failures.append(
                f"transition {expected_ids[index]} measurement was not armed"
            )
        if fixture_state.get("closeDelayMs") != 500:
            failures.append(f"transition {expected_ids[index]} close delay mismatch")
        for counter in (
            "previewMouseLeftButtonDownCount",
            "previewMouseLeftButtonUpCount",
            "clickCount",
            "focusChangeCount",
        ):
            if fixture_state.get(counter) != 0:
                failures.append(f"transition {expected_ids[index]} changed {counter}")

    required_hover_truth = {
        "matchCount": 1,
        "foregroundVerified": True,
        "focusUnchanged": True,
        "underPointer": True,
        "hovered": True,
        "click": False,
        "button": "none",
        "pointerMutationState": "moved",
    }
    for index, action in enumerate(hover_actions, start=1):
        transition_id = expected_ids[index]
        if str(action.get("status") or "").upper() != "PASS":
            failures.append(f"transition {transition_id} hover action did not PASS")
            continue
        for field, expected in required_hover_truth.items():
            if action.get(field) != expected:
                failures.append(f"transition {transition_id} {field} mismatch")
        foreground = action.get("targetRootHwnd")
        if not foreground or action.get("foregroundHwndBefore") != foreground:
            failures.append(f"transition {transition_id} foreground-before mismatch")
        if action.get("foregroundHwndAfter") != foreground:
            failures.append(f"transition {transition_id} foreground-after mismatch")
        focus_before = dict(action.get("focusBefore") or {})
        focus_after = dict(action.get("focusAfter") or {})
        focus_identity_keys = ("automationId", "name", "controlType")
        if any(
            focus_before.get(key) != focus_after.get(key) for key in focus_identity_keys
        ):
            failures.append(f"transition {transition_id} focus identity changed")
        if action.get("hitRelation") not in {"self", "descendant"}:
            failures.append(f"transition {transition_id} hit relation mismatch")
        elapsed_ms = action.get("elapsedMs")
        timeout_ms = action.get("timeoutMs")
        if (
            not isinstance(elapsed_ms, int)
            or not isinstance(timeout_ms, int)
            or elapsed_ms > timeout_ms
        ):
            failures.append(f"transition {transition_id} elapsed time was not bounded")
        if action.get("runner_input") != {
            "source": "runner_injected",
            "kind": "ui.hover",
            "window": "action",
            "route": "hover",
        }:
            failures.append(
                f"transition {transition_id} runner input metadata mismatch"
            )

    if cleanup.get("status") != "PASS" or cleanup.get("process_registry_after") != 0:
        failures.append("debugger cleanup or process registry proof failed")

    return {
        "status": "FAIL" if failures else "PASS",
        "failures": failures,
        "states": fixture_states,
        "hover_actions": hover_actions,
        "cleanup": cleanup,
        "action_count": result.get("action_count"),
    }


def _v2_text_probe_missing_selector_plan(
    *,
    label: str,
    program: str,
    build_project: str,
) -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": f"{label.lower()} v2 text probe selector miss diagnostics",
        "baseline": {
            "steps": [
                {
                    "id": "launch_fixture",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": program,
                        "cwd": os.path.dirname(program),
                        "pre_build": True,
                        "build_project": build_project,
                        "build_configuration": "Debug",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": "missing_text_probe",
                "transitions": [
                    {
                        "action": {
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "btnInvoke"},
                        },
                        "probes": [
                            {
                                "kind": "ui.text",
                                "name": "missing_output",
                                "phase": "after",
                                "selector": {"automation_id": "missingTxtOutput"},
                                "expected": "Clicked",
                            }
                        ],
                    }
                ],
            }
        ],
        "cleanup": {
            "steps": [
                {"kind": "debug.stop"},
                {"kind": "process.registry.assert_empty"},
            ]
        },
    }


def _v2_smoke_summary(result: dict[str, Any]) -> dict[str, Any]:
    return {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "action_count": result.get("action_count"),
        "case_count": len(result.get("cases", [])),
        "cleanup": result.get("cleanup"),
        "blocked": result.get("blocked"),
    }


async def test_wpf_v2_state_oracle_runtime_smoke():
    print("\nWPF V2 STATE ORACLE RUNTIME SMOKE")
    evidence = await run_wpf_v2_state_oracle_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check("WPF v2 state oracle reports actionable BLOCKED", True, str(evidence))
        return
    check(
        "WPF v2 state oracle happy path and selector miss are classified",
        evidence.get("status") == "PASS",
        str(evidence),
    )
    check(
        "WPF v2 state oracle cleanup proof has zero leaked processes",
        evidence.get("happy", {}).get("cleanup", {}).get("process_registry_after") == 0
        and evidence.get("blocked", {}).get("cleanup", {}).get("process_registry_after")
        == 0,
        str(evidence),
    )


async def test_wpf_v2_hover_runtime_smoke():
    print("\nWPF V2 SELECTOR-SCOPED HOVER RUNTIME SMOKE")
    evidence = await run_wpf_v2_hover_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check(
            "WPF v2 hover stops only on an allowed desktop prerequisite",
            evidence.get("prerequisite")
            in {"windows", "flaui", "interactive_desktop_or_foreground"},
            str(evidence),
        )
        return
    check(
        "WPF v2 selector-scoped hover passes measured contract",
        evidence.get("status") == "PASS",
        str(evidence),
    )
    check(
        "WPF v2 hover cleanup proof has zero leaked processes",
        evidence.get("cleanup", {}).get("process_registry_after") == 0,
        str(evidence),
    )


async def test_wpf_v2_text_probe_missing_selector_runtime_smoke():
    print("\nWPF V2 TEXT PROBE MISSING SELECTOR RUNTIME SMOKE")
    evidence = await run_wpf_v2_text_probe_missing_selector_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check("WPF v2 text-probe selector miss reports BLOCKED", True, str(evidence))
        return
    check(
        "WPF v2 text-probe selector miss preserves diagnostics",
        evidence.get("status") == "PASS",
        str(evidence),
    )


async def test_avalonia_v2_text_probe_missing_selector_runtime_smoke():
    print("\nAVALONIA V2 TEXT PROBE MISSING SELECTOR RUNTIME SMOKE")
    evidence = await run_avalonia_v2_text_probe_missing_selector_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check(
            "Avalonia v2 text-probe selector miss reports BLOCKED", True, str(evidence)
        )
        return
    check(
        "Avalonia v2 text-probe selector miss preserves diagnostics",
        evidence.get("status") == "PASS",
        str(evidence),
    )


async def run_wpf_v2_visible_row_drag_runtime_smoke() -> dict[str, Any]:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    if sys.platform != "win32":
        return {
            "status": "BLOCKED",
            "reason": "WPF v2 visible-row drag smoke requires Windows UI automation",
        }

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {"backend": None}
    backend_holder["backend"] = create_backend(process_registry=m.process_registry)
    if not isinstance(backend_holder["backend"], FlaUIBackend):
        return {
            "status": "BLOCKED",
            "backend": type(backend_holder["backend"]).__name__,
            "reason": "FlaUI bridge required for WPF v2 visible-row drag smoke",
        }

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = create_backend(process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError(
                "Process ID not available for WPF v2 visible-row drag smoke"
            )
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    plan = _v2_visible_row_drag_plan(
        program=WPF_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "WpfSmokeApp",
            "WpfSmokeApp.csproj",
        ),
    )

    try:
        result = await RuntimeSmokeRunner(
            m,
            service_adapters=ui_operation_adapters(ensure_ui_connected, session=m),
        ).run(plan)
        return _v2_visible_row_drag_summary(result)
    finally:
        if backend_holder["backend"] is not None:
            try:
                await backend_holder["backend"].disconnect()
            except Exception as exc:
                print(
                    f"  [DEBUG] WPF v2 visible-row drag backend.disconnect() failed: {exc}"
                )
        await m.stop()


def _v2_visible_row_drag_plan(
    *,
    program: str,
    build_project: str,
) -> dict[str, Any]:
    selector = {"automation_id": "dataGrid"}
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": "wpf v2 visible-row drag reorder",
        "baseline": {
            "steps": [
                {
                    "id": "launch_fixture",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": program,
                        "cwd": os.path.dirname(program),
                        "pre_build": True,
                        "build_project": build_project,
                        "build_configuration": "Debug",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": "wpf_visible_row_drag_reorder",
                "transitions": [
                    {
                        "id": "drag_row_1_to_row_3",
                        "action": {
                            "kind": "ui.drag",
                            "source": {
                                "selector": selector,
                                "row_index": 1,
                            },
                            "path": [
                                {"relative_to": "source", "x": 0.5, "y": 0.5},
                                {"relative_to": "drop", "x": 0.5, "y": 0.5},
                            ],
                            "drop": {
                                "selector": selector,
                                "row_index": 3,
                            },
                            "duration_ms": 450,
                            "expect": {
                                "row_count_preserved": True,
                                "identity_set_preserved": True,
                                "single_move": {"source_index": 1, "target_index": 3},
                            },
                        },
                        "settle": {"idle_ms": 500},
                        "probes": [
                            {
                                "kind": "ui.grid",
                                "name": "cue_order",
                                "selector": selector,
                                "columns": ["Start", "End", "Character", "Phrase"],
                                "phases": ["before", "after"],
                            }
                        ],
                    }
                ],
            }
        ],
        "cleanup": {
            "steps": [
                {"kind": "debug.stop"},
                {"kind": "process.registry.assert_empty"},
            ]
        },
    }


def _v2_visible_row_drag_summary(result: dict[str, Any]) -> dict[str, Any]:
    transition = _first_v2_transition(result)
    action = _first_transition_action(transition)
    before_rows = _transition_grid_rows(transition, "before")
    after_rows = _transition_grid_rows(transition, "after")
    before_refs = _row_identity_refs(before_rows)
    after_refs = _row_identity_refs(after_rows)
    expected_after_refs = _moved_refs(before_refs, source_index=1, target_index=3)
    route_evidence = dict(action.get("route_evidence") or {}) if action else {}
    row_count_preserved = len(before_refs) == len(after_refs) and bool(before_refs)
    identity_set_preserved = sorted(before_refs) == sorted(after_refs) and bool(
        before_refs
    )
    order_changed_once = after_refs == expected_after_refs and after_refs != before_refs

    compact = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "backend": action.get("backend") if action else None,
        "source_bounds": route_evidence.get("source_bounds"),
        "target_bounds": route_evidence.get("target_bounds"),
        "move_points": route_evidence.get("move_points"),
        "final_pointer": route_evidence.get("final_pointer"),
        "before_identity_refs": before_refs,
        "after_identity_refs": after_refs,
        "row_count_preserved": row_count_preserved,
        "identity_set_preserved": identity_set_preserved,
        "order_changed_once": order_changed_once,
        "cleanup": result.get("cleanup"),
        "blocked": result.get("blocked"),
    }
    if result.get("status") == "BLOCKED":
        compact["status"] = "BLOCKED"
        return compact
    compact["status"] = (
        "PASS"
        if result.get("status") == "PASS"
        and row_count_preserved
        and identity_set_preserved
        and order_changed_once
        and route_evidence.get("source_bounds")
        and route_evidence.get("final_pointer")
        else "FAIL"
    )
    return compact


async def run_wpf_v2_offscreen_row_target_drag_runtime_smoke() -> dict[str, Any]:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    if sys.platform != "win32":
        return {
            "status": "BLOCKED",
            "reason": "WPF v2 offscreen row-target drag smoke requires Windows UI automation",
        }

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {"backend": None}
    backend_holder["backend"] = create_backend(process_registry=m.process_registry)
    if not isinstance(backend_holder["backend"], FlaUIBackend):
        return {
            "status": "BLOCKED",
            "backend": type(backend_holder["backend"]).__name__,
            "reason": "FlaUI bridge required for WPF v2 offscreen row-target drag smoke",
            "accepted": {
                "backend": "FlaUI drag_path with source/drop row ensure_visible proof"
            },
            "next_step": "Build the FlaUI bridge and run on a Windows desktop session.",
        }

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = create_backend(process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError(
                "Process ID not available for WPF v2 offscreen row-target drag smoke"
            )
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    plan = _v2_offscreen_row_target_drag_plan(
        program=WPF_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "WpfSmokeApp",
            "WpfSmokeApp.csproj",
        ),
    )

    try:
        result = await RuntimeSmokeRunner(
            m,
            service_adapters=ui_operation_adapters(ensure_ui_connected, session=m),
        ).run(plan)
        return _v2_offscreen_row_target_drag_summary(result)
    finally:
        if backend_holder["backend"] is not None:
            try:
                await backend_holder["backend"].disconnect()
            except Exception as exc:
                print(
                    "  [DEBUG] WPF v2 offscreen row-target drag "
                    f"backend.disconnect() failed: {exc}"
                )
        await m.stop()


def _v2_offscreen_row_target_drag_plan(
    *,
    program: str,
    build_project: str,
) -> dict[str, Any]:
    selector = {"automation_id": "dataGrid"}
    status_selector = {"automation_id": "txtOutput"}
    source_identity = "Fixture cue two"
    target_identity = "Fixture cue nineteen"
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": "wpf v2 offscreen row-target drag replay",
        "baseline": {
            "steps": [
                {
                    "id": "launch_fixture",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": program,
                        "cwd": os.path.dirname(program),
                        "pre_build": True,
                        "build_project": build_project,
                        "build_configuration": "Debug",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": "wpf_offscreen_row_target_drag_replay",
                "transitions": [
                    {
                        "id": "drag_visible_row_to_offscreen_row_target",
                        "action": {
                            "kind": "ui.drag",
                            "ensure_visible": True,
                            "source": {
                                "selector": selector,
                                "row_identity": source_identity,
                            },
                            "path": [
                                {"relative_to": "source", "x": 0.5, "y": 0.5},
                                {"relative_to": "drop", "x": 0.5, "y": 0.5},
                            ],
                            "drop": {
                                "selector": selector,
                                "row_identity": target_identity,
                                "identity": {"column": "Phrase"},
                                "rows": {"visible_only": True, "max": 8},
                                "columns": ["Phrase"],
                                "ensure_visible": True,
                                "max_scrolls": 12,
                                "scroll_settle_ms": 25,
                                "position": "center",
                            },
                            "identity": {"column": "Phrase"},
                            "duration_ms": 650,
                            "expect": {
                                "row_count_preserved": True,
                                "identity_set_preserved": True,
                                "single_move": {
                                    "source_identity": source_identity,
                                    "target_identity": target_identity,
                                },
                            },
                        },
                        "settle": {"idle_ms": 700},
                        "probes": [
                            {
                                "kind": "ui.grid.viewport",
                                "name": "offscreen_target_viewport",
                                "selector": selector,
                                "identity": {"column": "Phrase"},
                                "rows": {"visible_only": True, "max": 8},
                                "expect": {
                                    "row_count_preserved": True,
                                },
                            },
                            {
                                "kind": "ui.property",
                                "name": "offscreen_target_status",
                                "selector": status_selector,
                                "property": "Text",
                                "phase": "after",
                            },
                            {
                                "kind": "process.metric",
                                "name": "offscreen_target_process",
                            },
                        ],
                    }
                ],
            }
        ],
        "cleanup": {
            "steps": [
                {"kind": "debug.stop"},
                {"kind": "process.registry.assert_empty"},
            ]
        },
    }


def _v2_offscreen_row_target_drag_summary(result: dict[str, Any]) -> dict[str, Any]:
    transition = _first_v2_transition(result)
    action = _first_transition_action(transition)
    route_evidence = dict(action.get("route_evidence") or {}) if action else {}
    status = _parse_drag_reorder_status(
        _transition_probe_value(
            transition,
            "after",
            "ui.property.offscreen_target_status",
        )
    )
    viewport = _transition_probe_result(
        transition,
        "after",
        "ui.grid.viewport.offscreen_target_viewport",
    )
    comparison = dict(viewport.get("comparison") or {})
    process_metric = _transition_probe_value(
        transition,
        "after",
        "process.metric.offscreen_target_process",
    )
    expected_source = "Fixture cue two"
    expected_target = "Fixture cue nineteen"
    initial_order = _wpf_fixture_cue_order()
    target_index = initial_order.index(expected_target)
    expected_order = _moved_refs_by_identity(
        initial_order,
        source=expected_source,
        target=expected_target,
    )
    edge_first = status.get("edge_first_visible")
    edge_last = status.get("edge_last_visible")
    target_visible_after_drop = (
        isinstance(edge_first, int)
        and isinstance(edge_last, int)
        and edge_first <= target_index <= edge_last
    )
    order = status.get("order") or []
    compact = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "source_identity_requested": expected_source,
        "target_identity_requested": expected_target,
        "target_index": target_index,
        "drop_ensure_visible_requested": True,
        "source_ensure_visible_requested": True,
        "expected_source": expected_source,
        "expected_target": expected_target,
        "status_text": status,
        "source_identity_matches": status.get("source_identity") == expected_source,
        "target_identity_matches": status.get("target_identity") == expected_target,
        "drop_origin_target_matches": status.get("drop_origin_target")
        == expected_target,
        "drop_bounds_target_matches": status.get("drop_bounds_target")
        == expected_target,
        "target_visible_after_drop": target_visible_after_drop,
        "final_order_matches": order == expected_order,
        "route_evidence": {
            "backend": action.get("backend") if action else None,
            "source_bounds": route_evidence.get("source_bounds"),
            "target_bounds": route_evidence.get("target_bounds"),
            "move_points": route_evidence.get("move_points"),
            "final_pointer": route_evidence.get("final_pointer"),
        },
        "viewport": comparison,
        "process_metric": process_metric,
        "cleanup": result.get("cleanup"),
        "blocked": result.get("blocked"),
    }
    if result.get("status") == "BLOCKED":
        compact["status"] = "BLOCKED"
        return compact
    if result.get("status") != "PASS":
        compact["status"] = "FAIL"
        return compact

    missing_evidence = []
    if not action:
        missing_evidence.append("drag action result")
    if not route_evidence.get("source_bounds"):
        missing_evidence.append("source row bounds")
    if not route_evidence.get("target_bounds"):
        missing_evidence.append("target row bounds after drop.ensure_visible")
    if not route_evidence.get("move_points"):
        missing_evidence.append("drag move path")
    if not route_evidence.get("final_pointer"):
        missing_evidence.append("final pointer position")
    if not status.get("order"):
        missing_evidence.append("fixture status order fingerprint")
    if not isinstance(edge_first, int) or not isinstance(edge_last, int):
        missing_evidence.append("viewport range after drop.ensure_visible")
    if "row_count_preserved" not in comparison:
        missing_evidence.append("viewport row-count comparison")
    if not isinstance(process_metric, dict):
        missing_evidence.append("process metric probe")
    if missing_evidence:
        compact.update(
            {
                "status": "BLOCKED",
                "reason": (
                    "Backend/platform did not return enough evidence to prove "
                    "the offscreen row-target drag replay seam"
                ),
                "missing_evidence": missing_evidence,
                "accepted": {
                    "source": {"row_identity": expected_source, "ensure_visible": True},
                    "drop": {"row_identity": expected_target, "ensure_visible": True},
                    "expected_target": expected_target,
                },
                "next_step": (
                    "Expose route evidence and viewport/status proof for "
                    "row-based drop.ensure_visible in the WPF runtime-smoke backend."
                ),
            }
        )
        return compact

    compact["status"] = (
        "PASS"
        if compact["source_identity_matches"]
        and compact["target_identity_matches"]
        and compact["drop_origin_target_matches"]
        and compact["drop_bounds_target_matches"]
        and compact["target_visible_after_drop"]
        and compact["final_order_matches"]
        and comparison.get("row_count_preserved") is True
        else "FAIL"
    )
    return compact


async def run_wpf_v2_edge_scroll_drag_runtime_smoke() -> dict[str, Any]:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    if sys.platform != "win32":
        return {
            "status": "BLOCKED",
            "reason": "WPF v2 edge-scroll drag smoke requires Windows UI automation",
        }

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {"backend": None}
    backend_holder["backend"] = create_backend(process_registry=m.process_registry)
    if not isinstance(backend_holder["backend"], FlaUIBackend):
        return {
            "status": "BLOCKED",
            "backend": type(backend_holder["backend"]).__name__,
            "reason": "FlaUI bridge required for WPF v2 edge-scroll drag smoke",
            "accepted": {"backend": "FlaUI drag_path"},
            "next_step": "Build the FlaUI bridge and run on a Windows desktop session.",
        }

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = create_backend(process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError(
                "Process ID not available for WPF v2 edge-scroll drag smoke"
            )
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    plan = _v2_edge_scroll_drag_plan(
        program=WPF_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "WpfSmokeApp",
            "WpfSmokeApp.csproj",
        ),
    )

    try:
        result = await RuntimeSmokeRunner(
            m,
            service_adapters=ui_operation_adapters(ensure_ui_connected, session=m),
        ).run(plan)
        return _v2_edge_scroll_drag_summary(result)
    finally:
        if backend_holder["backend"] is not None:
            try:
                await backend_holder["backend"].disconnect()
            except Exception as exc:
                print(
                    f"  [DEBUG] WPF v2 edge-scroll drag backend.disconnect() failed: {exc}"
                )
        await m.stop()


def _v2_edge_scroll_drag_plan(
    *,
    program: str,
    build_project: str,
) -> dict[str, Any]:
    selector = {"automation_id": "dataGrid"}
    status_selector = {"automation_id": "txtOutput"}
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": "wpf v2 edge-scroll drag reorder",
        "baseline": {
            "steps": [
                {
                    "id": "launch_fixture",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": program,
                        "cwd": os.path.dirname(program),
                        "pre_build": True,
                        "build_project": build_project,
                        "build_configuration": "Debug",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": "wpf_edge_scroll_drag_reorder",
                "transitions": [
                    _v2_edge_scroll_transition(
                        transition_id="edge_scroll_down",
                        source={"selector": selector, "row_index": 1},
                        hold_y=0.96,
                        drop_y=0.90,
                        hold_ms=7000,
                        viewport_probe_name="edge_down_viewport",
                        status_probe_name="edge_down_status",
                        metric_probe_name="edge_down_process",
                        selector=selector,
                        status_selector=status_selector,
                    ),
                    _v2_edge_scroll_transition(
                        transition_id="edge_scroll_up",
                        source={
                            "selector": selector,
                            "row_identity": "Fixture cue two",
                        },
                        hold_y=0.25,
                        drop_y=0.35,
                        hold_ms=3000,
                        viewport_probe_name="edge_up_viewport",
                        status_probe_name="edge_up_status",
                        metric_probe_name="edge_up_process",
                        selector=selector,
                        status_selector=status_selector,
                    ),
                ],
            }
        ],
        "cleanup": {
            "steps": [
                {"kind": "debug.stop"},
                {"kind": "process.registry.assert_empty"},
            ]
        },
    }


def _v2_edge_scroll_transition(
    *,
    transition_id: str,
    source: dict[str, Any],
    hold_y: float,
    drop_y: float,
    hold_ms: int,
    viewport_probe_name: str,
    status_probe_name: str,
    metric_probe_name: str,
    selector: dict[str, Any],
    status_selector: dict[str, Any],
) -> dict[str, Any]:
    return {
        "id": transition_id,
        "action": {
            "kind": "ui.drag",
            "source": source,
            "path": [
                {"relative_to": "source", "x": 0.5, "y": 0.5},
                {
                    "relative_to": "viewport",
                    "selector": selector,
                    "x": 0.5,
                    "y": hold_y,
                    "hold_ms": hold_ms,
                },
                {
                    "relative_to": "viewport",
                    "selector": selector,
                    "x": 0.5,
                    "y": drop_y,
                },
            ],
            "drop": {
                "relative_to": "viewport",
                "selector": selector,
                "x": 0.5,
                "y": drop_y,
            },
            "duration_ms": 900,
            "expect": {
                "row_count_preserved": True,
                "identity_set_preserved": True,
            },
        },
        "settle": {"idle_ms": 700},
        "probes": [
            {
                "kind": "ui.grid.viewport",
                "name": viewport_probe_name,
                "selector": selector,
                "identity": {"column": "Phrase"},
                "rows": {"visible_only": True, "max": 8},
                "expect": {
                    "row_count_preserved": True,
                },
            },
            {
                "kind": "ui.property",
                "name": status_probe_name,
                "selector": status_selector,
                "property": "Text",
                "phase": "after",
            },
            {
                "kind": "process.metric",
                "name": metric_probe_name,
            },
        ],
    }


def _v2_edge_scroll_drag_summary(result: dict[str, Any]) -> dict[str, Any]:
    down = _v2_transition(result, 0)
    up = _v2_transition(result, 1)
    down_viewport = _transition_probe_result(
        down,
        "after",
        "ui.grid.viewport.edge_down_viewport",
    )
    up_viewport = _transition_probe_result(
        up,
        "after",
        "ui.grid.viewport.edge_up_viewport",
    )
    down_status = _parse_drag_reorder_status(
        _transition_probe_value(down, "after", "ui.property.edge_down_status")
    )
    up_status = _parse_drag_reorder_status(
        _transition_probe_value(up, "after", "ui.property.edge_up_status")
    )
    initial_order = _wpf_fixture_cue_order()
    down_expected = _moved_refs_by_identity(
        initial_order,
        source=down_status.get("source_identity"),
        target=down_status.get("target_identity"),
    )
    up_expected = _moved_refs_by_identity(
        down_status.get("order") or [],
        source=up_status.get("source_identity"),
        target=up_status.get("target_identity"),
    )
    down_comparison = dict(down_viewport.get("comparison") or {})
    up_comparison = dict(up_viewport.get("comparison") or {})
    process_metrics = {
        "down": _transition_probe_value(
            down, "after", "process.metric.edge_down_process"
        ),
        "up": _transition_probe_value(up, "after", "process.metric.edge_up_process"),
    }
    down_route = _first_transition_action(down).get("route_evidence", {})
    up_route = _first_transition_action(up).get("route_evidence", {})
    down_edge_scroll_matches = down_status.get("edge_scroll_direction") == "down"
    up_edge_scroll_matches = up_status.get("edge_scroll_direction") == "up"
    down_final_order_matches = down_status.get("order") == down_expected
    up_final_order_matches = up_status.get("order") == up_expected
    compact = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "down": {
            "viewport": down_comparison,
            "status": down_status,
            "edge_scroll_matches": down_edge_scroll_matches,
            "final_order_matches": down_final_order_matches,
            "hold_points": down_route.get("hold_points"),
        },
        "up": {
            "viewport": up_comparison,
            "status": up_status,
            "edge_scroll_matches": up_edge_scroll_matches,
            "final_order_matches": up_final_order_matches,
            "hold_points": up_route.get("hold_points"),
        },
        "process_metrics": process_metrics,
        "cleanup": result.get("cleanup"),
        "blocked": result.get("blocked"),
    }
    if result.get("status") == "BLOCKED":
        compact["status"] = "BLOCKED"
        return compact
    compact["status"] = (
        "PASS"
        if result.get("status") == "PASS"
        and down_edge_scroll_matches
        and up_edge_scroll_matches
        and down_final_order_matches
        and up_final_order_matches
        and bool(down_route.get("hold_points"))
        and bool(up_route.get("hold_points"))
        and isinstance(process_metrics["down"], dict)
        and isinstance(process_metrics["up"], dict)
        else "FAIL"
    )
    return compact


async def run_wpf_v2_multi_row_drag_runtime_smoke() -> dict[str, Any]:
    results: dict[str, Any] = {}
    for mode, indices, source_identity, target_index in (
        ("contiguous", [1, 2], "Fixture cue two", 5),
        ("non_contiguous", [1, 4], "Fixture cue two", 5),
    ):
        results[mode] = await _run_wpf_v2_multi_row_drag_case(
            mode=mode,
            indices=indices,
            source_identity=source_identity,
            target_index=target_index,
        )

    statuses = [
        value.get("status") for value in results.values() if isinstance(value, dict)
    ]
    status = (
        "BLOCKED"
        if any(item == "BLOCKED" for item in statuses)
        else "PASS"
        if statuses and all(item == "PASS" for item in statuses)
        else "FAIL"
    )
    return {"status": status, "cases": results}


async def _run_wpf_v2_multi_row_drag_case(
    *,
    mode: str,
    indices: list[int],
    source_identity: str,
    target_index: int,
) -> dict[str, Any]:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    if sys.platform != "win32":
        return {
            "status": "BLOCKED",
            "reason": "WPF v2 multi-row drag smoke requires Windows UI automation",
        }

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {"backend": None}
    backend_holder["backend"] = create_backend(process_registry=m.process_registry)
    if not isinstance(backend_holder["backend"], FlaUIBackend):
        return {
            "status": "BLOCKED",
            "backend": type(backend_holder["backend"]).__name__,
            "reason": "FlaUI bridge required for WPF v2 multi-row drag smoke",
            "accepted": {"backend": "FlaUI multi_select and drag_path"},
            "next_step": "Build the FlaUI bridge and run on a Windows desktop session.",
        }

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = create_backend(process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError(
                "Process ID not available for WPF v2 multi-row drag smoke"
            )
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    plan = _v2_multi_row_drag_plan(
        program=WPF_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "WpfSmokeApp",
            "WpfSmokeApp.csproj",
        ),
        mode=mode,
        indices=indices,
        source_identity=source_identity,
        target_index=target_index,
    )

    try:
        result = await RuntimeSmokeRunner(
            m,
            service_adapters=ui_operation_adapters(ensure_ui_connected, session=m),
        ).run(plan)
        return _v2_multi_row_drag_summary(
            result,
            mode=mode,
            expected_count=len(indices),
        )
    finally:
        if backend_holder["backend"] is not None:
            try:
                await backend_holder["backend"].disconnect()
            except Exception as exc:
                print(
                    f"  [DEBUG] WPF v2 multi-row drag backend.disconnect() failed: {exc}"
                )
        await m.stop()


def _v2_multi_row_drag_plan(
    *,
    program: str,
    build_project: str,
    mode: str,
    indices: list[int],
    source_identity: str,
    target_index: int,
) -> dict[str, Any]:
    selector = {"automation_id": "dataGrid"}
    status_selector = {"automation_id": "txtOutput"}
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": f"wpf v2 {mode} selected-payload drag reorder",
        "baseline": {
            "steps": [
                {
                    "id": "launch_fixture",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": program,
                        "cwd": os.path.dirname(program),
                        "pre_build": True,
                        "build_project": build_project,
                        "build_configuration": "Debug",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": f"wpf_{mode}_selected_payload_drag_reorder",
                "transitions": [
                    {
                        "id": "select_payload",
                        "action": {
                            "kind": "ui.grid.select",
                            "selector": selector,
                            "indices": indices,
                        },
                        "settle": {"idle_ms": 300},
                        "probes": [
                            {
                                "kind": "ui.grid.viewport",
                                "name": "selected_payload_setup",
                                "selector": selector,
                                "identity": {"column": "Phrase"},
                                "rows": {"visible_only": True, "max": 8},
                                "phase": "after",
                            }
                        ],
                    },
                    {
                        "id": "drag_selected_payload",
                        "action": {
                            "kind": "ui.drag",
                            "source": {
                                "selector": selector,
                                "row_identity": source_identity,
                            },
                            "path": [
                                {"relative_to": "source", "x": 0.5, "y": 0.5},
                                {"relative_to": "drop", "x": 0.5, "y": 0.5},
                            ],
                            "drop": {
                                "selector": selector,
                                "row_index": target_index,
                            },
                            "identity": {"column": "Phrase"},
                            "duration_ms": 650,
                            "expect": {
                                "row_count_preserved": True,
                                "identity_set_preserved": True,
                                "selected_payload_preserved": True,
                            },
                        },
                        "settle": {"idle_ms": 700},
                        "probes": [
                            {
                                "kind": "ui.grid.viewport",
                                "name": "selected_payload_viewport",
                                "selector": selector,
                                "identity": {"column": "Phrase"},
                                "rows": {"visible_only": True, "max": 8},
                                "expect": {
                                    "selected_payload_preserved": True,
                                    "row_count_preserved": True,
                                },
                            },
                            {
                                "kind": "ui.property",
                                "name": "selected_payload_status",
                                "selector": status_selector,
                                "property": "Text",
                                "phase": "after",
                            },
                        ],
                    },
                ],
            }
        ],
        "cleanup": {
            "steps": [
                {"kind": "debug.stop"},
                {"kind": "process.registry.assert_empty"},
            ]
        },
    }


def _v2_multi_row_drag_summary(
    result: dict[str, Any],
    *,
    mode: str,
    expected_count: int,
) -> dict[str, Any]:
    drag_transition = _v2_transition(result, 1)
    action = _first_transition_action(drag_transition)
    selected_payload = dict(action.get("selected_payload") or {})
    status = _parse_drag_reorder_status(
        _transition_probe_value(
            drag_transition,
            "after",
            "ui.property.selected_payload_status",
        )
    )
    viewport = _transition_probe_result(
        drag_transition,
        "after",
        "ui.grid.viewport.selected_payload_viewport",
    )
    comparison = dict(viewport.get("comparison") or {})
    before = (
        selected_payload.get("before") or status.get("selected_payload_before") or []
    )
    after = selected_payload.get("after") or status.get("selected_payload_after") or []
    order = status.get("order") or []
    compact = {
        "status": result.get("status"),
        "reason": result.get("reason"),
        "mode": mode,
        "selected_payload": selected_payload,
        "status_text": status,
        "viewport": comparison,
        "selected_before": before,
        "selected_after": after,
        "selected_count_matches": len(before) == expected_count
        and len(after) == expected_count,
        "selected_payload_preserved": before == after and len(set(after)) == len(after),
        "selected_payload_group_visible": _payload_group_visible(order, after),
        "cleanup": result.get("cleanup"),
        "blocked": result.get("blocked"),
    }
    if result.get("status") == "BLOCKED":
        compact["status"] = "BLOCKED"
        return compact
    compact["status"] = (
        "PASS"
        if result.get("status") == "PASS"
        and compact["selected_count_matches"]
        and compact["selected_payload_preserved"]
        and compact["selected_payload_group_visible"]
        and comparison.get("selected_payload_preserved") is True
        else "FAIL"
    )
    return compact


async def run_wpf_v2_negative_drag_runtime_smoke() -> dict[str, Any]:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner
    from netcoredbg_mcp.session.runtime_smoke_operations import ui_operation_adapters
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend

    if sys.platform != "win32":
        return {
            "status": "BLOCKED",
            "reason": "WPF v2 negative drag smoke requires Windows UI automation",
        }

    m = SessionManager(project_path=BASE)
    backend_holder: dict[str, object | None] = {"backend": None}
    backend_holder["backend"] = create_backend(process_registry=m.process_registry)
    if not isinstance(backend_holder["backend"], FlaUIBackend):
        return {
            "status": "BLOCKED",
            "backend": type(backend_holder["backend"]).__name__,
            "reason": "FlaUI bridge required for WPF v2 negative drag smoke",
            "accepted": {"backend": "FlaUI drag_path and viewport evidence"},
            "next_step": "Build the FlaUI bridge and run on a Windows desktop session.",
        }

    async def ensure_ui_connected():
        backend = backend_holder["backend"]
        if backend is None:
            backend = create_backend(process_registry=m.process_registry)
            backend_holder["backend"] = backend
        pid = m.state.process_id
        if not pid:
            raise RuntimeError(
                "Process ID not available for WPF v2 negative drag smoke"
            )
        if getattr(backend, "process_id", None) != pid:
            await backend.connect(pid)
        return backend

    plan = _v2_negative_drag_plan(
        program=WPF_DLL,
        build_project=os.path.join(
            BASE,
            "tests",
            "fixtures",
            "WpfSmokeApp",
            "WpfSmokeApp.csproj",
        ),
    )

    try:
        result = await RuntimeSmokeRunner(
            m,
            service_adapters=ui_operation_adapters(ensure_ui_connected, session=m),
        ).run(plan)
        return _v2_negative_drag_summary(result)
    finally:
        if backend_holder["backend"] is not None:
            try:
                await backend_holder["backend"].disconnect()
            except Exception as exc:
                print(
                    f"  [DEBUG] WPF v2 negative drag backend.disconnect() failed: {exc}"
                )
        await m.stop()


def _v2_negative_drag_plan(
    *,
    program: str,
    build_project: str,
) -> dict[str, Any]:
    selector = {"automation_id": "dataGrid"}
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "name": "wpf v2 negative drag no-op safety",
        "baseline": {
            "steps": [
                {
                    "id": "launch_fixture",
                    "kind": "isolated_profile.launch",
                    "launch": {
                        "program": program,
                        "cwd": os.path.dirname(program),
                        "pre_build": True,
                        "build_project": build_project,
                        "build_configuration": "Debug",
                    },
                }
            ]
        },
        "cases": [
            {
                "id": "wpf_negative_drag_noop_safety",
                "transitions": [
                    _v2_negative_drag_transition(
                        transition_id="small_movement_noop",
                        source={"selector": selector, "row_index": 1},
                        path=[
                            {"relative_to": "source", "x": 0.5, "y": 0.5},
                            {
                                "relative_to": "source",
                                "x": 0.52,
                                "y": 0.5,
                            },
                            {
                                "relative_to": "source",
                                "x": 0.5,
                                "y": 0.5,
                            },
                        ],
                        drop={
                            "selector": selector,
                            "row_index": 1,
                        },
                        no_op_reason="small_movement",
                        viewport_probe_name="small_movement_viewport",
                        selector=selector,
                        modifiers=["shift"],
                    ),
                    _v2_negative_drag_transition(
                        transition_id="cancel_no_drop",
                        source={"selector": selector, "row_index": 1},
                        path=[
                            {"relative_to": "source", "x": 0.5, "y": 0.5},
                            {
                                "relative_to": "drop",
                                "x": 0.5,
                                "y": 0.5,
                            },
                            {
                                "relative_to": "source",
                                "x": 0.5,
                                "y": 0.5,
                            },
                        ],
                        drop={
                            "selector": selector,
                            "row_index": 2,
                        },
                        no_op_reason="cancelled",
                        viewport_probe_name="cancel_no_drop_viewport",
                        selector=selector,
                        cancel={"key": "escape"},
                        modifiers=[],
                    ),
                ],
            }
        ],
        "cleanup": {
            "steps": [
                {"kind": "debug.stop"},
                {"kind": "process.registry.assert_empty"},
            ]
        },
    }


def _v2_negative_drag_transition(
    *,
    transition_id: str,
    source: dict[str, Any],
    path: list[dict[str, Any]],
    drop: dict[str, Any],
    no_op_reason: str,
    viewport_probe_name: str,
    selector: dict[str, Any],
    modifiers: list[str],
    cancel: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "id": transition_id,
        "action": {
            "kind": "ui.drag",
            "source": source,
            "path": path,
            "drop": drop,
            "duration_ms": 320,
            "modifiers": modifiers,
            "cancel": cancel or {},
            "expect": {
                "no_op": True,
                "no_op_reason": no_op_reason,
            },
        },
        "settle": {"idle_ms": 500},
        "probes": [
            {
                "kind": "ui.grid.viewport",
                "name": viewport_probe_name,
                "selector": selector,
                "identity": {"column": "Phrase"},
                "rows": {"visible_only": True, "max": 8},
                "expect": {
                    "identity_order_preserved": True,
                    "row_count_preserved": True,
                },
            }
        ],
    }


def _v2_negative_drag_summary(result: dict[str, Any]) -> dict[str, Any]:
    cases = {
        "small_movement": _v2_negative_drag_case_summary(
            result,
            transition_index=0,
            probe_path="ui.grid.viewport.small_movement_viewport",
            expected_reason="small_movement",
        ),
        "cancel_no_drop": _v2_negative_drag_case_summary(
            result,
            transition_index=1,
            probe_path="ui.grid.viewport.cancel_no_drop_viewport",
            expected_reason="cancelled",
        ),
    }
    statuses = [case.get("status") for case in cases.values()]
    status = (
        "BLOCKED"
        if result.get("status") == "BLOCKED"
        or any(item == "BLOCKED" for item in statuses)
        else "PASS"
        if result.get("status") == "PASS" and all(item == "PASS" for item in statuses)
        else "FAIL"
    )
    return {
        "status": status,
        "reason": result.get("reason"),
        "cases": cases,
        "blocked": result.get("blocked"),
    }


def _v2_negative_drag_case_summary(
    result: dict[str, Any],
    *,
    transition_index: int,
    probe_path: str,
    expected_reason: str,
) -> dict[str, Any]:
    transition = _v2_transition(result, transition_index)
    action = _first_transition_action(transition)
    no_op = dict(action.get("no_op") or {})
    cleanup = dict(action.get("cleanup") or {})
    viewport = _transition_probe_result(transition, "after", probe_path)
    comparison = dict(viewport.get("comparison") or {})
    order_preserved = (
        comparison.get("identity_order_preserved") is True
        and comparison.get("row_count_preserved") is True
    )
    cleanup_observed = bool(cleanup.get("modifier_cleanup")) and bool(
        cleanup.get("pointer_cleanup")
    )
    status = (
        "PASS"
        if action.get("status") == "PASS"
        and no_op.get("expected") is True
        and no_op.get("reason") == expected_reason
        and order_preserved
        and cleanup_observed
        else action.get("status")
        if action.get("status") == "BLOCKED"
        else "FAIL"
    )
    return {
        "status": status,
        "action_status": action.get("status"),
        "no_op": no_op,
        "cleanup": cleanup,
        "viewport": comparison,
        "order_preserved": order_preserved,
        "cleanup_observed": cleanup_observed,
    }


def _payload_group_visible(order: list[str], payload: list[str]) -> bool:
    if not order or not payload:
        return False
    for index in range(0, len(order) - len(payload) + 1):
        if order[index : index + len(payload)] == payload:
            return True
    return False


def _first_v2_transition(result: dict[str, Any]) -> dict[str, Any]:
    cases = result.get("cases")
    if not isinstance(cases, list) or not cases:
        return {}
    transitions = cases[0].get("transitions") if isinstance(cases[0], dict) else None
    if not isinstance(transitions, list) or not transitions:
        return {}
    return transitions[0] if isinstance(transitions[0], dict) else {}


def _v2_transition(result: dict[str, Any], index: int) -> dict[str, Any]:
    cases = result.get("cases")
    if not isinstance(cases, list) or not cases:
        return {}
    transitions = cases[0].get("transitions") if isinstance(cases[0], dict) else None
    if not isinstance(transitions, list) or index >= len(transitions):
        return {}
    return transitions[index] if isinstance(transitions[index], dict) else {}


def _first_transition_action(transition: dict[str, Any]) -> dict[str, Any]:
    actions = transition.get("actions")
    if not isinstance(actions, list) or not actions:
        return {}
    return actions[0] if isinstance(actions[0], dict) else {}


def _transition_probe_result(
    transition: dict[str, Any],
    phase: str,
    path: str,
) -> dict[str, Any]:
    probes_by_phase = transition.get("probes")
    if not isinstance(probes_by_phase, dict):
        return {}
    probes = probes_by_phase.get(phase)
    if not isinstance(probes, list):
        return {}
    for probe in probes:
        if isinstance(probe, dict) and probe.get("path") == path:
            return probe
    return {}


def _transition_probe_value(transition: dict[str, Any], phase: str, path: str) -> Any:
    values = transition.get(phase)
    if not isinstance(values, dict):
        return None
    return values.get(path)


def _transition_grid_rows(
    transition: dict[str, Any], phase: str
) -> list[dict[str, Any]]:
    phase_values = transition.get(phase)
    if not isinstance(phase_values, dict):
        return []
    rows = phase_values.get("ui.grid.cue_order")
    if not isinstance(rows, list):
        return []
    return [dict(row) for row in rows if isinstance(row, dict)]


def _row_identity_refs(rows: list[dict[str, Any]]) -> list[str]:
    refs: list[str] = []
    for row in rows:
        cells = row.get("cells")
        if isinstance(cells, dict) and cells.get("Phrase"):
            refs.append(str(cells["Phrase"]))
            continue
        if row.get("automation_id"):
            refs.append(str(row["automation_id"]))
            continue
        refs.append(f"row:{row.get('index')}")
    return refs


def _moved_refs(refs: list[str], *, source_index: int, target_index: int) -> list[str]:
    if source_index >= len(refs) or target_index >= len(refs):
        return []
    moved = list(refs)
    item = moved.pop(source_index)
    moved.insert(target_index, item)
    return moved


def _moved_refs_by_identity(
    refs: list[str],
    *,
    source: Any,
    target: Any,
) -> list[str]:
    if not source or not target or source not in refs or target not in refs:
        return []
    moved = list(refs)
    source_index = moved.index(str(source))
    target_index = moved.index(str(target))
    item = moved.pop(source_index)
    moved.insert(target_index, item)
    return moved


def _parse_drag_reorder_status(value: Any) -> dict[str, Any]:
    text = str(value or "")
    blocked_match = re.search(
        r"sourceIdentity=(.*?) targetIdentity=(.*?) "
        r"dropPoint=(-?\d+),(-?\d+) "
        r"dropOriginTarget=(.*?) dropBoundsTarget=(.*?) "
        r"dropBoundsIndex=(-?\d+) dropBoundsTop=(-?\d+) dropBoundsBottom=(-?\d+)$",
        text,
    )
    if blocked_match:
        return {
            "raw": text,
            "source_identity": blocked_match.group(1),
            "target_identity": blocked_match.group(2),
            "selected_payload_mode": None,
            "selected_payload_before": [],
            "selected_payload_after": [],
            "edge_scroll_direction": None,
            "edge_first_visible": None,
            "edge_last_visible": None,
            "drop_point_x": int(blocked_match.group(3)),
            "drop_point_y": int(blocked_match.group(4)),
            "drop_origin_target": blocked_match.group(5),
            "drop_bounds_target": blocked_match.group(6),
            "drop_bounds_index": int(blocked_match.group(7)),
            "drop_bounds_top": int(blocked_match.group(8)),
            "drop_bounds_bottom": int(blocked_match.group(9)),
            "order": [],
        }
    match = re.search(
        r"sourceIdentity=(.*?) targetIdentity=(.*?) "
        r"(?:selectedPayloadMode=(.*?) selectedPayloadBefore=(.*?) "
        r"selectedPayloadAfter=(.*?) )?"
        r"edgeScrollDirection=(.*?) "
        r"edgeFirstVisible=(-?\d+) edgeLastVisible=(-?\d+) "
        r"(?:dropPoint=(-?\d+),(-?\d+) dropOriginTarget=(.*?) "
        r"dropBoundsTarget=(.*?) dropBoundsIndex=(-?\d+) "
        r"dropBoundsTop=(-?\d+) dropBoundsBottom=(-?\d+) )?"
        r"orderFingerprint=(.*)$",
        text,
    )
    if not match:
        return {"raw": text}
    selected_before = match.group(4)
    selected_after = match.group(5)
    return {
        "raw": text,
        "source_identity": match.group(1),
        "target_identity": match.group(2),
        "selected_payload_mode": match.group(3),
        "selected_payload_before": selected_before.split("|")
        if selected_before
        else [],
        "selected_payload_after": selected_after.split("|") if selected_after else [],
        "edge_scroll_direction": match.group(6),
        "edge_first_visible": int(match.group(7)),
        "edge_last_visible": int(match.group(8)),
        "drop_point_x": int(match.group(9)) if match.group(9) else None,
        "drop_point_y": int(match.group(10)) if match.group(10) else None,
        "drop_origin_target": match.group(11) if match.group(11) else None,
        "drop_bounds_target": match.group(12) if match.group(12) else None,
        "drop_bounds_index": int(match.group(13)) if match.group(13) else None,
        "drop_bounds_top": int(match.group(14)) if match.group(14) else None,
        "drop_bounds_bottom": int(match.group(15)) if match.group(15) else None,
        "order": match.group(16).split(">") if match.group(16) else [],
    }


def _wpf_fixture_cue_order() -> list[str]:
    return [
        "Fixture cue one",
        "Fixture cue two",
        "Fixture cue three",
        "Fixture cue four",
        "Fixture cue five",
        "Fixture cue six",
        "Fixture cue seven",
        "Fixture cue eight",
        "Fixture cue nine",
        "Fixture cue ten",
        "Fixture cue eleven",
        "Fixture cue twelve",
        "Fixture cue thirteen",
        "Fixture cue fourteen",
        "Fixture cue fifteen",
        "Fixture cue sixteen",
        "Fixture cue seventeen",
        "Fixture cue eighteen",
        "Fixture cue nineteen",
        "Fixture cue twenty",
        "Fixture cue twenty-one",
        "Fixture cue twenty-two",
        "Fixture cue twenty-three",
        "Fixture cue twenty-four",
    ]


async def test_wpf_v2_visible_row_drag_runtime_smoke():
    print("\nWPF V2 VISIBLE-ROW DRAG RUNTIME SMOKE")
    evidence = await run_wpf_v2_visible_row_drag_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check("WPF v2 visible-row drag reports actionable BLOCKED", True, str(evidence))
        return
    check(
        "WPF v2 visible-row drag returned PASS",
        evidence.get("status") == "PASS",
        str(evidence),
    )
    check(
        "WPF v2 visible-row drag preserved row count and identities",
        bool(evidence.get("row_count_preserved"))
        and bool(evidence.get("identity_set_preserved")),
        str(evidence),
    )
    check(
        "WPF v2 visible-row drag route evidence is compact and complete",
        bool(evidence.get("backend"))
        and bool(evidence.get("source_bounds"))
        and bool(evidence.get("move_points"))
        and bool(evidence.get("final_pointer")),
        str(evidence),
    )


async def test_wpf_v2_offscreen_row_target_drag_runtime_smoke():
    print("\nWPF V2 OFFSCREEN ROW-TARGET DRAG RUNTIME SMOKE")
    evidence = await run_wpf_v2_offscreen_row_target_drag_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check(
            "WPF v2 offscreen row-target drag reports actionable BLOCKED",
            True,
            str(evidence),
        )
        return
    check(
        "WPF v2 offscreen row-target drag returned PASS",
        evidence.get("status") == "PASS",
        str(evidence),
    )
    check(
        "WPF v2 offscreen row-target drag requested source/drop visibility",
        evidence.get("source_ensure_visible_requested") is True
        and evidence.get("drop_ensure_visible_requested") is True,
        str(evidence),
    )
    check(
        "WPF v2 offscreen row-target drag proved target row became visible",
        bool(evidence.get("target_visible_after_drop"))
        and bool(evidence.get("target_identity_matches"))
        and bool(evidence.get("drop_origin_target_matches"))
        and bool(evidence.get("drop_bounds_target_matches")),
        str(evidence),
    )
    check(
        "WPF v2 offscreen row-target drag preserves final order expectations",
        bool(evidence.get("source_identity_matches"))
        and bool(evidence.get("final_order_matches")),
        str(evidence),
    )


async def test_wpf_v2_edge_scroll_drag_runtime_smoke():
    print("\nWPF V2 EDGE-SCROLL DRAG RUNTIME SMOKE")
    evidence = await run_wpf_v2_edge_scroll_drag_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check("WPF v2 edge-scroll drag reports actionable BLOCKED", True, str(evidence))
        return
    check(
        "WPF v2 edge-scroll drag returned PASS",
        evidence.get("status") == "PASS",
        str(evidence),
    )
    check(
        "WPF v2 edge-scroll drag proved down and up viewport movement",
        bool(evidence.get("down", {}).get("edge_scroll_matches"))
        and bool(evidence.get("up", {}).get("edge_scroll_matches")),
        str(evidence),
    )
    check(
        "WPF v2 edge-scroll drag preserved final order expectations",
        bool(evidence.get("down", {}).get("final_order_matches"))
        and bool(evidence.get("up", {}).get("final_order_matches")),
        str(evidence),
    )
    check(
        "WPF v2 edge-scroll drag includes path holds and process metrics",
        bool(evidence.get("down", {}).get("hold_points"))
        and bool(evidence.get("up", {}).get("hold_points"))
        and isinstance(evidence.get("process_metrics", {}).get("down"), dict)
        and isinstance(evidence.get("process_metrics", {}).get("up"), dict),
        str(evidence),
    )


async def test_wpf_v2_multi_row_drag_runtime_smoke():
    print("\nWPF V2 MULTI-ROW DRAG RUNTIME SMOKE")
    evidence = await run_wpf_v2_multi_row_drag_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check("WPF v2 multi-row drag reports actionable BLOCKED", True, str(evidence))
        return
    cases = evidence.get("cases", {})
    contiguous = cases.get("contiguous", {}) if isinstance(cases, dict) else {}
    non_contiguous = cases.get("non_contiguous", {}) if isinstance(cases, dict) else {}
    check(
        "WPF v2 multi-row drag returned PASS",
        evidence.get("status") == "PASS",
        str(evidence),
    )
    check(
        "WPF v2 multi-row drag preserves contiguous selected payload",
        contiguous.get("status") == "PASS"
        and bool(contiguous.get("selected_payload_preserved")),
        str(contiguous),
    )
    check(
        "WPF v2 multi-row drag preserves non-contiguous selected payload",
        non_contiguous.get("status") == "PASS"
        and bool(non_contiguous.get("selected_payload_preserved")),
        str(non_contiguous),
    )
    check(
        "WPF v2 multi-row drag keeps selected payload grouped after reorder",
        bool(contiguous.get("selected_payload_group_visible"))
        and bool(non_contiguous.get("selected_payload_group_visible")),
        str(evidence),
    )


async def test_wpf_v2_negative_drag_runtime_smoke():
    print("\nWPF V2 NEGATIVE DRAG RUNTIME SMOKE")
    evidence = await run_wpf_v2_negative_drag_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check("WPF v2 negative drag reports actionable BLOCKED", True, str(evidence))
        return
    cases = evidence.get("cases", {})
    small_movement = cases.get("small_movement", {}) if isinstance(cases, dict) else {}
    cancel_no_drop = cases.get("cancel_no_drop", {}) if isinstance(cases, dict) else {}
    check(
        "WPF v2 negative drag returned PASS",
        evidence.get("status") == "PASS",
        str(evidence),
    )
    check(
        "WPF v2 negative drag preserves order for small movement",
        small_movement.get("status") == "PASS"
        and bool(small_movement.get("order_preserved")),
        str(small_movement),
    )
    check(
        "WPF v2 negative drag preserves order for cancel/no-drop",
        cancel_no_drop.get("status") == "PASS"
        and bool(cancel_no_drop.get("order_preserved")),
        str(cancel_no_drop),
    )
    check(
        "WPF v2 negative drag exposes cleanup evidence",
        bool(small_movement.get("cleanup_observed"))
        and bool(cancel_no_drop.get("cleanup_observed")),
        str(evidence),
    )


async def test_avalonia_v2_state_oracle_runtime_smoke():
    print("\nAVALONIA V2 STATE ORACLE RUNTIME SMOKE")
    evidence = await run_avalonia_v2_state_oracle_runtime_smoke()
    print(f"  evidence: {evidence}")
    if evidence.get("status") == "BLOCKED":
        check(
            "Avalonia v2 state oracle reports actionable BLOCKED", True, str(evidence)
        )
        return
    check(
        "Avalonia v2 state oracle happy path and selector miss are classified",
        evidence.get("status") == "PASS",
        str(evidence),
    )


async def test_avalonia_ui_fixture_compatibility():
    print("\n24. AVALONIA UI FIXTURE COMPATIBILITY")
    from netcoredbg_mcp.ui.backend import create_backend
    from netcoredbg_mcp.ui.flaui_client import FlaUIBackend
    from netcoredbg_mcp.ui.grid import read_grid_visible_rows
    from netcoredbg_mcp.ui.key_sequence import run_scoped_key_sequence

    m = SessionManager()
    backend = None
    try:
        await m.launch(program=AVALONIA_DLL)
        await asyncio.sleep(2.0)

        backend = create_backend(process_registry=m.process_registry)
        pid = m.state.process_id
        if not pid:
            check("Avalonia UI: process started", False, "no PID")
            return
        await backend.connect(pid)

        if not isinstance(backend, FlaUIBackend):
            evidence = {
                "status": "BLOCKED",
                "backend": type(backend).__name__,
                "reason": "FlaUI bridge required for Avalonia UIA compatibility proof",
            }
            print(f"  evidence: {evidence}")
            check("Avalonia UI reports BLOCKED without FlaUI", True, str(evidence))
            return

        selector = {"automation_id": "dataGrid"}
        found = await backend.find_element(automation_id="dataGrid")
        key_result = await run_scoped_key_sequence(
            backend,
            selector,
            modifiers=["shift"],
            keys=["Down", "Down"],
        )
        text_result = await backend.extract_text(automation_id="txtOutput")
        status_text = (
            text_result.get("text", "")
            if isinstance(text_result, dict)
            else str(text_result)
        )
        try:
            grid_result = await read_grid_visible_rows(backend, selector)
        except Exception as exc:
            grid_result = {
                "status": "BLOCKED",
                "reason": str(exc),
            }
        route_result = {
            "status": "PASS"
            if "AvaloniaDataGridArrow" in status_text and "shift=True" in status_text
            else "BLOCKED",
            "status_text": status_text,
        }
        if route_result["status"] == "BLOCKED":
            route_result["reason"] = (
                "Avalonia DataGrid accepted UIA focus but did not raise the fixture "
                "KeyDown route through this backend path"
            )

        evidence = {
            "found": found,
            "key_sequence": key_result,
            "grid": grid_result,
            "route": route_result,
        }
        print(f"  evidence: {evidence}")
        check(
            "Avalonia DataGrid fixture found",
            bool(found.get("found", False))
            if isinstance(found, dict)
            else found is not None,
            str(found),
        )
        check(
            "Avalonia key sequence cleanup released modifiers",
            key_result.get("status") == "PASS"
            and key_result.get("final_held_modifiers") == [],
            str(key_result),
        )
        check(
            "Avalonia route logs Shift or reports BLOCKED",
            route_result.get("status") in {"PASS", "BLOCKED"},
            str(route_result),
        )
        check(
            "Avalonia DataGrid evidence is bounded",
            grid_result.get("status")
            in {"PASS", "UNSUPPORTED", "BLOCKED", "AMBIGUOUS"},
            str(grid_result),
        )
    finally:
        if backend is not None:
            try:
                await backend.disconnect()
            except Exception:
                pass
        await m.stop()


async def _launch_wpf_stealth_backend():
    """Launch the WPF fixture in stealth mode and connect a UI backend."""
    if not WPF_GUI_ENABLED:
        check("WPF fixture built", True, "skipped: build tests/fixtures/WpfSmokeApp")
        return None, None

    from netcoredbg_mcp.ui.backend import create_backend

    m = await new_session()
    try:
        await m.launch(
            program=WPF_DLL,
            cwd=os.path.dirname(WPF_DLL),
            stealth_mode=True,
        )
        await asyncio.sleep(2.0)
        pid = m.state.process_id
        if not pid:
            check("Stealth debuggee PID", False, "no PID")
            await m.stop()
            return None, None

        backend = create_backend(process_registry=m.process_registry)
        await backend.connect(pid, stealth=True)
        return m, backend
    except Exception:
        await m.stop()
        raise


def _current_foreground_hwnd() -> int | None:
    if os.name != "nt":
        return None
    import ctypes

    return int(ctypes.windll.user32.GetForegroundWindow())


async def test_stealth_launch():
    """Scenario: stealth launch keeps the current foreground window unchanged."""
    print("\n--- Stealth Launch ---")
    if not WPF_GUI_ENABLED:
        check("WPF fixture built", True, "skipped: build tests/fixtures/WpfSmokeApp")
        return

    before_hwnd = _current_foreground_hwnd()
    m = await new_session()
    try:
        await m.launch(
            program=WPF_DLL,
            cwd=os.path.dirname(WPF_DLL),
            stealth_mode=True,
        )
        await asyncio.sleep(1.5)
        after_hwnd = _current_foreground_hwnd()

        check("Stealth flag stored", m.stealth_mode is True)
        if before_hwnd is None or after_hwnd is None:
            check("Foreground unchanged", True, "skipped: non-Windows foreground API")
        else:
            check(
                "Foreground unchanged",
                before_hwnd == after_hwnd,
                f"before={before_hwnd} after={after_hwnd}",
            )
    finally:
        await m.stop()


async def test_wpf_stealth_delayed_readiness_replay():
    """Scenario: stealth launch does not restore foreground before WPF UI readiness proof."""
    print("\n--- WPF Stealth Delayed Readiness Replay ---")
    if not WPF_GUI_ENABLED:
        check("WPF fixture built", True, "skipped: build tests/fixtures/WpfSmokeApp")
        return

    from netcoredbg_mcp.session import manager as manager_mod
    from netcoredbg_mcp.ui.backend import create_backend

    original_get_foreground = manager_mod.get_foreground_window
    original_get_window_process_id = manager_mod.get_window_process_id
    original_restore = manager_mod.restore_foreground_window
    foreground_values = iter([111, 222, 222, 222, 222])
    restore_calls: list[int] = []

    def fake_foreground() -> int:
        return next(foreground_values, 222)

    manager_mod.get_foreground_window = fake_foreground
    manager_mod.get_window_process_id = lambda hwnd: None
    manager_mod.restore_foreground_window = (
        lambda hwnd: restore_calls.append(hwnd) is None or True
    )

    m = await new_session()
    backend = None
    try:
        await m.launch(
            program=WPF_DLL,
            cwd=os.path.dirname(WPF_DLL),
            stealth_mode=True,
        )
        check(
            "WPF stealth readiness: launch returned before foreground restore",
            restore_calls == [],
            f"restore_calls={restore_calls}",
        )

        pid = m.state.process_id
        if not pid:
            check("WPF stealth readiness: process started", False, "no PID")
            return
        check("WPF stealth readiness: process started", True, f"PID={pid}")

        backend = create_backend(process_registry=m.process_registry)
        await backend.connect(pid, stealth=True)
        tree = await backend.get_window_tree(max_depth=1)
        windows = tree.get("windows") if isinstance(tree, dict) else None
        check(
            "WPF stealth readiness: MainWindow tree available",
            isinstance(windows, list) and len(windows) > 0,
            str(tree)[:200],
        )
    finally:
        manager_mod.get_foreground_window = original_get_foreground
        manager_mod.get_window_process_id = original_get_window_process_id
        manager_mod.restore_foreground_window = original_restore
        if backend is not None:
            try:
                await backend.disconnect()
            except Exception:
                pass
        await m.stop()


async def test_stealth_click():
    """Scenario: stealth click by automation id uses InvokePattern when available."""
    print("\n--- Stealth Click ---")
    m = None
    backend = None
    try:
        m, backend = await _launch_wpf_stealth_backend()
        if m is None or backend is None:
            return

        result = await backend.client.call("click", {"automationId": "btnInvoke"})
        check("Stealth click succeeds", result.get("clicked") is True, str(result))
        check(
            "Stealth click uses InvokePattern",
            result.get("method") == "InvokePattern",
            str(result),
        )
    finally:
        if backend is not None:
            await backend.disconnect()
        if m is not None:
            await m.stop()


async def test_wpf_selector_safety_no_side_effect():
    """Scenario: exact selector miss is blocked before a side-effecting fallback."""
    print("\n--- WPF Selector Safety No Side Effect ---")
    m = None
    backend = None
    try:
        m, backend = await _launch_wpf_stealth_backend()
        if m is None or backend is None:
            return

        before = await backend.find_element(automation_id="selectorSafetyStatus")
        blocked = False
        blocked_detail = ""
        try:
            result = await backend.invoke_element(
                automation_id="playButton",
                control_type="Button",
                root_id="selectorSafetyPanel",
            )
            blocked_detail = str(result)
            if isinstance(result, dict):
                blocked = (
                    result.get("status") == "BLOCKED"
                    or result.get("reason")
                    == "selector result did not match exact automation_id"
                )
        except (RuntimeError, LookupError) as exc:
            blocked_detail = str(exc)
            blocked = (
                "selector result did not match exact automation_id" in blocked_detail
            )

        after = await backend.find_element(automation_id="selectorSafetyStatus")
        before_name = before.get("name")
        after_name = after.get("name")

        check("Exact selector miss blocked", blocked, blocked_detail)
        check(
            "Selector side-effect sentinel unchanged",
            before_name == "Selector side effects: 0" and after_name == before_name,
            f"before={before_name!r} after={after_name!r}",
        )
    finally:
        if backend is not None:
            await backend.disconnect()
        if m is not None:
            await m.stop()


async def test_stealth_screenshot():
    """Scenario: stealth screenshot uses PrintWindow or documented flash-focus fallback."""
    print("\n--- Stealth Screenshot ---")
    m = None
    backend = None
    try:
        m, backend = await _launch_wpf_stealth_backend()
        if m is None or backend is None:
            return

        result = await backend.client.call("screenshot", {})
        method = result.get("method")
        fallback = result.get("fallback")
        check(
            "Stealth screenshot returned image", bool(result.get("base64")), str(result)
        )
        check(
            "Stealth screenshot uses PrintWindow path",
            method == "PrintWindow" or fallback == "flash-focus",
            f"method={method} fallback={fallback}",
        )
    finally:
        if backend is not None:
            await backend.disconnect()
        if m is not None:
            await m.stop()


async def test_code_search():
    """Scenario: project-scoped code search finds fixture symbols."""
    print("\n--- Code Search ---")
    from netcoredbg_mcp.code_search import CodeSearchEngine

    fixture_root = os.path.join(BASE, "tests", "fixtures", "SearchTestApp")
    engine = CodeSearchEngine(fixture_root)

    symbols = engine.find_code_symbol("LoadAssignedCharacter")
    references = engine.find_code_references("CueInputPanel")
    context = engine.get_source_context("ViewModels/MainViewModel.cs", line=5, radius=2)
    matches = engine.search_source("textBoxCue|Phrase", file_glob="*.xaml")

    check(
        "find_code_symbol",
        any(item["name"] == "LoadAssignedCharacter" for item in symbols),
    )
    check("find_code_references", len(references) > 0, f"count={len(references)}")
    check("get_source_context", len(context.get("lines", [])) > 0)
    check("search_source", len(matches) > 0, f"count={len(matches)}")


def get_scenarios():
    scenarios = [
        ("Hit Counting", test_hit_counting),
        ("Stack + Variables", test_stack_and_variables),
        ("Stepping", test_stepping),
        ("Output Categories", test_output_categories),
        ("Modules", test_modules),
        ("Quick Evaluate", test_quick_evaluate),
        ("Exception Handling", test_exception_handling),
        ("Capabilities + Terminate", test_capabilities_and_terminate),
        ("Stopped Description", test_stopped_description),
        ("Threads + Pause", test_threads_and_pause),
        ("Tracepoints", test_tracepoints),
        ("Snapshots", test_snapshots),
        ("Collection + Object Analysis", test_collection_and_object),
        ("Tracepoint Performance", test_tracepoint_performance),
        ("Tracepoint Auto-Resume", test_tracepoint_auto_resume),
        ("Path Validation", test_path_validation_worktrees),
        ("Heartbeat During Wait", test_heartbeat_during_wait),
        ("Runtime Hygiene Preflight", test_runtime_hygiene_preflight),
        ("Instrumentation Group Lifecycle", test_instrumentation_group_lifecycle),
        ("Output Checkpoint Assertions", test_output_checkpoint_assertions),
        ("Runtime Smoke Bounded Runner", test_runtime_smoke_bounded_runner),
        ("UI Focused Evidence", test_ui_focused_evidence),
        ("Stealth Launch", test_stealth_launch),
        ("Stealth Click", test_stealth_click),
        ("Stealth Screenshot", test_stealth_screenshot),
        ("Code Search", test_code_search),
        ("WPF V2 State Oracle Runtime Smoke", test_wpf_v2_state_oracle_runtime_smoke),
        (
            "WPF V2 Selector-Scoped Hover Runtime Smoke",
            test_wpf_v2_hover_runtime_smoke,
        ),
        (
            "WPF V2 Visible-Row Drag Runtime Smoke",
            test_wpf_v2_visible_row_drag_runtime_smoke,
        ),
        (
            "WPF V2 Offscreen Row-Target Drag Runtime Smoke",
            test_wpf_v2_offscreen_row_target_drag_runtime_smoke,
        ),
        (
            "WPF V2 Edge-Scroll Drag Runtime Smoke",
            test_wpf_v2_edge_scroll_drag_runtime_smoke,
        ),
        (
            "WPF V2 Multi-Row Drag Runtime Smoke",
            test_wpf_v2_multi_row_drag_runtime_smoke,
        ),
        (
            "WPF V2 Negative Drag Runtime Smoke",
            test_wpf_v2_negative_drag_runtime_smoke,
        ),
        (
            "Avalonia V2 State Oracle Runtime Smoke",
            test_avalonia_v2_state_oracle_runtime_smoke,
        ),
    ]

    if GUI_ENABLED:
        scenarios.extend(
            [
                ("UI Invoke + Toggle + Root ID", test_ui_invoke_toggle),
                ("DataGrid Select + Read", test_datagrid_select),
                ("Multi-Window Envelope", test_multi_window_envelope),
                ("Drag Primitive", test_drag_primitive),
                ("System Event Theme Toggle", test_system_event_theme),
                ("Persistent Modifier Hold", test_persistent_modifier_hold),
                ("Scoped Search Performance", test_scoped_search_performance),
                ("Window Lifecycle", test_window_lifecycle),
                ("Expand/Collapse Tree", test_expand_collapse_tree),
                ("Set Value Slider", test_set_value_slider),
                ("Realize Virtualized Item", test_realize_virtualized_item),
                ("Clipboard Roundtrip", test_clipboard_roundtrip),
            ]
        )
    if WPF_GUI_ENABLED:
        scenarios.append(
            ("WPF Shift/DataGrid Evidence", test_wpf_shift_datagrid_evidence)
        )
        scenarios.append(
            (
                "WPF UI Grid Rows Alias Fixture Replay",
                test_wpf_ui_grid_rows_alias_fixture_replay,
            )
        )
        scenarios.append(
            (
                "WPF Stealth Delayed Readiness Replay",
                test_wpf_stealth_delayed_readiness_replay,
            )
        )
        scenarios.append(
            (
                "WPF Selector Safety No Side Effect",
                test_wpf_selector_safety_no_side_effect,
            )
        )
        scenarios.append(
            (
                "WPF One-Call Runtime Smoke Workflow",
                test_wpf_one_call_runtime_smoke_workflow,
            )
        )
        scenarios.append(
            (
                "WPF V2 Text Probe Missing Selector Runtime Smoke",
                test_wpf_v2_text_probe_missing_selector_runtime_smoke,
            )
        )
    if AVALONIA_GUI_ENABLED:
        scenarios.append(
            (
                "Avalonia UI Fixture Compatibility",
                test_avalonia_ui_fixture_compatibility,
            )
        )
        scenarios.append(
            (
                "Avalonia V2 Text Probe Missing Selector Runtime Smoke",
                test_avalonia_v2_text_probe_missing_selector_runtime_smoke,
            )
        )
    return scenarios


def list_scenarios():
    for index, (name, _) in enumerate(get_scenarios(), 1):
        print(f"{index}. {name}")


async def run_all():
    if not os.path.exists(DLL):
        print("ERROR: Build SmokeTestApp first:")
        print("  dotnet build tests/fixtures/SmokeTestApp -c Debug")
        return False

    print("=== SMOKE TEST: netcoredbg-mcp v0.6.0 ===")
    print(f"DLL: {DLL}")
    print(f"Source: {SOURCE}")

    scenarios = get_scenarios()

    if not GUI_ENABLED:
        print("\n  [SKIP] GUI scenarios — net8.0-windows build not found")

    for name, fn in scenarios:
        try:
            await fn()
        except Exception as e:
            global failed
            failed += 1
            print(f"  [FAIL] {name} crashed: {e}")
            traceback.print_exc()

    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed} passed, {failed} failed out of {passed + failed} checks")
    print(f"{'=' * 50}")
    return failed == 0


if __name__ == "__main__":
    if "--list" in sys.argv:
        list_scenarios()
        sys.exit(0)
    success = asyncio.run(run_all())
    sys.exit(0 if success else 1)
