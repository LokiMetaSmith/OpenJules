"""Root wrapper composes the unchanged agent and closes child admission."""

import io
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from src.agent import managed_root_runner as runner
from src.agent.child_tools import ControlClient, ControlError
from tests.unit.test_child_tools import CHILD, result, reply, server, spawned


def tool(name, arguments):
    return {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "fixture-call-" + name,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }


def test_actual_agent_composes_child_output_and_runs_ordinary_verification(tmp_path):
    legacy = runner._load_sibling("managed_runner")
    ordinary_names = [
        definition["function"]["name"] for definition in legacy.TOOL_DEFINITIONS
    ]
    instructions = {
        "instructions": "Determine the fixture answer.",
        "seconds": 20,
        "max_turns": 4,
        "capabilities": ["python"],
    }
    messages = iter(
        [
            tool("spawn_child", instructions),
            tool("child_status", {"child_id": CHILD}),
            tool("child_result", {"child_id": CHILD, "kind": "log"}),
            tool("write_file", {"path": "answer.py", "content": "answer = 42\n"}),
            tool("finish", {"summary": "Composed and verified child work."}),
        ]
    )
    seen_messages = []

    def complete(history):
        seen_messages.append(list(history))
        return next(messages)

    def handle(request, _):
        if request["operation"] == "spawn":
            return spawned(request)
        if request["operation"] == "status":
            return reply(request, child_id=CHILD, state="exec_returned")
        if request["operation"] == "result":
            return result(request, "answer = 42", "answer = 42")
        assert request["operation"] == "close"
        return reply(request, admission_open=False)

    with server(handle) as fixture:
        status = runner.run_root(
            SimpleNamespace(complete=complete),
            legacy.LocalTools(tmp_path),
            ControlClient(fixture.path),
            "Use a child and compose its result.",
            max_turns=5,
            verify_argv=[
                sys.executable,
                "-c",
                "from answer import answer; assert answer == 42",
            ],
            legacy=legacy,
        )
        assert status == 0
        assert (tmp_path / "answer.py").read_text() == "answer = 42\n"
        assert [request["operation"] for request in fixture.requests] == [
            "spawn",
            "status",
            "result",
            "close",
        ]
        assert any(
            '"final":false' in message["content"]
            for history in seen_messages
            for message in history
            if message["role"] == "tool"
        )
    assert [
        definition["function"]["name"] for definition in legacy.TOOL_DEFINITIONS
    ] == ordinary_names
    assert "spawn_child" not in ordinary_names


@pytest.mark.parametrize("failure", [ValueError("invalid task"), KeyboardInterrupt()])
def test_wrapper_closes_on_agent_error_and_preserves_original_failure(failure):
    operations = []

    def request(operation, **_fields):
        operations.append(operation)
        raise ControlError("uncertain")

    def fail(*_args, **_kwargs):
        raise failure

    with pytest.raises(type(failure)) as caught:
        runner.run_root(
            object(),
            object(),
            SimpleNamespace(request=request),
            "task",
            legacy=SimpleNamespace(run_agent=fail),
        )
    assert caught.value is failure
    assert operations == ["close"]


def test_success_is_not_reported_when_admission_close_is_uncertain():
    with server(lambda *_: None) as fixture:
        with pytest.raises(ControlError, match="uncertain"):
            runner.run_root(
                object(),
                object(),
                ControlClient(fixture.path),
                "task",
                legacy=SimpleNamespace(run_agent=lambda *_args, **_kwargs: 0),
            )
        assert [item["operation"] for item in fixture.requests] == ["close"]


def test_child_control_uncertainty_stops_agent_and_closes_admission():
    legacy = runner._load_sibling("managed_runner")

    def handle(request, _):
        if request["operation"] == "spawn":
            return reply(request, ok=False, error="uncertain")
        return reply(request, admission_open=False)

    model = SimpleNamespace(
        complete=lambda _history: tool(
            "spawn_child",
            {"instructions": "task", "seconds": 10, "max_turns": 2, "capabilities": []},
        )
    )
    with server(handle) as fixture:
        with pytest.raises(ControlError, match="uncertain"):
            runner.run_root(
                model,
                SimpleNamespace(call=lambda *_: "no rules"),
                ControlClient(fixture.path),
                "task",
                legacy=legacy,
            )
        assert [item["operation"] for item in fixture.requests] == ["spawn", "close"]


def test_trusted_image_import_does_not_use_the_working_repository(
    tmp_path, monkeypatch
):
    (tmp_path / "managed_runner.py").write_text(
        "raise RuntimeError('guest replacement loaded')\n"
    )
    monkeypatch.chdir(tmp_path)
    loaded = runner._load_sibling("managed_runner")
    assert (
        Path(loaded.__file__).resolve().parent == Path(runner.__file__).resolve().parent
    )
    assert callable(loaded.run_agent)
    with pytest.raises(ValueError):
        runner._load_sibling("../managed_runner")


def test_main_closes_admission_when_input_validation_fails(monkeypatch):
    operations = []
    control = SimpleNamespace(
        request=lambda operation, **fields: operations.append((operation, fields))
    )
    monkeypatch.setattr(runner, "ControlClient", lambda *_args, **_kwargs: control)
    monkeypatch.setattr(
        runner.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(b"task"))
    )
    assert (
        runner.main(
            [
                "--socket",
                "/model.sock",
                "--control-socket",
                "/control.sock",
                "--verify-json",
                "invalid-json",
            ]
        )
        == 4
    )
    assert operations == [("close", {"reason": "root runner setup failed"})]
