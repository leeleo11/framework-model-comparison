"""render：固定 Runtime（零 AI、零语义判断）。

只做：Tag → mapping → 当前项目内容 → DOCX。
- 加载模板包（template.docx + mapping.json），SHA 校验；
- 构建当前项目 project_brief（facts/tables/images/load_combinations）；
- 遍历 mapping.slots：
    text   → resolve(field:) 替换 【TAG】；无数据 → 红【当前项目未输出该数据】
    format → resolve(sources) + .format(template) 替换 【TAG】；无数据 → 红
    table  → docx_tables.rebuild_table 整表重建；无输出 → 表头 + 红"空"行（整行）
    image  → docx_images 按槽宽/比例插图 + caption_name 填 OSIS 输出图名；
             无图/坏图 → 清槽和旧图名并标红
    conclusion → render_checks 解析验算表 + 固定句式确定性生成
- 文末追加红色“OSIS验算建议”；输出前校验：无 【…】 标签残留。

支撑域模块：docx_tables.py（表格）、docx_images.py（插图）、
render_checks.py（验算解析）、ooxml_utils.py（OOXML 原语）。
本文件保留主流程、source 解析、repeat 列表、输出自检与 CLI。

禁止：模板语义分析、LLM 调用、动态候选判断、根据原文字猜数据源、推导块号。

用法：
    python render.py --package PKG --project PROJ -o 计算书.docx
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import uuid
from typing import Any, Optional

from lxml import etree

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from compile_mapping import (  # noqa: E402
    TAG_RE, load_package,
    _normalize_col, _column_key, _replace_span_in_para,
)
from ooxml_utils import (  # noqa: E402
    DocxPackage, qn, para_text, _update_content_type,
    RED, _fmt_value, _set_run_texts_in_para, _make_run_red, _make_para_red,
    _strip_tag_red,
)
from project_brief import ProjectData, build_project_brief  # noqa: E402
from docx_images import (  # noqa: E402
    _image_pixel_size, _page_available_emu, _compute_image_extent,
    _add_image_rels_and_media, _reseed_drawing_ids,
    build_picture_drawing, _replace_para_with_drawing,
)
from docx_tables import (  # noqa: E402,F401 （re-export：外部/测试仍从 render 取）
    MISSING_TABLE_MARK, _parse_xml_fragment, _cell_text_of, rebuild_table,
    _make_missing_table_row, _table_column_mapping, _convert_table_value,
)
from render_checks import _norm_verdict, _resolve_verdict  # noqa: E402,F401

MISSING_TEXT = "【当前项目未输出该数据】"
MISSING_IMAGE = "【当前项目未输出该图片】"
#: 残留扫描与替换用同一套标签命名规则（模块-内容），统一定义于 standard_dict。
RUNTIME_TAG_RE = TAG_RE

def _set_update_fields(settings: etree._Element) -> None:
    """请求 Word/WPS 打开文档时刷新 SEQ 图表编号等域。"""
    nodes = settings.findall(qn("w:updateFields"))
    node = nodes[0] if nodes else etree.Element(qn("w:updateFields"))
    for existing in nodes:
        settings.remove(existing)
    node.set(qn("w:val"), "true")
    later_settings = {
        "hdrShapeDefaults", "footnotePr", "endnotePr", "compat", "docVars",
        "rsids", "mathPr", "attachedSchema", "themeFontLang", "clrSchemeMapping",
        "doNotIncludeSubdocsInStats", "doNotAutoCompressPictures", "forceUpgrade",
        "captions", "readModeInkLockDown", "schemaLibrary", "shapeDefaults",
        "doNotEmbedSmartTags", "decimalSymbol", "listSeparator",
    }
    insert_at = next(
        (i for i, child in enumerate(settings)
         if etree.QName(child).localname in later_settings),
        len(settings),
    )
    settings.insert(insert_at, node)


def _request_field_update(pkg: DocxPackage) -> None:
    if "word/settings.xml" not in pkg.parts:
        return
    settings = pkg.get_xml("word/settings.xml")
    _set_update_fields(settings)
    pkg.mark_dirty("word/settings.xml")


def _refresh_seq_field_results(root: etree._Element) -> dict[str, int]:
    """保留 SEQ 域，并同步其缓存编号，避免未刷新域时全部显示为 1。"""
    counters: dict[str, int] = {}
    stack: list[dict[str, Any]] = []
    for node in root.iter():
        if node.tag == qn("w:fldChar"):
            field_type = node.get(qn("w:fldCharType"))
            if field_type == "begin":
                stack.append({"instruction": [], "results": [], "separated": False})
            elif stack and field_type == "separate":
                stack[-1]["separated"] = True
            elif stack and field_type == "end":
                field = stack.pop()
                instruction = "".join(field["instruction"])
                match = re.match(r'^\s*SEQ\s+(?:"([^"]+)"|(\S+))', instruction, re.I)
                if not match or not field["results"]:
                    continue
                name = match.group(1) or match.group(2)
                reset = re.search(r"\\r\s+(-?\d+)", instruction, re.I)
                repeat_current = re.search(r"\\c(?:\s|$)", instruction, re.I)
                if reset:
                    value = int(reset.group(1))
                    counters[name] = value
                elif repeat_current:
                    value = counters.get(name, 0)
                else:
                    value = counters.get(name, 0) + 1
                    counters[name] = value
                field["results"][0].text = str(value)
                for extra in field["results"][1:]:
                    extra.text = ""
        elif stack and node.tag == qn("w:instrText") and not stack[-1]["separated"]:
            stack[-1]["instruction"].append(node.text or "")
        elif stack and node.tag == qn("w:t") and stack[-1]["separated"]:
            stack[-1]["results"].append(node)
    return counters

# ---------------------------------------------------------------- source 解析
def _field_value(brief: dict, ref: str, project: Any) -> Any:
    """解析 field: 数据源。优先 brief facts（英文键），回退 项目数据结构.json 点路径。"""
    facts = brief.get("facts", {})
    if ref in facts:
        entry = facts[ref]
        return entry.get("value") if isinstance(entry, dict) else entry
    # 回退：project 是 datasource.ProjectData；项目无 项目数据结构.json 时为
    # None（新项目未导出数据），此时一律视为缺失（红字），不得崩溃。
    if project is None:
        return None
    try:
        _, value = project.resolve(f"field:{ref}")
        return value
    except (KeyError, ValueError, AttributeError):
        return None


def _table_data(project_dir: str, brief: dict, ref: str) -> Optional[dict]:
    """解析 table: 数据源，返回 {header, data}；不存在返回 None。

    `table:荷载工况/荷载组合` 从 Project Brief 的日志提取结果构造。
    """
    if ref == "荷载工况":
        cases = brief.get("load_cases") or []
        header = ["序号", "工况名称", "描述"]
        data = [[c.get("id", ""), c.get("name", ""), c.get("description", "")]
                for c in cases]
        return {"header": header, "data": data} if cases else None

    if ref == "荷载组合":
        combos = brief.get("load_combinations") or []
        header = ["组合编号", "组合类型", "荷载组合公式"]
        data = [[c.get("id", ""), c.get("type_name", ""), c.get("chinese", "")]
                for c in combos]
        return {"header": header, "data": data} if combos else None

    if ref.startswith("材料参数."):
        base = _table_data(project_dir, brief, "材料参数")
        if base is None:
            return None
        header = list(base.get("header", []))
        name_idx = next(
            (i for i, name in enumerate(header)
             if _normalize_col(name) in {_normalize_col("材料名称"), _normalize_col("名称")}),
            None,
        )
        if name_idx is None:
            return None
        view = ref.partition(".")[2]

        def belongs(name: str) -> bool:
            text = name.strip().casefold()
            if view == "混凝土":
                return "混凝土" in text or bool(re.fullmatch(r"c\d+", text))
            if view == "普通钢筋":
                return text.startswith(("hrb", "hpb", "rrb")) or "普通钢筋" in text
            if view == "预应力钢材":
                return any(word in text for word in ("钢绞线", "钢丝", "strand", "预应力"))
            return False

        data = [
            row for row in base.get("data", [])
            if name_idx < len(row) and belongs(str(row[name_idx]))
        ]
        return {"header": header, "data": data} if data else None

    table = brief.get("tables", {}).get(ref)
    if not isinstance(table, dict):
        return None
    path = table.get("path") or table.get("source", "").replace("table:", "", 1)
    if not path:
        return None
    full = os.path.join(project_dir, path.replace("/", os.sep))
    if not os.path.isfile(full):
        return None
    try:
        with open(full, "r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(obj, dict) or "header" not in obj:
        return None
    return {"header": list(obj.get("header", [])), "data": list(obj.get("data", []))}


def _image_path(project_dir: str, brief: dict, ref: str) -> Optional[str]:
    """解析 image: 数据源，返回绝对路径；不存在返回 None。"""
    img = brief.get("images", {}).get(ref)
    if not isinstance(img, dict):
        return None
    rel = img.get("path")
    if not rel:
        return None
    full = os.path.join(project_dir, rel.replace("/", os.sep))
    return full if os.path.isfile(full) else None


# ---------------------------------------------------------------- 图片尺寸
# ---------------------------------------------------------------- 动态列表（repeat）
def _table_rows_as_dicts(table: dict) -> list[dict]:
    """{header, data} → [{col名: 值}]（列名用存在于 header 的原始名）。"""
    header = list(table.get("header", []))
    out = []
    for row in table.get("data", []):
        d = {}
        for i, h in enumerate(header):
            d[h] = row[i] if i < len(row) else ""
        out.append(d)
    return out


def _row_value(row: dict, col: str) -> Optional[Any]:
    """按语义主体键取表行字段。"""
    want = _column_key(col)
    for k, v in row.items():
        if _column_key(k) == want:
            return v
    return None


def _repeat_lines(slot: dict, brief: dict, project_dir: str) -> Optional[list[str]]:
    """format 动态列表：按 repeat_source 遍历全部行生成行文本。

    repeat_source 仅允许 `load_combinations` 或 `table:<brief真实键>`；
    每行用 item_template.format({key: 列值})。任一行解析失败 → None（红字）。
    """
    src = slot.get("repeat_source")
    item_tmpl = slot.get("item_template")
    item_sources = slot.get("item_sources") or {}
    if not src or not item_tmpl:
        return None
    rows: Optional[list[dict]] = None
    if src == "load_combinations":
        rows = list(brief.get("load_combinations") or [])
    elif src.startswith("table:"):
        tbl = _table_data(project_dir, brief, src.removeprefix("table:"))
        if tbl:
            rows = _table_rows_as_dicts(tbl)
    else:
        return None
    if not rows:
        return None
    lines: list[str] = []
    for row in rows:
        params: dict[str, Any] = {}
        for key, ref in item_sources.items():
            if ref.startswith("column:"):
                params[key] = _row_value(row, ref.removeprefix("column:"))
            elif ref.startswith("field:"):
                val = _field_value(brief, ref.removeprefix("field:"), None)
                params[key] = val
            else:
                params[key] = row.get(ref) if isinstance(row, dict) else None
        if any(v is None for v in params.values()):
            return None
        try:
            lines.append(item_tmpl.format(**{k: _fmt_value(v) for k, v in params.items()}))
        except (KeyError, ValueError, IndexError, AttributeError):
            return None
    return lines


def _apply_repeat_paragraph(p: etree._Element, lines: list[str]) -> None:
    """把整段 Tag 段落重建为多行文本（行间用 <w:br>，不把 \\n 塞进 w:t）。"""
    pPr = p.find(qn("w:pPr"))
    first_rPr = None
    for r in p.findall(qn("w:r")):
        rpr = r.find(qn("w:rPr"))
        if rpr is not None:
            first_rPr = copy.deepcopy(rpr)
        break
    for child in list(p):
        if child.tag != qn("w:pPr"):
            p.remove(child)

    def _line_run(text: str):
        r = etree.Element(qn("w:r"))
        if first_rPr is not None:
            r.append(copy.deepcopy(first_rPr))
            _strip_tag_red(r)  # 标签红不带入渲染数据
        t = etree.SubElement(r, qn("w:t"))
        t.text = text
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        return r

    def _br_run():
        r = etree.Element(qn("w:r"))
        etree.SubElement(r, qn("w:br"))
        return r

    for i, line in enumerate(lines):
        if i:
            p.append(_br_run())
        p.append(_line_run(line))


# ---------------------------------------------------------------- OOXML 帮助 · render 主流程
def _find_paras_by_text(body: etree._Element, text: str) -> list[etree._Element]:
    """返回文档顺序中全部精确文本段落。"""
    return [
        el for el in body.iter(qn("w:p"))
        if para_text(el).strip() == text
    ]


def _find_para_by_text(body: etree._Element, text: str) -> Optional[etree._Element]:
    matches = _find_paras_by_text(body, text)
    return matches[0] if matches else None


def _pair_block_occurrences(
    slot: dict, paras: list[etree._Element]
) -> list[tuple[etree._Element, dict]]:
    """把块 Tag 段落与 compile 保存的逐位置元数据按文档顺序配对。"""
    occurrences = slot.get("occurrences") or []
    if not occurrences:
        if len(paras) <= 1:
            return [(para, slot) for para in paras]
        raise ValueError(f"块 Tag 有 {len(paras)} 个位置但 mapping 无 occurrences")
    if len(occurrences) != len(paras):
        raise ValueError(
            f"块 Tag 位置数 {len(paras)} 与 occurrences {len(occurrences)} 不一致")
    return list(zip(paras, occurrences))


def _replace_missing_image_caption(body: etree._Element, slot: dict) -> None:
    """缺图时只替换图题中的旧项目图名，保留图号与单位。"""
    caption = slot.get("caption") or ""
    caption_name = slot.get("caption_name") or ""
    if not caption or not caption_name:
        return
    cap_para = _find_para_by_text(body, caption)
    if cap_para is None:
        return
    if _replace_span_in_para(cap_para, caption_name, MISSING_IMAGE):
        _make_para_red(cap_para)


# ---------------------------------------------------------------- 派生缓存清理
#: 渲染后默认清理的派生缓存（每次 brief 构建都会确定性重建，删除安全）。
_DERIVED_CACHE_FILES = (
    os.path.join("json", "材料参数.混凝土.json"),
    os.path.join("json", "材料参数.预应力钢材.json"),
    os.path.join("json", "材料参数.普通钢筋.json"),
)


def cleanup_derived_cache(project_dir: str) -> list[str]:
    """渲染后清理项目目录里的派生缓存文件，返回实际删除的相对路径。

    只删确定性可重建的文件（材料三张分表 json——每次 brief 构建都会从
    json/tMatChar.json 重新拆分生成）。**不删** 项目数据结构.json、
    json/tXxx.json、json/<验算名>.json：在 Temperary 为空的兼容项目里
    它们是 render 的唯一数据源，删掉就没有输入且无法重建（不得隐式重算）。
    删除失败（文件被占用等）不阻断渲染结果。

    独立于 render_docx：由编排层（插件/CLI）在渲染完成后按需调用。
    """
    cleaned: list[str] = []
    for rel in _DERIVED_CACHE_FILES:
        p = os.path.join(project_dir, rel)
        try:
            if os.path.isfile(p):
                os.remove(p)
                cleaned.append(rel.replace(os.sep, "/"))
        except OSError:
            continue
    return cleaned


def _append_check_advice(body: etree._Element, brief: dict) -> bool:
    """在文末追加由已输出验算结果确定性生成的红色建议。"""
    facts = brief.get("facts") or {}
    checks: list[tuple[str, str]] = []
    for key, entry in facts.items():
        if not (key.startswith("check.") and key.endswith(".pass")):
            continue
        value = entry.get("value") if isinstance(entry, dict) else entry
        verdict = _norm_verdict(value)
        if verdict in ("OK", "NG"):
            checks.append((key[len("check."):-len(".pass")], verdict))
    if not checks:
        return False
    if "OSIS验算建议" in "".join(t.text or "" for t in body.iter(qn("w:t"))):
        return False

    failed = [name for name, verdict in checks if verdict == "NG"]
    if failed:
        message = (f"未通过验算：{'、'.join(failed)}。"
                   "建议优先复核截面尺寸、材料等级、预应力配置及荷载组合。")
    else:
        message = ("当前项目已输出的验算均满足规范要求；"
                   "建议结合施工图和构造要求完成最终复核。")

    def paragraph(text: str, bold: bool = False) -> etree._Element:
        p = etree.Element(qn("w:p"))
        r = etree.SubElement(p, qn("w:r"))
        rpr = etree.SubElement(r, qn("w:rPr"))
        if bold:
            etree.SubElement(rpr, qn("w:b"))
        color = etree.SubElement(rpr, qn("w:color"))
        color.set(qn("w:val"), RED)
        t = etree.SubElement(r, qn("w:t"))
        t.text = text
        return p

    insert_at = len(body)
    sect = body.find(qn("w:sectPr"))
    if sect is not None:
        insert_at = body.index(sect)
    body.insert(insert_at, paragraph("OSIS验算建议", bold=True))
    body.insert(insert_at + 1, paragraph(message))
    return True


def render_docx(
    package_dir: str,
    project_dir: str,
    output_path: str,
) -> dict[str, Any]:
    """执行渲染。"""
    pkg_info = load_package(package_dir)
    mapping = pkg_info["mapping"]
    slots = mapping.get("slots", {})

    # 渲染是纯函数：绝不隐式导出/重算 OSIS。数据补齐是 OSISAI 的前置步骤
    # （插件 export 子命令）；缺数据在此一律红字如实呈现。
    brief = build_project_brief(project_dir, auto_generate=False)

    # 数据源回退（旧式 field 点路径）
    project = None
    try:
        project = ProjectData(project_dir)
    except (OSError, ValueError):
        project = None

    out_dir = os.path.dirname(os.path.abspath(output_path)) or "."
    os.makedirs(out_dir, exist_ok=True)
    # pid + 随机后缀：同目录并发渲染（或 pid 复用）不会撞临时文件名
    tmp_path = os.path.join(
        out_dir, f".tmp-render-{os.getpid()}-{uuid.uuid4().hex[:8]}.docx")
    try:
        shutil.copy2(pkg_info["template_path"], tmp_path)
        pkg = DocxPackage.open(tmp_path)
        root = pkg.get_xml("word/document.xml")
        body = root.find(qn("w:body"))
        if body is None:
            raise ValueError("template.docx 缺 w:body")

        red_missing: list[str] = []
        red_reasons: dict[str, str] = {}
        results: list[dict[str, Any]] = []

        # 模板可能保留 Logo/固定图（自带 drawing id）：新插图 id 从现有最大值续数
        _reseed_drawing_ids(root)

        # ---- Pass 1：块级 Tag（table / image / 整段 repeat-format）——整段替换
        for tag, slot in slots.items():
            stype = slot.get("type")
            is_repeat = stype == "format" and slot.get("repeat_source")
            if stype not in ("table", "image") and not is_repeat:
                continue
            paras = _find_paras_by_text(body, "【" + tag + "】")
            if not paras:
                continue

            if is_repeat:
                lines = _repeat_lines(slot, brief, project_dir)
                for occurrence_index, para in enumerate(paras):
                    if lines is None:
                        red_missing.append(tag)
                        red_reasons[tag] = _missing_reason(slot, brief)
                        _set_run_texts_in_para(para, MISSING_TEXT)
                        _make_para_red(para)
                        results.append({"tag": tag, "type": "repeat_format",
                                        "status": "missing",
                                        "occurrence": occurrence_index})
                    else:
                        _apply_repeat_paragraph(para, lines)
                        results.append({"tag": tag, "type": "repeat_format",
                                        "status": "filled", "lines": len(lines),
                                        "occurrence": occurrence_index})
                continue

            try:
                occurrence_pairs = _pair_block_occurrences(slot, paras)
            except ValueError as exc:
                red_reasons[tag] = "contract_error"
                results.append({"tag": tag, "type": stype, "status": "failed",
                                "reason": str(exc)})
                continue

            if stype == "table":
                proj_table = _table_data(
                    project_dir, brief, (slot.get("source") or "").removeprefix("table:"))
                missing_table = False
                for occurrence_index, (para, occurrence) in enumerate(occurrence_pairs):
                    style_xml = occurrence.get("style_xml") or slot.get("style_xml") or {}
                    new_tbl, occurrence_missing = rebuild_table(style_xml, proj_table)
                    missing_table = missing_table or occurrence_missing
                    parent = para.getparent()
                    if parent is not None:
                        parent.replace(para, new_tbl)
                        results.append({"tag": tag, "type": "table",
                                        "status": "missing" if occurrence_missing else "filled",
                                        "occurrence": occurrence_index})
                if missing_table:
                    red_missing.append(tag)
                    red_reasons[tag] = _missing_reason(slot, brief)
                continue

            elif stype == "image":
                source = slot.get("source") or ""
                img_path = (_image_path(project_dir, brief, source.removeprefix("image:"))
                            if source else None)
                if not img_path:
                    red_missing.append(tag)
                    red_reasons[tag] = _missing_reason(slot, brief)
                    for occurrence_index, (para, occurrence) in enumerate(occurrence_pairs):
                        _set_run_texts_in_para(para, MISSING_IMAGE)
                        _make_para_red(para)
                        _replace_missing_image_caption(body, occurrence)
                        results.append({"tag": tag, "type": "image", "status": "missing",
                                        "occurrence": occurrence_index})
                else:
                    pixel = _image_pixel_size(img_path)
                    if pixel is None:
                        # 图片文件存在但无法解析宽高（导出中断产生的坏文件）：
                        # 如实按缺图处理；不得用 (1,1) 估尺寸插出巨大空白图。
                        red_missing.append(tag)
                        red_reasons[tag] = "source_file_missing"
                        for occurrence_index, (para, occurrence) in enumerate(occurrence_pairs):
                            _set_run_texts_in_para(para, MISSING_IMAGE)
                            _make_para_red(para)
                            _replace_missing_image_caption(body, occurrence)
                            results.append({"tag": tag, "type": "image", "status": "missing",
                                            "occurrence": occurrence_index,
                                            "reason": "unreadable_image"})
                        continue
                    page_avail = _page_available_emu(root)
                    rid_cache: dict[str, str] = {}
                    for occurrence_index, (para, occurrence) in enumerate(occurrence_pairs):
                        cx, cy = _compute_image_extent(
                            pixel, occurrence.get("width_emu"), page_avail)
                        rid, _ = _add_image_rels_and_media(pkg, img_path, rid_cache)
                        drawing = build_picture_drawing(rid, cx, cy)
                        _replace_para_with_drawing(para, drawing)
                        caption_name = occurrence.get("caption_name")
                        if caption_name:
                            cap_para = _find_para_by_text(
                                body, occurrence.get("caption") or "")
                        else:
                            cap_para = None
                        if cap_para is not None:
                            ref = source.removeprefix("image:")
                            try:
                                _replace_span_in_para(cap_para, caption_name, ref)
                            except Exception:
                                pass  # 图名字串未命中则保留模板图名（不阻断插图）
                        results.append({"tag": tag, "type": "image", "status": "filled",
                                        "width_emu": cx, "height_emu": cy,
                                        "occurrence": occurrence_index})
                continue

        # ---- Pass 2：内联 Tag（text / format / conclusion / 图名）
        for t in list(root.iter(qn("w:t"))):
            text = t.text or ""
            if "【" not in text:
                continue
            new_text, changed, red_tags = _substitute_inline(
                text, slots, brief, project, project_dir, red_missing, red_reasons)
            if changed:
                t.text = new_text
                if new_text == MISSING_TEXT:
                    # 缺失提示（整段或句中）所在 run 一律染红
                    _make_run_red(t.getparent()
                                  if t.getparent() is not None else t)
                else:
                    # 正常数据：洗掉 compile 期的标签红，沿用模板格式
                    _strip_tag_red(t.getparent()
                                   if t.getparent() is not None else t)

        # 残留 Tag 检查
        full = "".join(t.text or "" for t in root.iter(qn("w:t")))
        tags_left = set(RUNTIME_TAG_RE.findall(full))
        if tags_left:
            results.append({"tag": "residual", "status": "failed",
                            "tags": sorted(tags_left)})

        _append_check_advice(body, brief)
        _refresh_seq_field_results(root)
        _request_field_update(pkg)
        pkg.mark_dirty("word/document.xml")
        pkg.write(output_path)

        status = ("failed" if tags_left or "contract_error" in red_reasons.values()
                  else "needs_mapping_review"
                  if "unresolved_mapping" in red_reasons.values()
                  else "success" if not red_missing else "partial")
        # 自动执行输出校验（ZIP/XML/Tag 残留/红字统计）并入结果
        validation = None
        mapping_path = os.path.join(package_dir, "mapping.json")
        if os.path.isfile(mapping_path):
            try:
                validation = validate_output(output_path, mapping_path)
            except (OSError, ValueError):
                validation = None
        return {
            "status": status,
            "output": os.path.abspath(output_path),
            "missing": sorted(set(red_missing)),
            "missing_reasons": dict(sorted(red_reasons.items())),
            "results": results,
            "validation": validation,
            "template_sha256": pkg_info["package_meta"].get("template_sha256", ""),
        }
    finally:
        # 无论成功或异常，清理渲染临时文件，不留中间产物
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _missing_reason(slot: dict, brief: dict) -> str:
    """缺失原因分类：unresolved_mapping / source_absent / source_file_missing。"""
    src = slot.get("source")
    if not src:
        return "unresolved_mapping"
    kind, _, ref = src.partition(":")
    if kind == "field":
        return "source_absent"
    if kind == "table":
        return "source_absent" if ref not in brief.get("tables", {}) else "source_file_missing"
    if kind == "image":
        return "source_absent" if ref not in brief.get("images", {}) else "source_file_missing"
    if kind == "verdict":
        lookup = ref.split("#")[0]
        lookup = lookup if lookup.startswith("验算表格.") else f"验算表格.{lookup}"
        return "source_absent" if lookup not in brief.get("tables", {}) else "source_file_missing"
    return "source_absent"


def _substitute_inline(
    text: str, slots: dict, brief: dict, project, project_dir: str,
    red_missing: list, red_reasons: dict,
) -> tuple[str, bool, set[str]]:
    changed = False
    red_tags: set[str] = set()
    for m in TAG_RE.finditer(text):
        tag = m.group(0)[1:-1]
        slot = slots.get(tag)
        if not slot:
            continue
        stype = slot.get("type")
        if stype == "text":
            val = _resolve_text(slot, brief, project)
        elif stype == "format":
            val = _resolve_format(slot, brief, project)
        elif stype == "conclusion":
            val = _resolve_conclusion(slot, brief, project, project_dir)
        else:
            continue  # table/image/repeat 已在 Pass 1 处理
        if val is None:
            red_missing.append(tag)
            red_reasons[tag] = _missing_reason(slot, brief)
            repl = MISSING_TEXT
            red_tags.add(tag)
        else:
            repl = str(val)
        text = text.replace(m.group(0), repl)
        changed = True
    return text, changed, red_tags


def _resolve_text(slot: dict, brief: dict, project) -> Optional[Any]:
    source = slot.get("source")
    if not source:
        return None
    kind, _, ref = source.partition(":")
    if kind == "field":
        return _field_value(brief, ref, project)
    if kind == "image":
        # 图名：OSIS 输出图名 = image: 后的键；即使当前项目无该图也显示（图题仍重拼为 OSIS 图名）
        return ref
    if kind == "table":
        return None  # 由 block 处理
    return None


def _resolve_conclusion(slot: dict, brief: dict, project, project_dir: str) -> Optional[str]:
    """conclusion：验算数据 + OK/NG 固定句式确定性生成。无可靠数据 → None（红字）。

    - 数据源：`verdict_source`（verdict:验算表#行，供 control/limit/unit）或字典 source（field:<pass 键>，仅 OK/NG）；
    - 句式：ok_text/ng_text（纯文本）或 ok_template/ng_template（{control}{limit}{unit}{verdict} 占位）。
    """
    source = slot.get("verdict_source") or slot.get("source")
    if not source:
        return None
    kind, _, ref = source.partition(":")
    if kind == "field":
        val = _field_value(brief, ref, project)
        if val is None:
            return None
        data = {"verdict": _norm_verdict(val)}
    elif kind == "verdict":
        data = _resolve_verdict(brief, project_dir, ref)
        if data is None:
            fallback = _field_value(brief, f"check.{ref.split('#', 1)[0]}.pass", project)
            if fallback is None:
                return None
            data = {"verdict": _norm_verdict(fallback)}
    else:
        return None

    verdict = str(data.get("verdict", "")).strip()
    if verdict == "OK":
        tmpl = slot.get("ok_template") or slot.get("ok_text")
    elif verdict == "NG":
        tmpl = slot.get("ng_template") or slot.get("ng_text")
    else:
        return None
    if not tmpl:
        if "control" in data and "limit" in data:
            operator = "≤" if verdict == "OK" else ">"
            phrase = "满足规范要求" if verdict == "OK" else "不满足规范要求"
            result = (
                f"控制值{_fmt_value(data['control'])}{_fmt_value(data.get('unit', ''))}"
                f"{operator}限值{_fmt_value(data['limit'])}{_fmt_value(data.get('unit', ''))}"
                f"，{phrase}；"
            )
        else:
            result = "满足规范要求；" if verdict == "OK" else "不满足规范要求；"
    elif "{" in str(tmpl):
        fmt = {k: _fmt_value(v) for k, v in data.items()}
        try:
            result = str(tmpl).format(**fmt)
        except (KeyError, ValueError, IndexError):
            return None
    else:
        result = str(tmpl)
    basis = str(slot.get("basis") or "").strip()
    if basis and basis not in result:
        return f"{basis}，{result}"
    return result


def _resolve_format(slot: dict, brief: dict, project) -> Optional[str]:
    template = slot.get("template")
    sources = slot.get("sources") or {}
    if not template:
        return None
    kwargs: dict[str, Any] = {}
    for key, src in sources.items():
        if not src:
            return None
        kind, _, ref = src.partition(":")
        if kind != "field":
            return None
        val = _field_value(brief, ref, project)
        if val is None:
            return None
        kwargs[key] = _fmt_value(val)
    try:
        return template.format(**kwargs)
    except (KeyError, ValueError, IndexError):
        return None


# ---------------------------------------------------------------- 校验（并入 render）
def validate_output(
    output_path: str,
    mapping_path: str,
    report_path: str | None = None,
) -> dict:
    """校验生成的计算书 DOCX（按 mapping）：ZIP/XML/rels 完整、无运行时 Tag 残留、红字统计。

    Args:
        output_path: 输出 DOCX 路径
        mapping_path: 模板包内 mapping.json 路径
        report_path: 报告输出路径（默认 output 同目录）
    """
    import zipfile

    with open(mapping_path, "r", encoding="utf-8") as f:
        mapping = json.load(f)
    failures: list = []
    warnings: list = []

    if report_path is None:
        report_path = os.path.join(
            os.path.dirname(os.path.abspath(output_path)),
            os.path.splitext(os.path.basename(output_path))[0] + ".report.json")

    try:
        with zipfile.ZipFile(output_path) as z:
            bad = z.testzip()
            if bad:
                failures.append(f"ZIP 损坏的 part: {bad}")
    except zipfile.BadZipFile as e:
        failures.append(f"不是合法 ZIP/DOCX: {e}")

    if not failures:
        pkg = DocxPackage.open(output_path)
        for name in pkg.parts:
            if name.endswith((".xml", ".rels")):
                try:
                    pkg.get_xml(name)
                except Exception as e:
                    failures.append(f"XML 解析失败 {name}: {e}")
        if not failures:
            rels = pkg.rels("word/document.xml")
            root = pkg.get_xml("word/document.xml")
            used = set()
            for el in root.iter():
                for attr in (qn("r:embed"), qn("r:id"), qn("r:link")):
                    v = el.get(attr)
                    if v:
                        used.add(v)
            for rid in sorted(used):
                rel = rels.get(rid)
                if rel is None:
                    failures.append(f"关系缺失: {rid}")
                    continue
                if rel.target_mode == "External":
                    continue
                part = ("word/" + rel.target if not rel.target.startswith("/")
                        else rel.target.lstrip("/"))
                part = os.path.normpath(part).replace("\\", "/")
                if pkg.get_part(part) is None:
                    failures.append(f"关系目标不存在: {rid} -> {part}")

            full = "".join(t.text or "" for t in root.iter(qn("w:t")))
            tags = set()
            for name in pkg.parts:
                if not (name.startswith("word/") and name.endswith(".xml")):
                    continue
                part_root = pkg.get_xml(name)
                part_text = "".join(t.text or "" for t in part_root.iter(qn("w:t")))
                tags.update(RUNTIME_TAG_RE.findall(part_text))
            tags = sorted(tags)
            if tags:
                failures.append(f"残留 Tag {len(tags)} 个: {tags}")
            for label in (MISSING_TEXT, MISSING_IMAGE):
                n = full.count(label)
                if n:
                    warnings.append(f"缺失提示 {n} 处: {label}")

    status = ("failed" if failures
              else "success_with_warning" if warnings else "success")
    report = {
        "status": status,
        "output": os.path.abspath(output_path),
        "checks": {"failures": failures, "warnings": warnings},
    }
    os.makedirs(os.path.dirname(os.path.abspath(report_path)), exist_ok=True)
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    return report


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="固定 Runtime：按 mapping.json 渲染计算书（零 AI）")
    ap.add_argument("--package", required=True, help="模板包目录")
    ap.add_argument("--project", required=True, help="OSIS 项目目录")
    ap.add_argument("-o", "--output", required=True, help="输出 DOCX 路径")
    ap.add_argument("--keep-cache", action="store_true",
                    help="保留派生缓存（json/材料参数.*.json、自检报告 json）；默认渲染后清理")
    args = ap.parse_args(argv)

    try:
        result = render_docx(args.package, args.project, args.output)
        if not args.keep_cache:
            cleaned = cleanup_derived_cache(args.project)
            report_file = os.path.join(
                os.path.dirname(os.path.abspath(args.output)),
                os.path.splitext(os.path.basename(args.output))[0] + ".report.json")
            if os.path.isfile(report_file):
                try:
                    os.remove(report_file)
                    cleaned.append(os.path.basename(report_file))
                except OSError:
                    pass
            result["cleaned_derived"] = cleaned
    except (OSError, ValueError, KeyError) as e:
        print(json.dumps({"status": "failed", "error": str(e)},
                         ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in ("success", "partial") else 1


if __name__ == "__main__":
    raise SystemExit(main())
