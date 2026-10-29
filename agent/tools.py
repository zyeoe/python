from __future__ import annotations

# =============================================================================
# Agent tools
# =============================================================================
# This module contains four small, model-friendly tools:
#   read  - paginated, line-numbered UTF-8 text reading
#   write - atomic UTF-8 file writing
#   edit  - exact and unique string replacement
#   bash  - bounded shell command execution
#
# The same module also exposes JSON Schemas in the OpenAI "function tool" form,
# so the agent loop can pass them directly to the model.
# =============================================================================

import json
import locale
import os
import signal
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

# Keep reads bounded by default. A single large file should be inspected in
# small pages instead of flooding the model context window.
DEFAULT_READ_LIMIT = 200
MAX_READ_LIMIT = 1_000
MAX_LINE_CHARS = 4_000

# Keep shell commands bounded as well. The model can request a larger timeout,
# but it cannot exceed MAX_BASH_TIMEOUT.
DEFAULT_BASH_TIMEOUT = 30
MAX_BASH_TIMEOUT = 300


class ToolError(RuntimeError):
    """A user/model-correctable error raised by one of the tools."""


def _resolve_path(path: str) -> Path:
    """Resolve a path relative to the process working directory.

    Relative paths are useful because the desktop app will normally set its
    working directory to the project/workspace the user has opened.
    """

    if not isinstance(path, str) or not path.strip():
        raise ToolError("path must be a non-empty string")
    p = Path(path).expanduser()
    if not p.is_absolute():
        p = Path.cwd() / p
    return p.resolve(strict=False)


def _read_text_exact(path: Path) -> str:
    """Read text without newline translation.

    newline='' preserves the original CRLF/LF sequences. That matters for
    edit(): an exact replacement should not silently rewrite every newline in
    the file.
    """

    with path.open("r", encoding="utf-8", errors="replace", newline="") as f:
        return f.read()


def read(path: str, offset: int = 0, limit: int = DEFAULT_READ_LIMIT) -> str:
    """Read a text file line by line and return numbered lines.

    Args:
        path: File path, relative to cwd or absolute.
        offset: Zero-based line offset. offset=0 starts at line 1.
        limit: Maximum number of lines to return. Capped at MAX_READ_LIMIT.

    Returns:
        A human/model-readable block containing line numbers and pagination
        metadata. The file is streamed once, so memory usage stays bounded
        even for very large files.
    """

    p = _resolve_path(path)
    if not p.exists():
        raise ToolError(f"file does not exist: {p}")
    if not p.is_file():
        raise ToolError(f"path is not a file: {p}")

    try:
        offset = int(offset)
        limit = int(limit)
    except (TypeError, ValueError) as exc:
        raise ToolError("offset and limit must be integers") from exc

    if offset < 0:
        raise ToolError("offset must be >= 0")
    if limit <= 0:
        raise ToolError("limit must be > 0")
    limit = min(limit, MAX_READ_LIMIT)

    selected: list[tuple[int, str]] = []
    total_lines = 0

    # A single streaming pass gives us both the requested window and the total
    # line count without retaining the whole file in memory.
    with p.open("r", encoding="utf-8", errors="replace", newline="") as f:
        for line_no, line in enumerate(f, start=1):
            total_lines = line_no
            if offset <= line_no - 1 < offset + limit:
                # Remove only the line ending; preserve indentation and other
                # whitespace inside the line.
                text = line.rstrip("\r\n")
                if len(text) > MAX_LINE_CHARS:
                    text = text[:MAX_LINE_CHARS] + " ... [line truncated]"
                selected.append((line_no, text))

    if total_lines == 0:
        return f"{p}\n[File is empty]"

    if not selected:
        return (
            f"{p}\n[File has {total_lines} lines; offset={offset} is past EOF. "
            f"Use offset < {total_lines}.]"
        )

    first_line = selected[0][0]
    last_line = selected[-1][0]
    output = [f"{p} (lines {first_line}-{last_line} of {total_lines})"]
    output.extend(f"{line_no:>6} | {text}" for line_no, text in selected)

    if last_line < total_lines:
        output.append(
            f"\n[More lines available: call read(path={json.dumps(str(p))}, "
            f"offset={last_line}, limit={limit})]"
        )

    return "\n".join(output)


def write(path: str, content: str) -> str:
    """Atomically write UTF-8 text to a file.

    The data is written to a temporary file in the same directory, flushed and
    fsynced, then moved into place with os.replace(). os.replace() is atomic on
    the same filesystem, so readers never observe a half-written target file.

    Parent directories are created automatically.
    """

    if not isinstance(content, str):
        raise ToolError("content must be a string")

    p = _resolve_path(path)
    p.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{p.name}.",
        suffix=".tmp",
        dir=str(p.parent),
    )
    temp_path = Path(temp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())

        # Same-directory rename is atomic. On Windows os.replace overwrites an
        # existing target without requiring a separate delete.
        os.replace(temp_path, p)
    except Exception:
        # The descriptor may already have been closed by fdopen on some error
        # paths, so ignore close errors during cleanup.
        try:
            os.close(fd)
        except OSError:
            pass
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise

    return f"Wrote {len(content.encode('utf-8'))} bytes to {p}"


def edit(path: str, old: str, new: str) -> str:
    """Replace exactly one occurrence of an exact string.

    Safety rule:
    - 0 occurrences -> error
    - 2 or more occurrences -> ambiguity error; no guessing
    - exactly 1 occurrence -> atomic replacement via write()
    """

    if not isinstance(old, str) or old == "":
        raise ToolError("old must be a non-empty string")
    if not isinstance(new, str):
        raise ToolError("new must be a string")

    p = _resolve_path(path)
    if not p.exists():
        raise ToolError(f"file does not exist: {p}")
    if not p.is_file():
        raise ToolError(f"path is not a file: {p}")

    content = _read_text_exact(p)
    count = content.count(old)

    if count == 0:
        raise ToolError("old string was not found; no changes were made")
    if count > 1:
        raise ToolError(
            f"old string occurs {count} times; replacement is ambiguous. "
            "Read more context and pass a longer unique string."
        )

    updated = content.replace(old, new, 1)
    write(str(p), updated)
    return f"Edited {p}: replaced 1 occurrence"


def _subprocess_encoding() -> str:
    """Choose an encoding based on the host platform.

    Windows shells commonly emit the active ANSI code page (for example GBK),
    while POSIX tools usually emit UTF-8. errors='replace' keeps the tool from
    crashing on mixed output.
    """

    if os.name == "nt":
        return locale.getpreferredencoding(False) or "utf-8"
    return "utf-8"


def _kill_process_tree(process: subprocess.Popen[str]) -> None:
    """Best-effort termination of the shell and its child processes."""

    if process.poll() is not None:
        return

    if os.name == "nt":
        # shell=True can create child processes. taskkill /T terminates the
        # whole command tree, which prevents orphaned subprocesses.
        try:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass
        if process.poll() is None:
            process.kill()
    else:
        try:
            os.killpg(os.getpgid(process.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            process.kill()


def bash(command: str, timeout: int = DEFAULT_BASH_TIMEOUT) -> dict[str, Any]:
    """Execute a shell command with a hard timeout.

    Returns:
        {
            "stdout": str,
            "stderr": str,
            "exit_code": int,
        }

    A timed-out command is treated as failure: exit_code is 124 and stderr
    includes a timeout message.
    """

    if not isinstance(command, str) or not command.strip():
        raise ToolError("command must be a non-empty string")

    try:
        timeout = int(timeout)
    except (TypeError, ValueError) as exc:
        raise ToolError("timeout must be an integer") from exc

    if timeout <= 0:
        raise ToolError("timeout must be > 0")
    timeout = min(timeout, MAX_BASH_TIMEOUT)

    popen_kwargs: dict[str, Any] = {
        "shell": True,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.PIPE,
        "stderr": subprocess.PIPE,
        "text": True,
        "encoding": _subprocess_encoding(),
        "errors": "replace",
        "cwd": str(Path.cwd()),
    }

    # Put the command in its own process group so timeout cleanup can terminate
    # children as well as the shell.
    if os.name == "nt":
        popen_kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        popen_kwargs["start_new_session"] = True

    process = subprocess.Popen(command, **popen_kwargs)

    try:
        stdout, stderr = process.communicate(timeout=timeout)
        return {
            "stdout": stdout or "",
            "stderr": stderr or "",
            "exit_code": int(process.returncode),
        }
    except subprocess.TimeoutExpired as exc:
        _kill_process_tree(process)

        # Do not wait indefinitely for inherited stdout/stderr handles. A child
        # process may still hold those pipes open after the shell is killed.
        partial_stdout, partial_stderr = exc.stdout or "", exc.stderr or ""
        try:
            final_stdout, final_stderr = process.communicate(timeout=1)
            stdout = final_stdout or partial_stdout
            stderr = final_stderr or partial_stderr
        except subprocess.TimeoutExpired:
            process.kill()
            stdout, stderr = partial_stdout, partial_stderr

        if isinstance(stdout, bytes):
            stdout = stdout.decode(_subprocess_encoding(), errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode(_subprocess_encoding(), errors="replace")

        timeout_message = f"Command timed out after {timeout} seconds."
        stderr = (str(stderr).rstrip() + "\n" + timeout_message).strip()
        return {
            "stdout": str(stdout),
            "stderr": stderr,
            "exit_code": 124,
        }


# Runtime function registry used by the ReAct loop.
TOOL_FUNCTIONS: dict[str, Callable[..., Any]] = {
    "read": read,
    "write": write,
    "edit": edit,
    "bash": bash,
}


# =============================================================================
# JSON Schemas
# =============================================================================
# TOOL_SCHEMAS is the plain documentation required by the project. The
# OPENAI_TOOLS wrapper below is the exact shape accepted by the Chat Completions
# tools parameter.
# =============================================================================

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "name": "read",
        "description": (
            "Read a UTF-8 text file with line numbers. Use offset and limit to "
            "page through large files. offset is zero-based. Always inspect "
            "files before editing them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, relative to the current workspace or absolute.",
                },
                "offset": {
                    "type": "integer",
                    "minimum": 0,
                    "default": 0,
                    "description": "Zero-based line offset. 0 starts at line 1.",
                },
                "limit": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_READ_LIMIT,
                    "default": DEFAULT_READ_LIMIT,
                    "description": "Maximum number of lines to return.",
                },
            },
            "required": ["path"],
            "additionalProperties": False,
        },
    },
    {
        "name": "write",
        "description": (
            "Write UTF-8 text to a file atomically. Parent directories are "
            "created automatically. The target is replaced only after the full "
            "temporary file has been written and fsynced."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Destination file path, relative or absolute.",
                },
                "content": {
                    "type": "string",
                    "description": "Complete UTF-8 text content to write.",
                },
            },
            "required": ["path", "content"],
            "additionalProperties": False,
        },
    },
    {
        "name": "edit",
        "description": (
            "Replace exactly one occurrence of an exact string. If the old "
            "string occurs zero times or more than once, the tool fails instead "
            "of guessing. Use a longer unique old string when needed."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "File path, relative or absolute.",
                },
                "old": {
                    "type": "string",
                    "description": "Exact string to replace. Must occur exactly once.",
                },
                "new": {
                    "type": "string",
                    "description": "Replacement string.",
                },
            },
            "required": ["path", "old", "new"],
            "additionalProperties": False,
        },
    },
    {
        "name": "bash",
        "description": (
            "Execute a shell command with a hard timeout. Returns stdout, "
            "stderr and exit_code. A timeout returns exit_code=124. Use commands "
            "for inspection and builds; avoid destructive commands unless the "
            "user explicitly asks for them."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute in the current workspace.",
                },
                "timeout": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": MAX_BASH_TIMEOUT,
                    "default": DEFAULT_BASH_TIMEOUT,
                    "description": "Maximum run time in seconds.",
                },
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
]


# OpenAI Chat Completions tool wrapper. This is passed directly to
# client.chat.completions.create(..., tools=OPENAI_TOOLS).
OPENAI_TOOLS: list[dict[str, Any]] = [
    {"type": "function", "function": spec} for spec in TOOL_SCHEMAS
]


def result_to_text(result: Any) -> str:
    """Serialize a tool result for a Chat Completions 'tool' message."""

    if isinstance(result, str):
        return result
    return json.dumps(result, ensure_ascii=False, indent=2)


__all__ = [
    "ToolError",
    "read",
    "write",
    "edit",
    "bash",
    "TOOL_SCHEMAS",
    "TOOL_FUNCTIONS",
    "OPENAI_TOOLS",
    "result_to_text",
]
