from __future__ import annotations

from pathlib import Path

from common import paths


def test_parent_repo_is_resolved_from_environment_without_machine_default(tmp_path, monkeypatch):
    parent = tmp_path / "osis-skill-enhance-main"
    (parent / "configs").mkdir(parents=True)
    (parent / ".agents" / "skills").mkdir(parents=True)
    (parent / "datasets").mkdir()
    (parent / "configs" / "datasets.yaml").write_text("bridges: {}\n", encoding="utf-8")

    monkeypatch.setattr(paths, "FRAMEWORK_ROOT", tmp_path / "comparison")
    monkeypatch.setenv("OSIS_PARENT_REPO", str(parent))
    assert paths.resolve_parent_repo() == parent.resolve()


def test_run_and_skill_roots_are_portable_and_overridable(tmp_path, monkeypatch):
    framework = tmp_path / "comparison"
    monkeypatch.setattr(paths, "FRAMEWORK_ROOT", framework)
    monkeypatch.delenv("OSIS_RUN_ROOT", raising=False)
    monkeypatch.delenv("OSIS_SKILLS_DIR", raising=False)

    assert paths.resolve_run_root() == (framework / "runs").resolve()
    assert paths.resolve_skills_dir() == (framework / "checkpoints" / "train-all" / "skills").resolve()

    run_root = tmp_path / "runs"
    skills = tmp_path / "skills"
    monkeypatch.setenv("OSIS_RUN_ROOT", str(run_root))
    monkeypatch.setenv("OSIS_SKILLS_DIR", str(skills))
    assert paths.resolve_run_root() == run_root.resolve()
    assert paths.resolve_skills_dir() == skills.resolve()


def test_opencode_dir_uses_explicit_or_environment_path(tmp_path, monkeypatch):
    install = tmp_path / "opencode"
    install.mkdir()
    monkeypatch.setenv("OSIS_OPENCODE_DIR", str(install))
    assert paths.resolve_opencode_dir() == install.resolve()
    explicit = tmp_path / "explicit-opencode"
    assert paths.resolve_opencode_dir(explicit) == explicit.resolve()


def test_recorded_parent_repo_ignores_placeholder_and_supports_relative_value(tmp_path, monkeypatch):
    framework = tmp_path / "comparison"
    (framework / "configs").mkdir(parents=True)
    (framework / "configs" / "parent_repo.txt").write_text(
        "<path-to-osis-skill-enhance-main>\n", encoding="utf-8"
    )
    monkeypatch.setattr(paths, "FRAMEWORK_ROOT", framework)
    assert paths.recorded_parent_repo() is None

    parent = tmp_path / "parent"
    (framework / "configs" / "parent_repo.txt").write_text(
        "../parent\n", encoding="utf-8"
    )
    assert paths.recorded_parent_repo() == parent.resolve()
