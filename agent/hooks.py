"""Deterministic pre/post tool hooks."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


TRUTHY = {"1", "true", "yes", "on"}
FALSY = {"0", "false", "no", "off"}


def hooks_enabled() -> bool:
    value = os.getenv("ENABLE_HOOKS", "1").strip().lower()
    if value in FALSY:
        return False
    if value in TRUTHY:
        return True
    raise ValueError("ENABLE_HOOKS must be 0/1, true/false, yes/no, on/off")


@dataclass(frozen=True)
class HookDecision:
    allowed: bool = True
    reason: str = ""
    alternative: str = ""


class HookManager:
    """PreToolUse and PostToolUse hooks with auditable counters."""

    def __init__(
        self,
        *,
        enabled: bool | None = None,
        workspace: str | Path | None = None,
        test_command: str | None = None,
    ) -> None:
        self.enabled = hooks_enabled() if enabled is None else enabled
        raw_workspace = workspace or os.getenv("AGENT_WORKSPACE", ".")
        workspace_path = Path(raw_workspace).expanduser()
        if not workspace_path.is_absolute():
            workspace_path = Path.cwd() / workspace_path
        self.workspace = workspace_path.resolve(strict=False)

        default_command = subprocess.list2cmdline(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"]
        )
        self.test_command = test_command or os.getenv("AGENT_TEST_COMMAND") or default_command
        self.denials: list[dict[str, str]] = []
        self.runs = 0
        self.red = 0
        self.green = 0
        self.red_to_green = 0
        self.injected_lines = 0
        self._last_test_status: str | None = None

    def before_tool(self, name: str, arguments: dict[str, Any]) -> HookDecision:
        if not self.enabled:
            return HookDecision()

        if name in {"write", "edit"}:
            raw_path = arguments.get("path")
            if isinstance(raw_path, str):
                filename = Path(raw_path).name.lower()
                if (
                    filename == ".env"
                    or filename.startswith(".env.")
                    or filename.endswith((".pem", ".key"))
                ):
                    decision = HookDecision(
                        allowed=False,
                        reason="密钥不入库，用环境变量",
                        alternative="把值写入仓库外的 .env 或系统环境变量，不要修改密钥文件。",
                    )
                    self._record_denial(name, arguments, decision)
                    return decision

        if name == "bash":
            command = str(arguments.get("command", ""))
            decision = self._dangerous_command_decision(command)
            if decision is not None:
                self._record_denial(name, arguments, decision)
                return decision

        return HookDecision()

    def _dangerous_command_decision(self, command: str) -> HookDecision | None:
        normalized = " ".join(command.lower().split())
        rm_pattern = (
            r"(?:^|[;&|]\s*)rm\s+-(?:[a-z]*r[a-z]*f|[a-z]*f[a-z]*r)\b"
        )
        if re.search(rm_pattern, normalized):
            return HookDecision(
                allowed=False,
                reason="rm -rf 可能批量删除无法恢复的文件",
                alternative="用 git clean -nd 先查看会删什么，再只删除确认过的具体路径。",
            )
        if re.search(
            r"\bgit\s+push\b[^;&|]*\s(?:--force(?:-with-lease)?|-f)\b",
            normalized,
        ):
            return HookDecision(
                allowed=False,
                reason="force push 会覆盖远程历史",
                alternative="用 git revert 创建反向提交，再正常 push。",
            )
        if re.search(r"\bgit\s+reset\s+--hard\b", normalized):
            return HookDecision(
                allowed=False,
                reason="git reset --hard 会丢弃未提交修改",
                alternative="用 git restore --source=<commit> -- <path> 只恢复需要的文件。",
            )
        return None

    def _record_denial(
        self,
        tool: str,
        arguments: dict[str, Any],
        decision: HookDecision,
    ) -> None:
        record: dict[str, str] = {
            "tool": tool,
            "reason": decision.reason,
            "alternative": decision.alternative,
        }
        if tool == "bash":
            record["command"] = str(arguments.get("command", ""))
        else:
            record["path"] = str(arguments.get("path", ""))
        self.denials.append(record)

    def after_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        result: Any,
    ) -> dict[str, Any] | None:
        if not self.enabled or name not in {"write", "edit"}:
            return None
        if isinstance(result, dict) and "error" in result:
            return None
        return self.run_tests()

    def run_tests(self) -> dict[str, Any]:
        self.runs += 1
        try:
            completed = subprocess.run(
                self.test_command,
                cwd=self.workspace,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=int(os.getenv("AGENT_TEST_TIMEOUT", "120")),
                shell=True,
                check=False,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
            )
            output = (completed.stdout + "\n" + completed.stderr).strip()
            exit_code = completed.returncode
        except subprocess.TimeoutExpired as exc:
            output = f"Test command timed out: {exc}"
            exit_code = 124

        failed = exit_code != 0
        if failed:
            self.red += 1
            self._last_test_status = "red"
            summary_lines = self._summarize_failure(output)
        else:
            self.green += 1
            if self._last_test_status == "red":
                self.red_to_green += 1
            self._last_test_status = "green"
            summary_lines = [f"测试通过：{self._last_nonempty_line(output) or 'exit_code=0'}"]

        injected = min(3, len(summary_lines)) if failed else 0
        self.injected_lines = max(self.injected_lines, injected)
        return {
            "ran": True,
            "exit_code": exit_code,
            "status": "red" if failed else "green",
            "summary": "\n".join(summary_lines[:3]),
            "injected_lines": injected,
        }

    @staticmethod
    def _summarize_failure(output: str) -> list[str]:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        failure = next(
            (line for line in lines if line.startswith(("FAIL:", "ERROR:"))),
            "测试失败",
        )
        cause = next(
            (
                line
                for line in lines
                if "AssertionError:" in line
                or "Error:" in line
                or "Exception:" in line
            ),
            "未提供具体异常摘要",
        )
        return [f"测试红灯：{failure}", f"原因：{cause}", "修复后重新运行同一测试命令。"]

    @staticmethod
    def _last_nonempty_line(output: str) -> str:
        lines = [line.strip() for line in output.splitlines() if line.strip()]
        return lines[-1] if lines else ""

    def report(self) -> dict[str, int]:
        return {
            "runs": self.runs,
            "red": self.red,
            "green": self.green,
            "red_to_green": self.red_to_green,
            "injected_lines": self.injected_lines,
        }


__all__ = ["HookDecision", "HookManager", "hooks_enabled"]
