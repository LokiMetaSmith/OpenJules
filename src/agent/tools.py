"""Agent tools executing within the sandbox container workspace."""

import json
from typing import Any, Dict, List, Optional
from src.sandbox.docker_driver import ExecResult, SandboxManager


class AgentTools:
    """Provides file and shell operations executed directly inside the sandbox container."""

    def __init__(self, sandbox_manager: SandboxManager, container_id: str):
        self.sandbox_manager = sandbox_manager
        self.container_id = container_id

    def read_file(self, path: str, offset: int = 1, limit: int = 200) -> str:
        """Reads a file with line offsets (1-indexed) and limit."""
        # Sanitize path to prevent breaking out of shell string quotes
        safe_path = path.replace("'", "'\\''")
        # Python script inside container to safely slice lines
        py_cmd = (
            f"python3 -c \""
            f"import sys\n"
            f"try:\n"
            f"    with open('{safe_path}', 'r', encoding='utf-8', errors='replace') as f:\n"
            f"        lines = f.readlines()\n"
            f"    start = max(0, {offset} - 1)\n"
            f"    end = start + {limit}\n"
            f"    selected = lines[start:end]\n"
            f"    out = []\n"
            f"    for idx, line in enumerate(selected, start=start + 1):\n"
            f"        out.append(f'{{idx}}: {{line}}')\n"
            f"    sys.stdout.write(''.join(out))\n"
            f"except Exception as e:\n"
            f"    sys.stderr.write(str(e))\n"
            f"    sys.exit(1)\n"
            f"\""
        )
        res = self.sandbox_manager.exec_in_sandbox(self.container_id, py_cmd)
        if res.exit_code != 0:
            return f"Error reading file '{path}': {res.stderr or res.stdout}"
        return res.stdout if res.stdout else "(empty file)"

    def write_file(self, path: str, content: str) -> str:
        """Writes content to a file, creating parent directories if needed."""
        # Transfer content safely by base64 encoding it into the shell command
        import base64

        b64_content = base64.b64encode(content.encode("utf-8")).decode("ascii")
        safe_path = path.replace("'", "'\\''")
        py_cmd = (
            f"python3 -c \""
            f"import base64, os, sys\n"
            f"try:\n"
            f"    target = '{safe_path}'\n"
            f"    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)\n"
            f"    data = base64.b64decode('{b64_content}')\n"
            f"    with open(target, 'wb') as f:\n"
            f"        f.write(data)\n"
            f"    sys.stdout.write('Successfully wrote ' + str(len(data)) + ' bytes to ' + target)\n"
            f"except Exception as e:\n"
            f"    sys.stderr.write(str(e))\n"
            f"    sys.exit(1)\n"
            f"\""
        )
        res = self.sandbox_manager.exec_in_sandbox(self.container_id, py_cmd)
        if res.exit_code != 0:
            return f"Error writing file '{path}': {res.stderr or res.stdout}"
        return res.stdout

    def apply_patch(self, path: str, patch_str: str) -> str:
        """Applies a patch or diff to the target file.

        Supports standard diff / git patch format or SEARCH/REPLACE block style.
        """
        import base64

        # If patch contains SEARCH/REPLACE blocks, handle in Python
        if "<<<<<<< SEARCH" in patch_str and ">>>>>>> REPLACE" in patch_str:
            b64_patch = base64.b64encode(patch_str.encode("utf-8")).decode("ascii")
            safe_path = path.replace("'", "'\\''")
            py_cmd = (
                f"python3 -c \""
                f"import base64, sys\n"
                f"raw_patch = base64.b64decode('{b64_patch}').decode('utf-8')\n"
                f"target = '{safe_path}'\n"
                f"try:\n"
                f"    with open(target, 'r', encoding='utf-8') as f:\n"
                f"        original = f.read()\n"
                f"    parts = raw_patch.split('<<<<<<< SEARCH')\n"
                f"    current_text = original\n"
                f"    for part in parts[1:]:\n"
                f"        search_chunk, rest = part.split('=======', 1)\n"
                f"        replace_chunk, _ = rest.split('>>>>>>> REPLACE', 1)\n"
                f"        search_text = search_chunk.strip('\\r\\n')\n"
                f"        replace_text = replace_chunk.strip('\\r\\n')\n"
                f"        if search_text not in current_text:\n"
                f"            sys.stderr.write('Search block not found in ' + target)\n"
                f"            sys.exit(1)\n"
                f"        current_text = current_text.replace(search_text, replace_text, 1)\n"
                f"    with open(target, 'w', encoding='utf-8') as f:\n"
                f"        f.write(current_text)\n"
                f"    sys.stdout.write('Successfully applied merge diff block(s) to ' + target)\n"
                f"except Exception as e:\n"
                f"    sys.stderr.write(str(e))\n"
                f"    sys.exit(1)\n"
                f"\""
            )
            res = self.sandbox_manager.exec_in_sandbox(self.container_id, py_cmd)
            if res.exit_code != 0:
                return f"Error applying patch to '{path}': {res.stderr or res.stdout}"
            return res.stdout
        else:
            # Standard unified diff via git apply or patch
            b64_patch = base64.b64encode(patch_str.encode("utf-8")).decode("ascii")
            cmd = (
                f"python3 -c \""
                f"import base64, subprocess, sys\n"
                f"patch_bytes = base64.b64decode('{b64_patch}')\n"
                f"proc = subprocess.run(['git', 'apply', '--whitespace=fix', '-'], input=patch_bytes, capture_output=True)\n"
                f"if proc.returncode != 0:\n"
                f"    proc2 = subprocess.run(['patch', '-p1'], input=patch_bytes, capture_output=True)\n"
                f"    if proc2.returncode != 0:\n"
                f"        sys.stderr.write(proc.stderr.decode('utf-8') + proc2.stderr.decode('utf-8'))\n"
                f"        sys.exit(1)\n"
                f"sys.stdout.write('Patch applied successfully')\n"
                f"\""
            )
            res = self.sandbox_manager.exec_in_sandbox(self.container_id, cmd)
            if res.exit_code != 0:
                return f"Error applying patch to '{path}': {res.stderr or res.stdout}"
            return res.stdout

    def run_command(self, command: str, timeout: int = 120) -> str:
        """Runs an arbitrary shell command within the container."""
        res = self.sandbox_manager.exec_in_sandbox(self.container_id, command, timeout=timeout)
        output = []
        if res.stdout:
            output.append(f"[STDOUT]\n{res.stdout}")
        if res.stderr:
            output.append(f"[STDERR]\n{res.stderr}")
        output.append(f"[EXIT CODE]: {res.exit_code}")
        return "\n".join(output)

    def list_files(self, directory: str = ".", max_depth: int = 3) -> str:
        """Lists files and directories up to max_depth."""
        safe_dir = directory.replace("'", "'\\''")
        py_cmd = (
            f"python3 -c \""
            f"import os, sys\n"
            f"base_dir = '{safe_dir}'\n"
            f"max_depth = {max_depth}\n"
            f"res = []\n"
            f"for root, dirs, files in os.walk(base_dir):\n"
            f"    rel_path = os.path.relpath(root, base_dir)\n"
            f"    depth = 0 if rel_path == '.' else rel_path.count(os.sep) + 1\n"
            f"    if depth > max_depth:\n"
            f"        dirs.clear()\n"
            f"        continue\n"
            f"    # ignore .git\n"
            f"    dirs[:] = [d for d in dirs if d != '.git' and not d.startswith('.git/')]\n"
            f"    indent = '  ' * depth\n"
            f"    folder_name = os.path.basename(root) if rel_path != '.' else '.'\n"
            f"    if rel_path != '.':\n"
            f"        res.append(f'{{indent}}[{{folder_name}}/]')\n"
            f"    for f in sorted(files):\n"
            f"        file_indent = '  ' * (depth + 1 if rel_path != '.' else 0)\n"
            f"        res.append(f'{{file_indent}}{{f}}')\n"
            f"sys.stdout.write('\\n'.join(res))\n"
            f"\""
        )
        res = self.sandbox_manager.exec_in_sandbox(self.container_id, py_cmd)
        if res.exit_code != 0:
            return f"Error listing directory '{directory}': {res.stderr or res.stdout}"
        return res.stdout if res.stdout else "(empty directory)"


TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Reads line-numbered content of a file from the repository workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file"},
                    "offset": {
                        "type": "integer",
                        "description": "Starting line number (1-indexed)",
                        "default": 1,
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Number of lines to read",
                        "default": 200,
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Writes or overwrites a file with new content.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file"},
                    "content": {"type": "string", "description": "Full file content to write"},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "apply_patch",
            "description": "Applies a unified patch or Git merge diff search/replace block to a file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Relative path to file"},
                    "patch_str": {
                        "type": "string",
                        "description": "Diff patch or SEARCH/REPLACE string block",
                    },
                },
                "required": ["path", "patch_str"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_command",
            "description": "Runs a shell command inside the sandbox container.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"},
                    "timeout": {
                        "type": "integer",
                        "description": "Timeout in seconds",
                        "default": 120,
                    },
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "Lists repository files and folders up to max_depth.",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "Directory to list",
                        "default": ".",
                    },
                    "max_depth": {
                        "type": "integer",
                        "description": "Maximum directory traversal depth",
                        "default": 3,
                    },
                },
            },
        },
    },
]
