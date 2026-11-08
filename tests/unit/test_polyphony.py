"""Unit and integration tests for Keystone Polyphony swarm coordination."""

import time
from unittest.mock import MagicMock
import pytest

from src.agent.engine import AgentEngine, PlanOutput
from src.agent.tools import AgentTools
from src.polyphony.client import PolyphonyClient, derive_repo_swarm_key
from src.polyphony.manager import PolyphonySwarmManager
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import ExecResult, SandboxContext, SandboxManager
from src.worker.dispatcher import TaskPayload
from src.worker.executor import TaskExecutor


def test_derive_repo_swarm_key():
    k1 = derive_repo_swarm_key("owner/repo")
    k2 = derive_repo_swarm_key("OWNER/REPO")
    k3 = derive_repo_swarm_key("other/repo")

    assert len(k1) == 64
    assert k1 == k2  # Case-insensitive
    assert k1 != k3


def test_polyphony_client_baton_and_thoughts():
    client1 = PolyphonyClient("owner/repo", node_id="agent-1")
    client2 = PolyphonyClient("owner/repo", node_id="agent-2")

    # Thoughts
    t = client1.share_thought("Examining repository...")
    assert t["origin"] == "agent-1"
    assert "Examining" in t["content"]
    assert client1.thoughts["agent-1"]["status"] == "idle"

    # Status
    client1.set_status("busy")
    assert client1.status == "busy"

    # Baton Acquisition
    assert client1.acquire_baton("task:42") is True
    # Same node re-acquires
    assert client1.acquire_baton("task:42") is True

    # Share baton state with client2
    client2.batons = client1.batons
    # client2 denied
    assert client2.acquire_baton("task:42") is False

    # Release baton
    assert client1.release_baton("task:42") is True
    # Now available
    assert client2.acquire_baton("task:42") is True

    # Backlog sync
    backlog_entry = client2.sync_task_backlog(42, "Fix bug", status="in_progress")
    assert backlog_entry["status"] == "in_progress"
    assert client2.backlog["42"]["owner"] == "agent-2"


def test_polyphony_manager():
    mgr = PolyphonySwarmManager()
    c1 = mgr.get_or_create_client("owner/repo")
    c2 = mgr.get_or_create_client("owner/repo")
    c3 = mgr.get_or_create_client("other/repo")

    assert c1 is c2
    assert c1.swarm_key != c3.swarm_key

    mgr.remove_client("owner/repo")
    c1_new = mgr.get_or_create_client("owner/repo")
    assert c1_new is not c1


def test_agent_engine_polyphony_coordination():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()

    plan_json = '{"rationale": "Add cache layer", "target_files": ["cache.py"], "planned_tests": ["pytest"], "action_steps": ["Write cache.py"]}'
    mock_llm.completion.side_effect = [
        MagicMock(choices=[MagicMock(message=MagicMock(content=plan_json))]),
        MagicMock(choices=[MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))]),
    ]

    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="ok", stderr="", exit_code=0)

    engine = AgentEngine(mock_sandbox, llm_client=mock_llm)
    poly = PolyphonyClient("owner/repo", node_id="agent-test")

    context = SandboxContext(
        container_id="c1",
        workspace_dir="/tmp/ws",
        repo_url="https://github.com/owner/repo.git",
        branch="main",
        config=OpenJulesConfig(test_command="pytest"),
    )

    res = engine.run(context, "Implement cache", polyphony=poly)
    assert res.success is True
    assert poly.status == "idle"
    # Verify baton was acquired for planned target file
    assert "file:cache.py" in poly.batons
    assert any("Step A" in th["content"] for th in poly.thought_stream)


def test_task_executor_baton_collision_prevention():
    mock_sandbox = MagicMock()
    mock_agent = MagicMock()
    mock_git = MagicMock()

    mgr = PolyphonySwarmManager()
    poly = mgr.get_or_create_client("owner/repo")
    # Simulate another agent already holding the baton for issue 100
    poly.batons["task:100"] = "agent-other"

    executor = TaskExecutor(
        sandbox_manager=mock_sandbox,
        agent_engine=mock_agent,
        git_manager=mock_git,
        polyphony_manager=mgr,
    )

    payload = TaskPayload(
        issue_id=100,
        issue_title="Collision task",
        repo_full_name="owner/repo",
        repo_clone_url="https://github.com/owner/repo.git",
    )

    result = executor.execute_task(payload)
    # Should abort due to lock contention
    assert result is False
    mock_agent.run.assert_not_called()
