"""Tests for EnC support detection."""

from netcoredbg_mcp.enc.detect import detect_enc_support


def test_detect_enc_support_returns_supported_when_ncdbhook_is_adjacent(tmp_path):
    netcoredbg = tmp_path / "netcoredbg.exe"
    ncdbhook = tmp_path / "ncdbhook.dll"
    netcoredbg.write_text("exe", encoding="utf-8")
    ncdbhook.write_text("hook", encoding="utf-8")

    result = detect_enc_support(netcoredbg)

    assert result == {
        "supported": True,
        "ncdbhook_path": str(ncdbhook),
        "error": None,
    }


def test_detect_enc_support_returns_clear_error_when_ncdbhook_missing(tmp_path):
    netcoredbg = tmp_path / "netcoredbg.exe"
    netcoredbg.write_text("exe", encoding="utf-8")

    result = detect_enc_support(netcoredbg)

    assert result["supported"] is False
    assert result["ncdbhook_path"] is None
    assert "ncdbhook.dll" in result["error"]
    assert str(tmp_path) in result["error"]


def test_detect_enc_support_returns_clear_error_when_netcoredbg_missing(tmp_path):
    netcoredbg = tmp_path / "netcoredbg.exe"

    result = detect_enc_support(netcoredbg)

    assert result["supported"] is False
    assert result["ncdbhook_path"] is None
    assert "netcoredbg not found" in result["error"]
