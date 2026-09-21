from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.solve_existing_runs import iter_run_dirs, solve_one


@pytest.fixture(autouse=True)
def _no_real_osis(monkeypatch):
    monkeypatch.setattr(
        "scripts.solve_existing_runs._ensure_osis_runtime_backend",
        lambda repo: None,
        raising=False,
    )
    monkeypatch.setattr(
        "scripts.solve_existing_runs.evaluate_runtime_snapshot",
        lambda *args, **kwargs: {"status": "evaluated"},
        raising=False,
    )


def _run_dir(root: Path, name: str, *, solve_requested: bool = False) -> Path:
    run = root / name
    (run / "candidate_project").mkdir(parents=True)
    (run / "candidate_project" / "py" / "prep").mkdir(parents=True)
    (run / "candidate_project" / "py" / "prep" / "main.py").write_text("", encoding="utf-8")
    (run / "input.json").write_text(json.dumps({
        "task_id": name,
        "bridge_type": "cantilever_box",
        "metadata": {"is_continuous": True},
        "subtask_timeout_s": {"P4": 30},
    }), encoding="utf-8")
    (run / "compile.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
    (run / "layout.json").write_text(json.dumps({"complete": True}), encoding="utf-8")
    (run / "reference_score.json").write_text(json.dumps({
        "status": "evaluated",
        "systems": {
            "generic_text": 1, "osis_text": 1, "python_syntax": 1,
            "model_conformance": 1, "efficiency": 1, "cost": 1,
        },
    }), encoding="utf-8")
    (run / "backend_status.json").write_text(json.dumps({
        "model_created": True,
        "validation_passed": True,
        "solve_requested": solve_requested,
        "solver_converged": solve_requested,
    }), encoding="utf-8")
    return run


def test_iter_run_dirs_only_returns_run_directories(tmp_path: Path):
    _run_dir(tmp_path, "cantilever_box__full__000__T1__seed0")
    (tmp_path / "campaign_summary.json").write_text("{}", encoding="utf-8")
    assert [p.name for p in iter_run_dirs(tmp_path)] == [
        "cantilever_box__full__000__T1__seed0"
    ]


def test_solve_one_reuses_candidate_and_refreshes_evaluation(tmp_path: Path, monkeypatch):
    run = _run_dir(tmp_path, "cantilever_box__full__000__T1__seed0")
    calls = []

    class FakeAdapter:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        def execute(self, candidate_project, run_dir, *, solve, timeout_s):
            calls.append(("execute", candidate_project, run_dir, solve, timeout_s))
            status = {
                "status": "succeeded", "execution_enabled": True,
                "model_created": True, "validation_passed": True,
                "solve_requested": True, "solver_converged": True,
            }
            (run_dir / "backend_status.json").write_text(json.dumps(status), encoding="utf-8")
            return status

    monkeypatch.setattr("scripts.solve_existing_runs.PyOSISAdapter", FakeAdapter)
    result = solve_one(run, tmp_path / "parent")

    assert result["status"] == "solved"
    assert calls[1][1] == run / "candidate_project"
    assert calls[1][3] is True
    assert json.loads((run / "solve_only.json").read_text(encoding="utf-8"))["solve_requested"] is True
    assert (run / "evaluation.json").is_file()


def test_solve_one_starts_osis_backend_before_rebuilding_project(tmp_path: Path, monkeypatch):
    run = _run_dir(tmp_path, "cantilever_box__full__000__T1__seed0")
    parent = tmp_path / "parent"
    started: list[Path] = []

    class FakeAdapter:
        def __init__(self, **kwargs):
            pass

        def execute(self, candidate_project, run_dir, *, solve, timeout_s):
            assert started == [parent]
            status = {
                "status": "succeeded", "execution_enabled": True,
                "model_created": True, "validation_passed": True,
                "solve_requested": True, "solver_converged": True,
            }
            (run_dir / "backend_status.json").write_text(json.dumps(status), encoding="utf-8")
            return status

    monkeypatch.setattr(
        "scripts.solve_existing_runs._ensure_osis_runtime_backend",
        lambda repo: started.append(repo),
        raising=False,
    )
    monkeypatch.setattr("scripts.solve_existing_runs.PyOSISAdapter", FakeAdapter)

    solve_one(run, parent)


def test_solve_one_build_failure_zeroes_official_score_but_keeps_diagnostic_score(tmp_path: Path, monkeypatch):
    run = _run_dir(tmp_path, "cantilever_box__full__000__T1__seed0")

    class FailedBuildAdapter:
        def __init__(self, **kwargs):
            pass

        def execute(self, candidate_project, run_dir, *, solve, timeout_s):
            status = {
                "status": "failed",
                "execution_enabled": True,
                "model_created": False,
                "validation_passed": False,
                "failure_code": "pyosis_build_failed",
            }
            (run_dir / "backend_status.json").write_text(
                json.dumps(status), encoding="utf-8"
            )
            return status

    monkeypatch.setattr("scripts.solve_existing_runs.PyOSISAdapter", FailedBuildAdapter)
    result = solve_one(run, tmp_path / "parent")

    assert result["status"] == "failed"
    assert result["quality_score"] == 0.0
    assert result["quality_score_before_gate"] == 60.0
    backend = json.loads((run / "backend_status.json").read_text(encoding="utf-8"))
    assert backend["solve_requested"] is True
    assert backend["solver_converged"] is False
    assert "solver_not_converged" not in json.loads(
        (run / "evaluation.json").read_text(encoding="utf-8")
    )["failure_reasons"]


def test_solve_one_rewrites_layout_when_profile_is_missing(tmp_path: Path, monkeypatch):
    run = _run_dir(tmp_path, "cantilever_box__gen__000__T6__seed0")
    (run / "layout.json").write_text(json.dumps({
        "complete": False, "missing_files": ["py/项目画像.md"],
    }), encoding="utf-8")
    from common.modeling_pipeline import CANONICAL_PREP_FILES
    prep = run / "candidate_project" / "py" / "prep"
    for name in CANONICAL_PREP_FILES:
        (prep / name).write_text("# generated\n", encoding="utf-8")

    class FakeAdapter:
        def __init__(self, **kwargs):
            pass

        def execute(self, candidate_project, run_dir, *, solve, timeout_s):
            status = {
                "status": "succeeded", "execution_enabled": True,
                "model_created": True, "validation_passed": True,
                "solve_requested": True, "solver_converged": True,
            }
            (run_dir / "backend_status.json").write_text(json.dumps(status), encoding="utf-8")
            return status

    monkeypatch.setattr("scripts.solve_existing_runs.PyOSISAdapter", FakeAdapter)
    solve_one(run, tmp_path / "parent")
    layout = json.loads((run / "layout.json").read_text(encoding="utf-8"))
    assert layout["complete"] is True
    assert "py/项目画像.md" not in layout["missing_files"]


def test_solve_one_skips_already_solved_run(tmp_path: Path, monkeypatch):
    run = _run_dir(tmp_path, "cantilever_box__full__000__T1__seed0", solve_requested=True)
    monkeypatch.setattr(
        "scripts.solve_existing_runs.PyOSISAdapter",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not execute")),
    )
    result = solve_one(run, tmp_path / "parent")
    assert result["status"] == "skipped"
    assert result["reason"] == "already_solved"
