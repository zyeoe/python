"""Application-level inspection commands for incremental assignments."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable, TypeVar

from agent.customization import load_agents_instructions
from agent.hooks import HookManager
from agent.mcp_client import MCPClientManager
from agent.mock_client import ScriptedMockClient, final_turn
from agent.reviewer import ReviewerAgent, reviewer_enabled
from agent.skills import SkillManager

T = TypeVar("T")

TRIALS = [
    {"query": "帮我跑一下测试", "expected": "run-tests"},
    {"query": "为什么 pytest 挂了", "expected": "run-tests"},
    {"query": "把这段 diff 审一遍", "expected": "review-diff"},
    {"query": "review diff 一下", "expected": "review-diff"},
    {"query": "看一下这个 issue", "expected": "fetch-issue"},
    {"query": "抓取 issue #42", "expected": "fetch-issue"},
    {"query": "今天天气怎么样", "expected": None},
    {"query": "帮我写一首短诗", "expected": None},
]


def build_skill_report() -> dict[str, Any]:
    manager = SkillManager()
    skills = [
        {
            "name": entry.name,
            "has_scripts": entry.has_scripts,
            "uses_mcp": entry.uses_mcp,
        }
        for entry in manager.entries
    ]

    trials = []
    false_positive = 0
    for trial in TRIALS:
        hit = manager.match(trial["query"])
        expected = trial["expected"]
        ok = hit == expected
        if expected is None and hit is not None:
            false_positive += 1
        trials.append(
            {
                "query": trial["query"],
                "expected": expected,
                "hit": hit,
                "ok": ok,
            }
        )

    catalog_tokens = manager.catalog_tokens()
    full_tokens = manager.full_tokens()
    ratio = manager.disclosure_ratio()
    checks = {
        "skill_count": len(skills) >= 3,
        "three_forms": (
            any(not item["has_scripts"] and not item["uses_mcp"] for item in skills)
            and any(item["has_scripts"] for item in skills)
            and any(item["uses_mcp"] for item in skills)
        ),
        "disclosure_ratio": ratio >= 5,
        "trial_count": len(trials) >= 6,
        "negative_trial": any(item["expected"] is None for item in trials),
        "false_positive": false_positive == 0,
        "all_trials": all(item["ok"] for item in trials),
    }
    return {
        "ok": all(checks.values()),
        "enabled": manager.enabled,

        "skills": skills,
        "catalog_tokens": catalog_tokens,
        "full_tokens": full_tokens,
        "disclosure_ratio": ratio,
        "trials": trials,
        "false_positive": false_positive,
        "checks": checks,
    }


def _build_agents_report(project_root: Path) -> dict[str, Any]:
    actual = load_agents_instructions(project_root)
    with tempfile.TemporaryDirectory(prefix="agents-md-test-") as temp_dir:
        root = Path(temp_dir)
        global_file = root / "home" / "AGENTS.md"
        project_file = root / "project" / "AGENTS.md"
        global_file.parent.mkdir(parents=True)
        project_file.parent.mkdir(parents=True)
        global_file.write_text("# Global rule\nglobal-only\n", encoding="utf-8")
        project_file.write_text("# Project rule\nproject-only\n", encoding="utf-8")
        merged = load_agents_instructions(
            project_file.parent,
            global_path=global_file,
            enabled=True,
        )
        order_ok = (
            merged.prompt.find("global-only") >= 0
            and merged.prompt.find("global-only") < merged.prompt.find("project-only")
        )
    return {
        "found": actual.found,
        "bytes": actual.total_bytes,
        "layers": actual.layers,
        "global_found": "global" in actual.layers,
        "project_found": "project" in actual.layers,
        "merge_test": {
            "layers": merged.layers,
            "order_ok": order_ok,
        },
    }


def _build_denied_report(project_root: Path) -> list[dict[str, str]]:
    manager = HookManager(enabled=True, workspace=project_root)
    manager.before_tool("write", {"path": ".env", "content": "SECRET=x"})
    manager.before_tool("bash", {"command": "rm -rf build/"})
    manager.before_tool("bash", {"command": "git push --force origin main"})
    manager.before_tool("bash", {"command": "git reset --hard HEAD~1"})
    return manager.denials


def _build_autotest_report(project_root: Path) -> dict[str, int]:
    with tempfile.TemporaryDirectory(prefix="hook-autotest-") as temp_dir:
        root = Path(temp_dir)
        tests = root / "tests"
        tests.mkdir()
        (tests / "__init__.py").write_text("", encoding="utf-8")
        (tests / "test_calc.py").write_text(
            "import unittest\n"
            "from calc import add\n\n"
            "class TestCalc(unittest.TestCase):\n"
            "    def test_add(self):\n"
            "        self.assertEqual(add(2, 3), 5)\n",
            encoding="utf-8",
        )
        command = subprocess.list2cmdline(
            [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-t", ".", "-q"]
        )
        manager = HookManager(
            enabled=True,
            workspace=root,
            test_command=command,
        )
        for _ in range(2):
            (root / "calc.py").write_text("def add(a, b):\n    return a - b\n", encoding="utf-8")
            red = manager.after_tool("write", {"path": str(root / "calc.py")}, {})
            assert red is not None and red["status"] == "red"
            (root / "calc.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
            green = manager.after_tool("write", {"path": str(root / "calc.py")}, {})
            assert green is not None and green["status"] == "green"
        return manager.report()


def _git(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )


def _build_reviewer_report() -> dict[str, int]:
    review_text = (
        "- app.py: debug `print(` remains in the diff.\n"
        "- tests/: the new branch has no test coverage.\n"
        "- app.py: `except: pass` hides failures.\n"
    )
    with tempfile.TemporaryDirectory(prefix="reviewer-test-") as temp_dir:
        root = Path(temp_dir)
        _git(root, "init")
        _git(root, "config", "user.email", "selftest@example.invalid")
        _git(root, "config", "user.name", "Selftest")
        (root / "app.py").write_text("def run():\n    return 1\n", encoding="utf-8")
        (root / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
        _git(root, "add", ".")
        _git(root, "commit", "-m", "baseline")
        (root / "app.py").write_text(
            "def run():\n"
            "    print('debug')\n"
            "    try:\n"
            "        return 1\n"
            "    except Exception:\n"
            "        pass\n",
            encoding="utf-8",
        )
        client = ScriptedMockClient(
            [final_turn(review_text), final_turn("No blocking issues found.")]
        )
        reviewer = ReviewerAgent(workspace=root, model="mock", client=client)
        reviewer.review()
        _git(root, "restore", "app.py")
        reviewer.review()
        return reviewer.report()


def _with_env(name: str, value: str, callback: Callable[[], T]) -> T:
    previous = os.environ.get(name)
    os.environ[name] = value
    try:
        return callback()
    finally:
        if previous is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = previous


def _build_switches_report(project_root: Path) -> dict[str, Any]:
    skills_off = _with_env("ENABLE_SKILLS", "0", SkillManager)
    hooks_off = _with_env(
        "ENABLE_HOOKS",
        "0",
        lambda: HookManager(enabled=None, workspace=project_root),
    )
    agents_off = _with_env(
        "ENABLE_AGENTS",
        "0",
        lambda: load_agents_instructions(project_root),
    )
    reviewer_off = _with_env("ENABLE_REVIEWER", "0", reviewer_enabled)
    mcp_off = _with_env(
        "ENABLE_MCP",
        "0",
        lambda: MCPClientManager(project_root / "mcp.json"),
    )
    return {
        "ENABLE_SKILLS": {
            "entries": len(skills_off.entries),
            "catalog_empty": not bool(skills_off.catalog_prompt()),
        },
        "ENABLE_HOOKS": {
            "denial_allowed": hooks_off.before_tool(
                "bash", {"command": "rm -rf build/"}
            ).allowed,
            "denials_recorded": len(hooks_off.denials),
        },
        "ENABLE_AGENTS": {
            "layers": agents_off.layers,
            "found": agents_off.found,
        },
        "ENABLE_REVIEWER": {"enabled": reviewer_off},
        "ENABLE_MCP": {
            "enabled": mcp_off.enabled,
            "servers": len(mcp_off.servers),
        },
    }


def build_custom_report() -> dict[str, Any]:
    project_root = Path(__file__).resolve().parent.parent
    agents_md = _build_agents_report(project_root)
    denied = _build_denied_report(project_root)
    autotest = _build_autotest_report(project_root)
    reviewer = _build_reviewer_report()
    switches = _build_switches_report(project_root)
    disabled = HookManager(enabled=False, workspace=project_root)
    hooks_off = disabled.before_tool("bash", {"command": "rm -rf build/"})

    checks = {
        "agents_md_found": agents_md["found"],
        "agents_md_size": 0 < agents_md["bytes"] <= 12_000,
        "agents_merge_order": agents_md["merge_test"]["order_ok"],
        "denied_count": len(denied) >= 2,
        "denied_reasons": all(
            item.get("reason") and item.get("alternative") for item in denied
        ),
        "autotest_red_to_green": autotest["red_to_green"] >= 1,
        "reviewer_context": reviewer["main_context_delta"] <= 500,
        "hooks_switch": hooks_off.allowed and not disabled.denials,
        "skills_switch": (
            switches["ENABLE_SKILLS"]["entries"] == 0
            and switches["ENABLE_SKILLS"]["catalog_empty"]
        ),
        "agents_switch": not switches["ENABLE_AGENTS"]["found"],
        "reviewer_switch": not switches["ENABLE_REVIEWER"]["enabled"],
        "mcp_switch": (
            not switches["ENABLE_MCP"]["enabled"]
            and switches["ENABLE_MCP"]["servers"] == 0
        ),
        "switches_ok": (
            switches["ENABLE_HOOKS"]["denial_allowed"]
            and switches["ENABLE_HOOKS"]["denials_recorded"] == 0
        ),
    }
    return {
        "ok": all(checks.values()),
        "agents_md": agents_md,
        "denied": denied,
        "autotest": autotest,
        "reviewer": reviewer,
        "switches": switches,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect incremental assignment outputs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    skill_parser = subparsers.add_parser("skills", help="Generate skill_report.json")
    skill_parser.add_argument("--out", type=Path, help="Report output path")
    custom_parser = subparsers.add_parser("custom", help="Generate custom_report.json")
    custom_parser.add_argument("--out", type=Path, help="Report output path")
    args = parser.parse_args(argv)

    if args.command == "skills":
        report = build_skill_report()
    elif args.command == "custom":
        report = build_custom_report()
    else:  # pragma: no cover - argparse enforces the available commands
        raise ValueError(f"unknown command: {args.command}")

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
