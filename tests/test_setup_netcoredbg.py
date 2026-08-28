"""NetCoreDbg downloader failure-contract tests."""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.error import URLError

import pytest

from netcoredbg_mcp.setup import netcoredbg


class _StaticResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> _StaticResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        return self._body


class _PartialFailureResponse:
    def __init__(self, error: OSError) -> None:
        self._error = error
        self._returned_partial = False

    def __enter__(self) -> _PartialFailureResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self, _size: int = -1) -> bytes:
        if not self._returned_partial:
            self._returned_partial = True
            return b"partial"
        raise self._error


class _KnownTemporaryFile:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._file = None

    def __enter__(self):
        self._file = self._path.open("wb")
        return self._file

    def __exit__(self, *_args: object) -> None:
        assert self._file is not None
        self._file.close()


@pytest.mark.parametrize("error_type", [URLError, OSError], ids=["url-error", "os-error"])
def test_get_latest_release_info_returns_none_for_network_errors(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[OSError],
) -> None:
    monkeypatch.setattr(netcoredbg, "_detect_platform", lambda: "win64")

    def raise_network_error(*_args: object, **_kwargs: object) -> None:
        raise error_type("offline")

    monkeypatch.setattr(netcoredbg, "urlopen", raise_network_error)
    caplog.set_level(logging.WARNING, logger=netcoredbg.__name__)

    assert netcoredbg.get_latest_release_info() is None
    assert "Failed to query GitHub API" in caplog.text


def test_get_latest_release_info_returns_none_for_malformed_json(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(netcoredbg, "_detect_platform", lambda: "win64")
    monkeypatch.setattr(netcoredbg, "urlopen", lambda *_args, **_kwargs: _StaticResponse(b"{"))
    caplog.set_level(logging.WARNING, logger=netcoredbg.__name__)

    assert netcoredbg.get_latest_release_info() is None
    assert "Failed to query GitHub API" in caplog.text


@pytest.mark.parametrize("error_type", [URLError, OSError], ids=["url-error", "os-error"])
def test_download_netcoredbg_cleans_partial_file_after_read_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    error_type: type[OSError],
) -> None:
    partial_path = tmp_path / "partial.download"
    monkeypatch.setattr(
        netcoredbg,
        "get_latest_release_info",
        lambda: ("https://example.test/netcoredbg.zip", "v-test", 100),
    )
    monkeypatch.setattr(
        netcoredbg,
        "urlopen",
        lambda *_args, **_kwargs: _PartialFailureResponse(error_type("interrupted")),
    )
    monkeypatch.setattr(
        netcoredbg.tempfile,
        "NamedTemporaryFile",
        lambda **_kwargs: _KnownTemporaryFile(partial_path),
    )
    caplog.set_level(logging.WARNING, logger=netcoredbg.__name__)

    assert netcoredbg.download_netcoredbg(tmp_path / "target") is None
    assert not partial_path.exists()
    assert "Download failed" in caplog.text
