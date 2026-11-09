"""Unit tests for AGENTS.md rule engine."""

import os
import pytest
from src.agent.agents_rules import AgentsRuleEngine


def test_agents_rule_discovery_and_scoping(tmp_path):
    ws = str(tmp_path)
    engine = AgentsRuleEngine()

    # 1. Root AGENTS.md
    root_file = tmp_path / "AGENTS.md"
    root_file.write_text(
        "# Root Agent Guidelines\n"
        "- Follow Conventional Commits.\n"
        "- `pytest tests/`\n"
    )

    # 2. Nested AGENTS.md
    sub_dir = tmp_path / "src" / "api"
    sub_dir.mkdir(parents=True)
    sub_file = sub_dir / "AGENTS.md"
    sub_file.write_text(
        "# API Scoped Guidelines\n"
        "- All routes must include docstrings.\n"
        "- `ruff check src/api`\n"
    )

    scopes = engine.discover_rules(ws)
    assert len(scopes) == 2
    assert scopes[0].directory == "."
    assert scopes[1].directory == os.path.join("src", "api")

    # Programmatic checks
    assert scopes[0].programmatic_checks == ["pytest tests/"]
    assert scopes[1].programmatic_checks == ["ruff check src/api"]

    # Context formatting
    ctx = engine.get_prompt_context(ws)
    assert "Root Guidelines:" in ctx
    assert "Scoped Guidelines for `src/api/`:" in ctx
    assert "Follow Conventional Commits" in ctx
