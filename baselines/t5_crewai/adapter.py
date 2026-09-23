"""T5 CrewAI adapter (runs inside .venvs/t5).

Verified against crewai 1.15.18 installed source: ``crewai.llm.LLM``
(llm.py:369 -> OpenAICompletion) with ``custom_openai=True`` pins the
chat-completions API (no /v1/responses upgrade, completion.py:1772); loop cap
is ``Agent.max_iter`` (default 25) and per-task wall clock is
``Agent.max_execution_time`` (agent/core.py:244); telemetry is disabled via
env vars read in crewai/telemetry/telemetry.py:162-170.
"""

from __future__ import annotations

import json
import os
import re
import time
from importlib import metadata
from pathlib import Path
from typing import Any

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")

from baselines._framework_common import (  # noqa: E402
    candidate_root,
    finish,
    load_request,
    normalize_tokens,
    resolve_max_steps,
    resolve_max_tokens,
)
from common.modeling_pipeline import CANONICAL_PROJECT_FILES

_STATE: dict[str, Any] = {}

# CrewAI 1.15 injects DelegateWork and AskQuestion when delegation is on, and
# injects load_skill when a skill directory is mounted on the crew.  Its code
# interpreter is deprecated and no longer registers a tool, so leave it off.
AGENT_POLICY = {
    "allow_code_execution": False,
    "allow_delegation": True,
}

_FILE_BLOCK = re.compile(r"^### FILE:\s*(?P<path>.+?)\s*$", re.MULTILINE)
_FENCE_LINE = re.compile(r"^```[A-Za-z0-9_+-]*\s*$")


def _package_version(name: str) -> str:
    try:
        return metadata.version(name)
    except metadata.PackageNotFoundError:
        return "unavailable"


def _crew_metrics(agents: Any) -> dict[str, Any]:
    """Collect role iterations and native tool-call counts when available."""

    role_iterations: dict[str, int] = {}
    model_calls = 0
    tool_calls = 0
    for agent in list(agents or []):
        role = str(getattr(agent, "role", "agent"))
        executor = getattr(agent, "agent_executor", None)
        iterations = getattr(executor, "iterations", None)
        if isinstance(iterations, int):
            role_iterations[role] = iterations
        messages = getattr(agent, "last_messages", None) or []
        for message in messages:
            if isinstance(message, dict):
                message_role = str(message.get("role") or "")
                calls = message.get("tool_calls") or []
            else:
                message_role = str(getattr(message, "role", "") or "")
                calls = getattr(message, "tool_calls", None) or []
            if message_role.lower() in {"assistant", "ai"}:
                model_calls += 1
            if isinstance(calls, (list, tuple)):
                tool_calls += len(calls)
    return {
        "role_iterations": role_iterations,
        "model_calls": model_calls,
        "tool_calls": tool_calls,
    }


def allocate_role_budgets(max_steps: int) -> dict[str, int]:
    """Split the shared model-call budget across CrewAI's three roles.

    Research and review receive fixed proportions; engineering receives the
    remainder so the sum is exactly the caller's budget.  At least one call is
    reserved for each role because CrewAI runs them sequentially.
    """

    total = int(max_steps)
    if total < 3:
        raise ValueError("T5 requires at least three model calls (one per role)")
    researcher = max(1, int(total * 0.25 + 0.5))
    reviewer = max(1, int(total * 0.15 + 0.5))
    engineer = total - researcher - reviewer
    if engineer < 1:
        # This branch is only reachable for very small totals; preserve the
        # three-role invariant while keeping the sum equal to ``total``.
        reviewer = max(1, total - researcher - 1)
        engineer = total - researcher - reviewer
    return {
        "researcher": researcher,
        "engineer": engineer,
        "reviewer": reviewer,
    }


def allocate_role_timeouts(generation_timeout_s: float) -> dict[str, int]:
    """Placeholder kept for interface compatibility; always returns None.

    Per-role ``max_execution_time`` is intentionally removed so CrewAI never
    prematurely kills a role before the outer runner's one-hour generation
    deadline (the only real governor).  The Agent constructors below skip
    ``max_execution_time`` entirely when this returns None.
    """

    return None


def _canonical_markers() -> str:
    paths = ["py/项目画像.md", *[f"py/prep/{name}" for name in CANONICAL_PROJECT_FILES[1:]]]
    return "\n".join(f"### FILE: {path}" for path in paths)


def _unwrap_file_body(content: str) -> str:
    """Keep the file body from a task answer.

    CrewAI returns plain text. Models still wrap a file in a markdown fence
    and may add a ``---`` rule after it. Those lines are not part of the file.
    """

    lines = content.strip().splitlines()
    if lines and _FENCE_LINE.match(lines[0].strip()):
        lines = lines[1:]
    while lines and (not lines[-1].strip() or lines[-1].strip() == "---" or _FENCE_LINE.match(lines[-1].strip())):
        lines.pop()
    return "\n".join(lines).rstrip() + "\n"


def _materialize_file_blocks(text: str, root: Path) -> list[str]:
    """Write the crew's final answer into the candidate project.

    This is the harness boundary after kickoff.  The model is not given a
    custom write tool; CrewAI's own task output is the file source.
    """

    matches = list(_FILE_BLOCK.finditer(text))
    written: list[str] = []
    root = root.resolve()
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = _unwrap_file_body(text[start:end])
        relative = match.group("path").strip().replace("\\", "/").lstrip("/")
        target = (root / relative).resolve()
        if not target.is_relative_to(root):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content.strip() + "\n", encoding="utf-8")
        written.append(relative)
    return written


def run_generation(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    from crewai import Agent, Crew, Process, Task
    from crewai.llm import LLM

    root = candidate_root(request)
    _STATE["candidate_root"] = root
    skills_dir = Path(request["skills_dir"])

    llm = LLM(
        model=request["model"],
        base_url=request["base_url"],
        api_key=request.get("api_key") or os.environ.get("OSIS_MODEL_API_KEY", ""),
        custom_openai=True,
        timeout=float(request["request_timeout_s"]),
        max_tokens=resolve_max_tokens(request.get("max_tokens")),
        temperature=float(request.get("temperature", 0.0)),
        **({"reasoning_effort": request["reasoning_effort"]}
           if request.get("reasoning_effort") else {}),
    )

    role_budgets = allocate_role_budgets(
        resolve_max_steps(request.get("max_steps"), default=200))
    role_timeouts = allocate_role_timeouts(
        float(request.get("generation_timeout_s") or 3600.0)
    )

    # Skills are mounted through CrewAI's own skills= path.  Delegation is on,
    # so kickoff adds the official tools "Delegate work to coworker" and
    # "Ask question to coworker".  No custom tools.
    researcher = Agent(
        role="Bridge case researcher",
        goal="Load the relevant mounted skills and produce a written design brief.",
        backstory="Expert in OSIS/PYOSIS bridge modelling conventions. Uses the crew's own load_skill tool.",
        llm=llm, max_iter=role_budgets["researcher"],
        **AGENT_POLICY, verbose=False,
    )
    engineer = Agent(
        role="OSIS bridge model engineer",
        goal="Write every canonical candidate-project file as FILE blocks, then repair the reviewer's fix list.",
        backstory="Precise bridge-engineering coder producing the 13 canonical files from loaded skills.",
        llm=llm, max_iter=role_budgets["engineer"],
        **AGENT_POLICY, verbose=False,
    )
    reviewer = Agent(
        role="Candidate reviewer",
        goal="Check the engineer's FILE blocks against the mounted skills and return a concrete fix list.",
        backstory="Rigorous QA agent. Names defects and may ask the engineer through the crew's delegation tool.",
        llm=llm, max_iter=role_budgets["reviewer"],
        **AGENT_POLICY, verbose=False,
    )

    task_json = json.dumps(request["task"], ensure_ascii=False, indent=2)
    markers = _canonical_markers()
    research_task = Task(
        description=(
            "Use the crew's load_skill tool to read the relevant mounted "
            "skills. You may also use the crew's own collaboration tools, "
            "\"Delegate work to coworker\" and \"Ask question to coworker\". "
            "Return a design brief the engineer can implement "
            "(structure, materials, key loads).\n\nTask:\n" + task_json
        ),
        expected_output="Design brief grounded in the loaded skills.",
        agent=researcher,
    )
    engineering_task = Task(
        description=(
            "From the research brief and the mounted skills, write the "
            "complete candidate project. Output exactly one block for each "
            "canonical file, in this order. Every block starts with its "
            "marker line and contains the complete file. Put nothing else "
            "outside the blocks.\n\n" + markers + "\n\nTask:\n" + task_json
        ),
        expected_output="The complete candidate project as one FILE block per canonical file.",
        agent=engineer, context=[research_task],
    )
    review_task = Task(
        description=(
            "Review the engineer's FILE blocks against the mounted skills. "
            "Do not rewrite the files. Report every missing canonical file "
            "and every concrete defect: empty or placeholder arguments, API "
            "calls that do not match the skill, and files that are not "
            "executable PYOSIS. If the candidate is acceptable, say that no "
            "change is required. Use \"Delegate work to coworker\" or "
            "\"Ask question to coworker\" when the engineer should repair or "
            "clarify a specific defect."
        ),
        expected_output=(
            "A fix list naming each missing file or concrete defect, or an "
            "explicit statement that no change is required."
        ),
        agent=reviewer, context=[engineering_task],
    )
    revise_task = Task(
        description=(
            "Apply the reviewer's fix list. Output the complete candidate "
            "again, exactly one FILE block per canonical file, in this order. "
            "If the reviewer stated that no change is required, repeat the "
            "engineer's blocks unchanged.\n\n" + markers
        ),
        expected_output="The final candidate project as one FILE block per canonical file.",
        agent=engineer, context=[engineering_task, review_task],
    )

    crew = Crew(
        agents=[researcher, engineer, reviewer],
        tasks=[research_task, engineering_task, review_task, revise_task],
        process=Process.sequential, verbose=False,
        skills=[skills_dir],
    )
    agents = [researcher, engineer, reviewer]
    meta: dict[str, Any] = {
        "architecture_id": "T5",
        "framework": "crewai",
        "framework_version": _package_version("crewai"),
        "model": request["model"],
        "agents": ["researcher", "engineer", "reviewer"],
        "role_budgets": role_budgets,
        "role_timeouts": role_timeouts,
        "model_calls": 0,
        "tool_calls": 0,
        "stop_reason": None,
        "collaboration_tools": [
            tool.name
            for tool in crew._prepare_tools(researcher, research_task, [])
        ],
    }
    try:
        result = crew.kickoff()
        raw = str(getattr(result, "raw", ""))
        written = _materialize_file_blocks(raw, root)
        meta.update(_crew_metrics(agents))
        meta["status"] = "completed"
        meta["files_written"] = written
        meta["final_answer"] = raw[:2000]
        usage = getattr(result, "token_usage", None)
        if usage is not None:
            meta["tokens"] = normalize_tokens(usage)
        exhausted = any(
            role in meta.get("role_iterations", {})
            and meta["role_iterations"][role] >= role_budgets[role]
            for role in role_budgets
        )
        meta["stop_reason"] = "max_steps" if exhausted else "completed"
    except Exception as exc:  # noqa: BLE001
        meta.update(_crew_metrics(agents))
        import traceback

        meta["status"] = "failed"
        meta["error_type"] = type(exc).__name__
        meta["error"] = str(exc)[:500]
        meta["traceback"] = traceback.format_exc()
        meta["stop_reason"] = "error"
    return finish(Path(request["workspace"]), "T5", meta, started)


def main() -> int:
    request = load_request()
    run_generation(request)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
