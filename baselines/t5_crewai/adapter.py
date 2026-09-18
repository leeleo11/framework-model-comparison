"""T5 CrewAI adapter (runs inside .venvs/t5).

Verified against crewai 1.15.18 installed source: ``crewai.llm.LLM``
(llm.py:369 -> OpenAICompletion) with ``custom_openai=True`` pins the
chat-completions API (no /v1/responses upgrade, completion.py:1772); loop cap
is ``Agent.max_iter`` (default 25) and per-task wall clock is
``Agent.max_execution_time`` (agent/core.py:244); telemetry is disabled via
env vars read in crewai/telemetry/telemetry.py:162-170.
"""

from __future__ import annotations

import os
import time
from importlib import metadata
from pathlib import Path
from typing import Any

os.environ.setdefault("OTEL_SDK_DISABLED", "true")
os.environ.setdefault("CREWAI_DISABLE_TELEMETRY", "true")
os.environ.setdefault("CREWAI_DISABLE_TRACKING", "true")

from baselines._framework_common import (
    resolve_max_steps, resolve_max_tokens,  # noqa: E402  (after env vars)
    build_prompt,
    candidate_root,
    check_project_completeness as check_project_completeness_shared,
    finish,
    list_reference_files as list_reference_files_shared,
    load_request,
    normalize_tokens,
    read_candidate_file as read_candidate_file_shared,
    reference_cases_payload,
    search_knowledge as _knowledge_search_shared,
    search_skill_cases as search_skill_cases_shared,
    skill_index_payload,
)
from common.tool_policy import tool_error

_STATE: dict[str, Any] = {}

# Do not rely on CrewAI's library defaults for the experiment boundary.  A
# future CrewAI release could enable its code executor or delegation by
# default, which would let a role bypass the bounded ``write_file`` tool and
# create files in an arbitrary working directory (including an OSIS project).
AGENT_POLICY = {
    "allow_code_execution": False,
    "allow_delegation": False,
}


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


def _resolve(relative_path: str) -> Path:
    root = _STATE["candidate_root"]
    resolved = (root / relative_path).resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("path escapes candidate workspace")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    return resolved


def list_skills() -> str:
    """List the ids of all available skills."""
    return "\n".join(
        entry["skill_id"] for entry in _STATE["skill_reader"].skill_index()
    )


def read_skill(skill_id: str) -> str:
    """Read the complete SKILL.md body of one skill.

    Args:
        skill_id: id of the skill, e.g. osis-bridge-cantilever-box
    """
    try:
        return _STATE["skill_reader"].read_skill(skill_id)
    except Exception as exc:  # noqa: BLE001 - let the role self-correct
        return tool_error(exc)


def read_skill_reference(skill_id: str, relative_path: str) -> str:
    """Read one reference file inside a skill directory.

    Args:
        skill_id: id of the skill
        relative_path: path inside the skill folder, e.g. references/templates/<name>/项目画像.md
    """
    try:
        return _STATE["skill_reader"].read_reference(skill_id, relative_path)
    except Exception as exc:  # noqa: BLE001
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
    except Exception as exc:  # noqa: BLE001 - let the role self-correct
        return tool_error(exc)


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
        template_name: the template directory name
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


def build_role_tools(tool_factory: Any) -> dict[str, list[Any]]:
    """Build one identical, bounded tool interface for every CrewAI role.

    Role prompts differ (research, implementation, review), but capability
    access must not become an unreported confounder.  In particular, the
    engineer and reviewer still need to be able to inspect the same mounted
    skills and references as the researcher; the prompts control when they
    use those capabilities.
    """

    functions = (
        list_skills,
        read_skill,
        read_skill_reference,
        list_reference_files,
        write_file,
        search_skill_cases,
        read_candidate_file,
        check_project_completeness,
        search_knowledge,
    )
    shared = [tool_factory(function) for function in functions]
    return {role: list(shared) for role in ("researcher", "engineer", "reviewer")}


def run_generation(request: dict[str, Any]) -> dict[str, Any]:
    started = time.monotonic()
    from crewai import Agent, Crew, Process, Task
    from crewai.llm import LLM
    from crewai.tools import tool

    from common.skill_adapter import SkillAdapter

    _STATE["skill_reader"] = SkillAdapter(Path(request["skills_dir"]))
    _STATE["candidate_root"] = candidate_root(request)

    role_tools = build_role_tools(tool)
    llm = LLM(
        model=request["model"],
        base_url=request["base_url"],
        api_key=request.get("api_key") or os.environ.get("OSIS_MODEL_API_KEY", ""),
        custom_openai=True,
        timeout=float(request["request_timeout_s"]),
        max_tokens=resolve_max_tokens(request.get("max_tokens")),
        temperature=float(request.get("temperature", 0.0)),
    )
    prompt = build_prompt(request, skill_index_payload(request["skills_dir"]),
                          reference_cases_payload(request["skills_dir"]))

    role_budgets = allocate_role_budgets(
        resolve_max_steps(request.get("max_steps"), default=200))
    role_timeouts = allocate_role_timeouts(
        float(request.get("generation_timeout_s") or 3600.0)
    )

    # Multi-agent role division: research -> engineering -> review.
    # The role max_iter values are derived from the shared budget and sum to it
    # exactly.  Wall-clock limits remain per-role safety bounds; the outer
    # runner owns the total task deadline.
    researcher = Agent(
        role="Bridge case researcher",
        goal="Inspect the shared skills, interface docs and reference-case "
             "templates and produce a written design brief for the engineer.",
        backstory="Expert in OSIS/PYOSIS bridge modelling conventions. Reads "
                  "skills and closest reference cases.",
        llm=llm, tools=role_tools["researcher"], max_iter=role_budgets["researcher"],
        **AGENT_POLICY, verbose=False,
    )
    engineer = Agent(
        role="OSIS bridge model engineer",
        goal="Write every canonical candidate-project file with real, "
             "executable PYOSIS code, using the research brief and write_file.",
        backstory="Precise bridge-engineering coder producing the 13 canonical files.",
        llm=llm, tools=role_tools["engineer"], max_iter=role_budgets["engineer"],
        **AGENT_POLICY, verbose=False,
    )
    reviewer = Agent(
        role="Candidate completeness reviewer",
        goal="Call check_completeness and read_candidate, and state "
             "exactly which canonical files remain missing or broken.",
        backstory="Rigorous QA agent. Never fabricates; reports facts only.",
        llm=llm, tools=role_tools["reviewer"], max_iter=role_budgets["reviewer"],
        **AGENT_POLICY, verbose=False,
    )

    research_task = Task(
        description=(
            "Study the task and the available skills/reference cases, then "
            "write a short design brief (structure, materials, key loads).\n\n"
            "Task context:\n" + prompt
        ),
        expected_output="Design brief for the engineer.", agent=researcher,
    )
    engineering_task = Task(
        description=(
            "Using the research brief, write EVERY canonical candidate file "
            "(py/项目画像.md, py/prep/main.py, _0_engine.py .. _10_stage.py) "
            "with write_file. Then call check_completeness until the project is complete."
        ),
        expected_output="All canonical files written; the project is complete.",
        agent=engineer, context=[research_task],
    )
    review_task = Task(
        description=(
            "Verify the candidate: call check_completeness, spot-check files "
            "with read_candidate_file, and report any missing/broken file."
        ),
        expected_output="Review verdict with the exact missing files.",
        agent=reviewer, context=[engineering_task],
    )

    crew = Crew(
        agents=[researcher, engineer, reviewer],
        tasks=[research_task, engineering_task, review_task],
        process=Process.sequential, verbose=False,
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
    }
    try:
        result = crew.kickoff()
        meta.update(_crew_metrics(agents))
        meta["status"] = "completed"
        meta["final_answer"] = str(getattr(result, "raw", ""))[:2000]
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
