"""render_checks：验算结论解析域（verdict: 数据源 → OK/NG + 控制值/限值/单位）。

从 render.py 拆出的纯解析：
- 验算表列族识别（结果/控制值/限值/单位/安全系数）；
- 整表 OK/NG 汇总，控制行确定性选取（安全系数最小优先，其次利用率最大）；
- γMd/γVd/SigMax 列族 → kN·m / kN / MPa 固定单位换算。
结论句式拼装（ok/ng_template、桥规依据前缀）与 brief facts 回退在
render._resolve_conclusion。
"""
from __future__ import annotations

import json
import os
import sys
from typing import Any, Optional

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)

from compile_mapping import _normalize_col  # noqa: E402

# ================================================================ conclusion（结论句）
#: 验算结论行关键列名（verdict 解析，沿用旧 datasource 约定）。
_VERDICT_COLUMNS = ("结果", "结论", "验算结果", "result", "verdict")
_CONTROL_COLUMNS = (
    "控制值", "效应值", "需求值", "设计值",
    "γMd", "Md", "γVd", "Vd", "SigMax", "rd",
)
_LIMIT_COLUMNS = (
    "限值", "允许值", "承载力", "能力值",
    "Mu", "Ru", "Vu", "SigAP", "SigALW",
)
_SAFETY_COLUMNS = ("安全系数", "safety", "factor")
_UNIT_COLUMNS = ("单位", "unit")
_ROW_KEY_COLUMNS = ("位置", "部位", "单元", "构件", "编号", "location", "id")
_VERDICT_NORMALIZE = {"ok": "OK", "ng": "NG", "pass": "OK", "fail": "NG",
                      "满足": "OK", "不满足": "NG"}


def _norm_verdict(value) -> str:
    """把验算结果列归一化为 OK/NG；无法识别返回原字符串。"""
    text = str(value).strip()
    return _VERDICT_NORMALIZE.get(text.casefold(), text)


def _as_float(value) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_check_values(
    control_name: str, limit_name: str, control, limit, explicit_unit,
) -> tuple[Any, Any, str]:
    """将 OSIS 验算原始值转成计算书常用单位。

    只处理当前标准输出的三个列族，不做通用单位推断。
    """
    c_val = _as_float(control)
    l_val = _as_float(limit)
    if c_val is None or l_val is None:
        return control, limit, str(explicit_unit or "")

    names = f"{control_name}|{limit_name}"
    if any(name in names for name in ("γMd", "Md", "Mu")):
        return abs(c_val) / 1000.0, abs(l_val) / 1000.0, "kN·m"
    if any(name in names for name in ("γVd", "Vd", "Vu")):
        return abs(c_val) / 1000.0, abs(l_val) / 1000.0, "kN"
    if "Sig" in names:
        if l_val < 0:
            c_val, l_val = abs(c_val), abs(l_val)
        return c_val / 1_000_000.0, l_val / 1_000_000.0, "MPa"
    return c_val, l_val, str(explicit_unit or "")


def _resolve_verdict(brief: dict, project_dir: str, ref: str) -> Optional[dict]:
    """解析 verdict:<验算表名>#<行标识>，返回 {control, limit, unit, verdict}。

    验算表数据来自 brief.tables['验算表格.<表名>'] 的 json 文件。
    多行表先汇总整表 OK/NG，再确定性选取控制行。
    """
    table_name, _, row_key = ref.partition("#")
    lookup = table_name if table_name.startswith("验算表格.") else f"验算表格.{table_name}"
    entry = brief.get("tables", {}).get(lookup)
    if not isinstance(entry, dict):
        return None
    path = entry.get("path")
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
    header = list(obj.get("header", []))
    rows = list(obj.get("data", []))

    def find_col(names):
        for i, h in enumerate(header):
            if _normalize_col(h) in {_normalize_col(n) for n in names}:
                return i
        return None

    r_idx = find_col(_VERDICT_COLUMNS)
    c_idx = find_col(_CONTROL_COLUMNS)
    l_idx = find_col(_LIMIT_COLUMNS)
    u_idx = find_col(_UNIT_COLUMNS)
    s_idx = find_col(_SAFETY_COLUMNS)
    if r_idx is None or c_idx is None or l_idx is None:
        return None  # 缺关键列 → 视为无可靠 verdict

    valid_rows = [
        row for row in rows
        if isinstance(row, list) and max(r_idx, c_idx, l_idx) < len(row)
    ]
    if not valid_rows:
        return None
    # 注意：不剔除 safety=0 的 NG 行——那是 OSIS 自己判定的真实不满足
    # （如端部无承载力），结论必须如实反映；只在控制行数值引用时避开
    # 哨兵值（0 未算 / ≥9999 不适用，见下方 with_safety 过滤）。

    if row_key:
        # 行键列：包含匹配更鲁棒（"单元编号"命中"单元"，"构件名称"命中"构件"）
        k_idx = None
        for i, h in enumerate(header):
            hn = _normalize_col(h)
            if any(_normalize_col(n) in hn for n in _ROW_KEY_COLUMNS):
                k_idx = i
                break
        row = None
        for r in valid_rows:
            if k_idx is not None and k_idx < len(r) and str(r[k_idx]).strip() == row_key:
                row = r
                break
        if row is None and len(valid_rows) == 1:
            row = valid_rows[0]
        overall_verdict = _norm_verdict(row[r_idx]) if row is not None else ""
    else:
        ng_rows = [r for r in valid_rows if _norm_verdict(r[r_idx]) == "NG"]
        ok_rows = [r for r in valid_rows if _norm_verdict(r[r_idx]) == "OK"]
        if not ng_rows and not ok_rows:
            return None
        overall_verdict = "NG" if ng_rows else "OK"
        pool = ng_rows or ok_rows

        with_safety = []
        if s_idx is not None:
            for candidate in pool:
                if s_idx >= len(candidate):
                    continue
                safety = _as_float(candidate[s_idx])
                if safety is not None and 0 < safety < 9999:
                    with_safety.append((safety, candidate))
        if with_safety:
            row = min(with_safety, key=lambda item: item[0])[1]
        else:
            def utilization(candidate):
                actual = _as_float(candidate[c_idx])
                allowed = _as_float(candidate[l_idx])
                if actual is None or allowed is None:
                    return float("-inf")
                if allowed < 0:
                    return abs(actual) / abs(allowed) if allowed else abs(actual)
                return actual / allowed if allowed else actual

            # 控制行退化：真实安全系数行（0<s<9999）优先被引用；
            # 仅剩哨兵行时按利用率最大如实引用（不掩盖 OSIS 的 NG 判定）。
            real_pool = []
            if s_idx is not None:
                for candidate in pool:
                    s = _as_float(candidate[s_idx]) if s_idx < len(candidate) else None
                    if s is not None and 0 < s < 9999:
                        real_pool.append(candidate)
            row = max(real_pool or pool, key=utilization)
    if row is None or max(r_idx, c_idx, l_idx) >= len(row):
        return None

    explicit_unit = row[u_idx] if u_idx is not None and u_idx < len(row) else ""
    control, limit, unit = _normalize_check_values(
        str(header[c_idx]), str(header[l_idx]), row[c_idx], row[l_idx], explicit_unit,
    )
    return {
        "control": control,
        "limit": limit,
        "unit": unit,
        "verdict": overall_verdict,
    }


