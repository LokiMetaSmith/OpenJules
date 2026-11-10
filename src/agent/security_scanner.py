"""Security scanner and secret leak prevention guardrail."""

import logging
import re
from dataclasses import dataclass, field
from typing import List, Optional

logger = logging.getLogger(__name__)

SECRET_PATTERNS = [
    (r"-----BEGIN (RSA |EC |DSA |OPENSSH )?PRIVATE KEY-----", "Private key block detected"),
    (r"(ghp_[a-zA-Z0-9]{36}|github_pat_[a-zA-Z0-9_]{82})", "GitHub personal access token detected"),
    (r"sk-[a-zA-Z0-9]{20,48}", "OpenAI API secret key detected"),
    (r"sk-ant-[a-zA-Z0-9]{20,48}", "Anthropic API secret key detected"),
    (r"(AWS|AKIA)[0-9A-Z]{16}", "AWS access key detected"),
    (r"(eyJ[a-zA-Z0-9_-]{10,}\.eyJ[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,})", "JWT token detected"),
]

INSECURE_CODE_PATTERNS = [
    (r"\beval\(", "Insecure use of eval() detected"),
    (r"\bexec\(", "Insecure use of exec() detected"),
    (r"subprocess\.(run|Popen|call)\(.*shell\s*=\s*True", "Dangerous subprocess shell=True execution"),
    (r"(SELECT|INSERT|UPDATE|DELETE).*%s", "Potential raw SQL format string injection"),
]


@dataclass
class SecurityFinding:
    category: str
    description: str
    line_content: str
    file_path: Optional[str] = None
    line_number: Optional[int] = None
    severity: str = "critical"


@dataclass
class SecurityScanReport:
    passed: bool
    findings: List[SecurityFinding] = field(default_factory=list)


class SecurityScanner:
    """Scans code changes and diffs to prevent secrets or vulnerabilities from being committed."""

    def scan_diff(self, diff_text: str) -> SecurityScanReport:
        findings = []
        current_file = None
        line_num = 0

        for line in diff_text.splitlines():
            if line.startswith("+++ b/"):
                current_file = line[6:].strip()
                line_num = 0
            elif line.startswith("@@"):
                line_num += 1
            elif line.startswith("+") and not line.startswith("+++"):
                line_num += 1
                added_content = line[1:].strip()

                for pattern, desc in SECRET_PATTERNS:
                    if re.search(pattern, added_content):
                        findings.append(
                            SecurityFinding(
                                category="secret_leak",
                                description=desc,
                                line_content=added_content[:60],
                                file_path=current_file,
                                line_number=line_num,
                                severity="critical",
                            )
                        )

                for pattern, desc in INSECURE_CODE_PATTERNS:
                    if re.search(pattern, added_content):
                        findings.append(
                            SecurityFinding(
                                category="vulnerability",
                                description=desc,
                                line_content=added_content[:60],
                                file_path=current_file,
                                line_number=line_num,
                                severity="high",
                            )
                        )

        return SecurityScanReport(passed=(len(findings) == 0), findings=findings)
