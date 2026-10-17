"""Native Git regressions; every repository and substituted send stays in tmp_path."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shlex
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import stateless_preview_artifact as artifact_contract


REPOSITORY = "thebtf/netcoredbg-mcp"
VERSION = "1.2.3-preview.4"
TAG = f"stateless-preview-v{VERSION}"
PUSH_URL = f"https://github.com/{REPOSITORY}.git"
JOB_TOKEN = "fixture-job-token-not-a-real-credential"
SENTINEL = "CHECKOUT_AUTH_SENTINEL_MUST_NOT_ESCAPE"


def _git(root: Path, environment: dict[str, str], *arguments: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(
        ["git", *arguments],
        cwd=root,
        env=environment,
        input=data,
        capture_output=True,
        check=False,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr.decode("utf-8", errors="replace")
    return result.stdout


def _candidate(commit: str) -> dict:
    def descriptor(name: str, raw: bytes) -> dict:
        return {"name": name, "size_bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()}

    archive = descriptor(f"netcoredbg-mcp-stateless-preview-win-x64-{VERSION}.zip", b"archive fixture")
    manifest = {
        "schema_version": "1.0",
        "version": VERSION,
        "tag": TAG,
        "commit": commit,
        "rid": "win-x64",
        "archive": archive,
        "executable": descriptor("netcoredbg-mcp-stateless-preview.exe", b"executable fixture"),
    }
    manifest_file = descriptor(
        f"netcoredbg-mcp-stateless-preview-win-x64-{VERSION}.manifest.json",
        json.dumps(manifest, sort_keys=True).encode(),
    )
    references = {
        kind: {
            "repository": REPOSITORY,
            "run_id": "101",
            "artifact_id": "501",
            "path": file["name"],
            "sha256": file["sha256"],
        }
        for kind, file in (("manifest_reference", manifest_file), ("archive_reference", archive))
    }
    return artifact_contract.assemble_candidate_identity(
        {
            "schema_version": "1.0",
            "candidate": {
                "source": {
                    "repository": REPOSITORY,
                    "ref": "refs/heads/main",
                    "commit": commit,
                    "origin_main_target": commit,
                },
                "build": {
                    "repository": REPOSITORY,
                    "workflow_path": ".github/workflows/stateless-preview.yml",
                    "mode": "build",
                    "event": "workflow_dispatch",
                    "run_id": "101",
                    "run_attempt": 1,
                    "ref": "refs/heads/main",
                    "commit": commit,
                    "artifact": {
                        "id": "501",
                        "name": "native-git-fixture-payload",
                        "sha256": hashlib.sha256(b"payload fixture").hexdigest(),
                        "retention": {"configured_days": 7, "expires_at": "2030-01-01T00:00:00Z"},
                    },
                },
                "preview_manifest": {"file": manifest_file, "contents": manifest, **references},
                "destination": {
                    "provider": "github",
                    "repository": REPOSITORY,
                    "tag": TAG,
                    "prerelease": True,
                },
            },
        }
    )


@pytest.fixture
def native_transport(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capfd):
    source, remote, trap, runner, home, templates = (
        tmp_path / name for name in ("source", "remote.git", "trap.git", "runner", "home", "templates")
    )
    for directory in (source, runner, home, templates):
        directory.mkdir()
    clean = {
        key: os.environ[key]
        for key in ("PATH", "SYSTEMROOT", "WINDIR", "COMSPEC", "PATHEXT")
        if key in os.environ
    }
    clean.update(
        HOME=str(home),
        USERPROFILE=str(home),
        XDG_CONFIG_HOME=str(home),
        TEMP=str(runner),
        TMP=str(runner),
        LC_ALL="C",
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_SYSTEM=os.devnull,
        GIT_CONFIG_GLOBAL=os.devnull,
        GIT_ALLOW_PROTOCOL="file",
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Native Git fixture",
        GIT_AUTHOR_EMAIL="native-git-fixture@example.invalid",
        GIT_AUTHOR_DATE="1700000000 +0000",
        GIT_COMMITTER_NAME="Native Git fixture",
        GIT_COMMITTER_EMAIL="native-git-fixture@example.invalid",
        GIT_COMMITTER_DATE="1700000000 +0000",
    )
    _git(source, clean, "init", "--initial-branch=main", f"--template={templates.as_posix()}")
    for repository in (remote, trap):
        _git(tmp_path, clean, "init", "--bare", f"--template={templates.as_posix()}", str(repository))
    blob = _git(source, clean, "hash-object", "-w", "--stdin", data=b"native Git fixture\n").strip()
    tree = _git(source, clean, "mktree", data=b"100644 blob " + blob + b"\tfixture.txt\n").strip()
    commit = _git(source, clean, "commit-tree", tree.decode(), data=b"Controlled source commit\n").decode().strip()
    for ref in ("refs/heads/main", "refs/remotes/origin/main"):
        _git(source, clean, "update-ref", ref, commit)

    hooks = tmp_path / "untrusted-hooks"
    hooks.mkdir()
    hook_marker, auth_marker = tmp_path / "hook-ran", tmp_path / "credential-helper-ran"
    for script, marker in ((hooks / "pre-push", hook_marker), (tmp_path / "credential-helper", auth_marker)):
        script.write_text(
            f"#!/bin/sh\nprintf '%s\\n' invoked > {shlex.quote(marker.as_posix())}\nexit 97\n",
            encoding="utf-8",
            newline="\n",
        )
        script.chmod(script.stat().st_mode | 0o111)
    included, ambient = tmp_path / "included.gitconfig", home / "ambient.gitconfig"
    included.write_text(
        f"[http]\n\textraHeader = AUTHORIZATION: basic {SENTINEL}\n"
        f"[credential]\n\tusername = {SENTINEL}\n",
        encoding="utf-8",
    )
    ambient.write_bytes(included.read_bytes())
    config_path = source / ".git" / "config"
    with config_path.open("a", encoding="utf-8", newline="\n") as output:
        output.write(
            f"[http]\n\textraHeader = AUTHORIZATION: basic {SENTINEL}\n"
            "\tproxy = http://127.0.0.1:1\n"
            f"[credential]\n\thelper = {json.dumps('!sh ' + shlex.quote((tmp_path / 'credential-helper').as_posix()))}\n"
            f"[include]\n\tpath = {json.dumps(included.as_posix())}\n"
            f"[core]\n\thooksPath = {json.dumps(hooks.as_posix())}\n"
            f"[url {json.dumps(trap.as_posix())}]\n\tinsteadOf = {PUSH_URL}\n"
            f"\tpushInsteadOf = {remote.as_posix()}\n"
            f"[remote \"origin\"]\n\turl = {PUSH_URL}\n\tpushurl = {trap.as_posix()}\n"
            f"[a1fixture]\n\tcheckoutOnly = {SENTINEL}\n"
        )
    preserved = {path: path.read_bytes() for path in (config_path, included, ambient)}
    identity = _candidate(commit)
    identity_raw = json.dumps(identity, sort_keys=True, separators=(",", ":")).encode() + b"\n"
    identity_hash = hashlib.sha256(identity_raw).hexdigest()
    reference = {
        "repository": REPOSITORY,
        "run_id": "101",
        "artifact": {
            "id": "502",
            "name": "native-git-fixture-records",
            "sha256": hashlib.sha256(b"records fixture").hexdigest(),
            "retention": {"configured_days": 7, "expires_at": "2030-01-01T00:00:00Z"},
        },
        "path": "candidate-identity.json",
        "sha256": identity_hash,
    }
    decision_hash = hashlib.sha256(b"decision fixture").hexdigest()
    attempt = {
        "candidate": identity["candidate"],
        "recorded_at": "2023-11-14T22:13:20Z",
        "candidate_identity_record_sha256": identity_hash,
        "promotion_decision_sha256": decision_hash,
    }
    raw_tag = (
        f"object {commit}\ntype commit\ntag {TAG}\n"
        "tagger github-actions[bot] <41898282+github-actions[bot]@users.noreply.github.com> "
        "1700000000 +0000\n\nA1 opt-in preview\n"
        f"candidate-identity-sha256 {identity_hash}\npromotion-decision-sha256 {decision_hash}\n"
    ).encode()
    expected_oid = hashlib.sha1(b"tag " + str(len(raw_tag)).encode() + b"\0" + raw_tag).hexdigest()
    environment = {
        **clean,
        "GITHUB_TOKEN": JOB_TOKEN,
        "GITHUB_REPOSITORY": REPOSITORY,
        "GITHUB_REF": "refs/heads/main",
        "GITHUB_SHA": commit,
        "GITHUB_WORKSPACE": str(source),
        "GITHUB_RUN_ID": "801",
        "GITHUB_RUN_ATTEMPT": "2",
        "RUNNER_TEMP": str(runner),
        "GITHUB_OUTPUT": str(runner / "outputs"),
        "GH_TOKEN": SENTINEL,
        "GIT_DIR": str(trap),
        "GIT_OBJECT_DIRECTORY": str(trap / "objects"),
        "GIT_ALTERNATE_OBJECT_DIRECTORIES": str(trap / "objects"),
        "GIT_CONFIG_GLOBAL": str(ambient),
        "GIT_CONFIG_SYSTEM": str(ambient),
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {SENTINEL}",
        "HTTPS_PROXY": "http://127.0.0.1:1",
    }
    inputs = {
        "root": source,
        "environment": environment,
        "candidate": identity["candidate"],
        "binding": {"candidate_identity_reference": reference},
        "context": {"current": {"current_attempt_id": "fixture-attempt-801-2-901"}},
        "locations": {"stage_path": str(runner / "stage.json")},
        "diagnostics": [],
    }
    intent_path = runner / "write-1-tag.json"
    pushes, constructed = [], []
    original_process = artifact_contract._promotion_process

    def process(root, child, command, *, data=None, limit=artifact_contract._PROMOTION_JSON_LIMIT):
        assert Path(root).resolve().is_relative_to(tmp_path.resolve())
        command = list(command)
        assert command[0] == "git", "native Git boundary must not invoke another transport"
        verbs = {"init", "config", "rev-parse", "cat-file", "mktag", "push", "count-objects"}
        index = next(index for index, word in enumerate(command[1:], 1) if word in verbs)
        verb = command[index]
        assert all("://" not in word or (verb == "push" and word == PUSH_URL) for word in command)
        if verb in ("mktag", "push"):
            prefix = command[:index]

            def inspect(*arguments):
                code, raw = original_process(root, child, [*prefix, *arguments])
                assert code == 0, f"native Git inspection failed: {arguments}"
                return raw

            git_dir = Path(inspect("rev-parse", "--absolute-git-dir").decode().strip()).resolve()
            assert git_dir.is_relative_to(runner.resolve())
            assert git_dir != (source / ".git").resolve()
            assert inspect("rev-parse", "--is-bare-repository").strip() == b"true"
            alternates = [
                Path(line.removeprefix("alternate: ")).resolve()
                for line in inspect("count-objects", "-v").decode().splitlines()
                if line.startswith("alternate: ")
            ]
            assert alternates == [(source / ".git" / "objects").resolve()]
            assert not (git_dir / "objects" / commit[:2] / commit[2:]).exists()
            assert inspect("cat-file", "-t", commit).strip() == b"commit"
            settings = [
                tuple(row.decode().split("\n", 1))
                for row in inspect("config", "--null", "--list").split(b"\0")
                if row
            ]
            effective = {key.lower(): value for key, value in settings}
            assert not any(key.startswith(("remote.", "url.", "include.", "includeif.", "a1fixture.")) for key in effective)
            assert SENTINEL not in repr(settings)
            assert child["GIT_CONFIG_GLOBAL"] == child["GIT_CONFIG_SYSTEM"] == os.devnull
            assert child["GIT_TERMINAL_PROMPT"] == "0" and "GH_TOKEN" not in child
            assert effective["credential.helper"] == ""
            assert effective["credential.interactive"] == effective["http.followredirects"] == "false"
            assert effective["http.proxy"] == "" and effective["core.hookspath"] == os.devnull
            header = "AUTHORIZATION: basic " + base64.b64encode(f"x-access-token:{JOB_TOKEN}".encode()).decode()
            assert [(key.lower(), value) for key, value in settings if key.lower().endswith("extraheader") and value] == [
                (f"http.https://github.com/{REPOSITORY}.git.extraheader", header)
            ]
            assert JOB_TOKEN not in " ".join(command) and SENTINEL not in repr(dict(child))
        if verb == "mktag":
            assert data == raw_tag
            constructed.append(data)
        if verb == "push":
            pushes.append(command.copy())
            assert len(pushes) == 1, "the original intended write must never be resent"
            assert command.count(PUSH_URL) == 1
            assert f"--force-with-lease=refs/tags/{TAG}:" in command
            assert command[-1] == f"{expected_oid}:refs/tags/{TAG}"
            assert "--force" not in command and "--mirror" not in command
            assert intent_path.is_file(), "durable intent must precede the native push"
            intent_raw = intent_path.read_bytes()
            assert json.loads(intent_raw) == {
                "origin_attempt_id": "fixture-attempt-801-2-901",
                "operation": "CREATE_TAG",
                "target": {
                    "repository": REPOSITORY,
                    "candidate_identity_record": {key: reference[key] for key in ("repository", "run_id", "path", "sha256")} | {"artifact_id": "502"},
                    "tag": TAG,
                    "object_id": expected_oid,
                },
            }
            result, = inputs["operation_results"]
            assert result["intent_sha256"] == hashlib.sha256(intent_raw).hexdigest()
            assert result["response"]["kind"] == "UNKNOWN"
            assert f"tag_intent_path={intent_path}" in Path(environment["GITHUB_OUTPUT"]).read_text()
            command[command.index(PUSH_URL)] = remote.as_posix()
        return original_process(root, child, command, data=data, limit=limit)

    monkeypatch.setattr(artifact_contract, "_promotion_process", process)
    yield SimpleNamespace(
        inputs=inputs, attempt=attempt, source=source, remote=remote, clean=clean,
        commit=commit, raw_tag=raw_tag, oid=expected_oid, pushes=pushes, constructed=constructed,
    )
    assert all(path.read_bytes() == raw for path, raw in preserved.items())
    assert not hook_marker.exists() and not auth_marker.exists()
    assert _git(trap, clean, "for-each-ref") == b""
    captured = capfd.readouterr()
    logs = captured.out + captured.err + json.dumps(inputs["diagnostics"])
    assert SENTINEL not in logs and JOB_TOKEN not in logs


def test_checkout_transport_settings_cannot_control_exact_tag_push(native_transport):
    fixture = native_transport

    oid = artifact_contract._promotion_create_tag(fixture.inputs, fixture.attempt, 1)

    assert oid == fixture.oid
    assert fixture.constructed == [fixture.raw_tag] and len(fixture.pushes) == 1
    assert _git(fixture.remote, fixture.clean, "rev-parse", f"refs/tags/{TAG}").strip() == oid.encode()
    assert _git(fixture.remote, fixture.clean, "cat-file", "tag", oid) == fixture.raw_tag
    assert _git(fixture.remote, fixture.clean, "rev-parse", f"refs/tags/{TAG}^{{commit}}").strip() == fixture.commit.encode()
    assert _git(fixture.remote, fixture.clean, "show", f"{fixture.commit}:fixture.txt") == b"native Git fixture\n"
    result, = fixture.inputs["operation_results"]
    assert result["response"]["kind"] == "ACKNOWLEDGED"
    assert result["response"]["git_returncode"] == 0


def test_preexisting_tag_is_denied_without_overwrite_or_retry(native_transport):
    fixture = native_transport
    _git(
        fixture.remote, fixture.clean, "fetch", "--no-tags", fixture.source.as_posix(),
        f"refs/heads/main:refs/tags/{TAG}",
    )
    before = _git(fixture.remote, fixture.clean, "show-ref", "--tags")

    assert artifact_contract._promotion_create_tag(fixture.inputs, fixture.attempt, 1) == fixture.oid

    assert fixture.constructed == [fixture.raw_tag] and len(fixture.pushes) == 1
    assert _git(fixture.remote, fixture.clean, "show-ref", "--tags") == before
    assert _git(fixture.remote, fixture.clean, "rev-parse", f"refs/tags/{TAG}").strip() == fixture.commit.encode()
    result, = fixture.inputs["operation_results"]
    assert result["response"]["kind"] == "DENIED"
    assert result["response"]["git_returncode"] != 0
