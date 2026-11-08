"""Unit and integration tests for Adaptive Agent Harness and EXPERIENCE.md persistence."""

import os
from unittest.mock import MagicMock
import pytest

from src.agent.engine import AgentEngine
from src.harness.adaptive_engine import AdaptiveHarness, HarnessProposal
from src.harness.config import HarnessConfig
from src.harness.experience import ExperienceManager
from src.sandbox.config import OpenJulesConfig
from src.sandbox.docker_driver import ExecResult, SandboxContext, SandboxManager


def test_experience_manager_load_and_append(tmp_path):
    ws = str(tmp_path)
    mgr = ExperienceManager("EXPERIENCE.md")

    # Initial empty load
    assert mgr.load_experience(ws) == ""

    # Append first entry
    p1 = mgr.append_experience(
        workspace_dir=ws,
        task_title="Fix division by zero",
        problem_summary="ZeroDivisionError when input is zero.",
        solution_pattern="Added guard check `if n == 0: return 0`.",
        discovered_rules=["Always validate numeric divisors before computing ratios."],
    )
    assert os.path.exists(p1)

    content = mgr.load_experience(ws)
    assert "Fix division by zero" in content
    assert "Always validate numeric divisors" in content

    # Append second entry (compounding)
    mgr.append_experience(
        workspace_dir=ws,
        task_title="Add SQLite timeout",
        problem_summary="Database locked errors under load.",
        solution_pattern="Set timeout=30.0 on sqlite3 connection.",
        discovered_rules=["Use busy_timeout pragma on all SQLite backends."],
    )

    updated_content = mgr.load_experience(ws)
    assert "Fix division by zero" in updated_content
    assert "Add SQLite timeout" in updated_content


def test_adaptive_harness_system_prompt_extension(tmp_path):
    ws = str(tmp_path)
    harness = AdaptiveHarness(HarnessConfig(experience_path="EXPERIENCE.md"))

    # When no experience file exists and no active guardrails
    assert harness.get_system_prompt_extension(ws) == ""

    # Add active guardrail
    harness.active_guardrails.append("Do not modify migration files directly.")
    ext = harness.get_system_prompt_extension(ws)
    assert "Do not modify migration files directly." in ext

    # Create experience file in workspace
    harness.experience_mgr.append_experience(
        workspace_dir=ws,
        task_title="Prior lesson",
        problem_summary="Prior issue",
        solution_pattern="Prior solution",
        discovered_rules=["Rule 1"],
    )

    full_ext = harness.get_system_prompt_extension(ws)
    assert "Historical Repository Learnings" in full_ext
    assert "Rule 1" in full_ext
    assert "Do not modify migration files directly." in full_ext


def test_adaptive_harness_propose_adaptation():
    mock_llm = MagicMock()
    proposal_json = (
        '{"targeted_guardrails": ["Check None before index access"], '
        '"suggested_helper_commands": ["python -m py_compile main.py"], '
        '"suggested_strategy": "Add null check in parser."}'
    )
    mock_llm.completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=proposal_json))]
    )

    harness = AdaptiveHarness()
    proposal = harness.propose_harness_adaptation(
        llm_client=mock_llm,
        model_name="gpt-4o",
        task_instruction="Fix crash",
        failing_command="pytest",
        test_traceback="TypeError: 'NoneType' object is not subscriptable",
    )

    assert "Check None before index access" in proposal.targeted_guardrails
    assert proposal.suggested_helper_commands == ["python -m py_compile main.py"]
    assert "Add null check" in proposal.suggested_strategy
    assert "Check None before index access" in harness.active_guardrails


def test_adaptive_harness_record_successful_run(tmp_path):
    ws = str(tmp_path)
    mock_llm = MagicMock()

    distill_json = (
        '{"problem_summary": "Handled empty git branches.", '
        '"solution_pattern": "Added fallback checkout command.", '
        '"discovered_rules": ["Verify remote branch exists before sparse-checkout."]}'
    )
    mock_llm.completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content=distill_json))]
    )

    harness = AdaptiveHarness(HarnessConfig(experience_path="EXPERIENCE.md"))
    exp_file = harness.record_successful_run(
        workspace_dir=ws,
        task_title="Support empty branch",
        task_instruction="Instruction details",
        llm_client=mock_llm,
        model_name="gpt-4o",
    )

    assert os.path.exists(exp_file)
    content = open(exp_file).read()
    assert "Handled empty git branches." in content
    assert "Verify remote branch exists" in content


def test_agent_engine_runs_with_adaptive_harness(tmp_path):
    ws = str(tmp_path)
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()

    plan_json = '{"rationale": "Adaptive test plan", "target_files": ["app.py"], "planned_tests": ["pytest"], "action_steps": ["Edit app.py"]}'
    distill_json = '{"problem_summary": "Done", "solution_pattern": "Fixed", "discovered_rules": ["Rule A"]}'

    mock_llm.completion.side_effect = [
        # Step A plan
        MagicMock(choices=[MagicMock(message=MagicMock(content=plan_json))]),
        # Step B action
        MagicMock(choices=[MagicMock(message=MagicMock(content="TASK_COMPLETED", tool_calls=None))]),
        # Outer loop distillation
        MagicMock(choices=[MagicMock(message=MagicMock(content=distill_json))]),
    ]

    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="1 passed", stderr="", exit_code=0)

    harness = AdaptiveHarness(HarnessConfig(experience_path="EXPERIENCE.md"))
    engine = AgentEngine(mock_sandbox, llm_client=mock_llm, adaptive_harness=harness)

    ctx = SandboxContext("c1", ws, "https://github.com/o/r.git", "main", OpenJulesConfig())
    res = engine.run(ctx, "Test adaptive harness run")

    assert res.success is True
    # Verify EXPERIENCE.md was created in workspace
    assert os.path.isfile(os.path.join(ws, "EXPERIENCE.md"))
    content = open(os.path.join(ws, "EXPERIENCE.md")).read()
    assert "Rule A" in content
