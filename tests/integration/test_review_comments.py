"""Integration tests for inline review comments submitted through Web UI."""

from unittest.mock import patch
import pytest
from fastapi.testclient import TestClient

from src.api.webhook import app
from src.worker.dispatcher import TaskPayload
from src.worker.executor import TaskExecutor

pytestmark = pytest.mark.integration


def test_inline_review_comment_submission():
    client = TestClient(app)

    with patch.object(TaskExecutor, "execute_task", return_value=True):
        # 1. Dispatch task
        task_payload = {
            "forge": "github",
            "issue_id": 777,
            "issue_title": "Interactive review task",
            "issue_body": "Implement endpoint",
            "repo_full_name": "owner/interactive-app",
            "repo_clone_url": "https://github.com/owner/interactive-app.git",
        }
        res_post = client.post("/api/tasks", json=task_payload)
        tid = res_post.json()["id"]

        # 2. Operator submits an inline review comment on line 42 of calc.py
        comment_payload = {
            "comment": "Ensure you check for negative divisors as well.",
            "file_path": "calc.py",
            "line_number": 42,
        }
        res_comment = client.post(f"/api/tasks/{tid}/comments", json=comment_payload)
        assert res_comment.status_code == 200
        data = res_comment.json()
        assert data["status"] == "recorded"
        assert "calc.py:42" in data["comment"]["content"]

        # 3. Retrieve task details - verify comment is in the thoughts stream
        res_get = client.get(f"/api/tasks/{tid}")
        details = res_get.json()
        thoughts = details["thoughts"]
        assert any("calc.py:42" in th.get("content", "") for th in thoughts)
        assert any(th.get("origin") == "Human Reviewer" for th in thoughts)
