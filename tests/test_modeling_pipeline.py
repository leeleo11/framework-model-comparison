import json
from pathlib import Path

from common.modeling_pipeline import (
    CANONICAL_PROJECT_FILES,
    compile_python_project,
    materialize_project,
    validate_project_layout,
)


def _write_canonical_project(root: Path) -> None:
    (root / "py" / "prep").mkdir(parents=True)
    (root / "py" / "项目画像.md").write_text("# smoke", encoding="utf-8")
    for rel in CANONICAL_PROJECT_FILES:
        if rel == "项目画像.md":
            continue
        (root / "py" / "prep" / rel).write_text("# generated\n", encoding="utf-8")


def test_materialize_project_copies_canonical_layout_and_reports_hashes(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "run" / "candidate_project"
    _write_canonical_project(source)
    (source / "py" / "prep" / "__pycache__").mkdir()
    (source / "py" / "prep" / "__pycache__" / "ignored.pyc").write_bytes(b"x")

    result = materialize_project(source, destination)

    assert result["status"] == "materialized"
    assert result["missing_files"] == []
    assert set(result["copied_files"]) == {
        "py/项目画像.md",
        *{f"py/prep/{path}" for path in CANONICAL_PROJECT_FILES[1:]},
    }
    assert not (destination / "py" / "prep" / "__pycache__").exists()
    assert (destination / "py" / "prep" / "main.py").exists()


def test_validate_project_layout_and_compile_are_explicit(tmp_path: Path):
    project = tmp_path / "project"
    _write_canonical_project(project)
    (project / "py" / "prep" / "bad.py").write_text("def broken(:\n", encoding="utf-8")

    layout = validate_project_layout(project)
    compile_result = compile_python_project(project)

    assert layout["complete"] is True
    assert compile_result["passed"] is False
    assert "py/prep/bad.py" in compile_result["failed_files"]


def test_materialize_project_writes_machine_readable_result(tmp_path: Path):
    source = tmp_path / "source"
    destination = tmp_path / "destination"
    _write_canonical_project(source)

    result = materialize_project(source, destination)
    (destination / "materialization.json").write_text(
        json.dumps(result, ensure_ascii=False), encoding="utf-8"
    )
    assert json.loads((destination / "materialization.json").read_text(encoding="utf-8"))["status"] == "materialized"


def test_materialize_accepts_existing_osis_template_as_py_root(tmp_path: Path):
    template = tmp_path / "template-name"
    (template / "prep").mkdir(parents=True)
    (template / "项目画像.md").write_text("# template", encoding="utf-8")
    for name in CANONICAL_PROJECT_FILES[1:]:
        (template / "prep" / name).write_text("# generated\n", encoding="utf-8")

    destination = tmp_path / "candidate_project"
    result = materialize_project(template, destination)

    assert result["complete"] is True
    assert (destination / "py" / "prep" / "main.py").exists()
    assert not (destination / "prep").exists()
