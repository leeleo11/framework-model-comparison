"""Serve a headless OSIS engine backend for the comparison harness.

pyosis proxies every engine operation (project creation, candidate builds,
model-state probes) to an HTTP backend exposing ``/OSIS_Run``.  Production
uses the OSIS desktop (``Osis.exe``, port 18080); unattended experiment runs
cannot assume the desktop, so this script starts the same engine headless
from ``PySolver.dll`` and holds it in the foreground.

Usage (parent-repo venv, which ships pyosis)::

    python scripts/serve_headless_osis.py [--port 18080] [--install <Rbin64-dir>]

Candidate install roots are probed in order: ``--install`` / ``OSIS_SOLVER_INSTALL`` /
``OSIS_SOLVER_INSTALL2``, then ``Rbin64*`` siblings of the comparison project.
A DLL that exists but cannot load (dependency
set missing from that install) is skipped, not fatal.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _load_solver(install: Path, port: int):
    os.add_dll_directory(str(install))
    os.environ["PATH"] = str(install) + os.pathsep + os.environ.get("PATH", "")
    from pyosis.core.solver import OSISSolver

    return OSISSolver(str(install), port=port)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=18080)
    parser.add_argument("--install", type=Path, default=None)
    args = parser.parse_args()

    installs: list[Path] = []
    if args.install:
        installs.append(args.install)
    for var in ("OSIS_SOLVER_INSTALL", "OSIS_SOLVER_INSTALL2"):
        if os.environ.get(var):
            installs.append(Path(os.environ[var]))
    # Packaged installs are discovered relative to the comparison project,
    # never from a user-specific drive or workspace name.
    packaging_root = PROJECT_ROOT.parent
    installs.extend(sorted(packaging_root.glob("Rbin64*")))
    # Also support launching the script from a separate checkout where the
    # deployment directory is a sibling of the current working directory.
    cwd_root = Path.cwd()
    if cwd_root != packaging_root:
        installs.extend(sorted(cwd_root.glob("Rbin64*")))

    # Keep order stable while removing duplicates.
    installs = list(dict.fromkeys(p.resolve() for p in installs))

    solver = None
    errors: list[str] = []
    for install in installs:
        if not (install / "PySolver.dll").is_file():
            continue
        try:
            solver = _load_solver(install, args.port)
            print(f"[serve_headless_osis] loaded PySolver.dll from {install}", flush=True)
            break
        except Exception as exc:  # noqa: BLE001 - try the next install
            errors.append(f"{install.name}: {exc}")
    if solver is None:
        print(
            "[serve_headless_osis] no loadable PySolver.dll found: "
            + "; ".join(errors or ["no candidate ships one"]),
            file=sys.stderr,
            flush=True,
        )
        return 1

    print(
        f"[serve_headless_osis] OSIS engine serving {solver.url} "
        "(Ctrl+C to stop; the server dies with this process)",
        flush=True,
    )
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        print("[serve_headless_osis] bye", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
