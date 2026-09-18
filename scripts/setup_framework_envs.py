"""Set up per-framework Python environments for T1-T6.

Design: each external framework gets its own virtual environment under
``.venvs/<name>/`` so its dependency pins (crewai/openhands pin their own
pydantic/openai/httpx stack) can never disturb the working T2 LangGraph stack
in the system interpreter. Adapters launch these interpreters via subprocess -
the same pattern the PyOSIS adapter already uses for the parent repo's venv.

The result is recorded in ``.venvs/environments.json`` - the auditable
environment manifest for the paper (referenced from each run's
frozen_config.json).

Framework pins come from ``common/adapters.py`` (single source of truth):

    T1  direct one-shot      (no framework; system python + requests)
    T2  langgraph==1.2.11 / langchain==1.3.18   (system python, already installed)
    T3  smolagents==1.26.0
    T4  openhands-sdk==1.44.1
    T5  crewai==1.15.18
    T6  OSIS-AI native       (no pip framework; parent-repo tooling)

Usage::

    uv run python scripts/setup_framework_envs.py            # create venvs + install + verify
    uv run python scripts/setup_framework_envs.py --only t3  # one framework
    uv run python scripts/setup_framework_envs.py --dry-run  # show plan
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.paths import resolve_parent_repo  # noqa: E402

VENV_ROOT = PROJECT_ROOT / ".venvs"


@dataclass(frozen=True)
class FrameworkEnv:
    key: str
    packages: tuple[str, ...]
    import_name: str
    version_attr: str = "__version__"


FRAMEWORKS: dict[str, FrameworkEnv] = {
    "main": FrameworkEnv(
        "main",
        (
            "langgraph==1.2.11",
            "langchain==1.3.18",
            "openpyxl>=3.1",
            "requests>=2.31",
            "PyYAML>=6.0",
            "pytest>=8",
        ),
        "langgraph",
    ),
    "t3": FrameworkEnv("t3", ("smolagents==1.26.0", "openai"), "smolagents"),
    "t4": FrameworkEnv("t4", ("openhands-sdk==1.44.1",), "openhands"),
    "t5": FrameworkEnv("t5", ("crewai==1.15.18",), "crewai"),
}


def _pyvenv_python(venv_dir: Path) -> Path:
    windows = venv_dir / "Scripts" / "python.exe"
    if windows.is_file() or sys.platform == "win32":
        return windows
    return venv_dir / "bin" / "python"


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8", errors="replace", **kwargs)


def _uv() -> str:
    uv = shutil.which("uv")
    if not uv:
        raise RuntimeError("uv is required; install it from https://docs.astral.sh/uv/")
    return uv


def _probe_import(python: Path, import_name: str, version_attr: str) -> dict[str, str]:
    code = (
        f"import {import_name} as m; "
        f"print(getattr(m, {version_attr!r}, 'installed'))"
    )
    proc = _run([str(python), "-c", code])
    if proc.returncode != 0:
        return {"status": "import_failed", "detail": (proc.stderr or "").strip()[-300:]}
    return {"status": "ok", "version": proc.stdout.strip()}


def setup_framework(env: FrameworkEnv, *, dry_run: bool, base_python: Path | None) -> dict[str, object]:
    venv_dir = VENV_ROOT / env.key
    record: dict[str, object] = {
        "framework": env.key,
        "packages": list(env.packages),
        "venv": str(venv_dir),
    }
    if dry_run:
        record["status"] = "dry_run"
        return record
    python = _pyvenv_python(venv_dir)
    if not python.is_file():
        print(f"[{env.key}] creating venv at {venv_dir} (base: {base_python})", flush=True)
        created = _run([_uv(), "venv", "--python", str(base_python), str(venv_dir)])
        if created.returncode != 0 or not python.is_file():
            record["status"] = "venv_create_failed"
            record["errors"] = [{"detail": (created.stderr or "").strip()[-400:]}]
            return record
    record["python"] = str(python)
    install = _run([_uv(), "pip", "install", "--python", str(python), "--upgrade", "pip"])
    if install.returncode != 0:
        record["uv_bootstrap"] = "failed (continuing)"
    for package in env.packages:
        print(f"[{env.key}] installing {package}", flush=True)
        proc = _run([_uv(), "pip", "install", "--python", str(python), package])
        if proc.returncode != 0:
            record["status"] = "install_failed"
            record.setdefault("errors", []).append(
                {"package": package, "detail": (proc.stderr or proc.stdout or "").strip()[-500:]}
            )
            return record
    probe = _probe_import(python, env.import_name, env.version_attr)
    record.update(probe)
    return record


def record_system_envs(environments: dict[str, object]) -> dict[str, object]:
    """Record the shared environments for the manifest.

    T1/T2 run on the unified ``main`` interpreter (created with the same base
    Python as every framework venv); T6 is parent-repo tooling. The system
    interpreter is only a bootstrap fallback.
    """

    main_python = _pyvenv_python(VENV_ROOT / "main")
    python = main_python if main_python.is_file() else Path(sys.executable)
    parent_root = resolve_parent_repo() / ".venv"
    parent_candidate = _pyvenv_python(parent_root)
    if not parent_candidate.is_file():
        parent_candidate = parent_root / "bin" / "python"
    t6_python = parent_candidate if parent_candidate.is_file() else python
    t2 = _probe_import(python, "langgraph", "__version__")
    requests_probe = _probe_import(python, "requests", "__version__")
    environments["t1"] = {
        "framework": "t1",
        "packages": ["requests (unified main venv)"],
        "python": str(python),
        "status": requests_probe.get("status", "unknown"),
    }
    environments["t2"] = {
        "framework": "t2",
        "packages": ["langgraph==1.2.11", "langchain==1.3.18"],
        "python": str(python),
        "status": t2.get("status", "unknown"),
        "version": t2.get("version"),
    }
    environments["t6"] = {
        "framework": "t6",
        "packages": ["OSIS-AI native (parent repo .venv, Python 3.11)"],
        "python": str(t6_python),
        "status": "native",
    }
    return environments


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Set up per-framework environments")
    parser.add_argument(
        "--only",
        choices=sorted(FRAMEWORKS),
        default=None,
        help="set up one environment (default: main + t3 + t4 + t5)",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--base-python", type=Path, default=None,
                        help="interpreter used to create the venvs "
                             "(default: current python; Python 3.13 is recommended)")
    args = parser.parse_args(argv)

    keys = [args.only] if args.only else ["main", "t3", "t4", "t5"]
    base_python = args.base_python or Path(sys.executable)
    results: dict[str, object] = {}
    failures = 0
    for key in keys:
        env = FRAMEWORKS[key]
        record = setup_framework(env, dry_run=args.dry_run, base_python=base_python)
        results[key] = record
        if record.get("status") in ("install_failed", "import_failed"):
            failures += 1

    if not args.dry_run:
        manifest_path = VENV_ROOT / "environments.json"
        existing: dict[str, object] = {}
        if manifest_path.is_file():
            try:
                existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                existing = {}
        existing.update(results)
        record_system_envs(existing)
        manifest_path.write_text(
            json.dumps(existing, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(json.dumps({"manifest": str(manifest_path), "failures": failures}, ensure_ascii=False))
    else:
        print(json.dumps(results, ensure_ascii=False, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
