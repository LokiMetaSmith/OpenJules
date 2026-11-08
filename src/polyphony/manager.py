"""Polyphony Swarm Manager managing repository-scoped swarm clients."""

from typing import Dict, Optional
from src.polyphony.client import PolyphonyClient, derive_repo_swarm_key


class PolyphonySwarmManager:
    """Manages active Polyphony swarms partitioned by repository."""

    def __init__(self, default_mqtt_host: Optional[str] = None):
        self.default_mqtt_host = default_mqtt_host
        self._clients: Dict[str, PolyphonyClient] = {}

    def get_or_create_client(
        self,
        repo_identifier: str,
        swarm_key_override: Optional[str] = None,
    ) -> PolyphonyClient:
        """Retrieves or instantiates the client subscribed to the repo's polyphony."""
        clean_id = repo_identifier.strip().lower()
        if clean_id not in self._clients:
            key = swarm_key_override or derive_repo_swarm_key(clean_id)
            client = PolyphonyClient(
                repo_identifier=clean_id,
                swarm_key=key,
                mqtt_host=self.default_mqtt_host,
            )
            self._clients[clean_id] = client
        return self._clients[clean_id]

    def remove_client(self, repo_identifier: str) -> None:
        clean_id = repo_identifier.strip().lower()
        if clean_id in self._clients:
            client = self._clients[clean_id]
            client.release_all_batons()
            client.set_status("idle")
            del self._clients[clean_id]
