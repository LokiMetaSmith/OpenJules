"""Use real HTTP to test credential, origin, deadline and response boundaries."""

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
import time

import pytest

from src.polyphony.managed_client import (
    AuthorityRejected,
    AuthorityUnavailable,
    ManagedAuthorityClient,
)
from src.worker.lifecycle import WorkerScope

SCOPE = WorkerScope("test", "github:github.com/example/repo")
BASE = "a" * 40


@contextmanager
def server(callback):
    seen = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            seen.append((self.path, self.headers.get("Authorization")))
            callback(self)

        def log_message(self, *_):
            pass

    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{http.server_port}", seen
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)


def json_reply(handler, payload):
    body = json.dumps(payload).encode()
    handler.send_response(200)
    handler.send_header("Content-Type", "application/json")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def capability(**changes):
    return {
        "schema_version": 1,
        "deployment_id": SCOPE.deployment_id,
        "repository": SCOPE.repository,
        "service": "keystone-polyphony",
        "protocol": "managed-runtime-v1",
        "profile": "coding",
        "base": BASE,
        "managed_dispatch": True,
        "external_root": True,
        "profile_supported": True,
        "children": False,
    } | changes


def test_real_http_capability_is_scoped_and_authenticated(monkeypatch):
    token = "local-test-credential-" * 3
    monkeypatch.setenv("TEST_RUNTIME_TOKEN", token)
    with server(lambda h: json_reply(h, capability())) as (url, seen):
        client = ManagedAuthorityClient(url, "env:TEST_RUNTIME_TOKEN")
        assert client.capabilities(SCOPE, "coding", BASE)["children"] is False
    assert seen[0][1] == "Bearer " + token
    assert "profile=coding" in seen[0][0]
    assert token not in seen[0][0]


@pytest.mark.parametrize(
    "change",
    [
        {"repository": "github:github.com/other/repo"},
        {"base": "b" * 40},
        {"schema_version": True},
        {"managed_dispatch": False},
        {"external_root": False},
    ],
)
def test_capability_mismatch_cannot_become_ready(monkeypatch, change):
    monkeypatch.setenv("TEST_RUNTIME_TOKEN", "x" * 40)
    with server(lambda h: json_reply(h, capability(**change))) as (url, _):
        with pytest.raises(AuthorityUnavailable):
            ManagedAuthorityClient(url, "env:TEST_RUNTIME_TOKEN").capabilities(
                SCOPE, "coding", BASE
            )


def test_redirect_does_not_forward_authority_credential(monkeypatch):
    monkeypatch.setenv("TEST_RUNTIME_TOKEN", "x" * 40)
    with server(lambda h: json_reply(h, capability())) as (destination, seen):

        def redirect(handler):
            handler.send_response(307)
            handler.send_header("Location", destination + "/stolen")
            handler.end_headers()

        with server(redirect) as (source, _):
            with pytest.raises(AuthorityRejected) as error:
                ManagedAuthorityClient(source, "env:TEST_RUNTIME_TOKEN").capabilities(
                    SCOPE, "coding", BASE
                )
            assert error.value.status == 307
        assert not seen


def test_whole_request_deadline_stops_trickled_response(monkeypatch):
    monkeypatch.setenv("TEST_RUNTIME_TOKEN", "x" * 40)

    def trickle(handler):
        handler.send_response(200)
        handler.send_header("Content-Length", "10000")
        handler.end_headers()
        try:
            for _ in range(100):
                handler.wfile.write(b" ")
                handler.wfile.flush()
                time.sleep(0.03)
        except (BrokenPipeError, ConnectionResetError):
            pass

    with server(trickle) as (url, _):
        started = time.monotonic()
        with pytest.raises(AuthorityUnavailable):
            ManagedAuthorityClient(
                url, "env:TEST_RUNTIME_TOKEN", timeout_seconds=0.15
            ).snapshot(SCOPE)
        assert time.monotonic() - started < 1


def test_oversized_reply_is_rejected(monkeypatch):
    monkeypatch.setenv("TEST_RUNTIME_TOKEN", "x" * 40)
    with server(lambda h: json_reply(h, {"unexpected": "x" * 10000})) as (url, _):
        with pytest.raises(AuthorityUnavailable):
            ManagedAuthorityClient(
                url, "env:TEST_RUNTIME_TOKEN", max_response_bytes=1024
            ).snapshot(SCOPE)


@pytest.mark.parametrize(
    "url",
    [
        "http://public.example",
        "https://user:password@example.com",
        "https://example.com/?token=secret",
        "file:///tmp/api",
    ],
)
def test_invalid_service_destinations_rejected(url):
    with pytest.raises(ValueError):
        ManagedAuthorityClient(url, "env:TEST_RUNTIME_TOKEN")


def test_remote_error_body_is_not_copied(monkeypatch):
    monkeypatch.setenv("TEST_RUNTIME_TOKEN", "x" * 40)

    def reject(handler):
        handler.send_response(403)
        handler.end_headers()
        handler.wfile.write(b"untrusted-credential-shaped-content")

    with server(reject) as (url, _):
        with pytest.raises(AuthorityRejected) as error:
            ManagedAuthorityClient(url, "env:TEST_RUNTIME_TOKEN").snapshot(SCOPE)
        assert "untrusted" not in str(error.value)
