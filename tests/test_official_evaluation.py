"""Tests for the official (src/evaluation-based) composite scoring."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.official_evaluation import (
    build_official_evaluation,
    load_weights,
    overwrite_evaluation,
)


def _ref_score(systems: dict[str, float]) -> dict:
    return {"status": "evaluated", "overall_score": 0.5, "systems": systems}


def _write_run(run_dir: Path, *, compile_passed=True, model_created=True,
               validation_passed=True, layout_complete=True) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "compile.json").write_text(json.dumps({"passed": compile_passed}), encoding="utf-8")
    (run_dir / "backend_status.json").write_text(
        json.dumps({"model_created": model_created, "validation_passed": validation_passed}),
        encoding="utf-8")
    (run_dir / "layout.json").write_text(
        json.dumps({"complete": layout_complete}), encoding="utf-8")


def test_weights_sum_to_one():
    weights = load_weights()
    assert abs(sum(weights.values()) - 1.0) < 1e-9


def test_complete_success_requires_all_gates(tmp_path: Path):
    _write_run(tmp_path, compile_passed=True, model_created=True, validation_passed=True)
    eval_result = build_official_evaluation(
        run_dir=tmp_path,
        ref_score=_ref_score({k: 1.0 for k in
                              ("generic_text", "osis_text", "python_syntax",
                               "model_conformance", "efficiency", "cost")}),
        model_conformance_gate=True,
    )
    assert eval_result.complete_success is True
    assert abs(eval_result.quality_score - 100.0) < 0.01
    assert eval_result.failure_reasons == []


def test_compile_failure_zeroes_construction_and_gates(tmp_path: Path):
    _write_run(tmp_path, compile_passed=False, model_created=False)
    eval_result = build_official_evaluation(
        run_dir=tmp_path,
        ref_score=_ref_score({k: 1.0 for k in
                              ("generic_text", "osis_text", "python_syntax",
                               "model_conformance", "efficiency", "cost")}),
        model_conformance_gate=False,
    )
    assert eval_result.components["dim_model_conformance"] == 0.0
    assert "compile_failed" in eval_result.failure_reasons
    assert "model_not_created" in eval_result.failure_reasons
    assert eval_result.complete_success is False
    # the composite still reflects the other dimensions (0.10+0.10+0.10+0.15+0.15=0.60)
    assert abs(eval_result.quality_score - 60.0) < 0.01


def test_pyosis_failure_zeroes_construction(tmp_path: Path):
    _write_run(tmp_path, compile_passed=True, model_created=False)
    eval_result = build_official_evaluation(
        run_dir=tmp_path,
        ref_score=_ref_score({k: 1.0 for k in
                              ("generic_text", "osis_text", "python_syntax",
                               "model_conformance", "efficiency", "cost")}),
        model_conformance_gate=False,
    )
    assert eval_result.components["dim_model_conformance"] == 0.0
    assert "model_not_created" in eval_result.failure_reasons


def test_missing_reference_marks_reference_not_evaluated(tmp_path: Path):
    _write_run(tmp_path)
    eval_result = build_official_evaluation(
        run_dir=tmp_path, ref_score=None, model_conformance_gate=True
    )
    assert "reference_not_evaluated" in eval_result.failure_reasons
    assert eval_result.complete_success is False


def test_overwrite_writes_evaluation_json(tmp_path: Path):
    _write_run(tmp_path)
    eval_result = build_official_evaluation(
        run_dir=tmp_path,
        ref_score=_ref_score({k: 1.0 for k in
                              ("generic_text", "osis_text", "python_syntax",
                               "model_conformance", "efficiency", "cost")}),
        model_conformance_gate=True,
    )
    overwrite_evaluation(tmp_path, eval_result)
    written = json.loads((tmp_path / "evaluation.json").read_text(encoding="utf-8"))
    assert written["quality_score"] == eval_result.quality_score
    assert written["complete_success"] is True
