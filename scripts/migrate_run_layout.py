"""Move leftover flat run directories into architecture/bridge/form folders,
then rename leftover source-named nested leaves back to the opaque cell name.

Skips a cell that is still being written (no evaluation.json, recently
modified) so an in-flight rerun is not yanked out from under the worker.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.run_layout import (
    CELL_NAME_RE,
    iter_cell_dirs,
    migrate_flat_run,
    migrate_source_cell_to_legacy,
    nested_run_dir,
    parse_cell_name,
)

ACTIVE_GRACE_S = 120.0


def _is_active(path: Path) -> bool:
    if (path / "evaluation.json").is_file() and (path / "manifest.json").is_file():
        return False
    try:
        age = time.time() - path.stat().st_mtime
    except OSError:
        return True
    return age < ACTIVE_GRACE_S


def _record_move(
    path: Path,
    dest: Path,
    *,
    dry_run: bool,
    moved: list[dict[str, str]],
    skipped: list[dict[str, str]],
) -> None:
    if dest.resolve() == path.resolve():
        return
    if dest.exists():
        skipped.append({"src": str(path), "dest": str(dest), "reason": "dest_exists"})
        return
    if dry_run:
        moved.append({"src": str(path), "dest": str(dest), "dry_run": "true"})
        return
    moved.append({"src": str(path), "dest": str(dest)})


def migrate_root(runs_dir: Path, *, dry_run: bool = False) -> dict:
    root = Path(runs_dir).expanduser().resolve()
    moved: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    if not root.is_dir():
        return {"root": str(root), "moved": moved, "skipped": skipped}

    leftover = [
        path for path in sorted(root.iterdir(), key=lambda item: item.name)
        if path.is_dir() and CELL_NAME_RE.match(path.name)
    ]
    for path in leftover:
        parsed = parse_cell_name(path.name)
        if parsed is None:
            continue
        dest = nested_run_dir(
            root,
            parsed["bridge"],
            parsed["form"],
            parsed["index"],
            parsed["architecture"],
            parsed["seed"],
        )
        if _is_active(path):
            skipped.append({"src": str(path), "reason": "in_flight"})
            continue
        _record_move(path, dest, dry_run=dry_run, moved=moved, skipped=skipped)
        if dest.exists() and dest.resolve() != path.resolve():
            continue
        if not dry_run:
            migrate_flat_run(path, root)

    source_named = [
        path for path in iter_cell_dirs(root)
        if parse_cell_name(path.name) is None
    ]
    for path in source_named:
        dest = migrate_source_cell_to_legacy(path, root, dry_run=True)
        if dest is None or dest.resolve() == path.resolve():
            continue
        if _is_active(path):
            skipped.append({"src": str(path), "reason": "in_flight"})
            continue
        _record_move(path, dest, dry_run=dry_run, moved=moved, skipped=skipped)
        if dest.exists() and dest.resolve() != path.resolve():
            continue
        if not dry_run:
            migrate_source_cell_to_legacy(path, root)
    return {"root": str(root), "moved": moved, "skipped": skipped}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs-dir", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    summary = migrate_root(args.runs_dir, dry_run=args.dry_run)
    print(json.dumps({
        "root": summary["root"],
        "moved": len(summary["moved"]),
        "skipped": len(summary["skipped"]),
        "dry_run": args.dry_run,
        "details": summary,
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
