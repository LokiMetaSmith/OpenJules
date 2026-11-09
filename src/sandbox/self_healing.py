"""Self-healing environment and dynamic dependency resolver.

Detects missing system packages, C/C++ compilation headers, and python build prerequisites
from failed setup command logs and applies targeted self-healing installations inside the container.
"""

import logging
import re
from typing import Dict, List, Optional
from src.sandbox.docker_driver import ExecResult, SandboxManager

logger = logging.getLogger(__name__)

# Heuristic patterns mapping common error signatures to system packages
PACKAGE_HEALING_RULES: Dict[str, str] = {
    r"fatal error: (Python\.h|python\.h)": "python3-dev",
    r"fatal error: (ffi\.h|ffi/ffi\.h)": "libffi-dev",
    r"fatal error: (openssl/ssl\.h|openssl/crypto\.h)": "libssl-dev",
    r"fatal error: (pq-fe\.h)": "libpq-dev",
    r"fatal error: (sqlite3\.h)": "libsqlite3-dev",
    r"error: command '(gcc|clang|cc)' failed": "build-essential",
    r"cargo: not found|Rust.*is required": "cargo rustc",
    r"cmake: not found": "cmake",
    r"pkg-config: not found": "pkg-config",
    r"git: command not found": "git",
    r"curl: command not found": "curl",
}


class EnvironmentSelfHealer:
    """Detects and repairs broken sandbox setup dependencies autonomously."""

    def __init__(self, sandbox_manager: SandboxManager):
        self.sandbox_manager = sandbox_manager

    def diagnose_missing_packages(self, error_output: str) -> List[str]:
        """Scans command stderr/stdout against diagnostic healing rules."""
        missing = []
        for pattern, package in PACKAGE_HEALING_RULES.items():
            if re.search(pattern, error_output, re.IGNORECASE):
                if package not in missing:
                    missing.append(package)
        return missing

    def heal_environment(
        self,
        container_id: str,
        failed_command: str,
        error_output: str,
    ) -> bool:
        """Applies dynamic system packages and re-attempts the failed command."""
        missing = self.diagnose_missing_packages(error_output)
        if not missing:
            logger.info("SelfHealer: No known system package remedies identified for error.")
            return False

        packages_str = " ".join(missing)
        logger.info(f"SelfHealer: Attempting dynamic installation of missing packages: {packages_str}")

        # Execute dynamic installation inside the container
        install_cmd = (
            f"apt-get update && "
            f"DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends {packages_str} && "
            f"rm -rf /var/lib/apt/lists/*"
        )
        install_res = self.sandbox_manager.exec_in_sandbox(container_id, install_cmd, timeout=300)
        if install_res.exit_code != 0:
            logger.warning(f"SelfHealer: Package installation failed: {install_res.stderr or install_res.stdout}")
            return False

        # Re-attempt original setup command
        logger.info(f"SelfHealer: Retrying failed command: '{failed_command}'")
        retry_res = self.sandbox_manager.exec_in_sandbox(container_id, failed_command, timeout=300)
        return retry_res.exit_code == 0
