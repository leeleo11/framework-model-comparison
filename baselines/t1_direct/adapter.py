"""T1 direct one-shot generation adapter.

The weakest baseline: ONE model request, no agent loop, no tool use. The
prompt injects the task input, the skill index, the visible reference-case
inventory and ONE deterministically retrieved training profile (top-1 by
character-bigram overlap between the task text and the mounted visible
templates' 项目画像.md). The model must emit the whole candidate project in a
single completion using machine-parseable file blocks:

    ### FILE: py/prep/main.py
    <content>

The block parser writes each file through the same candidate-path guard as
every other architecture. Truncation (max_tokens) is a legitimate T1 failure
mode, recorded verbatim in t1_generation.json.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

from baselines.t2_langgraph.adapter import OpenAICompatibleChatModel, _safe_candidate_path
from common.modeling_pipeline import CANONICAL_PROJECT_FILES
from common.skill_adapter import SkillAdapter
from common.task_schema import TaskSpec

FILE_BLOCK = re.compile(r"^### FILE:\s*(?P<path>.+?)\s*$", re.MULTILINE)
BIGRAMS = re.compile(r"[一-鿿]|[a-zA-Z0-9]+")


def _retrieve_profile(skill_reader: SkillAdapter, bridge_skill: str, text: str) -> str:
    """Deterministic top-1 visible reference profile for one-shot context."""

    inventory = skill_reader.visible_template_inventory()
    names = inventory.get(bridge_skill) or []
    if not names:
        return ""
    def bigrams(value: str) -> set[str]:
        tokens = BIGRAMS.findall(value)
        return {value[i:i + 2] for i in range(len(value) - 1)} | set(tokens)

    query = bigrams(text)

    def overlap(name: str) -> int:
        profile_path = f"references/templates/{name}/项目画像.md"
        try:
            profile = skill_reader.read_reference(bridge_skill, profile_path)
        except Exception:  # noqa: BLE001 - missing profile just ranks last
            return -1
        return len(query & bigrams(profile))

    best = max(sorted(names), key=overlap)
    try:
        return skill_reader.read_reference(bridge_skill, f"references/templates/{best}/项目画像.md")
    except Exception:  # noqa: BLE001
        return ""


def _knowledge_package(
    skill_reader: SkillAdapter, bridge_skill: str, text: str, char_budget: int = 100_000
) -> str:
    """Deterministic one-shot-equivalent knowledge package.

    Injects (within a fixed character budget) the bridge SKILL body, the
    engine/API interface skill, and the closest visible template's full code
    (profile + several prep files) — so T1 receives real SKILL and template
    code, not just names. Deterministic: same task -> same package.
    """

    parts: list[str] = []
    used = 0

    def push(title: str, body: str) -> None:
        nonlocal used
        if not body:
            return
        block = f"### {title}\n{body}\n"
        if used + len(block) > char_budget:
            block = block[: char_budget - used]
            parts.append(block)
            used = char_budget
            return
        parts.append(block)
        used += len(block)

    for skill_id in (bridge_skill, "osis-engine", "osis-python-helper"):
        try:
            push(f"SKILL: {skill_id}", skill_reader.read_skill(skill_id))
        except Exception:  # noqa: BLE001
            continue

    best = _retrieve_profile(skill_reader, bridge_skill, text)
    push("CLOSEST VISIBLE PROFILE (项目画像.md)", best)

    # code exemplars from the closest visible template
    inventory = skill_reader.visible_template_inventory().get(bridge_skill) or []
    chosen = None
    if best and inventory:
        for name in inventory:
            try:
                if skill_reader.read_reference(
                    bridge_skill, f"references/templates/{name}/项目画像.md"
                ) == best:
                    chosen = name
                    break
            except Exception:  # noqa: BLE001
                continue
    if chosen is None and inventory:
        chosen = inventory[0]
    if chosen:
        for rel in ("prep/main.py", "prep/_0_engine.py", "prep/_4_section.py",
                    "prep/_5_node.py", "prep/_8_loadcase.py"):
            try:
                body = skill_reader.read_reference(
                    bridge_skill, f"references/templates/{chosen}/{rel}"
                )
            except Exception:  # noqa: BLE001
                continue
            push(f"EXEMPLAR {chosen}/{rel}", body)
    return "\n".join(parts) if used < char_budget else "\n".join(parts)


def build_t1_prompt(task: TaskSpec, skill_reader: SkillAdapter, bridge_skill: str) -> str:
    # T1 remains one-shot, but it must receive the same canonical task object
    # as every other architecture.  The one-shot constraint is the framework
    # variable; silently dropping difficulty, expected fields or timeouts is
    # an input-confounder.
    task_json = json.dumps(task.to_dict(), ensure_ascii=False, indent=2)
    skill_index = json.dumps(skill_reader.skill_index(), ensure_ascii=False, indent=2)
    reference_cases = json.dumps(skill_reader.visible_template_inventory(), ensure_ascii=False, indent=2)
    # Full-corpus injection: a one-shot agent has no way to pull knowledge on
    # demand, so the fair fixed_bundle is the ENTIRE production snapshot (all
    # 31 skill bodies, the same bytes every architecture mounts).  ~185k chars
    # ≈ 97k tokens, inside the 256k context; wall-clock remains the only gate.
    knowledge = skill_reader.fixed_bundle_text()
    # Knowledge-base pre-fetch (T1's only possible "挂知识库": retrieval by the
    # harness before the single call, since the model itself has no tools).
    # Same Weknora corpus T2-T6 reach through their own channels.
    try:
        from baselines._framework_common import knowledge_search, knowledge_search_enabled

        if knowledge_search_enabled():
            kb_hits = knowledge_search(
                f"{task.natural_language_requirement[:800]} pyosis API 参数 签名 用法",
                top_k=8,
            )
            knowledge += (
                "\n\n### KNOWLEDGE BASE RETRIEVAL (Weknora, top hits for this task)\n"
                + kb_hits[:20000]
            )
    except Exception:  # noqa: BLE001 - retrieval is additive, never fatal
        pass
    canonical_markers = "\n".join(
        [
            "### FILE: py/项目画像.md",
            *[f"### FILE: py/prep/{name}" for name in CANONICAL_PROJECT_FILES[1:]],
        ]
    )
    return f"""You are T1, a direct one-shot OSIS bridge-model generator. You get ONE
response: no tools, no follow-ups. Output the complete candidate project.

Task:
{task_json}

Shared skill index:
{skill_index}

Reference cases you may imitate (visible templates):
{reference_cases}

Knowledge package (SKILL bodies, closest visible profile and exemplar code):
{knowledge}

Output contract — output exactly one block for EACH of the following 13 files,
in this order. Every block starts with its exact marker line; put the complete
UTF-8 file content below it, and put nothing else outside the blocks:

{canonical_markers}

Write real executable PYOSIS code for every file. Do not abbreviate with
placeholders. Do not add commentary outside the file blocks.
"""


def parse_file_blocks(text: str) -> list[tuple[str, str]]:
    matches = list(FILE_BLOCK.finditer(text))
    files: list[tuple[str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        content = text[start:end].strip()
        if content.startswith("```"):
            content = content.split("\n", 1)[1] if "\n" in content else content
            if content.rstrip().endswith("```"):
                content = content.rstrip()[:-3]
        files.append((match.group("path").strip(), content.strip() + "\n"))
    return files


def generate_t1(
    task: TaskSpec,
    skill_reader: SkillAdapter,
    workspace: Path,
    *,
    bridge_skill: str,
    base_url: str,
    model: str,
    api_key: str,
    max_tokens: int,
    request_timeout_s: float,
    temperature: float = 0.0,
    reasoning_effort: str | None = None,
) -> Path:
    started = time.monotonic()
    workspace = Path(workspace)
    candidate_root = workspace / "candidate_project"
    candidate_root.mkdir(parents=True, exist_ok=True)
    metadata: dict[str, Any] = {
        "architecture_id": "T1",
        "framework": "direct-one-shot",
        "framework_version": "direct-one-shot-v1",
        "model": model,
        "base_url": base_url,
        "status": "failed",
        "file_blocks": 0,
        "model_calls": 0,
        "tool_calls": 0,
        "stop_reason": None,
    }
    try:
        llm = OpenAICompatibleChatModel(
            model=model,
            base_url=base_url,
            api_key=api_key,
            temperature=temperature,
            max_tokens=max_tokens,
            request_timeout_s=request_timeout_s,
            reasoning_effort=reasoning_effort,
        )
        from langchain_core.messages import HumanMessage

        # The one-shot request counts as one model call even when the remote
        # endpoint fails before returning a completion.  This is an attempted
        # call, not a fabricated token count.
        metadata["model_calls"] = 1
        response = llm._generate(
            [HumanMessage(content=build_t1_prompt(task, skill_reader, bridge_skill))]
        )
        text = response.generations[0].message.content or ""
        metadata["response_chars"] = len(text)
        # capture token usage so the cost dimension is not 0 for T1
        message = response.generations[0].message
        response_metadata = message.response_metadata or {}
        metadata["stop_reason"] = response_metadata.get("finish_reason")
        usage = (message.response_metadata or {}).get("usage")
        if usage:
            from .._framework_common import normalize_tokens

            metadata["tokens"] = normalize_tokens(usage)
        files = parse_file_blocks(text)
        metadata["file_blocks"] = len(files)
        written = []
        for rel_path, content in files:
            target = _safe_candidate_path(candidate_root, rel_path)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            written.append(target.as_posix())
        metadata["files_written"] = written
        metadata["status"] = "completed" if files else "no_file_blocks"
        if metadata["stop_reason"] is None:
            metadata["stop_reason"] = metadata["status"]
    except Exception as exc:  # noqa: BLE001 - record and surface via metadata
        metadata["error_type"] = type(exc).__name__
        metadata["error"] = str(exc)[:400]
        metadata["stop_reason"] = "error"
    finally:
        metadata["elapsed_s"] = time.monotonic() - started
        (workspace / "t1_generation.json").write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    return candidate_root
