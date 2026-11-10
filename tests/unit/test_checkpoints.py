"""Unit tests for GitCheckpointManager."""

from unittest.mock import MagicMock
import pytest
from src.git_manager.checkpoints import GitCheckpointManager
from src.sandbox.docker_driver import ExecResult, SandboxManager


def test_checkpoint_create_and_rollback():
    mock_sandbox = MagicMock(spec=SandboxManager)
    # Mock tree commit creation
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="tree_commit_sha_123456\n", stderr="", exit_code=0)

    mgr = GitCheckpointManager(mock_sandbox)
    cp = mgr.create_checkpoint("c1", "turn_1", description="before editing calc.py")

    assert cp is not None
    assert cp.name == "turn_1"
    assert cp.commit_or_tree_sha == "tree_commit_sha_123456"

    # Rollback
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="", stderr="", exit_code=0)
    success = mgr.rollback_to_checkpoint("c1", "turn_1")
    assert success is True

    # List checkpoints
    cps = mgr.list_checkpoints("c1")
    assert len(cps) == 1
    assert cps[0].name == "turn_1"
