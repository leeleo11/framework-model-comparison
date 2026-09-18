import json
import subprocess
from pathlib import Path

from common.cli_scorer import ModelConformanceCLIScorer
from common.task_schema import TaskSpec


def _task() -> TaskSpec:
    return TaskSpec.from_dict(
        {
            "task_id": "score",
            "bridge_type": "conventional_box",
            "task_form": "whole",
            "difficulty": "L1",
            "natural_language_requirement": "build model",
            "metadata": {"is_continuous": True},
        }
    )


def test_cli_scorer_invokes_existing_repository_script(tmp_path: Path):
    parent = tmp_path / "parent"
    script = parent / ".agents" / "skills" / "osis-auto-testconformance" / "scripts" / "test_conformance.py"
    script.parent.mkdir(parents=True)
    script.write_text("# scorer\n", encoding="utf-8")
    candidate = tmp_path / "candidate" / "py" / "prep"
    candidate.mkdir(parents=True)
    calls = []

    def fake_runner(command, **kwargs):
        calls.append(list(command))
        output_path = Path(command[command.index("--output") + 1])
        output_path.write_text("# report\n", encoding="utf-8")
        return subprocess.CompletedProcess(
            command,
            0,
            ">>> model_conformance 总分: 0.8750  (满分 1.0)\n",
            "",
        )

    scorer = ModelConformanceCLIScorer(
        parent_repo=parent,
        python_executable="python",
        command_runner=fake_runner,
    )
    result = scorer.score(candidate.parents[1], tmp_path / "run", _task())

    assert result["status"] == "evaluated"
    assert result["candidate_score"] == 0.875
    assert "cast_in_place_box" in calls[0]
    assert "--is-continuous" in calls[0]
    persisted = json.loads((tmp_path / "run" / "model_score.json").read_text(encoding="utf-8"))
    assert persisted["scorer"] == "osis-auto-testconformance-cli"


def test_cli_scorer_reports_missing_script(tmp_path: Path):
    scorer = ModelConformanceCLIScorer(parent_repo=tmp_path / "missing")

    result = scorer.score(tmp_path / "candidate", tmp_path / "run", _task())

    assert result["status"] == "scorer_unavailable"
    assert result["candidate_score"] is None
