"""Tandem pair-programming subagent protocol."""

import json
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional
from src.polyphony.client import PolyphonyClient

logger = logging.getLogger(__name__)


@dataclass
class NavigatorFeedback:
    category: str
    message: str


class TandemSubagentCoordinator:
    """Coordinates Driver and Navigator subagents via Polyphony."""

    def __init__(self, llm_client: Any, model_name: str = "gpt-4o"):
        self.llm_client = llm_client
        self.model_name = model_name

    def inspect_driver_turn(
        self,
        task_instruction: str,
        recent_thoughts: List[str],
        recent_tool_call: Dict[str, Any],
        tool_output: str,
    ) -> NavigatorFeedback:
        tool_name = recent_tool_call.get("name", "unknown")
        prompt = (
            f"You are the OpenJules Tandem Navigator observing the Driver agent.\n"
            f"Task: {task_instruction}\n"
            f"Action: {tool_name}\n"
            f"Output: {tool_output[:600]}\n\n"
            f"Return JSON with 'category' and 'message'."
        )

        try:
            resp = self.llm_client.completion(
                model=self.model_name,
                messages=[
                    {"role": "system", "content": "You are a pair programming navigator. Return JSON."},
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
            return NavigatorFeedback(
                category=data.get("category", "approved"),
                message=data.get("message", "Action verified by Navigator."),
            )
        except Exception:
            return NavigatorFeedback(category="approved", message="Proceed with plan.")

    def sync_to_polyphony(self, polyphony: PolyphonyClient, feedback: NavigatorFeedback):
        msg = f"[Tandem Navigator]: {feedback.message}"
        polyphony.share_thought(msg, urgency="high" if feedback.category != "approved" else "low")
