from __future__ import annotations

"""Offline baseline checks for the desktop agent.

Run with:
    python -m agent.selftest
    python -m agent.selftest --out selftest_report.json
"""

import argparse
import base64
import json
import os
import sys
import tempfile
import traceback
from pathlib import Path
from typing import Any, Callable

from agent.core import ReActAgent
from agent.mock_client import ScriptedMockClient, final_turn, tool_call_turn
from agent.tools import TOOL_SCHEMAS, ToolError, bash, edit, read, write

TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


class SelfTestFailure(AssertionError):
    pass


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise SelfTestFailure(message)


def _run_check(
    name: str,
    check: Callable[[], dict[str, Any] | None],
) -> dict[str, Any]:
    try:
        details = check() or {}
        return {"name": name, "ok": True, **details}
    except Exception as exc:
        return {
            "name": name,
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def run_selftest() -> dict[str, Any]:
    checks: list[dict[str, Any]] = []

    with tempfile.TemporaryDirectory(prefix="desktop-agent-selftest-") as temp_dir:
        root = Path(temp_dir)

        def write_auto_mkdir() -> dict[str, Any]:
            target = root / "a" / "b" / "created.txt"
            write(str(target), "created\n")
            _require(target.is_file(), "write did not create parent directories")
            return {"path": str(target)}

        checks.append(_run_check("write_auto_mkdir", write_auto_mkdir))

        def read_pagination() -> dict[str, Any]:
            target = root / "pagination.txt"
            write(str(target), "alpha\nbeta\ngamma\ndelta\n")
            result = read(str(target), offset=1, limit=2)
            _require(isinstance(result, str), "text read must return a string")
            _require("2 | beta" in result, "line numbering is missing")
            _require("3 | gamma" in result, "pagination window is wrong")
            _require("alpha" not in result, "offset was not applied")
            return {"preview": result.splitlines()[-1]}

        checks.append(_run_check("read_text_pagination", read_pagination))

        def read_image_multimodal() -> dict[str, Any]:
            target = root / "pixel.png"
            target.write_bytes(TINY_PNG)
            result = read(str(target))
            _require(isinstance(result, dict), "image read must return metadata")
            _require(result.get("kind") == "image", "image kind is missing")
            _require(result.get("mime_type") == "image/png", "PNG was not detected")
            data_url = result.get("data_url")
            _require(
                isinstance(data_url, str)
                and data_url.startswith("data:image/png;base64,"),
                "image data URL is missing",
            )
            return {
                "mime_type": result["mime_type"],
                "size_bytes": result["size_bytes"],
            }

        checks.append(_run_check("read_image", read_image_multimodal))

        def edit_unique() -> dict[str, Any]:
            target = root / "edit.txt"
            write(str(target), "before\n")
            edit(str(target), "before", "after")
            _require(read(str(target)).endswith("1 | after"), "edit did not apply")
            return {"result": "replaced exactly once"}

        checks.append(_run_check("edit_unique", edit_unique))

        def edit_rejects_duplicate() -> dict[str, Any]:
            target = root / "duplicate.txt"
            write(str(target), "same\nsame\n")
            try:
                edit(str(target), "same", "different")
            except ToolError as exc:
                _require("unique" in str(exc).lower(), "unexpected duplicate error")
                return {"result": "duplicate replacement rejected"}
            raise SelfTestFailure("edit accepted a duplicate old string")

        checks.append(_run_check("edit_duplicate_rejected", edit_rejects_duplicate))

        def bash_execution() -> dict[str, Any]:
            result = bash("echo desktop-agent-selftest", timeout=5)
            _require(result.get("exit_code") == 0, "bash exit code was not zero")
            _require("desktop-agent-selftest" in result.get("stdout", ""), "stdout missing")
            return {"exit_code": result["exit_code"]}

        checks.append(_run_check("bash_execution", bash_execution))

        def image_reaches_model() -> dict[str, Any]:
            target = root / "model-image.png"
            target.write_bytes(TINY_PNG)
            client = ScriptedMockClient(
                [
                    tool_call_turn("read", {"path": str(target)}, call_id="image-call"),
                    final_turn("Image inspected."),
                ]
            )
            agent = ReActAgent(
                max_steps=3,
                client=client,
                model="mock",
                verbose=False,
            )
            events: list[dict[str, Any]] = []
            answer = agent.run("inspect the image", on_event=events.append)
            _require(answer == "Image inspected.", "mock final answer was not returned")
            event_types = {event.get("type") for event in events}
            _require("tool_call" in event_types, "tool_call event was not emitted")
            _require("tool_result" in event_types, "tool_result event was not emitted")
            _require(len(client.calls) >= 2, "the model was not called after the tool result")

            second_messages = client.calls[1]["messages"]
            has_image_part = any(
                isinstance(message.get("content"), list)
                and any(
                    isinstance(part, dict) and part.get("type") == "image_url"
                    for part in message["content"]
                )
                for message in second_messages
                if message.get("role") == "user"
            )
            _require(has_image_part, "image content was not attached for the model")
            _require(
                all(
                    "data_url" not in item["result"]
                    for item in agent.trace
                    if isinstance(item.get("result"), dict)
                ),
                "base64 image leaked into the trace",
            )
            return {"model_calls": len(client.calls), "events": sorted(event_types)}

        checks.append(_run_check("image_reaches_model", image_reaches_model))

        def tool_schema_documents_images() -> dict[str, Any]:
            schema = next(item for item in TOOL_SCHEMAS if item["name"] == "read")
            description = schema["description"].lower()
            _require("image" in description, "read schema does not mention images")
            return {"tools": [item["name"] for item in TOOL_SCHEMAS]}

        checks.append(_run_check("tool_schema", tool_schema_documents_images))

        def ui_handles_tool_events() -> dict[str, Any]:
            app_js = (Path(__file__).resolve().parent.parent / "ui" / "app.js").read_text(
                encoding="utf-8"
            )
            _require('case "tool_call"' in app_js, "UI lacks tool_call handling")
            _require('case "tool_result"' in app_js, "UI lacks tool_result handling")
            return {"events": ["tool_call", "tool_result"]}

        checks.append(_run_check("ui_tool_events", ui_handles_tool_events))

        def packaging_config() -> dict[str, Any]:
            root_dir = Path(__file__).resolve().parent.parent
            _require((root_dir / "main.py").is_file(), "main.py is missing")
            spec = (root_dir / "packaging" / "desktop-agent.spec").read_text(
                encoding="utf-8"
            )
            _require("console=False" in spec, "spec does not disable the console")
            return {"console": False, "spec": "packaging/desktop-agent.spec"}

        checks.append(_run_check("packaging_config", packaging_config))

    backend = os.getenv("AGENT_MODEL_BACKEND", "auto").strip().lower()
    return {
        "ok": all(check["ok"] for check in checks),
        "python": sys.version.split()[0],
        "model_backend": backend,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run offline desktop-agent checks")
    parser.add_argument("--out", type=Path, help="Write the JSON report to this path")
    args = parser.parse_args(argv)

    try:
        report = run_selftest()
    except Exception as exc:
        report = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "traceback": traceback.format_exc(),
        }

    payload = json.dumps(report, ensure_ascii=False, indent=2)
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(payload + "\n", encoding="utf-8")
    print(payload)
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
