from __future__ import annotations

import pytest

from netcoredbg_mcp.session.tracepoint_policy import tracepoint_expression_policy_error


@pytest.mark.parametrize(
    "expression",
    [
        "Mode.SpellCheckInput",
        "Items[0].Name",
        'Items["current"].Value',
        'Items[ "current" ].Value',
        'Settings["new"]',
        'Settings["a=b"]',
        "Count + 1",
        "Total / Count",
        "-1",
        "-Count",
        '"status=ready"',
        "'call(foo)'",
    ],
)
def test_tracepoint_policy_allows_read_only_expression_matrix(expression: str) -> None:
    assert tracepoint_expression_policy_error(expression) is None


@pytest.mark.parametrize(
    "expression",
    [
        "",
        "Mode.Reset()",
        "Mode.Value = 1",
        "Mode.Value++",
        "await Mode.Value",
        "new Mode()",
        "throw Mode.Value",
        "delete Mode.Value",
        "Mode.Value; Other.Value",
    ],
)
def test_tracepoint_policy_rejects_mutating_or_call_expression_matrix(
    expression: str,
) -> None:
    assert tracepoint_expression_policy_error(expression) is not None
