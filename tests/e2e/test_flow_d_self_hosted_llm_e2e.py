"""Flow D E2E Test: Self-Hosted LLM (vLLM / llama.cpp) Fallback Tool Execution."""

import json
from unittest.mock import MagicMock
import pytest

from src.agent.engine import AgentEngine, PlanOutput
from src.agent.llm_config import LLMConfig
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import ExecResult, SandboxContext, SandboxManager

pytestmark = pytest.mark.e2e


def test_flow_d_self_hosted_llm_e2e_fallback():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()

    # Step A plan
    plan_json = json.dumps({
        "rationale": "Offline inference fix",
        "target_files": ["server.py"],
        "planned_tests": ["pytest"],
        "action_steps": ["Add healthcheck endpoint"],
    })
    resp_plan = MagicMock(choices=[MagicMock(message=MagicMock(content=plan_json))])

    # Step B Turn 1: Local llama.cpp/vLLM server rejects tools parameter
    # Step B Turn 2: Model outputs tool call formatted inside JSON markdown codeblock
    json_action = '```json\n{"tool": "write_file", "arguments": {"path": "server.py", "content": "@app.get(\'/health\')\ndef h(): return 200"}}\n```'
    resp_text_tool = MagicMock(choices=[MagicMock(message=MagicMock(content=json_action, tool_calls=None))])

    # Step B Turn 3: Model finishes task
    resp_finish = MagicMock(choices=[MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))])

    # Verification step distillation
    resp_distill = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"problem_summary":"healthcheck","solution_pattern":"endpoint","discovered_rules":[]}'))]
    )

    mock_llm.completion.side_effect = [
        resp_plan,
        Exception("400 Client Error: function/tool calling is not supported on this model"),
        resp_text_tool,
        resp_finish,
        resp_distill,
    ]

    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="1 passed", stderr="", exit_code=0)

    # Configure self-hosted vLLM / llama.cpp endpoint
    llm_cfg = LLMConfig(
        model="hosted_vllm/meta-llama-3-8b-instruct",
        api_base="http://vllm-server:8000/v1",
        api_key="EMPTY",
    )

    engine = AgentEngine(mock_sandbox, llm_client=mock_llm, llm_config=llm_cfg)
    ctx = SandboxContext("c-selfhosted", "/tmp/ws", "https://github.com/org/repo.git", "main", OpenJulesConfig(test_command="pytest"))

    res = engine.run(ctx, "Add /health endpoint")

    assert res.success is True
    assert res.verification_passed is True
    assert mock_sandbox.exec_in_sandbox.called
