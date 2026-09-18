import json
from pathlib import Path

from scripts.progress_board import render, scan_roots


def _run(root: Path, name: str, *, quality: float | None = None) -> Path:
    run_dir = root / name
    (run_dir / "generated").mkdir(parents=True)
    if quality is not None:
        (run_dir / "evaluation.json").write_text(
            json.dumps({"quality_score": quality, "complete_success": quality > 50}),
            encoding="utf-8",
        )
    return run_dir


def test_scan_roots_keeps_form_and_index_separate(tmp_path):
    root = tmp_path / "campaign"
    _run(root, "cantilever_box__full__000__T1__seed0", quality=20)
    _run(root, "cantilever_box__gen__000__T1__seed0", quality=30)

    data = scan_roots([root])

    keys = {(row["form"], row["index"], row["architecture"]) for row in data["rows"]}
    assert keys == {("full", 0, "T1"), ("gen", 0, "T1")}
    assert data["summary"]["observed_runs"] == 2


def test_scan_roots_counts_expected_queued_tasks(tmp_path):
    root = tmp_path / "campaign"
    _run(root, "cantilever_box__full__000__T1__seed0", quality=20)

    data = scan_roots([root], expected={"total": 4})

    assert data["summary"]["observed_runs"] == 1
    assert data["summary"]["queued"] == 3


def test_render_contains_summary_and_framework_cells(tmp_path):
    root = tmp_path / "campaign"
    _run(root, "cantilever_box__full__000__T1__seed0", quality=20)
    data = scan_roots([root])

    html = render(data, "统一进度")

    assert "总任务" in html
    assert "已完成" in html
    assert "运行中" in html
    assert "排队" in html
    assert "cantilever_box" in html
    assert "T1" in html
