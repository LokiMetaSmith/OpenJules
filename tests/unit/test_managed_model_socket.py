"""Mandatory Linux Unix-socket checks; a blocked socket is a failure, not a skip."""

import json
import os
import socket
import struct

import pytest

from tests.unit.test_managed_agent import (
    ATTEMPT,
    MESSAGES,
    SCOPE_KEY,
    SPEC,
    TEST_TOKEN,
    WORKER,
    broker_client,
    message,
    provider,
    response,
)
from src.agent.managed_runner import BrokerClient, LocalTools, run_agent
from src.worker.model_broker import BrokerDenied, ModelBroker, ModelPolicy


def _read_response(connection):
    def read_exact(size):
        data = bytearray()
        while len(data) < size:
            chunk = connection.recv(size - len(data))
            assert chunk, "broker closed before its complete response"
            data.extend(chunk)
        return bytes(data)

    size = struct.unpack("!I", read_exact(4))[0]
    assert 0 < size < 2_500_000
    return json.loads(read_exact(size))


def test_real_unix_socket_roundtrip_and_payload_rejection(tmp_path, monkeypatch):
    with provider() as (endpoint, requests):
        with broker_client(tmp_path, monkeypatch, endpoint, uds=True) as (
            broker,
            client,
            _,
        ):
            broker.check_ready()
            assert (
                client.complete(MESSAGES, "first")["tool_calls"][0]["function"]["name"]
                == "finish"
            )
            assert client.complete(MESSAGES, "first")["role"] == "assistant"
            assert len(requests) == 1
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(client.socket_path)
                connection.sendall(struct.pack("!I", 50_000_000))
                result = _read_response(connection)
                assert result["error"] == "invalid_request"
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(client.socket_path)
                body = b'{"schema_version":1,"request_id":"one","request_id":"two","messages":[]}'
                connection.sendall(struct.pack("!I", len(body)) + body)
                assert _read_response(connection)["error"] == "invalid_request"
            assert len(requests) == 1


def test_worker_agent_uses_real_unix_socket_for_edit_and_verification(
    tmp_path, monkeypatch
):
    root = tmp_path / "repo"
    root.mkdir()
    (root / "answer.txt").write_text("old\n")
    messages = [
        message("write_file", path="answer.txt", content="new\n"),
        message("finish", summary="Changed answer.txt"),
    ]

    def answer(handler, requests):
        response(handler, messages[len(requests) - 1])

    with provider(answer) as (endpoint, requests):
        with broker_client(tmp_path, monkeypatch, endpoint, uds=True) as (_, client, _):
            assert (
                run_agent(client, LocalTools(root), "Change old to new.", max_turns=3)
                == 0
            )
            assert (root / "answer.txt").read_text() == "new\n"
            assert len(requests) == 2


def test_kernel_peer_uid_rejects_other_identity_before_inference(tmp_path):
    with provider() as (endpoint, requests):
        policy = ModelPolicy(endpoint, "fixture-model", "env:MOCK_MODEL_TOKEN")
        broker = ModelBroker(
            tmp_path / "private",
            {SCOPE_KEY: policy},
            lambda _: 60,
            peer_uids={SCOPE_KEY: os.getuid() + 1},
        )
        descriptor = None
        try:
            directory = broker.prepare_worker(WORKER, SPEC)
            broker.bind_attempt(WORKER, SPEC, ATTEMPT, 60)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
                connection.settimeout(2)
                connection.connect(f"/proc/self/fd/{descriptor}/model.sock")
                # No caller-provided identity or request body is trusted.
                assert _read_response(connection)["error"] == "not_authorized"
            assert not requests
            with broker.connection() as db:
                assert db.execute("SELECT COUNT(*) FROM calls").fetchone()[0] == 0
        finally:
            if descriptor is not None:
                os.close(descriptor)
            broker.close()


def test_restored_listener_rebinds_same_directory_and_preserves_budget(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MOCK_MODEL_TOKEN", TEST_TOKEN)
    with provider() as (endpoint, requests):
        policy = ModelPolicy(
            endpoint, "fixture-model", "env:MOCK_MODEL_TOKEN", max_calls=2
        )
        state = tmp_path / "private"
        broker = ModelBroker(
            state,
            {SCOPE_KEY: policy},
            lambda _: 60,
            peer_uids={SCOPE_KEY: os.getuid()},
        )
        descriptor = None
        try:
            directory = broker.prepare_worker(WORKER, SPEC)
            broker.bind_attempt(WORKER, SPEC, ATTEMPT, 60)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            client = BrokerClient(
                f"/proc/self/fd/{descriptor}/model.sock", timeout_seconds=2
            )
            first = client.complete(MESSAGES, "before-restart")
            with broker.connection() as db:
                deadline = db.execute("SELECT deadline FROM workers").fetchone()[0]
            broker.close()
            broker = ModelBroker(
                state,
                {SCOPE_KEY: policy},
                lambda _: 60,
                peer_uids={SCOPE_KEY: os.getuid()},
            )
            assert not broker.endpoints
            assert broker.restore_workers({WORKER: SPEC}) == {WORKER: directory}
            broker.check_ready()
            broker.bind_attempt(WORKER, SPEC, ATTEMPT, 120)
            # The open directory models a surviving read-only directory mount.
            assert client.complete(MESSAGES, "before-restart") == first
            assert client.complete(MESSAGES, "after-restart")["role"] == "assistant"
            assert len(requests) == 2
            with broker.connection() as db:
                assert (
                    db.execute("SELECT deadline FROM workers").fetchone()[0] == deadline
                )
            broker.revoke_attempt(WORKER, ATTEMPT)
            with pytest.raises(BrokerDenied, match="not_authorized"):
                broker.restore_workers({WORKER: SPEC})
        finally:
            if descriptor is not None:
                os.close(descriptor)
            broker.close()
