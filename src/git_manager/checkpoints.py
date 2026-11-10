"""Git tree checkpointing and time-travel rollback manager.

Enables ephemeral snapshots of workspace states before risky edits or during long ReAct loops,
allowing the agent or harness to instantly rewind to the last known good state.
"""

import logging
import time
from dataclasses import dataclass
from typing import Dict, List, Optional
from src.sandbox.docker_driver import ExecResult, SandboxManager

logger = logging.getLogger(__name__)


@dataclass
class Checkpoint:
    """Represents a snapshot of working tree state."""

    name: str
    commit_or_tree_sha: str
    timestamp: float
    description: str


class GitCheckpointManager:
    """Manages ephemeral Git checkpoints and rollbacks inside the sandbox workspace."""

    def __init__(self, sandbox_manager: SandboxManager):
        self.sandbox_manager = sandbox_manager
        self.checkpoints: Dict[str, Dict[str, Checkpoint]] = {}  # container_id -> {name: Checkpoint}

    def create_checkpoint(
        self,
        container_id: str,
        name: str,
        description: str = "",
    ) -> Optional[Checkpoint]:
        """Creates an ephemeral checkpoint by committing or writing a tree object."""
        safe_desc = description.replace("'", "'\\''")
        # Write current working tree and index to a tree object without moving HEAD
        cmd = (
            f"git add -A && "
            f"TREE_SHA=$(git write-tree) && "
            f"PARENT_SHA=$(git rev-parse HEAD 2>/dev/null || echo '') && "
            f"if [ -n \"$PARENT_SHA\" ]; then "
            f"  COMMIT_SHA=$(git commit-tree $TREE_SHA -p $PARENT_SHA -m 'checkpoint: {safe_desc}'); "
            f"else "
            f"  COMMIT_SHA=$(git commit-tree $TREE_SHA -m 'checkpoint: {safe_desc}'); "
            f"fi && "
            f"echo $COMMIT_SHA"
        )
        res = self.sandbox_manager.exec_in_sandbox(container_id, cmd)
        if res.exit_code != 0 or not res.stdout.strip():
            logger.warning(f"Failed to create checkpoint '{name}': {res.stderr or res.stdout}")
            return None

        sha = res.stdout.strip().splitlines()[-1]
        cp = Checkpoint(
            name=name,
            commit_or_tree_sha=sha,
            timestamp=time.time(),
            description=description,
        )
        if container_id not in self.checkpoints:
            self.checkpoints[container_id] = {}
        self.checkpoints[container_id][name] = cp
        logger.info(f"Created checkpoint '{name}' ({sha[:8]}) in container {container_id}")
        return cp

    def rollback_to_checkpoint(self, container_id: str, name: str) -> bool:
        """Rolls back the workspace working tree to the specified checkpoint."""
        if container_id not in self.checkpoints or name not in self.checkpoints[container_id]:
            logger.warning(f"Checkpoint '{name}' not found for container {container_id}")
            return False

        cp = self.checkpoints[container_id][name]
        # Checkout files from the checkpoint commit without detaching branch HEAD
        cmd = f"git read-tree -u --reset {cp.commit_or_tree_sha}"
        res = self.sandbox_manager.exec_in_sandbox(container_id, cmd)
        if res.exit_code != 0:
            logger.error(f"Rollback to checkpoint '{name}' failed: {res.stderr or res.stdout}")
            return False

        logger.info(f"Successfully rolled back to checkpoint '{name}' ({cp.commit_or_tree_sha[:8]})")
        return True

    def list_checkpoints(self, container_id: str) -> List[Checkpoint]:
        """Lists all active checkpoints for the container."""
        if container_id in self.checkpoints:
            return list(self.checkpoints[container_id].values())
        return []
