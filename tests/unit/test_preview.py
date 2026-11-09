"""Unit tests for LivePreviewManager."""

from unittest.mock import MagicMock
import pytest
from src.sandbox.docker_driver import ExecResult, SandboxManager
from src.sandbox.preview import LivePreviewManager


def test_live_preview_lifecycle():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="", stderr="", exit_code=0)

    mgr = LivePreviewManager(mock_sandbox)
    status = mgr.start_preview("cont-preview-1", preview_command="npm run dev", port=5173)

    assert status.active is True
    assert status.port == 5173
    assert status.url == "http://localhost:5173"

    retrieved = mgr.get_preview_status("cont-preview-1")
    assert retrieved is not None
    assert retrieved.active is True

    stopped = mgr.stop_preview("cont-preview-1")
    assert stopped is True
    assert mgr.get_preview_status("cont-preview-1") is None
