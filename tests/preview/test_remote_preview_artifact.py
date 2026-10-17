"""SIMULATED public HTTPS boundaries; never a published-release or MCP proof."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import subprocess
import urllib.request
from copy import deepcopy
from http.client import HTTPMessage
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlsplit
from urllib.response import addinfourl

import pytest

from scripts import stateless_preview_artifact as artifact
from tests import test_stateless_preview_artifact as existing

RELEASE_ID = 901
TAG_OID = "b" * 40
COMMIT = "a" * 40
API = f"https://api.github.com/repos/{existing.REPOSITORY}"


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode() + b"\n"


def _download_api() -> Any:
    # Missing implementation is a named behavioral RED, not a collection/dependency error.
    name = "scripts.stateless_preview_public_release"
    try:
        module = importlib.import_module(name)
    except ModuleNotFoundError as error:
        if error.name != name:
            raise
        pytest.fail("T029 public release transport module is missing")
    api = getattr(module, "download_public_release", None)
    assert callable(api), "T029 download_public_release public API is missing"
    return api


class _BoundedBody(io.BytesIO):
    def read(self, size: int = -1) -> bytes:
        assert size > 0, "public metadata and asset reads must have an explicit byte bound"
        return super().read(size)


class _PublicRelease:
    """Only the HTTPS wire is replaced; parsing, redirects, hashes and writes stay real."""

    def __init__(self, root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.identity, self.archive, self.manifest, self.executable = (
            existing._create_candidate_input(root, COMMIT)
        )
        self.candidate = artifact.assemble_candidate_identity(self.identity)["candidate"]
        manifest = self.candidate["preview_manifest"]
        self.descriptors = [dict(manifest["contents"]["archive"]), dict(manifest["file"])]
        self.raw = {902: self.archive.read_bytes(), 903: self.manifest.read_bytes()}
        self.assets = [
            {
                "id": asset_id,
                "name": descriptor["name"],
                "size": descriptor["size_bytes"],
                "state": "uploaded",
                "digest": f"sha256:{descriptor['sha256']}",
                "url": f"{API}/releases/assets/{asset_id}",
                "browser_download_url": (
                    f"https://github.com/{existing.REPOSITORY}/releases/download/"
                    f"{existing.TAG}/{descriptor['name']}"
                ),
                "download_count": 0,
                "created_at": "2026-10-16T00:00:00Z",
                "updated_at": "2026-10-16T00:00:00Z",
            }
            for asset_id, descriptor in zip((902, 903), self.descriptors, strict=True)
        ]
        self.release = {
            "id": RELEASE_ID,
            "url": f"{API}/releases/{RELEASE_ID}",
            "html_url": f"https://github.com/{existing.REPOSITORY}/releases/tag/{existing.TAG}",
            "tag_name": existing.TAG,
            "target_commitish": COMMIT,
            "draft": False,
            "prerelease": True,
            "published_at": "2026-10-16T00:00:00Z",
            "assets": self.assets,
        }
        self.ref = {
            "ref": f"refs/tags/{existing.TAG}",
            "object": {"type": "tag", "sha": TAG_OID, "url": f"{API}/git/tags/{TAG_OID}"},
        }
        self.tag = {
            "sha": TAG_OID,
            "tag": existing.TAG,
            "object": {"type": "commit", "sha": COMMIT, "url": f"{API}/git/commits/{COMMIT}"},
            "message": "SIMULATED annotated preview tag\n",
        }
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.downloaded: list[int] = []
        self.redirects: dict[int, str] = {}
        self.redirect_headers: dict[str, str] = {}
        self.status = 200
        self.after_download: Any = None
        self.changed = False
        self.extra_body = b""
        self.content_length: str | None = None
        self.destination = root / "fresh-public-assets"

        def https_open(_handler: Any, request: Any) -> Any:
            return self.respond(request)

        def forbidden(*_args: Any, **_kwargs: Any) -> Any:
            pytest.fail("public payload transport must not use HTTP, a process or Actions payload")

        monkeypatch.setattr(urllib.request.HTTPSHandler, "https_open", https_open)
        monkeypatch.setattr(urllib.request.HTTPHandler, "http_open", forbidden)
        monkeypatch.setattr(subprocess, "run", forbidden)
        monkeypatch.setattr(artifact.ArtifactDownloader, "download_payload", forbidden)
        monkeypatch.setenv("GH_TOKEN", "SIMULATED-ambient-gh-secret")
        monkeypatch.setenv("GITHUB_TOKEN", "SIMULATED-ambient-workflow-secret")

    def respond(self, request: Any) -> Any:
        url = request.full_url
        parsed = urlsplit(url)
        headers = {key.lower(): value for key, value in request.header_items()}
        self.requests.append((url, headers))
        assert request.get_method() == "GET", "public transport must never mutate GitHub"
        assert parsed.scheme == "https"
        assert not ({"authorization", "proxy-authorization", "cookie"} & headers.keys()), (
            "public downloads must be anonymous on API and delivery hops"
        )
        for secret in ("SIMULATED-ambient-gh-secret", "SIMULATED-ambient-workflow-secret"):
            assert secret not in url and all(secret not in value for value in headers.values()), (
                "ambient credentials must not leak through URLs or alternate headers"
            )
        assert parsed.hostname in {
            "api.github.com",
            "github.com",
            "release-assets.githubusercontent.com",
        }, "unsafe redirect was contacted before refusal"
        if len(set(self.downloaded)) == 2 and not self.changed and self.after_download:
            self.after_download(self)
            self.changed = True
        path = unquote(parsed.path)
        repository_path = f"/repos/{existing.REPOSITORY}"
        asset_id = next(
            (
                item["id"]
                for item in self.assets
                if url == item["browser_download_url"]
                or path == f"{repository_path}/releases/assets/{item['id']}"
                or path == f"/SIMULATED/{item['id']}"
            ),
            None,
        )
        binary = asset_id is not None and (
            parsed.hostname != "api.github.com"
            or "application/octet-stream" in headers.get("accept", "")
        )
        response_headers = HTTPMessage()
        status = self.status
        if binary:
            if asset_id in self.redirects and parsed.hostname == "api.github.com":
                status = 302
                response_headers["Location"] = self.redirects[asset_id]
                for name, value in self.redirect_headers.items():
                    response_headers[name] = value
                raw = b""
            else:
                self.downloaded.append(asset_id)
                self.assets[(902, 903).index(asset_id)]["download_count"] += 1
                raw = self.raw[asset_id] + self.extra_body
            response_headers["Content-Type"] = "application/octet-stream"
        else:
            if path == f"{repository_path}/releases/{RELEASE_ID}":
                value = self.release
            elif path == f"{repository_path}/releases/{RELEASE_ID}/assets":
                value = self.assets
            elif path in {
                f"{repository_path}/git/ref/tags/{existing.TAG}",
                f"{repository_path}/git/refs/tags/{existing.TAG}",
            }:
                value = self.ref
            elif path == f"{repository_path}/git/tags/{TAG_OID}":
                value = self.tag
            elif path == repository_path:
                value = {"full_name": existing.REPOSITORY, "private": False}
            elif asset_id is not None:
                value = next(item for item in self.assets if item["id"] == asset_id)
            else:
                pytest.fail(f"unexpected public HTTP target: {url}")
            raw = _json(value)
            response_headers["Content-Type"] = "application/json"
        response_headers["Content-Length"] = self.content_length or str(len(raw))
        response = addinfourl(_BoundedBody(raw), response_headers, url, status)
        response.msg = "SIMULATED"
        return response

    def download(self) -> Any:
        return _download_api()(self.candidate, RELEASE_ID, self.destination)


@pytest.fixture
def public_release(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _PublicRelease:
    return _PublicRelease(tmp_path, monkeypatch)


@pytest.mark.parametrize("redirect", [False, True], ids=["asset-200", "asset-https-302"])
@pytest.mark.parametrize("nullable_digest", [False, True], ids=["provider-digest", "null-digest"])
def test_public_release_download_admits_exact_annotated_tag_and_raw_pair(
    public_release: _PublicRelease, redirect: bool, nullable_digest: bool
) -> None:
    wire = public_release
    if redirect:
        wire.redirects = {
            asset_id: f"https://release-assets.githubusercontent.com/SIMULATED/{asset_id}?sig=opaque"
            for asset_id in wire.raw
        }
        wire.redirect_headers["Set-Cookie"] = "SIMULATED-provider-cookie=not-forwarded"
    if nullable_digest:
        for item in wire.assets:
            item["digest"] = None
    archive, manifest, origin = wire.download()

    assert archive == wire.destination / wire.archive.name
    assert manifest == wire.destination / wire.manifest.name
    assert (archive.read_bytes(), manifest.read_bytes()) == (wire.raw[902], wire.raw[903])
    assert origin == {
        "repository": existing.REPOSITORY,
        "release_id": str(RELEASE_ID),
        "tag": existing.TAG,
        "prerelease": True,
        "archive_asset": {"id": "902", **wire.descriptors[0]},
        "manifest_asset": {"id": "903", **wire.descriptors[1]},
    }
    with pytest.raises(TypeError):
        origin["release_id"] = "other"
    with pytest.raises(TypeError):
        origin["archive_asset"]["sha256"] = "0" * 64
    # The act of downloading changes provider counters, not identity.
    assert all(item["download_count"] == 1 for item in wire.assets)
    release_reads = [url for url, _ in wire.requests if url == f"{API}/releases/{RELEASE_ID}"]
    tag_reads = [url for url, _ in wire.requests if "/git/tags/" in url]
    assert len(release_reads) >= 2 and len(tag_reads) >= 2
    assert sorted(wire.downloaded) == [902, 903]


@pytest.mark.parametrize(
    "violation",
    [
        "wrong-tag",
        "wrong-release-id",
        "wrong-target-commit",
        "wrong-peeled-commit",
        "lightweight-tag",
        "wrong-tag-object-name",
        "draft",
        "not-prerelease",
        "missing-asset",
        "duplicate-name",
        "duplicate-id",
        "unexpected-asset",
        "unsafe-name",
        "wrong-size",
        "wrong-provider-digest",
        "not-uploaded",
    ],
)
def test_public_release_download_refuses_wrong_remote_identity_before_raw_download(
    public_release: _PublicRelease, violation: str
) -> None:
    wire = public_release
    if violation == "wrong-tag":
        wire.release["tag_name"] += "-other"
    elif violation == "wrong-release-id":
        wire.release["id"] += 1
    elif violation == "wrong-target-commit":
        wire.release["target_commitish"] = "c" * 40
    elif violation == "wrong-peeled-commit":
        wire.tag["object"]["sha"] = "c" * 40
    elif violation == "lightweight-tag":
        wire.ref["object"].update(type="commit", sha=COMMIT)
    elif violation == "wrong-tag-object-name":
        wire.tag["tag"] += "-other"
    elif violation == "draft":
        wire.release["draft"] = True
    elif violation == "not-prerelease":
        wire.release["prerelease"] = False
    elif violation == "missing-asset":
        wire.assets.pop()
    elif violation == "duplicate-name":
        wire.assets[1]["name"] = wire.assets[0]["name"]
    elif violation == "duplicate-id":
        wire.assets[1]["id"] = wire.assets[0]["id"]
    elif violation == "unexpected-asset":
        wire.assets.append({**wire.assets[0], "id": 904, "name": "extra.zip"})
    elif violation == "unsafe-name":
        wire.assets[0]["name"] = "../escaped.zip"
    elif violation == "wrong-size":
        wire.assets[0]["size"] += 1
    elif violation == "wrong-provider-digest":
        wire.assets[0]["digest"] = "sha256:" + "0" * 64
    else:
        wire.assets[0]["state"] = "new"

    with pytest.raises(ValueError):
        wire.download()
    assert wire.downloaded == [], "unadmitted release identity must not select raw bytes"
    assert not (wire.destination.parent / "escaped.zip").exists()


@pytest.mark.parametrize("asset_id", [902, 903], ids=["archive", "raw-manifest"])
@pytest.mark.parametrize("violation", ["same-size-wrong-hash", "truncated", "overlong"])
def test_public_release_download_checks_raw_size_and_sha256_independently(
    public_release: _PublicRelease, asset_id: int, violation: str
) -> None:
    wire = public_release
    raw = wire.raw[asset_id]
    if violation == "same-size-wrong-hash":
        wire.raw[asset_id] = bytes([raw[0] ^ 1]) + raw[1:]
    elif violation == "truncated":
        wire.raw[asset_id] = raw[:-1]
    else:
        wire.raw[asset_id] = raw + b"\n"
    with pytest.raises(ValueError):
        wire.download()
    assert asset_id in wire.downloaded


@pytest.mark.parametrize(
    "changed", ["release", "asset-id", "asset-size", "tag-oid", "peeled-commit"]
)
def test_public_release_download_rechecks_identity_after_both_downloads(
    public_release: _PublicRelease, changed: str
) -> None:
    def mutate(wire: _PublicRelease) -> None:
        if changed == "release":
            wire.release["draft"] = True
        elif changed == "asset-id":
            wire.assets[0]["id"] = 904
        elif changed == "asset-size":
            wire.assets[0]["size"] += 1
        elif changed == "tag-oid":
            wire.ref["object"]["sha"] = "c" * 40
        else:
            wire.tag["object"]["sha"] = "c" * 40

    public_release.after_download = mutate
    with pytest.raises(ValueError):
        public_release.download()
    assert sorted(public_release.downloaded) == [902, 903]
    assert public_release.changed


@pytest.mark.parametrize(
    "location",
    [
        "http://release-assets.githubusercontent.com/SIMULATED/902",
        "https://attacker.invalid/stolen",
        "https://release-assets.githubusercontent.com.attacker.invalid/stolen",
        "https://user:password@release-assets.githubusercontent.com/SIMULATED/902",
        "file:///SIMULATED/local.zip",
        "https://release-assets.githubusercontent.com:444/SIMULATED/902",
    ],
)
def test_public_release_download_refuses_redirect_before_contacting_unsafe_target(
    public_release: _PublicRelease, location: str
) -> None:
    public_release.redirects[902] = location
    with pytest.raises(ValueError):
        public_release.download()
    assert location not in [url for url, _ in public_release.requests]


def test_public_release_download_bounds_redirect_cycles(public_release: _PublicRelease) -> None:
    public_release.redirects[902] = f"{API}/releases/assets/902"
    with pytest.raises(ValueError):
        public_release.download()
    assert len(public_release.requests) < 20


@pytest.mark.parametrize(
    "release_id", [0, -1, True, "901/../../actions/artifacts", "https://evil.invalid"]
)
def test_public_release_download_refuses_nonpositive_or_nonidentity_release_id(
    public_release: _PublicRelease, release_id: Any
) -> None:
    with pytest.raises(ValueError):
        _download_api()(public_release.candidate, release_id, public_release.destination)
    assert public_release.requests == []


@pytest.mark.parametrize(
    "has_existing_bytes", [False, True], ids=["existing-empty", "existing-payload"]
)
def test_public_release_download_never_reuses_or_overwrites_existing_destination(
    public_release: _PublicRelease, has_existing_bytes: bool
) -> None:
    wire = public_release
    wire.destination.mkdir()
    sentinel = wire.destination / wire.archive.name
    if has_existing_bytes:
        sentinel.write_bytes(b"DO NOT OVERWRITE")
    with pytest.raises(ValueError):
        wire.download()
    assert wire.requests == []
    assert (
        sentinel.read_bytes() == b"DO NOT OVERWRITE"
        if has_existing_bytes
        else not sentinel.exists()
    )


@pytest.mark.parametrize("failure", ["not-public", "invalid-length", "excessive-length"])
def test_public_release_download_cannot_fall_back_to_retained_or_actions_payload(
    public_release: _PublicRelease, failure: str
) -> None:
    wire = public_release
    # Valid local payload remains available: it must never rescue refused public bytes.
    if failure == "not-public":
        wire.status = 404
    elif failure == "invalid-length":
        wire.content_length = "not-a-size"
    else:
        wire.content_length = str(1 << 63)
    with pytest.raises(ValueError):
        wire.download()
    assert wire.archive.read_bytes() == wire.raw[902]
    assert wire.manifest.read_bytes() == wire.raw[903]


def _remote_arguments(root: Path, *, release_id: str = "901") -> list[str]:
    return [
        "remote",
        "--references",
        str(root / "references.json"),
        "--release-id",
        release_id,
        "--download-root",
        str(root),
        "--fixture-root",
        str(root),
        "--receipt-output",
        str(root / "receipt.json"),
        "--receipt-id",
        "simulated-negative-remote-proof",
        "--recorded-at",
        "2026-10-17T00:00:00Z",
        "--python-rollback-command",
        "UNCHANGED-PYTHON-ROLLBACK-NOT-EXECUTED",
    ]


@pytest.mark.parametrize("option", ["--token", "--url", "--mirror", "--archive", "--executable"])
def test_remote_cli_has_no_public_token_url_or_local_payload_override(
    tmp_path: Path, option: str
) -> None:
    validator = existing.preview_validator
    arguments = _remote_arguments(tmp_path)
    admitted = validator.parse_args(arguments)
    assert admitted.command == "remote" and str(admitted.release_id) == "901"
    with pytest.raises(SystemExit):
        validator.parse_args([*arguments, option, "untrusted"])


@pytest.mark.parametrize(
    "existing_receipt", [False, True], ids=["no-receipt", "write-once-receipt"]
)
def test_remote_cli_refuses_invalid_downloaded_identity_without_receipt_or_execution(
    public_release: _PublicRelease,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    existing_receipt: bool,
) -> None:
    wire = public_release
    root = wire.destination.parent
    identity = deepcopy(wire.identity)
    identity["candidate"]["source"]["commit"] = "c" * 40
    raw = _json(identity)
    identity_path = root / "downloaded-identity.json"
    identity_path.write_bytes(raw)
    manifest = identity["candidate"]["preview_manifest"]
    identity_reference = existing._consumer_proof_reference(
        artifact_id="601",
        artifact_name="stateless-preview-candidate-identity",
        path="candidate-identity.json",
        sha256=hashlib.sha256(raw).hexdigest(),
    )
    references = {
        "reference_schema_version": "1.0",
        "candidate_identity": identity_reference,
        **{
            name: {
                "repository": existing.REPOSITORY,
                "run_id": "101",
                "artifact": identity["candidate"]["build"]["artifact"],
                "path": manifest[f"{name}_reference"]["path"],
                "sha256": manifest[f"{name}_reference"]["sha256"],
            }
            for name in ("archive", "manifest")
        },
        "release_gate_catalog": existing._consumer_proof_reference(
            artifact_id="602",
            artifact_name="stateless-preview-release-gate-catalog",
            path="release-gate-catalog.json",
            sha256="0" * 64,
        ),
    }
    (root / "references.json").write_bytes(_json(references))
    receipt = root / "receipt.json"
    if existing_receipt:
        receipt.write_bytes(b"IMMUTABLE EARLIER RECEIPT\n")
    reads = []

    def metadata_only(_downloader: Any, reference: Any) -> Path:
        reads.append(reference.path)
        assert reference.path == identity_reference["path"], (
            "invalid identity must stop metadata admission"
        )
        return identity_path

    monkeypatch.setattr(artifact.ArtifactDownloader, "download_file", metadata_only)
    validator = existing.preview_validator
    assert validator.main(_remote_arguments(root)) == 1
    output = capsys.readouterr()
    assert "PREVIEW_ARTIFACT_CONSUMER_PROOF_REFUSED" in output.err
    assert "PREVIEW_ARTIFACT_CONSUMER_PROOF_SEALED" not in output.out
    assert wire.requests == []
    if existing_receipt:
        assert reads == []
        assert receipt.read_bytes() == b"IMMUTABLE EARLIER RECEIPT\n"
    else:
        assert reads == ["candidate-identity.json"]
        assert not receipt.exists()


@pytest.mark.parametrize("existing_receipt", [False, True])
def test_remote_cli_refuses_unpublished_remote_identity_without_actions_payload_fallback(
    public_release: _PublicRelease,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    existing_receipt: bool,
) -> None:
    wire = public_release
    root = wire.destination.parent
    wire.release["draft"] = True
    identity_raw = _json(wire.identity)
    # SIMULATED metadata only; no Stage, publication or successful consumer receipt.
    catalog_raw = _json(
        {
            "catalog_schema_version": "1.0",
            "catalog": {
                "producer": {
                    "helper_path": "scripts/stateless_preview_artifact.py",
                    "operation": "resolve_release_gate_catalog",
                },
                "source_ref": existing.SOURCE_REF,
                "source_commit": COMMIT,
                "policy_authority_snapshots": [
                    {
                        "path": path,
                        "sha256": hashlib.sha256(
                            (existing.PROJECT_ROOT / path).read_bytes()
                        ).hexdigest(),
                        "source_commit": COMMIT,
                    }
                    for path in existing._authority_paths()
                ],
                "gate_descriptors": existing._fixed_gate_descriptors(),
                "resolved_at": "2026-10-16T00:00:00Z",
            },
        }
    )
    metadata = {
        "candidate-identity.json": identity_raw,
        "release-gate-catalog.json": catalog_raw,
    }

    def reference(name: str) -> dict[str, Any]:
        identity_record = name == "candidate-identity.json"
        return existing._consumer_proof_reference(
            artifact_id="601" if identity_record else "602",
            artifact_name=(
                "stateless-preview-candidate-identity"
                if identity_record
                else "stateless-preview-release-gate-catalog"
            ),
            path=name,
            sha256=hashlib.sha256(metadata[name]).hexdigest(),
        )

    manifest = wire.identity["candidate"]["preview_manifest"]
    (root / "references.json").write_bytes(
        _json(
            {
                "reference_schema_version": "1.0",
                "candidate_identity": reference("candidate-identity.json"),
                "release_gate_catalog": reference("release-gate-catalog.json"),
                **{
                    name: {
                        "repository": existing.REPOSITORY,
                        "run_id": "101",
                        "artifact": wire.identity["candidate"]["build"]["artifact"],
                        "path": manifest[f"{name}_reference"]["path"],
                        "sha256": manifest[f"{name}_reference"]["sha256"],
                    }
                    for name in ("archive", "manifest")
                },
            }
        )
    )

    def metadata_only(downloader: Any, item: Any) -> Path:
        assert item.path in metadata, "remote CLI must not download Actions payload members"
        path = root / ("controlled-" + item.path)
        path.write_bytes(metadata[item.path])
        return path

    def forbidden_extraction(*_args: Any, **_kwargs: Any) -> Any:
        pytest.fail("unpublished remote identity must refuse before extraction or execution")

    monkeypatch.setattr(artifact.ArtifactDownloader, "download_file", metadata_only)
    monkeypatch.setattr(
        existing.preview_validator, "verify_and_extract_retained_artifact", forbidden_extraction
    )
    receipt = root / "receipt.json"
    if existing_receipt:
        receipt.write_bytes(b"IMMUTABLE EARLIER RECEIPT\n")
    assert existing.preview_validator.main(_remote_arguments(root)) == 1
    output = capsys.readouterr()
    assert "PREVIEW_ARTIFACT_CONSUMER_PROOF_REFUSED" in output.err
    assert "PREVIEW_ARTIFACT_CONSUMER_PROOF_SEALED" not in output.out
    assert wire.downloaded == []
    if existing_receipt:
        assert wire.requests == []
        assert receipt.read_bytes() == b"IMMUTABLE EARLIER RECEIPT\n"
    else:
        assert any(url == f"{API}/releases/{RELEASE_ID}" for url, _ in wire.requests)
        assert not receipt.exists()
