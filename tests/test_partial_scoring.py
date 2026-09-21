"""Regression: partial candidates are kept and scored, not crushed to 0.

The completeness gate (all 13 canonical files) is a SCORING dimension and a
complete_success gate, NOT a hard rejection: a framework that writes some
files must keep them, get scored on the applicable dimensions, and only lose
``complete_success``. This lost T4's 4 files and T5's 12 files before.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.run_dataset import (
    FROZEN_GENERATION_STEP_TIMEOUT_S,
    _file_hashes,
    _effective_generation_timeout_s,
    _has_candidate_progress,
    _is_infra_error,
    _is_permanent_infra_error,
    _persist_framework_metadata,
    _read_prestage_manifest,
    _require_completed,
    _runtime_stats,
    _write_prestage_manifest,
)


def test_formal_timeout_matches_the_parent_repo_generation_budget():
    # The generation cap is the parent repo's own bridge-building limit
    # (opencode_client.DEFAULT_TIMEOUT = 6000s, mirrored by train/runner.py and
    # dashboard/jobs.py), so no architecture is truncated below what the native
    # OSIS-AI toolchain grants itself.
    assert FROZEN_GENERATION_STEP_TIMEOUT_S == 6000.0
    assert _effective_generation_timeout_s(7800.0) == 6000.0
    # An explicitly shorter run still keeps the post-processing reserve.
    assert _effective_generation_timeout_s(3600.0) == 1800.0


def test_infrastructure_marker_matching_is_case_insensitive():
    assert _is_infra_error("RateLimitError: insufficient balance")
    assert _is_infra_error("ConnectionError: upstream unavailable")
    assert _is_infra_error("framework subprocess exceeded 20s budget")
    assert not _is_infra_error("model returned valid tool calls")
    assert _is_permanent_infra_error("HTTP 402: insufficient balance")
    assert not _is_permanent_infra_error("HTTP 429: temporary rate limit")


def test_progress_probe_ignores_unchanged_prestaged_files_but_detects_changes(tmp_path: Path):
    candidate = tmp_path / "candidate_project"
    candidate.mkdir()
    staged = candidate / "py" / "prep" / "main.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("base\n", encoding="utf-8")
    staged_files = [str(staged)]
    staged_hashes = _file_hashes(staged_files)

    assert not _has_candidate_progress(
        candidate, preexisting_files=staged_files, preexisting_hashes=staged_hashes
    )
    (candidate / "py" / "项目画像.md").write_text("generated\n", encoding="utf-8")
    assert _has_candidate_progress(
        candidate, preexisting_files=staged_files, preexisting_hashes=staged_hashes
    )


def test_prestage_manifest_round_trips_hashes(tmp_path: Path):
    staged = tmp_path / "candidate" / "py" / "prep" / "main.py"
    staged.parent.mkdir(parents=True)
    staged.write_text("base\n", encoding="utf-8")
    files = [str(staged)]
    hashes = _file_hashes(files)
    _write_prestage_manifest(tmp_path, files, hashes)
    loaded_files, loaded_hashes = _read_prestage_manifest(tmp_path)
    assert loaded_files == files
    assert loaded_hashes == hashes


def test_frozen_config_records_task_and_resource_contract(tmp_path: Path):
    from scripts.run_dataset import _write_frozen_config
    from common.task_schema import TaskSpec

    task = TaskSpec.from_dict(
        {
            "task_id": "frozen",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build",
            "total_timeout_s": 123,
            "subtask_timeout_s": {"P2": 60},
        }
    )
    args = SimpleNamespace(
        architecture="T2", model="mimo-v2.5", base_url="http://gateway/v1",
        temperature=0.0, seed=7, split="test", total_timeout_s=None,
        no_pyosis=False, solve_gate=False, max_attempts=4, retry_backoff_s=120.0,
        # Resolved by main() from --parent-repo / $OSIS_PARENT_REPO / the
        # recorded config; T2's interpreter is framework-local so it goes unused.
        parent_repo=tmp_path,
    )
    _write_frozen_config(tmp_path, args, tmp_path / "skills", task=task)
    frozen = json.loads((tmp_path / "frozen_config.json").read_text(encoding="utf-8"))
    assert frozen["architecture"] == "T2"
    assert frozen["task_id"] == "frozen"
    assert frozen["total_timeout_s"] == 123.0
    assert frozen["subtask_timeout_s"]["P2"] == 60.0
    assert frozen["pyosis_enabled"] is True
    assert frozen["framework_python"]
    assert frozen["environment_manifest"].endswith(".venvs\\environments.json")


def test_require_completed_allows_partial_candidate(tmp_path: Path):
    """4/13 canonical files are KEPT and scored (not raised as a fatal gap)."""
    meta = {"status": "completed", "files_written": ["py/prep/_0_engine.py", "py/prep/main.py"]}
    candidate = tmp_path / "candidate_project"
    (candidate / "py" / "prep").mkdir(parents=True)
    (candidate / "py" / "prep" / "_0_engine.py").write_text("x\n", encoding="utf-8")
    # Must NOT raise: the partial is preserved for dimension scoring.
    _require_completed(meta, "T4", candidate)


def test_require_completed_rejects_zero_files_but_keeps_failed_partial(tmp_path: Path):
    with pytest.raises(RuntimeError):
        _require_completed({"status": "completed", "files_written": []}, "T1", tmp_path)
    candidate = tmp_path / "candidate_project"
    (candidate / "py" / "prep").mkdir(parents=True)
    (candidate / "py" / "prep" / "_0_engine.py").write_text("x\n", encoding="utf-8")
    _require_completed(
        {"status": "failed", "error": "x"},
        "T2",
        candidate,
    )


def test_persist_framework_metadata_marks_hard_timeout_with_partial_files(tmp_path: Path):
    candidate = tmp_path / "candidate_project"
    (candidate / "py").mkdir(parents=True)
    (candidate / "py" / "partial.py").write_text("# partial\n", encoding="utf-8")
    payload = _persist_framework_metadata(
        tmp_path,
        "T4",
        {"architecture_id": "T4", "status": "timeout", "error_type": "TimeoutExpired"},
    )
    assert payload["status"] == "timeout"
    assert payload["files_written"] == ["py/partial.py"]
    saved = json.loads((tmp_path / "t4_generation.json").read_text(encoding="utf-8"))
    assert saved["status"] == "timeout"


def test_runtime_stats_does_not_turn_missing_tokens_into_perfect_cost(tmp_path: Path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "t4_generation.json").write_text(
        json.dumps({"tokens": {"total_tokens": 0}}), encoding="utf-8"
    )
    stats = _runtime_stats(tmp_path, "T4")
    assert "total_tokens" not in stats


def test_official_evaluation_scores_partial_structure(tmp_path: Path):
    """A partial layout keeps a diagnostic composite but fails the official gate."""
    from common.official_evaluation import build_official_evaluation

    (tmp_path / "compile.json").write_text(json.dumps({"passed": True}), encoding="utf-8")
    (tmp_path / "backend_status.json").write_text(
        json.dumps({"model_created": False, "validation_passed": False}), encoding="utf-8"
    )
    (tmp_path / "layout.json").write_text(
        json.dumps({"complete": False, "expected_files": ["a"] * 13, "present_files": ["a"]}), encoding="utf-8"
    )
    systems = {
        "generic_text": 0.4, "osis_text": 0.25, "python_syntax": 1.0,
        "model_conformance": 0.0, "efficiency": 1.0, "cost": 0.8,
    }
    result = build_official_evaluation(
        run_dir=tmp_path,
        ref_score={"status": "evaluated", "systems": systems},
        model_conformance_gate=False,  # model not created -> construction zeroed
    )
    assert result.quality_score == 0.0
    assert result.quality_score_before_gate > 0.0
    assert result.complete_success is False  # but not complete
    assert result.components["dim_osis_text"] == 0.25  # partial structure counted


def test_framework_subprocess_isolated_to_generation_workspace(tmp_path: Path, monkeypatch):
    """Framework-side relative writes cannot land in the repository root."""

    import subprocess

    import scripts.run_dataset as driver

    request_dir = tmp_path / "generated"
    request_dir.mkdir()
    request = request_dir / "generation_request.json"
    request.write_text("{}", encoding="utf-8")
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return subprocess.CompletedProcess(
            command, 0, json.dumps({"status": "completed"}), ""
        )

    monkeypatch.setitem(driver.FRAMEWORK_LOCAL_VENVS, "T3", Path(sys.executable))
    monkeypatch.setattr(driver.subprocess, "run", fake_run)

    result = driver._framework_generation("T3", request, env={}, parent_repo=tmp_path)

    assert result["status"] == "completed"
    assert calls
    assert Path(calls[0][1]["cwd"]) == request_dir
    assert str(driver.PROJECT_ROOT) in calls[0][1]["env"]["PYTHONPATH"]
