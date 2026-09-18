"""The single authoritative composite score for official T1-T6 runs.

The plan (工作一实验计划总纲) requires six normalized dimensions and a
weighted total with weights summing to 1.0:

    compile           0.10   (all .py compile; also the PyOSIS pre-gate)
    reference_text    0.10   (file completeness + sequence/Jaccard/token-F1)
    structure         0.10   (13 canonical files, entry, module order, API use)
    construction      0.40   (model_conformance from the parent evaluator)
    efficiency        0.15   (1 / (1 + duration / 240))
    cost              0.15   (1 / (1 + total_tokens / 100000))

Gating (per the plan):
    compile fails       -> construction 0, PyOSIS not run
    PyOSIS build fails  -> construction 0
    failed tasks keep their time/token/why; complete_success is reported
    separately from the composite score.

This module does NOT score anything itself: the six per-dimension scores come
from the parent repository's ``src/evaluation`` (via common/reference_scorer),
and PyOSIS execution is a gate. It only assembles the authoritative
``evaluation.json`` from those sources, replacing the runner's simplified
fallback composite for official runs.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import yaml

from .evaluator import EvaluationResult

DEFAULT_WEIGHTS = {
    "python_syntax": 0.10,
    "generic_text": 0.10,
    "osis_text": 0.10,
    "model_conformance": 0.40,
    "efficiency": 0.15,
    "cost": 0.15,
}


def load_weights(config_path: Path | None = None) -> dict[str, float]:
    """Read the ``weights:`` block from the (comparison-local) evaluation.yaml."""

    if config_path is None:
        config_path = Path(__file__).resolve().parents[1] / "configs" / "evaluation.yaml"
    if not Path(config_path).is_file():
        return dict(DEFAULT_WEIGHTS)
    try:
        cfg = yaml.safe_load(Path(config_path).read_text(encoding="utf-8")) or {}
        weights = cfg.get("weights") or {}
        known = set(DEFAULT_WEIGHTS)
        filtered = {k: float(v) for k, v in weights.items() if k in known}
        if not filtered:
            return dict(DEFAULT_WEIGHTS)
        return filtered
    except (OSError, ValueError, TypeError):
        return dict(DEFAULT_WEIGHTS)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def build_official_evaluation(
    *,
    run_dir: Path,
    ref_score: dict[str, Any] | None,
    model_conformance_gate: bool,
) -> EvaluationResult:
    """Assemble the authoritative evaluation from the parent evaluator's output.

    ``model_conformance_gate`` is False when compile or PyOSIS failed, which
    forces construction (model_conformance) to 0 per the plan.
    """

    run_dir = Path(run_dir)
    layout = _read_json(run_dir / "layout.json")
    compile_result = _read_json(run_dir / "compile.json")
    backend = _read_json(run_dir / "backend_status.json")

    compile_passed = bool(compile_result.get("passed", False))
    layout_complete = bool(layout.get("complete", False))
    model_created = bool(backend.get("model_created", False))
    validation_passed = bool(backend.get("validation_passed", False))
    solve_requested = bool(backend.get("solve_requested", False))
    solver_converged = bool(backend.get("solver_converged", False))
    solve_ok = (not solve_requested) or solver_converged
    reference_evaluated = bool(ref_score and ref_score.get("status") == "evaluated")

    systems: dict[str, float] = {}
    if ref_score:
        for name, value in (ref_score.get("systems") or {}).items():
            if isinstance(value, (int, float)):
                systems[name] = max(0.0, min(1.0, float(value)))
    # fill any missing dimension with 0
    for name in DEFAULT_WEIGHTS:
        systems.setdefault(name, 0.0)

    # The construction dimension comes from the parent evaluator's original
    # source reading.  Runtime model measurements are deliberately not part of
    # the official score; they may remain as diagnostic sidecars.
    construction_static = systems["model_conformance"]
    if not model_conformance_gate:
        construction_static = 0.0

    weights = load_weights()
    active_weight = sum(weights.values()) or 1.0

    def _composite(construction: float) -> float:
        """Weighted composite with the construction dimension substituted."""

        merged = {**systems, "model_conformance": construction}
        return 100.0 * sum(
            weights[name] * merged.get(name, 0.0) for name in weights
        ) / active_weight

    quality_score = _composite(construction_static)
    if solve_requested and not solve_ok:
        # Solve is a hard gate, not an extra weighted dimension.  Preserve
        # the parent-evaluator components for diagnosis, but the official
        # total is zero when a requested solve did not converge.
        quality_score = 0.0
    systems["model_conformance"] = construction_static

    construction = construction_static
    construction_ok = construction >= 0.8
    components = {
        **{f"dim_{name}": systems[name] for name in DEFAULT_WEIGHTS},
        "compile_passed": 1.0 if compile_passed else 0.0,
        "model_created": 1.0 if model_created else 0.0,
        "validation_passed": 1.0 if validation_passed else 0.0,
        "layout_complete": 1.0 if layout_complete else 0.0,
        "solve_requested": 1.0 if solve_requested else 0.0,
        "solver_converged": 1.0 if solver_converged else 0.0,
    }

    failure_reasons: list[str] = []
    if not compile_passed:
        failure_reasons.append("compile_failed")
    if not model_created:
        failure_reasons.append("model_not_created")
    if not construction_ok:
        failure_reasons.append("construction_incorrect")
    if not validation_passed:
        failure_reasons.append("validation_failed")
    if solve_requested and not solve_ok:
        failure_reasons.append("solver_not_converged")
    if not layout_complete:
        failure_reasons.append("artifacts_incomplete")
    if not reference_evaluated:
        failure_reasons.append("reference_not_evaluated")

    complete_success = bool(
        layout_complete
        and compile_passed
        and model_created
        and validation_passed
        and reference_evaluated
        and construction_ok
        and solve_ok
    )
    return EvaluationResult(
        complete_success=complete_success,
        quality_score=round(quality_score, 4),
        failure_reasons=failure_reasons,
        components=components,
        quality_score_static=round(quality_score, 4),
        quality_score_runtime=None,
    )


def overwrite_evaluation(run_dir: Path, evaluation: EvaluationResult) -> None:
    """Replace the runner's simplified evaluation.json with the official one."""

    path = Path(run_dir) / "evaluation.json"
    path.write_text(
        json.dumps(evaluation.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
