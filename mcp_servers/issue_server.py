"""Local MCP server used by the fetch-issue skill."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from mcp.server.mcpserver import MCPServer


server = MCPServer(
    "desktop-issue-tools",
    version="1.0.0",
    instructions="Read GitHub issues for the desktop agent.",
)


def _from_fixture() -> dict[str, Any] | None:
    fixture = os.getenv("MCP_ISSUE_FIXTURE", "").strip()
    if not fixture:
        return None
    path = Path(fixture).expanduser()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, list):
        if not payload:
            raise ValueError("MCP_ISSUE_FIXTURE list is empty")
        payload = payload[0]
    if not isinstance(payload, dict):
        raise ValueError("MCP_ISSUE_FIXTURE must contain a JSON object")
    return payload


@server.tool()
def fetch_issue(repo: str, issue_number: int) -> dict[str, Any]:
    """Fetch one public GitHub issue and return its core fields."""

    if repo.count("/") != 1 or not all(part.strip() for part in repo.split("/")):
        raise ValueError("repo must be in owner/name format")
    if issue_number < 1:
        raise ValueError("issue_number must be >= 1")

    fixture = _from_fixture()
    if fixture is not None:
        return fixture

    url = f"https://api.github.com/repos/{repo}/issues/{issue_number}"
    request = Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "desktop-agent-mcp-issue-server",
        },
    )
    try:
        with urlopen(request, timeout=15) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"failed to fetch issue: {exc}") from exc

    if not isinstance(payload, dict):
        raise RuntimeError("GitHub returned an unexpected response")
    labels = [
        str(item.get("name", ""))
        for item in payload.get("labels", [])
        if isinstance(item, dict) and item.get("name")
    ]
    return {
        "repo": repo,
        "number": payload.get("number", issue_number),
        "title": payload.get("title", ""),
        "state": payload.get("state", ""),
        "body": payload.get("body", ""),
        "html_url": payload.get("html_url", ""),
        "labels": labels,
        "user": (payload.get("user") or {}).get("login", ""),
    }


if __name__ == "__main__":
    server.run("stdio")
