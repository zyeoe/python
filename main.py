from __future__ import annotations

import ctypes
import os
import socket
import sys
import threading
import time
import traceback
from pathlib import Path
from urllib.parse import quote

from dotenv import load_dotenv

APP_NAME = "Desktop Agent"
APP_HOST = "127.0.0.1"


def _base_dirs() -> tuple[Path, Path]:
    """Return the writable app directory and bundled-resource directory."""

    if getattr(sys, "frozen", False):
        app_dir = Path(sys.executable).resolve().parent
        resource_dir = Path(getattr(sys, "_MEIPASS", app_dir)).resolve()
    else:
        app_dir = Path(__file__).resolve().parent
        resource_dir = app_dir
    return app_dir, resource_dir


APP_DIR, RESOURCE_DIR = _base_dirs()

# Priority: OS environment > .env beside the executable > bundled .env.
for env_file in (APP_DIR / ".env", RESOURCE_DIR / ".env"):
    if env_file.is_file():
        load_dotenv(env_file, override=False)

# Resolve relative paths against the application directory instead of cwd.
workspace = Path(os.getenv("AGENT_WORKSPACE", ".")).expanduser()
if not workspace.is_absolute():
    workspace = APP_DIR / workspace
os.environ["AGENT_WORKSPACE"] = str(workspace.resolve())

mcp_config = Path(os.getenv("MCP_CONFIG_PATH", "mcp.json")).expanduser()
if not mcp_config.is_absolute():
    external_mcp = APP_DIR / mcp_config
    bundled_mcp = RESOURCE_DIR / mcp_config
    mcp_config = (
        external_mcp
        if external_mcp.exists()
        else bundled_mcp
        if bundled_mcp.exists()
        else external_mcp
    )
os.environ["MCP_CONFIG_PATH"] = str(mcp_config.resolve())

skills_dir = Path(os.getenv("SKILLS_DIR", "skills")).expanduser()
if not skills_dir.is_absolute():
    external_skills = APP_DIR / skills_dir
    bundled_skills = RESOURCE_DIR / skills_dir
    skills_dir = (
        external_skills
        if external_skills.is_dir()
        else bundled_skills
        if bundled_skills.is_dir()
        else external_skills
    )
os.environ["SKILLS_DIR"] = str(skills_dir.resolve())

# Import after loading .env because server.main reads environment at import time.
from fastapi.staticfiles import StaticFiles  # noqa: E402
from server.main import app  # noqa: E402


def main() -> None:
    import uvicorn
    import webview

    ui_dir = RESOURCE_DIR / "ui"
    index_file = ui_dir / "index.html"
    if not index_file.is_file():
        raise FileNotFoundError(f"UI asset not found: {index_file}")

    # API and WebSocket routes are registered first. Mount the UI last at root.
    app.mount("/", StaticFiles(directory=str(ui_dir), html=True), name="ui")

    # Bind a dynamic loopback port to avoid collisions with an existing server.
    requested_port = int(os.getenv("APP_PORT", "0"))
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind((APP_HOST, requested_port))
    sock.listen(128)
    port = int(sock.getsockname()[1])

    config = uvicorn.Config(
        app=app,
        host=APP_HOST,
        port=port,
        log_level="warning",
        access_log=False,
        http="h11",
        ws="websockets",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(
        target=server.run,
        kwargs={"sockets": [sock]},
        name="fastapi-backend",
        daemon=True,
    )
    thread.start()

    deadline = time.monotonic() + 15
    while not server.started:
        if not thread.is_alive():
            raise RuntimeError("FastAPI backend failed to start.")
        if time.monotonic() >= deadline:
            raise TimeoutError("FastAPI backend startup timed out.")
        time.sleep(0.05)

    api_base = f"http://{APP_HOST}:{port}"
    ui_url = f"{api_base}/?apiBase={quote(api_base, safe='')}"

    try:
        webview.create_window(
            APP_NAME,
            ui_url,
            width=1440,
            height=900,
            min_size=(1024, 680),
        )
        webview.start(
            debug=os.getenv("WEBVIEW_DEBUG", "").lower()
            in {"1", "true", "yes"},
            private_mode=False,
        )
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


if __name__ == "__main__":
    try:
        main()
    except BaseException as exc:
        details = traceback.format_exc()
        log_path = APP_DIR / "desktop-agent-error.log"
        try:
            log_path.write_text(details, encoding="utf-8")
        except OSError:
            log_path = None

        message = f"{APP_NAME} failed to start:\n\n{type(exc).__name__}: {exc}"
        if log_path is not None:
            message += f"\n\nLog: {log_path}"

        try:
            ctypes.windll.user32.MessageBoxW(None, message, APP_NAME, 0x10)
        except Exception:
            pass

        raise SystemExit(1) from None
