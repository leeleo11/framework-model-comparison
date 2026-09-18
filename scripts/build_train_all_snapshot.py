"""Build the Train-All skills snapshot for the official framework comparison.

Main experiment ``checkpoints/train-all/skills`` = every non-template SKILL
file + the **train** templates listed in ``configs/datasets.yaml``, with all
**test** templates removed and every test template name / raw path scrubbed
from SKILL bodies (the current raw tree leaks e.g. rigid-frame test names
into ``osis-bridge-rigid-frame-box/SKILL.md`` and ``osis-engine/SKILL.md``).

Also emits ``manifest.json`` (per-file SHA-256 + per-bridge included counts)
so all six adapters can prove they mount byte-identical knowledge. The
existing ``checkpoints/epoch0/skills`` stays as the visible-only ablation.

Usage::

    python scripts/build_train_all_snapshot.py
        [--parent-repo <path-to-osis-skill-enhance-main>]
        [--output <snapshot-output-directory>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import yaml  # noqa: E402

from common.paths import resolve_args_parent_repo, resolve_opencode_dir  # noqa: E402
SKIP_DIRS = {"__pycache__", ".git"}
SKIP_FILE_GLOBS = ("*.pyc", "*.pyo", "*.bak", "*.tmp", ".DS_Store")
TEXT_SUFFIXES = {".md", ".py", ".json", ".txt", ".yaml", ".yml", ".cfg", ".ini"}
SCRUB_PLACEHOLDER = "【HIDDEN-CASE】"


def load_sets(config_path: Path) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    cfg = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    train: dict[str, set[str]] = {}
    test: dict[str, set[str]] = {}
    for bridge, slot in (cfg.get("bridges") or {}).items():
        train[bridge] = set(slot.get("train") or [])
        test[bridge] = set(slot.get("test") or [])
    return train, test


def _ignore(_dir: str, names: list[str]) -> set[str]:
    out: set[str] = set()
    for n in names:
        if n in SKIP_DIRS or any(Path(n).match(p) for p in SKIP_FILE_GLOBS):
            out.add(n)
    return out


def copy_skill_with_train_filter(
    src_skill: Path,
    dest_skill: Path,
    train_names: set[str],
) -> dict[str, int]:
    dest_skill.mkdir(parents=True, exist_ok=True)
    # 1) all non-`references` content
    for item in src_skill.iterdir():
        if item.name == "references":
            continue
        target = dest_skill / item.name
        if item.is_dir():
            shutil.copytree(item, target, dirs_exist_ok=True, ignore=_ignore)
        else:
            shutil.copy2(item, target)
    # 2) references content except templates
    refs_dir = src_skill / "references"
    if refs_dir.is_dir():
        dest_refs = dest_skill / "references"
        dest_refs.mkdir(exist_ok=True)
        for item in refs_dir.iterdir():
            if item.name == "templates":
                continue
            target = dest_refs / item.name
            if item.is_dir():
                shutil.copytree(item, target, dirs_exist_ok=True, ignore=_ignore)
            else:
                shutil.copy2(item, target)
    # 3) templates filtered to the train set
    src_tpl = refs_dir / "templates" if refs_dir.is_dir() else None
    dest_tpl = dest_skill / "references" / "templates"
    dest_tpl.mkdir(parents=True, exist_ok=True)
    kept = skipped = total = 0
    if src_tpl and src_tpl.is_dir():
        for tpl in src_tpl.iterdir():
            if not tpl.is_dir():
                continue
            total += 1
            if tpl.name not in train_names:
                skipped += 1
                continue
            shutil.copytree(tpl, dest_tpl / tpl.name, dirs_exist_ok=True, ignore=_ignore)
            kept += 1
    return {"templates_total": total, "templates_included": kept, "templates_skipped": skipped}


def _scrub_text(text: str, all_test_names: set[str]) -> str:
    """Neutralise every test template name and raw path referencing it."""

    for name in sorted(all_test_names, key=len, reverse=True):
        path_frags = (
            f"references/templates/{name}",
            f"templates/{name}",
        )
        for frag in path_frags:
            if frag in text:
                text = text.replace(frag, SCRUB_PLACEHOLDER)
        if name in text:
            text = text.replace(name, SCRUB_PLACEHOLDER)
    return text


def scrub_tree(root: Path, test_names: set[str]) -> dict[str, int]:
    """Walk the snapshot and neutralise test-template references in all text files.

    A test template name anywhere in the mounted knowledge base is a leak, so
    every text file is scrubbed against the **union** of all bridges' test
    names (cross-cutting skills such as ``osis-engine`` can reference a bridge
    test case).
    """

    per_file: dict[str, int] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        scrubbed = _scrub_text(text, test_names)
        count = scrubbed.count(SCRUB_PLACEHOLDER)
        if count:
            path.write_text(scrubbed, encoding="utf-8")
            per_file[path.relative_to(root).as_posix()] = count
    return per_file


def hash_tree(root: Path) -> tuple[dict[str, str], str]:
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        files[path.relative_to(root).as_posix()] = digest
    aggregate = hashlib.sha256(
        "\n".join(f"{k}:{v}" for k, v in sorted(files.items())).encode("utf-8")
    ).hexdigest()
    return files, aggregate


def copy_global_skill_layer(opencode_dir: Path, dest_skills: Path) -> list[str]:
    """Copy the production global skill layer verbatim into the snapshot.

    The packaged opencode install carries a second, app-level skill layer under
    ``.config/opencode/skills`` (e.g. ``karpathy-guidelines``).  Production
    OSIS-AI sessions always see it, so equal-knowledge mounting requires every
    architecture to receive it too.  These packages contain no bridge
    templates, so no train/test filtering or scrubbing applies.
    """

    source = opencode_dir / ".config" / "opencode" / "skills"
    copied: list[str] = []
    if not source.is_dir():
        return copied
    for skill in sorted(source.iterdir(), key=lambda item: item.name):
        if not skill.is_dir() or skill.name.startswith("."):
            continue
        dest = dest_skills / skill.name
        if dest.exists():
            # The domain layer owns name collisions; never overwrite it.
            continue
        shutil.copytree(skill, dest, ignore=_ignore)
        copied.append(skill.name)
    return copied


_KNOWLEDGE_SEARCH_SCRIPT = '''#!/usr/bin/env python
"""Query the OSIS/pyosis knowledge base (Weknora) from the command line.

Configuration comes from the environment:
    WEKNORA_BASE_URL  (default: https://knowledge.osisbim.com/api/v1)
    WEKNORA_API_KEY   (required)
    WEKNORA_KB_IDS    (comma-separated knowledge-base ids, required)

Usage:
    python weknora_search.py "create_rect 矩形截面 参数 签名"
"""

import json
import os
import sys
import urllib.request


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: weknora_search.py QUERY", file=sys.stderr)
        return 2
    base = os.environ.get("WEKNORA_BASE_URL", "https://knowledge.osisbim.com/api/v1").rstrip("/")
    key = os.environ.get("WEKNORA_API_KEY", "")
    ids = [x.strip() for x in os.environ.get("WEKNORA_KB_IDS", "").split(",") if x.strip()]
    if not key or not ids:
        print("TOOL_ERROR: WEKNORA_API_KEY / WEKNORA_KB_IDS 未配置", file=sys.stderr)
        return 1
    payload = json.dumps({"query": sys.argv[1][:2000], "knowledge_base_ids": ids}).encode("utf-8")
    request = urllib.request.Request(
        base + "/knowledge-search", data=payload,
        headers={
            "X-API-Key": key,
            "Content-Type": "application/json",
            "User-Agent": "osis-framework-benchmark/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        body = json.loads(response.read().decode("utf-8"))
    data = body.get("data")
    chunks = data if isinstance(data, list) else (data or {}).get("chunks") or []
    for index, chunk in enumerate(chunks[:5], 1):
        source = chunk.get("document_name") or chunk.get("knowledge_name") or ""
        print(f"--- [{index}] {source} ---")
        print((chunk.get("content") or "").strip()[:3000])
        print()
    if not chunks:
        print("知识库无命中")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
'''

_KNOWLEDGE_SEARCH_DOC = """# 知识库检索（Weknora）

查 pyosis API 用法/参数语义/错误修法时，除 SKILL 文档外还可检索公司知识库。

## 用法（bash，环境变量已注入时）

```bash
python <skill_dir>/scripts/weknora_search.py "create_rect 参数 签名"
python <skill_dir>/scripts/weknora_search.py "钢束形状 控制点 超出范围 修复"
```

- 输出为 top-5 检索片段；无命中时提示改用 `pyosis_doc.py` / `read_skill`。
- 配置读取环境变量 `WEKNORA_BASE_URL` / `WEKNORA_API_KEY` / `WEKNORA_KB_IDS`；未配置时脚本返回 `TOOL_ERROR`，此时退回 SKILL/`pyosis_doc` 路径。
- 与铁律 10 的关系：本脚本即「查知识库」的可用实现；知识库无命中或未配置时按守则降级。
"""


def write_knowledge_search_assets(dest_skills: Path) -> None:
    """Bundle a working knowledge-search CLI + usage doc for agents with bash.

    T6 (the native architecture) can execute scripts, so the snapshot ships a
    ready-to-run standard-library CLI plus a short usage page inside
    ``osis-python-helper``.  Credentials travel via the environment, never in
    these files.
    """

    helper_dir = dest_skills / "osis-python-helper"
    scripts_dir = helper_dir / "scripts"
    refs_dir = helper_dir / "references"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    refs_dir.mkdir(parents=True, exist_ok=True)
    (scripts_dir / "weknora_search.py").write_text(
        _KNOWLEDGE_SEARCH_SCRIPT, encoding="utf-8"
    )
    (refs_dir / "knowledge_search.md").write_text(
        _KNOWLEDGE_SEARCH_DOC, encoding="utf-8"
    )


def build_train_all(
    parent: Path,
    output: Path,
    *,
    opencode_dir: Path | None = None,
) -> dict[str, object]:
    src_root = parent / ".agents" / "skills"
    config_path = parent / "configs" / "datasets.yaml"
    if not src_root.is_dir():
        raise FileNotFoundError(src_root)
    if not config_path.is_file():
        raise FileNotFoundError(config_path)

    train_map, test_map = load_sets(config_path)
    dest_skills = output / "skills"
    if dest_skills.exists():
        shutil.rmtree(dest_skills)
    dest_skills.mkdir(parents=True)

    per_bridge: dict[str, dict[str, int]] = {}
    skills_copied = 0
    for skill in sorted(src_root.iterdir(), key=lambda item: item.name):
        if not skill.is_dir() or skill.name.startswith("."):
            continue
        counts = copy_skill_with_train_filter(skill, dest_skills / skill.name, train_map.get(skill.name, set()))
        per_bridge[skill.name] = counts
        skills_copied += 1

    resolved_opencode_dir = opencode_dir or resolve_opencode_dir()
    global_layer = copy_global_skill_layer(resolved_opencode_dir, dest_skills)

    # Machine-truth API reference: reflect every template-called manager
    # method from the installed pyosis into one markdown file inside the
    # snapshot.  All six architectures therefore receive the same generated
    # signatures through their existing knowledge channels.
    write_knowledge_search_assets(dest_skills)

    scrubbed = scrub_tree(dest_skills, set().union(*test_map.values()))
    files, aggregate = hash_tree(dest_skills)

    manifest = {
        "experiment": "train-all",
        "parent_repo_sha": None,
        "skills_copied": skills_copied,
        "global_skill_layer": global_layer,
        "opencode_dir": str(resolved_opencode_dir),
        "per_bridge_template_counts": per_bridge,
        "test_templates_scrubbed": len(scrubbed),
        "scrubbed_files": scrubbed,
        "file_count": len(files),
        "sha256": aggregate,
        "files": files,
    }
    (output / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Build the Train-All skills snapshot")
    parser.add_argument("--parent-repo", type=Path, default=None)
    parser.add_argument("--output", type=Path, default=None)
    parser.add_argument(
        "--opencode-dir",
        type=Path,
        default=None,
        help="Packaged OpenCode directory; defaults to OSIS_OPENCODE_DIR or PATH discovery",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    resolve_args_parent_repo(args)
    parent = args.parent_repo
    # Keep generated snapshots in the comparison project by default. The
    # parent repository remains the read-only infrastructure baseline.
    output = (args.output or PROJECT_ROOT / "checkpoints" / "train-all").resolve()
    manifest = build_train_all(parent, output, opencode_dir=args.opencode_dir)
    print(
        json.dumps(
            {
                "output": str(output),
                "skills_copied": manifest["skills_copied"],
                "file_count": manifest["file_count"],
                "template_included": sum(
                    v["templates_included"] for v in manifest["per_bridge_template_counts"].values()
                ),
                "template_skipped": sum(
                    v["templates_skipped"] for v in manifest["per_bridge_template_counts"].values()
                ),
                "scrubbed_files": manifest["test_templates_scrubbed"],
                "sha256": manifest["sha256"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
