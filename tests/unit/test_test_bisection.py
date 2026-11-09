"""Unit tests for TestBisectionManager."""

from unittest.mock import MagicMock
import pytest
from src.sandbox.docker_driver import ExecResult, SandboxManager
from src.sandbox.test_bisection import BaselineTestReport, TestBisectionManager


def test_baseline_clean_run():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="5 passed in 0.1s\n", stderr="", exit_code=0)

    mgr = TestBisectionManager(mock_sandbox, flaky_probes=1)
    rep = mgr.run_baseline("c1", "pytest")

    assert rep.passed is True
    assert len(rep.failed_test_names) == 0
    assert len(rep.flaky_test_names) == 0


def test_baseline_flaky_and_preexisting_isolation():
    mock_sandbox = MagicMock(spec=SandboxManager)
    # Run 1 fails test_a and test_b
    out1 = "FAILED tests/test_calc.py::test_a\nFAILED tests/test_calc.py::test_b\n"
    # Run 2 fails test_a only (test_b passed on probe -> test_b is flaky!)
    out2 = "FAILED tests/test_calc.py::test_a\n"

    mock_sandbox.exec_in_sandbox.side_effect = [
        ExecResult(stdout=out1, stderr="", exit_code=1),
        ExecResult(stdout=out2, stderr="", exit_code=1),
    ]

    mgr = TestBisectionManager(mock_sandbox, flaky_probes=2)
    rep = mgr.run_baseline("c1", "pytest")

    assert rep.passed is False
    assert "tests/test_calc.py::test_a" in rep.failed_test_names
    assert "tests/test_calc.py::test_b" in rep.flaky_test_names

    # Check regression isolation
    # 1. Failure of test_a (pre-existing) should NOT be flagged as new regression
    assert mgr.is_true_regression({"tests/test_calc.py::test_a"}, rep) is False
    # 2. Failure of new test_c SHOULD be flagged as a regression
    assert mgr.is_true_regression({"tests/test_calc.py::test_a", "tests/test_calc.py::test_c"}, rep) is True
