"""OSIS 截图工具：切视角 + jpeg 命令截取当前视图。"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Mapping

from PIL import Image


RUN_ID_PATTERN = re.compile(r"^run_\d{8}_\d{6}$")

PRESETS = {
    "front": [
        ("plsm, 1", lambda e: e.run("plsm, 1")),
        ("/control,view,front", lambda e: e.run("/control,view,front")),
        # 关闭钢束符号，避免上一次钢束图残留（JTG_图_结构.mac 同款）。
        ("/PSYMB,td,0", lambda e: e.run("/PSYMB,td,0")),
        ("replot", lambda e: e.replot()),
    ],
    "side": [
        ("plsm, 1", lambda e: e.run("plsm, 1")),
        ("/control,view,right", lambda e: e.run("/control,view,right")),
        ("/PSYMB,td,0", lambda e: e.run("/PSYMB,td,0")),
        ("replot", lambda e: e.replot()),
    ],
    "tendon": [
        ("ClearResult", lambda e: e.run("ClearResult")),
        ("/control,view,front", lambda e: e.run("/control,view,front")),
        # plsm 是消隐开关：1=实体消隐，会遮挡截面内的钢束；0=取消消隐（线框），
        # 全部钢束可见。OSIS 原生钢束图（JTG_图_钢束.mac）同样用 plsm,0。
        ("plsm, 0", lambda e: e.run("plsm, 0")),
        # 关闭边界绘制、重置钢束选择集（原生宏同款步骤）。
        ("BCPlot,0", lambda e: e.run("BCPlot,0")),
        ("tdsel,none", lambda e: e.run("tdsel,none")),
        # 开启钢束符号绘制。OSIS 命令表（Command.ini）无 dispctrl/Plsm 类命令，
        # 钢束显示必须用 /PSYMB,td,1（与 JTG_图_钢束.mac 一致）。
        ("/PSYMB,td,1", lambda e: e.run("/PSYMB,td,1")),
        ("replot", lambda e: e.replot()),
    ],
    "load": [
        ("ClearResult", lambda e: e.run("ClearResult")),
        ("/control,view,front", lambda e: e.run("/control,view,front")),
        # 与钢束图一致取消消隐；荷载图开关用 LGPlot,Key（Command.ini），
        # dispctrl 命令在 OSIS 中不存在。
        ("plsm, 0", lambda e: e.run("plsm, 0")),
        ("ClearPlotLoad", lambda e: e.run("ClearPlotLoad")),
        ("LGPlot,1", lambda e: e.run("LGPlot,1")),
        ("/PSYMB,td,0", lambda e: e.run("/PSYMB,td,0")),
        ("replot", lambda e: e.replot()),
    ],
}


def _elapsed_ms(start_ns: int, end_ns: int) -> int:
    return max(0, (end_ns - start_ns) // 1_000_000)


class CaptureError(RuntimeError):
    """OSIS 内部截图失败。"""


def _absolute_path(path: str | Path, label: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        raise ValueError(f"{label} 必须使用绝对路径")
    return candidate.resolve()


def validate_project_run_dir(
    project_dir: str | Path,
    run_dir: str | Path,
    *,
    create: bool = True,
) -> Path:
    project = _absolute_path(project_dir, "Project directory")
    run = _absolute_path(run_dir, "Run directory")
    if not project.is_dir():
        raise ValueError(f"项目目录不存在: {project}")
    expected_parent = (project / "runs" / "osis-screenshot-check").resolve()
    if run.parent != expected_parent or not RUN_ID_PATTERN.fullmatch(run.name):
        raise ValueError("运行目录越界")
    if create:
        run.mkdir(parents=True, exist_ok=True)
    if not run.is_dir():
        raise ValueError(f"运行目录不存在: {run}")
    return run


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _load_engine() -> Any:
    try:
        from pyosis.core.engine import OSISEngine
        return OSISEngine()
    except Exception as error:
        raise CaptureError(f"无法加载 pyosis/OSISEngine: {error}") from error


def _project_from_manifest(run: Path) -> Path:
    manifest = run / "run-manifest.json"
    if not manifest.is_file():
        raise ValueError(f"运行清单缺失: {manifest}")
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(payload.get("project_dir"), str):
        raise ValueError("运行清单缺少 project_dir")
    return _absolute_path(payload["project_dir"], "Project directory")


def capture_auto(
    run_dir: str | Path,
    phase: str,
    *,
    preset: str = "front",
    engine: Any | None = None,
    timer_ns: Callable[[], int] = time.perf_counter_ns,
) -> dict[str, Any]:
    """设置视图并截取当前视图。"""

    if phase not in {"before", "after"}:
        raise ValueError("截图相位无效")
    if preset not in PRESETS:
        raise ValueError(f"不支持的视图预设: {preset}")
    started_ns = timer_ns()
    run = _absolute_path(run_dir, "Run directory")
    project = _project_from_manifest(run)
    run = validate_project_run_dir(project, run, create=False)
    target = run / f"{phase}-canvas.jpg"
    if target.exists():
        raise FileExistsError(f"截图已存在: {target}")
    active_engine = engine if engine is not None else _load_engine()
    try:
        for label, operation in PRESETS[preset]:
            operation(active_engine)
        active_engine.run(f"jpeg,{str(target.with_suffix(''))}")
    except Exception as error:
        raise CaptureError(f"OSIS 截图失败: {error}") from error
    if not target.is_file():
        raise CaptureError("OSIS 未产出 JPG 文件")
    with Image.open(target) as image:
        image.load()
        if image.format != "JPEG" or image.width <= 0 or image.height <= 0:
            raise CaptureError("截图不是有效 JPG")
        image_size = {"width": image.width, "height": image.height}
    result: dict[str, Any] = {
        "execution_status": "completed",
        "preset": preset,
        "screenshot": str(target),
        "screenshot_sha256": _sha256(target),
        "image_size": image_size,
        "timing": {"capture_elapsed_ms": _elapsed_ms(started_ns, timer_ns())},
    }
    metadata = run / f"{phase}-screenshot.json"
    _write_json_atomic(metadata, result)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="OSIS 截图")
    sub = parser.add_subparsers(dest="command", required=True)
    auto = sub.add_parser("capture-auto", help="设置视图并截取当前视图")
    auto.add_argument("--run-dir", required=True)
    auto.add_argument("--phase", required=True)
    auto.add_argument("--preset", default="front", choices=list(PRESETS.keys()),
                      help="视图预设（默认 front）")
    args = parser.parse_args()
    try:
        result = capture_auto(args.run_dir, args.phase, preset=args.preset)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Exception as error:
        print(json.dumps({"error": str(error)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
