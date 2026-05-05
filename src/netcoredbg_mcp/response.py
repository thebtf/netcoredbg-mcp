"""Response builders for MCP tool responses.

Provides consistent response format with state machine awareness,
next_actions hints, and diagnostic information for AI agent consumers.
"""

from __future__ import annotations

from typing import Any

from .session.state import DebugState

# Valid actions per state — the agent should only call these tools in the given state.
VALID_ACTIONS: dict[str, list[str]] = {
    DebugState.IDLE.value: [
        "start_debug", "attach_debug", "get_progress",
    ],
    DebugState.INITIALIZING.value: [
        "get_debug_state", "get_progress", "stop_debug",
    ],
    DebugState.CONFIGURED.value: [
        "get_debug_state", "get_progress", "stop_debug", "add_breakpoint",
        "add_function_breakpoint", "get_loaded_sources",
    ],
    DebugState.RUNNING.value: [
        "pause_execution", "get_output", "get_output_tail", "search_output",
        "get_debug_state", "get_progress", "stop_debug", "add_breakpoint",
        "add_function_breakpoint", "get_loaded_sources",
    ],
    DebugState.STOPPED.value: [
        "get_call_stack", "get_scopes", "get_variables", "evaluate_expression",
        "get_exception_info", "step_over", "step_into", "step_out",
        "continue_execution", "add_breakpoint", "add_function_breakpoint",
        "remove_breakpoint", "clear_breakpoints", "list_breakpoints",
        "set_variable", "get_progress", "read_memory", "write_memory", "stop_debug",
        "get_loaded_sources", "disassemble", "get_locations",
        "ui_get_window_tree", "ui_find_element", "ui_click",
        "ui_send_keys", "ui_send_keys_focused", "ui_set_focus",
    ],
    DebugState.TERMINATED.value: [
        "get_output", "get_output_tail", "search_output",
        "get_progress", "get_loaded_sources", "stop_debug", "start_debug",
    ],
}

# Human-readable state messages
STATE_MESSAGES: dict[str, str] = {
    DebugState.IDLE.value: "No active debug session.",
    DebugState.INITIALIZING.value: "Debug session is initializing.",
    DebugState.CONFIGURED.value: "Debug session configured. Breakpoints set, awaiting launch.",
    DebugState.RUNNING.value: (
        "Program is RUNNING. Variable references from previous stops are INVALID. "
        "Do NOT call get_variables — wait for the program to stop."
    ),
    DebugState.STOPPED.value: (
        "Program is PAUSED. UI is frozen, app cannot respond to input. "
        "Inspect state with get_call_stack/get_variables, then resume."
    ),
    DebugState.TERMINATED.value: (
        "Program has terminated. Check output for errors, then stop the session."
    ),
}


def build_response(
    data: dict[str, Any] | None = None,
    *,
    state: DebugState | str,
    next_actions: list[str] | None = None,
    message: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """Build a standard tool response with state machine awareness.

    Args:
        data: Tool-specific result data (merged into response)
        state: Current debug state
        next_actions: Override suggested next actions (defaults to state-based lookup)
        message: Override state message
        **extra: Additional top-level fields

    Returns:
        Response dict with state, next_actions, message, and data fields.
    """
    state_value = state.value if isinstance(state, DebugState) else state

    result: dict[str, Any] = {
        "state": state_value,
        "next_actions": next_actions or VALID_ACTIONS.get(state_value, []),
        "message": message or STATE_MESSAGES.get(state_value, ""),
    }

    if data is not None:
        result["data"] = data

    result.update(extra)
    return result


def build_error_response(
    error: str,
    *,
    state: DebugState | str,
    next_actions: list[str] | None = None,
) -> dict[str, Any]:
    """Build an error response with recovery hints.

    Args:
        error: Error message
        state: Current debug state
        next_actions: Override recovery actions

    Returns:
        Response dict with error and recovery information.
    """
    state_value = state.value if isinstance(state, DebugState) else state

    recovery_actions = next_actions or VALID_ACTIONS.get(state_value, ["get_debug_state", "stop_debug"])

    return {
        "error": error,
        "state": state_value,
        "next_actions": recovery_actions,
        "message": f"Error: {error}. Try one of the suggested next_actions.",
    }
