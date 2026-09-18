import json
import os
import subprocess
from pathlib import Path

from common.pyosis_adapter import PyOSISAdapter


class FakeCommandRunner:
    def __init__(self, *, timeout=False, probe_error=False, unsupported_summary=False):
        self.calls = []
        self.timeout = timeout
        self.probe_error = probe_error
        self.unsupported_summary = unsupported_summary

    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        command_text = " ".join(str(item) for item in command)
        # simulate a timeout on the actual model-build call (prep/main.py),
        # not on the non-fatal fresh-project setup step
        if self.timeout and "main.py" in command_text:
            raise subprocess.TimeoutExpired(command, kwargs.get("timeout", 1))
        if "model_summary" in command_text:
            if self.unsupported_summary:
                return subprocess.CompletedProcess(command, 1, "", "unsupported endpoint")
            if self.probe_error:
                return subprocess.CompletedProcess(command, 1, "", "probe failed")
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"geometries": 2, "nodes": 12, "elements": 8}),
                "",
            )
        if "engine.solve" in command_text:
            return subprocess.CompletedProcess(command, 0, "converged", "")
        if "_0_engine" in command_text and "json.dumps" in command_text:
            return subprocess.CompletedProcess(
                command,
                0,
                json.dumps({"nodes": 12, "elements": 8, "dynamic": None}),
                "",
            )
        return subprocess.CompletedProcess(command, 0, "model built", "")


class ProjectCreateFailureRunner(FakeCommandRunner):
    def __call__(self, command, **kwargs):
        self.calls.append((list(command), kwargs))
        command_text = " ".join(str(item) for item in command)
        if "project.create" in command_text:
            return subprocess.CompletedProcess(command, 1, "", "service unavailable")
        return super().__call__(command, **kwargs)


class DetailedProbeRunner(FakeCommandRunner):
    """Fake PyOSIS probe that writes the detailed runtime snapshot."""

    def __call__(self, command, **kwargs):
        result = super().__call__(command, **kwargs)
        command_text = " ".join(str(item) for item in command)
        output_path = kwargs.get("env", {}).get("OSIS_RUNTIME_MEASUREMENTS_PATH")
        if output_path and "json.dumps" in command_text and "_0_engine" in command_text:
            Path(output_path).write_text(
                json.dumps(
                    {
                        "schema_version": "osis-runtime-measurements-v1",
                        "status": "available",
                        "summary": {"nodes": 2, "elements": 1, "sections": 1},
                        "nodes": [{"no": 1, "x": 0.0, "y": 0.0, "z": 0.0}],
                        "elements": [],
                        "sections": [{"no": 1, "name": "girder", "height": 3.2}],
                        "materials": [],
                        "boundaries": [],
                        "loadcases": [],
                        "tendon_props": [],
                        "tendon_shapes": [],
                        "stages": [],
                        "element_groups": [],
                        "probe_errors": {},
                    }
                ),
                encoding="utf-8",
            )
        return result


def _candidate(tmp_path: Path) -> Path:
    code_root = tmp_path / "candidate" / "py" / "prep"
    code_root.mkdir(parents=True)
    (code_root / "main.py").write_text("print('build')\n", encoding="utf-8")
    (code_root / "_0_engine.py").write_text("engine = object()\n", encoding="utf-8")
    return code_root.parents[1]


def test_pyosis_adapter_executes_entrypoint_and_writes_model_state(tmp_path: Path):
    runner = FakeCommandRunner()
    adapter = PyOSISAdapter(
        python_executable="python",
        execution_enabled=True,
        command_runner=runner,
    )

    result = adapter.execute(
        _candidate(tmp_path),
        tmp_path / "run",
        solve=True,
    )

    assert result["status"] == "succeeded"
    assert result["model_created"] is True
    assert result["solver_converged"] is True
    assert result["validation_passed"] is True
    assert (tmp_path / "run" / "build_status.json").exists()
    assert (tmp_path / "run" / "model_state.json").exists()
    assert any(
        "_0_engine" in " ".join(call[0]) and "json.dumps" in " ".join(call[0])
        for call in runner.calls
    )


def test_pyosis_adapter_persists_detailed_runtime_measurements(tmp_path: Path):
    runner = DetailedProbeRunner()
    adapter = PyOSISAdapter(
        python_executable="python",
        execution_enabled=True,
        command_runner=runner,
    )

    result = adapter.execute(_candidate(tmp_path), tmp_path / "run")

    assert result["status"] == "succeeded"
    measurements_path = tmp_path / "run" / "runtime_measurements.json"
    assert measurements_path.exists()
    measurements = json.loads(measurements_path.read_text(encoding="utf-8"))
    assert measurements["schema_version"] == "osis-runtime-measurements-v1"
    assert measurements["sections"][0]["height"] == 3.2
    # Counts remain in the backward-compatible model_state artifact.
    state = json.loads((tmp_path / "run" / "model_state.json").read_text(encoding="utf-8"))
    assert state["summary"]["nodes"] == 12


def test_pyosis_adapter_marks_runtime_measurements_unavailable_when_probe_has_no_snapshot(
    tmp_path: Path,
):
    runner = FakeCommandRunner()
    adapter = PyOSISAdapter(
        python_executable="python",
        execution_enabled=True,
        command_runner=runner,
    )

    result = adapter.execute(_candidate(tmp_path), tmp_path / "run")

    assert result["status"] == "succeeded"
    sidecar = json.loads(
        (tmp_path / "run" / "runtime_measurements.json").read_text(encoding="utf-8")
    )
    assert sidecar["status"] == "unavailable"
    assert sidecar["schema_version"] == "osis-runtime-measurements-v1"


def test_pyosis_adapter_exposes_prep_modules_to_all_subprocesses(tmp_path: Path):
    runner = FakeCommandRunner()
    adapter = PyOSISAdapter(
        python_executable="python",
        execution_enabled=True,
        command_runner=runner,
    )

    adapter.execute(_candidate(tmp_path), tmp_path / "run")

    prep_root = str((tmp_path / "candidate" / "py" / "prep").resolve())
    assert runner.calls
    for _, kwargs in runner.calls:
        pythonpath = kwargs["env"]["PYTHONPATH"].split(os.pathsep)
        assert prep_root in pythonpath


def test_pyosis_adapter_tolerates_optional_summary_endpoint_failure(tmp_path: Path):
    runner = FakeCommandRunner(unsupported_summary=True)
    adapter = PyOSISAdapter(
        python_executable="python",
        execution_enabled=True,
        command_runner=runner,
    )

    result = adapter.execute(_candidate(tmp_path), tmp_path / "run")

    assert result["status"] == "succeeded"
    assert result["model_created"] is True
    assert result["validation_passed"] is True


def test_pyosis_adapter_records_timeout_without_false_success(tmp_path: Path):
    adapter = PyOSISAdapter(
        python_executable="python",
        execution_enabled=True,
        command_runner=FakeCommandRunner(timeout=True),
        timeout_s=1,
    )

    result = adapter.execute(_candidate(tmp_path), tmp_path / "run")

    assert result["status"] == "timeout"
    assert result["model_created"] is False
    assert result["failure_code"] == "pyosis_timeout"
    assert json.loads((tmp_path / "run" / "backend_status.json").read_text(encoding="utf-8"))["status"] == "timeout"


def test_pyosis_adapter_does_not_build_against_stale_project_when_create_fails(tmp_path: Path):
    runner = ProjectCreateFailureRunner()
    adapter = PyOSISAdapter(
        python_executable="python",
        execution_enabled=True,
        command_runner=runner,
    )

    result = adapter.execute(_candidate(tmp_path), tmp_path / "run")

    assert result["status"] == "failed"
    assert result["model_created"] is False
    assert result["failure_code"] == "pyosis_project_create_failed"
    assert not any("main.py" in " ".join(call[0]) for call in runner.calls)


def test_pyosis_adapter_disabled_is_explicit(tmp_path: Path):
    adapter = PyOSISAdapter(execution_enabled=False)

    result = adapter.execute(_candidate(tmp_path), tmp_path / "run")

    assert result["status"] == "not_configured"
    assert result["model_created"] is False
    assert result["failure_code"] == "pyosis_disabled"
