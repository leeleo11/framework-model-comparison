"""Run all six registered adapters for a metadata or modeling smoke test."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow direct execution as `python scripts/run_conformance.py`.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.adapters import ADAPTER_SPECS
from common.paths import resolve_args_parent_repo
from common.runner import ExperimentRunner
from common.task_schema import load_task


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skills-dir", type=Path, required=True)
    parser.add_argument(
        "--task",
        type=Path,
        default=Path("tasks/dev/bridge_whole_l1.json"),
    )
    parser.add_argument(
        "--runs-dir",
        type=Path,
        default=Path("runs/conformance"),
    )
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--candidate-project", type=Path, default=None)
    parser.add_argument("--parent-repo", type=Path, default=None)
    parser.add_argument("--execute-pyosis", action="store_true")
    parser.add_argument("--solve-gate", action="store_true")
    args = parser.parse_args()
    # A metadata-only adapter smoke test does not need the OSIS parent repo.
    # Resolve it only for the execution path that actually imports PyOSIS or
    # the parent-repo scorer; this keeps the CLI portable for a clean checkout.
    if args.parent_repo is not None or args.execute_pyosis:
        resolve_args_parent_repo(args)

    task = load_task(args.task)
    runner = ExperimentRunner(
        skills_dir=args.skills_dir,
        runs_dir=args.runs_dir,
        parent_repo=args.parent_repo,
        pyosis_enabled=args.execute_pyosis,
        solve_gate=args.solve_gate,
    )
    skills = runner.skill_adapter.list_skills()
    print(json.dumps({
        "skill_count": len(skills),
        "skills": [item.skill_id for item in skills],
        "skill_bundle_sha256": runner.skill_adapter.skill_bundle_hash(),
    }, ensure_ascii=False, indent=2))

    summaries = []
    for architecture_id in ADAPTER_SPECS:
        summary = runner.run(
            task,
            architecture_id=architecture_id,
            seed=args.seed,
            candidate_project=args.candidate_project,
        )
        summaries.append({
            "architecture_id": architecture_id,
            "status": summary.status,
            "complete_success": summary.evaluation.complete_success,
            "quality_score": summary.evaluation.quality_score,
            "run_dir": str(summary.run_dir),
        })
    print(json.dumps({"runs": summaries}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
