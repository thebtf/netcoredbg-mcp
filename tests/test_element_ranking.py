"""Tests for element ranking (FindAllCascade) and text extraction backends."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from netcoredbg_mcp.ui.flaui_client import FlaUIBackend


class TestFlaUIFindAllCascade:

    @pytest.mark.asyncio
    async def test_delegates_to_bridge(self):
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={
            "results": [
                {"found": True, "automationId": "btn1", "name": "Open", "score": 120, "depth": 1},
                {"found": True, "automationId": "btn2", "name": "Open", "score": 30, "depth": 5},
            ],
            "totalMatches": 2,
        })
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.find_all_cascade(name="Open", control_type="Button")
        assert result["totalMatches"] == 2
        assert len(result["results"]) == 2
        assert result["results"][0]["score"] > result["results"][1]["score"]

    @pytest.mark.asyncio
    async def test_passes_root_id(self):
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"results": [], "totalMatches": 0})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.find_all_cascade(name="Save", root_id="panel1")
        args = backend._client.call.call_args
        assert args[0][1]["rootAutomationId"] == "panel1"

    @pytest.mark.asyncio
    async def test_max_results(self):
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"results": [], "totalMatches": 0})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.find_all_cascade(name="Test", max_results=3)
        args = backend._client.call.call_args
        assert args[0][1]["maxResults"] == 3


class TestFlaUIExtractText:

    @pytest.mark.asyncio
    async def test_delegates_to_bridge(self):
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={
            "text": "Hello World",
            "source": "ValuePattern",
        })
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.extract_text(automation_id="txt1")
        assert result["text"] == "Hello World"
        assert result["source"] == "ValuePattern"

    @pytest.mark.asyncio
    async def test_passes_search_params(self):
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"text": "", "source": "None"})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.extract_text(name="label1", root_id="panel1")
        args = backend._client.call.call_args
        assert args[0][1]["name"] == "label1"
        assert args[0][1]["rootAutomationId"] == "panel1"


class TestFlaUIFindAllCascadeSingleResult:
    """Critical path: single result from find_all_cascade (no ambiguity)."""

    @pytest.mark.asyncio
    async def test_single_result_returned(self):
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={
            "results": [
                {"found": True, "automationId": "btn1", "name": "OK", "score": 100, "depth": 2},
            ],
            "totalMatches": 1,
        })
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.find_all_cascade(name="OK", control_type="Button")
        assert result["totalMatches"] == 1
        assert len(result["results"]) == 1

    @pytest.mark.asyncio
    async def test_no_criteria_still_sends_max_results(self):
        """find_all_cascade with no name/control_type still passes maxResults to bridge."""
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"results": [], "totalMatches": 0})
        backend._element_cache = {}
        backend._process_id = 1234

        await backend.find_all_cascade(max_results=7)
        args = backend._client.call.call_args
        assert args[0][1]["maxResults"] == 7
        # name and controlType should not be present when not provided
        assert "name" not in args[0][1]
        assert "controlType" not in args[0][1]


class TestFlaUIExtractTextXpath:
    """Critical path: extract_text with xpath passes through to bridge."""

    @pytest.mark.asyncio
    async def test_xpath_passed_to_bridge(self):
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"text": "label text", "source": "Name"})
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.extract_text(xpath="//Label[@AutomationId='lbl1']")
        assert result["text"] == "label text"
        args = backend._client.call.call_args
        assert args[0][1]["xpath"] == "//Label[@AutomationId='lbl1']"

    @pytest.mark.asyncio
    async def test_empty_result_returns_none_source(self):
        """extract_text returns source=None when bridge finds no text."""
        backend = FlaUIBackend.__new__(FlaUIBackend)
        backend._client = AsyncMock()
        backend._client.call = AsyncMock(return_value={"text": "", "source": "None"})
        backend._element_cache = {}
        backend._process_id = 1234

        result = await backend.extract_text(automation_id="unknownElem")
        assert result["text"] == ""
        assert result["source"] == "None"


class TestBuildSearchParams:

    def test_includes_all_fields(self):
        params = FlaUIBackend._build_search_params(
            automation_id="btn1", name="Save", control_type="Button",
            root_id="panel1", xpath="//Button",
        )
        assert params == {
            "automationId": "btn1",
            "name": "Save",
            "controlType": "Button",
            "rootAutomationId": "panel1",
            "xpath": "//Button",
        }

    def test_omits_none_values(self):
        params = FlaUIBackend._build_search_params(name="Test")
        assert params == {"name": "Test"}
        assert "automationId" not in params
