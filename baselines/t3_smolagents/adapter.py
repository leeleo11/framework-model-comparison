"""T3 smolagents CodeAgent adapter (runs inside .venvs/t3).

Verified against smolagents 1.26.0 installed source: ``OpenAIServerModel``
(models.py:1646/1797) takes ``client_kwargs={"timeout": ...}`` for the request
timeout and forwards ``max_tokens`` into the request body; ``CodeAgent``
(agents.py:1527) auto-registers ``final_answer``; ``@tool`` (tools.py:1061)
requires type hints plus an ``Args:`` docstring and parses the function source,
so the tools below stay module-level.
"""

from __future__ import annotations

import html
import json
import re
import time
from importlib import metadata
from pathlib import Path
from typing import Any

from baselines._framework_common import (
    resolve_max_steps, resolve_max_tokens,
    search_knowledge as _knowledge_search_shared,
    build_prompt,
    candidate_root,
    check_project_completeness as check_project_completeness_shared,
    finish,
    list_reference_files as list_reference_files_shared,
    load_request,
    normalize_tokens,
    read_candidate_file as read_candidate_file_shared,
    reference_cases_payload,
    search_skill_cases as search_skill_cases_shared,
    skill_index_payload,
)

_STATE: dict[str, Any] = {}


# Some OpenAI-compatible gateways expose the model's tool-call serialization as
# DeepSeek's DSML envelope even when the request asks for plain text.  The
# smolagents CodeAgent intentionally parses its own ``<code>...</code>``
# protocol, so the envelope must be unwrapped at the model boundary.  This is a
# serialization adapter only: the Python action body is preserved byte-for-byte
# (apart from XML entity decoding), and no tool selection or planning is added.
_DSML_TAG = r"(?:｜｜DSML｜｜|\|DSML\|)"
_DSML_PARAMETER = re.compile(
    rf"<{_DSML_TAG}\s+invoke\s+name=[\"'](?P<invoke>[^\"']+)[\"'][^>]*>"
    rf"\s*<{_DSML_TAG}\s+parameter\s+name=[\"'](?P<parameter>[^\"']+)[\"'][^>]*>"
    rf"(?P<body>.*?)</{_DSML_TAG}\s+parameter>.*?</{_DSML_TAG}\s+invoke>",
    re.DOTALL,
)


def normalize_code_agent_output(text: str) -> str:
    """Convert a provider DSML action envelope to CodeAgent's code envelope.

    ``CodeAgent`` remains responsible for parsing and executing the resulting
    Python action.  Unrecognized text is returned unchanged so the native
    parser can produce its normal self-correction message.
    """

    if not isinstance(text, str):
        return text
    if re.search(r"<code>.*?</code>", text, re.DOTALL):
        return text
    if re.search(r"```(?:python|py).*?```", text, re.DOTALL):
        return text

    actions: list[str] = []
    for match in _DSML_PARAMETER.finditer(text):
        invoke = match.group("invoke").strip()
        parameter = match.group("parameter").strip()
        body = html.unescape(match.group("body")).strip()
        if invoke in {"python_interpreter", "code"} and parameter == "code":
            if body:
                actions.append(body)
        elif invoke == "final_answer" and parameter in {"answer", "final_answer"}:
            if body:
                actions.append(f"final_answer({body!r})")

    if not actions:
        return text
    return "<code>\n" + "\n\n".join(actions) + "\n</code>"

# CodeAgent executes model-authored Python.  Keep the import allow-list narrow:
# file creation must go through the bounded ``write_file`` tool rather than a
# direct ``pathlib.Path.write_text`` escape from the candidate workspace.
ADDITIONAL_AUTHORIZED_IMPORTS = ["json"]


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unavailable"


def _result_metrics(result: Any) -> dict[str, Any]:
    """Extract comparable counters from smolagents' serialised steps.

    A CodeAgent step contains either ``model_output`` (an action response) or
    ``plan`` (a planning response), and a list of parsed ``tool_calls``.  The
    fields are optional across smolagents releases, so missing data is kept as
    zero rather than guessed from the framework's internal step number.
    """

    raw_steps = getattr(result, "steps", None) or []
    model_calls = 0
    tool_calls = 0
    for step in raw_steps:
        if isinstance(step, dict):
            output = step.get("model_output")
            plan = step.get("plan")
            calls = step.get("tool_calls") or []
        else:
            output = getattr(step, "model_output", None)
            plan = getattr(step, "plan", None)
            calls = getattr(step, "tool_calls", None) or []
        if output is not None or plan is not None:
            model_calls += 1
        if isinstance(calls, (list, tuple)):
            tool_calls += len(calls)
    return {
        "framework_steps": len(raw_steps),
        "model_calls": model_calls,
        "tool_calls": tool_calls,
    }


def _tool_error(exc: Exception) -> str:
    """Serialize a recoverable tool failure for CodeAgent."""

    return f"TOOL_ERROR: {type(exc).__name__}: {exc}"


def _resolve(relative_path: str) -> Path:
    root = _STATE["candidate_root"]
    resolved = (root / relative_path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("path escapes candidate workspace")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def list_skills() -> list[str]:
    """List the ids of all available skills.

    Returns: sorted list of skill ids
    """
    return sorted(_STATE["skill_reader"].skill_index() and
                  [entry["skill_id"] for entry in _STATE["skill_reader"].skill_index()])


def read_skill(skill_id: str) -> str:
    """Read the complete SKILL.md body of one skill.

    Args:
        skill_id: id of the skill, e.g. osis-bridge-cantilever-box
    """
    try:
        return _STATE["skill_reader"].read_skill(skill_id)
    except Exception as exc:  # noqa: BLE001 - tool failures are recoverable
        return _tool_error(exc)


def read_skill_reference(skill_id: str, relative_path: str) -> str:
    """Read one reference file inside a skill directory.

    Args:
        skill_id: id of the skill
        relative_path: path inside the skill folder, e.g. references/templates/<name>/项目画像.md
    """
    try:
        return _STATE["skill_reader"].read_reference(skill_id, relative_path)
    except Exception as exc:  # noqa: BLE001 - recoverable, model self-corrects
        return f"TOOL_ERROR: {type(exc).__name__}: {exc}"


def write_file(relative_path: str, content: str) -> str:
    """Write UTF-8 text to a candidate project file (creates parent dirs).

    Args:
        relative_path: target path relative to the candidate project root
        content: full text content to write
    """
    try:
        target = _resolve(relative_path)
        target.write_text(content, encoding="utf-8")
        return f"wrote {relative_path} ({len(content)} chars)"
    except Exception as exc:  # noqa: BLE001 - let the model self-correct
        return _tool_error(exc)


def search_skill_cases(query: str) -> str:
    """Search every skill's markdown for a case or API keyword.

    Args:
        query: keyword phrase to search for
    """
    return search_skill_cases_shared(_STATE["skill_reader"], query)



def search_knowledge(query: str) -> str:
    """Search the OSIS/pyosis knowledge base (Weknora) for API signatures,
    parameter semantics, usage examples and error fixes.

    Args:
        query: keyword phrase (Chinese or an API name)
    """
    return _knowledge_search_shared(query)

def list_reference_files(skill_id: str, template_name: str) -> str:
    """List the exact files inside one reference-case template.

    Args:
        skill_id: id of the skill
        template_name: the template directory name (e.g. 变截面悬浇连续梁-30+50+30)
    """
    return list_reference_files_shared(_STATE["skill_reader"], skill_id, template_name)


def read_candidate_file(relative_path: str) -> str:
    """Read a file the agent already wrote into the candidate project.

    Args:
        relative_path: path relative to the candidate project root
    """
    return read_candidate_file_shared(_STATE["candidate_root"], relative_path)


def check_project_completeness() -> str:
    """Report which canonical project files still need to be written."""
    return check_project_completeness_shared(_STATE["candidate_root"])


def _state_to_status(state: Any) -> tuple[str, str | None]:
    """smolagents RunResult.state -> (status, error).

    ``RunResult.state`` is ``Literal["success", "max_steps_error"]``: only an
    exhausted loop is a failure; ``final_answer`` reached -> success.
    """

    if isinstance(state, str) and state == "max_steps_error":
        return "failed", "agent loop did not finish (max_steps exceeded)"
    return "completed", None


def run_generation(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    import os

    from smolagents import CodeAgent, LogLevel, OpenAIServerModel, tool

    class _CodeActCompatibleOpenAIModel(OpenAIServerModel):
        """Keep the provider wire format compatible with CodeAgent's parser."""

        normalized_response_count = 0

        def generate(self, messages, *args, **kwargs):  # type: ignore[no-untyped-def]
            response = super().generate(messages, *args, **kwargs)
            content = getattr(response, "content", None)
            if isinstance(content, str):
                normalized = normalize_code_agent_output(content)
                if normalized != content:
                    response.content = normalized
                    self.normalized_response_count += 1
            return response

    _STATE["skill_reader"] = __import__(
        "common.skill_adapter", fromlist=["SkillAdapter"]
    ).SkillAdapter(Path(request["skills_dir"]))
    _STATE["candidate_root"] = candidate_root(request)

    # Decorate here so tools bind to the current request state.
    tools = [tool(list_skills), tool(read_skill), tool(read_skill_reference),
             tool(list_reference_files), tool(write_file), tool(search_skill_cases),
             tool(read_candidate_file), tool(check_project_completeness),
             tool(search_knowledge)]
    model = _CodeActCompatibleOpenAIModel(
        model_id=request["model"],
        api_base=request["base_url"],
        api_key=request.get("api_key") or os.environ.get("OSIS_MODEL_API_KEY", ""),
        client_kwargs={"timeout": float(request["request_timeout_s"]), "max_retries": 2},
        max_tokens=resolve_max_tokens(request.get("max_tokens")),
        temperature=float(request.get("temperature", 0.0)),
        **({"reasoning_effort": request["reasoning_effort"]}
           if request.get("reasoning_effort") else {}),
    )
    agent = CodeAgent(
        tools=tools,
        model=model,
        max_steps=resolve_max_steps(request.get("max_steps"), default=200),
        verbosity_level=LogLevel.ERROR,
        additional_authorized_imports=ADDITIONAL_AUTHORIZED_IMPORTS,
        return_full_result=True,
    )
    prompt = build_prompt(request, skill_index_payload(request["skills_dir"]),
                          reference_cases_payload(request["skills_dir"]))
    meta: dict[str, Any] = {
        "architecture_id": "T3",
        "framework": "smolagents",
        "framework_version": _package_version("smolagents"),
        "model": request["model"],
        "model_calls": 0,
        "tool_calls": 0,
        "framework_steps": 0,
        "stop_reason": None,
    }
    try:
        result = agent.run(prompt)
        meta.update(_result_metrics(result))
        meta["agent_state"] = getattr(result, "state", None) and str(getattr(result, "state"))
        usage = getattr(result, "token_usage", None)
        if usage is not None:
            meta["tokens"] = normalize_tokens(usage)
        meta["status"], meta_error = _state_to_status(meta["agent_state"])
        meta["protocol_normalized_responses"] = model.normalized_response_count
        if meta_error:
            meta["error"] = meta_error
        meta["stop_reason"] = "max_steps" if meta["status"] == "failed" else "completed"
    except Exception as exc:  # noqa: BLE001
        meta["status"] = "failed"
        meta["error_type"] = type(exc).__name__
        meta["error"] = str(exc)[:500]
        meta["stop_reason"] = "error"
    return finish(Path(request["workspace"]), "T3", meta, started)


def main() -> int:
    request = load_request()
    run_generation(request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
