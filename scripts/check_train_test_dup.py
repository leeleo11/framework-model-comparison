"""Train/test near-duplicate audit for the OSIS modeling datasets.

Checks every test template against every train template (same bridge first,
then cross-bridge) for both exact duplicates (identical content signature)
and near duplicates (per-file similarity above a threshold). A train/test
template pair should be flagged when the same engineered project appears on
both sides under a different name — that would make a test score partially
memorised.

Usage::

    python scripts/check_train_test_dup.py
        [--parent-repo <path-to-osis-skill-enhance-main>]
        [--ratio 0.97] [--json reports/diagnostic/train_test_dup.json]
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

from common.paths import resolve_args_parent_repo  # noqa: E402

TRACKED_SUFFIXES = {".md", ".py", ".json", ".txt", ".yaml", ".yml"}


def file_hashes(template_dir: Path) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for path in template_dir.rglob("*"):
        if path.is_file():
            rel = path.relative_to(template_dir).as_posix()
            hashes[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def signature(hashes: dict[str, str]) -> str:
    return hashlib.sha256(
        "\n".join(f"{k}:{v}" for k, v in sorted(hashes.items())).encode("utf-8")
    ).hexdigest()


def shared_byte_fraction(a: Path, b: Path) -> dict[str, float]:
    """Fraction of a test template's bytes that are byte-identical to a train one.

    A "same project renamed" pair keeps almost every file byte-identical, so
    the shared-byte fraction approaches 1.0. Shared boilerplate (the tiny
    template-independent bootstrap files) stays well below 0.5, so it does not
    produce false positives.
    """

    rels_a = {p.relative_to(a).as_posix(): p for p in a.rglob("*") if p.is_file()}
    rels_b = {p.relative_to(b).as_posix(): p for p in b.rglob("*") if p.is_file()}
    total = sum(p.stat().st_size for p in rels_a.values())
    if total == 0:
        return {"fraction": 0.0, "shared_bytes": 0, "total_bytes": 0}
    shared = 0
    identical_files: list[str] = []
    for rel, fa in rels_a.items():
        fb = rels_b.get(rel)
        if fb is None or not fb.is_file() or not fa.is_file():
            continue
        if hashlib.sha256(fa.read_bytes()).hexdigest() == hashlib.sha256(fb.read_bytes()).hexdigest():
            size = fa.stat().st_size
            shared += size
            if size >= 512:  # ignore tiny shared boilerplate in the report
                identical_files.append(f"{rel} ({size}B)")
    return {
        "fraction": round(shared / total, 4),
        "shared_bytes": shared,
        "total_bytes": total,
        "identical_files": identical_files,
    }


def collect_cases(root: Path, split: str) -> list[tuple[str, str, Path, dict[str, str]]]:
    """Return (bridge, template_name, dir, hashes) for every template in a split."""
    cfg = yaml.safe_load((root / "configs" / "datasets.yaml").read_text(encoding="utf-8"))
    out: list[tuple[str, str, Path, dict[str, str]]] = []
    for bridge, slot in cfg["bridges"].items():
        names = set(slot.get(split) or [])
        tpl_root = root / ".agents" / "skills" / bridge / "references" / "templates"
        for name in sorted(names):
            tdir = tpl_root / name
            if not tdir.is_dir():
                continue
            out.append((bridge, name, tdir, file_hashes(tdir)))
    return out


def audit(parent: Path, ratio_threshold: float) -> dict[str, object]:
    tests = collect_cases(parent, "test")
    trains = collect_cases(parent, "train")

    flagged: list[dict[str, object]] = []
    for tb, tn, tdir, _th in sorted(tests):
        hits: list[tuple[str, str, dict[str, float]]] = []
        for sb, sn, sdir, _sh in trains:
            if signature(file_hashes(sdir)) == signature(file_hashes(tdir)):
                # whole-directory byte-identical -> exact duplicate regardless of name
                hits.append((sb, sn, {"fraction": 1.0, "exact": True}))
            else:
                probe = shared_byte_fraction(tdir, sdir)
                if probe["fraction"] >= ratio_threshold:
                    hits.append((sb, sn, probe))
        if not hits:
            continue
        hits.sort(key=lambda h: h[2]["fraction"], reverse=True)
        sb, sn, probe = hits[0]
        entry: dict[str, object] = {
            "test": f"{tb}/{tn}",
            "train": f"{sb}/{sn}",
            "shared_byte_fraction": probe["fraction"],
        }
        if probe.get("exact"):
            entry["note"] = "exact duplicate (whole directory byte-identical)"
        elif probe.get("identical_files"):
            entry["identical_files"] = probe["identical_files"]
        flagged.append(entry)

    return {
        "test_templates": len(tests),
        "train_templates": len(trains),
        "flagged_pairs": flagged,
        "threshold": ratio_threshold,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Train/test near-duplicate audit")
    parser.add_argument("--parent-repo", type=Path, default=None)
    parser.add_argument("--ratio", type=float, default=0.6)
    parser.add_argument("--json", type=Path, default=None)
    args = parser.parse_args(argv)
    resolve_args_parent_repo(args)
    report = audit(args.parent_repo, args.ratio)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
