"""Hierarchical AGENTS.md rule engine.

Discovers, parses, and enforces scoped guidelines from AGENTS.md files across a repository workspace.
Nested AGENTS.md files take precedence over parent directories.
"""

import os
from pathlib import Path
from typing import Dict, List, Optional
from pydantic import BaseModel, Field


class AgentRuleScope(BaseModel):
    """Scoped guidelines defined in an AGENTS.md file."""

    directory: str
    content: str
    programmatic_checks: List[str] = Field(default_factory=list)


class AgentsRuleEngine:
    """Discovers, scopes, and formats AGENTS.md instructions."""

    @staticmethod
    def extract_programmatic_checks(content: str) -> List[str]:
        """Extracts shell commands marked for programmatic verification (e.g. `pytest`, `npm test`)."""
        checks = []
        for line in content.splitlines():
            line_str = line.strip()
            if line_str.startswith("- `") and line_str.endswith("`"):
                cmd = line_str[3:-1].strip()
                if cmd:
                    checks.append(cmd)
            elif "run:" in line_str.lower() and "`" in line_str:
                parts = line_str.split("`")
                if len(parts) >= 3:
                    checks.append(parts[1].strip())
        return checks

    def discover_rules(self, workspace_dir: str) -> List[AgentRuleScope]:
        """Walks the workspace directory discovering all AGENTS.md files."""
        scopes: List[AgentRuleScope] = []
        if not os.path.isdir(workspace_dir):
            return scopes

        for root, dirs, files in os.walk(workspace_dir):
            # Exclude version control and build folders
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", ".pytest_cache", "__pycache__")]
            for file in files:
                if file.upper() == "AGENTS.MD":
                    full_path = os.path.join(root, file)
                    rel_dir = os.path.relpath(root, workspace_dir)
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as f:
                            text = f.read().strip()
                        checks = self.extract_programmatic_checks(text)
                        scopes.append(AgentRuleScope(directory=rel_dir, content=text, programmatic_checks=checks))
                    except Exception:
                        pass

        # Sort scopes: root first ('.'), then nested directories deeper down
        scopes.sort(key=lambda s: (0 if s.directory == "." else s.directory.count(os.sep) + 1))
        return scopes

    def get_prompt_context(self, workspace_dir: str, target_file: Optional[str] = None) -> str:
        """Formats the applicable AGENTS.md rules for system prompt context."""
        scopes = self.discover_rules(workspace_dir)
        if not scopes:
            return ""

        context_lines = ["### Repository AGENTS.md Rules:"]
        for scope in scopes:
            prefix = "Root Guidelines" if scope.directory == "." else f"Scoped Guidelines for `{scope.directory}/`"
            context_lines.append(f"\n#### {prefix}:")
            context_lines.append(scope.content)
            if scope.programmatic_checks:
                context_lines.append(f"Required Programmatic Checks: {', '.join(scope.programmatic_checks)}")

        return "\n".join(context_lines)
