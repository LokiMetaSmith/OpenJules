"""Static analysis and linter guardrails running inside the sandbox workspace."""

import logging
from dataclasses import dataclass
from typing import List, Optional
from src.sandbox.docker_driver import ExecResult, SandboxManager

logger = logging.getLogger(__name__)


@dataclass
class LintDiagnostic:
    """Represents a single linter diagnostic issue."""

    message: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    severity: str = "warning"


@dataclass
class LintResult:
    """Outcome of static analysis check."""

    passed: bool
    command: str
    output: str
    exit_code: int
    diagnostics: List[LintDiagnostic]


class LinterGuardrail:
    """Executes static code analysis (ruff, flake8, mypy, eslint) and formats diagnostics."""

    def __init__(self, sandbox_manager: SandboxManager):
        self.sandbox_manager = sandbox_manager

    def run_linter(self, container_id: str, lint_command: str) -> LintResult:
        """Executes the linter command in the sandbox and parses results."""
        if not lint_command or not lint_command.strip():
            return LintResult(passed=True, command="", output="", exit_code=0, diagnostics=[])

        res = self.sandbox_manager.exec_in_sandbox(container_id, lint_command)
        passed = (res.exit_code == 0)
        output = (res.stdout + ("\n" + res.stderr if res.stderr else "")).strip()

        diagnostics = []
        for line in output.splitlines():
            line_str = line.strip()
            if not line_str:
                continue
            # Simple heuristic line format parsing (file:line:col: msg or file:line: msg)
            parts = line_str.split(":", 3)
            if len(parts) >= 3 and parts[1].strip().isdigit():
                file_path = parts[0].strip()
                line_no = int(parts[1].strip())
                msg = parts[2].strip() if len(parts) == 3 else parts[3].strip()
                diagnostics.append(LintDiagnostic(message=msg, file_path=file_path, line_number=line_no))
            else:
                diagnostics.append(LintDiagnostic(message=line_str))

        return LintResult(
            passed=passed,
            command=lint_command,
            output=output,
            exit_code=res.exit_code,
            diagnostics=diagnostics,
        )
