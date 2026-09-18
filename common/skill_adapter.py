"""Safe, read-only access to an OSIS .agents/skills directory."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path


class SkillPathError(ValueError):
    """Raised when a requested skill path escapes the mounted root."""


@dataclass(frozen=True)
class SkillMeta:
    skill_id: str
    name: str
    description: str
    path: Path


@dataclass(frozen=True)
class CaseHit:
    skill_id: str
    path: Path
    preview: str


class SkillAdapter:
    def __init__(self, skills_dir: Path):
        self.root = Path(skills_dir).expanduser().resolve()
        if not self.root.is_dir():
            raise NotADirectoryError(self.root)

    def _inside(self, candidate: Path) -> Path:
        resolved = candidate.resolve()
        try:
            resolved.relative_to(self.root)
        except ValueError as exc:
            raise SkillPathError(f"path escapes skills root: {candidate}") from exc
        return resolved

    def _skill_dir(self, skill_id: str) -> Path:
        if not skill_id or Path(skill_id).name != skill_id:
            raise SkillPathError(f"invalid skill id: {skill_id!r}")
        skill_dir = self._inside(self.root / skill_id)
        if skill_dir.parent != self.root or not skill_dir.is_dir():
            raise SkillPathError(f"unknown skill: {skill_id}")
        return skill_dir

    @staticmethod
    def _frontmatter(text: str) -> dict[str, str]:
        lines = text.splitlines()
        if not lines or lines[0].strip() != "---":
            return {}
        values: dict[str, str] = {}
        for line in lines[1:]:
            if line.strip() == "---":
                break
            key, separator, value = line.partition(":")
            if separator:
                values[key.strip()] = value.strip().strip("\"'")
        return values

    def list_skills(self) -> list[SkillMeta]:
        result: list[SkillMeta] = []
        for path in sorted(self.root.iterdir(), key=lambda item: item.name.lower()):
            skill_file = path / "SKILL.md"
            if not path.is_dir() or not skill_file.is_file():
                continue
            text = skill_file.read_text(encoding="utf-8", errors="replace")
            frontmatter = self._frontmatter(text)
            result.append(
                SkillMeta(
                    skill_id=path.name,
                    name=frontmatter.get("name", path.name),
                    description=frontmatter.get("description", ""),
                    path=path,
                )
            )
        return result

    def skill_index(self) -> list[dict[str, str]]:
        """Return a stable, prompt-friendly index without full skill bodies."""
        return [
            {
                "skill_id": item.skill_id,
                "name": item.name,
                "description": item.description,
            }
            for item in self.list_skills()
        ]

    def visible_template_inventory(self) -> dict[str, list[str]]:
        """Return the reference-case (visible template) inventory of the snapshot.

        Derived from the **mounted** snapshot's ``references/templates/*``
        directories, so it exactly matches what ``read_skill_reference`` can
        actually serve. On a leak-free snapshot this is the visible(apply) set
        and never contains test templates. Used by the T1-T6 shared system
        prompt fragment so every framework starts from the same explicit list
        of reference cases instead of discovering them by search (which would
        add retrieval variance to the comparison).
        """

        inventory: dict[str, list[str]] = {}
        for path in sorted(self.root.iterdir(), key=lambda item: item.name.lower()):
            if not path.is_dir() or not (path / "SKILL.md").is_file():
                continue
            templates_dir = path / "references" / "templates"
            if not templates_dir.is_dir():
                continue
            names = sorted(child.name for child in templates_dir.iterdir() if child.is_dir())
            if names:
                inventory[path.name] = names
        return inventory

    def fixed_bundle_text(self) -> str:
        """Assemble the deterministic full-corpus bundle (all skill bodies)."""

        sections = [
            "# Fixed OSIS skill bundle",
            f"<!-- sha256: {self.skill_bundle_hash()} -->",
            "",
        ]
        for item in self.list_skills():
            sections.extend(
                [
                    f"## {item.skill_id}: {item.name}",
                    item.description,
                    "",
                    self.read_skill(item.skill_id).rstrip(),
                    "",
                ]
            )
        return "\n".join(sections)

    def write_fixed_bundle(self, output: Path) -> Path:
        """Write a deterministic one-shot bundle for T1-style agents."""
        output = Path(output)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(self.fixed_bundle_text(), encoding="utf-8")
        return output

    def read_skill(self, skill_id: str) -> str:
        skill_file = self._inside(self._skill_dir(skill_id) / "SKILL.md")
        if not skill_file.is_file():
            raise SkillPathError(f"skill has no SKILL.md: {skill_id}")
        return skill_file.read_text(encoding="utf-8")

    def read_reference(self, skill_id: str, relative_path: str) -> str:
        if not relative_path or Path(relative_path).is_absolute():
            raise SkillPathError("reference path must be relative")
        reference = self._inside(self._skill_dir(skill_id) / relative_path)
        if not reference.is_file():
            raise FileNotFoundError(reference)
        return reference.read_text(encoding="utf-8")

    def search_cases(self, query: str) -> list[CaseHit]:
        query = query.strip().lower()
        if not query:
            return []
        hits: list[CaseHit] = []
        for path in sorted(self.root.rglob("*.md"), key=lambda item: str(item).lower()):
            text = path.read_text(encoding="utf-8", errors="replace")
            if query not in text.lower():
                continue
            relative = path.relative_to(self.root)
            skill_id = relative.parts[0] if relative.parts else ""
            line = next(
                (line.strip() for line in text.splitlines() if query in line.lower()),
                "",
            )
            hits.append(CaseHit(skill_id=skill_id, path=path, preview=line[:240]))
        return hits

    def skill_bundle_hash(self) -> str:
        digest = hashlib.sha256()
        files = sorted(
            (path for path in self.root.rglob("*") if path.is_file()),
            key=lambda item: item.relative_to(self.root).as_posix(),
        )
        for path in files:
            relative = path.relative_to(self.root).as_posix().encode("utf-8")
            digest.update(relative)
            digest.update(b"\0")
            digest.update(path.read_bytes())
            digest.update(b"\0")
        return digest.hexdigest()
