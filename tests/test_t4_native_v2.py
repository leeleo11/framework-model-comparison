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
    assert "file editor" in prompt
    assert "check_project_completeness" not in prompt
    assert "skill_id" not in prompt
    assert "Reference cases you may imitate" not in prompt
    assert len(prompt) < 5000


def test_t4_uses_openhands_default_exec_tools() -> None:
    source = Path(adapter.__file__).read_text(encoding="utf-8")
    assert "get_default_tools(enable_browser=False)" in source
    assert "def _build_custom_tools" not in source
    assert "def _write_file" not in source


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
