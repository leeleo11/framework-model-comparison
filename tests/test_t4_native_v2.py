from __future__ import annotations

import json
from pathlib import Path

from baselines.t4_openhands import adapter


def _request() -> dict:
    return {
        "task": {
            "task_id": "t4-native-v2",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L2",
            "natural_language_requirement": "build a complete model",
            "total_timeout_s": 1800,
            "expected_fields": {},
            "metadata": {},
        }
    }


def test_t4_prompt_hands_off_to_native_progressive_skills() -> None:
    prompt = adapter.build_t4_prompt(_request())
    assert "invoke_skill" in prompt
    assert "InvokeSkillTool" in prompt
    assert '"task_id": "t4-native-v2"' in prompt
    assert "check_project_completeness" in prompt
    assert "skill_id" not in prompt
    assert "Reference cases you may imitate" not in prompt
    assert len(prompt) < 5000


def test_t4_custom_tools_do_not_bypass_native_skill_loading() -> None:
    names = set(adapter.T4_CUSTOM_TOOL_NAMES)
    assert {"list_skills", "read_skill", "search_skill_cases"}.isdisjoint(names)
    assert names == {
        "read_skill_reference",
        "list_reference_files",
        "search_knowledge",
        "list_candidate_files",
        "read_candidate_file",
        "write_file",
        "check_python_syntax",
        "check_project_completeness",
    }


def test_t4_event_trace_counts_native_skill_invocations_without_content() -> None:
    action = type("Invoke", (), {"name": "osis-bridge-cantilever-box"})()
    event = type(
        "ActionEvent",
        (),
        {
            "id": "event-1",
            "timestamp": "2026-09-19T00:00:00Z",
            "source": "agent",
            "tool_name": "InvokeSkillTool",
            "tool_call_id": "call-1",
            "llm_response_id": "response-1",
            "action": action,
            "thought": "large private chain of thought must not be persisted",
        },
    )()

    summary = adapter.summarize_events([event])

    assert summary["model_calls"] == 1
    assert summary["tool_calls"] == 1
    assert summary["native_skill_invocations"] == ["osis-bridge-cantilever-box"]
    serialized = json.dumps(summary, ensure_ascii=False)
    assert "large private chain" not in serialized
    assert summary["trace"][0]["tool_name"] == "InvokeSkillTool"


def test_t4_event_trace_is_written_as_jsonl(tmp_path: Path) -> None:
    trace = [
        {
            "event_index": 0,
            "event_type": "ActionEvent",
            "tool_name": "InvokeSkillTool",
            "skill_name": "osis-engine",
        }
    ]
    path = adapter.write_event_trace(tmp_path, trace)
    assert path == tmp_path / "t4_events.jsonl"
    assert json.loads(path.read_text(encoding="utf-8")) == trace[0]


def test_t4_python_syntax_tool_reports_relative_failures(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    (candidate / "py").mkdir(parents=True)
    (candidate / "py" / "good.py").write_text("VALUE = 1\n", encoding="utf-8")
    (candidate / "py" / "bad.py").write_text("def broken(:\n", encoding="utf-8")
    adapter._STATE["candidate_root"] = candidate
    result = json.loads(adapter._check_python_syntax())
    assert result["ok"] is False
    assert list(result["failures"]) == ["py/bad.py"]
    assert str(tmp_path) not in json.dumps(result)
