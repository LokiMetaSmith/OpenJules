"""Keystone Polyphony client providing multi-agent coordination on the same repository.

Implements RTOS primitives:
- Liminal Space: Shared thoughts and status broadcasting
- Baton (Mutex): Resource and task locks preventing collisions
- Swarm Backlog: Pull-based task coordination and progress tracking
"""

import hashlib
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Set

logger = logging.getLogger(__name__)


def derive_repo_swarm_key(repo_identifier: str, salt: str = "keystone-polyphony-v1") -> str:
    """Derives a deterministic 64-character hex swarm key for a repository identifier."""
    clean_identifier = repo_identifier.strip().lower()
    raw = f"{salt}:{clean_identifier}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


class PolyphonyClient:
    """Client for Keystone Polyphony swarm coordination."""

    def __init__(
        self,
        repo_identifier: str,
        node_id: Optional[str] = None,
        swarm_key: Optional[str] = None,
        mqtt_host: Optional[str] = None,
        mqtt_port: int = 1883,
    ):
        self.repo_identifier = repo_identifier
        self.swarm_key = swarm_key or derive_repo_swarm_key(repo_identifier)
        self.topic = hashlib.sha256(self.swarm_key.encode("utf-8")).hexdigest()
        self.node_id = node_id or f"openjules-{os.urandom(4).hex()}"
        self.status = "idle"
        self.capabilities = ["coder", "sandbox_runner", "verifier"]

        # In-memory liminal state (synchronizable via transport)
        self.thoughts: Dict[str, Dict[str, Any]] = {}
        self.thought_stream: List[Dict[str, Any]] = []
        self.batons: Dict[str, str] = {}  # resource -> owner_id
        self.backlog: Dict[str, Dict[str, Any]] = {}  # task_id -> task_dict
        self.peers: Set[str] = set()

        self.mqtt_host = mqtt_host or os.environ.get("MQTT_HOST")
        self.mqtt_port = mqtt_port

    def share_thought(self, content: str, urgency: str = "low") -> Dict[str, Any]:
        """Broadcasts a thought to the repository's choir of agents."""
        thought_data = {
            "origin": self.node_id,
            "repo": self.repo_identifier,
            "content": content,
            "status": self.status,
            "capabilities": self.capabilities,
            "urgency": urgency,
            "timestamp": time.time(),
        }
        self.thoughts[self.node_id] = thought_data
        self.thought_stream.append(thought_data)
        logger.info(f"~~~ [Polyphony Swarm: {self.repo_identifier}] ({self.status}) {self.node_id}: {content}")
        return thought_data

    def set_status(self, new_status: str) -> None:
        """Updates and broadcasts the agent's status ('idle', 'busy', 'verifying')."""
        self.status = new_status
        self.share_thought(f"Status changed to {new_status}")

    def acquire_baton(self, resource: str, timeout: float = 5.0) -> bool:
        """Acquires an exclusive lock on a resource (e.g. 'task:123' or 'file:src/main.py')."""
        current_owner = self.batons.get(resource)
        if current_owner == self.node_id:
            return True

        if current_owner is None:
            self.batons[resource] = self.node_id
            self.share_thought(f"Acquired baton for {resource}", urgency="high")
            return True

        # Resource is currently locked by another node
        logger.warning(
            f"Baton for {resource} denied to {self.node_id}. Held by {current_owner}"
        )
        return False

    def release_baton(self, resource: str) -> bool:
        """Releases the lock on a resource if held by this node."""
        if self.batons.get(resource) == self.node_id:
            del self.batons[resource]
            self.share_thought(f"Released baton for {resource}", urgency="high")
            return True
        return False

    def sync_task_backlog(
        self,
        task_id: str | int,
        title: str,
        status: str = "in_progress",
        priority: str = "high",
    ) -> Dict[str, Any]:
        """Updates the shared swarm backlog task state."""
        tid = str(task_id)
        task_entry = {
            "id": tid,
            "title": title,
            "owner": self.node_id,
            "status": status,
            "priority": priority,
            "updated_at": time.time(),
        }
        self.backlog[tid] = task_entry
        self.share_thought(f"Backlog task #{tid} status -> {status}")
        return task_entry

    def release_all_batons(self, force: bool = False) -> List[str]:
        """Releases batons held by this node (or all batons if force=True)."""
        released = []
        for resource, owner in list(self.batons.items()):
            if owner == self.node_id or force:
                del self.batons[resource]
                released.append(resource)
        if released:
            self.share_thought(f"Cleaned up all held batons: {released}", urgency="high")
        return released
