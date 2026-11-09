"""Ephemeral live preview server manager for frontend and web applications."""

import logging
import time
from dataclasses import dataclass
from typing import Dict, Optional
from src.sandbox.docker_driver import ExecResult, SandboxManager

logger = logging.getLogger(__name__)


@dataclass
class PreviewStatus:
    """Status of an active live preview server."""

    active: bool
    port: int
    url: str
    command: str
    container_id: str


class LivePreviewManager:
    """Spawns and manages ephemeral live preview dev servers inside sandbox containers."""

    def __init__(self, sandbox_manager: SandboxManager):
        self.sandbox_manager = sandbox_manager
        self.previews: Dict[str, PreviewStatus] = {}

    def start_preview(
        self,
        container_id: str,
        preview_command: str = "python3 -m http.server 3000",
        port: int = 3000,
        host_port: Optional[int] = None,
    ) -> PreviewStatus:
        """Starts a background development preview server inside the sandbox container."""
        # Wrap command in background execution
        bg_cmd = f"nohup {preview_command} > /tmp/preview.log 2>&1 &"
        res = self.sandbox_manager.exec_in_sandbox(container_id, bg_cmd)

        h_port = host_port or port
        preview_url = f"http://localhost:{h_port}"
        status = PreviewStatus(
            active=(res.exit_code == 0),
            port=port,
            url=preview_url,
            command=preview_command,
            container_id=container_id,
        )
        self.previews[container_id] = status
        logger.info(f"Live preview server spawned on {preview_url} for container {container_id}")
        return status

    def stop_preview(self, container_id: str) -> bool:
        """Kills any running preview process in the container."""
        if container_id in self.previews:
            port = self.previews[container_id].port
            kill_cmd = f"fuser -k {port}/tcp 2>/dev/null || pkill -f http.server 2>/dev/null || true"
            self.sandbox_manager.exec_in_sandbox(container_id, kill_cmd)
            self.previews[container_id].active = False
            del self.previews[container_id]
            return True
        return False

    def get_preview_status(self, container_id: str) -> Optional[PreviewStatus]:
        """Gets current preview status for container."""
        return self.previews.get(container_id)
