"""Application-level inspection commands for incremental assignments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from agent.skills import SkillManager

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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Inspect incremental assignment outputs")
    subparsers = parser.add_subparsers(dest="command", required=True)
    skill_parser = subparsers.add_parser("skills", help="Generate skill_report.json")
    skill_parser.add_argument("--out", type=Path, help="Report output path")
    args = parser.parse_args(argv)

    if args.command == "skills":
        report = build_skill_report()
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
