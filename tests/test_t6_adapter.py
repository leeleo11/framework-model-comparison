"""Pure adapter-contract tests for T6 (no OSIS server or model calls)."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path

from baselines.t6_osisai import adapter


def test_t6_opencode_config_uses_request_model_and_limits(tmp_path: Path):
    if not hasattr(adapter, "_prepare_isolated_env"):
        raise AssertionError("T6 has no isolated environment builder")
    _prepare_isolated_env = adapter._prepare_isolated_env
    skills = tmp_path / "skills"
    (skills / "osis-engine").mkdir(parents=True)
    (skills / "osis-engine" / "SKILL.md").write_text("skill", encoding="utf-8")

    _prepare_isolated_env(
        tmp_path / "iso",
        skills,
        "http://gateway/v1",
        model="glm-5.3-flash",
        max_tokens=32000,
        temperature=0.2,
    )
    config = json.loads((tmp_path / "iso" / ".agents" / "opencode.json").read_text())
    assert config["model"] == "comparison/glm-5.3-flash"
    model = config["provider"]["comparison"]["models"]["glm-5.3-flash"]
    assert model["limit"]["output"] == 32000
    assert model["options"]["temperature"] == 0.2
    # The native adapter must retain the same reasoning/context contract as
    # the packaged OSIS-AI launcher.  The output cap is still run-scoped, but
    # the context window and modalities must not silently shrink.
    assert model["reasoning"] is True
    assert model["modalities"] == {
        "input": ["text", "image"],
        "output": ["text"],
    }
    assert model["limit"]["context"] == 256000
    assert config["instructions"] == [
        str((tmp_path / "iso" / ".agents" / "AGENTS.md").resolve())
    ]


def test_t6_isolated_env_matches_packaged_launcher_and_hides_user_skills(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "osis-engine").mkdir(parents=True)
    (skills / "osis-engine" / "SKILL.md").write_text("skill", encoding="utf-8")

    env = adapter._prepare_isolated_env(
        tmp_path / "iso", skills, "http://gateway/v1"
    )

    # The install contains no ``opencode/Python311`` tree.  Do not export a
    # stale PYTHONHOME that would poison Python-backed OpenCode tools.
    assert "PYTHONHOME" not in env
    assert env["OPENCODE_DISABLE_CLAUDE_CODE"] == "1"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] == "1"
    assert env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] == "1"


def test_t6_isolated_env_copies_packaged_agents_instructions(tmp_path: Path, monkeypatch):
    opencode_dir = tmp_path / "opencode"
    packaged_agents = opencode_dir / ".agents"
    packaged_agents.mkdir(parents=True)
    packaged_text = "# packaged OSIS-AI instructions\nuse the current project\n"
    (packaged_agents / "AGENTS.md").write_text(packaged_text, encoding="utf-8")
    monkeypatch.setattr(adapter, "OPENCODE_DIR", opencode_dir)

    skills = tmp_path / "skills"
    (skills / "osis-engine").mkdir(parents=True)
    (skills / "osis-engine" / "SKILL.md").write_text("skill", encoding="utf-8")

    adapter._prepare_isolated_env(tmp_path / "iso", skills, "http://gateway/v1")

    mounted = tmp_path / "iso" / ".agents" / "AGENTS.md"
    assert mounted.read_text(encoding="utf-8") == packaged_text


def test_t6_isolated_env_exports_normal_osis_ports(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("OSIS_HTTP_PORT", raising=False)
    (tmp_path / "skills").mkdir()
    env = adapter._prepare_isolated_env(
        tmp_path / "iso", tmp_path / "skills", "http://gateway/v1"
    )

    assert env["OSIS_AI_PORT"] == str(adapter.T6_PORT)
    assert env["OSIS_HTTP_PORT"] == "18080"


def test_t6_prompt_is_the_production_hand_off():
    """T6 must receive the same hand-off the parent repo's train runner sends.

    Production sends ``帮我建桥:<x>`` for whole projects and ``<x>`` verbatim for
    gen/edit, and lets AGENTS.md own the workflow.  Any benchmark scaffolding
    added here would replace the native workflow and stop measuring OSIS-AI.
    """

    request = {
        "bridge_skill": "osis-bridge-cantilever-box",
        "task": {
            "task_id": "cantilever_box__full__000",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "natural_language_requirement": "从零生成完整桥梁模型。",
        },
    }

    prompt = adapter._build_t6_prompt(request)

    assert prompt.startswith("你是 OSIS-AI")
    assert "AGENTS.md" in prompt
    # The task text reaches the model verbatim, prefixed the way the train
    # runner prefixes whole-project tasks.
    assert "帮我建桥:从零生成完整桥梁模型。" in prompt
    # No benchmark scaffolding: the completion contract lives in AGENTS.md.
    assert "canonical" not in prompt
    assert "入口导入与函数定义名称一致" not in prompt
    assert "set_gravity_acceleration" not in prompt
    assert "最小 smoke test" not in prompt


def test_t6_prompt_passes_module_and_edit_task_text_through():
    module_prompt = adapter._build_t6_prompt(
        {
            "bridge_skill": "osis-bridge-cantilever-box",
            "task": {
                "task_id": "cantilever_box__gen__000",
                "bridge_type": "cantilever_box",
                "task_form": "module",
                "natural_language_requirement": "仅生成材料模块。",
            },
        }
    )
    modify_prompt = adapter._build_t6_prompt(
        {
            "bridge_skill": "osis-bridge-cantilever-box",
            "task": {
                "task_id": "cantilever_box__edit__000",
                "bridge_type": "cantilever_box",
                "task_form": "modify",
                "natural_language_requirement": "更新目标模块。",
            },
        }
    )

    # gen/edit keep the raw task text (production does not add the 帮我建桥: prefix).
    assert "仅生成材料模块。" in module_prompt
    assert "帮我建桥" not in module_prompt
    assert "更新目标模块。" in modify_prompt
    assert "帮我建桥" not in modify_prompt


def test_t6_syncs_the_native_project_py_tree_to_candidate(tmp_path: Path):
    if not hasattr(adapter, "_sync_native_project"):
        raise AssertionError("T6 has no native-project synchronizer")
    _sync_native_project = adapter._sync_native_project
    native = tmp_path / "native"
    candidate = tmp_path / "candidate_project"
    (native / "py" / "prep").mkdir(parents=True)
    (native / "py" / "prep" / "main.py").write_text("print('native')\n", encoding="utf-8")

    copied = _sync_native_project(native, candidate)

    assert copied == ["py/prep/main.py"]
    assert (candidate / "py" / "prep" / "main.py").read_text(encoding="utf-8") == (
        "print('native')\n"
    )


def test_t6_native_project_can_use_candidate_root(tmp_path: Path, monkeypatch):
    candidate = tmp_path / "candidate_project"
    (candidate / "py" / "prep").mkdir(parents=True)
    (candidate / "py" / "prep" / "base.py").write_text("# staged\n", encoding="utf-8")

    class FakeProject:
        def create(self, _project_type, project_file):
            self.project_file = Path(project_file)
            Path(project_file).write_text("sis", encoding="utf-8")
            self.project_file.with_suffix("").mkdir(parents=True, exist_ok=True)

        def get_directory(self):
            return str(candidate)

    class FakeEngine:
        def __init__(self):
            self.project = FakeProject()

    engine_module = types.ModuleType("pyosis.core.engine")
    engine_module.OSISEngine = FakeEngine
    core_module = types.ModuleType("pyosis.core")
    pyosis_module = types.ModuleType("pyosis")
    monkeypatch.setitem(sys.modules, "pyosis", pyosis_module)
    monkeypatch.setitem(sys.modules, "pyosis.core", core_module)
    monkeypatch.setitem(sys.modules, "pyosis.core.engine", engine_module)

    created = adapter._create_native_project(
        tmp_path / ".osisai_t6", project_root=candidate
    )

    assert created == candidate.resolve()
    assert (candidate / "py" / "prep" / "base.py").read_text(encoding="utf-8") == "# staged\n"
    # The .sis file is intentionally a sibling; an in-project
    # ``candidate_project/project.sis`` would make OSIS create a nested
    # ``candidate_project/project`` directory.
    assert not (candidate / "project.sis").is_file()
    assert (candidate.parent / "candidate_project.sis").is_file()


def test_t6_cleanup_removes_native_outputs_but_keeps_py_sources(tmp_path: Path):
    candidate = tmp_path / "candidate_project"
    (candidate / "py" / "prep").mkdir(parents=True)
    (candidate / "py" / "prep" / "main.py").write_text("# source\n", encoding="utf-8")
    for name in ("Error", "image", "Check", "Result", "secmesh"):
        (candidate / name).mkdir()
    (candidate / "boundary_tria_1.vtk").write_text("mesh", encoding="utf-8")
    (candidate / "_logfile.log").write_text("log", encoding="utf-8")
    (candidate.parent / "candidate_project.sis").write_text("sis", encoding="utf-8")

    removed = adapter._cleanup_native_artifacts(candidate)

    assert set(removed) == {
        "Error", "image", "Check", "Result", "secmesh", "candidate_project.sis",
        "boundary_tria_1.vtk", "_logfile.log",
    }
    assert (candidate / "py" / "prep" / "main.py").is_file()
    assert not (candidate.parent / "candidate_project.sis").exists()


def test_t6_opencode_server_uses_native_project_as_cwd(tmp_path: Path, monkeypatch):
    calls = []

    class _FakeProcess:
        def poll(self):
            return None

    def fake_popen(*args, **kwargs):
        calls.append((args, kwargs))
        return _FakeProcess()

    monkeypatch.setattr(adapter.subprocess, "Popen", fake_popen)
    native = tmp_path / "native-project"
    env = {"OPENCODE_CONFIG_DIR": str(tmp_path / "agents")}
    adapter._start_opencode_server(native, env)

    assert calls
    assert calls[0][1]["cwd"] == str(native)


def test_t6_event_metrics_deduplicate_updates_and_sum_tokens():
    events = [
        {
            "type": "message.part.updated",
            "properties": {"part": {"type": "tool", "id": "call-1", "tool": "edit"}},
        },
        {
            "type": "message.part.updated",
            "properties": {"part": {"type": "tool", "id": "call-1", "tool": "edit"}},
        },
        {
            "type": "message.part.updated",
            "properties": {
                "part": {
                    "type": "step-finish",
                    "reason": "stop",
                    "tokens": {
                        "input": 10,
                        "output": 5,
                        "reasoning": 2,
                        "total": 17,
                        "cache": {"read": 3},
                    },
                }
            },
        },
    ]
    metrics = adapter._opencode_event_metrics(events)
    assert metrics["model_calls"] == 1
    assert metrics["tool_calls"] == 1
    assert metrics["tokens"]["total_tokens"] == 17
    assert metrics["tokens"]["cache_read_tokens"] == 3
    assert metrics["stop_reason"] == "stop"


def test_t6_event_metrics_derives_missing_total_tokens():
    metrics = adapter._opencode_event_metrics(
        [
            {
                "type": "message.part.updated",
                "properties": {
                    "part": {
                        "type": "step-finish",
                        "tokens": {"input": 11, "output": 7},
                    }
                },
            }
        ]
    )
    assert metrics["tokens"]["total_tokens"] == 18
