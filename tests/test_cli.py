import json
import subprocess
import sys
from pathlib import Path


def test_conformance_cli_runs_from_project_root(tmp_path: Path):
    skills = tmp_path / "skills"
    (skills / "alpha").mkdir(parents=True)
    (skills / "alpha" / "SKILL.md").write_text(
        "---\nname: Alpha\ndescription: test\n---\nBody", encoding="utf-8"
    )
    project = Path(__file__).resolve().parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_conformance.py",
            "--skills-dir",
            str(skills),
            "--runs-dir",
            str(tmp_path / "runs"),
        ],
        cwd=project,
        check=True,
        capture_output=True,
        text=True,
    )
    assert '"skill_count": 1' in result.stdout
    payload = json.loads(result.stdout[result.stdout.index('{', result.stdout.index('skill_count')):])
    assert len(payload["runs"]) == 6
    assert {item["status"] for item in payload["runs"]} == {"not_configured"}
