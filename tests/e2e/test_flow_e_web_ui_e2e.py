"""Flow E E2E Test: Web UI Dashboard Three-Pane Workflow and Command Palette Actions."""

from unittest.mock import MagicMock, patch
import pytest
from fastapi.testclient import TestClient

from src.api.webhook import app
from src.worker.dispatcher import TaskPayload
from src.worker.executor import TaskExecutor

pytestmark = pytest.mark.e2e


def test_flow_e_web_ui_dashboard_e2e():
    client = TestClient(app)

    # 1. Fetch root page - confirms 3-pane dashboard is served
    resp_ui = client.get("/")
    assert resp_ui.status_code == 200
    assert "three-pane-container" in resp_ui.text
    assert "diff-command-palette" in resp_ui.text

    # 2. Dispatch a task from the UI
    task_payload = {
        "forge": "github",
        "issue_id": 999,
        "issue_title": "UI Initiated Task: Fix responsive layout",
        "issue_body": "Left pane shrinks on mobile",
        "repo_full_name": "openjules/web-client",
        "repo_clone_url": "https://github.com/openjules/web-client.git",
        "default_branch": "main",
    }

    with patch.object(TaskExecutor, "execute_task", return_value=True):
        resp_post = client.post("/api/tasks", json=task_payload)
        assert resp_post.status_code == 200
        tid = resp_post.json()["id"]

        # 3. Poll task status for middle pane
        resp_get = client.get(f"/api/tasks/{tid}")
        assert resp_get.status_code == 200
        task_data = resp_get.json()
        assert task_data["issue_id"] == 999
        assert len(task_data["thoughts"]) > 0

        # 4. Trigger command palette quick actions underneath the git diff
        for cmd in ["verify", "commit", "proposal", "harness_refine"]:
            resp_cmd = client.post(f"/api/tasks/{tid}/command", json={"command": cmd})
            assert resp_cmd.status_code == 200
            assert "result" in resp_cmd.json()

        # 5. Check task diff endpoint for right pane
        resp_diff = client.get(f"/api/tasks/{tid}/diff")
        assert resp_diff.status_code == 200
