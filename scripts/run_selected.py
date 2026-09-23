"""Run exactly the runs you name -- and nothing else.

``run_campaign.py`` always expands to the full matrix, so re-running one or two
contaminated runs meant either re-running everything (wasteful, and it archives
results that were already fine) or invoking ``run_dataset.py`` by hand -- which
has no stale-directory handling and dies with ``FileExistsError`` when the
target directory is still there.  This is the missing middle.

Give it the runs you want; it archives any existing copy of each (never deletes)
and re-runs just those, strictly one at a time.

Usage::

    # everything that is not complete yet, across the whole sweep
    python scripts/run_selected.py --missing

    # specific runs (repeat the triple; --index/--seed default to 0)
    python scripts/run_selected.py \
        --arch T3 --bridge osis-bridge-cantilever-box --form edit \
        --arch T5 --bridge osis-bridge-cantilever-box --form edit

    # just show what would run
    python scripts/run_selected.py --missing --dry-run

Why serial: the model gateway sheds load with HTTP 503 once the host CPU passes
its own threshold ("system cpu overloaded"), and a run that loses that race does
zero model calls and scores nothing.  Running one at a time keeps the box quiet.
It also satisfies T6's hard constraint for free -- the OSIS engine is a
process-level singleton, so T6 must never overlap another run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.paths import resolve_args_parent_repo, resolve_parent_repo  # noqa: E402
from common.run_layout import candidate_run_dirs, lookup_source  # noqa: E402
from scripts.run_all_parallel import BASE_AI_PORT, ARCHITECTURES, run_one  # noqa: E402
from scripts.run_campaign import archive_stale_cell  # noqa: E402

BRIDGES = (
    "osis-bridge-cantilever-box",
    "osis-bridge-conventional-box",
    "osis-bridge-hollow-slab",
    "osis-bridge-precast-small-box",
    "osis-bridge-precast-t-girder",
    "osis-bridge-rigid-frame-box",
)
FORMS = ("full", "gen", "edit")


def _evaluation(run_dir: Path) -> dict:
    path = run_dir / "evaluation.json"
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def run_is_complete(run_dir: Path) -> bool:
    """Whether a run finished and its result may be kept as-is.

    A run is re-run when it has no ``manifest.json`` (killed mid-flight, or never
    started), or when it was recorded as an infrastructure failure -- a gateway
    503, a dropped connection, a protocol rejection.  Those say nothing about the
    architecture and must not be counted as capability results.
    """

    if not (run_dir / "manifest.json").is_file():
        return False
    evaluation = _evaluation(run_dir)
    if evaluation.get("infrastructure_failure"):
        return False
    return True


def missing_matrix(runs_dir: Path, seeds: list[int]) -> list[tuple[str, str, str, int]]:
    """Every matrix cell whose result is not complete."""

    try:
        parent = resolve_parent_repo()
    except Exception:
        parent = None
    pending: list[tuple[str, str, str, int]] = []
    for seed in seeds:
        for bridge in BRIDGES:
            for form in FORMS:
                source = lookup_source(parent, bridge, form, 0)
                for arch in ARCHITECTURES:
                    if not any(
                        run_is_complete(path)
                        for path in candidate_run_dirs(
                            runs_dir, bridge, form, 0, arch, seed, source=source
                        )
                    ):
                        pending.append((arch, bridge, form, seed))
    return pending


def _describe(arch: str, bridge: str, form: str, seed: int) -> str:
    return f"{arch} {bridge.removeprefix('osis-bridge-')} {form} seed{seed}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run exactly the named runs, one at a time.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--missing", action="store_true",
                        help="select every run in the sweep matrix that is not complete")
    parser.add_argument("--arch", action="append", default=[], choices=ARCHITECTURES,
                        help="architecture to run; pair each with --bridge/--form, repeatable")
    parser.add_argument("--bridge", action="append", default=[], choices=BRIDGES)
    parser.add_argument("--form", action="append", default=[], choices=FORMS)
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--seeds", type=int, nargs="+", default=[0])
    parser.add_argument("--parent-repo", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true",
                        help="list the runs that would execute, then stop")
    parser.add_argument("--archive-root", type=Path, default=PROJECT_ROOT / "runs" / "archive")
    args = parser.parse_args(argv)
    resolve_args_parent_repo(args)

    runs_dir = args.runs_dir if args.runs_dir.is_absolute() else PROJECT_ROOT / args.runs_dir

    if args.missing:
        selected = missing_matrix(runs_dir, args.seeds)
    else:
        if not args.arch:
            parser.error("give --missing, or at least one --arch/--bridge/--form triple")
        if not (len(args.arch) == len(args.bridge) == len(args.form)):
            parser.error("--arch, --bridge and --form must be repeated the same number of times")
        selected = [
            (arch, bridge, form, args.seeds[0])
            for arch, bridge, form in zip(args.arch, args.bridge, args.form)
        ]

    if not selected:
        print(json.dumps({"selected": 0, "note": "nothing to run"}, ensure_ascii=False))
        return 0

    print(json.dumps({"selected": len(selected), "dry_run": args.dry_run}, ensure_ascii=False))
    for arch, bridge, form, seed in selected:
        print(f"  {_describe(arch, bridge, form, seed)}")
    if args.dry_run:
        return 0

    if not (os.environ.get("OSIS_MODEL_API_KEY") or os.environ.get("OSIS_API_KEY")):
        print(json.dumps({"error": "OSIS_MODEL_API_KEY is not set"}), file=sys.stderr)
        return 2

    stale_root = args.archive_root / f"{runs_dir.name}-selected-{datetime.now():%Y%m%d-%H%M%S}"
    log_dir = PROJECT_ROOT / "tmp" / f"selected-{runs_dir.name}" / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    class NS:
        """Namespace shaped like ``run_all_parallel``'s parsed args."""

    results = []
    started = time.monotonic()
    for slot, (arch, bridge, form, seed) in enumerate(selected):
        source = lookup_source(args.parent_repo, bridge, form, args.index)
        archived = archive_stale_cell(
            runs_dir, bridge, form, args.index, arch, seed, stale_root,
            source=source,
        )
        if archived:
            print(json.dumps({"archived": archived}, ensure_ascii=False), flush=True)

        ns = NS()
        # A distinct port per run so a T6 run can never collide with a retry.
        ns.t6_port = BASE_AI_PORT + 100 + slot
        ns.runs_dir = runs_dir
        ns.bridge = bridge
        ns.form = form
        ns.index = args.index
        ns.seed = seed
        ns.parent_repo = args.parent_repo
        ns.log_dir = log_dir

        print(json.dumps({"starting": _describe(arch, bridge, form, seed)}, ensure_ascii=False),
              flush=True)
        try:
            outcome = run_one(ns, arch)
        except Exception as exc:  # noqa: BLE001 - one bad run must not stop the rest
            outcome = {"architecture": arch, "error": f"{type(exc).__name__}: {exc}"}
        outcome = {
            "architecture": arch, "bridge": bridge, "form": form, "seed": seed, **outcome,
        }
        results.append(outcome)
        print(json.dumps({"finished": outcome}, ensure_ascii=False), flush=True)

    summary = {
        "runs_dir": str(runs_dir),
        "archived_stale_to": str(stale_root),
        "selected": len(selected),
        "elapsed_s": round(time.monotonic() - started, 1),
        "results": results,
    }
    (PROJECT_ROOT / "tmp" / f"selected-{runs_dir.name}-summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps({"done": len(results),
                      "elapsed_s": summary["elapsed_s"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
