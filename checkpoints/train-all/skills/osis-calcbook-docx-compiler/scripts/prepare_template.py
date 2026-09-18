"""prepare_template：模板首接预处理（固定脚本，零 AI、零业务判断）。

职责（保守规则，只做结构工作，不判断动态/静态）：
1. 正文与版式原样保留；
2. 有"图x.x"图题关联的正文项目图 → 清空成图片槽，保存尺寸元数据，摘除旧 media/rels；
   无图题的 Logo / 固定示意图 / 封面图 → 默认保留；header/footer 属独立 part 一律不碰；
3. 有"表x.x"表题的数据表 → 保留表名/表头/格式，清空数据行；
   无表题的表（封面信息表/排版表/固定参数表）→ 默认不动；
4. 输出：
   - prepared-template.docx   高保真写回（未动 part 字节不变）
   - prepared-view.json       结构化视图（temp_id/章节/类型/图题/表题/表头/尺寸/前后文）
   - template-text.md         OSISAI 可读全文
   - report.json              清理统计

用法：
    python prepare_template.py 模板.docx -o OUTDIR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

from lxml import etree

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from ooxml_utils import (  # noqa: E402
    DocxPackage, qn, para_text, para_style, sha256_file,
    tbl_rows, row_texts, is_header_row,
)

# ---------------------------------------------------------------- caption 判定
#: 图题：图2.1 / 图3-1 / 图表1 …
FIGURE_CAPTION_RE = re.compile(r"^\s*(?:图|图表)\s*\d+(?:[.\-－]\d+)*")
#: 表题：表1.5-1 / 表2.1 / 表4-2 / 表格1 …（含 Word SEQ 域的表题可见文本以"表格"开头）
TABLE_CAPTION_RE = re.compile(r"^\s*表(?:格)?\s*\d+(?:[.\-－]\d+)*")

#: VML 形状尺寸（style="width:..pt;height:..pt"）。
_VML_PT_RE = re.compile(r"(width|height)\s*:\s*([\d.]+)\s*pt")
#: 1 pt = 12700 EMU
_PT_TO_EMU = 12700


def _heading_level(p: etree._Element, styles: dict[str, dict[str, Any]]) -> Optional[int]:
    """解析段落标题层级（复用 template_semantics 的判定思路，轻量版）。"""
    outline_el = p.find(f"{qn('w:pPr')}/{qn('w:outlineLvl')}")
    if outline_el is not None:
        try:
            outline = int(outline_el.get(qn("w:val"), ""))
            if 0 <= outline <= 8:
                return outline + 1
        except ValueError:
            pass
    style_id = para_style(p)
    while style_id and style_id in styles:
        info = styles[style_id]
        name = str(info.get("name", "")).strip()
        m = re.match(r"^(?:heading|标题)\s*(\d+)", name, re.IGNORECASE)
        if m:
            return int(m.group(1))
        outline = info.get("outline")
        if isinstance(outline, int) and 0 <= outline <= 8:
            return outline + 1
        style_id = info.get("based_on") or ""
    return None


def _load_paragraph_styles(pkg: DocxPackage) -> dict[str, dict[str, Any]]:
    """读取段落样式 ID 对应的名称与继承关系。"""
    if "word/styles.xml" not in pkg.parts:
        return {}
    root = pkg.get_xml("word/styles.xml")
    styles: dict[str, dict[str, Any]] = {}
    for style in root.findall(qn("w:style")):
        if style.get(qn("w:type")) != "paragraph":
            continue
        style_id = style.get(qn("w:styleId"), "")
        if not style_id:
            continue
        name_el = style.find(qn("w:name"))
        based_on_el = style.find(qn("w:basedOn"))
        outline_el = style.find(f"{qn('w:pPr')}/{qn('w:outlineLvl')}")
        outline = None
        if outline_el is not None:
            try:
                outline = int(outline_el.get(qn("w:val"), ""))
            except ValueError:
                outline = None
        styles[style_id] = {
            "name": name_el.get(qn("w:val"), "") if name_el is not None else "",
            "based_on": based_on_el.get(qn("w:val"), "") if based_on_el is not None else "",
            "outline": outline,
        }
    return styles


def _next_para(el: etree._Element) -> Optional[etree._Element]:
    """返回元素后最近的 w:p 兄弟（跳过 sdt 包装、节属性等）。"""
    nxt = el.getnext()
    while nxt is not None:
        if nxt.tag == qn("w:p"):
            return nxt
        if nxt.tag == qn("w:sdt"):
            content = nxt.find(qn("w:sdtContent"))
            if content is not None:
                for ch in content:
                    if ch.tag == qn("w:p"):
                        return ch
        nxt = nxt.getnext()
    return None


def _prev_para(el: etree._Element) -> Optional[etree._Element]:
    """返回元素前最近的 w:p 兄弟（跳过 sdt 包装）。"""
    prev = el.getprevious()
    while prev is not None:
        if prev.tag == qn("w:p"):
            return prev
        if prev.tag == qn("w:sdt"):
            content = prev.find(qn("w:sdtContent"))
            if content is not None:
                for ch in content:
                    if ch.tag == qn("w:p"):
                        return ch
        prev = prev.getprevious()
    return None


def _image_extent(p: etree._Element) -> Optional[dict[str, int]]:
    """提取图片段落尺寸（EMU）。VML 取 style 的 pt → EMU；DrawingML 取 wp:extent。"""
    for drawing in p.iter(qn("w:drawing")):
        extent = drawing.find(f".//{qn('wp:extent')}")
        if extent is not None:
            try:
                return {
                    "width_emu": int(extent.get("cx", 0)),
                    "height_emu": int(extent.get("cy", 0)),
                }
            except (TypeError, ValueError):
                return None
    for shape in p.iter("{urn:schemas-microsoft-com:vml}shape"):
        style = shape.get("style", "")
        m = dict(_VML_PT_RE.findall(style))
        if "width" in m and "height" in m:
            try:
                return {
                    "width_emu": int(float(m["width"]) * _PT_TO_EMU),
                    "height_emu": int(float(m["height"]) * _PT_TO_EMU),
                }
            except ValueError:
                return None
    return None


def _clear_image_content(p: etree._Element) -> list[str]:
    """清空段落内图片内容（w:pict / w:drawing，含嵌套在 run 内的），保留段落结构；返回被摘除的 rIds。"""
    rids: list[str] = []
    # 收集 rId（先于移除，避免元素被删除后取不到）
    for el in p.iter():
        if el.tag in (qn("w:pict"), qn("w:drawing")):
            rids.extend(_collect_rids(el))
    # 移除所有图片承载元素（嵌套层级任意）
    for el in list(p.iter(qn("w:pict"))) + list(p.iter(qn("w:drawing"))):
        parent = el.getparent()
        if parent is not None:
            parent.remove(el)
    return rids


def _collect_rids(el: etree._Element) -> list[str]:
    """收集元素内所有图片/图表 rId（r:embed / r:id / v:imagedata 的 r:id 与 o:id）。"""
    rids: list[str] = []
    for blip in el.iter(qn("a:blip")):
        rid = blip.get(qn("r:embed")) or blip.get(qn("r:link"))
        if rid:
            rids.append(rid)
    for chart in el.iter(qn("c:chart")):
        rid = chart.get(qn("r:id"))
        if rid:
            rids.append(rid)
    for imgdata in el.iter("{urn:schemas-microsoft-com:vml}imagedata"):
        rid = imgdata.get(qn("r:id")) or imgdata.get("{urn:schemas-microsoft-com:office:office}id")
        if rid:
            rids.append(rid)
    return rids


class SectionTracker:
    """标题层级栈跟踪，维护当前章节路径。"""

    def __init__(self):
        self._stack: list[str] = []
        self._levels: list[int] = []

    def update(self, level: Optional[int], text: str) -> list[str]:
        if level is None:
            return list(self._stack)
        while self._levels and self._levels[-1] >= level:
            self._levels.pop()
            self._stack.pop()
        self._levels.append(level)
        self._stack.append(text.strip()[:80])
        return list(self._stack)

    def current(self) -> list[str]:
        return list(self._stack)


class TempIdGen:
    """按类型生成临时位置编号（P001/IMG01/TBL01）。"""

    def __init__(self):
        self._counters = {"P": 0, "IMG": 0, "TBL": 0}

    def next(self, prefix: str) -> str:
        self._counters[prefix] += 1
        return f"{prefix}{self._counters[prefix]:03d}"


def prepare_template(template_path: str, outdir: str) -> dict[str, Any]:
    """执行模板预处理。

    Returns:
        {"status", "prepared_template", "prepared_view", "template_text", "report"}
    """
    sha = sha256_file(template_path)
    pkg = DocxPackage.open(template_path)
    root = pkg.get_xml("word/document.xml")
    body = root.find(qn("w:body"))
    if body is None:
        raise ValueError("document.xml 缺少 w:body")

    styles = _load_paragraph_styles(pkg)
    tracker = SectionTracker()
    ids = TempIdGen()
    blocks: list[dict[str, Any]] = []
    # 待摘除的 rId（图片媒体），全部遍历完后再统一清理 media/rels
    removed_rids: list[str] = []
    report = {
        "image_slots_cleared": 0,
        "image_paragraphs_kept": 0,   # 无图题、保留原样的图片段
        "tables_data_cleared": 0,
        "tables_kept": 0,             # 无表题、保留原样的表
    }

    def handle_paragraph(p: etree._Element) -> None:
        text = para_text(p).strip()
        style = para_style(p) or ""
        level = _heading_level(p, styles)
        chapter = tracker.update(level, text)
        rids = _collect_rids(p)
        # 图片可能嵌套在 run 内（w:p > w:r > w:pict / w:drawing）
        has_image = (p.find(f".//{qn('w:pict')}") is not None
                     or p.find(f".//{qn('w:drawing')}") is not None)

        if has_image:
            caption_para = _next_para(p)
            caption = para_text(caption_para).strip() if caption_para is not None else ""
            if FIGURE_CAPTION_RE.match(caption):
                # 项目图槽：先取尺寸，再清空图片内容，保留段落
                extent = _image_extent(p)
                removed_rids.extend(_clear_image_content(p))
                blocks.append({
                    "temp_id": ids.next("IMG"),
                    "kind": "image_slot",
                    "chapter": chapter,
                    "caption": caption,
                    "width_emu": extent["width_emu"] if extent else None,
                    "height_emu": extent["height_emu"] if extent else None,
                    "before_text": "",
                    "after_text": caption,
                })
                report["image_slots_cleared"] += 1
            else:
                # 无图题：视为 Logo/固定图，原样保留
                report["image_paragraphs_kept"] += 1
                if text or level is not None:
                    blocks.append({
                        "temp_id": ids.next("P"),
                        "kind": "paragraph",
                        "chapter": chapter,
                        "text": text,
                        "before_text": "",
                        "after_text": "",
                    })
            return

        # 普通段落 / 标题
        blocks.append({
            "temp_id": ids.next("P"),
            "kind": "heading" if level is not None else "paragraph",
            "chapter": chapter,
            "text": text,
            "before_text": "",
            "after_text": "",
        })

    def handle_table(tbl: etree._Element) -> None:
        rows = tbl_rows(tbl)
        prev = _prev_para(tbl)
        caption = para_text(prev).strip() if prev is not None else ""
        header = row_texts(rows[0]) if rows else []
        if TABLE_CAPTION_RE.match(caption):
            # 数据表：保留表头行，清空数据行
            header_count = 0
            for tr in rows:
                if is_header_row(tr):
                    header_count += 1
                else:
                    break
            header_count = max(header_count, 1)  # 至少保留首行作表头
            for tr in rows[header_count:]:
                tbl.remove(tr)
            blocks.append({
                "temp_id": ids.next("TBL"),
                "kind": "table",
                "chapter": tracker.current(),
                "caption": caption,
                "header": header[:12],
                "row_count": len(rows) - header_count,  # 清理后剩余数据行数（应为 0）
                "before_text": caption,
                "after_text": "",
            })
            report["tables_data_cleared"] += 1
        else:
            # 无表题：封面/排版/固定参数表，原样保留
            report["tables_kept"] += 1

    for child in list(body):
        if child.tag == qn("w:p"):
            handle_paragraph(child)
        elif child.tag == qn("w:tbl"):
            handle_table(child)
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is not None:
                for ch in list(content):
                    if ch.tag == qn("w:p"):
                        handle_paragraph(ch)
                    elif ch.tag == qn("w:tbl"):
                        handle_table(ch)

    # 统一摘除不再被引用的图片 media 与 relationship
    rels = pkg.rels("word/document.xml")
    media_to_drop: list[str] = []
    for rid in dict.fromkeys(removed_rids):
        rel = rels.get(rid)
        if rel is None:
            continue
        target = rel.target
        if target.startswith("/"):
            media_path = target.lstrip("/")
        else:
            media_path = f"word/{target}"
        # 确认 document.xml 中已无任何引用该 rId（不误删共享 media）
        if not _rid_still_used(root, rid):
            media_to_drop.append(media_path)

    # 从 document.xml.rels 摘除 rId
    if removed_rids:
        rels_root = pkg.get_xml("word/_rels/document.xml.rels")
        for rel in list(rels_root):
            if rel.get("Id") in set(removed_rids) and not _rid_still_used(root, rel.get("Id")):
                rels_root.remove(rel)
        pkg.mark_dirty("word/_rels/document.xml.rels")

    # 摘除 media part（仅当没有其它 part 引用它）
    for media_path in dict.fromkeys(media_to_drop):
        if media_path in pkg.parts and not _media_referenced_elsewhere(pkg, media_path):
            del pkg.parts[media_path]
            if media_path in pkg.order:
                pkg.order.remove(media_path)

    pkg.mark_dirty("word/document.xml")

    # 写回 prepared 模板
    os.makedirs(outdir, exist_ok=True)
    prepared_path = os.path.join(outdir, "prepared-template.docx")
    pkg.write(prepared_path)

    # 统计
    stats = {
        "total_blocks": len(blocks),
        "paragraph_blocks": sum(1 for b in blocks if b["kind"] == "paragraph"),
        "heading_blocks": sum(1 for b in blocks if b["kind"] == "heading"),
        "image_slots": sum(1 for b in blocks if b["kind"] == "image_slot"),
        "table_blocks": sum(1 for b in blocks if b["kind"] == "table"),
        "media_removed": len(set(media_to_drop)),
        "rels_removed": len(set(removed_rids)),
    }
    report.update(stats)
    report["cleaned_at"] = datetime.now(timezone.utc).isoformat()

    # prepared-view.json
    view = {
        "schema": "prepared-view-v1",
        "template_sha256": sha,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
        "blocks": blocks,
        "stats": stats,
    }
    view_path = os.path.join(outdir, "prepared-view.json")
    with open(view_path, "w", encoding="utf-8") as f:
        json.dump(view, f, ensure_ascii=False, indent=2)

    # template-text.md
    md_path = os.path.join(outdir, "template-text.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(generate_template_text_md(view))

    # report.json
    report_path = os.path.join(outdir, "report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)

    return {
        "status": "prepared",
        "template_sha256": sha,
        "prepared_template": prepared_path,
        "prepared_view": view_path,
        "template_text": md_path,
        "report": report_path,
        **stats,
        # 清理明细（写进 report.json 的同时也返回，便于调用方/测试直接读取）
        "image_slots_cleared": report["image_slots_cleared"],
        "image_paragraphs_kept": report["image_paragraphs_kept"],
        "tables_data_cleared": report["tables_data_cleared"],
        "tables_kept": report["tables_kept"],
    }


def _rid_still_used(root: etree._Element, rid: str) -> bool:
    """document.xml 中是否仍引用该 rId。"""
    for el in root.iter():
        for attr in (qn("r:embed"), qn("r:id"), qn("r:link")):
            if el.get(attr) == rid:
                return True
    return False


def _media_referenced_elsewhere(pkg: DocxPackage, media_path: str) -> bool:
    """media 是否仍被其它 part 引用（页眉页脚/脚注等）。"""
    for part_name, data in pkg.parts.items():
        if not part_name.endswith(".rels"):
            continue
        if part_name == "word/_rels/document.xml.rels":
            continue
        try:
            rel_root = etree.fromstring(data)
        except Exception:
            continue
        for rel in rel_root:
            target = rel.get("Target", "")
            if target.lstrip("/") == media_path or media_path.endswith("/" + target):
                return True
    return False


def generate_template_text_md(view: dict[str, Any]) -> str:
    """生成 OSISAI 可读的模板全文 Markdown。"""
    lines = []
    lines.append("# 模板全文（预处理后）")
    lines.append("")
    lines.append(f"SHA-256: `{view.get('template_sha256', '')[:16]}...`")
    lines.append("")
    stats = view.get("stats", {})
    lines.append(f"块数: {stats.get('total_blocks', 0)} | "
                 f"标题: {stats.get('heading_blocks', 0)} | "
                 f"段落: {stats.get('paragraph_blocks', 0)} | "
                 f"图片槽: {stats.get('image_slots', 0)} | "
                 f"表格: {stats.get('table_blocks', 0)}")
    lines.append("")
    for b in view.get("blocks", []):
        tid = b["temp_id"]
        chapter = " > ".join(b.get("chapter", []))
        if b["kind"] == "heading":
            lines.append(f"## {b['text']}  [{tid}]")
        elif b["kind"] == "paragraph":
            lines.append(f"- [{tid}] {b['text']}")
        elif b["kind"] == "image_slot":
            w = b.get("width_emu")
            lines.append(f"- [{tid}] 🖼 图片槽：{b['caption']}"
                         + (f"（尺寸 {w}x{b.get('height_emu')} EMU）" if w else ""))
        elif b["kind"] == "table":
            hdr = " | ".join(b.get("header", []))
            lines.append(f"- [{tid}] ▦ 表格：{b['caption']}  表头：{hdr}")
        if chapter:
            lines.append(f"  （章节：{chapter}）")
    return "\n".join(lines)


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="模板首接预处理（清旧图/清表数据行，保留版式）")
    ap.add_argument("template", help="原始模板 DOCX")
    ap.add_argument("-o", "--outdir", required=True, help="输出目录")
    args = ap.parse_args(argv)

    try:
        result = prepare_template(args.template, args.outdir)
    except (OSError, ValueError) as e:
        print(json.dumps({"status": "failed", "error": str(e)},
                         ensure_ascii=False, indent=2))
        return 1

    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
