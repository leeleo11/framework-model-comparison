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


def test_scan_roots_finds_source_named_nested_dirs(tmp_path):
    root = tmp_path / "campaign"
    nested = root / "T2" / "conventional_box" / "gen"
    run_dir = _run(nested, "现浇等截面箱梁-20+30+20-单箱双室-12.75-1", quality=80)
    (run_dir / "input.json").write_text(
        json.dumps({"task_id": "conventional_box__gen__000"}),
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps({"task_id": "conventional_box__gen__000", "seed": 0}),
        encoding="utf-8",
    )
    (run_dir / "scorer_private").mkdir()
    (run_dir / "scorer_private" / "reference.json").write_text(
        json.dumps({"source": "现浇等截面箱梁-20+30+20-单箱双室-12.75-1"}),
        encoding="utf-8",
    )

    data = scan_roots([root])

    assert data["summary"]["observed_runs"] == 1
    row = data["rows"][0]
    assert row["architecture"] == "T2"
    assert row["bridge"] == "conventional_box"
    assert row["form"] == "gen"
    assert row["source"] == "现浇等截面箱梁-20+30+20-单箱双室-12.75-1"
    html = render(data, "统一进度")
    assert "conventional_box__gen__000" in html


def test_scan_roots_finds_nested_architecture_bridge_form_dirs(tmp_path):
    root = tmp_path / "campaign"
    nested = root / "T1" / "cantilever_box" / "gen"
    _run(nested, "cantilever_box__gen__000__T1__seed0", quality=40)

    data = scan_roots([root])

    assert data["summary"]["observed_runs"] == 1
    row = data["rows"][0]
    assert row["architecture"] == "T1"
    assert row["bridge"] == "cantilever_box"
    assert row["form"] == "gen"


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
    assert 'http-equiv="refresh"' not in html
    assert "DOMParser" in html
