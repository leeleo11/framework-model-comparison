from common.evaluator import evaluate_run


def complete_artifacts():
    return {
        "compile_passed": True,
        "model_correctness": 0.9,
        "solver_converged": True,
        "constraints_correct": True,
        "validation_passed": True,
        "artifacts_complete": True,
        "structure_completeness": 0.8,
        "units_boundaries_loads": 0.9,
        "validation_quality": 0.85,
        "traceability": 1.0,
    }


def test_complete_success_requires_all_gates():
    result = evaluate_run(complete_artifacts())
    assert result.complete_success is True
    assert 0 < result.quality_score <= 100
    assert result.failure_reasons == []


def test_compile_failure_cannot_be_complete_success():
    artifacts = complete_artifacts()
    artifacts["compile_passed"] = False
    result = evaluate_run(artifacts)
    assert result.complete_success is False
    assert "compile_failed" in result.failure_reasons


def test_quality_score_uses_fixed_weights():
    artifacts = complete_artifacts()
    result = evaluate_run(artifacts)
    expected = 100 * (
        0.40 * 0.9
        + 0.20 * 1.0
        + 0.15 * 0.8
        + 0.10 * 0.9
        + 0.10 * 0.85
        + 0.05 * 1.0
    )
    assert abs(result.quality_score - expected) < 1e-9


def test_solver_is_not_a_model_quality_gate_when_not_requested():
    artifacts = complete_artifacts()
    artifacts["solver_converged"] = False
    artifacts["solve_required"] = False
    result = evaluate_run(artifacts)
    assert result.complete_success is True
    assert "solver_not_converged" not in result.failure_reasons
