"""Real PyOSIS execution boundary for modeling-line experiments.

Framework adapters only produce a candidate project.  This module is the
single place that executes that project, optionally asks PyOSIS to solve it,
and probes the model state.  The subprocess boundary keeps framework code and
the comparison runner from sharing mutable Python state.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, Sequence

from .modeling_pipeline import _locate_code_root
from .task_schema import DEFAULT_TOTAL_TIMEOUT_S


CommandRunner = Callable[..., subprocess.CompletedProcess[str]]


# ``OSISEngine.model_summary()`` calls every manager in one dictionary literal.
# Some installed OSIS versions do not expose optional dynamic-analysis endpoints,
# so one unsupported manager can hide an otherwise valid model.  Probe managers
# independently and keep optional failures in stderr for diagnosis.
_MODEL_STATE_PROBE_CODE = """import dataclasses
import json
import os
import sys
from collections.abc import Mapping
from enum import Enum
from _0_engine import engine

managers = {
    "geometries": engine.geometry,
    "properties": engine.prop,
    "materials": engine.material,
    "sections": engine.section,
    "nodes": engine.node,
    "elements": engine.element,
    "boundaries": engine.boundary,
    "loadcases": engine.load,
    "tendon_props": engine.tendon.prop,
    "tendon_shapes": engine.tendon.shape,
    "lives": engine.live,
    "settlements": engine.settlement,
    "stabilities": engine.stability,
    "dynamic": engine.dynamic,
    "stages": engine.stage,
}
summary = {}
probe_errors = {}
for name, manager in managers.items():
    try:
        summary[name] = manager.count()
    except Exception as exc:
        summary[name] = None
        probe_errors[name] = f"{type(exc).__name__}: {exc}"


def _jsonable(value, *, depth=0, seen=None):
    # Convert PyOSIS dataclasses/enums/vectors to bounded plain JSON data.
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return value.name or value.value
    if depth > 5:
        return repr(value)
    marker = id(value)
    if marker in seen:
        return "<cycle>"
    seen.add(marker)
    if isinstance(value, Mapping):
        return {
            str(key): _jsonable(item, depth=depth + 1, seen=seen)
            for key, item in value.items()
            if not str(key).startswith("_")
        }
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _jsonable(getattr(value, field.name), depth=depth + 1, seen=seen)
            for field in dataclasses.fields(value)
            if not field.name.startswith("_") and "related" not in field.name
        }
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item, depth=depth + 1, seen=seen) for item in value]
    attrs = getattr(value, "__dict__", None)
    if isinstance(attrs, dict):
        return {
            str(key): _jsonable(item, depth=depth + 1, seen=seen)
            for key, item in attrs.items()
            if not str(key).startswith("_")
            and "related" not in str(key)
            and str(key) not in {"hash_value", "_hash"}
            and not callable(item)
        }
    return repr(value)


detail_managers = {
    "geometries": engine.geometry,
    "materials": engine.material,
    "sections": engine.section,
    "nodes": engine.node,
    "elements": engine.element,
    "boundaries": engine.boundary,
    "loadcases": engine.load,
    "tendon_props": engine.tendon.prop,
    "tendon_shapes": engine.tendon.shape,
    "stages": engine.stage,
}
try:
    detail_managers["element_groups"] = engine.element.group
except Exception as exc:
    probe_errors["element_groups"] = f"{type(exc).__name__}: {exc}"

details = {}
for name, manager in detail_managers.items():
    try:
        all_method = getattr(manager, "all")
        details[name] = [_jsonable(item) for item in (all_method() or [])]
    except Exception as exc:
        details[name] = []
        probe_errors[f"{name}.all"] = f"{type(exc).__name__}: {exc}"

snapshot_path = os.environ.get("OSIS_RUNTIME_MEASUREMENTS_PATH")
snapshot = {
    "schema_version": "osis-runtime-measurements-v1",
    "status": "available",
    "summary": summary,
    **details,
    "probe_errors": probe_errors,
}
if snapshot_path:
    try:
        with open(snapshot_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
            handle.write("\\n")
    except Exception as exc:
        probe_errors["snapshot_write"] = f"{type(exc).__name__}: {exc}"
if probe_errors:
    print("PYOSIS_PROBE_WARNINGS " + json.dumps(probe_errors, ensure_ascii=False), file=sys.stderr)
print(json.dumps(summary, ensure_ascii=False))
"""


def resolve_python_executable(parent_repo: Path | None = None) -> str:
    """Prefer the parent repository's environment, which contains ``pyosis``."""

    if parent_repo is not None:
        parent = Path(parent_repo).expanduser().resolve()
        candidates = (
            parent / ".venv" / "Scripts" / "python.exe",
            parent / ".venv" / "bin" / "python",
        )
        for candidate in candidates:
            if candidate.is_file():
                return str(candidate)
    return sys.executable


# OSIS is a stateful singleton: ``project.create`` switches the engine's
# current project, and the P4 build runs against that global project.  Two
# runs executing concurrently would therefore switch the project out from
# under each other and cross-contaminate.  Generation is independent per run
# and stays parallel; only execution + scoring serialise on this lock.
_OSIS_RUNTIME_LOCK = Path(os.environ.get("OSIS_RUNTIME_LOCK", "")) if os.environ.get("OSIS_RUNTIME_LOCK") else None


def _runtime_lock_path() -> Path:
    """Best-effort cross-process lock file beside the comparison project."""

    if _OSIS_RUNTIME_LOCK is not None:
        return _OSIS_RUNTIME_LOCK
    return Path(__file__).resolve().parents[1] / ".osis_runtime.lock"


@contextmanager
def osis_runtime_lock(timeout_s: float | None = None, poll_s: float = 2.0) -> Iterator[None]:
    """Serialise PyOSIS execution across processes.

    Uses an exclusive-create lock file: the first process to create it holds
    the engine; others poll.  A stale lock (holder died) is reclaimed after
    ``stale_after_s``.  On platforms without atomic exclusive create the lock
    degrades to a no-op rather than failing the run.
    """

    path = _runtime_lock_path()
    deadline = None if timeout_s is None else time.monotonic() + timeout_s
    stale_after_s = 40 * 60.0
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
        except FileExistsError:
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = 0.0
            if age > stale_after_s:
                try:
                    path.unlink()
                except OSError:
                    pass
                continue
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError(
                    f"timed out waiting for the OSIS runtime lock ({path})"
                )
            time.sleep(poll_s)
            continue
        except OSError:
            # Cannot create the lock (read-only FS, unsupported platform):
            # proceed unlocked rather than failing the whole run.
            yield
            return
        try:
            os.write(fd, str(os.getpid()).encode())
            os.close(fd)
            yield
        finally:
            try:
                path.unlink()
            except OSError:
                pass
        return


class PyOSISAdapter:
    """Execute a generated OSIS project through the installed PyOSIS runtime."""

    def __init__(
        self,
        *,
        python_executable: str | Path | None = None,
        parent_repo: Path | None = None,
        execution_enabled: bool = False,
        solver_version: str = "pyosis",
        timeout_s: float = DEFAULT_TOTAL_TIMEOUT_S,
        command_runner: CommandRunner | None = None,
    ):
        self.python_executable = str(
            python_executable or resolve_python_executable(parent_repo)
        )
        self.execution_enabled = execution_enabled
        self.solver_version = solver_version
        self.timeout_s = float(timeout_s)
        self.command_runner = command_runner or subprocess.run

    @staticmethod
    def _write_json(run_dir: Path, name: str, value: dict[str, Any]) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _write_text(run_dir: Path, name: str, value: Any) -> None:
        run_dir.mkdir(parents=True, exist_ok=True)
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        (run_dir / name).write_text(str(value or ""), encoding="utf-8")

    @staticmethod
    def _parse_summary(stdout: str) -> dict[str, Any] | None:
        for line in reversed((stdout or "").splitlines()):
            try:
                value = json.loads(line.strip())
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                return value
        return None

    @staticmethod
    def _has_model_objects(summary: dict[str, Any] | None) -> bool:
        if not summary:
            return False
        for value in summary.values():
            if isinstance(value, (int, float)) and value > 0:
                return True
            if isinstance(value, (list, tuple, dict)) and len(value) > 0:
                return True
        return False

    @staticmethod
    def _status(
        *,
        status: str,
        execution_enabled: bool,
        failure_code: str | None = None,
        model_created: bool = False,
        solver_converged: bool = False,
        validation_passed: bool = False,
        **extra: Any,
    ) -> dict[str, Any]:
        return {
            "adapter": "pyosis",
            "status": status,
            "execution_enabled": execution_enabled,
            "model_created": model_created,
            "solver_converged": solver_converged,
            "validation_passed": validation_passed,
            "failure_code": failure_code,
            **extra,
        }

    def write_not_configured(self, run_dir: Path, *, reason: str = "disabled") -> dict[str, Any]:
        status = self._status(
            status="not_configured",
            execution_enabled=False,
            failure_code="pyosis_disabled",
            reason=reason,
            solver_version=self.solver_version,
        )
        self._write_json(Path(run_dir), "backend_status.json", status)
        self._write_json(Path(run_dir), "build_status.json", status)
        self._write_json(
            Path(run_dir),
            "model_state.json",
            {"status": "not_available", "summary": None},
        )
        return status

    def create_model(self, run_dir: Path) -> dict[str, Any]:
        """Create the initial status artifact without executing user code."""

        if not self.execution_enabled:
            return self.write_not_configured(run_dir)
        status = self._status(
            status="ready",
            execution_enabled=True,
            solver_version=self.solver_version,
        )
        self._write_json(Path(run_dir), "backend_status.json", status)
        return status

    def _environment(self, code_root: Path) -> dict[str, str]:
        env = os.environ.copy()
        old_pythonpath = env.get("PYTHONPATH", "")
        prep_root = code_root / "prep"
        env["PYTHONPATH"] = os.pathsep.join(
            item for item in (str(prep_root), str(code_root), old_pythonpath) if item
        )
        env["PYTHONIOENCODING"] = "utf-8"
        return env

    def _run(
        self,
        command: Sequence[str],
        *,
        cwd: Path,
        timeout_s: float,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = self._environment(cwd)
        if env_overrides:
            env.update({str(key): str(value) for key, value in env_overrides.items()})
        return self.command_runner(
            list(command),
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=max(0.01, timeout_s),
            check=False,
        )

    def execute(
        self,
        candidate_project: Path,
        run_dir: Path,
        *,
        solve: bool = False,
        timeout_s: float | None = None,
        deadline_monotonic: float | None = None,
    ) -> dict[str, Any]:
        """Build, optionally solve, and inspect one candidate project.

        Holds the cross-process OSIS runtime lock: the engine has ONE global
        current project, so concurrent builds would cross-contaminate.  The
        wait is bounded by the caller's remaining task deadline so a queued
        run cannot outlive its budget while blocked here.
        """

        run_dir = Path(run_dir)
        run_dir.mkdir(parents=True, exist_ok=True)
        if not self.execution_enabled:
            result = self.write_not_configured(run_dir)
        else:
            wait_s: float | None = None
            if deadline_monotonic is not None:
                wait_s = max(0.0, deadline_monotonic - time.monotonic())
            with osis_runtime_lock(timeout_s=wait_s):
                result = self._execute_locked(
                    candidate_project, run_dir, solve=solve,
                    timeout_s=timeout_s, deadline_monotonic=deadline_monotonic,
                )

        # Preserve the caller's solve intent even when execution fails before
        # reaching the solver (for example, project creation or model build
        # failure).  The official evaluator treats a requested but
        # non-converged solve as a hard gate; dropping this field on an early
        # return would incorrectly leave the static score in place.
        if solve:
            result = dict(result)
            result.setdefault("solve_requested", True)
            result.setdefault("solver_converged", False)
            result.setdefault("solve_status", "not_run")
            self._write_json(run_dir, "backend_status.json", result)
            self._write_json(run_dir, "build_status.json", result)
        return result

    def _execute_locked(
        self,
        candidate_project: Path,
        run_dir: Path,
        *,
        solve: bool,
        timeout_s: float | None,
        deadline_monotonic: float | None,
    ) -> dict[str, Any]:
        code_root = _locate_code_root(Path(candidate_project))
        entrypoint = code_root / "prep" / "main.py"
        if not entrypoint.is_file():
            status = self._status(
                status="failed",
                execution_enabled=True,
                failure_code="missing_entrypoint",
                entrypoint=str(entrypoint),
                solver_version=self.solver_version,
            )
            self._write_json(run_dir, "backend_status.json", status)
            self._write_json(run_dir, "build_status.json", status)
            return status

        started = time.monotonic()
        budget = float(timeout_s if timeout_s is not None else self.timeout_s)
        if deadline_monotonic is not None:
            budget = min(budget, max(0.01, deadline_monotonic - started))
        # Each run must build into a FRESH OSIS project: the desktop's leftover
        # current project degrades (section creates silently fail after clear()
        # on a dirty project). Creating a fresh project per run makes builds
        # repeatable without restarting the OSIS service. A failed create is
        # fatal: continuing would execute against whichever stale project the
        # desktop/backend happened to have selected.
        fresh_project = run_dir / "osis_project.sis"
        try:
            create = self._run(
                [
                    self.python_executable, "-c",
                    "from pyosis.core.engine import OSISEngine; "
                    f"OSISEngine().project.create(101, {str(fresh_project)!r})",
                ],
                cwd=code_root,
                timeout_s=min(60.0, budget),
            )
        except subprocess.TimeoutExpired as exc:
            status = self._status(
                status="timeout",
                execution_enabled=True,
                failure_code="pyosis_project_create_timeout",
                elapsed_s=time.monotonic() - started,
                entrypoint=str(entrypoint),
                solver_version=self.solver_version,
            )
            self._write_text(run_dir, "project_create_stdout.log", getattr(exc, "stdout", ""))
            self._write_text(run_dir, "project_create_stderr.log", getattr(exc, "stderr", ""))
            self._write_json(run_dir, "backend_status.json", status)
            self._write_json(run_dir, "build_status.json", status)
            return status
        except Exception as exc:  # noqa: BLE001 - surface service failures
            status = self._status(
                status="failed",
                execution_enabled=True,
                failure_code="pyosis_project_create_failed",
                error=f"{type(exc).__name__}: {exc}",
                elapsed_s=time.monotonic() - started,
                entrypoint=str(entrypoint),
                solver_version=self.solver_version,
            )
            self._write_json(run_dir, "backend_status.json", status)
            self._write_json(run_dir, "build_status.json", status)
            return status
        self._write_text(run_dir, "project_create_stdout.log", create.stdout)
        self._write_text(run_dir, "project_create_stderr.log", create.stderr)
        if create.returncode != 0:
            status = self._status(
                status="failed",
                execution_enabled=True,
                failure_code="pyosis_project_create_failed",
                returncode=create.returncode,
                elapsed_s=time.monotonic() - started,
                entrypoint=str(entrypoint),
                solver_version=self.solver_version,
            )
            self._write_json(run_dir, "backend_status.json", status)
            self._write_json(run_dir, "build_status.json", status)
            return status
        build_command = [self.python_executable, str(entrypoint)]
        try:
            build = self._run(build_command, cwd=code_root, timeout_s=budget)
        except subprocess.TimeoutExpired as exc:
            elapsed = time.monotonic() - started
            status = self._status(
                status="timeout",
                execution_enabled=True,
                failure_code="pyosis_timeout",
                elapsed_s=elapsed,
                entrypoint=str(entrypoint),
                solver_version=self.solver_version,
            )
            self._write_text(run_dir, "build_stdout.log", getattr(exc, "stdout", ""))
            self._write_text(run_dir, "build_stderr.log", getattr(exc, "stderr", ""))
            self._write_json(run_dir, "backend_status.json", status)
            self._write_json(run_dir, "build_status.json", status)
            return status

        self._write_text(run_dir, "build_stdout.log", build.stdout)
        self._write_text(run_dir, "build_stderr.log", build.stderr)
        if build.returncode != 0:
            status = self._status(
                status="failed",
                execution_enabled=True,
                failure_code="pyosis_build_failed",
                model_created=False,
                elapsed_s=time.monotonic() - started,
                returncode=build.returncode,
                entrypoint=str(entrypoint),
                solver_version=self.solver_version,
            )
            self._write_json(run_dir, "backend_status.json", status)
            self._write_json(run_dir, "build_status.json", status)
            return status

        solver_converged = False
        solve_status = "not_requested"
        if solve:
            remaining = budget - (time.monotonic() - started)
            if deadline_monotonic is not None:
                remaining = min(remaining, deadline_monotonic - time.monotonic())
            if remaining <= 0:
                solve_status = "timeout"
            else:
                solve_command = [
                    self.python_executable,
                    "-c",
                    "from _0_engine import engine; result = engine.solve(); print(result if result is not None else 'converged')",
                ]
                try:
                    solved = self._run(solve_command, cwd=code_root, timeout_s=remaining)
                    self._write_text(run_dir, "solve_stdout.log", solved.stdout)
                    self._write_text(run_dir, "solve_stderr.log", solved.stderr)
                    solver_converged = solved.returncode == 0
                    solve_status = "succeeded" if solver_converged else "failed"
                except subprocess.TimeoutExpired as exc:
                    self._write_text(run_dir, "solve_stdout.log", getattr(exc, "stdout", ""))
                    self._write_text(run_dir, "solve_stderr.log", getattr(exc, "stderr", ""))
                    solve_status = "timeout"

        remaining = budget - (time.monotonic() - started)
        if deadline_monotonic is not None:
            remaining = min(remaining, deadline_monotonic - time.monotonic())
        summary: dict[str, Any] | None = None
        probe_status = "not_run"
        runtime_measurements_path = run_dir / "runtime_measurements.json"
        if remaining > 0:
            probe_command = [
                self.python_executable,
                "-c",
                _MODEL_STATE_PROBE_CODE,
            ]
            try:
                probe = self._run(
                    probe_command,
                    cwd=code_root,
                    timeout_s=remaining,
                    env_overrides={
                        "OSIS_RUNTIME_MEASUREMENTS_PATH": str(runtime_measurements_path),
                    },
                )
                self._write_text(run_dir, "state_stdout.log", probe.stdout)
                self._write_text(run_dir, "state_stderr.log", probe.stderr)
                summary = self._parse_summary(probe.stdout) if probe.returncode == 0 else None
                probe_status = "succeeded" if summary is not None else "failed"
            except subprocess.TimeoutExpired as exc:
                self._write_text(run_dir, "state_stdout.log", getattr(exc, "stdout", ""))
                self._write_text(run_dir, "state_stderr.log", getattr(exc, "stderr", ""))
                probe_status = "timeout"
        else:
            probe_status = "timeout"

        # The detailed snapshot is a secondary artifact.  Keep a valid,
        # versioned sidecar even when an older PyOSIS service or a test double
        # only supports the counts probe; this makes downstream post-processing
        # explicit instead of treating a missing file as a silent success.
        if not runtime_measurements_path.is_file():
            self._write_json(
                run_dir,
                "runtime_measurements.json",
                {
                    "schema_version": "osis-runtime-measurements-v1",
                    "status": "unavailable",
                    "summary": summary,
                    "nodes": [],
                    "elements": [],
                    "sections": [],
                    "materials": [],
                    "boundaries": [],
                    "loadcases": [],
                    "tendon_props": [],
                    "tendon_shapes": [],
                    "stages": [],
                    "element_groups": [],
                    "probe_errors": {"snapshot": "probe did not persist detailed records"},
                },
            )

        validation_passed = self._has_model_objects(summary)
        final_status = "succeeded"
        failure_code = None
        if solve and solve_status != "succeeded":
            final_status = "solve_timeout" if solve_status == "timeout" else "solve_failed"
            failure_code = "pyosis_solve_timeout" if solve_status == "timeout" else "pyosis_solve_failed"
        elif probe_status != "succeeded":
            final_status = "state_probe_timeout" if probe_status == "timeout" else "state_probe_failed"
            failure_code = "pyosis_state_timeout" if probe_status == "timeout" else "pyosis_state_unavailable"
        status = self._status(
            status=final_status,
            execution_enabled=True,
            failure_code=failure_code,
            model_created=True,
            solver_converged=solver_converged,
            validation_passed=validation_passed,
            elapsed_s=time.monotonic() - started,
            entrypoint=str(entrypoint),
            solver_version=self.solver_version,
            solve_requested=solve,
            solve_status=solve_status,
            state_probe_status=probe_status,
        )
        self._write_json(
            run_dir,
            "model_state.json",
            {
                "status": "available" if summary is not None else "unavailable",
                "summary": summary,
                "validation_passed": validation_passed,
                "runtime_measurements_path": "runtime_measurements.json",
            },
        )
        self._write_json(run_dir, "backend_status.json", status)
        self._write_json(run_dir, "build_status.json", status)
        return status

    def run_model_script(self, run_dir: Path, script: Path, timeout_s: float | None = None) -> dict[str, Any]:
        """Compatibility method for callers that already resolved an entrypoint."""

        if not self.execution_enabled:
            return self.write_not_configured(run_dir)
        script = Path(script).resolve()
        try:
            result = self._run(
                [self.python_executable, str(script)],
                cwd=script.parent,
                timeout_s=float(timeout_s or self.timeout_s),
            )
        except subprocess.TimeoutExpired:
            status = self._status(
                status="timeout",
                execution_enabled=True,
                failure_code="pyosis_timeout",
            )
            self._write_json(Path(run_dir), "backend_status.json", status)
            return status
        status = self._status(
            status="succeeded" if result.returncode == 0 else "failed",
            execution_enabled=True,
            failure_code=None if result.returncode == 0 else "pyosis_build_failed",
            model_created=result.returncode == 0,
            returncode=result.returncode,
        )
        self._write_json(Path(run_dir), "backend_status.json", status)
        return status

    def solve_model(self, run_dir: Path) -> dict[str, Any]:
        status_path = Path(run_dir) / "backend_status.json"
        if not status_path.is_file():
            return {"status": "not_run", "converged": False}
        status = json.loads(status_path.read_text(encoding="utf-8"))
        return {
            "status": status.get("solve_status", "not_requested"),
            "converged": bool(status.get("solver_converged", False)),
        }

    def inspect_model_state(self, run_dir: Path) -> dict[str, Any]:
        path = Path(run_dir) / "model_state.json"
        if not path.is_file():
            return {"status": "not_run", "model_state": None}
        value = json.loads(path.read_text(encoding="utf-8"))
        return {"status": value.get("status"), "model_state": value.get("summary")}

    def validate_model(self, run_dir: Path) -> dict[str, Any]:
        state = self.inspect_model_state(run_dir)
        passed = state.get("status") == "available" and self._has_model_objects(state.get("model_state"))
        return {"status": "passed" if passed else "failed", "passed": passed}

    def snapshot_artifacts(self, run_dir: Path) -> dict[str, Any]:
        return {
            "status": "snapshotted",
            "path": str(Path(run_dir).resolve()),
            "files": sorted(
                str(path.relative_to(run_dir)).replace("\\", "/")
                for path in Path(run_dir).rglob("*")
                if path.is_file()
            ),
        }


# The old name is kept for external scripts that imported the scaffold class.
FilesystemOSISAdapter = PyOSISAdapter
