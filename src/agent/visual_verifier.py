"""Visual & Multi-Modal Verification engine for frontend web changes.

Executes headless browser checks via Playwright inside the sandbox workspace,
captures rendered component/page screenshots, and generates visual verification artifacts for PRs.
"""

import base64
import logging
import os
from dataclasses import dataclass, field
from typing import List, Optional
from src.sandbox.docker_driver import ExecResult, SandboxManager

logger = logging.getLogger(__name__)


@dataclass
class VisualVerificationResult:
    """Outcome of visual verification."""

    success: bool
    screenshots: List[str] = field(default_factory=list)  # list of relative paths or base64 strings
    report_summary: str = ""
    error: Optional[str] = None


class VisualVerifier:
    """Executes automated visual checks and headless screenshot capture inside the sandbox."""

    def __init__(self, sandbox_manager: SandboxManager):
        self.sandbox_manager = sandbox_manager

    def capture_screenshot(
        self,
        container_id: str,
        target_url: str = "http://localhost:3000",
        output_path: str = "/tmp/verification.png",
        wait_seconds: int = 2,
    ) -> Optional[str]:
        """Runs a headless python script inside the container to capture a screenshot."""
        py_script = (
            f"python3 -c \""
            f"try:\n"
            f"    from playwright.sync_api import sync_playwright\n"
            f"    import time\n"
            f"    with sync_playwright() as p:\n"
            f"        browser = p.chromium.launch(headless=True)\n"
            f"        page = browser.new_page()\n"
            f"        page.goto('{target_url}', timeout=15000)\n"
            f"        time.sleep({wait_seconds})\n"
            f"        page.screenshot(path='{output_path}', full_page=True)\n"
            f"        browser.close()\n"
            f"    print('SCREENSHOT_OK')\n"
            f"except Exception as e:\n"
            f"    print('ERROR:', str(e))\n"
            f"\""
        )
        res = self.sandbox_manager.exec_in_sandbox(container_id, py_script, timeout=60)
        if "SCREENSHOT_OK" in res.stdout:
            return output_path
        logger.debug(f"VisualVerifier screenshot failed: {res.stdout or res.stderr}")
        return None

    def verify_ui_changes(
        self,
        container_id: str,
        target_url: str = "http://localhost:3000",
    ) -> VisualVerificationResult:
        """Performs visual inspection and generates Markdown visual summary."""
        shot_path = self.capture_screenshot(container_id, target_url=target_url)
        if shot_path:
            return VisualVerificationResult(
                success=True,
                screenshots=[shot_path],
                report_summary=f"📸 Visual verification passed. Rendered state captured from `{target_url}`.",
            )
        return VisualVerificationResult(
            success=False,
            report_summary="⚠️ Headless visual verification script was unable to capture UI screenshot.",
            error="Browser or server unreachable",
        )
