"""Canonical runner for the bridge-modeling comparison line."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .adapters import get_adapter
from .cli_scorer import ModelConformanceCLIScorer
from .evaluator import EvaluationResult, evaluate_run
from .manifest import Manifest, artifact_hashes
from .modeling_pipeline import (
    CANONICAL_PROJECT_FILES,
    compile_python_project,
    materialize_project,
)
from .pyosis_adapter import PyOSISAdapter
from .runtime_scorer import evaluate_runtime_snapshot
from .run_layout import run_dir_from_task
from .skill_adapter import SkillAdapter
from .static_conformance import evaluate_static_conformance
from .task_schema import DEFAULT_TOTAL_TIMEOUT_S, TaskSpec


@dataclass(frozen=True)
class RunSummary:
    run_dir: Path
    status: str
    evaluation: EvaluationResult
    manifest: dict[str, Any]


class ExperimentRunner:
    def __init__(
        self,
        *,
        skills_dir: Path,
        runs_dir: Path,
        model_snapshot: str = "unconfigured",
        solver_version: str = "unconfigured",
        tool_schema_version: str = "v0",
        timeout_s: int = int(DEFAULT_TOTAL_TIMEOUT_S),
        token_budget: int = 100000,
        tool_call_budget: int = 200,
        parent_repo: Path | None = None,
        reference_project: Path | None = None,
        pyosis_enabled: bool = False,
        solve_gate: bool = False,
        pyosis_python: str | Path | None = None,
        pyosis_adapter: PyOSISAdapter | None = None,
        total_timeout_override_s: float | None = None,
    ):
        self.skill_adapter = SkillAdapter(skills_dir)
        self.runs_dir = Path(runs_dir).resolve()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self.model_snapshot = model_snapshot
        self.solver_version = solver_version
        self.tool_schema_version = tool_schema_version
        self.timeout_s = timeout_s
        self.token_budget = token_budget
        self.tool_call_budget = tool_call_budget
        self.parent_repo = Path(parent_repo).expanduser().resolve() if parent_repo else None
        self.reference_project = (
            Path(reference_project).expanduser().resolve()
            if reference_project
            else None
        )
        self.pyosis_enabled = pyosis_enabled
        self.solve_gate = solve_gate
        self.pyosis_python = pyosis_python
        self._pyosis_adapter = pyosis_adapter
        self.total_timeout_override_s = total_timeout_override_s

    @staticmethod
    def _safe_name(value: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", value).strip("._")
        return safe or "task"

    @staticmethod
    def _empty_layout() -> dict[str, Any]:
        expected = [
            "py/项目画像.md",
            *[f"py/prep/{name}" for name in CANONICAL_PROJECT_FILES[1:]],
        ]
        return {
            "project_root": None,
            "complete": False,
            "expected_files": expected,
            "present_files": [],
            "missing_files": expected,
            "file_hashes": {},
        }

    @staticmethod
    def _mark_pyosis_not_run(
        backend_status: dict[str, Any], *, failure_code: str, reason: str
    ) -> dict[str, Any]:
        """Replace the prepare-time ``ready`` marker when no build ran.

        ``ArchitectureAdapter.prepare`` creates a PyOSIS status before model
        generation so the run contract can record the selected backend.  That
        marker is not evidence that a candidate was executed, however.  Keep
        disabled PyOSIS as ``not_configured`` and explicitly mark enabled-but-
        skipped runs as ``not_run`` so a missing/invalid candidate cannot look
        like a successful backend connection.
        """

        status = dict(backend_status or {})
        if not bool(status.get("execution_enabled")):
            return status
        status.update(
            {
                "adapter": "pyosis",
                "status": "not_run",
                "model_created": False,
                "solver_converged": False,
                "validation_passed": False,
                "failure_code": failure_code,
                "reason": reason,
            }
        )
        return status

    @staticmethod
    def _parent_native_eval(architecture_id: str) -> bool:
        """T6 follows the parent train runner: no compile/layout hard gate.

        After the session goes idle, the parent solves the model already in
        the OSIS process. It does not rebuild by running ``main.py``. T1–T5
        keep the fresh-project build.
        """

        return architecture_id == "T6"

    def run(
        self,
        task: TaskSpec,
        *,
        architecture_id: str,
        seed: int,
        candidate_project: Path | None = None,
        reference_project: Path | None = None,
        candidate_generator: Callable[[TaskSpec, Path, SkillAdapter], Path] | None = None,
        started_monotonic: float | None = None,
        deadline_monotonic: float | None = None,
        source: str = "",
    ) -> RunSummary:
        adapter = get_adapter(architecture_id)
        run_dir = run_dir_from_task(
            self.runs_dir, task, architecture_id, seed, source=""
        ).resolve()
        try:
            run_dir.relative_to(self.runs_dir)
        except ValueError as exc:
            raise ValueError("run directory escaped runs root") from exc
        run_dir.mkdir(parents=True, exist_ok=False)

        total_timeout_s = float(
            self.total_timeout_override_s
            or getattr(task, "total_timeout_s", self.timeout_s)
            or self.timeout_s
        )
        if total_timeout_s <= 0:
            raise ValueError("total timeout must be positive")
        subtask_timeout_s = dict(getattr(task, "subtask_timeout_s", None) or {})
        invocation_monotonic = time.monotonic()
        # Dataset drivers may start the clock immediately before generation so
        # that the task's total timeout includes model calls.  The standalone
        # runner keeps its historical behaviour (clock starts on entry) when
        # no origin is supplied.
        run_started_monotonic = (
            float(started_monotonic)
            if started_monotonic is not None
            else invocation_monotonic
        )
        run_deadline_monotonic = (
            float(deadline_monotonic)
            if deadline_monotonic is not None
            else run_started_monotonic + total_timeout_s
        )
        wall_now = time.time()
        run_started_wall_time = wall_now - max(
            0.0, invocation_monotonic - run_started_monotonic
        )
        deadline_wall_time = run_started_wall_time + max(
            0.0, run_deadline_monotonic - run_started_monotonic
        )
        timer = {
            "total_timeout_s": total_timeout_s,
            "subtask_timeout_s": subtask_timeout_s,
            "started_at": datetime.fromtimestamp(
                run_started_wall_time, timezone.utc
            ).isoformat(),
            "deadline_at": datetime.fromtimestamp(
                deadline_wall_time, timezone.utc
            ).isoformat(),
            "elapsed_s": 0.0,
            "timeout_stage": None,
            "timeout_scope": None,
            "time_to_candidate_s": None,
            "time_to_compile_s": None,
            "time_to_model_s": None,
            "time_to_score_s": None,
            "status": "running",
        }
        (run_dir / "timer.json").write_text(
            json.dumps(timer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

        (run_dir / "input.json").write_text(
            json.dumps(task.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        osis = self._pyosis_adapter or PyOSISAdapter(
            execution_enabled=self.pyosis_enabled,
            solver_version=self.solver_version,
            timeout_s=subtask_timeout_s.get("P4", self.timeout_s),
            parent_repo=self.parent_repo,
            python_executable=self.pyosis_python,
        )
        adapter_result = adapter.prepare(task, run_dir, self.skill_adapter, osis)
        candidate_source = Path(candidate_project).expanduser().resolve() if candidate_project else None
        generation_status: dict[str, Any] = {
            "status": "not_requested" if candidate_source is not None or candidate_generator is None else "pending"
        }
        if candidate_source is None and candidate_generator is not None:
            generation_workspace = run_dir / "generated"
            generation_workspace.mkdir(parents=True, exist_ok=True)
            if time.monotonic() >= run_deadline_monotonic:
                generation_status = {
                    "status": "timeout",
                    "error_type": "TaskTimeout",
                    "error": "total task timeout expired before generation started",
                }
            else:
                try:
                    generated = candidate_generator(task, generation_workspace, self.skill_adapter)
                    candidate_source = Path(generated).expanduser().resolve()
                    if not candidate_source.is_dir():
                        raise NotADirectoryError(candidate_source)
                    # Adapters write their own generation metadata before
                    # returning.  Preserve a timeout/error status even when a
                    # partial candidate is usable for source-level diagnostics;
                    # otherwise the runner would relabel a failed framework as
                    # ``completed`` merely because the directory exists.
                    framework_meta: dict[str, Any] = {}
                    metadata_path = generation_workspace / f"{architecture_id.lower()}_generation.json"
                    try:
                        decoded = json.loads(metadata_path.read_text(encoding="utf-8"))
                        if isinstance(decoded, dict):
                            framework_meta = decoded
                    except (OSError, json.JSONDecodeError):
                        pass
                    framework_status = str(framework_meta.get("status") or "").lower()
                    generation_state = (
                        "completed"
                        if not framework_meta or framework_status in {"completed", "success"}
                        else "partial"
                    )
                    generation_status = {
                        "status": generation_state,
                        "candidate_project": str(candidate_source),
                    }
                    if framework_meta:
                        generation_status["framework_status"] = framework_meta.get("status")
                        for key in (
                        "error_type", "error", "elapsed_s", "tokens", "files_written",
                        "file_blocks", "agent_state", "execution_status",
                            "model", "framework", "framework_version", "model_calls",
                            "tool_calls", "invalid_tool_calls", "stop_reason", "framework_steps",
                            "role_budgets", "role_timeouts", "role_iterations",
                            "native_skill_count", "skill_loading", "session_idle",
                            "agents_mount", "native_project_dir", "native_project_file",
                            "native_artifacts_cleaned",
                        ):
                            if key in framework_meta:
                                generation_status[key] = framework_meta[key]
                except Exception as exc:
                    generation_status = {
                        "status": "failed",
                        "error_type": type(exc).__name__,
                        "error": str(exc),
                    }
                    # A child adapter can be killed before returning its
                    # candidate path (for example at the hard wall-clock
                    # limit).  Recover its on-disk metadata so the trace
                    # still distinguishes a framework timeout from an
                    # ordinary model/adapter exception.
                    metadata_path = generation_workspace / f"{architecture_id.lower()}_generation.json"
                    try:
                        framework_meta = json.loads(metadata_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        framework_meta = {}
                    if isinstance(framework_meta, dict):
                        framework_status = str(framework_meta.get("status") or "").lower()
                        if framework_status in {"timeout", "failed", "error"}:
                            generation_status["status"] = (
                                "timeout" if framework_status == "timeout" else "failed"
                            )
                        generation_status["framework_status"] = framework_meta.get("status")
                        for key in (
                            "error_type", "error", "elapsed_s", "tokens", "files_written",
                            "file_blocks", "agent_state", "execution_status", "model",
                            "framework", "framework_version", "model_calls", "tool_calls",
                            "invalid_tool_calls", "stop_reason", "framework_steps",
                            "role_budgets", "role_timeouts", "role_iterations",
                            "native_skill_count", "skill_loading", "session_idle",
                            "agents_mount", "native_project_dir", "native_project_file",
                            "native_artifacts_cleaned",
                        ):
                            if key in framework_meta:
                                generation_status[key] = framework_meta[key]
        candidate_phase_end = time.monotonic()
        compile_phase_end = candidate_phase_end
        model_phase_end = candidate_phase_end
        score_phase_end = candidate_phase_end
        backend_status = adapter_result.get("backend_status", {})
        model_score: dict[str, Any] = {
            "scorer": "osis-auto-testconformance-cli",
            "status": "not_run",
            "candidate_score": None,
            "reason": (
                generation_status.get("error")
                if generation_status.get("status") in {"failed", "timeout", "partial"}
                else "candidate_project was not supplied"
            ),
        }

        if candidate_source is None:
            backend_status = self._mark_pyosis_not_run(
                backend_status,
                failure_code="candidate_missing",
                reason="candidate project was not produced; PyOSIS was not invoked",
            )
            if backend_status.get("execution_enabled"):
                (run_dir / "backend_status.json").write_text(
                    json.dumps(backend_status, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            layout = self._empty_layout()
            compile_result = {"passed": False, "python_files": [], "failed_files": []}
            static_result = {
                "status": "not_run",
                "available": False,
                "candidate_score": None,
                "overall_score": None,
                "reason": "candidate_project was not supplied",
            }
            run_status = (
                "generation_failed"
                if generation_status.get("status") in {"failed", "timeout"}
                else adapter_result["status"]
            )
        else:
            candidate_root = run_dir / "candidate_project"
            materialization = materialize_project(candidate_source, candidate_root)
            layout = materialization["layout"]
            compile_result = compile_python_project(candidate_root)
            compile_phase_end = time.monotonic()
            run_pyosis = self._parent_native_eval(architecture_id) or (
                compile_result.get("passed") and layout.get("complete")
            )
            if run_pyosis and self._parent_native_eval(architecture_id):
                backend_status = osis.solve_live(
                    run_dir,
                    solve=self.solve_gate,
                    timeout_s=subtask_timeout_s.get("P4", self.timeout_s),
                    deadline_monotonic=run_deadline_monotonic,
                )
            elif run_pyosis:
                backend_status = osis.execute(
                    candidate_root,
                    run_dir,
                    solve=self.solve_gate,
                    timeout_s=subtask_timeout_s.get("P4", self.timeout_s),
                    deadline_monotonic=run_deadline_monotonic,
                )
            if run_pyosis:
                model_phase_end = time.monotonic()
                if self.parent_repo is not None:
                    scorer = ModelConformanceCLIScorer(
                        parent_repo=self.parent_repo,
                        python_executable=self.pyosis_python,
                        timeout_s=subtask_timeout_s.get("P5", 180.0),
                    )
                    remaining = max(0.01, run_deadline_monotonic - time.monotonic())
                    model_score = scorer.score(
                        candidate_root,
                        run_dir,
                        task,
                        timeout_s=min(subtask_timeout_s.get("P5", 180.0), remaining),
                    )
                else:
                    model_score = {
                        "scorer": "osis-auto-testconformance-cli",
                        "status": "scorer_unavailable",
                        "candidate_score": None,
                        "reason": "parent_repo was not supplied",
                    }
                    (run_dir / "model_score.json").write_text(
                        json.dumps(model_score, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                score_phase_end = time.monotonic()
                run_status = (
                    "executed_and_scored"
                    if backend_status.get("status") == "succeeded"
                    and model_score.get("status") == "evaluated"
                    else "scored" if model_score.get("status") == "evaluated" else "candidate_materialized"
                )
            else:
                backend_status = self._mark_pyosis_not_run(
                    backend_status,
                    failure_code=(
                        "compile_failed"
                        if not compile_result.get("passed")
                        else "candidate_layout_incomplete"
                    ),
                    reason=(
                        "candidate source did not pass compile; PyOSIS was not invoked"
                        if not compile_result.get("passed")
                        else "canonical candidate layout is incomplete; PyOSIS was not invoked"
                    ),
                )
                if backend_status.get("execution_enabled"):
                    (run_dir / "backend_status.json").write_text(
                        json.dumps(backend_status, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                run_status = "candidate_materialized"
                model_score["reason"] = "compile or canonical layout check failed"
                (run_dir / "model_score.json").write_text(
                    json.dumps(model_score, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )

            # Keep the in-process AST result as a diagnostic fallback, but run
            # it only after the PyOSIS/CLI stages so the canonical order is
            # execution → scoring → optional static diagnostics.
            static_result = evaluate_static_conformance(
                candidate_root,
                bridge_type=task.bridge_type,
                parent_repo=self.parent_repo,
                reference_project=reference_project or self.reference_project,
                is_continuous=(task.metadata or {}).get("is_continuous"),
            )

        # Runtime model extraction/scoring is deliberately a sidecar.  The
        # authoritative source score above remains the input to evaluation;
        # this call only records what can be measured from the executed PyOSIS
        # model (or an explicit ``not_available`` result when execution did
        # not produce a snapshot).
        try:
            runtime_score = evaluate_runtime_snapshot(
                run_dir / "runtime_measurements.json",
                run_dir,
                bridge_type=task.bridge_type,
                is_continuous=(task.metadata or {}).get("is_continuous"),
                is_prestressed=(task.metadata or {}).get("is_prestressed"),
                parent_repo=self.parent_repo,
            )
        except Exception as exc:  # pragma: no cover - defensive sidecar guard
            runtime_score = {
                "scorer": "osis-runtime-model-conformance",
                "source": "pyosis_runtime_snapshot",
                "status": "scorer_error",
                "candidate_score": None,
                "total_score_5": None,
                "reason": f"{type(exc).__name__}: {exc}",
            }
            (run_dir / "runtime_score.json").write_text(
                json.dumps(runtime_score, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

        (run_dir / "layout.json").write_text(
            json.dumps(layout, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (run_dir / "compile.json").write_text(
            json.dumps(compile_result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        (run_dir / "static_conformance.json").write_text(
            json.dumps(static_result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        static_score = model_score.get("candidate_score")
        if not isinstance(static_score, (int, float)):
            static_score = static_result.get("candidate_score")
        if not isinstance(static_score, (int, float)):
            static_score = 0.0
        expected_count = len(layout.get("expected_files", []))
        structure_score = (
            len(layout.get("present_files", [])) / expected_count
            if expected_count
            else 0.0
        )
        has_units_boundaries_loads = all(
            name in layout.get("present_files", [])
            for name in (
                "py/prep/_1_control.py",
                "py/prep/_7_boundary.py",
                "py/prep/_8_loadcase.py",
            )
        )
        solver_converged = bool(backend_status.get("solver_converged", False))
        validation_passed = bool(backend_status.get("validation_passed", False))
        model_created = bool(backend_status.get("model_created", False))
        artifacts_complete = bool(
            candidate_source is not None
            and layout.get("complete")
            and compile_result.get("passed")
            and model_score.get("status") == "evaluated"
            and model_created
            and validation_passed
        )

        (run_dir / "trace.json").write_text(
            json.dumps(
                {
                    "architecture_id": architecture_id,
                    "seed": seed,
                    "status": run_status,
                    "checkpoints": {
                        "P0": "completed",
                        "P1": "completed" if candidate_source else "not_started",
                        "P2": "completed" if layout.get("complete") else "failed",
                        "P3": "completed" if compile_result.get("passed") else "failed",
                        "P4": "completed" if model_created else "failed" if candidate_source else "not_started",
                        "P5": "completed" if model_score.get("status") == "evaluated" else "failed" if candidate_source else "not_started",
                        "P6": "completed",
                    },
                    "generation": generation_status,
                    "pyosis": backend_status,
                    "model_score": model_score,
                    "runtime_score": runtime_score,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )

        evaluation = evaluate_run(
            {
                "compile_passed": bool(compile_result.get("passed")),
                "model_correctness": static_score,
                "model_created": model_created,
                "execution_score": 1.0 if model_created and validation_passed else 0.0,
                "solver_converged": solver_converged,
                "solve_required": self.solve_gate,
                "constraints_correct": validation_passed,
                "validation_passed": validation_passed,
                "artifacts_complete": artifacts_complete,
                "structure_completeness": structure_score,
                "units_boundaries_loads": 1.0 if has_units_boundaries_loads else 0.0,
                "traceability": 1.0 if candidate_source is not None else 0.0,
            }
        )
        (run_dir / "evaluation.json").write_text(
            json.dumps(evaluation.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        manifest = Manifest(
            task_id=task.task_id,
            architecture_id=architecture_id,
            framework_version=adapter.spec.framework_version,
            commit="unresolved",
            model_snapshot=self.model_snapshot,
            skill_bundle_sha256=self.skill_adapter.skill_bundle_hash(),
            tool_schema_version=self.tool_schema_version,
            solver_version=self.solver_version,
            container_digest="unconfigured",
            seed=seed,
            timeout_s=int(total_timeout_s),
            token_budget=self.token_budget,
            tool_call_budget=self.tool_call_budget,
            subtask_timeout_s=subtask_timeout_s,
            failure_code=(
                generation_status.get("error")
                or backend_status.get("failure_code")
                or (model_score.get("reason") if model_score.get("status") in {"timeout", "scorer_error"} else None)
                or adapter_result.get("failure_code")
            ),
            artifact_hashes={},
        )
        final_now = time.monotonic()
        timer["elapsed_s"] = max(0.0, final_now - run_started_monotonic)
        timer["time_to_candidate_s"] = max(
            0.0, candidate_phase_end - run_started_monotonic
        )
        timer["time_to_compile_s"] = max(
            0.0, compile_phase_end - run_started_monotonic
        )
        timer["time_to_model_s"] = max(
            0.0, model_phase_end - run_started_monotonic
        )
        timer["time_to_score_s"] = max(
            0.0, score_phase_end - run_started_monotonic
        )
        timer["status"] = (
            "timeout" if final_now >= run_deadline_monotonic else run_status
        )
        # ``timeout_stage`` identifies the first bounded phase that consumed
        # the task deadline; ``timeout_scope`` distinguishes a task-wide
        # deadline from a phase/subtask timeout reported by a child process.
        generation_error_text = " ".join(
            str(generation_status.get(key) or "")
            for key in ("error_type", "error", "stop_reason")
        ).lower()
        generation_timed_out = (
            generation_status.get("status") == "timeout"
            or "timeout" in generation_error_text
            or "timed out" in generation_error_text
            or "timeoutexpired" in generation_error_text
        )
        backend_timed_out = backend_status.get("status") in {
            "timeout",
            "solve_timeout",
            "state_probe_timeout",
            "project_create_timeout",
        }
        scorer_timed_out = model_score.get("status") == "timeout"
        if timer["status"] == "timeout":
            timer["timeout_scope"] = "task"
            if generation_timed_out:
                timer["timeout_stage"] = "P1-P2"
            elif backend_timed_out:
                timer["timeout_stage"] = "P4"
            elif scorer_timed_out:
                timer["timeout_stage"] = "P5"
            else:
                timer["timeout_stage"] = "task"
        elif backend_timed_out:
            timer["timeout_scope"] = "subtask"
            timer["timeout_stage"] = "P4"
        elif scorer_timed_out:
            timer["timeout_scope"] = "subtask"
            timer["timeout_stage"] = "P5"
        elif generation_timed_out:
            timer["timeout_scope"] = "subtask"
            timer["timeout_stage"] = "P1-P2"
        (run_dir / "timer.json").write_text(
            json.dumps(timer, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        execution_trace = {
            "architecture_id": architecture_id,
            "task_id": task.task_id,
            "timer": timer,
            "generation": generation_status,
            "pyosis": backend_status,
            "model_score": model_score,
            "runtime_score": runtime_score,
            "candidate_project": str(candidate_source) if candidate_source else None,
        }
        (run_dir / "execution_trace.json").write_text(
            json.dumps(execution_trace, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        manifest.artifact_hashes = artifact_hashes(run_dir)
        manifest.write(run_dir / "manifest.json")
        return RunSummary(
            run_dir=run_dir,
            status=run_status,
            evaluation=evaluation,
            manifest=manifest.to_dict(),
        )
