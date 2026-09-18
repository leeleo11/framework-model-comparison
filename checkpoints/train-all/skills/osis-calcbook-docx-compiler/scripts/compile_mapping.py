"""compile_mapping：OSISAI taggedits → 写入 Tag + 生成 mapping.json + 打包三件套（固定脚本，零 AI 业务判断）。

职责（含原 mapping.py 校验与 template_package.py 打包，合并为 self-contained）：

一、Tag 写入（compile_from_taggedits）
- 段落 Tag 优先按 prepared-view 的 temp_id/出现顺序定位，文本只作校验与旧协议回退；
- text(value)：段落内 replace_span → `【TAG】`（标签/单位保留）；
- text(format)：原整句 → `【TAG】`（段落格式保留）；
- table：caption（表题）→ 其后第一个 w:tbl，提取样式骨架（tblPr/tblGrid/表头行/数据行样例），
  表格 → `【TAG】` 段（表题保留）；
- image：caption（图题）→ 上方图槽段写 `【TAG】`；mapping 保存 caption_name，
  Render 只替换图名字串（图号/单位静态）。

二、mapping 校验（validate_mapping）——Tag 命名 / 唯一 / source 可解析 / format 占位一致。

三、三件套打包（build_template_package）—— {template.docx, mapping.json, package.json} 事务式。

用法：
    python compile_mapping.py prepared-template.docx taggedits.json prepared-view.json -o OUTDIR [--id ID]
    python compile_mapping.py packages build template.docx --mapping mapping.json --id ID -o OUTDIR
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import shutil
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from lxml import etree

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from ooxml_utils import (  # noqa: E402
    DocxPackage, qn, para_text, sha256_file, tbl_rows,
    replace_paragraph_text,
)
from standard_dict import BJ as _BJ  # noqa: E402
from standard_dict import MISS_NAME_RE, TAG_NAME_RE, TAG_RE  # noqa: E402
from standard_dict import STANDARD_STRUCTURE_VERSION as _BJ_VERSION  # noqa: E402
from standard_dict import sha256 as _bj_sha256  # noqa: E402

#: 标准字典 sha（模块加载时计算一次）
_BJ_SHA = _bj_sha256()

# ================================================================ Tag 约定
#: 标签形如 `【image-shear】`（模块-内容），正则与命名规则统一定义在 standard_dict。
VALID_TYPES = ("text", "format", "conclusion", "table", "image")
RED = "FF0000"


def tag_names(text: str) -> list[str]:
    """提取字符串里所有 【模块-内容】 标签名（去重复、保留出现顺序）。"""
    seen: list[str] = []
    for m in TAG_RE.finditer(text):
        name = m.group(0)[1:-1]
        if name not in seen:
            seen.append(name)
    return seen


def _materialize_missing_tags(
    review: dict[str, Any], view_blocks: Optional[list[dict]] = None
) -> tuple[dict[str, Any], list[str]]:
    """校验未决条目的内容名，并同步 reviewed_blocks / reviewed_groups 中的引用。

    未决条目（unresolved: true）必须由 AI 按内容命名 ``tag = miss-<slug>``
    （小写字母/数字/连字符，拼音或英文缩写）；compile 强制唯一——两个不同
    未决位置不得共用一个 miss 名。返回 ``(review, errors)``：命名缺失、不合法
    或重复都记入 errors，不产生任何静默编号。
    """
    items = review.get("taggedits") or []
    blocks = review.get("reviewed_blocks") or []
    groups = review.get("reviewed_groups") or []

    block_by_id = {b.get("temp_id"): b for b in blocks if b.get("temp_id")}
    group_by_id = {g.get("group_id"): g for g in groups if g.get("group_id")}

    def tag_refs(block: dict) -> list:
        refs = block.get("tags")
        if isinstance(refs, str) and refs:
            refs = [refs]
            block["tags"] = refs
        elif not isinstance(refs, list):
            refs = []
            block["tags"] = refs
        return refs

    for block in blocks:
        tag_refs(block)

    errors: list[str] = []
    seen_miss: dict[str, str] = {}

    for item in items:
        if not item.get("unresolved"):
            continue
        tag = item.get("tag")
        where = (f"temp_id={item.get('temp_id')}" if item.get("temp_id")
                 else f"group_id={item.get('group_id')}")
        if not tag:
            errors.append(
                f"未决条目缺少内容名（{where}）：tag 写 miss-<拼音/英文slug>，"
                f"如 miss-kangla / miss-tension-envelope")
            continue
        if not MISS_NAME_RE.match(str(tag)):
            errors.append(
                f"未决条目命名不合法（{where}）: {tag!r}"
                f"——应为 miss-<slug>（小写字母/数字/连字符，不以数字开头）")
            continue
        if tag in seen_miss:
            errors.append(
                f"未决条目命名重复: {tag!r}（已用于 {seen_miss[tag]}，"
                f"现用于 {where}）——每个未决位置需要唯一内容名")
            continue
        seen_miss[tag] = where
        item["source"] = None

        temp_id = item.get("temp_id")
        if temp_id in block_by_id:
            refs = tag_refs(block_by_id[temp_id])
            if tag not in refs:
                refs.append(tag)

        # table/image 的题注由同一槽管理；缺失 Tag 同步到对应题注审阅块。
        if item.get("kind") in ("table", "image") and view_blocks:
            caption = str(item.get("caption") or "").strip()
            if caption:
                for view_block in view_blocks:
                    related_id = view_block.get("temp_id")
                    if related_id == temp_id or related_id not in block_by_id:
                        continue
                    visible = str(
                        view_block.get("text") or view_block.get("caption") or ""
                    ).strip()
                    if visible != caption:
                        continue
                    refs = tag_refs(block_by_id[related_id])
                    if tag not in refs:
                        refs.append(tag)

        group_id = item.get("group_id")
        group = group_by_id.get(group_id)
        if group is not None:
            group["tag"] = tag
            for member in group.get("member_temp_ids") or []:
                block = block_by_id.get(member)
                if block is None:
                    continue
                refs = tag_refs(block)
                if tag not in refs:
                    refs.append(tag)
    return review, errors


# ================================================================ mapping 校验
def write_mapping(mapping: dict[str, Any], path: str | os.PathLike) -> None:
    """原子写 mapping.json。"""
    _atomic_write_json(path, mapping)


def _atomic_write_json(path, obj) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=target.name + ".", suffix=".tmp",
                               dir=str(target.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.replace(tmp, str(target))
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def validate_mapping(
    mapping: dict[str, Any],
    *,
    brief: dict[str, Any] | None = None,
    doc_text: str | None = None,
) -> list[str]:
    """校验 mapping 结构（可选：brief 键存在性、doc 中 Tag 唯一性）。返回错误列表。"""
    errors: list[str] = []

    if not isinstance(mapping, dict):
        return ["mapping 必须是对象"]
    if mapping.get("mapping_schema") != 3:
        errors.append(f"mapping_schema 必须为 3，实际 {mapping.get('mapping_schema')!r}")
    slots = mapping.get("slots")
    if not isinstance(slots, dict):
        return errors + ["缺少 slots 对象"]

    seen: set[str] = set()
    for tag in slots:
        if tag in seen:
            errors.append(f"重复的 Tag: {tag}")
        seen.add(tag)
        if not TAG_NAME_RE.match(tag):
            errors.append(f"Tag 命名不合法: {tag!r}（应形如 模块-内容，如 image-shear；见 standard_dict）")

    for tag, slot in slots.items():
        if not isinstance(slot, dict):
            errors.append(f"slots[{tag}] 必须是对象")
            continue
        stype = slot.get("type")
        if stype not in VALID_TYPES:
            errors.append(f"slots[{tag}].type 非法: {stype!r}（应为 text/format/table/image）")
            continue

        unresolved = slot.get("source") is None and tag.startswith("miss-")

        if stype == "format":
            if unresolved:
                continue
            if slot.get("repeat_source"):
                # 动态列表：item_template 占位符与 item_sources 键一致
                item_tmpl = slot.get("item_template") or ""
                item_src = slot.get("item_sources") or {}
                if not item_tmpl:
                    errors.append(f"slots[{tag}] repeat format 缺少 item_template")
                used = set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", item_tmpl))
                keys = set(item_src)
                if not used.issubset(keys):
                    errors.append(
                        f"slots[{tag}] item_template 占位符 "
                        f"{sorted(used - keys)} 无对应 item_sources")
                if not keys.issubset(used):
                    errors.append(
                        f"slots[{tag}] item_sources 键 "
                        f"{sorted(keys - used)} 未在 item_template 中使用")
            else:
                if not slot.get("template"):
                    errors.append(f"slots[{tag}] type=format 缺少 template")
                sources = slot.get("sources") or {}
                if not isinstance(sources, dict):
                    errors.append(f"slots[{tag}] type=format sources 必须是对象")
                else:
                    used = set(re.findall(
                        r"\{([a-z_][a-z0-9_]*)\}", slot.get("template") or ""))
                    extra = set(sources)
                    if not used.issubset(extra):
                        errors.append(
                            f"slots[{tag}] template 占位符 {sorted(used - extra)} 无对应 sources")
                    if not extra.issubset(used):
                        errors.append(
                            f"slots[{tag}] sources 键 {sorted(extra - used)} 未在 template 中使用")
        elif stype in ("text", "table", "image"):
            if "source" not in slot:
                errors.append(f"slots[{tag}] type={stype} 缺少 source 键（可为 null）")
        elif stype == "conclusion":
            if "source" not in slot:
                errors.append(f"slots[{tag}] type=conclusion 缺少 source 键（可为 null）")
            conclusion_source = slot.get("verdict_source") or slot.get("source")
            has_fixed_default = (
                isinstance(conclusion_source, str)
                and conclusion_source.startswith("verdict:")
            )
            if (not unresolved and not has_fixed_default
                    and not (slot.get("ok_text") or slot.get("ok_template"))):
                errors.append(f"slots[{tag}] conclusion 缺少 ok 句式（ok_text/ok_template）")
            if (not unresolved and not has_fixed_default
                    and not (slot.get("ng_text") or slot.get("ng_template"))):
                errors.append(f"slots[{tag}] conclusion 缺少 ng 句式（ng_text/ng_template）")
            # ok_text/ng_text 是纯文本：含任何 {占位} 都拒绝（占位必须放 *_template）
            for f in ("ok_text", "ng_text"):
                txt = slot.get(f)
                if txt and re.search(r"\{[a-z_][a-z0-9_]*\}", str(txt)):
                    errors.append(f"slots[{tag}] {f} 必须是纯文本（不得含占位符）")
            # 句式占位符必须与 source 类型一致（否则 resolve 必然失败→红字）
            src = conclusion_source
            if isinstance(src, str):
                skind = src.partition(":")[0]
                allowed = ({"verdict"} if skind == "field"
                           else {"verdict", "control", "limit", "unit"} if skind == "verdict"
                           else None)
            else:
                allowed = None
            for f in ("ok_template", "ng_template"):
                tmpl = slot.get(f)
                if not tmpl:
                    continue
                used = set(re.findall(r"\{([a-z_][a-z0-9_]*)\}", tmpl))
                extra = used - {"verdict", "control", "limit", "unit"}
                if extra:
                    errors.append(
                        f"slots[{tag}] {f} 占位符超出允许域 "
                        f"(verdict/control/limit/unit): {sorted(extra)}")
                if allowed is not None:
                    banned = used - allowed
                    if banned:
                        errors.append(
                            f"slots[{tag}] {f} 占位符 {sorted(banned)} 与 source={skind}: "
                            f"不匹配（{skind} 源仅提供 {sorted(allowed)}；"
                            f"需要 control/limit 请改用 verdict: 源，或写成纯文本）")

        if (stype in ("text", "table", "image", "conclusion")
                and brief and slot.get("source") is not None):
            _check_source(slot.get("source"), tag, errors)

    if doc_text is not None:
        found = tag_names(doc_text)
        for tag in sorted(seen):
            if found.count(tag) == 0:
                errors.append(f"Tag {tag} 在模板中不存在")
            # 允许同一 tag 多处出现（同一数据项多位置引用同一槽位，compile 不覆盖槽）；
            # "不同数据项用同一 tag"由首接审阅 + 槽不覆盖保证，不由出现次数判定。
        for name in found:
            if name not in seen:
                errors.append(f"模板中出现的 Tag {name} 不在 mapping.slots 中")

    return errors


def _check_source(source, tag, errors) -> None:
    """校验固定 source 的写法，不用当前样例项目裁剪标准字典。

    模板只编译一次；首接样例没有某项输出，并不表示后续项目也没有。
    真实项目是否取到值统一留给 render 判定并标红。
    """
    if source is None:
        return
    if not isinstance(source, str):
        errors.append(f"slots[{tag}].source 类型非法: {type(source).__name__}")
        return
    kind, _, ref = source.partition(":")
    if kind not in ("field", "table", "image", "verdict"):
        errors.append(f"slots[{tag}].source 前缀未知: {source}")
    elif not ref:
        errors.append(f"slots[{tag}].source 缺少数据键: {source!r}")


# ================================================================ 三件套打包
class PackageBuildRefused(Exception):
    """打包前置拒绝（输入非法），不产生任何产物。"""


class PackageBuildFailed(Exception):
    """打包过程失败（staging 已清理）。"""

    def __init__(self, message, report=None):
        super().__init__(message)
        self.report = report


def build_template_package(
    template_docx_path: str,
    mapping_path: str,
    output_dir: str,
    template_id: str,
    scope: Optional[dict[str, str]] = None,
    source_template_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """构建三件套模板包 {template.docx, mapping.json, package.json}（staging 事务）。"""
    if not os.path.isfile(template_docx_path):
        raise PackageBuildRefused(f"template.docx 不存在: {template_docx_path}")
    if not os.path.isfile(mapping_path):
        raise PackageBuildRefused(f"mapping.json 不存在: {mapping_path}")

    template_sha = sha256_file(template_docx_path)
    mapping_sha = sha256_file(mapping_path)
    sha12 = template_sha[:12]
    package_name = f"{template_id}-{sha12}"
    output_root = Path(output_dir)
    final_dir = output_root / package_name

    if final_dir.exists():
        existing_pkg = final_dir / "package.json"
        if existing_pkg.exists():
            with open(existing_pkg, "r", encoding="utf-8") as f:
                existing = json.load(f)
            if existing.get("template_sha256") == template_sha:
                return {
                    "status": "exists",
                    "package_dir": str(final_dir),
                    "message": "模板包已存在且 SHA 匹配，跳过构建",
                }
        raise PackageBuildRefused(f"模板包已存在: {final_dir}")

    final_dir.parent.mkdir(parents=True, exist_ok=True)
    staging = final_dir.parent / f".staging-{uuid.uuid4().hex[:8]}"
    staging.mkdir()
    try:
        shutil.copy2(template_docx_path, str(staging / "template.docx"))
        shutil.copy2(mapping_path, str(staging / "mapping.json"))
        package_meta = {
            "package_schema": 1,
            "template_id": template_id,
            # 包内 template.docx（写 Tag 成品）的哈希；load_package 用它校验。
            "template_sha256": template_sha,
            "mapping_sha256": mapping_sha,
            # 源模板（原始 DOCX）哈希：二次渲染按它自动找到本包。
            "source_template_sha256": source_template_sha256 or "",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "scope": scope or {},
        }
        _write_json(staging / "package.json", package_meta)
        os.replace(staging, final_dir)
        staging = None
        return {
            "status": "built",
            "package_dir": str(final_dir),
            "template_id": template_id,
            "template_sha256": template_sha,
            "mapping_sha256": mapping_sha,
        }
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)


def load_package(package_dir: str) -> dict[str, Any]:
    """加载模板包，返回 {template_path, mapping, package_meta}（SHA 校验）。"""
    pkg_path = Path(package_dir)
    pkg_json_path = pkg_path / "package.json"
    if not pkg_json_path.exists():
        raise FileNotFoundError(f"模板包缺少 package.json: {package_dir}")
    with open(pkg_json_path, "r", encoding="utf-8") as f:
        package_meta = json.load(f)

    mapping_path = pkg_path / "mapping.json"
    if not mapping_path.exists():
        raise FileNotFoundError(f"模板包缺少 mapping.json: {package_dir}")
    with open(mapping_path, "r", encoding="utf-8") as f:
        mapping = json.load(f)

    template_path = pkg_path / "template.docx"
    if not template_path.exists():
        raise FileNotFoundError(f"模板包缺少 template.docx: {package_dir}")

    actual_sha = sha256_file(str(template_path))
    expected_sha = package_meta.get("template_sha256", "")
    if expected_sha and actual_sha != expected_sha:
        raise ValueError(
            f"template.docx SHA 不匹配: expected={expected_sha[:16]}... "
            f"vs actual={actual_sha[:16]}...")
    return {
        "template_path": str(template_path),
        "mapping": mapping,
        "package_meta": package_meta,
    }


def install_template_package(
        package_dir: str, calcbook_root: Optional[str]) -> dict[str, Any]:
    """安装模板包到 <root>/packages/（SHA 幂等）。"""
    calcbook_root = calcbook_root or os.environ.get("OSIS_CALCBOOK_ROOT")
    if not calcbook_root:
        raise PackageBuildRefused(
            "未指定模板包安装目录，且 OSIS_CALCBOOK_ROOT 未设置")
    src = Path(package_dir)
    pkg_json = src / "package.json"
    if not pkg_json.exists():
        raise PackageBuildRefused(f"模板包缺少 package.json: {package_dir}")
    with open(pkg_json, "r", encoding="utf-8") as f:
        meta = json.load(f)
    for name in ("template.docx", "mapping.json"):
        if not (src / name).exists():
            raise PackageBuildRefused(f"模板包缺少 {name}: {package_dir}")
    actual_sha = sha256_file(str(src / "template.docx"))
    expected_sha = meta.get("template_sha256", "")
    if expected_sha and actual_sha != expected_sha:
        raise PackageBuildRefused(
            f"template.docx SHA 不匹配: package.json记录={expected_sha[:16]}... "
            f"vs 实际={actual_sha[:16]}...")
    package_name = src.name
    target = Path(calcbook_root) / "packages" / package_name
    if target.exists():
        target_pkg = target / "package.json"
        if target_pkg.exists():
            with open(target_pkg, "r", encoding="utf-8") as f:
                target_meta = json.load(f)
            if target_meta.get("template_sha256") == actual_sha:
                return {
                    "status": "already_installed",
                    "package_dir": str(target),
                    "message": f"模板包已安装且 SHA 匹配: {package_name}",
                }
        raise PackageBuildRefused(
            f"目标目录已存在且 SHA 不同: {target}。请先删除旧包或换 template_id")
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(str(src), str(target))
    return {
        "status": "installed",
        "package_dir": str(target),
        "package_name": package_name,
        "template_id": meta.get("template_id", ""),
        "template_sha256": meta.get("template_sha256", ""),
    }


def find_package_by_id(template_id: str, calcbook_root: str) -> Optional[dict[str, Any]]:
    """在 packages/ 查找匹配 template_id 的包（最新优先）。"""
    packages = Path(calcbook_root) / "packages"
    if not packages.is_dir():
        return None
    matches = []
    for d in packages.iterdir():
        if not d.is_dir():
            continue
        pkg_json = d / "package.json"
        if not pkg_json.exists():
            continue
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("template_id") == template_id:
            matches.append({"package_dir": str(d), "package_meta": meta})
    if not matches:
        return None
    matches.sort(key=lambda m: m["package_meta"].get("created_at", ""), reverse=True)
    return matches[0]


def find_package_by_source_template(
    source_template_sha256: str, calcbook_root: str
) -> Optional[dict[str, Any]]:
    """按源模板（原始 DOCX）SHA 查找模板包——换项目二次渲染时自动路由。

    匹配 package.json.source_template_sha256（compile 时由调用方传入原始模板
    哈希写入）；旧包无该字段则不匹配。命中多个取最新。
    """
    packages = Path(calcbook_root) / "packages"
    if not packages.is_dir():
        return None
    matches = []
    for d in packages.iterdir():
        if not d.is_dir():
            continue
        pkg_json = d / "package.json"
        if not pkg_json.exists():
            continue
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        if meta.get("source_template_sha256") == source_template_sha256:
            matches.append({"package_dir": str(d), "package_meta": meta})
    if not matches:
        return None
    matches.sort(key=lambda m: m["package_meta"].get("created_at", ""), reverse=True)
    return matches[0]


def list_installed_packages(calcbook_root: str) -> list[dict[str, Any]]:
    packages = Path(calcbook_root) / "packages"
    if not packages.is_dir():
        return []
    result = []
    for d in sorted(packages.iterdir()):
        if not d.is_dir():
            continue
        pkg_json = d / "package.json"
        if not pkg_json.exists():
            continue
        try:
            with open(pkg_json, "r", encoding="utf-8") as f:
                meta = json.load(f)
        except (json.JSONDecodeError, OSError):
            continue
        result.append({
            "package_dir": str(d),
            "package_name": d.name,
            "template_id": meta.get("template_id", ""),
            "template_sha256": meta.get("template_sha256", "")[:16],
            "created_at": meta.get("created_at", ""),
            "scope": meta.get("scope", {}),
        })
    return result


def find_default_package(
    calcbook_root: str,
) -> tuple[Optional[dict[str, Any]], list[dict[str, Any]]]:
    """render 未指定包时的自动发现：唯一已装包 → 直接返回；多个 → 返回候选列表。

    返回 ``(package, candidates)``：唯一命中时 package 非 None；0 个或多个时
    package 为 None，candidates 为全部已装包（0 个时空列表），由调用方生成
    可操作的错误提示。二次渲染不需要源模板文件即可完成路由。
    """
    packages = list_installed_packages(calcbook_root)
    if len(packages) == 1:
        return packages[0], packages
    return None, packages


def _write_json(path: Path, obj: dict) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


# ================================================================ Tag 写入
class CompileMappingError(Exception):
    def __init__(self, message, failures=None):
        super().__init__(message)
        self.failures = failures or []


def _normalize_col(name: Any) -> str:
    """表头列名归一化（去空白/括号/下划线、小写、去 γ 前缀），用于整列名匹配。"""
    return re.sub(r"[\s_()（）\[\]{}]+", "", str(name)).casefold().lstrip("γ")


#: 列语义别名组：同物理量不同措辞（材料/容重/模量等）。匹配时归一化到组名。
_COLUMN_KEY_ALIASES: dict[str, set[str]] = {
    "材料名称": {"材料名称", "名称", "材料", "预应力钢筋材料名称",
                "预应力钢筋名称", "普通钢筋名称", "钢筋名称", "钢束名称",
                "属性名称"},
    "容重": {"容重", "密度"},
    "弹性模量": {"弹性模量", "弹模", "e"},
    "强度等级": {"强度等级", "强度级别"},
    # 预应力钢筋特性值表（midas 模板列）↔ tTDPChar（OSIS 列）
    "管道直径": {"管道直径", "波纹管外直径", "波纹管直径"},
    "摩阻系数": {"管道摩阻系数", "摩阻系数", "μ", "mu"},
    "偏差系数": {"局部偏差系数", "偏差系数", "k"},
    "锚具变形": {"锚具变形", "锚具变形量", "起点弹性回缩总变形"},
}


def _column_key(name: Any) -> str:
    """列匹配的语义主体键：去掉括号单位后归一化；命中别名组 → 组名。

    让"弹性模量(MPa)"与"弹性模量(N / m ^ 2)"、'"容重(kN/m3)"与"密度(N/m^3)"视为同一物理量。
    """
    text = str(name)
    # 去掉尾部括号单位
    base = re.sub(r"[（(][^（）()]*[)）]\s*$", "", text)
    if not base:
        base = text
    key = re.sub(r"[\s_（）\[\]{}]+", "", base).casefold().lstrip("γ")
    for group, members in _COLUMN_KEY_ALIASES.items():
        if key in members:
            return group.casefold()
    return key


def _paragraph_runs(p: etree._Element) -> list:
    return [ch for ch in p if ch.tag == qn("w:r")]


def _paragraph_text_pieces(p: etree._Element) -> list:
    """按文档序收集段落可见文本片段（run 内 fldChar/instrText/w:t 交错，Midas 单 run 混合）。

    返回 [(w:t 元素, 文本, start, end, is_field)]：is_field=True 表示该 w:t 是 Word 字段的
    结果文本（fldChar begin..end 之间），作为**原子区块**（不可被替换 span 部分切断）。
    """
    pieces = []
    pos = 0
    in_field = False
    for r in _paragraph_runs(p):
        for ch in r:
            if ch.tag == qn("w:fldChar"):
                if ch.get(qn("w:fldCharType")) == "begin":
                    in_field = True
                elif ch.get(qn("w:fldCharType")) == "end":
                    in_field = False
                continue
            if ch.tag == qn("w:instrText"):
                continue  # 不可见
            if ch.tag == qn("w:t"):
                txt = ch.text or ""
                pieces.append((ch, txt, pos, pos + len(txt), in_field))
                pos += len(txt)
    return pieces


def _replace_span_in_para(p: etree._Element, old: str, new: str) -> bool:
    """段内**唯一**子串替换（run 内字段原子性，保留字段结构）。

    - 0 或多命中返回 False；
    - Word 字段（fldChar..instrText..结果 w:t..end）作为原子区块：
      替换 span **完整覆盖**字段 → 允许（该字段整体替换）；
      替换 span **部分切断**字段 → CompileMappingError；
      字段**结束后**的普通文字 → 正常替换（不再因同一 run 含字段而误拒）。
    - 只修改与 span 重叠的 `w:t`，其余 run 与字段结构保留。
    """
    pieces = _paragraph_text_pieces(p)
    full = "".join(tx for _, tx, _, _, _ in pieces)
    if full.count(old) != 1:
        return False
    span_s = full.find(old)
    span_e = span_s + len(old)

    # 字段原子性：span 与字段部分重叠 → 拒绝；完全包含 → 允许整体移除
    for t_el, txt, s, e, isf in pieces:
        if not isf or e <= span_s or s >= span_e:
            continue
        if not (span_s <= s and e <= span_e):
            raise CompileMappingError(
                f"替换跨度切断 Word 字段结构: {old!r}",
                [f"替换跨度切断 Word 字段结构（含自动编号域）: {old!r}"])

    runs = _paragraph_runs(p)
    # 定位 anchor（含 span 起点的 run）
    anchor = runs[0] if runs else None
    for t_el, tx, s, e, isf in pieces:
        if s <= span_s < e:
            anchor = t_el.getparent()
            break

    # 修改重叠 w:t（普通 left/right；span 完整覆盖的字段结果整体清空）
    right_run = None
    for t_el, tx, s, e, isf in pieces:
        if e <= span_s or s >= span_e:
            continue
        if isf:
            t_el.text = None  # 整个字段被 span 覆盖 → 清字段结果文本
            continue
        txt = t_el.text or ""
        left = txt[: max(0, span_s - s)]
        right = "" if span_e >= e else txt[span_e - s:]
        t_el.text = left or None
        if right and right_run is None:
            right_run = _clone_as_run(t_el.getparent(), right)

    # 插入承载 new 的 run（anchor 之后；有 right-run 则插在它与 anchor 之间）
    carrier_rpr = None
    if anchor is not None:
        rpr = anchor.find(qn("w:rPr"))
        if rpr is not None:
            carrier_rpr = copy.deepcopy(rpr)
    carrier = etree.Element(qn("w:r"))
    if carrier_rpr is not None:
        carrier.append(carrier_rpr)
    t = etree.SubElement(carrier, qn("w:t"))
    t.text = new
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    if anchor is not None:
        anchor.addnext(carrier)
        if right_run is not None:
            carrier.addnext(right_run)
    else:
        pPr = p.find(qn("w:pPr"))
        if pPr is not None:
            pPr.addnext(carrier)
        else:
            p.insert(0, carrier)
    return True


def _clone_as_run(r: etree._Element, text: str) -> etree._Element:
    """新建 run：仅复制源 run 的 rPr（**不复制域结构** fldChar/instrText），内容为 text。

    Midas 单 run 混合（文字+EQ域+结论文字）场景下，right-run 若 deepcopy 整 run
    会把 fldChar/instrText 一并复制 → 域重复。此处只取 rPr。
    """
    rr = etree.Element(qn("w:r"))
    rpr = r.find(qn("w:rPr"))
    if rpr is not None:
        rr.append(copy.deepcopy(rpr))
    t = etree.SubElement(rr, qn("w:t"))
    t.text = text
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return rr


def _make_tag_paragraph(tag: str) -> etree._Element:
    p = etree.Element(qn("w:p"))
    r = etree.SubElement(p, qn("w:r"))
    t = etree.SubElement(r, qn("w:t"))
    t.text = f"【{tag}】"
    t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return p


def _color_tags_red(root: etree._Element) -> None:
    """把编译模板中的字面标签 run 统一染红，便于人工复核。"""
    for run in root.iter(qn("w:r")):
        text = "".join(t.text or "" for t in run.iter(qn("w:t")))
        if not TAG_RE.search(text):
            continue
        rpr = run.find(qn("w:rPr"))
        if rpr is None:
            rpr = etree.Element(qn("w:rPr"))
            run.insert(0, rpr)
        for color in rpr.findall(qn("w:color")):
            rpr.remove(color)
        color = etree.SubElement(rpr, qn("w:color"))
        color.set(qn("w:val"), RED)


def _find_unique_para_by_text(
    body, text: str, kind: str,
    before_text: str | None = None,
    after_text: str | None = None,
) -> Optional[etree._Element]:
    """在 body 中找文本匹配的段落；可加 before/after 复合上下文收窄。

    复合上下文（original+before+after）组合必须唯一，否则返回 None。
    """
    matches = []
    for el in _body_level_paras(body):
        t = para_text(el).strip()
        if kind == "exact" and t != text.strip():
            continue
        if kind == "contain" and text.strip() not in t:
            continue
        if before_text is not None:
            prev = _prev_para(el)
            if prev is None or para_text(prev).strip() != before_text.strip():
                continue
        if after_text is not None:
            nxt = _next_para(el)
            if nxt is None or para_text(nxt).strip() != after_text.strip():
                continue
        matches.append(el)
    return matches[0] if len(matches) == 1 else None


def _body_level_paras(body: etree._Element):
    """按 Prepare 的遍历顺序产出正文段落，不把表格单元格误当正文。"""
    for child in body:
        if child.tag == qn("w:p"):
            yield child
        elif child.tag == qn("w:sdt"):
            content = child.find(qn("w:sdtContent"))
            if content is None:
                continue
            for nested in content:
                if nested.tag == qn("w:p"):
                    yield nested


def _prev_para(el: etree._Element) -> Optional[etree._Element]:
    prev = el.getprevious()
    while prev is not None:
        if prev.tag == qn("w:p"):
            return prev
        prev = prev.getprevious()
    return None


def _next_para(el: etree._Element) -> Optional[etree._Element]:
    nxt = el.getnext()
    while nxt is not None:
        if nxt.tag == qn("w:p"):
            return nxt
        nxt = nxt.getnext()
    return None


def _extract_table_style(tbl: etree._Element) -> dict[str, str]:
    style: dict[str, str] = {}
    tbl_pr = tbl.find(qn("w:tblPr"))
    grid = tbl.find(qn("w:tblGrid"))
    rows = tbl_rows(tbl)
    style["tbl_pr"] = etree.tostring(tbl_pr).decode("utf-8") if tbl_pr is not None else ""
    style["tbl_grid"] = etree.tostring(grid).decode("utf-8") if grid is not None else ""
    header_row = rows[0] if rows else None
    style["header_row"] = (
        etree.tostring(header_row).decode("utf-8") if header_row is not None else "")
    data_row = None
    for tr in rows[1:]:
        data_row = tr
    if data_row is None:
        data_row = header_row
    style["data_row"] = (
        etree.tostring(data_row).decode("utf-8") if data_row is not None else "")
    return style


def _build_mapping(taggedits: list[dict], image_slots: list[dict]) -> dict[str, Any]:
    """从 taggedits 构建 mapping.slots（bj 协议）。

    - 有数据源的 tag 必须是标准字典编号；无数据源块由编译器分配
      ``miss-<slug>``（AI 按内容命名，全局唯一），其 source 固定为 null；
    - AI kind 决定槽类型：value→text、叙述句→format、结论→conclusion、表→table、图→image；
      value-like bj（text/conclusion）可复用为 format 叙述句；结构性 bj（table/image/组）kind 必须一致；
    - 图片本体与图名共用同一 tag（不生成独立 NAME 槽）；
    - 同一 table/image tag 多处引用同一 source → 槽只定义一次，occurrences 保存各位置样式。
    """
    slots: dict[str, dict[str, Any]] = {}
    for item in taggedits:
        tag = item.get("tag", "")
        kind = item.get("kind", "")
        std = _BJ.get(tag)
        is_missing = bool(MISS_NAME_RE.match(tag or ""))
        if std is None and not is_missing:
            raise CompileMappingError(
                f"tag {tag} 不在标准字典中（标准结构缺项或编号错误，见 standard_dict）",
                [f"{tag}: 非标准字典编号"])
        std_kind = std["kind"] if std is not None else kind
        # 验算结论模块（chk-*）强制 kind=conclusion：AI 不得写成 text/format 整段
        if std is not None and std_kind == "conclusion" and kind not in (None, "", "conclusion"):
            raise CompileMappingError(
                f"tag {tag} 属验算结论模块，kind 必须为 conclusion（收到 {kind}）",
                [f"{tag}: chk-* 结论标签应用 conclusion kind（AI 不得写 text/format 整段）"])
        if (std is not None and kind in ("table", "image", "format")
                and std_kind in ("table", "image", "format")):
            if kind != std_kind:
                raise CompileMappingError(
                    f"tag {tag} kind 与标准字典不一致: {kind} != {std_kind}",
                    [f"{tag}: 结构性 bj 的 kind 必须一致"])
        if tag == "lst-lc":
            allowed = {"id", "type", "type_name", "operation", "formula", "chinese"}
            refs = item.get("item_sources") or {}
            # column: 前缀已废弃（荷载组合列表直接引用 brief 字段），一律按
            # 去前缀后的字段名校验；裸键同样只允许固定字段，写错在 compile 期
            # 拦下而不是 render 时整段红字。
            invalid = sorted({
                str(ref).removeprefix("column:")
                for ref in refs.values()
                if str(ref).removeprefix("column:") not in allowed
            })
            if invalid:
                raise CompileMappingError(
                    f"tag {tag} 使用了不存在的荷载组合字段: {', '.join(invalid)}",
                    [f"{tag}: item_sources 仅可引用 id/type/type_name/operation/formula/chinese"])
        kind_eff = kind or std_kind

        slot: dict[str, Any] = {"type": kind_eff}
        # 标准 source 只来自字典；模板本地缺失 tag 始终为 null。
        slot["source"] = None if is_missing else std["source"]
        if kind_eff == "format":
            for f in ("template", "sources", "repeat_source", "item_template",
                      "item_sources"):
                if item.get(f) is not None:
                    slot[f] = item.get(f)
            if std is not None and std.get("repeat_source"):
                slot.setdefault("repeat_source", std["repeat_source"])
        elif kind_eff == "conclusion":
            for f in ("ok_text", "ng_text", "ok_template", "ng_template"):
                if item.get(f):
                    slot[f] = item.get(f)
            original = str(item.get("original_text") or "")
            span = str(item.get("replace_span") or "")
            static_text = original
            if span and original.count(span) == 1:
                static_text = original.replace(span, "", 1)
            # 模板静态前缀已经写明桥规依据时不重复注入；没有依据的模板才由
            # 标准字典补齐。公式本身始终留在 Tag 外。
            if (std is not None and std.get("basis")
                    and not re.search(r"《桥规》|JTG\s*3362|JTG\s*D60", static_text, re.I)):
                slot["basis"] = std["basis"]
            # 控制值结论：AI 可提供 verdict_source（verdict:验算表格.表名#行）用于 {control}{limit}{unit}
            if item.get("verdict_source"):
                slot["verdict_source"] = item.get("verdict_source")
        if kind_eff == "table":
            slot["caption"] = item.get("caption", "")
            slot["occurrences"] = [{
                "temp_id": item.get("temp_id"),
                "caption": item.get("caption", ""),
            }]
        if kind_eff == "image":
            slot["caption"] = item.get("caption", "")
            slot["caption_name"] = item.get("name") or item.get("caption_name")
            occurrence = {
                "temp_id": item.get("temp_id"),
                "caption": item.get("caption", ""),
                "caption_name": item.get("name") or item.get("caption_name"),
                "width_emu": None,
                "height_emu": None,
            }
            for b in image_slots:
                if b.get("caption") == item.get("caption", ""):
                    slot["width_emu"] = b.get("width_emu")
                    slot["height_emu"] = b.get("height_emu")
                    occurrence["width_emu"] = b.get("width_emu")
                    occurrence["height_emu"] = b.get("height_emu")
                    break
            else:
                slot["width_emu"] = None
                slot["height_emu"] = None
            slot["occurrences"] = [occurrence]

        # 同一 tag 多处引用同一槽位：只定义一次，不覆盖
        if tag in slots:
            prev = slots[tag]
            if prev.get("type") != slot.get("type"):
                raise CompileMappingError(
                    f"tag {tag} 多位置类型不一致（重复定义冲突）",
                    [f"{tag}: 同一 tag 出现了不同类型定义"])
            if is_missing and kind_eff in ("table", "image"):
                prev_cap = str(prev.get("caption_name") or prev.get("caption") or "")
                new_cap = str(slot.get("caption_name") or slot.get("caption") or "")
                if prev_cap and new_cap and prev_cap != new_cap:
                    # miss tag 是"某个具体缺项位置"的名字：两个不同图名/表名的
                    # 位置各是不同数据，必须分别命名 miss-<slug>，不得共用。
                    raise CompileMappingError(
                        f"tag {tag} 绑定了两个不同位置: {prev_cap!r} / {new_cap!r}",
                        [f"{tag}: 不同数据不得共用同一 miss Tag——"
                         f"请按图名/表名分别命名唯一的 miss-<slug>"])
            if kind_eff in ("table", "image"):
                prev.setdefault("occurrences", []).extend(slot.get("occurrences") or [])
            continue
        slots[tag] = slot
    return slots


def compile_from_taggedits(
    prepared_path: str,
    taggedits_path: str,
    prepared_view_path: str,
    outdir: str,
    template_id: str = "",
    brief: Optional[dict[str, Any]] = None,
    source_template_sha256: Optional[str] = None,
) -> dict[str, Any]:
    """执行 Tag 写入编译：prepared → template.docx(mapping) + mapping.json。

    Args:
        curated: ... 省略
        brief: 当前项目 project-brief（可选），仅作结构校验样例；不裁剪标准 source。
    """
    with open(taggedits_path, "r", encoding="utf-8") as f:
        taggedits = json.load(f)
    if not isinstance(taggedits, dict):
        raise CompileMappingError(
            "taggedits 必须是对象（至少含 reviewed_blocks + taggedits）")
    if "reviewed_blocks" not in taggedits:
        raise CompileMappingError(
            "缺少 reviewed_blocks：首接审阅必须显式声明每个模板位置的 "
            "static/dynamic 决定（见 onboarding-rules 审阅协议）",
            ["缺少 reviewed_blocks"])
    with open(prepared_view_path, "r", encoding="utf-8") as f:
        view = json.load(f)
    image_slots = [b for b in view.get("blocks", []) if b.get("kind") == "image_slot"]
    view_blocks = view.get("blocks", [])
    taggedits, miss_errors = _materialize_missing_tags(taggedits, view_blocks)
    if miss_errors:
        raise CompileMappingError("未决条目命名不完整", miss_errors)
    items = taggedits.get("taggedits", [])
    reviewed_blocks = taggedits.get("reviewed_blocks")
    reviewed_groups = taggedits.get("reviewed_groups")

    # 首次 AI 审阅完整性门禁（强制：缺少 reviewed_blocks 已在上方拒绝）
    review_errors = _validate_review_collection(
        items, view_blocks, reviewed_blocks, reviewed_groups or [])
    if review_errors:
        raise CompileMappingError("首接审阅不完整", review_errors)

    if not items:
        raise CompileMappingError("taggedits 为空，没有可写入的 Tag")

    pkg = DocxPackage.open(prepared_path)
    root = pkg.get_xml("word/document.xml")
    body = root.find(qn("w:body"))
    if body is None:
        raise CompileMappingError("document.xml 缺少 w:body")

    failures: list[str] = []
    mapping = _build_mapping(items, image_slots)
    groups = reviewed_groups or []
    group_tags = {g.get("tag") for g in groups}

    # 两阶段：先在原始 body 上预解析所有段落类 Tag 的定位（互不干扰），
    # 再统一执行替换——避免前面替换破坏后面 before/after 复合上下文。
    # 组（reviewed_groups）的 tag 不在 plan 中单独定位，由组替换整组处理。
    plan: list[tuple[dict, etree._Element]] = []
    for item in items:
        tag = item.get("tag", "")
        kind = item.get("kind", "")
        if not tag or not kind:
            failures.append(f"taggedit 缺少 tag/kind: {item}")
            continue
        if kind not in ("text", "format", "conclusion", "table", "image"):
            failures.append(f"taggedit {tag} kind 非法: {kind}")
            continue
        if kind in ("text", "format", "conclusion") and tag in group_tags:
            continue  # 该 Tag 由整组替换负责
        if kind in ("text", "format", "conclusion"):
            p = _locate_para(item, body, view_blocks)
            if p is None:
                failures.append(
                    f"{tag}: 定位失败（temp_id 与模板视图不一致，且文本回退无法唯一命中）: "
                    f"{item.get('original_text', '')[:50]!r}")
                continue
            plan.append((item, p))
        else:
            plan.append((item, None))  # table/image 各自即时定位

    # 先替换整组动态列表（组内全部旧段落 → 一个 【TAG】 槽）
    for g in groups:
        try:
            _apply_group_replacement(g, g.get("tag"), body, view_blocks)
        except CompileMappingError as e:
            failures.extend(e.failures)

    for item, el in plan:
        tag = item.get("tag", "")
        kind = item.get("kind", "")
        try:
            if kind in ("text", "format", "conclusion") and el is not None:
                if kind == "text":
                    span = item.get("replace_span", "")
                    if not span:
                        failures.append(f"{tag}: type=text 缺少 replace_span")
                        continue
                    if not _replace_span_in_para(el, span, f"【{tag}】"):
                        failures.append(
                            f"{tag}: replace_span {span!r} 在段内 0 或多命中")
                else:
                    # format / conclusion：有 replace_span 只替换子句，否则整段
                    span = item.get("replace_span")
                    if span:
                        if not _replace_span_in_para(el, span, f"【{tag}】"):
                            failures.append(
                                f"{tag}: replace_span {span!r} 在段内 0 或多命中")
                    else:
                        replace_paragraph_text(el, f"【{tag}】")
            elif kind == "table":
                style = _apply_table(item, tag, body)
                slot = mapping[tag]
                slot.setdefault("style_xml", style)
                for occurrence in slot.get("occurrences") or []:
                    if occurrence.get("temp_id") == item.get("temp_id"):
                        occurrence["style_xml"] = style
                        break
            elif kind == "image":
                _apply_image(item, tag, body)
        except CompileMappingError as e:
            failures.extend(e.failures)

    if failures:
        raise CompileMappingError(f"{len(failures)} 处 Tag 写入失败", failures)

    os.makedirs(outdir, exist_ok=True)
    template_out = os.path.join(outdir, "template.docx")
    _color_tags_red(root)
    pkg.mark_dirty("word/document.xml")
    pkg.write(template_out)

    written = DocxPackage.open(template_out)
    wroot = written.get_xml("word/document.xml")
    full_text = "".join(t.text or "" for t in wroot.iter(qn("w:t")))
    errors = validate_mapping(
        {"mapping_schema": 3, "template_sha256": sha256_file(prepared_path),
         "slots": mapping},
        brief=brief,
        doc_text=full_text)
    if errors:
        raise CompileMappingError("编译产物校验失败", errors)

    mapping_doc = {
        "mapping_schema": 3,
        "standard_structure_version": _BJ_VERSION,
        "standard_structure_sha256": _BJ_SHA,
        "template_id": template_id,
        # template_sha256 = 包内 template.docx（写 Tag 成品）的哈希，与
        # package.json.template_sha256 一致，供人工/工具核对；
        # prepared 中间件（写 Tag 前）的哈希单独记 prepared_template_sha256，
        # 两种"模板 SHA"不再混用同一个字段。
        "template_sha256": sha256_file(template_out),
        "prepared_template_sha256": sha256_file(prepared_path),
        "slots": mapping,
    }
    if source_template_sha256:
        mapping_doc["source_template_sha256"] = source_template_sha256
    mapping_path = os.path.join(outdir, "mapping.json")
    write_mapping(mapping_doc, mapping_path)

    return {
        "status": "compiled",
        "template": template_out,
        "mapping": mapping_path,
        "tag_count": len(mapping),
        "tags": sorted(mapping.keys()),
        "template_sha256": mapping_doc["template_sha256"],
    }


_STATIC_REASON_PREFIXES = ("标题", "规范原文", "公式定义", "通用说明")
_STATIC_FORBIDDEN_RE = re.compile(
    r"满足规范要求|不满足规范要求|"
    r"施工阶段\s*\d+|施工阶段数量|沉降组\s*\d+|不均匀沉降|"
    r"midas\s*Civil|全预应力混凝土结构|"
    r"设计程序\s*[:：]|安全等级\s*[:：]|"
    r"重要性系数\s*[:：]|收缩龄期\s*[:：]|"
    r"最大挠度设计值"
)


def _normalize_business_text(value: Any) -> str:
    """首接门禁用的精确业务文本：仅去格式字符，不做模糊推断。"""
    return re.sub(r"[\W_]+", "", str(value or "")).lower()


#: 标准字典 tag kind ↔ prepared-view 块类型的合法配对（跨类型绑定一律拒绝）。
_KIND_BLOCK_COMPAT = {
    "image": ("image_slot",),
    "table": ("table",),
    "conclusion": ("paragraph", "heading"),
}


def _standard_tag_matches_context(tag: str, item: dict, block: dict) -> bool:
    """table/image/conclusion 必须命中字典 label 或明确 alias，且与块类型一致。"""
    std = _BJ.get(tag)
    if std is None or std.get("kind") not in ("table", "image", "conclusion"):
        return True
    std_kind = std["kind"]
    if block.get("kind") not in _KIND_BLOCK_COMPAT.get(std_kind, ()):  # 跨类型绑定
        return False
    context = " ".join([
        *(str(part) for part in (block.get("chapter") or [])),
        str(block.get("caption") or ""),
        str(block.get("text") or ""),
        str(item.get("caption") or ""),
        str(item.get("caption_name") or item.get("name") or ""),
    ])
    normalized = _normalize_business_text(context)
    terms = [std.get("label"), *(std.get("aliases") or [])]
    return any(
        term and _normalize_business_text(term) in normalized
        for term in terms
    )


def _validate_review_collection(
    items: list[dict],
    view_blocks: list[dict],
    reviewed_blocks: list[dict],
    reviewed_groups: list[dict],
) -> list[str]:
    """首次 AI 审阅完整性门禁。

    - reviewed_blocks 必须覆盖 prepared-view 全部 temp_id（static/dynamic 决定）；
    - dynamic 决定的 tags 必须真实存在于 taggedits；
    - reviewed_groups：成员 temp_id 连续、decision=dynamic、tag 存在于 taggedits。

    Returns: 错误列表；空 = 通过。
    """
    errors: list[str] = []
    prepared_ids = [b["temp_id"] for b in view_blocks]
    prepared_set = set(prepared_ids)
    order = {tid: i for i, tid in enumerate(prepared_ids)}
    block_by_id = {b["temp_id"]: b for b in view_blocks}

    tag_set = {it.get("tag") for it in items if it.get("tag")}

    for item in items:
        tag = item.get("tag")
        tid = item.get("temp_id")
        if not tag or not tid or tid not in block_by_id:
            continue
        block = block_by_id[tid]
        if item.get("kind") == "conclusion":
            original = str(item.get("original_text") or block.get("text") or "")
            span = str(item.get("replace_span") or "")
            if re.search(r"桥规|公式|[≤≥]", original) and (not span or span == original):
                errors.append(
                    f"block {tid} 的结论含桥规或公式，replace_span 必须只圈定数据/"
                    "通过结论子句，公式正文必须保留为模板静态内容")
            # span 外残留模板样例数值（如 顶缘=1.270 MPa）→ 首接必须一并圈进 span，
            # 否则每个项目都会渲染出模板的旧数据。规范条文编号（第5.1.2-1条）
            # 和无量纲系数（0.85σpc）不受影响。
            kept = original
            if span and original.count(span) == 1:
                kept = original.replace(span, "", 1)
            leaked = re.findall(r"\d+(?:\.\d+)?\s*(?:MPa|kN|kPa|mm|℃|°C)", kept)
            if leaked:
                errors.append(
                    f"block {tid} 的结论 replace_span 外残留模板数值 {leaked[:4]}"
                    f"——模板样例数值必须一并圈进 replace_span；"
                    f"属于规范条文的数值应改写为不含单位的公式记号或留在条文引用内")
        if not _standard_tag_matches_context(tag, item, block):
            std = _BJ[tag]
            chapter = " / ".join(str(x) for x in (block.get("chapter") or []))
            expected_kinds = _KIND_BLOCK_COMPAT.get(std["kind"], ())
            kind_note = (
                f"（{std['kind']} 类 Tag 只能绑定 {'/'.join(expected_kinds)} 块，"
                f"此块是 {block.get('kind')}）"
                if block.get("kind") not in expected_kinds else "")
            errors.append(
                f"block {tid} 与 Tag {tag}（{std.get('label')}）业务语义不匹配{kind_note}；"
                f"模板章节/题注: {chapter or block.get('caption') or block.get('text') or '-'}。"
                "请选择精确 Tag；标准字典无对应项时使用 unresolved")

    reviewed = {}
    for rb in reviewed_blocks or []:
        tid = rb.get("temp_id")
        decision = rb.get("decision")
        if not tid:
            errors.append("reviewed_blocks 条目缺少 temp_id")
            continue
        reviewed[tid] = rb
        if tid not in prepared_set:
            errors.append(f"审阅引用了不存在的 block: {tid}")
            continue
        if decision not in ("static", "dynamic"):
            errors.append(f"block {tid} 的 decision 非法: {decision!r}（应为 static/dynamic）")
        if decision == "static":
            reason = str(rb.get("static_reason") or "").strip()
            if not any(reason.startswith(prefix) for prefix in _STATIC_REASON_PREFIXES):
                errors.append(
                    f"block {tid} 判定 static 但缺少合法 static_reason"
                    f"（标题/规范原文/公式定义/通用说明）")
            block = block_by_id[tid]
            if block.get("kind") in ("table", "image_slot"):
                errors.append(
                    f"block {tid} 是 {block.get('kind')}，项目表格/图槽不得判定 static")
            text = str(block.get("text") or block.get("caption") or "")
            if _STATIC_FORBIDDEN_RE.search(text):
                errors.append(
                    f"block {tid} 含项目参数或验算结果，不得判定 static")
        if decision == "dynamic":
            tags = rb.get("tags") or []
            if not tags:
                errors.append(
                    f"block {tid} 判定 dynamic 但未声明 tags"
                    f"（unresolved 块：tags 先写 []，并在 taggedits 中放同 temp_id、"
                    f"tag:null + unresolved:true 的条目，compile 会自动回填 "
                    f"miss-<slug>；合法 kind 仅限 text/format/conclusion/table/image）")
            for tg in tags:
                if tg not in tag_set:
                    errors.append(f"block {tid} 引用不存在的 Tag: {tg}")

    missing = prepared_set - set(reviewed)
    if missing:
        errors.append(f"未审阅 block（首接审阅必须覆盖全部模板位置）: "
                      f"{sorted(missing)[:20]}{'…' if len(missing) > 20 else ''}")

    for g in reviewed_groups or []:
        members = g.get("member_temp_ids") or []
        if g.get("decision") != "dynamic":
            errors.append(f"组 {g.get('group_id', '?')} 必须是 dynamic")
            continue
        if g.get("tag") not in tag_set:
            errors.append(f"组 {g.get('group_id', '?')} 引用不存在的 Tag: {g.get('tag')}")
        idxs = [order.get(m) for m in members]
        if any(i is None for i in idxs):
            errors.append(f"组 {g.get('group_id', '?')} 成员含不存在的 block: {members}")
            continue
        if idxs != list(range(min(idxs), max(idxs) + 1)):
            errors.append(f"组 {g.get('group_id', '?')} 成员在模板中不连续: {members}")
    return errors


def _apply_group_replacement(
    group: dict,
    tag: str,
    body: etree._Element,
    view_blocks: list[dict],
) -> None:
    """把动态组（同构列表）的全部旧段落替换成一个 {{TAG}} 槽。

    - 成员按 prepared-view 的 temp_id 顺序 → 文本序列；
    - 在 body 中按序定位（允许重复文本）；首段替换为 【TAG】，其余成员段删除；
    - 成员缺失/顺序不连续 → CompileMappingError。
    """
    members = group.get("member_temp_ids") or []
    if not members or not tag:
        raise CompileMappingError(
            f"组 {group.get('group_id', '?')} 缺成员或 tag", [f"组缺少成员/tag"])
    by_id = {b["temp_id"]: b for b in view_blocks}
    paras: list[etree._Element] = []
    for tid in members:
        b = by_id.get(tid)
        if b is None:
            raise CompileMappingError(
                f"组 {group.get('group_id', '?')} 成员不存在: {tid}",
                [f"组 {group.get('group_id', '?')} 成员不存在: {tid}"])
        para = _locate_para({"temp_id": tid}, body, view_blocks)
        if para is None:
            raise CompileMappingError(
                f"组 {group.get('group_id', '?')} 成员段落定位失败: {tid}",
                [f"组 {group.get('group_id', '?')} 成员段落定位失败: {tid}"])
        paras.append(para)
    if len(paras) != len(members):
        raise CompileMappingError(
            f"组 {group.get('group_id', '?')} 成员段落定位失败（可能不连续或被占）",
            [f"组 {group.get('group_id', '?')} 成员段落定位失败"])
    replace_paragraph_text(paras[0], f"【{tag}】")
    for p in paras[1:]:
        parent = p.getparent()
        if parent is not None:
            parent.remove(p)


def _locate_para(
    item: dict,
    body: etree._Element,
    view_blocks: Optional[list[dict]] = None,
) -> Optional[etree._Element]:
    """在 body 上定位段落类 Tag 的目标元素（text/format/conclusion）。

    text / 带 replace_span → 包含匹配；整段 format/conclusion → 精确匹配。
    支持 before_text/after_text 复合上下文。
    """
    temp_id = item.get("temp_id")
    if temp_id and view_blocks:
        target = next(
            (block for block in view_blocks if block.get("temp_id") == temp_id),
            None,
        )
        if target and target.get("kind") in ("paragraph", "heading"):
            target_text = str(target.get("text") or "").strip()
            same_before = [
                block for block in view_blocks
                if block.get("kind") in ("paragraph", "heading")
                and str(block.get("text") or "").strip() == target_text
            ]
            try:
                occurrence = same_before.index(target)
            except ValueError:
                occurrence = -1
            matches = [
                para for para in _body_level_paras(body)
                if para_text(para).strip() == target_text
            ]
            if 0 <= occurrence < len(matches):
                return matches[occurrence]

    original = item.get("original_text", "")
    if not original:
        return None
    before = item.get("before_text")
    after = item.get("after_text")
    if item.get("kind") == "text" or item.get("replace_span"):
        return _find_unique_para_by_text(body, original, "contain", before, after)
    return _find_unique_para_by_text(body, original, "exact", before, after)


def _apply_table(item: dict, tag: str, body: etree._Element) -> dict[str, str]:
    caption = item.get("caption", "")
    if not caption:
        raise CompileMappingError(f"tag {tag} type=table 缺少 caption",
                                  [f"{tag}: 缺少 caption"])
    cap_para = _find_unique_para_by_text(body, caption, "exact")
    if cap_para is None:
        raise CompileMappingError(
            f"tag {tag} 表题定位失败", [f"{tag}: caption {caption!r} 0 或多命中"])
    tbl = _next_table(cap_para)
    if tbl is None:
        raise CompileMappingError(f"tag {tag} 表题后无表格", [f"{tag}: caption 后没有 w:tbl"])
    style = _extract_table_style(tbl)
    parent = tbl.getparent()
    if parent is None:
        raise CompileMappingError(f"tag {tag} 表格无父节点", [f"{tag}: 表格无父节点"])
    parent.replace(tbl, _make_tag_paragraph(tag))
    return style


def _next_table(el: etree._Element) -> Optional[etree._Element]:
    nxt = el.getnext()
    while nxt is not None:
        if nxt.tag == qn("w:tbl"):
            return nxt
        if nxt.tag == qn("w:p"):
            return None
        nxt = nxt.getnext()
    return None


def _apply_image(item: dict, tag: str, body: etree._Element) -> None:
    caption = item.get("caption", "")
    name = item.get("name") or item.get("caption_name") or ""
    if not caption:
        raise CompileMappingError(f"tag {tag} type=image 缺少 caption",
                                  [f"{tag}: 缺少 caption"])
    if not name:
        raise CompileMappingError(f"tag {tag} type=image 缺少 caption_name",
                                  [f"{tag}: 缺少 caption_name"])
    cap_para = _find_unique_para_by_text(body, caption, "exact")
    if cap_para is None:
        raise CompileMappingError(f"tag {tag} 图题定位失败",
                                  [f"{tag}: caption {caption!r} 0 或多命中"])
    if para_text(cap_para).count(name) != 1:
        raise CompileMappingError(
            f"tag {tag} caption_name 定位失败",
            [f"{tag}: caption_name {name!r} 在图题内应唯一出现"])
    slot = _prev_para(cap_para)
    if slot is None:
        raise CompileMappingError(f"tag {tag} 图题上方无图槽段", [f"{tag}: 无图槽段"])
    replace_paragraph_text(slot, f"【{tag}】")


# （_prev_para/_next_para 定义见 _find_unique_para_by_text 上方）


# ================================================================ CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="Tag 写入 / mapping 打包")
    sub = ap.add_subparsers(dest="command")

    t = sub.add_parser("compile", help="写 Tag 生成 template.docx + mapping.json")
    t.add_argument("prepared", help="prepared-template.docx")
    t.add_argument("taggedits", help="taggedits.json（OSISAI 输出协议）")
    t.add_argument("prepared_view", help="prepared-view.json")
    t.add_argument("-o", "--outdir", required=True, help="输出目录")
    t.add_argument("--id", default="", help="模板 ID（可选）")

    b = sub.add_parser("packages", help="三件套打包")
    bs = b.add_subparsers(dest="pcmd")
    build = bs.add_parser("build", help="构建模板包")
    build.add_argument("template", help="template.docx")
    build.add_argument("--mapping", required=True, help="mapping.json")
    build.add_argument("--id", required=True, help="模板 ID")
    build.add_argument("-o", "--outdir", required=True, help="输出根目录")

    args = ap.parse_args(argv)

    if args.command == "compile":
        try:
            result = compile_from_taggedits(
                args.prepared, args.taggedits, args.prepared_view,
                args.outdir, args.id)
        except CompileMappingError as e:
            print(json.dumps({"status": "failed", "error": str(e),
                              "failures": e.failures}, ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    elif args.command == "packages" and args.pcmd == "build":
        try:
            result = build_template_package(
                args.template, args.mapping, args.outdir, args.id)
        except (PackageBuildRefused, PackageBuildFailed) as e:
            print(json.dumps({"status": "failed", "error": str(e)},
                             ensure_ascii=False, indent=2))
            return 1
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
