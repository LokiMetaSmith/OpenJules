"""Unit and integration tests for Phase 2: Agent Tools & Two-Stage Engine."""

import json
from unittest.mock import MagicMock
import pytest

from src.agent.engine import AgentEngine, PlanOutput
from src.agent.tools import AgentTools
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import ExecResult, SandboxContext, SandboxManager


def test_agent_tools_read_and_write(monkeypatch):
    mock_sandbox = MagicMock(spec=SandboxManager)
    # mock exec_in_sandbox
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout="1: line 1\n2: line 2\n",
        stderr="",
        exit_code=0,
    )

    tools = AgentTools(mock_sandbox, "test-container")
    read_res = tools.read_file("src/main.py", offset=1, limit=10)
    assert "1: line 1" in read_res

    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout="Successfully wrote 12 bytes to test.py",
        stderr="",
        exit_code=0,
    )
    write_res = tools.write_file("test.py", "hello world\n")
    assert "Successfully wrote" in write_res


def test_agent_tools_run_command():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout="all tests passed",
        stderr="",
        exit_code=0,
    )

    tools = AgentTools(mock_sandbox, "test-container")
    res = tools.run_command("pytest")
    assert "[STDOUT]\nall tests passed" in res
    assert "[EXIT CODE]: 0" in res


def test_agent_tools_list_files():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout="main.py\nREADME.md\n",
        stderr="",
        exit_code=0,
    )

    tools = AgentTools(mock_sandbox, "test-container")
    res = tools.list_files(".")
    assert "main.py" in res


def test_agent_tools_apply_patch_merge_diff():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout="Successfully applied merge diff block(s) to foo.py",
        stderr="",
        exit_code=0,
    )

    tools = AgentTools(mock_sandbox, "test-container")
    patch_str = "<<<<<<< SEARCH\nold\n=======\nnew\n>>>>>>> REPLACE"
    res = tools.apply_patch("foo.py", patch_str)
    assert "Successfully applied merge diff" in res


def test_agent_engine_step_a_planning():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()

    plan_json = json.dumps({
        "rationale": "Fix bug in calculation",
        "target_files": ["calc.py"],
        "planned_tests": ["pytest test_calc.py"],
        "action_steps": ["Inspect calc.py", "Fix division by zero"],
    })

    mock_choice = MagicMock()
    mock_choice.message.content = f"```json\n{plan_json}\n```"
    mock_llm.completion.return_value.choices = [mock_choice]

    engine = AgentEngine(mock_sandbox, llm_client=mock_llm)
    tools = AgentTools(mock_sandbox, "cont-1")

    context = SandboxContext(
        container_id="cont-1",
        workspace_dir="/tmp/ws",
        repo_url="https://github.com/org/repo.git",
        branch="main",
        config=OpenJulesConfig(test_command="pytest"),
    )

    plan = engine.step_a_planning(context, "Fix divide by zero", tools)
    assert plan.rationale == "Fix bug in calculation"
    assert plan.target_files == ["calc.py"]
    assert "Inspect calc.py" in plan.action_steps


def test_agent_engine_run_end_to_end():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()

    # Step A plan
    plan_json = json.dumps({
        "rationale": "Add helper function",
        "target_files": ["helper.py"],
        "planned_tests": ["pytest"],
        "action_steps": ["Create helper.py"],
    })

    resp_plan = MagicMock()
    resp_plan.choices = [MagicMock(message=MagicMock(content=plan_json))]

    # Step B tool call response
    tc = MagicMock()
    tc.id = "tc_1"
    tc.function.name = "write_file"
    tc.function.arguments = json.dumps({"path": "helper.py", "content": "def help(): pass"})
    resp_tool_call = MagicMock()
    resp_tool_call.choices = [
        MagicMock(message=MagicMock(content=None, tool_calls=[tc]))
    ]

    # Step B second response (completed)
    resp_finish = MagicMock()
    resp_finish.choices = [
        MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))
    ]

    mock_llm.completion.side_effect = [resp_plan, resp_tool_call, resp_finish]

    # Mock tool write_file and test verification
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout="pytest passed",
        stderr="",
        exit_code=0,
    )

    engine = AgentEngine(mock_sandbox, max_turns=5, llm_client=mock_llm)
    context = SandboxContext(
        container_id="cont-1",
        workspace_dir="/tmp/ws",
        repo_url="https://github.com/org/repo.git",
        branch="main",
        config=OpenJulesConfig(test_command="pytest"),
    )

    progress_messages = []
    result = engine.run(
        context,
        "Implement helper",
        on_progress=lambda m: progress_messages.append(m),
    )

    assert result.success is True
    assert result.verification_passed is True
    assert result.turns_taken == 2
    assert any("Step A" in m for m in progress_messages)
    assert any("Step B" in m for m in progress_messages)
    assert any("Step C" in m for m in progress_messages)
