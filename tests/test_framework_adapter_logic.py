"""Unit tests for framework-adapter logic that must not regress."""

from __future__ import annotations

from pathlib import Path

import pytest

from baselines.t1_direct.adapter import build_t1_prompt
from baselines.t3_smolagents.adapter import _state_to_status
from common.task_schema import TaskSpec


class _EmptySkillReader:
    def skill_index(self):
        return []

    def visible_template_inventory(self):
        return {}

    def fixed_bundle_text(self):
        return "# Fixed OSIS skill bundle"

    def read_skill(self, _skill_id):
        return ""

    def read_reference(self, _skill_id, _path):
        return ""


@pytest.mark.parametrize(
    "state,expected_status",
    [
        ("success", "completed"),
        ("max_steps_error", "failed"),
        (None, "completed"),
    ],
)
def test_t3_state_to_status(state, expected_status):
    status, error = _state_to_status(state)
    assert status == expected_status
    if expected_status == "failed":
        assert error is not None
    else:
        assert error is None


def test_t1_prompt_contains_the_complete_task_json():
    task = TaskSpec.from_dict(
        {
            "task_id": "t1-full-input",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L2",
            "natural_language_requirement": "build a bridge model",
            "total_timeout_s": 123.0,
            "expected_fields": {"span": 120},
            "metadata": {"is_continuous": True},
        }
    )
    prompt = build_t1_prompt(task, _EmptySkillReader(), "osis-bridge-cantilever-box")
    assert '"difficulty": "L2"' in prompt
    assert '"total_timeout_s": 123.0' in prompt
    assert '"expected_fields": {' in prompt
    assert '"metadata": {' in prompt
    assert prompt.count("### FILE:") == 13
    assert "### FILE: py/prep/_10_stage.py" in prompt


def test_t2_system_prompt_does_not_preload_reference_paths():
    from baselines.t2_langgraph.adapter import build_t2_system_prompt

    task = TaskSpec.from_dict(
        {
            "task_id": "t2-system",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build",
        }
    )
    prompt = build_t2_system_prompt(task, _EmptySkillReader())
    assert "check_project_completeness" not in prompt
    assert "search_knowledge" not in prompt
    assert "Reference case FILES" not in prompt
    assert "list_reference_files" in prompt


def test_t4_native_skill_loader_preserves_all_skill_groups(tmp_path: Path):
    from baselines.t4_openhands import adapter

    if not hasattr(adapter, "_load_native_skills"):
        pytest.fail("T4 has no native skill loader")
    _load_native_skills = adapter._load_native_skills
    calls = []

    def fake_loader(path):
        calls.append(path)
        return ({"repo": "R"}, {"knowledge": "K"}, {"agent": "A"})

    skills = _load_native_skills(tmp_path, loader=fake_loader)
    assert calls == [tmp_path]
    assert skills == ["R", "K", "A"]


def test_t5_mounts_skills_and_uses_crewai_delegation(tmp_path: Path):
    from baselines.t5_crewai import adapter

    source = Path(adapter.__file__).read_text(encoding="utf-8")
    assert "skills=[skills_dir]" in source
    assert "FileWriterTool" in source
    assert "base_dir=str(root)" in source
    assert "Delegate work to coworker" in source
    assert "Ask question to coworker" in source
    assert not hasattr(adapter, "build_role_tools")
    assert not hasattr(adapter, "write_file")
    assert adapter.AGENT_POLICY["allow_code_execution"] is False
    assert adapter.AGENT_POLICY["allow_delegation"] is True
    skill_root = tmp_path / "skills" / "demo-skill"
    (skill_root / "references").mkdir(parents=True)
    (skill_root / "SKILL.md").write_text("---\nname: demo-skill\n---\nbody\n", encoding="utf-8")
    (skill_root / "references" / "prep.py").write_text("template = 1\n", encoding="utf-8")
    assert "prep.py" in adapter.read_skill_resource(tmp_path / "skills", "demo-skill", "")
    assert adapter.read_skill_resource(
        tmp_path / "skills", "demo-skill", "references/prep.py"
    ) == "template = 1\n"
    assert "Path must be one file" in adapter.read_skill_resource(
        tmp_path / "skills", "demo-skill", "../SKILL.md"
    )
    root = tmp_path / "candidate"
    written = adapter._materialize_file_blocks(
        "### FILE: py/prep/main.py\nprint(1)\n### FILE: ../escape.py\nprint(2)\n",
        root,
    )
    assert written == ["py/prep/main.py"]
    assert (root / "py" / "prep" / "main.py").read_text(encoding="utf-8").startswith("print(1)")
    assert not (tmp_path / "escape.py").exists()


def test_t5_file_blocks_drop_markdown_fences(tmp_path: Path):
    from baselines.t5_crewai import adapter

    root = tmp_path / "candidate"
    written = adapter._materialize_file_blocks(
        "### FILE: py/prep/_1_control.py\n"
        "```python\n"
        "print('[OK] _1_control.setup_control')\n"
        "```\n"
        "\n"
        "---\n"
        "### FILE: py/prep/_10_stage.py\n"
        "print('[OK] _10_stage.build_stage')\n",
        root,
    )
    assert written == ["py/prep/_1_control.py", "py/prep/_10_stage.py"]
    control = (root / "py" / "prep" / "_1_control.py").read_text(encoding="utf-8")
    stage = (root / "py" / "prep" / "_10_stage.py").read_text(encoding="utf-8")
    compile(control, "_1_control.py", "exec")
    compile(stage, "_10_stage.py", "exec")
    assert "```" not in control
    assert "---" not in control


def test_t5_does_not_set_a_per_role_execution_limit():
    from baselines.t5_crewai import adapter

    source = Path(adapter.__file__).read_text(encoding="utf-8")
    assert "max_execution_time" not in source
    assert not hasattr(adapter, "allocate_role_timeouts")
    assert not hasattr(adapter, "allocate_role_budgets")


def test_t3_uses_the_code_agent_interpreter_for_files():
    from baselines.t3_smolagents import adapter

    source = Path(adapter.__file__).read_text(encoding="utf-8")
    assert adapter.ADDITIONAL_AUTHORIZED_IMPORTS == ["json", "pathlib"]
    assert "os" not in adapter.ADDITIONAL_AUTHORIZED_IMPORTS
    assert "tools=[]" in source
    assert not hasattr(adapter, "write_file")
    assert not hasattr(adapter, "read_skill")


def test_t4_write_stays_on_the_framework_file_editor(tmp_path: Path):
    """Candidate writes go through OpenHands file_editor, not a harness tool."""

    from baselines.t4_openhands import adapter as t4

    source = Path(t4.__file__).read_text(encoding="utf-8")
    assert "def _write_file" not in source
    assert not hasattr(t4, "_write_file")
    assert tmp_path.is_dir()


def test_shared_reference_and_candidate_read_tools_fail_closed(tmp_path: Path):
    """Malformed model paths stay inside the TOOL_ERROR contract."""

    from baselines._framework_common import list_reference_files, read_candidate_file

    class _Reader:
        def _skill_dir(self, _skill_id):
            return tmp_path / "skills"

    (tmp_path / "skills" / "references" / "templates" / "visible").mkdir(parents=True)
    assert list_reference_files(_Reader(), "skill", "../outside").startswith("TOOL_ERROR:")
    assert read_candidate_file(tmp_path / "candidate", None).startswith("TOOL_ERROR:")
