"""Run manifest and artifact hashing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def artifact_hashes(run_dir: Path) -> dict[str, str]:
    run_dir = Path(run_dir)
    result: dict[str, str] = {}
    for path in sorted(run_dir.rglob("*"), key=lambda item: str(item).lower()):
        if (
            not path.is_file()
            or path.name == "manifest.json"
            or path.suffix == ".pyc"
            or any(part in {"__pycache__", ".pytest_cache"} for part in path.parts)
        ):
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        result[str(path.relative_to(run_dir)).replace("\\", "/")] = digest
    return result


@dataclass
class Manifest:
    task_id: str
    architecture_id: str
    framework_version: str
    commit: str
    model_snapshot: str
    skill_bundle_sha256: str
    tool_schema_version: str
    solver_version: str
    container_digest: str
    seed: int
    timeout_s: int
    token_budget: int
    tool_call_budget: int
    subtask_timeout_s: dict[str, float] = field(default_factory=dict)
    artifact_hashes: dict[str, str] = field(default_factory=dict)
    failure_code: str | None = None
    human_intervention: bool = False
    created_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, path: Path) -> None:
        path = Path(path)
        path.write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
