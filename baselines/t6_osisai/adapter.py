"""T6 OSIS-AI native adapter (runs in-process in .venvs/main).

Design: an ISOLATED sandbox that is a byte-faithful clone of the production
OSIS-AI environment, differing only by four declared protocol pins.

Production-faithful (cloned verbatim from the packaged install):
- opencode.exe, MinGit sh.exe and the OSIS venv on PATH (same binaries)
- ``.agents/AGENTS.md`` (byte-identical) registered as an OpenCode instruction
- ``.agents/.env`` (Weknora / feedback MCP credentials) and the global
  ``.config/opencode/{skills,plugins,commands}`` layer
- launch sequence, env vars, ports and ``permission {"*": "allow"}``
- driving stack: the parent repo's ``opencode_client`` (OpencodeClient,
  QuestionMonitor, SSE event metrics) — the same stack the train runner uses
- task hand-off: the production one-liner (``帮我建桥:<x>`` / ``<x>``); AGENTS.md
  owns skill routing, L0/L1/L2 validation and completion gates.  No benchmark
  scaffolding, no task-specific API hints.

Protocol pins (declared deviations, identical for T1--T6):
- model pinned: comparison/<model> @ same base_url, temperature 0,
  with the run-scoped output limit (set by ``run_dataset.py``)
- knowledge: the train-all skills snapshot (NEVER the raw ``.agents/skills``,
  which contains the test answers)
- budget: at most one hour for generation inside the shared 90-minute task
  budget
- gates + scoring: unchanged (run_dataset handles leakage gate, PyOSIS gate,
  reference scoring, official evaluation)

Output: runs OpenCode with ``workspace/candidate_project`` as its native
project cwd, leaves only the canonical ``py/`` source there, and writes a
``t6_generation.json`` with the same contract as the other architectures.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from queue import Empty, Queue
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.paths import resolve_opencode_dir, resolve_parent_repo  # noqa: E402
from common.protocol import FORMAL_MODEL_ID  # noqa: E402

OPENCODE_DIR = resolve_opencode_dir()
OPENCODE_EXE = OPENCODE_DIR / "opencode.exe"
# Port is configurable so two T6 runs can never collide on one host.
T6_PORT = int(os.environ.get("T6_AI_PORT", "4097"))
T6_BASE = f"http://127.0.0.1:{T6_PORT}"
DEFAULT_MAX_TOKENS = 65536
DEFAULT_MAX_STEPS = 200


def resolve_max_steps(value: Any, *, default: int) -> int:
    """Map the protocol's ``0``/unset limit to "no step limit".

    Every architecture is measured on what it can build inside the wall-clock
    budget, not on how verbose a single response happened to be.  OpenCode
    needs a concrete integer, so unbounded is expressed as a value far above
    anything reachable in the one-hour generation gate.
    """

    try:
        steps = int(value)
    except (TypeError, ValueError):
        return default
    return steps if steps > 0 else 100_000


def resolve_max_tokens(value: Any) -> int | None:
    """Return the output cap, or ``None`` meaning "omit it" (no limit)."""

    try:
        tokens = int(value)
    except (TypeError, ValueError):
        return None
    return tokens if tokens > 0 else None
_CANONICAL_PROJECT_FILES = (
    "py/项目画像.md",
    "py/prep/main.py",
    "py/prep/_0_engine.py",
    "py/prep/_1_control.py",
    "py/prep/_2_property.py",
    "py/prep/_3_material.py",
    "py/prep/_4_section.py",
    "py/prep/_5_node.py",
    "py/prep/_6_element.py",
    "py/prep/_7_boundary.py",
    "py/prep/_8_loadcase.py",
    "py/prep/_9_analysis.py",
    "py/prep/_10_stage.py",
)
_NATIVE_ARTIFACT_NAMES = {
    # Names emitted by the OSIS project backend (observed in the previous
    # native-project run).  They are diagnostics/meshes, never candidate
    # source, so remove them before handing the project to the common runner.
    "project.sis", "Break", "Check", "Error", "Model", "MutiCase",
    "Result", "Temperary", "image", "secmesh", "_logfile.log",
}



##tokens
def _clone_production_config(iso_dir: Path, agents: Path, xdg_config_home: Path) -> dict[str, Any]:
    """Byte-clone the production OSIS-AI config layers into the sandbox.

    The comparison protocol pins four variables (skill snapshot, model,
    temperature, output cap); everything else must be what production runs
    with.  Copied verbatim from the packaged install (``OPENCODE_DIR``):

    - ``.agents/.env`` — third-party credentials (Weknora lookup, feedback
      MCP) that AGENTS.md's rules reference.  Values are never logged.
    - ``.config/opencode/skills`` — the global skill layer (e.g.
      ``karpathy-guidelines``) that production sessions always see.
    - ``.config/opencode/plugins`` and ``commands`` — global plugin layer.

    The frozen model pin and the AGENTS.md instruction stay in the generated
    project ``opencode.json``; the train-all skill snapshot stays under
    ``.agents/skills`` (test templates excluded by the leakage protocol).
    """

    audit: dict[str, Any] = {}

    prod_env = OPENCODE_DIR / ".agents" / ".env"
    if prod_env.is_file():
        shutil.copy2(prod_env, agents / ".env")
        audit["env_file"] = True
    else:
        audit["env_file"] = False

    prod_global = OPENCODE_DIR / ".config" / "opencode"
    for name in ("plugins", "commands"):
        source = prod_global / name
        target = xdg_config_home / "opencode" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if source.is_dir():
            shutil.copytree(source, target, dirs_exist_ok=True)
            audit[f"global_{name}"] = sorted(child.name for child in source.iterdir())
        else:
            audit[f"global_{name}"] = []
    # Global SKILLS are intentionally NOT cloned here: the train-all snapshot
    # now includes the production global skill layer, so every architecture
    # receives it through the same mounted bundle.  Cloning it again via
    # xdg-config would list the same skills twice in this sandbox.
    prod_skills = prod_global / "skills"
    audit["global_skills_in_snapshot"] = (
        sorted(child.name for child in prod_skills.iterdir())
        if prod_skills.is_dir()
        else []
    )
    return audit


def _opencode_event_metrics(events: Any) -> dict[str, Any]:
    """Summarize model/tool/token counters from the OpenCode SSE stream."""

    def _count(value: Any) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    model_calls = 0
    tool_ids: set[str] = set()
    framework_steps = 0
    stop_reason: str | None = None
    totals = {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "total_tokens": 0,
    }
    saw_tokens = False
    for inner in list(events or []):
        if not isinstance(inner, dict):
            continue
        if inner.get("type") != "message.part.updated":
            continue
        properties = inner.get("properties") or {}
        part = properties.get("part") if isinstance(properties, dict) else None
        if not isinstance(part, dict):
            continue
        framework_steps += 1
        part_type = str(part.get("type") or "")
        if part_type == "tool":
            state = part.get("state") or {}
            call_id = (
                part.get("id")
                or part.get("callID")
                or state.get("callID")
                or state.get("call_id")
            )
            # Updated events repeat the same part; count one logical call.
            key = str(call_id) if call_id else json.dumps(part, ensure_ascii=False, sort_keys=True)
            if key not in tool_ids:
                tool_ids.add(key)
        elif part_type == "step-finish":
            model_calls += 1
            stop_reason = str(part.get("reason") or part.get("finish") or "") or stop_reason
            raw_tokens = part.get("tokens") or {}
            cache = raw_tokens.get("cache") if isinstance(raw_tokens, dict) else {}
            if isinstance(raw_tokens, dict):
                saw_tokens = True
                totals["input_tokens"] += _count(raw_tokens.get("input"))
                totals["output_tokens"] += _count(raw_tokens.get("output"))
                totals["reasoning_tokens"] += _count(raw_tokens.get("reasoning"))
                totals["total_tokens"] += _count(raw_tokens.get("total"))
                if isinstance(cache, dict):
                    totals["cache_read_tokens"] += _count(cache.get("read"))
    result: dict[str, Any] = {
        "framework_steps": framework_steps,
        "model_calls": model_calls,
        "tool_calls": len(tool_ids),
    }
    if saw_tokens:
        # Parent-repo rule (src/train/runner.py): the framework's own ``total``
        # is authoritative; when it is absent, the fallback sums all four
        # components (input + output + reasoning + cache_read).  Keeping this
        # identical to the production train runner matters more than making
        # the cross-architecture token basis uniform — the six adapters each
        # report their SDK's own total, exactly as they do in production.
        if totals["total_tokens"] <= 0:
            totals["total_tokens"] = (
                totals["input_tokens"] + totals["output_tokens"]
                + totals["reasoning_tokens"] + totals["cache_read_tokens"]
            )
        result["tokens"] = totals
    if stop_reason:
        result["stop_reason"] = stop_reason
    return result


def _sysexit(msg: str) -> None:
    raise RuntimeError(msg)


def _build_t6_prompt(request: dict[str, Any]) -> str:
    """Production-style task hand-off.

    The parent repo's train runner drives OSIS-AI with a single line —
    ``帮我建桥:<x>`` for whole projects, ``<x>`` verbatim for gen/edit — and
    lets AGENTS.md own everything else (skill routing, L0/L1/L2 validation,
    completion gates).  T6 must receive the same hand-off: extra scaffolding
    here overrides the native workflow, and task-specific API hints would leak
    per-case answers into the prompt.  The only addition is one sandbox fact
    production does not need: where the OSIS project root is (the desktop app
    normally shows it).

    Only the sanitized task object is embedded.  Standard-answer paths, raw
    skill roots and scorer-private files remain outside the model input.
    """

    task = request.get("task") or {}
    if not isinstance(task, dict):
        task = {}
    requirement = str(task.get("natural_language_requirement") or "").strip()
    if str(task.get("task_form") or "whole").strip().lower() == "whole":
        prompt = f"帮我建桥:{requirement}" if requirement else requirement
    else:
        prompt = requirement
    return (
        "你是 OSIS-AI，请按 AGENTS.md 的原生工作流完成本任务。"
        "当前工作目录就是本次任务的 OSIS 工程根目录。\n\n"
        f"{prompt}"
    )


##注入api配置
def _prepare_isolated_env(
    iso_dir: Path,
    skills_src: Path,
    base_url: str,
    *,
    model: str = FORMAL_MODEL_ID,
    max_tokens: int = DEFAULT_MAX_TOKENS,
    temperature: float = 0.0,
) -> dict[str, str]:
    """Write the isolated opencode config (provider pin + train-all skills) and
    return the env for launching a dedicated server."""

    agents = iso_dir / ".agents"
    skills_dst = agents / "skills"
    if skills_dst.exists():
        shutil.rmtree(skills_dst)
    skills_dst.mkdir(parents=True)
    for child in skills_src.iterdir():
        target = skills_dst / child.name
        if child.is_dir():
            shutil.copytree(child, target, dirs_exist_ok=True)
        else:
            shutil.copy2(child, target)

    # OpenCode discovers AGENTS.md relative to its configured project.  The
    # packaged file is part of the native T6 framework contract, separate from
    # the domain SKILL snapshot, so copy it into the isolated config tree and
    # fail closed when the installation is incomplete.
    packaged_agents = OPENCODE_DIR / ".agents" / "AGENTS.md"
    if not packaged_agents.is_file():
        raise FileNotFoundError(
            f"T6 packaged agent instructions are missing: {packaged_agents}"
        )
    mounted_agents = agents / "AGENTS.md"
    shutil.copy2(packaged_agents, mounted_agents)
    agents_mount = {
        "source": str(packaged_agents),
        "mounted": str(mounted_agents),
        "sha256": hashlib.sha256(mounted_agents.read_bytes()).hexdigest(),
        "read_only": True,
    }
    (iso_dir / "agents_mount.json").write_text(
        json.dumps(agents_mount, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )

    model_name = {
        "mimo-v2.5": "fast",
        "glm-5.3-flash": "GLM 5.3 Flash",
        "MiniMax-M3": "MiniMax M3",
        "deepseek-v4.1-flash-expires-on-0910": "DeepSeek V4.1 Flash",
    }.get(model, model)
    opencode_json = {
        "$schema": "https://opencode.ai/config.json",
        "disabled_providers": [],
        # OpenCode does not automatically treat a file next to
        # OPENCODE_CONFIG_DIR as an instruction.  Pin the mounted file
        # explicitly; the old adapter copied AGENTS.md but never activated it.
        "instructions": [str(mounted_agents.resolve())],
        "provider": {
            "comparison": {
                "name": "OSIS Manual API",
                "npm": "@ai-sdk/openai-compatible",
                "options": {
                    "baseURL": base_url,
                    "apiKey": "{env:OSIS_MODEL_API_KEY}",
                },
                "models": {
                    model: {
                        "name": model_name,
                        "reasoning": True,
                        "modalities": {
                            "input": ["text", "image"],
                            "output": ["text"],
                        },
                        "options": {"temperature": temperature},
                        # Match the normal OSIS-AI context window.  The
                        # output cap remains the frozen comparison cap passed
                        # by run_dataset.py, so all six architectures retain
                        # the same generation budget.
                        # OpenCode's schema requires an integer output limit,
                        # so an unlimited run omits the field entirely rather
                        # than sending null.  Wall-clock is the termination
                        # mechanism in this protocol.
                        "limit": {"context": 256000, **({"output": max_tokens} if max_tokens else {})},
                    }
                },
            }
        },
        "model": f"comparison/{model}",
        "default_agent": "build",
        "permission": {"*": "allow"},
    }
    (agents / "opencode.json").write_text(json.dumps(opencode_json, ensure_ascii=False), encoding="utf-8")

    env = dict(os.environ)
    env["OPENCODE_CONFIG_DIR"] = str(agents)
    env["XDG_DATA_HOME"] = str(iso_dir / "xdg-data")
    env["XDG_CONFIG_HOME"] = str(iso_dir / "xdg-config")
    env["XDG_STATE_HOME"] = str(iso_dir / "xdg-state")
    env["XDG_CACHE_HOME"] = str(iso_dir / "xdg-cache")
    # Mirror the packaged OpenCode launcher.  ``Python311`` is a sibling of
    # the ``opencode`` install directory (not ``opencode/Python311``); the
    # previous path silently pointed at a non-existent directory and could
    # break Python-backed bash/MCP tools in the isolated session.  Prefer the
    # bundled OSIS venv on PATH and leave PYTHONHOME unset so child Python
    # processes use their normal Windows interpreter discovery.
    env.pop("PYTHONHOME", None)
    user_root = Path(os.environ["USERPROFILE"]) if os.environ.get("USERPROFILE") else Path.home()
    osis_venv = user_root / ".osisai" / ".venv"
    path_parts = []
    if (osis_venv / "Scripts").is_dir():
        path_parts.append(str(osis_venv / "Scripts"))
        env["OSIS_VENV"] = str(osis_venv)
    for part in (
        OPENCODE_DIR / ".local" / "share" / "opencode" / "bin",
        OPENCODE_DIR / "mingit" / "cmd",
        OPENCODE_DIR / "mingit" / "usr" / "bin",
    ):
        if part.is_dir():
            path_parts.append(str(part))
    path_parts.append(env.get("PATH", ""))
    env["PATH"] = os.pathsep.join(item for item in path_parts if item)
    shell = OPENCODE_DIR / "mingit" / "usr" / "bin" / "sh.exe"
    if shell.is_file():
        env["SHELL"] = str(shell)
    env["PYTHONIOENCODING"] = "utf-8"
    env["OPENCODE_DISABLE_AUTOUPDATE"] = "1"
    env["OPENCODE_DISABLE_LSP_DOWNLOAD"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE_PROMPT"] = "1"
    env["OPENCODE_DISABLE_CLAUDE_CODE_SKILLS"] = "1"
    # The packaged launcher exports both ports.  The native OSIS tools use the
    # HTTP port while OpenCode itself listens on the AI port; omitting these
    # variables makes a normal AGENTS workflow connect to a stale desktop
    # session instead of this run's isolated service.
    env["OSIS_HTTP_PORT"] = os.environ.get("OSIS_HTTP_PORT", "18080")
    env["OSIS_AI_PORT"] = str(T6_PORT)
    # Skill bodies invoke their own scripts through this variable, e.g.
    # ``python "%OSIS_EXTRA_CONFIG_DIR%\skills\osis-engine\scripts\seedtpl.py"``.
    # Production sets it to the deployed skill directory.  Leaving it UNSET in
    # the sandbox made that command fail, and the agent then reconstructed the
    # authoring repo's path from the sandbox's own path prefix
    # (…\osis-skill-enhance-main\osis-framework-comparison\runs\…\.osisai_t6\…)
    # in order to find seedtpl.py — landing in the repo's raw
    # ``.agents\skills`` tree, which still holds every test template (= the
    # answers).  Point it at THIS run's mounted snapshot instead, so the
    # documented command works and there is no reason to look outside.
    env["OSIS_EXTRA_CONFIG_DIR"] = str(agents)
    # Byte-clone the remaining production config layers (.env credentials,
    # global skills/plugins/commands) so the sandbox differs from production
    # only by the four declared protocol pins.  The audit record is written
    # next to agents_mount.json; credential values are never copied into it.
    audit = _clone_production_config(iso_dir, agents, iso_dir / "xdg-config")
    (iso_dir / "production_clone.json").write_text(
        json.dumps(audit, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return env


def _project_py_root(project_root: Path) -> Path:
    """Return the native project's code root (`py/` or a direct `prep/`)."""

    root = Path(project_root).expanduser().resolve()
    if (root / "py").is_dir():
        return root / "py"
    if (root / "prep").is_dir():
        return root
    return root / "py"


def _copy_project_py(source_root: Path, destination_root: Path) -> list[str]:
    """Copy only a project's `py` tree and return copied relative paths."""

    source_root = Path(source_root).expanduser().resolve()
    destination_root = Path(destination_root).expanduser().resolve()
    source_py = _project_py_root(source_root)
    if not source_py.is_dir():
        return []
    destination_py = destination_root / "py"
    if source_py == destination_py.resolve():
        return sorted(
            path.relative_to(destination_root).as_posix()
            for path in destination_py.rglob("*")
            if path.is_file()
        )
    copied: list[str] = []
    for path in sorted(source_py.rglob("*"), key=lambda item: item.as_posix().lower()):
        relative = path.relative_to(source_py)
        target = destination_py / relative
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if not path.is_file():
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        copied.append((Path("py") / relative).as_posix())
    return copied


def _sync_native_project(native_project: Path, candidate_root: Path) -> list[str]:
    """Materialize native OSIS output into the comparison candidate contract."""

    return _copy_project_py(native_project, candidate_root)


def _candidate_files(candidate_root: Path) -> list[str]:
    """List only source artifacts under the canonical candidate ``py`` tree."""

    root = Path(candidate_root).expanduser().resolve()
    py_root = _project_py_root(root)
    if not py_root.is_dir():
        return []
    return sorted(
        path.relative_to(root).as_posix()
        for path in py_root.rglob("*")
        if path.is_file()
    )


def _cleanup_native_artifacts(candidate_root: Path) -> list[str]:
    """Remove OSIS-generated metadata from the framework output root."""

    root = Path(candidate_root).expanduser().resolve()
    removed: list[str] = []
    for name in sorted(_NATIVE_ARTIFACT_NAMES):
        path = root / name
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
            if not path.exists():
                removed.append(name)
        elif path.is_file():
            try:
                path.unlink()
            except OSError:
                continue
            if not path.exists():
                removed.append(name)
    # Some OSIS builds emit mesh/log files directly at the project root rather
    # than below ``secmesh``.  Remove only the known native artifact suffixes;
    # the canonical ``py`` tree and any user-readable markdown remain intact.
    candidates = (
        sorted(root.iterdir(), key=lambda item: item.name.lower())
        if root.is_dir()
        else ()
    )
    for path in candidates:
        if path.name == "py" or path.name in _NATIVE_ARTIFACT_NAMES:
            continue
        if path.is_file() and path.suffix.lower() in {".vtk", ".log", ".err", ".out"}:
            try:
                path.unlink()
            except OSError:
                continue
            if not path.exists():
                removed.append(path.name)
    # ProjectManager derives the native project directory from the sibling
    # ``<project>.sis`` path.  The sibling is run-owned metadata, not a model
    # source artifact, and must not remain beside the canonical candidate.
    project_file = root.with_suffix(".sis")
    if project_file.is_file():
        try:
            project_file.unlink()
        except OSError:
            pass
        if not project_file.exists():
            removed.append(project_file.name)
    return removed


def _ensure_osis_backend() -> str:
    """Guarantee an OSIS engine backend and return which mode is in use.

    Production OSIS-AI talks to the OSIS desktop backend on ``OSIS_HTTP_PORT``
    (18080).  For unattended experiment runs the desktop cannot be assumed, so
    when the port is unreachable this starts a headless engine from
    ``PySolver.dll`` (``pyosis.core.solver.OSISSolver``) on a side port and
    pins ``OSIS_HTTP_PORT`` to it for the rest of the run.  Both modes expose
    the same ``/OSIS_Run`` dispatch surface; the active mode is recorded in
    the run metadata for the parity appendix.
    """

    import httpx

    port = os.environ.get("OSIS_HTTP_PORT", "18080")
    probe = f"http://127.0.0.1:{port}/OSIS_Run"
    try:
        httpx.get(probe, timeout=5.0)
        return "desktop-18080" if port == "18080" else f"external-{port}"
    except Exception:  # noqa: BLE001 - anything unreachable means "not serving"
        pass

    from pyosis.core.solver import OSISSolver

    solver_port = os.environ.get("OSIS_SOLVER_PORT", "18081")
    install_root = OPENCODE_DIR.parent  # packaged OSIS install (…/Rbin64(1))
    candidates = []
    if os.environ.get("OSIS_SOLVER_INSTALL"):
        candidates.append(Path(os.environ["OSIS_SOLVER_INSTALL"]))
    candidates.append(install_root)
    # PySolver.dll is a separately compiled component and the current install
    # may not ship it; sibling installs from the same deployment often do.
    stem = re.sub(r"\(\d+\)$", "", install_root.name)
    for sibling in sorted(install_root.parent.glob(f"{stem}*")):
        if sibling != install_root:
            candidates.append(sibling)
    seen: set[Path] = set()
    last_error: str | None = None
    for base in candidates:
        base = Path(base)
        if base in seen or not (base / "PySolver.dll").is_file():
            continue
        seen.add(base)
        # PySolver.dll resolves its engine dependencies from its own install
        # directory; make that directory visible before ctypes loads it.  A
        # DLL dropped into an install that lacks its dependency set (e.g. the
        # current packaged install) fails to load — fall through to the next
        # candidate instead of aborting the run.
        try:
            os.add_dll_directory(str(base))
            os.environ["PATH"] = str(base) + os.pathsep + os.environ.get("PATH", "")
            solver = OSISSolver(str(base), port=int(solver_port))
        except Exception as exc:  # noqa: BLE001 - try the next install
            last_error = f"{base.name}: {exc}"
            continue
        os.environ["OSIS_HTTP_PORT"] = str(solver.port)
        return f"headless-pysolver:{base.name}:{solver.port}"
    raise RuntimeError(
        "No OSIS engine backend available: the desktop backend is down and no "
        f"candidate install could load PySolver.dll ({last_error}). Start "
        "Osis.exe or point OSIS_SOLVER_INSTALL at an install that ships a "
        "loadable PySolver.dll."
    )


def _create_native_project(
    iso_dir: Path, *, project_root: Path | None = None
) -> Path:
    """Create and verify a fresh OSIS project owned by this T6 run.

    The old adapter ignored creation errors and silently continued against the
    desktop's stale project.  That made the agent write somewhere unknown and
    made an empty candidate look like a framework failure.  When a canonical
    ``project_root`` is supplied, the OSIS project is created at that exact
    directory (the sibling ``<root>.sis`` path is used because OSIS derives
    its directory from the file stem).  The returned directory is checked
    before OpenCode is started.
    """

    import tempfile

    from pyosis.core.engine import OSISEngine

    iso_dir = Path(iso_dir).expanduser().resolve()
    iso_dir.mkdir(parents=True, exist_ok=True)
    staged_root: Path | None = None
    if project_root is None:
        native_parent = Path(tempfile.mkdtemp(prefix="native-project-", dir=str(iso_dir)))
        expected_root: Path | None = None
        project_file = native_parent / "project.sis"
    else:
        # OSIS derives the project directory from the *stem* of the .sis file.
        # Passing ``candidate_project/project.sis`` therefore creates
        # ``candidate_project/project``.  Put the .sis beside the requested
        # directory so the returned native directory is exactly the canonical
        # candidate root.
        expected_root = Path(project_root).expanduser().resolve()
        native_parent = expected_root.parent
        native_parent.mkdir(parents=True, exist_ok=True)
        project_file = expected_root.with_suffix(".sis")

        # The runner may already have staged trusted base files (gen/edit) in
        # the candidate root.  Project creation is allowed to require a fresh
        # directory, so temporarily move that run-owned tree aside and restore
        # it into the newly-created native project below.
        if expected_root.exists():
            staged_parent = Path(tempfile.mkdtemp(prefix="candidate-staged-", dir=str(iso_dir)))
            staged_root = staged_parent / expected_root.name
            shutil.copytree(expected_root, staged_root, dirs_exist_ok=True)
            shutil.rmtree(expected_root)
    def _restore_staged() -> None:
        if staged_root is None or expected_root is None or not staged_root.exists():
            return
        expected_root.mkdir(parents=True, exist_ok=True)
        shutil.copytree(staged_root, expected_root, dirs_exist_ok=True)

    engine = OSISEngine()
    try:
        engine.project.create(101, str(project_file))
    except Exception:
        _restore_staged()
        raise
    try:
        current = Path(engine.project.get_directory()).expanduser().resolve()
        if current.is_file():
            current = current.parent
        try:
            current.relative_to(native_parent.resolve())
        except ValueError as exc:
            raise RuntimeError(
                f"OSIS project.create did not select the isolated project: {current}"
            ) from exc
        if expected_root is not None and current != expected_root:
            raise RuntimeError(
                "OSIS project.create selected a directory different from the "
                f"candidate root: expected {expected_root}, got {current}"
            )
        if not current.is_dir():
            raise NotADirectoryError(current)
    except Exception:
        _restore_staged()
        raise
    _restore_staged()
    return current


def _wait_healthy(base: str, timeout_s: float = 90.0) -> None:
    import httpx

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            if httpx.get(f"{base}/global/health", timeout=5.0).status_code == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(2)
    _sysexit(f"T6 opencode server on {base} did not become healthy")


def _start_opencode_server(
    project_root: Path, env: dict[str, str], log_path: Path | None = None
) -> subprocess.Popen:
    """Start the isolated server with the native project as its workspace.

    Server output is captured to ``log_path`` when provided: a failed start
    used to be undiagnosable because stdout/stderr went to DEVNULL, leaving
    only "did not become healthy" with no cause.
    """

    stdout = stderr = subprocess.DEVNULL
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        handle = log_path.open("w", encoding="utf-8", errors="replace")
        stdout = stderr = handle
    process = subprocess.Popen(
        [str(OPENCODE_EXE), "serve", "--port", str(T6_PORT)],
        cwd=str(Path(project_root)),
        env=env,
        stdout=stdout,
        stderr=stderr,
    )
    if log_path is not None:
        process._benchmark_log_handle = handle  # closed by the caller's cleanup
    return process


def generate_t6(
    request: dict[str, Any],
    *,
    workspace: Path,
    candidate_root: Path,
) -> None:
    """Drive one OSIS-AI session; write candidate + t6_generation.json."""

    started = time.monotonic()
    workspace = Path(workspace)
    candidate_root.mkdir(parents=True, exist_ok=True)
    iso_dir = workspace / ".osisai_t6"
    meta: dict[str, Any] = {
        "architecture_id": "T6",
        "framework": "osis-native",
        "framework_version": "osis-native-v1",
        "model": request["model"],
        "status": "failed",
        "model_calls": 0,
        "tool_calls": 0,
        "framework_steps": 0,
        "stop_reason": None,
    }
    server: subprocess.Popen | None = None
    sid: str | None = None
    monitor = None
    event_records: list[dict[str, Any]] = []
    try:
        skills_src = Path(request["skills_dir"])
        meta["skill_loading"] = "isolated_train_all"
        meta["native_skill_count"] = sum(
            1 for child in skills_src.iterdir() if child.is_dir()
        ) if skills_src.is_dir() else 0
        # Resolve the OSIS engine backend FIRST: the sandbox env inherits
        # OSIS_HTTP_PORT, so a headless fallback must be pinned before the
        # isolated environment is built.
        meta["osis_backend"] = _ensure_osis_backend()
        env = _prepare_isolated_env(
            iso_dir,
            skills_src,
            request["base_url"],
            model=request["model"],
            max_tokens=resolve_max_tokens(request.get("max_tokens")),
            temperature=float(request.get("temperature", 0.0)),
        )
        # Keep an auditable record in the framework metadata as well as the
        # isolated config directory.  This proves which packaged AGENTS.md
        # was mounted without ever recording the model API key.
        agents_mount_path = iso_dir / "agents_mount.json"
        try:
            mounted_info = json.loads(agents_mount_path.read_text(encoding="utf-8"))
            if isinstance(mounted_info, dict):
                meta["agents_mount"] = mounted_info
        except (OSError, json.JSONDecodeError):
            meta.setdefault("agents_mount_error", "agents_mount.json unavailable")
        try:
            meta["production_clone"] = json.loads(
                (iso_dir / "production_clone.json").read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            meta.setdefault("production_clone_error", "production_clone.json unavailable")
        env["OSIS_MODEL_API_KEY"] = request.get("api_key") or os.environ.get("OSIS_MODEL_API_KEY", "")

        # Create the isolated native project *before* starting OpenCode and
        # use it as the server's working directory.  Starting OpenCode in the
        # comparison candidate directory while creating a separate PyOSIS
        # project made the prompt's "current project" claim false; the final
        # sync could then overwrite files written by the agent with the stale
        # native tree.  Both the agent and the synchronizer now target the
        # same per-run native project.
        # Make the canonical candidate root the native OSIS project itself.
        # This removes the old hidden ``native-project-*`` indirection: the
        # OpenCode session, PyOSIS project and comparison runner now all see
        # exactly the same ``generated/candidate_project`` directory.
        native_project = _create_native_project(
            iso_dir, project_root=candidate_root
        )
        meta["native_project_dir"] = str(native_project)
        meta["native_project_file"] = str(native_project.with_suffix(".sis"))
        server = _start_opencode_server(
            native_project, env, log_path=workspace / "opencode_server.log"
        )
        _wait_healthy(T6_BASE, timeout_s=float(request.get("request_timeout_s", 90.0)))

        sys.path.insert(0, str(resolve_parent_repo(request.get("parent_repo")) / "src"))
        from opencode_client import DEFAULT_MODEL, OpencodeClient, QuestionMonitor  # noqa: E402
        from opencode_client import events as ev  # noqa: E402

        client = OpencodeClient(base_url=T6_BASE)
        sid = client.create_session(f"T6 {request['task'].get('task_id', '')}")
        # The task hand-off activates the packaged normal OSIS-AI workflow;
        # unlike T2--T5, T6 must not be reduced to a generic write-file loop.
        prompt = _build_t6_prompt(request)
        max_steps = resolve_max_steps(
            request.get("max_steps"), default=DEFAULT_MAX_STEPS)
        generation_timeout = float(
            request.get("generation_timeout_s")
            or request.get("request_timeout_s", 3600.0)
        )
        monitor = QuestionMonitor(client, sid, strategy="first", poll_interval=1.0)
        monitor.start()
        # Keep the exact hand-off text in the run metadata: the paper appendix
        # audits prompt parity between the sandbox and the parent repo's
        # production train runner.
        meta["prompt"] = prompt
        meta["session_id"] = sid

        chat_errors: list[BaseException] = []

        def _chat() -> None:
            try:
                client.chat(
                    sid, prompt, provider_id="comparison", model_id=request["model"],
                    timeout=generation_timeout,
                )
            except BaseException as exc:  # noqa: BLE001 - report to outer run
                chat_errors.append(exc)

        chat_thread = threading.Thread(target=_chat, daemon=True)
        chat_thread.start()

        # ``connect_events`` is a blocking SSE iterator.  Running it directly
        # in this thread meant an HTTP 402/429 from ``client.chat`` could sit
        # here until the full 30-minute read timeout elapsed, even though the
        # chat worker had already reported the real failure.  Keep the stream
        # reader in a daemon and poll a queue so transport errors terminate the
        # generation promptly while still retaining every event received so
        # far.
        event_queue: Queue[Any] = Queue()
        event_errors: list[BaseException] = []

        def _collect_events() -> None:
            try:
                for event in ev.connect_events(
                    client.base_url, timeout=generation_timeout
                ):
                    event_queue.put(event)
            except BaseException as exc:  # noqa: BLE001 - surfaced below
                event_errors.append(exc)

        event_thread = threading.Thread(target=_collect_events, daemon=True)
        event_thread.start()

        idle = False
        step_count = 0
        event_deadline = time.monotonic() + generation_timeout
        while True:
            if chat_errors and event_queue.empty():
                break
            if not event_thread.is_alive() and event_queue.empty():
                break
            remaining_events = event_deadline - time.monotonic()
            if remaining_events <= 0:
                meta.setdefault("error", "OpenCode event stream timed out")
                break
            try:
                raw = event_queue.get(timeout=min(0.5, remaining_events))
            except Empty:
                continue
            inner = ev._unwrap(raw)
            if isinstance(inner, dict):
                event_records.append(inner)
            etype = inner.get("type") if isinstance(inner, dict) else ""
            if etype == "message.part.updated":
                step_count += 1
            if etype == "session.idle":
                idle = True
                break
            if etype in {"session.error", "session.compiled"}:
                meta["error"] = f"opencode session aborted: {etype}"
                break
            if max_steps and step_count > max_steps * 3:
                # Only a finite (non-zero) step budget can trip this; an
                # unlimited run is bounded by the wall-clock deadline above.
                meta["error"] = f"opencode session exceeded ~{max_steps} steps ({step_count} parts)"
                break
        chat_thread.join(timeout=5)
        # A healthy event stream may still be blocked after session.idle or an
        # early chat failure; it is a daemon and will be released with the
        # isolated server during final cleanup.
        event_thread.join(timeout=0.2)

        # NO automated nudging: the protocol is one input, zero evaluator
        # intervention.  If the agent ends its turn planning-only (AGENTS.md
        # prescribes 规划先行), that is a legitimate measured outcome of the
        # native workflow under a single-message hand-off — the same outcome
        # any architecture could produce.  Automated "continue" prompts would
        # be evaluator feedback no T1-T5 run receives.

        if monitor is not None:
            monitor.stop()
            monitor = None

        if chat_errors:
            error = chat_errors[0]
            meta["error_type"] = type(error).__name__
            meta["error"] = f"OpenCode chat failed: {error}"[:800]
        elif event_errors:
            error = event_errors[0]
            meta["error_type"] = type(error).__name__
            meta.setdefault("error", f"OpenCode event stream failed: {error}"[:800])

        meta.update(_opencode_event_metrics(event_records))
        if chat_errors:
            meta["stop_reason"] = "error"

        _sync_native_project(native_project, candidate_root)
        files = _candidate_files(candidate_root)
        meta["files_written"] = files
        meta["status"] = "completed" if (idle and files and not chat_errors) else "failed"
        if meta["status"] != "completed":
            meta.setdefault("error", meta.get("error") or "session idle but no candidate files written")
            meta["stop_reason"] = meta.get("stop_reason") or "error"
        else:
            meta["stop_reason"] = meta.get("stop_reason") or "completed"
        meta["session_idle"] = idle
    except Exception as exc:  # noqa: BLE001
        meta.update(_opencode_event_metrics(event_records))
        meta["error_type"] = type(exc).__name__
        meta["error"] = str(exc)[:500]
        meta["stop_reason"] = "error"
    finally:
        if monitor is not None:
            try:
                monitor.stop()
            except Exception:  # noqa: BLE001
                pass
        if event_records:
            final_metrics = _opencode_event_metrics(event_records)
            for key, value in final_metrics.items():
                if key == "tokens" and "tokens" in meta:
                    continue
                if key == "stop_reason" and meta.get("stop_reason"):
                    continue
                if key in {"model_calls", "tool_calls", "framework_steps"} and meta.get(key):
                    continue
                meta[key] = value
        if "native_project" in locals():
            try:
                copied = _sync_native_project(native_project, candidate_root)
                meta["native_files_synced"] = copied
                meta["files_written"] = _candidate_files(candidate_root)
            except Exception as sync_exc:  # noqa: BLE001
                meta.setdefault("sync_error", f"{type(sync_exc).__name__}: {sync_exc}")
        if sid is not None:
            # Session data is intentionally KEPT: the isolated opencode.db and
            # log are the audit trail for what the model read, wrote and ran.
            # (The previous adapter deleted the session here, which destroyed
            # the only evidence of in-session verification behavior.)
            meta["session_db"] = str(iso_dir / "xdg-data" / "opencode" / "opencode.db")
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
        # Native OSIS commands may leave project metadata, meshes and logs in
        # the project root.  They are not candidate source and would make T6
        # differ from the other adapters' ``py``-only output contract.
        if "native_project" in locals():
            try:
                removed = _cleanup_native_artifacts(candidate_root)
                if removed:
                    meta["native_artifacts_cleaned"] = removed
                meta["files_written"] = _candidate_files(candidate_root)
            except Exception as cleanup_exc:  # noqa: BLE001
                meta.setdefault(
                    "cleanup_error", f"{type(cleanup_exc).__name__}: {cleanup_exc}"
                )
        meta["elapsed_s"] = time.monotonic() - started
        (workspace / "t6_generation.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def main() -> int:
    """CLI entry: baseline.<pkg>.adapter --request <path> (parent-venv subprocess).

    Runs inside the PARENT repo's python (which has the ``opencode_ai`` SDK),
    mirroring the T3/T4/T5 framework-venv subprocess pattern in run_dataset.py.
    Prints the generation metadata as the final stdout line so the external
    driver (``_framework_generation``) can parse status/files_written.
    """

    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--request", required=True)
    args = parser.parse_args()
    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    workspace = Path(request["workspace"])
    generate_t6(
        request,
        workspace=workspace,
        candidate_root=workspace / "candidate_project",
    )
    meta = json.loads((workspace / "t6_generation.json").read_text(encoding="utf-8"))
    print(json.dumps(meta, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
