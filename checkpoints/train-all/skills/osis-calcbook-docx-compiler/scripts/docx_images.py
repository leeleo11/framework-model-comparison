"""docx_images：插图域（尺寸解析 → DrawingML 构造 → media/rels 嵌入）。

render 主流程按图槽调用本模块原语：
- `_image_pixel_size`：标准库解析 PNG/JPEG/GIF/BMP 尺寸（PIL 兜底）；
- `_page_available_emu` / `_compute_image_extent`：页面可用 EMU、按原图比例
  算尺寸、超页高按高反算宽；
- `_add_image_rels_and_media`：新增 media part + relationship；`rid_cache`
  让同源图多槽复用同一 media，不重复嵌入字节；
- `_reseed_drawing_ids` / `build_picture_drawing`：drawing id 从模板现有
  最大 id 续数（OOXML 要求全文档唯一），构造标准 DrawingML；
- `_replace_para_with_drawing`：图槽段落原位替换为 w:r/w:drawing。
"""
from __future__ import annotations

import itertools
import os
import re
import struct
from typing import Optional

from lxml import etree

from ooxml_utils import DocxPackage, qn, _update_content_type

_EXP = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"

def _image_pixel_size(path: str) -> Optional[tuple[int, int]]:
    """读图片原始像素宽高（标准库解析 PNG/JPEG/GIF/BMP，PIL 兜底）。"""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError:
        return None
    if data[:8] == b"\x89PNG\r\n\x1a\n" and len(data) >= 24:
        return struct.unpack(">II", data[16:24])
    if data[:2] == b"\xff\xd8":
        i, n = 2, len(data)
        while i + 9 < n and data[i] == 0xFF:
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            ln = struct.unpack(">H", data[i + 2:i + 4])[0]
            if ln < 2:
                break
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                # SOF 段：height 在前、width 在后 → 返回 (width, height)
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return (w, h)
            i += 2 + ln
    if data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 10:
        return struct.unpack("<HH", data[6:10])
    if data[:2] == b"BM" and len(data) >= 26:
        # BMP 高度有符号：负值表示 top-down DIB，尺寸取绝对值
        w, h = struct.unpack("<ii", data[18:26])
        return (abs(w), abs(h))
    # PIL 兜底
    try:
        from PIL import Image
        with Image.open(path) as im:
            return im.size
    except Exception:
        return None
    return None


def _page_available_emu(root: etree._Element) -> tuple[int, int]:
    """从 sectPr 读页面可用宽高（EMU）。pgSz/pgMar 单位 twips(1/20pt)，1twip=635EMU。

    优先 body 直属 sectPr（文末节，单节文档即唯一节）；多节文档退回首个 sectPr。
    """
    sec = None
    body = root.find(qn("w:body"))
    if body is not None:
        sec = body.find(qn("w:sectPr"))
    if sec is None:
        sec = root.find(f".//{qn('w:sectPr')}")
    if sec is None:
        return 9144000, 9144000  # 默认 A4 左右
    pgSz = sec.find(qn("w:pgSz"))
    pgMar = sec.find(qn("w:pgMar"))
    w = int(pgSz.get(qn("w:w"), "11906")) if pgSz is not None else 11906
    h = int(pgSz.get(qn("w:h"), "16838")) if pgSz is not None else 16838
    top = int(pgMar.get(qn("w:top"), "1440")) if pgMar is not None else 1440
    bottom = int(pgMar.get(qn("w:bottom"), "1440")) if pgMar is not None else 1440
    left = int(pgMar.get(qn("w:left"), "1800")) if pgMar is not None else 1800
    right = int(pgMar.get(qn("w:right"), "1800")) if pgMar is not None else 1800
    emu = 635
    return (w - left - right) * emu, (h - top - bottom) * emu


def _compute_image_extent(
    pixel: tuple[int, int],
    slot_width_emu: Optional[int],
    page_avail: tuple[int, int],
) -> tuple[int, int]:
    """计算插图尺寸（EMU，保持原图比例、不写死、超页高反算）。"""
    pw, ph = pixel
    if pw <= 0 or ph <= 0:
        return (slot_width_emu or 4000000, 4000000 * ph // max(pw, 1))
    page_w, page_h = page_avail
    if slot_width_emu and slot_width_emu > 0:
        width = slot_width_emu
    else:
        width = int(page_w * 0.85)  # 无旧槽：正文宽 85%
    width = min(width, page_w)
    height = round(width * ph / pw)
    if height > page_h:  # 超页面可用高度 → 按高度反算宽度
        height = page_h
        width = round(height * pw / ph)
    return (width, height)


# ---------------------------------------------------------------- OOXML 帮助

def _add_image_rels_and_media(
    pkg: DocxPackage, image_path: str, rid_cache: Optional[dict[str, str]] = None
) -> tuple[str, dict]:
    """把图片加入包：新 media part + document.xml.rels 关系。返回 (rid, rel)。

    同一源文件在本文档出现多个图槽（如顶/底两槽共用一张合并包络图）时，
    传入 rid_cache 复用同一 media part 与关系，不重复嵌入图片字节。
    """
    cached = rid_cache.get(image_path) if rid_cache else None
    if cached:
        return cached, {"rid": cached, "target": ""}
    with open(image_path, "rb") as f:
        data = f.read()
    ext = os.path.splitext(image_path)[1].lower().lstrip(".") or "jpeg"
    # 唯一文件名
    existing = [n for n in pkg.order if n.startswith("word/media/") and not n.endswith("/")]
    n = len(existing) + 1
    media_path = f"word/media/osis_render_{n}.{ext}"
    pkg.set_part(media_path, data)

    # 关系 id
    max_rid = 0
    rid = None
    rels_root = pkg.get_xml("word/_rels/document.xml.rels")
    for rel in rels_root:
        m = re.match(r"rId(\d+)", rel.get("Id", ""))
        if m:
            max_rid = max(max_rid, int(m.group(1)))
    rid = f"rId{max_rid + 1}"

    rel = etree.SubElement(rels_root, qn("rel:Relationship"))
    rel.set("Id", rid)
    rel.set("Type", "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image")
    rel.set("Target", f"media/{os.path.basename(media_path)}")
    pkg.mark_dirty("word/_rels/document.xml.rels")
    _update_content_type(pkg, "", f".{ext}")

    # 深拷贝一份 rel 供图片关系读取类函数使用（返回前脱离树，避免脏树）
    rel_info = {"rid": rid, "target": media_path}
    if rid_cache is not None:
        rid_cache[image_path] = rid
    return rid, rel_info


#: 文档内唯一的 drawing id（OOXML 要求 wp:docPr/pic:cNvPr 的 id 全文档不重复，
#: 重复 id 会触发 Word/WPS 的修复提示甚至打不开）。render 开始时按模板里已有
#: 图片的最大 id 续数，避免与 prepare 保留的 Logo/固定图冲突。
_DRAWING_ID_SEQ = itertools.count(1)


def _reseed_drawing_ids(root: etree._Element) -> None:
    """把 drawing id 计数器拨到模板现有最大 id 之后（模板可能保留了非项目图）。"""
    global _DRAWING_ID_SEQ
    max_id = 0
    for tag in (f"{_WP}docPr", qn("pic:cNvPr")):
        for el in root.iter(tag):
            try:
                max_id = max(max_id, int(el.get("id", "0")))
            except (TypeError, ValueError):
                continue
    _DRAWING_ID_SEQ = itertools.count(max_id + 1)


def build_picture_drawing(rid: str, cx_emu: int, cy_emu: int) -> etree._Element:
    """构造标准 DrawingML 图片（w:drawing → wp:inline → a:graphic → pic:pic）。"""
    wp = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
    a = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
    pic = "{http://schemas.openxmlformats.org/drawingml/2006/picture}"
    drawing_id = next(_DRAWING_ID_SEQ)

    d = etree.Element(qn("w:drawing"))
    inline = etree.SubElement(d, wp + "inline")
    extent = etree.SubElement(inline, wp + "extent")
    extent.set("cx", str(cx_emu))
    extent.set("cy", str(cy_emu))
    eff = etree.SubElement(inline, wp + "effectExtent")
    for side, v in (("l", "0"), ("t", "0"), ("r", "0"), ("b", "0")):
        eff.set(side, v)
    docpr = etree.SubElement(inline, wp + "docPr")
    docpr.set("id", str(drawing_id))
    docpr.set("name", f"calcbook-image-{drawing_id}")
    etree.SubElement(inline, wp + "cNvGraphicFramePr")
    graphic = etree.SubElement(inline, a + "graphic")
    gdata = etree.SubElement(graphic, a + "graphicData")
    gdata.set("uri", "http://schemas.openxmlformats.org/drawingml/2006/picture")

    p = etree.SubElement(gdata, pic + "pic")
    nv = etree.SubElement(p, pic + "nvPicPr")
    cpr = etree.SubElement(nv, pic + "cNvPr")
    cpr.set("id", str(drawing_id))
    cpr.set("name", f"calcbook-image-{drawing_id}")
    etree.SubElement(nv, pic + "cNvPicPr")
    bfill = etree.SubElement(p, pic + "blipFill")
    blip = etree.SubElement(bfill, a + "blip")
    blip.set(_EXP + "embed", rid)
    stretch = etree.SubElement(bfill, a + "stretch")
    etree.SubElement(stretch, a + "fillRect")
    sp = etree.SubElement(p, pic + "spPr")
    xfrm = etree.SubElement(sp, a + "xfrm")
    off = etree.SubElement(xfrm, a + "off")
    off.set("x", "0")
    off.set("y", "0")
    ext = etree.SubElement(xfrm, a + "ext")
    ext.set("cx", str(cx_emu))
    ext.set("cy", str(cy_emu))
    prst = etree.SubElement(sp, a + "prstGeom")
    prst.set("prst", "rect")
    etree.SubElement(prst, a + "avLst")
    return d


def _replace_para_with_drawing(
    para: etree._Element,
    drawing: etree._Element,
) -> None:
    """用合法的 w:r/w:drawing 替换图片槽段落，同时保留段落属性。"""
    p_pr = para.find(qn("w:pPr"))
    for child in list(para):
        para.remove(child)
    if p_pr is not None:
        para.append(p_pr)
    run = etree.SubElement(para, qn("w:r"))
    run.append(drawing)


