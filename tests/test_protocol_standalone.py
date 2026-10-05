"""The protocol core stays standalone: it imports the standard library or itself, nothing else."""

from __future__ import annotations

import ast
import sys
from pathlib import Path

PACKAGE_NAME = "renac_protocol"
PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "renac_local" / PACKAGE_NAME
ALLOWED = set(sys.stdlib_module_names) | {PACKAGE_NAME}


def top_level_imports(path: Path) -> set[str]:
    """The first part of every absolute import in the file; relative imports are skipped."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module is not None:
            names.add(node.module.split(".")[0])
    return names


def test_imports_only_the_standard_library_or_itself() -> None:
    sources = sorted(PACKAGE_DIR.rglob("*.py"))
    assert sources
    foreign = {path.name: sorted(top_level_imports(path) - ALLOWED) for path in sources}
    assert {name: imports for name, imports in foreign.items() if imports} == {}
