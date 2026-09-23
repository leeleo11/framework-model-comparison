from pathlib import Path

from common.run_layout import (
    candidate_run_dirs,
    cell_name,
    iter_cell_dirs,
    migrate_flat_run,
    migrate_source_cell_to_legacy,
    nested_run_dir,
    parse_cell_name,
    result_tree,
    run_dir_for,
    run_dir_from_task,
)


class _Task:
    task_id = "conventional_box__gen__000"
    bridge_type = "conventional_box"
    task_form = "module"


def test_run_dir_for_nests_architecture_bridge_form(tmp_path: Path):
    path = run_dir_for(tmp_path, "osis-bridge-cantilever-box", "edit", 0, "T3", 1)
    assert path == tmp_path / "T3" / "cantilever_box" / "edit" / "cantilever_box__edit__000__T3__seed1"
    assert parse_cell_name(path.name)["bridge"] == "cantilever_box"


def test_run_dir_for_ignores_source_name(tmp_path: Path):
    path = run_dir_for(
        tmp_path,
        "osis-bridge-conventional-box",
        "gen",
        0,
        "T1",
        0,
        source="现浇等截面箱梁-20+30+20-单箱双室-12.75-1",
    )
    assert path == (
        tmp_path / "T1" / "conventional_box" / "gen"
        / "conventional_box__gen__000__T1__seed0"
    )


def test_run_dir_from_task_uses_dataset_form(tmp_path: Path):
    path = run_dir_from_task(tmp_path, _Task(), "T6", 0)
    assert path.parts[-4:] == ("T6", "conventional_box", "gen", "conventional_box__gen__000__T6__seed0")


def test_iter_cell_dirs_finds_nested_and_flat(tmp_path: Path):
    nested = nested_run_dir(tmp_path, "hollow_slab", "full", 0, "T1", 0)
    nested.mkdir(parents=True)
    (nested / "manifest.json").write_text("{}", encoding="utf-8")
    leftover = (
        tmp_path / "T1" / "hollow_slab" / "gen" / "空心板-20-示例"
    )
    leftover.mkdir(parents=True)
    (leftover / "evaluation.json").write_text("{}", encoding="utf-8")
    flat = tmp_path / cell_name("rigid_frame", "gen", 2, "T2", 4)
    flat.mkdir()
    found = {path.name for path in iter_cell_dirs(tmp_path)}
    assert found == {nested.name, leftover.name, flat.name}


def test_candidate_run_dirs_include_legacy_flat(tmp_path: Path):
    dirs = candidate_run_dirs(tmp_path, "hollow_slab", "full", 0, "T4", 0)
    assert dirs[0] == nested_run_dir(tmp_path, "hollow_slab", "full", 0, "T4", 0)
    assert dirs[1] == tmp_path / "hollow_slab__full__000__T4__seed0"


def test_candidate_run_dirs_keep_source_leaf_as_fallback(tmp_path: Path):
    dirs = candidate_run_dirs(
        tmp_path, "hollow_slab", "full", 0, "T4", 0, source="空心板-20-示例"
    )
    assert dirs[0].name == "hollow_slab__full__000__T4__seed0"
    assert dirs[1].name == "空心板-20-示例"
    assert dirs[2] == tmp_path / "hollow_slab__full__000__T4__seed0"


def test_migrate_flat_run_moves_into_nested_tree(tmp_path: Path):
    source = tmp_path / "precast_t_girder__edit__000__T5__seed0"
    source.mkdir()
    (source / "evaluation.json").write_text("{}", encoding="utf-8")
    dest = migrate_flat_run(source, tmp_path)
    assert dest == tmp_path / "T5" / "precast_t_girder" / "edit" / source.name
    assert dest.is_dir()
    assert not source.exists()
    assert result_tree(dest, tmp_path) == "T5/precast_t_girder/edit"


def test_migrate_source_cell_to_legacy_renames_nested_leaf(tmp_path: Path):
    leftover = (
        tmp_path / "T1" / "conventional_box" / "gen"
        / "现浇等截面箱梁-20+30+20-单箱双室-12.75-1"
    )
    leftover.mkdir(parents=True)
    (leftover / "input.json").write_text(
        '{"task_id": "conventional_box__gen__000"}',
        encoding="utf-8",
    )
    (leftover / "manifest.json").write_text('{"seed": 0}', encoding="utf-8")
    dest = migrate_source_cell_to_legacy(leftover, tmp_path)
    assert dest == (
        tmp_path / "T1" / "conventional_box" / "gen"
        / "conventional_box__gen__000__T1__seed0"
    )
    assert dest.is_dir()
    assert not leftover.exists()
