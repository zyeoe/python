from __future__ import annotations

# =============================================================================
# FastAPI + WebSocket backend
# =============================================================================
# WebSocket: /ws/{session_id}
#   Client text: {"type": "user_message", "content": "..."}
#   Interrupt:   {"type": "interrupt"}
#
# HTTP interrupt: POST /api/sessions/{session_id}/interrupt
#
# The agent loop is synchronous because the OpenAI Python client is
# synchronous. It runs in a worker thread while events are forwarded through an
# asyncio.Queue. Cancellation is checked by the agent at model/tool boundaries.
# =============================================================================

import asyncio
import json
import logging
import os
import sys
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

from agent.core import AgentInterrupted, ReActAgent


logger = logging.getLogger("desktop_agent.server")
logging.basicConfig(
    level=os.getenv("AGENT_LOG_LEVEL", os.getenv("LOG_LEVEL", "INFO")).upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)


app = FastAPI(
    title="Desktop Agent Backend",
    version="0.1.0",
    description="Internal FastAPI/WebSocket sidecar for the desktop agent.",
)

# pywebview loads the UI from a local file, so its origin may be "null".
# This permissive development policy is safe only because the server must bind
# to 127.0.0.1 and never be exposed externally.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _event(event_type: str, message: str | None = None, **payload: Any) -> dict[str, Any]:
    """Create the event envelope consumed by the frontend."""

    event: dict[str, Any] = {
        "type": event_type,
        "timestamp": _timestamp(),
        **payload,
    }
    if message is not None:
        event["message"] = message
    return event


@dataclass
class SessionState:
    """State owned by one WebSocket connection."""

    session_id: str
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue[dict[str, Any]] = field(default_factory=asyncio.Queue)
    cancel_event: threading.Event = field(default_factory=threading.Event)
    agent: ReActAgent | None = None
    runner: asyncio.Task[None] | None = None
    is_running: bool = False

    def emit(self, event: dict[str, Any]) -> None:
        """Queue an event safely from a worker thread or the event loop."""

        self.loop.call_soon_threadsafe(self.queue.put_nowait, event)

    def emit_now(self, event: dict[str, Any]) -> None:
        """Queue an event from the event-loop thread."""

        self.queue.put_nowait(event)


# One desktop process normally has one UI client. Keep sessions in memory for
# now; replace this with a persistent session manager when multi-window support
# is introduced.
ACTIVE_SESSIONS: dict[str, SessionState] = {}


WORKSPACE_ROOT = Path(os.getenv("AGENT_WORKSPACE", ".")).expanduser().resolve()
MAX_TREE_ENTRIES = 2_000
MAX_TREE_DEPTH = 12
MAX_FILE_BYTES = 2_000_000
EXCLUDED_DIRS = {
    ".git",
    ".idea",
    ".venv",
    ".vscode",
    "__pycache__",
    "build",
    "dist",
    "node_modules",
    "venv",
}
EXCLUDED_FILES = {".env"}


def _workspace_path(relative_path: str) -> Path:
    """Resolve a UI path and prevent escaping the configured workspace root."""

    if not isinstance(relative_path, str):
        raise HTTPException(status_code=400, detail="path must be a string")
    candidate = (WORKSPACE_ROOT / relative_path).resolve(strict=False)
    try:
        candidate.relative_to(WORKSPACE_ROOT)
    except ValueError as exc:
        raise HTTPException(status_code=403, detail="path is outside the workspace") from exc
    return candidate


def _relative_display_path(path: Path) -> str:
    return path.relative_to(WORKSPACE_ROOT).as_posix()


def _language_for_path(path: Path) -> str:
    return {
        ".py": "python",
        ".js": "javascript",
        ".mjs": "javascript",
        ".cjs": "javascript",
        ".ts": "typescript",
        ".tsx": "typescript",
        ".jsx": "javascript",
        ".html": "html",
        ".htm": "html",
        ".css": "css",
        ".scss": "css",
        ".json": "json",
        ".md": "markdown",
        ".txt": "text",
        ".yaml": "yaml",
        ".yml": "yaml",
        ".toml": "toml",
        ".xml": "xml",
        ".sh": "bash",
        ".ps1": "powershell",
    }.get(path.suffix.lower(), "text")


def _build_tree(directory: Path, depth: int, counter: list[int]) -> list[dict[str, Any]]:
    """Build a bounded, sorted workspace tree for the explorer panel."""

    if depth > MAX_TREE_DEPTH:
        return []

    entries: list[dict[str, Any]] = []
    try:
        children = sorted(
            directory.iterdir(),
            key=lambda item: (not item.is_dir(), item.name.lower()),
        )
    except OSError:
        return entries

    for child in children:
        if counter[0] >= MAX_TREE_ENTRIES:
            break
        if child.name in EXCLUDED_FILES or child.is_symlink():
            continue

        try:
            is_dir = child.is_dir()
        except OSError:
            continue

        if is_dir and child.name in EXCLUDED_DIRS:
            continue

        counter[0] += 1
        item: dict[str, Any] = {
            "name": child.name,
            "path": _relative_display_path(child),
            "type": "directory" if is_dir else "file",
        }
        if is_dir:
            item["children"] = _build_tree(child, depth + 1, counter)
        else:
            try:
                item["size"] = child.stat().st_size
            except OSError:
                item["size"] = None
        entries.append(item)

    return entries


async def _send_events(
    websocket: WebSocket,
    queue: asyncio.Queue[dict[str, Any]],
) -> None:
    """Forward queued events to the client in order."""

    while True:
        event = await queue.get()
        await websocket.send_json(event)


async def _run_agent(session: SessionState, user_text: str) -> None:
    """Run the synchronous ReAct loop in a worker thread."""

    session.is_running = True

    try:
        assert session.agent is not None
        await asyncio.to_thread(
            session.agent.run,
            user_text,
            on_event=session.emit,
            cancel_event=session.cancel_event,
        )
    except AgentInterrupted:
        session.emit(
            _event(
                "interrupted",
                "Agent 已被用户中断。",
                session_id=session.session_id,
                level="warning",
            )
        )
    except asyncio.CancelledError:
        # The websocket is closing. Keep cancel_event set so the worker thread
        # can observe it at its next safe point.
        session.emit(
            _event(
                "interrupted",
                "连接已关闭，Agent 已请求停止。",
                session_id=session.session_id,
                level="warning",
            )
        )
        raise
    except Exception as exc:
        logger.exception("Agent run failed")
        session.emit(
            _event(
                "error",
                "Agent 执行失败，请检查模型配置或服务端日志。",
                session_id=session.session_id,
                error_type=type(exc).__name__,
                error=str(exc),
                level="error",
            )
        )
    finally:
        session.is_running = False
        session.runner = None
        # Do not clear cancel_event here: if the asyncio task was cancelled, the
        # worker thread may still be unwinding and must see the stop request.


def _interrupt_session(session: SessionState) -> bool:
    """Set the cancellation flag and notify the client immediately."""

    if not session.is_running:
        return False

    session.cancel_event.set()
    session.emit(
        _event(
            "interrupt_requested",
            "已请求中断；Agent 会在最近的安全检查点停止。",
            session_id=session.session_id,
            level="warning",
        )
    )
    return True


@app.get("/api/workspace")
async def workspace_info() -> dict[str, Any]:
    return {
        "root": str(WORKSPACE_ROOT),
        "name": WORKSPACE_ROOT.name,
    }


@app.get("/api/workspace/tree")
async def workspace_tree() -> dict[str, Any]:
    if not WORKSPACE_ROOT.exists() or not WORKSPACE_ROOT.is_dir():
        raise HTTPException(status_code=404, detail="workspace not found")
    return {
        "root": str(WORKSPACE_ROOT),
        "entries": _build_tree(WORKSPACE_ROOT, 0, [0]),
        "limit": MAX_TREE_ENTRIES,
    }


@app.get("/api/workspace/file")
async def workspace_file(path: str) -> dict[str, Any]:
    target = _workspace_path(path)
    if not target.exists():
        raise HTTPException(status_code=404, detail="file not found")
    if not target.is_file():
        raise HTTPException(status_code=400, detail="path is not a file")

    try:
        size = target.stat().st_size
    except OSError as exc:
        raise HTTPException(status_code=500, detail="unable to stat file") from exc
    if size > MAX_FILE_BYTES:
        raise HTTPException(status_code=413, detail="file is too large to preview")

    try:
        raw = target.read_bytes()
    except OSError as exc:
        raise HTTPException(status_code=500, detail="unable to read file") from exc
    if b"\x00" in raw[:4096]:
        raise HTTPException(status_code=415, detail="binary files are not previewable")

    return {
        "path": _relative_display_path(target),
        "name": target.name,
        "language": _language_for_path(target),
        "size": size,
        "content": raw.decode("utf-8", errors="replace"),
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "service": "desktop-agent-backend",
        "active_sessions": len(ACTIVE_SESSIONS),
    }


@app.get("/api/sessions/{session_id}")
async def session_status(session_id: str) -> dict[str, Any]:
    session = ACTIVE_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")
    return {
        "session_id": session_id,
        "running": session.is_running,
    }


@app.post("/api/sessions/{session_id}/interrupt")
async def interrupt_session(session_id: str) -> dict[str, Any]:
    """Interrupt a running agent from ordinary HTTP."""

    session = ACTIVE_SESSIONS.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail="session not found")

    if not _interrupt_session(session):
        return {
            "ok": True,
            "session_id": session_id,
            "running": False,
            "message": "Agent 当前未运行。",
        }

    return {
        "ok": True,
        "session_id": session_id,
        "running": True,
        "message": "中断请求已发送。",
    }


@app.websocket("/ws/{session_id}")
async def agent_websocket(websocket: WebSocket, session_id: str) -> None:
    """Receive user text and stream agent events to the client."""

    await websocket.accept()

    if session_id in ACTIVE_SESSIONS:
        await websocket.send_json(
            _event(
                "error",
                "该 session_id 已有活动连接。",
                session_id=session_id,
                error_type="DuplicateSession",
                level="error",
            )
        )
        await websocket.close(code=1008)
        return

    try:
        max_steps = int(os.getenv("AGENT_MAX_STEPS", "6"))
        agent = ReActAgent(max_steps=max_steps, verbose=False)
    except Exception as exc:
        logger.exception("Failed to initialize ReAct agent")
        await websocket.send_json(
            _event(
                "error",
                "无法初始化 Agent，请检查 OPENAI_API_KEY、模型名称与网络配置。",
                session_id=session_id,
                error_type=type(exc).__name__,
                error=str(exc),
                level="error",
            )
        )
        await websocket.close(code=1011)
        return

    loop = asyncio.get_running_loop()
    session = SessionState(session_id=session_id, loop=loop, agent=agent)
    ACTIVE_SESSIONS[session_id] = session

    sender_task = asyncio.create_task(_send_events(websocket, session.queue))
    session.emit_now(
        _event(
            "session_ready",
            "Agent 会话已建立。",
            session_id=session_id,
            model=agent.model,
            max_steps=max_steps,
        )
    )

    try:
        while True:
            raw_message = await websocket.receive_text()

            try:
                data = json.loads(raw_message)
                if not isinstance(data, dict):
                    raise ValueError("message must be a JSON object")
            except (json.JSONDecodeError, ValueError):
                # Plain text frames are accepted as a convenience.
                data = {"type": "user_message", "content": raw_message}

            message_type = str(data.get("type", "user_message"))

            if message_type in {"interrupt", "cancel"}:
                if not _interrupt_session(session):
                    session.emit_now(
                        _event(
                            "error",
                            "当前没有正在运行的 Agent 任务。",
                            session_id=session_id,
                            error_type="NoActiveRun",
                            level="error",
                        )
                    )
                continue

            if message_type == "ping":
                session.emit_now(_event("pong", "pong", session_id=session_id))
                continue

            if message_type != "user_message":
                session.emit_now(
                    _event(
                        "error",
                        f"未知消息类型：{message_type}",
                        session_id=session_id,
                        error_type="UnknownMessageType",
                        level="error",
                    )
                )
                continue

            content = data.get("content")
            if not isinstance(content, str) or not content.strip():
                session.emit_now(
                    _event(
                        "error",
                        "user_message.content 必须是非空字符串。",
                        session_id=session_id,
                        error_type="InvalidUserMessage",
                        level="error",
                    )
                )
                continue

            if session.is_running:
                session.emit_now(
                    _event(
                        "error",
                        "Agent 正在处理上一条请求，请先等待或发送 interrupt。",
                        session_id=session_id,
                        error_type="AgentBusy",
                        level="error",
                    )
                )
                continue

            # Mark running before scheduling the task to close the small race
            # between task creation and the coroutine's first instruction.
            session.is_running = True
            session.cancel_event.clear()
            session.runner = asyncio.create_task(
                _run_agent(session, content.strip())
            )

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected: %s", session_id)
    except Exception:
        logger.exception("WebSocket error: %s", session_id)
    finally:
        # Request cancellation, then cancel the asyncio wrapper. The worker
        # thread observes cancel_event at its next model/tool boundary.
        session.cancel_event.set()
        if session.runner is not None and not session.runner.done():
            session.runner.cancel()

        sender_task.cancel()
        try:
            await sender_task
        except asyncio.CancelledError:
            pass

        ACTIVE_SESSIONS.pop(session_id, None)


if __name__ == "__main__":
    # Development shortcut. In the packaged desktop app, the FastAPI app should
    # be started programmatically by the pywebview launcher.
    import uvicorn

    uvicorn.run(
        "server.main:app",
        host=os.getenv("APP_HOST", "127.0.0.1"),
        port=int(os.getenv("APP_PORT", "8000")),
        reload=True,
    )
