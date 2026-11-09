"""Unit tests for Deep Planning Clarification detector."""

from unittest.mock import MagicMock
import pytest
from src.agent.clarification import TaskClarificationDetector


def test_clarification_empty_description():
    detector = TaskClarificationDetector()
    res = detector.evaluate_task("Fix bug", "")
    assert res.is_ambiguous is True
    assert len(res.questions) > 0


def test_clarification_sufficient_description():
    detector = TaskClarificationDetector()
    res = detector.evaluate_task(
        "Fix division by zero",
        "When running calculate(10, 0), it crashes with ZeroDivisionError. Return 0 if divisor is 0.",
    )
    assert res.is_ambiguous is False


def test_clarification_llm_evaluation():
    mock_llm = MagicMock()
    msg_content = '{"is_ambiguous": true, "rationale": "Missing reproduction steps", "questions": ["How to reproduce?"]}'
    mock_llm.completion.return_value = MagicMock(
        choices=[
            MagicMock(message=MagicMock(content=msg_content))
        ]
    )

    detector = TaskClarificationDetector(llm_client=mock_llm)
    res = detector.evaluate_task("Network crash", "The client crashed yesterday without details.")
    assert res.is_ambiguous is True
    assert res.questions == ["How to reproduce?"]
