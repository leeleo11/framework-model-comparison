"""OOXML (DOCX) 原位修改共享工具库。

设计原则：
- 只用 Python 标准库 zipfile + lxml，不引入第三方 DOCX 组件；
- DocxPackage 将整个包读入内存，未修改的 part 写回时保持字节不变；
- 所有结构化修改通过 lxml 在 XML 树上进行，保留未触及元素的原始属性与格式。
"""
from __future__ import annotations

import copy
import hashlib
import io
import zipfile
from dataclasses import dataclass
from typing import Optional

from lxml import etree

# ---------------------------------------------------------------- namespaces
NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "c": "http://schemas.openxmlformats.org/drawingml/2006/chart",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "v": "urn:schemas-microsoft-com:vml",
    "o": "urn:schemas-microsoft-com:office:office",
    "rel": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
    "mc": "http://schemas.openxmlformats.org/markup-compatibility/2006",
    "w14": "http://schemas.microsoft.com/office/word/2010/wordml",
    "m": "http://schemas.openxmlformats.org/officeDocument/2006/math",
}

def qn(tag: str) -> str:
    """'w:tbl' -> '{ns}tbl'"""
    prefix, local = tag.split(":", 1)
    return f"{{{NS[prefix]}}}{local}"


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


class DocxPackage:
    """DOCX(ZIP) 包的内存表示。

    - parts: {part_name: bytes}，未知/未修改 part 写回时字节不变；
    - XML part 通过 get_xml/set_xml 以 lxml 树操作。
    """

    def __init__(self, parts: dict[str, bytes], order: list[str]):
        self.parts = parts
        self.order = order
        self._xml_cache: dict[str, etree._Element] = {}
        self._xml_dirty: set[str] = set()

    @classmethod
    def open(cls, path: str) -> "DocxPackage":
        parts: dict[str, bytes] = {}
        order: list[str] = []
        with zipfile.ZipFile(path, "r") as z:
            for info in z.infolist():
                parts[info.filename] = z.read(info.filename)
                order.append(info.filename)
        return cls(parts, order)

    def get_xml(self, name: str) -> etree._Element:
        if name not in self._xml_cache:
            parser = etree.XMLParser(remove_blank_text=False, resolve_entities=False)
            self._xml_cache[name] = etree.fromstring(self.parts[name], parser)
        return self._xml_cache[name]

    def set_xml(self, name: str, root: etree._Element) -> None:
        self._xml_cache[name] = root
        self._xml_dirty.add(name)

    def mark_dirty(self, name: str) -> None:
        self._xml_dirty.add(name)

    def set_part(self, name: str, data: bytes) -> None:
        self.parts[name] = data
        if name not in self.order:
            self.order.append(name)
        self._xml_cache.pop(name, None)
        self._xml_dirty.discard(name)

    def get_part(self, name: str) -> Optional[bytes]:
        return self.parts.get(name)

    def write(self, path: str) -> None:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in self.order:
                if name in self._xml_dirty:
                    # 只有被显式标记为 dirty 的 part 才重新序列化
                    root = self._xml_cache[name]
                    data = etree.tostring(
                        root, xml_declaration=True, encoding="UTF-8", standalone=True
                    )
                else:
                    # 读过但未修改的 part 保持原始字节
                    data = self.parts[name]
                z.writestr(name, data)
        with open(path, "wb") as f:
            f.write(buf.getvalue())

    def rels(self, part: str = "word/document.xml") -> dict[str, "Relationship"]:
        """读取某 part 的 .rels，返回 {rId: Relationship}。"""
        base, _, fname = part.rpartition("/")
        rels_name = f"{base}/_rels/{fname}.rels" if base else f"_rels/{fname}.rels"
        out: dict[str, Relationship] = {}
        if rels_name in self._xml_cache:
            root = self._xml_cache[rels_name]
        else:
            data = self.parts.get(rels_name)
            if data is None:
                return out
            root = etree.fromstring(data)
        for rel in root.findall(qn("rel:Relationship")):
            rid = rel.get("Id")
            out[rid] = Relationship(
                rid=rid,
                type=rel.get("Type", ""),
                target=rel.get("Target", ""),
                target_mode=rel.get("TargetMode"),
            )
        return out

@dataclass
class Relationship:
    rid: str
    type: str
    target: str
    target_mode: Optional[str] = None

# ------------------------------------------------------- text replacement
def tbl_rows(tbl: etree._Element) -> list[etree._Element]:
    return tbl.findall(qn("w:tr"))


def row_cells(tr: etree._Element) -> list[etree._Element]:
    return tr.findall(qn("w:tc"))


def cell_text(tc: etree._Element) -> str:
    return "".join(t.text or "" for t in tc.iter(qn("w:t")))


def row_texts(tr: etree._Element) -> list[str]:
    return [cell_text(tc) for tc in row_cells(tr)]


def is_header_row(tr: etree._Element) -> bool:
    trPr = tr.find(qn("w:trPr"))
    return trPr is not None and trPr.find(qn("w:tblHeader")) is not None


def set_cell_text(tc: etree._Element, text: str, color: Optional[str] = None) -> None:
    """改写单元格文本：保留第一个段落第一个 run 的格式，清空其余文本 run。
    color 为 'FF0000' 等时覆盖 run 颜色（用于【待填写】标记）。"""
    ps = tc.findall(qn("w:p"))
    if not ps:
        p = etree.SubElement(tc, qn("w:p"))
        r = etree.SubElement(p, qn("w:r"))
        t = etree.SubElement(r, qn("w:t"))
        t.text = text
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        return
    first_done = False
    for p in ps:
        runs = p.findall(qn("w:r"))
        for r in runs:
            for child in list(r):
                if child.tag in (qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr")):
                    r.remove(child)
        if not first_done:
            carrier = runs[0] if runs else etree.SubElement(p, qn("w:r"))
            if color is not None:
                rPr = carrier.find(qn("w:rPr"))
                if rPr is None:
                    rPr = etree.Element(qn("w:rPr"))
                    carrier.insert(0, rPr)
                for old in rPr.findall(qn("w:color")):
                    rPr.remove(old)
                c = etree.SubElement(rPr, qn("w:color"))
                c.set(qn("w:val"), color)
            t = etree.SubElement(carrier, qn("w:t"))
            t.text = text
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
            first_done = True


# ---------------------------------------------------------------- misc
def para_text(p: etree._Element) -> str:
    return "".join(t.text or "" for t in p.iter(qn("w:t")))


def para_style(p: etree._Element) -> Optional[str]:
    st = p.find(f"{qn('w:pPr')}/{qn('w:pStyle')}")
    return st.get(qn("w:val")) if st is not None else None


# ---------------------------------------------------------------- block operations
def replace_paragraph_text(p: etree._Element, new_text: str) -> None:
    """替换段落全部可见文本，保留 pPr 和首个有效 rPr。

    - 保留 w:pPr（段落格式）；
    - 保留第一个含 w:rPr 的 run 的格式；
    - 清理所有 run 的文本承载子元素后，在第一个 run 写入新文本。
    """
    # 保存第一个有效 rPr
    first_rPr = None
    for r in p.findall(qn("w:r")):
        rPr = r.find(qn("w:rPr"))
        if rPr is not None:
            first_rPr = copy.deepcopy(rPr)
            break

    # 清理所有 run 的文本承载子元素
    for r in p.findall(qn("w:r")):
        for child in list(r):
            if child.tag in (qn("w:t"), qn("w:tab"), qn("w:br"), qn("w:cr"),
                             qn("w:noBreakHyphen"), qn("w:softHyphen"), qn("w:sym")):
                r.remove(child)

    # 找到第一个 run 作为载体，或新建
    runs = p.findall(qn("w:r"))
    if runs:
        carrier = runs[0]
    else:
        carrier = etree.Element(qn("w:r"))
        # 插入到 pPr 之后
        pPr = p.find(qn("w:pPr"))
        if pPr is not None:
            pPr.addnext(carrier)
        else:
            p.insert(0, carrier)

    # 恢复 rPr
    if first_rPr is not None:
        existing_rPr = carrier.find(qn("w:rPr"))
        if existing_rPr is not None:
            carrier.remove(existing_rPr)
        carrier.insert(0, first_rPr)

    # 写入新文本
    t = etree.SubElement(carrier, qn("w:t"))
    t.text = new_text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _update_content_type(pkg: "DocxPackage", old_ext: str, new_ext: str) -> None:
    """更新 [Content_Types].xml 中的扩展名映射（幂等，不重复添加）。"""
    ct_name = "[Content_Types].xml"
    if ct_name not in pkg.parts:
        return
    root = pkg.get_xml(ct_name)
    ext_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".emf": "image/x-emf",
        ".wmf": "image/x-wmf",
    }
    new_ext_clean = new_ext.lstrip(".")
    mime = ext_map.get(new_ext, "application/octet-stream")

    # 检查是否已存在相同扩展名映射
    exists = False
    for el in list(root):
        ext = el.get("Extension")
        if ext and ext.lstrip(".") == new_ext_clean:
            # 已存在，更新 ContentType（如果不同）
            if el.get("ContentType") != mime:
                el.set("ContentType", mime)
                pkg.mark_dirty(ct_name)
            exists = True
            break

    # 不存在则添加
    if not exists:
        ext_el = etree.SubElement(root, qn("ct:Default"))
        ext_el.set("Extension", new_ext_clean)
        ext_el.set("ContentType", mime)
        pkg.mark_dirty(ct_name)


# ---------------------------------------------------------------- run 文本/红字（渲染共用）
RED = "FF0000"

def _set_run_text(run: etree._Element, text: str, red: bool = False) -> None:
    """写入 run 文本；red=True 时把该 run 染红。"""
    for t in run.findall(qn("w:t")):
        t.text = None
    t = etree.SubElement(run, qn("w:t"))
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    if red:
        rPr = run.find(qn("w:rPr"))
        if rPr is None:
            rPr = etree.Element(qn("w:rPr"))
            run.insert(0, rPr)
        for old in rPr.findall(qn("w:color")):
            rPr.remove(old)
        c = etree.SubElement(rPr, qn("w:color"))
        c.set(qn("w:val"), RED)

def _set_run_texts_in_para(p: etree._Element, text: str) -> None:
    runs = [r for r in p if r.tag == qn("w:r")]
    if runs:
        _set_run_text(runs[0], text)
        for r in runs[1:]:
            for t in r.findall(qn("w:t")):
                t.text = None
    else:
        r = etree.SubElement(p, qn("w:r"))
        _set_run_text(r, text)

def _make_run_red(run: etree._Element) -> None:
    """把 run 染成红色（用于内联缺失提示）。"""
    rPr = run.find(qn("w:rPr"))
    if rPr is None:
        rPr = etree.Element(qn("w:rPr"))
        run.insert(0, rPr)
    for c in rPr.findall(qn("w:color")):
        rPr.remove(c)
    c = etree.SubElement(rPr, qn("w:color"))
    c.set(qn("w:val"), RED)

def _make_para_red(p: etree._Element) -> None:
    for r in p.findall(qn("w:r")):
        _make_run_red(r)



def _strip_tag_red(run: etree._Element) -> None:
    """清除 run 上 compile 染的标签红（FF0000）——填入正常数据时沿用模板格式。

    只删 val==FF0000 的 w:color，不动模板自带的其他颜色。
    """
    rPr = run.find(qn("w:rPr"))
    if rPr is None:
        return
    for color in rPr.findall(qn("w:color")):
        if color.get(qn("w:val")) == RED:
            rPr.remove(color)


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return f"{v:.6g}"
    return str(v)
