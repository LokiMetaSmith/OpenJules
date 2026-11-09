"""Unit tests for SQLite persistent storage provider."""

import os
import pytest
from src.storage.sqlite_storage import SQLiteStorageProvider


def test_sqlite_storage_tasks_lifecycle(tmp_path):
    db_file = str(tmp_path / "test_storage.db")
    storage = SQLiteStorageProvider(db_path=db_file)

    task_payload = {
        "id": "github-123",
        "forge": "github",
        "repo": "owner/repo",
        "issue_id": 123,
        "status": "running",
        "thoughts": [{"content": "Starting task..."}],
    }

    # Save
    storage.save_task("github-123", task_payload)

    # Retrieve
    t = storage.get_task("github-123")
    assert t is not None
    assert t["repo"] == "owner/repo"
    assert t["status"] == "running"

    # Update
    task_payload["status"] = "completed"
    storage.save_task("github-123", task_payload)
    t_updated = storage.get_task("github-123")
    assert t_updated["status"] == "completed"

    # List
    all_tasks = storage.list_tasks()
    assert len(all_tasks) == 1
    assert all_tasks[0]["id"] == "github-123"


def test_sqlite_storage_batons_lifecycle(tmp_path):
    db_file = str(tmp_path / "test_batons.db")
    storage = SQLiteStorageProvider(db_path=db_file)

    # Save batons
    storage.save_baton("owner/repo", "task:42", "node-1")
    storage.save_baton("owner/repo", "file:main.py", "node-1")

    batons = storage.get_all_batons()
    assert batons["owner/repo:task:42"] == "node-1"
    assert batons["owner/repo:file:main.py"] == "node-1"

    # Remove
    storage.remove_baton("owner/repo", "task:42")
    updated_batons = storage.get_all_batons()
    assert "owner/repo:task:42" not in updated_batons
    assert "owner/repo:file:main.py" in updated_batons
