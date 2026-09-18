"""Read-only live dashboard for one or more benchmark campaigns.

Examples::

    python scripts/progress_board.py --label formal-one-full-all-20260913
    python scripts/progress_board.py \
        --runs-root runs/formal-full-sweep-20260913 \
        --runs-root runs/formal-one-full-all-20260913

The generated HTML refreshes itself and never modifies experiment artifacts.
"""

from __future__ import annotations

import argparse
import html
import json
import re
import time
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
ARCHS = ("T1", "T2", "T3", "T4", "T5", "T6")
RUN_RE = re.compile(
    r"^(?P<bridge>.+)__(?P<form>full|gen|edit)__(?P<index>\d+)__"
    r"(?P<architecture>T[1-6])__seed(?P<seed>\d+)$"
)
BRIDGE_LABELS = {
    "cantilever_box": "悬浇连续梁",
    "conventional_box": "现浇箱梁",
    "hollow_slab": "空心板",
    "precast_small_box": "预制小箱梁",
    "precast_t_girder": "预制T梁",
    "rigid_frame": "连续刚构",
}


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _latest_mtime(path: Path) -> float:
    latest = 0.0
    for f in path.rglob("*"):
        try:
            if f.is_file():
                latest = max(latest, f.stat().st_mtime)
        except OSError:
            continue
    return latest


def _parse_run_dir(path: Path) -> dict | None:
    match = RUN_RE.match(path.name)
    if match is None:
        return None
    result = match.groupdict()
    result["index"] = int(result["index"])
    result["seed"] = int(result["seed"])
    return result


def _run_row(campaign_root: Path, path: Path) -> dict | None:
    parsed = _parse_run_dir(path)
    if parsed is None:
        return None
    ev = _read_json(path / "evaluation.json")
    backend = _read_json(path / "backend_status.json")
    compile_result = _read_json(path / "compile.json")
    layout = _read_json(path / "layout.json")
    arch = parsed["architecture"]
    generation = _read_json(
        path / "generated" / (
            "t6_generation.json" if arch == "T6" else f"{arch.lower()}_generation.json"
        )
    )
    score = _read_json(path / "model_score.json").get("candidate_score")
    if score is None:
        reference = _read_json(path / "reference_score.json")
        score = (reference.get("systems") or {}).get("model_conformance")

    if ev:
        state = "done"
    elif (path / "candidate_project").is_dir() or generation:
        state = "running"
    else:
        state = "starting"

    return {
        "campaign": campaign_root.name,
        "bridge": parsed["bridge"],
        "form": parsed["form"],
        "index": parsed["index"],
        "architecture": arch,
        "seed": parsed["seed"],
        "state": state,
        "quality": ev.get("quality_score"),
        "complete": ev.get("complete_success"),
        "reasons": ev.get("failure_reasons") or [],
        "created": backend.get("model_created"),
        "validated": backend.get("validation_passed"),
        "compiled": compile_result.get("passed"),
        "layout": layout.get("complete"),
        "score": score,
        "last_write": _latest_mtime(path),
        "run_dir": str(path),
    }


def _blocked_rows(campaign_root: Path) -> list[dict]:
    result = []
    blocked_root = campaign_root / "leakage_guard_failures"
    if not blocked_root.is_dir():
        return result
    for path in blocked_root.glob("*.json"):
        parsed = _parse_run_dir(path.with_suffix(""))
        if parsed is None:
            continue
        record = _read_json(path)
        checks = record.get("checks") or {}
        reason = next((str(v[0]) for v in checks.values() if v), "leakage_guard")
        result.append({
            "campaign": campaign_root.name,
            **parsed,
            "state": "blocked",
            "quality": None,
            "complete": False,
            "reasons": [reason[:160]],
            "score": None,
            "last_write": path.stat().st_mtime,
            "run_dir": str(path),
        })
    return result


def scan_roots(run_roots: list[Path], expected: dict | None = None) -> dict:
    """Aggregate all recognized run directories under one or more roots."""

    rows: list[dict] = []
    for root in run_roots:
        if not root.is_dir():
            continue
        rows.extend(_blocked_rows(root))
        for path in root.iterdir():
            if path.is_dir():
                row = _run_row(root, path)
                if row is not None:
                    rows.append(row)

    rows.sort(key=lambda row: (
        row["campaign"], row["bridge"], row["form"], row["index"],
        row["seed"], row["architecture"],
    ))
    groups: dict[tuple, dict] = {}
    for row in rows:
        key = (
            row["campaign"], row["bridge"], row["form"],
            row["index"], row["seed"],
        )
        group = groups.setdefault(key, {
            "campaign": row["campaign"],
            "bridge": row["bridge"],
            "form": row["form"],
            "index": row["index"],
            "seed": row["seed"],
            "cells": {},
        })
        group["cells"][row["architecture"]] = row

    expected_total = None
    if expected:
        if isinstance(expected.get("total"), int):
            expected_total = expected["total"]
        else:
            numeric_values = [value for value in expected.values() if isinstance(value, int)]
            if numeric_values:
                expected_total = sum(numeric_values)
    observed = len(rows)
    done = sum(row["state"] == "done" for row in rows)
    running = sum(row["state"] in {"running", "starting"} for row in rows)
    blocked = sum(row["state"] == "blocked" for row in rows)
    failed = sum(row["state"] == "done" and not row["complete"] for row in rows)
    total = max(observed, expected_total or 0)
    summary = {
        "total": total,
        "observed_runs": observed,
        "done": done,
        "running": running,
        "blocked": blocked,
        "failed": failed,
        "queued": max(total - observed, 0),
    }
    return {
        "rows": rows,
        "groups": list(groups.values()),
        "summary": summary,
        "ts": datetime.now().strftime("%H:%M:%S"),
    }


def scan(runs_dir: Path) -> dict:
    """Backward-compatible single-root scanner."""

    return scan_roots([runs_dir])


def _cell(row: dict | None) -> str:
    if row is None:
        return '<td class="queue">待排</td>'
    title = html.escape(" / ".join(str(item) for item in row["reasons"])[:220])
    if row["state"] == "blocked":
        return f'<td class="blocked" title="{title}">门禁</td>'
    if row["state"] in {"running", "starting"}:
        age = max(0, int(time.time() - row["last_write"])) if row["last_write"] else 0
        return f'<td class="running" title="{title}">⟳<br><span>{age}s</span></td>'
    score = row["quality"]
    score_text = f"{score:.1f}" if isinstance(score, (int, float)) else "—"
    cls = "success" if row["complete"] else "failed"
    mark = "✅" if row["complete"] else "❌"
    return f'<td class="{cls}" title="{title}">{mark}<br><span>{score_text}</span></td>'


def render(data: dict, title: str) -> str:
    summary = data["summary"]
    groups = data["groups"]
    total = summary["total"]
    finished = summary["done"] + summary["blocked"]
    percent = (finished / total * 100) if total else 0
    body = []
    for group in groups:
        task = f'{group["bridge"]}__{group["form"]}__{group["index"]:03d}'
        cells = "".join(_cell(group["cells"].get(arch)) for arch in ARCHS)
        body.append(
            "<tr>"
            f'<td class="task">{html.escape(group["campaign"])}</td>'
            f'<td>{html.escape(task)}</td>'
            f'<td>{html.escape(BRIDGE_LABELS.get(group["bridge"], group["bridge"]))}</td>'
            f'<td>{html.escape(group["form"])}</td>'
            f'<td>{group["index"]}</td>{cells}</tr>'
        )
    if not body:
        body.append('<tr><td colspan="11" class="empty">尚未发现运行目录</td></tr>')
    head = "".join(f"<th>{arch}</th>" for arch in ARCHS)
    return f"""<!doctype html><html lang="zh"><head><meta charset="utf-8">
<meta http-equiv="refresh" content="8">
<title>{html.escape(title)}</title>
<style>
body{{font:14px/1.5 -apple-system,"Segoe UI",sans-serif;margin:18px;background:#0f1115;color:#e6e6e6}}
h1{{font-size:19px;margin:0 0 4px}} .meta{{color:#8b93a1;font-size:12px;margin-bottom:14px}}
.cards{{display:flex;gap:10px;flex-wrap:wrap;margin:12px 0}}
.card{{background:#161a22;border:1px solid #232833;border-radius:6px;padding:8px 14px;min-width:92px}}
.card b{{display:block;font-size:20px}} .card span{{color:#9ca3af;font-size:12px}}
.bar{{height:10px;background:#1c2029;border-radius:5px;overflow:hidden;margin:10px 0 16px}}
.bar>div{{height:100%;background:linear-gradient(90deg,#2e7d32,#66bb6a);width:{percent:.1f}%}}
table{{border-collapse:collapse;width:100%}} th,td{{border:1px solid #232833;padding:7px 9px;text-align:center;white-space:nowrap}}
th{{background:#161a22;font-weight:600}} td.task{{text-align:left;color:#9ca3af;font-size:11px}}
td.success{{background:#14401a}} td.failed{{background:#3a1616}} td.running{{background:#14243a}}
td.queue{{background:#25252a;color:#8b93a1}} td.blocked{{background:#2a1a33;color:#c084fc}}
td.empty{{padding:24px;color:#8b93a1}} td span{{font-size:11px;color:#c0c5ce}}
.legend{{margin-top:14px;color:#8b93a1;font-size:12px}}
</style></head><body>
<h1>实验统一进度 · {html.escape(title)}</h1>
<div class="meta">已刷新 {data["ts"]} · 每 8 秒自动更新 · 只读</div>
<div class="cards">
<div class="card"><b>{summary["total"]}</b><span>总任务</span></div>
<div class="card"><b>{summary["observed_runs"]}</b><span>已提交</span></div>
<div class="card"><b>{summary["done"]}</b><span>已完成</span></div>
<div class="card"><b>{summary["running"]}</b><span>运行中</span></div>
<div class="card"><b>{summary["queued"]}</b><span>排队/未创建</span></div>
<div class="card"><b>{summary["failed"]}</b><span>完成但失败</span></div>
</div>
<div class="bar"><div></div></div>
<table><tr><th>批次</th><th>任务</th><th>桥型</th><th>形式</th><th>索引</th>{head}</tr>
{"".join(body)}
</table>
<div class="legend">✅ 完整成功　❌ 已结束但未达标　⟳ 运行中　待排 尚未创建运行目录　门禁 泄漏门禁拦截　数字为源码 quality 分</div>
</body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--label", help="single campaign label under runs/")
    parser.add_argument("--runs-root", action="append", type=Path,
                        help="run root; repeat to combine campaigns in one board")
    parser.add_argument("--expected-json", type=Path,
                        help="optional JSON object with a total task count")
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    if args.runs_root:
        roots = [
            path if path.is_absolute() else PROJECT_ROOT / path
            for path in args.runs_root
        ]
        title = "统一实验"
        out = PROJECT_ROOT / "tmp" / "progress-unified.html"
    elif args.label:
        roots = [PROJECT_ROOT / "runs" / args.label]
        title = args.label
        out = PROJECT_ROOT / "tmp" / f"progress-{args.label}.html"
    else:
        parser.error("必须提供 --label 或至少一个 --runs-root")

    expected = _read_json(args.expected_json) if args.expected_json else None
    out.parent.mkdir(parents=True, exist_ok=True)
    while True:
        try:
            data = scan_roots(roots, expected=expected)
            out.write_text(render(data, title), encoding="utf-8")
            print(
                f'[{data["ts"]}] 总任务 {data["summary"]["total"]} · '
                f'完成 {data["summary"]["done"]} · 运行 {data["summary"]["running"]} · '
                f'排队 {data["summary"]["queued"]} -> {out}',
                flush=True,
            )
        except Exception as exc:  # noqa: BLE001 - dashboard must keep running
            print(f"scan error: {exc}", flush=True)
        if args.once:
            return 0
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
