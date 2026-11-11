"""Actual socket framing and immutable guest control requests."""

from contextlib import contextmanager
import hashlib
import json
from pathlib import Path
import socket
import struct
import tempfile
import threading
import time
from types import SimpleNamespace

import pytest

from src.agent.child_tools import (
    CHILD_TOOL_DEFINITIONS,
    ControlClient,
    ControlError,
    MAX_RESPONSE_BYTES,
    RootTools,
)

CHILD = "child_" + "a" * 32
SPAWN = {
    "instructions": "Inspect and improve the approved source.",
    "seconds": 20,
    "max_turns": 4,
    "capabilities": ["python"],
}


def _read(connection, size):
    content = bytearray()
    while len(content) < size:
        part = connection.recv(size - len(content))
        if not part:
            raise AssertionError("fixture received an incomplete frame")
        content.extend(part)
    return bytes(content)


def reply(request, **fields):
    return {
        "schema_version": 1,
        "request_id": request["request_id"],
        "ok": True,
        **fields,
    }


def spawned(request):
    return reply(
        request,
        child_id=CHILD,
        state="running",
        deadline=time.time() + 20,
        capabilities=request["capabilities"],
    )


def result(request, content="a", artifact="a"):
    return reply(
        request,
        child_id=request["child_id"],
        kind=request["kind"],
        final=False,
        offset=request["offset"],
        content=content,
        content_sha256=hashlib.sha256(content.encode()).hexdigest(),
        artifact_sha256=hashlib.sha256(artifact.encode()).hexdigest(),
        total_bytes=len(artifact.encode()),
    )


@contextmanager
def server(callback):
    # Short private paths avoid Linux's Unix sockaddr pathname limit. A blocked
    # Unix transport is a test failure, not a skipped security qualification.
    with tempfile.TemporaryDirectory(prefix="oj-child-") as directory:
        path = str(Path(directory) / "control.sock")
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listener.bind(path)
        listener.listen(8)
        listener.settimeout(0.05)
        stopped = threading.Event()
        requests, raw_requests, errors = [], [], []

        def run():
            while not stopped.is_set():
                try:
                    connection, _ = listener.accept()
                except socket.timeout:
                    continue
                except OSError:
                    break
                try:
                    with connection:
                        connection.settimeout(2)
                        length = struct.unpack("!I", _read(connection, 4))[0]
                        raw = _read(connection, length)
                        request = json.loads(raw)
                        raw_requests.append(raw)
                        requests.append(request)
                        response = callback(request, len(requests))
                        if response is None:
                            continue
                        if isinstance(response, bytes):
                            connection.sendall(response)
                        else:
                            encoded = json.dumps(response).encode()
                            connection.sendall(
                                struct.pack("!I", len(encoded)) + encoded
                            )
                except (BrokenPipeError, ConnectionResetError):
                    pass
                except BaseException as exc:
                    errors.append(exc)

        thread = threading.Thread(target=run, daemon=True)
        thread.start()
        try:
            yield SimpleNamespace(
                path=path, requests=requests, raw_requests=raw_requests
            )
        finally:
            stopped.set()
            listener.close()
            thread.join(3)
            assert not thread.is_alive()
            assert not errors


def test_retryable_responses_keep_one_key_body_and_original_input():
    fields = {**SPAWN, "capabilities": ["python"]}

    def handle(request, number):
        if number < 3:
            fields["capabilities"].append("must-not-enter-a-retry")
            return reply(
                request,
                ok=False,
                error="retry_later" if number == 1 else "canonical_pending",
            )
        return spawned(request)

    with server(handle) as fixture:
        client = ControlClient(fixture.path, retry_delay_seconds=0.005)
        answer = client.request("spawn", request_id="stable-request", **fields)
        assert answer["child_id"] == CHILD
        assert len(fixture.requests) == 3
        assert len(set(fixture.raw_requests)) == 1
        assert fixture.requests[0]["capabilities"] == ["python"]


def test_repeated_explicit_identity_cannot_change_payload_or_restart_deadline():
    with server(
        lambda request, _: reply(request, ok=False, error="retry_later")
    ) as fixture:
        client = ControlClient(
            fixture.path, timeout_seconds=0.08, retry_delay_seconds=0.01
        )
        began = time.monotonic()
        with pytest.raises(ControlError, match="deadline_exceeded"):
            client.request("spawn", request_id="one-request", **SPAWN)
        assert time.monotonic() - began < 0.3
        count = len(fixture.requests)
        with pytest.raises(ControlError, match="deadline_exceeded"):
            client.request("spawn", request_id="one-request", **SPAWN)
        with pytest.raises(ControlError, match="conflict"):
            client.request(
                "spawn", request_id="one-request", **(SPAWN | {"seconds": 21})
            )
        assert len(fixture.requests) == count


@pytest.mark.parametrize("code", ["uncertain", "conflict", "not_authorized"])
def test_nonretryable_refusal_is_not_repeated(code):
    with server(lambda request, _: reply(request, ok=False, error=code)) as fixture:
        with pytest.raises(ControlError, match=code):
            ControlClient(fixture.path).request("spawn", **SPAWN)
        assert len(fixture.requests) == 1


def test_connection_loss_is_uncertain_and_never_repeated():
    with server(lambda *_: None) as fixture:
        with pytest.raises(ControlError, match="uncertain") as caught:
            ControlClient(fixture.path).request(
                "spawn", request_id="lost-reply", **SPAWN
            )
        assert caught.value.request_id == "lost-reply"
        assert len(fixture.requests) == 1


@pytest.mark.parametrize(
    "changed",
    [
        {"schema_version": True},
        {"request_id": "another-request"},
        {"ok": 1},
        {"child_id": "sibling-child"},
    ],
)
def test_response_identity_and_schema_are_checked(changed):
    with server(
        lambda request, _: reply(request, child_id=CHILD, state="running") | changed
    ) as fixture:
        with pytest.raises(ControlError, match="invalid_response"):
            ControlClient(fixture.path).request("status", child_id=CHILD)


def test_oversized_frame_is_rejected_before_reading_its_body():
    with server(lambda *_: struct.pack("!I", MAX_RESPONSE_BYTES + 1)) as fixture:
        with pytest.raises(ControlError, match="invalid_response"):
            ControlClient(fixture.path).request("status", child_id=CHILD)


def test_duplicate_json_fields_are_not_accepted_as_a_receipt():
    def handle(request, _):
        raw = (
            '{"schema_version":1,"request_id":'
            + json.dumps(request["request_id"])
            + ',"ok":false,"ok":true,"child_id":'
            + json.dumps(CHILD)
            + ',"state":"running"}'
        ).encode()
        return struct.pack("!I", len(raw)) + raw

    with server(handle) as fixture:
        with pytest.raises(ControlError):
            ControlClient(fixture.path).request("status", child_id=CHILD)
        assert len(fixture.requests) == 1


def test_utf8_result_pages_verify_chunk_and_stable_artifact_hashes():
    def handle(request, _):
        return result(request, "λ" if request["offset"] == 0 else "X", "λX")

    with server(handle) as fixture:
        client = ControlClient(fixture.path)
        first = client.request(
            "result", child_id=CHILD, kind="patch", offset=0, limit=2
        )
        second = client.request(
            "result", child_id=CHILD, kind="patch", offset=2, limit=1
        )
        assert first["content"] + second["content"] == "λX"
        assert first["artifact_sha256"] == second["artifact_sha256"]
        assert first["final"] is False


@pytest.mark.parametrize(
    "changed",
    [
        {"final": True},
        {"final": 0},
        {"offset": True},
        {"offset": 1},
        {"kind": "log"},
        {"total_bytes": True},
        {"total_bytes": 4_000_001},
        {"content_sha256": "0" * 64},
        {"artifact_sha256": "0" * 64},
        {"content": "λ"},
    ],
)
def test_result_hash_bounds_and_nonterminal_status_are_required(changed):
    with server(lambda request, _: result(request) | changed) as fixture:
        with pytest.raises(ControlError, match="invalid_response"):
            ControlClient(fixture.path).request(
                "result", child_id=CHILD, kind="patch", offset=0, limit=1
            )


def test_paged_reads_cannot_mix_two_artifact_snapshots():
    with server(
        lambda request, number: result(
            request, "a" if number == 1 else "b", "ab" if number == 1 else "xb"
        )
    ) as fixture:
        client = ControlClient(fixture.path)
        client.request("result", child_id=CHILD, kind="log", offset=0, limit=1)
        with pytest.raises(ControlError, match="conflict"):
            client.request("result", child_id=CHILD, kind="log", offset=1, limit=1)


def test_tool_extension_cannot_supply_actor_or_execution_policy():
    ordinary_calls = []
    ordinary = SimpleNamespace(
        call=lambda name, arguments: ordinary_calls.append((name, arguments))
        or "ordinary",
        run_command=lambda argv: json.dumps({"argv": argv}),
    )
    with server(lambda request, _: spawned(request)) as fixture:
        tools = RootTools(ordinary, ControlClient(fixture.path))
        assert tools.call("read_file", {"path": "README.md"}) == "ordinary"
        assert ordinary_calls == [("read_file", {"path": "README.md"})]
        assert json.loads(tools.run_command(["python3", "test.py"]))["argv"] == [
            "python3",
            "test.py",
        ]
        for field in (
            "actor",
            "child_id",
            "role",
            "command",
            "provider",
            "uid",
            "image",
            "request_id",
        ):
            assert tools.call("spawn_child", SPAWN | {field: "forged"}).startswith(
                "Tool error:"
            )
        assert tools.call("self_grant", {}).startswith("Tool error:")
        assert not fixture.requests
    assert {item["function"]["name"] for item in CHILD_TOOL_DEFINITIONS} == {
        "spawn_child",
        "child_status",
        "child_result",
        "cancel_child",
        "close_children",
    }


def test_tool_no_allocation_is_recoverable_but_unknown_outcome_is_fatal():
    ordinary = SimpleNamespace(call=lambda *_: "ordinary")
    with server(
        lambda request, number: reply(
            request,
            ok=False,
            error="child_not_allocated" if number == 1 else "uncertain",
        )
    ) as fixture:
        tools = RootTools(ordinary, ControlClient(fixture.path))
        assert (
            json.loads(tools.call("spawn_child", SPAWN))["error"]
            == "child_not_allocated"
        )
        with pytest.raises(ControlError, match="uncertain"):
            tools.call("spawn_child", SPAWN)


def test_explicit_close_prevents_further_local_spawn_and_duplicate_close():
    ordinary = SimpleNamespace(call=lambda *_: "ordinary")
    with server(lambda request, _: reply(request, admission_open=False)) as fixture:
        tools = RootTools(ordinary, ControlClient(fixture.path))
        tools.call("close_children", {"reason": "composition is complete"})
        assert tools.call("spawn_child", SPAWN).startswith("Tool error:")
        tools.close()
        assert len(fixture.requests) == 1


@pytest.mark.parametrize("request_id", ["", False, "/child/forged", "x" * 129])
def test_invalid_public_request_identity_never_connects(request_id):
    with server(lambda request, _: spawned(request)) as fixture:
        with pytest.raises(ValueError):
            ControlClient(fixture.path).request("spawn", request_id=request_id, **SPAWN)
        assert not fixture.requests
