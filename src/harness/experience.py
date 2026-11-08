"""Experience and learned skills manager (EXPERIENCE.md).

Manages reading, formatting, and appending compounded capabilities across tasks.
"""

import datetime
import logging
import os
from typing import List, Optional

logger = logging.getLogger(__name__)

DEFAULT_HEADER = """# Project Experience & Compounded Skills Library

Accumulated learnings, guardrails, and project-specific patterns discovered by OpenJules across tasks.
This file is autonomously updated by the adaptive agent harness to accelerate future work.

---
"""


class ExperienceManager:
    """Reads, extracts, and records learnings in EXPERIENCE.md."""

    def __init__(self, experience_path: str = "EXPERIENCE.md"):
        self.experience_path = experience_path

    def load_experience(self, workspace_dir: str) -> str:
        """Reads existing experience file from workspace if present."""
        full_path = os.path.join(workspace_dir, self.experience_path)
        if os.path.isfile(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    return f.read().strip()
            except Exception as e:
                logger.warning(f"Failed to read experience file {full_path}: {e}")
        return ""

    def append_experience(
        self,
        workspace_dir: str,
        task_title: str,
        problem_summary: str,
        solution_pattern: str,
        discovered_rules: List[str],
    ) -> str:
        """Appends a new experience entry to EXPERIENCE.md in the workspace."""
        full_path = os.path.join(workspace_dir, self.experience_path)
        timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

        rules_formatted = "\n".join(f"- {rule}" for rule in discovered_rules) if discovered_rules else "- Standard verification practices apply."

        entry = (
            f"\n### [{timestamp}] Task: {task_title}\n\n"
            f"**Problem:** {problem_summary}\n\n"
            f"**Strategy & Solution:** {solution_pattern}\n\n"
            f"**Compounded Rules / Guardrails:**\n"
            f"{rules_formatted}\n\n"
            f"---\n"
        )

        content = DEFAULT_HEADER
        if os.path.isfile(full_path):
            try:
                with open(full_path, "r", encoding="utf-8") as f:
                    content = f.read()
            except Exception:
                pass

        content = content.rstrip() + "\n" + entry

        os.makedirs(os.path.dirname(os.path.abspath(full_path)), exist_ok=True)
        with open(full_path, "w", encoding="utf-8") as f:
            f.write(content)

        logger.info(f"Appended new learnings to {full_path}")
        return full_path
