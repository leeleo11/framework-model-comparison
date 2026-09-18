"""Unit tests for the reference-aware scorer (parent-repo evaluator wrapper)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from common.reference_scorer import (
    BRIDGE_TYPE_MAP,
    _script,
    resolve_parent_python,
    score,
)


def test_bridge_type_map_covers_all_comparison_types():
    assert BRIDGE_TYPE_MAP["cantilever_box"] == "cantilever_box"
    assert BRIDGE_TYPE_MAP["rigid_frame"] == "rigid_frame"
    assert BRIDGE_TYPE_MAP["precast_t_girder"] == "t_girder"
    assert BRIDGE_TYPE_MAP["precast_small_box"] == "precast_small_box"
    assert BRIDGE_TYPE_MAP["conventional_box"] == "cast_in_place_box"
    assert BRIDGE_TYPE_MAP["hollow_slab"] == "hollow_slab"


def test_script_is_utf8_safe_and_imports_evaluate(tmp_path: Path):
    ref = tmp_path / "ref"
    cand = tmp_path / "cand"
    script = _script(
        reference_root=ref,
        candidate_root=cand,
        config_path=tmp_path / "eval.yaml",
        bridge_type="cantilever_box",
        is_continuous=True,
        runtime_stats={"duration_s": 60.0, "total_tokens": 1000},
    )
    assert "from evaluation.evaluate import evaluate" in script
    assert "runtime_stats" in script
    assert "evaluate(" in script
    assert "reference" in script and "candidate" in script and "config" in script


def test_score_degrades_gracefully_without_parent_evaluator(tmp_path: Path):
    """A parent repo without the evaluation package must not crash; it writes a result."""
    ref = tmp_path / "ref"
    cand = tmp_path / "cand"
    ref.mkdir()
    cand.mkdir()
    result = score(
        reference_root=ref,
        candidate_root=cand,
        parent_repo=tmp_path,  # no src/evaluation inside -> import fails, handled
        run_dir=tmp_path / "out",
        bridge_type="cantilever_box",
    )
    assert result["status"] in ("scorer_error", "config_unavailable")
    assert (tmp_path / "out" / "reference_score.json").is_file()


def test_resolve_parent_python_prefers_venv(tmp_path: Path):
    venv = tmp_path / ".venv" / "Scripts"
    venv.mkdir(parents=True)
    fake = venv / "python.exe"
    fake.write_bytes(b"")
    assert resolve_parent_python(tmp_path).endswith("python.exe")
