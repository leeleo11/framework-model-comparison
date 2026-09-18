"""Materialize the embedded comparison source as a safe sibling workspace.

The embedded copy lives inside the OSIS source repository for version control,
but formal experiments must run from a sibling directory so native T6 tools
cannot discover the source repository's raw skill tree through ancestor search.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path


EMBEDDED_ROOT = Path(__file__).resolve().parents[1]
PARENT_REPO = EMBEDDED_ROOT.parents[1]
DEFAULT_OUTPUT = PARENT_REPO.parent / "osis-framework-comparison-runtime"


def export_workspace(output: Path, *, replace: bool = False) -> Path:
    output = Path(output).expanduser().resolve()
    if output.exists():
        if not replace:
            raise FileExistsError(
                f"output already exists: {output}; pass --replace to recreate it"
            )
        shutil.rmtree(output)
    ignore = shutil.ignore_patterns(
        ".git", ".venv", ".venvs", "runs", "tmp", "reports",
        "node_modules", ".pytest_cache", "__pycache__", "*.pyc",
        "parent_repo.local.txt",
    )
    shutil.copytree(EMBEDDED_ROOT, output, ignore=ignore)
    local_config = output / "configs" / "parent_repo.local.txt"
    local_config.parent.mkdir(parents=True, exist_ok=True)
    local_config.write_text(str(PARENT_REPO) + "\n", encoding="utf-8")
    return output


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    output = export_workspace(args.output, replace=args.replace)
    print(f"exported comparison workspace: {output}")
    print(f"parent repo config: {output / 'configs' / 'parent_repo.local.txt'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
