"""Tests for Jules 2.0 capabilities (AST, Security, TDD, Rebase, Tandem)."""

from unittest.mock import MagicMock
import pytest
from src.agent.ast_indexer import ASTSymbolIndexer
from src.agent.security_scanner import SecurityScanner
from src.agent.tandem import TandemSubagentCoordinator
from src.agent.tdd_synthesizer import TDDSynthesizer
from src.git_manager.rebase import ConflictBlock, SemanticRebaseManager
from src.sandbox.docker_driver import SandboxManager


def test_ast_symbol_indexer(tmp_path):
    f = tmp_path / "sample.py"
    f.write_text("class MyService:\n    def execute(self):\n        return 42\n")

    indexer = ASTSymbolIndexer(workspace_dir=str(tmp_path))
    count = indexer.index_workspace()
    assert count >= 2
    assert len(indexer.find_symbol("MyService")) == 1
    assert len(indexer.find_symbol("execute")) == 1


def test_security_scanner_rules():
    scanner = SecurityScanner()
    clean_diff = "+def run(): return True\n"
    assert scanner.scan_diff(clean_diff).passed is True

    leaked_diff = "+API_KEY = 'sk-12345678901234567890123456'\n"
    rep = scanner.scan_diff(leaked_diff)
    assert rep.passed is False
    assert rep.findings[0].category == "secret_leak"


def test_tdd_synthesizer_generation():
    mock_llm = MagicMock()
    mock_llm.completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"test_file_path": "tests/test_x.py", "test_code": "def test_x(): pass", "target_behavior": "X"}'))]
    )

    synth = TDDSynthesizer(llm_client=mock_llm)
    test_obj = synth.synthesize_reproduction_test("Issue 1", "Bug description")
    assert test_obj.test_file_path == "tests/test_x.py"
    assert "def test_x" in test_obj.test_code


def test_semantic_rebase_resolver():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_llm = MagicMock()
    mock_llm.completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content="return a + b"))]
    )

    rebase = SemanticRebaseManager(mock_sandbox, llm_client=mock_llm)
    conflict = ConflictBlock(
        file_path="f.py",
        ours_code="return a",
        theirs_code="return b",
        raw_block="<<<<<<< ours\nreturn a\n=======\nreturn b\n>>>>>>> theirs",
    )
    resolved = rebase.resolve_conflict_block(conflict)
    assert resolved == "return a + b"


def test_tandem_navigator_subagent():
    mock_llm = MagicMock()
    mock_llm.completion.return_value = MagicMock(
        choices=[MagicMock(message=MagicMock(content='{"category": "approved", "message": "All good"}'))]
    )

    tandem = TandemSubagentCoordinator(llm_client=mock_llm)
    fb = tandem.inspect_driver_turn("task", ["thought"], {"name": "run_command"}, "output")
    assert fb.category == "approved"
