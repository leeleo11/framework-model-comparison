"""Wrapper around the existing OSIS model-conformance command-line scorer."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from .modeling_pipeline import _locate_code_root
from .pyosis_adapter import resolve_python_executable
from .static_conformance import BRIDGE_TYPE_ALIASES
from .task_schema import TaskSpec


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]
_SCORE_PATTERNS = (
    re.compile(r"model_conformance\s*(?:总分|score)\s*[:：]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
    re.compile(r"(?:总分|candidate_score)\s*[:：=]\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE),
)


class ModelConformanceCLIScorer:
    """Call the repository-owned scorer without reimplementing its rules."""

    def __init__(
        self,
        *,
        parent_repo: Path,
        python_executable: str | Path | None = None,
        timeout_s: float = 180.0,
        command_runner: CommandRunner | None = None,
    ):
        self.parent_repo = Path(parent_repo).expanduser().resolve()
        self.python_executable = str(
            python_executable or resolve_python_executable(self.parent_repo)
        )
        self.timeout_s = float(timeout_s)
        self.command_runner = command_runner or subprocess.run

    @property
    def script(self) -> Path:
        return (
            self.parent_repo
            / ".agents"
            / "skills"
            / "osis-auto-testconformance"
            / "scripts"
            / "test_conformance.py"
        )

    @staticmethod
    def _write(run_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "model_score.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return result

    @staticmethod
    def _parse_score(text: str) -> float | None:
        for pattern in _SCORE_PATTERNS:
            match = pattern.search(text or "")
            if match:
                return float(match.group(1))
        return None

    def score(
        self,
        candidate_project: Path,
        run_dir: Path,
        task: TaskSpec,
        *,
        timeout_s: float | None = None,
    ) -> dict[str, Any]:
        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        report_path = run_dir / "model_score.md"
        if not self.script.is_file():
            return self._write(
                run_dir,
                {
                    "scorer": "osis-auto-testconformance-cli",
                    "status": "scorer_unavailable",
                    "candidate_score": None,
                    "reason": f"scorer script does not exist: {self.script}",
                },
            )

        code_root = _locate_code_root(Path(candidate_project))
        command = [
            self.python_executable,
            str(self.script),
            "--candidate-dir",
            str(code_root),
            "--bridge-type",
            BRIDGE_TYPE_ALIASES.get(task.bridge_type, task.bridge_type),
            "--output",
            str(report_path),
        ]
        metadata = task.metadata or {}
        if metadata.get("is_continuous") is not None:
            command.extend(
                ["--is-continuous", str(bool(metadata["is_continuous"])).lower()]
            )
        expected_l = metadata.get("expected_L")
        if expected_l is None and task.expected_fields:
            expected_l = task.expected_fields.get("expected_L")
        if expected_l is not None:
            command.extend(["--expected-L", str(expected_l)])
        if metadata.get("is_prestressed") is not None:
            command.extend(
                ["--is-prestressed", str(bool(metadata["is_prestressed"])).lower()]
            )

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        try:
            completed = self.command_runner(
                command,
                cwd=str(self.parent_repo),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=max(0.01, float(timeout_s or self.timeout_s)),
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            (run_dir / "model_score_stdout.log").write_text(
                str(getattr(exc, "stdout", "") or ""), encoding="utf-8"
            )
            (run_dir / "model_score_stderr.log").write_text(
                str(getattr(exc, "stderr", "") or ""), encoding="utf-8"
            )
            return self._write(
                run_dir,
                {
                    "scorer": "osis-auto-testconformance-cli",
                    "status": "timeout",
                    "candidate_score": None,
                    "reason": "model-conformance CLI timed out",
                },
            )

        (run_dir / "model_score_stdout.log").write_text(
            completed.stdout or "", encoding="utf-8"
        )
        (run_dir / "model_score_stderr.log").write_text(
            completed.stderr or "", encoding="utf-8"
        )
        report = report_path.read_text(encoding="utf-8") if report_path.is_file() else ""
        candidate_score = self._parse_score("\n".join((completed.stdout or "", report)))
        status = "evaluated" if candidate_score is not None and completed.returncode in (0, 1) else "scorer_error"
        result = {
            "scorer": "osis-auto-testconformance-cli",
            "status": status,
            "candidate_score": candidate_score,
            "returncode": completed.returncode,
            "report_path": str(report_path) if report_path.is_file() else None,
        }
        if status == "scorer_error":
            result["reason"] = "CLI did not return a parseable model-conformance score"
        return self._write(run_dir, result)
