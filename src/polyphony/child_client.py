"""Bounded authenticated child authority transport, separate from guest sockets."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re

import httpx

from .managed_client import (
    AuthorityRejected,
    AuthorityUnavailable,
    ManagedAuthorityClient,
    token_from_reference,
)
from src.worker.lifecycle import WorkerScope

_CHILD = re.compile(r"child_[0-9a-f]{32}")
_HASH = re.compile(r"[0-9a-f]{64}")
_MUTATIONS = {"grant", "permission", "turn", "capture", "observe", "close", "revoke"}


class ChildRejected(AuthorityRejected):
    def __init__(self, status: int, *, child_not_allocated=False):
        self.child_not_allocated = child_not_allocated
        super().__init__(status)


class ChildAuthorityClient:
    """Use the root client's exact endpoint, credentials and transport bounds.

    A generic rejection is never evidence that a prior grant was not allocated.
    Responses and HTTP error bodies must not be copied into service logs.
    """

    def __init__(self, authority: ManagedAuthorityClient):
        self.authority = authority

    async def _request(self, method: str, operation: str, data: dict, text=False):
        client_config = self.authority
        try:
            token = token_from_reference(client_config.token_ref)
            async with asyncio.timeout(client_config.timeout_seconds):
                async with httpx.AsyncClient(
                    trust_env=False,
                    follow_redirects=False,
                    timeout=client_config.timeout_seconds,
                    headers={
                        "Authorization": "Bearer " + token,
                        "Accept-Encoding": "identity",
                    },
                ) as client:
                    arguments = {"params": data} if method == "GET" else {"json": data}
                    async with client.stream(
                        method,
                        f"{client_config.endpoint}/managed/{operation}",
                        **arguments,
                    ) as response:
                        if response.status_code != 200:
                            unallocated = False
                            if (
                                operation == "children/grant"
                                and response.status_code == 409
                            ):
                                raw = bytearray()
                                async for part in response.aiter_raw():
                                    raw.extend(part)
                                    if len(raw) > 4096:
                                        break
                                if len(raw) <= 4096:
                                    try:
                                        error = json.loads(raw)
                                        unallocated = (
                                            isinstance(error, dict)
                                            and error.get("code")
                                            == "child_not_allocated"
                                        )
                                    except ValueError:
                                        pass
                            raise ChildRejected(
                                response.status_code, child_not_allocated=unallocated
                            )
                        if response.headers.get("Content-Encoding", "identity") not in {
                            "",
                            "identity",
                        }:
                            raise AuthorityUnavailable(
                                "Unexpected canonical child response encoding"
                            )
                        content = bytearray()
                        async for part in response.aiter_raw():
                            content.extend(part)
                            if len(content) > client_config.max_response_bytes:
                                raise AuthorityUnavailable(
                                    "Canonical child response exceeds its bound"
                                )
            if text:
                return bytes(content).decode("utf-8")
            result = json.loads(content)
            if not isinstance(result, dict):
                raise AuthorityUnavailable("Canonical child response is not an object")
            return result
        except AuthorityUnavailable:
            raise
        except (OSError, ValueError, httpx.HTTPError, TimeoutError):
            raise AuthorityUnavailable(
                "Canonical child request did not complete"
            ) from None

    def call(
        self, operation: str, scope: WorkerScope, attempt_id: str, **payload
    ) -> dict:
        if (
            not isinstance(operation, str)
            or operation not in _MUTATIONS
            or set(payload)
            & {"schema_version", "deployment_id", "repository", "attempt_id"}
        ):
            raise ValueError("unsupported canonical child operation")
        result = asyncio.run(
            self._request(
                "POST",
                "children/" + operation,
                self.authority.scope_payload(scope)
                | {"attempt_id": attempt_id}
                | payload,
            )
        )
        self.authority.verify_scope(result, scope)
        if operation in {"close", "revoke"}:
            if (
                result.get("action") != operation
                or result.get("attempt", {}).get("id") != attempt_id
            ):
                raise AuthorityUnavailable(
                    "Canonical child control response changed its root"
                )
            if (
                operation == "close"
                and result["attempt"].get("child_admission_closed") is not True
            ):
                raise AuthorityUnavailable(
                    "Canonical child admission closure is not confirmed"
                )
        elif result.get("root_attempt_id") != attempt_id:
            raise AuthorityUnavailable("Canonical child response changed its root")
        return result

    def children(self, scope: WorkerScope, attempt_id: str) -> dict:
        result = asyncio.run(
            self._request(
                "GET",
                "children",
                {
                    "deployment_id": scope.deployment_id,
                    "repository": scope.repository,
                    "attempt_id": attempt_id,
                },
            )
        )
        self.authority.verify_scope(result, scope)
        if result.get("root_attempt_id") != attempt_id or not isinstance(
            result.get("children"), list
        ):
            raise AuthorityUnavailable(
                "Canonical child recovery response changed its root"
            )
        return result

    def artifact(
        self, scope: WorkerScope, attempt_id: str, child_id: str, artifact_id: str
    ) -> str:
        if (
            not isinstance(child_id, str)
            or not _CHILD.fullmatch(child_id)
            or not isinstance(artifact_id, str)
            or not _HASH.fullmatch(artifact_id)
        ):
            raise ValueError("invalid canonical child artifact identity")
        content = asyncio.run(
            self._request(
                "GET",
                f"children/{child_id}/artifacts/{artifact_id}",
                {
                    "deployment_id": scope.deployment_id,
                    "repository": scope.repository,
                    "attempt_id": attempt_id,
                },
                text=True,
            )
        )
        if hashlib.sha256(content.encode()).hexdigest() != artifact_id:
            raise AuthorityUnavailable(
                "Canonical child artifact content does not match its hash"
            )
        return content
