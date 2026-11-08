"""Integration tests for Keystone Polyphony swarm and Adaptive Harness coordination."""

import os
from unittest.mock import MagicMock
import pytest

from src.harness.adaptive_engine import AdaptiveHarness
from src.harness.config import HarnessConfig
from src.polyphony.client import PolyphonyClient
from src.polyphony.manager import PolyphonySwarmManager

pytestmark = pytest.mark.integration


def test_polyphony_and_adaptive_harness_integration(tmp_path):
    ws_dir = str(tmp_path)

    # 1. Initialize swarm manager and join repo polyphony
    mgr = PolyphonySwarmManager()
    poly_client = mgr.get_or_create_client("org/analytics-service")

    # 2. Agent updates thoughts and status in Polyphony
    poly_client.set_status("planning")
    poly_client.share_thought("Probing repository context and reading EXPERIENCE.md")

    # 3. Setup adaptive harness with custom experience file
    harness = AdaptiveHarness(HarnessConfig(experience_path="EXPERIENCE.md"))
    harness.experience_mgr.append_experience(
        workspace_dir=ws_dir,
        task_title="Optimize DB connection pool",
        problem_summary="Connection exhaustion under burst traffic.",
        solution_pattern="Set pool_size=20, max_overflow=10.",
        discovered_rules=["Always recycle database connections every 1800s."],
    )

    # 4. Harness loads past experience for prompt injection
    prompt_ext = harness.get_system_prompt_extension(ws_dir)
    assert "recycle database connections" in prompt_ext

    # 5. Acquire mutex batons in Polyphony for the target files
    assert poly_client.acquire_baton("file:db/pool.py") is True
    assert poly_client.acquire_baton("task:pool-opt") is True

    # 6. Verify batons are locked against other agents in the choir
    poly_other = PolyphonyClient("org/analytics-service", node_id="other-agent")
    poly_other.batons = poly_client.batons
    assert poly_other.acquire_baton("file:db/pool.py") is False

    # 7. Release all batons on teardown
    released = poly_client.release_all_batons()
    assert "file:db/pool.py" in released
    assert "task:pool-opt" in released
