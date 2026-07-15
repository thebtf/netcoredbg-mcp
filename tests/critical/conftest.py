"""Shared fixtures for the critical suite.

Re-exports the session-scoped Release-host build fixture from
``tests/test_host_proxy.py`` so ``tests/critical/test_host_proxy_critical.py``
reuses the exact same build path instead of a second, parallel one.
"""

from __future__ import annotations

from tests.test_host_proxy import host_dll  # noqa: F401 -- re-exported pytest fixture
