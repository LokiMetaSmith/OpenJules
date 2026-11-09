"""Unit tests for Model Context Protocol (MCP) server."""

from unittest.mock import MagicMock
import pytest
from src.mcp.server import OpenJulesMCPServer
from src.sandbox.docker_driver import ExecResult, SandboxManager


def test_mcp_tool_list():
    mcp = OpenJulesMCPServer()
    tools = mcp.get_tool_list()
    tool_names = [t["name"] for t in tools]
    assert "openjules_read_file" in tool_names
    assert "openjules_write_file" in tool_names
    assert "openjules_run_command" in tool_names
    assert "openjules_acquire_baton" in tool_names
    assert "openjules_dispatch_task" in tool_names


def test_mcp_call_run_command():
    mock_sandbox = MagicMock(spec=SandboxManager)
    mock_sandbox.exec_in_sandbox.return_value = ExecResult(stdout="command output", stderr="", exit_code=0)

    mcp = OpenJulesMCPServer(sandbox_manager=mock_sandbox)
    res = mcp.call_tool("openjules_run_command", {"container_id": "c1", "command": "echo test"})
    assert "command output" in res["content"][0]["text"]
