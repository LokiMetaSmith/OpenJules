"""Scoped GitHub object upload and create-once draft publication.

There are deliberately no ref-update, ready, review, acceptance or merge methods.
All provider errors are normalized without copying response bodies or headers.
"""

from __future__ import annotations

import asyncio
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
import math
import os
import re
import time
from urllib.parse import quote, urlsplit

import httpx

from src.git_manager.managed_composition import SHA, canonical, digest


class ForgeError(RuntimeError):
    def __init__(self, code, *, uncertain=False):
        self.code, self.uncertain = code, uncertain
        super().__init__(code)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _branch(value):
    return (
        isinstance(value, str)
        and 1 <= len(value) <= 200
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)
        and not any(
            part in {"", ".", ".."}
            or part.startswith(".")
            or part.endswith((".", ".lock"))
            for part in value.split("/")
        )
        and ".." not in value
    )


@dataclass(frozen=True)
class PublicationPolicy:
    deployment_id: str
    repository: str
    repository_id: int
    target_branch: str
    token_ref: str
    author_name: str
    author_email: str
    branch_prefix: str = "openjules/managed/"
    timeout_seconds: float = 10
    max_response_bytes: int = 8_000_000
    max_request_bytes: int = 8_000_000

    def __post_init__(self):
        if not isinstance(self.deployment_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}", self.deployment_id
        ):
            raise ValueError("invalid publication deployment")
        if not isinstance(self.repository, str) or not re.fullmatch(
            r"github:github\.com/[a-z0-9_.-]+/[a-z0-9_.-]+", self.repository
        ):
            raise ValueError(
                "managed publication supports an explicit GitHub repository"
            )
        if (
            any(part in {".", ".."} for part in self.full_name.split("/"))
            or type(self.repository_id) is not int
            or self.repository_id <= 0
        ):
            raise ValueError("an immutable numeric repository identity is required")
        if (
            not _branch(self.target_branch)
            or not isinstance(self.branch_prefix, str)
            or not self.branch_prefix.endswith("/")
            or not _branch(self.branch_prefix + "x")
            or len(self.branch_prefix) > 100
        ):
            raise ValueError("invalid publication branch policy")
        if self.target_branch.startswith(self.branch_prefix):
            raise ValueError("publication target cannot be in its feature namespace")
        if not isinstance(self.token_ref, str) or not re.fullmatch(
            r"env:[A-Z_][A-Z0-9_]*", self.token_ref
        ):
            raise ValueError(
                "forge credential must be an explicit environment reference"
            )
        for value in (self.author_name, self.author_email):
            if (
                not isinstance(value, str)
                or not value
                or len(value) > 200
                or any(c in value for c in "\r\n\0<>")
            ):
                raise ValueError("invalid publication commit identity")
        if any(c.isspace() for c in self.author_email) or "@" not in self.author_email:
            raise ValueError("invalid publication commit email")
        if (
            type(self.timeout_seconds) not in {int, float}
            or not math.isfinite(self.timeout_seconds)
            or not 0 < self.timeout_seconds <= 30
        ):
            raise ValueError("invalid forge timeout")
        for value in (self.max_response_bytes, self.max_request_bytes):
            if type(value) is not int or not 1024 <= value <= 8_000_000:
                raise ValueError("invalid forge byte limit")

    @property
    def full_name(self):
        return self.repository.removeprefix("github:github.com/")

    @property
    def fingerprint(self):
        return digest(asdict(self))


class ManagedGitHubPublisher:
    def __init__(self, policy: PublicationPolicy, *, transport=None):
        self.policy = policy
        # Transport injection is a trusted library dependency, never a config or
        # repository-controlled endpoint. Production always targets api.github.com.
        self.transport = transport

    def credential(self):
        token = os.environ.get(self.policy.token_ref[4:], "")
        if not 1 <= len(token) <= 8192 or any(not 33 <= ord(c) <= 126 for c in token):
            raise ForgeError("publication_unavailable")
        return token

    async def _request(self, method, resource, payload, params, deadline, absent):
        token = self.credential()
        remaining = min(self.policy.timeout_seconds, deadline - time.monotonic())
        if remaining <= 0:
            raise ForgeError("publication_deadline")
        raw = canonical(payload).encode() if payload is not None else None
        if raw is not None and len(raw) > self.policy.max_request_bytes:
            raise ForgeError("forge_request_limit")
        try:
            async with asyncio.timeout(remaining):
                async with httpx.AsyncClient(
                    transport=self.transport,
                    trust_env=False,
                    follow_redirects=False,
                    timeout=remaining,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept": "application/vnd.github+json",
                        "Accept-Encoding": "identity",
                        "Content-Type": "application/json",
                        "X-GitHub-Api-Version": "2026-03-10",
                    },
                ) as client:
                    async with client.stream(
                        method,
                        "https://api.github.com/repos/"
                        + self.policy.full_name
                        + resource,
                        content=raw,
                        params=params,
                    ) as response:
                        if absent and response.status_code == 404:
                            return None
                        if response.status_code not in (
                            {200} if method == "GET" else {201}
                        ):
                            # A failed mutation can have an ambiguous outcome.
                            # PR creation is never blindly retried by the caller.
                            raise ForgeError(
                                "forge_request_rejected", uncertain=method == "POST"
                            )
                        if response.headers.get("Content-Encoding", "identity") not in {
                            "",
                            "identity",
                        }:
                            raise ForgeError(
                                "forge_response_encoding", uncertain=method == "POST"
                            )
                        content = bytearray()
                        async for chunk in response.aiter_raw():
                            if (
                                len(content) + len(chunk)
                                > self.policy.max_response_bytes
                            ):
                                raise ForgeError(
                                    "forge_response_limit", uncertain=method == "POST"
                                )
                            content.extend(chunk)
            return json.loads(content)
        except ForgeError:
            raise
        except (OSError, ValueError, httpx.HTTPError, TimeoutError):
            raise ForgeError("forge_unavailable", uncertain=method == "POST") from None

    def _call(
        self, method, resource, *, payload=None, params=None, deadline, absent=False
    ):
        return asyncio.run(
            self._request(method, resource, payload, params, deadline, absent)
        )

    def validate_repository(self, base: str, base_tree: str, *, deadline):
        repository = self._call("GET", "", deadline=deadline)
        if (
            not isinstance(repository, dict)
            or type(repository.get("id")) is not int
            or repository.get("id") != self.policy.repository_id
            or str(repository.get("full_name", "")).casefold()
            != self.policy.full_name.casefold()
        ):
            raise ForgeError("repository_identity_mismatch")
        current = self.read_branch(self.policy.target_branch, deadline=deadline)
        if current != base:
            raise ForgeError("stale_base")
        commit = self._call("GET", "/git/commits/" + base, deadline=deadline)
        if (
            not isinstance(commit, dict)
            or commit.get("sha") != base
            or _mapping(commit.get("tree")).get("sha") != base_tree
        ):
            raise ForgeError("approved_base_mismatch")

    def read_branch(self, branch, *, deadline):
        if not _branch(branch):
            raise ForgeError("invalid_branch")
        result = self._call(
            "GET",
            "/git/ref/heads/" + quote(branch, safe="/"),
            deadline=deadline,
            absent=True,
        )
        if result is None:
            return None
        if (
            not isinstance(result, dict)
            or result.get("ref") != "refs/heads/" + branch
            or _mapping(result.get("object")).get("type") != "commit"
            or not isinstance(_mapping(result.get("object")).get("sha"), str)
            or not SHA.fullmatch(result["object"]["sha"])
        ):
            raise ForgeError("forge_reference_mismatch")
        return result["object"]["sha"]

    def upload_objects(self, composition, base, *, deadline):
        for expected, content in sorted(composition["blobs"].items()):
            blob = self._call(
                "POST",
                "/git/blobs",
                payload={
                    "content": base64.b64encode(content).decode("ascii"),
                    "encoding": "base64",
                },
                deadline=deadline,
            )
            if not isinstance(blob, dict) or blob.get("sha") != expected:
                raise ForgeError("forge_object_mismatch")
        tree = self._call(
            "POST",
            "/git/trees",
            payload={"tree": composition["entries"]},
            deadline=deadline,
        )
        if not isinstance(tree, dict) or tree.get("sha") != composition["tree_sha"]:
            raise ForgeError("forge_object_mismatch")
        identity = composition["identity"]
        person = {
            "name": identity["name"],
            "email": identity["email"],
            "date": datetime.fromtimestamp(
                identity["timestamp"], timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        commit = self._call(
            "POST",
            "/git/commits",
            payload={
                "tree": composition["tree_sha"],
                "parents": [base],
                "message": composition["commit_message"],
                "author": person,
                "committer": person,
            },
            deadline=deadline,
        )
        if (
            not isinstance(commit, dict)
            or commit.get("sha") != composition["commit_sha"]
            or _mapping(commit.get("tree")).get("sha") != composition["tree_sha"]
            or not isinstance(commit.get("parents"), list)
            or [_mapping(p).get("sha") for p in commit["parents"]] != [base]
        ):
            raise ForgeError("forge_object_mismatch")

    def create_branch(self, branch, commit, *, deadline):
        if (
            not branch.startswith(self.policy.branch_prefix)
            or not _branch(branch)
            or not SHA.fullmatch(commit)
        ):
            raise ForgeError("invalid_branch")
        result = self._call(
            "POST",
            "/git/refs",
            payload={"ref": "refs/heads/" + branch, "sha": commit},
            deadline=deadline,
        )
        if (
            not isinstance(result, dict)
            or result.get("ref") != "refs/heads/" + branch
            or _mapping(result.get("object")).get("sha") != commit
        ):
            raise ForgeError("forge_reference_mismatch", uncertain=True)

    def _proposal(self, value, branch, commit, marker):
        if not isinstance(value, dict):
            raise ForgeError("forge_proposal_mismatch")
        head, base, number = (
            _mapping(value.get("head")),
            _mapping(value.get("base")),
            value.get("number"),
        )
        try:
            url = urlsplit(
                value.get("html_url") if isinstance(value.get("html_url"), str) else ""
            )
            valid_url = (
                url.scheme == "https"
                and url.netloc == "github.com"
                and not url.query
                and not url.fragment
                and url.path.casefold()
                == f"/{self.policy.full_name}/pull/{number}".casefold()
            )
        except (ValueError, TypeError):
            valid_url = False
        if (
            type(number) is not int
            or number <= 0
            or type(_mapping(head.get("repo")).get("id")) is not int
            or type(_mapping(base.get("repo")).get("id")) is not int
            or _mapping(head.get("repo")).get("id") != self.policy.repository_id
            or _mapping(base.get("repo")).get("id") != self.policy.repository_id
            or head.get("ref") != branch
            or base.get("ref") != self.policy.target_branch
            or head.get("sha") != commit
            or not isinstance(base.get("sha"), str)
            or not SHA.fullmatch(base["sha"])
            or not isinstance(value.get("body"), str)
            or marker not in value["body"]
            or not isinstance(value.get("state"), str)
            or value["state"] not in {"open", "closed"}
            or type(value.get("draft")) is not bool
            or not valid_url
        ):
            raise ForgeError("forge_proposal_mismatch")
        return {
            "number": number,
            "url": value["html_url"],
            "state": value["state"],
            "draft": value["draft"],
            "head_sha": head["sha"],
            "base_sha": base["sha"],
            "head_branch": branch,
            "base_branch": self.policy.target_branch,
            "repository_id": self.policy.repository_id,
        }

    def find_proposal(self, branch, commit, marker, *, deadline):
        found = []
        for page in range(1, 11):
            items = self._call(
                "GET",
                "/pulls",
                params={
                    "state": "all",
                    "head": self.policy.full_name.split("/")[0] + ":" + branch,
                    "base": self.policy.target_branch,
                    "per_page": 100,
                    "page": page,
                },
                deadline=deadline,
            )
            if not isinstance(items, list) or len(items) > 100:
                raise ForgeError("forge_proposal_mismatch")
            found.extend(
                self._proposal(value, branch, commit, marker) for value in items
            )
            if len(found) > 1:
                raise ForgeError("multiple_proposals")
            if len(items) < 100:
                return found[0] if found else None
        raise ForgeError("forge_pagination_limit")

    def create_draft(self, branch, commit, marker, body, *, deadline):
        if (
            not branch.startswith(self.policy.branch_prefix)
            or not _branch(branch)
            or not SHA.fullmatch(commit)
        ):
            raise ForgeError("invalid_branch")
        value = self._call(
            "POST",
            "/pulls",
            payload={
                "head": branch,
                "base": self.policy.target_branch,
                "draft": True,
                "maintainer_can_modify": False,
                "title": "Managed composition " + commit[:12] + " (review pending)",
                "body": body + "\n\n" + marker,
            },
            deadline=deadline,
        )
        return self._proposal(value, branch, commit, marker)
