# -*- mode: python ; coding: utf-8 -*-

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_submodules, copy_metadata

SPEC_DIR = Path(SPECPATH).resolve()
ROOT = SPEC_DIR if (SPEC_DIR / "main.py").is_file() else SPEC_DIR.parent

APP_NAME = "desktop-agent"
BUILD_MODE = os.environ.get("BUILD_MODE", "onefile").lower()

if BUILD_MODE not in {"onefile", "onedir"}:
    raise ValueError("BUILD_MODE must be 'onefile' or 'onedir'")

datas = []
binaries = []
hiddenimports = []


def add_data(relative_path: str, target: str) -> None:
    path = ROOT / relative_path
    if path.exists():
        datas.append((str(path), target))


def add_package(package_name: str) -> None:
    """Collect package data, dynamic libraries, and dynamically imported modules."""
    try:
        package_datas, package_binaries, package_hidden = collect_all(package_name)
        datas.extend(package_datas)
        binaries.extend(package_binaries)
        hiddenimports.extend(package_hidden)
    except Exception:
        # Optional packages are allowed to be absent. Required packages will still
        # fail during Analysis when their imports are analyzed.
        pass


add_data("ui", "ui")
add_data("assets", "assets")
add_data("mcp.json", ".")
add_data(".env", ".")
add_data(".env.example", ".")

# Required by pywebview on Windows (Edge WebView2 / Python.NET).
add_package("webview")
add_package("pythonnet")
add_package("clr_loader")

# OpenAI SDK and MCP use dynamic imports in several places.
add_package("openai")
add_package("mcp")

# Uvicorn and websockets select protocol implementations dynamically.
hiddenimports += collect_submodules("uvicorn")
hiddenimports += collect_submodules("websockets")
hiddenimports += [
    "anyio._backends._asyncio",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.websockets_impl",
    "uvicorn.lifespan.on",
]

for distribution in (
    "fastapi",
    "uvicorn",
    "websockets",
    "openai",
    "mcp",
    "pydantic",
    "pydantic-settings",
    "python-dotenv",
    "pywebview",
    "pythonnet",
    "clr-loader",
):
    try:
        datas += copy_metadata(distribution)
    except Exception:
        pass

icon_file = ROOT / "assets" / "app.ico"
icon_path = str(icon_file) if icon_file.is_file() else None

a = Analysis(
    [str(ROOT / "main.py")],
    pathex=[str(ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
)

pyz = PYZ(a.pure)

if BUILD_MODE == "onefile":
    exe = EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        [],
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=icon_path,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name=APP_NAME,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=False,
        disable_windowed_traceback=False,
        icon=icon_path,
    )

    coll = COLLECT(
        exe,
        a.binaries,
        a.datas,
        strip=False,
        upx=False,
        upx_exclude=[],
        name=APP_NAME,
    )
