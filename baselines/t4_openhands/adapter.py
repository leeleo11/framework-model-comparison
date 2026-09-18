"""T4 OpenHands SDK adapter (runs inside .venvs/t4).

Verified against openhands_sdk 1.44.1 installed source: ``LLM``
(sdk/llm/llm.py:220, pydantic fields ``max_output_tokens``/``timeout``/
``num_retries``; litellm model routing needs the ``openai/`` prefix),
``Agent`` (sdk/agent/agent.py:375; FinishTool+ThinkTool auto-included),
``Conversation`` factory (sdk/conversation/conversation.py:34) with
``max_iteration_per_run``, blocking ``send_message``+``run()``
(sdk/conversation/base.py:199/213), final text via
``openhands.sdk.conversation.get_agent_final_response``. Custom tools wrap a
plain function as Action/ToolDefinition/ToolExecutor and register it
(sdk/tool/registry.py:113); the registered name must equal the ``Tool(name=..)``
spec string and the instance needs a concrete ``.executor``.
"""

from __future__ import annotations

import json
import os
import time
from importlib import metadata
from pathlib import Path
from typing import Any

os.environ.setdefault("OPENHANDS_SUPPRESS_BANNER", "1")

from baselines._framework_common import (
    resolve_max_steps, resolve_max_tokens,
    search_knowledge as _knowledge_search_shared,  # noqa: E402
    build_prompt,
    candidate_root,
    check_project_completeness,
    finish,
    list_reference_files as list_reference_files_shared,
    load_request,
    normalize_tokens,
    read_candidate_file,
    reference_cases_payload,
    search_skill_cases,
    skill_index_payload,
)
from common.tool_policy import tool_error

_STATE: dict[str, Any] = {}


def _conversation_metrics(events: Any) -> dict[str, Any]:
    """Extract model/tool counters from the OpenHands event log.

    OpenHands can emit several events for one response (for example one
    ``ActionEvent`` per parallel tool call).  Distinct ``llm_response_id``
    values therefore represent model calls, while ActionEvents represent
    tool calls.  The fallback counts agent message events for SDK versions
    that omit response ids.
    """

    items = list(events or [])
    action_events = []
    response_ids: set[str] = set()
    fallback_model_calls = 0
    for event in items:
        class_name = type(event).__name__
        if class_name == "ActionEvent" or (
            hasattr(event, "tool_call") and hasattr(event, "tool_name")
        ):
            action_events.append(event)
        response_id = getattr(event, "llm_response_id", None)
        if response_id:
            response_ids.add(str(response_id))
        elif class_name == "MessageEvent" and getattr(event, "source", None) == "agent":
            fallback_model_calls += 1
    model_calls = len(response_ids) or fallback_model_calls
    if model_calls == 0 and action_events:
        # A very old event type may not expose an id; all actions can still be
        # attributed to at least one model response.
        model_calls = 1
    return {
        "framework_steps": len(items),
        "model_calls": model_calls,
        "tool_calls": len(action_events),
    }


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


def _list_skills() -> str:
    return json.dumps(_STATE["skill_reader"].skill_index(), ensure_ascii=False)


def _read_skill(skill_id: str) -> str:
    try:
        return _STATE["skill_reader"].read_skill(skill_id)
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {type(exc).__name__}: {exc}"


def _read_skill_reference(skill_id: str, relative_path: str) -> str:
    try:
        return _STATE["skill_reader"].read_reference(skill_id, relative_path)
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {type(exc).__name__}: {exc}"


def _write_file(relative_path: str, content: str) -> str:
    try:
        root: Path = _STATE["candidate_root"]
        resolved = (root / relative_path).resolve()
        if not resolved.is_relative_to(root):
            raise ValueError("path escapes candidate workspace")
        resolved.parent.mkdir(parents=True, exist_ok=True)
        resolved.write_text(content, encoding="utf-8")
        return f"wrote {relative_path} ({len(content)} chars)"
    except Exception as exc:  # noqa: BLE001 - tool errors are recoverable
        return tool_error(exc)


def _search_cases(query: str) -> str:
    return search_skill_cases(_STATE["skill_reader"], query)



def _search_knowledge(query: str) -> str:
    return _knowledge_search_shared(query)

def _list_reference_files(skill_id: str, template_name: str) -> str:
    return list_reference_files_shared(_STATE["skill_reader"], skill_id, template_name)


def _read_candidate(relative_path: str) -> str:
    return read_candidate_file(_STATE["candidate_root"], relative_path)


def _check_completeness() -> str:
    return check_project_completeness(_STATE["candidate_root"])


def _expose(name: str, description: str, props: dict, required: list, fn) -> Any:
    from openhands.sdk import Tool
    from openhands.sdk.tool import Action, Observation, ToolDefinition, ToolExecutor, register_tool

    action_t = Action.from_mcp_schema(
        name + "_action",
        {"type": "object", "properties": props, "required": required},
    )

    # The SDK's Observation is a discriminated union on ``kind``; a concrete
    # subclass (auto-assigned a kind from its class name) is required, the base
    # ``Observation.from_text`` yields kind='' which the schema rejects.
    class _ToolObservation(Observation):
        """Observation returned by a comparison harness tool."""

    class _Exec(ToolExecutor):
        def __call__(self, action, conversation=None):  # noqa: ANN001, D102
            # action.model_dump() includes schema meta fields (e.g. ``kind``);
            # pass only the declared tool args.
            args = {k: v for k, v in action.model_dump().items() if k in props}
            try:
                return _ToolObservation.from_text(text=str(fn(**args)))
            except Exception as exc:  # noqa: BLE001
                return _ToolObservation.from_text(text=f"error: {exc}", is_error=True)

    class _ToolDef(ToolDefinition[action_t, _ToolObservation]):
        @classmethod
        def create(cls, conv_state=None, **kwargs):  # noqa: ANN001, D102
            return [cls(description=description, action_type=action_t,
                        observation_type=_ToolObservation, executor=_Exec())]

    _ToolDef.name = name
    register_tool(name, _ToolDef.create()[0])
    return Tool(name=name)


def _run_generation_impl(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    from openhands.sdk import Agent, AgentContext, Conversation, LLM
    from openhands.sdk.conversation import get_agent_final_response

    from common.skill_adapter import SkillAdapter

    _STATE["skill_reader"] = SkillAdapter(Path(request["skills_dir"]))
    _STATE["candidate_root"] = candidate_root(request)
    native_skills = _load_native_skills(Path(request["skills_dir"]))

    tools = [
        _expose("list_skills", "List available skill ids.", {}, [], _list_skills),
        _expose("read_skill", "Read one skill's SKILL.md by id.",
                {"skill_id": {"type": "string"}}, ["skill_id"], _read_skill),
        _expose("read_skill_reference", "Read one reference file inside a skill directory.",
                {"skill_id": {"type": "string"}, "relative_path": {"type": "string"}},
                ["skill_id", "relative_path"], _read_skill_reference),
        _expose("write_file", "Write UTF-8 text to a candidate project file.",
                {"relative_path": {"type": "string"}, "content": {"type": "string"}},
                ["relative_path", "content"], _write_file),
        _expose("search_skill_cases", "Search every skill's markdown for a keyword.",
                {"query": {"type": "string"}}, ["query"], _search_cases),
        _expose("search_knowledge", "Search the OSIS/pyosis knowledge base (Weknora) for API signatures, parameter semantics, usage examples and error fixes. Prefer this before guessing an API.",
                {"query": {"type": "string"}}, ["query"], _search_knowledge),
        _expose("list_reference_files", "List the exact files inside one reference-case template.",
                {"skill_id": {"type": "string"}, "template_name": {"type": "string"}},
                ["skill_id", "template_name"], _list_reference_files),
        _expose("read_candidate_file", "Read a file the agent wrote into the candidate project.",
                {"relative_path": {"type": "string"}}, ["relative_path"], _read_candidate),
        _expose("check_project_completeness", "Report which canonical files are still missing.", {},
                [], _check_completeness),
    ]
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
        workspace=str(_STATE["candidate_root"]),
        max_iteration_per_run=resolve_max_steps(request.get("max_steps"), default=200),
        visualizer=None,
        # The candidate workspace is an experiment artifact.  Do not let the
        # SDK's default conversation cleanup remove or detach it before the
        # outer runner materializes and scores the generated files.
        delete_on_close=False,
        # Default stuck detection is aggressive for a 13-file write loop and
        # tripped STUCK before any file was written. Relax it generously so the
        # agent uses its full run-scoped step budget; the outer subprocess kill still
        # bounds the run at the total budget.
        stuck_detection_thresholds={
            "action_observation": 24,
            "action_error": 12,
            "monologue": 12,
            "alternating_pattern": 40,
        },
    )
    prompt = build_prompt(request, skill_index_payload(request["skills_dir"]),
                          reference_cases_payload(request["skills_dir"]))
    meta: dict[str, Any] = {
        "architecture_id": "T4",
        "framework": "openhands",
        "framework_version": _package_version("openhands-sdk"),
        "model": request["model"],
        "skill_loading": "openhands_native",
        "native_skill_count": len(native_skills),
        "model_calls": 0,
        "tool_calls": 0,
        "framework_steps": 0,
        "stop_reason": None,
    }
    try:
        conversation.send_message(prompt)
        conversation.run()
        status = str(getattr(conversation.state, "execution_status", ""))
        meta["execution_status"] = status
        meta.update(_conversation_metrics(getattr(conversation.state, "events", [])))
        meta["final_answer"] = (get_agent_final_response(list(conversation.state.events)) or "")[:2000]
        # STUCK / ERROR mean the session did not reach a finishing state.
        if "STUCK" in status or "ERROR" in status:
            meta["status"] = "failed"
            meta["error"] = f"conversation {status}"
            meta["stop_reason"] = status.lower() or "error"
        else:
            meta["status"] = "completed"
            meta["stop_reason"] = status.lower() or "completed"
        conversation.close()
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
        "skill_loading": "openhands_native",
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
