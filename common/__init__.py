"""Framework-neutral contracts for OSIS modeling comparisons."""

from .adapters import ADAPTER_SPECS, get_adapter
from .evaluator import EvaluationResult, evaluate_run
from .runner import ExperimentRunner, RunSummary
from .skill_adapter import SkillAdapter
from .task_schema import TaskSpec, load_task

__all__ = [
    "ADAPTER_SPECS",
    "EvaluationResult",
    "ExperimentRunner",
    "RunSummary",
    "SkillAdapter",
    "TaskSpec",
    "evaluate_run",
    "get_adapter",
    "load_task",
]

