"""Loaders for the existing OSIS modeling datasets (full/gen/edit).

Dataset entries live in the parent repository as
``datasets/<split>/<bridge-skill>/<form>.json`` with one JSON object per
task::

    x          -> task input handed to the agent frameworks (full profile text)
    y          -> standard-answer path under the RAW skills tree, scorer-only
    source     -> provenance template name
    module/target/base_files (gen/edit) -> module-level task setup

Leakage rules enforced here:
- Agents must mount a skills snapshot (e.g. ``checkpoints/epoch0/skills``)
  that only contains *visible* train templates; test templates stay out.
- ``base_files`` for gen/edit are staged into the candidate workspace by this
  trusted module, reading from the raw skills tree. A base file whose
  normalized path equals the ``y`` answer path is rejected outright.
- ``y``/``source`` provenance never enters the model-facing ``TaskSpec``:
  :func:`to_task_spec` returns a sanitized spec carrying only ``x``, and the
  answer reference is written separately by the run driver into the private
  ``scorer_private/reference.json`` via :func:`reference_record`.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .task_schema import TaskSpec

BRIDGE_SKILL_TO_TYPE = {
    "osis-bridge-cantilever-box": "cantilever_box",
    "osis-bridge-conventional-box": "conventional_box",
    "osis-bridge-hollow-slab": "hollow_slab",
    "osis-bridge-precast-small-box": "precast_small_box",
    "osis-bridge-precast-t-girder": "precast_t_girder",
    "osis-bridge-rigid-frame-box": "rigid_frame",
}
DATASET_FORMS = ("full", "gen", "edit")
FORM_TO_TASK_FORM = {"full": "whole", "gen": "module", "edit": "modify"}
_CANDIDATE_PY_ROOT = "py"
_TEMPLATES_MARKER = "references" + "/" + "templates"


class DatasetError(ValueError):
    """Raised when a dataset entry cannot be loaded or staged safely."""


class LeakageError(DatasetError):
    """Raised when staging would expose the standard answer to the agent."""


@dataclass(frozen=True)
class DatasetEntry:
    split: str
    bridge_skill: str
    bridge_type: str
    form: str
    task_form: str
    index: int
    requirement: str
    source: str = ""
    source_b: str = ""
    module: str = ""
    target: str = ""
    base_files: tuple[str, ...] = field(default_factory=tuple)
    y_relative: str = ""
    y_template: str = ""

    @property
    def task_id(self) -> str:
        return f"{self.bridge_type}__{self.form}__{self.index:03d}"


def _template_relative(raw: str) -> str:
    """Strip ``.agents/skills/<bridge>/references/templates/<name>/`` from a dataset path.

    The remaining path maps 1:1 below the candidate project's ``py/`` root
    (``prep/_0_engine.py`` -> ``py/prep/_0_engine.py``).
    """

    normalized = str(raw).replace("\\", "/")
    marker = _TEMPLATES_MARKER + "/"
    position = normalized.find(marker)
    if position < 0:
        raise DatasetError(f"path is not a template path: {raw}")
    rest = normalized[position + len(marker):]
    segments = rest.split("/")
    if len(segments) < 2:
        # A bare template name: only valid for ``full`` entries whose y is the
        # whole answer template directory. Keep the name for path bookkeeping.
        return rest
    return "/".join(segments[1:])


def _y_template(raw: str) -> str:
    """The standard-answer template directory name (first segment after
    ``references/templates/``). Required to locate the reference PROJECT for
    gen/edit, whose ``y_relative`` drops the template name."""

    normalized = str(raw).replace("\\", "/")
    marker = _TEMPLATES_MARKER + "/"
    position = normalized.find(marker)
    if position < 0:
        raise DatasetError(f"path is not a template path: {raw}")
    segments = normalized[position + len(marker):].split("/")
    return segments[0] if segments and segments[0] else ""


def load_dataset_entry(
    parent_repo: Path,
    *,
    split: str,
    bridge_skill: str,
    form: str,
    index: int,
) -> DatasetEntry:
    """Load one task entry from ``datasets/<split>/<bridge>/<form>.json``."""

    if bridge_skill not in BRIDGE_SKILL_TO_TYPE:
        raise DatasetError(f"unknown bridge skill: {bridge_skill}")
    if form not in DATASET_FORMS:
        raise DatasetError(f"unknown dataset form: {form}")
    path = Path(parent_repo) / "datasets" / split / bridge_skill / f"{form}.json"
    if not path.is_file():
        raise FileNotFoundError(path)
    entries = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise DatasetError(f"dataset file must be a non-empty list: {path}")
    if not 0 <= index < len(entries):
        raise DatasetError(f"index {index} out of range for {path} ({len(entries)} entries)")
    raw = entries[index]
    requirement = str(raw.get("x") or "").strip()
    if not requirement:
        raise DatasetError(f"entry {index} in {path} has no x payload")
    y_raw = str(raw.get("y") or "")
    return DatasetEntry(
        split=split,
        bridge_skill=bridge_skill,
        bridge_type=BRIDGE_SKILL_TO_TYPE[bridge_skill],
        form=form,
        task_form=FORM_TO_TASK_FORM[form],
        index=index,
        requirement=requirement,
        source=str(raw.get("source") or ""),
        source_b=str(raw.get("source_b") or ""),
        module=str(raw.get("module") or ""),
        target=str(raw.get("target") or ""),
        base_files=tuple(str(item) for item in (raw.get("base_files") or [])),
        y_relative=_template_relative(y_raw) if y_raw else "",
        y_template=_y_template(y_raw) if y_raw else "",
    )


def to_task_spec(entry: DatasetEntry) -> TaskSpec:
    """Convert a dataset entry into the canonical, model-facing TaskSpec.

    The returned TaskSpec carries **only** the task input ``x`` plus the
    framework plumbing fields. No standard-answer provenance (``source``,
    ``source_b``, ``y_path``) and no raw template paths enter the spec, so
    neither the system prompt, ``input.json`` nor ``adapter_request.json``
    (all serialise ``task.to_dict()``) can leak answer names or paths.
    ``initial_project_snapshot`` stays unset here; gen/edit base files are
    staged directly into the candidate workspace by :func:`prestage_base_files`
    so the agent only sees ordinary copied filenames.
    """

    base_spec = {
        "task_id": entry.task_id,
        "bridge_type": entry.bridge_type,
        "task_form": entry.task_form,
        "difficulty": "L1",
        "natural_language_requirement": entry.requirement,
        # Scoring-intent metadata derived from the bridge type (NOT an answer
        # leak): the CLI needs is_continuous to score a cantilever/rigid-frame
        # as a continuous bridge instead of inferring from the directory name.
        "metadata": {
            "is_continuous": entry.bridge_type in ("cantilever_box", "conventional_box", "rigid_frame")
        },
    }
    if entry.task_form == "modify":
        # The schema requires a snapshot for modify tasks; the placeholder
        # carries no raw template path — the real files are staged separately
        # by prestage_base_files().
        base_spec["initial_project_snapshot"] = {"staged": "via_base_files"}
    return TaskSpec.from_dict(base_spec)


def reference_record(entry: DatasetEntry) -> dict[str, Any]:
    """Build the scorer-private reference record for one dataset entry.

    This data (hidden template name, answer path, provenance) must never reach
    the model or its visible artifacts. It is written by the run driver to the
    private ``scorer_private/reference.json`` after the run completes, for
    traceability and scorer-side use only.
    """

    return {
        "split": entry.split,
        "bridge_skill": entry.bridge_skill,
        "bridge_type": entry.bridge_type,
        "form": entry.form,
        "index": entry.index,
        "source": entry.source,
        "source_b": entry.source_b,
        "module": entry.module,
        "target": entry.target,
        "y_path": (
            f".agents/skills/{entry.bridge_skill}/references/templates/"
            f"{entry.y_template}{('/' + entry.y_relative) if entry.y_relative else ''}"
            if entry.y_template or entry.y_relative
            else ""
        ),
        "base_files_count": len(entry.base_files),
    }


def prestage_base_files(
    entry: DatasetEntry,
    parent_repo: Path,
    candidate_root: Path,
) -> list[str]:
    """Stage ``base_files`` from the raw skills tree into the candidate workspace.

    ``full`` entries stage nothing. Each base file is copied from the raw
    (trusted, tooling-side) skills tree to ``<candidate_root>/py/<template
    relative path>``. Copying the ``y`` answer itself raises :class:`LeakageError`.

    The answer check compares the TEMPLATE NAME as well as the relative path.
    ``edit`` deliberately stages the previous template's copy of the module
    under modification (``base_files = B 画像 + B prep, with the target cluster
    taken from A``), so a base file legitimately shares ``y``'s relative path
    while coming from a different template -- different content, not the answer.
    Only the same template's same path is the answer.
    """

    if entry.form == "full":
        return []
    candidate_root = Path(candidate_root)
    copied: list[str] = []
    for raw in entry.base_files:
        relative = _template_relative(raw)
        if (
            entry.y_relative
            and Path(relative).as_posix() == Path(entry.y_relative).as_posix()
            and _y_template(raw) == entry.y_template
        ):
            raise LeakageError(
                f"base_files contain the standard answer y: {raw}"
            )
        source = Path(parent_repo) / raw.replace("\\", "/")
        if not source.is_file():
            raise FileNotFoundError(source)
        destination = candidate_root / _CANDIDATE_PY_ROOT / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
        copied.append(destination.as_posix())
    return copied
