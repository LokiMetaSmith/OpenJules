"""Polyphony Swarm Resource Controller and dynamic cohort scaling loop.

OpenJules acts as the external Resource & Execution Host:
- Reconciles signals from Polyphony's host_signal_outbox
- Manages dynamic cohort scaling: SCALE_UP, SPAWN_REVIEWER, IDLE_DRAIN, ESCALATE_TRIAGE
- Enforces hardware/budget quotas and two-key security boundary
"""

import json
import logging
import uuid
from typing import Any, Dict, List, Optional
import urllib.request
import urllib.error

from src.polyphony.models import (
    HostSignalStatus,
    HostTelemetrySignal,
    PolyphonyCohortConfig,
    SignalType,
    WorkerContainerInfo,
)
from src.polyphony.outbox import BaseOutboxBridge
from src.polyphony.provisioner import ContainerProvisionerProtocol

logger = logging.getLogger(__name__)


class SwarmResourceController:
    """Host-side controller reconciling Polyphony swarm signals with container resources."""

    def __init__(
        self,
        config: PolyphonyCohortConfig,
        outbox_bridge: BaseOutboxBridge,
        provisioner: ContainerProvisionerProtocol,
    ):
        self.config = config
        self.outbox_bridge = outbox_bridge
        self.provisioner = provisioner
        self.internal_metered_cost: float = 0.0

    def bootstrap_cohort(self) -> List[WorkerContainerInfo]:
        """Spawns the initial base cohort workers up to base_worker_count."""
        active = self.provisioner.list_active_workers(swarm_id=self.config.swarm_id)
        current_count = len(active)
        needed = self.config.base_worker_count - current_count

        spawned = []
        for _ in range(max(0, needed)):
            actor_id = f"jules-worker-{uuid.uuid4().hex[:8]}"
            worker = self.provisioner.spawn_worker(
                config=self.config,
                actor_id=actor_id,
                role="worker",
                target_task_ids=[],
                is_burst=False,
            )
            spawned.append(worker)
            self.internal_metered_cost += self.config.estimated_worker_cost
            logger.info(f"Bootstrapped base worker {actor_id} (container {worker.container_id})")

        return spawned

    def reconcile_signals(self) -> List[Dict[str, Any]]:
        """Executes one pass of the reconciliation loop over pending outbox signals."""
        signals = self.outbox_bridge.poll_pending_signals()
        results = []

        for signal in signals:
            try:
                res = self._handle_signal(signal)
                results.append(res)
            except Exception as e:
                logger.exception(f"Error handling signal {signal.signal_id}: {e}")
                self.outbox_bridge.acknowledge_signal(
                    signal.signal_id,
                    HostSignalStatus.REJECTED,
                    f"Host reconciliation error: {str(e)}",
                )
                results.append({"signal_id": signal.signal_id, "status": "ERROR", "error": str(e)})

        return results

    def _handle_signal(self, signal: HostTelemetrySignal) -> Dict[str, Any]:
        if signal.signal_type == SignalType.SCALE_UP:
            return self._handle_scale_up(signal)
        elif signal.signal_type == SignalType.SPAWN_REVIEWER:
            return self._handle_spawn_reviewer(signal)
        elif signal.signal_type == SignalType.IDLE_DRAIN:
            return self._handle_idle_drain(signal)
        elif signal.signal_type == SignalType.ESCALATE_TRIAGE:
            return self._handle_escalate_triage(signal)
        else:
            self.outbox_bridge.acknowledge_signal(
                signal.signal_id,
                HostSignalStatus.REJECTED,
                f"Unknown signal type: {signal.signal_type}",
            )
            return {"signal_id": signal.signal_id, "status": "REJECTED", "reason": "Unknown signal"}

    def _handle_scale_up(self, signal: HostTelemetrySignal) -> Dict[str, Any]:
        active_workers = self.provisioner.list_active_workers(swarm_id=self.config.swarm_id)
        current_active = len(active_workers)

        # Composite accounting: Effective Burn = max(signal.unsettled_cost_snapshot, controller.internal_metered_cost)
        effective_burn = max(signal.unsettled_cost_snapshot, self.internal_metered_cost)

        # Calculate allowable delta capped by max_burst_workers
        available_slots = self.config.max_burst_workers - current_active
        allowed_delta = min(signal.requested_worker_delta, available_slots)

        if allowed_delta <= 0:
            reason = f"Cohort at or above max burst capacity ({current_active}/{self.config.max_burst_workers})"
            self.outbox_bridge.acknowledge_signal(signal.signal_id, HostSignalStatus.REJECTED, reason)
            return {"signal_id": signal.signal_id, "status": "REJECTED", "reason": reason}

        # Check projected budget with allowed_delta
        projected_cost = effective_burn + (allowed_delta * self.config.estimated_worker_cost)
        if projected_cost > self.config.max_host_budget:
            reason = (
                f"Budget ceiling exceeded: projected {projected_cost:.2f} > max {self.config.max_host_budget:.2f} "
                f"(effective burn: {effective_burn:.2f})"
            )
            self.outbox_bridge.acknowledge_signal(signal.signal_id, HostSignalStatus.REJECTED, reason)
            return {"signal_id": signal.signal_id, "status": "REJECTED", "reason": reason}

        # Spawn allowed_delta workers
        spawned = []
        for _ in range(allowed_delta):
            actor_id = f"jules-worker-{uuid.uuid4().hex[:8]}"
            worker = self.provisioner.spawn_worker(
                config=self.config,
                actor_id=actor_id,
                role="worker",
                target_task_ids=signal.target_task_ids,
                is_burst=True,
            )
            spawned.append(worker)
            self.internal_metered_cost += self.config.estimated_worker_cost

        note = f"Spawned {len(spawned)} burst workers (total active: {current_active + len(spawned)})"
        self.outbox_bridge.acknowledge_signal(signal.signal_id, HostSignalStatus.FULFILLED, note)
        return {
            "signal_id": signal.signal_id,
            "status": "FULFILLED",
            "spawned_workers": [w.actor_id for w in spawned],
        }

    def _handle_spawn_reviewer(self, signal: HostTelemetrySignal) -> Dict[str, Any]:
        # Always spawn 1 ephemeral reviewer container with distinct actor_id
        reviewer_actor_id = f"jules-reviewer-{uuid.uuid4().hex[:8]}"
        worker = self.provisioner.spawn_worker(
            config=self.config,
            actor_id=reviewer_actor_id,
            role="reviewer",
            target_task_ids=signal.target_task_ids,
            is_burst=True,
        )
        self.internal_metered_cost += self.config.estimated_worker_cost
        note = f"Spawned ephemeral reviewer {reviewer_actor_id} for target tasks {signal.target_task_ids}"
        self.outbox_bridge.acknowledge_signal(signal.signal_id, HostSignalStatus.FULFILLED, note)
        return {
            "signal_id": signal.signal_id,
            "status": "FULFILLED",
            "reviewer_actor_id": reviewer_actor_id,
        }

    def _handle_idle_drain(self, signal: HostTelemetrySignal) -> Dict[str, Any]:
        active_workers = self.provisioner.list_active_workers(swarm_id=self.config.swarm_id)
        burst_workers = [w for w in active_workers if w.is_burst]

        # Determine target number of workers to keep
        # If all tasks are accepted or requested delta drains down to base_worker_count
        # Sort burst workers by spawn time (oldest or newest, FIFO)
        drained_count = 0
        reaped_workers = []

        for worker in burst_workers:
            # Active lease guard: check Polyphony store for active leases
            active_leases = self.outbox_bridge.count_active_leases(worker.actor_id)
            if active_leases > 0:
                logger.info(f"Skipping drain for {worker.actor_id} because it holds {active_leases} active lease(s)")
                continue

            # Ensure we don't drain below base_worker_count unless requested delta specifies total drain
            current_total = len(self.provisioner.list_active_workers(swarm_id=self.config.swarm_id))
            if current_total <= self.config.base_worker_count and signal.requested_worker_delta <= 0:
                break

            success = self.provisioner.stop_worker(worker.container_id)
            if success:
                drained_count += 1
                reaped_workers.append(worker.actor_id)
                if signal.requested_worker_delta > 0 and drained_count >= signal.requested_worker_delta:
                    break

        note = f"Reaped {drained_count} idle burst workers: {reaped_workers}"
        self.outbox_bridge.acknowledge_signal(signal.signal_id, HostSignalStatus.FULFILLED, note)
        return {
            "signal_id": signal.signal_id,
            "status": "FULFILLED",
            "drained_count": drained_count,
            "reaped_workers": reaped_workers,
        }

    def _handle_escalate_triage(self, signal: HostTelemetrySignal) -> Dict[str, Any]:
        logger.error(
            f"Swarm escalation triggered for swarm {self.config.swarm_id} by {signal.emitted_by_actor}: "
            f"{signal.rationale} (targets: {signal.target_task_ids})",
            extra={
                "swarm_id": self.config.swarm_id,
                "signal_id": signal.signal_id,
                "emitted_by": signal.emitted_by_actor,
                "target_task_ids": signal.target_task_ids,
                "rationale": signal.rationale,
            },
        )

        response_note = "Escalation logged to operator"
        if self.config.escalation_webhook_url:
            webhook_payload = json.dumps({
                "swarm_id": self.config.swarm_id,
                "signal_id": signal.signal_id,
                "actor_id": signal.emitted_by_actor,
                "targets": signal.target_task_ids,
                "rationale": signal.rationale,
            }).encode("utf-8")

            req = urllib.request.Request(
                self.config.escalation_webhook_url,
                data=webhook_payload,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            try:
                with urllib.request.urlopen(req, timeout=5) as resp:
                    response_note = f"Escalation webhook dispatched, HTTP status {resp.status}"
            except Exception as e:
                logger.warning(f"Failed to post escalation webhook: {e}")
                response_note = f"Escalation logged; webhook dispatch failed: {str(e)}"

        self.outbox_bridge.acknowledge_signal(signal.signal_id, HostSignalStatus.ACKNOWLEDGED, response_note)
        return {
            "signal_id": signal.signal_id,
            "status": "ACKNOWLEDGED",
            "response_note": response_note,
        }
