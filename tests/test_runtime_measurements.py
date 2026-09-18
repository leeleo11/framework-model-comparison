import json
from pathlib import Path

import pytest

from common.runtime_measurements import (
    CANONICAL_PARAM_KEYS,
    EXTRACTOR_VERSION,
    load_measurements,
    measurements_to_params,
)


FIXTURE = Path(__file__).parent / "fixtures" / "runtime_measurements_cantilever.json"
REAL_REFERENCE = (
    Path(__file__).parent / "fixtures" / "runtime_measurements_reference.json"
)


def test_runtime_measurements_derive_bridge_geometry_and_full_canonical_key_set():
    measurements = load_measurements(FIXTURE)

    params, missing, provenance = measurements_to_params(
        measurements,
        bridge_type="cantilever_box",
        is_continuous=True,
    )

    assert set(params) == set(CANONICAL_PARAM_KEYS)
    assert params["L"] == 120.0
    assert params["span_lengths"] == [65.0, 120.0, 65.0]
    assert params["total_length"] == 250.0
    assert params["H_root"] == 7.5
    assert params["H_mid"] == 3.2
    assert params["concrete_grade"] == 60
    assert params["node_count"] == 4
    assert params["section_count"] == 2
    assert params["element_count"] == 3
    assert params["is_continuous"] is True
    assert provenance["source"] == "pyosis_runtime_snapshot"
    assert provenance["extractor_version"] == EXTRACTOR_VERSION


def test_runtime_measurements_do_not_turn_absent_tendon_data_into_true(tmp_path: Path):
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    payload.pop("loadcases", None)
    payload.pop("tendon_props", None)
    payload.pop("tendon_shapes", None)
    path = tmp_path / "measurements.json"
    path.write_text(json.dumps(payload), encoding="utf-8")

    params, missing, _ = measurements_to_params(load_measurements(path))

    assert params["has_prestress"] is None
    assert params["has_vertical_tendon"] is None
    assert "has_prestress" in missing
    assert "has_vertical_tendon" in missing


def test_runtime_measurements_extract_actual_nested_pyosis_reference_snapshot():
    """The persisted PyOSIS shape is the contract, not a synthetic flat export."""

    measurements = load_measurements(REAL_REFERENCE)
    params, missing, provenance = measurements_to_params(
        measurements,
        bridge_type="cantilever_box",
        is_continuous=True,
    )

    assert params["section_count"] == 19
    assert params["node_count"] == 91
    assert params["element_count"] == 82
    assert params["span_lengths"] == pytest.approx([65.0, 120.0, 65.0], abs=0.2)
    assert params["T_mid"] == pytest.approx(0.32, abs=0.03)
    assert params["T_root"] == pytest.approx(1.2, abs=0.05)
    assert params["T_top_mid"] == pytest.approx(0.28, abs=0.03)
    assert params["web_t_mid"] == pytest.approx(0.5, abs=0.05)
    assert params["flange_tip"] == pytest.approx(0.2, abs=0.03)
    assert params["concrete_grade"] == 60
    assert params["has_prestress"] is True
    assert params["section_area_avg"] == pytest.approx(13.98, rel=0.02)
    assert params["pst_steel_kg_per_m"] == pytest.approx(1065.5, rel=0.08)
    assert params["pst_steel_ratio"] == pytest.approx(76.2, rel=0.05)
    assert "T_mid" not in missing
    assert provenance["source"] == "pyosis_runtime_snapshot"
