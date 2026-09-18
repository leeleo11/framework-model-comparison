"""Run one canonical modeling-line experiment.

The framework executor supplies a candidate project.  With
``--execute-pyosis`` the comparison runner then builds it through PyOSIS and
calls the parent repository's model-conformance CLI; without that flag the
run is an explicit dry-run and cannot be a complete success.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.adapters import ADAPTER_SPECS
from common.paths import resolve_args_parent_repo
from common.runner import ExperimentRunner
from common.task_schema import load_task


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one OSIS bridge-modeling comparison trial")
    parser.add_argument("--skills-dir", type=Path, required=True)
    parser.add_argument("--task", type=Path, required=True)
    parser.add_argument("--candidate-project", type=Path, required=True)
    parser.add_argument("--architecture", choices=tuple(ADAPTER_SPECS), required=True)
    parser.add_argument("--reference-project", type=Path, default=None)
    parser.add_argument("--parent-repo", type=Path, default=None)
    parser.add_argument("--runs-dir", type=Path, default=Path("runs/modeling"))
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument(
        "--execute-pyosis",
        action="store_true",
        help="run the generated project through PyOSIS before scoring",
    )
    parser.add_argument(
        "--solve-gate",
        action="store_true",
        help="also run engine.solve(); convergence is recorded as an optional gate",
    )
    parser.add_argument(
        "--pyosis-python",
        type=Path,
        default=None,
        help="Python executable containing pyosis (defaults to parent .venv)",
    )
    parser.add_argument(
        "--total-timeout-s",
        type=float,
        default=None,
        help="override the task's total timeout for this trial",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolve_args_parent_repo(args)
    task = load_task(args.task)
    runner = ExperimentRunner(
        skills_dir=args.skills_dir,
        runs_dir=args.runs_dir,
        parent_repo=args.parent_repo,
        reference_project=args.reference_project,
        pyosis_enabled=args.execute_pyosis,
        solve_gate=args.solve_gate,
        pyosis_python=args.pyosis_python,
        total_timeout_override_s=args.total_timeout_s,
    )
    summary = runner.run(
        task,
        architecture_id=args.architecture,
        seed=args.seed,
        candidate_project=args.candidate_project,
    )
    print(
        json.dumps(
            {
                "architecture_id": args.architecture,
                "status": summary.status,
                "complete_success": summary.evaluation.complete_success,
                "quality_score": summary.evaluation.quality_score,
                "failure_reasons": summary.evaluation.failure_reasons,
                "run_dir": str(summary.run_dir),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
