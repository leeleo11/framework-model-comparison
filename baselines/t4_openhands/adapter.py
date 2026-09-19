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

import ast
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
    candidate_root,
    check_project_completeness,
    finish,
    list_reference_files as list_reference_files_shared,
    load_request,
    normalize_tokens,
    read_candidate_file,
)
from common.modeling_pipeline import CANONICAL_PROJECT_FILES
from common.tool_policy import tool_error

_STATE: dict[str, Any] = {}

T4_CUSTOM_TOOL_NAMES = (
    "read_skill_reference",
    "list_reference_files",
    "search_knowledge",
    "list_candidate_files",
    "read_candidate_file",
    "write_file",
    "check_python_syntax",
    "check_project_completeness",
)


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

Work progressively: invoke skill -> inspect candidate/reference -> write a
small coherent batch -> inspect it. Use check_python_syntax, then
check_project_completeness before FinishTool. Write real executable PYOSIS
code, never placeholders. In the final answer list the files written and any
remaining risk.
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


def _search_knowledge(query: str) -> str:
    return _knowledge_search_shared(query)

def _list_reference_files(skill_id: str, template_name: str) -> str:
    return list_reference_files_shared(_STATE["skill_reader"], skill_id, template_name)


def _read_candidate(relative_path: str) -> str:
    return read_candidate_file(_STATE["candidate_root"], relative_path)


def _list_candidate_files() -> str:
    try:
        root: Path = _STATE["candidate_root"]
        files = sorted(
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if path.is_file()
        )
        return json.dumps(files, ensure_ascii=False)
    except Exception as exc:  # noqa: BLE001
        return tool_error(exc)


def _check_python_syntax() -> str:
    root: Path = _STATE["candidate_root"]
    failures: dict[str, str] = {}
    for path in sorted(root.rglob("*.py")):
        relative = path.relative_to(root).as_posix()
        try:
            ast.parse(path.read_text(encoding="utf-8"), filename=relative)
        except (SyntaxError, UnicodeError) as exc:
            failures[relative] = str(exc).replace(str(root), "<candidate>")
    return json.dumps(
        {"ok": not failures, "checked": len(list(root.rglob("*.py"))), "failures": failures},
        ensure_ascii=False,
    )


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


def _build_custom_tools() -> list[Any]:
    """Register only candidate/reference tools; skills stay SDK-native."""
    return [
        _expose("read_skill_reference", "Read one reference file inside a skill directory.",
                {"skill_id": {"type": "string"}, "relative_path": {"type": "string"}},
                ["skill_id", "relative_path"], _read_skill_reference),
        _expose("list_reference_files", "List the exact files inside one reference-case template.",
                {"skill_id": {"type": "string"}, "template_name": {"type": "string"}},
                ["skill_id", "template_name"], _list_reference_files),
        _expose("search_knowledge", "Search the OSIS/pyosis knowledge base for an API signature or usage detail. Prefer this before guessing an API.",
                {"query": {"type": "string"}}, ["query"], _search_knowledge),
        _expose("list_candidate_files", "List files already present in the candidate project.",
                {}, [], _list_candidate_files),
        _expose("read_candidate_file", "Read a file the agent wrote into the candidate project.",
                {"relative_path": {"type": "string"}}, ["relative_path"], _read_candidate),
        _expose("write_file", "Write UTF-8 text to a candidate project file.",
                {"relative_path": {"type": "string"}, "content": {"type": "string"}},
                ["relative_path", "content"], _write_file),
        _expose("check_python_syntax", "Parse every candidate Python file and report syntax errors.",
                {}, [], _check_python_syntax),
        _expose("check_project_completeness", "Report which canonical files are still missing.", {},
                [], _check_completeness),
    ]


def _run_generation_impl(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    from openhands.sdk import Agent, AgentContext, Conversation, LLM
    from openhands.sdk.conversation import get_agent_final_response

    from common.skill_adapter import SkillAdapter

    _STATE["skill_reader"] = SkillAdapter(Path(request["skills_dir"]))
    _STATE["candidate_root"] = candidate_root(request)
    native_skills = _load_native_skills(Path(request["skills_dir"]))

    tools = _build_custom_tools()
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
    prompt = build_t4_prompt(request)
    meta: dict[str, Any] = {
        "architecture_id": "T4",
        "framework": "openhands",
        "framework_version": _package_version("openhands-sdk"),
        "model": request["model"],
        "skill_loading": "openhands_native_progressive_v2",
        "native_skill_count": len(native_skills),
        "native_prompt_chars": len(prompt),
        "custom_tools": list(T4_CUSTOM_TOOL_NAMES),
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
