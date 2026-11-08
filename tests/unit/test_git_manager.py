"""Unit tests for Phase 3: Git Lifecycle & Branch Management."""

from unittest.mock import MagicMock
import pytest

from src.git_manager.lifecycle import GitDiffSummary, GitLifecycleManager
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import ExecResult, SandboxContext, SandboxManager


def test_slugify():
    assert (
        GitLifecycleManager.slugify("Fix bug in calculation: division by zero!")
        == "fix-bug-in-calculation-divisio"
    )
    assert GitLifecycleManager.slugify("Feat: Add 123 support") == "feat-add-123-support"


def test_create_task_branch():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="", stderr="", exit_code=0)

    git_manager = GitLifecycleManager(mock_sandbox)
    ctx = SandboxContext(
        container_id="c1",
        workspace_dir="/tmp/ws",
        repo_url="https://github.com/org/repo.git",
        branch="main",
        config=OpenJulesConfig(),
    )

    branch = git_manager.create_task_branch(ctx, 42, "Fix user login crash")
    assert branch == "openjules/task-42-fix-user-login-crash"
    mock_sandbox.exec_in_sandbox.assert_called()


def test_commit_changes():
    mock_sandbox = MagicMock(spec=SandboxManager)
    fake_sha = "a" * 40
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout=f"[openjules/task-1 12345] fix: user login crash\n{fake_sha}\n",
        stderr="",
        exit_code=0,
    )

    git_manager = GitLifecycleManager(mock_sandbox)
    ctx = SandboxContext(
        container_id="c1",
        workspace_dir="/tmp/ws",
        repo_url="https://github.com/org/repo.git",
        branch="main",
        config=OpenJulesConfig(),
    )

    sha = git_manager.commit_changes(ctx, "fix: user login crash")
    assert sha == fake_sha


def test_push_branch():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout="Branch pushed",
        stderr="",
        exit_code=0,
    )

    git_manager = GitLifecycleManager(mock_sandbox)
    ctx = SandboxContext(
        container_id="c1",
        workspace_dir="/tmp/ws",
        repo_url="https://github.com/org/repo.git",
        branch="main",
        config=OpenJulesConfig(),
    )

    success = git_manager.push_branch(ctx, "openjules/task-42-fix-login")
    assert success is True


def test_generate_pr_summary():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(
        stdout="diff --git a/file.py b/file.py\n+new line\n",
        stderr="",
        exit_code=0,
    )

    git_manager = GitLifecycleManager(mock_sandbox)
    ctx = SandboxContext(
        container_id="c1",
        workspace_dir="/tmp/ws",
        repo_url="https://github.com/org/repo.git",
        branch="main",
        config=OpenJulesConfig(test_command="pytest"),
    )

    summary = git_manager.generate_pr_summary(
        ctx,
        issue_id=99,
        issue_title="Support IPv6",
        branch_name="openjules/task-99-support-ipv6",
        test_output="1 passed in 0.05s",
    )

    assert "Fixes #99" in summary.summary
    assert "openjules/task-99-support-ipv6" in summary.summary
    assert "+new line" in summary.diff
    assert "1 passed in 0.05s" in summary.summary
