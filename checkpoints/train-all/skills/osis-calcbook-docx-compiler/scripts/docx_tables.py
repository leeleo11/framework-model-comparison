"""docx_tables：表格整表重建域。

**现行方案（20260902 用户终审）**：`rebuild_table` 以 OSIS 输出为准整表重建——
表头、列数、顺序和全部数据行来自项目输出（与 OSIS 官方计算书 JTG 宏
Get_JTG_Table_材料/钢束属性/… 的表头完全一致），模板表只提供样式骨架
（tblPr/列宽/单元格格式）。项目没有该表输出时：保留模板表头并补一行
红色 "/" 占位（MISSING_TABLE_MARK，用户约定），不保留旧数据。

备选实现：`rebuild_table_template_headers`（保留模板表头/列序，项目数据按
语义列名映射 + 单位换算填充）——20260902 曾启用，现备份待用，主流程不调用。
"""
from __future__ import annotations

import copy
import os
import re
import sys
from typing import Any, Optional

from lxml import etree

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from compile_mapping import _column_key  # noqa: E402,F401 （rebuild_table_template_headers 备用）
from ooxml_utils import qn, _fmt_value, _set_run_text, _make_run_red  # noqa: E402

#: 缺表占位：项目确实没有该表输出时，表头下补一行红色 "/"（用户约定）。
MISSING_TABLE_MARK = "空"

# ---------------------------------------------------------------- 表格整表重建
def _parse_xml_fragment(s: str) -> Optional[etree._Element]:
    if not s:
        return None
    try:
        return etree.fromstring(s.encode("utf-8"))
    except etree.XMLSyntaxError:
        return None


def _cell_text_of(tc: etree._Element) -> str:
    return "".join(t.text or "" for t in tc.iter(qn("w:t")))


def _set_cell_text(tc: etree._Element, text: str) -> None:
    ps = tc.findall(qn("w:p"))
    if not ps:
        return
    first = True
    for p in ps:
        runs = p.findall(qn("w:r"))
        if first and runs:
            _set_run_text(runs[0], text)
            first = False
            # 清空多余 run
            for r in runs[1:]:
                for t in r.findall(qn("w:t")):
                    t.text = None
        else:
            for r in runs:
                for t in r.findall(qn("w:t")):
                    t.text = None


def _row_from_sample(
    sample: Optional[etree._Element], values: list[Any], *, header: bool,
    fill_empty_red: bool = False,
) -> etree._Element:
    """按项目列数缩放样例行，只复用原表格的单元格样式。

    fill_empty_red=True 时（数据行），对不上/缺值的空格填红色 "空"，不留空白。
    """
    tr = copy.deepcopy(sample) if sample is not None else etree.Element(qn("w:tr"))
    cells = tr.findall(qn("w:tc"))
    if not cells:
        tc = etree.SubElement(tr, qn("w:tc"))
        etree.SubElement(tc, qn("w:tcPr"))
        p = etree.SubElement(tc, qn("w:p"))
        r = etree.SubElement(p, qn("w:r"))
        etree.SubElement(r, qn("w:t"))
        cells = [tc]

    while len(cells) > len(values):
        tr.remove(cells.pop())
    while len(cells) < len(values):
        new_cell = copy.deepcopy(cells[-1])
        tr.append(new_cell)
        cells.append(new_cell)

    tr_pr = tr.find(qn("w:trPr"))
    if not header and tr_pr is not None:
        marker = tr_pr.find(qn("w:tblHeader"))
        if marker is not None:
            tr_pr.remove(marker)
    for tc, value in zip(cells, values):
        text = _fmt_value(value)
        _set_cell_text(tc, text)
        if fill_empty_red and not text.strip():
            # 对不上的空格：填红色 "空"，不留空白（用户约定）
            _set_cell_text(tc, "空")
            for r in tc.iter(qn("w:r")):
                _make_run_red(r)
    return tr


def _grid_for_columns(
    sample: Optional[etree._Element], column_count: int,
) -> Optional[etree._Element]:
    """保留原表总宽度，按项目列数等分 tblGrid；无样例时按默认总宽新建。

    w:tblGrid 是 OOXML 表格的必需子元素，缺失会被 Word 判为无效表格。
    """
    if column_count <= 0:
        return None
    if sample is not None:
        grid = copy.deepcopy(sample)
        columns = grid.findall(qn("w:gridCol"))
        widths = []
        for column in columns:
            try:
                widths.append(float(column.get(qn("w:w")) or 0))
            except ValueError:
                pass
        total = round(sum(widths))
        for column in columns:
            grid.remove(column)
    else:
        grid = etree.Element(qn("w:tblGrid"))
        total = 9000
    width = max(1, int(total) // column_count)
    for _ in range(column_count):
        column = etree.SubElement(grid, qn("w:gridCol"))
        column.set(qn("w:w"), str(width))
    return grid


def _normalize_table_property_order(tbl: etree._Element) -> None:
    """纠正模板样式片段中 Word 会拒绝的两个常见属性顺序。"""
    tbl_pr = tbl.find(qn("w:tblPr"))
    if tbl_pr is not None:
        cell_mar = tbl_pr.find(qn("w:tblCellMar"))
        look = tbl_pr.find(qn("w:tblLook"))
        if (cell_mar is not None and look is not None
                and tbl_pr.index(cell_mar) > tbl_pr.index(look)):
            tbl_pr.remove(cell_mar)
            tbl_pr.insert(tbl_pr.index(look), cell_mar)
    for p_pr in tbl.iter(qn("w:pPr")):
        spacing = p_pr.find(qn("w:spacing"))
        jc = p_pr.find(qn("w:jc"))
        if (spacing is not None and jc is not None
                and p_pr.index(spacing) > p_pr.index(jc)):
            p_pr.remove(spacing)
            p_pr.insert(p_pr.index(jc), spacing)


# ---------------------------------------------------------------- 备用实现（未调用）
def rebuild_table_template_headers(style_xml: dict, project_table: Optional[dict]) -> tuple[etree._Element, bool]:
    """【备用】保留模板表头/列序，按语义列名映射项目 ``header/data``。

    返回 ``(tbl, missing)``。部分列匹配时填已匹配列、其余留空；零列匹配
    才视为整表缺失。已知的 N/m²→MPa、N/m³→kN/m³、m→mm 在填值时换算。
    """
    tbl = etree.Element(qn("w:tbl"))
    tbl_pr = _parse_xml_fragment(style_xml.get("tbl_pr", ""))
    grid = _parse_xml_fragment(style_xml.get("tbl_grid", ""))
    header_sample = _parse_xml_fragment(style_xml.get("header_row", ""))
    data_sample = _parse_xml_fragment(style_xml.get("data_row", ""))
    if tbl_pr is not None:
        tbl.append(copy.deepcopy(tbl_pr))

    template_header = [
        _cell_text_of(tc) for tc in header_sample.findall(qn("w:tc"))
    ] if header_sample is not None else []

    project_header = list((project_table or {}).get("header", []))
    project_data = list((project_table or {}).get("data", []))
    output_header = template_header or project_header
    column_mapping = (
        _table_column_mapping(output_header, project_header)
        if output_header and project_header else []
    )
    matched = sum(index is not None for index in column_mapping)
    missing = not output_header or not project_data or matched == 0
    if missing:
        resized_grid = _grid_for_columns(grid, len(output_header))
        if resized_grid is not None:
            tbl.append(resized_grid)
        if header_sample is not None:
            tbl.append(copy.deepcopy(header_sample))
        tbl.append(_make_missing_table_row(style_xml))
    else:
        resized_grid = _grid_for_columns(grid, len(output_header))
        if resized_grid is not None:
            tbl.append(resized_grid)
        tbl.append(_row_from_sample(header_sample, output_header, header=True))
        row_sample = data_sample if data_sample is not None else header_sample
        for row in project_data:
            values = []
            for target_name, source_index in zip(output_header, column_mapping):
                if source_index is None or source_index >= len(row):
                    values.append("")
                    continue
                values.append(_convert_table_value(
                    row[source_index], project_header[source_index], target_name,
                ))
            tbl.append(_row_from_sample(row_sample, values, header=False))
    _normalize_table_property_order(tbl)
    return tbl, missing


def _table_unit(name: Any) -> str:
    """提取并归一化表头单位，仅覆盖计算书当前需要的换算组。"""
    match = re.search(r"[（(]([^（）()]*)[)）]\s*$", str(name))
    if not match:
        return ""
    return re.sub(r"[\s^·*]", "", match.group(1)).casefold()


def _convert_table_value(value: Any, source_header: Any, target_header: Any) -> Any:
    """按表头单位换算数值；无法识别时原样返回。"""
    source_unit = _table_unit(source_header)
    target_unit = _table_unit(target_header)
    if not source_unit or not target_unit or source_unit == target_unit:
        return value
    factor = None
    if source_unit in {"n/m2", "n/m²"} and target_unit == "mpa":
        factor = 1e-6
    elif source_unit in {"n/m3", "n/m³"} and target_unit in {"kn/m3", "kn/m³"}:
        factor = 1e-3
    elif source_unit == "m" and target_unit == "mm":
        factor = 1000.0   # 波纹管外直径(m)/回缩变形(m) → 模板 mm 列
    if factor is None:
        return value
    try:
        converted = float(str(value).strip()) * factor
    except (TypeError, ValueError):
        return value
    return f"{converted:.12g}"


def _table_column_mapping(
    template_header: list[Any], project_header: list[Any],
) -> list[Optional[int]]:
    """模板列 → 项目列；按语义主体键匹配，并保证一个项目列只使用一次。"""
    mapping: list[Optional[int]] = []
    used: set[int] = set()
    project_keys = [_column_key(name) for name in project_header]
    for name in template_header:
        key = _column_key(name)
        index = next(
            (i for i, project_key in enumerate(project_keys)
             if i not in used and project_key == key),
            None,
        )
        mapping.append(index)
        if index is not None:
            used.add(index)
    return mapping


def rebuild_table(
    style_xml: dict, project_table: Optional[dict],
) -> tuple[etree._Element, bool]:
    """【现行】以 OSIS 输出为准整表重建：表头、列数、顺序和全部数据行来自
    项目，模板表只提供样式骨架（tblPr/列宽/单元格格式）——与 OSIS 官方
    计算书 JTG 宏（Get_JTG_Table_材料/钢束属性/…）输出的表头完全一致。
    项目没有该表输出时：保留模板表头并补一行红色"空"占位（整行每格）。
    """
    tbl = etree.Element(qn("w:tbl"))
    tbl_pr = _parse_xml_fragment(style_xml.get("tbl_pr", ""))
    grid = _parse_xml_fragment(style_xml.get("tbl_grid", ""))
    header_sample = _parse_xml_fragment(style_xml.get("header_row", ""))
    data_sample = _parse_xml_fragment(style_xml.get("data_row", ""))
    if tbl_pr is not None:
        tbl.append(copy.deepcopy(tbl_pr))

    template_header = [
        _cell_text_of(tc) for tc in header_sample.findall(qn("w:tc"))
    ] if header_sample is not None else []

    project_header = list((project_table or {}).get("header", []))
    project_data = list((project_table or {}).get("data", []))
    missing = not project_header or not project_data
    if missing:
        resized_grid = _grid_for_columns(grid, len(template_header))
        if resized_grid is not None:
            tbl.append(resized_grid)
        if header_sample is not None:
            tbl.append(copy.deepcopy(header_sample))
        tbl.append(_make_missing_table_row(style_xml))
    else:
        resized_grid = _grid_for_columns(grid, len(project_header))
        if resized_grid is not None:
            tbl.append(resized_grid)
        tbl.append(_row_from_sample(header_sample, project_header, header=True))
        row_sample = data_sample if data_sample is not None else header_sample
        for row in project_data:
            values = [row[i] if i < len(row) else "" for i in range(len(project_header))]
            tbl.append(_row_from_sample(row_sample, values, header=False,
                                        fill_empty_red=True))
    _normalize_table_property_order(tbl)
    return tbl, missing



def _make_missing_table_row(style_xml: dict) -> etree._Element:
    """项目确实没有该表输出：表头下补一行红色"空"占位（整行每格），不保留旧项目数据。"""
    sample = _parse_xml_fragment(style_xml.get("data_row", ""))
    if sample is None:
        sample = _parse_xml_fragment(style_xml.get("header_row", ""))
    if sample is None:
        sample = etree.Element(qn("w:tr"))
    tr = copy.deepcopy(sample)
    trPr = tr.find(qn("w:trPr"))
    if trPr is not None:
        hl = trPr.find(qn("w:tblHeader"))
        if hl is not None:
            trPr.remove(hl)
    for tc in tr.findall(qn("w:tc")):
        _set_cell_text(tc, MISSING_TABLE_MARK)
        for r in tc.iter(qn("w:r")):
            _make_run_red(r)
    return tr


