"""Actual HTTP and Git fixture contract. This does not create a live forge PR."""

import base64
from contextlib import AbstractContextManager
from copy import deepcopy
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from urllib.parse import parse_qs

import httpx
import pytest

from src.forge.managed_github import ManagedGitHubPublisher
from src.git_manager.managed_composition import CompositionLimits
from src.polyphony.managed_client import ManagedAuthorityClient
from src.worker.managed_publication import (
    CanonicalPublicationReader,
    PublicationService,
)
from tests.unit.test_managed_composition import final_case
from tests.unit.test_managed_publication import FixtureReader, policy

AUTHORITY_TOKEN = "fixture-authority-" + "a" * 32
FORGE_TOKEN = "fixture-forge-" + "b" * 32


def git_bytes(repository, *arguments, content=b"", environment=None):
    return subprocess.run(
        ["git", "--git-dir=" + str(repository), *arguments],
        input=content,
        capture_output=True,
        check=True,
        timeout=10,
        env=environment,
    ).stdout


class LocalGitHubTransport(httpx.AsyncBaseTransport):
    """Trusted test dependency routes the fixed GitHub destination to real HTTP."""

    def __init__(self, fixture):
        self.fixture = fixture

    async def handle_async_request(self, request):
        assert request.url.scheme == "https" and request.url.host == "api.github.com"
        routed = httpx.Request(
            request.method,
            self.fixture.endpoint + request.url.raw_path.decode(),
            headers=request.headers,
            content=await request.aread(),
        )
        async with httpx.AsyncHTTPTransport(retries=0, trust_env=False) as transport:
            response = await transport.handle_async_request(routed)
            content = await response.aread()
            return httpx.Response(
                response.status_code,
                headers=response.headers,
                stream=httpx.ByteStream(content),
                request=request,
            )


class ContractFixture(AbstractContextManager):
    def __init__(self, case, root):
        self.case, self.root = case, root
        self.repository = root / "remote.git"
        subprocess.run(
            ["git", "clone", "-q", "--bare", str(case["source"]), str(self.repository)],
            check=True,
            timeout=10,
        )
        self.base = case["inputs"]["manifest"]["base"]
        git_bytes(self.repository, "update-ref", "refs/heads/main", self.base)
        self.reader = FixtureReader(case)
        self.requests, self.effects, self.errors, self.proposals = [], [], [], []
        self.drop_after, self.tamper_object, self.redirect, self.before_draft = (
            None,
            False,
            False,
            None,
        )
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.serve("GET")

            def do_POST(self):
                self.serve("POST")

            def serve(self, method):
                try:
                    self.connection.settimeout(5)
                    path, _, query = self.path.partition("?")
                    length = int(self.headers.get("Content-Length", "0"))
                    assert length <= 8_000_000
                    payload = json.loads(self.rfile.read(length)) if length else None
                    fixture.requests.append(
                        (method, path, parse_qs(query), dict(self.headers), payload)
                    )
                    status, value, effect = fixture.route(
                        method, path, parse_qs(query), dict(self.headers), payload
                    )
                    if effect and fixture.drop_after == effect:
                        fixture.drop_after = None
                        self.close_connection = True
                        self.connection.shutdown(socket.SHUT_RDWR)
                        self.connection.close()
                        return
                    content = (
                        value.encode()
                        if isinstance(value, str)
                        else json.dumps(value).encode()
                    )
                    self.send_response(status)
                    if status == 302:
                        self.send_header(
                            "Location", fixture.endpoint + "/must-not-follow"
                        )
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(content)))
                    self.end_headers()
                    self.wfile.write(content)
                except Exception as exc:
                    fixture.errors.append(type(exc).__name__)
                    self.send_response(500)
                    self.end_headers()

            def log_message(self, *_):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.endpoint = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def _commit(self, sha):
        raw = git_bytes(self.repository, "cat-file", "commit", sha).decode()
        header = raw.split("\n\n", 1)[0].splitlines()
        return {
            "sha": sha,
            "tree": {"sha": header[0].removeprefix("tree ")},
            "parents": [
                {"sha": line.removeprefix("parent ")}
                for line in header
                if line.startswith("parent ")
            ],
        }

    def _ref(self, branch):
        result = subprocess.run(
            [
                "git",
                "--git-dir=" + str(self.repository),
                "show-ref",
                "--verify",
                "--hash",
                "refs/heads/" + branch,
            ],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else None

    def route(self, method, path, query, headers, payload):
        if path.startswith("/managed/"):
            assert (
                method == "GET"
                and headers.get("Authorization") == "Bearer " + AUTHORITY_TOKEN
            )
            assert query.get("deployment_id") == ["publication-test"]
            assert query.get("repository") == ["github:github.com/owner/project"]
            return (
                200,
                self.reader._get(
                    path.removeprefix("/managed/"), query=query, deadline=1e20
                ),
                None,
            )
        assert headers.get("Authorization") == "Bearer " + FORGE_TOKEN
        assert path.startswith("/repos/owner/project")
        assert method in {"GET", "POST"}
        if self.redirect:
            return 302, {"error": FORGE_TOKEN}, None
        resource = path.removeprefix("/repos/owner/project")
        if method == "GET" and resource == "":
            return 200, {"id": 123, "full_name": "owner/project"}, None
        if method == "GET" and resource.startswith("/git/ref/heads/"):
            branch = resource.removeprefix("/git/ref/heads/")
            sha = self._ref(branch)
            return (
                (
                    200,
                    {
                        "ref": "refs/heads/" + branch,
                        "object": {"type": "commit", "sha": sha},
                    },
                    None,
                )
                if sha
                else (404, {}, None)
            )
        if method == "GET" and resource.startswith("/git/commits/"):
            return 200, self._commit(resource.rsplit("/", 1)[1]), None
        if method == "POST" and resource == "/git/blobs":
            assert (
                set(payload) == {"encoding", "content"}
                and payload["encoding"] == "base64"
            )
            raw = base64.b64decode(payload["content"], validate=True)
            sha = (
                git_bytes(self.repository, "hash-object", "-w", "--stdin", content=raw)
                .decode()
                .strip()
            )
            self.effects.append(("blob", sha))
            return (
                201,
                {"sha": "f" * 40 if self.tamper_object == "blob" else sha},
                "blob",
            )
        if method == "POST" and resource == "/git/trees":
            assert set(payload) == {"tree"}
            environment = dict(os.environ) | {
                "GIT_INDEX_FILE": str(self.root / "remote-index")
            }
            git_bytes(self.repository, "read-tree", "--empty", environment=environment)
            for item in payload["tree"]:
                assert item["type"] == "blob"
                git_bytes(
                    self.repository,
                    "update-index",
                    "--add",
                    "--cacheinfo",
                    item["mode"],
                    item["sha"],
                    item["path"],
                    environment=environment,
                )
            sha = (
                git_bytes(self.repository, "write-tree", environment=environment)
                .decode()
                .strip()
            )
            self.effects.append(("tree", sha))
            return (
                201,
                {"sha": "f" * 40 if self.tamper_object == "tree" else sha},
                "tree",
            )
        if method == "POST" and resource == "/git/commits":
            assert len(payload["parents"]) == 1 and payload["parents"][0] == self.base

            def person(value):
                stamp = int(
                    datetime.fromisoformat(
                        value["date"].replace("Z", "+00:00")
                    ).timestamp()
                )
                return f"{value['name']} <{value['email']}> {stamp} +0000"

            raw = (
                f"tree {payload['tree']}\nparent {self.base}\nauthor {person(payload['author'])}\n"
                f"committer {person(payload['committer'])}\n\n{payload['message']}"
            ).encode()
            sha = (
                git_bytes(
                    self.repository,
                    "hash-object",
                    "-t",
                    "commit",
                    "-w",
                    "--stdin",
                    content=raw,
                )
                .decode()
                .strip()
            )
            self.effects.append(("commit", sha))
            response = self._commit(sha)
            if self.tamper_object == "commit":
                response["sha"] = "f" * 40
            return 201, response, "commit"
        if method == "POST" and resource == "/git/refs":
            assert payload["ref"].startswith("refs/heads/openjules/managed/")
            branch = payload["ref"].removeprefix("refs/heads/")
            assert self._ref(branch) is None
            git_bytes(
                self.repository, "update-ref", payload["ref"], payload["sha"], "0" * 40
            )
            self.effects.append(("branch", branch))
            return (
                201,
                {
                    "ref": payload["ref"],
                    "object": {"type": "commit", "sha": payload["sha"]},
                },
                "branch",
            )
        if method == "GET" and resource == "/pulls":
            assert query["state"] == ["all"] and query["base"] == ["main"]
            assert query["head"][0].startswith("owner:openjules/managed/")
            return 200, deepcopy(self.proposals), None
        if method == "POST" and resource == "/pulls":
            assert (
                payload["draft"] is True and payload["maintainer_can_modify"] is False
            )
            assert payload["base"] == "main" and not self.proposals
            if self.before_draft:
                self.before_draft()
            proposal = {
                "number": 1,
                "html_url": "https://github.com/owner/project/pull/1",
                "state": "open",
                "draft": True,
                "body": payload["body"],
                "head": {
                    "ref": payload["head"],
                    "sha": self._ref(payload["head"]),
                    "repo": {"id": 123},
                },
                "base": {"ref": "main", "sha": self._ref("main"), "repo": {"id": 123}},
            }
            self.proposals.append(proposal)
            self.effects.append(("draft", 1))
            return 201, proposal, "draft"
        raise AssertionError("unsupported fixture request")

    def service(self, state):
        approved = policy()
        reader = CanonicalPublicationReader(
            ManagedAuthorityClient(self.endpoint, "env:AUTHORITY_TEST_TOKEN"),
            approved,
            self.base,
            CompositionLimits(),
        )
        publisher = ManagedGitHubPublisher(
            approved, transport=LocalGitHubTransport(self)
        )
        return PublicationService(reader, publisher, self.case["source"], state)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *_):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(3)


@pytest.fixture
def actual(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTHORITY_TEST_TOKEN", AUTHORITY_TOKEN)
    monkeypatch.setenv("PUBLICATION_TEST_TOKEN", FORGE_TOKEN)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:1")
    case = final_case(tmp_path)
    with ContractFixture(case, tmp_path) as fixture:
        yield case, fixture, fixture.service(tmp_path / "state")
        assert not fixture.errors


def test_real_http_uploads_actual_git_objects_and_creates_only_a_replayable_draft(
    actual,
):
    case, fixture, service = actual
    cid = case["inputs"]["manifest"]["candidate_id"]
    result = service.run(cid)
    assert result["status"] == "draft_published"
    assert fixture._commit(result["commit_sha"])["tree"]["sha"] == result["tree_sha"]
    assert fixture.proposals[0]["draft"] is True
    before = list(fixture.effects)
    restarted = fixture.service(service.journal.root)
    assert restarted.run(cid) == result and fixture.effects == before
    for method, path, query, headers, payload in fixture.requests:
        assert method in {"GET", "POST"}
        assert not any(piece in path for piece in ("merge", "ready", "review"))
        assert FORGE_TOKEN not in json.dumps(query) + json.dumps(payload)
        assert AUTHORITY_TOKEN not in json.dumps(query) + json.dumps(payload)
        assert headers.get("Authorization") == "Bearer " + (
            AUTHORITY_TOKEN if path.startswith("/managed/") else FORGE_TOKEN
        )
    encoded = json.dumps(service.journal.read(result["operation"]))
    assert FORGE_TOKEN not in encoded and AUTHORITY_TOKEN not in encoded
    assert not result["release_authorized"] and not result["ready_for_review"]


@pytest.mark.parametrize("effect", ["blob", "tree", "commit", "branch", "draft"])
def test_real_lost_http_reply_keeps_exact_branch_and_pr_identity(actual, effect):
    case, fixture, service = actual
    cid = case["inputs"]["manifest"]["candidate_id"]
    fixture.drop_after = effect
    first = service.run(cid)
    assert first["status"] == "uncertain"
    recovered = fixture.service(service.journal.root).run(cid)
    assert recovered["status"] == "draft_published"
    assert recovered["commit_sha"] == first["commit_sha"]
    assert sum(item[0] == "draft" for item in fixture.effects) == 1
    assert sum(item[0] == "branch" for item in fixture.effects) == 1


@pytest.mark.parametrize("kind", ["blob", "tree", "commit"])
def test_tampered_object_response_blocks_ref_creation(actual, kind):
    case, fixture, service = actual
    fixture.tamper_object = kind
    result = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert (
        result["status"] == "conflict" and result["reason"] == "forge_object_mismatch"
    )
    assert all(item[0] not in {"branch", "draft"} for item in fixture.effects)


def test_redirect_is_not_followed_and_error_body_secret_is_not_reported(actual):
    case, fixture, service = actual
    fixture.redirect = True
    result = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert result["status"] == "unavailable"
    assert all(path != "/must-not-follow" for _, path, _, _, _ in fixture.requests)
    assert FORGE_TOKEN not in json.dumps(result)


def test_closed_and_changed_forge_proposals_are_not_replaced(actual):
    case, fixture, service = actual
    cid = case["inputs"]["manifest"]["candidate_id"]
    first = service.run(cid)
    fixture.proposals[0]["state"] = "closed"
    assert service.run(cid)["status"] == "existing_closed"
    fixture.proposals[0]["head"]["sha"] = "f" * 40
    changed = service.run(cid)
    assert (
        changed["status"] == "uncertain"
        and changed["reason"] == "forge_proposal_mismatch"
    )
    assert changed["proposal"]["url"] == first["proposal"]["url"]
    assert sum(item[0] == "draft" for item in fixture.effects) == 1


def test_actual_transport_detects_canonical_revision_change_during_draft_creation(
    actual,
):
    case, fixture, service = actual
    fixture.before_draft = lambda: case["snapshot"]["task"][0].update(revision=2)
    result = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert result["status"] == "draft_stale" and result["proposal"]["draft"]
    assert not result["release_authorized"]


def test_same_repository_with_display_case_in_url_is_reconciled_without_redirect(
    actual,
):
    case, fixture, service = actual
    cid = case["inputs"]["manifest"]["candidate_id"]
    assert service.run(cid)["status"] == "draft_published"
    fixture.proposals[0]["html_url"] = "https://github.com/Owner/Project/pull/1"
    observed = service.run(cid)
    assert observed["status"] == "draft_published"
    assert observed["proposal"]["url"] == "https://github.com/Owner/Project/pull/1"
    fixture.proposals[0]["html_url"] = "https://github.com/foreign/project/pull/1"
    assert service.run(cid)["reason"] == "forge_proposal_mismatch"
    assert sum(item[0] == "draft" for item in fixture.effects) == 1


def test_numeric_repository_identity_mismatch_prevents_all_mutations(
    actual, monkeypatch
):
    case, fixture, service = actual
    route = fixture.route

    def renamed(method, path, *args):
        if method == "GET" and path == "/repos/owner/project":
            return 200, {"id": 999, "full_name": "owner/project"}, None
        return route(method, path, *args)

    monkeypatch.setattr(fixture, "route", renamed)
    result = service.run(case["inputs"]["manifest"]["candidate_id"])
    assert result["reason"] == "repository_identity_mismatch" and not fixture.effects


def test_existing_wrong_feature_head_cannot_be_overwritten(actual):
    from src.git_manager.managed_composition import digest

    case, fixture, service = actual
    cid = case["inputs"]["manifest"]["candidate_id"]
    operation = digest([policy().deployment_id, policy().repository, cid])
    branch = policy().branch_prefix + operation
    git_bytes(fixture.repository, "update-ref", "refs/heads/" + branch, fixture.base)
    result = service.run(cid)
    assert result["reason"] == "feature_branch_changed"
    assert fixture._ref(branch) == fixture.base
    assert all(item[0] not in {"branch", "draft"} for item in fixture.effects)


def test_absent_pr_after_lost_http_reply_never_resends_creation(actual):
    case, fixture, service = actual
    cid = case["inputs"]["manifest"]["candidate_id"]
    fixture.drop_after = "draft"
    assert service.run(cid)["status"] == "uncertain"
    fixture.proposals.clear()
    result = service.run(cid)
    assert (
        result["status"] == "uncertain"
        and result["reason"] == "pr_creation_requires_reconciliation"
    )
    assert sum(item[0] == "draft" for item in fixture.effects) == 1
