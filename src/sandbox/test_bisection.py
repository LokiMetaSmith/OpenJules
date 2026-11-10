"""Autonomous flaky test detection and baseline test bisection.

Executes baseline verification before agent modifications to detect pre-existing test failures,
probes for flaky tests across repeat runs, and isolates true regressions introduced by code changes.
"""

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Set
from src.sandbox.docker_driver import ExecResult, SandboxManager

logger = logging.getLogger(__name__)


@dataclass
class BaselineTestReport:
    """Report of baseline test suite health prior to agent edits."""

    exit_code: int
    passed: bool
    failed_test_names: Set[str] = field(default_factory=set)
    flaky_test_names: Set[str] = field(default_factory=set)
    raw_output: str = ""


class TestBisectionManager:
    """Manages baseline pre-flight verification and flaky test isolation."""

    __test__ = False  # Prevent pytest from treating this operational class as a test case

    def __init__(self, sandbox_manager: SandboxManager, flaky_probes: int = 2):
        self.sandbox_manager = sandbox_manager
        self.flaky_probes = flaky_probes

    @staticmethod
    def parse_failed_tests(output: str) -> Set[str]:
        """Extracts failed test identifiers from pytest or standard test output."""
        failed = set()
        for line in output.splitlines():
            line_str = line.strip()
            if line_str.startswith("FAILED ") and "::" in line_str:
                test_id = line_str.split()[1]
                failed.add(test_id)
            elif "FAIL:" in line_str:
                failed.add(line_str.split("FAIL:")[1].strip())
        return failed

    def run_baseline(
        self,
        container_id: str,
        test_command: str = "pytest",
    ) -> BaselineTestReport:
        """Runs baseline verification and isolates pre-existing vs flaky test failures."""
        if not test_command or not test_command.strip():
            return BaselineTestReport(exit_code=0, passed=True)

        res1 = self.sandbox_manager.exec_in_sandbox(container_id, test_command)
        failed1 = self.parse_failed_tests(res1.stdout + "\n" + res1.stderr)

        flaky = set()
        if not (res1.exit_code == 0) and self.flaky_probes > 1:
            # Repeat run to probe for flakiness
            res2 = self.sandbox_manager.exec_in_sandbox(container_id, test_command)
            failed2 = self.parse_failed_tests(res2.stdout + "\n" + res2.stderr)
            # Flaky tests are tests whose outcome differs across consecutive runs
            flaky = failed1.symmetric_difference(failed2)

        report = BaselineTestReport(
            exit_code=res1.exit_code,
            passed=(res1.exit_code == 0),
            failed_test_names=failed1 - flaky,
            flaky_test_names=flaky,
            raw_output=res1.stdout,
        )
        logger.info(
            f"Baseline test run complete: passed={report.passed}, "
            f"pre-existing failures={len(report.failed_test_names)}, flaky={len(report.flaky_test_names)}"
        )
        return report

    def is_true_regression(
        self,
        current_failed_tests: Set[str],
        baseline_report: BaselineTestReport,
    ) -> bool:
        """Determines if current test failures represent new regressions or pre-existing/flaky failures."""
        new_failures = current_failed_tests - baseline_report.failed_test_names - baseline_report.flaky_test_names
        return len(new_failures) > 0
