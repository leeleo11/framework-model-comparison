"""Where the OSIS skill repository (the "parent repo") lives.

This framework is designed to sit BESIDE ``osis-skill-enhance-main``, not
inside it.  ``对比实验框架设计文档.md`` names two sibling absolute paths
(实验目录 / 被测项目), and the scaffold plan states the constraint outright:
"Keep the experiment project independent from ``osis-skill-enhance-main``".

Because the two directories are siblings, there is no reliable directory
arithmetic that can find the parent repo from this file's position.  The
location is therefore supplied explicitly by ``--parent-repo`` or
``OSIS_PARENT_REPO`` (and may be kept in a local, uncommitted config file).

Resolution order:

1. an explicit value (``--parent-repo``)
2. ``OSIS_PARENT_REPO`` in the environment
3. ``configs/parent_repo.txt`` next to this package (first non-comment line)

When no explicit configuration is present, a conventional sibling named
``osis-skill-enhance-main`` is considered only if it passes the repository
marker check.  This keeps the common sibling layout convenient without
silently deriving a path from a nested checkout.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

#: Framework root (this repository).  Depth-stable: the whole tree moves as a
#: unit, so every ``parents[1]`` in ``scripts/`` agrees with this.
FRAMEWORK_ROOT = Path(__file__).resolve().parents[1]

ENV_VAR = "OSIS_PARENT_REPO"
CONFIG_FILENAME = "parent_repo.txt"
LOCAL_CONFIG_FILENAME = "parent_repo.local.txt"
RUN_ROOT_ENV = "OSIS_RUN_ROOT"
SKILLS_DIR_ENV = "OSIS_SKILLS_DIR"
OPENCODE_DIR_ENV = "OSIS_OPENCODE_DIR"

#: A directory is the OSIS skill repository when it carries the dataset index
#: the framework reads, the skill tree it mounts read-only, and the dataset
#: root the tasks are loaded from.
_REQUIRED_MARKERS = ("configs/datasets.yaml", ".agents/skills", "datasets")


class ParentRepoNotFound(RuntimeError):
    """Raised when no candidate resolves to the OSIS skill repository."""


def _looks_like_parent_repo(path: Path) -> bool:
    """Whether ``path`` carries the markers that identify the parent repo."""

    try:
        return all((path / marker).exists() for marker in _REQUIRED_MARKERS)
    except OSError:
        return False


def recorded_parent_repo() -> Path | None:
    """The path recorded in ``configs/parent_repo.txt``, or ``None``."""

    config_root = FRAMEWORK_ROOT / "configs"
    for filename in (LOCAL_CONFIG_FILENAME, CONFIG_FILENAME):
        path = config_root / filename
        try:
            raw = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("#"):
                if stripped.startswith("<") and stripped.endswith(">"):
                    break
                expanded = os.path.expandvars(stripped)
                candidate = Path(expanded).expanduser()
                if not candidate.is_absolute():
                    candidate = FRAMEWORK_ROOT / candidate
                return candidate.resolve()
    return None


def try_resolve_parent_repo(explicit: str | os.PathLike[str] | None = None) -> Path | None:
    """Best candidate for the parent repo, or ``None`` if there is none.

    An explicit value is trusted as given -- the caller named it, so a typo
    should surface as a missing-directory error against the path they typed,
    not be silently replaced by a fallback.  Ambient candidates (environment,
    recorded config) are validated against the parent-repo markers so a stale
    entry cannot quietly redirect a whole experiment.
    """

    if explicit is not None:
        return Path(explicit).expanduser()

    env_value = os.environ.get(ENV_VAR)
    if env_value:
        candidate = Path(os.path.expandvars(env_value)).expanduser()
        if _looks_like_parent_repo(candidate):
            return candidate

    recorded = recorded_parent_repo()
    if recorded is not None and _looks_like_parent_repo(recorded):
        return recorded

    # Portable convenience for the documented sibling layout.  This is a
    # directory-name convention, not a machine-specific absolute path, and it
    # is accepted only after the full marker check above.
    sibling = FRAMEWORK_ROOT.parent / "osis-skill-enhance-main"
    if _looks_like_parent_repo(sibling):
        return sibling.resolve()

    return None


def _resolve_configured_path(
    explicit: str | os.PathLike[str] | None,
    *,
    env_name: str,
    default: Path | None = None,
) -> Path | None:
    """Resolve a path without embedding a machine-specific absolute path.

    Explicit CLI values take precedence over environment variables. Relative
    defaults are interpreted from ``FRAMEWORK_ROOT`` so the whole comparison
    project can be moved as one unit.
    """

    raw: str | os.PathLike[str] | None = explicit
    if raw is None:
        raw = os.environ.get(env_name)
    if raw is None:
        candidate = default
    else:
        candidate = Path(os.path.expandvars(str(raw))).expanduser()
    if candidate is None:
        return None
    if not candidate.is_absolute():
        candidate = FRAMEWORK_ROOT / candidate
    return candidate.resolve()


def resolve_run_root(explicit: str | os.PathLike[str] | None = None) -> Path:
    return _resolve_configured_path(
        explicit, env_name=RUN_ROOT_ENV, default=FRAMEWORK_ROOT / "runs"
    )  # type: ignore[return-value]


def resolve_skills_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    return _resolve_configured_path(
        explicit,
        env_name=SKILLS_DIR_ENV,
        default=FRAMEWORK_ROOT / "checkpoints" / "train-all" / "skills",
    )  # type: ignore[return-value]


def resolve_opencode_dir(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Find the OpenCode installation without assuming a user's drive layout."""

    configured = _resolve_configured_path(
        explicit, env_name=OPENCODE_DIR_ENV, default=None
    )
    if configured is not None:
        return configured

    executable = shutil.which("opencode") or shutil.which("opencode.exe")
    if executable:
        return Path(executable).resolve().parent

    # A packaged deployment is commonly a sibling of the comparison project.
    # This is intentionally relative discovery, not a machine-specific path.
    for sibling in sorted(FRAMEWORK_ROOT.parent.glob("Rbin64*")):
        candidate = sibling / "opencode"
        if candidate.is_dir():
            return candidate.resolve()

    # Keep import-time behaviour deterministic; callers that need to launch
    # OpenCode will report a useful missing-install error for this relative
    # placeholder instead of silently using another machine's path.
    return Path("opencode").resolve()


def resolve_parent_repo(explicit: str | os.PathLike[str] | None = None) -> Path:
    """Like :func:`try_resolve_parent_repo`, but never returns ``None``."""

    resolved = try_resolve_parent_repo(explicit)
    if resolved is not None:
        return resolved

    raise ParentRepoNotFound(
        "Cannot locate the OSIS skill repository (the parent repo). "
        f"Pass --parent-repo, set {ENV_VAR}, or write the path into "
        f"configs/{LOCAL_CONFIG_FILENAME} or configs/{CONFIG_FILENAME}. Tried: "
        + ", ".join(
            filter(
                None,
                (
                    f"{ENV_VAR}={os.environ.get(ENV_VAR)!r}"
                    if os.environ.get(ENV_VAR)
                    else None,
                    f"recorded_config={recorded_parent_repo()!r}",
                ),
            )
        )
    )


def resolve_args_parent_repo(args: object, attr: str = "parent_repo") -> Path:
    """Resolve and store ``args.parent_repo`` in place.

    Keeps ``--parent-repo`` optional on the command line while guaranteeing
    that everything downstream sees a concrete path.
    """

    resolved = resolve_parent_repo(getattr(args, attr, None))
    setattr(args, attr, resolved)
    return resolved
