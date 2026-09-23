import json
from pathlib import Path

from openpyxl import load_workbook

from scripts.export_results_xlsx import collect_run_records, export_results


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _make_run(
    root: Path,
    *,
    task_id: str = "bridge_case__full__000",
    architecture: str = "T2",
    bridge_type: str = "cantilever_box",
    task_form: str = "whole",
    split: str = "test",
    score: float = 0.91,
    complete_success: bool = True,
    generation_status: str = "completed",
    excluded: bool = False,
    target: str = "",
    module: str = "",
) -> Path:
    run = root / f"{task_id}__{architecture}__seed0"
    _write_json(
        run / "manifest.json",
        {
            "task_id": task_id,
            "architecture_id": architecture,
            "framework_version": "langgraph==1.2.11/langchain==1.3.18",
            "model_snapshot": "glm-5.3-flash@http://example/v1",
            "seed": 0,
            "failure_code": None,
            "skill_bundle_sha256": "abc",
        },
    )
    _write_json(
        run / "input.json",
        {
            "task_id": task_id,
            "bridge_type": bridge_type,
            "task_form": task_form,
            "difficulty": "L1",
            "metadata": {"split": split},
        },
    )
    _write_json(
        run / "evaluation.json",
        {
            "complete_success": complete_success,
            "quality_score": 88.5,
            "failure_reasons": [] if complete_success else ["generation_failed"],
        },
    )
    _write_json(
        run / "model_score.json",
        {"status": "evaluated", "candidate_score": score},
    )
    _write_json(
        run / "timer.json",
        {
            "status": "executed_and_scored",
            "elapsed_s": 12.5,
            "total_timeout_s": 300,
        },
    )
    _write_json(
        run / "backend_status.json",
        {
            "status": "succeeded",
            "model_created": True,
            "validation_passed": True,
            "solver_converged": False,
        },
    )
    _write_json(
        run / "generated" / "t2_generation.json",
        {"status": generation_status, "tool_calls": 4, "elapsed_s": 5.0},
    )
    _write_json(
        run / "trace.json",
        {
            "architecture_id": architecture,
            "excluded_from_aggregate": excluded,
            "generation": {
                "infrastructure_failure": excluded,
            },
        },
    )
    _write_json(
        run / "frozen_config.json",
        {
            "split": split,
            "model": "glm-5.3-flash",
            "base_url": "http://example/v1",
            "temperature": 0.0,
            "max_tokens": 65536,
            "total_timeout_s": 300,
            "skills_snapshot": {"sha256": "abc"},
        },
    )
    _write_json(
        run / "scorer_private" / "reference.json",
        {"split": split, "form": {"whole": "full", "module": "gen", "modify": "edit"}.get(task_form),
         "index": 0, "module": module, "target": target,
         "source": "悬浇连续梁-20+30+20-示例-1"},
    )
    return run


def test_collect_run_records_reads_all_modeling_artifacts(tmp_path: Path):
    run = _make_run(tmp_path / "runs")
    records = collect_run_records(tmp_path / "runs")
    assert len(records) == 1
    record = records[0]
    assert record["task_id"] == "bridge_case__full__000"
    assert record["architecture_id"] == "T2"
    assert record["model_id"] == "glm-5.3-flash"
    assert record["split"] == "test"
    assert record["dataset_form"] == "full"
    assert record["task_index"] == 0
    assert record["aggregation_eligible"] == 1
    assert record["complete_success"] == 1
    assert record["quality_score"] == 88.5
    assert record["model_score"] == 0.91
    assert record["model_created"] == 1
    assert record["tool_calls"] == 4
    assert record["run_dir"] == str(run.resolve())
    assert record["result_tree"] == ""
    assert record["source"] == "悬浇连续梁-20+30+20-示例-1"


def test_export_results_creates_formatted_workbook_with_summary_formulas(tmp_path: Path):
    _make_run(tmp_path / "runs")
    output = export_results(tmp_path / "runs", tmp_path / "results.xlsx")
    workbook = load_workbook(output, data_only=False)

    assert workbook.sheetnames[:3] == ["Table 1", "按任务形式", "桥型-悬浇箱梁"]
    assert "结果目录" in workbook.sheetnames
    tree = workbook["结果目录"]
    assert [cell.value for cell in tree[1]][:3] == ["架构", "桥型", "任务形式"]
    table1 = workbook["Table 1"]
    assert [cell.value for cell in table1[1]] == [
        "Configuration", "Compile", "Struct.", "Constr.", "Sim.",
        "Time", "Resource", "Weighted",
    ]
    assert table1[2][0].value == "T1 direct"
    assert table1[7][0].value == "T6 OSIS-AI"
    task_forms = workbook["按任务形式"]
    assert [cell.value for cell in task_forms[1]][:2] == ["Task form", "Configuration"]
    assert {task_forms.cell(row, 1).value for row in range(2, task_forms.max_row + 1)} == {
        "full", "gen", "edit",
    }
    bridge = workbook["桥型-悬浇箱梁"]
    assert {bridge.cell(row, 1).value for row in range(2, bridge.max_row + 1)} == {
        "full", "gen", "edit",
    }
    runs = workbook["运行明细"]
    assert runs.max_row == 2
    assert runs["A2"].value == "bridge_case__full__000"
    assert runs["B2"].value == "test"
    assert runs["A1"].font.name == "Arial"
    summary = workbook["按父仓库规则"]
    # One physical row per (bridge type × dataset form): 6 × 3 + 1 header = 19.
    # Each metric gets its own column triple inside every architecture group, so
    # adding metrics widens the sheet rather than lengthening it.
    assert summary.max_row == 6 * 3 + 1
    assert "full" in [summary.cell(2, 2).value, summary.cell(3, 2).value]
    assert any(
        isinstance(cell.value, str) and "AVERAGEIFS" in cell.value
        for row in summary.iter_rows()
        for cell in row
    )
    assert any(
        isinstance(cell.number_format, str) and "%" in cell.number_format
        for row in summary.iter_rows()
        for cell in row
    )


def test_nonformal_and_infrastructure_runs_are_visible_but_not_aggregated(tmp_path: Path):
    root = tmp_path / "runs"
    _make_run(root, split="dev", complete_success=False, generation_status="failed")
    _make_run(
        root,
        task_id="hollow_slab__gen__001",
        architecture="T3",
        bridge_type="hollow_slab",
        task_form="module",
        split="test",
        score=0.72,
        target="prep/_2_property.py",
        module="property",
    )
    _make_run(
        root,
        task_id="hollow_slab__gen__001",
        architecture="T4",
        bridge_type="hollow_slab",
        task_form="module",
        split="test",
        score=0.55,
        generation_status="timeout",
        complete_success=False,
        excluded=True,
    )
    records = collect_run_records(root)
    assert len(records) == 3
    assert sum(item["aggregation_eligible"] for item in records) == 1

    output = export_results(root, tmp_path / "results.xlsx")
    workbook = load_workbook(output, data_only=False)
    detail = workbook["运行明细"]
    headers = [cell.value for cell in detail[1]]
    scope_col = headers.index("统计范围") + 1
    eligible_col = headers.index("纳入统计") + 1
    assert {detail.cell(row, scope_col).value for row in range(2, 5)} == {"正式", "开发/冒烟"}
    assert [detail.cell(row, eligible_col).value for row in range(2, 5)].count(1) == 1

    failures = workbook["失败汇总"]
    failure_headers = [cell.value for cell in failures[1]]
    assert "timeout" in {
        failures.cell(row, failure_headers.index("失败类别") + 1).value
        for row in range(2, failures.max_row + 1)
    }


def test_comparison_matrix_has_one_row_for_each_bridge_and_dataset_form(tmp_path: Path):
    root = tmp_path / "runs"
    _make_run(root, task_id="cantilever_box__full__000", architecture="T1")
    _make_run(
        root,
        task_id="precast_t_girder__edit__002",
        architecture="T6",
        bridge_type="precast_t_girder",
        task_form="modify",
        target="prep/_2_property.py",
        module="property",
    )
    output = export_results(root, tmp_path / "results.xlsx")
    workbook = load_workbook(output, data_only=False)
    sheet = workbook["按父仓库规则"]
    # Header + 6 bridge types × 3 forms = 19 rows, but several (bridge, form)
    # cells repeat for each of the 6 metrics.  The 18 (bridge, form) pairs
    # are still present as unique values.
    rows = {(sheet.cell(row, 1).value, sheet.cell(row, 2).value) for row in range(2, sheet.max_row + 1)}
    assert len(rows) == 18
    assert ("cantilever_box", "full") in rows
    assert ("precast_t_girder", "edit") in rows


def test_runtime_sidecars_do_not_enter_official_workbook(tmp_path: Path):
    root = tmp_path / "runs"
    run = _make_run(root, score=0.91)
    _write_json(
        run / "runtime_measurements.json",
        {
            "schema_version": "osis-runtime-measurements-v1",
            "status": "available",
            "summary": {"nodes": 4},
        },
    )
    _write_json(
        run / "runtime_score.json",
        {
            "scorer": "osis-runtime-model-conformance",
            "source": "pyosis_runtime_snapshot",
            "status": "evaluated",
            "bridge_type": "cantilever_box",
            "candidate_score": 0.734,
            "total_score_5": 3.67,
            "params": {"L": 120.0, "H_root": 7.5, "concrete_grade": 60},
            "missing_params": ["has_prestress"],
            "dimensions": {
                "D1_H_root": {"score_5": 1.0, "actual": 7.5},
                "D5_consistency": {
                    "score_5": 0.8,
                    "items": {
                        "span_fit": {"score": 1.0, "note": "L=120"},
                        "material": {"score": 0.6, "note": "C60"},
                    },
                },
            },
        },
    )

    output = export_results(root, tmp_path / "runtime-results.xlsx")
    workbook = load_workbook(output, data_only=False)
    detail_headers = [cell.value for cell in workbook["运行明细"][1]]
    assert "按实际建模质量" not in workbook.sheetnames
    assert "运行态逐项评分" not in workbook.sheetnames
    assert "运行态模型分" not in detail_headers
    assert "运行态评分状态" not in detail_headers
