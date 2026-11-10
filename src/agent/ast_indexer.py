"""AST and symbol graph navigation engine for semantic code exploration."""

import ast
import logging
import os
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


@dataclass
class SymbolLocation:
    """Metadata and location for an indexed symbol."""

    name: str
    kind: str
    file_path: str
    line_number: int
    end_line_number: Optional[int] = None
    docstring: Optional[str] = None


class ASTSymbolIndexer:
    """Indexes and queries symbols across a repository workspace."""

    def __init__(self, workspace_dir: str = "."):
        self.workspace_dir = workspace_dir
        self.symbols: Dict[str, List[SymbolLocation]] = {}

    def index_workspace(self, extensions: Optional[List[str]] = None) -> int:
        """Indexes symbols from Python source files."""
        exts = extensions or [".py"]
        self.symbols.clear()
        count = 0

        for root, dirs, files in os.walk(self.workspace_dir):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv", "venv")]
            for f in files:
                if any(f.endswith(ext) for ext in exts):
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, self.workspace_dir)
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as src_file:
                            content = src_file.read()
                        tree = ast.parse(content, filename=rel_path)
                        for node in ast.walk(tree):
                            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                                sym = SymbolLocation(
                                    name=node.name,
                                    kind="function",
                                    file_path=rel_path,
                                    line_number=node.lineno,
                                    end_line_number=getattr(node, "end_lineno", None),
                                    docstring=ast.get_docstring(node),
                                )
                                self._add_symbol(sym)
                                count += 1
                            elif isinstance(node, ast.ClassDef):
                                sym = SymbolLocation(
                                    name=node.name,
                                    kind="class",
                                    file_path=rel_path,
                                    line_number=node.lineno,
                                    end_line_number=getattr(node, "end_lineno", None),
                                    docstring=ast.get_docstring(node),
                                )
                                self._add_symbol(sym)
                                count += 1
                    except Exception as e:
                        logger.debug(f"Failed to parse AST for {rel_path}: {e}")

        logger.info(f"Indexed {count} symbols across workspace")
        return count

    def _add_symbol(self, symbol: SymbolLocation):
        if symbol.name not in self.symbols:
            self.symbols[symbol.name] = []
        self.symbols[symbol.name].append(symbol)

    def find_symbol(self, name: str) -> List[SymbolLocation]:
        """Finds definitions for a symbol name."""
        return self.symbols.get(name, [])

    def find_references(self, symbol_name: str) -> List[Dict[str, Any]]:
        """Finds line occurrences of a symbol name across the codebase."""
        references = []
        for root, dirs, files in os.walk(self.workspace_dir):
            dirs[:] = [d for d in dirs if d not in (".git", "node_modules", "__pycache__", ".venv")]
            for f in files:
                if f.endswith(".py"):
                    full_path = os.path.join(root, f)
                    rel_path = os.path.relpath(full_path, self.workspace_dir)
                    try:
                        with open(full_path, "r", encoding="utf-8", errors="replace") as src:
                            for idx, line in enumerate(src, start=1):
                                if symbol_name in line:
                                    references.append({
                                        "file": rel_path,
                                        "line": idx,
                                        "content": line.strip(),
                                    })
                    except Exception:
                        pass
        return references
