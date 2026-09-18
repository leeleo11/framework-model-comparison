"""draft_taggedits：首接审阅草稿生成 + 提交前预检（固定脚本，零 AI 业务判断）。

背景：首接要求 prepared-view 的**每个块**都有 static/dynamic 决定（大模板 385+ 块），
由 AI 徒手写整份 taggedits.json 已被实测证明又慢又易错（三种自造格式、协议细节
kind/replace_span/tags:[] 反复出错）。本脚本把机械部分 100% 固定，并把 AI 的剩余
工作压缩成"选择题"：

- `draft`：读 prepared-view.json，生成合法草稿 draft.json——
    * heading            → static"标题"；
    * table / image_slot → dynamic + 未决 table/image 条目（caption/caption_name 预填）；
    * 段落命中项目参数信号 → dynamic + 条目（original_text 预填整句）；
    * 其余段落           → static"通用说明"。
  在此之上做三级自动决策，AI 只复核：
    1) **唯一候选自动预选**：用与门禁同款的 label/alias 归一匹配扫描 standard_dict，
       恰好命中一个 tag → 预填（图题→img、表题→tbl、段落→text/conclusion；
       图/表题注段跳过建议，保持 static，避免与图/表槽双重绑定）；
    2) **多候选列清单**：`_draft.candidates[tid]` 交给 AI 二选一，不瞎猜；
    3) **text 类 span 预窄**：`标签：值` 结构把 replace_span 预窄到值；
       conclusion 保持整句由 AI 收窄（保留规范条文的判断无法机械化）。
    4) **同构列表自动建组**：视图顺序相邻、数字掩码后同构的未决 text 段落
       合并为一个 reviewed_group + 一条 format 未决条目（渲染只出一次红字）。
  荷载组合 lst-lc/lst-stage 需要 item_template 句式设计，不参与自动预选（按
  onboarding-rules 第 5 节手工配置）。
- `check`：在内存里跑 materialize + 审阅门禁 + mapping 构建（不写任何产物），
  错误精确到条目，供提交 compile 前快速自检。

未编辑的草稿即可通过审阅门禁的结构校验，但**未命名的未决条目**（tag 为空或
tag:null）会在 compile 的 miss 命名强制检查处被拒——AI 编辑至少要给每个未决
位置命名唯一 `miss-<slug>`（或改选标准 tag），check 子命令可提前验证。

用法：
    python draft_taggedits.py draft --prepared-view prepared-view.json -o OUTDIR
    python draft_taggedits.py check --taggedits taggedits.json --prepared-view prepared-view.json
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from typing import Any

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from compile_mapping import (  # noqa: E402
    CompileMappingError,
    _BJ,
    _build_mapping,
    _materialize_missing_tags,
    _normalize_business_text,
    _STATIC_FORBIDDEN_RE,
    _validate_review_collection,
)

#: 段落项目参数信号（草稿据此判定 dynamic 候选）。包含门禁的 _STATIC_FORBIDDEN_RE，
#: 并补充模型统计/荷载等常见事实词——保证门禁禁止 static 的文本在草稿里必为 dynamic。
_EXTRA_PARAM_RE = re.compile(
    r"节点数量|单元数量|边界条件数量|施工阶段数量|自重系数|收缩龄期"
    r"|桥梁等级|结构基频|冲击系数|温度梯度|整体升温|整体降温|沉降组"
    r"|控制值|限值|预拱度|配筋率|钢绞线|荷载组合"
)
_DYNAMIC_HINT_RE = re.compile(
    f"(?:{_STATIC_FORBIDDEN_RE.pattern}|{_EXTRA_PARAM_RE.pattern})"
)

#: 图/表题注段（"图1 …""图表1 …""表格1 …""表1.5-1 …"）：保持 static，其动态部分
#: （图名/表数据）由图槽/表槽条目负责，段落本身不做 tag 建议，避免双重绑定。
_CAPTIONISH_RE = re.compile(r"^\s*(?:图表|表格|图|表)\s*[\d.\-－]")
#: 目录行（"2.4荷载工况及荷载组合5"——节号开头、行尾页码数字、无句末标点）。
_TOC_LINE_RE = re.compile(r"^\s*\d+(?:\.\d+)+\s*\S.*\d\s*$")

#: 图题 → 图名草稿：剥掉开头的"图x.x / 图表N"编号（SEQ 域文本），AI 可再收窄。
_CAPTION_NUM_RE = re.compile(r"^\s*图\s*表?\s*[\d.\-－]*\s*")
_LOAD_COMB_HEADING_RE = re.compile(r"^\s*荷载组合列表\s*[:：]?\s*$")
_LOAD_COMB_LINE_RE = re.compile(
    r"^\s*[^:：\s]{1,20}\d+\s*[:：].*\d+(?:\.\d+)?\s*\([^()]+\)"
)

#: `标签：值` 结构的值片段（span 预窄目标）。
_KV_RE = re.compile(r"([^：:]{2,16}[：:])\s*([^；;。，,]{1,40})")


def _draft_image_name(caption: str) -> str:
    return _CAPTION_NUM_RE.sub("", caption or "").strip()


def _block_context(block: dict[str, Any]) -> str:
    return " ".join([
        *(str(part) for part in (block.get("chapter") or [])),
        str(block.get("caption") or ""),
        str(block.get("text") or ""),
    ])


_KIND_ANCHORS = {
    "table": {"table"},       # 表格块只接受 table 类 tag
    "image_slot": {"image"},  # 图槽块只接受 image 类 tag
    "paragraph": {"text", "conclusion"},
}


def _candidate_tags(block: dict[str, Any]) -> list[str]:
    """按门禁同款的归一匹配列出该块可能的标准 tag（保持 kind 锚点约束）。"""
    normalized = _normalize_business_text(_block_context(block))
    allowed_kinds = _KIND_ANCHORS.get(block.get("kind"), set())
    hits = []
    for tag, b in _BJ.items():
        if tag.startswith("miss-") or b.get("kind") not in allowed_kinds:
            continue
        terms = [b.get("label"), *(b.get("aliases") or [])]
        if any(t and _normalize_business_text(t) in normalized for t in terms):
            hits.append(tag)
    return hits


def _narrow_span(text: str, tag: str) -> str:
    """`标签：值` 结构把 span 预窄到值片段；无法安全收窄时返回整句。"""
    m = _KV_RE.search(text)
    if m:
        label_norm = _normalize_business_text(m.group(1))
        terms = [_BJ[tag].get("label"), *(_BJ[tag].get("aliases") or [])]
        if any(t and _normalize_business_text(t) in label_norm for t in terms):
            span = m.group(2)
            if text.count(span) == 1:
                return span
    return text


def _mask_text(text: str) -> str:
    """数字掩码后的归一文本：识别同构列表行（第沉降组1组…/第沉降组2组…）。"""
    return re.sub(r"\d+", "#", _normalize_business_text(text))


def build_draft_taggedits(view: dict[str, Any]) -> dict[str, Any]:
    """从 prepared-view 生成合法的 taggedits 草稿（未编辑即可通过 compile 校验）。"""
    blocks = view.get("blocks", [])
    reviewed: list[dict[str, Any]] = []
    items: list[dict[str, Any]] = []
    needs_decision: list[str] = []
    auto_suggested: dict[str, str] = {}
    candidates: dict[str, list[str]] = {}
    group_pool: list[tuple[str, str]] = []  # (temp_id, 掩码文本) —— 未决 text 段

    # 封面标题和荷载组合是结构明确的两类位置，先机械识别，避免交给 AI 猜。
    title_tid = next((
        b.get("temp_id") for i, b in enumerate(blocks[:20])
        if str(b.get("text") or "").strip() == "计算书"
    ), None)
    # 封面样例项目名行：紧邻"计算书"上一行的非空短段落（midas 模板常把
    # 样例项目名单独放一行）。它必须替换为本项目名，否则每次渲染都会
    # 带着模板旧项目名，看起来像"在原标题后又加了一个标题"。
    title_name_tid = None
    if title_tid is not None:
        idx = next(i for i, b in enumerate(blocks) if b.get("temp_id") == title_tid)
        for j in range(idx - 1, -1, -1):
            b = blocks[j]
            if b.get("kind") not in (None, "paragraph"):
                break
            text = str(b.get("text") or "").strip()
            if not text:
                continue
            if 0 < len(text) <= 40 and "计算书" not in text:
                title_name_tid = b.get("temp_id")
            break
    loadcomb_tids: list[str] = []
    for i, block in enumerate(blocks):
        if not _LOAD_COMB_HEADING_RE.match(str(block.get("text") or "")):
            continue
        for following in blocks[i + 1:]:
            text = str(following.get("text") or "")
            if following.get("kind") != "paragraph" or not _LOAD_COMB_LINE_RE.match(text):
                break
            loadcomb_tids.append(following.get("temp_id"))
        if loadcomb_tids:
            break
    loadcomb_set = set(loadcomb_tids)

    for b in blocks:
        tid = b.get("temp_id", "")
        kind = b.get("kind")
        text = str(b.get("text") or "")
        if tid == title_name_tid:
            # 封面标题名行：整行替换为**桥型名称**（用户约定：标题不使用项目
            # 名字，用桥型名称——项目名因工程而异且常与目录名同名）
            reviewed.append({"temp_id": tid, "decision": "dynamic",
                             "tags": ["pro-btype"]})
            items.append({
                "temp_id": tid, "tag": "pro-btype", "kind": "text",
                "original_text": text, "replace_span": text,
            })
            auto_suggested[tid] = "pro-btype"
            continue
        if tid == title_tid:
            if title_name_tid is not None:
                # 桥型名已单独成槽 → "计算书"本身是固定标题，保持静态
                reviewed.append({"temp_id": tid, "decision": "static",
                                 "static_reason": "标题"})
            else:
                # 封面只有一行"计算书" → 整行替换为 "{桥型}计算书"
                reviewed.append({"temp_id": tid, "decision": "dynamic",
                                 "tags": ["pro-btype"]})
                items.append({
                    "temp_id": tid, "tag": "pro-btype", "kind": "format",
                    "original_text": text, "template": "{bridge_type}计算书",
                    "sources": {"bridge_type": "field:profile.基本信息.桥型"},
                })
            auto_suggested[tid] = "pro-btype"
            continue
        if tid in loadcomb_set:
            reviewed.append({"temp_id": tid, "decision": "dynamic",
                             "tags": ["lst-lc"]})
            auto_suggested[tid] = "lst-lc"
            continue
        if kind == "heading":
            reviewed.append({"temp_id": tid, "decision": "static",
                             "static_reason": "标题"})
            continue

        if kind == "table":
            reviewed.append({"temp_id": tid, "decision": "dynamic", "tags": []})
            entry: dict[str, Any] = {"temp_id": tid, "tag": None, "unresolved": True,
                                     "kind": "table", "caption": b.get("caption", "")}
            cands = _candidate_tags(b)
            if len(cands) == 1:
                entry.update({"tag": cands[0], "unresolved": False})
                del entry["unresolved"]
                reviewed[-1]["tags"] = [cands[0]]
                auto_suggested[tid] = cands[0]
            elif len(cands) > 1:
                candidates[tid] = cands
            items.append(entry)
            needs_decision.append(tid)
            continue

        if kind == "image_slot":
            reviewed.append({"temp_id": tid, "decision": "dynamic", "tags": []})
            caption = str(b.get("caption") or "")
            entry = {"temp_id": tid, "tag": None, "unresolved": True,
                     "kind": "image", "caption": caption,
                     "caption_name": _draft_image_name(caption)}
            cands = _candidate_tags(b)
            if len(cands) == 1:
                entry.update({"tag": cands[0], "unresolved": False})
                del entry["unresolved"]
                reviewed[-1]["tags"] = [cands[0]]
                auto_suggested[tid] = cands[0]
            elif len(cands) > 1:
                candidates[tid] = cands
            items.append(entry)
            needs_decision.append(tid)
            continue

        # paragraph
        stripped = text.rstrip()
        is_toc = bool(_TOC_LINE_RE.match(text))
        is_short_title = (0 < len(text) <= 8 and not re.search(r"\d", text)
                          and not stripped.endswith(("；", ";", "，", ",", "。")))
        is_captionish = bool(_CAPTIONISH_RE.match(text))
        if (not (text and _DYNAMIC_HINT_RE.search(text))
                or is_captionish or is_toc or is_short_title):
            # 静态默认：非信号段落、图/表题注段（动态部分由槽条目负责）、
            # 目录行、无数字短标题——AI 复核后可改
            reviewed.append({"temp_id": tid, "decision": "static",
                             "static_reason": "通用说明"})
            continue
        reviewed.append({"temp_id": tid, "decision": "dynamic", "tags": []})
        needs_decision.append(tid)

        cands = [t for t in _candidate_tags(b) if not t.startswith("list-")]
        # 结论段：存在 conclusion 候选时压制 profile 等文本类噪音候选
        concl = [t for t in cands if _BJ[t].get("kind") == "conclusion"]
        if concl:
            cands = concl
        if len(cands) == 1:
            tag = cands[0]
            bj_kind = _BJ[tag].get("kind")
            item_kind = "conclusion" if bj_kind == "conclusion" else "text"
            items.append({"temp_id": tid, "tag": tag, "kind": item_kind,
                          "original_text": text,
                          "replace_span": (_narrow_span(text, tag)
                                           if item_kind == "text" else text)})
            reviewed[-1]["tags"] = [tag]
            auto_suggested[tid] = tag
        elif len(cands) > 1:
            candidates[tid] = cands
            items.append({"temp_id": tid, "tag": None, "unresolved": True,
                          "kind": "text", "original_text": text,
                          "replace_span": text})
        else:
            items.append({"temp_id": tid, "tag": None, "unresolved": True,
                          "kind": "text", "original_text": text,
                          "replace_span": text})
            group_pool.append((tid, _mask_text(text)))

    # 同构列表自动建组：视图顺序相邻、掩码同构、≥2 条的未决 text 段
    groups: list[dict[str, Any]] = []
    if loadcomb_tids:
        group_id = "G-LOADCOMB"
        groups.append({"group_id": group_id,
                       "member_temp_ids": loadcomb_tids,
                       "decision": "dynamic", "tag": "lst-lc"})
        items.append({
            "group_id": group_id, "tag": "lst-lc", "kind": "format",
            "repeat_source": "load_combinations",
            "item_template": "{type_name}{id}：{chinese}；",
            "item_sources": {"id": "id", "type_name": "type_name",
                             "chinese": "chinese"},
        })
    grouped_tids: set[str] = set()
    run: list[str] = []
    run_mask = None
    tid_order = [b.get("temp_id") for b in blocks]
    pool = dict(group_pool)

    def _flush():
        nonlocal run, run_mask
        if len(run) >= 2:
            gid = f"G-DRAFT-{len(groups) + 1:02d}"
            groups.append({"group_id": gid, "member_temp_ids": list(run),
                           "decision": "dynamic", "tag": None})
            items.append({"group_id": gid, "tag": None, "unresolved": True,
                          "kind": "format"})
            grouped_tids.update(run)
        run, run_mask = [], None

    prev_tid = None
    for tid in tid_order:
        if tid in pool:
            mask = pool[tid]
            adjacent = (prev_tid in (None, "", *grouped_tids)
                        or (prev_tid in pool) or _order_adjacent(blocks, prev_tid, tid))
            if run and (mask == run_mask and _order_adjacent(blocks, run[-1], tid)):
                run.append(tid)
            else:
                _flush()
                run, run_mask = [tid], mask
            prev_tid = tid
        else:
            _flush()
            prev_tid = tid
    _flush()
    if grouped_tids:
        items[:] = [it for it in items if it.get("temp_id") not in grouped_tids]

    return {
        "reviewed_blocks": reviewed,
        "reviewed_groups": groups,
        "taggedits": items,
        "_draft": {
            "readme": "工作项：① 处理 _candidates 多候选块；② 复核 _auto_suggested；"
                      "③ 未决条目改精确 tag 或命名唯一 miss-<slug>；"
                      "④ 误判 dynamic 改 static；⑤ 复核封面标题与组合组。"
                      "规则详见 SKILL.md；改完用 check 子命令自检。",
            "needs_decision": needs_decision,
            "auto_suggested": auto_suggested,
            "candidates": candidates,
            "auto_groups": [g["group_id"] for g in groups],
        },
    }


def _order_adjacent(blocks: list[dict[str, Any]], tid_a: str, tid_b: str) -> bool:
    """tid_a 与 tid_b 是否为视图中相邻的块。"""
    for i in range(len(blocks) - 1):
        if blocks[i].get("temp_id") == tid_a:
            return blocks[i + 1].get("temp_id") == tid_b
    return False


def check_taggedits(taggedits: dict[str, Any], view: dict[str, Any]) -> dict[str, Any]:
    """提交前预检：materialize + 门禁 + mapping 构建，全部在内存，不写产物。"""
    work, miss_errors = _materialize_missing_tags(
        json.loads(json.dumps(taggedits, ensure_ascii=False)), view.get("blocks", []))
    items = work.get("taggedits", [])
    errors = list(miss_errors)
    if not errors:
        errors = _validate_review_collection(items, view.get("blocks", []),
                                            work.get("reviewed_blocks") or [],
                                            work.get("reviewed_groups") or [])
    slots = 0
    if not errors:
        try:
            slots = len(_build_mapping(items, [b for b in view.get("blocks", [])
                                               if b.get("kind") == "image_slot"]))
        except CompileMappingError as e:
            errors = [str(e), *e.failures]
    return {"status": "ok" if not errors else "invalid",
            "errors": errors, "slot_count": slots}


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="首接审阅草稿生成 + 提交前预检")
    sub = ap.add_subparsers(dest="command")

    d = sub.add_parser("draft", help="从 prepared-view 生成 taggedits 草稿")
    d.add_argument("--prepared-view", required=True)
    d.add_argument("-o", "--outdir", required=True, help="输出目录")

    c = sub.add_parser("check", help="内存预检（不写任何产物）")
    c.add_argument("--taggedits", required=True)
    c.add_argument("--prepared-view", required=True)

    args = ap.parse_args(argv)
    if args.command == "draft":
        with open(args.prepared_view, "r", encoding="utf-8") as f:
            view = json.load(f)
        draft = build_draft_taggedits(view)
        os.makedirs(args.outdir, exist_ok=True)
        out = os.path.join(args.outdir, "draft.json")
        with open(out, "w", encoding="utf-8") as f:
            json.dump(draft, f, ensure_ascii=False, indent=2)
        print(json.dumps({
            "status": "drafted", "taggedits_draft": out,
            "blocks": len(draft["reviewed_blocks"]),
            "taggedits": len(draft["taggedits"]),
            "needs_decision": len(draft["_draft"]["needs_decision"]),
            "auto_suggested": len(draft["_draft"]["auto_suggested"]),
            "ambiguous": len(draft["_draft"]["candidates"]),
            "auto_groups": len(draft["_draft"]["auto_groups"]),
        }, ensure_ascii=False, indent=2))
        return 0
    if args.command == "check":
        with open(args.taggedits, "r", encoding="utf-8") as f:
            tg = json.load(f)
        with open(args.prepared_view, "r", encoding="utf-8") as f:
            view = json.load(f)
        result = check_taggedits(tg, view)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result["status"] == "ok" else 1
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
