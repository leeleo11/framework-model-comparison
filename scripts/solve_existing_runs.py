"""Run PyOSIS solve on existing runs without regenerating model candidates.

This is intentionally a post-processing command.  It reuses each run's
``candidate_project`` and existing source/reference score, then refreshes the
runtime status and official evaluation after ``engine.solve()``.  No model
adapter is started and no generation transcript is changed.

Usage::

    python scripts/solve_existing_runs.py \
        --runs-dir runs/formal-one-full-all-20260913 \
        --parent-repo ..\\osis-skill-enhance-main
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.modeling_pipeline import validate_project_layout
from common.official_evaluation import build_official_evaluation, overwrite_evaluation
from common.paths import resolve_args_parent_repo
from common.pyosis_adapter import PyOSISAdapter, resolve_python_executable
from common.runtime_scorer import evaluate_runtime_snapshot
from scripts.run_dataset import _ensure_osis_runtime_backend


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def iter_run_dirs(runs_dir: Path) -> list[Path]:
    """Return direct run directories, excluding campaign summaries and archives."""

    root = Path(runs_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    return sorted(
        path for path in root.iterdir()
        if path.is_dir() and (path / "candidate_project").is_dir()
    )


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def solve_one(
    run_dir: Path,
    parent_repo: Path,
    *,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Solve and re-evaluate one existing run in place."""

    run_dir = Path(run_dir).expanduser().resolve()
    candidate = run_dir / "candidate_project"
    if not candidate.is_dir():
        return {"run_dir": str(run_dir), "status": "skipped", "reason": "candidate_missing"}

    old_backend = _read_json(run_dir / "backend_status.json")
    if old_backend.get("solve_requested") and not force:
        return {"run_dir": str(run_dir), "status": "skipped", "reason": "already_solved"}

    task = _read_json(run_dir / "input.json")
    layout = validate_project_layout(candidate)
    _write_json(run_dir / "layout.json", layout)
    subtask = task.get("subtask_timeout_s") or {}
    try:
        timeout_s = float(subtask.get("P4", 1200.0))
    except (TypeError, ValueError):
        timeout_s = 1200.0

    record: dict[str, Any] = {
        "status": "planned" if dry_run else "running",
        "solve_requested": True,
        "started_at": time.time(),
        "candidate_project": str(candidate),
        "parent_repo": str(Path(parent_repo).resolve()),
        "timeout_s": timeout_s,
    }
    if dry_run:
        _write_json(run_dir / "solve_only.json", record)
        return {"run_dir": str(run_dir), "status": "planned"}

    _ensure_osis_runtime_backend(parent_repo)
    adapter = PyOSISAdapter(
        execution_enabled=True,
        parent_repo=parent_repo,
        python_executable=resolve_python_executable(parent_repo),
        timeout_s=timeout_s,
    )
    backend = adapter.execute(candidate, run_dir, solve=True, timeout_s=timeout_s)
    # ``PyOSISAdapter`` now preserves this on every early return.  Keep the
    # post-processing command defensive for older adapters or test doubles:
    # this invocation explicitly requested the solve gate, so a build failure
    # that prevents entering ``engine.solve()`` must be scored the same way as
    # a non-converged solve.
    backend = dict(backend or {})
    backend.setdefault("solve_requested", True)
    backend.setdefault("solver_converged", False)
    backend.setdefault("solve_status", "not_run")
    _write_json(run_dir / "backend_status.json", backend)
    runtime = evaluate_runtime_snapshot(
        run_dir / "runtime_measurements.json",
        run_dir,
        bridge_type=str(task.get("bridge_type") or "unknown"),
        is_continuous=(task.get("metadata") or {}).get("is_continuous"),
        is_prestressed=(task.get("metadata") or {}).get("is_prestressed"),
        parent_repo=parent_repo,
    )

    ref_score = _read_json(run_dir / "reference_score.json") or None
    compile_result = _read_json(run_dir / "compile.json")
    gate = bool(compile_result.get("passed")) and bool(backend.get("model_created"))
    evaluation = build_official_evaluation(
        run_dir=run_dir,
        ref_score=ref_score,
        model_conformance_gate=gate,
    )
    overwrite_evaluation(run_dir, evaluation)
    record.update({
        "status": "solved" if backend.get("status") == "succeeded" else "failed",
        "finished_at": time.time(),
        "backend_status": backend,
        "runtime_status": runtime.get("status"),
        "quality_score": evaluation.quality_score,
        "quality_score_before_gate": evaluation.quality_score_before_gate,
        "complete_success": evaluation.complete_success,
        "failure_reasons": list(evaluation.failure_reasons),
    })
    _write_json(run_dir / "solve_only.json", record)
    return {
        "run_dir": str(run_dir),
        "status": record["status"],
        "solver_converged": bool(backend.get("solver_converged")),
        "quality_score": evaluation.quality_score,
        "quality_score_before_gate": evaluation.quality_score_before_gate,
        "complete_success": evaluation.complete_success,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--run-dir", action="append", default=[], type=Path,
                        help="solve only these run directories (repeatable)")
    parser.add_argument("--parent-repo", type=Path, default=None)
    parser.add_argument("--force", action="store_true", help="solve runs even when solve_requested is already true")
    parser.add_argument("--dry-run", action="store_true", help="write planned solve_only.json without starting PyOSIS")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args(argv)
    resolve_args_parent_repo(args)
    if args.parent_repo is None:
        parser.error("--parent-repo or OSIS_PARENT_REPO is required")
    if args.runs_dir is None and not args.run_dir:
        parser.error("give --runs-dir or at least one --run-dir")

    if args.run_dir:
        targets = [Path(path).expanduser().resolve() for path in args.run_dir]
    else:
        targets = iter_run_dirs(args.runs_dir)
    if args.limit is not None:
        targets = targets[:max(0, args.limit)]
    results = [solve_one(path, args.parent_repo, force=args.force, dry_run=args.dry_run) for path in targets]
    print(json.dumps({"count": len(results), "results": results}, ensure_ascii=False, indent=2))
    return 0 if all(item.get("status") not in {"failed"} for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
