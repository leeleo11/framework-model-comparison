"""Regression tests for snapshot building and train/test near-duplicate audit."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from scripts.build_train_all_snapshot import (
    SCRUB_PLACEHOLDER,
    copy_skill_with_train_filter,
    hash_tree,
    scrub_tree,
)
from scripts.check_train_test_dup import collect_cases, shared_byte_fraction, signature


def _write_config(root: Path) -> None:
    cfg = {
        "bridges": {
            "osis-bridge-cantilever-box": {
                "visible": ["tpl-A"],
                "train": ["tpl-A", "tpl-B"],
                "test": ["tpl-HIDDEN"],
            }
        }
    }
    config = root / "configs"
    config.mkdir(parents=True, exist_ok=True)
    (config / "datasets.yaml").write_text(yaml.safe_dump(cfg), encoding="utf-8")


def _skill_tree(root: Path) -> Path:
    skill = root / ".agents" / "skills" / "osis-bridge-cantilever-box"
    (skill / "references" / "templates" / "tpl-A" / "prep").mkdir(parents=True)
    (skill / "references" / "templates" / "tpl-B" / "prep").mkdir(parents=True)
    (skill / "references" / "templates" / "tpl-HIDDEN" / "prep").mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "模板见 references/templates/tpl-HIDDEN。tpl-HIDDEN 是示例。",
        encoding="utf-8",
    )
    for name in ("tpl-A", "tpl-B", "tpl-HIDDEN"):
        (skill / "references" / "templates" / name / "prep" / "_0_engine.py").write_text(
            f"# {name}\n", encoding="utf-8"
        )
    return skill


def test_build_filter_keeps_train_removes_test(tmp_path: Path):
    root = tmp_path / "repo"
    _write_config(root)
    _skill_tree(root)
    src = root / ".agents" / "skills" / "osis-bridge-cantilever-box"
    dest = tmp_path / "out" / "skills" / "osis-bridge-cantilever-box"
    counts = copy_skill_with_train_filter(src, dest, {"tpl-A", "tpl-B"})
    assert counts["templates_included"] == 2
    assert counts["templates_skipped"] == 1
    assert (dest / "references/templates/tpl-A/prep/_0_engine.py").is_file()
    assert (dest / "references/templates/tpl-B/prep/_0_engine.py").is_file()
    assert not (dest / "references/templates/tpl-HIDDEN").exists()


def test_scrub_removes_test_names_everywhere(tmp_path: Path):
    root = tmp_path / "repo"
    _write_config(root)
    skill = _skill_tree(root)
    dest = tmp_path / "out" / "skills"
    (dest / "osis-bridge-cantilever-box").mkdir(parents=True)
    # simulate a copied tree that still contains the test name in a SKILL body
    (dest / "osis-bridge-cantilever-box" / "SKILL.md").write_text(
        "模板见 references/templates/tpl-HIDDEN。tpl-HIDDEN 是示例。",
        encoding="utf-8",
    )
    scrubbed = scrub_tree(dest, {"tpl-HIDDEN"})
    assert scrubbed == {"osis-bridge-cantilever-box/SKILL.md": 2}
    text = (dest / "osis-bridge-cantilever-box" / "SKILL.md").read_text(encoding="utf-8")
    assert "tpl-HIDDEN" not in text
    assert text.count(SCRUB_PLACEHOLDER) == 2


def test_hash_tree_deterministic(tmp_path: Path):
    root = tmp_path / "tree"
    (root / "a").mkdir(parents=True)
    (root / "a" / "f.py").write_text("x=1\n", encoding="utf-8")
    files1, agg1 = hash_tree(root)
    files2, agg2 = hash_tree(root)
    assert files1 == files2
    assert agg1 == agg2
    (root / "a" / "f.py").write_text("x=2\n", encoding="utf-8")
    _, agg3 = hash_tree(root)
    assert agg3 != agg1


def _make_template(path: Path, rel_files: dict[str, str]) -> None:
    for rel, text in rel_files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def test_shared_byte_fraction_ignores_boilerplate(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    boiler = "engine = Engine()\n"
    body_a = "node = engine.node.add(1, 0, 0, 0)\n" * 50
    body_b = "node = engine.node.add(1, 5, 0, 0)\n" * 50
    _make_template(a, {"prep/_0_engine.py": boiler, "prep/_5_node.py": body_a})
    _make_template(b, {"prep/_0_engine.py": boiler, "prep/_5_node.py": body_b})
    probe = shared_byte_fraction(a, b)
    # shared boilerplate is a tiny fraction; the discriminating file differs
    assert probe["fraction"] < 0.1


def test_shared_byte_fraction_flags_renamed_project(tmp_path: Path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    files = {
        "prep/_0_engine.py": "engine = Engine()\n",
        "prep/_1_control.py": "c = Control(engine)\n" * 30,
        "prep/_2_property.py": "p = Property('C55')\n" * 40,
        "prep/_5_node.py": "node = engine.node.add(1, 0, 0, 0)\n" * 50,
        "prep/_8_loadcase.py": "load = engine.load.add('DL', 1.0)\n" * 60,
    }
    _make_template(a, files)
    renamed = {rel: text for rel, text in files.items()}
    # a renamed project differs only in a handful of parameter values
    renamed["prep/_8_loadcase.py"] = renamed["prep/_8_loadcase.py"].replace(
        "load.add('DL', 1.0)", "load.add('DL', 1.1)"
    )
    _make_template(b, renamed)
    probe = shared_byte_fraction(a, b)
    assert probe["fraction"] > 0.6
    assert len(probe["identical_files"]) >= 3


def test_signature_stable(tmp_path: Path):
    a = tmp_path / "a"
    _make_template(a, {"prep/main.py": "x\n"})
    assert signature({k: hashlib.sha256(b"x\n").hexdigest() for k in ("prep/main.py",)}) == signature(
        {k: hashlib.sha256(b"x\n").hexdigest() for k in ("prep/main.py",)}
    )
