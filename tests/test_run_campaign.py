from __future__ import annotations

import json
from pathlib import Path

from scripts.run_campaign import run_dir_for, run_is_terminal


def test_run_dir_for_uses_stable_matrix_name(tmp_path: Path):
    path = run_dir_for(
        tmp_path,
        "osis-bridge-cantilever-box",
        "gen",
        3,
        "T6",
        2,
    )
    assert path.name == "cantilever_box__gen__003__T6__seed2"


def test_run_is_terminal_requires_auditable_pair(tmp_path: Path):
    assert not run_is_terminal(tmp_path)
    (tmp_path / "evaluation.json").write_text("{}", encoding="utf-8")
    assert not run_is_terminal(tmp_path)
    (tmp_path / "manifest.json").write_text(json.dumps({}), encoding="utf-8")
    assert run_is_terminal(tmp_path)
