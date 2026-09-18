"""Turn a persisted PyOSIS model snapshot into scorer parameters.

The source evaluator reads generated Python with an AST.  This module is the
parallel runtime adapter: it reads only the records returned by PyOSIS
managers after the generated project has executed, normalises them into the
same parameter vocabulary, and leaves all bridge-specific scoring formulas to
the parent repository's scorer.
"""

from __future__ import annotations

import json
import math
import os
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .paths import try_resolve_parent_repo


EXTRACTOR_VERSION = "runtime-measurements-v1"


CANONICAL_PARAM_KEYS: tuple[str, ...] = (
    "bridge_type",
    "L",
    "is_continuous",
    "is_prestressed",
    "H_root",
    "H_mid",
    "T_root",
    "T_dia_root",
    "T_mid",
    "T_top_mid",
    "T_top_root",
    "web_t_mid",
    "web_t_support",
    "flange_tip",
    "t_girder_mid",
    "t_girder_support",
    "small_box_mid",
    "small_box_support",
    "hollow_slab_mid",
    "hollow_slab_support",
    "concrete_ratio",
    "component_thickness_avg",
    "beam_width",
    "section_area_avg",
    "pst_steel_kg_per_m",
    "pst_steel_ratio",
    "void_ratio",
    "stage_names",
    "total_length",
    "has_vertical_tendon",
    "has_prestress",
    "heights",
    "thicknesses",
    "thickness_profile",
    "section_count",
    "girder_section_count",
    "section_types",
    "node_count",
    "element_count",
    "boundary_count",
    "stage_count",
    "support_xs",
    "height_profile",
    "side_spans",
    "main_span_from_nodes",
    "pier_heights",
    "pier_source",
    "span_lengths",
    "concrete_grade",
    "zero_block_len",
    "zero_block_lens",
)

_SECTION_TYPE_BY_NUMBER = {
    7: "HOLLOWSLAB",
    8: "SMALLBOX",
    9: "TGIRDER",
    10: "CONVENTIONALBOX",
}
_ELEMENT_TYPE_BY_NUMBER = {1: "BEAM3D", 2: "TRUSS", 3: "SPRING", 4: "CABLE", 5: "SHELL"}
_MAIN_SECTION_TYPES = {"CONVENTIONALBOX", "CONVENTIONAL", "TGIRDER", "SMALLBOX", "HOLLOWSLAB"}
_VERTICAL_RE = re.compile(r"竖向|vertical|(?:^|[^a-z0-9])(?:sv|wv|vps)(?:[^a-z0-9]|$)", re.I)
_CONCRETE_RE = re.compile(r"\bC(\d{2,3})\b", re.I)
_DENSITY = 7850.0


def load_measurements(path: Path) -> dict[str, Any]:
    """Load and validate one ``runtime_measurements.json`` artifact."""

    path = Path(path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("runtime measurements must be a JSON object")
    version = payload.get("schema_version")
    if version not in (None, "osis-runtime-measurements-v1"):
        raise ValueError(f"unsupported runtime measurements schema: {version}")
    return payload


def _normal_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _enum_name(value: Any) -> Any:
    if isinstance(value, Mapping):
        return value.get("name") or value.get("value")
    return value


def _fields(record: Mapping[str, Any] | None) -> dict[str, Any]:
    """Flatten a backend record and its ``prop`` object for alias lookup."""

    if not isinstance(record, Mapping):
        return {}
    out: dict[str, Any] = {}
    prop = record.get("prop")
    if isinstance(prop, Mapping):
        out.update(prop)
    out.update(record)
    return {_normal_key(key): value for key, value in out.items()}


def _first(record: Mapping[str, Any] | None, *names: str) -> Any:
    values = _fields(record)
    for name in names:
        key = _normal_key(name)
        if key in values and values[key] is not None:
            return values[key]
    return None


def _number(value: Any) -> float | None:
    value = _enum_name(value)
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
    elif isinstance(value, str):
        match = re.search(r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?", value)
        if not match:
            return None
        try:
            result = float(match.group(0))
        except ValueError:
            return None
    else:
        return None
    return result if math.isfinite(result) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, str):
        text = value.strip()
        if not re.fullmatch(r"[-+]?\d+(?:\.0+)?", text):
            return None
    number = _number(value)
    if number is None or abs(number - round(number)) > 1e-9:
        return None
    return int(round(number))


def _round(value: Any, digits: int = 4) -> float | None:
    number = _number(value)
    return None if number is None else round(number, digits)


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def _section_type(record: Mapping[str, Any]) -> str | None:
    raw = _enum_name(_first(record, "type", "section_type", "sectionType"))
    number = _integer(raw)
    if number is not None:
        return _SECTION_TYPE_BY_NUMBER.get(number, str(number))
    if raw is None:
        return None
    text = str(raw).upper().replace("-", "").replace("_", "")
    aliases = {
        "CONVENTIONAL": "CONVENTIONALBOX",
        "CONVENTIONALBOX": "CONVENTIONALBOX",
        "TGIRDER": "TGIRDER",
        "SMALLBOX": "SMALLBOX",
        "HOLLOWSLAB": "HOLLOWSLAB",
        "RECTANGLE": "RECT",
        "RECT": "RECT",
    }
    return aliases.get(text, str(raw).upper())


def _point(value: Any) -> tuple[float, float] | None:
    """Read one point from a PyOSIS contour representation."""

    if isinstance(value, Mapping):
        x = _number(value.get("x", value.get("X", value.get("coorX"))))
        y = _number(value.get("y", value.get("Y", value.get("coorY"))))
        return (x, y) if x is not None and y is not None else None
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)) and len(value) >= 2:
        x, y = _number(value[0]), _number(value[1])
        return (x, y) if x is not None and y is not None else None
    return None


def _contour_polygons(contour: Any) -> list[list[tuple[float, float]]]:
    """Flatten nested outer/inner polygons emitted by ``Section.contour``.

    The runtime manager serialises a conventional box as ``[[{x,y}, ...],
    [{x,y}, ...]]``.  Older/fake adapters may emit a single flat point list or
    coordinate pairs, so the parser intentionally accepts all three forms.
    """

    point = _point(contour)
    if point is not None:
        return [[point]]
    values = _as_list(contour)
    if not values:
        return []
    direct = [item for item in (_point(value) for value in values) if item is not None]
    if len(direct) >= 2:
        return [direct]
    polygons: list[list[tuple[float, float]]] = []
    for value in values:
        polygons.extend(_contour_polygons(value))
    return polygons


def _contour_size(contour: Any) -> tuple[float | None, float | None]:
    points = [point for polygon in _contour_polygons(contour) for point in polygon]
    if len(points) < 2:
        return None, None
    return max(x for x, _ in points) - min(x for x, _ in points), max(y for _, y in points) - min(y for _, y in points)


def _polygon_area(points: Sequence[tuple[float, float]]) -> float:
    if len(points) < 3:
        return 0.0
    return abs(sum(
        points[index][0] * points[(index + 1) % len(points)][1]
        - points[(index + 1) % len(points)][0] * points[index][1]
        for index in range(len(points))
    )) / 2.0


def _contour_area(contour: Any) -> float | None:
    """Return the filled area represented by outer minus inner contours."""

    polygons = [polygon for polygon in _contour_polygons(contour) if len(polygon) >= 3]
    if not polygons:
        return None
    outer = max(polygons, key=_polygon_area)
    area = _polygon_area(outer) - sum(_polygon_area(polygon) for polygon in polygons if polygon is not outer)
    return round(area, 8) if area > 0.0 else None


def _horizontal_extent(
    polygon: Sequence[tuple[float, float]],
    y: float,
) -> tuple[float, float] | None:
    """Intersect a polygon with a horizontal line and return its x extent."""

    if len(polygon) < 2:
        return None
    xs: list[float] = []
    eps = 1e-8
    for first, second in zip(polygon, (*polygon[1:], polygon[0])):
        x1, y1 = first
        x2, y2 = second
        if abs(y2 - y1) <= eps:
            if abs(y - y1) <= eps:
                xs.extend((x1, x2))
            continue
        if min(y1, y2) - eps <= y <= max(y1, y2) + eps:
            ratio = (y - y1) / (y2 - y1)
            if -eps <= ratio <= 1.0 + eps:
                xs.append(x1 + ratio * (x2 - x1))
    if len(xs) < 2:
        return None
    return min(xs), max(xs)


def _conventional_contour_geometry(contour: Any) -> dict[str, float]:
    """Derive box-section dimensions from the persisted outer/inner contours.

    ``Section.prop`` is ``None`` for the current PyOSIS runtime objects; the
    contour is therefore the authoritative source for wall thicknesses.  The
    returned keys use the same half-width/side-thickness convention as the
    source evaluator's ``CONVENTIONALBOX`` rows.
    """

    polygons = [polygon for polygon in _contour_polygons(contour) if len(polygon) >= 3]
    if not polygons:
        return {}
    outer = max(polygons, key=_polygon_area)
    inner_candidates = [polygon for polygon in polygons if polygon is not outer]
    inner = max(inner_candidates, key=_polygon_area) if inner_candidates else None
    ox = [point[0] for point in outer]
    oy = [point[1] for point in outer]
    result: dict[str, float] = {}
    outer_left, outer_right = min(ox), max(ox)
    outer_bottom, outer_top = min(oy), max(oy)
    result["outer_width"] = outer_right - outer_left
    result["outer_height"] = outer_top - outer_bottom
    if inner is None:
        return result

    ix = [point[0] for point in inner]
    iy = [point[1] for point in inner]
    inner_left, inner_right = min(ix), max(ix)
    inner_bottom, inner_top = min(iy), max(iy)
    if outer_top > inner_top:
        result["tt"] = outer_top - inner_top
    if inner_bottom > outer_bottom:
        result["tb"] = inner_bottom - outer_bottom

    # At one quarter of the opening height the box webs are away from the
    # sloped corner transitions, making this a stable wall-thickness sample.
    sample_y = inner_bottom + 0.25 * (inner_top - inner_bottom)
    outer_extent = _horizontal_extent(outer, sample_y)
    inner_extent = _horizontal_extent(inner, sample_y)
    if outer_extent and inner_extent:
        left_t = inner_extent[0] - outer_extent[0]
        right_t = outer_extent[1] - inner_extent[1]
        web_values = [value for value in (left_t, right_t) if value > 0.0]
        if web_values:
            result["web_t"] = sum(web_values) / len(web_values)

    # Source scorer expects each side width, not the full width.  The outer
    # bottom edge supplies the bottom slab width; their difference is the
    # cantilever length on each side for a symmetric conventional box.
    bottom_extent = _horizontal_extent(outer, outer_bottom)
    if bottom_extent is not None:
        bottom_half = max(abs(bottom_extent[0]), abs(bottom_extent[1]))
        result["bottom_half"] = bottom_half
        result["bc_l"] = max(0.0, abs(outer_left) - bottom_half)
        result["bc_r"] = max(0.0, outer_right - bottom_half)
    result["top_half"] = max(abs(outer_left), abs(outer_right))

    # At the two outermost x coordinates the sloping cantilever tip reaches
    # its minimum y.  ``top - min`` is Tc, the quantity used by D5/D6.
    side_tips: list[float] = []
    for side_x in (outer_left, outer_right):
        side_ys = [y for x, y in outer if abs(x - side_x) <= 1e-7]
        if side_ys:
            side_tips.append(max(0.0, outer_top - min(side_ys)))
    if side_tips:
        result["tc"] = sum(side_tips) / len(side_tips)
    return result


def runtime_section_row(record: Mapping[str, Any]) -> dict[str, Any] | None:
    """Convert one persisted Section object to the scorer's section row."""

    if not isinstance(record, Mapping):
        return None
    values = _fields(record)
    section_type = _section_type(record)
    name = _first(record, "name", "section_name")
    no = _integer(_first(record, "no", "number", "section_no"))
    height = _round(values.get(_normal_key("height")) or values.get(_normal_key("h")), 5)
    contour = _first(record, "contour", "contour_matrix", "boundary")
    contour_width, contour_height = _contour_size(contour)
    if height is None and contour_height is not None and contour_height > 0:
        height = round(contour_height, 5)

    def n(*aliases: str) -> float | None:
        return _round(_first(record, *aliases), 5)

    row: dict[str, Any] = {
        "no": no,
        "name": str(name) if name is not None else None,
        "type": section_type,
        "girder_pos": _first(record, "girder_pos", "girder_position", "eGirderPos"),
        "h": height,
        "tb": n("tb", "Tb", "bottom_thickness", "bottomThickness", "tb1"),
        "web_t": n("web_t", "webThickness", "tw", "Tw", "tw1", "Tw1"),
        "tc_l": n("tc_l", "TcL", "left_tip_thickness", "leftTipThickness"),
        "tc_r": n("tc_r", "TcR", "right_tip_thickness", "rightTipThickness"),
        "bt_l": n("bt_l", "BtL", "top_width_left", "topWidthLeft"),
        "bt_r": n("bt_r", "BtR", "top_width_right", "topWidthRight"),
        "bb_l": n("bb_l", "BbL", "bottom_width_left", "bottomWidthLeft"),
        "bb_r": n("bb_r", "BbR", "bottom_width_right", "bottomWidthRight"),
        "tw2": n("tw2", "Tw2", "middle_web_thickness", "middleWebThickness"),
        "bc_l": n("bc_l", "BcL", "left_cantilever", "leftCantilever"),
        "bc_r": n("bc_r", "BcR", "right_cantilever", "rightCantilever"),
        "bs": n("bs", "Bs", "slab_width", "slabWidth"),
        "bm": n("bm", "Bm", "half_width", "halfWidth"),
        "bc": n("bc", "Bc", "wet_joint_half_width", "wetJointHalfWidth"),
        "bb": n("bb", "Bb", "bottom_width", "bottomWidth"),
        "bj": n("bj", "Bj", "hinge_width", "hingeWidth"),
        "tt": n("tt", "Tt", "top_thickness", "topThickness", "tt1"),
        "tt1": n("tt1", "Tt1", "top_flange_thickness", "topFlangeThickness"),
        "tt2": n("tt2", "Tt2", "top_flange_root_thickness", "topFlangeRootThickness"),
        "tw": n("tw", "Tw", "web_thickness", "webThickness", "tw1", "Tw1"),
        "bh": n("bh", "Bh", "hoof_width", "hoofWidth"),
        "hh": n("hh", "Hh", "hoof_height", "hoofHeight"),
        "yh": n("yh", "Yh", "hoof_chamfer_height", "hoofChamferHeight"),
        "x": n("x", "flange_chamfer_width", "flangeChamferWidth"),
        "xi1": n("xi1", "Xi1"),
        "yi1": n("yi1", "Yi1"),
        "xi2": n("xi2", "Xi2"),
        "yi2": n("yi2", "Yi2"),
        "flange_tip": n("flange_tip", "flangeTip"),
        "box_chamfers": _first(record, "box_chamfers", "boxChamfers") or [],
        "contour": contour or [],
        "section_area": _contour_area(contour),
    }

    # Current PyOSIS sections expose ``prop=null`` but retain the exact
    # outer/inner contour.  Recover the conventional-box fields from that
    # contour before applying the older flat-contour fallbacks below.
    if row["type"] in {"CONVENTIONALBOX", "CONVENTIONAL"} and contour:
        contour_values = _conventional_contour_geometry(contour)
        for key in ("tt", "tb", "web_t"):
            if row.get(key) is None and contour_values.get(key) is not None:
                row[key] = round(contour_values[key], 5)
        if row["tw"] is None:
            row["tw"] = row.get("web_t")
        if row["tw2"] is None:
            row["tw2"] = row.get("web_t")
        if row["tt1"] is None:
            row["tt1"] = row.get("tt")
        if row["tt2"] is None:
            row["tt2"] = row.get("tt")
        top_half = contour_values.get("top_half")
        bottom_half = contour_values.get("bottom_half")
        for key, value in (("bt_l", top_half), ("bt_r", top_half),
                           ("bb_l", bottom_half), ("bb_r", bottom_half),
                           ("bc_l", contour_values.get("bc_l")),
                           ("bc_r", contour_values.get("bc_r"))):
            if row.get(key) is None and value is not None:
                row[key] = round(value, 5)
        if row["tc_l"] is None and contour_values.get("tc") is not None:
            row["tc_l"] = round(contour_values["tc"], 5)
        if row["tc_r"] is None and contour_values.get("tc") is not None:
            row["tc_r"] = round(contour_values["tc"], 5)

    if row["web_t"] is None:
        row["web_t"] = row["tw"]
    if row["tw"] is None:
        row["tw"] = row["web_t"]
    if row["tt1"] is None:
        row["tt1"] = row["tt"]
    if row["tt2"] is None:
        row["tt2"] = row["tt"]
    if row["tb"] is None and row["type"] in {"TGIRDER", "SMALLBOX", "HOLLOWSLAB"}:
        row["tb"] = n("tb1", "bottom_flange_thickness", "bottomFlangeThickness")
    if row["bs"] is None and contour_width is not None and row["type"] in {"HOLLOWSLAB", "SMALLBOX"}:
        row["bs"] = round(contour_width, 5)
    if row["bt_l"] is None and row["type"] in {"CONVENTIONALBOX", "CONVENTIONAL"} and contour_width is not None:
        row["bt_l"] = row["bt_r"] = round(contour_width / 2.0, 5)
    if row["bb_l"] is None and row["type"] in {"CONVENTIONALBOX", "CONVENTIONAL"}:
        row["bb_l"] = row["bb_r"] = row["bt_l"]
    if row["box_chamfers"] and isinstance(row["box_chamfers"], list):
        row["box_chamfers"] = [
            (float(_number(item[0]) or 0.0), float(_number(item[1]) or 0.0))
            for item in row["box_chamfers"]
            if isinstance(item, Sequence) and not isinstance(item, (str, bytes)) and len(item) >= 2
        ]
    rebar = _first(record, "rebar")
    if isinstance(rebar, Mapping):
        row["has_web_vertical_rebar"] = bool(
            _first(rebar, "has_web_vertical_rebar", "hasWebVerticalRebar")
        )
    else:
        row["has_web_vertical_rebar"] = bool(
            _first(record, "has_web_vertical_rebar", "hasWebVerticalRebar")
        )

    return row if row["name"] is not None or row["h"] is not None else None


def _normalise_nodes(records: Any) -> dict[int, tuple[float, float, float]]:
    result: dict[int, tuple[float, float, float]] = {}
    for record in _as_list(records):
        if not isinstance(record, Mapping):
            continue
        no = _integer(_first(record, "no", "node_no", "number"))
        coordinate = _first(record, "coordinate", "coor")
        source = coordinate if isinstance(coordinate, Mapping) else record
        x = _number(_first(source, "x", "coor_x", "coordinate_x"))
        y = _number(_first(source, "y", "coor_y", "coordinate_y"))
        z = _number(_first(source, "z", "coor_z", "coordinate_z"))
        if no is not None and x is not None:
            result[no] = (x, y or 0.0, z or 0.0)
    return result


def _element_type(record: Mapping[str, Any]) -> str | None:
    raw = _enum_name(_first(record, "element_type", "elementType", "type"))
    number = _integer(raw)
    if number is not None:
        return _ELEMENT_TYPE_BY_NUMBER.get(number, str(number))
    return str(raw).upper() if raw is not None else None


def _normalise_elements(records: Any) -> tuple[list[dict[str, Any]], dict[int, float]]:
    result: list[dict[str, Any]] = []
    thicknesses: dict[int, float] = {}
    for record in _as_list(records):
        if not isinstance(record, Mapping):
            continue
        no = _integer(_first(record, "no", "element_no", "number"))
        node_vec = _first(record, "node_vec", "nodeVec", "nodes")
        node_ids = [_integer(item) for item in _as_list(node_vec)]
        node_ids = [item for item in node_ids if item is not None]
        n1 = _integer(_first(record, "node_i", "nodeI", "node1"))
        n2 = _integer(_first(record, "node_j", "nodeJ", "node2"))
        if n1 is None and node_ids:
            n1 = node_ids[0]
        if n2 is None and len(node_ids) > 1:
            n2 = node_ids[1]
        sec_vec = _first(record, "sec_vec", "secVec", "sections")
        sec_ids = [_integer(item) for item in _as_list(sec_vec)]
        sec_ids = [item for item in sec_ids if item is not None]
        if no is None:
            continue
        thickness = _number(_first(record, "component_thickness", "componentThickness", "thickness"))
        if thickness is not None:
            thicknesses[no] = thickness
        result.append(
            {
                "no": no,
                "element_type": _element_type(record),
                "node1": n1,
                "node2": n2,
                "nSec1": sec_ids[0] if sec_ids else _integer(_first(record, "section_i", "sectionI")),
                "nSec2": sec_ids[1] if len(sec_ids) > 1 else _integer(_first(record, "section_j", "sectionJ")),
                "length": _number(_first(record, "length")),
                "mat": _integer(_first(record, "mat", "material", "material_no")),
            }
        )
    return result, thicknesses


def _merge_xs(values: list[float], tolerance: float = 0.5) -> list[float]:
    if not values:
        return []
    result = [sorted(values)[0]]
    for value in sorted(values)[1:]:
        if value - result[-1] > tolerance:
            result.append(value)
    return [round(value, 4) for value in result]


def _cluster_xs(values: list[float], tolerance: float = 10.0) -> list[float]:
    if not values:
        return []
    clusters: list[list[float]] = [[sorted(values)[0]]]
    for value in sorted(values)[1:]:
        if value - clusters[-1][-1] <= tolerance:
            clusters[-1].append(value)
        else:
            clusters.append([value])
    return [round(sum(cluster) / len(cluster), 3) for cluster in clusters]


def _support_xs(records: Any, nodes: dict[int, tuple[float, float, float]]) -> list[float]:
    boundary_records = [record for record in _as_list(records) if isinstance(record, Mapping)]
    # A runtime boundary manager also contains rail/deck groups.  When the
    # backend provides constraints or a RIGID boundary, retain only records
    # that can actually represent a bearing/pier support.  Minimal fixtures
    # often have only ``entity_vec``; in that case retain the legacy behavior.
    has_support_semantics = any(
        _first(record, "constraints", "constraint", "boundary_type", "boundaryType") is not None
        and (
            _first(record, "constraints", "constraint") is not None
            or str(_enum_name(_first(record, "boundary_type", "boundaryType")) or "").upper() == "RIGID"
        )
        for record in boundary_records
    )
    xs: list[float] = []
    for record in boundary_records:
        boundary_type = str(_enum_name(_first(record, "boundary_type", "boundaryType")) or "").upper()
        constraints = _as_list(_first(record, "constraints", "constraint"))
        if has_support_semantics:
            if boundary_type == "RIGID":
                pass
            elif constraints:
                translations = [_number(value) for value in constraints[:3]]
                if len(translations) < 3 or not all(value is not None and value > 0.0 for value in translations):
                    continue
            else:
                continue
        entities = _first(record, "entity_vec", "entityVec", "nodes", "node_vec", "nodeVec")
        for node_no in _as_list(entities):
            number = _integer(node_no)
            if number in nodes:
                xs.append(nodes[number][0])
    return _merge_xs(xs)


def _spans(nodes: dict[int, tuple[float, float, float]], support_xs: list[float]) -> tuple[list[float], float | None]:
    if not nodes:
        return [], None
    axis = [coords[0] for coords in nodes.values() if abs(coords[1]) <= 0.5 and abs(coords[2]) <= 0.5]
    if not axis:
        axis = [coords[0] for coords in nodes.values()]
    x0, x1 = min(axis), max(axis)
    if x1 - x0 <= 1.0:
        return [], None
    origin = 0.0 if 0.0 < x0 < 1.0 else x0
    end = x1 + x0 if 0.0 < x0 < 1.0 else x1
    interior = [x for x in support_xs if x - x0 > 1.0 and x1 - x > 1.0]
    stations = [origin, *_cluster_xs(interior), end]
    spans = [round(b - a, 3) for a, b in zip(stations, stations[1:]) if b - a > 1.0]
    return spans, round(end - origin, 3)


def _parent_common(parent_repo: Path | None = None) -> Any | None:
    candidates: list[Path] = []
    if parent_repo is not None:
        candidates.append(Path(parent_repo).expanduser().resolve())
    # Environment variable, else the recorded configs/parent_repo.txt.  The
    # parent repo cannot be derived from this file's location: the framework
    # is a sibling of it, not a child.
    resolved = try_resolve_parent_repo()
    if resolved is not None:
        candidates.append(resolved.expanduser().resolve())
    for repo in candidates:
        src = repo / "src"
        if not src.is_dir():
            continue
        src_text = str(src)
        inserted = src_text not in sys.path
        if inserted:
            sys.path.insert(0, src_text)
        try:
            from evaluation.levels.model_conformance import common as module
            return module
        except Exception:
            if inserted:
                try:
                    sys.path.remove(src_text)
                except ValueError:
                    pass
    return None


def _is_vertical_name(value: Any) -> bool:
    return bool(value and _VERTICAL_RE.search(str(value)))


def _area_m2(value: Any) -> float | None:
    number = _number(value)
    if number is None or number <= 0:
        return None
    return number * 1e-6 if number >= 1e-2 else number


def _tendon_values(
    measurements: Mapping[str, Any],
    girder_length: float | None,
    *,
    element_lengths: Mapping[int, float] | None = None,
    element_groups: Mapping[str, set[int]] | None = None,
) -> tuple[bool | None, bool | None, float | None, dict[str, Any]]:
    tendon_keys_present = any(key in measurements for key in ("tendon_props", "tendon_shapes", "loadcases"))
    props = [item for item in _as_list(measurements.get("tendon_props")) if isinstance(item, Mapping)]
    shapes = [item for item in _as_list(measurements.get("tendon_shapes")) if isinstance(item, Mapping)]
    loadcases = [item for item in _as_list(measurements.get("loadcases")) if isinstance(item, Mapping)]
    if not tendon_keys_present:
        return None, None, None, {"available": False}

    prop_by_name = {str(_first(item, "name")): item for item in props if _first(item, "name") is not None}
    prestressed_names: set[str] = set()
    for loadcase in loadcases:
        for item in _as_list(_first(loadcase, "prestressed", "prestress", "pst")):
            if isinstance(item, Mapping):
                name = _first(item, "name", "key_name", "keyName", "tendon")
            else:
                name = item
            if name is not None:
                prestressed_names.add(str(name))
    # When loadcases are present, a defined PST entry is the runtime analogue
    # of the source scorer's ``engine.load.get(...).create('PST', ...)`` test.
    # If the backend did not persist loadcases at all, props/shapes are the
    # only available evidence and remain a useful fallback.
    has_prestress = bool(prestressed_names) if loadcases else bool(props or shapes)
    vertical = any(
        _is_vertical_name(_first(item, "name", "prompt", "key_name", "keyName"))
        for item in [*props, *shapes, *loadcases]
    )
    if prestressed_names:
        vertical = vertical or any(_is_vertical_name(name) for name in prestressed_names)
    total_kg = 0.0
    found_mass = False
    for shape in shapes:
        shape_name = _first(shape, "name")
        if prestressed_names and str(shape_name) not in prestressed_names:
            continue
        prop_name = _first(shape, "tendon_prop", "tendonProp", "prop")
        prop = prop_by_name.get(str(prop_name)) if prop_name is not None else None
        area = _area_m2(_first(prop, "tendon_area", "tendonArea")) if prop else None
        if area is None and prop:
            area = _area_m2(_first(prop, "area"))
        shape_count = _number(_first(shape, "tendon_num", "tendonNum", "num")) or 1.0
        # In the runtime object ``area`` is the area of one strand and the
        # property name/field ``tendon_num`` is the strand count (e.g. 15-20).
        # The shape's tendon_num is the number of parallel tendons.
        prop_count = _number(_first(prop, "tendon_num", "tendonNum", "num")) or 1.0
        count = shape_count * prop_count
        length = _number(_first(shape, "length"))
        group_name = _first(shape, "ele_grp", "eleGrp", "element_group", "elementGroup")
        if element_lengths is not None and element_groups is not None and group_name is not None:
            group_length = sum(element_lengths.get(int(item), 0.0) for item in element_groups.get(str(group_name), set()))
            if group_length > 1e-12:
                length = group_length
        if area is None or length is None:
            continue
        total_kg += area * count * length * _DENSITY
        found_mass = True
        vertical = vertical or _is_vertical_name(_first(shape, "ele_grp", "eleGrp"))
    if not found_mass and props:
        for prop in props:
            area = _area_m2(_first(prop, "tendon_area", "tendonArea", "area"))
            count = _number(_first(prop, "tendon_num", "tendonNum", "num")) or 1.0
            if area is not None:
                total_kg += area * count * (girder_length or 0.0) * _DENSITY
                found_mass = bool(girder_length)
    kg_per_m = total_kg / girder_length if found_mass and girder_length and girder_length > 1e-12 else None
    return has_prestress, vertical, kg_per_m, {
        "available": True,
        "prop_count": len(props),
        "shape_count": len(shapes),
        "prestressed_shape_count": len(prestressed_names) if prestressed_names else len(shapes),
        "mass_derived": found_mass,
    }


def _normalise_element_groups(groups: Any) -> dict[str, set[int]]:
    result: dict[str, set[int]] = {}
    if isinstance(groups, Mapping):
        for name, values in groups.items():
            elements = {_integer(item) for item in (values if isinstance(values, (list, tuple, set)) else [])}
            elements = {item for item in elements if item is not None}
            if elements:
                result[str(name)] = elements
        return result
    for group in _as_list(groups):
        if not isinstance(group, Mapping):
            continue
        name = _first(group, "name", "group_name", "groupName")
        elements = {
            _integer(item)
            for item in _as_list(_first(group, "elements", "element_ids", "elementIds"))
        }
        elements = {item for item in elements if item is not None}
        if name is not None and elements:
            result[str(name)] = elements
    return result


def _avg_runtime_section_area(
    sections: Sequence[Mapping[str, Any]],
    nodes: Mapping[int, tuple[float, float, float]],
    beams: Sequence[Mapping[str, Any]],
) -> float | None:
    """Length-weight section areas measured directly from runtime contours."""

    by_no = {
        _integer(section.get("no")): section
        for section in sections
        if _integer(section.get("no")) is not None and _number(section.get("section_area")) is not None
    }
    if not by_no:
        return None
    total_area_length = 0.0
    total_length = 0.0
    for beam in beams:
        first = by_no.get(_integer(beam.get("nSec1")))
        second = by_no.get(_integer(beam.get("nSec2")))
        node_i = nodes.get(_integer(beam.get("node1")))
        node_j = nodes.get(_integer(beam.get("node2")))
        if first is None or second is None or node_i is None or node_j is None:
            continue
        length = math.dist(node_i, node_j)
        if length <= 1e-12:
            continue
        area_i = _number(first.get("section_area"))
        area_j = _number(second.get("section_area"))
        if area_i is None or area_j is None:
            continue
        total_area_length += 0.5 * (area_i + area_j) * length
        total_length += length
    if total_length > 1e-12:
        return total_area_length / total_length
    values = [_number(section.get("section_area")) for section in by_no.values()]
    values = [value for value in values if value is not None]
    return sum(values) / len(values) if values else None


def _zero_blocks(
    groups: Any,
    nodes: dict[int, tuple[float, float, float]],
    beams: list[dict[str, Any]],
    *,
    parent_repo: Path | None = None,
) -> list[float]:
    group_map: dict[str, set[int]] = {}
    group_map.update(_normalise_element_groups(groups))
    if not group_map:
        return []
    parent = _parent_common(parent_repo)
    if parent is not None:
        try:
            return list(parent._zero_block_lengths(group_map, nodes, beams))
        except Exception:
            pass
    selected = {element for name, elements in group_map.items() if re.match(r"^(?:0号块|零号块)", name) for element in elements}
    spans: list[float] = []
    for element in beams:
        if element["no"] not in selected:
            continue
        coords = [nodes[node] for node in (element.get("node1"), element.get("node2")) if node in nodes]
        if len(coords) == 2:
            spans.append(abs(coords[1][0] - coords[0][0]))
    return [round(value, 3) for value in spans if value > 0]


def measurements_to_params(
    measurements: Mapping[str, Any],
    *,
    bridge_type: str | None = None,
    is_continuous: bool | None = None,
    is_prestressed: bool | None = None,
    parent_repo: Path | None = None,
) -> tuple[dict[str, Any], list[str], dict[str, Any]]:
    """Derive canonical scorer parameters from a runtime snapshot.

    No expected task value is accepted here: a value is present only when it
    can be measured from the executed model.  Task metadata may supply the
    bridge route and construction intent, exactly as it does for source
    scoring.
    """

    if not isinstance(measurements, Mapping):
        raise TypeError("measurements must be a mapping")
    sections = [row for raw in _as_list(measurements.get("sections")) if (row := runtime_section_row(raw))]
    nodes = _normalise_nodes(measurements.get("nodes"))
    beams, component_values = _normalise_elements(measurements.get("elements"))
    main_sections = [row for row in sections if (row.get("type") or "").upper() in _MAIN_SECTION_TYPES and row.get("h") is not None]
    main_section_nos = {row.get("no") for row in main_sections if row.get("no") is not None}
    beam_rows = [
        row for row in beams
        if row.get("element_type") in (None, "BEAM3D")
        and row.get("node1") in nodes and row.get("node2") in nodes
    ]
    support_xs = _support_xs(measurements.get("boundaries"), nodes)
    span_lengths, design_total = _spans(nodes, support_xs)
    total_length = design_total
    if total_length is None and nodes:
        total_length = round(max(item[0] for item in nodes.values()) - min(item[0] for item in nodes.values()), 4)

    parent = _parent_common(parent_repo)
    height_profile: list[tuple[float, float]] = []
    thickness_profile: list[tuple[float, float]] = []
    area_type = next(
        (name for name in ("CONVENTIONALBOX", "TGIRDER", "SMALLBOX", "HOLLOWSLAB") if any((row.get("type") or "").upper() == name for row in main_sections)),
        "TGIRDER",
    )
    if parent is not None:
        try:
            height_profile = list(parent._build_height_profile(main_sections, nodes, beam_rows))
            thickness_profile = list(parent._build_thickness_profile(main_sections, nodes, beam_rows))
        except Exception:
            height_profile, thickness_profile = [], []
    if not height_profile:
        by_no = {row.get("no"): row for row in main_sections}
        for beam in beam_rows:
            for node_no, sec_no in ((beam.get("node1"), beam.get("nSec1")), (beam.get("node2"), beam.get("nSec2"))):
                section = by_no.get(sec_no)
                if section and node_no in nodes and section.get("h") is not None:
                    height_profile.append((round(nodes[node_no][0], 4), float(section["h"])))
                if section and node_no in nodes and section.get("tb") is not None:
                    thickness_profile.append((round(nodes[node_no][0], 4), float(section["tb"])))
        height_profile.sort()
        thickness_profile.sort()

    heights = sorted((float(row["h"]) for row in main_sections if row.get("h") is not None), reverse=True)
    thicknesses = sorted((float(row["tb"]) for row in main_sections if row.get("tb") is not None), reverse=True)
    h_root = max(heights) if heights else None
    h_mid = min(heights) if heights else None
    root_sections = [row for row in main_sections if row.get("h") == h_root]
    mid_sections = [row for row in main_sections if row.get("h") == h_mid]
    t_root_values = [row["tb"] for row in root_sections if row.get("tb") is not None]
    t_mid_values = [row["tb"] for row in mid_sections if row.get("tb") is not None]
    web_root_values = [row["web_t"] for row in root_sections if row.get("web_t") is not None]
    web_mid_values = [row["web_t"] for row in mid_sections if row.get("web_t") is not None]
    top_root_values = [row["tt"] for row in root_sections if row.get("tt") is not None]
    top_mid_values = [row["tt"] for row in mid_sections if row.get("tt") is not None]
    flange_tips = [row[key] for row in main_sections for key in ("tc_l", "tc_r") if row.get(key) is not None]

    def pair(section_type: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
        if parent is not None:
            try:
                return parent._pick_girder_pair(main_sections, section_type)
            except Exception:
                pass
        typed = [row for row in main_sections if (row.get("type") or "").upper() == section_type]
        if not typed:
            return None, None
        mid = min(typed, key=lambda row: row.get("tw") if row.get("tw") is not None else float("inf"))
        support = max(typed, key=lambda row: row.get("tw") if row.get("tw") is not None else float("-inf"))
        return mid, support if support is not mid else None

    tg_mid, tg_support = pair("TGIRDER")
    sb_mid, sb_support = pair("SMALLBOX")
    hs_mid, hs_support = pair("HOLLOWSLAB")
    section_area_avg = None
    if parent is not None:
        try:
            section_area_avg = parent._avg_girder_section_area(main_sections, nodes, beam_rows, area_type)
        except Exception:
            section_area_avg = None
    contour_area_avg = _avg_runtime_section_area(main_sections, nodes, beam_rows)
    if contour_area_avg is not None:
        # Runtime contours are the executed model's geometry.  Prefer their
        # filled area over the source evaluator's component approximation;
        # fall back to the parent formula when a backend omits contours.
        section_area_avg = contour_area_avg
    beam_width = None
    prefab = tg_mid or sb_mid or hs_mid
    if parent is not None:
        try:
            beam_width = parent._beam_width_from_section(prefab)
        except Exception:
            beam_width = None
    if beam_width is None:
        beam_width = _round(_first(prefab, "beam_width", "width", "bs")) if prefab else None
    component_thickness_avg = (
        round(sum(component_values.values()) / len(component_values), 5) if component_values else None
    )
    concrete_ratio = (
        round(section_area_avg / beam_width, 5)
        if section_area_avg is not None and beam_width and beam_width > 1e-12
        else component_thickness_avg
    )
    if section_area_avg is None and concrete_ratio is not None and beam_width:
        section_area_avg = round(concrete_ratio * beam_width, 5)

    girder_length = 0.0
    element_lengths: dict[int, float] = {}
    for beam in beam_rows:
        n1, n2 = nodes[beam["node1"]], nodes[beam["node2"]]
        length = math.dist(n1, n2)
        if length > 1e-12:
            element_lengths[int(beam["no"])] = length
        if beam.get("nSec1") in main_section_nos and beam.get("nSec2") in main_section_nos:
            girder_length += length
    if girder_length <= 1e-12:
        girder_length = total_length or 0.0
    element_groups = _normalise_element_groups(measurements.get("element_groups"))
    has_prestress, has_vertical, pst_kg_m, tendon_provenance = _tendon_values(
        measurements,
        girder_length if girder_length > 1e-12 else None,
        element_lengths=element_lengths,
        element_groups=element_groups,
    )
    if has_vertical is not None and any(row.get("has_web_vertical_rebar") for row in main_sections):
        has_vertical = True
    stage_records = [item for item in _as_list(measurements.get("stages")) if isinstance(item, Mapping)]
    stage_names = [str(_first(item, "name", "stage_name")) for item in stage_records if _first(item, "name", "stage_name")]
    summary = measurements.get("summary") if isinstance(measurements.get("summary"), Mapping) else {}
    count = lambda key, records: _integer(summary.get(key)) or len(records)
    section_count = count("sections", _as_list(measurements.get("sections")))
    node_count = count("nodes", _as_list(measurements.get("nodes")))
    element_count = count("elements", _as_list(measurements.get("elements")))
    boundary_count = count("boundaries", _as_list(measurements.get("boundaries")))
    stage_count = count("stages", stage_records) if ("stages" in measurements or stage_records) else _integer(summary.get("stages"))
    if stage_count is None:
        stage_count = 0
    if is_continuous is None:
        is_continuous = stage_count >= 4

    side_spans = main_span_from_nodes = pier_heights = pier_source = None
    if parent is not None:
        try:
            from evaluation.levels.model_conformance.bridges.rigid_frame import _extract_rigid_frame_geometry

            rigid = _extract_rigid_frame_geometry(nodes, main_sections, beam_rows, None)
            side_spans = rigid.get("side_spans")
            main_span_from_nodes = rigid.get("main_span")
            pier_heights = rigid.get("pier_heights")
            pier_source = rigid.get("pier_source")
        except Exception:
            pass
    if parent is not None:
        try:
            L = parent._main_span_from_nodes(span_lengths, total_length, main_span_from_piers=main_span_from_nodes)
        except Exception:
            L = max(span_lengths) if span_lengths else total_length
    else:
        L = max(span_lengths) if span_lengths else total_length
    zero_block_lens = _zero_blocks(
        element_groups,
        nodes,
        beam_rows,
        parent_repo=parent_repo,
    )
    zero_block_len = max(zero_block_lens, key=lambda value: max(9.0 - value, value - 14.0, 0.0)) if zero_block_lens else None
    concrete_grade = None
    grades: list[int] = []
    for material in _as_list(measurements.get("materials")):
        if not isinstance(material, Mapping):
            continue
        kind = str(_enum_name(_first(material, "material_type", "materialType", "type")) or "").upper()
        grade_texts = [
            str(value)
            for value in (
                _first(material, "name", "material_name"),
                _first(material, "grade", "material_grade"),
            )
            if value is not None
        ]
        grade_text = " ".join(grade_texts)
        if "CONC" in kind or "CONCRETE" in kind or _CONCRETE_RE.search(grade_text):
            grades.extend(int(match.group(1)) for match in _CONCRETE_RE.finditer(grade_text))
    concrete_grade = max(grades) if grades else None
    void_ratio = None
    if parent is not None:
        try:
            void_ratio = parent._hollow_void_ratio(hs_mid)
        except Exception:
            pass
    if void_ratio is None and hs_mid:
        h, bs, tw, tt, tb = (hs_mid.get(key) for key in ("h", "bs", "tw", "tt", "tb"))
        if all(value is not None for value in (h, bs, tw, tt, tb)) and bs and h:
            void_ratio = max(0.0, (bs - 2 * tw) * (h - tt - tb) / (bs * h))

    params: dict[str, Any] = {key: None for key in CANONICAL_PARAM_KEYS}
    params.update(
        {
            "bridge_type": bridge_type,
            "L": None if L is None else round(float(L), 4),
            "is_continuous": bool(is_continuous) if is_continuous is not None else None,
            "is_prestressed": bool(is_prestressed) if is_prestressed is not None else None,
            "H_root": h_root,
            "H_mid": h_mid,
            "T_root": min(t_root_values) if t_root_values else None,
            "T_dia_root": max(t_root_values) if t_root_values else None,
            "T_mid": min(t_mid_values) if t_mid_values else None,
            "T_top_mid": min(top_mid_values) if top_mid_values else None,
            "T_top_root": max(top_root_values) if top_root_values else None,
            "web_t_mid": min(web_mid_values) if web_mid_values else None,
            "web_t_support": max(web_root_values) if web_root_values else None,
            "flange_tip": round(sum(flange_tips) / len(flange_tips), 5) if flange_tips else None,
            "t_girder_mid": tg_mid,
            "t_girder_support": tg_support,
            "small_box_mid": sb_mid,
            "small_box_support": sb_support,
            "hollow_slab_mid": hs_mid,
            "hollow_slab_support": hs_support,
            "concrete_ratio": concrete_ratio,
            "component_thickness_avg": component_thickness_avg,
            "beam_width": beam_width,
            "section_area_avg": section_area_avg,
            "pst_steel_kg_per_m": pst_kg_m,
            "pst_steel_ratio": round(pst_kg_m / section_area_avg, 5) if pst_kg_m is not None and section_area_avg and section_area_avg > 1e-12 else None,
            "void_ratio": void_ratio,
            "stage_names": stage_names,
            "total_length": total_length,
            "has_vertical_tendon": has_vertical,
            "has_prestress": has_prestress,
            "heights": heights,
            "thicknesses": thicknesses,
            "thickness_profile": [(round(float(x), 4), round(float(t), 5)) for x, t in thickness_profile],
            "section_count": section_count,
            "girder_section_count": len(main_sections),
            "section_types": sorted({str(row.get("type")) for row in sections if row.get("type")}),
            "node_count": node_count,
            "element_count": element_count,
            "boundary_count": boundary_count,
            "stage_count": stage_count,
            "support_xs": support_xs,
            "height_profile": [(round(float(x), 4), round(float(h), 5)) for x, h in height_profile],
            "side_spans": side_spans,
            "main_span_from_nodes": main_span_from_nodes,
            "pier_heights": pier_heights,
            "pier_source": pier_source,
            "span_lengths": span_lengths or None,
            "concrete_grade": concrete_grade,
            "zero_block_len": zero_block_len,
            "zero_block_lens": zero_block_lens,
        }
    )
    missing = [
        key for key, value in params.items()
        if value is None or (isinstance(value, (list, tuple, dict)) and len(value) == 0)
    ]
    provenance = {
        "source": "pyosis_runtime_snapshot",
        "extractor_version": EXTRACTOR_VERSION,
        "schema_version": measurements.get("schema_version", "osis-runtime-measurements-v1"),
        "manager_counts": dict(summary),
        "tendon": tendon_provenance,
        "section_area_source": "runtime_contour" if contour_area_avg is not None else "parent_section_formula",
        "support_source": "runtime_boundary_constraints" if any(
            _first(record, "constraints", "constraint") is not None
            for record in _as_list(measurements.get("boundaries"))
            if isinstance(record, Mapping)
        ) else "runtime_boundary_entities",
        "parent_common_available": parent is not None,
        "missing_params": missing,
    }
    return params, missing, provenance


__all__ = [
    "EXTRACTOR_VERSION",
    "CANONICAL_PARAM_KEYS",
    "load_measurements",
    "measurements_to_params",
    "runtime_section_row",
]
