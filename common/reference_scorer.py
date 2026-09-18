"""Reference-aware scoring against the parent repo's OSIS evaluator.

Runs the parent repository's ``evaluation.evaluate(reference, candidate, ...)``
(reference-vs-candidate, from ``src/evaluation/evaluate.py``) in the parent
repo's Python environment, and writes ``reference_score.json`` (overall_score +
the model_conformance subsystem detail) plus a markdown report.

This is the *actual* model-relative score — the candidate project compared
against the standard-answer project — as opposed to the intrinsic
``model_conformance`` CLI which only checks absolute construct plausibility.
It requires the scorer-private reference project (staged by the driver) and,
to include the efficiency/cost systems, a ``runtime_stats`` payload.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any, Callable

CommandRunner = Callable[..., subprocess.CompletedProcess[str]]

# comparison bridge_type -> parent evaluator model_conformance bridge_type
BRIDGE_TYPE_MAP = {
    "cantilever_box": "cantilever_box",
    "rigid_frame": "rigid_frame",
    "precast_t_girder": "t_girder",
    "precast_small_box": "precast_small_box",
    "conventional_box": "cast_in_place_box",
    "hollow_slab": "hollow_slab",
}


def resolve_parent_python(parent_repo: Path) -> str:
    for candidate in (
        Path(parent_repo) / ".venv" / "Scripts" / "python.exe",
        Path(parent_repo) / ".venv" / "bin" / "python",
    ):
        if candidate.is_file():
            return str(candidate)
    return "python"


def _candidate_files_for_scoring(candidate_root: Path) -> Path:
    """Expose candidate sources under the path layout the scorer expects.

    The parent evaluator's ``_extract_params`` keys on the reference layout,
    where ``prep/_N.py`` and ``main.py`` sit at the project root.  Comparison
    candidates live one level deeper under ``py/`` (the canonical output
    contract for T1--T6).  Scoring the raw tree therefore yields an empty
    extraction for EVERY architecture — every candidate collapses to the
    scorer's default 0.5 and the construction dimension stops discriminating.

    Returns a directory holding the same sources without the ``py/`` prefix.
    A directory (not an inlined dict) is used because candidate sources are
    large enough to overflow the Windows command line when embedded in the
    scoring script.
    """

    root = Path(candidate_root).expanduser().resolve()
    py_root = root / "py"
    source = py_root if py_root.is_dir() else root
    return source


def _script(
    reference_root: Path,
    candidate_root: Path,
    config_path: Path,
    bridge_type: str | None,
    is_continuous: bool | None,
    runtime_stats: dict[str, Any],
) -> str:
    payload = {
        "reference": str(reference_root),
        "candidate": str(_candidate_files_for_scoring(candidate_root)),
        "config": str(config_path),
        "bridge_type": bridge_type,
        "is_continuous": is_continuous,
        "runtime_stats": runtime_stats,
    }
    return (
        "import json, sys, os\n"
        "sys.path.insert(0, os.path.join(os.getcwd(), 'src'))\n"
        "from evaluation.evaluate import evaluate\n"
        f"p = {payload!r}\n"
        "resources = {'runtime_stats': p['runtime_stats']} if p['runtime_stats'] else None\n"
        "report = evaluate(\n"
        "    p['reference'], p['candidate'],\n"
        "    config_path=p['config'],\n"
        "    resources=resources,\n"
        "    bridge_type=p['bridge_type'],\n"
        "    is_continuous=p['is_continuous'],\n"
        ")\n"
        "print(json.dumps(report, ensure_ascii=False))\n"
    )


_COMPARISON_ROOT = Path(__file__).resolve().parents[1]


def _eval_config_path(parent_repo: Path) -> Path:
    """Prefer the comparison-local evaluation.yaml (fixed weights sum=1.0).

    Falls back to the parent repo's config only if the local one is absent.
    """
    local = _COMPARISON_ROOT / "configs" / "evaluation.yaml"
    if local.is_file():
        return local
    return Path(parent_repo) / "configs" / "evaluation.yaml"


def score(
    *,
    reference_root: Path,
    candidate_root: Path,
    parent_repo: Path,
    run_dir: Path,
    bridge_type: str,
    is_continuous: bool | None = None,
    runtime_stats: dict[str, Any] | None = None,
    python_executable: str | None = None,
    command_runner: CommandRunner | None = None,
    timeout_s: float = 300.0,
) -> dict[str, Any]:
    """Score candidate against reference and write reference_score.json/.md."""

    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    python = python_executable or resolve_parent_python(parent_repo)
    config_path = _eval_config_path(parent_repo)
    if not config_path.is_file():
        result = {"scorer": "osis-reference-eval", "status": "config_unavailable",
                  "reason": str(config_path)}
        (run_dir / "reference_score.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    mapped_bridge = BRIDGE_TYPE_MAP.get(bridge_type, bridge_type)
    script = _script(
        reference_root, candidate_root, config_path,
        mapped_bridge, is_continuous, runtime_stats or {},
    )
    runner = command_runner or subprocess.run
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = runner(
            [python, "-c", script],
            cwd=str(Path(parent_repo)),
            env=env,
            capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=timeout_s, check=False,
        )
    except subprocess.TimeoutExpired as exc:
        result = {"scorer": "osis-reference-eval", "status": "timeout",
                  "reason": "reference evaluation timed out",
                  "stdout": str(getattr(exc, "stdout", "") or "")[-800:]}
        (run_dir / "reference_score.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    (run_dir / "reference_score_stdout.log").write_text(proc.stdout or "", encoding="utf-8")
    (run_dir / "reference_score_stderr.log").write_text(proc.stderr or "", encoding="utf-8")
    try:
        report = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        result = {"scorer": "osis-reference-eval", "status": "scorer_error",
                  "reason": (proc.stderr or proc.stdout or "")[-800:]}
        (run_dir / "reference_score.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return result

    overall = report.get("overall_score")
    evaluation = report.get("evaluation") or {}
    systems = evaluation.get("systems") or {}
    conformance = systems.get("model_conformance") or {}
    report_text = conformance.get("report") or ""
    if report_text:
        (run_dir / "reference_score.md").write_text(report_text, encoding="utf-8")

    result = {
        "scorer": "osis-reference-eval",
        "status": "evaluated",
        "overall_score": overall,
        "model_conformance_score": conformance.get("overall_score"),
        "systems": {name: detail.get("overall_score") for name, detail in systems.items()},
        "reference_root": str(reference_root),
        "candidate_root": str(candidate_root),
    }
    (run_dir / "reference_score.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result
