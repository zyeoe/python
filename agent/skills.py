"""Progressive-disclosure skill catalog for the desktop agent."""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml


TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}
TRIGGER_RE = re.compile(r"[“\"']([^”\"']{2,40})[”\"']")


def skills_enabled() -> bool:
    value = os.getenv("ENABLE_SKILLS", "1").strip().lower()
    if value in FALSY:
        return False
    if value in TRUTHY:
        return True
    raise ValueError("ENABLE_SKILLS must be one of 0/1, true/false, yes/no, on/off")


def estimate_tokens(text: str) -> int:
    """Stable token estimate used only for report comparisons."""

    if not text:
        return 0
    return max(1, math.ceil(len(text.encode("utf-8")) / 4))


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold()).strip()


@dataclass(frozen=True)
class SkillCatalogEntry:
    name: str
    description: str
    directory: Path
    has_scripts: bool
    uses_mcp: bool


@dataclass(frozen=True)
class Skill:
    entry: SkillCatalogEntry
    body: str


class SkillManager:
    """Read only catalog metadata at startup; load skill bodies on demand."""

    def __init__(
        self,
        root: str | Path | None = None,
        *,
        enabled: bool | None = None,
    ) -> None:
        self.enabled = skills_enabled() if enabled is None else enabled
        configured_root = root or os.getenv("SKILLS_DIR", "skills")
        path = Path(configured_root).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        self.root = path.resolve(strict=False)
        self._entries = self._discover() if self.enabled else []
        self._by_name = {entry.name: entry for entry in self._entries}

    def _discover(self) -> list[SkillCatalogEntry]:
        if not self.root.is_dir():
            return []

        entries: list[SkillCatalogEntry] = []
        for skill_file in sorted(self.root.glob("*/SKILL.md")):
            directory = skill_file.parent
            metadata, _ = self._parse_document(skill_file)
            name = str(metadata.get("name", "")).strip()
            description = str(metadata.get("description", "")).strip()
            if not name or not description:
                raise ValueError(f"skill metadata requires name and description: {skill_file}")
            if name != directory.name:
                raise ValueError(
                    f"skill name must match directory name: {name!r} != {directory.name!r}"
                )
            if not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", name):
                raise ValueError(f"skill name must be kebab-case: {name}")
            entries.append(
                SkillCatalogEntry(
                    name=name,
                    description=description,
                    directory=directory.resolve(),
                    has_scripts=any((directory / "scripts").glob("**/*"))
                    if (directory / "scripts").is_dir()
                    else False,
                    uses_mcp=bool(metadata.get("uses_mcp", False)),
                )
            )
        return entries

    @staticmethod
    def _parse_document(path: Path) -> tuple[dict[str, Any], str]:
        text = path.read_text(encoding="utf-8")
        lines = text.splitlines(keepends=True)
        if not lines or lines[0].strip() != "---":
            raise ValueError(f"SKILL.md must start with YAML frontmatter: {path}")
        end_index = None
        for index in range(1, len(lines)):
            if lines[index].strip() == "---":
                end_index = index
                break
        if end_index is None:
            raise ValueError(f"SKILL.md frontmatter is not closed: {path}")
        metadata = yaml.safe_load("".join(lines[1:end_index])) or {}
        if not isinstance(metadata, dict):
            raise ValueError(f"SKILL.md frontmatter must be a mapping: {path}")
        body = "".join(lines[end_index + 1 :]).strip()
        return metadata, body

    @property
    def entries(self) -> list[SkillCatalogEntry]:
        return list(self._entries)

    def catalog_prompt(self) -> str:
        if not self.enabled or not self._entries:
            return ""
        lines = [
            "Available skills (catalog only; bodies are not loaded):",
            "Call use_skill(name) only when a description clearly matches the task.",
        ]
        lines.extend(f"- {entry.name}: {entry.description}" for entry in self._entries)
        return "\n".join(lines)

    def load(self, name: str) -> Skill:
        if not self.enabled:
            raise ValueError("skills are disabled by ENABLE_SKILLS=0")
        entry = self._by_name.get(name)
        if entry is None:
            available = ", ".join(sorted(self._by_name)) or "(none)"
            raise ValueError(f"unknown skill {name!r}; available: {available}")
        _, body = self._parse_document(entry.directory / "SKILL.md")
        return Skill(entry=entry, body=body)

    def full_text(self) -> str:
        return "\n\n".join(
            (entry.directory / "SKILL.md").read_text(encoding="utf-8")
            for entry in self._entries
        )

    def catalog_tokens(self) -> int:
        return estimate_tokens(self.catalog_prompt())

    def full_tokens(self) -> int:
        return estimate_tokens(self.full_text())

    def disclosure_ratio(self) -> float:
        catalog = self.catalog_tokens()
        if catalog <= 0:
            return 0.0
        return round(self.full_tokens() / catalog, 2)

    def triggers_for(self, name: str) -> list[str]:
        entry = self._by_name.get(name)
        if entry is None:
            return []
        return [match.strip() for match in TRIGGER_RE.findall(entry.description)]

    def match(self, query: str) -> str | None:
        """Deterministic description-trigger matcher used by the report trials."""

        normalized_query = _normalize(query)
        best_name = None
        best_score = 0
        for entry in self._entries:
            score = 0
            for trigger in self.triggers_for(entry.name):
                normalized_trigger = _normalize(trigger)
                if normalized_trigger and normalized_trigger in normalized_query:
                    score += len(normalized_trigger)
            if score > best_score:
                best_name = entry.name
                best_score = score
        return best_name

    def use_skill_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "use_skill",
                "description": (
                    "Load the full instructions for one skill from the catalog. "
                    "Call this only when the skill description matches the task."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "enum": [entry.name for entry in self._entries],
                            "description": "Skill name from the catalog.",
                        }
                    },
                    "required": ["name"],
                    "additionalProperties": False,
                },
            },
        }


__all__ = [
    "Skill",
    "SkillCatalogEntry",
    "SkillManager",
    "estimate_tokens",
    "skills_enabled",
]
