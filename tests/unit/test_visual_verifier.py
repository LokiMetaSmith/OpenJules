"""Unit tests for VisualVerifier."""

from unittest.mock import MagicMock
import pytest
from src.agent.visual_verifier import VisualVerifier
from src.sandbox.docker_driver import ExecResult, SandboxManager


def test_visual_verifier_capture_screenshot_success():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="SCREENSHOT_OK\n", stderr="", exit_code=0)

    verifier = VisualVerifier(mock_sandbox)
    path = verifier.capture_screenshot("c1", target_url="http://localhost:3000")
    assert path == "/tmp/verification.png"


def test_visual_verifier_verify_ui_changes():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="SCREENSHOT_OK\n", stderr="", exit_code=0)

    verifier = VisualVerifier(mock_sandbox)
    result = verifier.verify_ui_changes("c1", target_url="http://localhost:3000")
    assert result.success is True
    assert len(result.screenshots) == 1
    assert "Visual verification passed" in result.report_summary
