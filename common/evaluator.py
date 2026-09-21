"""Automatic scoring for canonical modeling run artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

QUALITY_WEIGHTS = {
    "model_correctness": 0.40,
    "execution_score": 0.20,
    "structure_completeness": 0.15,
    "units_boundaries_loads": 0.10,
    "validation_quality": 0.10,
    "traceability": 0.05,
}


@dataclass(frozen=True)
class EvaluationResult:
    complete_success: bool
    quality_score: float
    failure_reasons: list[str]
    components: dict[str, float]
    # Continuous six-dimension composite before the mandatory execution
    # gates are applied.  This is diagnostic only; the official score may be
    # zero when model construction or solving fails.
    quality_score_before_gate: float | None = None
    # The same weighted composite computed twice, differing only in where the
    # 0.40-weight construction dimension comes from: the candidate's source text
    # (static, the parent evaluator's original reading) or the model that was
    # actually built (runtime).  Both are reported; ``quality_score`` above
    # keeps the static reading for backward compatibility.
    quality_score_static: float | None = None
    quality_score_runtime: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return default
    return min(1.0, max(0.0, value))


def evaluate_run(artifacts: dict[str, Any]) -> EvaluationResult:
    compile_passed = bool(artifacts.get("compile_passed", False))
    model_created = bool(artifacts.get("model_created", True))
    solver_converged = bool(artifacts.get("solver_converged", False))
    solve_required = bool(artifacts.get("solve_required", False))
    constraints_correct = bool(artifacts.get("constraints_correct", False))
    validation_passed = bool(artifacts.get("validation_passed", False))
    artifacts_complete = bool(artifacts.get("artifacts_complete", False))

    model_correctness = _number(artifacts.get("model_correctness"))
    execution_score = _number(
        artifacts.get(
            "execution_score",
            1.0 if compile_passed and model_created and validation_passed else 0.0,
        )
    )
    components = {
        "model_correctness": model_correctness,
        "execution_score": execution_score,
        "structure_completeness": _number(
            artifacts.get("structure_completeness")
        ),
        "units_boundaries_loads": _number(
            artifacts.get("units_boundaries_loads")
        ),
        "validation_quality": _number(
            artifacts.get("validation_quality", 1.0 if validation_passed else 0.0)
        ),
        "traceability": _number(
            artifacts.get("traceability", 1.0 if artifacts_complete else 0.0)
        ),
    }
    score_before_gate = 100 * sum(
        QUALITY_WEIGHTS[name] * components[name] for name in QUALITY_WEIGHTS
    )

    failure_reasons: list[str] = []
    if not compile_passed:
        failure_reasons.append("compile_failed")
    if not model_created:
        failure_reasons.append("model_not_created")
    if model_correctness < 0.80:
        failure_reasons.append("model_incorrect")
    if solve_required and not solver_converged:
        failure_reasons.append("solver_not_converged")
    if not constraints_correct:
        failure_reasons.append("constraints_incorrect")
    if not validation_passed:
        failure_reasons.append("validation_failed")
    if not artifacts_complete:
        failure_reasons.append("artifacts_incomplete")

    model_failure = (not compile_passed) or (not model_created)
    solve_failure = solve_required and not solver_converged
    score = 0.0 if (model_failure or solve_failure) else score_before_gate

    return EvaluationResult(
        complete_success=not failure_reasons,
        quality_score=score,
        failure_reasons=failure_reasons,
        components=components,
        quality_score_before_gate=score_before_gate,
    )
