"""Shared scaffolding for the framework-venv adapters (T3/T4/T5).

Each adapter runs INSIDE its framework's virtualenv (subprocessed by
``scripts/run_dataset.py``), receives one ``--request <path>`` JSON with the
frozen run configuration, writes its candidate project under
``workspace/candidate_project`` (already created and prestaged by the outer
driver), records ``workspace/tX_generation.json``, and prints its metadata as
the last stdout line for the outer driver to parse.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

# ``common`` is a sibling package of ``baselines`` under the framework root, so
# the root is one level up from this file -- not two.  (The previous
# ``parents[2]`` pointed at the directory that contained the framework, which
# was only ever importable by accident: it holds no ``common`` package.  The
# driver masked that by injecting the framework root into PYTHONPATH.)
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))


def load_request(argv: list[str] | None = None) -> dict[str, Any]:
    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args(argv)
    return json.loads(Path(args.request).read_text(encoding="utf-8"))


# A frozen limit of ``0`` means "no limit" in this protocol: wall-clock is the
# sole termination mechanism, so every architecture is measured on what it can
# build in the budget rather than on how verbose a single response happened to
# be.  Framework APIs require a concrete integer, so unbounded limits are
# expressed as a value far above anything reachable inside the one-hour
# generation gate; the time gate still stops the run.
UNBOUNDED_STEPS = 100_000
UNBOUNDED_TOKENS = 0  # sentinel meaning "omit the field"; see resolve_max_tokens


def resolve_max_steps(value: Any, *, default: int) -> int:
    """Map the protocol's ``0``/unset to the framework's unbounded value."""

    try:
        steps = int(value)
    except (TypeError, ValueError):
        return default
    return steps if steps > 0 else UNBOUNDED_STEPS


def resolve_max_tokens(value: Any) -> int | None:
    """Return the per-request output cap, or ``None`` to omit it entirely."""

    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    return tokens if tokens > 0 else None


# ---- Weknora knowledge-base retrieval (v3 "工具相同" channel) -------------
# All architectures get the same retrieval capability through their own
# official mechanism: T2-T5 as a harness tool, T1 as pre-fetch injection,
# T6 via the script+doc bundled into the skill snapshot.  The key travels
# through the environment only; it is never written into run artifacts.

KNOWLEDGE_SEARCH_DEFAULT_URL = "https://knowledge.osisbim.com/api/v1"


def knowledge_search_config() -> dict[str, Any]:
    return {
        "base_url": os.environ.get("WEKNORA_BASE_URL", KNOWLEDGE_SEARCH_DEFAULT_URL).rstrip("/"),
        "api_key": os.environ.get("WEKNORA_API_KEY", ""),
        "kb_ids": [
            part.strip()
            for part in os.environ.get("WEKNORA_KB_IDS", "").split(",")
            if part.strip()
        ],
    }


def knowledge_search_enabled() -> bool:
    cfg = knowledge_search_config()
    return bool(cfg["api_key"] and cfg["kb_ids"])


def search_knowledge(query: str, *, top_k: int = 5) -> str:
    """Query the Weknora knowledge base and format the top hits as text.

    Returns a ``TOOL_ERROR:``-prefixed message on misconfiguration or
    transport failure so the agent can degrade gracefully (same convention
    as the other bounded tools), never raising into the framework loop.
    """

    import json as _json
    import urllib.request

    cfg = knowledge_search_config()
    if not cfg["api_key"] or not cfg["kb_ids"]:
        return "TOOL_ERROR: 知识库未配置（需要环境变量 WEKNORA_API_KEY 与 WEKNORA_KB_IDS）"
    query = (query or "").strip()
    if not query:
        return "TOOL_ERROR: 检索词为空"
    payload = _json.dumps({
        "query": query[:2000],
        "knowledge_base_ids": cfg["kb_ids"],
    }).encode("utf-8")
    request = urllib.request.Request(
        cfg["base_url"] + "/knowledge-search",
        data=payload,
        # The gateway's WAF rejects the default ``Python-urllib`` user agent
        # with 403; identify the benchmark client instead.
        headers={
            "X-API-Key": cfg["api_key"],
            "Content-Type": "application/json",
            "User-Agent": "osis-framework-benchmark/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            body = _json.loads(response.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001 - degrade, do not crash the loop
        return f"TOOL_ERROR: 知识库检索失败: {type(exc).__name__}: {exc}"
    data = body.get("data")
    chunks = data if isinstance(data, list) else (data or {}).get("chunks") or []
    if not chunks:
        return "知识库无命中。可改用 read_skill / read_skill_reference / pyosis_doc 查询。"
    formatted: list[str] = []
    for index, chunk in enumerate(chunks[:top_k], 1):
        content = (chunk.get("content") or "").strip()
        source = (chunk.get("document_name") or chunk.get("knowledge_name")
                  or chunk.get("filename") or "")
        score = chunk.get("score")
        header = f"[{index}] {source}" + (f" (score={score:.3f})" if isinstance(score, (int, float)) else "")
        formatted.append(f"{header}\n{content[:3000]}")
    return "\n\n".join(formatted)


def candidate_root(request: dict[str, Any]) -> Path:
    root = Path(request["workspace"]) / "candidate_project"
    root.mkdir(parents=True, exist_ok=True)
    return root


def finish(workspace: Path, architecture_id: str, meta: dict[str, Any], started: float) -> dict[str, Any]:
    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    meta.setdefault("architecture_id", architecture_id)
    meta["elapsed_s"] = time.monotonic() - started
    candidate = workspace / "candidate_project"
    if candidate.is_dir():
        meta["files_written"] = sorted(
            p.relative_to(candidate).as_posix() for p in candidate.rglob("*") if p.is_file()
        )
    (workspace / f"{architecture_id.lower()}_generation.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(meta, ensure_ascii=False))
    return meta


def build_prompt(request: dict[str, Any], skill_index: Any, reference_cases: Any) -> str:
    """Shared task prompt for tool-loop architectures (T3/T4/T5)."""

    task_json = json.dumps(request["task"], ensure_ascii=False, indent=2)
    return f"""You are generating a complete OSIS bridge-model candidate project.

Task:
{task_json}

Available skills (read them with the tools before writing code):
{skill_index}

Reference cases you may imitate (visible templates, read via
read_skill_reference, e.g. relative_path "references/templates/<name>/项目画像.md"):
{reference_cases}

Contract:
- Inspect skills and references first, then write EVERY canonical file with
  write_file: py/项目画像.md and py/prep/main.py, py/prep/_0_engine.py,
  py/prep/_1_control.py, py/prep/_2_property.py, py/prep/_3_material.py,
  py/prep/_4_section.py, py/prep/_5_node.py, py/prep/_6_element.py,
  py/prep/_7_boundary.py, py/prep/_8_loadcase.py, py/prep/_9_analysis.py,
  py/prep/_10_stage.py.
- Write real executable PYOSIS code; never placeholders.
- Work in small inspect -> write -> inspect steps.
- Finish by calling check_project_completeness, then report the list of files
  you wrote.
"""


def skill_index_payload(skills_dir: str) -> str:
    from common.skill_adapter import SkillAdapter

    return json.dumps(SkillAdapter(Path(skills_dir)).skill_index(), ensure_ascii=False, indent=2)


def reference_cases_payload(skills_dir: str) -> str:
    """Per-skill visible templates INCLUDING their internal file inventory.

    Listing the files inside each reference case removes the guessing game
    (``prep/_*.py`` paths) that pushed T2 into unbounded read loops.
    """

    from common.skill_adapter import SkillAdapter

    adapter = SkillAdapter(Path(skills_dir))
    inventory = adapter.visible_template_inventory()
    detailed: dict[str, dict[str, list[str]]] = {}
    for skill_id, names in inventory.items():
        detailed[skill_id] = {}
        for name in names:
            template_dir = Path(skills_dir) / skill_id / "references" / "templates" / name
            files = sorted(
                p.relative_to(template_dir).as_posix()
                for p in template_dir.rglob("*")
                if p.is_file()
            ) if template_dir.is_dir() else []
            detailed[skill_id][name] = files
    return json.dumps(detailed, ensure_ascii=False, indent=2)


def normalize_tokens(raw: Any) -> dict[str, int]:
    """Normalize any framework's token report into the unified 5-field shape.

    Handles dicts and dataclass/pydantic objects with any of:
    input/prompt_tokens, output/completion_tokens, reasoning_tokens,
    cached/cache_read_prompt_tokens, total_tokens.
    """

    def coerce(value: Any) -> int | None:
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def get(*keys: str) -> int | None:
        for key in keys:
            if isinstance(raw, dict):
                value = raw.get(key)
            else:
                try:
                    value = getattr(raw, key, None)
                except Exception:  # noqa: BLE001
                    value = None
            value = coerce(value)
            if value is not None:
                return value
        return None

    input_tokens = get("input_tokens", "prompt_tokens")
    output_tokens = get("output_tokens", "completion_tokens")
    reasoning_tokens = get("reasoning_tokens") or 0
    cache_read_tokens = get(
        "cache_read_tokens", "cached_prompt_tokens", "cache_read_prompt_tokens", "cached_tokens"
    )
    total = get("total_tokens")
    if total is None and any(
        v is not None for v in (input_tokens, output_tokens, reasoning_tokens, cache_read_tokens)
    ):
        # Parent-repo rule (src/train/runner.py): when the SDK omits its own
        # total, the fallback sums all four components.
        total = (
            int(input_tokens or 0) + int(output_tokens or 0)
            + int(reasoning_tokens or 0) + int(cache_read_tokens or 0)
        )
    return {
        "input_tokens": int(input_tokens or 0),
        "output_tokens": int(output_tokens or 0),
        "reasoning_tokens": int(reasoning_tokens or 0),
        "cache_read_tokens": int(cache_read_tokens or 0),
        "total_tokens": int(total or 0),
    }


def search_skill_cases(skill_reader: Any, query: str) -> str:
    """Search every mounted skill's markdown for a case/API keyword."""

    try:
        hits = skill_reader.search_cases(query)
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {type(exc).__name__}: {exc}"
    if not hits:
        return "no matches"
    lines = []
    for hit in hits[:10]:
        path = getattr(hit, "path", "") or ""
        preview = getattr(hit, "preview", "") or ""
        skill = getattr(hit, "skill_id", "") or ""
        lines.append(f"[{skill}] {Path(str(path)).name}: {preview[:200]}")
    return "\n".join(lines)


def list_reference_files(skill_reader: Any, skill_id: str, template_name: str) -> str:
    """List the exact files inside one reference-case template."""

    try:
        skill_dir = skill_reader._skill_dir(skill_id)
        raw_name = str(template_name or "").replace("\\", "/")
        # Accept either the documented template name or the full relative
        # ``references/templates/<name>`` path, but never let ``..`` escape
        # the mounted skill snapshot.
        prefix = "references/templates/"
        if Path(raw_name).is_absolute():
            raise ValueError("invalid template name")
        raw_name = raw_name.strip("/")
        if raw_name.startswith(prefix):
            raw_name = raw_name[len(prefix):]
        if not raw_name or ".." in Path(raw_name).parts:
            raise ValueError("invalid template name")
        templates_root = (skill_dir / "references" / "templates").resolve()
        template_dir = (templates_root / raw_name).resolve()
        template_dir.relative_to(templates_root)
        if not template_dir.is_dir():
            return f"TOOL_ERROR: FileNotFoundError: {template_name}"
        files = sorted(
            p.relative_to(template_dir).as_posix()
            for p in template_dir.rglob("*")
            if p.is_file()
        )
    except Exception as exc:  # noqa: BLE001
        return f"TOOL_ERROR: {type(exc).__name__}: {exc}"
    return json.dumps(files, ensure_ascii=False)


def read_candidate_file(candidate_root: Path, relative_path: str) -> str:
    """Read a file from the candidate project (agent-written output)."""

    try:
        root = Path(candidate_root).expanduser().resolve()
        raw_path = str(relative_path or "")
        resolved = (root / raw_path).resolve()
        if not resolved.is_relative_to(root):
            return "TOOL_ERROR: path escapes candidate workspace"
        if not resolved.is_file():
            return f"TOOL_ERROR: FileNotFoundError: {relative_path}"
        return resolved.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001 - recoverable model tool failure
        return f"TOOL_ERROR: {type(exc).__name__}: {exc}"


def check_project_completeness(candidate_root: Path) -> str:
    """Report which canonical project files still need to be written."""

    from common.modeling_pipeline import CANONICAL_PROJECT_FILES

    root = Path(candidate_root)
    expected = ["py/项目画像.md"] + [f"py/prep/{name}" for name in CANONICAL_PROJECT_FILES[1:]]
    missing = [rel for rel in expected if not (root / rel).is_file()]
    present_count = len(expected) - len(missing)
    result = {
        "present": present_count,
        "total": len(expected),
        "complete": not missing,
        "missing": missing,
    }
    return json.dumps(result, ensure_ascii=False, indent=2)
