"""Polyphony Outbox Bridge interfaces and implementations.

Provides host communication with the Polyphony coordinator via:
1. SQLiteOutboxBridge (direct WAL inspection and update on shared host mount)
2. McpOutboxBridge (calling MCP tools using OPENJULES_HOST_KEY)
3. InMemoryOutboxBridge (deterministic in-memory bridge for testing)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
import json
import logging
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Union

from src.polyphony.models import HostSignalStatus, HostTelemetrySignal, SignalType

logger = logging.getLogger(__name__)


class BaseOutboxBridge(ABC):
    """Abstract interface for polling and acknowledging host signals from Polyphony."""

    @abstractmethod
    def poll_pending_signals(self) -> List[HostTelemetrySignal]:
        """Polls for signals awaiting OpenJules host processing (host_status == 'PENDING')."""
        pass

    @abstractmethod
    def acknowledge_signal(
        self,
        signal_id: str,
        new_status: HostSignalStatus | str,
        response_note: str = "",
    ) -> bool:
        """Updates the status and response note for a processed signal."""
        pass

    @abstractmethod
    def count_active_leases(self, actor_id: str, now: Optional[float] = None) -> int:
        """Counts active task assignments/leases held by an actor in the Polyphony store."""
        pass


class SQLiteOutboxBridge(BaseOutboxBridge):
    """Direct SQLite WAL inspection and update on a shared host volume."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        # Enable WAL mode for concurrent multi-process access
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA busy_timeout = 30000;")
        return conn

    def _init_db(self) -> None:
        """Ensures the host_signal_outbox and assignments tables exist."""
        with self._get_connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS host_signal_outbox (
                    signal_id TEXT PRIMARY KEY,
                    emitted_by_actor TEXT NOT NULL,
                    signal_type TEXT NOT NULL,
                    requested_worker_delta INTEGER NOT NULL,
                    target_task_ids TEXT NOT NULL,
                    unsettled_cost_snapshot REAL NOT NULL,
                    rationale TEXT NOT NULL,
                    host_status TEXT NOT NULL DEFAULT 'PENDING',
                    host_response_note TEXT DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS assignments (
                    assignment_id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    created_at REAL NOT NULL
                );
                """
            )
            conn.commit()

    def poll_pending_signals(self) -> List[HostTelemetrySignal]:
        signals = []
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT signal_id, emitted_by_actor, signal_type, requested_worker_delta,
                       target_task_ids, unsettled_cost_snapshot, rationale, host_status,
                       host_response_note, created_at, updated_at
                FROM host_signal_outbox
                WHERE host_status = 'PENDING'
                ORDER BY created_at ASC
                """
            )
            for row in cursor.fetchall():
                try:
                    task_ids = json.loads(row["target_task_ids"]) if row["target_task_ids"] else []
                except Exception:
                    task_ids = []

                signals.append(
                    HostTelemetrySignal(
                        signal_id=row["signal_id"],
                        emitted_by_actor=row["emitted_by_actor"],
                        signal_type=SignalType(row["signal_type"]),
                        requested_worker_delta=row["requested_worker_delta"],
                        target_task_ids=task_ids,
                        unsettled_cost_snapshot=float(row["unsettled_cost_snapshot"]),
                        rationale=row["rationale"] or "",
                        host_status=HostSignalStatus(row["host_status"]),
                        host_response_note=row["host_response_note"] or "",
                        created_at=float(row["created_at"]),
                        updated_at=float(row["updated_at"]),
                    )
                )
        return signals

    def acknowledge_signal(
        self,
        signal_id: str,
        new_status: HostSignalStatus | str,
        response_note: str = "",
    ) -> bool:
        status_val = new_status.value if isinstance(new_status, HostSignalStatus) else str(new_status)
        now = time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                UPDATE host_signal_outbox
                SET host_status = ?, host_response_note = ?, updated_at = ?
                WHERE signal_id = ?
                """,
                (status_val, response_note, now, signal_id),
            )
            conn.commit()
            return cursor.rowcount > 0

    def count_active_leases(self, actor_id: str, now: Optional[float] = None) -> int:
        current_time = now if now is not None else time.time()
        with self._get_connection() as conn:
            cursor = conn.execute(
                """
                SELECT COUNT(*) as count FROM assignments
                WHERE actor_id = ?
                  AND status IN ('assigned', 'running')
                  AND lease_expires_at > ?
                """,
                (actor_id, current_time),
            )
            row = cursor.fetchone()
            return int(row["count"]) if row else 0

    def emit_signal(self, signal: HostTelemetrySignal) -> None:
        """Utility for inserting a signal (used by testing or coordinator)."""
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO host_signal_outbox (
                    signal_id, emitted_by_actor, signal_type, requested_worker_delta,
                    target_task_ids, unsettled_cost_snapshot, rationale, host_status,
                    host_response_note, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal.signal_id,
                    signal.emitted_by_actor,
                    signal.signal_type.value,
                    signal.requested_worker_delta,
                    json.dumps(signal.target_task_ids),
                    signal.unsettled_cost_snapshot,
                    signal.rationale,
                    signal.host_status.value,
                    signal.host_response_note,
                    signal.created_at,
                    signal.updated_at,
                ),
            )
            conn.commit()


class McpOutboxBridge(BaseOutboxBridge):
    """Communicates with Polyphony MCP coordinator daemon using OPENJULES_HOST_KEY."""

    def __init__(
        self,
        host_control_key: str,
        mcp_caller: Optional[Callable[[str, Dict[str, Any]], Dict[str, Any]]] = None,
        db_path: Optional[str] = None,
    ):
        self.host_control_key = host_control_key
        self.mcp_caller = mcp_caller
        self.db_path = db_path
        self._fallback_sqlite = SQLiteOutboxBridge(db_path) if db_path else None

    def poll_pending_signals(self) -> List[HostTelemetrySignal]:
        if self.mcp_caller:
            try:
                res = self.mcp_caller(
                    "openjules_poll_outbox",
                    {"host_key": self.host_control_key},
                )
                raw_signals = res.get("signals", [])
                signals = []
                for s in raw_signals:
                    signals.append(
                        HostTelemetrySignal(
                            signal_id=s["signal_id"],
                            emitted_by_actor=s.get("emitted_by_actor", "unknown"),
                            signal_type=SignalType(s["signal_type"]),
                            requested_worker_delta=s.get("requested_worker_delta", 0),
                            target_task_ids=s.get("target_task_ids", []),
                            unsettled_cost_snapshot=float(s.get("unsettled_cost_snapshot", 0.0)),
                            rationale=s.get("rationale", ""),
                            host_status=HostSignalStatus(s.get("host_status", "PENDING")),
                            host_response_note=s.get("host_response_note", ""),
                            created_at=float(s.get("created_at", time.time())),
                            updated_at=float(s.get("updated_at", time.time())),
                        )
                    )
                return signals
            except Exception as e:
                logger.warning(f"MCP poll failed, falling back to SQLite if available: {e}")

        if self._fallback_sqlite:
            return self._fallback_sqlite.poll_pending_signals()
        return []

    def acknowledge_signal(
        self,
        signal_id: str,
        new_status: HostSignalStatus | str,
        response_note: str = "",
    ) -> bool:
        status_val = new_status.value if isinstance(new_status, HostSignalStatus) else str(new_status)
        if self.mcp_caller:
            try:
                res = self.mcp_caller(
                    "openjules_ack_signal",
                    {
                        "host_key": self.host_control_key,
                        "signal_id": signal_id,
                        "status": status_val,
                        "response_note": response_note,
                    },
                )
                if res.get("success", False):
                    return True
            except Exception as e:
                logger.warning(f"MCP ack failed, falling back to SQLite if available: {e}")

        if self._fallback_sqlite:
            return self._fallback_sqlite.acknowledge_signal(signal_id, status_val, response_note)
        return False

    def count_active_leases(self, actor_id: str, now: Optional[float] = None) -> int:
        if self.mcp_caller:
            try:
                res = self.mcp_caller(
                    "openjules_count_active_leases",
                    {"host_key": self.host_control_key, "actor_id": actor_id},
                )
                if "count" in res:
                    return int(res["count"])
            except Exception:
                pass

        if self._fallback_sqlite:
            return self._fallback_sqlite.count_active_leases(actor_id, now)
        return 0


class InMemoryOutboxBridge(BaseOutboxBridge):
    """In-memory bridge for fast, deterministic unit testing."""

    def __init__(self):
        self.signals: Dict[str, HostTelemetrySignal] = {}
        self.active_leases: Dict[str, int] = {}  # actor_id -> active lease count

    def add_signal(self, signal: HostTelemetrySignal) -> None:
        self.signals[signal.signal_id] = signal

    def set_active_leases(self, actor_id: str, count: int) -> None:
        self.active_leases[actor_id] = count

    def poll_pending_signals(self) -> List[HostTelemetrySignal]:
        return [
            s for s in sorted(self.signals.values(), key=lambda x: x.created_at)
            if s.host_status == HostSignalStatus.PENDING
        ]

    def acknowledge_signal(
        self,
        signal_id: str,
        new_status: HostSignalStatus | str,
        response_note: str = "",
    ) -> bool:
        if signal_id not in self.signals:
            return False
        status_val = new_status if isinstance(new_status, HostSignalStatus) else HostSignalStatus(new_status)
        sig = self.signals[signal_id]
        sig.host_status = status_val
        sig.host_response_note = response_note
        sig.updated_at = time.time()
        return True

    def count_active_leases(self, actor_id: str, now: Optional[float] = None) -> int:
        return self.active_leases.get(actor_id, 0)
