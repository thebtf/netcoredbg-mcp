"""Download only the exact public GitHub prerelease assets of a sealed preview candidate."""

from __future__ import annotations

import hashlib
import stat
from collections.abc import Iterator, Mapping
from http.client import HTTPException
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from scripts import stateless_preview_artifact as _artifact

_JSON_LIMIT = 1024 * 1024
_ARCHIVE_LIMIT = 256 * 1024 * 1024
_PUBLIC_HOSTS = frozenset(("api.github.com", "github.com", "release-assets.githubusercontent.com"))


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str
    ) -> None:
        return None


def _safe_url(url: Any) -> str:
    if not isinstance(url, str) or any(ord(char) <= 32 or ord(char) >= 127 for char in url):
        raise ValueError("public download URL is invalid")
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or parsed.hostname not in _PUBLIC_HOSTS
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 443)
            or parsed.fragment
            or "\\" in url
        ):
            raise ValueError("public download URL is not an allowed HTTPS target")
    except (ValueError, UnicodeError):
        raise ValueError("public download URL is not an allowed HTTPS target") from None
    return url


def _get(opener: Any, url: str, *, binary: bool = False) -> Any:
    visited: set[str] = set()
    for _ in range(5):
        url = _safe_url(url)
        if url in visited:
            raise ValueError("public download redirect cycle")
        visited.add(url)
        request = Request(
            url,
            method="GET",
            headers={
                "Accept": "application/octet-stream" if binary else "application/vnd.github+json",
                "Accept-Encoding": "identity",
                "User-Agent": "netcoredbg-mcp-a1-public-consumer",
                "X-GitHub-Api-Version": "2026-03-10",
                "Cache-Control": "no-cache",
            },
        )
        try:
            response = opener.open(request, timeout=30)
        except HTTPError as error:
            response = error
        if response.getcode() == 200:
            if response.geturl() != url:
                response.close()
                raise ValueError("public download followed an unvalidated redirect")
            return response
        try:
            if response.getcode() != 302 or not binary:
                raise ValueError("public release or asset is unavailable anonymously")
            locations = response.headers.get_all("Location", [])
            if len(locations) != 1:
                raise ValueError("public download redirect is ambiguous")
            location = locations[0]
            if not location or any(ord(char) <= 32 or ord(char) >= 127 for char in location):
                raise ValueError("public download redirect is invalid")
            url = _safe_url(urljoin(url, location))
        finally:
            response.close()
    raise ValueError("public download exceeds the redirect bound")


def _chunks(response: Any, limit: int, *, exact_size: int | None = None) -> Iterator[bytes]:
    lengths = response.headers.get_all("Content-Length", [])
    declared = None
    if lengths:
        if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdecimal():
            raise ValueError("public response has an invalid content length")
        declared = int(lengths[0])
        if declared > limit or (exact_size is not None and declared != exact_size):
            raise ValueError("public response length does not match its byte bound")
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise ValueError("public response must contain unencoded raw bytes")
    total = 0
    while True:
        chunk = response.read(min(64 * 1024, limit + 1 - total))
        if not chunk:
            break
        total += len(chunk)
        if total > limit:
            raise ValueError("public response exceeds its byte bound")
        yield chunk
    if (declared is not None and total != declared) or (
        exact_size is not None and total != exact_size
    ):
        raise ValueError("public response is truncated")


def _metadata(opener: Any, url: str) -> Mapping[str, Any]:
    with _get(opener, url) as response:
        raw = b"".join(_chunks(response, _JSON_LIMIT))
    return _artifact._load_json_object(raw, "public GitHub metadata")


def _snapshot(
    opener: Any, candidate: Mapping[str, Any], release_id: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    repository = candidate["destination"]["repository"]
    tag_name = candidate["destination"]["tag"]
    commit = candidate["source"]["commit"]
    api = f"https://api.github.com/repos/{repository}"
    repository_metadata = _metadata(opener, api)
    if (
        repository_metadata.get("full_name") != repository
        or repository_metadata.get("private") is not False
    ):
        raise ValueError("candidate repository is not public")
    release_url = f"{api}/releases/{release_id}"
    release = _metadata(opener, release_url)
    if (
        type(release.get("id")) is not int
        or release["id"] != release_id
        or release.get("url") != release_url
        or release.get("html_url") != f"https://github.com/{repository}/releases/tag/{tag_name}"
        or release.get("tag_name") != tag_name
        or release.get("target_commitish") != commit
        or release.get("draft") is not False
        or release.get("prerelease") is not True
    ):
        raise ValueError("public release does not bind the exact prerelease candidate")
    _artifact._expect_datetime(release.get("published_at"), "public release publication time")
    ref = _metadata(opener, f"{api}/git/ref/tags/{quote(tag_name, safe='')}")
    ref_object = ref.get("object")
    if ref.get("ref") != f"refs/tags/{tag_name}" or not isinstance(ref_object, Mapping):
        raise ValueError("public tag reference is invalid")
    oid = _artifact._expect_commit(ref_object.get("sha"), "public annotated tag object")
    if ref_object.get("type") != "tag" or ref_object.get("url") != f"{api}/git/tags/{oid}":
        raise ValueError("public tag must be an annotated tag")
    tag = _metadata(opener, f"{api}/git/tags/{oid}")
    target = tag.get("object")
    if (
        tag.get("sha") != oid
        or tag.get("tag") != tag_name
        or not isinstance(target, Mapping)
        or target.get("type") != "commit"
        or target.get("sha") != commit
        or target.get("url") != f"{api}/git/commits/{commit}"
    ):
        raise ValueError("public annotated tag does not dereference to the candidate commit")
    assets = release.get("assets")
    if (
        not isinstance(assets, list)
        or len(assets) != 2
        or any(not isinstance(asset, Mapping) for asset in assets)
    ):
        raise ValueError("public release must contain exactly the candidate asset pair")
    manifest = candidate["preview_manifest"]
    origin = {
        "repository": repository,
        "release_id": str(release_id),
        "tag": tag_name,
        "prerelease": True,
    }
    stable_assets = []
    for field, descriptor in (
        ("archive_asset", manifest["contents"]["archive"]),
        ("manifest_asset", manifest["file"]),
    ):
        matching = [asset for asset in assets if asset.get("name") == descriptor["name"]]
        if len(matching) != 1:
            raise ValueError("public candidate asset is missing or ambiguous")
        asset = matching[0]
        asset_id = _artifact._expect_positive_integer(asset.get("id"), "public asset ID")
        size = _artifact._expect_positive_integer(asset.get("size"), "public asset size")
        if (
            size != descriptor["size_bytes"]
            or asset.get("state") != "uploaded"
            or asset.get("digest") not in (None, f"sha256:{descriptor['sha256']}")
            or asset.get("url") != f"{api}/releases/assets/{asset_id}"
            or asset.get("browser_download_url")
            != f"https://github.com/{repository}/releases/download/{tag_name}/{descriptor['name']}"
        ):
            raise ValueError("public asset metadata does not match the candidate")
        origin[field] = {"id": str(asset_id), **descriptor}
        stable_assets.append(
            {
                key: asset.get(key)
                for key in (
                    "id",
                    "name",
                    "size",
                    "state",
                    "digest",
                    "url",
                    "browser_download_url",
                    "created_at",
                    "updated_at",
                    "content_type",
                    "node_id",
                )
            }
        )
    if origin["archive_asset"]["id"] == origin["manifest_asset"]["id"]:
        raise ValueError("public asset IDs must be distinct")
    # Download counters and other provider telemetry are not object identity.
    stable = {
        "release": {
            key: release.get(key)
            for key in (
                "id",
                "url",
                "html_url",
                "tag_name",
                "target_commitish",
                "draft",
                "prerelease",
                "published_at",
                "created_at",
                "updated_at",
                "node_id",
            )
        },
        "ref": {"ref": ref["ref"], "object": dict(ref_object)},
        "tag": {key: tag.get(key) for key in ("sha", "tag", "object", "message", "tagger")},
        "assets": stable_assets,
    }
    return origin, stable


def download_public_release(
    candidate: Mapping[str, Any], release_id: int | str, destination: str | Path
) -> tuple[Path, Path, Mapping[str, Any]]:
    """Refuse changed/nonpublic identities; never use credentials or retained payload bytes."""
    candidate = _artifact._validate_candidate_binding(candidate)
    if isinstance(release_id, str):
        release_id = int(_artifact._expect_identifier(release_id, "public release ID"))
    release_id = _artifact._expect_positive_integer(release_id, "public release ID")
    if any(part in (".", "..") for part in candidate["destination"]["repository"].split("/")):
        raise ValueError("public repository path is invalid")
    manifest = candidate["preview_manifest"]
    descriptors = (manifest["contents"]["archive"], manifest["file"])
    for descriptor, limit in zip(descriptors, (_ARCHIVE_LIMIT, _JSON_LIMIT), strict=True):
        if descriptor["size_bytes"] > limit:
            raise ValueError("candidate public asset exceeds the download bound")
    try:
        destination = Path(destination).absolute()
        if destination.exists() or destination.is_symlink():
            raise ValueError("public asset destination already exists")
        for parent in destination.parents:
            attributes = parent.lstat()
            if stat.S_ISLNK(attributes.st_mode) or (
                getattr(attributes, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
            ):
                raise ValueError("public asset destination has a redirected parent")
        destination.mkdir(exist_ok=False)
        # A private opener has no global cookies/auth handlers or environment proxies.
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        opener.addheaders = []
        origin, before = _snapshot(opener, candidate, release_id)
        paths = []
        payload_hash = hashlib.sha256()
        api = f"https://api.github.com/repos/{origin['repository']}"
        for field, descriptor in zip(("archive_asset", "manifest_asset"), descriptors, strict=True):
            path = destination / descriptor["name"]
            digest = hashlib.sha256()
            with _get(
                opener, f"{api}/releases/assets/{origin[field]['id']}", binary=True
            ) as response:
                with path.open("xb") as output:
                    for chunk in _chunks(
                        response, descriptor["size_bytes"], exact_size=descriptor["size_bytes"]
                    ):
                        output.write(chunk)
                        digest.update(chunk)
                        payload_hash.update(chunk)
            if digest.hexdigest() != descriptor["sha256"]:
                raise ValueError("public asset raw SHA-256 does not match the candidate")
            paths.append(path)
        if payload_hash.hexdigest() != candidate["build"]["artifact"]["sha256"]:
            raise ValueError("public raw pair digest does not match the candidate payload")
        after_origin, after = _snapshot(opener, candidate, release_id)
        if before != after or origin != after_origin:
            raise ValueError("public release, tag or assets changed during download")
        return paths[0], paths[1], _artifact._freeze(origin)
    except (OSError, URLError, HTTPException):
        raise ValueError("fresh public release bytes could not be downloaded") from None
