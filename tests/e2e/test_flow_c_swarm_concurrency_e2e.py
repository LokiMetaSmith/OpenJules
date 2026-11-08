"""Flow C E2E Test: Multi-Agent Swarm Concurrency and Baton Mutex Contention."""

from unittest.mock import MagicMock
import pytest

from src.agent.engine import AgentEngine
from src.polyphony.client import PolyphonyClient
from src.polyphony.manager import PolyphonySwarmManager
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import SandboxContext, SandboxManager
from src.worker.dispatcher import TaskPayload
from src.worker.executor import TaskExecutor

pytestmark = pytest.mark.e2e


def test_flow_c_multi_agent_swarm_concurrency_e2e():
    swarm_mgr = PolyphonySwarmManager()

    # Agent 1 starts working on issue 88 in owner/repo
    poly_agent1 = swarm_mgr.get_or_create_client("owner/shared-repo")
    # Simulate agent 1 owning the baton with a distinct node id
    poly_agent1.batons["task:88"] = "agent-1-active-node"
    poly_agent1.batons["file:src/core.py"] = "agent-1-active-node"
    poly_agent1.set_status("busy")
    poly_agent1.share_thought("Agent 1 modifying src/core.py for issue #88")

    # Agent 2 attempts to run on the exact same task
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_agent_engine = MagicMock(spec=AgentEngine)
    mock_git = MagicMock()

    # Agent 2 client has a different node id
    poly_agent2 = PolyphonyClient("owner/shared-repo", node_id="agent-2-node")
    poly_agent2.batons = dict(poly_agent1.batons)
    swarm_mgr._clients["owner/shared-repo"] = poly_agent2

    executor = TaskExecutor(
        sandbox_manager=mock_sandbox,
        agent_engine=mock_agent_engine,
        git_manager=mock_git,
        polyphony_manager=swarm_mgr,
    )

    duplicate_payload = TaskPayload(
        forge="github",
        issue_id=88,
        issue_title="Same issue duplicate task",
        repo_full_name="owner/shared-repo",
        repo_clone_url="https://github.com/owner/shared-repo.git",
    )

    # Execution must abort because task:88 baton is already locked
    success = executor.execute_task(duplicate_payload)
    assert success is False
    mock_agent_engine.run.assert_not_called()

    # Agent 2 attempts a different task (issue 89) targeting the same file
    diff_task_payload = TaskPayload(
        forge="github",
        issue_id=89,
        issue_title="Different task but overlapping file",
        repo_full_name="owner/shared-repo",
        repo_clone_url="https://github.com/owner/shared-repo.git",
    )

    # In polyphony, Agent 2 cannot acquire file:src/core.py until Agent 1 releases it
    poly_agent2 = PolyphonyClient("owner/shared-repo", node_id="agent-2")
    poly_agent2.batons = poly_agent1.batons
    assert poly_agent2.acquire_baton("file:src/core.py") is False

    # Once Agent 1 completes and releases batons:
    poly_agent1.release_all_batons(force=True)
    assert poly_agent2.acquire_baton("file:src/core.py") is True
