import json
import time
from pathlib import Path

from common.runner import ExperimentRunner
from common.task_schema import TaskSpec
from common.modeling_pipeline import CANONICAL_PREP_FILES, CANONICAL_PROJECT_FILES


def test_runner_default_timeout_matches_formal_baseline(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    runner = ExperimentRunner(skills_dir=skills, runs_dir=tmp_path / "runs")
    assert runner.timeout_s == 7800


def _write_canonical_project(root: Path) -> None:
    (root / "py" / "prep").mkdir(parents=True)
    (root / "py" / "项目画像.md").write_text("# smoke", encoding="utf-8")
    for rel in CANONICAL_PROJECT_FILES:
        if rel == "项目画像.md":
            continue
        (root / "py" / "prep" / rel).write_text("# generated\n", encoding="utf-8")


def test_runner_writes_canonical_artifacts_and_manifest(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text(
        "---\nname: Alpha\ndescription: test\n---\nBody", encoding="utf-8"
    )
    task = TaskSpec.from_dict(
        {
            "task_id": "smoke",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "建立基础模型。",
        }
    )
    runner = ExperimentRunner(skills_dir=skills, runs_dir=tmp_path / "runs")
    summary = runner.run(task, architecture_id="T1", seed=7)
    assert summary.status == "not_configured"
    assert summary.run_dir.exists()
    for name in ("input.json", "adapter_request.json", "backend_status.json", "evaluation.json", "manifest.json"):
        assert (summary.run_dir / name).exists()
    assert (summary.run_dir / "skill_bundle.md").exists()
    manifest = json.loads((summary.run_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["architecture_id"] == "T1"
    assert manifest["seed"] == 7
    assert manifest["skill_bundle_sha256"]


def test_runner_materializes_candidate_and_records_static_stage(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    candidate = tmp_path / "candidate"
    _write_canonical_project(candidate)
    task = TaskSpec.from_dict(
        {
            "task_id": "candidate-smoke",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )

    runner = ExperimentRunner(
        skills_dir=skills,
        runs_dir=tmp_path / "runs",
        parent_repo=tmp_path / "missing-parent",
    )
    summary = runner.run(task, architecture_id="T6", seed=1, candidate_project=candidate)

    assert (summary.run_dir / "candidate_project" / "py" / "prep" / "main.py").exists()
    layout = json.loads((summary.run_dir / "layout.json").read_text(encoding="utf-8"))
    assert layout["complete"] is True
    static = json.loads((summary.run_dir / "static_conformance.json").read_text(encoding="utf-8"))
    assert static["status"] == "evaluator_unavailable"
    assert summary.evaluation.complete_success is False


def test_runner_orders_pyosis_execution_before_final_artifacts(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    candidate = tmp_path / "candidate"
    _write_canonical_project(candidate)
    task = TaskSpec.from_dict(
        {
            "task_id": "execution-order",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )

    class FakePyOSIS:
        def create_model(self, run_dir):
            return {"execution_enabled": True, "status": "ready", "failure_code": None}

        def execute(self, candidate_project, run_dir, **kwargs):
            status = {
                "execution_enabled": True,
                "status": "succeeded",
                "model_created": True,
                "solver_converged": False,
                "validation_passed": True,
                "failure_code": None,
            }
            (run_dir / "backend_status.json").write_text(json.dumps(status), encoding="utf-8")
            (run_dir / "build_status.json").write_text(json.dumps(status), encoding="utf-8")
            (run_dir / "model_state.json").write_text(
                json.dumps({"status": "available", "summary": {"nodes": 2}}),
                encoding="utf-8",
            )
            return status

    runner = ExperimentRunner(
        skills_dir=skills,
        runs_dir=tmp_path / "runs",
        parent_repo=tmp_path / "missing-parent",
        pyosis_enabled=True,
        pyosis_adapter=FakePyOSIS(),
    )
    summary = runner.run(task, architecture_id="T6", seed=2, candidate_project=candidate)

    assert summary.run_dir.joinpath("model_state.json").exists()
    assert summary.run_dir.joinpath("execution_trace.json").exists()
    assert json.loads(summary.run_dir.joinpath("backend_status.json").read_text(encoding="utf-8"))["model_created"] is True
    assert summary.evaluation.complete_success is False  # CLI scorer is unavailable


def test_runner_can_generate_inside_the_task_timeout_window(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    task = TaskSpec.from_dict(
        {
            "task_id": "generated-candidate",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
            "total_timeout_s": 10,
        }
    )

    def generator(task_spec, workspace, skill_reader):
        assert task_spec.task_id == "generated-candidate"
        assert skill_reader.list_skills()[0].skill_id == "alpha"
        candidate = workspace / "candidate_project"
        _write_canonical_project(candidate)
        return candidate

    runner = ExperimentRunner(skills_dir=skills, runs_dir=tmp_path / "runs")
    summary = runner.run(
        task,
        architecture_id="T2",
        seed=0,
        candidate_generator=generator,
    )

    assert (summary.run_dir / "candidate_project" / "py" / "prep" / "main.py").exists()
    timer = json.loads((summary.run_dir / "timer.json").read_text(encoding="utf-8"))
    assert timer["total_timeout_s"] == 10.0
    assert timer["time_to_candidate_s"] is not None
    assert timer["time_to_compile_s"] is not None
    assert timer["time_to_model_s"] is not None
    assert timer["time_to_score_s"] is not None


def test_runner_preserves_framework_partial_status(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    task = TaskSpec.from_dict(
        {
            "task_id": "partial-generation",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )

    def generator(task_spec, workspace, skill_reader):
        del task_spec, skill_reader
        candidate = workspace / "candidate_project"
        candidate.mkdir(parents=True)
        (candidate / "py").mkdir()
        (candidate / "py" / "项目画像.md").write_text("partial\n", encoding="utf-8")
        (workspace / "t2_generation.json").write_text(
            json.dumps(
                {
                    "architecture_id": "T2",
                    "status": "failed",
                    "error_type": "TimeoutError",
                    "error": "agent timed out after writing one file",
                    "files_written": ["py/项目画像.md"],
                }
            ),
            encoding="utf-8",
        )
        return candidate

    runner = ExperimentRunner(skills_dir=skills, runs_dir=tmp_path / "runs")
    summary = runner.run(task, architecture_id="T2", seed=0, candidate_generator=generator)
    trace = json.loads((summary.run_dir / "execution_trace.json").read_text(encoding="utf-8"))
    assert trace["generation"]["status"] == "partial"
    assert trace["generation"]["framework_status"] == "failed"
    assert "timed out" in trace["generation"]["error"]


def test_runner_can_include_pre_generation_time_in_total_timeout(tmp_path: Path):
    skills = tmp_path / "skills"
    skills.mkdir()
    (skills / "alpha").mkdir()
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    task = TaskSpec.from_dict(
        {
            "task_id": "clock-origin",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
            "total_timeout_s": 1,
        }
    )
    origin = time.monotonic() - 2.0
    calls = []

    def generator(*_args):
        calls.append(True)
        raise AssertionError("generator must not start after the task deadline")

    runner = ExperimentRunner(skills_dir=skills, runs_dir=tmp_path / "runs")
    summary = runner.run(
        task,
        architecture_id="T1",
        seed=0,
        candidate_generator=generator,
        started_monotonic=origin,
        deadline_monotonic=origin + 1.0,
    )
    timer = json.loads((summary.run_dir / "timer.json").read_text(encoding="utf-8"))
    trace = json.loads((summary.run_dir / "execution_trace.json").read_text(encoding="utf-8"))
    assert calls == []
    assert timer["status"] == "timeout"
    assert timer["timeout_scope"] == "task"
    assert timer["timeout_stage"] == "P1-P2"
    assert trace["generation"]["status"] == "timeout"


def test_runner_recovers_child_generation_metadata_on_exception(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    task = TaskSpec.from_dict(
        {
            "task_id": "child-timeout",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )

    def generator(_task, workspace, _skills):
        (workspace / "t4_generation.json").write_text(
            json.dumps({"status": "timeout", "error_type": "TimeoutExpired"}),
            encoding="utf-8",
        )
        raise RuntimeError("child stopped before returning")

    runner = ExperimentRunner(skills_dir=skills, runs_dir=tmp_path / "runs")
    summary = runner.run(task, architecture_id="T4", seed=0, candidate_generator=generator)
    trace = json.loads((summary.run_dir / "execution_trace.json").read_text(encoding="utf-8"))
    assert trace["generation"]["status"] == "timeout"
    assert trace["generation"]["framework_status"] == "timeout"


def _write_prep_project(root: Path, *, skip: str | None = None, profile: bool = True) -> None:
    (root / "py" / "prep").mkdir(parents=True)
    if profile:
        (root / "py" / "项目画像.md").write_text("# smoke", encoding="utf-8")
    for rel in CANONICAL_PREP_FILES:
        if rel == skip:
            continue
        (root / "py" / "prep" / rel).write_text("# generated\n", encoding="utf-8")


class _RecordingPyOSIS:
    def __init__(self):
        self.calls = []

    def create_model(self, run_dir):
        return {"execution_enabled": True, "status": "ready", "failure_code": None}

    def execute(self, candidate_project, run_dir, **kwargs):
        self.calls.append((candidate_project, run_dir, kwargs))
        status = {
            "execution_enabled": True,
            "status": "succeeded",
            "model_created": True,
            "solver_converged": kwargs.get("solve", False),
            "validation_passed": True,
            "solve_requested": bool(kwargs.get("solve", False)),
            "failure_code": None,
        }
        (run_dir / "backend_status.json").write_text(json.dumps(status), encoding="utf-8")
        return status


def _skills_and_task(tmp_path: Path, task_id: str):
    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    task = TaskSpec.from_dict(
        {
            "task_id": task_id,
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )
    return skills, task


def test_runner_t6_executes_without_profile_like_parent_eval(tmp_path: Path):
    skills, task = _skills_and_task(tmp_path, "t6-no-profile")
    candidate = tmp_path / "candidate"
    _write_prep_project(candidate, profile=False)
    adapter = _RecordingPyOSIS()
    runner = ExperimentRunner(
        skills_dir=skills,
        runs_dir=tmp_path / "runs",
        pyosis_enabled=True,
        solve_gate=True,
        pyosis_adapter=adapter,
    )
    summary = runner.run(task, architecture_id="T6", seed=0, candidate_project=candidate)
    backend = json.loads((summary.run_dir / "backend_status.json").read_text(encoding="utf-8"))
    assert adapter.calls, "T6 must invoke PyOSIS even when 项目画像.md is absent"
    assert backend["status"] == "succeeded"
    assert adapter.calls[0][2].get("solve") is True


def test_runner_t6_executes_when_a_prep_module_is_missing(tmp_path: Path):
    skills, task = _skills_and_task(tmp_path, "t6-missing-prep")
    candidate = tmp_path / "candidate"
    _write_prep_project(candidate, skip="_10_stage.py")
    adapter = _RecordingPyOSIS()
    runner = ExperimentRunner(
        skills_dir=skills,
        runs_dir=tmp_path / "runs",
        pyosis_enabled=True,
        pyosis_adapter=adapter,
    )
    summary = runner.run(task, architecture_id="T6", seed=0, candidate_project=candidate)
    backend = json.loads((summary.run_dir / "backend_status.json").read_text(encoding="utf-8"))
    assert adapter.calls, "parent eval has no 13-file gate; T6 must still execute"
    assert backend["failure_code"] != "candidate_layout_incomplete"


def test_runner_other_arch_skips_pyosis_when_a_prep_module_is_missing(tmp_path: Path):
    skills, task = _skills_and_task(tmp_path, "t5-missing-prep")
    candidate = tmp_path / "candidate"
    _write_prep_project(candidate, skip="_10_stage.py")
    adapter = _RecordingPyOSIS()
    runner = ExperimentRunner(
        skills_dir=skills,
        runs_dir=tmp_path / "runs",
        pyosis_enabled=True,
        pyosis_adapter=adapter,
    )
    summary = runner.run(task, architecture_id="T5", seed=0, candidate_project=candidate)
    backend = json.loads((summary.run_dir / "backend_status.json").read_text(encoding="utf-8"))
    assert adapter.calls == []
    assert backend["failure_code"] == "candidate_layout_incomplete"


def test_runner_marks_pyosis_not_run_when_generation_has_no_candidate(tmp_path: Path):
    """A missing candidate must not leave the prepare-time ``ready`` status."""

    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    task = TaskSpec.from_dict(
        {
            "task_id": "no-candidate-pyosis",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )

    def generator(_task, _workspace, _skills):
        raise RuntimeError("framework failed before writing candidate files")

    runner = ExperimentRunner(
        skills_dir=skills,
        runs_dir=tmp_path / "runs",
        pyosis_enabled=True,
    )
    summary = runner.run(
        task,
        architecture_id="T5",
        seed=0,
        candidate_generator=generator,
    )

    backend = json.loads((summary.run_dir / "backend_status.json").read_text(encoding="utf-8"))
    assert backend["execution_enabled"] is True
    assert backend["status"] == "not_run"
    assert backend["model_created"] is False
    assert backend["failure_code"] == "candidate_missing"


def test_runner_keeps_source_score_and_adds_runtime_score_sidecar(tmp_path: Path, monkeypatch):
    """Runtime scoring is a parallel post-processing artifact, never a replacement."""

    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    candidate = tmp_path / "candidate"
    _write_canonical_project(candidate)
    task = TaskSpec.from_dict(
        {
            "task_id": "runtime-sidecar",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )

    fixture = Path(__file__).parent / "fixtures" / "runtime_measurements_cantilever.json"

    class FakePyOSIS:
        def create_model(self, run_dir):
            return {"execution_enabled": True, "status": "ready", "failure_code": None}

        def execute(self, candidate_project, run_dir, **kwargs):
            del candidate_project, kwargs
            status = {
                "execution_enabled": True,
                "status": "succeeded",
                "model_created": True,
                "solver_converged": False,
                "validation_passed": True,
                "failure_code": None,
            }
            (run_dir / "backend_status.json").write_text(json.dumps(status), encoding="utf-8")
            (run_dir / "build_status.json").write_text(json.dumps(status), encoding="utf-8")
            (run_dir / "model_state.json").write_text(
                json.dumps({"status": "available", "summary": {"nodes": 4}}),
                encoding="utf-8",
            )
            (run_dir / "runtime_measurements.json").write_text(
                fixture.read_text(encoding="utf-8"), encoding="utf-8"
            )
            return status

    class FakeSourceScorer:
        def __init__(self, **kwargs):
            del kwargs

        def score(self, candidate_root, run_dir, task_spec, **kwargs):
            del candidate_root, task_spec, kwargs
            value = {
                "scorer": "source-sentinel",
                "status": "evaluated",
                "candidate_score": 0.123,
                "reason": None,
            }
            (run_dir / "model_score.json").write_text(
                json.dumps(value), encoding="utf-8"
            )
            return value

    monkeypatch.setattr("common.runner.ModelConformanceCLIScorer", FakeSourceScorer)
    runner = ExperimentRunner(
        skills_dir=skills,
        runs_dir=tmp_path / "runs",
        parent_repo=tmp_path / "parent",
        pyosis_enabled=True,
        pyosis_adapter=FakePyOSIS(),
    )

    summary = runner.run(task, architecture_id="T6", seed=0, candidate_project=candidate)

    source = json.loads((summary.run_dir / "model_score.json").read_text(encoding="utf-8"))
    runtime = json.loads((summary.run_dir / "runtime_score.json").read_text(encoding="utf-8"))
    evaluation = json.loads((summary.run_dir / "evaluation.json").read_text(encoding="utf-8"))
    assert source["scorer"] == "source-sentinel"
    assert source["candidate_score"] == 0.123
    assert runtime["status"] == "evaluated"
    assert runtime["source"] == "pyosis_runtime_snapshot"
    assert runtime["candidate_score"] != source["candidate_score"]
    # The official runner result remains sourced from the existing source score.
    assert evaluation["components"]["model_correctness"] == 0.123
