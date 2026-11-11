"""Continuous asynchronous background reconciliation daemon for Polyphony Swarm cohorts."""

import asyncio
import logging
from typing import Callable, Dict, List, Optional

from src.polyphony.controller import SwarmResourceController
from src.polyphony.registry import SwarmCohortRegistry, cohort_registry

logger = logging.getLogger(__name__)


class SwarmReconciliationDaemon:
    """Asynchronous polling daemon that continuously reconciles swarm signals across cohorts."""

    def __init__(
        self,
        registry: Optional[SwarmCohortRegistry] = None,
        poll_interval: float = 2.0,
        on_reconciliation_event: Optional[Callable[[str, List[Dict[str, any]]], None]] = None,
    ):
        self.registry = registry or cohort_registry
        self.poll_interval = poll_interval
        self.on_reconciliation_event = on_reconciliation_event
        self._running = False
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Starts the background reconciliation loop."""
        if self._running:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())
        logger.info(f"SwarmReconciliationDaemon started (poll_interval={self.poll_interval}s)")

    async def stop(self) -> None:
        """Gracefully stops the background loop."""
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("SwarmReconciliationDaemon stopped")

    def run_one_pass(self) -> Dict[str, List[Dict[str, any]]]:
        """Executes a single synchronous reconciliation pass across all registered cohorts."""
        all_results = {}
        for controller in self.registry.list_cohorts():
            swarm_id = controller.config.swarm_id
            try:
                results = controller.reconcile_signals()
                if results:
                    all_results[swarm_id] = results
                    if self.on_reconciliation_event:
                        self.on_reconciliation_event(swarm_id, results)
            except Exception as e:
                logger.error(f"Error reconciling cohort {swarm_id}: {e}")
        return all_results

    async def _run_loop(self) -> None:
        while self._running:
            try:
                self.run_one_pass()
            except Exception as e:
                logger.error(f"Error in reconciliation daemon pass: {e}")
            await asyncio.sleep(self.poll_interval)
