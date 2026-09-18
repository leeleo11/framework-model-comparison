"""Run one dataset task on ANY architecture (official unified entry).

One command, six architectures::

    python scripts/run_dataset.py --architecture T1 --bridge osis-bridge-cantilever-box \
        --form full --index 0 --seed 0

Shared, frozen experiment protocol (identical for every architecture):
- sanitized task (only the dataset ``x`` reaches the model);
- leakage gate before generation (marks leakage_guard_failed and blocks);
- leak-free skills snapshot mount (train-all for official runs);
- gen/edit base_files staged into the candidate workspace;
- PyOSIS execution + CLI scoring by the unified ExperimentRunner;
- frozen_config.json, scorer_private/, leakage_guard.json artifacts.

Correctness guarantees (per the 2026-09-04 audit):
- Status propagation: a framework that writes no candidate files makes the run
  ``generation_failed``. If it fails / sticks / times out after writing files,
  the partial source is preserved for applicable dimension scoring.
- Hard timeout: the framework subprocess is killed at the run's 90-minute
  task budget by default (historical 1800s/3600s runs remain unchanged).
- Unified retry: only infrastructure failures (transport / HTTP 429 / timeout)
  are retried with backoff, up to ``--max-attempts``; model-behavior failures
  are never retried and consume no extra runs.
- PyOSIS execution is ON by default (official runs must actually model);
  ``--no-pyosis`` exists only for diagnostics.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path
from urllib.parse import urlparse

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.dataset import (  # noqa: E402
    DatasetEntry,
    load_dataset_entry,
    prestage_base_files,
    reference_record,
    to_task_spec,
)
from common.leakage_guard import run_guard  # noqa: E402
from common.manifest import artifact_hashes  # noqa: E402
from common.protocol import FORMAL_MODEL_ID  # noqa: E402
from common.paths import (  # noqa: E402
    resolve_args_parent_repo,
    resolve_parent_repo,
    resolve_run_root,
    resolve_skills_dir,
)
from common.runner import ExperimentRunner  # noqa: E402
from common.skill_adapter import SkillAdapter  # noqa: E402
from baselines._framework_common import (  # noqa: E402
    knowledge_search_enabled,
    normalize_tokens,
)

# Only TIME terminates a run.  The protocol has no token budget and no step
# budget; the generation-stage wall-clock gate is the SOLE termination
# mechanism.
#
# History, twice corrected.  (1) A frozen ``max_tokens=65536`` per request was
# described as inert — false, because T1 is architecturally ONE request, so
# its per-request cap WAS its total output budget while tool-using arms could
# spend it 200 times.  (2) Setting the field to 0/omitting it — also false:
# the gateway then applies ITS default cap, which is exactly 65,536
# completion tokens.  T1's real prompt needs ~67k (reasoning-heavy), so it
# was cut mid-string again, at the same number.  The fix is to send an
# explicit, effectively-unbounded cap: the gateway accepts it, the endpoint's
# own model limit still applies equally to every architecture, and
# wall-clock remains the only gate this protocol imposes.
FROZEN_MAX_TOKENS = 200_000
FROZEN_MODEL_TIMEOUT_S = 900.0
FROZEN_GENERATION_STEP_TIMEOUT_S = 3600.0
FROZEN_POST_GENERATION_RESERVE_S = 1800.0
# Generation is capped at one hour; the formal 90-minute task budget leaves
# 30 minutes for unified PyOSIS execution and source scoring. For an explicit
# shorter override, the generation cap is reduced to preserve that reserve.
# No step cap either: see FROZEN_MAX_TOKENS above.
FROZEN_MAX_STEPS = 0
BRIDGE_SKILLS = {
    "cantilever_box": "osis-bridge-cantilever-box",
    "conventional_box": "osis-bridge-conventional-box",
    "hollow_slab": "osis-bridge-hollow-slab",
    "precast_small_box": "osis-bridge-precast-small-box",
    "precast_t_girder": "osis-bridge-precast-t-girder",
    "rigid_frame": "osis-bridge-rigid-frame-box",
}
# Interpreter per architecture.  T1-T5 run on the framework's own venvs, which
# move with this directory; T6 is resolved per run because it needs the parent
# repo's interpreter (see ``framework_venv``).
FRAMEWORK_LOCAL_VENVS = {
    # T1/T2 share the comparison runner environment, whose interpreter is
    # fixed by the setup manifest rather than by whichever ``python`` happens
    # to be on PATH.
    "T1": PROJECT_ROOT / ".venvs" / "main" / "Scripts" / "python.exe",
    "T2": PROJECT_ROOT / ".venvs" / "main" / "Scripts" / "python.exe",
    "T3": PROJECT_ROOT / ".venvs" / "t3" / "Scripts" / "python.exe",
    "T4": PROJECT_ROOT / ".venvs" / "t4" / "Scripts" / "python.exe",
    "T5": PROJECT_ROOT / ".venvs" / "t5" / "Scripts" / "python.exe",
}
FRAMEWORK_ARCHITECTURES = (*FRAMEWORK_LOCAL_VENVS, "T6")


def framework_venv(architecture: str, parent_repo: Path | None = None) -> Path:
    """Interpreter that runs ``architecture``'s adapter.

    T6 needs the parent repo's python: it imports opencode_client/opencode_ai
    which are installed in the parent venv, not in .venvs/main.  That location
    comes from ``parent_repo`` (or the recorded config) -- never from this
    file's position, since the framework is a sibling of the parent repo
    rather than a child of it.  T1-T5 never consult it: their interpreters live
    in .venvs and move with this directory.
    """

    if architecture == "T6":
        return Path(resolve_parent_repo(parent_repo)) / ".venv" / "Scripts" / "python.exe"
    return FRAMEWORK_LOCAL_VENVS[architecture]


FRAMEWORK_ADAPTER_MODULES = {
    "T3": "baselines.t3_smolagents.adapter",
    "T4": "baselines.t4_openhands.adapter",
    "T5": "baselines.t5_crewai.adapter",
    "T6": "baselines.t6_osisai.adapter",
}

# Infrastructure failures: transport / quota / timeout. Everything else is
# treated as a model-behaviour outcome that must NOT be retried.
INFRA_MARKERS = (
    "model request failed",
    "model returned a non-JSON response",
    "HTTP 429",
    "429",
    "RateLimit",
    "rate limit",
    "upstream_error",
    "1302",
    "timed out",
    "TimeoutExpired",
    "ConnectionError",
    "connection error",
    "HTTP 402",
    "402",
    "insufficient balance",
    "insufficient quota",
    "quota exceeded",
    "billing",
    "payment required",
    "task timeout",
    "total timeout",
    "TimeoutError",
    "framework subprocess exceeded",
    "generation exceeded",
    # headless/desktop OSIS backend dropped the connection mid-build
    "connectionreseterror",
    "connection aborted",
    "远程主机强迫关闭",
    "10054",
    # T6 isolated opencode server never came up (port clash / slow start)
    "did not become healthy",
)


class GenerationFailure(RuntimeError):
    """A framework generation that did not succeed (stuck/failed/incomplete)."""


def _ensure_osis_runtime_backend(parent_repo: Path) -> None:
    """Start the headless OSIS engine if nothing serves the runtime port.

    Every architecture's P4 phase proxies engine operations to
    ``OSIS_HTTP_PORT`` (default 18080).  The desktop app normally provides it;
    unattended runs cannot assume that, and the detached headless server used
    for diagnostics dies with whatever session spawned it.  This prober
    launches ``serve_headless_osis.py`` as a child of THIS process when the
    port is dark, so the engine reliably outlives interactive sessions and
    covers exactly the lifetime of the run.
    """

    import httpx
    import time as _time

    port = os.environ.get("OSIS_HTTP_PORT", "18080")

    def _serving() -> bool:
        try:
            httpx.get(f"http://127.0.0.1:{port}/OSIS_Run", timeout=3.0)
            return True
        except Exception:  # noqa: BLE001 - any failure means "not serving"
            return False

    if _serving():
        return

    helper = PROJECT_ROOT / "scripts" / "serve_headless_osis.py"
    # ``serve_headless_osis.py`` imports pyosis, which lives in the PARENT
    # repo's venv; the framework's own .venvs do not carry it.
    parent_python = Path(parent_repo) / ".venv" / "Scripts" / "python.exe"
    if parent_python.is_file():
        interpreter = str(parent_python)
    else:
        interpreter = sys.executable
        print(
            f"warning: {parent_python} not found; starting the headless OSIS "
            f"engine with {interpreter}, which may not provide pyosis",
            file=sys.stderr,
        )
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
    )
    subprocess.Popen(
        [interpreter, str(helper), "--port", port],
        cwd=str(PROJECT_ROOT),
        env=dict(os.environ),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags if os.name == "nt" else 0,
    )
    deadline = _time.monotonic() + 90.0
    while _time.monotonic() < deadline:
        if _serving():
            return
        _time.sleep(2)
    # Last resort: if the port never comes up the run will fail at P4 with
    # the engine's own connection error, which the infra retry classifies.


def _bypass_system_proxy_for_gateway(base_url: str | None = None) -> None:
    """Make the model gateway exempt from the Windows system proxy.

    Windows' ``ProxyEnable``/``ProxyServer`` settings route HTTP through a
    local proxy, and neither httpx (openai SDK, used by T3/T4/T5/T6) nor
    requests (T1/T2) reliably honours the ``ProxyOverride`` exclusion list for
    a public gateway IP.  The request then hangs until the model timeout
    instead of failing fast.  Pinning ``NO_PROXY`` in the process environment
    covers every architecture, in-process and subprocess alike.
    """

    hosts = ["127.0.0.1", "localhost", "::1", "47.92.150.231"]
    resolved = urlparse(base_url or os.environ.get("OSIS_MODEL_BASE_URL", "")).hostname
    if resolved:
        hosts.append(resolved)
    for var in ("NO_PROXY", "no_proxy"):
        current = [p.strip() for p in os.environ.get(var, "").split(",") if p.strip()]
        os.environ[var] = ",".join(sorted(set(current) | set(hosts)))


def _run_dir(runs_dir: Path, task_id: str, architecture_id: str, seed: int) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", task_id).strip("._") or "task"
    return (Path(runs_dir) / f"{safe}__{architecture_id}__seed{seed}").resolve()


def _is_infra_error(text: str | None) -> bool:
    normalized = str(text or "").lower()
    return any(marker.lower() in normalized for marker in INFRA_MARKERS)


def _effective_generation_timeout_s(total_timeout_s: float) -> float:
    """Return the generation budget while reserving post-processing time.

    The formal profile is 5400s total, of which at most 3600s is available to
    a framework.  Keeping a reserve makes an explicit 3600s diagnostic run
    behave like the previous one-hour profile instead of starving PyOSIS and
    the source scorer.
    """

    total = float(total_timeout_s)
    return min(
        FROZEN_GENERATION_STEP_TIMEOUT_S,
        max(1.0, total - FROZEN_POST_GENERATION_RESERVE_S),
    )


PERMANENT_INFRA_MARKERS = (
    "http 402",
    "status code 402",
    "error code: 402",
    "code 402",
    "insufficient balance",
    "insufficient quota",
    "quota exceeded",
    "payment required",
    "billing",
    "余额不足",
)


def _is_permanent_infra_error(text: str | None) -> bool:
    """Return true for account/configuration failures that cannot recover by retry."""

    normalized = str(text or "").lower()
    return any(marker in normalized for marker in PERMANENT_INFRA_MARKERS)


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _framework_generation(
    architecture: str, request: str | Path, env: dict[str, str] | None = None,
    timeout_s: float = FROZEN_GENERATION_STEP_TIMEOUT_S,
    *, parent_repo: Path | None = None,
) -> dict:
    """Run one framework adapter in its own venv and generation workspace.

    The adapter process must not inherit the comparison repository as its
    current directory.  Some framework/SDK helpers use relative paths for
    temporary files; keeping the child in ``generated/`` prevents those files
    from becoming repository-level or OSIS-project output.  The repository is
    added explicitly to ``PYTHONPATH`` because the child still imports the
    adapter package by module name.
    """

    request_path = Path(request)
    log_dir = request_path.parent.resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    def _write_child_logs(stdout: object = "", stderr: object = "") -> None:
        """Keep raw child output for post-mortem diagnosis without printing it."""

        log_dir.mkdir(parents=True, exist_ok=True)
        for name, value in (
            ("framework_stdout.log", stdout),
            ("framework_stderr.log", stderr),
        ):
            if isinstance(value, bytes):
                value = value.decode("utf-8", errors="replace")
            try:
                (log_dir / name).write_text(str(value or ""), encoding="utf-8")
            except OSError:
                # Logging must never hide the framework result.
                pass

    python = framework_venv(architecture, parent_repo)
    if not python.is_file():
        result = {"architecture_id": architecture, "status": "framework_env_missing",
                  "error": f"interpreter not found: {python}"}
        _write_child_logs(stderr=result["error"])
        return result
    module = FRAMEWORK_ADAPTER_MODULES[architecture]
    child_env = dict(env or os.environ)
    # Skill bodies and agent logs contain non-GBK characters (✅ etc.); the
    # packaged OSIS-AI launcher sets PYTHONIOENCODING=utf-8 for the same
    # reason.  Without it a child printing such text dies with
    # UnicodeEncodeError on a GBK Windows console.
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    # Windows ships a system proxy (ProxyEnable/ProxyServer in the registry)
    # that the openai SDK's httpx transport picks up.  httpx does not honour
    # Windows' ``ProxyOverride`` exclusion list, so the model gateway — a
    # public IP that is NOT in that list — gets routed through a local proxy
    # that cannot reach it, and the request hangs until the model timeout.
    # ``requests`` (T1/T2) resolves ProxyOverride correctly, which is why only
    # the httpx-based architectures (T3/T4/T5/T6) stalled.  Pin the bypass
    # explicitly for every child so all six architectures behave identically.
    proxy_bypass = [host for host in (
        "127.0.0.1", "localhost", "::1",
        # model gateway host, derived from the configured base url
        *((urlparse(os.environ.get("OSIS_MODEL_BASE_URL", "")).hostname,) if os.environ.get("OSIS_MODEL_BASE_URL") else ()),
        "47.92.150.231",
    ) if host]
    for var in ("NO_PROXY", "no_proxy"):
        current = [p.strip() for p in child_env.get(var, "").split(",") if p.strip()]
        merged = sorted(set(current) | set(proxy_bypass))
        child_env[var] = ",".join(merged)
    old_pythonpath = child_env.get("PYTHONPATH", "")
    child_env["PYTHONPATH"] = os.pathsep.join(
        item for item in (str(PROJECT_ROOT), old_pythonpath) if item
    )
    # The adapter cannot derive the parent repo from its own location (the
    # framework is a sibling of it), so hand it over explicitly.  The request
    # JSON carries the same value for adapters that read it there.
    child_env["OSIS_PARENT_REPO"] = str(resolve_parent_repo(parent_repo))
    try:
        proc = subprocess.run(
            [str(python), "-m", module, "--request", str(request_path)],
            cwd=str(log_dir), capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout_s, env=child_env,
        )
    except subprocess.TimeoutExpired as exc:
        _write_child_logs(getattr(exc, "stdout", ""), getattr(exc, "stderr", ""))
        return {"architecture_id": architecture, "status": "timeout",
                "error_type": "TimeoutExpired",
                "error": f"framework subprocess exceeded {timeout_s:.0f}s budget"}
    _write_child_logs(proc.stdout, proc.stderr)
    if proc.returncode != 0:
        return {"architecture_id": architecture, "status": "framework_crashed",
                "error": (proc.stderr or proc.stdout or "")[-800:]}
    try:
        return json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"architecture_id": architecture, "status": "framework_bad_output",
                "error": (proc.stdout or "")[-400:]}


def _persist_framework_metadata(
    workspace: Path, architecture: str, meta: dict[str, object]
) -> dict[str, object]:
    """Persist a child-process result even when it died before ``finish()``.

    The framework adapters normally write ``tX_generation.json`` themselves.
    A hard subprocess timeout can kill them before their ``finally`` block,
    however, while still leaving several candidate files behind.  The outer
    driver must retain the timeout/crash status in that case; otherwise the
    runner sees a directory with no metadata and incorrectly calls it
    ``completed``.
    """

    workspace = Path(workspace)
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / f"{architecture.lower()}_generation.json"
    payload: dict[str, object] = {}
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            payload.update(existing)
    except (OSError, json.JSONDecodeError):
        pass
    # Keep a complete adapter record if one exists, but make the subprocess
    # outcome authoritative for fields that were unavailable in that record.
    payload.update({key: value for key, value in meta.items() if key not in payload})
    payload.setdefault("architecture_id", architecture)
    candidate = workspace / "candidate_project"
    if candidate.is_dir():
        payload["files_written"] = sorted(
            path.relative_to(candidate).as_posix()
            for path in candidate.rglob("*")
            if path.is_file()
        )
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return payload


def _file_hashes(paths: list[str] | tuple[str, ...]) -> dict[Path, str]:
    """Snapshot files staged before model generation."""

    result: dict[Path, str] = {}
    for raw in paths:
        path = Path(raw).expanduser().resolve()
        try:
            result[path] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
    return result


def _write_prestage_manifest(
    workspace: Path,
    files: list[str] | tuple[str, ...],
    hashes: dict[Path, str],
) -> None:
    """Persist the pre-generation baseline used by retry/progress probes.

    A list of ``*.py`` files is not sufficient for gen/edit tasks: those files
    are trusted, unchanged base files and must not make an empty generation
    look successful.  Store content hashes so a changed base file, a newly
    created markdown profile, or any other new artifact is detected exactly.
    """

    payload = {
        "files": [str(path) for path in files],
        "sha256": {str(path): digest for path, digest in hashes.items()},
    }
    Path(workspace, "prestaged_files.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_prestage_manifest(workspace: Path) -> tuple[list[str], dict[Path, str]]:
    """Read a baseline manifest, accepting the old list-only format."""

    path = Path(workspace) / "prestaged_files.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return [], {}
    if isinstance(payload, list):
        return [str(item) for item in payload], {}
    if not isinstance(payload, dict):
        return [], {}
    files = payload.get("files") or []
    raw_hashes = payload.get("sha256") or {}
    if not isinstance(files, list) or not isinstance(raw_hashes, dict):
        return [], {}
    hashes: dict[Path, str] = {}
    for raw_path, digest in raw_hashes.items():
        if isinstance(raw_path, str) and isinstance(digest, str):
            hashes[Path(raw_path).expanduser().resolve()] = digest
    return [str(item) for item in files], hashes


def _refresh_manifest(run_dir: Path) -> None:
    """Hash artifacts added/updated by the post-run scoring finalizer."""

    path = Path(run_dir) / "manifest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict):
        return
    payload["artifact_hashes"] = artifact_hashes(Path(run_dir))
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _has_candidate_progress(
    candidate_root: Path,
    *,
    preexisting_files: list[str] | tuple[str, ...] = (),
    preexisting_hashes: dict[Path, str] | None = None,
) -> bool:
    """Return whether generation produced or changed any candidate file."""

    root = Path(candidate_root).expanduser().resolve()
    if not root.is_dir():
        return False
    baseline = {Path(raw).expanduser().resolve() for raw in preexisting_files}
    baseline_hashes = preexisting_hashes or {}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        resolved = path.resolve()
        if resolved not in baseline:
            return True
        old_hash = baseline_hashes.get(resolved)
        if old_hash is None:
            continue
        try:
            if hashlib.sha256(path.read_bytes()).hexdigest() != old_hash:
                return True
        except OSError:
            continue
    return False


def _require_completed(
    meta: dict,
    architecture_id: str,
    candidate_root: Path,
    *,
    preexisting_files: list[str] | tuple[str, ...] = (),
    preexisting_hashes: dict[Path, str] | None = None,
) -> None:
    """Keep real generated progress even when the framework reports failure.

    A non-completed framework status is diagnostic, not a reason to delete a
    partial candidate. The runner scores available source dimensions and keeps
    ``complete_success`` false when the project cannot pass all gates. A run
    with no new or changed file remains fatal.
    """

    if not _has_candidate_progress(
        candidate_root,
        preexisting_files=preexisting_files,
        preexisting_hashes=preexisting_hashes,
    ):
        status = meta.get("status")
        raise GenerationFailure(
            meta.get("error")
            or f"{architecture_id} generation status={status} with no candidate files"
        )


def _write_scorer_private(run_dir: Path, entry: DatasetEntry, parent_repo: Path) -> None:
    private = Path(run_dir) / "scorer_private"
    private.mkdir(parents=True, exist_ok=True)
    (private / "reference.json").write_text(
        json.dumps(reference_record(entry), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if not entry.y_template:
        return
    # The reference PROJECT is the whole standard-answer template directory
    # (for full it IS y; for gen/edit the target file lives inside it), so the
    # reference-relative scorer can diff the full project. ``y_template`` is
    # the template dir name (y_relative drops it for gen/edit).
    y_project_dir = (
        Path(parent_repo) / ".agents" / "skills" / entry.bridge_skill
        / "references" / "templates" / entry.y_template
    )
    if not y_project_dir.is_dir():
        return
    reference_root = private / "reference_project"
    reference_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(y_project_dir, reference_root, dirs_exist_ok=True)


def _runtime_stats(run_dir: Path, architecture_id: str) -> dict[str, object]:
    """Best-effort runtime_stats for the reference evaluator (efficiency/cost)."""

    stats: dict[str, object] = {}
    timer_path = Path(run_dir) / "timer.json"
    if timer_path.is_file():
        try:
            elapsed = json.loads(timer_path.read_text(encoding="utf-8")).get("elapsed_s")
            if isinstance(elapsed, (int, float)):
                stats["duration_s"] = float(elapsed)
        except (OSError, json.JSONDecodeError):
            pass

    tokens: int | None = None
    workspace = Path(run_dir) / "generated"
    gen_path = workspace / f"{architecture_id.lower()}_generation.json"
    if gen_path.is_file():
        try:
            gen = json.loads(gen_path.read_text(encoding="utf-8"))
            tok = gen.get("tokens") or {}
            if isinstance(tok, dict) and isinstance(tok.get("total_tokens"), int):
                # A normalized all-zero placeholder means the framework did
                # not expose usage; treating it as real zero would award a
                # perfect cost score to a failed/missing-token run.
                candidate_tokens = int(tok["total_tokens"])
                if candidate_tokens > 0:
                    tokens = candidate_tokens
        except (OSError, json.JSONDecodeError):
            tokens = None
    if tokens is None:
        transcript = workspace / "t2_transcript.jsonl"
        if transcript.is_file():
            total = 0
            try:
                for line in transcript.read_text(encoding="utf-8").splitlines():
                    record = json.loads(line)
                    usage = record.get("usage") or {}
                    if isinstance(usage, dict):
                        total += normalize_tokens(usage)["total_tokens"]
                if total > 0:
                    tokens = total
            except (OSError, json.JSONDecodeError, ValueError):
                tokens = None
    if tokens is not None:
        stats["total_tokens"] = tokens
    return stats


def _write_frozen_config(
    run_dir: Path,
    args: argparse.Namespace,
    skills_dir: Path,
    *,
    task: TaskSpec | None = None,
) -> None:
    snapshot_sha = None
    manifest_path = Path(skills_dir).parent / "manifest.json"
    if manifest_path.is_file():
        try:
            snapshot_sha = json.loads(manifest_path.read_text(encoding="utf-8")).get("sha256")
        except (OSError, json.JSONDecodeError):
            snapshot_sha = None
    configured_total_timeout = (
        float(args.total_timeout_s)
        if args.total_timeout_s is not None
        else (float(task.total_timeout_s) if task is not None else None)
    )
    # Record the interpreter that actually ran the adapter, so the frozen
    # config names the same environment the run used.
    framework_python = framework_venv(args.architecture, args.parent_repo)
    if not framework_python.is_file():
        framework_python = Path(sys.executable).resolve()
    frozen = {
        "architecture": args.architecture,
        "task_id": task.task_id if task is not None else None,
        "model": args.model,
        "base_url": args.base_url,
        "temperature": args.temperature,
        "max_tokens": FROZEN_MAX_TOKENS,
        "weknora_enabled": knowledge_search_enabled(),
        "model_timeout_s": FROZEN_MODEL_TIMEOUT_S,
        "max_steps": FROZEN_MAX_STEPS,
        "generation_step_timeout_s": (
            _effective_generation_timeout_s(configured_total_timeout)
            if configured_total_timeout is not None
            else FROZEN_GENERATION_STEP_TIMEOUT_S
        ),
        "seed": args.seed,
        "replicate_id": args.seed,
        "split": args.split,
        "total_timeout_s": configured_total_timeout,
        "subtask_timeout_s": (
            dict(task.subtask_timeout_s or {}) if task is not None else None
        ),
        "pyosis_enabled": not args.no_pyosis,
        "solve_gate": bool(args.solve_gate),
        "retry_policy": f"infra-only, max {args.max_attempts} attempts, backoff {args.retry_backoff_s}s",
        "skills_snapshot": {"path": str(skills_dir), "sha256": snapshot_sha},
        "framework_python": str(framework_python),
        "environment_manifest": str(PROJECT_ROOT / ".venvs" / "environments.json"),
    }
    Path(run_dir, "frozen_config.json").write_text(
        json.dumps(frozen, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one dataset task on one architecture")
    parser.add_argument("--architecture", required=True, choices=("T1", "T2", "T3", "T4", "T5", "T6"))
    parser.add_argument(
        "--parent-repo", type=Path, default=None,
        help="OSIS skill repository to read datasets and skills from "
             "(default: $OSIS_PARENT_REPO, else configs/parent_repo.txt)",
    )
    parser.add_argument("--split", default="test", choices=("test", "train"))
    parser.add_argument("--bridge", required=True)
    parser.add_argument("--form", required=True, choices=("full", "gen", "edit"))
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--skills-dir", type=Path, default=None)
    parser.add_argument("--allow-raw-skills", action="store_true")
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--model", default=FORMAL_MODEL_ID)
    parser.add_argument("--base-url", default="http://47.92.150.231/v1")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--max-attempts", type=_positive_int, default=4)
    parser.add_argument("--retry-backoff-s", type=_nonnegative_float, default=120.0)
    # Official runs MUST execute PyOSIS; the flag only exists for diagnostics.
    parser.add_argument("--no-pyosis", action="store_true",
                        help="diagnostics only: skip PyOSIS execution and CLI scoring")
    parser.add_argument("--solve-gate", action="store_true")
    parser.add_argument("--total-timeout-s", type=_positive_float, default=None)
    return parser


def _resolve_skills_dir(args: argparse.Namespace) -> Path:
    skills_dir = resolve_skills_dir(args.skills_dir)
    if not skills_dir.is_dir():
        raise SystemExit(
            f"skills snapshot does not exist: {skills_dir} "
            "(build it with scripts/build_train_all_snapshot.py)"
        )
    if not args.allow_raw_skills:
        try:
            skills_dir.relative_to((args.parent_repo / ".agents").resolve())
        except ValueError:
            pass
        else:
            raise SystemExit(
                "refusing to mount the raw .agents/skills tree (test templates are the answers)"
            )
    return skills_dir


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # The framework is a sibling of the parent repo, so its location is named
    # explicitly (--parent-repo / OSIS_PARENT_REPO / configs/parent_repo.txt)
    # rather than derived from this file's position.  Resolved first: the
    # backend bootstrap below needs the parent venv.
    resolve_args_parent_repo(args)
    # Applied before any model call: the gateway must bypass the Windows
    # system proxy for every architecture (see the helper's docstring).
    _bypass_system_proxy_for_gateway(args.base_url)
    # And the OSIS engine backend must be alive before P4 runs; start the
    # headless engine when the desktop is not serving (see the helper).
    _ensure_osis_runtime_backend(args.parent_repo)
    skills_dir = _resolve_skills_dir(args)
    if args.runs_dir is None:
        # This is the unified official entry point.  Diagnostics/pilots pass
        # an explicit ``--runs-dir`` so their artifacts cannot silently enter
        # (or be mistaken for) the official result tree.
        args.runs_dir = resolve_run_root() / "official" / args.architecture
    entry: DatasetEntry = load_dataset_entry(
        args.parent_repo, split=args.split, bridge_skill=args.bridge,
        form=args.form, index=args.index,
    )
    task = to_task_spec(entry)
    skill_reader = SkillAdapter(skills_dir)

    # ---- leakage gate (blocks the run on any issue; crashes are tolerated) --
    try:
        report = run_guard(
            parent_repo=args.parent_repo, bridge=args.bridge, skills_dir=skills_dir,
            task_dict=task.to_dict(), base_files=entry.base_files,
        )
    except Exception as exc:  # noqa: BLE001
        # The gate is fail-closed: a guard crash must never silently turn into
        # an unverified official run.  Keep a small failure record so the
        # operator can distinguish it from an actual model/adapter failure.
        failures_dir = Path(args.runs_dir) / "leakage_guard_failures"
        failures_dir.mkdir(parents=True, exist_ok=True)
        record = failures_dir / f"{task.task_id}__{args.architecture}__seed{args.seed}.json"
        record.write_text(
            json.dumps(
                {
                    "task_id": task.task_id,
                    "architecture_id": args.architecture,
                    "seed": args.seed,
                    "status": "leakage_guard_error",
                    "ok": False,
                    "checks": {"guard_error": [f"{type(exc).__name__}: {exc}"]},
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        print(json.dumps(
            {"leakage_guard": "guard_error", "record": str(record)},
            ensure_ascii=False,
        ))
        return 2
    if report is not None and not report.ok:
        failures_dir = Path(args.runs_dir) / "leakage_guard_failures"
        failures_dir.mkdir(parents=True, exist_ok=True)
        record = failures_dir / f"{task.task_id}__{args.architecture}__seed{args.seed}.json"
        record.write_text(
            json.dumps(
                {"task_id": task.task_id, "status": "leakage_guard_failed", **report.to_dict()},
                ensure_ascii=False, indent=2,
            ) + "\n",
            encoding="utf-8",
        )
        print(json.dumps({"leakage_guard": "failed", "record": str(record)}, ensure_ascii=False))
        return 2
    print(json.dumps({"leakage_guard": "passed"}, ensure_ascii=False))

    api_key = os.environ.get("OSIS_MODEL_API_KEY") or os.environ.get("OSIS_API_KEY") or ""
    if not api_key:
        print(json.dumps(
            {"status": "api_key_missing",
             "error": "OSIS_MODEL_API_KEY is not set; nothing can be generated"},
            ensure_ascii=False))
        return 3
    bridge_skill = BRIDGE_SKILLS.get(task.bridge_type, "")
    total_timeout_s = float(args.total_timeout_s or task.total_timeout_s)
    # Generation has its own ceiling, but it also consumes the task's total
    # timeout.  Pass this same clock into ExperimentRunner below so the
    # post-generation PyOSIS/CLI stages cannot silently obtain a second full
    # budget.
    generation_started = time.monotonic()
    total_deadline = generation_started + total_timeout_s
    generation_timeout_s = _effective_generation_timeout_s(total_timeout_s)
    generation_deadline = min(
        total_deadline, generation_started + generation_timeout_s
    )

    def generate_candidate(task_spec, workspace, mounted_skills):
        prestaged = prestage_base_files(entry, args.parent_repo, workspace / "candidate_project")
        prestaged_hashes = _file_hashes(prestaged)
        _write_prestage_manifest(workspace, prestaged, prestaged_hashes)
        remaining = min(
            generation_deadline - time.monotonic(),
            total_deadline - time.monotonic(),
        )
        if remaining <= 0:
            raise TimeoutError("total task timeout expired before generation completed")
        architecture = args.architecture
        if architecture == "T1":
            from baselines.t1_direct.adapter import generate_t1

            generate_t1(
                task_spec, mounted_skills, workspace, bridge_skill=bridge_skill,
                base_url=args.base_url, model=args.model, api_key=api_key,
                max_tokens=FROZEN_MAX_TOKENS,
                # T1 is one-shot, but the per-request limit remains the same
                # frozen model timeout as T2-T6.  It may use less when the
                # task-wide deadline is already close.
                request_timeout_s=max(1.0, min(FROZEN_MODEL_TIMEOUT_S, remaining)),
                temperature=args.temperature,
            )
            meta_path = workspace / "t1_generation.json"
            _require_completed(
                json.loads(meta_path.read_text(encoding="utf-8")),
                "T1",
                workspace / "candidate_project",
                preexisting_files=prestaged,
                preexisting_hashes=prestaged_hashes,
            )
            return workspace / "candidate_project"
        if architecture == "T2":
            from baselines.t2_langgraph.adapter import T2Config, generate

            generate(
                task_spec, mounted_skills, workspace,
                config=T2Config.from_env(
                    base_url=args.base_url, model=args.model,
                    temperature=args.temperature, max_tokens=FROZEN_MAX_TOKENS,
                    request_timeout_s=max(1.0, min(FROZEN_MODEL_TIMEOUT_S, remaining)),
                    max_steps=FROZEN_MAX_STEPS,
                ),
                deadline_monotonic=min(
                    generation_deadline,
                    total_deadline,
                ),
            )
            meta_path = workspace / "t2_generation.json"
            _require_completed(
                json.loads(meta_path.read_text(encoding="utf-8")),
                "T2",
                workspace / "candidate_project",
                preexisting_files=prestaged,
                preexisting_hashes=prestaged_hashes,
            )
            return workspace / "candidate_project"
        if architecture in FRAMEWORK_ARCHITECTURES:
            request_path = workspace / "generation_request.json"
            # api key travels via env only; never into run artifacts.
            request_path.write_text(
                json.dumps(
                    {
                        "task": task_spec.to_dict(), "bridge_skill": bridge_skill,
                        "skills_dir": str(skills_dir), "workspace": str(workspace),
                        # adapters cannot derive this from their own location
                        "parent_repo": str(args.parent_repo),
                        "base_url": args.base_url, "model": args.model,
                        "max_tokens": FROZEN_MAX_TOKENS,
                        "request_timeout_s": FROZEN_MODEL_TIMEOUT_S,
                        "max_steps": FROZEN_MAX_STEPS,
                        "generation_timeout_s": remaining,
                        "total_timeout_s": task_spec.total_timeout_s,
                        "subtask_timeout_s": task_spec.subtask_timeout_s,
                        "temperature": args.temperature,
                    },
                    ensure_ascii=False, indent=2,
                ),
                encoding="utf-8",
            )
            meta = _framework_generation(
                architecture, request_path,
                env={**os.environ, "OSIS_MODEL_API_KEY": api_key},
                timeout_s=remaining,
                parent_repo=args.parent_repo,
            )
            meta = _persist_framework_metadata(workspace, architecture, meta)
            _require_completed(
                meta,
                architecture,
                workspace / "candidate_project",
                preexisting_files=prestaged,
                preexisting_hashes=prestaged_hashes,
            )
            return workspace / "candidate_project"
        raise RuntimeError(
            f"architecture {architecture} is not wired yet"
        )  # caught by runner -> clean generation_failed artifact

    runner = ExperimentRunner(
        skills_dir=skills_dir, runs_dir=args.runs_dir, parent_repo=args.parent_repo,
        pyosis_enabled=not args.no_pyosis, solve_gate=args.solve_gate,
        total_timeout_override_s=args.total_timeout_s,
        model_snapshot=f"{args.model}@{args.base_url}",
    )

    # ---- unified infra-only retry around the whole run ----------------------
    summary = None
    infra_failure: dict[str, object] | None = None
    for attempt in range(1, args.max_attempts + 1):
        if attempt > 1:
            delay = args.retry_backoff_s * (attempt - 1)
            remaining_before_retry = total_deadline - time.monotonic()
            if remaining_before_retry <= 0:
                if infra_failure is not None:
                    infra_failure["retry_skipped"] = "task_deadline_expired"
                break
            if delay >= remaining_before_retry:
                if infra_failure is not None:
                    infra_failure["retry_skipped"] = "backoff_exceeds_task_deadline"
                break
            print(json.dumps({"retry": f"attempt {attempt}/{args.max_attempts} after infra failure"},
                             ensure_ascii=False))
            time.sleep(delay)
        summary = runner.run(
            task, architecture_id=args.architecture, seed=args.seed,
            candidate_generator=generate_candidate,
            started_monotonic=generation_started,
            deadline_monotonic=total_deadline,
        )
        execution_trace_path = Path(summary.run_dir) / "execution_trace.json"
        infra = False
        try:
            trace = json.loads(execution_trace_path.read_text(encoding="utf-8"))
            gen = trace.get("generation") or {}
            infra = _is_infra_error(gen.get("error")) or _is_infra_error(gen.get("error_type"))
        except (OSError, json.JSONDecodeError):
            infra = False
        if infra:
            infra_failure = {
                "status": "infrastructure_failure",
                "attempt": attempt,
                "max_attempts": args.max_attempts,
                "error_type": gen.get("error_type") if isinstance(gen, dict) else None,
                "error": gen.get("error") if isinstance(gen, dict) else None,
                "retry_policy": "infra-only",
                "excluded_from_aggregate": True,
            }
        if not infra or attempt == args.max_attempts:
            break
        infra_text = ""
        if isinstance(gen, dict):
            infra_text = f"{gen.get('error_type', '')} {gen.get('error', '')}"
        if _is_permanent_infra_error(infra_text):
            print(json.dumps(
                {"infra_failure": "permanent",
                 "note": "account/quota failure; skipping redundant retries"},
                ensure_ascii=False,
            ))
            break
        # Only retry on infra if ZERO candidate progress was made. Retrying an
        # infra failure that struck AFTER the framework wrote real files would
        # delete that partial output (a valuable diagnostic + near-complete
        # project), so keep it and record generation_failed instead.
        # Probe the ACTUAL candidate path during generation: the framework
        # writes into run_dir/generated/candidate_project (runner.py), not the
        # post-materialization run_dir/candidate_project.
        candidate_root_probe = Path(summary.run_dir) / "generated" / "candidate_project"
        if not candidate_root_probe.is_dir():
            candidate_root_probe = Path(summary.run_dir) / "candidate_project"
        baseline_files, baseline_hashes = _read_prestage_manifest(
            candidate_root_probe.parent
        )
        any_progress = _has_candidate_progress(
            candidate_root_probe,
            preexisting_files=baseline_files,
            preexisting_hashes=baseline_hashes,
        )
        if any_progress:
            print(json.dumps(
                {"infra_failure": "not_retrying_candidate_progress",
                 "note": "infra error hit after files were written; keeping partial"},
                ensure_ascii=False))
            break
        shutil.rmtree(Path(summary.run_dir), ignore_errors=True)
        print(json.dumps({"infra_failure": "retrying"}, ensure_ascii=False))

    if infra_failure is not None:
        final_trace_path = Path(summary.run_dir) / "execution_trace.json"
        try:
            final_trace = json.loads(final_trace_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            final_trace = {}
        if isinstance(final_trace, dict):
            generation = final_trace.setdefault("generation", {})
            if isinstance(generation, dict):
                generation["infrastructure_failure"] = True
            final_trace["excluded_from_aggregate"] = True
            final_trace_path.write_text(
                json.dumps(final_trace, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
        Path(summary.run_dir, "infra_failure.json").write_text(
            json.dumps(infra_failure, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    _write_scorer_private(Path(summary.run_dir), entry, args.parent_repo)
    _write_frozen_config(Path(summary.run_dir), args, skills_dir, task=task)
    if report is not None:
        Path(summary.run_dir, "leakage_guard.json").write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    # Reference-aware model scoring: candidate vs the standard project, using
    # the parent repo's src/evaluation (the SINGLE official score). PyOSIS
    # execution stays a gate above. The runner's simplified composite is
    # replaced here for official runs (kept only as a no-reference fallback).
    candidate_root = Path(summary.run_dir) / "candidate_project"
    reference_root = Path(summary.run_dir) / "scorer_private" / "reference_project"
    ref_score = None
    reference_available = reference_root.is_dir()
    if candidate_root.is_dir() and reference_available:
        from common.pyosis_adapter import resolve_python_executable
        from common.reference_scorer import score as reference_score

        ref_score = reference_score(
            reference_root=reference_root,
            candidate_root=candidate_root,
            parent_repo=args.parent_repo,
            run_dir=Path(summary.run_dir),
            bridge_type=task.bridge_type,
            is_continuous=(task.metadata or {}).get("is_continuous"),
            runtime_stats=_runtime_stats(Path(summary.run_dir), args.architecture),
            python_executable=resolve_python_executable(args.parent_repo),
        )
    if ref_score:
        from common.official_evaluation import build_official_evaluation, overwrite_evaluation

        gate = True
        run_dir_path = Path(summary.run_dir)
        backend = {}
        compile_result = {}
        for name in ("backend_status.json", "compile.json"):
            try:
                import json as _json

                data = _json.loads((run_dir_path / name).read_text(encoding="utf-8"))
            except (OSError, _json.JSONDecodeError):
                data = {}
            if name == "backend_status.json":
                backend = data
            else:
                compile_result = data
        if not compile_result.get("passed", False) or not backend.get("model_created", False):
            gate = False
        official = build_official_evaluation(
            run_dir=run_dir_path, ref_score=ref_score, model_conformance_gate=gate
        )
        overwrite_evaluation(run_dir_path, official)
        summary = replace(summary, evaluation=official)
    else:
        # The standard-answer project is absent (missing/failed staging), so
        # this run cannot be scored against the answer at all.  Overwrite the
        # runner's simplified composite: leaving it in place made a
        # different-scope number sit under the official ``evaluation.json``
        # filename, where a cross-architecture table reads it as the source
        # score.  The official structure records the honest verdict instead
        # (``reference_not_evaluated``), and the aggregate excludes it.
        from common.official_evaluation import build_official_evaluation, overwrite_evaluation

        official = build_official_evaluation(
            run_dir=Path(summary.run_dir), ref_score=None, model_conformance_gate=False
        )
        overwrite_evaluation(Path(summary.run_dir), official)
        summary = replace(summary, evaluation=official)
        # Generation produced no candidate at all (framework crash / service
        # never started).  Record it explicitly so the run is excluded from
        # aggregates instead of contributing a runner-scope zero.
        if not candidate_root.is_dir():
            infra = {
                "status": "generation_failed",
                "note": "no candidate project was produced; not a model outcome",
                "excluded_from_aggregate": True,
            }
            Path(summary.run_dir, "infra_failure.json").write_text(
                json.dumps(infra, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
    _refresh_manifest(Path(summary.run_dir))
    print(
        json.dumps(
            {
                "architecture_id": args.architecture,
                "task_id": task.task_id,
                "status": summary.status,
                "complete_success": summary.evaluation.complete_success,
                "quality_score": summary.evaluation.quality_score,
                "failure_reasons": summary.evaluation.failure_reasons,
                "infrastructure_failure": infra_failure is not None,
                "excluded_from_aggregate": infra_failure is not None,
                "run_dir": str(summary.run_dir),
            },
            ensure_ascii=False, indent=2,
        )
    )
    # A model/architecture failure is still a valid experimental observation
    # (and therefore returns success to the batch driver).  Infrastructure
    # failures are explicitly non-statistical and return a distinct non-zero
    # code so orchestration can schedule a later retry.
    return 4 if infra_failure is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
