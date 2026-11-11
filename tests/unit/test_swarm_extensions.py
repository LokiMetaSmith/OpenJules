"""Unit and integration tests for the 5 Swarm Controller extensions.

Covers:
1. SwarmCohortRegistry & SwarmReconciliationDaemon background loop
2. WorkerHealthMonitor container liveness detection & self-healing
3. FastAPI REST & WebSocket endpoints (/api/polyphony/cohorts, /ws/polyphony/{swarm_id})
4. CLI swarm commands (start, status, drain)
"""

import asyncio
from unittest.mock import MagicMock
from fastapi.testclient import TestClient
import pytest

from src.api.webhook import app
from src.cli import handle_swarm_cli
from src.polyphony import (
    InMemoryOutboxBridge,
    MockContainerProvisioner,
    PolyphonyCohortConfig,
    SignalType,
    SwarmResourceController,
)
from src.polyphony.daemon import SwarmReconciliationDaemon
from src.polyphony.health import WorkerHealthMonitor
from src.polyphony.registry import SwarmCohortRegistry, cohort_registry


def test_swarm_cohort_registry_and_daemon():
    mock_prov = MockContainerProvisioner()
    registry = SwarmCohortRegistry(default_provisioner=mock_prov)

    cfg = PolyphonyCohortConfig(
        swarm_id="swarm-reg-1",
        repo_url="https://github.com/org/repo.git",
        base_worker_count=2,
    )
    bridge = InMemoryOutboxBridge()
    ctrl = registry.register_cohort(cfg, outbox_bridge=bridge, bootstrap=True)

    assert registry.get_controller("swarm-reg-1") is ctrl
    assert len(mock_prov.list_active_workers()) == 2
    assert len(registry.list_cohorts()) == 1

    events_received = []

    def on_event(s_id, results):
        events_received.append((s_id, results))

    daemon = SwarmReconciliationDaemon(
        registry=registry,
        poll_interval=0.1,
        on_reconciliation_event=on_event,
    )

    # Queue a scale up signal
    from src.polyphony.models import HostTelemetrySignal
    bridge.add_signal(
        HostTelemetrySignal(
            signal_id="sig-daemon-1",
            emitted_by_actor="test",
            signal_type=SignalType.SCALE_UP,
            requested_worker_delta=1,
            rationale="Daemon test",
        )
    )

    res = daemon.run_one_pass()
    assert "swarm-reg-1" in res
    assert len(events_received) == 1
    assert len(mock_prov.list_active_workers()) == 3

    # Clean removal
    assert registry.remove_cohort("swarm-reg-1", drain_workers=True) is True
    assert len(mock_prov.list_active_workers()) == 0


def test_worker_health_monitor_self_healing():
    mock_prov = MockContainerProvisioner()
    cfg = PolyphonyCohortConfig(
        swarm_id="swarm-health-1",
        repo_url="https://github.com/org/repo.git",
        base_worker_count=2,
    )
    bridge = InMemoryOutboxBridge()
    ctrl = SwarmResourceController(cfg, bridge, mock_prov)
    ctrl.bootstrap_cohort()

    workers = mock_prov.list_active_workers()
    assert len(workers) == 2
    crashed_id = workers[0].container_id

    # Liveness check returns false for crashed container
    monitor = WorkerHealthMonitor(ctrl, liveness_check=lambda cid: cid != crashed_id)
    heal_result = monitor.check_and_heal()

    assert heal_result["swarm_id"] == "swarm-health-1"
    assert len(heal_result["reaped_dead_workers"]) == 1
    assert len(heal_result["replenished_base_workers"]) == 1
    assert heal_result["current_total_active"] == 2


def test_polyphony_rest_api_lifecycle():
    cohort_registry._controllers.clear()
    cohort_registry.default_provisioner = MockContainerProvisioner()

    client = TestClient(app)

    # 1. Create cohort
    create_payload = {
        "swarm_id": "api-swarm-lifecycle",
        "repo_url": "https://github.com/org/test-repo.git",
        "base_worker_count": 2,
        "max_burst_workers": 6,
        "max_host_budget": 50.0,
    }
    resp = client.post("/api/polyphony/cohorts", json=create_payload)
    assert resp.status_code == 200
    assert resp.json()["status"] == "created"
    assert resp.json()["base_workers_spawned"] == 2

    # 2. Duplicate rejection
    dup_resp = client.post("/api/polyphony/cohorts", json=create_payload)
    assert dup_resp.status_code == 400

    # 3. List cohorts
    list_resp = client.get("/api/polyphony/cohorts")
    assert list_resp.status_code == 200
    assert len(list_resp.json()) == 1

    # 4. Get cohort details
    detail_resp = client.get("/api/polyphony/cohorts/api-swarm-lifecycle")
    assert detail_resp.status_code == 200
    data = detail_resp.json()
    assert data["active_worker_count"] == 2
    assert len(data["base_workers"]) == 2

    # 5. Trigger Signal (SCALE_UP)
    sig_resp = client.post(
        "/api/polyphony/cohorts/api-swarm-lifecycle/signal",
        json={
            "signal_type": "SCALE_UP",
            "requested_worker_delta": 2,
            "target_task_ids": ["task-1"],
            "rationale": "Scaling via API",
        },
    )
    assert sig_resp.status_code == 200
    assert sig_resp.json()["status"] == "reconciled"

    # Verify scaled
    scaled_resp = client.get("/api/polyphony/cohorts/api-swarm-lifecycle")
    assert scaled_resp.json()["active_worker_count"] == 4

    # 6. Delete cohort
    del_resp = client.delete("/api/polyphony/cohorts/api-swarm-lifecycle")
    assert del_resp.status_code == 200
    assert del_resp.json()["status"] == "deleted"


def test_cli_swarm_subcommands(capsys):
    cohort_registry._controllers.clear()
    cohort_registry.default_provisioner = MockContainerProvisioner()

    # CLI start
    args_start = MagicMock()
    args_start.swarm_action = "start"
    args_start.repo = "https://github.com/org/cli-repo.git"
    args_start.swarm_id = "cli-test-swarm"
    args_start.workers = 2
    args_start.max_burst = 6
    args_start.budget = 75.0

    handle_swarm_cli(args_start)
    captured = capsys.readouterr().out
    assert "Started Polyphony Cohort 'cli-test-swarm'" in captured
    assert "Base Workers: 2 / 2" in captured

    # CLI status
    args_status = MagicMock()
    args_status.swarm_action = "status"
    args_status.swarm_id = "cli-test-swarm"

    handle_swarm_cli(args_status)
    captured_status = capsys.readouterr().out
    assert "Polyphony Cohort Status: cli-test-swarm" in captured_status
    assert "Active Workers: 2" in captured_status

    # CLI drain
    args_drain = MagicMock()
    args_drain.swarm_action = "drain"
    args_drain.swarm_id = "cli-test-swarm"
    args_drain.delta = 0

    handle_swarm_cli(args_drain)
    captured_drain = capsys.readouterr().out
    assert "Drained cohort 'cli-test-swarm'" in captured_drain
