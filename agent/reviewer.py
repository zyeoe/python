"""Read-only reviewer sub-agent with an isolated context window."""

from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent.skills import estimate_tokens


REVIEWER_SYSTEM_PROMPT = """You are an isolated, read-only code reviewer.

You receive only a git diff and a list of changed files. You have no write tools.
Use this fixed checklist:
1. Did the change touch files outside the requested scope?
2. Were new dependencies added?
3. Is there any `except: pass`?
4. Do tests cover the new branch or behavior?
5. Is there debug residue such as `print(` or `TODO`?

Return only the final review. List concrete findings as Markdown bullets with file
and risk. If there are no blockers, say so explicitly.
"""


def reviewer_enabled() -> bool:
    value = os.getenv("ENABLE_REVIEWER", "1").strip().lower()
    if value in {"0", "false", "no", "off"}:
        return False
    if value in {"1", "true", "yes", "on"}:
        return True
    raise ValueError("ENABLE_REVIEWER must be 0/1, true/false, yes/no, on/off")


@dataclass
class ReviewResult:
    text: str
    issues_found: int
    main_context_delta: int
    subagent_tokens: int


class ReviewerAgent:
    """A reviewer that runs in a fresh model context and returns only its answer."""

    def __init__(
        self,
        *,
        workspace: str | Path = ".",
        model: str = "mock",
        client: Any,
    ) -> None:
        path = Path(workspace).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        self.workspace = path.resolve(strict=False)
        self.model = model
        self.client = client
        self.runs = 0
        self.issues_found = 0
        self.max_main_context_delta = 0
        self.subagent_tokens = 0

    def review(self, *, base: str | None = None, staged: bool = False) -> ReviewResult:
        diff, files = self._collect_diff(base=base, staged=staged)
        self.runs += 1
        if not diff.strip():
            text = "未发现可审查的 git diff。"
            delta = estimate_tokens(text)
            self.max_main_context_delta = max(self.max_main_context_delta, delta)
            self.subagent_tokens += estimate_tokens(REVIEWER_SYSTEM_PROMPT)
            return ReviewResult(text, 0, delta, estimate_tokens(REVIEWER_SYSTEM_PROMPT))

        user_content = (
            "Review this isolated diff. Do not modify files.\n\n"
            f"Changed files:\n{files or '(none)'}\n\n"
            f"Diff:\n{diff}"
        )
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": REVIEWER_SYSTEM_PROMPT},
                {"role": "user", "content": user_content},
            ],
        )
        message = response.choices[0].message
        text = self._message_text(getattr(message, "content", None)).strip()
        if not text:
            text = "Reviewer returned no final message."
        text = self._clip_to_token_budget(text, 500)
        issues = self._count_issues(text)
        delta = estimate_tokens(text)
        context_tokens = estimate_tokens(REVIEWER_SYSTEM_PROMPT + "\n" + user_content)

        self.issues_found += issues
        self.max_main_context_delta = max(self.max_main_context_delta, delta)
        self.subagent_tokens += context_tokens
        return ReviewResult(text, issues, delta, context_tokens)

    def _collect_diff(
        self,
        *,
        base: str | None,
        staged: bool,
    ) -> tuple[str, str]:
        args = ["git", "diff"]
        if staged:
            args.append("--staged")
        if base:
            args.append(base)
        diff_result = subprocess.run(
            [*args, "--"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        files_result = subprocess.run(
            [*args, "--name-only"],
            cwd=self.workspace,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
        )
        if diff_result.returncode != 0:
            return (
                f"git diff failed: {diff_result.stderr.strip()}",
                files_result.stdout.strip(),
            )
        return diff_result.stdout, files_result.stdout.strip()

    @staticmethod
    def _message_text(content: Any) -> str:
        if content is None:
            return ""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict) and item.get("type") == "text":
                    parts.append(str(item.get("text", "")))
                else:
                    text = getattr(item, "text", None)
                    if text:
                        parts.append(str(text))
            return "".join(parts)
        return str(content)

    @staticmethod
    def _count_issues(text: str) -> int:
        return sum(
            1
            for line in text.splitlines()
            if re.match(r"^\s*(?:[-*]|\d+[.)])\s+\S", line)
        )

    @staticmethod
    def _clip_to_token_budget(text: str, budget: int) -> str:
        if estimate_tokens(text) <= budget:
            return text
        marker = "\n\n[review truncated to fit main context budget]"
        low, high = 0, len(text)
        while low < high:
            middle = (low + high + 1) // 2
            if estimate_tokens(text[:middle] + marker) <= budget:
                low = middle
            else:
                high = middle - 1
        return text[:low].rstrip() + marker

    def review_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": "review_changes",
                "description": (
                    "Run an isolated read-only reviewer on the current git diff. "
                    "The reviewer returns only its final findings."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "base": {
                            "type": "string",
                            "description": "Optional git base revision.",
                        },
                        "staged": {
                            "type": "boolean",
                            "default": False,
                            "description": "Review staged changes instead of worktree changes.",
                        },
                    },
                    "additionalProperties": False,
                },
            },
        }

    def report(self) -> dict[str, int]:
        return {
            "runs": self.runs,
            "issues_found": self.issues_found,
            "main_context_delta": self.max_main_context_delta,
            "subagent_tokens": self.subagent_tokens,
        }


__all__ = [
    "ReviewerAgent",
    "ReviewResult",
    "REVIEWER_SYSTEM_PROMPT",
    "reviewer_enabled",
]
