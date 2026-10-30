from __future__ import annotations

# =============================================================================
# ReAct agent loop
# =============================================================================
# The loop uses the OpenAI-compatible Chat Completions "tool calling" format.
# A ReAct step is:
#   1. The model decides whether it needs a tool.
#   2. If needed, it emits one or more tool calls.
#   3. The tool results are appended to the message list.
#   4. The model receives the updated context and continues.
#   5. When it produces ordinary assistant text, the loop returns that answer.
#
# Conversation state is stored in self.messages, which is a normal Chat
# Completions message list. This makes the loop easy to inspect, persist, and
# later stream over WebSocket.
# =============================================================================

import argparse
import json
import os
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

# Support both:
#   python -m agent.core
#   python agent/core.py
if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.tools import OPENAI_TOOLS, TOOL_FUNCTIONS, result_to_text


SYSTEM_PROMPT = """You are a careful desktop coding agent.

Use a ReAct-style tool loop internally:
- Understand the user's request.
- If you need file contents, command output, or a filesystem change, call the
  appropriate tool instead of guessing.
- Inspect every tool result and decide whether another action is needed.
- Never invent file contents, command output, or successful edits.
- When enough evidence is available, answer the user directly and concisely.

Tool-use rules:
- Read a file before editing it unless the user has already supplied the exact
  content and position.
- edit() requires a unique exact old string. If it is ambiguous, read more
  context and use a longer old string. Never guess a replacement location.
- Prefer small, reversible shell commands. Do not run destructive commands
  unless the user explicitly requests them.
- Relative paths are resolved from the current working directory.
- Answer in the same language as the user.

Do not reveal hidden chain-of-thought. You may provide a short summary of the
actions taken and the final result.
"""


DEFAULT_MODEL = "gpt-4.1-mini"
MAX_TOOL_MESSAGE_CHARS = 60_000


class AgentInterrupted(RuntimeError):
    """Raised when a client requests cancellation at a safe loop boundary."""


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise AgentInterrupted("Agent run interrupted by client")


def _emit_event(
    on_event: Callable[[dict[str, Any]], None] | None,
    event_type: str,
    message: str | None = None,
    **payload: Any,
) -> None:
    """Send one high-level progress event to the caller.

    This is intentionally a progress/observability stream, not a disclosure of
    the model's hidden chain-of-thought. Consumers receive status, tool actions,
    tool results and the final answer.
    """

    if on_event is None:
        return

    event: dict[str, Any] = {
        "type": event_type,
        "timestamp": datetime.now(timezone.utc).isoformat(),
        **payload,
    }
    if message is not None:
        event["message"] = message

    try:
        on_event(event)
    except Exception as exc:
        # Observability must not break the agent loop. A disconnected websocket,
        # for example, should be handled by the server rather than corrupting
        # the conversation state.
        print(f"[event callback error] {type(exc).__name__}: {exc}", file=sys.stderr)


def _describe_tool_call(name: str, arguments: dict[str, Any]) -> str:
    if name == "read":
        return f"读取文件：{arguments.get('path', '?')}"
    if name == "write":
        return f"写入文件：{arguments.get('path', '?')}"
    if name == "edit":
        return f"编辑文件：{arguments.get('path', '?')}"
    if name == "bash":
        return f"执行命令：{arguments.get('command', '?')}"
    return f"调用工具：{name}"


def _load_dotenv_if_available() -> None:
    """Load .env when python-dotenv is installed.

    The desktop app will eventually own configuration management, but loading
    .env here makes `python -m agent.core` convenient during development.
    """

    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv()


def _create_openai_client() -> Any:
    """Create an OpenAI-compatible client from environment variables."""

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError(
            "The openai package is not installed. Run: pip install -r requirements.txt"
        ) from exc

    api_key = os.getenv("OPENAI_API_KEY", "").strip()
    base_url = os.getenv("OPENAI_BASE_URL", "").strip() or None

    # Local OpenAI-compatible servers often do not require a real key. Allowing
    # a placeholder keeps local development friction low.
    if not api_key:
        if base_url:
            api_key = "not-needed"
        else:
            raise RuntimeError(
                "OPENAI_API_KEY is not set. Add it to .env or pass a custom client."
            )

    kwargs: dict[str, Any] = {"api_key": api_key}
    if base_url:
        kwargs["base_url"] = base_url
    return OpenAI(**kwargs)


def _message_text(content: Any) -> str:
    """Normalize assistant content to plain text.

    Chat Completions usually returns a string. Some compatible providers may
    return a list of content parts, so we handle both without tying the loop to
    one provider's exact response classes.
    """

    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            else:
                text = getattr(item, "text", None)
                if text:
                    parts.append(str(text))
        return "".join(parts)
    return str(content)


def _tool_call_dict(tool_call: Any) -> dict[str, Any]:
    """Convert an SDK tool-call object into a message-list dictionary."""

    if isinstance(tool_call, dict):
        return tool_call

    if hasattr(tool_call, "model_dump"):
        data = tool_call.model_dump(exclude_none=True)
        if isinstance(data, dict):
            return data

    function = getattr(tool_call, "function", None)
    return {
        "id": getattr(tool_call, "id", ""),
        "type": getattr(tool_call, "type", "function"),
        "function": {
            "name": getattr(function, "name", ""),
            "arguments": getattr(function, "arguments", "{}"),
        },
    }


def _clip_tool_message(text: str) -> str:
    """Bound one tool result before it enters the model context."""

    if len(text) <= MAX_TOOL_MESSAGE_CHARS:
        return text
    omitted = len(text) - MAX_TOOL_MESSAGE_CHARS
    return (
        text[:MAX_TOOL_MESSAGE_CHARS]
        + f"\n\n[Tool output truncated: {omitted} characters omitted]"
    )


class ReActAgent:
    """A minimal multi-step ReAct agent using Chat Completions tools.

    Args:
        max_steps: Maximum model turns for one user request. The final turn is
            made without tools so the model is encouraged to produce an answer.
        client: Optional pre-built OpenAI-compatible client. Injecting a client
            is useful for tests or for plugging in a different provider.
        model: Model name. Defaults to OPENAI_MODEL or DEFAULT_MODEL.
        verbose: Print tool actions to stderr for terminal debugging.
    """

    def __init__(
        self,
        max_steps: int = 6,
        *,
        client: Any | None = None,
        model: str | None = None,
        verbose: bool = True,
    ) -> None:
        try:
            max_steps = int(max_steps)
        except (TypeError, ValueError) as exc:
            raise ValueError("max_steps must be an integer") from exc
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")

        self.max_steps = max_steps
        self.model = model or os.getenv("OPENAI_MODEL", DEFAULT_MODEL)
        self.verbose = verbose
        self.client = client if client is not None else _create_openai_client()

        # The full conversation context lives here. Do not replace this with a
        # single prompt string: tool calls and tool results must remain in order
        # for subsequent model requests to be valid.
        self.messages: list[dict[str, Any]] = [
            {"role": "system", "content": SYSTEM_PROMPT}
        ]
        self.trace: list[dict[str, Any]] = []

    def reset(self) -> None:
        """Clear conversation history while keeping configuration."""

        self.messages = [{"role": "system", "content": SYSTEM_PROMPT}]
        self.trace = []

    def run(
        self,
        user_input: str,
        *,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        cancel_event: threading.Event | None = None,
    ) -> str:
        """Run one user request through the ReAct loop.

        Args:
            user_input: The user's new message.
            on_event: Optional callback receiving high-level event dictionaries.
            cancel_event: Optional threading.Event checked at model/tool
                boundaries. Setting it stops the run with AgentInterrupted.
        """

        if not isinstance(user_input, str) or not user_input.strip():
            raise ValueError("user_input must be a non-empty string")

        self.messages.append({"role": "user", "content": user_input})
        _emit_event(on_event, "thinking", "正在分析请求并规划下一步。", step=0)

        for step in range(1, self.max_steps + 1):
            _check_cancelled(cancel_event)
            _emit_event(
                on_event,
                "thinking",
                f"第 {step}/{self.max_steps} 轮：正在等待模型决定下一步。",
                step=step,
            )

            # On the final turn, omit tools. This keeps the hard turn limit while
            # giving the model a chance to summarize the evidence it already has.
            request: dict[str, Any] = {
                "model": self.model,
                "messages": self.messages,
            }
            if step < self.max_steps:
                request["tools"] = OPENAI_TOOLS
                request["tool_choice"] = "auto"

            response = self.client.chat.completions.create(**request)
            _check_cancelled(cancel_event)
            message = response.choices[0].message
            content = _message_text(getattr(message, "content", None))
            raw_tool_calls = getattr(message, "tool_calls", None) or []
            tool_calls = [_tool_call_dict(call) for call in raw_tool_calls]

            # Preserve the assistant's tool-call request in the conversation.
            # The following tool messages reference these call IDs.
            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            if tool_calls:
                assistant_message["tool_calls"] = tool_calls
            self.messages.append(assistant_message)

            if not tool_calls:
                answer = content or "The model returned an empty response."
                _emit_event(
                    on_event,
                    "final",
                    "Agent 已生成最终回答。",
                    content=answer,
                    step=step,
                )
                return answer

            # A provider should not emit tool calls when tools were omitted on
            # the final turn. Fail clearly rather than making an extra request.
            if step == self.max_steps:
                answer = content or (
                    f"Stopped after reaching max_steps={self.max_steps}. "
                    "The model did not produce a final answer."
                )
                _emit_event(
                    on_event,
                    "final",
                    "Agent 已达到最大轮次并返回当前结果。",
                    content=answer,
                    step=step,
                )
                return answer

            for call in tool_calls:
                _check_cancelled(cancel_event)
                call_id = str(call.get("id", ""))
                function = call.get("function", {}) or {}
                name = str(function.get("name", ""))
                raw_arguments = function.get("arguments", "{}")

                if self.verbose:
                    print(
                        f"[step {step}/{self.max_steps}] tool={name} args={raw_arguments}",
                        file=sys.stderr,
                    )

                arguments: dict[str, Any] = {}
                try:
                    parsed_arguments = json.loads(raw_arguments or "{}")
                    if not isinstance(parsed_arguments, dict):
                        raise ValueError("tool arguments must be a JSON object")
                    arguments = parsed_arguments
                except (json.JSONDecodeError, ValueError) as exc:
                    tool_result: Any = {
                        "error": f"Invalid tool arguments: {exc}",
                    }
                else:
                    _emit_event(
                        on_event,
                        "tool_call",
                        _describe_tool_call(name, arguments),
                        step=step,
                        tool=name,
                        arguments=arguments,
                    )
                    function_impl = TOOL_FUNCTIONS.get(name)
                    if function_impl is None:
                        tool_result = {"error": f"Unknown tool: {name}"}
                    else:
                        try:
                            tool_result = function_impl(**arguments)
                        except Exception as exc:  # Tool errors are model-visible.
                            tool_result = {
                                "error": f"{type(exc).__name__}: {exc}",
                            }

                self.trace.append(
                    {
                        "step": step,
                        "tool": name,
                        "arguments": arguments,
                        "result": tool_result,
                    }
                )

                is_error = isinstance(tool_result, dict) and "error" in tool_result
                _emit_event(
                    on_event,
                    "tool_result",
                    f"工具 {name} 执行完成。",
                    step=step,
                    tool=name,
                    arguments=arguments,
                    result=tool_result,
                    is_error=is_error,
                )
                _check_cancelled(cancel_event)

                tool_content = _clip_tool_message(result_to_text(tool_result))
                self.messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "name": name,
                        "content": tool_content,
                    }
                )

                if self.verbose:
                    preview = tool_content.replace("\n", " ")[:240]
                    print(f"[tool result] {preview}", file=sys.stderr)

        # Defensive fallback. The final iteration normally returns above because
        # tools are omitted on the last turn.
        answer = (
            f"Stopped after reaching max_steps={self.max_steps}. "
            "No final answer was produced."
        )
        _emit_event(
            on_event,
            "final",
            "Agent 已停止并返回当前状态。",
            content=answer,
            step=self.max_steps,
        )
        return answer


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Interactive ReAct agent")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=6,
        help="Maximum model turns per user request (default: 6)",
    )
    parser.add_argument(
        "--model",
        default=None,
        help="Model name; defaults to OPENAI_MODEL or gpt-4.1-mini",
    )
    parser.add_argument(
        "--workspace",
        default=".",
        help="Working directory for tools (default: current directory)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Do not print individual tool actions",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Terminal chat entry point.

    Run from the project root:
        python -m agent.core

    Type a request, press Enter, and the agent will use tools as needed. Type
    /exit or /quit to leave. Context is retained across inputs in the same
    process.
    """

    _load_dotenv_if_available()
    args = build_arg_parser().parse_args(argv)

    workspace = Path(args.workspace).expanduser().resolve()
    if not workspace.exists() or not workspace.is_dir():
        print(f"Workspace directory does not exist: {workspace}", file=sys.stderr)
        return 2
    os.chdir(workspace)

    try:
        agent = ReActAgent(
            max_steps=args.max_steps,
            model=args.model,
            verbose=not args.quiet,
        )
    except Exception as exc:
        print(f"Failed to start agent: {exc}", file=sys.stderr)
        return 1

    print(f"ReAct agent ready. model={agent.model} workspace={workspace}")
    print("Type /exit or /quit to stop.")

    while True:
        try:
            user_input = input("\nYou > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if not user_input:
            continue
        if user_input.lower() in {"/exit", "/quit"}:
            break

        try:
            answer = agent.run(user_input)
        except Exception as exc:
            print(f"Agent error: {type(exc).__name__}: {exc}", file=sys.stderr)
            continue
        print(f"\nAgent > {answer}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
