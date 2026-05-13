"""Apply Roslyn Edit-and-Continue deltas through netcoredbg DAP."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ApplyDeltasResult:
    success: bool
    message: str | None
    body: dict[str, Any]


async def apply_deltas(
    dap_client: Any,
    dll_name: str,
    metadata_path: str,
    il_path: str,
    pdb_path: str,
    line_updates_path: str | None,
    *,
    timeout: float = 30.0,
) -> ApplyDeltasResult:
    """Send the custom netcoredbg applyDeltas DAP request."""
    arguments = {
        "dllFileName": dll_name,
        "metadataPath": metadata_path,
        "ilPath": il_path,
        "pdbPath": pdb_path,
    }
    if line_updates_path:
        arguments["lineUpdatesPath"] = line_updates_path

    response = await dap_client.send_request("applyDeltas", arguments, timeout=timeout)
    return ApplyDeltasResult(
        success=bool(response.success),
        message=response.message,
        body=dict(response.body) if response.body is not None else {},
    )
