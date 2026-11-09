"""Unit tests for LinterGuardrail."""

from unittest.mock import MagicMock
import pytest

from src.agent.linter import LinterGuardrail
from src.sandbox.docker_driver import ExecResult, SandboxManager


def test_linter_guardrail_pass():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="All checks passed!\n", stderr="", exit_code=0)

    linter = LinterGuardrail(mock_sandbox)
    res = linter.run_linter("cont-test", "ruff check .")

    assert res.passed is True
    assert res.exit_code == 0
    assert len(res.diagnostics) == 1
    assert "All checks passed" in res.diagnostics[0].message


def test_linter_guardrail_diagnostic_parsing():
    mock_sandbox = MagicMock(spec=SandboxManager)
    linter_output = "src/main.py:12:5: F841 local variable 'x' is assigned to but never used\nsrc/app.py:4:1: E302 expected 2 blank lines"
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout=linter_output, stderr="", exit_code=1)

    linter = LinterGuardrail(mock_sandbox)
    res = linter.run_linter("cont-test", "flake8")

    assert res.passed is False
    assert res.exit_code == 1
    assert len(res.diagnostics) == 2
    assert res.diagnostics[0].file_path == "src/main.py"
    assert res.diagnostics[0].line_number == 12
    assert "assigned to but never used" in res.diagnostics[0].message
