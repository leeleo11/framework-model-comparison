"""Refresh official scores for runs that were executed with the solve gate.

The command does not call PyOSIS or the model.  It repairs/normalizes the
solve intent in existing artifacts, rebuilds ``evaluation.json`` from the
already stored ``reference_score.json``, and refreshes the manifest hashes.
It is useful for older runs whose model build failed before the adapter wrote
``solve_requested`` to ``backend_status.json``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.official_evaluation import build_official_evaluation, overwrite_evaluation
from scripts.run_dataset import _refresh_manifest


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def iter_run_dirs(runs_dir: Path, *, all_runs: bool = False) -> list[Path]:
    root = Path(runs_dir).expanduser().resolve()
    if not root.is_dir():
        raise FileNotFoundError(root)
    if all_runs:
        return sorted(
            path for path in root.iterdir()
            if path.is_dir()
            and any((path / name).is_file() for name in
                    ("evaluation.json", "backend_status.json", "solve_only.json"))
        )
    return sorted(
        path for path in root.iterdir()
        if path.is_dir() and (path / "solve_only.json").is_file()
    )


def refresh_one(run_dir: Path) -> dict[str, Any]:
    run_dir = Path(run_dir).resolve()
    solve_record = _read_json(run_dir / "solve_only.json")
    solve_was_requested = bool(solve_record.get("solve_requested", False))

    backend = _read_json(run_dir / "backend_status.json")
    nested = solve_record.get("backend_status")
    nested = nested if isinstance(nested, dict) else {}
    backend = dict(backend)
    if solve_was_requested or backend.get("solve_requested"):
        backend["solve_requested"] = True
        backend["solver_converged"] = bool(
            backend.get("solver_converged", nested.get("solver_converged", False))
        )
        backend.setdefault(
            "solve_status",
            "succeeded" if backend["solver_converged"] else "not_run",
        )
        _write_json(run_dir / "backend_status.json", backend)
        if (run_dir / "build_status.json").is_file():
            _write_json(run_dir / "build_status.json", backend)

    reference_score = _read_json(run_dir / "reference_score.json") or None
    compile_result = _read_json(run_dir / "compile.json")
    gate = bool(compile_result.get("passed", False)) and bool(backend.get("model_created", False))
    official = build_official_evaluation(
        run_dir=run_dir,
        ref_score=reference_score,
        model_conformance_gate=gate,
    )
    overwrite_evaluation(run_dir, official)

    solve_record.update(
        {
            "backend_status": backend,
            "quality_score": official.quality_score,
            "quality_score_before_gate": official.quality_score_before_gate,
            "complete_success": official.complete_success,
            "failure_reasons": list(official.failure_reasons),
        }
    )
    _write_json(run_dir / "solve_only.json", solve_record)
    _refresh_manifest(run_dir)
    return {
        "run_dir": str(run_dir),
        "status": "refreshed",
        "solver_converged": backend["solver_converged"],
        "quality_score": official.quality_score,
        "quality_score_before_gate": official.quality_score_before_gate,
        "complete_success": official.complete_success,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, action="append", required=True)
    parser.add_argument("--all-runs", action="store_true",
                        help="refresh every run with evaluation/backend artifacts, not only solve-only runs")
    args = parser.parse_args(argv)
    results: list[dict[str, Any]] = []
    for root in args.runs_dir:
        results.extend(refresh_one(path) for path in iter_run_dirs(root, all_runs=args.all_runs))
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
