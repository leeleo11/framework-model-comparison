"""Adapter for the repository's AST-based model conformance evaluator."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .modeling_pipeline import _resolve_project_root


BRIDGE_TYPE_ALIASES = {
    "conventional_box": "cast_in_place_box",
    "precast_t_girder": "t_girder",
}


def _candidate_code_root(project: Path) -> Path:
    root = _resolve_project_root(project)
    return root / "py" if (root / "py").is_dir() else root


def evaluate_static_conformance(
    candidate_project: Path,
    *,
    bridge_type: str,
    parent_repo: Path | None = None,
    reference_project: Path | None = None,
    expected_l: float | None = None,
    is_continuous: bool | None = None,
) -> dict[str, Any]:
    """Run OSIS's static evaluator, or return an explicit unavailable result.

    The evaluator is intentionally imported lazily from the configured parent
    repository.  This keeps the comparison project usable without installing
    the full OSIS application and prevents a missing evaluator from becoming
    a false pass.
    """

    parent = Path(parent_repo).expanduser().resolve() if parent_repo else None
    src = parent / "src" if parent else None
    if src is None or not src.is_dir():
        return {
            "status": "evaluator_unavailable",
            "available": False,
            "candidate_score": None,
            "overall_score": None,
            "reason": "parent_repo/src does not exist",
        }

    candidate_root = _candidate_code_root(Path(candidate_project))
    if not candidate_root.is_dir():
        return {
            "status": "skipped",
            "available": True,
            "candidate_score": 0.0,
            "overall_score": 0.0,
            "reason": "candidate project has no code root",
        }

    inserted = str(src) not in sys.path
    if inserted:
        sys.path.insert(0, str(src))
    try:
        from evaluation.levels import model_conformance as mc

        reference_root = None
        if reference_project is not None:
            reference_root = _candidate_code_root(Path(reference_project))
        resources: dict[str, Any] = {
            "bridge_type": BRIDGE_TYPE_ALIASES.get(bridge_type, bridge_type),
            "expected_L": expected_l,
            "is_continuous": is_continuous,
        }
        report = mc.evaluate(
            reference_root,
            candidate_root,
            system_config={},
            resources=resources,
        )
    except Exception as exc:  # evaluator dependencies are outside this repo
        return {
            "status": "evaluator_error",
            "available": True,
            "candidate_score": None,
            "overall_score": None,
            "reason": f"{type(exc).__name__}: {exc}",
        }
    finally:
        if inserted:
            try:
                sys.path.remove(str(src))
            except ValueError:
                pass

    return {
        "status": "skipped" if report.get("skipped") else "evaluated",
        "available": True,
        "candidate_score": report.get("candidate_score"),
        "overall_score": report.get("overall_score"),
        "report": report,
    }
