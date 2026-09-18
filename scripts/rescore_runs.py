"""Re-score existing runs with the current scorer (no regeneration).

Regeneration costs ~1.5 h for six architectures; the generation artifacts are
unchanged when a scoring defect is fixed, so the runs only need their
``reference_score.json`` and ``evaluation.json`` rebuilt.  This replay keeps
the frozen configuration and the leakage guard untouched — it re-reads the
candidate the framework already produced.

Usage::

    python scripts/rescore_runs.py --runs-dir "runs\\diagnostic\\retest-dsv41-20260910"
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.official_evaluation import (  # noqa: E402
    build_official_evaluation,
    overwrite_evaluation,
)
from common.paths import resolve_args_parent_repo  # noqa: E402
from common.pyosis_adapter import resolve_python_executable  # noqa: E402
from common.reference_scorer import score as reference_score  # noqa: E402
from scripts.run_dataset import _runtime_stats  # noqa: E402


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _architecture_id(run_dir: Path) -> str:
    """Recover the architecture id from the canonical run-dir name."""

    parts = run_dir.name.split("__")
    return parts[3] if len(parts) > 3 else ""


def rescore_one(run_dir: Path, parent_repo: Path) -> dict:
    run_dir = run_dir.resolve()
    candidate_root = run_dir / "candidate_project"
    reference_root = run_dir / "scorer_private" / "reference_project"
    if not candidate_root.is_dir() or not reference_root.is_dir():
        return {"run_dir": str(run_dir), "status": "skipped",
                "reason": "candidate or reference project missing"}

    task = _read_json(run_dir / "input.json")
    bridge_type = task.get("bridge_type")
    metadata = task.get("metadata") or {}
    ref_score = reference_score(
        reference_root=reference_root,
        candidate_root=candidate_root,
        parent_repo=parent_repo,
        run_dir=run_dir,
        bridge_type=bridge_type,
        is_continuous=metadata.get("is_continuous") if isinstance(metadata, dict) else None,
        runtime_stats=_runtime_stats(run_dir, _architecture_id(run_dir)),
        python_executable=resolve_python_executable(parent_repo),
    )

    backend = _read_json(run_dir / "backend_status.json")
    compile_result = _read_json(run_dir / "compile.json")
    gate = bool(compile_result.get("passed", False)) and bool(backend.get("model_created", False))
    official = build_official_evaluation(
        run_dir=run_dir, ref_score=ref_score, model_conformance_gate=gate
    )
    overwrite_evaluation(run_dir, official)
    return {
        "run_dir": str(run_dir),
        "status": "rescored",
        "model_conformance": (ref_score or {}).get("model_conformance_score"),
        "quality_score": official.quality_score,
        "complete_success": official.complete_success,
        "failure_reasons": list(official.failure_reasons),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--parent-repo", type=Path, default=None)
    args = parser.parse_args()
    resolve_args_parent_repo(args)

    runs_dir = args.runs_dir.resolve()
    targets = [p for p in sorted(runs_dir.iterdir()) if p.is_dir()]
    results = [rescore_one(p, args.parent_repo) for p in targets]
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
