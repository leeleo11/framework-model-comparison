"""Filesystem primitives for the canonical OSIS modeling task line.

Every framework is evaluated on the same project contract: a project root
containing ``py/项目画像.md`` and the twelve files under ``py/prep``.  The
framework-specific code is responsible for producing a project; this module
only materializes and checks that project in an isolated run directory.
"""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any


CANONICAL_PREP_FILES = (
    "main.py",
    "_0_engine.py",
    "_1_control.py",
    "_2_property.py",
    "_3_material.py",
    "_4_section.py",
    "_5_node.py",
    "_6_element.py",
    "_7_boundary.py",
    "_8_loadcase.py",
    "_9_analysis.py",
    "_10_stage.py",
)
CANONICAL_PROJECT_FILES = ("项目画像.md",) + CANONICAL_PREP_FILES

# Files a candidate MUST contain before PyOSIS is invoked.  The profile
# (项目画像.md) is deliberately absent: the parent repo's own pipeline neither
# requires nor scores it (osis_text required_files = main + prep modules only),
# and its gen/edit tasks stage the 11 prep files without the profile by design
# ("画像文件不进 base_files").  Gating PyOSIS on it killed native T6 gen runs
# that faithfully followed "仅需生成目标模块" and never wrote a profile -- a
# framework-invented rule stricter than the parent repo it benchmarks.
REQUIRED_PROJECT_FILES = CANONICAL_PREP_FILES

_IGNORED_NAMES = {".git", ".venv", "venv", "env", "__pycache__", ".pytest_cache"}


def _resolve_project_root(path: Path) -> Path:
    """Accept either a project root or its ``py`` directory."""

    resolved = Path(path).expanduser().resolve()
    if resolved.is_dir() and resolved.name.lower() == "py":
        return resolved.parent
    return resolved


def _locate_code_root(path: Path) -> Path:
    """Locate the directory that directly contains ``prep``."""

    resolved = Path(path).expanduser().resolve()
    if (resolved / "py" / "prep").is_dir():
        return resolved / "py"
    if (resolved / "prep").is_dir():
        return resolved
    return resolved / "py"


def _source_file(code_root: Path, relative: str) -> Path:
    if relative == "项目画像.md":
        return code_root / relative
    return code_root / "prep" / relative


def _canonical_relative(relative: str) -> str:
    if relative == "项目画像.md":
        return "py/项目画像.md"
    return f"py/prep/{relative}"


def validate_project_layout(project: Path) -> dict[str, Any]:
    """Validate the canonical project files without executing user code.

    ``complete`` reflects only ``REQUIRED_PROJECT_FILES`` (the prep modules).
    The profile is still reported in ``expected_files`` / ``file_hashes`` when
    present, so downstream consumers keep a stable view, but a missing profile
    no longer blocks PyOSIS execution.
    """

    root = _resolve_project_root(project)
    code_root = _locate_code_root(project)
    present: list[str] = []
    missing: list[str] = []
    file_hashes: dict[str, str] = {}
    for relative in CANONICAL_PROJECT_FILES:
        path = _source_file(code_root, relative)
        key = _canonical_relative(relative)
        if not path.is_file():
            if relative in REQUIRED_PROJECT_FILES:
                missing.append(key)
            continue
        present.append(key)
        file_hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {
        "project_root": str(root),
        "complete": not missing,
        "expected_files": [_canonical_relative(item) for item in CANONICAL_PROJECT_FILES],
        "present_files": present,
        "missing_files": missing,
        "file_hashes": file_hashes,
    }


def _copy_tree(source_root: Path, destination_root: Path) -> list[str]:
    copied: list[str] = []
    for path in sorted(source_root.rglob("*"), key=lambda item: item.as_posix().lower()):
        relative = path.relative_to(source_root)
        if any(part in _IGNORED_NAMES for part in relative.parts):
            continue
        target = destination_root / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append(relative.as_posix())
    return copied


def materialize_project(source: Path, destination: Path) -> dict[str, Any]:
    """Copy one framework's output into an isolated canonical project.

    The source is never modified.  Both ``<root>`` and ``<root>/py`` inputs
    are accepted.  Extra files are preserved (so a framework can include
    requirements or logs), while hidden caches and virtual environments are
    excluded.  The returned object is safe to persist as ``layout.json``.
    """

    source_input = Path(source).expanduser().resolve()
    source_root = _resolve_project_root(source)
    source_code_root = _locate_code_root(source)
    destination_root = _resolve_project_root(destination)
    if not source_input.is_dir():
        raise NotADirectoryError(source_input)
    if source_root == destination_root:
        raise ValueError("source and destination project roots must differ")

    destination_root.mkdir(parents=True, exist_ok=True)
    if source_code_root == source_input:
        copied_files = [
            f"py/{path}" for path in _copy_tree(source_code_root, destination_root / "py")
        ]
    else:
        copied_files = _copy_tree(source_root, destination_root)
    layout = validate_project_layout(destination_root)
    result = {
        "status": "materialized",
        "source_root": str(source_root),
        "destination_root": str(destination_root),
        "copied_files": copied_files,
        "missing_files": layout["missing_files"],
        "complete": layout["complete"],
        "layout": layout,
    }
    (destination_root / "materialization.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return result


def compile_python_project(project: Path) -> dict[str, Any]:
    """Compile every Python file in the candidate project in isolation."""

    root = _resolve_project_root(project)
    py_root = _locate_code_root(project)
    if not py_root.is_dir():
        return {"passed": False, "python_files": [], "failed_files": ["py"]}
    python_files = [
        path
        for path in sorted(py_root.rglob("*.py"), key=lambda item: item.as_posix().lower())
        if not any(part in _IGNORED_NAMES for part in path.parts)
    ]
    failed: list[str] = []
    for path in python_files:
        try:
            source = path.read_text(encoding="utf-8")
            compile(source, str(path), "exec")
        except (OSError, SyntaxError, ValueError):
            failed.append(path.relative_to(root).as_posix())
    return {
        "passed": bool(python_files) and not failed,
        "python_files": [path.relative_to(root).as_posix() for path in python_files],
        "failed_files": failed,
    }
