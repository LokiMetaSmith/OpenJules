"""Integration tests for Multi-Repository Concurrent Swarm Orchestrator."""

import asyncio
from unittest.mock import MagicMock
import pytest

from src.worker.dispatcher import TaskPayload
from src.worker.executor import TaskExecutor
from src.worker.swarm_orchestrator import MultiRepoOrchestrator, SwarmWorkerLimits

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_multi_repo_orchestrator_concurrency_limits():
    mock_executor = MagicMock(spec=TaskExecutor)
    mock_executor.execute_task.return_value = True

    # Configure small limits for testing: max 3 global, max 2 per repo
    limits = SwarmWorkerLimits(max_global_workers=3, max_workers_per_repo=2)
    orchestrator = MultiRepoOrchestrator(task_executor=mock_executor, limits=limits)

    payload_r1_t1 = TaskPayload(forge="github", issue_id=1, issue_title="T1", repo_full_name="repo1", repo_clone_url="url1")
    payload_r1_t2 = TaskPayload(forge="github", issue_id=2, issue_title="T2", repo_full_name="repo1", repo_clone_url="url1")
    payload_r2_t1 = TaskPayload(forge="github", issue_id=1, issue_title="R2T1", repo_full_name="repo2", repo_clone_url="url2")

    assert orchestrator.can_dispatch("repo1") is True

    # Run tasks concurrently through orchestrator
    t1 = asyncio.create_task(orchestrator.execute_task_managed(payload_r1_t1))
    t2 = asyncio.create_task(orchestrator.execute_task_managed(payload_r1_t2))
    t3 = asyncio.create_task(orchestrator.execute_task_managed(payload_r2_t1))

    results = await asyncio.gather(t1, t2, t3)
    assert all(r is True for r in results)
    assert mock_executor.execute_task.call_count == 3

    # Status should return to 0 active
    status = orchestrator.get_orchestrator_status()
    assert status["global_active"] == 0
    assert status["max_global_workers"] == 3
