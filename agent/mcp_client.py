"""Minimal synchronous adapter around MCP stdio servers."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import sys
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Coroutine


@dataclass(frozen=True)
class MCPToolDefinition:
    server_name: str
    tool_name: str
    openai_name: str
    description: str
    input_schema: dict[str, Any]

    def openai_tool(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.openai_name,
                "description": self.description,
                "parameters": self.input_schema,
            },
        }


def _safe_function_name(server_name: str, tool_name: str) -> str:
    raw = f"mcp__{server_name}__{tool_name}"
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", raw)
    if len(safe) <= 64:
        return safe
    digest = hashlib.sha1(raw.encode("utf-8")).hexdigest()[:8]
    return f"{safe[:55]}_{digest}"


class MCPClientManager:
    """Load MCP tools lazily and call them without keeping a live event loop."""

    def __init__(self, config_path: str | Path | None = None) -> None:
        self.enabled = os.getenv("ENABLE_MCP", "1").strip().lower() not in {
            "0",
            "false",
            "no",
            "off",
        }
        configured = config_path or os.getenv("MCP_CONFIG_PATH", "mcp.json")
        path = Path(configured).expanduser()
        if not path.is_absolute():
            path = Path.cwd() / path
        self.config_path = path.resolve(strict=False)
        self.config_dir = self.config_path.parent
        self.servers: dict[str, dict[str, Any]] = {}
        self.errors: list[str] = []
        self._tools: list[MCPToolDefinition] = []
        self._loaded = False
        if self.enabled:
            self.servers = self._load_config()

    def _load_config(self) -> dict[str, dict[str, Any]]:
        if not self.config_path.is_file():
            return {}
        try:
            payload = json.loads(self.config_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            self.errors.append(f"failed to read MCP config: {exc}")
            return {}
        servers = payload.get("mcpServers", {})
        if not isinstance(servers, dict):
            self.errors.append("mcpServers must be an object")
            return {}
        return {
            str(name): value
            for name, value in servers.items()
            if isinstance(value, dict)
        }

    def load_tools(self) -> list[MCPToolDefinition]:
        if self._loaded:
            return list(self._tools)
        self._loaded = True
        if not self.enabled or not self.servers:
            return []

        for server_name, config in self.servers.items():
            try:
                params = self._server_params(config)
                raw_tools = self._run_async(self._list_tools(params))
            except Exception as exc:
                self.errors.append(f"{server_name}: {type(exc).__name__}: {exc}")
                continue

            for raw in raw_tools:
                if not isinstance(raw, dict):
                    continue
                tool_name = str(raw.get("name", "")).strip()
                if not tool_name:
                    continue
                schema = raw.get("inputSchema") or raw.get("input_schema")
                if not isinstance(schema, dict):
                    schema = {"type": "object", "properties": {}}
                description = str(raw.get("description", "")).strip()
                self._tools.append(
                    MCPToolDefinition(
                        server_name=server_name,
                        tool_name=tool_name,
                        openai_name=_safe_function_name(server_name, tool_name),
                        description=(
                            f"MCP tool {tool_name!r} from server {server_name!r}. "
                            f"{description}"
                        ).strip(),
                        input_schema=schema,
                    )
                )
        return list(self._tools)

    def openai_tools(self) -> list[dict[str, Any]]:
        return [tool.openai_tool() for tool in self.load_tools()]

    def call_tool(self, openai_name: str, arguments: dict[str, Any]) -> Any:
        tool = next(
            (item for item in self.load_tools() if item.openai_name == openai_name),
            None,
        )
        if tool is None:
            raise ValueError(f"unknown MCP tool: {openai_name}")
        config = self.servers[tool.server_name]
        params = self._server_params(config)
        return self._run_async(
            self._call_tool(params, tool.tool_name, arguments)
        )

    def _server_params(self, config: dict[str, Any]) -> Any:
        from mcp.client.stdio import StdioServerParameters

        raw_command = str(config.get("command", "")).strip()
        if not raw_command:
            raise ValueError("MCP server command is empty")
        if raw_command == "$PYTHON":
            if getattr(sys, "frozen", False):
                command = (
                    os.getenv("MCP_PYTHON", "").strip()
                    or shutil.which("python")
                    or shutil.which("python3")
                    or ""
                )
                if not command:
                    raise RuntimeError(
                        "MCP_PYTHON is required for stdio MCP servers in the "
                        "packaged app"
                    )
            else:
                command = sys.executable
        else:
            command = raw_command
        raw_args = config.get("args", [])
        if not isinstance(raw_args, list):
            raise ValueError("MCP server args must be a list")
        args = [str(item) for item in raw_args]

        env = dict(config.get("env") or {})
        for key in ("PYTHONPATH", "VIRTUAL_ENV", "MCP_ISSUE_FIXTURE"):
            if os.getenv(key):
                env.setdefault(key, os.environ[key])

        raw_cwd = config.get("cwd", ".")
        cwd = Path(str(raw_cwd)).expanduser()
        if not cwd.is_absolute():
            cwd = self.config_dir / cwd
        return StdioServerParameters(
            command=command,
            args=args,
            env={str(key): str(value) for key, value in env.items()},
            cwd=str(cwd.resolve(strict=False)),
        )

    @staticmethod
    async def _list_tools(params: Any) -> list[dict[str, Any]]:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.list_tools()
                return [
                    tool.model_dump(by_alias=True, exclude_none=True)
                    if hasattr(tool, "model_dump")
                    else dict(tool)
                    for tool in result.tools
                ]

    @staticmethod
    async def _call_tool(
        params: Any,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> Any:
        from mcp.client.session import ClientSession
        from mcp.client.stdio import stdio_client

        async with stdio_client(params) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                result = await session.call_tool(tool_name, arguments=arguments)
                if hasattr(result, "model_dump"):
                    return result.model_dump(mode="json", by_alias=True, exclude_none=True)
                return result

    @staticmethod
    def _run_async(coro: Coroutine[Any, Any, Any]) -> Any:
        """Run a coroutine from sync code, including from an async caller thread."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coro)

        box: dict[str, Any] = {}

        def runner() -> None:
            try:
                box["result"] = asyncio.run(coro)
            except BaseException as exc:  # propagate to the caller thread
                box["error"] = exc

        thread = threading.Thread(target=runner, name="mcp-call", daemon=True)
        thread.start()
        thread.join()
        if "error" in box:
            raise box["error"]
        return box.get("result")


__all__ = ["MCPClientManager", "MCPToolDefinition"]
