"""Unit tests for EnsembleEngine."""

from unittest.mock import MagicMock
import pytest
from src.agent.ensemble import EnsembleEngine


def test_ensemble_critic_approved():
    mock_llm = MagicMock()
    critic_json = '{"approved": true, "confidence_score": 0.98, "critique": "Clean bugfix with all tests green.", "recommended_amendments": []}'
    mock_llm.completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=critic_json))]
    )

    ensemble = EnsembleEngine(llm_client=mock_llm)
    eval_res = ensemble.evaluate_diff_consensus(
        task_instruction="Fix division error",
        git_diff="+ if b == 0: return 0",
        test_output="1 passed",
    )

    assert eval_res.approved is True
    assert eval_res.confidence_score == 0.98
    assert "Clean bugfix" in eval_res.critique


def test_ensemble_critic_rejected_amendments():
    mock_llm = MagicMock()
    critic_json = '{"approved": false, "confidence_score": 0.4, "critique": "Misses negative numbers.", "recommended_amendments": ["Handle abs()"]}'
    mock_llm.completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=critic_json))]
    )

    ensemble = EnsembleEngine(llm_client=mock_llm)
    eval_res = ensemble.evaluate_diff_consensus(
        task_instruction="Handle negative numbers",
        git_diff="+ pass",
        test_output="FAILED",
    )

    assert eval_res.approved is False
    assert eval_res.confidence_score == 0.4
    assert eval_res.recommended_amendments == ["Handle abs()"]
