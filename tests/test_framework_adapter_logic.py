"""Unit tests for framework-adapter logic that must not regress."""

from __future__ import annotations

from pathlib import Path

import pytest

from baselines.t1_direct.adapter import build_t1_prompt
from baselines.t3_smolagents.adapter import _state_to_status
from baselines._framework_common import build_prompt as build_t2_prompt
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


def test_t2_prompt_names_the_real_completion_tool():
    request = {
        "task": {"task_id": "t2", "natural_language_requirement": "build"},
    }
    prompt = build_t2_prompt(request, "[]", "{}")
    assert "check_project_completeness" in prompt
    assert "finalize_project" not in prompt


def test_t2_system_prompt_names_the_real_completion_tool():
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
    assert "check_project_completeness" in prompt
    assert "finalize_project" not in prompt


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


def test_t5_role_budgets_are_derived_from_the_shared_step_budget():
    from baselines.t5_crewai import adapter

    if not hasattr(adapter, "allocate_role_budgets"):
        pytest.fail("T5 has no shared role-budget allocator")
    allocate_role_budgets = adapter.allocate_role_budgets
    assert allocate_role_budgets(24) == {"researcher": 6, "engineer": 14, "reviewer": 4}
    assert allocate_role_budgets(200) == {"researcher": 50, "engineer": 120, "reviewer": 30}
    with pytest.raises(ValueError):
        allocate_role_budgets(2)


def test_t5_all_roles_receive_the_same_controlled_tool_interface():
    from baselines.t5_crewai import adapter

    if not hasattr(adapter, "build_role_tools"):
        pytest.fail("T5 has no shared role-tool builder")
    groups = adapter.build_role_tools(lambda function: function.__name__)
    names = {role: set(tools) for role, tools in groups.items()}
    assert set(names) == {"researcher", "engineer", "reviewer"}
    expected = {
        "list_skills", "read_skill", "read_skill_reference", "list_reference_files",
        "write_file", "search_skill_cases", "read_candidate_file",
        "check_project_completeness", "search_knowledge",
    }
    assert all(tool_names == expected for tool_names in names.values())


def test_t5_agents_explicitly_disable_code_execution_bypass():
    """CrewAI defaults are not part of the frozen adapter contract."""

    from baselines.t5_crewai import adapter

    assert adapter.AGENT_POLICY["allow_code_execution"] is False
    assert adapter.AGENT_POLICY["allow_delegation"] is False


def test_t5_role_wall_clock_budgets_fit_generation_budget():
    from baselines.t5_crewai import adapter

    # Per-role max_execution_time is intentionally removed; the function
    # now returns None so CrewAI never kills a role prematurely.
    timeouts = adapter.allocate_role_timeouts(600)
    assert timeouts is None


def test_t3_authorized_imports_do_not_allow_direct_filesystem_paths():
    from baselines.t3_smolagents.adapter import ADDITIONAL_AUTHORIZED_IMPORTS

    assert ADDITIONAL_AUTHORIZED_IMPORTS == ["json"]


def test_t3_tool_errors_are_recoverable(tmp_path: Path):
    """A bad tool argument must return TOOL_ERROR, not crash CodeAgent."""

    from baselines.t3_smolagents import adapter

    class _Reader:
        def read_skill(self, _skill_id):
            raise FileNotFoundError("missing skill")

    adapter._STATE["candidate_root"] = tmp_path / "candidate"
    adapter._STATE["candidate_root"].mkdir()
    adapter._STATE["skill_reader"] = _Reader()

    assert adapter.read_skill("missing").startswith("TOOL_ERROR:")
    assert adapter.write_file("../escape.py", "x").startswith("TOOL_ERROR:")
    assert adapter.write_file("py/prep/main.py", "x")


def test_t4_and_t5_write_errors_are_recoverable(tmp_path: Path):
    """All tool-loop adapters use the same model-visible error contract."""

    from baselines.t4_openhands import adapter as t4
    from baselines.t5_crewai import adapter as t5

    for adapter in (t4, t5):
        adapter._STATE["candidate_root"] = tmp_path / adapter.__name__.split(".")[-2]
        adapter._STATE["candidate_root"].mkdir()
        write = t4._write_file if adapter is t4 else t5.write_file
        assert write("../escape.py", "x").startswith("TOOL_ERROR:")


def test_shared_reference_and_candidate_read_tools_fail_closed(tmp_path: Path):
    """Malformed model paths stay inside the TOOL_ERROR contract."""

    from baselines._framework_common import list_reference_files, read_candidate_file

    class _Reader:
        def _skill_dir(self, _skill_id):
            return tmp_path / "skills"

    (tmp_path / "skills" / "references" / "templates" / "visible").mkdir(parents=True)
    assert list_reference_files(_Reader(), "skill", "../outside").startswith("TOOL_ERROR:")
    assert read_candidate_file(tmp_path / "candidate", None).startswith("TOOL_ERROR:")
