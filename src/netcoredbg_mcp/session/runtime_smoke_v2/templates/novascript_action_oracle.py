from __future__ import annotations

from typing import Any

from ._helpers import (
    action_from_record,
    file_json_probes_from_record,
    render_case_id,
    settle_from_record,
)


def render_novascript_action_oracle(
    record: dict[str, Any],
    id_pattern: str,
) -> dict[str, Any]:
    transition: dict[str, Any] = {
        "probes": file_json_probes_from_record(record),
    }
    action = action_from_record(record)
    if action is not None:
        transition["action"] = action
    settle = settle_from_record(record)
    if settle is not None:
        transition["settle"] = settle

    return {
        "id": render_case_id(id_pattern, record),
        "transitions": [transition],
    }
