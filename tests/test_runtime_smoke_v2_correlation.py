from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunner, RuntimeSmokeSession

_CORRELATION_SCHEMA = "netcoredbg.runtime_smoke.correlation.v1"
_ENGINE_HASH = "7c24d6c4ee89a2fb9d80094dffd2626d2b711f960b5513af89bdf95067584daa"
_MEDIA_HASH = "b5f7caf51f752ccd762e1318592fa6b3c7b10b23fbffd11ba4efe1b1cafa91f0"


def _media_identity(engine: str, media: str) -> dict[str, str]:
    return {
        "schema": _CORRELATION_SCHEMA,
        "media_engine_id": engine,
        "media_instance_id": media,
    }


class CorrelationSmokeSession:
    def __init__(
        self,
        *,
        action_identity: dict[str, str] | None,
        evaluate_identity: dict[str, str] | None,
        trace_identity: dict[str, str] | None,
    ) -> None:
        self.runtime_smoke = RuntimeSmokeSession()
        self.session_id = "debug-session-01"
        self.state = SimpleNamespace(
            process_id=404,
            activity_epoch_sequence=7,
            output_sequence=21,
            current_thread_id=17,
            current_frame_id=31,
        )
        self._action_identity = action_identity
        self._evaluate_identity = evaluate_identity
        self._trace_identity = trace_identity

    async def invoke(self, **_: Any) -> dict[str, Any]:
        result: dict[str, Any] = {"status": "PASS", "invoked": True}
        if self._action_identity is not None:
            result["correlation"] = self._action_identity
        return result

    async def evaluate(self, **_: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "PASS",
            "value": "Playing",
            "thread_id": 17,
            "frame_id": 31,
        }
        if self._evaluate_identity is not None:
            result["correlation"] = self._evaluate_identity
        return result

    async def tracepoint(self, **_: Any) -> dict[str, Any]:
        result: dict[str, Any] = {
            "status": "PASS",
            "hit_count": 1,
            "logs": ["Playing"],
            "thread_id": 17,
            "frame_id": 31,
        }
        if self._trace_identity is not None:
            result["correlation"] = self._trace_identity
        return result


def _app_diagnostics(_identity: dict[str, str] | None) -> dict[str, Any]:
    return {
        "kind": "app_diagnostics",
        "name": "app-state",
        "phase": "after",
        "schema": "netcoredbg.runtime_smoke.diagnostics.v1",
        "app": {"name": "NovaScript"},
        "status": "PASS",
        "observations": [{"kind": "playback", "status": "PASS"}],
        "redaction": {"omit_fields": []},
        "limits": {
            "max_text_length": 240,
            "max_list_items": 8,
            "max_json_bytes": 32768,
        },
        "correlation_source": "app-state",
    }


def _plan(app_identity: dict[str, str] | None) -> dict[str, Any]:
    return {
        "schema": "netcoredbg.runtime_smoke.v2",
        "cases": [
            {
                "id": "media-playback",
                "transitions": [
                    {
                        "id": "play",
                        "settle": {"idle_ms": 0},
                        "correlation": {
                            "scope": "media_instance",
                            "required_sources": [
                                "play-action",
                                "engine-state",
                                "play-handler",
                            ],
                        },
                        "action": {
                            "id": "play-action",
                            "kind": "ui.invoke",
                            "selector": {"automation_id": "PlayPauseButton"},
                            "correlation_source": "play-action",
                        },
                        "probes": [
                            {
                                "kind": "debug.evaluate",
                                "name": "engine-state",
                                "phase": "after",
                                "expression": "mediaEngine.State",
                                "correlation_source": "engine-state",
                            },
                            {
                                "kind": "debug.tracepoint",
                                "name": "play-handler",
                                "phase": "after",
                                "file": "PlayerViewModel.cs",
                                "line": 42,
                                "expression": "mediaEngine.State",
                                "expected_hit_count": 1,
                                "correlation_source": "play-handler",
                            },
                            _app_diagnostics(app_identity),
                        ],
                    }
                ],
            }
        ],
    }


def _runner(
    session: CorrelationSmokeSession,
    *,
    run_id: str | None = "runtime-smoke-run-01",
) -> RuntimeSmokeRunner:
    return RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "debug.evaluate": session.evaluate,
            "debug.tracepoint": session.tracepoint,
        },
        run_id=run_id,
    )


def _sample_envelopes(result: dict[str, Any]) -> list[dict[str, Any]]:
    transition = result["cases"][0]["transitions"][0]
    return [
        transition["actions"][0]["correlation"],
        *[probe["correlation"] for probe in transition["probes"]["after"]],
    ]


@pytest.mark.asyncio
async def test_media_correlation_matching_sources_emits_safe_pass_envelope() -> None:
    identity = _media_identity("engine-01", "media-01")
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=identity,
            evaluate_identity=identity,
            trace_identity=identity,
        )
    ).run(_plan(identity))

    transition = result["cases"][0]["transitions"][0]

    assert transition["status"] == "PASS"
    assert transition["correlation"] == {
        "schema": _CORRELATION_SCHEMA,
        "scope": "media_instance",
        "status": "PASS",
        "comparison": "SAME_MEDIA_INSTANCE",
        "identity": {
            "media_engine_sha256": _ENGINE_HASH,
            "media_instance_sha256": _MEDIA_HASH,
        },
        "sources": [
            "play-action",
            "engine-state",
            "play-handler",
        ],
    }
    correlated_samples = _sample_envelopes(result)[:-1]
    assert all(
        sample["provenance"]
        == {
            "session_id": "debug-session-01",
            "debuggee": {"pid": 404, "epoch": 7, "sequence": 21},
            "run": {
                "id": "runtime-smoke-run-01",
                "case_id": "media-playback",
                "transition_id": "play",
                "transition_index": 0,
                "action": {"id": "play-action", "kind": "ui.invoke"},
            },
            "thread_id": 17,
            "frame_id": 31,
        }
        for sample in correlated_samples
    )
    assert all(
        sample["media_identity"]
        == {
            "media_engine_sha256": _ENGINE_HASH,
            "media_instance_sha256": _MEDIA_HASH,
        }
        for sample in correlated_samples
    )
    from netcoredbg_mcp.tools.runtime_smoke import _bounded_runtime_smoke_result

    assert result["correlations"] == [
        {
            "case_id": "media-playback",
            "transition_id": "play",
            "transition_index": 0,
            **transition["correlation"],
        }
    ]
    assert _bounded_runtime_smoke_result(result)["correlations"] == result["correlations"]
    assert "engine-01" not in str(result)
    assert "media-01" not in str(result)


@pytest.mark.asyncio
async def test_acquired_app_diagnostic_identity_can_prove_named_source(tmp_path: Path) -> None:
    identity = _media_identity("engine-01", "media-01")
    diagnostic_payload = _app_diagnostics(None)
    diagnostic_payload["correlation"] = identity
    diagnostic_payload["state"] = {"current_media": "engine-01"}
    diagnostic_path = tmp_path / "app-diagnostics.json"
    diagnostic_path.write_text(json.dumps(diagnostic_payload), encoding="utf-8")
    plan = _plan(None)
    transition = plan["cases"][0]["transitions"][0]
    transition["correlation"] = {
        "scope": "media_instance",
        "required_sources": ["app-state"],
    }
    transition["probes"][-1]["wait_json"] = {
        "path": str(diagnostic_path),
        "timeout_ms": 0,
        "poll_interval_ms": 0,
        "condition": {
            "jsonpath": "$.state.current_media",
            "expected": "engine-01",
        },
    }
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=None,
            evaluate_identity=None,
            trace_identity=None,
        )
    ).run(plan)

    observed = result["cases"][0]["transitions"][0]
    app_diagnostic = observed["probes"]["after"][-1]

    assert observed["correlation"]["comparison"] == "SAME_MEDIA_INSTANCE"
    assert app_diagnostic["correlation"]["status"] == "OBSERVED"
    assert "engine-01" not in str(result)
    assert "media-01" not in str(result)


@pytest.mark.asyncio
async def test_plan_controlled_app_identity_cannot_self_certify_source(tmp_path: Path) -> None:
    identity = _media_identity("engine-01", "media-01")
    diagnostic_path = tmp_path / "app-diagnostics-without-correlation.json"
    diagnostic_payload = _app_diagnostics(None)
    diagnostic_payload["state"] = {"current_media": "actual-media"}
    diagnostic_path.write_text(json.dumps(diagnostic_payload), encoding="utf-8")
    plan = _plan(None)
    transition = plan["cases"][0]["transitions"][0]
    transition["correlation"] = {
        "scope": "media_instance",
        "required_sources": ["app-state"],
    }
    transition["probes"][-1]["correlation"] = identity
    transition["probes"][-1]["wait_json"] = {
        "path": str(diagnostic_path),
        "condition": {
            "jsonpath": "$.state.current_media",
            "expected": "engine-01",
        },
        "timeout_ms": 0,
        "poll_interval_ms": 0,
    }
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=None,
            evaluate_identity=None,
            trace_identity=None,
        )
    ).run(plan)

    observed = result["cases"][0]["transitions"][0]

    assert observed["correlation"]["comparison"] == "NOT_COMPARABLE"
    assert "engine-01" not in str(result)
    assert "actual-media" not in str(result)


@pytest.mark.asyncio
async def test_blocked_diagnostic_details_redact_acquired_media_identity(tmp_path: Path) -> None:
    identity = _media_identity("engine-01", "media-01")
    diagnostic_payload = _app_diagnostics(None)
    diagnostic_payload["correlation"] = identity
    diagnostic_payload["status"] = "BLOCKED"
    diagnostic_payload["observations"] = [
        {
            "kind": "media.backend",
            "status": "BLOCKED",
            "reason": "engine-01 unavailable",
            "requested": {"media_engine_id": "engine-01"},
            "accepted": {"media_instance_id": "media-01"},
            "next_step": "Reconnect engine-01.",
        }
    ]
    diagnostic_path = tmp_path / "blocked-app-diagnostics.json"
    diagnostic_path.write_text(json.dumps(diagnostic_payload), encoding="utf-8")
    plan = _plan(None)
    transition = plan["cases"][0]["transitions"][0]
    transition["correlation"] = {
        "scope": "media_instance",
        "required_sources": ["app-state"],
    }
    transition["probes"][-1]["wait_json"] = {
        "path": str(diagnostic_path),
        "timeout_ms": 0,
        "poll_interval_ms": 0,
    }
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=None,
            evaluate_identity=None,
            trace_identity=None,
        )
    ).run(plan)

    probe = result["cases"][0]["transitions"][0]["probes"]["after"][-1]

    assert probe["status"] == "BLOCKED"
    assert "engine-01" not in str(probe)
    assert "media-01" not in str(probe)


@pytest.mark.asyncio
async def test_malformed_correlation_source_rejects_plan() -> None:
    identity = _media_identity("engine-01", "media-01")
    plan = _plan(identity)
    plan["cases"][0]["transitions"][0]["action"]["correlation_source"] = 123
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=identity,
            evaluate_identity=identity,
            trace_identity=identity,
        )
    ).run(plan)

    assert result["status"] == "INVALID_SETUP"
    assert result["run_id"].startswith("runtime-smoke-")
    assert any(
        "correlation_source must be a non-empty string" in error
        for error in result["validation_errors"]
    )


def test_sample_envelope_recursively_redacts_raw_identity_fields() -> None:
    from netcoredbg_mcp.session.runtime_smoke_correlation import attach_sample_correlation

    identity = _media_identity("engine-01", "media-01")
    result = attach_sample_correlation(
        {
            "value": {
                "nested": {"correlation": identity},
                "media_engine_id": "engine-01",
                "media_instance_id": "media-01",
            }
        },
        identity,
        provenance={
            "session_id": "session",
            "debuggee": {"pid": 1, "epoch": 1, "sequence": 1},
            "run": {
                "id": "run",
                "case_id": "case",
                "transition_id": "transition",
                "transition_index": 0,
                "action": {"id": "action", "kind": "ui.invoke"},
            },
            "thread_id": None,
            "frame_id": None,
        },
    )

    assert "engine-01" not in str(result)
    assert result["correlation"]["media_identity"]["media_engine_sha256"] == _ENGINE_HASH
    short_identity = _media_identity("a", "b")
    short_result = attach_sample_correlation(
        {"value": "Playing"},
        short_identity,
        provenance=result["correlation"]["provenance"],
    )
    assert short_result["value"] == "Playing"
    embedded_result = attach_sample_correlation(
        {"value": "playing media-01"},
        identity,
        provenance=result["correlation"]["provenance"],
    )
    assert "media-01" not in embedded_result["value"]
    ordinary_result = attach_sample_correlation(
        {"value": {"observation": {"correlation": {"request_id": "trace-42"}}}},
        None,
        provenance=result["correlation"]["provenance"],
    )
    assert ordinary_result["value"]["observation"]["correlation"] == {"request_id": "trace-42"}
    collision_result = attach_sample_correlation(
        {"value": "Playing"},
        _media_identity("action", "media-01"),
        provenance=result["correlation"]["provenance"],
    )
    assert collision_result["correlation"]["status"] == "NOT_COMPARABLE"
    assert collision_result["correlation"]["reason"] == (
        "media identity collides with emitted provenance"
    )
    source_collision_result = attach_sample_correlation(
        {"value": "Playing"},
        _media_identity("engine-01", "media-01"),
        provenance=result["correlation"]["provenance"],
        source_label="engine-01",
    )
    assert source_collision_result["correlation"]["status"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_media_correlation_mixed_identity_blocks_as_not_comparable() -> None:
    identity = _media_identity("engine-01", "media-01")
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=identity,
            evaluate_identity=identity,
            trace_identity=_media_identity("engine-01", "media-02"),
        )
    ).run(_plan(identity))

    transition = result["cases"][0]["transitions"][0]

    assert transition["status"] == "BLOCKED"
    assert transition["correlation"]["comparison"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_media_correlation_missing_identity_blocks_as_not_comparable() -> None:
    identity = _media_identity("engine-01", "media-01")
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=identity,
            evaluate_identity=None,
            trace_identity=identity,
        )
    ).run(_plan(identity))

    transition = result["cases"][0]["transitions"][0]

    assert transition["status"] == "BLOCKED"
    assert transition["correlation"]["comparison"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_media_correlation_duplicate_required_source_blocks_as_not_comparable() -> None:
    identity = _media_identity("engine-01", "media-01")
    plan = _plan(identity)
    transition = plan["cases"][0]["transitions"][0]
    transition["probes"].append(
        {
            "kind": "debug.evaluate",
            "name": "duplicate-engine-state",
            "phase": "after",
            "expression": "mediaEngine.State",
            "correlation_source": "engine-state",
        }
    )
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=identity,
            evaluate_identity=identity,
            trace_identity=identity,
        )
    ).run(plan)

    observed = result["cases"][0]["transitions"][0]

    assert observed["status"] == "BLOCKED"
    assert observed["correlation"]["ambiguous_sources"] == ["engine-state"]


@pytest.mark.asyncio
async def test_media_correlation_pid_alone_blocks_as_not_comparable() -> None:
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=None,
            evaluate_identity=None,
            trace_identity=None,
        )
    ).run(_plan(None))

    transition = result["cases"][0]["transitions"][0]

    assert transition["status"] == "BLOCKED"
    assert transition["correlation"]["comparison"] == "NOT_COMPARABLE"


def test_media_correlation_schema_contract_documents_provenance_and_comparison() -> None:
    from netcoredbg_mcp.session.runtime_smoke_schema import diagnostic_schema_contract

    contract = diagnostic_schema_contract()["correlation_envelope"]

    assert contract["schema"] == _CORRELATION_SCHEMA
    assert contract["transition_policy"]["comparison"] == {
        "PASS": "SAME_MEDIA_INSTANCE",
        "BLOCKED": "NOT_COMPARABLE",
    }
    assert contract["provenance"]["debuggee"] == ["pid", "epoch", "sequence"]
    assert contract["provenance"]["optional"] == ["thread_id", "frame_id"]
    assert contract["product_media_identity"]["emitted_fields"] == [
        "media_engine_sha256",
        "media_instance_sha256",
    ]
    assert "PID/epoch/sequence" in contract["comparison_rule"]


@pytest.mark.asyncio
async def test_lifecycle_run_id_reaches_correlation_samples() -> None:
    from netcoredbg_mcp.session.runtime_smoke import RuntimeSmokeRunRegistry

    identity = _media_identity("engine-01", "media-01")
    session = CorrelationSmokeSession(
        action_identity=identity,
        evaluate_identity=identity,
        trace_identity=identity,
    )
    registry = RuntimeSmokeRunRegistry()
    started = await registry.start(
        _plan(identity),
        lambda: RuntimeSmokeRunner(
            session,
            service_adapters={
                "ui.invoke": session.invoke,
                "debug.evaluate": session.evaluate,
                "debug.tracepoint": session.tracepoint,
            },
        ),
    )
    run_id = started["run_id"]
    for _ in range(20):
        result = await registry.get_result(run_id)
        if result.get("final"):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("runtime smoke lifecycle run did not finish")

    transition = result["cases"][0]["transitions"][0]

    assert transition["correlation"]["comparison"] == "SAME_MEDIA_INSTANCE"
    assert _sample_envelopes(result)[0]["provenance"]["run"]["id"] == run_id
    assert result["app_diagnostics_history"][0]["correlation"] == _sample_envelopes(result)[-1]


@pytest.mark.asyncio
async def test_one_shot_runner_generates_durable_correlation_run_id() -> None:
    identity = _media_identity("engine-01", "media-01")
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=identity,
            evaluate_identity=identity,
            trace_identity=identity,
        ),
        run_id=None,
    ).run(_plan(identity))

    generated_run_id = result["run_id"]

    assert generated_run_id.startswith("runtime-smoke-")
    assert _sample_envelopes(result)[0]["provenance"]["run"]["id"] == generated_run_id


@pytest.mark.asyncio
async def test_media_correlation_requires_debuggee_sequence() -> None:
    identity = _media_identity("engine-01", "media-01")
    session = CorrelationSmokeSession(
        action_identity=identity,
        evaluate_identity=identity,
        trace_identity=identity,
    )
    session.state.output_sequence = None
    result = await _runner(session).run(_plan(identity))

    transition = result["cases"][0]["transitions"][0]

    assert transition["status"] == "BLOCKED"
    assert transition["correlation"]["comparison"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_media_correlation_rejects_negative_debuggee_provenance() -> None:
    identity = _media_identity("engine-01", "media-01")
    session = CorrelationSmokeSession(
        action_identity=identity,
        evaluate_identity=identity,
        trace_identity=identity,
    )
    session.state.process_id = -1
    result = await _runner(session).run(_plan(identity))

    transition = result["cases"][0]["transitions"][0]

    assert transition["correlation"]["comparison"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_media_correlation_rejects_invalid_optional_provenance() -> None:
    identity = _media_identity("engine-01", "media-01")

    class InvalidThreadSession(CorrelationSmokeSession):
        async def evaluate(self, **kwargs: Any) -> dict[str, Any]:
            result = await super().evaluate(**kwargs)
            result["thread_id"] = -1
            return result

    result = await _runner(
        InvalidThreadSession(
            action_identity=identity,
            evaluate_identity=identity,
            trace_identity=identity,
        )
    ).run(_plan(identity))

    transition = result["cases"][0]["transitions"][0]

    assert transition["correlation"]["comparison"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_media_correlation_is_absent_without_transition_policy() -> None:
    identity = _media_identity("engine-01", "media-01")
    plan = _plan(identity)
    del plan["cases"][0]["transitions"][0]["correlation"]
    result = await _runner(
        CorrelationSmokeSession(
            action_identity=identity,
            evaluate_identity=identity,
            trace_identity=identity,
        )
    ).run(plan)

    transition = result["cases"][0]["transitions"][0]

    assert result["status"] == "PASS"
    assert "correlation" not in transition


@pytest.mark.asyncio
async def test_action_adapter_nested_identity_is_not_emitted() -> None:
    from netcoredbg_mcp.session.runtime_smoke_v2.actions import ActionContext

    identity = _media_identity("engine-01", "media-01")

    async def nested_adapter(**_: Any) -> dict[str, Any]:
        return {"status": "PASS", "nested": [{"correlation": identity}]}

    captured: list[dict[str, Any]] = []
    context = ActionContext(
        service_adapters={"nested": nested_adapter},
        clock=lambda: 0.0,
        action_adapter_results=captured,
    )
    result = await context.call_adapter("nested")

    assert "engine-01" not in str(result)
    assert captured[0]["nested"][0]["correlation"] == identity


@pytest.mark.asyncio
async def test_unavailable_tracepoint_still_emits_sample_envelope() -> None:
    identity = _media_identity("engine-01", "media-01")
    session = CorrelationSmokeSession(
        action_identity=identity,
        evaluate_identity=identity,
        trace_identity=identity,
    )
    runner = RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "debug.evaluate": session.evaluate,
        },
        run_id="runtime-smoke-run-01",
    )
    result = await runner.run(_plan(identity))

    tracepoint = result["cases"][0]["transitions"][0]["probes"]["after"][1]

    assert tracepoint["correlation"]["status"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_no_global_input_action_still_emits_sample_envelope() -> None:
    from netcoredbg_mcp.session.runtime_smoke_v2.actions import (
        ActionContext,
        dispatch_action,
    )

    session = CorrelationSmokeSession(
        action_identity=None,
        evaluate_identity=None,
        trace_identity=None,
    )
    context = ActionContext(
        service_adapters={},
        clock=lambda: 0.0,
        session=session,
        run_id="runtime-smoke-run-01",
        case_id="case",
        transition_id="transition",
        transition_index=0,
        input_policy={"no_global_input": True},
    )
    result = await dispatch_action(
        {
            "id": "play-action",
            "kind": "ui.click_verified",
            "selector": {"automation_id": "PlayPauseButton"},
            "correlation_source": "play-action",
        },
        context,
    )

    assert result["correlation"]["status"] == "NOT_COMPARABLE"
    assert result["correlation_source"] == "play-action"


@pytest.mark.asyncio
async def test_invalid_app_diagnostics_still_emits_sample_envelope() -> None:
    from netcoredbg_mcp.session.runtime_smoke_v2.actions import ActionContext
    from netcoredbg_mcp.session.runtime_smoke_v2.probe_dispatcher import ProbeContext
    from netcoredbg_mcp.session.runtime_smoke_v2.probes.app_diagnostics import (
        handle_app_diagnostics,
    )

    session = CorrelationSmokeSession(
        action_identity=None,
        evaluate_identity=None,
        trace_identity=None,
    )
    context = ProbeContext(
        action_context=ActionContext(
            service_adapters={},
            clock=lambda: 0.0,
            session=session,
            run_id="runtime-smoke-run-01",
            case_id="case",
            transition_id="transition",
            transition_index=0,
            action_id="play-action",
            action_kind="ui.invoke",
        )
    )
    result = await handle_app_diagnostics(
        {"kind": "app_diagnostics", "schema": "invalid"},
        context,
        phase="after",
    )

    assert result["correlation"]["status"] == "NOT_COMPARABLE"


@pytest.mark.asyncio
async def test_confidence_short_circuit_emits_not_comparable_transition() -> None:
    identity = _media_identity("engine-01", "media-01")

    async def blocked_monitor(**_: Any) -> dict[str, Any]:
        return {"status": "BLOCKED", "reason": "input monitor unavailable"}

    session = CorrelationSmokeSession(
        action_identity=identity,
        evaluate_identity=identity,
        trace_identity=identity,
    )
    runner = RuntimeSmokeRunner(
        session,
        service_adapters={
            "ui.invoke": session.invoke,
            "debug.evaluate": session.evaluate,
            "debug.tracepoint": session.tracepoint,
            "runtime.input_monitor.check": blocked_monitor,
        },
        run_id="runtime-smoke-run-01",
    )
    plan = _plan(identity)
    plan["run_confidence"] = {"no_operator": True}
    result = await runner.run(plan)

    transition = result["cases"][0]["transitions"][0]

    assert transition["correlation"]["comparison"] == "NOT_COMPARABLE"
