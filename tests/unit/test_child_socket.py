"""Mandatory real Unix-socket transport and kernel-peer authentication checks.

Authority/execution are named fixtures here. Distinct guest UIDs are exercised
by the real Docker qualification. Socket denial in the environment is a failure.
"""

import json
import os
import socket
import struct

import pytest

from src.agent.child_tools import ControlClient
from src.agent.managed_runner import BrokerClient
from src.worker.child_broker import ChildBroker
from src.worker.model_broker import ModelBroker, ModelPolicy
from tests.unit.test_child_supervisor import supervised  # noqa: F401
from tests.unit.test_managed_agent import MESSAGES, TEST_TOKEN, provider
from tests.unit.test_managed_model_socket import _read_response


def exchange(directory_fd, name, payload=None):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(f"/proc/self/fd/{directory_fd}/{name}")
        if payload is not None:
            encoded = json.dumps(payload).encode()
            connection.sendall(struct.pack("!I", len(encoded)) + encoded)
        return _read_response(connection)


def test_real_root_control_and_child_socket_contexts_survive_restoration(
    supervised, tmp_path, monkeypatch
):
    supervisor, root, _, _ = supervised
    key = (root.spec.deployment_id, root.spec.repository, root.spec.profile)
    monkeypatch.setenv("MOCK_MODEL_TOKEN", TEST_TOKEN)
    descriptors = []
    with provider() as (endpoint, requests):
        policies = {key: ModelPolicy(endpoint, "fixture", "env:MOCK_MODEL_TOKEN")}

        def open_broker():
            broker = ModelBroker(
                tmp_path / "model-journal",
                policies,
                lambda _: 50,
                peer_uids={key: os.getuid()},
            )
            supervisor.model_broker = broker
            supervisor.endpoints = ChildBroker(supervisor, broker)
            broker.child_supervisor = supervisor
            directory = broker.prepare_worker(root.worker_id, root.spec)
            broker.bind_attempt(
                root.worker_id,
                root.spec,
                root.attempt_id,
                40,
                deadline_monotonic=root.monotonic_deadline,
            )
            supervisor.endpoints.prepare_root(root.worker_id, root.spec, directory)
            return broker, directory

        broker, directory = open_broker()
        try:
            root_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            descriptors.append(root_fd)
            control = ControlClient(
                f"/proc/self/fd/{root_fd}/control.sock", timeout_seconds=5
            )
            payload = {
                "instructions": "Bounded child",
                "seconds": 10,
                "max_turns": 2,
                "capabilities": ["python"],
            }
            first = control.request("spawn", request_id="stable-spawn", **payload)
            child_id = first["child_id"]
            child = supervisor.journal.child(root.worker_id, child_id)
            assert child["uid"] != os.getuid()
            child_fd = os.open(
                directory / "children" / child_id, os.O_RDONLY | os.O_DIRECTORY
            )
            descriptors.append(child_fd)
            # No JSON actor label can give the root process a child's identity.
            assert exchange(child_fd, "model.sock")["error"] == "not_authorized"
            assert not requests
            model = BrokerClient(f"/proc/self/fd/{root_fd}/model.sock")
            first_model = model.complete(MESSAGES, "root-inference")
            assert len(requests) == 1
            for changes in (
                {"operation": []},
                {"operation": {}},
                {"operation": "spawn", **payload, "actor": child_id},
                {"operation": "status", "child_id": []},
            ):
                refused = exchange(
                    root_fd,
                    "control.sock",
                    {"schema_version": 1, "request_id": "invalid-fields", **changes},
                )
                assert refused["ok"] is False and refused["error"] == "invalid_request"
            completed = []
            supervisor.endpoints.close(lambda: completed.append(True))
            broker.close()
            assert completed == [True]
            broker, restored = open_broker()
            assert restored == directory
            supervisor.endpoints.restore(root.worker_id)
            retry = control.request("spawn", request_id="stable-spawn", **payload)
            assert (
                retry["child_id"] == child_id and len(supervisor.driver.launches) == 1
            )
            assert (
                model.complete(MESSAGES, "root-inference") == first_model
                and len(requests) == 1
            )
            assert exchange(child_fd, "model.sock")["error"] == "not_authorized"
            assert (
                supervisor.journal.child(root.worker_id, child_id)["exec_id"]
                == child["exec_id"]
            )
        finally:
            supervisor.close()
            broker.close()
            for descriptor in descriptors:
                os.close(descriptor)


def test_kernel_peer_denial_precedes_root_control_payload(supervised, tmp_path):
    supervisor, root, _, _ = supervised
    key = (root.spec.deployment_id, root.spec.repository, root.spec.profile)
    with provider() as (endpoint, requests):
        broker = ModelBroker(
            tmp_path / "private-models",
            {key: ModelPolicy(endpoint, "fixture", "env:MOCK_MODEL_TOKEN")},
            lambda _: 50,
            peer_uids={key: os.getuid() + 1},
        )
        descriptor = None
        try:
            supervisor.endpoints = ChildBroker(supervisor, broker)
            directory = broker.prepare_worker(root.worker_id, root.spec)
            supervisor.endpoints.prepare_root(root.worker_id, root.spec, directory)
            descriptor = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
            assert exchange(descriptor, "control.sock")["error"] == "not_authorized"
            assert not supervisor.authority.events and not supervisor.journal.intents(
                root.worker_id
            )
            assert not requests
        finally:
            supervisor.close()
            broker.close()
            if descriptor is not None:
                os.close(descriptor)
