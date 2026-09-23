"""Run a full benchmark campaign: matrix × phases × parallel × archive × xlsx.

Wraps ``run_all_parallel.run_one`` with the operational plumbing a long
campaign needs:

- matrix expansion over architectures × bridges × forms × seeds;
- optional phase scheduling: full → gen → edit, with a hard gate between phases;
- resumable runs: an existing terminal result is kept and skipped;
- stale-target handling: an existing run dir is moved to the archive (never
  deleted — every attempt stays auditable);
- post-scoring slimming: per-run bulk that is reproducible from the snapshot
  (the T6 sandbox skill copy, ``__pycache__``, native mesh artifacts) is
  removed once scoring is done; audit-critical files (candidate project,
  session log/db, all scoring JSONs, manifest) are kept;
- a free-disk guard that stops the campaign before the volume fills;
- a final summary JSON plus the Excel export used by the paper.

Usage::

    WEKNORA_* OSIS_MODEL_API_KEY set in env; then:

    python scripts/run_campaign.py --label v4 \\
        --seeds 0 1 2 3 4 \\
        --bridges osis-bridge-cantilever-box \\
        --auto-forms --resume \\
        --jobs 3
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from common.paths import resolve_args_parent_repo  # noqa: E402
from common.run_layout import candidate_run_dirs, lookup_source, run_dir_for  # noqa: E402
from scripts.run_all_parallel import BASE_AI_PORT, ARCHITECTURES, build_command  # noqa: E402


FORM_ORDER = ("full", "gen", "edit")


def run_is_terminal(run_dir: Path) -> bool:
    """Whether a run has a final auditable result.

    ``evaluation.json`` is written for both successful and model-behaviour
    failures.  A missing evaluation means the process was interrupted and the
    run must be resumed.  ``manifest.json`` is required so a hand-created or
    partially copied evaluation cannot open the next phase gate.
    """

    return (run_dir / "evaluation.json").is_file() and (run_dir / "manifest.json").is_file()


def cell_is_terminal(
    runs_dir: Path,
    bridge: str,
    form: str,
    index: int,
    architecture: str,
    seed: int,
    source: str = "",
) -> bool:
    return any(
        run_is_terminal(path)
        for path in candidate_run_dirs(
            runs_dir, bridge, form, index, architecture, seed, source=source
        )
    )


def _dir_mb(path: Path) -> float:
    if not path.is_dir():
        return 0.0
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file()) / 1048576


def archive_stale(target: Path, archive_dir: Path) -> str | None:
    """Move an existing run dir into the archive; return where it went."""

    if not target.is_dir():
        return None
    archive_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%H%M%S")
    destination = archive_dir / f"{target.name}--{stamp}"
    shutil.move(str(target), str(destination))
    return str(destination)


def archive_stale_cell(
    runs_dir: Path,
    bridge: str,
    form: str,
    index: int,
    architecture: str,
    seed: int,
    archive_dir: Path,
    source: str = "",
) -> list[str]:
    moved: list[str] = []
    for target in candidate_run_dirs(
        runs_dir, bridge, form, index, architecture, seed, source=source
    ):
        archived = archive_stale(target, archive_dir)
        if archived:
            moved.append(archived)
    return moved


_PROJECT_DIR_NAMES = (
    "candidate_project",
    "osis_project",
    "secmesh",
    "Model",
    "Result",
    "Break",
    "Check",
    "Error",
    "MutiCase",
    "Temperary",
    "image",
)


def _drop_path(path: Path) -> float:
    if not path.exists():
        return 0.0
    before = path.stat().st_size / 1048576 if path.is_file() else _dir_mb(path)
    if path.is_dir():
        shutil.rmtree(path, ignore_errors=True)
    else:
        path.unlink(missing_ok=True)
    return before


def slim_run_dir(run_dir: Path) -> dict[str, float]:
    """Drop project files after scoring. Keep scores, traces, and T6 session logs.

    Removed once the cell has been scored:
    - candidate project, reference project, and ``*.sis``
    - OSIS native trees (``osis_project``, meshes, Model/Result, and the rest)
    - the T6 sandbox skill-snapshot copy
    - ``__pycache__``

    Kept: evaluation and other scoring JSON, manifest, frozen config, traces,
    and the T6 ``xdg-data`` session log.
    """

    removed = {"sandbox": 0.0, "pycache": 0.0, "projects": 0.0}
    gen = run_dir / "generated"
    sandbox = gen / ".osisai_t6"
    if sandbox.is_dir():
        before = _dir_mb(sandbox)
        for child in list(sandbox.iterdir()):
            if child.name == "xdg-data":
                continue
            if child.is_dir():
                shutil.rmtree(child, ignore_errors=True)
            else:
                child.unlink(missing_ok=True)
        removed["sandbox"] = round(before - _dir_mb(sandbox), 1)
    for cache in list(run_dir.rglob("__pycache__")):
        removed["pycache"] += round(_drop_path(cache), 1)
    for name in _PROJECT_DIR_NAMES:
        removed["projects"] += _drop_path(run_dir / name)
        removed["projects"] += _drop_path(gen / name)
    removed["projects"] += _drop_path(run_dir / "scorer_private" / "reference_project")
    for sis in list(run_dir.glob("*.sis")) + list(gen.glob("*.sis")):
        removed["projects"] += _drop_path(sis)
    removed["projects"] = round(removed["projects"], 1)
    return removed


def export_xlsx(runs_dir: Path, output: Path) -> str | None:
    script = PROJECT_ROOT / "scripts" / "export_results_xlsx.py"
    if not script.is_file():
        return None
    output.parent.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        [sys.executable, str(script), "--runs-dir", str(runs_dir), "--output", str(output)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if proc.returncode != 0:
        return (proc.stderr or proc.stdout or "")[-300:]
    return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", required=True, help="campaign name; runs land in runs/<label>/")
    parser.add_argument("--seeds", type=int, nargs="+", default=[0])
    parser.add_argument("--bridges", nargs="+", default=["osis-bridge-cantilever-box"])
    parser.add_argument("--forms", nargs="+", default=["full"])
    parser.add_argument(
        "--auto-forms", action="store_true",
        help="run the three gated phases in order: full, gen, edit",
    )
    parser.add_argument(
        "--resume", action="store_true",
        help="skip runs that already have evaluation.json and manifest.json",
    )
    parser.add_argument("--architectures", nargs="+", default=list(ARCHITECTURES))
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--jobs", type=int, default=3)
    parser.add_argument("--model", default="deepseek-v4.1-flash-expires-on-0910")
    parser.add_argument("--base-url", default="http://47.92.150.231/v1")
    parser.add_argument("--temperature", type=float, default=0.0)
    parser.add_argument("--reasoning-effort", choices=("low", "medium", "high"), default=None)
    parser.add_argument("--archive-root", type=Path, default=PROJECT_ROOT / "runs" / "archive")
    parser.add_argument("--parent-repo", type=Path, default=None,
                        help="OSIS skill repository for every child run "
                             "(default: $OSIS_PARENT_REPO, else configs/parent_repo.txt)")
    parser.add_argument("--no-xlsx", action="store_true")
    args = parser.parse_args(argv)
    resolve_args_parent_repo(args)

    if not (os.environ.get("OSIS_MODEL_API_KEY") or os.environ.get("OSIS_API_KEY")):
        print(json.dumps({"error": "OSIS_MODEL_API_KEY is not set"}), file=sys.stderr)
        return 2

    runs_dir = PROJECT_ROOT / "runs" / args.label
    runs_dir.mkdir(parents=True, exist_ok=True)
    stale_archive = args.archive_root / f"{args.label}-stale-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
    source_cache: dict[tuple[str, str, int], str] = {}

    def _source(bridge: str, form: str, index: int) -> str:
        key = (bridge, form, index)
        if key not in source_cache:
            source_cache[key] = lookup_source(args.parent_repo, bridge, form, index)
        return source_cache[key]

    results: list[dict] = []
    started = time.monotonic()

    def launch(arch: str, bridge: str, form: str, seed: int, slot: int) -> dict:
        from scripts.run_all_parallel import run_one  # reuse the hardened runner

        class NS:  # minimal namespace shaped like run_all_parallel's args
            pass
        ns = NS()
        ns.t6_port = BASE_AI_PORT + 50 + slot  # unique per in-flight task
        ns.runs_dir = runs_dir
        ns.bridge = bridge
        ns.form = form
        ns.index = args.index
        ns.seed = seed
        ns.parent_repo = args.parent_repo
        ns.model = args.model
        ns.base_url = args.base_url
        ns.temperature = args.temperature
        ns.reasoning_effort = args.reasoning_effort
        ns.log_dir = PROJECT_ROOT / "tmp" / f"campaign-{args.label}" / "logs"
        archive_stale_cell(
            runs_dir, bridge, form, args.index, arch, seed, stale_archive,
            source=_source(bridge, form, args.index),
        )
        outcome = run_one(ns, arch)
        run_dir = run_dir_for(
            runs_dir, bridge, form, args.index, arch, seed,
            source=_source(bridge, form, args.index),
        )
        slim = {}
        try:
            # failed runs hold the same reproducible bulk — slim them too
            slim = slim_run_dir(run_dir)
        except Exception as exc:  # noqa: BLE001 - slimming must never fail a run
            slim = {"error": str(exc)}
        outcome.update({"slimmed_mb": slim})
        return outcome

    def _phase_matrix(form: str) -> list[tuple[str, str, str, int]]:
        return [
            (arch, bridge, form, seed)
            for seed in args.seeds
            for bridge in args.bridges
            for arch in args.architectures
        ]

    def _run_phase(form: str, phase_number: int) -> dict:
        matrix = _phase_matrix(form)
        pending: list[tuple[str, str, str, int]] = []
        skipped = 0
        for arch, bridge, phase_form, seed in matrix:
            target = run_dir_for(
                runs_dir, bridge, phase_form, args.index, arch, seed,
                source=_source(bridge, phase_form, args.index),
            )
            if args.resume and cell_is_terminal(
                runs_dir, bridge, phase_form, args.index, arch, seed,
                source=_source(bridge, phase_form, args.index),
            ):
                skipped += 1
                outcome = {
                    "architecture": arch,
                    "bridge": bridge,
                    "form": phase_form,
                    "seed": seed,
                    "skipped": True,
                    "run_dir": str(target),
                }
                results.append(outcome)
                print(json.dumps({"skipped": outcome}, ensure_ascii=False), flush=True)
            else:
                pending.append((arch, bridge, phase_form, seed))

        print(json.dumps({
            "phase": phase_form,
            "phase_number": phase_number,
            "expected": len(matrix),
            "pending": len(pending),
            "skipped_terminal": skipped,
        }, ensure_ascii=False), flush=True)

        # ---- two-phase scheduling (T6 exclusivity) ----------------------
        # The OSIS engine is a process-wide SINGLETON: ``project.create``
        # switches the current project for every client.  P4 execution
        # serialises on the runtime lock, but T6's native session runs inside
        # its own sandbox, which no lock can cover.  Phase 1 runs every
        # NON-T6 architecture in parallel; phase 2 runs T6 strictly serially.
        t6_tasks = [t for t in pending if t[0] == "T6"]
        other_tasks = [t for t in pending if t[0] != "T6"]
        print(json.dumps({
            "phase": phase_form,
            "phase1_parallel": len(other_tasks),
            "phase2_t6_serial": len(t6_tasks),
        }), flush=True)

        def _record(outcome: dict) -> None:
            results.append(outcome)
            print(json.dumps({"finished": outcome}, ensure_ascii=False), flush=True)

        if other_tasks:
            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
                futures = {}
                for slot, (arch, bridge, phase_form, seed) in enumerate(other_tasks):
                    futures[pool.submit(launch, arch, bridge, phase_form, seed, slot)] = (
                        arch, bridge, phase_form, seed)
                for future in concurrent.futures.as_completed(futures):
                    key = futures[future]
                    try:
                        outcome = future.result()
                    except Exception as exc:  # noqa: BLE001
                        outcome = {
                            "architecture": key[0],
                            "bridge": key[1],
                            "form": key[2],
                            "seed": key[3],
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    _record(outcome)

        for slot, (arch, bridge, phase_form, seed) in enumerate(t6_tasks):
            key = (arch, bridge, phase_form, seed)
            try:
                outcome = launch(arch, bridge, phase_form, seed, len(other_tasks) + slot)
            except Exception as exc:  # noqa: BLE001
                outcome = {
                    "architecture": arch,
                    "bridge": bridge,
                    "form": phase_form,
                    "seed": seed,
                    "error": f"{type(exc).__name__}: {exc}",
                }
            _record(outcome)

        # A phase is allowed to finish with model failures, but not with
        # interrupted runs.  This is the gate that prevents gen/edit from
        # starting while full (or the preceding phase) is incomplete.
        incomplete = [
            str(run_dir_for(
                runs_dir, arch_bridge, phase_form, args.index, arch, seed,
                source=_source(arch_bridge, phase_form, args.index),
            ))
            for arch, arch_bridge, phase_form, seed in matrix
            if not cell_is_terminal(
                runs_dir, arch_bridge, phase_form, args.index, arch, seed,
                source=_source(arch_bridge, phase_form, args.index),
            )
        ]
        phase_summary = {
            "campaign": args.label,
            "phase": phase_form,
            "expected": len(matrix),
            "terminal": len(matrix) - len(incomplete),
            "incomplete": incomplete,
            "elapsed_s": round(time.monotonic() - started, 1),
        }
        (runs_dir / f"phase_{phase_form}_summary.json").write_text(
            json.dumps(phase_summary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        if incomplete:
            raise RuntimeError(
                f"phase {phase_form} is incomplete; refusing to start the next phase: "
                + ", ".join(incomplete[:5])
            )
        return phase_summary

    forms = list(FORM_ORDER if args.auto_forms else args.forms)
    invalid = [form for form in forms if form not in FORM_ORDER]
    if invalid:
        parser.error(f"invalid form(s): {', '.join(invalid)}")
    if args.auto_forms and tuple(forms) != FORM_ORDER:
        parser.error("--auto-forms must use the fixed order full gen edit")

    print(json.dumps({
        "campaign": args.label,
        "phases": forms,
        "runs_per_phase": len(args.seeds) * len(args.bridges) * len(args.architectures),
        "resume": args.resume,
    }, ensure_ascii=False), flush=True)
    phase_summaries: list[dict] = []
    for phase_number, form in enumerate(forms, start=1):
        phase_summaries.append(_run_phase(form, phase_number))

    summary = {
        "campaign": args.label,
        "phases": phase_summaries,
        "total_elapsed_s": round(time.monotonic() - started, 1),
        "results": results,
    }
    (runs_dir / "campaign_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    xlsx_error = None
    if not args.no_xlsx:
        xlsx_error = export_xlsx(
            runs_dir, PROJECT_ROOT / "reports" / f"{args.label}.xlsx")

    print(json.dumps({"summary": str(runs_dir / "campaign_summary.json"),
                      "xlsx_error": xlsx_error}, ensure_ascii=False))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
