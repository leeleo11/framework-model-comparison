from pathlib import Path

import pytest

from common.task_schema import TaskSpec, TaskValidationError, load_task


def test_task_spec_accepts_modeling_task():
    task = TaskSpec.from_dict(
        {
            "task_id": "cantilever_whole_l1_01",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "建立一座连续刚构桥的基础模型。",
        }
    )
    assert task.task_id == "cantilever_whole_l1_01"
    assert task.task_form == "whole"
    assert task.initial_project_snapshot is None
    assert task.total_timeout_s == 5400
    assert task.subtask_timeout_s["P4"] == 1200


def test_task_spec_accepts_explicit_time_budgets():
    task = TaskSpec.from_dict(
        {
            "task_id": "timed",
            "bridge_type": "cantilever_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
            "total_timeout_s": 900,
            "subtask_timeout_s": {"P0": 60, "P1": 120, "P2": 300, "P3": 120, "P4": 180, "P5": 90, "P6": 30},
        }
    )
    assert task.total_timeout_s == 900
    assert task.subtask_timeout_s["P2"] == 300


def test_task_spec_rejects_non_positive_time_budget():
    with pytest.raises(TaskValidationError, match="total_timeout_s"):
        TaskSpec.from_dict(
            {
                "task_id": "bad-time",
                "bridge_type": "cantilever_box",
                "task_form": "whole",
                "difficulty": "L1",
                "natural_language_requirement": "build model",
                "total_timeout_s": 0,
            }
        )


def test_task_spec_requires_snapshot_for_modify():
    with pytest.raises(TaskValidationError, match="initial_project_snapshot"):
        TaskSpec.from_dict(
            {
                "task_id": "modify_01",
                "bridge_type": "hollow_slab",
                "task_form": "modify",
                "difficulty": "L2",
                "natural_language_requirement": "修改截面厚度。",
            }
        )


def test_task_spec_rejects_unknown_bridge_type():
    with pytest.raises(TaskValidationError, match="bridge_type"):
        TaskSpec.from_dict(
            {
                "task_id": "bad",
                "bridge_type": "unknown",
                "task_form": "whole",
                "difficulty": "L1",
                "natural_language_requirement": "建立模型。",
            }
        )


def test_load_task_reads_json(tmp_path: Path):
    path = tmp_path / "task.json"
    path.write_text(
        '{"task_id":"x","bridge_type":"rigid_frame","task_form":"module",'
        '"difficulty":"L1","natural_language_requirement":"生成节点模块。"}',
        encoding="utf-8",
    )
    assert load_task(path).task_id == "x"
