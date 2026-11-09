"""Deep Planning & Interactive Clarification Mode.

Detects ambiguous requirements, underspecified tasks, or missing reproduction steps,
and generates structured clarification questions for the issue author.
"""

import json
import logging
from typing import Any, List, Optional
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ClarificationEvaluation(BaseModel):
    """Result of ambiguity evaluation during deep planning."""

    is_ambiguous: bool = Field(
        default=False,
        description="Whether the task description is ambiguous, missing reproduction steps, or conflicting.",
    )
    rationale: str = Field(
        default="",
        description="Why the task is considered ambiguous or ready to execute.",
    )
    questions: List[str] = Field(
        default_factory=list,
        description="Specific clarification questions to present to the user.",
    )


class TaskClarificationDetector:
    """Evaluates task specifications for ambiguity before commencing action loop."""

    def __init__(self, llm_client: Optional[Any] = None, model_name: str = "gpt-4o"):
        self.llm_client = llm_client
        self.model_name = model_name

    def evaluate_task(self, title: str, description: str) -> ClarificationEvaluation:
        """Evaluates whether the prompt is ambiguous and formulates questions if needed."""
        # Simple fast heuristic: if description is empty or very short (<10 chars)
        if not description or len(description.strip()) < 10:
            return ClarificationEvaluation(
                is_ambiguous=True,
                rationale="The task description is missing or too brief to determine required changes.",
                questions=[
                    f"Could you provide more context or reproduction steps for '{title}'?",
                    "Which specific components or files are expected to be modified?",
                ],
            )

        if not self.llm_client:
            return ClarificationEvaluation(is_ambiguous=False, rationale="Task has sufficient detail.")

        prompt = (
            f"You are the OpenJules Deep Planning Clarification engine.\n"
            f"Evaluate if this issue has enough detail for an autonomous agent to execute safely:\n\n"
            f"Title: {title}\n"
            f"Description:\n{description}\n\n"
            f"Determine if the task is ambiguous or lacks critical constraints.\n"
            f"Respond with JSON:\n"
            f"- is_ambiguous: boolean (true if vital details are missing, false if clear enough)\n"
            f"- rationale: short explanation\n"
            f"- questions: list of specific questions (if is_ambiguous is true)\n"
            f"Respond ONLY with valid JSON."
        )

        try:
            resp = self.llm_client.completion(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are a software architect evaluating task clarity. Return JSON."},
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
            return ClarificationEvaluation(**data)
        except Exception as e:
            logger.debug(f"Clarification evaluation fallback: {e}")
            return ClarificationEvaluation(is_ambiguous=False, rationale="Proceeding with autonomous execution.")
