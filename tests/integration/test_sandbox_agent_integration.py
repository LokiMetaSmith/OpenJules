"""Integration tests for SandboxManager, AgentTools, and AgentEngine interactions."""

import json
from unittest.mock import MagicMock
import pytest

from src.agent.engine import AgentEngine
from src.agent.tools import AgentTools
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import ExecResult, SandboxContext, SandboxManager

pytestmark = pytest.mark.integration


def test_sandbox_agent_engine_multi_turn_execution(tmp_path):
    ws_dir = str(tmp_path)
    mock_sandbox = MagicMock(spec=SandboxManager)

    # Simulate container execs: file listing, file reading, writing file, verification
    def fake_exec(cid, cmd, timeout=120, workdir="/workspace", env=None):
        if "list_files" in cmd or "os.walk" in cmd:
            return ExecResult(stdout="main.py\ncalc.py\n", stderr="", exit_code=0)
        elif "read_file" in cmd or "open('README.md'" in cmd:
            return ExecResult(stdout="1: # Calculator Project", stderr="", exit_code=0)
        elif "pytest" in cmd:
            return ExecResult(stdout="2 passed in 0.12s", stderr="", exit_code=0)
        else:
            return ExecResult(stdout="Successfully wrote 45 bytes to calc.py", stderr="", exit_code=0)

    mock_sandbox.exec_in_sandbox.side_effect = fake_exec

    # Mock LLM sequence
    mock_llm = MagicMock()
    plan_json = json.dumps({
        "rationale": "Fix divide by zero check",
        "target_files": ["calc.py"],
        "planned_tests": ["pytest"],
        "action_steps": ["Inspect calc.py", "Add zero guard"],
    })

    # Turn 1: Planning
    resp_plan = MagicMock(choices=[MagicMock(message=MagicMock(content=plan_json))])

    # Turn 2: Agent issues tool call to edit calc.py
    tc = MagicMock()
    tc.id = "tc_edit"
    tc.function.name = "write_file"
    tc.function.arguments = json.dumps({
        "path": "calc.py",
        "content": "def divide(a, b):\n    if b == 0: return 0\n    return a / b\n",
    })
    resp_tool = MagicMock(choices=[MagicMock(message=MagicMock(content=None, tool_calls=[tc]))])

    # Turn 3: Agent finishes action loop
    resp_done = MagicMock(choices=[MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))])

    # Outer loop experience distillation
    distill_content = json.dumps({
        "problem_summary": "Zero division error",
        "solution_pattern": "Guarded divisor check",
        "discovered_rules": ["Always validate divisor != 0"],
    })
    resp_distill = MagicMock(
        choices=[MagicMock(message=MagicMock(content=distill_content))]
    )

    mock_llm.completion.side_effect = [resp_plan, resp_tool, resp_done, resp_distill]

    engine = AgentEngine(mock_sandbox, llm_client=mock_llm)
    context = SandboxContext(
        container_id="cont-integration-1",
        workspace_dir=ws_dir,
        repo_url="https://github.com/org/repo.git",
        branch="main",
        config=OpenJulesConfig(test_command="pytest"),
    )

    result = engine.run(context, "Handle division by zero in calc.py")

    assert result.success is True
    assert result.verification_passed is True
    assert result.turns_taken == 2
    assert result.plan.target_files == ["calc.py"]
    assert mock_sandbox.exec_in_sandbox.called
