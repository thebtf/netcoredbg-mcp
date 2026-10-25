"""Shared fixtures for the critical suite.

Re-exports the session-scoped Release-host build fixture from
``tests/test_host_proxy.py`` so ``tests/critical/test_host_proxy_critical.py``
reuses the exact same build path instead of a second, parallel one.
"""

from __future__ import annotations

import pytest

from tests.test_host_proxy import host_dll  # noqa: F401 -- re-exported pytest fixture


@pytest.fixture(scope="session")
def two_application_gui_evidence(tmp_path_factory: pytest.TempPathFactory) -> dict:
    from tests.critical.test_typed_bitblt_fallback_public import run_two_application_verification

    return run_two_application_verification(tmp_path_factory.mktemp("two-application-gui"))
