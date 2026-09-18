"""Inspect T6 `external_directory` permission events (paths outside the project).

opencode asks for an extra permission when a tool touches a directory outside
the session's project root.  Any such request is exactly where an
out-of-scope access would show up, so enumerate them all.
"""

from __future__ import annotations

import glob
import re
from pathlib import Path

BACKSLASH = chr(92)
PATTERN = re.compile(r'evaluated permission=external_directory pattern="?([^"]*?)"? action')


def main() -> None:
    logs = sorted(glob.glob("runs/full-sweep-2/*__T6__seed0/generated/.osisai_t6/xdg-data/opencode/log/opencode.log"))
    grand = 0
    for log in logs:
        bridge = Path(log).parts[2].split("__")[0]
        events = PATTERN.findall(Path(log).read_text(encoding="utf-8", errors="replace"))
        if not events:
            print(f"  {bridge:20} 0 external_directory events")
            continue
        grand += len(events)
        seen: set[str] = set()
        print(f"  {bridge:20} {len(events)} events — unique tails:")
        for raw in events:
            tail = raw.replace(BACKSLASH * 2, BACKSLASH)[-105:]
            if tail in seen:
                continue
            seen.add(tail)
            print(f"      {tail}")
    print()
    print(f"external_directory 事件总数: {grand}")


if __name__ == "__main__":
    main()
