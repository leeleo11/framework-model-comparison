"""T4 OpenHands SDK adapter (runs inside .venvs/t4).

Verified against openhands_sdk 1.44.1 installed source: ``LLM``
(sdk/llm/llm.py:220, pydantic fields ``max_output_tokens``/``timeout``/
``num_retries``; litellm model routing needs the ``openai/`` prefix),
``Agent`` (sdk/agent/agent.py:375; FinishTool+ThinkTool auto-included),
``Conversation`` factory (sdk/conversation/conversation.py:34) with
``max_iteration_per_run``, blocking ``send_message``+``run()``
(sdk/conversation/base.py:199/213), final text via
``openhands.sdk.conversation.get_agent_final_response``. Exec tools come from
``openhands-tools`` 1.44.1 ``get_default_tools(enable_browser=False)``:
terminal, file editor, and task tracker. Skills stay on ``invoke_skill``.
"""

from __future__ import annotations

import json
import os
import time
from importlib import metadata
from pathlib import Path
from typing import Any

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from baselines._framework_common import (  # noqa: E402
    LIBRARY_LOOP_BOUND,
    candidate_root,
    finish,
    load_request,
    normalize_tokens,
    resolve_max_tokens,
)
from common.execution_feedback import observe_candidate, run_feedback_loop
from common.modeling_pipeline import CANONICAL_PROJECT_FILES


def summarize_events(events: Any) -> dict[str, Any]:
    """Build a content-free audit trace and native-skill usage counters."""
    items = list(events or [])
    response_ids: set[str] = set()
    fallback_model_calls = 0
    tool_calls = 0
    tool_call_counts: dict[str, int] = {}
    native_skills: list[str] = []
    trace: list[dict[str, Any]] = []
    for index, event in enumerate(items):
        class_name = type(event).__name__
        tool_name = str(getattr(event, "tool_name", None) or "")
        if class_name == "ActionEvent" or (
            hasattr(event, "tool_call") and hasattr(event, "tool_name")
        ):
            tool_calls += 1
            key = tool_name or "unknown"
            tool_call_counts[key] = tool_call_counts.get(key, 0) + 1
        response_id = getattr(event, "llm_response_id", None)
        if response_id:
            response_ids.add(str(response_id))
        elif class_name == "MessageEvent" and getattr(event, "source", None) == "agent":
            fallback_model_calls += 1
        skill_name = ""
        if tool_name.casefold() in {"invokeskilltool", "invoke_skill"}:
            skill_name = str(getattr(getattr(event, "action", None), "name", "") or "")
            if skill_name and skill_name not in native_skills:
                native_skills.append(skill_name)
        activated = [str(name) for name in (getattr(event, "activated_skills", None) or [])]
        for name in activated:
            if name and name not in native_skills:
                native_skills.append(name)
        trace.append(
            {
                "event_index": index,
                "event_type": class_name,
                "event_id": str(getattr(event, "id", "") or ""),
                "timestamp": str(getattr(event, "timestamp", "") or ""),
                "source": str(getattr(event, "source", "") or ""),
                "llm_response_id": str(response_id or ""),
                "tool_name": tool_name,
                "tool_call_id": str(getattr(event, "tool_call_id", "") or ""),
                "skill_name": skill_name,
                "activated_skills": activated,
            }
        )
    model_calls = len(response_ids) or fallback_model_calls
    if model_calls == 0 and tool_calls:
        model_calls = 1
    return {
        "framework_steps": len(items),
        "model_calls": model_calls,
        "tool_calls": tool_calls,
        "tool_call_counts": tool_call_counts,
        "native_skill_invocations": native_skills,
        "trace": trace,
    }


def _conversation_metrics(events: Any) -> dict[str, Any]:
    """Compatibility wrapper retained for existing callers and tests."""
    summary = summarize_events(events)
    return {
        key: summary[key]
        for key in ("framework_steps", "model_calls", "tool_calls")
    }


def write_event_trace(workspace: Path, trace: list[dict[str, Any]]) -> Path:
    path = Path(workspace) / "t4_events.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as stream:
        for event in trace:
            stream.write(json.dumps(event, ensure_ascii=False) + "\n")
    return path


def build_t4_prompt(request: dict[str, Any]) -> str:
    """Concise hand-off that lets OpenHands perform native skill routing."""
    task_json = json.dumps(request["task"], ensure_ascii=False, indent=2)
    expected = [
        "py/项目画像.md",
        *[f"py/prep/{name}" for name in CANONICAL_PROJECT_FILES[1:]],
    ]
    file_list = "\n".join(f"- {path}" for path in expected)
    return f"""You are T4, the OpenHands native-skills architecture.

Use the native invoke_skill tool (OpenHands InvokeSkillTool) first to load the relevant bridge and OSIS skills from
the native <available_skills> catalog. Do not reconstruct skill bodies from
an injected inventory. Read only the references needed for this task.

Task:
{task_json}

Candidate contract:
{file_list}

Work in the candidate workspace. Use invoke_skill for the mounted skills, then
the framework terminal and file editor to write the files. Finish with
FinishTool. Write real executable PYOSIS code, never placeholders. In the
final answer list the files written and any remaining risk.
"""


def _load_native_skills(skills_dir: Path, loader=None) -> list[Any]:
    """Load the mounted snapshot through OpenHands' native skill loader.

    ``load_skills_from_dir`` returns three categorized dictionaries (repo,
    knowledge and AgentSkills-format skills).  Preserve all categories in a
    deterministic order so T4 exercises the SDK's native progressive
    disclosure path rather than merely receiving skill text in a custom
    prompt.  ``loader`` is injectable for adapter-level tests.
    """

    if loader is None:
        from openhands.sdk.skills import load_skills_from_dir

        loader = load_skills_from_dir
    groups = loader(Path(skills_dir))
    skills: list[Any] = []
    for group in groups:
        if isinstance(group, dict):
            skills.extend(group.values())
    return skills


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unavailable"


def native_tool_specs() -> list[Any]:
    """OpenHands default exec tools. Browser stays off."""

    from openhands.tools.preset.default import get_default_tools

    return get_default_tools(enable_browser=False)


def _run_generation_impl(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    from openhands.sdk import Agent, AgentContext, Conversation, LLM
    from openhands.sdk.conversation import get_agent_final_response

    root = candidate_root(request)
    native_skills = _load_native_skills(Path(request["skills_dir"]))
    tools = native_tool_specs()
    llm = LLM(
        model=f"openai/{request['model']}",
        base_url=request["base_url"],
        api_key=request.get("api_key") or os.environ.get("OSIS_MODEL_API_KEY", ""),
        timeout=int(request["request_timeout_s"]),
        max_output_tokens=resolve_max_tokens(request.get("max_tokens")),
        usage_id="t4",
        num_retries=2,
        api_mode="chat",  # force chat-completions; glm/deepseek endpoints do not serve /responses
        temperature=float(request.get("temperature", 0.0)),
        **({"reasoning_effort": request["reasoning_effort"]}
           if request.get("reasoning_effort") else {}),
    )
    agent = Agent(
        llm=llm,
        tools=tools,
        agent_context=AgentContext(
            skills=native_skills,
            load_user_skills=False,
            load_public_skills=False,
            load_project_skills=False,
            load_memory=False,
        ),
    )
    conversation = Conversation(
        agent=agent,
        workspace=str(root),
        # OpenHands defaults this to 500. The experiment does not stop on steps.
        max_iteration_per_run=LIBRARY_LOOP_BOUND,
        visualizer=None,
        # The candidate workspace is an experiment artifact.  Do not let the
        # SDK's default conversation cleanup remove or detach it before the
        # outer runner materializes and scores the generated files.
        delete_on_close=False,
        # Default stuck detection is aggressive for a 13-file write loop and
        # tripped STUCK before any file was written. Relax it so a repeated
        # action is not cut off before the wall-clock deadline.
        stuck_detection_thresholds={
            "action_observation": 24,
            "action_error": 12,
            "monologue": 12,
            "alternating_pattern": 40,
        },
    )
    prompt = build_t4_prompt(request)
    meta: dict[str, Any] = {
        "architecture_id": "T4",
        "framework": "openhands",
        "framework_version": _package_version("openhands-sdk"),
        "model": request["model"],
        "skill_loading": "openhands_native_progressive_v2",
        "native_skill_count": len(native_skills),
        "native_prompt_chars": len(prompt),
        "framework_tools": [tool.name for tool in tools],
        "model_calls": 0,
        "tool_calls": 0,
        "framework_steps": 0,
        "stop_reason": None,
    }
    try:
        conversation.send_message(prompt)
        conversation.run()
        status = str(getattr(conversation.state, "execution_status", ""))
        if (
            request.get("execution_feedback")
            and request.get("parent_repo")
            and "STUCK" not in status
            and "ERROR" not in status
        ):
            deadline = time.monotonic() + float(request.get("generation_timeout_s") or 3600.0)
            parent_repo = Path(request["parent_repo"])

            def _observe(scratch: Path) -> str | None:
                return observe_candidate(
                    root,
                    scratch,
                    parent_repo=parent_repo,
                    deadline_monotonic=deadline,
                )

            def _resume(observation: str) -> None:
                conversation.send_message(observation)
                conversation.run()

            meta["execution_feedback"] = run_feedback_loop(
                candidate=root,
                scratch_root=Path(request["workspace"]) / "build_feedback",
                deadline_monotonic=deadline,
                observe=_observe,
                resume=_resume,
            )
            status = str(getattr(conversation.state, "execution_status", ""))
        meta["execution_status"] = status
        events = list(getattr(conversation.state, "events", []) or [])
        event_summary = summarize_events(events)
        trace = event_summary.pop("trace")
        meta.update(event_summary)
        meta["event_trace"] = write_event_trace(Path(request["workspace"]), trace).name
        meta["final_answer"] = (get_agent_final_response(events) or "")[:2000]
        # STUCK / ERROR mean the session did not reach a finishing state.
        if "STUCK" in status or "ERROR" in status:
            meta["status"] = "failed"
            meta["error"] = f"conversation {status}"
            meta["stop_reason"] = status.lower() or "error"
        else:
            meta["status"] = "completed"
            meta["stop_reason"] = status.lower() or "completed"
        feedback_stop = (meta.get("execution_feedback") or {}).get("stop_reason")
        if feedback_stop == "task_timeout":
            meta["status"] = "failed"
            meta["stop_reason"] = "task_timeout"
            meta["error"] = "generation exceeded the total task budget"
        elif feedback_stop:
            meta["stop_reason"] = feedback_stop
        # best-effort unified token extraction
        try:
            stats = getattr(conversation, "conversation_stats", None) or getattr(
                conversation, "stats", None
            )
            if stats is None and hasattr(conversation, "export_conversation_stats"):
                stats = conversation.export_conversation_stats(use_snapshot=True)
            if stats is not None and hasattr(stats, "get_combined_metrics"):
                usage = getattr(stats.get_combined_metrics(), "accumulated_token_usage", None)
                if usage is not None:
                    meta["tokens"] = normalize_tokens(usage)
        except Exception:  # noqa: BLE001 - tokens optional, cost dim just skips
            pass
    except Exception as exc:  # noqa: BLE001
        meta["status"] = "failed"
        meta["error_type"] = type(exc).__name__
        meta["error"] = str(exc)[:500]
        meta["stop_reason"] = "error"
    finally:
        try:
            conversation.close()
        except Exception:  # noqa: BLE001 - preserve the generation result
            pass
    return finish(Path(request["workspace"]), "T4", meta, started)


def run_generation(request: dict[str, Any]) -> dict[str, Any]:
    """Run T4 and always leave a generation record, including setup failures.

    OpenHands performs native skill loading and tool registration before its
    conversation object exists.  Keep that setup inside a small outer guard
    so a bad snapshot/SDK configuration is recorded as a normal framework
    failure instead of terminating the subprocess without ``t4_generation``.
    """

    started = time.monotonic()
    meta = {
        "architecture_id": "T4",
        "framework": "openhands",
        "framework_version": _package_version("openhands-sdk"),
        "model": request.get("model"),
        "skill_loading": "openhands_native_progressive_v2",
        "native_skill_count": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "framework_steps": 0,
        "status": "failed",
        "stop_reason": "error",
    }
    try:
        return _run_generation_impl(request)
    except Exception as exc:  # noqa: BLE001 - convert setup failure to metadata
        meta["error_type"] = type(exc).__name__
        meta["error"] = str(exc)[:500]
        return finish(Path(request["workspace"]), "T4", meta, started)


def main() -> int:
    request = load_request()
    run_generation(request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
