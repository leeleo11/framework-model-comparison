"""T3 smolagents CodeAgent adapter (runs inside .venvs/t3).

Verified against smolagents 1.26.0 installed source: ``OpenAIServerModel``
(models.py:1646/1797) takes ``client_kwargs={"timeout": ...}`` for the request
timeout and forwards ``max_tokens`` into the request body; ``CodeAgent``
(agents.py:1527) auto-registers ``final_answer``. The action is model-authored
Python. ``pathlib`` and ``pyosis`` are on the interpreter allow-list so the agent
reads the mounted skills, writes the candidate project, and may run PyOSIS
itself.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from importlib import metadata
from pathlib import Path
from typing import Any

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

# CodeAgent's own action is Python.  pathlib is the interpreter's file API.
# pyosis is the same execution library the scorer uses; the interpreter may
# import it.  os, subprocess and shutil stay off the allow-list.
ADDITIONAL_AUTHORIZED_IMPORTS = ["json", "pathlib", "pyosis"]


def _expose_parent_pyosis(parent_repo: Path | None) -> None:
    """Let this interpreter import pyosis without the parent venv's packages.

    The parent venv is Python 3.11. Putting its whole site-packages on this
    3.13 path makes numpy/pydantic load the wrong binaries and hides openai.
    Only the pure-Python ``pyosis`` package is linked in.
    """

    if parent_repo is None:
        return
    source = Path(parent_repo) / ".venv" / "Lib" / "site-packages" / "pyosis"
    if not source.is_dir():
        return
    holder = Path(__file__).resolve().parents[2] / ".venvs" / "t3" / "pyosis-link"
    link = holder / "pyosis"
    holder.mkdir(parents=True, exist_ok=True)
    if not link.exists():
        import subprocess

        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(source)], check=False)
    if link.is_dir() and str(holder) not in sys.path:
        sys.path.append(str(holder))


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


def build_t3_prompt(request: dict[str, Any], skills_dir: Path, candidate: Path) -> str:
    """Hand the task to CodeAgent. File access is pathlib, not a custom tool."""

    task_json = json.dumps(request["task"], ensure_ascii=False, indent=2)
    files = "\n".join(
        f"- {candidate / 'py' / '项目画像.md'}" if name == CANONICAL_PROJECT_FILES[0]
        else f"- {candidate / 'py' / 'prep' / name}"
        for name in CANONICAL_PROJECT_FILES
    )
    return f"""Generate a complete OSIS bridge-model candidate by executing Python.

Use import pathlib to read the mounted skills and to write the candidate files.
Each subdirectory of the skills directory that contains SKILL.md is one skill.
Read the SKILL.md files you need, and any references under those skill directories.

Skills directory:
{skills_dir}

Write these files with real executable PYOSIS code. Create parent directories
with pathlib. Do not leave placeholders.

{files}

Task:
{task_json}

When the files are written, call final_answer with the list of paths you wrote.
"""


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

    from smolagents import CodeAgent, LogLevel, OpenAIServerModel

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

    skills_dir = Path(request["skills_dir"])
    root = candidate_root(request)
    parent = request.get("parent_repo")
    _expose_parent_pyosis(Path(parent) if parent else None)
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
        tools=[],
        model=model,
        # smolagents defaults this to 20. The experiment does not stop on steps.
        max_steps=LIBRARY_LOOP_BOUND,
        verbosity_level=LogLevel.ERROR,
        additional_authorized_imports=ADDITIONAL_AUTHORIZED_IMPORTS,
        return_full_result=True,
    )
    prompt = build_t3_prompt(request, skills_dir, root)
    meta: dict[str, Any] = {
        "architecture_id": "T3",
        "framework": "smolagents",
        "framework_version": _package_version("smolagents"),
        "model": request["model"],
        "model_calls": 0,
        "tool_calls": 0,
        "framework_steps": 0,
        "stop_reason": None,
        "authorized_imports": list(ADDITIONAL_AUTHORIZED_IMPORTS),
        "tools": ["final_answer"],
    }
    try:
        result = agent.run(prompt)
        meta.update(_result_metrics(result))
        usage = getattr(result, "token_usage", None)
        if usage is not None:
            meta["tokens"] = normalize_tokens(usage)
        if request.get("execution_feedback") and request.get("parent_repo"):
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
                nonlocal result
                result = agent.run(observation, reset=False)
                meta["model_calls"] += _result_metrics(result)["model_calls"]
                meta["tool_calls"] += _result_metrics(result)["tool_calls"]
                meta["framework_steps"] += _result_metrics(result)["framework_steps"]

            meta["execution_feedback"] = run_feedback_loop(
                candidate=root,
                scratch_root=Path(request["workspace"]) / "build_feedback",
                deadline_monotonic=deadline,
                observe=_observe,
                resume=_resume,
            )
        meta["agent_state"] = getattr(result, "state", None) and str(getattr(result, "state"))
        meta["status"], meta_error = _state_to_status(meta["agent_state"])
        meta["protocol_normalized_responses"] = model.normalized_response_count
        if meta_error:
            meta["error"] = meta_error
        feedback_stop = (meta.get("execution_feedback") or {}).get("stop_reason")
        if feedback_stop == "task_timeout":
            meta["status"] = "failed"
            meta["stop_reason"] = "task_timeout"
            meta["error"] = "generation exceeded the total task budget"
        else:
            meta["stop_reason"] = feedback_stop or (
                "completed" if meta["status"] == "completed" else "error"
            )
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
