import json
from pathlib import Path

from scripts.run_modeling import build_parser


def test_modeling_cli_exposes_one_architecture_and_candidate_project():
    args = build_parser().parse_args(
        [
            "--skills-dir",
            "skills",
            "--task",
            "task.json",
            "--candidate-project",
            "candidate",
            "--architecture",
            "T6",
        ]
    )

    assert args.architecture == "T6"
    assert args.candidate_project == Path("candidate")
