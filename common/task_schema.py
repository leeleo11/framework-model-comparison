"""Canonical task schema for bridge-modeling experiments."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Mapping

BRIDGE_TYPES = (
    "cantilever_box",
    "conventional_box",
    "hollow_slab",
    "precast_small_box",
    "precast_t_girder",
    "rigid_frame",
)
TASK_FORMS = ("whole", "module", "modify")
DIFFICULTIES = ("L1", "L2", "L3")
TASK_STAGES = ("P0", "P1", "P2", "P3", "P4", "P5", "P6")
# Formal comparison baseline.  The generation budget matches the parent repo's
# own bridge-building limit (``opencode_client.DEFAULT_TIMEOUT`` = 6000s, also
# used by ``train/runner.py`` and ``dashboard/jobs.py``), so no architecture is
# cut short relative to what the native OSIS-AI toolchain grants itself.  The
# reserve on top covers compilation, PyOSIS execution and source scoring, which
# are measured at under 200s in practice.
DEFAULT_TOTAL_TIMEOUT_S = 7800.0
DEFAULT_SUBTASK_TIMEOUT_S = {
    "P0": 300.0,
    "P1": 450.0,
    "P2": 1800.0,
    "P3": 450.0,
    "P4": 1200.0,
    "P5": 600.0,
    "P6": 120.0,
}


class TaskValidationError(ValueError):
    """Raised when a task does not satisfy the canonical modeling contract."""


@dataclass(frozen=True)
class TaskSpec:
    task_id: str
    bridge_type: str
    task_form: str
    difficulty: str
    natural_language_requirement: str
    total_timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S
    subtask_timeout_s: dict[str, float] | None = None
    initial_project_snapshot: str | dict[str, Any] | None = None
    expected_fields: dict[str, Any] | None = None
    metadata: dict[str, Any] | None = None

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TaskSpec":
        if not isinstance(data, Mapping):
            raise TaskValidationError("task must be a JSON object")

        required = (
            "task_id",
            "bridge_type",
            "task_form",
            "difficulty",
            "natural_language_requirement",
        )
        missing = [key for key in required if not str(data.get(key, "")).strip()]
        if missing:
            raise TaskValidationError(f"missing required field(s): {', '.join(missing)}")

        values = {key: str(data[key]).strip() for key in required}
        if values["bridge_type"] not in BRIDGE_TYPES:
            raise TaskValidationError(
                f"bridge_type must be one of {', '.join(BRIDGE_TYPES)}"
            )
        if values["task_form"] not in TASK_FORMS:
            raise TaskValidationError(
                f"task_form must be one of {', '.join(TASK_FORMS)}"
            )
        if values["difficulty"] not in DIFFICULTIES:
            raise TaskValidationError(
                f"difficulty must be one of {', '.join(DIFFICULTIES)}"
            )

        snapshot = data.get("initial_project_snapshot")
        if values["task_form"] == "modify" and snapshot in (None, ""):
            raise TaskValidationError(
                "initial_project_snapshot is required for modify tasks"
            )

        expected_fields = data.get("expected_fields")
        if expected_fields is not None and not isinstance(expected_fields, dict):
            raise TaskValidationError("expected_fields must be an object")
        metadata = data.get("metadata")
        if metadata is not None and not isinstance(metadata, dict):
            raise TaskValidationError("metadata must be an object")

        total_timeout_s = data.get("total_timeout_s", DEFAULT_TOTAL_TIMEOUT_S)
        try:
            total_timeout_s = float(total_timeout_s)
        except (TypeError, ValueError) as exc:
            raise TaskValidationError("total_timeout_s must be a positive number") from exc
        if total_timeout_s <= 0:
            raise TaskValidationError("total_timeout_s must be a positive number")

        raw_subtask_timeout_s = data.get("subtask_timeout_s")
        if raw_subtask_timeout_s is not None and not isinstance(raw_subtask_timeout_s, Mapping):
            raise TaskValidationError("subtask_timeout_s must be an object")
        subtask_timeout_s = dict(DEFAULT_SUBTASK_TIMEOUT_S)
        if raw_subtask_timeout_s is not None:
            unknown = [stage for stage in raw_subtask_timeout_s if stage not in TASK_STAGES]
            if unknown:
                raise TaskValidationError(
                    f"subtask_timeout_s contains unknown stage(s): {', '.join(map(str, unknown))}"
                )
            for stage, value in raw_subtask_timeout_s.items():
                try:
                    value = float(value)
                except (TypeError, ValueError) as exc:
                    raise TaskValidationError(
                        f"subtask_timeout_s[{stage}] must be a positive number"
                    ) from exc
                if value <= 0:
                    raise TaskValidationError(
                        f"subtask_timeout_s[{stage}] must be a positive number"
                    )
                subtask_timeout_s[str(stage)] = value

        return cls(
            **values,
            total_timeout_s=total_timeout_s,
            subtask_timeout_s=subtask_timeout_s,
            initial_project_snapshot=snapshot,
            expected_fields=expected_fields,
            metadata=metadata,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def load_task(path: Path) -> TaskSpec:
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise TaskValidationError(f"invalid task JSON: {path}") from exc
    return TaskSpec.from_dict(data)
