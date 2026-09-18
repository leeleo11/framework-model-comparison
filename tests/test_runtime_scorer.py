import json
from pathlib import Path

import pytest

from common.runtime_scorer import evaluate_runtime_snapshot


FIXTURE = Path(__file__).parent / "fixtures" / "runtime_measurements_cantilever.json"


def test_runtime_scorer_reuses_parent_dimensions_and_writes_sidecars(tmp_path: Path):
    result = evaluate_runtime_snapshot(
        FIXTURE,
        tmp_path,
        bridge_type="cantilever_box",
        is_continuous=True,
    )

    assert result["status"] == "evaluated"
    assert 0.0 <= result["candidate_score"] <= 1.0
    assert result["total_score_5"] == pytest.approx(result["candidate_score"] * 5.0)
    assert "D4_thickness" in result["dimensions"]
    assert result["dimensions"]["D4_thickness"]["items"]
    assert (tmp_path / "runtime_score.json").exists()
    assert (tmp_path / "runtime_score.md").exists()
    persisted = json.loads((tmp_path / "runtime_score.json").read_text(encoding="utf-8"))
    assert persisted["source"] == "pyosis_runtime_snapshot"
    assert persisted["params"]["H_root"] == 7.5


def test_runtime_scorer_missing_snapshot_is_explicit_and_nonfatal(tmp_path: Path):
    result = evaluate_runtime_snapshot(
        tmp_path / "missing.json",
        tmp_path,
        bridge_type="cantilever_box",
    )

    assert result["status"] == "not_available"
    assert result["candidate_score"] is None
    assert (tmp_path / "runtime_score.json").exists()
    assert not (tmp_path / "runtime_score.md").exists()
