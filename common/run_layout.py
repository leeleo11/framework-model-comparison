"""Canonical nested result-directory layout for comparison runs.

New writes go to::

    runs/<sweep>/<architecture>/<bridge_type>/<form>/{bridge}__{form}__{index:03d}__{arch}__seed{seed}

Source/template names are not used as leaf folders.  Older source-named
leaves remain readable until they are migrated back.
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

TASK_FORM_TO_DATASET = {"whole": "full", "module": "gen", "modify": "edit"}
DATASET_FORMS = ("full", "gen", "edit")

BRIDGE_SKILL_TO_TYPE = {
    "osis-bridge-cantilever-box": "cantilever_box",
    "osis-bridge-conventional-box": "conventional_box",
    "osis-bridge-hollow-slab": "hollow_slab",
    "osis-bridge-precast-small-box": "precast_small_box",
    "osis-bridge-precast-t-girder": "precast_t_girder",
    "osis-bridge-rigid-frame-box": "rigid_frame",
}

CELL_NAME_RE = re.compile(
    r"^(?P<bridge>[A-Za-z0-9_]+)__(?P<form>full|gen|edit)__(?P<index>\d+)"
    r"__(?P<arch>T[1-6])__seed(?P<seed>\d+)$"
)

SKIP_SCAN_PARTS = frozenset(
    {
        "_archive",
        "archive",
        "candidate_project",
        "generated",
        "scorer_private",
        "private",
        "leakage_guard_failures",
    }
)


def normalize_bridge_type(bridge: str) -> str:
    text = str(bridge).strip()
    mapped = BRIDGE_SKILL_TO_TYPE.get(text)
    if mapped:
        return mapped
    if text.startswith("osis-bridge-"):
        return text.removeprefix("osis-bridge-").replace("-", "_")
    return text


def dataset_form(task_form: str) -> str:
    return TASK_FORM_TO_DATASET.get(str(task_form), str(task_form))


_WIN_FORBIDDEN = re.compile(r'[<>:"/\\|?*]')
TYPE_TO_SKILL = {value: key for key, value in BRIDGE_SKILL_TO_TYPE.items()}


def as_bridge_skill(bridge: str) -> str:
    text = str(bridge).strip()
    if text in BRIDGE_SKILL_TO_TYPE:
        return text
    return TYPE_TO_SKILL.get(text, text)


def safe_source_name(source: str) -> str:
    return _WIN_FORBIDDEN.sub("-", str(source).strip()).strip(" .")


def lookup_source(
    parent_repo: Path | None,
    bridge: str,
    form: str,
    index: int,
    split: str = "test",
) -> str:
    if parent_repo is None:
        return ""
    try:
        from common.dataset import load_dataset_entry

        entry = load_dataset_entry(
            Path(parent_repo),
            split=split,
            bridge_skill=as_bridge_skill(bridge),
            form=form,
            index=int(index),
        )
    except Exception:
        return ""
    return str(getattr(entry, "source", "") or "")


def legacy_cell_name(
    bridge: str,
    form: str,
    index: int,
    architecture: str,
    seed: int,
) -> str:
    return (
        f"{normalize_bridge_type(bridge)}__{form}__{int(index):03d}__"
        f"{str(architecture).upper()}__seed{int(seed)}"
    )


def cell_name(
    bridge: str,
    form: str,
    index: int,
    architecture: str,
    seed: int,
    source: str = "",
) -> str:
    del source
    return legacy_cell_name(bridge, form, index, architecture, seed)


def nested_run_dir(
    runs_dir: Path,
    bridge: str,
    form: str,
    index: int,
    architecture: str,
    seed: int,
    source: str = "",
) -> Path:
    del source
    bridge_type = normalize_bridge_type(bridge)
    architecture = str(architecture).upper()
    name = legacy_cell_name(bridge_type, form, index, architecture, seed)
    return Path(runs_dir) / architecture / bridge_type / str(form) / name


def source_named_run_dir(
    runs_dir: Path,
    bridge: str,
    form: str,
    architecture: str,
    seed: int,
    source: str,
) -> Path | None:
    """Read-only fallback path used before leftover source leaves are migrated."""

    label = safe_source_name(source)
    if not label:
        return None
    if int(seed):
        label = f"{label}__seed{int(seed)}"
    bridge_type = normalize_bridge_type(bridge)
    architecture = str(architecture).upper()
    return Path(runs_dir) / architecture / bridge_type / str(form) / label


def flat_run_dir(
    runs_dir: Path,
    bridge: str,
    form: str,
    index: int,
    architecture: str,
    seed: int,
) -> Path:
    return Path(runs_dir) / legacy_cell_name(bridge, form, index, architecture, seed)


def run_dir_for(
    runs_dir: Path,
    bridge: str,
    form: str,
    index: int,
    architecture: str,
    seed: int,
    source: str = "",
) -> Path:
    """Primary write path: architecture / bridge / form / opaque cell name."""

    return nested_run_dir(runs_dir, bridge, form, index, architecture, seed)


def candidate_run_dirs(
    runs_dir: Path,
    bridge: str,
    form: str,
    index: int,
    architecture: str,
    seed: int,
    source: str = "",
) -> list[Path]:
    """Legacy nested path, leftover source-named nested path, then flat."""

    ordered = [
        nested_run_dir(runs_dir, bridge, form, index, architecture, seed),
        flat_run_dir(runs_dir, bridge, form, index, architecture, seed),
    ]
    source_path = source_named_run_dir(
        runs_dir, bridge, form, architecture, seed, source
    )
    if source_path is not None:
        ordered.insert(1, source_path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in ordered:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return unique


def index_from_task_id(task_id: str) -> int:
    parts = str(task_id).split("__")
    if parts and parts[-1].isdigit():
        return int(parts[-1])
    return 0


def form_from_task(task: Any) -> str:
    task_id = str(getattr(task, "task_id", "") or "")
    parts = task_id.split("__")
    if len(parts) >= 2 and parts[1] in DATASET_FORMS:
        return parts[1]
    return dataset_form(str(getattr(task, "task_form", "") or ""))


def run_dir_from_task(
    runs_dir: Path,
    task: Any,
    architecture_id: str,
    seed: int,
    source: str = "",
) -> Path:
    return nested_run_dir(
        runs_dir,
        str(getattr(task, "bridge_type", "") or "unknown"),
        form_from_task(task),
        index_from_task_id(str(getattr(task, "task_id", "") or "")),
        architecture_id,
        seed,
    )


def parse_cell_name(name: str) -> dict[str, Any] | None:
    match = CELL_NAME_RE.match(str(name))
    if match is None:
        return None
    return {
        "bridge": match.group("bridge"),
        "form": match.group("form"),
        "index": int(match.group("index")),
        "architecture": match.group("arch"),
        "seed": int(match.group("seed")),
    }


def is_cell_dir(path: Path) -> bool:
    item = Path(path)
    if not item.is_dir():
        return False
    if CELL_NAME_RE.match(item.name):
        return True
    return (item / "evaluation.json").is_file() or (item / "manifest.json").is_file()


def iter_cell_dirs(runs_dir: Path) -> list[Path]:
    root = Path(runs_dir)
    if not root.is_dir():
        return []
    found: list[Path] = []
    for architecture in ("T1", "T2", "T3", "T4", "T5", "T6"):
        arch_dir = root / architecture
        if not arch_dir.is_dir():
            continue
        for bridge_dir in arch_dir.iterdir():
            if not bridge_dir.is_dir() or bridge_dir.name in SKIP_SCAN_PARTS:
                continue
            for form_dir in bridge_dir.iterdir():
                if not form_dir.is_dir() or form_dir.name not in DATASET_FORMS:
                    continue
                for cell in form_dir.iterdir():
                    if is_cell_dir(cell):
                        found.append(cell)
    for path in root.iterdir():
        if path.is_dir() and CELL_NAME_RE.match(path.name):
            found.append(path)
    unique: list[Path] = []
    seen: set[str] = set()
    for path in found:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        unique.append(path)
    return sorted(unique, key=lambda item: item.as_posix())


def result_tree(run_dir: Path, runs_root: Path) -> str:
    try:
        rel = Path(run_dir).resolve().relative_to(Path(runs_root).resolve())
    except ValueError:
        return ""
    parents = rel.parts[:-1]
    return "/".join(parents)


def is_run_manifest(root: Path, manifest_path: Path) -> bool:
    try:
        parts = manifest_path.relative_to(root).parts
    except ValueError:
        return False
    if any(part in SKIP_SCAN_PARTS for part in parts[:-1]):
        return False
    parent = manifest_path.parent
    if CELL_NAME_RE.match(parent.name):
        return True
    return (parent / "evaluation.json").is_file() and (parent / "input.json").is_file()


def source_from_run_dir(run_dir: Path) -> str:
    path = Path(run_dir) / "scorer_private" / "reference.json"
    if not path.is_file():
        return ""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return ""
    if not isinstance(raw, dict):
        return ""
    return str(raw.get("source") or "").strip()


def _move_run(run_dir: Path, dest: Path, *, dry_run: bool) -> Path:
    if dest.resolve() == Path(run_dir).resolve():
        return dest
    if dest.exists():
        return dest
    if dry_run:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(run_dir), str(dest))
    return dest


def migrate_flat_run(
    run_dir: Path,
    runs_root: Path,
    *,
    dry_run: bool = False,
    source: str = "",
) -> Path | None:
    del source
    parsed = parse_cell_name(Path(run_dir).name)
    if parsed is None:
        return None
    dest = nested_run_dir(
        runs_root,
        parsed["bridge"],
        parsed["form"],
        parsed["index"],
        parsed["architecture"],
        parsed["seed"],
    )
    return _move_run(run_dir, dest, dry_run=dry_run)


def _index_and_seed_from_cell(run_dir: Path) -> tuple[int, int]:
    task: dict[str, Any] = {}
    manifest: dict[str, Any] = {}
    for name, bucket in (("input.json", task), ("manifest.json", manifest)):
        path = Path(run_dir) / name
        if not path.is_file():
            continue
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, TypeError):
            continue
        if isinstance(raw, dict):
            bucket.update(raw)
    task_id = str(task.get("task_id") or manifest.get("task_id") or "")
    try:
        seed = int(manifest.get("seed") or 0)
    except (TypeError, ValueError):
        seed = 0
    return index_from_task_id(task_id), seed


def migrate_source_cell_to_legacy(
    run_dir: Path,
    runs_root: Path,
    *,
    dry_run: bool = False,
) -> Path | None:
    """Rename a leftover source-named nested cell back to the opaque leaf."""

    run_dir = Path(run_dir)
    if parse_cell_name(run_dir.name) is not None:
        return run_dir
    try:
        parts = run_dir.resolve().relative_to(Path(runs_root).resolve()).parts
    except ValueError:
        return None
    if (
        len(parts) != 4
        or parts[0] not in {"T1", "T2", "T3", "T4", "T5", "T6"}
        or parts[2] not in DATASET_FORMS
    ):
        return None
    index, seed = _index_and_seed_from_cell(run_dir)
    dest = nested_run_dir(
        runs_root, parts[1], parts[2], index, parts[0], seed
    )
    return _move_run(run_dir, dest, dry_run=dry_run)
