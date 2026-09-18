"""Real LangGraph ReAct generation adapter for the modeling comparison.

This module deliberately stops at the generation boundary.  LangGraph writes a
candidate project through bounded tools; the common runner later executes that
project with PyOSIS and invokes the OSIS conformance CLI.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any, Sequence

import requests
from langchain.agents import create_agent
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import (
    AIMessage,
    BaseMessage,
    HumanMessage,
    SystemMessage,
    ToolMessage,
)
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import BaseTool, tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import ConfigDict, Field

from common.modeling_pipeline import CANONICAL_PROJECT_FILES, validate_project_layout
from common.protocol import FORMAL_MODEL_ID
from common.skill_adapter import SkillAdapter
from common.task_schema import TaskSpec
from common.tool_policy import tool_error
from baselines._framework_common import (
    check_project_completeness as _check_completeness_shared,
    search_knowledge as _knowledge_search_shared,
    list_reference_files as _list_reference_files_shared,
)


DEFAULT_BASE_URL = "http://47.92.150.231/v1"
DEFAULT_MODEL = FORMAL_MODEL_ID
DEFAULT_TEMPERATURE = 0.0
DEFAULT_MAX_TOKENS = 12000
DEFAULT_REQUEST_TIMEOUT_S = 180.0
DEFAULT_MAX_STEPS = 24


def _recursion_limit(max_steps: int, default_steps: int = 10_000) -> int:
    """LangGraph recursion cap; ``0`` means "no step limit" (wall-clock only)."""

    steps = int(max_steps) if max_steps else default_steps
    return max(4, steps * 2 + 2)


@dataclass(frozen=True)
class T2Config:
    """Run-scoped model settings for the T2 adapter.

    ``api_key`` is intentionally not serialised into run metadata.  Callers
    should normally obtain it from ``OSIS_MODEL_API_KEY``.
    """

    base_url: str = DEFAULT_BASE_URL
    model: str = DEFAULT_MODEL
    api_key: str | None = None
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    max_steps: int = DEFAULT_MAX_STEPS

    @classmethod
    def from_env(
        cls,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        request_timeout_s: float | None = None,
        max_steps: int | None = None,
    ) -> "T2Config":
        resolved_key = api_key or os.environ.get("OSIS_MODEL_API_KEY") or os.environ.get(
            "OSIS_API_KEY"
        )
        if not resolved_key:
            raise ValueError(
                "T2 requires an API key; set OSIS_MODEL_API_KEY or pass api_key explicitly"
            )
        return cls(
            base_url=(base_url or os.environ.get("OSIS_MODEL_BASE_URL") or DEFAULT_BASE_URL).rstrip(
                "/"
            ),
            model=model or os.environ.get("OSIS_MODEL_ID") or DEFAULT_MODEL,
            api_key=resolved_key,
            temperature=(
                temperature
                if temperature is not None
                else float(os.environ.get("OSIS_MODEL_TEMPERATURE", DEFAULT_TEMPERATURE))
            ),
            max_tokens=(
                max_tokens
                if max_tokens is not None
                else int(os.environ.get("OSIS_MODEL_MAX_TOKENS", DEFAULT_MAX_TOKENS))
            ),
            request_timeout_s=(
                request_timeout_s
                if request_timeout_s is not None
                else float(
                    os.environ.get("OSIS_MODEL_REQUEST_TIMEOUT_S", DEFAULT_REQUEST_TIMEOUT_S)
                )
            ),
            max_steps=(
                max_steps
                if max_steps is not None
                else int(os.environ.get("T2_MAX_STEPS", DEFAULT_MAX_STEPS))
            ),
        )


def _content_for_api(content: Any) -> Any:
    """Keep OpenAI-compatible content blocks while normalising empty content."""

    if content is None:
        return ""
    return content


def _message_to_api(message: BaseMessage) -> dict[str, Any]:
    if isinstance(message, SystemMessage):
        role = "system"
    elif isinstance(message, HumanMessage):
        role = "user"
    elif isinstance(message, ToolMessage):
        role = "tool"
    elif isinstance(message, AIMessage):
        role = "assistant"
    else:
        role = "user"

    payload: dict[str, Any] = {"role": role, "content": _content_for_api(message.content)}
    if isinstance(message, ToolMessage):
        payload["tool_call_id"] = message.tool_call_id
        if getattr(message, "name", None):
            payload["name"] = message.name
    if isinstance(message, AIMessage):
        tool_calls = list(message.tool_calls or [])
        if not tool_calls:
            raw_tool_calls = message.additional_kwargs.get("tool_calls", [])
            tool_calls = list(raw_tool_calls) if isinstance(raw_tool_calls, list) else []
        if tool_calls:
            payload["tool_calls"] = []
            for index, call in enumerate(tool_calls):
                args = call.get("args", {}) if isinstance(call, dict) else {}
                if isinstance(args, str):
                    arguments = args
                else:
                    arguments = json.dumps(args, ensure_ascii=False)
                payload["tool_calls"].append(
                    {
                        "id": call.get("id", f"call_{index}"),
                        "type": "function",
                        "function": {
                            "name": call.get("name", ""),
                            "arguments": arguments,
                        },
                    }
                )
    return payload


def _parse_tool_calls(raw_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(raw_calls, list):
        return []
    parsed: list[dict[str, Any]] = []
    for index, raw in enumerate(raw_calls):
        if not isinstance(raw, dict):
            continue
        function = raw.get("function") or {}
        name = function.get("name")
        if not name:
            continue
        raw_arguments = function.get("arguments", "{}")
        try:
            arguments = (
                json.loads(raw_arguments)
                if isinstance(raw_arguments, str)
                else raw_arguments
            )
        except json.JSONDecodeError as exc:
            raise RuntimeError("model returned malformed tool arguments") from exc
        if not isinstance(arguments, dict):
            raise RuntimeError("model tool arguments must be a JSON object")
        parsed.append(
            {
                "name": name,
                "args": arguments,
                "id": raw.get("id") or f"call_{index}",
                "type": "tool_call",
            }
        )
    return parsed


class OpenAICompatibleChatModel(BaseChatModel):
    """Small LangChain chat-model implementation using ``requests``.

    It avoids coupling the comparison project to a provider-specific SDK while
    still giving LangGraph a normal ``BaseChatModel`` with tool-calling support.
    """

    model_name: str = Field(default=DEFAULT_MODEL, alias="model")
    base_url: str = DEFAULT_BASE_URL
    api_key: str = Field(repr=False)
    temperature: float = DEFAULT_TEMPERATURE
    max_tokens: int = DEFAULT_MAX_TOKENS
    request_timeout_s: float = DEFAULT_REQUEST_TIMEOUT_S
    bound_tools: tuple[dict[str, Any], ...] = Field(default_factory=tuple, exclude=True)
    bound_tool_choice: str | None = Field(default=None, exclude=True)

    model_config = ConfigDict(arbitrary_types_allowed=True, populate_by_name=True)

    @property
    def _llm_type(self) -> str:
        return "osis-openai-compatible"

    def bind_tools(
        self,
        tools: Sequence[dict[str, Any] | type | Any],
        *,
        tool_choice: str | None = None,
        **kwargs: Any,
    ) -> "OpenAICompatibleChatModel":
        schemas = tuple(convert_to_openai_tool(item) for item in tools)
        return self.model_copy(
            update={"bound_tools": schemas, "bound_tool_choice": tool_choice}
        )

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        del run_manager
        if not self.api_key:
            raise ValueError("T2 model API key is empty")
        payload: dict[str, Any] = {
            "model": self.model_name,
            "messages": [_message_to_api(message) for message in messages],
            "temperature": self.temperature,
        }
        # ``0`` means "no limit" in this protocol: omit the field so the
        # endpoint's own ceiling applies, identically for every architecture.
        if self.max_tokens:
            payload["max_tokens"] = self.max_tokens
        if self.bound_tools:
            payload["tools"] = list(self.bound_tools)
            if self.bound_tool_choice:
                payload["tool_choice"] = self.bound_tool_choice
        if stop:
            payload["stop"] = stop
        payload.update({key: value for key, value in kwargs.items() if value is not None})

        try:
            response = requests.post(
                f"{self.base_url.rstrip('/')}/chat/completions",
                headers={
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=self.request_timeout_s,
            )
            response.raise_for_status()
            body = response.json()
        except requests.RequestException as exc:
            status_code = getattr(getattr(exc, "response", None), "status_code", None)
            suffix = f" (HTTP {status_code})" if status_code is not None else ""
            # This transport class is also reused by the T1 one-shot adapter;
            # keep the diagnostic framework-neutral so an HTTP failure in T1
            # is not mislabeled as a T2 failure.
            raise RuntimeError(f"model request failed{suffix}") from exc
        except (ValueError, TypeError) as exc:
            raise RuntimeError("T2 model returned a non-JSON response") from exc

        choices = body.get("choices") if isinstance(body, dict) else None
        if not isinstance(choices, list) or not choices:
            raise RuntimeError("T2 model response did not contain choices")
        raw_message = choices[0].get("message") or {}
        tool_calls = _parse_tool_calls(raw_message.get("tool_calls"))
        message = AIMessage(
            content=_content_for_api(raw_message.get("content", "")),
            tool_calls=tool_calls,
            additional_kwargs={
                key: raw_message[key]
                for key in ("reasoning_content", "refusal")
                if key in raw_message
            },
            response_metadata={
                "finish_reason": choices[0].get("finish_reason"),
                "model": body.get("model"),
                "usage": body.get("usage"),
            },
        )
        return ChatResult(generations=[ChatGeneration(message=message)])


def _safe_candidate_path(candidate_root: Path, relative_path: str) -> Path:
    raw = str(relative_path or "").strip()
    if not raw:
        raise ValueError("relative_path is required")
    path = Path(raw)
    if ".." in path.parts:
        raise ValueError("path is outside candidate workspace")
    resolved = (path if path.is_absolute() else candidate_root / path).resolve()
    try:
        resolved.relative_to(candidate_root)
    except ValueError as exc:
        raise ValueError("path is outside candidate workspace") from exc
    return resolved


def build_t2_tools(candidate_root: Path, skill_reader: SkillAdapter) -> list[BaseTool]:
    """Create the read-only skill tools and candidate-only write tools."""

    candidate_root = Path(candidate_root).expanduser().resolve()
    candidate_root.mkdir(parents=True, exist_ok=True)

    @tool
    def list_skills() -> str:
        """List the shared OSIS skills available to this run."""
        try:
            return json.dumps(skill_reader.skill_index(), ensure_ascii=False)
        except Exception as exc:  # noqa: BLE001 - model can self-correct
            return tool_error(exc)

    @tool
    def read_skill(skill_id: str) -> str:
        """Read one complete shared SKILL.md by its skill id."""

        try:
            return skill_reader.read_skill(skill_id)
        except Exception as exc:  # noqa: BLE001 - model can self-correct
            return tool_error(exc)

    @tool
    def read_skill_reference(skill_id: str, relative_path: str) -> str:
        """Read one reference file below a shared skill directory."""

        try:
            return skill_reader.read_reference(skill_id, relative_path)
        except Exception as exc:  # noqa: BLE001 - model can self-correct
            return tool_error(exc)

    @tool
    def list_reference_files(skill_id: str, template_name: str) -> str:
        """List the exact files inside one reference-case template.

        The arguments are the path relative to the skill directory (e.g.
        ``references/templates/<template_name>``) or the template name itself;
        returns the full file list so you never have to guess paths.
        """

        # Keep the same path validation as T3-T5.  In particular, an unknown
        # skill or ``../`` template must become a model-visible TOOL_ERROR,
        # never an exception that aborts the LangGraph tool node.
        return _list_reference_files_shared(skill_reader, skill_id, template_name)

    @tool
    def search_skill_cases(query: str) -> str:
        """Search shared skill markdown for a case or API keyword."""
        try:
            return json.dumps(
                [
                    {
                        "skill_id": hit.skill_id,
                        "path": str(hit.path),
                        "preview": hit.preview,
                    }
                    for hit in skill_reader.search_cases(query)
                ],
                ensure_ascii=False,
            )
        except Exception as exc:  # noqa: BLE001 - model can self-correct
            return tool_error(exc)

    @tool
    def read_candidate_file(relative_path: str) -> str:
        """Read a candidate project file using a path relative to its root."""

        try:
            path = _safe_candidate_path(candidate_root, relative_path)
            if not path.is_file():
                raise FileNotFoundError(relative_path)
            return path.read_text(encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - model can self-correct
            return tool_error(exc)

    @tool
    def write_file(relative_path: str, content: str) -> str:
        """Write UTF-8 text to a candidate project path; never write outside it."""

        try:
            path = _safe_candidate_path(candidate_root, relative_path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        except Exception as exc:  # noqa: BLE001 - model can self-correct
            return tool_error(exc)
        return f"wrote {path.relative_to(candidate_root).as_posix()} ({len(content)} chars)"

    @tool
    def check_project_completeness() -> str:
        """Report which canonical project files are still missing."""
        try:
            return _check_completeness_shared(candidate_root)
        except Exception as exc:  # noqa: BLE001 - model can self-correct
            return tool_error(exc)

    @tool
    def search_knowledge(query: str) -> str:
        """Search the OSIS/pyosis knowledge base (Weknora) for API signatures,
        parameter semantics, usage examples and error fixes. Prefer this before
        guessing an API; input a Chinese or English keyword phrase."""
        return _knowledge_search_shared(query)

    return [
        list_skills,
        read_skill,
        read_skill_reference,
        list_reference_files,
        search_skill_cases,
        read_candidate_file,
        write_file,
        check_project_completeness,
        search_knowledge,
    ]


def build_t2_system_prompt(task: TaskSpec, skill_reader: SkillAdapter) -> str:
    task_json = json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
    skill_index = json.dumps(skill_reader.skill_index(), ensure_ascii=False)
    reference_cases = json.dumps(
        skill_reader.visible_template_inventory(), ensure_ascii=False, indent=2
    )
    # Detailed per-template FILE LIST, so the model does not have to guess
    # paths (parity with T3-T5's reference_cases_payload).
    detail = {}
    for skill_id, names in skill_reader.visible_template_inventory().items():
        detail[skill_id] = {}
        for name in names:
            tpl = skill_reader._skill_dir(skill_id) / "references" / "templates" / name
            files = (
                sorted(p.relative_to(tpl).as_posix() for p in tpl.rglob("*") if p.is_file())
                if tpl.is_dir() else []
            )
            detail[skill_id][name] = files
    reference_files = json.dumps(detail, ensure_ascii=False, indent=2)
    expected = json.dumps(
        ["py/" + item if item == CANONICAL_PROJECT_FILES[0] else "py/prep/" + item for item in CANONICAL_PROJECT_FILES],
        ensure_ascii=False,
    )
    return f"""You are T2, a LangGraph ReAct agent generating an OSIS bridge model.

You are in the generation layer only. Use the read-only skill tools to inspect
the shared OSIS instructions and references, then use write_file to create the
candidate project. Do not execute PyOSIS, call a scorer, modify any run result,
or invent a different output format. Work in small inspect -> write -> inspect
steps and finish by calling check_project_completeness.

The candidate must contain every canonical file listed below. Write real,
executable PYOSIS code guided by the skills; do not write placeholders merely
to satisfy the file list. For a modify task, preserve the supplied snapshot
contract and change only the requested fields.

Task JSON:
{task_json}

Canonical files:
{expected}

Shared skill index (full bodies are available through read_skill):
{skill_index}

Reference cases (visible templates, per skill; read them through
read_skill_reference when a case could inform your model, e.g. a bridge of a
similar structural type):
{reference_cases}

Reference case FILES (exact paths inside each visible template — use
list_reference_files or read_skill_reference to read them; do NOT guess):
{reference_files}
"""


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unavailable"


TRANSCRIPT_HEAD_CHARS = 8000


def _transcript_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(value)


def _transcript_digest(text: str) -> dict[str, Any]:
    data = text.encode("utf-8", errors="replace")
    return {
        "chars": len(data),
        "head": text[:TRANSCRIPT_HEAD_CHARS],
        "sha256": hashlib.sha256(data).hexdigest(),
    }


class _TranscriptWriter:
    """Append-only JSONL recorder for one ReAct trajectory.

    Records stream incrementally so an aborted generation still leaves the
    steps that already happened. Tool returns are truncated to
    ``TRANSCRIPT_HEAD_CHARS`` plus a SHA256 to keep the file bounded.
    """

    def __init__(self, path: Path) -> None:
        self._handle = path.open("w", encoding="utf-8")
        self._step = 0
        self._pending: dict[str, Any] | None = None
        self._returns: dict[str, dict[str, Any]] = {}
        self.model_calls = 0
        self.tool_calls = 0
        self.invalid_tool_calls = 0
        self.last_finish_reason: str | None = None

    def _emit(self, record: dict[str, Any]) -> None:
        self._handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        self._handle.flush()

    def _flush_pending(self) -> None:
        if self._pending is None:
            return
        for call in self._pending["tool_calls"]:
            digest = self._returns.pop(str(call.get("id")), None)
            if digest is not None:
                call["return"] = digest
        self._emit(self._pending)
        self._pending = None
        self._returns.clear()

    def add_message(self, message: BaseMessage) -> None:
        if isinstance(message, AIMessage):
            self._flush_pending()
            self.model_calls += 1
            calls = [
                {"id": call.get("id"), "name": call.get("name"), "args": call.get("args")}
                for call in (message.tool_calls or [])
            ]
            self.tool_calls += len(calls)
            invalid_calls = [
                {
                    "id": call.get("id"),
                    "name": call.get("name"),
                    "args": _transcript_digest(_transcript_text(call.get("args", ""))),
                    "error": call.get("error"),
                }
                for call in (getattr(message, "invalid_tool_calls", None) or [])
            ]
            self.invalid_tool_calls += len(invalid_calls)
            response_metadata = message.response_metadata or {}
            self.last_finish_reason = response_metadata.get("finish_reason")
            self._pending = {
                "step": self._step,
                "role": "ai",
                "model_called_tool": bool(calls),
                "tool_calls": calls,
                "invalid_tool_calls": invalid_calls,
                "finish_reason": response_metadata.get("finish_reason"),
                "final_text": None if calls else _transcript_text(message.content),
                "usage": response_metadata.get("usage"),
            }
        elif isinstance(message, ToolMessage):
            self._returns[str(getattr(message, "tool_call_id", ""))] = _transcript_digest(
                _transcript_text(message.content)
            )
        else:
            self._flush_pending()
            self._emit(
                {
                    "step": self._step,
                    "role": "user",
                    "content_head": _transcript_digest(_transcript_text(message.content))[
                        "head"
                    ],
                }
            )
        self._step += 1

    def close(self) -> None:
        self._flush_pending()
        self._handle.close()


def generate(
    task: TaskSpec,
    skill_reader: SkillAdapter,
    workspace: Path,
    *,
    config: T2Config | None = None,
    deadline_monotonic: float | None = None,
) -> Path:
    """Run the real LangGraph ReAct graph and return its candidate project."""

    config = config or T2Config.from_env()
    workspace = Path(workspace).expanduser().resolve()
    workspace.mkdir(parents=True, exist_ok=True)
    candidate_root = workspace / "candidate_project"
    candidate_root.mkdir(parents=True, exist_ok=True)
    metadata_path = workspace / "t2_generation.json"
    started = time.monotonic()

    metadata_payload: dict[str, Any] = {
        "architecture_id": "T2",
        "framework": "langgraph",
        "framework_version": _package_version("langgraph"),
        "langchain_version": _package_version("langchain"),
        "model": config.model,
        "base_url": config.base_url,
        "temperature": config.temperature,
        "max_tokens": config.max_tokens,
        "max_steps": config.max_steps,
        "status": "running",
        "model_calls": 0,
        "tool_calls": 0,
        "invalid_tool_calls": 0,
        "stop_reason": None,
    }

    transcript: _TranscriptWriter | None = None
    try:
        model = OpenAICompatibleChatModel(
            model=config.model,
            base_url=config.base_url,
            api_key=config.api_key or "",
            temperature=config.temperature,
            max_tokens=config.max_tokens,
            request_timeout_s=config.request_timeout_s,
        )
        graph = create_agent(
            model=model,
            tools=build_t2_tools(candidate_root, skill_reader),
            system_prompt=build_t2_system_prompt(task, skill_reader),
            name="t2_langgraph_react",
        )
        transcript = _TranscriptWriter(workspace / "t2_transcript.jsonl")
        final_state: dict[str, Any] | None = None
        try:
            seen_messages = 0
            budget_exceeded = False
            for chunk in graph.stream(
                {"messages": [{"role": "user", "content": task.natural_language_requirement}]},
                config={"recursion_limit": _recursion_limit(config.max_steps)},
                stream_mode="values",
            ):
                messages = chunk.get("messages", []) if isinstance(chunk, dict) else []
                for message in messages[seen_messages:]:
                    transcript.add_message(message)
                seen_messages = len(messages)
                final_state = chunk
                # Hard wall-clock budget: the in-process graph must abort at the
                # experiment's total budget (recursion_limit alone allows up to
                # ~25 steps x 600s => far beyond 1800s).
                if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
                    budget_exceeded = True
                    break
        finally:
            transcript.close()
        messages = final_state.get("messages", []) if isinstance(final_state, dict) else []
        metadata_payload["transcript"] = "t2_transcript.jsonl"
        metadata_payload["model_calls"] = transcript.model_calls
        metadata_payload["tool_calls"] = transcript.tool_calls
        metadata_payload["invalid_tool_calls"] = transcript.invalid_tool_calls
        metadata_payload["message_count"] = len(messages)
        if budget_exceeded:
            metadata_payload["status"] = "failed"
            metadata_payload["error"] = "generation exceeded the total task budget"
            metadata_payload["stop_reason"] = "task_timeout"
        else:
            metadata_payload["status"] = "completed"
            metadata_payload["stop_reason"] = transcript.last_finish_reason or "completed"
    except Exception as exc:
        if transcript is not None:
            metadata_payload["model_calls"] = transcript.model_calls
            metadata_payload["tool_calls"] = transcript.tool_calls
            metadata_payload["invalid_tool_calls"] = transcript.invalid_tool_calls
            metadata_payload["transcript"] = "t2_transcript.jsonl"
        metadata_payload["status"] = "failed"
        metadata_payload["error_type"] = type(exc).__name__
        metadata_payload["error"] = str(exc).replace(config.api_key or "", "<redacted>")
        metadata_payload["stop_reason"] = "error"
        raise
    finally:
        metadata_payload["elapsed_s"] = round(time.monotonic() - started, 3)
        metadata_path.write_text(
            json.dumps(metadata_payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    return candidate_root
