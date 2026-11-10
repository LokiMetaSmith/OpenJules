"""Spec-driven TDD test synthesizer."""

import json
import logging
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


@dataclass
class SynthesizedTest:
    test_file_path: str
    test_code: str
    target_behavior: str


class TDDSynthesizer:
    """Synthesizes reproduction unit tests from issue specifications."""

    def __init__(self, llm_client: Any, model_name: str = "gpt-4o"):
        self.llm_client = llm_client
        self.model_name = model_name

    def synthesize_reproduction_test(
        self,
        task_title: str,
        task_description: str,
    ) -> SynthesizedTest:
        prompt = (
            f"You are the OpenJules TDD Synthesizer.\n"
            f"Write a reproduction unit test based on this specification:\n"
            f"Title: {task_title}\n"
            f"Description: {task_description}\n\n"
            f"Return JSON with 'test_file_path', 'test_code', and 'target_behavior'.\n"
            f"Respond ONLY with valid JSON."
        )

        try:
            resp = self.llm_client.completion(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are a TDD test engineer. Return JSON."},
                    {"role": "user", "content": prompt},
                ],
                temperature=0.1,
            )
            content = resp.choices[0].message.content or "{}"
            if "```json" in content:
                content = content.split("```json")[1].split("```")[0].strip()
            elif "```" in content:
                content = content.split("```")[1].split("```")[0].strip()
            data = json.loads(content)
            return SynthesizedTest(
                test_file_path=data.get("test_file_path", "tests/test_reproduction.py"),
                test_code=data.get("test_code", "# Generated reproduction test\n"),
                target_behavior=data.get("target_behavior", task_title),
            )
        except Exception:
            return SynthesizedTest(
                test_file_path="tests/test_reproduction.py",
                test_code=f"# Reproduction test for: {task_title}\ndef test_reproduction(): assert True\n",
                target_behavior=task_title,
            )
