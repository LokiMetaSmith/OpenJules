"""SQLite persistent storage provider for tasks, thoughts, and Polyphony state."""

import json
import sqlite3
import threading
from typing import Any, Dict, List, Optional
from src.storage.base import BaseStorageProvider


class SQLiteStorageProvider(BaseStorageProvider):
    """SQLite-backed storage provider for zero-dependency persistence."""

    def __init__(self, db_path: str = "openjules.db"):
        self.db_path = db_path
        self._lock = threading.Lock()
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self):
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        task_id TEXT PRIMARY KEY,
                        forge TEXT,
                        repo TEXT,
                        issue_id TEXT,
                        status TEXT,
                        data_json TEXT,
                        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS batons (
                        repo TEXT,
                        resource TEXT,
                        owner TEXT,
                        acquired_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY (repo, resource)
                    )
                    """
                )
                conn.commit()

    def save_task(self, task_id: str, task_data: Dict[str, Any]) -> None:
        data_json = json.dumps(task_data)
        forge = task_data.get("forge", "")
        repo = task_data.get("repo", "")
        issue_id = str(task_data.get("issue_id", ""))
        status = task_data.get("status", "running")

        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT INTO tasks (task_id, forge, repo, issue_id, status, data_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    ON CONFLICT(task_id) DO UPDATE SET
                        status = excluded.status,
                        data_json = excluded.data_json,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (task_id, forge, repo, issue_id, status, data_json),
                )
                conn.commit()

    def get_task(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute("SELECT data_json FROM tasks WHERE task_id = ?", (task_id,))
                row = cur.fetchone()
                if row:
                    return json.loads(row["data_json"])
        return None

    def list_tasks(self) -> List[Dict[str, Any]]:
        tasks = []
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute("SELECT data_json FROM tasks ORDER BY updated_at DESC")
                for row in cur.fetchall():
                    tasks.append(json.loads(row["data_json"]))
        return tasks

    def save_baton(self, repo: str, resource: str, owner: str) -> None:
        with self._lock:
            with self._get_connection() as conn:
                conn.execute(
                    """
                    INSERT OR REPLACE INTO batons (repo, resource, owner)
                    VALUES (?, ?, ?)
                    """,
                    (repo, resource, owner),
                )
                conn.commit()

    def remove_baton(self, repo: str, resource: str) -> None:
        with self._lock:
            with self._get_connection() as conn:
                conn.execute("DELETE FROM batons WHERE repo = ? AND resource = ?", (repo, resource))
                conn.commit()

    def get_all_batons(self) -> Dict[str, str]:
        batons = {}
        with self._lock:
            with self._get_connection() as conn:
                cur = conn.execute("SELECT repo, resource, owner FROM batons")
                for row in cur.fetchall():
                    key = f"{row['repo']}:{row['resource']}"
                    batons[key] = row["owner"]
        return batons
