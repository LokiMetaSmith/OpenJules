"""Real Docker child outcomes, kernel identity, capture and service restoration.

The canonical authority and model provider are deterministic protocol fixtures.
The installed three-repository trial separately qualifies actual authority HTTP.
"""

import json
import os
import time

import pytest

from tests.integration.test_managed_agent_container import agent_image  # noqa: F401
from tests.integration.test_managed_children_runtime import (
    child_stack,
    reopen_stack,
    wait_complete,
)
from tests.unit.test_managed_agent import message, provider, response

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        os.environ.get("OPENJULES_MANAGED_DOCKER_TESTS") != "1",
        reason="real managed Docker qualification requires explicit opt-in",
    ),
]

RPC = """import json,socket,struct,sys
path,payload=json.loads(sys.argv[1])
def read(s,n):
 data=b''
 while len(data)<n:
  part=s.recv(n-len(data))
  assert part
  data+=part
 return data
with socket.socket(socket.AF_UNIX,socket.SOCK_STREAM) as s:
 s.settimeout(5);s.connect(path)
 if payload is not None:
  data=json.dumps(payload).encode();s.sendall(struct.pack('!I',len(data))+data)
 size=struct.unpack('!I',read(s,4))[0]
 assert 0<size<=65536
 print(read(s,size).decode())
"""


def exchange(container, uid, path, payload=None):
    result = container.exec_run(
        ["python3", "-I", "-S", "-c", RPC, json.dumps([path, payload])],
        user=f"{uid}:{uid}",
        workdir="/",
        privileged=False,
    )
    assert result.exit_code == 0, result.output.decode(errors="replace")
    return json.loads(result.output)


def test_real_reports_retain_dissent_survive_reconnect_and_cannot_impersonate_siblings(
    tmp_path,
    monkeypatch,
    agent_image,
):
    turns = {"left": 0, "right": 0}

    def deterministic(handler, requests):
        initial = requests[-1]["body"]["messages"][1]["content"]
        name = "left" if initial.startswith("CHILD_left") else "right"
        assert initial.startswith("CHILD_" + name)
        names = {tool["function"]["name"] for tool in requests[-1]["body"]["tools"]}
        assert "report_outcome" in names and "spawn_child" not in names
        turns[name] += 1
        if name == "right":
            assert turns[name] == 1
            answer = message(
                "report_outcome", outcome="declined", summary="Private right refusal λ"
            )
        elif turns[name] == 1:
            answer = message(
                "write_file", path="left.txt", content="left implemented\n"
            )
        else:
            assert turns[name] == 2
            answer = message("finish", summary="Private left completion")
        response(handler, answer)

    with provider(deterministic) as (endpoint, requests):
        with child_stack(
            tmp_path,
            monkeypatch,
            agent_image,
            endpoint,
            sleeping_root=True,
            seconds=60,
            child_seconds=40,
            outcome_protocol="initial-v1",
        ) as stack:
            stack.dispatcher.tick_worker(stack.worker)
            container = stack.client.containers.get(
                stack.runtime._name(stack.worker_id)
            )
            control = "/run/openjules-model/control.sock"
            child_ids = []
            for name in ("left", "right"):
                result = exchange(
                    container,
                    stack.profile.uid,
                    control,
                    {
                        "schema_version": 1,
                        "request_id": "spawn-" + name,
                        "operation": "spawn",
                        "instructions": "CHILD_"
                        + name
                        + ": change only "
                        + name
                        + ".txt",
                        "seconds": 35,
                        "max_turns": 3,
                        "capabilities": ["python"],
                    },
                )
                assert result["ok"] is True
                child_ids.append(result["child_id"])
            root = stack.children.journal.context(stack.worker_id)
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                children = [
                    stack.children.driver.observe(
                        root, stack.children.journal.child(stack.worker_id, child_id)
                    )
                    for child_id in child_ids
                ]
                if all(child["phase"] == "exec_returned" for child in children):
                    break
                time.sleep(0.05)
            assert all(child["phase"] == "exec_returned" for child in children)
            assert all(child["capture"] is None for child in children)
            assert set(
                stack.lifecycle.observe_worker(stack.spec.scope, stack.worker_id)[
                    "active_executions"
                ]
            ) == {root.attempt_id, *child_ids}
            reports = []
            for child in children:
                status = exchange(
                    container,
                    stack.profile.uid,
                    control,
                    {
                        "schema_version": 1,
                        "request_id": "status-" + child["child_id"],
                        "operation": "status",
                        "child_id": child["child_id"],
                    },
                )
                assert status["ok"] is True and status["final"] is False
                assert status["initial_outcome"]["canonical_committed"] is False
                assert status["initial_outcome"]["release_authorized"] is False
                reports.append(status["initial_outcome"])
            assert [outcome["report"]["outcome"] for outcome in reports] == [
                "completed",
                "declined",
            ]
            for index, child in enumerate(children):
                path = (
                    "/run/openjules-model/children/"
                    + child["child_id"]
                    + "/outcome.sock"
                )
                retained = reports[index]["report"]
                retry = {
                    "schema_version": 1,
                    "request_id": retained["request_key"],
                    "outcome": retained["outcome"],
                    "summary": retained["summary"],
                }
                assert (
                    exchange(container, child["uid"], path, retry)["report"] == retained
                )
                assert (
                    exchange(container, children[1 - index]["uid"], path)["error"]
                    == "not_authorized"
                )
                assert (
                    exchange(container, stack.profile.uid, path)["error"]
                    == "not_authorized"
                )
            assert len(requests) == len(stack.child_authority.turns) == 3
            with stack.broker.connection() as db:
                assert tuple(
                    db.execute(
                        "SELECT COUNT(*),SUM(reserved_tokens) FROM calls"
                    ).fetchone()
                ) == (3, 6144)
            # Recreate the actual host services while the existing container and
            # original child identities remain live. No child exec is repeated.
            original_execs = [child["exec_id"] for child in children]
            stack.dispatcher.stop()
            stack.children.close()
            stack.broker.close()
            reopen_stack(stack, tmp_path)
            for index, child_id in enumerate(child_ids):
                status = exchange(
                    container,
                    stack.profile.uid,
                    control,
                    {
                        "schema_version": 1,
                        "request_id": "reconnect-" + child_id,
                        "operation": "status",
                        "child_id": child_id,
                    },
                )
                assert status["initial_outcome"] == reports[index]
                assert (
                    stack.children.journal.child(stack.worker_id, child_id)["exec_id"]
                    == original_execs[index]
                )
            cancelled = exchange(
                container,
                stack.profile.uid,
                control,
                {
                    "schema_version": 1,
                    "request_id": "capture-outcomes",
                    "operation": "cancel",
                    "child_id": child_ids[0],
                    "reason": "Outcome probes complete; retain final capture",
                },
            )
            assert cancelled["ok"] is True
            wait_complete(stack)
            for index, child_id in enumerate(child_ids):
                child = stack.children.journal.child(stack.worker_id, child_id)
                assert (
                    child["phase"] == "completed"
                    and child["exec_id"] == original_execs[index]
                )
                contents = json.loads(child["capture"])["contents"]
                extension = json.loads(contents["manifest"])["work_outcome"]
                assert extension["report"] == reports[index]["report"]
                if index == 0:
                    assert "+left implemented" in contents["patch"]
                    assert "CHILD_IDENTITY_VERIFIED" in contents["log"]
                else:
                    assert contents["patch"] == ""
                    assert "CHILD_IDENTITY_VERIFIED" not in contents["log"]
            assert len(requests) == 3
            assert not stack.lifecycle.observe_worker(
                stack.spec.scope, stack.worker_id
            )["active_executions"]
