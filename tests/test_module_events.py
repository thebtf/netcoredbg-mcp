"""Tests for module event tracking."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from netcoredbg_mcp.dap.protocol import DAPEvent


class TestModuleEvents:
    """Tests for _on_module event handler."""

    @pytest.fixture
    def manager(self):
        with patch("netcoredbg_mcp.session.manager.DAPClient"):
            from netcoredbg_mcp.session import SessionManager
            m = SessionManager()
            return m

    def test_module_new_adds_to_list(self, manager):
        """New module event adds module to state."""
        event = DAPEvent(seq=1, event="module", body={
            "reason": "new",
            "module": {
                "id": 1,
                "name": "MyApp.dll",
                "path": "/app/MyApp.dll",
                "version": "1.0.0",
                "isOptimized": False,
                "symbolStatus": "loaded",
            },
        })
        manager._on_module(event)

        assert len(manager.state.modules) == 1
        assert manager.state.modules[0].name == "MyApp.dll"
        assert manager.state.modules[0].path == "/app/MyApp.dll"
        assert manager.state.modules[0].version == "1.0.0"
        assert manager.state.modules[0].symbol_status == "loaded"

    def test_module_changed_updates(self, manager):
        """Changed module event updates existing module."""
        # Add initial
        event_new = DAPEvent(seq=1, event="module", body={
            "reason": "new",
            "module": {"id": 1, "name": "MyApp.dll", "symbolStatus": "not loaded"},
        })
        manager._on_module(event_new)

        # Update
        event_changed = DAPEvent(seq=2, event="module", body={
            "reason": "changed",
            "module": {"id": 1, "name": "MyApp.dll", "symbolStatus": "loaded"},
        })
        manager._on_module(event_changed)

        assert len(manager.state.modules) == 1
        assert manager.state.modules[0].symbol_status == "loaded"

    def test_module_removed(self, manager):
        """Removed module event removes from list."""
        event_new = DAPEvent(seq=1, event="module", body={
            "reason": "new",
            "module": {"id": 1, "name": "MyApp.dll"},
        })
        manager._on_module(event_new)
        assert len(manager.state.modules) == 1

        event_removed = DAPEvent(seq=2, event="module", body={
            "reason": "removed",
            "module": {"id": 1, "name": "MyApp.dll"},
        })
        manager._on_module(event_removed)
        assert len(manager.state.modules) == 0

    def test_module_duplicate_prevented(self, manager):
        """Duplicate module events don't create duplicates."""
        event = DAPEvent(seq=1, event="module", body={
            "reason": "new",
            "module": {"id": 1, "name": "MyApp.dll"},
        })
        manager._on_module(event)
        manager._on_module(event)

        assert len(manager.state.modules) == 1

    def test_module_before_initialize(self, manager):
        """Module event before initialize doesn't crash."""
        event = DAPEvent(seq=1, event="module", body={
            "reason": "new",
            "module": {"id": 1, "name": "System.dll"},
        })
        # Should not raise even without initialization
        manager._on_module(event)
        assert len(manager.state.modules) == 1

    def test_module_to_dict(self):
        """ModuleInfo.to_dict() returns correct format."""
        from netcoredbg_mcp.session.state import ModuleInfo
        m = ModuleInfo(
            id=1, name="MyApp.dll", path="/app/MyApp.dll",
            version="1.0.0", is_optimized=True, symbol_status="loaded",
        )
        d = m.to_dict()
        assert d["name"] == "MyApp.dll"
        assert d["isOptimized"] is True
        assert d["symbolStatus"] == "loaded"

    @pytest.mark.asyncio
    async def test_get_modules_returns_all_6_fields(self, manager):
        """get_modules exposes the complete CR-002 module field set."""
        from netcoredbg_mcp.session.state import ModuleInfo
        from netcoredbg_mcp.tools.inspection import register_inspection_tools

        class ToolRegistry:
            def __init__(self):
                self.tools = {}

            def tool(self, *args, **kwargs):
                def decorator(func):
                    self.tools[func.__name__] = func
                    return func
                return decorator

        manager.state.modules = [
            ModuleInfo(
                id=1,
                name="MyApp.dll",
                path="/app/MyApp.dll",
                version="1.0.0",
                is_optimized=False,
                symbol_status="loaded",
            ),
            ModuleInfo(
                id="System.Private.CoreLib",
                name="System.Private.CoreLib.dll",
                path="/shared/System.Private.CoreLib.dll",
                version="8.0.0",
                is_optimized=True,
                symbol_status="symbols skipped",
            ),
        ]
        registry = ToolRegistry()
        register_inspection_tools(registry, manager, lambda ctx: None)

        response = await registry.tools["get_modules"]()

        required = {"id", "name", "path", "version", "isOptimized", "symbolStatus"}
        for module in response["data"]["modules"]:
            assert required <= set(module)
