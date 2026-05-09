from __future__ import annotations

from typing import Any

from ..blocked import build_blocked
from ._common import blocked_probe, evidence_ref, probe_name, service_available


async def handle_output_since(
    probe: dict[str, Any],
    context: Any,
    *,
    phase: str,
) -> dict[str, Any]:
    kind = "output.since"
    checkpoint = str(probe.get("checkpoint") or "default")
    required = [str(item) for item in probe.get("required") or []]
    forbidden = [str(item) for item in probe.get("forbidden") or []]
    regex = bool(probe.get("regex", True))
    max_matches = int(probe.get("max_matches", 20))
    if service_available(context, "output_assert_since"):
        result = await context.call_adapter(
            "output_assert_since",
            checkpoint=checkpoint,
            required=required,
            forbidden=forbidden,
            regex=regex,
            max_matches=max_matches,
        )
    else:
        result = _assert_since_session(
            context.session,
            checkpoint=checkpoint,
            required=required,
            forbidden=forbidden,
            regex=regex,
            max_matches=max_matches,
        )
        if result.get("status") == "BLOCKED":
            return blocked_probe(
                probe,
                kind=kind,
                requested={"checkpoint": checkpoint},
                next_step="Create an output checkpoint before running output.since.",
            )
    status = str(result.get("status", "PASS"))
    value = {
        "matched_line_count": len(result.get("matches") or []),
        "missing_required": list(result.get("missing_required") or []),
        "forbidden_matches": list(result.get("forbidden_matches") or []),
    }
    output = {
        "name": probe_name(probe, kind),
        "kind": kind,
        "status": status,
        "value": value,
    }
    if status != "PASS":
        output["reason"] = result.get("reason", "output assertion failed")
    ref = evidence_ref(result)
    if ref:
        output["evidence_ref"] = ref
    if status == "BLOCKED":
        output.update(
            build_blocked(
                reason=str(output.get("reason") or "output probe blocked"),
                requested={"checkpoint": checkpoint},
                accepted={"checkpoint": "existing output checkpoint name"},
                next_step="Create the checkpoint before running this output.since probe.",
            )
        )
    return output


def _assert_since_session(
    session: Any,
    *,
    checkpoint: str,
    required: list[str],
    forbidden: list[str],
    regex: bool,
    max_matches: int,
) -> dict[str, Any]:
    from ...output_assertions import OutputAssertionService

    if getattr(session, "runtime_smoke", None) is None or getattr(session, "state", None) is None:
        return {"status": "BLOCKED", "reason": "output assertion service unavailable"}
    return OutputAssertionService(session).assert_since(
        checkpoint,
        required=required,
        forbidden=forbidden,
        regex=regex,
        max_matches=max_matches,
    ).to_dict()
