"""Adversarial check: did any T6 session touch files outside its own sandbox?

Reads the isolated opencode permission log for each T6 run and flags any
accessed path that points at the answer (scorer_private), the raw skill tree
(.agents/skills, which holds the test templates), the test datasets, or
another run's directory.
"""

from __future__ import annotations

import glob
import re
from collections import Counter
from pathlib import Path

PATTERN = re.compile(r'evaluated permission=(\w+) pattern="?([^"]*?)"? action')
ABS_PATH = re.compile(r'[A-Za-z]:[\\/][^"\s,)]+')


def classify(path: str, bridge: str) -> str | None:
    low = path.lower().replace("/", "\\")
    if "scorer_private" in low:
        return "ANSWER(scorer_private)"
    if "datasets\\test" in low or "datasets/test" in path.lower():
        return "TEST DATASET"
    # raw skill tree = parent repo .agents/skills, NOT the sandbox copy
    if "\\.agents\\skills" in low and ".osisai_t6" not in low:
        return "RAW SKILL TREE (contains answers)"
    if "osis-framework-comparison\\runs" in low or "osis-framework-comparison/runs" in path:
        if f"{bridge}__" not in path:
            return "OTHER RUN DIR"
    return None


def main() -> None:
    logs = sorted(glob.glob("runs/full-sweep-2/*__T6__seed0/generated/.osisai_t6/xdg-data/opencode/log/opencode.log"))
    if not logs:
        print("no T6 session logs found (slim may have removed them)")
        return
    total_outside = 0
    for log in logs:
        bridge = Path(log).parts[2].split("__")[0]
        text = Path(log).read_text(encoding="utf-8", errors="replace")
        events = PATTERN.findall(text)
        counts = Counter(kind for kind, _ in events)

        paths: set[str] = set()
        for _, pattern in events:
            for hit in ABS_PATH.findall(pattern):
                paths.add(hit)

        outside: list[tuple[str, str]] = []
        for path in paths:
            tag = classify(path, bridge)
            if tag:
                outside.append((tag, path))

        total_outside += len(outside)
        print(f"  {bridge:20} events={dict(counts)} unique_paths={len(paths)} outside={len(outside)}")
        for tag, path in outside[:4]:
            print(f"      [!] {tag}: ...{path[-95:]}")

    print()
    print(f"越界访问总数: {total_outside}")
    if total_outside == 0:
        print("✅ 所有 T6 会话访问的路径均在本 run 的沙箱/候选工程范围内")


if __name__ == "__main__":
    main()
