"""T5 CrewAI adapter (runs inside .venvs/t5).

Verified against crewai 1.15.22 installed source: ``crewai.llm.LLM``
(llm.py:369 -> OpenAICompletion) with ``custom_openai=True`` pins the
chat-completions API (no /v1/responses upgrade, completion.py:1772). CrewAI's
own ``Agent.max_iter`` defaults to 25; the adapter overrides that default
because this experiment has no step cap. Telemetry is disabled via
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
    LIBRARY_LOOP_BOUND,
    candidate_root,
    finish,
    load_request,
    normalize_tokens,
    resolve_max_tokens,
)
from common.execution_feedback import observe_candidate, run_feedback_loop
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
_SKILL_NAME = re.compile(r"(?m)^name:\s*(?P<name>\S+)\s*$")
_RESOURCE_ROOTS = ("references", "scripts", "assets")


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


def _find_skill_dir(skills_dir: Path, skill_name: str) -> Path | None:
    for child in Path(skills_dir).iterdir():
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.is_file():
            continue
        if child.name == skill_name:
            return child
        match = _SKILL_NAME.search(skill_md.read_text(encoding="utf-8", errors="replace"))
        if match and match.group("name") == skill_name:
            return child
    return None


def read_skill_resource(skills_dir: Path, skill_name: str, relative_path: str = "") -> str:
    """List resource names, or return one file under references, scripts, or assets.

    CrewAI's load_skill stops at the SKILL.md body. This is the resource step
    of the same progressive disclosure: names first, then a single file.
    """

    skill_dir = _find_skill_dir(skills_dir, skill_name)
    if skill_dir is None:
        names = sorted(
            child.name
            for child in Path(skills_dir).iterdir()
            if child.is_dir() and (child / "SKILL.md").is_file()
        )
        available = ", ".join(names) or "none"
        return f"Skill {skill_name!r} is not available. Available skills: {available}."
    relative = relative_path.strip().replace("\\", "/")
    if not relative:
        lines: list[str] = []
        for folder in _RESOURCE_ROOTS:
            base = skill_dir / folder
            if not base.is_dir():
                continue
            files = sorted(
                path.relative_to(base).as_posix()
                for path in base.rglob("*")
                if path.is_file()
            )
            if files:
                lines.append(f"{folder}/: " + ", ".join(files))
        return "\n".join(lines) or "No resource files."
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts or not path.parts or path.parts[0] not in _RESOURCE_ROOTS:
        return "Path must be one file inside references/, scripts/, or assets/."
    target = (skill_dir / path).resolve()
    root = (skill_dir / path.parts[0]).resolve()
    if not target.is_relative_to(root):
        return "Path must be one file inside references/, scripts/, or assets/."
    if not target.is_file():
        return f"File not found: {path.as_posix()}"
    return target.read_text(encoding="utf-8", errors="replace")


def _canonical_paths() -> str:
    paths = ["py/项目画像.md", *[f"py/prep/{name}" for name in CANONICAL_PROJECT_FILES[1:]]]
    return "\n".join(paths)


def _candidate_file_tools(root: Path, *, write: bool) -> list[Any]:
    """CrewAI's own file tools, confined to the candidate project."""

    from crewai_tools import DirectoryReadTool, FileReadTool, FileWriterTool

    tools: list[Any] = [
        DirectoryReadTool(directory=str(root)),
        FileReadTool(base_dir=str(root)),
    ]
    if write:
        tools.append(FileWriterTool(base_dir=str(root)))
    return tools


def _files_on_disk(root: Path) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    )


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
    from crewai.tools import tool

    root = candidate_root(request)
    _STATE["candidate_root"] = root
    skills_dir = Path(request["skills_dir"])

    @tool("read_skill_resource")
    def read_skill_resource_tool(skill_name: str, relative_path: str = "") -> str:
        """List resource file names, or read one file under references, scripts, or assets."""

        return read_skill_resource(skills_dir, skill_name, relative_path)

    resource_tools = [read_skill_resource_tool]

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

    # Skills are mounted through CrewAI's own skills= path.  Delegation is on,
    # so kickoff adds "Delegate work to coworker" and "Ask question to coworker".
    # load_skill returns SKILL.md only. read_skill_resource is the missing
    # resource step: list names, then read one file. CrewAI's default max_iter
    # is 25; override it so a role is not cut off before the wall clock.
    read_tools = _candidate_file_tools(root, write=False)
    write_tools = _candidate_file_tools(root, write=True)
    researcher = Agent(
        role="Bridge case researcher",
        goal="Load the relevant mounted skills and produce a written design brief.",
        backstory="Expert in OSIS/PYOSIS bridge modelling conventions. Uses the crew's own load_skill tool.",
        llm=llm, max_iter=LIBRARY_LOOP_BOUND, tools=resource_tools,
        **AGENT_POLICY, verbose=False,
    )
    engineer = Agent(
        role="OSIS bridge model engineer",
        goal="Write every canonical candidate-project file with the file writer, then repair the reviewer's fix list.",
        backstory="Precise bridge-engineering coder. Writes the candidate project with CrewAI file tools.",
        llm=llm, max_iter=LIBRARY_LOOP_BOUND, tools=[*resource_tools, *write_tools],
        **AGENT_POLICY, verbose=False,
    )
    reviewer = Agent(
        role="Candidate reviewer",
        goal="Read the candidate files and return a concrete fix list.",
        backstory="Rigorous QA agent. Reads files with CrewAI file tools and does not write them.",
        llm=llm, max_iter=LIBRARY_LOOP_BOUND, tools=[*resource_tools, *read_tools],
        **AGENT_POLICY, verbose=False,
    )

    task_json = json.dumps(request["task"], ensure_ascii=False, indent=2)
    paths = _canonical_paths()
    research_task = Task(
        description=(
            "Use the crew's load_skill tool to read the relevant mounted "
            "skills. load_skill returns the SKILL.md body. To see a template, "
            "call read_skill_resource with an empty path for the file names, "
            "then call it again with one path under references/, scripts/, "
            "or assets/. You may also use \"Delegate work to coworker\" and "
            "\"Ask question to coworker\". "
            "Return a design brief the engineer can implement "
            "(structure, materials, key loads).\n\nTask:\n" + task_json
        ),
        expected_output="Design brief grounded in the loaded skills.",
        agent=researcher,
    )
    engineering_task = Task(
        description=(
            "From the research brief and the mounted skills, write the "
            "complete candidate project with the File Writer Tool. The tool "
            "is confined to the candidate project. Pass each path as the "
            "filename, for example py/prep/main.py. Read a template with "
            "read_skill_resource before writing a file that has one. Read a "
            "file back with the file reader before replacing it. Write every "
            "path below. Do not put the file bodies in the final message.\n\n"
            + paths + "\n\nTask:\n" + task_json
        ),
        expected_output="Every canonical file written into the candidate project.",
        agent=engineer, context=[research_task],
    )
    review_task = Task(
        description=(
            "Read the candidate files with the file reader and directory "
            "listing tools. Do not write files. Report every missing "
            "canonical file and every concrete defect: empty or placeholder "
            "arguments, API calls that do not match the skill, and files "
            "that are not executable PYOSIS. If the candidate is acceptable, "
            "say that no change is required. Use \"Delegate work to "
            "coworker\" or \"Ask question to coworker\" when the engineer "
            "should repair or clarify a specific defect."
        ),
        expected_output=(
            "A fix list naming each missing file or concrete defect, or an "
            "explicit statement that no change is required."
        ),
        agent=reviewer, context=[engineering_task],
    )
    revise_task = Task(
        description=(
            "Apply the reviewer's fix list by editing the candidate files "
            "with the file reader and the File Writer Tool. Change only the "
            "files that need a fix. If the reviewer stated that no change is "
            "required, leave the files as they are.\n\n" + paths
        ),
        expected_output="The candidate project files updated on disk.",
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
        "model_calls": 0,
        "tool_calls": 0,
        "stop_reason": None,
        "collaboration_tools": [
            tool.name
            for tool in crew._prepare_tools(researcher, research_task, [])
        ],
        "file_tools": [tool.name for tool in engineer.tools if tool.name != "read_skill_resource"],
    }
    try:
        result = crew.kickoff()
        raw = str(getattr(result, "raw", ""))
        written = _files_on_disk(root)
        meta.update(_crew_metrics(agents))
        meta["status"] = "completed"
        meta["files_written"] = written
        meta["final_answer"] = raw[:2000]
        usage = getattr(result, "token_usage", None)
        if usage is not None:
            meta["tokens"] = normalize_tokens(usage)
        meta["stop_reason"] = "completed"
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
                revision = Task(
                    description=(
                        observation + "\n\nEdit the existing candidate files "
                        "with the file reader and the File Writer Tool. "
                        "Change the files that caused this error.\n\n" + paths
                    ),
                    expected_output="The candidate project files updated on disk.",
                    agent=engineer,
                )
                Crew(
                    agents=[engineer],
                    tasks=[revision],
                    process=Process.sequential,
                    verbose=False,
                    skills=[skills_dir],
                ).kickoff()
                meta["files_written"] = _files_on_disk(root)
                meta.update(_crew_metrics(agents))

            feedback = run_feedback_loop(
                candidate=root,
                scratch_root=Path(request["workspace"]) / "build_feedback",
                deadline_monotonic=deadline,
                observe=_observe,
                resume=_resume,
            )
            meta["execution_feedback"] = feedback
            if feedback.get("stop_reason") == "task_timeout":
                meta["status"] = "failed"
                meta["stop_reason"] = "task_timeout"
                meta["error"] = "generation exceeded the total task budget"
            elif feedback.get("stop_reason"):
                meta["stop_reason"] = feedback["stop_reason"]
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
