"""Execution feedback returns the failed step and stops without a step cap."""

from __future__ import annotations

from pathlib import Path

from common.execution_feedback import observe_candidate, run_feedback_loop
from common.modeling_pipeline import CANONICAL_PREP_FILES


def _project(tmp_path: Path, *, broken: bool = False) -> Path:
    root = tmp_path / "candidate"
    prep = root / "py" / "prep"
    prep.mkdir(parents=True)
    for name in CANONICAL_PREP_FILES:
        text = "def (\n" if broken and name == "_10_stage.py" else "x = 1\n"
        (prep / name).write_text(text, encoding="utf-8")
    return root


def test_compile_failure_does_not_call_pyosis(tmp_path: Path):
    calls = []

    def executor(candidate, scratch, deadline):
        calls.append(candidate)
        return {"model_created": True}

    text = observe_candidate(
        _project(tmp_path, broken=True),
        tmp_path / "scratch",
        parent_repo=None,
        executor=executor,
    )
    assert calls == []
    assert text is not None
    assert text.startswith("compile failed")
    assert "_10_stage.py" in text
    assert "SyntaxError" in text


def test_build_failure_returns_stderr_and_success_returns_none(tmp_path: Path):
    project = _project(tmp_path)
    scratch = tmp_path / "scratch"

    def fail(candidate, scratch_dir, deadline):
        scratch_dir.mkdir(parents=True, exist_ok=True)
        (scratch_dir / "build_stderr.log").write_text("spline rejected\n", encoding="utf-8")
        return {"model_created": False, "failure_code": "pyosis_build_failed"}

    failed = observe_candidate(project, scratch, parent_repo=None, executor=fail)
    assert failed is not None
    assert failed.startswith("pyosis_build_failed")
    assert "spline rejected" in failed

    def succeed(candidate, scratch_dir, deadline):
        return {"model_created": True}

    assert observe_candidate(project, scratch, parent_repo=None, executor=succeed) is None


def test_feedback_loop_stops_when_the_resubmit_is_unchanged(tmp_path: Path):
    project = _project(tmp_path)
    resumes = []

    def observe(scratch: Path) -> str:
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "build_stderr.log").write_text("still broken\n", encoding="utf-8")
        return "compile failed\npy/prep/main.py: SyntaxError: still broken"

    def resume(observation: str) -> None:
        resumes.append(observation)

    result = run_feedback_loop(
        candidate=project,
        scratch_root=tmp_path / "rounds",
        deadline_monotonic=None,
        observe=observe,
        resume=resume,
    )
    assert result["stop_reason"] == "unchanged"
    assert len(resumes) == 1
    assert resumes[0].startswith("compile failed")
    assert not (tmp_path / "rounds").exists()


def test_feedback_loop_stops_after_the_build_succeeds(tmp_path: Path):
    project = _project(tmp_path)
    seen = {"n": 0}

    def observe(scratch: Path) -> str | None:
        seen["n"] += 1
        if seen["n"] == 1:
            return "pyosis_build_failed\nbad curve"
        return None

    def resume(observation: str) -> None:
        (project / "py" / "prep" / "main.py").write_text("x = 2\n", encoding="utf-8")

    result = run_feedback_loop(
        candidate=project,
        scratch_root=tmp_path / "rounds",
        deadline_monotonic=None,
        observe=observe,
        resume=resume,
    )
    assert result["stop_reason"] == "completed"
    assert seen["n"] == 2
