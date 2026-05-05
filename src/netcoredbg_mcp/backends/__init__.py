"""Debugger backend capability abstractions."""

from .protocol import DebuggerBackend, NetcoredbgBackend

__all__ = ["DebuggerBackend", "NetcoredbgBackend"]
