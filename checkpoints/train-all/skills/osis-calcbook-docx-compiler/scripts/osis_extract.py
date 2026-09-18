"""osis_extract：OSIS 输出解析器（只读，零业务组装）。

把 OSIS 项目目录里的各种输出解析成结构化数据，供 project_brief 组装：
- Temperary/：GBK 键值 txt（DATA_*/Coeff_*）、扁平 JSON 模型表（tXxx）；
- Check/ 与 Temperary/*.txt：验算表（表头嗅探 + 手工解析，兼容 4.x/5.00 文件名）；
- image/：两代图片命名（IMG_英文 / IMG_中文）→ 标准图名；
- 求解日志（_logfile.log / Error/Command.log / OSIS.out）：
  Combine → 荷载组合（`系数(码)` 展示串）、LoadCase → 荷载工况（英文码描述）、
  UTEMP → 整体升降温、Material → 材料规范与等级；
- py/项目画像.md → 结构化 facts + 多列记录表；
- ensure_osis_data：缺数据时显式导出一次（仅 OSISAI 经插件 export 子命令调用；
  brief/render 代码不隐式触发）。

facts/tables 合并、缺失报告等 brief 组装见 project_brief.py。
"""
from __future__ import annotations

import json
import os
import re
from typing import Any, Optional

import sys

_SCRIPTS = os.path.dirname(os.path.abspath(__file__))
if _SCRIPTS not in sys.path:
    sys.path.insert(0, _SCRIPTS)
# ================================================================ OSIS 数据提取
# 以下函数从 data.py 移植，直接读取 OSIS 输出目录

def _parse_value_file(file_path: str) -> dict[str, Any]:
    """解析 Temperary 目录下的键值对 txt 文件（GBK 编码）。"""
    result = {}
    if not os.path.exists(file_path):
        return result
    with open(file_path, "r", encoding="gbk", errors="ignore") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            parts = line.split("\t")
            if len(parts) >= 2:
                key = parts[0].strip()
                candidates = [parts[1].strip()]
                if len(parts) >= 3:
                    candidates.append(parts[2].strip())
                for val_str in candidates:
                    if not val_str:
                        continue
                    try:
                        if "." in val_str or "e" in val_str.lower():
                            value = float(val_str)
                            if value == int(value):
                                value = int(value)
                        else:
                            value = int(val_str)
                        result[key] = value
                        break
                    except (ValueError, OverflowError):
                        # OverflowError：1e400 之类的超界浮点，不能让整次渲染崩溃
                        continue
    return result


def _flat_list_to_table(data: list, col_count: int) -> dict:
    """将扁平列表转换为 {header, data} 表格格式。"""
    header = data[:col_count]
    body = data[col_count:]
    rows = []
    for i in range(0, len(body), col_count):
        rows.append(body[i:i + col_count])
    return {"header": header, "data": rows}


def _format_version(version: int) -> str:
    """数字版本号 → x.yy.zz 字符串。"""
    s = str(version).zfill(5)
    return f"{s[0]}.{s[1:3]}.{s[3:5]}"


def _read_json(path: str) -> Any:
    """读取 JSON 文件（UTF-8-sig 或 UTF-8）。"""
    for enc in ("utf-8-sig", "utf-8"):
        try:
            with open(path, "r", encoding=enc) as f:
                return json.load(f)
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
    raise ValueError(f"无法解析 JSON: {path}")


def _dir_has_files(path: str) -> bool:
    """目录存在且至少含一个条目（空目录视为"没有"）。"""
    return os.path.isdir(path) and any(os.scandir(path))


def ensure_osis_data(project_dir: str) -> dict[str, Any]:
    """确保项目目录有 OSIS 输出数据（Temperary/Check/image/）。

    如果数据已存在（Temperary 与 image 均有内容），直接返回。
    如果不存在，调用 pyosis.output_result_for_calc_book() 生成。

    Returns:
        {"status": "ok"|"generated"|"failed", "message": "..."}
    """
    temp_dir = os.path.join(project_dir, "Temperary")
    check_dir = os.path.join(project_dir, "Check")
    image_dir = os.path.join(project_dir, "image")

    # 已有数据（Temperary 与 image 均非空；空 image/ 视为"未导出"）
    if _dir_has_files(temp_dir) and _dir_has_files(image_dir):
        return {"status": "ok", "message": "项目数据已存在"}

    # 尝试调用 pyosis 生成
    try:
        from pyosis.general import output_result_for_calc_book
    except ImportError:
        return {
            "status": "failed",
            "message": "pyosis 未安装，无法自动生成项目数据。"
                       "请先用 OSIS 完成验算（生成 Temperary/Check/image/），"
                       "或手动运行 data.py 生成 项目数据结构.json",
        }

    try:
        # 切换到项目目录执行
        old_cwd = os.getcwd()
        os.chdir(project_dir)
        try:
            result = output_result_for_calc_book()
        finally:
            os.chdir(old_cwd)

        # 检查是否生成了数据
        if os.path.isdir(temp_dir) and os.path.isdir(image_dir):
            return {"status": "generated", "message": f"pyosis 已生成项目数据到 {project_dir}"}
        else:
            return {"status": "failed", "message": f"pyosis 执行完成但未生成预期目录。项目目录: {project_dir}"}

    except Exception as e:
        return {"status": "failed", "message": f"pyosis 执行失败: {e}"}


# ================================================================ 数据源有效性
#: Temperary 中表示"有效模型数据"的关键文件（任一存在即视为有数据）。
#: v5.00 布局：Post.inf / Solve.inf 是后处理完成的标志。
_OSIS_MODEL_SIGNAL_FILES = (
    "DATA_NodeNum.txt",
    "tMatChar.json",
    "tBdChar.json",
    "tCsChar.json",
    "Coeff_DeadWeight.txt",
    "Post.inf",
    "Solve.inf",
)


def _has_valid_osis_data(project_dir: str) -> bool:
    """判断是否有有效 OSIS 模型数据（不能只看目录是否存在）。

    两代形态都认：
    - Temperary/ 下有模型信号文件（4.x/5.00 运行时布局）；
    - json/ 下有模型表 JSON（5.00 计算书导出后 Temperary 可能被清空，
      json/tMatChar.json 等即提取器消费的数据源）。
    """
    temp_dir = os.path.join(project_dir, "Temperary")
    if os.path.isdir(temp_dir):
        for name in _OSIS_MODEL_SIGNAL_FILES:
            if os.path.isfile(os.path.join(temp_dir, name)):
                return True
    json_dir = os.path.join(project_dir, "json")
    if os.path.isdir(json_dir):
        for name in ("tMatChar.json", "tBdChar.json", "tCsChar.json"):
            if os.path.isfile(os.path.join(json_dir, name)):
                return True
    return False


def _struct_basic_is_valid(struct: dict[str, Any]) -> bool:
    """判断 项目数据结构.json 的基本信息是否有效（非全零）。"""
    basic = (struct or {}).get("基本信息") or {}
    total = sum(
        int(basic.get(k) or 0) for k in
        ("节点数量", "单元数量", "边界条件数量", "施工阶段数量")
    )
    return total > 0


# ================================================================ 项目画像解析
#: 画像管道表格的表头词（首列命中则视为表头行，跳过）。
_PIPE_TABLE_HEADER_WORDS = {
    "键", "项目", "名称", "属性", "参数", "字段", "指标", "内容",
    "单元范围", "截面", "编号", "阶段", "工况名", "工况", "部位", "序号",
    "类型", "类别", "描述", "构件", "位置", "荷载", "组合",
}


def parse_project_profile(profile_text: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """从项目画像 Markdown 提取 (结构化事实, 多列记录表)。

    - `- 键: 值` 列表项 → facts；
    - 2 列键值管道表 `| 键 | 值 |` → facts（跳过表头/分隔/数字范围行）；
    - **多列表格（每行一条记录，如截面分布/施工阶段）→ 结构化保留到 tables**
      （`{章节}.{首列表头}` → {header, data}），不丢弃。

    无法可靠解析的原始文本作为 project_profile_text 保留。
    """
    facts: dict[str, Any] = {}
    tables: dict[str, Any] = {}
    section = ""
    for raw_line in profile_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # 一级标题是画像对项目名称的明确声明；不从桥型/目录名推断。
        if line.startswith("# "):
            title = line[2:].strip()
            title = re.sub(r"^项目画像\s*[—–-]\s*", "", title).strip()
            if title:
                facts.setdefault("基本信息 | 项目名称", title)
            continue
        # 小节标题
        if line.startswith("##"):
            section = line[2:].strip()
            continue
        # 列表项
        if line.startswith("- ") and ":" in line:
            key, _, value = line[2:].partition(":")
            key = key.strip()
            value = value.strip()
            if key and value:
                facts[f"{section} | {key}"] = value
            continue
        # 管道表格
        if line.startswith("|"):
            cells = [c.strip() for c in line.strip("|").split("|")]
            if len(cells) < 2:
                continue
            # 多列表格 → 结构化保留（按连续行累积，见下方表头判断）
            if len(cells) > 2:
                _collect_profile_table(tables, section, cells)
                continue
            # 2 列键值：跳过表头/分隔/数字范围行
            key, value = cells[0], cells[1]
            if not key or set(key) <= {"-", ":", " "}:
                continue
            if key in _PIPE_TABLE_HEADER_WORDS:
                continue
            if key[0].isdigit() or "~" in key or "," in key:
                continue
            if key and value:
                facts[f"{section} | {key}"] = value
    return facts, tables


def _collect_profile_table(tables: dict[str, Any], section: str, cells: list[str]) -> None:
    """累积多列管道表：按 section 归属一张表；表头行建 header，其余行作数据。"""
    if not section:
        return
    # 跳过分隔行（| --- | --- |）
    if all(set(c.strip()) <= {"-", ":", " "} for c in cells):
        return
    key = section
    entry = tables.setdefault(key, {"header": [], "data": []})
    _HEADER_LIKE = ("单元范围", "截面", "编号", "阶段", "工况", "部位", "序号", "名称")
    if not entry["header"] and cells[0] in _HEADER_LIKE:
        entry["header"] = cells
    elif entry["header"]:
        entry["data"].append(cells)
    else:
        # 无表头词的首行：当作表头（多列记录首行即列名）
        entry["header"] = cells


#: 材料分表拆分规则（按材料名称分类，纯拆分不新增业务）：类名 → 匹配正则。
_MATERIAL_SPLIT_RULES = [
    ("预应力钢材", re.compile(r"钢绞线|strand|预应力", re.I)),
    ("普通钢筋", re.compile(r"HRB|HPB|RRB|钢筋", re.I)),
    ("混凝土", re.compile(r"C\s*\d|混凝土", re.I)),
]

# JTG 3362-2018 材料强度基准表放在 skill 的 schemas/mat-strength.json
# （数据与脚本分离，便于对照规范核查/更新）。加载时做形状校验：JSON 被改坏
# 时 fail-fast 报错，绝不静默产出空强度。
#: 各材料类在 _MATERIAL_STRENGTH_COLUMNS 中占用的列数（与 JSON 值序一致）。
_MATERIAL_KIND_COLUMNS = {"concrete": 4, "rebar": 3, "strand": 3}
_MATERIAL_STRENGTH_COLUMNS = (
    "fck(MPa)", "ftk(MPa)", "fcd(MPa)", "ftd(MPa)",
    "fsk(MPa)", "fsd(MPa)", "f'sd(MPa)",
    "fpk(MPa)", "fpd(MPa)", "f'pd(MPa)",
)

_MATERIAL_STRENGTH_DATA_FILE = "mat-strength.json"


def _material_strength_data_path() -> str:
    """定位基准表：依次找 <skill>/schemas/（仓库与 OSIS 内嵌 skill 布局）、
    <插件根>/schemas/（插件 authoring 布局：authoring/scripts 的上两级）、
    脚本同目录（平铺兜底）。"""
    here = os.path.dirname(os.path.abspath(__file__))
    skill_root = os.path.dirname(here)
    candidates = [
        os.path.join(skill_root, "schemas", _MATERIAL_STRENGTH_DATA_FILE),
        os.path.join(os.path.dirname(skill_root), "schemas",
                     _MATERIAL_STRENGTH_DATA_FILE),
        os.path.join(here, _MATERIAL_STRENGTH_DATA_FILE),
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return candidates[0]


def _load_material_strength_data() -> dict[str, dict[str, tuple]]:
    """读取并校验材料强度基准表（进程内缓存一次）。

    Returns:
        {kind: {等级(大写): 按列序对齐的数值 tuple}}，kind ∈ concrete/rebar/strand。
    """
    path = _material_strength_data_path()
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        raise RuntimeError(f"材料强度基准表缺失或损坏: {path} ({e})") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"材料强度基准表形状不合法（须为对象）: {path}")
    out: dict[str, dict[str, tuple]] = {}
    for kind, width in _MATERIAL_KIND_COLUMNS.items():
        table = data.get(kind)
        if not isinstance(table, dict) or not table:
            raise RuntimeError(f"材料强度基准表缺少 {kind} 表: {path}")
        grades: dict[str, tuple] = {}
        for grade, values in table.items():
            if (not isinstance(values, list) or len(values) != width
                    or not all(isinstance(v, (int, float)) for v in values)):
                raise RuntimeError(
                    f"材料强度基准表 {kind}.{grade} 值序不合法（应为 {width} 个数值）: {path}")
            grades[str(grade).upper()] = tuple(float(v) for v in values)
        out[kind] = grades
    return out


_MATERIAL_STRENGTH_DATA: dict[str, dict[str, tuple]] | None = None


def _material_strength_table() -> dict[str, dict[str, tuple]]:
    global _MATERIAL_STRENGTH_DATA
    if _MATERIAL_STRENGTH_DATA is None:
        _MATERIAL_STRENGTH_DATA = _load_material_strength_data()
    return _MATERIAL_STRENGTH_DATA


def _read_command_text(path: str) -> str:
    try:
        try:
            with open(path, "r", encoding="utf-8", errors="strict") as f:
                return f.read()
        except UnicodeDecodeError:
            with open(path, "r", encoding="gbk", errors="ignore") as f:
                return f.read()
    except OSError:
        return ""


def extract_material_specs(project_dir: str) -> dict[str, dict[str, str]]:
    """从 OSIS Material 命令提取材料编号、类型、规范和等级。"""
    specs: dict[str, dict[str, str]] = {}
    for rel in ("_logfile.log", os.path.join("Error", "Command.log"), "OSIS.out"):
        text = _read_command_text(os.path.join(project_dir, rel))
        for raw in text.splitlines():
            parts = [part.strip() for part in raw.strip().rstrip(";").split(",")]
            if len(parts) < 6 or parts[0] != "Material":
                continue
            material_id = parts[1]
            if material_id and material_id not in specs:
                specs[material_id] = {
                    "name": parts[2], "type": parts[3],
                    "standard": parts[4], "grade": parts[5],
                }
        if specs:
            break
    return specs


#: OSIS Material 命令的 type 字段 → JSON 基准表的材料类键。
_MATERIAL_KIND_BY_OSIS_TYPE = {"CONC": "concrete", "REBAR": "rebar",
                               "PRESTRESSED": "strand"}
#: 各材料类值在 _MATERIAL_STRENGTH_COLUMNS 中的起始列。
_MATERIAL_KIND_COLUMN_START = {"concrete": 0, "rebar": 4, "strand": 7}


def _material_strengths(spec: dict[str, str]) -> dict[str, Any]:
    standard = re.sub(r"[^A-Z0-9]", "", spec.get("standard", "").upper())
    if standard != "JTG33622018":
        return {}
    kind = _MATERIAL_KIND_BY_OSIS_TYPE.get(spec.get("type", "").upper())
    if kind is None:
        return {}
    grade = re.sub(r"[\s_-]", "", spec.get("grade", "").upper())
    values = _material_strength_table()[kind].get(grade)
    if values is None:
        return {}
    start = _MATERIAL_KIND_COLUMN_START[kind]
    width = _MATERIAL_KIND_COLUMNS[kind]
    return dict(zip(_MATERIAL_STRENGTH_COLUMNS[start:start + width], values))


def enrich_material_table(
    table: dict[str, Any], specs: dict[str, dict[str, str]],
) -> dict[str, Any]:
    """在 OSIS 六列材料表后补规范强度列；不改变已有列和值。"""
    header = list(table.get("header", []))
    rows = [list(row) for row in table.get("data", [])]
    if not header:
        return {"header": header, "data": rows}
    material_id_idx = next(
        (i for i, name in enumerate(header) if "材料编号" in str(name)), 0)
    for column in _MATERIAL_STRENGTH_COLUMNS:
        if column not in header:
            header.append(column)
    for row in rows:
        row.extend([""] * (len(header) - len(row)))
        material_id = str(row[material_id_idx]).strip() if material_id_idx < len(row) else ""
        for column, value in _material_strengths(specs.get(material_id, {})).items():
            row[header.index(column)] = value
    return {"header": header, "data": rows}


def normalize_reaction_table(table: dict[str, Any]) -> dict[str, Any]:
    """把 OSIS 支反力的单位首行折叠进 5 列表头。"""
    header = list(table.get("header", []))
    rows = [list(row) for row in table.get("data", [])]
    if len(header) == 5 and rows and len(rows[0]) >= 5:
        units = [str(value).strip() for value in rows[0][:5]]
        if units[2].lower() == "(kn)" and "max(kn)" in units[3].lower():
            header = ["边界编号", "节点", "恒载(kN)",
                      "标准组合Max(kN)", "标准组合Min(kN)"]
            rows = rows[1:]
    return {"header": header, "data": rows}


def _derive_material_subtables(project_dir: str, tables: dict[str, Any]) -> None:
    """从混合材料表（table:材料参数）按材料名称派生三张分表（纯拆分）。

    - 混凝土（C50/混凝土…）、普通钢筋（HRB/HPB…）、预应力钢材（钢绞线/Strand…）；
    - 每类写独立 `json/材料参数.<类>.json` 并注册 `tables['材料参数.<类>']`；
    - 某类无行 → 不生成该分表（模板绑它时 Render 红字，如实缺失）；
    - 未匹配任何类的行只留在总表，不强行归类。
    """
    base = tables.get("材料参数")
    if not isinstance(base, dict) or not base.get("path"):
        return
    full = os.path.join(project_dir, base["path"].replace("/", os.sep))
    if not os.path.isfile(full):
        return
    try:
        tbl = json.load(open(full, "r", encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    header = list(tbl.get("header", []))
    rows = list(tbl.get("data", []))
    # 材料名称列（归一化匹配"材料名称"，退化为第 2 列）
    name_idx = next((i for i, h in enumerate(header)
                     if "材料名称" in str(h)), 1 if len(header) > 1 else None)
    if name_idx is None:
        return

    groups: dict[str, list] = {"混凝土": [], "普通钢筋": [], "预应力钢材": []}
    for row in rows:
        name = str(row[name_idx]) if name_idx < len(row) else ""
        for cls, pattern in _MATERIAL_SPLIT_RULES:
            if pattern.search(name):
                groups[cls].append(row)
                break

    json_dir = os.path.join(project_dir, "json")
    os.makedirs(json_dir, exist_ok=True)
    for cls, cls_rows in groups.items():
        if not cls_rows:
            continue
        fn = f"材料参数.{cls}.json"
        with open(os.path.join(json_dir, fn), "w", encoding="utf-8") as f:
            json.dump({"header": header, "data": cls_rows}, f, ensure_ascii=False)
        key = f"材料参数.{cls}"
        tables[key] = {
            "source": f"table:{key}",
            "header": header,
            "row_count": len(cls_rows),
            "path": f"json/{fn}",
        }

def _read_project_profile(project_dir: str) -> tuple[dict[str, Any], dict[str, Any], str]:
    """读取 py/项目画像.md，返回 (结构化 facts, 多列记录表, 原始文本)。

    编码优先 UTF-8（含 BOM），失败回退 GBK；都失败按空处理，不阻断渲染。
    """
    profile_path = os.path.join(project_dir, "py", "项目画像.md")
    if not os.path.isfile(profile_path):
        return {}, {}, ""
    text = ""
    for kwargs in ({"encoding": "utf-8"}, {"encoding": "gbk", "errors": "replace"}):
        try:
            with open(profile_path, "r", **kwargs) as f:
                text = f.read()
            break
        except (OSError, UnicodeDecodeError, LookupError):
            continue
    facts, tables = parse_project_profile(text)
    return facts, tables, text


def extract_profile_model_stats(
    facts: dict[str, Any],
) -> dict[str, int]:
    """从画像 facts 提取节点/单元/边界/施工阶段数量。

    画像用"节点:55 个"格式，值含数字。提取失败返回空 dict。
    键匹配必须精确（"单元" 不能匹配 "单元组"）。
    """
    import re
    result: dict[str, int] = {}
    for full_key, value in facts.items():
        nums = re.findall(r"\d+", str(value))
        if not nums:
            continue
        num = int(nums[0])
        key = full_key.split(" | ")[-1]
        # 精确匹配（排除复合键）
        if key == "节点" and num > 0:
            result["节点数量"] = num
        elif key == "单元" and num > 0:
            result["单元数量"] = num
        elif "边界" in key and num > 0:
            result["边界条件数量"] = num
        elif key == "阶段数" and num > 0:
            result["施工阶段数量"] = num
        elif key == "施工" and "阶段" in str(value) and num > 0:
            result["施工阶段数量"] = num
    return result


# ================================================================ 荷载组合提取
#: OSIS 求解命令日志中的荷载组合定义命令。
_COMBINE_RE = re.compile(
    r"^Combine,(?P<id>[^,]+),(?P<kind>LC|Env),Concrete,"
    r"(?P<type>Basic|Standard|Frequent|Quasipermanent|Concreted1|Concreted2|ConcretePre1|ConcretePre2),"
    r"(?P<operation>ADD|OR),"
    r"(?:[^:]*:)?(?P<formula>.*)$"
)
_LOAD_CASE_RE = re.compile(
    r"^LoadCase,(?P<name>[^,]+),(?P<description>[^,]+),(?P<factor>[^,]+)$"
)
#: 组合公式中的 OSIS 荷载符号码 → 模板英文码（与 midas 模板"荷载工况"表
#: 描述列/组合列表一致：徐变二次→CS、钢束二次→TS、汽车→M、温度梯度→TPG）。
_COMBINE_SYMBOL_CODE = {
    "csD": "DL", "csPS": "PS", "csSH": "SS", "csCR": "CS",
    "STL": "SM", "L": "M", "T": "T", "TG": "TPG",
}
#: 工况名称关键词 → 模板英文码（荷载工况表"描述"列 = 对工况名称的英文描述）。
#: 按序首个命中生效；无命中回退 OSIS 日志给出的类型码。
_LOAD_CASE_NAME_CODE = (
    ("徐变", "CS"), ("收缩", "SS"),
    ("钢束二次", "TS"), ("钢束一次", "TP"), ("预应力", "PS"),
    ("汽车", "M"), ("活载", "M"),
    ("整体降温", "T"), ("整体升温", "T1"), ("整体温度", "T"),
    ("梯度降温", "TPG"), ("梯度升温", "TPG1"), ("温度梯度", "TPG"),
    ("支座沉降", "SM"), ("沉降", "SM"),
    ("二期", "DL"), ("铺装", "DL"), ("护栏", "DL"),
    ("恒载", "DL"), ("恒荷载", "DL"), ("自重", "DL"),
)
_COMBINE_TYPE_NAME = {
    "Basic": "基本组合",
    "Standard": "标准组合",
    "Frequent": "频遇组合",
    "Quasipermanent": "准永久组合",
    "Concreted1": "混凝土挠度组合",
    "Concreted2": "混凝土挠度组合",
    "ConcretePre1": "混凝土预拱度组合",
    "ConcretePre2": "混凝土预拱度组合",
}


def _load_case_code(name: str, fallback: str = "") -> str:
    """工况名称 → 模板英文码（如 徐变二次→CS）；无命中回退 OSIS 类型码。"""
    for key, code in _LOAD_CASE_NAME_CODE:
        if key in name:
            return code
    return fallback


def _format_combine_formula(formula: str, combo_names: Optional[dict] = None) -> str:
    """把组合公式格式化为模板同款 `系数(码)` 串：``0.500(SM)+1.200(DL)``。

    - 荷载符号 csD/csPS/csSH/csCR/STL/L/T/TG → DL/PS/SS/CS/SM/M/T/TPG；
    - 数字引用优先按被引组合名显示（如 1.000(基本组合3)），未知编号显示 工况N；
    - 冲击分母 /(1+) → /(1+μ)；系数统一三位小数；csPS[2] 的阶段后缀去除；
    - 不再翻译成中文：模板组合列表使用英文码（用户约定的展示格式）。
    """
    combo_names = combo_names or {}
    result = formula.replace("/(1+)", "/(1+μ)")
    # 符号码 → 模板码（先长后短；\b 断言防止 T 命中 TPG / L 命中 DL 等）
    for sym in sorted(_COMBINE_SYMBOL_CODE, key=len, reverse=True):
        result = re.sub(rf"(?<![A-Za-z]){re.escape(sym)}(?![A-Za-z])",
                        _COMBINE_SYMBOL_CODE[sym], result)

    def _term(m):
        coeff = f"{float(m.group(1)):.3f}"
        ref = m.group(2)
        if ref.startswith("("):
            # 系数*(表达式)：括号内已是模板码，整组保留
            return f"{coeff}{ref}"
        ref = re.sub(r"\[\d+\]", "", ref)
        if ref.isdigit():
            label = combo_names.get(ref) or f"工况{ref}"
        else:
            label = ref
        return f"{coeff}({label})"

    result = re.sub(
        r"(\d+(?:\.\d+)?)\*([A-Za-z][A-Za-z0-9]*(?:\[\d+\])?|\([^()]*\)|\d+)",
        _term, result)
    return result


_UNIFORM_TEMP_RE = re.compile(
    r"^Load,UTEMP,[^,]+,[^,]+,[^,]+,(?P<value>[+-]?\d+(?:\.\d+)?)\s*;?$",
    re.I,
)


def extract_uniform_temperatures(project_dir: str) -> dict[str, float]:
    """从 UTEMP 命令提取整体升温正值和整体降温负值。"""
    values: set[float] = set()
    for rel in ("_logfile.log", os.path.join("Error", "Command.log"), "OSIS.out"):
        text = _read_command_text(os.path.join(project_dir, rel))
        for raw in text.splitlines():
            match = _UNIFORM_TEMP_RE.match(raw.strip())
            if match:
                values.add(float(match.group("value")))
        if values:
            break
    result: dict[str, float] = {}
    positive = [value for value in values if value > 0]
    negative = [value for value in values if value < 0]
    if positive:
        result["rise"] = max(positive)
    if negative:
        result["fall"] = min(negative)
    return result


def extract_load_combinations(project_dir: str) -> list[dict[str, str]]:
    """从 OSIS 求解命令日志提取荷载组合定义。

    解析 Combine 命令，返回结构化列表：
    [{"id", "type", "type_name", "operation", "formula", "chinese"}]
    `chinese` 为展示串（沿用旧字段名，模板 item_sources 引用它），格式与
    midas 模板一致：`1.000(基本组合3)+1.400(M)`——系数(英文码)，不再中文翻译。

    日志不存在或无组合定义时返回空列表（调用方标记 missing，不编造）。
    """
    for rel in ("_logfile.log", os.path.join("Error", "Command.log")):
        log_path = os.path.join(project_dir, rel)
        if not os.path.isfile(log_path):
            continue
        text = _read_command_text(log_path)

        combos = []
        for line in text.splitlines():
            m = _COMBINE_RE.match(line.strip())
            if not m:
                continue
            formula = m.group("formula").strip()
            if not formula or re.fullmatch(r"0(?:\.0+)?", formula):
                continue
            combos.append({
                "id": m.group("id"),
                "type": m.group("type"),
                "type_name": _COMBINE_TYPE_NAME.get(m.group("type"), m.group("type")),
                "operation": m.group("operation"),
                "formula": formula,
                "chinese": "",
            })
        if combos:
            # 公式里的数字引用是别的组合的 id；先收齐 id→显示名再统一格式化
            combo_names = {c["id"]: f"{c['type_name']}{c['id']}" for c in combos}
            for c in combos:
                c["chinese"] = _format_combine_formula(c["formula"], combo_names)
            return combos
    return []


def extract_load_cases(project_dir: str) -> list[dict[str, Any]]:
    """从 OSIS 求解命令日志提取唯一荷载工况，保持首次出现顺序。"""
    for rel in ("_logfile.log", os.path.join("Error", "Command.log")):
        log_path = os.path.join(project_dir, rel)
        if not os.path.isfile(log_path):
            continue
        text = _read_command_text(log_path)

        cases: list[dict[str, Any]] = []
        seen: set[tuple[str, str]] = set()
        for line in text.splitlines():
            match = _LOAD_CASE_RE.match(line.strip())
            if not match:
                continue
            name = match.group("name").strip()
            description = match.group("description").strip()
            key = (name, description)
            if not name or key in seen:
                continue
            seen.add(key)
            # 描述列 = 对工况名称的英文码（参考 midas 模板：徐变二次→CS、
            # 钢束二次→TS）；名称无命中时回退 OSIS 日志自带类型码。
            cases.append({
                "id": len(cases) + 1,
                "name": name,
                "description": _load_case_code(name, description),
            })
        if cases:
            return cases
    return []


#: OSIS 5.00 验算 txt 文件名 → 标准验算名。旧版（≤4.x）文件名即标准名，不需归一；
#: 5.00 起文件名带材料/荷载包络/组合前后缀，按此表归一到字典使用的标准名。
#: 未命中表的文件保留原名（coverage_diff 会列出，供字典追加时核对）。
_CHECK_NAME_ALIASES_500: dict[str, str] = {
    "混凝土_正截面抗弯验算_基本组合包络": "正截面抗弯承载能力验算",
    "混凝土_斜截面抗剪验算_基本组合包络": "斜截面抗剪承载能力验算",
    "混凝土_正截面抗拉压验算_基本组合包络": "正截面抗拉压验算",
    "混凝土_PC使用阶段正截面压应力验算_标准组合包络": "正截面压应力验算",
    "混凝土_PC使用阶段斜截面主压应力验算_标准组合包络": "斜截面主压应力验算",
    "混凝土_PC正截面短期抗裂验算_频遇组合包络": "正截面频遇组合抗裂验算",
    "混凝土_PC正截面长期抗裂验算_准永久组合包络": "正截面准永久组合抗裂验算",
    "混凝土_PC腹板斜截面抗裂验算_频遇组合包络": "斜截面频遇组合抗裂验算",
    "施工阶段荷载包络_PC施工阶段正截面压应力验算_MinMax": "施工阶段压应力验算",
    "施工阶段荷载包络_PC施工阶段正截面拉应力验算_MinMax": "施工阶段拉应力验算",
    "混凝土_裂缝宽度验算_频遇组合包络": "裂缝宽度验算",
}

#: 验算 txt 表头嗅探的最大行数。
_CHECK_HEADER_SNIFF_MAX = 16

#: Temperary JSON → {表键, 列数}（固定 8 张模型表）。
_OSIS_TABLE_SPECS = [
    ("tMatChar.json", 6, "材料参数"),
    ("tBdChar.json", 9, "边界条件"),
    ("tCsChar.json", 4, "施工阶段"),
    ("tPTChar.json", 6, "梯度温度"),
    ("tTDPChar.json", 11, "钢束属性"),
    ("tTdChar.json", 7, "钢束坐标"),
    ("tCreepChar.json", 6, "收缩徐变"),
    ("strReaction.json", 5, "支座反力"),
]

#: 验算结果固定排序（字典顺序；新验算项按名追加在后）。
_CHECK_ORDER = [
    "斜截面主压应力验算", "斜截面抗剪承载能力验算",
    "斜截面频遇组合抗裂验算", "施工阶段压应力验算",
    "施工阶段拉应力验算", "正截面准永久组合抗裂验算",
    "正截面压应力验算", "正截面抗弯承载能力验算",
    "正截面频遇组合抗裂验算", "正截面抗拉压验算", "裂缝宽度验算",
]

#: image/ 目录文件名 → 标准图名。
_IMG_FILE_NAMES = {
    "IMG_Structure.jpg": "计算模型图",
    "IMG_Tendon.jpg": "预应力钢束布置图",
    "IMG_MomentCapacityMax.jpg": "UxMax下正截面抗弯承载能力包络图",
    "IMG_MomentCapacityMin.jpg": "UxMin下正截面抗弯承载能力包络图",
    "IMG_ShearCapacityMax.jpg": "UzMax下斜截面抗剪承载能力包络图",
    "IMG_ShearCapacityMin.jpg": "UzMin下斜截面抗剪承载能力包络图",
}

#: LcMoment_<工况> 文件后缀 → 中文工况名。
_LC_MOMENT_NAMES = {
    "DeadLoad": "恒载", "VehicleLoad": "汽车荷载",
    "CharacteristicComb": "标准组合", "FrequentComb": "频遇组合",
    "QuasiPermanentComb": "准永久组合", "FundamentalComb": "基本组合",
    "CreepSecondary": "徐变二次", "ShrinkageSecondary": "收缩二次",
    "TendonPrimary": "钢束一次", "TendonSecondary": "钢束二次",
    "UniformNegative": "整体降温", "UniformPositive": "整体升温",
    "GradientNegative": "梯度降温", "GradientPositive": "梯度升温",
}

#: 中文验算名图文件 → 标准图名（OSIS 5.00 中文命名）。
_CHECK_IMG_NAMES = {
    "正截面频遇组合抗裂验算": "正截面在作用频遇组合下抗裂验算结果",
    "正截面准永久组合抗裂验算": "正截面在作用准永久组合下抗裂验算结果",
    "斜截面频遇组合抗裂验算": "斜截面在作用频遇组合下抗裂验算结果",
    "正截面压应力验算": "正截面混凝土压应力验算结果",
    "斜截面主压应力验算": "斜截面混凝土主压应力验算结果",
    "施工阶段压应力验算": "施工阶段混凝土压应力验算结果",
    "施工阶段拉应力验算": "施工阶段混凝土拉应力验算结果",
}

#: brief.images 的固定排序。
_IMG_ORDER = [
    "计算模型图", "预应力钢束布置图",
    "恒载内力图My", "钢束一次内力图My", "钢束二次内力图My", "收缩二次内力图My", "徐变二次内力图My",
    "整体降温内力图My", "整体升温内力图My", "梯度降温内力图My", "梯度升温内力图My",
    "汽车荷载内力图My", "基本组合内力图My", "标准组合内力图My",
    "频遇组合内力图My", "准永久组合内力图My",
    "UxMin下正截面抗弯承载能力包络图", "UxMax下正截面抗弯承载能力包络图",
    "UzMin下斜截面抗剪承载能力包络图", "UzMax下斜截面抗剪承载能力包络图",
    "正截面在作用频遇组合下抗裂验算结果", "正截面在作用准永久组合下抗裂验算结果",
    "斜截面在作用频遇组合下抗裂验算结果",
    "正截面混凝土压应力验算结果", "斜截面混凝土主压应力验算结果",
    "施工阶段混凝土压应力验算结果", "施工阶段混凝土拉应力验算结果",
]


def _normalize_check_name(file_name: str) -> str:
    return _CHECK_NAME_ALIASES_500.get(file_name, file_name)


def _sniff_check_header(path: str) -> Optional[int]:
    """找到验算 txt 的表头行号：前若干行中出现含"结果"列的行；找不到返回 None。

    旧版 Check/*.txt 表头在第 3 行（header=2）；5.00 的 Temperary/*.txt 前几行
    是"验算数据：…/空行/验算名/空行"，表头在第 5 行。
    """
    try:
        with open(path, "r", encoding="gbk", errors="replace") as f:
            for i, line in enumerate(f):
                if i >= _CHECK_HEADER_SNIFF_MAX:
                    break
                if "结果" in line.split():
                    return i
    except OSError:
        return None
    return None


def _parse_check_table(path: str, header_idx: int) -> tuple[list[str], list[list]]:
    """按表头行号手工解析验算表（不依赖 pandas 的空行计数行为）。

    数值 token 转 int/float，其余保留字符串；空行跳过。
    """
    with open(path, "r", encoding="gbk", errors="replace") as f:
        lines = f.read().splitlines()
    columns = lines[header_idx].split()
    data: list[list] = []
    for line in lines[header_idx + 1:]:
        if not line.strip():
            continue
        row: list = []
        for tok in line.split():
            try:
                row.append(int(tok))
            except ValueError:
                try:
                    row.append(float(tok))
                except ValueError:
                    row.append(tok)
        data.append(row)
    return columns, data


def extract_osis_data(project_dir: str) -> dict[str, Any]:
    """从 OSIS 输出目录直接提取项目数据。

    替代 data.py 的 build_project_data()，不需要预处理步骤。
    直接读取 Temperary/、Check/、image/ 目录。

    Returns:
        与 项目数据结构.json 格式一致的 dict
    """
    temp_dir = os.path.join(project_dir, "Temperary")
    check_dir = os.path.join(project_dir, "Check")
    image_dir = os.path.join(project_dir, "image")
    json_dir = os.path.join(project_dir, "json")

    # 1. 基本信息
    node_info = _parse_value_file(os.path.join(temp_dir, "DATA_NodeNum.txt"))
    elem_info = _parse_value_file(os.path.join(temp_dir, "DATA_ElemNum.txt"))
    bc_info = _parse_value_file(os.path.join(temp_dir, "DATA_BCNum.txt"))

    # 施工阶段数
    cs_stage_count = 0
    cs_char_path = os.path.join(temp_dir, "tCsChar.json")
    if os.path.exists(cs_char_path):
        try:
            cs_data = _read_json(cs_char_path)
            # 扁平列表按 4 列折叠出阶段数；其他 JSON 形状（dict 等）不猜，按 0 处理
            if isinstance(cs_data, list):
                cs_cols = 4
                cs_stage_count = (len(cs_data) - cs_cols) // cs_cols
        except (ValueError, OSError):
            pass

    # 版本号
    version_str = "0.00.00"
    version_path = os.path.join(temp_dir, "VersionNO.json")
    if os.path.exists(version_path):
        try:
            ver_data = _read_json(version_path)
            version_str = _format_version(int(ver_data[0]))
        except (ValueError, OSError, IndexError):
            pass

    basic_info = {
        "节点数量": int(node_info.get("DATA_NodeNum", 0)),
        "单元数量": int(elem_info.get("DATA_ElemNum", 0)),
        "边界条件数量": int(bc_info.get("DATA_BCNum", 0)),
        "施工阶段数量": cs_stage_count,
        "版本号": version_str,
    }

    # 2. 自重系数
    dw_info = _parse_value_file(os.path.join(temp_dir, "Coeff_DeadWeight.txt"))
    dead_weight_coeff = float(dw_info.get("Coeff_DeadWeight", 0.0))

    # 3. 表格数据（Temperary → 结构化表格）
    tables_data: dict[str, dict] = {}
    table_paths: dict[str, str] = {}
    material_specs = extract_material_specs(project_dir)

    for src_name, col_count, key in _OSIS_TABLE_SPECS:
        src_path = os.path.join(temp_dir, src_name)
        json_path = os.path.join(json_dir, src_name)
        if not os.path.exists(src_path) and not os.path.exists(json_path):
            continue
        try:
            if os.path.exists(src_path):
                raw = _read_json(src_path)
                tbl = _flat_list_to_table(raw, col_count)
            else:
                with open(json_path, "r", encoding="utf-8") as f:
                    tbl = json.load(f)
                # 兼容旧缓存：裁剪到 OSIS 官方列宽（历史运行可能附加过
                # enrich 派生列，如材料表 fck/ftk…；裁剪无损 OSIS 原始数据）
                if isinstance(tbl, dict) and isinstance(tbl.get("header"), list):
                    tbl = {
                        "header": tbl["header"][:col_count],
                        "data": [row[:col_count] if isinstance(row, list) else row
                                 for row in tbl.get("data", [])],
                    }
            # 材料表保持 OSIS 官方 6 列（enrich_material_table 强度列附加为
            # 备用能力，20260902 起不再默认调用——16 列过宽且类型互斥空格多）
            if key == "支座反力":
                tbl = normalize_reaction_table(tbl)
            # 写入 json/ 目录（供 datasource.py 兼容读取）
            dest_path = os.path.join(json_dir, src_name)
            os.makedirs(json_dir, exist_ok=True)
            with open(dest_path, "w", encoding="utf-8") as f:
                json.dump(tbl, f, ensure_ascii=False, indent=2)
            rel_path = os.path.relpath(dest_path, project_dir).replace("\\", "/")
            table_paths[key] = rel_path
            tables_data[key] = tbl
        except (ValueError, OSError, TypeError, KeyError, IndexError):
            # 单个 Temperary JSON 形状异常只丢该表，不abort整次渲染
            pass

    # 4. 验算表格（Check/ 与 Temperary/ 双目录，兼容 OSIS 5.00 起验算 txt 移入 Temperary）
    check_results: dict[str, dict] = {}
    check_table_data: dict[str, dict] = {}

    txt_files: list[str] = []
    for d in (check_dir, temp_dir):
        if os.path.isdir(d):
            txt_files.extend(
                os.path.join(d, f) for f in sorted(os.listdir(d))
                if f.lower().endswith(".txt"))

    for file_path in txt_files:
        file_name = _normalize_check_name(
            os.path.splitext(os.path.basename(file_path))[0])
        if file_name in check_results:
            continue
        header_idx = _sniff_check_header(file_path)
        if header_idx is None:
            continue  # 非验算表（如旧版 DATA_*.txt）；Post.inf/Solve.inf 非 .txt

        columns, data_rows = _parse_check_table(file_path, header_idx)
        if "结果" not in columns:
            continue

        # 写入 json/
        dest_path = os.path.join(json_dir, f"{file_name}.json")
        os.makedirs(json_dir, exist_ok=True)
        with open(dest_path, "w", encoding="utf-8") as f:
            json.dump({"header": columns, "data": data_rows}, f,
                      ensure_ascii=False, indent=2)
        rel_path = os.path.relpath(dest_path, project_dir).replace("\\", "/")

        entry: dict[str, Any] = {"path": rel_path}
        res_idx = columns.index("结果")
        values = [row[res_idx] for row in data_rows if res_idx < len(row)]
        has_ng = "NG" in values
        entry["满足规范"] = not has_ng
        entry["总项数"] = len(values)
        entry["NG项数"] = sum(1 for v in values if v == "NG")

        col_idx = {c: i for i, c in enumerate(columns)}

        def _minmax(name: str, scale: float = 1.0):
            i = col_idx.get(name)
            if i is None:
                return None, None
            vals = [r[i] for r in data_rows
                    if i < len(r) and isinstance(r[i], (int, float))]
            if not vals:
                return None, None
            return min(vals) * scale, max(vals) * scale

        # 验算摘要（仅当对应数值列存在；5.00 新表可能没有 γMd/γVd/SigMax 列族）
        if ("抗弯" in file_name or "抗拉压" in file_name):
            lo, hi = _minmax("γMd", 1 / 1000.0)
            if lo is not None:
                entry["最小弯矩"], entry["最大弯矩"] = lo, hi
        if "抗剪" in file_name:
            lo, hi = _minmax("γVd", 1 / 1000.0)
            if lo is not None:
                entry["最小剪力"], entry["最大剪力"] = lo, hi
        if "抗裂" in file_name:
            lo, hi = _minmax("SigMax", 1 / 1000000.0)
            if lo is not None:
                entry["最不利应力"] = hi
        if "压应力" in file_name:
            lo, hi = _minmax("SigMax", 1 / 1000000.0)
            if lo is not None:
                entry["最大压应力"] = lo
        if "拉应力" in file_name:
            lo, hi = _minmax("SigMax", 1 / 1000000.0)
            if lo is not None:
                entry["最大拉应力"] = hi

        check_results[file_name] = entry
        check_table_data[f"验算表格.{file_name}"] = tbl = {
            "header": columns, "data": data_rows,
        }

    # 已知验算按固定顺序在前，其余（字典未收录的新验算项）按名追加
    ordered: dict[str, dict] = {k: check_results[k] for k in _CHECK_ORDER
                                if k in check_results}
    for k in sorted(set(check_results) - set(ordered)):
        ordered[k] = check_results[k]
    check_results = ordered

    # 5. 图片文件（文件名 → 标准图名的映射表见模块顶部 _IMG_FILE_NAMES 等）
    image_files: dict[str, str] = {}
    if os.path.isdir(image_dir):
        for f in os.listdir(image_dir):
            if not f.lower().endswith((".jpg", ".jpeg", ".png", ".bmp")):
                continue
            if f in _IMG_FILE_NAMES:
                key = _IMG_FILE_NAMES[f]
            elif f.startswith("IMG_") and f.lower().endswith(".jpg"):
                inner = f[4:-4]
                if inner.startswith("LcMoment_"):
                    suffix = inner[9:]
                    key = f"{_LC_MOMENT_NAMES.get(suffix, suffix)}内力图My"
                elif inner in _CHECK_IMG_NAMES:
                    key = _CHECK_IMG_NAMES[inner]
                else:
                    key = inner
            else:
                key = f
            image_files[key] = "image/" + f

    # 按固定顺序排列
    image_files = {k: image_files[k] for k in _IMG_ORDER if k in image_files}

    # 6. 组装
    result = {
        "基本信息": basic_info,
        "自重系数": dead_weight_coeff,
        **table_paths,
        "验算表格": check_results,
        "图片文件": image_files,
    }

    # 7. 写入 项目数据结构.json 缓存（供 datasource.py / validate_output.py 使用）
    # 守卫：本次空跑（Temperary 被清理、txt 缺失 → 基本信息/验算/图片解析为空）
    # **不得覆盖**上一次的好缓存——只保留旧值或合并，绝不把真实数据清成零。
    cache_path = os.path.join(project_dir, "项目数据结构.json")
    try:
        old = None
        if os.path.exists(cache_path):
            try:
                with open(cache_path, "r", encoding="utf-8") as f:
                    old = json.load(f)
            except (OSError, json.JSONDecodeError, ValueError):
                old = None

        def _basic_total(info):
            if not isinstance(info, dict):
                return 0
            return sum(int(info.get(k) or 0) for k in
                       ("节点数量", "单元数量", "边界条件数量", "施工阶段数量"))

        old_basic = (old or {}).get("基本信息")
        if _basic_total(basic_info) == 0 and _basic_total(old_basic) > 0:
            result["基本信息"] = old_basic          # 本次没读到 → 沿用旧值
        old_gravity = (old or {}).get("自重系数")
        if not dead_weight_coeff and isinstance(old_gravity, (int, float)) and old_gravity:
            result["自重系数"] = old_gravity
        if not check_results and isinstance((old or {}).get("验算表格"), dict) and old["验算表格"]:
            result["验算表格"] = old["验算表格"]      # Check txt 缺失时沿用旧验算
        if not image_files and isinstance((old or {}).get("图片文件"), dict) and old["图片文件"]:
            result["图片文件"] = old["图片文件"]

        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
    except OSError:
        pass  # 写入失败不影响返回

    # 附加原始数据（供 brief 直接使用，不经过 datasource.py 再读一遍）
    result["_tables_data"] = tables_data
    result["_check_table_data"] = check_table_data

    return result

