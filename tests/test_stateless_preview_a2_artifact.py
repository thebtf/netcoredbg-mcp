"""Behavioral preview generation/sealing regressions; simulated receipts are not release proof."""

from __future__ import annotations

import io
import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import ValidationError

from scripts import stateless_preview_artifact as artifact
from tests import test_stateless_preview_artifact as existing

validator = existing.preview_validator
A1 = "a1-preview-inherited-matrix-v1"
A2 = "a2-preview-readonly-family-v1"
A3 = "a3-preview-optional-client-info-v1"
TOOLS = ["find_code_symbol", "find_code_references", "get_source_context"]


def _upgrade_candidate(
    identity: dict[str, Any], archive: Path, manifest: Path, generation: str = A2
) -> None:
    identity["schema_version"] = "2.0"
    candidate = identity["candidate"]
    binding = candidate["preview_manifest"]
    binding["contents"].update(
        schema_version="2.0", consumer_contract=artifact._consumer_contract(generation)
    )
    raw = existing._canonical_json_bytes(binding["contents"])
    manifest.write_bytes(raw)
    binding["file"].update(size_bytes=len(raw), sha256=existing._sha256_bytes(raw))
    binding["manifest_reference"]["sha256"] = binding["file"]["sha256"]
    candidate["build"]["artifact"]["sha256"] = existing._sha256_bytes(archive.read_bytes() + raw)


def _set_proof_generation(receipt: dict[str, Any], contract_id: str) -> None:
    catalog = artifact.consumer_proof_scenario_catalog(contract_id)
    receipt["fixture_identity"].update(
        scenario_catalog_id=catalog["scenario_catalog_id"],
        scenario_catalog_sha256=catalog["scenario_catalog_sha256"],
    )
    scenarios = catalog["scenarios"]
    ids = [scenario["scenario_id"] for scenario in scenarios]
    receipt["scenario_matrix"] = {
        "required_scenario_ids": ids.copy(),
        "executed_scenario_ids": ids.copy(),
        "required_count": len(ids),
        "executed_count": len(ids),
        "missing_scenario_ids": [],
        "unexpected_scenario_ids": [],
        "results": [
            {
                **scenario,
                "observed_outcome": artifact._EXPECTED_OBSERVED_OUTCOMES[
                    scenario["documented_outcome"]
                ],
                "status": "PASS",
                "no_partial_output": True,
                "no_unintended_side_effect": True,
            }
            for scenario in scenarios
        ],
        "outcome": "PASS",
    }
    receipt["runtime_results"]["catalog"] = list(
        ["find_code_symbol"] if contract_id == A1 else TOOLS
    )


def _proof(tmp_path: Path, generation: str, remote: bool) -> tuple[dict[str, Any], dict[str, Any]]:
    receipt, identity, _, reference, catalog = existing._valid_retained_consumer_proof(tmp_path)
    if generation != A1:
        candidate = identity["candidate"]
        archive = (
            tmp_path / "retained" / candidate["preview_manifest"]["contents"]["archive"]["name"]
        )
        manifest = tmp_path / "retained" / candidate["preview_manifest"]["file"]["name"]
        _upgrade_candidate(identity, archive, manifest, generation)
        candidate = identity["candidate"]
        receipt["candidate"] = deepcopy(candidate)
        receipt["input_identity_results"]["manifest"] = deepcopy(
            candidate["preview_manifest"]["file"]
        )
        receipt["download_origin"]["artifact"] = deepcopy(candidate["build"]["artifact"])
    _set_proof_generation(receipt, generation)
    raw_identity = existing._canonical_json_bytes(identity)
    reference["sha256"] = existing._sha256_bytes(raw_identity)
    receipt["candidate_identity_record"] = deepcopy(reference)
    if remote:
        candidate = identity["candidate"]
        receipt["proof_stage"] = "remote_release"
        receipt["download_origin"] = {
            "repository": existing.REPOSITORY,
            "release_id": "901",
            "tag": candidate["destination"]["tag"],
            "prerelease": True,
            "archive_asset": {"id": "902", **candidate["preview_manifest"]["contents"]["archive"]},
            "manifest_asset": {"id": "903", **candidate["preview_manifest"]["file"]},
        }
    return receipt, {
        "candidate_identity": identity,
        "candidate_identity_bytes": raw_identity,
        "candidate_identity_reference": reference,
        "release_gate_catalog": catalog,
    }


def test_a3_catalog_adds_only_optional_client_info_before_rollback() -> None:
    historical = artifact.consumer_proof_scenario_catalog(A2)
    current = artifact.consumer_proof_scenario_catalog(A3)

    assert current["scenario_catalog_id"] == A3
    assert len(current["scenarios"]) == 16
    assert current["scenarios"] == [
        *historical["scenarios"][:-1],
        {
            "scenario_id": "optional-client-info-request-local",
            "surface": "protocol_catalog",
            "documented_outcome": "EXPECT_SUCCESS",
        },
        historical["scenarios"][-1],
    ]
    assert (
        current["scenario_catalog_sha256"]
        == (existing._expected_a3_consumer_contract()["scenario_catalog_sha256"])
    )
    assert artifact.consumer_proof_scenario_catalog() == artifact.consumer_proof_scenario_catalog(
        A1
    )


@pytest.mark.parametrize("remote", [False, True], ids=["retained", "remote"])
def test_a3_sealers_accept_complete_candidate_bound_proof(tmp_path: Path, remote: bool) -> None:
    receipt, binding = _proof(tmp_path, A3, remote)

    assert artifact._validate_stage_consumer_proof(receipt, remote=remote, **binding) == receipt
    assert receipt["scenario_matrix"]["required_count"] == 16
    assert receipt["runtime_results"]["catalog"] == TOOLS
    existing._validate_review_schema(
        binding["candidate_identity"], "candidate-identity.schema.json"
    )
    existing._validate_review_schema(receipt, "artifact-consumer-proof.schema.json")


@pytest.mark.parametrize("remote", [False, True], ids=["retained", "remote"])
@pytest.mark.parametrize("violation", ["unknown-id", "a2-hash", "missing-optional-case"])
def test_a3_sealers_reject_unknown_mixed_or_incomplete_proof(
    tmp_path: Path, remote: bool, violation: str
) -> None:
    receipt, binding = _proof(tmp_path, A3, remote)
    if violation == "unknown-id":
        receipt["fixture_identity"]["scenario_catalog_id"] = "unknown-generation"
    elif violation == "a2-hash":
        receipt["fixture_identity"]["scenario_catalog_sha256"] = (
            artifact.consumer_proof_scenario_catalog(A2)["scenario_catalog_sha256"]
        )
    else:
        matrix = receipt["scenario_matrix"]
        index = matrix["required_scenario_ids"].index("optional-client-info-request-local")
        matrix["required_scenario_ids"].pop(index)
        matrix["executed_scenario_ids"].pop(index)
        matrix["results"].pop(index)
        matrix["required_count"] -= 1
        matrix["executed_count"] -= 1

    with pytest.raises(ValueError, match="candidate generation|scenario matrix"):
        artifact._validate_stage_consumer_proof(receipt, remote=remote, **binding)


@pytest.mark.parametrize("violation", ["unknown-id", "a2-hash", "missing-hash", "extra-field"])
def test_a3_candidate_requires_exact_closed_consumer_contract(
    tmp_path: Path, violation: str
) -> None:
    _, binding = _proof(tmp_path, A3, False)
    identity = binding["candidate_identity"]
    contract = identity["candidate"]["preview_manifest"]["contents"]["consumer_contract"]
    if violation == "unknown-id":
        contract["scenario_catalog_id"] = "unknown-generation"
    elif violation == "a2-hash":
        contract["scenario_catalog_sha256"] = artifact.consumer_proof_scenario_catalog(A2)[
            "scenario_catalog_sha256"
        ]
    elif violation == "missing-hash":
        del contract["scenario_catalog_sha256"]
    else:
        contract["receipt_selects_validator"] = True

    with pytest.raises(ValueError):
        artifact.assemble_candidate_identity(identity)


@pytest.mark.parametrize("remote", [False, True], ids=["retained", "remote"])
@pytest.mark.parametrize("generation", [A1, A2])
def test_original_a1_and_successor_a2_keep_distinct_passing_semantics(
    tmp_path: Path, remote: bool, generation: str
) -> None:
    receipt, binding = _proof(tmp_path, generation, remote)
    sealed = artifact._validate_stage_consumer_proof(receipt, remote=remote, **binding)
    assert sealed == receipt
    existing._validate_review_schema(
        binding["candidate_identity"], "candidate-identity.schema.json"
    )
    existing._validate_review_schema(receipt, "artifact-consumer-proof.schema.json")
    assert receipt["runtime_results"]["catalog"] == (
        TOOLS if generation == A2 else ["find_code_symbol"]
    )


@pytest.mark.parametrize("remote", [False, True], ids=["retained", "remote"])
@pytest.mark.parametrize(
    "candidate_generation,receipt_generation", [(A2, A1), (A1, A2), (A3, A2), (A2, A3)]
)
def test_receipt_cannot_select_a_weaker_or_cross_generation_validator(
    tmp_path: Path, remote: bool, candidate_generation: str, receipt_generation: str
) -> None:
    receipt, binding = _proof(tmp_path, candidate_generation, remote)
    _set_proof_generation(receipt, receipt_generation)
    with pytest.raises(ValueError, match="candidate generation"):
        artifact._validate_stage_consumer_proof(receipt, remote=remote, **binding)
    with pytest.raises(ValidationError):
        existing._validate_review_schema(receipt, "artifact-consumer-proof.schema.json")


@pytest.mark.parametrize("remote", [False, True], ids=["retained", "remote"])
@pytest.mark.parametrize(
    "violation",
    [
        "unknown-id",
        "a1-hash",
        "missing-case",
        "old-tools",
        "extra-tool",
        "reordered-tools",
        "raw-identity",
    ],
)
def test_successor_sealers_reject_catalog_matrix_tool_and_identity_substitution(
    tmp_path: Path, remote: bool, violation: str
) -> None:
    receipt, binding = _proof(tmp_path, A2, remote)
    if violation == "unknown-id":
        receipt["fixture_identity"]["scenario_catalog_id"] = "unknown-generation"
    elif violation == "a1-hash":
        receipt["fixture_identity"]["scenario_catalog_sha256"] = (
            artifact.consumer_proof_scenario_catalog()["scenario_catalog_sha256"]
        )
    elif violation == "missing-case":
        matrix = receipt["scenario_matrix"]
        matrix["required_scenario_ids"].pop()
        matrix["executed_scenario_ids"].pop()
        matrix["results"].pop()
        matrix["required_count"] -= 1
        matrix["executed_count"] -= 1
    elif violation == "old-tools":
        receipt["runtime_results"]["catalog"] = ["find_code_symbol"]
    elif violation == "extra-tool":
        receipt["runtime_results"]["catalog"].append("start_debug")
    elif violation == "reordered-tools":
        receipt["runtime_results"]["catalog"].reverse()
    elif violation == "raw-identity":
        binding["candidate_identity_bytes"] += b" "
    with pytest.raises(ValueError):
        artifact._validate_stage_consumer_proof(receipt, remote=remote, **binding)


@pytest.mark.parametrize(
    "violation",
    [
        "unknown-schema",
        "record-downgrade",
        "unknown-id",
        "a1-id",
        "a1-hash",
        "old-tools",
        "extra-field",
        "missing-contract",
    ],
)
def test_immutable_successor_candidate_rejects_generation_downgrade(
    tmp_path: Path, violation: str
) -> None:
    _, binding = _proof(tmp_path, A2, False)
    identity = binding["candidate_identity"]
    manifest = identity["candidate"]["preview_manifest"]["contents"]
    contract = manifest["consumer_contract"]
    if violation == "unknown-schema":
        manifest["schema_version"] = "3.0"
    elif violation == "record-downgrade":
        identity["schema_version"] = "1.0"
    elif violation in ("unknown-id", "a1-id"):
        contract["scenario_catalog_id"] = A1 if violation == "a1-id" else "unknown-generation"
    elif violation == "a1-hash":
        contract["scenario_catalog_sha256"] = artifact.consumer_proof_scenario_catalog()[
            "scenario_catalog_sha256"
        ]
    elif violation == "old-tools":
        contract["tools"] = ["find_code_symbol"]
    elif violation == "extra-field":
        contract["receipt_selects_validator"] = True
    elif violation == "missing-contract":
        del manifest["consumer_contract"]
    with pytest.raises(ValueError):
        artifact.assemble_candidate_identity(identity)


def test_historical_a1_manifest_cannot_adopt_successor_contract(tmp_path: Path) -> None:
    _, binding = _proof(tmp_path, A1, False)
    binding["candidate_identity"]["candidate"]["preview_manifest"]["contents"][
        "consumer_contract"
    ] = artifact._consumer_contract(A2)
    with pytest.raises(ValueError):
        artifact.assemble_candidate_identity(binding["candidate_identity"])


def test_preview_version_is_not_the_generation_selector(tmp_path: Path) -> None:
    receipt, binding = _proof(tmp_path, A2, False)
    assert (
        binding["candidate_identity"]["candidate"]["preview_manifest"]["contents"]["version"]
        == existing.VERSION
    )
    assert receipt["fixture_identity"]["scenario_catalog_id"] == A2
    assert artifact.consumer_proof_scenario_catalog()["scenario_catalog_id"] == A1
    with pytest.raises(ValueError, match="unsupported"):
        artifact.consumer_proof_scenario_catalog("unknown-generation")


@pytest.mark.parametrize("generation", [A1, A2], ids=["a1", "a2"])
def test_current_source_producer_cannot_seal_historical_payload(
    tmp_path: Path, generation: str
) -> None:
    root, commit, _ = existing._create_authority_repository(tmp_path)
    existing._create_publish_output(root)
    environment = existing._build_environment(commit)
    admission = artifact.admit_build(root, environment)
    payload = artifact.prepare_preview_payload(root, environment)
    manifest_path = root / "artifacts/stateless-preview/payload" / payload["manifest"]["name"]
    manifest = json.loads(manifest_path.read_bytes())
    assert manifest["schema_version"] == "2.0"
    if generation == A1:
        manifest["schema_version"] = "1.0"
        del manifest["consumer_contract"]
    else:
        manifest["consumer_contract"] = artifact._consumer_contract(generation)
    manifest_path.write_bytes(existing._canonical_json_bytes(manifest))
    environment.update(A1_PAYLOAD_ARTIFACT_ID="501", GITHUB_TOKEN="SIMULATED-token")

    def reader(*_: Any) -> dict[str, Any]:
        return {
            "id": 501,
            "name": admission["artifacts"]["payload"],
            "expired": False,
            "expires_at": "2030-01-01T00:00:00Z",
            "workflow_run": {"id": 101},
        }

    with pytest.raises(ValueError, match="current source producer"):
        artifact.seal_build_records(root, environment, artifact_reader=reader)


@pytest.mark.parametrize("failure", ["premature-eof", "wrong-marker"])
def test_optional_client_info_runner_rejects_early_failure_and_wrong_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    processes: list[Any] = []

    class RejectedProcess:
        def __init__(self, *_: Any) -> None:
            self.aborted = False
            processes.append(self)

        def request(self, *_: Any) -> dict[str, Any]:
            if failure == "premature-eof":
                raise ValueError("SIMULATED process ended before a JSON-RPC response")
            wrong_marker = {
                "kind": "find_code_symbol_success",
                "results": [
                    {
                        "file": "Markers.cs",
                        "line": 4,
                        "name": "PreviewMarker",
                        "kind": "class",
                        "context": "public sealed class PreviewMarker { }",
                    }
                ],
            }
            return {
                "result": {
                    "resultType": "complete",
                    "isError": False,
                    "structuredContent": wrong_marker,
                    "content": [{"type": "text", "text": json.dumps(wrong_marker)}],
                }
            }

        def close_eof(self) -> None:
            raise AssertionError("An invalid first call must not reach clean EOF acceptance")

        def abort(self) -> None:
            self.aborted = True

    monkeypatch.setattr(validator, "JsonRpcProcess", RejectedProcess)
    with pytest.raises(ValueError, match="ended before a JSON-RPC response|result values"):
        validator._run_optional_client_info(tmp_path / "preview.exe", tmp_path)
    assert processes and all(process.aborted for process in processes)


@pytest.mark.parametrize("tool", ["find_code_references", "get_source_context"])
@pytest.mark.parametrize("violation", [None, "wrong-route", "partial-data", "text-mismatch"])
def test_consumer_requires_route_correct_closed_text_and_structured_failures(
    tool: str, violation: str | None
) -> None:
    payload: dict[str, Any] = {
        "kind": "preview_search_budget_exceeded",
        "error": "PREVIEW_SEARCH_BUDGET_EXCEEDED",
        "tool": tool,
    }
    result = {
        "resultType": "complete",
        "isError": True,
        "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }
    if violation == "wrong-route":
        payload["tool"] = "find_code_symbol"
    elif violation == "partial-data":
        payload["results"] = [{"file": "Private.cs", "line": 1}]
    elif violation == "text-mismatch":
        result["content"][0]["text"] = "{}"
    if violation:
        with pytest.raises(ValueError):
            validator._expect_failure(result, tool, "PREVIEW_SEARCH_BUDGET_EXCEEDED")
    else:
        validator._expect_failure(result, tool, "PREVIEW_SEARCH_BUDGET_EXCEEDED")


@pytest.mark.parametrize("oversized", [False, True])
def test_consumer_bounds_the_actual_jsonrpc_frame_including_request_id(oversized: bool) -> None:
    request_id = "i" * (256 * 1024 if oversized else 8192)
    raw = json.dumps({"jsonrpc": "2.0", "id": request_id, "result": {}}).encode() + b"\n"
    process = validator.JsonRpcProcess.__new__(validator.JsonRpcProcess)

    class Wire:
        stdin = io.BytesIO()
        stdout = io.BytesIO(raw)

    process._process = Wire()
    if oversized:
        with pytest.raises(ValueError, match="oversized complete response frame"):
            process.request(request_id, "tools/call", {})
    else:
        assert process.request(request_id, "tools/call", {})["id"] == request_id


def test_public_acquisition_verifies_the_a2_schema_without_a1_reconstruction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.preview.test_remote_preview_artifact import _PublicRelease

    wire = _PublicRelease(tmp_path, monkeypatch)
    _upgrade_candidate(wire.identity, wire.archive, wire.manifest)
    wire.candidate = artifact.assemble_candidate_identity(wire.identity)["candidate"]
    wire.raw[903] = wire.manifest.read_bytes()
    descriptor = wire.candidate["preview_manifest"]["file"]
    wire.assets[1].update(size=descriptor["size_bytes"], digest=f"sha256:{descriptor['sha256']}")
    archive_path, manifest_path, origin = wire.download()
    verified = artifact.verify_retained_artifact_inputs(wire.identity, archive_path, manifest_path)
    assert origin["manifest_asset"]["sha256"] == descriptor["sha256"]
    assert verified["executable"] == wire.candidate["preview_manifest"]["contents"]["executable"]
    assert sorted(wire.downloaded) == [902, 903]


def test_public_acquisition_rejects_unknown_generation_before_network(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from tests.preview.test_remote_preview_artifact import _PublicRelease

    wire = _PublicRelease(tmp_path, monkeypatch)
    _upgrade_candidate(wire.identity, wire.archive, wire.manifest)
    wire.identity["candidate"]["preview_manifest"]["contents"]["consumer_contract"][
        "scenario_catalog_id"
    ] = "unknown-generation"
    wire.candidate = wire.identity["candidate"]
    with pytest.raises(ValueError, match="consumer contract"):
        wire.download()
    assert wire.requests == []


@pytest.mark.parametrize("route", ["find_code_references", "get_source_context"])
@pytest.mark.parametrize(
    "violation",
    [
        None,
        "wrong-line",
        "absolute-file",
        "partial-shape",
        "text-mismatch",
        "output-line-type",
        "input-contract",
        "missing-required",
        "schema-extension",
    ],
)
def test_actual_journey_oracle_checks_new_route_values_not_just_tool_presence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, route: str, violation: str | None
) -> None:
    symbol = {
        "kind": "find_code_symbol_success",
        "results": [
            {
                "file": "Markers.cs",
                "line": 3,
                "name": "PreviewMarker",
                "kind": "class",
                "context": "public sealed class PreviewMarker { }",
            }
        ],
    }
    references = {
        "kind": "find_code_references_success",
        "results": [
            {"file": "Markers.cs", "line": 3, "context": "public sealed class PreviewMarker { }"},
            {
                "file": "PreviewReferences.cs",
                "line": 5,
                "context": "public PreviewMarker Value { get; } = new PreviewMarker();",
            },
        ],
    }
    context = {
        "kind": "get_source_context_success",
        "file": "PreviewReferences.cs",
        "start_line": 4,
        "end_line": 6,
        "lines": [
            {"line": 4, "text": "{"},
            {"line": 5, "text": "    public PreviewMarker Value { get; } = new PreviewMarker();"},
            {"line": 6, "text": "}"},
        ],
    }

    def success(payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "resultType": "complete",
            "isError": False,
            "structuredContent": deepcopy(payload),
            "content": [{"type": "text", "text": json.dumps(payload)}],
        }

    frozen_schemas = json.loads(
        existing.PREVIEW_ARTIFACT_VALIDATOR_PATH.with_name("a2-tool-schemas.json").read_bytes()
    )
    tools = [
        {
            "name": name,
            "annotations": {"readOnlyHint": True, "idempotentHint": True, "openWorldHint": False},
            **deepcopy(frozen_schemas[name]),
        }
        for name in TOOLS
    ]
    advertised = tools[1 if route == "find_code_references" else 2]
    if violation == "output-line-type":
        rows = "results" if route == "find_code_references" else "lines"
        advertised["outputSchema"]["properties"][rows]["items"]["properties"]["line"]["type"] = (
            "string"
        )
    elif violation == "input-contract":
        if route == "find_code_references":
            advertised["inputSchema"]["properties"]["max_results"]["maximum"] = 128
        else:
            advertised["inputSchema"]["properties"]["line"]["minimum"] = 0
    elif violation == "missing-required":
        advertised["outputSchema"]["required"].remove("kind")
    elif violation == "schema-extension":
        advertised["inputSchema"]["allOf"] = [{"type": "string"}]
    responses = {
        1: {"capabilities": {"tools": {}}},
        2: {"tools": tools},
        3: success(symbol),
        4: success(references),
        5: success(context),
    }
    selected = responses[4 if route == "find_code_references" else 5]
    payload = selected["structuredContent"]
    if violation == "wrong-line":
        if route == "find_code_references":
            payload["results"][1]["line"] = 4
        else:
            payload["lines"][1]["line"] = 4
    elif violation == "absolute-file":
        if route == "find_code_references":
            payload["results"][1]["file"] = "C:/private/PreviewReferences.cs"
        else:
            payload["file"] = "C:/private/PreviewReferences.cs"
    elif violation == "partial-shape":
        payload["partial"] = True
    elif violation == "text-mismatch":
        selected["content"][0]["text"] = "{}"
    if violation and violation != "text-mismatch":
        selected["content"][0]["text"] = json.dumps(payload)

    class Process:
        closed = False

        def __init__(self, *_: Any) -> None:
            pass

        def request(self, request_id: int, *_: Any) -> dict[str, Any]:
            return {"result": responses[request_id]}

        def close_eof(self) -> None:
            self.closed = True

        def abort(self) -> None:
            pass

    monkeypatch.setattr(validator, "JsonRpcProcess", Process)
    if violation:
        expected_error = (
            "schema"
            if violation
            in ("output-line-type", "input-contract", "missing-required", "schema-extension")
            else "result values"
        )
        with pytest.raises(ValueError, match=expected_error):
            validator._run_valid_journey(tmp_path / "preview.exe", tmp_path, TOOLS)
    else:
        validator._run_valid_journey(tmp_path / "preview.exe", tmp_path, TOOLS)


@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize(
    "advertised_schema", [False, True], ids=["frozen-matrix", "advertised-journey"]
)
def test_success_values_must_satisfy_json_schema_not_python_boolean_integer_equality(
    tool: str, advertised_schema: bool
) -> None:
    expected: dict[str, Any]
    if tool == "get_source_context":
        expected = {
            "kind": "get_source_context_success",
            "file": "Markers.cs",
            "start_line": 1,
            "end_line": 1,
            "lines": [{"line": 1, "text": "// marker"}],
        }
        field = "lines"
    else:
        row = {"file": "Markers.cs", "line": 1, "context": "// marker"}
        if tool == "find_code_symbol":
            row.update(name="PreviewMarker", kind="class")
        expected = {"kind": f"{tool}_success", "results": [row]}
        field = "results"
    payload = deepcopy(expected)
    payload[field][0]["line"] = True
    assert payload == expected  # Python equality alone does not establish JSON integer types.
    result = {
        "resultType": "complete",
        "isError": False,
        "structuredContent": payload,
        "content": [{"type": "text", "text": json.dumps(payload)}],
    }
    options = {}
    if advertised_schema:
        schemas = json.loads(
            existing.PREVIEW_ARTIFACT_VALIDATOR_PATH.with_name("a2-tool-schemas.json").read_bytes()
        )
        options["output_schema"] = schemas[tool]["outputSchema"]
    with pytest.raises(ValueError, match="schema"):
        validator._expect_success(result, expected, **options)


def test_historical_a1_journey_keeps_its_original_schema_semantics_without_jsonschema(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import builtins

    original_import = builtins.__import__

    def without_jsonschema(name: str, *args: Any, **kwargs: Any) -> Any:
        if name == "jsonschema":
            raise ImportError("SIMULATED missing optional A2 proof dependency")
        return original_import(name, *args, **kwargs)

    responses = {
        1: {"capabilities": {"tools": {}}},
        2: {"tools": [{"name": "find_code_symbol"}]},
        3: {
            "resultType": "complete",
            "isError": False,
            "structuredContent": {
                "kind": "find_code_symbol_success",
                "results": [{"name": "PreviewMarker", "kind": "class"}],
            },
        },
    }

    class Process:
        def __init__(self, *_: Any) -> None:
            pass

        def request(self, request_id: int, *_: Any) -> dict[str, Any]:
            return {"result": responses[request_id]}

        def close_eof(self) -> None:
            pass

        def abort(self) -> None:
            pass

    monkeypatch.setattr(builtins, "__import__", without_jsonschema)
    monkeypatch.setattr(validator, "JsonRpcProcess", Process)
    validator._run_valid_journey(tmp_path / "historical-preview.exe", tmp_path)
