"""Git lifecycle and branch management inside the sandbox workspace."""

import logging
import re
from dataclasses import dataclass
from typing import Optional
from src.sandbox.docker_driver import SandboxContext, SandboxManager

logger = logging.getLogger(__name__)


@dataclass
class GitDiffSummary:
    """Contains git diff and generated PR summary details."""

    branch_name: str
    diff: str
    summary: str
    commit_sha: Optional[str] = None


class GitLifecycleManager:
    """Manages branches, commits, pushes, and diff generation within sandbox workspace."""

    def __init__(self, sandbox_manager: SandboxManager):
        self.sandbox_manager = sandbox_manager

    @staticmethod
    def slugify(text: str) -> str:
        """Slugify a string for branch naming."""
        text = text.lower()
        text = re.sub(r"[^a-z0-9]+", "-", text)
        return text.strip("-")[:30]

    def create_task_branch(
        self,
        context: SandboxContext,
        issue_id: int | str,
        title: str,
    ) -> str:
        """Creates and checks out branch: openjules/task-{issue_id}-{slug}."""
        slug = self.slugify(title)
        branch_name = f"openjules/task-{issue_id}-{slug}"

        cmd = (
            f"git config user.name 'OpenJules[bot]' && "
            f"git config user.email 'openjules-bot@users.noreply.github.com' && "
            f"git checkout -b {branch_name}"
        )
        res = self.sandbox_manager.exec_in_sandbox(context.container_id, cmd)
        if res.exit_code != 0:
            raise RuntimeError(f"Failed to create branch {branch_name}: {res.stderr or res.stdout}")
        return branch_name

    def commit_changes(
        self,
        context: SandboxContext,
        commit_message: str,
    ) -> Optional[str]:
        """Stages all changes and commits with conventional commit message."""
        # Sanitize commit message for shell
        safe_msg = commit_message.replace("'", "'\\''")
        cmd = (
            f"git add -A && "
            f"git diff --cached --quiet || "
            f"(git commit -m '{safe_msg}' && git rev-parse HEAD)"
        )
        res = self.sandbox_manager.exec_in_sandbox(context.container_id, cmd)
        if res.exit_code != 0:
            raise RuntimeError(f"Failed to commit changes: {res.stderr or res.stdout}")

        # Extract commit sha if newly committed
        lines = res.stdout.strip().splitlines()
        if lines:
            last_line = lines[-1].strip()
            if len(last_line) == 40 and all(c in "0123456789abcdef" for c in last_line):
                return last_line
        return None

    def push_branch(
        self,
        context: SandboxContext,
        branch_name: str,
        force: bool = False,
    ) -> bool:
        """Pushes branch to remote origin using injected credentials."""
        force_flag = "--force" if force else ""
        cmd = f"git push {force_flag} -u origin {branch_name}"
        res = self.sandbox_manager.exec_in_sandbox(context.container_id, cmd)
        if res.exit_code != 0:
            raise RuntimeError(f"Failed to push branch {branch_name}: {res.stderr or res.stdout}")
        return True

    def get_diff(self, context: SandboxContext, base_branch: str = "main") -> str:
        """Runs git diff base_branch...HEAD inside workspace."""
        cmd = f"git diff origin/{base_branch}...HEAD 2>/dev/null || git diff {base_branch}...HEAD 2>/dev/null || git diff HEAD~1"
        res = self.sandbox_manager.exec_in_sandbox(context.container_id, cmd)
        return res.stdout

    def generate_pr_summary(
        self,
        context: SandboxContext,
        issue_id: int | str,
        issue_title: str,
        branch_name: str,
        test_output: Optional[str] = None,
        base_branch: str = "main",
    ) -> GitDiffSummary:
        """Summarizes changes and test outputs for PR description."""
        diff = self.get_diff(context, base_branch=base_branch)

        # Build clean markdown PR summary
        summary_lines = [
            f"## Summary of Changes for Issue #{issue_id}: {issue_title}",
            "",
            "### Autonomous Work by OpenJules",
            f"- **Branch:** `{branch_name}`",
            f"- **Verification Target:** `{context.config.test_command}`",
            "",
            "### Verification Status",
            "```text",
            test_output if test_output else "All automated tests verified successfully.",
            "```",
            "",
            f"Fixes #{issue_id}",
        ]
        summary_body = "\n".join(summary_lines)

        return GitDiffSummary(
            branch_name=branch_name,
            diff=diff,
            summary=summary_body,
        )
