"""Tests for the pre-run leakage gate (final framework section 10)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from common.dataset import load_dataset_entry, to_task_spec
from common.leakage_guard import (
    prestage_issues,
    run_guard,
    scan_snapshot_for_test_names,
    task_boundary_issues,
)

BRIDGE = "osis-bridge-cantilever-box"
TPL_VISIBLE = "变截面悬浇连续梁-30+50+30"
TPL_TEST = "变截面悬浇连续梁-65+120+65"


def _bootstrap_repo(tmp_path: Path, *, skill_body: str) -> Path:
    """Synthetic parent repo: dataset entry + raw tree + mounted snapshot."""

    datasets = tmp_path / "datasets" / "test" / BRIDGE
    datasets.mkdir(parents=True)
    (datasets / "full.json").write_text(
        json.dumps(
            [
                {
                    "source": TPL_TEST,
                    "x": "建立悬臂梁完整模型。",
                    "y": f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}",
                }
            ],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    config = tmp_path / "configs"
    config.mkdir(parents=True)
    (config / "datasets.yaml").write_text(
        yaml.safe_dump(
            {
                "bridges": {
                    BRIDGE: {"visible": [TPL_VISIBLE], "train": [TPL_VISIBLE], "test": [TPL_TEST]}
                }
            },
            allow_unicode=True,
        ),
        encoding="utf-8",
    )
    raw = tmp_path / ".agents" / "skills" / BRIDGE / "references" / "templates"
    for name in (TPL_VISIBLE, TPL_TEST):
        (raw / name / "prep").mkdir(parents=True)
        (raw / name / "prep" / "_0_engine.py").write_text(f"# {name}\n", encoding="utf-8")
    snapshot = tmp_path / "checkpoints" / "epoch0" / "skills" / BRIDGE
    (snapshot / "references" / "templates" / TPL_VISIBLE / "prep").mkdir(parents=True)
    (snapshot / "SKILL.md").write_text(skill_body, encoding="utf-8")
    (snapshot / "references" / "templates" / TPL_VISIBLE / "prep" / "_0_engine.py").write_text(
        f"# {TPL_VISIBLE}\n", encoding="utf-8"
    )
    return tmp_path


def test_snapshot_scan_finds_test_name(tmp_path: Path):
    repo = _bootstrap_repo(tmp_path, skill_body=f"模板见 references/templates/{TPL_TEST}。")
    snapshot = repo / "checkpoints" / "epoch0" / "skills"
    hits = scan_snapshot_for_test_names(snapshot, {TPL_TEST})
    assert hits and TPL_TEST in hits[0]

    clean_repo = _bootstrap_repo(tmp_path / "clean", skill_body="通用建模规范。")
    clean_snapshot = clean_repo / "checkpoints" / "epoch0" / "skills"
    assert scan_snapshot_for_test_names(clean_snapshot, {TPL_TEST}) == []


def test_task_boundary_flags_leaky_spec(tmp_path: Path):
    repo = _bootstrap_repo(tmp_path, skill_body="通用建模规范。")
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=0)
    clean = task_boundary_issues(to_task_spec(entry).to_dict(), {TPL_TEST})
    assert clean == []
    leaky = {
        **to_task_spec(entry).to_dict(),
        "metadata": {"y_path": f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}"},
    }
    issues = task_boundary_issues(leaky, {TPL_TEST})
    assert any(TPL_TEST in i for i in issues)
    assert any(".agents/skills" in i for i in issues)


def test_prestage_rejects_test_template_source(tmp_path: Path):
    repo = _bootstrap_repo(tmp_path, skill_body="通用建模规范。")
    base_ok = (f".agents/skills/{BRIDGE}/references/templates/{TPL_VISIBLE}/prep/_0_engine.py",)
    assert prestage_issues(base_ok, BRIDGE, {TPL_TEST}) == []
    base_leak = (f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}/prep/_0_engine.py",)
    issues = prestage_issues(base_leak, BRIDGE, {TPL_TEST})
    assert issues and TPL_TEST in issues[0]


def test_run_guard_passes_clean_setup(tmp_path: Path):
    repo = _bootstrap_repo(tmp_path, skill_body="通用建模规范。")
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=0)
    task = to_task_spec(entry)
    report = run_guard(
        parent_repo=repo,
        bridge=BRIDGE,
        skills_dir=repo / "checkpoints" / "epoch0" / "skills",
        task_dict=task.to_dict(),
        base_files=entry.base_files,
    )
    assert report.ok, report.checks


def test_run_guard_blocks_contaminated_snapshot(tmp_path: Path):
    repo = _bootstrap_repo(tmp_path, skill_body=f"模板见 references/templates/{TPL_TEST}。")
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=0)
    task = to_task_spec(entry)
    report = run_guard(
        parent_repo=repo,
        bridge=BRIDGE,
        skills_dir=repo / "checkpoints" / "epoch0" / "skills",
        task_dict=task.to_dict(),
        base_files=entry.base_files,
    )
    assert not report.ok
    assert report.checks["snapshot_clean"]
