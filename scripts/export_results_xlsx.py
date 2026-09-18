"""Export canonical OSIS comparison run artifacts to a formatted workbook."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table, TableStyleInfo

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from common.paths import resolve_run_root


ARCHITECTURES = ("T1", "T2", "T3", "T4", "T5", "T6")
DATASET_FORMS = ("full", "gen", "edit")
TASK_FORM_TO_DATASET_FORM = {"whole": "full", "module": "gen", "modify": "edit"}
FORMAL_SPLITS = frozenset({"train", "test"})
BRIDGE_TYPES = (
    "cantilever_box",
    "conventional_box",
    "hollow_slab",
    "precast_small_box",
    "precast_t_girder",
    "rigid_frame",
)
RUN_COLUMNS = (
    ("task_id", "任务ID"),
    ("split", "数据集"),
    ("dataset_form", "任务形式"),
    ("task_form", "内部形式"),
    ("task_index", "任务序号"),
    ("module", "目标模块"),
    ("target", "目标文件"),
    ("record_scope", "统计范围"),
    ("aggregation_eligible", "纳入统计"),
    ("excluded_reason", "排除原因"),
    ("architecture_id", "架构"),
    ("seed", "Seed"),
    ("bridge_type", "桥型"),
    ("difficulty", "难度"),
    ("framework_version", "框架版本"),
    ("model_id", "模型"),
    ("base_url", "模型接口"),
    ("temperature", "温度"),
    ("max_tokens", "最大Token"),
    ("model_timeout_s", "单次模型时限(s)"),
    ("max_steps", "最大步数"),
    ("run_status", "运行状态"),
    ("complete_success", "完整成功"),
    ("quality_score", "综合质量分"),
    ("model_score", "CLI模型分"),
    ("model_score_status", "CLI评分状态"),
    # Runtime extraction is a parallel result.  These columns are intentionally
    # adjacent to the source score so a row can be audited without confusing
    # the two scoring paths.
    ("runtime_score", "运行态模型分"),
    ("runtime_score_5", "运行态模型分(5分)"),
    ("runtime_score_status", "运行态评分状态"),
    ("runtime_measurements_status", "运行态快照状态"),
    ("runtime_missing_params", "运行态缺失参数"),
    ("runtime_score_path", "运行态评分文件"),
    ("reference_score", "参考相对总分"),
    ("model_conformance", "参考模型符合分"),
    ("reference_score_status", "参考评分状态"),
    ("compile_passed", "代码编译通过"),
    ("model_created", "PyOSIS建模成功"),
    ("validation_passed", "模型状态验证通过"),
    ("solver_converged", "求解收敛"),
    ("generation_status", "生成状态"),
    ("generation_elapsed_s", "生成耗时(s)"),
    ("model_calls", "模型调用数"),
    ("tool_calls", "工具调用数"),
    ("invalid_tool_calls", "无效工具调用数"),
    ("framework_steps", "框架步数/事件数"),
    ("stop_reason", "停止原因"),
    ("infrastructure_failure", "基础设施失败"),
    ("excluded_from_aggregate", "排除统计"),
    ("elapsed_s", "总耗时(s)"),
    ("total_timeout_s", "总时限(s)"),
    ("failure_code", "失败码"),
    ("failure_reasons", "失败原因"),
    ("skill_bundle_sha256", "Skill包SHA256"),
    ("run_dir", "运行目录"),
)

HEADER_FILL = PatternFill("solid", fgColor="1F4E78")
SUBHEADER_FILL = PatternFill("solid", fgColor="D9EAF7")
SUCCESS_FILL = PatternFill("solid", fgColor="E2F0D9")
FAIL_FILL = PatternFill("solid", fgColor="FCE4D6")
THIN_BORDER = Border(
    left=Side(style="thin", color="D9E1F2"),
    right=Side(style="thin", color="D9E1F2"),
    top=Side(style="thin", color="D9E1F2"),
    bottom=Side(style="thin", color="D9E1F2"),
)


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _bool_number(value: Any) -> int | None:
    if value is None:
        return None
    return 1 if bool(value) else 0


def _first_text(*values: Any) -> str:
    for value in values:
        text = str(value or "").strip()
        if text:
            return text
    return ""


def _task_index(task_id: str, reference: dict[str, Any]) -> int | None:
    value = reference.get("index")
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(task_id or "")
    parts = text.rsplit("__", 1)
    if len(parts) == 2 and parts[1].isdigit():
        return int(parts[1])
    return None


def _dataset_form(task_form: Any, task_id: str, reference: dict[str, Any]) -> str:
    value = _first_text(reference.get("form"), TASK_FORM_TO_DATASET_FORM.get(str(task_form or "")))
    if value in DATASET_FORMS:
        return value
    match = str(task_id or "").split("__")
    for item in match:
        if item in DATASET_FORMS:
            return item
    return ""


def _record_scope(split: str) -> str:
    if split in FORMAL_SPLITS:
        return "正式"
    if split == "dev":
        return "开发/冒烟"
    if split:
        return f"非正式/{split}"
    return "未知"


def _model_id(
    manifest: dict[str, Any], generation: dict[str, Any], frozen: dict[str, Any]
) -> str:
    generated_model = str(generation.get("model") or "").strip()
    if generated_model:
        return generated_model
    frozen_model = str(frozen.get("model") or "").strip()
    if frozen_model:
        return frozen_model
    snapshot = str(manifest.get("model_snapshot") or "").strip()
    if not snapshot or snapshot == "unconfigured":
        return ""
    return snapshot.split("@", 1)[0]


def collect_run_records(runs_root: Path) -> list[dict[str, Any]]:
    """Collect one flat record per directory containing ``manifest.json``."""

    root = Path(runs_root).expanduser().resolve()
    if not root.is_dir():
        raise NotADirectoryError(root)
    records: list[dict[str, Any]] = []
    for manifest_path in sorted(root.rglob("manifest.json"), key=lambda item: item.as_posix()):
        run_dir = manifest_path.parent
        manifest = _read_json(manifest_path)
        task = _read_json(run_dir / "input.json")
        frozen = _read_json(run_dir / "frozen_config.json")
        reference = _read_json(run_dir / "scorer_private" / "reference.json")
        evaluation = _read_json(run_dir / "evaluation.json")
        model_score = _read_json(run_dir / "model_score.json")
        timer = _read_json(run_dir / "timer.json")
        trace = _read_json(run_dir / "trace.json")
        backend = _read_json(run_dir / "backend_status.json") or (
            trace.get("pyosis") if isinstance(trace.get("pyosis"), dict) else {}
        )
        compile_result = _read_json(run_dir / "compile.json")
        # per-architecture generation log (t1_..t5_generation.json / t2_generation.json)
        generation = _read_json(run_dir / "generated" / "t2_generation.json") or {}
        for candidate in ("t1_generation.json", "t3_generation.json",
                          "t4_generation.json", "t5_generation.json",
                          "t6_generation.json"):
            if not generation:
                generation = _read_json(run_dir / "generated" / candidate)
        reference_score = _read_json(run_dir / "reference_score.json")
        runtime_score = _read_json(run_dir / "runtime_score.json")
        runtime_measurements = _read_json(run_dir / "runtime_measurements.json")
        failure_reasons = evaluation.get("failure_reasons") or []
        if isinstance(failure_reasons, list):
            failure_reasons = "; ".join(str(item) for item in failure_reasons)
        else:
            failure_reasons = str(failure_reasons)

        task_id = manifest.get("task_id") or task.get("task_id") or run_dir.name
        task_form = task.get("task_form")
        split = _first_text(
            frozen.get("split"), reference.get("split"),
            (task.get("metadata") or {}).get("split")
            if isinstance(task.get("metadata"), dict) else "",
        )
        dataset_form = _dataset_form(task_form, str(task_id), reference)
        trace_excluded = bool(trace.get("excluded_from_aggregate"))
        scope = _record_scope(split)
        aggregation_eligible = int(split in FORMAL_SPLITS and not trace_excluded)
        excluded_reason = ""
        if trace_excluded:
            excluded_reason = "infrastructure_failure"
        elif aggregation_eligible == 0:
            excluded_reason = f"split={split or 'unknown'}"
        skill_snapshot = frozen.get("skills_snapshot")
        skill_hash = manifest.get("skill_bundle_sha256")
        if not skill_hash and isinstance(skill_snapshot, dict):
            skill_hash = skill_snapshot.get("sha256")

        record = {
            "task_id": task_id,
            "split": split,
            "dataset_form": dataset_form,
            "task_form": task_form,
            "task_index": _task_index(str(task_id), reference),
            "module": reference.get("module"),
            "target": reference.get("target"),
            "record_scope": scope,
            "aggregation_eligible": aggregation_eligible,
            "excluded_reason": excluded_reason,
            "architecture_id": manifest.get("architecture_id") or trace.get("architecture_id"),
            "seed": manifest.get("seed", trace.get("seed")),
            "bridge_type": task.get("bridge_type") or reference.get("bridge_type"),
            "difficulty": task.get("difficulty"),
            "framework_version": manifest.get("framework_version"),
            "model_id": _model_id(manifest, generation, frozen),
            "base_url": frozen.get("base_url"),
            "temperature": frozen.get("temperature"),
            "max_tokens": frozen.get("max_tokens"),
            "model_timeout_s": frozen.get("model_timeout_s"),
            "max_steps": frozen.get("max_steps"),
            "run_status": timer.get("status") or trace.get("status"),
            "complete_success": _bool_number(evaluation.get("complete_success")),
            "quality_score": evaluation.get("quality_score"),
            "model_score": model_score.get("candidate_score"),
            "model_score_status": model_score.get("status"),
            "runtime_score": runtime_score.get("candidate_score"),
            "runtime_score_5": runtime_score.get("total_score_5"),
            "runtime_score_status": runtime_score.get("status"),
            "runtime_measurements_status": runtime_measurements.get("status"),
            "runtime_missing_params": "; ".join(
                str(item) for item in (runtime_score.get("missing_params") or [])
            ),
            "runtime_score_path": (
                str((run_dir / "runtime_score.json").resolve())
                if (run_dir / "runtime_score.json").is_file()
                else ""
            ),
            "reference_score": reference_score.get("overall_score"),
            "model_conformance": reference_score.get("model_conformance_score"),
            "reference_score_status": reference_score.get("status"),
            "compile_passed": _bool_number(compile_result.get("passed")),
            "model_created": _bool_number(backend.get("model_created")),
            "validation_passed": _bool_number(backend.get("validation_passed")),
            "solver_converged": _bool_number(backend.get("solver_converged")),
            "generation_status": generation.get("status"),
            "generation_elapsed_s": generation.get("elapsed_s"),
            "model_calls": generation.get("model_calls"),
            "tool_calls": generation.get("tool_calls"),
            "invalid_tool_calls": generation.get("invalid_tool_calls"),
            "framework_steps": generation.get("framework_steps"),
            "stop_reason": generation.get("stop_reason"),
            "infrastructure_failure": _bool_number(
                trace.get("generation", {}).get("infrastructure_failure")
                if isinstance(trace.get("generation"), dict)
                else None
            ),
            "excluded_from_aggregate": _bool_number(trace.get("excluded_from_aggregate")),
            "elapsed_s": timer.get("elapsed_s"),
            "total_timeout_s": timer.get("total_timeout_s", manifest.get("timeout_s")),
            "failure_code": manifest.get("failure_code") or backend.get("failure_code"),
            "failure_reasons": failure_reasons,
            "skill_bundle_sha256": skill_hash,
            "run_dir": str(run_dir.resolve()),
        }
        records.append(record)
    return sorted(
        records,
        key=lambda item: (
            str(item.get("task_id") or ""),
            str(item.get("architecture_id") or ""),
            item.get("seed") if isinstance(item.get("seed"), int) else -1,
        ),
    )


def _style_header(sheet, row: int = 1) -> None:
    for cell in sheet[row]:
        cell.font = Font(name="Arial", bold=True, color="FFFFFF")
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = THIN_BORDER


def _style_body(sheet) -> None:
    for row in sheet.iter_rows():
        for cell in row:
            if cell.row != 1:
                cell.font = Font(name="Arial", size=10)
                cell.alignment = Alignment(vertical="top", wrap_text=True)
                cell.border = THIN_BORDER


def _fit_columns(sheet, minimum: int = 10, maximum: int = 42) -> None:
    for cells in sheet.columns:
        letter = get_column_letter(cells[0].column)
        longest = max(len(str(cell.value or "")) for cell in cells)
        sheet.column_dimensions[letter].width = min(max(longest + 2, minimum), maximum)


def _add_table(sheet, display_name: str) -> None:
    """Add a filterable Excel table to a populated report sheet."""

    if sheet.max_row < 2 or sheet.max_column < 1:
        return
    table = Table(
        displayName=display_name,
        ref=f"A1:{get_column_letter(sheet.max_column)}{sheet.max_row}",
    )
    table.tableStyleInfo = TableStyleInfo(
        name="TableStyleMedium2",
        showFirstColumn=False,
        showLastColumn=False,
        showRowStripes=True,
        showColumnStripes=False,
    )
    sheet.add_table(table)


def _runs_sheet(sheet, records: list[dict[str, Any]]):
    sheet.append([label for _, label in RUN_COLUMNS])
    for record in records:
        sheet.append([record.get(key) for key, _ in RUN_COLUMNS])
    _style_header(sheet)
    _style_body(sheet)
    # Keep task identity visible while horizontal-scrolling through metrics.
    sheet.freeze_panes = "K2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    _fit_columns(sheet)

    key_to_col = {key: index + 1 for index, (key, _) in enumerate(RUN_COLUMNS)}
    for row in range(2, sheet.max_row + 1):
        for key in (
            "complete_success", "compile_passed", "model_created",
            "validation_passed", "aggregation_eligible",
        ):
            cell = sheet.cell(row=row, column=key_to_col[key])
            cell.fill = SUCCESS_FILL if cell.value == 1 else FAIL_FILL
            cell.alignment = Alignment(horizontal="center")
        sheet.cell(row=row, column=key_to_col["quality_score"]).number_format = "0.0"
        for key in ("model_score", "runtime_score"):
            sheet.cell(row=row, column=key_to_col[key]).number_format = "0.0%"
        sheet.cell(row=row, column=key_to_col["runtime_score_5"]).number_format = "0.00"
        for key in ("temperature",):
            sheet.cell(row=row, column=key_to_col[key]).number_format = "0.0"
        for key in (
            "generation_elapsed_s", "model_timeout_s", "elapsed_s", "total_timeout_s",
        ):
            sheet.cell(row=row, column=key_to_col[key]).number_format = "0.0"

    if records:
        _add_table(sheet, "RunDetails")
        quality_col = get_column_letter(key_to_col["quality_score"])
        quality_target = f"{quality_col}2:{quality_col}{sheet.max_row}"
        sheet.conditional_formatting.add(
            quality_target,
            FormulaRule(
                formula=[f"AND(ISNUMBER({quality_col}2),{quality_col}2>=80)"],
                fill=SUCCESS_FILL,
            ),
        )
        sheet.conditional_formatting.add(
            quality_target,
            FormulaRule(
                formula=[f"AND(ISNUMBER({quality_col}2),{quality_col}2<60)"],
                fill=FAIL_FILL,
            ),
        )
    return {key: index + 1 for index, (key, _) in enumerate(RUN_COLUMNS)}


def _bounded_ref(key_to_col: dict[str, int], key: str, last_row: int) -> str:
    column = get_column_letter(key_to_col[key])
    end = max(2, last_row)
    return f"'运行明细'!${column}$2:${column}${end}"


def _comparison_sheet(
    sheet,
    key_to_col: dict[str, int],
    last_row: int,
    *,
    score_key: str = "model_score",
    status_key: str = "model_score_status",
    score_label: str = "源码分均值",
    rate_label: str = "完整成功率",
    rate_mode: str = "complete_success",
    table_name: str = "FrameworkComparison",
) -> None:
    """One matrix row per bridge type × dataset form for one score path."""

    headers = ["桥型", "任务形式", "纳入统计运行数"]
    for architecture in ARCHITECTURES:
        headers.extend(
            [
                f"{architecture} {score_label}",
                f"{architecture} 有效N",
                f"{architecture} {rate_label}",
            ]
        )
    sheet.append(headers)

    bridge_ref = _bounded_ref(key_to_col, "bridge_type", last_row)
    form_ref = _bounded_ref(key_to_col, "dataset_form", last_row)
    arch_ref = _bounded_ref(key_to_col, "architecture_id", last_row)
    eligible_ref = _bounded_ref(key_to_col, "aggregation_eligible", last_row)
    status_ref = _bounded_ref(key_to_col, status_key, last_row)
    score_ref = _bounded_ref(key_to_col, score_key, last_row)
    success_ref = _bounded_ref(key_to_col, "complete_success", last_row)

    row_index = 2
    for bridge_type in BRIDGE_TYPES:
        for form in DATASET_FORMS:
            sheet.cell(row_index, 1, bridge_type)
            sheet.cell(row_index, 2, form)
            sheet.cell(
                row_index,
                3,
                f'=COUNTIFS({bridge_ref},$A{row_index},{form_ref},$B{row_index},{eligible_ref},1)',
            )
            column = 4
            for architecture in ARCHITECTURES:
                sheet.cell(
                    row_index,
                    column,
                    f'=IFERROR(AVERAGEIFS({score_ref},{bridge_ref},$A{row_index},{form_ref},$B{row_index},{arch_ref},"{architecture}",{eligible_ref},1,{status_ref},"evaluated"),"")',
                )
                sheet.cell(
                    row_index,
                    column + 1,
                    f'=COUNTIFS({bridge_ref},$A{row_index},{form_ref},$B{row_index},{arch_ref},"{architecture}",{eligible_ref},1,{status_ref},"evaluated")',
                )
                if rate_mode == "evaluated":
                    rate_formula = (
                        f'=IFERROR(COUNTIFS({bridge_ref},$A{row_index},{form_ref},$B{row_index},'
                        f'{arch_ref},"{architecture}",{eligible_ref},1,{status_ref},"evaluated")/'
                        f'COUNTIFS({bridge_ref},$A{row_index},{form_ref},$B{row_index},'
                        f'{arch_ref},"{architecture}",{eligible_ref},1),"")'
                    )
                else:
                    rate_formula = (
                        f'=IFERROR(COUNTIFS({bridge_ref},$A{row_index},{form_ref},$B{row_index},'
                        f'{arch_ref},"{architecture}",{eligible_ref},1,{success_ref},1)/'
                        f'COUNTIFS({bridge_ref},$A{row_index},{form_ref},$B{row_index},'
                        f'{arch_ref},"{architecture}",{eligible_ref},1),"")'
                    )
                sheet.cell(row_index, column + 2, rate_formula)
                column += 3
            row_index += 1

    _style_header(sheet)
    _style_body(sheet)
    sheet.freeze_panes = "D2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    _fit_columns(sheet, maximum=24)
    for row in range(2, sheet.max_row + 1):
        for column in range(4, sheet.max_column + 1):
            if (column - 4) % 3 in (0, 2):
                sheet.cell(row, column).number_format = "0.0%"
        sheet.cell(row, 3).number_format = "0"
    score_columns = [
        get_column_letter(column)
        for column in range(4, sheet.max_column + 1)
        if (column - 4) % 3 == 0
    ]
    # Formula rules explicitly require a numeric value, so an unavailable
    # score stays visually blank rather than being painted as a failure.
    for column in score_columns:
        target = f"{column}2:{column}{sheet.max_row}"
        sheet.conditional_formatting.add(
            target,
            FormulaRule(
                formula=[f"AND(ISNUMBER({column}2),{column}2>=0.8)"],
                fill=SUCCESS_FILL,
            ),
        )
        sheet.conditional_formatting.add(
            target,
            FormulaRule(
                formula=[f"AND(ISNUMBER({column}2),{column}2<0.6)"],
                fill=FAIL_FILL,
            ),
        )
    _add_table(sheet, table_name)


def _group_summary_sheet(sheet, key_to_col: dict[str, int], last_row: int) -> None:
    """Long-form group summary for filtering and statistical export."""

    sheet.append(
        [
            "桥型", "任务形式", "架构", "纳入统计记录数", "源码评分有效N",
            "源码评分均值", "完整成功率", "生成失败数", "超时数", "平均总耗时(s)",
        ]
    )
    refs = {key: _bounded_ref(key_to_col, key, last_row) for key in (
        "bridge_type", "dataset_form", "architecture_id", "aggregation_eligible",
        "model_score_status", "model_score", "complete_success", "generation_status",
        "elapsed_s",
    )}
    row_index = 2
    for bridge_type in BRIDGE_TYPES:
        for form in DATASET_FORMS:
            for architecture in ARCHITECTURES:
                sheet.cell(row_index, 1, bridge_type)
                sheet.cell(row_index, 2, form)
                sheet.cell(row_index, 3, architecture)
                base = (
                    f"{refs['bridge_type']},$A{row_index},{refs['dataset_form']},$B{row_index},"
                    f"{refs['architecture_id']},$C{row_index},{refs['aggregation_eligible']},1"
                )
                sheet.cell(row_index, 4, f"=COUNTIFS({base})")
                scored = base + f",{refs['model_score_status']},\"evaluated\""
                sheet.cell(row_index, 5, f"=COUNTIFS({scored})")
                sheet.cell(
                    row_index,
                    6,
                    f'=IFERROR(AVERAGEIFS({refs["model_score"]},{base},{refs["model_score_status"]},"evaluated"),"")',
                )
                sheet.cell(
                    row_index,
                    7,
                    f'=IFERROR(COUNTIFS({base},{refs["complete_success"]},1)/COUNTIFS({base}),"")',
                )
                sheet.cell(
                    row_index,
                    8,
                    f'=COUNTIFS({base},{refs["generation_status"]},"failed")+'
                    f'COUNTIFS({base},{refs["generation_status"]},"partial")',
                )
                sheet.cell(
                    row_index,
                    9,
                    f'=COUNTIFS({base},{refs["generation_status"]},"timeout")',
                )
                sheet.cell(
                    row_index,
                    10,
                    f'=IFERROR(AVERAGEIFS({refs["elapsed_s"]},{base}),"")',
                )
                row_index += 1
    _style_header(sheet)
    _style_body(sheet)
    sheet.freeze_panes = "D2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    _fit_columns(sheet, maximum=24)
    for row in range(2, sheet.max_row + 1):
        for column in (6, 7):
            sheet.cell(row, column).number_format = "0.0%"
        sheet.cell(row, 10).number_format = "0.0"
    _add_table(sheet, "GroupSummary")


_RUNTIME_ITEM_PARAM_KEYS = {
    "span_fit": "L",
    "total_length": "total_length",
    "min_height": "H_mid",
    "steel_ratio": "pst_steel_ratio",
    "vertical_pst": "has_vertical_tendon",
    "side_mid_ratio": "span_lengths",
    "zero_block": "zero_block_len",
    "material": "concrete_grade",
    "top_mid": "T_top_mid",
    "bot_mid": "T_mid",
    "top_root": "T_top_root",
    "bot_root": "T_root",
    "dia_root": "T_dia_root",
    "web_mid": "web_t_mid",
    "web_root": "web_t_support",
    "flange_tip": "flange_tip",
    "trend": "thickness_profile",
    "pier_height": "pier_heights",
}
_RUNTIME_DIM_PARAM_KEYS = {
    "D1_H_root": "H_root",
    "D2_H_mid": "H_mid",
    "D6_area": "section_area_avg",
    "D7_material": "concrete_grade",
}


def _runtime_cell(value: Any) -> Any:
    """Keep Excel cells scalar while retaining structured parameter values."""

    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple, set)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def _runtime_param_key(
    params: dict[str, Any],
    dimension_name: str,
    item_name: str,
) -> str:
    """Resolve a displayed scoring item to its canonical runtime parameter."""

    param_key = _RUNTIME_ITEM_PARAM_KEYS.get(item_name)
    if param_key is None:
        param_key = _RUNTIME_DIM_PARAM_KEYS.get(dimension_name)
    if param_key is None and item_name in params:
        param_key = item_name
    if param_key is None and dimension_name in params:
        param_key = dimension_name
    return param_key or ""


def _runtime_actual(
    params: dict[str, Any],
    dimension_name: str,
    item_name: str,
    detail: dict[str, Any],
) -> Any:
    if detail.get("actual") is not None:
        return detail.get("actual")
    param_key = _runtime_param_key(params, dimension_name, item_name)
    return params.get(param_key) if param_key else None


def _runtime_item_rows(records: list[dict[str, Any]]) -> list[list[Any]]:
    """Flatten runtime dimensions/items for audit, without changing source metrics."""

    rows: list[list[Any]] = []
    for record in records:
        path_text = str(record.get("runtime_score_path") or "")
        if not path_text:
            continue
        payload = _read_json(Path(path_text))
        if not payload:
            continue
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        missing = "; ".join(str(item) for item in (payload.get("missing_params") or []))
        dimensions = payload.get("dimensions") if isinstance(payload.get("dimensions"), dict) else {}
        for dimension_name, dimension in dimensions.items():
            if not isinstance(dimension, dict):
                continue
            dimension_score = dimension.get("score_5", dimension.get("score"))
            items = dimension.get("items") if isinstance(dimension.get("items"), dict) else {}
            if not items:
                rows.append([
                    record.get("task_id"), record.get("split"), record.get("dataset_form"),
                    record.get("bridge_type"), record.get("architecture_id"), record.get("seed"),
                    payload.get("status"), payload.get("candidate_score"), payload.get("total_score_5"),
                    dimension_name, dimension_score, "",
                    _runtime_param_key(params, dimension_name, ""),
                    dimension.get("score", dimension_score),
                    _runtime_cell(_runtime_actual(params, dimension_name, "", dimension)),
                    bool(dimension.get("skipped", False)),
                    dimension.get("note") or dimension.get("issue") or "",
                    missing, path_text,
                ])
                continue
            for item_name, item in items.items():
                item = item if isinstance(item, dict) else {"score": item}
                rows.append([
                    record.get("task_id"), record.get("split"), record.get("dataset_form"),
                    record.get("bridge_type"), record.get("architecture_id"), record.get("seed"),
                    payload.get("status"), payload.get("candidate_score"), payload.get("total_score_5"),
                    dimension_name, dimension_score, item_name,
                    _runtime_param_key(params, dimension_name, item_name),
                    item.get("score", item.get("score_5")),
                    _runtime_cell(_runtime_actual(params, dimension_name, item_name, item)),
                    bool(item.get("skipped", False)),
                    item.get("note") or item.get("issue") or "",
                    missing, path_text,
                ])
    return rows


def _runtime_item_sheet(sheet, records: list[dict[str, Any]]) -> None:
    sheet.append([
        "任务ID", "数据集", "任务形式", "桥型", "架构", "Seed", "运行态状态",
        "运行态模型分", "运行态模型分(5分)", "维度", "维度分(5分)", "评分项",
        "参数键", "评分项分", "实际值", "跳过", "说明", "缺失参数", "运行态评分文件",
    ])
    for row in _runtime_item_rows(records):
        sheet.append(row)
    _style_header(sheet)
    _style_body(sheet)
    sheet.freeze_panes = "J2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    _fit_columns(sheet, maximum=48)
    headers = [cell.value for cell in sheet[1]]
    for key in ("运行态模型分",):
        column = headers.index(key) + 1
        for row in range(2, sheet.max_row + 1):
            sheet.cell(row, column).number_format = "0.0%"
    for key in ("运行态模型分(5分)", "维度分(5分)", "评分项分"):
        column = headers.index(key) + 1
        for row in range(2, sheet.max_row + 1):
            sheet.cell(row, column).number_format = "0.00"
    _add_table(sheet, "RuntimeItemScores")


def _failure_category(record: dict[str, Any]) -> str | None:
    status = str(record.get("generation_status") or "").lower()
    run_status = str(record.get("run_status") or "").lower()
    failure_code = str(record.get("failure_code") or "").lower()
    if status == "timeout" or run_status == "timeout" or "timeout" in failure_code:
        return "timeout"
    if status in {"failed", "partial", "no_file_blocks"}:
        return status
    if failure_code:
        return failure_code[:80]
    if record.get("complete_success") != 1:
        reasons = str(record.get("failure_reasons") or "").split(";")
        return next((item.strip() for item in reasons if item.strip()), "other_failure")
    return None


def _failure_sheet(sheet, records: list[dict[str, Any]]) -> None:
    sheet.append(["失败类别", "总次数", "正式统计次数", "开发/冒烟次数", "示例任务"])
    grouped: dict[str, dict[str, Any]] = {}
    for record in records:
        category = _failure_category(record)
        if not category:
            continue
        item = grouped.setdefault(category, {"total": 0, "formal": 0, "dev": 0, "examples": []})
        item["total"] += 1
        if record.get("aggregation_eligible") == 1:
            item["formal"] += 1
        if record.get("record_scope") == "开发/冒烟":
            item["dev"] += 1
        if len(item["examples"]) < 3:
            item["examples"].append(str(record.get("task_id") or ""))
    for category in sorted(grouped):
        item = grouped[category]
        sheet.append([
            category, item["total"], item["formal"], item["dev"], "; ".join(item["examples"]),
        ])
    _style_header(sheet)
    _style_body(sheet)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    _fit_columns(sheet, maximum=42)
    _add_table(sheet, "FailureSummary")


def _frozen_config_sheet(sheet, records: list[dict[str, Any]]) -> None:
    sheet.append([
        "模型", "模型接口", "温度", "最大Token", "单次模型时限(s)", "最大步数",
        "总任务时限(s)", "数据集", "Skill包SHA256", "记录数", "纳入统计数", "示例运行目录",
    ])
    grouped: dict[tuple[Any, ...], dict[str, Any]] = {}
    for record in records:
        key = (
            record.get("model_id") or "",
            record.get("base_url") or "",
            record.get("temperature"),
            record.get("max_tokens"),
            record.get("model_timeout_s"),
            record.get("max_steps"),
            record.get("total_timeout_s"),
            record.get("split") or "",
            record.get("skill_bundle_sha256") or "",
        )
        item = grouped.setdefault(key, {"count": 0, "eligible": 0, "run_dir": record.get("run_dir")})
        item["count"] += 1
        item["eligible"] += int(record.get("aggregation_eligible") == 1)
    for key, item in sorted(grouped.items(), key=lambda pair: tuple(str(value) for value in pair[0])):
        sheet.append([*key, item["count"], item["eligible"], item["run_dir"]])
    _style_header(sheet)
    _style_body(sheet)
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = sheet.dimensions
    sheet.sheet_view.showGridLines = False
    _fit_columns(sheet, maximum=42)
    _add_table(sheet, "FrozenConfig")


def _notes_sheet(sheet, runs_root: Path, records: list[dict[str, Any]]):
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "OSIS 智能体框架对比实验结果"
    sheet["A1"].font = Font(name="Arial", size=16, bold=True, color="FFFFFF")
    sheet["A1"].fill = HEADER_FILL
    sheet["A1"].alignment = Alignment(horizontal="center")
    rows = [
        ("生成时间（UTC）", datetime.now(timezone.utc).isoformat()),
        ("结果目录", str(Path(runs_root).resolve())),
        ("识别运行数", len(records)),
        ("纳入正式统计数", sum(item.get("aggregation_eligible") == 1 for item in records)),
        ("排除记录数", sum(item.get("aggregation_eligible") != 1 for item in records)),
        ("源码主指标", "运行明细中的 CLI模型分（model_score.json:candidate_score）；这是当前正式主评分，不被运行态评分覆盖"),
        ("运行态旁路指标", "运行态模型分来自 runtime_measurements.json → runtime_score.json；仅在 PyOSIS 执行后快照可用时计算，用于后续逐项参数审计"),
        ("数据来源", "每个 run_dir 的 manifest/input/frozen_config/evaluation/model_score/runtime_score/timer/backend_status/runtime_measurements JSON"),
        ("正式统计范围", "仅纳入 split=train/test 且未标记 infrastructure_failure 的记录；开发/冒烟记录保留在明细但不进入汇总"),
        ("任务形式", "数据集 full/gen/edit 映射为内部 whole/module/modify，表格统一展示 full/gen/edit"),
        ("完整成功", "同时满足候选工程、PyOSIS建模、验证、CLI评分和产物完整性"),
        ("注意", "框架对比和分组汇总只统计纳入正式统计的记录；空白源码分表示没有可用 CLI 评分，不等同于 0 分。"),
    ]
    for row in rows:
        sheet.append(row)
    for row in range(2, sheet.max_row + 1):
        sheet.cell(row, 1).font = Font(name="Arial", bold=True)
        sheet.cell(row, 1).fill = SUBHEADER_FILL
        sheet.cell(row, 2).font = Font(name="Arial")
        for column in (1, 2):
            sheet.cell(row, column).border = THIN_BORDER
            sheet.cell(row, column).alignment = Alignment(vertical="top", wrap_text=True)
    sheet.column_dimensions["A"].width = 24
    sheet.column_dimensions["B"].width = 90
    sheet.sheet_view.showGridLines = False
    return sheet


def export_results(runs_root: Path, output: Path) -> Path:
    records = collect_run_records(runs_root)
    output = Path(output).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    workbook = Workbook()
    default = workbook.active
    workbook.remove(default)
    # Create all referenced worksheets before writing cross-sheet formulas.
    comparison = workbook.create_sheet("框架对比")
    group_summary = workbook.create_sheet("分组汇总")
    runs_sheet = workbook.create_sheet("运行明细")
    failure_sheet = workbook.create_sheet("失败汇总")
    frozen_sheet = workbook.create_sheet("冻结配置")
    notes = workbook.create_sheet("说明")

    key_to_col = _runs_sheet(runs_sheet, records)
    last_row = runs_sheet.max_row
    _comparison_sheet(comparison, key_to_col, last_row)
    _group_summary_sheet(group_summary, key_to_col, last_row)
    _failure_sheet(failure_sheet, records)
    _frozen_config_sheet(frozen_sheet, records)
    _notes_sheet(notes, Path(runs_root), records)
    # Keep historical workbooks stable when they predate runtime sidecars.
    # Once a runner has emitted either availability status, expose the two
    # runtime views alongside (never instead of) the source-score views.
    runtime_present = any(
        record.get("runtime_score_status") or record.get("runtime_measurements_status")
        for record in records
    )
    if runtime_present:
        runtime_comparison = workbook.create_sheet("运行态对比", 1)
        _comparison_sheet(
            runtime_comparison,
            key_to_col,
            last_row,
            score_key="runtime_score",
            status_key="runtime_score_status",
            score_label="运行态分均值",
            rate_label="运行态可用率",
            rate_mode="evaluated",
            table_name="RuntimeComparison",
        )
        runtime_items = workbook.create_sheet("运行态逐项评分", 2)
        _runtime_item_sheet(runtime_items, records)
    workbook.calculation.fullCalcOnLoad = True
    workbook.calculation.forceFullCalc = True
    workbook.calculation.calcMode = "auto"
    workbook.save(output)
    return output


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export OSIS comparison runs to Excel")
    parser.add_argument("--runs-dir", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    records = collect_run_records(args.runs_dir)
    runs_dir = args.runs_dir or resolve_run_root()
    output = args.output or (runs_dir / "experiment_results.xlsx")
    output = export_results(runs_dir, output)
    print(
        json.dumps(
            {"status": "written", "records": len(records), "output": str(output)},
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

