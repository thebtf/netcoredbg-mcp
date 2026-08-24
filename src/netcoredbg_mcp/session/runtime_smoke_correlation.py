from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Any

CORRELATION_SCHEMA = "netcoredbg.runtime_smoke.correlation.v1"
MEDIA_INSTANCE_SCOPE = "media_instance"
NOT_COMPARABLE = "NOT_COMPARABLE"
SAME_MEDIA_INSTANCE = "SAME_MEDIA_INSTANCE"


def validate_correlation_policy(value: Any, *, path: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, Mapping):
        return [f"{path}.correlation must be an object"]

    errors: list[str] = []
    unknown_keys = sorted(set(value) - {"scope", "required_sources"})
    if unknown_keys:
        errors.append(f"{path}.correlation has unsupported fields: {unknown_keys}")
    if value.get("scope") != MEDIA_INSTANCE_SCOPE:
        errors.append(f"{path}.correlation.scope must be {MEDIA_INSTANCE_SCOPE}")
    sources = value.get("required_sources")
    if not isinstance(sources, list) or not sources:
        errors.append(f"{path}.correlation.required_sources must be a non-empty list")
        return errors

    seen: set[str] = set()
    for index, source in enumerate(sources):
        source_path = f"{path}.correlation.required_sources[{index}]"
        if not isinstance(source, str) or not source.strip():
            errors.append(f"{source_path} must be a non-empty string")
            continue
        if source in seen:
            errors.append(f"{source_path} duplicates earlier source: {source}")
            continue
        seen.add(source)
    return errors


def validate_sample_correlation(value: Any, *, path: str) -> list[str]:
    _, reason = _media_identity(value)
    return [] if reason is None else [f"{path}.correlation {reason}"]


def redact_raw_correlation(
    value: Any,
    *,
    sensitive_values: frozenset[str] = frozenset(),
    redact_condition_values: bool = False,
) -> Any:
    if isinstance(value, dict):
        is_condition_metadata = isinstance(value.get("jsonpath"), str)
        condition_values_are_sensitive = is_condition_metadata and (
            redact_condition_values or _condition_targets_raw_media_identity(value)
        )
        redacted = {
            key: redact_raw_correlation(
                item,
                sensitive_values=sensitive_values,
                redact_condition_values=redact_condition_values,
            )
            for key, item in value.items()
            if not _is_raw_media_identity_field(key, item)
            and not (condition_values_are_sensitive and key in {"expected", "value"})
        }
        if condition_values_are_sensitive and any(key in value for key in {"expected", "value"}):
            redacted["redacted_fields"] = [key for key in ("expected", "value") if key in value]
        return redacted
    if isinstance(value, list):
        return [
            redact_raw_correlation(
                item,
                sensitive_values=sensitive_values,
                redact_condition_values=redact_condition_values,
            )
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            redact_raw_correlation(
                item,
                sensitive_values=sensitive_values,
                redact_condition_values=redact_condition_values,
            )
            for item in value
        )
    if isinstance(value, str):
        return _redact_sensitive_tokens(value, sensitive_values)
    return value


def _is_raw_media_identity_field(key: str, value: Any) -> bool:
    if key in {"media_engine_id", "media_instance_id"}:
        return True
    if key != "correlation" or not isinstance(value, Mapping):
        return False
    return value.get("schema") == CORRELATION_SCHEMA or (
        "media_engine_id" in value and "media_instance_id" in value
    )


def _condition_targets_raw_media_identity(value: Mapping[str, Any]) -> bool:
    jsonpath = value.get("jsonpath")
    return isinstance(jsonpath, str) and any(
        field in jsonpath for field in ("media_engine_id", "media_instance_id")
    )


def _redact_sensitive_tokens(value: str, sensitive_values: frozenset[str]) -> str:
    redacted = value
    for candidate in sorted(sensitive_values, key=len, reverse=True):
        pattern = rf"(?<![A-Za-z0-9_]){re.escape(candidate)}(?![A-Za-z0-9_])"
        redacted = re.sub(pattern, "<redacted correlation identity>", redacted)
    return redacted


def _raw_media_identity_values(value: Any) -> frozenset[str]:
    if not isinstance(value, Mapping):
        return frozenset()
    return frozenset(
        raw
        for field in ("media_engine_id", "media_instance_id")
        if (raw := _text_or_none(value.get(field))) is not None
    )


def attach_sample_correlation(
    result: dict[str, Any],
    raw_correlation: Any,
    *,
    provenance: Mapping[str, Any],
    source_label: str | None = None,
) -> dict[str, Any]:
    raw_identity_values = _raw_media_identity_values(raw_correlation)
    media_identity, reason = _media_identity(raw_correlation)
    emitted_values = _provenance_scalar_values(provenance)
    if source_label is not None:
        emitted_values.add(source_label)
    if media_identity is not None and raw_identity_values & emitted_values:
        media_identity = None
        reason = "media identity collides with emitted provenance"
    output = redact_raw_correlation(
        result,
        sensitive_values=raw_identity_values,
        redact_condition_values=True,
    )
    output["correlation"] = {
        "schema": CORRELATION_SCHEMA,
        "status": "OBSERVED" if media_identity is not None else NOT_COMPARABLE,
        "provenance": _copy_provenance(provenance),
        "media_identity": media_identity,
    }
    if reason is not None:
        output["correlation"]["reason"] = reason
    return output


def _provenance_scalar_values(value: Any) -> set[str]:
    if isinstance(value, Mapping):
        return {scalar for item in value.values() for scalar in _provenance_scalar_values(item)}
    if isinstance(value, (list, tuple, set)):
        return {scalar for item in value for scalar in _provenance_scalar_values(item)}
    if isinstance(value, (str, int)) and not isinstance(value, bool):
        return {str(value)}
    return set()


def transition_correlation(
    policy: Mapping[str, Any] | None,
    samples: Sequence[Mapping[str, Any]],
) -> dict[str, Any] | None:
    if policy is None:
        return None

    required_sources = [str(source) for source in policy.get("required_sources", [])]
    selected: dict[str, list[Mapping[str, Any]]] = {source: [] for source in required_sources}
    for sample in samples:
        source = str(sample.get("correlation_source") or "")
        if source in selected:
            selected[source].append(sample)
    missing_sources = [source for source in required_sources if not selected[source]]
    ambiguous_sources = [source for source in required_sources if len(selected[source]) > 1]
    invalid_sources = [
        source
        for source in required_sources
        if len(selected[source]) == 1 and _sample_status(selected[source][0]) != "OBSERVED"
    ]
    if missing_sources or ambiguous_sources or invalid_sources:
        return _not_comparable(
            required_sources,
            reason="required correlation identity is unavailable",
            missing_sources=missing_sources,
            invalid_sources=invalid_sources,
            ambiguous_sources=ambiguous_sources,
        )

    sample_envelopes = [selected[source][0]["correlation"] for source in required_sources]
    identities = [sample["media_identity"] for sample in sample_envelopes]
    provenance = [sample["provenance"] for sample in sample_envelopes]
    if not _same_core_provenance(provenance) or not _same_identity(identities):
        return _not_comparable(
            required_sources,
            reason="correlation tuples are unequal",
        )

    return {
        "schema": CORRELATION_SCHEMA,
        "scope": MEDIA_INSTANCE_SCOPE,
        "status": "PASS",
        "comparison": SAME_MEDIA_INSTANCE,
        "identity": dict(identities[0]),
        "sources": required_sources,
    }


def action_sample_provenance(
    context: Any,
    *,
    raw_result: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    raw_result = raw_result or {}
    session = getattr(context, "session", None)
    state = getattr(session, "state", None)
    action_kind = getattr(context, "action_kind", None)
    action_id = getattr(context, "action_id", None)
    thread_value = raw_result.get("thread_id", getattr(state, "current_thread_id", None))
    frame_value = raw_result.get("frame_id", getattr(state, "current_frame_id", None))
    thread_id = _int_or_none(thread_value)
    frame_id = _int_or_none(frame_value)
    invalid_optional_fields: list[str] = []
    if thread_value is not None and thread_id is None:
        invalid_optional_fields.append("thread_id")
    if frame_value is not None and frame_id is None:
        invalid_optional_fields.append("frame_id")
    provenance: dict[str, Any] = {
        "session_id": _text_or_none(getattr(session, "session_id", None)),
        "debuggee": {
            "pid": _int_or_none(getattr(state, "process_id", None)),
            "epoch": _int_or_none(getattr(state, "activity_epoch_sequence", None)),
            "sequence": _int_or_none(getattr(state, "output_sequence", None)),
        },
        "run": {
            "id": _text_or_none(getattr(context, "run_id", None)),
            "case_id": _text_or_none(getattr(context, "case_id", None)),
            "transition_id": _text_or_none(getattr(context, "transition_id", None)),
            "transition_index": _int_or_none(getattr(context, "transition_index", None)),
            "action": {
                "id": _text_or_none(action_id),
                "kind": _text_or_none(action_kind),
            },
        },
        "thread_id": thread_id,
        "frame_id": frame_id,
    }
    if invalid_optional_fields:
        provenance["invalid_optional_fields"] = invalid_optional_fields
    return provenance


def correlation_source(value: Mapping[str, Any], *, fallback: str) -> str:
    source = value.get("correlation_source")
    if isinstance(source, str) and source.strip():
        return source
    return fallback


def validate_correlation_source(value: Mapping[str, Any], *, path: str) -> list[str]:
    if "correlation_source" not in value:
        return []
    source = value["correlation_source"]
    if isinstance(source, str) and source.strip():
        return []
    return [f"{path}.correlation_source must be a non-empty string"]


def _media_identity(value: Any) -> tuple[dict[str, str] | None, str | None]:
    if not isinstance(value, Mapping):
        return None, "product media identity was not observed"
    if value.get("schema") != CORRELATION_SCHEMA:
        return None, f"correlation.schema must be {CORRELATION_SCHEMA}"
    engine = _text_or_none(value.get("media_engine_id"))
    media = _text_or_none(value.get("media_instance_id"))
    if engine is None or media is None:
        return None, "media_engine_id and media_instance_id are required"
    return {
        "media_engine_sha256": _sha256(engine),
        "media_instance_sha256": _sha256(media),
    }, None


def _copy_provenance(value: Mapping[str, Any]) -> dict[str, Any]:
    debuggee = value.get("debuggee")
    run = value.get("run")
    action = run.get("action") if isinstance(run, Mapping) else None
    copied = {
        "session_id": _text_or_none(value.get("session_id")),
        "debuggee": {
            "pid": _int_or_none(debuggee.get("pid") if isinstance(debuggee, Mapping) else None),
            "epoch": _int_or_none(debuggee.get("epoch") if isinstance(debuggee, Mapping) else None),
            "sequence": _int_or_none(
                debuggee.get("sequence") if isinstance(debuggee, Mapping) else None
            ),
        },
        "run": {
            "id": _text_or_none(run.get("id") if isinstance(run, Mapping) else None),
            "case_id": _text_or_none(run.get("case_id") if isinstance(run, Mapping) else None),
            "transition_id": _text_or_none(
                run.get("transition_id") if isinstance(run, Mapping) else None
            ),
            "transition_index": _int_or_none(
                run.get("transition_index") if isinstance(run, Mapping) else None
            ),
            "action": {
                "id": _text_or_none(action.get("id") if isinstance(action, Mapping) else None),
                "kind": _text_or_none(action.get("kind") if isinstance(action, Mapping) else None),
            },
        },
        "thread_id": _int_or_none(value.get("thread_id")),
        "frame_id": _int_or_none(value.get("frame_id")),
    }
    invalid_optional_fields = value.get("invalid_optional_fields")
    if isinstance(invalid_optional_fields, list) and all(
        isinstance(field, str) for field in invalid_optional_fields
    ):
        copied["invalid_optional_fields"] = list(invalid_optional_fields)
    return copied


def _sample_status(sample: Mapping[str, Any]) -> str:
    correlation = sample.get("correlation")
    if not isinstance(correlation, Mapping):
        return NOT_COMPARABLE
    return str(correlation.get("status") or NOT_COMPARABLE)


def _same_core_provenance(provenance: Sequence[Mapping[str, Any]]) -> bool:
    if not provenance:
        return False
    first = _core_provenance_tuple(provenance[0])
    return first is not None and all(
        _core_provenance_tuple(candidate) == first for candidate in provenance[1:]
    )


def _core_provenance_tuple(
    value: Mapping[str, Any],
) -> tuple[str, int, int, int, str, str, str, int, str, str] | None:
    debuggee = value.get("debuggee")
    run = value.get("run")
    action = run.get("action") if isinstance(run, Mapping) else None
    if value.get("invalid_optional_fields"):
        return None
    fields = (
        _text_or_none(value.get("session_id")),
        _int_or_none(debuggee.get("pid") if isinstance(debuggee, Mapping) else None),
        _int_or_none(debuggee.get("epoch") if isinstance(debuggee, Mapping) else None),
        _int_or_none(debuggee.get("sequence") if isinstance(debuggee, Mapping) else None),
        _text_or_none(run.get("id") if isinstance(run, Mapping) else None),
        _text_or_none(run.get("case_id") if isinstance(run, Mapping) else None),
        _text_or_none(run.get("transition_id") if isinstance(run, Mapping) else None),
        _int_or_none(run.get("transition_index") if isinstance(run, Mapping) else None),
        _text_or_none(action.get("id") if isinstance(action, Mapping) else None),
        _text_or_none(action.get("kind") if isinstance(action, Mapping) else None),
    )
    if any(field is None for field in fields):
        return None
    return fields  # type: ignore[return-value]


def _same_identity(identities: Sequence[Mapping[str, Any]]) -> bool:
    if not identities:
        return False
    first = dict(identities[0])
    return all(dict(candidate) == first for candidate in identities[1:])


def _not_comparable(
    sources: Sequence[str],
    *,
    reason: str,
    missing_sources: Sequence[str] | None = None,
    invalid_sources: Sequence[str] | None = None,
    ambiguous_sources: Sequence[str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "schema": CORRELATION_SCHEMA,
        "scope": MEDIA_INSTANCE_SCOPE,
        "status": "BLOCKED",
        "comparison": NOT_COMPARABLE,
        "reason": reason,
        "sources": list(sources),
    }
    if missing_sources:
        result["missing_sources"] = list(missing_sources)
    if invalid_sources:
        result["invalid_sources"] = list(invalid_sources)
    if ambiguous_sources:
        result["ambiguous_sources"] = list(ambiguous_sources)
    return result


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text_or_none(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text or None


def _int_or_none(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value
