"""Return compile and PyOSIS build errors to the same generation run.

The observation is the error text from the step that just failed. A passing
compile is followed by a model build. Solving and official scoring stay in
the outer runner, which still runs once on the final candidate.
"""

from __future__ import annotations

import hashlib
import time
from pathlib import Path
from typing import Any, Callable

from common.modeling_pipeline import validate_project_layout
from common.pyosis_adapter import PyOSISAdapter, resolve_python_executable

_STDERR_TAIL = 12_000


def source_digest(candidate: Path) -> str:
    """Hash the canonical sources so an unchanged resubmit can stop."""

    layout = validate_project_layout(candidate)
    payload = "\n".join(
        [
            "missing:" + ",".join(layout.get("missing_files") or []),
            *[
                f"{name}:{digest}"
                for name, digest in sorted((layout.get("file_hashes") or {}).items())
            ],
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compile_observation(candidate: Path) -> str | None:
    root = Path(candidate)
    layout = validate_project_layout(root)
    if not layout.get("complete"):
        missing = "\n".join(layout.get("missing_files") or [])
        return f"candidate layout incomplete\n{missing}".rstrip()
    code_root = root / "py" if (root / "py").is_dir() else root
    lines: list[str] = []
    for path in sorted(code_root.rglob("*.py")):
        if any(part in {"__pycache__", ".osisai_t6"} for part in path.parts):
            continue
        try:
            compile(path.read_text(encoding="utf-8"), str(path), "exec")
        except (OSError, SyntaxError, ValueError) as exc:
            relative = path.relative_to(root).as_posix()
            lines.append(f"{relative}: {type(exc).__name__}: {exc}")
    if not lines:
        return None
    return "compile failed\n" + "\n".join(lines)


def _stderr_text(scratch: Path) -> str:
    chunks: list[str] = []
    for name in ("build_stderr.log", "project_create_stderr.log", "build_stdout.log"):
        path = scratch / name
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace").strip()
        if text:
            chunks.append(text)
    body = "\n".join(chunks).strip()
    if len(body) > _STDERR_TAIL:
        body = body[-_STDERR_TAIL:]
    return body


def observe_candidate(
    candidate: Path,
    scratch: Path,
    *,
    parent_repo: Path | None,
    deadline_monotonic: float | None = None,
    executor: Callable[..., dict[str, Any]] | None = None,
) -> str | None:
    """Return the failed step's text, or None when the model build succeeded.

    Compile failures do not call PyOSIS. ``executor`` replaces the real
    adapter in tests.
    """

    compile_error = _compile_observation(candidate)
    if compile_error is not None:
        return compile_error
    scratch.mkdir(parents=True, exist_ok=True)
    if executor is not None:
        status = executor(candidate, scratch, deadline_monotonic)
    else:
        python = resolve_python_executable(parent_repo)
        if not Path(python).is_file():
            raise FileNotFoundError(f"parent python is not available: {python}")
        remaining = None
        if deadline_monotonic is not None:
            remaining = max(0.01, deadline_monotonic - time.monotonic())
        adapter = PyOSISAdapter(
            python_executable=python,
            parent_repo=parent_repo,
            execution_enabled=True,
            timeout_s=remaining or 600.0,
        )
        status = adapter.execute(
            candidate,
            scratch,
            solve=False,
            timeout_s=remaining,
            deadline_monotonic=deadline_monotonic,
        )
    if status.get("model_created"):
        return None
    code = str(status.get("failure_code") or "pyosis_failed")
    detail = _stderr_text(scratch) or str(status.get("error") or status.get("reason") or "")
    return f"{code}\n{detail}".rstrip()


def run_feedback_loop(
    *,
    candidate: Path,
    scratch_root: Path,
    deadline_monotonic: float | None,
    observe: Callable[[Path], str | None],
    resume: Callable[[str], None],
) -> dict[str, Any]:
    """Keep returning the latest error until success, an unchanged submit, or timeout."""

    rounds: list[dict[str, Any]] = []
    previous: str | None = None
    while True:
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            return {"stop_reason": "task_timeout", "rounds": rounds}
        digest = source_digest(candidate)
        scratch = scratch_root / f"{len(rounds):03d}"
        observation = observe(scratch)
        rounds.append(
            {
                "source_digest": digest,
                "ok": observation is None,
                "observation": None if observation is None else observation[:2000],
            }
        )
        if observation is None:
            return {"stop_reason": "completed", "rounds": rounds}
        if previous is not None and previous == digest:
            return {"stop_reason": "unchanged", "rounds": rounds}
        previous = digest
        if deadline_monotonic is not None and time.monotonic() > deadline_monotonic:
            return {"stop_reason": "task_timeout", "rounds": rounds}
        resume(observation)
