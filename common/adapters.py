"""Architecture metadata and optional external-framework hooks."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .modeling_pipeline import CANONICAL_PROJECT_FILES
from .pyosis_adapter import PyOSISAdapter
from .skill_adapter import SkillAdapter
from .task_schema import TaskSpec


@dataclass(frozen=True)
class AdapterSpec:
    architecture_id: str
    name: str
    framework_version: str
    mounting_mode: str
    interaction_mode: str
    framework_import: str | None


@dataclass
class ArchitectureAdapter:
    spec: AdapterSpec

    def prepare(
        self,
        task: TaskSpec,
        run_dir: Path,
        skill_adapter: SkillAdapter,
        osis_adapter: PyOSISAdapter,
    ) -> dict[str, Any]:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        request = {
            "architecture_id": self.spec.architecture_id,
            "name": self.spec.name,
            "framework_version": self.spec.framework_version,
            "mounting_mode": self.spec.mounting_mode,
            "interaction_mode": self.spec.interaction_mode,
            "task": task.to_dict(),
            "skill_bundle_sha256": skill_adapter.skill_bundle_hash(),
            "output_contract": {
                "project_root": "candidate_project",
                "code_root": "candidate_project/py",
                "canonical_files": [
                    "py/项目画像.md",
                    *[f"py/prep/{name}" for name in CANONICAL_PROJECT_FILES[1:]],
                ],
            },
            "executor": {
                "owner": "common.runner.ExperimentRunner",
                "phase": "after_generation_compile",
                "type": "pyosis",
                "status": "pending_candidate",
                "score_after_execution": "osis-auto-testconformance-cli",
            },
        }
        (run_dir / "skill_index.json").write_text(
            json.dumps(skill_adapter.skill_index(), ensure_ascii=False, indent=2)
            + "\n",
            encoding="utf-8",
        )
        # Every architecture receives exactly the same immutable skill bundle.
        # ``mounting_mode`` describes how an adapter exposes it to its agent;
        # it must not change the content available to the experiment.
        skill_adapter.write_fixed_bundle(run_dir / "skill_bundle.md")
        (run_dir / "skill_mount.json").write_text(
            json.dumps(
                {
                    "delivery": self.spec.mounting_mode,
                    "source": str(skill_adapter.root),
                    "bundle_sha256": skill_adapter.skill_bundle_hash(),
                    "read_only": True,
                    "equal_bundle_for_all_architectures": True,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        (run_dir / "adapter_request.json").write_text(
            json.dumps(request, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        backend_status = osis_adapter.create_model(run_dir)
        prepared_status = "prepared" if backend_status.get("execution_enabled") else "not_configured"
        return {
            "status": prepared_status,
            "failure_code": backend_status.get("failure_code"),
            "backend_status": backend_status,
        }


ADAPTER_SPECS = {
    "T1": AdapterSpec("T1", "direct-one-shot", "direct-one-shot-v1", "fixed_bundle", "one_shot", None),
    "T2": AdapterSpec("T2", "langgraph-react", "langgraph==1.2.11/langchain==1.3.18", "generic_tools", "progressive", "langgraph"),
    "T3": AdapterSpec("T3", "smolagents-codeact", "smolagents==1.26.0", "generic_tools", "progressive", "smolagents"),
    "T4": AdapterSpec("T4", "openhands-codeact", "openhands-sdk==1.44.1", "native", "progressive", "openhands"),
    "T5": AdapterSpec("T5", "crewai-roles", "crewai==1.15.18", "generic_tools", "progressive", "crewai"),
    "T6": AdapterSpec("T6", "osis-native", "local-clean-snapshot", "native", "stateful", None),
}


def get_adapter(architecture_id: str) -> ArchitectureAdapter:
    try:
        return ArchitectureAdapter(ADAPTER_SPECS[architecture_id])
    except KeyError as exc:
        raise KeyError(f"unknown architecture: {architecture_id}") from exc
