"""AGENTS.md discovery and system-prompt injection."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


def agents_enabled() -> bool:
    value = os.getenv("ENABLE_AGENTS", "1").strip().lower()
    if value in FALSY:
        return False
    if value in TRUTHY:
        return True
    raise ValueError("ENABLE_AGENTS must be 0/1, true/false, yes/no, on/off")


@dataclass
class AgentsInstructions:
    global_path: Path | None = None
    project_path: Path | None = None
    global_text: str = ""
    project_text: str = ""
    layers: list[str] = field(default_factory=list)
    total_bytes: int = 0

    @property
    def found(self) -> bool:
        return bool(self.layers)

    @property
    def prompt(self) -> str:
        if not self.found:
            return ""
        parts = [
            "Follow this AGENTS.md instruction hierarchy. "
            "Project instructions refine global instructions."
        ]
        if self.global_text:
            parts.append(f"# Global AGENTS.md\n\n{self.global_text}")
        if self.project_text:
            parts.append(f"# Project AGENTS.md\n\n{self.project_text}")
        return "\n\n".join(parts)


def _find_project_agents(start_dir: Path) -> Path | None:
    current = start_dir.resolve(strict=False)
    while True:
        candidate = current / "AGENTS.md"
        if candidate.is_file():
            return candidate
        if current.parent == current:
            return None
        current = current.parent


def load_agents_instructions(
    start_dir: str | Path | None = None,
    *,
    global_path: str | Path | None = None,
    enabled: bool | None = None,
) -> AgentsInstructions:
    """Load user-global instructions first, then the nearest project file."""

    if enabled is None:
        enabled = agents_enabled()
    if not enabled:
        return AgentsInstructions()

    start = Path(start_dir or os.getenv("AGENT_WORKSPACE", ".")).expanduser()
    if not start.is_absolute():
        start = Path.cwd() / start
    start = start.resolve(strict=False)

    configured_global = global_path or os.getenv("AGENTS_GLOBAL_PATH")
    if configured_global:
        global_file = Path(configured_global).expanduser().resolve(strict=False)
    else:
        user_global = (Path.home() / "AGENTS.md").resolve(strict=False)
        portable_global = (
            Path(__file__).resolve().parent.parent / "AGENTS.global.md"
        ).resolve(strict=False)
        global_file = user_global if user_global.is_file() else portable_global
    project_file = _find_project_agents(start)

    result = AgentsInstructions()
    if global_file.is_file():
        result.global_path = global_file
        result.global_text = global_file.read_text(encoding="utf-8").strip()
        if result.global_text:
            result.layers.append("global")
            result.total_bytes += len(result.global_text.encode("utf-8"))

    if project_file is not None and project_file != global_file:
        project_text = project_file.read_text(encoding="utf-8").strip()
        if project_text:
            result.project_path = project_file
            result.project_text = project_text
            result.layers.append("project")
            result.total_bytes += len(project_text.encode("utf-8"))

    return result


__all__ = ["AgentsInstructions", "agents_enabled", "load_agents_instructions"]
