"""project_brief：Project Brief 组装。

把 osis_extract 解析出的 OSIS 输出（模型表/验算/图片/荷载组合/画像）合并为
统一的 Project Brief（facts/tables/images/缺失报告/bj 索引），供首接审阅
与固定渲染使用。OSIS 输出格式的解析不在本模块（见 osis_extract.py）；
本模块同时 re-export 解析器 API，外部 `from project_brief import ...` 不受影响。

用法：
    python project_brief.py --project OSIS项目目录 [--profile 画像.json] -o OUTDIR
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any, Optional

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from standard_dict import BJ as _BJ  # noqa: E402
from standard_dict import BJ_ORDER as _BJ_ORDER  # noqa: E402
from osis_extract import (  # noqa: E402,F401 （re-export：外部仍从 project_brief 取解析 API）
    ensure_osis_data,
    extract_load_cases,
    extract_load_combinations,
    extract_uniform_temperatures,
    extract_osis_data,
    extract_profile_model_stats,
    extract_material_specs,
    parse_project_profile,
    enrich_material_table,
    normalize_reaction_table,
    _derive_material_subtables,
    _format_combine_formula,
    _has_valid_osis_data,
    _load_case_code,
    _material_strengths,
    _read_command_text,
    _read_project_profile,
    _struct_basic_is_valid,
)


def _coverage_diff(project_dir: str, brief: dict[str, Any]) -> dict[str, Any]:
    """项目文件存在但 brief 未收录的覆盖差异报告。

    - 扫描项目 json/ 目录的表格 JSON，对照 brief.tables 已知 path；
    - 未收录的（如项目输出的表不在固定 _OSIS_TABLE_SPECS 内）列进 not_in_brief，
      供 OSISAI 首接判断"模板是否有表对应到这些被漏收的数据"。
    """
    json_dir = os.path.join(project_dir, "json")
    json_files: list[str] = []
    if os.path.isdir(json_dir):
        json_files = sorted(
            f for f in os.listdir(json_dir)
            if f.endswith(".json") and not f.startswith("."))
    known: set[str] = set()
    for t in brief.get("tables", {}).values():
        p = t.get("path", "") if isinstance(t, dict) else ""
        if p:
            known.add(os.path.basename(p.replace("/", os.sep)))
    return {
        "project_json_files": json_files,
        "not_in_brief": [f for f in json_files if f not in known],
    }


def _profile_identity_label(key: str) -> Optional[str]:
    """key 命中 standard_dict 画像板块 → 返回 bj.source 的字段键。

    返回的是 **source 键**（如 基础或支座体系/车道标准/收缩徐变），不是字典
    label（基础/支座体系、车道标准/活载等级、收缩徐变参数）：facts 必须写成
    `profile.基本信息.<source键>` 才能被 `field:profile.基本信息.<键>` 取到。
    历史上这里返回 label，导致画像有值、render 仍红字（键不匹配）。
    """
    for tag, b in _BJ.items():
        if not tag.startswith("profile-"):
            continue
        if key == b["label"] or key in b.get("aliases", []):
            src = b.get("source", "")
            prefix = "field:profile.基本信息."
            if src.startswith(prefix):
                return src[len(prefix):]
            return b["label"]
    return None


def _profile_support_summary(profile_facts: dict[str, Any]) -> Optional[str]:
    """把项目画像“边界体系”章节的支座行合成一个基础/支座事实。"""
    parts = []
    for full_key, value in profile_facts.items():
        section, _, key = full_key.partition(" | ")
        if "边界体系" not in section or not key or value in (None, ""):
            continue
        parts.append(f"{key}：{str(value).strip('`')}")
    return "；".join(parts) if parts else None


def _build_bj_index(
    facts: dict, tables: dict, images: dict, load_combinations: list
) -> list[dict[str, Any]]:
    """按 standard_dict 固定顺序输出 bj 索引（bjN → label/kind/source/present）。

    present：当前项目是否提供该标准项数据（field/table/image/组合）。
    """
    out = []
    for bj in _BJ_ORDER:
        b = _BJ[bj]
        src = b["source"]
        if src == "load_combinations":
            present = bool(load_combinations)
        elif src.startswith("field:"):
            present = src.removeprefix("field:") in facts
        elif src.startswith("table:"):
            ref = src.removeprefix("table:")
            if ref == "荷载组合":
                present = bool(load_combinations)
            elif ref.startswith("材料参数."):
                present = "材料参数" in tables
            else:
                present = ref in tables
        elif src.startswith("image:"):
            present = src.removeprefix("image:") in images
        elif src.startswith("verdict:"):
            ref = src.removeprefix("verdict:").split("#", 1)[0]
            key = ref if ref.startswith("验算表格.") else f"验算表格.{ref}"
            present = key in tables
        else:
            present = False
        out.append({"bj": bj, "label": b["label"], "kind": b["kind"],
                    "source": src, "present": present})
    return out

# ================================================================ Project Brief 构建

PROFILE_KEYS = {
    "bridge_type": "project.bridge_type",
    "structural_system": "project.structural_system",
    "span": "project.span",
    "pier_height": "project.pier_height",
    "deck_width": "project.deck_width",
    "design_code": "project.design_code",
    "location": "project.location",
}

OSIS_BASIC_KEYS = {
    "节点数量": "model.node_count",
    "单元数量": "model.element_count",
    "边界条件数量": "model.boundary_count",
    "施工阶段数量": "model.construction_stages",
    "版本号": "model.version",
}


def build_project_brief(
    project_dir: str,
    profile: Optional[dict[str, Any]] = None,
    auto_generate: bool = False,
) -> dict[str, Any]:
    """构建 Project Brief。

    自动检测项目目录类型：
    1. 有 Temperary/ → 直接提取 OSIS 数据
    2. 无数据 → **不自动导出**：数据完整性由 OSISAI 在渲染前负责
       （`python main.py export --project P` 显式导出一次）；brief 只如实
       报告缺失，绝不隐式触发 OSIS（避免求解/闪退副作用）
    3. 有 项目数据结构.json → 兼容模式
    4. 都失败 → 返回明确错误提示

    Args:
        project_dir: OSIS 项目目录
        profile: 项目画像（可选）
        auto_generate: 缺数据时是否调用 pyosis 生成（默认 False；
            仅显式传 True 的调用方——如 export 子命令——才允许触发）

    Returns:
        Project Brief dict
    """
    facts: dict[str, dict[str, Any]] = {}
    tables: dict[str, dict[str, Any]] = {}
    images: dict[str, dict[str, Any]] = {}
    conflicts: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []

    # 数据源选择：依据数据有效性，而非目录是否存在
    has_temperary = _has_valid_osis_data(project_dir)
    has_json_struct = os.path.isfile(os.path.join(project_dir, "项目数据结构.json"))
    struct_valid = False
    if has_json_struct:
        try:
            with open(os.path.join(project_dir, "项目数据结构.json"),
                      "r", encoding="utf-8") as f:
                struct_data = json.load(f)
            struct_valid = _struct_basic_is_valid(struct_data)
        except (OSError, json.JSONDecodeError):
            struct_valid = False

    # 画像读取（始终进行，补充业务事实）
    profile_facts, profile_tables, profile_text = _read_project_profile(project_dir)
    profile_stats = extract_profile_model_stats(profile_facts)

    # 仅在模型数据整体缺失时导出。Temperary 已有效但图片缺失时不得重复调用
    # output_result_for_calc_book()；渲染阶段直接如实标记缺图。
    if auto_generate:
        needs_export = not has_temperary and not struct_valid
        if needs_export:
            gen_result = ensure_osis_data(project_dir)
            if gen_result["status"] == "ok" or gen_result["status"] == "generated":
                has_temperary = _has_valid_osis_data(project_dir)
                # 重新验证 JSON 数据有效性（不能只看文件存在）
                has_json_struct = os.path.isfile(
                    os.path.join(project_dir, "项目数据结构.json"))
                struct_valid = False
                if has_json_struct:
                    try:
                        with open(os.path.join(project_dir, "项目数据结构.json"),
                                  "r", encoding="utf-8") as f:
                            struct_data = json.load(f)
                        struct_valid = _struct_basic_is_valid(struct_data)
                    except (OSError, json.JSONDecodeError):
                        struct_valid = False
            elif gen_result["status"] == "failed":
                return {
                    "project_dir": os.path.abspath(project_dir),
                    "built_at": datetime.now(timezone.utc).isoformat(),
                    "facts": {}, "tables": {}, "images": {},
                    "conflicts": [],
                    "missing": [{
                        "key": "project_dir",
                        "source": "system",
                        "message": gen_result["message"],
                    }],
                    "seismic_flag": None,
                }

    # 选择数据源：Temperary 有效优先，否则有效 JSON，否则画像
    data_source = None
    osis_data: dict[str, Any] = {}
    if has_temperary:
        osis_data = extract_osis_data(project_dir)
        data_source = "Temperary"
    elif struct_valid:
        pd = ProjectData(project_dir)
        osis_data = pd.data
        data_source = "项目数据结构.json"
    elif profile_stats:
        # 画像提供模型统计
        osis_data = {
            "基本信息": profile_stats,
            "自重系数": None,
            "验算表格": {},
            "图片文件": {},
        }
        data_source = "项目画像.md"
    else:
        # 画像原始文本至少作为 project_profile_text 提供给 Agent；
        # 数据补齐是 OSISAI 的前置职责（export 子命令），brief 只如实报告。
        return {
            "project_dir": os.path.abspath(project_dir),
            "built_at": datetime.now(timezone.utc).isoformat(),
            "facts": {}, "tables": {}, "images": {},
            "conflicts": [],
            "missing": [{
                "key": "project_dir",
                "source": "system",
                "message": (f"项目无有效数据（Temperary/Check/image 缺失或无效）。"
                            f"请先用 export 子命令显式导出一次："
                            f"python main.py export --project {project_dir}；"
                            f"导出失败（如 OSIS 闪退）则停止并报告，不带病渲染。"),
            }],
            "seismic_flag": None,
            "project_profile_text": profile_text,
        }

    basic = osis_data.get("基本信息") or {}
    raw_tables = osis_data.get("_tables_data", {})
    raw_check = osis_data.get("_check_table_data", {})
    image_files = osis_data.get("图片文件") or {}
    seismic = osis_data.get("抗震验算")

    # 1. 画像 facts
    if profile:
        for profile_key, brief_key in PROFILE_KEYS.items():
            val = profile.get(profile_key)
            if val is not None:
                facts[brief_key] = {
                    "value": val, "source": "project_profile", "status": "confirmed",
                }

    # 2. 基本信息 facts
    for osis_key, brief_key in OSIS_BASIC_KEYS.items():
        val = basic.get(osis_key)
        if val is not None:
            facts[brief_key] = {
                "value": val, "source": "osis_model", "status": "confirmed",
            }

    # 3. 自重系数
    gravity = osis_data.get("自重系数")
    if gravity is not None:
        facts["model.gravity_coefficient"] = {
            "value": gravity, "source": "osis_model", "status": "confirmed",
        }

    # 4. 表格
    for key in ["材料参数", "边界条件", "施工阶段", "钢束属性", "钢束坐标",
                 "梯度温度", "收缩徐变", "支座反力"]:
        if key in raw_tables:
            tbl = raw_tables[key]
            tables[key] = {
                "source": f"table:{key}",
                "header": tbl.get("header", []),
                "row_count": len(tbl.get("data", [])),
                "path": osis_data.get(key, ""),
            }
        elif osis_data.get(key):
            # 兼容模式：从 JSON 文件读取
            table_path = osis_data[key]
            if isinstance(table_path, str):
                full_path = os.path.join(project_dir, table_path.replace("/", os.sep))
                if os.path.exists(full_path):
                    try:
                        with open(full_path, "r", encoding="utf-8") as f:
                            tbl = json.load(f)
                        tables[key] = {
                            "source": f"table:{key}",
                            "header": tbl.get("header", []),
                            "row_count": len(tbl.get("data", [])),
                            "path": table_path,
                        }
                    except (json.JSONDecodeError, OSError):
                        missing.append({"key": f"table:{key}", "source": "osis_model",
                                        "message": f"表格文件读取失败: {table_path}"})

    # 5.5 材料分表派生（纯拆分：混凝土/普通钢筋/预应力钢材 → 独立 json + brief 键）
    _derive_material_subtables(project_dir, tables)

    # 5. 验算表格
    check_results = osis_data.get("验算表格") or {}
    for check_name, entry in check_results.items():
        if not isinstance(entry, dict):
            continue
        table_key = f"验算表格.{check_name}"
        if table_key in raw_check:
            tbl = raw_check[table_key]
            tables[table_key] = {
                "source": f"table:{table_key}",
                "header": tbl.get("header", []),
                "row_count": len(tbl.get("data", [])),
                "path": entry.get("path", ""),
            }
        elif entry.get("path"):
            table_path = entry["path"]
            full_path = os.path.join(project_dir, table_path.replace("/", os.sep))
            if os.path.exists(full_path):
                try:
                    with open(full_path, "r", encoding="utf-8") as f:
                        tbl = json.load(f)
                    tables[table_key] = {
                        "source": f"table:{table_key}",
                        "header": tbl.get("header", []),
                        "row_count": len(tbl.get("data", [])),
                        "path": table_path,
                    }
                except (json.JSONDecodeError, OSError):
                    pass

        # 验算摘要 facts
        for summary_key in ["最大弯矩", "最小弯矩", "最大剪力", "最小剪力",
                             "最大压应力", "最大拉应力"]:
            val = entry.get(summary_key)
            if val is not None:
                facts[f"check.{check_name}.{summary_key}"] = {
                    "value": val, "source": "osis_check", "status": "confirmed",
                }
        if entry.get("满足规范") is not None:
            facts[f"check.{check_name}.pass"] = {
                "value": "OK" if entry["满足规范"] else "NG",
                "source": "osis_check", "status": "confirmed",
            }

    # 6. 图片
    for img_key, img_path in image_files.items():
        if not isinstance(img_path, str):
            continue
        full_path = os.path.join(project_dir, img_path.replace("/", os.sep))
        images[img_key] = {
            "source": f"image:{img_key}",
            "path": img_path,
            "exists": os.path.exists(full_path),
        }

    # 7. 画像 facts（保留章节层级 + 身份字段规范键，来源 project_profile）
    for full_key, value in profile_facts.items():
        section = full_key.split(" | ")[0]
        key = full_key.split(" | ")[-1]
        # 层级键：profile.<干净章节>.<键>
        clean = re.sub(r"^\s*\d+[.、]\s*", "", section)
        clean = re.sub(r"[\s]+", "", clean)
        brief_key = f"profile.{clean}.{key}"
        facts[brief_key] = {
            "value": value,
            "source": "project_profile",
            "status": "confirmed",
        }
        # 身份字段规范键：key 命中 standard_dict 画像板块 label/别名 →
        # 生成 profile.基本信息.<label>（与 bj 字典 source 精确一致）
        label = _profile_identity_label(key)
        if label:
            facts[f"profile.基本信息.{label}"] = {
                "value": value,
                "source": "project_profile",
                "status": "confirmed",
            }
        else:
            # 保留无章节别名（兼容旧引用 profile.<key>）
            facts.setdefault(f"profile.{key}", {
                "value": value, "source": "project_profile",
                "status": "confirmed",
            })

    support_summary = _profile_support_summary(profile_facts)
    if support_summary:
        facts.setdefault("profile.基本信息.基础或支座体系", {
            "value": support_summary,
            "source": "project_profile",
            "status": "confirmed",
        })

    # 固定业务事实：设计程序统一为 OSIS；整体升降温来自求解命令。
    facts["profile.基本信息.设计程序"] = {
        "value": "OSIS", "source": "skill_default", "status": "confirmed",
    }
    temperatures = extract_uniform_temperatures(project_dir)
    if "rise" in temperatures:
        facts["profile.基本信息.整体升温"] = {
            "value": temperatures["rise"], "source": "osis_command", "status": "confirmed",
        }
    if "fall" in temperatures:
        facts["profile.基本信息.整体降温"] = {
            "value": temperatures["fall"], "source": "osis_command", "status": "confirmed",
        }
    if temperatures:
        parts = []
        if "rise" in temperatures:
            parts.append(f"整体升温 {temperatures['rise']:g}℃")
        if "fall" in temperatures:
            parts.append(f"整体降温 {temperatures['fall']:g}℃")
        facts["profile.基本信息.温度"] = {
            "value": "；".join(parts), "source": "osis_command", "status": "confirmed",
        }

    # 8. 冲突检测
    if profile:
        profile_bt = profile.get("bridge_type", "")
        if "连续" in profile_bt:
            if any("简支" in str(v) for v in basic.values()):
                conflicts.append({
                    "key": "project.bridge_type",
                    "profile_value": profile_bt,
                    "osis_value": "简支",
                    "message": "画像声明连续梁，但 OSIS 基本信息含简支特征",
                })

    # 荷载工况、荷载组合分别提取；二者不可共用同一表格数据。
    load_cases = extract_load_cases(project_dir)
    if load_cases:
        tables["荷载工况"] = {
            "source": "table:荷载工况",
            "header": ["序号", "工况名称", "描述"],
            "row_count": len(load_cases),
            "path": "",
        }
    else:
        missing.append({
            "key": "load_cases",
            "source": "osis_log",
            "message": "未找到 OSIS 求解命令日志中的 LoadCase 定义",
        })

    load_combinations = extract_load_combinations(project_dir)
    if not load_combinations:
        missing.append({
            "key": "load_combinations",
            "source": "osis_log",
            "message": "未找到 OSIS 求解命令日志（_logfile.log / Error/Command.log），"
                       "无法提取荷载组合定义",
        })

    result = {
        "project_dir": os.path.abspath(project_dir),
        "built_at": datetime.now(timezone.utc).isoformat(),
        "facts": facts,
        "tables": tables,
        "images": images,
        "conflicts": conflicts,
        "missing": missing,
        "seismic_flag": seismic,
        "data_source": data_source,
        "load_cases": load_cases,
        "load_combinations": load_combinations,
    }
    result["coverage_diff"] = _coverage_diff(project_dir, result)
    result["profile_tables"] = profile_tables
    result["bj_index"] = _build_bj_index(facts, tables, images, load_combinations)
    if profile_text:
        result["project_profile_text"] = profile_text
    return result


def generate_brief_md(brief: dict[str, Any]) -> str:
    """生成 AI 易读的 Markdown 格式 Project Brief。"""
    lines = []
    lines.append("# Project Brief（= bj1 项目输出标准结构清单）")
    lines.append("")
    lines.append(f"项目目录: {brief['project_dir']}")
    lines.append(f"构建时间: {brief['built_at']}")
    lines.append("")

    # 项目身份（画像提取：桥型/跨径/体系/梁高等）——首接时先对照这节
    identity = sorted(
        (k, e["value"]) for k, e in brief.get("facts", {}).items()
        if k.startswith("profile.") and any(
            w in k for w in ("桥型", "跨径", "体系", "梁高", "材料", "等级",
                             "车道", "全长", "宽度", "自重", "温度", "收缩")))
    if identity:
        lines.append("## 项目身份（画像，bj1 优先对照）")
        lines.append("")
        for k, v in identity:
            lines.append(f"- `{k}` = {v}")
        lines.append("")

    lines.append("## 事实")
    lines.append("")
    for key, entry in sorted(brief.get("facts", {}).items()):
        status_icon = "✅" if entry["status"] == "confirmed" else "⚠️"
        lines.append(f"- {status_icon} `{key}` = {entry['value']}  ({entry['source']})")
    lines.append("")

    lines.append("## 表格")
    lines.append("")
    for key, entry in sorted(brief.get("tables", {}).items()):
        lines.append(f"- `{key}`: {entry['row_count']}行, 表头={', '.join(entry['header'][:6])}")
    lines.append("")

    lines.append("## 图片")
    lines.append("")
    for key, entry in sorted(brief.get("images", {}).items()):
        icon = "✅" if entry["exists"] else "❌"
        lines.append(f"- {icon} `{key}`: {entry['path']}")
    lines.append("")

    combos = brief.get("load_combinations") or []
    if combos:
        lines.append("## 荷载组合")
        lines.append("")
        from collections import OrderedDict
        by_type: dict[str, list] = OrderedDict()
        for c in combos:
            by_type.setdefault(c["type_name"], []).append(c)
        for type_name, items in by_type.items():
            lines.append(f"### {type_name}")
            for c in items:
                lines.append(f"- `{c['id']}`: {c['chinese']}")
        lines.append("")

    if brief.get("conflicts"):
        lines.append("## ⚠️ 冲突")
        for c in brief["conflicts"]:
            lines.append(f"- `{c['key']}`: 画像={c['profile_value']} vs OSIS={c['osis_value']}")
            lines.append(f"  {c['message']}")
        lines.append("")

    if brief.get("missing"):
        lines.append("## ❌ 缺失")
        for m in brief["missing"]:
            lines.append(f"- `{m['key']}`: {m['message']}")
        lines.append("")

    seismic = brief.get("seismic_flag")
    if seismic is not None:
        lines.append(f"## 抗震标志")
        lines.append(f"- {'抗震' if seismic else '非抗震'}")
        lines.append("")

    return "\n".join(lines)


class ProjectData:
    """项目数据结构.json 兼容数据源（field 点路径；原 datasource.py 精简内联）。

    仅用于「有 项目数据结构.json、无 Temperary」的兼容读取，以及 render 对旧式
    `field:基本信息.节点数量` 类 source 的回退。
    """

    def __init__(self, project_dir: str):
        self.dir = project_dir
        path = os.path.join(project_dir, "项目数据结构.json")
        with open(path, "r", encoding="utf-8") as f:
            self.data = json.load(f)

    def resolve(self, data_source: str):
        kind, _, ref = data_source.partition(":")
        if kind == "field":
            return ("field", self._resolve_field(ref))
        raise ValueError(f"未知 data_source 类型: {data_source}")

    def _resolve_field(self, ref: str):
        parts = ref.split(".")
        node = self.data
        for p in parts:
            if not isinstance(node, dict) or p not in node:
                raise KeyError(ref)
            node = node[p]
        if isinstance(node, (dict, list)):
            raise KeyError(f"{ref} 不是标量")
        return node


# ---------------------------------------------------------------- CLI
def main(argv=None):
    ap = argparse.ArgumentParser(description="构建 Project Brief")
    ap.add_argument("--project", required=True,
                    help="OSIS 项目目录（含 Temperary/Check/image/）或已预处理目录")
    ap.add_argument("--profile", help="项目画像 JSON 文件（可选）")
    ap.add_argument("--ensure", action="store_true",
                    help="缺数据时显式调用 output_result_for_calc_book() 首次导出")
    ap.add_argument("-o", "--outdir", required=True, help="输出目录")
    args = ap.parse_args(argv)

    profile = None
    if args.profile:
        with open(args.profile, "r", encoding="utf-8") as f:
            profile = json.load(f)

    brief = build_project_brief(args.project, profile, auto_generate=args.ensure)

    os.makedirs(args.outdir, exist_ok=True)

    json_path = os.path.join(args.outdir, "project-brief.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(brief, f, ensure_ascii=False, indent=2)

    md_path = os.path.join(args.outdir, "project-brief.md")
    md = generate_brief_md(brief)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md)

    print(json.dumps({
        "status": "ok",
        "project_brief": json_path,
        "project_brief_md": md_path,
        "fact_count": len(brief["facts"]),
        "table_count": len(brief["tables"]),
        "image_count": len(brief["images"]),
        "conflict_count": len(brief["conflicts"]),
        "missing_count": len(brief["missing"]),
        "data_source": "Temperary/" if os.path.isdir(os.path.join(args.project, "Temperary"))
                       else "项目数据结构.json",
    }, ensure_ascii=False, indent=2))

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
