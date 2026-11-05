"""Collect a bounded git diff for the review-diff skill."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path


def _git(*args: str, cwd: Path) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    if result.returncode != 0:
        return result.stderr.strip() or f"git exited with {result.returncode}"
    return result.stdout


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staged", action="store_true", help="Review staged changes")
    parser.add_argument("--base", default=None, help="Optional git base revision")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--max-lines", type=int, default=400)
    args = parser.parse_args()

    root = Path(args.root).expanduser().resolve()
    diff_args = ["diff"]
    if args.staged:
        diff_args.append("--staged")
    if args.base:
        diff_args.append(args.base)

    stat = _git(*diff_args, "--stat", cwd=root)
    names = _git(*diff_args, "--name-status", cwd=root)
    diff = _git(*diff_args, "--", cwd=root)
    lines = diff.splitlines()
    truncated = len(lines) > args.max_lines
    if truncated:
        diff = "\n".join(lines[: args.max_lines])
        diff += f"\n\n[diff truncated: {len(lines) - args.max_lines} lines omitted]"

    print(
        json.dumps(
            {
                "root": str(root),
                "staged": args.staged,
                "base": args.base,
                "stat": stat,
                "name_status": names,
                "diff": diff,
                "truncated": truncated,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
