"""Docker sandbox runtime and container management for OpenJules."""

import logging
import os
import shutil
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Dict, Generator, Optional
from urllib.parse import urlparse, urlunparse

import docker
from docker.errors import DockerException, NotFound

from src.sandbox.config import OpenJulesConfig

logger = logging.getLogger(__name__)


@dataclass
class ExecResult:
    """Result of command execution within a container."""

    stdout: str
    stderr: str
    exit_code: int


@dataclass
class SandboxContext:
    """Context information for an active sandbox container."""

    container_id: str
    workspace_dir: str
    repo_url: str
    branch: str
    config: OpenJulesConfig


class SandboxManager:
    """Manages container lifecycle, command execution, and workspace teardown."""

    def __init__(self, docker_client: Optional[docker.DockerClient] = None):
        if docker_client is not None:
            self.client = docker_client
        else:
            docker_host = os.environ.get("DOCKER_HOST")
            if docker_host:
                self.client = docker.DockerClient(base_url=docker_host)
            else:
                self.client = docker.from_env()

    @staticmethod
    def _inject_git_token(repo_url: str, git_token: Optional[str]) -> str:
        """Inject GitHub token into HTTPS repo URL if provided."""
        if not git_token or not repo_url.startswith("https://"):
            return repo_url
        parsed = urlparse(repo_url)
        # format: https://x-access-token:<token>@github.com/...
        netloc = f"x-access-token:{git_token}@{parsed.hostname}"
        if parsed.port:
            netloc += f":{parsed.port}"
        return urlunparse(
            (parsed.scheme, netloc, parsed.path, parsed.params, parsed.query, parsed.fragment)
        )

    def spawn_sandbox(
        self,
        repo_url: str,
        branch: str,
        config: OpenJulesConfig,
        git_token: Optional[str] = None,
        workspace_base_dir: Optional[str] = None,
    ) -> SandboxContext:
        """Provisions a container with resource constraints, mounts workspace, and clones repo.

        Resource constraints: mem_limit="4g", nano_cpus=2000000000 (2 CPUs).
        """
        temp_dir = tempfile.mkdtemp(prefix="openjules_ws_", dir=workspace_base_dir)

        # Build environment dictionary
        env = dict(config.env)
        env["DEBIAN_FRONTEND"] = "noninteractive"
        env["PYTHONUNBUFFERED"] = "1"

        try:
            # Mount host temp_dir to /workspace inside container
            volumes = {
                temp_dir: {
                    "bind": "/workspace",
                    "mode": "rw",
                }
            }

            container = self.client.containers.run(
                image=config.image,
                command="tail -f /dev/null",  # Keep container running
                detach=True,
                volumes=volumes,
                working_dir="/workspace",
                mem_limit="4g",
                nano_cpus=2000000000,
                environment=env,
                network_mode="bridge",
            )
            container_id = container.id

            context = SandboxContext(
                container_id=container_id,
                workspace_dir=temp_dir,
                repo_url=repo_url,
                branch=branch,
                config=config,
            )

            # Clone the repository
            auth_repo_url = self._inject_git_token(repo_url, git_token)
            clone_cmd = f"git clone --branch {branch} --depth 1 {auth_repo_url} /workspace"
            res = self.exec_in_sandbox(container_id, clone_cmd, timeout=config.timeout_seconds)
            if res.exit_code != 0:
                # If branch didn't exist or single-branch failed, try full clone or fallback
                fallback_cmd = f"git clone {auth_repo_url} /workspace"
                res_fallback = self.exec_in_sandbox(
                    container_id, fallback_cmd, timeout=config.timeout_seconds
                )
                if res_fallback.exit_code != 0:
                    raise RuntimeError(
                        f"Failed to clone repository: {res.stderr or res_fallback.stderr}"
                    )
                # Check out the branch or create it if missing
                self.exec_in_sandbox(
                    container_id,
                    f"git checkout {branch} 2>/dev/null || git checkout -b {branch}",
                    timeout=30,
                )

            # Execute setup commands defined in .openjules.yaml
            for setup_cmd in config.setup:
                setup_res = self.exec_in_sandbox(
                    container_id, setup_cmd, timeout=config.timeout_seconds
                )
                if setup_res.exit_code != 0:
                    logger.warning(
                        f"Setup command '{setup_cmd}' failed with code {setup_res.exit_code}: {setup_res.stderr}"
                    )

            return context

        except Exception as e:
            # Guarantee cleanup on failure during spawn
            if "container" in locals():
                self.cleanup_sandbox(container.id, temp_dir)
            else:
                shutil.rmtree(temp_dir, ignore_errors=True)
            raise e

    def exec_in_sandbox(
        self,
        container_id: str,
        cmd: str,
        timeout: int = 120,
        workdir: str = "/workspace",
        env: Optional[Dict[str, str]] = None,
    ) -> ExecResult:
        """Executes a command inside the container with timeout and captures output."""
        try:
            container = self.client.containers.get(container_id)
        except NotFound as e:
            raise RuntimeError(f"Container {container_id} not found") from e

        # Wrap execution in bash -c or sh -c for proper pipe and command chaining
        wrapped_cmd = ["/bin/sh", "-c", cmd]
        exec_instance = self.client.api.exec_create(
            container=container_id,
            cmd=wrapped_cmd,
            workdir=workdir,
            environment=env,
            stdout=True,
            stderr=True,
        )
        exec_id = exec_instance["Id"]

        # Run with streaming or demux output
        output_stream = self.client.api.exec_start(exec_id, stream=False, demux=True)
        inspect_res = self.client.api.exec_inspect(exec_id)

        stdout_bytes, stderr_bytes = (b"", b"")
        if isinstance(output_stream, tuple):
            stdout_bytes, stderr_bytes = output_stream
        elif isinstance(output_stream, bytes):
            stdout_bytes = output_stream

        stdout_str = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
        stderr_str = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""
        exit_code = inspect_res.get("ExitCode", 0) or 0

        return ExecResult(
            stdout=stdout_str,
            stderr=stderr_str,
            exit_code=exit_code,
        )

    def cleanup_sandbox(self, container_id: str, workspace_dir: Optional[str] = None):
        """Guarantees complete container and volume teardown even on fatal errors."""
        try:
            container = self.client.containers.get(container_id)
            container.stop(timeout=2)
            container.remove(force=True, v=True)
        except (NotFound, DockerException, Exception) as e:
            logger.debug(f"Exception while cleaning container {container_id}: {e}")

        if workspace_dir and os.path.exists(workspace_dir):
            try:
                shutil.rmtree(workspace_dir, ignore_errors=True)
            except Exception as e:
                logger.debug(f"Exception cleaning workspace dir {workspace_dir}: {e}")

    @contextmanager
    def run_sandbox(
        self,
        repo_url: str,
        branch: str,
        config: OpenJulesConfig,
        git_token: Optional[str] = None,
        workspace_base_dir: Optional[str] = None,
    ) -> Generator[SandboxContext, None, None]:
        """Context manager guaranteeing strict try...finally teardown."""
        context = self.spawn_sandbox(
            repo_url=repo_url,
            branch=branch,
            config=config,
            git_token=git_token,
            workspace_base_dir=workspace_base_dir,
        )
        try:
            yield context
        finally:
            self.cleanup_sandbox(context.container_id, context.workspace_dir)
