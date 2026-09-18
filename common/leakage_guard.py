"""Pre-run leakage gate for official dataset runs (final framework section 10).

Every official run must pass this gate before generation starts. Any failing
check marks the task ``leakage_guard_failed`` and excludes it from official
results. Checks:

1. snapshot_clean   - no test template name appears in any text file of the
                      mounted skills snapshot;
2. task_boundary    - the model-facing TaskSpec (which the system prompt,
                      input.json and adapter_request.json serialise) contains
                      no test template name, no raw ``.agents/skills`` path
                      and no ``references/templates`` fragment;
3. prestage_clean   - no staged base file originates from a test template OTHER
                      than the task's own case.  ``gen``/``edit`` are defined as
                      "modify this case's project", so their base files
                      necessarily come from that case's own template; handing a
                      task a DIFFERENT test case's files is what must not happen.
                      ``full`` stages nothing, so this is vacuous there;
4. no_exact_dup     - no test template is byte-identical (whole directory) to
                      any train template mounted in the snapshot.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

import yaml

TEXT_SUFFIXES = {".md", ".py", ".json", ".txt", ".yaml", ".yml", ".cfg", ".ini"}


@dataclass
class GuardReport:
    ok: bool
    checks: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "checks": self.checks}


def load_test_names(parent_repo: Path) -> dict[str, set[str]]:
    cfg = yaml.safe_load((Path(parent_repo) / "configs" / "datasets.yaml").read_text(encoding="utf-8"))
    return {
        bridge: set(slot.get("test") or [])
        for bridge, slot in (cfg.get("bridges") or {}).items()
    }


def scan_snapshot_for_test_names(skills_dir: Path, test_names: set[str]) -> list[str]:
    hits: list[str] = []
    root = Path(skills_dir)
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for name in test_names:
            if name in text:
                hits.append(f"{path.relative_to(root).as_posix()} contains {name}")
                break
    return hits


def task_boundary_issues(task_dict: dict, test_names: set[str]) -> list[str]:
    serialized = json.dumps(task_dict, ensure_ascii=False)
    issues: list[str] = []
    for name in sorted(test_names):
        if name in serialized:
            issues.append(f"task serialisation contains test template name: {name}")
    if ".agents/skills" in serialized:
        issues.append("task serialisation contains raw .agents/skills path")
    if "references/templates" in serialized:
        issues.append("task serialisation contains references/templates path")
    return issues


def prestage_issues(
    base_files: tuple[str, ...],
    bridge: str,
    test_names: set[str],
    own_cases: frozenset[str] | set[str] = frozenset(),
) -> list[str]:
    """Staged base files that come from another test case's template.

    ``gen``/``edit`` start from a case's own project by definition, so templates
    named by the task's own ``source``/``source_b`` are allowed.  What is still
    rejected is a task being handed base files from a *different* test case --
    that would seed one test case's run with another case's model.  ``full``
    stages no base files, so this check is vacuous for it, which is why it never
    fired before ``gen``/``edit`` ran.
    """

    issues: list[str] = []
    marker = "references/templates/"
    for raw in base_files:
        normalized = str(raw).replace("\\", "/")
        position = normalized.find(marker)
        if position < 0:
            continue
        rest = normalized[position + len(marker):]
        template_name = rest.split("/", 1)[0]
        if template_name in own_cases:
            continue
        if template_name in test_names:
            issues.append(f"staged base file comes from test template: {raw}")
    return issues


def _dir_signature(template_dir: Path) -> str:
    hashes: list[str] = []
    for path in sorted(template_dir.rglob("*")):
        if path.is_file():
            hashes.append(
                f"{path.relative_to(template_dir).as_posix()}:"
                f"{hashlib.sha256(path.read_bytes()).hexdigest()}"
            )
    return hashlib.sha256("\n".join(hashes).encode("utf-8")).hexdigest()


def exact_dup_issues(
    parent_repo: Path,
    bridge: str,
    test_names: set[str],
    snapshot_templates_dir: Path,
    train_names: set[str],
) -> list[str]:
    """Hard-fail when a test template is byte-identical to a mounted train one."""

    issues: list[str] = []
    test_sigs: dict[str, str] = {}
    raw_templates = Path(parent_repo) / ".agents" / "skills" / bridge / "references" / "templates"
    for name in test_names:
        tdir = raw_templates / name
        if tdir.is_dir():
            test_sigs[name] = _dir_signature(tdir)
    if not test_sigs:
        return issues
    mounted_sigs: dict[str, str] = {}
    for name in train_names:
        tdir = snapshot_templates_dir / name
        if tdir.is_dir():
            mounted_sigs[name] = _dir_signature(tdir)
    test_sig_set = set(test_sigs.values())
    for mounted_name, sig in mounted_sigs.items():
        if sig in test_sig_set:
            issues.append(
                f"mounted train template {mounted_name} is byte-identical to a test template"
            )
    return issues


def run_guard(
    *,
    parent_repo: Path,
    bridge: str,
    skills_dir: Path,
    task_dict: dict,
    base_files: tuple[str, ...] = (),
    own_cases: frozenset[str] | set[str] = frozenset(),
) -> GuardReport:
    """Run every leakage check. Returns a report; ok=False must block the run."""

    parent_repo = Path(parent_repo)
    test_map = load_test_names(parent_repo)
    test_names = set().union(*test_map.values()) if test_map else set()
    bridge_test = test_map.get(bridge, set())

    snapshot_hits = scan_snapshot_for_test_names(skills_dir, test_names)
    boundary = task_boundary_issues(task_dict, test_names)
    staging = prestage_issues(base_files, bridge, bridge_test, own_cases)

    train_map = yaml.safe_load(
        (parent_repo / "configs" / "datasets.yaml").read_text(encoding="utf-8")
    )["bridges"][bridge].get("train") or []
    dup = exact_dup_issues(
        parent_repo,
        bridge,
        bridge_test,
        Path(skills_dir) / bridge / "references" / "templates",
        set(train_map),
    )

    report = GuardReport(
        ok=not (snapshot_hits or boundary or staging or dup),
        checks={
            "snapshot_clean": snapshot_hits,
            "task_boundary": boundary,
            "prestage_clean": staging,
            "no_exact_dup": dup,
        },
    )
    return report
