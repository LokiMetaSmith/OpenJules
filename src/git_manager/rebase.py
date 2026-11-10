"""Semantic rebase and merge conflict resolver."""

import logging
import re
from dataclasses import dataclass
from typing import Any, List
from src.sandbox.docker_driver import SandboxManager

logger = logging.getLogger(__name__)


@dataclass
class ConflictBlock:
    file_path: str
    ours_code: str
    theirs_code: str
    raw_block: str


class SemanticRebaseManager:
    """Manages rebasing and resolving merge conflicts semantically."""

    def __init__(self, sandbox_manager: SandboxManager, llm_client: Any, model_name: str = "gpt-4o"):
        self.sandbox_manager = sandbox_manager
        self.llm_client = llm_client
        self.model_name = model_name

    def parse_conflicts(self, conflict_file_text: str, file_path: str) -> List[ConflictBlock]:
        pattern = re.compile(r"<<<<<<< [^\n]*\n(.*?)\n=======\n(.*?)\n>>>>>>> [^\n]*", re.DOTALL)
        blocks = []
        for match in pattern.finditer(conflict_file_text):
            blocks.append(
                ConflictBlock(
                    file_path=file_path,
                    ours_code=match.group(1),
                    theirs_code=match.group(2),
                    raw_block=match.group(0),
                )
            )
        return blocks

    def resolve_conflict_block(self, conflict: ConflictBlock) -> str:
        prompt = (
            f"Resolve this 3-way git conflict in `{conflict.file_path}`:\n"
            f"[OURS]:\n{conflict.ours_code}\n"
            f"[THEIRS]:\n{conflict.theirs_code}\n\n"
            f"Return ONLY the merged replacement code block."
        )
        try:
            resp = self.llm_client.completion(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You resolve git conflicts cleanly."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            resolved = resp.choices[0].message.content or conflict.theirs_code
            if "```" in resolved:
                lines = resolved.splitlines()
                if lines[0].startswith("```"):
                    lines = lines[1:]
                if lines and lines[-1].startswith("```"):
                    lines = lines[:-1]
                resolved = "\n".join(lines)
            return resolved
        except Exception:
            return conflict.ours_code
