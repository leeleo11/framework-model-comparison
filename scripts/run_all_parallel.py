"""Launch the six architecture runs in parallel with a bounded worker pool.

Generation is per-run independent (separate venvs, separate workspaces) and
runs concurrently; the PyOSIS execution/scoring phase serialises itself on the
shared OSIS runtime lock (see ``common.pyosis_adapter.osis_runtime_lock``), so
the pool size only needs to bound model-gateway and CPU pressure.

The API key is read from the environment once and passed to each child, never
written to disk or printed.

Usage::

    OSIS_MODEL_API_KEY=<key> python scripts/run_all_parallel.py \
        --runs-dir runs/official-final-20260910 \
        --bridge osis-bridge-cantilever-box --form full --index 0 --seed 0 \
        --jobs 3
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.paths import resolve_args_parent_repo  # noqa: E402

ARCHITECTURES = ("T1", "T2", "T3", "T4", "T5", "T6")
# Give each architecture its own opencode AI port so two T6-style runs (or a
# retry alongside a live run) can never collide.
BASE_AI_PORT = 4100


def build_command(args: argparse.Namespace, architecture: str) -> list[str]:
    command = [
        sys.executable,
        str(PROJECT_ROOT / "scripts" / "run_dataset.py"),
        "--architecture", architecture,
        "--bridge", args.bridge,
        "--form", args.form,
        "--index", str(args.index),
        "--seed", str(args.seed),
        "--runs-dir", str(args.runs_dir),
    ]
    # Without this the child falls back to its own resolver, and a campaign
    # must read the same parent repo the campaign was launched against.
    parent_repo = getattr(args, "parent_repo", None)
    if parent_repo is not None:
        command += ["--parent-repo", str(parent_repo)]
    return command


def run_one(args: argparse.Namespace, architecture: str) -> dict:
    env = dict(os.environ)
    # Campaigns assign a unique port per task; solo runs fall back to the
    # architecture-derived default.
    port = getattr(args, "t6_port", None)
    env["T6_AI_PORT"] = str(port if port else BASE_AI_PORT + ARCHITECTURES.index(architecture))
    env.setdefault("PYTHONIOENCODING", "utf-8")
    log_path = Path(args.log_dir) / f"{architecture}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with log_path.open("w", encoding="utf-8") as handle:
        proc = subprocess.run(
            build_command(args, architecture),
            cwd=str(PROJECT_ROOT),
            env=env,
            stdout=handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
    return {
        "architecture": architecture,
        "returncode": proc.returncode,
        "elapsed_s": round(time.monotonic() - started, 1),
        "log": str(log_path),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--bridge", default="osis-bridge-cantilever-box")
    parser.add_argument("--form", default="full")
    parser.add_argument("--index", type=int, default=0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--jobs", type=int, default=3,
                        help="concurrent generations (execution self-serialises)")
    parser.add_argument("--architectures", nargs="*", default=list(ARCHITECTURES))
    parser.add_argument("--parent-repo", type=Path, default=None,
                        help="OSIS skill repository for every child run "
                             "(default: $OSIS_PARENT_REPO, else configs/parent_repo.txt)")
    parser.add_argument("--log-dir", type=Path, default=PROJECT_ROOT / "tmp" / "parallel-logs")
    args = parser.parse_args(argv)
    resolve_args_parent_repo(args)

    if not (os.environ.get("OSIS_MODEL_API_KEY") or os.environ.get("OSIS_API_KEY")):
        print(json.dumps({"error": "OSIS_MODEL_API_KEY is not set"}), file=sys.stderr)
        return 2

    started = time.monotonic()
    results: list[dict] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.jobs)) as pool:
        futures = {
            pool.submit(run_one, args, arch): arch for arch in args.architectures
        }
        for future in concurrent.futures.as_completed(futures):
            arch = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001 - report, keep the batch going
                result = {"architecture": arch, "returncode": -1,
                          "error": f"{type(exc).__name__}: {exc}"}
            results.append(result)
            print(json.dumps({"finished": result}, ensure_ascii=False), flush=True)

    print(json.dumps({"total_elapsed_s": round(time.monotonic() - started, 1),
                      "results": results}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
