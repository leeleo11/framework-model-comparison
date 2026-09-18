from pathlib import Path

import pytest

from common.skill_adapter import SkillAdapter, SkillPathError


def make_skill(root: Path, directory: str, name: str, body: str) -> None:
    skill_dir = root / directory
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: test skill\n---\n\n{body}\n",
        encoding="utf-8",
    )


def test_skill_adapter_indexes_and_reads_skills(tmp_path: Path):
    make_skill(tmp_path, "alpha", "Alpha Skill", "Use the alpha workflow.")
    adapter = SkillAdapter(tmp_path)
    skills = adapter.list_skills()
    assert [item.skill_id for item in skills] == ["alpha"]
    assert skills[0].name == "Alpha Skill"
    assert "alpha workflow" in adapter.read_skill("alpha")


def test_skill_adapter_rejects_traversal(tmp_path: Path):
    make_skill(tmp_path, "alpha", "Alpha Skill", "body")
    adapter = SkillAdapter(tmp_path)
    with pytest.raises(SkillPathError):
        adapter.read_skill("../alpha")
    with pytest.raises(SkillPathError):
        adapter.read_reference("alpha", "../../outside.md")


def test_skill_adapter_hash_is_deterministic_and_changes_on_edit(tmp_path: Path):
    make_skill(tmp_path, "alpha", "Alpha Skill", "body")
    adapter = SkillAdapter(tmp_path)
    first = adapter.skill_bundle_hash()
    second = adapter.skill_bundle_hash()
    assert first == second
    (tmp_path / "alpha" / "SKILL.md").write_text("changed", encoding="utf-8")
    assert adapter.skill_bundle_hash() != first


def test_skill_adapter_searches_markdown_cases(tmp_path: Path):
    make_skill(tmp_path, "alpha", "Alpha Skill", "bridge case: cantilever")
    (tmp_path / "alpha" / "references").mkdir()
    (tmp_path / "alpha" / "references" / "case.md").write_text(
        "Cantilever example case", encoding="utf-8"
    )
    hits = SkillAdapter(tmp_path).search_cases("cantilever")
    assert hits
    assert hits[0].skill_id == "alpha"


def test_skill_adapter_writes_fixed_bundle(tmp_path: Path):
    make_skill(tmp_path, "alpha", "Alpha Skill", "Use the alpha workflow.")
    output = tmp_path / "bundle.md"
    SkillAdapter(tmp_path).write_fixed_bundle(output)
    text = output.read_text(encoding="utf-8")
    assert "Alpha Skill" in text
    assert "Use the alpha workflow" in text
