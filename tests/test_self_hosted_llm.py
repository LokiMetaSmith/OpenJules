"""Unit and integration tests for self-hosted LLMs (vLLM, llama.cpp, Ollama)."""

import json
from unittest.mock import MagicMock
import pytest

from src.agent.engine import AgentEngine
from src.agent.llm_config import LLMConfig, resolve_llm_config
from src.sandbox.config import OpenJulesConfig, load_config_from_yaml
from src.sandbox.docker_driver import ExecResult, SandboxContext, SandboxManager


def test_resolve_llm_config_defaults(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.delenv("LLM_API_BASE", raising=False)
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    cfg = resolve_llm_config()
    assert cfg.model == "gpt-4o"
    assert cfg.api_base is None
    assert cfg.api_key == "EMPTY"


def test_resolve_llm_config_from_env(monkeypatch):
    monkeypatch.setenv("LLM_MODEL", "Qwen/Qwen2.5-Coder-7B-Instruct")
    monkeypatch.setenv("LLM_API_BASE", "http://vllm:8000/v1")
    monkeypatch.setenv("LLM_API_KEY", "token123")

    cfg = resolve_llm_config()
    assert cfg.model == "Qwen/Qwen2.5-Coder-7B-Instruct"
    assert cfg.api_base == "http://vllm:8000/v1"
    assert cfg.api_key == "token123"


def test_resolve_llm_config_from_yaml():
    yaml_content = """
    llm:
      model: "mistral-7b-instruct"
      api_base: "http://localhost:8080/v1"
      temperature: 0.1
    """
    conf = load_config_from_yaml(yaml_content)
    assert conf.llm is not None
    assert conf.llm.model == "mistral-7b-instruct"
    assert conf.llm.api_base == "http://localhost:8080/v1"
    assert conf.llm.temperature == 0.1


def test_agent_engine_self_hosted_api_base_propagation():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()

    mock_llm.completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))]
    )

    llm_cfg = LLMConfig(
        model="hosted_vllm/llama-3",
        api_base="http://vllm-host:8000/v1",
        api_key="secret",
    )

    engine = AgentEngine(mock_sandbox, llm_client=mock_llm, llm_config=llm_cfg)
    messages = [{"role": "user", "content": "hello"}]
    engine._call_llm(messages)

    mock_llm.completion.assert_called_once()
    kwargs = mock_llm.completion.call_args[1]
    assert kwargs["model"] == "hosted_vllm/llama-3"
    assert kwargs["api_base"] == "http://vllm-host:8000/v1"
    assert kwargs["api_key"] == "secret"


def test_agent_engine_tools_schema_rejection_fallback():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()

    # First call with tools fails (simulating local server rejecting tools parameter)
    # Second call succeeds without tools
    mock_llm.completion.side_effect = [
        Exception("tools schema is not supported by this server"),
        MagicMock(choices=[MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))]),
    ]

    llm_cfg = LLMConfig(model="llamacpp-model", api_base="http://localhost:8080/v1")
    engine = AgentEngine(mock_sandbox, llm_client=mock_llm, llm_config=llm_cfg)

    messages = [{"role": "user", "content": "do task"}]
    res = engine._call_llm(messages, tools=[{"type": "function"}])

    assert mock_llm.completion.call_count == 2
    # Second call should not have 'tools'
    second_kwargs = mock_llm.completion.call_args_list[1][1]
    assert "tools" not in second_kwargs


def test_agent_engine_json_tool_fallback_execution():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()

    # Model outputs a JSON tool call block in text instead of native function call
    json_action = '```json\n{"tool": "write_file", "arguments": {"path": "a.txt", "content": "data"}}\n```'
    resp1 = MagicMock(choices=[MagicMock(message=MagicMock(content=json_action, tool_calls=None))])
    resp2 = MagicMock(choices=[MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))])

    mock_llm.completion.side_effect = [resp1, resp2]
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="Successfully wrote", stderr="", exit_code=0)

    engine = AgentEngine(mock_sandbox, llm_client=mock_llm)
    from src.agent.engine import PlanOutput
    from src.agent.tools import AgentTools

    plan = PlanOutput(rationale="test", target_files=[], planned_tests=[], action_steps=[])
    tools = AgentTools(mock_sandbox, "c1")
    ctx = SandboxContext("c1", "/tmp/ws", "https://github.com/o/r.git", "main", OpenJulesConfig())

    turns = engine.step_b_action_loop(ctx, "write a file", plan, tools)
    assert turns == 2
    mock_sandbox.exec_in_sandbox.assert_called()
