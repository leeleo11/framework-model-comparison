"""Independent scorer for models persisted by the PyOSIS runtime."""

from __future__ import annotations

import json
import traceback
from pathlib import Path
from typing import Any

from .runtime_measurements import (
    EXTRACTOR_VERSION,
    load_measurements,
    measurements_to_params,
)


# Task-schema bridge names are intentionally kept separate from the parent
# evaluator's historical scorer names.  Runtime scoring uses the same alias
# contract as the existing source/static path.
BRIDGE_TYPE_ALIASES = {
    "conventional_box": "cast_in_place_box",
    "precast_t_girder": "t_girder",
}


def _write_result(run_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "runtime_score.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, default=str) + "\n",
        encoding="utf-8",
    )
    return result


def _unavailable(run_dir: Path, *, status: str, reason: str, **extra: Any) -> dict[str, Any]:
    return _write_result(
        run_dir,
        {
            "scorer": "osis-runtime-model-conformance",
            "source": "pyosis_runtime_snapshot",
            "extractor_version": EXTRACTOR_VERSION,
            "status": status,
            "candidate_score": None,
            "total_score_5": None,
            "reason": reason,
            **extra,
        },
    )


def evaluate_runtime_snapshot(
    snapshot_path: Path,
    run_dir: Path,
    *,
    bridge_type: str,
    is_continuous: bool | None = None,
    is_prestressed: bool | None = None,
    parent_repo: Path | None = None,
) -> dict[str, Any]:
    """Extract and score one executed model without touching source scores."""

    snapshot_path = Path(snapshot_path)
    run_dir = Path(run_dir)
    if not snapshot_path.is_file():
        return _unavailable(run_dir, status="not_available", reason="runtime snapshot does not exist")

    try:
        measurements = load_measurements(snapshot_path)
    except Exception as exc:  # noqa: BLE001 - sidecar must explain malformed snapshots
        return _unavailable(
            run_dir,
            status="extraction_error",
            reason=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=5),
        )
    if measurements.get("status") not in (None, "available"):
        return _unavailable(
            run_dir,
            status="not_available",
            reason=str(measurements.get("probe_error") or "runtime snapshot unavailable"),
            snapshot_status=measurements.get("status"),
        )

    try:
        params, missing_params, extraction_provenance = measurements_to_params(
            measurements,
            bridge_type=bridge_type,
            is_continuous=is_continuous,
            is_prestressed=is_prestressed,
            parent_repo=parent_repo,
        )
    except Exception as exc:  # noqa: BLE001 - preserve source evaluation on any adapter error
        return _unavailable(
            run_dir,
            status="extraction_error",
            reason=f"{type(exc).__name__}: {exc}",
            traceback=traceback.format_exc(limit=8),
        )

    try:
        # Import the authoritative bridge rules lazily.  This is the same
        # parent source package used by static_conformance.py; no formulas are
        # copied into this comparison project.
        from .runtime_measurements import _parent_common

        parent_common = _parent_common(parent_repo)
        if parent_common is None:
            raise ImportError("parent_repo/src evaluation package is unavailable")
        from evaluation.levels import model_conformance as mc

        scorer_bridge_type = BRIDGE_TYPE_ALIASES.get(bridge_type, bridge_type)
        scorer = mc.SCORERS.get(scorer_bridge_type, mc.SCORERS["unknown"])
        candidate_score, details = scorer(params)
        candidate_score = float(candidate_score)
        report_input = {
            "bridge_type": scorer_bridge_type,
            "candidate": details,
        }
        report_text = mc.generate_report(report_input, "runtime model")
    except Exception as exc:  # noqa: BLE001 - sidecar reports scorer failures
        return _unavailable(
            run_dir,
            status="score_error",
            reason=f"{type(exc).__name__}: {exc}",
            params=params,
            missing_params=missing_params,
            provenance=extraction_provenance,
            traceback=traceback.format_exc(limit=8),
        )

    result = {
        "scorer": "osis-runtime-model-conformance",
        "source": "pyosis_runtime_snapshot",
        "extractor_version": EXTRACTOR_VERSION,
        "status": "evaluated",
        "bridge_type": bridge_type,
        "scorer_bridge_type": scorer_bridge_type,
        "candidate_score": candidate_score,
        "total_score_5": round(candidate_score * 5.0, 6),
        "params": params,
        "missing_params": missing_params,
        "dimensions": details.get("dimensions", {}),
        "candidate": details,
        "provenance": {
            **extraction_provenance,
            "runtime_measurements_path": str(snapshot_path.resolve()),
            "model_state_path": str((run_dir / "model_state.json").resolve()),
        },
    }
    result = _write_result(run_dir, result)
    (run_dir / "runtime_score.md").write_text(report_text, encoding="utf-8")
    return result


__all__ = ["evaluate_runtime_snapshot"]
