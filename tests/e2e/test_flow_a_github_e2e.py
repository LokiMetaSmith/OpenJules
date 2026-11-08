"""Flow A E2E Test: Full GitHub Issue Webhook -> Agent Sandbox -> Tests -> PR created."""

import json
from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from src.agent.engine import AgentEngine
from src.api.webhook import app
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import ExecResult, SandboxContext, SandboxManager
from src.worker.executor import TaskExecutor

pytestmark = pytest.mark.e2e


def test_flow_a_github_full_lifecycle_e2e(tmp_path):
    ws_dir = str(tmp_path)
    client = TestClient(app)

    # Mock Docker sandbox run
    mock_sandbox = MagicMock(spec=SandboxManager)
    ctx = SandboxContext("cont-flow-a", ws_dir, "https://github.com/org/app.git", "main", OpenJulesConfig(test_command="pytest"))
    mock_sandbox.run_sandbox.return_value.__enter__.return_value = ctx

    # Simulate container commands
    def fake_exec(cid, cmd, timeout=120, workdir="/workspace", env=None):
        if "cat .openjules.yaml" in cmd:
            return ExecResult(stdout="test_command: pytest\n", stderr="", exit_code=0)
        elif "git checkout -b" in cmd:
            return ExecResult(stdout="", stderr="", exit_code=0)
        elif "git commit" in cmd:
            return ExecResult(stdout="commit created\n" + ("a" * 40), stderr="", exit_code=0)
        elif "git push" in cmd:
            return ExecResult(stdout="pushed to origin", stderr="", exit_code=0)
        elif "git diff" in cmd:
            return ExecResult(stdout="diff --git a/calc.py b/calc.py\n+ return a / b if b != 0 else 0", stderr="", exit_code=0)
        elif "pytest" in cmd:
            return ExecResult(stdout="1 passed in 0.04s", stderr="", exit_code=0)
        return ExecResult(stdout="ok", stderr="", exit_code=0)

    mock_sandbox.exec_in_sandbox.side_effect = fake_exec

    # Mock LLM calls (Step A plan, Step B tool edit, Step B done, outer loop experience)
    mock_llm = MagicMock()
    plan_json = json.dumps({"rationale": "Add zero guard", "target_files": ["calc.py"], "planned_tests": ["pytest"], "action_steps": ["Fix division"]})
    tc = MagicMock(id="tc_1")
    tc.function.name = "write_file"
    tc.function.arguments = json.dumps({"path": "calc.py", "content": "def div(a, b): return a / b if b else 0"})
    mock_llm.completion.side_effect = [
        MagicMock(choices=[MagicMock(message=MagicMock(content=plan_json))]),
        MagicMock(choices=[MagicMock(message=MagicMock(content=None, tool_calls=[tc]))]),
        MagicMock(choices=[MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))]),
        MagicMock(choices=[MagicMock(message=MagicMock(content='{"problem_summary":"zero div","solution_pattern":"guard","discovered_rules":[]}'))]),
    ]

    # Mock PyGithub
    mock_gh = MagicMock()
    mock_repo = MagicMock()
    mock_issue = MagicMock()
    mock_pr = MagicMock(html_url="https://github.com/org/app/pull/101")
    mock_repo.get_issue.return_value = mock_issue
    mock_repo.create_pull.return_value = mock_pr
    mock_gh.get_repo.return_value = mock_repo

    # Wire mocked components into TaskExecutor
    custom_executor = TaskExecutor(
        sandbox_manager=mock_sandbox,
        agent_engine=AgentEngine(mock_sandbox, llm_client=mock_llm),
        github_client=mock_gh,
    )

    with patch("src.api.webhook.executor", custom_executor):
        webhook_payload = {
            "action": "labeled",
            "issue": {
                "number": 101,
                "title": "Fix crash on divide by zero",
                "body": "Running calc with b=0 throws exception",
                "labels": [{"name": "openjules:run"}],
            },
            "repository": {
                "full_name": "org/app",
                "clone_url": "https://github.com/org/app.git",
                "default_branch": "main",
            },
            "sender": {"login": "developer"},
        }

        resp = client.post("/api/webhook", json=webhook_payload, headers={"X-GitHub-Event": "issues"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "dispatched"
        assert resp.json()["issue_id"] == 101

        # Verify PR creation call
        mock_repo.create_pull.assert_called_once()
        pr_kwargs = mock_repo.create_pull.call_args[1]
        assert "Fix crash on divide by zero" in pr_kwargs["title"]
        assert "Fixes #101" in pr_kwargs["body"]
