"""Unit tests for EnvironmentSelfHealer."""

from unittest.mock import MagicMock
import pytest
from src.sandbox.docker_driver import ExecResult, SandboxManager
from src.sandbox.self_healing import EnvironmentSelfHealer


def test_diagnose_missing_packages():
    mock_sandbox = MagicMock(spec=SandboxManager)
    healer = EnvironmentSelfHealer(mock_sandbox)

    err1 = "fatal error: Python.h: No such file or directory"
    assert healer.diagnose_missing_packages(err1) == ["python3-dev"]

    err2 = "error: command 'gcc' failed: No such file or directory"
    assert healer.diagnose_missing_packages(err2) == ["build-essential"]

    err3 = "fatal error: pq-fe.h: No such file or directory\nfatal error: openssl/ssl.h: No such file"
    diagnostics = healer.diagnose_missing_packages(err3)
    assert "libpq-dev" in diagnostics
    assert "libssl-dev" in diagnostics


def test_heal_environment_success():
    mock_sandbox = MagicMock(spec=SandboxManager)
    # 1. apt-get install succeeds (exit 0)
    # 2. retry of pip install succeeds (exit 0)
    mock_sandbox.exec_in_sandbox.side_effect = [
        ExecResult(stdout="Installed python3-dev", stderr="", exit_code=0),
        ExecResult(stdout="Successfully installed package", stderr="", exit_code=0),
    ]

    healer = EnvironmentSelfHealer(mock_sandbox)
    success = healer.heal_environment(
        container_id="c1",
        failed_command="pip install -e .",
        error_output="fatal error: Python.h: No such file or directory",
    )
    assert success is True
    assert mock_sandbox.exec_in_sandbox.call_count == 2
