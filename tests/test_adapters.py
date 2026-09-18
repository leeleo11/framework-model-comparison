import json
from pathlib import Path

import pytest

from common.adapters import ADAPTER_SPECS, get_adapter
from common.pyosis_adapter import PyOSISAdapter
from common.skill_adapter import SkillAdapter
from common.task_schema import TaskSpec


def test_all_six_architectures_are_registered():
    assert list(ADAPTER_SPECS) == ["T1", "T2", "T3", "T4", "T5", "T6"]


def test_openhands_uses_native_skill_mounting():
    adapter = get_adapter("T4")
    assert adapter.spec.mounting_mode == "native"
    assert adapter.spec.interaction_mode == "progressive"


def test_unknown_architecture_is_rejected():
    with pytest.raises(KeyError):
        get_adapter("T9")


def test_all_architectures_receive_the_same_read_only_skill_bundle(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text("---\nname: Alpha\n---\nBody", encoding="utf-8")
    task = TaskSpec.from_dict(
        {
            "task_id": "mount",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )
    skill_adapter = SkillAdapter(skills)
    hashes = []
    for architecture_id in ADAPTER_SPECS:
        run_dir = tmp_path / architecture_id
        get_adapter(architecture_id).prepare(
            task,
            run_dir,
            skill_adapter,
            PyOSISAdapter(execution_enabled=False),
        )
        mount = json.loads((run_dir / "skill_mount.json").read_text(encoding="utf-8"))
        hashes.append(mount["bundle_sha256"])
        assert mount["read_only"] is True
    assert len(set(hashes)) == 1


def test_model_execution_owner_is_the_unified_runner(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text(
        "---\nname: Alpha\n---\nBody", encoding="utf-8"
    )
    task = TaskSpec.from_dict(
        {
            "task_id": "execution-owner",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
        }
    )
    adapter = get_adapter("T5")
    run_dir = tmp_path / "T5"
    adapter.prepare(
        task,
        run_dir,
        SkillAdapter(skills),
        PyOSISAdapter(execution_enabled=False),
    )
    request = json.loads((run_dir / "adapter_request.json").read_text(encoding="utf-8"))
    assert request["executor"]["owner"] == "common.runner.ExperimentRunner"
    assert request["executor"]["phase"] == "after_generation_compile"
