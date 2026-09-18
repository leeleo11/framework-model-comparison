import json
from dataclasses import replace
from pathlib import Path

import pytest

from common.dataset import (
    DatasetError,
    LeakageError,
    load_dataset_entry,
    prestage_base_files,
    reference_record,
    to_task_spec,
)

BRIDGE = "osis-bridge-cantilever-box"
TPL_VISIBLE = "变截面悬浇连续梁-30+50+30"
TPL_TEST = "变截面悬浇连续梁-65+120+65"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    datasets = tmp_path / "datasets" / "test" / BRIDGE
    _write_json(
        datasets / "full.json",
        [
            {
                "source": TPL_TEST,
                "x": "建立悬臂梁完整模型,跨径 65+120+65m。",
                "y": f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}",
            }
        ],
    )
    base_files = [
        f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}/prep/_0_engine.py",
        f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}/prep/main.py",
    ]
    _write_json(
        datasets / "gen.json",
        [
            {
                "source": TPL_TEST,
                "module": "property",
                "target": "prep/_2_property.py",
                "x": "补全 _2_property.py。",
                "base_files": base_files,
                "y": f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}/prep/_2_property.py",
            }
        ],
    )
    _write_json(
        datasets / "edit.json",
        [
            {
                "source": TPL_TEST,
                "source_b": TPL_VISIBLE,
                "module": "property",
                "target": "prep/_2_property.py",
                "x": "修改 _2_property.py。",
                "base_files": base_files,
                "y": f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}/prep/_2_property.py",
            }
        ],
    )
    templates = tmp_path / ".agents" / "skills" / BRIDGE / "references" / "templates"
    for name in (TPL_TEST,):
        for rel in ("prep/_0_engine.py", "prep/main.py", "prep/_2_property.py"):
            target = templates / name / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(f"# {name} {rel}\n", encoding="utf-8")
    return tmp_path


def test_load_full_entry_and_map_to_task_spec(repo: Path):
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=0)
    assert entry.task_id == "cantilever_box__full__000"
    assert entry.task_form == "whole"
    assert entry.requirement.startswith("建立悬臂梁完整模型")
    task = to_task_spec(entry)
    assert task.task_id == "cantilever_box__full__000"
    assert task.natural_language_requirement == entry.requirement


def test_model_facing_task_spec_has_no_answer_provenance(repo: Path):
    """input.json / adapter_request.json serialize task.to_dict(); it must be clean."""
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=0)
    task = to_task_spec(entry)
    assert task.metadata == {"is_continuous": True}  # scoring intent only, no answer info
    assert task.initial_project_snapshot is None
    serialized = json.dumps(task.to_dict(), ensure_ascii=False)
    assert TPL_TEST not in serialized
    assert ".agents/skills" not in serialized
    assert "references/templates" not in serialized


def test_edit_task_spec_still_valid_and_path_clean(repo: Path):
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="edit", index=0)
    assert entry.task_form == "modify"
    task = to_task_spec(entry)
    assert task.initial_project_snapshot == {"staged": "via_base_files"}
    serialized = json.dumps(task.to_dict(), ensure_ascii=False)
    assert TPL_TEST not in serialized
    assert TPL_VISIBLE not in serialized
    assert ".agents/skills" not in serialized


def test_visible_template_inventory_reports_only_snapshot_templates(tmp_path: Path):
    """Option B: the shared prompt fragment lists visible cases from the mount."""
    from common.skill_adapter import SkillAdapter

    snapshot = tmp_path / "snapshot"
    (snapshot / BRIDGE / "references" / "templates" / TPL_VISIBLE).mkdir(parents=True)
    (snapshot / BRIDGE / "SKILL.md").write_text("---\nname: OSIS bridge\n---\n", encoding="utf-8")
    inventory = SkillAdapter(snapshot).visible_template_inventory()
    # A leak-free snapshot mount reports exactly the visible case; the hidden
    # test template (only present in the raw tree) never appears.
    assert inventory == {BRIDGE: [TPL_VISIBLE]}
    assert TPL_TEST not in inventory[BRIDGE]


def test_system_prompt_lists_visible_cases_but_no_hidden(repo: Path):
    """Option B: prompt names visible cases; hidden test template stays out."""
    from baselines.t2_langgraph.adapter import build_t2_system_prompt
    from common.skill_adapter import SkillAdapter

    snapshot = repo / "snapshot"
    (snapshot / BRIDGE / "references" / "templates" / TPL_VISIBLE).mkdir(parents=True)
    (snapshot / BRIDGE / "SKILL.md").write_text(
        "---\nname: OSIS bridge\n---\n通用建模规范。", encoding="utf-8",
    )
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=0)
    prompt = build_t2_system_prompt(to_task_spec(entry), SkillAdapter(snapshot))
    assert TPL_VISIBLE in prompt          # visible reference case is advertised
    assert TPL_TEST not in prompt          # hidden test template must not leak
    assert ".agents/skills" not in prompt


def test_reference_record_is_separate_and_private(repo: Path):
    """The standard answer lives only in the scorer-private record."""
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=0)
    record = reference_record(entry)
    assert record["source"] == TPL_TEST
    assert record["bridge_type"] == "cantilever_box"
    assert TPL_TEST in record["y_path"] or record["y_path"].rstrip("/").endswith(TPL_TEST)
    serialized_task = json.dumps(to_task_spec(entry).to_dict(), ensure_ascii=False)
    assert TPL_TEST not in serialized_task


def test_prestage_copies_base_files_but_never_the_answer(repo: Path):
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="gen", index=0)
    candidate = repo / "candidate"
    copied = prestage_base_files(entry, repo, candidate)
    assert (candidate / "py/prep/_0_engine.py").is_file()
    assert (candidate / "py/prep/main.py").is_file()
    assert not (candidate / "py/prep/_2_property.py").exists()
    assert all(path.endswith(".py") for path in copied)


def test_prestage_rejects_answer_inside_base_files(repo: Path):
    entry = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="gen", index=0)
    poisoned = replace(
        entry,
        base_files=(
            f".agents/skills/{BRIDGE}/references/templates/{TPL_TEST}/prep/_2_property.py",
        ),
    )
    with pytest.raises(LeakageError):
        prestage_base_files(poisoned, repo, repo / "candidate2")


def test_load_dataset_entry_validation(repo: Path):
    with pytest.raises(DatasetError):
        load_dataset_entry(repo, split="test", bridge_skill="osis-bridge-nope", form="full", index=0)
    with pytest.raises(DatasetError):
        load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=9)


def test_y_template_preserved_for_gen_edit(repo: Path):
    """gen/edit y_relative drops the template name; y_template must keep it
    so the reference PROJECT directory can be located for official scoring."""
    full = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="full", index=0)
    assert full.y_template == TPL_TEST
    assert full.y_relative == TPL_TEST
    gen = load_dataset_entry(repo, split="test", bridge_skill=BRIDGE, form="gen", index=0)
    assert gen.y_template == TPL_TEST
    assert gen.y_relative == "prep/_2_property.py"
